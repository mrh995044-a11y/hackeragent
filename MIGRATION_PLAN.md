# Jcode-aligned migration plan

## Decision

`jcode-master` is a mature, multi-crate Rust coding-agent workspace. Copying it
verbatim would replace this project with Jcode rather than produce an LLM
red-team tool. The migration will therefore preserve the authorised-red-team
purpose while adopting the same separation of concerns:

| Jcode layer | `hackeragent` equivalent |
|---|---|
| root CLI | `redteam-cli` binary |
| app core / agent runtime | `redteam-core` case execution and scoring |
| provider runtimes | `redteam-providers` provider adapters |
| tool and permission types | `redteam-policy` target authorization and run policy |
| session and report storage | `redteam-storage` run artifacts and regression history |
| TUI presentation | `redteam-tui` in a later milestone |

## Compatibility and transition

The existing Python `redteam.py`, `agent.py`, and batch launchers stay intact
while the Rust workspace is introduced. This avoids breaking existing users.
The first Rust binary will support `--dry-run` and local JSON case validation,
then provider execution and report storage will migrate behind compatible CLI
flags.

## Current status

Milestones 1 and 2 now have working source-level implementations:

- root Cargo workspace with `redteam-cli`, `redteam-core`, `redteam-policy`,
  `redteam-providers`, and `redteam-storage` crates.
- `redteam-core` owns cases, verdicts, Python-compatible classification,
  validation, summaries, CI fail modes, and default corpus data.
- `redteam-storage` owns dependency-free JSON case loading plus report
  serialization for offline/dry-run workflows.
- `redteam-policy` owns explicit authorization and bounded run policy.
- `redteam-providers` defines the live provider boundary and currently fails
  closed, directing users to the existing Python harness for live traffic.
- `redteam-cli` supports `--help`, `--dry-run`, `--cases`, `--fail-on`, target
  metadata, and safe validation/report plumbing.

Validation completed: Python `unittest` regression suite passes and Python entry
points compile. Rust validation is still blocked by the local Rustup toolchain:
`rustc` is missing from `stable-x86_64-pc-windows-msvc`, and repeated Rustup
repair/install attempts failed or stalled on component download/manifest errors.

## Delivery milestones

1. Add a Cargo workspace and a compilable `redteam-cli` entry point.
2. Move case schema, validation, classifications, and report generation into
   `redteam-core` with unit tests.
3. Add provider adapters with explicit target authorization and bounded retry.
4. Add persisted runs, regression comparisons, and machine-readable reports.
5. Add an interactive TUI only after CLI parity is proven.

## Non-goals

- Copying all of Jcode's unrelated desktop, swarm, memory, and provider
  authentication code.
- Automated offensive activity or testing systems without authorization.
