# SPDX-License-Identifier: GPL-3.0-or-later
"""Pytest configuration: install xbmcgui stubs missing from sakee.

``xbmcgui.WindowXMLDialog`` is not provided by the sakee emulator. Installing
``FakeWindowXMLDialog`` here ensures every module that imports
``DeviceAuthDialog`` (which inherits from ``WindowXMLDialog``) gets a usable
base class regardless of test collection order.

A session-scoped logger is also initialised here so tests that indirectly
trigger ``KodiSettings`` (via ``LanguageHelper``) work in isolation.
"""

import pytest
import xbmcgui
import sys
import os
from typing import Generator

sys.path.insert(0, os.path.dirname(__file__))
from fakexbmcgui import FakeWindowXMLDialog  # noqa: F401


if not hasattr(xbmcgui, "WindowXMLDialog"):
    xbmcgui.WindowXMLDialog = FakeWindowXMLDialog


@pytest.fixture(scope="session", autouse=True)
def _session_logger() -> Generator[None, None, None]:
    """Initialise the global Logger once for the entire test session."""
    from resources.lib.logger import Logger
    Logger.create_logger(None, "pytest-session", min_log_level=0)
    yield
    logger = Logger.instance()
    if logger is not None:
        logger.close_log()
