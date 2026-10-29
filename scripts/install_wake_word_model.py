#!/usr/bin/env python3
"""Explicit wake-word model installer for ALWIN FUSION (v0.10.0).

The companion **never** downloads anything: the wake-word detector
loads its model from a fixed local path and honestly reports an
install hint when it is missing.  This script is the *only* sanctioned
way to fetch it — it runs outside the application, states the licence,
reports the size and your free disk space, and asks before downloading.

Model:  vosk-model-small-en-us-0.15 (Alpha Cephei) — **Apache 2.0**
        (https://alphacephei.com/vosk/models), ~40 MiB zip / ~68 MiB
        extracted.  Engine: vosk (PyPI) — Apache 2.0.
The model is used purely locally for the stage-1 keyword gate; audio
never leaves this machine.

Usage::

    python3 scripts/install_wake_word_model.py --status   # report only
    python3 scripts/install_wake_word_model.py            # install (asks)
    python3 scripts/install_wake_word_model.py --yes      # scripted

Target: ~/.cache/alwin-vosk/ (override with ALWIN_WAKE_MODEL_DIR)
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import urllib.request
import zipfile

MODEL_NAME = "vosk-model-small-en-us-0.15"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"
APPROX_ZIP_MIB = 40
DEFAULT_ROOT = os.path.expanduser("~/.cache/alwin-vosk")
ROOT_ENV = "ALWIN_WAKE_MODEL_DIR"


def target_dir() -> str:
    override = os.environ.get(ROOT_ENV)
    if override:
        # The env var points at the MODEL directory itself.
        return override
    return os.path.join(DEFAULT_ROOT, MODEL_NAME)


def installed() -> bool:
    path = target_dir()
    # A real model has am/ and graph/ (an interrupted unzip does not).
    return (os.path.isdir(os.path.join(path, "am"))
            and os.path.isdir(os.path.join(path, "graph")))


def print_status() -> int:
    print("Wake-word model (stage-1 keyword gate):")
    print(f"  licence: Apache 2.0 (Alpha Cephei / vosk models)")
    print(f"  path:    {target_dir()}")
    print(f"  size:    ~{APPROX_ZIP_MIB} MiB download, ~68 MiB extracted")
    if installed():
        print("  state:   installed")
    else:
        print("  state:   NOT installed — install it explicitly with:")
        print("           python3 scripts/install_wake_word_model.py")
        try:
            import vosk  # noqa: F401
        except Exception:  # noqa: BLE001
            print("  note:    the 'vosk' package is also missing "
                  "(pip install vosk)")
    try:
        import vosk  # noqa: F401
    except Exception:  # noqa: BLE001
        pass
    return 0 if installed() else 1


def install(assume_yes: bool) -> int:
    if installed():
        print(f"Already installed: {target_dir()}")
        return 0

    root = DEFAULT_ROOT
    free = shutil.disk_usage(
        root if os.path.isdir(root) else os.path.expanduser("~")
    ).free // (1024 * 1024)
    print("About to download the vosk wake-word model:")
    print(f"  source:  {MODEL_URL}")
    print(f"  licence: Apache 2.0")
    print(f"  size:    ~{APPROX_ZIP_MIB} MiB (approximate)")
    print(f"  target:  {target_dir()}")
    print(f"  free:    {free} MiB on the target device")
    if free < APPROX_ZIP_MIB * 2:
        print("Not enough free space — nothing was downloaded.")
        return 1
    print()
    print("The model is used locally only: microphone audio never "
          "leaves this machine, and the app itself never downloads "
          "anything.")
    if not assume_yes:
        try:
            answer = input("Proceed with the download? [y/N] ").strip()
        except EOFError:  # pragma: no cover - non-interactive
            answer = ""
        if answer.lower() not in ("y", "yes"):
            print("Cancelled — nothing was downloaded.")
            return 1

    os.makedirs(root, exist_ok=True)
    zip_path = os.path.join(root, f"{MODEL_NAME}.zip.part")
    try:
        print("Downloading …")
        with urllib.request.urlopen(MODEL_URL, timeout=60) as resp, \
                open(zip_path, "wb") as out:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
        print("Extracting …")
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(root)
    except Exception as exc:  # noqa: BLE001 - honest failure
        print(f"Download failed: {exc}")
        print("Nothing is claimed as installed (a partial file may "
              "remain; re-running this script overwrites it).")
        return 1
    finally:
        try:
            if os.path.isfile(zip_path):
                os.remove(zip_path)
        except OSError:  # pragma: no cover - best effort
            pass

    if not installed():
        print("The archive did not contain the expected model — "
              "nothing was installed.")
        return 1
    print(f"Installed: {target_dir()} (Apache 2.0, local use only).")
    print("Enable it in Settings → Wake word; status: "
          "python3 scripts/install_wake_word_model.py --status")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 scripts/install_wake_word_model.py",
        description=("Explicitly install the local vosk wake-word "
                     "model (Apache 2.0; the app never downloads)."),
    )
    parser.add_argument("--status", action="store_true",
                        help="report install state and exit (no network)")
    parser.add_argument("--yes", action="store_true",
                        help="skip the interactive confirmation")
    args = parser.parse_args(argv)
    if args.status:
        return print_status()
    return install(args.yes)


if __name__ == "__main__":  # pragma: no cover - manual workflow
    sys.exit(main())
