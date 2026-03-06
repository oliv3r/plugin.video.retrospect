# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the NLZIET API endpoint constants (api.py).

These tests verify the structural contracts of the path constants so that
accidental edits to a path are caught immediately without needing a live server.
"""

import sys
import unittest

sys.path.insert(0, "channels/channel.nlziet/nlziet")
import api  # noqa: E402


class TestApiPaths(unittest.TestCase):
    """Every endpoint path must be a relative path starting with /."""

    def _all_url_constants(self):
        return [
            (name, value)
            for name, value in vars(api).items()
            if name.startswith("API_") and isinstance(value, str)
        ]

    def test_all_endpoints_are_relative_paths(self):
        for name, value in self._all_url_constants():
            self.assertTrue(
                value.startswith("/"),
                "Endpoint {} is not a relative path: {!r}".format(name, value))

    def test_no_endpoint_is_empty(self):
        for name, value in self._all_url_constants():
            self.assertTrue(value.strip(), "Endpoint {} is empty".format(name))

    def test_no_endpoint_contains_host(self):
        for name, value in self._all_url_constants():
            self.assertNotIn(
                "://", value,
                "Endpoint {} must be a relative path, not a full URL: {!r}".format(name, value))
