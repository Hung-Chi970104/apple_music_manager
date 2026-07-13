"""Recording identification (MusicBrainz) + downloaded-file verification.

Pre-download: resolve a free-text song query into a specific recording via
MusicBrainz (keyless -- only a contact e-mail in the User-Agent is required).
The chosen RecordingMatch drives provider queries, the expected duration used
for verification, the canonical tags, and the Cover Art Archive lookup.

Post-download: confirm the file we got is actually the intended recording, with
graceful degradation:
  Tier 1  AcoustID/Chromaprint web lookup   (needs pyacoustid + fpcalc + key)
  Tier 2  fingerprint computed, no key       -> fall through to fuzzy
  Tier 3  fuzzy: duration + title/artist     (rapidfuzz, else stdlib difflib)
  Tier 4  duration-only                       (when tags are unreliable)

Everything degrades to Tier 3/4, which is the default here (no key/fpcalc).
"""

import difflib
import shutil
import subprocess
from dataclasses import dataclass, field

from . import config

_mb = None          # musicbrainzngs module (or None)
_mb_ready = False


@dataclass
class RecordingMatch:
    mbid: str | None
    title: str
    artist: str
    album: str | None = None
    length_s: float | None = None
    score: int = 0
    disambiguation: str | None = None
    isrcs: list = field(default_factory=list)
    release_group_mbid: str | None = None

    def query(self) -> str:
        return f"{self.artist} - {self.title}".strip(" -")


@dataclass
class IdentityCheck:
    method: str                 # acoustid | fuzzy | duration-only | skipped
    matched: bool
    confidence: float
    matched_mbids: list = field(default_factory=list)
    detail: str = ""
    fingerprint: str | None = None
    fp_duration: float | None = None


# --------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------

def parse_query(query: str) -> tuple[str | None, str]:
    """'Artist - Title' -> (artist, title); otherwise (None, query)."""
    q = (query or "").strip()
    for sep in (" - ", " – ", " — "):
        if sep in q:
            a, t = q.split(sep, 1)
            return a.strip() or None, t.strip()
    return None, q


def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in (s or "") if ch.isalnum() or ch.isspace()).strip()


def ratio(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    try:
        from rapidfuzz import fuzz
        return fuzz.token_sort_ratio(a, b) / 100.0
    except Exception:
        return difflib.SequenceMatcher(None, a, b).ratio()


# --------------------------------------------------------------------------
# MusicBrainz search
# --------------------------------------------------------------------------

def mb_init(contact_email: str | None = None) -> bool:
    """Lazily configure musicbrainzngs. Returns True if available."""
    global _mb, _mb_ready
    if _mb_ready:
        return _mb is not None
    _mb_ready = True
    try:
        import musicbrainzngs as m
        m.set_useragent(config.APP_NAME, config.APP_VERSION,
                        contact_email or config.CONTACT_EMAIL)
        _mb = m
    except Exception:
        _mb = None
    return _mb is not None


def search_recordings(query: str, artist: str | None = None,
                      limit: int = 8) -> list[RecordingMatch]:
    """Ranked recording candidates for `query`. Degrades to a single
    synthesized match from the raw query if MusicBrainz is unavailable."""
    from .providers.base import throttle
    q_artist, title = parse_query(query)
    artist = artist or q_artist

    if not mb_init():
        return [RecordingMatch(mbid=None, title=title or query,
                               artist=artist or "", score=0)]
    try:
        throttle("musicbrainz", config.MB_MIN_INTERVAL_S)
        kwargs = {"recording": title or query, "limit": limit}
        if artist:
            kwargs["artist"] = artist
        res = _mb.search_recordings(**kwargs)
    except Exception:
        return [RecordingMatch(mbid=None, title=title or query,
                               artist=artist or "", score=0)]

    out: list[RecordingMatch] = []
    for rec in res.get("recording-list", [])[:limit]:
        artists = rec.get("artist-credit") or []
        artist_name = ""
        for ac in artists:
            if isinstance(ac, dict) and ac.get("artist"):
                artist_name += ac["artist"].get("name", "")
            elif isinstance(ac, str):
                artist_name += ac
        rel = (rec.get("release-list") or [{}])[0]
        rg = rel.get("release-group") or {}
        length = rec.get("length")
        out.append(RecordingMatch(
            mbid=rec.get("id"),
            title=rec.get("title", title or query),
            artist=artist_name or (artist or ""),
            album=rel.get("title"),
            length_s=(float(length) / 1000.0) if length else None,
            score=int(rec.get("ext:score", 0) or 0),
            disambiguation=rec.get("disambiguation") or None,
            isrcs=[i for i in (rec.get("isrc-list") or [])],
            release_group_mbid=rg.get("id"),
        ))
    if not out:
        out.append(RecordingMatch(mbid=None, title=title or query,
                                  artist=artist or "", score=0))
    return out


# --------------------------------------------------------------------------
# fingerprinting + verification
# --------------------------------------------------------------------------

def has_fpcalc() -> bool:
    return shutil.which("fpcalc") is not None


def fingerprint_file(path, ff: dict | None = None):
    """(duration, fingerprint_str) via Chromaprint, or (None, None).

    Uses pyacoustid.fingerprint_file if importable and fpcalc is present.
    """
    try:
        import acoustid
    except Exception:
        return None, None
    if not has_fpcalc():
        return None, None
    try:
        dur, fp = acoustid.fingerprint_file(str(path))
        fp = fp.decode() if isinstance(fp, bytes) else fp
        return float(dur), fp
    except Exception:
        return None, None


def _measured_duration(path, ff: dict) -> float | None:
    from . import spectral
    _sr, dur = spectral._probe_sr_dur(path, ff)
    return dur or None


def _read_tags(path) -> dict:
    try:
        import itunes
        return itunes.read_tags(path)
    except Exception:
        return {}


def _duration_ok(measured: float | None, target: float | None) -> bool | None:
    if measured is None or target is None:
        return None
    tol = max(config.VERIFY_DUR_TOL_S, target * config.VERIFY_DUR_TOL_FRAC)
    return abs(measured - target) <= tol


def verify_recording(path, target: RecordingMatch | None, ff: dict,
                     acoustid_key: str | None = None) -> IdentityCheck:
    """Confirm `path` is `target`. Never raises."""
    acoustid_key = acoustid_key or config.ACOUSTID_API_KEY
    measured = _measured_duration(path, ff)
    tags = _read_tags(path)
    got_title = tags.get("title", "")
    got_artist = tags.get("artist", "") or tags.get("albumartist", "")

    fp_dur, fp = fingerprint_file(path, ff)

    # Tier 1: AcoustID web lookup
    if fp and acoustid_key:
        try:
            import acoustid
            matched_mbids, best = [], 0.0
            titles = []
            for score, rid, rtitle, rartist in acoustid.match(
                    acoustid_key, str(path)):
                matched_mbids.append(rid)
                titles.append((rtitle or "", rartist or ""))
                best = max(best, float(score or 0))
                if target and target.mbid and rid == target.mbid and score and score >= 0.5:
                    return IdentityCheck("acoustid", True, float(score),
                                         [rid], "AcoustID MBID match",
                                         fingerprint=fp, fp_duration=fp_dur)
            # no exact MBID (or no target mbid): fall back to title/artist of top hit
            if titles and target:
                tr = max(ratio(t, target.title) for t, _ in titles)
                ar = max(ratio(a, target.artist) for _, a in titles) if target.artist else 1.0
                if tr >= config.VERIFY_FUZZY and ar >= 0.7:
                    return IdentityCheck("acoustid", True, best,
                                         matched_mbids,
                                         "AcoustID title/artist match",
                                         fingerprint=fp, fp_duration=fp_dur)
            if matched_mbids:
                return IdentityCheck("acoustid", False, best, matched_mbids,
                                     "AcoustID returned only other recordings",
                                     fingerprint=fp, fp_duration=fp_dur)
        except Exception:
            pass  # fall through to fuzzy

    # No usable target metadata -> can't reject on identity; accept (duration-only if possible)
    if target is None or (not target.title and target.length_s is None):
        d_ok = _duration_ok(measured, target.length_s if target else None)
        if d_ok is False:
            return IdentityCheck("duration-only", False, 0.3, [],
                                 "duration mismatch", fingerprint=fp, fp_duration=fp_dur)
        return IdentityCheck("skipped", True, 0.5, [],
                             "no target metadata to verify against",
                             fingerprint=fp, fp_duration=fp_dur)

    # Tier 3: fuzzy title/artist + duration
    d_ok = _duration_ok(measured, target.length_s)
    t_ok = ratio(got_title, target.title) if got_title else None
    a_ok = ratio(got_artist, target.artist) if (got_artist and target.artist) else None

    # Strong signal: duration clearly wrong (e.g. 7-min live vs 3-min studio).
    if d_ok is False:
        return IdentityCheck("fuzzy", False, 0.2, [],
                             f"duration {measured:.0f}s vs expected "
                             f"{target.length_s:.0f}s", fingerprint=fp, fp_duration=fp_dur)

    if t_ok is not None:
        title_pass = t_ok >= config.VERIFY_FUZZY
        artist_pass = (a_ok is None) or (a_ok >= 0.7)
        if title_pass and artist_pass:
            conf = 0.5 + 0.5 * min(t_ok, a_ok if a_ok is not None else 1.0)
            method = "fuzzy" if d_ok is None else "fuzzy+duration"
            return IdentityCheck(method, True, conf, [],
                                 "title/artist match", fingerprint=fp, fp_duration=fp_dur)
        # If MB length unknown, only reject on a clear title failure.
        if not title_pass:
            return IdentityCheck("fuzzy", False, t_ok, [],
                                 f"title '{got_title}' != '{target.title}'",
                                 fingerprint=fp, fp_duration=fp_dur)

    # Tier 4: duration-only (tags unreliable/missing)
    if d_ok is True:
        return IdentityCheck("duration-only", True, 0.55, [],
                             "duration matches (no usable tags)",
                             fingerprint=fp, fp_duration=fp_dur)
    return IdentityCheck("duration-only", True, 0.4, [],
                         "no strong signal; accepted",
                         fingerprint=fp, fp_duration=fp_dur)
