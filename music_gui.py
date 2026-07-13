#!/usr/bin/env python3
"""Music Manager -- one window for recovering, importing, downloading and
exporting music.

Tabs:
  Devices  - see connected iPads/iPhones, recover their music into Music.app
  YouTube  - download best-quality audio from a YouTube link (or search) and
             add it to the Music library
  Import   - add local audio files; unsupported formats (FLAC/OGG/OPUS/...)
             are converted automatically so Music.app accepts them
  Export   - copy the library to any folder/drive, or push songs into the
             VLC app on a connected device

Run under the recovery venv:
    ~/.venvs/ipad-recovery/bin/python music_gui.py
"""

import asyncio
import contextlib
import io
import queue
import shutil
import subprocess
import sys
import threading
from argparse import Namespace
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import itunes  # noqa: E402  (reuses the whole recovery pipeline)

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    BaseTk = TkinterDnD.Tk
    HAS_DND = True
except Exception:
    BaseTk = tk.Tk
    HAS_DND = False

# ---- file-type policy ------------------------------------------------------
# Music.app accepts these as-is:
NATIVE = {".mp3", ".m4a", ".m4b", ".wav", ".aif", ".aiff"}
# lossless sources -> convert to ALAC (no quality loss):
TO_ALAC = {".flac", ".ape", ".wv"}
# lossy sources -> convert to AAC 256k:
TO_AAC = {".ogg", ".oga", ".opus", ".wma", ".aac"}
# video containers -> extract the audio track:
VIDEO = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".ts", ".m4v"}

YT_DEST = Path.home() / "Music" / "YouTube Music"
EXPORT_SRC = Path.home() / "Music" / "iPad Recovery" / "Music"
VLC_BUNDLE = "org.videolan.vlc-ios"

_FF: dict = {}


def get_ffmpeg(log=print) -> dict:
    """Locate ffmpeg/ffprobe; fall back to the bundled static build."""
    if _FF:
        return _FF
    ff, fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ff:
        log("Fetching a bundled ffmpeg (first time only, ~50 MB)...")
        from static_ffmpeg import run
        ff, fp = run.get_or_fetch_platform_executables_else_raise()
    _FF.update(ffmpeg=str(ff), ffprobe=str(fp) if fp else None)
    return _FF


def classify(path: Path) -> tuple[str, str]:
    """-> (plan, human description)"""
    ext = path.suffix.lower()
    if ext in NATIVE:
        return "native", "OK for Music.app"
    if ext in TO_ALAC:
        return "alac", "convert to ALAC (lossless)"
    if ext in TO_AAC:
        return "aac", "convert to AAC 256k"
    if ext in VIDEO:
        return "extract", "extract audio track"
    return "skip", "not an audio file -- skipped"


def convert(src: Path, out_dir: Path, plan: str, log) -> Path | None:
    """Convert/remux `src` into an .m4a Music.app accepts."""
    ff = get_ffmpeg(log)["ffmpeg"]
    fp = get_ffmpeg(log)["ffprobe"]
    out = out_dir / (src.stem + ".m4a")
    n = 2
    while out.exists():
        out = out_dir / f"{src.stem} ({n}).m4a"
        n += 1
    base = [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
    if plan == "alac":
        codec = ["-map", "0:a:0", "-c:a", "alac"]
    elif plan == "aac":
        codec = ["-map", "0:a:0", "-c:a", "aac", "-b:a", "256k"]
    else:  # extract from video: copy the track when it's already AAC/ALAC
        acodec = ""
        if fp:
            r = subprocess.run(
                [fp, "-v", "error", "-select_streams", "a:0", "-show_entries",
                 "stream=codec_name", "-of", "csv=p=0", str(src)],
                capture_output=True, text=True)
            acodec = r.stdout.strip()
        if acodec in ("aac", "alac"):
            codec = ["-vn", "-c:a", "copy"]
        else:
            codec = ["-vn", "-c:a", "aac", "-b:a", "256k"]
    cmd = base + codec + ["-map_metadata", "0", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log(f"  convert FAILED: {src.name}: {r.stderr.strip()[:200]}")
        return None
    return out


class QueueWriter(io.TextIOBase):
    """Redirects the CLI pipeline's print() output into the GUI log."""

    def __init__(self, q):
        self.q = q
        self._buf = ""

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.q.put(("log", line))
        return len(s)


class App(BaseTk):
    def __init__(self):
        super().__init__()
        self.title("Music Manager")
        self.geometry("980x680")
        self.minsize(820, 560)
        self.q: queue.Queue = queue.Queue()
        self.devices: list[dict] = []
        self._busy = False
        self.yt_cancel = threading.Event()
        self.find_cancel = threading.Event()

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        self.tab_dev = ttk.Frame(nb)
        self.tab_find = ttk.Frame(nb)
        self.tab_yt = ttk.Frame(nb)
        self.tab_imp = ttk.Frame(nb)
        self.tab_exp = ttk.Frame(nb)
        nb.add(self.tab_dev, text="  Devices  ")
        nb.add(self.tab_find, text="  Find  ")
        nb.add(self.tab_yt, text="  YouTube  ")
        nb.add(self.tab_imp, text="  Import  ")
        nb.add(self.tab_exp, text="  Export  ")
        self._build_devices()
        self._build_finder()
        self._build_youtube()
        self._build_import()
        self._build_export()

        logf = ttk.LabelFrame(self, text="Activity")
        logf.pack(fill="both", expand=False, padx=8, pady=8)
        self.logbox = tk.Text(logf, height=10, state="disabled", wrap="word",
                              font=("Menlo", 11))
        sb = ttk.Scrollbar(logf, command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=sb.set)
        self.logbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.status = ttk.Label(self, text="Ready", anchor="w")
        self.status.pack(fill="x", padx=10, pady=(0, 6))

        self.after(120, self._drain)
        self.after(300, self.refresh_devices)

    # ---- plumbing ----------------------------------------------------------
    def log(self, msg: str):
        self.q.put(("log", str(msg)))

    def _drain(self):
        # this loop must survive anything -- if it dies, the whole UI freezes
        try:
            while True:
                kind, payload = self.q.get_nowait()
                try:
                    if kind == "log":
                        self.logbox.configure(state="normal")
                        self.logbox.insert("end", payload + "\n")
                        self.logbox.see("end")
                        self.logbox.configure(state="disabled")
                    elif kind == "status":
                        self.status.configure(text=payload)
                    elif kind == "devices":
                        self._show_devices(payload)
                    elif kind == "yt_choose":
                        self._yt_choose(payload)
                    elif kind == "yt_start":
                        self._yt_start(payload["url"], payload["extra"])
                    elif kind == "find_versions":
                        self._show_versions(payload)
                except Exception as e:
                    print(f"ui event {kind} failed: {e}", file=sys.__stderr__)
        except queue.Empty:
            pass
        finally:
            self.after(120, self._drain)

    def run_bg(self, label: str, fn):
        if self._busy:
            self.log("Something is already running -- wait for it to finish.")
            return

        def worker():
            self._busy = True
            self.q.put(("status", label + " ..."))
            try:
                fn()
                self.q.put(("status", label + " -- done"))
            except SystemExit as e:  # itunes.py uses sys.exit(msg)
                for ln in str(e.code or "").splitlines():
                    self.q.put(("log", ln))
                self.q.put(("status", label + " -- stopped"))
            except Exception as e:
                self.q.put(("log", f"ERROR: {type(e).__name__}: {e}"))
                self.q.put(("status", label + " -- failed"))
            finally:
                self._busy = False

        threading.Thread(target=worker, daemon=True).start()

    # ---- Devices tab -------------------------------------------------------
    def _build_devices(self):
        f = self.tab_dev
        top = ttk.Frame(f)
        top.pack(fill="x", pady=6, padx=6)
        ttk.Button(top, text="Refresh", command=self.refresh_devices).pack(
            side="left")
        ttk.Label(top, text="Plug a device in with a USB cable, unlock it, "
                            "tap Trust if asked.").pack(side="left", padx=10)
        cols = ("name", "model", "ios", "conn")
        self.devtree = ttk.Treeview(f, columns=cols, show="headings", height=5)
        for c, w in zip(cols, (260, 150, 90, 90)):
            self.devtree.heading(c, text=c.title())
            self.devtree.column(c, width=w)
        self.devtree.pack(fill="x", padx=6, pady=4)
        btns = ttk.Frame(f)
        btns.pack(fill="x", padx=6, pady=6)
        ttk.Button(btns, text="Recover music from device → Music.app",
                   command=self.recover_device).pack(side="left")
        ttk.Button(btns, text="Send library to VLC on device",
                   command=self.send_to_vlc).pack(side="left", padx=8)
        ttk.Label(f, justify="left", foreground="gray", text=(
            "Recover pulls every song off the device, restores names, rebuilds "
            "playlists and adds\neverything to Music.app. Safe to run twice -- "
            "it never duplicates. Uses ~/Music/iPad Recovery as its backup home."
        )).pack(anchor="w", padx=8, pady=4)

    def refresh_devices(self):
        def work():
            async def _go():
                out = []
                devs = await itunes.usbmux.list_devices()
                # a device can show up twice (USB + Wi-Fi) -- keep one entry,
                # preferring the USB connection
                best = {}
                for d in devs:
                    if d.serial not in best or d.connection_type == "USB":
                        best[d.serial] = d
                devs = list(best.values())
                for d in devs:
                    try:
                        ld = await itunes.create_using_usbmux(serial=d.serial)
                        info = await itunes.maybe_await(ld.get_value()) or {}
                        out.append({
                            "udid": d.serial,
                            "name": info.get("DeviceName", "?"),
                            "model": info.get("ProductType", "?"),
                            "ios": info.get("ProductVersion", "?"),
                            "conn": d.connection_type,
                        })
                        with contextlib.suppress(Exception):
                            await itunes.maybe_await(ld.aclose())
                    except Exception as e:
                        out.append({"udid": d.serial, "name": f"(locked? {e})",
                                    "model": "?", "ios": "?",
                                    "conn": d.connection_type})
                return out

            devices = asyncio.run(_go())
            self.devices = devices
            self.q.put(("devices", devices))
            self.q.put(("log", f"Found {len(devices)} device(s)."))

        self.run_bg("Scanning for devices", work)

    def _show_devices(self, devices):
        self.devtree.delete(*self.devtree.get_children())
        shown = set()
        for d in devices:
            if d["udid"] in shown:
                continue
            shown.add(d["udid"])
            self.devtree.insert("", "end", iid=d["udid"], values=(
                d["name"], d["model"], d["ios"], d["conn"]))
        if shown:
            self.devtree.selection_set(devices[0]["udid"])

    def _selected_udid(self):
        sel = self.devtree.selection()
        if not sel:
            messagebox.showinfo("No device", "Refresh and select a device first.")
            return None
        return sel[0]

    def recover_device(self):
        udid = self._selected_udid()
        if not udid:
            return

        def work():
            ns = Namespace(dry_run=False, dest=str(itunes.DEFAULT_DEST),
                           udid=udid, limit=None, no_import=False,
                           playlists=True, offline=False, wifi=True)
            with contextlib.redirect_stdout(QueueWriter(self.q)):
                asyncio.run(itunes.async_main(ns))
            self.q.put(("log", "Open Music.app -- new songs are added "
                               "automatically."))

        self.run_bg("Recovering music", work)

    def send_to_vlc(self):
        udid = self._selected_udid()
        if not udid:
            return
        src = Path(self.exp_src.get()).expanduser()
        if not src.exists():
            self.log(f"Source folder not found: {src}")
            return

        def work():
            from pymobiledevice3.services.house_arrest import HouseArrestService
            files = [p for p in sorted(src.rglob("*"))
                     if p.is_file() and p.suffix.lower() in
                     (NATIVE | {".m4p"})]
            if not files:
                self.q.put(("log", f"No audio files under {src}"))
                return

            async def _go():
                ld = await itunes.create_using_usbmux(serial=udid)
                try:
                    ha = await HouseArrestService.create(
                        ld, VLC_BUNDLE, documents_only=True)
                except Exception as e:
                    self.q.put(("log",
                                "Could not open VLC on the device -- is the "
                                "free VLC app installed there? "
                                f"({type(e).__name__}: {e})"))
                    return
                async with ha:
                    sent = 0
                    for i, p in enumerate(files, 1):
                        rel = p.relative_to(src)
                        flat = " - ".join(rel.parts)[:180]
                        try:
                            await ha.set_file_contents(flat, p.read_bytes())
                            sent += 1
                            if i % 10 == 0 or i == len(files):
                                self.q.put(
                                    ("log", f"  sent {i}/{len(files)}"))
                        except Exception as e:
                            self.q.put(("log", f"  failed {flat}: {e}"))
                    self.q.put(("log",
                                f"Done -- {sent} songs now inside the VLC app "
                                "on the device (VLC > Audio)."))

            asyncio.run(_go())

        self.run_bg("Sending to VLC", work)

    # ---- Find tab ----------------------------------------------------------
    def _build_finder(self):
        f = self.tab_find
        row = ttk.Frame(f)
        row.pack(fill="x", padx=8, pady=(10, 4))
        ttk.Label(row, text="Song (Artist - Title):").pack(side="left")
        self.find_q = ttk.Entry(row)
        self.find_q.pack(side="left", fill="x", expand=True, padx=6)
        self.find_q.bind("<Return>", lambda e: self.find_get_best())
        ttk.Button(row, text="Search", command=self.find_search).pack(side="left")

        row2 = ttk.Frame(f)
        row2.pack(fill="x", padx=8, pady=4)
        self.find_lossless_only = tk.BooleanVar(value=False)
        self.find_add = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="Lossless only",
                        variable=self.find_lossless_only).pack(side="left")
        ttk.Checkbutton(row2, text="Also add to Music.app library",
                        variable=self.find_add).pack(side="left", padx=10)
        ttk.Button(row2, text="⬇ Get the best (genuine lossless)",
                   command=self.find_get_best).pack(side="left", padx=12)
        ttk.Button(row2, text="Stop", command=lambda: (
            self.find_cancel.set(),
            self.q.put(("log", "Stopping after the current step...")))).pack(
            side="left")

        cols = ("provider", "format", "detail", "score")
        self.findtree = ttk.Treeview(f, columns=cols, show="headings", height=8)
        for c, w in zip(cols, (120, 130, 360, 70)):
            self.findtree.heading(c, text=c.title())
            self.findtree.column(c, width=w)
        self.findtree.pack(fill="both", expand=True, padx=8, pady=4)

        ttk.Label(f, justify="left", foreground="gray", text=(
            "Type a song, Search to see matching versions from different legal "
            "sources (Internet Archive,\nWikimedia, ccMixter, YouTube ...), then "
            "Get the best: it identifies the recording, avoids fake\nlossless, "
            "verifies the real audio quality, fixes tags + artwork, skips "
            "duplicates, and adds it\nto Music. For personal use only. (First run "
            "may fetch a bundled ffmpeg, ~50 MB.)"
        )).pack(anchor="w", padx=10, pady=8)

    def _finder_opts(self):
        from finder.finder import FinderOptions
        return FinderOptions(
            want_lossless=True,
            lossless_only=self.find_lossless_only.get(),
            add_to_library=self.find_add.get())

    def _version_rows(self, ranked):
        rows = []
        for c in ranked[:15]:
            fmt = (c.codec or "?").upper() + (" lossless" if c.lossless_claimed else "")
            rows.append((c.provider, fmt, c.label(),
                         f"{c.extra.get('prescore', 0):.2f}"))
        return rows

    def _show_versions(self, rows):
        self.findtree.delete(*self.findtree.get_children())
        for r in rows:
            self.findtree.insert("", "end", values=r)

    def find_search(self):
        q = self.find_q.get().strip()
        if not q:
            messagebox.showinfo("Find", "Type a song name first.")
            return

        def work():
            from finder import identify
            from finder import finder as fmod
            self.q.put(("log", f"Searching for: {q}"))
            recs = identify.search_recordings(q)
            rec = recs[0] if recs else None
            if rec:
                self.q.put(("log", f"Best recording match: {rec.artist} - "
                            f"{rec.title}"
                            + (f"  [{rec.mbid}]" if rec.mbid else "  (unverified)")))
            ranked = fmod.search_versions(rec, q, self._finder_opts(),
                                          lambda m: self.q.put(("log", m)))
            self.q.put(("find_versions", self._version_rows(ranked)))
            self.q.put(("log", f"Found {len(ranked)} version(s). Click "
                               "'Get the best' to download."))

        self.run_bg("Searching sources", work)

    def find_get_best(self):
        q = self.find_q.get().strip()
        if not q:
            messagebox.showinfo("Find", "Type a song name first.")
            return
        self.find_cancel.clear()

        def work():
            from finder import finder as fmod

            def cc(ranked):
                self.q.put(("find_versions", self._version_rows(ranked)))
                return ranked   # show the list, then proceed automatically

            res = fmod.run_finder(q, self._finder_opts(),
                                  log=lambda m: self.q.put(("log", m)),
                                  cancel=self.find_cancel, choose_candidates=cc)
            self.q.put(("log", f"== {res.status.upper()}: {res.message} =="))
            if res.status == "added" and self.find_add.get():
                self.q.put(("log", "Open Music.app -- the song is being added "
                                   "automatically."))

        self.run_bg("Finding best version", work)

    # ---- YouTube tab -------------------------------------------------------
    def _build_youtube(self):
        f = self.tab_yt
        row = ttk.Frame(f)
        row.pack(fill="x", padx=8, pady=(10, 4))
        ttk.Label(row, text="Link or search:").pack(side="left")
        self.yt_url = ttk.Entry(row)
        self.yt_url.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row, text="Paste", command=lambda: (
            self.yt_url.delete(0, "end"),
            self.yt_url.insert(0, self.clipboard_get()))).pack(side="left")
        row2 = ttk.Frame(f)
        row2.pack(fill="x", padx=8, pady=4)
        ttk.Label(row2, text="Save to:").pack(side="left")
        self.yt_dest = ttk.Entry(row2)
        self.yt_dest.insert(0, str(YT_DEST))
        self.yt_dest.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row2, text="Browse", command=lambda: self._pick_dir(
            self.yt_dest)).pack(side="left")
        row3 = ttk.Frame(f)
        row3.pack(fill="x", padx=8, pady=4)
        self.yt_add = tk.BooleanVar(value=True)
        ttk.Checkbutton(row3, text="Also add to Music.app library",
                        variable=self.yt_add).pack(side="left")
        ttk.Button(row3, text="Download", command=self.yt_download).pack(
            side="left", padx=12)
        ttk.Button(row3, text="Stop", command=lambda: (
            self.yt_cancel.set(),
            self.q.put(("log", "Stopping after the current file...")))).pack(
            side="left")
        ttk.Label(f, justify="left", foreground="gray", text=(
            "Takes video links, playlist links, or plain text (searches "
            "YouTube and grabs the top hit).\nDownloads the highest-quality "
            "audio stream, converts to Music-compatible m4a, keeps the\ntitle/"
            "artist metadata and embeds the cover art. For personal use only."
        )).pack(anchor="w", padx=10, pady=8)

    def yt_download(self):
        url = self.yt_url.get().strip()
        if not url:
            messagebox.showinfo("YouTube", "Paste a link or type a search first.")
            return
        if not url.lower().startswith(("http://", "https://")):
            self._yt_start(f"ytsearch1:{url}", {})
            return

        def probe():
            import yt_dlp

            class Quiet:
                def debug(self, m):
                    pass
                info = warning = error = debug

            opts = {"quiet": True, "no_warnings": True,
                    "extract_flat": "in_playlist", "playlistend": 100,
                    "logger": Quiet()}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info.get("_type") == "playlist":
                entries = []
                for i, e in enumerate(info.get("entries") or [], 1):
                    if e:
                        entries.append(
                            {"i": i, "title": e.get("title") or e.get("id", "?")})
                self.q.put(("yt_choose", {
                    "url": url, "title": info.get("title") or "playlist",
                    "entries": entries, "has_single": "v=" in url}))
            else:
                self.q.put(("yt_start", {"url": url, "extra": {}}))

        self.run_bg("Checking the link", probe)

    def _yt_choose(self, data):
        """The link is a playlist -- ask which songs to take."""
        win = tk.Toplevel(self)
        win.title("Playlist detected")
        win.geometry("620x460")
        ttk.Label(win, justify="left", text=(
            f"This link contains a playlist — “{data['title']}” "
            f"with {len(data['entries'])} videos.\n"
            "Pick what to download (Cmd-click / Shift-click for several):")
        ).pack(anchor="w", padx=12, pady=10)
        lb = tk.Listbox(win, selectmode="extended", font=("Menlo", 11))
        for e in data["entries"]:
            lb.insert("end", f"{e['i']:>3}.  {e['title']}")
        lb.pack(fill="both", expand=True, padx=12)
        if data["entries"]:
            lb.selection_set(0)

        def start(mode):
            if mode == "selected":
                picked = [data["entries"][int(i)]["i"] for i in lb.curselection()]
                if not picked:
                    return
                extra = {"playlist_items": ",".join(map(str, picked))}
            elif mode == "single":
                extra = {"noplaylist": True}
            else:
                extra = {}
            win.destroy()
            self._yt_start(data["url"], extra)

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=12, pady=10)
        if data.get("has_single"):
            ttk.Button(btns, text="Just this one video",
                       command=lambda: start("single")).pack(side="left")
        ttk.Button(btns, text="Download selected",
                   command=lambda: start("selected")).pack(side="left", padx=6)
        ttk.Button(btns, text=f"All {len(data['entries'])}",
                   command=lambda: start("all")).pack(side="left", padx=6)
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")
        win.transient(self)
        win.grab_set()

    def _yt_start(self, url: str, extra: dict):
        dest = Path(self.yt_dest.get()).expanduser()
        dest.mkdir(parents=True, exist_ok=True)
        add = self.yt_add.get()
        self.yt_cancel.clear()

        def work():
            import time
            import yt_dlp
            from yt_dlp.utils import DownloadCancelled
            ff = get_ffmpeg(lambda m: self.q.put(("log", m)))
            start_ts = time.time()
            last = {"pct": -10, "name": ""}

            def hook(d):
                if self.yt_cancel.is_set():
                    raise DownloadCancelled("stopped by user")
                meta = d.get("info_dict") or {}
                name = meta.get("title") or Path(d.get("filename", "?")).stem
                if d["status"] == "downloading":
                    if name != last["name"]:
                        last["name"], last["pct"] = name, -10
                        idx, n = meta.get("playlist_index"), meta.get("n_entries")
                        tag = f"[{idx}/{n}] " if idx and n else ""
                        self.q.put(("log", f"{tag}downloading: {name}"))
                    total = d.get("total_bytes") or d.get(
                        "total_bytes_estimate") or 0
                    if total:
                        pct = int(d.get("downloaded_bytes", 0) * 100 / total)
                        if pct >= last["pct"] + 10:
                            last["pct"] = pct
                            self.q.put(
                                ("status", f"Downloading {name[:40]} {pct}%"))
                elif d["status"] == "finished":
                    self.q.put(("log", f"  converting: {name}"))

            class Logger:
                def debug(self, m):
                    pass
                info = warning = debug

                def error(inner, m):
                    self.q.put(("log", f"  {m}"))

            opts = {
                "format": "bestaudio/best",
                "outtmpl": str(dest / "%(title)s.%(ext)s"),
                "ffmpeg_location": ff["ffmpeg"],
                "postprocessors": [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "m4a",
                     "preferredquality": "0"},
                    {"key": "FFmpegMetadata"},
                    {"key": "EmbedThumbnail"},
                ],
                "writethumbnail": True,
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "logger": Logger(),
                "progress_hooks": [hook],
                "ignoreerrors": "only_download",
                **extra,
            }
            cancelled, info = False, None
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
            except DownloadCancelled:
                cancelled = True
            files = []
            if info:
                for e in (info.get("entries") or [info]):
                    if not e:
                        continue
                    for rd in e.get("requested_downloads") or []:
                        fp = rd.get("filepath")
                        if fp and Path(fp).exists():
                            files.append(Path(fp))
            if not files:  # e.g. stopped mid-playlist: keep what finished
                files = [p for p in dest.glob("*.m4a")
                         if p.stat().st_mtime >= start_ts]
            for p in files:
                self.q.put(("log", f"Saved: {p.name}"))
            if cancelled:
                self.q.put(("log",
                            f"Stopped. {len(files)} finished song(s) kept in "
                            f"{dest} -- NOT added to Music. Use the Import "
                            "tab if you want any of them."))
                return
            if add and files:
                if itunes.AUTO_ADD.exists():
                    for p in files:
                        shutil.copy2(p, itunes.AUTO_ADD / p.name)
                    self.q.put(("log",
                                f"Added {len(files)} song(s) to Music.app -- "
                                "open Music to see them."))
                else:
                    self.q.put(("log", "Music auto-add folder missing -- "
                                       "files kept in the save folder only."))
            if not files:
                self.q.put(("log", "Nothing downloaded -- check the link."))

        self.run_bg("Downloading from YouTube", work)

    # ---- Import tab --------------------------------------------------------
    def _build_import(self):
        f = self.tab_imp
        top = ttk.Frame(f)
        top.pack(fill="x", padx=6, pady=6)
        ttk.Button(top, text="Add files...", command=self.imp_add_files).pack(
            side="left")
        ttk.Button(top, text="Add folder...", command=self.imp_add_folder).pack(
            side="left", padx=6)
        ttk.Button(top, text="Clear list", command=lambda: self.imptree.delete(
            *self.imptree.get_children())).pack(side="left", padx=6)
        ttk.Button(top, text="Import into Music.app",
                   command=self.imp_run).pack(side="right")
        cols = ("file", "plan")
        self.imptree = ttk.Treeview(f, columns=cols, show="headings", height=9)
        self.imptree.heading("file", text="File")
        self.imptree.heading("plan", text="What will happen")
        self.imptree.column("file", width=520)
        self.imptree.column("plan", width=260)
        self.imptree.pack(fill="both", expand=True, padx=6, pady=4)
        hint = ("Drag files here or use the buttons. " if HAS_DND
                else "Use the buttons to pick files. ")
        ttk.Label(f, foreground="gray", text=(
            hint + "MP3/M4A/WAV/AIFF go straight in; FLAC/APE/WV become "
            "lossless ALAC;\nOGG/OPUS/WMA become AAC 256k; video files get "
            "their audio track extracted.")).pack(anchor="w", padx=8, pady=4)
        if HAS_DND:
            self.imptree.drop_target_register(DND_FILES)
            self.imptree.dnd_bind("<<Drop>>", self._imp_drop)

    def _imp_drop(self, event):
        for raw in self.tk.splitlist(event.data):
            self._imp_add_path(Path(raw))

    def _imp_add_path(self, p: Path):
        if p.is_dir():
            for sub in sorted(p.rglob("*")):
                if sub.is_file():
                    self._imp_add_path(sub)
            return
        plan, desc = classify(p)
        if plan == "skip" and p.suffix.lower() not in (
                NATIVE | TO_ALAC | TO_AAC | VIDEO):
            if p.name.startswith("."):
                return
        self.imptree.insert("", "end", values=(str(p), desc))

    def imp_add_files(self):
        for name in filedialog.askopenfilenames(title="Choose audio files"):
            self._imp_add_path(Path(name))

    def imp_add_folder(self):
        d = filedialog.askdirectory(title="Choose a folder")
        if d:
            self._imp_add_path(Path(d))

    def imp_run(self):
        items = [self.imptree.item(i)["values"][0]
                 for i in self.imptree.get_children()]
        if not items:
            messagebox.showinfo("Import", "Add some files first.")
            return
        if not itunes.AUTO_ADD.exists():
            messagebox.showerror("Import", "Music.app auto-add folder not "
                                 "found. Open Music once, then retry.")
            return

        def work():
            conv_dir = Path.home() / "Music" / "Converted"
            done = skipped = failed = 0
            for raw in items:
                p = Path(raw)
                if not p.exists():
                    continue
                plan, _ = classify(p)
                if plan == "skip":
                    skipped += 1
                    continue
                if plan == "native":
                    shutil.copy2(p, itunes.AUTO_ADD / p.name)
                    done += 1
                    self.q.put(("log", f"added: {p.name}"))
                else:
                    conv_dir.mkdir(parents=True, exist_ok=True)
                    out = convert(p, conv_dir,
                                  plan if plan != "extract" else "extract",
                                  lambda m: self.q.put(("log", m)))
                    if out:
                        shutil.copy2(out, itunes.AUTO_ADD / out.name)
                        done += 1
                        self.q.put(("log", f"converted + added: {p.name}"))
                    else:
                        failed += 1
            self.q.put(("log",
                        f"Import finished: {done} added, {skipped} skipped, "
                        f"{failed} failed. Open Music.app to see them. "
                        f"(Converted copies kept in {conv_dir})"))

        self.run_bg("Importing", work)

    # ---- Export tab --------------------------------------------------------
    def _build_export(self):
        f = self.tab_exp
        row = ttk.Frame(f)
        row.pack(fill="x", padx=8, pady=(10, 4))
        ttk.Label(row, text="Export from:").pack(side="left")
        self.exp_src = ttk.Entry(row)
        self.exp_src.insert(0, str(EXPORT_SRC))
        self.exp_src.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row, text="Browse", command=lambda: self._pick_dir(
            self.exp_src)).pack(side="left")
        btns = ttk.Frame(f)
        btns.pack(fill="x", padx=8, pady=8)
        ttk.Button(btns, text="Copy to folder / USB drive...",
                   command=self.exp_folder).pack(side="left")
        ttk.Button(btns, text="Send to VLC on connected device",
                   command=self.send_to_vlc).pack(side="left", padx=8)
        ttk.Button(btns, text="Get songs into an iPhone's Music app?",
                   command=self.exp_help).pack(side="left", padx=8)
        ttk.Label(f, justify="left", foreground="gray", text=(
            "Export from any folder of music -- the default is the iPad "
            "recovery backup.\nVLC export needs the free VLC app on the device "
            "and a USB connection."))\
            .pack(anchor="w", padx=10, pady=6)

    def exp_folder(self):
        src = Path(self.exp_src.get()).expanduser()
        if not src.exists():
            self.log(f"Source folder not found: {src}")
            return
        d = filedialog.askdirectory(title="Copy the music where?")
        if not d:
            return
        target = Path(d) / src.name

        def work():
            shutil.copytree(src, target, dirs_exist_ok=True)
            n = sum(1 for p in target.rglob("*") if p.is_file())
            self.q.put(("log", f"Copied library to {target} ({n} files)."))

        self.run_bg("Exporting to folder", work)

    def exp_help(self):
        messagebox.showinfo(
            "iPhone Music app",
            "Apple only lets songs into the iPhone's built-in Music app two "
            "ways:\n\n"
            "1. FREE, at home: Home Sharing. Mac: Music > File > Home Sharing "
            "> Turn On. iPhone: Settings > Music > Home Sharing (same Apple "
            "ID). Then Music app > Library > Home Sharing -- streams your "
            "whole Mac library over Wi-Fi.\n\n"
            "2. Full offline copy: connect the iPhone to this Mac with a "
            "cable ONCE, tick 'Show this iPhone when on Wi-Fi' in Finder, "
            "and sync Music. After that one time, syncing is wireless "
            "forever.\n\n"
            "No cable and away from home? Use 'Send to VLC' instead -- songs "
            "play offline in the free VLC app.")

    # ---- shared ------------------------------------------------------------
    def _pick_dir(self, entry: ttk.Entry):
        d = filedialog.askdirectory()
        if d:
            entry.delete(0, "end")
            entry.insert(0, d)


def main():
    app = App()
    app.log("Welcome! Tabs: recover from Devices, download from YouTube, "
            "Import local files, Export anywhere.")
    if "--smoke-test" in sys.argv:
        app.after(1500, app.destroy)
    app.mainloop()


if __name__ == "__main__":
    main()
