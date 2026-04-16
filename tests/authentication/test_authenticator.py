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

    def __init__(self, realm: str, error: Optional[str] = None) -> None:
        super().__init__(realm, device_id=None)
        self._error = error

    def log_on(self, username: str, password: str) -> AuthenticationResult:
        return AuthenticationResult("", error=self._error)

    def active_authentication(self) -> AuthenticationResult:
        return AuthenticationResult("")

    def log_off(self, username: str) -> bool:
        return True

    def get_authentication_token(self) -> Optional[str]:
        return None


class TestAuthenticator(unittest.TestCase):
    # noinspection PyPep8Naming
    def __init__(self, methodName):  # NOSONAR
        super(TestAuthenticator, self).__init__(methodName)

        self.user_name = os.environ.get("RTLXL_USERNAME")
        self.password = os.environ.get("RTLXL_PASSWORD")
        self.device_id = binascii.hexlify(os.urandom(16)).decode()
        self.rtl_api_key = "3_R0XjstXd4MpkuqdK3kKxX20icLSE3FB27yQKl4zQVjVpqmgSyRCPKKLGdn5kjoKq"

    @classmethod
    def setUpClass(cls):
        Logger.create_logger(None, str(cls), min_log_level=0)
        UriHandler.create_uri_handler(ignore_ssl_errors=False)

    @classmethod
    def tearDownClass(cls):
        Logger.instance().close_log()

    def tearDown(self):
        pass

    def setUp(self):
        UriHandler.delete_cookie(domain=".sso.rtl.nl")

    def test_init_authenticator_no_handler(self):
        with self.assertRaises(ValueError):
            # noinspection PyTypeChecker
            Authenticator(None)

    def test_init_authenticator_incorrect_type(self):
        with self.assertRaises(ValueError):
            # noinspection PyTypeChecker
            Authenticator("handler")

    def test_init_authenticator(self):
        h = RtlXlHandler("rtlxl.nl", self.rtl_api_key)
        a = Authenticator(h)
        self.assertIsNotNone(a)

    def test_login_no_username(self):
        h = RtlXlHandler("rtlxl.nl", self.rtl_api_key)
        a = Authenticator(h)
        res = a.log_on("", "secret")
        self.assertFalse(res.logged_on)

    def test_login_no_password(self):
        h = RtlXlHandler("rtlxl.nl", self.rtl_api_key)
        a = Authenticator(h)
        res = a.log_on("username", "")
        self.assertFalse(res.logged_on)

    @unittest.skipIf(not os.environ.get("RTLXL_USERNAME"), "Not testing login without credentials")
    def test_current_user(self):
        h = RtlXlHandler("rtlxl.nl", self.rtl_api_key)
        a = Authenticator(h)
        a.log_on(self.user_name, self.password)
        h_user = h.active_authentication()
        a_user = a.active_authentication()
        self.assertEqual(h_user.username, a_user.username)

    @unittest.skipIf(not os.environ.get("RTLXL_USERNAME"), "Not testing login without credentials")
    def test_log_on(self):
        h = RtlXlHandler("rtlxl.nl", self.rtl_api_key)
        a = Authenticator(h)
        res = a.log_on(self.user_name, self.password)
        self.assertTrue(res.logged_on)

    @unittest.skipIf(not os.environ.get("RTLXL_USERNAME"), "Not testing login without credentials")
    def test_log_on_twice(self):
        h = RtlXlHandler("rtlxl.nl", self.rtl_api_key)
        a = Authenticator(h)
        res = a.log_on(self.user_name, self.password)
        self.assertTrue(res.logged_on)
        res = a.log_on(self.user_name, self.password)
        self.assertTrue(res.logged_on)
        self.assertTrue(res.existing_login)

    @unittest.skipIf(not os.environ.get("RTLXL_USERNAME"), "Not testing login without credentials")
    def test_log_off(self):
        h = RtlXlHandler("rtlxl.nl", self.rtl_api_key)
        a = Authenticator(h)
        res = a.log_on(self.user_name, self.password)
        self.assertTrue(res.logged_on)
        a.log_off(self.user_name)
        self.assertFalse(a.active_authentication().logged_on)

    @unittest.skipIf(not os.environ.get("RTLXL_USERNAME"), "Not testing login without credentials")
    def test_log_on_without_log_off(self):
        h = RtlXlHandler("rtlxl.nl", self.rtl_api_key)
        a = Authenticator(h)
        res = a.log_on(self.user_name, self.password)
        self.assertTrue(res.logged_on)
        user_name = self.user_name.replace("lf@m", "lf2@m")
        res = a.log_on(user_name, self.password)
        self.assertTrue(res.logged_on)
        self.assertEqual(user_name, a.active_authentication().username)

    def test_safe_log_masks_odd_indices(self):
        h = RtlXlHandler("rtlxl.nl", self.rtl_api_key)
        a = Authenticator(h)
        self.assertEqual(a._Authenticator__safe_log("user@example.com"),
                         "u*e*@*x*m*l*.*o*")

    def test_safe_log_passes_none_through(self):
        h = RtlXlHandler("rtlxl.nl", self.rtl_api_key)
        a = Authenticator(h)
        self.assertIsNone(a._Authenticator__safe_log(None))

    def test_safe_log_collapses_empty_string_to_none(self):
        h = RtlXlHandler("rtlxl.nl", self.rtl_api_key)
        a = Authenticator(h)
        self.assertIsNone(a._Authenticator__safe_log(""))

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
        a = Authenticator(h)
        with patch.object(h, "log_on",
                          return_value=AuthenticationResult("user@example.com")) as mock_log_on, \
             patch("resources.lib.authentication.authenticator.Vault") as mock_vault_cls:
            mock_vault_cls.return_value.get_channel_setting.return_value = "vault-pwd"
            result = a.log_on("user@example.com", password=None,
                              setting_id="pwd_setting", channel_guid="chan-guid")
        mock_vault_cls.return_value.get_channel_setting.assert_called_once_with(
            "chan-guid", "pwd_setting")
        mock_log_on.assert_called_once_with("user@example.com", "vault-pwd")
        self.assertTrue(result.logged_on)

    def test_log_on_fetches_password_from_global_setting(self) -> None:
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h)
        with patch.object(h, "log_on",
                          return_value=AuthenticationResult("user@example.com")) as mock_log_on, \
             patch("resources.lib.authentication.authenticator.Vault") as mock_vault_cls:
            mock_vault_cls.return_value.get_setting.return_value = "vault-pwd"
            result = a.log_on("user@example.com", password=None, setting_id="pwd_setting")
        mock_vault_cls.return_value.get_setting.assert_called_once_with("pwd_setting")
        mock_log_on.assert_called_once_with("user@example.com", "vault-pwd")
        self.assertTrue(result.logged_on)

    def test_log_on_returns_unauthenticated_when_vault_has_no_password(self) -> None:
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h)
        with patch.object(h, "log_on") as mock_log_on, \
             patch("resources.lib.authentication.authenticator.Vault") as mock_vault_cls:
            mock_vault_cls.return_value.get_setting.return_value = None
            result = a.log_on("user@example.com", password=None, setting_id="pwd_setting")
        mock_log_on.assert_not_called()
        self.assertFalse(result.logged_on)

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
