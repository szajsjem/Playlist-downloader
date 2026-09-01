# Playlist-downloader

Download YouTube playlists as tagged M4A files.

## Quick Start

1. Ensure ffmpeg is in your PATH
2. Download Ollama and install a model (e.g., `ollama run llama3.2`)
3. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Run the script:
   ```bash
   python main.py "https://www.youtube.com/playlist?list=PLAYLIST_ID" --workers 4 --out MyAudioFolder
   ```
5. Audio files will be in ``MyAudioFolder/audio/`` with embedded metadata

## Features

- Download entire playlists as AAC (M4A)
- Automatic metadata extraction via Ollama LLM for artist/title/album info
- Nightcore detection and handling
- Resume support: skip already downloaded tracks
- Thumbnail extraction as cover art
- remove_duplicates.py to organize duplicate files by audio fingerprint

## Command Line Options

```bash
python main.py [options] PLAYLIST_URL

Options:
  --outdir DIR        Output folder (default: out_music)
  --model NAME        Ollama model (default: llama3.2)
  --cookies FILE      Path to cookies.txt for YouTube auth
  --limit NUM         Number of videos to download (0 = all)
  --sleep SECONDS     Wait between downloads (default: 1.0)
  --aac-bitrate RATE  AAC bitrate target, e.g., "256k", "320k" (default: 256k)
  --workers NUM       Parallel conversion threads (default: half CPU cores)
  --refresh-playlist  Force re-extract playlist from YouTube
```

## Dependencies

- `requests` - HTTP requests
- `mutagen` - MP4 tag handling
- `yt-dlp` - YouTube downloading
- `ffmpeg`/`ffprobe` - Audio conversion (must be in PATH)

## building py2exe Release

1. Install build tools:
   ```bash
   pip install py2exe setuptools
   ```

2. Ensure ffmpeg is accessible in system PATH

3. Build:
   ```bash
   python setup.py py2exe
   ```
4. Output will be in `dist/Playlist-Downloader.exe` plus dependencies
5. Upload to GitHub Releases as your release file
