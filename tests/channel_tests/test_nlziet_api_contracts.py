# SPDX-License-Identifier: GPL-3.0-or-later
"""NLZIET API contract verification tests.

These tests authenticate against the real NLZIET API and verify that
endpoint responses still match the structure our mocks assume.  They
do NOT test channel logic — only that the API contracts haven't changed.

Guard: set NLZIET_USERNAME and NLZIET_PASSWORD environment variables.
"""

import json
import os
import sys
import unittest

# Allow importing const.py from the channel directory
sys.path.insert(0, "channels/channel.nl/nlziet")

from api import (  # noqa: E402
    API_V9_PLACEMENT, API_V9_EPG_LIVE, API_V8_PROFILE,
    API_V9_RECOMMEND_FILTERED, API_V9_SEARCH, API_V8_SERIES,
)

# Bootstrap Kodi emulator before importing Retrospect modules
os.environ.setdefault("KODI_HOME", os.path.join("tests", "home"))
os.environ.setdefault("KODI_INTERACTIVE", "0")

from resources.lib.logger import Logger  # noqa: E402
from resources.lib.urihandler import UriHandler  # noqa: E402
from resources.lib.authentication.nlzietoauth2handler import NLZIETOAuth2Handler  # noqa: E402


class TestNLZIETApiContracts(unittest.TestCase):
    """Verify real API responses match the structure our mocks assume."""

    handler = None
    headers = None

    @classmethod
    def setUpClass(cls):
        # 1. Check credentials first — skip before touching anything
        cls.username = os.getenv("NLZIET_USERNAME")
        cls.password = os.getenv("NLZIET_PASSWORD")
        if not cls.username or not cls.password:
            raise unittest.SkipTest("NLZIET credentials not in environment.")

        # 2. Initialize Logger and UriHandler
        Logger.create_logger(None, str(cls), min_log_level=0)
        UriHandler.create_uri_handler(ignore_ssl_errors=False)

        # 3. Ensure config directories exist
        from resources.lib.retroconfig import Config
        os.makedirs(Config.profileDir, exist_ok=True)

        # 4. Log in
        cls.handler = NLZIETOAuth2Handler(use_device_flow=False)
        result = cls.handler.log_on(cls.username, cls.password)
        if not result.logged_on:
            raise unittest.SkipTest("NLZIET login failed — cannot verify API contracts")

        token = cls.handler.get_valid_token()
        cls.headers = {
            "Authorization": f"Bearer {token}",
            "Nlziet-AppName": "WebApp",
            "Nlziet-AppVersion": "5.65.5",
            "Accept": "application/json",
        }

    @classmethod
    def tearDownClass(cls):
        if Logger.instance():
            Logger.instance().close_log()

    # -- helpers -----------------------------------------------------------

    def _get_json(self, url):
        """Fetch a URL and parse as JSON, failing on empty/invalid response."""
        raw = UriHandler.open(url, additional_headers=self.headers, no_cache=True)
        self.assertTrue(raw, f"Empty response from {url}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            self.fail(f"Non-JSON response from {url}")

    def _assert_keys(self, obj, required_keys, context=""):
        """Assert a dict contains all required keys."""
        missing = set(required_keys) - set(obj.keys())
        self.assertFalse(missing, f"Missing keys {missing} in {context}")

    # -- placement ---------------------------------------------------------

    def test_home_placement_has_components(self):
        """Home placement returns a components array."""
        data = self._get_json(API_V9_PLACEMENT.format("home"))
        self.assertIn("components", data)
        self.assertIsInstance(data["components"], list)
        self.assertGreater(len(data["components"]), 0)

    def test_home_placement_has_explore_pages(self):
        """Home placement contains a Placements component with explore items."""
        data = self._get_json(API_V9_PLACEMENT.format("home"))
        placements = [c for c in data["components"]
                      if c.get("type") == "Placements"]
        self.assertGreater(len(placements), 0,
                           "No Placements component in home response")
        items = placements[0].get("items", [])
        self.assertGreater(len(items), 0, "Placements component has no items")
        for item in items:
            self._assert_keys(item, ["id", "title"],
                              f"Placements item {item}")

    def test_home_placement_item_tile_list_has_url(self):
        """ItemTileList components have a url field."""
        data = self._get_json(API_V9_PLACEMENT.format("home"))
        tile_lists = [c for c in data["components"]
                      if c.get("type") == "ItemTileList"]
        self.assertGreater(len(tile_lists), 0,
                           "No ItemTileList in home response")
        for tile in tile_lists:
            self.assertIn("url", tile,
                          f"ItemTileList '{tile.get('title')}' missing url")

    # -- explore pages -----------------------------------------------------

    def test_explore_movies_has_filters_and_genres(self):
        """explore-movies returns Filters and genre ItemTileLists."""
        data = self._get_json(API_V9_PLACEMENT.format("explore-movies"))
        components = data.get("components", [])

        filters = [c for c in components if c.get("type") == "Filters"]
        self.assertEqual(len(filters), 1,
                         "Expected exactly one Filters component")
        self._assert_keys(filters[0], ["url", "itemsUrl"],
                          "Filters component")
        self.assertIn("genres/movies", filters[0]["url"])

        tile_lists = [c for c in components
                      if c.get("type") == "ItemTileList"]
        self.assertGreater(len(tile_lists), 0,
                           "No genre ItemTileLists in explore-movies")

    def test_explore_series_structure(self):
        """explore-series returns Filters component and genre rows."""
        data = self._get_json(API_V9_PLACEMENT.format("explore-series"))
        components = data.get("components", [])

        filters = [c for c in components if c.get("type") == "Filters"]
        self.assertEqual(len(filters), 1)
        self.assertIn("genres/series", filters[0]["url"])

        tile_lists = [c for c in components
                      if c.get("type") == "ItemTileList"]
        self.assertGreater(len(tile_lists), 0)

    def test_explore_programs_structure(self):
        """explore-programs returns Filters and many genre rows."""
        data = self._get_json(API_V9_PLACEMENT.format("explore-programs"))
        components = data.get("components", [])

        filters = [c for c in components if c.get("type") == "Filters"]
        self.assertEqual(len(filters), 1)
        self.assertIn("genres/programs", filters[0]["url"])

    def test_explore_youth_no_filters(self):
        """explore-youth has no Filters component (curated only)."""
        data = self._get_json(API_V9_PLACEMENT.format("explore-youth"))
        components = data.get("components", [])

        filters = [c for c in components if c.get("type") == "Filters"]
        self.assertEqual(len(filters), 0,
                         "Youth page should not have genre filters")

        tile_lists = [c for c in components
                      if c.get("type") == "ItemTileList"]
        self.assertGreater(len(tile_lists), 0)

    def test_explore_documentaries_structure(self):
        """explore-documentaries returns ItemTileLists, no Filters."""
        data = self._get_json(API_V9_PLACEMENT.format("explore-documentaries"))
        components = data.get("components", [])

        tile_lists = [c for c in components
                      if c.get("type") == "ItemTileList"]
        self.assertGreater(len(tile_lists), 0)

    # -- recommend ---------------------------------------------------------

    def test_recommend_filtered_returns_data_array(self):
        """recommend/filtered returns {data: [...]} with content objects."""
        url = f"{API_V9_RECOMMEND_FILTERED}?category=Movies&genre=Drama&limit=5"
        data = self._get_json(url)
        self.assertIn("data", data)
        self.assertIsInstance(data["data"], list)
        if data["data"]:
            item = data["data"][0]
            self.assertIn("content", item)
            self._assert_keys(item["content"],
                              ["id", "title", "type"],
                              "recommend/filtered item.content")

    # -- EPG ---------------------------------------------------------------

    def test_epg_live_returns_data_with_channels(self):
        """EPG live endpoint returns {data: [...]} with channel objects."""
        data = self._get_json(API_V9_EPG_LIVE)
        self.assertIn("data", data)
        self.assertIsInstance(data["data"], list)
        self.assertGreater(len(data["data"]), 0)
        first = data["data"][0]
        self.assertIn("channel", first)
        self._assert_keys(first["channel"], ["content"],
                          "EPG live channel")
        self._assert_keys(first["channel"]["content"], ["id", "title"],
                          "EPG live channel.content")

    # -- search ------------------------------------------------------------

    def test_search_returns_content_items(self):
        """Search endpoint returns {data: [...]} with content objects."""
        url = API_V9_SEARCH % "test"
        data = self._get_json(url)
        self.assertIn("data", data)
        self.assertIsInstance(data["data"], list)
        if data["data"]:
            item = data["data"][0]
            self.assertIn("content", item)
            self._assert_keys(item["content"],
                              ["id", "title", "type"],
                              "search result content")

    # -- profile -----------------------------------------------------------

    def test_profile_returns_list(self):
        """Profile endpoint returns a list of profile objects."""
        data = self._get_json(API_V8_PROFILE)
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0)
        self._assert_keys(data[0],
                          ["id", "displayName", "type"],
                          "profile item")

    # -- series (needs a real series ID, discovered from search) -----------

    def test_series_detail_structure(self):
        """Series detail returns content with seasons array."""
        # Use genre-filtered series to find a real, available series ID
        url = f"{API_V9_RECOMMEND_FILTERED}?category=Series&genre=Drama&limit=5"
        filtered = self._get_json(url)
        items = filtered.get("data", [])
        series = [i for i in items
                  if isinstance(i, dict) and i.get("content", {}).get("id")]
        if not series:
            self.skipTest("No series found via recommend/filtered")

        for candidate in series[:3]:
            series_id = candidate["content"]["id"]
            raw = UriHandler.open(
                API_V8_SERIES.format(series_id),
                additional_headers=self.headers, no_cache=True)
            if not raw:
                continue
            data = json.loads(raw)
            self.assertIn("content", data)
            self._assert_keys(data["content"],
                              ["id", "title", "seasons"],
                              "series detail content")
            self.assertIsInstance(data["content"]["seasons"], list)
            return

        self.skipTest("All candidate series returned 404")
