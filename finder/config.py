"""Central configuration for the finder package.

Kept dependency-light on purpose (stdlib only) so the pure-logic modules
(spectral, quality) can import it without dragging in itunes / device libraries.
Paths mirror itunes.py constants rather than importing itunes here.
"""

import os
from pathlib import Path

# ---- identity / API keys ---------------------------------------------------
APP_NAME = "MusicManagerFinder"
APP_VERSION = "0.1"
# MusicBrainz requires a contact in the User-Agent (keyless otherwise).
CONTACT_EMAIL = os.environ.get("MB_CONTACT", "hungchi970104@gmail.com")
# Optional -- enables AcoustID web lookups when a free key + fpcalc are present.
ACOUSTID_API_KEY = os.environ.get("ACOUSTID_API_KEY") or None

# ---- paths (mirror itunes.py) ----------------------------------------------
HOME = Path.home()
LIBRARY_ROOT = HOME / "Music" / "iPad Recovery"          # itunes.DEFAULT_DEST
FINDER_DEST = LIBRARY_ROOT / "Found"                      # organized finder output
# Raw downloads land here. Deliberately OUTSIDE FINDER_DEST / the index roots so
# an in-progress download is never mistaken for an existing library file.
FINDER_STAGING = LIBRARY_ROOT / "_finder_staging"
FINDER_MANIFEST = LIBRARY_ROOT / "_finder_manifest.json"  # separate from recovery ledger
CATALOG_DB = LIBRARY_ROOT / "_catalog.db"                # dedup index (shared)
AUTO_ADD = (                                             # itunes.AUTO_ADD
    HOME / "Music/Music/Media.localized/Automatically Add to Music.localized"
)
# Library roots scanned when building the dedup catalog.
LIBRARY_INDEX_ROOTS = [
    LIBRARY_ROOT / "Music",                              # itunes EXPORT_SRC
    FINDER_DEST,
    HOME / "Music" / "YouTube Music",
]

# ---- providers -------------------------------------------------------------
# Enabled providers, in preference order. A user can drop a new module into
# finder/providers/ exposing a module-level PROVIDER and add its name here.
PROVIDERS_ENABLED = [
    "internetarchive",
    "musopen",
    "ccmixter",
    "wikimedia",
    "fma",
    "jamendo",
    "bandcamp",
    "soundcloud",
    "youtube",
]

# ---- pipeline defaults -----------------------------------------------------
MAX_DOWNLOADS = 3            # bandwidth bound: try at most N candidates
CANDIDATES_PER_PROVIDER = 4  # search breadth per source
WANT_LOSSLESS = True         # prefer genuine lossless; fall back to best lossy
REPLACE_LOWER_QUALITY = False  # user choice: safe / never delete existing files

# ---- spectral (genuine vs fake lossless) thresholds ------------------------
SPECTRAL_GENUINE = 0.66      # genuine_confidence >= -> "genuine"
SPECTRAL_TRANSCODE = 0.35    # genuine_confidence <  -> "transcode" (reject as fake)
SPECTRAL_WINDOWS = 3         # number of analysis windows
SPECTRAL_WINDOW_S = 20.0     # seconds per window

# ---- identity verification thresholds --------------------------------------
VERIFY_FUZZY = 0.85          # title/artist fuzzy match to accept a download
VERIFY_DUR_TOL_S = 3.0       # absolute duration tolerance (seconds)
VERIFY_DUR_TOL_FRAC = 0.02   # relative duration tolerance

# ---- dedup thresholds ------------------------------------------------------
DEDUP_FP_SIM = 0.90          # chromaprint similarity -> same recording
DEDUP_FUZZY = 0.88           # title/artist fuzzy for the no-fingerprint path
DEDUP_DUR_TOL_S = 2.0        # duration tolerance for a duplicate

# MusicBrainz keyless politeness: <= ~1 request/second.
MB_MIN_INTERVAL_S = 1.1
