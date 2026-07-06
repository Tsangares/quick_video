#!/usr/bin/env python3
"""
QuickVideo — Local video splitter/trimmer/downloader using native ffmpeg.
No re-encoding for split/trim operations (stream copy = instant).
Handles files of any size since ffmpeg runs natively.
"""

import sys
import os
import subprocess
import json
import tempfile
import shutil
import re
import hashlib
import configparser
import enum
import uuid
import random
from pathlib import Path
from dataclasses import dataclass, field

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QScrollArea, QSplitter,
    QFrame, QSizePolicy, QMessageBox, QShortcut,
    QLineEdit, QCheckBox, QTabWidget, QProgressBar,
    QListWidget, QListWidgetItem,
    QDialog, QFormLayout, QGroupBox,
    QStackedWidget, QComboBox, QSlider, QGridLayout, QMenu,
    QPlainTextEdit,
)
from PyQt5.QtCore import (
    Qt, QTimer, QThread, pyqtSignal, QRect, QPoint, QEvent,
)
from PyQt5.QtGui import (
    QPixmap, QImage, QPainter, QColor, QFont, QPen, QKeySequence,
    QLinearGradient, QBrush,
)
import time as _time

# ── Locale fix for mpv (requires C numeric locale) ─────────────────────────
import locale
os.environ["LC_NUMERIC"] = "C"
locale.setlocale(locale.LC_NUMERIC, "C")

import mpv

# ── HiDPI ───────────────────────────────────────────────────────────────────
os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
if hasattr(Qt, 'AA_EnableHighDpiScaling'):
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
if hasattr(Qt, 'AA_UseHighDpiPixmaps'):
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)


# ── Settings ────────────────────────────────────────────────────────────────
SETTINGS_FILE = Path(__file__).resolve().parent / "settings.ini"
SETTINGS_EXAMPLE = Path(__file__).resolve().parent / "settings.example.ini"

def _load_settings():
    cfg = configparser.ConfigParser()
    if SETTINGS_FILE.exists():
        cfg.read(SETTINGS_FILE)
    elif SETTINGS_EXAMPLE.exists():
        cfg.read(SETTINGS_EXAMPLE)
    return cfg

_settings = _load_settings()

def _get_path(key, default):
    return Path(_settings.get("paths", key, fallback=default))

DOWNLOAD_DIR = _get_path("download_dir", str(Path.home() / "Videos"))
CACHE_DIR = _get_path("cache_dir", str(Path.home() / ".quick_video"))
WAVEFORM_CACHE_DIR = _get_path("waveform_dir", str(Path.home() / ".quick_video" / "audio"))
SAVE_DIR = _get_path("save_dir", str(Path.home() / "Videos"))
EXPORT_DIR = _get_path("export_dir", str(Path.home() / "Videos"))
RECENTS_FILE = _get_path("recents_file", str(Path.home() / ".quick_video" / ".recents.txt"))
FILE_MANAGER = _settings.get("paths", "file_manager", fallback="nemo")
PRIVACY_MODE = _settings.getboolean("ui", "privacy_mode", fallback=False)
QUEUE_FILE = CACHE_DIR / ".cache" / "queue.txt"


def _save_settings():
    """Write current settings back to settings.ini."""
    cfg = configparser.ConfigParser()
    cfg["paths"] = {
        "download_dir": str(DOWNLOAD_DIR),
        "cache_dir": str(CACHE_DIR),
        "waveform_dir": str(WAVEFORM_CACHE_DIR),
        "save_dir": str(SAVE_DIR),
        "export_dir": str(EXPORT_DIR),
        "recents_file": str(RECENTS_FILE),
        "file_manager": FILE_MANAGER,
    }
    cfg["ui"] = {
        "privacy_mode": str(PRIVACY_MODE).lower(),
    }
    if _settings.has_section("downloads"):
        cfg["downloads"] = dict(_settings.items("downloads"))
    with open(SETTINGS_FILE, "w") as f:
        cfg.write(f)

SPEED_STEPS = [0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0]


def _video_cache_key(filepath):
    """Generate a stable cache key from file path + size + mtime."""
    p = Path(filepath)
    stat = p.stat()
    raw = f"{p.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _cache_dir_for(filepath):
    """Return (and create) the per-video cache directory."""
    d = CACHE_DIR / _video_cache_key(filepath)
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_last_video(filepath):
    """Remember the last opened video for --resume."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / "last_video.txt").write_text(str(Path(filepath).resolve()))


def load_last_video():
    """Return the last opened video path, or None."""
    p = CACHE_DIR / "last_video.txt"
    if p.exists():
        path = p.read_text().strip()
        if os.path.isfile(path):
            return path
    return None


def save_download_link(url):
    """Append a download URL to the link history (deduped)."""
    cache = CACHE_DIR / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    link_file = cache / "link_list.txt"
    existing = set()
    if link_file.exists():
        existing = set(link_file.read_text().splitlines())
    if url not in existing:
        with open(link_file, 'a') as f:
            f.write(url + '\n')


def save_queue(urls):
    """Persist the pending queue to disk."""
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_FILE.write_text("\n".join(urls) + "\n" if urls else "")


def load_queue():
    """Load pending queue from disk."""
    if QUEUE_FILE.exists():
        return [u for u in QUEUE_FILE.read_text().splitlines() if u.strip()]
    return []


def load_recents(max_count=5):
    """Load recent video paths, most recent first, filtering out missing files."""
    if not RECENTS_FILE.exists():
        return []
    paths = []
    for line in RECENTS_FILE.read_text().splitlines():
        line = line.strip()
        if line and os.path.isfile(line) and line not in paths:
            paths.append(line)
    return paths[:max_count]


def add_recent(filepath):
    """Add a video to the top of the recents list."""
    resolved = str(Path(filepath).resolve())
    existing = []
    if RECENTS_FILE.exists():
        existing = [l.strip() for l in RECENTS_FILE.read_text().splitlines() if l.strip()]
    if resolved in existing:
        existing.remove(resolved)
    existing.insert(0, resolved)
    RECENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    RECENTS_FILE.write_text('\n'.join(existing[:20]) + '\n')


def clear_recents():
    """Delete the recents file."""
    if RECENTS_FILE.exists():
        RECENTS_FILE.unlink()


def get_download_dir():
    """Use aux_download_dir from settings if available and accessible, else download_dir."""
    aux = _settings.get("downloads", "aux_download_dir", fallback=None)
    if aux:
        p = Path(aux)
        if p.is_dir():
            return p
    return DOWNLOAD_DIR


# ── Helpers ──────────────────────────────────────────────────────────────────

def fmt_time(seconds):
    if seconds is None:
        return "--"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:05.2f}"
    return f"{m}:{s:05.2f}"


def fmt_size(nbytes):
    if nbytes is None:
        return "--"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


def parse_time(text):
    text = text.strip()
    parts = text.split(':')
    try:
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        else:
            return float(parts[0])
    except ValueError:
        return None


def probe_video(filepath):
    cmd = [
        'ffprobe', '-v', 'quiet', '-print_format', 'json',
        '-show_format', '-show_streams', str(filepath)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def get_duration(filepath):
    info = probe_video(filepath)
    if not info:
        return 0
    dur = info.get('format', {}).get('duration')
    if dur:
        return float(dur)
    for s in info.get('streams', []):
        if s.get('duration'):
            return float(s['duration'])
    return 0


def extract_frame(filepath, time_sec, width=320):
    cmd = [
        'ffmpeg', '-ss', str(max(0, time_sec)), '-i', str(filepath),
        '-vframes', '1', '-vf', f'scale={width}:-1',
        '-f', 'image2pipe', '-vcodec', 'png', '-v', 'quiet', '-'
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=10)
        if result.returncode == 0 and result.stdout:
            img = QImage()
            img.loadFromData(result.stdout, 'PNG')
            if not img.isNull():
                return QPixmap.fromImage(img)
    except Exception:
        pass
    return None


def find_ytdlp():
    for name in ['yt-dlp', 'yt-dlp_linux']:
        path = shutil.which(name)
        if path:
            return path
    for p in ['/usr/local/bin/yt-dlp', os.path.expanduser('~/.local/bin/yt-dlp')]:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def cleanup_temp_downloads():
    """Delete temp downloads older than 24 hours."""
    if not DOWNLOAD_DIR.exists():
        return
    cutoff = _time.time() - 86400  # 24 hours
    for f in DOWNLOAD_DIR.iterdir():
        try:
            if f.stat().st_mtime < cutoff:
                if f.is_file():
                    f.unlink()
                elif f.is_dir():
                    shutil.rmtree(f)
        except Exception:
            pass


def make_separator():
    sep = QFrame()
    sep.setFrameShape(QFrame.HLine)
    sep.setStyleSheet("background: #444; max-height: 1px; margin: 4px 0;")
    return sep


@dataclass
class Segment:
    start: float
    end: float
    keep: bool = True
    label: str = ""
    speed: float = 1.0


# ── Workers ──────────────────────────────────────────────────────────────────

class ThumbnailWorker(QThread):
    thumbnail_ready = pyqtSignal(int, object)
    finished_all = pyqtSignal()

    def __init__(self, filepath, times, width=240, cache_dir=None):
        super().__init__()
        self.filepath = filepath
        self.times = times
        self.width = width
        self.cache_dir = cache_dir
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        for i, t in enumerate(self.times):
            if self._cancel:
                break
            # Try loading from cache first
            cached = self._load_cached(i)
            if cached:
                self.thumbnail_ready.emit(i, cached)
                continue
            px = extract_frame(self.filepath, t, self.width)
            if px:
                self._save_cached(i, px)
                self.thumbnail_ready.emit(i, px)
        self.finished_all.emit()

    def _cache_path(self, index):
        if self.cache_dir:
            return self.cache_dir / f"thumb_{index:04d}.png"
        return None

    def _load_cached(self, index):
        p = self._cache_path(index)
        if p and p.exists():
            px = QPixmap(str(p))
            if not px.isNull():
                return px
        return None

    def _save_cached(self, index, pixmap):
        p = self._cache_path(index)
        if p:
            try:
                pixmap.save(str(p), "PNG")
            except Exception:
                pass


class DownloadWorker(QThread):
    progress = pyqtSignal(str, float)
    finished = pyqtSignal(str)
    error = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, url, output_dir, ytdlp_path):
        super().__init__()
        self.url = url
        self.output_dir = output_dir
        self.ytdlp_path = ytdlp_path
        self._proc = None
        self._cancel = False

    def cancel(self):
        self._cancel = True
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def run(self):
        try:
            out_template = os.path.join(self.output_dir, '%(title).80s.%(ext)s')
            cmd = [
                self.ytdlp_path,
                '-f', 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                '--merge-output-format', 'mp4',
                '--no-playlist', '--newline',
                '-o', out_template, self.url,
            ]
            self.progress.emit("Starting download...", -1)
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1
            )
            for line in self._proc.stdout:
                if self._cancel:
                    break
                line = line.strip()
                if not line:
                    continue
                m = re.search(r'(\d+\.?\d*)%', line)
                if m:
                    self.progress.emit(line, float(m.group(1)))
                else:
                    self.progress.emit(line, -1)
            self._proc.wait()
            if self._cancel:
                self.cancelled.emit()
                return
            if self._proc.returncode != 0:
                self.error.emit(f"yt-dlp exited with code {self._proc.returncode}")
                return
            files = sorted(Path(self.output_dir).glob('*'), key=lambda f: f.stat().st_mtime, reverse=True)
            video_files = [f for f in files if f.suffix.lower() in ('.mp4', '.mkv', '.webm', '.avi', '.mov')]
            if video_files:
                self.finished.emit(str(video_files[0]))
            else:
                self.error.emit("Download completed but no video file found")
        except Exception as e:
            if not self._cancel:
                self.error.emit(str(e))


class ExportWorker(QThread):
    progress = pyqtSignal(str)
    detail = pyqtSignal(dict)
    finished = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, filepath, segments, output_path, srt_path=None, music_path=None, music_volume=50):
        super().__init__()
        self.filepath = filepath
        self.segments = segments
        self.output_path = output_path
        self.srt_path = srt_path
        self.music_path = music_path
        self.music_volume = music_volume
        self._cancel = False
        self._process = None
        self._start_time = None
        self._total_duration = 0.0

    def cancel(self):
        self._cancel = True
        if self._process and self._process.poll() is None:
            self._process.kill()

    def _sub_filter(self, srt_path, offset=0):
        """Build ffmpeg subtitles filter with white text, margin from bottom."""
        # Escape special chars for ffmpeg filter syntax
        escaped = srt_path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        style = "FontSize=24,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,MarginV=40"
        return f"subtitles='{escaped}':force_style='{style}'"

    # Each field extracted independently — ffmpeg may emit them in varying
    # order and with extra fields (dup=, drop=, q=) in between. fps/bitrate
    # can also be N/A early in encoding.
    _FFMPEG_FIELD_RES = {
        'frame':   re.compile(r'frame=\s*(\d+)'),
        'fps':     re.compile(r'fps=\s*([\d.]+|N/A)'),
        'size':    re.compile(r'(?:L?size|total_size)=\s*(\S+)'),
        'time':    re.compile(r'time=\s*([\d:.]+|N/A)'),
        'bitrate': re.compile(r'bitrate=\s*(\S+)'),
        'speed':   re.compile(r'speed=\s*(\S+)'),
    }

    def _parse_ffmpeg_time(self, ts):
        parts = ts.split(':')
        try:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        except (ValueError, IndexError):
            return 0.0

    def _run_cmd(self, cmd, label="ffmpeg"):
        if self._cancel:
            return False
        self.progress.emit(f"Running: {' '.join(cmd)}")
        self._process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stderr_buf = b""
        import select
        while self._process.poll() is None:
            if self._cancel:
                self._process.kill()
                self._process.wait()
                return False
            try:
                ready, _, _ = select.select([self._process.stderr], [], [], 0.3)
                if ready:
                    chunk = self._process.stderr.read1(4096) if hasattr(self._process.stderr, 'read1') else os.read(self._process.stderr.fileno(), 4096)
                    if chunk:
                        stderr_buf += chunk
                        # ffmpeg uses \r for progress lines
                        lines = stderr_buf.replace(b'\r', b'\n').split(b'\n')
                        stderr_buf = lines[-1]
                        for line in lines[:-1]:
                            text = line.decode(errors='replace').strip()
                            if not text:
                                continue
                            if 'time=' not in text or 'frame=' not in text:
                                continue
                            fields = {}
                            for key, rx in self._FFMPEG_FIELD_RES.items():
                                m = rx.search(text)
                                if m:
                                    fields[key] = m.group(1)
                            if 'time' not in fields or fields['time'] == 'N/A':
                                continue
                            fps_raw = fields.get('fps', '0')
                            self.detail.emit({
                                'frame': int(fields.get('frame', '0')),
                                'fps': float(fps_raw) if fps_raw != 'N/A' else 0,
                                'size': fields.get('size', 'N/A'),
                                'time': self._parse_ffmpeg_time(fields['time']),
                                'bitrate': fields.get('bitrate', 'N/A'),
                                'speed': fields.get('speed', 'N/A'),
                                'elapsed': _time.time() - self._start_time if self._start_time else 0,
                                'total_duration': self._total_duration,
                            })
            except Exception:
                try:
                    self._process.wait(timeout=0.3)
                except subprocess.TimeoutExpired:
                    pass
        # Read remaining stderr
        remaining = self._process.stderr.read().decode(errors='replace')
        stderr_buf_str = stderr_buf.decode(errors='replace') + remaining
        if self._process.returncode != 0 and not self._cancel:
            self.error.emit(f"{label} error (code {self._process.returncode}):\n{stderr_buf_str[-1000:]}")
            return False
        return True

    def run(self):
        kept = [s for s in self.segments if s.keep]
        if not kept:
            self.error.emit("No segments selected to keep")
            return
        self._start_time = _time.time()
        self._total_duration = sum((s.end - s.start) / s.speed for s in kept)
        try:
            if self.music_path:
                self._run_with_music(kept)
            elif self.srt_path:
                self._run_with_subtitles(kept)
            else:
                self._run_stream_copy(kept)
        except Exception as e:
            if not self._cancel:
                self.error.emit(str(e))
        if self._cancel:
            # Clean up partial output
            try:
                Path(self.output_path).unlink(missing_ok=True)
            except Exception:
                pass
            self.error.emit("Export cancelled")

    def _run_with_music(self, kept):
        """Export with music mixed in (requires audio re-encode, video stream-copy)."""
        tmpdir = tempfile.mkdtemp(prefix="quickvideo_")
        # First produce intermediate via segment commands (handles speed)
        intermediate = os.path.join(tmpdir, "intermediate.mp4")
        has_speed = any(s.speed != 1.0 for s in kept)
        if len(kept) == 1:
            seg = kept[0]
            cmd = self._build_segment_cmd(seg, intermediate)
            self.progress.emit(f"Cutting: {fmt_time(seg.start)} -> {fmt_time(seg.end)}")
            if not self._run_cmd(cmd, "Cut"):
                return
        else:
            parts = []
            for i, seg in enumerate(kept):
                part_path = os.path.join(tmpdir, f"part_{i:04d}.mp4")
                cmd = self._build_segment_cmd(seg, part_path)
                self.progress.emit(f"Part {i+1}/{len(kept)}: {fmt_time(seg.start)} -> {fmt_time(seg.end)}")
                if not self._run_cmd(cmd, f"Part {i+1}"):
                    return
                parts.append(part_path)
            list_path = os.path.join(tmpdir, "concat.txt")
            with open(list_path, 'w') as f:
                for p in parts:
                    f.write(f"file '{p}'\n")
            self.progress.emit("Joining segments...")
            cmd = [
                'ffmpeg', '-y', '-fflags', '+genpts',
                '-f', 'concat', '-safe', '0',
                '-i', list_path, '-c', 'copy',
                '-avoid_negative_ts', 'make_zero',
                '-movflags', '+faststart', intermediate
            ]
            if not self._run_cmd(cmd, "Concat"):
                return

        # Now mix music into the intermediate
        vol = self.music_volume / 100.0
        if self.srt_path:
            # Re-encode video for subtitles + mix music
            vf = self._sub_filter(self.srt_path)
            cmd = [
                'ffmpeg', '-y', '-i', intermediate, '-i', self.music_path,
                '-filter_complex',
                f'[1:a]volume={vol}[music];[0:a][music]amix=inputs=2:duration=first[aout]',
                '-map', '0:v', '-map', '[aout]',
                '-vf', vf,
                '-c:v', 'libx264', '-crf', '18', '-preset', 'fast',
                '-c:a', 'aac',
                '-movflags', '+faststart', str(self.output_path)
            ]
        else:
            # Video stream-copy, only re-encode audio
            cmd = [
                'ffmpeg', '-y', '-i', intermediate, '-i', self.music_path,
                '-filter_complex',
                f'[1:a]volume={vol}[music];[0:a][music]amix=inputs=2:duration=first[aout]',
                '-map', '0:v', '-map', '[aout]',
                '-c:v', 'copy', '-c:a', 'aac',
                '-movflags', '+faststart', str(self.output_path)
            ]
        self.progress.emit("Mixing music track...")
        if not self._run_cmd(cmd, "Music mix"):
            return

        # Cleanup
        try: shutil.rmtree(tmpdir)
        except Exception: pass
        self.finished.emit(str(self.output_path))

    def _build_segment_cmd(self, seg, out_path):
        """Build ffmpeg command for a single segment. Stream-copies when possible; re-encodes only when speed changes or for very short segments."""
        if seg.speed != 1.0:
            # Re-encode with speed change; atempo preserves pitch
            vf = f"setpts={1.0/seg.speed}*PTS"
            # atempo only accepts 0.5-100.0; chain filters for extreme values
            atempo_filters = []
            remaining = seg.speed
            while remaining > 2.0:
                atempo_filters.append("atempo=2.0")
                remaining /= 2.0
            atempo_filters.append(f"atempo={remaining}")
            af = ",".join(atempo_filters)
            return [
                'ffmpeg', '-y',
                '-ss', str(seg.start),
                '-i', str(self.filepath),
                '-t', str(seg.end - seg.start),
                '-vf', vf, '-af', af,
                '-c:v', 'libx264', '-crf', '20', '-preset', 'veryfast',
                '-c:a', 'aac',
                '-avoid_negative_ts', 'make_zero',
                '-movflags', '+faststart',
                out_path
            ]
        elif (seg.end - seg.start) < 3.0:
            # Short segments: re-encode to avoid keyframe stuttering
            return [
                'ffmpeg', '-y',
                '-ss', str(seg.start),
                '-i', str(self.filepath),
                '-t', str(seg.end - seg.start),
                '-c:v', 'libx264', '-crf', '20', '-preset', 'veryfast',
                '-c:a', 'aac',
                '-avoid_negative_ts', 'make_zero',
                '-movflags', '+faststart',
                out_path
            ]
        else:
            # Video stream-copy (snaps to keyframe) + audio re-encode.
            # Pure -c copy lets audio frames drift across cut boundaries and
            # leaves per-part PTS inconsistent, which after concat produces
            # MP4s with wildly inflated duration in moov (a "30s" clip
            # reported as 5 min) and progressive A/V desync. Re-encoding
            # audio + regenerating PTS keeps each part clean for concat.
            return [
                'ffmpeg', '-y',
                '-fflags', '+genpts',
                '-ss', str(seg.start),
                '-i', str(self.filepath),
                '-t', str(seg.end - seg.start),
                '-c:v', 'copy',
                '-c:a', 'aac', '-b:a', '192k',
                '-avoid_negative_ts', 'make_zero',
                '-movflags', '+faststart',
                out_path
            ]

    def _run_stream_copy(self, kept):
        has_speed = any(s.speed != 1.0 for s in kept)
        if len(kept) == 1 and not has_speed:
            seg = kept[0]
            cmd = self._build_segment_cmd(seg, str(self.output_path))
            self.progress.emit(f"Exporting: {fmt_time(seg.start)} -> {fmt_time(seg.end)}")
            if not self._run_cmd(cmd):
                return
        else:
            tmpdir = tempfile.mkdtemp(prefix="quickvideo_")
            parts = []
            for i, seg in enumerate(kept):
                part_path = os.path.join(tmpdir, f"part_{i:04d}.mp4")
                cmd = self._build_segment_cmd(seg, part_path)
                speed_tag = f" @{seg.speed}x" if seg.speed != 1.0 else ""
                self.progress.emit(f"Part {i+1}/{len(kept)}: {fmt_time(seg.start)} -> {fmt_time(seg.end)}{speed_tag}")
                if not self._run_cmd(cmd, f"Part {i+1}"):
                    return
                parts.append(part_path)
            list_path = os.path.join(tmpdir, "concat.txt")
            with open(list_path, 'w') as f:
                for p in parts:
                    f.write(f"file '{p}'\n")
            self.progress.emit("Joining segments...")
            cmd = [
                'ffmpeg', '-y', '-fflags', '+genpts',
                '-f', 'concat', '-safe', '0',
                '-i', list_path, '-c', 'copy',
                '-avoid_negative_ts', 'make_zero',
                '-movflags', '+faststart', str(self.output_path)
            ]
            if not self._run_cmd(cmd, "Concat"):
                return
            for p in parts:
                try: os.unlink(p)
                except: pass
            try: os.unlink(list_path)
            except: pass
            try: os.rmdir(tmpdir)
            except: pass
        self.finished.emit(str(self.output_path))

    def _run_with_subtitles(self, kept):
        """Export with burned-in subtitles (requires re-encode)."""
        tmpdir = tempfile.mkdtemp(prefix="quickvideo_")
        has_speed = any(s.speed != 1.0 for s in kept)
        # First: extract segments into a single intermediate file
        if len(kept) == 1:
            seg = kept[0]
            intermediate = os.path.join(tmpdir, "intermediate.mp4")
            cmd = self._build_segment_cmd(seg, intermediate)
            self.progress.emit(f"Cutting: {fmt_time(seg.start)} -> {fmt_time(seg.end)}")
            if not self._run_cmd(cmd, "Cut"):
                return
        else:
            parts = []
            for i, seg in enumerate(kept):
                part_path = os.path.join(tmpdir, f"part_{i:04d}.mp4")
                cmd = self._build_segment_cmd(seg, part_path)
                self.progress.emit(f"Part {i+1}/{len(kept)}: {fmt_time(seg.start)} -> {fmt_time(seg.end)}")
                if not self._run_cmd(cmd, f"Part {i+1}"):
                    return
                parts.append(part_path)
            list_path = os.path.join(tmpdir, "concat.txt")
            with open(list_path, 'w') as f:
                for p in parts:
                    f.write(f"file '{p}'\n")
            intermediate = os.path.join(tmpdir, "intermediate.mp4")
            self.progress.emit("Joining segments...")
            cmd = [
                'ffmpeg', '-y', '-fflags', '+genpts',
                '-f', 'concat', '-safe', '0',
                '-i', list_path, '-c', 'copy',
                '-avoid_negative_ts', 'make_zero',
                '-movflags', '+faststart', intermediate
            ]
            if not self._run_cmd(cmd, "Concat"):
                return

        # Shift SRT timestamps to match the cut video
        shifted_srt = os.path.join(tmpdir, "subs.srt")
        self._shift_srt(kept, shifted_srt)

        # Re-encode with subtitles burned in
        self.progress.emit("Burning in subtitles (re-encoding)...")
        vf = self._sub_filter(shifted_srt)
        cmd = [
            'ffmpeg', '-y', '-i', intermediate,
            '-vf', vf, '-c:v', 'libx264', '-crf', '18',
            '-preset', 'fast', '-c:a', 'copy',
            '-movflags', '+faststart', str(self.output_path)
        ]
        if not self._run_cmd(cmd, "Subtitle burn"):
            return

        # Cleanup
        try: shutil.rmtree(tmpdir)
        except: pass
        self.finished.emit(str(self.output_path))

    def _shift_srt(self, kept, output_srt):
        """Create a new SRT with timestamps shifted to match the kept segments."""
        try:
            with open(self.srt_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            return
        blocks = content.strip().split("\n\n")
        new_index = 1
        lines_out = []
        # Build a mapping: for each kept segment, what's its offset in the output
        output_offset = 0.0
        for seg in kept:
            for block in blocks:
                blines = block.strip().split("\n")
                if len(blines) < 3:
                    continue
                match = re.match(r"(\d+:\d+:\d+[,\.]\d+)\s*-->\s*(\d+:\d+:\d+[,\.]\d+)", blines[1])
                if not match:
                    continue
                sub_start = self._parse_srt(match.group(1))
                sub_end = self._parse_srt(match.group(2))
                # Check if subtitle overlaps this segment
                if sub_end <= seg.start or sub_start >= seg.end:
                    continue
                # Clamp to segment bounds
                clamped_start = max(sub_start, seg.start)
                clamped_end = min(sub_end, seg.end)
                # Shift to output timeline
                new_start = output_offset + (clamped_start - seg.start)
                new_end = output_offset + (clamped_end - seg.start)
                text = " ".join(blines[2:])
                lines_out.append(f"{new_index}\n{self._fmt_srt(new_start)} --> {self._fmt_srt(new_end)}\n{text}\n")
                new_index += 1
            output_offset += seg.end - seg.start
        with open(output_srt, "w", encoding="utf-8") as f:
            f.write("\n".join(lines_out))

    @staticmethod
    def _parse_srt(s):
        s = s.replace(",", ".")
        parts = s.split(":")
        return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])

    @staticmethod
    def _fmt_srt(seconds):
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds % 1) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


class SubtitleWorker(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, filepath, output_path, model_name="base"):
        super().__init__()
        self.filepath = filepath
        self.output_path = output_path
        self.model_name = model_name

    def run(self):
        try:
            import whisper
            self.progress.emit(f"Loading whisper model '{self.model_name}'...")
            model = whisper.load_model(self.model_name)
            self.progress.emit("Transcribing audio (this may take a while)...")
            result = model.transcribe(self.filepath, verbose=False)
            segments = result.get("segments", [])
            if not segments:
                self.error.emit("No speech detected in the video.")
                return
            self.progress.emit(f"Writing {len(segments)} subtitle segments...")
            with open(self.output_path, "w", encoding="utf-8") as f:
                for i, seg in enumerate(segments, 1):
                    start = self._fmt_srt_time(seg["start"])
                    end = self._fmt_srt_time(seg["end"])
                    text = seg["text"].strip()
                    f.write(f"{i}\n{start} --> {end}\n{text}\n\n")
            self.finished.emit(str(self.output_path))
        except Exception as e:
            self.error.emit(str(e))

    @staticmethod
    def _fmt_srt_time(seconds):
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds % 1) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"



class WaveformWorker(QThread):
    waveform_ready = pyqtSignal(list)

    def __init__(self, filepath, num_samples=2000):
        super().__init__()
        self.filepath = filepath
        self.num_samples = num_samples
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def _cache_path(self):
        h = hashlib.sha256(str(self.filepath).encode()).hexdigest()[:16]
        return WAVEFORM_CACHE_DIR / f"{h}_{self.num_samples}.json"

    def run(self):
        cache = self._cache_path()
        if cache.exists():
            try:
                peaks = json.loads(cache.read_text())
                if not self._cancel:
                    self.waveform_ready.emit(peaks)
                return
            except Exception:
                pass
        try:
            cmd = [
                'ffmpeg', '-i', str(self.filepath),
                '-ac', '1', '-filter:a', f'aresample=8000',
                '-f', 's16le', '-acodec', 'pcm_s16le', 'pipe:1'
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=120)
            if self._cancel or result.returncode != 0:
                return
            import struct
            raw = result.stdout
            samples = struct.unpack(f'<{len(raw)//2}h', raw)
            chunk = max(1, len(samples) // self.num_samples)
            peaks = []
            for i in range(0, len(samples), chunk):
                if self._cancel:
                    return
                block = samples[i:i+chunk]
                peaks.append(max(abs(s) for s in block) if block else 0)
            # Normalize to 0.0-1.0
            max_peak = max(peaks) if peaks else 1
            if max_peak > 0:
                peaks = [p / max_peak for p in peaks]
            # Cache (best-effort)
            try:
                WAVEFORM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(peaks))
            except Exception:
                pass
            if not self._cancel:
                self.waveform_ready.emit(peaks)
        except Exception as e:
            print(f"Waveform extraction failed: {e}")


# ── Timeline Widget ──────────────────────────────────────────────────────────

class TimelineWidget(QWidget):
    position_changed = pyqtSignal(float)
    seek_finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.duration = 0
        self.position = 0
        self.segments = []
        self.thumbnails = {}
        self.thumb_count = 0
        self.waveform = []
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self._dragging = False
        self._panning = False
        self._pan_last_x = 0
        self._hover_time = None
        self._snap_threshold_px = 8
        self.privacy_mode = PRIVACY_MODE
        self.setFixedHeight(180)  # extra space for waveform + minimap
        self._zoom = 1.0
        self._offset = 0.0
        self._zoom_min = 1.0
        self._zoom_max = 50.0

    def set_duration(self, dur):
        self.duration = dur
        self._zoom = 1.0
        self._offset = 0.0
        self.update()

    def set_position(self, pos):
        self.position = pos
        self._ensure_playhead_visible()
        self.update()

    def set_segments(self, segments):
        self.segments = segments
        self.update()

    def set_thumbnail(self, index, pixmap):
        self.thumbnails[index] = pixmap
        self.update()

    def set_waveform(self, peaks):
        self.waveform = peaks
        self.update()

    def _snap_time(self, t):
        """Snap time to nearest segment boundary if within threshold."""
        if not self.segments:
            return t
        for seg in self.segments:
            for edge in (seg.start, seg.end):
                edge_x = self._time_to_x(edge)
                t_x = self._time_to_x(t)
                if abs(edge_x - t_x) <= self._snap_threshold_px:
                    return edge
        return t

    def _time_to_x(self, t):
        if self.duration <= 0:
            return 0
        virtual_width = self.width() * self._zoom
        return int((t / self.duration) * virtual_width - self._offset)

    def _x_to_time(self, x):
        if self.width() <= 0 or self.duration <= 0:
            return 0
        virtual_width = self.width() * self._zoom
        return max(0, min(self.duration, ((x + self._offset) / virtual_width) * self.duration))

    def paintEvent(self, event):
        if self.duration <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        minimap_h = 16 if self._zoom > 1.0 else 0
        thumb_h = 72
        wave_h = 28
        seg_y = minimap_h + thumb_h + wave_h + 4
        seg_h = 32
        ruler_y = seg_y + seg_h + 4
        thumb_y = minimap_h

        # ── Minimap (only when zoomed) ──
        if self._zoom > 1.0:
            p.fillRect(0, 0, w, minimap_h, QColor(30, 30, 30))
            _mini_speed_colors = {
                1.25: QColor(76, 175, 80, 140),
                1.5:  QColor(255, 235, 59, 140),
                1.75: QColor(255, 152, 0, 140),
                2.0:  QColor(183, 28, 28, 140),
            }
            for seg in self.segments:
                mx1 = int((seg.start / self.duration) * w)
                mx2 = int((seg.end / self.duration) * w)
                if seg.keep:
                    color = _mini_speed_colors.get(seg.speed, QColor(76, 175, 80, 120))
                else:
                    color = QColor(244, 67, 54, 80)
                p.fillRect(mx1, 0, mx2 - mx1, minimap_h, color)
            vx1 = int((self._offset / (w * self._zoom)) * w)
            vw = int(w / self._zoom)
            p.setPen(QPen(QColor(255, 165, 0, 180), 1))
            p.setBrush(QColor(255, 165, 0, 30))
            p.drawRect(vx1, 0, vw, minimap_h - 1)
            mpx = int((self.position / self.duration) * w)
            p.setPen(QPen(QColor(255, 165, 0), 2))
            p.drawLine(mpx, 0, mpx, minimap_h)
            p.setBrush(Qt.NoBrush)

        # ── Thumbnails ──
        if self.thumbnails and not self.privacy_mode:
            virtual_width = w * self._zoom
            tw = max(1, int(virtual_width) // max(len(self.thumbnails), 1))
            for i, px in sorted(self.thumbnails.items()):
                x = int(i * (virtual_width / max(self.thumb_count, 1)) - self._offset)
                if x + tw < 0 or x > w:
                    continue
                p.drawPixmap(QRect(x, thumb_y, tw + 1, thumb_h), px)
        elif self.privacy_mode:
            # Dark fill in place of thumbnails
            p.fillRect(0, thumb_y, w, thumb_h, QColor(20, 20, 20))

        # ── Waveform ──
        if self.waveform:
            virtual_width = w * self._zoom
            n = len(self.waveform)
            wave_base = thumb_y + thumb_h + wave_h
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 180, 255, 100))
            for i in range(n):
                x1 = int(i / n * virtual_width - self._offset)
                x2 = int((i + 1) / n * virtual_width - self._offset)
                if x2 < 0 or x1 > w:
                    continue
                bar_w = max(1, x2 - x1)
                bar_h = max(1, int(self.waveform[i] * wave_h))
                p.drawRect(x1, wave_base - bar_h, bar_w, bar_h)

        # ── Segment bars ──
        _speed_colors = {
            1.25: QColor(76, 175, 80, 200),   # green
            1.5:  QColor(255, 235, 59, 200),   # yellow
            1.75: QColor(255, 152, 0, 200),    # orange
            2.0:  QColor(183, 28, 28, 200),    # dark red
        }
        for seg in self.segments:
            x1 = self._time_to_x(seg.start)
            x2 = self._time_to_x(seg.end)
            if seg.keep:
                color = _speed_colors.get(seg.speed, QColor(76, 175, 80, 160))
                p.fillRect(x1, seg_y, x2 - x1, seg_h, color)
            else:
                p.fillRect(x1, seg_y, x2 - x1, seg_h, QColor(244, 67, 54, 120))
                p.setPen(QPen(QColor(244, 67, 54, 180), 1))
                for lx in range(max(x1, 0), min(x2, w), 10):
                    p.drawLine(lx, seg_y, min(lx + seg_h, x2), seg_y + seg_h)
            p.setPen(QPen(QColor(255, 255, 255, 200), 1))
            p.drawRect(x1, seg_y, x2 - x1, seg_h)

        # ── Ruler ──
        p.setPen(QPen(QColor(180, 180, 180), 1))
        step = self._nice_step(self.duration, w * self._zoom)
        t = 0
        p.setFont(QFont("monospace", 9))
        while t <= self.duration:
            x = self._time_to_x(t)
            p.drawLine(x, ruler_y, x, ruler_y + 8)
            p.drawText(x + 3, ruler_y + 18, fmt_time(t))
            t += step

        # ── Playhead ──
        px_x = self._time_to_x(self.position)
        p.setPen(QPen(QColor(255, 165, 0), 3))
        p.drawLine(px_x, thumb_y, px_x, h)
        p.setBrush(QColor(255, 165, 0))
        p.drawPolygon(QPoint(px_x - 7, thumb_y), QPoint(px_x + 7, thumb_y), QPoint(px_x, thumb_y + 10))

        # ── Snap indicators ──
        if self._hover_time is not None:
            for seg in self.segments:
                for edge in (seg.start, seg.end):
                    ex = self._time_to_x(edge)
                    hx_check = self._time_to_x(self._hover_time)
                    if abs(ex - hx_check) <= self._snap_threshold_px:
                        p.setPen(QPen(QColor(255, 255, 0, 150), 2, Qt.DotLine))
                        p.drawLine(ex, thumb_y, ex, h)

        # ── Hover ──
        if self._hover_time is not None:
            hx = self._time_to_x(self._hover_time)
            p.setPen(QPen(QColor(255, 255, 255, 80), 1, Qt.DashLine))
            p.drawLine(hx, thumb_y, hx, h)
            p.setPen(QColor(255, 255, 255, 200))
            p.setFont(QFont("monospace", 9))
            p.drawText(hx + 4, h - 4, fmt_time(self._hover_time))

        p.end()

    def _nice_step(self, duration, width):
        if duration <= 0:
            return 1
        target_marks = max(width // 100, 2)
        raw = duration / target_marks
        for step in [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600]:
            if raw <= step:
                return step
        return 600

    def mousePressEvent(self, event):
        # Steal focus from text fields so keyboard shortcuts work
        self.setFocus()
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self.position_changed.emit(self._snap_time(self._x_to_time(event.x())))
        elif event.button() == Qt.RightButton and self._zoom > 1.0:
            self._panning = True
            self._pan_last_x = event.x()

    def mouseMoveEvent(self, event):
        raw_time = self._x_to_time(event.x())
        self._hover_time = self._snap_time(raw_time)
        if self._panning:
            dx = event.x() - self._pan_last_x
            self._offset -= dx
            self._clamp_offset()
            self._pan_last_x = event.x()
            self.update()
        elif self._dragging:
            self.position_changed.emit(self._hover_time)
            self.update()
        else:
            self.update()

    def mouseReleaseEvent(self, event):
        if self._dragging:
            self._dragging = False
            self.seek_finished.emit()
        if self._panning:
            self._panning = False

    def wheelEvent(self, event):
        if self.duration <= 0:
            return
        if event.modifiers() & Qt.ControlModifier:
            old_zoom = self._zoom
            delta = event.angleDelta().y()
            factor = 1.15 if delta > 0 else 1 / 1.15
            new_zoom = max(self._zoom_min, min(self._zoom_max, self._zoom * factor))
            mouse_x = event.x()
            time_at_cursor = self._x_to_time(mouse_x)
            self._zoom = new_zoom
            virtual_width = self.width() * self._zoom
            self._offset = (time_at_cursor / self.duration) * virtual_width - mouse_x
            self._clamp_offset()
            self.update()
            event.accept()
        elif self._zoom > 1.0:
            delta = event.angleDelta().y()
            self._offset -= delta
            self._clamp_offset()
            self.update()
            event.accept()
        else:
            super().wheelEvent(event)

    def _clamp_offset(self):
        virtual_width = self.width() * self._zoom
        max_offset = virtual_width - self.width()
        self._offset = max(0, min(max_offset, self._offset))

    def _ensure_playhead_visible(self):
        if self._zoom <= 1.0:
            return
        px_x = self._time_to_x(self.position)
        margin = 40
        if px_x < margin:
            self._offset += px_x - margin
            self._clamp_offset()
        elif px_x > self.width() - margin:
            self._offset += px_x - (self.width() - margin)
            self._clamp_offset()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._clamp_offset()

    def leaveEvent(self, event):
        self._hover_time = None
        self.update()


# ── Export Panel ─────────────────────────────────────────────────────────────

class SegmentProgressBar(QWidget):
    """Custom progress bar showing equal-width blocks per segment."""

    def __init__(self, num_segments, parent=None):
        super().__init__(parent)
        self._num = max(1, num_segments)
        self.completed_parts = 0
        self.current_frac = 0.0
        self._glow_phase = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(40)
        self.setFixedHeight(28)
        self.setMinimumWidth(100)
        self._done = False

    def _tick(self):
        import math
        self._glow_phase = (self._glow_phase + 0.08) % (2 * math.pi)
        self.update()

    def set_progress(self, completed_parts, current_frac=0.0):
        self.completed_parts = completed_parts
        self.current_frac = max(0.0, min(1.0, current_frac))
        self.update()

    def set_complete(self):
        self._done = True
        self.completed_parts = self._num
        self.current_frac = 0.0
        self._timer.stop()
        self.update()

    def paintEvent(self, event):
        import math
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#1a1a1a"))
        p.drawRoundedRect(0, 0, w, h, 6, 6)

        margin = 3
        gap = 2
        inner_w = w - 2 * margin
        inner_h = h - 2 * margin
        seg_w = max(4, (inner_w - gap * (self._num - 1)) / self._num)

        for i in range(self._num):
            x = margin + i * (seg_w + gap)
            if i < self.completed_parts:
                color = QColor("#66BB6A") if self._done else QColor("#4CAF50")
                p.setPen(Qt.NoPen)
                p.setBrush(color)
                p.drawRoundedRect(int(x), margin, int(seg_w), inner_h, 3, 3)
            elif i == self.completed_parts and not self._done:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("#2a2a2a"))
                p.drawRoundedRect(int(x), margin, int(seg_w), inner_h, 3, 3)
                fill_w = int(self.current_frac * seg_w)
                if fill_w > 0:
                    glow = int(25 * (1 + math.sin(self._glow_phase)))
                    p.setBrush(QColor(255, 152, 0, 200 + glow))
                    p.drawRoundedRect(int(x), margin, fill_w, inner_h, 3, 3)
                a = int(100 + 80 * math.sin(self._glow_phase))
                p.setPen(QPen(QColor(255, 152, 0, a), 1.5))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(int(x), margin, int(seg_w), inner_h, 3, 3)
            else:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("#222"))
                p.drawRoundedRect(int(x), margin, int(seg_w), inner_h, 3, 3)

        p.end()


class ExportPanel(QWidget):
    """Rich export dashboard that replaces the right pane during export."""
    dismiss_requested = pyqtSignal()
    open_folder_requested = pyqtSignal(str)
    open_video_requested = pyqtSignal(str)
    delete_original_requested = pyqtSignal()

    def __init__(self, source_info, kept_segments, export_mode, output_path, parent=None):
        super().__init__(parent)
        self._output_path = output_path
        self._source_info = source_info
        self._start_time = _time.time()
        self._kept_segments = kept_segments
        self._total_duration = sum((s.end - s.start) / s.speed for s in kept_segments)
        self._current_part = 0
        self._total_parts = len(kept_segments)

        self.setStyleSheet("background: transparent;")

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setSpacing(14)
        layout.setContentsMargins(16, 14, 16, 14)

        # Header
        header_row = QHBoxLayout()
        self._header = QLabel("<b>Exporting...</b>")
        self._header.setStyleSheet("font-size: 18px; color: #FF9800;")
        header_row.addWidget(self._header)
        header_row.addStretch()
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setFixedHeight(30)
        self._cancel_btn.setFocusPolicy(Qt.NoFocus)
        self._cancel_btn.setCursor(Qt.PointingHandCursor)
        self._cancel_btn.setStyleSheet(
            "QPushButton { background: #b71c1c; color: white; font-size: 12px; font-weight: bold; "
            "border-radius: 4px; padding: 2px 14px; } "
            "QPushButton:hover { background: #d32f2f; }"
        )
        header_row.addWidget(self._cancel_btn)
        layout.addLayout(header_row)

        # Current task — prominent
        self._task_label = QLabel("Preparing...")
        self._task_label.setAlignment(Qt.AlignCenter)
        self._task_label.setStyleSheet(
            "font-size: 16px; font-weight: bold; color: #FF9800; "
            "background: #2a2000; border: 1px solid #4a3500; border-radius: 6px; padding: 10px 8px;"
        )
        self._task_label.setWordWrap(True)
        layout.addWidget(self._task_label)

        # Segment progress bar (equal-width blocks)
        self._seg_bar = SegmentProgressBar(len(kept_segments))
        layout.addWidget(self._seg_bar)

        # ETA — biggest element
        self._eta_label = QLabel("ETA: calculating...")
        self._eta_label.setAlignment(Qt.AlignCenter)
        self._eta_label.setStyleSheet("font-size: 28px; font-weight: bold; color: #FF9800; font-family: monospace;")
        layout.addWidget(self._eta_label)

        # Elapsed + percentage row
        time_row = QHBoxLayout()
        self._elapsed_label = QLabel("Elapsed: 0:00")
        self._elapsed_label.setStyleSheet("font-size: 14px; color: #888; font-family: monospace;")
        time_row.addWidget(self._elapsed_label)
        time_row.addStretch()
        self._pct_label = QLabel("0%")
        self._pct_label.setStyleSheet("font-size: 14px; color: #aaa; font-family: monospace;")
        time_row.addWidget(self._pct_label)
        layout.addLayout(time_row)

        layout.addWidget(self._make_sep())

        # Stats — single column layout, fixed label widths
        self._stat_values = {}

        def stat_row(label_text, key):
            row = QHBoxLayout()
            row.setSpacing(12)
            lbl = QLabel(label_text)
            lbl.setFixedWidth(110)
            lbl.setStyleSheet("font-size: 14px; color: #777; font-family: monospace;")
            val = QLabel("—")
            val.setStyleSheet("font-size: 14px; color: #ddd; font-family: monospace; font-weight: bold;")
            row.addWidget(lbl)
            row.addWidget(val, 1)
            self._stat_values[key] = val
            return row

        layout.addLayout(stat_row("Bitrate", "bitrate"))
        layout.addLayout(stat_row("Speed", "speed"))
        layout.addLayout(stat_row("Output Size", "out_size"))
        layout.addLayout(stat_row("Frames", "frames"))
        layout.addLayout(stat_row("FPS", "fps"))

        layout.addWidget(self._make_sep())

        # Source info
        info_header = QLabel("<b>Source</b>")
        info_header.setStyleSheet("font-size: 14px; color: #888;")
        layout.addWidget(info_header)

        def info_row(label_text, value):
            row = QHBoxLayout()
            row.setSpacing(12)
            lbl = QLabel(label_text)
            lbl.setFixedWidth(110)
            lbl.setStyleSheet("font-size: 13px; color: #666; font-family: monospace;")
            val = QLabel(str(value))
            val.setStyleSheet("font-size: 13px; color: #aaa; font-family: monospace;")
            val.setWordWrap(True)
            row.addWidget(lbl)
            row.addWidget(val, 1)
            return row

        layout.addLayout(info_row("File", source_info.get('name', '—')))
        layout.addLayout(info_row("Size", source_info.get('size', '—')))
        layout.addLayout(info_row("Resolution", source_info.get('resolution', '—')))
        layout.addLayout(info_row("Codec", source_info.get('codec', '—')))
        layout.addLayout(info_row("Mode", export_mode))
        layout.addLayout(info_row("Segments", f"{len(kept_segments)} kept"))
        layout.addLayout(info_row("Kept", fmt_time(self._total_duration)))
        layout.addLayout(info_row("Removed", fmt_time(source_info.get('duration', 0) - self._total_duration)))
        layout.addLayout(info_row("Output", os.path.basename(output_path)))

        layout.addWidget(self._make_sep())

        # Log area (collapsible)
        self._log_toggle = QPushButton("Show Log")
        self._log_toggle.setFixedHeight(26)
        self._log_toggle.setFocusPolicy(Qt.NoFocus)
        self._log_toggle.setCursor(Qt.PointingHandCursor)
        self._log_toggle.setStyleSheet(
            "QPushButton { background: transparent; color: #555; font-size: 12px; border: 1px solid #444; "
            "border-radius: 3px; padding: 2px 8px; } "
            "QPushButton:hover { color: #aaa; border-color: #666; }"
        )
        self._log_toggle.clicked.connect(self._toggle_log)
        layout.addWidget(self._log_toggle)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(120)
        self._log.setStyleSheet(
            "QPlainTextEdit { background: #1a1a1a; color: #555; font-size: 11px; "
            "font-family: monospace; border: 1px solid #333; border-radius: 4px; }"
        )
        self._log.setVisible(False)
        layout.addWidget(self._log)

        # Action buttons (hidden until complete)
        self._actions_widget = QWidget()
        actions_layout = QVBoxLayout(self._actions_widget)
        actions_layout.setSpacing(8)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        btn_row1 = QHBoxLayout()
        self._open_folder_btn = QPushButton("Open Folder")
        self._open_video_btn = QPushButton("Open Video")
        for btn in (self._open_folder_btn, self._open_video_btn):
            btn.setFixedHeight(34)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(
                "QPushButton { background: #1565C0; color: white; font-size: 13px; font-weight: bold; "
                "border-radius: 4px; padding: 4px 16px; } "
                "QPushButton:hover { background: #1976D2; }"
            )
        btn_row1.addWidget(self._open_folder_btn)
        btn_row1.addWidget(self._open_video_btn)
        actions_layout.addLayout(btn_row1)

        btn_row2 = QHBoxLayout()
        self._delete_btn = QPushButton("Delete Original")
        self._delete_btn.setFixedHeight(34)
        self._delete_btn.setFocusPolicy(Qt.NoFocus)
        self._delete_btn.setCursor(Qt.PointingHandCursor)
        self._delete_btn.setStyleSheet(
            "QPushButton { background: #4a1a1a; color: #ef5350; font-size: 13px; font-weight: bold; "
            "border: 1px solid #c62828; border-radius: 4px; padding: 4px 16px; } "
            "QPushButton:hover { background: #5a2a2a; }"
        )
        self._done_btn = QPushButton("Done")
        self._done_btn.setFixedHeight(34)
        self._done_btn.setFocusPolicy(Qt.NoFocus)
        self._done_btn.setCursor(Qt.PointingHandCursor)
        self._done_btn.setStyleSheet(
            "QPushButton { background: #2E7D32; color: white; font-size: 13px; font-weight: bold; "
            "border-radius: 4px; padding: 4px 16px; } "
            "QPushButton:hover { background: #388E3C; }"
        )
        btn_row2.addWidget(self._delete_btn)
        btn_row2.addWidget(self._done_btn)
        actions_layout.addLayout(btn_row2)

        self._actions_widget.setVisible(False)
        layout.addWidget(self._actions_widget)

        layout.addStretch()

        scroll.setWidget(inner)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        # Connect buttons
        self._open_folder_btn.clicked.connect(lambda: self.open_folder_requested.emit(self._output_path))
        self._open_video_btn.clicked.connect(lambda: self.open_video_requested.emit(self._output_path))
        self._delete_btn.clicked.connect(lambda: self.delete_original_requested.emit())
        self._done_btn.clicked.connect(lambda: self.dismiss_requested.emit())

    def _make_sep(self):
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #333;")
        sep.setFixedHeight(1)
        return sep

    def _toggle_log(self):
        visible = not self._log.isVisible()
        self._log.setVisible(visible)
        self._log_toggle.setText("Hide Log" if visible else "Show Log")

    def get_cancel_button(self):
        return self._cancel_btn

    def _fmt_human_time(self, secs):
        secs = max(0, secs)
        if secs < 60:
            return f"{int(secs)}s"
        elif secs < 3600:
            return f"{int(secs // 60)}m {int(secs % 60)}s"
        return fmt_time(secs)

    def update_progress(self, msg):
        """Called with text progress messages from ExportWorker.progress signal."""
        self._log.appendPlainText(msg)
        if msg.startswith("Part "):
            try:
                current = int(msg.split(" ")[1].split("/")[0])
                total = int(msg.split("/")[1].split(":")[0])
                self._current_part = current
                self._seg_bar.set_progress(current - 1, 1.0)
                self._task_label.setText(f"Segment {current} of {total}")
            except (ValueError, IndexError):
                pass
        elif "Joining" in msg or "Mixing" in msg or "Burning" in msg:
            self._seg_bar.set_progress(self._total_parts, 0.0)
            self._task_label.setText(msg.split("...")[0] if "..." in msg else msg)
            self._pct_label.setText("99%")
        elif "Exporting:" in msg:
            self._task_label.setText("Segment 1 of 1")

    def update_detail(self, data):
        """Called with structured ffmpeg progress data."""
        elapsed = data.get('elapsed', 0)
        total_dur = data.get('total_duration', self._total_duration)
        current_time = data.get('time', 0)

        completed_dur = sum(
            (s.end - s.start) / s.speed
            for s in self._kept_segments[:self._current_part]
        )
        overall_progress = (completed_dur + current_time) / total_dur if total_dur > 0 else 0
        overall_progress = min(0.99, max(0.0, overall_progress))

        pct = int(overall_progress * 100)
        self._pct_label.setText(f"{pct}%")

        # Update segment bar
        if self._current_part < self._total_parts:
            seg = self._kept_segments[self._current_part]
            seg_dur = (seg.end - seg.start) / seg.speed
            seg_frac = current_time / seg_dur if seg_dur > 0 else 0
            self._seg_bar.set_progress(self._current_part, min(1.0, seg_frac))

        # ETA — the star
        if overall_progress > 0.01 and elapsed > 2:
            eta_secs = max(0, (elapsed / overall_progress) - elapsed)
            self._eta_label.setText(f"ETA: {self._fmt_human_time(eta_secs)}")
        else:
            self._eta_label.setText("ETA: calculating...")

        self._elapsed_label.setText(f"Elapsed: {self._fmt_human_time(elapsed)}")
        self._stat_values['bitrate'].setText(data.get('bitrate', '—'))
        self._stat_values['speed'].setText(data.get('speed', '—'))
        self._stat_values['frames'].setText(str(data.get('frame', '—')))
        self._stat_values['fps'].setText(str(data.get('fps', '—')))
        self._stat_values['out_size'].setText(data.get('size', '—'))

    def show_complete(self, path, size):
        """Transform panel to show completion state."""
        self._output_path = path
        self._header.setText("<b>Export Complete</b>")
        self._header.setStyleSheet("font-size: 18px; color: #4CAF50;")
        self._seg_bar.set_complete()

        elapsed = _time.time() - self._start_time
        self._task_label.setText(fmt_size(size))
        self._task_label.setStyleSheet(
            "font-size: 16px; font-weight: bold; color: #4CAF50; "
            "background: #1a2a1a; border: 1px solid #2E7D32; border-radius: 6px; padding: 10px 8px;"
        )
        self._eta_label.setText("Done")
        self._eta_label.setStyleSheet("font-size: 28px; font-weight: bold; color: #4CAF50; font-family: monospace;")
        self._elapsed_label.setText(f"Took: {self._fmt_human_time(elapsed)}")
        self._pct_label.setText("100%")

        src_size = self._source_info.get('raw_size', 0)
        if src_size > 0:
            ratio = size / src_size
            self._stat_values['out_size'].setText(f"{fmt_size(size)} ({ratio:.0%} of source)")

        self._cancel_btn.setVisible(False)
        self._actions_widget.setVisible(True)

    def show_error(self, err):
        """Show error state."""
        self._header.setText("<b>Export Failed</b>")
        self._header.setStyleSheet("font-size: 18px; color: #ef5350;")
        self._eta_label.setVisible(False)
        self._pct_label.setVisible(False)
        self._task_label.setText(err[:200])
        self._task_label.setStyleSheet(
            "font-size: 14px; color: #ef5350; "
            "background: #3a1a1a; border: 1px solid #c62828; border-radius: 6px; padding: 10px 8px;"
        )
        self._cancel_btn.setVisible(False)
        self._done_btn.setText("Close")
        self._delete_btn.setVisible(False)
        self._open_folder_btn.setVisible(False)
        self._open_video_btn.setVisible(False)
        self._actions_widget.setVisible(True)


# ── Segment List Item ────────────────────────────────────────────────────────

class SegmentWidget(QFrame):
    toggled = pyqtSignal(int)
    selected = pyqtSignal(int)

    def __init__(self, index, segment, parent=None):
        super().__init__(parent)
        self.index = index
        self.segment = segment
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(36)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(0)

        dur = segment.end - segment.start
        status = "KEEP" if segment.keep else "CUT"
        self.status_label = QLabel(status)
        self.status_label.setFixedWidth(40)
        self.status_label.setStyleSheet("font-family: monospace; font-size: 11px; font-weight: bold;")
        layout.addWidget(self.status_label)

        speed_tag = f"  {segment.speed}x" if segment.speed != 1.0 else ""
        time_text = f"{fmt_time(segment.start)}    [{fmt_time(dur)}]{speed_tag}"
        self.time_label = QLabel(time_text)
        self.time_label.setStyleSheet("font-family: monospace; font-size: 13px; color: #ddd;")
        layout.addWidget(self.time_label)

        layout.addStretch()
        self._update_style()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.toggled.emit(self.index)
        elif event.button() == Qt.RightButton:
            self.selected.emit(self.index)

    _SPEED_BG = {
        1.25: ("#1a3a1a", "#1e4a1e", "#4CAF50", "#388E3C"),   # green
        1.5:  ("#3a3a1a", "#4a4a1e", "#FFEB3B", "#F9A825"),   # yellow
        1.75: ("#3a2a1a", "#4a3a1e", "#FF9800", "#E65100"),   # orange
        2.0:  ("#3a1a1a", "#4a1e1e", "#ef5350", "#b71c1c"),   # dark red
    }

    def _update_style(self, selected=False):
        border_w = 2 if selected else 1
        if self.segment.keep:
            speed_style = self._SPEED_BG.get(self.segment.speed)
            if speed_style:
                bg = speed_style[1] if selected else speed_style[0]
                border = speed_style[2] if selected else speed_style[3]
                status_color = speed_style[2]
            else:
                bg = "#1a2a4a" if not selected else "#1e3a6a"
                border = "#42A5F5" if selected else "#1565C0"
                status_color = "#42A5F5"
        else:
            bg = "#3a1a1a" if not selected else "#5a2a2a"
            border = "#ef9a9a" if selected else "#c62828"
            status_color = "#ef5350"
        self.setStyleSheet(f"SegmentWidget {{ background: {bg}; border: {border_w}px solid {border}; border-radius: 4px; }}")
        self.status_label.setText("KEEP" if self.segment.keep else "CUT")
        self.status_label.setStyleSheet(f"font-family: monospace; font-size: 11px; font-weight: bold; color: {status_color}; border: none;")

    def mousePressEvent(self, event):
        self.selected.emit(self.index)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.toggled.emit(self.index)
        super().mouseDoubleClickEvent(event)


# ── Styled Button Helper ─────────────────────────────────────────────────────

MSG_BOX_STYLE = """
QMessageBox { background: #2b2b2b; }
QMessageBox QLabel { color: #ddd; font-size: 14px; }
QMessageBox QPushButton {
    background: #444; color: #ddd; border: 1px solid #666;
    border-radius: 4px; padding: 6px 20px; font-size: 13px; min-width: 80px;
}
QMessageBox QPushButton:hover { background: #555; }
"""

def styled_msg(parent, icon, title, text, buttons=QMessageBox.Ok):
    box = QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setText(text)
    box.setStandardButtons(buttons)
    box.setStyleSheet(MSG_BOX_STYLE)
    # Make the first positive button the default (Enter key)
    if buttons & QMessageBox.Ok:
        box.setDefaultButton(QMessageBox.Ok)
    elif buttons & QMessageBox.Yes:
        box.setDefaultButton(QMessageBox.Yes)
    return box.exec_()

def action_btn(text, color, shortcut_hint=None):
    label = text
    if shortcut_hint:
        label = f"{text}  [{shortcut_hint}]"
    btn = QPushButton(label)
    btn.setFixedHeight(42)
    btn.setFocusPolicy(Qt.NoFocus)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setStyleSheet(f"""
        QPushButton {{
            background: {color}; color: white; font-weight: bold;
            border-radius: 6px; padding: 0 18px; font-size: 13px; border: none;
        }}
        QPushButton:hover {{ background: {color}; border: 2px solid white; }}
        QPushButton:pressed {{ background: {color}; opacity: 0.8; }}
    """)
    return btn


# ── Settings Dialog ──────────────────────────────────────────────────────────

class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(550)
        self.setStyleSheet("""
            QDialog { background: #2b2b2b; color: #ddd; }
            QLabel { color: #ccc; font-size: 13px; }
            QLineEdit { background: #1e1e1e; color: #eee; border: 1px solid #555;
                        border-radius: 4px; padding: 6px 8px; font-size: 13px; }
            QGroupBox { font-size: 14px; font-weight: bold; color: #aaa;
                        border: 1px solid #444; border-radius: 6px;
                        margin-top: 12px; padding-top: 18px; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }
            QPushButton { font-size: 13px; padding: 4px 10px; border-radius: 4px; }
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Paths group
        paths_group = QGroupBox("Directories")
        form = QFormLayout()
        form.setSpacing(10)

        self._fields = {}
        field_defs = [
            ("export_dir", "Export Directory", str(EXPORT_DIR)),
            ("download_dir", "Download Directory", str(DOWNLOAD_DIR)),
            ("save_dir", "Save Directory", str(SAVE_DIR)),
            ("cache_dir", "Cache Directory", str(CACHE_DIR)),
            ("waveform_dir", "Waveform Cache", str(WAVEFORM_CACHE_DIR)),
            ("recents_file", "Recents File", str(RECENTS_FILE)),
            ("file_manager", "File Manager", FILE_MANAGER),
        ]

        for key, label, value in field_defs:
            row = QHBoxLayout()
            edit = QLineEdit(value)
            edit.setMinimumWidth(320)
            row.addWidget(edit, 1)
            browse = QPushButton("Browse")
            browse.setFixedHeight(30)
            browse.setStyleSheet(
                "QPushButton { background: #444; color: #ddd; border: 1px solid #666; } "
                "QPushButton:hover { background: #555; }"
            )
            is_file = key == "recents_file"
            browse.clicked.connect(lambda _, e=edit, f=is_file: self._browse(e, f))
            row.addWidget(browse)
            form.addRow(QLabel(label), row)
            self._fields[key] = edit

        paths_group.setLayout(form)
        layout.addWidget(paths_group)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        save_btn = QPushButton("Save")
        save_btn.setFixedSize(100, 36)
        save_btn.setStyleSheet(
            "QPushButton { background: #2E7D32; color: white; font-weight: bold; } "
            "QPushButton:hover { background: #388E3C; }"
        )
        save_btn.clicked.connect(self.accept)
        btn_row.addWidget(save_btn)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFixedSize(100, 36)
        cancel_btn.setStyleSheet(
            "QPushButton { background: #555; color: #ddd; } "
            "QPushButton:hover { background: #666; }"
        )
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def _browse(self, line_edit, is_file=False):
        if is_file:
            path, _ = QFileDialog.getSaveFileName(self, "Select File", line_edit.text())
        else:
            path = QFileDialog.getExistingDirectory(self, "Select Directory", line_edit.text())
        if path:
            line_edit.setText(path)

    def get_values(self):
        return {k: v.text().strip() for k, v in self._fields.items()}



# ── Compression Feature ──────────────────────────────────────────────────────


@dataclass
class CompressPreset:
    name: str
    label: str
    description: str
    crf: int
    preset: str
    audio_bitrate: str
    scale: int | None


DEFAULT_PRESETS = [
    CompressPreset(
        "lossless_lite", "Near-Lossless",
        "Minimal compression, archival quality",
        17, "fast", "192k", None),
    CompressPreset(
        "high", "High Quality",
        "Very high quality, noticeable size reduction",
        20, "medium", "160k", None),
    CompressPreset(
        "balanced", "Balanced",
        "Good quality with significant compression",
        24, "medium", "128k", None),
    CompressPreset(
        "compact", "Compact",
        "Smaller file, decent quality at full resolution",
        28, "medium", "96k", None),
    CompressPreset(
        "small", "Small File",
        "Maximum compression at 720p",
        32, "medium", "64k", 720),
]


class SnippetExtractWorker(QThread):
    finished = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, filepath, tmp_dir):
        super().__init__()
        self.filepath = filepath
        self.tmp_dir = tmp_dir

    def run(self):
        try:
            duration = get_duration(self.filepath)
            if duration < 1:
                self.error.emit("Could not determine video duration")
                return
            snippet_len = min(5.0, duration)
            max_start = max(0, duration - snippet_len)
            if max_start > 0:
                lo = duration * 0.1
                hi = min(duration * 0.8, max_start)
                start = random.uniform(lo, hi) if hi > lo else 0
            else:
                start = 0
            out = os.path.join(self.tmp_dir, "snippet.mp4")
            cmd = [
                'ffmpeg', '-y', '-ss', str(start),
                '-i', str(self.filepath),
                '-t', str(snippet_len),
                '-c', 'copy', '-avoid_negative_ts', 'make_zero',
                '-movflags', '+faststart', out,
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                self.error.emit(f"Snippet extraction failed:\n{r.stderr[-500:]}")
                return
            if not os.path.exists(out) or os.path.getsize(out) < 100:
                self.error.emit("Snippet extraction produced no output")
                return
            self.finished.emit(out)
        except Exception as e:
            self.error.emit(str(e))


class CompressWorker(QThread):
    progress = pyqtSignal(int, str)
    finished = pyqtSignal(str, dict)
    error = pyqtSignal(str)

    def __init__(self, filepath, output_path, cpreset, duration=None):
        super().__init__()
        self.filepath = filepath
        self.output_path = output_path
        self.cpreset = cpreset
        self.duration = duration

    def run(self):
        try:
            if self.duration is None:
                self.duration = get_duration(self.filepath)
            input_size = os.path.getsize(self.filepath)

            cmd = ['ffmpeg', '-y', '-i', str(self.filepath)]
            p = self.cpreset
            if p.scale:
                cmd += ['-vf', f'scale=-2:{p.scale}']
            cmd += [
                '-c:v', 'libx264', '-preset', p.preset, '-crf', str(p.crf),
                '-c:a', 'aac', '-b:a', p.audio_bitrate,
                '-movflags', '+faststart',
                str(self.output_path),
            ]

            proc = subprocess.Popen(
                cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
                universal_newlines=True,
            )
            time_re = re.compile(r'time=(\d{2}):(\d{2}):(\d{2})\.(\d{2})')
            for line in proc.stderr:
                m = time_re.search(line)
                if m and self.duration > 0:
                    t = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                         + int(m.group(3)) + int(m.group(4)) / 100)
                    pct = min(int(t / self.duration * 100), 99)
                    self.progress.emit(pct, f"Encoding... {pct}%")
            proc.wait()
            if proc.returncode != 0:
                self.error.emit("Compression failed")
                return

            output_size = os.path.getsize(self.output_path)
            ratio = output_size / input_size if input_size > 0 else 1.0
            savings = (1 - ratio) * 100
            stats = {
                'input_size': input_size,
                'output_size': output_size,
                'output_path': self.output_path,
                'ratio': ratio,
                'savings': savings,
            }
            self.progress.emit(100, "Done")
            self.finished.emit(str(self.output_path), stats)
        except Exception as e:
            self.error.emit(str(e))


class PresetCard(QFrame):
    selected = pyqtSignal(object)

    def __init__(self, cpreset, parent=None):
        super().__init__(parent)
        self.cpreset = cpreset
        self._is_selected = False
        self.setFixedWidth(210)
        self.setFrameShape(QFrame.StyledPanel)
        self._update_border()
        self.setCursor(Qt.PointingHandCursor)

        lay = QVBoxLayout(self)
        lay.setSpacing(6)
        lay.setContentsMargins(12, 10, 12, 10)

        self.title_label = QLabel(cpreset.label)
        self.title_label.setStyleSheet("font-size: 15px; font-weight: bold; color: #eee;")
        lay.addWidget(self.title_label)

        self.desc_label = QLabel(cpreset.description)
        self.desc_label.setWordWrap(True)
        self.desc_label.setStyleSheet("font-size: 11px; color: #999;")
        lay.addWidget(self.desc_label)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #555;")
        lay.addWidget(sep)

        info_text = f"CRF {cpreset.crf} / {cpreset.preset}"
        if cpreset.scale:
            info_text += f" / {cpreset.scale}p"
        self.info_label = QLabel(info_text)
        self.info_label.setStyleSheet("font-family: monospace; font-size: 11px; color: #aaa;")
        lay.addWidget(self.info_label)

        self.size_label = QLabel("--")
        self.size_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #FF9800;")
        lay.addWidget(self.size_label)

        self.ratio_label = QLabel("")
        self.ratio_label.setStyleSheet("font-size: 12px; color: #aaa;")
        lay.addWidget(self.ratio_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(16)
        self.progress_bar.setTextVisible(False)
        lay.addWidget(self.progress_bar)

        self._test_output_path = None

        btn_row = QHBoxLayout()
        self.open_btn = QPushButton("Open")
        self.open_btn.setStyleSheet(
            "QPushButton { background: #37474F; color: white; font-weight: bold; "
            "border-radius: 4px; padding: 6px; font-size: 12px; }"
            "QPushButton:hover { background: #455A64; }")
        self.open_btn.setVisible(False)
        self.open_btn.clicked.connect(self._open_test_file)
        btn_row.addWidget(self.open_btn)

        self.select_btn = QPushButton("Select")
        self.select_btn.setStyleSheet(
            "QPushButton { background: #E65100; color: white; font-weight: bold; "
            "border-radius: 4px; padding: 6px; font-size: 12px; }"
            "QPushButton:hover { background: #FF6D00; }")
        self.select_btn.setVisible(False)
        self.select_btn.clicked.connect(lambda: self.selected.emit(self.cpreset))
        btn_row.addWidget(self.select_btn)
        lay.addLayout(btn_row)
        lay.addStretch()

    def set_progress(self, pct):
        self.progress_bar.setValue(pct)

    def set_result(self, stats, source_size=None):
        self.progress_bar.setVisible(False)
        self._test_output_path = stats.get('output_path')
        ratio_text = (
            f"{stats['savings']:.1f}% smaller  "
            f"(snippet: {fmt_size(stats['input_size'])} \u2192 {fmt_size(stats['output_size'])})")
        if source_size and stats.get('ratio'):
            est_size = int(source_size * stats['ratio'])
            self.size_label.setText(f"~{fmt_size(est_size)}")
            ratio_text += f"\nEstimated full: {fmt_size(est_size)}"
        else:
            self.size_label.setText(fmt_size(stats['output_size']))
        self.ratio_label.setText(ratio_text)
        self.select_btn.setVisible(True)
        self.open_btn.setVisible(True)

    def _open_test_file(self):
        if self._test_output_path and os.path.exists(self._test_output_path):
            subprocess.Popen(["xdg-open", self._test_output_path])

    def set_selected(self, sel):
        self._is_selected = sel
        self._update_border()

    def _update_border(self):
        if self._is_selected:
            self.setStyleSheet(
                "PresetCard, QFrame { background: #3a3a3a; "
                "border: 2px solid #FF9800; border-radius: 8px; }")
        else:
            self.setStyleSheet(
                "PresetCard, QFrame { background: #333; "
                "border: 1px solid #555; border-radius: 8px; }")

    def mousePressEvent(self, ev):
        if self.select_btn.isVisible():
            self.selected.emit(self.cpreset)


class CompressPanel(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self._main = main_window
        self._source_path = None
        self._snippet_path = None
        self._tmp_dir = None
        self._snippet_worker = None
        self._preview_workers = []
        self._full_worker = None
        self._cards = []
        self._selected_preset = None
        self._preview_mpv = None
        self._compressed_path = None
        self._build_compress_ui()

    def _build_compress_ui(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(10)
        outer.setContentsMargins(16, 12, 16, 12)

        # File selection row
        file_row = QHBoxLayout()
        file_row.setSpacing(10)
        lbl = QLabel("Source:")
        lbl.setStyleSheet("font-size: 14px; font-weight: bold; color: #ccc;")
        file_row.addWidget(lbl)

        self.source_label = QLabel("No file selected")
        self.source_label.setStyleSheet("font-size: 14px; color: #999;")
        file_row.addWidget(self.source_label, 1)

        self.browse_btn = QPushButton("Select File")
        self.browse_btn.setFixedSize(120, 34)
        self.browse_btn.setStyleSheet(
            "QPushButton { background: #1565C0; color: white; font-size: 13px; "
            "font-weight: bold; border-radius: 6px; }"
            "QPushButton:hover { background: #1976D2; }")
        self.browse_btn.clicked.connect(self._browse_file)
        file_row.addWidget(self.browse_btn)

        self.use_current_btn = QPushButton("Use Current Video")
        self.use_current_btn.setFixedSize(150, 34)
        self.use_current_btn.setStyleSheet(
            "QPushButton { background: #E65100; color: white; font-size: 13px; "
            "font-weight: bold; border-radius: 6px; }"
            "QPushButton:hover { background: #FF6D00; }")
        self.use_current_btn.clicked.connect(self._use_current)
        file_row.addWidget(self.use_current_btn)
        outer.addLayout(file_row)

        # Stacked pages
        self.cstack = QStackedWidget()

        # Page 0: Empty state
        empty_page = QWidget()
        el = QVBoxLayout(empty_page)
        el.addStretch()
        empty_lbl = QLabel("Select a video file to begin compression testing")
        empty_lbl.setAlignment(Qt.AlignCenter)
        empty_lbl.setStyleSheet("font-size: 18px; color: #666;")
        el.addWidget(empty_lbl)
        el.addStretch()
        self.cstack.addWidget(empty_page)

        # Page 1: Preview page (cards + settings)
        preview_page = QWidget()
        ph = QHBoxLayout(preview_page)
        ph.setSpacing(12)
        ph.setContentsMargins(0, 0, 0, 0)

        # Left: scrollable card area
        self._cards_scroll = QScrollArea()
        self._cards_scroll.setWidgetResizable(True)
        self._cards_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._cards_scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }")
        self._cards_container = QWidget()
        self._cards_layout = QGridLayout(self._cards_container)
        self._cards_layout.setSpacing(12)
        self._cards_layout.setContentsMargins(0, 0, 0, 0)
        self._cards_scroll.setWidget(self._cards_container)
        ph.addWidget(self._cards_scroll, 1)

        # Right: settings panel
        settings_panel = QWidget()
        settings_panel.setFixedWidth(300)
        sl = QVBoxLayout(settings_panel)
        sl.setSpacing(10)
        sl.setContentsMargins(12, 0, 0, 0)

        self.preview_status = QLabel("Select a video to begin.")
        self.preview_status.setStyleSheet("font-size: 13px; color: #aaa;")
        sl.addWidget(self.preview_status)

        self.test_presets_btn = QPushButton("Test Presets")
        self.test_presets_btn.setFixedHeight(36)
        self.test_presets_btn.setStyleSheet(
            "QPushButton { background: #1565C0; color: white; font-size: 13px; font-weight: bold; "
            "border-radius: 4px; padding: 4px 12px; } "
            "QPushButton:hover { background: #1976D2; }")
        self.test_presets_btn.clicked.connect(self._start_preview)
        self.test_presets_btn.setVisible(False)
        sl.addWidget(self.test_presets_btn)

        custom_group = QGroupBox("Custom Compression")
        custom_group.setStyleSheet(
            "QGroupBox { font-size: 13px; font-weight: bold; color: #ccc; "
            "border: 1px solid #555; border-radius: 6px; margin-top: 8px; padding-top: 16px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 6px; }")
        cf = QFormLayout(custom_group)
        cf.setSpacing(8)

        crf_row = QHBoxLayout()
        self.crf_slider = QSlider(Qt.Horizontal)
        self.crf_slider.setRange(15, 40)
        self.crf_slider.setValue(24)
        self.crf_label = QLabel("24")
        self.crf_label.setFixedWidth(28)
        self.crf_label.setStyleSheet(
            "font-family: monospace; font-size: 13px; color: #FF9800;")
        self.crf_slider.valueChanged.connect(lambda v: self.crf_label.setText(str(v)))
        crf_row.addWidget(self.crf_slider, 1)
        crf_row.addWidget(self.crf_label)
        cf.addRow("CRF:", crf_row)

        combo_style = (
            "QComboBox { background: #3a3a3a; color: #eee; border: 1px solid #555; "
            "border-radius: 4px; padding: 4px 8px; }")

        self.x264_combo = QComboBox()
        self.x264_combo.addItems([
            "ultrafast", "superfast", "veryfast", "faster",
            "fast", "medium", "slow", "slower", "veryslow"])
        self.x264_combo.setCurrentText("medium")
        self.x264_combo.setStyleSheet(combo_style)
        cf.addRow("Preset:", self.x264_combo)

        self.audio_combo = QComboBox()
        self.audio_combo.addItems(["64k", "96k", "128k", "160k", "192k", "256k"])
        self.audio_combo.setCurrentText("128k")
        self.audio_combo.setStyleSheet(combo_style)
        cf.addRow("Audio:", self.audio_combo)

        self.scale_combo = QComboBox()
        self.scale_combo.addItems(["Original", "1080p", "720p", "480p"])
        self.scale_combo.setStyleSheet(combo_style)
        cf.addRow("Scale:", self.scale_combo)

        sl.addWidget(custom_group)

        self.add_custom_btn = QPushButton("Test Custom Preset")
        self.add_custom_btn.setStyleSheet(
            "QPushButton { background: #6A1B9A; color: white; font-weight: bold; "
            "border-radius: 6px; padding: 8px; font-size: 13px; }"
            "QPushButton:hover { background: #7B1FA2; }")
        self.add_custom_btn.clicked.connect(self._add_custom_preset)
        sl.addWidget(self.add_custom_btn)

        sl.addSpacing(16)
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #555;")
        sl.addWidget(sep)
        sl.addSpacing(8)

        self.compress_full_btn = QPushButton("Compress Full Video")
        self.compress_full_btn.setFixedHeight(44)
        self.compress_full_btn.setEnabled(False)
        self.compress_full_btn.setStyleSheet(
            "QPushButton { background: #2E7D32; color: white; font-weight: bold; "
            "border-radius: 6px; font-size: 15px; }"
            "QPushButton:hover { background: #388E3C; }"
            "QPushButton:disabled { background: #555; color: #888; }")
        self.compress_full_btn.clicked.connect(self._compress_full)
        sl.addWidget(self.compress_full_btn)

        self.full_progress = QProgressBar()
        self.full_progress.setFixedHeight(20)
        self.full_progress.setVisible(False)
        sl.addWidget(self.full_progress)

        self.full_status = QLabel("")
        self.full_status.setStyleSheet("font-size: 12px; color: #aaa;")
        sl.addWidget(self.full_status)

        sl.addStretch()
        ph.addWidget(settings_panel)
        self.cstack.addWidget(preview_page)

        # Page 2: Result page
        result_page = QWidget()
        rl = QVBoxLayout(result_page)
        rl.setSpacing(10)

        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet("font-size: 15px; color: #ccc;")
        self.stats_label.setAlignment(Qt.AlignCenter)
        rl.addWidget(self.stats_label)

        self.preview_widget = QWidget()
        self.preview_widget.setMinimumHeight(300)
        self.preview_widget.setStyleSheet("background: #0a0a0a; border-radius: 6px;")
        self.preview_widget.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.preview_widget.setAttribute(Qt.WA_NativeWindow)
        rl.addWidget(self.preview_widget, 1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        btn_blue = (
            "QPushButton { background: #1565C0; color: white; font-weight: bold; "
            "border-radius: 6px; padding: 8px 16px; font-size: 13px; }"
            "QPushButton:hover { background: #1976D2; }")
        btn_green = (
            "QPushButton { background: #2E7D32; color: white; font-weight: bold; "
            "border-radius: 6px; padding: 8px 16px; font-size: 13px; }"
            "QPushButton:hover { background: #388E3C; }")
        btn_orange = (
            "QPushButton { background: #E65100; color: white; font-weight: bold; "
            "border-radius: 6px; padding: 8px 16px; font-size: 13px; }"
            "QPushButton:hover { background: #FF6D00; }")
        btn_red = (
            "QPushButton { background: #b71c1c; color: white; font-weight: bold; "
            "border-radius: 6px; padding: 8px 16px; font-size: 13px; }"
            "QPushButton:hover { background: #d32f2f; }")
        btn_grey = (
            "QPushButton { background: #3a3a3a; color: #ddd; border: 1px solid #555; "
            "border-radius: 6px; padding: 8px 16px; font-size: 13px; }"
            "QPushButton:hover { background: #4a4a4a; }")

        self.play_btn = QPushButton("Play / Pause")
        self.play_btn.setStyleSheet(btn_blue)
        self.play_btn.clicked.connect(self._toggle_preview_play)
        btn_row.addWidget(self.play_btn)

        self.save_as_btn = QPushButton("Save As...")
        self.save_as_btn.setStyleSheet(btn_green)
        self.save_as_btn.clicked.connect(self._save_as)
        btn_row.addWidget(self.save_as_btn)

        self.replace_btn = QPushButton("Replace Original")
        self.replace_btn.setStyleSheet(btn_orange)
        self.replace_btn.clicked.connect(self._replace_original)
        btn_row.addWidget(self.replace_btn)

        self.delete_btn = QPushButton("Delete Original")
        self.delete_btn.setStyleSheet(btn_red)
        self.delete_btn.clicked.connect(self._delete_original)
        btn_row.addWidget(self.delete_btn)

        self.cleanup_btn = QPushButton("Clean Up Temp Files")
        self.cleanup_btn.setStyleSheet(btn_grey)
        self.cleanup_btn.clicked.connect(self._cleanup_temps)
        btn_row.addWidget(self.cleanup_btn)

        self.restart_btn = QPushButton("Start Over")
        self.restart_btn.setStyleSheet(btn_grey)
        self.restart_btn.clicked.connect(self._start_over)
        btn_row.addWidget(self.restart_btn)

        rl.addLayout(btn_row)
        self.cstack.addWidget(result_page)

        outer.addWidget(self.cstack, 1)

    # ── File selection ──

    def _browse_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Video", "",
            "Video Files (*.mp4 *.mkv *.avi *.mov *.webm *.flv *.ts *.m4v);;All Files (*)")
        if path:
            self._load_source(path)

    def _use_current(self):
        if self._main.filepath:
            self._load_source(self._main.filepath)

    def _load_source(self, path):
        self._source_path = path
        size = os.path.getsize(path)
        info = probe_video(path)
        res = ""
        if info:
            for s in info.get('streams', []):
                if s.get('codec_type') == 'video':
                    res = f"  |  {s.get('width', '?')}x{s.get('height', '?')}"
                    break
        self.source_label.setText(f"{os.path.basename(path)}  |  {fmt_size(size)}{res}")
        self.source_label.setStyleSheet("font-size: 14px; color: #eee;")
        self.cstack.setCurrentIndex(1)
        self.preview_status.setText("Ready. Click 'Test Presets' to start.")
        self.test_presets_btn.setVisible(True)

    def _start_preview(self):
        self._stop_workers()
        self._clear_cards()
        self._selected_preset = None
        self.compress_full_btn.setEnabled(False)
        self.full_progress.setVisible(False)
        self.full_status.setText("")
        self.test_presets_btn.setVisible(False)

        self._tmp_dir = tempfile.mkdtemp(prefix="qv_compress_")
        self.cstack.setCurrentIndex(1)
        self.preview_status.setText("Extracting 5-second snippet...")

        self._snippet_worker = SnippetExtractWorker(self._source_path, self._tmp_dir)
        self._snippet_worker.finished.connect(self._on_snippet_ready)
        self._snippet_worker.error.connect(self._on_snippet_error)
        self._snippet_worker.start()

    def _on_snippet_ready(self, snippet_path):
        self._snippet_path = snippet_path
        self.preview_status.setText("Testing compression presets...")
        self._run_preset_previews(DEFAULT_PRESETS)

    def _on_snippet_error(self, msg):
        self.preview_status.setText(f"Error: {msg}")

    def _run_preset_previews(self, presets):
        snippet_dur = get_duration(self._snippet_path)
        for i, cpreset in enumerate(presets):
            out_path = os.path.join(self._tmp_dir, f"preview_{cpreset.name}.mp4")
            card = PresetCard(cpreset)
            card.selected.connect(self._on_preset_selected)
            self._cards.append(card)
            total = len(self._cards)
            row, col = divmod(total - 1, 3)
            self._cards_layout.addWidget(card, row, col)

            worker = CompressWorker(self._snippet_path, out_path, cpreset, snippet_dur)
            worker.progress.connect(lambda pct, msg, c=card: c.set_progress(pct))
            worker.finished.connect(
                lambda path, stats, c=card: self._on_preview_done(c, stats))
            worker.error.connect(
                lambda msg, c=card: self._on_preview_error(c, msg))
            self._preview_workers.append(worker)
            worker.start()

    def _on_preview_done(self, card, stats):
        source_size = os.path.getsize(self._source_path) if self._source_path else None
        card.set_result(stats, source_size=source_size)
        if all(not w.isRunning() for w in self._preview_workers):
            self.preview_status.setText("All presets tested. Select one or customize.")

    def _on_preview_error(self, card, msg):
        card.size_label.setText("Error")
        card.ratio_label.setText(msg[:60])
        card.progress_bar.setVisible(False)

    def _on_preset_selected(self, cpreset):
        self._selected_preset = cpreset
        for card in self._cards:
            card.set_selected(card.cpreset.name == cpreset.name)
        self.compress_full_btn.setEnabled(True)
        self.compress_full_btn.setText(f"Compress Full Video ({cpreset.label})")

    def _add_custom_preset(self):
        if not self._snippet_path:
            return
        scale_map = {"Original": None, "1080p": 1080, "720p": 720, "480p": 480}
        scale = scale_map.get(self.scale_combo.currentText())
        crf = self.crf_slider.value()
        desc = f"{self.x264_combo.currentText()} / {self.audio_combo.currentText()}"
        if scale:
            desc += f" / {self.scale_combo.currentText()}"
        cpreset = CompressPreset(
            name=f"custom_{len(self._cards)}",
            label=f"Custom (CRF {crf})",
            description=desc,
            crf=crf,
            preset=self.x264_combo.currentText(),
            audio_bitrate=self.audio_combo.currentText(),
            scale=scale,
        )
        self._run_preset_previews([cpreset])

    # ── Full compression ──

    def _compress_full(self):
        if not self._selected_preset or not self._source_path:
            return
        base = os.path.splitext(os.path.basename(self._source_path))[0]
        self._compressed_path = os.path.join(self._tmp_dir, f"{base}_compressed.mp4")

        self.compress_full_btn.setEnabled(False)
        self.full_progress.setVisible(True)
        self.full_progress.setValue(0)
        self.full_status.setText("Starting full compression...")

        self._full_worker = CompressWorker(
            self._source_path, self._compressed_path, self._selected_preset)
        self._full_worker.progress.connect(self._on_full_progress)
        self._full_worker.finished.connect(self._on_full_done)
        self._full_worker.error.connect(self._on_full_error)
        self._full_worker.start()

    def _on_full_progress(self, pct, msg):
        self.full_progress.setValue(pct)
        self.full_status.setText(msg)

    def _on_full_done(self, path, stats):
        self.full_progress.setValue(100)
        self.full_status.setText("Compression complete!")
        self._compressed_path = path

        self.stats_label.setText(
            f"<b>Original:</b> {fmt_size(stats['input_size'])}  "
            f"\u2192  <b>Compressed:</b> {fmt_size(stats['output_size'])}  |  "
            f"<span style='color: #4CAF50;'>{stats['savings']:.1f}% smaller</span>")
        self.cstack.setCurrentIndex(2)
        # Defer MPV init so the preview widget is visible/mapped first
        QTimer.singleShot(100, lambda: self._start_preview(path))

    def _start_preview(self, path):
        self.preview_widget.repaint()
        QApplication.processEvents()
        self._init_preview_mpv()
        if self._preview_mpv:
            self._preview_mpv.play(path)

    def _on_full_error(self, msg):
        self.full_progress.setVisible(False)
        self.full_status.setText(f"Error: {msg}")
        self.compress_full_btn.setEnabled(True)

    # ── Preview player ──

    def _init_preview_mpv(self):
        if self._preview_mpv:
            return
        wid = int(self.preview_widget.winId())
        try:
            self._preview_mpv = mpv.MPV(
                wid=str(wid), vo='gpu', gpu_context='x11egl',
                keep_open='yes', keep_open_pause='yes',
                input_default_bindings=False, input_vo_keyboard=False,
                osc=False, cursor_autohide='no',
                log_handler=lambda *a: None)
        except Exception:
            try:
                self._preview_mpv = mpv.MPV(
                    wid=str(wid), vo='x11',
                    keep_open='yes', keep_open_pause='yes',
                    input_default_bindings=False, input_vo_keyboard=False,
                    osc=False, log_handler=lambda *a: None)
            except Exception:
                self._preview_mpv = None

    def _toggle_preview_play(self):
        if self._preview_mpv:
            self._preview_mpv.cycle('pause')

    # ── File actions ──

    def _save_as(self):
        if not self._compressed_path or not os.path.exists(self._compressed_path):
            return
        default_name = os.path.basename(self._compressed_path)
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Compressed Video", default_name,
            "MP4 Files (*.mp4);;All Files (*)")
        if path:
            shutil.copy2(self._compressed_path, path)
            self.full_status.setText(f"Saved to {path}")

    def _replace_original(self):
        if not self._compressed_path or not self._source_path:
            return
        reply = styled_msg(
            self, QMessageBox.Question, "Replace Original",
            f"This will overwrite:\n{self._source_path}\n\nwith the compressed version. Continue?",
            QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            shutil.copy2(self._compressed_path, self._source_path)
            self.full_status.setText("Original replaced with compressed version.")

    def _delete_original(self):
        if not self._source_path:
            return
        reply = styled_msg(
            self, QMessageBox.Question, "Delete Original",
            f"Permanently delete:\n{self._source_path}\n\nThis cannot be undone!",
            QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            try:
                os.unlink(self._source_path)
                self.full_status.setText("Original file deleted.")
                self.delete_btn.setEnabled(False)
                self.replace_btn.setEnabled(False)
            except Exception as e:
                self.full_status.setText(f"Delete failed: {e}")

    def _cleanup_temps(self):
        if self._tmp_dir and os.path.isdir(self._tmp_dir):
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
            self._tmp_dir = None
        self.full_status.setText("Temp files cleaned up.")

    def _start_over(self):
        self._stop_workers()
        if self._preview_mpv:
            try:
                self._preview_mpv.terminate()
            except Exception:
                pass
            self._preview_mpv = None
        self._cleanup_temps()
        self._clear_cards()
        self._source_path = None
        self._snippet_path = None
        self._compressed_path = None
        self._selected_preset = None
        self.source_label.setText("No file selected")
        self.source_label.setStyleSheet("font-size: 14px; color: #999;")
        self.compress_full_btn.setEnabled(False)
        self.compress_full_btn.setText("Compress Full Video")
        self.full_progress.setVisible(False)
        self.full_status.setText("")
        self.delete_btn.setEnabled(True)
        self.replace_btn.setEnabled(True)
        self.cstack.setCurrentIndex(0)

    def _stop_workers(self):
        if self._snippet_worker and self._snippet_worker.isRunning():
            self._snippet_worker.terminate()
            self._snippet_worker.wait(2000)
        for w in self._preview_workers:
            if w.isRunning():
                w.terminate()
                w.wait(1000)
        self._preview_workers.clear()
        if self._full_worker and self._full_worker.isRunning():
            self._full_worker.terminate()
            self._full_worker.wait(2000)

    def _clear_cards(self):
        for card in self._cards:
            self._cards_layout.removeWidget(card)
            card.deleteLater()
        self._cards.clear()

    def cleanup(self):
        self._stop_workers()
        if self._preview_mpv:
            try:
                self._preview_mpv.terminate()
            except Exception:
                pass
            self._preview_mpv = None
        if self._tmp_dir and os.path.isdir(self._tmp_dir):
            shutil.rmtree(self._tmp_dir, ignore_errors=True)


# ── Main Window ──────────────────────────────────────────────────────────────

class QuickVideoApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("QuickVideo")
        self.setMinimumSize(1000, 750)
        self.filepath = None
        self.source_url = None
        self.duration = 0
        self.position = 0
        self.segments = []
        self.selected_segment = -1
        self.thumb_worker = None
        self.waveform_worker = None
        self.video_info = None
        self.dl_worker = None
        self.ytdlp_path = find_ytdlp()
        self._queue = load_queue()
        self._queue_worker = None

        # Undo/redo history
        self._undo_stack = []
        self._redo_stack = []

        # Export panel (shown during export in right pane)
        self.export_panel = None

        # Music layer
        self.music_file = None
        self.music_mpv = None
        self.music_volume = 50
        self._music_sync_counter = 0

        # Privacy mode
        self._privacy_mode = PRIVACY_MODE

        # Playback state
        self.playing = False
        self.play_speed = 1.0
        self.mpv_player = None
        self.play_timer = QTimer()
        self.play_timer.timeout.connect(self._play_tick)

        # Debounce timer for seeking — avoids restarting ffplay on every frame step
        self._seek_debounce = QTimer()
        self._seek_debounce.setSingleShot(True)
        self._seek_debounce.setInterval(50)
        self._seek_debounce.timeout.connect(self._debounced_seek)

        self._build_ui()
        self._apply_style()
        self._cleanup_orphaned_srts()

    def _cleanup_orphaned_srts(self):
        """Remove .srt files in download_dir whose matching video is gone."""
        try:
            dl_dir = Path(DOWNLOAD_DIR)
            if not dl_dir.exists():
                return
            for srt in dl_dir.glob("*.srt"):
                # Check if any video file with the same stem exists
                has_video = any(
                    srt.with_suffix(ext).exists()
                    for ext in (".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".ts", ".m4v")
                )
                if not has_video:
                    srt.unlink()
        except Exception:
            pass

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setSpacing(6)
        outer.setContentsMargins(12, 10, 12, 8)

        # ── Tab bar: Download / Open ─────────────────────────────────
        self.tabs = QTabWidget()
        self._tabs_expanded = True
        self._tabs_full_height = 120
        self._tabs_collapsed_height = 32
        self.tabs.setFixedHeight(self._tabs_full_height)
        self.tabs.tabBarClicked.connect(self._on_tab_clicked)

        # Auto-collapse timer
        self._tab_collapse_timer = QTimer()
        self._tab_collapse_timer.setSingleShot(True)
        self._tab_collapse_timer.setInterval(30000)
        self._tab_collapse_timer.timeout.connect(self._collapse_tabs)
        self._tab_collapse_timer.start()

        # Tab 1: Download URL (default)
        dl_tab = QWidget()
        dl_layout = QVBoxLayout(dl_tab)
        dl_layout.setContentsMargins(16, 10, 16, 10)
        url_row = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Paste video URL (YouTube, TikTok, Twitter, etc.)")
        self.url_input.setFixedHeight(38)
        self.url_input.setStyleSheet("font-size: 14px; padding: 4px 12px;")
        self.url_input.returnPressed.connect(self._download_url)
        url_row.addWidget(self.url_input, 1)
        self.dl_btn = QPushButton("Download")
        self.dl_btn.setFixedSize(130, 38)
        self.dl_btn.setFocusPolicy(Qt.NoFocus)
        self.dl_btn.clicked.connect(self._download_url)
        self.dl_btn.setStyleSheet("QPushButton { background: #E65100; color: white; font-size: 14px; font-weight: bold; border-radius: 6px; }")
        url_row.addWidget(self.dl_btn)
        dl_layout.addLayout(url_row)
        self.dl_progress = QProgressBar()
        self.dl_progress.setFixedHeight(20)
        self.dl_progress.setVisible(False)
        dl_layout.addWidget(self.dl_progress)
        self.dl_status = QLabel("")
        self.dl_status.setStyleSheet("color: #aaa; font-size: 12px;")
        dl_layout.addWidget(self.dl_status)
        ytdlp_status = "yt-dlp found" if self.ytdlp_path else "yt-dlp NOT found — install: sudo pacman -S yt-dlp"
        ytdlp_color = "#4CAF50" if self.ytdlp_path else "#f44336"
        self.dl_status.setText(f'<span style="color:{ytdlp_color}">{ytdlp_status}</span>')
        self.tabs.addTab(dl_tab, "Download URL")

        # Tab 2: Open file
        open_tab = QWidget()
        open_layout = QVBoxLayout(open_tab)
        open_layout.setContentsMargins(16, 8, 16, 8)
        open_top = QHBoxLayout()
        self.open_btn = QPushButton("Open Video File")
        self.open_btn.setFixedSize(200, 38)
        self.open_btn.setFocusPolicy(Qt.NoFocus)
        self.open_btn.clicked.connect(self._open_file)
        self.open_btn.setStyleSheet("QPushButton { background: #1565C0; color: white; font-size: 15px; font-weight: bold; border-radius: 6px; }")
        open_top.addWidget(self.open_btn)
        self.file_label = QLabel("No file loaded — open or drag & drop a video")
        self.file_label.setStyleSheet("color: #999; font-size: 14px; padding-left: 16px;")
        open_top.addWidget(self.file_label, 1)
        open_layout.addLayout(open_top)
        # Recent files row
        self._recents_row = QHBoxLayout()
        self._recents_row.setSpacing(6)
        self._recents_row.addStretch()
        open_layout.addLayout(self._recents_row)
        self.tabs.addTab(open_tab, "Open File")
        self._refresh_recents()

        # Tab 3: Download Queue
        queue_tab = QWidget()
        queue_layout = QVBoxLayout(queue_tab)
        queue_layout.setContentsMargins(16, 10, 16, 10)
        queue_top = QHBoxLayout()
        self.queue_input = QLineEdit()
        self.queue_input.setPlaceholderText("Paste URL to queue for background download")
        self.queue_input.setFixedHeight(38)
        self.queue_input.setStyleSheet("font-size: 14px; padding: 4px 12px;")
        self.queue_input.returnPressed.connect(self._queue_add)
        queue_top.addWidget(self.queue_input, 1)
        self.queue_add_btn = QPushButton("Add to Queue")
        self.queue_add_btn.setFixedSize(140, 38)
        self.queue_add_btn.setFocusPolicy(Qt.NoFocus)
        self.queue_add_btn.clicked.connect(self._queue_add)
        self.queue_add_btn.setStyleSheet("QPushButton { background: #6A1B9A; color: white; font-size: 14px; font-weight: bold; border-radius: 6px; }")
        queue_top.addWidget(self.queue_add_btn)
        queue_layout.addLayout(queue_top)
        queue_bottom = QHBoxLayout()
        self.queue_status = QLabel("")
        self.queue_status.setStyleSheet("color: #aaa; font-size: 12px;")
        queue_bottom.addWidget(self.queue_status, 1)
        self.queue_progress = QProgressBar()
        self.queue_progress.setFixedHeight(20)
        self.queue_progress.setFixedWidth(300)
        self.queue_progress.setVisible(False)
        queue_bottom.addWidget(self.queue_progress)
        queue_layout.addLayout(queue_bottom)
        self.tabs.addTab(queue_tab, "Queue")

        # Tab 4: Compress
        compress_tab = QWidget()
        compress_layout = QVBoxLayout(compress_tab)
        compress_layout.setContentsMargins(16, 10, 16, 10)
        compress_hint = QLabel(
            "Compress videos with quality preview \u2014 "
            "select a file below or use the current video")
        compress_hint.setStyleSheet("color: #aaa; font-size: 13px;")
        compress_layout.addWidget(compress_hint)
        compress_layout.addStretch()
        self.tabs.addTab(compress_tab, "Compress")
        self.tabs.currentChanged.connect(self._on_tab_changed)

        settings_btn = QPushButton("\u2699 Settings")
        settings_btn.setFixedSize(100, 28)
        settings_btn.setFocusPolicy(Qt.NoFocus)
        settings_btn.setCursor(Qt.PointingHandCursor)
        settings_btn.setStyleSheet(
            "QPushButton { background: #3a3a3a; color: #bbb; font-size: 12px; "
            "border: 1px solid #555; border-radius: 4px; } "
            "QPushButton:hover { background: #4a4a4a; color: #fff; }"
        )
        settings_btn.clicked.connect(self._open_settings)
        self.tabs.setCornerWidget(settings_btn, Qt.TopRightCorner)
        # Add right padding so corner widget doesn't clip
        self.tabs.setStyleSheet(self.tabs.styleSheet() + "QTabWidget { padding-right: 110px; }" if self.tabs.styleSheet() else "QTabWidget { padding-right: 110px; }")

        outer.addWidget(self.tabs)

        # ── Main splitter: left (video+controls) | right (segments) ──
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setHandleWidth(6)
        self.splitter.setStyleSheet("QSplitter::handle { background: #444; border-radius: 2px; }")

        # ── LEFT PANE: video + controls ──────────────────────────────
        left_pane = QWidget()
        left = QVBoxLayout(left_pane)
        left.setSpacing(6)
        left.setContentsMargins(0, 0, 8, 0)

        # Video info
        self.info_label = QLabel("")
        self.info_label.setStyleSheet("color: #aaa; font-size: 13px; padding: 2px 4px;")
        left.addWidget(self.info_label)

        # Video preview via mpv
        self.video_widget = QWidget()
        self.video_widget.setMinimumHeight(200)
        self.video_widget.setStyleSheet("background: #0a0a0a;")
        self.video_widget.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.video_widget.setAttribute(Qt.WA_NativeWindow)
        left.addWidget(self.video_widget, 1)

        # Click-to-seek handled via eventFilter on video_widget directly
        self.video_widget.installEventFilter(self)

        # Privacy mode: use mpv brightness=-100 to black out video (no overlay needed)

        # Placeholder label (shown over video widget when nothing loaded)
        self.frame_label = QLabel("Open a video to begin")
        self.frame_label.setAlignment(Qt.AlignCenter)
        self.frame_label.setStyleSheet("color: #555; font-size: 18px;")
        # Will be hidden once video loads

        left.addSpacing(8)

        # Position + playback display
        pos_layout = QHBoxLayout()
        pos_layout.setSpacing(10)

        self.play_state_label = QLabel("||")
        self.play_state_label.setStyleSheet("font-size: 18px; color: #888; min-width: 28px;")
        pos_layout.addWidget(self.play_state_label)

        self.pos_label = QLabel("0:00.00")
        self.pos_label.setStyleSheet("font-family: monospace; font-size: 22px; color: #FF9800; font-weight: bold;")
        pos_layout.addWidget(self.pos_label)

        self.duration_label = QLabel("/ --")
        self.duration_label.setStyleSheet("font-family: monospace; font-size: 16px; color: #666;")
        pos_layout.addWidget(self.duration_label)

        self.speed_label = QLabel("1x")
        self.speed_label.setStyleSheet("font-family: monospace; font-size: 14px; color: #666; padding-left: 8px;")
        pos_layout.addWidget(self.speed_label)

        self._privacy_indicator = QLabel("\u25cf")
        self._privacy_indicator.setStyleSheet("font-size: 10px; color: #e91e63; padding-left: 4px;")
        self._privacy_indicator.setToolTip("Privacy mode")
        self._privacy_indicator.setVisible(self._privacy_mode)
        pos_layout.addWidget(self._privacy_indicator)

        pos_layout.addStretch()

        self.kept_duration_label = QLabel("")
        self.kept_duration_label.setStyleSheet("font-family: monospace; font-size: 14px; color: #4CAF50; font-weight: bold;")
        self.kept_duration_label.setAlignment(Qt.AlignCenter)
        pos_layout.addWidget(self.kept_duration_label)

        pos_layout.addStretch()

        self.time_input = QLineEdit()
        self.time_input.setPlaceholderText("Jump to (e.g. 1:30)")
        self.time_input.setFixedWidth(160)
        self.time_input.setFixedHeight(32)
        self.time_input.setStyleSheet("font-size: 13px;")
        self.time_input.returnPressed.connect(self._jump_to_time)
        pos_layout.addWidget(self.time_input)

        jump_btn = QPushButton("Go")
        jump_btn.setFixedSize(44, 32)
        jump_btn.setFocusPolicy(Qt.NoFocus)
        jump_btn.clicked.connect(self._jump_to_time)
        pos_layout.addWidget(jump_btn)
        left.addLayout(pos_layout)

        left.addSpacing(4)

        # Timeline
        self.timeline = TimelineWidget()
        self.timeline.position_changed.connect(self._on_timeline_seek)
        self.timeline.seek_finished.connect(self._on_timeline_seek_finished)
        left.addWidget(self.timeline)

        # Music layer controls
        music_layout = QHBoxLayout()
        music_layout.setSpacing(6)

        self.music_toggle_btn = QPushButton("Music")
        self.music_toggle_btn.setFixedHeight(30)
        self.music_toggle_btn.setFixedWidth(70)
        self.music_toggle_btn.setFocusPolicy(Qt.NoFocus)
        self.music_toggle_btn.setCursor(Qt.PointingHandCursor)
        self.music_toggle_btn.setCheckable(True)
        self.music_toggle_btn.setStyleSheet("""
            QPushButton { background: #333; color: #aaa; border: 1px solid #555; border-radius: 4px; font-size: 12px; }
            QPushButton:checked { background: #7B1FA2; color: #fff; border: 1px solid #9C27B0; }
        """)
        self.music_toggle_btn.clicked.connect(self._toggle_music_controls)
        music_layout.addWidget(self.music_toggle_btn)

        self.music_controls = QWidget()
        mc_layout = QHBoxLayout(self.music_controls)
        mc_layout.setContentsMargins(0, 0, 0, 0)
        mc_layout.setSpacing(6)

        self.music_browse_btn = QPushButton("Browse...")
        self.music_browse_btn.setFixedHeight(30)
        self.music_browse_btn.setFocusPolicy(Qt.NoFocus)
        self.music_browse_btn.setCursor(Qt.PointingHandCursor)
        self.music_browse_btn.clicked.connect(self._pick_music_file)
        mc_layout.addWidget(self.music_browse_btn)

        self.music_filename_label = QLabel("No music")
        self.music_filename_label.setStyleSheet("color: #888; font-size: 12px;")
        self.music_filename_label.setFixedWidth(200)
        mc_layout.addWidget(self.music_filename_label)

        self.music_volume_slider = QSlider(Qt.Horizontal)
        self.music_volume_slider.setRange(0, 100)
        self.music_volume_slider.setValue(50)
        self.music_volume_slider.setFixedHeight(24)
        self.music_volume_slider.setStyleSheet("""
            QSlider::groove:horizontal { background: #333; height: 6px; border-radius: 3px; }
            QSlider::handle:horizontal { background: #FF9800; width: 14px; margin: -4px 0; border-radius: 7px; }
            QSlider::sub-page:horizontal { background: #7B1FA2; border-radius: 3px; }
        """)
        self.music_volume_slider.valueChanged.connect(self._set_music_volume)
        mc_layout.addWidget(self.music_volume_slider)

        self.music_volume_label = QLabel("50%")
        self.music_volume_label.setStyleSheet("color: #aaa; font-size: 12px; min-width: 32px;")
        mc_layout.addWidget(self.music_volume_label)

        self.music_controls.setVisible(False)
        music_layout.addWidget(self.music_controls)
        music_layout.addStretch()
        left.addLayout(music_layout)

        left.addWidget(make_separator())

        # Action buttons
        actions = QHBoxLayout()
        actions.setSpacing(6)

        self.split_btn = action_btn("Split", "#1565C0", "S")
        self.split_btn.clicked.connect(self._split_at_current)
        actions.addWidget(self.split_btn)

        self.rm_before_btn = action_btn("Rm Before", "#E65100", "Q")
        self.rm_before_btn.clicked.connect(self._remove_before)
        actions.addWidget(self.rm_before_btn)

        self.rm_after_btn = action_btn("Rm After", "#E65100", "W")
        self.rm_after_btn.clicked.connect(self._remove_after)
        actions.addWidget(self.rm_after_btn)

        self.delete_btn = action_btn("Toggle Del", "#c62828", "X")
        self.delete_btn.clicked.connect(self._toggle_selected)
        actions.addWidget(self.delete_btn)

        actions.addSpacing(12)

        self.set_start_btn = action_btn("In", "#37474F", "I")
        self.set_start_btn.clicked.connect(self._set_trim_start)
        actions.addWidget(self.set_start_btn)

        self.set_end_btn = action_btn("Out", "#37474F", "O")
        self.set_end_btn.clicked.connect(self._set_trim_end)
        actions.addWidget(self.set_end_btn)

        actions.addStretch()

        self.export_btn = action_btn("Export", "#2E7D32", "Ctrl+E")
        self.export_btn.setFixedWidth(180)
        self.export_btn.setStyleSheet(self.export_btn.styleSheet().replace("font-size: 13px", "font-size: 15px"))
        self.export_btn.clicked.connect(self._export)
        actions.addWidget(self.export_btn)

        self.delete_orig_btn = QPushButton("Delete Video")
        self.delete_orig_btn.setFixedWidth(140)
        self.delete_orig_btn.setStyleSheet(
            "QPushButton { background: #b71c1c; color: #fff; border: 1px solid #e53935; border-radius: 4px; "
            "padding: 4px 8px; font-size: 13px; font-weight: bold; } "
            "QPushButton:hover { background: #d32f2f; }"
        )
        self.delete_orig_btn.clicked.connect(self._delete_original)
        actions.addWidget(self.delete_orig_btn)

        self.hamburger_btn = QPushButton("\u2630")
        self.hamburger_btn.setFixedSize(42, 42)
        self.hamburger_btn.setFocusPolicy(Qt.NoFocus)
        self.hamburger_btn.setCursor(Qt.PointingHandCursor)
        self.hamburger_btn.setStyleSheet(
            "QPushButton { background: #3a3a3a; color: #ccc; font-size: 20px; "
            "border: 1px solid #555; border-radius: 6px; } "
            "QPushButton:hover { background: #4a4a4a; color: #fff; }"
        )
        self.hamburger_btn.clicked.connect(self._show_hamburger_menu)
        actions.addWidget(self.hamburger_btn)

        left.addLayout(actions)

        # Status bar
        self.status = QLabel(
            "Space=play  S=split  Q/W=rm before/after  X=delete  "
            "I/O=in/out  J/K/L=speed  L/R=step  Up/Down,[/]=segments  Click video: L/R=±15s"
        )
        self.status.setStyleSheet("color: #555; font-size: 11px; padding: 4px 4px; font-family: monospace;")
        left.addWidget(self.status)

        self.export_progress = QProgressBar()
        self.export_progress.setFixedHeight(8)
        self.export_progress.setTextVisible(False)
        self.export_progress.setStyleSheet(
            "QProgressBar { background: #333; border: none; border-radius: 3px; }"
            "QProgressBar::chunk { background: #E65100; border-radius: 3px; }"
        )
        self.export_progress.setVisible(False)
        left.addWidget(self.export_progress)

        self.splitter.addWidget(left_pane)

        # ── RIGHT PANE: segments ─────────────────────────────────────
        self.right_pane = QWidget()
        self.right_pane.setMinimumWidth(320)
        self.right_pane.setStyleSheet("background: #2f2f2f; border-left: 1px solid #444;")
        self.right_layout = QVBoxLayout(self.right_pane)
        self.right_layout.setSpacing(8)
        self.right_layout.setContentsMargins(10, 8, 6, 8)

        self.seg_header = QLabel("<b>Segments</b>")
        self.seg_header.setStyleSheet("font-size: 15px; color: #ccc; padding: 4px 0;")
        self.right_layout.addWidget(self.seg_header)

        self.seg_separator = make_separator()
        self.right_layout.addWidget(self.seg_separator)

        self.seg_scroll = QScrollArea()
        self.seg_scroll.setWidgetResizable(True)
        self.seg_scroll.setStyleSheet("background: transparent;")
        self.seg_container = QWidget()
        self.seg_layout = QVBoxLayout(self.seg_container)
        self.seg_layout.setSpacing(10)
        self.seg_layout.setContentsMargins(8, 8, 8, 8)
        self.seg_layout.addStretch()
        self.seg_scroll.setWidget(self.seg_container)

        # Subtitles panel
        sub_pane = QWidget()
        sub_pane_layout = QVBoxLayout(sub_pane)
        sub_pane_layout.setSpacing(6)
        sub_pane_layout.setContentsMargins(0, 0, 0, 0)

        sub_header_row = QHBoxLayout()
        sub_header = QLabel("<b>Subtitles</b>")
        sub_header.setStyleSheet("font-size: 15px; color: #ccc; padding: 4px 0;")
        sub_header_row.addWidget(sub_header)
        sub_header_row.addStretch()
        self.gen_subs_btn = QPushButton("Generate  [Ctrl+T]")
        self.gen_subs_btn.setFixedHeight(28)
        self.gen_subs_btn.setFocusPolicy(Qt.NoFocus)
        self.gen_subs_btn.setCursor(Qt.PointingHandCursor)
        self.gen_subs_btn.setStyleSheet(
            "QPushButton { background: #1565C0; color: white; font-size: 11px; font-weight: bold; "
            "border-radius: 4px; padding: 2px 10px; } "
            "QPushButton:hover { background: #1976D2; }"
        )
        self.gen_subs_btn.clicked.connect(self._generate_subtitles)
        sub_header_row.addWidget(self.gen_subs_btn)
        self.toggle_subs_btn = QPushButton("On")
        self.toggle_subs_btn.setFixedSize(55, 28)
        self.toggle_subs_btn.setFocusPolicy(Qt.NoFocus)
        self.toggle_subs_btn.setCursor(Qt.PointingHandCursor)
        self.toggle_subs_btn.setCheckable(True)
        self.toggle_subs_btn.setChecked(True)
        self.subs_enabled = True
        self.toggle_subs_btn.clicked.connect(self._toggle_subs)
        self._update_subs_toggle_style()
        sub_header_row.addWidget(self.toggle_subs_btn)
        sub_pane_layout.addLayout(sub_header_row)

        self.sub_list = QListWidget()
        self.sub_list.setStyleSheet("""
            QListWidget { background: #252525; border: 1px solid #444; border-radius: 4px; font-size: 12px; }
            QListWidget::item { padding: 4px 6px; color: #ccc; }
            QListWidget::item:selected { background: #3a5a8a; color: #fff; }
            QListWidget::item:hover { background: #383838; }
        """)
        self.sub_list.itemClicked.connect(self._on_subtitle_clicked)
        sub_pane_layout.addWidget(self.sub_list, 1)

        self.subtitles = []  # list of (start, end, text)

        # Vertical splitter for segments / subtitles
        self.right_splitter = QSplitter(Qt.Vertical)
        self.right_splitter.setHandleWidth(5)
        self.right_splitter.setStyleSheet("QSplitter::handle { background: #444; border-radius: 2px; }")
        self.right_splitter.addWidget(self.seg_scroll)
        self.right_splitter.addWidget(sub_pane)
        self.right_splitter.setSizes([300, 300])
        self.right_layout.addWidget(self.right_splitter, 1)

        self.splitter.addWidget(self.right_pane)
        self.splitter.setCollapsible(1, False)

        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        # Set initial sizes: 70% left, 30% right
        self.splitter.setSizes([1400, 600])

        # Wrap splitter + compress panel in a stacked widget
        self.compress_panel = CompressPanel(self)
        self.content_stack = QStackedWidget()
        self.content_stack.addWidget(self.splitter)
        self.content_stack.addWidget(self.compress_panel)
        outer.addWidget(self.content_stack, 1)

    def _on_tab_changed(self, index):
        if index == 3:  # Compress tab
            self.content_stack.setCurrentIndex(1)
        else:
            self.content_stack.setCurrentIndex(0)

    def _apply_style(self):
        self.setStyleSheet("""
            QMainWindow { background: #2b2b2b; }
            QWidget { color: #eee; font-family: 'Segoe UI', 'SF Pro', 'Noto Sans', sans-serif; }
            QPushButton {
                background: #3a3a3a; color: #ddd; border: 1px solid #555;
                border-radius: 6px; padding: 6px 14px; font-size: 13px;
            }
            QPushButton:hover { background: #484848; border-color: #777; }
            QPushButton:pressed { background: #555; }
            QLineEdit {
                background: #333; color: #eee; border: 1px solid #555;
                border-radius: 6px; padding: 6px 12px; font-size: 13px;
            }
            QLineEdit:focus { border-color: #FF9800; }
            QScrollArea { border: none; background: transparent; }
            QCheckBox::indicator { width: 22px; height: 22px; }
            QTabWidget::pane { border: 1px solid #444; border-radius: 8px; background: #303030; }
            QTabBar::tab {
                background: #383838; color: #bbb; padding: 8px 16px; font-size: 13px;
                border-top-left-radius: 8px; border-top-right-radius: 8px; margin-right: 2px;
            }
            QTabBar::tab:selected { background: #303030; color: #FF9800; font-weight: bold; }
            QProgressBar { background: #333; border: 1px solid #555; border-radius: 4px; text-align: center; color: #eee; font-size: 12px; }
            QProgressBar::chunk { background: #E65100; border-radius: 3px; }
        """)

        # Resume any pending queue items from last session
        if self._queue:
            self._queue_update_status()
            self._queue_start_next()

    # ── mpv player ─────────────────────────────────────────────────

    def _init_mpv(self):
        """Create the mpv player embedded in video_widget."""
        if hasattr(self, 'mpv_player') and self.mpv_player:
            return
        wid = int(self.video_widget.winId())
        try:
            self.mpv_player = mpv.MPV(
                wid=str(wid),
                vo='gpu',
                gpu_context='x11egl',
                keep_open='yes',
                keep_open_pause='yes',
                input_default_bindings=False,
                input_vo_keyboard=False,
                osc=False,
                cursor_autohide='no',
                log_handler=lambda *a: None,
            )
            if self._privacy_mode:
                self.mpv_player.brightness = -100
        except Exception as e:
            self.status.setText(f"mpv init failed: {e}")
            self.mpv_player = None

    def _toggle_privacy(self):
        global PRIVACY_MODE
        self._privacy_mode = not self._privacy_mode
        PRIVACY_MODE = self._privacy_mode
        if hasattr(self, 'mpv_player') and self.mpv_player:
            try:
                self.mpv_player.brightness = -100 if self._privacy_mode else 0
            except Exception:
                pass
        self._privacy_indicator.setVisible(self._privacy_mode)
        self.timeline.privacy_mode = self._privacy_mode
        self.timeline.update()
        _save_settings()

    def _toggle_play(self):
        if not self.filepath:
            return
        if not hasattr(self, 'mpv_player') or not self.mpv_player:
            return
        if self.playing:
            self._pause()
        else:
            self._play()

    def _play(self):
        if not self.filepath:
            return
        if not hasattr(self, 'mpv_player') or not self.mpv_player:
            return
        try:
            # Just unpause — mpv is already at the right position from seeks
            self.mpv_player.pause = False
            # Sync our position from mpv's actual position
            t = self.mpv_player.time_pos
            if t is not None:
                self.position = t
        except Exception:
            return
        if self.music_mpv:
            try:
                self.music_mpv.pause = False
            except Exception:
                pass
        self.playing = True
        self.play_timer.start(100)
        self._update_play_ui()

    def _pause(self):
        if hasattr(self, 'mpv_player') and self.mpv_player:
            try:
                self.mpv_player.pause = True
            except Exception:
                pass
        if self.music_mpv:
            try:
                self.music_mpv.pause = True
            except Exception:
                pass
        self.playing = False
        self.play_timer.stop()
        if self.play_speed != 1.0:
            self._speed_reset()
        else:
            self._update_play_ui()
        self._sync_pos_from_mpv()

    def _play_tick(self):
        self._sync_pos_from_mpv()
        # Periodic music drift correction
        self._music_sync_counter += 1
        if self.music_mpv and self.playing and self._music_sync_counter % 5 == 0:
            try:
                vt = self.mpv_player.time_pos
                mt = self.music_mpv.time_pos
                if vt is not None and mt is not None and abs(vt - mt) > 0.5:
                    self.music_mpv.seek(vt, reference='absolute', precision='exact')
            except Exception:
                pass

    def _sync_pos_from_mpv(self):
        if hasattr(self, 'mpv_player') and self.mpv_player:
            try:
                t = self.mpv_player.time_pos
                if t is not None:
                    self.position = t
            except Exception:
                pass
        self.pos_label.setText(fmt_time(self.position))
        self.timeline.set_position(self.position)
        if self.subtitles:
            self._highlight_current_subtitle()

    def _mpv_seek(self, time_sec):
        """Seek mpv to a specific time."""
        if hasattr(self, 'mpv_player') and self.mpv_player:
            try:
                self.mpv_player.seek(time_sec, reference='absolute', precision='exact')
            except Exception:
                pass
        self._sync_music_seek(time_sec)

    def _update_play_ui(self):
        if self.playing:
            self.play_state_label.setText(">")
            self.play_state_label.setStyleSheet("font-size: 18px; color: #4CAF50; min-width: 28px;")
        else:
            self.play_state_label.setText("||")
            self.play_state_label.setStyleSheet("font-size: 18px; color: #888; min-width: 28px;")
        speed_str = f"{self.play_speed}x" if self.play_speed != int(self.play_speed) else f"{int(self.play_speed)}x"
        self.speed_label.setText(speed_str)
        if self.play_speed != 1.0:
            self.speed_label.setStyleSheet("font-family: monospace; font-size: 14px; color: #FF9800; padding-left: 8px;")
        else:
            self.speed_label.setStyleSheet("font-family: monospace; font-size: 14px; color: #666; padding-left: 8px;")

    def _speed_up(self):
        idx = SPEED_STEPS.index(self.play_speed) if self.play_speed in SPEED_STEPS else 2
        if idx < len(SPEED_STEPS) - 1:
            self.play_speed = SPEED_STEPS[idx + 1]
        if hasattr(self, 'mpv_player') and self.mpv_player:
            self.mpv_player.speed = self.play_speed
        if self.music_mpv:
            try: self.music_mpv.speed = self.play_speed
            except Exception: pass
        self._update_play_ui()
        self.status.setText(f"Speed: {self.play_speed}x")

    def _speed_down(self):
        idx = SPEED_STEPS.index(self.play_speed) if self.play_speed in SPEED_STEPS else 2
        if idx > 0:
            self.play_speed = SPEED_STEPS[idx - 1]
        if hasattr(self, 'mpv_player') and self.mpv_player:
            self.mpv_player.speed = self.play_speed
        if self.music_mpv:
            try: self.music_mpv.speed = self.play_speed
            except Exception: pass
        self._update_play_ui()
        self.status.setText(f"Speed: {self.play_speed}x")

    def _speed_reset(self):
        self.play_speed = 1.0
        if hasattr(self, 'mpv_player') and self.mpv_player:
            self.mpv_player.speed = 1.0
        if self.music_mpv:
            try: self.music_mpv.speed = 1.0
            except Exception: pass
        self._update_play_ui()
        self.status.setText("Speed: 1x")

    # ── Music Layer ─────────────────────────────────────────────────

    def _toggle_music_controls(self):
        visible = self.music_toggle_btn.isChecked()
        self.music_controls.setVisible(visible)

    def _pick_music_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Music File", "",
            "Audio Files (*.mp3 *.wav *.flac *.ogg *.m4a *.aac);;All Files (*)"
        )
        if not path:
            return
        self.music_file = path
        self.music_filename_label.setText(os.path.basename(path))
        self.music_filename_label.setStyleSheet("color: #ccc; font-size: 12px;")
        self._init_music_mpv()

    def _init_music_mpv(self):
        if self.music_mpv:
            try:
                self.music_mpv.terminate()
            except Exception:
                pass
            self.music_mpv = None
        if not self.music_file:
            return
        try:
            self.music_mpv = mpv.MPV(
                vid='no',
                input_default_bindings=False,
                log_handler=lambda *a: None,
            )
            self.music_mpv.play(self.music_file)
            self.music_mpv.wait_until_playing()
            self.music_mpv.pause = True
            self.music_mpv.volume = self.music_volume
            # Sync to current video position
            if self.filepath and self.position > 0:
                try:
                    self.music_mpv.seek(self.position, reference='absolute', precision='exact')
                except Exception:
                    pass
            self.status.setText(f"Music loaded: {os.path.basename(self.music_file)}")
        except Exception as e:
            self.status.setText(f"Music load failed: {e}")
            self.music_mpv = None

    def _set_music_volume(self, value):
        self.music_volume = value
        self.music_volume_label.setText(f"{value}%")
        if self.music_mpv:
            try:
                self.music_mpv.volume = value
            except Exception:
                pass

    def _sync_music_seek(self, time_sec):
        if self.music_mpv:
            try:
                self.music_mpv.seek(time_sec, reference='absolute', precision='exact')
            except Exception:
                pass

    # ── Download ─────────────────────────────────────────────────────

    def _download_url(self):
        url = self.url_input.text().strip()
        if not url:
            return
        if not self.ytdlp_path:
            styled_msg(self, QMessageBox.Critical, "yt-dlp not found",
                "Install yt-dlp first:\n\nsudo pacman -S yt-dlp")
            return
        if self.dl_worker and self.dl_worker.isRunning():
            self.dl_status.setText("Download already in progress...")
            return
        dl_path = get_download_dir()
        dl_path.mkdir(parents=True, exist_ok=True)
        dl_dir = str(dl_path)
        self.dl_progress.setVisible(True)
        self.dl_progress.setValue(0)
        # Swap to Cancel button
        self.dl_btn.setText("Cancel")
        self.dl_btn.setStyleSheet("QPushButton { background: #c62828; color: white; font-size: 14px; font-weight: bold; border-radius: 6px; }")
        self.dl_btn.clicked.disconnect()
        self.dl_btn.clicked.connect(self._cancel_download)
        try:
            save_download_link(url)
        except Exception:
            pass
        self.dl_worker = DownloadWorker(url, dl_dir, self.ytdlp_path)
        self.dl_worker.progress.connect(self._on_dl_progress)
        self.dl_worker.finished.connect(self._on_dl_finished)
        self.dl_worker.error.connect(self._on_dl_error)
        self.dl_worker.cancelled.connect(self._on_dl_cancelled)
        self.dl_worker.start()

    def _cancel_download(self):
        if self.dl_worker and self.dl_worker.isRunning():
            self.dl_worker.cancel()
            self.dl_status.setText("Cancelling...")

    def _reset_dl_btn(self):
        """Restore the download button to its normal state."""
        self.dl_btn.setText("Download")
        self.dl_btn.setStyleSheet("QPushButton { background: #E65100; color: white; font-size: 14px; font-weight: bold; border-radius: 6px; }")
        try:
            self.dl_btn.clicked.disconnect()
        except Exception:
            pass
        self.dl_btn.clicked.connect(self._download_url)
        self.dl_progress.setVisible(False)
        self.dl_progress.setRange(0, 100)

    def _on_dl_cancelled(self):
        self._reset_dl_btn()
        self.dl_status.setText("Download cancelled.")

    def _on_dl_progress(self, msg, pct):
        if pct >= 0:
            self.dl_progress.setRange(0, 100)
            self.dl_progress.setValue(int(pct))
            self.dl_progress.setFormat(f"{pct:.1f}%")
        else:
            self.dl_progress.setRange(0, 0)
        self.dl_status.setText(msg[:120])

    def _on_dl_finished(self, filepath):
        self._reset_dl_btn()
        self.dl_status.setText(f"Downloaded: {Path(filepath).name}")
        self.source_url = self.url_input.text().strip()
        self._load_video(filepath)
        # Restore segments if this was a redownload from quick_load
        if hasattr(self, '_pending_load') and self._pending_load:
            pl = self._pending_load
            self._pending_load = None
            self.source_url = pl["url"]
            self.segments = pl["segments"]
            self.selected_segment = pl["selected"]
            self.position = pl["position"]
            self._update_segments_ui()
            self.timeline.set_segments(self.segments)
            self._highlight_segment(self.selected_segment)
            self._update_frame_preview()
            self.status.setText("Redownloaded and restored segments")

    def _on_dl_error(self, err):
        self._reset_dl_btn()
        self.dl_status.setText(f'<span style="color:#f44336">Error: {err[:200]}</span>')

    # ── Download Queue ───────────────────────────────────────────────

    def _queue_add(self):
        url = self.queue_input.text().strip()
        if not url:
            return
        if not self.ytdlp_path:
            self.queue_status.setText('<span style="color:#f44336">yt-dlp not found</span>')
            return
        self._queue.append(url)
        self.queue_input.clear()
        try:
            save_download_link(url)
            save_queue(self._queue)
        except Exception:
            pass
        self._queue_update_status()
        self._queue_start_next()

    def _queue_update_status(self):
        active = "downloading" if self._queue_worker and self._queue_worker.isRunning() else "idle"
        self.queue_status.setText(f"{len(self._queue)} queued  |  {active}")

    def _queue_start_next(self):
        if self._queue_worker and self._queue_worker.isRunning():
            return
        if not self._queue:
            self.queue_progress.setVisible(False)
            self._queue_update_status()
            return
        url = self._queue.pop(0)
        try:
            save_queue(self._queue)
        except Exception:
            pass
        dl_path = get_download_dir()
        dl_path.mkdir(parents=True, exist_ok=True)
        self.queue_progress.setVisible(True)
        self.queue_progress.setValue(0)
        self.queue_status.setText(f"Downloading: {url[:80]}...  ({len(self._queue)} queued)")
        self._queue_worker = DownloadWorker(url, str(dl_path), self.ytdlp_path)
        self._queue_worker.progress.connect(self._on_queue_progress)
        self._queue_worker.finished.connect(self._on_queue_finished)
        self._queue_worker.error.connect(self._on_queue_error)
        self._queue_worker.start()

    def _on_queue_progress(self, msg, pct):
        if pct >= 0:
            self.queue_progress.setRange(0, 100)
            self.queue_progress.setValue(int(pct))
            self.queue_progress.setFormat(f"{pct:.1f}%")
        else:
            self.queue_progress.setRange(0, 0)

    def _on_queue_finished(self, filepath):
        self.queue_status.setText(f"Done: {Path(filepath).name}  ({len(self._queue)} queued)")
        self._queue_start_next()

    def _on_queue_error(self, err):
        self.queue_status.setText(f'<span style="color:#f44336">Error: {err[:100]}</span>  ({len(self._queue)} queued)')
        self._queue_start_next()

    # ── File loading ─────────────────────────────────────────────────

    def _refresh_recents(self):
        """Rebuild the recent-files buttons in the Open tab."""
        # Clear old buttons
        while self._recents_row.count():
            item = self._recents_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        recents = load_recents(5)
        if not recents:
            return
        btn_style = "QPushButton { background: #424242; color: #ccc; font-size: 11px; border-radius: 4px; padding: 4px 10px; } QPushButton:hover { background: #555; }"
        for path in recents:
            name = Path(path).stem
            short = (name[:28] + "...") if len(name) > 31 else name
            btn = QPushButton(short)
            btn.setToolTip(path)
            btn.setFixedHeight(28)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.setStyleSheet(btn_style)
            btn.clicked.connect(lambda checked, p=path: self._load_video(p))
            self._recents_row.addWidget(btn)
        clear_btn = QPushButton("Clear")
        clear_btn.setFixedHeight(28)
        clear_btn.setFocusPolicy(Qt.NoFocus)
        clear_btn.setStyleSheet("QPushButton { background: #333; color: #888; font-size: 11px; border-radius: 4px; padding: 4px 8px; } QPushButton:hover { background: #555; color: #f44336; }")
        clear_btn.clicked.connect(self._clear_recents)
        self._recents_row.addWidget(clear_btn)
        self._recents_row.addStretch()

    def _clear_recents(self):
        clear_recents()
        self._refresh_recents()

    def _show_hamburger_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background: #2b2b2b; border: 1px solid #555; border-radius: 4px; padding: 4px; }"
            "QMenu::item { color: #ddd; padding: 8px 20px; font-size: 13px; }"
            "QMenu::item:selected { background: #3a5a8a; }"
        )
        menu.addAction("Open Media Folder", self._open_file_folder)
        menu.addAction("Open Export Folder", self._open_export_folder)
        menu.addSeparator()
        menu.addAction("Redownload Video", self._redownload_video)
        menu.addSeparator()
        menu.addAction("Undo  [Ctrl+Z]", self._undo)
        menu.addAction("Redo  [Ctrl+Shift+Z]", self._redo)
        menu.exec_(self.hamburger_btn.mapToGlobal(self.hamburger_btn.rect().topRight()))

    def _redownload_video(self):
        if not self.source_url:
            self.status.setText("No source URL known for this video")
            return
        self._pending_load = {
            "segments": list(self.segments),
            "selected": self.selected_segment,
            "position": self.position,
            "url": self.source_url,
        }
        self.url_input.setText(self.source_url)
        self._download_url()

    def _open_file_folder(self):
        if not self.filepath:
            self.status.setText("No video loaded")
            return
        folder = os.path.dirname(self.filepath)
        subprocess.Popen([FILE_MANAGER, folder])

    def _open_export_folder(self):
        folder = str(EXPORT_DIR)
        if not os.path.isdir(folder):
            self.status.setText(f"Export folder not found: {folder}")
            return
        subprocess.Popen([FILE_MANAGER, folder])

    def _open_file(self):
        open_dir = str(DOWNLOAD_DIR) if DOWNLOAD_DIR.exists() else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Video", open_dir,
            "Video Files (*.mp4 *.mkv *.avi *.mov *.webm *.flv *.ts *.m4v);;All Files (*)"
        )
        if path:
            self._load_video(path)

    def _load_video(self, filepath):
        self._pause()
        self.filepath = filepath
        self.video_info = probe_video(filepath)
        if not self.video_info:
            styled_msg(self, QMessageBox.Critical, "Error", f"Could not read video: {filepath}")
            return
        self.duration = get_duration(filepath)
        if self.duration <= 0:
            styled_msg(self, QMessageBox.Critical, "Error", "Could not determine video duration")
            return

        fmt = self.video_info.get('format', {})
        size = int(fmt.get('size', 0))
        fname = Path(filepath).name
        res = codec = ""
        for s in self.video_info.get('streams', []):
            if s.get('codec_type') == 'video':
                res = f"{s.get('width', '?')}x{s.get('height', '?')}"
                codec = s.get('codec_name', '')
                break

        self.file_label.setText(f"<b>{fname}</b>")
        self.info_label.setText(f"{fmt_size(size)}   |   {fmt_time(self.duration)}   |   {res}   |   {codec}")
        self.duration_label.setText(f"/ {fmt_time(self.duration)}")

        self.segments = [Segment(0, self.duration, True, "Full Video")]
        self.position = 0
        self.selected_segment = 0
        self.play_speed = 1.0
        self._update_play_ui()

        self.timeline.set_duration(self.duration)
        self.timeline.set_position(0)
        self.timeline.set_segments(self.segments)
        self._extract_thumbnails()
        self._extract_waveform()
        self._update_segments_ui()
        self._undo_stack.clear()
        self._redo_stack.clear()

        # Load into mpv
        self._init_mpv()
        self.mpv_player.speed = 1.0
        self.mpv_player.play(str(filepath))
        self.mpv_player.pause = True
        self.frame_label.hide()

        self.pos_label.setText(fmt_time(0))
        self.status.setText(f"Loaded: {fname}")
        try:
            save_last_video(filepath)
            add_recent(filepath)
        except Exception:
            pass
        self._refresh_recents()

        # Auto-load subtitles: use existing SRT or generate
        srt_path = Path(filepath).with_suffix(".srt")
        self.subtitles = []
        self.sub_list.clear()
        if srt_path.exists():
            self._load_srt(str(srt_path))
            self.status.setText(f"Loaded: {fname} (subtitles found)")
        else:
            self._generate_subtitles()

    def _extract_thumbnails(self):
        if self.thumb_worker:
            self.thumb_worker.cancel()
            self.thumb_worker.wait()
        n = min(30, max(10, int(self.duration / 5)))
        times = [i * self.duration / n for i in range(n)]
        self.timeline.thumb_count = n
        self.timeline.thumbnails.clear()
        try:
            cache_dir = _cache_dir_for(self.filepath)
        except Exception:
            cache_dir = None
        self.thumb_worker = ThumbnailWorker(self.filepath, times, 240, cache_dir=cache_dir)
        self.thumb_worker.thumbnail_ready.connect(self.timeline.set_thumbnail)
        self.thumb_worker.start()

    def _extract_waveform(self):
        if self.waveform_worker:
            self.waveform_worker.cancel()
            self.waveform_worker.wait()
        self.timeline.waveform = []
        self.waveform_worker = WaveformWorker(self.filepath)
        self.waveform_worker.waveform_ready.connect(self.timeline.set_waveform)
        self.waveform_worker.start()

    def _update_frame_preview(self):
        if not self.filepath:
            return
        self.pos_label.setText(fmt_time(self.position))
        self.timeline.set_position(self.position)
        # Seek mpv (debounced for rapid stepping)
        self._seek_debounce.start()

    def _debounced_seek(self):
        """Seek mpv to current position after user stops seeking."""
        if not self.filepath or self.playing:
            return
        self._mpv_seek(self.position)

    # ── Editing ──────────────────────────────────────────────────────

    def _on_timeline_seek(self, time_sec):
        if not hasattr(self, '_was_playing_before_seek'):
            self._was_playing_before_seek = self.playing
        self._pause()
        self.position = time_sec
        self._update_frame_preview()
        for i, seg in enumerate(self.segments):
            if seg.start <= time_sec < seg.end:
                self.selected_segment = i
                self._highlight_segment(i)
                break

    def _on_timeline_seek_finished(self):
        was_playing = getattr(self, '_was_playing_before_seek', False)
        self._was_playing_before_seek = None
        if was_playing:
            self._play()
        else:
            self._mpv_seek(self.position)

    def _step(self, delta):
        if self.duration <= 0:
            return
        self._pause()
        self.position = max(0, min(self.duration, self.position + delta))
        self._update_frame_preview()

    def _jump_to_time(self):
        t = parse_time(self.time_input.text())
        if t is not None and 0 <= t <= self.duration:
            self._pause()
            self.position = t
            self._update_frame_preview()

    def _split_at_current(self):
        if not self.segments or self.duration <= 0:
            return
        t = self.position
        for i, seg in enumerate(self.segments):
            if seg.start < t < seg.end:
                self._save_undo()
                new_seg = Segment(t, seg.end, seg.keep, f"Segment {len(self.segments) + 1}")
                seg.end = t
                seg.label = seg.label or f"Segment {i + 1}"
                self.segments.insert(i + 1, new_seg)
                self.selected_segment = i + 1
                self._update_segments_ui()
                self.timeline.set_segments(self.segments)
                self._mpv_seek(self.position)
                self.status.setText(f"Split at {fmt_time(t)}")
                return
        self.status.setText("Cannot split here (at segment boundary)")

    def _split_one_second(self):
        """Split a 1-second clip at the current position and advance past it."""
        if not self.segments or self.duration <= 0:
            return
        t = self.position
        end = min(t + 1.0, self.duration)
        if end - t < 0.05:
            self.status.setText("Not enough room for 1s split")
            return
        self._save_undo()
        # First split at current position
        left_seg = None
        for i, seg in enumerate(self.segments):
            if seg.start < t < seg.end:
                new_seg = Segment(t, seg.end, seg.keep, f"Segment {len(self.segments) + 1}")
                seg.end = t
                left_seg = seg
                self.segments.insert(i + 1, new_seg)
                break
            elif abs(seg.start - t) < 0.01:
                # Playhead is at a boundary — the segment just before is the left one
                if i > 0:
                    left_seg = self.segments[i - 1]
                break
        # Mark the segment to the left as CUT
        if left_seg is not None:
            left_seg.keep = False
        # Second split at t+1s
        for i, seg in enumerate(self.segments):
            if seg.start <= t + 0.01 and seg.end > end + 0.01:
                after = Segment(end, seg.end, seg.keep, f"Segment {len(self.segments) + 1}")
                seg.end = end
                self.segments.insert(i + 1, after)
                break
        # Move playhead past the 1s clip
        self._pause()
        self.position = end
        self.selected_segment = next(
            (i for i, s in enumerate(self.segments) if abs(s.start - end) < 0.01), self.selected_segment
        )
        self._update_segments_ui()
        self.timeline.set_segments(self.segments)
        self._update_frame_preview()
        self.status.setText(f"1s split: {fmt_time(t)} → {fmt_time(end)}")

    def _remove_before(self):
        if not self.segments or self.duration <= 0:
            return
        self._save_undo()
        self._split_at_current()
        # Only mark the segment immediately before the playhead
        for seg in self.segments:
            if abs(seg.end - self.position) < 0.01:
                seg.keep = False
                break
        self._update_segments_ui()
        self.timeline.set_segments(self.segments)
        self._mpv_seek(self.position)
        self.status.setText(f"Removed segment before {fmt_time(self.position)}")

    def _remove_after(self):
        if not self.segments or self.duration <= 0:
            return
        self._save_undo()
        self._split_at_current()
        # Only mark the segment immediately after the playhead
        for seg in self.segments:
            if abs(seg.start - self.position) < 0.01:
                seg.keep = False
                break
        self._update_segments_ui()
        self.timeline.set_segments(self.segments)
        self._mpv_seek(self.position)
        self.status.setText(f"Removed segment after {fmt_time(self.position)}")

    def _set_segment_speed(self, speed):
        """Set speed multiplier on the segment under the playhead."""
        if not self.segments:
            return
        idx = None
        for i, seg in enumerate(self.segments):
            if seg.start <= self.position + 0.01 and self.position < seg.end + 0.01:
                idx = i
                break
        if idx is None:
            return
        self._save_undo()
        self.segments[idx].speed = speed
        self.selected_segment = idx
        self._update_segments_ui()
        self.timeline.set_segments(self.segments)
        self.status.setText(f"Segment {idx + 1}: speed {speed}x")

    def _toggle_selected(self):
        if 0 <= self.selected_segment < len(self.segments):
            self._save_undo()
            seg = self.segments[self.selected_segment]
            seg.keep = not seg.keep
            self._update_segments_ui()
            self.timeline.set_segments(self.segments)
            self.status.setText(f"Segment {self.selected_segment + 1}: {'keep' if seg.keep else 'delete'}")

    def _set_trim_start(self):
        if 0 <= self.selected_segment < len(self.segments):
            seg = self.segments[self.selected_segment]
            # Only allow setting in-point within the segment's current range
            if seg.start <= self.position < seg.end:
                self._save_undo()
                seg.start = self.position
                self._update_segments_ui()
                self.timeline.set_segments(self.segments)
                self.status.setText(f"In point: {fmt_time(self.position)}")

    def _set_trim_end(self):
        if 0 <= self.selected_segment < len(self.segments):
            seg = self.segments[self.selected_segment]
            # Only allow setting out-point within the segment's current range
            if seg.start < self.position <= seg.end:
                self._save_undo()
                seg.end = self.position
                self._update_segments_ui()
                self.timeline.set_segments(self.segments)
                self.status.setText(f"Out point: {fmt_time(self.position)}")

    def _save_undo(self):
        """Snapshot current segments before an edit."""
        snapshot = [(Segment(s.start, s.end, s.keep, s.label)) for s in self.segments]
        self._undo_stack.append((snapshot, self.selected_segment))
        self._redo_stack.clear()

    def _undo(self):
        if not self._undo_stack:
            self.status.setText("Nothing to undo")
            return
        # Save current state to redo
        current = [(Segment(s.start, s.end, s.keep, s.label)) for s in self.segments]
        self._redo_stack.append((current, self.selected_segment))
        # Restore
        snapshot, sel = self._undo_stack.pop()
        self.segments = snapshot
        self.selected_segment = min(sel, len(self.segments) - 1)
        self._update_segments_ui()
        self.timeline.set_segments(self.segments)
        self.status.setText(f"Undo ({len(self._undo_stack)} left)")

    def _redo(self):
        if not self._redo_stack:
            self.status.setText("Nothing to redo")
            return
        current = [(Segment(s.start, s.end, s.keep, s.label)) for s in self.segments]
        self._undo_stack.append((current, self.selected_segment))
        snapshot, sel = self._redo_stack.pop()
        self.segments = snapshot
        self.selected_segment = min(sel, len(self.segments) - 1)
        self._update_segments_ui()
        self.timeline.set_segments(self.segments)
        self.status.setText(f"Redo ({len(self._redo_stack)} left)")

    def _select_prev_segment(self):
        if self.segments and self.selected_segment > 0:
            self.selected_segment -= 1
            self.position = self.segments[self.selected_segment].start
            self._update_frame_preview()
            self._highlight_segment(self.selected_segment)

    def _select_next_segment(self):
        if self.segments and self.selected_segment < len(self.segments) - 1:
            self.selected_segment += 1
            self.position = self.segments[self.selected_segment].start
            self._update_frame_preview()
            self._highlight_segment(self.selected_segment)

    def _update_segments_ui(self):
        while self.seg_layout.count() > 1:
            item = self.seg_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for i, seg in enumerate(self.segments):
            w = SegmentWidget(i, seg)
            if i == self.selected_segment:
                w._update_style(selected=True)
            w.toggled.connect(self._on_segment_toggled)
            w.selected.connect(self._on_segment_selected)
            self.seg_layout.insertWidget(i, w)
        self._update_kept_duration()

    def _update_kept_duration(self):
        if len(self.segments) <= 1:
            self.kept_duration_label.setText("")
            return
        kept_secs = sum((s.end - s.start) / s.speed for s in self.segments if s.keep)
        if kept_secs <= 0:
            self.kept_duration_label.setText("")
            return
        total = int(kept_secs)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        parts = []
        if h:
            parts.append(f"{h}h")
        if m:
            parts.append(f"{m}m")
        parts.append(f"{s}s")
        self.kept_duration_label.setText(" ".join(parts))

    def _on_segment_toggled(self, index):
        if 0 <= index < len(self.segments):
            self.segments[index].keep = not self.segments[index].keep
            self._update_segments_ui()
            self.timeline.set_segments(self.segments)

    def _on_segment_selected(self, index):
        self.selected_segment = index
        self._highlight_segment(index)
        if 0 <= index < len(self.segments):
            self.position = self.segments[index].start
            self._update_frame_preview()

    def _highlight_segment(self, index):
        for i in range(self.seg_layout.count()):
            item = self.seg_layout.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), SegmentWidget):
                w = item.widget()
                w._update_style(selected=(w.index == index))
                if w.index == index:
                    self.seg_scroll.ensureWidgetVisible(w, 50, 50)

    # ── Quick Save/Load ────────────────────────────────────────────

    def _next_save_path(self):
        save_dir = SAVE_DIR
        save_dir.mkdir(parents=True, exist_ok=True)
        i = 0
        while (save_dir / f".save_{i}").exists():
            i += 1
        return save_dir / f".save_{i}"

    def _latest_save_path(self):
        save_dir = SAVE_DIR
        i = 0
        latest = None
        while (save_dir / f".save_{i}").exists():
            latest = save_dir / f".save_{i}"
            i += 1
        return latest

    def _quick_save(self):
        if not self.filepath or not self.segments:
            self.status.setText("Nothing to save")
            return
        save_path = self._next_save_path()
        data = {
            "filepath": self.filepath,
            "source_url": self.source_url,
            "position": self.position,
            "selected_segment": self.selected_segment,
            "segments": [
                {"start": s.start, "end": s.end, "keep": s.keep, "label": s.label, "speed": s.speed}
                for s in self.segments
            ],
        }
        save_path.write_text(json.dumps(data, indent=2))
        self.status.setText(f"Saved to {save_path}")

    def _quick_load(self):
        save_path = self._latest_save_path()
        if not save_path:
            self.status.setText("No save file found")
            return
        data = json.loads(save_path.read_text())
        saved_segments = [
            Segment(start=s["start"], end=s["end"], keep=s["keep"],
                    label=s.get("label", ""), speed=s.get("speed", 1.0))
            for s in data["segments"]
        ]
        saved_selected = data.get("selected_segment", 0)
        saved_position = data.get("position", 0)
        saved_url = data.get("source_url")

        filepath = data["filepath"]
        if not os.path.exists(filepath):
            if saved_url:
                reply = styled_msg(self, QMessageBox.Question, "File Missing",
                    f"Video file not found:\n{filepath}\n\nRedownload from:\n{saved_url[:80]}?",
                    QMessageBox.Yes | QMessageBox.No)
                if reply == QMessageBox.Yes:
                    self._pending_load = {
                        "segments": saved_segments,
                        "selected": saved_selected,
                        "position": saved_position,
                        "url": saved_url,
                    }
                    self.url_input.setText(saved_url)
                    self._download_url()
                    return
            else:
                styled_msg(self, QMessageBox.Warning, "File Missing",
                    f"Video file not found:\n{filepath}\n\nNo source URL saved — cannot redownload.")
                return

        if filepath != self.filepath:
            self._load_video(filepath)
        self.source_url = saved_url
        # Apply saved state after _load_video (which resets segments)
        self.segments = saved_segments
        self.selected_segment = saved_selected
        self.position = saved_position
        self._update_segments_ui()
        self.timeline.set_segments(self.segments)
        self._highlight_segment(self.selected_segment)
        self._update_frame_preview()
        self.status.setText(f"Loaded from {save_path}")

    # ── Export ───────────────────────────────────────────────────────

    def _export(self):
        if not self.filepath or not self.segments:
            return
        kept = [s for s in self.segments if s.keep]
        if not kept:
            styled_msg(self, QMessageBox.Warning, "Nothing to export", "All segments are marked for deletion.")
            return

        src = Path(self.filepath)
        default_name = f"{src.stem}_edited{src.suffix}"
        export_dir = Path(EXPORT_DIR)
        if not export_dir.exists():
            export_dir = src.parent
        default_path = str(export_dir / default_name)
        output_path, _ = QFileDialog.getSaveFileName(
            self, "Export Video", default_path,
            "MP4 (*.mp4);;MKV (*.mkv);;All Files (*)"
        )
        if not output_path:
            return
        if not Path(output_path).suffix:
            output_path += ".mp4"

        total_kept = sum((s.end - s.start) / s.speed for s in kept)
        total_removed = self.duration - total_kept

        # Check for subtitles and music
        srt_path = Path(self.filepath).with_suffix(".srt")
        burn_subs = False
        music_note = ""
        if self.music_file:
            music_note = f"\nMusic: {os.path.basename(self.music_file)} ({self.music_volume}% vol) — audio re-encode"

        if srt_path.exists() and self.subtitles and self.subs_enabled:
            msg = (
                f"Keeping {len(kept)} segment(s): {fmt_time(total_kept)}\n"
                f"Removing: {fmt_time(total_removed)}{music_note}\n\n"
                f"Subtitles found. Burn them in?\n"
                f"  Yes = re-encode with subtitles (slower)\n"
                f"  No = {'audio re-encode for music' if self.music_file else 'stream copy'} without subtitles"
            )
            reply = styled_msg(self, QMessageBox.Question, "Export", msg, QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if reply == QMessageBox.Cancel:
                return
            burn_subs = (reply == QMessageBox.Yes)
        else:
            mode = "Audio re-encode for music mix." if self.music_file else "Video stream-copy + audio re-encode — fast."
            msg = (
                f"Keeping {len(kept)} segment(s): {fmt_time(total_kept)}\n"
                f"Removing: {fmt_time(total_removed)}{music_note}\n\n"
                f"{mode}"
            )
            reply = styled_msg(self, QMessageBox.Question, "Export", msg, QMessageBox.Ok | QMessageBox.Cancel)
            if reply != QMessageBox.Ok:
                return

        self._set_export_btn_cancel_mode(True)
        self.status.setText("Exporting...")
        self._export_kept_count = len(kept)
        self._export_part = 0
        self.export_progress.setVisible(True)
        self.export_progress.setRange(0, self._export_kept_count + 1)
        self.export_progress.setValue(0)

        # Build source info for export panel
        fmt_info = self.video_info.get('format', {}) if self.video_info else {}
        raw_size = int(fmt_info.get('size', 0))
        res = codec = ""
        for s in (self.video_info or {}).get('streams', []):
            if s.get('codec_type') == 'video':
                res = f"{s.get('width', '?')}x{s.get('height', '?')}"
                codec = s.get('codec_name', '')
                break
        if burn_subs and self.music_file:
            export_mode = "Re-encode (subtitles + music)"
        elif burn_subs:
            export_mode = "Re-encode (subtitles)"
        elif self.music_file:
            export_mode = "Audio re-encode (music mix)"
        elif any(seg.speed != 1.0 for seg in kept):
            export_mode = "Re-encode (speed change)"
        else:
            export_mode = "Video stream-copy + audio re-encode"

        source_info = {
            'name': Path(self.filepath).name,
            'size': fmt_size(raw_size),
            'raw_size': raw_size,
            'resolution': res,
            'codec': codec,
            'duration': self.duration,
        }

        # Show export panel in right pane
        self._show_export_panel(source_info, kept, export_mode, output_path)

        self._export_worker = ExportWorker(
            self.filepath, self.segments, output_path,
            srt_path=str(srt_path) if burn_subs else None,
            music_path=self.music_file,
            music_volume=self.music_volume
        )
        self._export_worker.progress.connect(self._on_export_progress)
        self._export_worker.detail.connect(self._on_export_detail)
        self._export_worker.finished.connect(self._on_export_done)
        self._export_worker.error.connect(self._on_export_error)
        self._export_worker.start()

    def _toggle_subs(self):
        self.subs_enabled = self.toggle_subs_btn.isChecked()
        self._update_subs_toggle_style()
        # Toggle mpv subtitle visibility (list stays visible for navigation)
        if hasattr(self, 'mpv_player') and self.mpv_player:
            try:
                self.mpv_player.sub_visibility = self.subs_enabled
            except Exception:
                pass
        self.status.setText(f"Subtitles {'enabled' if self.subs_enabled else 'disabled'}")

    def _update_subs_toggle_style(self):
        if self.subs_enabled:
            self.toggle_subs_btn.setText("On")
            self.toggle_subs_btn.setStyleSheet(
                "QPushButton { background: #2E7D32; color: white; font-size: 11px; font-weight: bold; "
                "border-radius: 4px; } QPushButton:hover { background: #388E3C; }"
            )
        else:
            self.toggle_subs_btn.setText("Off")
            self.toggle_subs_btn.setStyleSheet(
                "QPushButton { background: #555; color: #999; font-size: 11px; font-weight: bold; "
                "border-radius: 4px; } QPushButton:hover { background: #666; }"
            )

    def _generate_subtitles(self):
        if not self.filepath:
            self.status.setText("No video loaded")
            return
        src = Path(self.filepath)
        output_path = src.with_suffix(".srt")
        self.status.setText("Starting transcription...")
        self._subtitle_worker = SubtitleWorker(self.filepath, str(output_path))
        self._subtitle_worker.progress.connect(lambda s: self.status.setText(s))
        self._subtitle_worker.finished.connect(self._on_subtitles_done)
        self._subtitle_worker.error.connect(self._on_subtitles_error)
        self._subtitle_worker.start()

    def _on_subtitles_done(self, path):
        self.status.setText(f"Subtitles saved: {path}")
        self._load_srt(path)

    def _on_subtitles_error(self, err):
        self.status.setText(f"Subtitle error: {err[:100]}")

    def _load_srt(self, srt_path):
        self.subtitles = []
        self.sub_list.clear()
        try:
            with open(srt_path, "r", encoding="utf-8") as f:
                content = f.read()
            blocks = content.strip().split("\n\n")
            for block in blocks:
                lines = block.strip().split("\n")
                if len(lines) >= 3:
                    time_line = lines[1]
                    text = " ".join(lines[2:])
                    match = re.match(r"(\d+:\d+:\d+[,\.]\d+)\s*-->\s*(\d+:\d+:\d+[,\.]\d+)", time_line)
                    if match:
                        start = self._parse_srt_time(match.group(1))
                        end = self._parse_srt_time(match.group(2))
                        self.subtitles.append((start, end, text))
                        item = QListWidgetItem(f"[{fmt_time(start)}] {text}")
                        item.setData(Qt.UserRole, start)
                        self.sub_list.addItem(item)
        except Exception:
            pass

    @staticmethod
    def _parse_srt_time(s):
        s = s.replace(",", ".")
        parts = s.split(":")
        return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])

    def _on_subtitle_clicked(self, item):
        t = item.data(Qt.UserRole)
        if t is not None:
            was_playing = self.playing
            self._pause()
            self.position = t
            self._mpv_seek(t)
            self._update_frame_preview()
            self.setFocus()
            if was_playing:
                self._play()

    def _highlight_current_subtitle(self):
        for i, (start, end, _) in enumerate(self.subtitles):
            if start <= self.position <= end:
                if self.sub_list.currentRow() != i:
                    self.sub_list.setCurrentRow(i)
                    self.sub_list.scrollToItem(self.sub_list.item(i))
                return


    def _set_export_btn_cancel_mode(self, cancel_mode):
        try:
            self.export_btn.clicked.disconnect()
        except Exception:
            pass
        if cancel_mode:
            self.export_btn.setText("Cancel Export")
            self.export_btn.setStyleSheet(
                "QPushButton { background: #b71c1c; color: #fff; border: 1px solid #e53935; "
                "border-radius: 4px; padding: 4px 8px; font-size: 15px; font-weight: bold; } "
                "QPushButton:hover { background: #d32f2f; }"
            )
            self.export_btn.clicked.connect(self._cancel_export)
        else:
            self.export_btn.setText("Export  [Ctrl+E]")
            self.export_btn.setStyleSheet(
                "QPushButton { background: #2E7D32; color: #fff; border: none; "
                "border-radius: 4px; padding: 4px 8px; font-size: 15px; font-weight: bold; } "
                "QPushButton:hover { background: #388E3C; }"
            )
            self.export_btn.clicked.connect(self._export)
            self.export_btn.setEnabled(True)

    def _cancel_export(self):
        if hasattr(self, '_export_worker') and self._export_worker:
            self._export_worker.cancel()
            self.status.setText("Cancelling export...")

    def _show_export_panel(self, source_info, kept_segments, export_mode, output_path):
        """Hide segments/subtitles and show the export dashboard."""
        self.seg_header.setVisible(False)
        self.seg_separator.setVisible(False)
        self.right_splitter.setVisible(False)

        self.export_panel = ExportPanel(source_info, kept_segments, export_mode, output_path)
        self.export_panel.dismiss_requested.connect(self._dismiss_export_panel)
        self.export_panel.open_folder_requested.connect(
            lambda p: subprocess.Popen([FILE_MANAGER, os.path.dirname(p)])
        )
        self.export_panel.open_video_requested.connect(
            lambda p: subprocess.Popen(["xdg-open", p])
        )
        self.export_panel.delete_original_requested.connect(self._delete_original)
        self.export_panel.get_cancel_button().clicked.connect(self._cancel_export)
        self.right_layout.insertWidget(0, self.export_panel, 1)

    def _dismiss_export_panel(self):
        """Remove export panel and restore segments/subtitles view."""
        if hasattr(self, 'export_panel') and self.export_panel:
            self.export_panel.setParent(None)
            self.export_panel.deleteLater()
            self.export_panel = None
        self.seg_header.setVisible(True)
        self.seg_separator.setVisible(True)
        self.right_splitter.setVisible(True)
        self._set_export_btn_cancel_mode(False)
        self.export_progress.setVisible(False)

    def _on_export_progress(self, msg):
        self.status.setText(msg)
        if hasattr(self, 'export_panel') and self.export_panel:
            self.export_panel.update_progress(msg)
        if msg.startswith("Part "):
            try:
                self._export_part += 1
                self.export_progress.setValue(self._export_part)
            except Exception:
                pass
        elif "Joining" in msg or "Mixing" in msg or "Burning" in msg:
            self.export_progress.setValue(self._export_kept_count)

    def _on_export_detail(self, data):
        """Forward structured ffmpeg progress to the export panel."""
        if hasattr(self, 'export_panel') and self.export_panel:
            self.export_panel.update_detail(data)

    def _on_export_done(self, path):
        self.export_progress.setVisible(False)
        self._set_export_btn_cancel_mode(False)
        size = os.path.getsize(path)
        self.status.setText(f"Exported: {path} ({fmt_size(size)})")
        if hasattr(self, 'export_panel') and self.export_panel:
            self.export_panel.show_complete(path, size)
        else:
            # Fallback if panel was somehow dismissed
            styled_msg(self, QMessageBox.Information, "Done", f"Exported to:\n{path}\n\nSize: {fmt_size(size)}")

    def _collapse_tabs(self):
        if self._tabs_expanded:
            self._tabs_expanded = False
            self.tabs.setFixedHeight(self._tabs_collapsed_height)
            idx = self.tabs.currentIndex()
            text = self.tabs.tabText(idx)
            if not text.endswith(" \u25BC"):
                self.tabs.setTabText(idx, text + " \u25BC")

    def _expand_tabs(self):
        if not self._tabs_expanded:
            self._tabs_expanded = True
            self.tabs.setFixedHeight(self._tabs_full_height)
            for i in range(self.tabs.count()):
                text = self.tabs.tabText(i)
                if text.endswith(" \u25BC"):
                    self.tabs.setTabText(i, text[:-2])
        self._tab_collapse_timer.start()

    def _on_tab_clicked(self, index):
        if self._tabs_expanded:
            self._tab_collapse_timer.start()
        else:
            self._expand_tabs()

    def _open_settings(self):
        dlg = SettingsDialog(self)
        if dlg.exec_() != QDialog.Accepted:
            return
        vals = dlg.get_values()
        global DOWNLOAD_DIR, CACHE_DIR, WAVEFORM_CACHE_DIR, SAVE_DIR, EXPORT_DIR, RECENTS_FILE, FILE_MANAGER
        DOWNLOAD_DIR = Path(vals["download_dir"])
        CACHE_DIR = Path(vals["cache_dir"])
        WAVEFORM_CACHE_DIR = Path(vals["waveform_dir"])
        SAVE_DIR = Path(vals["save_dir"])
        EXPORT_DIR = Path(vals["export_dir"])
        RECENTS_FILE = Path(vals["recents_file"])
        FILE_MANAGER = vals.get("file_manager", "nemo")
        _save_settings()
        self.status.setText("Settings saved")

    def _delete_original(self):
        if not self.filepath:
            return
        name = Path(self.filepath).name
        reply = styled_msg(self, QMessageBox.Warning, "Delete Original",
                           f"Permanently delete:\n{name}?",
                           QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        try:
            vid_path = Path(self.filepath)
            srt_path = vid_path.with_suffix(".srt")
            vid_path.unlink()
            if srt_path.exists():
                srt_path.unlink()
            self._clear_scene()
            self.status.setText(f"Deleted: {name}")
        except Exception as e:
            self.status.setText(f"Delete failed: {e}")

    def _clear_scene(self):
        """Reset UI to initial empty state."""
        self._pause()
        if hasattr(self, 'mpv_player') and self.mpv_player:
            self.mpv_player.terminate()
            self.mpv_player = None
        if self.thumb_worker:
            self.thumb_worker.cancel()
        if self.waveform_worker:
            self.waveform_worker.cancel()
        self.filepath = None
        self.duration = 0
        self.position = 0
        self.segments = []
        self.selected_segment = -1
        self.video_info = None
        self.subtitles = []
        self._undo_stack.clear()
        self._redo_stack.clear()
        self.file_label.setText("No file loaded — open or drag & drop a video")
        self.info_label.setText("")
        self.duration_label.setText("/ --")
        self.pos_label.setText("0:00.00")
        self.frame_label.setText("Open a video to begin")
        self.frame_label.show()
        self.sub_list.clear()
        self.timeline.set_duration(0)
        self.timeline.set_position(0)
        self.timeline.set_segments([])
        self.timeline.thumbnails.clear()
        self.timeline.waveform = []
        self.timeline.update()
        self._update_segments_ui()

    def _on_export_error(self, err):
        self.export_progress.setVisible(False)
        self._set_export_btn_cancel_mode(False)
        self.status.setText(f"Export failed: {err[:100]}")
        if hasattr(self, 'export_panel') and self.export_panel:
            if "cancelled" in err.lower():
                self._dismiss_export_panel()
            else:
                self.export_panel.show_error(err)
        elif "cancelled" not in err.lower():
            styled_msg(self, QMessageBox.Critical, "Export Error", err)

    # ── Events ───────────────────────────────────────────────────────

    def closeEvent(self, event):
        self._pause()
        if hasattr(self, 'music_mpv') and self.music_mpv:
            try:
                self.music_mpv.terminate()
            except Exception:
                pass
            self.music_mpv = None
        if hasattr(self, 'mpv_player') and self.mpv_player:
            self.mpv_player.terminate()
            self.mpv_player = None
        if self.thumb_worker:
            self.thumb_worker.cancel()
            self.thumb_worker.wait()
        if self.waveform_worker:
            self.waveform_worker.cancel()
            self.waveform_worker.wait()
        if hasattr(self, 'compress_panel'):
            self.compress_panel.cleanup()
        cleanup_temp_downloads()
        event.accept()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if path:
                self._load_video(path)

    def _in_text_field(self):
        return isinstance(self.focusWidget(), QLineEdit)

    def eventFilter(self, obj, event):
        """Intercept keys globally and clear text focus on outside clicks."""
        # Click-to-seek on video widget (left half = -15s, right half = +15s)
        if event.type() == QEvent.MouseButtonPress and obj is self.video_widget:
            if self.filepath and self.duration > 0:
                half = self.video_widget.width() / 2
                if event.pos().x() < half:
                    self._step(-15)
                else:
                    self._step(15)
                return True
        if event.type() == QEvent.MouseButtonPress:
            if not isinstance(obj, QLineEdit):
                focused = self.focusWidget()
                if isinstance(focused, QLineEdit):
                    focused.clearFocus()
                    self.setFocus()
        if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Space:
            if not self._in_text_field():
                self._toggle_play()
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        key = event.key()
        ctrl = event.modifiers() & Qt.ControlModifier
        shift = event.modifiers() & Qt.ShiftModifier

        # Modifier shortcuts always work (even in text fields)
        if ctrl:
            if key == Qt.Key_Z and shift:
                self._redo()
                return
            elif key == Qt.Key_Z:
                self._undo()
                return
            elif key == Qt.Key_E:
                self._export()
                return
            elif key == Qt.Key_O:
                self._open_file()
                return
            elif key == Qt.Key_S:
                self._quick_save()
                return
            elif key == Qt.Key_L:
                self._quick_load()
                return
            elif key == Qt.Key_T:
                self._generate_subtitles()
                return
            elif key == Qt.Key_Left:
                self._step(-10)
                return
            elif key == Qt.Key_Right:
                self._step(10)
                return

        # Don't capture single keys when typing in a text field
        if self._in_text_field():
            super().keyPressEvent(event)
            return

        if key == Qt.Key_Space:
            self._toggle_play()
        elif key == Qt.Key_Left:
            self._step(-0.1 if shift else -1)
        elif key == Qt.Key_Right:
            self._step(0.1 if shift else 1)
        elif key == Qt.Key_S and shift:
            self._split_one_second()
        elif key == Qt.Key_S:
            self._split_at_current()
        elif key in (Qt.Key_X, Qt.Key_Delete):
            self._toggle_selected()
        elif key == Qt.Key_Q:
            self._remove_before()
        elif key == Qt.Key_W:
            self._remove_after()
        elif key == Qt.Key_I:
            self._set_trim_start()
        elif key == Qt.Key_O:
            self._set_trim_end()
        elif key == Qt.Key_J:
            self._speed_down()
        elif key == Qt.Key_K:
            self._speed_reset()
        elif key == Qt.Key_L:
            self._speed_up()
        elif key == Qt.Key_U:
            self._set_segment_speed(1.25)
        elif key == Qt.Key_Y:
            self._set_segment_speed(1.5)
        elif key == Qt.Key_Semicolon:
            self._set_segment_speed(1.75)
        elif key == Qt.Key_BracketLeft:
            self._set_segment_speed(2.0)
        elif key in (Qt.Key_Up,):
            self._select_prev_segment()
        elif key in (Qt.Key_BracketRight, Qt.Key_Down):
            self._select_next_segment()
        elif key == Qt.Key_QuoteLeft:
            self._toggle_privacy()
        elif key == Qt.Key_Home:
            self._pause()
            self.position = 0
            self._update_frame_preview()
        elif key == Qt.Key_End:
            self._pause()
            self.position = self.duration
            self._update_frame_preview()
        else:
            super().keyPressEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("QuickVideo")
    window = QuickVideoApp()
    app.installEventFilter(window)
    window.setAcceptDrops(True)

    # Determine which file to load
    filepath = None
    args = sys.argv[1:]
    if '--resume' in args:
        args.remove('--resume')
        filepath = load_last_video()
        if not filepath:
            print("No previous video to resume.")
    if not filepath and args and os.path.isfile(args[0]):
        filepath = args[0]
    if filepath:
        window._load_video(filepath)
        if '--resume' in sys.argv:
            window._quick_load()

    window.showMaximized()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
