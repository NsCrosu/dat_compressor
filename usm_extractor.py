"""Extract VP9 IVF streams from WannaCRI USM .dat files."""

from __future__ import annotations

import os
import struct
from typing import BinaryIO

from dat_compressor import cipher


DEFAULT_KEY = 0x7F4551499DF55E68
_CHUNK_HEADER_SIZE = 0x20
_PAYLOAD_OFFSET_BASE = 0x18
_STREAM_PAYLOAD_TYPE = 0
_VIDEO_CHUNK_SIGNATURE = b"@SFV"
_IVF_SIGNATURE = b"DKIF"


def extract_ivf(dat_path: str, output_dir: str) -> str:
    """Extract the encrypted VP9 video stream from a WannaCRI .dat file.

    USM chunk headers are 32 bytes, with multibyte integer fields stored as
    big-endian values. Only stream payloads from video chunks are decrypted and
    concatenated; audio and metadata/header chunks are skipped.
    """
    os.makedirs(output_dir, exist_ok=True)
    original_name = os.path.splitext(os.path.basename(dat_path))[0]
    output_path = os.path.join(output_dir, f"{original_name}.ivf")
    video_key, _audio_key = cipher.generate_keys(DEFAULT_KEY)

    wrote_video_payload = False

    with open(dat_path, "rb") as source, open(output_path, "wb") as output:
        while True:
            header = source.read(_CHUNK_HEADER_SIZE)
            if not header:
                break
            if len(header) != _CHUNK_HEADER_SIZE:
                raise EOFError("Unexpected EOF while reading USM chunk header.")

            chunk_size_after_header = struct.unpack(">I", header[4:8])[0]
            payload_offset = header[9]
            padding_size = struct.unpack(">H", header[10:12])[0]
            payload_type = header[15] & 0x03

            payload_size = chunk_size_after_header - payload_offset - padding_size
            if payload_size < 0:
                raise ValueError(
                    "Invalid payload size in USM chunk "
                    f"(size={chunk_size_after_header}, offset={payload_offset}, "
                    f"padding={padding_size})."
                )

            _skip_exactly(source, max(0, payload_offset - _PAYLOAD_OFFSET_BASE))
            payload = bytearray(_read_exactly(source, payload_size))
            _skip_exactly(source, padding_size)

            if (
                header[:4] != _VIDEO_CHUNK_SIGNATURE
                or payload_type != _STREAM_PAYLOAD_TYPE
                or not payload
            ):
                continue

            decoded = cipher.decrypt_video_packet(payload, video_key)
            if not wrote_video_payload and decoded[:4] != _IVF_SIGNATURE:
                raise ValueError(
                    f"Expected first video payload to start with {_IVF_SIGNATURE!r}, "
                    f"got {bytes(decoded[:4])!r}."
                )

            output.write(decoded)
            wrote_video_payload = True

    if not wrote_video_payload:
        raise ValueError(f"No IVF video stream found in {dat_path!r}.")

    return output_path


def _read_exactly(source: BinaryIO, size: int) -> bytes:
    data = source.read(size)
    if len(data) != size:
        raise EOFError("Unexpected EOF while reading USM chunk payload.")
    return data


def _skip_exactly(source: BinaryIO, size: int) -> None:
    if size <= 0:
        return

    skipped = len(source.read(size))
    if skipped != size:
        raise EOFError("Unexpected EOF while skipping USM chunk padding.")
