# Alwin character assets (v0.7.0)

**Status: this milestone ships a real animated character — a full-body
original cartoon boy** drawn procedurally with cairo (large expressive
head, tousled dark-brown hair, big brown eyes with brows, small nose,
friendly mouth, rounded cheeks, neck, a blue hoodie with an original
ALWIN chevron emblem, two jointed arms with hands, legs and red
sneakers). No image files are required to run the companion, no artwork
was copied from macOS or any other product, and no image-processing
dependencies were added.

The procedural drawing in `companion/character.py` is the **default and
the fallback**. It is a pure-cairo, headless-testable rendering of a
single reusable `Pose` (a float-only dataclass), so every state shows
the *same* character with the *same* proportions, hair and clothing —
only the pose and facial knobs change.

## Per-state override convention

Every character state has its own directory:

```
character/
├── base/                     # reserved: future shared artwork
├── idle/
│   └── frame.png             # ← optional override for this state
├── greeting/
│   └── frame.png
├── listening/  thinking/  walking/  writing/  coding/  reading/
├── waiting_for_approval/  working/  verifying/
├── celebrating/  error/  resting/  sleeping/  waking_up/
├── desk/                     # reserved: desk/notebook/laptop props
```

Drop a `frame.png` into a state directory and that **one** state starts
showing the image (scaled to fit the character area) instead of the
procedural drawing. Everything else keeps rendering as before, so
individual states can be replaced independently and incrementally.

Rules implemented in `companion.renderer2d.AssetStore`:

* missing file → procedural vector rendering (graceful fallback),
* unreadable/corrupt file → procedural vector rendering (a bad asset
  degrades, it never crashes),
* assets are loaded once and cached per state,
* the renderer never infers anything from an asset — state semantics
  come only from the trusted task-event adapter.

## Suggested artwork guidance (if you add real assets)

* 240 × 260 px or larger, transparent background, PNG (RGBA).
* Keep the palette warm and light (the window background is a warm
  off-white `#f4f1ea`); avoid black-background-only art.
* Match the procedural proportions (large head ≈ 40 % of height) so an
  overridden state does not visibly "jump" against neighbouring states.
* Document the source and licence here if you add files: format, pixel
  dimensions, licence (e.g. CC0 / your own), and how to regenerate.
* Multi-frame animation sequences (per-state frame strips) are future
  work alongside the renderer interface (see `docs/ARCHITECTURE.md`,
  "2D → 3D path").

## Which states are "real animation" today

All 16 states animate procedurally with genuine motion driven by
elapsed time (not frame counts), so delayed frames stay correct:

* **idle** — subtle breathing (vertical bob), gentle head micro-motion,
  natural varied blinking (~2.5–4 s, occasional double blink),
* **waking_up** — a morning stretch (arms up) and yawn, eyes opening,
* **greeting** — smile, blush, one hand raised and waved several times,
  then a smooth return to idle (does not restart on redraw),
* **listening** — attentive wide eyes, raised brows, small nod,
* **thinking** — gaze up-and-to-one-side, hand at the chin, thought dots,
* **walking** — alternating leg/arm swing with a body bob,
* **writing** — seated at a desk, leaning over a notebook, a hand making
  a small scribbling motion with a pencil (decorative),
* **coding** — seated at a desk with a laptop, both hands tapping the
  keyboard, decorative code glyphs (`< / >`) floating above,
* **reading** — a book held open, eyes scanning left-to-right,
* **waiting_for_approval** — a raised open palm and a pulsing "…",
* **working** — holding a small wrench with a slowly turning gear,
* **verifying** — a magnifying glass held up, gaze sweeping side to side,
* **celebrating** — both arms raised, a big open smile, a small jump and
  sparkles; entered **only** on a trusted verified completion,
* **error** — a concerned-but-friendly expression, a hand to the head
  and a small sweat drop; **never** a celebration,
* **sleeping / resting** — slow breathing, closed eyes, drifting "z"s.

The desk, notebook, laptop, book, wrench, magnifier and floating symbols
are **honest visual depictions** of what the character is doing — they
are never evidence that a task succeeded. Only the trusted execution and
verification pipeline establishes task outcomes.

The states are real *behaviour* — they are entered from trusted task
events — while the artwork is original procedural vector drawing that
can be replaced per-state in this directory.
