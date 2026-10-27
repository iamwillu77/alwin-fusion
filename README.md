# ALWIN FUSION

**Open-source, offline-first AI-native operating system — in progress.**

Version **0.2.0** · Milestone: **Local Document and Spreadsheet Automation**

Alwin Fusion lets you control your computer through natural-language
commands. v0.2 adds local document generation — plain-text reports,
PDFs and `.xlsx` spreadsheets — on top of the trusted core: a
lightweight Python CLI that turns commands into **validated, approved,
executed and verified** filesystem actions — fully offline, with an
optional local Ollama model that is never required and never
auto-downloaded.

```
ALWIN FUSION v0.2
Local AI Core + Documents

You: create a report named summary.txt in Documents titled "Q1 Summary" containing "Sales rose 12 percent."

ALWIN: Proposed action:
       Create text report ~/Documents/summary.txt (1 blocks)
       Approve? [y/N] y

ALWIN: Text report created and verified byte-for-byte.
```

## Features

- **Natural-language commands** (deterministic parser, works with no
  LLM at all):
  - list files and folders in an allowed directory
  - create a directory in an allowed location
  - create a text file with user-provided content
  - read a text file from an allowed location
  - **create a formatted text report** from `titled`/`containing` text
    or a `using report.json` source
  - **create a PDF document** from the same content structures
  - **create an `.xlsx` spreadsheet** (sheets, headers, rows, formulas,
    number formats, column widths, styling) from a JSON specification
  - explain which operation would be performed
- **Structured plans with an explicit action allowlist** — model
  output is JSON-parsed and validated by trusted code; it is never
  executed, never `eval`-ed, never turned into shell commands.
- **Structured content is validated field by field** (`ai_core/specs.py`):
  closed key sets, bounded sizes, safe file names, and a conservative
  spreadsheet formula grammar (allow-listed functions only — no
  `WEBSERVICE`/`HYPERLINK`, no external-workbook or cross-sheet links,
  every cell reference inside the declared grid).
- **Path security**: operations confined to configured roots inside
  your home (default: `~/Documents`); rejects `..` traversal, absolute
  paths outside roots, and symbolic-link escapes (symlinks resolved
  before containment checks). Content source files (`using spec.json`)
  go through the same permission checks.
- **Approval before modification** for every mutating action; `y/N`
  prompt, EOF/Ctrl-C = *no*. `--dry-run` mode makes zero changes.
- **Verification before success**: the verifier re-checks the
  filesystem independently — reports are re-rendered and compared
  byte-for-byte, PDFs are structurally validated, and workbooks are
  **reopened** and compared cell-by-cell against the approved spec
  (including formula strings). Failures report the real error.
- **Audit log** in `~/.local/state/alwin-fusion/audit.jsonl`,
  mode 0600/0700, with file content and structured `spec`/`content`
  redacted.
- **Optional local AI runtime**: stdlib-only Ollama HTTP client with
  precise errors for missing server/model/timeouts, visible fallback
  to the built-in parser, and **no model downloads, ever**.

## Requirements

- Ubuntu 26.04.1 LTS (or any Linux) · x86_64 · Python **3.10+**
  (developed on Python 3.14.4)
- The core (parse → plan → permission → approval → execute → verify →
  audit) and all v0.1 features need **only the standard library**.
- Optional, for the v0.2 document features (installed by **you**, with
  your own approval — Alwin Fusion never installs anything):

  ```bash
  python3 -m pip install --user openpyxl reportlab   # or: pip install -r requirements.txt
  ```

  | Package | Enables | If missing |
  |---|---|---|
  | `openpyxl` | `create_spreadsheet` (.xlsx) | clear "dependency missing" error with the install command |
  | `reportlab` | `create_pdf` | clear "dependency missing" error with the install command |

  Text reports (`create_report`) always work — they are pure stdlib.
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

# v0.2: text report (stdlib only)
python3 -m ai_core.cli 'create a report named summary.txt in Documents titled "Q1 Summary" containing "Sales rose 12 percent."'

# v0.2: spreadsheet from a JSON spec (needs openpyxl)
python3 -m ai_core.cli "create a spreadsheet named sales.xlsx in Documents using sales.json"

# v0.2: PDF (needs reportlab)
python3 -m ai_core.cli 'create a pdf named readme.pdf in Documents containing "Welcome to Alwin Fusion."'

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

### Spreadsheet specifications

Spreadsheet content comes as JSON — either a file referenced with
`using sales.json` (read with full permission checks inside the
allowed roots) or inline after `from {...}`:

```json
{
  "title": "Sales 2026",
  "sheets": [
    {
      "name": "Q1",
      "headers": ["Region", "Amount"],
      "rows": [["North", 100], ["South", 250.5], ["Total", null]],
      "formulas": [{"cell": "B4", "formula": "=SUM(B2:B3)"}],
      "number_formats": {"B2:B3": "#,##0.00"},
      "column_widths": {"A": 16},
      "styles": {"header_bold": true, "freeze_header": true, "header_fill": "#DDEBF7"}
    }
  ]
}
```

Report/PDF content uses `{"title": "...", "blocks": [...]}` with
`heading` (level 1–3), `paragraph` and `list` (`bullet`/`numbered`)
blocks.

> **Formulas are stored, not calculated.** Alwin Fusion validates and
> writes formula text (`=SUM(B2:B3)`); it never computes results —
> openpyxl does not calculate either. Open the workbook in a
> spreadsheet application (LibreOffice Calc, Excel, …) to calculate.

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
`~/Documents`. The suite (241 tests) covers supported/unsupported
commands, traversal and symlink escapes, permission denial, approval
rejection, directory creation, verification failure, Ollama-unavailable
operation, absence of overwrite/delete capability, dry-run inertness,
audit redaction/permissions, and the v0.2 document features: report
rendering, workbook creation with formula preservation, PDF structural
validation, content-source permission/size/encoding checks,
existing-file protection and lying/tampering handlers failing
verification. The spreadsheet/PDF pipeline tests skip themselves
politely when the optional package is not installed (the standard
library validator tests always run).

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
ai_core/        config · command_parser · specs · planner · permissions ·
                executor · verifier · audit · pipeline · cli
ai_runtime/     ollama_client (stdlib HTTP, never pulls models)
integrations/   ActionDispatcher + filesystem tools + documents/
                (report · pdf · spreadsheet · tools — extension point)
tests/          unittest suite (temporary sandboxes only)
docs/           ARCHITECTURE.md · SECURITY.md
scripts/        run_tests.sh
requirements.txt  optional v0.2 document dependencies (you install them)
app-bridge/ desktop/ models/ os-build/   reserved for later milestones
```

## Documentation

- `docs/ARCHITECTURE.md` — modules, data flow, action schema,
  configuration, testing, future extension guide.
- `docs/SECURITY.md` — security controls **and their known limits**.

## Status and known limitations

Implemented: the full parse → plan → permission → approval →
execute → verify → audit loop for all eight actions (including
`create_report`, `create_pdf`, `create_spreadsheet`), CLI (dry-run,
single-command and interactive modes), structured-content validation,
audit logging with structured-content redaction, local Ollama client
with graceful degradation, and the automated test suite.

Known limitations (details in `docs/SECURITY.md`):

- **Formulas are written as text and never calculated** — open a
  workbook in a spreadsheet application to compute results; Alwin
  Fusion never claims a calculated value.
- **PDFs are validated structurally, not byte-compared** (PDFs embed
  creation metadata), and text outside latin-1 is replaced (basic
  Helvetica fonts only — no CJK/emoji glyphs).
- Optional dependencies are loaded lazily; without `openpyxl` /
  `reportlab` those actions fail cleanly with an install hint.
- Still deliberately absent: file deletion/overwrite, shell or
  arbitrary command execution, network operations, process
  sandboxing, multi-user support, MCP/LibreOffice integrations,
  desktop automation, GUI, and bootable OS images.

Passing tests does not by itself make a system secure — read
`docs/SECURITY.md` for the actual threat model (TOCTOU races,
approval as last line of defense, non-tamper-proof audit log, and
more).

## License

Open-source project (license to be added with the first release
tag).
