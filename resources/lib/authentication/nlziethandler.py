# SPDX-License-Identifier: GPL-3.0-or-later

import re
import secrets
import time
import json
import os
from datetime import datetime
from typing import List, Optional, Tuple, final
from urllib.parse import urlencode, urlparse, parse_qs, quote

from resources.lib.addonsettings import AddonSettings, LOCAL
from resources.lib.authentication.authenticationhandler import DeviceAuthData
from resources.lib.authentication.authenticationresult import AuthenticationResult
from resources.lib.authentication.oauth2handler import DEVICE_FLOW_INTERVAL
from resources.lib.authentication.oidchandler import OIDCHandler
from resources.lib.helpers.jsonhelper import JsonHelper
from resources.lib.logger import Logger
from resources.lib.urihandler import UriHandler


# Identity server API (https://id.nlziet.nl)
API_ID_AUTHORIZE = "https://id.nlziet.nl/connect/authorize"
API_ID_DEVICE = "https://id.nlziet.nl/device"
API_ID_DEVICE_AUTHORIZATION = "https://id.nlziet.nl/connect/deviceauthorization"
API_ID_END_SESSION = "https://id.nlziet.nl/connect/endsession"
API_ID_LOGIN = "https://id.nlziet.nl/account/login"
API_ID_REVOCATION = "https://id.nlziet.nl/connect/revocation"
API_ID_SESSION = "https://id.nlziet.nl/api/session"
API_ID_SESSION_REVOKE = "https://id.nlziet.nl/api/session/revoke"
API_ID_TOKEN = "https://id.nlziet.nl/connect/token"
API_ID_USERINFO = "https://id.nlziet.nl/connect/userinfo"

# Web client (triple-web on https://app.nlziet.nl)
REDIRECT_URI = "https://app.nlziet.nl/callback"
WEB_CLIENT_ID = "triple-web"
WEB_CLIENT_SILENT_REDIRECT_URI = "https://app.nlziet.nl/callback-silent.html"

# Device flow client (triple-android-tv)
DEVICE_CLIENT_ID = "triple-android-tv"
DEVICE_FLOW_USER_AGENT = "okhttp/5.3.2"
DEVICE_SESSION_DRIFT_THRESHOLD = 60  # seconds

# Account management client (https://mijn.nlziet.nl)
ACCOUNT_CLIENT_ID = "mijn-nlziet"
ACCOUNT_ORIGIN = "https://mijn.nlziet.nl"
ACCOUNT_REDIRECT_URI = "https://mijn.nlziet.nl/callback-silent.html"
ACCOUNT_REFERER = "https://mijn.nlziet.nl/"
ACCOUNT_SERVER = "mijn.nlziet.nl"

# Settings storage keys
AUTH_CLIENT_ID_KEY = "nlziet_auth_client_id"
DEVICE_SESSION_KEY = "nlziet_device_session_key"


@final
class NLZIETHandler(OIDCHandler):
    """
    NLZiet OAuth2 authentication handler supporting both web and device flows.

    Implements the NLZIET-specific OAuth2 authentication on top of the
    protocol stack:

      AuthenticationHandler          ← abstract interface
        └── OAuth2Handler            ← RFC 6749 + RFC 7636
              └── OIDCHandler        ← OpenID Connect Core 1.0
                    └── NLZIETHandler ← NLZIET-specific adapter

    Web flow: Uses the ``triple-web`` client with PKCE (RFC 7636) and
    silent re-authentication via ``prompt=none``
    (OpenID Connect Core 1.0, §3.1.2.1).

    Device flow: Uses the ``triple-android-tv`` client with RFC 8628 device
    authorization and refresh tokens.

    The handler receives a reference to the channel's ``httpHeaders`` dict so
    that its own API calls (to the identity server) include the same app and
    device identification headers the channel sends on content requests.

    NLZIET extends the standard OpenID Connect ID token with two proprietary
    claims: ``profileId`` (active profile UUID) and ``profileType`` (e.g.
    ``"Adult"``, ``"ChildYoung"``, ...).
    """

    _pre_registration_keys: Optional[set] = None

    def __init__(self, http_headers: Optional[dict] = None) -> None:
        """
        Initialize NLZiet OAuth2 handler.

        Reads the stored client ID from settings to determine the active flow.
        New users default to device flow.

        :param http_headers: Application-identification headers for this
                            handler layer (app name, version, platform etc.).
        """

        client_id = AddonSettings.get_setting(AUTH_CLIENT_ID_KEY, store=LOCAL) or DEVICE_CLIENT_ID

        super(NLZIETHandler, self).__init__(
            realm="nlziet", client_id=client_id,
            userinfo_endpoint=API_ID_USERINFO,
            headers=http_headers)


    @property
    def authorization_endpoint(self) -> str:
        """
        NLZIET authorization endpoint URL.

        :return: The URL string.
        """

        return API_ID_AUTHORIZE


    @property
    def token_endpoint(self) -> str:
        """
        NLZIET token endpoint URL.

        :return: The URL string.
        """

        return API_ID_TOKEN


    @property
    def redirect_uri(self) -> str:
        """
        OAuth2 redirect URI for the interactive authorization code flow.

        :return: The URI string.
        """

        return REDIRECT_URI


    def _redirect_uri_silent(self) -> str:
        """
        Silent redirect URI for the web client's ``prompt=none`` flow.

        :return: The URI string.
        """

        return WEB_CLIENT_SILENT_REDIRECT_URI


    @property
    def device_authorization_endpoint(self) -> str:
        """
        NLZIET device authorization endpoint URL (RFC 8628).

        :return: The URL string.
        """

        return API_ID_DEVICE_AUTHORIZATION


    @property
    def scopes(self) -> List[str]:
        """
        OAuth2 scopes required for NLZIET API access.

        :return: ``openid`` (identity) and ``api`` (content access).
        """

        return ["openid", "api"]


    @property
    def _device_flow(self) -> bool:
        """
        Whether the Android TV client (``triple-android-tv``) is active.

        Computed from :attr:`_client_id` — no separate stored flag needed.

        :return: ``True`` when the device flow client is active.
        """

        return self._client_id == DEVICE_CLIENT_ID


    @property
    def device_flow(self) -> bool:
        """
        Whether the handler is currently operating in device authorization flow.

        :return: ``True`` if device flow is active, ``False`` otherwise.
        """

        return self._device_flow


    @property
    def authentication_headers(self) -> dict:
        """
        Return the complete header dict for authenticated NLZIET API requests.

        Combines the RFC 6750 Bearer authorization header with static
        application-identification headers, and adds the device-flow
        User-Agent when the Android TV client is active.

        :return: Header dict ready for use as ``additional_headers``.
        """

        headers = super().authentication_headers
        if self._device_flow:
            headers["User-Agent"] = DEVICE_FLOW_USER_AGENT
        headers.update(self._authorization_bearer())
        return headers


    def _token_request_headers(self) -> dict:
        """
        NLZIET-specific base headers for token endpoint requests.

        Injects the OkHttp User-Agent for device flow so that all token
        endpoint calls (refresh, code exchange, silent re-auth) identify
        themselves correctly. Web flow omits the User-Agent so that
        UriHandler injects its modern browser default.

        :return: Header dict for token endpoint requests.
        """

        if self._device_flow:
            return {"User-Agent": DEVICE_FLOW_USER_AGENT}

        return {}


    @property
    def token_profile_id(self) -> Optional[str]:
        """
        The ``profileId`` claim from the current access token.

        :return: - The claim value as a string (e.g. ``"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"``).
                 - ``None`` if unavailable.
        """

        if not self._access_token:
            return None

        claims = self.decode_token(self._access_token) or {}
        profile_id = claims.get("profileId")
        return profile_id if isinstance(profile_id, str) else None


    @property
    def token_profile_type(self) -> str:
        """
        The ``profileType`` claim from the current access token.

        :return: - The claim value as a string (e.g. ``"Adult"``, ``"ChildYoung"``).
                 - ``""`` if unavailable.
        """

        if not self._access_token:
            return ""

        claims = self.decode_token(self._access_token) or {}
        profile_type = claims.get("profileType")
        return profile_type if isinstance(profile_type, str) else ""


    # -- Token handling -------------------------------------------------

    def _refresh_token_grant(self) -> bool:
        """
        Extend the OIDC refresh with a silent re-auth fallback
        when no refresh token is available or the refresh grant fails.

        When no refresh token is stored, or when the refresh grant fails,
        falls back to :meth:`_silent_authentication` directly.
        """

        if not self._refresh_token:
            if self._silent_authentication():
                return True
            self._clear_tokens()
            return False

        if super()._refresh_token_grant():
            return True

        self._clear_tokens("access_token")
        self._clear_tokens("refresh_token")
        # id_token hint may still be valid so retry again
        if self._silent_authentication():
            return True

        self._clear_tokens()
        return False


    def set_profile_claim(self, profile_id: str) -> bool:
        """
        Exchange the current access token for a profile-scoped token.

        Uses a custom OIDC extension grant (``grant_type=profile``) to perform
        a token exchange at the token endpoint.  Validates that the returned
        JWT contains the ``profileId`` claim before updating the handler's
        state, ensuring profile-based content filtering is enforced by the
        backend.

        On success, also registers the ``profileId`` as an OIDC claims request
        (OpenID Connect Core 1.0, §5.5) so that any subsequent silent
        re-authentication preserves the profile binding.

        :param profile_id: The profile UUID to embed as a claim.

        :return: - True if the profile token exchange succeeded and the ``profileId`` claim was verified.
                 - False otherwise.
        """

        if not self._access_token:
            Logger.warning("NLZIET.auth: set_profile_claim called with no access token")
            return False

        if not self._client_id:
            Logger.warning("NLZIET.auth: set_profile_claim called with no active client")
            return False

        data = {
            "client_id": self._client_id,
            "grant_type": "profile",
            "profile": profile_id,
            "scope": "openid api",
        }

        claims = self._token_exchange_request(data, headers=self.authentication_headers)
        if claims is None:
            Logger.error("NLZIET.auth: Profile token exchange failed")
            return False

        if "profileId" not in claims:
            Logger.warning(
                "NLZIET.auth: Profile token exchange response missing profileId "
                f"(expected {profile_id!r})"
            )
            return False

        token_profile_id = claims["profileId"]
        if not isinstance(token_profile_id, str):
            Logger.warning(
                "NLZIET.auth: Profile claim has unexpected type "
                f"(expected {profile_id!r}, got {token_profile_id!r})"
            )
            return False

        if token_profile_id.lower() != profile_id.lower():
            Logger.warning(
                f"NLZIET.auth: Profile claim not embedded in returned token "
                f"(expected {profile_id!r}, got {token_profile_id!r})"
            )
            return False

        id_token_claims = {
            "profileId": {
                "essential": True,
                "value": profile_id,
            },
        }
        if self._set_claims(id_token_claims) is None:
            Logger.error("NLZIET.auth: Failed to set profile claims")
            return False

        return True


    # -- Device management ----------------------------------

    def _get_account_access_token(self) -> Optional[str]:
        """
        Obtain a short-lived access token for the account OAuth client.

        The device management API (``id.nlziet.nl/api/session``) accepts only
        tokens issued to the ``ACCOUNT_CLIENT_ID`` client. Uses
        :meth:`~oidchandler.OIDCHandler._silent_authentication_request` with
        NLZIET-specific client credentials.

        :return: - Access token string.
                 - ``None`` if silent auth fails.
        """

        Logger.debug("NLZIET.auth: Obtaining account token (silent auth)")
        tokens = self._silent_authentication_request(
            ACCOUNT_CLIENT_ID,
            ACCOUNT_REDIRECT_URI,
            "IdentityServerApi openid api",
        )
        if tokens is None:
            Logger.warning("NLZIET.auth: Silent auth returned no tokens")
            return None

        return tokens.get("access_token")


    def _account_headers(self, account_token: Optional[str] = None) -> dict:
        """
        Build request headers for ``ACCOUNT_SERVER`` API calls.

        :param account_token: If provided, sent as Bearer Authorization header.

        :return: Header dict for ``ACCOUNT_SERVER`` API calls.
        """

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": ACCOUNT_ORIGIN,
            "Referer": ACCOUNT_REFERER,
        }

        if account_token:
            headers["Authorization"] = f"Bearer {account_token}"

        return headers


    @staticmethod
    def _load_device_session_key() -> str:
        """
        Return the stored device session key, or empty string if none.

        The session key uniquely identifies this device in the user's account
        and is needed to de-register it on log-off.

        :return: - Session key string.
                 - ``""`` if no device session is stored.
        """

        session_key = AddonSettings.get_setting(DEVICE_SESSION_KEY, store=LOCAL) or ""
        Logger.debug(f"NLZIET.auth: Device session key loaded (present={bool(session_key)})")
        return session_key


    @staticmethod
    def _save_device_session_key(session_key: str) -> None:
        """
        Persist the device session key used for later deregistration.

        :param session_key: The session key to persist.
        """

        if not session_key:
            Logger.debug("NLZIET.auth: Invalid key to be saved")
            return

        AddonSettings.set_setting(DEVICE_SESSION_KEY, session_key, store=LOCAL)
        Logger.debug(f"NLZIET.auth: Device session key {session_key!r} saved")


    @staticmethod
    def _clear_device_session_key() -> None:
        """Remove the locally stored device session key."""

        AddonSettings.set_setting(DEVICE_SESSION_KEY, "", store=LOCAL)
        Logger.debug("NLZIET.auth: Device session key cleared")


    def _list_devices(self, account_token: Optional[str] = None) -> Optional[list]:
        """
        List all linked devices for the current user.

        :param account_token: Token for the account API. Obtain
            via ``_get_account_access_token()``.

        :return: - List of device session dicts.
                 - ``None`` on error.
        """

        Logger.debug("NLZIET.auth: Fetching device list")

        response = UriHandler.open(
            API_ID_SESSION,
            additional_headers=self._account_headers(account_token),
            no_cache=True,
        )
        status = UriHandler.last_status()
        if status.error:
            Logger.error(f"NLZIET.auth: Device list request failed: {status.code} {status.reason}")
            return None
        try:
            data = JsonHelper(response).json
        except (ValueError, TypeError):
            Logger.error("NLZIET.auth: Failed to parse device list response")
            return None

        if isinstance(data, dict) and "sessions" in data:
            sessions = data["sessions"]
            Logger.debug(f"NLZIET.auth: Found {len(sessions)} device(s)")
            return sessions
        elif isinstance(data, list):
            Logger.debug(f"NLZIET.auth: Found {len(data)} device(s)")
            return data
        else:
            Logger.error(f"NLZIET.auth: Unexpected response format: {type(data)}")
            return []


    def _remove_device(self, session_key: str, account_token: Optional[str] = None) -> bool:
        """
        Remove a linked device by its session key.

        :param session_key: The session key of the device to remove.
        :param account_token: Token for the account API. Obtain
            via ``_get_account_access_token()``.

        :return: - True if the device was removed successfully.
                 - False otherwise.
        """

        if not session_key:
            Logger.error("NLZIET.auth: Cannot remove device - no session key provided")
            return False

        Logger.debug(f"NLZIET.auth: Removing device with key: {session_key[:20]}...")

        UriHandler.open(
            f"{API_ID_SESSION_REVOKE}/{session_key}",
            additional_headers=self._account_headers(account_token),
            method="DELETE",
            no_cache=True,
        )

        status = UriHandler.last_status()
        if not status.error:
            Logger.info("NLZIET.auth: Device removed successfully")
            return True

        Logger.error(f"NLZIET.auth: Device removal failed: {status.code} {status.reason}")
        return False


    def _deregister_device(self) -> bool:
        """
        Remove this device from the user's account and clear the stored key.

        Only meaningful when the device-flow login method was used.  If no
        session key has been stored (e.g. the user logged in with
        username/password), this is a silent no-op.

        :return: - True if the device was removed successfully.
                 - False if no session key is stored or removal failed.
        """

        key = self._load_device_session_key()
        if not key:
            Logger.warning("NLZIET.auth: no session key stored; device remains registered in account")
            return False

        account_token = self._get_account_access_token()
        if not account_token:
            Logger.warning("NLZIET.auth: no account token; attempting revoke with device token")
            account_token = self._access_token or None

        removed = self._remove_device(key, account_token)
        if removed:
            self._clear_device_session_key()
        return removed


    def _get_device_sessions_and_keys(self) -> Tuple[List[dict], set]:
        """
        Fetch raw sessions and a set of their unique keys.

        :return: Tuple of (session list, set of session key strings).
        """
        account_token = self._get_account_access_token()
        devices: List[dict] = (self._list_devices(account_token) or []) if account_token else []
        keys = {d["key"] for d in devices if d.get("key")}
        return devices, keys


    def _find_device_session_key(self, ref_ts: int) -> Optional[str]:
        """
        Identify the session key of the device we just registered.

        The backend does not return the key for a newly created device, so this
        method infers it indirectly: it diffs the current device list against
        the snapshot taken before registration (``_pre_registration_keys``),
        then picks the new entry whose timestamp is closest to ``ref_ts``.
        The timestamp comparison guards against the unlikely case where two
        registrations race within the same account.

        :param ref_ts: Reference Unix timestamp (``iat`` from the id_token).

        :return: - The matched session key string.
                 - ``None`` if no match found.
        """

        devices, current_keys = self._get_device_sessions_and_keys()
        new_keys = current_keys - (self._pre_registration_keys or set())
        self._pre_registration_keys = None

        if not new_keys:
            Logger.warning("NLZIET.auth: No new device keys found.")
            return None

        candidates = []
        for d in devices:
            if d.get("key") in new_keys:
                raw_ts = d.get("updatedAt") or d.get("createdAt") or ""
                try:
                    d["ref_ts"] = datetime.fromisoformat(raw_ts).timestamp()
                    candidates.append(d)
                except (ValueError, TypeError):
                    continue
        if not candidates:
            Logger.error("NLZIET.auth: New keys found with invalid timestamps.")
            return None

        best_device = min(candidates, key=lambda c: abs(c["ref_ts"] - ref_ts))
        drift = abs(best_device["ref_ts"] - ref_ts)
        if drift > DEVICE_SESSION_DRIFT_THRESHOLD:
            Logger.error(
                f"NLZIET.auth: Candidate {best_device['key']} rejected "
                f"(drift: {drift:.0f}s > {DEVICE_SESSION_DRIFT_THRESHOLD}s limit).")
            return None

        return best_device["key"]


    # -- Device flow ----------------------------------------

    def _device_access_token_granted(self, tokens: dict) -> None:
        """
        Store the NLZIET client ID and device session key after a successful
        token exchange.

        :param tokens: The parsed token response dict from the server.
        """

        Logger.info("NLZIET.auth: Device flow authentication successful!")
        AddonSettings.set_setting(AUTH_CLIENT_ID_KEY, DEVICE_CLIENT_ID, store=LOCAL)

        ref_ts = (self.decode_token(self._id_token) or {}).get("iat")
        key = self._find_device_session_key(ref_ts) if ref_ts else None
        if key:
            self._save_device_session_key(key)
            Logger.debug(f"NLZIET.auth: Device session key saved: {key}")
        else:
            Logger.warning(
                "NLZIET.auth: Session key not saved — device cannot be deregistered on logoff")


    def _device_request_headers(self) -> dict:
        """
        NLZIET-specific request headers for the device flow poll (RFC 8628, §3.4).

        :return: Header dict for device flow requests.
        """

        return self.authentication_headers


    def _start_device_authorization(self, device_name: str) -> Optional[DeviceAuthData]:
        """
        Start NLZIET device authorization with NLZIET-specific headers.

        Switches to the device OAuth2 client first so that tokens are stored
        under the correct client prefix. Specialises the base by adding
        ``device_name``, embedded in the QR URL so the device appears with a
        recognisable name in the user's account.

        :param device_name: Device name shown in the user's NLZIET account.

        :return: - :class:`DeviceAuthData` dict (``device_code``, ``user_code``, etc.).
                 - ``None`` on error.
        """

        self._client_id = DEVICE_CLIENT_ID
        self._poll_interval = float(DEVICE_FLOW_INTERVAL)

        result = self._device_authorization_request(additional_headers=self.authentication_headers)
        if result is None:
            self._client_id = None
            return None

        device_auth = self._parse_device_auth_data(result)
        if device_auth is None:
            Logger.error(f"NLZIET.auth: Invalid device flow response: {result!r}")
            self._client_id = None
            return None

        _, self._pre_registration_keys = self._get_device_sessions_and_keys()
        Logger.debug(f"NLZIET.auth: Snapshot taken — {len(self._pre_registration_keys)} existing device(s)")

        device_auth.qr_url = f"{API_ID_DEVICE}?code={quote(device_auth.user_code)}&name={quote(device_name)}"

        return device_auth


    def _revoke_device_authorization(self, username: str) -> bool:
        """
        Revoke device tokens and deregister the device from the account.

        Clears all stored authentication tokens. As a provider-specific
        side-effect, also removes the NLZIET device session key from the
        account.

        :param username: The account username.

        :return: ``True`` if deregistration succeeded, ``False`` otherwise.
        """

        result = self._deregister_device()

        self._clear_tokens()
        AddonSettings.set_setting(AUTH_CLIENT_ID_KEY, "", store=LOCAL)
        Logger.info(f"NLZIET.auth: Device flow logged off for {self.realm}")

        return result


    # -- Web flow -------------------------------------------

    def _extract_csrf_token(self, content: str) -> Optional[str]:
        """
        Extract the RequestVerificationToken CSRF token from HTML.

        :param content:     The HTML response content

        :return: - The CSRF token string.
                 - ``None`` if not found.
        """

        csrf_pattern = r'name="__RequestVerificationToken".*?value="([^"]+)"'
        csrf = re.search(csrf_pattern, content)
        return csrf.group(1) if csrf else None


    def _web_form_login(self, username: str, password: str) -> Optional[str]:
        """
        Perform an OAuth2 Authorization Code + PKCE flow (RFC 7636) via web form.

        Automates the browser steps of RFC 6749, §4.1 by driving the NLZIET
        login form directly. The CSRF ``state`` parameter (RFC 6749, §10.12)
        is verified on the redirect callback.

        :param username: The NLZIET account username/email.
        :param password: The NLZIET account password.

        :return: - ``None`` on success.
                 - ``"invalid_credentials"`` on bad credentials.
                 - ``"network_error"`` on request failure.
        """

        Logger.debug(f"NLZIET.auth: Starting authorization at {self.authorization_endpoint}")

        state = secrets.token_urlsafe(self.STATE_LENGTH)
        verifier, challenge = self._generate_pkce()
        params = {
            "client_id": self._client_id,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "redirect_uri": self.redirect_uri,
            "response_mode": "query",
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "state": state,
        }
        headers = self.authentication_headers
        auth_url = f"{self.authorization_endpoint}?{urlencode(params)}"
        login_page = UriHandler.open(
            auth_url,
            additional_headers=headers,
            no_cache=True,
        )
        page_redirect = UriHandler.last_status()
        if page_redirect.error:
            Logger.error(f"NLZIET.auth: Login page request failed: {page_redirect.code} {page_redirect.reason}")
            return "network_error"

        auth_code = self._extract_auth_code(page_redirect.url, state)
        if auth_code:
            Logger.info("NLZIET.auth: SSO session detected, extracting authorization code from callback")
            Logger.debug(f"NLZIET.auth:  SSO authorization code: {auth_code[:10]}...")
            if not self._exchange_code(auth_code, verifier):
                return "network_error"

            Logger.debug("NLZIET.auth: Already logged in, SSO session still active")
            return None

        Logger.info("NLZIET.auth: No SSO session, continuing with login form")

        csrf_token = self._extract_csrf_token(login_page)
        if not csrf_token:
            Logger.error("NLZIET.auth: Missing CSRF token from login page")
            Logger.debug(f"NLZIET.auth:  Page preview: {(login_page or '')[:500]}")
            return "network_error"

        redirect_params = parse_qs(urlparse(page_redirect.url).query)
        if not redirect_params.get("ReturnUrl"):
            Logger.error("NLZIET.auth: Missing ReturnUrl from redirect URL")
            Logger.debug(f"NLZIET.auth:  Redirect URL: {page_redirect.url}")
            return "network_error"

        return_url = redirect_params["ReturnUrl"][0]
        if not return_url:
            Logger.error("NLZIET.auth: Empty ReturnUrl in redirect URL")
            Logger.debug(f"NLZIET.auth:  Redirect URL: {page_redirect.url}")
            return "network_error"

        Logger.debug(f"NLZIET.auth: Extracted ReturnUrl: {return_url[:100]}...")

        login_data = {
            "button": "login",
            "EmailAddress": username,
            "Password": password,
            "RememberLogin": "true",
            "ReturnUrl": return_url,
            "__RequestVerificationToken": csrf_token
        }

        login_response = UriHandler.open(
            API_ID_LOGIN,
            additional_headers=headers,
            params=urlencode(login_data),
            no_cache=True,
        )
        post_redirect = UriHandler.last_status()
        if post_redirect.error:
            Logger.error(f"NLZIET.auth: Login POST request failed: {post_redirect.code} {post_redirect.reason}")
            return "network_error"
        Logger.debug(f"NLZIET.auth: Response URL after login POST: {post_redirect.url}")

        auth_code = self._extract_auth_code(post_redirect.url, state)
        if not auth_code:
            Logger.error("NLZIET.auth: No authorization code in final URL")
            Logger.debug(f"NLZIET.auth:  Response URL: {post_redirect.url}")
            Logger.debug(f"NLZIET.auth:  Response preview: {(login_response or '')[:500]}")
            return "invalid_credentials"

        Logger.debug(f"NLZIET.auth: Received authorization code: {auth_code[:10]}...")
        if not self._exchange_code(auth_code, verifier):
            return "network_error"

        Logger.debug("NLZIET.auth: Login successful")
        return None


    def _credential_log_on(self, username: str, password: str) -> AuthenticationResult:
        """
        Authenticate against the NLZIET identity server using the web client.

        Drives the headless PKCE + form-submission flow and, on success,
        marks the active client as the web client in persistent settings.

        :param username:    The NLZIET username/email
        :param password:    The NLZIET password

        :return: AuthenticationResult with login status
        """

        self._client_id = WEB_CLIENT_ID

        result = self._web_form_login(username, password)
        if result:
            self._client_id = None
            return AuthenticationResult("", error=result)

        AddonSettings.set_setting(AUTH_CLIENT_ID_KEY, WEB_CLIENT_ID, store=LOCAL)

        token = self._get_access_token()
        if not token:
            self._client_id = None
            return AuthenticationResult("", error="network_error")

        extracted_username = self._get_username_from_access_token(token)
        return AuthenticationResult(
            existing_login=False,
            jwt=token,
            username=extracted_username or username,
        )


    def _credential_log_off(self, username: str) -> bool:
        """
        Revoke the access token and end the OIDC server session.

        Delegates to :meth:`OIDCHandler._end_session` using the
        NLZIET-specific endpoints, then clears all stored tokens and the
        stored client ID.

        :param username: The account username.

        :return: ``True`` if both server calls succeeded, ``False`` otherwise.
        """

        success = self._end_session(WEB_CLIENT_ID, API_ID_REVOCATION, API_ID_END_SESSION, REDIRECT_URI)
        self._clear_tokens()
        AddonSettings.set_setting(AUTH_CLIENT_ID_KEY, "", store=LOCAL)
        Logger.info(f"NLZIET.auth: Credential flow logged off for {self.realm}")
        return success


    # -- Authentication handler API -------------------------

    def verify_token(self) -> str:
        """Check whether the current access token is accepted by the server.

        Uses :meth:`get_user_info` as a lightweight probe. A 401/403 means the
        token is rejected; transport failures and 5xx responses are treated as
        transient (the token may still be valid).

        :return: - ``"valid"``
                 - ``"expired"``
                 - ``"network_error"``
        """

        try:
            self.get_user_info()
            Logger.debug("NLZIET.auth: Token validation — valid")
            return "valid"
        except PermissionError:          # 401/403 — must come before IOError
            Logger.debug("NLZIET.auth: Token validation — expired (server rejected token)")
            return "expired"
        except (RuntimeError, IOError):  # 5xx, bad JSON, transport failure
            Logger.warning("NLZIET.auth: Token validation — network error (token status unknown)")
            return "network_error"


    def active_authentication(self) -> AuthenticationResult:
        """
        Resume an existing authentication session, refreshing tokens if needed.

        :return: - AuthenticationResult with current username, if authenticated.
                 - AuthenticationResult with empty username, if no session is active.
        """

        token = self.refresh_access_token()
        if token:
            username = self._get_username_from_access_token(token)
            Logger.debug(f"NLZIET.auth: Active authentication found for user {username!r}")
            return AuthenticationResult(username, existing_login=True, jwt=token)

        Logger.debug("NLZIET.auth: No active authentication session")
        return AuthenticationResult("")


    def get_authentication_token(self) -> Optional[str]:
        """
        Refresh and return a valid access token for use by the Retrospect framework.

        :return: - A valid access token string.
                 - ``None`` if refresh fails.
        """

        return self.refresh_access_token()
