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
  - **YouTube** — download best-quality audio from a link or search
  - **Import** — add local audio files; unsupported formats (FLAC/OGG/OPUS/…) are converted automatically
  - **Export** — copy the library to any folder/drive, or push songs into the VLC app on a device

## Requirements

- Python 3
- [`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3) >= 9.33
- [`mutagen`](https://github.com/quodlibet/mutagen) >= 1.48

## Usage

```bash
# Recover music (look without touching anything first)
python itunes.py --dry-run

# Full recovery
python itunes.py

# Launch the GUI
python music_gui.py
```
