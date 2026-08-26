"""Environment-backed settings.

Secrets live in .env.planner (gitignored); .env.example is the empty template.
Override the file with ENV_FILE=... for tests or alternate deployments.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

POLLING = "polling"
WEBHOOK = "webhook"

# Confirmed against models.list() and a real generateContent call, not assumed.
# gemini-2.5-flash now 404s for newly issued keys, and gemini-3.7-flash exists
# but returns 503 under load often enough to be a poor default; this one has
# answered every call. Override with GEMINI_MODEL to move up.
DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"


@dataclass(frozen=True)
class Settings:
    """Everything the bot needs to start, already validated."""

    bot_token: str
    chat_id: int
    transport: str
    allowed_user_ids: frozenset[int] = frozenset()
    webhook_url: str | None = None
    webhook_port: int = 8443
    webhook_secret: str | None = None
    # Either a Postgres URL or a path to the SQLite file. SQLite remains the
    # local default and the fallback; the deployed host has no persistent disk,
    # so it must be given a DATABASE_URL.
    db_path: Path | str = PROJECT_ROOT / "planner.db"
    sqlite_path: Path = PROJECT_ROOT / "planner.db"
    gemini_api_key: str | None = None
    gemini_model: str = DEFAULT_GEMINI_MODEL


def _require(name: str, env_file: str) -> str:
    """Read a required setting, or explain precisely what is missing.

    Settings come from the process environment, which the env file merely
    populates when one exists. Naming only the file was actively misleading in
    CI and on the host, where there is no file and the value is expected to
    come from repository secrets or the dashboard - it sent people looking for
    a file that was never supposed to be there.
    """
    value = (os.getenv(name) or "").strip()
    if value:
        return value

    env_path = PROJECT_ROOT / env_file
    if env_path.exists():
        where = (
            f"It is not set in the environment, and {env_file} exists but does "
            f"not define it (or defines it as empty)."
        )
    else:
        where = (
            f"It is not set in the environment, and there is no {env_file} to "
            f"read it from.\n"
            "Running in CI or on a host? Set it as a repository secret / "
            "dashboard environment variable.\n"
            "Running locally? Add it to .env.planner, or point ENV_FILE at the "
            "file that has it."
        )
    raise RuntimeError(f"{name} is required but missing.\n{where}")


def _parse_user_ids(raw: str) -> frozenset[int]:
    """Parse the comma-separated ALLOWED_USER_IDS list.

    A malformed entry raises rather than being skipped: silently dropping an id
    would quietly lock a real user out, and silently keeping a bad one would be
    worse.
    """
    ids = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError as exc:
            raise RuntimeError(
                f"ALLOWED_USER_IDS contains a non-numeric entry: {chunk!r}"
            ) from exc
    return frozenset(ids)


def _validate_webhook_secret(secret: str) -> str:
    """Check the secret against Telegram's accepted alphabet.

    Telegram allows only A-Z, a-z, 0-9, underscore and hyphen, 1-256
    characters, and rejects setWebhook outright otherwise. Render's generated
    values are base64 and can contain +, / or =, so this fails at startup with
    an actionable message rather than at the first setWebhook call.
    """
    if len(secret) > 256:
        raise RuntimeError(
            f"WEBHOOK_SECRET is {len(secret)} characters; Telegram allows at most 256."
        )
    invalid = sorted(set(re.sub(r"[A-Za-z0-9_-]", "", secret)))
    if invalid:
        raise RuntimeError(
            "WEBHOOK_SECRET contains characters Telegram rejects: "
            f"{' '.join(repr(c) for c in invalid)}.\n"
            "Only A-Z, a-z, 0-9, underscore and hyphen are allowed.\n"
            "Generate a valid one with:\n"
            "  python -c \"import secrets; print(secrets.token_urlsafe(32))\"\n"
            "then set it as WEBHOOK_SECRET and redeploy."
        )
    return secret


def _resolve_database() -> Path | str:
    """DATABASE_URL wins when set; otherwise the local SQLite file.

    Keeping both means the same code runs locally with no server and on a host
    with no disk, and that the SQLite file stays a usable fallback after the
    migration rather than becoming dead weight.
    """
    url = (os.getenv("DATABASE_URL") or "").strip()
    if url:
        return url
    return Path(os.getenv("DATABASE_PATH") or PROJECT_ROOT / "planner.db")


def load_settings(env_file: str | None = None) -> Settings:
    """Read and validate settings. Fails loudly at startup rather than midway
    through the first update."""
    env_file = env_file or os.getenv("ENV_FILE", ".env.planner")
    load_dotenv(PROJECT_ROOT / env_file, override=False)

    chat_id_raw = _require("TELEGRAM_CHAT_ID", env_file)
    try:
        chat_id = int(chat_id_raw)
    except ValueError as exc:
        raise RuntimeError(
            f"TELEGRAM_CHAT_ID must be a number, got {chat_id_raw!r}"
        ) from exc

    transport = (os.getenv("TELEGRAM_TRANSPORT") or POLLING).strip().lower()
    if transport not in (POLLING, WEBHOOK):
        raise RuntimeError(
            f"TELEGRAM_TRANSPORT must be {POLLING!r} or {WEBHOOK!r}, got {transport!r}"
        )

    webhook_url = (os.getenv("WEBHOOK_URL") or "").strip() or None
    if transport == WEBHOOK and not webhook_url:
        raise RuntimeError("WEBHOOK_URL is required when TELEGRAM_TRANSPORT=webhook")

    webhook_secret = (os.getenv("WEBHOOK_SECRET") or "").strip() or None
    if webhook_secret:
        # Validated whenever set, not only in webhook mode, so a bad value is
        # caught before the deploy that would first use it.
        webhook_secret = _validate_webhook_secret(webhook_secret)
    elif transport == WEBHOOK:
        # The webhook endpoint is public and cannot receive initData, so this
        # token is the only thing proving an update came from Telegram. Without
        # it, anyone who learns the URL could forge updates that appear to come
        # from the allowlisted chat.
        raise RuntimeError(
            "WEBHOOK_SECRET is required when TELEGRAM_TRANSPORT=webhook: it is "
            "what proves an incoming update really came from Telegram.\n"
            "Generate one with:\n"
            '  python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )

    return Settings(
        bot_token=_require("TELEGRAM_BOT_TOKEN", env_file),
        chat_id=chat_id,
        transport=transport,
        webhook_url=webhook_url,
        webhook_port=int(os.getenv("WEBHOOK_PORT") or 8443),
        webhook_secret=webhook_secret,
        db_path=_resolve_database(),
        sqlite_path=Path(os.getenv("DATABASE_PATH") or PROJECT_ROOT / "planner.db"),
        allowed_user_ids=_parse_user_ids(os.getenv("ALLOWED_USER_IDS") or ""),
        # Absent key is not fatal: link capture still works, captions simply go
        # unparsed until one is configured.
        gemini_api_key=(os.getenv("GEMINI_API_KEY") or "").strip() or None,
        gemini_model=(os.getenv("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL).strip(),
    )
