"""REST API for the Mini App.

Two endpoints for now: list links, and update one (done / rating / note).

Authentication is HTTP middleware rather than a per-route dependency, so it is
structurally impossible to add a route that forgets it - a new endpoint is
protected by existing before it is written. Only an explicit, small set of
paths is exempt (the docs and a health check), and that list lives here in one
place where it can be reviewed.

Every request must carry Telegram's signed initData in the
`X-Telegram-Init-Data` header. The middleware verifies the HMAC signature
against the bot token, rejects stale data, checks the user id against
ALLOWED_USER_IDS, and only then attaches the proven user to request.state.
Handlers therefore never see an unauthenticated caller.

Run locally:  python -m app.api        (then open http://127.0.0.1:8000/docs)
"""

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any

from telegram import Bot, Update

from calendar import monthrange
from datetime import date

from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Path as PathParam,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, Response
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.auth import InitDataError, TelegramUser, authorise_user
from app.config import PROJECT_ROOT, WEBHOOK, Settings, load_settings
from app.db.engine import describe
from app.db.database import (
    PHOTO_INTAKE,
    PHOTO_VISIT,
    add_date,
    add_link_photos,
    claim_update,
    delete_date,
    get_date,
    get_link,
    get_link_photo,
    get_plan,
    init_db,
    is_day_trip,
    list_calendar_notes,
    list_dates,
    list_link_photos,
    list_links,
    photos_by_link,
    prune_processed_updates,
    release_update,
    save_calendar_note,
    save_plan,
    set_setting,
    update_date,
    update_link,
)
from app.handlers.link_handler import photo_sizes
from app.handlers.plan_handler import next_saturday
from app.services import appsettings as app_settings
from app.services.planner import plan_date
from app.services.reminders import (
    AVAILABLE_MILESTONES,
    format_milestones,
    occurrences_in_month,
    parse_milestones,
    resolve,
    upcoming,
)

logger = logging.getLogger(__name__)

# Telegram's own sendPhoto ceiling. A constant rather than a setting because it
# is the provider's limit, not a preference: raising it here would only move
# the rejection from this handler to Telegram, with a worse error message.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

INIT_DATA_HEADER = "X-Telegram-Init-Data"

# Paths that skip authentication. Deliberately tiny: the interactive docs must
# load before the user can authorise, and the health check must work for the
# host's uptime probe. Neither exposes any data.
# Telegram posts updates here. It cannot send initData, so this path is exempt
# from the Mini App gate and protected instead by the secret token Telegram
# echoes back, plus the chat allowlist every handler already passes through.
TELEGRAM_WEBHOOK_PATH = "/telegram/webhook"

PUBLIC_PATHS = frozenset(
    {
        "/docs",
        "/redoc",
        "/openapi.json",
        "/health",
        "/docs/oauth2-redirect",
        TELEGRAM_WEBHOOK_PATH,
    }
)

# Prefixes that skip authentication. The Mini App's own HTML/CSS/JS must load
# before Telegram can hand it initData, so the static bundle is public - it
# contains no secrets and no data, only the code that then authenticates. Every
# /api/* route stays behind the gate.
PUBLIC_PREFIXES = ("/miniapp",)

MINIAPP_DIR = PROJECT_ROOT / "miniapp"

# Declared so Swagger UI shows an Authorize button and sends the header. This
# is documentation of the real check, not the check itself - the middleware
# enforces it regardless of what any route declares.
init_data_scheme = APIKeyHeader(
    name=INIT_DATA_HEADER,
    auto_error=False,
    description=(
        "Telegram WebApp initData, taken verbatim from "
        "window.Telegram.WebApp.initData."
    ),
)


class LinkOut(BaseModel):
    """A stored link as the Mini App sees it."""

    id: int
    url: str
    canonical_url: str | None = None
    platform: str
    title: str | None = None
    caption: str | None = None
    location: str | None = None
    region: str | None = None
    category: str | None = None
    subcategory: str | None = None
    lat: float | None = None
    lng: float | None = None
    # Why coordinates are missing, when they are: "ambiguous" and "not_found"
    # are outcomes, not the same as never having been looked up (NULL).
    geocode_status: str | None = None
    # True when the link is outside the home region: kept and browsable, but
    # excluded from MRT-based Saturday clustering.
    is_day_trip: bool = False
    parsed_at: str | None = None
    # Stored comma-separated in SQLite; exposed as a list so clients never
    # re-implement the split.
    tags: list[str] = []
    added_by: int
    added_at: str
    done: bool
    done_at: str | None = None
    done_by: int | None = None
    rating: int | None = None
    note: str | None = None
    # Telegram file_ids are deliberately NOT exposed. A client cannot do
    # anything with one except hand it back, and the bot token is what turns
    # an id into an image, so the photo is addressed by its row id and the
    # bytes come from the endpoint below.
    photos: list["PhotoOut"] = []
    event_start: str | None = None
    event_end: str | None = None
    is_evergreen: bool


class PhotoOut(BaseModel):
    """One photo attached to a link.

    `kind` is the whole point: 'intake' is a screenshot of the post, which the
    parser reads as data and which a card previews; 'visit' is a photo from
    the day, which is only ever shown back to the couple.
    """

    id: int
    kind: str  # intake | visit
    added_by: int | None = None
    added_at: str
    # Ready to use as a URL; the client never assembles paths itself.
    url: str
    thumb_url: str


class DateOut(BaseModel):
    """A date resolved against today."""

    id: int
    label: str
    # The stored date: for a recurring entry this is the original event.
    date: str
    recurrence: str  # once | monthly | yearly
    recurring: bool
    # When it next happens, which is what the Mini App counts down to.
    occurs_on: str
    days_until: int
    # Anniversary number for yearly dates, months elapsed for monthly ones.
    count: int | None = None
    # Which milestones this date announces at, resolved from its own setting or
    # the default. Editable per date rather than fixed in the source.
    milestones: list[int] = []


class DateIn(BaseModel):
    """Create or edit a date."""

    label: str = Field(min_length=1, max_length=120)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    recurrence: str = Field(default="once", pattern=r"^(once|monthly|yearly)$")
    # Null keeps the default schedule; [] means never announce.
    milestones: list[int] | None = None


class DatePatch(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    recurrence: str | None = Field(default=None, pattern=r"^(once|monthly|yearly)$")
    milestones: list[int] | None = None


class CalendarNoteOut(BaseModel):
    """One person's note for one day."""

    day: str
    note: str
    user_id: int
    author_name: str | None = None
    # Resolved per request: with two people, "mine" and "theirs" is all the
    # attribution the UI needs, and it works before anyone has a stored name.
    is_mine: bool
    milestones: list[int] = []


class DateOccurrenceOut(BaseModel):
    """A stored date falling inside the month being viewed.

    Read-only in the calendar: the Dates section stays the source of truth, so
    these are shown but never edited from the grid.
    """

    id: int
    label: str
    day: str
    recurrence: str
    count: int | None = None


class CalendarNoteIn(BaseModel):
    note: str = Field(
        default="",
        max_length=280,
        description="Free text for the day. An empty note clears the entry.",
    )
    # Per-entry, like dates: "remind me the day before this one" without
    # changing anything else.
    milestones: list[int] | None = None


class SettingsOut(BaseModel):
    """Values the couple can change themselves, with the defaults alongside so
    the UI can show what "unset" would mean."""

    max_stops: int
    cluster_radius_metres: int
    home_region: str
    defaults: dict[str, object] = {}


class SettingsIn(BaseModel):
    max_stops: int | None = Field(default=None, ge=1, le=12)
    cluster_radius_metres: int | None = Field(default=None, ge=200, le=50_000)
    home_region: str | None = Field(default=None, min_length=2, max_length=60)


class PlanRequest(BaseModel):
    """Which links to plan around.

    An empty or absent list means "everything eligible", which is the
    Plan-with-all button; a list is a deliberate selection and is honoured
    as given rather than re-clustered.
    """

    link_ids: list[int] | None = Field(
        default=None, description="Selected link ids, or null for all eligible links"
    )
    # Per-plan, not a preference: "put everything in this one", overriding the
    # configured stop limit for this request only.
    include_all: bool = Field(
        default=False,
        description="Include every eligible link, ignoring the max-stops setting",
    )


class PlanStopOut(BaseModel):
    # Null for a discovered venue: it has no saved post behind it.
    link_id: int | None = None
    title: str
    url: str | None = None
    location: str | None = None
    when: str | None = None
    why: str | None = None
    # "saved" (they chose it) or "discovered" (found nearby to fill a gap).
    # Kept distinct so the app can show what has been vetted and what has not.
    source: str = "saved"


class PlanOut(BaseModel):
    id: int | None = None
    week_of: str
    summary: str | None = None
    stops: list[PlanStopOut] = []
    text: str
    # Selected links that could not be used, with the reason, so the app can
    # say why rather than appearing to lose them.
    excluded: dict[int, str] = {}
    spread_metres: int = 0
    # Non-fatal notes about the plan that was produced, e.g. that it is longer
    # than one day realistically holds.
    warnings: list[str] = []


class LinkUpdate(BaseModel):
    """Partial update. Omitted fields are left untouched; sending null clears
    a field."""

    done: bool | None = Field(
        default=None,
        description="Mark visited or not. Sets/clears done_at and done_by automatically.",
    )
    rating: int | None = Field(
        default=None,
        ge=1,
        le=10,
        description="1-10. Feeds back into plan_date() so suggestions improve.",
    )
    note: str | None = Field(
        default=None,
        max_length=2000,
        description="Free text, e.g. 'go before 7pm or you queue'.",
    )


def _photo_out(row) -> PhotoOut:
    data = dict(row)
    photo_id = int(data["id"])
    base = f"/api/links/{int(data['link_id'])}/photos/{photo_id}"
    return PhotoOut(
        id=photo_id,
        kind=data["kind"],
        added_by=data.get("added_by"),
        added_at=data["added_at"],
        url=base,
        # Falls back to the full size server-side when no smaller one was
        # offered, so the client can always ask for a thumbnail.
        thumb_url=f"{base}?size=thumb",
    )


def _row_to_link(
    row, home_region: str | None = None, photos: list | None = None
) -> LinkOut:
    data: dict[str, Any] = dict(row)
    data["photos"] = [_photo_out(photo) for photo in (photos or [])]
    # SQLite stores booleans as integers.
    data["done"] = bool(data["done"])
    data["is_evergreen"] = bool(data["is_evergreen"])
    # Derived server-side so the rule lives in one place rather than being
    # re-implemented by every client.
    data["is_day_trip"] = is_day_trip(data.get("region"), home_region)
    raw_tags = data.get("tags") or ""
    data["tags"] = [tag for tag in (t.strip() for t in raw_tags.split(",")) if tag]
    return LinkOut(**data)


def _valid_date(value: str) -> None:
    try:
        date.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date must be a real calendar date, YYYY-MM-DD",
        ) from None


def _milestones_for_storage(milestones: list[int] | None) -> str | None:
    """None keeps the default schedule; [] stores "never announce"."""
    if milestones is None:
        return None
    invalid = [m for m in milestones if m not in AVAILABLE_MILESTONES]
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unsupported milestone(s) {invalid}; choose from {list(AVAILABLE_MILESTONES)}",
        )
    return format_milestones(milestones)


def _date_out(item) -> "DateOut":
    return DateOut(
        id=item.id,
        label=item.label,
        date=item.stored_date,
        recurrence=item.recurrence,
        recurring=item.recurring,
        occurs_on=item.occurs_on.isoformat(),
        days_until=item.days_until,
        count=item.count,
        milestones=list(item.milestones),
    )


def current_user(request: Request) -> TelegramUser:
    """Hand the middleware-verified user to a route.

    If this ever raises, the middleware did not run for this path - that is a
    bug in the exemption list, not a client error, so it fails closed loudly.
    """
    user = getattr(request.state, "user", None)
    if user is None:
        logger.error("route reached without an authenticated user: %s", request.url.path)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="authentication did not run for this route",
        )
    return user


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(settings.db_path)
        logger.info(
            "API ready: db=%s allowed_users=%s",
            describe(settings.db_path),
            sorted(settings.allowed_user_ids),
        )

        # In webhook mode the bot lives inside this same web service: one
        # process, one port, one deployment - which is what a free host with a
        # single web service can actually run.
        app.state.telegram_app = None
        if settings.transport == WEBHOOK:
            try:
                prune_processed_updates(settings.db_path)
            except Exception:
                logger.exception("could not prune processed updates")
            from app.bot import build_application

            telegram_app = build_application(settings)
            await telegram_app.initialize()
            await telegram_app.start()
            webhook_url = settings.webhook_url.rstrip("/") + TELEGRAM_WEBHOOK_PATH
            await telegram_app.bot.set_webhook(
                url=webhook_url,
                secret_token=settings.webhook_secret,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=False,
            )
            app.state.telegram_app = telegram_app
            logger.info("telegram webhook registered at %s", webhook_url)

        yield

        telegram_app = app.state.telegram_app
        if telegram_app is not None:
            await telegram_app.stop()
            await telegram_app.shutdown()
            logger.info("telegram application stopped")

    app = FastAPI(
        title="Couple's Planner API",
        description=(
            "Backend for the Telegram Mini App. Every endpoint requires a valid "
            f"`{INIT_DATA_HEADER}` header.\n\n"
            "**To try these endpoints:** click Authorize and paste the value of "
            "`window.Telegram.WebApp.initData` from the Mini App."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        """The single gate every request passes through."""
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)

        init_data = request.headers.get(INIT_DATA_HEADER, "")
        try:
            user = authorise_user(
                init_data,
                bot_token=settings.bot_token,
                allowed_user_ids=settings.allowed_user_ids,
            )
        except InitDataError as exc:
            # Deliberately uniform: do not tell a caller whether the signature
            # was wrong, stale, or simply not on the allowlist.
            logger.warning("rejected %s %s: %s", request.method, path, exc)
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "unauthorised"},
            )

        request.state.user = user
        logger.info("%s %s by user %s", request.method, path, user.id)
        return await call_next(request)

    @app.api_route(
        "/health",
        methods=["GET", "HEAD"],
        tags=["meta"],
        summary="Liveness probe (unauthenticated)",
    )
    async def health() -> dict[str, str]:
        """HEAD is answered as well as GET.

        FastAPI's @app.get registers GET alone, so a HEAD probe got a 405 and
        an uptime monitor read that as downtime. Free monitoring plans often
        only offer HEAD, and a keep-alive ping is the whole reason this route
        exists on a host that sleeps. Starlette discards the body for HEAD, so
        one handler serves both.
        """
        return {"status": "ok"}

    @app.post(
        TELEGRAM_WEBHOOK_PATH,
        tags=["meta"],
        summary="Telegram webhook (called by Telegram, not by the Mini App)",
        include_in_schema=False,
    )
    async def telegram_webhook(request: Request):
        """Feed one Telegram update into the bot.

        Authenticated by the secret token Telegram echoes back, which is the
        only credential it can carry. A wrong or missing token is refused
        before the body is parsed.
        """
        telegram_app = request.app.state.telegram_app
        if telegram_app is None:
            # Running in polling mode; nothing should be posting here.
            logger.warning("webhook called while not in webhook mode")
            return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"ok": False})

        if settings.webhook_secret:
            sent = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
            if not sent or not hmac.compare_digest(sent, settings.webhook_secret):
                logger.warning("webhook called with a bad secret token")
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN, content={"ok": False}
                )

        try:
            payload = await request.json()
        except Exception:
            logger.warning("webhook received a non-JSON body")
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST, content={"ok": False}
            )

        update = Update.de_json(payload, telegram_app.bot)
        update_id = getattr(update, "update_id", None)

        # Telegram redelivers an update it did not get a prompt 200 for, and on
        # a host that sleeps the first delivery routinely completes *after* the
        # retry was sent - so both ran and one message was saved twice. The id
        # is claimed before any work: whoever loses the race acknowledges and
        # stops, rather than repeating it.
        if update_id is not None:
            try:
                claimed = await asyncio.to_thread(
                    claim_update, settings.db_path, update_id
                )
            except Exception:
                # If the claim itself fails, process rather than drop: a
                # duplicate reply is a smaller harm than a lost message.
                logger.exception("could not claim update %s; processing anyway", update_id)
                claimed = True
            if not claimed:
                return {"ok": True, "skipped": "duplicate"}

        try:
            await telegram_app.process_update(update)
        except Exception:
            logger.exception("failed to process update %s", update_id)
            # At-least-once: release the claim and answer with a 5xx so
            # Telegram redelivers, rather than losing the message. A retry only
            # gets through because the claim was released; a genuine duplicate
            # still finds the claim held and is skipped.
            if update_id is not None:
                try:
                    retryable = await asyncio.to_thread(
                        release_update, settings.db_path, update_id
                    )
                except Exception:
                    logger.exception("could not release update %s", update_id)
                    retryable = False
                if retryable:
                    return JSONResponse(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        content={"ok": False, "retry": True},
                    )
                # Out of attempts: acknowledge, so an update that fails every
                # time stops being redelivered.
                logger.error(
                    "giving up on update %s after repeated failures", update_id
                )
        return {"ok": True}

    # Serve the Mini App from the same origin as the API so browser requests
    # need no CORS handling and Telegram loads one host.
    if MINIAPP_DIR.is_dir():
        app.mount(
            "/miniapp",
            StaticFiles(directory=MINIAPP_DIR, html=True),
            name="miniapp",
        )
        logger.info("serving Mini App from %s at /miniapp", MINIAPP_DIR)
    else:  # pragma: no cover - only if the directory is missing
        logger.warning("Mini App directory not found at %s", MINIAPP_DIR)

    def _fetch_date_out(date_id: int) -> DateOut:
        row = get_date(settings.db_path, date_id)
        item = resolve(row) if row is not None else None
        if item is None:
            # A one-off in the past resolves to nothing, but the caller still
            # needs the row back after saving it.
            data = dict(row)
            return DateOut(
                id=data["id"],
                label=data["label"],
                date=data["date"],
                recurrence=data.get("recurrence") or "once",
                recurring=bool(data.get("recurring")),
                occurs_on=data["date"],
                days_until=-1,
                milestones=list(parse_milestones(data.get("reminder_days"))),
            )
        return _date_out(item)

    @app.get(
        "/api/settings",
        response_model=SettingsOut,
        tags=["settings"],
        summary="Values the couple can change themselves",
        dependencies=[Depends(init_data_scheme)],
    )
    async def read_settings(
        user: Annotated[TelegramUser, Depends(current_user)],
    ) -> SettingsOut:
        current = await asyncio.to_thread(app_settings.load, settings.db_path)
        return SettingsOut(
            max_stops=current.max_stops,
            cluster_radius_metres=current.cluster_radius_metres,
            home_region=current.home_region,
            defaults=app_settings.defaults(),
        )

    @app.put(
        "/api/settings",
        response_model=SettingsOut,
        tags=["settings"],
        summary="Change a setting without a redeploy",
        dependencies=[Depends(init_data_scheme)],
    )
    async def write_settings(
        payload: SettingsIn,
        user: Annotated[TelegramUser, Depends(current_user)],
    ) -> SettingsOut:
        sent = payload.model_dump(exclude_unset=True, exclude_none=True)
        if not sent:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="nothing to change"
            )
        for key, value in sent.items():
            await asyncio.to_thread(
                set_setting, settings.db_path, key, str(value).strip()
            )
        # So the person who just changed it sees the new value immediately,
        # rather than up to the cache TTL later.
        app_settings.invalidate()
        logger.info("settings changed by user %s: %s", user.id, sorted(sent))
        current = await asyncio.to_thread(app_settings.load, settings.db_path)
        return SettingsOut(
            max_stops=current.max_stops,
            cluster_radius_metres=current.cluster_radius_metres,
            home_region=current.home_region,
            defaults=app_settings.defaults(),
        )

    @app.get(
        "/api/calendar",
        response_model=list[CalendarNoteOut],
        tags=["calendar"],
        summary="Shared calendar notes for a month",
        dependencies=[Depends(init_data_scheme)],
    )
    async def read_calendar(
        user: Annotated[TelegramUser, Depends(current_user)],
        month: Annotated[
            str, Query(pattern=r"^\d{4}-\d{2}$", description="Month as YYYY-MM")
        ],
    ) -> list[CalendarNoteOut]:
        """Both users' notes. The calendar is shared by design - seeing each
        other's week is the point."""
        year, month_number = (int(part) for part in month.split("-"))
        if not 1 <= month_number <= 12:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month must be between 01 and 12",
            )
        last_day = monthrange(year, month_number)[1]
        start = f"{year:04d}-{month_number:02d}-01"
        end = f"{year:04d}-{month_number:02d}-{last_day:02d}"

        rows = await asyncio.to_thread(list_calendar_notes, settings.db_path, start, end)
        return [
            CalendarNoteOut(
                day=dict(r)["day"],
                note=dict(r)["note"],
                user_id=dict(r)["user_id"],
                author_name=dict(r).get("author_name"),
                is_mine=dict(r)["user_id"] == user.id,
                milestones=list(parse_milestones(dict(r).get("reminder_days"))),
            )
            for r in rows
        ]

    @app.get(
        "/api/dates/in-month",
        response_model=list[DateOccurrenceOut],
        tags=["dates"],
        summary="Anniversaries and monthsaries falling inside one month",
        dependencies=[Depends(init_data_scheme)],
    )
    async def dates_in_month(
        user: Annotated[TelegramUser, Depends(current_user)],
        month: Annotated[
            str, Query(pattern=r"^\d{4}-\d{2}$", description="Month as YYYY-MM")
        ],
    ) -> list[DateOccurrenceOut]:
        """What the month view marks. Different question from /api/dates, which
        answers "when is this next" rather than "does it land in this month"."""
        year, month_number = (int(part) for part in month.split("-"))
        if not 1 <= month_number <= 12:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month must be between 01 and 12",
            )
        rows = await asyncio.to_thread(list_dates, settings.db_path)
        found = occurrences_in_month(rows, year, month_number)
        logger.debug("%d date occurrence(s) in %s for user %s", len(found), month, user.id)
        return [DateOccurrenceOut(**item) for item in found]

    @app.put(
        "/api/calendar/{day}",
        tags=["calendar"],
        summary="Add, change or clear your own note for a day",
        dependencies=[Depends(init_data_scheme)],
    )
    async def write_calendar(
        day: Annotated[str, PathParam(pattern=r"^\d{4}-\d{2}-\d{2}$")],
        payload: CalendarNoteIn,
        user: Annotated[TelegramUser, Depends(current_user)],
    ) -> dict[str, object]:
        """A note is always written against the authenticated caller, so one
        person cannot edit or clear the other's entry."""
        try:
            date.fromisoformat(day)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="day must be a real date, YYYY-MM-DD",
            ) from None

        outcome = await asyncio.to_thread(
            save_calendar_note,
            settings.db_path,
            user.id,
            day,
            payload.note,
            user.first_name,
            _milestones_for_storage(payload.milestones),
        )
        logger.info("calendar %s for %s by user %s", outcome, day, user.id)
        return {"ok": True, "day": day, "outcome": outcome}

    @app.post(
        "/api/plan",
        response_model=PlanOut,
        tags=["plan"],
        summary="Build a date plan from all eligible links, or from a selection",
        dependencies=[Depends(init_data_scheme)],
    )
    async def create_plan(
        payload: PlanRequest,
        user: Annotated[TelegramUser, Depends(current_user)],
    ) -> PlanOut:
        """Same plan_date() the scheduled job uses, so the two cannot drift.

        The result is filed immediately: it is a generated plan either way, and
        storing it here means posting it later needs only an id rather than
        trusting text sent back from the client.
        """
        logger.info(
            "plan requested by user %s for %s",
            user.id,
            f"{len(payload.link_ids)} selected link(s)" if payload.link_ids else "all eligible links",
        )
        rows = await asyncio.to_thread(list_links, settings.db_path)
        plan = await asyncio.to_thread(
            lambda: plan_date(
                rows,
                settings.gemini_api_key,
                settings.gemini_model,
                link_ids=payload.link_ids,
                settings=settings,
                include_all=payload.include_all,
            )
        )
        if not plan.ok:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": plan.error, "excluded": plan.excluded},
            )

        week_of = next_saturday().isoformat()
        text = plan.render()
        plan_id = await asyncio.to_thread(save_plan, settings.db_path, week_of, text)
        return PlanOut(
            id=plan_id,
            week_of=week_of,
            summary=plan.summary,
            stops=[
                PlanStopOut(
                    link_id=s.link_id,
                    title=s.title,
                    url=s.url,
                    location=s.location,
                    when=s.when,
                    why=s.why,
                    source=s.source,
                )
                for s in plan.stops
            ],
            text=text,
            excluded=plan.excluded,
            spread_metres=plan.spread_metres,
            warnings=plan.warnings,
        )

    @app.post(
        "/api/plans/{plan_id}/post",
        tags=["plan"],
        summary="Send an already-generated plan to the group chat",
        dependencies=[Depends(init_data_scheme)],
    )
    async def post_plan(
        plan_id: Annotated[int, PathParam(ge=1)],
        user: Annotated[TelegramUser, Depends(current_user)],
    ) -> dict[str, object]:
        """Post a stored plan.

        The text comes from the stored row, not from the request, so what
        reaches the group is exactly what was generated and shown - a client
        cannot use this to broadcast arbitrary text to the chat.
        """
        stored = await asyncio.to_thread(get_plan, settings.db_path, plan_id)
        if stored is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"plan {plan_id} not found"
            )
        row = dict(stored)
        message = f"Plan for Saturday {row['week_of']}:\n{row['summary']}"

        bot = Bot(settings.bot_token)
        try:
            async with bot:
                await bot.send_message(
                    chat_id=settings.chat_id,
                    text=message,
                    disable_web_page_preview=True,
                )
        except Exception as exc:
            logger.exception("could not post plan %s to the group", plan_id)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"could not post to the group: {exc}",
            ) from exc

        logger.info("plan %s posted to the group by user %s", plan_id, user.id)
        return {"ok": True, "plan_id": plan_id}

    @app.get(
        "/api/dates",
        response_model=list[DateOut],
        tags=["dates"],
        summary="Upcoming anniversaries and important dates",
        dependencies=[Depends(init_data_scheme)],
    )
    async def read_dates(
        user: Annotated[TelegramUser, Depends(current_user)],
    ) -> list[DateOut]:
        """Soonest first, with past one-offs already dropped.

        The next occurrence is computed here rather than stored, because a
        recurring date has no single future value to keep in a column.
        """
        rows = await asyncio.to_thread(list_dates, settings.db_path)
        items = upcoming(rows)
        logger.debug("returning %d upcoming date(s) to user %s", len(items), user.id)
        return [_date_out(item) for item in items]

    @app.post(
        "/api/dates",
        response_model=DateOut,
        tags=["dates"],
        summary="Add an anniversary, monthsary or one-off date",
        dependencies=[Depends(init_data_scheme)],
    )
    async def create_date(
        payload: DateIn,
        user: Annotated[TelegramUser, Depends(current_user)],
    ) -> DateOut:
        _valid_date(payload.date)
        date_id = await asyncio.to_thread(
            add_date,
            settings.db_path,
            payload.label.strip(),
            payload.date,
            payload.recurrence,
            _milestones_for_storage(payload.milestones),
        )
        logger.info("date %s added by user %s", date_id, user.id)
        return _fetch_date_out(date_id)

    @app.patch(
        "/api/dates/{date_id}",
        response_model=DateOut,
        tags=["dates"],
        summary="Edit a date, including which milestones it announces at",
        dependencies=[Depends(init_data_scheme)],
    )
    async def edit_date(
        date_id: Annotated[int, PathParam(ge=1)],
        payload: DatePatch,
        user: Annotated[TelegramUser, Depends(current_user)],
    ) -> DateOut:
        if payload.date:
            _valid_date(payload.date)
        sent = payload.model_dump(exclude_unset=True)
        updated = await asyncio.to_thread(
            update_date,
            settings.db_path,
            date_id,
            payload.label.strip() if payload.label else None,
            payload.date,
            payload.recurrence,
            _milestones_for_storage(payload.milestones),
            "milestones" in sent,
        )
        if updated is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"date {date_id} not found"
            )
        logger.info("date %s edited by user %s: %s", date_id, user.id, sorted(sent))
        return _fetch_date_out(date_id)

    @app.delete(
        "/api/dates/{date_id}",
        tags=["dates"],
        summary="Remove a date",
        dependencies=[Depends(init_data_scheme)],
    )
    async def remove_date(
        date_id: Annotated[int, PathParam(ge=1)],
        user: Annotated[TelegramUser, Depends(current_user)],
    ) -> dict[str, object]:
        removed = await asyncio.to_thread(delete_date, settings.db_path, date_id)
        if not removed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"date {date_id} not found"
            )
        logger.info("date %s deleted by user %s", date_id, user.id)
        return {"ok": True, "id": date_id}

    @app.get(
        "/api/links",
        response_model=list[LinkOut],
        tags=["links"],
        summary="List every stored link",
        dependencies=[Depends(init_data_scheme)],
    )
    async def read_links(
        user: Annotated[TelegramUser, Depends(current_user)],
    ) -> list[LinkOut]:
        """Newest first. The Mini App splits these into To visit / Done itself,
        so both are returned together."""
        rows = list_links(settings.db_path)
        home = app_settings.load(settings.db_path).home_region
        # One query for every photo rather than one per card.
        grouped = photos_by_link(settings.db_path)
        logger.debug("returning %d link(s) to user %s", len(rows), user.id)
        return [
            _row_to_link(row, home, grouped.get(int(dict(row)["id"]), []))
            for row in rows
        ]

    @app.patch(
        "/api/links/{link_id}",
        response_model=LinkOut,
        tags=["links"],
        summary="Update a link's done status, rating, or note",
        dependencies=[Depends(init_data_scheme)],
    )
    async def patch_link(
        link_id: Annotated[int, PathParam(ge=1, description="Link id")],
        payload: LinkUpdate,
        user: Annotated[TelegramUser, Depends(current_user)],
    ) -> LinkOut:
        # exclude_unset distinguishes "field omitted" from "field set to null":
        # omitting leaves the value alone, sending null clears it.
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="no fields to update",
            )

        if get_link(settings.db_path, link_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"link {link_id} not found",
            )

        row = update_link(
            settings.db_path, link_id, changes, acting_user_id=user.id
        )
        if row is None:  # deleted between the check and the write
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"link {link_id} not found",
            )
        return _row_to_link(
            row, photos=list_link_photos(settings.db_path, link_id)
        )

    @app.get(
        "/api/links/{link_id}/photos/{photo_id}",
        tags=["links"],
        summary="Stream one photo's bytes",
        response_class=Response,
        responses={200: {"content": {"image/jpeg": {}}}},
        dependencies=[Depends(init_data_scheme)],
    )
    async def read_link_photo(
        link_id: Annotated[int, PathParam(ge=1)],
        photo_id: Annotated[int, PathParam(ge=1)],
        user: Annotated[TelegramUser, Depends(current_user)],
        size: Annotated[str, Query(pattern="^(full|thumb)$")] = "full",
    ) -> Response:
        """Fetch the image from Telegram and return the bytes.

        This has to be a proxy. Turning a file_id into an image needs the bot
        token, and a URL carrying that token would let anyone holding it read
        every file the bot has ever seen - so the token stays server-side and
        the client only ever names a row.

        The consequence, which the Mini App has to live with, is that these
        bytes sit behind the same initData header as everything else, and an
        <img src> cannot send a header. The client fetches them and renders a
        blob URL instead.

        Nothing is stored: Telegram re-serves the image on demand, and the
        response carries a private cache header so re-rendering a card does
        not ask twice.
        """
        row = await asyncio.to_thread(get_link_photo, settings.db_path, photo_id)
        # Right photo but wrong link is a 404, not a redirect: the pair of ids
        # names one thing or nothing.
        if row is None or int(dict(row)["link_id"]) != link_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"photo {photo_id} not found on link {link_id}",
            )
        data = dict(row)
        file_id = data["file_id"]
        if size == "thumb" and data.get("thumb_file_id"):
            file_id = data["thumb_file_id"]

        bot = Bot(settings.bot_token)
        try:
            async with bot:
                telegram_file = await bot.get_file(file_id)
                content = bytes(await telegram_file.download_as_bytearray())
        except Exception as exc:
            logger.exception("could not fetch photo %s (file_id=%s)", photo_id, file_id)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"could not fetch the photo: {exc}",
            ) from exc

        path = (getattr(telegram_file, "file_path", "") or "").lower()
        media_type = "image/png" if path.endswith(".png") else "image/jpeg"
        logger.info(
            "served photo %s (%s, %d bytes) to user %s",
            photo_id,
            size,
            len(content),
            user.id,
        )
        return Response(
            content=content,
            media_type=media_type,
            # private: one couple's photo, not something a proxy on the way
            # should keep. immutable because a row's file_id never changes.
            headers={"Cache-Control": "private, max-age=86400, immutable"},
        )

    @app.post(
        "/api/links/{link_id}/photos",
        response_model=PhotoOut,
        status_code=status.HTTP_201_CREATED,
        tags=["links"],
        summary="Attach a photo from the visit",
        dependencies=[Depends(init_data_scheme)],
    )
    async def upload_link_photo(
        link_id: Annotated[int, PathParam(ge=1)],
        user: Annotated[TelegramUser, Depends(current_user)],
        file: Annotated[UploadFile, File(description="An image from the visit")],
    ) -> PhotoOut:
        """Store a photo taken on the day.

        Uploads are always 'visit'. An intake screenshot is model input and
        arrives through the bot, where the caption says which link it belongs
        to; nothing the Mini App uploads is ever shown to Gemini.

        **This posts the photo to the group.** Not a flourish - the Bot API
        mints a file_id only by sending the photo somewhere, and file_ids are
        what this app stores instead of image bytes. Since the alternative is
        holding bytes on a host with no persistent disk, the photo goes to the
        couple's own chat, captioned with the place.
        """
        link = await asyncio.to_thread(get_link, settings.db_path, link_id)
        if link is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"link {link_id} not found",
            )

        if not (file.content_type or "").startswith("image/"):
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"expected an image, got {file.content_type or 'no content type'}",
            )

        content = await file.read()
        if not content:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="that file was empty"
            )
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=(
                    f"that photo is {len(content) // (1024 * 1024)}MB; Telegram "
                    f"accepts up to {MAX_UPLOAD_BYTES // (1024 * 1024)}MB"
                ),
            )

        row = dict(link)
        place = row.get("title") or row.get("location") or row["url"]
        caption = f"\U0001f4f8 {place}"

        bot = Bot(settings.bot_token)
        try:
            async with bot:
                sent = await bot.send_photo(
                    chat_id=settings.chat_id, photo=content, caption=caption
                )
        except Exception as exc:
            logger.exception("could not relay a photo for link %s", link_id)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Telegram would not accept the photo: {exc}",
            ) from exc

        sizes = photo_sizes(sent)
        if sizes is None:
            # sendPhoto succeeded but returned no sizes: nothing to store.
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Telegram accepted the photo but returned no file id",
            )

        added = await asyncio.to_thread(
            add_link_photos, settings.db_path, link_id, [sizes], PHOTO_VISIT, user.id
        )
        if not added:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="that photo is already attached to this link",
            )

        stored = [
            r
            for r in await asyncio.to_thread(
                list_link_photos, settings.db_path, link_id, PHOTO_VISIT
            )
            if dict(r)["file_id"] == sizes[0]
        ]
        logger.info(
            "user %s attached a visit photo to link %s (%d bytes)",
            user.id,
            link_id,
            len(content),
        )
        return _photo_out(stored[-1])

    return app


# Deliberately no module-level `app = create_app()`: that would read settings at
# import time, which breaks tests that supply their own. Serve it with
#   uvicorn app.api:create_app --factory
# or just `python -m app.api`.


def main() -> None:
    import uvicorn

    from app.bot import setup_logging

    setup_logging()
    settings = load_settings()
    logger.info("starting API on http://127.0.0.1:8000 (docs at /docs)")
    uvicorn.run(create_app(settings), host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
