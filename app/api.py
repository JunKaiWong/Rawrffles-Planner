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

import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Path as PathParam, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.auth import InitDataError, TelegramUser, authorise_user
from app.config import PROJECT_ROOT, Settings, load_settings
from app.db.database import get_link, init_db, is_day_trip, list_links, update_link

logger = logging.getLogger(__name__)

INIT_DATA_HEADER = "X-Telegram-Init-Data"

# Paths that skip authentication. Deliberately tiny: the interactive docs must
# load before the user can authorise, and the health check must work for the
# host's uptime probe. Neither exposes any data.
PUBLIC_PATHS = frozenset({"/docs", "/redoc", "/openapi.json", "/health", "/docs/oauth2-redirect"})

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
    lat: float | None = None
    lng: float | None = None
    # True when the link is outside the home region: kept and browsable, but
    # excluded from MRT-based Saturday clustering.
    is_day_trip: bool = False
    parsed_at: str | None = None
    tags: str | None = None
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
            settings.db_path,
            sorted(settings.allowed_user_ids),
        )
        yield

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
