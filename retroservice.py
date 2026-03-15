# SPDX-License-Identifier: GPL-3.0-or-later

import xbmc
import xbmcaddon

from resources.lib.service import run_service


def autorun_retrospect():
    if xbmcaddon.Addon().getSetting("auto_run") == "true":
        xbmc.executebuiltin('RunAddon(plugin.video.retrospect)')
    return


autorun_retrospect()
run_service()
