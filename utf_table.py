"""CRI @UTF binary table serializer used by WannaCRI USM chunks."""

from __future__ import annotations

import struct
from enum import IntEnum
from typing import Any, Iterable, Mapping, Sequence


class ElementType(IntEnum):
    I8 = 0x10
    U8 = 0x11
    I16 = 0x12
    U16 = 0x13
    I32 = 0x14
    U32 = 0x15
    I64 = 0x16
    U64 = 0x17
    F32 = 0x18
    STRING = 0x1A
    BYTES = 0x1B


_RECURRING_FLAG = 1 << 5      # 0x20 — shared/constant column
_NON_RECURRING_FLAG = 2 << 5  # 0x40 — per-page column
_STRING_ENCODING = "UTF-8"


def pack_pages(
    elements: Sequence[tuple[str, ElementType, Any]],
    pages: Sequence[Mapping[str, Any]],
    page_name: str = "<NULL>",
) -> bytes:
    if not elements:
        raise ValueError("elements must not be empty")
    if not pages:
        raise ValueError("pages must not be empty")

    normalized_elements = _normalize_elements(elements)
    _validate_pages(normalized_elements, pages)

    strings = _StringTable()
    strings.add("<NULL>")
    page_name_offset = strings.add(page_name)
    name_offsets = {name: strings.add(name) for name, _, _ in normalized_elements}

    column_data = bytearray()
    row_data = bytearray()
    bytes_data = bytearray()

    for name, element_type, constant_value in normalized_elements:
        if constant_value is None:
            column_data.append(_NON_RECURRING_FLAG | element_type)
            column_data.extend(_u32(name_offsets[name]))
        else:
            column_data.append(_RECURRING_FLAG | element_type)
            column_data.extend(_u32(name_offsets[name]))
            _pack_value(column_data, element_type, constant_value, strings, bytes_data)

    for page in pages:
        for name, element_type, constant_value in normalized_elements:
            if constant_value is None:
                _pack_value(row_data, element_type, page[name], strings, bytes_data)

    header_size = 24
    rows_size = len(row_data)
    row_size = rows_size // len(pages)
    rows_offset = header_size + len(column_data)
    string_offset = rows_offset + rows_size
    bytes_offset = string_offset + len(strings.data)
    table_size = bytes_offset + len(bytes_data)

    result = bytearray(b"@UTF")
    result.extend(_u32(table_size))
    result.extend(_u32(rows_offset))
    result.extend(_u32(string_offset))
    result.extend(_u32(bytes_offset))
    result.extend(_u32(page_name_offset))
    result.extend(_u16(len(normalized_elements)))
    result.extend(_u16(row_size))
    result.extend(_u32(len(pages)))
    result.extend(column_data)
    result.extend(row_data)
    result.extend(strings.data)
    result.extend(bytes_data)
    return bytes(result)


def _normalize_elements(
    elements: Iterable[tuple[str, ElementType, Any]],
) -> list[tuple[str, ElementType, Any]]:
    normalized: list[tuple[str, ElementType, Any]] = []
    seen: set[str] = set()
    for raw_name, raw_type, constant_value in elements:
        if not raw_name:
            raise ValueError("element names must not be empty")
        if raw_name in seen:
            raise ValueError(f"duplicate element name: {raw_name}")
        seen.add(raw_name)
        element_type = ElementType(raw_type)
        normalized.append((raw_name, element_type, constant_value))
    return normalized


def _validate_pages(
    elements: Sequence[tuple[str, ElementType, Any]],
    pages: Sequence[Mapping[str, Any]],
) -> None:
    required_names = [name for name, _, constant_value in elements if constant_value is None]
    for index, page in enumerate(pages):
        for name in required_names:
            if name not in page:
                raise ValueError(f"page {index} is missing value for element {name!r}")


def _pack_value(
    target: bytearray,
    element_type: ElementType,
    value: Any,
    strings: "_StringTable",
    bytes_data: bytearray,
) -> None:
    if element_type is ElementType.I32:
        target.extend(struct.pack(">i", int(value)))
    elif element_type is ElementType.U32:
        target.extend(struct.pack(">I", int(value)))
    elif element_type is ElementType.I64:
        target.extend(struct.pack(">q", int(value)))
    elif element_type is ElementType.U64:
        target.extend(struct.pack(">Q", int(value)))
    elif element_type is ElementType.F32:
        target.extend(struct.pack("<f", float(value)))
    elif element_type is ElementType.STRING:
        target.extend(_u32(strings.add("" if value is None else str(value))))
    elif element_type is ElementType.I16:
        target.extend(struct.pack(">h", int(value)))
    elif element_type is ElementType.U16:
        target.extend(struct.pack(">H", int(value)))
    elif element_type is ElementType.I8:
        target.extend(struct.pack(">b", int(value)))
    elif element_type is ElementType.U8:
        target.extend(struct.pack(">B", int(value)))
    else:
        raise NotImplementedError(f"unsupported @UTF element type: {element_type.name}")


class _StringTable:
    def __init__(self) -> None:
        self.data = bytearray()
        self._offsets: dict[str, int] = {}

    def add(self, value: str) -> int:
        if value in self._offsets:
            return self._offsets[value]
        offset = len(self.data)
        self.data.extend(value.encode(_STRING_ENCODING))
        self.data.append(0)
        self._offsets[value] = offset
        return offset


def _u16(value: int) -> bytes:
    return struct.pack(">H", value)


def _u32(value: int) -> bytes:
    return struct.pack(">I", value)
