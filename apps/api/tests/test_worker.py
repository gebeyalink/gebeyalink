"""The worker process: the scheduled heartbeat and the application that runs it.

The worker is the first thing this repository runs that answers nobody, so these tests are about
the two facts an operator depends on rather than about a request. The heartbeat is written where
an operator (or the CI job) can read it, and it is written by the schedule rather than by the
test; the Celery application is configured to run that schedule against the same Redis the API
declares.

Nothing here starts the broker. The task body is an ordinary function reachable through the
application's registry, so it can be invoked directly and its effect asserted against the Redis
this run started - which is what the CI job does end to end with a real beat and a real worker.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from celery.beat import PersistentScheduler  # type: ignore[import-untyped]
from redis import Redis

from apps.api import worker
from apps.api.core.config import Settings

pytestmark = pytest.mark.integration


def test_the_heartbeat_records_liveness_and_counts_the_tick(
    redis_url: str, clean_store: None
) -> None:
    """The liveness key carries the moment and expires; the tick counter only goes up."""
    client = Redis.from_url(redis_url, decode_responses=True)
    try:
        first = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)

        worker.record_heartbeat(client, ttl_seconds=30, now=first)

        assert client.get(worker.HEARTBEAT_KEY) == first.isoformat()
        ttl = client.ttl(worker.HEARTBEAT_KEY)
        assert 0 < ttl <= 30
        assert client.get(worker.HEARTBEAT_TICKS_KEY) == "1"

        worker.record_heartbeat(client, ttl_seconds=30, now=first + timedelta(seconds=5))

        assert client.get(worker.HEARTBEAT_TICKS_KEY) == "2"
    finally:
        client.close()


def test_the_application_schedules_the_heartbeat_in_the_pilot_timezone(
    inert_settings: Settings,
) -> None:
    """The cadence is the setting, and the schedule is expressed on the operator's clock."""
    settings = inert_settings.model_copy(update={"worker_heartbeat_interval_seconds": 7})

    app = worker.build_celery(settings)

    assert app.conf.timezone == "Africa/Addis_Ababa"
    assert app.conf.broker_url == settings.redis_url
    entry = app.conf.beat_schedule[worker.HEARTBEAT_TASK_NAME]
    assert entry["task"] == worker.HEARTBEAT_TASK_NAME
    assert entry["schedule"] == 7


def test_the_application_schedules_the_notification_drain(
    inert_settings: Settings,
) -> None:
    """The drain is on the schedule, on the cadence the setting names.

    The queue's clock is itself — a message is due the moment it is written — so unlike the two
    sweeps there is no window for the task to wait on, and the cadence is the only thing that
    decides how long a trader waits for a message that is already owed.
    """
    settings = inert_settings.model_copy(update={"worker_notification_drain_interval_seconds": 11})

    app = worker.build_celery(settings)

    entry = app.conf.beat_schedule[worker.NOTIFICATION_DRAIN_TASK_NAME]
    assert entry["task"] == worker.NOTIFICATION_DRAIN_TASK_NAME
    assert entry["schedule"] == 11
    assert worker.NOTIFICATION_DRAIN_TASK_NAME in app.tasks


def test_the_application_wires_the_persistent_scheduler_to_the_configured_file(
    inert_settings: Settings,
) -> None:
    """The restart loses nothing if the scheduler writes where the deployment said to.

    The in-memory scheduler replans from now on every start; the persistent one reads its
    entries back. Which of those the worker gets, and where it keeps them, is configuration
    the same way the cadence is — set here, on the Settings the worker reads.
    """
    settings = inert_settings.model_copy(
        update={"worker_beat_schedule_path": "state/beat-schedule"}
    )

    app = worker.build_celery(settings)

    assert app.conf.beat_scheduler == "celery.beat:PersistentScheduler"
    assert app.conf.beat_schedule_filename == settings.worker_beat_schedule_path


def test_the_persistent_schedule_survives_a_new_scheduler_instance(
    tmp_path: Path, inert_settings: Settings
) -> None:
    """A fire is not forgotten by a restart; the next instance counts from where it was.

    Beat advances an entry by reserving it before the task is dispatched. The scheduler that
    comes back after the process that reserved it has gone must still see that fire — that is
    the whole point of writing the schedule to a file rather than keeping it in memory.
    """
    settings = inert_settings.model_copy(
        update={"worker_beat_schedule_path": str(tmp_path / "beat-schedule")}
    )
    app = worker.build_celery(settings)

    first = PersistentScheduler(app=app, schedule_filename=settings.worker_beat_schedule_path)
    first.reserve(first.schedule[worker.HEARTBEAT_TASK_NAME])
    first.close()

    reopened = PersistentScheduler(app=app, schedule_filename=settings.worker_beat_schedule_path)
    try:
        entry = reopened.schedule[worker.HEARTBEAT_TASK_NAME]
        assert entry.total_run_count == 1
    finally:
        reopened.close()


def test_a_worker_without_a_schedule_file_starts_fresh(
    tmp_path: Path, inert_settings: Settings
) -> None:
    """No file means a fresh schedule: the count begins at zero.

    The file is what carries the state. The negative case is the proof: with nothing to read,
    beat creates an entry that has never fired, exactly as the CI job demonstrates with a real
    restart.
    """
    settings = inert_settings.model_copy(
        update={"worker_beat_schedule_path": str(tmp_path / "beat-schedule")}
    )
    app = worker.build_celery(settings)

    scheduler = PersistentScheduler(app=app, schedule_filename=settings.worker_beat_schedule_path)
    try:
        entry = scheduler.schedule[worker.HEARTBEAT_TASK_NAME]
        assert entry.total_run_count == 0
    finally:
        scheduler.close()


def test_the_scheduled_task_writes_the_heartbeat(
    redis_url: str, clean_store: None, inert_settings: Settings
) -> None:
    """Invoking the registered task leaves the same side effect the schedule leaves."""
    settings = inert_settings.model_copy(
        update={"redis_url": redis_url, "worker_heartbeat_ttl_seconds": 30}
    )
    app = worker.build_celery(settings)

    app.tasks[worker.HEARTBEAT_TASK_NAME]()

    client = Redis.from_url(redis_url, decode_responses=True)
    try:
        assert client.get(worker.HEARTBEAT_KEY) is not None
        assert client.get(worker.HEARTBEAT_TICKS_KEY) == "1"
        ttl = client.ttl(worker.HEARTBEAT_KEY)
        assert 0 < ttl <= 30
    finally:
        client.close()


def test_the_worker_starts_with_its_scheduler_embedded(
    inert_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`main` starts a worker that also runs beat, on the pool that works everywhere.

    The scheduler is requested as the embedded-beat step rather than through Celery's `-B`
    flag, because that flag refuses to run on Windows and the worker is meant to run on a
    developer's laptop too. The pool is `solo`, the one executor both platforms share.
    """
    started: dict[str, object] = {}
    app = worker.build_celery(inert_settings)
    monkeypatch.setattr(worker, "get_settings", lambda: inert_settings)
    monkeypatch.setattr(worker, "build_celery", lambda settings: app)

    class FakeWorker:
        def __init__(self, **kwargs: object) -> None:
            started.update(kwargs)

        def start(self) -> None:
            started["started"] = True

    monkeypatch.setattr(app, "Worker", lambda **kwargs: FakeWorker(**kwargs))

    worker.main()

    assert started["beat"] is True
    assert started["pool_cls"] == "solo"
    assert started["loglevel"] == "INFO"
    assert started["started"] is True
