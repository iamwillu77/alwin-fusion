# Alwin Fusion — Security Model and Limitations

Version: 0.2.0 — Milestone: Local Document and Spreadsheet Automation

This document states what the prototype defends against, how, and —
importantly — **what it does not defend against**. Passing tests does
not make a system secure; the boundaries below are what the code is
designed around, and the *Known limitations* section is what remains
open.

## Assets and adversaries (working assumptions)

Assets: files inside the user's configured permitted roots, the audit
log's integrity, and the user's system outside those roots.

Assumed-trusted: the Alwin Fusion code itself, the Python
interpreter, the OS, and the human user making approval decisions.

Assumed-untrusted: natural-language input (it may be accidental or
hostile), local LLM output (models can be wrong or prompt-injected),
and anything a concurrent process does to the filesystem.

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
  `create_spreadsheet`, `list_directory`, `read_file`, `explain`.
  The exact set is pinned by tests (v0.2 deliberately extended it from
  five to eight actions; all three new ones are create-only,
  approval-gated and cannot delete, move, overwrite or execute code).
* There is no delete, overwrite, move, chmod, or shell action — not
  disabled, simply absent (meta-tests assert this).
* Model output is parsed with `json.loads` and validated by the same
  code as the deterministic parser. Nothing is ever `eval`/`exec`-ed,
  formatted into a shell command, or passed to `subprocess`.
* File creation uses `os.open(..., O_CREAT|O_EXCL)`, so an existing
  file can never be clobbered even by a race. All v0.2 document
  writes go through the same `O_EXCL` path.

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
  resolved operation is displayed.
* EOF, Ctrl-C, or any input failure while approving = **no**.
* There is deliberately no `--yes`/`--force` bypass in this milestone.
* `--dry-run` performs no writes and never even asks for approval.

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

### 5. Audit log

* One JSON line per task attempt (including rejected/denied/
  unsupported) in `<state_dir>/audit.jsonl`, directory 0700, file
  0600, written via `os.open` with `0600`.
* Fields: task id, UTC timestamp, parser, proposed action,
  permission decision, approval, execution, verification, status.
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
  `validate_action()`; an out-of-allowlist proposal is discarded.
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
  never modify the real home directory.

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

## Reporting security issues

Open an issue in the project repository with the label `security`,
including the version, your configuration (roots, not personal file
contents), and reproduction steps. Do not include real secrets or
personal file contents in reports.
