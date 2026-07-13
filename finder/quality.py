"""Quality measurement, scoring, and best-version selection.

Two scoring passes:
  * pre-download  -- on the advertised specs a provider reports (format, sample
    rate, bitrate, source trust, metadata). Trust is weighted heavily here
    because advertised specs lie. Produces the "few matching versions" list.
  * post-download -- on the MEASURED specs (ffprobe) plus the genuine-lossless
    confidence from spectral analysis. A FLAC transcode (low genuineness)
    collapses to roughly its true lossy score, so it can never win over an
    honest version but may still serve as a last resort.
"""

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from . import spectral
from .spectral import SpectralReport
from .providers.base import Candidate

LOSSLESS_CODECS = {
    "flac", "alac", "wav", "aiff", "aif", "ape", "wv", "wavpack",
    "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le",
}


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


@dataclass
class QualityReport:
    path: Path
    codec: str | None = None
    container: str | None = None
    sample_rate: int | None = None
    bit_depth: int | None = None
    bitrate_kbps: int | None = None
    duration_s: float | None = None
    is_lossless_format: bool = False
    spectral: SpectralReport | None = None
    genuine_confidence: float = 1.0     # 1.0 for honest lossy; spectral for lossless
    score: float = 0.0
    rejected: bool = False
    reject_reason: str | None = None
    notes: list = field(default_factory=list)

    def summary(self) -> str:
        fmt = (self.codec or "?").upper()
        if self.is_lossless_format:
            depth = f"/{self.bit_depth}bit" if self.bit_depth else ""
            sr = f" {self.sample_rate/1000:.1f}kHz" if self.sample_rate else ""
            v = self.spectral.verdict if self.spectral else "?"
            return f"{fmt}{depth}{sr} [{v}, conf {self.genuine_confidence:.2f}]"
        br = f" {self.bitrate_kbps}kbps" if self.bitrate_kbps else ""
        return f"{fmt}{br} (lossy)"


# --------------------------------------------------------------------------
# component score helpers
# --------------------------------------------------------------------------

def _fmt_score(codec: str | None, lossless: bool, bitrate: int | None) -> float:
    c = (codec or "").lower()
    if lossless or c in LOSSLESS_CODECS:
        return 1.0
    b = bitrate or 0
    if c == "opus":
        return 0.7 if b >= 128 else (0.55 if b >= 96 else 0.35)
    if c in ("aac", "m4a", "mp4a", "vorbis"):
        return 0.6 if b >= 256 else (0.45 if b >= 192 else (0.3 if b >= 128 else 0.2))
    if c in ("mp3", "wma"):
        return 0.6 if b >= 320 else (0.45 if b >= 192 else (0.3 if b >= 128 else 0.2))
    # unknown lossy
    return 0.4 if b >= 256 else 0.3


def _res_score(sr: int | None, depth: int | None) -> float:
    if not sr:
        return 0.5
    if sr >= 88200:
        base = 1.0
    elif sr >= 48000:
        base = 0.9
    elif sr >= 44100:
        base = 0.8
    elif sr >= 32000:
        base = 0.6
    else:
        base = 0.4
    if depth and depth >= 24:
        base = min(1.0, base + 0.1)
    return base


def _br_score(codec: str | None, lossless: bool, bitrate: int | None) -> float:
    if lossless:
        return 1.0
    b = bitrate or 0
    if not b:
        return 0.4
    return clamp(b / 320.0)


def _meta_score(c: Candidate | None) -> float:
    if not c:
        return 0.5
    return clamp(0.7 * c.metadata_completeness + 0.3 * (1.0 if c.has_artwork else 0.0))


# --------------------------------------------------------------------------
# pre-download scoring + ranking
# --------------------------------------------------------------------------

def score_candidate(c: Candidate) -> float:
    fmt = _fmt_score(c.codec, c.lossless_claimed, c.bitrate_kbps)
    res = _res_score(c.sample_rate, c.bit_depth)
    br = _br_score(c.codec, c.lossless_claimed, c.bitrate_kbps)
    trust = clamp(c.source_trust)
    meta = _meta_score(c)
    return 0.40 * fmt + 0.15 * res + 0.10 * br + 0.25 * trust + 0.10 * meta


def rank_candidates(cands: list[Candidate]) -> list[Candidate]:
    """Best-first. Deterministic tie-break: score -> format -> trust ->
    filesize -> (input/provider order, via stable sort) -> completeness."""
    scored = []
    for c in cands:
        s = score_candidate(c)
        c.extra["prescore"] = round(s, 4)
        scored.append(c)
    return sorted(
        scored,
        key=lambda c: (
            c.extra.get("prescore", 0.0),
            _fmt_score(c.codec, c.lossless_claimed, c.bitrate_kbps),
            clamp(c.source_trust),
            (c.filesize or 0),
            c.metadata_completeness,
        ),
        reverse=True,
    )


# --------------------------------------------------------------------------
# post-download measurement + scoring
# --------------------------------------------------------------------------

def probe_specs(path, ff: dict) -> dict:
    """ffprobe -> codec/container/sample_rate/bit_depth/bitrate/duration."""
    out = {"codec": None, "container": None, "sample_rate": None,
           "bit_depth": None, "bitrate_kbps": None, "duration_s": None}
    fp = ff.get("ffprobe")
    if not fp:
        return out
    try:
        r = subprocess.run(
            [fp, "-v", "error", "-select_streams", "a:0", "-show_entries",
             "stream=codec_name,sample_rate,bits_per_raw_sample,"
             "bits_per_sample,bit_rate:format=duration,bit_rate,format_name",
             "-of", "json", str(path)],
            capture_output=True, text=True)
        data = json.loads(r.stdout or "{}")
        st = (data.get("streams") or [{}])[0]
        fmt = data.get("format") or {}
        out["codec"] = st.get("codec_name")
        out["container"] = fmt.get("format_name")
        if st.get("sample_rate"):
            out["sample_rate"] = int(st["sample_rate"])
        depth = st.get("bits_per_raw_sample") or st.get("bits_per_sample")
        if depth and int(depth) > 0:
            out["bit_depth"] = int(depth)
        dur = fmt.get("duration")
        if dur:
            out["duration_s"] = float(dur)
        # bitrate: prefer stream, then format, then filesize/duration
        br = st.get("bit_rate") or fmt.get("bit_rate")
        if br and int(br) > 0:
            out["bitrate_kbps"] = int(int(br) / 1000)
        elif out["duration_s"]:
            try:
                size = Path(path).stat().st_size
                out["bitrate_kbps"] = int(size * 8 / out["duration_s"] / 1000)
            except OSError:
                pass
    except Exception:
        pass
    return out


def is_fake_lossless(r: QualityReport) -> bool:
    return bool(r.is_lossless_format and r.spectral
               and r.spectral.verdict == "transcode")


def is_genuine_lossless(r: QualityReport) -> bool:
    if not r.is_lossless_format:
        return False
    if r.spectral is None:
        return True                      # lossless format, analysis skipped
    return r.spectral.verdict == "genuine"


def is_acceptable(r: QualityReport, want_lossless: bool = True,
                  reject_threshold: float = None) -> tuple[bool, str | None]:
    """Hard gate. Only fake lossless is rejected outright; anything honest is
    'acceptable' (the orchestrator decides whether it's good ENOUGH to stop)."""
    if is_fake_lossless(r):
        return False, "fake-lossless (lossy transcoded into a lossless container)"
    return True, None


def score_report(r: QualityReport, candidate: Candidate | None = None) -> float:
    lossless = r.is_lossless_format
    codec = r.codec
    bitrate = r.bitrate_kbps
    genuine = r.genuine_confidence
    if is_fake_lossless(r):
        # collapse to the equivalent honest lossy score
        lossless = False
        codec = "mp3"
        bitrate = (r.spectral.est_source_kbps if r.spectral else None) or 256
        genuine = 1.0
    fmt = _fmt_score(codec, lossless, bitrate)
    fmt_eff = fmt * (genuine if lossless else 1.0)
    res = _res_score(r.sample_rate, r.bit_depth)
    br = _br_score(codec, lossless, bitrate)
    trust = clamp(candidate.source_trust) if candidate else 0.5
    meta = _meta_score(candidate)
    return 0.50 * fmt_eff + 0.15 * res + 0.10 * br + 0.15 * trust + 0.10 * meta


def intrinsic_quality(r: QualityReport) -> float:
    """Audio-only quality (format-effective + resolution + bitrate), EXCLUDING
    source trust / metadata completeness. Used for dedup, where we compare the
    stored file against a new one and acquisition attributes don't apply."""
    lossless = r.is_lossless_format
    codec = r.codec
    bitrate = r.bitrate_kbps
    genuine = r.genuine_confidence
    if is_fake_lossless(r):
        lossless = False
        codec = "mp3"
        bitrate = (r.spectral.est_source_kbps if r.spectral else None) or 256
        genuine = 1.0
    fmt = _fmt_score(codec, lossless, bitrate)
    fmt_eff = fmt * (genuine if lossless else 1.0)
    res = _res_score(r.sample_rate, r.bit_depth)
    br = _br_score(codec, lossless, bitrate)
    return 0.65 * fmt_eff + 0.20 * res + 0.15 * br


def measure_quality(path, ff: dict, candidate: Candidate | None = None,
                    want_lossless: bool = True) -> QualityReport:
    """Probe the downloaded file and, for lossless formats, run spectral
    genuineness analysis. Fills in score + rejection state."""
    specs = probe_specs(path, ff)
    r = QualityReport(path=Path(path), **{k: specs[k] for k in specs})
    r.is_lossless_format = bool(
        (r.codec or "").lower() in LOSSLESS_CODECS)
    if r.is_lossless_format:
        r.spectral = spectral.analyze_genuineness(
            path, ff, claimed_lossless=True,
            sample_rate=r.sample_rate, duration_s=r.duration_s)
        # 'unknown' verdicts shouldn't nuke a real lossless file; keep lenient.
        if r.spectral.verdict == "unknown":
            r.genuine_confidence = clamp(r.spectral.genuine_confidence or 0.6, 0.5, 0.7)
        else:
            r.genuine_confidence = r.spectral.genuine_confidence
    else:
        r.genuine_confidence = 1.0       # honest lossy
    ok, reason = is_acceptable(r, want_lossless)
    if not ok:
        r.rejected = True
        r.reject_reason = reason
    r.score = score_report(r, candidate)
    return r
