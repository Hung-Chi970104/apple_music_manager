#!/usr/bin/env python3
"""Recover iTunes-synced music from an iPad/iPhone back onto this Mac.

Pulls songs out of the device's hidden iTunes_Control folder over USB,
restores real filenames from the embedded tags, rebuilds playlists from the
device's media database, and drops everything into the Music app's
"Automatically Add" folder so it appears in your library.

Run under the recovery venv:
    ~/.venvs/ipad-recovery/bin/python itunes.py --dry-run   # look, don't touch
    ~/.venvs/ipad-recovery/bin/python itunes.py             # full recovery

Requires: pymobiledevice3 >= 9.33, mutagen >= 1.48
"""

import argparse
import asyncio
import inspect
import json
import shutil
import sqlite3
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

try:
    from pymobiledevice3 import usbmux
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService
    from pymobiledevice3.exceptions import (
        AfcException,
        AfcFileNotFoundError,
        ConnectionTerminatedError,
        NotPairedError,
        PairingDialogResponsePendingError,
        PasswordRequiredError,
        UserDeniedPairingError,
    )
except ImportError:
    sys.exit(
        "pymobiledevice3 is not installed in this Python.\n"
        "Run me with the recovery venv:\n"
        "  ~/.venvs/ipad-recovery/bin/python itunes.py"
    )

try:
    import mutagen
except ImportError:
    sys.exit("mutagen is not installed. Use ~/.venvs/ipad-recovery/bin/python")

AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".aac", ".wav", ".aif", ".aiff", ".flac", ".alac"}
DRM_EXTS = {".m4p"}
MUSIC_ROOTS_AFC = ["iTunes_Control/Music", "Purchases"]
MEDIA_DB_AFC = "iTunes_Control/iTunes/MediaLibrary.sqlitedb"
DEFAULT_DEST = Path.home() / "Music" / "iPad Recovery"
AUTO_ADD = (
    Path.home()
    / "Music/Music/Media.localized/Automatically Add to Music.localized"
)


@dataclass
class Track:
    afc_path: str          # path on the device, relative to the AFC media root
    size: int
    is_drm: bool = False
    staged: Path | None = None    # where it lands in _staging/
    final: Path | None = None     # organized Artist/Album/NN Title.ext path
    status: str = "pending"       # pulled | resumed | failed | duplicate | drm | organized


@dataclass
class Counters:
    pulled: int = 0
    resumed: int = 0
    failed: list = field(default_factory=list)
    duplicates: int = 0
    tag_fallbacks: int = 0
    drm: int = 0
    playlists: int = 0
    imported: int = 0


async def maybe_await(value):
    return await value if inspect.isawaitable(value) else value


def human(nbytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024 or unit == "GB":
            return f"{nbytes:.1f} {unit}" if unit != "B" else f"{int(nbytes)} B"
        nbytes /= 1024
    return f"{nbytes:.1f} GB"


def load_manifest(dest: Path) -> dict:
    """afc_path -> {size, final} for everything recovered in earlier runs."""
    try:
        return json.loads((dest / "_manifest.json").read_text())
    except (OSError, ValueError):
        return {}


def save_manifest(dest: Path, tracks: list, manifest: dict):
    for t in tracks:
        if t.final and t.status in ("organized", "duplicate", "drm"):
            manifest[t.afc_path] = {"size": t.size, "final": str(t.final)}
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "_manifest.json").write_text(
        json.dumps(manifest, indent=1, ensure_ascii=False))


# ---------------------------------------------------------------- stage 1

async def connect(udid: str | None, allow_wifi: bool):
    devices = await usbmux.list_devices()
    allowed = {"USB"} | ({"Network"} if allow_wifi else set())
    devices = [d for d in devices if d.connection_type in allowed]
    if udid:
        devices = [d for d in devices if d.serial == udid]

    if not devices:
        sys.exit(
            "No device found.\n"
            "  * Plug the iPad into this Mac with a USB-C cable (the MacBook's\n"
            "    charging cable works). Some cheap cables are charge-only -- if\n"
            "    nothing shows up, try another cable.\n"
            "  * Unlock the iPad and keep it unlocked.\n"
            "  * If it asks 'Trust This Computer?', tap Trust."
        )

    async def describe(dev):
        try:
            ld = await create_using_usbmux(serial=dev.serial)
            info = await maybe_await(ld.get_value()) or {}
            return dev, ld, info
        except (PasswordRequiredError,):
            sys.exit("The device is locked. Unlock it and run me again.")
        except (PairingDialogResponsePendingError, NotPairedError):
            sys.exit(
                "The device doesn't trust this Mac yet. Unlock it, tap 'Trust'\n"
                "on the popup, then run me again."
            )
        except UserDeniedPairingError:
            sys.exit(
                "'Don't Trust' was tapped on the device. Unplug, replug, and\n"
                "tap Trust this time."
            )

    if len(devices) > 1:
        # More than one device attached: prefer the sole iPad, else make the
        # user choose with --udid.
        described = [await describe(d) for d in devices]
        ipads = [t for t in described if "iPad" in str(t[2].get("ProductType", ""))]
        if len(ipads) == 1:
            dev, lockdown, info = ipads[0]
        else:
            print("Multiple devices connected -- pick one with --udid:")
            for dev, _, info in described:
                print(f"  {dev.serial}  {info.get('DeviceName', '?')} "
                      f"({info.get('ProductType', '?')})")
            sys.exit(2)
    else:
        dev, lockdown, info = await describe(devices[0])

    print(
        f"Connected: {info.get('DeviceName', 'device')} "
        f"({info.get('ProductType', '?')}, iOS {info.get('ProductVersion', '?')}, "
        f"via {dev.connection_type})"
    )
    return lockdown


# ---------------------------------------------------------------- stage 2

async def inventory(afc) -> tuple[list[Track], int]:
    tracks: list[Track] = []
    skipped_other = 0
    found_root = False
    for root in MUSIC_ROOTS_AFC:
        try:
            async for dirpath, _dirnames, filenames in afc.walk(root):
                found_root = True
                for name in filenames:
                    afc_path = f"{dirpath}/{name}" if dirpath else name
                    ext = Path(name).suffix.lower()
                    if ext in AUDIO_EXTS or ext in DRM_EXTS:
                        st = await afc.stat(afc_path)
                        tracks.append(Track(
                            afc_path=afc_path,
                            size=int(st.get("st_size", 0)),
                            is_drm=ext in DRM_EXTS,
                        ))
                    else:
                        skipped_other += 1
        except (AfcFileNotFoundError, AfcException):
            continue
    if not found_root:
        sys.exit(
            "Could not see iTunes_Control/Music on the device. Either there is\n"
            "no iTunes-synced music on it, or this iPadOS version no longer\n"
            "exposes it over USB. Nothing was changed."
        )
    return tracks, skipped_other


async def pull_media_db(afc, dest: Path) -> Path | None:
    db_dir = dest / "_db"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = None
    for suffix in ("", "-wal", "-shm"):
        remote = MEDIA_DB_AFC + suffix
        local = db_dir / (Path(MEDIA_DB_AFC).name + suffix)
        try:
            data = await afc.get_file_contents(remote)
            local.write_bytes(data)
            if suffix == "":
                db_path = local
        except (AfcFileNotFoundError, AfcException):
            if suffix == "":
                print("note: media database not found on device -- "
                      "playlists will be skipped, songs are unaffected")
    return db_path


# ---------------------------------------------------------------- stage 3

async def pull_all(afc, tracks: list[Track], staging: Path,
                   limit: int | None, counters: Counters, manifest: dict):
    todo = tracks[:limit] if limit else tracks
    total = len(todo)
    for i, track in enumerate(todo, 1):
        done = manifest.get(track.afc_path)
        if (done and done.get("size") == track.size
                and Path(done["final"]).exists()):
            track.final = Path(done["final"])
            track.status = "resumed"
            counters.resumed += 1
            print(f"[{i:>4}/{total}] already recovered  {track.afc_path}",
                  flush=True)
            continue
        local = staging / track.afc_path
        track.staged = local
        if local.exists() and local.stat().st_size == track.size:
            track.status = "resumed"
            counters.resumed += 1
            print(f"[{i:>4}/{total}] already have  {track.afc_path}", flush=True)
            continue
        local.parent.mkdir(parents=True, exist_ok=True)
        ok = False
        for attempt in (1, 2):
            try:
                data = await afc.get_file_contents(track.afc_path)
                if data is None:
                    raise AfcException(f"could not open {track.afc_path}", None)
                local.write_bytes(data)
                if track.size and len(data) != track.size:
                    raise AfcException(f"size mismatch on {track.afc_path}", None)
                ok = True
                break
            except ConnectionTerminatedError:
                raise
            except AfcException as exc:
                if attempt == 2:
                    counters.failed.append((track.afc_path, str(exc)))
                    track.status = "failed"
        if ok:
            track.status = "pulled"
            counters.pulled += 1
            print(f"[{i:>4}/{total}] {human(track.size):>9}  {track.afc_path}",
                  flush=True)
    # anything beyond --limit stays pending and is simply not processed further
    for track in tracks[len(todo):]:
        track.status = "skipped-limit"


# ---------------------------------------------------------------- stage 4

def sanitize(component: str) -> str:
    component = unicodedata.normalize("NFC", component)
    component = component.replace("/", "_").replace(":", "_").replace("\0", "")
    component = " ".join(component.split()).strip(". ")
    return component[:150] or "_"


def read_tags(path: Path) -> dict:
    try:
        f = mutagen.File(path, easy=True)
        if not f:
            return {}
        def first(key):
            v = f.get(key)
            return str(v[0]).strip() if v else ""
        return {
            "title": first("title"),
            "artist": first("artist"),
            "albumartist": first("albumartist"),
            "album": first("album"),
            "tracknumber": first("tracknumber").split("/")[0],
        }
    except Exception:
        return {}


def build_final_path(base: Path, tags: dict, orig_name: str) -> Path:
    artist = tags.get("albumartist") or tags.get("artist") or "Unknown Artist"
    album = tags.get("album") or "Unknown Album"
    title = tags.get("title")
    ext = Path(orig_name).suffix.lower()
    if title:
        num = tags.get("tracknumber", "")
        prefix = f"{int(num):02d} " if num.isdigit() else ""
        fname = f"{prefix}{sanitize(title)}{ext}"
    else:
        fname = orig_name
    return base / sanitize(artist) / sanitize(album) / fname


def organize(tracks: list[Track], dest: Path, db_meta: dict, counters: Counters):
    music_dir = dest / "Music"
    drm_dir = dest / "DRM Protected"
    for track in tracks:
        if track.status in ("failed", "skipped-limit") or not track.staged:
            continue
        if not track.staged.exists():
            continue
        tags = read_tags(track.staged)
        if not (tags.get("title") and tags.get("artist")):
            db_tags = db_meta.get(track.staged.name.lower(), {})
            merged = {**db_tags, **{k: v for k, v in tags.items() if v}}
            if merged != tags:
                tags = merged
            if not tags.get("title"):
                counters.tag_fallbacks += 1
        base = drm_dir if track.is_drm else music_dir
        final = build_final_path(base, tags, track.staged.name)
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            if final.stat().st_size == track.staged.stat().st_size:
                counters.duplicates += 1
                track.staged.unlink()
                track.final = final
                track.status = "duplicate"
                continue
            stem, suffix = final.stem, final.suffix
            n = 2
            while final.exists():
                final = final.with_name(f"{stem} ({n}){suffix}")
                n += 1
        shutil.move(str(track.staged), final)
        track.final = final
        if track.is_drm:
            counters.drm += 1
            track.status = "drm"
        else:
            track.status = "organized"


# ---------------------------------------------------------------- stage 5

def table_cols(cur, table: str) -> list[str]:
    return [r[1] for r in cur.execute(f"PRAGMA table_info({table})")]


def load_db_metadata(db_path: Path | None) -> dict:
    """basename.lower() -> tags dict, from the device's media database."""
    if not db_path or not db_path.exists():
        return {}
    meta = {}
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = con.cursor()
        tables = {r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "item_extra" not in tables:
            return {}
        ie_cols = table_cols(cur, "item_extra")
        if not {"item_pid", "location", "title"} <= set(ie_cols):
            return {}
        select = ["ie.item_pid", "ie.location", "ie.title"]
        joins = []
        select.append("ie.total_time_ms" if "total_time_ms" in ie_cols else "NULL")
        if "item" in tables and "item_pid" in table_cols(cur, "item"):
            i_cols = table_cols(cur, "item")
            joins.append("JOIN item i ON i.item_pid = ie.item_pid")
            select.append("i.track_number" if "track_number" in i_cols else "NULL")
            if ("item_artist" in tables and "item_artist_pid" in i_cols
                    and "item_artist" in table_cols(cur, "item_artist")):
                joins.append("LEFT JOIN item_artist ia "
                             "ON ia.item_artist_pid = i.item_artist_pid")
                select.append("ia.item_artist")
            else:
                select.append("NULL")
            if ("album" in tables and "album_pid" in i_cols
                    and "album" in table_cols(cur, "album")):
                joins.append("LEFT JOIN album al ON al.album_pid = i.album_pid")
                select.append("al.album")
            else:
                select.append("NULL")
        else:
            select += ["NULL", "NULL", "NULL"]
        sql = f"SELECT {', '.join(select)} FROM item_extra ie {' '.join(joins)}"
        for pid, location, title, ms, trackno, artist, album in cur.execute(sql):
            if not location:
                continue
            key = Path(str(location)).name.lower()
            meta[key] = {
                "item_pid": pid,
                "title": str(title or ""),
                "artist": str(artist or ""),
                "album": str(album or ""),
                "tracknumber": str(trackno or ""),
                "seconds": int(ms // 1000) if ms else -1,
            }
        con.close()
    except sqlite3.Error as exc:
        print(f"note: could not read media database ({exc}) -- "
              "playlists and metadata fallback skipped")
    return meta


def introspect_playlist_tables(cur) -> dict | None:
    tables = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    container = next((t for t in ("container", "playlist") if t in tables), None)
    member = next((t for t in ("container_item", "playlist_item") if t in tables),
                  None)
    if not container or not member:
        print("note: no playlist tables recognized in the media database.")
        print(f"      tables present: {', '.join(sorted(tables))}")
        return None
    c_cols = table_cols(cur, container)
    m_cols = table_cols(cur, member)
    name_col = next((c for c in ("name", "title") if c in c_cols), None)
    c_pid = next((c for c in c_cols if c.endswith("_pid") or c == "pid"), None)
    # the FK to the container table is normally named exactly like its pid
    # column; the member table's own primary key (e.g. container_item_pid)
    # must not match
    own_pk = f"{member}_pid"
    m_c_fk = next((c for c in m_cols if c == c_pid), None) or next(
        (c for c in m_cols
         if c != own_pk and ("container" in c or "playlist" in c)
         and c.endswith("_pid")),
        None)
    m_i_fk = next((c for c in m_cols if c == "item_pid"), None) or next(
        (c for c in m_cols if "item" in c and c.endswith("_pid")), None)
    order_col = next(
        (c for c in m_cols
         if c in ("position", "play_order", "sort_order", "orderno")
         or "order" in c or "position" in c),
        None)
    dk_col = next((c for c in c_cols if "distinguished" in c), None)
    if not all((name_col, c_pid, m_c_fk, m_i_fk)):
        print("note: playlist tables found but their layout is unrecognized -- "
              "skipping playlists.")
        print(f"      {container}: {', '.join(c_cols)}")
        print(f"      {member}: {', '.join(m_cols)}")
        return None
    return dict(container=container, member=member, name_col=name_col,
                c_pid=c_pid, m_c_fk=m_c_fk, m_i_fk=m_i_fk,
                order_col=order_col, dk_col=dk_col)


def extract_playlists(db_path: Path | None, db_meta: dict,
                      tracks: list[Track], dest: Path, counters: Counters):
    if not db_path or not db_path.exists():
        return
    # map item_pid -> recovered file on disk, via location basename
    by_basename = {}
    for t in tracks:
        if not t.final:
            continue
        by_basename[Path(t.afc_path).name.lower()] = t
        if t.staged:
            by_basename[t.staged.name.lower()] = t
        by_basename[t.final.name.lower()] = t
    pid_to_track = {}
    for key, m in db_meta.items():
        t = by_basename.get(key)
        if t and t.final:
            pid_to_track[m["item_pid"]] = (t, m)
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = con.cursor()
        layout = introspect_playlist_tables(cur)
        if not layout:
            return
        where = (f"WHERE ({layout['dk_col']} IS NULL OR {layout['dk_col']} = 0)"
                 if layout["dk_col"] else "")
        playlists = cur.execute(
            f"SELECT {layout['c_pid']}, {layout['name_col']} "
            f"FROM {layout['container']} {where}").fetchall()
        out_dir = dest / "Playlists"
        for c_pid, name in playlists:
            if not name:
                continue
            order = f"ORDER BY {layout['order_col']}" if layout["order_col"] else ""
            rows = cur.execute(
                f"SELECT {layout['m_i_fk']} FROM {layout['member']} "
                f"WHERE {layout['m_c_fk']} = ? {order}", (c_pid,)).fetchall()
            entries = [pid_to_track[r[0]] for r in rows if r[0] in pid_to_track]
            if not entries:
                continue
            out_dir.mkdir(parents=True, exist_ok=True)
            lines = ["#EXTM3U"]
            for t, m in entries:
                artist = m.get("artist") or "Unknown Artist"
                title = m.get("title") or t.final.stem
                lines.append(f"#EXTINF:{m.get('seconds', -1)},{artist} - {title}")
                lines.append(str(t.final))
            (out_dir / f"{sanitize(str(name))}.m3u8").write_text(
                "\n".join(lines) + "\n", encoding="utf-8")
            counters.playlists += 1
        con.close()
    except sqlite3.Error as exc:
        print(f"note: playlist extraction failed ({exc}) -- songs are unaffected")


# ---------------------------------------------------------------- stage 6

def import_to_music(tracks: list[Track], dest: Path, counters: Counters,
                    manifest: dict):
    if not AUTO_ADD.exists():
        print(f"note: {AUTO_ADD} does not exist -- open Music.app once, then "
              f"copy {dest / 'Music'} into it manually.")
        return
    music_dir = dest / "Music"
    for track in tracks:
        if (track.status not in ("organized", "duplicate", "resumed")
                or track.is_drm or not track.final):
            continue
        entry = manifest.setdefault(
            track.afc_path, {"size": track.size, "final": str(track.final)})
        if entry.get("imported"):
            continue  # fed to Music in an earlier run; don't create dupes
        rel = track.final.relative_to(music_dir)
        target = AUTO_ADD / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(track.final, target)
            counters.imported += 1
        entry["imported"] = True


def report(tracks: list[Track], counters: Counters, dest: Path):
    print("\n" + "=" * 62)
    print("RECOVERY SUMMARY")
    print("=" * 62)
    print(f"  pulled from device:      {counters.pulled}")
    print(f"  already had (resumed):   {counters.resumed}")
    print(f"  duplicates skipped:      {counters.duplicates}")
    print(f"  named from tags:         "
          f"{sum(1 for t in tracks if t.status == 'organized')}")
    print(f"  fallback-named:          {counters.tag_fallbacks}")
    print(f"  DRM-protected set aside: {counters.drm}"
          + ("   (see 'DRM Protected' folder -- these old iTunes Store"
             " purchases only play under the Apple ID that bought them)"
             if counters.drm else ""))
    print(f"  playlists rebuilt:       {counters.playlists}")
    print(f"  copied into Music.app:   {counters.imported}")
    if counters.failed:
        print(f"  FAILED ({len(counters.failed)}):")
        for path, err in counters.failed:
            print(f"    {path}: {err}")
    print(f"\nYour permanent backup is in: {dest / 'Music'}")
    print("\nNEXT STEPS")
    print("  1. Open Music.app -- the songs will be added automatically.")
    print("     (If some linger in the 'Automatically Add' folder, quit and")
    print("     reopen Music. Rejected files end up in its 'Not Added' folder.)")
    if counters.playlists:
        print(f"  2. In Music: File > Library > Import Playlist... and pick the")
        print(f"     .m3u8 files in {dest / 'Playlists'}")
    print("  3. iPhone (no cable needed if Wi-Fi sync is already on):")
    print("     With the iPhone on the same Wi-Fi, look for it in the Finder")
    print("     sidebar. If it's there: click it > Music > Sync Music. If not,")
    print("     borrow any Lightning cable once, tick 'Show this iPhone when")
    print("     on Wi-Fi' in Finder > General, and it's wireless forever.")


# ---------------------------------------------------------------- main

def discover_staged(staging: Path) -> list[Track]:
    tracks = []
    if not staging.exists():
        return tracks
    for path in sorted(staging.rglob("*")):
        ext = path.suffix.lower()
        if path.is_file() and (ext in AUDIO_EXTS or ext in DRM_EXTS):
            tracks.append(Track(
                afc_path=str(path.relative_to(staging)),
                size=path.stat().st_size,
                is_drm=ext in DRM_EXTS,
                staged=path,
                status="resumed",
            ))
    return tracks


async def async_main(args) -> int:
    dest = Path(args.dest).expanduser()
    staging = dest / "_staging"
    counters = Counters()
    manifest = load_manifest(dest)
    db_path = dest / "_db" / Path(MEDIA_DB_AFC).name

    if args.offline:
        tracks = discover_staged(staging)
        if not tracks:
            print(f"Nothing staged in {staging} -- run without --offline first.")
            return 1
        print(f"Offline mode: organizing {len(tracks)} staged files.")
        db_path = db_path if db_path.exists() else None
    else:
        lockdown = await connect(args.udid, args.wifi)
        async with AfcService(lockdown) as afc:
            print("Scanning the device's music folders...")
            tracks, skipped_other = await inventory(afc)
            drm_count = sum(1 for t in tracks if t.is_drm)
            total_bytes = sum(t.size for t in tracks)
            print(f"Found {len(tracks)} audio files ({human(total_bytes)})"
                  + (f", of which {drm_count} are DRM-protected .m4p"
                     if drm_count else "")
                  + (f"; ignored {skipped_other} non-audio files"
                     if skipped_other else ""))
            db_path = await pull_media_db(afc, dest)

            if args.dry_run:
                db_meta = load_db_metadata(db_path)
                print(f"Media database: "
                      f"{'found, ' + str(len(db_meta)) + ' tracks listed' if db_meta else 'not usable'}")
                if db_path:
                    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                    introspect_playlist_tables(con.cursor())
                    con.close()
                print("\nDry run only -- nothing was copied. "
                      "Run again without --dry-run to recover.")
                return 0

            staging.mkdir(parents=True, exist_ok=True)
            try:
                await pull_all(afc, tracks, staging, args.limit, counters,
                               manifest)
            except ConnectionTerminatedError:
                print("\nConnection to the device was lost (cable unplugged?).")
                print("Run me again -- completed files are kept and skipped.")
                return 1

    db_meta = load_db_metadata(db_path if db_path and Path(db_path).exists() else None)
    print("Restoring names from tags and organizing...")
    organize(tracks, dest, db_meta, counters)
    save_manifest(dest, tracks, manifest)
    if args.playlists:
        extract_playlists(
            db_path if db_path and Path(db_path).exists() else None,
            db_meta, tracks, dest, counters)
    if not args.no_import:
        import_to_music(tracks, dest, counters, manifest)
        save_manifest(dest, tracks, manifest)
    report(tracks, counters, dest)
    return 0


def main():
    p = argparse.ArgumentParser(
        description="Recover iTunes-synced music from an iPad/iPhone.")
    p.add_argument("--dry-run", action="store_true",
                   help="connect and list what's on the device; copy nothing")
    p.add_argument("--dest", default=str(DEFAULT_DEST),
                   help=f"recovery folder (default: {DEFAULT_DEST})")
    p.add_argument("--udid", help="pick a device when several are connected")
    p.add_argument("--limit", type=int,
                   help="only pull the first N files (for testing)")
    p.add_argument("--no-import", action="store_true",
                   help="recover and organize, but don't feed Music.app")
    p.add_argument("--no-playlists", dest="playlists", action="store_false",
                   help="skip playlist recovery")
    p.add_argument("--offline", action="store_true",
                   help="re-run organize/playlists/import from already-staged "
                        "files, no device needed")
    p.add_argument("--wifi", action="store_true",
                   help="also accept devices connected over Wi-Fi (slower)")
    args = p.parse_args()
    try:
        sys.exit(asyncio.run(async_main(args)))
    except KeyboardInterrupt:
        print("\nInterrupted. Run me again -- completed files are kept "
              "and skipped.")
        sys.exit(130)


if __name__ == "__main__":
    main()
 