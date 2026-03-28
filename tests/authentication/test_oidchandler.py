# SPDX-License-Identifier: GPL-3.0-or-later

import unittest
import unittest.mock
from typing import Any, Optional

from resources.lib.authentication.authenticationresult import AuthenticationResult
from resources.lib.authentication.oidchandler import OIDCHandler


# ---------------------------------------------------------------------------
# Minimal concrete subclass — OIDCHandler is still abstract via OAuth2Handler
# ---------------------------------------------------------------------------

class _StubOIDCHandler(OIDCHandler):
    authorization_endpoint = "https://auth.example.com/authorize"
    token_endpoint = "https://auth.example.com/token"
    redirect_uri = "https://localhost/callback"
    device_authorization_endpoint = "https://auth.example.com/device"

    def log_on(self, username: Optional[str] = None, password: Optional[str] = None, **_: Any) -> AuthenticationResult:  # type: ignore[override]
        return AuthenticationResult("", error="stub")


def _make_oidc_handler(**overrides: Any) -> "_StubOIDCHandler":
    """Return a freshly constructed _StubOIDCHandler with no real settings I/O."""
    with unittest.mock.patch(
        "resources.lib.addonsettings.AddonSettings.get_setting", return_value=None
    ):
        h = _StubOIDCHandler(
            "testoidcrealm", "test_client", "https://auth.example.com/userinfo")
    for k, v in overrides.items():
        setattr(h, k, v)
    return h


def _make_jwt(exp: int) -> str:
    """Return a minimal unsigned JWT with the given ``exp`` claim."""
    import base64
    import json as _json
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(_json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"{header}.{body}."


class TestOIDCHandlerInit(unittest.TestCase):
    """Verify id_token and userinfo_endpoint are initialized correctly."""

    def test_id_token_defaults_to_empty_string(self) -> None:
        """id_token starts empty when no cached token exists."""

        handler = _make_oidc_handler()
        self.assertEqual(handler._id_token, "")

    def test_userinfo_endpoint_stored(self) -> None:
        """Constructor stores the userinfo_endpoint for use by get_user_info()."""

        handler = _make_oidc_handler()
        self.assertEqual(handler._userinfo_endpoint, "https://auth.example.com/userinfo")


class TestOIDCHandlerSaveTokens(unittest.TestCase):
    """Tests for id_token persistence in _save_tokens()."""

    def setUp(self) -> None:
        self.handler = _make_oidc_handler()

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time")
    def test_save_tokens_stores_id_token(
            self,
            mock_time: unittest.mock.MagicMock,
            _mock_set: unittest.mock.MagicMock) -> None:
        """_save_tokens() persists id_token when present in the token dict."""

        mock_time.time.return_value = 0

        self.handler._save_tokens({
            "access_token": "acc",
            "expires_in": 3600,
            "id_token": "my_id_token",
        })

        self.assertEqual(self.handler._id_token, "my_id_token")

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time")
    def test_save_tokens_without_id_token_preserves_existing(
            self,
            mock_time: unittest.mock.MagicMock,
            _mock_set: unittest.mock.MagicMock) -> None:
        """_save_tokens() leaves _id_token unchanged when not in the token dict."""

        mock_time.time.return_value = 0
        self.handler._id_token = "existing"

        self.handler._save_tokens({"access_token": "acc", "expires_in": 3600})

        self.assertEqual(self.handler._id_token, "existing")

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time")
    def test_save_tokens_ignores_id_token_when_token_name_is_refresh_token(
            self,
            mock_time: unittest.mock.MagicMock,
            _mock_set: unittest.mock.MagicMock) -> None:
        """_save_tokens() does not update id_token when token_name targets a different token.

        Even if the dict contains an id_token key, it must not be persisted when
        token_name is set to a different field (e.g. "refresh_token").
        """

        mock_time.time.return_value = 0
        self.handler._id_token = "old_id"

        self.handler._save_tokens(
            {"refresh_token": "new_rt", "id_token": "sneaky"},
            token_name="refresh_token",
        )

        self.assertEqual(self.handler._id_token, "old_id")


class TestOIDCHandlerLoadTokens(unittest.TestCase):
    """Tests for id_token loading in _load_tokens()."""

    def setUp(self) -> None:
        self.handler = _make_oidc_handler()

    def _settings_map(self, **extra: str):  # type: ignore[no-untyped-def]
        prefix = self.handler._prefix

        def _get(key: str, **_kw: object) -> str:
            return extra.get(key.replace(prefix, "", 1), "")

        return _get

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.get_setting")
    def test_load_all_also_loads_id_token(self, mock_get: unittest.mock.MagicMock) -> None:
        """_load_tokens() with no arg loads id_token alongside standard fields."""

        mock_get.side_effect = self._settings_map(
            access_token="acc", access_token_expires_at="1000",
            refresh_token="ref", id_token="id_t")

        self.handler._load_tokens()

        self.assertEqual(self.handler._id_token, "id_t")

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.get_setting")
    def test_load_all_returns_dict_including_id_token(
            self, mock_get: unittest.mock.MagicMock) -> None:
        """_load_tokens() return value includes id_token."""

        mock_get.side_effect = self._settings_map(
            access_token="acc", access_token_expires_at="1000",
            refresh_token="ref", id_token="id_t")

        result = self.handler._load_tokens()

        self.assertIn("id_token", result)
        self.assertEqual(result["id_token"], "id_t")

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.get_setting")
    def test_load_access_token_does_not_touch_id_token(
            self, mock_get: unittest.mock.MagicMock) -> None:
        """_load_tokens('access_token') leaves id_token unchanged."""

        self.handler._id_token = "preserved"
        mock_get.side_effect = self._settings_map(access_token="new_acc", access_token_expires_at="2000")

        self.handler._load_tokens("access_token")

        self.assertEqual(self.handler._id_token, "preserved")

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.get_setting")
    def test_load_id_token_returns_dict_with_only_id_token(
            self, mock_get: unittest.mock.MagicMock) -> None:
        """_load_tokens('id_token') returns only the id_token key."""

        mock_get.side_effect = self._settings_map(id_token="my_id")

        result = self.handler._load_tokens("id_token")

        self.assertEqual(result, {"id_token": "my_id"})

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value="")
    def test_load_unknown_key_returns_empty_dict(self, _mock_get: unittest.mock.MagicMock) -> None:
        """_load_tokens() with an unrecognised token_name returns an empty dict."""

        result = self.handler._load_tokens("bogus_token")

        self.assertEqual(result, {})


class TestOIDCHandlerClearTokens(unittest.TestCase):
    """Tests for id_token cleanup in _clear_tokens()."""

    def setUp(self) -> None:
        self.handler = _make_oidc_handler()

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_clear_tokens_wipes_id_token(self, _mock_set: unittest.mock.MagicMock) -> None:
        """_clear_tokens() resets id_token to empty string."""

        self.handler._id_token = "some_id_token"

        self.handler._clear_tokens()

        self.assertEqual(self.handler._id_token, "")

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_clear_tokens_with_token_name_preserves_id_token(self, _mock_set: unittest.mock.MagicMock) -> None:
        """_clear_tokens('access_token') does not wipe id_token."""

        self.handler._id_token = "some_id_token"

        self.handler._clear_tokens(token_name="access_token")

        self.assertEqual(self.handler._id_token, "some_id_token")

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_clear_tokens_refresh_token_preserves_id_token(self, _mock_set: unittest.mock.MagicMock) -> None:
        """_clear_tokens('refresh_token') does not wipe id_token."""

        self.handler._id_token = "some_id_token"

        self.handler._clear_tokens(token_name="refresh_token")

        self.assertEqual(self.handler._id_token, "some_id_token")

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_clear_tokens_with_id_token_name_wipes_only_id_token(self, _mock_set: unittest.mock.MagicMock) -> None:
        """_clear_tokens('id_token') clears id_token but preserves access and refresh tokens."""

        self.handler._id_token = "some_id_token"
        self.handler._access_token = "some_access_token"
        self.handler._refresh_token = "some_refresh_token"

        self.handler._clear_tokens(token_name="id_token")

        self.assertEqual(self.handler._id_token, "")
        self.assertEqual(self.handler._access_token, "some_access_token")
        self.assertEqual(self.handler._refresh_token, "some_refresh_token")

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_clear_tokens_id_token_passes_name_to_hook(self, _mock_set: unittest.mock.MagicMock) -> None:
        """_clear_tokens('id_token') forwards 'id_token' to _token_updated()."""

        calls = []
        self.handler._token_updated = lambda token_name=None: calls.append(token_name)  # type: ignore[method-assign]

        self.handler._clear_tokens("id_token")

        self.assertEqual(calls, ["id_token"])


class TestOIDCHandlerGetUserInfo(unittest.TestCase):
    """Tests for the get_user_info() OIDC UserInfo endpoint."""

    def setUp(self) -> None:
        self.handler = _make_oidc_handler()
        self.handler._access_token = "test_token"

    def test_get_user_info_raises_permission_error_on_401(self) -> None:
        """get_user_info() raises PermissionError on HTTP 401 (token rejected)."""

        with unittest.mock.patch("resources.lib.urihandler.UriHandler.open",
                                 return_value='{"error":"invalid_token"}'), \
             unittest.mock.patch("resources.lib.urihandler.UriHandler.instance") as mock_instance:
            mock_instance.return_value.status.error = True
            mock_instance.return_value.status.code = 401

            with self.assertRaises(PermissionError):
                self.handler.get_user_info()

    def test_get_user_info_raises_permission_error_on_403(self) -> None:
        """get_user_info() raises PermissionError on HTTP 403 (insufficient scope)."""

        with unittest.mock.patch("resources.lib.urihandler.UriHandler.open",
                                 return_value='{"error":"insufficient_scope"}'), \
             unittest.mock.patch("resources.lib.urihandler.UriHandler.instance") as mock_instance:
            mock_instance.return_value.status.error = True
            mock_instance.return_value.status.code = 403

            with self.assertRaises(PermissionError):
                self.handler.get_user_info()

    def test_get_user_info_raises_runtime_error_on_5xx(self) -> None:
        """get_user_info() raises RuntimeError on HTTP 5xx."""

        with unittest.mock.patch("resources.lib.urihandler.UriHandler.open",
                                 return_value=""), \
             unittest.mock.patch("resources.lib.urihandler.UriHandler.instance") as mock_instance:
            mock_instance.return_value.status.error = True
            mock_instance.return_value.status.code = 503

            with self.assertRaises(RuntimeError):
                self.handler.get_user_info()

    @unittest.mock.patch("resources.lib.urihandler.UriHandler.open")
    def test_get_user_info_returns_dict_on_success(
            self, mock_open: unittest.mock.MagicMock) -> None:
        """get_user_info() returns the parsed userinfo dict on success."""

        mock_open.return_value = '{"email": "test@example.com", "name": "Test User"}'
        with unittest.mock.patch("resources.lib.urihandler.UriHandler.instance") as mock_instance:
            mock_instance.return_value.status.error = False

            result = self.handler.get_user_info()

        self.assertEqual(result.get("email"), "test@example.com")

    @unittest.mock.patch("resources.lib.urihandler.UriHandler.open")
    def test_get_user_info_uses_bearer_token_header(
            self, mock_open: unittest.mock.MagicMock) -> None:
        """get_user_info() passes Authorization: Bearer <access_token> to the request."""

        mock_open.return_value = '{"email": "x"}'
        with unittest.mock.patch("resources.lib.urihandler.UriHandler.instance") as mock_instance:
            mock_instance.return_value.status.error = False
            self.handler.get_user_info()

        _, kwargs = mock_open.call_args
        self.assertEqual(
            kwargs.get("additional_headers", {}).get("Authorization"),
            "Bearer test_token")

    @unittest.mock.patch("resources.lib.urihandler.UriHandler.open")
    def test_get_user_info_propagates_ioerror_on_transport_failure(
            self, mock_open: unittest.mock.MagicMock) -> None:
        """get_user_info() lets IOError propagate unmodified for transport-level failures."""

        original = IOError("connection refused")
        mock_open.side_effect = original
        with self.assertRaises(IOError) as ctx:
            self.handler.get_user_info()
        self.assertIs(ctx.exception, original)


class TestOIDCHandlerOnTokensUpdate(unittest.TestCase):
    """Tests for _token_updated() JWT exp extraction (RFC 9068, §2.2)."""

    def setUp(self) -> None:
        self.handler = _make_oidc_handler()

    def _make_jwt(self, exp: int) -> str:
        return _make_jwt(exp)

    def test_exp_extracted_from_jwt_access_token(self) -> None:
        """_token_updated() sets _access_token_expires_at from the JWT exp claim."""

        self.handler._access_token = self._make_jwt(9999888777)
        self.handler._token_updated("access_token")

        self.assertEqual(self.handler._access_token_expires_at, 9999888777)

    def test_expires_at_cleared_when_token_absent(self) -> None:
        """_token_updated() resets _access_token_expires_at to 0 when no access token."""

        self.handler._access_token = ""
        self.handler._access_token_expires_at = 9999999999

        self.handler._token_updated("access_token")

        self.assertEqual(self.handler._access_token_expires_at, 0)

    def test_non_access_token_name_does_not_touch_expiry(self) -> None:
        """_token_updated('refresh_token') leaves _access_token_expires_at unchanged."""

        self.handler._access_token_expires_at = 12345

        self.handler._token_updated("refresh_token")

        self.assertEqual(self.handler._access_token_expires_at, 12345)

    def test_jwt_without_exp_leaves_expiry_unchanged(self) -> None:
        """_token_updated() leaves expiry unchanged when JWT has no exp claim."""

        import base64, json
        header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
        body = base64.urlsafe_b64encode(json.dumps({"sub": "u1"}).encode()).rstrip(b"=").decode()
        self.handler._access_token = f"{header}.{body}."
        self.handler._access_token_expires_at = 5555

        self.handler._token_updated("access_token")

        self.assertEqual(self.handler._access_token_expires_at, 5555)


class TestOIDCExtractUsername(unittest.TestCase):
    """Tests for OIDCHandler._get_username_from_access_token() — OIDC Standard Claims priority."""

    def setUp(self) -> None:
        self.handler = _make_oidc_handler()

    def _jwt(self, payload: dict) -> str:
        import base64, json
        header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
        body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        return f"{header}.{body}.sig"

    def test_email_is_highest_priority(self) -> None:
        """email is preferred over preferred_username, nickname, and sub."""

        token = self._jwt({
            "email": "e@example.com",
            "preferred_username": "pref",
            "nickname": "nick",
            "sub": "subval",
        })
        self.assertEqual(self.handler._get_username_from_access_token(token), "e@example.com")

    def test_preferred_username_used_when_no_email(self) -> None:
        """preferred_username is used when email is absent."""

        token = self._jwt({"preferred_username": "pref_user", "sub": "subval"})
        self.assertEqual(self.handler._get_username_from_access_token(token), "pref_user")

    def test_nickname_used_when_no_email_or_preferred_username(self) -> None:
        """nickname is used when email and preferred_username are absent."""

        token = self._jwt({"nickname": "nick_val", "sub": "subval"})
        self.assertEqual(self.handler._get_username_from_access_token(token), "nick_val")

    def test_sub_used_as_last_resort(self) -> None:
        """sub is used when no other identifier is present."""

        token = self._jwt({"sub": "only_sub"})
        self.assertEqual(self.handler._get_username_from_access_token(token), "only_sub")

    def test_falls_back_to_base_for_empty_token(self) -> None:
        """Returns None when token is empty."""

        self.assertIsNone(self.handler._get_username_from_access_token(""))

    def test_falls_back_to_base_when_no_identity_claims(self) -> None:
        """Returns diagnostic string when payload carries none of the known OIDC identity fields."""

        token = self._jwt({"iss": "https://example.com", "aud": "client"})
        self.assertEqual(self.handler._get_username_from_access_token(token),
                         "Invalid server response, missing user, email or sub")


class TestOIDCDoTokenRefresh(unittest.TestCase):
    """Tests for OIDCHandler._refresh_token_grant() — id_token freshness."""

    def setUp(self) -> None:
        self.handler = _make_oidc_handler()

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_silent_reauth_called_when_id_token_near_expiry(
            self, _mock_set: unittest.mock.MagicMock) -> None:
        """_refresh_token_grant() calls _silent_authentication() when the id_token is close to expiry."""

        import time
        self.handler._refresh_token = "refresh"
        self.handler._id_token = _make_jwt(int(time.time()) + 60)  # expires in 60s, within margin
        calls: list = []
        self.handler._silent_authentication = lambda: calls.append(True) or True  # type: ignore[method-assign]

        with unittest.mock.patch.object(self.handler, "_request_token", return_value=True):
            self.assertTrue(self.handler._refresh_token_grant())

        self.assertEqual(calls, [True])

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_silent_reauth_not_called_when_id_token_has_long_life(
            self, _mock_set: unittest.mock.MagicMock) -> None:
        """_refresh_token_grant() skips _silent_authentication() when the id_token has ample lifetime left."""

        import time
        self.handler._refresh_token = "refresh"
        self.handler._id_token = _make_jwt(int(time.time()) + 86400)  # expires in 24h
        calls: list = []
        self.handler._silent_authentication = lambda: calls.append(True) or True  # type: ignore[method-assign]

        with unittest.mock.patch.object(self.handler, "_request_token", return_value=True):
            self.assertTrue(self.handler._refresh_token_grant())

        self.assertEqual(calls, [])

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_silent_reauth_failure_returns_false(
            self, _mock_set: unittest.mock.MagicMock) -> None:
        """_refresh_token_grant() returns False when _silent_authentication() fails."""

        import time
        self.handler._refresh_token = "refresh"
        self.handler._id_token = _make_jwt(int(time.time()) + 60)  # near expiry, triggers reauth
        self.handler._silent_authentication = unittest.mock.Mock(  # type: ignore[method-assign]
            return_value=False)

        with unittest.mock.patch.object(self.handler, "_request_token", return_value=True):
            self.assertFalse(self.handler._refresh_token_grant())

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_missing_id_token_triggers_silent_reauth(
            self, _mock_set: unittest.mock.MagicMock) -> None:
        """_refresh_token_grant() attempts silent reauth when id_token is absent (exp defaults to 0)."""

        self.handler._refresh_token = "refresh"
        self.handler._id_token = ""
        calls: list = []
        self.handler._silent_authentication = lambda: calls.append(True) or True  # type: ignore[method-assign]

        with unittest.mock.patch.object(self.handler, "_request_token", return_value=True):
            self.assertTrue(self.handler._refresh_token_grant())

        self.assertEqual(calls, [True])


class TestOIDCDoSilentReauth(unittest.TestCase):
    """Tests for OIDCHandler._silent_authentication()."""

    def setUp(self) -> None:
        self.handler = _make_oidc_handler()

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_returns_false_when_no_id_token(
            self, _mock_set: unittest.mock.MagicMock) -> None:
        """_silent_authentication() returns False when no id_token is available."""

        self.handler._id_token = ""

        self.assertFalse(self.handler._silent_authentication())

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.authentication.oidchandler.UriHandler")
    def test_uses_redirect_uri_silent(
            self, mock_uri: unittest.mock.MagicMock,
            _mock_set: unittest.mock.MagicMock) -> None:
        """_silent_authentication() passes _redirect_uri_silent to _silent_authentication_request."""

        self.handler._id_token = "id"

        with unittest.mock.patch.object(
                self.handler, "_silent_authentication_request",
                return_value={"access_token": "new_acc"}) as mock_flow:
            self.handler._silent_authentication()

        _args, _kwargs = mock_flow.call_args
        self.assertEqual(_args[1], self.handler._redirect_uri_silent())

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.authentication.oidchandler.UriHandler")
    def test_clears_tokens_on_failure(
            self, mock_uri: unittest.mock.MagicMock,
            _mock_set: unittest.mock.MagicMock) -> None:
        """_silent_authentication() clears tokens and returns False on any failure."""

        from resources.lib.urihandler import UriStatus
        self.handler._id_token = "id"
        self.handler._access_token = "acc"
        mock_uri.open.return_value = ""
        mock_uri.instance.return_value.status = UriStatus(
            code=0, url="", error=True, reason="network down")

        self.assertFalse(self.handler._silent_authentication())

        self.assertEqual(self.handler._access_token, "")


class TestOIDCSetClaims(unittest.TestCase):
    """Tests for _set_claims() and _get_claims_param()."""

    def setUp(self) -> None:
        self.handler = _make_oidc_handler()

    def test_get_claims_param_returns_none_when_not_set(self) -> None:
        """_get_claims_param() returns None when no claims have been registered."""

        self.assertIsNone(self.handler._get_claims_param())

    def test_set_claims_id_token_only(self) -> None:
        """_set_claims() with id_token_claims builds a claims object with id_token key."""

        self.handler._set_claims(id_token_claims={"profileId": {"value": "abc", "essential": True}})

        import json
        result = json.loads(self.handler._get_claims_param())  # type: ignore[arg-type]
        self.assertIn("id_token", result)
        self.assertNotIn("userinfo", result)
        self.assertEqual(result["id_token"]["profileId"]["value"], "abc")

    def test_set_claims_userinfo_only(self) -> None:
        """_set_claims() with userinfo_claims builds a claims object with userinfo key."""

        self.handler._set_claims(userinfo_claims={"email": None})

        import json
        result = json.loads(self.handler._get_claims_param())  # type: ignore[arg-type]
        self.assertIn("userinfo", result)
        self.assertNotIn("id_token", result)

    def test_set_claims_both(self) -> None:
        """_set_claims() with both dicts produces id_token and userinfo keys."""

        self.handler._set_claims(
            id_token_claims={"profileId": {"value": "p1"}},
            userinfo_claims={"email": None},
        )

        import json
        result = json.loads(self.handler._get_claims_param())  # type: ignore[arg-type]
        self.assertIn("id_token", result)
        self.assertIn("userinfo", result)

    def test_set_claims_none_args_clears_request(self) -> None:
        """_set_claims() called with no arguments clears any previously set claims."""

        self.handler._set_claims(id_token_claims={"profileId": {"value": "x"}})
        self.handler._set_claims()

        self.assertIsNone(self.handler._get_claims_param())

    def test_set_claims_returns_dict_on_success(self) -> None:
        """_set_claims() returns the stored claims dict when claims are set."""

        result = self.handler._set_claims(id_token_claims={"profileId": {"value": "x"}})

        self.assertIsNotNone(result)
        self.assertIn("id_token", result)  # type: ignore[arg-type]

    def test_set_claims_returns_none_when_no_claims(self) -> None:
        """_set_claims() returns None when called with no arguments."""

        result = self.handler._set_claims()

        self.assertIsNone(result)

    def test_clear_tokens_all_clears_claims(self) -> None:
        """_clear_tokens() with no argument clears the claims request."""

        self.handler._set_claims(id_token_claims={"profileId": {"value": "x"}})
        with unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            self.handler._clear_tokens()

        self.assertIsNone(self.handler._claims_request)

    def test_clear_tokens_named_preserves_claims(self) -> None:
        """_clear_tokens('access_token') does not clear the claims request."""

        self.handler._set_claims(id_token_claims={"profileId": {"value": "x"}})
        with unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            self.handler._clear_tokens(token_name="access_token")

        self.assertIsNotNone(self.handler._claims_request)

    def test_get_claims_param_produces_compact_json(self) -> None:
        """_get_claims_param() returns compact JSON (no extra spaces)."""

        self.handler._set_claims(id_token_claims={"profileId": {"value": "x"}})
        param = self.handler._get_claims_param()

        self.assertIsNotNone(param)
        self.assertNotIn(" ", param)  # type: ignore[operator]


class TestOIDCSilentTokenGrantClaims(unittest.TestCase):
    """Tests that _silent_authentication_request() includes the claims parameter when set."""

    def setUp(self) -> None:
        self.handler = _make_oidc_handler()

    @unittest.mock.patch("resources.lib.authentication.oidchandler.UriHandler")
    def test_claims_included_in_auth_url_when_set(
            self, mock_uri: unittest.mock.MagicMock) -> None:
        """_silent_authentication_request() appends claims= to the auth URL when claims are set."""

        from resources.lib.urihandler import UriStatus
        mock_uri.open.return_value = ""
        mock_uri.instance.return_value.status = UriStatus(
            code=302, url="https://localhost/callback?code=abc&state=", error=False, reason="Found")

        self.handler._set_claims(id_token_claims={"profileId": {"value": "p1", "essential": True}})
        self.handler._id_token = "id"

        with unittest.mock.patch.object(self.handler, "_extract_auth_code", return_value="abc"), \
             unittest.mock.patch.object(self.handler, "_generate_pkce",
                                        return_value=("verifier", "challenge")):
            try:
                self.handler._silent_authentication_request("client", "https://localhost/cb", "openid")
            except Exception:
                pass

        auth_call_url = mock_uri.open.call_args_list[0][0][0]
        self.assertIn("claims=", auth_call_url)
        self.assertIn("profileId", auth_call_url)

    @unittest.mock.patch("resources.lib.authentication.oidchandler.UriHandler")
    def test_claims_absent_from_auth_url_when_not_set(
            self, mock_uri: unittest.mock.MagicMock) -> None:
        """_silent_authentication_request() does not include claims= when no claims are registered."""

        from resources.lib.urihandler import UriStatus
        mock_uri.open.return_value = ""
        mock_uri.instance.return_value.status = UriStatus(
            code=302, url="https://localhost/callback?code=abc&state=", error=False, reason="Found")

        self.handler._id_token = "id"

        with unittest.mock.patch.object(self.handler, "_extract_auth_code", return_value="abc"), \
             unittest.mock.patch.object(self.handler, "_generate_pkce",
                                        return_value=("verifier", "challenge")):
            try:
                self.handler._silent_authentication_request("client", "https://localhost/cb", "openid")
            except Exception:
                pass

        auth_call_url = mock_uri.open.call_args_list[0][0][0]
        self.assertNotIn("claims=", auth_call_url)


class TestOIDCSilentRedirectUri(unittest.TestCase):
    """Tests for OIDCHandler._redirect_uri_silent default."""

    def test_defaults_to_redirect_uri(self) -> None:
        """_redirect_uri_silent() falls back to redirect_uri when not overridden."""

        handler = _make_oidc_handler()
        self.assertEqual(handler._redirect_uri_silent(), handler.redirect_uri)


if __name__ == "__main__":
    unittest.main()
