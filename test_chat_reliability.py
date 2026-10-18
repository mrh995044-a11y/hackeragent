"""Regression tests for chat delivery and runaway agent tool calls."""
import contextlib
import copy
import io
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import aichat
import agent


class AichatReliabilityTests(unittest.TestCase):
    @patch("aichat.load_env_file")
    @patch("aichat.send")
    @patch("builtins.input", side_effect=["first request", "second request", "exit"])
    def test_failed_interactive_send_is_removed_from_history(self, _input, send, _load_env):
        observed_histories = []

        def fake_send(_args, messages, _system):
            observed_histories.append(copy.deepcopy(messages))
            if len(observed_histories) == 1:
                raise RuntimeError("offline")
            return "reply"

        send.side_effect = fake_send
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(aichat.main([]), 0)

        self.assertEqual(observed_histories, [
            [{"role": "user", "content": "first request"}],
            [{"role": "user", "content": "second request"}],
        ])

    @patch("aichat.load_env_file")
    @patch("aichat.send", side_effect=RuntimeError("service unavailable"))
    def test_one_shot_request_error_returns_nonzero_without_traceback(self, _send, _load_env):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = aichat.main(["hello", "--api-key", "test-key"])
        self.assertEqual(result, 1)
        self.assertIn("no answer was received", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_sse_parser_accepts_bytes_without_a_response_charset(self):
        response = SimpleNamespace(iter_lines=lambda decode_unicode: iter([
            bytes.fromhex("efbbbf") + b"data: answer", b"data: [DONE]",
        ]))
        self.assertEqual(list(aichat._iter_sse(response)), ["answer"])

    @patch("aichat.chat_openai", return_value="reply")
    def test_aichat_routes_groq_to_openai_compatible_adapter(self, chat_openai):
        args = SimpleNamespace(
            provider="groq", api_key="test-key", base_url=None, model=None,
            stream=False, temperature=0.2, max_tokens=None, timeout=5, retries=0,
        )
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(aichat.send(args, [{"role": "user", "content": "hello"}], None), "reply")
        self.assertEqual(chat_openai.call_args.args[:3],
                         (aichat.DEFAULT_BASE["groq"], "test-key", aichat.DEFAULT_MODEL["groq"]))

    @patch("aichat.chat_openai", return_value="reply")
    def test_ollama_does_not_prompt_for_an_api_key(self, chat_openai):
        args = SimpleNamespace(
            provider="ollama", api_key=None, base_url=None, model=None,
            stream=False, temperature=0.2, max_tokens=None, timeout=5, retries=0,
        )
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(aichat.send(args, [{"role": "user", "content": "hello"}], None), "reply")
        self.assertEqual(chat_openai.call_args.args[1], "ollama")

    @patch("aichat.requests.post")
    def test_gemini_key_is_sent_in_header_not_url(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        post.return_value = response

        answer = aichat.chat_gemini(
            "https://example.test/v1beta", "secret-key", "model", [],
            system=None, temperature=0.2, max_tokens=None, timeout=5, retries=0,
        )

        self.assertEqual(answer, "ok")
        self.assertNotIn("secret-key", post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], "secret-key")


class AgentReliabilityTests(unittest.TestCase):
    def test_agent_sse_parser_accepts_byte_lines(self):
        response = SimpleNamespace(iter_lines=lambda decode_unicode: iter([
            b"data: hello", b"data: [DONE]",
        ]))
        self.assertEqual(list(agent._iter_sse(response)), ["hello"])

    def _cfg(self, workspace):
        return SimpleNamespace(
            provider="openai", model="mock", max_steps=5, stream=False,
            workspace=workspace, allow_outside_workspace=False, yes=True,
            timeout=5, retries=0, temperature=0.0, base_url="http://mock/v1", key="local",
        )

    @patch("agent.execute_tool", return_value="tool finished")
    @patch("agent.openai_turn")
    def test_repeated_tool_request_stops_without_repeating_side_effect(self, openai_turn, execute_tool):
        repeated_call = {
            "type": "function",
            "function": {"name": "write_file", "arguments": '{"path":"out.txt","content":"x"}'},
        }
        openai_turn.side_effect = [
            ("", [{**repeated_call, "id": "call_1"}]),
            ("", [{**repeated_call, "id": "call_2"}]),
        ]
        history = [{"role": "user", "content": "write a file"}]
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as workspace, contextlib.redirect_stdout(stdout):
            agent.run_agent(self._cfg(workspace), history, "system")

        execute_tool.assert_called_once()
        self.assertEqual(openai_turn.call_count, 2)
        self.assertIn("stopped after a repeated tool request", stdout.getvalue())
        self.assertEqual(history[-2]["role"], "tool")
        self.assertIn("loop prevention", history[-2]["content"])
        self.assertEqual(history[-1]["role"], "assistant")

    @patch("agent.execute_tool", return_value="tool finished")
    @patch("agent.anthropic_turn")
    def test_anthropic_repeated_tool_request_keeps_tool_result_pairing(self, anthropic_turn, execute_tool):
        cfg = self._cfg(tempfile.gettempdir())
        cfg.provider = "anthropic"
        repeated_call = {"id": "toolu_1", "function": {
            "name": "system_info", "arguments": "{}",
        }}
        anthropic_turn.side_effect = [
            ("", [repeated_call]),
            ("", [{**repeated_call, "id": "toolu_2"}]),
        ]
        history = [{"role": "user", "content": "inspect the system"}]

        with contextlib.redirect_stdout(io.StringIO()):
            agent.run_agent(cfg, history, "system")

        execute_tool.assert_called_once()
        self.assertEqual(history[-2]["role"], "user")
        self.assertEqual(history[-2]["content"][0]["type"], "tool_result")
        self.assertEqual(history[-1]["role"], "assistant")

    @patch("agent.execute_tool", return_value="tool finished")
    @patch("agent.gemini_turn")
    def test_gemini_repeated_tool_request_keeps_function_response(self, gemini_turn, execute_tool):
        cfg = self._cfg(tempfile.gettempdir())
        cfg.provider = "gemini"
        raw_parts = [{"functionCall": {"name": "system_info", "args": {}}}]
        repeated_call = {"id": "call_system_info", "function": {
            "name": "system_info", "arguments": "{}",
        }}
        gemini_turn.side_effect = [
            ("", [repeated_call], raw_parts),
            ("", [repeated_call], raw_parts),
        ]
        history = [{"role": "user", "content": "inspect the system"}]

        with contextlib.redirect_stdout(io.StringIO()):
            agent.run_agent(cfg, history, "system")

        execute_tool.assert_called_once()
        self.assertEqual(history[-2]["role"], "user")
        self.assertIn("functionResponse", history[-2]["parts"][0])
        self.assertEqual(history[-1]["role"], "model")

    @patch("agent.execute_tool")
    @patch("agent.openai_turn")
    def test_invalid_tool_json_is_returned_as_a_tool_error(self, openai_turn, execute_tool):
        openai_turn.side_effect = [
            ("", [{"id": "call_bad", "type": "function", "function": {
                "name": "read_file", "arguments": "not-json",
            }}]),
            ("Recovered.", []),
        ]
        history = [{"role": "user", "content": "read a file"}]

        with tempfile.TemporaryDirectory() as workspace, contextlib.redirect_stdout(io.StringIO()):
            agent.run_agent(self._cfg(workspace), history, "system")

        execute_tool.assert_not_called()
        assistant_call = history[1]["tool_calls"][0]
        self.assertEqual(assistant_call["function"]["arguments"], "{}")
        self.assertIn("invalid arguments", history[2]["content"])
        self.assertEqual(history[-1], {"role": "assistant", "content": "Recovered."})

    @patch("agent.print_banner")
    @patch("agent.run_agent", side_effect=RuntimeError("service unavailable"))
    @patch("agent.prompt_animated_cursor", side_effect=["failed request", "/history", "/exit"])
    def test_interactive_agent_drops_unanswered_turn_after_request_error(
        self, _prompt, _run_agent, _banner
    ):
        cfg = SimpleNamespace(provider="openai", model="mock", system="system")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(agent.interactive(cfg), 0)
        self.assertIn("0 messages", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
