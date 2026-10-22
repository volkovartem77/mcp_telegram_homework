"""Manually authorize a user session. Never call this from the MCP server."""

import asyncio
from getpass import GetPassWarning, getpass
import logging
import os
import re
import sys
import warnings

from dotenv import dotenv_values
from telethon import TelegramClient, errors, functions, types, utils

from telegram_mcp.paths import PROJECT_ROOT, SESSION_PATH


def read_secret(prompt: str) -> str:
    """Refuse getpass's echoing fallback when no secure terminal is available."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", GetPassWarning)
        value = getpass(prompt)
    if not value.strip():
        raise ValueError("Empty authentication input")
    return value


def load_credentials() -> tuple[int, str]:
    """Read credentials only from the project's local .env file."""
    values = dotenv_values(PROJECT_ROOT / ".env", interpolate=False)
    api_id = int(values.get("TELEGRAM_API_ID") or "0")
    api_hash = values.get("TELEGRAM_API_HASH") or ""
    if api_id <= 0 or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise ValueError("Invalid API credentials")
    return api_id, api_hash


def restrict_session_permissions() -> None:
    """Protect the session and any SQLite sidecar files on POSIX systems."""
    if os.name == "posix":
        SESSION_PATH.parent.chmod(0o700)
        for path in SESSION_PATH.parent.glob(SESSION_PATH.name + "*"):
            if path.is_file():
                path.chmod(0o600)


async def authenticate(client: TelegramClient, api_id: int, api_hash: str) -> str:
    """Validate an existing session or perform one interactive login attempt."""
    if await client.is_user_authorized():
        return "Existing Telegram session is authorized. No login code requested."

    phone = utils.parse_phone(read_secret("Phone number (international format, hidden): "))
    if not phone:
        raise ValueError("Invalid phone number")
    # The high-level helper retries AuthRestartError internally. Send only once.
    sent = await client(functions.auth.SendCodeRequest(
        phone_number=phone, api_id=api_id, api_hash=api_hash,
        settings=types.CodeSettings(),
    ))
    if not isinstance(sent, types.auth.SentCode):
        raise RuntimeError("Unsupported login response")
    code = read_secret("Telegram login code (hidden): ").strip()
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
    except errors.SessionPasswordNeededError:
        password = read_secret("Two-step verification password (hidden): ")
        await client.sign_in(password=password)

    if not await client.is_user_authorized():
        raise RuntimeError("Session is not authorized")
    client.session.save()
    return "Telegram authentication successful. Session saved to data/telegram.session."


async def run_login() -> str:
    """Open the persistent session and always disconnect after the attempt."""
    api_id, api_hash = load_credentials()
    previous_umask = os.umask(0o077)
    client = None
    try:
        SESSION_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        restrict_session_permissions()
        # Keep library messages and raw RPC errors out of terminal output.
        logger = logging.getLogger("telegram_login")
        logger.setLevel(logging.CRITICAL + 1)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        client = TelegramClient(
            str(SESSION_PATH), api_id, api_hash,
            request_retries=0, connection_retries=0, auto_reconnect=False,
            flood_sleep_threshold=0, raise_last_call_error=True,
            receive_updates=False, base_logger=logger,
        )
        restrict_session_permissions()
        await client.connect()
        return await authenticate(client, api_id, api_hash)
    finally:
        try:
            if client is not None:
                await client.disconnect()
        finally:
            try:
                if SESSION_PATH.parent.exists():
                    restrict_session_permissions()
            finally:
                os.umask(previous_umask)


def main() -> int:
    """Report sanitized outcomes without exposing exception contents."""
    try:
        print(asyncio.run(run_login()))
        return 0
    except (KeyboardInterrupt, EOFError):
        message = "Authentication cancelled. No automatic retry was made."
    except GetPassWarning:
        message = "Authentication requires a terminal that supports hidden input."
    except errors.FloodWaitError:
        message = "Telegram rate limit reached. Stop and wait before manually trying again."
    except (errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError):
        message = "Login code is invalid or expired. No automatic retry was made."
    except errors.PasswordHashInvalidError:
        message = "Two-step verification password is invalid. No automatic retry was made."
    except ValueError:
        message = "Invalid configuration or input. Check .env and the entered values."
    except Exception:
        message = "Authentication failed. Check configuration and connectivity. No automatic retry was made."
    print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
