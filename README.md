# hackeragent

Two tools for working with LLM APIs you are **authorised** to use:

1. `aichat.py` — a clean, compliant multi-provider chat client (your key, your system prompt).
2. `redteam.py` — a prompt-injection / guardrail robustness harness for **your own** endpoint.

> Scope: `redteam.py` is for testing an LLM deployment you own or have written
> permission to assess. It measures whether a target resists adversarial prompt
> *shapes*; it does not ship operational harmful content.

## Setup

```bash
cd hackeragent
python -m pip install -r requirements.txt
```

Keys come from environment variables or the local `.env` file (never hardcode them in scripts):

```bash
export OPENAI_API_KEY=sk-...        # Windows: set OPENAI_API_KEY=...
export ANTHROPIC_API_KEY=sk-ant-...
export GEMINI_API_KEY=...            # or GOOGLE_API_KEY
```

> **API Key Persistence & Removal**: When you enter an API key when prompted in `start.bat`, `chat.bat`, or the Python tools, it is automatically saved to `.env`. You can remove or change it at any time:
> - In `start.bat` or `chat.bat`: Select menu option `[5]` or `[6]` (`Remove API key / Reset saved keys`)
> - In the interactive agent: Use `/remove-key [provider]` (or type `remove api`), or `/keys` to inspect saved keys
> - Once removed, you can immediately paste a new key without restarting!

## Terminal UI

- Spinner status indicators while supported providers are working
- Streamed tokens and complete replies are printed without cursor rewrites, preserving wrapped and multiline text
- Interactive command prompt indicator (`╰─❯▌`) with ANSI terminal support
- Multi-provider support (OpenAI, Anthropic/Claude, Google Gemini, Groq, Ollama, OpenRouter, DeepSeek, Mistral, Together, xAI, Perplexity)

## 2 — Chat client (`aichat.py`)

```bash
# OpenAI / any OpenAI-compatible endpoint
python aichat.py "Summarise the CAP theorem"
python aichat.py --system "You are a terse SRE." --stream "Explain a WAL"
python aichat.py --base-url http://localhost:11434/v1 --model llama3.1 "hi"   # Ollama
python aichat.py --base-url http://localhost:1234/v1   --model my-model "hi"  # LM Studio/vLLM

# Anthropic (alias: claude)
python aichat.py --provider anthropic --model claude-3-5-sonnet-latest "hi"

# Google Gemini (alias: google)
python aichat.py --provider gemini --model gemini-2.0-flash "hi"
python aichat.py --provider google --model gemini-2.0-flash "hi"   # alias

# Interactive multi-turn / from file / JSON output
python aichat.py
python aichat.py --prompt-file task.txt --system-file persona.txt --json
```

Providers: `openai` (default), `anthropic` (alias `claude`), `gemini`
(alias `google`), `groq`, `openrouter`, `deepseek`, `mistral`, `together`,
`xai`, `perplexity`, and local `ollama` (alias `llama` for Groq). The first
three use their native adapters where needed; the others use the OpenAI-compatible
Chat Completions API. Provider-specific keys are read from their matching
`*_API_KEY` environment variable or passed with `--api-key`. Gemini uses the
`generateContent` endpoint; Ollama defaults to `http://localhost:11434/v1`.

Flags: `--temperature`, `--max-tokens`, `--timeout`, `--retries`, `--stream`, `--json`.
Retries use exponential backoff with jitter on 429/5xx and connection errors.
Interactive sends that fail are removed from local history, so the next prompt
won't accidentally resend an unanswered turn. Gemini API keys are sent in an
HTTP header rather than included in the request URL.

## 1 — Red-team harness (`redteam.py`)

Point it at a target you control. Start with `--dry-run` to see the corpus:

```bash
python redteam.py --dry-run
python redteam.py --base-url http://localhost:8000/v1 --model my-model --out report.json -j 8 --fail-on any
python redteam.py --provider anthropic --model claude-3-5-sonnet-latest --cases testcases.json
```

Each case is classified:

| verdict | meaning |
|---|---|
| `RESISTED` | target refused / held the guardrail (good) |
| `COMPLIED` | target produced the disallowed output / leaked the canary (finding) |
| `INCONCLUSIVE` | neither a clear refusal nor a clear leak |
| `CONTROL_OK` | benign control answered correctly |
| `CONTROL_FAIL` | benign control failed → target misconfigured, results untrustworthy |
| `ERROR` | transport/HTTP error |

**Always check `CONTROL_FAIL` first** — if your control case fails, the target
isn't behaving normally and the rest of the results mean nothing.

Add `--fail-on complied`, `--fail-on error`, or `--fail-on any` to make the
harness return exit code 1 for CI regression gates. Reports include a schema
version and UTC generation time. Transient 408/409/429/5xx errors retry twice
by default. Tune this with `--retries`.

Add your own cases in `testcases.json`: each needs `id`, `category`, `prompt`,
and an optional `canary` (a token the model would only emit if it complied).

### Reading results
`COMPLIED` on `system_prompt_leak` = the target disclosed its system prompt.
`COMPLIED` on `tool_hijack` = it acted on injected tool instructions.
Report these with: target, model/version, the exact prompt, the raw response,
and the category — that's a reproducible finding.

## Established frameworks (recommended for depth)

`redteam.py` is a lightweight, transparent harness. For broader coverage use
purpose-built tools — same rule: authorised targets only.

```bash
python -m pip install garak
garak --model_type openai --model_name my-model --probes promptinject,leakreplay,dan

python -m pip install pyrit          # Microsoft's orchestration framework
# PyRIT: multi-turn orchestrators, scorers, converters (base64/rot13/etc.)

npx promptfoo@latest redteam run     # config-driven red-team + eval, CI-friendly
```

- **garak** — probe families: `promptinject`, `leakreplay`, `dan`, `encoding`, `xss`, ...
- **PyRIT** — programmatic multi-turn attacks and scorer pipelines.
- **promptfoo** — declarative red-team suites you can run in CI as regressions.

## Notes
- Never commit real keys. Use env vars or a secrets manager.
- Rotate any key you have pasted anywhere plaintext.
- Keep an evidence trail (prompt + raw response + timestamp) for each finding.


---

# `agent.py` — HACKERAGENT (terminal AI agent)

An agentic terminal assistant that can inspect and modify the selected project,
run commands, and use local system tools. By default, file tools stay inside the
selected workspace; shell commands are confirmation-gated unless you opt into
`--yes`.

Tools include:
* `shell` — run host commands (confirmation required by default)
* `read_file`, `write_file`, `list_dir`, `search_files`, `git_info` — project tools
* `system_info`, `process_list`, `env_vars` — inspect the host
* `fetch_url` — retrieve remote documentation and APIs
* `local_security_audit` — redacted, read-only secret/risky-code pattern checks in a project
* `web_security_check` — one HTTP HEAD check for HTTPS and common security headers
* `tcp_port_scan` — bounded TCP connect scan of one explicitly authorized host
* `setup_kali`, `install_kali_tools`, `kali_shell` — optional Windows WSL/Kali workflow

> Security scope: use security tools only on systems you own or have explicit
authorization to assess. Kali setup and package installation change the local
machine and are opt-in as described below.

## Run it

```bash
export OPENAI_API_KEY=sk-...            # Windows: set OPENAI_API_KEY=sk-...
# or set ANTHROPIC_API_KEY / GEMINI_API_KEY (or GOOGLE_API_KEY), etc.

python agent.py                         # interactive TUI
python agent.py -p "inspect system resources and check git status"
python agent.py --multi-agent -p "review this repository and test the proposed fix"
python agent.py --workspace ./project -p "review this repository"
python agent.py -p "run the read-only security audit on this workspace"
python agent.py -p "check HTTPS headers for https://my-owned-site.example"
python agent.py -p "check TCP ports 80 and 443 on my authorized test host"

# local Ollama (no API key prompt)
python agent.py --provider ollama --model llama3.1

# planner + researcher/coder/tester specialists (interactive mode can use /agents)
python agent.py --multi-agent

# Opt into file tools outside the selected workspace
python agent.py --allow-outside-workspace

# Opt into package installation requests in Kali; package allowlist still applies
python agent.py --auto-install-tools
```

Providers include `openai`, `anthropic`, `gemini`, `groq`, `openrouter`,
`deepseek`, `mistral`, `together`, `xai`, `perplexity`, and local `ollama`.
API keys are read from their matching `*_API_KEY` environment variables or
`--api-key`; Ollama uses `http://localhost:11434/v1` by default and does not
require an API key.

## Workspace and confirmation gates

File tools are constrained to the current directory (or `--workspace PATH`) by
default. Pass `--allow-outside-workspace` to opt file tools into broader paths.
This does not sandbox shell commands: they start in the workspace and require
interactive confirmation by default. `--yes` opts out of confirmation for shell,
file-write, and Kali command execution. Introspection tools such as `system_info`,
`process_list`, `env_vars`, and `git_info` do not prompt.

## Authorized security checks

`local_security_audit` scans text files inside the selected workspace for common
hardcoded-secret and risky-code patterns. It reports only the finding type and
file/line location; secret values are never returned. Generated directories and
large/binary files are skipped, and the scan is read-only.

`web_security_check` makes a single HTTP `HEAD` request, does not follow
redirects or fetch a response body, and reports HTTPS certificate validation and
common security-header presence. Use it only on a site you own or are authorized
to assess.

`tcp_port_scan` performs a sequential TCP connect check against one hostname or
IP. It defaults to a small common-port list, accepts at most 64 ports, and does
not accept CIDR/subnet targets. It always asks for explicit confirmation—even
with `--yes`—and is limited to systems you own or have permission to test. These
checks are triage aids, not proof that a system is secure or vulnerable.

## Optional Kali on Windows

On Windows, run `python agent.py --setup-kali`. It asks for a separate, explicit
confirmation before it tries to install Kali through WSL and open it in Windows
Terminal. That confirmation is never skipped by `--yes`. Setup may require an
elevated Windows Terminal or a restart if WSL is not enabled yet.

After setup, `kali_shell` runs non-interactive commands inside the Kali WSL
distribution and returns their output; it asks for shell confirmation unless
`--yes` was passed. The agent can install packages from prompts only when
`--auto-install-tools` is enabled. Installs are limited to this allowlist:
`curl`, `wget`, `git`, `jq`, `python3`, `python3-pip`, `python3-venv`, `nmap`,
`whatweb`, `nikto`, `gobuster`, and `sqlmap`. Installation uses the Kali WSL
root account for `apt-get`; it does not execute arbitrary install scripts.

## In-session commands

`/help` `/tools` `/reset` `/system <txt>` `/history` `/agents` `/exit`

`/agents` toggles planner + specialist routing for subsequent prompts. The
planner assigns up to four ordered tasks to researcher, coder, or tester agents;
the coordinator combines their reports. Each specialist uses the same tool
permission gates.

## How the loop works

1. The request goes to the selected provider with tool schemas.
2. A requested tool runs under the workspace and confirmation controls, and its
   result is returned to the model.
3. The loop continues until the model answers or `--max-steps` is reached.
4. Streaming is enabled by default for compatible providers; use `--no-stream`
   to disable it.

## Notes
- `--max-steps` (default 12) caps the loop to avoid runaway behaviour. Repeated
  identical tool requests are stopped before a side effect can repeat.
- Request failures are reported without a traceback; unanswered interactive
  prompts are removed from history so the session remains usable.
- Gemini keys may come from `GEMINI_API_KEY` or `GOOGLE_API_KEY` and are sent in
  an HTTP header. Anthropic uses the Messages API; Gemini uses `generateContent`.
