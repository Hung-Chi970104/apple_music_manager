"""Wikimedia Commons provider -- public-domain / Creative Commons audio.

Keyless MediaWiki API. Yields direct file URLs (often FLAC/WAV/OGG). Good for
public-domain and historical recordings.
"""

from pathlib import Path

from .base import BaseProvider, Candidate, guess_codec_from_url, http_session

API = "https://commons.wikimedia.org/w/api.php"


class Wikimedia(BaseProvider):
    name = "wikimedia"
    trust = 0.6

    def search(self, rec, query, limit):
        term = rec.query() if rec else query
        params = {
            "action": "query", "format": "json",
            "generator": "search",
            "gsrsearch": f"{term} filetype:audio",
            "gsrnamespace": 6, "gsrlimit": max(2, min(6, limit)),
            "prop": "imageinfo",
            "iiprop": "url|size|mime|mediatype|extmetadata",
        }
        try:
            r = http_session().get(API, params=params, timeout=25)
            pages = (r.json().get("query") or {}).get("pages") or {}
        except Exception:
            return []
        out = []
        for page in pages.values():
            info = (page.get("imageinfo") or [{}])[0]
            url = info.get("url")
            if not url:
                continue
            codec, lossless = guess_codec_from_url(url)
            if not codec:
                continue
            title = (page.get("title") or "").replace("File:", "")
            out.append(Candidate(
                provider=self.name, source_id=page.get("title", url), url=url,
                title=Path(title).stem, artist="",
                codec=codec, lossless_claimed=lossless,
                filesize=info.get("size"),
                source_trust=self.trust, metadata_completeness=0.4,
                extra={"filename": Path(url.split("?")[0]).name}))
        return out

    def download(self, cand, dest_dir, ff, log, cancel):
        name = cand.extra.get("filename") or Path(cand.url.split("?")[0]).name
        return self._http_download(cand.url, Path(dest_dir) / name, log, cancel)


PROVIDER = Wikimedia()
