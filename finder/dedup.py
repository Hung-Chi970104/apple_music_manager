"""Smart deduplication against the existing library.

Two files are "the same song" when their acoustic fingerprints match, or (no
fingerprint) their title/artist match closely AND their durations agree -- NOT
just when they share a path and byte size (which is all the recovery pipeline
checks). Backed by a lightweight SQLite sidecar catalog (sqlite3 is already a
dependency), indexed incrementally.

Policy is SAFE / never-delete: on a duplicate we skip if the existing copy is
same-or-better; if the new download is genuinely better we add it as a flagged
alternate. Existing library files are never removed unless the caller explicitly
opts in with replace_lower_quality=True (off by default).
"""

import sqlite3
from pathlib import Path

from . import config
from . import quality
from .identify import _norm, ratio

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE,
    size INTEGER, mtime REAL,
    artist TEXT, album TEXT, title TEXT, duration_s REAL,
    codec TEXT, sample_rate INTEGER, bit_depth INTEGER, bitrate_kbps INTEGER,
    is_lossless INTEGER, genuine_confidence REAL,
    fingerprint TEXT, fp_duration REAL,
    norm_key TEXT, added_at REAL
);
CREATE INDEX IF NOT EXISTS idx_tracks_normkey ON tracks(norm_key);
"""


def open_catalog(db_path=None) -> sqlite3.Connection:
    db_path = Path(db_path or config.CATALOG_DB)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def norm_key(artist: str, title: str, duration_s: float | None = None) -> str:
    """Bucket key = normalized artist|title (duration checked separately, so a
    1-second rounding boundary never splits a genuine duplicate)."""
    return f"{_norm(artist)}|{_norm(title)}"


def fp_similarity(fp_a: str | None, fp_b: str | None) -> float:
    """0..1 Chromaprint similarity via bitwise Hamming distance."""
    if not fp_a or not fp_b:
        return 0.0
    if fp_a == fp_b:
        return 1.0
    try:
        from acoustid.chromaprint import decode_fingerprint
        a, _ = decode_fingerprint(fp_a.encode() if isinstance(fp_a, str) else fp_a)
        b, _ = decode_fingerprint(fp_b.encode() if isinstance(fp_b, str) else fp_b)
        n = min(len(a), len(b))
        if n == 0:
            return 0.0
        bits = sum(bin(a[i] ^ b[i]).count("1") for i in range(n))
        return max(0.0, 1.0 - bits / (32.0 * n))
    except Exception:
        return 0.0


def index_file(conn: sqlite3.Connection, path, ff: dict,
               fingerprint: bool = False,
               genuine_confidence: float | None = None) -> None:
    """Insert/refresh one file in the catalog.

    genuine_confidence is stored when the caller already measured it (finder-
    added files); library files indexed in bulk leave it NULL (spectral
    analysis is too costly to run on the whole library), and are then treated
    as trustworthy lossless when scored -- the safe / don't-churn direction.
    """
    import itunes
    from . import identify
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return
    tags = itunes.read_tags(p)
    specs = quality.probe_specs(p, ff)
    is_lossless = (specs.get("codec") or "").lower() in quality.LOSSLESS_CODECS
    fp = fp_dur = None
    if fingerprint:
        fp_dur, fp = identify.fingerprint_file(p, ff)
    artist = tags.get("albumartist") or tags.get("artist") or ""
    title = tags.get("title") or p.stem
    conn.execute(
        """INSERT INTO tracks
           (path,size,mtime,artist,album,title,duration_s,codec,sample_rate,
            bit_depth,bitrate_kbps,is_lossless,genuine_confidence,fingerprint,
            fp_duration,norm_key,added_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, strftime('%s','now'))
           ON CONFLICT(path) DO UPDATE SET
            size=excluded.size, mtime=excluded.mtime, artist=excluded.artist,
            album=excluded.album, title=excluded.title,
            duration_s=excluded.duration_s, codec=excluded.codec,
            sample_rate=excluded.sample_rate, bit_depth=excluded.bit_depth,
            bitrate_kbps=excluded.bitrate_kbps, is_lossless=excluded.is_lossless,
            genuine_confidence=excluded.genuine_confidence,
            fingerprint=excluded.fingerprint, fp_duration=excluded.fp_duration,
            norm_key=excluded.norm_key""",
        (str(p), st.st_size, st.st_mtime, artist, tags.get("album"), title,
         specs.get("duration_s"), specs.get("codec"), specs.get("sample_rate"),
         specs.get("bit_depth"), specs.get("bitrate_kbps"), int(is_lossless),
         genuine_confidence, fp, fp_dur, norm_key(artist, title)))
    conn.commit()


def build_index(conn: sqlite3.Connection, roots=None, ff: dict = None,
                fingerprint: bool = False, progress=None) -> int:
    """Incrementally index audio files under `roots`. Files whose (size,mtime)
    are unchanged are skipped. Returns the number of files (re)indexed."""
    import itunes
    roots = roots if roots is not None else config.LIBRARY_INDEX_ROOTS
    ff = ff or {}
    known = {row["path"]: (row["size"], row["mtime"])
             for row in conn.execute("SELECT path,size,mtime FROM tracks")}
    n = 0
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in itunes.AUDIO_EXTS:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            prev = known.get(str(p))
            if prev and prev[0] == st.st_size and abs(prev[1] - st.st_mtime) < 1:
                continue
            index_file(conn, p, ff, fingerprint=fingerprint)
            n += 1
            if progress and n % 25 == 0:
                progress(f"  indexed {n} files...")
    return n


def _row_to_report(row) -> quality.QualityReport:
    r = quality.QualityReport(
        path=Path(row["path"]), codec=row["codec"],
        sample_rate=row["sample_rate"], bit_depth=row["bit_depth"],
        bitrate_kbps=row["bitrate_kbps"], duration_s=row["duration_s"],
        is_lossless_format=bool(row["is_lossless"]))
    # Unknown confidence (bulk-indexed library file) -> assume genuine. That
    # keeps us from "upgrading" a copy you already have unless we can prove the
    # new one is better, matching the safe / never-delete policy.
    gc = row["genuine_confidence"]
    r.genuine_confidence = gc if gc is not None else 1.0
    r.score = quality.intrinsic_quality(r)
    return r


def find_duplicates(conn: sqlite3.Connection, report: quality.QualityReport,
                    tags: dict, fingerprint: str | None = None) -> list[dict]:
    """Rows in the catalog that are the same recording as the downloaded file."""
    artist = tags.get("albumartist") or tags.get("artist") or ""
    title = tags.get("title") or ""
    key = norm_key(artist, title)
    rows = list(conn.execute(
        "SELECT * FROM tracks WHERE norm_key = ?", (key,)))
    # Also try a looser title match when artist tagging differs.
    if title:
        rows += [r for r in conn.execute(
            "SELECT * FROM tracks WHERE title LIKE ?",
            (f"%{title[:40]}%",)) if r["norm_key"] != key]

    dur = report.duration_s
    matches = []
    seen = set()
    for row in rows:
        if row["path"] in seen:
            continue
        seen.add(row["path"])
        # Tier A: fingerprint
        if fingerprint and row["fingerprint"]:
            sim = fp_similarity(fingerprint, row["fingerprint"])
            if sim >= config.DEDUP_FP_SIM:
                matches.append({"row": row, "reason": f"fingerprint {sim:.2f}",
                                "sim": sim})
                continue
        # Tier B: fuzzy title/artist + duration
        t_ok = ratio(title, row["title"]) if (title and row["title"]) else 0.0
        a_ok = ratio(artist, row["artist"]) if (artist and row["artist"]) else 1.0
        d_ok = True
        if dur and row["duration_s"]:
            d_ok = abs(dur - row["duration_s"]) <= config.DEDUP_DUR_TOL_S
        if t_ok >= config.DEDUP_FUZZY and a_ok >= 0.7 and d_ok:
            matches.append({"row": row, "reason": f"title/artist {t_ok:.2f}",
                            "sim": t_ok})
    return matches


def decide_dedup(new_report: quality.QualityReport, existing_matches: list[dict],
                 replace_lower_quality: bool = None) -> tuple[str, dict | None]:
    """-> (action, best_existing_match).

    action:
      "add"     no duplicate, or new is better but we keep the old (safe mode)
      "skip"    an existing copy is same-or-better
      "replace" new is genuinely better AND replace_lower_quality is on
    """
    if replace_lower_quality is None:
        replace_lower_quality = config.REPLACE_LOWER_QUALITY
    if not existing_matches:
        return "add", None
    # Compare on INTRINSIC audio quality (format/genuineness/resolution/bitrate)
    # so a fresh download's source-trust doesn't make an identical library copy
    # look worse than it is.
    new_score = quality.intrinsic_quality(new_report)
    best = max(existing_matches,
              key=lambda m: _row_to_report(m["row"]).score)
    existing_score = _row_to_report(best["row"]).score
    # small margin so a trivially-higher score doesn't churn the library
    if new_score <= existing_score + 0.03:
        return "skip", best
    # new is genuinely better
    if replace_lower_quality:
        return "replace", best
    return "add", best   # keep both; caller flags the new one as an upgrade
