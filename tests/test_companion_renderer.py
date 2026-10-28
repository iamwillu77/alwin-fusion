"""v0.6 renderer tests: interface contract, 2D artwork, asset fallback.

Everything here runs **headless** — the drawing core is pure cairo on a
``cairo.ImageSurface``, so no display, window or animation loop is ever
needed.  Covered:

* the renderer interface is a clean seam (an abstract contract any
  future 3D renderer can implement; validated states/expressions),
* the null renderer used by tests and degraded machines,
* the procedural vector character draws every state deterministically,
  each state visually distinct (real placeholder artwork, not a blank),
* missing/corrupt per-state assets fall back gracefully (never crash),
* the on-disk asset layout supports per-state replacement.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import cairo

from companion.renderer import (
    ALL_EXPRESSIONS,
    CharacterRenderer,
    NullRenderer,
    RendererUnavailable,
)
from companion.renderer2d import DEFAULT_ASSET_ROOT, AssetStore, draw_character
from companion.states import ALL_STATES, EXPRESSIONS, CharacterController

WIDTH, HEIGHT = 160, 170


def render(state: str, *, t: float = 1.0, expression: str = "neutral") -> bytes:
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, WIDTH, HEIGHT)
    ctx = cairo.Context(surface)
    draw_character(ctx, WIDTH, HEIGHT, state, expression=expression, t=t)
    surface.flush()
    return bytes(surface.get_data())


class InterfaceTests(unittest.TestCase):
    def test_interface_is_abstract_and_complete(self):
        with self.assertRaises(TypeError):
            CharacterRenderer()   # cannot instantiate the contract itself
        required = ("is_available", "show_state", "set_expression", "tick")
        for name in required:
            with self.subTest(name=name):
                self.assertTrue(hasattr(CharacterRenderer, name))

    def test_interface_exposes_host_integration_hooks(self):
        for name in ("attach", "detach"):
            with self.subTest(name=name):
                self.assertTrue(hasattr(CharacterRenderer, name))

    def test_expression_vocabulary_covers_the_states(self):
        for expression in EXPRESSIONS.values():
            self.assertIn(expression, ALL_EXPRESSIONS)
        self.assertIn("neutral", ALL_EXPRESSIONS)

    def test_validate_state_rejects_invented_states(self):
        with self.assertRaises(ValueError):
            CharacterRenderer.validate_state("doing-something-new")
        self.assertEqual(CharacterRenderer.validate_state("idle"), "idle")

    def test_validate_expression_degrades_to_neutral(self):
        self.assertEqual(CharacterRenderer.validate_expression("wild"), "neutral")
        self.assertEqual(CharacterRenderer.validate_expression("proud"),
                         "proud")

    def test_future_3d_renderer_satisfies_the_contract(self):
        # A stand-in for the future 3D implementation: same interface,
        # zero changes to the controller/adapter/greeting/speech.
        class Fake3DRenderer(CharacterRenderer):
            name = "fake 3d"

            def __init__(self):
                self.states = []

            @property
            def is_available(self):
                return True

            def show_state(self, state, *, expression=None):
                self.validate_state(state)
                self.states.append((state, expression))

            def set_expression(self, expression):
                pass

            def tick(self, now):
                pass

        renderer = Fake3DRenderer()
        controller = CharacterController(renderer)
        self.assertIsInstance(renderer, CharacterRenderer)
        controller.enter("waiting_for_approval")
        controller.force("celebrating")
        self.assertEqual(renderer.states[-2][0], "waiting_for_approval")
        self.assertEqual(renderer.states[-1][0], "celebrating")
        self.assertEqual(renderer.states[-1][1], "proud")

    def test_null_renderer_is_fully_headless(self):
        renderer = NullRenderer()
        self.assertTrue(renderer.is_available)
        renderer.show_state("thinking", expression="thoughtful")
        renderer.set_expression("focused")
        renderer.tick(12.5)
        renderer.attach(object())
        renderer.detach()
        self.assertEqual(renderer.state, "thinking")
        self.assertTrue(renderer.calls)
        with self.assertRaises(ValueError):
            renderer.show_state("invented-state")


class VectorArtworkTests(unittest.TestCase):
    def test_every_state_draws_without_error(self):
        for state in ALL_STATES:
            for t in (0.0, 0.4, 2.7, 19.3):
                with self.subTest(state=state, t=t):
                    data = render(state, t=t, expression=EXPRESSIONS[state])
                    self.assertTrue(any(b for b in data),
                                    "state drew a blank frame")

    def test_frames_are_deterministic(self):
        first = render("thinking", t=1.234)
        second = render("thinking", t=1.234)
        self.assertEqual(hashlib.sha256(first).hexdigest(),
                         hashlib.sha256(second).hexdigest())

    def test_each_state_has_a_distinct_frame(self):
        hashes = {state: hashlib.sha256(render(state, t=1.0)).hexdigest()
                  for state in ALL_STATES}
        self.assertEqual(len(set(hashes.values())), len(ALL_STATES),
                         "every state renders visibly differently")

    def test_animation_changes_over_time(self):
        # Animated states must actually move (it is an animation, even
        # though the artwork is placeholder vector drawing).
        for state in ("thinking", "celebrating", "working", "walking"):
            with self.subTest(state=state):
                self.assertNotEqual(render(state, t=0.0),
                                    render(state, t=0.5))

    def test_unknown_state_still_draws_safely(self):
        data = render("not-a-real-state")
        self.assertTrue(any(b for b in data))

    def test_expressions_change_the_frame(self):
        self.assertNotEqual(render("idle", expression="happy"),
                            render("idle", expression="sleepy"))


class AssetFallbackTests(unittest.TestCase):
    def test_missing_assets_resolve_to_procedural_rendering(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AssetStore(Path(tmp))
            self.assertEqual(store.available_states(), [])
            for state in ALL_STATES:
                with self.subTest(state=state):
                    self.assertIsNone(store.texture_for(state))

    def test_corrupt_asset_falls_back_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "idle").mkdir()
            (root / "idle" / "frame.png").write_bytes(b"not a png at all")
            store = AssetStore(root)
            # Whether or not GTK is present, a broken asset must never
            # raise — it degrades to the vector character.
            self.assertIsNone(store.texture_for("idle"))

    def test_missing_asset_directory_is_fine(self):
        store = AssetStore(Path("/definitely/not/here"))
        self.assertEqual(store.available_states(), [])
        self.assertIsNone(store.texture_for("error"))

    def test_texture_results_are_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AssetStore(Path(tmp))
            first = store.texture_for("idle")
            second = store.texture_for("idle")
            self.assertIs(first, second)

    def test_shipped_asset_layout_has_one_directory_per_state(self):
        self.assertTrue(DEFAULT_ASSET_ROOT.is_dir(),
                        "asset root directory ships with the package")
        self.assertTrue((DEFAULT_ASSET_ROOT / "README.md").is_file(),
                        "assets README documents the replacement convention")
        for state in ALL_STATES:
            with self.subTest(state=state):
                self.assertTrue((DEFAULT_ASSET_ROOT / state).is_dir(),
                                f"missing asset directory for {state}")
        self.assertTrue((DEFAULT_ASSET_ROOT / "base").is_dir())

    def test_assets_are_optional(self):
        # No image files are required: the package ships dirs only.
        shipped = list(DEFAULT_ASSET_ROOT.glob("*/frame.png"))
        self.assertEqual(shipped, [],
                         "the character must run without image assets")


if __name__ == "__main__":
    unittest.main()
