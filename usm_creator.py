"""VP9 IVF to CRI USM/DAT packer."""

from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import BinaryIO, Iterable

from . import ffmpeg_ops, utf_table
from .cipher import DEFAULT_KEY, encrypt_video_packet, generate_keys
from .utf_table import ElementType


USM_FORMAT_VERSION = 16777984
VP9_FORMAT_VERSION = 16777984
VP9_MPEG_CODEC = 9
VP9_MPEG_DCPREC = 0

_CRID_SIGNATURE = b"CRID"
_VIDEO_SIGNATURE = b"@SFV"

_CONTENTS_END_PAYLOAD = b"#CONTENTS END   ===============\0"
_HEADER_END_PAYLOAD = b"#HEADER END     ===============\0"
_METADATA_END_PAYLOAD = b"#METADATA END   ===============\0"

_PAYLOAD_TYPE_STREAM = 0
_PAYLOAD_TYPE_HEADER = 1
_PAYLOAD_TYPE_SECTION_END = 2
_PAYLOAD_TYPE_METADATA = 3


def create_usm(ivf_path: str, output_path: str) -> None:
    """Create a CRI USM ``.dat`` file from a VP9 IVF elementary stream.

    The implementation intentionally supports only VP9-in-IVF inputs, matching
    the compression pipeline this package produces.
    """

    source = Path(ivf_path)
    if not source.is_file():
        raise FileNotFoundError(f"Input IVF file not found: {ivf_path}")

    video = ffmpeg_ops.probe_video(str(source))
    _validate_vp9_ivf(source, video)

    packets = ffmpeg_ops.probe_packets(str(source))
    frame_offsets = _packet_offsets(packets)
    frame_sizes = _build_frame_sizes(frame_offsets, source.stat().st_size)
    if not frame_sizes:
        raise ValueError("No frames were found in input IVF")

    keyframe_indices = {index for index, pkt in enumerate(packets) if pkt.get("is_keyframe")}
    bitrate = int(video.get("bitrate") or 0)
    frame_rate = float(video.get("frame_rate") or 0.0)

    max_frame_size = max(frame_sizes)
    max_packed_size = 0x18 + max_frame_size + _alignment_padding(max_frame_size, 0x20)

    video_crid_payload = _create_video_crid_payload(
        filename=source.name,
        filesize=source.stat().st_size,
        max_size=max_frame_size,
        bitrate=bitrate,
    )
    video_header_payload = _create_video_header_payload(
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        num_frames=len(frame_sizes),
        num_keyframes=len(keyframe_indices),
        frame_rate=frame_rate,
        max_packed_size=max_packed_size,
    )

    video_key, _ = generate_keys(DEFAULT_KEY)
    stream_bytes, keyframe_offsets, max_packet_size = _pack_video_stream(
        source,
        frame_sizes,
        keyframe_indices,
        frame_rate,
        video_key,
    )
    prestream_chunks = _build_prestream_chunks(
        source,
        video_crid_payload,
        video_header_payload,
        keyframe_offsets,
        max_packet_size,
        len(stream_bytes),
        bitrate,
    )

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as handle:
        for chunk in prestream_chunks:
            handle.write(chunk)
        handle.write(stream_bytes)


def _validate_vp9_ivf(source: Path, video: dict) -> None:
    if video.get("codec_name") != "vp9":
        raise ValueError(f"Only VP9 input is supported, got {video.get('codec_name')!r}")
    with source.open("rb") as handle:
        if handle.read(4) != b"DKIF":
            raise ValueError("VP9 input must be an IVF file with DKIF signature")


def _packet_offsets(packets: Iterable[dict]) -> list[int]:
    offsets = [int(pkt.get("pos", -1)) for pkt in packets]
    if not offsets:
        raise ValueError("ffprobe returned no packet offsets")
    if any(offset < 0 for offset in offsets):
        raise ValueError("ffprobe returned packet offsets without byte positions")
    return offsets


def _build_frame_sizes(offsets: list[int], file_size: int) -> list[int]:
    sizes: list[int] = []
    for index, frame_offset in enumerate(offsets):
        if index == len(offsets) - 1:
            frame_size = file_size - frame_offset
        elif index == 0:
            frame_size = offsets[index + 1]
        else:
            frame_size = offsets[index + 1] - frame_offset
        if frame_size <= 0:
            raise ValueError(f"Invalid frame size at index {index}: {frame_size}")
        sizes.append(frame_size)
    return sizes


def _create_video_crid_payload(
    *,
    filename: str,
    filesize: int,
    max_size: int,
    bitrate: int,
) -> bytes:
    elements = [
        ("fmtver", ElementType.INT, None),
        ("filename", ElementType.STRING, None),
        ("filesize", ElementType.INT, None),
        ("datasize", ElementType.INT, None),
        ("stmid", ElementType.INT, None),
        ("chno", ElementType.INT, None),
        ("minchk", ElementType.INT, None),
        ("minbuf", ElementType.INT, None),
        ("avbps", ElementType.INT, None),
    ]
    page = {
        "fmtver": VP9_FORMAT_VERSION,
        "filename": filename,
        "filesize": int(filesize),
        "datasize": 0,
        "stmid": 1079199318,
        "chno": 0,
        "minchk": 3,
        "minbuf": int(max_size),
        "avbps": int(bitrate),
    }
    return utf_table.pack_pages(elements, [page])


def _create_video_header_payload(
    *,
    width: int,
    height: int,
    num_frames: int,
    num_keyframes: int,
    frame_rate: float,
    max_packed_size: int,
) -> bytes:
    elements = [(name, ElementType.INT, None) for name in (
        "width",
        "height",
        "mat_width",
        "mat_height",
        "disp_width",
        "disp_height",
        "scrn_width",
        "mpeg_dcprec",
        "mpeg_codec",
        "alpha_type",
        "total_frames",
        "framerate_n",
        "framerate_d",
        "metadata_count",
        "metadata_size",
        "ixsize",
        "pre_padding",
        "max_picture_size",
        "color_space",
        "picture_type",
    )]
    page = {
        "width": width,
        "height": height,
        "mat_width": width,
        "mat_height": height,
        "disp_width": width,
        "disp_height": height,
        "scrn_width": 0,
        "mpeg_dcprec": VP9_MPEG_DCPREC,
        "mpeg_codec": VP9_MPEG_CODEC,
        "alpha_type": 0,
        "total_frames": num_frames,
        "framerate_n": int(frame_rate * 1000),
        "framerate_d": 1000,
        "metadata_count": 1,
        "metadata_size": num_keyframes,
        "ixsize": max_packed_size,
        "pre_padding": 0,
        "max_picture_size": 0,
        "color_space": 0,
        "picture_type": 0,
    }
    return utf_table.pack_pages(elements, [page])


def _create_usm_crid_payload(
    *,
    filename: str,
    size_after_crid_part: int,
    max_packet_size: int,
    bitrate: int,
) -> bytes:
    elements = [
        ("fmtver", ElementType.INT, None),
        ("filename", ElementType.STRING, None),
        ("filesize", ElementType.INT, None),
        ("datasize", ElementType.INT, None),
        ("stmid", ElementType.INT, None),
        ("chno", ElementType.INT, None),
        ("minchk", ElementType.INT, None),
        ("minbuf", ElementType.INT, None),
        ("avbps", ElementType.INT, None),
    ]
    page = {
        "fmtver": USM_FORMAT_VERSION,
        "filename": filename,
        "filesize": int(size_after_crid_part),
        "datasize": 0,
        "stmid": 0,
        "chno": 0,
        "minchk": 1,
        "minbuf": int(max_packet_size),
        "avbps": int(bitrate),
    }
    return utf_table.pack_pages(elements, [page])


def _create_seekinfo_payload(keyframe_offsets: list[tuple[int, int]]) -> bytes:
    elements = [
        ("ofs_byte", ElementType.LONGLONG, None),
        ("ofs_frmid", ElementType.INT, None),
        ("num_skip", ElementType.INT, None),
        ("resv", ElementType.INT, None),
    ]
    pages = [
        {"ofs_byte": offset, "ofs_frmid": frame_index, "num_skip": 0, "resv": 0}
        for frame_index, offset in keyframe_offsets
    ]
    if not pages:
        pages = [{"ofs_byte": 0, "ofs_frmid": 0, "num_skip": 0, "resv": 0}]
    return utf_table.pack_pages(elements, pages)


def _pack_video_stream(
    source: Path,
    frame_sizes: list[int],
    keyframe_indices: set[int],
    frame_rate: float,
    video_key: bytes,
) -> tuple[bytes, list[tuple[int, int]], int]:
    keyframe_offsets: list[tuple[int, int]] = []
    max_packet_size = 1
    frame_rate_value = int(frame_rate * 100)
    output = bytearray()

    with source.open("rb") as handle:
        for index, frame_size in enumerate(frame_sizes):
            payload = bytearray(_read_exactly(handle, frame_size))
            encrypt_video_packet(payload, video_key)

            if index in keyframe_indices:
                keyframe_offsets.append((index, len(output)))

            padding = _alignment_padding(frame_size, 0x20)
            chunk = _pack_chunk(
                _VIDEO_SIGNATURE,
                _PAYLOAD_TYPE_STREAM,
                payload,
                frame_rate=frame_rate_value,
                frame_time=int(index * 99.9),
                padding=padding,
                channel_number=0,
            )
            output.extend(chunk)
            max_packet_size = max(max_packet_size, len(chunk))

        end_chunk = _pack_chunk(
            _VIDEO_SIGNATURE,
            _PAYLOAD_TYPE_SECTION_END,
            _CONTENTS_END_PAYLOAD,
            frame_rate=frame_rate_value,
            frame_time=0,
            padding=0,
            channel_number=0,
        )
        output.extend(end_chunk)
        max_packet_size = max(max_packet_size, len(end_chunk))

    return bytes(output), keyframe_offsets, max_packet_size


def _build_prestream_chunks(
    source: Path,
    video_crid_payload: bytes,
    video_header_payload: bytes,
    keyframe_offsets: list[tuple[int, int]],
    max_packet_size: int,
    stream_size: int,
    bitrate: int,
) -> list[bytes]:
    video_header_chunk = _pack_chunk(
        _VIDEO_SIGNATURE,
        _PAYLOAD_TYPE_HEADER,
        video_header_payload,
        frame_rate=30,
        frame_time=0,
        padding=0x18,
        channel_number=0,
    )
    header_end_chunk = _pack_chunk(
        _VIDEO_SIGNATURE,
        _PAYLOAD_TYPE_SECTION_END,
        _HEADER_END_PAYLOAD,
        frame_rate=30,
        frame_time=0,
        padding=0,
        channel_number=0,
    )
    current_position = len(video_header_chunk) + len(header_end_chunk)

    initial_seek_payload = _create_seekinfo_payload(keyframe_offsets)
    metadata_chunk = _pack_chunk(
        _VIDEO_SIGNATURE,
        _PAYLOAD_TYPE_METADATA,
        initial_seek_payload,
        frame_rate=30,
        frame_time=0,
        padding=_metadata_padding(0x20 + len(initial_seek_payload)),
        channel_number=0,
    )
    metadata_end_chunk = _pack_chunk(
        _VIDEO_SIGNATURE,
        _PAYLOAD_TYPE_SECTION_END,
        _METADATA_END_PAYLOAD,
        frame_rate=30,
        frame_time=0,
        padding=0,
        channel_number=0,
    )
    metadata_section_size = len(metadata_chunk) + len(metadata_end_chunk)

    adjusted_keyframes = [
        (frame_index, offset + 0x800 + current_position + metadata_section_size)
        for frame_index, offset in keyframe_offsets
    ]
    seek_payload = _create_seekinfo_payload(adjusted_keyframes)
    metadata_chunk = _pack_chunk(
        _VIDEO_SIGNATURE,
        _PAYLOAD_TYPE_METADATA,
        seek_payload,
        frame_rate=30,
        frame_time=0,
        padding=_metadata_padding(0x20 + len(seek_payload)),
        channel_number=0,
    )
    metadata_section_size = len(metadata_chunk) + len(metadata_end_chunk)

    header_metadata_size = current_position + metadata_section_size
    size_after_crid_part = 0x800 + header_metadata_size + stream_size
    usm_crid_payload = _create_usm_crid_payload(
        filename=source.with_suffix(".usm").name,
        size_after_crid_part=size_after_crid_part,
        max_packet_size=max_packet_size,
        bitrate=bitrate,
    )
    info_payload = usm_crid_payload + video_crid_payload
    info_chunk = _pack_chunk(
        _CRID_SIGNATURE,
        _PAYLOAD_TYPE_HEADER,
        info_payload,
        frame_rate=30,
        frame_time=0,
        padding=_pad_to_next_sector(0, 0x20 + len(info_payload)),
        channel_number=0,
    )

    return [info_chunk, video_header_chunk, header_end_chunk, metadata_chunk, metadata_end_chunk]


def _pack_chunk(
    signature: bytes,
    payload_type: int,
    payload: bytes | bytearray,
    *,
    frame_rate: int,
    frame_time: int,
    padding: int,
    channel_number: int,
) -> bytes:
    chunk_size = 0x18 + len(payload) + padding
    header = bytearray(0x20)
    header[0:4] = signature
    struct.pack_into(">I", header, 4, chunk_size)
    header[9] = 0x18
    struct.pack_into(">H", header, 10, padding)
    header[12] = channel_number
    header[15] = payload_type
    struct.pack_into(">i", header, 16, frame_time)
    struct.pack_into(">i", header, 20, frame_rate)
    return bytes(header) + bytes(payload) + (b"\0" * padding)


def _read_exactly(handle: BinaryIO, size: int) -> bytes:
    data = handle.read(size)
    if len(data) != size:
        raise EOFError("Unexpected EOF while reading video packet")
    return data


def _metadata_padding(chunk_size: int) -> int:
    if chunk_size <= 0xF0:
        return 0xF0 - chunk_size
    return _alignment_padding(chunk_size, 0x8)


def _pad_to_next_sector(position: int, chunk_size: int) -> int:
    unpadded = position + chunk_size
    return ((unpadded + 0x7FF) // 0x800) * 0x800 - unpadded


def _alignment_padding(size: int, alignment: int) -> int:
    remainder = size % alignment
    return 0 if remainder == 0 else alignment - remainder


__all__ = ["create_usm"]
