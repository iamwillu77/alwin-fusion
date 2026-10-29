#!/usr/bin/env python3
"""Fetch a small FLEURS human-speech evaluation set (CC-BY-4.0).

google/fleurs — Google LLC, licensed CC-BY-4.0
(https://creativecommons.org/licenses/by/4.0/). Samples and reference
transcripts are kept verbatim for evaluation only; nothing is uploaded
or redistributed.

Strategy (the evaluation fixtures used for v0.9.1, ~65 MB total for
all 15 languages):

1. **datasets-server /rows** — 10 validation samples per language as
   small JSON requests (~1-3 MB each), audio decoded to 16 kHz mono
   wav with PyAV. This is the preferred, budget-friendly path.
2. **Range-streamed dev.tar.gz fallback** — for configs the rows API
   errors on (observed: ml, bn, ur, zh, es), stream only the FIRST
   ~8-16 MB of the original ``data/<lang>/audio/dev.tar.gz`` (tar
   members are ordered) and pair extracted wavs with ``dev.tsv``
   transcripts. The repo-native folder names differ from the datasets
   config names (zh -> cmn_hans_cn, ur -> ur_pk, es -> es_419).

Interruption-safe (meta flushed + fsynced per record) and resumable
(complete languages are skipped). No large dataset is ever downloaded
in full; per-language budgets are hard-capped here.

Usage:  python3 scripts/fetch_fleurs_samples.py [--out DIR]
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import tarfile
import time
import urllib.request

DEFAULT_OUT = os.path.expanduser("~/.cache/alwin-fleur-samples")
API = "https://datasets-server.huggingface.co/rows"
BASE = "https://huggingface.co/datasets/google/fleurs/resolve/main/data"

#: our ISO code -> datasets config name (rows API)
CONFIGS = {
    "en": "en_us", "ta": "ta_in", "hi": "hi_in", "te": "te_in",
    "ml": "ml_in", "kn": "kn_in", "ur": "ur_in", "zh": "zh_cn",
    "fr": "fr_fr", "ru": "ru_ru", "es": "es_es", "ar": "ar_eg",
    "bn": "bn_in", "pt": "pt_br", "id": "id_id",
}

#: our ISO code -> repo-native folder (tar fallback, for rows failures)
FOLDERS = {
    "ml": "ml_in", "bn": "bn_in", "ur": "ur_pk",
    "zh": "cmn_hans_cn", "es": "es_419",
}

PER_LANG = 10
RETRIES = 5
TAR_BUDGET = 16 * 1024 * 1024   # hard cap per language's tar stream


def http_json(url, timeout=60):
    last = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"request failed after {RETRIES} tries: {last}")


def http_bytes(url, timeout=60):
    last = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"request failed after {RETRIES} tries: {last}")


def to_wav16k(raw: bytes) -> bytes:
    """Any container -> S16LE 16 kHz mono wav bytes (via bundled ffmpeg)."""
    import av
    import numpy as np
    from av.audio.resampler import AudioResampler

    resampler = AudioResampler(format="s16", layout="mono", rate=16000)
    with av.open(io.BytesIO(raw)) as container:
        audio = container.streams.audio[0]
        chunks = []

        def _collect(frame):
            for out in resampler.resample(frame):   # always a list
                if out is not None:
                    chunks.append(out.to_ndarray().reshape(-1))

        for frame in container.decode(audio):
            _collect(frame)
        try:
            _collect(None)                          # flush
        except Exception:  # noqa: BLE001 - flush optional
            pass
        if not chunks:
            raise ValueError("no audio frames decoded")
        mono = np.concatenate(chunks)
    pcm = np.asarray(mono, dtype=np.int16)
    import wave as wavemod

    buf = io.BytesIO()
    with wavemod.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


class BudgetReader(io.RawIOBase):
    """Read at most *cap* bytes from a streaming HTTP response."""

    def __init__(self, resp, cap):
        super().__init__()
        self._resp = resp
        self.left = cap
        self.used = 0

    def readable(self):
        return True

    def read(self, n=-1):
        if self.left <= 0:
            return b""
        if n is None or n < 0 or n > self.left:
            n = self.left
        data = self._resp.read(n)
        self.left -= len(data)
        self.used += len(data)
        return data


def write_sample(meta, code, index, folder, wav, text, source):
    lang_dir = os.path.join(os.path.dirname(meta.name), code)
    os.makedirs(lang_dir, exist_ok=True)
    fname = os.path.join(lang_dir, f"{index:02d}.wav")
    with open(fname, "wb") as f:
        f.write(wav)
    meta.write(json.dumps({
        "id": f"{code}/{index:02d}", "lang": code,
        "fleurs_config": folder, "file": fname,
        "transcript": text, "bytes": len(wav),
        "dataset": "google/fleurs (CC-BY-4.0)", "source": source,
    }, ensure_ascii=False) + "\n")
    meta.flush()
    os.fsync(meta.fileno())


def fetch_rows(meta, code, config):
    """Preferred path: datasets-server /rows (small JSON requests)."""
    url = (f"{API}?dataset=google%2Ffleurs&config={config}"
           f"&split=validation&offset=0&length={PER_LANG * 3}")
    payload = http_json(url)
    kept = 0
    for row in payload.get("rows", []):
        if kept >= PER_LANG:
            break
        r = row.get("row", {})
        audio = r.get("audio") or {}
        if isinstance(audio, list):
            entries = [e for e in audio if isinstance(e, dict)]
            audio = entries[0] if entries else {}
        text = (r.get("transcription") or "").strip()
        if not text:
            continue
        try:
            if isinstance(audio.get("bytes"), str):
                raw = base64.b64decode(audio["bytes"])
            elif audio.get("src"):
                src = audio["src"]
                if src.startswith("/"):
                    src = "https://datasets-server.huggingface.co" + src
                raw = http_bytes(src)
            else:
                continue
            wav = to_wav16k(raw)
        except Exception as exc:  # noqa: BLE001
            print(f"  {code}: row {row.get('row_idx')} failed: {exc}",
                  flush=True)
            continue
        if len(wav) < 40000:
            continue
        write_sample(meta, code, kept, config, wav, text, "rows-api")
        kept += 1
    return kept


def fetch_tar(meta, code, folder):
    """Fallback: first MBs of dev.tar.gz + dev.tsv transcripts."""
    raw = http_bytes(f"{BASE}/{folder}/dev.tsv")
    transcripts = {}
    for line in raw.decode("utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[1].endswith(".wav"):
            transcripts[parts[1]] = parts[2].strip()
    url = f"{BASE}/{folder}/audio/dev.tar.gz"
    req = urllib.request.Request(url, headers={"Range": "bytes=0-8388607"})
    resp = urllib.request.urlopen(req, timeout=90)
    budget = BudgetReader(resp, TAR_BUDGET)
    kept = 0
    try:
        tar = tarfile.open(fileobj=budget, mode="r|gz")
        try:
            for member in tar:
                if kept >= PER_LANG:
                    break
                if not member.isfile() or not member.name.endswith(".wav"):
                    continue
                base = os.path.basename(member.name)
                text = transcripts.get(base)
                if not text:
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                wav = to_wav16k(extracted.read())
                if len(wav) < 40000:
                    continue
                write_sample(meta, code, kept, folder, wav, text,
                             "dev.tar.gz range stream")
                kept += 1
        except tarfile.ReadError:
            pass  # budget exhausted — keep what we have
    finally:
        resp.close()
    return kept, budget.used


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 scripts/fetch_fleurs_samples.py")
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    out = args.out
    os.makedirs(out, exist_ok=True)
    meta_path = os.path.join(out, "meta.jsonl")
    existing = set()
    if os.path.isfile(meta_path):
        with open(meta_path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    try:
                        existing.add(json.loads(line)["lang"])
                    except Exception:  # noqa: BLE001
                        pass

    t0 = time.time()
    with open(meta_path, "a", encoding="utf-8") as meta:
        for code, config in CONFIGS.items():
            if code in existing:
                print(f"{code}: already present, skip", flush=True)
                continue
            kept = 0
            try:
                kept = fetch_rows(meta, code, config)
            except Exception as exc:  # noqa: BLE001
                print(f"{code}/{config}: rows failed ({exc}); falling "
                      "back to dev.tar.gz range stream", flush=True)
            if kept == 0 and code in FOLDERS:
                try:
                    kept, used = fetch_tar(meta, code, FOLDERS[code])
                    print(f"{code}/{FOLDERS[code]}: {kept} samples from "
                          f"{used / 1e6:.1f} MB streamed", flush=True)
                except Exception as exc:  # noqa: BLE001
                    print(f"{code}: tar fallback failed: {exc}", flush=True)
            elif kept:
                print(f"{code}/{config}: {kept} samples", flush=True)
    print(f"DONE in {time.time() - t0:.0f}s -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
