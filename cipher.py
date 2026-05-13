"""WannaCRI key derivation and video packet rolling cipher."""

from __future__ import annotations

import struct


DEFAULT_KEY = 0x7F4551499DF55E68
_VIDEO_KEY_SIZE = 0x40
_HALF_KEY_SIZE = 0x20
_HEADER_SIZE = 0x40
_ROLLING_WINDOW = 0x100


def _byte(value: int) -> int:
    """Match C# unchecked byte arithmetic by keeping the low 8 bits."""
    return value & 0xFF


def generate_keys(key: int = DEFAULT_KEY) -> tuple[bytes, bytes]:
    """Derive WannaCRI video and audio keys from a 64-bit integer key."""
    if key < 0 or key > 0xFFFF_FFFF_FFFF_FFFF:
        raise ValueError("key must fit in an unsigned 64-bit integer")

    # WannaCRI writes the ulong into bytes as little-endian before expanding it.
    cipher_key = struct.pack("<Q", key)

    # C# casts use unchecked byte arithmetic: additions/subtractions wrap at 8 bits.
    derived = bytearray(_HALF_KEY_SIZE)
    derived[0x00] = cipher_key[0]
    derived[0x01] = cipher_key[1]
    derived[0x02] = cipher_key[2]
    derived[0x03] = _byte(cipher_key[3] - 0x34)
    derived[0x04] = _byte(cipher_key[4] + 0xF9)
    derived[0x05] = cipher_key[5] ^ 0x13
    derived[0x06] = _byte(cipher_key[6] + 0x61)
    derived[0x07] = derived[0x00] ^ 0xFF
    derived[0x08] = _byte(derived[0x01] + derived[0x02])
    derived[0x09] = _byte(derived[0x01] - derived[0x07])
    derived[0x0A] = derived[0x02] ^ 0xFF
    derived[0x0B] = derived[0x01] ^ 0xFF
    derived[0x0C] = _byte(derived[0x0B] + derived[0x09])
    derived[0x0D] = _byte(derived[0x08] - derived[0x03])
    derived[0x0E] = derived[0x0D] ^ 0xFF
    derived[0x0F] = _byte(derived[0x0A] - derived[0x0B])
    derived[0x10] = _byte(derived[0x08] - derived[0x0F])
    derived[0x11] = derived[0x10] ^ derived[0x07]
    derived[0x12] = derived[0x0F] ^ 0xFF
    derived[0x13] = derived[0x03] ^ 0x10
    derived[0x14] = _byte(derived[0x04] - 0x32)
    derived[0x15] = _byte(derived[0x05] + 0xED)
    derived[0x16] = derived[0x06] ^ 0xF3
    derived[0x17] = _byte(derived[0x13] - derived[0x0F])
    derived[0x18] = _byte(derived[0x15] + derived[0x07])
    derived[0x19] = _byte(0x21 - derived[0x13])
    derived[0x1A] = derived[0x14] ^ derived[0x17]
    derived[0x1B] = _byte(derived[0x16] + derived[0x16])
    derived[0x1C] = _byte(derived[0x17] + 0x44)
    derived[0x1D] = _byte(derived[0x03] + derived[0x04])
    derived[0x1E] = _byte(derived[0x05] - derived[0x16])
    derived[0x1F] = derived[0x1D] ^ derived[0x13]

    video_key = bytearray(_VIDEO_KEY_SIZE)
    audio_key = bytearray(_HALF_KEY_SIZE)
    audio_template = b"URUC"

    for index in range(_HALF_KEY_SIZE):
        video_key[index] = derived[index]
        # The second video-key half is the bitwise inverse of the derived half.
        video_key[_HALF_KEY_SIZE + index] = derived[index] ^ 0xFF
        audio_key[index] = (
            audio_template[(index >> 1) % len(audio_template)]
            if index % 2 != 0
            else derived[index] ^ 0xFF
        )

    return bytes(video_key), bytes(audio_key)


def decrypt_video_packet(data: bytearray, key: bytes) -> bytearray:
    """Decrypt a WannaCRI video packet in place and return it."""
    _validate_video_key(key)
    encrypted_part_size = len(data) - _HEADER_SIZE
    if encrypted_part_size < 0x200:
        return data

    rolling = bytearray(key[:_VIDEO_KEY_SIZE])

    # Pass 1 decrypts bytes after the first 0x100 payload bytes using the
    # inverse-key half. The rolling byte is refreshed from the newly decrypted
    # plaintext XORed with the original key byte, matching the C# mutation order.
    for index in range(_ROLLING_WINDOW, encrypted_part_size):
        packet_index = _HEADER_SIZE + index
        key_index = _HALF_KEY_SIZE + index % _HALF_KEY_SIZE
        data[packet_index] ^= rolling[key_index]
        rolling[key_index] = data[packet_index] ^ key[key_index]

    # Pass 2 decrypts the first 0x100 payload bytes. Each rolling byte is first
    # mixed with the corresponding already-decrypted byte at payload +0x100.
    for index in range(_ROLLING_WINDOW):
        key_index = index % _HALF_KEY_SIZE
        rolling[key_index] ^= data[0x140 + index]
        data[_HEADER_SIZE + index] ^= rolling[key_index]

    return data


def encrypt_video_packet(data: bytearray, key: bytes) -> bytearray:
    """Encrypt a WannaCRI video packet in place and return it."""
    _validate_video_key(key)
    if len(data) < 0x240:
        return data

    rolling = bytearray(key[:_VIDEO_KEY_SIZE])
    encrypted_part_size = len(data) - _HEADER_SIZE

    # Encryption is the inverse order of decryption: first mix/encrypt the first
    # 0x100 payload bytes against plaintext from the following 0x100-byte window.
    for index in range(_ROLLING_WINDOW):
        key_index = index % _HALF_KEY_SIZE
        rolling[key_index] ^= data[0x140 + index]
        data[_HEADER_SIZE + index] ^= rolling[key_index]

    # Then encrypt the remainder while rolling stores plaintext XOR key, so the
    # next byte uses the same state that decrypt_video_packet reconstructs.
    for index in range(_ROLLING_WINDOW, encrypted_part_size):
        packet_index = _HEADER_SIZE + index
        key_index = _HALF_KEY_SIZE + index % _HALF_KEY_SIZE
        plain = data[packet_index]
        data[packet_index] ^= rolling[key_index]
        rolling[key_index] = plain ^ key[key_index]

    return data


def _validate_video_key(key: bytes) -> None:
    if len(key) < _VIDEO_KEY_SIZE:
        raise ValueError("video key should be 0x40 bytes long")
