# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import re
import secrets
import time
from typing import Optional
from urllib.parse import urlencode, urlparse, parse_qs

from resources.lib.addonsettings import AddonSettings, LOCAL
from resources.lib.authentication.authenticationresult import AuthenticationResult
from resources.lib.authentication.oauth2handler import OAuth2Handler
from resources.lib.helpers.htmlentityhelper import HtmlEntityHelper
from resources.lib.helpers.jsonhelper import JsonHelper
from resources.lib.logger import Logger
from resources.lib.retroconfig import Config
from resources.lib.urihandler import UriHandler


class NLZIETOAuth2Handler(OAuth2Handler):
    """NLZiet OAuth2 authentication handler supporting both web and device flows.

    Implements the publicly known NLZiet OAuth2 authentication mechanism with
    support for headless browser-based login and RFC 8628 device flow for TV devices.

    Web flow: Uses triple-web client with PKCE and silent refresh
    Device flow: Uses triple-android-tv client with refresh tokens
    """
    # User-agent rotation
    USER_AGENTS_URL = "https://jnrbsn.github.io/user-agents/user-agents.json"
    USER_AGENTS_CACHE_DAYS = 7
    USER_AGENTS_CACHE_FILE = "user_agent_cache.json"
    FALLBACK_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"

    # Identity server API (https://id.nlziet.nl)
    API_ID_AUTHORIZE = "https://id.nlziet.nl/connect/authorize"
    API_ID_DEVICE = "https://id.nlziet.nl/device"
    API_ID_DEVICE_AUTHORIZATION = "https://id.nlziet.nl/connect/deviceauthorization"
    API_ID_LOGIN = "https://id.nlziet.nl/account/login"
    API_ID_SESSION = "https://id.nlziet.nl/api/session"
    API_ID_SESSION_REVOKE = "https://id.nlziet.nl/api/session/revoke"
    API_ID_TOKEN = "https://id.nlziet.nl/connect/token"
    API_ID_USERINFO = "https://id.nlziet.nl/connect/userinfo"

    # OAuth2 flow settings
    AUTH_METHOD_KEY = "nlziet_auth_method"
    DEVICE_SESSION_KEY = "nlziet_device_session_key"
    REDIRECT_URI = "https://app.nlziet.nl/callback"

    # OAuth2 client IDs
    TV_CLIENT_ID = "triple-android-tv"
    WEB_CLIENT_ID = "triple-web"

    # PKCE constants for NLZIET
    # NLZIET seems to accept 16 or 24 bytes for state, 24 is safer
    STATE_LENGTH_SILENT = 24
    STATE_LENGTH_HEADLESS = 16

    def __init__(self, use_device_flow: Optional[bool] = None):
        """Initialize NLZiet OAuth2 handler.

        Reads the stored auth method from settings to determine which client to
        use. If no method is stored (new user), defaults to device flow.

        :param use_device_flow: Optional override (mainly for tests).
                                If None, reads from stored settings.
        """
        if use_device_flow is None:
            stored = AddonSettings.get_setting(self.AUTH_METHOD_KEY, store=LOCAL)
            use_device_flow = stored != "web"

        self._use_device_flow = use_device_flow

        client_id = self.TV_CLIENT_ID if use_device_flow else self.WEB_CLIENT_ID
        super(NLZIETOAuth2Handler, self).__init__(realm="nlziet", client_id=client_id)

        # Stored for use as id_token_hint in silent PKCE re-authentication (web flow only).
        self._id_token = AddonSettings.get_setting(f"{self.prefix}id_token", store=LOCAL) or ""

        # Set by start_device_flow(); read by _do_poll_once() to use the device client identity.
        self._active_device_client_id: Optional[str] = None

    @property
    def base_auth_url(self) -> str: return self.API_ID_AUTHORIZE

    @property
    def use_device_flow(self) -> bool: return self._use_device_flow

    @property
    def device_session_key(self) -> str:
        """Return the stored device session key, or empty string if none.

        The session key uniquely identifies this device in the user's account
        and is needed to de-register it on log-off.

        :return: Session key string, or ``""`` if no device session is stored.
        """
        return AddonSettings.get_setting(self.DEVICE_SESSION_KEY, store=LOCAL) or ""

    def save_device_session(self, device_name: str) -> bool:
        """Find the current device by name in the session list and persist its key.

        Should be called once after a successful device-flow login so that
        :meth:`deregister_device` can later remove the device without
        requiring the user to identify it.

        :param str device_name: The name used when registering the device.
        :return: True if the key was found and stored, False otherwise.
        """
        try:
            sessions = self.list_devices()
            if not sessions:
                Logger.warning("NLZIET: save_device_session — device list empty or unavailable")
                return False

            match = next((s for s in sessions if s.get("name") == device_name), None)
            if not match:
                Logger.warning(f"NLZIET: save_device_session — device '{device_name}' not found")
                return False

            key = match.get("key", "")
            if not key:
                Logger.warning("NLZIET: save_device_session — device has no session key")
                return False

            AddonSettings.set_setting(self.DEVICE_SESSION_KEY, key, store=LOCAL)
            Logger.debug(f"NLZIET: Device session key stored for '{device_name}'")
            return True
        except Exception as e:
            Logger.error(f"NLZIET: save_device_session failed: {e}")
            return False

    def deregister_device(self) -> bool:
        """Remove this device from the user's account and clear the stored key.

        Only meaningful when the device-flow login method was used.  If no
        session key has been stored (e.g. the user logged in with
        username/password), this is a silent no-op.

        :return: True if the device was removed (or there was nothing to remove).
        """
        key = self.device_session_key
        if not key:
            Logger.debug("NLZIET: deregister_device — no session key stored, nothing to do")
            return True

        AddonSettings.set_setting(self.DEVICE_SESSION_KEY, "", store=LOCAL)
        return self.remove_device(key)

    @property
    def token_endpoint(self) -> str: return self.API_ID_TOKEN

    @property
    def redirect_uri(self) -> str: return self.REDIRECT_URI

    @property
    def device_authorization_endpoint(self) -> str: return self.API_ID_DEVICE_AUTHORIZATION

    @property
    def scopes(self) -> list: return ["openid", "api"]

    @staticmethod
    def _load_cached_user_agents(cache_filename: str, max_age_days: int) -> Optional[list]:
        """Load user agents from cache if fresh enough."""
        cache_path = os.path.join(Config.cacheDir, cache_filename)

        if not os.path.exists(cache_path):
            return None

        try:
            cache_mtime = os.path.getmtime(cache_path)
            cache_age_seconds = time.time() - cache_mtime
            max_age_seconds = max_age_days * 24 * 60 * 60

            if cache_age_seconds >= max_age_seconds:
                return None

            with open(cache_path, 'r') as f:
                user_agents = json.load(f)

            Logger.trace("NLZiet: Using cached user agents")
            return user_agents
        except Exception as e:
            Logger.warning(f"NLZiet: Failed to load cached user agents: {e}")
            return None

    @staticmethod
    def _fetch_and_cache_user_agents(source_url: str, cache_filename: str) -> Optional[list]:
        """Fetch fresh user agents from source and cache them."""
        try:
            Logger.debug("NLZiet: Fetching fresh user agents list")
            response = UriHandler.open(source_url, no_cache=True)

            user_agents = JsonHelper(response).json
            if not isinstance(user_agents, list):
                Logger.warning(f"NLZiet: Expected list of user agents, got {type(user_agents).__name__}")
                return None

            cache_path = os.path.join(Config.cacheDir, cache_filename)

            os.makedirs(Config.cacheDir, exist_ok=True)
            with open(cache_path, 'w') as f:
                json.dump(user_agents, f)

            Logger.debug(f"NLZiet: Cached {len(user_agents)} user agents")
            return user_agents
        except Exception as e:
            Logger.warning(f"NLZiet: Failed to fetch user agents: {e}")
            return None

    @staticmethod
    def _get_latest_user_agent() -> str:
        """Get latest browser user agent string, cached for 7 days.

        Returns the first (most common) user agent from jnrbsn's list, or a fallback.
        """
        user_agents = NLZIETOAuth2Handler._load_cached_user_agents(
            NLZIETOAuth2Handler.USER_AGENTS_CACHE_FILE,
            NLZIETOAuth2Handler.USER_AGENTS_CACHE_DAYS
        )

        if not user_agents:
            user_agents = NLZIETOAuth2Handler._fetch_and_cache_user_agents(
                NLZIETOAuth2Handler.USER_AGENTS_URL,
                NLZIETOAuth2Handler.USER_AGENTS_CACHE_FILE
            )

        if user_agents:
            return user_agents[0]

        Logger.debug("NLZiet: Using fallback user agent")
        return NLZIETOAuth2Handler.FALLBACK_USER_AGENT

    def _store_tokens(self, tokens: dict):
        """Override to also store id_token for NLZiet silent auth."""
        super()._store_tokens(tokens)

        if "id_token" in tokens:
            self._id_token = tokens["id_token"]
            AddonSettings.set_setting(f"{self.prefix}id_token", self._id_token, store=LOCAL)
            Logger.debug(f"OAuth2: Stored id_token for {self.realm} silent auth")

    def _exchange_code_with_verifier(self, auth_code: str, code_verifier: str):
        """Exchange authorization code for tokens with explicit code verifier."""
        self.exchange_code(auth_code, code_verifier, redirect_uri=f"{self.redirect_uri}-silent.html")

    def _do_token_refresh(self):
        """Unconditional token refresh: uses refresh_token (device flow) or silent re-auth (web flow)."""
        if self._refresh_token:
            Logger.debug(f"OAuth2: Refreshing access token using refresh_token for {self.realm}")
            super()._do_token_refresh()
            return

        if not self._id_token:
            raise ValueError("No refresh_token or id_token available for authentication.")

        Logger.debug(f"OAuth2: Attempting silent re-authentication for {self.realm}")

        code_verifier, code_challenge = self._generate_pkce()
        headers = {"User-Agent": self._get_latest_user_agent()}

        state = secrets.token_urlsafe(self.STATE_LENGTH_SILENT)
        auth_params = {
            "response_type": "code",
            "client_id": self.client_id_val,
            "scope": " ".join(self.scopes),
            "redirect_uri": f"{self.redirect_uri}-silent.html",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "response_mode": "query",
            "prompt": "none",
            "id_token_hint": self._id_token
        }

        auth_url = f"{self.base_auth_url}?{urlencode(auth_params)}"

        try:
            response = UriHandler.open(auth_url, no_cache=True, additional_headers=headers)
            response_url = UriHandler.instance().status.url

            parsed = urlparse(response_url)
            params = parse_qs(parsed.query)

            if "code" not in params:
                raise RuntimeError("Silent auth failed: no authorization code in redirect")

            received_state = params.get("state", [None])[0]
            if received_state != state:
                raise RuntimeError(f"Silent auth state mismatch: expected {state}, got {received_state}")

            auth_code = params["code"][0]
            Logger.debug(f"OAuth2: Silent auth successful, exchanging code for tokens")

            self._exchange_code_with_verifier(auth_code, code_verifier)

        except Exception as e:
            Logger.warning(f"OAuth2: Silent re-authentication failed for {self.realm}: {e}")
            self._id_token = ""
            self._access_token = ""
            AddonSettings.set_setting(f"{self.prefix}id_token", "", store=LOCAL)
            AddonSettings.set_setting(f"{self.prefix}access_token", "", store=LOCAL)
            raise

        return True

    def set_profile_claim(self, profile_id: str) -> bool:
        """Embed a profile claim into the access token.

        Sends the current access token to the token endpoint with a
        ``grant_type=profile`` request to obtain a new access token that
        carries the profile UUID as a JWT claim (required for server-side
        content filtering, e.g. kids profiles).

        :param profile_id: The profile UUID to embed as a claim.
        :return: True if a profile-claimed token was obtained, False otherwise.
        """
        if not self._access_token:
            Logger.warning("NLZIET: set_profile_claim called with no access token")
            return False

        data = {
            "grant_type": "profile",
            "profile": profile_id,
            "scope": " ".join(self.scopes),
            "client_id": self.client_id_val,
        }
        headers = {"Authorization": f"Bearer {self._access_token}"}
        try:
            self.request_token(data, headers=headers)
            return True
        except Exception as e:
            Logger.error(f"NLZIET: Profile claim request failed: {e}")
            return False

    def _mijn_nlziet_headers(self) -> dict:
        """Base headers required for mijn.nlziet.nl management API calls."""
        return {
            "User-Agent": self._get_latest_user_agent(),
            "Origin": "https://mijn.nlziet.nl",
            "Referer": "https://mijn.nlziet.nl/",
            "Accept": "application/json, text/plain, */*"
        }

    def list_devices(self, access_token: str = None) -> Optional[list]:
        """List all linked devices for the current user.

        :param access_token: Optional access token. If not provided, uses stored token.
        :return: List of device sessions, or None on error.

        Example response:
        [
            {
                "key": "session_key_string",
                "name": "My Device",
                "lastActivityUtc": "2026-02-18T12:34:56Z",
                ...
            }
        ]
        """
        try:
            Logger.debug("NLZIET: Fetching device list")

            headers = self._mijn_nlziet_headers()

            if access_token:
                headers["Authorization"] = f"Bearer {access_token}"

            response = UriHandler.open(
                self.API_ID_SESSION,
                additional_headers=headers,
                no_cache=True
            )

            data = JsonHelper(response).json

            if isinstance(data, dict) and "sessions" in data:
                sessions = data["sessions"]
                Logger.debug(f"NLZIET: Found {len(sessions)} device(s)")
                return sessions
            elif isinstance(data, list):
                Logger.debug(f"NLZIET: Found {len(data)} device(s)")
                return data
            else:
                Logger.error(f"NLZIET: Unexpected response format: {type(data)}")
                return []

        except Exception as e:
            Logger.error(f"NLZIET: Failed to list devices: {e}")
            return None

    def start_device_flow(self, device_name: str,
                          device_client_id: Optional[str] = None) -> Optional[dict]:
        """Start OAuth2 device authorization flow (RFC 8628).

        This starts the device flow where the user authenticates on another device.

        :param device_name: Device name to display in user's account
        :param device_client_id: Optional client ID for device flow (defaults to self.client_id_val)
        :return: Dict with device_code, user_code, verification_uri, etc., or None on error

        Example response:
        {
            "device_code": "...",
            "user_code": "GHM77G",
            "verification_uri": "https://nlziet.nl/koppel",
            "verification_uri_complete": "https://nlziet.nl/koppel?user_code=GHM77G",
            "expires_in": 900,
            "interval": 5
        }
        """
        self._active_device_client_id = device_client_id
        client_id = device_client_id or self.client_id_val
        headers = {"User-Agent": self._get_latest_user_agent()}

        device_scopes = self.scopes + ["offline_access"]

        data = {
            "client_id": client_id,
            "scope": " ".join(device_scopes),
            "device_name": device_name
        }

        try:
            Logger.info(f"NLZIET: Starting device flow with client_id={client_id}")
            response = UriHandler.open(
                self.API_ID_DEVICE_AUTHORIZATION,
                data=data,
                additional_headers=headers,
                no_cache=True
            )

            result = JsonHelper(response).json
            if "device_code" not in result or "user_code" not in result:
                Logger.error(f"NLZIET: Invalid device flow response: {result}")
                return None

            Logger.info(f"NLZIET: Device flow started. User code: {result.get('user_code')}")
            return result

        except OSError as e:
            Logger.error(f"NLZIET: Device flow start failed (connection error): {e}")
            raise
        except Exception as e:
            Logger.error(f"NLZIET: Device flow start failed: {e}")
            return None

    def poll_device_flow_once(self, device_code: str,
                             device_client_id: Optional[str] = None) -> str:
        """Perform a single device flow poll attempt.

        :param device_code: The device_code from start_device_flow()
        :param device_client_id: Optional client ID for device flow
        :return: "pending", "slow_down", "success", or an error string
        """
        client_id = device_client_id or self.client_id_val
        headers = {"User-Agent": self._get_latest_user_agent()}

        data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
            "client_id": client_id
        }

        try:
            response = UriHandler.open(
                self.token_endpoint,
                data=data,
                additional_headers=headers,
                no_cache=True
            )

            tokens = JsonHelper(response).json

            if "error" in tokens:
                error = tokens["error"]
                if error in ("authorization_pending", "slow_down"):
                    return error

                Logger.warning(f"NLZIET: Device flow error: {error}")
                return error

            if "access_token" in tokens:
                Logger.info("NLZIET: Device flow authentication successful!")
                self._store_tokens(tokens)
                AddonSettings.set_setting(self.AUTH_METHOD_KEY, "device", store=LOCAL)
                return "success"

            return "unknown_response"

        except Exception as e:
            Logger.error(f"NLZIET: Device flow polling error: {e}")
            return "error"

    def _do_poll_once(self, device_code: str) -> str:
        """Override: use NLZIET User-Agent header and device-flow client identity."""
        return self.poll_device_flow_once(device_code, self._active_device_client_id)

    def log_on_with_device_flow(self, device_name: str, device_client_id: Optional[str] = None,
                                display_callback=None) -> AuthenticationResult:
        """Perform device flow authentication.

        :param device_name: Device name to register (e.g., "Living Room Kodi")
        :param device_client_id: Optional client ID for device flow
        :param display_callback: Optional callback function(user_code, verification_uri, verification_uri_complete)
                                to display the code to the user. If None, logs to Logger.
        :return: AuthenticationResult with login status
        """
        device_flow = self.start_device_flow(device_name, device_client_id)
        if not device_flow:
            return AuthenticationResult(None, error="Failed to start device flow")

        user_code = device_flow.get("user_code")
        verification_uri = device_flow.get("verification_uri", self.API_ID_DEVICE)
        verification_uri_complete = device_flow.get("verification_uri_complete")
        expires_in = device_flow.get("expires_in", 900)
        interval = device_flow.get("interval", 5)

        if not display_callback:
            Logger.error("NLZIET: Device flow started without display callback!")
            return AuthenticationResult(None, error="Internal error: No UI callback provided for device flow")

        display_callback(user_code, verification_uri, verification_uri_complete)

        if not self.poll_device_flow(device_flow["device_code"], interval, expires_in, max_attempts=12):
            return AuthenticationResult(None, error="Device flow authentication failed or timed out")

        token = self.get_valid_token()
        if not token:
            return AuthenticationResult(None, error="Failed to retrieve authentication token")

        extracted_username = self._extract_username_from_token(token)
        return AuthenticationResult(
            username=extracted_username or "NLZiet User",
            existing_login=False,
            jwt=token
        )

    def remove_device(self, session_key: str, access_token: str = None) -> bool:
        """Remove a linked device by its session key.

        :param session_key: The session key of the device to remove.
        :param access_token: Optional access token. If not provided, uses stored token.
        :return: True if successful, False otherwise.
        """
        if not session_key:
            Logger.error("NLZIET: Cannot remove device - no session key provided")
            return False

        try:
            Logger.debug(f"NLZIET: Removing device with key: {session_key[:20]}...")

            headers = self._mijn_nlziet_headers()

            if access_token:
                headers["Authorization"] = f"Bearer {access_token}"

            UriHandler.open(
                f"{self.API_ID_SESSION_REVOKE}/{session_key}",
                additional_headers=headers,
                no_cache=True,
                method="DELETE"
            )

            Logger.info("NLZIET: Device removed successfully")
            return True

        except Exception as e:
            Logger.error(f"NLZIET: Failed to remove device: {e}")
            return False

    def get_user_info(self) -> Optional[dict]:
        """Get user information using the current access token.

        :return: Dictionary with user info (email, name, etc.) or None on error.
        """
        token = self.get_valid_token()
        if not token:
            Logger.error("NLZIET: No valid token available for userinfo request")
            return None

        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": self._get_latest_user_agent()
        }

        try:
            Logger.debug("NLZIET: Requesting user info from /connect/userinfo")
            response = UriHandler.open(
                self.API_ID_USERINFO,
                additional_headers=headers,
                no_cache=True
            )

            userinfo = JsonHelper(response).json
            Logger.debug(f"NLZIET: User info retrieved for: {userinfo.get('email', 'N/A')}")
            return userinfo

        except Exception as e:
            Logger.error(f"NLZIET: Failed to get user info: {e}")
            return None

    def _clear_token_settings(self) -> None:
        super()._clear_token_settings()
        self._id_token = ""
        AddonSettings.set_setting(f"{self.prefix}id_token", "", store=LOCAL)
        AddonSettings.set_setting(self.DEVICE_SESSION_KEY, "", store=LOCAL)

    def _extract_csrf_token(self, content: str) -> Optional[str]:
        """Extract the RequestVerificationToken CSRF token from HTML.

        :param content:     The HTML response content
        :return:            The CSRF token string, or None if not found

        """
        csrf_pattern = r'name="__RequestVerificationToken".*?value="([^"]+)"'
        csrf = re.search(csrf_pattern, content)
        return csrf.group(1) if csrf else None

    def perform_headless_login(self, username: str, password: str) -> bool:
        """Perform headless OAuth2 PKCE login flow.

        Flow:
        1. GET authorize endpoint -> redirects to login page (302)
        2. GET login page -> extract CSRF token and ReturnUrl
        3. POST credentials to login -> redirects with code (302)
        4. Follow redirect chain to get final callback with auth code
        5. Exchange auth code for tokens
        """
        state = secrets.token_urlsafe(self.STATE_LENGTH_HEADLESS)
        verifier, challenge = self._generate_pkce()

        headers = {"User-Agent": self._get_latest_user_agent()}

        params = {
            "client_id": self.client_id_val,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "response_mode": "query"
        }

        try:
            Logger.debug(f"NLZIET: Starting authorization at {self.base_auth_url}")
            auth_url = f"{self.base_auth_url}?{urlencode(params)}"

            login_page = UriHandler.open(auth_url, no_cache=True, additional_headers=headers)

            response_url = UriHandler.instance().status.url
            sso_code_match = re.search(r'[?&]code=([^&\s]+)', response_url)
            sso_state_match = re.search(r'[?&]state=([^&\s]+)', response_url)

            if sso_code_match and sso_state_match:
                received_state = sso_state_match.group(1)
                if received_state != state:
                    Logger.error(f"NLZIET: SSO state mismatch! Expected: {state}, Got: {received_state}")
                    return False

                Logger.info("NLZIET: SSO session detected, extracting authorization code from callback")
                auth_code = sso_code_match.group(1)
                Logger.debug(f"NLZIET: SSO authorization code: {auth_code[:10]}...")
                return self.exchange_code(auth_code, verifier)

            csrf_token = self._extract_csrf_token(login_page)

            ret_url_match = re.search(r'<input[^>]*name="ReturnUrl"[^>]*value="([^"]+)"', login_page)
            if not ret_url_match:
                ret_url_match = re.search(r'ReturnUrl=([^&"\s]+)', login_page)

            if not csrf_token:
                Logger.error("NLZIET: Missing CSRF token from login page")
                Logger.debug(f"NLZIET: Page preview: {login_page[:500]}")
                return False

            if not ret_url_match:
                Logger.error("NLZIET: Missing ReturnUrl from login page")
                Logger.debug(f"NLZIET: Page preview: {login_page[:500]}")
                return False

            return_url = HtmlEntityHelper.convert_html_entities(ret_url_match.group(1))
            Logger.debug(f"NLZIET: Extracted ReturnUrl: {return_url[:100]}...")

            login_data = {
                "ReturnUrl": return_url,
                "EmailAddress": username,
                "Password": password,
                "RememberLogin": "true",
                "button": "login",
                "__RequestVerificationToken": csrf_token
            }

            login_response = UriHandler.open(
                self.API_ID_LOGIN,
                params=urlencode(login_data),
                no_cache=True,
                additional_headers=headers
            )

            response_url = UriHandler.instance().status.url
            Logger.debug(f"NLZIET: Response URL after login POST: {response_url}")

            code_match = re.search(r'[?&]code=([^&\s]+)', response_url)
            state_match = re.search(r'[?&]state=([^&\s]+)', response_url)

            if not code_match:
                Logger.error("NLZIET: No authorization code in final URL")
                Logger.debug(f"NLZIET: Response URL: {response_url}")
                Logger.debug(f"NLZIET: Response preview: {login_response[:500]}")
                return False

            if not state_match:
                Logger.error("NLZIET: No state in final URL")
                return False

            received_state = state_match.group(1)
            if received_state != state:
                Logger.error(f"NLZIET: State mismatch! Expected: {state}, Got: {received_state}")
                return False

            auth_code = code_match.group(1)
            Logger.debug(f"NLZIET: Received authorization code: {auth_code[:10]}...")

            return self.exchange_code(auth_code, verifier)

        except Exception as e:
            Logger.error(f"NLZIET: Login failed with exception: {e}")
            return False

    def log_on(self, username: str, password: str) -> AuthenticationResult:
        """Framework override to support headless login.

        :param username:    The NLZIET username/email
        :param password:    The NLZIET password
        :return:            AuthenticationResult with login status

        """
        if not username or not password:
            return AuthenticationResult(None, error="Username and password are required.")

        if not self.perform_headless_login(username, password):
            return AuthenticationResult(None, error="NLZIET login failed.")

        AddonSettings.set_setting(self.AUTH_METHOD_KEY, "web", store=LOCAL)

        token = self.get_valid_token()
        if not token:
            return AuthenticationResult(None, error="Failed to retrieve authentication token.")

        extracted_username = self._extract_username_from_token(token)
        return AuthenticationResult(
            username=extracted_username or username,
            existing_login=False,
            jwt=token
        )

    def log_off(self, username) -> bool:
        self._clear_token_settings()
        Logger.info(f"OAuth2: Logged off user for {self.realm}")
        return True
