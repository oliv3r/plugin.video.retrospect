# SPDX-License-Identifier: GPL-3.0-or-later
"""NLZIET API endpoint constants.

Relative paths only — the host is provided by ``self.baseUrl`` in
``Channel.__init__``.  Import as ``import api`` and combine with
``self.baseUrl`` at call sites.

Naming convention
-----------------
    API_V{n}_RESOURCE         - relative path (starts with /)
    API_V{n}_RESOURCE_PREFIX  - parser match pattern (prefix-match)
"""

# -- v7 -----------------------------------------------------------------------
API_V7_APPCONFIG = "/v7/appconfig"

# -- v8 -----------------------------------------------------------------------
API_V8_PROFILE = "/v8/profile"

# -- v9 -----------------------------------------------------------------------
API_V9_EPG_LIVE = "/v9/epg/programlocations/live"
API_V9_EPG_LIVE_PREFIX = "/v9/epg/programlocations/live"

API_V9_EPG_LIVE_CHANNEL = "/v9/epg/programlocations/live?channel={}"

API_V9_LIVE_HANDSHAKE = (
    "/v9/stream/handshake"
    "?context=Live"
    "&channel={}"
    "&drmType=Widevine"
    "&sourceType=Dash"
    "&playerName=BitmovinWeb"
    "&offsetType=Live"
)
