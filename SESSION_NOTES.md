# QuickVideo — Session Notes (2026-07-06)

## What this session did

Picked up the stale April 10 notes below, which flagged the video preview
(mpv shows nothing, black) as the critical unresolved bug. Reviewed the
uncommitted diff sitting in the tree since May 18 (263+/133-) and verified it.

### Root cause of the black-preview bug

Two widgets (`video_click_overlay` for click-to-seek, `_privacy_overlay` for
privacy blackout) were stacked directly on top of `video_widget`, the same
QWidget whose native `winId()` is handed to mpv for embedding. Layering
child widgets over that surface interfered with the embedded render target.

### Fix (now committed)

- Removed both overlay widgets entirely.
  - Click-to-seek: handled via `eventFilter` on `video_widget` itself
    (left half of the widget = seek -15s, right half = +15s).
  - Privacy mode: no more opaque overlay. Toggling privacy now sets
    `mpv_player.brightness = -100` (and back to `0`), and `_init_mpv`
    re-applies `brightness = -100` on load if privacy mode was already on.
- `CompressPanel`'s preview mpv init is deferred with
  `QTimer.singleShot(100, ...)` plus a `repaint()` + `processEvents()` call
  before `_init_preview_mpv()`, so the preview widget is mapped/visible
  before mpv attaches to its window. (Note: the *main* editor's `_init_mpv()`
  is not deferred — it didn't need to be once the overlay was removed; this
  timer only applies to the Compress tab's preview.)

### Other changes in the diff (also verified/committed)

- **ffmpeg progress parsing hardened.** Old code matched one big regex
  expecting fields in a fixed order; ffmpeg emits `dup=`/`drop=`/`q=` between
  fields and can print `N/A` for `fps`/`time` early on, which silently broke
  the whole match. New code (`_FFMPEG_FIELD_RES`) matches each field
  independently and skips a progress line only if `time` is missing/`N/A`.
  Verified by feeding real-world ffmpeg progress-line samples (including a
  `dup=/drop=` line and an all-`N/A` startup line) through the parser directly.
- **Concat/PTS drift fix.** Segment export commands add `-fflags +genpts` and
  `-avoid_negative_ts make_zero` to the per-segment ffmpeg calls and to all
  three concat call sites. The default (no speed change, not a short segment)
  path also changed from pure `-c copy` to `-c:v copy -c:a aac -b:a 192k`
  audio re-encode — mixed audio PTS across cut boundaries was the actual
  cause of moov durations getting wildly inflated after concat.
  **Verified end-to-end**: cut a synthetic 10s video into two 3s segments and
  concatenated them through the exact `_build_segment_cmd`/concat commands
  the app builds.
  - First pass with a sparse-keyframe source (only 2 I-frames in 10s, ffmpeg's
    default GOP) reproduced duration blowup even with the fix (each 3s part
    reported as several seconds too long) — but this is a property of
    stream-copy needing a keyframe at/near the cut point, not a bug in the
    fix itself.
  - Re-tested with a keyframe-dense source (`-g 15`, keyframe every 0.5s,
    representative of real camera/screen-recording footage): both parts
    reported the correct ~3.09s duration, and the concatenated output
    reported ~6.18s (matches 2 × 3.09s) with no drift. Fix confirmed working
    for realistic source material; stream-copy export quality still depends
    on the source having reasonably frequent keyframes near cut points
    (true of ffmpeg regardless of this app).
- **Export panel rework**: sticky bottom action bar (Cancel during export;
  Open Folder / Open Video / Delete Original / Done after), collapsible
  "Show details" section (bitrate/speed/size stats, source info, ffmpeg log)
  that auto-collapses on completion, Escape-to-dismiss once complete,
  fixed an off-by-one in which segment's progress bar was treated as
  "current" (`_current_part` is 1-based; the bar now correctly reads
  `active_idx = _current_part - 1`).
- **Export panel stacking bug fix**: `_show_export_panel` now tears down any
  prior `ExportPanel` before creating a new one — repeated exports without
  reopening the app used to stack orphaned panels in the layout and push the
  action bar off-screen.
- **Download/Queue tab auto-expand**: tabs now re-expand automatically if a
  download or queue progress update arrives while they're collapsed, and the
  collapse timer no longer collapses tabs while a download/queue job is
  still running.
- Minor: segment re-encode paths switch `-preset fast` → `-preset veryfast`
  and `-crf 18` → `-crf 20` (faster, slightly smaller); `-ss` moved before
  `-i` for input seeking (was already input-seek in the copy path, now
  consistent across all three segment-cmd branches).

## Verification performed

1. `python -m py_compile quick_video.py` — clean.
2. `python -c "import mpv, PyQt5"` — resolves against system Python 3.14
   (no project venv; this app has always run against system site-packages).
3. **Full visual confirmation**, not just clean-startup: generated a test
   clip with `ffmpeg -f lavfi testsrc=...`, launched
   `python quick_video.py <clip>` for real under the live Wayland/XWayland
   session, and captured a screenshot of the actual app window
   (`import -window <id>` against the XWayland window found via
   `xprop -root _NET_CLIENT_LIST` / `WM_CLASS=quick_video.py`). The
   screenshot showed the mpv preview rendering the SMPTE test pattern
   correctly, with thumbnails and waveform populated in the timeline —
   the previously-broken bug is confirmed fixed, not just "didn't crash."
   stderr showed only expected whisper CPU/FP32 warnings, no exceptions.
4. Confirmed `python-mpv`'s `brightness` property works as used for privacy
   mode (`mpv.MPV().brightness = -100` round-trips correctly); did not
   interactively exercise the privacy-mode keybinding itself (no input
   injection tool — `xdotool`/`ydotool`/`wtype` — available in this
   environment) but the code path is a one-line property set.
5. ffmpeg progress-parsing regex and the concat/PTS fix verified directly
   against ffmpeg (see above) — not just read, actually executed.
6. App process was killed cleanly after testing; confirmed no orphaned
   `quick_video`/`mpv`/`ffmpeg` processes remained.

## Files modified
- `quick_video.py` — all fixes above.
- `SESSION_NOTES.md` — this file.

## Open items / ideas for next session
- Named saves (save/restore a named edit session rather than only
  "last video").
- Auto-save of in-progress edits (segments/subtitles) so a crash doesn't
  lose work.
- Trash-bin for deleted originals — "Delete Original" in the export panel
  currently deletes for good; consider moving to a recoverable trash
  location instead.
- Export panel: the "Show details" collapse state isn't remembered between
  exports (always starts collapsed after completion) — probably fine, but
  worth a quick look if it feels repetitive in daily use.
- Interactive verification of the privacy-mode keybinding and click-to-seek
  eventFilter behavior would benefit from an input-injection tool
  (`ydotool`/`xdotool`) being available in this environment for future
  sessions.
