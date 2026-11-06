"""The worker process: a scheduler and an executor, over the API's own Redis.

The API is request-shaped: it does work only while somebody is waiting for an answer. Some work
has no request behind it — a heartbeat that says the machinery is alive, and the deadline
evaluations that used to run lazily on the read path. That work needs a process of its own, and
this is it. Today there are seven such jobs: the heartbeat, the shipment sweep that settles
orders whose crop's holding window has closed without anybody asking, the pickup sweep that
settles a pickup whose day at the counter has passed, the notification drain that sends the
messages the queue's own clock has made due, the expiry reminder that warns a seller whose
listing is nearly over, the price board, computed while the pilot sleeps so the morning
reads a finished figure, and the saved-search digest, which tells a trader once a day what
arrived that matches a search they kept.

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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from celery import Celery  # type: ignore[import-untyped]
from celery.schedules import crontab  # type: ignore[import-untyped]
from redis import Redis
from sqlalchemy.exc import IntegrityError

from apps.api.core.config import Settings, get_settings
from apps.api.core.idempotency import IdempotencyStore
from apps.api.core.redis_client import get_client, init_client
from apps.api.core.short_codes import CrockfordOrderCode
from apps.api.db.session import get_session_factory, init_engine

# The worker's tasks touch the database, and a settlement writes rows whose foreign keys reach
# every other table: an order's seller is a user, its listing names a crop, a region, and a
# woreda. SQLAlchemy resolves those keys across the whole *MetaData* when it flushes, so a
# process whose own imports reached only some of the models fails at the first write with
# ``NoReferencedTableError``. That is what the shipment sweep did — it settled nothing, and
# only the heartbeat worked, because the heartbeat touches the store and not the database.
#
# Every model is loaded here, so the registry belongs to the process rather than to whichever
# task its imports happened to reach first. It is loaded at module scope, not inside ``main``,
# so every entry point that imports this module — the container's ``python -m``, the CI job, a
# test — starts with a complete registry. The imports are the models alone: the worker needs no
# router or service of its own, so ``apps.api.main`` would load everything the worker is not.
# The names are imported for the registration side effect, which is what the ``noqa`` marks;
# ``alembic/env.py`` assembles the same set for the migration, and the registry test beside
# the worker asserts the whole of it.
from apps.api.models import action_token as _action_token_model  # noqa: F401  (registers it)
from apps.api.models import agent_assignment as _agent_assignment_model  # noqa: F401
from apps.api.models import audit_log as _audit_log_model  # noqa: F401  (registers it)
from apps.api.models import crop as _crop_model  # noqa: F401  (registers the table)
from apps.api.models import kyc_case as _kyc_case_model  # noqa: F401  (registers it)
from apps.api.models import listing as _listing_model  # noqa: F401  (registers the table)
from apps.api.models import notification as _notification_model  # noqa: F401  (registers it)
from apps.api.models import offer as _offer_model  # noqa: F401  (registers the table)
from apps.api.models import order as _order_model  # noqa: F401  (registers the table)
from apps.api.models import price_index as _price_index_model  # noqa: F401  (registers it)
from apps.api.models import profile as _profile_model  # noqa: F401  (registers the table)
from apps.api.models import quantity_reservation as _reservation_model  # noqa: F401
from apps.api.models import region as _region_model  # noqa: F401  (registers the table)
from apps.api.models import saved_search as _saved_search_model  # noqa: F401  (registers it)
from apps.api.models import staff_session as _staff_session_model  # noqa: F401  (registers it)
from apps.api.models import staff_user as _staff_user_model  # noqa: F401  (registers it)
from apps.api.models import user as _user_model  # noqa: F401  (registers the table)
from apps.api.models import user_role as _user_role_model  # noqa: F401
from apps.api.models import woreda as _woreda_model  # noqa: F401  (registers the table)
from apps.api.models.crop import Crop
from apps.api.models.notification import Notification, NotificationChannel, NotificationStatus
from apps.api.models.region import Region
from apps.api.providers.sms import build_messaging_provider, log_recording_provider_startup
from apps.api.repositories.crop_repository import CropRepository
from apps.api.repositories.listing_repository import ListingRepository
from apps.api.repositories.notification_repository import NotificationRepository
from apps.api.repositories.offer_repository import OfferRepository
from apps.api.repositories.order_repository import OrderRepository
from apps.api.repositories.price_index_repository import PriceIndexRepository
from apps.api.repositories.reservation_repository import ReservationRepository
from apps.api.repositories.saved_search_repository import (
    SavedSearchDigest,
    SavedSearchRepository,
)
from apps.api.repositories.user_repository import UserRepository
from apps.api.services.notification_service import DrainReport, NotificationService
from apps.api.services.order_service import OrderService
from apps.api.services.pricing_service import PricingService
from packages.shared.i18n.languages import Language
from packages.shared.i18n.templates import render_template
from packages.shared.messaging.provider import MessagingProvider
from packages.shared.messaging.queues import SmsPriority, priority_rank

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

#: The pickup sweep's name, shared by the schedule and the operator (or CI) that looks for its
#: effect, the same way the shipment sweep's name is. Its clock is the pickup window, a fixed day
#: from the moment the seller said the goods were waiting.
PICKUP_SWEEP_TASK_NAME = "gebeyalink.worker.pickup_sweep"

#: The notification drain's name, shared by the schedule, the worker that runs it and whoever
#: looks for its effect. Its clock is the queue itself: a message is due the moment it is
#: written, so unlike the two sweeps this task has no window to wait on and runs on a cadence.
NOTIFICATION_DRAIN_TASK_NAME = "gebeyalink.worker.notification_drain"

#: The price board's name, shared by the schedule, the worker that runs it and whoever looks
#: for its effect, the same way the sweeps' names are. Its clock is the calendar rather than
#: an interval: the board is due once a day at two in the morning on the operator's clock, so
#: it is a crontab below and not every N seconds.
PRICE_INDEX_TASK_NAME = "gebeyalink.worker.price_index"

#: The expiry reminder's name, shared by the beat entry, the worker that runs it and whoever
#: looks for its effect. Its clock is each listing's own expiry, read at the lead times the
#: settings name.
EXPIRY_REMINDER_TASK_NAME = "gebeyalink.worker.expiry_reminder"

#: The saved-search digest's name, shared by the beat entry, the task and whoever looks for
#: its effect. Its clock is the calendar: one note per trader per day, at 18:00 EAT.
SAVED_SEARCH_DIGEST_TASK_NAME = "gebeyalink.worker.saved_search_digest"

#: The crop and region columns to read for each language, so a digest line names what the
#: trader asked about in the words they read. Oromo is nullable, which is why the lookups
#: below fall back to English.
_NAME_COLUMNS: dict[Language, str] = {
    Language.AM: "name_am",
    Language.OM: "name_om",
    Language.EN: "name_en",
}

_REGION_NAME_COLUMNS: dict[Language, str] = {
    Language.AM: "name_am",
    Language.OM: "name_om",
    Language.EN: "name_en",
}

#: The fixed offset EAT has kept since 1942. A fixed offset rather than ``zoneinfo``: the
#: Windows development environment does not carry the tz database, and the digest's day
#: boundary is a date, not a historical transition.
EAT_OFFSET = timedelta(hours=3)

#: The closing line when more searches matched than the five a digest names. It lives here,
#: not in the registry, because the registry's single ``saved_search_digest`` template has
#: exactly two placeholders (``count`` and ``rows``) and cannot conditionally add a sixth
#: line without a placeholder the SMS budget fixtures do not measure. This is authored
#: copy pending native-speaker review under O-3, exactly like the template's own variants.
_MORE_SEARCHES: dict[Language, str] = {
    Language.AM: "እና ተጨማሪ {count} ፍለጋዎች አዲስ ዕቃዎች አላቸው።",
    Language.OM: "fi {count} barbaadota biroo meeshaalee haaraa qabu.",
    Language.EN: "and {count} more searches with new listings.",
}

#: How many search lines one digest names. The note names five and summarises the rest, so a
#: trader with ten matches gets a message that fits a handset rather than ten paragraphs.
MAX_SEARCH_LINES = 5


def _crop_name(crop: Crop, language: Language) -> str:
    """The crop's name in the seller's language, or the English one it does not carry."""
    column = _NAME_COLUMNS.get(language, "name_en")
    return getattr(crop, column, None) or crop.name_en


def _region_name(region: Region, language: Language) -> str:
    """The region's name in the trader's language, falling back to the English one."""
    column = _REGION_NAME_COLUMNS.get(language, "name_en")
    return getattr(region, column, None) or region.name_en


def _eat_day(moment: datetime) -> str:
    """The EAT calendar day a moment falls on, as ``YYYY-MM-DD``.

    The dedup key is one digest per trader per EAT day, so the day is computed in EAT
    rather than in the server's timezone or UTC. A run at 23:30 UTC is already tomorrow in
    Addis, and the key must say so or two runs on either side of local midnight could both
    write.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (moment.astimezone(UTC) + EAT_OFFSET).date().isoformat()


def _saved_search_label(digest: SavedSearchDigest, language: Language) -> str:
    """One search's line, named as the trader asked for it.

    The line is a crop and a region, not a rendered sentence: the template's header already
    says these are new listings, and putting the count beside the thing it counts keeps the
    line short enough for a handset. A search with neither filter falls back to a short
    identity, because two unfiltered searches must still be two distinguishable lines.
    """
    crop = _crop_name(digest.crop, language) if digest.crop is not None else None
    region: str | None
    if digest.region is not None:
        region = _region_name(digest.region, language)
    else:
        region = digest.search.region_code
    if crop is not None and region is not None:
        label = f"{crop} ({region})"
    elif crop is not None:
        label = crop
    elif region is not None:
        label = region
    else:
        label = str(digest.search.id)[:8]
    return f"{label}: {digest.new_listing_count}"


def _more_searches_line(language: Language, count: int) -> str:
    """The closing line for searches the five-line cap left out."""
    template = _MORE_SEARCHES.get(language, _MORE_SEARCHES[Language.EN])
    return template.format(count=count)


def _render_saved_search_digest(
    language: Language, digest_lines: list[str], total_count: int
) -> tuple[str, str]:
    """The saved-search digest, and the language its words were actually written in."""
    return render_template(
        "saved_search_digest",
        language,
        count=str(total_count),
        rows="\n".join(digest_lines),
    )


@dataclass(frozen=True, slots=True)
class _RenderedDigest:
    """One trader's finished digest, detached from the rows that produced it.

    The worker commits (or rolls a collision back) once per trader. Plain values here mean a
    rollback cannot leave the next group's ORM objects expired and waiting for a lazy load
    that an async session cannot perform.
    """

    user_id: UUID
    rendered_text: str
    rendered_language: str
    search_ids: tuple[UUID, ...]
    push_fell_back: bool


def _render_listing_expiring(
    language: Language, *, crop: str, days: int, listing_code: str
) -> tuple[str, str]:
    """FR-307's reminder, and the language its words were actually written in."""
    return render_template(
        "listing_expiring",
        language,
        crop=crop,
        days=str(days),
        listing_code=listing_code,
    )


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
            notifications=NotificationRepository(session),
            users=UserRepository(session),
            crops=CropRepository(session),
            fee_payer=settings.fee_payer,
            cod_enabled=settings.cod_enabled,
            escrow_enabled=settings.escrow_enabled,
            codes=CrockfordOrderCode(orders.short_code_exists),
            idempotency=IdempotencyStore(get_client()),
            tolerance_pct=settings.transit_shrinkage_tolerance_pct,
        )
        return await service.settle_lapsed_shipments(now)


async def run_pickup_sweep(settings: Settings, *, now: datetime | None = None) -> int:
    """Settle the pickups whose confirmation window closed, and say how many did.

    One run, one session, and the decision is the order service's: `settle_lapsed_pickups` is
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
            notifications=NotificationRepository(session),
            users=UserRepository(session),
            crops=CropRepository(session),
            fee_payer=settings.fee_payer,
            cod_enabled=settings.cod_enabled,
            escrow_enabled=settings.escrow_enabled,
            codes=CrockfordOrderCode(orders.short_code_exists),
            idempotency=IdempotencyStore(get_client()),
            tolerance_pct=settings.transit_shrinkage_tolerance_pct,
        )
        return await service.settle_lapsed_pickups(now)


async def run_expiry_reminder(settings: Settings, *, now: datetime | None = None) -> int:
    """Queue the reminders a listing nearing expiry owes its seller, and say how many.

    One run, one session. For each lead time in the settings the query names the active
    listings whose expiry falls inside that lead time's window and that carry no open offer;
    each of those is written into the notification queue, not sent directly, so the drain
    remains the one road a message takes to a handset.

    The dedup key is the listing and the lead time, so a second sweep during the same window
    is refused by the unique constraint rather than writing a second reminder. The insert is
    wrapped in a savepoint so that refusal costs one row rather than the whole sweep, and a
    listing with no seller or crop is logged and passed over rather than aborting the rest.

    ``expiry_reminder_enabled`` is read before the engine is touched: a deployment that has
    switched the reminder off asks for no work and gets none.
    """
    if not settings.expiry_reminder_enabled:
        return 0

    init_engine(settings)
    moment = now or datetime.now(UTC)
    sweep = timedelta(seconds=settings.worker_expiry_reminder_interval_seconds)
    session_factory = get_session_factory()
    written = 0

    async with session_factory() as session:
        listings = ListingRepository(session)
        users = UserRepository(session)
        crops = CropRepository(session)
        notifications = NotificationRepository(session)

        for lead_hour in settings.expiry_reminder_lead_hours:
            target = moment + timedelta(hours=lead_hour)
            listing_ids = await listings.list_expiring_within_hours(
                lead_hour,
                target - sweep,
                target + sweep,
            )
            for listing_id in listing_ids:
                loaded = await listings.find_by_id(listing_id)
                if loaded is None:
                    continue
                listing = loaded.listing
                seller = await users.find_by_id(listing.seller_id)
                if seller is None:
                    logger.error("expiry reminder seller missing listing_id=%s", listing_id)
                    continue
                crop = await crops.find_by_id(listing.crop_id)
                if crop is None:
                    logger.error("expiry reminder crop missing listing_id=%s", listing_id)
                    continue

                rendered_text, rendered_language = _render_listing_expiring(
                    seller.language,
                    crop=_crop_name(crop, seller.language),
                    days=lead_hour // 24,
                    listing_code=listing.short_code,
                )
                notification = Notification(
                    user_id=listing.seller_id,
                    channel=NotificationChannel.SMS,
                    template_id="listing_expiring",
                    priority=priority_rank(SmsPriority.Q3),
                    language=rendered_language,
                    rendered_text=rendered_text,
                    status=NotificationStatus.QUEUED,
                    dedup_key=f"listing_expiring:{listing_id}:{lead_hour}",
                )
                try:
                    async with session.begin_nested():
                        await notifications.add(notification)
                    written += 1
                except IntegrityError:
                    logger.info(
                        "expiry reminder already queued listing_id=%s lead_hour=%s",
                        listing_id,
                        lead_hour,
                    )
                except Exception:
                    logger.exception(
                        "expiry reminder failed listing_id=%s lead_hour=%s",
                        listing_id,
                        lead_hour,
                    )

        await session.commit()

    logger.info("expiry reminder wrote=%s", written)
    return written


async def run_notification_drain(
    settings: Settings,
    *,
    now: datetime | None = None,
    provider: MessagingProvider | None = None,
) -> DrainReport:
    """Send the messages that are due, in lane order, and report the tally.

    One run, one session, and the decision is the notification service's: a message is held,
    sent or failed by the same code a caller with a request behind it would reach. The
    provider is built from configuration when none is handed in, which is what lets a test
    pass a recorder it can read back; a worker builds the configured one.
    """
    init_engine(settings)
    init_client(settings)
    backend = provider or build_messaging_provider(settings.sms_provider)
    session_factory = get_session_factory()
    async with session_factory() as session:
        service = NotificationService(
            notifications=NotificationRepository(session),
            provider=backend,
        )
        return await service.drain(now)


async def run_saved_search_digest(settings: Settings, *, now: datetime | None = None) -> int:
    """Queue one saved-search digest per trader with new matches, and say how many.

    The run is one session and a sequence of per-trader transactions. For each active
    account whose digest-enabled searches have new listings since their own window, the
    worker renders one message, writes one notification row, and advances only the searches
    that message names. A trader with no matches gets no row at all.

    The dedup key is ``saved_search_digest:{user_id}:{YYYY-MM-DD EAT}``. A second run on the
    same EAT day loses the unique-constraint race and rolls back only that trader's insert,
    so a retried beat or two overlapping workers cannot send the same daily note twice. The
    insert and the window advance share a savepoint and a commit: either the trader has both
    a queued message and an advanced window, or neither.

    ``saved_search_digest_enabled`` is read before the engine is touched. A deployment that
    has switched the digest off asks for no work and gets none.
    """
    if not settings.saved_search_digest_enabled:
        return 0

    init_engine(settings)
    moment = now or datetime.now(UTC)
    session_factory = get_session_factory()
    written = 0

    async with session_factory() as session:
        searches = SavedSearchRepository(session)
        notifications = NotificationRepository(session)

        since_map = await searches.list_digest_since_map()
        if not since_map:
            logger.info("saved search digest candidates=0 written=0")
            return 0

        matches = await searches.list_saved_searches_with_new_matches(since_map)
        grouped: dict[UUID, list[SavedSearchDigest]] = {}
        for digest in matches:
            grouped.setdefault(digest.user.id, []).append(digest)

        # Render first, without touching the session again. A collision later rolls a
        # transaction back, and plain values survive that where expired ORM rows would not.
        rendered: list[_RenderedDigest] = []
        for user_id, entries in grouped.items():
            language = entries[0].user.language
            total_count = sum(entry.new_listing_count for entry in entries)
            shown = entries[:MAX_SEARCH_LINES]
            lines = [_saved_search_label(entry, language) for entry in shown]
            remaining = len(entries) - len(shown)
            if remaining > 0:
                lines.append(_more_searches_line(language, remaining))
            text, rendered_language = _render_saved_search_digest(language, lines, total_count)
            rendered.append(
                _RenderedDigest(
                    user_id=user_id,
                    rendered_text=text,
                    rendered_language=rendered_language,
                    search_ids=tuple(entry.search.id for entry in entries),
                    push_fell_back=any(
                        entry.search.digest_channel is NotificationChannel.PUSH for entry in entries
                    ),
                )
            )

        for message in rendered:
            notification = Notification(
                user_id=message.user_id,
                # The platform has no push rail in this slice. A search that asked for PUSH
                # is still sent on SMS, and the row says what actually travelled rather
                # than what was requested.
                channel=NotificationChannel.SMS,
                template_id="saved_search_digest",
                priority=priority_rank(SmsPriority.Q3),
                language=message.rendered_language,
                rendered_text=message.rendered_text,
                status=NotificationStatus.QUEUED,
                dedup_key=(f"saved_search_digest:{message.user_id}:{_eat_day(moment)}"),
            )
            try:
                async with session.begin_nested():
                    await notifications.add(notification)
                    await searches.mark_digested(list(message.search_ids), moment)
                await session.commit()
                written += 1
                if message.push_fell_back:
                    logger.info(
                        "saved search digest push fallback to SMS user_id=%s",
                        message.user_id,
                    )
            except IntegrityError:
                await session.rollback()
                logger.info(
                    "saved search digest already queued user_id=%s day=%s",
                    message.user_id,
                    _eat_day(moment),
                )
            except Exception:
                await session.rollback()
                logger.exception("saved search digest failed user_id=%s", message.user_id)

    logger.info("saved search digest written=%s", written)
    return written


async def run_price_index(settings: Settings, *, now: datetime | None = None) -> int:
    """Compute and publish today's price board, and say how many pairs it wrote.

    One run, one session, and the arithmetic is the pricing service's: the same board no
    request computes, computed here because nothing asked for it yet and the traders of
    tomorrow morning are the ones who will. `now` is injectable so a test can put a window's
    edge under the clock without waiting a month for the calendar.

    Re-running is harmless by construction. The board is written as an upsert against the
    day's own unique row, so a crashed run that is retried or a second run that overlaps
    leaves one row rather than two — which is also why this task, unlike the sweeps, needs no
    idempotency store: the table already knows how to be asked the same question twice.
    """
    init_engine(settings)
    session_factory = get_session_factory()
    async with session_factory() as session:
        service = PricingService(
            orders=OrderRepository(session),
            listings=ListingRepository(session),
            price_indices=PriceIndexRepository(session),
        )
        return await service.compute_and_persist(now=now)


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
            PICKUP_SWEEP_TASK_NAME: {
                "task": PICKUP_SWEEP_TASK_NAME,
                "schedule": settings.worker_pickup_sweep_interval_seconds,
            },
            NOTIFICATION_DRAIN_TASK_NAME: {
                "task": NOTIFICATION_DRAIN_TASK_NAME,
                "schedule": settings.worker_notification_drain_interval_seconds,
            },
            EXPIRY_REMINDER_TASK_NAME: {
                "task": EXPIRY_REMINDER_TASK_NAME,
                "schedule": settings.worker_expiry_reminder_interval_seconds,
            },
            PRICE_INDEX_TASK_NAME: {
                "task": PRICE_INDEX_TASK_NAME,
                # A crontab rather than an interval, because the board is due on the
                # calendar and not every N seconds: two in the morning, on the Ethiopian
                # clock the schedule above is already expressed in. It fires before the
                # trading day opens, so the price a trader reads at dawn is the figure the
                # overnight window finished computing, and it is hours clear of the sweeps
                # so the run has the database to itself.
                "schedule": crontab(hour=2, minute=0),
            },
            SAVED_SEARCH_DIGEST_TASK_NAME: {
                "task": SAVED_SEARCH_DIGEST_TASK_NAME,
                # The one daily note a trader asked for. The schedule is already expressed
                # in Africa/Addis_Ababa above, so 18 here is 18:00 EAT; the setting moves
                # the hour without moving the zone.
                "schedule": crontab(hour=settings.saved_search_digest_hour_eat, minute=0),
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

    def worker_pickup_sweep() -> None:
        """Settle the lapsed pickups. The body of the task the schedule runs.

        The work is asynchronous and this is a Celery task, so it opens a loop of its own for
        the length of the sweep. That is also why the task is registered with `shared=False`
        below: the closure's settings decide what its `asyncio.run` reads.
        """
        settled = asyncio.run(run_pickup_sweep(settings))
        logger.info("%s settled=%s", PICKUP_SWEEP_TASK_NAME, settled)

    def worker_notification_drain() -> None:
        """Send the due notifications. The body of the task the schedule runs.

        Asynchronous work in a synchronous task, so it opens a loop of its own, and registered
        `shared=False` below for the same reason the sweeps are: this app's settings decide
        what the loop reads. The tally is logged whole, so a run that sent nothing and a run
        that never happened look different in the log.
        """
        report = asyncio.run(run_notification_drain(settings))
        logger.info(
            "%s sent=%s queued=%s failed=%s",
            NOTIFICATION_DRAIN_TASK_NAME,
            report.sent,
            report.queued,
            report.failed,
        )

    def worker_expiry_reminder() -> None:
        """Queue the reminders a listing nearing expiry owes its seller.

        The work is asynchronous and this is a Celery task, so it opens a loop of its own for
        the length of the sweep, and registered `shared=False` below for the same reason the
        other sweeps are: this app's settings decide what the loop reads. The count is logged
        whole, so a sweep that wrote nothing and one that never ran look different.
        """
        written = asyncio.run(run_expiry_reminder(settings))
        logger.info("%s wrote=%s", EXPIRY_REMINDER_TASK_NAME, written)

    def worker_price_index() -> None:
        """Publish today's price board. The body of the task the schedule runs.

        The work is asynchronous and this is a Celery task, so it opens a loop of its own for
        the length of the run, and registered `shared=False` below for the same reason the
        sweeps are: this app's settings decide what the loop reads. The pair count is logged
        whole, so a morning that published a board and a morning that found nothing to say
        look different in the log rather than equally silent.
        """
        pairs = asyncio.run(run_price_index(settings))
        logger.info("%s pairs=%s", PRICE_INDEX_TASK_NAME, pairs)

    def worker_saved_search_digest() -> None:
        """Queue the day's saved-search digests. The body of the task the schedule runs.

        Asynchronous work in a synchronous task, so it opens a loop of its own, and registered
        `shared=False` below for the same reason the other tasks are: this app's settings
        decide what the loop reads. The number of notes written is logged whole, so an
        evening that sent nothing and an evening that never ran look different.
        """
        written = asyncio.run(run_saved_search_digest(settings))
        logger.info("%s written=%s", SAVED_SEARCH_DIGEST_TASK_NAME, written)

    # `shared=False` and `lazy=False` are load-bearing. Celery's defaults defer the registration
    # through a process-global finalize hook, so a second app built in the same process can end
    # up executing the first app's task object — the heartbeat would write to the first app's
    # Redis. This app registers its own task, closed over its own settings, the moment it is
    # built, and no other app's task can take the name in it.
    app.task(name=HEARTBEAT_TASK_NAME, shared=False, lazy=False)(worker_heartbeat)
    app.task(name=SHIPMENT_SWEEP_TASK_NAME, shared=False, lazy=False)(worker_shipment_sweep)
    app.task(name=PICKUP_SWEEP_TASK_NAME, shared=False, lazy=False)(worker_pickup_sweep)
    app.task(name=NOTIFICATION_DRAIN_TASK_NAME, shared=False, lazy=False)(worker_notification_drain)
    app.task(name=EXPIRY_REMINDER_TASK_NAME, shared=False, lazy=False)(worker_expiry_reminder)
    app.task(name=PRICE_INDEX_TASK_NAME, shared=False, lazy=False)(worker_price_index)
    app.task(name=SAVED_SEARCH_DIGEST_TASK_NAME, shared=False, lazy=False)(
        worker_saved_search_digest
    )
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
    log_recording_provider_startup(settings.sms_provider, settings.environment)
    logger.info(
        "worker starting: heartbeat every %ss, TTL %ss, shipment sweep every %ss, "
        "pickup sweep every %ss, notification drain every %ss, expiry reminder every %ss, "
        "price board daily at 02:00, saved-search digest daily at %02d:00 EAT, "
        "beat schedule at %s",
        settings.worker_heartbeat_interval_seconds,
        settings.worker_heartbeat_ttl_seconds,
        settings.worker_shipment_sweep_interval_seconds,
        settings.worker_pickup_sweep_interval_seconds,
        settings.worker_notification_drain_interval_seconds,
        settings.worker_expiry_reminder_interval_seconds,
        settings.saved_search_digest_hour_eat,
        settings.worker_beat_schedule_path,
    )
    app.Worker(pool_cls="solo", loglevel="INFO", beat=True).start()


if __name__ == "__main__":  # pragma: no cover
    main()
