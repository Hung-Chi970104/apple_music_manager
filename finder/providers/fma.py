"""Free Music Archive provider.

FMA's official public API was retired when the original site shut down (2018);
the revived site currently exposes no stable keyless search endpoint. This
adapter is therefore a best-effort stub that returns nothing rather than
scraping fragile HTML -- most historical FMA (Creative Commons) content is also
mirrored on the Internet Archive, which IS fully supported. If a public FMA API
returns, implement search()/download() here; the registry will pick it up
automatically.
"""

from .base import BaseProvider


class FreeMusicArchive(BaseProvider):
    name = "fma"
    trust = 0.5

    def search(self, rec, query, limit):
        return []

    def download(self, cand, dest_dir, ff, log, cancel):
        return None


PROVIDER = FreeMusicArchive()
