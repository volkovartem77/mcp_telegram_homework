"""Authentication tests use fake clients and temporary files, never Telegram."""

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telethon import errors, types

from scripts import login


def fake_client() -> MagicMock:
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.is_user_authorized = AsyncMock(side_effect=[False, True])
    client.sign_in = AsyncMock()
    client.return_value = None
    return client


class AuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = fake_client()
        self.sent = types.auth.SentCode(
            type=types.auth.SentCodeTypeApp(length=5), phone_code_hash="test-hash",
        )
        self.client = AsyncMock(wraps=self.client)
        self.client.return_value = self.sent
        self.client.session = MagicMock()

    async def test_existing_session_requests_no_input_or_code(self) -> None:
        self.client.is_user_authorized = AsyncMock(return_value=True)
        with patch("builtins.input") as prompt, patch.object(login, "getpass") as password:
            result = await login.authenticate(self.client, 123, "a" * 32)
        self.assertIn("Existing", result)
        prompt.assert_not_called()
        password.assert_not_called()
        self.client.assert_not_awaited()
        self.client.sign_in.assert_not_awaited()
        self.client.session.save.assert_not_called()

    async def test_code_login_saves_session(self) -> None:
        with patch("builtins.input", side_effect=["+15555550123", "12345"]):
            result = await login.authenticate(self.client, 123, "a" * 32)
        self.assertIn("successful", result)
        self.client.assert_awaited_once()
        self.client.sign_in.assert_awaited_once_with(
            phone="15555550123", code="12345", phone_code_hash="test-hash",
        )
        self.client.session.save.assert_called_once()

    async def test_two_factor_login(self) -> None:
        self.client.sign_in.side_effect = [errors.SessionPasswordNeededError(None), None]
        with (
            patch("builtins.input", side_effect=["+15555550123", "12345"]),
            patch.object(login, "getpass", return_value="test-password"),
        ):
            await login.authenticate(self.client, 123, "a" * 32)
        self.assertEqual(self.client.sign_in.await_count, 2)
        self.client.sign_in.assert_awaited_with(password="test-password")
        self.client.session.save.assert_called_once()

    async def test_code_request_failure_is_not_retried(self) -> None:
        self.client.side_effect = errors.AuthRestartError(None)
        with patch("builtins.input", return_value="+15555550123"):
            with self.assertRaises(errors.AuthRestartError):
                await login.authenticate(self.client, 123, "a" * 32)
        self.client.assert_awaited_once()
        self.client.sign_in.assert_not_awaited()

    async def test_invalid_code_is_not_retried(self) -> None:
        self.client.sign_in.side_effect = errors.PhoneCodeInvalidError(None)
        with patch("builtins.input", side_effect=["+15555550123", "12345"]):
            with self.assertRaises(errors.PhoneCodeInvalidError):
                await login.authenticate(self.client, 123, "a" * 32)
        self.client.assert_awaited_once()
        self.client.sign_in.assert_awaited_once()
        self.client.session.save.assert_not_called()

    async def test_empty_code_cannot_trigger_another_code_request(self) -> None:
        with patch("builtins.input", side_effect=["+15555550123", ""]):
            with self.assertRaises(ValueError):
                await login.authenticate(self.client, 123, "a" * 32)
        self.client.assert_awaited_once()
        self.client.sign_in.assert_not_awaited()

    async def test_failed_final_authorization_is_not_success(self) -> None:
        self.client.is_user_authorized = AsyncMock(return_value=False)
        with patch("builtins.input", side_effect=["+15555550123", "12345"]):
            with self.assertRaises(RuntimeError):
                await login.authenticate(self.client, 123, "a" * 32)
        self.client.session.save.assert_not_called()


class SessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_and_preserve_file_on_success_and_failures(self) -> None:
        for failure_at in (None, "connect", "authenticate"):
            with self.subTest(failure_at=failure_at), tempfile.TemporaryDirectory() as directory:
                session = Path(directory) / "data" / "telegram.session"
                session.parent.mkdir()
                session.write_bytes(b"existing-test-session")
                client = fake_client()
                if failure_at == "connect":
                    client.connect.side_effect = RuntimeError("sensitive-error")
                authenticate = AsyncMock(return_value="Success")
                if failure_at == "authenticate":
                    authenticate.side_effect = RuntimeError("sensitive-error")
                with (
                    patch.object(login, "SESSION_PATH", session),
                    patch.object(login, "load_credentials", return_value=(123, "a" * 32)),
                    patch.object(login, "TelegramClient", return_value=client) as factory,
                    patch.object(login, "authenticate", authenticate),
                ):
                    if failure_at:
                        with self.assertRaises(RuntimeError):
                            await login.run_login()
                    else:
                        self.assertEqual(await login.run_login(), "Success")
                client.disconnect.assert_awaited_once()
                self.assertEqual(session.read_bytes(), b"existing-test-session")
                options = factory.call_args.kwargs
                self.assertEqual(options["request_retries"], 0)
                self.assertEqual(options["connection_retries"], 0)
                self.assertFalse(options["auto_reconnect"])
                if os.name == "posix":
                    self.assertEqual(session.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(session.parent.stat().st_mode & 0o777, 0o700)


class InputAndOutputTests(unittest.TestCase):
    def test_credentials_from_local_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("TELEGRAM_API_ID=123\nTELEGRAM_API_HASH=" + "a" * 32 + "\n")
            with patch.object(login, "PROJECT_ROOT", root):
                self.assertEqual(login.load_credentials(), (123, "a" * 32))

    def test_missing_credentials_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(login, "PROJECT_ROOT", Path(directory)):
                with self.assertRaises(ValueError):
                    login.load_credentials()

    def test_hidden_password_does_not_use_visible_input(self) -> None:
        output = io.StringIO()
        with (
            patch.object(login, "getpass", return_value="test-password"),
            patch("builtins.input") as visible_input,
            redirect_stdout(output), redirect_stderr(output),
        ):
            self.assertEqual(login.read_password(), "test-password")
        visible_input.assert_not_called()
        self.assertEqual(output.getvalue(), "")

    def test_password_fallback_warns_before_reading(self) -> None:
        import warnings

        output = io.StringIO()

        def unavailable(prompt):
            warnings.warn("Cannot hide input", login.GetPassWarning)
            self.fail("getpass must not read using its own echoing fallback")

        def visible_input(prompt):
            self.assertIn("password may be visible on screen", output.getvalue())
            return "test-password"

        with (
            patch.object(login, "getpass", side_effect=unavailable),
            patch("builtins.input", side_effect=visible_input) as fallback,
            redirect_stdout(output), redirect_stderr(output),
        ):
            self.assertEqual(login.read_password(), "test-password")
        fallback.assert_called_once()
        self.assertNotIn("test-password", output.getvalue())

    def test_empty_password_is_rejected_in_both_modes(self) -> None:
        for hidden in (True, False):
            with (
                self.subTest(hidden=hidden),
                patch.object(login, "getpass", return_value="", side_effect=(
                    None if hidden else login.GetPassWarning("Cannot hide input")
                )),
                patch("builtins.input", return_value=""),
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(ValueError):
                    login.read_password()

    def test_password_cancellation_does_not_fall_back(self) -> None:
        for failure in (EOFError(), KeyboardInterrupt()):
            with (
                self.subTest(failure=type(failure).__name__),
                patch.object(login, "getpass", side_effect=failure),
                patch("builtins.input") as fallback,
            ):
                with self.assertRaises(type(failure)):
                    login.read_password()
                fallback.assert_not_called()

    def test_errors_are_sanitized(self) -> None:
        failures = [
            RuntimeError("SECRET_MARKER"),
            errors.PhoneCodeInvalidError(None),
            errors.PhoneCodeExpiredError(None),
            errors.PasswordHashInvalidError(None),
            errors.FloodWaitError(None, capture=30),
            EOFError("SECRET_MARKER"),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                output = io.StringIO()
                with (
                    patch.object(login, "run_login", AsyncMock(side_effect=failure)),
                    redirect_stdout(output), redirect_stderr(output),
                ):
                    self.assertEqual(login.main(), 1)
                self.assertNotIn("SECRET_MARKER", output.getvalue())
                self.assertNotIn("Traceback", output.getvalue())
                self.assertTrue(output.getvalue().strip())
