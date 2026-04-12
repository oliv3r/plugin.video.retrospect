# SPDX-License-Identifier: GPL-3.0-or-later

import binascii
import os
import unittest
from typing import Optional
from unittest.mock import MagicMock, patch

from resources.lib.authentication.authenticationhandler import AuthenticationHandler
from resources.lib.authentication.authenticationresult import AuthenticationResult
from resources.lib.authentication.rtlxlhandler import RtlXlHandler
from resources.lib.authentication.authenticator import Authenticator
from resources.lib.logger import Logger
from resources.lib.urihandler import UriHandler


class _MockAuthHandler(AuthenticationHandler):
    """Minimal in-process handler for unit tests — no network required."""

    def __init__(self, realm: str, error: Optional[str] = None,
                 session_error: Optional[str] = None,
                 active_user: Optional[str] = None) -> None:
        super().__init__(realm, device_id=None)
        self._error = error
        self._session_error = session_error
        self._active_user = active_user

    def log_on(self, username: str, password: str) -> AuthenticationResult:
        return AuthenticationResult("", error=self._error)

    def active_authentication(self) -> AuthenticationResult:
        return AuthenticationResult(self._active_user or "", error=self._session_error)

    def log_off(self, username: str) -> bool:
        return True

    def get_authentication_token(self) -> Optional[str]:
        return None


class TestAuthenticator(unittest.TestCase):
    # noinspection PyPep8Naming
    def __init__(self, methodName: str) -> None:  # NOSONAR
        super(TestAuthenticator, self).__init__(methodName)

        self.user_name: Optional[str] = os.environ.get("RTLXL_USERNAME")
        self.password: Optional[str] = os.environ.get("RTLXL_PASSWORD")
        self.device_id: str = binascii.hexlify(os.urandom(16)).decode()
        self.rtl_api_key: str = "3_R0XjstXd4MpkuqdK3kKxX20icLSE3FB27yQKl4zQVjVpqmgSyRCPKKLGdn5kjoKq"

    @classmethod
    def setUpClass(cls) -> None:
        Logger.create_logger(None, str(cls), min_log_level=0)
        UriHandler.create_uri_handler(ignore_ssl_errors=False)

    @classmethod
    def tearDownClass(cls) -> None:
        Logger.instance().close_log()

    def tearDown(self) -> None:
        pass

    def setUp(self) -> None:
        UriHandler.delete_cookie(domain=".sso.rtl.nl")

    def test_init_authenticator_no_handler(self) -> None:
        with self.assertRaises(ValueError):
            # noinspection PyTypeChecker
            Authenticator(None)  # type: ignore[arg-type]

    def test_init_authenticator_incorrect_type(self) -> None:
        with self.assertRaises(ValueError):
            # noinspection PyTypeChecker
            Authenticator("handler")  # type: ignore[arg-type]

    def test_init_authenticator(self) -> None:
        h = RtlXlHandler("rtlxl.nl", self.rtl_api_key)
        a = Authenticator(h)
        self.assertIsNotNone(a)

    def test_login_no_username(self) -> None:
        h = RtlXlHandler("rtlxl.nl", self.rtl_api_key)
        a = Authenticator(h)
        res = a.log_on("", "secret")
        self.assertFalse(res.logged_on)
        self.assertEqual(res.error, "missing_username")

    def test_login_no_password(self) -> None:
        h = RtlXlHandler("rtlxl.nl", self.rtl_api_key)
        a = Authenticator(h)
        res = a.log_on("username", "")
        self.assertFalse(res.logged_on)
        self.assertEqual(res.error, "missing_password")

    @unittest.skipIf(not os.environ.get("RTLXL_USERNAME"), "Not testing login without credentials")
    def test_current_user(self) -> None:
        assert self.user_name is not None and self.password is not None
        h = RtlXlHandler("rtlxl.nl", self.rtl_api_key)
        a = Authenticator(h)
        a.log_on(self.user_name, self.password)
        h_user = h.active_authentication()
        a_user = a.active_authentication()
        self.assertEqual(h_user.username, a_user.username)

    @unittest.skipIf(not os.environ.get("RTLXL_USERNAME"), "Not testing login without credentials")
    def test_log_on(self) -> None:
        assert self.user_name is not None and self.password is not None
        h = RtlXlHandler("rtlxl.nl", self.rtl_api_key)
        a = Authenticator(h)
        res = a.log_on(self.user_name, self.password)
        self.assertTrue(res.logged_on)

    @unittest.skipIf(not os.environ.get("RTLXL_USERNAME"), "Not testing login without credentials")
    def test_log_on_twice(self) -> None:
        assert self.user_name is not None and self.password is not None
        h = RtlXlHandler("rtlxl.nl", self.rtl_api_key)
        a = Authenticator(h)
        res = a.log_on(self.user_name, self.password)
        self.assertTrue(res.logged_on)
        res = a.log_on(self.user_name, self.password)
        self.assertTrue(res.logged_on)
        self.assertTrue(res.existing_login)

    @unittest.skipIf(not os.environ.get("RTLXL_USERNAME"), "Not testing login without credentials")
    def test_log_off(self) -> None:
        assert self.user_name is not None and self.password is not None
        h = RtlXlHandler("rtlxl.nl", self.rtl_api_key)
        a = Authenticator(h)
        res = a.log_on(self.user_name, self.password)
        self.assertTrue(res.logged_on)
        a.log_off(self.user_name)
        self.assertFalse(a.active_authentication().logged_on)

    @unittest.skipIf(not os.environ.get("RTLXL_USERNAME"), "Not testing login without credentials")
    def test_log_on_without_log_off(self) -> None:
        assert self.user_name is not None and self.password is not None
        h = RtlXlHandler("rtlxl.nl", self.rtl_api_key)
        a = Authenticator(h)
        res = a.log_on(self.user_name, self.password)
        self.assertTrue(res.logged_on)
        user_name = self.user_name.replace("lf@m", "lf2@m")
        res = a.log_on(user_name, self.password)
        self.assertTrue(res.logged_on)
        self.assertEqual(user_name, a.active_authentication().username)

    def test_safe_log_masks_odd_indices(self) -> None:
        h = RtlXlHandler("rtlxl.nl", self.rtl_api_key)
        a = Authenticator(h)
        self.assertEqual(a._Authenticator__safe_log("user@example.com"),  # type: ignore[attr-defined]
                         "u*e*@*x*m*l*.*o*")

    def test_safe_log_passes_none_through(self) -> None:
        h = RtlXlHandler("rtlxl.nl", self.rtl_api_key)
        a = Authenticator(h)
        self.assertIsNone(a._Authenticator__safe_log(None))  # type: ignore[attr-defined]

    def test_safe_log_collapses_empty_string_to_none(self) -> None:
        h = RtlXlHandler("rtlxl.nl", self.rtl_api_key)
        a = Authenticator(h)
        self.assertIsNone(a._Authenticator__safe_log(""))  # type: ignore[attr-defined]

    def test_empty_realm_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            AuthenticationHandler("", device_id=None)

    def test_realm_returns_realm(self) -> None:
        h = _MockAuthHandler("test.realm")
        self.assertEqual(h.realm, "test.realm")

    def test_store_and_retrieve_current_user_in_settings(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch("resources.lib.authentication.authenticationhandler.AddonSettings") as mock_settings:
            mock_store = MagicMock()
            mock_settings.store.return_value = mock_store
            h._store_current_user_in_settings("user@example.com")
        mock_store.set_setting.assert_called_once_with(
            "test.realm:authenticated_user", "user@example.com")

    def test_get_current_user_in_settings(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch("resources.lib.authentication.authenticationhandler.AddonSettings") as mock_settings:
            mock_store = MagicMock()
            mock_store.get_setting.return_value = "user@example.com"
            mock_settings.store.return_value = mock_store
            result = h._get_current_user_in_settings()
        self.assertEqual(result, "user@example.com")

    def test_active_authentication_delegates_to_handler(self) -> None:
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h)
        with patch.object(h, "active_authentication",
                          return_value=AuthenticationResult("user@example.com")) as mock_active:
            result = a.active_authentication()
        self.assertTrue(result.logged_on)
        self.assertEqual(result.username, "user@example.com")
        mock_active.assert_called_once()

    def test_get_authentication_token_delegates_to_handler(self) -> None:
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h)
        with patch.object(h, "get_authentication_token", return_value="tok") as mock_tok:
            result = a.get_authentication_token()
        self.assertEqual(result, "tok")
        mock_tok.assert_called_once()

    def test_log_on_switches_user_when_different_user_active(self) -> None:
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h)
        with patch.object(h, "active_authentication",
                          return_value=AuthenticationResult("old@example.com")), \
             patch.object(h, "log_off", return_value=True) as mock_log_off, \
             patch.object(h, "log_on",
                          return_value=AuthenticationResult("new@example.com")) as mock_log_on:
            result = a.log_on("new@example.com", "secret")
        mock_log_off.assert_called_once_with("old@example.com")
        mock_log_on.assert_called_once_with("new@example.com", "secret")
        self.assertTrue(result.logged_on)
        self.assertEqual(result.username, "new@example.com")

    def test_log_on_returns_existing_when_same_user_active(self) -> None:
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h)
        existing = AuthenticationResult("user@example.com")
        with patch.object(h, "active_authentication", return_value=existing), \
             patch.object(h, "log_on") as mock_log_on:
            result = a.log_on("user@example.com", "secret")
        mock_log_on.assert_not_called()
        self.assertIs(result, existing)

    def test_log_on_fetches_password_from_channel_setting(self) -> None:
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h, channel_guid="chan-guid", password_setting_id="pwd_setting")
        with patch.object(h, "log_on",
                          return_value=AuthenticationResult("user@example.com")) as mock_log_on, \
             patch("resources.lib.authentication.authenticator.Vault") as mock_vault_cls:
            mock_vault_cls.return_value.get_channel_setting.return_value = "vault-pwd"
            result = a.log_on("user@example.com", password=None)
        mock_vault_cls.return_value.get_channel_setting.assert_called_once_with(
            "chan-guid", "pwd_setting")
        mock_log_on.assert_called_once_with("user@example.com", "vault-pwd")
        self.assertTrue(result.logged_on)

    def test_log_on_fetches_password_from_global_setting(self) -> None:
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h, password_setting_id="pwd_setting")
        with patch.object(h, "log_on",
                          return_value=AuthenticationResult("user@example.com")) as mock_log_on, \
             patch("resources.lib.authentication.authenticator.Vault") as mock_vault_cls:
            mock_vault_cls.return_value.get_setting.return_value = "vault-pwd"
            result = a.log_on("user@example.com", password=None)
        mock_vault_cls.return_value.get_setting.assert_called_once_with("pwd_setting")
        mock_log_on.assert_called_once_with("user@example.com", "vault-pwd")
        self.assertTrue(result.logged_on)

    def test_log_on_returns_unauthenticated_when_vault_has_no_password(self) -> None:
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h, password_setting_id="pwd_setting")
        with patch.object(h, "log_on") as mock_log_on, \
             patch("resources.lib.authentication.authenticator.Vault") as mock_vault_cls:
            mock_vault_cls.return_value.get_setting.return_value = None
            result = a.log_on("user@example.com", password=None)
        mock_log_on.assert_not_called()
        self.assertFalse(result.logged_on)
        self.assertEqual(result.error, "missing_password")

    def test_log_on_shows_dialog_on_handler_error(self) -> None:
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h)
        with patch.object(h, "log_on",
                          return_value=AuthenticationResult("", error="bad creds")), \
             patch("resources.lib.authentication.authenticator.XbmcWrapper") as mock_wrapper:
            result = a.log_on("user@example.com", "secret")
        mock_wrapper.show_dialog.assert_called_once_with(None, "bad creds")
        self.assertEqual(result.error, "bad creds")

    def test_log_off_returns_early_when_not_logged_on(self) -> None:
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h)
        with patch.object(h, "active_authentication",
                          return_value=AuthenticationResult("")), \
             patch.object(h, "log_off") as mock_log_off:
            a.log_off("user@example.com")
        mock_log_off.assert_not_called()

    def test_log_off_force_logs_off_regardless_of_username(self) -> None:
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h)
        with patch.object(h, "active_authentication",
                          return_value=AuthenticationResult("active@example.com")), \
             patch.object(h, "log_off", return_value=True) as mock_log_off:
            a.log_off("other@example.com", force=True)
        mock_log_off.assert_called_once_with("active@example.com")

    def test_log_off_skips_when_username_differs_and_not_forced(self) -> None:
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h)
        with patch.object(h, "active_authentication",
                          return_value=AuthenticationResult("active@example.com")), \
             patch.object(h, "log_off") as mock_log_off:
            a.log_off("other@example.com", force=False)
        mock_log_off.assert_not_called()

    def test_log_off_logs_error_on_handler_failure(self) -> None:
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h)
        with patch.object(h, "active_authentication",
                          return_value=AuthenticationResult("user@example.com")), \
             patch.object(h, "log_off", return_value=False) as mock_log_off:
            a.log_off("user@example.com", force=True)
        mock_log_off.assert_called_once_with("user@example.com")


class TestAuthenticatorUnit(unittest.TestCase):
    """Unit tests for Authenticator — no network required."""

    @classmethod
    def setUpClass(cls) -> None:
        Logger.create_logger(None, str(cls), min_log_level=0)
        UriHandler.create_uri_handler(ignore_ssl_errors=False)

    @classmethod
    def tearDownClass(cls) -> None:
        Logger.instance().close_log()

    def test_init_with_channel_name(self) -> None:
        a = Authenticator(_MockAuthHandler("test.realm"), channel_name="Test Channel")
        self.assertIsNotNone(a)

    def test_login_error_passes_channel_name_to_dialog(self) -> None:
        h = _MockAuthHandler("test.realm", error="Login failed")
        with patch("resources.lib.authentication.authenticator.XbmcWrapper.show_dialog") as mock_dialog:
            a = Authenticator(h, channel_name="My Channel")
            a.log_on("user", "pass")
        mock_dialog.assert_called_once_with("My Channel", "Login failed")

    def test_login_error_passes_none_when_no_channel_name(self) -> None:
        h = _MockAuthHandler("test.realm", error="Login failed")
        with patch("resources.lib.authentication.authenticator.XbmcWrapper.show_dialog") as mock_dialog:
            a = Authenticator(h)
            a.log_on("user", "pass")
        mock_dialog.assert_called_once_with(None, "Login failed")

    def test_login_success_does_not_show_dialog(self) -> None:
        h = _MockAuthHandler("test.realm", error=None)
        with patch("resources.lib.authentication.authenticator.XbmcWrapper.show_dialog") as mock_dialog:
            a = Authenticator(h, channel_name="My Channel")
            a.log_on("user", "pass")
        mock_dialog.assert_not_called()

    def test_init_with_setting_id(self) -> None:
        a = Authenticator(_MockAuthHandler("test.realm"), password_setting_id="my_setting")
        self.assertIsNotNone(a)

    def test_init_with_channel_guid(self) -> None:
        a = Authenticator(_MockAuthHandler("test.realm"), channel_guid="abc-123", password_setting_id="pw")
        self.assertIsNotNone(a)

    def test_log_on_looks_up_vault_setting(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch("resources.lib.authentication.authenticator.Vault") as MockVault:
            MockVault.return_value.get_setting.return_value = None
            a = Authenticator(h, password_setting_id="my_setting")
            a.log_on("user")
        MockVault.return_value.get_setting.assert_called_once_with("my_setting")

    def test_log_on_looks_up_vault_channel_setting(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch("resources.lib.authentication.authenticator.Vault") as MockVault:
            MockVault.return_value.get_channel_setting.return_value = None
            a = Authenticator(h, channel_guid="abc-123", password_setting_id="pw")
            a.log_on("user")
        MockVault.return_value.get_channel_setting.assert_called_once_with("abc-123", "pw")

    def test_log_on_vault_returns_none_fails_without_login(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch("resources.lib.authentication.authenticator.Vault") as MockVault:
            MockVault.return_value.get_setting.return_value = None
            a = Authenticator(h, password_setting_id="my_setting")
            result = a.log_on("user")
        self.assertFalse(result.logged_on)

    def test_log_on_explicit_password_skips_vault(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch("resources.lib.authentication.authenticator.Vault") as MockVault:
            a = Authenticator(h, password_setting_id="my_setting")
            a.log_on("user", "explicit_pass")
        MockVault.assert_not_called()

    def test_log_on_empty_username_returns_not_logged_on(self) -> None:
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h)
        result = a.log_on("")
        self.assertFalse(result.logged_on)

    def test_network_error_aborts_and_shows_localized_dialog(self) -> None:
        h = _MockAuthHandler("test.realm", session_error="network_error")
        with patch("resources.lib.authentication.authenticator.XbmcWrapper.show_dialog") as mock_dialog, \
             patch("resources.lib.authentication.authenticator.LanguageHelper.get_localized_string",
                   return_value="Network error message"):
            a = Authenticator(h, channel_name="My Channel")
            result = a.log_on("user", "pass")
        self.assertFalse(result.logged_on)
        mock_dialog.assert_called_once_with("My Channel", mock_dialog.call_args[0][1])

    def test_network_error_does_not_reach_handler_log_on(self) -> None:
        h = _MockAuthHandler("test.realm", session_error="network_error")
        with patch("resources.lib.authentication.authenticator.XbmcWrapper.show_dialog"), \
             patch.object(h, "log_on", wraps=h.log_on) as mock_log_on:
            a = Authenticator(h)
            a.log_on("user", "pass")
        mock_log_on.assert_not_called()

    def test_session_error_passes_raw_message_to_dialog(self) -> None:
        h = _MockAuthHandler("test.realm", session_error="some_other_error")
        with patch("resources.lib.authentication.authenticator.XbmcWrapper.show_dialog") as mock_dialog:
            a = Authenticator(h, channel_name="My Channel")
            a.log_on("user", "pass")
        mock_dialog.assert_called_once_with("My Channel", "some_other_error")

    def test_log_off_without_force_skips_handler_when_no_active_session(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch.object(h, "log_off", return_value=True) as mock_log_off:
            a = Authenticator(h)
            a.log_off("", force=False)
        mock_log_off.assert_not_called()

    def test_force_log_off_skips_handler_when_no_active_session(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch.object(h, "log_off", return_value=True) as mock_log_off:
            a = Authenticator(h)
            a.log_off("", force=True)
        mock_log_off.assert_not_called()

    def test_log_off_without_force_skips_handler_for_different_active_user(self) -> None:
        h = _MockAuthHandler("test.realm", active_user="other@example.com")
        with patch.object(h, "log_off", return_value=True) as mock_log_off:
            a = Authenticator(h)
            a.log_off("user@example.com", force=False)
        mock_log_off.assert_not_called()

    def test_force_log_off_uses_active_username(self) -> None:
        h = _MockAuthHandler("test.realm", active_user="other@example.com")
        with patch.object(h, "log_off", return_value=True) as mock_log_off:
            a = Authenticator(h)
            a.log_off("user@example.com", force=True)
        mock_log_off.assert_called_once_with("other@example.com")
