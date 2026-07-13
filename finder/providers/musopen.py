"""Musopen provider -- public-domain classical recordings (lossless).

Musopen's public browsing API is undocumented/auth-gated, so this ships as a
best-effort stub that returns nothing rather than depending on a fragile private
endpoint. A large share of Musopen's public-domain catalog is also mirrored on
the Internet Archive (fully supported here). Fill in search()/download() against
a stable Musopen endpoint if one becomes available; the registry auto-discovers
it.
"""

from .base import BaseProvider


class Musopen(BaseProvider):
    name = "musopen"
    trust = 0.6

    def search(self, rec, query, limit):
        return []

    def download(self, cand, dest_dir, ff, log, cancel):
        return None


PROVIDER = Musopen()
