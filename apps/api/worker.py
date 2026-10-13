"""The worker process: a scheduler and an executor, over the API's own Redis.

The API is request-shaped: it does work only while somebody is waiting for an answer. Some work
has no request behind it — a heartbeat that says the machinery is alive, and the deadline
evaluations that used to run lazily on the read path. That work needs a process of its own, and
this is it. Today there are two such evaluations: the heartbeat, and the shipment sweep that
settles orders whose crop's holding window has closed without anybody asking.

Celery is the scheduler and the executor. The broker is the Redis the API already declares and
points at, so a deployment adds no datastore to run a worker: the same instance that holds
verification codes holds the queue, under a key namespace of its own. The schedule lives in the
application configuration, in the same *Settings* the API reads, so the worker and the API can
never disagree about which Redis they are talking to.

Three decisions are stated here rather than left to be inferred.

The pool is ``solo``. The work this process runs is a heartbeat and the deadline sweeps —
coarse-grained and infrequent, not throughput-bound. A single-threaded executor is simpler than a
prefork pool and, unlike prefork, it runs on the developer's Windows laptop as well as on the
Linux runner, so the CI job's commands are commands a person can run.

The beat scheduler keeps its schedule in a file the deployment points at. A heartbeat that
fires every interval does not need it, but the evaluators that follow it do: an in-memory
schedule replans from now on every restart, so a worker restarted just before a daily-run
time would re-decide whether that run already happened. The schedule is written where the
deployment says, and a restart reads the next-fire instead of inventing one.

The heartbeat is a key with a short TTL, not a row. Liveness is a fact about right now, not a
record to keep: a key whose absence means "no worker has run recently" answers the operator's
question and then forgets itself, where a row would accumulate a row per tick that nobody reads.
A second key counts ticks, because "alive" and "still working" are different questions and an
operator checking a deployment wants the second one.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from celery import Celery  # type: ignore[import-untyped]
from redis import Redis

from apps.api.core.config import Settings, get_settings
from apps.api.core.idempotency import IdempotencyStore
from apps.api.core.redis_client import get_client, init_client
from apps.api.core.short_codes import CrockfordOrderCode
from apps.api.db.session import get_session_factory, init_engine
from apps.api.repositories.listing_repository import ListingRepository
from apps.api.repositories.offer_repository import OfferRepository
from apps.api.repositories.order_repository import OrderRepository
from apps.api.repositories.reservation_repository import ReservationRepository
from apps.api.services.order_service import OrderService

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

#: The shipment sweep's name, shared by the schedule and the operator (or CI) that looks for its
#: effect, exactly as the heartbeat's name is shared by its own three readers. It is a task name
#: rather than an order's, because what it is due on is the worker's clock.
SHIPMENT_SWEEP_TASK_NAME = "gebeyalink.worker.shipment_sweep"


async def run_shipment_sweep(settings: Settings, *, now: datetime | None = None) -> int:
    """Settle the shipments whose holding window closed, and say how many did.

    One run, one session, and the decision is the order service's: `settle_lapsed_shipments` is
    the same delivery road the read path reaches when its own window closed, and building the
    service here is what lets the worker ask for it without a request behind the question.

    The negative case is answered by construction. An order that has already been delivered is
    not selected by the query, so a second run has nothing to settle and a stopped worker costs
    a delay rather than a settlement made twice.
    """
    init_engine(settings)
    init_client(settings)
    session_factory = get_session_factory()
    async with session_factory() as session:
        orders = OrderRepository(session)
        service = OrderService(
            orders=orders,
            listings=ListingRepository(session),
            offers=OfferRepository(session),
            reservations=ReservationRepository(session),
            fee_payer=settings.fee_payer,
            cod_enabled=settings.cod_enabled,
            escrow_enabled=settings.escrow_enabled,
            codes=CrockfordOrderCode(orders.short_code_exists),
            idempotency=IdempotencyStore(get_client()),
            tolerance_pct=settings.transit_shrinkage_tolerance_pct,
        )
        return await service.settle_lapsed_shipments(now)


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
        beat_scheduler="celery.beat:PersistentScheduler",
        beat_schedule_filename=settings.worker_beat_schedule_path,
        # The schedule file is the crash-safety story: a restart reads the next-fire from it.
        # Celery otherwise spills the store only every few minutes; syncing after every fire is
        # what keeps the on-disk state one tick behind at most, which is the whole point of
        # having written the schedule down in the first place.
        beat_sync_every=1,
        beat_schedule={
            HEARTBEAT_TASK_NAME: {
                "task": HEARTBEAT_TASK_NAME,
                "schedule": settings.worker_heartbeat_interval_seconds,
            },
            SHIPMENT_SWEEP_TASK_NAME: {
                "task": SHIPMENT_SWEEP_TASK_NAME,
                "schedule": settings.worker_shipment_sweep_interval_seconds,
            },
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

    def worker_shipment_sweep() -> None:
        """Settle the lapsed shipments. The body of the task the schedule runs.

        The work is asynchronous and this is a Celery task, so it opens a loop of its own for
        the length of the sweep. That is also why the task is registered with `shared=False`
        below: the closure's settings decide what its `asyncio.run` reads.
        """
        settled = asyncio.run(run_shipment_sweep(settings))
        logger.info("%s settled=%s", SHIPMENT_SWEEP_TASK_NAME, settled)

    # `shared=False` and `lazy=False` are load-bearing. Celery's defaults defer the registration
    # through a process-global finalize hook, so a second app built in the same process can end
    # up executing the first app's task object — the heartbeat would write to the first app's
    # Redis. This app registers its own task, closed over its own settings, the moment it is
    # built, and no other app's task can take the name in it.
    app.task(name=HEARTBEAT_TASK_NAME, shared=False, lazy=False)(worker_heartbeat)
    app.task(name=SHIPMENT_SWEEP_TASK_NAME, shared=False, lazy=False)(worker_shipment_sweep)
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
        "worker starting: heartbeat every %ss, TTL %ss, shipment sweep every %ss, "
        "beat schedule at %s",
        settings.worker_heartbeat_interval_seconds,
        settings.worker_heartbeat_ttl_seconds,
        settings.worker_shipment_sweep_interval_seconds,
        settings.worker_beat_schedule_path,
    )
    app.Worker(pool_cls="solo", loglevel="INFO", beat=True).start()


if __name__ == "__main__":  # pragma: no cover
    main()
