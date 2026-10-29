#!/usr/bin/env python3
"""Explicit speech-model installer for ALWIN FUSION (v0.11.0).

The companion **never** downloads anything: ``companion.speech_recognition``
loads models with ``local_files_only=True``, so a missing model is an
honest error, not a silent fetch.  This script is the *only* sanctioned
way to install one — it runs outside the application, asks for explicit
confirmation first, and reports the approximate download size and the
free space on the target machine before touching the network.

Usage::

    python3 scripts/install_voice_models.py --status    # what is installed
    python3 scripts/install_voice_models.py             # install 'small'
    python3 scripts/install_voice_models.py --model tiny
    python3 scripts/install_voice_models.py --model base --yes

Notes (honest):

* sizes are **approximate** (`companion.speech_recognition.
  APPROX_MODEL_MIB`, measured on the download that was verified during
  development) — a few MiB of variance is normal,
* the download comes from the Hugging Face hub (``Systran/
  faster-whisper-<size>``) through ``huggingface_hub`` — Alwin itself
  has no network code at all,
* recognition quality varies strongly per language; see the README's
  quality table before picking a model,
* ``--yes`` skips the interactive confirmation for scripted use; there
  is no configuration file or environment variable that can make the
  companion download on its own.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

# The closed model set + approximate sizes come from the companion
# module itself so the installer and the application can never drift.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from companion.speech_recognition import (  # noqa: E402
    APPROX_MODEL_MIB,
    DEFAULT_MODEL_SIZE,
    MODEL_SIZES,
    _hub_cache_dir,
    model_installed,
    model_status,
)

REPO_PREFIX = "Systran/faster-whisper-"


def _format_mib(mib: int) -> str:
    return f"{mib / 1024:.1f} GiB" if mib >= 1024 else f"{mib} MiB"


def print_status() -> int:
    """Report what is installed — no network, no prompt."""
    print("Speech models (faster-whisper, local Hugging Face cache):")
    print(f"  cache directory: {_hub_cache_dir()}")
    for size in MODEL_SIZES:
        status = model_status(size)
        mark = "installed" if status["installed"] else "not installed"
        print(f"  {size:<7} ~{APPROX_MODEL_MIB[size]:>5} MiB  {mark}")
    installed_any = any(model_installed(size) for size in MODEL_SIZES)
    if not installed_any:
        print()
        print("No model is installed yet. Voice input will honestly say")
        print("'model not installed' until you install one explicitly:")
        print("  python3 scripts/install_voice_models.py --model "
              f"{DEFAULT_MODEL_SIZE}")
    return 0


def install(model_size: str, assume_yes: bool) -> int:
    mib = APPROX_MODEL_MIB[model_size]
    cache = _hub_cache_dir()

    if model_installed(model_size):
        print(f"Model '{model_size}' is already installed "
              f"(~{mib} MiB, cache: {cache}).")
        return 0

    try:
        free_bytes = shutil.disk_usage(cache if os.path.isdir(cache)
                                       else os.path.expanduser("~")).free
    except OSError:  # pragma: no cover - exotic filesystems
        free_bytes = 0
    free_mib = free_bytes // (1024 * 1024)

    print(f"About to download the faster-whisper '{model_size}' model:")
    print(f"  source:     Hugging Face hub ({REPO_PREFIX}{model_size})")
    print(f"  size:       ~{_format_mib(mib)} (approximate)")
    print(f"  cache:      {cache}")
    print(f"  free space: {_format_mib(free_mib)} on the target device")
    if free_bytes and free_bytes < mib * 1024 * 1024 * 1.2:
        print()
        print("NOT enough free space is available for a safe download.")
        print("Free up disk space (or pick a smaller model, e.g. 'tiny')")
        print("and try again. Nothing was downloaded.")
        return 1
    print()
    print("Recognition runs fully offline afterwards; the model is stored")
    print("locally and never uploaded. The companion will not download")
    print("anything by itself.")

    if not assume_yes:
        try:
            answer = input("Proceed with the download? [y/N] ").strip()
        except EOFError:  # pragma: no cover - non-interactive
            answer = ""
        if answer.lower() not in ("y", "yes"):
            print("Cancelled — nothing was downloaded.")
            return 1

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("The 'huggingface_hub' package is required to download a "
              "model (pip install huggingface_hub). Nothing was "
              "downloaded.")
        return 1

    repo = f"{REPO_PREFIX}{model_size}"
    print(f"Downloading {repo} ...")
    try:
        snapshot_download(
            repo,
            # Only the files faster-whisper needs at inference time.
            allow_patterns=["config.json", "model.bin", "tokenizer.json",
                            "vocabulary*"],
        )
    except Exception as exc:  # noqa: BLE001 - report honestly, exit non-zero
        print(f"Download failed: {exc}")
        print("No partial state is claimed: check the message above, then "
              "re-run this script (an interrupted download can be resumed).")
        return 1

    if not model_installed(model_size):
        print("The download finished but the cache does not look complete "
              "— re-run this script to resume it.")
        return 1
    print(f"Model '{model_size}' installed (~{_format_mib(mib)}).")
    print("It will be picked up on the next launch; recognition stays "
          "offline.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 scripts/install_voice_models.py",
        description=("Explicitly install a local faster-whisper speech "
                     "model (the companion never downloads anything)."),
    )
    parser.add_argument("--model", default=DEFAULT_MODEL_SIZE,
                        choices=MODEL_SIZES,
                        help=f"model size to install (default: "
                             f"{DEFAULT_MODEL_SIZE})")
    parser.add_argument("--status", action="store_true",
                        help="report installed models and exit "
                             "(no network, no prompt)")
    parser.add_argument("--yes", action="store_true",
                        help="skip the interactive confirmation")
    args = parser.parse_args(argv)

    if args.status:
        return print_status()
    return install(args.model, args.yes)


if __name__ == "__main__":  # pragma: no cover - manual workflow
    sys.exit(main())
