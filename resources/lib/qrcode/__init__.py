# SPDX-License-Identifier: GPL-3.0-or-later
"""QR code wrapper around vendored python-qrcode subtree.

The upstream library lives unmodified at resources/lib/python-qrcode/
(git subtree from lincolnloop/python-qrcode). We add its path to
sys.path at import time and inject a stdlib-only PNG writer so that
the library works without pypng or PIL.
"""

import os
import sys

# Temporarily add the vendored subtree to sys.path so that the upstream
# code's absolute imports (``from qrcode.xxx import ...``) resolve.
_vendor = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, "python-qrcode")
)
sys.path.insert(0, _vendor)

# Inject our stdlib PNG writer before the rest of qrcode loads.
# The upstream compat/png.py sets PngWriter = None when pypng is
# absent; we replace it with a minimal writer using only zlib/struct
# so that PyPNGImage always works (no external deps).
from resources.lib.qrcode._png_writer import PngWriter as _StdlibPngWriter

import qrcode.compat.png  # noqa: E402
qrcode.compat.png.PngWriter = _StdlibPngWriter

# pure.py imports PngWriter at module level, so we must patch
# compat.png BEFORE pure.py is loaded.  Force-import it now so
# it picks up the injected writer.
import qrcode.image.pure  # noqa: E402
qrcode.image.pure.PngWriter = _StdlibPngWriter

# PIL is not available in Kodi — stub the module so make_image()
# can fall back to PyPNGImage without hitting ModuleNotFoundError.
import types as _types

_pil_stub = _types.ModuleType("qrcode.image.pil")
_pil_stub.Image = None
_pil_stub.PilImage = None
sys.modules["qrcode.image.pil"] = _pil_stub

# Re-export public API
from qrcode import QRCode, make  # noqa: E402, F401
from qrcode.constants import (  # noqa: E402, F401
    ERROR_CORRECT_H,
    ERROR_CORRECT_L,
    ERROR_CORRECT_M,
    ERROR_CORRECT_Q,
)

sys.path.remove(_vendor)
