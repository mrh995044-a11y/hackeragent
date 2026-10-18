"""Tests for specialist routing and opt-in Kali/WSL controls."""
import contextlib
import io
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import agent


class AgentRoutingTests(unittest.TestCase):
    def test_planner_output_is_limited_to_four_usable_tasks(self):
        plan = {"tasks": [
            {"agent": "researcher", "task": f"Task {i}"} for i in range(6)
        ]}
        parsed = agent._parse_agent_plan(json.dumps(plan), "original")
        self.assertEqual(len(parsed), 4)
        self.assertEqual(parsed[0], {"agent": "researcher", "task": "Task 0"})

    def test_invalid_planner_role_falls_back_to_generalist(self):
        parsed = agent._parse_agent_plan(
            '{"tasks":[{"agent":"root","task":"inspect the project"}]}',
            "original request",
        )
        self.assertEqual(parsed, [{"agent": "generalist", "task": "inspect the project"}])

    @patch("agent._provider_text_turn", side_effect=[
        json.dumps({"tasks": [
            {"agent": "researcher", "task": "inspect the relevant files"},
            {"agent": "coder", "task": "implement the requested fix"},
            {"agent": "tester", "task": "run focused tests"},
        ]}),
        "Coordinated result.",
    ])
    @patch("agent.run_agent")
    def test_planner_runs_specialists_and_synthesizes_reports(self, run_agent, text_turn):
        cfg = SimpleNamespace(provider="openai", auto_install_tools=False)
        history = [{"role": "user", "content": "Fix the bug and test it."}]
        specialist_roles = []

        def run_specialist(_cfg, worker_history, system):
            role = next(role for role in ("researcher", "coder", "tester")
                        if f"the {role} specialist" in system)
            specialist_roles.append(role)
            worker_history.append({"role": "assistant", "content": f"{role} report"})

        run_agent.side_effect = run_specialist
        with contextlib.redirect_stdout(io.StringIO()):
            agent.run_multi_agent(cfg, history, "base system")

        self.assertEqual(specialist_roles, ["researcher", "coder", "tester"])
        self.assertEqual(run_agent.call_count, 3)
        self.assertEqual(history[-1], {"role": "assistant", "content": "Coordinated result."})
        synthesis_prompt = text_turn.call_args_list[-1].args[1]
        for role in specialist_roles:
            self.assertIn(f"{role} report", synthesis_prompt)


class TerminalRenderingTests(unittest.TestCase):
    def test_multiline_provider_response_is_written_without_cursor_rewrites(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            agent.animated_print("First line" + chr(10) + "Second line")
        self.assertEqual(output.getvalue(), "First line" + chr(10) + "Second line" + chr(10))
        self.assertNotIn(chr(8), output.getvalue())

    def test_gemini_text_is_printed_after_spinner_stops(self):
        events = []

        class FakeSpinner:
            def __init__(self, _message):
                pass

            def __enter__(self):
                events.append("spinner-start")
                return self

            def __exit__(self, _type, _value, _traceback):
                events.append("spinner-stop")

        cfg = SimpleNamespace(provider="gemini", max_steps=1)
        history = [{"role": "user", "content": "hello"}]
        with patch("agent.Spinner", FakeSpinner):
            with patch("agent.gemini_turn", return_value=("Complete answer.", [], [{"text": "Complete answer."}])):
                with patch("agent.animated_print", side_effect=lambda text: events.append(("response", text))):
                    with contextlib.redirect_stdout(io.StringIO()):
                        agent.run_agent(cfg, history, "system")
        self.assertEqual(events, ["spinner-start", "spinner-stop", ("response", "Complete answer.")])


class PlainTextProviderTests(unittest.TestCase):
    def test_openai_compatible_router_call_omits_tool_schemas(self):
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": "route"}}]}
        cfg = SimpleNamespace(
            provider="groq", model="mock-model", base_url="https://api.example/v1",
            key="test-key", temperature=0.1, timeout=5, retries=0,
        )
        with patch("agent._post", return_value=response) as post:
            text = agent._provider_text_turn(cfg, "request", "router system")
        self.assertEqual(text, "route")
        url, headers, payload = post.call_args.args[:3]
        self.assertEqual(url, "https://api.example/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        self.assertEqual(payload["messages"][0], {"role": "system", "content": "router system"})
        self.assertNotIn("tools", payload)
        self.assertFalse(payload["stream"])

    def test_gemini_router_call_keeps_api_key_out_of_url(self):
        response = Mock()
        response.json.return_value = {"candidates": [{"content": {
            "parts": [{"text": "route"}],
        }}]}
        cfg = SimpleNamespace(
            provider="gemini", model="gemini-test", base_url="https://generativelanguage.example/v1beta",
            key="AIza-test-key", temperature=0.1, timeout=5, retries=0,
        )
        with patch("agent._post", return_value=response) as post:
            text = agent._provider_text_turn(cfg, "request", "router system")
        self.assertEqual(text, "route")
        url, headers, payload = post.call_args.args[:3]
        self.assertNotIn("AIza-test-key", url)
        self.assertEqual(headers["x-goog-api-key"], "AIza-test-key")
        self.assertEqual(payload["systemInstruction"]["parts"][0]["text"], "router system")


class SecurityFeatureTests(unittest.TestCase):
    def test_local_audit_redacts_secret_values_and_flags_risky_patterns(self):
        secret = "super_secret_value_2026"
        with tempfile.TemporaryDirectory() as workspace:
            with open(f"{workspace}/.env", "w", encoding="utf-8") as file:
                file.write(f"OPENAI_API_KEY={secret}\n")
            with open(f"{workspace}/app.py", "w", encoding="utf-8") as file:
                file.write("subprocess.run(command, shell=True)\n")
            result = agent.tool_local_security_audit({"path": workspace})
        self.assertIn("possible hardcoded credential", result)
        self.assertIn("shell=True subprocess", result)
        self.assertNotIn(secret, result)

    def test_local_audit_default_path_is_workspace_and_escape_is_blocked(self):
        with tempfile.TemporaryDirectory() as workspace:
            cfg = SimpleNamespace(workspace=workspace, allow_outside_workspace=False)
            guarded = agent.guard_tool_args(cfg, "local_security_audit", {})
            self.assertEqual(guarded["path"], workspace)
            with self.assertRaisesRegex(ValueError, "outside workspace"):
                agent.guard_tool_args(cfg, "local_security_audit", {"path": "../outside"})

    def test_web_security_check_uses_one_head_request_and_checks_headers(self):
        response = Mock(status_code=200, headers={
            "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff",
            "Strict-Transport-Security": "max-age=31536000",
        })
        with patch("agent.requests.head", return_value=response) as head:
            result = agent.tool_web_security_check({"url": "https://owned.example/"})
        self.assertIn("TLS certificate validation: passed", result)
        self.assertIn("Content-Security-Policy: present", result)
        self.assertIn("Referrer-Policy: missing", result)
        head.assert_called_once()
        self.assertFalse(head.call_args.kwargs["allow_redirects"])
        response.close.assert_called_once()

    def test_web_security_check_rejects_embedded_credentials(self):
        with patch("agent.requests.head") as head:
            result = agent.tool_web_security_check({"url": "https://user:pass@owned.example/"})
        self.assertIn("embedded credentials", result)
        head.assert_not_called()

    def test_port_scan_is_one_host_bounded_and_sequential(self):
        class FakeSocket:
            def __init__(self, *_args):
                self.timeout = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def settimeout(self, timeout):
                self.timeout = timeout

            def connect(self, address):
                if address[1] != 443:
                    raise ConnectionRefusedError("closed")

        resolved = [(agent.socket.AF_INET, agent.socket.SOCK_STREAM,
                     agent.socket.IPPROTO_TCP, "", ("192.0.2.10", 0))]
        with patch("agent.socket.getaddrinfo", return_value=resolved) as getaddrinfo, \
                patch("agent.socket.socket", side_effect=FakeSocket):
            result = agent.tool_tcp_port_scan({
                "target": "owned.example", "ports": [80, 443, 80], "timeout_ms": 100,
            })
        self.assertIn("resolved to 192.0.2.10; open TCP ports: 443", result)
        getaddrinfo.assert_called_once()

    def test_port_scan_rejects_cidr_and_oversized_port_lists(self):
        with patch("agent.socket.getaddrinfo") as getaddrinfo:
            cidr = agent.tool_tcp_port_scan({"target": "192.0.2.0/24"})
            oversized = agent.tool_tcp_port_scan({
                "target": "owned.example", "ports": list(range(1, 66)),
            })
        self.assertIn("CIDR", cidr)
        self.assertIn("at most 64 ports", oversized)
        getaddrinfo.assert_not_called()

    def test_port_scan_confirmation_is_required_even_with_yes(self):
        cfg = SimpleNamespace(
            workspace=tempfile.gettempdir(), allow_outside_workspace=False, yes=True,
        )
        with patch("agent.confirm", return_value=False) as confirm, \
                patch("agent.tool_tcp_port_scan") as scan, \
                contextlib.redirect_stdout(io.StringIO()):
            result = agent.execute_tool(cfg, "tcp_port_scan", {
                "target": "127.0.0.1", "ports": [80],
            })
        self.assertIn("denied", result)
        confirm.assert_called_once()
        self.assertFalse(confirm.call_args.kwargs["auto"])
        scan.assert_not_called()


class KaliSafetyTests(unittest.TestCase):
    def test_install_is_denied_without_explicit_flag(self):
        cfg = SimpleNamespace(auto_install_tools=False)
        with patch("agent.subprocess.run") as run:
            result = agent.tool_install_kali_tools(cfg, {"packages": ["nmap"]})
        self.assertIn("--auto-install-tools", result)
        run.assert_not_called()

    def test_non_allowlisted_package_is_rejected_before_wsl(self):
        cfg = SimpleNamespace(auto_install_tools=True)
        with patch("agent._is_windows", return_value=True), \
                patch("agent._wsl_executable") as wsl:
            result = agent.tool_install_kali_tools(cfg, {"packages": ["unknown-pkg"]})
        self.assertIn("not on the Kali allowlist", result)
        wsl.assert_not_called()

    def test_allowlisted_packages_install_as_root_inside_kali(self):
        cfg = SimpleNamespace(auto_install_tools=True)
        completed = SimpleNamespace(returncode=0, stdout="installed", stderr="")
        with patch("agent._is_windows", return_value=True), \
                patch("agent._wsl_executable", return_value="wsl.exe"), \
                patch("agent._kali_installed", return_value=(True, "")), \
                patch("agent.subprocess.run", return_value=completed) as run:
            result = agent.tool_install_kali_tools(cfg, {"packages": ["nmap", "jq", "nmap"]})
        self.assertIn("[ok] installed nmap, jq in Kali", result)
        command = run.call_args.args[0]
        self.assertEqual(command[:5], ["wsl.exe", "-d", "kali-linux", "-u", "root"])
        self.assertIn("apt-get install -y -- nmap jq", command[-1])
        self.assertEqual(run.call_args.kwargs["timeout"], 900)

    def test_kali_setup_confirmation_cannot_be_bypassed(self):
        with patch("agent._is_windows", return_value=True), \
                patch("agent.confirm", return_value=False) as confirm, \
                patch("agent.tool_setup_kali") as setup:
            result = agent.setup_kali_with_confirmation()
        self.assertEqual(result, "[denied] Kali setup cancelled")
        confirm.assert_called_once()
        self.assertFalse(confirm.call_args.kwargs["auto"])
        setup.assert_not_called()

    def test_kali_shell_requires_confirmation_unless_yes(self):
        cfg = SimpleNamespace(
            workspace=tempfile.gettempdir(), allow_outside_workspace=False, yes=False,
        )
        with patch("agent.confirm", return_value=False) as confirm, \
                contextlib.redirect_stdout(io.StringIO()):
            result = agent.execute_tool(cfg, "kali_shell", {"command": "id"})
        self.assertIn("denied", result)
        confirm.assert_called_once_with("id", False)

    def test_kali_command_runs_inside_wsl_and_returns_output(self):
        completed = SimpleNamespace(returncode=0, stdout="hello\n", stderr="")
        with patch("agent._is_windows", return_value=True), \
                patch("agent._wsl_executable", return_value="wsl.exe"), \
                patch("agent._kali_installed", return_value=(True, "")), \
                patch("agent.subprocess.run", return_value=completed) as run:
            result = agent.tool_kali_shell({"command": "echo hello", "timeout": 15})
        self.assertEqual(result, "[exit 0]\nhello")
        command = run.call_args.args[0]
        self.assertEqual(command, ["wsl.exe", "-d", "kali-linux", "--", "bash", "-lc", "echo hello"])
        self.assertEqual(run.call_args.kwargs["timeout"], 15)
        self.assertIs(run.call_args.kwargs["stdin"], agent.subprocess.DEVNULL)

    def test_setup_cli_returns_nonzero_if_cancelled(self):
        stdout = io.StringIO()
        with patch("agent.setup_kali_with_confirmation", return_value="[denied] Kali setup cancelled"), \
                contextlib.redirect_stdout(stdout):
            result = agent.main(["--setup-kali"])
        self.assertEqual(result, 1)
        self.assertIn("Kali setup cancelled", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
