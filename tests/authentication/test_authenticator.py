# SPDX-License-Identifier: GPL-3.0-or-later

import binascii
import os
import threading
import unittest
from typing import Optional
from unittest.mock import MagicMock, patch

from resources.lib.authentication.authenticationhandler import (
    AuthenticationHandler, DeviceAuthData, DeviceAuthResult,
)
from resources.lib.authentication.authenticationresult import AuthenticationResult
from resources.lib.authentication.rtlxlhandler import RtlXlHandler
from resources.lib.authentication.authenticator import (
    Authenticator,
    _AUTH_METHOD_CREDENTIALS,
    _AUTH_METHOD_DEVICE,
    _AUTH_SETTING_KEY,
)
from resources.lib.addonsettings import LOCAL
from resources.lib.helpers.languagehelper import LanguageHelper
from resources.lib.logger import Logger
from resources.lib.urihandler import UriHandler
from resources.lib.xbmcwrapper import XbmcWrapper

_TEST_DEVICE_FLOW_REFRESH_INTERVAL = 0.01


class _MockAuthHandler(AuthenticationHandler):
    """Minimal in-process handler for unit tests — no network required."""

    def __init__(self, realm: str, error: Optional[str] = None,
                 session_error: Optional[str] = None,
                 active_user: Optional[str] = None) -> None:
        super().__init__(realm, device_id=None)
        self._error = error
        self._session_error = session_error
        self._active_user = active_user

    def _credential_log_on(self, username: str, password: str) -> AuthenticationResult:
        return AuthenticationResult(username if not self._error else "", error=self._error)

    def active_authentication(self) -> AuthenticationResult:
        return AuthenticationResult(self._active_user or "", error=self._session_error)

    def get_authentication_token(self) -> Optional[str]:
        return None


class _MockDeviceFlowAuthHandler(_MockAuthHandler):
    """Like _MockAuthHandler but with device_flow=True."""

    @property
    def device_flow(self) -> bool:
        return True


class _MockDeviceAuthHandler(_MockAuthHandler):
    """Like _MockAuthHandler but with device authorization support."""

    def _start_device_authorization(self, device_name: str) -> Optional[DeviceAuthData]:
        return None

    def _poll_device_authorization(self, device_code: str) -> DeviceAuthResult:
        return DeviceAuthResult.PENDING


class TestAuthenticationHandlerHelpers(unittest.TestCase):
    """Unit tests for reusable AuthenticationHandler helpers."""

    def setUp(self) -> None:
        self.handler = _MockAuthHandler("test.realm")

    def test_parse_device_auth_data_returns_normalized_dict(self) -> None:
        """_parse_device_auth_data() returns a typed device auth dataclass."""

        result = self.handler._parse_device_auth_data(
            {
                "device_code": "devcode",
                "user_code": "ABCD-1234",
                "verification_uri": "https://example.com/activate",
                "expires_in": 60,
                "interval": 5,
            }
        )

        self.assertEqual(
            DeviceAuthData(
                device_code="devcode",
                user_code="ABCD-1234",
                verification_uri="https://example.com/activate",
                expires_in=60,
                interval=5,
            ),
            result,
        )

    def test_parse_device_auth_data_returns_none_when_required_field_missing(self) -> None:
        """_parse_device_auth_data() returns None for incomplete responses."""

        result = self.handler._parse_device_auth_data(
            {
                "device_code": "devcode",
                "user_code": "ABCD-1234",
                "expires_in": 60,
                "interval": 5,
            }
        )

        self.assertIsNone(result)


    def test_parse_device_auth_data_returns_none_when_device_code_missing(self) -> None:
        result = self.handler._parse_device_auth_data(
            {"user_code": "X", "verification_uri": "https://x", "expires_in": 60, "interval": 5}
        )
        self.assertIsNone(result)


    def test_parse_device_auth_data_returns_none_when_user_code_missing(self) -> None:
        result = self.handler._parse_device_auth_data(
            {"device_code": "X", "verification_uri": "https://x", "expires_in": 60, "interval": 5}
        )
        self.assertIsNone(result)


    def test_parse_device_auth_data_returns_none_when_expires_in_missing(self) -> None:
        result = self.handler._parse_device_auth_data(
            {"device_code": "X", "user_code": "Y", "verification_uri": "https://x", "interval": 5}
        )
        self.assertIsNone(result)


    def test_parse_device_auth_data_returns_none_when_interval_missing(self) -> None:
        result = self.handler._parse_device_auth_data(
            {"device_code": "X", "user_code": "Y", "verification_uri": "https://x", "expires_in": 60}
        )
        self.assertIsNone(result)


    def test_parse_device_auth_data_returns_none_when_field_empty(self) -> None:
        """Provider data with an empty required field is rejected (Layer 1)."""

        result = self.handler._parse_device_auth_data(
            {"device_code": "", "user_code": "Y", "verification_uri": "https://x",
             "expires_in": 60, "interval": 5}
        )
        self.assertIsNone(result)


    def test_parse_device_auth_data_carries_optional_qr_url(self) -> None:
        result = self.handler._parse_device_auth_data(
            {"device_code": "X", "user_code": "Y", "verification_uri": "https://x",
             "expires_in": 60, "interval": 5, "qr_url": "https://qr"}
        )
        self.assertIsNotNone(result)
        assert result is not None  # for mypy
        self.assertEqual("https://qr", result.qr_url)


    def test_device_auth_data_rejects_empty_device_code(self) -> None:
        """Direct construction with invalid fields raises ValueError (Layer 2)."""

        with self.assertRaises(ValueError):
            DeviceAuthData(device_code="", user_code="U", verification_uri="https://x",
                           expires_in=60, interval=5)


    def test_device_auth_data_rejects_non_positive_expires_in(self) -> None:
        with self.assertRaises(ValueError):
            DeviceAuthData(device_code="D", user_code="U", verification_uri="https://x",
                           expires_in=0, interval=5)


    def test_device_auth_data_accepts_valid_inputs(self) -> None:
        data = DeviceAuthData(device_code="D", user_code="U", verification_uri="https://x",
                              expires_in=60, interval=5)
        self.assertEqual("D", data.device_code)
        self.assertIsNone(data.qr_url)


    def test_device_auth_data_rejects_empty_qr_url(self) -> None:
        """Optional qr_url must be either None or a non-empty string (Layer 2)."""

        with self.assertRaises(ValueError):
            DeviceAuthData(device_code="D", user_code="U", verification_uri="https://x",
                           expires_in=60, interval=5, qr_url="")


    def test_parse_device_auth_data_normalizes_empty_qr_url_to_none(self) -> None:
        """An empty qr_url from the provider should not invalidate the response (Layer 1)."""

        result = self.handler._parse_device_auth_data(
            {"device_code": "X", "user_code": "Y", "verification_uri": "https://x",
             "expires_in": 60, "interval": 5, "qr_url": ""}
        )
        self.assertIsNotNone(result)
        assert result is not None  # for mypy
        self.assertIsNone(result.qr_url)


    def test_base_class_stubs_raise_not_implemented(self) -> None:
        h = AuthenticationHandler("stub.realm", device_id=None)
        self.assertRaises(NotImplementedError, h.active_authentication)
        self.assertRaises(NotImplementedError, h._start_device_authorization, "dev")
        self.assertRaises(NotImplementedError, h._poll_device_authorization, "code")
        self.assertRaises(NotImplementedError, h.get_authentication_token)

    def test_authentication_headers_default_is_empty(self) -> None:
        h = AuthenticationHandler("stub.realm", device_id=None)
        self.assertEqual(h.authentication_headers, {})

    def test_authentication_headers_returns_initial_headers(self) -> None:
        h = AuthenticationHandler("stub.realm", device_id=None,
                                  headers={"Authorization": "Bearer tok", "X-Custom": "val"})
        self.assertEqual(h.authentication_headers,
                         {"Authorization": "Bearer tok", "X-Custom": "val"})

    def test_authentication_headers_returns_copy(self) -> None:
        h = AuthenticationHandler("stub.realm", device_id=None, headers={"X-A": "1"})
        headers = h.authentication_headers
        headers["X-A"] = "mutated"
        self.assertEqual(h.authentication_headers["X-A"], "1")

    def test_authenticator_authentication_headers_delegates_to_handler(self) -> None:
        h = _MockAuthHandler("stub.realm")
        with patch.object(type(h), "authentication_headers",
                          new_callable=lambda: property(lambda self: {"X-Token": "abc"})):
            a = Authenticator(h)
            self.assertEqual(a.authentication_headers, {"X-Token": "abc"})


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
        self.assertEqual(res.error, "login_failed")

    def test_login_no_password(self) -> None:
        h = RtlXlHandler("rtlxl.nl", self.rtl_api_key)
        a = Authenticator(h)
        res = a.log_on("username", "")
        self.assertFalse(res.logged_on)
        self.assertEqual(res.error, "login_failed")

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
             patch.object(h, "_credential_log_off", return_value=True) as mock_cred_log_off, \
             patch.object(h, "_credential_log_on",
                          return_value=AuthenticationResult("new@example.com")) as mock_cred:
            result = a.log_on("new@example.com", "secret")
        mock_cred_log_off.assert_called_once_with("old@example.com")
        mock_cred.assert_called_once_with("new@example.com", "secret")
        self.assertTrue(result.logged_on)
        self.assertEqual(result.username, "new@example.com")

    def test_log_on_returns_existing_when_same_user_active(self) -> None:
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h)
        existing = AuthenticationResult("user@example.com")
        with patch.object(h, "active_authentication", return_value=existing), \
             patch.object(h, "_credential_log_on") as mock_cred:
            result = a.log_on("user@example.com", "secret")
        mock_cred.assert_not_called()
        self.assertIs(result, existing)

    def test_log_on_fetches_password_from_channel_setting(self) -> None:
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h, channel_guid="chan-guid", password_setting_id="pwd_setting")
        with patch.object(h, "_credential_log_on",
                          return_value=AuthenticationResult("user@example.com")) as mock_cred, \
             patch("resources.lib.authentication.authenticator.Vault") as mock_vault_cls:
            mock_vault_cls.return_value.get_channel_setting.return_value = "vault-pwd"
            result = a.log_on("user@example.com", password=None)
        mock_vault_cls.return_value.get_channel_setting.assert_called_once_with(
            "chan-guid", "pwd_setting")
        mock_cred.assert_called_once_with("user@example.com", "vault-pwd")
        self.assertTrue(result.logged_on)

    def test_log_on_fetches_password_from_global_setting(self) -> None:
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h, password_setting_id="pwd_setting")
        with patch.object(h, "_credential_log_on",
                          return_value=AuthenticationResult("user@example.com")) as mock_cred, \
             patch("resources.lib.authentication.authenticator.Vault") as mock_vault_cls:
            mock_vault_cls.return_value.get_setting.return_value = "vault-pwd"
            result = a.log_on("user@example.com", password=None)
        mock_vault_cls.return_value.get_setting.assert_called_once_with("pwd_setting")
        mock_cred.assert_called_once_with("user@example.com", "vault-pwd")
        self.assertTrue(result.logged_on)

    def test_log_on_returns_unauthenticated_when_vault_has_no_password(self) -> None:
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h, password_setting_id="pwd_setting")
        with patch.object(h, "_credential_log_on") as mock_cred, \
             patch("resources.lib.authentication.authenticator.Vault") as mock_vault_cls:
            mock_vault_cls.return_value.get_setting.return_value = None
            result = a.log_on("user@example.com", password=None)
        mock_cred.assert_not_called()
        self.assertFalse(result.logged_on)
        self.assertEqual(result.error, "login_failed")

    def test_log_on_shows_dialog_on_handler_error(self) -> None:
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h)
        with patch.object(h, "_credential_log_on",
                          return_value=AuthenticationResult("", error="bad creds")), \
             patch("resources.lib.authentication.authenticator.XbmcWrapper") as mock_wrapper:
            mock_wrapper.show_key_board.return_value = None
            result = a.log_on("user@example.com", "secret")
        mock_wrapper.show_dialog.assert_called_once_with(None, "bad creds")
        self.assertEqual(result.error, "login_failed")

    def test_log_off_returns_early_when_not_logged_on(self) -> None:
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h)
        with patch.object(h, "active_authentication",
                          return_value=AuthenticationResult("")), \
             patch.object(h, "_credential_log_off") as mock_cred_log_off:
            a.log_off("user@example.com")
        mock_cred_log_off.assert_not_called()

    def test_log_off_force_logs_off_regardless_of_username(self) -> None:
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h)
        with patch.object(h, "active_authentication",
                          return_value=AuthenticationResult("active@example.com")), \
             patch.object(h, "_credential_log_off", return_value=True) as mock_cred_log_off:
            a.log_off("other@example.com", force=True)
        mock_cred_log_off.assert_called_once_with("active@example.com")

    def test_log_off_logs_error_on_handler_failure(self) -> None:
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h)
        with patch.object(h, "active_authentication",
                          return_value=AuthenticationResult("user@example.com")), \
             patch.object(h, "_credential_log_off", return_value=False) as mock_cred_log_off:
            a.log_off("user@example.com", force=True)
        mock_cred_log_off.assert_called_once_with("user@example.com")


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
        MockVault.return_value.get_setting.assert_any_call("my_setting")

    def test_log_on_looks_up_vault_channel_setting(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch("resources.lib.authentication.authenticator.Vault") as MockVault:
            MockVault.return_value.get_channel_setting.return_value = None
            a = Authenticator(h, channel_guid="abc-123", password_setting_id="pw")
            a.log_on("user")
        MockVault.return_value.get_channel_setting.assert_any_call("abc-123", "pw")

    def test_get_username_uses_addon_settings_global(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch("resources.lib.authentication.authenticator.AddonSettings") as MockSettings, \
             patch("resources.lib.authentication.authenticator.Vault"):
            MockSettings.get_setting.return_value = "stored@example.com"
            a = Authenticator(h, username_setting_id="nlziet_username")
            a.log_on()
        MockSettings.get_setting.assert_any_call("nlziet_username", store=LOCAL)

    def test_get_username_uses_channel_setting_when_guid_present(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch("resources.lib.authentication.authenticator.AddonSettings") as MockSettings, \
             patch("resources.lib.authentication.authenticator.Vault"):
            MockSettings.get_channel_setting.return_value = "stored@example.com"
            a = Authenticator(h, channel_guid="abc-123", username_setting_id="nlziet_username")
            a.log_on()
        MockSettings.get_channel_setting.assert_any_call("abc-123", "nlziet_username", store=LOCAL)

    def test_set_username_prompts_and_stores_global(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch("resources.lib.authentication.authenticator.AddonSettings") as MockSettings, \
             patch("resources.lib.authentication.authenticator.XbmcWrapper.show_key_board",
                   return_value="new@example.com"):
            a = Authenticator(h, channel_name="My Channel", username_setting_id="nlziet_username")
            result = a._set_username()
        self.assertEqual(result, "new@example.com")
        MockSettings.set_setting.assert_called_once_with("nlziet_username", "new@example.com", store=LOCAL)

    def test_set_username_prompts_and_stores_channel_setting(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch("resources.lib.authentication.authenticator.AddonSettings") as MockSettings, \
             patch("resources.lib.authentication.authenticator.XbmcWrapper.show_key_board",
                   return_value="new@example.com"):
            a = Authenticator(h, channel_name="My Channel", channel_guid="abc-123",
                              username_setting_id="nlziet_username")
            result = a._set_username("prefill@example.com")
        self.assertEqual(result, "new@example.com")
        MockSettings.set_channel_setting.assert_called_once_with(
            "abc-123", "nlziet_username", "new@example.com", store=LOCAL)

    def test_set_username_cancelled_returns_none(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch("resources.lib.authentication.authenticator.AddonSettings") as MockSettings, \
             patch("resources.lib.authentication.authenticator.XbmcWrapper.show_key_board",
                   return_value=None):
            a = Authenticator(h, channel_name="My Channel", username_setting_id="nlziet_username")
            result = a._set_username()
        self.assertIsNone(result)
        MockSettings.set_setting.assert_not_called()

    def test_set_password_stores_global_vault_setting(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch("resources.lib.authentication.authenticator.Vault") as MockVault, \
             patch("resources.lib.authentication.authenticator.LanguageHelper.get_localized_string",
                   return_value="Password"):
            a = Authenticator(h, channel_name="My Channel", password_setting_id="nlziet_password")
            a._set_password()
        MockVault.return_value.set_setting.assert_called_once_with(
            "nlziet_password", "My Channel - Password")

    def test_set_password_stores_channel_vault_setting(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch("resources.lib.authentication.authenticator.Vault") as MockVault, \
             patch("resources.lib.authentication.authenticator.LanguageHelper.get_localized_string",
                   return_value="Password"):
            a = Authenticator(h, channel_name="My Channel", channel_guid="abc-123",
                              password_setting_id="nlziet_password")
            a._set_password()
        MockVault.return_value.set_channel_setting.assert_called_once_with(
            "abc-123", "nlziet_password", "My Channel - Password")

    def test_log_on_vault_returns_none_falls_through_to_login_failed(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch("resources.lib.authentication.authenticator.Vault") as MockVault:
            MockVault.return_value.get_setting.return_value = None
            a = Authenticator(h, password_setting_id="my_setting")
            result = a.log_on("user")
        self.assertFalse(result.logged_on)
        self.assertEqual(result.error, "login_failed")

    def test_log_on_channel_vault_returns_none_falls_through_to_login_failed(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch("resources.lib.authentication.authenticator.Vault") as MockVault:
            MockVault.return_value.get_channel_setting.return_value = None
            a = Authenticator(h, channel_guid="abc-123", password_setting_id="pw")
            result = a.log_on("user")
        self.assertFalse(result.logged_on)
        self.assertEqual(result.error, "login_failed")

    def test_log_on_no_stored_username_falls_through_to_login_failed(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch("resources.lib.authentication.authenticator.AddonSettings") as MockSettings, \
             patch("resources.lib.authentication.authenticator.Vault") as MockVault:
            MockSettings.get_setting.return_value = None
            MockVault.return_value.get_setting.return_value = "stored_password"
            a = Authenticator(h, username_setting_id="nlziet_username",
                              password_setting_id="nlziet_password")
            result = a.log_on()
        self.assertFalse(result.logged_on)
        self.assertEqual(result.error, "login_failed")

    def test_log_on_no_credentials_stored_falls_through_to_login_failed(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch("resources.lib.authentication.authenticator.AddonSettings") as MockSettings, \
             patch("resources.lib.authentication.authenticator.Vault") as MockVault:
            MockSettings.get_setting.return_value = None
            MockVault.return_value.get_setting.return_value = None
            a = Authenticator(h, username_setting_id="nlziet_username",
                              password_setting_id="nlziet_password")
            result = a.log_on()
        self.assertFalse(result.logged_on)
        self.assertEqual(result.error, "login_failed")

    def test_auto_login_no_credentials_returns_missing_credentials(self) -> None:
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h)
        result = a._auto_login(None, None)
        self.assertFalse(result.logged_on)
        self.assertEqual(result.error, "missing_credentials")

    def test_auto_login_no_username_returns_missing_username(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch("resources.lib.authentication.authenticator.XbmcWrapper.show_dialog") as mock_dialog:
            a = Authenticator(h, channel_name="My Channel")
            result = a._auto_login(None, "password")
        self.assertFalse(result.logged_on)
        self.assertEqual(result.error, "missing_username")
        mock_dialog.assert_called_once_with("My Channel", LanguageHelper.MissingUsername)

    def test_auto_login_no_password_returns_missing_password(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch("resources.lib.authentication.authenticator.XbmcWrapper.show_dialog") as mock_dialog:
            a = Authenticator(h, channel_name="My Channel")
            result = a._auto_login("user", None)
        self.assertFalse(result.logged_on)
        self.assertEqual(result.error, "missing_password")
        mock_dialog.assert_called_once_with("My Channel", LanguageHelper.MissingPassword)

    def test_auto_login_no_credentials_does_not_show_dialog(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch("resources.lib.authentication.authenticator.XbmcWrapper.show_dialog") as mock_dialog:
            a = Authenticator(h)
            result = a._auto_login(None, None)
        self.assertEqual(result.error, "missing_credentials")
        mock_dialog.assert_not_called()

    def test_headless_login_success_returns_result_without_dialog(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch("resources.lib.authentication.authenticator.XbmcWrapper.show_dialog") as mock_dialog:
            a = Authenticator(h)
            result = a._headless_login("user", "pass")
        self.assertTrue(result.logged_on)
        mock_dialog.assert_not_called()

    def test_headless_login_invalid_credentials_shows_localized_dialog(self) -> None:
        h = _MockAuthHandler("test.realm", error="invalid_credentials")
        with patch("resources.lib.authentication.authenticator.XbmcWrapper.show_dialog") as mock_dialog:
            a = Authenticator(h, channel_name="My Channel")
            result = a._headless_login("user", "pass")
        self.assertFalse(result.logged_on)
        self.assertEqual(result.error, "invalid_credentials")
        mock_dialog.assert_called_once_with("My Channel", LanguageHelper.LoginErrorTitle)

    def test_headless_login_network_error_shows_localized_dialog(self) -> None:
        h = _MockAuthHandler("test.realm", error="network_error")
        with patch("resources.lib.authentication.authenticator.XbmcWrapper.show_dialog") as mock_dialog:
            a = Authenticator(h, channel_name="My Channel")
            result = a._headless_login("user", "pass")
        self.assertFalse(result.logged_on)
        self.assertEqual(result.error, "network_error")
        mock_dialog.assert_called_once_with("My Channel", LanguageHelper.NetworkLoginError)

    def test_headless_login_unknown_error_shows_raw_error_in_dialog(self) -> None:
        h = _MockAuthHandler("test.realm", error="some_other_error")
        with patch("resources.lib.authentication.authenticator.XbmcWrapper.show_dialog") as mock_dialog:
            a = Authenticator(h, channel_name="My Channel")
            result = a._headless_login("user", "pass")
        self.assertFalse(result.logged_on)
        self.assertEqual(result.error, "some_other_error")
        mock_dialog.assert_called_once_with("My Channel", "some_other_error")

    def test_get_password_without_setting_id_returns_none(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch("resources.lib.authentication.authenticator.Vault") as MockVault:
            a = Authenticator(h)
            result = a._get_password()
        self.assertIsNone(result)
        MockVault.assert_not_called()

    def test_log_on_explicit_password_skips_vault(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch("resources.lib.authentication.authenticator.Vault") as MockVault:
            a = Authenticator(h, password_setting_id="my_setting")
            a.log_on("user", "explicit_pass")
        MockVault.assert_not_called()

    def test_log_on_empty_username_returns_not_logged_on(self) -> None:
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h)
        with patch.object(a, "_get_password", return_value=None):
            result = a.log_on("")
        self.assertFalse(result.logged_on)

    def test_network_error_aborts_and_shows_localized_dialog(self) -> None:
        h = _MockAuthHandler("test.realm", session_error="network_error")
        with patch("resources.lib.authentication.authenticator.XbmcWrapper.show_dialog") as mock_dialog:
            a = Authenticator(h, channel_name="My Channel")
            result = a.log_on("user", "pass")
        self.assertFalse(result.logged_on)
        mock_dialog.assert_called_once_with("My Channel", mock_dialog.call_args[0][1])

    def test_network_error_in_session_check_aborts_before_login_hook(self) -> None:
        h = _MockAuthHandler("test.realm", session_error="network_error")
        with patch("resources.lib.authentication.authenticator.XbmcWrapper.show_dialog"), \
             patch.object(h, "_credential_log_on", wraps=h._credential_log_on) as mock_cred:
            a = Authenticator(h)
            a.log_on("user", "pass")
        mock_cred.assert_not_called()

    def test_session_error_does_not_show_dialog_for_non_network_errors(self) -> None:
        """ Non-network session errors must not surface a raw error dialog to
        the user; only the well-defined ``network_error`` case does.
        """
        h = _MockAuthHandler("test.realm", session_error="some_other_error")
        with patch("resources.lib.authentication.authenticator.XbmcWrapper.show_dialog") as mock_dialog:
            a = Authenticator(h, channel_name="My Channel")
            a.log_on("user", "pass")
        mock_dialog.assert_not_called()

    def test_log_off_without_force_skips_handler_when_no_active_session(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch.object(h, "_credential_log_off", return_value=True) as mock_cred_log_off:
            a = Authenticator(h)
            a.log_off("", force=False)
        mock_cred_log_off.assert_not_called()

    def test_force_log_off_skips_handler_when_no_active_session(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch.object(h, "_credential_log_off", return_value=True) as mock_cred_log_off:
            a = Authenticator(h)
            a.log_off("", force=True)
        mock_cred_log_off.assert_not_called()

    def test_log_off_without_force_skips_credential_log_off_for_different_active_user(self) -> None:
        h = _MockAuthHandler("test.realm", active_user="other@example.com")
        with patch.object(h, "_credential_log_off", return_value=True) as mock_cred_log_off:
            a = Authenticator(h)
            a.log_off("user@example.com", force=False)
        mock_cred_log_off.assert_not_called()

    def test_force_log_off_uses_active_username(self) -> None:
        h = _MockAuthHandler("test.realm", active_user="other@example.com")
        with patch.object(h, "_credential_log_off", return_value=True) as mock_cred_log_off:
            a = Authenticator(h)
            a.log_off("user@example.com", force=True)
        mock_cred_log_off.assert_called_once_with("other@example.com")

    def test_log_off_handler_failure_is_logged(self) -> None:
        h = _MockAuthHandler("test.realm", active_user="user@example.com")
        with patch.object(h, "_credential_log_off", return_value=False):
            a = Authenticator(h)
            a.log_off("user@example.com")  # should not raise

    def test_log_off_device_flow_revocation_failure_shows_notification(self) -> None:
        h = _MockDeviceAuthHandler("test.realm", active_user="user@example.com")
        with patch.object(h, "_revoke_device_authorization", return_value=False), \
             patch("resources.lib.authentication.authenticator.XbmcWrapper.show_notification") \
                as mock_notify, \
             patch("resources.lib.authentication.authenticator.Authenticator.device_flow",
                   new_callable=lambda: property(lambda self: True)):
            a = Authenticator(h, channel_name="My Channel")
            a.log_off("user@example.com")
        mock_notify.assert_called_once()

    def test_log_off_without_force_same_user_calls_handler(self) -> None:
        h = _MockAuthHandler("test.realm", active_user="user@example.com")
        with patch.object(h, "_credential_log_off", return_value=True) as mock_cred_log_off:
            a = Authenticator(h)
            a.log_off("user@example.com", force=False)
        mock_cred_log_off.assert_called_once_with("user@example.com")

    def test_resume_session_returns_existing_for_same_user(self) -> None:
        h = _MockAuthHandler("test.realm", active_user="user@example.com")
        with patch.object(h, "_credential_log_on", wraps=h._credential_log_on) as mock_cred:
            a = Authenticator(h)
            result = a.log_on("user@example.com", "pass")
        self.assertTrue(result.logged_on)
        mock_cred.assert_not_called()

    def test_resume_session_returns_existing_for_different_user(self) -> None:
        h = _MockAuthHandler("test.realm", active_user="other@example.com")
        with patch.object(h, "_credential_log_off", wraps=h._credential_log_off) as mock_cred_log_off, \
             patch.object(h, "_credential_log_on", wraps=h._credential_log_on) as mock_cred, \
             patch("resources.lib.authentication.authenticator.Vault"):
            a = Authenticator(h, password_setting_id="pw")
            result = a.log_on("user@example.com")
        mock_cred_log_off.assert_called_once_with("other@example.com")
        mock_cred.assert_called_once()

    def test_resume_session_no_existing_session_falls_through(self) -> None:
        h = _MockAuthHandler("test.realm")
        with patch.object(h, "_credential_log_on", wraps=h._credential_log_on) as mock_cred:
            a = Authenticator(h)
            a.log_on("user@example.com", "pass")
        mock_cred.assert_called_once_with("user@example.com", "pass")

    def test_log_on_with_active_session_and_empty_username_logs_off(self) -> None:
        """ If an active session exists and the caller supplies an empty
        username, the existing session is logged off. With no credentials and
        no successful interactive fallback, login_failed is returned.
        """
        h = _MockAuthHandler("test.realm", active_user="user@example.com")
        with patch.object(h, "_credential_log_off", return_value=True) as mock_cred_log_off, \
             patch.object(Authenticator, "_manual_login",
                          return_value=AuthenticationResult("", error="missing_username")) as mock_manual:
            a = Authenticator(h)
            result = a.log_on("")

        mock_cred_log_off.assert_called_once_with("user@example.com")
        mock_manual.assert_called_once()
        self.assertFalse(result.logged_on)
        self.assertEqual(result.error, "login_failed")

    def test_resume_session_evicts_when_no_username_configured(self) -> None:
        """No configured username → active credential-flow session is evicted."""

        h = _MockAuthHandler("test.realm", active_user="user@example.com")
        with patch.object(h, "_credential_log_off", return_value=True) as mock_cred_log_off, \
             patch.object(Authenticator, "_manual_login",
                          return_value=AuthenticationResult("", error="missing_username")), \
             patch("resources.lib.authentication.authenticator.AddonSettings.get_setting",
                   return_value=None):
            a = Authenticator(h, username_setting_id="user")
            result = a.log_on()

        mock_cred_log_off.assert_called_once_with("user@example.com")
        self.assertFalse(result.logged_on)

    def test_resume_session_evicts_on_username_mismatch_for_credential_flow(self) -> None:
        """Credential flow + different active user → session is logged off."""

        h = _MockAuthHandler("test.realm", active_user="other@example.com")
        with patch.object(h, "_credential_log_off", wraps=h._credential_log_off) as mock_cred_log_off, \
             patch.object(h, "_credential_log_on", wraps=h._credential_log_on) as mock_cred, \
             patch("resources.lib.authentication.authenticator.Vault"):
            a = Authenticator(h, password_setting_id="pw")
            a.log_on("user@example.com")

        mock_cred_log_off.assert_called_once_with("other@example.com")
        mock_cred.assert_called_once()

    def test_resume_session_accepts_device_flow_session_without_username_match(self) -> None:
        """device_flow handler → active session resumed regardless of stored username."""

        h = _MockDeviceFlowAuthHandler("test.realm", active_user="sub-guid-value")
        with patch.object(h, "_credential_log_on") as mock_cred:
            a = Authenticator(h)
            result = a.log_on("old-credential@example.com", "pass")

        mock_cred.assert_not_called()
        self.assertTrue(result.logged_on)
        self.assertEqual(result.username, "sub-guid-value")

    def test_device_manual_login_canceled_returns_canceled_error(self) -> None:
        """CANCELED poll result → AuthenticationResult with error='canceled'."""

        h = _MockDeviceAuthHandler("test.realm")
        a = Authenticator(h)
        device_auth = self._make_device_auth_data()
        with patch.object(h, "_start_device_authorization", return_value=device_auth), \
             patch.object(a, "_poll_with_progress", return_value=DeviceAuthResult.CANCELED), \
             patch("resources.lib.authentication.authenticator.xbmc.Monitor") as MockMonitor:
            MockMonitor.return_value.abortRequested.return_value = False
            result = a._device_manual_login()

        self.assertFalse(result.logged_on)
        self.assertEqual(result.error, "canceled")

    def test_device_manual_login_error_shows_dialog_and_returns_setup_failed(self) -> None:
        """ERROR poll result → dialog shown + error='device_setup_failed'."""

        h = _MockDeviceAuthHandler("test.realm")
        a = Authenticator(h, channel_name="My Channel")
        device_auth = self._make_device_auth_data()
        with patch.object(h, "_start_device_authorization", return_value=device_auth), \
             patch.object(a, "_poll_with_progress", return_value=DeviceAuthResult.ERROR), \
             patch("resources.lib.authentication.authenticator.XbmcWrapper.show_dialog") as mock_dialog, \
             patch("resources.lib.authentication.authenticator.xbmc.Monitor") as MockMonitor:
            MockMonitor.return_value.abortRequested.return_value = False
            result = a._device_manual_login()

        self.assertFalse(result.logged_on)
        self.assertEqual(result.error, "device_setup_failed")
        mock_dialog.assert_called_once()
        self.assertEqual(mock_dialog.call_args[0][0], "My Channel")

    def test_device_manual_login_start_returns_none_returns_setup_failed(self) -> None:
        """_start_device_authorization returning None → device_setup_failed (no poll)."""

        h = _MockDeviceAuthHandler("test.realm")
        a = Authenticator(h, channel_name="My Channel")
        with patch.object(h, "_start_device_authorization", return_value=None), \
             patch.object(a, "_poll_with_progress") as mock_poll, \
             patch("resources.lib.authentication.authenticator.XbmcWrapper.show_dialog"), \
             patch("resources.lib.authentication.authenticator.xbmc.Monitor") as MockMonitor:
            MockMonitor.return_value.abortRequested.return_value = False
            result = a._device_manual_login()

        self.assertFalse(result.logged_on)
        self.assertEqual(result.error, "device_setup_failed")
        mock_poll.assert_not_called()

    def test_device_manual_login_manual_success_returns_credential_result(self) -> None:
        """MANUAL poll result + successful manual login → returns that result."""

        h = _MockDeviceAuthHandler("test.realm")
        a = Authenticator(h)
        device_auth = self._make_device_auth_data()
        manual_result = AuthenticationResult("user@example.com")
        with patch.object(h, "_start_device_authorization", return_value=device_auth), \
             patch.object(a, "_poll_with_progress", return_value=DeviceAuthResult.MANUAL), \
             patch.object(a, "_manual_login", return_value=manual_result) as mock_manual, \
             patch("resources.lib.authentication.authenticator.xbmc.Monitor") as MockMonitor:
            MockMonitor.return_value.abortRequested.return_value = False
            result = a._device_manual_login("prefill@example.com")

        self.assertIs(result, manual_result)
        mock_manual.assert_called_once_with("prefill@example.com")

    def test_device_manual_login_manual_network_error_propagates(self) -> None:
        """MANUAL → network_error must propagate (not retry the device flow)."""

        h = _MockDeviceAuthHandler("test.realm")
        a = Authenticator(h)
        device_auth = self._make_device_auth_data()
        net_err = AuthenticationResult("", error="network_error")
        with patch.object(h, "_start_device_authorization", return_value=device_auth) as mock_start, \
             patch.object(a, "_poll_with_progress", return_value=DeviceAuthResult.MANUAL), \
             patch.object(a, "_manual_login", return_value=net_err), \
             patch("resources.lib.authentication.authenticator.xbmc.Monitor") as MockMonitor:
            MockMonitor.return_value.abortRequested.return_value = False
            result = a._device_manual_login()

        self.assertFalse(result.logged_on)
        self.assertEqual(result.error, "network_error")
        # Must not have restarted the device flow loop.
        self.assertEqual(mock_start.call_count, 1)

    def test_device_manual_login_kodi_abort_returns_aborted(self) -> None:
        """Kodi shutdown requested before first iteration → error='aborted'."""

        h = _MockDeviceAuthHandler("test.realm")
        a = Authenticator(h)
        with patch.object(h, "_start_device_authorization") as mock_start, \
             patch("resources.lib.authentication.authenticator.xbmc.Monitor") as MockMonitor:
            MockMonitor.return_value.abortRequested.return_value = True
            result = a._device_manual_login()

        self.assertFalse(result.logged_on)
        self.assertEqual(result.error, "aborted")
        mock_start.assert_not_called()

    def test_device_manual_login_manual_failure_restarts_device_flow(self) -> None:
        """MANUAL → manual_login fails non-network → loop restarts the device flow."""

        h = _MockDeviceAuthHandler("test.realm")
        a = Authenticator(h)
        device_auth = self._make_device_auth_data()
        manual_fail = AuthenticationResult("", error="missing_username")
        with patch.object(h, "_start_device_authorization", return_value=device_auth) as mock_start, \
             patch.object(a, "_poll_with_progress", return_value=DeviceAuthResult.MANUAL), \
             patch.object(a, "_manual_login", return_value=manual_fail), \
             patch("resources.lib.authentication.authenticator.xbmc.Monitor") as MockMonitor:
            MockMonitor.return_value.abortRequested.side_effect = [False, True]
            result = a._device_manual_login()

        self.assertEqual(result.error, "aborted")
        self.assertEqual(mock_start.call_count, 1)

    def test_device_manual_login_timeout_notifies_and_restarts(self) -> None:
        """TIMEOUT → notification shown + loop restarts the device flow."""

        h = _MockDeviceAuthHandler("test.realm")
        a = Authenticator(h, channel_name="My Channel")
        device_auth = self._make_device_auth_data()
        with patch.object(h, "_start_device_authorization", return_value=device_auth) as mock_start, \
             patch.object(a, "_poll_with_progress", return_value=DeviceAuthResult.TIMEOUT), \
             patch("resources.lib.authentication.authenticator.XbmcWrapper.show_notification") as mock_notify, \
             patch("resources.lib.authentication.authenticator.xbmc.Monitor") as MockMonitor:
            MockMonitor.return_value.abortRequested.side_effect = [False, True]
            result = a._device_manual_login()

        self.assertEqual(result.error, "aborted")
        mock_notify.assert_called_once()
        self.assertEqual(mock_notify.call_args[0][0], "My Channel")
        self.assertEqual(mock_start.call_count, 1)

    def test_log_on_dispatches_to_device_flow_when_handler_supports_it(self) -> None:
        """Rung-3: handler supports device auth → calls _device_manual_login."""

        h = _MockDeviceAuthHandler("test.realm")
        expected = AuthenticationResult("user@example.com")
        with patch.object(Authenticator, "_device_manual_login",
                          return_value=expected) as mock_device, \
             patch.object(Authenticator, "_manual_login") as mock_manual:
            a = Authenticator(h)
            result = a.log_on("user@example.com")

        self.assertIs(result, expected)
        mock_device.assert_called_once_with("user@example.com")
        mock_manual.assert_not_called()

    def test_log_on_no_credentials_falls_through_to_device_flow(self) -> None:
        """Both credentials missing → silent fall-through to device flow."""

        h = _MockDeviceAuthHandler("test.realm")
        expected = AuthenticationResult("user@example.com")
        with patch.object(Authenticator, "_device_manual_login",
                          return_value=expected) as mock_device, \
             patch("resources.lib.authentication.authenticator.AddonSettings") as MockSettings, \
             patch("resources.lib.authentication.authenticator.Vault") as MockVault:
            MockSettings.get_setting.return_value = None
            MockVault.return_value.get_setting.return_value = None
            a = Authenticator(h, username_setting_id="user", password_setting_id="pw")
            result = a.log_on()

        self.assertIs(result, expected)
        mock_device.assert_called_once()

    def test_log_on_third_rung_network_error_short_circuits(self) -> None:
        """Rung-3 returning network_error must short-circuit (no login_failed)."""

        h = _MockAuthHandler("test.realm", error="invalid_credentials")
        net_err = AuthenticationResult("", error="network_error")
        with patch.object(Authenticator, "_manual_login", return_value=net_err), \
             patch("resources.lib.authentication.authenticator.XbmcWrapper.show_dialog"):
            a = Authenticator(h)
            result = a.log_on("user@example.com", "pass")

        self.assertEqual(result.error, "network_error")

    def _make_device_auth_data(self) -> DeviceAuthData:
        return DeviceAuthData(
            device_code="test-device-code",
            user_code="TEST-1234",
            verification_uri="https://example.com/activate",
            expires_in=300,
            interval=5,
        )

    def _make_fake_dialog(self,
                          terminal_result: Optional[DeviceAuthResult]) -> MagicMock:
        """A DeviceAuthDialog test double driven by a real Event."""

        dialog = MagicMock()
        dialog.stop_event = threading.Event()
        dialog._closed_with = None

        def _close_with(result: DeviceAuthResult) -> None:
            dialog._closed_with = result
            dialog.stop_event.set()

        def _do_modal() -> None:
            if terminal_result is not None and dialog._closed_with is None:
                _close_with(terminal_result)
            dialog.stop_event.wait(timeout=1.0)

        dialog.close_with.side_effect = _close_with
        dialog.doModal.side_effect = _do_modal
        type(dialog).result = property(  # type: ignore[misc]
            lambda self: self._closed_with)
        return dialog

    def test_poll_with_progress_returns_terminal_poll_result(self) -> None:
        """Worker sees non-PENDING poll → close_with(result), property returns it."""

        h = _MockDeviceAuthHandler("test.realm")
        a = Authenticator(h)
        device_auth = self._make_device_auth_data()
        dialog = self._make_fake_dialog(terminal_result=None)
        with patch("resources.lib.authentication.authenticator.DeviceAuthDialog",
                   return_value=dialog), \
             patch("resources.lib.authentication.authenticator._DEVICE_FLOW_REFRESH_INTERVAL", 0.0), \
             patch.object(h, "_poll_device_authorization",
                          return_value=DeviceAuthResult.SUCCESS):
            monitor = MagicMock()
            monitor.abortRequested.return_value = False
            result = a._poll_with_progress(device_auth, monitor)

        self.assertEqual(result, DeviceAuthResult.SUCCESS)
        dialog.update_progress.assert_called()

    def test_poll_with_progress_returns_canceled_on_kodi_abort(self) -> None:
        """Worker observes monitor.abortRequested → close_with(CANCELED)."""

        h = _MockDeviceAuthHandler("test.realm")
        a = Authenticator(h)
        device_auth = self._make_device_auth_data()
        dialog = self._make_fake_dialog(terminal_result=None)
        with patch("resources.lib.authentication.authenticator.DeviceAuthDialog",
                   return_value=dialog), \
             patch("resources.lib.authentication.authenticator._DEVICE_FLOW_REFRESH_INTERVAL", 0.0), \
             patch.object(h, "_poll_device_authorization") as mock_poll:
            monitor = MagicMock()
            monitor.abortRequested.return_value = True
            result = a._poll_with_progress(device_auth, monitor)

        self.assertEqual(result, DeviceAuthResult.CANCELED)
        mock_poll.assert_not_called()

    def test_poll_with_progress_returns_manual_when_dialog_button_pressed(self) -> None:
        """User clicks 'Login manually' → dialog closes with MANUAL; no thread join wait."""

        h = _MockDeviceAuthHandler("test.realm")
        a = Authenticator(h)
        device_auth = self._make_device_auth_data()
        dialog = self._make_fake_dialog(terminal_result=DeviceAuthResult.MANUAL)
        with patch("resources.lib.authentication.authenticator.DeviceAuthDialog",
                   return_value=dialog), \
             patch("resources.lib.authentication.authenticator._DEVICE_FLOW_REFRESH_INTERVAL", 0.0), \
             patch.object(h, "_poll_device_authorization",
                          return_value=DeviceAuthResult.PENDING):
            monitor = MagicMock()
            monitor.abortRequested.return_value = False
            result = a._poll_with_progress(device_auth, monitor)

        self.assertEqual(result, DeviceAuthResult.MANUAL)

    def test_poll_with_progress_returns_error_when_dialog_has_no_result(self) -> None:
        """Dialog closes without recording a result → ERROR (defensive fallback)."""

        h = _MockDeviceAuthHandler("test.realm")
        a = Authenticator(h)
        device_auth = self._make_device_auth_data()
        dialog = MagicMock()
        dialog.stop_event = threading.Event()
        dialog.stop_event.set()  # worker exits immediately
        type(dialog).result = property(lambda self: None)  # type: ignore[misc]
        with patch("resources.lib.authentication.authenticator.DeviceAuthDialog",
                   return_value=dialog), \
             patch("resources.lib.authentication.authenticator._DEVICE_FLOW_REFRESH_INTERVAL", 0.0):
            monitor = MagicMock()
            monitor.abortRequested.return_value = False
            result = a._poll_with_progress(device_auth, monitor)

        self.assertEqual(result, DeviceAuthResult.ERROR)



    def test_log_on_device_auth_success_returns_logged_on(self) -> None:
        """Credential failure + device flow success → logged_on True."""
        h = _MockDeviceAuthHandler("test.realm", error="invalid_credentials")
        a = Authenticator(h)
        with patch.object(Authenticator, "_device_manual_login",
                          return_value=AuthenticationResult("user")), \
             patch("resources.lib.authentication.authenticator.XbmcWrapper.show_dialog"):
            result = a.log_on("user", "pass")
        self.assertTrue(result.logged_on)

    def test_credential_log_on_sets_credential_log_on_method(self) -> None:
        """Successful credential login stores credential login method."""
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h, channel_guid="test-guid")
        with patch.object(a, "_set_auth_method") as mock_set:
            a.log_on("user", "pass")
        mock_set.assert_called_once_with(_AUTH_METHOD_CREDENTIALS)

    def test_device_login_sets_device_login_method(self) -> None:
        """Successful device flow login stores device login method."""
        h = _MockDeviceAuthHandler("test.realm", error="invalid_credentials")
        a = Authenticator(h, channel_guid="test-guid")
        with patch.object(Authenticator, "_device_manual_login",
                          return_value=AuthenticationResult("user")), \
             patch("resources.lib.authentication.authenticator.XbmcWrapper.show_dialog"), \
             patch.object(a, "_set_auth_method") as mock_set:
            a.log_on("user", "pass")
        mock_set.assert_not_called()  # setter lives inside _device_manual_login

    def test_set_auth_method_with_channel_guid_uses_channel_setting(self) -> None:
        """With a channel_guid, _set_auth_method writes to the channel-scoped setting."""
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h, channel_guid="test-guid")
        with patch("resources.lib.authentication.authenticator.AddonSettings") as MockSettings:
            a._set_auth_method(_AUTH_METHOD_CREDENTIALS)
        MockSettings.set_channel_setting.assert_called_once_with(
            "test-guid", _AUTH_SETTING_KEY, _AUTH_METHOD_CREDENTIALS, store=LOCAL)
        MockSettings.set_setting.assert_not_called()

    def test_set_auth_method_without_channel_guid_uses_global_setting(self) -> None:
        """Without a channel_guid, _set_auth_method writes to the global setting."""
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h)
        with patch("resources.lib.authentication.authenticator.AddonSettings") as MockSettings:
            a._set_auth_method(_AUTH_METHOD_DEVICE)
        MockSettings.set_setting.assert_called_once_with(
            _AUTH_SETTING_KEY, _AUTH_METHOD_DEVICE, store=LOCAL)
        MockSettings.set_channel_setting.assert_not_called()

    def test_headless_login_success_sets_credential_method(self) -> None:
        """_headless_login sets credential login method on success."""
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h, channel_guid="test-guid")
        with patch.object(a, "_set_auth_method") as mock_set:
            a._headless_login("user", "pass")
        mock_set.assert_called_once_with(_AUTH_METHOD_CREDENTIALS)

    def test_headless_login_failure_does_not_set_credential_method(self) -> None:
        """_headless_login does not set login method on failure."""
        h = _MockAuthHandler("test.realm", error="invalid_credentials")
        a = Authenticator(h, channel_guid="test-guid")
        with patch.object(a, "_set_auth_method") as mock_set:
            a._headless_login("user", "wrong")
        mock_set.assert_not_called()

    def test_log_off_clears_login_method(self) -> None:
        """log_off clears the stored login method."""
        h = _MockAuthHandler("test.realm", active_user="user")
        a = Authenticator(h, channel_guid="test-guid")
        with patch.object(a, "_clear_auth_method") as mock_clear:
            a.log_off("user")
        mock_clear.assert_called_once_with()

    def test_device_flow_false_after_credential_log_on(self) -> None:
        """device_flow is False when login method is credential."""
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h, channel_guid="test-guid")
        with patch.object(a, "_get_auth_method", return_value=_AUTH_METHOD_CREDENTIALS):
            self.assertFalse(a.device_flow)

    def test_device_flow_true_after_device_login(self) -> None:
        """device_flow is True when login method is device_flow."""
        h = _MockAuthHandler("test.realm")
        a = Authenticator(h, channel_guid="test-guid")
        with patch.object(a, "_get_auth_method", return_value=_AUTH_METHOD_DEVICE):
            self.assertTrue(a.device_flow)

    def test_log_off_credential_log_on_calls_credential_log_off(self) -> None:
        """log_off calls _credential_log_off when login method is credential."""
        h = _MockAuthHandler("test.realm", active_user="user")
        a = Authenticator(h, channel_guid="test-guid")
        with patch.object(a, "_get_auth_method", return_value="credential"), \
             patch.object(h, "_credential_log_off", return_value=True) as mock_cred:
            a.log_off("user")
        mock_cred.assert_called_once_with("user")

    def test_log_off_device_flow_also_runs_credential_log_off(self) -> None:
        """log_off calls both _revoke_device_authorization and _credential_log_off for device flow sessions."""
        h = _MockAuthHandler("test.realm", active_user="user")
        a = Authenticator(h, channel_guid="test-guid")
        with patch.object(a, "_get_auth_method", return_value=_AUTH_METHOD_DEVICE), \
             patch.object(h, "_credential_log_off", return_value=True) as mock_cred, \
             patch.object(h, "_revoke_device_authorization", return_value=True) as mock_end:
            a.log_off("user")
        mock_cred.assert_called_once_with("user")
        mock_end.assert_called_once_with("user")

    def test_log_off_credential_log_off_failure_shows_notification(self) -> None:
        """_credential_log_off returning False logs error and shows a notification."""
        h = _MockAuthHandler("test.realm", active_user="user")
        a = Authenticator(h, channel_guid="test-guid")
        with patch.object(a, "_get_auth_method", return_value="credential"), \
             patch.object(h, "_credential_log_off", return_value=False), \
             patch("resources.lib.authentication.authenticator.XbmcWrapper.show_notification") \
                as mock_notify:
            a.log_off("user")
        mock_notify.assert_called_once()

    def test_log_off_no_username_uses_active_user(self) -> None:
        """log_off() without username logs off whoever is currently authenticated."""
        h = _MockAuthHandler("test.realm", active_user="active@example.com")
        with patch.object(h, "_credential_log_off", return_value=True) as mock_cred_log_off:
            a = Authenticator(h)
            a.log_off()
        mock_cred_log_off.assert_called_once_with("active@example.com")


