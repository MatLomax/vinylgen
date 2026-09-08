"""Assemble a `.npz` that reuses one huge member from a parent archive verbatim.

A label edit rewrites the small segmentation members (labels, palette RGB, protected
masks) but leaves the per-pixel/per-palette cost volume — by far the largest member —
byte-for-byte unchanged. Re-serialising it would decompress and recompress hundreds of
megabytes for no reason. `write_npz_reusing_member` instead copies the parent's already
compressed cost member straight into the new archive, and only the small members are
encoded fresh.

The verbatim copy pokes at the zip container layout, so it is a fast path guarded by
`reuse_member_supported`; callers fall back to a plain `np.savez_compressed` when it is
not available, and a round-trip test pins the fast path against that fallback.
"""

from __future__ import annotations

import io
import struct
import zipfile
from typing import Any

import numpy as np


def _npy_bytes(array: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    np.save(buffer, array)
    return buffer.getvalue()


def _raw_member(source: bytes, member: str) -> tuple[zipfile.ZipInfo, bytes]:
    """The stored (still-compressed) bytes of one zip member, without decompressing it."""
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        info = archive.getinfo(member)
    reader = io.BytesIO(source)
    reader.seek(info.header_offset)
    local = reader.read(30)
    if local[:4] != b"PK\x03\x04":
        raise ValueError("member local header is not where the directory says it is")
    name_length = struct.unpack("<H", local[26:28])[0]
    extra_length = struct.unpack("<H", local[28:30])[0]
    reader.seek(info.header_offset + 30 + name_length + extra_length)
    raw = reader.read(info.compress_size)
    if len(raw) != info.compress_size:
        raise ValueError("member compressed payload is truncated")
    return info, raw


def reuse_member_supported() -> bool:
    """Whether this interpreter's zipfile exposes the internals the fast path pokes at."""
    probe = zipfile.ZipFile(io.BytesIO(), "w")
    try:
        return all(hasattr(probe, attribute)
                   for attribute in ("fp", "start_dir", "filelist", "NameToInfo", "_didModify"))
    finally:
        probe.close()


def write_npz_reusing_member(destination: io.BytesIO, *, source: bytes,
                             reuse: str, fresh: dict[str, np.ndarray]) -> None:
    """Write an `.npz` of `fresh` arrays plus `reuse` copied verbatim from `source`.

    `reuse` is the member name (without the `.npy` suffix) to carry over; `source` is the
    raw bytes of the parent `.npz`. Raises if the container cannot be assembled, so the
    caller can fall back to re-encoding every array.
    """
    info, raw = _raw_member(source, f"{reuse}.npy")
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, array in fresh.items():
            archive.writestr(f"{name}.npy", _npy_bytes(array))
        carried = zipfile.ZipInfo(f"{reuse}.npy")
        carried.compress_type = zipfile.ZIP_DEFLATED
        carried.compress_size = info.compress_size
        carried.file_size = info.file_size
        carried.CRC = info.CRC
        carried.flag_bits = 0
        carried.create_system = 0
        archive.fp.seek(0, 2)
        carried.header_offset = archive.fp.tell()
        archive.fp.write(carried.FileHeader())
        archive.fp.write(raw)
        archive.start_dir = archive.fp.tell()
        archive.filelist.append(carried)
        archive.NameToInfo[carried.filename] = carried
        archive._didModify = True  # noqa: SLF001 - direct append bypasses the public writer


def savez_bytes(**arrays: Any) -> bytes:
    """The plain, always-correct fallback: compress every array with numpy."""
    buffer = io.BytesIO()
    np.savez_compressed(buffer, **arrays)
    return buffer.getvalue()
