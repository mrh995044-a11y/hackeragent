#!/usr/bin/env python3
"""Provider-agnostic CLI chat client (compliant, general purpose).

Uses YOUR OWN API key and YOUR OWN system prompt. Talks to:
  * OpenAI-compatible endpoints (OpenAI, Azure OpenAI, vLLM, Ollama, LM Studio,
    OpenRouter, Groq, Together, llama.cpp server, ...)
  * Anthropic Messages API
  * Google Gemini (generateContent)

Features: streaming (SSE), retries with exponential backoff + jitter,
multi-turn interactive mode, prompt-from-file, JSON output.

Examples:
  export OPENAI_API_KEY=sk-...
  python aichat.py "Summarise the CAP theorem"
  python aichat.py --system "You are a terse SRE." --stream "Explain a WAL"
  python aichat.py --base-url http://localhost:11434/v1 --model llama3.1 "hi"
  python aichat.py --provider anthropic --model claude-3-5-sonnet-latest "hi"
  python aichat.py --provider gemini --model gemini-2.0-flash "hi"
  python aichat.py            # interactive multi-turn
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time

import requests

# The OpenAI-compatible providers share the Chat Completions protocol. The
# Anthropic and Gemini adapters below handle their native request formats.
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


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


def _post(url: str, headers: dict, payload: dict, *, stream: bool,
          timeout: float, retries: int) -> requests.Response:
    """POST with retry on connection errors, 429 and 5xx."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite number greater than zero")
    if retries < 0:
        raise ValueError("retries cannot be negative")
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload,
                                 stream=stream, timeout=timeout)
        except requests.RequestException as exc:
            last_exc = exc
            resp = None  # type: ignore[assignment]
        if resp is not None:
            if resp.status_code < 400:
                return resp
            # Retry only transient statuses.
            if resp.status_code in (408, 409, 429) or resp.status_code >= 500:
                last_exc = ApiError(resp.status_code, resp.text)
            else:
                raise ApiError(resp.status_code, resp.text)
        if attempt < retries:
            backoff = min(2 ** attempt, 30) + random.uniform(0, 0.75)
            print(f"[retry {attempt + 1}/{retries}] {last_exc}; sleeping {backoff:.1f}s",
                  file=sys.stderr)
            time.sleep(backoff)
    raise last_exc if last_exc else RuntimeError("request failed")


def _iter_sse(resp: requests.Response):
    for raw in resp.iter_lines(decode_unicode=True):
        # Requests can yield bytes when the server omits an explicit charset.
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if not isinstance(raw, str):
            continue
        raw = raw.lstrip(chr(0xfeff))
        if not raw or not raw.startswith("data:"):
            continue
        data = raw[5:].strip()
        if data == "[DONE]":
            return
        yield data


def chat_openai(base_url: str, key: str, model: str, messages: list[dict],
                *, stream: bool, temperature: float, max_tokens: int | None,
                timeout: float, retries: int) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages, "stream": stream,
               "temperature": temperature}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    resp = _post(url, headers, payload, stream=stream, timeout=timeout, retries=retries)
    if not stream:
        data = resp.json()
        choices = data.get("choices") or [{}]
        return choices[0].get("message", {}).get("content") or ""
    out: list[str] = []
    for chunk in _iter_sse(resp):
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        delta = (obj.get("choices") or [{}])[0].get("delta", {})
        piece = delta.get("content")
        if piece:
            out.append(piece)
            print(piece, end="", flush=True)
    if out:
        print()
    return "".join(out)


def chat_anthropic(base_url: str, key: str, model: str, messages: list[dict],
                   *, system: str | None, stream: bool, temperature: float,
                   max_tokens: int | None, timeout: float, retries: int) -> str:
    url = base_url.rstrip("/") + "/messages"
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
               "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages, "stream": stream,
               "temperature": temperature, "max_tokens": max_tokens or 1024}
    if system:
        payload["system"] = system
    resp = _post(url, headers, payload, stream=stream, timeout=timeout, retries=retries)
    if not stream:
        data = resp.json()
        return "".join(b.get("text", "") for b in data.get("content", []))
    out: list[str] = []
    for chunk in _iter_sse(resp):
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "content_block_delta":
            piece = (obj.get("delta") or {}).get("text")
            if piece:
                out.append(piece)
                print(piece, end="", flush=True)
    if out:
        print()
    return "".join(out)


def chat_gemini(base_url: str, key: str, model: str, messages: list[dict],
                *, system: str | None, temperature: float, max_tokens: int | None,
                timeout: float, retries: int) -> str:
    url = f"{base_url.rstrip('/')}/models/{model}:generateContent"
    contents = []
    for m in messages:
        role = m.get("role")
        if "parts" in m:
            contents.append(m)
        elif role == "user":
            contents.append({"role": "user", "parts": [{"text": m.get("content") or ""}]})
        elif role in ("assistant", "model"):
            contents.append({"role": "model", "parts": [{"text": m.get("content") or ""}]})
    gen = {"temperature": temperature}
    if max_tokens is not None:
        gen["maxOutputTokens"] = max_tokens
    payload = {"contents": contents, "generationConfig": gen}
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    data = _post(url, {"Content-Type": "application/json", "x-goog-api-key": key},
                 payload, stream=False, timeout=timeout, retries=retries).json()
    candidates = data.get("candidates") or []
    if not candidates:
        feedback = data.get("promptFeedback")
        return f"[no response generated: {feedback or data}]"
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts)


def send(args, messages: list[dict], system: str | None) -> str:
    if args.provider == "gemini":
        key = (args.api_key or os.environ.get("GEMINI_API_KEY")
               or os.environ.get("GOOGLE_API_KEY"))
    else:
        key = args.api_key or os.environ.get(KEY_ENV[args.provider])
    if not key:
        if args.provider == "ollama":
            key = "ollama"  # local Ollama ignores the bearer token
        elif args.base_url:
            key = "local"
        else:
            env_var = "GEMINI_API_KEY" if args.provider == "gemini" else KEY_ENV[args.provider]
            print(f"{env_var} is not set.", file=sys.stderr)
            try:
                entered = input(f"paste your {args.provider} API key (blank cancels): ").strip()
            except (EOFError, KeyboardInterrupt):
                entered = ""
            if entered:
                key = entered
                os.environ[env_var] = entered
                save_env_key(env_var, entered)
                print(f"[saved {env_var} to .env]\n", file=sys.stderr)
            else:
                sys.exit(f"error: no API key (set ${env_var} or pass --api-key)")
    base = args.base_url or DEFAULT_BASE[args.provider]
    model = args.model or DEFAULT_MODEL[args.provider]
    common = dict(stream=args.stream, temperature=args.temperature,
                  max_tokens=args.max_tokens, timeout=args.timeout,
                  retries=args.retries)
    if args.provider == "anthropic":
        return chat_anthropic(base, key, model, messages, system=system, **common)
    if args.provider == "gemini":
        gcommon = {k: v for k, v in common.items() if k != "stream"}
        return chat_gemini(base, key, model, messages, system=system, **gcommon)
    msgs = ([{"role": "system", "content": system}] if system else []) + messages
    return chat_openai(base, key, model, msgs, **common)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Compliant multi-provider chat client.")
    p.add_argument("prompt", nargs="?", help="prompt text (omit for interactive mode)")
    p.add_argument("--provider", default="openai",
                   help="one of: " + ", ".join(DEFAULT_BASE) + " (aliases: claude, google, gpt, llama)")
    p.add_argument("--model", default=None)
    p.add_argument("--base-url", default=None,
                   help="override base URL (e.g. http://localhost:11434/v1)")
    p.add_argument("--api-key", default=None, help="else read from env var")
    p.add_argument("--system", default=None, help="system prompt")
    p.add_argument("--system-file", default=None, help="read system prompt from file")
    p.add_argument("--prompt-file", default=None, help="read the prompt from file")
    p.add_argument("--stream", action="store_true", help="stream tokens as they arrive")
    p.add_argument("--temperature", type=float, default=0.7)
    p.add_argument("--max-tokens", type=int, default=None)
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--json", action="store_true", help="print final answer as JSON object")
    return p


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    parser = build_parser()
    args = parser.parse_args(argv)
    alias = {"claude": "anthropic", "google": "gemini", "gpt": "openai",
             "llama": "groq"}
    args.provider = alias.get(args.provider.lower(), args.provider.lower())
    if args.provider not in DEFAULT_BASE:
        parser.error(f"unknown provider '{args.provider}' (supported: {', '.join(DEFAULT_BASE)})")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a finite number greater than zero")
    if not math.isfinite(args.temperature):
        parser.error("--temperature must be finite")
    if args.retries < 0:
        parser.error("--retries cannot be negative")
    if args.max_tokens is not None and args.max_tokens <= 0:
        parser.error("--max-tokens must be greater than zero")

    try:
        system = args.system
        if args.system_file:
            with open(args.system_file, encoding="utf-8") as file:
                system = file.read()

        prompt = args.prompt
        if args.prompt_file:
            with open(args.prompt_file, encoding="utf-8") as file:
                prompt = file.read()
    except (OSError, UnicodeError) as exc:
        parser.error(f"could not read prompt file: {exc}")

    if prompt is not None:
        if not prompt.strip():
            parser.error("prompt cannot be empty")
        try:
            text = send(args, [{"role": "user", "content": prompt}], system)
        except Exception as exc:  # noqa: BLE001 - avoid a raw traceback for API/file errors
            print(f"error: request failed; no answer was received: {exc}", file=sys.stderr)
            return 1
        if not isinstance(text, str) or not text.strip():
            print("error: the provider returned an empty response", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps({"answer": text}))
        elif not args.stream:
            print(text)
        return 0

    # Interactive multi-turn mode.
    print("interactive mode — Ctrl-C or 'exit' to quit (type /remove-key to switch API keys)", file=sys.stderr)
    history: list[dict] = []
    while True:
        try:
            line = input("you▌> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line.lower() in ("exit", "quit"):
            break
        if not line:
            continue
        if line.startswith("/remove-key") or line.lower() in ("remove api", "remove-key"):
            env_var = "GEMINI_API_KEY" if args.provider == "gemini" else KEY_ENV.get(args.provider, f"{args.provider.upper()}_API_KEY")
            remove_env_key(env_var)
            args.api_key = None
            if args.provider == "gemini":
                remove_env_key("GOOGLE_API_KEY")
                os.environ.pop("GOOGLE_API_KEY", None)
            print(f"[cleared {env_var}]", file=sys.stderr)
            try:
                new_key = input(f"paste new {args.provider} API key (or Enter to skip): ").strip()
            except (EOFError, KeyboardInterrupt):
                new_key = ""
            if new_key:
                save_env_key(env_var, new_key)
                os.environ[env_var] = new_key
                args.api_key = new_key
                print(f"[saved {env_var} to .env]\n", file=sys.stderr)
            continue
        # A failed request must not leave an unanswered user turn in history;
        # otherwise later prompts resend it and can trigger provider role errors.
        history.append({"role": "user", "content": line})
        try:
            answer = send(args, history, system)
        except Exception as exc:  # noqa: BLE001 - keep the interactive session usable
            history.pop()
            print(f"[send failed; input was not added to conversation history: {exc}]",
                  file=sys.stderr)
            continue
        if not isinstance(answer, str) or not answer.strip():
            history.pop()
            print("[no answer received; input was not added to conversation history]",
                  file=sys.stderr)
            continue
        if not args.stream:
            print(answer)
        history.append({"role": "assistant", "content": answer})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
