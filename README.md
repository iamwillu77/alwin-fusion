# ALWIN FUSION

**Open-source, offline-first AI-native operating system — in progress.**

Version **0.3.0** · Milestone: **Application Integration Engine**

Alwin Fusion lets you control your computer through natural-language
commands. v0.3 adds a controlled application integration engine —
read-only discovery of installed desktop applications (LibreOffice
Writer/Calc, common PDF viewers) and approval-gated opening of an
existing, permission-checked file with a registered application — on
top of the trusted core: a lightweight Python CLI that turns commands
into **validated, approved, executed and verified** actions — fully
offline, with an optional local Ollama model that is never required
and never auto-downloaded.

```
ALWIN FUSION v0.3
Local AI Core + Documents + Applications

You: which applications are installed?

ALWIN: Detected 5 application(s): 1 available, 4 not_installed. Nothing was launched, installed or changed.
         Desktop default application: available
         Document Viewer (Evince): not_installed
         LibreOffice Calc: not_installed
         LibreOffice Writer: not_installed
         Okular: not_installed

You: open summary.txt in Documents with LibreOffice Writer

ALWIN: Proposed action:
       Open ~/Documents/summary.txt with LibreOffice Writer
       Approve? [y/N] y

ALWIN: Verified: the file still exists inside the permitted roots and the
       application process was started. This confirms the launch only —
       Alwin Fusion does not inspect or control the application window.
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
  - **detect installed applications** (read-only: LibreOffice
    Writer/Calc, Evince, Okular, the desktop default handler — statuses
    `available` / `not_installed` / `installed_but_unsupported` /
    `detection_failed`)
  - **open an existing file with a registered application**
    (`open report.txt in Documents with LibreOffice Writer`, or `with
    the default application` via `xdg-open`) — approval required,
    argv-only launch, `shell=False`
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
- **Application integration engine** (`integrations/apps/`): a closed,
  schema-validated registry of application definitions — identifiers,
  capabilities, detection/launch/verification strategies. Applications
  are selected only by registered id; executables and command-line
  flags come exclusively from the reviewed builtin registry, never
  from a user, a file or the LLM. Detection is read-only (`PATH`
  lookups + desktop-entry file names in fixed directories — no
  process is ever spawned, installed, removed or updated), launching
  uses argv lists with `shell=False`, and success is reported
  honestly as *"the process was started"* — never as visual
  inspection, editing or formula recalculation.

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
- v0.3 application integration needs **no new Python packages**. To
  actually *open* files you need a desktop application registered in
  the builtin registry (LibreOffice Writer/Calc, Evince, Okular, or
  the `xdg-open` desktop default handler) — detection and all tests
  work without any of them; Alwin Fusion never installs applications.

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

# v0.3: read-only application detection (nothing is launched or installed)
python3 -m ai_core.cli "Which applications are installed?"
python3 -m ai_core.cli "Is LibreOffice Calc installed?"

# v0.3: open an existing file with a registered application (asks approval)
python3 -m ai_core.cli "Open summary.txt in Documents with LibreOffice Writer"
python3 -m ai_core.cli "Open sales.xlsx in Documents with LibreOffice Calc"
python3 -m ai_core.cli "Open summary.pdf in Documents with the default application"

# v0.3: see what a launch would do, without doing it (no approval prompt)
python3 -m ai_core.cli --dry-run "Open summary.txt in Documents with LibreOffice Writer"

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
`~/Documents` and never start a real desktop application (the launch
adapter's `popen`/`which` are injected fakes in tests). The suite
(316 tests) covers supported/unsupported commands, traversal and
symlink escapes, permission denial, approval rejection, directory
creation, verification failure, Ollama-unavailable operation, absence
of overwrite/delete capability, dry-run inertness, audit
redaction/permissions, the v0.2 document features (report rendering,
workbook creation with formula preservation, PDF structural
validation, content-source checks, lying/tampering handlers), and the
v0.3 application engine: registry schema validation, unknown
integration ids, arbitrary-executable/flag/shell rejection,
discovery statuses, discovery never spawning processes, argv-only
launches with `shell=False`, opening permitted files, outside-root
refusal, launch-failure reporting, verification failures, audit
fields for launches, and preservation of v0.1/v0.2 behaviour. The
spreadsheet/PDF pipeline tests skip themselves politely when the
optional package is not installed (the standard library validator
tests always run).

### Manual smoke test (opt-in, real LibreOffice)

For a developer who has LibreOffice installed and wants to watch one
real, personally approved launch:

```bash
ALWIN_MANUAL_SMOKE=1 scripts/manual_smoke_apps.sh
```

Without the environment variable the script is a no-op. It runs
detection first (read-only), then asks you to approve a single
`open ... with LibreOffice Writer` inside a throwaway sandbox home
(your real `~/Documents` is never touched) and prints the resulting
audit record.

## How the local model is used

The core works with **zero** AI runtime. With `--llm`, the pipeline
asks Ollama (local HTTP only) for a single JSON action; the response
goes through the same allowlist validation as the deterministic
parser — including application proposals, where `application` /
`integration` must be registered ids (a model cannot name an
executable or a command-line flag). If the server is down, the model
is missing, the timeout expires, or the response is invalid, you get
an explanatory note and the built-in parser handles the command
instead. Alwin Fusion never downloads models and contains no cloud
fallback.

## Project layout

```
ai_core/        config · command_parser · specs · planner · permissions ·
                executor · verifier · audit · pipeline · cli
ai_runtime/     ollama_client (stdlib HTTP, never pulls models)
integrations/   ActionDispatcher + filesystem tools + documents/
                (report · pdf · spreadsheet · tools) + apps/
                (registry · discovery · launch · tools — extension point)
tests/          unittest suite (temporary sandboxes only)
docs/           ARCHITECTURE.md · SECURITY.md
scripts/        run_tests.sh · manual_smoke_apps.sh (opt-in)
requirements.txt  optional v0.2 document dependencies (you install them)
app-bridge/ desktop/ models/ os-build/   reserved for later milestones
```

## Documentation

- `docs/ARCHITECTURE.md` — modules, data flow, action schema,
  configuration, testing, future extension guide.
- `docs/SECURITY.md` — security controls **and their known limits**.

## Status and known limitations

Implemented: the full parse → plan → permission → approval →
execute → verify → audit loop for all ten actions (including
`create_report`, `create_pdf`, `create_spreadsheet`,
`detect_applications`, `open_file`), CLI (dry-run, single-command and
interactive modes), structured-content validation, audit logging with
structured-content redaction, local Ollama client with graceful
degradation, the application registry/discovery/launch engine, and the
automated test suite.

Known limitations (details in `docs/SECURITY.md`):

- **Formulas are written as text and never calculated** — open a
  workbook in a spreadsheet application to compute results; Alwin
  Fusion never claims a calculated value.
- **PDFs are validated structurally, not byte-compared** (PDFs embed
  creation metadata), and text outside latin-1 is replaced (basic
  Helvetica fonts only — no CJK/emoji glyphs).
- Optional dependencies are loaded lazily; without `openpyxl` /
  `reportlab` those actions fail cleanly with an install hint.
- **A successful launch proves only that the process started** —
  Alwin Fusion never inspects, controls or automates the application
  window, never injects keystrokes, and never claims a document was
  visually reviewed or a spreadsheet recalculated.
- **Detection is heuristic** (PATH executables + desktop-entry file
  names in fixed directories); a broken install can be reported as
  `detection_failed`, and unknown viewers as `installed_but_unsupported`.
- Still deliberately absent: file deletion/overwrite, shell or
  arbitrary command execution, arbitrary application flags/commands,
  software installation, network operations, process sandboxing,
  multi-user support, MCP integrations, GUI accessibility
  automation, and bootable OS images.

Passing tests does not by itself make a system secure — read
`docs/SECURITY.md` for the actual threat model (TOCTOU races,
approval as last line of defense, non-tamper-proof audit log, and
more).

## License

Open-source project (license to be added with the first release
tag).
