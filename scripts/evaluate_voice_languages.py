#!/usr/bin/env python3
"""Reproducible multilingual voice evaluation for ALWIN FUSION (v0.9.1).

Evaluates each of the 15 supported languages across THREE separate
capabilities, never conflated:

  A. speech recognition  (real human speech samples, WER + CER),
  B. AI response language (live local Ollama, script + detection check),
  C. speech synthesis     (espeak-ng voice availability + synthesis,
                          optional real playback with --playback).

By default it consumes the user-approved FLEURS sample set
(``~/.cache/alwin-fleur-samples``; google/fleurs, **CC-BY-4.0**,
credited below) produced by the sampling step.  With ``--espeak`` it
instead drives SYNTHETIC speech through the same pipeline — clearly
labelled, because espeak audio is out-of-domain for Whisper and must
never be presented as human-speech accuracy.

The tool NEVER submits real tasks (a recording fake manager stands in
for the TaskManager) and NEVER downloads anything: samples must already
exist.  Every stage records its own failure reason; nothing is faked.

Usage:
  python3 scripts/evaluate_voice_languages.py --samples-dir DIR \
      [--languages en,ta,hi] [--model small] [--ollama] [--playback] \
      [--espeak] [--save results.json]

Dataset credit: FLEURS (Few-shot Learning of User Languages) —
Google LLC, CC-BY-4.0 (https://creativecommons.org/licenses/by/4.0/).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import wave

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

DEFAULT_SAMPLES = os.path.expanduser("~/.cache/alwin-fleur-samples")

#: Unicode-script test for "does this text look like language X".
#: Latin is shared by en/fr/es/pt/id, so for those the statistical
#: detector (not the script check) is the deciding evidence.
SCRIPT_RANGES = {
    "en": [(0x0041, 0x024F)], "fr": [(0x0041, 0x024F)],
    "es": [(0x0041, 0x024F)], "pt": [(0x0041, 0x024F)],
    "id": [(0x0041, 0x024F)],
    "ru": [(0x0400, 0x04FF)],
    "ur": [(0x0600, 0x06FF), (0x0750, 0x077F)],
    "ar": [(0x0600, 0x06FF), (0x0750, 0x077F)],
    "zh": [(0x4E00, 0x9FFF)],
    "hi": [(0x0900, 0x097F)],
    "bn": [(0x0980, 0x09FF)],
    "ta": [(0x0B80, 0x0BFF)],
    "te": [(0x0C00, 0x0C7F)],
    "ml": [(0x0D00, 0x0D7F)],
    "kn": [(0x0C80, 0x0CFF)],
}

LATIN_LANGS = {"en", "fr", "es", "pt", "id"}


def script_match(code: str, text: str) -> bool:
    """Whether *text* contains the expected script (Latin langs excluded
    — every Latin language would pass for English)."""
    ranges = SCRIPT_RANGES.get(code, [])
    hits = sum(1 for ch in text
               if any(lo <= ord(ch) <= hi for lo, hi in ranges))
    return hits >= max(2, len(text.replace(" ", "")) // 10)


def levenshtein(a, b) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def normalize_words(text: str):
    cleaned = "".join(ch.lower() if ch.isalnum() else " "
                      for ch in text)
    return [w for w in cleaned.split() if w]


def wer(reference: str, hypothesis: str) -> float:
    ref, hyp = normalize_words(reference), normalize_words(hypothesis)
    if not ref:
        return 1.0
    return levenshtein(ref, hyp) / len(ref)


def cer(reference: str, hypothesis: str) -> float:
    ref = "".join(reference.split())
    hyp = "".join(hypothesis.split())
    if not ref:
        return 1.0
    return levenshtein(list(ref), list(hyp)) / len(ref)


def read_wav_mono16k(path: str):
    """Stdlib wav read -> S16LE 16 kHz mono bytes (resampled if needed)."""
    import numpy as np

    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        width = w.getsampwidth()
        frames = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"unsupported sample width {width}")
    audio = np.frombuffer(frames, dtype=np.int16)
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1).astype(np.int16)
    if rate != 16000:
        n = int(len(audio) * 16000 / rate)
        audio = np.interp(np.linspace(0, len(audio) - 1, n),
                          np.arange(len(audio)), audio).astype(np.int16)
    return audio.tobytes()


def load_meta(samples_dir: str):
    meta_path = pathlib.Path(samples_dir) / "meta.jsonl"
    if not meta_path.is_file():
        raise SystemExit(
            f"No samples at {samples_dir} (meta.jsonl missing). "
            "Produce them first (user-approved FLEURS sampling step) "
            "or pass --samples-dir."
        )
    rows = []
    with open(meta_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 scripts/evaluate_voice_languages.py",
        description="Multilingual A/B/C voice evaluation (offline models, "
                    "no downloads, no real tasks).",
    )
    parser.add_argument("--samples-dir", default=DEFAULT_SAMPLES)
    parser.add_argument("--languages", default="",
                        help="comma list (default: every language with "
                             "samples)")
    parser.add_argument("--model", default="small",
                        help="faster-whisper model size (installed one)")
    parser.add_argument("--ollama", action="store_true",
                        help="also evaluate reply language with the local "
                             "Ollama model (uses OLLAMA_BASE_URL/"
                             "OLLAMA_MODEL, or --ollama-model)")
    parser.add_argument("--ollama-model", default="")
    parser.add_argument("--playback", action="store_true",
                        help="play one synthesized line per language to the "
                             "real audio device (honest sink test)")
    parser.add_argument("--espeak", action="store_true",
                        help="synthetic espeak pipeline mode instead of "
                             "human samples (NOT human-speech accuracy)")
    parser.add_argument("--save", default="",
                        help="write full JSON results here")
    args = parser.parse_args(argv)

    from companion.language_detection import LanguageDetectionService
    from companion.language_registry import LANGUAGES, get
    from companion.speech_recognition import (SpeechRecognitionService,
                                              model_status)
    from companion.conversation import ConversationService
    from companion.voice_output import (CompanionVoice, EspeakSynthesizer,
                                        GStreamerPlayer)

    languages = [s.code for s in LANGUAGES]
    if args.languages:
        languages = [s.strip() for s in args.languages.split(",") if s.strip()]
        unknown = set(languages) - set(languages_all := {
            s.code for s in LANGUAGES})
        if unknown:
            raise SystemExit(f"unknown language codes: {sorted(unknown)} "
                             f"(choose from {sorted(languages_all)})")

    engine_desc = (f"faster-whisper '{args.model}' (cpu, int8, "
                   f"local_files_only)")
    if not model_status(args.model)["installed"]:
        raise SystemExit(
            f"Model '{args.model}' is not installed. Install it with: "
            "python3 scripts/install_voice_models.py "
            f"--model {args.model}"
        )

    print(f"engine: {engine_desc}")
    asr = SpeechRecognitionService(model_size=args.model)
    detector = LanguageDetectionService()

    # Recording fake manager: this tool evaluates conversation quality,
    # it must NEVER create real tasks.
    class RecordingManager:
        def __init__(self):
            self.submissions = []

        def submit(self, command, *, dry_run=False):
            self.submissions.append(command)
            return object()

    llm = None
    if args.ollama:
        from ai_runtime.ollama_client import OllamaClient

        llm = OllamaClient(model=args.ollama_model or None)
        try:
            llm.list_models()
            print(f"ollama: {llm.base_url} model={llm.model or '(configured)'}")
        except Exception as exc:  # noqa: BLE001 - honest degradation
            print(f"ollama: UNAVAILABLE ({exc}) — B evaluation skipped")
            llm = None

    conv = ConversationService(manager=RecordingManager(), llm_client=llm,
                               replies_enabled=llm is not None)
    synth = EspeakSynthesizer()
    voice = CompanionVoice()
    spec_by_code = {s.code: s for s in LANGUAGES}

    results = []
    t_start = time.time()

    def evaluate_sample(code, sample_id, pcm, reference, mode):
        row = {
            "language": code, "sample": sample_id, "mode": mode,
            "engine": engine_desc,
            "reference": reference, "hypothesis": None,
            "wer": None, "cer": None,
            "asr_engine_lang": None, "asr_prob": None,
            "detected": None, "detection_correct": None,
            "ollama_replied": None, "reply_language_ok": None,
            "reply_excerpt": None,
            "tts_voice": spec_by_code[code].espeak if code in spec_by_code
                          else None,
            "tts_synthesized": None, "tts_played": None,
            "failure": None,
        }
        # A. recognition
        try:
            result = asr.recognize(pcm, language=None)
            row["hypothesis"] = result.text
            row["wer"] = round(wer(reference, result.text), 3) \
                if reference else None
            row["cer"] = round(cer(reference, result.text), 3) \
                if reference else None
            row["asr_engine_lang"] = result.asr_hint
            row["asr_prob"] = round(result.language_probability, 2)
            if result.no_speech or not result.text.strip():
                row["failure"] = "asr:no-speech"
        except Exception as exc:  # noqa: BLE001
            row["failure"] = f"asr:{exc}"
            return row
        # detection (on the real transcript)
        det = detector.detect(result.text, asr_hint=result.asr_hint,
                              hint_confidence=result.language_probability)
        row["detected"] = det.code
        row["detection_correct"] = (det.code == code)
        # B. reply language (only when the transcript is usable)
        reply = None
        if llm is not None and result.text.strip() and \
                not row["failure"]:
            reply, reason = conv.reply_with_reason(result.text, det.code)
            row["ollama_replied"] = reply is not None
            if reply:
                row["reply_excerpt"] = reply[:120]
                if code in LATIN_LANGS:
                    d2 = detector.detect(reply)
                    row["reply_language_ok"] = (d2.code == code) or None
                else:
                    row["reply_language_ok"] = script_match(code, reply)
            else:
                row["failure"] = f"ollama:{reason}"
        # C. synthesis (of the reply when present, else the reference)
        speak_text = None
        if spec_by_code.get(code) and synth.available:
            speak_text = (row["reply_excerpt"] or "") if reply \
                else reference
            try:
                speech = synth.synthesize(speak_text[:300],
                                          voice=spec_by_code[code].espeak)
                row["tts_synthesized"] = len(speech.pcm) > 0
                if args.playback and row["tts_synthesized"]:
                    error = GStreamerPlayer(speech).play()
                    row["tts_played"] = error is None
                    if error:
                        row["failure"] = f"playback:{error}"
            except Exception as exc:  # noqa: BLE001
                row["tts_synthesized"] = False
                row["failure"] = f"tts:{exc}"
        else:
            row["tts_synthesized"] = None
            row["failure"] = row["failure"] or "tts:no-engine"
        return row

    if args.espeak:
        samples = {
            "en": "Open my browser.", "ta": "என் உலாவியைத் திற.",
            "hi": "मेरा ब्राउज़र खोलो।", "te": "నా బ్రౌజర్ తెరవండి.",
            "ml": "എന്റെ ബ്രൗസർ തുറക്കൂ.", "kn": "ನನ್ನ ಬ್ರೌಸರ್ ತೆರೆಯಿರಿ.",
            "ur": "میرا براؤزر کھولیں۔", "zh": "打开我的浏览器。",
            "fr": "Ouvre mon navigateur.", "ru": "Открой мой браузер.",
            "es": "Abre mi navegador.", "ar": "افتح المتصفح.",
            "bn": "আমার ব্রাউজার খুলুন।", "pt": "Abra meu navegador.",
            "id": "Buka browser saya.",
        }
        import numpy as np

        for code in languages:
            if code not in samples:
                continue
            speech = synth.synthesize(samples[code],
                                      voice=spec_by_code[code].espeak)
            x = np.frombuffer(speech.pcm, dtype=np.int16).astype(float)
            n = int(len(x) * 16000 / speech.sample_rate)
            pcm = np.interp(np.linspace(0, len(x) - 1, n),
                            np.arange(len(x)), x).astype(np.int16).tobytes()
            results.append(evaluate_sample(code, f"{code}/espeak", pcm,
                                           samples[code], "espeak-synthetic"))
    else:
        rows = load_meta(args.samples_dir)
        by_lang = {}
        for meta in rows:
            by_lang.setdefault(meta["lang"], []).append(meta)
        for code in languages:
            for meta in by_lang.get(code, []):
                pcm = read_wav_mono16k(meta["file"])
                results.append(evaluate_sample(
                    code, meta["id"], pcm, meta["transcript"],
                    "human (google/fleurs, CC-BY-4.0)"))

    # ---- report ------------------------------------------------------- #
    print(f"\n== per-language summary ({len(results)} samples, "
          f"{time.time()-t_start:.0f}s) ==")
    header = (f"{'lang':<4} {'mode':<8} {'n':>2} {'WER':>6} {'CER':>6} "
              f"{'det%':>5} {'llm%':>5} {'langOK%':>7} {'tts':>5}")
    print(header)
    by_lang = {}
    for row in results:
        by_lang.setdefault(row["language"], []).append(row)
    for code, rows_ in by_lang.items():
        wers = [r["wer"] for r in rows_ if r["wer"] is not None]
        cers = [r["cer"] for r in rows_ if r["cer"] is not None]
        dets = [r["detection_correct"] for r in rows_
                if r["detection_correct"] is not None]
        llms = [r["ollama_replied"] for r in rows_
                if r["ollama_replied"] is not None]
        oks = [r["reply_language_ok"] for r in rows_
               if r["reply_language_ok"] is not None]
        tts = "yes" if any(r["tts_synthesized"] for r in rows_) else "no"
        pct = lambda xs: f"{100 * sum(xs) / len(xs):.0f}%" if xs else "-"
        mode = rows_[0]["mode"].split()[0]
        print(f"{code:<4} {mode:<8} {len(rows_):>2} "
              f"{(sum(wers)/len(wers) if wers else 0):>6.2f} "
              f"{(sum(cers)/len(cers) if cers else 0):>6.2f} "
              f"{pct(dets):>5} {pct(llms):>5} {pct(oks):>7} {tts:>5}")
    failures = {}
    for row in results:
        if row["failure"]:
            failures.setdefault(row["failure"].split(":")[0], 0)
            failures[row["failure"].split(":")[0]] += 1
    if failures:
        print(f"failures by stage: {failures}")
    print("\ndataset credit: google/fleurs, CC-BY-4.0" if not args.espeak
          else "\nmode: SYNTHETIC espeak audio — pipeline test only, "
               "NOT human-speech accuracy")

    if args.save:
        with open(args.save, "w", encoding="utf-8") as fh:
            json.dump({"engine": engine_desc,
                       "model": args.model,
                       "mode": "espeak" if args.espeak else "fleurs",
                       "ollama": bool(llm), "playback": args.playback,
                       "results": results}, fh, ensure_ascii=False,
                      indent=2)
        print(f"saved: {args.save}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
