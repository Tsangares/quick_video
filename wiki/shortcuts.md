# Keyboard Shortcuts

All single-key shortcuts are disabled when typing in a text field (URL input, time input).

## Playback

| Key | Action |
|-----|--------|
| `Space` | Play / pause |
| `J` | Decrease speed (0.25x > 0.5x > 1x > 1.5x > 2x > 3x > 4x) |
| `K` | Reset speed to 1x |
| `L` | Increase speed |

## Navigation

| Key | Action |
|-----|--------|
| `Left` | Step back 1 second |
| `Right` | Step forward 1 second |
| `Shift+Left` | Step back 0.1 seconds |
| `Shift+Right` | Step forward 0.1 seconds |
| `Ctrl+Left` | Step back 10 seconds |
| `Ctrl+Right` | Step forward 10 seconds |
| `Home` | Jump to start |
| `End` | Jump to end |
| `[` | Select previous segment (seek to its start) |
| `]` | Select next segment (seek to its start) |

## Editing

| Key | Action |
|-----|--------|
| `S` | Split at playhead |
| `Q` | Split + remove everything before playhead |
| `W` | Split + remove everything after playhead |
| `X` / `Delete` | Toggle keep/delete on selected segment |
| `I` | Set in-point (trim start of selected segment to playhead) |
| `O` | Set out-point (trim end of selected segment to playhead) |
| `Ctrl+Z` | Undo last split |

## File Operations

| Key | Action |
|-----|--------|
| `Ctrl+O` | Open file dialog |
| `Ctrl+E` | Export |
| `Ctrl+S` | Quick save (segments + position to `{save_dir}/.save_N`) |
| `Ctrl+L` | Quick load (latest save file) |
| `Ctrl+T` | Generate subtitles (Whisper transcription) |

## Design Notes

Shortcut choices follow common conventions:
- **J/K/L** -- standard in video editors (Premiere, DaVinci, mpv)
- **I/O** -- industry-standard in/out points
- **Q/W** -- quick trim (remove before/after) -- chosen because they're next to the left hand and don't conflict with common shortcuts
- **S** -- split (mnemonic)
- **X** -- delete/cut (common in file managers and editors)
- **[/]** -- segment navigation (matches bracket-based navigation in other tools)
