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

from fastapi import Depends, FastAPI, HTTPException, Path as PathParam, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.auth import InitDataError, TelegramUser, authorise_user
from app.config import PROJECT_ROOT, WEBHOOK, Settings, load_settings
from app.db.engine import describe
from app.db.database import (
    get_link,
    get_plan,
    init_db,
    is_day_trip,
    list_dates,
    list_links,
    save_plan,
    update_link,
)
from app.handlers.plan_handler import next_saturday
from app.services.planner import CLUSTER_RADIUS_METRES, plan_date
from app.services.reminders import upcoming

logger = logging.getLogger(__name__)

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
    photo_file_id: str | None = None
    event_start: str | None = None
    event_end: str | None = None
    is_evergreen: bool


class DateOut(BaseModel):
    """An anniversary or one-off date, resolved against today."""

    id: int
    label: str
    # The stored date: for a recurring entry this is the original event.
    date: str
    recurring: bool
    # When it next happens, which is what the Mini App counts down to.
    occurs_on: str
    days_until: int
    years: int | None = None


class PlanRequest(BaseModel):
    """Which links to plan around.

    An empty or absent list means "everything eligible", which is the
    Plan-with-all button; a list is a deliberate selection and is honoured
    as given rather than re-clustered.
    """

    link_ids: list[int] | None = Field(
        default=None, description="Selected link ids, or null for all eligible links"
    )


class PlanStopOut(BaseModel):
    link_id: int
    title: str
    url: str
    location: str | None = None
    when: str | None = None
    why: str | None = None


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


def _row_to_link(row) -> LinkOut:
    data: dict[str, Any] = dict(row)
    # SQLite stores booleans as integers.
    data["done"] = bool(data["done"])
    data["is_evergreen"] = bool(data["is_evergreen"])
    # Derived server-side so the rule lives in one place rather than being
    # re-implemented by every client.
    data["is_day_trip"] = is_day_trip(data.get("region"))
    raw_tags = data.get("tags") or ""
    data["tags"] = [tag for tag in (t.strip() for t in raw_tags.split(",")) if tag]
    return LinkOut(**data)


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

    @app.get("/health", tags=["meta"], summary="Liveness probe (unauthenticated)")
    async def health() -> dict[str, str]:
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
        # Telegram retries on a non-200, so failures are logged and swallowed
        # rather than causing the same bad update to be redelivered forever.
        try:
            await telegram_app.process_update(update)
        except Exception:
            logger.exception("failed to process update %s", getattr(update, "update_id", "?"))
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
            plan_date,
            rows,
            settings.gemini_api_key,
            settings.gemini_model,
            None,
            CLUSTER_RADIUS_METRES,
            payload.link_ids,
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
                )
                for s in plan.stops
            ],
            text=text,
            excluded=plan.excluded,
            spread_metres=plan.spread_metres,
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
        rows = list_dates(settings.db_path)
        items = upcoming(rows)
        logger.debug("returning %d upcoming date(s) to user %s", len(items), user.id)
        return [
            DateOut(
                id=item.id,
                label=item.label,
                date=item.stored_date,
                recurring=item.recurring,
                occurs_on=item.occurs_on.isoformat(),
                days_until=item.days_until,
                years=item.years,
            )
            for item in items
        ]

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
        logger.debug("returning %d link(s) to user %s", len(rows), user.id)
        return [_row_to_link(row) for row in rows]

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
        return _row_to_link(row)

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
