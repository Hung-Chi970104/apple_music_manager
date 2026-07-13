"""Orchestrator -- the one-button pipeline.

run_finder(query) is the single entry point used by both the GUI "Find" tab and
the CLI. It:

  1. identifies the recording (MusicBrainz);
  2. fans out across the enabled source providers;
  3. ranks the versions on advertised specs;
  4. downloads best-first, verifying each (right recording? real quality?),
     rejecting fake lossless and wrong recordings and falling back;
  5. dedups against the library (safe / never-delete);
  6. fixes metadata + artwork;
  7. names/organizes the file and adds it to Music.app.

GUI vs CLI differ only in the optional choose_recording / choose_candidates
callbacks; with neither, it auto-picks the top recording and the ranked order.
"""

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import artwork, config, dedup, identify, quality, tagging
from . import providers

# Formats Music.app ingests as-is; others get converted before import.
NATIVE_EXTS = {".mp3", ".m4a", ".m4b", ".wav", ".aif", ".aiff"}
TO_ALAC_EXTS = {".flac", ".ape", ".wv", ".alac"}

_FF: dict = {}


@dataclass
class FinderOptions:
    providers: list | None = None
    max_downloads: int | None = None
    want_lossless: bool = True
    lossless_only: bool = False           # fail rather than accept a lossy fallback
    replace_lower_quality: bool | None = None
    dest: Path | None = None
    add_to_library: bool = True
    fingerprint_index: bool = False       # fingerprint files while indexing (needs fpcalc)


@dataclass
class FinderResult:
    status: str = "failed"                 # added|replaced|skipped|failed|cancelled
    recording: object | None = None
    candidate: object | None = None
    report: object | None = None
    final_path: Path | None = None
    action: str | None = None
    tried: list = field(default_factory=list)   # [(candidate_label, reason)]
    message: str = ""


def _log(log, msg):
    (log or print)(msg)


def _cancelled(cancel) -> bool:
    return cancel is not None and getattr(cancel, "is_set", lambda: False)()


def _safe_unlink(path):
    try:
        Path(path).unlink()
    except Exception:
        pass


def get_ffmpeg(log=print) -> dict:
    """Resolve ffmpeg/ffprobe (PATH, else fetch static build). Mirrors
    music_gui.get_ffmpeg so the finder works headless without importing tkinter."""
    if _FF:
        return _FF
    ff, fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ff:
        _log(log, "Fetching a bundled ffmpeg (first time only, ~50 MB)...")
        from static_ffmpeg import run
        ff, fp = run.get_or_fetch_platform_executables_else_raise()
    _FF.update(ffmpeg=str(ff), ffprobe=str(fp) if fp else None)
    return _FF


# --------------------------------------------------------------------------
# format compatibility (convert non-native downloads for Music.app)
# --------------------------------------------------------------------------

def _ensure_compatible(path, ff: dict, log) -> Path | None:
    path = Path(path)
    ext = path.suffix.lower()
    if ext in NATIVE_EXTS:
        return path
    out = path.with_suffix(".m4a")
    i = 2
    while out.exists() and out != path:
        out = path.with_name(f"{path.stem} ({i}).m4a")
        i += 1
    if ext in TO_ALAC_EXTS:
        codec = ["-map", "0:a:0", "-c:a", "alac"]      # lossless -> ALAC (no loss)
    else:
        codec = ["-map", "0:a:0", "-c:a", "aac", "-b:a", "256k"]  # lossy -> AAC
    cmd = ([ff["ffmpeg"], "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(path)] + codec + ["-map_metadata", "0", str(out)])
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        _log(log, f"  format conversion failed: {r.stderr.strip()[:200]}")
        return None
    _safe_unlink(path)
    return out


# --------------------------------------------------------------------------
# manifest ledger (separate from the recovery pipeline's _manifest.json)
# --------------------------------------------------------------------------

def _load_manifest() -> dict:
    try:
        return json.loads(config.FINDER_MANIFEST.read_text())
    except (OSError, ValueError):
        return {}


def _record_manifest(rec, cand, final_path, report):
    man = _load_manifest()
    key = (rec.mbid if getattr(rec, "mbid", None)
           else f"{rec.artist}|{rec.title}".lower())
    man[key] = {
        "mbid": getattr(rec, "mbid", None),
        "artist": rec.artist, "title": rec.title,
        "path": str(final_path), "provider": cand.provider,
        "score": round(report.score, 3), "added_at": int(time.time()),
    }
    config.FINDER_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    config.FINDER_MANIFEST.write_text(json.dumps(man, indent=1, ensure_ascii=False))


# --------------------------------------------------------------------------
# search (steps 1-3) -- also used standalone by the GUI to populate the list
# --------------------------------------------------------------------------

def search_versions(rec, query, opts, log, cancel=None) -> list:
    """Fan out across providers and return ranked candidates for `rec`."""
    provs = providers.enabled_providers(
        opts.providers, log=lambda m: _log(log, m))
    cands = []
    for p in provs:
        if _cancelled(cancel):
            break
        try:
            found = p.search(rec, rec.query() if rec else query,
                             config.CANDIDATES_PER_PROVIDER) or []
        except Exception as exc:
            _log(log, f"  {p.name}: search error ({type(exc).__name__}: {exc})")
            found = []
        if found:
            _log(log, f"  {p.name}: {len(found)} version(s)")
        cands.extend(found)
    return quality.rank_candidates(cands)


# --------------------------------------------------------------------------
# the pipeline
# --------------------------------------------------------------------------

def run_finder(query, opts: FinderOptions = None, log=print, cancel=None,
               choose_recording=None, choose_candidates=None) -> FinderResult:
    opts = opts or FinderOptions()
    max_dl = opts.max_downloads or config.MAX_DOWNLOADS
    dest = Path(opts.dest or config.FINDER_DEST)
    staging = config.FINDER_STAGING
    result = FinderResult()

    ff = get_ffmpeg(log)

    # 1. identify the recording
    _log(log, f"Identifying: {query}")
    recs = identify.search_recordings(query)
    if not recs:
        result.message = "no recording match found"
        return result
    rec = recs[0]
    if choose_recording:
        picked = choose_recording(recs)
        if picked is None:
            result.status = "cancelled"
            result.message = "cancelled at recording selection"
            return result
        rec = picked
    result.recording = rec
    _log(log, f"  -> {rec.artist} - {rec.title}"
         + (f"  [{rec.mbid}]" if rec.mbid else "  (unverified query)"))

    # 2-3. fan out + rank
    _log(log, "Searching sources...")
    ranked = search_versions(rec, query, opts, log, cancel)
    if _cancelled(cancel):
        result.status = "cancelled"
        return result
    if not ranked:
        result.message = "no downloadable versions found on the enabled sources"
        return result
    _log(log, f"Found {len(ranked)} version(s); best-first.")
    if choose_candidates:
        chosen = choose_candidates(ranked)
        if chosen is None:
            result.status = "cancelled"
            return result
        if chosen:
            ranked = chosen

    # 4. download best-first, verifying each
    staging.mkdir(parents=True, exist_ok=True)
    best_fallback = None    # (report, candidate, path, ident) -- best non-winner
    tried = 0
    for cand in ranked:
        if _cancelled(cancel):
            result.status = "cancelled"
            break
        if tried >= max_dl:
            _log(log, f"Reached max downloads ({max_dl}).")
            break
        prov = providers.registry().get(cand.provider)
        if not prov:
            continue
        tried += 1
        _log(log, f"Downloading [{cand.label()}] \"{cand.title}\" ...")
        try:
            path = prov.download(cand, staging, ff,
                                 lambda m: _log(log, m), cancel)
        except Exception as exc:
            _log(log, f"  download error: {type(exc).__name__}: {exc}")
            path = None
        if not path or not Path(path).exists():
            result.tried.append((cand.label(), "download failed / skipped"))
            continue
        if _cancelled(cancel):
            _safe_unlink(path)
            result.status = "cancelled"
            break

        # verify it's the right recording
        ident = identify.verify_recording(path, rec, ff, config.ACOUSTID_API_KEY)
        if not ident.matched:
            _log(log, f"  rejected -- wrong recording ({ident.detail})")
            result.tried.append((cand.label(), f"wrong recording: {ident.detail}"))
            _safe_unlink(path)
            continue

        # measure the real quality (spectral genuineness for lossless formats)
        report = quality.measure_quality(path, ff, cand,
                                         want_lossless=opts.want_lossless)
        _log(log, f"  measured: {report.summary()}  score={report.score:.2f}"
             + (f"  [{ident.method} {ident.confidence:.2f}]"))
        if report.rejected:
            _log(log, f"  rejected -- {report.reject_reason}")

        winner = ((not report.rejected)
                  and (quality.is_genuine_lossless(report) or not opts.want_lossless))
        if winner:
            return _finalize((report, cand, Path(path), ident), rec, ff,
                             opts, dest, log, result)

        # keep only the best-scoring non-winner as a fallback
        reason = (report.reject_reason
                  or ("lossy" if not report.is_lossless_format
                      else (report.spectral.verdict if report.spectral else "?")))
        result.tried.append((cand.label(), reason))
        if best_fallback is None or report.score > best_fallback[0].score:
            if best_fallback is not None:
                _safe_unlink(best_fallback[2])
            best_fallback = (report, cand, Path(path), ident)
        else:
            _safe_unlink(path)

    if _cancelled(cancel):
        result.status = "cancelled"
        result.message = "cancelled"
        return result

    # 5-8. no genuine lossless -> use the best acceptable fallback (unless lossless-only)
    if best_fallback:
        rep = best_fallback[0]
        if opts.lossless_only and (not rep.is_lossless_format
                                   or quality.is_fake_lossless(rep)):
            _safe_unlink(best_fallback[2])
            result.message = "no genuine lossless version found (--lossless-only)"
            return result
        _log(log, "No verified-genuine lossless found -- using best available.")
        return _finalize(best_fallback, rec, ff, opts, dest, log, result)

    result.message = "no acceptable version could be downloaded"
    return result


def _finalize(winner, rec, ff, opts, dest, log, result) -> FinderResult:
    report, cand, path, ident = winner
    result.candidate = cand
    result.report = report

    # make it Music.app-compatible (FLAC/APE/... -> ALAC; other lossy -> AAC)
    src = _ensure_compatible(path, ff, log)
    if src is None:
        result.message = "format conversion failed"
        return result
    report.path = src

    # 5. dedup against the library (index excludes the staging dir)
    conn = dedup.open_catalog()
    try:
        _log(log, "Checking your library for duplicates...")
        n = dedup.build_index(conn, ff=ff, fingerprint=opts.fingerprint_index,
                              progress=lambda m: _log(log, m))
        if n:
            _log(log, f"  indexed {n} new/changed file(s)")
        matches = dedup.find_duplicates(
            conn, report,
            {"artist": rec.artist, "title": rec.title, "album": rec.album},
            ident.fingerprint)
        action, existing = dedup.decide_dedup(report, matches,
                                              opts.replace_lower_quality)
    finally:
        conn.close()

    if action == "skip":
        where = existing["row"]["path"] if existing else ""
        _log(log, f"You already have an equal-or-better copy -- skipping.\n  {where}")
        _safe_unlink(src)
        result.status = "skipped"
        result.action = "skip"
        result.message = "duplicate skipped (existing copy is as good or better)"
        return result

    # 6. fix metadata + artwork (on the downloaded copy only)
    _log(log, "Fixing metadata and artwork...")
    art = artwork.fetch_artwork(rec, cand, log=lambda m: _log(log, m))
    tagging.write_tags(src, rec, art, log=lambda m: _log(log, m))
    if art:
        _log(log, "  embedded cover art")

    # 7. name + organize (reuse the recovery pipeline's naming)
    import itunes
    tags = itunes.read_tags(src)
    final_path = itunes.build_final_path(dest, tags, src.name)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if final_path.exists():
        if action == "add" and existing:      # keep old, mark new as an upgrade
            final_path = final_path.with_name(
                f"{final_path.stem} (upgrade){final_path.suffix}")
        else:
            stem, suf, k = final_path.stem, final_path.suffix, 2
            while final_path.exists():
                final_path = final_path.with_name(f"{stem} ({k}){suf}")
                k += 1
    shutil.move(str(src), final_path)

    if action == "replace" and existing:
        old = Path(existing["row"]["path"])
        try:
            if old.exists() and old != final_path:
                old.unlink()
                _log(log, f"  replaced lower-quality copy: {old}")
        except Exception as exc:
            _log(log, f"  could not remove old copy ({exc})")

    # 8. add to Music.app + record ledgers
    added = False
    if opts.add_to_library and config.AUTO_ADD.exists():
        target = config.AUTO_ADD / final_path.name
        if not target.exists():
            shutil.copy2(final_path, target)
            added = True
    elif opts.add_to_library:
        _log(log, "  note: Music.app auto-add folder not found -- open Music once.")

    _record_manifest(rec, cand, final_path, report)
    conn = dedup.open_catalog()
    try:
        dedup.index_file(conn, final_path, ff,
                         fingerprint=opts.fingerprint_index,
                         genuine_confidence=report.genuine_confidence)
    finally:
        conn.close()

    result.status = "replaced" if action == "replace" else "added"
    result.action = action
    result.final_path = final_path
    result.message = (f"{result.status}: {final_path}"
                      + ("  (copied into Music.app)" if added else ""))
    _log(log, f"Done -- {result.message}")
    if action == "add" and existing:
        _log(log, "  (kept your existing copy too; this one is a higher-quality alternate)")
    return result
