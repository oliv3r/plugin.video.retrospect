# SPDX-License-Identifier: GPL-3.0-or-later

import json
import secrets
import time
from typing import Optional
from urllib.parse import urlencode

from http import HTTPStatus

from resources.lib.addonsettings import AddonSettings, LOCAL
from resources.lib.authentication.oauth2handler import OAuth2Handler
from resources.lib.helpers.jsonhelper import JsonHelper
from resources.lib.logger import Logger
from resources.lib.urihandler import UriHandler

ID_TOKEN_REFRESH_MARGIN = 300  # seconds before id_token expiry to trigger silent reauth


class OIDCHandler(OAuth2Handler):
    """
    OAuth2 handler extended with OpenID Connect (OIDC) support.

    Adds ``id_token`` persistence and the standard OIDC UserInfo endpoint
    (OpenID Connect Core 1.0, §5.3) on top of the base RFC 6749 token
    handling. Subclasses provide the ``userinfo_endpoint`` URL and any
    provider-specific behavior.
    """

    _id_token: str = ""
    """OpenID Connect Core 1.0, §2 ID token."""

    _claims_request: Optional[dict] = None
    """Pending OIDC claims request parameter (OpenID Connect Core 1.0, §5.5)."""

    def __init__(self, realm: str, client_id: str, userinfo_endpoint: str,
                 headers: Optional[dict] = None) -> None:
        """
        :param realm:              Realm name used as a settings key prefix.
        :param client_id:          OAuth2 client identifier.
        :param userinfo_endpoint:  OIDC UserInfo Endpoint URL.
        :param headers:            Initial set of headers for authenticated requests.
        """

        self._claims_request = None
        self._id_token = ""
        self._userinfo_endpoint = userinfo_endpoint

        super().__init__(realm, client_id, headers=headers)


    def _token_exchange_request(self, data: dict,
                               headers: Optional[dict] = None) -> Optional[dict]:
        """
        Perform a Token Exchange (RFC 8693) and return the claims from the result.

        Fetches tokens via the token endpoint, saves the result, and decodes
        the new access token to return its JWT claims.  The caller may inspect
        the claims (e.g. to verify a domain-specific claim was embedded) before
        proceeding.

        Because :meth:`_fetch_tokens` guarantees ``access_token`` is a valid
        JWT, the returned dict is always decodable — callers need not guard
        against ``None`` from :meth:`decode_token`.

        :param data:    Form data for the token endpoint POST.
        :param headers: Optional HTTP headers (e.g. ``Authorization: Bearer``
                        for grant types that require the current token).

        :return: - JWT claims dict from the new access token.
                 - ``None`` on any failure (HTTP error, missing token, invalid JWT).
        """

        tokens = self._fetch_tokens(data, headers)
        if tokens is None:
            return None

        self._save_tokens(tokens)

        return self.decode_token(tokens["access_token"])


    def _get_username_from_access_token(self, access_token: str) -> str:
        """
        Extract a human-readable username from an OIDC JWT access token.

        Tries the following standard OIDC claims in priority order, preferring
        human-readable identifiers over the opaque subject identifier
        (OpenID Connect Core 1.0, §5.1):

        1. ``email``              — user's e-mail address
        2. ``preferred_username`` — login name chosen by the user
        3. ``nickname``           — casual display name
        4. ``sub``                — subject identifier (opaque, but always
                                    present per OpenID Connect Core 1.0 §2 /
                                    RFC 7519 §4.1.2)

        :param access_token: The OIDC JWT access token string (RFC 9068).

        :return: - A human-readable username string.
                 - ``""`` if unavailable.
        """

        if not access_token:
            return ""

        claims = self.decode_token(access_token) or {}
        return (claims.get("email") or
                claims.get("preferred_username") or
                claims.get("nickname") or
                claims.get("sub") or
                "")


    def _set_claims(self, id_token_claims: Optional[dict] = None,
                    userinfo_claims: Optional[dict] = None) -> Optional[dict]:
        """
        Set the OIDC claims request parameter (OpenID Connect Core 1.0, §5.5).

        Claims are included as the ``claims`` parameter in subsequent
        authorization requests, persisting across token refreshes and silent
        re-authentication until cleared by :meth:`_clear_tokens`.

        :param id_token_claims: Claims to embed in the ID Token.  Each key
                                maps to a request object with optional
                                ``essential`` and/or ``value`` fields, or
                                ``None`` for a voluntary claim.
        :param userinfo_claims: Claims to include in the UserInfo response.
                                Same structure as ``id_token_claims``.

        :return: - The stored claims dict.
                 - ``None`` if no claims were set.
        """

        claims: dict = {}

        if id_token_claims:
            claims["id_token"] = id_token_claims

        if userinfo_claims:
            claims["userinfo"] = userinfo_claims

        self._claims_request = claims or None
        return self._claims_request


    def _get_claims_param(self) -> Optional[str]:
        """
        Return the JSON-serialized OIDC claims request parameter, or None.

        Per OpenID Connect Core 1.0, §5.5, this string is passed as the
        ``claims`` query parameter in the authorization request so the server
        knows which claims to embed in the returned tokens.

        :return: - Compact JSON string for the ``claims`` parameter.
                 - ``None`` if no claims have been set via :meth:`_set_claims`.
        """

        if not self._claims_request:
            return None

        try:
            return json.dumps(self._claims_request, separators=(",", ":"))
        except (TypeError, ValueError):
            Logger.error(f"OIDC: Failed to serialise claims request for {self.realm}", exc_info=True)
            return None


    def _redirect_uri_silent(self) -> str:
        """
        Redirect URI for silent authentication callbacks (OIDC ``prompt=none`` flow).

        Override in subclass when the identity server requires a dedicated silent
        endpoint, separate from the interactive login redirect.

        :return: The redirect URI string.
        """

        return self.redirect_uri


    def _token_updated(self, token_name: Optional[str] = None) -> None:
        """
        Update ``_access_token_expires_at`` from the JWT ``exp`` claim.

        When the access token is a signed JWT, the ``exp`` claim (RFC 7519,
        §4.1.4) gives the authoritative expiry as an absolute Unix timestamp,
        more accurate than ``now + expires_in``. Mandated by RFC 9068, §2.2.
        When the token is absent the expiry is reset to zero.

        :param token_name: Name of the token that changed, or None when all changed.
        """

        if (token_name is None or
            token_name == "access_token"):
            if self._access_token:
                claims = self.decode_token(self._access_token)
                if claims and "exp" in claims:
                    self._access_token_expires_at = claims["exp"]
                    Logger.debug(f"OIDC: access_token expiry set from JWT exp for {self.realm}: {self._access_token_expires_at}")
            else:
                self._access_token_expires_at = 0

        super()._token_updated(token_name)


    def _load_tokens(self, token_name: Optional[str] = None) -> dict:
        """
        Load standard tokens and also restore the ``id_token``.

        Note: :meth:`_token_updated` fires inside the base-class call before
                    ``id_token`` is restored. Subclass hooks cannot rely on
                    ``self._id_token`` being current.

        :param token_name: Token to load; None loads all.

        :return: Dict of token fields that were loaded (includes ``id_token``).
        """

        tokens = super()._load_tokens(token_name)

        if (token_name is None or
            token_name == "id_token"):
            self._id_token = AddonSettings.get_setting(self._token_key("id_token"), store=LOCAL) or ""
            tokens["id_token"] = self._id_token
            Logger.debug(f"OIDC: Loaded id_token for {self.realm}: {'present' if self._id_token else 'absent'}")

        return tokens


    def _save_tokens(self, tokens: dict, token_name: Optional[str] = None) -> None:
        """
        Persist standard tokens and also store the ``id_token`` when present.

        :param tokens: Token fields to persist.
        :param token_name: Primary token name, forwarded to the update hook.
        """

        if ("id_token" in tokens and
            (token_name is None or
             token_name == "id_token")):
            self._id_token = tokens["id_token"]
            AddonSettings.set_setting(self._token_key("id_token"), self._id_token, store=LOCAL)
            Logger.debug(f"OIDC: Stored id_token for {self.realm}")

        super()._save_tokens(tokens, token_name)


    def _end_session(self, client_id: str, revocation_endpoint: str,
                          end_session_endpoint: str, redirect_uri: str) -> bool:
        """
        Revoke the access token and end the OIDC server session.

        Performs two calls against the identity server while tokens are still
        available:

        1. ``POST <revocation_endpoint>`` — invalidates the access token
           (RFC 7009).
        2. ``GET <end_session_endpoint>?id_token_hint=…`` — ends the
           server-side OIDC session so silent re-authentication no longer
           succeeds (OpenID Connect Session Management 1.0).

        :param client_id:               The OAuth2 client ID to include in the
                                        revocation request.
        :param revocation_endpoint:     URL of the token revocation endpoint.
        :param end_session_endpoint:    URL of the OIDC end-session endpoint.
        :param redirect_uri:            Post-logout redirect URI passed as hint.

        :returns: ``True`` if both server calls succeeded, ``False`` otherwise.
        """

        success = True

        if self._access_token:
            UriHandler.open(
                revocation_endpoint,
                additional_headers={"Content-Type": "application/x-www-form-urlencoded"},
                data=urlencode({
                    "token": self._access_token,
                    "token_type_hint": "access_token",
                    "client_id": client_id,
                }),
                no_cache=True,
            )
            if UriHandler.instance().status.error:
                Logger.warning(f"OIDC: Token revocation failed for {self.realm}")
                success = False
            else:
                Logger.debug(f"OIDC: Access token revoked for {self.realm}")

        if self._id_token:
            params = urlencode({
                "id_token_hint": self._id_token,
                "post_logout_redirect_uri": redirect_uri,
            })
            UriHandler.open(f"{end_session_endpoint}?{params}", no_cache=True)
            if UriHandler.instance().status.error:
                Logger.warning(f"OIDC: End session failed for {self.realm}")
                success = False
            else:
                Logger.debug(f"OIDC: End session called for {self.realm}")

        return success


    def _clear_tokens(self, token_name: Optional[str] = None) -> None:
        """
        Clear all tokens including the ``id_token``.

        :param token_name: Token to clear; None clears all.
        """

        if (token_name is None or
            token_name == "id_token"):
            self._id_token = ""
            AddonSettings.set_setting(self._token_key("id_token"), "", store=LOCAL)
            Logger.debug(f"OIDC: Cleared id_token for {self.realm}")

        if token_name is None:
            self._claims_request = None

        super()._clear_tokens(token_name)


    def _silent_authentication_request(self, client_id: str,
                                       redirect_uri: str,
                                       scope: str) -> Optional[dict]:
        """
        Run a silent PKCE authorization-code flow; return the token response.

        Uses ``prompt=none`` with an optional ``id_token_hint`` when available.
        Does not save tokens — the caller decides what to do with the result.

        :param client_id:    OAuth2 client identifier for this flow.
        :param redirect_uri: Redirect URI registered for this client.
        :param scope:        Space-separated scope string to request.

        :return: - Raw token response dict from the token endpoint.
                 - ``None`` if the authorization code could not be obtained.
        """

        grant_headers = self._token_request_headers()
        grant_headers.update(self._authorization_bearer())

        code_verifier, code_challenge = self._generate_pkce()
        state = secrets.token_urlsafe(self.STATE_LENGTH)
        auth_params = {
            "client_id": client_id,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "prompt": "none",
            "redirect_uri": redirect_uri,
            "response_mode": "query",
            "response_type": "code",
            "scope": scope,
            "state": state,
        }

        if self._id_token:
            auth_params["id_token_hint"] = self._id_token

        claims_param = self._get_claims_param()
        if claims_param:
            auth_params["claims"] = claims_param

        auth_url = f"{self.authorization_endpoint}?{urlencode(auth_params)}"

        UriHandler.open(auth_url,
                        additional_headers=grant_headers,
                        no_cache=True,
        )
        status = UriHandler.instance().status
        if status.error:
            Logger.warning(f"OIDC: Silent auth request failed: {status.code} {status.reason}")
            return None

        auth_code = self._extract_auth_code(status.url, state)
        if auth_code is None:
            Logger.warning(f"OIDC: Silent auth failed — no authorization code in redirect: {status.url}")
            return None

        token_data = {
            "client_id": client_id,
            "code": auth_code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        grant_headers["Accept"] = "application/json"

        response = UriHandler.open(self.token_endpoint,
                                   additional_headers=grant_headers,
                                   data=token_data,
                                   no_cache=True,
        )
        status = UriHandler.instance().status
        if status.error:
            Logger.error(f"OIDC: Token endpoint request failed: {status.code} {status.reason}")
            return None

        try:
            tokens = JsonHelper(response).json
        except (ValueError, TypeError):
            Logger.warning("OIDC: Token endpoint returned invalid JSON")
            return None

        if not isinstance(tokens, dict):
            Logger.warning("OIDC: Token endpoint returned non-dict response")
            return None

        return tokens


    def _silent_authentication(self) -> bool:
        """
        Obtain fresh tokens via a silent PKCE authorization-code flow.

        Uses ``prompt=none`` with ``id_token_hint`` (OpenID Connect Core 1.0,
        §3.1.2.1) so the identity server can re-authenticate the user without
        an interactive prompt.  The redirect URI used is
        :meth:`~oauth2handler.OAuth2Handler._redirect_uri_silent`, which may
        differ from the interactive login redirect.

        Returns False when no ``id_token`` hint is available or the server
        request fails.  Callers are responsible for clearing stale tokens
        as appropriate for their context.
        """

        if not self._id_token:
            Logger.warning("OIDC: No id_token available for silent re-authentication")
            return False

        Logger.debug(f"OIDC: Attempting silent re-authentication for {self.realm}")
        tokens = self._silent_authentication_request(
            self._client_id,
            self._redirect_uri_silent(),
            " ".join(self.scopes),
        )
        if tokens is not None:
            Logger.debug("OIDC: Silent auth successful, saving tokens")
            self._save_tokens(tokens)
            return True

        Logger.warning("OIDC: Silent re-authentication failed — no tokens returned")
        return False


    def _refresh_token_grant(self) -> bool:
        """
        Extend the base refresh to keep the ``id_token`` current
        (OpenID Connect Core 1.0, §12.2).

        Skipped for device flow handlers, which do not support silent re-auth.
        For headless login, checks whether the ``id_token`` is near expiry or
        absent and calls :meth:`_silent_authentication` if so.
        """

        result = super()._refresh_token_grant()
        if (result and
            not self._device_flow):
            if self._id_token:
                exp = (self.decode_token(self._id_token) or {}).get("exp")
                id_token_expiry = int(exp) if isinstance(exp, (int, float)) else 0
            else:
                id_token_expiry = 0

            if time.time() >= (id_token_expiry - ID_TOKEN_REFRESH_MARGIN):
                Logger.debug("OIDC: id_token near expiry or absent — triggering silent re-auth")
                if not self._silent_authentication():
                    Logger.warning("OIDC: Silent re-auth failed; id_token not renewed "
                                   "but access token is valid — clearing stale id_token")
                    self._clear_tokens("id_token")

        return result


    def get_user_info(self) -> dict:
        """Retrieve the UserInfo Claims for the authenticated End-User.

        Calls the UserInfo Endpoint using the current access token as a Bearer
        credential. The response is a JSON Claims Set as defined by
        OpenID Connect Core 1.0, §5.3. The ``sub`` Claim is always present;
        standard claims (``name``, ``email``, etc.) are provider-dependent.

        :return: Claims dict on success.
        :raises PermissionError: HTTP 401/403 — token rejected by the server;
                                 re-authentication is needed.
        :raises RuntimeError:    HTTP 5xx or an unexpected (non-dict) response;
                                 the server failed to fulfil a valid request.
        :raises IOError:         Transport failure (DNS, timeout, refused).

        .. note::
            ``PermissionError`` is a subclass of ``IOError``; catch it
            **first**, or 401/403 responses are misclassified.
        """

        Logger.debug(f"OIDC: Requesting user info from {self._userinfo_endpoint}")
        headers = self._authorization_bearer()
        headers["Accept"] = "application/json"
        response = UriHandler.open(
            self._userinfo_endpoint,
            additional_headers=headers,
            no_cache=True,
        )

        status = UriHandler.instance().status
        if status.error:
            if status.code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
                Logger.warning(f"OIDC: UserInfo rejected token: {status.code} {status.reason}")
                raise PermissionError(f"HTTP {status.code}")
            Logger.error(f"OIDC: UserInfo request failed: {status.code} {status.reason}")
            raise RuntimeError(f"HTTP {status.code}")

        try:
            userinfo = JsonHelper(response).json
        except (ValueError, TypeError) as json_exception:
            raise RuntimeError("UserInfo response is not valid JSON") from json_exception

        if not isinstance(userinfo, dict):
            raise RuntimeError("Unexpected non-dict response from UserInfo endpoint")

        Logger.debug(f"OIDC: User info retrieved for: {userinfo.get('email', 'N/A')}")
        return userinfo
