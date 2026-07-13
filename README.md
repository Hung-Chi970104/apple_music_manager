# Music Manager

Tools for recovering and managing music between an iPad/iPhone and a Mac's
Music app — all over USB, no paid services required.

## Contents

- **`itunes.py`** — Recover iTunes-synced music from an iPad/iPhone back onto
  your Mac. Pulls songs out of the device's hidden `iTunes_Control` folder over
  USB, restores real filenames from the embedded tags, rebuilds playlists from
  the device's media database, and drops everything into the Music app's
  "Automatically Add" folder so it appears in your library.

- **`music_gui.py`** — A single-window app (Tkinter) for recovering, importing,
  downloading, and exporting music:
  - **Devices** — see connected iPads/iPhones and recover their music into Music.app
  - **Find** — search a song by name, compare versions from different legal sources, and
    one-click download the best *genuine* lossless copy (see below)
  - **YouTube** — download best-quality audio from a link or search
  - **Import** — add local audio files; unsupported formats (FLAC/OGG/OPUS/…) are converted automatically
  - **Export** — copy the library to any folder/drive, or push songs into the VLC app on a device

- **`finder/`** — The engine behind the **Find** tab (also usable from the CLI). Type a
  song name and it:
  1. **identifies** the recording (MusicBrainz),
  2. **fans out** across legal sources for matching versions,
  3. **ranks** them and downloads best-first,
  4. **verifies** each download is the right recording and rejects **fake lossless**
     (lossy transcoded into FLAC/ALAC — detected by spectral analysis),
  5. **fixes** metadata + embeds cover art,
  6. **skips duplicates** you already own (never deletes anything), and
  7. **adds** the result to your Music library.

  **Sources** (pluggable — drop a module in `finder/providers/` to add your own): Internet
  Archive (incl. the Live Music Archive — real lossless FLAC), Wikimedia Commons and
  ccMixter (Creative Commons / public domain), Jamendo (needs a free `JAMENDO_CLIENT_ID`),
  Bandcamp (by URL), SoundCloud (artist-enabled free downloads only), and YouTube as a
  lossy fallback. Free Music Archive / Musopen ship as stubs (no current public API — their
  content is largely mirrored on the Internet Archive). No DRM-protected streaming rippers
  are included. **For personal use only.**

## Requirements

- Python 3
- [`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3) >= 9.33 — device access
- [`mutagen`](https://github.com/quodlibet/mutagen) >= 1.48 — tag read/write
- `numpy` — spectral fake-lossless detection (Find)
- `musicbrainzngs` — recording identification (Find; degrades gracefully if absent)
- `yt-dlp`, `static_ffmpeg`, `requests`, `Pillow`
- *Optional:* `pyacoustid` + the `fpcalc` binary (`brew install chromaprint`) for AcoustID
  fingerprinting; `rapidfuzz` for faster fuzzy matching; `tkinterdnd2` for GUI drag-and-drop

Install everything with:

```bash
~/.venvs/ipad-recovery/bin/python -m pip install -r requirements.txt
```

## Usage

```bash
# Recover music (look without touching anything first)
python itunes.py --dry-run

# Full recovery
python itunes.py

# Launch the GUI (Devices / Find / YouTube / Import / Export)
python music_gui.py

# Find the best genuine-lossless copy of a song from the command line
python -m finder "Miles Davis - So What"
python -m finder "Bach - Cello Suite No. 1" --lossless-only --max-downloads 3
python -m finder "some song" --providers internetarchive,ccmixter,wikimedia --no-add
```

Run the tests with `python -m pytest tests/` (or run each `tests/test_*.py` directly).
