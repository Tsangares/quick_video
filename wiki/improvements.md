# Improvements Roadmap

## UI Improvements

### Keyframe visualization on timeline
Show keyframe positions as tick marks on the timeline. Stream copy can only cut at keyframes, so splits between keyframes cause the first/last frames to be wrong. Snapping splits to keyframes would eliminate this.

Extract keyframes: `ffprobe -select_streams v -show_entries frame=pts_time,key_frame -of csv`

### Timeline zoom and scroll
Currently the entire video duration maps to the widget width. For a 2-hour video, 1 second is ~1 pixel. Add scroll-wheel zoom (store `view_start`/`view_end`) and horizontal panning. Adjust `_time_to_x`/`_x_to_time` to use the visible range.

### Audio waveform overlay
Extract audio waveform via `ffmpeg -i file -ac 1 -filter:a aresample=8000 -f f32le -`, read the float samples, and draw as an overlay in `TimelineWidget.paintEvent`. Gives visual context for cutting dialogue and music.

### Export options dialog
Replace the simple confirmation `QMessageBox` with a proper dialog:
- Output format selector (mp4/mkv/webm)
- Mode: stream copy (fast) vs re-encode (CRF slider + resolution picker)
- Estimated output size
- Checkbox: open file after export

### Segment renaming
Double-click a segment label to edit it inline. The `Segment.label` field already exists.

### Export progress bar
Parse ffmpeg's `-progress pipe:1` output to get `out_time_us` and calculate real percentage instead of just status strings.

---

## Performance Improvements

### Parallel thumbnail extraction
Currently spawns a separate ffmpeg process per thumbnail (10-30 processes). Replace with:
- Single ffmpeg command using `select` filter: `ffmpeg -i file -vf "select='eq(n,X)+eq(n,Y)'" -vsync vfr -f image2pipe -`
- Or use mpv's `screenshot` command since it's already loaded

### Thumbnail caching
Store extracted thumbnails in `~/.cache/quickvideo/thumbs/` keyed by file path hash. Skip re-extraction when reopening the same file.

### Fix redundant ffprobe call
`get_duration()` runs `probe_video()` again even though `_load_video()` already stored the result in `self.video_info`. Pass the existing data instead.

---

## General Improvements

### Full undo/redo stack
Currently only undoes the last split by merging adjacent segments. Implement a proper undo stack: snapshot `self.segments` (small list of dataclasses) before each operation. Store as a list with a pointer. Ctrl+Z pops, Ctrl+Shift+Z pushes back.

### Default paths
All paths are now configurable via `settings.ini`. Defaults use `~/Videos` and `~/.quick_video`.

### Better mpv error recovery
Many mpv calls have bare `try/except: pass`. If mpv crashes (e.g., GPU driver issue), the app silently loses playback. Detect mpv death and offer to reinitialize.

### Session save/restore
Save editing state (filepath, segments, position) to a JSON sidecar file next to the video. Prompt to restore when reopening the same file.

### Config file
Move hardcoded values to `~/.config/quickvideo/config.json`:
- Temp download directory
- yt-dlp format string
- Default export format
- Window size/position
- Thumbnail count

### Clean up unused import
`QShortcut` is imported but never used (all shortcuts go through `keyPressEvent`).

---

## Rust Rewrite: No

**Don't rewrite.** The cost/benefit is strongly negative for a personal tool.

### What Rust would improve
- **Binary distribution**: Single static binary, no Python/PyQt runtime. This is the only real win.
- Everything else (startup, memory, CPU) is irrelevant -- all heavy work is in C (mpv, ffmpeg).

### What Rust would cost
- **3-4x more code**: 1400 lines Python -> 3000-5000 lines Rust (error handling, lifetimes, less mature GUI)
- **GUI immaturity**: No Rust GUI framework matches PyQt5 for embedded video. egui has no native mpv embedding. gtk-rs works but is verbose. Tauri means writing HTML/JS anyway.
- **Iteration speed**: Python edit-run is instant. Rust compilation adds 10-30s per change.
- **mpv bindings**: `python-mpv` is excellent. Rust equivalents (`libmpv-rs`) are less maintained.
- **2-4 weeks work** for feature parity vs the days the Python version took.

### If you want a binary
Use **PyInstaller** or **Nuitka** to package the Python app as a single binary. Solves the distribution problem without a rewrite.

### When Rust would make sense
- Custom video decoding (mpv handles it)
- Batch-processing thousands of files (this doesn't)
- Commercial distribution with minimal deps (it isn't)
