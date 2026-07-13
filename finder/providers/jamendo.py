"""Jamendo provider -- Creative Commons catalog.

Jamendo's API needs a free client id. Set JAMENDO_CLIENT_ID in the environment
(register at https://devportal.jamendo.com/) to enable it; without it this
provider simply returns nothing. Can serve FLAC where the track offers it.
"""

import os
from pathlib import Path

from .base import BaseProvider, Candidate, http_session

API = "https://api.jamendo.com/v3.0/tracks/"


class Jamendo(BaseProvider):
    name = "jamendo"
    trust = 0.5

    def search(self, rec, query, limit):
        client_id = os.environ.get("JAMENDO_CLIENT_ID")
        if not client_id:
            return []  # needs a free client id; degrade silently
        term = rec.query() if rec else query
        try:
            r = http_session().get(API, timeout=25, params={
                "client_id": client_id, "format": "json",
                "limit": max(2, min(6, limit)), "search": term,
                "audioformat": "flac", "include": "musicinfo",
                "audiodlformat": "flac"})
            results = (r.json() or {}).get("results", [])
        except Exception:
            return []
        out = []
        for t in results:
            url = t.get("audiodownload")
            if not url or not t.get("audiodownload_allowed", True):
                url = t.get("audio")  # fall back to the stream
            if not url:
                continue
            lossless = "flac" in url.lower()
            out.append(Candidate(
                provider=self.name, source_id=str(t.get("id")), url=url,
                title=t.get("name", term), artist=t.get("artist_name", ""),
                album=t.get("album_name"),
                codec="flac" if lossless else "mp3", lossless_claimed=lossless,
                duration_s=t.get("duration"),
                artwork_url=t.get("album_image") or t.get("image"),
                has_artwork=bool(t.get("album_image")),
                source_trust=self.trust, metadata_completeness=0.7,
                extra={"filename": f"{t.get('id')}."
                       + ("flac" if lossless else "mp3")}))
        return out

    def download(self, cand, dest_dir, ff, log, cancel):
        name = cand.extra.get("filename") or Path(cand.url.split("?")[0]).name
        return self._http_download(cand.url, Path(dest_dir) / name, log, cancel)


PROVIDER = Jamendo()
