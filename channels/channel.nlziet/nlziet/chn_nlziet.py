# SPDX-License-Identifier: GPL-3.0-or-later
""" NLZIET channel for Retrospect. """

import json
import time
import xbmc
from typing import Any, ClassVar, Dict, List, Optional, Tuple, final
from urllib.parse import parse_qs, urlparse

from resources.lib import chn_class, contenttype, mediatype
from resources.lib.addonsettings import AddonSettings, LOCAL
from resources.lib.authentication.authenticator import Authenticator
from resources.lib.authentication.nlziethandler import NLZIETHandler
from resources.lib.channelinfo import ChannelInfo
from resources.lib.helpers.jsonhelper import JsonHelper
from resources.lib.helpers.languagehelper import LanguageHelper
from resources.lib.logger import Logger
from resources.lib.mediaitem import FolderItem, MediaItem, MediaStream
from resources.lib.streams.mpd import Mpd
from resources.lib.urihandler import UriHandler
from resources.lib.xbmcwrapper import XbmcWrapper


# App identification headers sent with every API request (auth + content).
# Values are faked to match the expected API client — not real app versions;
# these were current at implementation time (2025-03).
NLZIET_APP_NAME = "AndroidTv"
NLZIET_APP_VERSION = "5.65.5"
NLZIET_PLAYER_NAME_DEVICE = "NLZIETAndroidTVExoPlayer"
NLZIET_PLAYER_NAME_WEB = "BitmovinWeb"
NLZIET_WEB_NAME = "WebApp"
NLZIET_WEB_VERSION = "5.65.9"

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

# V9 API
API_V9_EPG_LIVE = "/v9/epg/programlocations/live"
API_V9_ITEM_DETAIL = "/v9/item/detail"
API_V9_LIVE_HANDSHAKE = "/v9/stream/handshake"

# NLZIET channel defaults
APPCONFIG_CACHE_KEY = "nlziet_appconfig"
APPCONFIG_HEARTBEAT_DEFAULT = 90  # seconds

# Indices into the programLocations list returned by the live EPG endpoint.
PROGRAM_LOCATION_CURRENT = 0

# Subscription feature strings that map to a premium (add-on package) item.
# Extend this set when NLZIET introduces additional package tiers.
_PREMIUM_PACKAGES: frozenset = frozenset({
    "ExtraChannelPackage1",
})

ICON_OVERLAY_NONE = 0  # xbmcgui.ICON_OVERLAY_NONE


@final
class Channel(chn_class.Channel):
    """ NLZIET channel for Retrospect. """

    service_interval: int = APPCONFIG_HEARTBEAT_DEFAULT
    """ Appconfig heartbeat interval in seconds. """

    is_blocked: bool = False
    """ Set to ``True`` when the server reports ``isAppBlocked`` in the appconfig. """

    blocked_reason: str = ""
    """ Human-readable block reason from the API (``appBlockedReason``), or empty string. """

    is_update_required: bool = False
    """ Set to ``True`` when the server reports ``isUpdateRequired`` in the appconfig. """

    update_reason: str = ""
    """ Human-readable update reason from the API (``updateText``), or empty string. """

    _item_detail_cache: ClassVar[Dict[str, dict]] = {}
    """
    In-process RAM cache mapping contentItemId → content dict from v9/item/detail.

    Avoids one HTTP round-trip per channel on every channel-list refresh within
    the same Kodi session. Invalidated only by process restart.
    """

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
        self.mainListContentType = contenttype.NONE
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

        # Static Nlziet-* app-identification headers for API calls to api.nlziet.nl.
        if self._authenticator.device_flow:
            app_name = NLZIET_APP_NAME
            app_version = NLZIET_APP_VERSION
            self._player_name = NLZIET_PLAYER_NAME_DEVICE
        else:
            app_name = NLZIET_WEB_NAME
            app_version = NLZIET_WEB_VERSION
            self._player_name = NLZIET_PLAYER_NAME_WEB
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

        self._add_data_parser(
            self._prefix_urls(API_V9_EPG_LIVE),
            creator=self.create_live_channel_item,
            json=True,
            name="Live TV channels",
            parser=["data"],
            requires_logon=True,
            updater=self.update_live_item,
        )


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

        live_tv = FolderItem(
            LanguageHelper.get_localized_string(LanguageHelper.LiveTv),
            self._prefix_urls(API_V9_EPG_LIVE),
            content_type=contenttype.NONE,
        )
        live_tv.isLive = True
        live_tv.thumb = self.noImage
        items.append(live_tv)

        return data, items


    def _get_server_time(self) -> float:
        """
        Get the current Unix timestamp in seconds, preferring the server clock.

        Falls back to ``time.time()`` when the API call fails or the server
        timestamp deviates from the local clock by more than
        ``EPG_MAX_SERVER_TIME_DRIFT`` seconds.

        :returns:   Unix timestamp as a float.
        """

        raw = UriHandler.open(self._prefix_urls(API_V7_CURRENT_TIME))
        status = UriHandler.instance().status
        if not status.error:
            server_ts = float(raw or 0.0) / _MS_PER_SECOND
            if abs(server_ts - time.time()) <= EPG_MAX_SERVER_TIME_DRIFT:
                return server_ts

        return time.time()


    # -- Shared metadata helpers ------------------------------------------

    def _apply_broadcast_date(self, item: MediaItem,
                              iso_str: Optional[str]) -> None:
        """
        Set the broadcast date on ``item`` from an ISO 8601 string.

        No-op when ``iso_str`` is absent or cannot be parsed.

        :param item:     The item to update.
        :param iso_str:  ISO 8601 datetime string.
        """

        if not iso_str:
            return

        try:
            dt = datetime.fromisoformat(iso_str)
            item.set_date(
                year=dt.year,
                month=dt.month,
                day=dt.day,
                hour=dt.hour,
                minutes=dt.minute,
                seconds=dt.second,
            )
        except (ValueError, TypeError):
            pass


    def _apply_expire_date(self, item: MediaItem,
                           iso_str: Optional[str]) -> None:
        """
        Set the expiry datetime on ``item`` from an ISO 8601 string.

        No-op when ``iso_str`` is absent or cannot be parsed.

        :param item:     The item to update.
        :param iso_str:  ISO 8601 datetime string.
        """

        if not iso_str:
            return

        try:
            dt = datetime.fromisoformat(iso_str)
            item.set_expire_datetime(
                year=dt.year,
                month=dt.month,
                day=dt.day,
                hour=dt.hour,
                minutes=dt.minute,
                seconds=dt.second,
            )
        except (ValueError, TypeError):
            pass


    def _fetch_and_cache_detail(self, content_item_id: str) -> dict:
        """
        Fetch ``/v9/item/detail/{id}``, store in the class-level cache, and
        return the ``content`` dict.

        :param content_item_id:  Content item ID to fetch.

        :returns: - ``content`` dict, or
                  - ``{}`` on any error.
        """

        raw_detail = UriHandler.open(
            self._prefix_urls(f"{API_V9_ITEM_DETAIL}/{content_item_id}"),
            additional_headers=self._request_headers,
        )
        status = UriHandler.instance().status
        if status.error:
            return {}
        try:
            detail = json.loads(raw_detail).get("content") or {}
        except (ValueError, TypeError):
            return {}
        Channel._item_detail_cache[content_item_id] = detail

        return detail


    def _add_item_detail(self, item: MediaItem, content_item_id: str) -> None:
        """ Enrich a MediaItem with v9/item/detail metadata.

        Fetches (or retrieves from cache) the detail record for
        ``content_item_id`` and applies duration, genre, NICAM, series art,
        and description to ``item`` in place.

        :param item:             The item to enrich.
        :param content_item_id:  Content item ID used to query the detail API.

        """

        if content_item_id not in Channel._item_detail_cache:
            self._fetch_and_cache_detail(content_item_id)

        detail = Channel._item_detail_cache.get(content_item_id) or {}
        if not detail:
            return

        content_type = detail.get("type") or ""
        if content_type == "Movie":
            item.media_type = mediatype.MOVIE
        elif content_type == "Episode":
            item.media_type = mediatype.EPISODE

        if "durationInSeconds" in detail:
            item.set_info_label(MediaItem.LabelDuration, int(detail["durationInSeconds"]))

        genres = detail.get("genres") or []
        genre_str = ", ".join(g["name"] for g in genres if g.get("name"))
        if genre_str:
            item.set_info_label("Genre", genre_str)

        nicam = detail.get("nicam") or {}
        nicam_age = nicam.get("age") or ""
        if nicam_age and nicam_age != "AllAges":
            item.set_info_label("Mpaa", f"NICAM {nicam_age}+")

        series = detail.get("series") or {}
        series_title = series.get("title") or ""
        if series_title:
            item.tv_show_title = series_title

        broadcasters = detail.get("broadcasters") or []
        broadcaster_logo = (broadcasters[0].get("logoUrl") or "") if broadcasters else ""

        series_image = series.get("image") or {}
        detail_image = detail.get("image") or {}
        item.fanart = (series_image.get("landscapeUrl") or detail_image.get("landscapeUrl")
                       or broadcaster_logo or item.fanart)
        item.poster = (series_image.get("portraitUrl") or detail_image.get("portraitUrl")
                       or broadcaster_logo or item.poster)

        if not item.clearlogo:
            item.clearlogo = detail.get("logoUrl") or ""

        description = detail.get("description") or ""
        if description:
            episode_title = detail.get("title") or ""
            header = "\n".join(filter(None, [
                f"[B]{item.tv_show_title}[/B]" if item.tv_show_title else "",
                f"[I]{episode_title}[/I]" if episode_title else "",
            ]))
            item.description = f"{header}\n\n{description}" if header else description

        self._apply_expire_date(item, detail.get("availableUntil"))


    def _build_program_item(self, channel: Optional[dict],
                            program: Optional[dict],
                            stream_url: str = "") -> Optional[MediaItem]:
        """
        Build and enrich a program MediaItem from a channel and program dict.

        Enriches the item with v9/item/detail metadata (when ``contentItemId``
        is present in ``program``) and adds channel-level identity onto the item.

        :param channel:     The ``channel`` dict from the result set.
        :param program:     A ``program`` from ``programLocations``;
                            may be ``None`` or ``{}`` to skip EPG metadata.
        :param stream_url:  Stream URL for the item.

        :return: - A populated MediaItem, or
                 - ``None`` on error.
        """

        if not channel:
            return None

        program = program or {}

        channel_content = channel.get("content", {})
        channel_id = channel_content.get("id")
        channel_title = channel_content.get("title", "")
        if (not channel_id or
            not channel_title):
            return None

        channel_logos = channel_content.get("logo") or {}
        channel_logo = channel_logos.get("normalUrl") or ""

        title = program.get("title", "")
        item = MediaItem(title, stream_url, media_type=mediatype.VIDEO)
        if program.get("isMovie"):
            item.media_type = mediatype.MOVIE
        item.name = channel_title
        item.tv_show_title = title
        item.isDrmProtected = True
        item.isGeoLocked = True
        if channel.get("missingSubscriptionFeature") in _PREMIUM_PACKAGES:
            item.isPaid = True
        item.set_info_label("Overlay", ICON_OVERLAY_NONE)
        item.icon = self.icon
        item.thumb = channel_logo or self.noImage
        item.clearlogo = channel_logos.get("flatUrl") or channel_logo
        if channel_logo:
            item.metaData["logo_url"] = channel_logo
        item.metaData["channel_id"] = channel_id
        if program.get("assetId"):
            item.metaData["asset_id"] = program["assetId"]
        content_provider = channel_content.get("contentProvider", "")
        if content_provider:
            item.metaData["content_provider"] = content_provider

        prog_image = program.get("image") or {}
        item.fanart = prog_image.get("landscapeUrl") or channel_logo or self.fanart
        item.poster = prog_image.get("portraitUrl") or channel_logo or self.poster

        content_item_id = program.get("contentItemId") or ""
        if content_item_id:
            self._add_item_detail(item, content_item_id)

        self._apply_broadcast_date(item, program.get("firstBroadcast") or program.get("startAt"))

        item.HttpHeaders = self._nlziet_headers
        return item


    # -- Live channel items ------------------------------------------------

    def _report_stream_errors(self, name: str, errors: Any) -> None:
        """
        Log errors and show a user-facing dialog for known error types.

        Each entry in ``errors`` is either a plain string (logged only) or a
        dict with ``type``, ``message``, and an optional ``data`` payload::

          {
            "type": "MaximumStreamsReached",
            "message": "...",
            "data": {"maximumNumberOfStreams": 2}
          }

        :param str name:    Channel name or URL, used in log messages.
        :param Any errors:  Flat strings or typed dicts from the stream response.
        """

        if isinstance(errors, dict):
            errors = [e for v in errors.values()
                      for e in (v if isinstance(v, list) else [v])]
        if not errors:
            return

        for error in errors:
            if isinstance(error, str):
                Logger.error(f"Stream configuration error for {name}: {error}")
                continue

            error_type = error.get("type", "")
            error_msg = error.get("message", "")
            Logger.error(f"Stream configuration error for {name}: {error_type} - {error_msg}")

            if error_type == "MaximumStreamsReached":
                max_streams = str(error.get("data", {}).get("maximumNumberOfStreams", "?"))
                msg = LanguageHelper.get_localized_string(LanguageHelper.MaxStreamsReached).replace("{0}", max_streams)
            elif error_type == "MissingSubscriptionFeature":
                msg = LanguageHelper.get_localized_string(LanguageHelper.MissingSubscription)
            elif error_type == "Unauthorized":
                msg = LanguageHelper.get_localized_string(LanguageHelper.UnauthorizedStream)
                if not self.loggedOn:
                    login = LanguageHelper.get_localized_string(LanguageHelper.LoginFirst)
                    msg = f"{msg}\n\n{login}"
            elif error_type == "ChannelNotFound":
                msg = LanguageHelper.get_localized_string(LanguageHelper.ChannelUnavailable)
            elif error_type == "InvalidAsset":
                msg = LanguageHelper.get_localized_string(LanguageHelper.ContentNotPlayable)
            else:
                continue

            XbmcWrapper.show_dialog("NLZIET", msg)


    def _configure_drm_stream(self, url: str,
                              manifest_update: bool = False) -> Optional[MediaStream]:
        """
        Fetch a stream descriptor from ``url`` and configure it for playback.

        Calls the given API endpoint, extracts the DASH manifest URL and
        Widevine licence details from the response, and returns a configured
        stream.  Works for live, VoD, and catchup content — the caller is
        responsible for supplying the correct endpoint URL.

        :param str url:              Stream API URL with a manifest URL and DRM.
        :param bool manifest_update: Full manifest refresh on each update cycle.
                                     For live streams on pre-Omega Kodi only;
                                     Omega+ enables this automatically for
                                     dynamic DASH content.

        :return: Configured stream, or ``None`` if the request or parse failed.
        """

        data = UriHandler.open(
            url,
            additional_headers=self._request_headers,
            no_cache=True,
        )
        status = UriHandler.instance().status
        if status.error:
            Logger.error(f"Empty stream response for: {url}")
            return None

        try:
            json_data = JsonHelper(data)
        except (json.JSONDecodeError, ValueError):
            Logger.error(f"NLZIET: Invalid JSON in stream response for '{url}'", exc_info=True)
            return None

        errors = json_data.get_value("errors", fallback=None)
        if errors:
            self._report_stream_errors(url, errors)
            return None

        mpd_url = json_data.get_value("manifestUrl", fallback=None)
        if not mpd_url:
            Logger.error(f"No manifest URL in stream response for: {url}")
            return None

        stream = MediaStream(mpd_url)

        manifest_update_params = None
        license_key = None

        drm = json_data.get_value("drm", fallback={})
        license_url = drm.get("licenseUrl") if drm else None
        if license_url:
            license_headers = drm.get("headers", {})
            license_key = Mpd.get_license_key(license_url, key_headers=license_headers)
            if manifest_update:
                manifest_update_params = "full"

        Mpd.set_input_stream_addon_input(
            stream,
            license_key=license_key,
            manifest_update_params=manifest_update_params
        )

        return stream


    def _get_live_restart_padding(self) -> int:
        """
        Get padding offset from configuration settings.

        :return: ``liveStreamRestartStartPadding`` from the cached appconfig, or 0.
        """

        try:
            data = json.loads(AddonSettings.get_setting(APPCONFIG_CACHE_KEY, store=LOCAL) or "{}")
            return int(data.get("liveStreamRestartStartPadding") or 0)
        except (ValueError, TypeError):
            return 0


    def update_live_item(self, item: MediaItem) -> MediaItem:
        """
        Fetch the DASH stream URL for a live channel.

        :param MediaItem item: The item to update with stream info.

        :return: The updated item.
        """

        Logger.debug(f"Updating live stream for: {item.name}")

        channel_id = parse_qs(urlparse(item.url).query).get("channel", [""])[0]
        if not channel_id:
            Logger.error(f"No channel ID in URL for: {item.name}")
            return item

        stream_url = self._prefix_urls(
            f"{API_V9_LIVE_HANDSHAKE}?channel={channel_id}"
            f"&playerName={self._player_name}"
            "&drmType=Widevine"
            "&sourceType=Dash"
            "&context=Live"
            "&offsetType=Live"
        )

        live_offset = 0
        if self._get_setting("nlziet_restart_padding") == "true":
            padding = self._get_live_restart_padding()
            slider = int(self._get_setting("nlziet_live_start_offset") or "0")
            live_offset = max(0, padding + slider)
            if live_offset > 0:
                stream_url += f"&startOffsetInSeconds={live_offset}"

        stream = self._configure_drm_stream(stream_url, True)
        if stream:
            if live_offset > 0:
                stream.add_property(
                    "inputstream.adaptive.manifest_config",
                    json.dumps({"live_offset": live_offset})
                )
            item.streams.append(stream)
            item.complete = True

        return item


    def create_live_channel_item(self, result_set: dict) -> Optional[MediaItem]:
        """
        Create a MediaItem for a live TV channel.

        :param dict result_set: A single entry from the ``data`` array of the
            ``/v9/epg/programlocations/live`` response.

        :return: A playable MediaItem or None.
        """

        channel = result_set.get("channel")
        channel_id = (channel or {}).get("content", {}).get("id")
        if not channel_id:
            return None

        live_url = self._prefix_urls(f"{API_V9_EPG_LIVE}?channel={channel_id}")
        program_locations = result_set.get("programLocations", [])

        item = self._build_program_item(
            channel,
            program_locations[PROGRAM_LOCATION_CURRENT].get("content") if program_locations else None,
            live_url
        )
        if item is None:
            return None

        item.isLive = True
        return item


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
        status = UriHandler.instance().status
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


    # -- Appconfig cache ---------------------------------------------------

    def _sync_appconfig(self) -> None:
        """ Fetch and cache the appconfig from the API. """

        Logger.debug(f"NLZIET: Syncing appconfig from {self.baseUrl}{API_V7_APPCONFIG}")

        raw = UriHandler.open(self._prefix_urls(API_V7_APPCONFIG + "?os=web&origin=app"))
        status = UriHandler.instance().status
        if status.error:
            Logger.error(f"NLZIET: Could not fetch appconfig: {status.code} {status.reason}")
            return
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            Logger.warning("NLZIET: Could not parse appconfig response")
            return
        if not isinstance(data, dict):
            Logger.warning("NLZIET: Appconfig response is not a JSON object")
            return

        cached_raw = AddonSettings.get_setting(APPCONFIG_CACHE_KEY, store=LOCAL)
        try:
            settings_cached = json.loads(cached_raw) if cached_raw else {}
        except (ValueError, TypeError):
            settings_cached = {}

        settings_cached.pop("_synced_at", None)
        settings_no_synced_at = {k: v for k, v in data.items() if k != "_synced_at"}
        if settings_no_synced_at != settings_cached:
            data["_synced_at"] = time.time()
            AddonSettings.set_setting(APPCONFIG_CACHE_KEY, json.dumps(data), store=LOCAL)

        Channel.service_interval = data.get("heartbeatInterval", APPCONFIG_HEARTBEAT_DEFAULT)
        Logger.debug(f"NLZIET: Next heartbeat in {Channel.service_interval}s")

        Channel.is_blocked = data.get("isAppBlocked", False)
        Channel.blocked_reason = data.get("appBlockedReason") or ""
        if Channel.is_blocked:
            Logger.warning(f"NLZIET: App is blocked - {Channel.blocked_reason!r}")

        Channel.is_update_required = data.get("isUpdateRequired", False)
        Channel.update_reason = data.get("updateText") or ""
        if Channel.is_update_required:
            Logger.warning(f"NLZIET: API update required - {Channel.update_reason!r}")


    # -- Background service ------------------------------------------------

    def service_update(self) -> None:
        """ Periodic background callback. """

        self._sync_appconfig()
        self._authenticator.active_authentication()
