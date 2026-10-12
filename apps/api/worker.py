"""The worker process: a scheduler and an executor, over the API's own Redis.

The API is request-shaped: it does work only while somebody is waiting for an answer. Some work
has no request behind it — a heartbeat that says the machinery is alive, and later the
evaluations that used to run lazily on the read path. That work needs a process of its own, and
this is it.

Celery is the scheduler and the executor. The broker is the Redis the API already declares and
points at, so a deployment adds no datastore to run a worker: the same instance that holds
verification codes holds the queue, under a key namespace of its own. The schedule lives in the
application configuration, in the same *Settings* the API reads, so the worker and the API can
never disagree about which Redis they are talking to.

Three decisions are stated here rather than left to be inferred.

The pool is ``solo``. The work this process runs is a heartbeat and, later, deadline
evaluations — coarse-grained and infrequent, not throughput-bound. A single-threaded executor is
simpler than a prefork pool and, unlike prefork, it runs on the developer's Windows laptop as
well as on the Linux runner, so the CI job's commands are commands a person can run.

The beat scheduler keeps its schedule in memory. A heartbeat that fires every interval does not
need a schedule written to disk, and a file the scheduler owns is one more thing a container has
to be allowed to write and one more thing that can go stale.

The heartbeat is a key with a short TTL, not a row. Liveness is a fact about right now, not a
record to keep: a key whose absence means "no worker has run recently" answers the operator's
question and then forgets itself, where a row would accumulate a row per tick that nobody reads.
A second key counts ticks, because "alive" and "still working" are different questions and an
operator checking a deployment wants the second one.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from celery import Celery  # type: ignore[import-untyped]
from redis import Redis

from apps.api.core.config import Settings, get_settings

logger = logging.getLogger("gebeyalink.worker")

#: The scheduled task's name. Shared by the beat schedule, the worker that runs it, and the
#: operator (or CI) that looks for its effect, so the three cannot drift apart.
HEARTBEAT_TASK_NAME = "gebeyalink.worker.heartbeat"

#: The liveness signal: a key that is present while a worker can be trusted and gone once none
#: has run for the TTL. Its value is the moment the last heartbeat fired, readable by an
#: operator who wants to know when, not only whether.
HEARTBEAT_KEY = "gebeyalink:worker:heartbeat"

#: A monotonic count of heartbeats, without a TTL. Its value going up between two reads is the
#: difference between a worker that is up and one that is still ticking.
HEARTBEAT_TICKS_KEY = "gebeyalink:worker:ticks"


def record_heartbeat(client: Redis, *, ttl_seconds: int, now: datetime | None = None) -> datetime:
    """Write the liveness key and count the tick, in one round trip.

    Both writes go through a pipeline so a reader never sees a tick without its timestamp or a
    timestamp without its tick. The timestamp is returned so the task can log the moment it
    recorded rather than the moment it happened to log.
    """
    moment = now or datetime.now(UTC)
    pipeline = client.pipeline()
    pipeline.set(HEARTBEAT_KEY, moment.isoformat(), ex=ttl_seconds)
    pipeline.incr(HEARTBEAT_TICKS_KEY)
    pipeline.execute()
    return moment


def build_celery(settings: Settings) -> Celery:
    """The Celery application: a broker the API's Redis already is, and one scheduled task.

    The task is registered by name from here rather than decorated at module scope, because the
    task needs the settings and the settings are only known once the process has started. It
    closes over them, so the worker reads the same configuration object the API's ``main`` reads,
    resolved the same way.
    """
    app = Celery("gebeyalink", broker=settings.redis_url)
    app.conf.update(
        broker_url=settings.redis_url,
        result_backend=None,
        task_ignore_result=True,
        task_serializer="json",
        accept_content=["json"],
        # The schedule is expressed in local time for the pilot's operators, who run on the
        # Ethiopian clock. W1b's boundary test fixes the zone; this is where it is pinned.
        timezone="Africa/Addis_Ababa",
        enable_utc=True,
        beat_scheduler="celery.beat:Scheduler",
        beat_schedule={
            HEARTBEAT_TASK_NAME: {
                "task": HEARTBEAT_TASK_NAME,
                "schedule": settings.worker_heartbeat_interval_seconds,
            }
        },
    )

    def worker_heartbeat() -> None:
        """Write the liveness signal. The body of the task the schedule runs."""
        client = Redis.from_url(settings.redis_url, decode_responses=True)
        try:
            moment = record_heartbeat(client, ttl_seconds=settings.worker_heartbeat_ttl_seconds)
        finally:
            client.close()
        logger.info("%s fired at %s", HEARTBEAT_TASK_NAME, moment.isoformat())

    # `shared=False` and `lazy=False` are load-bearing. Celery's defaults defer the registration
    # through a process-global finalize hook, so a second app built in the same process can end
    # up executing the first app's task object — the heartbeat would write to the first app's
    # Redis. This app registers its own task, closed over its own settings, the moment it is
    # built, and no other app's task can take the name in it.
    app.task(name=HEARTBEAT_TASK_NAME, shared=False, lazy=False)(worker_heartbeat)
    return app


def main() -> None:
    """Start the worker with its scheduler embedded, the way the container and CI run it.

    The schedule runs through the same embedded-beat step the `-B` flag enables, but the step is
    requested by building the worker directly rather than by way of the command line: Celery
    refuses its `-B` flag on Windows outright, and the worker is meant to run on a developer's
    laptop as well as on the Linux runner. `pool_cls="solo"` is passed the same way, because the
    single-threaded pool is the one that runs on both platforms.
    """
    settings = get_settings()
    app = build_celery(settings)
    logger.info(
        "worker starting: heartbeat every %ss, TTL %ss",
        settings.worker_heartbeat_interval_seconds,
        settings.worker_heartbeat_ttl_seconds,
    )
    app.Worker(pool_cls="solo", loglevel="INFO", beat=True).start()


if __name__ == "__main__":  # pragma: no cover
    main()
