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
