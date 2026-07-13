"""YouTube provider -- lossy last-resort fallback via yt-dlp.

Always lossy (best audio extracted to m4a/AAC); the quality analyzer will never
label this "genuine lossless". Lowest trust so it only wins when nothing better
is available. Reuses the same yt-dlp path as the existing YouTube tab.
"""

from .base import BaseProvider, Candidate


class YouTube(BaseProvider):
    name = "youtube"
    trust = 0.3

    def search(self, rec, query, limit):
        from . import _ytdlp
        q = rec.query() if rec else query
        entries = _ytdlp.search("ytsearch", q, min(limit, 4), log=lambda m: None)
        out = []
        for e in entries:
            if not e.get("id"):
                continue
            out.append(Candidate(
                provider=self.name, source_id=str(e["id"]), url=e["url"],
                title=e["title"], artist=e.get("uploader", ""),
                codec="aac", lossless_claimed=False,
                duration_s=e.get("duration"), source_trust=self.trust,
                metadata_completeness=0.35))
        return out

    def download(self, cand, dest_dir, ff, log, cancel):
        from . import _ytdlp
        return _ytdlp.download(cand.url, dest_dir, ff, log, cancel)


PROVIDER = YouTube()
