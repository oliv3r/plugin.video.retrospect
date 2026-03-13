# SPDX-License-Identifier: GPL-3.0-or-later
"""Mock response data for NLZIET channel tests."""

MOCK_APPCONFIG_RESPONSE = {
    "isAppBlocked": False,
    "appBlockedReason": "App is currently unavailable.",
    "isUpdateRequired": False,
    "heartbeatInterval": 90,
    "epgCacheTime": 300,
}

MOCK_EPG_LIVE_RESPONSE = {
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
