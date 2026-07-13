"""Album-art fetching + normalization.

Tries, in order: the MusicBrainz Cover Art Archive (via the recording's
release-group MBID), any artwork URL the provider handed us, then the keyless
iTunes Search API as a last resort. Images are normalized to a reasonable JPEG
with Pillow (already installed) before embedding.
"""

import io
import urllib.parse

from .providers.base import http_session

CAA = "https://coverartarchive.org"


def _get(url: str, log) -> bytes | None:
    try:
        r = http_session().get(url, timeout=20, allow_redirects=True)
        if r.status_code == 200 and r.content:
            return r.content
    except Exception as exc:
        log(f"  artwork fetch failed ({type(exc).__name__})")
    return None


def _from_caa(rec, log) -> bytes | None:
    if not rec or not getattr(rec, "release_group_mbid", None):
        return None
    return _get(f"{CAA}/release-group/{rec.release_group_mbid}/front-500", log)


def _from_itunes(rec, log) -> bytes | None:
    if not rec:
        return None
    term = urllib.parse.quote(f"{rec.artist} {rec.title}".strip())
    if not term:
        return None
    try:
        r = http_session().get(
            f"https://itunes.apple.com/search?term={term}"
            "&entity=song&limit=1", timeout=20)
        results = (r.json() or {}).get("results") or []
        if results:
            art = results[0].get("artworkUrl100")
            if art:
                return _get(art.replace("100x100bb", "600x600bb"), log)
    except Exception:
        pass
    return None


def fetch_artwork(rec, candidate=None, log=print) -> bytes | None:
    """Best available cover art as normalized JPEG bytes, or None."""
    art_url = getattr(candidate, "artwork_url", None) if candidate else None
    for getter in (lambda: _from_caa(rec, log),
                   lambda: (_get(art_url, log) if art_url else None),
                   lambda: _from_itunes(rec, log)):
        data = getter()
        if data:
            return normalize_image(data)
    return None


def normalize_image(data: bytes, max_px: int = 1000) -> bytes:
    """Re-encode to RGB JPEG, capped to max_px on the long edge."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB")
        if max(im.size) > max_px:
            im.thumbnail((max_px, max_px))
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=90)
        return out.getvalue()
    except Exception:
        return data  # embed as-is if Pillow can't parse it
