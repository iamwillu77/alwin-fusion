# Alwin Fusion — Architecture

Version: 0.4.0 — Milestone: Desktop Automation Engine

Alwin Fusion is an open-source, offline-first AI-native operating
system in the making. This document describes what exists today, how
the pieces fit together, and where future features plug in.

## Design principles

1. **Trusted core, untrusted input.** Natural-language text and local
   model output are untrusted. Only trusted Python code decides what
   executes, against an explicit action allowlist.
2. **Offline by default.** The system is fully useful without any LLM.
   The local Ollama runtime is optional and never replaced by a cloud
   service.
3. **Least privilege.** Filesystem access is confined to explicitly
   configured roots inside the user's home directory. No shell, no
   `eval`, no code generation from model output.
4. **Honest results.** Success is reported only after independent
   verification. Failures return the real error.
5. **Clear module boundaries** so the prototype can grow into a full
   Linux-based operating system (see *Future compatibility*).

## Package layout

```
ai_core/                 Trusted application core (pure Python, stdlib only)
    __init__.py          Version marker
    config.py            Configuration from env/args, validated roots
    command_parser.py    Deterministic natural-language -> ParsedIntent
    specs.py             Structured content validation (reports, specs,
                         formulas) — closed schemas, bounded sizes
    planner.py           Intent/model proposal -> validated Action (allowlist)
    permissions.py       Path resolution, containment, symlink/traversal gates
    executor.py          Runs validated actions via the dispatcher
    verifier.py          Re-checks the world after execution
    audit.py             Redacting, restricted-permission JSONL audit log
    pipeline.py          Stage orchestration: parse -> plan -> permission
                         -> approval -> execute -> verify -> audit.
                         v0.5 split into two-phase: preview() (stages
                         1-3, never executes, not audited) +
                         run_previewed() (stages 4-7 on the SAME
                         validated action); run() = composition of the
                         two (one implementation per stage).  Adds
                         plan_digest() + ApprovalToken (approval bound
                         to one exact plan) and an optional progress
                         callback ("executing"/"verifying").
    cli.py               `python3 -m ai_core.cli` interface
ai_runtime/              Local AI runtime integration
    ollama_client.py     stdlib-only Ollama HTTP client (never pulls models)
integrations/            Pluggable action handlers (the extension point)
    __init__.py          ActionDispatcher + build_default_dispatcher()
    filesystem/tools.py  list/read/create-only filesystem tools
    documents/           v0.2 document generation
        deps.py          lazy optional-dependency loading + clear errors
        report.py        deterministic plain-text rendering (stdlib)
        pdf.py           reportlab rendering + stdlib PDF validation
        spreadsheet.py   openpyxl workbook build + reopen/inspect
        tools.py         O_EXCL create-only handlers re-checking security
    apps/                v0.3 application integration engine
        registry.py      closed, schema-validated integration definitions
        discovery.py     read-only detection (PATH + desktop-entry names)
        launch.py        argv-only LaunchAdapter (shell=False, one {path})
        tools.py         detect/open_file handlers re-checking permissions
    automation/          v0.4 desktop automation engine (AT-SPI)
        base.py          state machine, control locators, capabilities,
                         role policies, bounds, untrusted-text sanitizing
        processes.py     read-only /proc observation (never starts processes)
        atspi_backend.py AtspiSession over gi.repository.Atspi (no gi = clean
                         BackendUnavailable; no keystrokes/mouse/shell ever)
        libreoffice.py   LibreOfficeAdapter: Writer/Calc root discovery,
                         find/read/activate/enter/save with revalidation
        tools.py         the 7 automation handlers re-checking permissions
        sequence.py      TaskSequenceRunner (stop-on-failure, per-step
                         records; optional step_observer for live progress)
        mcp.py           future-MCP tool descriptors only (no server/transport)
taskui/                  v0.5 task experience (UI-agnostic core + thin view)
    lifecycle.py         the task state machine (10 states) with a
                         validated transition table; TaskRecord (curated,
                         display-safe fields; the live pipeline preview
                         is kept out of serialization)
    history.py           TaskHistory: persistent, validated, display-only
                         JSONL task history (<state_dir>/task_history.jsonl,
                         0600/0700); schema-validated on read; malformed
                         records skipped and counted, never executed
    manager.py           TaskManager: the ONLY bridge between a UI and
                         the trusted pipeline; one worker thread per task;
                         approval gate blocks on an event the UI releases
                         via approve()/reject()/cancel(); never executes
                         anything itself
    presenter.py         pure, headless view-model (no GTK, no I/O):
                         submission validation, plan badges, risk notes,
                         step rendering, verification evidence, history
                         rows/detail, status labels, bounded untrusted text
    status.py            honest capability probes (AI runtime reachable?
                         model configured/installed? automation bindings
                         importable? offline posture) — read-only, injectable
    gtk_app.py           thin GTK4 (PyGObject) view: renders presenter
                         dicts, forwards intents to the manager, marshals
                         worker-thread updates with GLib.idle_add; inserts
                         ALL dynamic text as plain text (never markup)
    __main__.py          `python3 -m taskui` entry point (--llm, --dry-run,
                         --check headless self-check, --smoke scripted
                         headless lifecycle walkthrough)
tests/                   unittest suite (temporary sandboxes + in-memory
                         accessibility trees only)
docs/                    ARCHITECTURE.md, SECURITY.md
scripts/run_tests.sh     Test entry point
scripts/manual_smoke_apps.sh  opt-in real-launch smoke test (ALWIN_MANUAL_SMOKE=1)
scripts/manual_smoke_automation.sh  opt-in real-AT-SPI automation smoke test
                         (ALWIN_AUTOMATION_SMOKE=1)
requirements.txt         optional v0.2 document dependencies
```

Note on naming: the original scaffold used `ai-core/` and
`ai-runtime/`; they were renamed to `ai_core/` and `ai_runtime/`
because Python cannot import hyphenated package names — the required
entry point `python3 -m ai_core.cli` depends on it.

## Data flow

```
 user text
    |
    v
command_parser.parse_command()          deterministic, offline
    |  ParsedIntent {intent, slots}
    v
planner.plan_from_intent()              (+ optional plan_via_llm())
    |  content_loader(source.json)          permission-checked JSON read,
    |  Action dict  -- validated by          512 KiB limit, strict UTF-8
    |  planner.validate_action()             (+ ai_core.specs content rules)
    v
permissions.PermissionChecker           path resolved, realpath applied,
    |  PermissionDecision               containment + symlink checks
    v
approval callback (CLI: interactive y/N)   required for mutating actions
    v
executor.execute()  -> ActionDispatcher -> integrations.filesystem.tools
    |  ExecutionResult (real errors, no fakes)   or integrations.documents.tools
    v                                              or integrations.apps.tools
verifier.verify()   -> re-resolves paths, re-checks the filesystem   or integrations.automation.tools
    |  VerificationResult {passed|failed|skipped}  (re-renders reports,
    v                                              reopens workbooks, validates PDFs,
    |                                              re-resolves open_file paths and
    |                                              requires a reported process start;
    |                                              automation: state claims must match
    |                                              their evidence, reported controls
    |                                              must match validated locators,
    |                                              saves must match disk digests,
    |                                              sequence steps re-verified one by one)
audit.record()      -> <state_dir>/audit.jsonl (0600, content redacted)
    v
cli.print_report()  -> human-readable outcome + exit code
```

Each stage is a separate object with a single responsibility, so a
future GUI or MCP server can drive the same `TaskPipeline`.

### Two-phase pipeline (v0.5)

The desktop task experience needs to *show* a plan before anything
runs, then continue the *same* validated action once the user decides.
`TaskPipeline` therefore exposes the loop as two composable methods —
with exactly one implementation of every stage:

```
 preview(command)  ->  TaskPreview                      (stages 1-3)
   parse -> plan -> permission check
   * NEVER executes
   * NOT audited (nothing was attempted)
   carries: the validated action, resolved target, proposal text,
            requires_approval, plan_digest, and the shared TaskReport

 run_previewed(preview, approval=..., progress=...)     (stages 4-7)
   re-check permissions at execution time (defence in depth — the
     world may have changed while the user was deciding)
   approval  (None = interactive approver | ApprovalToken | False)
   execute -> verify -> audit (one audit record per attempt)
   progress("executing" / "verifying") for live status
```

`run(command)` is now simply
`run_previewed(preview(command))` — the CLI path is unchanged and its
behaviour is pinned by parity tests.

**Approval binds to one exact plan.** `plan_digest(action)` is a
sha256 of the canonical (sorted-key) JSON of the validated action,
truncated to 16 hex chars. An `ApprovalToken(digest)` handed to
`run_previewed` is honoured only while the digest of the action about
to execute still equals the approved one; otherwise the run is an
honest rejection (`approval="invalidated"`, nothing executed). A bare
`True` is deliberately *not* an approval mechanism — approval must be
bound to a plan, never granted to "whatever comes next".

## Task experience (taskui/, v0.5)

The task experience is the first user-facing surface. It sits **on
top of** the trusted core and adds no second execution engine, no
second permission system and no direct execution of anything.

```
 gtk_app.py       thin GTK4 view: renders presenter dicts, forwards
    |             user intents (submit/approve/reject/cancel/history),
    |             marshals worker-thread updates via GLib.idle_add.
    |             Executes nothing; inserts all dynamic text as plain
    |             text (never Pango markup).
    v
 presenter.py     pure view-model (headless, fully unit-tested):
    |             submission validation, plan rows + badges, risk
    |             notes, step rendering, verification evidence,
    |             history rows/detail, status labels, honest
    |             suggestions for unsupported requests.
    v
 manager.py       TaskManager — one worker thread per task.  Calls
    |             pipeline.preview(), shows the plan, blocks on an
    |             approval Event the UI releases via approve() (with
    |             the displayed digest) / reject() / cancel(), then
    |             pipeline.run_previewed(ApprovalToken(digest)) with
    |             live progress transitions.  Maps the report onto the
    |             lifecycle honestly and writes task history.
    v
 ai_core.pipeline + integrations  (the existing trusted core —
    unchanged in responsibility: it still owns permissions, approval,
    execution, verification and audit)
```

**Task lifecycle** (`taskui/lifecycle.py`). Ten states, one validated
transition table; an impossible jump raises `InvalidTransition`:

```
 draft ──> planning ──┬──> awaiting_approval ──> approved ──> executing
                      │                                │          │
                      ├──> executing (read-only/dry)   │          v
                      ├──> failed (unsupported/denied) │      verifying
                      └──> cancelled                   │          │
                             ^                         │          ├──> completed
                             └─────────────────────────┴──────────┤
                                                                  ├──> failed
                                                       cancelled <┘
                                                       needs_attention
```

Honesty rules encoded in the graph: *completed* is reachable **only
from verifying** (never straight from executing — dispatching an
action does not complete a task); *cancelled* is only reachable
**before execution starts** (a running action is never interrupted and
no rollback is ever claimed); *needs_attention* is the honest state for
"the world may have changed but the result could not be fully
verified" (failed verification after a successful execution, or a
partially applied sequence).

**Local task history** (`taskui/history.py`). One sanitized JSON line
per finished task in `<state_dir>/task_history.jsonl` (0600 file in a
0700 directory, beside the audit log). Each record carries the task
id, timestamps, the **masked** request (the pipeline's own
`_mask_command` replaces user-supplied file content with a length
marker), a curated plan summary, per-step statuses, the final result
and the verification status — reusing the audit log's redaction, never
duplicating sensitive content. It is **display-only by construction**:
the module never imports the executor, planner, dispatcher or
sequence runner (a single documented local import of the pipeline's
*masking helper* is the only pipeline reference), there is no function
that can execute, re-run or interpret a record, and the manager exposes
no "run this history entry" path. Records are validated against a
closed schema on read; malformed, unknown-version or hostile records
are **skipped and counted** — never repaired, never executed, and never
able to become instructions. It is a normal user-owned file, explicitly
**not** claimed to be tamper-proof.

**Honest capability status** (`taskui/status.py`). Read-only probes,
all injectable for tests: is the local Ollama runtime reachable, is a
model configured and installed (distinct honest answers, reusing the
typed `OllamaError`s; an unexpected bug surfaces rather than being
masked as "offline")? Are the AT-SPI bindings importable (an *import*
check only — probing never opens a live desktop session)? Offline is
stated as a fact about the design (the only network client in the
codebase talks to a local Ollama server), not as a network probe. When
AI is unavailable the deterministic parser keeps working and the status
says so.

## The action model

`ai_core/planner.py` owns the only allowlist:

| Action                | Fields                    | Mutates | Approval |
|-----------------------|---------------------------|---------|----------|
| `create_directory`    | `path`                    | yes     | yes      |
| `create_text_file`    | `path`, `content`         | yes     | yes      |
| `create_report`       | `path` (.txt), `content`  | yes     | yes      |
| `create_pdf`          | `path` (.pdf), `content`  | yes     | yes      |
| `create_spreadsheet`  | `path` (.xlsx), `spec`    | yes     | yes      |
| `list_directory`      | `path`                    | no      | no       |
| `read_file`           | `path`                    | no      | no       |
| `detect_applications` | `integration` (id or `""`) | no     | no       |
| `open_file`           | `path`, `application` (id) | yes    | yes      |
| `inspect_application` | `application` (id)         | no      | no       |
| `find_control`        | `application`, `control`   | no      | no       |
| `read_control`        | `application`, `control`   | no      | no       |
| `activate_control`    | `application`, `control`   | yes     | yes      |
| `enter_text`          | `application`, `control`, `content` | yes | yes |
| `save_document`       | `application`, `path` (.txt/.xlsx) | yes | yes   |
| `task_sequence`       | `steps` (2–8 validated actions) | yes | yes      |
| `explain`             | `target` (nested action)  | no      | no       |

There is **no** delete, overwrite, move, shell, network or
arbitrary-application-command action in this milestone, by
construction. Path strings are validated (length, null bytes, `..`)
before the permission checker even runs.

The two v0.3 application actions carry *identifiers*, never commands:
`application` and `integration` are resolved against the closed builtin
registry (`integrations/apps/registry.py`) during validation — an
unknown token (including any executable path or flag string such as
`/usr/bin/soffice` or `soffice --calc`) is rejected with the list of
known ids. `open_file` accepts only `.txt`/`.pdf`/`.xlsx` (further
narrowed per integration by its `file_types`), requires an already
existing file inside the permitted roots, and is in `MUTATING_ACTIONS`
so every launch needs explicit approval. `detect_applications` is
read-only and never requires approval.

The seven v0.4 automation actions carry *identifiers and locators*,
never commands or coordinates: `application` is again a closed-registry
id, and `control` is a `{role, name}` locator validated against a
closed 24-role vocabulary (`KNOWN_ROLES`) — unknown roles, unknown
fields (including any `x`/`y` coordinates) and over-long names are
rejected at planning time. `read_control`/`activate_control` require an
exact control name; `find_control` may search by role alone.
`enter_text` content is bounded (≤ 4096 chars, no null bytes) and the
automation layer further restricts typing to input fields (role
`entry`). `save_document` accepts only the allowlisted extensions
(`.txt`, `.xlsx`) and is digest-confirmed. `task_sequence` embeds 2–8
*individually validated* actions (no nesting), so model output can
never smuggle an unvalidated step inside a sequence. The three
read-only automation actions are not in `MUTATING_ACTIONS`; the three
automation mutations plus `task_sequence` are.

The three v0.2 document actions carry *structured* payloads instead of
free text: `content` is a report/PDF document (`{"title": ..., "blocks":
[heading|paragraph|list]}`) and `spec` is a workbook specification
(sheets, headers, rows, formulas, number formats, column widths,
styles). Both are validated field-by-field in `ai_core/specs.py`
before and after planning — closed key sets, bounded sizes, scalar
cells only, and a conservative spreadsheet formula grammar
(allow-listed functions; no external-workbook links, cross-sheet
references or references outside the declared grid). Generated files
have fixed extensions (`.txt`/`.pdf`/`.xlsx`); a wrong extension or
unsafe file name is rejected. The model-proposal path receives no
`source` key, so it can never trigger a file read from model output.

Paths in actions are **relative to the user's home directory**
(e.g. `Documents/Business Reports`); `.` means the primary permitted
root. Absolute paths are accepted syntactically but must resolve
inside a permitted root to pass the permission stage.

## Application integration engine (integrations/apps/)

The engine is deliberately separate from NL parsing, model inference,
permission decisions, filesystem tools and document generation: it
consumes a *validated* action and nothing else.

**Registry (`registry.py`).** An integration definition is reviewed
data in this repository: stable id, human name, semantic version,
aliases, capabilities (`open_file` only for now), file types,
detection strategy (executable names + desktop-entry ids), launch
strategy (executable, argv template with exactly one `{path}`,
optional fallback), verification strategy (`process_started` only),
and required permissions. `validate_integration()` enforces a closed
schema — unknown keys, unknown capabilities/strategies, shell
metacharacters, absolute or slashed executables, and multiple `{path}`
tokens are all rejected. There is intentionally **no** manifest
loading from disk or model output in this milestone; the builtin
registry is the only source, and identifiers are resolved against it
(`resolve_integration_id`, case/separator-insensitive).

**Discovery (`discovery.py`).** Read-only: `shutil.which` for the
registered executable names, and the *file names* (never contents) of
`.desktop` entries in three fixed directories (`/usr/share/applications`,
`/usr/local/share/applications`, `~/.local/share/applications`).
Desktop names are sanitized to a conservative character set, listings
are bounded, and filesystem errors surface as `detection_failed`.
Statuses: `available` (launch executable resolves), `not_installed`
(no signal), `installed_but_unsupported` (known desktop entry with no
integration, e.g. LibreOffice Impress, atril, zathura),
`detection_failed` (desktop entry but no executable — a broken install,
reported honestly rather than as available). Nothing is installed,
removed, updated or launched during detection.

**Launch (`launch.py`).** `LaunchAdapter` builds argv from the
registry template — `[resolved_executable] + flags + [file_path]`,
with the path as its own element — and calls `Popen` with an explicit
`shell=False`, `close_fds=True`, `start_new_session=True` and
`/dev/null` streams. The executable is resolved through PATH at launch
time; missing executable = clear `LaunchError` ("never installs
automatically"), immediate non-zero exit = `LaunchError`, exit 0 =
clean handoff (typical of `xdg-open`). `popen`/`which` are injectable,
so tests never start a real process.

**Verification honesty.** For `open_file` the verifier re-resolves the
path (still inside the roots, still a file) and requires the
execution result to report a real process start; the success message
states that only the launch was verified. No claim about window
contents, document editing or formula recalculation is ever made, and
no GUI automation or keystroke injection exists.

**Future extension seams.** The registry's closed
launch/verification-strategy enums are where an MCP-backed or
accessibility-API adapter would plug in (as new *reviewed* strategy
values with their own trusted adapter code); an untrusted manifest
still cannot introduce one.

## Desktop automation engine (integrations/automation/)

v0.4 adds a safe, AT-SPI-based automation layer for LibreOffice Writer
and Calc. Like the apps engine, it is deliberately separate from NL
parsing, model inference, discovery, permissions, filesystem tools and
document generation: it consumes a *validated* action and nothing
else.

**Honest state machine (`base.py`).** Every operation reports one of a
fixed set of application states:
`not_installed` → `installed_not_running` → `launch_requested` /
`process_started` → `interface_detected` / `document_open` →
`control_identified` → `action_performed` → `result_verified`, or
`failed_or_unknown`. The rules are enforced, not aspirational: a
started process is never reported as a visible window, an interface
detection is never reported as an open document, and a Save-control
activation is never reported as a save. `document_open` requires a
live accessible connection *and* a window title matching the module
(e.g. contains "LibreOffice Writer"); anything weaker reports
`interface_detected` with `document_reliable: false`.

**Capabilities and backend seam (`base.py`, `atspi_backend.py`).**
Each adapter declares a capability set (`inspect`, `find`, `read`,
`activate`, `enter`, `save`); a missing capability or missing binding
(`gi.repository.Atspi`) produces a clean
`unavailable`/`capability_unavailable` report — never a fake. The real
backend is `AtspiSession` over AT-SPI: it contains no keystroke
injection, no mouse/coordinate events and no shell — the only mutation
primitives are the accessibility *action* interface of an
already-identified control and `EditableText.set_text_contents` on an
already-identified input field (pinned by source-level meta-bans in
the test suite). Every external dependency (AT-SPI module, session,
adapter factory, `which`, `/proc` root, clock, sleep) is injectable,
so the automated tests run the real code against an in-memory tree.

**Process observation (`processes.py`).** Read-only `/proc` scanning
(`*/comm` files) distinguishes "installed but not running" from
"running". An unreadable `/proc` yields *unknown*, never a guess, and
the inspection never launches anything.

**LibreOffice adapter (`libreoffice.py`).** Discovers the Writer/Calc
application root on the bus, walks the tree with depth/node bounds,
finds controls by canonical role + accessible name (case/whitespace
insensitive), reads permitted roles' text (bounded, truncated marks),
activates identified controls, enters text into `entry` fields with a
read-back confirmation, and performs the digest-confirmed save. Before
every mutation the locator is **revalidated** against a freshly walked
tree — if the control changed identity or vanished, the action aborts
with `failed_or_unknown` instead of acting on the wrong control.

**Digest-confirmed save.** The tools layer records the target file's
sha256 before activating the Save control and re-reads the file after
a bounded wait (injectable clock/sleep, default 2 s). If the digest
did not change, the result is an honest failure ("the file did not
change on disk; a save cannot be confirmed — state unknown"), and the
verifier independently re-reads the disk rather than trusting the
execution record.

**Untrusted application text.** Accessible names, window titles and
control text are application-controlled data: they are sanitized
(control characters stripped, length-capped) before appearing in any
message or report, and they never take part in a permission, policy or
approval decision — an application cannot grant itself approval by
naming a button "approved".

**Multi-step sequences (`sequence.py`).** `task_sequence` re-checks
every step through the permission stage (first denial denies all),
then executes steps in order with stop-on-failure semantics: a failed
step marks all dependents `skipped` and the task fails — a partial run
is never reported as success. Each executed step's data is kept so the
verifier re-verifies every step independently.

**MCP readiness (`mcp.py`).** A descriptors-only manifest for a future
MCP server: each automation tool's name, schema, permission
requirements, timeout and result-validation rules. No server, no
transport, no listener exists in this milestone.

**Environment dependency (observed).** Automation needs the desktop to
expose the application on the AT-SPI bus. On Ubuntu GNOME/Wayland
(observed on the dev machine: Ubuntu 26.04.1), the AT-SPI bus and
registry are live and GNOME apps expose interfaces, but with GNOME's
`toolkit-accessibility=false` LibreOffice does *not* register; ALWIN
then honestly reports `process_started`/"state unknown". Alwin Fusion
never changes that desktop setting itself. Nothing in this milestone implements
unrestricted UI automation.

## The local AI runtime

`ai_runtime/ollama_client.py` talks to Ollama over HTTP using only
`urllib`:

* `OLLAMA_BASE_URL` (default `http://localhost:11434`),
* `OLLAMA_MODEL` (no default — a model is never assumed or downloaded),
* `OLLAMA_TIMEOUT` (default 30 s).

Failure modes are typed (`OllamaUnavailableError`,
`OllamaTimeoutError`, `OllamaModelError`, `OllamaResponseError`) and
surfaced to the user with actionable text. The client **never** calls
`/api/pull`.

With `--llm`, `planner.plan_via_llm()` asks the local model for one
JSON action. The response is parsed with `json.loads` and pushed
through the same `validate_action()` gate as the deterministic plan;
on *any* problem the pipeline falls back to the built-in parser and
says so in a visible note. There is no cloud fallback path anywhere in
the codebase.

## Audit

`ai_core/audit.py` appends one JSON object per task to
`<state_dir>/audit.jsonl` (default
`$XDG_STATE_HOME/alwin-fusion` or `~/.local/state/alwin-fusion`),
with directory mode 0700 and file mode 0600. Recorded: task id,
timestamp, parser used, proposed action, permission decision,
approval, execution result, verification result, status, and any
error/failure reason. For application actions the record also carries
the integration id (`action.application` / `action.integration`), the
requested capability (the action name, e.g. `open_file`), the
permission operation (`open` / `detect`), the launch outcome
(`execution.data.started`, executable, pid, returncode, detail) and
the verification verdict. File `content` values and content embedded
in the command string are redacted to length markers; long strings
are truncated. Audit-write failures are surfaced as a visible report
note rather than silently ignored.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ALWIN_HOME` | `Path.home()` | Home directory (tests/portable installs) |
| `ALWIN_PERMITTED_ROOTS` | `Documents` | `os.pathsep`-separated roots, must be inside home |
| `ALWIN_STATE_DIR` | `$XDG_STATE_HOME/alwin-fusion` | Audit log location |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Local Ollama endpoint |
| `OLLAMA_MODEL` | *(empty)* | Model name; empty disables `--llm` |
| `OLLAMA_TIMEOUT` | `30` | HTTP timeout (seconds) |

CLI flags: `--dry-run`, `--llm`, `--root DIR` (repeatable),
`--ai-status`, `--show-config`, `--version`, `--help`.

## Testing

`scripts/run_tests.sh` runs the unittest suite (stdlib only). Every
test constructs a temporary home/root/state sandbox — the developer's
real `~/Documents` is never touched — and application tests inject
fake `popen`/`which` callables, so **no real desktop application is
ever started by the automated suite**; automation tests inject a fake
AT-SPI module (in-memory accessible trees), fake `/proc` directories
(`<pid>/comm` files), and fake clock/sleep callables, so **no real
desktop is ever inspected or driven by the automated suite**. The
v0.4 suite is **404 tests**; v0.5 adds **149 more** for a total of
**553 tests**: the full v0.1/v0.2 set (242, preserved
unchanged — covering supported/unsupported commands, traversal,
symlink escapes, permission denial, approval rejection, verification
failure, Ollama-unavailable behaviour, no-overwrite/no-delete
capability, dry-run inertness, audit redaction/permissions,
deterministic report rendering, PDF structural validation, workbook
reopen/compare with formulas preserved as text, formula-grammar
rejection, content-source checks and lying-handler detection), the 74
v0.3 application-engine tests, plus 88 new ones (64 automation + 22
sequence + 2 updated pin tests) covering:

* registry schema validation — invalid definitions (absolute/slashed
  executables, shell metacharacters in argv, unknown keys,
  capabilities, strategies, missing/duplicate `{path}`) are rejected;
* identifier resolution — unknown ids and executable-style tokens
  (`/usr/bin/rm`, `bash -c evil`) fail with the known-id list;
* discovery statuses (`available`, `not_installed`,
  `installed_but_unsupported`, `detection_failed`), desktop-name
  sanitizing, and proof that discovery never spawns a process
  (`subprocess.Popen` is patched to explode);
* meta-checks that `integrations/apps/*.py` and `planner.py` contain
  no `shell=True`, `os.system`, `subprocess.run/call`, `eval`, `exec`
  or `__import__`;
* planner/LLM rejection of arbitrary executables, extra flag fields,
  non-openable extensions and shell-style actions;
* the launch adapter contract: argv list with the path as a separate
  element, `shell=False`, fallback executable, missing app, immediate
  non-zero exit, `Popen` OSError, `{path}`-less template refusal;
* end-to-end pipeline behaviour: opening permitted files (approved
  argv verified), approval rejection, dry-run (approver never called),
  uninstalled applications failing honestly without any install
  attempt, unknown integration ids, traversal/symlink/outside-root
  refusals, missing files, launch-failure reporting, a lying launch
  result failing verification, audit fields + redaction, and v0.1/v0.2
  commands still working with the new dispatcher attached;

plus the 88 v0.4 tests covering:

* planner rejections — unknown actions (`send_keys`, `click_at`,
  `run_shell`, ... all rejected as "not allowed"), unknown
  applications, unknown roles, coordinate fields, missing control
  names, unbounded/oversized/null-byte text, non-allowlisted save
  extensions, empty/oversized/nested sequences, and per-step error
  messages naming the failing step;
* permission denials — applications without the `automate` capability
  (evince, okular, default) denied for every automation action,
  save-path containment, and read-only automation confirmed *not* in
  `MUTATING_ACTIONS`;
* the honest state machine — `not_installed`, `installed_not_running`
  (bus never touched), unreadable `/proc` → unknown (never a guess),
  `process_started` never upgraded to a window/document claim,
  `document_open` requiring a matching module window, and the
  state-report verifier rejecting internally inconsistent claims;
* find/read with bounds — role+name and role-only search, missing
  controls failing honestly, bounded/truncated reads marked untrusted,
  non-readable roles refused, role-vocabulary consistency pins;
* mutations — activation with revalidation (changed-identity control
  aborts before acting), refused actions and controls dying mid-action
  reported as failures/state-unknown, text entry confirmed by
  read-back (mismatch = failure), approval and dry-run inertness
  (nothing touched, approver never called);
* digest-confirmed saves — changed digest verified, unchanged file an
  honest failure, missing target refused, and the verifier
  independently failing a save whose recorded digest does not match
  the disk;
* backend failures — dead AT-SPI bus, exited applications,
  unavailable adapters reporting unsupported capabilities, and
  inspect with a dying backend never claiming a window;
* dishonest adapters failing verification — wrong reported control,
  `document_open` claimed without a live accessible connection,
  unconfirmed text entry;
* untrusted application text — control characters stripped, hostile
  names/titles sanitized, app text unable to grant permission/approval;
* audit redaction of entered text, source-level meta-bans on
  keystroke/mouse/shell primitives in `integrations/automation/*.py`,
  and parser preservation (`then` inside file content, v0.1 phrasing);
* task sequences — parsing (including v0.1 `then`-in-content
  preservation and quote awareness), per-step validation and
  permission recursion (first denial denies all), stop-on-failure with
  `skipped` dependents, runtime denial via the runner, per-step
  re-verification (lying step data fails the whole task), approval
  even for read-only steps, dry-run inertness, and audit records with
  step content redacted.

The spreadsheet/PDF pipeline tests skip themselves politely when
`openpyxl`/`reportlab` is not installed; the pure-stdlib validator
tests always run.

Plus the 149 v0.5 tests (all headless — no display, no desktop, no
window; the task experience is covered through the presenter and
manager with mocked/sandboxed dependencies):

* two-phase pipeline (`test_pipeline_twophase.py`) — `preview()`
  never executes and never audits; `preview()` + `run_previewed()`
  is behaviourally identical to `run()` (parity for read-only,
  approved, rejected, dry-run and unsupported outcomes); approval
  tokens execute only with a matching plan digest, a wrong/stale/
  cross-plan digest is an honest rejection with nothing executed;
  a bare `True` approval is never honoured; permissions are
  re-checked at execution time (a checker that denies after the
  preview blocks execution); `progress` reports `executing` then
  `verifying`;
* task lifecycle (`test_task_lifecycle.py`) — every legal transition,
  every illegal jump rejected (`draft→completed`, `draft→executing`,
  `planning→completed`, `executing→completed`, `verifying→executing`,
  `needs_attention` only from executing/verifying), terminal states
  absorbing, completed reachable only via verifying, cancellation
  refused while executing/verifying/terminal;
* task manager (`test_task_manager.py`) — intake validation
  (empty/over-long/busy), read-only tasks completing with no approval
  gate, mutating tasks waiting at `awaiting_approval` with nothing on
  disk, approve → execute → verify → complete, reject → cancel with
  nothing changed, wrong-digest approval refused, approval outside the
  gate refused, cancellation before execution (never claiming
  rollback), refusal to cancel a running action, unsupported/denied →
  failed (never completed), execution failure → failed, verification
  failure → `needs_attention` (never success), live per-step progress
  for sequences, legal-transition invariants under real runs, worker-
  thread update delivery, history written for completed/cancelled/
  failed outcomes, history-write failures surfaced (not hidden),
  requests masked in history, and an exploding `on_update` unable to
  kill a task;
* task history (`test_task_history.py`) — save/reload roundtrip,
  newest-first ordering, 0600/0700 permissions, corrupt/unknown-
  version/unknown-state/non-dict lines skipped and counted, binary
  garbage handled, malformed step entries dropped (not repaired),
  command/content redaction, bounded fields on read, hostile records
  inert (no execution surface), the display-only public API, and the
  source-level guarantee that history imports no execution code;
* capability status (`test_task_status.py`) — AI unreachable /
  no-model-configured / model-missing / ready, short probe timeout,
  typed `OllamaError` handling, unexpected bugs surfacing (not masked
  as "offline"), automation binding presence/absence, finder errors,
  and the offline statement being a design fact;
* presenter (`test_task_presenter.py`) — submission validation,
  plan badges (read-only / needs-approval / unverifiable), sequence
  step rows, risk notes (approval-invalidated, dry-run, launch
  honesty), approval affordances only while awaiting with the exact
  digest, tones per state, step marks/labels, verification evidence,
  failed verification never presented as verified, bounded untrusted
  text preserved as inert text, cancel affordances, honest
  suggestions for unsupported/denied requests, history rows/detail
  (always `replayable: False`);
* security meta-checks (`test_taskui_security.py`) — AST-based (not
  substring) analysis of every `taskui` module: no banned imports
  (subprocess, pickle, ctypes, ...), no banned calls (`eval`, `exec`,
  `compile`, `__import__`, `os.system`, `subprocess.*`, `pty.spawn`,
  `socket.socket`, ...); only the manager and entry point import the
  execution engine (`ai_core.pipeline/executor/planner/permissions/
  verifier`, `integrations`); history's only nested engine import is
  the documented masking helper; the GTK view never imports the engine
  and never calls `set_markup` (no Pango) — untrusted text can never
  be rendered as markup; and the package imports headless.

Three separate **opt-in** manual smoke tests exist and are never
invoked by the automated suite:

* `ALWIN_MANUAL_SMOKE=1 scripts/manual_smoke_apps.sh` — read-only
  detection and one real, personally approved launch of LibreOffice
  Writer inside a throwaway sandbox home, then the audit record;
* `ALWIN_AUTOMATION_SMOKE=1 scripts/manual_smoke_automation.sh` — a
  real AT-SPI session against LibreOffice: state inspection, an
  approval-gated launch, re-inspection, find/read, an approval-gated
  controlled-save attempt (whose expected outcome on an unmodified
  document is an honest "state unknown" failure), and a two-step task
  sequence — every step explained in the script, everything inside a
  throwaway sandbox home. If the desktop does not expose LibreOffice
  on the accessibility bus, every step fails honestly instead of
  guessing (observed on the dev machine — see the automation section).
* `ALWIN_UI_SMOKE=1 scripts/manual_smoke_ui.sh` — the v0.5 task
  experience: the headless lifecycle walkthrough (`python3 -m taskui
  --smoke`, real manager + trusted pipeline: read-only completion, a
  rejected mutating task proving nothing runs, then the approved task
  created and verified on disk), the headless capability check, the
  history it wrote, and finally the real GTK4 window on the developer's
  display. Everything inside a throwaway sandbox home; skipped with an
  honest message if GTK4 or a display is missing.

## Future compatibility

The prototype intentionally stops short of the full vision. Extension
points already in place:

* **New application integrations:** add a reviewed definition to
  `integrations/apps/registry.py` (validated by the same strict
  schema) — no handler code changes are needed, because the tools
  resolve everything from the definition. New *kinds* of integration
  follow the general pattern, now worked three times: create
  `integrations/<name>/` with handlers shaped like
  `handler(action, dry_run=...) -> ToolResult`, register them in a
  dispatcher factory, and add their action names/schemas to
  `planner.ACTION_SCHEMA` (plus `MUTATING_ACTIONS` if world-changing)
  and `permissions.check_action`. The pipeline, permissions,
  verification, audit and CLI then work unchanged.
  `integrations/documents/` (v0.2), `integrations/apps/` (v0.3) and
  `integrations/automation/` (v0.4) are the worked examples.
* **More automation backends/adapters:** `integrations/automation/base.py`
  defines the `AutomationAdapter` interface (inspect/find/read/
  activate/enter/save + capabilities); the LibreOffice adapter is one
  implementation behind the tools layer's injectable adapter factory.
  A UNO-based LibreOffice adapter is *planned*: it would talk to
  LibreOffice over its documented API — the unauthenticated UNO
  listening socket was deliberately not opened in v0.4 because that
  would weaken the v0.3 launch posture (any local process could drive
  it). Additional application adapters would first need a reviewed
  registry entry with the `automate` capability; there is
  deliberately no generic "automate anything" path.
* **MCP server:** `integrations/automation/mcp.py` already describes
  every automation tool as an MCP-shaped manifest (schema, permission
  requirements, timeouts, result-validation rules) so a future server
  can expose exactly the same allowlist. No server, transport or
  listener exists today; adding one must reuse `TaskPipeline`
  unchanged.
* **Model management:** `ai_runtime` is already the seam for
  additional runtimes and model lifecycle helpers (explicit user
  action only — no silent downloads).
* **Graphical desktop:** drive `TaskPipeline` directly; it is UI
  agnostic (approval and proposal are callbacks).
* **Bootable OS image:** `os-build/`, `models/`, `desktop/`,
  `app-bridge/` are reserved for those milestones.

What this milestone deliberately does *not* do: process isolation or
sandboxing, multi-user support, file deletion/overwrite, arbitrary
command or application-flag execution, keystroke/mouse synthesis or
any coordinate-based clicking (v0.4 automates only identified
controls through the accessibility action interface), software
installation, desktop-setting changes (e.g. enabling
`toolkit-accessibility`), UNO-socket control, an MCP server, network
operations, or any cloud integration. See `docs/SECURITY.md` for the
security model and its limits.
