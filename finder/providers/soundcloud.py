"""SoundCloud provider -- artist-enabled free downloads only.

Searches via yt-dlp's scsearch, but only actually downloads tracks the uploader
marked as downloadable (checked at download time), honoring the free-download
intent rather than grabbing arbitrary streams.
"""

from .base import BaseProvider, Candidate


class SoundCloud(BaseProvider):
    name = "soundcloud"
    trust = 0.4

    def search(self, rec, query, limit):
        from . import _ytdlp
        q = rec.query() if rec else query
        entries = _ytdlp.search("scsearch", q, min(limit, 5), log=lambda m: None)
        out = []
        for e in entries:
            if not e.get("id"):
                continue
            out.append(Candidate(
                provider=self.name, source_id=str(e["id"]), url=e["url"],
                title=e["title"], artist=e.get("uploader", ""),
                codec="mp3", lossless_claimed=False,
                duration_s=e.get("duration"), source_trust=self.trust,
                metadata_completeness=0.4,
                extra={"downloadable": e.get("downloadable")}))
        return out

    def download(self, cand, dest_dir, ff, log, cancel):
        from . import _ytdlp
        return _ytdlp.download(cand.url, dest_dir, ff, log, cancel,
                               require_downloadable=True)


PROVIDER = SoundCloud()
