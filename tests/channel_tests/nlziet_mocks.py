# SPDX-License-Identifier: GPL-3.0-or-later
"""Mock response data for NLZIET channel tests."""

from typing import Any, Dict

MOCK_APPCONFIG_RESPONSE: Dict[str, Any] = {
    "isAppBlocked": False,
    "appBlockedReason": "",
    "isUpdateRequired": False,
    "updateText": "",
    "heartbeatInterval": 90,
}

MOCK_EPG_LIVE_RESPONSE: Dict[str, Any] = {
    "data": [
        {
            "channel": {
                "content": {
                    "id": "test-live-1",
                    "title": "Test Channel 1",
                    "logo": {"normalUrl": "https://example.com/test-live-1.png"},
                }
            },
            "programLocations": [
                {"content": {
                    "assetId": "live-abc",
                    "title": "Test Programme 1",
                    "contentItemId": "item-001",
                    "image": {"landscapeUrl": "https://example.com/landscape-1.jpg"},
                    "firstBroadcast": "2026-03-10T20:00:00+01:00",
                }}
            ],
        },
        {
            "channel": {
                "content": {
                    "id": "test-live-2",
                    "title": "Test Channel 2",
                    "logo": {"normalUrl": "https://example.com/test-live-2.png"},
                }
            },
            "programLocations": [
                {"content": {
                    "assetId": "live-def",
                    "title": "Test Programme 2",
                    "contentItemId": "item-002",
                    "image": {"landscapeUrl": "https://example.com/landscape-2.jpg"},
                    "firstBroadcast": "2026-03-11T21:00:00+01:00",
                }}
            ],
        },
    ]
}
