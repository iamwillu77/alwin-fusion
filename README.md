# ALWIN FUSION

**Open-source, offline-first AI-native operating system — in progress.**

Version **0.1.0** · Milestone: **Local AI Core Prototype**

Alwin Fusion lets you control your computer through natural-language
commands. This first milestone implements the trusted core: a
lightweight Python CLI that turns commands like *"Create a folder named
Business Reports in my Documents"* into **validated, approved, executed
and verified** filesystem actions — fully offline, with an optional
local Ollama model that is never required and never auto-downloaded.

```
ALWIN FUSION v0.1
Local AI Core

You: Create a folder named Business Reports in my Documents.

ALWIN: Proposed action:
       Create directory ~/Documents/Business Reports
       Approve? [y/N] y

ALWIN: Folder created and verified successfully.
```

## Features

- **Natural-language commands** (deterministic parser, works with no
  LLM at all):
  - list files and folders in an allowed directory
  - create a directory in an allowed location
  - create a text file with user-provided content
  - read a text file from an allowed location
  - explain which operation would be performed
- **Structured plans with an explicit action allowlist** — model
  output is JSON-parsed and validated by trusted code; it is never
  executed, never `eval`-ed, never turned into shell commands.
- **Path security**: operations confined to configured roots inside
  your home (default: `~/Documents`); rejects `..` traversal, absolute
  paths outside roots, and symbolic-link escapes (symlinks resolved
  before containment checks).
- **Approval before modification** for every mutating action; `y/N`
  prompt, EOF/Ctrl-C = *no*. `--dry-run` mode makes zero changes.
- **Verification before success**: the verifier re-checks the
  filesystem independently; failures report the real error.
- **Audit log** (task id, action, permission, approval, execution,
  verification) in `~/.local/state/alwin-fusion/audit.jsonl`,
  mode 0600/0700, with file content redacted.
- **Optional local AI runtime**: stdlib-only Ollama HTTP client with
  precise errors for missing server/model/timeouts, visible fallback
  to the built-in parser, and **no model downloads, ever**.

## Requirements

- Ubuntu 26.04.1 LTS (or any Linux) · x86_64 · Python **3.10+**
  (developed on Python 3.14.4)
- No third-party packages needed — standard library only.
- Optional: [Ollama](https://ollama.com) running locally for `--llm`.

## Quick start

```bash
cd alwin-fusion

# Interactive session (recommended)
python3 -m ai_core.cli

# Single command, dry run (nothing is written)
python3 -m ai_core.cli --dry-run "Create a folder named Test in Documents"

# Single command (asks for approval on stdin)
python3 -m ai_core.cli "Create a file named todo.txt in Documents containing buy milk"

# Show effective configuration / probe the local AI runtime
python3 -m ai_core.cli --show-config
python3 -m ai_core.cli --ai-status

# Use the local Ollama model to propose actions (always validated;
# falls back to the built-in parser with a visible note)
OLLAMA_MODEL=qwen3:0.6b python3 -m ai_core.cli --llm "make a folder called Reports in Documents"

# Help / exit
python3 -m ai_core.cli --help
#   ... in the prompt: type  help   or   exit
```

Exit codes: `0` success (or dry run / clean exit), `1` unsupported,
denied, rejected or failed action, `2` configuration error.

## Configuration

| Environment variable | Default | Meaning |
|---|---|---|
| `ALWIN_HOME` | your home dir | Home directory (used by tests) |
| `ALWIN_PERMITTED_ROOTS` | `Documents` | `:`-separated permitted roots; must be inside home |
| `ALWIN_STATE_DIR` | `~/.local/state/alwin-fusion` | Audit log directory |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Local Ollama endpoint |
| `OLLAMA_MODEL` | *(empty)* | Model name for `--llm`; never auto-downloaded |
| `OLLAMA_TIMEOUT` | `30` | HTTP timeout in seconds |

CLI flags: `--dry-run`, `--llm`, `--root DIR` (repeatable),
`--ai-status`, `--show-config`, `--version`, `--help`.

Never run this (or anything in this project) with `sudo`.

## Running the tests

```bash
scripts/run_tests.sh
# or
python3 -m unittest discover -s tests -v
```

All tests run in temporary directories; they never modify your real
`~/Documents`. The suite covers supported/unsupported commands,
traversal and symlink escapes, permission denial, approval rejection,
directory creation, verification failure, Ollama-unavailable
operation, absence of overwrite/delete capability, dry-run inertness
and audit redaction/permissions.

## How the local model is used

The core works with **zero** AI runtime. With `--llm`, the pipeline
asks Ollama (local HTTP only) for a single JSON action; the response
goes through the same allowlist validation as the deterministic
parser. If the server is down, the model is missing, the timeout
expires, or the response is invalid, you get an explanatory note and
the built-in parser handles the command instead. Alwin Fusion never
downloads models and contains no cloud fallback.

## Project layout

```
ai_core/        config · command_parser · planner · permissions ·
                executor · verifier · audit · pipeline · cli
ai_runtime/     ollama_client (stdlib HTTP, never pulls models)
integrations/   ActionDispatcher + filesystem tools (extension point)
tests/          unittest suite (temporary sandboxes only)
docs/           ARCHITECTURE.md · SECURITY.md
scripts/        run_tests.sh
app-bridge/ desktop/ models/ os-build/   reserved for later milestones
```

## Documentation

- `docs/ARCHITECTURE.md` — modules, data flow, action schema,
  configuration, testing, future extension guide.
- `docs/SECURITY.md` — security controls **and their known limits**.

## Status and known limitations

Implemented: the full parse → plan → permission → approval →
execute → verify → audit loop for the five supported actions, CLI
(dry-run, single-command and interactive modes), audit logging, local
Ollama client with graceful degradation, and the automated test
suite.

Deliberately **not** implemented yet: file deletion/overwrite, shell
or arbitrary command execution, network operations, process
sandboxing, multi-user support, MCP/LibreOffice/PDF integrations,
desktop automation, GUI, and bootable OS images. Passing tests does
not by itself make a system secure — read `docs/SECURITY.md` for the
actual threat model (TOCTOU races, approval as last line of defense,
non-tamper-proof audit log, and more).

## License

Open-source project (license to be added with the first release
tag).
