#!/usr/bin/env python3
"""HACKERAGENT — a terminal AI agent.

An agentic CLI coding / pentest assistant. It connects to YOUR LLM API key,
runs an agent loop (LLM -> tool call -> execute -> feed result back -> repeat),
and ships these tools:

    shell       run a command on the local machine (confirmation-gated by default)
    read_file   read a file
    write_file  create/overwrite a file
    list_dir    list a directory
    fetch_url   HTTP GET a URL

Use it to build tooling, script tasks, and drive authorised assessments.

Scope: use only against systems you own or are authorised to test.

Examples:
  export OPENAI_API_KEY=sk-...
  python agent.py                          # interactive TUI
  python agent.py -p "write a port scanner and save it to scan.py"
  python agent.py --yes -p "run 'id' and summarise"       # auto-approve tools
  python agent.py --base-url http://localhost:11434/v1 --model llama3.1
  python agent.py --provider anthropic --model claude-3-5-sonnet-latest
"""
from __future__ import annotations

import argparse
import fnmatch
import ipaddress
import json
import math
import os
import platform
import random
import re
import shlex
import socket
import shutil
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit

import requests

# ---------------------------------------------------------------- styling ----
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    GREY = "\033[90m"


def enable_ansi() -> None:
    # Force UTF-8 output so the banner/box-drawing chars render on Windows (cp1252).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if os.name == "nt":
        os.system("")  # enables VT processing on modern Windows terminals


class Spinner:
    """Animated terminal spinner for background tasks / LLM calls."""
    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, message: str = "Thinking"):
        self.message = message
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _spin(self) -> None:
        idx = 0
        colors = [C.CYAN, C.BLUE, C.MAGENTA, C.GREEN]
        while not self._stop.is_set():
            color = colors[idx % len(colors)]
            frame = self.FRAMES[idx % len(self.FRAMES)]
            sys.stdout.write(f"\r  {color}{frame}{C.RESET} {C.GREY}{self.message}...{C.RESET} ")
            sys.stdout.flush()
            idx += 1
            time.sleep(0.08)
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()

    def start(self) -> "Spinner":
        if sys.stdout.isatty():
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.2)

    def __enter__(self) -> "Spinner":
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()


class AnimatedCursor:
    """Stream writer that avoids cursor rewrites which can corrupt wrapped text."""
    BLOCKS = ["█", "▓", "▒", "░", " "]
    PULSE = ["▋", "▊", "▉", "█", "▉", "▊", "▋", "▌"]

    def __init__(self, color: str = C.CYAN):
        self.color = color
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def emit_token(cls, token: str, cursor_char: str = "▌", color: str = C.CYAN) -> None:
        """Write streamed tokens without terminal cursor-repositioning controls."""
        sys.stdout.write(token)
        sys.stdout.flush()

    @classmethod
    def finalize_stream(cls) -> None:
        """Finish a streamed response with a clean line break."""
        sys.stdout.write("\n")
        sys.stdout.flush()



def animated_prompt_input(prompt_prefix: str, color_cursor: str = C.CYAN) -> str:
    """Read a line with clean prompt display and cursor support."""
    try:
        return input(prompt_prefix).strip()
    except (EOFError, KeyboardInterrupt):
        raise


def animated_print(text: str, delay: float = 0.005, color_cursor: str = C.CYAN) -> None:
    """Write a complete provider response intact, including wrapped lines."""
    sys.stdout.write(text)
    if text and not text.endswith(("\n", "\r")):
        sys.stdout.write("\n")
    sys.stdout.flush()



BANNER = r"""
 ██╗  ██╗ █████╗  ██████╗██╗  ██╗███████╗██████╗  █████╗  ██████╗ ███████╗███╗   ██╗████████╗
 ██║  ██║██╔══██╗██╔════╝██║ ██╔╝██╔════╝██╔══██╗██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝
 ███████║███████║██║     █████╔╝ █████╗  ██████╔╝███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║
 ██╔══██║██╔══██║██║     ██╔═██╗ ██╔══╝  ██╔══██╗██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║
 ██║  ██║██║  ██║╚██████╗██║  ██╗███████╗██║  ██║██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║
 ╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝
"""


def print_banner(subtitle: str, animate: bool = True) -> None:
    lines = [l for l in BANNER.strip("\n").split("\n") if l]
    colors = [C.CYAN, C.BLUE, C.MAGENTA, C.MAGENTA, C.CYAN, C.GREEN]
    if animate and sys.stdout.isatty():
        for i, line in enumerate(lines):
            c = colors[i % len(colors)]
            print(f"{c}{line}{C.RESET}")
            time.sleep(0.04)

        print(f"\n  {C.CYAN}┌─────────────────────────────────────────────────────────────┐{C.RESET}")
        print(f"  {C.CYAN}│{C.RESET}  {C.BOLD}{C.GREEN}◈ HACKERAGENT v2.5{C.RESET} {C.GREY}— Autonomous Terminal Agent{C.RESET}         {C.CYAN}│{C.RESET}")
        print(f"  {C.CYAN}│{C.RESET}  {C.CYAN}⚡ Engine:{C.RESET} {C.BOLD}{subtitle:<49}{C.RESET}{C.CYAN}│{C.RESET}")
        print(f"  {C.CYAN}│{C.RESET}  {C.YELLOW}⚙ Tools:{C.RESET} {C.GREY}16 active (project, audit, host, Kali){C.RESET}  {C.CYAN}│{C.RESET}")
        print(f"  {C.CYAN}│{C.RESET}  {C.GREY}✦ Type /help for commands, /exit to quit{C.RESET}                   {C.CYAN}│{C.RESET}")
        print(f"  {C.CYAN}└─────────────────────────────────────────────────────────────┘{C.RESET}\n")
    else:
        print(C.CYAN + BANNER + C.RESET)
        print(f"  {C.BOLD}{C.GREEN}HACKERAGENT{C.RESET} {C.GREY}— terminal agent — {subtitle}{C.RESET}")
        print(f"  {C.GREY}type /help for commands, /exit to quit{C.RESET}\n")


# ---------------------------------------------------------------- config -----
DEFAULT_BASE = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "mistral": "https://api.mistral.ai/v1",
    "together": "https://api.together.xyz/v1",
    "xai": "https://api.x.ai/v1",
    "perplexity": "https://api.perplexity.ai",
    "ollama": "http://localhost:11434/v1",
}
KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "together": "TOGETHER_API_KEY",
    "xai": "XAI_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "ollama": "OLLAMA_API_KEY",
}
DEFAULT_MODEL = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-sonnet-latest",
    "gemini": "gemini-flash-latest",
    "groq": "llama-3.3-70b-versatile",
    "openrouter": "anthropic/claude-3.5-sonnet",
    "deepseek": "deepseek-chat",
    "mistral": "mistral-large-latest",
    "together": "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
    "xai": "grok-2-latest",
    "perplexity": "sonar-pro",
    "ollama": "llama3.1",
}

def load_env_file() -> None:
    """Load key=value pairs from a local .env file into os.environ if not already set."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("\"'")
                    if k and k not in os.environ:
                        os.environ[k] = v
    except Exception:
        pass


def remove_env_key(key: str) -> bool:
    """Remove a key from both os.environ and the local .env file. Return True if found."""
    if key in os.environ:
        del os.environ[key]
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return False
    lines = []
    removed = False
    try:
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                raw = line.strip()
                if raw and not raw.startswith("#") and "=" in raw:
                    k, _ = raw.split("=", 1)
                    if k.strip() == key:
                        removed = True
                        continue
                lines.append(line if line.endswith("\n") else line + "\n")
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
    except Exception:
        pass
    return removed


def list_saved_env_keys() -> list[str]:
    """Return all non-empty key names saved in the local .env file."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    keys = []
    if os.path.exists(env_path):
        try:
            with open(env_path, encoding="utf-8") as fh:
                for line in fh:
                    raw = line.strip()
                    if raw and not raw.startswith("#") and "=" in raw:
                        k, _ = raw.split("=", 1)
                        if k.strip():
                            keys.append(k.strip())
        except Exception:
            pass
    return keys


def save_env_key(key: str, value: str) -> None:
    """Persist a key=value pair to the local .env file."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    lines = []
    found = False
    if os.path.exists(env_path):
        try:
            with open(env_path, encoding="utf-8") as fh:
                for line in fh:
                    raw = line.strip()
                    if raw and not raw.startswith("#") and "=" in raw:
                        k, _ = raw.split("=", 1)
                        if k.strip() == key:
                            lines.append(f"{key}={value}\n")
                            found = True
                            continue
                    lines.append(line if line.endswith("\n") else line + "\n")
        except Exception:
            pass
    if not found:
        lines.append(f"{key}={value}\n")
    try:
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
    except Exception:
        pass


SYSTEM_PROMPT = """You are HACKERAGENT, an autonomous terminal assistant for \
authorised security work and software engineering. Local file access is limited to the approved workspace unless the user opts out of that restriction.

You have tools:
- shell: execute CLI commands, scripts, compilers, package managers, network tools
- kali_shell: run a confirmed non-interactive command in installed Kali WSL on Windows
- setup_kali: offer WSL/Kali installation; always require separate user confirmation
- install_kali_tools: install packages from a fixed allowlist only with --auto-install-tools
- local_security_audit: read-only, redacted secret and risky-code pattern scan inside the workspace
- web_security_check: one non-following HTTP HEAD check of TLS and security headers
- tcp_port_scan: low-rate, single-host TCP connect checks after explicit confirmation
- read_file / write_file: inspect and create/edit source code and config files
- list_dir: explore folders and directory trees
- fetch_url: retrieve documentation, web APIs, and remote resources
- system_info: monitor OS, hardware specs, CPU cores, memory/disk utilization
- search_files: locate files by pattern or grep inside files across project trees
- process_list: inspect running system processes, PIDs, and memory usage
- env_vars: inspect system and environment configurations
- git_info: check git version control status, branches, diffs, and history

Work step by step: think, call tools to inspect actual state, then act.
Rules:
- Only assess systems the user owns or is explicitly authorized to test; keep checks non-destructive.
- Prefer doing over describing. When asked to build a tool, write the file and execute it.
- Keep shell commands focused and explain what you're about to run.
- After acting, verify your result with another tool call before concluding.
- Be concise in prose; let the tool output speak.
"""


# ------------------------------------------------------------- tool defs -----
TOOLS = [
    {"type": "function", "function": {
        "name": "shell",
        "description": "Run a shell command on the local machine and return stdout+stderr.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "command line to execute"},
            "timeout": {"type": "integer", "description": "seconds before kill (default 60)"}},
            "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a text file and return its contents.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create or overwrite a file with the given text content.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List files in a directory.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "fetch_url",
        "description": "HTTP GET a URL and return the status and a text preview.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "system_info",
        "description": "Inspect host system specs, OS platform, CPU cores, RAM/disk space, and environment.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "search_files",
        "description": "Search for files by glob pattern or grep inside files across a directory tree.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "root folder to search (default .)"},
            "pattern": {"type": "string", "description": "filename glob pattern, e.g. '*.py' or '*test*'"},
            "content_query": {"type": "string", "description": "text to grep inside matching files"},
            "max_results": {"type": "integer", "description": "max matches (default 50)"}}}}},
    {"type": "function", "function": {
        "name": "process_list",
        "description": "List currently running system processes, PIDs, and memory usage.",
        "parameters": {"type": "object", "properties": {
            "filter": {"type": "string", "description": "optional process name filter (e.g. 'python' or 'chrome')"},
            "max_count": {"type": "integer", "description": "max processes to return (default 30)"}}}}},
    {"type": "function", "function": {
        "name": "env_vars",
        "description": "Inspect system environment variables or get a specific variable.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "variable name to lookup, or omit to list common system variables"}}}}},
    {"type": "function", "function": {
        "name": "git_info",
        "description": "Query git repository status, diffs, branches, or recent commit history.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["status", "diff", "log", "branch"], "description": "git action (default 'status')"},
            "path": {"type": "string", "description": "repository folder path (default '.')"},
            "count": {"type": "integer", "description": "number of commits for log (default 5)"}}}}},
    {"type": "function", "function": {
        "name": "local_security_audit",
        "description": "Read-only audit of one local workspace for common hardcoded-secret patterns and risky code constructs. Output is redacted and contains no secret values.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "workspace folder to audit (default: --workspace)"},
            "max_files": {"type": "integer", "description": "maximum text files to inspect (default 2000; max 5000)"}}}}},
    {"type": "function", "function": {
        "name": "web_security_check",
        "description": "Make one non-following HTTP HEAD request to check HTTPS certificate validation and common security headers. Only use on a site the user owns or is authorized to assess; no exploitation or crawling.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "http:// or https:// URL to check"}},
            "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "tcp_port_scan",
        "description": "Low-rate TCP connect check of up to 64 specified ports on one authorized host (no CIDR/subnet scanning). An explicit interactive confirmation is required even when --yes is used.",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string", "description": "single hostname or IP address; no CIDR"},
            "ports": {"type": "array", "items": {"type": "integer"},
                      "description": "up to 64 TCP ports; defaults to a small common-port set"},
            "timeout_ms": {"type": "integer", "description": "per-port timeout from 100 to 1000 ms (default 500)"}},
            "required": ["target"]}}},
    {"type": "function", "function": {
        "name": "setup_kali",
        "description": "On Windows, offer to install Kali Linux through WSL and open it in Windows Terminal. The user must confirm this system change; this is not bypassed by --yes.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "install_kali_tools",
        "description": "Install allowlisted Kali packages only when --auto-install-tools was explicitly enabled. Use only for the user's authorized lab or systems.",
        "parameters": {"type": "object", "properties": {
            "packages": {"type": "array", "items": {"type": "string"},
                          "description": "Kali package names from the documented allowlist"}},
            "required": ["packages"]}}},
    {"type": "function", "function": {
        "name": "kali_shell",
        "description": "Run a non-interactive command inside an installed Kali WSL distro and return output. Shell confirmation applies; --yes bypasses it. Use only on systems the user owns or is authorized to test.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "command to run inside Kali's bash"},
            "timeout": {"type": "integer", "description": "timeout in seconds (default 60; max 900)"}},
            "required": ["command"]}}},
]

MAX_OUTPUT = 6000  # cap tool output fed back to the model


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + f"\n... [truncated {len(text) - MAX_OUTPUT} chars]"


def tool_shell(args: dict) -> str:
    cmd = args.get("command")
    if not cmd:
        return "[error] no command provided"
    try:
        timeout = int(args.get("timeout") or 60)
    except (TypeError, ValueError):
        timeout = 60
    try:
        proc = subprocess.run(cmd, shell=True, cwd=args.get("_workspace"), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout)
        out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        return _truncate(f"[exit {proc.returncode}]\n{out}".strip())
    except subprocess.TimeoutExpired:
        return f"[timeout after {timeout}s]"
    except Exception as exc:  # noqa: BLE001
        return f"[error] {exc}"


def tool_read_file(args: dict) -> str:
    path = args.get("path") or args.get("file") or args.get("filename")
    if not path:
        return "[error] no path provided"
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return _truncate(fh.read())
    except Exception as exc:  # noqa: BLE001
        return f"[error] {exc}"


def tool_write_file(args: dict) -> str:
    path = args.get("path") or args.get("file") or args.get("filename")
    content = args.get("content")
    if not path:
        return "[error] no path provided"
    if content is None:
        return "[error] no content provided"
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(content)
        return f"[ok] wrote {len(content)} chars to {path}"
    except Exception as exc:  # noqa: BLE001
        return f"[error] {exc}"


def tool_list_dir(args: dict) -> str:
    path = args.get("path", ".") or "."
    try:
        entries = sorted(os.listdir(path))
        return _truncate("\n".join(entries) if entries else "[empty]")
    except Exception as exc:  # noqa: BLE001
        return f"[error] {exc}"


def tool_fetch_url(args: dict) -> str:
    url = args.get("url")
    if not url:
        return "[error] no url provided"
    try:
        resp = requests.get(url, timeout=30)
        return _truncate(f"[HTTP {resp.status_code}]\n{resp.text[:2000]}")
    except Exception as exc:  # noqa: BLE001
        return f"[error] {exc}"


def tool_system_info(args: dict) -> str:
    try:
        uname = platform.uname()
        cwd = os.getcwd()
        try:
            total, used, free = shutil.disk_usage(cwd)
            disk_str = f"Total: {total // (2**30)}GB, Used: {used // (2**30)}GB, Free: {free // (2**30)}GB"
        except Exception:
            disk_str = "N/A"
        cpu_count = os.cpu_count() or "unknown"
        user = os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"
        info = [
            f"OS: {platform.system()} {platform.release()} ({platform.version()})",
            f"Architecture: {platform.machine()} ({uname.processor or 'N/A'})",
            f"CPU Cores: {cpu_count}",
            f"Disk Usage ({cwd[:3] if os.name == 'nt' else '/'}): {disk_str}",
            f"User: {user}",
            f"Hostname: {uname.node}",
            f"Current Directory: {cwd}",
            f"Python: {platform.python_version()} ({sys.executable})",
        ]
        return "\n".join(info)
    except Exception as exc:  # noqa: BLE001
        return f"[error] {exc}"


def tool_search_files(args: dict) -> str:
    root = args.get("path") or "."
    pattern = args.get("pattern") or "*"
    content_query = args.get("content_query") or args.get("query")
    try:
        max_results = int(args.get("max_results") or 50)
    except (TypeError, ValueError):
        max_results = 50
    matches = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("node_modules", "__pycache__", "venv", ".git")]
            for f in filenames:
                if fnmatch.fnmatch(f, pattern):
                    rel = os.path.relpath(os.path.join(dirpath, f), root)
                    if content_query:
                        filepath = os.path.join(dirpath, f)
                        try:
                            with open(filepath, "r", encoding="utf-8", errors="ignore") as fh:
                                for line_no, line in enumerate(fh, 1):
                                    if content_query.lower() in line.lower():
                                        snippet = line.strip()[:100]
                                        matches.append(f"{rel}:{line_no}: {snippet}")
                                        if len(matches) >= max_results:
                                            break
                        except Exception:
                            continue
                    else:
                        matches.append(rel)
                    if len(matches) >= max_results:
                        break
            if len(matches) >= max_results:
                break
        if not matches:
            return "[no matches found]"
        suffix = f"\n... [capped at {max_results} results]" if len(matches) >= max_results else ""
        return _truncate("\n".join(matches) + suffix)
    except Exception as exc:  # noqa: BLE001
        return f"[error] {exc}"


def tool_process_list(args: dict) -> str:
    filter_name = (args.get("filter") or args.get("name") or "").lower()
    try:
        max_count = int(args.get("max_count") or 30)
    except (TypeError, ValueError):
        max_count = 30
    try:
        if os.name == "nt":
            cmd = "tasklist /FO CSV /NH"
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=15)
            lines = []
            for row in proc.stdout.strip().splitlines():
                parts = [p.strip('"') for p in row.split('","')]
                if len(parts) >= 5:
                    pname, pid, sname, snum, mem = parts[0], parts[1], parts[2], parts[3], parts[4]
                    if not filter_name or filter_name in pname.lower():
                        lines.append(f"PID: {pid:<8} Image: {pname:<28} Memory: {mem}")
            if not lines:
                return "[no processes matched filter]"
            return _truncate(f"Total matching: {len(lines)}\n" + "\n".join(lines[:max_count]))
        else:
            cmd = "ps aux --sort=-%mem"
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=15)
            lines = proc.stdout.strip().splitlines()
            if filter_name:
                header = lines[0] if lines else ""
                lines = [header] + [l for l in lines[1:] if filter_name in l.lower()]
            return _truncate("\n".join(lines[:max_count]))
    except Exception as exc:  # noqa: BLE001
        return f"[error] {exc}"


def tool_env_vars(args: dict) -> str:
    name = args.get("name")
    if name:
        val = os.environ.get(name)
        return f"{name}={val}" if val is not None else f"[environment variable '{name}' not found]"
    safe_keys = ["PATH", "USER", "USERNAME", "OS", "SHELL", "COMSPEC", "HOME",
                 "USERPROFILE", "TEMP", "TMP", "PYTHONPATH", "LANG", "TERM"]
    lines = [f"{k}={os.environ.get(k)}" for k in safe_keys if k in os.environ]
    return _truncate("\n".join(lines))


def tool_git_info(args: dict) -> str:
    action = (args.get("action") or "status").lower()
    path = args.get("path") or "."
    try:
        if action == "diff":
            cmd = "git diff --stat"
        elif action == "log":
            count = int(args.get("count") or 5)
            cmd = f"git log -n {count} --oneline"
        elif action == "branch":
            cmd = "git branch -a"
        else:
            cmd = "git status -s"
        proc = subprocess.run(cmd, shell=True, cwd=path, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=15)
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0 and err:
            return f"[git error {proc.returncode}]: {err}"
        return _truncate(out if out else "[clean/empty]")
    except Exception as exc:  # noqa: BLE001
        return f"[error] {exc}"


SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b[A-Z0-9_]*(?:API[_-]?KEY|SECRET|PASSWORD|PASSWD|TOKEN)\b"
    r"\s*[:=]\s*[\"']?([A-Za-z0-9_./+=:-]{8,})"
)
PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
AWS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
GITHUB_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
RISKY_CODE_PATTERNS = (
    ("dynamic eval/exec", re.compile(r"\b(?:eval|exec)\s*\("), "medium"),
    ("shell=True subprocess", re.compile(r"\bshell\s*=\s*True\b", re.IGNORECASE), "medium"),
    ("pickle deserialization", re.compile(r"\bpickle\.loads?\s*\("), "high"),
    ("TLS verification disabled", re.compile(r"\bverify\s*=\s*False\b", re.IGNORECASE), "high"),
    ("review yaml.load use", re.compile(r"\byaml\.load\s*\("), "medium"),
)
AUDIT_SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".next", "dist", "build", "coverage", "target",
})


def _looks_like_placeholder(value: str) -> bool:
    normalized = value.strip().strip(".,;:)}]").lower().replace("_", "-")
    return normalized in {
        "changeme", "change-me", "placeholder", "example", "your-api-key",
        "your-secret", "your-token", "dummy", "not-a-real-key", "replace-me",
        "password", "secret", "null", "none", "xxxxxxx",
    }


def tool_local_security_audit(args: dict) -> str:
    """Read-only local audit; reports locations and finding types, never values."""
    root = os.path.realpath(args.get("path") or ".")
    if not os.path.isdir(root):
        return f"[error] audit path is not a directory: {root}"
    try:
        max_files = int(args.get("max_files") or 2000)
    except (TypeError, ValueError):
        return "[error] max_files must be an integer from 1 to 5000"
    if not 1 <= max_files <= 5000:
        return "[error] max_files must be from 1 to 5000"

    findings: list[str] = []
    scanned = 0
    files_seen = 0
    findings_capped = False
    file_cap_hit = False
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dirnames[:] = [name for name in dirnames
                       if name not in AUDIT_SKIP_DIRS and not os.path.islink(os.path.join(dirpath, name))]
        for filename in filenames:
            files_seen += 1
            if files_seen > max_files:
                file_cap_hit = True
                break
            file_path = os.path.join(dirpath, filename)
            if os.path.islink(file_path):
                continue
            try:
                if os.path.getsize(file_path) > 1_000_000:
                    continue
                with open(file_path, "rb") as file:
                    raw = file.read(1_000_001)
            except OSError:
                continue
            if b"\x00" in raw:
                continue
            scanned += 1
            relative_path = _safe_terminal_text(os.path.relpath(file_path, root))
            for line_number, line in enumerate(raw.decode("utf-8", errors="ignore").splitlines(), 1):
                matches = []
                if PRIVATE_KEY_RE.search(line):
                    matches.append(("private-key material", "critical"))
                if AWS_KEY_RE.search(line):
                    matches.append(("cloud access-key pattern", "high"))
                if GITHUB_TOKEN_RE.search(line):
                    matches.append(("source-control token pattern", "high"))
                secret_match = SECRET_ASSIGNMENT_RE.search(line)
                if secret_match and not _looks_like_placeholder(secret_match.group(1)):
                    matches.append(("possible hardcoded credential", "high"))
                for finding, _pattern, severity in RISKY_CODE_PATTERNS:
                    if _pattern.search(line):
                        matches.append((finding, severity))
                for finding, severity in matches:
                    findings.append(f"[{severity}] {relative_path}:{line_number}: {finding}")
                    if len(findings) >= 100:
                        findings_capped = True
                        break
                if findings_capped:
                    break
            if findings_capped:
                break
        if file_cap_hit or findings_capped:
            break

    if not findings:
        result = f"[ok] scanned {scanned} text files; no matching patterns found"
    else:
        result = f"[audit] scanned {scanned} text files; {len(findings)} finding(s):\n" + "\n".join(findings)
    if file_cap_hit:
        result += "\n[scan capped at max_files; narrow the workspace or raise max_files up to 5000]"
    if findings_capped:
        result += "\n[output capped at 100 findings]"
    return _truncate(result)


def tool_web_security_check(args: dict) -> str:
    """Check one HTTP response's headers and HTTPS certificate validation; no crawling."""
    url = args.get("url")
    if not isinstance(url, str) or len(url) > 2048:
        return "[error] url must be a string no longer than 2048 characters"
    url = url.strip()
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        return f"[error] invalid URL: {exc}"
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return "[error] provide a complete http:// or https:// URL"
    if parsed.username or parsed.password:
        return "[error] URLs containing embedded credentials are not allowed"
    try:
        response = requests.head(
            url, timeout=10, allow_redirects=False,
            headers={"User-Agent": "HACKERAGENT-authorized-security-check/1.0"},
        )
    except requests.exceptions.SSLError as exc:
        return f"[TLS error] certificate validation failed: {exc}"
    except requests.RequestException as exc:
        return f"[error] HTTP HEAD request failed: {exc}"

    try:
        headers = response.headers
        safe_url = _safe_terminal_text(parsed._replace(query="", fragment="").geturl())
        lines = [f"URL: {safe_url}", f"HTTP status: {response.status_code}"]
        if parsed.scheme.lower() == "https":
            lines.append("TLS certificate validation: passed (standard CA verification enabled)")
        else:
            lines.append("[warning] URL is HTTP; transport is not encrypted")
        if 300 <= response.status_code < 400:
            lines.append("[info] redirect not followed")

        csp = headers.get("Content-Security-Policy", "")
        checks = [
            ("Content-Security-Policy", bool(csp)),
            ("X-Content-Type-Options", bool(headers.get("X-Content-Type-Options"))),
            ("Referrer-Policy", bool(headers.get("Referrer-Policy"))),
            ("Permissions-Policy", bool(headers.get("Permissions-Policy"))),
        ]
        has_frame_control = bool(headers.get("X-Frame-Options")) or "frame-ancestors" in csp.lower()
        checks.insert(1, ("X-Frame-Options / CSP frame-ancestors", has_frame_control))
        if parsed.scheme.lower() == "https":
            checks.append(("Strict-Transport-Security", bool(headers.get("Strict-Transport-Security"))))
        for header, present in checks:
            lines.append(f"[{ 'ok' if present else 'review' }] {header}: {'present' if present else 'missing'}")
        if response.status_code in (405, 501):
            lines.append("[info] server does not support HEAD; body was not fetched")
        if headers.get("Server") or headers.get("X-Powered-By"):
            lines.append("[info] server-identification header is present")
        return _truncate("\n".join(lines))
    finally:
        response.close()


def tool_tcp_port_scan(args: dict) -> str:
    """Perform a small sequential TCP connect check against one explicitly confirmed host."""
    target = args.get("target")
    if not isinstance(target, str) or not target.strip() or len(target) > 253:
        return "[error] target must be one hostname or IP address"
    target = target.strip()
    if any(char.isspace() for char in target) or "/" in target or target.startswith("-"):
        return "[error] target must be one hostname or IP address; CIDR and lists are not supported"
    try:
        ipaddress.ip_address(target)
    except ValueError:
        hostname_pattern = r"(?i)(?=.{1,253}\.?$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\.?"
        if not re.fullmatch(hostname_pattern, target):
            return "[error] target must be a single valid hostname or IP address"

    default_ports = [22, 53, 80, 443, 445, 3389, 5432, 8080, 8443]
    raw_ports = args.get("ports") or default_ports
    if not isinstance(raw_ports, list) or not raw_ports:
        return "[error] ports must be a non-empty array of integer TCP ports"
    if len(raw_ports) > 64:
        return "[error] at most 64 ports can be checked per scan"
    if any(isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
           for port in raw_ports):
        return "[error] every port must be an integer from 1 to 65535"
    ports = sorted(set(raw_ports))
    try:
        timeout_ms = int(args.get("timeout_ms", 500))
    except (TypeError, ValueError):
        return "[error] timeout_ms must be an integer from 100 to 1000"
    if not 100 <= timeout_ms <= 1000:
        return "[error] timeout_ms must be from 100 to 1000"

    try:
        resolved = socket.getaddrinfo(target, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return f"[error] could not resolve target: {exc}"
    if not resolved:
        return "[error] target did not resolve to an address"
    # Use only the first resolved address so a hostname cannot fan out to a
    # list of hosts; the confirmation also identifies the single input target.
    family, socktype, proto, _canonname, sockaddr = resolved[0]
    address = sockaddr[0]
    opened = []
    timeout = timeout_ms / 1000.0
    for port in ports:
        destination = ((address, port, sockaddr[2], sockaddr[3]) if family == socket.AF_INET6
                       else (address, port))
        try:
            with socket.socket(family, socktype, proto) as connection:
                connection.settimeout(timeout)
                connection.connect(destination)
            opened.append(port)
        except OSError:
            continue
    if opened:
        status = ", ".join(str(port) for port in opened)
        return f"[scan complete] {target} resolved to {address}; open TCP ports: {status}"
    return f"[scan complete] {target} resolved to {address}; no open TCP ports among {len(ports)} checked"


KALI_PACKAGE_ALLOWLIST = frozenset({
    "curl", "wget", "git", "jq", "python3", "python3-pip", "python3-venv",
    "nmap", "whatweb", "nikto", "gobuster", "sqlmap",
})


def _is_windows() -> bool:
    return os.name == "nt"


def _wsl_executable() -> str | None:
    return shutil.which("wsl.exe") or shutil.which("wsl")


def _kali_installed(wsl: str) -> tuple[bool, str]:
    try:
        result = subprocess.run([wsl, "--list", "--quiet"], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (result.stdout or "").replace(chr(0), "").lower()
    if result.returncode:
        return False, (result.stderr or result.stdout or "could not query WSL distributions").strip()
    return "kali-linux" in output, ""


def tool_setup_kali(_args: dict) -> str:
    """Install Kali in WSL on Windows and launch it in Windows Terminal."""
    if not _is_windows():
        return "[error] Kali/WSL setup is available only when this app runs on Windows"
    wsl = _wsl_executable()
    if not wsl:
        return "[error] wsl.exe was not found; enable/install WSL on Windows first"

    # A failed distro-list query can mean that the WSL feature itself is not
    # initialized. Since this helper is confirmation-gated, try the supported
    # WSL installer and let it report whether elevation or a restart is needed.
    installed, _check_error = _kali_installed(wsl)
    if not installed:
        try:
            result = subprocess.run(
                [wsl, "--install", "--distribution", "kali-linux"], timeout=900,
            )
        except subprocess.TimeoutExpired:
            return "[error] Kali installation timed out; check WSL status and rerun --setup-kali"
        except OSError as exc:
            return f"[error] could not start WSL installation: {exc}"
        if result.returncode:
            return (f"[error] WSL could not install Kali (exit {result.returncode}). "
                    "Try `wsl --install --distribution kali-linux` from an elevated Windows Terminal.")
        installed, _ = _kali_installed(wsl)
        if not installed:
            return ("[ok] WSL accepted the Kali installation request. A Windows restart or first-time "
                    "WSL setup may be required; rerun `python agent.py --setup-kali` afterward.")

    terminal = shutil.which("wt.exe") or shutil.which("wt")
    try:
        if terminal:
            subprocess.Popen([terminal, "new-tab", "--title", "Kali Linux", wsl, "-d", "kali-linux"])
            return "[ok] Kali Linux is installed and was opened in a new Windows Terminal tab"
        subprocess.Popen([wsl, "-d", "kali-linux"])
        return "[ok] Kali Linux is installed and was opened in this terminal (Windows Terminal was not found)"
    except OSError as exc:
        return f"[error] Kali is installed, but could not be launched: {exc}"


def tool_install_kali_tools(cfg, args: dict) -> str:
    """Install a strictly allowlisted set of packages in an installed Kali WSL distro."""
    if not getattr(cfg, "auto_install_tools", False):
        return "[denied] relaunch with --auto-install-tools to enable allowlisted Kali package installs"
    if not _is_windows():
        return "[error] Kali package installation is available only when this app runs on Windows"
    raw_packages = args.get("packages")
    if not isinstance(raw_packages, list) or not raw_packages:
        return "[error] packages must be a non-empty list of package names"
    if any(not isinstance(package, str) for package in raw_packages):
        return "[error] every package name must be a string"
    packages = list(dict.fromkeys(package.strip().lower() for package in raw_packages))
    invalid = [package for package in packages if package not in KALI_PACKAGE_ALLOWLIST]
    if invalid:
        return ("[denied] package(s) not on the Kali allowlist: " + ", ".join(invalid)
                + "; allowed: " + ", ".join(sorted(KALI_PACKAGE_ALLOWLIST)))

    wsl = _wsl_executable()
    if not wsl:
        return "[error] wsl.exe was not found; run --setup-kali first"
    installed, error = _kali_installed(wsl)
    if error:
        return f"[error] could not check WSL distributions: {error}"
    if not installed:
        return "[error] Kali is not installed; run `python agent.py --setup-kali` first"

    apt_command = "apt-get update && apt-get install -y -- " + " ".join(
        shlex.quote(package) for package in packages
    )
    try:
        result = subprocess.run(
            [wsl, "-d", "kali-linux", "-u", "root", "--", "bash", "-lc", apt_command],
            capture_output=True, stdin=subprocess.DEVNULL, text=True,
            encoding="utf-8", errors="replace", timeout=900,
        )
    except subprocess.TimeoutExpired:
        return "[error] Kali package installation timed out after 15 minutes"
    except OSError as exc:
        return f"[error] could not start Kali package installation: {exc}"
    output = chr(10).join(part for part in (result.stdout, result.stderr) if part).strip()
    output = _truncate(output)
    if result.returncode:
        return f"[error] apt-get failed (exit {result.returncode})" + (chr(10) + output if output else "")
    return f"[ok] installed {', '.join(packages)} in Kali" + (chr(10) + output if output else "")


def tool_kali_shell(args: dict) -> str:
    """Run a bounded, non-interactive command in Kali WSL and return its output."""
    if not _is_windows():
        return "[error] Kali commands are available only when this app runs on Windows"
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return "[error] no Kali command provided"
    if len(command) > 4000 or "\x00" in command:
        return "[error] Kali command is too long or contains a NUL character"
    try:
        timeout = int(args.get("timeout") or 60)
    except (TypeError, ValueError):
        return "[error] timeout must be an integer from 1 to 900 seconds"
    if not 1 <= timeout <= 900:
        return "[error] timeout must be from 1 to 900 seconds"

    wsl = _wsl_executable()
    if not wsl:
        return "[error] wsl.exe was not found; run --setup-kali first"
    installed, error = _kali_installed(wsl)
    if error:
        return f"[error] could not check WSL distributions: {error}"
    if not installed:
        return "[error] Kali is not installed; run `python agent.py --setup-kali` first"
    try:
        result = subprocess.run(
            [wsl, "-d", "kali-linux", "--", "bash", "-lc", command],
            capture_output=True, stdin=subprocess.DEVNULL, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"[timeout after {timeout}s]"
    except OSError as exc:
        return f"[error] could not start Kali command: {exc}"
    output = (result.stdout or "")
    if result.stderr:
        output += "\n[stderr]\n" + result.stderr
    return _truncate(f"[exit {result.returncode}]\n{output}".strip())


DISPATCH = {
    "shell": tool_shell,
    "kali_shell": tool_kali_shell,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "list_dir": tool_list_dir,
    "fetch_url": tool_fetch_url,
    "system_info": tool_system_info,
    "search_files": tool_search_files,
    "process_list": tool_process_list,
    "env_vars": tool_env_vars,
    "git_info": tool_git_info,
    "local_security_audit": tool_local_security_audit,
    "web_security_check": tool_web_security_check,
    "tcp_port_scan": tool_tcp_port_scan,
}

DESTRUCTIVE_HINT = ("rm ", "del ", "format", "mkfs", "dd ", "shutdown",
                    "taskkill", ":(){", "> /dev/sda")


def confirm(command: str, auto: bool) -> bool:
    if auto:
        return True
    risky = any(h in command.lower() for h in DESTRUCTIVE_HINT)
    tag = f"{C.RED}risky{C.RESET}" if risky else f"{C.YELLOW}shell{C.RESET}"
    print(f"\n  {tag} {C.BOLD}{command}{C.RESET}")
    try:
        ans = input(f"  {C.GREY}run? [y/N]{C.RESET} ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def workspace_path(workspace: str, value: str) -> str:
    """Resolve a tool path and reject paths escaping the approved workspace."""
    root = os.path.realpath(workspace)
    candidate = os.path.realpath(value if os.path.isabs(value) else os.path.join(root, value))
    if os.path.commonpath((root, candidate)) != root:
        raise ValueError(f"path is outside workspace: {value}")
    return candidate


def guard_tool_args(cfg, name: str, args: dict) -> dict:
    """Canonicalize filesystem paths before a tool receives them."""
    safe = dict(args)
    if name == "shell":
        safe["_workspace"] = cfg.workspace
        return safe
    if name == "local_security_audit" and not safe.get("path"):
        safe["path"] = cfg.workspace
    if cfg.allow_outside_workspace:
        return safe
    path_keys = ("path", "file", "filename")
    if name in ("read_file", "write_file", "list_dir", "search_files", "git_info", "local_security_audit"):
        for key in path_keys:
            if key in safe and safe[key]:
                safe[key] = workspace_path(cfg.workspace, str(safe[key]))
        if name in ("list_dir", "search_files", "git_info") and not safe.get("path"):
            safe["path"] = cfg.workspace
    return safe


def gemini_api_key(key: str) -> str:
    """Validate and normalize a Gemini API key before it is sent.

    The Generative Language API key is not an OAuth bearer token, service
    account JSON, or OpenAI key. Catching that locally turns Google's opaque
    ACCESS_TOKEN_TYPE_UNSUPPORTED 401 into an actionable error.
    """
    value = (key or "").strip()
    lowered = value.lower()
    if not value:
        raise RuntimeError("Gemini API key is empty; set GEMINI_API_KEY to an AI Studio API key")
    if lowered.startswith("bearer "):
        raise RuntimeError("Gemini needs the raw API key, not 'Bearer ...'; remove the Bearer prefix")
    if value.startswith(("ya29.", "sk-", "sk_", "{")) or "-----BEGIN" in value:
        raise RuntimeError(
            "Gemini provider expects an AI Studio API key, usually starting with AIza. "
            "The configured value looks like an OAuth token, service-account credential, or another provider key. "
            "Create an API key in Google AI Studio and set GEMINI_API_KEY to that value."
        )
    return value


def gemini_headers(key: str) -> dict:
    return {"Content-Type": "application/json", "x-goog-api-key": gemini_api_key(key)}


def _post(url: str, headers: dict, payload: dict, *, stream: bool,
          timeout: float, retries: int) -> requests.Response:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite number greater than zero")
    if retries < 0:
        raise ValueError("retries cannot be negative")
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload,
                                 stream=stream, timeout=timeout)
        except requests.RequestException as exc:
            last, resp = exc, None
        if resp is not None:
            if resp.status_code < 400:
                return resp
            if resp.status_code in (408, 409, 429) or resp.status_code >= 500:
                last = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            else:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        if attempt < retries:
            backoff = min(2 ** attempt, 20) + random.uniform(0, 0.5)
            # A spinner may be redrawing stdout in a TTY; don't let retry
            # diagnostics overwrite it. Streaming calls have no spinner.
            if not sys.stdout.isatty() or stream:
                print(f"{C.GREY}[retry {attempt+1}/{retries}] {last}; {backoff:.1f}s{C.RESET}",
                      file=sys.stderr)
            time.sleep(backoff)
    raise last if last else RuntimeError("request failed")


def _iter_sse(resp):
    for raw in resp.iter_lines(decode_unicode=True):
        # Requests may return bytes when the streaming endpoint omits a charset.
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if not isinstance(raw, str):
            continue
        raw = raw.lstrip(chr(0xfeff))
        if raw.startswith("data:"):
            data = raw[5:].strip()
            if data == "[DONE]":
                return
            yield data


def openai_turn(cfg, messages: list[dict], system: str | None = None) -> tuple[str, list[dict]]:
    """One model call. Returns (text, tool_calls)."""
    url = cfg.base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {cfg.key}", "Content-Type": "application/json"}
    msgs = ([{"role": "system", "content": system}] if system else []) + messages
    payload = {"model": cfg.model, "messages": msgs, "tools": TOOLS,
               "tool_choice": "auto", "temperature": cfg.temperature,
               "stream": cfg.stream}

    if not cfg.stream:
        data = _post(url, headers, payload, stream=False,
                     timeout=cfg.timeout, retries=cfg.retries).json()
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        return msg.get("content") or "", msg.get("tool_calls") or []

    resp = _post(url, headers, payload, stream=True,
                 timeout=cfg.timeout, retries=cfg.retries)
    content: list[str] = []
    calls: dict[int, dict] = {}
    for chunk in _iter_sse(resp):
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if not obj.get("choices"):
            continue
        delta = obj["choices"][0].get("delta", {})
        if delta.get("content"):
            content.append(delta["content"])
            AnimatedCursor.emit_token(delta["content"])
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            entry = calls.setdefault(idx, {"id": "", "type": "function",
                                           "function": {"name": "", "arguments": ""}})
            if tc.get("id"):
                entry["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                entry["function"]["name"] += fn["name"]
            if fn.get("arguments"):
                entry["function"]["arguments"] += fn["arguments"]
    if content:
        AnimatedCursor.finalize_stream()
    return "".join(content), [calls[i] for i in sorted(calls)]


def anthropic_turn(cfg, messages, system) -> tuple[str, list[dict]]:
    """Anthropic Messages API. Non-streaming; returns text + normalised tool_calls."""
    url = cfg.base_url.rstrip("/") + "/messages"
    headers = {"x-api-key": cfg.key, "anthropic-version": "2023-06-01",
               "Content-Type": "application/json"}
    atools = [{"name": t["function"]["name"],
               "description": t["function"]["description"],
               "input_schema": t["function"]["parameters"]} for t in TOOLS]
    payload = {"model": cfg.model, "max_tokens": 4096, "system": system,
               "messages": messages, "tools": atools, "temperature": cfg.temperature}
    data = _post(url, headers, payload, stream=False,
                 timeout=cfg.timeout, retries=cfg.retries).json()
    text_parts, tool_calls = [], []
    for block in data.get("content", []):
        if block.get("type") == "text":
            text_parts.append(block["text"])
        elif block.get("type") == "tool_use":
            tool_calls.append({"id": block["id"], "type": "function",
                               "function": {"name": block["name"],
                                            "arguments": json.dumps(block["input"])}})
    text = "".join(text_parts)
    return text, tool_calls


def gemini_turn(cfg, messages, system) -> tuple[str, list[dict]]:
    """Google Gemini generateContent. Non-streaming; returns text + tool_calls."""
    url = f"{cfg.base_url.rstrip('/')}/models/{cfg.model}:generateContent"
    fdecls = [{"name": t["function"]["name"],
               "description": t["function"]["description"],
               "parameters": t["function"]["parameters"]} for t in TOOLS]

    # Translate our internal history into Gemini 'contents'.
    contents: list[dict] = []
    for m in messages:
        role = m.get("role")
        if "parts" in m:
            contents.append({"role": "model" if role == "model" else "user",
                             "parts": m["parts"]})
        elif role == "user":
            contents.append({"role": "user", "parts": [{"text": m.get("content") or ""}]})
        elif role in ("assistant", "model") and m.get("content"):
            contents.append({"role": "model", "parts": [{"text": m["content"]}]})

    payload = {"contents": contents,
               "tools": [{"functionDeclarations": fdecls}],
               "generationConfig": {"temperature": cfg.temperature}}
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}

    data = _post(url, gemini_headers(cfg.key), payload, stream=False,
                 timeout=cfg.timeout, retries=cfg.retries).json()
    parts = (((data.get("candidates") or [{}])[0].get("content") or {})
             .get("parts") or [])
    text_parts, tool_calls = [], []
    for part in parts:
        if "text" in part:
            text_parts.append(part["text"])
        elif "functionCall" in part:
            fc = part["functionCall"]
            tool_calls.append({"id": f"call_{fc.get('name', 'fn')}", "type": "function",
                               "function": {"name": fc.get("name", ""),
                                            "arguments": json.dumps(fc.get("args") or {})},
                               "_raw_part": part})
    text = "".join(text_parts)
    return text, tool_calls, parts


# --------------------------------------------------------------- agent -------
def setup_kali_with_confirmation() -> str:
    """Require an interactive yes before any WSL/Kali system change."""
    if not _is_windows():
        return tool_setup_kali({})
    prompt = "Install Kali Linux through WSL and open it in Windows Terminal? This changes Windows."
    if not confirm(prompt, auto=False):
        return "[denied] Kali setup cancelled"
    return tool_setup_kali({})


def _safe_terminal_text(value) -> str:
    """Replace control characters in untrusted text before echoing it to a terminal."""
    return "".join(char if char.isprintable() else "?" for char in str(value))


def execute_tool(cfg, name: str, args: dict) -> str:
    label = _safe_terminal_text(args.get("command") or args.get("path") or args.get("url") or
                                args.get("pattern") or args.get("filter") or args.get("name") or
                                args.get("action") or args.get("packages") or "")
    if len(label) > 55:
        label = label[:52] + "..."
    print(f"\n  {C.CYAN}┌── {C.BOLD}{C.MAGENTA}⚙ {name}{C.RESET} {C.GREY}{label}{C.RESET}")
    try:
        args = guard_tool_args(cfg, name, args)
    except ValueError as exc:
        result = f"[denied] {exc}"
        print(f"  {C.CYAN}└── {C.YELLOW}{result}{C.RESET}\n")
        return result

    action = str(args.get("command") or args.get("path") or name)
    if name == "tcp_port_scan":
        ports = args.get("ports") or "default common ports"
        action = f"TCP connect scan: host={args.get('target')} ports={ports}"
    action = _safe_terminal_text(action)
    if name == "setup_kali":
        result = setup_kali_with_confirmation()
    elif name == "install_kali_tools":
        with Spinner("Installing allowlisted Kali packages"):
            result = tool_install_kali_tools(cfg, args)
    elif name == "tcp_port_scan" and not confirm(action, auto=False):
        result = "[user denied network scan permission]"
    elif name in ("shell", "kali_shell", "write_file") and not confirm(action, cfg.yes):
        result = "[user denied permission to run this command]"
    else:
        with Spinner(f"Running {name}"):
            result = DISPATCH.get(name, lambda a: f"[unknown tool {name}]")(args)

    preview = result if len(result) < 400 else result[:400] + " ..."
    lines = preview.split("\n")
    for line in lines[:8]:
        print(f"  {C.CYAN}│{C.RESET}  {C.GREY}{_safe_terminal_text(line)}{C.RESET}")
    if len(lines) > 8:
        print(f"  {C.CYAN}│{C.RESET}  {C.GREY}... [{len(lines)-8} more lines]{C.RESET}")
    print(f"  {C.CYAN}└── {C.GREEN}✓ done{C.RESET}\n")
    return result


def _parse_args(call: dict) -> dict:
    """Parse tool arguments and reject malformed/non-object JSON explicitly."""
    function = call.get("function") if isinstance(call, dict) else None
    raw = function.get("arguments", "{}") if isinstance(function, dict) else "{}"
    if isinstance(raw, dict):
        parsed = raw
    elif isinstance(raw, str):
        if not raw.strip():
            parsed = {}
        else:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"arguments are not valid JSON: {exc.msg}") from exc
    else:
        raise ValueError("arguments must be a JSON object")
    if not isinstance(parsed, dict):
        raise ValueError("arguments must be a JSON object")
    return parsed


def _prepare_tool_calls(calls, step: int) -> list[dict]:
    """Normalize provider tool calls and parse their arguments once."""
    prepared = []
    if not isinstance(calls, list):
        return prepared
    for index, raw_call in enumerate(calls):
        call = raw_call if isinstance(raw_call, dict) else {}
        function = call.get("function")
        function = function if isinstance(function, dict) else {}
        name = function.get("name")
        name = name if isinstance(name, str) and name else "unknown_tool"
        try:
            args = _parse_args({"function": function})
            parse_error = None
        except ValueError as exc:
            args = {}
            parse_error = str(exc)

        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id:
            call_id = f"call_local_{step}_{index}_{uuid.uuid4().hex}"
        normalized_call = {
            "id": call_id,
            "type": "function",
            "function": {
                "name": name,
                # Keep the saved conversation valid even if a compatible API
                # returned malformed argument JSON.
                "arguments": json.dumps(args, ensure_ascii=False),
            },
        }
        signature_args = (json.dumps(args, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False)
                          if parse_error is None else f"<invalid:{function.get('arguments')!r}>")
        prepared.append({
            "call": normalized_call,
            "name": name,
            "args": args,
            "error": parse_error,
            "signature": (name, signature_args),
        })
    return prepared


def _execute_prepared_calls(cfg, calls: list[dict], previous_signatures: set[tuple]) -> tuple[set[tuple], bool]:
    """Execute a tool batch once and block repeated calls in adjacent turns."""
    current_signatures: set[tuple] = set()
    loop_detected = False
    for call in calls:
        signature = call["signature"]
        if signature in previous_signatures or signature in current_signatures:
            call["result"] = (
                f"[loop prevention] repeated {call['name']} call with identical arguments "
                "was not executed again"
            )
            loop_detected = True
        elif call["error"]:
            call["result"] = f"[error] invalid arguments for {call['name']}: {call['error']}"
        else:
            try:
                call["result"] = execute_tool(cfg, call["name"], call["args"])
            except Exception as exc:  # noqa: BLE001 - return tool failures to the model as results
                call["result"] = f"[error] tool execution failed: {exc}"
        current_signatures.add(signature)
    return current_signatures, loop_detected


def run_agent(cfg, history: list[dict], system: str) -> None:
    enable_ansi()
    previous_signatures: set[tuple] = set()
    for step in range(cfg.max_steps):
        if cfg.provider == "anthropic":
            with Spinner("Consulting Anthropic"):
                text, calls = anthropic_turn(cfg, history, system)
        elif cfg.provider == "gemini":
            with Spinner("Consulting Gemini"):
                text, calls, raw_parts = gemini_turn(cfg, history, system)
        else:
            if not cfg.stream:
                with Spinner("Consulting Model"):
                    text, calls = openai_turn(cfg, history, system)
            else:
                text, calls = openai_turn(cfg, history, system)
            if text and not cfg.stream:
                print(text)

        if cfg.provider in ("anthropic", "gemini") and text:
            # Print only after the spinner has stopped; writing while it redraws
            # the same terminal line can overwrite parts of multiline responses.
            animated_print(text)

        if not calls:
            final_text = text or "[The provider returned an empty response.]"
            if not text:
                print(final_text)
            if cfg.provider == "gemini":
                if raw_parts:
                    history.append({"role": "model", "parts": raw_parts})
                else:
                    history.append({"role": "model", "content": final_text})
            elif cfg.provider == "anthropic":
                history.append({"role": "assistant", "content": final_text})
            else:
                history.append({"role": "assistant", "content": final_text})
            return  # done — final answer already printed

        prepared_calls = _prepare_tool_calls(calls, step)
        if not prepared_calls:
            message = "[The model returned invalid tool-call data; this turn was stopped.]"
            history.append({"role": "model" if cfg.provider == "gemini" else "assistant",
                            "content": message})
            print(f"\n  {C.YELLOW}{message}{C.RESET}")
            return
        current_signatures, loop_detected = _execute_prepared_calls(
            cfg, prepared_calls, previous_signatures
        )
        previous_signatures = current_signatures

        if cfg.provider == "gemini":
            # Keep exact model parts, including thought signatures, in history.
            history.append({"role": "model", "parts": raw_parts})
            responses = [{"functionResponse": {
                "name": call["name"], "response": {"result": call["result"]}}}
                for call in prepared_calls]
            history.append({"role": "user", "parts": responses})
            if loop_detected:
                notice = "[Agent stopped after a repeated tool request to prevent a loop.]"
                history.append({"role": "model", "content": notice})
                print(f"\n  {C.YELLOW}{notice}{C.RESET}")
                return
            continue

        if cfg.provider == "anthropic":
            # Anthropic requires all tool_use blocks in one assistant turn,
            # followed by all matching tool_results in one user turn.
            assistant_content: list[dict] = []
            if text:
                assistant_content.append({"type": "text", "text": text})
            for call in prepared_calls:
                assistant_content.append({
                    "type": "tool_use",
                    "id": call["call"]["id"],
                    "name": call["name"],
                    "input": call["args"],
                })
            history.append({"role": "assistant", "content": assistant_content})

            user_content = [{
                "type": "tool_result",
                "tool_use_id": call["call"]["id"],
                "content": call["result"],
            } for call in prepared_calls]
            history.append({"role": "user", "content": user_content})
            if loop_detected:
                notice = "[Agent stopped after a repeated tool request to prevent a loop.]"
                history.append({"role": "assistant", "content": notice})
                print(f"\n  {C.YELLOW}{notice}{C.RESET}")
                return
            continue

        # OpenAI-compatible format: every call gets exactly one result.
        msg_dict: dict = {"role": "assistant"}
        if text:
            msg_dict["content"] = text
        msg_dict["tool_calls"] = [call["call"] for call in prepared_calls]
        history.append(msg_dict)

        for call in prepared_calls:
            history.append({"role": "tool",
                            "tool_call_id": call["call"]["id"],
                            "content": call["result"]})
        if loop_detected:
            notice = "[Agent stopped after a repeated tool request to prevent a loop.]"
            history.append({"role": "assistant", "content": notice})
            print(f"\n  {C.YELLOW}{notice}{C.RESET}")
            return

    notice = f"[Reached the agent step limit of {cfg.max_steps}; ask it to continue if needed.]"
    history.append({"role": "model" if cfg.provider == "gemini" else "assistant",
                    "content": notice})
    print(f"\n  {C.YELLOW}{notice}{C.RESET}")


def _extract_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(part.get("text", "") for part in value
                       if isinstance(part, dict) and isinstance(part.get("text"), str))
    return ""


def _provider_text_turn(cfg, user_text: str, system: str) -> str:
    """Make a plain-text provider call without exposing execution tools."""
    if cfg.provider == "anthropic":
        payload = {
            "model": cfg.model,
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": user_text}],
            "temperature": cfg.temperature,
        }
        if system:
            payload["system"] = system
        data = _post(cfg.base_url.rstrip("/") + "/messages",
                     {"x-api-key": cfg.key, "anthropic-version": "2023-06-01",
                      "Content-Type": "application/json"}, payload, stream=False,
                     timeout=cfg.timeout, retries=cfg.retries).json()
        text = _extract_text(data.get("content", []))
    elif cfg.provider == "gemini":
        payload = {
            "contents": [{"role": "user", "parts": [{"text": user_text}]}],
            "generationConfig": {"temperature": cfg.temperature, "maxOutputTokens": 2048},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        data = _post(
            f"{cfg.base_url.rstrip('/')}/models/{cfg.model}:generateContent",
            gemini_headers(cfg.key), payload, stream=False,
            timeout=cfg.timeout, retries=cfg.retries,
        ).json()
        candidates = data.get("candidates") or []
        parts = (candidates[0].get("content") or {}).get("parts", []) if candidates else []
        text = _extract_text(parts)
    else:
        messages = ([{"role": "system", "content": system}] if system else [])
        messages.append({"role": "user", "content": user_text})
        payload = {"model": cfg.model, "messages": messages,
                   "temperature": cfg.temperature, "max_tokens": 2048, "stream": False}
        data = _post(
            cfg.base_url.rstrip("/") + "/chat/completions",
            {"Authorization": f"Bearer {cfg.key}", "Content-Type": "application/json"},
            payload, stream=False, timeout=cfg.timeout, retries=cfg.retries,
        ).json()
        choices = data.get("choices") or []
        message = choices[0].get("message") or {} if choices else {}
        text = _extract_text(message.get("content"))
    if not text.strip():
        raise RuntimeError("provider returned an empty text response")
    return text.strip()


SPECIALIST_ROLES = ("researcher", "coder", "tester")
AGENT_SPECIALTIES = {
    "researcher": "Research the task, inspect relevant files or sources, and report concise evidence. Avoid modifying files.",
    "coder": "Implement the assigned software change carefully, inspect existing conventions, and verify the change.",
    "tester": "Run relevant tests or checks and report exact failures. Avoid unrelated edits.",
    "generalist": "Complete the assigned task using the available tools and report what you did.",
}


def _parse_agent_plan(text: str, original_prompt: str) -> list[dict]:
    """Parse and constrain a planner response; fall back to one generalist task."""
    fallback = [{"agent": "generalist", "task": original_prompt}]
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end <= start:
        print("[agent router returned invalid JSON; using a generalist task]", file=sys.stderr)
        return fallback
    try:
        data = json.loads(candidate[start:end + 1])
    except json.JSONDecodeError:
        print("[agent router returned invalid JSON; using a generalist task]", file=sys.stderr)
        return fallback
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        print("[agent router returned no task list; using a generalist task]", file=sys.stderr)
        return fallback

    tasks = []
    for item in data["tasks"]:
        if not isinstance(item, dict):
            continue
        role = item.get("agent")
        task = item.get("task")
        if not isinstance(task, str) or not task.strip():
            continue
        if role not in SPECIALIST_ROLES:
            role = "generalist"
        tasks.append({"agent": role, "task": task.strip()})
        if len(tasks) >= 4:
            break
    if not tasks:
        print("[agent router returned no usable tasks; using a generalist task]", file=sys.stderr)
        return fallback
    return tasks


def _last_agent_response(history: list[dict]) -> str:
    for message in reversed(history):
        if message.get("role") not in ("assistant", "model"):
            continue
        text = _extract_text(message.get("content"))
        if not text:
            text = _extract_text(message.get("parts"))
        if text.strip():
            return text.strip()
    return "(the specialist returned no final text)"


def _agent_conversation_context(history: list[dict]) -> str:
    entries = []
    for message in history[-13:-1]:
        role = message.get("role")
        if role not in ("user", "assistant", "model"):
            continue
        text = _extract_text(message.get("content")) or _extract_text(message.get("parts"))
        if text.strip():
            display_role = "assistant" if role == "model" else role
            entries.append(f"{display_role}: {text.strip()[:1400]}")
    return "\n".join(entries)[-8000:]


def run_multi_agent(cfg, history: list[dict], system: str) -> None:
    """Plan a small task graph, execute specialist agents sequentially, then synthesize."""
    if not history or not isinstance(history[-1].get("content"), str):
        raise ValueError("multi-agent mode requires a user prompt in conversation history")
    original_prompt = history[-1]["content"]
    conversation_context = _agent_conversation_context(history)
    roles = ", ".join(SPECIALIST_ROLES)
    router_system = (
        system + "\n\nYou are the task planner for a small team of specialist agents. "
        f"Choose agents only from: {roles}. Break the request into one to four useful, "
        "ordered tasks; avoid redundant work. Return only a JSON object in this shape: "
        '{"tasks":[{"agent":"researcher","task":"..."}]}. The task text must preserve '
        "the user's constraints. Planning does not authorize actions that the user did not request."
    )
    plan_prompt = (
        (f"Earlier conversation for context:\n{conversation_context}\n\n"
         if conversation_context else "")
        + f"Route this current request to specialist agents:\n{original_prompt}"
    )
    with Spinner("Planning specialist tasks"):
        plan_text = _provider_text_turn(cfg, plan_prompt, router_system)
    tasks = _parse_agent_plan(plan_text, original_prompt)
    print(f"\n  {C.CYAN}[router assigned {len(tasks)} specialist task(s)]{C.RESET}")

    reports = []
    for index, task in enumerate(tasks, 1):
        role = task["agent"]
        print(f"\n  {C.MAGENTA}[{index}/{len(tasks)} {role} agent]{C.RESET}")
        prior_reports = "\n\n".join(
            f"{report['agent']} report:\n{report['text'][:2500]}" for report in reports
        )
        worker_prompt = (
            (f"Earlier conversation for context:\n{conversation_context}\n\n"
             if conversation_context else "")
            + f"Original user request:\n{original_prompt}\n\n"
            + f"Your assigned {role} task:\n{task['task']}"
        )
        if prior_reports:
            worker_prompt += f"\n\nEarlier specialist reports (for context):\n{prior_reports}"
        specialist_system = (
            system + f"\n\nYou are the {role} specialist subagent. "
            + AGENT_SPECIALTIES[role]
            + " Do not claim another specialist's work as your own. Use only the existing "
            "permission gates; do not install Kali or packages unless the user specifically "
            "requested it and the installation controls are enabled."
        )
        worker_history = [{"role": "user", "content": worker_prompt}]
        try:
            run_agent(cfg, worker_history, specialist_system)
            report_text = _last_agent_response(worker_history)
        except Exception as exc:  # noqa: BLE001 - continue other specialists after one fails
            partial = _last_agent_response(worker_history)
            report_text = f"[specialist error: {exc}]\n{partial}"
        reports.append({"agent": role, "text": report_text})

    report_text = "\n\n".join(
        f"{report['agent']} agent:\n{report['text'][:4000]}" for report in reports
    )
    synthesis_system = (
        system + "\n\nYou are the coordinator. Synthesize the specialist reports into one "
        "accurate answer to the original user. Clearly distinguish completed actions from "
        "recommendations and mention any failures. Do not claim work that the reports do not support."
    )
    synthesis_prompt = (
        (f"Earlier conversation for context:\n{conversation_context}\n\n"
         if conversation_context else "")
        + f"Original user request:\n{original_prompt}\n\n"
        + f"Specialist reports:\n{report_text}"
    )
    try:
        with Spinner("Combining specialist reports"):
            final_text = _provider_text_turn(cfg, synthesis_prompt, synthesis_system)
    except Exception as exc:  # noqa: BLE001 - preserve completed subagent work if synthesis fails
        final_text = f"[synthesis request failed: {exc}]\n{report_text}"
    role = "model" if cfg.provider == "gemini" else "assistant"
    history.append({"role": role, "content": final_text})
    print(final_text)


def prompt_animated_cursor(prompt_prefix: str) -> str:
    """Read a line of input while showing an animated cursor."""
    return animated_prompt_input(prompt_prefix)


HELP = f"""
  {C.BOLD}commands{C.RESET}
    /help                this help
    /tools               list available tools
    /remove-key [prov]   remove current or specified provider API key from .env and enter a new one
    /keys                list saved API keys in .env
    /reset               clear conversation history
    /system <txt>        replace the system prompt
    /history             show message count
    /model <name>        switch current model
    /agents              toggle planner + specialist agents for later prompts
    /exit /quit          leave
  {C.BOLD}usage{C.RESET}: just type a task, e.g.
    > inspect system resources, search for .py files, and check git status
"""


def interactive(cfg) -> int:
    enable_ansi()
    print_banner(f"{cfg.provider}/{cfg.model}")
    system = cfg.system
    multi_agent = getattr(cfg, "multi_agent", False)
    history: list[dict] = []
    while True:
        try:
            cwd_name = os.path.basename(os.getcwd()) or os.getcwd()
            prompt_str = (
                f"{C.CYAN}╭─[{C.GREEN}{C.BOLD}◈ HACKERAGENT{C.RESET}{C.CYAN}]─"
                f"[{C.YELLOW}{cfg.provider}/{cfg.model}{C.CYAN}]─[{C.GREY}{cwd_name}{C.CYAN}]\n"
                f"╰─❯{C.CYAN}▌{C.RESET} "
            )
            line = prompt_animated_cursor(prompt_str)
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("/exit", "/quit"):
            return 0
        if line == "/help":
            print(HELP)
            continue
        if line == "/tools":
            for t in TOOLS:
                print(f"  {C.MAGENTA}{t['function']['name']}{C.RESET} — "
                      f"{t['function']['description']}")
            continue
        if line == "/keys":
            keys = list_saved_env_keys()
            if keys:
                print(f"  {C.CYAN}Saved API Keys in .env:{C.RESET}")
                for k in keys:
                    print(f"    {C.GREEN}•{C.RESET} {k}")
            else:
                print(f"  {C.GREY}No saved keys found in .env{C.RESET}")
            continue
        if line.startswith("/remove-key") or line.strip().lower() in ("remove api", "remove-key", "/remove_key"):
            parts = line.split()
            target_prov = parts[1].lower() if len(parts) > 1 else cfg.provider
            alias = {"claude": "anthropic", "google": "gemini", "gpt": "openai", "grok": "xai", "llama": "groq"}
            target_prov = alias.get(target_prov, target_prov)
            env_var = "GEMINI_API_KEY" if target_prov == "gemini" else KEY_ENV.get(target_prov, f"{target_prov.upper()}_API_KEY")
            
            removed = remove_env_key(env_var)
            if target_prov == "gemini":
                remove_env_key("GOOGLE_API_KEY")
            status_msg = f"removed {env_var} from environment and .env" if removed else f"{env_var} cleared from environment"
            print(f"  {C.YELLOW}[{status_msg}]{C.RESET}")
            
            try:
                new_key = input(f"  {C.CYAN}paste new {target_prov} API key (or Enter to skip):{C.RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                new_key = ""
            
            if new_key:
                save_env_key(env_var, new_key)
                os.environ[env_var] = new_key
                if target_prov == cfg.provider:
                    cfg.key = new_key
                print(f"  {C.GREEN}[saved new {env_var} to .env]{C.RESET}\n")
            else:
                if target_prov == cfg.provider:
                    cfg.key = ""
                print(f"  {C.GREY}[no key provided]{C.RESET}\n")
            continue
        if line.startswith("/model "):
            new_model = line[len("/model "):].strip()
            if new_model:
                cfg.model = new_model
                print(f"  {C.GREEN}switched model to: {cfg.model}{C.RESET}")
            continue
        if line == "/reset":
            history.clear()
            print(f"  {C.GREY}history cleared{C.RESET}")
            continue
        if line == "/history":
            print(f"  {C.GREY}{len(history)} messages{C.RESET}")
            continue
        if line == "/agents":
            multi_agent = not multi_agent
            mode = "planner + specialists" if multi_agent else "single agent"
            print(f"  {C.GREEN}task routing: {mode}{C.RESET}")
            continue
        if line.startswith("/system "):
            system = line[len("/system "):]
            print(f"  {C.GREY}system prompt updated{C.RESET}")
            continue

        history_start = len(history)
        history.append({"role": "user", "content": line})
        try:
            if multi_agent:
                run_multi_agent(cfg, history, system)
            else:
                run_agent(cfg, history, system)
        except Exception as exc:  # noqa: BLE001
            # Discard an unanswered turn if the model failed before making any
            # tool/history progress; subsequent prompts then start cleanly.
            if len(history) == history_start + 1:
                history.pop()
            else:
                recovery = "[The agent could not finish this turn because the request failed.]"
                history.append({"role": "model" if cfg.provider == "gemini" else "assistant",
                                "content": recovery})
            print(f"\n  {C.RED}[agent error] {exc}{C.RESET}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="HACKERAGENT — terminal AI agent.")
    p.add_argument("-p", "--prompt", default=None, help="one-shot task (else interactive)")
    p.add_argument("--provider", default="openai",
                   help="one of: " + ", ".join(DEFAULT_BASE) + " (aliases: claude, google, gpt, llama)")
    p.add_argument("--model", default=None)
    p.add_argument("--base-url", default=None)
    p.add_argument("--api-key", default=None)
    p.add_argument("--system", default=SYSTEM_PROMPT)
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument("--max-steps", type=int, default=12)
    p.add_argument("--timeout", type=float, default=180.0)
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--no-stream", action="store_true", help="disable token streaming")
    p.add_argument("--yes", action="store_true",
                   help="auto-approve shell and file-write tools (Kali setup still asks)")
    p.add_argument("--multi-agent", action="store_true",
                   help="use a planner plus researcher/coder/tester specialists")
    p.add_argument("--auto-install-tools", action="store_true",
                   help="allow prompt-triggered installation of allowlisted Kali packages (Windows only)")
    p.add_argument("--setup-kali", action="store_true",
                   help="ask permission, install Kali through WSL on Windows, and open Windows Terminal")
    p.add_argument("--workspace", default=os.getcwd(),
                   help="root directory available to file tools (default: current directory)")
    p.add_argument("--allow-outside-workspace", action="store_true",
                   help="allow file tools to access paths outside --workspace")
    cfg = p.parse_args(argv)
    if cfg.max_steps < 1:
        p.error("--max-steps must be at least 1")
    if not math.isfinite(cfg.timeout) or cfg.timeout <= 0:
        p.error("--timeout must be a finite number greater than zero")
    if not math.isfinite(cfg.temperature):
        p.error("--temperature must be finite")
    if cfg.retries < 0:
        p.error("--retries cannot be negative")
    alias = {"claude": "anthropic", "google": "gemini", "gpt": "openai",
             "grok": "xai", "llama": "groq"}
    cfg.provider = alias.get(cfg.provider.lower(), cfg.provider.lower())
    if cfg.provider not in DEFAULT_BASE:
        p.error(f"unknown provider '{cfg.provider}' (supported: {', '.join(DEFAULT_BASE)})")

    if cfg.setup_kali and cfg.prompt is not None:
        p.error("--setup-kali is a setup-only command; do not combine it with --prompt")
    enable_ansi()
    if cfg.setup_kali:
        result = setup_kali_with_confirmation()
        print(result)
        return 1 if result.startswith(("[error]", "[denied]")) else 0
    load_env_file()
    if cfg.provider == "gemini":
        cfg.key = (cfg.api_key or os.environ.get("GEMINI_API_KEY")
                   or os.environ.get("GOOGLE_API_KEY"))
    else:
        cfg.key = cfg.api_key or os.environ.get(KEY_ENV[cfg.provider])
    if not cfg.key:
        if cfg.provider == "ollama":
            cfg.key = "ollama"
        elif cfg.base_url:
            cfg.key = "local"
        else:
            env_var = "GEMINI_API_KEY" if cfg.provider == "gemini" else KEY_ENV[cfg.provider]
            print(f"  {C.YELLOW}{env_var} is not set.{C.RESET}")
            try:
                entered = input(f"  paste your {cfg.provider} API key (blank cancels): ").strip()
            except (EOFError, KeyboardInterrupt):
                entered = ""
            if entered:
                cfg.key = entered
                os.environ[env_var] = entered
                save_env_key(env_var, entered)
                print(f"  {C.GREEN}[saved {env_var} to .env]{C.RESET}\n")
            else:
                sys.exit(f"error: no API key (set ${env_var} or pass --api-key)")
    cfg.base_url = cfg.base_url or DEFAULT_BASE[cfg.provider]
    cfg.model = cfg.model or DEFAULT_MODEL[cfg.provider]
    cfg.stream = not cfg.no_stream
    cfg.workspace = os.path.realpath(cfg.workspace)
    if not os.path.isdir(cfg.workspace):
        p.error(f"workspace does not exist or is not a directory: {cfg.workspace}")

    if cfg.prompt is not None:
        if not cfg.prompt.strip():
            p.error("--prompt cannot be empty")
        print_banner(f"{cfg.provider}/{cfg.model}")
        history = [{"role": "user", "content": cfg.prompt}]
        try:
            if cfg.multi_agent:
                run_multi_agent(cfg, history, cfg.system)
            else:
                run_agent(cfg, history, cfg.system)
        except Exception as exc:  # noqa: BLE001 - report request/tool errors without a traceback
            print(f"\n  {C.RED}[agent error] {exc}{C.RESET}", file=sys.stderr)
            return 1
        return 0
    return interactive(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
