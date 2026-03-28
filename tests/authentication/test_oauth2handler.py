# SPDX-License-Identifier: GPL-3.0-or-later

import base64
import hashlib
import json
import time
import unittest
import unittest.mock
import urllib.parse
from typing import Any, Optional

import resources.lib.authentication.oauth2handler as oauth2handler_module
from resources.lib.authentication.oauth2handler import (
    _JwtFallback, OAuth2Handler)


# ---------------------------------------------------------------------------
# Minimal mock JWT tokens used throughout this test module
# ---------------------------------------------------------------------------

def _make_jwt(payload: Any) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}."


MOCK_USER_ID = "00374815-0000-0000-cafe-f1de05deeffe"
MOCK_EMAIL = "test@example.com"

MOCK_ACCESS_TOKEN = _make_jwt({
    "sub": MOCK_USER_ID,
    "email": MOCK_EMAIL,
    "profileType": "Adult",
    "exp": 9999999999,
    "iat": 1700000000,
})

MOCK_PROFILE_ACCESS_TOKEN = _make_jwt({
    "sub": MOCK_USER_ID,
    "email": MOCK_EMAIL,
    "profileType": "ChildYoung",
    "exp": 9999999999,
    "iat": 1700000000,
})


# ---------------------------------------------------------------------------
# Minimal concrete subclass — OAuth2Handler is ABC; needs concrete properties
# ---------------------------------------------------------------------------

class _StubOAuth2Handler(OAuth2Handler):
    authorization_endpoint = "https://auth.example.com/authorize"
    token_endpoint = "https://auth.example.com/token"
    redirect_uri = "https://localhost/callback"


def _make_handler(**overrides: Any) -> "_StubOAuth2Handler":
    """Return a freshly constructed _StubOAuth2Handler with no real settings I/O."""
    with unittest.mock.patch(
        "resources.lib.addonsettings.AddonSettings.get_setting", return_value=None
    ):
        h = _StubOAuth2Handler("testrealm", "test_client")
    for k, v in overrides.items():
        setattr(h, k, v)
    return h


class TestJwtFallback(unittest.TestCase):
    """
    Tests for the _JwtFallback shim.

    These run against the class itself — independent of which JWT library
    is installed — so the shim is always exercised.
    """

    def setUp(self) -> None:
        self.fallback = _JwtFallback()

    def test_decode_valid_jwt_returns_claims(self) -> None:
        """Fallback decodes a well-formed JWT and returns its payload."""

        claims = self.fallback.decode(MOCK_ACCESS_TOKEN)

        self.assertEqual(claims.get("email"), MOCK_EMAIL)
        self.assertEqual(claims.get("sub"), MOCK_USER_ID)
        self.assertEqual(claims.get("profileType"), "Adult")

    def test_decode_profile_token_returns_profile_type(self) -> None:
        """Fallback decodes a JWT that carries a non-default profileType."""

        claims = self.fallback.decode(MOCK_PROFILE_ACCESS_TOKEN)

        self.assertEqual(claims.get("profileType"), "ChildYoung")

    def test_decode_ignores_verify_signature_kwarg(self) -> None:
        """Fallback accepts the verify_signature options kwarg without error."""

        claims = self.fallback.decode(
            MOCK_ACCESS_TOKEN, options={"verify_signature": False}
        )

        self.assertIsInstance(claims, dict)
        self.assertIn("email", claims)

    def test_decode_empty_string_raises(self) -> None:
        """Fallback raises ValueError for an empty token (IndexError path)."""

        with self.assertRaises(ValueError):
            self.fallback.decode("")

    def test_decode_no_dots_raises(self) -> None:
        """Fallback raises ValueError when token has no dot separators."""

        with self.assertRaises(ValueError):
            self.fallback.decode("nodots")

    def test_decode_only_header_raises(self) -> None:
        """Fallback raises ValueError for a single-part token (missing payload)."""

        with self.assertRaises(ValueError):
            self.fallback.decode("onlyone.")

    def test_decode_invalid_base64_payload_raises(self) -> None:
        """Fallback raises ValueError when payload is not valid base64/JSON."""

        with self.assertRaises(ValueError):
            self.fallback.decode("header.!!!invalid!!!.sig")

    def test_decode_valid_base64_but_not_json_raises(self) -> None:
        """Fallback raises ValueError when payload is valid base64 but not JSON."""

        import base64
        not_json = base64.urlsafe_b64encode(b"not json at all").decode().rstrip("=")
        token = f"header.{not_json}.sig"
        with self.assertRaises(ValueError):
            self.fallback.decode(token)


class TestDecodeTokenWithFallback(unittest.TestCase):
    """
    Tests for OAuth2Handler.decode_token() using _JwtFallback as the backend.

    Patches the module-level ``jwt`` object so the real PyJWT library is
    bypassed.  This verifies the full decode_token() code path works with
    the shim — covering the 'neither library installed' scenario.
    """

    def setUp(self) -> None:
        self.handler = _make_handler()

    def test_decode_token_via_fallback_returns_claims(self) -> None:
        """decode_token() works correctly when jwt is replaced by _JwtFallback."""

        with unittest.mock.patch.object(oauth2handler_module, "jwt", _JwtFallback()):
            claims = self.handler.decode_token(MOCK_ACCESS_TOKEN)

        self.assertEqual(claims.get("email"), MOCK_EMAIL)
        self.assertEqual(claims.get("sub"), MOCK_USER_ID)

    def test_decode_token_via_fallback_empty_string(self) -> None:
        """decode_token() returns None via fallback for empty string."""

        with unittest.mock.patch.object(oauth2handler_module, "jwt", _JwtFallback()):
            claims = self.handler.decode_token("")

        self.assertIsNone(claims)

    def test_decode_token_via_fallback_invalid_token(self) -> None:
        """decode_token() returns None via fallback for malformed token."""

        with unittest.mock.patch.object(oauth2handler_module, "jwt", _JwtFallback()):
            claims = self.handler.decode_token("not.a.jwt")

        self.assertIsNone(claims)


class TestDecodeTokenWithRealJwt(unittest.TestCase):
    """
    Tests for OAuth2Handler.decode_token() using the real installed jwt library.

    These cover both the ``import jwt`` and ``import pyjwt as jwt`` paths:
    both resolve to the same PyJWT library and the same decode_token behaviour.
    """

    def setUp(self) -> None:
        self.handler = _make_handler()

    def test_decode_token_returns_claims(self) -> None:
        """decode_token() with the real jwt returns the full payload."""

        claims = self.handler.decode_token(MOCK_ACCESS_TOKEN)

        self.assertEqual(claims.get("email"), MOCK_EMAIL)
        self.assertEqual(claims.get("sub"), MOCK_USER_ID)
        self.assertEqual(claims.get("profileType"), "Adult")

    def test_decode_token_profile_token(self) -> None:
        """decode_token() returns profileType from a profile-scoped token."""

        claims = self.handler.decode_token(MOCK_PROFILE_ACCESS_TOKEN)

        self.assertEqual(claims.get("profileType"), "ChildYoung")

    def test_decode_token_returns_none_on_invalid(self) -> None:
        """decode_token() returns None for a malformed token (no exception)."""

        claims = self.handler.decode_token("not.a.jwt")

        self.assertIsNone(claims)

    def test_decode_token_returns_none_on_empty_string(self) -> None:
        """decode_token() returns None for an empty string."""

        self.assertIsNone(self.handler.decode_token(""))

    def test_decode_token_returns_none_when_jwt_raises(self) -> None:
        """decode_token() swallows unexpected exceptions and returns None."""

        broken_jwt = unittest.mock.MagicMock()
        broken_jwt.decode.side_effect = RuntimeError("unexpected library error")

        with unittest.mock.patch.object(oauth2handler_module, "jwt", broken_jwt):
            claims = self.handler.decode_token(MOCK_ACCESS_TOKEN)

        self.assertIsNone(claims)


class TestOAuth2HandlerPkce(unittest.TestCase):
    """Tests for PKCE helpers — pure crypto, no I/O."""

    def setUp(self) -> None:
        self.handler = _make_handler()

    def test_generate_pkce_returns_two_strings(self) -> None:
        """_generate_pkce() returns a (verifier, challenge) tuple of strings."""

        verifier, challenge = self.handler._generate_pkce()

        self.assertIsInstance(verifier, str)
        self.assertIsInstance(challenge, str)

    def test_generate_pkce_challenge_is_sha256_of_verifier(self) -> None:
        """Challenge must be base64url(sha256(verifier)) per RFC 7636."""

        verifier, challenge = self.handler._generate_pkce()

        digest = hashlib.sha256(verifier.encode()).digest()
        expected = base64.urlsafe_b64encode(digest).decode().rstrip("=")
        self.assertEqual(challenge, expected)

    def test_generate_pkce_no_padding_chars(self) -> None:
        """Verifier and challenge must not contain base64 padding '='."""

        verifier, challenge = self.handler._generate_pkce()

        self.assertNotIn("=", verifier)
        self.assertNotIn("=", challenge)

    def test_generate_pkce_unique_each_call(self) -> None:
        """Each call produces distinct values (PRNG-sourced)."""

        v1, _ = self.handler._generate_pkce()
        v2, _ = self.handler._generate_pkce()

        self.assertNotEqual(v1, v2)


class TestOAuth2HandlerExtractAuthCode(unittest.TestCase):
    """Tests for _extract_auth_code() — RFC 6749 §4.1.2 + §10.12."""

    def setUp(self) -> None:
        self.handler = _make_handler()

    def test_returns_code_when_code_and_state_match(self) -> None:
        """Returns the auth code when both code and state are present and state matches."""

        url = "https://example.com/callback?code=abc123&state=mystate"
        self.assertEqual(self.handler._extract_auth_code(url, "mystate"), "abc123")

    def test_returns_none_when_no_code(self) -> None:
        """Returns None when the redirect URL has no code parameter."""

        url = "https://example.com/callback?state=mystate"
        self.assertIsNone(self.handler._extract_auth_code(url, "mystate"))

    def test_returns_none_when_no_state(self) -> None:
        """Returns None when the redirect URL has no state parameter."""

        url = "https://example.com/callback?code=abc123"
        self.assertIsNone(self.handler._extract_auth_code(url, "mystate"))

    def test_returns_none_on_state_mismatch(self) -> None:
        """Returns None when state in URL does not match expected state (RFC 6749 §10.12)."""

        url = "https://example.com/callback?code=abc123&state=wrong"
        self.assertIsNone(self.handler._extract_auth_code(url, "mystate"))

    def test_works_with_fragment_separator(self) -> None:
        """Returns the auth code when code appears after an ampersand, not just '?'."""

        url = "https://example.com/callback?foo=bar&code=abc123&state=mystate"
        self.assertEqual(self.handler._extract_auth_code(url, "mystate"), "abc123")

class TestOAuth2HandlerSaveTokens(unittest.TestCase):
    """Tests for _save_tokens() and _clear_tokens()."""

    def setUp(self) -> None:
        self.handler = _make_handler()

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time")
    def test_save_tokens_updates_instance_state(self, mock_time: unittest.mock.MagicMock, _mock_set: unittest.mock.MagicMock) -> None:
        """_save_tokens() sets _access_token, _refresh_token, _access_token_expires_at."""

        mock_time.time.return_value = 1000

        self.handler._save_tokens({
            "access_token": "acc123",
            "refresh_token": "ref456",
            "expires_in": 3600,
        })

        self.assertEqual(self.handler._access_token, "acc123")
        self.assertEqual(self.handler._refresh_token, "ref456")
        self.assertEqual(self.handler._access_token_expires_at, 4600)  # 1000 + 3600

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time")
    def test_save_tokens_without_refresh_token_preserves_existing(self, mock_time: unittest.mock.MagicMock, _mock_set: unittest.mock.MagicMock) -> None:
        """_save_tokens() leaves _refresh_token unchanged if not in response."""

        mock_time.time.return_value = 1000
        self.handler._refresh_token = "existing_refresh"

        self.handler._save_tokens({"access_token": "new_acc", "expires_in": 900})

        self.assertEqual(self.handler._refresh_token, "existing_refresh")

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time")
    def test_save_tokens_refresh_only_preserves_access_token(
            self, mock_time: unittest.mock.MagicMock, _mock_set: unittest.mock.MagicMock) -> None:
        """_save_tokens() with only refresh_token leaves access_token unchanged."""

        mock_time.time.return_value = 1000
        self.handler._access_token = "existing_acc"
        self.handler._access_token_expires_at = 9999

        self.handler._save_tokens({"refresh_token": "new_ref"}, token_name="refresh_token")

        self.assertEqual(self.handler._access_token, "existing_acc")
        self.assertEqual(self.handler._access_token_expires_at, 9999)
        self.assertEqual(self.handler._refresh_token, "new_ref")

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time")
    def test_save_tokens_defaults_expires_in_to_3600(self, mock_time: unittest.mock.MagicMock, _mock_set: unittest.mock.MagicMock) -> None:
        """_save_tokens() uses 3600s default when expires_in is absent."""

        mock_time.time.return_value = 0

        self.handler._save_tokens({"access_token": "x"})

        self.assertEqual(self.handler._access_token_expires_at, 3600)

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_clear_tokens_wipes_state(self, _mock_set: unittest.mock.MagicMock) -> None:
        """_clear_tokens() resets all token fields and marks the token as expired."""

        self.handler._access_token = "old"
        self.handler._refresh_token = "old"
        self.handler._access_token_expires_at = 9999999999

        self.handler._clear_tokens()

        self.assertEqual(self.handler._access_token, "")
        self.assertEqual(self.handler._refresh_token, "")
        self.assertLessEqual(self.handler._access_token_expires_at, int(time.time()))

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_clear_tokens_with_token_name_clears_only_that_token(self, _mock_set: unittest.mock.MagicMock) -> None:
        """_clear_tokens('access_token') clears only the access token, not the refresh token."""

        self.handler._access_token = "acc"
        self.handler._refresh_token = "ref"
        self.handler._access_token_expires_at = 9999999999

        self.handler._clear_tokens(token_name="access_token")

        self.assertEqual(self.handler._access_token, "")
        self.assertEqual(self.handler._refresh_token, "ref")
        self.assertLessEqual(self.handler._access_token_expires_at, int(time.time()))

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_clear_tokens_refresh_token_preserves_access_token(self, _mock_set: unittest.mock.MagicMock) -> None:
        """_clear_tokens('refresh_token') clears only the refresh token, not access_token."""

        self.handler._access_token = "acc"
        self.handler._refresh_token = "ref"
        self.handler._access_token_expires_at = 9999999999

        self.handler._clear_tokens(token_name="refresh_token")

        self.assertEqual(self.handler._access_token, "acc")
        self.assertEqual(self.handler._refresh_token, "")

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_clear_tokens_passes_token_name_to_hook(self, _mock_set: unittest.mock.MagicMock) -> None:
        """_clear_tokens() calls _token_updated() exactly once with token_name=None."""

        calls: list = []
        self.handler._token_updated = lambda tn=None: calls.append(tn)  # type: ignore[method-assign]

        self.handler._clear_tokens()

        self.assertEqual(calls, [None])

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_clear_tokens_access_token_passes_name_to_hook(self, _mock_set: unittest.mock.MagicMock) -> None:
        """_clear_tokens('access_token') forwards 'access_token' to _token_updated()."""

        calls: list = []
        self.handler._token_updated = lambda tn=None: calls.append(tn)  # type: ignore[method-assign]

        self.handler._clear_tokens("access_token")

        self.assertEqual(calls, ["access_token"])

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_clear_tokens_refresh_token_passes_name_to_hook(self, _mock_set: unittest.mock.MagicMock) -> None:
        """_clear_tokens('refresh_token') forwards 'refresh_token' to _token_updated()."""

        calls: list = []
        self.handler._token_updated = lambda tn=None: calls.append(tn)  # type: ignore[method-assign]

        self.handler._clear_tokens("refresh_token")

        self.assertEqual(calls, ["refresh_token"])

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    def test_clear_tokens_bogus_name_does_nothing(self, _mock_set: unittest.mock.MagicMock) -> None:
        """_clear_tokens() with an unknown token name calls _token_updated() once but clears nothing."""

        calls: list = []
        self.handler._token_updated = lambda tn=None: calls.append(tn)  # type: ignore[method-assign]
        self.handler._access_token = "acc"
        self.handler._refresh_token = "ref"

        self.handler._clear_tokens("id_token")

        self.assertEqual(calls, ["id_token"])
        self.assertEqual(self.handler._access_token, "acc")
        self.assertEqual(self.handler._refresh_token, "ref")

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time")
    def test_save_tokens_passes_token_name_to_hook(
            self, mock_time: unittest.mock.MagicMock, _mock_set: unittest.mock.MagicMock) -> None:
        """_save_tokens() forwards token_name to _token_updated()."""

        mock_time.time.return_value = 0
        calls: list = []
        self.handler._token_updated = lambda tn=None: calls.append(tn)  # type: ignore[method-assign]

        self.handler._save_tokens({"access_token": "acc", "expires_in": 3600}, token_name="access_token")

        self.assertEqual(calls, ["access_token"])

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time")
    def test_save_tokens_passes_none_to_hook_by_default(
            self, mock_time: unittest.mock.MagicMock, _mock_set: unittest.mock.MagicMock) -> None:
        """_save_tokens() passes None to _token_updated() when token_name is omitted."""

        mock_time.time.return_value = 0
        calls: list = []
        self.handler._token_updated = lambda tn=None: calls.append(tn)  # type: ignore[method-assign]

        self.handler._save_tokens({"access_token": "acc", "expires_in": 3600})

        self.assertEqual(calls, [None])

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time")
    def test_save_tokens_unknown_token_name_still_passes_to_hook(
            self, mock_time: unittest.mock.MagicMock, _mock_set: unittest.mock.MagicMock) -> None:
        """_save_tokens() with an unrecognised token_name forwards the name to the hook but saves nothing."""

        mock_time.time.return_value = 0
        calls: list = []
        self.handler._token_updated = lambda tn=None: calls.append(tn)  # type: ignore[method-assign]

        self.handler._save_tokens({"access_token": "acc", "expires_in": 60}, token_name="bogus_token")

        self.assertNotEqual(self.handler._access_token, "acc")
        self.assertEqual(calls, ["bogus_token"])

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time")
    def test_save_tokens_with_token_name_does_not_save_other_tokens(
            self, mock_time: unittest.mock.MagicMock, _mock_set: unittest.mock.MagicMock) -> None:
        """_save_tokens() with token_name='refresh_token' does not overwrite access_token.

        Even if the dict contains access_token, it must not be saved when token_name
        names a different field.
        """

        mock_time.time.return_value = 0
        self.handler._access_token = "existing_acc"

        self.handler._save_tokens(
            {"access_token": "new_acc", "refresh_token": "new_ref", "expires_in": 3600},
            token_name="refresh_token",
        )

        self.assertEqual(self.handler._access_token, "existing_acc")
        self.assertEqual(self.handler._refresh_token, "new_ref")


class TestOAuth2HandlerLoadTokens(unittest.TestCase):
    """Tests for selective _load_tokens() and _token_updated() hook."""

    def setUp(self) -> None:
        self.handler = _make_handler()

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.get_setting",
                         side_effect=lambda key, **_: {
                             "testrealm_oauth2_test_client_access_token": "loaded_acc",
                             "testrealm_oauth2_test_client_access_token_expires_at": "5000",
                             "testrealm_oauth2_test_client_refresh_token": "loaded_ref",
                         }.get(key, ""))
    def test_load_tokens_loads_all_by_default(self, _mock_get: unittest.mock.MagicMock) -> None:
        """_load_tokens() with no argument loads all standard token fields."""

        self.handler._load_tokens()

        self.assertEqual(self.handler._access_token, "loaded_acc")
        self.assertEqual(self.handler._access_token_expires_at, 5000)
        self.assertEqual(self.handler._refresh_token, "loaded_ref")

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.get_setting",
                         side_effect=lambda key, **_: {
                             "testrealm_oauth2_test_client_access_token": "new_acc",
                             "testrealm_oauth2_test_client_access_token_expires_at": "7000",
                         }.get(key, ""))
    def test_load_tokens_access_token_only_preserves_refresh(
            self, _mock_get: unittest.mock.MagicMock) -> None:
        """_load_tokens('access_token') reloads access_token+access_token_expires_at but not refresh_token."""

        self.handler._refresh_token = "existing_ref"

        self.handler._load_tokens("access_token")

        self.assertEqual(self.handler._access_token, "new_acc")
        self.assertEqual(self.handler._access_token_expires_at, 7000)
        self.assertEqual(self.handler._refresh_token, "existing_ref")

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.get_setting",
                         side_effect=lambda key, **_: {
                             "testrealm_oauth2_test_client_refresh_token": "new_ref",
                         }.get(key, ""))
    def test_load_tokens_refresh_token_only_preserves_access(
            self, _mock_get: unittest.mock.MagicMock) -> None:
        """_load_tokens('refresh_token') reloads only refresh_token."""

        self.handler._access_token = "existing_acc"
        self.handler._access_token_expires_at = 9999

        self.handler._load_tokens("refresh_token")

        self.assertEqual(self.handler._access_token, "existing_acc")
        self.assertEqual(self.handler._access_token_expires_at, 9999)
        self.assertEqual(self.handler._refresh_token, "new_ref")

    def test_token_updated_called_with_token_name(self) -> None:
        """_load_tokens() passes token_name to _token_updated()."""

        calls: list = []
        self.handler._token_updated = lambda tn=None: calls.append(tn)  # type: ignore[method-assign]

        with unittest.mock.patch("resources.lib.addonsettings.AddonSettings.get_setting",
                                 return_value=""):
            self.handler._load_tokens("access_token")

        self.assertEqual(calls, ["access_token"])

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.get_setting",
                         side_effect=lambda key, **_: {
                             "testrealm_oauth2_test_client_access_token": "ret_acc",
                             "testrealm_oauth2_test_client_access_token_expires_at": "8000",
                             "testrealm_oauth2_test_client_refresh_token": "ret_ref",
                         }.get(key, ""))
    def test_load_tokens_returns_all_loaded_fields(self, _mock_get: unittest.mock.MagicMock) -> None:
        """_load_tokens() returns a dict containing all loaded token fields."""

        result = self.handler._load_tokens()

        self.assertEqual(result["access_token"], "ret_acc")
        self.assertEqual(result["access_token_expires_at"], 8000)
        self.assertEqual(result["refresh_token"], "ret_ref")

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.get_setting",
                         side_effect=lambda key, **_: {
                             "testrealm_oauth2_test_client_access_token": "only_acc",
                             "testrealm_oauth2_test_client_access_token_expires_at": "3000",
                         }.get(key, ""))
    def test_load_tokens_access_token_returns_only_access_fields(
            self, _mock_get: unittest.mock.MagicMock) -> None:
        """_load_tokens('access_token') returns only access_token and access_token_expires_at."""

        result = self.handler._load_tokens("access_token")

        self.assertIn("access_token", result)
        self.assertIn("access_token_expires_at", result)
        self.assertNotIn("refresh_token", result)

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.get_setting",
                         side_effect=lambda key, **_: {
                             "testrealm_oauth2_test_client_refresh_token": "only_ref",
                         }.get(key, ""))
    def test_load_tokens_refresh_token_returns_only_refresh_field(
            self, _mock_get: unittest.mock.MagicMock) -> None:
        """_load_tokens('refresh_token') returns only refresh_token."""

        result = self.handler._load_tokens("refresh_token")

        self.assertIn("refresh_token", result)
        self.assertNotIn("access_token", result)
        self.assertNotIn("access_token_expires_at", result)

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value="")
    def test_load_tokens_unknown_key_returns_empty_dict(
            self, _mock_get: unittest.mock.MagicMock) -> None:
        """_load_tokens() with an unrecognised token_name loads nothing and returns an empty dict."""

        self.handler._access_token = "preserved"
        result = self.handler._load_tokens("bogus_token")

        self.assertEqual(result, {})
        self.assertEqual(self.handler._access_token, "preserved")


class TestOAuth2HandlerRefresh(unittest.TestCase):
    """Tests for refresh_access_token() and _get_access_token()."""

    def setUp(self) -> None:
        self.handler = _make_handler()

    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time")
    def test_no_refresh_when_token_still_valid(self, mock_time: unittest.mock.MagicMock) -> None:
        """refresh_access_token() returns the token without refreshing if not near expiry."""

        mock_time.time.return_value = 1000
        self.handler._access_token_expires_at = 2000  # 700s remaining, above ACCESS_TOKEN_REFRESH_MARGIN=300
        self.handler._access_token = "valid_token"

        with unittest.mock.patch.object(self.handler, "_refresh_token_grant") as mock_refresh:
            result = self.handler.refresh_access_token()

        self.assertEqual(result, "valid_token")
        mock_refresh.assert_not_called()

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time")
    def test_refresh_triggered_near_expiry(self, mock_time: unittest.mock.MagicMock, _mock_set: unittest.mock.MagicMock) -> None:
        """refresh_access_token() calls _refresh_token_grant() when within ACCESS_TOKEN_REFRESH_MARGIN."""

        mock_time.time.return_value = 1000
        self.handler._access_token_expires_at = 1200  # 200s remaining, below ACCESS_TOKEN_REFRESH_MARGIN=300
        self.handler._access_token = "valid_token"

        with unittest.mock.patch.object(self.handler, "_refresh_token_grant") as mock_refresh:
            result = self.handler.refresh_access_token()

        self.assertEqual(result, "valid_token")
        mock_refresh.assert_called_once()

    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time")
    def test_returns_none_when_refresh_fails(self, mock_time: unittest.mock.MagicMock) -> None:
        """refresh_access_token() returns None if _refresh_token_grant returns False."""

        mock_time.time.return_value = 1000
        self.handler._access_token_expires_at = 1100

        with unittest.mock.patch.object(
            self.handler, "_refresh_token_grant", return_value=False
        ):
            result = self.handler.refresh_access_token()

        self.assertIsNone(result)

    def test_get_authentication_token_returns_none_when_no_token(self) -> None:
        """_get_access_token() returns None when _access_token is empty."""

        self.handler._access_token = ""

        self.assertIsNone(self.handler._get_access_token())

    def test_get_authentication_token_returns_token_when_set(self) -> None:
        """_get_access_token() returns the stored token regardless of expiry."""

        self.handler._access_token = "mytoken"

        self.assertEqual(self.handler._get_access_token(), "mytoken")


class TestOAuth2HandlerNetwork(unittest.TestCase):
    """Tests for methods that make HTTP calls — UriHandler is mocked."""

    def setUp(self) -> None:
        self.handler = _make_handler()
        from resources.lib.urihandler import UriStatus
        ok_status = UriStatus(code=200, url="", error=False, reason="OK")
        instance_patcher = unittest.mock.patch("resources.lib.urihandler.UriHandler.instance")
        self._mock_instance = instance_patcher.start()
        self._mock_instance.return_value.status = ok_status
        self.addCleanup(instance_patcher.stop)
        decode_patcher = unittest.mock.patch.object(
            self.handler, "decode_token", return_value={"sub": "test"})
        decode_patcher.start()
        self.addCleanup(decode_patcher.stop)

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.urihandler.UriHandler.open")
    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time")
    def test_request_token_stores_on_success(self, mock_time: unittest.mock.MagicMock, mock_open: unittest.mock.MagicMock, _mock_set: unittest.mock.MagicMock) -> None:
        """_request_token() stores tokens from a successful response."""

        mock_time.time.return_value = 0
        mock_open.return_value = json.dumps({
            "access_token": "tok",
            "expires_in": 600,
        })

        self.handler._request_token({"grant_type": "authorization_code", "code": "c"})

        self.assertEqual(self.handler._access_token, "tok")

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.urihandler.UriHandler.open")
    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time")
    def test_request_token_passes_through_user_agent_none_header(
            self,
            mock_time: unittest.mock.MagicMock,
            mock_open: unittest.mock.MagicMock,
            _mock_set: unittest.mock.MagicMock) -> None:
        """_request_token() forwards an explicit User-Agent suppression marker to UriHandler."""

        mock_time.time.return_value = 0
        mock_open.return_value = json.dumps({
            "access_token": "tok",
            "expires_in": 600,
        })

        self.handler._request_token(
            {"grant_type": "authorization_code", "code": "c"},
            headers={"User-Agent": None}
        )

        _, kwargs = mock_open.call_args
        self.assertIsNone(kwargs.get("additional_headers", {}).get("User-Agent"))

    @unittest.mock.patch("resources.lib.urihandler.UriHandler.open")
    def test_request_token_raises_on_network_error(self, mock_open: unittest.mock.MagicMock) -> None:
        """_request_token() re-raises exceptions for callers to handle."""

        mock_open.side_effect = IOError("timeout")

        with self.assertRaises(IOError):
            self.handler._request_token({"grant_type": "authorization_code"})

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.urihandler.UriHandler.open")
    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time")
    def test_exchange_code_returns_true_on_success(self, mock_time: unittest.mock.MagicMock, mock_open: unittest.mock.MagicMock, _mock_set: unittest.mock.MagicMock) -> None:
        """_exchange_code() returns True when token exchange succeeds."""

        mock_time.time.return_value = 0
        mock_open.return_value = json.dumps({"access_token": "tok", "expires_in": 600})

        result = self.handler._exchange_code("authcode", "verifier123")

        self.assertTrue(result)

    @unittest.mock.patch("resources.lib.urihandler.UriHandler.open")
    def test_exchange_code_raises_on_network_failure(self, mock_open: unittest.mock.MagicMock) -> None:
        """_exchange_code() propagates network errors to its caller."""

        mock_open.side_effect = IOError("bad gateway")

        with self.assertRaises(IOError):
            self.handler._exchange_code("authcode", "verifier123")


class TestOAuth2HandlerDoTokenRefresh(unittest.TestCase):
    """Tests for _refresh_token_grant() — the base refresh-token grant."""

    def setUp(self) -> None:
        self.handler = _make_handler()
        from resources.lib.urihandler import UriStatus
        ok_status = UriStatus(code=200, url="", error=False, reason="OK")
        instance_patcher = unittest.mock.patch("resources.lib.urihandler.UriHandler.instance")
        self._mock_instance = instance_patcher.start()
        self._mock_instance.return_value.status = ok_status
        self.addCleanup(instance_patcher.stop)
        decode_patcher = unittest.mock.patch.object(
            self.handler, "decode_token", return_value={"sub": "test"})
        decode_patcher.start()
        self.addCleanup(decode_patcher.stop)

    @unittest.mock.patch("resources.lib.addonsettings.AddonSettings.set_setting")
    @unittest.mock.patch("resources.lib.urihandler.UriHandler.open")
    @unittest.mock.patch("resources.lib.authentication.oauth2handler.time")
    def test_do_token_refresh_posts_refresh_grant(
            self,
            mock_time: unittest.mock.MagicMock,
            mock_open: unittest.mock.MagicMock,
            _mock_set: unittest.mock.MagicMock) -> None:
        """_refresh_token_grant() POSTs a refresh_token grant and stores the new token."""

        mock_time.time.return_value = 0
        mock_open.return_value = json.dumps({"access_token": "new_tok", "expires_in": 3600})
        self.handler._refresh_token = "old_refresh"

        self.assertTrue(self.handler._refresh_token_grant())

        _, kwargs = mock_open.call_args
        self.assertEqual(kwargs["data"]["grant_type"], "refresh_token")
        self.assertEqual(kwargs["data"]["refresh_token"], "old_refresh")
        self.assertEqual(self.handler._access_token, "new_tok")

    def test_do_token_refresh_raises_without_refresh_token(self) -> None:
        """_refresh_token_grant() returns False when no refresh token is available."""

        self.handler._refresh_token = ""

        self.assertFalse(self.handler._refresh_token_grant())


class TestOAuth2HandlerScopes(unittest.TestCase):
    """Tests for the scopes property default."""

    def test_default_scopes(self) -> None:
        """The base scopes property returns the standard OpenID Connect scope set."""

        handler = _make_handler()

        self.assertEqual(handler.scopes, ["openid", "profile", "email", "offline_access"])


class TestOAuth2AuthorizationBearer(unittest.TestCase):
    """Tests for _authorization_bearer (RFC 6750 §2.1)."""

    def setUp(self) -> None:
        self.handler = _make_handler()

    def test_returns_bearer_header_for_explicit_token(self) -> None:
        """An explicit token produces Authorization: Bearer <token>."""

        result = self.handler._authorization_bearer("mytoken")

        self.assertEqual(result, {"Authorization": "Bearer mytoken"})

    def test_falls_back_to_stored_access_token(self) -> None:
        """No explicit token: falls back to the stored access token."""

        self.handler._access_token = "stored"

        result = self.handler._authorization_bearer()

        self.assertEqual(result, {"Authorization": "Bearer stored"})

    def test_explicit_token_overrides_stored(self) -> None:
        """Explicit token wins over the stored access token."""

        self.handler._access_token = "stored"

        result = self.handler._authorization_bearer("override")

        self.assertEqual(result, {"Authorization": "Bearer override"})

    def test_returns_empty_dict_when_no_token(self) -> None:
        """Returns empty dict when neither an explicit token nor a stored one exists."""

        self.handler._access_token = ""

        result = self.handler._authorization_bearer()

        self.assertEqual(result, {})

    def test_returned_dict_is_mutable(self) -> None:
        """Callers can safely add headers to the returned dict."""

        result = self.handler._authorization_bearer("tok")
        result["Accept"] = "application/json"

        self.assertIn("Accept", result)


if __name__ == "__main__":
    unittest.main()
