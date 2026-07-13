"""Internet Archive provider (incl. the Live Music Archive).

Uses the keyless advancedsearch + metadata JSON APIs. A huge source of genuine
lossless FLAC (concerts, public-domain, Creative Commons) -- exactly where
fake-lossless detection earns its keep. One best candidate per matching item.
"""

import urllib.parse
from pathlib import Path

from .base import BaseProvider, Candidate, guess_codec_from_url, http_session
from .. import config

SEARCH = "https://archive.org/advancedsearch.php"
META = "https://archive.org/metadata"
DL = "https://archive.org/download"

# IA "format" strings -> (codec, lossless)
FORMAT_MAP = {
    "Flac": ("flac", True), "24bit Flac": ("flac", True),
    "AIFF": ("wav", True), "WAVE": ("wav", True), "Shorten": ("wav", True),
    "VBR MP3": ("mp3", False), "MP3": ("mp3", False),
    "128Kbps MP3": ("mp3", False), "64Kbps MP3": ("mp3", False),
    "Ogg Vorbis": ("vorbis", False), "Apple Lossless Audio": ("alac", True),
}


def _codec_of(f):
    fmt = f.get("format", "")
    if fmt in FORMAT_MAP:
        return FORMAT_MAP[fmt]
    return guess_codec_from_url(f.get("name", ""))


def _parse_len(v):
    if v is None:
        return None
    s = str(v)
    try:
        if ":" in s:
            parts = [float(p) for p in s.split(":")]
            sec = 0.0
            for p in parts:
                sec = sec * 60 + p
            return sec
        return float(s)
    except ValueError:
        return None


def _best_file(files, want_title):
    from ..identify import ratio
    audio = []
    for f in files:
        codec, lossless = _codec_of(f)
        if not codec:
            continue
        name = f.get("name", "")
        title = f.get("title") or Path(name).stem
        m = max(ratio(title, want_title), ratio(name, want_title)) if want_title else 0.5
        audio.append((f, codec, lossless, title, m))
    if not audio:
        return None
    if want_title:
        good = [a for a in audio if a[4] >= 0.5]
        audio = good or audio
    audio.sort(key=lambda a: (a[4] >= 0.5, a[2], int(a[0].get("size") or 0)),
               reverse=True)
    return audio[0]


class InternetArchive(BaseProvider):
    name = "internetarchive"
    trust = 0.7

    def search(self, rec, query, limit):
        want_title = rec.title if rec else query
        creator = rec.artist if rec else ""
        try:
            r = http_session().get(SEARCH, timeout=25, params={
                "q": f"({query}) AND mediatype:(audio)",
                "fl[]": ["identifier", "title", "creator"],
                "rows": max(2, min(4, limit)), "page": 1, "output": "json"})
            docs = (r.json().get("response") or {}).get("docs", [])
        except Exception:
            return []
        out = []
        for doc in docs:
            ident = doc.get("identifier")
            if not ident:
                continue
            try:
                meta = http_session().get(f"{META}/{ident}", timeout=25).json()
            except Exception:
                continue
            chosen = _best_file(meta.get("files", []), want_title)
            if not chosen:
                continue
            f, codec, lossless, title, _m = chosen
            name = f.get("name", "")
            url = f"{DL}/{ident}/{urllib.parse.quote(name)}"
            br = f.get("bitrate")
            out.append(Candidate(
                provider=self.name, source_id=f"{ident}/{name}", url=url,
                title=title, artist=creator or doc.get("creator") or "",
                album=doc.get("title"),
                codec=codec, lossless_claimed=lossless,
                bitrate_kbps=int(br) if br and str(br).isdigit() else None,
                duration_s=_parse_len(f.get("length")),
                filesize=int(f.get("size")) if str(f.get("size", "")).isdigit() else None,
                source_trust=self.trust, has_artwork=False,
                metadata_completeness=0.6 if doc.get("creator") else 0.4,
                extra={"filename": name}))
        return out

    def download(self, cand, dest_dir, ff, log, cancel):
        name = cand.extra.get("filename") or Path(cand.url.split("?")[0]).name
        return self._http_download(cand.url, Path(dest_dir) / name, log, cancel)


PROVIDER = InternetArchive()
