"""Write canonical metadata + embedded cover art with mutagen.

This is the app's first tag *writing* (the recovery pipeline only ever reads
tags). Writes are confined to the freshly downloaded copy, before it enters the
library -- existing library files are never modified here.
"""

from pathlib import Path

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def write_tags(path, rec, art_bytes: bytes | None = None, log=print) -> bool:
    """Write title/artist/album/MBID from `rec` and embed `art_bytes`."""
    path = Path(path)
    ok = _write_text_tags(path, rec, log)
    if art_bytes:
        _embed_art(path, art_bytes, log)
    return ok


def _write_text_tags(path: Path, rec, log) -> bool:
    import mutagen
    try:
        f = mutagen.File(path, easy=True)
        if f is None:
            return False

        def setk(key, value):
            if value:
                try:
                    f[key] = str(value)
                except Exception:
                    pass

        setk("title", getattr(rec, "title", ""))
        setk("artist", getattr(rec, "artist", ""))
        setk("albumartist", getattr(rec, "artist", ""))
        setk("album", getattr(rec, "album", None))
        setk("musicbrainz_trackid", getattr(rec, "mbid", None))
        f.save()
        return True
    except Exception as exc:
        log(f"  tag write failed: {type(exc).__name__}: {exc}")
        return False


def _embed_art(path: Path, data: bytes, log) -> bool:
    ext = path.suffix.lower()
    is_png = data[:8] == PNG_MAGIC
    mime = "image/png" if is_png else "image/jpeg"
    try:
        if ext in (".m4a", ".mp4", ".m4b", ".aac"):
            from mutagen.mp4 import MP4, MP4Cover
            au = MP4(path)
            fmt = MP4Cover.FORMAT_PNG if is_png else MP4Cover.FORMAT_JPEG
            au["covr"] = [MP4Cover(data, imageformat=fmt)]
            au.save()
        elif ext == ".mp3":
            from mutagen.id3 import APIC, ID3
            from mutagen.id3._util import ID3NoHeaderError
            try:
                au = ID3(path)
            except ID3NoHeaderError:
                au = ID3()
            au.delall("APIC")
            au.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
            au.save(path)
        elif ext == ".flac":
            from mutagen.flac import FLAC, Picture
            au = FLAC(path)
            pic = Picture()
            pic.type = 3          # front cover
            pic.mime = mime
            pic.data = data
            au.clear_pictures()
            au.add_picture(pic)
            au.save()
        else:
            return False
        return True
    except Exception as exc:
        log(f"  artwork embed failed: {type(exc).__name__}: {exc}")
        return False
