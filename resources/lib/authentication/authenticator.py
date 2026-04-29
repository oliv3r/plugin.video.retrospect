# SPDX-License-Identifier: GPL-3.0-or-later
from typing import Optional

from .authenticationhandler import AuthenticationHandler
from .authenticationresult import AuthenticationResult
from ..helpers.languagehelper import LanguageHelper
from ..logger import Logger
from ..vault import Vault
from ..xbmcwrapper import XbmcWrapper


class Authenticator(object):
    def __init__(self, handler: AuthenticationHandler,
                 channel_name: Optional[str] = None,
                 channel_guid: Optional[str] = None,
                 password_setting_id: Optional[str] = None):
        """ Main logic handler for authentication.

        :param handler:             The authentication handler to use.
        :param channel_name:        Channel display name used in dialogs.
        :param channel_guid:        Channel GUID for Vault password lookup.
        :param password_setting_id: Vault setting ID for the password.

        """

        if handler is None:
            raise ValueError("No authenication handler specified.")

        if not isinstance(handler, AuthenticationHandler):
            raise ValueError("Invalid authenication handler specified.")

        self.__handler = handler
        self.__channel_name = channel_name
        self.__channel_guid = channel_guid
        self.__password_setting_id = password_setting_id

    def log_on(self, username: str, password: Optional[str] = None) -> AuthenticationResult:
        """ Performs the logon of a user. Either with the specified password or via a lookup. Also
        logs off a previous user if the username has changed from previous logins.

        :param username:        The username
        :param password:        The password to use

        :returns: An indication of a successful login.

        """

        result = self.__handler.active_authentication()
        Logger.debug("Cached session present: %s", result.logged_on)

        logged_on_user = result.username
        if result.logged_on and logged_on_user:
            if username and logged_on_user.lower() == username.lower():
                Logger.info("Active session found for user (%s), skipping login.",
                            self.__safe_log(logged_on_user))
                return result

            Logger.warning("Different user requested (%s → %s). Logging off first.",
                           self.__safe_log(logged_on_user), self.__safe_log(username))
            self.__handler.log_off(logged_on_user)

        if result.error:
            Logger.error("Session check failed: %s", result.error)

            if result.error == "network_error":
                XbmcWrapper.show_dialog(self.__channel_name, LanguageHelper.NetworkLoginError)
            else:
                XbmcWrapper.show_dialog(self.__channel_name, result.error)

            return result

        if not username:
            Logger.warning("No username specified")
            return AuthenticationResult("", error="missing_username")

        Logger.info("Logging on user: %s", self.__safe_log(username))
        if password is None:
            Logger.info("Retrieving password for user: %s", self.__safe_log(username))
            v = Vault()
            if self.__channel_guid:
                password = v.get_channel_setting(self.__channel_guid, self.__password_setting_id)
            else:
                password = v.get_setting(self.__password_setting_id)

        if not password:
            Logger.error("No password specified")
            return AuthenticationResult("", error="missing_password")

        result = self.__handler.log_on(username, password)
        if result.error:
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

    def __safe_log(self, text: Optional[str]) -> Optional[str]:
        """ Obfuscate a string for logging by masking every odd-positioned character.

        :param text:    The string to obfuscate, or None.
        :returns:       Obfuscated string, or None if input was empty/None.

        """

        if not text:
            return None
        return "".join([text[i] if i % 2 == 0 else "*" for i in range(0, len(text))])
