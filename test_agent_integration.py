"""End-to-end public CLI tests using a local OpenAI-compatible mock server."""
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import agent


class MockOpenAIHandler(BaseHTTPRequestHandler):
    requests_seen = []
    mode = "write"

    def log_message(self, _format, *_args):
        pass

    def do_POST(self):
        size = int(self.headers["Content-Length"])
        payload = json.loads(self.rfile.read(size))
        type(self).requests_seen.append(payload)
        calls = sum(1 for request in type(self).requests_seen if request["messages"][-1].get("role") != "tool")
        if len(type(self).requests_seen) == 1:
            path = "result.txt" if type(self).mode == "write" else "../escaped.txt"
            response = {"choices": [{"message": {"content": None, "tool_calls": [{
                "id": "call_1", "type": "function", "function": {
                    "name": "write_file", "arguments": json.dumps({"path": path, "content": "safe output"})
                }}]}}]}
        else:
            response = {"choices": [{"message": {"content": "Completed safely.", "tool_calls": []}}]}
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


@contextlib.contextmanager
def mock_openai(mode):
    MockOpenAIHandler.mode = mode
    MockOpenAIHandler.requests_seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockOpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


class AgentCliIntegrationTests(unittest.TestCase):
    def run_cli(self, base_url, workspace):
        with contextlib.redirect_stdout(io.StringIO()):
            return agent.main(["--base-url", base_url, "--model", "mock", "--workspace", workspace,
                               "--yes", "--no-stream", "-p", "perform a tool action"])

    def test_cli_executes_approved_write_inside_workspace(self):
        with tempfile.TemporaryDirectory() as workspace, mock_openai("write") as base_url:
            self.assertEqual(self.run_cli(base_url, workspace), 0)
            with open(os.path.join(workspace, "result.txt"), encoding="utf-8") as file:
                self.assertEqual(file.read(), "safe output")
            self.assertEqual(len(MockOpenAIHandler.requests_seen), 2)
            self.assertTrue(MockOpenAIHandler.requests_seen[1]["messages"][-1]["content"].startswith("[ok] wrote 11 chars to "))

    def test_cli_denies_model_path_escape_and_reports_it_to_model(self):
        with tempfile.TemporaryDirectory() as workspace, mock_openai("escape") as base_url:
            self.assertEqual(self.run_cli(base_url, workspace), 0)
            self.assertFalse(os.path.exists(os.path.join(os.path.dirname(workspace), "escaped.txt")))
            tool_result = MockOpenAIHandler.requests_seen[1]["messages"][-1]["content"]
            self.assertIn("[denied] path is outside workspace", tool_result)


if __name__ == "__main__":
    unittest.main()
