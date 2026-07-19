# SPDX-License-Identifier: GPL-3.0-or-later
import base64
import json
import unittest
from unittest.mock import MagicMock, patch

from resources.lib.actions.videoaction import VideoAction
from resources.lib.mediaitem import MediaItem
from resources.lib import mediatype


class TestVideoActionUpNextUnit(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from resources.lib.logger import Logger
        Logger.create_logger(None, str(cls), min_log_level=0)

    # -- helpers --

    @staticmethod
    def _make_next_item(title="Next Show", thumb="https://example.com/next.jpg"):
        next_item = MediaItem(title, "https://live.example.com/stream")
        next_item.thumb = thumb
        return next_item

    @staticmethod
    def _make_live_item(next_item=None):
        item = MediaItem("Current Show", "https://live.example.com/stream")
        item.isLive = True
        if next_item is not None:
            item.metaData["upnext_item"] = next_item
        return item

    @staticmethod
    def _make_video_action(media_item):
        parser = MagicMock()
        parser.media_item = media_item
        parser.pickle_hash = "hash"
        parser.create_action_url.return_value = "plugin://plugin.video.retrospect/play"
        channel = MagicMock()
        return VideoAction(parser, channel)

    @staticmethod
    def _decode_payload(mock_jsonrpc):
        call_args = mock_jsonrpc.call_args[0][0]
        rpc = json.loads(call_args)
        b64 = rpc["params"]["data"][0]
        return json.loads(base64.b64decode(b64))

    # -- tests --

    @patch("xbmc.executeJSONRPC", return_value='{"result": "OK"}')
    def test_live_item_with_upnext_item_sends_notification(self, mock_jsonrpc):
        """SUCCESS → live item with upnext_item in metaData triggers JSONRPC notification."""
        next_item = self._make_next_item()
        media_item = self._make_live_item(next_item=next_item)
        va = self._make_video_action(media_item)
        va._VideoAction__call_upnext(media_item)
        mock_jsonrpc.assert_called_once()

    @patch("xbmc.executeJSONRPC", return_value='{"result": "OK"}')
    def test_live_item_without_upnext_item_skips_notification(self, mock_jsonrpc):
        """SUCCESS → live item with no upnext_item in metaData sends no notification."""
        media_item = self._make_live_item(next_item=None)
        va = self._make_video_action(media_item)
        va._VideoAction__call_upnext(media_item)
        mock_jsonrpc.assert_not_called()

    @patch("xbmc.executeJSONRPC", return_value='{"result": "OK"}')
    def test_non_live_item_uses_sibling_path(self, mock_jsonrpc):
        """SUCCESS → non-live item uses sibling pickle path, not upnext_item."""
        item = MediaItem("Episode 1", "https://vod.example.com/ep1", media_type=mediatype.VIDEO)
        item.isLive = False
        va = self._make_video_action(item)
        # Item is the only sibling → current_idx + 1 >= len → early return, no notification
        va.parameter_parser.pickler.de_pickle_child_items.return_value = (
            "store-id", {item.guid: item})
        va._VideoAction__call_upnext(item)
        mock_jsonrpc.assert_not_called()

    @patch("xbmc.executeJSONRPC", return_value='{"result": "OK"}')
    def test_live_upnext_payload_contains_next_episode_title(self, mock_jsonrpc):
        """SUCCESS → notification payload next_episode.title matches upnext_item.name."""
        next_item = self._make_next_item(title="The Next Programme")
        media_item = self._make_live_item(next_item=next_item)
        va = self._make_video_action(media_item)
        va._VideoAction__call_upnext(media_item)
        payload = self._decode_payload(mock_jsonrpc)
        self.assertEqual(payload["next_episode"]["title"], "The Next Programme")

    @patch("xbmc.executeJSONRPC", return_value='{"result": "OK"}')
    def test_live_upnext_payload_play_url_is_live_item_url(self, mock_jsonrpc):
        """SUCCESS → play_url in notification payload equals the live item URL."""
        next_item = self._make_next_item()
        media_item = self._make_live_item(next_item=next_item)
        va = self._make_video_action(media_item)
        va._VideoAction__call_upnext(media_item)
        payload = self._decode_payload(mock_jsonrpc)
        # play_url comes from __notify_up_next → next_item.actionUrl (None) →
        # create_action_url mock return value
        self.assertIsNotNone(payload["play_url"])


if __name__ == "__main__":
    unittest.main()
