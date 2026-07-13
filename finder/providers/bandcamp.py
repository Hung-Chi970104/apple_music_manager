"""Bandcamp provider.

Bandcamp has no public text-search API, so free-text queries return nothing
here. If the query IS a Bandcamp track/album URL, we resolve and download it via
yt-dlp (which fetches the artist's free/streamable version). Purchased downloads
require your Bandcamp login and are out of scope for the shipped adapter -- add
your own credentialed provider module if you want them.
"""

from .base import BaseProvider, Candidate, guess_codec_from_url


class Bandcamp(BaseProvider):
    name = "bandcamp"
    trust = 0.6

    def search(self, rec, query, limit):
        q = (query or "").strip()
        if "bandcamp.com" in q and q.lower().startswith(("http://", "https://")):
            codec, lossless = guess_codec_from_url(q)
            return [Candidate(
                provider=self.name, source_id=q, url=q,
                title=(rec.title if rec else q), artist=(rec.artist if rec else ""),
                codec=codec, lossless_claimed=lossless,
                source_trust=self.trust, metadata_completeness=0.5)]
        return []  # no search API for plain text

    def download(self, cand, dest_dir, ff, log, cancel):
        from . import _ytdlp
        return _ytdlp.download(cand.url, dest_dir, ff, log, cancel)


PROVIDER = Bandcamp()
