"""Single-file compression pipeline and MP4 preview for maimai ``.dat`` videos.

Both entry points share the same extract-then-re-encode prefix:

* :func:`process_single_file` repacks the re-encoded VP9 IVF back into a CRI
  USM ``.dat`` container.
* :func:`preview` remuxes the re-encoded VP9 IVF into an MP4 for quick visual
  inspection. No USM repacking is performed.

Both helpers own their temporary working directories and remove them on every
exit path (success, exception, KeyboardInterrupt). Partial output files are
also removed on failure so the user never has to clean up a half-written
``.dat``/``.mp4``.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
import time
from typing import Optional

from . import ffmpeg_ops, usm_creator, usm_extractor


@dataclasses.dataclass
class ProcessResult:
    """Outcome of a single-file compression run.

    Attributes:
        input_path: Source ``.dat`` path that was processed.
        output_path: Destination ``.dat`` path that was written.
        input_size: Size of the input ``.dat`` in bytes.
        output_size: Size of the output ``.dat`` in bytes.
        compression_ratio: ``output_size / input_size``. ``0.0`` when the input
            is empty. Values <1 mean the output is smaller than the input.
        elapsed_seconds: Wall-clock duration of the run.
        skipped: ``True`` when the file was deliberately not processed
            (reserved for the batch engine; always ``False`` here).
        skip_reason: Human-readable reason populated alongside ``skipped``.
    """

    input_path: str
    output_path: str
    input_size: int
    output_size: int
    compression_ratio: float
    elapsed_seconds: float
    skipped: bool = False
    skip_reason: str = ""


def _make_work_dir(temp_dir: Optional[str]) -> str:
    if temp_dir is not None:
        os.makedirs(temp_dir, exist_ok=True)
        return tempfile.mkdtemp(dir=temp_dir, prefix="dat_compressor_")
    return tempfile.mkdtemp(prefix="dat_compressor_")


def _compressed_ivf_path(work_dir: str, ivf_path: str) -> str:
    stem = os.path.splitext(os.path.basename(ivf_path))[0]
    return os.path.join(work_dir, f"{stem}.compressed.ivf")


def _remove_if_exists(path: str) -> None:
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def process_single_file(
    input_dat: str,
    output_dat: str,
    quality: int,
    temp_dir: Optional[str] = None,
    progress_callback: Optional[callable] = None,
) -> ProcessResult:
    """Extract → re-encode → repack a single ``.dat`` file.

    Args:
        input_dat: Path to the source CRI USM ``.dat``.
        output_dat: Path where the recompressed ``.dat`` is written. The
            parent directory is created if it does not exist.
        quality: 1-100. ``100`` skips re-encoding (the extracted IVF is copied
            byte-for-byte) but the file is still repacked into a fresh USM.
        temp_dir: Optional parent directory for intermediate IVFs. A unique
            sub-directory is created inside it and removed on every exit
            path. When ``None``, a system temp directory is used.
        progress_callback: If provided, called with an integer percent (0-100)
            during VP9 encoding.

    Returns:
        A :class:`ProcessResult` populated with byte counts, ratio, and
        elapsed wall time. ``skipped`` is always ``False`` here; the batch
        engine sets it when applying its own skip rules.

    Raises:
        Exception: Any error raised by extraction, re-encoding, or USM
            repacking is re-raised verbatim. Before re-raising, the temp
            directory and any partially-written ``output_dat`` are removed.
    """
    start = time.monotonic()
    input_size = os.path.getsize(input_dat)
    _ensure_parent_dir(output_dat)

    work_dir = _make_work_dir(temp_dir)
    try:
        ivf_path = usm_extractor.extract_ivf(input_dat, work_dir)
        compressed_ivf = _compressed_ivf_path(work_dir, ivf_path)
        ffmpeg_ops.reencode_vp9(ivf_path, compressed_ivf, quality, on_progress=progress_callback)
        usm_creator.create_usm(compressed_ivf, output_dat)
    except BaseException:
        _remove_if_exists(output_dat)
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    output_size = os.path.getsize(output_dat)
    elapsed = time.monotonic() - start
    ratio = (output_size / input_size) if input_size > 0 else 0.0
    return ProcessResult(
        input_path=input_dat,
        output_path=output_dat,
        input_size=input_size,
        output_size=output_size,
        compression_ratio=ratio,
        elapsed_seconds=elapsed,
    )


def preview(input_dat: str, output_mp4: str, quality: int) -> None:
    """Compress a ``.dat`` to a playable MP4 for visual quality inspection.

    Unlike :func:`process_single_file`, this helper does **not** repack the
    re-encoded VP9 stream into a USM. The output is a normal MP4 (VP9 in MP4
    container) suitable for ``ffplay``/QuickTime preview.

    Prints a one-line size summary to stdout on success.

    Args:
        input_dat: Path to the source CRI USM ``.dat``.
        output_mp4: Path where the preview ``.mp4`` is written. The parent
            directory is created if it does not exist.
        quality: 1-100. ``100`` skips re-encoding (the extracted IVF is
            remuxed directly to MP4).

    Raises:
        Exception: Any error from extraction, re-encoding, or remuxing is
            re-raised verbatim. The temp directory and any partial output
            ``.mp4`` are removed first.
    """
    input_size = os.path.getsize(input_dat)
    _ensure_parent_dir(output_mp4)

    def _progress(percent: int) -> None:
        filled = percent // 4
        bar = "█" * filled + "░" * (25 - filled)
        print(f"\r{bar} {percent}%", end="", flush=True)

    work_dir = _make_work_dir(None)
    try:
        ivf_path = usm_extractor.extract_ivf(input_dat, work_dir)
        compressed_ivf = _compressed_ivf_path(work_dir, ivf_path)
        ffmpeg_ops.reencode_vp9(ivf_path, compressed_ivf, quality, on_progress=_progress)
        print()
        ffmpeg_ops.ivf_to_mp4(compressed_ivf, output_mp4)
    except BaseException:
        _remove_if_exists(output_mp4)
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    output_size = os.path.getsize(output_mp4)
    ratio = (output_size / input_size) if input_size > 0 else 0.0
    mb = 1024 * 1024
    print(
        f"Original: {input_size / mb:.2f} MB, "
        f"Compressed: {output_size / mb:.2f} MB "
        f"(ratio: {ratio * 100:.1f}%)"
    )
