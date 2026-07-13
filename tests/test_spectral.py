"""Offline tests for the genuine-vs-fake-lossless detector (no ffmpeg/network).

Synthesize signals with known spectral shapes and assert the verdict. Runnable
either with pytest (`python -m pytest tests/`) or directly
(`python tests/test_spectral.py`).
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from finder import spectral  # noqa: E402

SR = 44100
DUR = 30.0
N = int(SR * DUR)


def _classify(samples):
    f, p = spectral.average_loud_spectrum(samples.astype(np.float32), SR)
    return spectral.classify_spectrum(f, p, SR, DUR, claimed_lossless=True)


def _white():
    rng = np.random.default_rng(0)
    return rng.standard_normal(N) * 0.1


def _brickwall(cut_hz):
    """White noise hard low-passed at cut_hz -- simulates a lossy transcode."""
    x = _white()
    X = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(N, 1 / SR)
    X[freqs > cut_hz] = 0.0
    return np.fft.irfft(X, N)


def test_genuine_full_band():
    rep = _classify(_white())
    assert rep.verdict == "genuine", rep
    assert rep.genuine_confidence >= 0.66
    assert rep.cutoff_ratio and rep.cutoff_ratio >= 0.95


def test_transcode_128k_brickwall():
    rep = _classify(_brickwall(16000))
    assert rep.verdict == "transcode", rep
    assert rep.genuine_confidence < 0.35
    assert rep.rolloff_db_per_khz and rep.rolloff_db_per_khz > 40
    assert rep.est_source_kbps in (128, 160)


def test_transcode_192k_brickwall():
    rep = _classify(_brickwall(19000))
    assert rep.verdict in ("transcode", "suspect"), rep
    assert rep.genuine_confidence < 0.66


def test_bandlimited_master_not_flagged():
    """A gentle (non-brick-wall) rolloff is a genuine band-limited master,
    NOT a transcode."""
    x = _white()
    X = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(N, 1 / SR)
    gain = 1.0 / (1.0 + (freqs / 8000.0) ** 2)   # ~ -6 dB/oct from 8 kHz
    band = np.fft.irfft(X * gain, N)
    rep = _classify(band)
    assert rep.verdict != "transcode", rep


def test_short_track_unknown():
    rng = np.random.default_rng(1)
    short = (rng.standard_normal(int(SR * 3)) * 0.1).astype(np.float32)
    f, p = spectral.average_loud_spectrum(short, SR)
    rep = spectral.classify_spectrum(f, p, SR, 3.0, claimed_lossless=True)
    assert rep.verdict == "unknown", rep


def test_empty_unknown():
    rep = spectral.classify_spectrum(np.array([]), np.array([]), SR, 0.0, True)
    assert rep.verdict == "unknown"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} spectral tests passed.")
