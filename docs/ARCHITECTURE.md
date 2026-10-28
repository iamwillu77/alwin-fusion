# Alwin Fusion — Architecture

Version: 0.3.0 — Milestone: Application Integration Engine

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
                         -> approval -> execute -> verify -> audit
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
tests/                   unittest suite (temporary sandboxes only)
docs/                    ARCHITECTURE.md, SECURITY.md
scripts/run_tests.sh     Test entry point
scripts/manual_smoke_apps.sh  opt-in real-launch smoke test (ALWIN_MANUAL_SMOKE=1)
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
verifier.verify()   -> re-resolves paths, re-checks the filesystem
    |  VerificationResult {passed|failed|skipped}  (re-renders reports,
    v                                              reopens workbooks, validates PDFs,
    |                                              re-resolves open_file paths and
    |                                              requires a reported process start)
audit.record()      -> <state_dir>/audit.jsonl (0600, content redacted)
    v
cli.print_report()  -> human-readable outcome + exit code
```

Each stage is a separate object with a single responsibility, so a
future GUI or MCP server can drive the same `TaskPipeline`.

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
still cannot introduce one. Nothing in this milestone implements
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
ever started by the automated suite**. The v0.3 suite is **316
tests**: the full v0.1/v0.2 set (242, preserved unchanged — covering
supported/unsupported commands, traversal, symlink escapes,
permission denial, approval rejection, verification failure,
Ollama-unavailable behaviour, no-overwrite/no-delete capability,
dry-run inertness, audit redaction/permissions, deterministic report
rendering, PDF structural validation, workbook reopen/compare with
formulas preserved as text, formula-grammar rejection,
content-source checks and lying-handler detection) plus 74
new ones covering:

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
  commands still working with the new dispatcher attached.

The spreadsheet/PDF pipeline tests skip themselves politely when
`openpyxl`/`reportlab` is not installed; the pure-stdlib validator
tests always run.

A separate **opt-in** manual smoke test
(`ALWIN_MANUAL_SMOKE=1 scripts/manual_smoke_apps.sh`) runs read-only
detection and one real, personally approved launch of LibreOffice
Writer inside a throwaway sandbox home, then prints the audit record.
It is never invoked by the automated suite.

## Future compatibility

The prototype intentionally stops short of the full vision. Extension
points already in place:

* **New application integrations:** add a reviewed definition to
  `integrations/apps/registry.py` (validated by the same strict
  schema) — no handler code changes are needed, because the tools
  resolve everything from the definition. New *kinds* of integration
  (MCP servers, accessibility APIs, controlled UI automation) follow
  the general pattern: create `integrations/<name>/` with handlers
  shaped like `handler(action, dry_run=...) -> ToolResult`, register
  them in a dispatcher factory, and add their action names/schemas to
  `planner.ACTION_SCHEMA` (plus `MUTATING_ACTIONS` if world-changing)
  and `permissions.check_action`. The pipeline, permissions,
  verification, audit and CLI then work unchanged.
  `integrations/documents/` (v0.2) and `integrations/apps/` (v0.3)
  are the worked examples of exactly this pattern.
* **Model management:** `ai_runtime` is already the seam for
  additional runtimes and model lifecycle helpers (explicit user
  action only — no silent downloads).
* **Graphical desktop:** drive `TaskPipeline` directly; it is UI
  agnostic (approval and proposal are callbacks).
* **Bootable OS image:** `os-build/`, `models/`, `desktop/`,
  `app-bridge/` are reserved for those milestones.

What this milestone deliberately does *not* do: process isolation or
sandboxing, multi-user support, file deletion/overwrite, arbitrary
command or application-flag execution, software installation,
GUI automation/keystroke injection, network operations, or any cloud
integration. See `docs/SECURITY.md` for the security model and its
limits.
