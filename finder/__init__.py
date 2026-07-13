"""finder -- one-button "find the best genuine-lossless version" downloader.

Type a song name, see a few matching versions from different (legitimate)
sources, and press one button to fetch the highest-quality *genuine* lossless
copy. The package identifies the correct recording, compares versions, rejects
fake lossless (lossy transcoded into FLAC/ALAC), verifies the real audio
quality, fixes metadata + artwork, prevents duplicates, and adds the result to
the Music.app library.

It reuses the existing app: itunes.py (AUTO_ADD sink, sanitize/build_final_path,
read_tags) and music_gui.get_ffmpeg for ffmpeg/ffprobe. The single entry point
is `finder.finder.run_finder`, used by both the GUI "Find" tab and the CLI
(`python -m finder "artist - title"`).

For personal use only.
"""

import os
import sys
from pathlib import Path

# Make the sibling itunes.py importable no matter how we're launched
# (python -m finder from the repo, or imported by music_gui.py).
_REPO = str(Path(__file__).resolve().parent.parent)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

# urllib-based libs (musicbrainzngs, yt-dlp, acoustid) need a CA bundle. The
# macOS python.org build ships none wired into urllib, so TLS verification
# fails ("unable to get local issuer certificate"). certifi (a requests dep) has
# one; point urllib at it if nothing else already set it.
try:
    if not os.environ.get("SSL_CERT_FILE"):
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
except Exception:
    pass

__all__ = ["run_finder"]


def run_finder(*args, **kwargs):
    """Lazy proxy so `from finder import run_finder` works without importing
    the whole pipeline (and its heavier deps) until it's actually called."""
    from .finder import run_finder as _run
    return _run(*args, **kwargs)
