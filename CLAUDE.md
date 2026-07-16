# CLAUDE.md — project map (read this first, don't re-explore)

**What this is:** Tools to (a) recover iTunes-synced music off an iPad/iPhone back
onto a Mac over USB, and (b) find + download the best *genuine-lossless* copy of a
song from legal sources. ~4,500 lines of Python. **For personal use only.**

**Platform:** The *app* targets **macOS** (Music.app, `~/.venvs/ipad-recovery`,
`pymobiledevice3`). This *dev machine is Windows* — pure-logic tests
(`dedup`/`quality`/`spectral`) run here; device/Music.app/ffmpeg paths do not.

## How to use this file (token discipline)
1. Consult the **File map** and **Where to look** tables below *before* grepping/reading.
   Go straight to the named file+symbol.
2. Read only the function you need, not whole files. `finder/*` modules have accurate
   docstrings — read those first.
3. Line *counts* below are size hints; symbol *names* are the stable anchors (line
   numbers drift — don't trust remembered ones, re-grep the symbol).
4. Resuming work? Read `CURRENT_TASK.md`. Structure changed? Update the map here in
   the same change.
5. **Auto-update cadence (standing instruction):** set `CURRENT_TASK.md` when starting
   substantive work; append a line to `PROGRESS.md` when finishing it. No reminder needed.
6. **Delegate broad exploration.** For any "sweep many files" question, spawn an
   `Explore`/`general-purpose` subagent and keep only its conclusion — don't pull file
   dumps into the main context. Use the map above for pinpoint lookups instead.
7. **Session hygiene.** One task per session; continuity lives in `CURRENT_TASK.md`/
   `PROGRESS.md`, not the transcript. Unrelated task → start fresh (`/clear`).
8. **Targeted tests.** Run the single test/node affected, not the whole suite, while iterating.

## Architecture — 3 entry points, 1 engine
- **`itunes.py`** (CLI, 693 ln) — device recovery. `main()` orchestrates: AFC pull →
  `read_tags`/`build_final_path`/`sanitize` → `load_db_metadata`/`extract_playlists`
  → `import_to_music` (drops into `AUTO_ADD`). `--dry-run` looks without touching.
- **`music_gui.py`** (Tkinter, 893 ln) — `App` class, one tab per `_build_*` method:
  Devices / Find / YouTube / Import / Export. `run_bg` runs work off the UI thread;
  `get_ffmpeg` resolves ffmpeg/ffprobe (finder reuses this).
- **`finder/`** (engine, ~1,900 ln) — the "Find" feature, CLI + GUI. **Single entry:
  `finder.finder.run_finder(query, opts, log, cancel, choose_*)`**. GUI vs CLI differ
  only by the optional `choose_recording`/`choose_candidates` callbacks.

### finder pipeline (run_finder, in order) → where each step lives
1. **identify** recording (MusicBrainz) → `identify.py` (`search_recordings`, `mb_init`)
2. **fan out** across sources → `finder.search_versions` + `providers/` registry
   (`providers/__init__.py: enabled_providers`)
3. **rank** by advertised specs → `quality.py` (`rank_candidates`, `score_candidate`)
4. **download + verify** each best-first → `provider.download`, then
   `quality.measure_quality`, `spectral.analyze_genuineness` (**fake-lossless reject**),
   `identify.verify_recording` (right recording?)
5. **dedup** vs library (safe, never deletes) → `dedup.py` (`build_index`,
   `find_duplicates`, `decide_dedup`)
6. **tag + artwork** → `tagging.write_tags`, `artwork.fetch_artwork`/`normalize_image`
7. **finalize + add to Music** → `finder._finalize` (reuses `itunes.build_final_path`/
   `sanitize`; copies into `config.AUTO_ADD`)

## File map
| Path | Responsibility |
|---|---|
| `itunes.py` | Device recovery CLI. Track/Counters dataclasses, AFC pull, tag→filename, playlist DB extraction, import to Music.app. |
| `music_gui.py` | Tkinter GUI. `App`, per-tab `_build_*`, `run_bg`, `get_ffmpeg`, `classify`/`convert`. |
| `finder/finder.py` | **Orchestrator** — `run_finder`, `search_versions`, `_finalize`, `get_ffmpeg`, manifest. |
| `finder/config.py` | All paths (mirror itunes.py), provider order, API keys/env. stdlib-only on purpose. |
| `finder/identify.py` | MusicBrainz search + AcoustID fingerprint + recording verification. |
| `finder/quality.py` | `QualityReport`, spec scoring, ranking, `probe_specs`, fake/genuine-lossless predicates. |
| `finder/spectral.py` | numpy spectral analysis → detect lossy-transcoded-into-lossless (frequency cutoff). |
| `finder/dedup.py` | sqlite catalog of owned files; match + keep-both/replace decision. |
| `finder/tagging.py` | Write text tags + embed cover art (mutagen). |
| `finder/artwork.py` | Fetch art (Cover Art Archive → iTunes), normalize image. |
| `finder/providers/base.py` | `Candidate` dataclass, `Provider` protocol, `BaseProvider`, http download/throttle. |
| `finder/providers/__init__.py` | Auto-discovery registry; `enabled_providers(names)`. |
| `finder/providers/*.py` | One source each: internetarchive, wikimedia, ccmixter, jamendo, bandcamp, soundcloud, youtube; fma/musopen are stubs; `_ytdlp.py` shared yt-dlp helper. |
| `finder/cli.py` | `python -m finder` argparse front end. |
| `tests/test_{dedup,quality,spectral}.py` | Pure-logic unit tests (run on Windows). |

## Where to look (symptom → file:symbol)
| Symptom / task | Go to |
|---|---|
| Wrong/missing song identified | `identify.py: search_recordings`, `parse_query`, `verify_recording` |
| "Fake lossless" mis-classified (false accept/reject) | `spectral.py: classify_spectrum`, `estimate_cutoff`; `quality.py: is_fake_lossless` |
| Bad ranking / wrong version picked | `quality.py: score_candidate`, `rank_candidates`, `_*_score` |
| Duplicate handling / re-downloads | `dedup.py: norm_key`, `find_duplicates`, `decide_dedup` |
| Add a new music source | new module in `finder/providers/` exposing `PROVIDER`; add name to `config.PROVIDERS` order |
| Provider returns nothing / API change | `finder/providers/<name>.py: search`; `base.py: http_session`, `throttle` |
| Tags/artwork wrong on output | `tagging.py`, `artwork.py` |
| Paths / where files land / API keys | `finder/config.py` |
| Device not detected / recovery fails | `itunes.py: main`, AFC constants (top), `load_db_metadata`, `extract_playlists` |
| Playlists missing/wrong after recovery | `itunes.py: introspect_playlist_tables`, `extract_playlists` |
| GUI tab bug / freeze | `music_gui.py: _build_<tab>`, `run_bg` (threading), `_drain` |
| ffmpeg/ffprobe not found / convert fails | `music_gui.py: get_ffmpeg`, `convert`; `finder/finder.py: get_ffmpeg` |

## Cross-module contracts (don't break these)
- `finder/` **imports from `itunes.py`**: `AUTO_ADD` sink, `sanitize`, `build_final_path`,
  `read_tags`. `finder/__init__.py` puts the repo root on `sys.path` so this works from
  `python -m finder` or when imported by the GUI.
- `finder/config.py` is **stdlib-only** so pure-logic modules import it without pulling in
  device libs. Don't add heavy imports there.
- `config.FINDER_STAGING` is deliberately *outside* the dedup index roots — an in-progress
  download must never look like an owned file.

## Commands
```bash
python itunes.py --dry-run        # recovery, look-don't-touch
python itunes.py                  # full recovery (macOS)
python music_gui.py               # GUI
python -m finder "Artist - Title" [--lossless-only --max-downloads N --providers a,b --no-add]
python -m pytest tests/           # or run tests/test_*.py directly
```

## Conventions
- Match surrounding style; `finder/*` uses precise module docstrings + `log=print`
  injectable logging + `cancel` callbacks for GUI cancellation. Keep both.
- Never delete user music — dedup is keep-both by default (`--replace-lower` opts in).
- Env keys: `JAMENDO_CLIENT_ID`, `ACOUSTID_API_KEY`, `MB_CONTACT` (see `config.py`).
