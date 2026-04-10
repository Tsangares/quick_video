# QuickVideo Wiki

Local video splitter/trimmer/downloader. Downloads videos via yt-dlp, plays them with mpv, lets you split/trim/delete segments, and exports via ffmpeg stream copy (instant, no re-encoding).

Built because the browser-based FFmpeg.wasm compressor choked on 500MB+ files.

## Quick Start

```bash
# Dependencies (Arch)
sudo pacman -S ffmpeg mpv yt-dlp python-pyqt5 python-mpv python-openai-whisper

# Run
python3 quick_video.py              # opens GUI
python3 quick_video.py video.mp4    # opens with file preloaded
```

## Wiki Pages

- [Architecture](architecture.md) -- code structure, data flow, dependencies
- [Usage Guide](usage.md) -- how to use every feature
- [Keyboard Shortcuts](shortcuts.md) -- full shortcut reference
- [Improvements Roadmap](improvements.md) -- proposed UI, performance, and general improvements + Rust rewrite assessment

## Tech Stack

| Component | Role |
|-----------|------|
| Python 3 + PyQt5 | GUI framework, dark theme, HiDPI |
| mpv (python-mpv) | Embedded video playback, seeking, speed control |
| ffmpeg / ffprobe | Video metadata, thumbnail extraction, stream-copy export |
| yt-dlp | Download from YouTube, TikTok, Twitter, etc. |
| OpenAI Whisper | Speech-to-text for automatic subtitles (base model) |

## Features

### Video Playback
- Embedded mpv player with play/pause, speed control (0.25x–4x), frame-accurate seeking
- Thumbnails and audio waveform on the timeline
- Drag & drop or open via file dialog / URL download

### Splitting & Trimming
- Split at playhead (`S`), remove before/after (`Q`/`W`), set in/out points (`I`/`O`)
- Toggle segments keep/delete (`X` or `Delete`)
- Segment list with visual highlighting and auto-scroll
- Full undo/redo (`Ctrl+Z` / `Ctrl+Shift+Z`)

### Subtitles (Speech-to-Text)
- Auto-transcribes when a video loads using OpenAI Whisper (base model)
- If an `.srt` file already exists next to the video, loads that instead
- Subtitle panel in the sidebar — click any line to seek to that moment
- Current subtitle highlights during playback
- `Ctrl+T` to manually re-trigger transcription

### Export
- Stream copy (no re-encode) for fast, lossless export
- If subtitles exist, offers to burn them in (white text, black outline, bottom margin)
- Subtitle burn-in re-encodes with libx264 CRF 18 — slower but includes subs in the video
- Timestamps auto-shift to match kept segments
- `Ctrl+E` to export

### Save / Load
- Quick save editing state (segments, position, selection) to `{save_dir}/.save_N`
- `Ctrl+S` to save, `Ctrl+L` to load latest save
- Saves are incremental (`.save_0`, `.save_1`, etc.)
- Restores segments, position, and keep/delete states

### Download
- Paste a URL to download via yt-dlp (YouTube, TikTok, Twitter, etc.)
- Downloads to the configured `download_dir` (see `settings.ini`)

## File Structure

```
quick_video/
  quick_video.py    -- the entire app (~4200 lines)
  wiki/             -- this documentation
  settings.ini      -- user config (gitignored)
```
