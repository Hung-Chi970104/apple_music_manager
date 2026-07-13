"""Offline tests for the dedup decision logic (safe / never-delete default).

Decisions compare INTRINSIC audio quality, so reports are built from real
audio attributes (codec/lossless/bitrate/sr), not raw score numbers.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from finder import dedup, quality  # noqa: E402


def _new(codec, lossless, bitrate=None, sr=44100, depth=16, conf=1.0):
    r = quality.QualityReport(path=Path("new." + codec), codec=codec,
                              sample_rate=sr, bit_depth=depth,
                              bitrate_kbps=bitrate, is_lossless_format=lossless)
    r.genuine_confidence = conf
    r.score = quality.intrinsic_quality(r)
    return r


def _match(codec, lossless, bitrate=None, sr=44100, depth=16, conf=1.0):
    row = {"path": "/lib/old." + codec, "codec": codec, "sample_rate": sr,
           "bit_depth": depth, "bitrate_kbps": bitrate, "duration_s": 200.0,
           "is_lossless": int(lossless),
           "genuine_confidence": conf, "title": "t", "artist": "a"}
    return [{"row": row, "reason": "test", "sim": 1.0}]


def test_no_match_adds():
    action, existing = dedup.decide_dedup(_new("flac", True), [])
    assert action == "add" and existing is None


def test_identical_copy_skips():
    # same intrinsic quality already in library -> skip (no churn)
    action, _ = dedup.decide_dedup(_new("alac", True), _match("flac", True))
    assert action == "skip"


def test_existing_better_skips():
    # existing genuine FLAC vs new 128k mp3 -> skip
    action, _ = dedup.decide_dedup(_new("mp3", False, bitrate=128),
                                   _match("flac", True))
    assert action == "skip"


def test_new_better_adds_when_safe():
    # existing 128k mp3, new lossless, safe mode -> add (keep both)
    action, existing = dedup.decide_dedup(
        _new("flac", True), _match("mp3", False, bitrate=128),
        replace_lower_quality=False)
    assert action == "add" and existing is not None


def test_new_better_replaces_when_opted_in():
    action, _ = dedup.decide_dedup(
        _new("flac", True), _match("mp3", False, bitrate=128),
        replace_lower_quality=True)
    assert action == "replace"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} dedup tests passed.")
