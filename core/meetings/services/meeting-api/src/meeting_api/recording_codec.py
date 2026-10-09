"""recording.v1 MASTER codec (Python) — the PURE master-build core.

Bytes-in -> bytes-out, deterministic, no IO / DB / subprocess. This is the cloud
meeting-api's recording-assembly core: it turns the recording.v1 chunks a meeting
emitted into one master media file when the meeting ends.

PARALLEL PATH (keep in sync). This is the Python TWIN of the Node module
``meetings/modules/recording/src/recording-codec.ts`` (``buildRecordingMaster``).
Same scope, two languages, names aligned and pinned to the SHARED golden vectors
in ``meetings/modules/recording/src/contracts/golden/``::

    _build_recording_master  <->  buildRecordingMaster   (format dispatch)
    _build_webm_master       <->  buildWebmMaster        (WebM byte-concat)
    _build_wav_master        <->  buildWavMaster         (WAV RIFF header-merge)
    _parse_wav_header        <->  parseWavHeader

The deliberate two-language duplication exists because the all-Node desktop has no
Python meeting-api, while prod assembles via this twin. The goldens drift-lock the
two cores: ``test_recording_golden.py`` (here) and ``golden.test.ts`` (there) both
read the SAME vectors and must reproduce them byte-for-byte. Change a builder here
-> mirror it there, and vice-versa.

Python only: ``MasterWriter`` holds both layouts and the builders are it run over a
list, so meeting-api can stream a master chunk by chunk (``recordings.finalize_master``)
and still write exactly the bytes the goldens pin.

Two strategies, dispatched on the wire's recording.v1 ``format``:

* **webm** — BYTE-CONCAT in seq order. The MediaRecorder stream emits a
  self-describing chunk 0 (EBML + Segment + first Cluster) then Cluster-only
  chunks; stacking the Clusters inside the Segment yields a valid container. The
  empty final chunk concatenates as a no-op. (ffmpeg's concat demuxer would drop
  the Cluster-only inputs, so this is NOT ffmpeg.)
* **wav** — RIFF-aware merge: strip each chunk's 44-byte header, sum the PCM
  payloads, prepend one corrected master header (``fmt`` copied from chunk 0).
"""
from __future__ import annotations

import io
import struct
from typing import Optional, Sequence, Tuple

# WAV / RIFF container magic: "RIFF" then size (4) then "WAVE".
_WAV_MAGIC = b"RIFF"
_WAV_FORMAT = b"WAVE"

# A canonical PCM WAV header is exactly 44 bytes:
# RIFF<sz4>WAVE fmt <16><fmt-chunk-16-bytes> data<datasz4>
_WAV_HEADER_BYTES = 44


def _parse_wav_header(buf: bytes) -> Tuple[bytes, int]:
    """Return ``(fmt_chunk_bytes, declared_data_size)`` for a canonical WAV chunk.

    ``fmt_chunk_bytes`` is the 16-byte fmt-chunk body (PCM format, channels,
    sample-rate, byte-rate, block-align, bits-per-sample) copied verbatim into the
    master so it inherits the source PCM format. ``declared_data_size`` is the data
    size the RIFF header claims (returned for sanity only — the caller slices the
    payload by ``len(buf) - 44``, never by the declared size). Raises on any
    chunk that is too short or not the canonical RIFF/WAVE/fmt/data layout.
    """
    if len(buf) < _WAV_HEADER_BYTES:
        raise ValueError(f"WAV chunk shorter than the 44-byte header: {len(buf)} bytes")
    if buf[:4] != _WAV_MAGIC or buf[8:12] != _WAV_FORMAT:
        raise ValueError(f"WAV chunk missing RIFF/WAVE magic: head={buf[:12]!r}")
    if buf[36:40] != b"data":
        raise ValueError(
            "WAV chunk non-canonical: 'data' expected at offset 36, "
            f"found {buf[36:40]!r}"
        )
    fmt_chunk_bytes = buf[20:36]  # the 16-byte fmt body
    declared_data_size = struct.unpack("<I", buf[40:44])[0]
    return fmt_chunk_bytes, declared_data_size


def _wav_master_header(fmt_chunk: bytes, total_data: int) -> bytes:
    """The one master header: ``RIFF<36+total_data>WAVE fmt <16><fmt-chunk>data<total_data>``."""
    out = io.BytesIO()
    out.write(_WAV_MAGIC)                          # 0..3   "RIFF"
    out.write(struct.pack("<I", 36 + total_data))  # 4..7   RIFF size = header(36) + data
    out.write(_WAV_FORMAT)                         # 8..11  "WAVE"
    out.write(b"fmt ")                             # 12..15 "fmt "
    out.write(struct.pack("<I", 16))               # 16..19 fmt chunk size = 16
    out.write(fmt_chunk)                           # 20..35 16-byte fmt body
    out.write(b"data")                             # 36..39 "data"
    out.write(struct.pack("<I", total_data))       # 40..43 data chunk size
    return out.getvalue()


class MasterWriter:
    """The master, one ALREADY-ordered chunk at a time — the single home of both layouts.

    ``add(chunk)`` returns the master bytes that chunk contributes; ``finish()`` checks the
    whole once every chunk was added. Joining every ``add`` IS the master the builders below
    return, so a caller can stream a master of any length without holding it.

    * **webm** — each chunk as-is (byte-concat).
    * **wav** — the empty final chunk adds nothing; every other chunk adds its PCM payload,
      and the first one is preceded by the master header. That header carries the total PCM
      length, so the writer takes every chunk's byte size up front, and ``finish`` raises if
      the chunks did not hold exactly that much.
    """

    def __init__(self, media_format: str, chunk_sizes: Sequence[int]):
        self._wav = (media_format or "").lower() == "wav"
        self._total_data = sum(
            size - _WAV_HEADER_BYTES for size in chunk_sizes if size >= _WAV_HEADER_BYTES
        )
        self._fmt_chunk: Optional[bytes] = None
        self._chunks = 0
        self._wav_chunks = 0
        self._data_written = 0

    def add(self, chunk: bytes) -> bytes:
        self._chunks += 1
        if not self._wav:
            return chunk
        if len(chunk) < _WAV_HEADER_BYTES:  # the empty final chunk
            return b""
        fmt_chunk, _ = _parse_wav_header(chunk)
        header = b""
        if self._fmt_chunk is None:
            self._fmt_chunk = fmt_chunk
            header = _wav_master_header(fmt_chunk, self._total_data)
        elif fmt_chunk != self._fmt_chunk:
            raise ValueError(f"WAV fmt chunk mismatch at chunk index {self._wav_chunks}")
        self._wav_chunks += 1
        payload = chunk[_WAV_HEADER_BYTES:]
        self._data_written += len(payload)
        return header + payload

    def finish(self) -> None:
        if not self._wav:
            if not self._chunks:
                raise ValueError("_build_webm_master requires at least one chunk")
            return
        if self._fmt_chunk is None:
            raise ValueError("_build_wav_master requires at least one non-empty chunk")
        if self._data_written != self._total_data:
            raise ValueError(
                f"WAV chunks held {self._data_written} PCM bytes; their sizes declared "
                f"{self._total_data}"
            )


def _write_master(media_format: str, chunks: Sequence[bytes]) -> bytes:
    writer = MasterWriter(media_format, [len(c) for c in chunks])
    master = b"".join([writer.add(c) for c in chunks])
    writer.finish()
    return master


def _build_wav_master(chunks: Sequence[bytes]) -> bytes:
    """RIFF-aware merge (mirrors ``buildWavMaster``).

    Skips the empty final chunk, strips each remaining chunk's 44-byte header, sums
    the PCM payloads, and prepends one corrected master header::

        RIFF<36+total_data>WAVE fmt <16><fmt-chunk><data><total_data><payload...>

    The ``fmt`` body is copied verbatim from the FIRST non-empty chunk; every chunk
    must declare the same ``fmt`` (mismatch -> raise).
    """
    return _write_master("wav", chunks)


def _build_webm_master(chunks: Sequence[bytes]) -> bytes:
    """Byte-concat WebM chunks in seq order (mirrors ``buildWebmMaster``).

    The empty final chunk concatenates as a no-op.
    """
    return _write_master("webm", chunks)


def _build_recording_master(media_format: str, chunks: Sequence[bytes]) -> bytes:
    """Dispatch to the master builder by ``format`` — the twin of
    ``buildRecordingMaster``. ``wav`` -> RIFF header-merge; anything else
    (i.e. ``webm``) -> byte-concat. Pure: ALREADY-ordered chunks -> master bytes.
    """
    return _write_master(media_format, chunks)


def build_recording_master(chunks: Sequence[bytes], media_format: str) -> bytes:
    """Public front door: assemble ALREADY-ordered recording.v1 ``chunks`` into a
    single master media buffer for ``media_format`` (``"webm"`` | ``"wav"``).

    The host writes the result to ``master.<format>``. WebM output is a plain
    byte-concat (playable; no top-level duration metadata — meeting-api optionally
    injects it via ffmpeg downstream). WAV output is a RIFF header-merge with the
    data size corrected to the summed PCM length.
    """
    return _build_recording_master(media_format, chunks)
