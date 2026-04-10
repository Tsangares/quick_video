# Architecture

## Overview

Single-file Python app (`quick_video.py`, ~1400 lines). All heavy lifting is delegated to native C tools -- Python is glue code.

```
User Input
    |
    v
[PyQt5 GUI]  <-->  [mpv]       playback, seeking, speed
    |               embedded via python-mpv (WID)
    |
    +------------>  [ffmpeg]    thumbnails, probe, export
    |               via subprocess
    |
    +------------>  [yt-dlp]    URL downloads
                    via subprocess
```

## Code Structure

The file is organized into sections marked with `# --` dividers:

### Helpers (top of file)
- `fmt_time()`, `fmt_size()`, `parse_time()` -- formatting/parsing
- `probe_video()` -- runs `ffprobe -json`, returns parsed dict
- `get_duration()` -- extracts duration from probe data
- `extract_frame()` -- single-frame PNG extraction via ffmpeg pipe
- `find_ytdlp()` -- searches PATH and common locations
- `cleanup_temp_downloads()` -- deletes files older than 1hr from temp dir

### Data Model
```python
@dataclass
class Segment:
    start: float    # seconds
    end: float
    keep: bool      # True = keep on export, False = discard
    label: str
```

All editing is done by manipulating a list of `Segment` objects. Export iterates over `keep=True` segments and stream-copies each.

### Worker Threads (QThread subclasses)

| Worker | Job | Communication |
|--------|-----|---------------|
| `ThumbnailWorker` | Extract N frames for timeline strip | `thumbnail_ready(index, pixmap)` |
| `DownloadWorker` | Run yt-dlp, parse progress | `progress(msg, pct)`, `finished(path)`, `cancelled()` |
| `ExportWorker` | ffmpeg stream-copy per segment, concat | `progress(msg)`, `finished(path)` |

### Custom Widgets

**TimelineWidget** -- Custom QPainter widget:
- Thumbnail strip (top 72px)
- Segment bars (green = keep, red striped = delete)
- Time ruler with adaptive tick spacing
- Orange playhead with drag support
- Hover cursor with time tooltip

**SegmentWidget** -- Per-segment row in the right panel:
- Checkbox (keep/delete toggle)
- Label, start/end times, duration
- Click to select + seek

### Main Window (QuickVideoApp)

Two-pane layout via QSplitter (75/25):
- **Left**: tabs (open/download), video widget (mpv), position bar, timeline, step buttons, action buttons, status bar
- **Right**: scrollable segment list

## Data Flow

```
1. Load video (file or URL download)
      |
2. ffprobe extracts metadata (size, duration, resolution, codec)
      |
3. mpv loads file, paused at 0:00
      |
4. ThumbnailWorker extracts frames in background
      |
5. User splits/trims/deletes segments
   (manipulates self.segments list)
      |
6. Export: ffmpeg stream-copies each kept segment
   - 1 segment: single ffmpeg -ss/-t -c copy
   - N segments: copy each to temp, concat demuxer, merge
```

## Key Design Decisions

- **Stream copy only**: No re-encoding means export is nearly instant for any file size. Trade-off: cuts land on nearest keyframe, not frame-accurate.
- **mpv for playback**: Native C library, handles any codec, embedded via window ID. Much better than ffplay (no embed support on modern SDL2 builds).
- **Single file**: Keeps it simple. No package structure, no imports to chase. One file, one tool.
- **Debounced seeking**: 300ms debounce timer prevents hammering mpv with seeks during rapid arrow-key stepping.
- **Ephemeral downloads**: Downloads go to a configurable directory -- only the export matters.
