import argparse
import os
import sys
from pathlib import Path


def _validate_quality(value):
    try:
        q = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"quality must be an integer, got '{value}'")
    if q < 1 or q > 100:
        raise argparse.ArgumentTypeError("quality must be between 1 and 100")
    return q


def _default_workers():
    return max(1, os.cpu_count() // 3)


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="dat_compressor",
        description="Batch-compress maimai .dat video files (CRI USM format)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    batch = sub.add_parser("batch", help="Batch compress all .dat files in a directory")
    batch.add_argument("-i", "--input", required=True, help="Input directory containing .dat files")
    batch.add_argument("-o", "--output", required=True, help="Output directory for compressed files")
    batch.add_argument("-q", "--quality", type=_validate_quality, required=True, help="Video quality 1-100")
    batch.add_argument("-w", "--workers", type=int, default=None, help=f"Parallel workers (default: {_default_workers()})")
    batch.add_argument("--force", action="store_true", help="Overwrite existing output files")

    preview = sub.add_parser("preview", help="Preview compress a single .dat file")
    preview.add_argument("-i", "--input", required=True, help="Input .dat file path")
    preview.add_argument("-o", "--output", default=None, help="Output .mp4 path (default: same dir, .mp4 extension)")
    preview.add_argument("-q", "--quality", type=_validate_quality, required=True, help="Video quality 1-100")

    return parser


def main():
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "batch":
        from .batch import run_batch

        workers = args.workers if args.workers is not None else _default_workers()
        if workers < 1:
            parser.error("workers must be >= 1")
        exit_code = run_batch(
            input_dir=args.input,
            output_dir=args.output,
            quality=args.quality,
            workers=workers,
            force=args.force,
        )
        sys.exit(exit_code)
    elif args.command == "preview":
        from .pipeline import preview

        inp = Path(args.input)
        output = args.output if args.output else str(inp.with_suffix(".mp4"))
        preview(args.input, output, args.quality)
