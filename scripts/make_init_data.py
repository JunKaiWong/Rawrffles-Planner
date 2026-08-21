"""Mint a correctly-signed initData string for local API testing.

DEVELOPMENT ONLY. Real initData comes from Telegram via
`window.Telegram.WebApp.initData` inside the Mini App. Until that frontend
exists there is no way to exercise the API's authenticated routes by hand, so
this signs one locally with the same bot token the server verifies against.

It grants no access an attacker could not already obtain with the bot token
itself - the token is the secret, and it never leaves .env.planner. Do not
deploy this or expose it over the network.

    python -m scripts.make_init_data              # first ALLOWED_USER_IDS entry
    python -m scripts.make_init_data --user-id 12345

Paste the output into the Authorize box at http://127.0.0.1:8000/docs.
"""

import argparse
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from app.config import load_settings


def make_init_data(user_id: int, bot_token: str, first_name: str = "Dev") -> str:
    fields = {
        "query_id": "AAF_local_dev",
        "user": json.dumps(
            {"id": user_id, "first_name": first_name, "username": "localdev"}
        ),
        "auth_date": str(int(time.time())),
    }
    data_check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    return urlencode(fields)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--user-id",
        type=int,
        default=None,
        help="Telegram user id to sign for (default: first ALLOWED_USER_IDS entry)",
    )
    args = parser.parse_args()

    settings = load_settings()
    user_id = args.user_id
    if user_id is None:
        if not settings.allowed_user_ids:
            raise SystemExit("ALLOWED_USER_IDS is empty - pass --user-id explicitly")
        user_id = sorted(settings.allowed_user_ids)[0]

    if user_id not in settings.allowed_user_ids:
        print(f"note: {user_id} is NOT in ALLOWED_USER_IDS, so the API will reject it\n")

    print(make_init_data(user_id, settings.bot_token))


if __name__ == "__main__":
    main()
