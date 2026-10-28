# ALWIN FUSION

**Open-source, offline-first AI-native operating system — in progress.**

Version **0.5.0** · Milestone: **Task Experience Engine**

Alwin Fusion lets you control your computer through natural-language
commands. v0.5 adds the first user-facing **task experience** — a
lightweight offline GTK4 desktop interface (`taskui`) where you submit
a request, inspect the *real* plan the trusted core produced, approve
or reject it, watch truthful per-step execution with verification
evidence, and browse a persistent, validated, display-only local task
history. It builds directly on the unchanged trusted core (parse →
plan → permission → approval → execute → verify → audit); there is no
second execution engine. Fully offline, with an optional local Ollama
model that is never required and never auto-downloaded.

```
ALWIN FUSION v0.5
Local AI Core + Documents + Applications + Desktop Automation
Task experience: python3 -m taskui

You: inspect the state of LibreOffice Writer

ALWIN: libreoffice-writer: state 'installed_not_running'.
       installed; no running process observed

You: find the Save button in LibreOffice Writer

ALWIN: Control identified: role 'button', accessible name 'Save'.
       control: role 'button' name 'Save'
       state: control_identified
       Control identity verified against the validated locator
       (read-only operation; nothing was changed).

You: save the document in LibreOffice Writer to Documents/notes.txt

ALWIN: Proposed action:
       Save the active LibreOffice Writer document to
       ~/Documents/notes.txt (confirmed only if the file changes)
       Approve? [y/N] y

ALWIN: The Save control was activated, but ~/Documents/notes.txt did not
       change on disk within 2s. A save cannot be confirmed — state
       unknown (the active document may be a different file or
       unmodified).
```

The same core is now also driven by a desktop window:

```
$ python3 -m taskui
┌──────────────────────────────────────────────────────────────┐
│ ALWIN FUSION   [Local AI partial] [Automation available] [Works fully offline] │
│ What should Alwin do?  [ Create a folder named Invoices…    ] [Submit] │
│                                                              │
│ Task plan                                                    │
│ Planning…  ·  #4f2a9c                                        │
│ 1. Create directory 'Invoices' in Documents.    [Changes…]   │
│ • This task changes something on disk. Nothing runs until you │
│   approve this exact plan; if the plan changes, the approval  │
│   is invalidated.                                             │
│ [Approve this plan] [Reject]                                  │
│                                                              │
│ Execution                                                    │
│ ✔ Verified: Folder created and verified successfully.         │
│    ✔ path_exists · ✔ is_directory · ✔ not_a_symlink           │
│                                                              │
│ History (local, display-only)                                │
│ 4f2a9c  2026-10-27T18:02Z  Completed  Create a folder named…  │
└──────────────────────────────────────────────────────────────┘
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
  - **automate LibreOffice Writer/Calc** (v0.4): `inspect the state of
    LibreOffice Writer`, `find the Save button in LibreOffice Writer`,
    `read the text of the Title label in ...`, `activate the OK button
    in ...`, `enter "42" into the Search entry in ...`, `save the
    document in LibreOffice Writer to Documents/notes.txt` — read-only
    inspection/finding/reading, approval-gated mutations
  - **multi-step tasks** (v0.4): `create a folder named Batch in
    Documents then create a file named log.txt in Documents containing
    done` — every step planned, permission-checked and verified
    independently; the first failure stops the rest (the split only
    happens when every part is a real command, so `... containing buy
    milk then eggs` still means what it says)
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
- **Verification before success**: the verifier re-checks the world
  independently of what the executor claims — reports are re-rendered
  and compared byte-for-byte, PDFs are structurally validated,
  workbooks are **reopened** and compared cell-by-cell against the
  approved spec (including formula strings), automation state reports
  must be internally consistent with their evidence (a
  `document_open` claim without a live accessible connection fails
  verification), reported controls must match the validated locator,
  and a save only verifies when the file on disk matches the digest
  recorded after the Save control was activated. Failures report the
  real error.
- **Audit log** in `~/.local/state/alwin-fusion/audit.jsonl`,
  mode 0600/0700, with file content and structured `spec`/`content`
  redacted.
- **Task experience** (`taskui/`, v0.5) — a lightweight, offline GTK4
  desktop window (`python3 -m taskui`) on top of the *same* trusted
  core: type a request, see the **actual structured plan** the planner
  produced (annotated read-only / needs-approval / unverifiable — never
  a more permissive UI-specific variant), approve or reject it, watch
  live per-step progress, and inspect independently **verified**
  outcomes or honest failures. Clear separation: `gtk_app` (thin view)
  → `presenter` (pure view-model) → `manager` (one worker thread per
  task) → the existing `ai_core`/`integrations` pipeline. The window
  never executes anything itself, never grants itself permission, and
  the core re-checks permissions at execution time even after the UI
  approved. It stays useful with no AI and no desktop accessibility:
  the deterministic parser handles every supported command and the
  status area states honestly what is and is not available.
- **Persistent local task history** (`taskui/history.py`, v0.5) —
  `~/.local/state/alwin-fusion/task_history.jsonl` (mode 0600/0700),
  one sanitized record per finished task (id, timestamps, masked
  request, plan summary, per-step statuses, final result, verification
  status). It is **display-only by construction** (no code path from a
  record to execution, no automatic replay), validated against a
  closed schema on read, and malformed records are skipped and counted
  — never repaired or executed. It is a normal user-owned file, not
  tamper-proof.
- **Two-phase pipeline** (`preview()` + `run_previewed()`,
  `ai_core/pipeline.py`) — the additive split the desktop UI needs:
  `preview()` runs parse → plan → permission and **never executes**;
  `run_previewed()` continues the *same* validated action through
  approval → execute → verify → audit. `run()` is now their
  composition (one implementation of every stage). Approvals are bound
  to a `plan_digest()` via an `ApprovalToken`; a changed plan is an
  honest rejection with nothing executed. A bare `True` approval is
  deliberately not accepted.
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
- **Desktop automation engine** (`integrations/automation/`, v0.4):
  a safe AT-SPI layer for LibreOffice Writer and Calc with a **closed
  action set** — `inspect_application`, `find_control`, `read_control`
  (read-only, no approval), and `activate_control`, `enter_text`,
  `save_document`, `task_sequence` (approval required). Every operation
  reports one of a fixed set of honest application states
  (`not_installed` → `installed_not_running` → `process_started` →
  `interface_detected`/`document_open` → `control_identified` →
  `action_performed` → `result_verified`, or `failed_or_unknown`): a
  started process is never claimed to be a visible window, and a
  Save-button activation is never claimed to be a save — the file's
  sha256 must actually change, and the verifier re-reads the file
  independently. Text entry is confined to input fields (role `entry`)
  and is confirmed by reading the field back; document bodies are
  never typed into. Multi-step tasks (`... then ...`) validate every
  step independently, re-check permissions per step, stop at the first
  failure, skip dependents, and are re-verified step by step.
- **No synthetic input, ever**: the automation layer contains no
  keystroke injection, no mouse/coordinate events, no shell — the only
  mutation primitives are the accessibility *action* interface of an
  identified control and `EditableText` on an identified input field
  (pinned by source-level meta-bans in the test suite).
- **Untrusted application text**: accessible names, window titles and
  control text are sanitized before display and never take part in a
  permission, policy or approval decision.

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
- v0.4 desktop automation additionally needs (installed by **you** —
  Alwin Fusion never installs anything):

  | Dependency | Enables | If missing |
  |---|---|---|
  | `python3-gi` + `gir1.2-atspi-2.0` (`gi.repository.Atspi`) | the AT-SPI backend | clear "capability unavailable" errors; everything else keeps working |
  | a running AT-SPI bus (`org.a11y.Bus`, normally present in any logged-in desktop session) | live interface access | honest `state unknown` reports |
  | LibreOffice Writer or Calc **exposing its accessibility interface** | inspect/find/read/activate/enter/save | honest `process_started` / "no accessible interface" reports |

  **Desktop accessibility must be enabled for the application to be
  visible on the bus.** On Ubuntu GNOME this means GNOME Settings →
  Accessibility (or `gsettings set org.gnome.desktop.interface
  toolkit-accessibility true` — *you* run this; Alwin Fusion never
  changes desktop settings). Observed on the development machine
  (Ubuntu 26.04.1, GNOME on Wayland): the AT-SPI bus and registry are
  live and GNOME apps (Nautilus, gnome-shell) expose interfaces, but
  with `toolkit-accessibility=false` LibreOffice 26.2.6.3 does *not*
  register — ALWIN then reports `process_started` and refuses to fake
  a window it cannot see. The core and all tests work without any of
  these; the automated suite never touches a real desktop.
- v0.5 desktop task experience (`python3 -m taskui`) additionally
  needs GTK4 via PyGObject. On the development machine this was
  already present as part of the v0.4 automation dependency, so it
  adds **no new Python package** to `requirements.txt`:

  | Dependency | Enables | If missing |
  |---|---|---|
  | `python3-gi` + `gir1.2-gtk-4.0` (`gi.repository.Gtk` 4.0) | the task window | `python3 -m taskui --check` still works; launching the window prints an honest message and the CLI keeps working |

  The rest of the task experience (lifecycle, manager, presenter,
  history, status probes) is pure standard library and runs headless.
  Alwin Fusion never installs any of these.

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

# v0.4: read-only desktop automation (no approval, nothing mutated)
python3 -m ai_core.cli "Inspect the state of LibreOffice Writer"
python3 -m ai_core.cli "Find the Save button in LibreOffice Writer"
python3 -m ai_core.cli "Read the text of the Title label in LibreOffice Writer"

# v0.4: approval-gated automation mutations
python3 -m ai_core.cli "Activate the OK button in LibreOffice Calc"
python3 -m ai_core.cli 'Enter "42" into the Search entry in LibreOffice Calc'
python3 -m ai_core.cli "Save the document in LibreOffice Writer to Documents/notes.txt"

# v0.4: multi-step task (approved once, every step verified)
python3 -m ai_core.cli "Create a folder named Batch in Documents then create a file named log.txt in Documents containing done"

# v0.4: see the whole sequence without doing anything
python3 -m ai_core.cli --dry-run "Inspect the state of LibreOffice Writer then find the Save button in LibreOffice Writer"

# Show effective configuration / probe the local AI runtime
python3 -m ai_core.cli --show-config
python3 -m ai_core.cli --ai-status

# v0.5: the desktop task experience (GTK4 window)
python3 -m taskui                 # submit a request, inspect the plan,
                                  # approve/reject, watch verified progress
python3 -m taskui --llm           # use the local Ollama model for proposals
python3 -m taskui --dry-run       # every task runs as a dry run
python3 -m taskui --check         # headless self-check (no window): prints
                                  # AI/automation status + history location
python3 -m taskui --smoke         # scripted headless walkthrough of the
                                  # full lifecycle (used by the UI smoke test)

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
`~/Documents` and never start or touch a real desktop application (the
launch adapter's `popen`/`which`, the automation backend's AT-SPI
session, adapter factory, clock and `/proc` root are all injected
fakes; fake `/proc` trees are temporary directories of `<pid>/comm`
files). The suite (553 tests) covers supported/unsupported commands,
traversal and symlink escapes, permission denial, approval rejection,
directory creation, verification failure, Ollama-unavailable operation,
absence of overwrite/delete capability, dry-run inertness,
audit redaction/permissions, the v0.2 document features (report
rendering, workbook creation with formula preservation, PDF structural
validation, content-source checks, lying/tampering handlers), the
v0.3 application engine (registry schema validation, unknown
integration ids, arbitrary-executable/flag/shell rejection, discovery
statuses, argv-only launches, opening permitted files, outside-root
refusal, launch-failure reporting, verification failures, audit
fields), the v0.4 automation engine: unknown action/role/name
rejections, capability-unavailable reporting, permission denial for
applications without the `automate` capability, the honest state
machine (`not_installed`/`installed_not_running`/`process_started`
never upgraded to a window or document claim), control find/read with
bounds, approval and dry-run inertness for mutations, revalidation and
stale-control refusal, dead-bus/dead-control reporting, digest-confirmed
saves and lying-save verification failures, dishonest adapters failing
verification, untrusted application text sanitization, audit redaction
of entered text, per-step sequence verification with stop-on-failure
and skipped dependents, source-level bans on keystroke/mouse/shell
primitives in the automation package, and preservation of
v0.1/v0.2/v0.3 behaviour. The v0.5 task experience is covered by
headless tests only (no window, no display, no desktop): the task
lifecycle state machine (valid/invalid transitions, cancellation,
failure, needs-attention), the two-phase pipeline (`preview()` +
`run_previewed()` parity with `run()`, approval-token digest binding,
plan-changed invalidation, permission re-check at execution time),
the task manager (submission validation, read-only and approval flows,
rejection cancelling with nothing executed, cancellation refused while
running, execution-failure → failed, verification-failure →
needs_attention never success, live per-step progress, history writes
including for failures, and isolation from UI bugs), the history store
(roundtrip, corruption/malformed-record skipping, content redaction,
display-only/never-executable guarantee), the capability-status probes
(mocked AI/automation availability), the presenter view-model (plan
badges, approval affordances, step rendering, verification evidence,
bounded untrusted text), and AST-based source meta-checks (no
subprocess/os.system/eval/exec anywhere in `taskui/`, only the manager
imports the execution engine, the GTK view never renders markup). The
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

### Manual automation smoke test (opt-in, real LibreOffice + AT-SPI)

For a developer who wants to watch ALWIN drive a *real* LibreOffice
window over the accessibility bus, approving every mutation:

```bash
ALWIN_AUTOMATION_SMOKE=1 scripts/manual_smoke_automation.sh
```

The script explains every step before running it. It runs read-only
state inspection first, then an approval-gated launch of a sandbox
file, re-inspection, find/read, an approval-gated controlled-save
attempt (whose *expected* outcome on an unmodified document is an
honest "state unknown" failure — the digest rule working), and a
two-step task sequence. Everything runs inside a throwaway sandbox
home — your real `~/Documents` and your real LibreOffice profile are
never touched (the sandbox is deleted on exit). If the desktop does
not expose LibreOffice's accessibility interface, every step fails
honestly instead of guessing; see Requirements above.

### Manual task-experience smoke test (opt-in, real task window)

For a developer who wants to confirm the whole v0.5 task experience
works on a real display — the headless lifecycle walkthrough first,
then the actual GTK4 window you can drive yourself:

```bash
ALWIN_UI_SMOKE=1 scripts/manual_smoke_ui.sh
```

Without the environment variable the script is a no-op. It runs the
headless walkthrough (`python3 -m taskui --smoke`) against the real
manager and trusted pipeline inside a throwaway sandbox home — a
read-only task, then a mutating task you see *rejected* (nothing runs),
then the same task *approved* (created and verified on disk) — prints
the headless capability check and the history it wrote, and finally
opens the real GTK4 window so you can submit a task, inspect the plan,
approve or reject it, and inspect the verified result and history.
Everything runs inside the sandbox; your real `~/Documents` and state
directory are never touched, and the sandbox is deleted on exit. If
GTK4 or a display is unavailable the window step is skipped with an
honest message — the headless walkthrough still proves the lifecycle.

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
                executor · verifier · audit · pipeline (preview +
                run_previewed + ApprovalToken) · cli
ai_runtime/     ollama_client (stdlib HTTP, never pulls models)
integrations/   ActionDispatcher + filesystem tools + documents/
                (report · pdf · spreadsheet · tools) + apps/
                (registry · discovery · launch · tools) + automation/
                (base · processes · atspi_backend · libreoffice ·
                 tools · sequence — with optional step_observer ·
                 mcp — extension point)
taskui/         lifecycle (state machine) · history (validated,
                display-only JSONL) · manager (worker-thread bridge to
                the pipeline) · presenter (pure view-model) · status
                (honest AI/automation probes) · gtk_app (thin GTK4
                view) · __main__ entry point
tests/          unittest suite (temporary sandboxes + in-memory
                accessibility trees only; no display, no desktop)
docs/           ARCHITECTURE.md · SECURITY.md
scripts/        run_tests.sh · manual_smoke_apps.sh ·
                manual_smoke_automation.sh · manual_smoke_ui.sh (opt-in)
requirements.txt  optional v0.2 document dependencies (you install them)
app-bridge/ desktop/ models/ os-build/   reserved for later milestones
```

## Documentation

- `docs/ARCHITECTURE.md` — modules, data flow, action schema,
  configuration, testing, future extension guide.
- `docs/SECURITY.md` — security controls **and their known limits**.

## Status and known limitations

Implemented: the full parse → plan → permission → approval →
execute → verify → audit loop for all seventeen actions (including
`create_report`, `create_pdf`, `create_spreadsheet`,
`detect_applications`, `open_file`, and the seven v0.4 automation
actions), CLI (dry-run, single-command and interactive modes),
structured-content validation, audit logging with structured-content
redaction, local Ollama client with graceful degradation, the
application registry/discovery/launch engine, the AT-SPI desktop
automation engine for LibreOffice Writer/Calc (honest state machine,
control finding/reading, allowlisted activation and text entry,
digest-confirmed saves, validated multi-step sequences), and the
automated test suite (553 tests). v0.5 adds the **task experience**:
a two-phase pipeline (`preview()` + `run_previewed()` with
digest-bound `ApprovalToken`s), the `taskui` package (validated task
lifecycle, redacted display-only task history, worker-thread manager,
pure presenter, honest capability probes, thin GTK4 window), and the
opt-in UI smoke test.

Known limitations (details in `docs/SECURITY.md`):

- **The v0.5 task window is a thin view over the existing core** — it
  exposes only the seventeen supported actions; there is no
  document-editing or free-form "do anything" capability, and an
  unsupported request is explained honestly with safe supported
  alternatives shown.
- **The task window requires GTK4 (PyGObject)**. It was exercised on
  the development machine (Ubuntu 26.04.1, GNOME/Wayland) via the
  opt-in smoke test; a clean installation has **not** been tested, so
  no clean-install claim is made.
- **Task history is a normal user-owned file** (`task_history.jsonl`,
  mode 0600/0700) — it is validated and display-only, but it is **not
  tamper-proof**, and nothing in Alwin Fusion ever replays it.
- **One task at a time** — the manager runs a single worker thread and
  refuses a second submission until the current task reaches a
  terminal state (a deliberate simplicity, not a queue).
- **Cancellation is honoured only before execution starts.** A running
  action is never interrupted, and a cancelled task never claims
  rollback (nothing is "undone" — nothing had run yet).
- **Formulas are written as text and never calculated** — open a
  workbook in a spreadsheet application to compute results; Alwin
  Fusion never claims a calculated value.
- **PDFs are validated structurally, not byte-compared** (PDFs embed
  creation metadata), and text outside latin-1 is replaced (basic
  Helvetica fonts only — no CJK/emoji glyphs).
- Optional dependencies are loaded lazily; without `openpyxl` /
  `reportlab` those actions fail cleanly with an install hint; without
  `gi`/AT-SPI the automation backend reports "capability unavailable"
  honestly and nothing fakes it.
- **Automation needs the application to expose accessibility.** If the
  desktop keeps assistive technology disabled (e.g. GNOME
  `toolkit-accessibility=false`), LibreOffice will not appear on the
  AT-SPI bus and every control operation honestly reports
  `process_started` / "state unknown". Alwin Fusion never enables this
  setting itself.
- **v0.4 automates LibreOffice Writer and Calc only** (via AT-SPI);
  other registry applications are denied by the permission stage, and
  there is deliberately no generic "automate anything" path.
- **Text entry targets input fields only** (role `entry`, ≤ 4096
  chars, confirmed by read-back); document bodies are never edited
  through GUI automation — content creation stays with the trusted
  document generators.
- **A save is reported only when the file digest actually changes**
  (≤ 2s confirmation window); an activation that produces no visible
  file change is an honest failure, not a silent success.
- **A successful launch still proves only that the process started** —
  that claim is separate from (and never upgraded into) automation
  states.
- **Detection is heuristic** (PATH executables + desktop-entry file
  names in fixed directories); a broken install can be reported as
  `detection_failed`, and unknown viewers as `installed_but_unsupported`.
- **No MCP server exists** — v0.4 ships future-MCP-compatible tool
  *descriptors* (capabilities, schemas, permissions, timeouts, result
  rules) only; `integrations/automation/mcp.py` is a manifest, not a
  transport.
- Still deliberately absent: file deletion/overwrite, shell or
  arbitrary command execution, keystroke/mouse synthesis, arbitrary
  application flags/commands, software installation, network
  operations, process sandboxing, multi-user support, UNO-based
  LibreOffice control (planned; the unauthenticated UNO socket was
  deliberately not opened in v0.4), and bootable OS images.

Passing tests does not by itself make a system secure — read
`docs/SECURITY.md` for the actual threat model (TOCTOU races,
approval as last line of defense, non-tamper-proof audit log, and
more).

## License

Open-source project (license to be added with the first release
tag).
