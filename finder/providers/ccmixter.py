"""ccMixter provider -- Creative Commons remixes / originals.

Keyless JSON query API. Yields MP3 (and sometimes lossless) with a direct
download URL per upload.
"""

from pathlib import Path

from .base import BaseProvider, Candidate, guess_codec_from_url, http_session

API = "https://ccmixter.org/api/query"


class CCMixter(BaseProvider):
    name = "ccmixter"
    trust = 0.55

    def search(self, rec, query, limit):
        term = rec.query() if rec else query
        try:
            r = http_session().get(API, timeout=25, params={
                "f": "json", "search": term, "search_type": "all",
                "limit": max(2, min(6, limit))})
            uploads = r.json()
        except Exception:
            return []
        if not isinstance(uploads, list):
            return []
        out = []
        for up in uploads:
            files = up.get("files") or []
            best = None
            for f in files:
                url = f.get("download_url") or f.get("file_url")
                if not url:
                    continue
                codec, lossless = guess_codec_from_url(url)
                if not codec:
                    continue
                if best is None or (lossless and not best[2]):
                    best = (f, url, lossless, codec)
            if not best:
                continue
            f, url, lossless, codec = best
            out.append(Candidate(
                provider=self.name, source_id=str(up.get("upload_id") or url),
                url=url, title=up.get("upload_name") or (rec.title if rec else term),
                artist=up.get("user_name", ""),
                codec=codec, lossless_claimed=lossless,
                source_trust=self.trust, metadata_completeness=0.5,
                extra={"filename": Path(url.split("?")[0]).name}))
        return out

    def download(self, cand, dest_dir, ff, log, cancel):
        name = cand.extra.get("filename") or Path(cand.url.split("?")[0]).name
        return self._http_download(cand.url, Path(dest_dir) / name, log, cancel)


PROVIDER = CCMixter()
