# SPDX-License-Identifier: GPL-3.0-or-later
""" NLZIET channel for Retrospect. """

import json
import time
import xbmc

from typing import ClassVar, Dict, List, Optional, final

from resources.lib import chn_class
from resources.lib.addonsettings import AddonSettings, LOCAL
from resources.lib.authentication.authenticator import Authenticator
from resources.lib.authentication.nlziethandler import NLZIETHandler
from resources.lib.channelinfo import ChannelInfo
from resources.lib.helpers.jsonhelper import JsonHelper
from resources.lib.helpers.languagehelper import LanguageHelper
from resources.lib.logger import Logger
from resources.lib.mediaitem import MediaItem
from resources.lib.urihandler import UriHandler
from resources.lib.xbmcwrapper import XbmcWrapper


# App identification headers sent with every API request (auth + content).
# Values are faked to match the expected API client — not real app versions;
# these were current at implementation time (2025-06).
NLZIET_APP_NAME = "AndroidTv"
NLZIET_APP_VERSION = "5.65.5"
NLZIET_WEB_NAME = "WebApp"
NLZIET_WEB_VERSION = "6.1.25"

# Device identification headers — we don't run on real Android hardware,
# so brand/model/platform are "unknown".
NLZIET_BRAND_NAME = "unknown"
NLZIET_MODEL_NAME = "unknown"
NLZIET_PLATFORM_VERSION = "unknown"

# NLZIET API
API_CONTENT_URL = "https://api.nlziet.nl"

# V7 API
API_V7_APPCONFIG = "/v7/appconfig"

# V8 API
API_V8_PROFILE = "/v8/profile"

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

        self._handler: NLZIETHandler = NLZIETHandler()
        self._authenticator: Authenticator = Authenticator(
            self._handler,
            channel_name=self.channelName,
            channel_guid=self.guid,
            # These IDs must match the "id" fields in chn_nlziet.json.
            username_setting_id="nlziet_username",
            password_setting_id="nlziet_password",
            channel_icon=self.icon,
        )

        if self._authenticator.device_flow:
            app_name = NLZIET_APP_NAME
            app_version = NLZIET_APP_VERSION
        else:
            app_name = NLZIET_WEB_NAME
            app_version = NLZIET_WEB_VERSION
        self._nlziet_headers: dict = {
            "Accept": "application/json",
            "Nlziet-AppName": app_name,
            "Nlziet-AppVersion": app_version,
            "Nlziet-BrandName": NLZIET_BRAND_NAME,
            "Nlziet-ModelName": NLZIET_MODEL_NAME,
            "Nlziet-PlatformVersion": NLZIET_PLATFORM_VERSION,
            "Nlziet-DeviceCapabilities": "",
        }

        self._add_data_parser(
            "#mainlist",
            preprocessor=self.get_initial_folder_items,
        )

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


    @property
    def _request_headers(self) -> Dict[str, str]:
        """
        Returns the combined authentication headers and channel headers.

        :return: Merged header `dict`.
        """

        headers = dict(self._authenticator.authentication_headers)
        headers.update(self._nlziet_headers)
        return headers


    @property
    def loggedOn(self) -> bool:
        """
        True when a valid, non-expired access token is held by the authenticator.

        :return: - True if the authenticator holds a usable access token,
                 - False otherwise.
        """

        return self._authenticator.get_authentication_token() is not None


    @loggedOn.setter
    def loggedOn(self, state: bool) -> None:
        """
        Ignored — login state is derived from the authenticator token, not set directly.

        :param state: Ignored.
        """

        pass


    # -- Content parsers ---------------------------------------------------

    def get_initial_folder_items(self, data: str) -> tuple[str, list[MediaItem]]:
        """
        Populate the main entry list for this channel.

        :param str data: The retrieved data for the current item and URL.

        :return: A tuple of the data and a list of MediaItems.
        """

        items: list[MediaItem] = []

        return data, items


    # -- Profile handling -------------------------------------------------

    def _get_profile_id(self) -> str:
        """
        Return the stored profile ID, or empty string if none selected.

        :return: - Profile UUID string,
                 - ``""`` if no profile is selected.
        """

        Logger.debug("NLZIET: Getting profile ID ... ")
        profile_id = AddonSettings.get_setting("nlziet_profile_id", store=LOCAL) or ""
        Logger.debug(f"NLZIET: got Profile ID: {profile_id or '<none>'}")
        return profile_id


    def _set_profile_id(self, profile_id: str) -> None:
        """
        Store the selected profile ID.

        :param profile_id: Profile UUID to store.
        """

        Logger.debug(f"NLZIET: Setting profile ID: {profile_id}")
        AddonSettings.set_setting("nlziet_profile_id", profile_id, store=LOCAL)


    def _clear_profile_id(self) -> None:
        """ Clear the stored profile ID. """

        Logger.debug("NLZIET: Clearing profile ID")
        AddonSettings.set_setting("nlziet_profile_id", "", store=LOCAL)


    def _profile_type(self) -> str:
        """
        Return the profile type from the current access token's JWT claims.

        :return: - Profile type string,
                 - ``""`` if not available.
        """

        profile_type = self._handler.token_profile_type
        Logger.debug(f"NLZIET: Profile type: {profile_type or '<none>'}")
        return profile_type


    def _list_profiles(self) -> List[dict]:
        """
        Fetch the list of available profiles from the NLZIET API.

        :return: - List of profile dicts (id, displayName, type, color),
                 - [] on error.
        """

        response = UriHandler.open(
            self._prefix_urls(API_V8_PROFILE),
            additional_headers=self._request_headers,
            no_cache=True,
        )
        status = UriHandler.last_status()
        if status.error:
            Logger.error(f"NLZIET: Profile API failed: {status.code} {status.reason}")
            return []

        try:
            profiles = JsonHelper(response).get_value()
            return profiles if isinstance(profiles, list) else []
        except Exception:
            Logger.error("NLZIET: Failed to parse profile response", exc_info=True)
            return []


    def _select_profile(self) -> bool:
        """
        Let the user select a valid profile and set a token claim for it.

        :return: - True if a profile claim was successfully set,
                 - False otherwise.
        """

        profile_id = self._get_profile_id()
        if profile_id:
            # Fast path: if the current token already carries this profile's
            # claim (token was not refreshed since last run), skip the extra
            # grant round-trip entirely.
            if self._handler.token_profile_id == profile_id:
                Logger.debug(f"NLZIET: Token already scoped to profile {profile_id}, skipping claim")
                return True
            if self._handler.set_profile_claim(profile_id):
                return True
            Logger.warning(f"NLZIET: Stored profile {profile_id!r} is no longer valid; re-selecting")
            self._clear_profile_id()

        profiles = self._list_profiles()
        if not profiles:
            Logger.warning("NLZIET: No profiles available")
            XbmcWrapper.show_dialog("NLZIET",
                LanguageHelper.get_localized_string(LanguageHelper.NoProfilesAvailable))
            return False

        if len(profiles) == 1:
            profile_id = profiles[0]["id"]
            result = self._handler.set_profile_claim(profile_id)
            if result:
                self._set_profile_id(profile_id)
                Logger.info(f"NLZIET: Auto-selected only available profile: {profiles[0]['displayName']}")
            else:
                Logger.error(f"NLZIET: Failed to set profile claim for auto-selected profile: {profiles[0]['displayName']}")
            return result

        options = [p["displayName"] for p in profiles]
        label = LanguageHelper.get_localized_string(LanguageHelper.SelectProfile)
        selected = XbmcWrapper.show_selection_dialog(label, options)
        if selected < 0:
            Logger.info("NLZIET: Profile selection canceled")
            return False

        profile_id = profiles[selected]["id"]
        if self._handler.set_profile_claim(profile_id):
            self._set_profile_id(profile_id)
            return True

        return False


    # -- Settings actions --------------------------------------------------

    def switch_profile(self) -> None:
        """ Re-trigger profile selection from settings. """

        if not self.loggedOn:
            XbmcWrapper.show_dialog("NLZIET",
                LanguageHelper.get_localized_string(LanguageHelper.LoginFirst))
            return

        self._clear_profile_id()
        if self._select_profile():
            xbmc.executebuiltin("Container.Refresh()")
        else:
            self.log_off()


    def log_on(self) -> Optional[bool]:
        """
        Authenticate and set up the session.

        Delegates all authentication logic to the authenticator, which handles
        session resume, credential login, and device flow internally.

        :return: - True if authenticated and profile selected,
                 - False on failure,
                 - None if canceled.
        """

        if Channel.is_blocked:
            reason = Channel.blocked_reason or LanguageHelper.get_localized_string(LanguageHelper.UnknownError)
            XbmcWrapper.show_dialog("NLZIET",
                f"{LanguageHelper.get_localized_string(LanguageHelper.AccountBlocked)}\n\n({reason})")
            return False

        result = self._authenticator.log_on()
        if not result.logged_on:
            Logger.debug("NLZIET: Authentication failed.")
            return False

        if not result.existing_login:
            result = self._authenticator.active_authentication()
            welcome = LanguageHelper.get_localized_string(LanguageHelper.WelcomeUser)
            display_name = (result.username or
                            LanguageHelper.get_localized_string(LanguageHelper.UnknownUser))
            XbmcWrapper.show_dialog(self.channelName, welcome.replace("{0}", display_name))

        if self._select_profile():
            return True

        self.log_off()
        return False


    def log_off(self) -> None:
        """ Logoff for the channel. """

        self._authenticator.log_off("", force=True)

        msg = LanguageHelper.get_localized_string(LanguageHelper.LoggedOutSuccessfully)
        xbmc.executebuiltin("Action(ParentDir)")
        xbmc.executebuiltin("Container.Refresh()")
        XbmcWrapper.show_dialog("NLZIET", msg)

    # -- Background service ------------------------------------------------

    def _sync_appconfig(self) -> bool:
        """
        Sync the channel's shared config from the API.

        Repeated failure resets the poll interval; last-known block/update state is
        preserved rather than cleared, since we can't verify it changed.

        :return: - ``True`` on success,
                 - ``False`` on any network or parse failure.
        """

        Logger.debug(f"NLZIET: Syncing appconfig from {self.baseUrl}{API_V7_APPCONFIG}")

        raw = UriHandler.open(
            self._prefix_urls(API_V7_APPCONFIG + "?os=web&origin=app"),
            additional_headers=self._request_headers,
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
                    f"consecutive failures — resetting poll interval to default")
                Channel.service_interval = APPCONFIG_HEARTBEAT_DEFAULT
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
        self._authenticator.active_authentication()
