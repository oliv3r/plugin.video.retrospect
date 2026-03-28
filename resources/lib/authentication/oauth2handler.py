# SPDX-License-Identifier: GPL-3.0-or-later

import base64
import hashlib
import json
import re
import secrets
import time
import urllib.parse
from abc import ABC, abstractmethod
from typing import Any, List, Optional, Tuple


class _JwtFallback:
    """
    Bare-minimum JWT payload decoder used when PyJWT is unavailable.

    PyJWT's cryptography dependency uses Rust (PyO3) bindings, which don't
    support Python 3.14 subinterpreters yet (https://github.com/PyO3/pyo3/issues/576).
    This shim decodes the payload without signature verification.

    Can be removed once PyO3 issue #576 is resolved and PyJWT ships a
    compatible release.
    """

    @staticmethod
    def decode(token: str, **_kwargs: Any) -> dict:
        """
        Decode a JWT token payload without signature verification.

        :param token:    The JWT string to decode.
        :param _kwargs: Accepted for API compatibility; ignored.
        """

        try:
            payload = token.split('.')[1]
            padding = 4 - len(payload) % 4
            if padding != 4:
                payload += '=' * padding
            return json.loads(base64.urlsafe_b64decode(payload))
        except (IndexError, ValueError) as e:
            raise ValueError(f"Not a valid JWT: {e}") from e


try:
    import jwt
except ImportError:
    try:
        import pyjwt as jwt  # type: ignore[no-redef]
    except ImportError:
        jwt = _JwtFallback()  # type: ignore[assignment]

from resources.lib.addonsettings import AddonSettings, LOCAL
from resources.lib.urihandler import UriHandler
from resources.lib.helpers.jsonhelper import JsonHelper
from resources.lib.logger import Logger

# Device flow defaults (RFC 8628 §3.2)
DEVICE_FLOW_INTERVAL = 5           # seconds between poll attempts
DEVICE_FLOW_EXPIRES_IN = 900       # seconds until the device code expires

# Device flow defaults (RFC 8628 §3.2)
DEVICE_FLOW_INTERVAL = 5           # seconds between poll attempts
DEVICE_FLOW_EXPIRES_IN = 900       # seconds until the device code expires

# Token expiry
ACCESS_TOKEN_REFRESH_MARGIN = 300  # seconds before token expiry for refresh
ACCESS_TOKEN_EXPIRY_DEFAULT = 3600 # seconds default fallback


class OAuth2Handler(ABC):
    """
    Abstract base class for OAuth 2.0 authorization code flow with PKCE.

    Implements the Authorization Code Grant (RFC 6749, §4.1) extended with
    Proof Key for Code Exchange (RFC 7636) for public clients. Subclasses
    supply endpoints, client identity, and any flow-specific behavior;
    this class handles HTTP, token storage, PKCE math, and proactive token
    refresh.
    """

    # PKCE constants (RFC 7636)
    PKCE_VERIFIER_LENGTH: int = 64  # bytes of entropy for the code verifier
    STATE_LENGTH: int = 16          # bytes of entropy for the state parameter

    # Device flow state — per-instance, reset in __init__ (RFC 8628 §3.5)
    _use_device_flow: bool = False
    """Uses the device authorization grant (RFC 8628), not headless login."""
    _poll_interval: float = float(DEVICE_FLOW_INTERVAL)
    """Seconds between polls; increased by ``slow_down`` responses."""
    _next_poll_at: float = 0.0
    """Earliest epoch time at which the next poll is permitted."""


    def __init__(self, realm: str, client_id: str) -> None:
        """
        Initialize the handler, loading any previously stored tokens.

        :param realm:     Unique realm identifier used as a storage key prefix.
        :param client_id: OAuth2 client identifier for authorization requests.
        """

        self._access_token = ""
        self._access_token_expires_at = 0
        self._client_id = client_id
        self._next_poll_at = 0.0
        self._poll_interval = float(DEVICE_FLOW_INTERVAL)
        self._prefix = f"{realm}_oauth2_{client_id}_"
        self._realm = realm
        self._refresh_token = ""
        self._use_device_flow = False

        self._load_tokens()


    @property
    @abstractmethod
    def authorization_endpoint(self) -> str:
        """Authorization Endpoint URL (RFC 6749, §3.1)."""

        pass


    @property
    def device_authorization_endpoint(self) -> Optional[str]:
        """RFC 8628 Device Authorization Endpoint. ``None`` if unsupported."""

        return None


    @property
    def realm(self) -> str:
        """Realm identifier used as a storage key prefix."""

        return self._realm


    @property
    @abstractmethod
    def redirect_uri(self) -> str:
        """Redirect URI (RFC 6749, §3.1.2) for interactive authorization."""

        pass


    @property
    def scopes(self) -> List[str]:
        """Default OAuth2 scopes. Override in subclass for specific needs."""

        return ["openid", "profile", "email", "offline_access"]


    @property
    @abstractmethod
    def token_endpoint(self) -> str:
        """Token Endpoint URL (RFC 6749, §3.2) — code and token exchange."""

        pass


    # -- Token handling ----------------------------------------------------

    def _generate_pkce(self) -> Tuple[str, str]:
        """
        Generate a PKCE code verifier and S256 challenge (RFC 7636, §4.1–4.2).

        The verifier is URL-safe base64 (no padding), which uses only characters
        from the RFC 7636 §4.1 unreserved alphabet (``[A-Z] / [a-z] / [0-9] /
        "-" / "_"``).  The challenge is the SHA-256 digest of the verifier,
        base64url-encoded without padding, as required by §4.2.

        :return: ``(verifier, challenge)`` — URL-safe base64, no padding.
        """

        raw = secrets.token_bytes(self.PKCE_VERIFIER_LENGTH)
        verifier = base64.urlsafe_b64encode(raw).decode().rstrip('=')

        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).decode().rstrip('=')

        return verifier, challenge


    def _extract_auth_code(self, url: str, expected_state: str) -> Optional[str]:
        """
        Extract and validate the OAuth2 authorization code from a redirect URL.

        Verifies that both ``code`` and ``state`` are present and ``state``
        matches the value sent in the original authorization request (RFC 6749,
        §4.1.2 and §10.12).

        :param url:             The redirect URL to parse.
        :param expected_state:  State value from the authorization request.
        :return: The authorization code,
                 ``None`` on failure.
        """

        state_pattern = r'[?&]state=([^&\s]+)'
        state = re.search(state_pattern, url)
        if not state:
            Logger.warning("OAuth2: No state parameter in redirect URL")
            return None

        if state.group(1) != expected_state:
            Logger.warning(f"OAuth2: State mismatch, expected {expected_state!r}, got {state.group(1)!r}")
            return None

        code_pattern = r'[?&]code=([^&\s]+)'
        code = re.search(code_pattern, url)
        return code.group(1) if code else None


    def decode_token(self, token: str) -> Optional[dict]:
        """
        Decode a JWT token and return its claims payload.

        Does not verify the signature — for claim inspection only.

        :param token: A JWT token string.
        :return: Decoded claims dict,
                 ``None`` on failure.
        """

        try:
            return jwt.decode(token, options={"verify_signature": False})
        except Exception as e:
            Logger.error("OAuth2: Could not decode token claims", exc_info=True)
            return None


    # -- Token management --------------------------------------------------

    def _fetch_tokens(self, data: dict,
                      headers: Optional[dict] = None) -> Optional[dict]:
        """
        Make a token request and return the validated response without saving.

        Performs the HTTP POST, checks the response status, parses the JSON,
        and validates that ``access_token`` is present and is a structurally
        valid JWT.  Does **not** call :meth:`_save_tokens` — the caller
        inspects the returned dict and commits state when ready.

        When a non-``None`` dict is returned, ``tokens["access_token"]`` is
        guaranteed to exist and be a decodable JWT — callers may use it
        directly without further existence or structure checks.

        This is the correct primitive for callers that need to verify
        domain-specific claims before committing (e.g. a custom grant that
        must embed a ``profileId`` before the new token replaces the old one).

        :param data: The form data to post to the token endpoint.
        :param headers: Optional HTTP headers.
        :return: Parsed token response dict,
                 ``None`` on any failure.
        """

        headers = headers or {}
        headers["Accept"] = "application/json"
        response = UriHandler.open(self.token_endpoint,
                                   additional_headers=headers,
                                   data=data,
                                   no_cache=True,
        )
        if UriHandler.instance().status.error:
            status = UriHandler.instance().status
            Logger.error(f"OAuth2: Token request failed: {status.code} {status.reason} for {self.realm}")
            return None

        try:
            tokens = JsonHelper(response).json
        except Exception:
            Logger.error(f"OAuth2: Failed to parse token response for {self.realm}", exc_info=True)
            return None

        if "error" in tokens and tokens["error"]:
            error = tokens["error"]
            description = tokens["error_description"] if "error_description" in tokens else "no description"
            Logger.error(f"OAuth2: Token response for {self.realm} has error: {error} - {description}")
            return None

        if "access_token" not in tokens:
            Logger.error(f"OAuth2: Token response for {self.realm} missing access_token")
            return None

        if self.decode_token(tokens["access_token"]) is None:
            Logger.error(f"OAuth2: Token response for {self.realm} contains an invalid access token")
            return None

        return tokens


    def _request_token(self, data: dict, headers: Optional[dict] = None) -> bool:
        """
        Make a token request and save the result.

        Convenience wrapper around :meth:`_fetch_tokens` + :meth:`_save_tokens`
        for callers that do not need to inspect the response before committing.

        :param data: The form data to post to the token endpoint.
        :param headers: Optional HTTP headers.
        :return: True if tokens were successfully obtained and saved,
                 False on any failure.
        """

        tokens = self._fetch_tokens(data, headers)
        if tokens is None:
            return False

        self._save_tokens(tokens)
        return True


    def _exchange_code(self, code: str, verifier: str, redirect_uri: Optional[str] = None) -> bool:
        """
        Exchange an authorization code for tokens
        (RFC 6749, §4.1.3 / RFC 7636, §4.5).

        :param code:         The authorization code from the OAuth2 provider.
        :param verifier:     PKCE code verifier matching the earlier challenge.
        :param redirect_uri: Override (default: :attr:`redirect_uri`).
        :return: True if exchange successful,
                 False otherwise.
        """

        data = {
            "client_id": self._client_id,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri or self.redirect_uri,
        }
        return self._request_token(data)


    def _token_updated(self, token_name: Optional[str] = None) -> None:
        """
        Hook called after token state changes (load or save).
        Override in subclasses.

        :param token_name: Name of the token that changed,
                           ``None`` when all tokens changed.
        """

        pass


    def _load_tokens(self, token_name: Optional[str] = None) -> dict:
        """
        Load persisted token fields from settings.

        :param token_name: If given, load only that token; otherwise load all.
                           ``"access_token"`` also reloads
                           ``access_token_expires_at``.
        :returns: Dict of token fields that were loaded.
        """

        Logger.debug(f"OAuth2: loading tokens for {self.realm}")

        tokens: dict = {}

        if (token_name is None or
            token_name == "access_token"):
            self._access_token = (
                AddonSettings.get_setting(f"{self._prefix}access_token", store=LOCAL) or "")
            tokens["access_token"] = self._access_token

            self._access_token_expires_at = int(
                AddonSettings.get_setting(f"{self._prefix}access_token_expires_at", store=LOCAL) or 0)
            tokens["access_token_expires_at"] = self._access_token_expires_at

        if (token_name is None or
            token_name == "refresh_token"):
            self._refresh_token = (
                AddonSettings.get_setting(f"{self._prefix}refresh_token", store=LOCAL) or "")
            tokens["refresh_token"] = self._refresh_token

        self._token_updated(token_name)

        return tokens


    def _save_tokens(self, tokens: dict, token_name: Optional[str] = None) -> None:
        """
        Persist token fields to settings and update cached instance state.

        :param tokens: Token fields to persist; any combination of
                       ``"access_token"``, ``"refresh_token"``, and
                       ``"expires_in"`` is accepted.
        :param token_name: Primary token name.
        """

        Logger.debug(f"OAuth2: saving tokens for {self.realm}")

        if not tokens:
            Logger.warning(f"OAuth2: _save_tokens called without tokens for {self.realm}")

        if ("access_token" in tokens and
            (token_name is None or
             token_name == "access_token")):
            self._access_token = tokens["access_token"]
            AddonSettings.set_setting(f"{self._prefix}access_token", self._access_token, store=LOCAL)

            expires_in = tokens.get("expires_in", ACCESS_TOKEN_EXPIRY_DEFAULT)
            self._access_token_expires_at = int(time.time()) + expires_in
            AddonSettings.set_setting(f"{self._prefix}access_token_expires_at",
                                      str(self._access_token_expires_at), store=LOCAL)

        if ("refresh_token" in tokens and
            (token_name is None or
             token_name == "refresh_token")):
            self._refresh_token = tokens["refresh_token"]
            AddonSettings.set_setting(f"{self._prefix}refresh_token", self._refresh_token, store=LOCAL)

        self._token_updated(token_name)


    def _clear_tokens(self, token_name: Optional[str] = None) -> None:
        """Clear stored tokens and reset persisted settings.

        :param token_name: ``"access_token"`` or ``"refresh_token"`` to clear.
                           Clears all tokens when omitted.
        """

        Logger.debug(f"OAuth2: Clearing tokens for {self.realm}")

        if (token_name is None or
            token_name == "access_token"):
            self._access_token = ""
            self._access_token_expires_at = 0
            AddonSettings.set_setting(f"{self._prefix}access_token", "", store=LOCAL)
            AddonSettings.set_setting(f"{self._prefix}access_token_expires_at", "0", store=LOCAL)

        if (token_name is None or
            token_name == "refresh_token"):
            self._refresh_token = ""
            AddonSettings.set_setting(f"{self._prefix}refresh_token", "", store=LOCAL)

        self._token_updated(token_name)


    def _refresh_token_grant(self) -> bool:
        """
        Unconditional access token refresh using the stored refresh token.

        Performs a standard ``refresh_token`` grant (RFC 6749, §6).
        Subclasses may extend this for flow-specific post-refresh behavior.

        :returns: True when a refresh token was retrieved,
                  False when no refresh token is available or the request fails.
        """

        if not self._refresh_token:
            Logger.warning(f"OAuth2: No refresh token available for {self.realm}")
            return False

        Logger.debug(f"OAuth2: Refreshing access token for {self.realm}")

        data = {
            "client_id": self._client_id,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
        }
        return self._request_token(data)


    def refresh_access_token(self) -> Optional[str]:
        """
        Refresh the access token if within
        :attr:`ACCESS_TOKEN_REFRESH_MARGIN` seconds of expiry.

        Intended to be called on every heartbeat cycle.  The method is a no-op
        when the token is still comfortably valid, so calling it frequently is
        cheap (one in-memory comparison).

        :return: Current access token after refresh,
                 ``None`` on failure.
        """

        if time.time() < (self._access_token_expires_at - ACCESS_TOKEN_REFRESH_MARGIN):
            Logger.trace(f"OAuth2: Token still valid for {self.realm} "
                         f"({self._access_token_expires_at - time.time():.0f}s remaining)")
            return self._access_token or None

        if self._refresh_token_grant():
            return self._access_token or None

        return None


    def _get_access_token(self) -> Optional[str]:
        """Return the stored access token without triggering a refresh."""

        return self._access_token or None


    def _authorization_bearer(self, token: Optional[str] = None) -> dict:
        """
        Return an ``Authorization: Bearer`` header dict (RFC 6750, §2.1).

        Returns an empty dict when no token is available — callers can safely
        merge the result without checking first.

        :param token: Access token to use. Defaults to the stored access token.
        :return: ``{"Authorization": "Bearer <token>"}``,
                 ``{}`` on error.
        """

        bearer_token = token or self._access_token or None
        if bearer_token:
            return {"Authorization": f"Bearer {bearer_token}"}

        return {}


    # -- Device authorization ----------------------------------------------

    def _device_access_token_granted(self, tokens: dict) -> None:
        """
        Called after a successful device flow token exchange (RFC 8628, §3.4).

        Override in subclasses to perform post-authentication setup
        (e.g. storing a client ID or device session key from the token claims).

        :param tokens: The parsed token response dict from the server.
        """

        pass


    def _device_request_headers(self) -> dict:
        """
        Return HTTP headers for device flow requests (RFC 8628, §3.4).

        Override in subclasses to supply custom headers (e.g. client identity,
        User-Agent). The return value replaces the default empty dict entirely.
        """

        return {}


    def _device_access_token_request(self, device_code: str) -> str:
        """
        Perform a single device authorization token request (RFC 8628, §3.4).

        Calls :meth:`_device_request_headers` for extra request headers and
        :meth:`_device_access_token_granted` after a successful token exchange.
        Override those hooks instead of this method.

        :param device_code: The device code from :meth:`_device_authorization_request`.
        :return: ``'success'``,
                 ``'authorization_pending'``,
                 ``'slow_down'``,
                 ``'error'``,
                 ```'unknown_response'``,
                 other errors from endpoint.
        """

        data = {
            "client_id": self._client_id,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }
        poll_headers = self._device_request_headers()
        poll_headers["Accept"] = "application/json"
        response = UriHandler.open(
            self.token_endpoint,
            additional_headers=poll_headers,
            data=data,
            no_cache=True,
        )
        if UriHandler.instance().status.error:
            Logger.warning(f"OAuth2: Device access token request failed: {UriHandler.instance().status.code}")
            return "error"

        try:
            tokens = JsonHelper(response).json
        except Exception:
            Logger.warning("OAuth2: Failed to parse device access token response", exc_info=True)
            return "error"

        if ("error" in tokens and
            tokens["error"]):
            error = tokens["error"]
            if error not in ("authorization_pending", "slow_down"):
                Logger.warning(f"OAuth2: Device flow error: {error}")
            return error

        if ("access_token" in tokens and
            tokens["access_token"]):
            self._save_tokens(tokens)
            self._device_access_token_granted(tokens)
            return "success"

        return "unknown_response"


    def poll_device_authorization(self, device_code: str) -> str:
        """
        Non-blocking single tick of the device flow poll loop (RFC 8628, §3.5).

        Manages the polling interval — returns ``"pending"`` immediately if
        the interval has not elapsed; on ``slow_down``, increases the interval
        by :data:`DEVICE_FLOW_INTERVAL` seconds as required by RFC 8628, §3.5.

        Suitable for callers driving their own event loop (e.g. a UI progress
        dialog) and need to remain responsive between poll attempts.

        :param device_code: Device code from :meth:`_device_authorization_request`.
        :return: ``"success"``,
                 ``"pending"``,
                 ``"error"``.
        """

        if time.time() < self._next_poll_at:
            return "pending"

        result = self._device_access_token_request(device_code)
        if result == "success":
            return "success"

        if result == "slow_down":
            self._poll_interval += DEVICE_FLOW_INTERVAL

        if result in ("slow_down", "authorization_pending"):
            self._next_poll_at = time.time() + self._poll_interval
            return "pending"

        return "error"


    def _device_authorization_request(self, scope: Optional[List[str]] = None,
                          additional_headers: Optional[dict] = None) -> Optional[dict]:
        """
        Send a Device Authorization Request (RFC 8628, §3.1–3.2).

        :param scope:               Scopes to request (defaults to
                                    :attr:`scopes` + ``offline_access``).
        :param additional_headers:  Optional extra HTTP headers.
        :return: Response dict (``device_code``, ``user_code``,
                 ``verification_uri``, ``expires_in``, ``interval``, …)
                 as defined by RFC 8628, §3.2,
                 None on error.
        """

        Logger.info(f"OAuth2: Starting device flow for {self.realm}")

        if not self.device_authorization_endpoint:
            Logger.error(f"OAuth2: Device authorization endpoint not configured for {self.realm}")
            return None

        additional_headers = additional_headers or {}
        additional_headers["Accept"] = "application/json"
        device_scopes = scope or (self.scopes + ["offline_access"])
        data = {
            "client_id": self._client_id,
            "scope": " ".join(device_scopes),
        }
        response = UriHandler.open(
            self.device_authorization_endpoint,
            additional_headers=additional_headers,
            data=data,
            no_cache=True,
        )

        if UriHandler.instance().status.error:
            Logger.error(f"OAuth2: Device authorization request failed for {self.realm}")
            return None

        try:
            result = JsonHelper(response).json
        except Exception:
            Logger.error("OAuth2: Failed to parse device authorization response", exc_info=True)
            return None

        self._poll_interval = max(float(result.get("interval") or DEVICE_FLOW_INTERVAL), 1.0)
        self._next_poll_at = time.time() + self._poll_interval

        return result
