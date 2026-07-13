"""Command-line entry: python -m finder "Artist - Title" [options]

Mirrors itunes.py's argparse style. Synchronous (providers are sync HTTP /
yt-dlp). For personal use only.
"""

import argparse
import sys
from pathlib import Path

from . import config
from .finder import FinderOptions, run_finder


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m finder",
        description="Find the best genuine-lossless version of a song from "
                    "legitimate sources and add it to your Music library.")
    p.add_argument("query", help='song name, e.g. "Miles Davis - So What"')
    p.add_argument("--lossless-only", action="store_true",
                   help="fail instead of falling back to a lossy copy")
    p.add_argument("--max-downloads", type=int, default=config.MAX_DOWNLOADS,
                   help=f"try at most N candidates (default {config.MAX_DOWNLOADS})")
    p.add_argument("--pick", type=int, metavar="N",
                   help="use the Nth recording match (1-based) instead of the top")
    p.add_argument("--providers",
                   help="comma-separated subset, e.g. internetarchive,ccmixter,wikimedia")
    p.add_argument("--dest", default=str(config.FINDER_DEST),
                   help=f"output folder (default {config.FINDER_DEST})")
    p.add_argument("--no-add", action="store_true",
                   help="organize + tag but don't copy into Music.app")
    p.add_argument("--replace-lower", action="store_true",
                   help="replace an existing lower-quality copy (default: keep both)")
    p.add_argument("--fingerprint", action="store_true",
                   help="fingerprint files during dedup indexing (needs fpcalc)")
    a = p.parse_args(argv)

    provs = [s.strip() for s in a.providers.split(",")] if a.providers else None
    opts = FinderOptions(
        providers=provs, max_downloads=a.max_downloads,
        want_lossless=True, lossless_only=a.lossless_only,
        replace_lower_quality=a.replace_lower,
        dest=Path(a.dest).expanduser(), add_to_library=not a.no_add,
        fingerprint_index=a.fingerprint)

    choose = None
    if a.pick:
        def choose(recs):
            i = a.pick - 1
            return recs[i] if 0 <= i < len(recs) else recs[0]

    res = run_finder(a.query, opts, log=print, choose_recording=choose)
    print("\n" + "=" * 60)
    print(f"RESULT: {res.status} -- {res.message}")
    if res.tried:
        print("versions tried:")
        for label, reason in res.tried:
            print(f"  - {label}: {reason}")
    print("=" * 60)
    return 0 if res.status in ("added", "replaced", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
