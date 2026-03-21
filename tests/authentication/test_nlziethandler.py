# SPDX-License-Identifier: GPL-3.0-or-later

import base64
import datetime
import json
import os
import re
import secrets
import threading
import time
import unittest
import unittest.mock
from typing import Any, Callable, Optional

from resources.lib.authentication.nlziethandler import (
    API_ID_AUTHORIZE, AUTH_CLIENT_ID_KEY, DEVICE_CLIENT_ID, DEVICE_FLOW_USER_AGENT,
    NLZIETHandler, WEB_CLIENT_ID)
from resources.lib.authentication.authenticationhandler import DeviceAuthResult
from resources.lib.authentication.authenticationresult import AuthenticationResult
from resources.lib.helpers.jsonhelper import JsonHelper
from resources.lib.logger import Logger
from resources.lib.urihandler import UriHandler
from resources.lib.addonsettings import AddonSettings, LOCAL
from tests.authentication.nlziethandler_mocks import MOCK_INVALID_PASSWORD


# ============================================================================
# Test Helper Functions
# ============================================================================


def _make_fast_time() -> Callable[[], float]:
    # Seed at real epoch so token-expiry arithmetic (int(time.time()) + expires_in)
    # stays consistent with unpatched callers in the same test.
    t = [float(int(time.time()))]

    def _tick() -> float:
        t[0] += 10.0  # 10 s > DEVICE_FLOW_INTERVAL (5 s) — poll gate always passes
        return t[0]

    return _tick


_fast_time = _make_fast_time()


def generate_test_device_name() -> str:
    """Generate a unique test device name with timestamp and random ID.

    Format: retrospect_unit_test_YYYYMMDD_<8-char-hex>
    Example: retrospect_unit_test_20260218_a32a6c6f
    """

    date_str = datetime.datetime.now().strftime("%Y%m%d")
    uid = secrets.token_hex(4)
    return f"retrospect_unit_test_{date_str}_{uid}"


def submit_device_code(_handler: NLZIETHandler, user_code: str, device_name: str) -> bool:
    """Automate device code submission using authenticated session cookies."""

    Logger.info(f"Attempting to submit device code '{user_code}' with name '{device_name}'")

    uri_handler = UriHandler.instance()
    try:
        Logger.debug("Fetching https://id.nlziet.nl/device to get CSRF token...")
        html = uri_handler.open('https://id.nlziet.nl/device')

        if ('account/login' in uri_handler.status.url.lower() or
            'ReturnUrl' in html):
            Logger.error("Not authenticated - redirected to login page")
            return False

        csrf_match = re.search(
            r'<input[^>]*name=["\']__RequestVerificationToken["\'][^>]*value=["\']([^"\']+)["\']',
            html,
            re.IGNORECASE
        )
        if not csrf_match:
            csrf_match = re.search(
                r'<input[^>]*value=["\']([^"\']+)["\'][^>]*name=["\']__RequestVerificationToken["\']',
                html,
                re.IGNORECASE
            )
        if not csrf_match:
            Logger.error("Could not find CSRF token on /device page")
            return False

        csrf_token = csrf_match.group(1)
        Logger.debug(f"Found CSRF token: {csrf_token[:20]}...")

        from urllib.parse import urlencode
        form_data = {
            'Name': device_name,
            'Code': user_code,
            'button': '',
            '__RequestVerificationToken': csrf_token
        }

        Logger.info(f"Submitting device code to https://id.nlziet.nl/device")
        response = uri_handler.open(
            'https://id.nlziet.nl/device',
            params=urlencode(form_data)
        )

        response_url = uri_handler.status.url
        Logger.debug(f"Response URL after submission: {response_url}")

        if '/device/confirmed' in response_url:
            Logger.info("Device code submitted successfully - confirmed!")
            return True
        elif ('success' in response.lower() or
              'gekoppeld' in response.lower()):
            Logger.info("Device code submitted successfully (success message)")
            return True
        else:
            Logger.warning("Device code submitted but no success confirmation found")
            return False

    except Exception as error:
        Logger.error(f"Failed to submit device code: {error}")
        return False


# ============================================================================
# Test Class
# ============================================================================


class TestNLZIETAuthLive(unittest.TestCase):

    handler: NLZIETHandler
    username: Optional[str]
    password: Optional[str]

    @classmethod
    def setUpClass(cls) -> None:

        # 1. Initialize Logger
        Logger.create_logger(None, str(cls), min_log_level=0)

        # 2. Initialize UriHandler
        UriHandler.create_uri_handler(ignore_ssl_errors=False)

        # 3. Initialize Config and ensure addon_data directory exists
        from resources.lib.retroconfig import Config
        if not os.path.exists(Config.profileDir):
            os.makedirs(Config.profileDir, exist_ok=True)
            Logger.debug(f"Created profile directory: {Config.profileDir}")

        # 4. Initialize handler
        cls.handler = NLZIETHandler()

        # 5. Load credentials
        cls.username = os.getenv('NLZIET_USERNAME')
        cls.password = os.getenv('NLZIET_PASSWORD')

        if (not cls.username or
            not cls.password):
            raise unittest.SkipTest("NLZIET credentials not in environment.")


    @classmethod
    def tearDownClass(cls) -> None:
        """Clean up logger."""

        if Logger.instance():
            Logger.instance().close_log()


    def setUp(self) -> None:
        """Clean cookies before each test for total isolation."""

        UriHandler.delete_cookie(domain=".nlziet.nl")

    # =================================================================
    # CREDENTIAL LOGIN TESTS (Username/Password)
    # =================================================================

    # --- 1. PREFLIGHT CHECKS ---


    def test_01_preflight_empty_credentials(self) -> None:
        """Verify handler forwards empty credentials to the web form login flow."""

        with unittest.mock.patch.object(
            self.handler, "_web_form_login", return_value="invalid_credentials"
        ) as mock_login:
            result = self.handler._credential_log_on("", "")

        mock_login.assert_called_once_with("", "")
        self.assertIsInstance(result, AuthenticationResult)
        self.assertFalse(result.logged_on, "Should fail with empty credentials")
        self.assertEqual(result.error, "invalid_credentials")


    def test_02_preflight_invalid_password(self) -> None:
        """Verify backend rejects incorrect credentials with invalid_credentials error."""

        # Clear identity server session to prevent SSO bypass
        UriHandler.delete_cookie(domain="id.nlziet.nl")
        result = self.handler._credential_log_on(self.username, MOCK_INVALID_PASSWORD)  # type: ignore[arg-type]
        self.assertFalse(result.logged_on, "Should fail with wrong password")
        self.assertEqual(result.error, "invalid_credentials", "Wrong password must surface as invalid_credentials")

    # --- 2. AUTHENTICATION (_credential_log_on) ---


    def test_03_credential_login_success(self) -> None:
        """Main Integration: Full credential login flow against NLZIET."""

        Logger.info(f"NLZIET: Testing live _credential_log_on for {self.username}")

        result = self.handler._credential_log_on(self.username, self.password)  # type: ignore[arg-type]

        self.assertTrue(result.logged_on, f"Live login failed: {result.error}")
        self.assertIsNotNone(result.jwt, "No JWT returned")
        self.assertFalse(result.existing_login, "Should be new login")
        self.assertIsNotNone(result.username, "No username extracted")

        self.assertGreater(len(result.jwt), 100, "JWT too short")  # type: ignore[arg-type]
        self.assertEqual(result.jwt.count('.'), 2, "JWT should have 3 parts")  # type: ignore[union-attr]

        Logger.info(f"NLZIET: Authenticated as: {result.username}")

    # --- 3. SESSION PERSISTENCE ---


    def test_04_active_authentication_persistence(self) -> None:
        """Verify session persists in AddonSettings."""

        result = self.handler.active_authentication()

        self.assertTrue(result.logged_on, "Session not persisted")
        self.assertTrue(result.existing_login, "Should be existing session")

        token = self.handler.get_authentication_token()
        self.assertIsNotNone(token, "Stored token missing")

    # --- 4. DATA INTEGRITY ---


    def test_05_username_extraction_from_jwt(self) -> None:
        """Verify username extraction handles real NLZIET JWT claims.

        Uses token from previous tests (either test_03 or test_06)."""

        token = self.handler.get_authentication_token()
        if not token:
            self.skipTest("No token available - test_03 must run first")

        username = self.handler._get_username_from_access_token(token)
        self.assertIsNotNone(username, "Failed to extract username from real JWT")
        # Flexible assertion as suggested: check that it contains data
        self.assertGreater(len(username), 0, "Extracted username is empty")  # type: ignore[arg-type]
        Logger.info(f"NLZIET: Verified identity extraction: {username}")

    # --- 5. TOKEN REFRESH ---


    def test_06_token_refresh(self) -> None:
        """Verify automatic token refresh via silent re-authentication.

        Depends on test_03 having established a session with id_token."""

        # Verify we have an active session from previous test
        result = self.handler.active_authentication()
        if not result.logged_on:
            self.skipTest("No active session - test_03 must run first")

        # Force token expiry on both settings and cached instance state
        expiry_key = self.handler._token_key("access_token_expires_at")
        AddonSettings.set_setting(expiry_key, str(int(time.time()) - 100), store=LOCAL)
        self.handler._access_token_expires_at = int(time.time()) - 100

        # refresh_access_token() performs silent re-authentication when needed
        self.assertTrue(self.handler.refresh_access_token(), "Silent re-authentication failed")
        token = self.handler.get_authentication_token()
        self.assertIsNotNone(token, "Silent re-authentication failed")

        # Verify expiry was updated
        new_expiry = int(AddonSettings.get_setting(expiry_key, store=LOCAL))
        self.assertGreater(new_expiry, int(time.time()), "Expiry not updated after refresh")

    # --- 6. SESSION TERMINATION ---


    def test_07_log_off_cleanup(self) -> None:
        """Verify tokens are wiped and session cleared."""

        success = self.handler._credential_log_off(self.username)  # type: ignore[arg-type]
        self.assertTrue(success, "log_off returned False")

        result = self.handler.active_authentication()
        self.assertFalse(result.logged_on, "Session still active after log_off")

    # =================================================================
    # DEVICE FLOW TESTS (TV-Friendly Authentication)
    # =================================================================


    def test_08_device_flow_initiation(self) -> None:
        """Test device flow initiation with real API (does NOT complete login)."""

        # Create handler configured for device flow
        device_handler = NLZIETHandler()

        # Generate unique test device name
        device_name = generate_test_device_name()

        result = device_handler._start_device_authorization(device_name=device_name)

        self.assertIsNotNone(result, "Device flow should return a result")
        self.assertTrue(result.device_code)
        self.assertTrue(result.user_code)
        self.assertTrue(result.verification_uri)
        self.assertEqual(result.verification_uri, "https://nlziet.nl/koppel")
        self.assertGreater(result.expires_in, 0)
        self.assertGreater(result.interval, 0)

        Logger.info("=" * 60)
        Logger.info(f"Device flow initiated successfully!")
        Logger.info(f"Device name: {device_name}")
        Logger.info(f"User code: {result.user_code}")
        Logger.info(f"Verification URL: {result.verification_uri}")
        Logger.info(f"NOTE: Flow was NOT completed - no device added to account")
        Logger.info("=" * 60)


    @unittest.skipIf(os.getenv('TEST_NLZIET_DEVICE_FLOW', 'AUTO') not in ('AUTO', 'MANUAL'),
                     "Set TEST_NLZIET_DEVICE_FLOW=AUTO or MANUAL to enable device flow test")
    def test_09_device_flow_authentication(self) -> None:
        """Test device flow authentication and cleanup.

        Steps:
        1. Initiate device flow
        2. Ensure authentication (web login or existing SSO)
        3. Auto-submit device code
        4. Poll and complete device flow
        5. Activate device session (makes device visible in session list)
        6. Clean up: remove the device (AUTO mode only)

        Environment:
        - TEST_NLZIET_DEVICE_FLOW=AUTO: Full automation with cleanup
        - TEST_NLZIET_DEVICE_FLOW=MANUAL: Manual code submission, device left for testing
        """

        self._run_device_flow_authentication()


    def _run_device_flow_authentication(self) -> None:
        Logger.info("=" * 60)
        Logger.info("DEVICE FLOW AUTHENTICATION TEST")
        Logger.info("=" * 60)

        device_handler = NLZIETHandler()
        device_name = generate_test_device_name()

        Logger.info("Step 1: Initiating device flow...")
        device_info = device_handler._start_device_authorization(device_name=device_name)
        self.assertIsNotNone(device_info, "Failed to initiate device flow")
        assert device_info is not None
        user_code = device_info.user_code
        device_code = device_info.device_code

        Logger.info("Step 2: Ensuring authentication...")
        try:
            test_response = UriHandler.open("https://id.nlziet.nl/device", no_cache=True)
            if 'account/login' in UriHandler.last_status().url.lower():
                Logger.debug("No active session - logging in...")
                login_result = self.handler._credential_log_on(self.username or "", self.password or "")
                self.assertTrue(login_result.logged_on, f"Web login failed: {login_result.error}")
                Logger.info("Web login successful")
            else:
                Logger.info("Using existing SSO session")
        except Exception:
            login_result = self.handler._credential_log_on(self.username or "", self.password or "")
            self.assertTrue(login_result.logged_on, f"Web login failed: {login_result.error}")
            Logger.info("Web login successful")

        Logger.info("Step 3: Auto-submitting device code...")
        submission_result = {'success': False}


        def submit_with_result() -> None:
            submission_result['success'] = submit_device_code(self.handler, user_code, device_name)

        submit_thread = threading.Thread(target=submit_with_result)
        submit_thread.start()
        submit_thread.join(timeout=10)

        self.assertTrue(submission_result['success'], "Device code submission failed")
        Logger.info("Device code submitted")

        Logger.info("Step 4: Polling for device flow completion...")
        end_time = time.time() + device_info.expires_in
        success = False
        while time.time() < end_time:
            poll_result = device_handler._poll_device_authorization(device_code)
            if poll_result == DeviceAuthResult.SUCCESS:
                success = True
                break
            if poll_result == DeviceAuthResult.ERROR:
                break
            time.sleep(0.1)
        self.assertTrue(success, "Device flow polling failed")

        auth_result = device_handler.active_authentication()
        self.assertTrue(auth_result.logged_on, "Not authenticated after device flow")
        Logger.info(f"Device flow complete - authenticated as: {auth_result.username}")

        Logger.info("Step 5: Activating device...")
        userinfo = device_handler.get_user_info()
        self.assertIsNotNone(userinfo, "Failed to get user info after device flow")
        Logger.info("Device activated")

        device_flow_mode = os.getenv('TEST_NLZIET_DEVICE_FLOW', 'AUTO')
        Logger.info("Step 6: Verifying device was added to account...")

        devices, _ = device_handler._get_device_sessions_and_keys()
        self.assertIsNotNone(devices, "Failed to list devices for verification")

        device_found = any(d.get('name') == device_name for d in devices)
        self.assertTrue(device_found, f"Device '{device_name}' not found in account after creation!")
        Logger.info(f"Verified device '{device_name}' exists in account")

        device_key = next((d['key'] for d in devices if d.get('name') == device_name), None)

        if device_flow_mode == 'AUTO':
            Logger.info("Step 7: Cleanup - removing test device (AUTO mode)...")
            self.assertIsNotNone(device_key, f"Could not find device key for '{device_name}'")

            account_token = device_handler._get_account_access_token()
            removed = device_handler._remove_device(device_key or "", account_token=account_token)
            self.assertTrue(removed, f"Failed to remove device '{device_name}'")
            Logger.info(f"Device '{device_name}' removed")

            devices_after, _ = device_handler._get_device_sessions_and_keys()
            device_still_exists = (
                any(d.get('name') == device_name for d in devices_after) if devices_after else False)
            self.assertFalse(device_still_exists, f"Device '{device_name}' still exists after removal!")
            Logger.info(f"Verified device '{device_name}' was removed from account")

            for key in ['access_token', 'refresh_token', 'id_token', 'access_token_expires_at']:
                AddonSettings.set_setting(device_handler._token_key(key), "", store=LOCAL)
            Logger.info("Cleared device flow tokens")

            Logger.info("=" * 60)
            Logger.info("DEVICE FLOW TEST COMPLETE - Cleanup successful")
            Logger.info("=" * 60)
        else:
            Logger.info("=" * 60)
            Logger.info("DEVICE FLOW TEST COMPLETE - Device left for manual testing")
            Logger.info(f"Device name: {device_name}")
            if device_key:
                removal_url = f"https://mijn.nlziet.nl/instellingen/apparaten"
                Logger.info(f"Remove via web: {removal_url}")
                Logger.info(f"Device key: {device_key}")
            Logger.info("To remove later, use: test script with AUTO mode or mijn.nlziet.nl")
            Logger.info("=" * 60)


    @unittest.skipIf(os.getenv('TEST_NLZIET_DEVICE_FLOW', 'AUTO') not in ('AUTO', 'MANUAL'),
                     "Set TEST_NLZIET_DEVICE_FLOW=AUTO or MANUAL to enable device flow test")
    def test_10_device_flow_refresh_token(self) -> None:
        """Test device flow refresh token grant.

        Self-sufficient: If device flow tokens don't exist, performs device flow first.
        Then tests that refresh_token grant works correctly.
        """

        self._run_device_flow_refresh_token()


    def _run_device_flow_refresh_token(self) -> None:
        device_handler = NLZIETHandler()
        refresh_token = AddonSettings.get_setting(device_handler._token_key("refresh_token"), store=LOCAL)

        created_device_name = None
        device_flow_mode = os.getenv('TEST_NLZIET_DEVICE_FLOW', 'AUTO')

        if not refresh_token:
            Logger.info("No device flow tokens found - performing device flow authentication first...")

            device_name = generate_test_device_name()
            created_device_name = device_name

            device_info = device_handler._start_device_authorization(device_name=device_name)
            self.assertIsNotNone(device_info, "Failed to initiate device flow for test_10")
            assert device_info is not None

            if not self.handler.get_authentication_token():
                result = self.handler._credential_log_on(self.username or "", self.password or "")
                self.assertTrue(result.logged_on, "Web login failed in test_10 setup")

            submit_device_code(
                self.handler,
                device_info.user_code,
                device_name
            )

            end_time = time.time() + device_info.expires_in
            success = False
            while time.time() < end_time:
                poll_result = device_handler._poll_device_authorization(device_info.device_code)
                if poll_result == DeviceAuthResult.SUCCESS:
                    success = True
                    break
                if poll_result == DeviceAuthResult.ERROR:
                    break
                time.sleep(0.1)
            self.assertTrue(success, "Device flow polling failed in test_10 setup")

            # Activate the device session
            device_handler._get_device_sessions_and_keys()

            Logger.info("Device flow authentication complete for test_10")

            refresh_token = AddonSettings.get_setting(
                device_handler._token_key("refresh_token"), store=LOCAL)

        Logger.info("Testing device flow refresh token grant...")

        expiry_key = device_handler._token_key("access_token_expires_at")
        old_expiry = int(AddonSettings.get_setting(expiry_key, store=LOCAL) or 0)
        AddonSettings.set_setting(expiry_key, str(int(time.time()) - 10), store=LOCAL)
        device_handler._access_token_expires_at = int(time.time()) - 10

        self.assertTrue(device_handler.refresh_access_token(), "Refresh token grant failed")
        token = device_handler.get_authentication_token()
        self.assertIsNotNone(token, "Refresh token grant failed")

        new_expiry = int(AddonSettings.get_setting(expiry_key, store=LOCAL))
        self.assertGreater(new_expiry, int(time.time()), "Expiry not updated")

        Logger.info(
            f"Device refresh token grant successful - new expiry: {new_expiry - int(time.time())}s")

        if (created_device_name and
            device_flow_mode == 'AUTO'):
            Logger.info(f"Cleanup: Removing test device '{created_device_name}'...")

            devices, _ = device_handler._get_device_sessions_and_keys()
            if devices:
                device_key = next(
                    (d['key'] for d in devices if d.get('name') == created_device_name), None)
                if device_key:
                    account_token = device_handler._get_account_access_token()
                    removed = device_handler._remove_device(device_key, account_token=account_token)
                    if removed:
                        Logger.info(f"Cleanup: Device '{created_device_name}' removed")
                    else:
                        Logger.warning(f"Cleanup: Failed to remove device '{created_device_name}'")
                else:
                    Logger.warning(
                        f"Cleanup: Could not find device key for '{created_device_name}'")


    @unittest.skipIf(os.getenv('TEST_NLZIET_DEVICE_FLOW', 'AUTO') not in ('AUTO', 'MANUAL'),
                     "Set TEST_NLZIET_DEVICE_FLOW=AUTO or MANUAL to enable device flow test")
    def test_11_device_flow_log_off_removes_device(self) -> None:
        """Test that _revoke_device_authorization deregisters the device from the account.

        Steps:
        1. Log on via device flow (creates a device entry)
        2. Verify the device appears in the account's device list
        3. Call _revoke_device_authorization
        4. Verify the device is no longer in the account's device list
        """

        self._run_device_flow_log_off()


    def _run_device_flow_log_off(self) -> None:
        Logger.info("=" * 60)
        Logger.info("DEVICE FLOW LOG-OFF TEST")
        Logger.info("=" * 60)

        device_handler = NLZIETHandler()
        device_name = generate_test_device_name()

        Logger.info("Step 1: Initiating device flow...")
        device_info = device_handler._start_device_authorization(device_name=device_name)
        self.assertIsNotNone(device_info, "Failed to initiate device flow")
        assert device_info is not None
        user_code = device_info.user_code
        device_code = device_info.device_code

        try:
            UriHandler.open("https://id.nlziet.nl/device", no_cache=True)
            if 'account/login' in UriHandler.last_status().url.lower():
                Logger.debug("No active web session — logging in...")
                result = self.handler._credential_log_on(self.username or "", self.password or "")
                self.assertTrue(result.logged_on, f"Web login failed: {result.error}")
            else:
                Logger.info("Using existing SSO session")
        except Exception:
            result = self.handler._credential_log_on(self.username or "", self.password or "")
            self.assertTrue(result.logged_on, f"Web login failed: {result.error}")

        submit_device_code(self.handler, user_code, device_name)

        end_time = time.time() + device_info.expires_in
        success = False
        while time.time() < end_time:
            poll_result = device_handler._poll_device_authorization(device_code)
            if poll_result == DeviceAuthResult.SUCCESS:
                success = True
                break
            if poll_result == DeviceAuthResult.ERROR:
                break
            time.sleep(0.1)
        self.assertTrue(success, "Device flow polling failed")

        Logger.info("Step 2: Activating device and verifying it appears in account...")
        device_handler.get_user_info()
        devices_before, _ = device_handler._get_device_sessions_and_keys()
        self.assertIsNotNone(devices_before, "Failed to list devices before _revoke_device_authorization")
        device_found = any(d.get('name') == device_name for d in (devices_before or []))
        self.assertTrue(device_found, f"Device '{device_name}' not found in account after login")
        Logger.info(f"Verified device '{device_name}' exists in account")

        Logger.info("Step 3: Calling _revoke_device_authorization...")
        log_off_result = device_handler._revoke_device_authorization(self.username)  # type: ignore[arg-type]
        self.assertTrue(log_off_result, "_revoke_device_authorization returned False")
        Logger.info("_revoke_device_authorization returned True")

        Logger.info("Step 4: Verifying device was removed from account...")
        verifier = NLZIETHandler()
        devices_after, _ = verifier._get_device_sessions_and_keys()
        device_still_exists = any(
            d.get('name') == device_name for d in (devices_after or []))
        self.assertFalse(device_still_exists,
                         f"Device '{device_name}' still present after _revoke_device_authorization")
        Logger.info(f"Verified device '{device_name}' was removed by _revoke_device_authorization")

        Logger.info("=" * 60)
        Logger.info("DEVICE FLOW LOG-OFF TEST COMPLETE")
        Logger.info("=" * 60)


# Mocked Test Class — runs all tests against mock API responses
# ============================================================================


class TestNLZIETAuthMocked(TestNLZIETAuthLive):
    """Runs all NLZiet auth tests against mocked API responses.

    Inherits all test methods from TestNLZIETAuthLive. Overrides
    setUpClass to inject mock UriHandler.open() instead of requiring real
    credentials. This class always runs, even without NLZIET_USERNAME/PASSWORD.
    """

    _mock_dispatcher = None
    _original_open = None


    @classmethod
    def setUpClass(cls) -> None:
        """Initialize with mock dispatcher instead of real credentials."""

        from tests.authentication.nlziethandler_mocks import NLZietMockDispatcher

        Logger.create_logger(None, str(cls), min_log_level=0)
        UriHandler.create_uri_handler(ignore_ssl_errors=False)

        from resources.lib.retroconfig import Config
        if not os.path.exists(Config.profileDir):
            os.makedirs(Config.profileDir, exist_ok=True)

        cls.handler = NLZIETHandler()

        cls.username = "test@example.com"
        cls.password = "mock_password"

        cls._mock_dispatcher = NLZietMockDispatcher()
        cls._original_open = UriHandler.instance().open

        uri_handler_instance = UriHandler.instance()


        def mock_open(uri: str, proxy: Optional[str] = None, params: Optional[str] = None,
                      data: Optional[Any] = None, json: Optional[Any] = None,
                      referer: Optional[str] = None, additional_headers: Optional[Any] = None,
                      no_cache: bool = False, force_text: bool = False,
                      force_cache_duration: Optional[int] = None, method: str = "") -> str:
            return cls._mock_dispatcher.dispatch(  # type: ignore[union-attr]
                uri, uri_handler_instance,
                params=params, data=data, json=json, method=method,
                additional_headers=additional_headers
            )

        uri_handler_instance.open = mock_open


    @classmethod
    def tearDownClass(cls) -> None:
        """Restore original UriHandler.open and clean up auth state."""

        if cls._original_open:
            UriHandler.instance().open = cls._original_open

        # Clear NLZIET auth settings to prevent cross-test pollution.
        # Tests in this class write tokens and client-id settings to the shared
        # LocalSettings file; leaving them set causes channel tests that run
        # afterward to pick up stale tokens and appear logged-in.
        for client_prefix in ("nlziet_oauth2_triple-web_",
                               "nlziet_oauth2_triple-android-tv_"):
            for token_key in ("access_token", "refresh_token", "id_token",
                              "access_token_expires_at"):
                AddonSettings.set_setting(f"{client_prefix}{token_key}", "", store=LOCAL)
        AddonSettings.set_setting(AUTH_CLIENT_ID_KEY, "", store=LOCAL)

        super().tearDownClass()


    def setUp(self) -> None:
        """Reset mock state and clean cookies between tests."""

        if self._mock_dispatcher:
            self._mock_dispatcher.reset()
        UriHandler.delete_cookie(domain=".nlziet.nl")

    # Override to bypass parent's @skipIf — mock tests always run
    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time.time", new=_fast_time)
    def test_09_device_flow_authentication(self) -> None:
        self._run_device_flow_authentication()


    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time.time", new=_fast_time)
    def test_10_device_flow_refresh_token(self) -> None:
        self._run_device_flow_refresh_token()


    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time.time", new=_fast_time)
    def test_11_device_flow_log_off_removes_device(self) -> None:
        self._run_device_flow_log_off()


    def test_refresh_no_tokens_raises_value_error(self) -> None:
        """refresh_access_token returns None when no refresh or id token is stored."""

        AddonSettings.set_setting(self.handler._token_key("refresh_token"), "", store=LOCAL)
        AddonSettings.set_setting(self.handler._token_key("id_token"), "", store=LOCAL)
        self.handler._refresh_token = ""
        self.handler._id_token = ""
        self.handler._access_token_expires_at = 0  # force expiry so the margin check doesn't short-circuit
        self.assertIsNone(self.handler.refresh_access_token())


    def test_get_authentication_token_refreshes_when_expired(self) -> None:
        """get_authentication_token() proactively refreshes the token when it is near expiry."""

        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN, MOCK_REFRESH_TOKEN
        self.handler._access_token = MOCK_ACCESS_TOKEN
        self.handler._refresh_token = MOCK_REFRESH_TOKEN
        self.handler._access_token_expires_at = 0  # expired — get_authentication_token must refresh

        refresh_called = []
        original = self.handler._refresh_token_grant
        self.handler._refresh_token_grant = lambda: refresh_called.append(True) or original()  # type: ignore[method-assign, func-returns-value]
        try:
            token = self.handler.get_authentication_token()
        finally:
            self.handler._refresh_token_grant = original  # type: ignore[method-assign]

        self.assertIsNotNone(token, "Must return a token after refresh")
        self.assertNotEqual(
            refresh_called, [], "_refresh_token_grant MUST be called when token is expired")


    def test_instance_vars_loaded_from_settings_at_init(self) -> None:
        """Token state read from settings once at construction, not on every call."""

        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN, MOCK_REFRESH_TOKEN

        # Align AUTH_CLIENT_ID_KEY with self.handler so fresh NLZIETHandler() loads the same prefix.
        AddonSettings.set_setting(AUTH_CLIENT_ID_KEY, self.handler._client_id, store=LOCAL)
        AddonSettings.set_setting(self.handler._token_key("access_token"), MOCK_ACCESS_TOKEN, store=LOCAL)
        AddonSettings.set_setting(
            self.handler._token_key("refresh_token"), MOCK_REFRESH_TOKEN, store=LOCAL)

        fresh = NLZIETHandler()
        self.assertEqual(fresh._access_token, MOCK_ACCESS_TOKEN)
        self.assertEqual(fresh._refresh_token, MOCK_REFRESH_TOKEN)
        # access_token_expires_at is derived from the JWT exp claim, not from a stored setting
        self.assertEqual(fresh._access_token_expires_at, 9999999999)


    def test_id_token_loaded_for_device_flow_at_init(self) -> None:
        """_id_token is loaded from settings at init for device flow, not just web flow."""

        from tests.authentication.nlziethandler_mocks import MOCK_ID_TOKEN
        device_handler = NLZIETHandler()
        device_handler._client_id = DEVICE_CLIENT_ID
        AddonSettings.set_setting(AUTH_CLIENT_ID_KEY, DEVICE_CLIENT_ID, store=LOCAL)
        AddonSettings.set_setting(device_handler._token_key("id_token"), MOCK_ID_TOKEN, store=LOCAL)

        fresh = NLZIETHandler()
        self.assertEqual(fresh._id_token, MOCK_ID_TOKEN)


    def test_properties_correctness(self) -> None:
        """Verify NLZIET-specific OAuth2 properties are correctly configured."""

        # Check scopes
        self.assertEqual(self.handler.scopes, ["openid", "api"],
                         "NLZIET scopes must be ['openid', 'api']")

        # Check endpoints
        self.assertEqual(self.handler.device_authorization_endpoint,
                         "https://id.nlziet.nl/connect/deviceauthorization",
                         "Device auth endpoint incorrect")

        self.assertEqual(self.handler.token_endpoint,
                         "https://id.nlziet.nl/connect/token",
                         "Token endpoint incorrect")

        # Check client IDs based on flow mode
        web_handler = NLZIETHandler()
        web_handler._client_id = WEB_CLIENT_ID
        self.assertEqual(web_handler._client_id, "triple-web",
                         "Web flow must use triple-web client ID")


    def test_load_and_save_device_session_key(self) -> None:
        """Device session keys can be stored, loaded, and cleared explicitly."""

        from tests.authentication.nlziethandler_mocks import MOCK_SESSION_KEY

        device_handler = NLZIETHandler()
        device_handler._clear_device_session_key()
        self.assertEqual(device_handler._load_device_session_key(), "")

        device_handler._save_device_session_key(MOCK_SESSION_KEY)
        self.assertEqual(device_handler._load_device_session_key(), MOCK_SESSION_KEY)

        device_handler._clear_device_session_key()
        self.assertEqual(device_handler._load_device_session_key(), "")


    def test_find_device_session_key_from_diff_returns_new_key(self) -> None:
        """_find_device_session_key() returns the one key that appeared after registration."""

        from tests.authentication.nlziethandler_mocks import MOCK_SESSION_KEY

        device_handler = NLZIETHandler()
        device_handler._pre_registration_keys = {"EXISTING_KEY_1", "EXISTING_KEY_2"}

        ref_ts = 1700000000
        with unittest.mock.patch.object(device_handler, "_get_account_access_token", return_value="mock_account_token"):
            with unittest.mock.patch.object(
                device_handler, "_list_devices",
                return_value=[
                    {"key": "EXISTING_KEY_1", "createdAt": "2023-11-14T22:12:00Z"},
                    {"key": "EXISTING_KEY_2", "createdAt": "2023-11-14T22:11:00Z"},
                    {"key": MOCK_SESSION_KEY, "createdAt": "2023-11-14T22:13:20Z"},
                ]
            ):
                result = device_handler._find_device_session_key(ref_ts)

        self.assertEqual(result, MOCK_SESSION_KEY)
        self.assertIsNone(device_handler._pre_registration_keys)


    def test_find_device_session_key_from_diff_returns_none_when_no_new_key(self) -> None:
        """_find_device_session_key() returns None when the diff is empty."""

        device_handler = NLZIETHandler()
        device_handler._pre_registration_keys = {"EXISTING_KEY_1"}

        ref_ts = 1700000000
        with unittest.mock.patch.object(device_handler, "_get_account_access_token", return_value="mock_account_token"):
            with unittest.mock.patch.object(
                device_handler, "_list_devices",
                return_value=[{"key": "EXISTING_KEY_1", "createdAt": "2023-11-14T22:13:20Z"}]
            ):
                result = device_handler._find_device_session_key(ref_ts)

        self.assertIsNone(result)


    def test_find_device_session_key_from_diff_picks_closest_timestamp(self) -> None:
        """_find_device_session_key() picks the candidate closest to ref_ts when multiple new keys appear."""

        device_handler = NLZIETHandler()
        device_handler._pre_registration_keys = set()

        ref_ts = 1700000000
        with unittest.mock.patch.object(device_handler, "_get_account_access_token", return_value="mock_account_token"):
            with unittest.mock.patch.object(
                device_handler, "_list_devices",
                return_value=[
                    {"key": "NEW_KEY_1", "createdAt": "2023-11-14T22:13:20Z"},
                    {"key": "NEW_KEY_2", "createdAt": "2023-11-14T22:14:00Z"},
                ]
            ):
                result = device_handler._find_device_session_key(ref_ts)

        self.assertEqual(result, "NEW_KEY_1")


    def test_find_device_session_key_returns_none_when_no_account_token(self) -> None:
        """_find_device_session_key() returns None when the account token is unavailable."""

        device_handler = NLZIETHandler()
        device_handler._pre_registration_keys = set()

        ref_ts = 1700000000
        with unittest.mock.patch.object(device_handler, "_get_account_access_token", return_value=None):
            with unittest.mock.patch.object(device_handler, "_list_devices") as mock_list:
                result = device_handler._find_device_session_key(ref_ts)

        mock_list.assert_not_called()
        self.assertIsNone(result)


    def test_deregister_device_clears_stored_key_on_success(self) -> None:
        """_deregister_device() clears the stored key after a successful removal."""

        from tests.authentication.nlziethandler_mocks import MOCK_SESSION_KEY

        device_handler = NLZIETHandler()
        device_handler._save_device_session_key(MOCK_SESSION_KEY)

        with unittest.mock.patch.object(device_handler, "_get_account_access_token", return_value="mock_account_token"):
            with unittest.mock.patch.object(device_handler, "_remove_device", return_value=True) as mock_remove:
                result = device_handler._deregister_device()

        self.assertTrue(result)
        mock_remove.assert_called_once_with(MOCK_SESSION_KEY, "mock_account_token")
        self.assertEqual(device_handler._load_device_session_key(), "")


    def test_deregister_device_keeps_stored_key_on_failure(self) -> None:
        """_deregister_device() preserves the key when removal fails."""

        from tests.authentication.nlziethandler_mocks import MOCK_SESSION_KEY

        device_handler = NLZIETHandler()
        device_handler._save_device_session_key(MOCK_SESSION_KEY)

        with unittest.mock.patch.object(device_handler, "_get_account_access_token", return_value="mock_account_token"):
            with unittest.mock.patch.object(device_handler, "_remove_device", return_value=False) as mock_remove:
                result = device_handler._deregister_device()

        self.assertFalse(result)
        mock_remove.assert_called_once_with(MOCK_SESSION_KEY, "mock_account_token")
        self.assertEqual(device_handler._load_device_session_key(), MOCK_SESSION_KEY)
        device_handler._clear_device_session_key()


    def test_deregister_device_returns_false_when_no_key_stored(self) -> None:
        """_deregister_device() returns False when no session key is stored (device remains registered)."""

        device_handler = NLZIETHandler()
        device_handler._clear_device_session_key()

        with unittest.mock.patch.object(device_handler, "_remove_device") as mock_remove:
            result = device_handler._deregister_device()

        self.assertFalse(result)
        mock_remove.assert_not_called()


    def test_deregister_device_falls_back_to_device_token_when_no_account_token(self) -> None:
        """_deregister_device() falls back to the device access token when account token unavailable."""

        from tests.authentication.nlziethandler_mocks import MOCK_SESSION_KEY, MOCK_ACCESS_TOKEN

        device_handler = NLZIETHandler()
        device_handler._save_device_session_key(MOCK_SESSION_KEY)
        device_handler._access_token = MOCK_ACCESS_TOKEN

        with unittest.mock.patch.object(device_handler, "_get_account_access_token", return_value=None), \
             unittest.mock.patch.object(device_handler, "_remove_device",
                                        return_value=True) as mock_remove:
            result = device_handler._deregister_device()

        self.assertTrue(result)
        mock_remove.assert_called_once_with(MOCK_SESSION_KEY, MOCK_ACCESS_TOKEN)


    def test_get_account_access_token_includes_id_token_hint_when_available(self) -> None:
        """_get_account_access_token() includes id_token_hint in the authorize request when stored."""

        device_handler = NLZIETHandler()
        device_handler._id_token = "stored_id_token"

        authorize_urls: list = []

        def _fake_open(url: str, data: Optional[dict] = None, **kwargs: Any) -> str:
            if data is None:
                authorize_urls.append(url)
            return '{"access_token": "mock_account_token"}'

        redirect_url = "https://mijn.nlziet.nl/callback-silent.html?code=fake_code&state=FIXED"
        with unittest.mock.patch("resources.lib.urihandler.UriHandler.open",
                                 side_effect=_fake_open), \
             unittest.mock.patch("resources.lib.urihandler.UriHandler.instance") as mock_inst, \
             unittest.mock.patch("resources.lib.authentication.oidchandler.secrets.token_urlsafe",
                                 return_value="FIXED"):
            mock_inst.return_value.status.url = redirect_url
            mock_inst.return_value.status.error = False
            result = device_handler._get_account_access_token()

        self.assertEqual(result, "mock_account_token")
        self.assertEqual(len(authorize_urls), 1)
        self.assertIn("id_token_hint=stored_id_token", authorize_urls[0])


    def test_get_account_access_token_omits_id_token_hint_when_not_set(self) -> None:
        """_get_account_access_token() does not include id_token_hint when no id_token is stored."""

        device_handler = NLZIETHandler()
        device_handler._id_token = ""

        authorize_urls: list = []

        def _fake_open(url: str, data: Optional[dict] = None, **kwargs: Any) -> str:
            if data is None:
                authorize_urls.append(url)
            return '{"access_token": "mock_account_token"}'

        redirect_url = "https://mijn.nlziet.nl/callback-silent.html?code=fake_code&state=FIXED"
        with unittest.mock.patch("resources.lib.urihandler.UriHandler.open",
                                 side_effect=_fake_open), \
             unittest.mock.patch("resources.lib.urihandler.UriHandler.instance") as mock_inst, \
             unittest.mock.patch("resources.lib.authentication.oidchandler.secrets.token_urlsafe",
                                 return_value="FIXED"):
            mock_inst.return_value.status.url = redirect_url
            mock_inst.return_value.status.error = False
            device_handler._get_account_access_token()

        self.assertEqual(len(authorize_urls), 1)
        self.assertNotIn("id_token_hint", authorize_urls[0])


    def test_refresh_failure_clears_local_state(self) -> None:
        """Any token refresh failure clears local device auth state."""

        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN, MOCK_REFRESH_TOKEN
        from tests.authentication.nlziethandler_mocks import MOCK_SESSION_KEY

        device_handler = NLZIETHandler()
        device_handler._access_token = MOCK_ACCESS_TOKEN
        device_handler._refresh_token = MOCK_REFRESH_TOKEN
        device_handler._access_token_expires_at = 0
        device_handler._id_token = ""  # prevent silent re-auth from succeeding
        device_handler._save_device_session_key(MOCK_SESSION_KEY)

        with unittest.mock.patch.object(
            device_handler,
            "_request_token",
            return_value=False
        ):
            result = device_handler.refresh_access_token()

        self.assertIsNone(result)
        self.assertEqual(device_handler._refresh_token, "")
        device_handler._clear_device_session_key()


    def test_log_off_clears_tokens_but_preserves_session_key_on_deregister_failure(self) -> None:
        """_revoke_device_authorization() clears local tokens even when deregistration fails, but preserves the session key."""

        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN, MOCK_REFRESH_TOKEN
        from tests.authentication.nlziethandler_mocks import MOCK_SESSION_KEY

        device_handler = NLZIETHandler()
        device_handler._client_id = DEVICE_CLIENT_ID
        device_handler._access_token = MOCK_ACCESS_TOKEN
        device_handler._refresh_token = MOCK_REFRESH_TOKEN
        device_handler._id_token = "mock_id_token"
        device_handler._save_device_session_key(MOCK_SESSION_KEY)

        with unittest.mock.patch.object(device_handler, "_deregister_device", return_value=False) as mock_remove:
            result = device_handler._revoke_device_authorization("test@example.com")

        self.assertFalse(result)
        mock_remove.assert_called_once_with()
        self.assertEqual(device_handler._access_token, "")
        self.assertEqual(device_handler._refresh_token, "")
        self.assertEqual(device_handler._id_token, "")
        self.assertEqual(device_handler._load_device_session_key(), MOCK_SESSION_KEY)
        device_handler._clear_device_session_key()


    def test_set_profile_claim_returns_true_on_success(self) -> None:
        """set_profile_claim() returns True and stores the profile token."""

        from tests.authentication.nlziethandler_mocks import (
            MOCK_ACCESS_TOKEN,
            MOCK_PROFILE_ACCESS_TOKEN,
            MOCK_PROFILE_ID,
        )

        self.handler._access_token = MOCK_ACCESS_TOKEN

        result = self.handler.set_profile_claim(MOCK_PROFILE_ID)

        self.assertTrue(result)
        self.assertEqual(self.handler._access_token, MOCK_PROFILE_ACCESS_TOKEN)

    def test_set_profile_claim_registers_oidc_claims_on_success(self) -> None:
        """set_profile_claim() registers the profileId as an OIDC claims request on success."""

        import json
        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN, MOCK_PROFILE_ID

        self.handler._access_token = MOCK_ACCESS_TOKEN

        self.handler.set_profile_claim(MOCK_PROFILE_ID)

        param = self.handler._get_claims_param()
        self.assertIsNotNone(param)
        claims = json.loads(param)  # type: ignore[arg-type]
        self.assertIn("id_token", claims)
        self.assertEqual(claims["id_token"]["profileId"]["value"], MOCK_PROFILE_ID)
        self.assertTrue(claims["id_token"]["profileId"]["essential"])

    def test_set_profile_claim_does_not_register_claims_on_failure(self) -> None:
        """set_profile_claim() does not set OIDC claims when the token exchange fails."""

        import unittest.mock
        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN

        self.handler._access_token = MOCK_ACCESS_TOKEN

        with unittest.mock.patch.object(self.handler, "_request_token", return_value=None):
            self.handler.set_profile_claim("any-profile-id")

        self.assertIsNone(self.handler._get_claims_param())


    def test_set_profile_claim_returns_false_without_access_token(self) -> None:
        """set_profile_claim() returns False immediately when not logged in."""

        self.handler._access_token = ""

        result = self.handler.set_profile_claim("some-profile-id")

        self.assertFalse(result)


    def test_set_profile_claim_token_carries_profile_type(self) -> None:
        """After set_profile_claim(), the stored token contains the profile claim."""

        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN, MOCK_PROFILE_ID

        self.handler._access_token = MOCK_ACCESS_TOKEN

        self.handler.set_profile_claim(MOCK_PROFILE_ID)

        claims = self.handler.decode_token(self.handler._access_token) or {}
        self.assertEqual(claims.get("profileType"), "ChildYoung")


    def test_set_profile_claim_returns_false_on_network_error(self) -> None:
        """set_profile_claim() returns False when the token exchange fails."""

        import unittest.mock
        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN

        self.handler._access_token = MOCK_ACCESS_TOKEN

        with unittest.mock.patch.object(
            self.handler, "_token_exchange_request", return_value=None
        ):
            result = self.handler.set_profile_claim("any-profile-id")

        self.assertFalse(result)


    def test_set_profile_claim_returns_false_when_claim_not_in_token(self) -> None:
        """set_profile_claim() returns False when the returned token lacks the profile claim."""

        import unittest.mock
        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN

        self.handler._access_token = MOCK_ACCESS_TOKEN
        original_token = MOCK_ACCESS_TOKEN

        wrong_profile_id = "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF"
        with unittest.mock.patch.object(self.handler, "_token_exchange_request",
                                        return_value={}):
            result = self.handler.set_profile_claim(wrong_profile_id)

        self.assertFalse(result)
        self.assertEqual(self.handler._access_token, original_token)


    def test_set_profile_claim_returns_false_when_claim_is_not_a_string(self) -> None:
        """set_profile_claim() returns False when profileId is present but not a string."""

        import unittest.mock
        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN

        self.handler._access_token = MOCK_ACCESS_TOKEN
        original_token = MOCK_ACCESS_TOKEN

        with unittest.mock.patch.object(
            self.handler, "_token_exchange_request",
            return_value={"profileId": ["wrong-type"]}
        ):
            result = self.handler.set_profile_claim("FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF")

        self.assertFalse(result)
        self.assertEqual(self.handler._access_token, original_token)


    def test_set_profile_claim_rolls_back_on_claim_mismatch(self) -> None:
        """set_profile_claim() does not commit state when profileId is wrong."""

        import unittest.mock
        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN, MOCK_PROFILE_ID

        self.handler._access_token = MOCK_ACCESS_TOKEN
        self.handler._refresh_token = "old-refresh"

        # _token_exchange_request already saved the new token internally, but
        # set_profile_claim must not call _save_tokens again on mismatch.
        # We mock it to return claims for a different profile — state is unchanged.
        wrong_profile_id = "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF"
        with unittest.mock.patch.object(
            self.handler, "_token_exchange_request",
            return_value={"profileId": MOCK_PROFILE_ID}
        ):
            result = self.handler.set_profile_claim(wrong_profile_id)

        self.assertFalse(result)


    def test_set_profile_claim_uses_handler_client_and_minimal_scope(self) -> None:
        """set_profile_claim() must use the handler's own client_id and 'openid api' scope."""

        import unittest.mock
        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN, MOCK_PROFILE_ID

        self.handler._access_token = MOCK_ACCESS_TOKEN

        captured: list = []
        original = self.handler._token_exchange_request

        def capture_data(data: dict, headers: Optional[dict] = None) -> Optional[dict]:
            captured.append(data)
            return original(data, headers=headers)

        with unittest.mock.patch.object(self.handler, "_token_exchange_request",
                                        side_effect=capture_data):
            self.handler.set_profile_claim(MOCK_PROFILE_ID)

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["client_id"], self.handler._client_id,
                         "Profile grant must use the handler's own client_id (matches the Bearer token)")
        self.assertEqual(captured[0]["scope"], "openid api",
                         "Profile grant must use 'openid api' scope without offline_access")
        self.assertEqual(captured[0]["grant_type"], "profile")
        self.assertEqual(captured[0]["profile"], MOCK_PROFILE_ID)


    def test_set_profile_claim_sends_bearer_token(self) -> None:
        """set_profile_claim() must include a Bearer Authorization header."""

        import unittest.mock
        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN, MOCK_PROFILE_ID

        self.handler._access_token = MOCK_ACCESS_TOKEN
        captured_headers: list = []

        def capture_headers(data: dict, headers: Optional[dict] = None,
                            **kwargs: object) -> Optional[dict]:
            captured_headers.append(headers or {})
            return None

        with unittest.mock.patch.object(self.handler, "_token_exchange_request",
                                        side_effect=capture_headers):
            self.handler.set_profile_claim(MOCK_PROFILE_ID)

        self.assertEqual(len(captured_headers), 1)
        auth = captured_headers[0].get("Authorization", "")
        self.assertTrue(auth.startswith("Bearer "),
                        "Profile grant must include Authorization: Bearer <token>")


def _make_nlziet_handler(device_flow: bool = True) -> NLZIETHandler:
    """Return a freshly constructed NLZIETHandler with no real settings I/O."""

    with unittest.mock.patch("resources.lib.addonsettings.AddonSettings.get_setting",
                             return_value=None):
        handler = NLZIETHandler()
        if not device_flow:
            handler._client_id = WEB_CLIENT_ID
    return handler


class TestNlzietHeaders(unittest.TestCase):
    """Unit tests for NLZIET request-header construction."""

    def test_device_flow_property_true_for_device_client(self) -> None:
        """_device_flow is True when the Android TV (device) client is active."""

        handler = _make_nlziet_handler(device_flow=True)
        self.assertTrue(handler._device_flow)


    def test_device_flow_property_false_for_web_client(self) -> None:
        """_device_flow is False when the web client is active."""

        handler = _make_nlziet_handler(device_flow=False)
        self.assertFalse(handler._device_flow)


    def test_device_flow_property_mirrors_internal_device_flow(self) -> None:
        """Public device_flow property delegates to _device_flow."""

        for flag in (True, False):
            with self.subTest(device_flow=flag):
                handler = _make_nlziet_handler(device_flow=flag)
                self.assertEqual(handler._device_flow, handler.device_flow)


    def test_authentication_headers_omits_user_agent_for_web_flow(self) -> None:
        """Web flow omits User-Agent so UriHandler injects its modern default."""

        handler = _make_nlziet_handler(device_flow=False)
        headers = handler.authentication_headers

        self.assertNotIn("User-Agent", headers)


    def test_authentication_headers_sends_okhttp_user_agent_for_device_flow(self) -> None:
        """Device flow sends the OkHttp User-Agent matching the Android TV app."""

        handler = _make_nlziet_handler(device_flow=True)
        headers = handler.authentication_headers

        self.assertEqual(DEVICE_FLOW_USER_AGENT, headers.get("User-Agent"))


    def test_authentication_headers_uses_stored_access_token(self) -> None:
        """authentication_headers uses the internally stored access token."""

        handler = _make_nlziet_handler()
        handler._access_token = "stored-tok"
        headers = handler.authentication_headers

        self.assertEqual(headers.get("Authorization"), "Bearer stored-tok")


    def test_authentication_headers_omits_bearer_when_no_stored_token(self) -> None:
        """authentication_headers omits Authorization when no token is stored."""

        handler = _make_nlziet_handler()
        handler._access_token = ""
        headers = handler.authentication_headers

        self.assertNotIn("Authorization", headers)


    def test_token_profile_id_returns_claim_from_stored_token(self) -> None:
        """token_profile_id returns the profileId JWT claim."""

        from tests.authentication.nlziethandler_mocks import (
            MOCK_PROFILE_ACCESS_TOKEN, MOCK_PROFILE_ID)
        handler = _make_nlziet_handler()
        handler._access_token = MOCK_PROFILE_ACCESS_TOKEN

        self.assertEqual(handler.token_profile_id, MOCK_PROFILE_ID)


    def test_token_profile_id_returns_none_without_stored_token(self) -> None:
        """token_profile_id returns None when no token is stored."""

        handler = _make_nlziet_handler()
        handler._access_token = ""

        self.assertIsNone(handler.token_profile_id)


    def test_token_profile_id_returns_none_for_unscoped_token(self) -> None:
        """token_profile_id returns None when the token carries no profileId claim."""

        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN
        handler = _make_nlziet_handler()
        handler._access_token = MOCK_ACCESS_TOKEN

        self.assertIsNone(handler.token_profile_id)


    def test_token_profile_id_returns_none_for_non_string_claim(self) -> None:
        """token_profile_id returns None when the profileId claim is not a string."""

        payload = base64.urlsafe_b64encode(
            json.dumps({"profileId": ["bad"], "exp": 9999999999}).encode()
        ).decode().rstrip("=")
        handler = _make_nlziet_handler()
        handler._access_token = f"header.{payload}.sig"

        self.assertIsNone(handler.token_profile_id)


    def test_token_profile_type_returns_claim_from_stored_token(self) -> None:
        """token_profile_type returns the profileType JWT claim."""

        from tests.authentication.nlziethandler_mocks import MOCK_PROFILE_ACCESS_TOKEN
        handler = _make_nlziet_handler()
        handler._access_token = MOCK_PROFILE_ACCESS_TOKEN

        self.assertEqual(handler.token_profile_type, "ChildYoung")


    def test_token_profile_type_returns_empty_without_stored_token(self) -> None:
        """token_profile_type returns '' when no token is stored."""

        handler = _make_nlziet_handler()
        handler._access_token = ""

        self.assertEqual(handler.token_profile_type, "")


    def test_token_profile_type_returns_empty_for_unscoped_token(self) -> None:
        """token_profile_type returns '' when the token carries no profileType claim."""

        import base64
        payload = base64.urlsafe_b64encode(
            json.dumps({"sub": "u1", "exp": 9999999999}).encode()
        ).decode().rstrip("=")
        handler = _make_nlziet_handler()
        handler._access_token = f"header.{payload}.sig"

        self.assertEqual(handler.token_profile_type, "")


    def test_token_profile_type_returns_empty_for_non_string_claim(self) -> None:
        """token_profile_type returns '' when the profileType claim is not a string."""

        payload = base64.urlsafe_b64encode(
            json.dumps({"profileType": {"value": "ChildYoung"}, "exp": 9999999999}).encode()
        ).decode().rstrip("=")
        handler = _make_nlziet_handler()
        handler._access_token = f"header.{payload}.sig"

        self.assertEqual(handler.token_profile_type, "")


class TestNlzietPollDeviceFlow(unittest.TestCase):
    """Unit tests for _device_access_token_request() — NLZIET-specific polling behaviour."""

    def setUp(self) -> None:
        self.handler = _make_nlziet_handler(device_flow=True)
        self._patch_diff = unittest.mock.patch.object(
            self.handler, "_get_device_sessions_and_keys")
        self._patch_diff.start()

    def tearDown(self) -> None:
        self._patch_diff.stop()

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.urihandler.UriHandler.open")
    def test_device_access_token_request_sends_okhttp_user_agent(
            self,
            mock_open: unittest.mock.MagicMock,
            _mock_set: unittest.mock.MagicMock) -> None:
        """Device flow sends the OkHttp User-Agent for the token endpoint."""

        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN
        mock_open.return_value = f'{{"access_token": "{MOCK_ACCESS_TOKEN}", "expires_in": 3600}}'

        self.handler._device_access_token_request("device_code_123")

        _, kwargs = mock_open.call_args
        self.assertEqual(DEVICE_FLOW_USER_AGENT,
                         kwargs.get("additional_headers", {}).get("User-Agent"))


    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.urihandler.UriHandler.open")
    def test_device_access_token_request_passes_through_channel_app_headers(
            self,
            mock_open: unittest.mock.MagicMock,
            _mock_set: unittest.mock.MagicMock) -> None:
        """_device_access_token_request() includes Nlziet-* app headers for the device client."""

        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN
        mock_open.return_value = f'{{"access_token": "{MOCK_ACCESS_TOKEN}", "expires_in": 3600}}'

        # handler receives app headers from caller
        handler = _make_nlziet_handler(device_flow=True)
        # Manually set app headers to test they're passed through
        handler._headers = {
            "Nlziet-AppName": "AndroidTv",
            "Nlziet-AppVersion": "5.65.5"
        }
        handler._device_access_token_request("device_code_123")

        _, kwargs = mock_open.call_args
        headers = kwargs.get("additional_headers", {})
        self.assertEqual(headers.get("Nlziet-AppName"), "AndroidTv")
        self.assertEqual(headers.get("Nlziet-AppVersion"), "5.65.5")

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.urihandler.UriHandler.open")
    def test_device_access_token_request_sets_device_client_id_on_success(
            self,
            mock_open: unittest.mock.MagicMock,
            mock_set: unittest.mock.MagicMock) -> None:
        """_device_access_token_request() stores AUTH_CLIENT_ID_KEY=DEVICE_CLIENT_ID on success."""

        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN
        mock_open.return_value = f'{{"access_token": "{MOCK_ACCESS_TOKEN}", "expires_in": 3600}}'

        self.handler._device_access_token_request("device_code_123")

        set_calls = [(a[0], a[1]) for a, _ in mock_set.call_args_list]
        self.assertIn((AUTH_CLIENT_ID_KEY, DEVICE_CLIENT_ID), set_calls)

    @unittest.mock.patch("resources.lib.urihandler.UriHandler.open")
    def test_device_access_token_request_returns_authorization_pending(
            self, mock_open: unittest.mock.MagicMock) -> None:
        """_device_access_token_request() returns 'authorization_pending' while user hasn't acted."""

        mock_open.return_value = '{"error": "authorization_pending"}'

        result = self.handler._device_access_token_request("device_code_123")

        self.assertEqual(result, "authorization_pending")

    @unittest.mock.patch("resources.lib.urihandler.UriHandler.open")
    def test_device_access_token_request_returns_error_on_network_failure(
            self, mock_open: unittest.mock.MagicMock) -> None:
        """_device_access_token_request() returns 'error' when UriHandler reports a network failure."""

        from resources.lib.urihandler import UriStatus
        mock_open.return_value = ""
        with unittest.mock.patch("resources.lib.urihandler.UriHandler.instance") as mock_instance:
            mock_instance.return_value.status = UriStatus(
                code=0, url="", error=True, reason="timeout")
            result = self.handler._device_access_token_request("device_code_123")

        self.assertEqual(result, "error")


class TestNlzietSaveAndClearTokens(unittest.TestCase):
    """Unit tests for _save_tokens() and _clear_tokens() overrides."""

    def setUp(self) -> None:
        self.handler = _make_nlziet_handler()

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_clear_tokens_wipes_tokens_not_session_key(self, mock_set: unittest.mock.MagicMock) -> None:
        """_clear_tokens() clears id_token and access/refresh tokens but never the device session key."""

        self.handler._id_token = "some_id_token"
        self.handler._access_token = "some_access_token"

        self.handler._clear_tokens()

        self.assertEqual(self.handler._id_token, "")
        self.assertEqual(self.handler._access_token, "")
        saved_keys = [call.args[0] for call in mock_set.call_args_list]
        self.assertNotIn("nlziet_device_session_key", saved_keys)


    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time")
    def test_save_tokens_stores_access_token_in_authentication_headers(
            self,
            mock_time: unittest.mock.MagicMock,
            _mock_set: unittest.mock.MagicMock) -> None:
        """_save_tokens() stores the access token; authentication_headers reflects it as Bearer."""

        mock_time.time.return_value = 0

        self.handler._save_tokens({"access_token": "tok123", "expires_in": 3600})

        self.assertEqual(self.handler._access_token, "tok123")
        self.assertEqual(self.handler.authentication_headers.get("Authorization"), "Bearer tok123")


    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_clear_tokens_does_not_leave_authorization_in_headers(
            self, _mock_set: unittest.mock.MagicMock) -> None:
        """_clear_tokens() leaves no Authorization in http_headers (none was there to begin with)."""

        http_headers: dict = {}
        self.handler._access_token = "old_token"

        self.handler._clear_tokens()

        self.assertEqual(self.handler._access_token, "")
        self.assertNotIn("Authorization", self.handler.authentication_headers)


    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time")
    def test_init_with_cached_token_does_not_mutate_http_headers_dict(
            self,
            mock_time: unittest.mock.MagicMock,
            _mock_set: unittest.mock.MagicMock) -> None:
        """Handler init loads the access token without affecting the caller's header state."""

        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN

        mock_time.time.return_value = 0

        with unittest.mock.patch(
            "resources.lib.addonsettings.AddonSettings.get_setting",
            side_effect=lambda key, **kw: MOCK_ACCESS_TOKEN if key.endswith("access_token") else None,
        ):
            handler = NLZIETHandler()

        self.assertEqual(handler._access_token, MOCK_ACCESS_TOKEN)
        self.assertIn("Authorization", handler.authentication_headers)


    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.urihandler.UriHandler.open")
    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time")
    def test_set_profile_claim_updates_access_token(
            self,
            mock_time: unittest.mock.MagicMock,
            mock_open: unittest.mock.MagicMock,
            _mock_set: unittest.mock.MagicMock) -> None:
        """Authorization header reflects the profile token via authentication_headers after set_profile_claim()."""

        from tests.authentication.nlziethandler_mocks import (
            MOCK_ACCESS_TOKEN,
            MOCK_PROFILE_ACCESS_TOKEN,
            MOCK_PROFILE_ID,
            MOCK_PROFILE_TOKEN_RESPONSE,
        )
        import json

        mock_time.time.return_value = 0
        mock_open.return_value = json.dumps(MOCK_PROFILE_TOKEN_RESPONSE)

        self.handler._access_token = MOCK_ACCESS_TOKEN

        result = self.handler.set_profile_claim(MOCK_PROFILE_ID)

        self.assertTrue(result)
        self.assertEqual(self.handler._access_token, MOCK_PROFILE_ACCESS_TOKEN)
        self.assertEqual(
            self.handler.authentication_headers.get("Authorization"),
            f"Bearer {MOCK_PROFILE_ACCESS_TOKEN}")

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time")
    def test_save_tokens_with_access_token_name_stores_access_token(
            self,
            mock_time: unittest.mock.MagicMock,
            _mock_set: unittest.mock.MagicMock) -> None:
        """_save_tokens(..., token_name='access_token') stores the token in _access_token."""

        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN

        mock_time.time.return_value = 0

        self.handler._save_tokens(
            {"access_token": MOCK_ACCESS_TOKEN, "expires_in": 3600},
            token_name="access_token",
        )

        self.assertEqual(self.handler._access_token, MOCK_ACCESS_TOKEN)
        self.assertIn("Authorization", self.handler.authentication_headers)

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time")
    def test_save_tokens_with_refresh_token_name_does_not_update_access_token(
            self,
            mock_time: unittest.mock.MagicMock,
            _mock_set: unittest.mock.MagicMock) -> None:
        """_save_tokens(..., token_name='refresh_token') does not change _access_token."""

        mock_time.time.return_value = 0
        self.handler._access_token = "old_access"

        self.handler._save_tokens(
            {"access_token": "old_access", "refresh_token": "new_ref", "expires_in": 3600},
            token_name="refresh_token",
        )

        self.assertEqual(self.handler._access_token, "old_access")


    def test_request_token_returns_false_on_error_status(self) -> None:
        """_request_token() returns False when UriHandler reports an HTTP error status."""

        from resources.lib.urihandler import UriStatus
        error_status = UriStatus(code=400, url=self.handler.token_endpoint,
                                 error=True, reason="Bad Request")
        with unittest.mock.patch("resources.lib.urihandler.UriHandler.open",
                                 return_value='{"error":"invalid_grant"}'), \
             unittest.mock.patch("resources.lib.urihandler.UriHandler.instance") as mock_instance:
            mock_instance.return_value.status = error_status
            result = self.handler._request_token({"grant_type": "refresh_token",
                                                  "refresh_token": "stale"})
        self.assertFalse(result)

    def test_request_token_returns_false_on_error_body(self) -> None:
        """_request_token() returns False when the server returns an OAuth2 error body."""

        from resources.lib.urihandler import UriStatus
        ok_status = UriStatus(code=200, url=self.handler.token_endpoint,
                              error=False, reason="OK")
        error_body = '{"error":"invalid_grant","error_description":"Token has been revoked"}'
        with unittest.mock.patch("resources.lib.urihandler.UriHandler.open",
                                 return_value=error_body), \
             unittest.mock.patch("resources.lib.urihandler.UriHandler.instance") as mock_instance:
            mock_instance.return_value.status = ok_status
            result = self.handler._request_token({"grant_type": "refresh_token",
                                                  "refresh_token": "stale"})
        self.assertFalse(result)

    def test_request_token_returns_false_on_invalid_jwt(self) -> None:
        """_request_token() returns False when access_token in response is not a valid JWT."""

        from resources.lib.urihandler import UriStatus
        ok_status = UriStatus(code=200, url=self.handler.token_endpoint,
                              error=False, reason="OK")
        bad_body = '{"access_token":"not-a-jwt","token_type":"Bearer","expires_in":3600}'
        with unittest.mock.patch("resources.lib.urihandler.UriHandler.open",
                                 return_value=bad_body), \
             unittest.mock.patch("resources.lib.urihandler.UriHandler.instance") as mock_instance:
            mock_instance.return_value.status = ok_status
            result = self.handler._request_token({"grant_type": "refresh_token",
                                                  "refresh_token": "stale"})
        self.assertFalse(result)


class TestNlzietLoadTokens(unittest.TestCase):
    """Unit tests for selective _load_tokens() in NLZIET."""

    def setUp(self) -> None:
        self.handler = _make_nlziet_handler()

    def _settings_map(self, **extra: str) -> Callable[..., str]:
        """Return a side_effect function that maps setting keys to values."""

        def _get(key: str, **_kw: object) -> str:
            for name, value in extra.items():
                if key == self.handler._token_key(name):
                    return value
            return ""

        return _get

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.get_setting")
    def test_load_access_token_sets_access_token_field(
            self, mock_get: unittest.mock.MagicMock) -> None:
        """_load_tokens('access_token') stores the token in _access_token."""

        mock_get.side_effect = self._settings_map(access_token="tok_new", access_token_expires_at="5000")

        self.handler._load_tokens("access_token")

        self.assertEqual(self.handler._access_token, "tok_new")

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.get_setting")
    def test_load_id_token_only_does_not_change_access_token(
            self, mock_get: unittest.mock.MagicMock) -> None:
        """_load_tokens('id_token') does not touch access_token."""

        self.handler._access_token = "existing_acc"
        mock_get.side_effect = self._settings_map(id_token="new_id")

        self.handler._load_tokens("id_token")

        self.assertEqual(self.handler._id_token, "new_id")
        self.assertEqual(self.handler._access_token, "existing_acc")


class TestNlzietLogOff(unittest.TestCase):
    """Unit tests for _revoke_device_authorization() — deregistration and token cleanup."""

    def setUp(self) -> None:
        self.handler = _make_nlziet_handler(device_flow=True)

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_log_off_returns_true_when_deregistration_succeeds(
            self, _mock_set: unittest.mock.MagicMock) -> None:
        """_revoke_device_authorization() returns True and clears tokens when device removal succeeds."""

        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN, MOCK_SESSION_KEY
        self.handler._access_token = MOCK_ACCESS_TOKEN
        self.handler._id_token = "id_tok"
        self.handler._save_device_session_key(MOCK_SESSION_KEY)

        with unittest.mock.patch.object(self.handler, "_deregister_device", return_value=True):
            result = self.handler._revoke_device_authorization("test@example.com")

        self.assertTrue(result)
        self.assertEqual(self.handler._access_token, "")
        self.assertEqual(self.handler._id_token, "")


    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_log_off_returns_false_when_deregistration_fails(
            self, _mock_set: unittest.mock.MagicMock) -> None:
        """_revoke_device_authorization() returns False and still clears tokens when device removal fails."""

        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN
        self.handler._access_token = MOCK_ACCESS_TOKEN
        self.handler._id_token = "id_tok"

        with unittest.mock.patch.object(self.handler, "_deregister_device", return_value=False):
            result = self.handler._revoke_device_authorization("test@example.com")

        self.assertFalse(result)
        self.assertEqual(self.handler._access_token, "")
        self.assertEqual(self.handler._id_token, "")



class TestNlzietCredentialLogOff(unittest.TestCase):
    """Unit tests for _credential_log_off() — OIDC revocation and end-session."""

    def setUp(self) -> None:
        self.handler = _make_nlziet_handler(device_flow=False)

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_credential_log_off_success_clears_tokens(
            self, _mock_set: unittest.mock.MagicMock) -> None:
        """_credential_log_off() clears tokens on success."""

        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN, MOCK_ID_TOKEN
        self.handler._access_token = MOCK_ACCESS_TOKEN
        self.handler._id_token = MOCK_ID_TOKEN

        with unittest.mock.patch.object(
                self.handler, "_end_session", return_value=True) as mock_oidc:
            result = self.handler._credential_log_off("test@example.com")

        self.assertTrue(result)
        mock_oidc.assert_called_once()
        self.assertEqual(self.handler._access_token, "")
        self.assertEqual(self.handler._id_token, "")

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_credential_log_off_propagates_oidc_failure(
            self, _mock_set: unittest.mock.MagicMock) -> None:
        """_credential_log_off() returns False when _end_session reports failure."""

        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN
        self.handler._access_token = MOCK_ACCESS_TOKEN

        with unittest.mock.patch.object(
                self.handler, "_end_session", return_value=False):
            result = self.handler._credential_log_off("test@example.com")

        self.assertFalse(result)
        self.assertEqual(self.handler._access_token, "", "Tokens must be cleared even on failure")

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_credential_log_off_passes_correct_endpoints(
            self, _mock_set: unittest.mock.MagicMock) -> None:
        """_credential_log_off() passes WEB_CLIENT_ID and the NLZIET endpoints to _end_session."""

        from resources.lib.authentication.nlziethandler import (
            WEB_CLIENT_ID, API_ID_REVOCATION, API_ID_END_SESSION, REDIRECT_URI)

        with unittest.mock.patch.object(
                self.handler, "_end_session", return_value=True) as mock_oidc:
            self.handler._credential_log_off("test@example.com")

        mock_oidc.assert_called_once_with(
            WEB_CLIENT_ID, API_ID_REVOCATION, API_ID_END_SESSION, REDIRECT_URI)


class TestNlzietStartDeviceFlow(unittest.TestCase):
    """Unit tests for the _start_device_authorization() NLZIET override."""

    def setUp(self) -> None:
        self.handler = _make_nlziet_handler(device_flow=True)
        self._patch_token = unittest.mock.patch.object(
            self.handler, "_get_account_access_token", return_value=None)
        self._patch_uri_handler = unittest.mock.patch("resources.lib.urihandler.UriHandler.instance")
        self._patch_token.start()
        mock_uri_handler = self._patch_uri_handler.start()
        mock_uri_handler.return_value = unittest.mock.MagicMock(
            status=unittest.mock.MagicMock(error=False, code=200, reason="OK")
        )

    def tearDown(self) -> None:
        self._patch_token.stop()
        self._patch_uri_handler.stop()

    def test_supports_device_authorization(self) -> None:
        """NLZIETHandler overrides the device authorization interface."""

        self.assertTrue(self.handler.supports_device_authorization)

    @unittest.mock.patch("resources.lib.urihandler.UriHandler.open")
    def test_start_device_flow_sends_okhttp_user_agent(
            self, mock_open: unittest.mock.MagicMock) -> None:
        """_device_authorization_request() sends the OkHttp User-Agent for device flow."""

        mock_open.return_value = (
            '{"device_code": "d", "user_code": "U", "verification_uri": '
            '"https://nlziet.nl/koppel", "expires_in": 900, "interval": 5}'
        )

        self.handler._start_device_authorization(device_name="Test TV")

        _, kwargs = mock_open.call_args
        self.assertEqual(DEVICE_FLOW_USER_AGENT,
                         kwargs.get("additional_headers", {}).get("User-Agent"))

    @unittest.mock.patch("resources.lib.urihandler.UriHandler.open")
    def test_start_device_flow_returns_none_when_device_code_missing(
            self, mock_open: unittest.mock.MagicMock) -> None:
        """_device_authorization_request() returns None when the response lacks device_code."""

        mock_open.return_value = '{"error": "invalid_request"}'

        result = self.handler._start_device_authorization(device_name="Test TV")

        self.assertIsNone(result)

    @unittest.mock.patch("resources.lib.urihandler.UriHandler.open")
    def test_start_device_flow_returns_full_response_on_success(
            self, mock_open: unittest.mock.MagicMock) -> None:
        """_device_authorization_request() returns the full response dict on success."""

        mock_open.return_value = (
            '{"device_code": "dev123", "user_code": "AB12", "verification_uri": '
            '"https://nlziet.nl/koppel", "expires_in": 900, "interval": 5}'
        )

        result = self.handler._start_device_authorization(device_name="Test TV")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.device_code, "dev123")
        self.assertEqual(result.user_code, "AB12")


class TestNlzietSetProfileClaimCaseInsensitive(unittest.TestCase):
    """Unit tests for set_profile_claim() profileId case-insensitive matching."""

    def setUp(self) -> None:
        self.handler = _make_nlziet_handler()

    def test_set_profile_claim_accepts_lowercase_profile_id_in_token(self) -> None:
        """set_profile_claim() accepts a match even when token has lowercase profileId."""

        import base64
        import json as _json
        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN, MOCK_PROFILE_ID

        header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
        payload = {"profileId": MOCK_PROFILE_ID.lower(), "exp": 9999999999}
        enc = base64.urlsafe_b64encode(_json.dumps(payload).encode()).rstrip(b"=").decode()
        lowercase_token = f"{header}.{enc}."

        self.handler._access_token = MOCK_ACCESS_TOKEN

        with unittest.mock.patch.object(
            self.handler, "_fetch_tokens",
            return_value={"access_token": lowercase_token}
        ), unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            result = self.handler.set_profile_claim(MOCK_PROFILE_ID)

        self.assertTrue(result)


# ============================================================================
# Unit tests for _web_form_login()
# ============================================================================


class TestNlzietWebFormLogin(unittest.TestCase):
    """Unit tests for the NLZIET web login flow internals."""

    def setUp(self) -> None:
        self.handler = _make_nlziet_handler(device_flow=False)


    @unittest.mock.patch("resources.lib.urihandler.UriHandler.open")
    def test_web_form_login_returns_network_error_when_return_url_missing(
            self, mock_open: unittest.mock.MagicMock) -> None:
        """_web_form_login() returns network_error when the login redirect lacks ReturnUrl."""

        from tests.authentication.nlziethandler_mocks import MOCK_LOGIN_PAGE_HTML

        mock_open.return_value = MOCK_LOGIN_PAGE_HTML.format(return_url="")
        with unittest.mock.patch("resources.lib.urihandler.UriHandler.instance") as mock_instance:
            mock_instance.return_value.status.url = "https://id.nlziet.nl/account/login?foo=bar"

            result = self.handler._web_form_login("user@example.com", "pass")

        self.assertEqual(result, "network_error")
        mock_open.assert_called_once()


# ============================================================================
# Unit tests for _credential_login() error types
# ============================================================================


class TestNlzietCredentialLoginErrorTypes(unittest.TestCase):
    """Unit tests verifying _credential_login() surfaces the correct error strings."""

    def setUp(self) -> None:
        self.handler = _make_nlziet_handler()


    def test_credential_login_empty_credentials_delegates_to_web_form_login(self) -> None:
        """_credential_login() delegates empty credentials to the web form flow without a local guard."""

        with unittest.mock.patch.object(
            self.handler, "_web_form_login", return_value="invalid_credentials"
        ) as mock_login:
            result = self.handler._credential_log_on("", "")

        mock_login.assert_called_once_with("", "")
        self.assertFalse(result.logged_on)
        self.assertEqual(result.error, "invalid_credentials")


    def test_credential_login_invalid_credentials_returns_invalid_credentials(self) -> None:
        """_credential_login() surfaces 'invalid_credentials' when _web_form_login fails."""

        with unittest.mock.patch.object(
            self.handler, "_web_form_login", return_value="invalid_credentials"
        ):
            result = self.handler._credential_log_on("user@example.com", "wrongpass")

        self.assertFalse(result.logged_on)
        self.assertEqual(result.error, "invalid_credentials")


    def test_credential_login_network_error_returns_network_error(self) -> None:
        """_credential_login() surfaces 'network_error' when _web_form_login fails."""

        with unittest.mock.patch.object(
            self.handler, "_web_form_login", return_value="network_error"
        ):
            result = self.handler._credential_log_on("user@example.com", "pass")

        self.assertFalse(result.logged_on)
        self.assertEqual(result.error, "network_error")


    def test_credential_login_token_failure_returns_network_error(self) -> None:
        """_credential_login() returns 'network_error' when token fetch fails after headless login."""

        with unittest.mock.patch.object(self.handler, "_web_form_login", return_value=None), \
             unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting"), \
             unittest.mock.patch.object(self.handler, "_get_access_token", return_value=None):
            result = self.handler._credential_log_on("user@example.com", "pass")

        self.assertFalse(result.logged_on)
        self.assertEqual(result.error, "network_error")


# ============================================================================
# Unit tests for active_authentication()
# ============================================================================


class TestNLZIETActiveAuthentication(unittest.TestCase):
    """Tests for NLZIETHandler.active_authentication()."""

    def setUp(self) -> None:
        self.handler = _make_nlziet_handler()

    def test_no_token_returns_empty_result(self) -> None:
        """active_authentication() returns AuthenticationResult('') when not logged in."""

        self.handler._access_token = ""
        result = self.handler.active_authentication()
        self.assertFalse(result.existing_login)
        self.assertEqual(result.username, "")

    def test_with_token_returns_existing_login(self) -> None:
        """active_authentication() returns existing_login=True with username from a valid JWT."""

        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN
        self.handler._access_token = MOCK_ACCESS_TOKEN
        self.handler._access_token_expires_at = int(time.time()) + 3600
        result = self.handler.active_authentication()
        self.assertTrue(result.existing_login)
        self.assertIsNotNone(result.username)


# ============================================================================
# Live Regression: Login Page Structure
# ============================================================================


@unittest.skipIf(not os.getenv('NLZIET_USERNAME'),
                 "Set NLZIET_USERNAME to enable live login-page structure tests")
class TestNlzietLoginPageStructure(unittest.TestCase):
    """
    Regression tests that verify the NLZIET login page still provides
    the expected ReturnUrl and CSRF token.

    These hit the live identity server (no credentials posted) to detect
    upstream changes that would break the web form login flow.
    """

    handler: NLZIETHandler

    @classmethod
    def setUpClass(cls) -> None:
        Logger.create_logger(None, str(cls), min_log_level=0)
        UriHandler.create_uri_handler(ignore_ssl_errors=False)

        from resources.lib.retroconfig import Config
        if not os.path.exists(Config.profileDir):
            os.makedirs(Config.profileDir, exist_ok=True)

        cls.handler = NLZIETHandler()


    @classmethod
    def tearDownClass(cls) -> None:
        if Logger.instance():
            Logger.instance().close_log()


    def setUp(self) -> None:
        UriHandler.delete_cookie(domain=".nlziet.nl")


    def _fetch_login_page(self) -> tuple:
        """
        Navigate to the authorize endpoint and return (response_url, html_body).

        No credentials are submitted — the server redirects to the login form.
        """

        from urllib.parse import urlencode
        state = secrets.token_urlsafe(16)
        verifier, challenge = self.handler._generate_pkce()
        params = {
            "client_id": WEB_CLIENT_ID,
            "redirect_uri": self.handler.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.handler.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "response_mode": "query"
        }
        auth_url = f"{API_ID_AUTHORIZE}?{urlencode(params)}"
        html = UriHandler.open(auth_url, no_cache=True,
                               additional_headers=self.handler.authentication_headers)
        response_url = UriHandler.last_status().url
        return response_url, html


    def test_return_url_present_in_redirect_params(self) -> None:
        """The authorize → login redirect must include ReturnUrl as a query param."""

        from urllib.parse import parse_qs, urlparse
        response_url, _ = self._fetch_login_page()
        redirect_params = parse_qs(urlparse(response_url).query)

        self.assertIn("ReturnUrl", redirect_params,
                       f"ReturnUrl missing from redirect: {response_url}")
        return_url = redirect_params["ReturnUrl"][0]
        self.assertIn("/connect/authorize/callback", return_url,
                       f"ReturnUrl does not point to authorize callback: {return_url}")


    def test_csrf_token_present_in_login_page(self) -> None:
        """The login page must still embed a __RequestVerificationToken."""

        _, html = self._fetch_login_page()
        csrf = self.handler._extract_csrf_token(html)
        self.assertIsNotNone(csrf, "CSRF token not found in login page HTML")
        assert csrf is not None
        self.assertGreater(len(csrf), 20, "CSRF token suspiciously short")


class TestNlzietValidateToken(unittest.TestCase):
    """Unit tests for NLZIETHandler.verify_token()."""

    def setUp(self) -> None:
        self.handler = _make_nlziet_handler()

    def test_returns_valid_when_user_info_succeeds(self) -> None:
        """verify_token() returns 'valid' when get_user_info() returns claims."""

        with unittest.mock.patch.object(self.handler, "get_user_info",
                                        return_value={"sub": "u1", "email": "x@x.com"}):
            self.assertEqual(self.handler.verify_token(), "valid")

    def test_returns_expired_on_permission_error(self) -> None:
        """verify_token() returns 'expired' when get_user_info() raises PermissionError (401/403)."""

        with unittest.mock.patch.object(self.handler, "get_user_info",
                                        side_effect=PermissionError("HTTP 401")):
            self.assertEqual(self.handler.verify_token(), "expired")

    def test_returns_network_error_on_runtime_error(self) -> None:
        """verify_token() returns 'network_error' when get_user_info() raises RuntimeError (5xx)."""

        with unittest.mock.patch.object(self.handler, "get_user_info",
                                        side_effect=RuntimeError("HTTP 503")):
            self.assertEqual(self.handler.verify_token(), "network_error")

    def test_returns_network_error_on_ioerror(self) -> None:
        """verify_token() returns 'network_error' when get_user_info() raises IOError (transport failure)."""

        with unittest.mock.patch.object(self.handler, "get_user_info",
                                        side_effect=IOError("connection refused")):
            self.assertEqual(self.handler.verify_token(), "network_error")

    def test_permission_error_caught_before_ioerror(self) -> None:
        """verify_token() classifies PermissionError as 'expired', not 'network_error'.

        PermissionError is a subclass of IOError; catching order matters.
        """

        with unittest.mock.patch.object(self.handler, "get_user_info",
                                        side_effect=PermissionError("HTTP 403")):
            result = self.handler.verify_token()

        self.assertEqual(result, "expired")


class TestNlzietDoTokenRefresh(unittest.TestCase):
    """Tests for NLZIETHandler._refresh_token_grant() — refresh and silent re-auth fallback."""

    def setUp(self) -> None:
        self.handler = _make_nlziet_handler()

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_falls_back_to_silent_reauth_when_no_refresh_token(
            self, _mock_set: unittest.mock.MagicMock) -> None:
        """_refresh_token_grant() calls _silent_authentication() when no refresh_token is available."""

        self.handler._refresh_token = ""
        calls: list = []
        self.handler._silent_authentication = lambda: calls.append(True) or True  # type: ignore[method-assign, func-returns-value]

        self.assertTrue(self.handler._refresh_token_grant())

        self.assertEqual(calls, [True])

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_falls_back_to_silent_reauth_when_refresh_grant_fails(
            self, _mock_set: unittest.mock.MagicMock) -> None:
        """_refresh_token_grant() clears access/refresh tokens but preserves id_token for silent re-auth fallback."""

        self.handler._refresh_token = "stale-refresh-token"
        self.handler._access_token = "acc"
        self.handler._id_token = "valid-id-token"
        calls: list = []
        self.handler._silent_authentication = lambda: calls.append(True) or True  # type: ignore[method-assign, func-returns-value]

        with unittest.mock.patch.object(
                self.handler, "_request_token", return_value=False):
            result = self.handler._refresh_token_grant()

        self.assertTrue(result)
        self.assertEqual(calls, [True])
        self.assertEqual(self.handler._access_token, "")
        self.assertEqual(self.handler._refresh_token, "")

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_clears_tokens_and_returns_false_when_both_grant_and_silent_fail(
            self, _mock_set: unittest.mock.MagicMock) -> None:
        """_refresh_token_grant() returns False when both refresh grant and silent re-auth fail."""

        self.handler._refresh_token = "stale-refresh-token"
        self.handler._access_token = "acc"
        self.handler._silent_authentication = lambda: False  # type: ignore[method-assign]

        with unittest.mock.patch.object(
                self.handler, "_request_token", return_value=False):
            result = self.handler._refresh_token_grant()

        self.assertFalse(result)
        self.assertEqual(self.handler._access_token, "")

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_access_token_preserved_when_silent_reauth_fails_after_refresh_grant(
            self, _mock_set: unittest.mock.MagicMock) -> None:
        """
        Regression: _silent_authentication() failure must not wipe the access
        token that was just obtained via a successful refresh_token grant.

        Previously, _silent_authentication() called _clear_tokens() on failure,
        destroying the freshly-refreshed access token.  This caused
        refresh_access_token() to return None hours after login (once the
        id_token hint became too stale for the server to accept).
        """

        # Use a headless-login handler — device flow skips the id_token block.
        handler = _make_nlziet_handler(device_flow=False)
        handler._refresh_token = "valid-refresh-token"
        handler._id_token = "stale-id-token"

        def grant_succeeds(data: dict) -> bool:
            handler._access_token = "new-access-token"
            handler._access_token_expires_at = int(time.time()) + 3600
            return True

        with unittest.mock.patch.object(handler, "_request_token",
                                        side_effect=grant_succeeds), \
             unittest.mock.patch.object(handler, "_silent_authentication",
                                        return_value=False):
            result = handler._refresh_token_grant()

        self.assertTrue(result)
        self.assertEqual(handler._access_token, "new-access-token")
        self.assertEqual(handler._id_token, "", "stale id_token must be cleared")
