import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import AsyncMock, patch

from backend.app.agent.smoke_check import API_URL, MODEL, main, run_check, smoke_config


class SmokeCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_mock_mode_cannot_open_a_socket(self):
        with patch.dict(os.environ, {}, clear=True):
            config = smoke_config()
        with patch("socket.socket.connect", side_effect=AssertionError("Network is forbidden")):
            report = await run_check(config)

        self.assertEqual(report["mode"], "mock")
        self.assertTrue(report["success"])
        self.assertTrue(report["reply_matches_expected"])
        self.assertFalse(report["authorization_configured"])
        self.assertIsNone(report["usage"])
        self.assertEqual(report["request"]["url"], API_URL)
        self.assertEqual(report["request"]["json"]["model"], MODEL)
        self.assertEqual(report["request"]["json"]["max_tokens"], 32)

    async def test_header_is_transmitted_to_mock_but_redacted_in_report(self):
        # This tests a possible header configuration, not the organizer's auth scheme.
        with patch.dict(
            os.environ,
            {"AGENT_LLM_AUTH_HEADER": "X-Test-Key", "AGENT_LLM_AUTH_VALUE": "smoke-test-secret"},
            clear=True,
        ):
            config = smoke_config()
        report = await run_check(config)
        self.assertTrue(report["success"])
        self.assertTrue(report["authorization_configured"])
        self.assertEqual(report["request"]["headers"]["X-Test-Key"], "<redacted>")
        self.assertNotIn("smoke-test-secret", json.dumps(report))

    async def test_live_mode_without_auth_is_blocked_before_client_call(self):
        with patch.dict(os.environ, {}, clear=True):
            config = smoke_config()
        with patch("backend.app.agent.smoke_check.ChatCompletionsClient.generate", new_callable=AsyncMock) as generate:
            with self.assertRaises(ValueError):
                await run_check(config, send=True)
        generate.assert_not_awaited()


class SmokeCheckCommandTests(unittest.TestCase):
    def test_command_defaults_to_mock_and_does_not_print_secret(self):
        output = io.StringIO()
        with patch.dict(
            os.environ,
            {"AGENT_LLM_AUTH_HEADER": "X-Test-Key", "AGENT_LLM_AUTH_VALUE": "smoke-test-secret"},
            clear=True,
        ), redirect_stdout(output):
            exit_code = main([])
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue())["mode"], "mock")
        self.assertNotIn("smoke-test-secret", output.getvalue())
