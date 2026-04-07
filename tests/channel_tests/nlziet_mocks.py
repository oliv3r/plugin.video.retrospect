# SPDX-License-Identifier: GPL-3.0-or-later
"""Mock response data for NLZIET channel tests."""

from typing import Any, Dict

MOCK_APPCONFIG_RESPONSE: Dict[str, Any] = {
    "isAppBlocked": False,
    "appBlockedReason": "",
    "isUpdateRequired": False,
    "updateText": "",
    "heartbeatInterval": 90,
    "epgCacheTime": 300,
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
                {"content": {"assetId": "live-abc", "title": "Test Programme 1"}}
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
                {"content": {"assetId": "live-def", "title": "Test Programme 2"}}
            ],
        },
    ]
}
