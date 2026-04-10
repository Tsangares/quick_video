# Usage Guide

## Opening a Video

Three ways:
1. **Open File tab** -- click "Open Video File", pick from file dialog
2. **Drag and drop** -- drop a video file onto the window
3. **Command line** -- `python3 quick_video.py /path/to/video.mp4`

Supported formats: mp4, mkv, avi, mov, webm, flv, ts, m4v (anything ffmpeg can read).

## Downloading from a URL

1. Switch to the **Download URL** tab (default on launch)
2. Paste a URL (YouTube, TikTok, Twitter, etc.)
3. Click **Download** or press Enter
4. Progress bar shows download status
5. Click **Cancel** (button turns red) to abort
6. Video auto-loads when download completes

Downloads are stored in the configured `download_dir` (see `settings.ini`). Only your exported files are permanent.

## Playback

- **Space** -- play/pause (mpv renders directly in the preview area)
- **J** -- slower (0.25x, 0.5x, 1x, 1.5x, 2x, 3x, 4x)
- **K** -- reset to 1x
- **L** -- faster

## Navigation

- **Left/Right arrows** -- step 1 second
- **Shift + arrows** -- fine step 0.1 seconds
- **Ctrl + arrows** -- big step 10 seconds
- **Home/End** -- jump to start/end
- **Click timeline** -- seek to that point
- **Type a time** in the "Jump to" field and press Enter (formats: `1:30`, `90`, `1:02:30.5`)

## Splitting

1. Navigate to the exact point you want to cut
2. Press **S** (or click "Split")
3. The current segment splits into two at the playhead
4. Both new segments are initially marked as "keep"

## Quick Trim

- **Q** -- split here + mark everything **before** as deleted (quick start trim)
- **W** -- split here + mark everything **after** as deleted (quick end trim)

These are the fastest way to trim a video to a specific section.

## Deleting Segments

- **X** or **Delete** -- toggle keep/delete on the selected segment
- Or click the checkbox in the segment list (right panel)
- Green = keep, red with stripes = delete

## In/Out Points

For fine-tuning segment boundaries without splitting:
- **I** -- set the selected segment's start to the current playhead position
- **O** -- set the selected segment's end to the current playhead position

## Segment Navigation

- **[** -- select previous segment (also seeks to its start)
- **]** -- select next segment
- Click a segment in the right panel to select + seek to it

## Undo

- **Ctrl+Z** -- merges the selected segment with the next one (reverses a split)

## Exporting

1. Press **Ctrl+E** or click "Export"
2. Confirm the summary (segments kept, time removed)
3. Choose output path and format (MP4 or MKV)
4. Export runs using **stream copy** -- no re-encoding, nearly instant

Stream copy means the video isn't re-encoded, so quality is preserved exactly. The trade-off is that cuts land on the nearest keyframe (usually within 1-2 seconds of where you split).

## Typical Workflow

```
1. Paste URL, download video
2. Space to preview, scrub to find the good part
3. Q to remove junk at the beginning
4. W to remove junk at the end
5. Ctrl+E to export just the part you want
```

For more complex edits:
```
1. Open a long video
2. S to split at each section boundary
3. X to toggle-delete the parts you don't want
4. Ctrl+E to export -- kept segments are joined automatically
```
