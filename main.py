#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from yt_dlp import YoutubeDL


# -----------------------------
# Thread-local HTTP sessions
# -----------------------------
_TLS = threading.local()


def http_session() -> requests.Session:
    s = getattr(_TLS, "session", None)
    if s is None:
        s = requests.Session()
        _TLS.session = s
    return s


# -----------------------------
# Utilities
# -----------------------------
def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def sanitize_filename(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def truncate_filename(s: str, max_len: int = 160) -> str:
    s = s.strip()
    return s if len(s) <= max_len else s[: max_len - 1].rstrip()


def is_probably_youtube_id(s: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", s or ""))


def normalize_video_url(entry_url_or_id: str) -> str:
    if entry_url_or_id.startswith(("http://", "https://")):
        return entry_url_or_id
    if is_probably_youtube_id(entry_url_or_id):
        return f"https://www.youtube.com/watch?v={entry_url_or_id}"
    return entry_url_or_id


def safe_json_loads(s: str) -> Any:
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(s[start : end + 1])
        raise


def write_json(path: str, obj: Any) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def read_text(path: str) -> Optional[str]:
    if not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


# -----------------------------
# Playlist cache
# -----------------------------
def load_playlist_cache(cache_path: str) -> Optional[Tuple[str, List[str]]]:
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        title = data.get("playlist_title") or "Playlist"
        urls = data.get("urls") or []
        if not isinstance(urls, list) or not urls:
            return None
        return title, [str(u) for u in urls]
    except Exception:
        return None


def save_playlist_cache(
    cache_path: str,
    playlist_title: str,
    playlist_url: str,
    urls: List[str],
) -> None:
    ensure_dir(os.path.dirname(cache_path))
    payload = {
        "playlist_title": playlist_title,
        "playlist_url": playlist_url,
        "count": len(urls),
        "urls": urls,
    }
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # Also write plain text list (nice for debugging / reuse)
    txt_path = os.path.splitext(cache_path)[0] + ".urls.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(urls) + ("\n" if urls else ""))


def extract_playlist(playlist_url: str) -> Tuple[str, List[str]]:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(playlist_url, download=False)

    playlist_title = info.get("title") or "Playlist"
    entries = info.get("entries") or []
    urls: List[str] = []
    for e in entries:
        if not e:
            continue
        u = e.get("url") or e.get("id")
        if not u:
            continue
        urls.append(normalize_video_url(u))
    return playlist_title, urls


# -----------------------------
# Resume helpers
# -----------------------------
def final_audio_exists_for_id(final_dir: str, video_id: str) -> bool:
    return len(glob.glob(os.path.join(final_dir, f"*[{video_id}].m4a"))) > 0


def load_infojson_from_intermediate(intermediate_dir: str) -> Optional[Dict[str, Any]]:
    candidates = glob.glob(os.path.join(intermediate_dir, "*.info.json"))
    if not candidates:
        return None
    candidates.sort(key=lambda p: os.path.getsize(p), reverse=True)
    try:
        with open(candidates[0], "r", encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except Exception:
        return None


def find_src_media_file(intermediate_dir: str) -> Optional[str]:
    candidates = glob.glob(os.path.join(intermediate_dir, "*.src.*"))
    candidates = [
        p
        for p in candidates
        if os.path.isfile(p)
        and not p.endswith(".info.json")
        and not p.endswith(".description")
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: os.path.getsize(p), reverse=True)
    return candidates[0]


# -----------------------------
# yt-dlp download
# -----------------------------
def pick_downloaded_filepath(info: Dict[str, Any], ydl: YoutubeDL) -> Optional[str]:
    req = info.get("requested_downloads")
    if isinstance(req, list) and req:
        fp = req[0].get("filepath")
        if fp and os.path.exists(fp):
            return fp

    fp = info.get("filepath") or info.get("_filename")
    if fp and os.path.exists(fp):
        return fp

    try:
        fp2 = ydl.prepare_filename(info)
        if fp2 and os.path.exists(fp2):
            return fp2
    except Exception:
        pass

    return None


def download_one_to_intermediate(
    *,
    url: str,
    intermediate_dir: str,
    archive_path: str,
    cookies: Optional[str],
    sleep: float,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_dir(intermediate_dir)

    # IMPORTANT: ".src." prevents collisions with our converted output name.
    outtmpl = os.path.join(intermediate_dir, "%(title)s [%(id)s].src.%(ext)s")
    ydl_opts: Dict[str, Any] = {
        "quiet": False,
        "no_warnings": True,
        "ignoreerrors": True,
        "format": "bestaudio/best",
        "format_sort": ["abr:desc"],
        "outtmpl": outtmpl,
        "writedescription": True,
        "writeinfojson": True,
        "writethumbnail": True,
        "noplaylist": True,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 5,
        "file_access_retries": 5,
        "continuedl": True,
        "overwrites": True,
        "download_archive": archive_path,
        "keepvideo": True,
        "sleep_interval": sleep,
        "max_sleep_interval": max(5, int(sleep) + 5),
    }
    if cookies:
        ydl_opts["cookiefile"] = cookies

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info:
            return None, None
        fp = pick_downloaded_filepath(info, ydl)
        return info, fp


# -----------------------------
# Duration / truncation detection
# -----------------------------
def ffprobe_duration_seconds(path: str) -> Optional[float]:
    try:
        p = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        s = (p.stdout or "").strip()
        return float(s) if s else None
    except Exception:
        return None


def looks_truncated(expected_s: Optional[float], actual_s: Optional[float]) -> bool:
    if expected_s is None or actual_s is None:
        return False
    if expected_s >= 30 and actual_s <= 10:
        return True
    if expected_s >= 60 and actual_s < expected_s * 0.25:
        return True
    return False


def ffmpeg_convert_to_m4a(
    input_path: str,
    output_path: str,
    *,
    copy_if_aac_in_mp4: bool,
    aac_bitrate: str,
    expected_duration_s: Optional[float] = None,
) -> None:
    ensure_dir(os.path.dirname(output_path))

    if os.path.abspath(input_path) == os.path.abspath(output_path):
        raise RuntimeError("Refusing to convert in-place (input == output).")

    def do_stream_copy() -> bool:
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-map",
            "0:a:0",
            "-vn",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            output_path,
        ]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            return False
        out_dur = ffprobe_duration_seconds(output_path)
        return not looks_truncated(expected_duration_s, out_dur)

    def do_reencode(extra_filters: Optional[str] = None) -> None:
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-map",
            "0:a:0",
            "-vn",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-fflags",
            "+genpts",
            "-avoid_negative_ts",
            "make_zero",
        ]
        if extra_filters:
            cmd += ["-af", extra_filters]
        cmd += [
            "-c:a",
            "aac",
            "-b:a",
            aac_bitrate,
            "-movflags",
            "+faststart",
            output_path,
        ]
        subprocess.run(cmd, check=True)

    if copy_if_aac_in_mp4:
        ext = os.path.splitext(input_path)[1].lower()
        if ext in (".m4a", ".mp4"):
            if do_stream_copy():
                return

    do_reencode()
    out_dur = ffprobe_duration_seconds(output_path)
    if looks_truncated(expected_duration_s, out_dur):
        do_reencode("aresample=async=1:first_pts=0")

    out_dur2 = ffprobe_duration_seconds(output_path)
    if looks_truncated(expected_duration_s, out_dur2):
        raise RuntimeError(
            f"Converted file looks truncated: expected~{expected_duration_s}s, got "
            f"{out_dur2}s"
        )


# -----------------------------
# Thumbnails (cover.jpg, 256x256)
# -----------------------------
def pick_best_thumbnail_url(info: Dict[str, Any]) -> Optional[str]:
    thumbs = info.get("thumbnails") or []
    if not thumbs:
        t = info.get("thumbnail")
        return t if isinstance(t, str) else None

    def score(th: Dict[str, Any]) -> int:
        w = th.get("width") or 0
        h = th.get("height") or 0
        pref = 0
        if th.get("id") in ("maxresdefault", "maxres", "high"):
            pref += 1_000_000
        return pref + (w * h)

    usable = [t for t in thumbs if t.get("url")]
    if not usable:
        return None
    best = sorted(usable, key=score, reverse=True)[0]
    return best.get("url")


def download_thumbnail(url: str, outpath_no_ext: str, timeout_s: int = 60) -> Optional[str]:
    try:
        r = http_session().get(url, timeout=timeout_s)
        r.raise_for_status()
        content_type = (r.headers.get("content-type") or "").lower()
        ext = ".jpg"
        if "png" in content_type:
            ext = ".png"
        elif "webp" in content_type:
            ext = ".webp"

        final_path = outpath_no_ext + ext
        with open(final_path, "wb") as f:
            f.write(r.content)
        return final_path
    except Exception:
        return None


def find_any_thumbnail_file(intermediate_dir: str) -> Optional[str]:
    patterns = ["*.jpg", "*.jpeg", "*.png", "*.webp"]
    candidates: List[str] = []
    for pat in patterns:
        candidates.extend(glob.glob(os.path.join(intermediate_dir, pat)))
    candidates = [p for p in candidates if os.path.isfile(p)]
    if not candidates:
        return None
    candidates.sort(key=lambda p: os.path.getsize(p), reverse=True)
    return candidates[0]


def convert_image_to_cover_jpg(input_path: str, cover_jpg_path: str) -> Optional[str]:
    """
    Convert to square JPEG 256x256 for maximum Windows/player compatibility.
    """
    try:
        size = 256
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                input_path,
                "-vf",
                f"crop='min(iw,ih)':'min(iw,ih)',scale={size}:{size}",
                "-q:v",
                "3",
                cover_jpg_path,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return cover_jpg_path if os.path.exists(cover_jpg_path) else None
    except Exception:
        return None


def ensure_cover_jpg(intermediate_dir: str, info: Dict[str, Any]) -> Optional[str]:
    cover_jpg = os.path.join(intermediate_dir, "cover.jpg")

    existing = find_any_thumbnail_file(intermediate_dir)
    if existing:
        return convert_image_to_cover_jpg(existing, cover_jpg)

    thumb_url = pick_best_thumbnail_url(info)
    if not thumb_url:
        return None

    downloaded = download_thumbnail(thumb_url, os.path.join(intermediate_dir, "thumb"))
    if not downloaded:
        return None

    return convert_image_to_cover_jpg(downloaded, cover_jpg)


# -----------------------------
# Metadata extraction / merge
# -----------------------------
@dataclass
class TrackMeta:
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    album_artist: Optional[str] = None
    composer: Optional[str] = None
    genre: Optional[str] = None
    date: Optional[str] = None
    compilation: Optional[bool] = None
    grouping: Optional[str] = None
    copyright: Optional[str] = None
    encoded_by: Optional[str] = None
    publisher: Optional[str] = None
    sort_title: Optional[str] = None
    sort_artist: Optional[str] = None
    sort_album: Optional[str] = None
    sort_album_artist: Optional[str] = None
    sort_composer: Optional[str] = None
    confidence: Optional[float] = None
    other: Optional[Dict[str, Any]] = None


def parse_artist_title_from_title(yt_title: str) -> Tuple[Optional[str], Optional[str]]:
    # Common: "Artist - Title" or "Artist – Title"
    m = re.match(r"^\s*(.+?)\s*[-–—]\s*(.+?)\s*$", yt_title)
    if not m:
        return None, None

    left = m.group(1).strip()
    right = m.group(2).strip()
    if not left or not right:
        return None, None

    # Do NOT treat Nightcore as artist
    if left.lower() in ("nightcore", "nightcore lyrics", "nightcore music"):
        return None, right

    return left, right


def guess_artist(channel: str, uploader: str, yt_title: str) -> Optional[str]:
    if channel.endswith(" - Topic"):
        return channel.replace(" - Topic", "").strip()
    if uploader.endswith("VEVO"):
        return uploader.replace("VEVO", "").strip()
    a, _t = parse_artist_title_from_title(yt_title)
    if a and len(a) <= 60:
        return a
    return None


def normalize_date(upload_date: Optional[str]) -> Optional[str]:
    if not upload_date:
        return None
    if re.fullmatch(r"\d{8}", str(upload_date)):
        return f"{upload_date[0:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
    return str(upload_date).strip() or None


def sort_key(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s2 = s.strip()
    s2 = re.sub(r"^(the|a|an)\s+", "", s2, flags=re.IGNORECASE)
    return s2


def parse_provided_to_youtube(description: str) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {
        "publisher": None,
        "copyright": None,
        "release_date": None,
        "composer": None,
        "artist": None,
        "title": None,
        "album": None,
    }

    lines = [ln.strip() for ln in description.splitlines() if ln.strip()]

    for ln in lines[:25]:
        m = re.match(r"^Provided to YouTube by (.+)$", ln, re.IGNORECASE)
        if m and not out["publisher"]:
            out["publisher"] = m.group(1).strip()

        m = re.match(r"^Released on:\s*(.+)$", ln, re.IGNORECASE)
        if m and not out["release_date"]:
            out["release_date"] = m.group(1).strip()

        m = re.match(r"^Composer:\s*(.+)$", ln, re.IGNORECASE)
        if m and not out["composer"]:
            out["composer"] = m.group(1).strip()

        if ln.startswith("℗") or ln.startswith("©"):
            if not out["copyright"]:
                out["copyright"] = ln

    for i in range(min(len(lines), 10)):
        if "·" in lines[i] and not out["artist"]:
            parts = [p.strip() for p in lines[i].split("·")]
            if len(parts) >= 2:
                if not out["title"]:
                    out["title"] = parts[0]
                out["artist"] = parts[1]
                if i + 1 < len(lines) and not out["album"]:
                    out["album"] = lines[i + 1]

    return out


def ollama_extract_metadata(
    *,
    model: str,
    source_url: str,
    yt_title: str,
    channel: str,
    uploader: str,
    upload_date: Optional[str],
    description: Optional[str],
    candidates: Dict[str, Any],
    timeout_s: int = 120,
) -> TrackMeta:
    prompt = f"""
You extract music file metadata from YouTube title/channel/description plus heuristics.

Return ONLY valid JSON. No markdown. No commentary.
Do not invent facts. If unknown, use null.

Output JSON keys (exactly these):
{{
  "title": string|null,
  "artist": string|null,
  "album": string|null,
  "album_artist": string|null,
  "composer": string|null,
  "genre": string|null,
  "date": string|null,
  "compilation": boolean|null,
  "grouping": string|null,
  "copyright": string|null,
  "encoded_by": string|null,
  "publisher": string|null,
  "sort_title": string|null,
  "sort_artist": string|null,
  "sort_album": string|null,
  "sort_album_artist": string|null,
  "sort_composer": string|null,
  "confidence": number|null,
  "other": object|null
}}

Special rules:
- If this is a Nightcore upload (e.g. yt_title contains "Nightcore" or candidates indicate nightcore=true):
  - Set genre to "Nightcore" unless a better explicit genre exists.
  - Do NOT set artist to "Nightcore".
- If you can identify the original artist(s), put them in "artist".
- Use the channel/uploader as "publisher" if no explicit label/publisher is given.

Input:
source_url: {source_url}
yt_title: {yt_title}
channel: {channel}
uploader: {uploader}
upload_date: {upload_date or None}

candidates_json:
{json.dumps(candidates, ensure_ascii=False)}

description:
{description or ""}
""".strip()

    last_err: Optional[str] = None
    for attempt in range(1, 4):
        prompt_to_send = prompt
        if attempt > 1 and last_err:
            prompt_to_send = (
                prompt
                + "\n\nIMPORTANT: Previous response was invalid.\n"
                + f"Error: {last_err}\n"
                + "Return ONLY strict JSON (double quotes, no trailing commas)."
            )

        payload = {
            "model": model,
            "prompt": prompt_to_send,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2},
        }

        try:
            r = http_session().post(
                "http://localhost:11434/api/generate",
                json=payload,
                timeout=(3, timeout_s),
            )
            r.raise_for_status()
            data = r.json()
            raw = data.get("response", "")
            parsed = safe_json_loads(raw)

            return TrackMeta(
                title=parsed.get("title"),
                artist=parsed.get("artist"),
                album=parsed.get("album"),
                album_artist=parsed.get("album_artist"),
                composer=parsed.get("composer"),
                genre=parsed.get("genre"),
                date=parsed.get("date"),
                compilation=parsed.get("compilation"),
                grouping=parsed.get("grouping"),
                copyright=parsed.get("copyright"),
                encoded_by=parsed.get("encoded_by"),
                publisher=parsed.get("publisher"),
                sort_title=parsed.get("sort_title"),
                sort_artist=parsed.get("sort_artist"),
                sort_album=parsed.get("sort_album"),
                sort_album_artist=parsed.get("sort_album_artist"),
                sort_composer=parsed.get("sort_composer"),
                confidence=parsed.get("confidence"),
                other=parsed.get("other"),
            )
        except Exception as e:
            last_err = str(e)
            if attempt < 3:
                time.sleep(1.5 * attempt)
                continue
            raise


def is_nightcore(yt_title: str, llm_other: Optional[Dict[str, Any]]) -> bool:
    if llm_other and isinstance(llm_other.get("nightcore"), bool):
        if llm_other["nightcore"]:
            return True
    return "nightcore" in (yt_title or "").lower()


def merge_meta(
    *,
    llm: TrackMeta,
    playlist_title: str,
    yt_title: str,
    channel: str,
    uploader: str,
    upload_date: Optional[str],
    desc_meta: Dict[str, Optional[str]],
) -> TrackMeta:
    m = llm

    other = m.other or {}
    other.update({"channel": channel, "uploader": uploader})
    m.other = other

    nightcore_flag = is_nightcore(yt_title, m.other)

    if not m.title:
        m.title = desc_meta.get("title") or yt_title

    if not m.artist:
        m.artist = desc_meta.get("artist") or guess_artist(channel, uploader, yt_title)

    if not m.album:
        m.album = desc_meta.get("album") or playlist_title

    if not m.album_artist:
        m.album_artist = m.artist

    if not m.composer:
        m.composer = desc_meta.get("composer")

    if not m.publisher:
        m.publisher = desc_meta.get("publisher")

    if not m.copyright:
        m.copyright = desc_meta.get("copyright")

    if not m.date:
        m.date = desc_meta.get("release_date") or upload_date

    if m.compilation is None:
        m.compilation = False

    if not m.grouping:
        m.grouping = playlist_title

    if not m.encoded_by:
        m.encoded_by = "yt-dlp + ffmpeg + mutagen (Ollama metadata)"

    # Your Nightcore rules:
    if nightcore_flag:
        if not m.genre:
            m.genre = "Nightcore"

        original_artist = m.other.get("original_artist") if isinstance(m.other, dict) else None
        if isinstance(original_artist, str) and original_artist.strip():
            m.artist = original_artist.strip()

        ch = m.other.get("channel") if isinstance(m.other, dict) else None
        if isinstance(ch, str) and ch.strip():
            m.publisher = ch.strip()

        # Keep album artist as Nightcore for these uploads
        m.album_artist = "Nightcore"

    if not m.sort_title:
        m.sort_title = sort_key(m.title)
    if not m.sort_artist:
        m.sort_artist = sort_key(m.artist)
    if not m.sort_album:
        m.sort_album = sort_key(m.album)
    if not m.sort_album_artist:
        m.sort_album_artist = sort_key(m.album_artist)
    if not m.sort_composer:
        m.sort_composer = sort_key(m.composer)

    # --- Nightcore / uploader-channel promotion rules ---
    other = m.other or {}
    if isinstance(other, dict):
            nightcore_flag = bool(other.get("nightcore")) or (
                    "nightcore" in (yt_title or "").lower()
            )

            original_artist = other.get("original_artist")
            if not isinstance(original_artist, str) or not original_artist.strip():
                original_artist = None

            # Genre
            if nightcore_flag and not m.genre:
                m.genre = "Nightcore"

            # Artist / Album Artist
            if nightcore_flag and original_artist:
                if (not m.artist) or (m.artist.strip().lower() == "nightcore"):
                    m.artist = original_artist

                if (not m.album_artist) or (m.album_artist.strip().lower() == "nightcore"):
                    m.album_artist = original_artist

            # Publisher/Label: use channel/uploader if missing
            if not m.publisher:
                m.publisher = channel or uploader or None

            m.other = other

    return m


# -----------------------------
# Tagging (MP4/M4A)
# -----------------------------
def embed_tags_mp4(m4a_path: str, meta: TrackMeta, cover_path: Optional[str]) -> bool:
    try:
        mp4 = MP4(m4a_path)
    except Exception:
        return False

    def set_str(key: str, value: Optional[str]) -> None:
        if value is None:
            return
        v = str(value).strip()
        if not v:
            return
        mp4.tags[key] = [v]

    def set_bool(key: str, value: Optional[bool]) -> None:
        if value is None:
            return
        mp4.tags[key] = [1 if value else 0]

    set_str("\xa9nam", meta.title)        # Title
    set_str("\xa9ART", meta.artist)       # Artist
    set_str("\xa9alb", meta.album)        # Album
    set_str("aART", meta.album_artist)    # Album Artist
    set_str("\xa9wrt", meta.composer)     # Composer
    set_str("\xa9gen", meta.genre)        # Genre
    set_str("\xa9day", meta.date)         # Year/Date
    set_bool("cpil", meta.compilation)    # Compilation
    set_str("\xa9grp", meta.grouping)     # Grouping
    set_str("cprt", meta.copyright)       # Copyright
    set_str("\xa9too", meta.encoded_by)   # Encoder

    set_str("sonm", meta.sort_title)
    set_str("soar", meta.sort_artist)
    set_str("soal", meta.sort_album)
    set_str("soaa", meta.sort_album_artist)
    set_str("soco", meta.sort_composer)

    if meta.publisher:
        mp4.tags["----:com.apple.iTunes:LABEL"] = [
            MP4FreeForm(str(meta.publisher).encode("utf-8"))
        ]

    if cover_path and os.path.exists(cover_path):
        try:
            with open(cover_path, "rb") as f:
                img = f.read()
            mp4.tags["covr"] = [MP4Cover(img, imageformat=MP4Cover.FORMAT_JPEG)]
        except Exception:
            pass

    try:
        mp4.save()
        return True
    except Exception:
        return False


# -----------------------------
# Pipeline: downloader thread -> worker threads
# -----------------------------
@dataclass
class Task:
    idx: int
    total: int
    video_url: str
    vid: str
    intermediate_dir: str
    out_root: str
    final_dir: str
    meta_dir: str
    playlist_title: str
    # May be None if only intermediate exists and infojson failed to load
    info: Optional[Dict[str, Any]] = None
    downloaded_fp: Optional[str] = None


SENTINEL = object()


def downloader_thread_fn(
    *,
    urls: List[str],
    out_root: str,
    final_dir: str,
    meta_dir: str,
    intermediate_root: str,
    archive_path: str,
    playlist_title: str,
    cookies: Optional[str],
    sleep: float,
    q: queue.Queue,
) -> None:
    total = len(urls)
    for idx, video_url in enumerate(urls, start=1):
        # Best-effort ID from URL
        vid = None
        m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", video_url)
        if m:
            vid = m.group(1)
        if not vid:
            vid = f"item_{idx:04d}"

        # Resume skip: final exists
        if is_probably_youtube_id(vid) and final_audio_exists_for_id(final_dir, vid):
            print(f"\n[{idx}/{total}] {video_url}")
            print("  Resume: final audio already exists, skipping.")
            continue

        intermediate_dir = os.path.join(intermediate_root, vid)
        ensure_dir(intermediate_dir)

        # Resume: reuse intermediate if present
        info = load_infojson_from_intermediate(intermediate_dir)
        downloaded_fp = find_src_media_file(intermediate_dir)

        # If not present, download
        if not info or not downloaded_fp:
            print(f"\n[{idx}/{total}] {video_url}")
            info, downloaded_fp = download_one_to_intermediate(
                url=video_url,
                intermediate_dir=intermediate_dir,
                archive_path=archive_path,
                cookies=cookies,
                sleep=sleep,
            )

        if not info or not downloaded_fp:
            print(f"\n[{idx}/{total}] {video_url}")
            print("  Skipped (download failed).")
            continue

        q.put(
            Task(
                idx=idx,
                total=total,
                video_url=video_url,
                vid=vid,
                intermediate_dir=intermediate_dir,
                out_root=out_root,
                final_dir=final_dir,
                meta_dir=meta_dir,
                playlist_title=playlist_title,
                info=info,
                downloaded_fp=downloaded_fp,
            )
        )

    # Tell workers to stop
    workers = q.maxsize if q.maxsize > 0 else 8
    for _ in range(9999):
        # We'll send sentinels from main based on worker count; no-op here.
        break


def worker_fn(
    *,
    worker_id: int,
    model: str,
    aac_bitrate: str,
    q: queue.Queue,
    stop_token: object,
) -> None:
    while True:
        item = q.get()
        try:
            if item is stop_token:
                return
            assert isinstance(item, Task)
            process_task(
                item,
                model=model,
                aac_bitrate=aac_bitrate,
                worker_id=worker_id,
            )
        finally:
            q.task_done()


def process_task(task: Task, *, model: str, aac_bitrate: str, worker_id: int) -> None:
    info = task.info or {}
    downloaded_fp = task.downloaded_fp

    print(f"\n[{task.idx}/{task.total}] (worker {worker_id}) {task.video_url}")

    if not downloaded_fp or not os.path.exists(downloaded_fp):
        print("  Missing downloaded source file, skipping.")
        return

    # Pull basics
    yt_title = info.get("title") or ""
    channel = info.get("channel") or info.get("uploader") or ""
    uploader = info.get("uploader") or ""
    upload_date = normalize_date(info.get("upload_date"))

    # Description
    description = info.get("description")
    if not description:
        base, _ext = os.path.splitext(downloaded_fp)
        description = read_text(base + ".description")

    desc_meta = parse_provided_to_youtube(description or "")

    # Cover (always re-derive; cheap and avoids stale/bad cover)
    cover_path = ensure_cover_jpg(task.intermediate_dir, info)

    # Convert
    vid = info.get("id") or task.vid
    base_name = sanitize_filename(f"{yt_title} [{vid}]")
    base_name = truncate_filename(base_name, 160)
    converted_m4a = os.path.join(task.intermediate_dir, base_name + ".m4a")

    expected_s = info.get("duration")
    expected_s = float(expected_s) if isinstance(expected_s, (int, float)) else None

    # Resume: do not reuse converted because bitrate might have changed, todo:check if bitrate match before converting again
    #if os.path.exists(converted_m4a):
    #    out_dur = ffprobe_duration_seconds(converted_m4a)
    #    if looks_truncated(expected_s, out_dur):
    #        print("  Converted exists but looks truncated, reconverting.")
    #        try:
    #            os.remove(converted_m4a)
    #        except Exception:
    #            pass

    if True: #not os.path.exists(converted_m4a):
        try:
            ffmpeg_convert_to_m4a(
                downloaded_fp,
                converted_m4a,
                copy_if_aac_in_mp4=True,
                aac_bitrate=aac_bitrate,
                expected_duration_s=expected_s,
            )
        except Exception as e:
            print(f"  Conversion failed: {e}")
            return

    # LLM candidates
    title_guess_artist, title_guess_title = parse_artist_title_from_title(yt_title)
    candidates = {
        "yt_dlp": {
            "title": yt_title,
            "artist": info.get("artist"),
            "track": info.get("track"),
            "album": info.get("album"),
            "creator": info.get("creator"),
            "channel": channel,
            "uploader": uploader,
            "upload_date": upload_date,
        },
        "desc_heuristics": desc_meta,
        "title_parse": {"artist": title_guess_artist, "title": title_guess_title},
        "guess_artist": guess_artist(channel, uploader, yt_title),
        "playlist_title": task.playlist_title,
    }

    # IMPORTANT per your request:
    # Always re-prompt Ollama even if ollama.meta.json exists.
    try:
        llm_meta = ollama_extract_metadata(
            model=model,
            source_url=task.video_url,
            yt_title=yt_title,
            channel=channel,
            uploader=uploader,
            upload_date=upload_date,
            description=description,
            candidates=candidates,
        )
        write_json(
            os.path.join(task.intermediate_dir, "ollama.meta.json"),
            llm_meta.__dict__,
        )
    except Exception as e:
        print(f"  Ollama failed: {e}")
        llm_meta = TrackMeta(other={"ollama_error": str(e)})

    merged = merge_meta(
        llm=llm_meta,
        playlist_title=task.playlist_title,
        yt_title=yt_title,
        channel=channel,
        uploader=uploader,
        upload_date=upload_date,
        desc_meta=desc_meta,
    )
    write_json(os.path.join(task.intermediate_dir, "merged.meta.json"), merged.__dict__)

    ok = embed_tags_mp4(converted_m4a, merged, cover_path)

    # Final output
    final_artist = truncate_filename(sanitize_filename(merged.artist or "Unknown Artist"), 80)
    final_title = truncate_filename(sanitize_filename(merged.title or yt_title or "Unknown Title"), 110)
    final_name = f"{final_artist} - {final_title} [{vid}].m4a"
    final_path = os.path.join(task.final_dir, final_name)

    shutil.copy2(converted_m4a, final_path)
    write_json(os.path.join(task.meta_dir, final_name + ".json"), merged.__dict__)

    print(f"  Intermediate: {task.intermediate_dir}")
    print(f"  Final audio:   {final_path}")
    print(f"  Tagging:       {'OK' if ok else 'FAILED (kept JSON sidecars)'}")


# -----------------------------
# Main
# -----------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("playlist_url", help="YouTube playlist URL")
    ap.add_argument("--out", default="out_music", help="Output root directory")
    ap.add_argument("--model", default="llama3.2", help="Ollama model name")
    ap.add_argument("--cookies", default=None, help="Path to cookies.txt (optional)")
    ap.add_argument("--limit", type=int, default=0, help="Limit items (0=all)")
    ap.add_argument("--sleep", type=float, default=1.0, help="Sleep between downloads")
    ap.add_argument(
        "--aac-bitrate",
        default="256k",
        help="AAC bitrate for transcoding to m4a (e.g. 192k, 256k, 320k)",
    )
    ap.add_argument(
        "--refresh-playlist",
        action="store_true",
        help="Re-extract playlist and overwrite cached playlist list",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=max(1, (os.cpu_count() or 4) // 2),
        help="Number of conversion/tagging workers",
    )
    ap.add_argument(
        "--queue-size",
        type=int,
        default=32,
        help="Max queued downloaded items waiting for workers",
    )
    args = ap.parse_args()

    out_root = args.out
    final_dir = os.path.join(out_root, "audio")
    meta_dir = os.path.join(out_root, "meta")
    intermediate_root = os.path.join(out_root, "_intermediate")
    archive_path = os.path.join(out_root, "downloaded.txt")
    playlist_cache_path = os.path.join(out_root, "playlist_cache.json")

    ensure_dir(final_dir)
    ensure_dir(meta_dir)
    ensure_dir(intermediate_root)

    cached = None if args.refresh_playlist else load_playlist_cache(playlist_cache_path)
    if cached:
        playlist_title, urls = cached
    else:
        playlist_title, urls = extract_playlist(args.playlist_url)
        save_playlist_cache(playlist_cache_path, playlist_title, args.playlist_url, urls)

    if args.limit and args.limit > 0:
        urls = urls[: args.limit]

    print(f"Playlist: {playlist_title}")
    print(f"Entries:  {len(urls)}")
    print(f"Workers:  {args.workers}")
    if cached:
        print(f"Using cached playlist list: {playlist_cache_path}")

    q: queue.Queue = queue.Queue(maxsize=max(1, args.queue_size))

    # Start worker threads
    workers: List[threading.Thread] = []
    for wid in range(1, args.workers + 1):
        t = threading.Thread(
            target=worker_fn,
            kwargs={
                "worker_id": wid,
                "model": args.model,
                "aac_bitrate": args.aac_bitrate,
                "q": q,
                "stop_token": SENTINEL,
            },
            daemon=True,
        )
        t.start()
        workers.append(t)

    # Start downloader thread
    dl = threading.Thread(
        target=downloader_thread_fn,
        kwargs={
            "urls": urls,
            "out_root": out_root,
            "final_dir": final_dir,
            "meta_dir": meta_dir,
            "intermediate_root": intermediate_root,
            "archive_path": archive_path,
            "playlist_title": playlist_title,
            "cookies": args.cookies,
            "sleep": args.sleep,
            "q": q,
        },
        daemon=True,
    )
    dl.start()

    # Wait for downloader to finish
    dl.join()

    # Wait for queue to drain
    q.join()

    # Stop workers
    for _ in workers:
        q.put(SENTINEL)
    for t in workers:
        t.join()


if __name__ == "__main__":
    main()