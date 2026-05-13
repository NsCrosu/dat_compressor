"""Batch compression engine for maimai ``.dat`` videos."""

from __future__ import annotations

import concurrent.futures
import glob
import os
import queue
import re
import shutil
import sys
import threading
import time
import traceback
from typing import Callable, List, Optional, Tuple

from . import pipeline

_MIN_PROCESS_BYTES = 1048576
_NAME_PATTERN = re.compile(r"\d{6}\.dat")


def _format_mb(num_bytes: int) -> str:
    return f"{num_bytes / (1024 * 1024):.1f} MB"


def _classify_files(
    input_dir: str,
    output_dir: str,
    force: bool,
) -> Tuple[List[str], List[Tuple[str, str]], List[Tuple[str, str]]]:
    dat_paths = sorted(glob.glob(os.path.join(input_dir, "*.dat")))

    to_process: List[str] = []
    to_copy: List[Tuple[str, str]] = []
    to_skip: List[Tuple[str, str]] = []

    for path in dat_paths:
        filename = os.path.basename(path)
        out_path = os.path.join(output_dir, filename)

        if not force and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            to_skip.append((path, "exists"))
            continue

        if not _NAME_PATTERN.fullmatch(filename):
            to_copy.append((path, "filename"))
            continue

        try:
            size = os.path.getsize(path)
        except OSError:
            to_copy.append((path, "filename"))
            continue

        if size < _MIN_PROCESS_BYTES:
            to_copy.append((path, "size<1MB"))
        else:
            to_process.append(path)

    return to_process, to_copy, to_skip


class _MultiLineDisplay:
    """ANSI-based multi-line progress display. Each worker gets a slot."""

    def __init__(self, num_slots: int, total: int):
        self._num_slots = num_slots
        self._total_lines = num_slots + 1
        self._header = f"  Progress: 0/{total} completed"
        self._total = total
        self._completed = 0
        self._lines: List[str] = [""] * num_slots
        self._lock = threading.Lock()
        self._drawn = False

    def _draw(self) -> None:
        if self._drawn:
            sys.stdout.write(f"\033[{self._total_lines}A")
        sys.stdout.write(f"\033[K{self._header}\n")
        for line in self._lines:
            sys.stdout.write(f"\033[K{line}\n")
        sys.stdout.flush()
        self._drawn = True

    def mark_completed(self) -> None:
        with self._lock:
            self._completed += 1
            remaining = self._total - self._completed
            self._header = f"  Progress: {self._completed}/{self._total} completed, {remaining} remaining"
            self._draw()

    def update(self, slot: int, text: str) -> None:
        with self._lock:
            self._lines[slot] = text
            self._draw()

    def clear(self) -> None:
        with self._lock:
            if self._drawn:
                sys.stdout.write(f"\033[{self._total_lines}A")
                for _ in range(self._total_lines):
                    sys.stdout.write("\033[K\n")
                sys.stdout.write(f"\033[{self._total_lines}A")
                sys.stdout.flush()
            self._drawn = False
            self._lines = [""] * self._num_slots


def _process_one(
    path: str,
    out_path: str,
    quality: int,
    on_progress: Optional[Callable[[int], None]],
) -> pipeline.ProcessResult:
    return pipeline.process_single_file(
        path, out_path, quality,
        progress_callback=on_progress,
    )


def run_batch(
    input_dir: str,
    output_dir: str,
    quality: int,
    workers: int,
    force: bool = False,
) -> int:
    if not os.path.isdir(input_dir):
        print(f"Error: input directory does not exist: {input_dir}")
        return 1

    os.makedirs(output_dir, exist_ok=True)

    to_process, to_copy, to_skip = _classify_files(input_dir, output_dir, force)
    total_files = len(to_process) + len(to_copy) + len(to_skip)

    if total_files == 0:
        print(f"No .dat files found in {input_dir}")
        return 0

    print(f"Scanned {total_files} .dat files in {input_dir}")
    print(f"  To process: {len(to_process)}")
    print(f"  To copy:    {len(to_copy)}")
    print(f"  To skip:    {len(to_skip)} (output exists; use --force to overwrite)")
    print(f"Output directory: {output_dir}")
    print(f"Quality: {quality}  |  Workers: {workers}")
    print()

    batch_start = time.monotonic()

    copy_errors: List[Tuple[str, str]] = []
    for path, reason in to_copy:
        filename = os.path.basename(path)
        dest = os.path.join(output_dir, filename)
        try:
            shutil.copy2(path, dest)
            size = os.path.getsize(dest)
            print(f"[copy] {filename}: {_format_mb(size)} ({reason})")
        except OSError as exc:
            copy_errors.append((filename, str(exc)))
            print(f"[copy] {filename}: FAILED — {exc}")

    total_input_bytes = 0
    total_output_bytes = 0
    process_errors: List[Tuple[str, str]] = []
    completed = 0
    total_to_process = len(to_process)

    if to_process:
        display = _MultiLineDisplay(workers, total_to_process)
        slot_queue: queue.Queue[int] = queue.Queue()
        for i in range(workers):
            slot_queue.put(i)

        def _run_file(path: str) -> Tuple[str, Optional[pipeline.ProcessResult], Optional[str]]:
            filename = os.path.basename(path)
            out_path = os.path.join(output_dir, filename)
            slot = slot_queue.get()

            def on_progress(percent: int) -> None:
                filled = percent // 4
                bar = "█" * filled + "░" * (25 - filled)
                display.update(slot, f"  [{filename}] {bar} {percent}%")

            display.update(slot, f"  [{filename}] decrypting...")
            try:
                result = pipeline.process_single_file(
                    path, out_path, quality,
                    progress_callback=on_progress,
                )
                display.update(slot, f"  [{filename}] done")
                return (filename, result, None)
            except Exception as exc:
                tb = traceback.format_exc()
                display.update(slot, f"  [{filename}] FAILED")
                return (filename, None, f"{exc}\n{tb}")
            finally:
                slot_queue.put(slot)

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_run_file, path) for path in to_process]

            for future in concurrent.futures.as_completed(futures):
                filename, result, error = future.result()
                completed += 1
                display.mark_completed()

                if error:
                    process_errors.append((filename, error))
                else:
                    total_input_bytes += result.input_size
                    total_output_bytes += result.output_size

        display.clear()
        print(f"Processed {completed}/{total_to_process} files")
        for filename, error in process_errors:
            first_line = error.splitlines()[0] if error else ""
            print(f"  FAILED: {filename}: {first_line}")

    elapsed = time.monotonic() - batch_start
    overall_ratio = (
        (total_output_bytes / total_input_bytes * 100)
        if total_input_bytes > 0
        else 0.0
    )

    print()
    print("=" * 60)
    print("Batch complete")
    print("=" * 60)
    print(f"Total files scanned:  {total_files}")
    print(f"  Processed:          {total_to_process - len(process_errors)}")
    print(f"  Copied:             {len(to_copy) - len(copy_errors)}")
    print(f"  Skipped (existing): {len(to_skip)}")
    print(f"  Failed:             {len(process_errors) + len(copy_errors)}")
    if total_input_bytes > 0:
        print(
            f"Processed bytes:     {_format_mb(total_input_bytes)} → "
            f"{_format_mb(total_output_bytes)} ({overall_ratio:.1f}%)"
        )
    print(f"Elapsed time:        {elapsed:.1f}s")

    if process_errors or copy_errors:
        print()
        print("Errors:")
        for filename, msg in copy_errors:
            print(f"  [copy] {filename}: {msg}")
        for filename, msg in process_errors:
            first_line = msg.splitlines()[0] if msg else "(no message)"
            print(f"  [process] {filename}: {first_line}")
        return 1

    return 0
