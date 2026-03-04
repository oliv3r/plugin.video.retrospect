# SPDX-License-Identifier: GPL-3.0-or-later
from typing import Optional

from .authenticationhandler import AuthenticationHandler
from .authenticationresult import AuthenticationResult
from ..addonsettings import AddonSettings, LOCAL
from ..helpers.languagehelper import LanguageHelper
from ..logger import Logger
from ..vault import Vault
from ..xbmcwrapper import XbmcWrapper


class Authenticator(object):
    def __init__(self, handler: AuthenticationHandler,
                 channel_name: Optional[str] = None,
                 channel_guid: Optional[str] = None,
                 username_setting_id: Optional[str] = None,
                 password_setting_id: Optional[str] = None):
        """ Main logic handler for authentication.

        :param handler:             The authentication handler to use.
        :param channel_name:        Channel display name used in dialogs.
        :param channel_guid:        Channel GUID for credential lookup.
        :param username_setting_id: Settings ID for persisting the username.
        :param password_setting_id: Vault setting ID for the password.

        """

        if handler is None:
            raise ValueError("No authenication handler specified.")

        if not isinstance(handler, AuthenticationHandler):
            raise ValueError("Invalid authenication handler specified.")

        self.__handler = handler
        self.__channel_name = channel_name
        self.__channel_guid = channel_guid
        self.__username_setting_id = username_setting_id
        self.__password_setting_id = password_setting_id

    def log_on(self, username: Optional[str] = None, password: Optional[str] = None) -> AuthenticationResult:
        """ Performs the logon of a user. Either with the specified credentials or via a lookup.
        Logs off a previous user if the username has changed from previous logins.

        Resolves username from AddonSettings and password from the Vault when not supplied,
        then attempts an automatic credential login before falling back to interactive login.

        :param username:        The username; looked up from settings or prompted interactively if None.
        :param password:        The password to use; looked up from Vault if None.

        :returns: An indication of a successful login.

        """

        if username is None:
            username = self._get_username()

        Logger.debug("Attempting to log on: username=%s", self.__safe_log(username))

        result = self._resume_session(username)
        if result.logged_on or result.error == "network_error":
            return result

        if password is None:
            password = self._get_password()

        return self._auto_login(username, password)

    def _resume_session(self, username: Optional[str]) -> AuthenticationResult:
        """ Check whether an existing active session can be reused.

        Checks for a valid session matching the requested username first. If a
        different user is stored, the current session is logged off as a side
        effect. Errors are surfaced via a dialog as a side effect.

        :param username:    The username being logged on (may be None).

        :returns: - ``AuthenticationResult`` with ``logged_on=True``: valid resumed session.
                  - ``AuthenticationResult`` with ``error=<error>``: error surfaced; caller falls through.
                  - ``AuthenticationResult`` with ``error="no_active_session"``: no session; caller falls through.

        """

        result = self.__handler.active_authentication()
        Logger.debug("Cached session present: %s", result.logged_on)

        if result.logged_on:
            logged_on_user = result.username
            if username and logged_on_user.lower() == username.lower():
                Logger.info("Active session found for user (%s), skipping login.",
                            self.__safe_log(logged_on_user))
                return result

            Logger.warning("Different user requested (%s → %s). Logging off first.",
                           self.__safe_log(logged_on_user), self.__safe_log(username))
            self.log_off(logged_on_user)

        if result.error:
            Logger.error("Session check failed: %s", result.error)

            if result.error == "network_error":
                XbmcWrapper.show_dialog(self.__channel_name, LanguageHelper.NetworkLoginError)
            else:
                XbmcWrapper.show_dialog(self.__channel_name, result.error)

            return result

        return AuthenticationResult("", error="no_active_session")

    def _auto_login(self, username: Optional[str],
                    password: Optional[str] = None) -> AuthenticationResult:
        """
        Validate that credentials are present, then perform a headless login.

        :param username:    The username to log on with.
        :param password:    The password to use.

        :returns: The result of the login attempt.

        """

        Logger.debug("Attempting credential login for: %s", self.__safe_log(username))

        if not username and not password:
            Logger.warning("Missing credentials")
            return AuthenticationResult("", error="missing_credentials")

        if not username:
            Logger.error("No username specified")
            XbmcWrapper.show_dialog(self.__channel_name, LanguageHelper.MissingUsername)
            return AuthenticationResult("", error="missing_username")

        if not password:
            Logger.error("No password specified")
            XbmcWrapper.show_dialog(self.__channel_name, LanguageHelper.MissingPassword)
            return AuthenticationResult("", error="missing_password")

        return self._headless_login(username, password)

    def _headless_login(self, username: str, password: str) -> AuthenticationResult:
        """ Perform a direct credential login via the handler.

        :param username:    The username to log on with.
        :param password:    The password to use.

        :returns: The result of the login attempt.

        """

        Logger.debug("Headless login for: %s", self.__safe_log(username))
        result = self.__handler.log_on(username, password)
        if result.logged_on:
            return result

        Logger.debug("Headless login failed for %s: %s", self.__safe_log(username), result.error)
        if result.error == "invalid_credentials":
            XbmcWrapper.show_dialog(self.__channel_name, LanguageHelper.LoginErrorTitle)
        elif result.error == "network_error":
            XbmcWrapper.show_dialog(self.__channel_name, LanguageHelper.NetworkLoginError)
        else:
            XbmcWrapper.show_dialog(self.__channel_name, result.error)

        return result

    def active_authentication(self) -> AuthenticationResult:
        """ Check if the user with the given name is currently authenticated.

        :returns: a AuthenticationResult with the account data

        """

        return self.__handler.active_authentication()

    def get_authentication_token(self) -> Optional[str]:
        """ Fetches an authentication token for the given login

        :return: token value

        """

        return self.__handler.get_authentication_token()

    def log_off(self, username: str, force: bool = True) -> None:
        """ Logs off the currently authenticated user, clearing stored tokens.

        :param username:   The username to log off.
        :param force:      If True, log off regardless of whether the stored
                           username matches the given one.

        """

        auth_result = self.__handler.active_authentication()
        if not auth_result.logged_on:
            Logger.debug("User was not logged on.")
            return

        logged_on_user = auth_result.username
        if logged_on_user is not None and (force or logged_on_user == username):
            result = self.__handler.log_off(logged_on_user)
            if result:
                Logger.debug("Logged off successfully")
            else:
                Logger.error("Log off failed")
                XbmcWrapper.show_notification(
                    self.__channel_name, LanguageHelper.LogOffError,
                    notification_type=XbmcWrapper.Warning)

    def _get_username(self) -> Optional[str]:
        """ Read the stored username.

        :returns: The stored username, or None if not set.

        """

        if self.__channel_guid:
            username = AddonSettings.get_channel_setting(self.__channel_guid,
                                                         self.__username_setting_id,
                                                         store=LOCAL)
        else:
            username = AddonSettings.get_setting(self.__username_setting_id, store=LOCAL)
        Logger.debug("Read username from local settings: %s", self.__safe_log(username))
        return username

    def _set_username(self, username: Optional[str] = None) -> Optional[str]:
        """ Prompt for a username, then store and return it.

        :param username: Value to pre-fill in the keyboard.

        :returns: The entered username, or None if the prompt was cancelled.

        """

        label = LanguageHelper.get_localized_string(LanguageHelper.Username)
        username = XbmcWrapper.show_key_board(username, f"{self.__channel_name} - {label}")
        if not username:
            Logger.debug("Username prompt cancelled")
            return None

        Logger.debug("Storing username in local settings: %s", self.__safe_log(username))
        if self.__channel_guid:
            AddonSettings.set_channel_setting(self.__channel_guid,
                                              self.__username_setting_id,
                                              username,
                                              store=LOCAL)
        else:
            AddonSettings.set_setting(self.__username_setting_id, username, store=LOCAL)

        return username

    def _get_password(self) -> Optional[str]:
        """ Read the stored password.

        :returns: The stored password, or None if not set.

        """

        Logger.debug("Reading password from vault (setting_id=%s)", self.__password_setting_id)
        if not self.__password_setting_id:
            return None
        if self.__channel_guid:
            return Vault().get_channel_setting(self.__channel_guid, self.__password_setting_id)
        else:
            return Vault().get_setting(self.__password_setting_id)

    def _set_password(self) -> Optional[str]:
        """ Prompt for a password, store it in the Vault, then return the stored value.

        :returns: The password now in the Vault, or None if no value is stored
                  (e.g. first-time setup with the keyboard cancelled).

        """

        label = LanguageHelper.get_localized_string(LanguageHelper.Password)
        heading = "{} - {}".format(self.__channel_name, label)
        Logger.debug("Prompting and storing password in vault (setting_id=%s)", self.__password_setting_id)
        if self.__channel_guid:
            Vault().set_channel_setting(self.__channel_guid, self.__password_setting_id, heading)
        else:
            Vault().set_setting(self.__password_setting_id, heading)

        return self._get_password()

    def __safe_log(self, text: Optional[str]) -> Optional[str]:
        """ Obfuscate a string for logging by masking every odd-positioned character.

        :param text:    The string to obfuscate, or None.
        :returns:       Obfuscated string, or None if input was empty/None.

        """

        if not text:
            return None
        return "".join([text[i] if i % 2 == 0 else "*" for i in range(0, len(text))])
