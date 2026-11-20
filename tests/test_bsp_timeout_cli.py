import os
import sys
import unittest
from unittest.mock import patch

from storage_node.__main__ import main


class BspTimeoutCliTests(unittest.TestCase):
    def run_cli(self, args, env):
        with patch.dict(os.environ, env), patch.object(sys, "argv", ["storage_node", *args]), \
             patch("storage_node.__main__.StorageNode") as node, \
             patch("storage_node.__main__.asyncio.run", side_effect=lambda coro: coro.close()):
            main()
        return node.call_args_list

    def test_environment_config_reaches_single_node(self):
        calls = self.run_cli(["--cluster", "0"], {
            "NODE_IDLE_TIMEOUT_S": "42", "NODE_HEADER_TIMEOUT_S": "12",
            "NODE_PAYLOAD_IDLE_TIMEOUT_S": "90",
        })
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].kwargs["idle_timeout"], 42)
        self.assertEqual(calls[0].kwargs["header_timeout"], 12)
        self.assertEqual(calls[0].kwargs["payload_idle_timeout"], 90)

    def test_flags_override_environment_for_every_cluster_node(self):
        calls = self.run_cli(["--cluster", "3", "--idle-timeout", "45", "--header-timeout", "15",
                              "--payload-idle-timeout", "120"], {
            "NODE_IDLE_TIMEOUT_S": "42", "NODE_HEADER_TIMEOUT_S": "12",
            "NODE_PAYLOAD_IDLE_TIMEOUT_S": "90",
        })
        self.assertEqual(len(calls), 3)
        for call in calls:
            self.assertEqual(call.kwargs["idle_timeout"], 45)
            self.assertEqual(call.kwargs["header_timeout"], 15)
            self.assertEqual(call.kwargs["payload_idle_timeout"], 120)
