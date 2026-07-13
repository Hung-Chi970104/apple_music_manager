"""Genuine-vs-fake lossless detection via spectral analysis (numpy only).

Lossy encoders (MP3/AAC/Vorbis) apply a psychoacoustic low-pass that leaves a
brick-wall cliff: energy is roughly full up to a codec/bitrate-dependent
frequency, then falls 40-60+ dB within a few hundred Hz onto a flat, near-silent
shelf. Re-encoding that audio into FLAC/ALAC *preserves the cliff* -- that is
the tell for a fake lossless file. Genuine lossless from a CD/master keeps
energy up near Nyquist with a gradual rolloff and no dead shelf.

The discriminator is the SHARPNESS of the rolloff, not the cutoff frequency
alone (which would wrongly flag genuinely band-limited masters -- old,
classical, or deliberately warm recordings).

The DSP is deliberately split so the decision logic (`classify_spectrum`) is a
pure numpy function testable on synthetic signals with no ffmpeg or files.
"""

import subprocess
from dataclasses import dataclass, field

import numpy as np

from . import config

# Known lossy low-pass cliffs (Hz) -> approximate source bitrate (kbps).
# Used only as a weak corroborating signal / to estimate the source bitrate.
KNOWN_CLIFFS = [
    (15900, 128),
    (16500, 160),
    (19000, 192),
    (19700, 256),
    (20200, 320),
]


@dataclass
class SpectralReport:
    sample_rate: int
    nyquist_hz: float
    cutoff_hz: float | None = None
    cutoff_ratio: float | None = None          # cutoff / nyquist
    rolloff_db_per_khz: float | None = None     # sharpness of the drop at cutoff
    floor_flatness: float | None = None         # 0..1, 1 = dead flat shelf above cutoff
    est_source_kbps: int | None = None
    est_source_codec: str | None = None
    genuine_confidence: float = 0.5             # 0..1
    verdict: str = "unknown"                    # genuine|suspect|transcode|unknown
    fake_hires: bool = False
    analyzed_seconds: float = 0.0
    notes: list = field(default_factory=list)


# --------------------------------------------------------------------------
# pure DSP (testable without ffmpeg)
# --------------------------------------------------------------------------

def _smooth(x: np.ndarray, w: int) -> np.ndarray:
    if w <= 1 or x.size < w:
        return x
    # Edge-pad (NOT zero-pad): in a peak-normalized dB spectrum 0 dB is the
    # loudest possible value, so zero-padding would smear the quiet Nyquist
    # boundary UP to ~-w-fraction dB and fake high-frequency energy there.
    pad = w // 2
    xp = np.pad(x, pad, mode="edge")
    kernel = np.ones(w) / w
    return np.convolve(xp, kernel, mode="valid")[:x.size]


def average_loud_spectrum(samples: np.ndarray, sr: int, frame: int = 8192,
                          hop: int = 4096, top_fraction: float = 0.25):
    """Average power spectrum of the loudest `top_fraction` of Hann-windowed
    frames. Returns (freqs, psd_db) with psd_db peak-normalized to 0 dB.

    Restricting to loud frames defeats quiet passages / rests / ambient
    sections that would otherwise fake a low cutoff.
    """
    samples = np.asarray(samples, dtype=np.float64).ravel()
    if samples.size == 0:
        return np.array([]), np.array([])
    if samples.size < frame:
        frame = samples.size
        hop = max(1, frame // 2)
    win = np.hanning(frame)
    n_frames = 1 + (samples.size - frame) // hop
    if n_frames < 1:
        n_frames = 1
    idx = np.arange(frame)[None, :] + hop * np.arange(n_frames)[:, None]
    frames = samples[idx] * win
    spec = np.abs(np.fft.rfft(frames, axis=1)) ** 2      # (n_frames, bins)
    energy = spec.sum(axis=1)
    k = max(1, int(round(n_frames * top_fraction)))
    top = np.argsort(energy)[-k:]
    psd = spec[top].mean(axis=0)
    freqs = np.fft.rfftfreq(frame, 1.0 / sr)
    peak = float(psd.max())
    if peak <= 0:
        psd_db = np.full_like(psd, -120.0)
    else:
        psd_db = 10.0 * np.log10(np.maximum(psd, peak * 1e-12) / peak)
    return freqs, psd_db


def estimate_cutoff(freqs: np.ndarray, psd_db: np.ndarray, sr: int,
                    ref_lo: float = 2000.0, ref_hi: float = 4000.0,
                    drop_db: float = 60.0):
    """-> (cutoff_hz, rolloff_db_per_khz, floor_flatness).

    cutoff       : highest frequency whose (smoothed) level is within `drop_db`
                   of the passband reference (median level in 2-4 kHz).
    rolloff      : dB drop across the 1 kHz window straddling the cutoff
                   (>= ~50 dB/kHz is a brick wall).
    floor_flatness: how flat-and-dead the shelf above the cutoff is (0..1);
                   transcodes leave a very flat, very low shelf (~1.0).
    """
    if freqs.size == 0:
        return None, None, None
    nyq = sr / 2.0
    smooth = _smooth(psd_db, 5)
    band = (freqs >= ref_lo) & (freqs <= min(ref_hi, nyq))
    if not band.any():
        band = freqs <= min(ref_hi, nyq)
    ref = float(np.median(smooth[band])) if band.any() else 0.0
    thresh = ref - drop_db
    above = np.where(smooth > thresh)[0]
    cutoff = float(freqs[above[-1]]) if above.size else 0.0

    lo = (freqs >= cutoff - 1000.0) & (freqs < cutoff)
    hi = (freqs >= cutoff) & (freqs < cutoff + 1000.0)
    lo_level = float(np.median(smooth[lo])) if lo.any() else ref
    hi_level = float(np.median(smooth[hi])) if hi.any() else lo_level
    rolloff = max(0.0, lo_level - hi_level)

    shelf = (freqs > cutoff + 500.0) & (freqs <= nyq * 0.98)
    if int(shelf.sum()) > 4:
        std = float(np.std(smooth[shelf]))
        flatness = float(np.clip(1.0 - std / 20.0, 0.0, 1.0))
    else:
        flatness = 0.0  # cutoff near Nyquist -> no dead shelf
    return cutoff, rolloff, flatness


def _est_source(cutoff: float | None, sr: int):
    """(kbps, codec) estimate from the cutoff, or (None, None) if lossless."""
    if cutoff is None:
        return None, None
    nyq = sr / 2.0
    if cutoff >= nyq * 0.95:
        return None, None  # energy to Nyquist -> genuinely lossless
    for f, k in KNOWN_CLIFFS:
        if cutoff <= f + 300:
            return k, ("mp3/aac" if k >= 128 else "mp3")
    return 320, "mp3/aac"


def _near_known_cliff(cutoff: float | None, tol: float = 400.0) -> bool:
    if cutoff is None:
        return False
    return any(abs(cutoff - f) <= tol for f, _ in KNOWN_CLIFFS)


def classify_spectrum(freqs: np.ndarray, psd_db: np.ndarray, sr: int,
                      analyzed_seconds: float, claimed_lossless: bool
                      ) -> SpectralReport:
    """Pure decision function: spectrum -> SpectralReport. No I/O."""
    nyq = sr / 2.0
    rep = SpectralReport(sample_rate=sr, nyquist_hz=nyq,
                         analyzed_seconds=analyzed_seconds)
    if freqs.size == 0 or analyzed_seconds < 1.0:
        rep.verdict = "unknown"
        rep.notes.append("no analyzable audio")
        return rep

    cutoff, rolloff, flatness = estimate_cutoff(freqs, psd_db, sr)
    ratio = (cutoff / nyq) if (cutoff is not None and nyq > 0) else None
    rep.cutoff_hz = cutoff
    rep.cutoff_ratio = ratio
    rep.rolloff_db_per_khz = rolloff
    rep.floor_flatness = flatness

    kbps, codec = _est_source(cutoff, sr)
    rep.est_source_kbps = kbps
    rep.est_source_codec = codec

    # Component scores (each 0..1; higher = more genuine).
    cutoff_score = float(ratio) if ratio is not None else 0.5
    brickwall = float(np.clip((rolloff or 0.0) / 50.0, 0.0, 1.0))  # 1 = sharp cliff (bad)
    flat = float(flatness or 0.0)                                   # 1 = dead shelf (bad)
    penalty = 0.15 if _near_known_cliff(cutoff) else 0.0

    genuine = (0.5 * cutoff_score
               + 0.3 * (1.0 - brickwall)
               + 0.2 * (1.0 - flat)
               - penalty)
    genuine = float(np.clip(genuine, 0.0, 1.0))

    # Short-circuits.
    if ratio is not None and ratio >= 0.90:
        genuine = max(genuine, 0.90)          # energy up to Nyquist
    if (rolloff or 0.0) < 30.0:
        genuine = max(genuine, 0.66)          # gradual rolloff = band-limited master, not a transcode
    if (ratio is not None and ratio <= 0.86
            and (rolloff or 0.0) > 40.0 and flat > 0.7):
        genuine = min(genuine, 0.20)          # unmistakable brick wall + dead shelf
        rep.notes.append("brick-wall low-pass with dead HF shelf")

    rep.genuine_confidence = genuine
    if analyzed_seconds < 8.0:
        rep.verdict = "unknown"
        rep.genuine_confidence = min(max(genuine, 0.4), 0.6)
        rep.notes.append(f"only {analyzed_seconds:.0f}s analyzed -- low certainty")
        return rep

    if genuine >= config.SPECTRAL_GENUINE:
        rep.verdict = "genuine"
    elif genuine < config.SPECTRAL_TRANSCODE:
        rep.verdict = "transcode"
    else:
        rep.verdict = "suspect"

    # Fake hi-res: a >48kHz file whose energy dies well below its own Nyquist.
    if sr > 48000 and cutoff is not None and cutoff < 22000.0:
        rep.fake_hires = True
        rep.notes.append("energy dies below 22 kHz -- likely upsampled hi-res")

    if claimed_lossless and rep.verdict == "transcode" and kbps:
        rep.notes.append(f"looks like a ~{kbps} kbps lossy source re-wrapped as lossless")
    return rep


# --------------------------------------------------------------------------
# ffmpeg decode + orchestration (needs ffmpeg; used in real runs)
# --------------------------------------------------------------------------

def _probe_sr_dur(path, ff: dict) -> tuple[int, float]:
    """Native sample rate + duration via ffprobe (best-effort)."""
    fp = ff.get("ffprobe")
    sr, dur = 44100, 0.0
    if not fp:
        return sr, dur
    try:
        r = subprocess.run(
            [fp, "-v", "error", "-select_streams", "a:0", "-show_entries",
             "stream=sample_rate:format=duration", "-of",
             "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True)
        vals = [v for v in r.stdout.split() if v]
        if vals:
            sr = int(float(vals[0]))
        if len(vals) > 1:
            dur = float(vals[1])
    except Exception:
        pass
    return sr, dur


def decode_pcm_mono(path, ff: dict, start_s: float, dur_s: float,
                    sr: int) -> np.ndarray:
    """Decode a slice to mono float32 at the file's NATIVE sample rate.

    Never resample (`-ar`) -- that would destroy the very cutoff we measure.
    """
    cmd = [ff["ffmpeg"], "-v", "error", "-ss", f"{start_s:.3f}",
           "-t", f"{dur_s:.3f}", "-i", str(path), "-map", "0:a:0",
           "-ac", "1", "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or not r.stdout:
        return np.array([], dtype=np.float32)
    return np.frombuffer(r.stdout, dtype=np.float32)


def analyze_genuineness(path, ff: dict, claimed_lossless: bool,
                        sample_rate: int | None = None,
                        duration_s: float | None = None,
                        windows: int = None, window_s: float = None
                        ) -> SpectralReport:
    """Decode a few loud windows from the middle of `path` and classify.

    Runs only on the downloaded winner (a full decode + FFT), never on every
    candidate. Robust to short/undecodable files: returns verdict "unknown"
    rather than raising.
    """
    windows = windows or config.SPECTRAL_WINDOWS
    window_s = window_s or config.SPECTRAL_WINDOW_S
    try:
        if sample_rate is None or duration_s is None:
            sr, dur = _probe_sr_dur(path, ff)
            sample_rate = sample_rate or sr
            duration_s = duration_s if duration_s is not None else dur
        sr = int(sample_rate or 44100)
        dur = float(duration_s or 0.0)

        chunks = []
        if dur <= 0 or dur < 8.0:
            # short/unknown: just grab up to `window_s * windows` from the start
            chunks.append(decode_pcm_mono(path, ff, 0.0, window_s * windows, sr))
        else:
            usable_lo, usable_hi = 0.10 * dur, 0.90 * dur
            span = usable_hi - usable_lo
            n = max(1, windows)
            for i in range(n):
                # spread window starts across the middle 80%
                start = usable_lo + span * (i / max(1, n)) if n > 1 else usable_lo
                take = min(window_s, max(1.0, usable_hi - start))
                if take <= 0:
                    continue
                chunks.append(decode_pcm_mono(path, ff, start, take, sr))
        samples = np.concatenate(chunks) if chunks else np.array([], np.float32)
        analyzed = samples.size / sr if sr else 0.0
        if samples.size == 0:
            rep = SpectralReport(sample_rate=sr, nyquist_hz=sr / 2.0)
            rep.verdict = "unknown"
            rep.notes.append("could not decode audio for analysis")
            return rep
        freqs, psd_db = average_loud_spectrum(samples, sr)
        return classify_spectrum(freqs, psd_db, sr, analyzed, claimed_lossless)
    except Exception as exc:
        rep = SpectralReport(sample_rate=int(sample_rate or 44100),
                             nyquist_hz=int(sample_rate or 44100) / 2.0)
        rep.verdict = "unknown"
        rep.notes.append(f"spectral analysis failed: {type(exc).__name__}: {exc}")
        return rep
