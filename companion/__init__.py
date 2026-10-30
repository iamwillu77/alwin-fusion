"""Alwin Companion — a friendly desktop character for ALWIN FUSION (v0.13.0).

The companion is a **presentation layer on top of the existing trusted
task experience**.  It adds a persistent, lightweight 2D character
window (Alwin) that reflects real task states with truthful animations,
lives in an **animated bedroom world** (v0.8), and shows a configurable
startup greeting.

Layering (each talks only to the one below it):

* ``companion.window``     — thin GTK4 view (character area, speech
  bubble, status strip, controls); owns the morning wake-up sequence
  and wires task states to the scene; executes nothing,
* ``companion.presentation``— pure view-model (headless, unit-tested),
* ``companion.adapter``    — task-event adapter: trusted TaskRecord
  states -> character states + curated speech (cannot grant
  permissions or change outcomes),
* ``companion.states``     — the character state controller (validated
  transitions, safe fallbacks, error-isolated renderer dispatch),
* ``companion.scene``      — the renderer-independent world model (v0.8):
  scene objects, named positions, elapsed-time movement, the wake-up
  sequence and a closed command allowlist; pure Python, never executes
  or approves anything,
* ``companion.bedroom``    — the cairo bedroom artwork (v0.8): the room
  the scene describes, drawn headless; reads the scene, never mutates it,
* ``companion.renderer``   — renderer interface (+ headless null
  renderer); ``companion.renderer2d`` is the first implementation
  (procedural cairo vector character + the animated world, replaceable
  per-state assets),
* ``companion.greeting``   — configurable startup greeting (text-first,
  quiet hours, once-per-day), ``companion.voice`` — the replaceable
  TTS/ASR interface (honestly absent today),
* ``companion.settings``   — validated local preferences.

Nothing here plans, permission-checks, approves, executes, verifies or
audits anything: that remains exclusively ``ai_core``/``integrations``
via ``taskui.manager``.  The animated world is purely visual.  A future
3D renderer replaces only ``companion.renderer``/``renderer2d`` (and the
cairo ``bedroom``); the scene model and every other module are reused
unchanged.
"""

from companion.adapter import TaskEventAdapter, action_state
from companion.greeting import (
    GreetingConfig,
    GreetingController,
    greeting_text,
    parse_quiet_hours,
    sanitize_name,
)
from companion.presentation import (
    bubble_view,
    controls_view,
    subtitle_view,
    task_status_view,
)
from companion.renderer import CharacterRenderer, NullRenderer
from companion.scene import SceneController
from companion.states import (
    ALL_STATES,
    EXPRESSIONS,
    STATE_LABELS,
    TRANSITIONS,
    CharacterController,
    InvalidCharacterState,
    is_valid_transition,
)
from companion.voice import NullVoice, VoiceEngine, voice_status

__all__ = [
    "ALL_STATES",
    "CharacterController",
    "CharacterRenderer",
    "EXPRESSIONS",
    "GreetingConfig",
    "GreetingController",
    "InvalidCharacterState",
    "NullRenderer",
    "NullVoice",
    "STATE_LABELS",
    "SceneController",
    "TRANSITIONS",
    "TaskEventAdapter",
    "VoiceEngine",
    "action_state",
    "bubble_view",
    "controls_view",
    "greeting_text",
    "is_valid_transition",
    "parse_quiet_hours",
    "sanitize_name",
    "subtitle_view",
    "task_status_view",
    "voice_status",
]
