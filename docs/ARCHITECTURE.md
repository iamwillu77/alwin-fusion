# Alwin Fusion — Architecture

Version: 0.2.0 — Milestone: Local Document and Spreadsheet Automation

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
tests/                   unittest suite (temporary sandboxes only)
docs/                    ARCHITECTURE.md, SECURITY.md
scripts/run_tests.sh     Test entry point
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
    v
verifier.verify()   -> re-resolves paths, re-checks the filesystem
    |  VerificationResult {passed|failed|skipped}  (re-renders reports,
    v                                              reopens workbooks, validates PDFs)
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
| `explain`             | `target` (nested action)  | no      | no       |

There is **no** delete, overwrite, move, shell or network action in
this milestone, by construction. Path strings are validated (length,
null bytes, `..`) before the permission checker even runs.

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
approval, execution result, verification result, status. File
`content` values and content embedded in the command string are
redacted to length markers; long strings are truncated. Audit-write
failures are surfaced as a visible report note rather than silently
ignored.

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
real `~/Documents` is never touched. The v0.2 suite is **241 tests**
(137 from v0.1 plus 104 new ones) covering supported and unsupported
commands, traversal, symlink escapes, permission denial, approval
rejection, verification failure, Ollama-unavailable behaviour,
no-overwrite/no-delete capability, dry-run inertness and audit
redaction/permissions, plus the document features:

* report rendering is deterministic and byte-compared by the verifier;
* PDF generation and structural validation (`%PDF-` signature,
  catalog, object count, `startxref`, `%%EOF`);
* workbook creation, reopening and cell-by-cell comparison against
  the spec, with formulas preserved as text;
* formula-grammar rejection (network/hyperlink functions,
  external-workbook and cross-sheet links, out-of-grid references);
* content-source files: permission checks, size limit, strict UTF-8,
  single-object JSON, invalid-spec rejection;
* content/spec validation idempotency (the tools layer re-validates
  the planner's normalized output);
* lying or tampering handlers failing verification.

The spreadsheet/PDF pipeline tests skip themselves politely when
`openpyxl`/`reportlab` is not installed; the pure-stdlib validator
tests always run.

## Future compatibility

The prototype intentionally stops short of the full vision. Extension
points already in place:

* **New application integrations (LibreOffice Writer/Calc, application
  discovery, desktop automation, MCP servers):** create
  `integrations/<name>/` with handlers shaped like
  `handler(action, dry_run=...) -> ToolResult`, register them in a
  dispatcher factory, and add their action names/schemas to
  `planner.ACTION_SCHEMA` (plus `MUTATING_ACTIONS` and approval
  metadata). The pipeline, permissions, verification, audit and CLI
  then work unchanged. `integrations/documents/` (v0.2) is the worked
  example of exactly this pattern.
* **Model management:** `ai_runtime` is already the seam for
  additional runtimes and model lifecycle helpers (explicit user
  action only — no silent downloads).
* **Graphical desktop:** drive `TaskPipeline` directly; it is UI
  agnostic (approval and proposal are callbacks).
* **Bootable OS image:** `os-build/`, `models/`, `desktop/`,
  `app-bridge/` are reserved for those milestones.

What this milestone deliberately does *not* do: process isolation or
sandboxing, multi-user support, file deletion/overwrite, arbitrary
command execution, network operations, or any cloud integration.
See `docs/SECURITY.md` for the security model and its limits.
