# SPDX-License-Identifier: GPL-3.0-or-later
""" NLZIET channel for Retrospect. """

import json
import re
import time
import xbmc

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any, ClassVar, Dict, List, Optional, Tuple, Union, final
from urllib.parse import parse_qs, urlencode, urlparse

from resources.lib import chn_class, contenttype, mediatype
from resources.lib.actions import action
from resources.lib.actions.actionparser import ActionParser
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


_MS_PER_SECOND = 1000

# App identification headers sent with every API request (auth + content).
# Values are faked to match the expected API client — not real app versions;
# these were current at implementation time (2025-06).
NLZIET_APP_NAME = "AndroidTv"
NLZIET_APP_PLAYER_NAME = "NLZIETAndroidTVExoPlayer"
NLZIET_APP_VERSION = "5.65.5"
NLZIET_WEB_NAME = "WebApp"
NLZIET_WEB_PLAYER_NAME = "BitmovinWeb"
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
API_V7_CURRENT_TIME = "/v7/currenttime"

# V8 API
API_V8_PROFILE = "/v8/profile"
API_V8_SERIES_BASE = "/v8/series/"

# V9 API
API_V9_CONTINUE_WATCHING = "/v9/continueWatching"
API_V9_EPG_DATE = "/v9/epg/programlocations"
API_V9_EPG_LIVE = "/v9/epg/programlocations/live"
API_V9_ITEM_DETAIL = "/v9/item/detail"
API_V9_LIVE_HANDSHAKE = "/v9/stream/handshake"
API_V9_PLACEMENT = "/v9/placement/rows/{}"
API_V9_PLACEMENT_EXPLORE_BASE = "/v9/placement/rows/explore-"
API_V9_RECOMMEND_FILTERED = "/v9/recommend/filtered"
API_V9_RECOMMEND_WITH = "/v9/recommend/with"
API_V9_SEARCH = (
    "/v9/search"
    "?searchTerm=%s"
    "&limit=100"
    "&offset=0"
    "&contentType=Movie"
    "&contentType=Series"
)
API_V9_SEARCH_BASE = "/v9/search?"
API_V9_SEASON_ALL_EPISODES = (
    "/v9/series/{}/episodes"
    "?seasonId={}"
    "&limit=400"
)
API_V9_SERIES_EPISODES = (
    "/v9/series/{}/episodes"
    "?limit=100"
    "&offset=0"
)
API_V9_SERIES_PLAY = "/v9/series/{}/play"
API_V9_SERIES_BASE = "/v9/series/"
API_V9_SERIES_SEASON_EPISODES = (
    "/v9/series/{}/episodes"
    "?seasonId={}"
    "&limit=100"
    "&offset=0"
)
API_V9_TRACKED_SERIES = "/v9/trackedseries"
API_V9_VOD_HANDSHAKE = (
    "/v9/stream/handshake"
    "?context=OnDemand"
    "&id={}"
    "&drmType=Widevine"
    "&sourceType=Dash"
    "&playerName={}"
)
API_V9_WATCH_IN_ADVANCE = "/v9/watchinadvance"

# NLZIET channel defaults
APPCONFIG_HEARTBEAT_DEFAULT = 90  # seconds
APPCONFIG_SYNC_MAX_AGE = 240      # seconds (15 minutes) before a stale appconfig resets to defaults
APPCONFIG_SYNC_MAX_FAIL = 10      # consecutive failures before resetting to defaults
APPCONFIG_INIT_RETRY_MAX = 3      # interactive retries shown to the user during init

# EPG
EPG_DEFAULT_PAST_DAYS        = 3          # days
EPG_DEFAULT_FUTURE_DAYS      = 3          # days
EPG_NOW_PLAYING_TYPE_LIVE    = "Live"
EPG_NOW_PLAYING_TYPE_RESTART = "Restart"
EPG_NOW_PLAYING_TYPE_REPLAY  = "Replay"
EPG_MAX_SERVER_TIME_DRIFT    = 300        # seconds; discard server timestamp if clock drift exceeds 5 minutes
EPG_PROGRAM_LOCATION_CURRENT = 0
EPG_PROGRAM_LOCATION_NEXT = 1

# Subscription feature strings that map to a premium (add-on package) item.
_PREMIUM_PACKAGES: frozenset = frozenset({
    "ExtraChannelPackage1",
})

ICON_OVERLAY_NONE = 0  # xbmcgui.ICON_OVERLAY_NONE


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

    live_restart_padding: ClassVar[int] = 0
    """ ``liveStreamRestartStartPadding`` from the last successful appconfig sync. """

    _item_detail_cache: ClassVar[Dict[str, dict]] = {}
    """
    In-process RAM cache mapping contentItemId → content dict from v9/item/detail.

    Avoids one HTTP round-trip per channel on every channel-list refresh within
    the same Kodi session. Invalidated only by process restart.
    """

    _now_playing: ClassVar[Optional[dict]] = None
    """Currently playing item state, set by update_live_item; used by the heartbeat."""


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

        if self._authenticator.device_flow:
            app_name = NLZIET_APP_NAME
            app_version = NLZIET_APP_VERSION
            self._player_name = NLZIET_APP_PLAYER_NAME
        else:
            app_name = NLZIET_WEB_NAME
            app_version = NLZIET_WEB_VERSION
            self._player_name = NLZIET_WEB_PLAYER_NAME
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
            preprocessor=self._prefetch_live_details,
            requires_logon=True,
            updater=self.update_live_item,
        )

        self._add_data_parser(
            self._prefix_urls(f"{API_V9_LIVE_HANDSHAKE}?context=Epg"),
            name="EPG catchup / watch-ahead stream",
            requires_logon=True,
            updater=self.update_replay_item,
        )

        # -- VOD (on demand) parsers ----------------------------------

        self._add_data_parser(
            self._prefix_urls(API_V9_PLACEMENT_EXPLORE_BASE),
            name="Explore category page",
            preprocessor=self.get_explore_items,
            requires_logon=True,
        )

        self._add_data_parser(
            self._prefix_urls(API_V9_RECOMMEND_WITH),
            creator=self.create_vod_item,
            json=True,
            name="VOD content list",
            parser=["data"],
            requires_logon=True,
            updater=self.update_vod_item,
        )

        self._add_data_parser(
            self._prefix_urls(API_V9_RECOMMEND_FILTERED),
            creator=self.create_vod_item,
            json=True,
            name="Genre-filtered VOD content",
            parser=["data"],
            requires_logon=True,
            updater=self.update_vod_item,
        )

        self._add_data_parser(
            self._prefix_urls(API_V9_TRACKED_SERIES),
            creator=self.create_vod_item,
            json=True,
            name="Watchlist",
            parser=["data"],
            requires_logon=True,
            updater=self.update_vod_item,
        )

        self._add_data_parser(
            self._prefix_urls(API_V9_CONTINUE_WATCHING),
            creator=self.create_vod_item,
            json=True,
            name="Continue watching",
            parser=["data"],
            requires_logon=True,
            updater=self.update_vod_item,
        )

        self._add_data_parser(
            self._prefix_urls(API_V9_WATCH_IN_ADVANCE),
            creator=self.create_vod_item,
            json=True,
            name="Watch in advance",
            parser=["data"],
            requires_logon=True,
            updater=self.update_vod_item,
        )

        self._add_data_parser(
            self._prefix_urls(API_V8_SERIES_BASE),
            name="Series detail",
            preprocessor=self.extract_series_data,
            requires_logon=True,
        )

        self._add_data_parser(
            self._prefix_urls(API_V9_SERIES_BASE),
            creator=self.create_episode_item,
            json=True,
            name="Series episodes",
            parser=["data"],
            postprocessor=self._postprocess_series_episodes,
            preprocessor=self.extract_series_title,
            requires_logon=True,
            updater=self.update_vod_item,
        )

        self._add_data_parser(
            self._prefix_urls(API_V9_SEARCH_BASE),
            creator=self.create_search_result_item,
            json=True,
            name="Search results",
            parser=["data"],
            requires_logon=True,
            updater=self.update_vod_item,
        )

        self._add_data_parser(
            self._prefix_urls(f"{API_V9_LIVE_HANDSHAKE}?context=OnDemand"),
            name="VOD stream resolver",
            requires_logon=True,
            updater=self.update_vod_item,
        )

        self._current_series_title: str = ""
        self._current_season_number: int = 0

        for _ in range(APPCONFIG_INIT_RETRY_MAX):
            if self._sync_appconfig(allow_interactive_login=True):
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

        if not self.loggedOn:
            return data, items

        search = FolderItem(
            LanguageHelper.get_localized_string(LanguageHelper.Search),
            self.search_url,
            content_type=contenttype.TVSHOWS,
        )
        search.complete = True
        search.dontGroup = True
        items.append(search)

        page = "kids-home" if self._profile_type() == "ChildYoung" else "home"
        placement_url = self._prefix_urls(API_V9_PLACEMENT.format(page))
        Logger.debug(f"NLZIET: Fetching placement rows from {placement_url}")

        placement_data = UriHandler.open(
            placement_url, additional_headers=self._request_headers, no_cache=True)
        status = UriHandler.last_status()
        if status.error or not placement_data:
            Logger.warning("NLZIET: Empty placement response, skipping On Demand rows")
            return data, items

        try:
            placement = JsonHelper(placement_data)
        except (ValueError, TypeError):
            Logger.warning("NLZIET: Could not parse placement response")
            return data, items

        components = placement.get_value("components") or []
        for component in components:
            result = self._create_placement_item(component)
            if isinstance(result, list):
                items.extend(result)
            elif result:
                items.append(result)

        return data, items


    # -- IPTV/EPG handler --------------------------------------------------

    def _create_replay_item(self, cid: str,
                            channel: Optional[dict],
                            program: Optional[dict]) -> Optional[MediaItem]:
        """
        Build a playable MediaItem for a catchup or watch-ahead stream.

        Delegates to ``_build_program_item`` with a pre-built catchup URL so
        that the resulting item carries full channel art and detail metadata.

        :param cid:      Content item ID (``contentItemId`` from the API).
        :param channel:  The ``channel`` dict from the EPG entry.
        :param program:  The ``content`` dict from the program location entry.

        :returns:        - A fully populated ``MediaItem``.
                         - ``None`` when ``cid`` or ``channel`` is missing/invalid.

        """

        if (not cid or
            not channel):
            return None

        channel_id = channel.get("content", {}).get("id")
        if not channel_id:
            return None

        asset_id = (program or {}).get("assetId") or ""
        url = self._prefix_urls(
            f"{API_V9_LIVE_HANDSHAKE}?context=Epg"
            f"&channel={channel_id}"
            f"&id={cid}"
            f"&preferredAssetId={asset_id}"
            f"&playerName={self._player_name}"
            "&drmType=Widevine"
            "&sourceType=Dash"
        )

        return self._build_program_item(channel, program, url)


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
                timestamp=None,
                year=dt.year,
                month=dt.month,
                day=dt.day,
                hour=dt.hour,
                minutes=dt.minute,
                seconds=dt.second,
            )
        except (ValueError, TypeError):
            pass


    def _get_server_time(self) -> float:
        """
        Get the current Unix timestamp in seconds, preferring the server clock.

        Falls back to ``time.time()`` when the API call fails or the server
        timestamp deviates from the local clock by more than
        ``EPG_MAX_SERVER_TIME_DRIFT`` seconds.

        :returns:   Unix timestamp as a float.
        """

        raw = UriHandler.open(self._prefix_urls(API_V7_CURRENT_TIME))
        status = UriHandler.last_status()
        if not status.error:
            server_ts = float(raw or 0.0) / _MS_PER_SECOND
            if abs(server_ts - time.time()) <= EPG_MAX_SERVER_TIME_DRIFT:
                return server_ts

        return time.time()


    def create_iptv_epg(self, parameter_parser: Optional[ActionParser] = None) -> Dict[str, Any]:
        """
        Build a pvr.iptvsimple EPG dict spanning past and future days.

        :param parameter_parser: When provided, replay and watch-ahead stream
                                 URLs are embedded in each EPG entry and items
                                 are pickled for playback.  Pass ``None`` to
                                 skip stream URL generation.

        :returns:                - Dict of channel-id → EPG entry list.
                                 - ``{}`` when not authenticated.

        """

        if not self.loggedOn:
            return {}

        parent = None
        if parameter_parser is not None:
            parent = MediaItem(self.channelName, self.mainListUri)

        epg: Dict[str, Any] = {}
        replay_items = []

        now_ts = self._get_server_time()
        today = date.fromtimestamp(now_ts)

        for day_offset in range(-EPG_DEFAULT_PAST_DAYS, EPG_DEFAULT_FUTURE_DAYS + 1):
            day = (today + timedelta(days=day_offset)).isoformat()

            raw = UriHandler.open(
                self._prefix_urls("{0}?date={1}".format(API_V9_EPG_DATE, day)),
                additional_headers=self._request_headers
            )
            status = UriHandler.last_status()
            if status.error:
                continue

            try:
                day_data = json.loads(raw)
            except (ValueError, TypeError):
                continue

            for channel_entry in day_data.get("data", []):
                channel = channel_entry.get("channel")
                channel_id = (channel or {}).get("content", {}).get("id")
                if not channel_id:
                    continue

                if channel_id not in epg:
                    epg[channel_id] = []

                for prog in channel_entry.get("programLocations", []):
                    content = prog.get("content", {})
                    cid = content.get("contentItemId")
                    start_at = content.get("startAt")
                    end_at = content.get("endAt")
                    title = content.get("title")
                    landscape = (content.get("image") or {}).get("landscapeUrl")
                    tags = content.get("tags") or []

                    if (not title or
                        not start_at or
                        not end_at):
                        continue

                    epg_item = {"start": start_at, "stop": end_at, "title": title}
                    if landscape:
                        epg_item["image"] = landscape
                    first_broadcast = content.get("firstBroadcast", "")
                    if first_broadcast:
                        epg_item["date"] = first_broadcast[:10].replace("-", "")

                    if (parameter_parser is not None and
                        parent is not None):
                        try:
                            program_start = datetime.fromisoformat(start_at).timestamp()
                            program_end = datetime.fromisoformat(end_at).timestamp()
                        except (ValueError, TypeError):
                            program_start = None
                            program_end = None

                        if (content.get("isReplayAllowed") and
                            program_end is not None and
                            program_end <= now_ts):
                            replay_item = self._create_replay_item(cid, channel, content)
                            if replay_item:
                                epg_item["stream"] = parameter_parser.create_action_url(
                                    self,
                                    action=action.PLAY_VIDEO,
                                    item=replay_item,
                                    store_id=parent.guid,
                                )
                                replay_items.append(replay_item)

                        if ("WatchInAdvance" in tags and
                              program_start is not None and
                              program_start > now_ts):
                            watch_ahead = self._create_replay_item(cid, channel, content)
                            if watch_ahead:
                                epg_item["stream"] = parameter_parser.create_action_url(
                                    self,
                                    action=action.PLAY_VIDEO,
                                    item=watch_ahead,
                                    store_id=parent.guid,
                                )
                                replay_items.append(watch_ahead)

                    epg[channel_id].append(epg_item)

        if (replay_items and
            parameter_parser is not None and
            parent is not None):
            parameter_parser.pickler.store_media_items(parent.guid or "", parent, replay_items)

        return epg


    def create_iptv_streams(self, parameter_parser: ActionParser) -> List[Dict[str, Any]]:
        """
        Build pvr.iptvsimple stream dicts for all live channels.

        :param parameter_parser: Action parser to build URLs and store items.

        :returns:                - List of stream dicts, one per live channel.
                                 - ``[]`` when not authenticated or the API call fails.
        """

        if not self.loggedOn:
            return []

        raw = UriHandler.open(
            self._prefix_urls(API_V9_EPG_LIVE),
            additional_headers=self._request_headers,
        )
        status = UriHandler.last_status()
        if status.error:
            return []

        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return []

        if not isinstance(data, dict):
            return []

        parent = MediaItem(self.channelName, self.mainListUri)
        items = []
        streams = []
        entries = data.get("data", [])
        for entry in entries:
            item = self.create_live_channel_item(entry)
            if item is None:
                continue

            stream_url = parameter_parser.create_action_url(
                self, action=action.PLAY_VIDEO,
                item=item,
                store_id=parent.guid,
            )
            stream_dict = {
                "group": self.channelName,
                "id": item.metaData["channel_id"],
                "logo": item.metaData.get("logo_url", ""),
                "name": item.name,
                "stream": stream_url,
            }
            content_provider = item.metaData.get("content_provider", "")
            if content_provider:
                stream_dict["provider"] = content_provider
            streams.append(stream_dict)
            items.append(item)

        if items:
            parameter_parser.pickler.store_media_items(parent.guid or "", parent, items)

        return streams


    def _get_item_detail(self, content_item_id: str) -> dict:
        """
        Return the v9/item/detail record for ``content_item_id``, using the
        class-level cache when available and fetching (and caching) it
        otherwise.

        :param content_item_id:  Content item ID to look up.

        :return: - The cached/fetched ``content`` dict, or
                 - ``{}`` if not found or on fetch error.
        """

        if content_item_id in Channel._item_detail_cache:
            return Channel._item_detail_cache[content_item_id]

        detail = UriHandler.open(
            self._prefix_urls(f"{API_V9_ITEM_DETAIL}/{content_item_id}"),
            additional_headers=self._request_headers,
        )
        status = UriHandler.last_status()
        if status.error:
            return {}
        try:
            data = json.loads(detail)
        except (ValueError, TypeError):
            return {}

        if not isinstance(data, dict):
            return {}
        content = data.get("content") or {}

        Channel._item_detail_cache[content_item_id] = content
        return content


    def _add_item_detail(self, item: MediaItem, content_item_id: str) -> None:
        """
        Enrich a MediaItem with v9/item/detail metadata.

        Fetches (or retrieves from cache) the detail record for
        ``content_item_id`` and applies duration, genre, NICAM, series art,
        and description to ``item`` in place.

        :param item:             The item to enrich.
        :param content_item_id:  Content item ID used to query the detail API.

        """

        detail = self._get_item_detail(content_item_id)
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

        broadcasters = detail.get("broadcasters") or []
        broadcaster_logo = (broadcasters[0].get("logoUrl") or "") if broadcasters else ""

        series = detail.get("series") or {}
        series_image = series.get("image") or {}
        series_title = series.get("title") or ""
        if series_title:
            item.tv_show_title = series_title

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


    def _prefetch_live_details(self, data: str) -> tuple:
        """
        Pre-fetch all v9/item/detail entries for the live EPG in parallel.

        Collects every ``contentItemId`` found in ``programLocations`` (current
        and next program) from the raw live-EPG JSON, then fetches all that are
        not already cached using a ``ThreadPoolExecutor``.  The original ``data``
        string is returned unchanged so the normal creator pipeline can proceed.

        :param data:    Raw JSON string from the live-EPG endpoint.

        :returns:       ``(data, [])`` — data unchanged, no pre-built items.

        """

        try:
            parsed = json.loads(data)
        except (ValueError, TypeError):
            return data, []

        if not isinstance(parsed, dict):
            return data, []

        ids = set()
        for channel_entry in parsed.get("data", []):
            for loc in channel_entry.get("programLocations", []):
                cid = (loc.get("content") or {}).get("contentItemId") or ""
                if cid:
                    ids.add(cid)

        to_fetch = [cid for cid in ids if cid not in Channel._item_detail_cache]
        if to_fetch:
            with ThreadPoolExecutor(max_workers=min(len(to_fetch), 10)) as executor:
                executor.map(self._get_item_detail, to_fetch)

        return data, []


    # -- Live channel items ------------------------------------------------

    def _report_epg_heartbeat(self) -> None:
        """ Playback progress to the heartbeat endpoint. """

        if self._get_setting("nlziet_report_progress") != "true":
            return

        state = Channel._now_playing
        if state is None:
            return

        player = xbmc.Player()
        if not player.isPlaying():
            Channel._now_playing = None
            return

        content_id = state["id"]
        progress_ms = int(player.getTime() * _MS_PER_SECOND)
        params = urlencode({"progress": progress_ms, "epgType": state["epg_type"]})
        url = f"{API_CONTENT_URL}/heartbeat/epg/{content_id}?{params}"
        UriHandler.open(url, additional_headers=self._request_headers, method="POST")
        status = UriHandler.last_status()
        if status.error:
            Logger.debug(f"NLZIET: Heartbeat POST failed for {url}: {status.code} {status.reason}")


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
                msg = LanguageHelper.get_localized_string(LanguageHelper.MaxStreamsReached)
                if isinstance(msg, str):
                    msg = msg.replace("{0}", max_streams)
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
            # What about other error types? premium? subscribtion?

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
        status = UriHandler.last_status()
        if status.error:
            Logger.error(f"Empty stream response for: {url}")
            return None

        try:
            json_data = JsonHelper(data)
        except (ValueError, TypeError):
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


    def update_replay_item(self, item: MediaItem) -> MediaItem:
        """
        Fetch the DASH stream URL for a catchup or watch-ahead programme.

        Both catchup (already-aired) and watch-ahead (not-yet-aired) items
        share the same ``context=Epg`` handshake URL shape; the server handles
        the time-relative difference internally.

        :param MediaItem item: The item to update with stream info.
        :return: The updated item.
        """

        Logger.debug(f"Updating catchup stream for: {item.name}")
        stream = self._configure_drm_stream(item.url)
        if stream:
            item.streams.append(stream)
            item.complete = True
        return item


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
            padding = Channel.live_restart_padding
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

        Channel._now_playing = {
            "type": "epg",
            "id": channel_id,
            "epg_type": EPG_NOW_PLAYING_TYPE_RESTART if live_offset > 0 else EPG_NOW_PLAYING_TYPE_LIVE,
        }

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
            program_locations[EPG_PROGRAM_LOCATION_CURRENT].get("content") if program_locations else None,
            live_url
        )
        if item is None:
            return None

        if len(program_locations) > EPG_PROGRAM_LOCATION_NEXT:
            upnext = self._build_program_item(
                channel,
                program_locations[EPG_PROGRAM_LOCATION_NEXT].get("content"),
                live_url
            )
            if upnext:
                item.metaData["upnext_item"] = upnext

        item.isLive = True
        return item


    # -- VOD content (movies, series, trending) -----------------------------

    # Explore/placement row component types that render as navigable folders.
    _PLACEMENT_TYPES: ClassVar[frozenset] = frozenset({
        "ItemTileList",
        "ItemTileListWithFolder",
        "PersonalizedProgramLocationsLive",
    })


    def get_explore_items(self, data: str) -> Tuple[str, List[MediaItem]]:
        """
        Create folder items for an explore category page.

        Turns each ``ItemTileList``/``Placements`` component of a placement
        response into a navigable genre folder or nested explore folder.

        :param data: Raw JSON response from an explore placement page.

        :return: A tuple of the data and a list of MediaItems.
        """

        items: List[MediaItem] = []
        if not data:
            return data, items

        try:
            placement = JsonHelper(data)
        except (ValueError, TypeError):
            return data, items

        components = placement.get_value("components") or []
        for component in components:
            result = self._create_placement_item(component)
            if isinstance(result, list):
                items.extend(result)
            elif result:
                items.append(result)

        return data, items


    def _create_placement_item(
            self, component: dict) -> Union[FolderItem, List[FolderItem], None]:
        """
        Create a FolderItem from a placement row component.

        ``Placements`` components are expanded into individual explore-page
        folders instead of being returned as a single item.

        :param component: A single component from a placement response.

        :return: A FolderItem, a list of FolderItems, or None.
        """

        comp_type = component.get("type", "")

        if comp_type == "Placements":
            return self._create_explore_items(component.get("items", []))

        url = component.get("url") or component.get("parameters", {}).get("url")
        title = component.get("title") or component.get("parameters", {}).get("title")
        if comp_type not in Channel._PLACEMENT_TYPES or not url or not title:
            return None

        item = FolderItem(title, self._prefix_urls(url), content_type=contenttype.VIDEOS)
        if comp_type == "PersonalizedProgramLocationsLive":
            item.isLive = True
            item.dontGroup = True
        item.complete = True
        item.HttpHeaders = self._nlziet_headers
        return item


    def _create_explore_items(self, placement_items: List[dict]) -> List[FolderItem]:
        """
        Create folder items for each explore category.

        :param placement_items: Items from a ``Placements`` component.

        :return: A list of FolderItems for explore pages.
        """

        items: List[FolderItem] = []
        for entry in placement_items:
            entry_id = entry.get("id")
            title = entry.get("title")
            if not entry_id or not title:
                continue
            url = self._prefix_urls(API_V9_PLACEMENT.format(entry_id))
            item = FolderItem(title, url, content_type=contenttype.TVSHOWS)
            item.dontGroup = True
            item.complete = True
            item.HttpHeaders = self._nlziet_headers
            items.append(item)
        return items


    def create_vod_item(self, result_set: dict) -> Optional[MediaItem]:
        """
        Create an item from a recommend/watchlist/continue-watching response entry.

        Items without a ``type`` field are treated as series folders. Items
        tagged ``"Movie"`` become playable movie items. Everything else
        becomes a playable episode item.

        :param result_set: A single ``data[]`` entry (contains ``content``).

        :return: A MediaItem, FolderItem, or None.
        """

        content = result_set.get("content") or {}
        if not content:
            return None

        if content.get("isAvailable") is False:
            return None

        item_id = content.get("id")
        title = content.get("title", "")
        if not item_id or not title:
            return None

        item_type = content.get("type")
        tags = content.get("tags") or []
        is_movie = "Movie" in tags

        item: MediaItem
        if item_type is None or item_type == "Series":
            item = FolderItem(title, self._prefix_urls(f"{API_V8_SERIES_BASE}{item_id}"),
                               content_type=contenttype.EPISODES)
            item.complete = True
            item.dontGroup = True
        elif is_movie:
            item = MediaItem(title, self._vod_handshake_url(item_id), media_type=mediatype.MOVIE)
            item.isDrmProtected = True
            item.isGeoLocked = True
            item.dontGroup = True
        else:
            item = MediaItem(title, self._vod_handshake_url(item_id), media_type=mediatype.EPISODE)
            item.isDrmProtected = True
            item.isGeoLocked = True
            item.dontGroup = True

        numbering = content.get("formattedEpisodeNumbering") or ""
        if numbering:
            match = re.match(r"S(\d+):A(\d+)", numbering)
            if match:
                item.set_season_info(int(match.group(1)), int(match.group(2)))

        self._set_vod_metadata(item, content)
        item.HttpHeaders = self._nlziet_headers
        return item


    def extract_series_title(self, data: str) -> Tuple[str, List[MediaItem]]:
        """
        Preprocessor for ``/v9/series/{id}/episodes`` URLs.

        Reads the series title and season number stored in the parent
        season folder's metaData (set by :meth:`extract_series_data`) and
        caches them on this instance so :meth:`create_episode_item` can
        populate ``tv_show_title`` for Up Next and fall back to the real
        season number when an episode's own numbering text doesn't carry
        one (e.g. the plain "Afl. <n>" format has no season component).

        :param data: Raw JSON response, passed through unchanged.

        :return: A tuple of the data and an empty item list.
        """

        series_title = ""
        season_number = 0
        if self.parentItem:
            series_title = self.parentItem.metaData.get("nlziet:series_title", "")
            season_number = self.parentItem.metaData.get("nlziet:season_number", 0)
        self._current_series_title = series_title
        self._current_season_number = season_number
        return data, []


    def create_episode_item(self, result_set: Union[str, dict]) -> Optional[MediaItem]:
        """
        Create a playable item from a ``/v9/series/{id}/episodes`` entry.

        :param result_set: A single ``data[]`` entry.

        :return: A playable MediaItem or None.
        """

        if not isinstance(result_set, dict):
            return None

        content = result_set.get("content") or {}
        if not content:
            return None

        item_id = content.get("id")
        title = content.get("title") or content.get("subtitle", "")
        if not item_id or not title:
            return None

        item = MediaItem(title, self._vod_handshake_url(item_id), media_type=mediatype.EPISODE,
                          tv_show_title=self._current_series_title or None)
        item.isDrmProtected = True
        item.isGeoLocked = True
        item.dontGroup = True

        # No season/episode number is parsed from episode text here. The
        # API's numbering text is unreliable (absent, inconsistent between
        # captures, or in a dozen different formats — "S1:A6", "Afl. 4",
        # bare specials, ...) but its *array order* is always correct — the
        # real NLZIET webapp trusts it as-is and does no client-side
        # reordering. _normalize_episode_sequence() assigns season/episode
        # numbers from each item's position in that order, using the real
        # season number carried by the season folder (see
        # extract_series_title()), not from text pattern-matching.
        subtitle = content.get("subtitle") or ""
        if subtitle and subtitle != title:
            item.metaData["nlziet:subtitle"] = subtitle

        self._set_vod_metadata(item, content)
        item.HttpHeaders = self._nlziet_headers
        return item


    def _postprocess_series_episodes(self, data: Any, items: List[MediaItem]) -> List[MediaItem]:
        """
        Post-process a ``/v9/series/{id}/episodes`` item list.

        Combines episode-sequence normalization with title de-duplication.

        :param data:  Unused; required by the post-processor signature.
        :param items: The items produced by the parser/creator.

        :return: The processed list of items.
        """

        items = self._normalize_episode_sequence(data, items)
        return self.deduplicate_episode_titles(data, items)


    def _normalize_episode_sequence(self, data: Any, items: List[MediaItem]) -> List[MediaItem]:
        """
        Assign season/episode numbers purely from API response order.

        NLZIET's episode numbering text is not trustworthy as a source of
        truth — it's absent as often as not, inconsistent between captures
        of the very same episode, and shows up in a handful of different
        formats depending on the show. The array *order* the API returns,
        however, is always correct: it's exactly what the real NLZIET
        webapp displays, unmodified — it performs no client-side episode
        sorting at all.

        So instead of trying to parse a number out of unreliable text,
        every item is simply numbered by its position in that order
        (1-based), using the real season number carried by the season
        folder (see extract_series_title()). This guarantees Kodi's
        episode sort exactly reproduces the API sequence, in either
        direction, for every show regardless of its numbering-text quirks.

        :param data:  Unused; required by the post-processor signature.
        :param items: The items produced by the parser/creator, in API order.

        :return: The (unchanged, but now numbered) list of items.
        """

        episodes = [i for i in items if not i.is_folder]
        if not episodes:
            return items

        season = self._current_season_number or 1
        for idx, ep in enumerate(episodes):
            ep.set_season_info(season, idx + 1)

        return items


    def deduplicate_episode_titles(self, data: Any, items: List[MediaItem]) -> List[MediaItem]:
        """
        Disambiguate duplicate episode titles using subtitles.

        When all episodes share the same title (common for kids shows), each
        title is replaced by its subtitle. When only some titles are
        duplicated (2+), those get ``subtitle (title)`` format while unique
        titles are left untouched.

        :param data:  Unused; required by the post-processor signature.
        :param items: The items produced by the parser/creator.

        :return: The (potentially modified) list of items.
        """

        episodes = [i for i in items if not i.is_folder]
        if len(episodes) < 2:
            return items

        counts = Counter(e.name for e in episodes)
        unique_titles = len(counts)

        if unique_titles == 1:
            for episode in episodes:
                subtitle = episode.metaData.get("nlziet:subtitle")
                if subtitle:
                    episode.name = f"{subtitle} ({episode.name})"
        elif any(c >= 2 for c in counts.values()):
            for episode in episodes:
                if counts[episode.name] >= 2:
                    subtitle = episode.metaData.get("nlziet:subtitle")
                    if subtitle:
                        episode.name = f"{subtitle} ({episode.name})"

        return items


    def create_search_result_item(self, result_set: dict) -> Optional[MediaItem]:
        """
        Create an item from a ``/v9/search`` response entry.

        The search response uses ``type`` values ``"Movie"`` and ``"Series"``
        (different from the recommend endpoints).

        :param result_set: A single ``data[]`` entry.

        :return: A MediaItem, FolderItem, or None.
        """

        content = result_set.get("content") or {}
        if not content:
            return None

        item_id = content.get("id")
        title = content.get("title", "")
        item_type = content.get("type", "")
        if not item_id or not title:
            return None

        tags = content.get("tags") or []

        item: MediaItem
        if item_type == "Series" or item_type is None:
            item = FolderItem(title, self._prefix_urls(f"{API_V8_SERIES_BASE}{item_id}"),
                               content_type=contenttype.EPISODES)
            item.complete = True
            item.dontGroup = True
        elif item_type == "Movie" or "Movie" in tags:
            item = MediaItem(title, self._vod_handshake_url(item_id), media_type=mediatype.MOVIE)
            item.isDrmProtected = True
            item.isGeoLocked = True
            item.dontGroup = True
        else:
            item = MediaItem(title, self._vod_handshake_url(item_id), media_type=mediatype.EPISODE)
            item.isDrmProtected = True
            item.isGeoLocked = True
            item.dontGroup = True

        self._set_vod_metadata(item, content)
        item.HttpHeaders = self._nlziet_headers
        return item


    @staticmethod
    def _parse_season_number(title: str, position: int) -> int:
        """
        Extract a season number from a season title.

        Handles a bare numeric title (``"5"``) and a prefixed one
        (``"Seizoen 6 - Zomer 2026"``). When neither matches, falls back
        to the season's chronological position (1 = oldest) rather than
        a constant — a constant would collide with real season numbers
        for every other season in the same series.

        :param title:    The season's ``title`` field from the API.
        :param position: 1-based chronological position (1 = oldest),
                          used when the title carries no number.

        :return: The season number.
        """

        title = (title or "").strip()
        if title.isdigit():
            return int(title)
        match = re.search(r"Seizoen\s*(\d+)", title, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return position


    def extract_series_data(self, data: str) -> Tuple[str, List[MediaItem]]:
        """
        Preprocessor for ``/v8/series/{id}`` detail URLs.

        Parses the season list from the series detail response and returns
        them as folder items pointing to season-episode URLs. The downstream
        parser is skipped because the data is replaced with an empty string.

        Shortcut items (Continue Watching, Most Recent Episode, Oldest
        Episode) are prepended before the season folders so users can jump
        straight to an episode without drilling into seasons.

        :param data: Raw JSON response.

        :return: A tuple of the (empty) data and a list of items.
        """

        json_data = JsonHelper(data)
        # The API may return {"content": {...}} or {"data": {"content": {...}}}.
        series_content = json_data.get_value("content", fallback=None)
        if not series_content:
            series_content = json_data.get_value("data", "content", fallback={})
        if not series_content:
            return "", []

        series_id = series_content.get("id", "")
        series_title = series_content.get("title", "")
        # API returns seasons newest-first; reverse so seasons[0] = oldest
        # (used by shortcuts) and present newest-first to the user below.
        seasons = list(reversed(series_content.get("seasons", [])))

        items: List[MediaItem] = []
        if seasons:
            # seasons[] is chronological (oldest-first) at this point;
            # position 1 = oldest, used as a season-number fallback when a
            # season's own title carries no parseable number.
            season_positions = {s.get("id"): idx + 1 for idx, s in enumerate(seasons)}

            # Present newest season first in the folder listing.
            for season in reversed(seasons):
                season_id = season.get("id")
                season_title = season.get("title", "")
                if not season_id:
                    continue

                season_number = self._parse_season_number(
                    season_title, season_positions.get(season_id, 1))

                url = self._prefix_urls(
                    API_V9_SERIES_SEASON_EPISODES.format(series_id, season_id))
                folder = FolderItem(season_title or series_title, url,
                                     content_type=contenttype.EPISODES)
                folder.complete = True
                folder.metaData["nlziet:series_title"] = series_title
                folder.metaData["nlziet:season_number"] = season_number
                items.append(folder)
        else:
            # Seasonless series — fetch episodes directly to avoid an
            # intermediate folder that just duplicates the series name.
            url = self._prefix_urls(API_V9_SERIES_EPISODES.format(series_id))
            episodes_item = MediaItem(series_title, url, media_type=mediatype.VIDEO)
            episodes_item.metaData["nlziet:series_title"] = series_title
            episodes = self.process_folder_list(episodes_item)
            items.extend(episodes)

        shortcuts = self._build_episode_shortcuts(series_id, seasons)
        # Return empty data so the parser does not run again.
        return "", shortcuts + items


    def update_vod_item(self, item: MediaItem) -> MediaItem:
        """
        Fetch the DASH stream URL for a VOD or search-result item.

        :param item: The item to update.

        :return: The updated item.
        """

        Logger.debug(f"Updating VOD stream for: {item.name}")
        stream = self._configure_drm_stream(item.url)
        if stream:
            item.streams.append(stream)
            item.complete = True
        return item


    def search_site(self, url: Optional[str] = None,
                    needle: Optional[str] = None) -> List[MediaItem]:
        """
        Search the NLZIET catalogue.

        :param url:    Unused; the search URL is constructed here.
        :param needle: The search query.

        :return: A list of search result items.
        """

        return chn_class.Channel.search_site(self, self._prefix_urls(API_V9_SEARCH), needle)


    def _build_episode_shortcuts(self, series_id: str,
                                 seasons: List[dict]) -> List[MediaItem]:
        """
        Build Continue / Most Recent / First episode shortcut items.

        Runs the continue-item and season-episode fetches concurrently:
        each is an independent HTTP round-trip, and this method blocks the
        series-detail folder listing until they all return, so serialising
        them would multiply worst-case latency by up to three.

        :param series_id: The series ID.
        :param seasons:   Season dicts from the series detail response,
                          oldest-first (already reversed by the caller).

        :return: Up to three playable shortcut items.
        """

        if not series_id or not seasons:
            return []

        oldest_season_id = seasons[0].get("id", "")
        newest_season_id = seasons[-1].get("id", "")
        single_season = oldest_season_id == newest_season_id

        with ThreadPoolExecutor(max_workers=2 if single_season else 3) as executor:
            continue_future = executor.submit(self._fetch_continue_item, series_id)
            newest_future = executor.submit(
                self._fetch_season_episodes, series_id, newest_season_id)
            oldest_future = None if single_season else executor.submit(
                self._fetch_season_episodes, series_id, oldest_season_id)

            continue_item = continue_future.result()
            newest_eps = newest_future.result()
            oldest_eps = newest_eps if oldest_future is None else oldest_future.result()

        first_item = Channel._pick_boundary_episode(oldest_eps, pick_first=True)
        recent_item = Channel._pick_boundary_episode(newest_eps, pick_first=False)

        if first_item and recent_item and first_item.url == recent_item.url:
            recent_item = None

        if first_item:
            first_item.name = Channel._shortcut_label(first_item, LanguageHelper.FirstEpisode)
        if recent_item:
            recent_item.name = Channel._shortcut_label(
                recent_item, LanguageHelper.MostRecentEpisode)

        shortcuts: List[MediaItem] = []
        if continue_item:
            first_id = first_item.url if first_item else None
            if continue_item.url != first_id:
                shortcuts.append(continue_item)
        if recent_item:
            shortcuts.append(recent_item)
        if first_item:
            shortcuts.append(first_item)

        return shortcuts


    @staticmethod
    def _shortcut_label(item: MediaItem, label_id: int) -> str:
        """
        Build a shortcut display name like ``Oldest Episode: Title``.

        :param item:     The shortcut item with metaData.
        :param label_id: LanguageHelper string ID for the label.

        :return: The formatted name.
        """

        label = str(LanguageHelper.get_localized_string(label_id))
        ep_title = item.metaData.get("nlziet:ep_title", "")
        if ep_title:
            return f"{label}: {ep_title}"
        return label


    def _fetch_continue_item(self, series_id: str) -> Optional[MediaItem]:
        """
        Fetch the "Continue Watching" episode via the series play endpoint.

        :param series_id: The series ID.

        :return: A playable MediaItem or None.
        """

        url = self._prefix_urls(API_V9_SERIES_PLAY.format(series_id))
        data = UriHandler.open(url, additional_headers=self._request_headers, no_cache=True)
        status = UriHandler.last_status()
        if status.error or not data:
            return None

        try:
            play_json = JsonHelper(data)
        except (ValueError, TypeError):
            return None
        content = play_json.get_value("content", fallback=None)
        if not content:
            return None

        content_id = content.get("id")
        ep_title = content.get("title", "")
        if not content_id:
            return None

        label = LanguageHelper.get_localized_string(LanguageHelper.ContinueWatching)
        name = f"{label}: {ep_title}" if ep_title else label
        item = MediaItem(name, self._vod_handshake_url(content_id), media_type=mediatype.EPISODE)
        item.isDrmProtected = True
        item.isGeoLocked = True
        item.dontGroup = True
        self._set_vod_metadata(item, content)
        item.HttpHeaders = self._nlziet_headers
        return item


    def _fetch_season_episodes(self, series_id: str,
                               season_id: str) -> List[Tuple[str, MediaItem]]:
        """
        Fetch all episodes for a season.

        :param series_id: The series ID.
        :param season_id: The season ID.

        :return: List of ``(broadcastAt, item)`` tuples in API response
                 order. Date strings are ISO-8601 or empty when absent.
        """

        if not season_id:
            return []

        url = self._prefix_urls(API_V9_SEASON_ALL_EPISODES.format(series_id, season_id))
        data = UriHandler.open(url, additional_headers=self._request_headers, no_cache=True)
        status = UriHandler.last_status()
        if status.error or not data:
            return []

        try:
            episodes = JsonHelper(data).get_value("data", fallback=[])
        except (ValueError, TypeError):
            return []

        result: List[Tuple[str, MediaItem]] = []
        for episode in episodes:
            content = episode.get("content", {})
            content_id = content.get("id")
            if not content_id:
                continue

            subtitle = content.get("subtitle") or content.get("title", "")
            item = MediaItem(subtitle, self._vod_handshake_url(content_id),
                             media_type=mediatype.EPISODE)
            item.isDrmProtected = True
            item.isGeoLocked = True
            item.dontGroup = True
            item.metaData["nlziet:ep_title"] = subtitle
            self._set_vod_metadata(item, content)
            item.HttpHeaders = self._nlziet_headers
            broadcast_at = content.get("broadcastAt") or ""
            result.append((broadcast_at, item))

        return result


    @staticmethod
    def _pick_boundary_episode(eps: List[Tuple[str, MediaItem]],
                               pick_first: bool) -> Optional[MediaItem]:
        """
        Pick the earliest or most-recent episode from API results.

        The API always returns episodes in a logically correct order
        (episode numbers are monotonic), but the direction varies per
        series: some are newest-first (descending), others oldest-first
        (ascending).

        Direction is detected by checking whether ``broadcastAt``
        timestamps are monotonically increasing or decreasing. When
        ``broadcastAt`` is non-monotonic (re-broadcasts, bulk imports) or
        absent, this defaults to descending — the most common ordering
        across the NLZIET catalogue.

        :param eps:        (broadcastAt, item) tuples in API response order.
        :param pick_first: True for the earliest episode; False for the
                           most-recent.

        :return: The selected MediaItem, or None if ``eps`` is empty.
        """

        if not eps:
            return None
        if len(eps) == 1:
            return eps[0][1]

        ascending = Channel._is_ascending(eps)

        if pick_first:
            return eps[0][1] if ascending else eps[-1][1]
        return eps[-1][1] if ascending else eps[0][1]


    @staticmethod
    def _is_ascending(eps: List[Tuple[str, MediaItem]]) -> bool:
        """
        Detect whether the API returned episodes oldest-first.

        Checks ``broadcastAt`` monotonicity: if every adjacent pair has a
        non-decreasing timestamp the list is ascending; if non-increasing
        it is descending. Non-monotonic or missing timestamps default to
        descending (the most common API ordering).

        :param eps: (broadcastAt, item) tuples.

        :return: True if the episode list is oldest-first (ascending).
        """

        dates = [ba for ba, _ in eps if ba]
        if len(dates) < 2:
            return False

        is_asc = all(dates[i] <= dates[i + 1] for i in range(len(dates) - 1))
        if is_asc:
            return True

        # Not ascending — could be descending or non-monotonic; both → False.
        return False


    def _vod_handshake_url(self, content_id: str) -> str:
        """
        Build a v9 stream handshake URL for on-demand playback.

        :param content_id: The content ID.

        :return: The handshake URL.
        """

        return self._prefix_urls(API_V9_VOD_HANDSHAKE.format(content_id, self._player_name))


    def _set_vod_metadata(self, item: MediaItem, content: dict) -> None:
        """
        Set common metadata fields on a VOD item.

        :param item:    The item to update.
        :param content: The ``content`` dict from the API response.
        """

        subtitle = content.get("subtitle") or ""
        description = content.get("description") or ""
        if subtitle and description and subtitle != description:
            description = f"[B]{subtitle}[/B]\n{description}"
        elif not description:
            description = subtitle
        availability = content.get("formattedAvailabilityWindow") or ""
        if availability and description:
            description = f"{description}\n\n{availability}"
        if description:
            item.description = description

        image = content.get("image") or {}
        item.thumb = image.get("landscapeUrl") or image.get("portraitUrl") or item.thumb
        item.poster = image.get("portraitUrl") or item.poster

        logo = content.get("logo") or {}
        if logo.get("normalUrl"):
            item.icon = logo["normalUrl"]

        provider = content.get("contentProvider") or ""
        if provider:
            item.set_info_label("Studio", provider)

        duration = content.get("formattedDuration") or ""
        if duration:
            item.set_info_label(MediaItem.LabelDuration, Channel._parse_duration(duration))

        self._apply_broadcast_date(item, content.get("broadcastedAt"))
        self._apply_expire_date(item, content.get("availableUntil"))


    @staticmethod
    def _parse_duration(formatted: str) -> int:
        """
        Parse a formatted duration string like ``"1u 23m"`` into seconds.

        :param formatted: Duration string from the API.

        :return: Duration in seconds, or 0 if unparseable.
        """

        total = 0
        hours = re.search(r"(\d+)\s*u", formatted)
        if hours:
            total += int(hours.group(1)) * 3600
        minutes = re.search(r"(\d+)\s*m", formatted)
        if minutes:
            total += int(minutes.group(1)) * 60
        return total


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
        selected = XbmcWrapper.show_selection_dialog(str(label), options)
        if not isinstance(selected, int) or selected < 0:
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


    def log_on(self) -> bool:
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
            if isinstance(welcome, str):
                XbmcWrapper.show_dialog(self.channelName, welcome.replace("{0}", str(display_name)))

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

    def _sync_appconfig(self, allow_interactive_login: bool = False) -> bool:
        """
        Sync the channel's shared config from the API.

        Almost everything NLZIET-side needs a session, so appconfig is
        always fetched with ``_request_headers`` (auth if we have it).
        A 401 here means the stored access token is dead — reading it
        raw via ``authentication_headers`` does not refresh it. That is
        an authentication problem, not a service outage, and has a known
        fix: recover the session via the authenticator (silent token
        refresh first, falling back to a full device-flow
        re-authentication if the refresh token is also dead) and retry
        once. This is distinct from a network/5xx failure below, which
        has no automated fix and is left to the caller's retry policy.

        :param allow_interactive_login: Whether a 401 may trigger the
                                        authenticator's full recovery
                                        cascade, including an interactive
                                        device-flow prompt. Must be
                                        ``False`` when called from the
                                        headless background service
                                        (``service_update``) — there is
                                        no user present to complete a
                                        device-flow prompt there.

        Repeated failure resets the poll interval; last-known block/update state is
        preserved rather than cleared, since we can't verify it changed.

        :return: - ``True`` on success,
                 - ``False`` on any auth, network, or parse failure.
        """

        Logger.debug(f"NLZIET: Syncing appconfig from {self.baseUrl}{API_V7_APPCONFIG}")

        url = self._prefix_urls(API_V7_APPCONFIG + "?os=web&origin=app")
        raw = UriHandler.open(url, additional_headers=self._request_headers)
        status = UriHandler.last_status()

        if status.code == 401 and allow_interactive_login:
            Logger.warning("NLZIET: Appconfig rejected our session (401) — re-authenticating")
            result = self._authenticator.log_on()
            if result.logged_on:
                raw = UriHandler.open(url, additional_headers=self._request_headers)
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

        Channel.live_restart_padding = int(data.get("liveStreamRestartStartPadding") or 0)

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

        # allow_interactive_login stays False (the default): this runs in the
        # headless background service, with no user present to complete a
        # device-flow prompt if the session is dead. active_authentication()
        # below still attempts a silent refresh.
        self._sync_appconfig()
        self._authenticator.active_authentication()
        self._report_epg_heartbeat()
