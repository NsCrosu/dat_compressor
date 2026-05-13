"""ffmpeg/ffprobe subprocess wrappers and quality-to-CRF mapping for VP9 re-encoding.

This module is intentionally dependency-free: it shells out to ``ffmpeg`` and
``ffprobe`` via :mod:`subprocess`. macOS Apple Silicon has no VP9 hardware
encoder, so all VP9 work runs through the libvpx-vp9 software encoder using
pure CRF mode (no bitrate cap, no resolution scaling).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Callable, Optional


class FfmpegError(RuntimeError):
    """Raised when an ffmpeg/ffprobe invocation fails."""


def _which(binary: str) -> Optional[str]:
    return shutil.which(binary)


def check_dependencies() -> None:
    """Verify that ``ffmpeg`` and ``ffprobe`` are installed on PATH.

    Raises:
        FfmpegError: When either binary is missing. The error message hints at
            ``brew install ffmpeg`` for macOS users.
    """
    missing = [b for b in ("ffmpeg", "ffprobe") if _which(b) is None]
    if missing:
        joined = ", ".join(missing)
        raise FfmpegError(
            f"Missing required binaries: {joined}. "
            "Install with: brew install ffmpeg"
        )


def _run(cmd: list[str], *, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a command and raise :class:`FfmpegError` with stderr on failure."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
        )
    except FileNotFoundError as exc:
        raise FfmpegError(f"Executable not found: {cmd[0]}") from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise FfmpegError(
            f"{cmd[0]} exited with code {result.returncode}: {stderr or '<no stderr>'}"
        )
    return result


def _parse_frame_rate(rate: str) -> float:
    """Parse ffprobe ``r_frame_rate`` (``"30000/1001"`` style) to float."""
    if not rate:
        return 0.0
    if "/" in rate:
        num, _, den = rate.partition("/")
        try:
            n = float(num)
            d = float(den)
        except ValueError:
            return 0.0
        if d == 0:
            return 0.0
        return n / d
    try:
        return float(rate)
    except ValueError:
        return 0.0


def probe_video(path: str) -> dict:
    """Probe a video file for stream + format info.

    Args:
        path: Path to the video file.

    Returns:
        Dict with keys ``width``, ``height``, ``codec_name``, ``frame_rate``
        (float, frames per second), ``duration`` (float, seconds) and
        ``bitrate`` (int, bits per second). Missing values default to ``0`` /
        empty string.

    Raises:
        FfmpegError: When ffprobe fails or no video stream is found.
    """
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    proc = _run(cmd)
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise FfmpegError(f"ffprobe returned invalid JSON for {path}: {exc}") from exc

    streams = data.get("streams") or []
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video_stream is None:
        raise FfmpegError(f"No video stream found in {path}")

    fmt = data.get("format") or {}

    duration_raw = video_stream.get("duration") or fmt.get("duration") or "0"
    try:
        duration = float(duration_raw)
    except (TypeError, ValueError):
        duration = 0.0

    bitrate_raw = video_stream.get("bit_rate") or fmt.get("bit_rate") or "0"
    try:
        bitrate = int(bitrate_raw)
    except (TypeError, ValueError):
        bitrate = 0

    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)
    codec_name = video_stream.get("codec_name") or ""
    frame_rate = _parse_frame_rate(video_stream.get("r_frame_rate") or "")

    return {
        "width": width,
        "height": height,
        "codec_name": codec_name,
        "frame_rate": frame_rate,
        "duration": duration,
        "bitrate": bitrate,
    }


def probe_packets(path: str) -> list[dict]:
    """Probe per-packet positions and flags (for USM re-packaging).

    Args:
        path: Path to the video file.

    Returns:
        List of dicts, one per packet, with keys ``pos`` (int, byte offset in
        the file; ``-1`` if unknown) and ``is_keyframe`` (bool, derived from
        the ``K`` flag in the ffprobe ``flags`` field).

    Raises:
        FfmpegError: When ffprobe fails or returns invalid JSON.
    """
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_entries", "packet=pos,flags",
        path,
    ]
    proc = _run(cmd)
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise FfmpegError(f"ffprobe returned invalid JSON for {path}: {exc}") from exc

    packets = data.get("packets") or []
    out: list[dict] = []
    for pkt in packets:
        pos_raw = pkt.get("pos")
        try:
            pos = int(pos_raw) if pos_raw is not None else -1
        except (TypeError, ValueError):
            pos = -1
        flags = pkt.get("flags") or ""
        # ffprobe encodes flags as a short string like "K_" or "__"; the 'K'
        # bit indicates a keyframe.
        is_keyframe = "K" in flags
        out.append({"pos": pos, "is_keyframe": is_keyframe})
    return out


def _quality_to_crf(quality: int) -> Optional[int]:
    """Map a 1-100 quality value to libvpx-vp9 ``-crf`` (or ``None`` to copy).

    The formula ``crf = round(63 * (100 - quality) / 99)`` produces:

    * ``quality=100`` -> ``None`` (no re-encode, copy input)
    * ``quality=99``  -> ``1``    (near-lossless)
    * ``quality=50``  -> ``32``   (mid)
    * ``quality=1``   -> ``63``   (maximum compression)
    """
    if not isinstance(quality, int) or quality < 1 or quality > 100:
        raise ValueError(f"quality must be an integer in 1..100, got {quality!r}")
    if quality == 100:
        return None
    return round(63 * (100 - quality) / 99)


def _quality_to_speed(quality: int) -> int:
    """Pick the libvpx-vp9 ``-speed`` value for a given quality.

    Higher quality uses a slower (more thorough) encoder; lower quality picks a
    faster speed to keep batch wall-clock time tractable.
    """
    return 2 if quality >= 70 else 4


# ffmpeg progress lines look like:
#   frame=  123 fps=42 q=-0.0 size=    1024kB time=00:00:04.50 bitrate=...
# We pull the ``time=HH:MM:SS.ss`` field for percent computation.
_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def _parse_time_seconds(line: str) -> Optional[float]:
    m = _TIME_RE.search(line)
    if not m:
        return None
    h, mnt, sec = m.groups()
    try:
        return int(h) * 3600 + int(mnt) * 60 + float(sec)
    except ValueError:
        return None


def reencode_vp9(
    input_path: str,
    output_path: str,
    quality: int,
    on_progress: Optional[Callable[[int], None]] = None,
    threads: int = 3,
) -> None:
    """Re-encode an IVF (or any ffmpeg-readable input) to VP9 IVF.

    Uses pure CRF mode (no bitrate cap), ``yuv420p`` pixel format and a single
    output stream; no resolution scaling is applied.

    Args:
        input_path: Source video path.
        output_path: Destination IVF path.
        quality: 1-100. ``100`` skips ffmpeg entirely and copies the input
            byte-for-byte. ``<70`` selects ``-speed 4``, ``>=70`` selects
            ``-speed 2``.
        on_progress: Optional callback receiving an integer percent (0-100).
            Only invoked when ffmpeg is actually run.
        threads: ``-threads`` value passed to ffmpeg. Defaults to ``3`` to play
            well with the multi-process batch driver.

    Raises:
        FfmpegError: When ffmpeg returns a non-zero exit code.
    """
    crf = _quality_to_crf(quality)
    if crf is None:
        shutil.copy(input_path, output_path)
        if on_progress is not None:
            on_progress(100)
        return

    speed = _quality_to_speed(quality)

    duration = 0.0
    try:
        info = probe_video(input_path)
        duration = float(info.get("duration") or 0.0)
    except FfmpegError:
        # Probe failure must not abort the encode; we just lose progress%.
        duration = 0.0

    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_path,
        "-c:v", "libvpx-vp9",
        "-crf", str(crf),
        "-b:v", "0",
        "-pix_fmt", "yuv420p",
        "-threads", str(threads),
        "-tile-columns", "2",
        "-frame-parallel", "1",
        "-speed", str(speed),
        "-f", "ivf",
        output_path,
    ]

    if on_progress is None or duration <= 0:
        _run(cmd)
        if on_progress is not None:
            on_progress(100)
        return

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    stderr_tail: list[str] = []
    last_percent = -1
    assert proc.stderr is not None
    try:
        for line in proc.stderr:
            stderr_tail.append(line)
            if len(stderr_tail) > 50:
                del stderr_tail[: len(stderr_tail) - 50]
            t = _parse_time_seconds(line)
            if t is None:
                continue
            percent = int(min(99, max(0, (t / duration) * 100)))
            if percent != last_percent:
                last_percent = percent
                try:
                    on_progress(percent)
                except Exception:
                    # Caller bug in progress callback must not abort encode.
                    pass
    finally:
        rc = proc.wait()
    if rc != 0:
        tail = "".join(stderr_tail).strip()
        raise FfmpegError(
            f"ffmpeg exited with code {rc}: {tail or '<no stderr>'}"
        )
    try:
        on_progress(100)
    except Exception:
        pass


def ivf_to_mp4(input_path: str, output_path: str) -> None:
    """Remux an IVF file into an MP4 container without re-encoding.

    Uses ``-c copy`` so the underlying VP9 bitstream is preserved exactly.

    Args:
        input_path: Source IVF path.
        output_path: Destination MP4 path.

    Raises:
        FfmpegError: When ffmpeg fails.
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_path,
        "-c", "copy",
        "-f", "mp4",
        output_path,
    ]
    _run(cmd)
