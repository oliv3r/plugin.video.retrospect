# SPDX-License-Identifier: GPL-3.0-or-later

from .channeltest import ChannelTest


class TestNLZietChannel(ChannelTest):
    # noinspection PyPep8Naming
    def __init__(self, methodName):  # NOSONAR
        super(TestNLZietChannel, self).__init__(methodName, "channel.nl.nlziet", None)

    def test_channel_exists(self):
        self.assertIsNotNone(self.channel)

    def test_process_folder_list_login_failure(self):
        """process_folder_list returns None when login fails."""
        original_log_on = self.channel.log_on
        try:
            self.channel.log_on = lambda *a, **kw: False
            result = self.channel.process_folder_list(None)
            self.assertIsNone(result)
        finally:
            self.channel.log_on = original_log_on
