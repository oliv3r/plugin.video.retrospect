# SPDX-License-Identifier: GPL-3.0-or-later
""" NLZIET channel for Retrospect. """

import json
import time

from typing import ClassVar, Optional, final

from resources.lib import chn_class
from resources.lib.channelinfo import ChannelInfo
from resources.lib.helpers.languagehelper import LanguageHelper
from resources.lib.logger import Logger
from resources.lib.urihandler import UriHandler
from resources.lib.xbmcwrapper import XbmcWrapper


# NLZIET API
API_CONTENT_URL = "https://api.nlziet.nl"

# V7 API
API_V7_APPCONFIG = "/v7/appconfig"

# NLZIET channel defaults
APPCONFIG_HEARTBEAT_DEFAULT = 90  # seconds
APPCONFIG_SYNC_MAX_AGE = 240      # seconds (15 minutes) before a stale appconfig resets to defaults
APPCONFIG_SYNC_MAX_FAIL = 10      # consecutive failures before resetting to defaults
APPCONFIG_INIT_RETRY_MAX = 3      # interactive retries shown to the user during init


@final
class Channel(chn_class.Channel):
    """ NLZIET channel for Retrospect. """

    service_interval: Optional[int] = APPCONFIG_HEARTBEAT_DEFAULT
    """ Appconfig heartbeat interval in seconds. """

    is_blocked: bool = False
    """ Set to ``True`` when the server reports ``isAppBlocked`` in the appconfig. """

    blocked_reason: str = ""
    """ Human-readable block reason from the API (``appBlockedReason``), or empty string. """

    is_update_required: bool = False
    """ Set to ``True`` when the server reports ``isUpdateRequired`` in the appconfig. """

    update_reason: str = ""
    """ Human-readable update reason from the API (``updateText``), or empty string. """

    _appconfig_fail_count: ClassVar[int] = 0
    """ Consecutive failed ``_sync_appconfig`` calls since the last success. """

    _appconfig_last_synced_at: ClassVar[float] = 0.0
    """ ``time.time()`` of the last successful appconfig sync; ``0.0`` if never synced. """


    def __init__(self, channel_info: ChannelInfo) -> None:
        """
        Initialization of the class.

        All class variables should be instantiated here and this method should
        not be overridden by any derived classes.

        :param channel_info: The channel info object to base this channel on.
        """

        chn_class.Channel.__init__(self, channel_info)

        self.baseUrl = API_CONTENT_URL
        self.mainListUri = "#mainlist"
        self.noImage = channel_info.icon

        for _ in range(APPCONFIG_INIT_RETRY_MAX):
            if self._sync_appconfig():
                break
            if not XbmcWrapper.show_yes_no(
                "NLZIET",
                LanguageHelper.get_localized_string(LanguageHelper.ServiceUnavailable),
            ):
                raise RuntimeError("NLZIET: Service unavailable.")
        else:
            XbmcWrapper.show_dialog(
                "NLZIET",
                LanguageHelper.get_localized_string(LanguageHelper.ServiceUnavailableExhausted),
            )
            raise RuntimeError("NLZIET: Service unavailable.")


    # -- Background service ------------------------------------------------

    def _sync_appconfig(self) -> bool:
        """
        Fetch and apply the appconfig from the API.

        :return: - ``True`` on success,
                 - ``False`` on any network or parse failure.
        """

        Logger.debug(f"NLZIET: Syncing appconfig from {self.baseUrl}{API_V7_APPCONFIG}")

        raw = UriHandler.open(
            self._prefix_urls(API_V7_APPCONFIG + "?os=web&origin=app"),
        )
        status = UriHandler.last_status()
        if status.error:
            Logger.error(f"NLZIET: Could not fetch appconfig: {status.code} {status.reason}")
            Channel._appconfig_fail_count += 1
            if (Channel._appconfig_fail_count >= APPCONFIG_SYNC_MAX_FAIL or
                (Channel._appconfig_last_synced_at > 0 and
                 time.time() - Channel._appconfig_last_synced_at >= APPCONFIG_SYNC_MAX_AGE)):
                Logger.warning(
                    f"NLZIET: Appconfig stale after {Channel._appconfig_fail_count} "
                    f"consecutive failures — resetting to defaults")
                Channel.service_interval = APPCONFIG_HEARTBEAT_DEFAULT
                Channel.is_blocked = False
                Channel.blocked_reason = ""
                Channel.is_update_required = False
                Channel.update_reason = ""
                Channel._appconfig_fail_count = 0
                Channel._appconfig_last_synced_at = 0.0
                XbmcWrapper.show_notification(
                    "NLZIET", "Service unavailable", notification_type=XbmcWrapper.Error)
            return False

        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            Logger.warning("NLZIET: Could not parse appconfig response")
            return False

        if not isinstance(data, dict):
            Logger.warning("NLZIET: Appconfig response is not a JSON object")
            return False

        Channel._appconfig_fail_count = 0
        Channel._appconfig_last_synced_at = time.time()

        Channel.service_interval = data.get("heartbeatInterval") or APPCONFIG_HEARTBEAT_DEFAULT
        Logger.debug(f"NLZIET: Next heartbeat in {Channel.service_interval}s")

        Channel.is_blocked = data.get("isAppBlocked", False)
        Channel.blocked_reason = data.get("appBlockedReason") or ""
        if Channel.is_blocked:
            Logger.warning(f"NLZIET: App is blocked - {Channel.blocked_reason!r}")

        Channel.is_update_required = data.get("isUpdateRequired", False)
        Channel.update_reason = data.get("updateText") or ""
        if Channel.is_update_required:
            Logger.warning(f"NLZIET: API update required - {Channel.update_reason!r}")

        return True


    def service_update(self) -> None:
        """ Periodic background callback. """

        self._sync_appconfig()
