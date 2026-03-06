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
