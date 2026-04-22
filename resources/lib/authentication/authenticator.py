# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import xbmc
from typing import Optional

from .authenticationhandler import AuthenticationHandler, DeviceAuthData, DeviceAuthResult
from .authenticationresult import AuthenticationResult
from ..addonsettings import AddonSettings, LOCAL
from ..deviceauthdialog import DeviceAuthDialog
from ..helpers.languagehelper import LanguageHelper
from ..logger import Logger
from ..vault import Vault
from ..xbmcwrapper import XbmcWrapper

_AUTH_METHOD_CREDENTIALS = "credential_auth" # username/password login
_AUTH_METHOD_DEVICE = "device_auth"          # device flow with code login
_AUTH_SETTING_KEY = "authentication_method"  # addon-settings key for persisted auth method

_DEVICE_FLOW_REFRESH_INTERVAL = 0.5        # seconds between UI refresh ticks during device flow
_DEVICE_FLOW_STOP_TIMEOUT = 2.0            # seconds to wait for poll thread after dialog closes
_DEVICE_FLOW_NOTIFICATION_TIMEOUT = 30000  # milliseconds to show the notification to the user


class Authenticator(object):
    def __init__(self, handler: AuthenticationHandler,
                 channel_name: Optional[str] = None,
                 channel_guid: Optional[str] = None,
                 username_setting_id: Optional[str] = None,
                 password_setting_id: Optional[str] = None,
                 channel_icon: Optional[str] = None):
        """ Main logic handler for authentication.

        :param handler:             The authentication handler to use.
        :param channel_name:        Channel display name used in dialogs.
        :param channel_guid:        Channel GUID for credential lookup.
        :param username_setting_id: Settings ID for persisting the username.
        :param password_setting_id: Vault setting ID for the password.
        :param channel_icon:        Path to the channel icon for dialogs.

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
        self.__channel_icon = channel_icon

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

        result = self._auto_login(username, password)
        if result.logged_on or result.error == "network_error":
            return result

        if self.__handler.supports_device_authorization:
            result = self._device_manual_login(username)
        else:
            result = self._manual_login(username)

        if result.logged_on or result.error == "network_error":
            return result

        Logger.debug("Failed to log on: username=%s, password=%s",
                     self.__safe_log(username),
                     "******" if password else None)
        return AuthenticationResult("", error="login_failed")

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
            if self.__handler.device_flow:
                Logger.info("Active device-flow session found (%s), resuming.",
                            self.__safe_log(result.username))
                return result

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
            Logger.debug("No credentials configured, skipping headless login")
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

    def _device_manual_login(self, username: Optional[str] = None) -> AuthenticationResult:
        """ Run the device authorization flow with a progress dialog and retry logic.

        :param username: Last known username, to pass on as a pre-fill hint.

        :returns: The result of the login attempt; one of ``canceled``,
                  ``device_setup_failed``, ``aborted``, or a result delegated
                  from the manual fallback.

        """

        device_name = xbmc.getInfoLabel("System.FriendlyName") or "Kodi Retrospect"
        Logger.debug("Starting device authorization login for %s", device_name)

        monitor = xbmc.Monitor()
        while not monitor.abortRequested():
            result: Optional[DeviceAuthResult] = None

            device_auth = self.__handler._start_device_authorization(device_name)
            if device_auth:
                result = self._poll_with_progress(device_auth, monitor)
                Logger.debug("Device authorization poll result: %r", result)

            if result == DeviceAuthResult.SUCCESS:
                auth_result = self.__handler.active_authentication()
                self._set_auth_method(_AUTH_METHOD_DEVICE)
                return auth_result
            if result == DeviceAuthResult.MANUAL:
                auth_result = self._manual_login(username)
                if auth_result.logged_on or auth_result.error == "network_error":
                    return auth_result
                continue  # failed or canceled → restart with fresh device flow
            if result == DeviceAuthResult.TIMEOUT:
                Logger.warning("Device authorization timed out, restarting")
                XbmcWrapper.show_notification(self.__channel_name, LanguageHelper.DeviceCodeExpired,
                                              notification_type=XbmcWrapper.Warning,
                                              display_time=_DEVICE_FLOW_NOTIFICATION_TIMEOUT)
                continue  # device code expired → restart
            if result == DeviceAuthResult.CANCELED:
                Logger.debug("Device authorization canceled by user")
                return AuthenticationResult("", error="canceled")
            if result == DeviceAuthResult.ERROR:
                Logger.error("Device authorization failed with an error")
                XbmcWrapper.show_dialog(self.__channel_name,
                                        LanguageHelper.get_localized_string(LanguageHelper.ConnectionError))
                return AuthenticationResult("", error="device_setup_failed")

            Logger.error("Device authorization login failed with result: %r", result)
            XbmcWrapper.show_dialog(self.__channel_name,
                                    LanguageHelper.get_localized_string(LanguageHelper.DeviceSetupFailed))
            return AuthenticationResult("", error="device_setup_failed")

        return AuthenticationResult("", error="aborted")  # Kodi shutdown requested

    def _poll_with_progress(self, auth_data: DeviceAuthData,
                            monitor: xbmc.Monitor) -> DeviceAuthResult:
        """ Poll device flow with a progress dialog.

        :param auth_data:   The device flow response from _start_device_authorization().
        :param monitor:     Kodi monitor used to detect Kodi shutdown.

        :returns: The terminal :class:`DeviceAuthResult` from the dialog.
                  (see :class:`~resources.lib.deviceauthdialog.DeviceAuthDialog`)

        """

        Logger.debug("Starting device flow poll (code=%s, expires_in=%s)",
                     auth_data.user_code, auth_data.expires_in)

        dialog = DeviceAuthDialog(
            logo_path=self.__channel_icon or None,
            qr_url=auth_data.qr_url,
            visit_url=auth_data.verification_uri,
            code=auth_data.user_code,
            timeout=auth_data.expires_in,
        )

        def _poll_worker() -> None:
            while not dialog.stop_event.wait(_DEVICE_FLOW_REFRESH_INTERVAL):
                if monitor.abortRequested():
                    Logger.debug("Kodi abort requested during device flow poll")
                    dialog.close_with(DeviceAuthResult.CANCELED)
                    return

                dialog.update_progress()

                result = self.__handler._poll_device_authorization(auth_data.device_code)
                Logger.debug("Device flow poll result: %s", result)
                if result != DeviceAuthResult.PENDING:
                    dialog.close_with(result)
                    return

        poll_thread = threading.Thread(target=_poll_worker, daemon=True)
        poll_thread.start()
        dialog.doModal()

        # On ``manual`` login, skip join() -- stop_event is set and the daemon
        # thread exits as soon as the current poll request returns.
        if dialog.result != DeviceAuthResult.MANUAL:
            poll_thread.join(timeout=_DEVICE_FLOW_STOP_TIMEOUT)

        Logger.debug("Device flow poll completed with result: %s", dialog.result)
        return dialog.result or DeviceAuthResult.ERROR

    def _manual_login(self, username: Optional[str] = None) -> AuthenticationResult:
        """ Prompt for username and password interactively, then attempt a headless login.

        Pre-fills the username field with the last known value so the user can
        correct it if needed. Credentials are stored at prompt time.

        :param username: Value to pre-fill in the username keyboard.

        :returns: The result of the login attempt; ``missing_username`` /
                  ``missing_password`` if the keyboard was cancelled.

        """

        Logger.debug("Manual login for: %s", self.__safe_log(username) if username else "new user")
        username = self._set_username(username)
        if not username:
            return AuthenticationResult("", error="missing_username")

        password = self._set_password()
        if not password:
            return AuthenticationResult("", error="missing_password")

        return self._headless_login(username, password)

    def _headless_login(self, username: str, password: str) -> AuthenticationResult:
        """ Perform a direct credential login via the handler.

        :param username:    The username to log on with.
        :param password:    The password to use.

        :returns: The result of the login attempt.

        """

        Logger.debug("Headless login for: %s", self.__safe_log(username))
        result = self.__handler._credential_log_on(username, password)
        if result.logged_on:
            self._set_auth_method(_AUTH_METHOD_CREDENTIALS)
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

    def log_off(self, username: Optional[str] = None, force: bool = True) -> None:
        """ Logs off the currently authenticated user, clearing stored tokens.

        :param username:   The username to log off. If omitted, logs off whoever
                           is currently authenticated.
        :param force:      If True, log off regardless of whether the stored
                           username matches the given one.

        """

        auth_result = self.__handler.active_authentication()
        if not auth_result.logged_on:
            Logger.debug("User was not logged on.")
            return

        logged_on_user = auth_result.username

        if self.device_flow:
            result = self.__handler._revoke_device_authorization(logged_on_user)
            if result:
                Logger.debug("Device authorization revoked successfully")
            else:
                Logger.error("Device authorization revocation failed")
                XbmcWrapper.show_notification(
                    self.__channel_name, LanguageHelper.RevocationError,
                    notification_type=XbmcWrapper.Warning)

        if force or logged_on_user == username:
            result = self.__handler._credential_log_off(logged_on_user)
            if result:
                Logger.debug("Credential log off succeeded")
            else:
                Logger.error("Credential log off failed")
                XbmcWrapper.show_notification(
                    self.__channel_name, LanguageHelper.LogOffError,
                    notification_type=XbmcWrapper.Warning)
        else:
            Logger.warning("Username mismatch, skipping credential log off")

        self._clear_auth_method()

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

    @property
    def device_flow(self) -> bool:
        """ Whether the last successful login used the device authorization flow. """

        return self._get_auth_method() == _AUTH_METHOD_DEVICE

    def _get_auth_method(self) -> Optional[str]:
        """ Read the stored authentication method.

        :returns: The stored authentication method, or None if not set.

        """

        if self.__channel_guid:
            return AddonSettings.get_channel_setting(self.__channel_guid,
                                                     _AUTH_SETTING_KEY,
                                                     store=LOCAL)
        else:
            return AddonSettings.get_setting(_AUTH_SETTING_KEY, store=LOCAL)

    def _set_auth_method(self, method: str) -> None:
        """ Persist the authentication method used for the current session.

        :param method:  ``_AUTH_METHOD_CREDENTIALS`` or ``_AUTH_METHOD_DEVICE``.

        """

        if self.__channel_guid:
            AddonSettings.set_channel_setting(self.__channel_guid, _AUTH_SETTING_KEY, method,
                                              store=LOCAL)
        else:
            AddonSettings.set_setting(_AUTH_SETTING_KEY, method, store=LOCAL)

    def _clear_auth_method(self) -> None:
        """ Clear the stored authentication method (e.g. on log-off). """

        self._set_auth_method("")

    def __safe_log(self, text: Optional[str]) -> Optional[str]:
        """ Obfuscate a string for logging by masking every odd-positioned character.

        :param text:    The string to obfuscate, or None.
        :returns:       Obfuscated string, or None if input was empty/None.

        """

        if not text:
            return None
        return "".join([text[i] if i % 2 == 0 else "*" for i in range(0, len(text))])
