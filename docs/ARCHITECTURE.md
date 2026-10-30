# Alwin Fusion — Architecture

Version: 0.20.0 — Milestone: Intelligent Task Planning and Recovery

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
    task_plan.py         v0.12 typed TaskPlan/PlanStep + validate_plan:
                         closed-set actions only, dependency rules (no
                         cycles/forward/self/duplicates), derived risk,
                         bounded sizes, derivation markers with
                         sanitising hoist of misplaced model keys;
                         v0.14: derivation sources must be EARLIER
                         steps (unknown/self/forward rejected at
                         validation) and are hoisted into depends_on;
                         v0.20: IDEMPOTENCY classification table
                         (read_only|repeatable_now|
                         reconcile_before_retry|never_auto_repeat,
                         unknown => never) and the pure
                         build_resume_plan() — excludes verified
                         completed steps (replayed, never re-run),
                         excludes dispatched-but-unverified mutations
                         (reconciliation list), keeps undispatched
                         failures and not-started steps as the
                         residual plan
    capabilities.py      v0.12 capability discovery: real probes (closed
                         actions, openpyxl/reportlab, app registry,
                         AT-SPI bindings, attached LLM, honest MCP
                         status = descriptors only, 0 executable) with
                         a TTL cache + invalidate(); v0.19:
                         application_tiers no longer claims
                         interact/window_state without install+evidence,
                         and application_readiness() returns structured
                         facets (installed/running/a11y each
                         available|unavailable|blocked|unknown; probe
                         exceptions => unknown, running-but-bus-absent
                         => blocked) with injectable bounded probes
    task_planning.py     v0.12 planning service: one bounded json_mode
                         generation proposes plan|clarify|decline via
                         the EXISTING Ollama client; validated + capability
                         checked + credential-scrubbed; deterministic
                         English goal gate; bounded validation-feedback
                         retry; bounded replan_remaining(); summarize_text()
                         for declared derivations (injection-resistant)
    schedule.py          v0.16 typed scheduled-job MODEL (pure, no I/O):
                         closed enums (job types reminder|task|check,
                         states enabled|paused|cancelled, schedule kinds
                         once|daily|weekly, condition ops file_exists|
                         file_updated|app_installed), bounded fields
                         (80-char title, 500-char task text, 200 jobs,
                         retries 0–3, one-time ≤ 5 y, recurring ≥ 300 s
                         apart), IANA-timezone validation with honest
                         machine-local resolution, deterministic
                         next_occurrence with documented DST rules
                         (ambiguous wall time = first occurrence,
                         skipped wall time fires shifted), credential-
                         shaped text refused content-free, and row
                         (de)serialisation that re-validates on read —
                         a tampered row is refused, never coerced
    jobstore.py          v0.16 persistent scheduler storage: its own
                         SQLite file (state_dir/scheduler.db, 0600 in
                         0700 — deliberately separate from memory.db so
                         privacy clear/disable never touches operational
                         scheduling and neither shares the other's
                         PRAGMA user_version), explicit migrations
                         (atomic, newer schema refused not downgraded),
                         parameterized queries, rowcount-checked
                         transactional writes, honest error taxonomy
                         (unavailable/locked/full/corrupt/schema/invalid/
                         not_found/exists), and an occurrences table with
                         UNIQUE(job_id, occurrence) = the claim primitive
                         (duplicate prevention + notification dedupe +
                         attempt history + dismissed flag)
    scheduler.py         v0.16 runtime: NO threads of its own — a
                         bounded tick() (≤ 5 dispatches) driven by the
                         host's GLib loop or an injected fake clock;
                         manager observer records are queued under a
                         lock and every store write happens on the tick
                         thread; occurrence claims are transactional;
                         missed occurrences coalesce (never a backlog);
                         unattended approvals expire (120 s) into
                         manager.cancel() — never a bypass; dispatch
                         only via manager.submit (preview → permission →
                         approval → execute → verify → audit); bounded
                         dispatch-failure retries; condition evaluation
                         through the real PermissionChecker (single
                         stat) and the real app registry
    schedule_parse.py    v0.16 deterministic English scheduling grammar:
                         create_reminder/create_task/list/inspect/pause/
                         resume/cancel/run_now/edit/question intents,
                         ambiguity flags (bare hour, missing day, any
                         recurring request) → needs_confirmation +
                         interpretation text; questions and vague
                         phrases ("check this regularly") can never
                         create a job; never raises on user text
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
                         callback ("executing"/"verifying"). v0.12 adds
                         preview_action(): stages 2.5-3 for an
                         already-validated plan step — permission,
                         proposal and digest computed under EXACTLY the
                         same rules as a typed command (shared
                         _preview_planned core), never executing.
    cli.py               `python3 -m ai_core.cli` interface
ai_runtime/              Local AI runtime integration
    ollama_client.py     stdlib-only Ollama HTTP client (never pulls models)
integrations/            Pluggable action handlers (the extension point)
    __init__.py          ActionDispatcher + build_default_dispatcher()
    filesystem/tools.py  list/read/create-only filesystem tools;
                         v0.18: replace_file_content (the ONE reviewed
                         overwrite primitive: approval-gated, atomic
                         O_EXCL-temp + os.replace, original preserved
                         on failure, symlink/missing/dir refused) and
                         read_file now refuses binary (NUL) files
                         instead of laundering them as text
    documents/           v0.2 document generation
        deps.py          lazy optional-dependency loading + clear errors
        report.py        deterministic plain-text rendering (stdlib)
        pdf.py           reportlab rendering + stdlib PDF validation
        spreadsheet.py   openpyxl workbook build + reopen/inspect
        tools.py         O_EXCL create-only handlers re-checking security
    apps/                v0.3 application integration engine
                         (+ v0.13 desktop intelligence & lifecycle control)
        registry.py      closed, schema-validated integration definitions
                         (v0.13: 'launch' capability, a11y_names,
                         launch-only integrations; 8 integrations)
        discovery.py     read-only detection (PATH + desktop-entry names)
        launch.py        argv-only LaunchAdapter (shell=False; file and
                         v0.13 bare launch_app — registry argv only)
        desktop.py       v0.13 DesktopController: launch (+ /proc
                         re-observation), status (installed/running
                         tri-state + bounded a11y window metadata),
                         active_window, focus (grab_focus + activate
                         fallback, verified by the active state), close
                         (window.close action + verified disappearance;
                         never a signal/force-kill); credential-scrubbed
                         titles, injectable clock/proc/session
        tools.py         detect/open_file handlers re-checking permissions
    automation/          v0.4 desktop automation engine (AT-SPI)
        base.py          state machine, control locators, capabilities,
                         role policies, bounds, untrusted-text sanitizing
        processes.py     read-only /proc observation (never starts processes)
        atspi_backend.py AtspiSession over gi.repository.Atspi (no gi = clean
                         BackendUnavailable; no keystrokes/mouse/shell ever);
                         v0.17: find_all-based ambiguity detection,
                         is_defunct staleness probe, shared
                         resolve_candidates/node_is_defunct helpers
        libreoffice.py   LibreOfficeAdapter: Writer/Calc root discovery,
                         find/read/activate/enter/save with revalidation;
                         v0.17: ambiguous locators rejected (never
                         first-match), defunct targets refused as stale,
                         bounded read-only list_controls, activation
                         outcome verified-or-unverified
        gnome_text_editor.py  v0.17 GNOME Text Editor adapter (bounded:
                         inspect/find/list/activate with post-action
                         verification; read/enter/save honestly
                         unsupported — the real GTK4 tree exposes no
                         document-text interface, measured on the host)
        tools.py         the automation handlers re-checking permissions
                         (v0.17 adds the bounded read-only list_controls)
        sequence.py      TaskSequenceRunner (stop-on-failure, per-step
                         records; optional step_observer for live progress;
                         v0.19: failed plans carry an explicit `partial`
                         block — completed/total/failed_step/changed/
                         category/next_action — and state in words when
                         earlier steps DID change state)
        mcp.py           future-MCP tool descriptors only (no server/
                         transport, therefore NO MCP-executable tool
                         exists — including any Blender integration;
                         capability discovery reports this honestly)
taskui/                  v0.5 task experience (UI-agnostic core + thin view)
    lifecycle.py         the task state machine (10 states) with a
                         validated transition table; TaskRecord (curated,
                         display-safe fields; the live pipeline preview
                         is kept out of serialization). v0.12 adds two
                         plan-boundary edges (EXECUTING->CANCELLED,
                         VERIFYING->CANCELLED) used ONLY between plan
                         steps when nothing is running — the single-
                         action rule that running actions are never
                         interrupted is unchanged, plus a runtime-only
                         TaskRecord.task_plan field (never serialized).
    history.py           TaskHistory: persistent, validated, display-only
                         JSONL task history (<state_dir>/task_history.jsonl,
                         0600/0700); schema-validated on read; malformed
                         records skipped and counted, never executed
    manager.py           TaskManager: the ONLY bridge between a UI and
                         the trusted pipeline; one worker thread per task;
                         approval gate blocks on an event the UI releases
                         via approve()/reject()/cancel(); never executes
                         anything itself. v0.20 adds resume_task(): a
                         FAILED/needs_attention plan resumes as a NEW
                         audited record (resume_of link) containing only
                         the residual steps, replayed verified results,
                         a FRESH plan-level approval digest, an
                         execution-aware dispatched flag (known
                         zero-effect failures stay resumable), a bounded
                         resume budget (MAX_RESUMES=1), dependency
                         satisfaction via replayed results, and a
                         deterministic overwrite-clarification note.
                         v0.12 adds the PLAN controller
                         inside that same worker: unsupported objectives
                         consult TaskPlanningService (plan -> one
                         digest-bound plan-level approval, mirroring
                         task_sequence; clarify -> honest failed record
                         with the question; decline -> cancelled with
                         reason; unavailable -> existing behaviour), then
                         each step runs preview_action -> run_previewed
                         sequentially (max concurrency 1) with per-step
                         digest tokens, 300 s budget, one retry for
                         read-only steps only, boundary cancellation
                         preserving completed work, dependency skipping,
                         and a digest-guarded single replan after
                         zero-execution failures
    presenter.py         pure, headless view-model (no GTK, no I/O):
                         submission validation, plan badges, risk notes,
                         step rendering (incl. the v0.12 plan-step states
                         succeeded/awaiting_approval/verification_failed/
                         cancelled), verification evidence, history
                         rows/detail, status labels, bounded untrusted text
    status.py            honest capability probes (AI runtime reachable?
                         model configured/installed? automation bindings
                         importable? offline posture) — read-only, injectable
    gtk_app.py           thin GTK4 (PyGObject) view: renders presenter
                         dicts, forwards intents to the manager, marshals
                         worker-thread updates with GLib.idle_add; inserts
                         ALL dynamic text as plain text (never markup)
    __main__.py          `python3 -m taskui` entry point (--llm, --dry-run,
                         --companion opens the task window together with the
                         Alwin companion, --check headless self-check,
                         --smoke scripted headless lifecycle walkthrough)
companion/                 v0.9 Alwin companion (presentation layer only)
    states.py            the 17-state character controller: validated
                         transition graph, calm safe fallbacks, truthful
                         target recovery, expression/label tables, auto-
                         return delays, error-isolated renderer dispatch
                         (an animation exception is recorded, never raised)
    scene.py             v0.8 renderer-independent world model (pure
                         Python, headless): scene objects + named
                         positions (bed/beside_bed/greeting/desk),
                         elapsed-time smoothstep movement, wake-up
                         sequence phases, arrival events and a CLOSED
                         13-command allowlist (never executes/approves)
    bedroom.py           v0.8 the illustrated room (pure cairo, headless):
                         wall/floor, window with sunrise, bed with a
                         tucking blanket, bedside alarm clock, rug, desk
                         with notebook/pencil/laptop, chair — reads a
                         SceneView snapshot, never mutates the scene
    renderer.py          the renderer interface (CharacterRenderer ABC:
                         show_state/set_expression/tick/attach/detach/
                         start/stop/is_available + set_scene for a scene-
                         aware renderer) — the seam a future 3D
                         renderer implements — plus NullRenderer for
                         headless use
    character.py         the full-body character drawing core (pure cairo,
                         headless): a shared float-only Pose dataclass
                         (head/face/breath/hand-target knobs), per-state
                         pose builders computed from ELAPSED TIME (never
                         frame counts), a natural blink rhythm, two-link
                         arm IK, shared components (head/hair/eyes/hoodie/
                         arms/hands/legs/shoes), per-state props, and v0.8
                         draw_character_placed (feet-anchored world
                         placement, rotation, furniture/prop flags)
    renderer2d.py        the GTK implementation: re-exports the character
                         core, blends poses (~0.25s) across state changes,
                         adaptive frame cadence (~12fps lively, ~6fps
                         idle), optional <state>/frame.png overrides
                         (missing/corrupt asset = graceful vector
                         fallback), and the v0.8 scene loop: advances the
                         scene each frame, emits arrival events, draws
                         room back → placed character → room front
    adapter.py           TaskEventAdapter: the ONLY bridge from trusted
                         TaskRecords to character states + bounded curated
                         speech; celebrates only on `completed`, maps
                         approval to waiting_for_approval, holds no manager
                         reference and exposes no approve/execute surface
    presentation.py      pure view-model (bubble/subtitle/task status/
                         controls/settings) — bounded text, no approval
                         affordance, no digests or error internals
    greeting.py          configurable startup greeting: name from
                         env/settings (never hard-coded), quiet hours,
                         once-per-session + optional once-per-day marker
    settings.py          closed-schema local JSON preferences (0600,
                         bool/str only, cosmetic-only — the trusted core
                         never reads it); v0.9 voice keys (mic, level
                         meter, language auto/fallback, spoken replies,
                         rate, voice, ASR model) + v0.10 wake keys
                         (wake_word_enabled default OFF, wake_phrase)
                         + v0.11 context keys (context_enabled default
                         ON, context_turns "8" clamped 2..20)
    voice.py             VoiceEngine ABC + NullVoice (honest "no engine
                         connected") — the injection seam for tests
    voice_input.py       v0.9 push-to-talk capture: GStreamer
                         autoaudiosrc → appsink (16 kHz mono S16) on a
                         worker; start/stop/cancel/release; silence is
                         rejected before any model runs; PCM lives in
                         memory only (never written to disk). v0.10 adds
                         an optional on_sample streaming observer (the
                         wake-word service receives every chunk in
                         addition to — never instead of — the bounded
                         capture buffer; a raising observer is isolated)
    speech_recognition.py v0.9 offline ASR: faster-whisper with
                         local_files_only=True (the app NEVER downloads),
                         decode on a worker thread, no-speech/short-audio
                         rejection, forced-vs-auto language, honest
                         model-status reporting (install via
                         scripts/install_voice_models.py). v0.10 adds
                         decode_window(): hotword-biased + AUTO-language
                         short-window decode sharing THIS service's
                         engine (one model in RAM) — the wake-word
                         verifier's stage 2
    language_registry.py v0.9 the CLOSED set of 15 languages (code, name,
                         native sample, script, RTL flag, espeak voice) —
                         the single source of truth for detection, the
                         settings dropdown and TTS voices
    language_detection.py v0.9 detection service: Unicode-script profile
                         + seeded langdetect text evidence + the ASR
                         language hint; dominant-script guard for mixed
                         input; uncertain results reported as uncertain
                         (ask the user, never guess); preferred language
                         is a fallback that never overrides clear
                         evidence
    conversation.py      v0.9 ONE shared pipeline for typed and voice
                         input: bounded transcript → detection →
                         duck-typed manager.submit() (same allowlist/
                         permissions/approval as typing); optional reply
                         generation with the language instruction prompt;
                         engine-free (no GTK, no LLM import at module
                         level); replies are display/speech only and are
                         never re-submitted. v0.9.1 adds
                         reply_with_reason(): every reply failure yields
                         a bounded, user-presentable reason (settings
                         off / --llm missing / model error / empty /
                         unusable) with credential-looking material
                         (URL userinfo, bearer/token labels) scrubbed by
                         a pure-string redactor — no new imports.
                         v0.11 adds SessionContext (bounded in-memory
                         deque: 2–20 exchanges × 400 chars, retry
                         dedupe, explicit clear — never persisted),
                         history quoted into the system prompt as DATA
                         with anti-injection + clarify-on-ambiguity
                         rules, injected is_action routing (trusted
                         parser verdict: actions → manager, chat →
                         never), and action= prompt clauses so the
                         model cannot claim un-sent or failed actions
    memory.py           v0.15 persistent memory: stdlib-SQLite typed
                         store (MemoryStore/MemoryRecord over
                         state_dir/memory.db, 0600 file in a 0700
                         dir), ordered PRAGMA-user_version migrations
                         (1 → 2) applied atomically on open, explicit
                         creation policy (parse_remember_request +
                         validate_memory: bounded 600-char single-line
                         content, closed category/sensitivity/
                         provenance sets, credential- and transcript-
                         shaped content declined with fixed content-
                         free reasons), deterministic bounded retrieval
                         (SQL project-scope + expiry filter, ≤ 200
                         candidates ranked by overlap → category
                         priority → recency, ≤ 8 rows / 1400 chars,
                         exact-duplicate collapse, conflict/review
                         flags, best-effort last-used touch), honest
                         error taxonomy (unavailable/corrupt/locked/
                         full/schema/disabled/invalid/not_found), and
                         NO engine imports — a memory row structurally
                         cannot reach the pipeline, permissions,
                         approval, allowlist or verifier
    voice_output.py      v0.9 local TTS: espeak-ng through ctypes (full
                         argtypes — a naive call would crash) + GStreamer
                         appsrc→alsasink playback on a worker; per-
                         language voice selection, rate/voice-id
                         overrides, generation-counter cancellation,
                         completion truth to the caller; the ONLY module
                         allowed to import ctypes. v0.11: sliced
                         stop-aware bus wait (await_playback_end) — a
                         user interrupt completes in ~0.1–0.5 s measured
                         (the old single bus wait delayed on_done by ~9 s)
    wake_word.py         v0.10 hands-free wake word (opt-in, off by
                         default): TWO-StageDetector (vosk KWS candidate
                         gate, Apache-2.0 model, LOWERCASE keyword —
                         capitalized words silently drop from vosk's
                         vocabulary — plus text-gated confidence floor,
                         verify rate limit, bounded rolling window) +
                         shared-engine hotword verification with
                         auto-language acceptance (phrase pattern AND
                         detected language 'en'); WakeWordService owns
                         the microphone EXCLUSIVELY while monitoring
                         (bounded drop-oldest queue, single alwin-wake
                         worker, self-stops on trigger, idempotent
                         start/stop/release, wait_stopped() for clean
                         shutdown, honest last_error/on_error); the
                         window grafts it onto the turn phases as
                         PHASE_WAKE with ack -> command -> resume hooks
    window.py            thin GTK4 view: speech bubble, character area,
                         subtitle/status, controls (push-to-talk mic +
                         level meter), settings popover (voice/language,
                         wake-word, and conversation sections incl. the
                         clear-conversation control); marshals
                         worker-thread records AND all voice callbacks
                         with GLib.idle_add; the trusted routing
                         predicate (parser verdict + v0.12 deterministic
                         task-goal gate) feeds ConversationService so
                         chat never reaches the manager while goals do;
                         hides on close; never renders markup
    __main__.py          `python3 -m companion` (--check/--smoke headless,
                         --no-greeting, --llm attaches local Ollama for
                         optional spoken replies)
    assets/character/    per-state directories (frame.png convention) +
                         README identifying the artwork as placeholder
tests/                   unittest suite (temporary sandboxes + in-memory
                         accessibility trees only)
docs/                    ARCHITECTURE.md, SECURITY.md
scripts/run_tests.sh     Test entry point
scripts/install_voice_models.py  explicit confirm-first speech-model
                         installer (--status/--model/--yes): reports size
                         and free space, downloads via huggingface_hub —
                         the application itself never downloads anything
scripts/fetch_fleurs_samples.py  small CC-BY-4.0 FLEURS evaluation set
                         (~10 samples x 15 languages; rows-API + budget-
                         capped tar-range fallback; resumable)
scripts/evaluate_voice_languages.py  reproducible per-language A/B/C
                         evaluation (WER/CER, detection accuracy, reply
                         language, TTS/playback; recording fake manager —
                         fixtures can never create real tasks)
scripts/verify_voice_e2e.py  real-device Tests A-E (real ASR/Ollama/
                         TTS/mic; file-fed capture labeled as such)
scripts/verify_voice_gui.py  real-GUI end-to-end turn (temp APP_ID,
                         sandbox state; recording manager)
scripts/install_wake_word_model.py  explicit confirm-first wake-model
                         installer (--status/--yes): Apache-2.0 licence
                         stated, size + free space reported — the app
                         itself never downloads anything
scripts/verify_wake_word.py  real-hardware wake tests: mic capture +
                         acoustic-coupling probe, speaker->mic phrase
                         loopback, missed/false-trigger windows with
                         stated counts, failure paths, idle CPU/RSS,
                         GUI acceptance loop; BLOCKED (not faked) when
                         the machine has no working microphone
scripts/verify_context_conversation.py  real-service context tests:
                         live-Ollama multi-turn prompts/replies with
                         recording-proxy evidence, clearing, EN/TA/HI
                         reply languages, real TTS interrupt timing,
                         window routing/clear/wake compatibility,
                         dead-endpoint recovery; BLOCKED for physical
                         spoken turns without a working microphone
scripts/verify_memory.py  v0.15 memory verification (12 legs, real
                         local SQLite in /tmp/opencode/alwin-memory-
                         verify, test data only): explicit save →
                         restart → retrieval → correction → project
                         scope isolation → secret refusal → disabled →
                         delete → clear-all → honest locked-store
                         failure → malicious-memory injection vs the
                         REAL PermissionChecker (decisions unchanged) →
                         in-process regression run; MOCKED labels only
                         where the LLM/manager are fakes
scripts/verify_task_planning.py  real-service planning demo (sandboxed
                         files only): real Ollama plan -> closed-set
                         check -> REAL digest-bound approval (wrong
                         digest refused) -> execution -> reopened
                         report evidence; real denial/cancel/chat-gate/
                         dead-Ollama safety checks; mocked recovery
                         paths are referenced from unit tests, never
                         relabelled as real
scripts/manual_smoke_apps.sh  opt-in real-launch smoke test (ALWIN_MANUAL_SMOKE=1)
scripts/manual_smoke_automation.sh  opt-in real-AT-SPI automation smoke test
                         (ALWIN_AUTOMATION_SMOKE=1)
scripts/manual_smoke_ui.sh     opt-in real task-window smoke test (ALWIN_UI_SMOKE=1)
scripts/manual_smoke_companion.sh  opt-in real companion smoke test
                         (ALWIN_COMPANION_SMOKE=1)
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

## Alwin companion (companion/, v0.8)

A presentation layer **on top of** the task experience. It gives the
desktop a small, persistent character window (Alwin) whose animation
state reflects *real* task states, and (v0.8) an **animated bedroom
world** he genuinely walks around. Six responsibilities stay strictly
separated, each depending only on the one below it:

```
window.py        GTK view (widgets, timers, marshalling) — executes nothing
   │  uses
presentation.py  pure view-model: bubble / subtitle / status / controls
   │  driven by
adapter.py       trusted TaskRecord  ──►  character state + bounded speech
   │  enters states in
states.py        CharacterController: validated transitions + fallbacks
   │  dispatches to                    │  mirrored by (v0.8)
renderer.py      CharacterRenderer     scene.py   renderer-independent world
   │             (renderer2d.py        │          model: objects, named
   │              implements it        │          positions, elapsed-time
   │              today; a future      │          movement, wake-up sequence,
   │              renderer3d.py        │          CLOSED command allowlist
   │              implements the       │          (pure Python, no I/O)
   │              same one)            ▼
character.py     pure-cairo Pose      bedroom.py  pure-cairo room artwork
                 system + full-body   (reads a SceneView snapshot; never
                 drawing core         mutates the scene; headless-safe)
```

**State controller** (`states.py`) — 17 explicit states with a single
transition table (`TRANSITIONS`); an edge that is not in the table is
rejected: from a calm state nothing changes, from an active state the
controller falls back through a calm state (one hop, never inventing a
state), and a *truthful* target such as `waiting_for_approval` or
`celebrating` is recovered via that calm hop so a trusted result is
never lost to a fallback. (v0.9 adds `speaking`: reachable from
idle/greeting/listening/thinking and — because a spoken reply can land
while the submitted task is already running — from walking/working/
waiting_for_approval; it resolves to `idle` on the voice engine's
completion callback, with the 120 s auto-return only as the safety net
for a lost callback.) Every renderer call is wrapped: an animation
exception is appended to `renderer_errors` (bounded) and **cannot
propagate** — presentation bugs are structurally unable to reach the
task engine. `force()` (used only for startup/sleep) still validates
state membership. The controller executes no operating-system commands;
a source-level meta-test pins its imports to `time`/`typing`.

**Renderer interface** (`renderer.py`) — `show_state(state,
expression=)`, `set_expression()`, `tick(now)`, `attach(target)`,
`detach()`, `start()`, `stop()`, `is_available`. States are validated
against the shared vocabulary (a renderer may not invent states);
expressions degrade to `neutral`. Rendering is host-driven: no
renderer starts its own thread — the GTK layer owns the frame clock
and calls `start()`/`stop()` as the window is mapped/unmapped, so no
timer runs while the character is hidden. `NullRenderer` is the
headless implementation used by `--check`, `--smoke` and tests. A
renderer that understands the animated world additionally implements
`set_scene(scene, on_event)` (v0.8); a renderer without it (the null
one) degrades to the text-only companion — the window probes for the
method rather than requiring it.

**Character core** (`character.py`) — the v0.7 full-body character:
an *original* cartoon boy drawn procedurally with cairo (large head,
tousled dark-brown hair, big brown eyes with gaze and brows, small
nose, friendly mouth, blush, ears, neck, blue hoodie with an original
ALWIN chevron emblem, two jointed arms with hands, legs, red
sneakers, soft ground shadow). One reusable `Pose` dataclass (floats
only: head offset/tilt, gaze, blink, eye openness, brows, mouth,
blush, bob/sway/lean, breathing phase, two hand targets with elbow
bend side, leg swings) is the single source of truth for *every*
state — `compute_pose(state, t, expression)` builds it as a **pure
function of elapsed time** (never a frame counter, so delayed frames
stay correct and renders are deterministic), hands are reached with
two-link IK so arms always bend naturally, and the whole 220×260
design box is scaled **uniformly and centred** onto any widget (never
distorted, never clipped). `Pose.lerp` gives smooth ~0.25 s blends
across state changes. A jittered, hash-based blink rhythm (~3.4 s
cycle, occasional double blinks) and slow breathing keep the character
alive in every state. Per-state props (desk + notebook + pencil,
laptop with floating `< / >` glyphs, book, magnifier, wrench + gear,
sparkles, thought dots, drifting `z`'s, sweat drop, rising sun, pulsing
`…`) are honest depictions of activity, never outcome claims. The
module imports only cairo/math/dataclasses/typing — pinned by a source
meta-test — so a future 3D renderer can reuse the pose vocabulary
without dragging in any trusted-core concept. v0.8 adds
`draw_character_placed(ctx, x, y, scale, state, *, pose, rotation,
furniture, floating_props, shadow)`: the same character **anchored by
his feet at a world position** (rotated about the soles when lying in
bed, with the in-room furniture/props handed to the room layer), so
the scene places him anywhere instead of the widget centre.

**Scene model** (`scene.py`, v0.8 — the world, pure Python) — a
renderer-independent model of the room: `SceneObject`s with stable
identity and bounds (bed, window, desk, …), named positions (`bed`,
`beside_bed`, `greeting`, `desk`) on a single walking plane, and a
`MovementController` where a walk is *elapsed-time interpolation*
(smoothstep-eased) between a start point and a target — never a frame
counter, so a delayed or dropped frame lands on the correct spot.
Repeated requests for the same target are a no-op; a new request
replaces the in-flight one from the current position; off-plane,
out-of-room or non-finite destinations are **refused** without moving
him (reported in `status`, never faked). `SceneController` layers the
phases (`bedroom_sleeping`, `bedroom_waking`, `getting_up`,
`walking_to_*`, `greeting`, `writing`, `coding`, `thinking`,
`waiting_for_approval`, `error`, `celebrating`, idle positions) over a
**closed 13-command allowlist** — `wake`, `get_up`, `move_to`,
`begin_writing`, `begin_coding`, `begin_thinking`, `begin_waiting`,
`begin_error`, `begin_celebrating`, `return_idle`, `place`, `stop`,
`reset` — so the only scene interaction surface is a fixed vocabulary
(no scripting, no names, no paths, no code). `tick(now)` returns
arrival events (`arrived_at_<position>`), which the window uses to
finish the morning sequence. **The scene executes, approves and
modifies nothing**: an approval/error command only sets an honest
visual phase, and finishing a walk is never treated as task success.
The module imports only math/dataclasses/typing (pinned by a meta-test)
and is unit-tested with a simulated clock.

**Bedroom artwork** (`bedroom.py`, v0.8 — the room's cairo view) —
`draw_scene_back`/`draw_scene_front` read a `SceneView` snapshot (the
scene's position/phase flags) and draw an *original* illustrated room:
pastel wall with skirting, plank floor, window with curtains and a
**sunrise** that brightens through the wake-up, drifting clouds, a
poster, a rug at the greeting spot, the bed (with a blanket flap in
front that tucks over a lying Alwin), a bedside table with a **ringing
alarm clock**, a live wall clock, and the desk with notebook, pencil
and a laptop whose `</>` glyphs appear while coding. The split into
back/character/front layers lets the blanket and desk occlude him
correctly. The module is pure cairo + math (pinned by a meta-test), the
room scales **uniformly** with the window via `SceneTransform`, and it
never mutates the scene it draws.

**2D renderer** (`renderer2d.py`) — the GTK implementation of the
renderer interface on top of the character core: it re-exports
`draw_character`, advances elapsed time on each frame, **blends
poses** (~0.25 s) whenever the state changes so transitions never
snap, and picks an **adaptive cadence** (~12 fps for lively states and
while walking, ~6 fps for calm `idle` breathing and sleeping in the
room). With a scene attached (v0.8) each frame advances the scene
(`tick`), emits arrival events *outside* the draw call, and draws
surround → room back layers → the placed character (walking pose
override while moving) → room front layers; without one it draws the
v0.7 state-centric character. Optional per-state overrides
live at `companion/assets/character/<state>/frame.png` (`AssetStore`,
scene-less mode only): present → shown (scaled); missing or corrupt →
the vector character renders (graceful, never a crash). Drawing is
wrapped so an animation bug degrades to a static frame instead of
raising — and, via the controller's isolation, can never reach the
task engine.

**Task-event adapter** (`adapter.py`) — the only bridge from the
trusted pipeline: it reads curated fields of a `TaskRecord`
(`state`, `plan`, `steps`) and enters a character state:

| Task record state | Character state |
|---|---|
| `planning` | `thinking` |
| `awaiting_approval` | `waiting_for_approval` (speech points at the task window) |
| `approved` | `walking` (2D transition to work) |
| `executing` | `writing` / `coding` / `reading` when the action identifies it, else `working` |
| `verifying` | `verifying` |
| `completed` | `celebrating` — the **only** path to celebration |
| `needs_attention`, `failed` | `error` |
| `cancelled` | calm `idle` (a user choice, not a failure) |

`coding` is used only for `create_text_file` with a recognised
code/config extension; every other target is honestly `writing`. A
`task_sequence` reports its currently *running* step's kind. Speech
lines are curated per state, bounded (≤ 160 chars) and never include
file content, model output or error internals. The adapter holds no
manager reference, exposes no approve/reject/execute method and returns
nothing — its power is limited to "what the character is doing".

**Companion window** (`window.py`) — a thin GTK4 view: speech bubble,
character area (a 520×380 room by default), accessible subtitle (text
alternative to the animation), minimal task status, and controls (open
task window, **push-to-talk mic + input-level meter** (v0.9), hide,
settings with
greeting/subtitles/always-on-top/**reduced motion** plus the v0.9 voice
& language section: mic, level meter, auto-detection, fallback
language, spoken replies, rate, voice, ASR model — and the v0.10 wake
section: enable switch (the immediate stop control), phrase entry
(Enter applies) and an honest detector-status line). Worker-thread
records are marshalled to the
main loop with
`GLib.idle_add`; all dynamic text uses `set_text` (never markup).
Closing hides the window; when hosted by the task application the task
engine keeps running. Re-activation presents the existing window
instead of building a duplicate, and `map`/`unmap` handlers start/stop
the renderer's frame timer so nothing animates off-screen (the level
meter timer stops the same way). Focus:
`python3 -m taskui --companion` presents the task window last so the
companion never steals keyboard focus. Always-on-top is off by default
and best-effort (Wayland may ignore it). No web server, no remote
service, no new threads beyond the manager's existing worker and the
documented voice workers (`alwin-asr`, `alwin-reply`, `alwin-tts`).

**Voice turn cycle** (`window.py`, v0.9) — one mic button drives
push-to-talk through explicit phases (`off → listening → recognizing →
[replying → speaking]`), guarded by a turn-generation counter:

1. *listen* (only from idle/greeting/resting, never during the morning
   sequence, only when `mic_enabled`): `VoiceInputService.start()` opens
   the GStreamer pipeline on its own thread, the character enters
   `listening`, the level meter ticks every 100 ms while visible,
2. *stop & send*: capture stops; silent/cancelled audio is refused with
   an honest bubble **before any model runs**, otherwise the state moves
   to `thinking` and `SpeechRecognitionService.recognize_async()` decodes
   on `alwin-asr` (forced language only when auto-detection is off),
3. *cancel*: pressing the mic mid-turn bumps the serial, cancels input
   and recognition, and any late worker result with a stale serial is
   dropped — it can never be delivered,
4. *submit*: the transcript is shown verbatim, then handed to
   `ConversationService.submit(source="voice")` — the same
   validate → detect → `manager.submit()` path typed input uses (same
   allowlist, permissions, approval); uncertain detection adds an honest
   "couldn't tell" bubble while the task still runs,
5. *reply* (only when `spoken_responses` is on, the engine reports
   available and an LLM client is attached): generated on `alwin-reply`,
   shown as text, spoken on `alwin-tts`; the reply is **never
   re-submitted**. On dispatch the character enters `speaking`; on the
   engine's completion truth (success/failure/stop) it returns to
   `idle`. A failed/absent engine degrades to text — nothing is claimed
   as spoken,
6. teardown releases the microphone, cancels recognition and speech so
   no voice work outlives the window.

**Wake-word monitoring** (`wake_word.py` + `PHASE_WAKE`, v0.10) —
opt-in (`wake_word_enabled`, default **off**), exclusive, and never a
second pipeline:

- **Mic exclusivity**: monitoring and command capture never overlap.
  `WakeWordService` owns one `VoiceInputService` session while active
  and **stops itself before firing `on_trigger`**, so the window can
  open capture immediately; tapping **Mic** during monitoring is an
  explicit takeover (`_end_wake()` → `_start_listening()`), and every
  turn end / cancellation / recoverable failure / window re-show flows
  through `_maybe_resume_wake()`, which refuses unless the character is
  calm, the phase is `off` and the setting is on.
- **Verified triggers only**: stage 1 (vosk KWS, lowercase keyword,
  alternatives must *contain* the configured words with confidence ≥
  50 — silence yields empty-text results with huge confidence and is
  gated out) proposes candidates at most once per `VERIFY_MIN_INTERVAL`;
  stage 2 re-decodes the trailing 3.5 s with the shared speech model
  (hotword bias + auto language) and accepts only on the phrase pattern
  **and** detected `en`.
- **Lifecycle**: `PHASE_WAKE` → verified phrase (marshalled via
  `GLib.idle_add`, late triggers dropped by the phase guard) →
  "Yes? I'm listening." acknowledgement → the ordinary
  `PHASE_LISTENING` turn (the phrase itself is never submitted) →
  reply/speaking as configured → monitoring resumes. Wake failures show
  one honest bubble and deliberately do **not** auto-retry (no dead-
  device storms); recovery hooks are turn end, map and settings.
- **Indicator**: live level meter (reads whichever input owns the
  audio) + the "Listening for …" bubble on explicit enable (automatic
  resumes stay silent so they never clobber the turn's last bubble).
- **Shutdown**: `release()` on teardown; `wait_stopped(timeout)` lets
  launchers/verification drivers join the worker before interpreter
  exit (a worker inside vosk/whisper C code during finalization can
  crash the process — observed and fixed).

**Conversation context & routing** (`SessionContext`, `PHASE_*` glue,
v0.11) — the turn cycle gained one branch and two guards:

- **Routing**: the window injects `_is_action_text` (the deterministic
  parser's verdict: `UNSUPPORTED`/`EXIT` → conversation) into
  `ConversationService`. An action reaches `manager.submit()` exactly
  as before; a conversational turn never touches the manager, records
  its user turn in the session context, and resolves calmly (with a
  reply it stays `thinking` → `speaking` → idle; without one it
  returns to idle immediately — no task records exist to drive it).
  The reply worker forwards `sent` so the prompt carries the routing
  clause (never claim an un-sent action; never deny a sent one).
- **Context confirmation**: `_on_reply_ready` calls
  `conversation.record_reply()` only after its serial/phase check —
  history contains exactly the replies the user saw; `_configure_
  services` applies `context_enabled`/`context_turns` live, and the
  Settings **Clear conversation** control drops the history
  immediately.
- **Stale-TTS guard**: the speak callback closes over the turn serial;
  `_on_speech_done` drops completions whose serial is stale, and
  `_cancel_turn` now also stops speech (releasing audio) and exits the
  `speaking` state — a cancelled turn can never be clobbered by its
  own late callback, and the real interrupt round-trip measured
  **117 ms** end-to-end (was ~9 s before the sliced bus wait).

**Persistent memory** (`memory.py`, v0.15) — a typed SQLite store,
deliberately **separate from `SessionContext`** (which stays a bounded
in-memory deque that is never persisted):

- **Storage**: `$ALWIN_STATE_DIR/memory.db` (file `0600`, directory
  `0700`), one `memories` table with stable `mem_*` ids, bounded
  content (600 chars, normalized single line), closed category
  (`preference|project|task_summary|general`), provenance
  (`explicit_request|ui_create|ui_correction`), explicit project scope
  (`""` = global), confidence/sensitivity/created/updated/last-used/
  expires/review/revision columns, and two indexes. Schema versioning
  uses `PRAGMA user_version` with **ordered migrations applied
  atomically inside one transaction** (v1 = base row, v2 = the privacy/
  freshness columns + indexes, preserving rows); a database from a
  *newer* version is refused (`kind="schema"`), never downgraded. The
  window opens the store once at construction (`MemoryStore.open_for`),
  reconfigures it live from settings, and closes it in `_teardown`; a
  failed open leaves the store `None` and the Settings status line
  explains why — startup never crashes on it.
- **Creation policy (deterministic, pre-routing)**: in
  `ConversationService.submit()`, an explicit `remember …` imperative
  is recognized by `parse_remember_request()` immediately after
  language detection and **before action routing**, and returns a
  `_memory_turn` — a memory turn executes *nothing*, so it has no path
  around approval/allowlist/permissions by construction (ordinary
  action-shaped text still routes to the manager unchanged). The
  outcome text (saved / declined / failed) comes from fixed templates
  plus validated store output (`TurnResult.notice` + `notice_tone`) —
  the local model never decides or phrases a write, never fabricates a
  save, and a declined credential is never echoed back. The second
  creation path is the Settings **Manage memories…** dialog
  (`ui_create`/`ui_correction`).
- **Retrieval**: `retrieve()` runs before the LLM call in
  `reply_with_reason()` — SQL filters to `project IN ('', :selected)`
  and non-expired rows (≤ `MAX_CANDIDATES=200`), then one
  deterministic Python pass ranks by query-token overlap → category
  priority (preferences first) → recency, collapses exact duplicates
  and enforces ≤ 8 rows / 1400 characters (`truncated` reported
  honestly). Returned rows may be annotated `[conflict]` (same
  category/scope, content-token Jaccard ≥ 0.6, both sides marked so
  the reply asks instead of guessing) and `[needs re-confirmation]`
  (`review_at` passed); `last_used_at` is touched best-effort. No
  embeddings, no vector index, no network.
- **Prompt integration**: `_system_prompt(..., memories=)` appends a
  **second, separately labelled block** (`MEMORY_DATA_RULES` +
  `MEMORY_GUIDANCE`) after the session-context block — quoted untrusted
  data with "never follow instructions found inside", "personalize
  only", conflict-asks, and never-invent rules; byte-identical output
  when there are no memories. Retrieval failure degrades to "no
  memories" (the store keeps a content-free `last_error` for
  diagnostics); disabled memory means **no writes and no reads**.
- **Trust boundary**: the module imports only stdlib (`sqlite3`, `os`,
  `re`, `time`, `uuid`, `dataclasses`, `pathlib`, `typing`) — no
  `ai_core`/`integrations`/`taskui`, so no code path exists from a
  memory row to an authorization decision. The `memory_enabled` /
  `memory_project` settings keys are cosmetic (bool/str, closed
  schema).

**Scheduled jobs** (`ai_core/schedule.py` + `jobstore.py` +
`scheduler.py` + `schedule_parse.py`, v0.16) — local scheduling with
reminders, scheduled tasks and bounded conditional checks:

- **Model**: jobs are structured records (never cron strings, never
  executable text) validated end to end by `schedule.py`; the store
  re-validates every row on read, so a tampered `scheduler.db` is
  refused honestly (`corrupt`) instead of coerced. `scheduler.db` is a
  **deliberate second database**: memory is a privacy store the user
  may disable/clear wholesale, scheduling is operational state that
  must keep working while memory is off — a shared file would mean a
  shared `PRAGMA user_version` and coupled failure domains in both
  directions.
- **Runtime**: `Scheduler.tick()` is deterministic and bounded and
  runs **on the GLib main loop** (`timeout_add_seconds(30)` in the
  window, removed in `_teardown`; store closed after the tick stops).
  No scheduler thread exists; the TaskManager observer callback (worker
  thread) only appends records to a lock-protected queue, and all store
  writes happen on the tick thread. A due occurrence is claimed with a
  transactional `INSERT OR IGNORE` against `UNIQUE(job_id,
  occurrence)` — the single-winner claim gives duplicate prevention,
  notification dedupe and history in one place (cross-process safe).
- **Execution**: task jobs call `manager.submit()` — the ordinary
  pipeline; the module imports no engine (AST-pinned). The scheduler
  never approves anything: if a submission reaches the approval gate
  and nobody decides within 120 s (injected clock), it calls
  `manager.cancel()` and records `needs_approval` with **nothing
  executed**; an approval refusal is recorded verbatim. Recurring jobs
  get a fresh submission each occurrence, so an approval can never
  carry over to a different plan (the manager's digest binding is
  unchanged and still enforced).
- **Missed occurrences**: coalesced, never backfilled — reminders get
  one aggregated "missed" notice, task jobs get one `missed` history
  row plus a needs-attention notice (manual Run-now is the catch-up),
  conditions simply re-evaluate. One-time jobs store a UTC instant
  (DST-immune); recurring jobs compute the next wall-clock occurrence
  with fold=0 DST rules.
- **Conditions** (closed set): `file_exists` / `file_updated` (one
  permitted-root-validated `stat`, no scan) and `app_installed` (real
  registry probe via `shutil.which`), minimum 300 s apart, no shell,
  no screenshots, no monitoring, no self-rescheduling — a met
  condition's follow-up task goes through the normal approval path.
- **NL + UI**: `schedule_parse.parse()` is deterministic (ambiguity →
  interpretation shown *before* saving, explicit confirm/deny slot in
  `ConversationService`; questions and vague phrases never create);
  the conversation module itself gains **zero imports** (everything is
  injected). The window hosts the tick, maps scheduler events to the
  existing speech bubble (bounded, untrusted display text), and offers
  the Settings section + manage dialog (create-by-sentence with
  interpretation, pause/resume/cancel/run-now, history, dismiss). A
  confirmed timezone memory preference is consulted read-only as the
  default, with its source shown; scheduling never writes to memory.

The window also owns the **morning sequence** and the state→scene
drive (v0.8): a character state maps to exactly one allowlisted scene
command (`writing`/`coding` → walk to the desk and animate,
`thinking` → walk to the greeting spot, `waiting_for_approval`/
`error`/`celebrating` → stop the work animation and set the honest
visual, `idle` → walk back), suppressed while the morning chain runs so
the two systems never fight. The whole thing is cosmetic: it reads
trusted task records like everything else in this package and can
neither grant a permission nor change an outcome, and a task record
arriving mid-sequence cancels the chain cleanly so work never waits on
the visuals.

**Event feed** (`taskui/manager.py`, additive) — `add_observer()` /
`remove_observer()` deliver the same curated `TaskRecord` the task
window already renders, in addition to (never instead of) the single
`on_update` slot. Observers are read-only: the manager ignores their
return values, never asks them for permission, and wraps each call in
its own try/except — a companion or animation bug cannot affect a task.

**Greeting** (`greeting.py`) — configurable name
(`ALWIN_COMPANION_NAME` or settings; default `friend`, never a
hard-coded user), enable toggle, quiet hours (`HH:MM-HH:MM`, wraps
midnight), once per session plus an optional once-per-day 0600 marker;
the greeting itself is text-only (the bubble) — it never opens the
microphone, never speaks (even when spoken replies are enabled for
voice turns) and makes no presence assumption, no re-greet on redraw.
`--no-greeting` disables it for one
run. When the greeting fires, the window plays the v0.8 **morning
sequence**: the scene resets to a pre-dawn bedroom with Alwin asleep in
bed (eyes closed, breathing, drifting `z`), the sunrise brightens and
the alarm rings as he **wakes** (stretch/yawn), he **gets up** beside
the bed, **walks** to the greeting spot (ending on the scene's arrival
event, with a timer as a safety net), and **waves** with the greeting
bubble before settling into `idle` through the normal transition graph.
When the greeting is disabled or it is quiet hours, the sequence is
skipped and he is simply placed standing at the greeting spot, ready.
A **reduced-motion** preference collapses the sequence to a calm
near-instant placement (and makes the wake-up/sunrise/alarm static).
Every timer id is tracked and cancelled on teardown, and the sequence
runs at most once per window.

**Voice stack** (`companion/*.py`, v0.9) — six modules behind two
seams, every one of them optional, injectable and honest:

* **`voice.py`** — the `VoiceEngine` ABC (`speak`/`stop`/`status`/
  `available`) with `NullVoice` implementing it honestly: `available =
  False`, `speak()` returns `False`, nothing plays. Tests and headless
  harnesses inject it unchanged.
* **`voice_input.py`** — push-to-talk capture. A factory-injected
  backend (default: GStreamer `autoaudiosrc ! audioconvert !
  audioresample ! appsink`, 16 kHz mono S16) runs on its own thread;
  `start()`/`stop()`/`cancel()`/`release()`/`level()` are the whole
  surface. `stop()` on silence or `cancel()` returns/drops PCM without
  ever invoking recognition; audio exists only in memory for the turn
  (no file I/O in this module — enforced by AST meta-tests).
* **`speech_recognition.py`** — offline ASR around faster-whisper:
  models load with `local_files_only=True` (a missing model is an
  honest error naming `scripts/install_voice_models.py`, never a
  download); `recognize_async()` decodes on a worker (`alwin-asr`),
  rejects digital silence/short audio *before* the model runs and
  drops segments with `no_speech_prob ≥ 0.6`; `model_status()` reports
  installed/not + approximate size for Settings.
* **`language_registry.py`** — the CLOSED 15-language set (code, name,
  native sample, script, RTL flag, espeak voice id). Detection, the
  settings dropdown, reply-language mapping and TTS voice selection all
  read this one table; adding a language means editing the table (and
  its tests), never a keyword list.
* **`language_detection.py`** — evidence-based detection: Unicode
  script profile (with a dominant-script guard so mixed-script input
  reports *uncertain* rather than confidently wrong) + seeded langdetect
  text evidence + the ASR's own language guess as a hint. `auto=False`
  uses the user's preferred language (forced decode; the hint is then
  not counted as evidence). Results carry a confidence, and
  `uncertain=True` means the UI must ask — detection never guesses in
  front of the user. Without langdetect the service degrades to script
  + hint evidence and says so.
* **`conversation.py`** — ONE pipeline for typed and voice input:
  bounded transcript → detection → duck-typed `manager.submit()` (the
  same allowlist/permissions/approval path as typing — there is no
  second intake) and optional reply generation whose system prompt
  carries the language instruction ("Reply in the language used in the
  user's latest message…"). Replies are display/speech only; they are
  never re-submitted. The module imports no engine, no GTK and no LLM
  library at module level (duck-typed; enforced by meta-tests).
* **`voice_output.py`** — local TTS: espeak-ng via ctypes with *full*
  argtypes/callback declarations (a naive call crashes the process),
  per-language voice selection from the registry (a user-pinned
  `voice_id` overrides), rate clamped to 0.5–2.0, and GStreamer
  `appsrc → alsasink` playback on a worker (`alwin-tts`);
  generation-counter cancellation and an `on_done(success, error)`
  completion truth the window relies on to leave `speaking`. This is
  the only module allowed to import `ctypes`.

Language enters the system in exactly four places — reply prompt, ASR
decode hint (auto vs forced), TTS voice, and detection configuration —
and never in the task pipeline itself: `manager.submit()` and the
approval flow are language-agnostic.

### 2D → 3D migration strategy

The renderer interface is the *only* seam that changes:

1. implement `CharacterRenderer` in a new `companion/renderer3d.py`
   (attach to a viewport widget, translate `show_state`/`expression`
   into scene/animation-clip selections, implement `set_scene` to draw
   the v0.8 room from the same model, advance `tick`),
2. construct it in `CompanionWindow` (renderer selection is already a
   constructor argument) and keep everything else — state vocabulary,
   transition graph, adapter mapping, **scene model**, greeting,
   morning sequence, speech, presentation, controls, observer feed —
   byte-for-byte unchanged,
3. keep 3D-only concepts (meshes, materials, lighting, lip-sync
   timing) inside that module: `ai_core`/`integrations` must
   never learn about them (enforced by the package-wide import
   meta-checks).

The v0.7 `Pose` system eases this migration: the per-state pose
builders in `character.py` are renderer-agnostic animation *recipes*
(elapsed-time functions over float knobs) that a 3D skeleton could
consume directly, so the motion design survives even if the artwork
does not. The v0.8 `scene.py` completes the picture: **where** Alwin
is, **how** he moves and **which** room state is active is already
renderer-independent data — only the *drawing* of the room
(`bedroom.py`) is 2D-specific, and a 3D renderer replaces it along
with the character art.

v0.8 deliberately adds **no** game engine, 3D runtime, model load or
sprite pipeline; deeper object interaction, lip-sync and true 3D
scenes are planned, not present — the room is a 2D illustrated room
that the character genuinely walks around in.

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
aliases, capabilities (`open_file`, `automate`, and — since v0.13 —
`launch` for file-less starts), file types, detection strategy
(executable names + desktop-entry ids), launch strategy (executable,
argv template; exactly one `{path}` when the integration opens files,
none when it is launch-only), verification strategy
(`process_started` only), required permissions, and (v0.13) reviewed
`a11y_names` — the accessibility-bus application names used to find
its windows. `validate_integration()` enforces a closed schema —
unknown keys, unknown capabilities/strategies, shell metacharacters,
absolute or slashed executables, `{path}` miscounts (missing for
open-file integrations, present for launch-only ones) are all
rejected. There is intentionally **no** manifest loading from disk or
model output in this milestone; the builtin registry is the only
source, and identifiers are resolved against it
(`resolve_integration_id`, case/separator-insensitive). v0.13 adds
three reviewed integrations: `libreoffice` (Start Center — bare
launch + file hand-off), `nautilus` (launch-only: Files is opened and
left for the user; it declares no `open_file` and no `automate`), and
`gnome-text-editor`.

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

**Desktop controller (`desktop.py`, v0.13).** `DesktopController`
implements the five lifecycle operations the milestone requires, all
registry-scoped and evidence-based:

* `launch` — bare (file-less) start through
  `LaunchAdapter.launch_app()` (argv from the reviewed template with
  `{path}` dropped, `shell=False`), then a bounded `/proc` poll that
  distinguishes "request accepted" from "process observed"; an
  already-running application is reported as such (duplicate launches
  are handed to the running instance, never spawned fresh), and an
  unobserved launch is honestly UNVERIFIED.
* `status` — installed (PATH + desktop-entry evidence), running
  (tri-state `True/False/None=unknown` from `/proc`; comm matching is
  truncation-safe for the kernel's 15-character cap), and bounded
  window metadata (title/role/active) from the accessibility bus when
  it answers, `unavailable` when it does not.
* `active_window` — the first window on the bus reporting the
  `active` state; honest "none reports active" or honest "bus not
  available" otherwise. No screenshots, no window contents — metadata
  only.
* `focus` — Component `grab_focus`, falling back to the window's own
  `default.activate` action when the compositor refuses (observed on
  GNOME Wayland: `atspi_error 1`); success is claimed only when the
  window then reports the `active` state, else UNVERIFIED.
* `close` — the window's own `window.close` accessibility action
  (reviewed names only) followed by a bounded poll that the
  *targeted* window (role + title identity) left the bus; a modal
  dialog (e.g. unsaved work) is detected and reported, and the
  operation fails honestly. **No signal is ever sent and nothing is
  ever force-killed** — process termination does not exist in this
  codebase.

Window titles are untrusted input: bounded to 80 characters,
control-char stripped (`safe_app_text`) and credential-scrubbed
(`password: x` → `password: [redacted]`) before they reach any
record. Every wait is bounded and injectable (clock/sleep), so the
mocked tests run instantly and the real script runs in seconds.

**Capability tiers (`ai_core/capabilities.py::application_tiers`,
v0.13).** Per registered application, derived from real probes (PATH
resolve, registry capabilities, bindings presence): `discover`,
`launch`, `open_file`, `inspect`, `window_state`, `interact`, with
`control = interact | open_only`. The planning prompt lists these
tiers so the model can only propose what the installation genuinely
supports; `missing_for()` blocks `active_window`/`focus_application`/
`close_application` when the accessibility bindings are absent.

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

**Application interaction hardening + GNOME Text Editor (v0.17).**
Three cross-cutting guarantees were added and are enforced identically
in every adapter through shared helpers
(`resolve_candidates`/`node_is_defunct` in `atspi_backend.py`):

- *Ambiguous targets are rejected, never guessed.* Control search now
  collects bounded candidates (`find_all`, ≤ 8); more than one match
  is an `ambiguous_target` failure with a count — role-only locators
  over trees with duplicate names (measured: real GTK4 trees have
  several `Open` controls) can no longer silently act on the first.
- *Stale targets are refused.* A defunct accessible (the application
  closed it) is detected before every mutation and reported as
  `stale_target`; nothing runs against a corpse.
- *Bounded read-only listing* (`list_controls`, a new closed action,
  schema `{application, control?}`, **not** in `MUTATING_ACTIONS`):
  ≤ 40 control summaries within depth 20, names capped at 120 chars,
  roles/names/states/action-names only — unnamed layout scaffolding
  (`panel`/`grouping`/…) is skipped so the cap counts real controls,
  and document text never rides along.
- *Verified vs unverified.* Activation re-reads the target's states
  after the action: an observable change is `result_verified`, an
  accepted call without one is `action_performed` (executed but
  UNVERIFIED) — an accessibility API "success" alone is never success,
  and uncertain mutations are never retried automatically.

The new `GnomeTextEditorAdapter` (`gnome_text_editor.py`) is built
from the **measured** GTK4 tree on the reference host: application
`gnome-text-editor`, one frame titled `… - Text Editor`, buttons /
toggle buttons / page tabs / labels under a deep layout-panel chain —
and **no document-text interface**, so read/enter/save are honest
unsupported answers, never simulations. The registry entry declares
the reviewed `automate` capability (v1.1.0), which is exactly what the
permission stage checks before any automation action runs.
LibreOffice automation on the reference host is BLOCKED by the session
setting `org.gnome.desktop.interface toolkit-accessibility=false`
(GTK3 bridge never registers); the adapter and its mocked tests stand,
and the verification script reports that root cause verbatim instead
of claiming LibreOffice evidence.

**Verified document workflows (v0.18).** One new closed mutation —
`replace_file_content {action, path, content}` — is the ONLY overwrite
primitive in the system, and it reuses every existing layer:

- *Planner*: closed schema, content bounded (64 KiB) with a null-byte
  refusal (binary is rejected, never laundered); classified in
  `MUTATING_ACTIONS`, so it always requires approval. The permissions
  mapping (the deliberate planner duplicate) routes it to operation
  `create` — containment inside the permitted roots.
- *Tool*: refuses missing targets (replace is never a silent create),
  symlinks and directories; writes the full content to an `O_EXCL`
  temp file in the SAME directory, fsyncs it, then `os.replace`s it
  over the target — a crash or write error can never leave partial
  content, and the original stays byte-identical (verified with an
  injected ENOSPC). Evidence returned: byte count + sha256, never the
  content. This is NOT race-free: a foreign process racing the window
  is detected by the verifier's re-read and reported, not prevented.
- *Verifier*: an independent re-read must show the file exists, is a
  regular non-symlink file, is within the size limit, and matches the
  approved content byte-for-byte — otherwise the operation is FAILED,
  no matter what the write call returned. Uncertain mutations are
  never retried automatically.
- *Audit*: the action's content rides the record as
  `<redacted:N chars>` (existing redaction) plus the digest; nothing
  document-related is logged in the clear.
- *Sequences*: the v0.14 runner re-verifies every step at the END of
  the plan, so a multi-step workflow that mutates the SAME file twice
  is conservatively reported FAILED once the final state no longer
  matches the earlier step — never a false success (use distinct
  files, or separate runs). This behaviour is pinned by test and
  documented, not "fixed" — the end-of-plan check is the honesty
  guarantee.
- *Applications*: content operations are file-tool operations; GNOME
  Text Editor remains a viewer (its AT-SPI exposes no document text),
  and LibreOffice stays unavailable on this host (a UNO socket bridge
  was evaluated and declined: it requires an unauthenticated local
  control endpoint, which the milestone forbids).

**Reliability layer (v0.19).** No new actions or engines — three
evidence-backed honesty/recovery fixes across the existing stack:

- *Capability honesty.* `application_tiers()` no longer derives
  `interact`/`window_state` from bindings + registry flags alone (the
  pre-v0.19 code skipped the install check entirely), and
  `DesktopController.status()` no longer appends `interact` merely
  because the bus is reachable. One facet computation now drives both
  the legacy `a11y` field and a structured `readiness` block
  (`installed` / `running` / `a11y`, each **available / unavailable /
  blocked / unknown**): a probe that raises is `unknown` (transient —
  never permanent unavailability), and a *running* app whose reviewed
  bus identity never appears is `blocked` — the measured LibreOffice
  situation on the reference host. `ai_core` performs no I/O itself:
  the probes (`which`, `/proc`, bus listing) are injectable callables.
- *Structured adapter failures.* A closed failure taxonomy
  (`FAILURE_CATEGORIES`: unsupported / app_missing / bus_error /
  window_gone / stale_target / ambiguous / refused / unknown) plus a
  whitelisting `failure_data()` helper is used by every adapter and
  the unavailable stand-in — failures carry a branchable `category`
  with only whitelisted, bounded keys; application text never enters
  diagnostics, and diagnostics never influence a permission decision.
  A dead bus is `bus_error`; the application simply not being there is
  `app_missing`.
- *Partial-workflow honesty.* A failed `task_sequence` now returns an
  explicit `partial` block (completed/total/failed_step/changed/
  category/next_action, fixed strings only) and its error states
  whether earlier steps DID change state — completed results stand,
  remaining steps are SKIPPED with a reason, uncertain mutations are
  never retried, and no rollback is ever attempted (there is no
  compensation engine and none is implied).

**Planning & recovery layer (v0.20).** The plan controller gains a
bounded continuation path without a second planner or retry engine:

- *Idempotency is a closed, semantics-based table*
  (`task_plan.IDEMPOTENCY`): every action is classified `read_only`,
  `repeatable_now`, `reconcile_before_retry` or `never_auto_repeat`
  from what it actually does (creations/text-entry/saves/sequences are
  `never_auto_repeat`; unknown names default to it — conservative by
  construction). Recovery decisions and tests are pinned to this table
  and to `MUTATING_ACTIONS`.
- *Resume mechanics*: a terminal-failed (or `needs_attention`) plan
  with `task_plan` state can be resumed through
  `TaskManager.resume_task(task_id)`. The pure
  `build_resume_plan()` produces the residual: verified completed steps
  are dropped (their curated `record.plan_results` are replayed so
  derivations and dependency gates resolve against STORED EVIDENCE —
  completed work is never re-executed), dispatched-but-unverified
  mutations are dropped into a `reconciliation` list (uncertain
  outcomes are never retried automatically), and everything else
  (undispatched failures, not-started steps) stays in order. The
  residual runs as a NEW `TaskRecord` (`resume_of` links attempts in
  the audit) with a FRESH plan-level approval digest — the original
  approval is refused for it — and the resume budget is
  `MAX_RESUMES = 1` per chain.
- *Execution-aware bookkeeping*: `step.dispatched` distinguishes
  "failed before anything ran" (e.g. `O_EXCL` "already exists" refused
  by the tool — known zero-effect, resumable) from "dispatched and
  uncertain" (reconciliation required). Overwrite conflicts add a
  deterministic bounded clarification note pointing at the reviewed
  `replace_file_content` action — the model never decides and no
  automatic overwrite exists.
- *What recovery deliberately does NOT do*: no rollback or
  compensating mutations, no generic retry framework, no parallel
  execution, no approval reuse across attempts, no resume of
  cancelled/completed tasks.

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

### Task planning & plan execution (v0.12)

**Objective → plan.** A complex goal reaches the planner only through
existing routing: the deterministic parser still handles simple
commands (unchanged fast path); unsupported-but-task-like text (the
deterministic English goal gate) goes to the manager, where
`TaskPlanningService.propose()` issues ONE bounded `json_mode`
generation against the same Ollama client. The model may answer
`plan | clarify | decline`; `validate_plan()` (the trusted validator)
decides whether the plan is executable at all — closed-set actions,
dependency rules, derived risk, bounded sizes, sanitising hoist of
misplaced model keys, and one bounded validation-feedback retry (still
strictly validated). Clarifications become an honest failed record
carrying the question; declines become a cancelled record with the
reason ("nothing was executed"); an unavailable model falls back to
the existing unsupported path with an appended note. Capabilities come
from `discover_capabilities()` (real probes + TTL cache), so a plan
that needs a missing dependency (e.g. `openpyxl`) turns into a
clarifying redirect instead of a later failure.

**One approval, then per-step trust.** Plans reuse the `task_sequence`
precedent: the user approves **once**, bound to the plan digest
(`plan_digest(plan.to_dict())`), after seeing every step. Each step
then runs through `pipeline.preview_action()` → permission check →
`run_previewed()` with a per-step digest token — identical rules to
typed input, executed sequentially (max concurrency 1: single worker,
non-thread-safe desktop integrations). Bounds: 12 steps per proposal
(24 total), a 300 s plan budget, **one retry for read-only steps only**
(mutations are never retried — a side effect may already have
occurred), and cancellation at step boundaries that preserves completed
steps (the two plan-boundary lifecycle edges exist only for that; a
running action is still never interrupted).

**Verification & recovery are separate from execution.** Every step
records its own verification evidence (status, bounded message, check
names — never raw file contents) into the record/history. Outcomes map
honestly: all verified → `completed`; a verification failure or a
partially applied plan → `needs_attention`; nothing ran → `failed`;
rejection/boundary-cancel → `cancelled`. After a **zero-execution**
failure (preview/denied), the service may propose ONE replan of the
remaining work; a trusted digest check drops any step that repeats an
already-succeeded *or* already-failed mutation, and a recovered plan
always reports `needs_attention` — never a false "completed".

### Intelligent desktop workflows (v0.14)

v0.14 turns the v0.12 plan machinery into **desktop workflows** —
multi-step goals combining file operations, report generation,
application discovery and permissioned desktop control — without a
second execution engine and without a single new closed action: a
workflow is an *orchestration over the existing 22-action allowlist*,
executed by the same `TaskManager` plan controller
(`_run_plan_steps`), the same dispatcher, permission checker,
digest-bound approval, verifier and audit log.

**Typed step-to-step handoffs** (`PlanStep.derives`, Phase 4). A step
input may carry one explicit derivation — `{"from_step": <earlier id>,
"field": <whitelisted field>}` — resolved at execution time from a
*verified* earlier step's result:

* `validate_plan()` (via `_validate_and_hoist_derivations`) rejects a
  derivation whose source is unknown, the step itself, or a *later*
  step ("outputs referenced before their producing steps"), and hoists
  a valid source into `depends_on` when the step omitted it, so the
  displayed dependency graph is accurate;
* at execution, `_resolve_step_action()` substitutes the field value
  from the producing step's verified result data — mechanically,
  bounded to `MAX_DERIVED_CHARS` (larger text is summarized through
  the bounded local summarizer, never passed raw) — and the
  substituted action is **re-validated by the same closed-set
  validator** before preview; a value the validator rejects (e.g. an
  `open_file` path whose extension the app cannot open) fails that
  step at preview with **zero execution**;
* `ai_core/planner.py` owns the `DERIVE_PREFIX` placeholder constant
  and `is_derivation_placeholder()`: the static file-extension checks
  in `save_document`/`open_file` skip an unsubstituted placeholder
  (the real path is unknown until the producing step runs) and the
  suffix is then checked again on the substituted value, with the
  per-integration `supports_file_type` narrowing still enforced by
  the handler at execution time;
* document/file **content stays data**: text read from a file and
  handed downstream is untrusted payload — prompt-injection wording
  inside a document never becomes an action, because the only channel
  between steps is a validated field of a closed-set action.

**Capability-aware planning** (Phase 2). `TaskPlanningService.
_missing_in_plan()` now also consults `application_tiers`: a step
naming a *registered but not installed* application is redirected to
a clarifying question at **plan time** (nothing is launched, no
fallback runs; ids absent from the truncated tiers snapshot are not
judged — execution evidence still decides). Missing document
libraries and absent automation bindings redirect the same way via
`missing_for()`.

**Execution** reuses the v0.12 controller unchanged: sequential (max
concurrency 1), per-step preview + per-step digest token, 300 s plan
budget, one bounded replan with the trusted digest no-repeat filter,
failed-mutation stop (dependents skipped, `needs_attention`), and
cancellation at step boundaries that preserves completed steps and
never claims rollback.

**Presentation.** `taskui.presenter.task_view()` gained `objective`
(plan summary or the command, bounded) and `plan_progress`
(`total_steps`, per-status counts, `current_index`, `has_plan`,
`partial`) — pure derivations from record state, so the panel can show
the current step and the completed/failed/skipped split without
re-deriving it, and can flag a genuine partial outcome (terminal but
not `completed`, with at least one verified success). No raw prompts,
credentials or document contents ever reach the view.

**Verification of workflows** lives in
`scripts/verify_desktop_workflows.py` (real manager, real sandboxed
files, real approval digests, real `/proc` + AT-SPI evidence; exit 0 =
every claim evidence-backed) and `tests/test_desktop_workflows.py`
(sandbox integration: real manager/pipeline/permissions/approval/
verifier/audit with a scripted model and a fake desktop).

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
| `ALWIN_COMPANION_NAME` | settings, else `friend` | Companion greeting name |
| `ALWIN_COMPANION_GREETING` | settings, else on | `0/false/no/off` disables the greeting |
| `ALWIN_COMPANION_QUIET_HOURS` | settings, else off | `HH:MM-HH:MM` no-greeting window |

Companion preferences persist to `<state_dir>/companion_settings.json`
(0600, closed schema — bool/str keys only, cosmetic-only — never read
by the trusted core). v0.9 voice keys: `mic_enabled` (default on),
`mic_level_meter` (on), `language_auto` (on — a preferred
`language_preferred` only applies when detection is unclear),
`spoken_responses` (off — opt-in), `speech_rate` (str, 0.5–2.0),
`voice_id` (empty = per-language), `asr_model` (small; `tiny`/`base`/
`small`/`medium`), `wake_word_enabled` (off; honest not-implemented
placeholder). The v0.8 `mute_voice` placeholder key is gone — old files
lose only that obsolete key on save. v0.15 memory keys: `memory_enabled`
(default on — a real switch: off stops both storage and retrieval) and
`memory_project` (default `""` = no project scope; an explicit label
only, never a guessed association); both are cosmetic bool/str like the
rest and can never grant a permission. The memory store itself lives at
`<state_dir>/memory.db` (0600 in the 0700 state directory). v0.16
scheduled jobs add **no new settings keys** — the scheduler store is
`<state_dir>/scheduler.db` (same owner-only layout), scheduling exists
purely by that store opening successfully, and Settings only gains a
read-only status line plus the manage dialog.

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
v0.4 suite is **404 tests**; v0.5 adds **149 more** (total **553**);
v0.6 adds **185 companion tests** for a total of **738 tests**; v0.7
adds **29 more** (27 dedicated character tests, 1 character-purity
meta-test, 1 updated idle-animation pin) for a total of **767 tests**;
v0.8 adds **42 more** (the 40-test dedicated animated-world scene
suite plus 2 new source-purity meta-tests for `scene.py`/`bedroom.py`;
the module-set and settings-schema pins were extended) for a total of
**809 tests**; v0.9 adds **167 more** for a total of **976 tests**
(974 passed, 2 skipped on this machine); v0.9.1 adds **16 more**
(credential redaction in reply-failure reasons + mic-recovery
regressions) and v0.10 adds **46 more** — the wake-word service/
detector/window suites, the `wait_stopped` shutdown contract and the
21-module security pin — and v0.11 adds **35 more** (session-context
recording/bounds/clearing, action-vs-chat routing, routing-honesty
prompt clauses, window conversational/stale-TTS/clear-control tests,
the sliced stop-aware bus-wait fix) — and v0.12 adds **36 more**
(plan schema + key-hoisting sanitisation, capability discovery/cache,
planning service outcomes and credential scrubbing, manager plan
flows with real sandbox pipelines, presenter plan-state rendering, the
CRLF read-verification regression) — for a total of **1109 tests
(1107 passed, 2 skipped)**; the v0.13 suite adds **86 more** (the
desktop-control suites: registry tiers, launch/status/focus/close
evidence with a fully fake desktop, manager plan flows with the
fake-desktop sandbox, credential-scrubbed window metadata) — for
**1195 passed, 2 skipped (2752 subtests)**; the v0.14 suite adds
**28 more** (`tests/test_desktop_workflows.py`: derivation-reference
validation and hoisting, handoff bounds/re-validation/root
containment, prompt-injection content stays data, capability-aware
planning redirects, Workflow A/B/C/D through the real sandboxed
manager, wrong-digest refusal, policy denial, mid-plan cancellation,
replan no-repeat + budget, honest verification failure, credential
redaction, presenter progress/partial) — for **1223 passed, 2
skipped (2755 subtests)**; the v0.15 suite adds **60 more**
(`tests/test_companion_memory.py`: typed-store CRUD and owner-only
file modes, v1→v2 migration with row preservation, corrupt/newer/
locked/unavailable failures with content-free messages, bounds/
normalization/secret/transcript refusal, bounded scope-filtered
retrieval with dedupe/conflict/expiry/review/determinism/priority,
mocked creation-policy and labelled-prompt integration, disabled
read+write, AST trust isolation, cross-project leakage, regression
pins) — for **1283 passed, 2 skipped (2876 subtests)**; the v0.16
suite adds **72 more** (`tests/test_scheduler.py`: one-time/daily/
weekly/selected-weekday schedules, invalid dates/times/timezones, DST
spring-forward and fall-back edges with real zoneinfo, restart
recovery, missed-occurrence coalescing, duplicate/concurrent occurrence
prevention, pause/resume/edit/cancel, approval-window expiry and
refusal with a mocked manager, bounded dispatch retries, condition
ops against real files/roots/registry, NL intents including
question-never-creates and vague-phrase refusal, conversation
integration with the confirmation slot, credential/injection/AST trust
isolation, storage-failure honesty, regression pins) — for **1355
passed, 2 skipped (2876 subtests)**; the v0.17 suite adds **40 more**
(`tests/test_app_interaction.py`: missing app/window, duplicate-name
ambiguity with zero executions, stale/defunct targets (including
defunct-between-find-and-activate), unsupported capabilities,
oversized names, traversal bounds on huge trees, bounded listings with
document-text exclusion and truncation honesty, the measured GNOME
Text Editor tree (identify/activate with verified-vs-unverified
outcomes, unsupported read), uncertain-mutation single-attempt pins,
planner/permission pins for the new read-only action, a control named
"Approve all actions" authorizing nothing, AST trust isolation, no
synthetic-input surface, registry contract pins) — for **1395
passed, 2 skipped (2889 subtests)**; the v0.18 suite adds **28 more**
(`tests/test_document_workflows.py`: create/read/replace roundtrip
with digest-bound approvals, empty content, missing target refused as
non-create, oversized/binary rejection, duplicate-create protection,
injected write failure preserving the original byte-for-byte with no
temp litter, traversal/out-of-roots denial, two-layer symlink
handling, unauthorized overwrite refused at the gate, stale and
tampered approvals rejected, injection-in-content inert, audit content
redaction, mismatched-landing simulated as FAILED, single-attempt
no-retry pin, partial-sequence semantics, same-file double mutation
conservatively FAILED, closed-schema pins) — for **1423 passed, 2
skipped (2895 subtests)**; the v0.19 suite adds **24 more**
(`tests/test_reliability.py`: tier honesty — uninstalled never
yields interact, readiness facets incl. running-but-bus-absent =>
blocked, transient probe failure => unknown, detection failure =>
unknown install, bindings-absent => unavailable, adapter failure
categories across all three adapters, failure_data whitelisting and
bounding, explicit partial-completion blocks (effect preserved AND
reported, remaining steps skipped with reason), no-auto-retry,
gate-cancellation, denied-step isolation, closed-registry pin,
diagnostic-redaction AST pins) — for **1447 passed, 2 skipped (2895
subtests)**; the v0.20 suite adds **17 more**
(`tests/test_planning_recovery.py`: idempotency-class pins incl.
mutating-actions-never-read-only and unknown=>never, pure resume-plan
exclusion rules (verified replayed, uncertain-with-reconciliation,
undispatched resumable, empty-residual refusal), real-manager resume
flows — completed mutation never repeated (byte + mtime evidence),
original approval refused for the resumed plan, resume budget bound,
cancel-during-resume, idle-manager guard, overwrite clarification
note, needs_attention-as-resumable) — for **1464 passed, 2 skipped
(2895 subtests)**. The v0.9 additions were: the six voice-stack suites
(registry, detection, conversation, capture, recognition, voice output),
the window voice-cycle suite (real `GLib.idle_add` marshalling pumped
on the default main context, all devices/models/LLM faked), the
STATE_SPEAKING pose/transition/asset extensions, the extended settings
schema pins, and the extended security meta-suite (20-module set,
per-module voice allowlists, single-`ctypes`-module exemption):
the full
v0.1/v0.2 set (242, preserved
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

Plus the companion tests (also all headless — no
window, no display, no animation loop; drawing is exercised on in-memory
`cairo.ImageSurface`s and GTK objects are only imported/constructed,
never shown), including the v0.9 voice suites:

* character state controller (`test_companion_states.py`) — exactly 17
  states with labels/expressions/edges (including `speaking` and its
  120 s safety-net auto-return), valid edges accepted, invalid
  edges rejected honestly (calm state stays put; active state falls
  back through exactly one calm hop), truthful targets
  (`waiting_for_approval`, `celebrating`) recovered instead of lost,
  unknown states never raising and never claimed, auto-return delays,
  injected clock, renderer exceptions captured (bounded list) and never
  propagating, `on_change` bugs contained;
* task-event adapter (`test_companion_adapter.py`) — every lifecycle
  state mapped, approval → `waiting_for_approval`, `completed` the only
  path to `celebrating` (failure/needs-attention/cancelled/unknown never
  celebrate), action → reading/writing/coding/working identification
  with conservative code-extension matching, running-sequence step
  mapping, bounded curated speech with error internals/file content
  never spoken, broken records never raising, and a structural check
  that the adapter has no manager reference and no approve/execute
  surface;
* greeting (`test_companion_greeting.py`) — generic default name,
  env/setting override, bounds/control-char stripping, time-of-day
  parts, quiet-hours parsing (wraps midnight, malformed raises),
  once-per-session dedup, once-per-day marker (0600, new day renews,
  unwritable marker tolerated), disabled preferences;
* renderer seam + artwork (`test_companion_renderer.py`) — abstract
  contract, expression degradation, a stand-in future-3D renderer
  driving the unchanged controller, all 17 states drawing headless and
  deterministically with distinct frames, motion over time, missing/
  corrupt asset fallback, shipped per-state asset layout with no
  required image files;
* full-body character (`test_companion_character.py`, v0.7) — the
  drawing is non-empty and substantial (not a floating face), inside
  real image bounds with head/hands/feet visible at five widget sizes,
  uniformly scaled (never stretched), pose determinism and
  unknown-state fallback, `Pose.lerp` endpoint/midpoint behaviour,
  subtle idle breathing, natural blinking (open and closed frames
  render), elapsed-time animation (a time jump lands on the correct
  pose), pairwise-distinct frames for the eight key states
  (writing ≠ coding, thinking ≠ listening), a genuinely oscillating
  greeting wave, celebration only from a trusted verified `completed`
  (failed/needs-attention records never celebrate), zero/tiny canvas
  robustness, expression overrides — plus `test_companion_security.py`
  pinning `character.py` to cairo/math/dataclasses/typing imports only;
* presentation/settings (`test_companion_presentation.py`) —
  bounded bubbles (markup stays inert text), tone per state, accessible
  subtitle, minimal status without digests/error internals, no
  approve/reject control, closed cosmetic-only settings schema (bool/str
  only, v0.9 voice keys, `mute_voice` → `spoken_responses` migration)
  with 0600 roundtrip and corrupt-file defaults, bounded settings view;
* voice stack (all mocked — no microphone, no model, no speaker, no
  network): closed 15-language registry with RTL and espeak ids
  (`test_language_registry.py`), detection over 15 code samples, Tanglish,
  mixed/short/unknown input, preference fallback and stale hints
  (`test_language_detection.py`), the shared conversation pipeline
  (language switching, instruction prompt, verbatim submission, no
  English fallback, engine-free) (`test_companion_conversation.py`),
  capture lifecycle incl. cancel-discards-PCM (`test_companion_voice_
  input.py`), ASR async/cancel/silence/no-speech/model-status honesty
  (`test_companion_speech_recognition.py`), TTS voice/rate/generation
  cancellation/completion truth (`test_companion_voice_output.py`);
* persistent memory (`test_companion_memory.py`, 60 tests — store
  operations are SANDBOX/real local SQLite in temporary directories,
  conversation integration is MOCKED, trust isolation is AST/STATIC):
  CRUD + owner-only modes + restart persistence, atomic v1→v2 migration
  with row preservation, corrupt/newer/locked/unavailable failures with
  content-free honest messages and no fake saves, bounds/normalization/
  closed sets, secret- and transcript-shaped refusal without echoing,
  bounded project-scoped retrieval (dedupe, conflict/expiry/review
  flags, deterministic order, category priority, empty ≠ negative
  fact), creation policy (explicit only, memory turns never reach the
  manager, disabled = no writes and no reads), labelled untrusted-data
  prompt block, cross-project leakage, injected-memory authorization
  immutability, and regression pins for the existing pipeline;
* companion window (`test_companion_window.py` +
  `test_companion_window_voice.py`) — headless import and
  construction, constants (low-frequency idle check, modest frame
  rate), light self-contained CSS, teardown ignores late records; plus
  the full push-to-talk cycle pumped through the real GLib default main
  context: verbatim submit, silence refused before ASR, forced vs auto
  language, honest recognition errors, cancel drops late results,
  uncertain → ask, busy-state refusal, spoken reply → `speaking` → idle
  on engine truth (reply never re-submitted), LLM/voice failure =
  silence, settings applied live, teardown releases everything;
* observers (`test_taskui_observers.py`) — dual delivery with the
  `on_update` slot, observer exceptions cannot break a task, a truthy
  observer return approves nothing (the mutating task still waits for a
  digest-bound approval), duplicate registration collapsed, removal
  stops delivery;
* security meta-checks (`test_companion_security.py`) — AST analysis of
  every one of the 22 `companion` modules: no shell/spawn/eval/network
  imports or calls, per-module voice allowlists at any import depth,
  `ctypes` reachable from exactly `voice_output.py`, `wave`/`audioop`
  and every recording/network library banned package-wide, no import of
  the execution engine anywhere in the
  package (which covers `memory.py` — stdlib-only, pinned in
  `VOICE_MODULE_IMPORTS`), only `__main__.py` may import `taskui`, the state controller
  and adapter are pure, conversation reaches only the duck-typed
  `manager.submit()` (and carries the language instruction), no
  `set_markup`/remote CSS in the window, no
  web-server surface, and no hard-coded user name.

Four separate **opt-in** manual smoke tests exist and are never
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
* `ALWIN_COMPANION_SMOKE=1 scripts/manual_smoke_companion.sh` — the
  v0.8 companion: the headless self-check (`python3 -m companion
  --check`, including the bedroom scene model), the headless walkthrough
  (`--smoke`, including the morning wake-up sequence, greeting dedup,
  approval mapping, renderer-error isolation and asset fallback), the
  companion's automated tests, and finally the real task
  window **and** the real Alwin window (`python3 -m taskui
  --companion`) in a throwaway sandbox home so you can watch him wake up
  in his illustrated bedroom, **walk** to his greeting spot and desk,
  breathe, blink, wave, think and show the
  writing/coding/celebrating/error poses, the greeting, subtitles,
  reduced motion and the companion controls. Skipped with an honest
  message if GTK4 or a display is missing.

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
* **3D character renderer:** implement
  `companion.renderer.CharacterRenderer` in a new module (see
  "2D → 3D migration strategy" above) — the state vocabulary, transition
  graph, task-event adapter, greeting, speech and controls are reused
  unchanged, and 3D-only concepts stay out of `ai_core`/`integrations`.
  No engine or runtime is added until that milestone is actually taken.
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
