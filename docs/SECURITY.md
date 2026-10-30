# Alwin Fusion — Security Model and Limitations

Version: 0.13.0 — Milestone: Desktop Intelligence & Application Control

This document states what the prototype defends against, how, and —
importantly — **what it does not defend against**. Passing tests does
not make a system secure; the boundaries below are what the code is
designed around, and the *Known limitations* section is what remains
open.

## Assets and adversaries (working assumptions)

Assets: files inside the user's configured permitted roots, the audit
log's integrity, and the user's system outside those roots. Since v0.6
the **truthfulness of the companion's display** is also an asset: Alwin
must never visually claim a permission, an approval or a success that
did not actually happen, because the user makes decisions based on what
they see.

Assumed-trusted: the Alwin Fusion code itself, the Python
interpreter, the OS, and the human user making approval decisions.

Assumed-untrusted: natural-language input (it may be accidental or
hostile) — **including anything said into the microphone**: since v0.9
a voice transcript is treated as *exactly* as untrusted as typed text
(it flows through the same validation, permission and approval path and
confers nothing extra; the recognition result is shown verbatim and is
never executed), local LLM output (models can be wrong or
prompt-injected),
anything a concurrent process does to the filesystem, application
manifests and desktop-entry files (their *names* are used for
detection only, after sanitizing; their contents are never read), any
file a user might drop in a directory claiming something is
installed, and — since v0.4 — **everything a running application says
about itself**: accessible names, window titles and control text are
application-controlled data and can be hostile (escape sequences,
newlines, fake "permission granted" strings). They are sanitized
before display and never take part in a permission, policy or approval
decision.

Not defended against: a compromised OS account, a malicious
`sitecustomize`/Python environment, kernel attacks, or another
process racing the filesystem (see TOCTOU below).

## Security controls

### 1. Path confinement (least privilege)

* Filesystem operations only resolve through
  `ai_core/permissions.py` (`PermissionChecker.resolve`).
* Relative plan paths resolve against the home directory; the result
  must be equal to or inside one of the configured permitted roots
  (`realpath`-based containment check).
* Roots themselves are validated at config load: each must be inside
  the home directory, and a root that is a symlink pointing outside
  the home directory is rejected (`ConfigError`).
* Default configuration: a single root, `~/Documents`.
* Rejected outright: `..` traversal (textual check), null bytes,
  over-long paths (1024 chars), `~`/home itself, absolute paths
  outside roots, and protected system prefixes (`/etc`, `/usr`,
  `/bin`, `/var`, `/proc`, `/sys`, `/dev`, `/root`, ...).
* Symbolic links are resolved with `os.path.realpath` **before** the
  containment check, so directory symlinks, file symlinks, dangling
  symlinks and symlink chains that leave the roots are denied
  (covered by tests).
* Defence in depth: the pipeline checks permissions *and* every
  filesystem tool re-checks them itself before touching disk.
* **Content sources (v0.2):** a `using spec.json` file is read
  through the same `PermissionChecker` (`operation="read"`), so
  traversal, symlink escapes and paths outside the roots are denied
  exactly like any other read (tested). Additionally the read is
  bounded (512 KiB), strictly UTF-8 decoded (no replacement
  characters), and must parse to a single JSON object.

### 2. No arbitrary execution

* The action allowlist is closed: `create_directory`,
  `create_text_file`, `create_report`, `create_pdf`,
  `create_spreadsheet`, `list_directory`, `read_file`,
  `detect_applications`, `open_file`, `inspect_application`,
  `find_control`, `read_control`, `activate_control`, `enter_text`,
  `save_document`, `task_sequence`, `explain`. The exact set is
  pinned by tests (v0.2 went from five to eight actions, all
  create-only; v0.3 deliberately added exactly two: read-only
  detection and an approval-gated launch; v0.4 added exactly seven —
  three read-only automation actions, three approval-gated automation
  mutations and `task_sequence` — none of which can delete, move,
  overwrite, synthesize keystrokes/mouse events or execute arbitrary
  code).
* There is no delete, overwrite, move, chmod, or shell action — not
  disabled, simply absent (meta-tests assert this).
* Model output is parsed with `json.loads` and validated by the same
  code as the deterministic parser. Nothing is ever `eval`/`exec`-ed
  and nothing is formatted into a shell command.
* File creation uses `os.open(..., O_CREAT|O_EXCL)`, so an existing
  file can never be clobbered even by a race. All v0.2 document
  writes go through the same `O_EXCL` path.
* `subprocess` appears in exactly one place — the v0.3 launch adapter
  (`integrations/apps/launch.py`) — always with an argv **list**,
  `shell=False`, and never with a string built from user, file or
  model text (meta-tests pin the absence of `shell=True`,
  `os.system`, `os.popen`, `subprocess.run/call/check_output` in the
  apps package and in the planner).

### 2c. Application integration (v0.3)

**Registry — what may ever run.** Applications are referenced only by
registered identifiers resolved against the closed builtin registry.
Definitions are validated against a strict schema: closed key sets
(unknown fields rejected), bare executable names only — no `/`, no
absolute path, no `..` — argv templates whose alphabet excludes every
shell metacharacter and which must contain exactly one `{path}`, and
only reviewed strategies (`exec`; `process_started`). There is **no**
manifest loading from disk or from model output, so an untrusted file
or the LLM cannot introduce an executable, a flag, an import or a
capability. Unknown tokens (including `soffice --calc`,
`/usr/bin/rm`, `bash -c evil`) are rejected at planning time with the
list of known ids.

**Discovery — read-only, evidence-based.** Detection uses only
`shutil.which` on registered executable names and the *file names* of
`.desktop` entries in three fixed directories; entry **contents are
never read or executed**, names are sanitized to a conservative
character set, and listings are bounded. No process is spawned,
nothing is installed/removed/updated. An application is `available`
only when its launch executable resolves — a name in a user-supplied
file is never evidence. A desktop entry without a working executable
is reported `detection_failed`, not `available`.

**Launch — constrained, approved, honest.** `open_file` re-resolves
the target through `PermissionChecker` (traversal, symlink, roots —
defence in depth), requires the file to already exist, supports only
`.txt`/`.pdf`/`.xlsx` for registered file types, is in
`MUTATING_ACTIONS` (explicit `y/N` approval per launch; `--dry-run`
never launches and never asks), and starts the process with the
reviewed argv template, `shell=False`, the path as its own argv
element, std streams on `/dev/null`. A missing application is a clear
failure ("never installs applications automatically") — not an
install attempt. An immediate non-zero exit is a launch failure.
Verification re-resolves the path and requires a reported process
start; the message claims **only** that the process was started, and
explicitly not that any window was inspected. Detection requires no
approval because it is read-only.

**Capability policy.** What this milestone allows end-to-end:
detect (read-only), open one approved existing file with one
registered application. What is prohibited/unsupported and simply
absent: arbitrary application commands, model- or user-supplied
executables/flags, software installation, reading or editing
"application data" beyond the file being opened, and file
deletion/overwrite by this path. (Window inspection, which v0.3 listed
as absent, arrived in v0.4 — but only through the reviewed automation
engine below, never via `open_file`.)

### 2d. Desktop automation (v0.4)

**What may ever be automated.** Only applications with the `automate`
capability in the closed registry — currently LibreOffice Writer and
Calc. Every other registry application (Evince, Okular, the desktop
default) is denied by the permission stage for every automation
action, and unknown application tokens are rejected at planning time
exactly like `open_file` identifiers. There is deliberately **no**
generic "automate anything" path.

**Closed action set, no synthetic input.** The seven automation
actions accept only `{role, name}` control locators validated against
a closed 24-role vocabulary; coordinate fields (`x`, `y`) are rejected
at planning time. The automation package
(`integrations/automation/*.py`) contains **no keystroke injection,
no mouse/coordinate events and no shell** — the only mutation
primitives are the accessibility *action* interface of an
already-identified control (`doAction`) and
`EditableText.set_text_contents` on an already-identified input field.
Source-level meta-bans in the test suite pin the absence of
`subprocess`, `os.system`, `os.popen`, `eval`, `exec`,
keyboard/mouse-event generators and tools like `xdotool`/`ydotool`/
`wtype`. Text entry is further restricted to input fields (role
`entry`); document bodies are never typed into, so GUI automation
cannot bypass the reviewed document generators.

**Honest state machine.** Every operation reports a state from a fixed
set (`not_installed`, `installed_not_running`, `launch_requested`,
`process_started`, `interface_detected`, `document_open`,
`control_identified`, `action_performed`, `result_verified`,
`failed_or_unknown`) and the verifier rejects any claim that exceeds
its evidence: `document_open` without a live accessible connection
fails verification (tested with a lying adapter), a started process is
never reported as a window, and process observation uses read-only
`/proc` scanning that reports *unknown* when `/proc` is unreadable —
never a guess. The inspection path never launches anything.

**Revalidation before every mutation.** Immediately before activating
a control or entering text, the locator is re-verified against a
freshly walked accessibility tree; if the control changed identity or
vanished, the action aborts with `failed_or_unknown` instead of
acting on the wrong control (tested).

**Digest-confirmed saves.** `save_document` records the target file's
sha256 before activating the Save control and re-reads the file after
a bounded wait. If the digest did not change, the result is an honest
failure ("a save cannot be confirmed — state unknown") — activating a
Save button is never itself reported as a save. The verifier
independently re-reads the disk and fails a save whose recorded digest
does not match (tested with a lying adapter). Save targets must be
inside the permitted roots with an allowlisted extension (`.txt`,
`.xlsx`); a missing target file is refused (the automation layer never
creates or overwrites files — content creation stays with the
`O_EXCL`-guarded v0.2 tools).

**Untrusted application text.** Accessible names, window titles and
control text are sanitized (control characters stripped, length-capped)
before appearing in any message, report or audit record, and they
never take part in a permission, policy or approval decision — an
application naming a button "permission granted" or a window
"... (approved)" changes nothing (tested).

**Approval and read-only/mutation separation.**
`inspect_application`/`find_control`/`read_control` are read-only:
never in `MUTATING_ACTIONS`, never requiring approval, mutating
nothing (asserted by tests). `activate_control`, `enter_text`,
`save_document` and `task_sequence` require interactive approval per
action; `--dry-run` touches nothing and never asks. `task_sequence`
re-checks *every* step through the permission stage (first denial
denies the whole sequence) and stops at the first failed step, marking
dependents `skipped` — a partial run is never reported as success, and
every executed step is re-verified independently by the verifier.

**Model output is never executable.** An LLM proposal naming an
automation action re-enters `validate_action()` exactly like the
deterministic parser: unknown actions, applications, roles, coordinate
fields or oversized text are discarded.

**Environment dependency and honest degradation.** Automation needs
`python3-gi` + `gir1.2-atspi-2.0`, a running AT-SPI bus, and the
desktop exposing the application (on Ubuntu GNOME:
`toolkit-accessibility` enabled). Without these, the backend reports
"capability unavailable" or `process_started`/"state unknown" honestly
— **Alwin Fusion never changes desktop accessibility settings, never
installs dependencies and never fakes a window it cannot see**.
Observed on the development machine (Ubuntu 26.04.1 GNOME/Wayland,
`toolkit-accessibility=false`): the AT-SPI bus is live and GNOME apps
expose interfaces, but LibreOffice does not register — ALWIN then
reports `process_started` and refuses to fabricate control data.

**Deliberately not opened: the UNO socket.** LibreOffice's documented
UNO API would allow stronger control, but listening on an UNO socket
would be an unauthenticated local control channel (any process on the
machine could drive it), weakening the v0.3 launch posture. v0.4 does
not open such a socket; a UNO adapter is *planned* and must first
solve authentication. Likewise **no MCP server exists**: `mcp.py` is a
descriptors-only manifest (schemas, permissions, timeouts, result
rules) with no transport or listener.

**Capability policy summary.** Allowed end-to-end: inspect a
registered application's honest state, find a control by role/name,
read a permitted control's text, activate an identified control, type
into an identified input field, digest-confirmed save of the open
document to an allowed path — each behind the permission stage and
(approval-gated where mutating), each independently verified.
Prohibited and simply absent: keystroke/mouse synthesis, coordinate
clicking, shell or arbitrary command execution, automating
unregistered applications, typing into document bodies, hidden or
unattended sensitive actions (every mutation is individually
approved), disabling desktop security, and any auto-install.

### 2e. Desktop intelligence & application control (v0.13)

**Registry-scoped lifecycle only.** The five new desktop actions
(`launch_application`, `application_status`, `active_window`,
`focus_application`, `close_application`) accept **only** an
application id resolved against the closed registry. Validation
rejects unknown ids (`PlanValidationError`) and the permission branch
denies them again (defence in depth); there is no executable path, no
command string and no model-generated text anywhere in the launch
flow — the argv is the reviewed registry template with `{path}`
dropped, executed with `shell=False`.

**No process termination, ever.** `close_application` invokes only
the window's own `window.close` (or `close`/`quit`) accessibility
action and then *observes* whether the targeted window left the
accessibility bus. No signal constant (`SIGTERM`, `SIGKILL`), no
`kill`, no `popen("kill ...")` exists in the desktop controller; the
test suite pins the absence of `os.kill`, `signal`, `SIGKILL` and
`terminate(` in `integrations/apps/desktop.py`. A modal dialog (e.g.
unsaved work) that keeps the window open is detected and reported —
Alwin never overrides it.

**Untrusted accessibility content.** Window titles come from other
applications and are treated as hostile input: bounded (80 chars),
control-character stripped, and scrubbed of credential-shaped text
(`password:`, `token=`, `Bearer …`) by a deterministic regex before
they can enter a task record, the audit log or the UI. An
application window cannot authorize anything: no action is ever
derived from window contents, and a prompt-injection payload in a
title or document cannot add a step, change a digest or alter the
permission policy (the pipeline only ever sees closed-set validated
actions).

**Observation minimization.** The only desktop metadata collected is
(a) `/proc/<pid>/comm` names for registered executables (process
presence) and (b) window title, role name and the boolean `active`
flag from the accessibility bus — read only when a desktop action
actually runs. No screenshots, no screen or window *contents*, no
keystrokes, no pointer events, no accessibility-tree dumps, and
nothing is persisted beyond the bounded audit fields. Everything is
local; no network API exists in this path.

**Approval and verification.** `launch_application`,
`focus_application` and `close_application` are derived-mutating
(pinned to the trusted `MUTATING_ACTIONS` set) and require the same
digest-bound approval as filesystem mutations;
`application_status` and `active_window` are read-only and run
without approval, exactly like `detect_applications`. Verification
never exceeds the evidence: a launch passes only with
`process_observed`, focus only with the re-read `active` state, a
close only with the window's disappearance; otherwise the step is
FAILED with an explicit UNVERIFIED message and the task lands in
`needs_attention` — never "completed".

### 2b. Structured content validation (v0.2)

Document and spreadsheet payloads are untrusted data (user text, a
JSON file, or a model proposal) and are validated in
`ai_core/specs.py` **before** planning and again in the tools layer
before rendering:

* Closed key sets per structure — unknown fields are rejected, so a
  future dangerous option can never be smuggled in.
* Bounded sizes: sheet/header/row/formula/format counts, per-cell
  length, a 200 000-cell global cap, and 64 KiB inline content.
* Cell values are scalars only (no nested objects), and row strings
  may not start with `=` — openpyxl would silently turn those into
  formulas, bypassing formula validation.
* Spreadsheet formulas are checked against a conservative grammar:
  allow-listed functions only (no `WEBSERVICE`, `HYPERLINK`, `RTD`,
  `CALL`, ...), no external-workbook links (`[Book1]...`), no
  cross-sheet references, no undefined names, balanced parentheses,
  and every referenced cell must lie inside the declared data grid.
* Output file names are constrained: fixed extensions
  (`.txt`/`.pdf`/`.xlsx`), no hidden/leading-dot names, no `..`,
  length-limited — enforced by the planner and re-enforced by the
  tools.
* The model-proposal path never receives a `source` key, so model
  output cannot trigger a file read; a content source can only come
  from the deterministic parser (which then goes through the
  permission-checked loader).
* Validation is idempotent (normalized output re-validates cleanly),
  so the defence-in-depth re-check cannot desync from planning.
* The optional libraries (`openpyxl`, `reportlab`) are imported
  lazily and only ever receive already-validated data; a missing
  package produces a clean, actionable error, never an import-time
  crash of the core. Nothing is auto-installed — the error only shows
  the `pip install` command for the user to run themselves.

### 3. Explicit approval before modification

* Mutating actions require interactive `y/N` approval after the
  resolved operation is displayed. `open_file` is mutating: every
  application launch is individually approved; `detect_applications`
  is not, because it reads fixed system locations and starts nothing.
  v0.4: `activate_control`, `enter_text`, `save_document` and
  `task_sequence` are mutating — every automation mutation is
  individually approved, and a sequence is approved once as a whole
  only after every step has passed the permission stage;
  `inspect_application`/`find_control`/`read_control` are read-only
  and never require approval.
* EOF, Ctrl-C, or any input failure while approving = **no**.
* There is deliberately no `--yes`/`--force` bypass in this milestone.
* `--dry-run` performs no writes, launches no process, and never even
  asks for approval.
* v0.5 (two-phase / GUI): approval is **bound to the exact validated
  plan the user was shown**, via a sha256 `plan_digest` of the
  canonical action JSON. `run_previewed(..., approval=ApprovalToken
  (digest))` recomputes the digest of the action it is about to
  execute and refuses (honest rejection, nothing executed, approval
  marked `invalidated`) on any mismatch — a plan that changed after
  approval can never run on the strength of the stale approval
  (tested). A bare `True` is deliberately **not** an approval
  mechanism; only an `ApprovalToken`, the interactive approver, or an
  explicit `False` (rejection) are accepted. The UI-layer approval the
  manager collects is handed back to the pipeline, which re-checks
  permissions at execution time even though both `preview()` and the
  UI already checked them (the world may have changed while the user
  was deciding). The UI can never grant itself permission: it has no
  access to the permission engine, and the pipeline does not trust it.

### 3c. Task experience (v0.5) and its new attack surface

The desktop task experience (`taskui/`) is a **thin view plus a
session manager over the existing trusted core**. It adds no second
execution engine, no second permission system, and no new action
types. Its own security properties and the new surface it introduces:

* **The window executes nothing.** `taskui/gtk_app.py` renders
  presenter dictionaries and forwards five user intents (submit,
  approve, reject, cancel, history selection) to the manager. It has
  no subprocess, no code evaluation, no file writes, and no import of
  the execution engine — enforced by AST-based meta-checks that parse
  every `taskui` module and fail the suite if `subprocess`, `os.system`,
  `eval`, `exec`, `compile`, `__import__`, `socket.socket`, etc. appear
  as imports or calls, or if any module except the manager/entry point
  imports `ai_core.pipeline/executor/planner/permissions/verifier` or
  `integrations` (tested).
* **Untrusted text is rendered as plain text, never markup.** Every
  dynamic string — the user's request, the model's proposal, file
  names, application/window titles, control text, history records and
  error messages — is inserted with `set_text`/plain `Gtk.Label`, and
  the view never imports Pango nor calls `set_markup` (tested). Such
  content can therefore never impersonate UI chrome, inject markup, or
  influence a permission/approval/policy decision; the presenter only
  ever truncates and passes through curated fields.
* **New attack surface, stated honestly:**
  1. *The task-history file* (`<state_dir>/task_history.jsonl`) is a
     new user-owned data file an attacker with local write access could
     corrupt or forge. It is **display-only by construction**: the
     module imports no execution code, exposes no run/replay/execute
     function, validates every record against a closed schema on read,
     and skips-and-counts anything malformed or hostile — a forged
     record can, at worst, be *displayed* (sanitized and bounded), and
     nothing in Alwin Fusion ever replays it (tested). It is **not
     tamper-proof** and makes no integrity claim; it can supplement
     but never replace the audit log.
  2. *The worker-thread approval bridge* (`taskui/manager.py`) blocks
     a worker thread on an event the UI releases. Approval carries the
     displayed plan digest; a wrong digest is refused before the worker
     wakes, and the pipeline independently re-verifies the digest and
     re-checks permissions at execution time, so a compromised UI
     thread cannot approve a plan the user did not see or bypass the
     permission engine (tested).
  3. *GTK/CSS theming* loads a fixed, embedded stylesheet from a
     bytes literal; no external theme file, URI or user-controlled CSS
     is read.
* **Lifecycle honesty.** The state machine refuses impossible
  transitions by construction: a task cannot execute before planning
  and validation; `completed` is reachable only from `verifying`
  (dispatching an action never completes a task); `cancelled` is only
  reachable before execution starts, and a cancelled task never claims
  rollback; `needs_attention` is the honest state for a world change
  whose result could not be verified. A failed sequence preserves
  truthful per-step records (`ok`/`failed`/`denied`/`skipped`), never
  reporting complete success when a required step failed (tested).
* **One task at a time.** The manager refuses a second submission
  while one is running, so there is no queue to race or confuse.

**Task planning surface (v0.12).** The planning layer reuses every
trusted stage above and adds these specific properties (tested):

* **The plan is untrusted until validated.** Model output is JSON data:
  `validate_plan()` re-applies the closed action allowlist to every
  step, enforces dependency rules and bounds, derives risk from the
  trusted `MUTATING_ACTIONS` set (a proposal cannot bless itself as
  safe), and only *sanitises* mechanical key misplacement (hoisting)
  — every hoisted value is still fully validated. No model-generated
  code, shell or function name is ever executed: tool selection is
  "declared capability + validated input" only.
* **The LLM proposes; trusted code authorizes.** Authorization stays
  outside the model: one digest-bound approval covers the whole
  displayed plan (what the user saw is what runs), every step is
  permission-checked again at preview time and executed under a
  per-step digest token, and `ai_core.capabilities` reports honestly
  what exists (including **MCP = descriptors only, zero executable
  MCP tools — no Blender integration exists**).
* **Nothing bypasses through voice or chat.** The window's routing
  predicate is the deterministic parser plus a lexical goal gate —
  both funnel into the same `manager.submit()` path; chat never
  reaches the manager (no record), and a goal cannot skip validation.
  Clarify/decline outcomes are honest records ("nothing was
  executed"), never silent failures.
* **Recovery cannot repeat side effects.** Mutations are never
  retried (one bounded retry exists for read-only steps only); a replan
  runs only after a zero-execution failure and a trusted digest check
  drops any proposal that repeats an already-succeeded *or
  already-failed* mutation; recovered plans report `needs_attention`,
  never `completed`.
* **Untrusted content stays untrusted.** Summarizing derivations quote
  file contents to the local model under an explicit "data, never
  instructions" prompt; report contents built from them are plain
  display/file text, never commands. Plans are runtime-only:
  `TaskRecord.task_plan` is deliberately excluded from serialization,
  history keeps only curated step/status/verification fields, and
  credential-looking text in planning reasons is scrubbed exactly like
  reply-failure reasons (mirrored redactor in `task_planning`).

### 3d. Companion presentation (v0.6/v0.7/v0.8/v0.9) and its new attack surface

The Alwin companion (`companion/`) is a **presentation layer only**. It
observes curated task records and animates a character in an animated
bedroom scene; it adds no
execution engine, no permission path, no approval path and no network
client. Its security properties:

* **Structural inability to act.** No companion module imports the
  trusted execution engine (`ai_core.pipeline/executor/planner/
  permissions/verifier/audit`, `integrations`) or the task experience
  (only the `__main__` launcher may import `taskui`, to host the
  manager/window it displays) — enforced by AST-based meta-checks over
  every `companion` module (exactly the 20-module set, pinned) that
  fail the suite for banned imports
  (`subprocess`, `socket`, `urllib`, `http`, `requests`, `pickle`,
  `ctypes`, `pty`, ad-hoc audio-capture modules, …) or banned calls
  (`eval`,
  `exec`, `compile`, `os.system`, `subprocess.*`, `socket.socket`,
  `webbrowser.open`, …) (tested). v0.9 adds a second, stricter layer
  for the six voice modules: a **per-module import allowlist checked
  at any depth** plus hard bans (no network library, no `wave`/
  `audioop` — recording to file is impossible package-wide — no
  `eval`), and `ctypes` reachable from **exactly one** module
  (`voice_output.py`, tested); the one supported audio path is
  GStreamer via `gi`. The state controller's own imports
  are pinned to `time`/`typing`, the adapter's to `typing` +
  `companion.*`, and — new in v0.7 — the entire character drawing core
  (`companion/character.py`) is pinned to `cairo`/`math`/`dataclasses`/
  `typing` (tested): the artwork cannot read a file, open a socket or
  learn about the task engine. v0.8 extends the same pins to the two
  new modules: the **scene model** (`companion/scene.py`) is pinned to
  `math`/`dataclasses`/`typing` — no cairo, no GTK, no I/O, no task
  engine, so the world/movement logic is inert data — and the
  **bedroom artwork** (`companion/bedroom.py`) to `cairo`/`math`/
  `typing`/`companion.scene` (tested); `conversation.py` (v0.9) is
  additionally engine-free and pinned to the duck-typed
  `manager.submit()` path (tested).
* **The animated world cannot act, script or be scripted.** The scene
  is driven exclusively through a **closed command allowlist** (13
  names: `wake`, `get_up`, `move_to`, `begin_writing`, `begin_coding`,
  `begin_thinking`, `begin_waiting`, `begin_error`, `begin_celebrating`,
  `return_idle`, `place`, `stop`, `reset`) with only enumerated string
  positions (`bed`, `beside_bed`, `greeting`, `desk`) as arguments —
  there is no scripting surface, no eval of scene input, no file path
  or name ever passed into the scene (tested: unknown commands and
  positions are refused without moving the character). The scene model
  executes, approves and modifies nothing: `begin_waiting`/`begin_error`/
  `begin_celebrating` only set an honest visual phase, and a walk
  finishing is never treated as task success (the scene has no notion
  of approval or completion at all). Mouse clicks and other input never
  reach the scene — the GTK view exposes no scene command surface, and
  the closed allowlist is the only interaction vocabulary (tested).
* **The observer feed is read-only and cannot approve.**
  `TaskManager.add_observer()` delivers the same curated `TaskRecord`
  the task window renders; the manager ignores observer return values,
  never asks an observer for permission, wraps every call in
  try/except, and the adapter exposes no `approve`/`reject`/`execute`/
  `grant` method and holds no manager reference (tested). Approval
  still requires the digest-bound call path through the pipeline, so a
  compromised companion cannot approve a plan or change an outcome
  (tested: a truthy observer return approves nothing).
* **Truthfulness rules are structural, not cosmetic.** The renderer
  only ever *receives* states — it never decides them and cannot infer
  success from animation. `celebrating` is reachable only from the
  adapter's `completed` mapping (which the task lifecycle allows only
  after verification); `failed`, `needs_attention`, `cancelled` and
  unknown states are mapped to `error`/calm `idle` and a test proves
  the celebration state is never sent to the renderer on those paths.
  An invalid state transition falls back to a calm state instead of
  inventing behaviour, and a truthful target (`waiting_for_approval`,
  `celebrating`) is never lost to a fallback (tested).
* **Animation failures cannot affect tasks.** Every renderer call the
  controller makes is wrapped: exceptions are captured into a bounded
  `renderer_errors` list and never propagate — and because the adapter
  sits *downstream* of the manager (the manager swallows observer
  exceptions too), neither an animation bug nor an adapter bug can
  interrupt or alter task execution (tested at both layers).
* **Bounded plain-text display.** Speech bubbles, subtitles, status and
  greeting name are normalized and bounded (≤ 160–240 chars), come
  from curated fields (never raw file content, model output or error
  internals — tested), and the GTK view inserts them with `set_text`
  only: no `set_markup`, no Pango import (tested). Untrusted text
  cannot impersonate UI chrome or influence a policy decision.
* **No network, no recording, no unattended listening (v0.9).** The
  companion opens no socket, contacts no server and starts no web
  service (AST-tested package-wide; the voice modules are additionally
  banned from importing any network library at any depth). The
  microphone opens **only on an explicit mic click** (push-to-talk) and
  only while the window is usable; audio lives in memory for the turn
  and is discarded — no recording is ever written to disk (the `wave`
  and `audioop` modules are banned package-wide, the voice modules
  perform no file writes at all, and no cloud ASR/TTS exists to upload
  to). Teardown and cancellation release the device and drop captured
  PCM (tested). Wake-word monitoring (v0.10) reads audio **only while
  the opt-in switch is on** (default off) through the bounded in-memory
  queue/window described above — never a recording, never an
  always-on mode. Speech,
  when it happens, is claimed only after the engine reports it; a
  missing engine keeps `NullVoice`'s honest `available = False` /
  `speak() → False` behaviour, and nothing is ever claimed as heard,
  recognized or spoken before it actually is (tested).
* **The greeting assumes nothing.** It is text, fires at most once per
  session (optionally once per day), honours an explicit disable and
  quiet hours, takes its name from configuration (no hard-coded user —
  tested), and does not claim the user is present.

New surface introduced by v0.6, stated honestly:

1. *`companion_settings.json`* (`<state_dir>`, mode 0600) is a new
   user-owned file. Its schema is closed (unknown keys dropped, type
   mismatches fall back to defaults) and it is **cosmetic-only**: at
   worst a local writer can change greeting/subtitle/always-on-top
   preferences. The trusted core never reads it (AST-tested), so it
   cannot grant a permission, enable an action or bypass approval.
2. *Per-state image assets* (`companion/assets/character/<state>/frame.png`)
   are optional local files decoded by `Gdk.Texture`. Missing or
   corrupt files fall back to the vector character (tested) — a
   hostile-but-valid image can, at worst, change what the character
   *looks like* for one state; it cannot change which state is shown or
   any task decision (states come only from the adapter).
3. *The once-per-day greeting marker* (`greeted.json`, mode 0600) only
   records a date; forging it can at most suppress or permit one extra
   greeting.
4. *A second window on the same display.* The companion hides on
   close, never grabs keyboard focus after launch (the task window is
   presented last), does not block other applications, and
   always-on-top is off by default and best-effort — none of this
   changes any trust decision, which still happens in the task window.
   v0.7 additionally guards against duplicate windows on
   re-activation (the existing window is presented instead) and stops
   the animation timer while the window is hidden (no off-screen
   redraws).
5. *The animated world (v0.8) adds no new file, network or process
   surface.* The scene model and the bedroom artwork are pure
   computation over in-memory data (source-pinned, tested); the room is
   drawn from the same window that already existed (no new window, no
   new widget that accepts input — clicks never trigger commands). The
   morning sequence and walks are driven only by elapsed time and the
   same closed command allowlist; every timer id is tracked and
   cancelled on close/hide (tested), and a task record arriving
   mid-sequence cancels the visuals without touching the task. The new
   `reduced_motion` preference lives in the same cosmetic-only
   settings file (closed schema) and cannot influence any trust
   decision.
6. *The voice stack (v0.9) adds one bounded input surface and no
   network, no file and no new trust decision.* Stated precisely:
   - **Microphone capture** is a GStreamer pipeline that runs only
     between an explicit mic click and stop/cancel/teardown; captured
     PCM exists in process memory for that turn only and is dropped
     afterwards (no file writes in the capture module — AST-tested;
     `wave`/`audioop` banned package-wide; no cloud endpoint exists to
     send it to). The only new dependency surface is GStreamer device
     access itself, which the user can see (an open microphone is
     shown as the listening state + level meter).
   - **Recognition output is untrusted text, exactly like typing it.**
     It is displayed verbatim (bounded, `set_text`) and reaches only
     `TaskManager.submit()` through the shared conversation pipeline —
     same allowlist, same permission checks, same digest-bound
     approval. Nothing spoken can grant a permission, auto-approve a
     plan or bypass the task window (tested: the security suite pins
     conversation's import surface and its single submission path).
   - **Language detection is advisory, not a policy input.** Its
     result only selects reply language / ASR hint / TTS voice — it
     never participates in a permission or approval decision, and an
     uncertain result is surfaced as uncertainty instead of a claim.
   - **Model installation is manual and outside the app.**
     `scripts/install_voice_models.py` (user-run, confirm-first,
     reports size/free space) is the only sanctioned path to a model;
     inside `companion/`, every network library (socket/urllib/http/
     requests/…) is banned at any import depth — the only permitted
     engine-adjacent imports are `faster_whisper`/`numpy`/
     `huggingface_hub` (lazy, for the model and its cache *path*
     constants) and loading pins `local_files_only=True`. A missing
     model is an honest error naming the installer, never a download.
   - **Speech synthesis is opt-in and honest.** `spoken_responses`
     defaults to off; the espeak/GStreamer playback runs on a worker,
     the `speaking` state is entered only after dispatch and left on
     the engine's completion truth; a failure is reported, never
     masked. `ctypes` — the one memory-unsafe escape hatch in Python —
     is importable from exactly one module (`voice_output.py`,
     tested) and used only for espeak's documented C API with full
     argtypes.
   - **Replies never re-enter the pipeline.** An LLM reply is
     display/speech text only; the sole `submit()` call carries the
     user's own transcript (tested), so a prompt-injected reply cannot
     become an action.
   - The new voice settings (`mic_enabled`, `language_auto`,
     `language_preferred`, `spoken_responses`, `speech_rate`,
     `voice_id`, `asr_model`, `mic_level_meter`) are additional
     cosmetic keys in the same closed schema: a tampered file can at
     worst change voice/language preferences — never a permission,
     never an execution (the bool/str-only schema pin now covers
     them). The `mute_voice` placeholder key was removed rather than
     repurposed. v0.10 adds `wake_word_enabled` (default **off**) and
     `wake_phrase` — same cosmetic-only guarantees; neither can grant
     anything. v0.11 adds `context_enabled` (default **on**) and
     `context_turns` ("8", clamped 2–20) — they only bound how many
     *in-memory* turns are quoted into a local prompt; no permission,
     execution or data-retention effect (tested).
   - **Reply-failure reasons are scrubbed before display** (v0.9.1):
     embedded exception text is bounded and stripped of URL userinfo
     (`scheme://user:pass@`) and bearer/token/key-style secrets by a
     pure-string redactor in `conversation.py` (no new imports; pinned
     by tests), so a misconfigured endpoint URL cannot leak credentials
     into a bubble or a log. Reasons are diagnostic text only — they
     never substitute a reply, and the failure bubble states the task
     was submitted, never that it executed.
   - **The `speaking` state adds no capability**: it is a renderer
     pose with the same structural guarantees as every other state
     (only entered on valid transitions, auto-returns after 120 s,
     renderer errors contained).
7. *Wake-word monitoring (v0.10) adds one opt-in audio-reading mode
   and no new trust decision.* Stated precisely:
   - **Off by default** — the application never listens unless the user
     flips `wake_word_enabled`; the same switch is the immediate stop
     control, and monitoring runs only while the character is calm
     (never during the morning sequence, never mid-turn).
   - **Audio contract**: ambient audio is read ONLY while monitoring,
     flows through a bounded drop-oldest queue (≤ 12.8 s worst case)
     and a bounded 112 KB rolling window, is decoded locally (vosk +
     the local speech model) and **never written to disk, never
     logged, never transmitted** — the wake path cannot reach Ollama
     (AST/meta-tested: no network library is importable in the module,
     no file writes, `wave`/`audioop` remain banned package-wide).
     Only the user's spoken *command*, after an acknowledged trigger,
     becomes a transcript — and it enters the same validate → detect →
     `manager.submit()` path as typed text (same allowlist, same
     permissions, same approval; the wake phrase itself is never
     submitted).
   - **Exclusivity**: the monitoring session stops itself before a
     turn's capture opens (no overlapping sessions), cancellations and
     teardown discard all buffered audio, and `listening` reports the
     real capture state (never "listening" after a device failure).
   - **Verification is local and language-guarded**: acceptance needs
     the exact phrase in a local decode AND detected `en` — a hotword-
     biased decode of foreign audio cannot fabricate a trigger
     (measured), so monitoring cannot be tricked into starting a turn
     by non-English ambient streams. False-trigger measurements and
     sample counts are reported by `scripts/verify_wake_word.py`;
     detection on real acoustic input was BLOCKED on the development
     machine (no working microphone — reported, not faked).
8. *Conversation context & routing (v0.11) add no persistence and no
   new trust decision.* Stated precisely:
   - **In-memory only**: session history lives in a bounded deque
     inside `ConversationService` (2–20 exchanges × 400-char entries,
     default 8) — never written to disk, never logged, discarded with
     the service (application run) or on the explicit **Clear
     conversation** control; no database, no "personal memory", no
     side-channel retention (module allowlist still bans all file I/O;
     tested).
   - **History is untrusted data**: it is quoted into the *system*
     prompt with explicit rules — never followed as instructions,
     never overriding safety/permission/approval rules — and the reply
     prompt keeps the *latest* turn's language (history cannot steer
     it). Redaction of failure reasons (item 6) still applies with
     history present (tested).
   - **Routing cannot authorize anything**: the injected predicate is
     the deterministic parser's own verdict, evaluated inside the
     engine-free conversation module. Actions go through the unchanged
     validate → permission → approval → verification chain; chat
     never reaches the manager (so it can neither execute nor create
     fake task history); predicate *errors fail closed* (submitted as
     actions). Voice/typed input share the identical path — speech
     still cannot bypass approval (tested). The model is told the true
     routing outcome (`action=` clause) so a reply cannot claim an
     action that was never sent, and the conversational failure bubble
     no longer says "still sent" when nothing was.
   - **Interruption remains counter-guarded**: cancelled/superseded
     turns drop their late ASR/reply/TTS results by serial
     (including the v0.11 stale-completion guard), `_cancel_turn`
     stops speech and releases its audio, and the sliced bus wait
     restores user control within ~0.2–0.5 s (measured 117 ms) —
     still one exclusive capture owner (no hot-mic barge-in; push-to-
     talk stays the fallback).

### 4. Verification before success

* The verifier re-resolves paths and re-inspects the filesystem
  independently of the executor's claim: exists, is a directory, is
  a file, content matches, listing matches.
* v0.2 documents are verified against what was actually approved:
  text reports are re-rendered from the approved content and compared
  **byte-for-byte**; PDFs are structurally validated (signature,
  catalog, object count, `startxref`, `%%EOF`) — not byte-compared,
  because PDFs embed creation metadata; workbooks are **reopened** and
  compared sheet-by-sheet, cell-by-cell (formula strings included)
  against the approved spec.
* If execution failed, or verification finds the world missing or
  altered, the task is reported as **failed** with the real error.
  A handler that "claims" success without doing the work — or that
  writes different bytes than approved — fails verification (tested).
* v0.3 `open_file` verification re-resolves the path (still inside
  the roots, still a regular file) and requires the execution result
  to contain a real reported process start; a lying launch adapter
  that claims success without `started=True` fails verification
  (tested). The verified claim is deliberately narrow: *"the process
  was started"* — never that the application displayed, edited or
  recalculated anything.
* v0.4 automation verification is evidence-based and adapter-independent:
  an application-state report must be internally consistent with its
  evidence (a `document_open` claim without a live accessible
  connection fails verification — tested with a lying adapter), a
  reported control must match the validated locator's role and name
  (case/whitespace-insensitive; a lying adapter pointing at the wrong
  control fails — tested), entered text is only verified when the
  field read-back matches, and a save verifies only when the file on
  disk matches the sha256 digest recorded after the Save control was
  activated — the verifier re-reads the disk itself rather than
  trusting the execution record (tested). For `task_sequence` the
  verifier re-verifies **every executed step independently** using its
  recorded data, so one lying step fails the whole task.

### 5. Audit log

* One JSON line per task attempt (including rejected/denied/
  unsupported) in `<state_dir>/audit.jsonl`, directory 0700, file
  0600, written via `os.open` with `0600`.
* Fields: task id, UTC timestamp, parser, proposed action,
  permission decision, approval, execution, verification, status,
  and any failure reason. Application tasks therefore carry the
  integration id, requested capability, permission operation
  (`open`/`detect`), approval verdict, launch outcome (started flag,
  executable path, pid, returncode) and verification verdict — but
  never file contents. Automation tasks carry the application id,
  the requested capability (`automate`), the reported honest state,
  the identified control's role/name and the save digests — and
  entered text is redacted exactly like file content (tested).
* Redaction: `content`, `spec`, passwords, tokens and similar keys
  become `<redacted:N chars>`; user-file content embedded in the raw
  command string is masked too (inline structured JSON is redacted as
  a whole `{...}` region); all strings are truncated at 400 chars.
* Audit-write failures are surfaced to the user as a visible note —
  never swallowed silently.

### 6. Local AI runtime

* Ollama only: base URL defaults to `http://localhost:11434`. No
  cloud endpoint exists in the code.
* The client never calls `/api/pull`; models are never downloaded
  (asserted by tests).
* `OLLAMA_MODEL` has no default — no model is assumed to be
  installed. Missing runtime, timeouts, missing models and malformed
  responses produce typed, actionable errors and a visible fallback
  to the deterministic parser.
* Even when the LLM proposes an action, it goes through
  `validate_action()`; an out-of-allowlist proposal is discarded. For
  application actions, `application`/`integration` must be registered
  ids — a model cannot name an executable, a path or a flag, and no
  such field exists in the schema.
* The system prompt explicitly forbids referencing files as content
  sources, and the schema gives model proposals no `source` key at
  all — a model can never trigger a file read, only inline content.

### 7. Operational

* Never run Alwin Fusion with `sudo`. Nothing in the code requires
  or attempts privilege escalation.
* The core, all v0.1 features, text reports and every security
  control run on the Python standard library alone. `openpyxl` and
  `reportlab` are optional, loaded lazily, and only ever fed
  already-validated data. Alwin Fusion **never installs packages
  itself**; `requirements.txt` documents the pinned versions and the
  user installs them explicitly.
* Tests run in temporary sandboxes (`ALWIN_HOME`-style injection) and
  never modify the real home directory. Application tests inject fake
  `popen`/`which` callables, and automation tests inject a fake AT-SPI
  module (in-memory accessibility trees), fake `/proc` directories and
  fake clock/sleep callables — so the automated suite never starts a
  real desktop application and never inspects or drives a real
  desktop. The two manual smoke tests are separate, explicit opt-ins
  (`ALWIN_MANUAL_SMOKE=1` for a real launch;
  `ALWIN_AUTOMATION_SMOKE=1` for real AT-SPI automation, every step
  approval-gated, inside a throwaway sandbox home).

## Known limitations (read this honestly)

1. **No sandbox.** The core runs as a normal user process with your
   full user privileges. It confines *its own* filesystem actions; it
   does not contain a compromised Python runtime, malicious site
   packages, or anything else you run.
2. **TOCTOU races.** Path resolution and the subsequent `mkdir`/
   `open` are two steps. Another process with write access to a
   permitted root could swap a component for a symlink in between.
   A production version should use `openat`-style dirfd APIs or
   `O_NOFOLLOW`. Not implemented in this milestone.
3. **Approval is the last line of defense.** A confused or hostile
   model (prompt injection) could propose an *allowed* action at an
   *unwanted-but-permitted* path. Validation cannot judge intent —
   the human approval prompt is the mitigation. `--llm` is off by
   default.
4. **Audit log is not tamper-proof.** It is a plain local file with
   restricted modes. The same user (or root) can edit or truncate it.
   No hashing/chain-signing yet.
5. **Single-user, single-process.** No file locking between
   concurrent Alwin Fusion instances; two simultaneous creations of
   the same path will have one winner (`O_EXCL` handles the file
   case safely).
6. **Roots are home-confined by policy, not by OS enforcement.** A
   bug in `PermissionChecker` would be a security bug; the checks are
   unit-tested but this is application-level, not kernel-level,
   confinement.
7. **Reads are bounded but not streamed** (1 MiB for `read_file`,
   512 KiB for `using *.json` content sources). The source file for a
   document is read in one go; very large structured content must be
   split across files or reduced.
8. **`~/Documents` may not exist** on a fresh system; creating the
   root itself is permitted, but only with an explicit approved
   action.
9. **Content redaction is best-effort string matching.** If a command
   is rewritten by a future parser in a way that hides the content
   substring, the audit falls back to replacing the whole command
   with a redaction marker. Inline structured JSON is masked as a
   `{...}` region; if a command contained JSON-like text *before* the
   content region, the mask could start too early (it errs toward
   over-redacting, never under-redacting, but review `audit.jsonl`
   after unusual commands).
10. **No delete/overwrite yet** — this is a deliberate milestone
    restriction, not a oversight; when added, it must reuse the same
    approval + verification + audit stages with extra safeguards.
11. **Formulas are stored, never calculated.** openpyxl (like Alwin
    Fusion) writes formula text without computing results; the
    verifier confirms the formula *strings* survive a round-trip but
    cannot check a computed value. A spreadsheet application must
    open the file to calculate. Verification never claims a
    calculated result.
12. **PDF verification is structural, not semantic.** PDFs embed
    creation timestamps and IDs, so byte-for-byte comparison is
    impossible; the validator checks signature, catalog, object
    count, `startxref` and `%%EOF`, which proves the file is a
    structurally sound PDF — not that every glyph matches the
    approved text. Text is drawn with built-in Helvetica fonts and
    encoded latin-1: characters outside that set (CJK, emoji, most
    accents) are replaced with `?` rather than rejected (tested that
    it does not crash). Also, the PDF *content* a user approves is
    the JSON they supplied; reportlab's interpretation of it is
    trusted.
13. **Formula validation is a grammar, not an evaluator.** The
    allowlist-based tokenizer rejects known-dangerous constructs
    (network functions, external/cross-sheet links, out-of-grid
    references, macros), but it is not a full Excel formula parser.
    An exotic construct it fails to recognise is rejected (fail
    closed), so the residual risk is a *weird but harmless* formula
    getting through, not a dangerous one — still, this is
    application-level assurance only.
14. **Optional dependencies are trusted code.** `openpyxl` and
    `reportlab` run with full user privileges once installed; a
    compromised package would be outside Alwin Fusion's control.
    Versions are pinned in `requirements.txt` to make review easier.
15. **Not a sandboxed OS yet.** No process isolation, no MAC policy,
    no signed boot, no reproducible image — all future work under
    `os-build/`.
16. **A launched application runs with *your* full privileges and is
    outside Alwin Fusion's control.** Approving a launch hands the
    file to that program (or, via `xdg-open`, to whatever the desktop
    associates with it — including a different program than you
    expected). Alwin Fusion does not sandbox, monitor, constrain or
    communicate with the application afterwards, and a hostile
    document could exploit a vulnerability in the application itself.
    This is inherent to opening files with apps, not a bug — but it
    is why every launch requires explicit approval.
17. **Launch verification proves only that a process started.**
    `poll()` immediately after `Popen` cannot know whether a window
    appeared, whether the file actually rendered, or whether the
    application later crashed. There is deliberately no *launch-time*
    window inspection and no claim of visual review or formula
    recalculation from the launch path. v0.4's separate automation
    engine can inspect an already-running application over AT-SPI —
    but only when the desktop exposes it, and always as its own
    independently approved action, never as a silent upgrade of a
    launch result.
18. **Detection is heuristic and metadata-driven.** It reads PATH
    and `.desktop` file *names* only; a manually copied binary that
    is not on PATH, a `.desktop` file in an unsearched location, or a
    renamed wrapper can produce `not_installed` / `detection_failed`
    for software that is actually present (and vice versa: a stale
    `soffice` name on PATH with a broken install can report
    `available` until the launch itself fails). Desktop-entry
    *contents* are never read precisely so untrusted metadata cannot
    influence decisions, at the cost of ignoring `Hidden=true` and
    `NoDisplay=true` entries in reports.
19. **TOCTOU applies to opened files too.** The target is resolved
    and checked, then passed to the application by path; another
    process could replace the file between the check and the
    application's own open. Same root cause and mitigation as (2):
    dirfd/`O_NOFOLLOW`-style APIs are future work.
20. **Registry extensibility is code-reviewed, not enforced by the
    OS.** The schema validation and closed enums are strong against
    *untrusted* input, but a future change to `registry.py` itself
    (reviewed code) could add a dangerous definition — the test pins
    and review process, not cryptography, are what guard that.
21. **Automation sees only what accessibility exposes — which can be
    incomplete or wrong.** AT-SPI trees vary by toolkit and version;
    a control may be missing, mislabeled or renamed by an application
    update, and an application could expose *hostile* accessible data
    (a button named like another button). Mitigations: exact role+name
    matching against the validated locator, revalidation immediately
    before every mutation, sanitizing of all application text, and
    honest "not found or not exposed" failures — but ALWIN cannot
    prove the visible on-screen control is the one it actuated.
    Automation is restricted to reviewed applications (Writer/Calc)
    precisely to keep this surface small.
22. **Automation depends on a desktop setting Alwin Fusion will not
    change.** If assistive technology is disabled at the desktop level
    (e.g. GNOME `toolkit-accessibility=false`), LibreOffice does not
    register on the AT-SPI bus and every control operation honestly
    reports `process_started`/"state unknown". This is fail-closed by
    design; enabling the setting is a manual, user-side action (a
    global desktop change that could affect other applications'
    threat models — which is why ALWIN never does it itself).
23. **Text entry is confirmed by field read-back, not by
    application semantics.** ALWIN reads the input field back after
    typing and reports a mismatch honestly, but it cannot verify what
    the application *does* with the text afterwards (e.g. whether a
    later implicit action discards or transforms it). Similarly, the
    entered text is bounded (4096 chars) and restricted to `entry`
    fields — there is no path to bulk-edit document content.
24. **A digest-confirmed save confirms bytes on disk, not user
    intent.** If the application writes something other than what the
    user expected (a filter, an auto-correct, a different document in
    focus), ALWIN reports only that the file changed and the change
    verified — it does not semantically validate the saved document.
    The ≤ 2 s confirmation window can also miss a slow save, which is
    reported as honest failure ("state unknown"), never as success.
25. **Sequence steps are validated individually, not as a
    interaction whole.** Each step passes planning, permissions and
    verification, but no model checks whether step 3 makes sense
    after step 2 changed the application state — stop-on-failure and
    per-step verification catch *errors*, not *illogical-but-valid*
    plans. Approval shows the user every step before anything runs,
    and `--dry-run` enumerates the full sequence without executing.
26. **The task-history file is not tamper-proof and is never
    replayed.** `task_history.jsonl` is a plain user-owned file
    (modes 0600/0700) — the same user or root can edit it. It is
    validated against a closed schema on read, malformed/hostile
    records are skipped and counted, and nothing in Alwin Fusion ever
    executes or replays a record (there is no code path from history
    to execution), but a local attacker who can write it can still
    *forge what the history displays*. The audit log remains the
    authoritative record; the history is a convenience view.
27. **The v0.5 task window has not been tested on a clean
    installation.** GTK4/PyGObject availability, font metrics and
    window behaviour were verified only on the development machine
    (Ubuntu 26.04.1, GNOME/Wayland) via the opt-in smoke test and the
    headless lifecycle walkthrough. No clean-install support is
    claimed.
28. **The GUI approval bridge trusts the desktop session.** Approval
    travels from the GTK main loop through `TaskManager.approve()`
    (carrying the displayed plan digest) to the pipeline, which
    independently re-verifies the digest and re-checks permissions at
    execution time. This defends against a *changed plan* and against
    the UI granting itself permission, but it cannot defend against a
    compromised desktop session (a hostile process driving the same
    input devices as the user) — that is the same trust boundary as
    the CLI's `y/N` prompt.
29. **One task at a time; no queue.** The manager refuses a second
    submission while one is running. This is a deliberate simplicity
    (no inter-task ordering or scheduling guarantees), not a
    concurrency control: there is still no file locking between
    separate Alwin Fusion processes (see 5).
30. **The companion's character is procedural vector artwork, not
    hand-drawn frame animation** (updated in v0.8). Alwin is a
    full-body cartoon boy drawn with cairo and driven by a
    parameterized `Pose` system — real, elapsed-time-based motion
    (breathing, blinking, waving, typing, jumping) — and, since v0.8,
    lives in a **2D illustrated bedroom** (wall, floor, window with a
    sunrise, bed, bedside alarm clock, rug, desk with
    notebook/pencil/laptop, chair) that he genuinely **walks around**
    (smoothstep-eased world movement between named positions). There
    are no sprite sheets, no lip-sync, no 3D scene and the poses are
    simplified (the "getting out of bed" and "lying in bed" poses are a
    rotation + blanket layer, not a full articulated lying animation —
    stated honestly). The *states and mapping are real behaviour*; the
    per-state artwork remains replaceable via
    `companion/assets/character/<state>/frame.png` (scene-less mode),
    and a hostile-but-valid image file can at worst change what one
    state looks like (see 3d, new surface 2). The drawing core and the
    bedroom artwork are pinned to pure imports (cairo/math/… and
    cairo/math/typing/companion.scene) and the scene model to
    math/dataclasses/typing (tested).
31. **Voice is real but optional, local and imperfect** (updated in
    v0.9). The companion has push-to-talk microphone input, offline
    speech recognition, automatic language detection over 15 languages
    and — when the user opts in — spoken replies. Honest limits:
    - nothing is recorded to disk and nothing is uploaded; audio lives
      in memory for one turn (enforced by banned modules and
      no-file-I/O meta-tests),
    - the microphone opens only on an explicit click (push-to-talk) or,
      since v0.10, while the **opt-in** wake-word switch is on (default
      off; bounded in-memory buffers; no other continuous listening —
      see the wake-word bullet below),
    - recognition requires a locally installed model (manual,
      confirm-first installer; the app itself **never downloads**);
      without it, an honest error — never an invented transcript,
    - speech requires local espeak + an audio device and is **off by
      default**; a missing engine keeps `NullVoice`'s honest contract,
    - recognition quality is language-dependent (measured on FLEURS
      human speech, 10 samples/language, default `small`, auto-decode:
      es/en/pt/fr/ru/id/ar/zh strong, ta/hi usable-but-noisy
      WER 0.44/0.45, te/ml/kn/ur/bn weak with WER ≥ 0.95; a
      25-configuration probe proved no setting rescues te/ml/bn —
      a model limitation, not configuration) — transcripts are shown
      verbatim so errors are visible
      and correctable, and typing remains fully supported. No
      15-language end-to-end perfection is claimed,
    - the `speaking` animation is simple mouth motion (no lip-sync
      tracking), and an LLM reply is never fed back into the task
      pipeline.
    - **wake word (v0.10) is opt-in and measured**: 25/25 recall on
      synthesized utterances and 0/149 false accepts on human-speech
      fixtures (stated sample counts, not statistical rates); the gate
      accepts only English windows, so foreign ambient audio cannot
      fabricate a trigger. Physical acoustic detection could NOT be
      demonstrated on the development machine — its only capture device
      carries line noise with zero acoustic coupling (measured; the
      verification script reports BLOCKED with the evidence instead of
      claiming success). Wake-word quality for other languages of the
      phrase is untested, and this says nothing new about the 15-
      language recognition table above.
    - **context & routing (v0.11) are bounded, local and English-gated**:
      history is in-memory only (nothing survives a restart; no
      personal memory feature), bounded 2–20 exchanges and quoted as
      untrusted data — follow-up answer *quality* depends on the local
      model (verified with `qwen3:0.6b`; other models vary). Routing
      uses the English deterministic parser, so a command spoken in
      another language becomes a conversational turn with **no task
      record** — the reply prompt forbids claiming the action ran, but
      the user must repeat it in English (or type it) to execute it.
      No hot-mic barge-in exists (one exclusive capture owner);
      interrupted speech now returns control within ~0.5 s (measured
      117 ms), and a *real* spoken multi-turn conversation could not be
      demonstrated on this machine (no working microphone — BLOCKED,
      reported with evidence).
32. **The companion's "walking" is movement inside a 2D room, not
    desktop locomotion**, and the companion is
    a small character window — not a desktop shell, not a wallpaper
    overlay; it never draws over or blocks other applications. The
    character moves between positions *within his room* (bed, greeting
    spot, desk) — he never leaves the window, never follows the mouse
    and never touches the desktop. Always-on-top is off by default and
    best-effort (Wayland compositors may ignore it).
33. **The companion window has not been tested on a clean
    installation**, same as limitation 27: verified on the development
    machine (Ubuntu 26.04.1, GNOME/Wayland, GTK 4.22) via the opt-in
    companion smoke test, headless `--check`/`--smoke` and programmatic
    checks. Companion settings (`companion_settings.json`) are
    cosmetic-only and not tamper-proof (see 3d, new surface 1).
34. **Desktop control is bounded by what the accessibility bus
    exposes — and compositors may refuse focus** (v0.13). Real
    observation on GNOME/Wayland: the Component `grab_focus` request
    is refused (`atspi_error 1`, cross-application focus is
    restricted by design); the controller falls back to the window's
    own `default.activate` action and then *verifies* by re-reading
    the active state — on this desktop the result is honestly
    UNVERIFIED/BLOCKED, never a claimed success. A window exposing
    no `window.close` action cannot be closed (Alwin never kills
    processes); an unsaved-work dialog keeps the window open and is
    reported, not overridden.
35. **Window identity for close verification is role + title** (v0.13).
    The targeted window is considered gone when no window of that
    application matches the same role and sanitized title. Two windows
    of the same application with identical role and title are
    indistinguishable — closing one while an identical twin remains
    would honestly report the close as unverified (the poll still
    sees a match), never as a false success. Multi-window applications
    close the *first* window deterministically; the total count is
    reported so the caller can see the ambiguity.
36. **A launched application is outside Alwin's control — same
    boundary as limitation 16, now also for bare launches** (v0.13).
    `launch_application` starts a registered application with your
    privileges; Alwin does not sandbox, monitor or constrain it
    afterwards. `/proc` re-observation confirms the *process* only;
    no window, document or behaviour claim follows from it. Duplicate
    launches are handed to the running instance (application-defined
    behaviour) and reported as such.
37. **Desktop metadata handling is best-effort sanitizing** (v0.13).
    Window titles from other applications are untrusted: bounded,
    control-char stripped and scrubbed of credential-shaped text by
    deterministic regexes. A credential format the regex does not
    recognise would pass through into a task record/audit entry
    (bounded to 80 chars); the scrubber errs toward over-redacting
    common patterns (`password:`, `token=`, `Bearer …`), it is not a
    general secret detector. No window *contents* are ever read, so
    secrets inside documents never reach Alwin at all.

## Reporting security issues

Open an issue in the project repository with the label `security`,
including the version, your configuration (roots, not personal file
contents), and reproduction steps. Do not include real secrets or
personal file contents in reports.
