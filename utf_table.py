"""CRI @UTF binary table serializer used by WannaCRI USM chunks."""

from __future__ import annotations

import struct
from enum import IntEnum
from typing import Any, Iterable, Mapping, Sequence


class ElementType(IntEnum):
    """Public element type values expected by the Python USM creator tasks."""

    CHAR = 0x10
    SHORT = 0x11
    INT = 0x12
    LONGLONG = 0x13
    FLOAT = 0x14
    STRING = 0x15
    BYTES = 0x16


_CONSTANT_FLAG = 0x10
_PER_PAGE_FLAG = 0x30
_STRING_ENCODING = "cp932"


def pack_pages(
    elements: Sequence[tuple[str, ElementType, Any]],
    pages: Sequence[Mapping[str, Any]],
) -> bytes:
    """Serialize CRI @UTF pages.

    ``elements`` defines the table columns as ``(name, type, constant_value)``.
    When ``constant_value`` is not ``None`` the column is written once in the
    column/shared-value area with the constant flag.  Otherwise each page must
    provide the column value and the column definition receives the per-page
    flag.
    """

    if not elements:
        raise ValueError("elements must not be empty")
    if not pages:
        raise ValueError("pages must not be empty")

    normalized_elements = _normalize_elements(elements)
    _validate_pages(normalized_elements, pages)

    strings = _StringTable()
    table_name_offset = strings.add("<NULL>")
    name_offsets = {name: strings.add(name) for name, _, _ in normalized_elements}

    column_data = bytearray()
    row_data = bytearray()
    bytes_data = bytearray()

    for name, element_type, constant_value in normalized_elements:
        if constant_value is None:
            # Column definition: one type byte with the per-page flag, followed
            # by a 32-bit string-table offset for the column name.  Actual row
            # values are packed later in page order.
            column_data.append(_PER_PAGE_FLAG | element_type)
            column_data.extend(_u32(name_offsets[name]))
        else:
            # Constant/shared column: definition and its single shared value are
            # stored together before the row data.
            column_data.append(_CONSTANT_FLAG | element_type)
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

    # Binary layout:
    #   0x00  "@UTF" signature
    #   0x04  table size from the first header field through byte-array data
    #   0x08  row-data offset relative to the first header field
    #   0x0C  string-table offset relative to the first header field
    #   0x10  byte-array table offset relative to the first header field
    #   0x14  table-name string offset (WannaCRI uses "<NULL>")
    #   0x18  column count
    #   0x1A  per-row byte size for per-page values
    #   0x1C  row/page count
    result = bytearray(b"@UTF")
    result.extend(_u32(table_size))
    result.extend(_u32(rows_offset))
    result.extend(_u32(string_offset))
    result.extend(_u32(bytes_offset))
    result.extend(_u32(table_name_offset))
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
        if element_type not in {
            ElementType.INT,
            ElementType.STRING,
            ElementType.FLOAT,
            ElementType.LONGLONG,
        }:
            raise NotImplementedError(f"unsupported @UTF element type: {element_type.name}")
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
    if element_type is ElementType.INT:
        target.extend(struct.pack(">i", int(value)))
    elif element_type is ElementType.LONGLONG:
        target.extend(struct.pack(">q", int(value)))
    elif element_type is ElementType.FLOAT:
        target.extend(struct.pack(">f", float(value)))
    elif element_type is ElementType.STRING:
        target.extend(_u32(strings.add("" if value is None else str(value))))
    else:
        raise NotImplementedError(f"unsupported @UTF element type: {element_type.name}")


class _StringTable:
    """Deduplicated, null-terminated Shift-JIS string table."""

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
