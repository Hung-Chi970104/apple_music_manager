"""Shared yt-dlp helpers for the URL-scraping providers (youtube, soundcloud,
bandcamp). Leading underscore -> the registry ignores this module.

yt-dlp is imported lazily so it's only needed when one of those providers runs.
Downloads extract best audio to a Music-compatible m4a (these are the lossy
fallback tier; genuine lossless comes from the direct-download HTTP providers).
"""

from pathlib import Path


def search(prefix: str, query: str, limit: int, log) -> list[dict]:
    """Flat (fast) search -> list of {id,title,url,duration,uploader,downloadable}."""
    try:
        import yt_dlp
    except Exception as exc:
        log(f"  yt-dlp unavailable: {exc}")
        return []
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
            "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"{prefix}{max(1, limit)}:{query}",
                                    download=False)
    except Exception as exc:
        log(f"  search failed: {exc}")
        return []
    out = []
    for e in (info.get("entries") or []):
        if not e:
            continue
        out.append({
            "id": e.get("id"),
            "title": e.get("title") or "",
            "url": e.get("url") or e.get("webpage_url") or e.get("id"),
            "duration": e.get("duration"),
            "uploader": e.get("uploader") or e.get("channel") or "",
            "downloadable": e.get("downloadable"),
        })
    return out


def download(url: str, dest_dir, ff: dict, log, cancel,
             require_downloadable: bool = False) -> Path | None:
    """Download best audio at `url` to `dest_dir` as m4a. Honors `cancel`.

    If require_downloadable, first checks the track exposes an artist-enabled
    download and bails out (returns None) otherwise.
    """
    try:
        import yt_dlp
        from yt_dlp.utils import DownloadCancelled
    except Exception as exc:
        log(f"  yt-dlp unavailable: {exc}")
        return None
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    def hook(d):
        if cancel is not None and getattr(cancel, "is_set", lambda: False)():
            raise DownloadCancelled("cancelled")

    if require_downloadable:
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                                   "skip_download": True}) as ydl:
                probe = ydl.extract_info(url, download=False)
            if not (probe.get("downloadable")):
                log("  skipped: not an artist-enabled free download")
                return None
        except Exception as exc:
            log(f"  could not verify downloadable ({exc})")
            return None

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(dest_dir / "%(title).150s.%(ext)s"),
        "ffmpeg_location": ff.get("ffmpeg"),
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "m4a",
             "preferredquality": "0"},
            {"key": "FFmpegMetadata"},
        ],
        "quiet": True, "no_warnings": True, "noprogress": True,
        "ignoreerrors": "only_download",
        "progress_hooks": [hook],
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadCancelled:
        return None
    except Exception as exc:
        log(f"  download failed: {exc}")
        return None
    if not info:
        return None
    entry = info
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        entry = entries[0] if entries else info
    for rd in (entry.get("requested_downloads") or []):
        fp = rd.get("filepath")
        if fp and Path(fp).exists():
            return Path(fp)
    m4as = sorted(dest_dir.glob("*.m4a"), key=lambda p: p.stat().st_mtime)
    return m4as[-1] if m4as else None
