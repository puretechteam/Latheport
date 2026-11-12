"""Writing 7z archives on several cores.

py7zr packs everything as one solid block on one core: a 190 MB folder took a minute. Here the files, in
path order (neighbours compress best together), are cut into blocks of 4 to 16 MB, each block packed on its
own thread (Python's lzma lets go of the GIL while it works) and written as one of the archive's "folders",
which every 7z reader opens. The coders are py7zr's: the x86 filter, then LZMA2. Blocks are written as they
finish, so at most one block per thread is held in memory, and the file list follows that order.

Layout (the 7z format as 7-Zip documents it): a 32-byte signature header, the packed blocks, then the header
(itself packed with LZMA2, as 7-Zip and py7zr do) describing blocks and files."""

from __future__ import annotations

import concurrent.futures as cf
import lzma
import os
import struct
import sys
import threading
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from .. import cancel

SIGNATURE = b"7z\xbc\xaf\x27\x1c\x00\x04"
PRESET = 6                  # 8 MB dictionary; with blocks this size 7 packs no smaller and needs twice the memory
DICT = 8 << 20
BLOCK_MIN, BLOCK_MAX = 4 << 20, 16 << 20
CHUNK = 1 << 20
MAX_THREADS = 8             # each packer holds ~94 MB, so 8 is about 750 MB at most
X86 = b"\x03\x03\x01\x03"
LZMA2 = b"\x21"
FILETIME_1970 = 116444736000000000
DIRECTORY, ARCHIVE, UNIX_EXTENSION = 0x10, 0x20, 0x8000


@dataclass
class _Entry:
    name: str
    path: Path
    is_dir: bool
    size: int
    mtime: int        # FILETIME
    attributes: int
    crc: int = 0


@dataclass
class _Block:
    entries: list[_Entry] = field(default_factory=list)
    size: int = 0
    packed: bytes = b""


def threads() -> int:
    """Packers to run: three quarters of the cores, as the smart tools use, and no more than MAX_THREADS."""
    return max(1, min(MAX_THREADS, (os.cpu_count() or 1) * 3 // 4))


def write(dst: Path, items: list[tuple[str, Path]], preset: int = PRESET) -> None:
    """A 7z at dst holding items: (name in the archive, with "/" between folders; the file or folder on disk)."""
    entries = [_entry(name, path) for name, path in items]
    blocks = _blocks([e for e in entries if not e.is_dir and e.size], threads())
    total = sum(b.size for b in blocks)
    cancel.expect(total, "packing")
    stop = threading.Event()
    done = [0]
    lock = threading.Lock()

    def moved(n: int) -> None:
        with lock:
            done[0] += n

    order: list[_Block] = []
    with dst.open("wb") as out:
        out.write(b"\0" * 32)  # the signature header, filled in at the end
        if blocks:
            with cf.ThreadPoolExecutor(min(threads(), len(blocks))) as pool:
                pending = {pool.submit(_pack, b, preset, stop, moved) for b in
                           sorted(blocks, key=lambda b: -b.size)}  # biggest first: no long block left for last
                told = 0
                try:
                    while pending:
                        finished, pending = cf.wait(pending, timeout=0.25, return_when=cf.FIRST_COMPLETED)
                        for f in finished:
                            block = f.result()
                            out.write(block.packed)
                            order.append(block)
                        with lock:
                            now = done[0]
                        cancel.moved(now - told)
                        told = now
                        cancel.check()
                finally:
                    stop.set()  # an error or Cancel: the other packers stop at their next chunk
        header = _header(order, [e for e in entries if e.is_dir or not e.size])
        data_end = out.tell()
        packed_header = _lzma2(header, preset)
        out.write(packed_header)
        encoded = _encoded_header(data_end - 32, len(packed_header), len(header), zlib.crc32(header), _dict(len(header)))
        out.write(encoded)
        start = struct.pack("<QQI", out.tell() - len(encoded) - 32, len(encoded), zlib.crc32(encoded))
        out.seek(0)
        out.write(SIGNATURE + struct.pack("<I", zlib.crc32(start)) + start)


def _entry(name: str, path: Path) -> _Entry:
    st = path.stat()
    is_dir = path.is_dir()
    if sys.platform == "win32":
        attributes = getattr(st, "st_file_attributes", 0) & 0x37  # read-only, hidden, system, folder, archive
    else:
        attributes = UNIX_EXTENSION | (st.st_mode << 16)  # the permissions, as py7zr and p7zip keep them
    attributes |= DIRECTORY if is_dir else 0
    if not is_dir and not attributes & 0x37:
        attributes |= ARCHIVE
    return _Entry(name, path, is_dir, 0 if is_dir else st.st_size,
                  FILETIME_1970 + int(st.st_mtime * 10_000_000), attributes & 0xFFFFFFFF)


def _blocks(files: list[_Entry], n: int) -> list[_Block]:
    """Files cut, in order, into blocks of about a share of the whole each: three per packer, so one that
    finishes early takes another (a big file holds up its block), of 4 to 16 MB."""
    total = sum(e.size for e in files)
    target = min(BLOCK_MAX, max(BLOCK_MIN, total // (3 * n) if n else total))
    blocks: list[_Block] = []
    current = _Block()
    for e in files:
        current.entries.append(e)
        current.size += e.size
        if current.size >= target:
            blocks.append(current)
            current = _Block()
    if current.entries:
        blocks.append(current)
    return blocks


def _dict(size: int) -> int:
    """The dictionary a block needs: its own size, from 64 KB, up to DICT (a smaller one takes less memory)."""
    d = 1 << 16
    while d < min(size, DICT):
        d <<= 1
    return d


def _filters(dict_size: int, preset: int, x86: bool) -> list[dict]:
    lz = {"id": lzma.FILTER_LZMA2, "preset": preset, "dict_size": dict_size}
    return [{"id": lzma.FILTER_X86}, lz] if x86 else [lz]


def _lzma2(data: bytes, preset: int) -> bytes:
    return lzma.compress(data, lzma.FORMAT_RAW, filters=_filters(_dict(len(data)), preset, False))


def _pack(block: _Block, preset: int, stop: threading.Event, moved) -> _Block:
    packer = lzma.LZMACompressor(lzma.FORMAT_RAW, filters=_filters(_dict(block.size), preset, True))
    parts = []
    for e in block.entries:
        crc = 0
        left = e.size
        with e.path.open("rb") as f:
            while left > 0:
                if stop.is_set():
                    return block
                chunk = f.read(min(CHUNK, left))
                if not chunk:
                    raise OSError(f"{e.name} got shorter while it was being packed")
                crc = zlib.crc32(chunk, crc)
                parts.append(packer.compress(chunk))
                left -= len(chunk)
                moved(len(chunk))
        e.crc = crc
    parts.append(packer.flush())
    block.packed = b"".join(parts)
    return block


# ---------------------------------------------------------------- the header

def _number(v: int) -> bytes:
    """7z's variable-length number: the first byte's leading ones say how many bytes follow."""
    first, mask = 0, 0x80
    for i in range(8):
        if v < 1 << (7 * (i + 1)):
            first |= v >> (8 * i)
            break
        first |= mask
        mask >>= 1
    else:
        i = 8
    return bytes([first & 0xFF]) + (v & ((1 << (8 * i)) - 1)).to_bytes(i, "little")


def _bits(flags: list[bool]) -> bytes:
    out = bytearray((len(flags) + 7) // 8)
    for i, on in enumerate(flags):
        if on:
            out[i // 8] |= 0x80 >> (i % 8)
    return bytes(out)


def _dict_property(size: int) -> int:
    for p in range(40):
        if (2 | (p & 1)) << (p // 2 + 11) >= size:
            return p
    return 40


def _folder(dict_size: int, x86: bool) -> bytes:
    lz = bytes([0x20 | len(LZMA2)]) + LZMA2 + b"\x01" + bytes([_dict_property(dict_size)])  # with a property
    if not x86:
        return b"\x01" + lz
    # two coders in py7zr's order, LZMA2 then the x86 filter (libarchive reads only that order): the filter
    # takes LZMA2's output (bind pair: coder 1's input <- coder 0's output); the one packed stream is LZMA2's
    # input, which is implied when there's only one
    return b"\x02" + lz + bytes([len(X86)]) + X86 + _number(1) + _number(0)


def _property(kind: int, body: bytes) -> bytes:
    return bytes([kind]) + _number(len(body)) + body


def _header(blocks: list[_Block], streamless: list[_Entry]) -> bytes:
    h = bytearray(b"\x01")  # Header
    if blocks:
        h += b"\x04"  # MainStreamsInfo
        h += b"\x06" + _number(0) + _number(len(blocks))  # PackInfo: from the start, one stream per block
        h += b"\x09" + b"".join(_number(len(b.packed)) for b in blocks) + b"\x00"
        h += b"\x07\x0b" + _number(len(blocks)) + b"\x00"  # UnpackInfo: the folders, inline
        for b in blocks:
            h += _folder(_dict(b.size), True)
        h += b"\x0c" + b"".join(_number(b.size) * 2 for b in blocks)  # both coders' output sizes
        h += b"\x00"
        h += b"\x08\x0d" + b"".join(_number(len(b.entries)) for b in blocks)  # SubStreamsInfo
        h += b"\x09" + b"".join(_number(e.size) for b in blocks for e in b.entries[:-1])
        h += b"\x0a\x01" + b"".join(struct.pack("<I", e.crc) for b in blocks for e in b.entries)
        h += b"\x00\x00"
    files = [e for b in blocks for e in b.entries] + streamless
    if files:
        h += b"\x05" + _number(len(files))  # FilesInfo
        if streamless:
            empty = [False] * (len(files) - len(streamless)) + [True] * len(streamless)
            h += _property(0x0E, _bits(empty))
            if any(not e.is_dir for e in streamless):
                h += _property(0x0F, _bits([not e.is_dir for e in streamless]))
        h += _property(0x11, b"\x00" + b"".join(e.name.encode("utf-16-le") + b"\0\0" for e in files))
        h += _property(0x14, b"\x01\x00" + b"".join(struct.pack("<Q", e.mtime) for e in files))
        h += _property(0x15, b"\x01\x00" + b"".join(struct.pack("<I", e.attributes) for e in files))
        h += b"\x00"
    h += b"\x00"
    return bytes(h)


def _encoded_header(pack_pos: int, packed: int, size: int, crc: int, dict_size: int) -> bytes:
    """Where the packed header is and how to unpack it."""
    return (b"\x17\x06" + _number(pack_pos) + _number(1) + b"\x09" + _number(packed) + b"\x00"
            + b"\x07\x0b" + _number(1) + b"\x00" + _folder(dict_size, False)
            + b"\x0c" + _number(size) + b"\x0a\x01" + struct.pack("<I", crc) + b"\x00\x00")
