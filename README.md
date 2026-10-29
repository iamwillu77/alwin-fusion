# ALWIN FUSION

**Open-source, offline-first AI-native operating system — in progress.**

Version **0.9.1** · Milestone: **End-to-End Voice Intelligence**

Alwin Fusion lets you control your computer through natural-language
commands. v0.9 gives **Alwin**, the persistent, lightweight desktop
companion (`companion/`), **multilingual voice intelligence**: he now
listens (push-to-talk microphone, nothing recorded to disk), recognizes
speech offline with a local Whisper model, **detects which of 15
languages you used** (auto-detection on by default; a fallback
preference never overrides a clear result), submits your words through
the *same* approval-gated task pipeline typed input uses, and — if you
opt in — replies in that language with a local voice. Every engine is
probed and optional: no microphone, no model, no espeak or no audio
device each degrade to an honest message, never a fake transcript or a
silent English fallback. It builds on the unchanged trusted core (parse
→ plan → permission → approval → execute → verify → audit) and on the
v0.5 task experience (`taskui`); there is no second execution engine.
Fully offline, with an optional local Ollama model that is never
required and never auto-downloaded.

v0.8 gave the same companion an **animated bedroom world**: he lives in
an illustrated room (pastel wall, window with a sunrise, bed, bedside
table with an alarm clock, rug, desk with a notebook, pencil and laptop,
chair) and genuinely **moves between places** — he wakes up in bed, gets
up, and *walks* to his greeting spot or to his desk, driven by real
elapsed-time movement (not frame counting). Writing/coding tasks walk
him to the desk, where the notebook/pencil or laptop animate; thinking
returns him to the greeting spot; an approval or failure stops the work
animation and shows an honest waiting/error state. The character is the
same recognizable **full-body cartoon boy** from v0.7 (expressive head,
tousled hair, blue hoodie, jointed arms/legs) drawn procedurally with
cairo. He mirrors *real* task states truthfully, shows a configurable
startup greeting, and stays strictly outside the trusted pipeline — the
scene can move him around his room but can never grant a permission or
change an outcome.

```
ALWIN FUSION v0.9
Local AI Core + Documents + Applications + Desktop Automation
Task experience: python3 -m taskui · Companion: python3 -m companion

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

And, next to it, the companion:

```
$ python3 -m taskui --companion
┌─────────────────────────┐
│  Hi Friend! Good        │   ← speech bubble (text; spoken aloud
│  morning! What shall     │      only if you opt in — v0.9 voice)
│  we do today?            │
│          ╭─────╮         │
│         ▕▟█▙▀▟▙▛▜        │   ← Alwin: a full-body cartoon boy
│          │ ◕ ‿ ◕│        │      (tousled hair, blue hoodie,
│          │ ╭─A─╮ │       │       emblem, arms, legs, shoes)
│          │ │   │ │       │   ← original procedural vector artwork,
│          ╰─┤   ├─╯       │      animated (breathes, blinks, waves)
│         ╱  ╰───╯  ╲      │
│  Alwin is thinking.      │   ← accessible subtitle (text alternative)
│  Planning…               │   ← minimal task status
│ [Open task window][Mic][Settings][Hide]   ← push-to-talk (v0.9)
└─────────────────────────┘
```

The companion is presentation only: it reads the same curated task
records the task window renders and can do nothing else — approval,
execution and verification stay where they were.

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
- **Alwin companion** (`companion/`, v0.9) — a persistent, lightweight
  desktop character beside the task window (`python3 -m taskui
  --companion`, or `python3 -m companion` alone): a small GTK4 window
  showing an **original full-body cartoon boy** (large expressive head,
  tousled dark-brown hair, big brown eyes, a blue hoodie with an
  original ALWIN emblem, jointed arms with hands, legs and red
  sneakers) drawn procedurally with cairo — no image assets, no new
  dependencies — in one of **17 explicit states** with validated
  transitions and safe fallbacks. Every state reuses one shared `Pose`
  system (`companion/character.py`), so proportions, hair and clothing
  stay identical while the pose and face animate: idle breathing and
  natural varied blinking, a greeting wave, thinking (hand to chin),
  listening, **speaking** (v0.9: subtle mouth motion while a reply is
  audible), a walking swing, distinguishable **writing** and **coding**
  poses at a desk (notebook with a scribbling pencil; laptop with
  floating code glyphs), reading, verifying with a magnifier, a wrench
  for general work, celebrating **only** from a trusted `completed` (a
  failed task always shows a concerned `error`, never a success
  animation), and calm idle. Animation is a pure function of *elapsed
  time* (correct even with delayed frames) and the frame timer stops
  while the window is hidden. A task-event adapter maps real
  `TaskRecord` states onto character states. Speech bubbles and
  subtitles are bounded plain text; the startup greeting is a sequenced
  wake-up → wave → bubble → idle flow, configurable
  (`ALWIN_COMPANION_NAME`, enable toggle, quiet hours, once per
  session/day — never hard-coded, never re-greeted on redraw); the
  renderer sits behind a small interface a future 3D
  implementation can replace unchanged. The companion observes
  read-only records and is structurally incapable of approving,
  executing or changing anything (source-level meta-checks enforce that
  no companion module imports the execution engine or can shell out).
- **Multilingual voice** (`companion/` voice stack, v0.9) — talk to the
  companion instead of typing, through the *same* trusted pipeline:
  - **Push-to-talk capture** (`voice_input.py`): one **Mic** button
    starts the microphone, a second press stops and sends; cancel at
    any point **discards** the audio. Digital silence is refused
    *before* any model runs. Audio lives in memory only — **no
    recording is ever written to disk or uploaded** (pinned by source
    meta-checks: no companion module can open a file for writing from
    the voice path, `wave`/`audioop` are banned package-wide).
  - **Offline speech recognition** (`speech_recognition.py`):
    faster-whisper with a **local, explicitly installed** model
    (`tiny`/`base`/`small`/`medium`, default `small`, loaded with
    `local_files_only=True` — Alwin **never** downloads anything; install
    a model with `python3 scripts/install_voice_models.py`, which asks
    first and reports the size). Recognition runs on a worker thread,
    never the GTK main loop, and never invents words: the transcript is
    shown verbatim and empty/low-confidence results stay empty.
  - **Automatic language detection** (`language_detection.py` +
    `language_registry.py`): a **closed registry of exactly 15
    languages** (en, ta, hi, te, ml, kn, ur, zh, fr, ru, es, ar, bn, pt,
    id) with native-script samples and correct espeak voice per entry;
    detection combines Unicode-script profiling with a seeded statistical
    text detector plus the recognizer's own language guess — **no
    keyword lists**. Auto-detection is **on by default**; your preferred
    language is only a fallback for unclear input and never overrides a
    clear result. Mixed-script or ambiguous input is reported as
    *uncertain* and the companion **asks you** instead of guessing.
    Urdu and Arabic are distinct entries with correct right-to-left
    handling (no transcode, no substitution).
  - **Language-matched replies** (`conversation.py`): one shared
    conversation service drives typed *and* voice input — a single
    pipeline (validate → detect → `manager.submit()`), never a
    per-language assistant. The detected language is injected into the
    reply prompt as a plain instruction ("Reply in the language used in
    the user's latest message…"), and technical names are preserved.
    The reply text is display/speech only — it is **never re-submitted**
    as a task, and approval/permissions are untouched.
  - **Optional spoken output** (`voice_output.py`): local espeak-ng
    synthesis (ctypes, no subprocess) with a voice verified for every
    one of the 15 languages, played through GStreamer off the GTK
    thread; the character enters `speaking` and returns to idle on the
    engine's completion truth (success, failure or user-cancel). Speech
    is **opt-in** (off by default); a missing engine, voice or audio
    device is reported honestly and never faked.
  - **Honest degradation**: no microphone → the Mic button explains
    itself; no model → an install hint with the exact command; no
    espeak/audio → the text reply still shows. Nothing is ever claimed
    as heard, recognized or spoken before it actually happened.
  - **Wake word: not implemented** (v0.9). The settings switch exists
    but is disabled and labelled as unavailable — it will not be
    claimed until it is implemented and tested.
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
- Optional, for the v0.9 voice features (installed by **you**, with
  your own approval — Alwin Fusion never installs anything and the
  application itself never downloads a model):

  ```bash
  python3 -m pip install --user faster-whisper espeakng-loader langdetect
  python3 scripts/install_voice_models.py --status   # report only
  python3 scripts/install_voice_models.py            # install a model (asks first)
  sudo apt install espeak-ng                         # optional system voice data
  ```

  | Dependency | Enables | If missing |
  |---|---|---|
  | `faster-whisper` (+ an explicitly installed model) | speech recognition | the mic button explains what to install; **no transcript is ever invented** |
  | `espeakng-loader` / `espeak-ng` (15 verified voices) | spoken replies (opt-in) | replies stay text-only; status says "espeak-ng is not installed" honestly |
  | `langdetect` (small, optional) | full statistical text detection | detection degrades to script + recognizer hint (reported honestly; mixed-script input stays "uncertain") |
  | GStreamer with `autoaudiosrc`/`alsasink` (present on a normal GNOME desktop) | microphone capture + audio playback | clear "no audio device" errors; everything else keeps working |
  | a working microphone | push-to-talk input | honest "microphone unavailable" bubble; typing still works |

  Recognition needs a model you install explicitly with
  `scripts/install_voice_models.py` (it reports the approximate size and
  your free disk space before asking; `--status` never touches the
  network). **Alwin performs no model downloads, ever** — recognition
  loads with `local_files_only=True`. Hardware-dependent behaviour (real
  mic, real speakers) depends on your machine; every test in the suite
  fakes or injects these layers.

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
python3 -m taskui --companion      # v0.9: task window + Alwin companion together

# v0.9: the Alwin companion (animated character + multilingual voice)
python3 -m companion               # companion alone (its Settings can open
                                   # the task window in the same process)
python3 -m companion --llm         # local Ollama for optional spoken replies
python3 -m companion --no-greeting # skip the startup greeting this run
python3 -m companion --check       # headless self-check: state graph,
                                   # task mapping, greeting, all 17 drawings,
                                   # language registry + detection + voice probe
python3 -m companion --smoke       # scripted headless companion walkthrough
ALWIN_COMPANION_NAME=Ada python3 -m companion   # greeting name (never hard-coded)
ALWIN_COMPANION_SMOKE=1 scripts/manual_smoke_companion.sh  # opt-in real-GUI smoke

# v0.9 voice: install a speech model explicitly (asks first; reports
# the size and your free space — Alwin itself never downloads anything)
python3 scripts/install_voice_models.py --status   # what is installed
python3 scripts/install_voice_models.py            # install 'small' (~484 MiB)
python3 scripts/install_voice_models.py --model tiny   # or a smaller one

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

## The Alwin companion (v0.9)

### Launching it

```bash
python3 -m taskui --companion    # recommended: task window + Alwin side by side
python3 -m companion             # Alwin alone (Settings can open the task
                                 # window later — same process, same engine)
python3 -m companion --llm       # attach local Ollama for optional spoken replies
python3 -m companion --check     # headless self-check (no display)
python3 -m companion --smoke     # headless walkthrough (no display)
ALWIN_COMPANION_SMOKE=1 scripts/manual_smoke_companion.sh   # opt-in real window
```

Everything is manual: Alwin Fusion installs **no** autostart entry,
service, cron job or desktop-shell component, and it never modifies
system settings. If GTK4 or a display is missing, `python3 -m
companion` says so honestly and exits; the headless `--check`/`--smoke`
modes still prove the companion core.

### The 17 character states

| State | Entered when (trusted source) | Animation |
|---|---|---|
| `sleeping` | startup before waking / quiet rest | slow breathing, closed eyes, drifting `z` |
| `waking_up` | companion session starts | morning stretch + yawn, eyes opening, rising sun |
| `greeting` | startup greeting fires | raised hand waving several times + big smile |
| `idle` | nothing to do; returns after outcomes | subtle breathing + natural varied blinking |
| `listening` | you submit a request / open the mic (v0.9) | attentive wide eyes, raised brows, small nod |
| `thinking` | planning a plan / recognizing speech (v0.9) | hand at the chin, gaze up, drifting thought dots |
| `speaking` | a spoken reply is audible (v0.9) | upright posture, subtle mouth motion (~9 Hz, documented) while the voice engine reports audio |
| `walking` | plan approved — moving to work | alternating leg/arm swing with a body bob |
| `writing` | executing a document/text action | seated at a desk, scribbling pencil over a notebook |
| `coding` | executing a code/config file creation | seated at a desk, hands tapping a laptop, floating `< / >` glyphs |
| `reading` | executing a read/inspect action | book held open, eyes scanning left to right |
| `waiting_for_approval` | plan awaits your approval | raised open palm, hopeful face, pulsing `…` |
| `working` | executing anything else | hand holding a small wrench beside a turning gear |
| `verifying` | independent verification running | magnifying glass held up, gaze sweeping |
| `celebrating` | **only** a trusted `completed` result | arms raised, big smile, small jump + sparkles |
| `error` | failed / needs attention | concerned-but-friendly face, hand to head, sweat drop |
| `resting` | user quiet for minutes | slow breath, drowsy eyes |

Invalid transitions never raise: they fall back through a calm state
(and a truthful target such as `waiting_for_approval` is never lost to
that fallback). A failed, rejected or cancelled task **never** renders a
success animation; only the verified `completed` lifecycle state reaches
`celebrating`.

The `speaking` state (v0.9) is entered only where the transition graph
allows it and only after the voice engine has *actually* dispatched
audio; a reply that finishes while Alwin is already walking/working on
your task plays the audio and skips the animation rather than forcing an
invalid state. It returns to `idle` when the engine reports completion
(success, failure or user cancel alike); a 120 s auto-return is only the
safety net for a lost callback. The mouth motion is deliberately simple
(a documented sine oscillation — no lip-sync tracking) and stops with
every other animation while the window is hidden or reduced motion is
on.

### Talking to Alwin (v0.9 voice workflow)

The mic button is **push-to-talk** — one click cycles the whole turn:

1. click **Mic** while Alwin is idle → the microphone opens (`listening`
   with a live input-level meter; the level meter runs only while the
   window is visible),
2. speak, then click **Mic** again → capture stops; pure silence is
   refused with an honest message *before* any model runs,
3. recognition runs on a worker thread (the GTK main loop never blocks);
   the transcript appears **verbatim** in the bubble,
4. your words go through the **same** conversation pipeline typed input
   uses — one `ConversationService`, validate → detect →
   `manager.submit()`, the same allowlist, permissions and approval
   gates. A transcript is untrusted text, exactly like typing it,
5. language: auto-detection (default **on**) picks the language from the
   transcript + the recognizer's own guess; with auto-detection off your
   preferred language is forced into the decode instead. Detection that
   cannot decide honestly says so and asks you — it never invents a
   language,
6. with **Spoken replies** enabled (Settings, off by default) and
   `--llm`, the local model drafts a short acknowledgement *in the
   detected language* on a worker thread and espeak-ng speaks it; the
   character enters `speaking` and returns to idle on completion truth.
   If the model fails, returns nothing, or is not attached, the bubble
   says exactly that (v0.9.1: "I couldn't write a reply (the local AI
   model failed: …). Your request was still sent." — never a fake
   reply, and the microphone stays usable for the next turn),
7. click **Mic** during recognition/reply → the turn is **cancelled**:
   the in-flight result is discarded by generation counter and can never
   be delivered late; during speech it interrupts playback.

Failure paths are all honest: no microphone → an explanatory bubble;
no installed model → the exact install command; no espeak/audio device →
the text reply still appears (nothing claims to have been spoken); no
LLM, an unreachable Ollama, or an empty model response → an explanatory
bubble (v0.9.1) that names the stage and confirms the request was still
sent — **never** a fabricated reply, and the microphone is never left
disabled by an error. `NullVoice` (still
available for tests/injection) never claims to have spoken.

**Push-to-talk only in v0.9** — there is no wake word (the Settings
switch is shown disabled and labelled "not available yet"), no
continuous listening, and no hands-free mode; those are future work and
are not claimed.

### Speech-model and language quality (measured, honest)

Models are **never** downloaded by the application. Install one
explicitly:

```bash
python3 scripts/install_voice_models.py --status   # report, no network
python3 scripts/install_voice_models.py --model small   # ~484 MiB, asks first
```

| Model | Approx size | Notes |
|---|---|---|
| `tiny` | ~75 MiB | fastest, weakest — fine for wake-free short commands |
| `base` | ~145 MiB | quick, decent for clear speech |
| `small` (default) | ~484 MiB | the balance measured below |
| `medium` | ~1530 MiB | best overall, but see the weak-language caveat |

Recognition quality was measured on **real speech** (FLEURS
recordings) with the default `small` model, auto-detect: **14 of 15
languages** were recognized with a usable transcript (Urdu was decoded
as Hindi — forcing `ur` fixes it: similarity 0.96). Telugu, Malayalam
and Bengali remain weak even with `medium` (measured word similarities
0.11 / 0.00 / 0.07) — **no model size fixes them today**, so treat those
three as best-effort: the transcript is shown verbatim so you can see
exactly what was (mis)heard and correct it, and typing remains fully
supported. This is stated here rather than hidden because a
multilingual UI that pretends to perfect coverage would be dishonest.
Espeak TTS voices, by contrast, were verified for **all 15 languages**.

### The animated world (v0.8)

Alwin is no longer stuck at the centre of the window — he lives in an
**illustrated bedroom** and genuinely moves around it. The world is split
into a small, renderer-independent **scene model** and a cairo **room
drawing**, so the same logic drives today's 2D window and a future 3D
renderer:

- **`companion/scene.py` (the model, pure Python)** — scene objects with
  stable identity and bounds, four **named positions** (`bed`,
  `beside_bed`, `greeting`, `desk`), **elapsed-time movement**, the
  wake-up sequence, and a **closed command allowlist** (`wake`, `get_up`,
  `move_to`, `begin_writing`, `begin_coding`, `begin_thinking`,
  `begin_waiting`, `begin_error`, `begin_celebrating`, `return_idle`,
  `place`, `stop`, `reset`). It has no cairo, no GTK, no I/O and no task
  engine — it is unit-tested headless with a simulated clock.
- **`companion/bedroom.py` (the drawing, pure cairo)** — the pastel room:
  wall and plank floor, a window with curtains and a **sunrise** that
  brightens as he wakes, a bed with a pillow and a blanket that tucks
  over him, a bedside table with a **ringing alarm clock**, a rug, a wall
  clock, and a desk with a notebook, a pencil and a laptop. It reads the
  scene snapshot and never mutates it.

**Real movement.** A walk is a request with a start time, a duration and
a target; the current position is a pure function of *elapsed time*
(smoothstep-eased), so a delayed or dropped frame simply advances him to
the correct spot — movement never overshoots by frames. Repeating the
same request is a no-op; a new request replaces the old one from wherever
he currently is. He is anchored by his feet on the single walking plane
(soles on the floor at every window size), and the whole room **and** the
character scale uniformly when you resize — nothing stretches. An
off-plane or out-of-room destination is rejected without disturbing his
current position, and the failure is reported honestly.

**The morning sequence.** When the greeting preferences allow it, Alwin
starts asleep in bed (eyes closed, breathing, drifting `z`), then **wakes**
(the sunrise brightens, the alarm rings, he stretches and yawns), **gets
up** beside the bed, **walks** to the greeting spot, and **waves** with a
configurable greeting bubble (the name is never hard-coded) before
settling into `idle`. It runs once per window (never again on a redraw),
its timers are cleaned up when the window closes or hides, it respects
quiet hours and the disable toggle, and a **reduced-motion** setting
collapses it to a calm near-instant placement. A real task arriving
mid-sequence cancels it cleanly — the task never waits on the visuals.

**Scene-aware tasks.** A writing/coding task walks Alwin to the desk,
where the notebook-and-pencil or the laptop animate (driven only by
trusted action identifiers); thinking returns him to the greeting spot;
an approval or failure **stops** the work animation and shows an honest
waiting/error state; and celebration is shown **only** after a verified
completion. The scene controller never executes, approves or modifies a
task — it is only ever a place Alwin stands — and finishing a walk
animation is never treated as task success.

**Timers and cost.** All animation is elapsed-time based (~12 fps while
active, ~6 fps when calm), pauses entirely while the window is hidden and
resumes on show, and never leaks a duplicate. There is still **no** game
engine, no blur or particle system, and no AI model is used to move him —
it is plain cairo on the existing stack.

### Real behaviour, original procedural artwork

The *states, transitions and task mapping are real behaviour* — they are
driven by actual `TaskRecord` updates from the trusted pipeline, and the
*animation is real*: every state is a distinct procedural pose computed
by `companion/character.py` as a pure function of **elapsed time**
(correct even when frames are delayed), blended smoothly (~0.25 s)
across state changes, and drawn with cairo as an original full-body
cartoon boy **placed in the room** at his world position. There are no
shipped image assets; drop a `frame.png` into
`companion/assets/character/<state>/` to replace **one** state's art
(see the README there). Lively states animate at ~12 fps; the subtle
`idle` state ticks at ~6 fps, and the frame timer stops entirely whenever
the window is hidden — an idle companion costs almost nothing.

### Startup greeting and the morning sequence

Configurable and honest: the name comes from `ALWIN_COMPANION_NAME` or
your saved settings (default: `friend` — no name is hard-coded), it
fires **once per session** (never again on redraw) with an optional
once-per-day marker, respects the enable toggle and quiet hours
(`22:00-07:00`), and is **text only** — nothing is spoken, no
microphone is opened, and presence is never assumed. `--no-greeting`
skips it for one run. When the greeting fires, it plays the full
**morning sequence**: Alwin sleeps in bed, wakes (sunrise + alarm +
stretch), gets up, **walks** to the greeting spot, and waves with a smile
while the bubble appears, then settles back into `idle` through the normal
validated state machine. When the greeting is disabled or it is quiet
hours, he simply stands up at the greeting spot, ready — no sequence. A
**reduced-motion** preference (in Settings) shortens the whole thing to a
calm placement, and every timer is cleaned up when the window closes.

### How it connects to the task engine

`TaskManager.add_observer()` (added in v0.6, additive and read-only)
delivers the same curated `TaskRecord`s the task window renders to
`companion/adapter.py`, which maps them to character states and bounded
speech. The adapter holds no manager reference, exposes no
approve/reject/execute surface, and returns nothing; observers are
error-isolated, so a companion or animation bug can never affect a task.
Approval remains exclusively in the task window, where the validated
plan digest is shown.

### From 2D to 3D

Renderers implement one small interface (`companion/renderer.py`:
`show_state`, `set_expression`, `tick`, `attach`, `detach`,
`is_available`). A future 3D renderer swaps in behind exactly that
interface and reuses the state vocabulary, transition graph,
task-event adapter, greeting, speech content, controls **and the scene
model** unchanged; 3D-specific concerns (meshes, lighting, lip-sync) stay
out of `ai_core`/`integrations`. The v0.8 **scene model**
(`companion/scene.py`) is deliberately renderer-independent — it owns
where Alwin is and how he moves, while `companion/bedroom.py` is just the
cairo view of it — so a 3D renderer would draw the same room and the same
movement from the same model. v0.8 still ships **no** game engine or 3D
runtime — the room and character are plain cairo.

## Configuration

| Environment variable | Default | Meaning |
|---|---|---|
| `ALWIN_HOME` | your home dir | Home directory (used by tests) |
| `ALWIN_PERMITTED_ROOTS` | `Documents` | `:`-separated permitted roots; must be inside home |
| `ALWIN_STATE_DIR` | `~/.local/state/alwin-fusion` | Audit log directory |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Local Ollama endpoint |
| `OLLAMA_MODEL` | *(empty)* | Model name for `--llm`; never auto-downloaded |
| `OLLAMA_TIMEOUT` | `30` | HTTP timeout in seconds |
| `ALWIN_COMPANION_NAME` | *(settings, else `friend`)* | Companion greeting name |
| `ALWIN_COMPANION_GREETING` | *(settings, else on)* | `0`/`false`/`no`/`off` disables the startup greeting |
| `ALWIN_COMPANION_QUIET_HOURS` | *(settings, else off)* | `HH:MM-HH:MM` window in which Alwin does not greet |

Companion preferences (greeting on/off, name, quiet hours,
always-on-top, subtitles, reduced motion, and — v0.9 — microphone on
off, level meter, auto language detection on/off, preferred fallback
language, spoken replies, speech rate, voice, ASR model, wake-word
placeholder) persist to
`$ALWIN_STATE_DIR/companion_settings.json` (mode 0600, closed schema —
bool/str values only; unknown keys are ignored; this file is
cosmetic-only, never read by the trusted core, and can never grant a
permission or enable execution). Auto language detection defaults to
**on** and a preferred language only applies when detection is unclear;
spoken replies default to **off**.

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
files). The suite (974 tests) covers supported/unsupported commands,
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
companion is covered the same way (headless tests, no window and no
display): the 17-state controller (valid/invalid
transitions, calm fallbacks, truthful-target recovery, renderer errors
contained and recorded), the task-event adapter (every lifecycle state
mapped, approval → `waiting_for_approval`, **failed/needs-attention/
cancelled never reach `celebrating`**, action → writing/coding/reading
identification, sequence step mapping, bounded curated speech, no
manager/approve/execute surface), the greeting (name configurability,
session and once-per-day dedup, quiet hours, disabled preference,
0600 marker, unwritable-marker tolerance), the renderer seam (abstract
contract, null renderer, a stand-in "future 3D" renderer driving the
unchanged controller), the cairo artwork (all 17 states draw headless,
deterministic, visually distinct), asset fallback (missing/corrupt
`frame.png` degrade to the vector character), the pure presentation
view-model (bounded text, no approve/reject affordance, no digests or
error internals exposed), manager observers (dual delivery with
`on_update`, observer exceptions cannot break a task, a truthy observer
return approves nothing), and AST-based meta-checks (no companion module
can shell out/eval/socket, imports of the execution engine
are banned package-wide, only `__main__.py` may import `taskui`, the
state controller is pure, the window never renders markup or remote
resources, no user name is hard-coded). The v0.9 **voice stack** has its
own dedicated suites (all deterministic; hardware, models and the LLM
are faked or injected, no network): the closed 15-language registry
(including RTL Urdu/Arabic and per-language espeak ids), language
detection (15/15 code samples, Tanglish tolerance, mixed-script and
short/ambiguous input honestly *uncertain*, preferred-language fallback
never overriding a clear result), the shared conversation pipeline
(language switching across turns, one instruction-driven reply prompt,
technical names preserved, transcripts submitted verbatim, **no English
fallback and no per-language assistant**), microphone capture (start/
stop/cancel/release, silence rejected before any model, cancelled audio
discarded for good), recognition (worker-thread decoding, no-speech
rejection, forced-vs-auto language, honest failure surfaces, model-status
reporting), voice output (per-language voice selection, generation-
counter cancellation, completion truth on success *and* failure,
`NullVoice` never claims speech), the window's full push-to-talk cycle
(submit verbatim, cancel drops late results, uncertain → ask, spoken
reply enters `speaking` and returns to idle on engine truth, settings
apply live, teardown releases everything), settings schema (closed,
bool/str only, `mute_voice` → `spoken_responses` migration), the
security meta-suite (per-module import allowlists for the 20 companion
modules, `ctypes` reachable from exactly `voice_output.py`, no recording
persistence, transcripts reaching only `TaskManager.submit()`), and the
STATE_SPEAKING pose/transition/asset coverage. The v0.7 full-body
character is
covered by its own dedicated suite (`tests/test_companion_character.py`,
27 tests): non-empty and substantial drawing inside real image bounds at
five widget sizes (head, hands and feet never clipped), uniform
non-distorting scaling, pose determinism and unknown-state fallback,
`Pose.lerp` endpoint/midpoint blending, subtle idle breathing, natural
blinking (open + closed frames), elapsed-time animation (delayed frames
still land on the right pose), pairwise-distinct frames for the eight
key states (writing ≠ coding, thinking ≠ listening), the greeting wave
genuinely oscillating, **celebration only from a trusted verified
`completed`** (failed and needs-attention records never celebrate),
graceful handling of zero/tiny canvases, and expression overrides —
plus a source purity meta-test pinning `companion/character.py` to
cairo/math/dataclasses/typing only. The v0.8 animated world is covered
by its own suite (`tests/test_companion_scene.py`, 40 tests): scene
objects with stable bounds inside the design box, uniform non-distorting
scaling, smoothstep-eased elapsed-time movement (delayed frames land on
the right spot, invalid destinations are refused without disturbing his
position, an arrival fires once), the full morning sequence
(sleep → wake → get up → walk → arrive) plus its reduced-motion
variant, scene-driven writing/coding/thinking/waiting/error, the closed
command allowlist, deterministic headless room rendering, and a
desk-hand drift guard. The spreadsheet/PDF pipeline tests skip themselves politely when the
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

### Manual companion smoke test (opt-in, real Alwin window)

For a developer who wants to watch the real companion react to a real
task:

```bash
ALWIN_COMPANION_SMOKE=1 scripts/manual_smoke_companion.sh
```

Without the environment variable the script is a no-op. It reports the
environment, runs the headless companion self-check and walkthrough,
runs the companion's automated tests, then opens the real task window
**and** the real Alwin window (`python3 -m taskui --companion`) inside
a throwaway sandbox home so you can watch Alwin think, wait for your
approval, celebrate only a verified result and stay calm on rejection —
plus try its greeting, subtitle, settings and hide controls. Your real
`~/Documents`, state directory and companion settings are never
touched, and the sandbox is deleted on exit.

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
                the pipeline + read-only observers) · presenter (pure
                view-model) · status (honest AI/automation probes) ·
                gtk_app (thin GTK4 view) · __main__ entry point
companion/      states (17-state character controller) · character
                (full-body cartoon boy: shared Pose system, per-state
                procedural animation — pure cairo, headless) ·
                renderer (renderer interface + headless null renderer) ·
                renderer2d (GTK renderer: pose blending, adaptive frame
                cadence + per-state asset overrides) · adapter (trusted
                records → character states + bounded speech) ·
                presentation (pure view-model) · greeting (sequenced,
                configurable startup greeting) · settings (validated
                local JSON) · voice (engine interface — NullVoice) ·
                voice_input (push-to-talk capture, GStreamer appsink) ·
                speech_recognition (faster-whisper, local_files_only) ·
                language_registry (closed 15-language set) ·
                language_detection (script profile + text + ASR hint) ·
                conversation (shared typed/voice pipeline + replies) ·
                voice_output (espeak-ng ctypes + GStreamer playback) ·
                scene (renderer-independent bedroom world) · bedroom
                (cairo room drawing) · window (thin GTK4 view) ·
                __main__ entry point (--check/--smoke/--llm) ·
                assets/ (per-state art slots + convention README)
tests/          unittest suite (temporary sandboxes + in-memory
                accessibility trees only; no display, no desktop)
docs/           ARCHITECTURE.md · SECURITY.md
scripts/        run_tests.sh · install_voice_models.py (explicit,
                confirm-first speech-model installer — the app never
                downloads) · manual_smoke_apps.sh ·
                manual_smoke_automation.sh · manual_smoke_ui.sh ·
                manual_smoke_companion.sh (opt-in)
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
automated test suite. v0.5 adds the **task experience**:
a two-phase pipeline (`preview()` + `run_previewed()` with
digest-bound `ApprovalToken`s), the `taskui` package (validated task
lifecycle, redacted display-only task history, worker-thread manager,
pure presenter, honest capability probes, thin GTK4 window), and the
opt-in UI smoke test. v0.6 adds the **Alwin companion**: the
`companion` package (16-state character controller, renderer interface
with a cairo 2D implementation, task-event adapter, pure presentation,
configurable greeting, validated settings, honest voice placeholder,
thin GTK4 window), the read-only `TaskManager` observer feed, the
`--companion` launcher and the opt-in companion smoke test. v0.7
upgrades the companion into an **animated character**: a full-body
original cartoon boy (`companion/character.py`) driven by a shared
float-only `Pose` system — idle breathing, natural blinking, greeting
wave, thinking/listening/walking/writing/coding/reading/verifying/
working poses, a jumping celebration reachable **only** from a trusted
verified `completed`, and a concerned-but-friendly error — with
elapsed-time animation, smooth pose blending across state changes,
adaptive frame cadence, a sequenced morning greeting
(wake → wave → bubble → idle), animation timers stopped while the
window is hidden, and 27 dedicated headless character tests. v0.8 adds
the **animated world**: a renderer-independent scene model
(`companion/scene.py` — named positions, elapsed-time movement, the
wake-up sequence, a closed command allowlist) and an original cairo
bedroom (`companion/bedroom.py` — wall, floor, window with a sunrise,
bed with a tucking blanket, bedside alarm clock, rug, desk with
notebook/pencil/laptop, chair), so Alwin genuinely **walks** between the
bed, the greeting spot and the desk, driven by real task events, with a
reduced-motion option and 40 dedicated headless scene tests. v0.9 adds
**multilingual voice intelligence**: push-to-talk microphone capture
(`voice_input`, GStreamer, nothing persisted), offline recognition
(`speech_recognition`, faster-whisper with `local_files_only` models
installed only by `scripts/install_voice_models.py`), a closed
15-language registry and detection service (`language_registry`,
`language_detection` — script profiling + seeded text detection + the
ASR language hint, uncertain → ask), one shared conversation pipeline
for typed and voice input (`conversation` — language injected as a
reply instruction, never per-language assistants), optional local TTS
(`voice_output` — espeak-ng ctypes voices for all 15 languages,
GStreamer playback), the `speaking` character state (17th state,
documented simple mouth motion), the mic/level-meter/push-to-talk UI and
voice & language settings (closed schema, bool/str only), the `--llm`
wiring for optional replies (also fixing `taskui --llm`, which was
previously inert), and the honest wake-word placeholder. The suite is
now **974 tests**, all headless (voice hardware, models and the LLM are
faked or injected).

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
- **The character is procedural vector artwork, not hand-drawn frame
  animation** — Alwin is an original cairo drawing of a full-body
  cartoon boy driven by a parameterized `Pose` system: real motion
  (breathing, blinking, waving, typing, jumping) but no sprite sheets,
  no lip-sync, no full room/bed scene beyond per-state props, and no
  artist-polished frame-by-frame animation. Individual states can be
  replaced by dropping a `frame.png` into
  `companion/assets/character/<state>/`, and the renderer interface
  keeps the door open for a future 3D or sprite-based implementation.
- **Voice is real but strictly optional and locally installed** (v0.9):
  - The microphone only opens when *you* click the mic (push-to-talk);
    there is no wake word and no continuous listening — the wake-word
    Settings switch exists but is disabled and labelled "not available
    yet (v0.9)" because it is not implemented.
  - **Nothing you say is ever saved or uploaded**: audio lives in
    memory during a turn and is discarded (no `.wav`/recording files, no
    cloud ASR; `wave`/`audioop` are banned in the companion package and
    the voice modules cannot import any network library).
  - Recognition requires `faster-whisper` **and** a model you installed
    yourself with `scripts/install_voice_models.py` (explicit, confirms
    first, reports size + free space; the application itself performs
    **no downloads, ever**). Without them the mic button explains
    exactly what is missing — it never invents a transcript.
  - Speech output requires `espeakng-loader`/`espeak-ng` **and** an
    audio device; it is **off by default** (`spoken_responses` is
    opt-in). Without them, replies stay text-only — nothing claims to
    have been spoken, and `NullVoice` reports "no speech engine".
  - **Recognition quality is language-dependent and imperfect**
    (measured on FLEURS real speech with the default `small` model:
    14/15 languages usable; `ur` needs the forced language; `te`/`ml`/
    `bn` are weak with every current size). The transcript is always
    shown verbatim so you can see and correct it; typing remains a
    fully supported path. No end-to-end "15 languages perfect speech
    round-trip" is claimed.
  - Optional spoken replies need `--llm` with a running local Ollama;
    the reply is display/speech only and is never fed back into the
    task pipeline — your transcript is the only thing ever submitted,
    and always through the normal approval path.
  - The `speaking` animation is deliberately simple (documented mouth
    oscillation, no lip-sync tracking) and like all animation it stops
    while the window is hidden and under reduced motion.
- **Voice dependencies were exercised on the development machine**
  (Ubuntu 26.04.1) with a venv (`faster-whisper`, `espeakng-loader`,
  GStreamer capture/playback) and the installed Whisper models; a clean
  installation has **not** been tested, and hardware-dependent steps
  (real microphone, real speaker output) depend on your machine.
- **`walking` is a 2D waddle, not locomotion through a 3D world**, and
  the companion is a small character window — not a desktop shell, not
  a wallpaper overlay, and it does not draw over or block other
  applications.
- **Always-on-top is opt-in and best effort** — off by default, and
  Wayland compositors may ignore it entirely.
- **The companion window was exercised on the development machine**
  (Ubuntu 26.04.1, GNOME/Wayland, GTK 4.22) via the opt-in smoke and
  programmatic checks; a clean installation has **not** been tested.
- **The companion settings file is cosmetic-only and not
  tamper-proof** — a modified file can change greeting/subtitle/voice
  preferences at most; the trusted core never reads it. (The v0.8
  placeholder key `mute_voice` was replaced by `spoken_responses`; an
  old file simply loses the obsolete key on the next save.)
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
