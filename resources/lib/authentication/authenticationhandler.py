# SPDX-License-Identifier: GPL-3.0-or-later
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional, final

from resources.lib.addonsettings import AddonSettings
from resources.lib.addonsettings import LOCAL
from resources.lib.authentication.authenticationresult import AuthenticationResult


@dataclass
class DeviceAuthData:
    """
    Device flow response returned by
    :meth:`AuthenticationHandler._start_device_authorization`.

    Construction validates the contract; consumers may use fields directly.
    """

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int
    qr_url: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.device_code, str) or not self.device_code:
            raise ValueError("device_code must be a non-empty string")
        if not isinstance(self.user_code, str) or not self.user_code:
            raise ValueError("user_code must be a non-empty string")
        if not isinstance(self.verification_uri, str) or not self.verification_uri:
            raise ValueError("verification_uri must be a non-empty string")
        if not isinstance(self.expires_in, int) or self.expires_in <= 0:
            raise ValueError("expires_in must be a positive int")
        if not isinstance(self.interval, int) or self.interval <= 0:
            raise ValueError("interval must be a positive int")
        if self.qr_url is not None and (not isinstance(self.qr_url, str) or not self.qr_url):
            raise ValueError("qr_url must be a non-empty string when provided")


class DeviceAuthResult(Enum):
    """ Outcome of a device authorization poll or dialog interaction. """

    SUCCESS = "success"
    PENDING = "pending"
    MANUAL = "manual"
    TIMEOUT = "timeout"
    CANCELED = "canceled"
    ERROR = "error"


class AuthenticationHandler(object):
    def __init__(self, realm: str, device_id: Optional[str]):
        """ Initializes a handler for the authentication provider

        :param realm:
        :param device_id:

        """

        if not realm:
            raise ValueError("Missing 'realm' initializer.")

        self._device_id = device_id
        self._realm = realm
        return

    @property
    def realm(self) -> str:
        return self._realm

    def log_on(self, username: str, password: str) -> AuthenticationResult:
        """ Peforms the logon of a user.

        :param username:    The username
        :param password:    The password to use

        :returns: a AuthenticationResult with the result of the log on

        """

        raise NotImplementedError

    def active_authentication(self) -> AuthenticationResult:
        """ Check if the user with the given name is currently authenticated.

        :returns: a AuthenticationResult with the account data.

        """

        raise NotImplementedError

    def log_off(self, username: str) -> bool:
        """ Check if the user with the given name is currently authenticated.

        :param username:    The username to log off

        :returns: Indication of success

        """

        raise NotImplementedError

    def _revoke_device_authorization(self, username: str) -> bool:
        """
        Called by :class:`Authenticator` to revoke a device flow session.

        Override to revoke device tokens and perform any provider-specific
        cleanup (such as deregistering the device from the user's account).
        Called while authentication tokens are still available, after
        :meth:`log_off` has run.

        Only called for device flow logins; credential logins use
        :meth:`_credential_log_off` instead.
        The default implementation is a no-op returning ``True``.

        :param username: The account username.

        :returns: ``True`` on success, ``False`` on failure.

        """

        return True

    @final
    @property
    def supports_device_authorization(self) -> bool:
        """ Whether this handler supports device authorization flow (RFC 8628).

        Derived automatically: True if the subclass overrides both
        _start_device_authorization and _poll_device_authorization, False otherwise.

        :return: True if the handler supports the full device authorization flow.
        :rtype: bool
        """

        return (type(self)._start_device_authorization
                is not AuthenticationHandler._start_device_authorization
                and type(self)._poll_device_authorization
                is not AuthenticationHandler._poll_device_authorization)

    @property
    def device_flow(self) -> bool:
        """
        Whether the handler is currently operating in device authorization flow.

        :return: ``True`` if device flow is active, ``False`` otherwise.
        """

        return False

    def _start_device_authorization(self, device_name: str) -> Optional[DeviceAuthData]:
        """ Start an interactive authentication session.

        :param device_name: A human-readable name for this device.

        :returns: A :class:`DeviceAuthData` dict, or None on failure.

        """

        raise NotImplementedError

    def _poll_device_authorization(self, device_code: str) -> DeviceAuthResult:
        """ Poll the status of an interactive authentication session.

        :param device_code: The device code returned by _start_device_authorization().

        :returns: One of ``PENDING``, ``SUCCESS``, ``TIMEOUT``, or ``ERROR``.

        """

        raise NotImplementedError

    @staticmethod
    def _parse_device_auth_data(auth_data: Mapping[str, Any]) -> Optional[DeviceAuthData]:
        """ Normalize a device authorization response into ``DeviceAuthData``.

        :param auth_data: Raw response mapping from the auth provider.

        :returns: A normalized ``DeviceAuthData`` dict, or ``None`` when
                  required fields are missing.

        """

        if ("device_code" not in auth_data or
            not auth_data["device_code"]):
            return None

        if ("user_code" not in auth_data or
            not auth_data["user_code"]):
            return None

        if ("verification_uri" not in auth_data or
            not auth_data["verification_uri"]):
            return None

        if ("expires_in" not in auth_data or
            auth_data["expires_in"] is None):
            return None

        if ("interval" not in auth_data or
            auth_data["interval"] is None):
            return None

        try:
            return DeviceAuthData(
                device_code=auth_data["device_code"],
                user_code=auth_data["user_code"],
                verification_uri=auth_data["verification_uri"],
                expires_in=auth_data["expires_in"],
                interval=auth_data["interval"],
                qr_url=auth_data.get("qr_url") or None,
            )
        except (TypeError, ValueError):
            return None

    def get_authentication_token(self) -> Optional[str]:
        """ Returns a token that can be used for authentication of the current session.

        The user needs to be logged in for this.

        :return: An authentication token.

        """

        raise NotImplementedError

    def _store_current_user_in_settings(self, username: str) -> None:
        """ Store the current user in the local settings.

        :param username: The currently authenticated user

        Can be used if there is no other means of retrieving the currently authenticated user.

        """

        store = AddonSettings.store(LOCAL)
        store.set_setting("{}:authenticated_user".format(self._realm), username)

    def _get_current_user_in_settings(self) -> str:
        """ Retrieves the current user in the local settings.

        Can be used if there is no other means of retrieving the currently authenticated user.

        """

        store = AddonSettings.store(LOCAL)
        return store.get_setting("{}:authenticated_user".format(self._realm), default=None)
