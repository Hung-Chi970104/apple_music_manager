"""Offline tests for quality scoring, ranking, and fake-lossless rejection."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from finder import quality              # noqa: E402
from finder.spectral import SpectralReport  # noqa: E402
from finder.providers.base import Candidate  # noqa: E402


def _cand(**kw):
    base = dict(provider="x", source_id="1", url="u")
    base.update(kw)
    return Candidate(**base)


def _report(codec, lossless, verdict=None, kbps=None, sr=44100, depth=16,
            bitrate=None, conf=None):
    spec = None
    if lossless and verdict:
        spec = SpectralReport(sample_rate=sr, nyquist_hz=sr / 2,
                              verdict=verdict, est_source_kbps=kbps,
                              genuine_confidence=(conf if conf is not None
                                                  else (0.9 if verdict == "genuine"
                                                        else 0.1)))
    r = quality.QualityReport(
        path=Path(f"x.{codec}"), codec=codec, sample_rate=sr, bit_depth=depth,
        bitrate_kbps=bitrate, is_lossless_format=lossless, spectral=spec)
    r.genuine_confidence = (spec.genuine_confidence if spec else 1.0)
    r.score = quality.score_report(r, None)
    return r


def test_rank_prefers_lossless():
    flac = _cand(codec="flac", lossless_claimed=True, sample_rate=44100,
                 bit_depth=16, source_trust=0.7)
    mp3 = _cand(codec="mp3", lossless_claimed=False, bitrate_kbps=128,
                source_trust=0.3)
    ranked = quality.rank_candidates([mp3, flac])
    assert ranked[0] is flac, [c.provider for c in ranked]


def test_fake_lossless_rejected():
    r = _report("flac", True, verdict="transcode", kbps=128)
    assert quality.is_fake_lossless(r)
    ok, reason = quality.is_acceptable(r)
    assert not ok and "fake" in reason.lower()


def test_genuine_lossless_accepted():
    r = _report("flac", True, verdict="genuine")
    assert quality.is_genuine_lossless(r)
    ok, _ = quality.is_acceptable(r)
    assert ok


def test_fake_lossless_score_collapses():
    genuine = _report("flac", True, verdict="genuine")
    fake = _report("flac", True, verdict="transcode", kbps=128)
    honest_mp3 = _report("mp3", False, bitrate=128)
    # fake FLAC must score far below a genuine FLAC ...
    assert fake.score < genuine.score - 0.2
    # ... and roughly like the honest lossy source it really is
    assert abs(fake.score - honest_mp3.score) < 0.15


def test_genuine_lossless_beats_320_mp3():
    genuine = _report("flac", True, verdict="genuine")
    mp3_320 = _report("mp3", False, bitrate=320)
    assert genuine.score > mp3_320.score


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} quality tests passed.")
