"""Provider plugin interface + the Candidate model + small HTTP helpers.

A provider is any module in finder/providers/ that exposes a module-level
`PROVIDER` object implementing the Provider protocol. The registry
(providers/__init__.py) auto-discovers them, so adding a source -- including
your own -- is just dropping a file here and listing its name in
config.PROVIDERS_ENABLED.

Kept lightweight (stdlib + requests) so importing a single adapter never pulls
in numpy / mutagen / device libraries.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

import requests

from .. import config

USER_AGENT = f"{config.APP_NAME}/{config.APP_VERSION} ( {config.CONTACT_EMAIL} )"

_session: requests.Session | None = None


def http_session() -> requests.Session:
    """Shared session with a polite User-Agent."""
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT})
        _session = s
    return _session


@dataclass
class Candidate:
    """One downloadable version of a recording, from one source.

    Fields default to "unknown" so a thin adapter can fill in only what its API
    exposes; scoring handles missing values gracefully.
    """
    provider: str
    source_id: str                       # stable id within the provider
    url: str                             # page or direct URL (provider-specific)
    title: str = ""
    artist: str = ""
    album: str | None = None
    codec: str | None = None            # advertised: flac|alac|mp3|aac|opus|...
    lossless_claimed: bool = False
    sample_rate: int | None = None
    bit_depth: int | None = None
    bitrate_kbps: int | None = None
    duration_s: float | None = None
    filesize: int | None = None
    source_trust: float = 0.5           # 0..1 (provider baseline * item signals)
    has_artwork: bool = False
    metadata_completeness: float = 0.0  # 0..1
    mbid: str | None = None
    artwork_url: str | None = None
    extra: dict = field(default_factory=dict)  # provider download hints

    def label(self) -> str:
        fmt = (self.codec or "?").upper()
        if self.lossless_claimed:
            depth = f"/{self.bit_depth}bit" if self.bit_depth else ""
            sr = f" {self.sample_rate/1000:.1f}kHz" if self.sample_rate else ""
            spec = f"{fmt}{depth}{sr}"
        else:
            spec = f"{fmt} {self.bitrate_kbps}kbps" if self.bitrate_kbps else fmt
        return f"{self.provider}: {spec}"


@runtime_checkable
class Provider(Protocol):
    name: str
    trust: float  # 0..1 baseline trust for this source

    def search(self, rec, query: str, limit: int) -> list[Candidate]:
        """Return candidate versions for `rec` (a RecordingMatch or None) /
        the raw `query`. Must never raise -- return [] on any failure."""
        ...

    def download(self, cand: Candidate, dest_dir: Path, ff: dict,
                 log: Callable[[str], None], cancel) -> Path | None:
        """Fetch `cand` into dest_dir, honoring `cancel` (a threading.Event or
        anything with .is_set()). Return the downloaded path, or None."""
        ...


class BaseProvider:
    """Convenience base with a cancel-aware streaming HTTP download. Adapters
    may subclass it or just implement the protocol directly."""
    name = "base"
    trust = 0.5

    def search(self, rec, query, limit):  # pragma: no cover - overridden
        return []

    def download(self, cand, dest_dir, ff, log, cancel):  # pragma: no cover
        return None

    def _http_download(self, url: str, dest_path: Path, log, cancel,
                       headers: dict | None = None) -> Path | None:
        return http_download(url, dest_path, log, cancel, headers=headers)


def _cancelled(cancel) -> bool:
    return bool(cancel is not None and getattr(cancel, "is_set", lambda: False)())


def http_download(url: str, dest_path: Path, log, cancel,
                  headers: dict | None = None, timeout: int = 30) -> Path | None:
    """Stream `url` to `dest_path` in chunks, aborting promptly if cancelled.

    Returns the path on success, None on failure/cancel (partial file removed).
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_path.with_suffix(dest_path.suffix + ".part")
    try:
        with http_session().get(url, stream=True, timeout=timeout,
                                headers=headers) as r:
            r.raise_for_status()
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if _cancelled(cancel):
                        log("  download cancelled")
                        fh.close()
                        tmp.unlink(missing_ok=True)
                        return None
                    if chunk:
                        fh.write(chunk)
        tmp.replace(dest_path)
        return dest_path
    except Exception as exc:  # provider APIs are flaky; never crash the fan-out
        log(f"  download failed ({type(exc).__name__}: {exc})")
        tmp.unlink(missing_ok=True)
        return None


def guess_codec_from_url(url: str) -> tuple[str | None, bool]:
    """(codec, lossless_claimed) from a file extension in the URL."""
    ext = Path(url.split("?")[0]).suffix.lower()
    lossless = {".flac": "flac", ".alac": "alac", ".wav": "wav",
                ".aiff": "wav", ".aif": "wav", ".ape": "ape", ".wv": "wv"}
    lossy = {".mp3": "mp3", ".m4a": "aac", ".aac": "aac", ".ogg": "vorbis",
             ".oga": "vorbis", ".opus": "opus", ".wma": "wma"}
    if ext in lossless:
        return lossless[ext], True
    if ext in lossy:
        return lossy[ext], False
    return None, False


# Small politeness throttle usable by any adapter hitting a rate-limited API.
_last_hit: dict[str, float] = {}


def throttle(key: str, min_interval: float) -> None:
    now = time.monotonic()
    prev = _last_hit.get(key, 0.0)
    wait = min_interval - (now - prev)
    if wait > 0:
        time.sleep(wait)
    _last_hit[key] = time.monotonic()
