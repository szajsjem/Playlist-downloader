#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from mutagen import File as MutagenFile
from mutagen.mp4 import MP4

AUDIO_EXTS_DEFAULT = [
    ".m4a",
    ".mp3",
    ".flac",
    ".ogg",
    ".opus",
    ".aac",
    ".wav",
    ".m4b",
]


@dataclass
class Track:
    path: str
    artist: Optional[str]
    title: Optional[str]
    album: Optional[str]
    duration_s: Optional[float]
    bitrate_bps: Optional[int]
    size_bytes: int


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def norm(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    s2 = re.sub(r"\s+", " ", str(s)).strip()
    return s2.lower() if s2 else None


def strip_yt_id_suffix(name: str) -> str:
    return re.sub(r"\s*\[[A-Za-z0-9_-]{11}\]\s*$", "", name).strip()


def loose_title(t: str) -> str:
    t2 = t
    t2 = re.sub(r"\s*\((lyrics?|official.*?|audio)\)\s*$", "", t2, flags=re.I)
    t2 = re.sub(
        r"\s*\[(lyrics?|sped up|slowed|nightcore|edit|remix)\]\s*$",
        "",
        t2,
        flags=re.I,
    )
    t2 = re.sub(r"\s+", " ", t2).strip()
    return t2


def sanitize_folder_name(s: str) -> str:
    s2 = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    s2 = re.sub(r"\s+", " ", s2).strip()
    return s2 or "Unknown"


def parse_artist_title_from_filename(path: str) -> Tuple[Optional[str], Optional[str]]:
    base = os.path.splitext(os.path.basename(path))[0]
    base = strip_yt_id_suffix(base)
    if " - " in base:
        left, right = base.split(" - ", 1)
        left = left.strip()
        right = right.strip()
        if left.lower() in ("nightcore", "nightcore lyrics", "nightcore music"):
            return None, right or None
        return left or None, right or None
    return None, None


def read_tags(path: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    ext = os.path.splitext(path)[1].lower()

    if ext in (".m4a", ".mp4", ".m4b"):
        try:
            mp4 = MP4(path)
            tags = mp4.tags or {}

            def first(key: str) -> Optional[str]:
                v = tags.get(key)
                if not v:
                    return None
                return str(v[0]).strip() or None

            title = first("\xa9nam")
            artist = first("\xa9ART")
            album = first("\xa9alb")
            return artist, title, album
        except Exception:
            pass

    try:
        f = MutagenFile(path, easy=True)
        if not f or not f.tags:
            raise RuntimeError("no tags")

        def first_easy(key: str) -> Optional[str]:
            v = f.tags.get(key)
            if not v:
                return None
            return str(v[0]).strip() or None

        artist = first_easy("artist")
        title = first_easy("title")
        album = first_easy("album")
        return artist, title, album
    except Exception:
        return None, None, None


def read_duration_bitrate_mutagen(path: str) -> Tuple[Optional[float], Optional[int]]:
    try:
        f = MutagenFile(path)
        if not f or not getattr(f, "info", None):
            return None, None
        dur = getattr(f.info, "length", None)
        br = getattr(f.info, "bitrate", None)
        dur_f = float(dur) if isinstance(dur, (int, float)) else None
        br_i = int(br) if isinstance(br, int) else None
        return dur_f, br_i
    except Exception:
        return None, None


def ffprobe_duration_bitrate(path: str) -> Tuple[Optional[float], Optional[int]]:
    try:
        p = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration,bit_rate",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        lines = [ln.strip() for ln in (p.stdout or "").splitlines() if ln.strip()]
        dur = float(lines[0]) if len(lines) >= 1 else None
        br = int(float(lines[1])) if len(lines) >= 2 else None
        return dur, br
    except Exception:
        return None, None


def get_track(path: str, use_ffprobe: bool) -> Track:
    size = os.path.getsize(path)
    artist, title, album = read_tags(path)

    if not artist or not title:
        fa, ft = parse_artist_title_from_filename(path)
        artist = artist or fa
        title = title or ft

    dur, br = read_duration_bitrate_mutagen(path)
    if use_ffprobe or dur is None or br is None:
        dur2, br2 = ffprobe_duration_bitrate(path)
        dur = dur if dur is not None else dur2
        br = br if br is not None else br2

    return Track(
        path=path,
        artist=artist,
        title=title,
        album=album,
        duration_s=dur,
        bitrate_bps=br,
        size_bytes=size,
    )


def name_score(path: str, artist: Optional[str], title: Optional[str]) -> float:
    """
    Higher is "better named".
    """
    bn = os.path.splitext(os.path.basename(path))[0]
    s = 0.0

    if re.search(r"\[[A-Za-z0-9_-]{11}\]$", bn):
        s += 8
    if " - " in bn:
        s += 10
    if "_" in bn:
        s -= 3
    if re.search(r"\bunknown\b", bn, re.I):
        s -= 20
    if re.search(r"nightcore\s*-\s*nightcore", bn, re.I):
        s -= 25

    if artist and title:
        bnl = bn.lower()
        if artist.lower() in bnl:
            s += 6
        if title.lower() in bnl:
            s += 6

    s += max(0.0, 30.0 - (len(bn) / 4.0))
    return s


def group_key_metadata(t: Track, loose: bool) -> Optional[Tuple[str, str, int]]:
    a = norm(t.artist)
    ti = norm(t.title)
    if a is None or ti is None or t.duration_s is None:
        return None
    title_norm = loose_title(ti) if loose else ti
    dur_bucket = int(round(float(t.duration_s)))
    return a, title_norm, dur_bucket


def iter_audio_files(root: str, recurse: bool, exts: Iterable[str]) -> List[str]:
    exts_l = {e.lower() for e in exts}
    out: List[str] = []
    if recurse:
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext in exts_l:
                    out.append(os.path.join(dirpath, fn))
    else:
        for fn in os.listdir(root):
            p = os.path.join(root, fn)
            if not os.path.isfile(p):
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext in exts_l:
                out.append(p)
    return out


def unique_path(dest_dir: str, filename: str) -> str:
    base, ext = os.path.splitext(filename)
    cand = os.path.join(dest_dir, filename)
    if not os.path.exists(cand):
        return cand
    i = 2
    while True:
        cand2 = os.path.join(dest_dir, f"{base} (dup{i}){ext}")
        if not os.path.exists(cand2):
            return cand2
        i += 1


def move_file(src: str, dest_dir: str, dry_run: bool) -> str:
    ensure_dir(dest_dir)
    dest = unique_path(dest_dir, os.path.basename(src))
    if dry_run:
        print(f"  MOVE   {src}\n    ->   {dest}")
        return dest
    return shutil.move(src, dest)


def rename_in_place(src: str, new_basename: str, dry_run: bool) -> str:
    d = os.path.dirname(src)
    ext = os.path.splitext(src)[1]
    base = os.path.splitext(new_basename)[0]
    desired = os.path.join(d, base + ext)

    if os.path.abspath(desired) == os.path.abspath(src):
        return src

    desired = unique_path(d, os.path.basename(desired))
    if dry_run:
        print(f"  RENAME {src}\n    ->   {desired}")
        return desired
    os.replace(src, desired)
    return desired


# -------------------------
# Fingerprinting
# -------------------------


class FingerprintCache:
    def __init__(self, path: str) -> None:
        self.path = path
        self.data: Dict[str, Dict[str, object]] = {"version": 1, "entries": {}}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("entries"), dict):
                self.data = d
        except Exception:
            pass

    def save(self) -> None:
        ensure_dir(os.path.dirname(self.path))
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def get(self, path: str, *, method: str, seconds: int) -> Optional[str]:
        entries = self.data.setdefault("entries", {})
        st = os.stat(path)
        key = os.path.abspath(path)

        rec = entries.get(key)
        if (
            isinstance(rec, dict)
            and rec.get("mtime") == st.st_mtime
            and rec.get("size") == st.st_size
            and rec.get("method") == method
            and rec.get("seconds") == seconds
            and isinstance(rec.get("fp"), str)
        ):
            return str(rec["fp"])

        fp = compute_fingerprint(path, method=method, seconds=seconds)
        if fp is None:
            return None

        entries[key] = {
            "mtime": st.st_mtime,
            "size": st.st_size,
            "method": method,
            "seconds": seconds,
            "fp": fp,
        }
        return fp


def compute_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def compute_pcmhash_ffmpeg(path: str, seconds: int) -> Optional[str]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found in PATH (required for pcmhash)")

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        path,
        "-t",
        str(seconds),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "11025",
        "-f",
        "s16le",
        "pipe:1",
    ]

    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert p.stdout is not None
        h = hashlib.sha256()
        while True:
            chunk = p.stdout.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
        _stderr = p.stderr.read() if p.stderr else b""
        rc = p.wait()
        if rc != 0:
            return None
        return f"pcmhash:{seconds}s:" + h.hexdigest()
    except Exception:
        return None


def compute_chromaprint_fpcalc(path: str, seconds: int) -> Optional[str]:
    fpcalc = shutil.which("fpcalc")
    if not fpcalc:
        raise RuntimeError("fpcalc not found in PATH (required for chromaprint)")

    try:
        p = subprocess.run(
            [fpcalc, "-length", str(seconds), path],
            capture_output=True,
            text=True,
            check=True,
        )
        fp = None
        for ln in (p.stdout or "").splitlines():
            if ln.startswith("FINGERPRINT="):
                fp = ln.split("=", 1)[1].strip()
                break
        if not fp:
            return None
        return f"chromaprint:{seconds}s:" + fp
    except Exception:
        return None


def compute_fingerprint(path: str, *, method: str, seconds: int) -> Optional[str]:
    method = method.lower().strip()
    if method == "sha256":
        return compute_sha256(path)
    if method == "pcmhash":
        return compute_pcmhash_ffmpeg(path, seconds)
    if method in ("chromaprint", "fpcalc"):
        return compute_chromaprint_fpcalc(path, seconds)
    raise ValueError(f"Unknown fingerprint method: {method}")


def fp_groups_from_tracks(
    tracks: List[Track],
    *,
    cache: FingerprintCache,
    method: str,
    seconds: int,
    dry_run: bool,
) -> Dict[str, List[Track]]:
    """
    Compute fingerprints for given tracks; return groups keyed by fingerprint string.
    """
    tmp: Dict[str, Dict[str, Track]] = {}
    for t in tracks:
        try:
            fp = cache.get(t.path, method=method, seconds=seconds)
        except Exception as e:
            if dry_run:
                print(f"  Fingerprint error: {t.path}: {e}")
            fp = None
        if not fp:
            continue
        tmp.setdefault(fp, {})[os.path.abspath(t.path)] = t

    return {fp: list(m.values()) for fp, m in tmp.items() if len(m) > 1}


# -------------------------
# Close-name scan
# -------------------------


_STOPWORDS = {
    "lyrics",
    "lyric",
    "official",
    "audio",
    "video",
    "mv",
    "nightcore",
    "edit",
    "remix",
    "sped",
    "up",
    "slowed",
    "reverb",
    "bass",
    "boosted",
}


def filename_tokens(path: str) -> List[str]:
    base = os.path.splitext(os.path.basename(path))[0]
    base = strip_yt_id_suffix(base)
    base = base.replace("–", "-").replace("—", "-")
    base = re.sub(r"[\(\)\[\]\{\}]", " ", base)
    base = re.sub(r"[^a-zA-Z0-9]+", " ", base).strip().lower()
    toks = [t for t in base.split() if t and t not in _STOPWORDS]
    # Drop ultra-short tokens except numbers
    out: List[str] = []
    for t in toks:
        if len(t) >= 3 or t.isdigit():
            out.append(t)
    return out


def close_keys_for_track(t: Track, *, loose: bool) -> List[str]:
    """
    Generate blocking keys that catch partial name matches without O(n^2).
    Keys include duration bucket to reduce collisions.
    """
    keys: List[str] = []

    if t.duration_s is None:
        return keys

    dur_bucket = int(round(float(t.duration_s) / 5.0) * 5)  # 5s buckets

    # Title-based key
    if t.title:
        tt = loose_title(t.title) if loose else t.title
        tt_norm = re.sub(r"[^a-zA-Z0-9]+", " ", tt).strip().lower()
        toks = [x for x in tt_norm.split() if x and x not in _STOPWORDS]
        if toks:
            keys.append(f"{dur_bucket}:t:" + " ".join(toks[:8]))

    # Artist+Title key (stronger)
    if t.artist and t.title:
        aa = re.sub(r"[^a-zA-Z0-9]+", " ", t.artist).strip().lower()
        tt = loose_title(t.title) if loose else t.title
        tt = re.sub(r"[^a-zA-Z0-9]+", " ", tt).strip().lower()
        at = (aa + " " + tt).strip()
        at_toks = [x for x in at.split() if x and x not in _STOPWORDS]
        if at_toks:
            keys.append(f"{dur_bucket}:at:" + " ".join(at_toks[:10]))

    # Filename prefix key
    ftoks = filename_tokens(t.path)
    if ftoks:
        keys.append(f"{dur_bucket}:fn:" + " ".join(ftoks[:10]))

    return list(dict.fromkeys(keys))  # dedupe preserving order


def close_candidate_tracks(tracks: List[Track], *, loose: bool) -> List[Track]:
    """
    Returns a de-duplicated list of tracks that are in "close match" candidate groups.
    (We will fingerprint only these to save time.)
    """
    buckets: Dict[str, List[Track]] = {}
    for t in tracks:
        for k in close_keys_for_track(t, loose=loose):
            buckets.setdefault(k, []).append(t)

    cand: Dict[str, Track] = {}
    for _k, ts in buckets.items():
        if len(ts) <= 1:
            continue
        for t in ts:
            cand[os.path.abspath(t.path)] = t

    return list(cand.values())


def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Move duplicate audio files to a subfolder.\n"
            "Default matching: metadata (Artist+Title+duration).\n"
            "Optional: fingerprint passes for stronger duplicate detection."
        )
    )
    ap.add_argument("root", help="Folder to scan (e.g. your audio output folder)")
    ap.add_argument(
        "--duplicates-subdir",
        default="_duplicates",
        help="Subfolder name to move duplicates into",
    )
    ap.add_argument("--recurse", action="store_true", help="Recurse into subfolders")
    ap.add_argument(
        "--ffprobe",
        action="store_true",
        help="Use ffprobe for bitrate/duration (slower, more consistent)",
    )
    ap.add_argument(
        "--loose",
        action="store_true",
        help="Looser title matching (strips trailing '(Lyrics)', etc.)",
    )
    ap.add_argument(
        "--min-duration",
        type=float,
        default=20.0,
        help="Ignore files shorter than this many seconds (default: 20)",
    )
    ap.add_argument("--dry-run", action="store_true", help="Print actions only")
    ap.add_argument(
        "--exts",
        default=",".join(AUDIO_EXTS_DEFAULT),
        help="Comma-separated extensions to include",
    )

    # Fingerprint options
    ap.add_argument(
        "--fingerprint",
        action="store_true",
        help="Fingerprint-refine metadata duplicates (safer than metadata-only)",
    )
    ap.add_argument(
        "--fingerprint-scan-close",
        action="store_true",
        help=(
            "Fingerprint-scan likely duplicates by close name matching (more thorough, "
            "still cheaper than scan-all)"
        ),
    )
    ap.add_argument(
        "--fingerprint-scan-all",
        action="store_true",
        help="Fingerprint-scan all audio files and group exact fingerprint matches",
    )
    ap.add_argument(
        "--fingerprint-method",
        default="pcmhash",
        choices=["pcmhash", "sha256", "chromaprint"],
        help="Fingerprint method (default: pcmhash)",
    )
    ap.add_argument(
        "--fingerprint-seconds",
        type=int,
        default=90,
        help="Seconds to fingerprint for pcmhash/chromaprint (default: 90)",
    )
    ap.add_argument(
        "--fingerprint-cache",
        default="",
        help="Path to fingerprint cache JSON (default: <root>/_fingerprints.json)",
    )

    args = ap.parse_args()

    root = os.path.abspath(args.root)
    dup_root = os.path.join(root, args.duplicates_subdir)
    exts = [e.strip().lower() for e in args.exts.split(",") if e.strip()]

    files = iter_audio_files(root, args.recurse, exts)
    files = [p for p in files if args.duplicates_subdir not in p.split(os.sep)]

    print(f"Scanning: {root}")
    print(f"Files:    {len(files)}")

    # Load tracks
    tracks_all: List[Track] = []
    skipped = 0
    for p in files:
        try:
            t = get_track(p, use_ffprobe=args.ffprobe)
            if t.duration_s is not None and t.duration_s < args.min_duration:
                skipped += 1
                continue
            tracks_all.append(t)
        except Exception:
            skipped += 1

    print(f"Tracks loaded: {len(tracks_all)} (skipped: {skipped})")

    # Build duplicate groups based on chosen mode
    dup_groups: Dict[str, List[Track]] = {}

    fp_cache: Optional[FingerprintCache] = None
    if args.fingerprint_scan_all or args.fingerprint_scan_close or args.fingerprint:
        cache_path = (
            args.fingerprint_cache
            if args.fingerprint_cache
            else os.path.join(root, "_fingerprints.json")
        )
        fp_cache = FingerprintCache(cache_path)

    if args.fingerprint_scan_all:
        print(
            "Mode: fingerprint-scan-all "
            f"(method={args.fingerprint_method}, seconds={args.fingerprint_seconds})"
        )
        assert fp_cache is not None
        dup_groups = fp_groups_from_tracks(
            tracks_all,
            cache=fp_cache,
            method=args.fingerprint_method,
            seconds=args.fingerprint_seconds,
            dry_run=args.dry_run,
        )

    elif args.fingerprint_scan_close:
        print(
            "Mode: fingerprint-scan-close "
            f"(method={args.fingerprint_method}, seconds={args.fingerprint_seconds})"
        )
        candidates = close_candidate_tracks(tracks_all, loose=args.loose)
        print(f"Close-match candidates to fingerprint: {len(candidates)}")
        assert fp_cache is not None
        dup_groups = fp_groups_from_tracks(
            candidates,
            cache=fp_cache,
            method=args.fingerprint_method,
            seconds=args.fingerprint_seconds,
            dry_run=args.dry_run,
        )

    else:
        # Metadata-first grouping
        meta_groups: Dict[Tuple[str, str, int], List[Track]] = {}
        for t in tracks_all:
            k = group_key_metadata(t, loose=args.loose)
            if k is None:
                continue
            meta_groups.setdefault(k, []).append(t)

        meta_dups = {k: v for k, v in meta_groups.items() if len(v) > 1}
        print(f"Duplicate groups (metadata): {len(meta_dups)}")

        if args.fingerprint:
            print(
                "Refining metadata duplicates with fingerprints "
                f"(method={args.fingerprint_method}, seconds={args.fingerprint_seconds})"
            )
            assert fp_cache is not None
            # fingerprint only tracks that are in metadata-dup groups
            pool: Dict[str, Track] = {}
            for ts in meta_dups.values():
                for t in ts:
                    pool[os.path.abspath(t.path)] = t
            fp_dups = fp_groups_from_tracks(
                list(pool.values()),
                cache=fp_cache,
                method=args.fingerprint_method,
                seconds=args.fingerprint_seconds,
                dry_run=args.dry_run,
            )
            dup_groups = fp_dups
            print(f"Duplicate groups (fingerprint-refined): {len(dup_groups)}")
        else:
            # Convert metadata dup groups to string-keyed map for processing below
            dup_groups = {
                f"{k[0]}|{k[1]}|{k[2]}": v for k, v in meta_dups.items()
            }

    print(f"Duplicate groups selected: {len(dup_groups)}")

    moved_count = 0
    renamed_count = 0

    # Process each group
    for _gk, tracks in sorted(dup_groups.items(), key=lambda kv: len(kv[1]), reverse=True):
        if len(tracks) <= 1:
            continue

        def keep_sort_key(t: Track) -> Tuple[int, int, float]:
            br = t.bitrate_bps or 0
            ns = name_score(t.path, t.artist, t.title)
            return br, t.size_bytes, ns

        keep = sorted(tracks, key=keep_sort_key, reverse=True)[0]
        best_name = sorted(
            tracks,
            key=lambda t: name_score(t.path, t.artist, t.title),
            reverse=True,
        )[0]

        artist_disp = keep.artist or parse_artist_title_from_filename(keep.path)[0] or "Unknown Artist"
        title_disp = keep.title or parse_artist_title_from_filename(keep.path)[1] or "Unknown Title"

        dest_dir = os.path.join(
            dup_root, sanitize_folder_name(f"{artist_disp} - {title_disp}")
        )

        print(
            f"\nGroup: {artist_disp} - {title_disp} files={len(tracks)}"
        )
        print(
            f"  KEEP: {os.path.basename(keep.path)} "
            f"(bitrate={keep.bitrate_bps}, size={keep.size_bytes})"
        )

        to_move = [t for t in tracks if os.path.abspath(t.path) != os.path.abspath(keep.path)]

        # If a different file has a better name, move it first, then rename kept file to that name
        if os.path.abspath(best_name.path) != os.path.abspath(keep.path):
            bn = os.path.basename(best_name.path)
            print(f"  Best filename source: {bn}")

            move_file(best_name.path, dest_dir, args.dry_run)
            moved_count += 1
            to_move = [t for t in to_move if os.path.abspath(t.path) != os.path.abspath(best_name.path)]

            new_path = rename_in_place(keep.path, bn, args.dry_run)
            if os.path.abspath(new_path) != os.path.abspath(keep.path):
                keep = Track(
                    path=new_path,
                    artist=keep.artist,
                    title=keep.title,
                    album=keep.album,
                    duration_s=keep.duration_s,
                    bitrate_bps=keep.bitrate_bps,
                    size_bytes=keep.size_bytes,
                )
                renamed_count += 1

        for t in to_move:
            move_file(t.path, dest_dir, args.dry_run)
            moved_count += 1

    if fp_cache and not args.dry_run:
        fp_cache.save()

    print(f"\nDone. Moved: {moved_count}, Renamed kept: {renamed_count}")


if __name__ == "__main__":
    main()