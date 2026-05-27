"""AsyncWavWriter: writer-thread offload of PortAudio callback writes.

The whole point of this module (issue #31) is that the audio
callback never blocks on disk I/O. The tests below pin the
contract:

  * Round-trip: bytes enqueued from the "callback side" land in the
    on-disk WAV verbatim, in order.
  * Silence-fill via the explicit count path produces an equivalent
    on-disk result to calling write_frames with the same zero bytes.
  * close() drains the queue before returning -- a write enqueued
    just before close must end up on disk.
  * A truly stuck writer (mocked) doesn't cause write_frames to
    block; instead the queue fills, the writer logs the drop, and
    the callback returns False. This is the load-bearing invariant
    for the audio-corruption-at-15-minutes symptom: a stalled disk
    must never propagate back into the PortAudio callback thread.
"""
from __future__ import annotations

import threading
import time
import wave
from pathlib import Path

import pytest

from meeting_notetaker.audio.wav_writer import AsyncWavWriter


def _read_wav_bytes(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


def test_round_trip_writes_bytes_in_order(tmp_path: Path) -> None:
    """Bytes pushed via write_frames land on disk in the order
    they were enqueued. This is the basic contract -- if it ever
    fails, the writer thread is reordering items, which means the
    audio would scramble at playback time."""
    target = tmp_path / "out.wav"
    writer = AsyncWavWriter(
        target, channels=1, sample_width=2, sample_rate=16000,
    )
    writer.start()
    # 3 distinct chunks, recognizable in the output bytes.
    chunks = [
        b"\x01\x00" * 100,
        b"\x02\x00" * 100,
        b"\x03\x00" * 100,
    ]
    for chunk in chunks:
        assert writer.write_frames(chunk)
    writer.close()
    on_disk = _read_wav_bytes(target)
    assert on_disk == b"".join(chunks)


def test_silence_fill_writes_zeros(tmp_path: Path) -> None:
    """write_silence_frames(N) produces N frames of zero bytes."""
    target = tmp_path / "silence.wav"
    writer = AsyncWavWriter(
        target, channels=2, sample_width=2, sample_rate=48000,
    )
    writer.start()
    writer.write_silence_frames(1000)  # 1000 frames x 2 ch x 2 bytes = 4000 bytes
    writer.close()
    on_disk = _read_wav_bytes(target)
    assert on_disk == b"\x00" * 4000


def test_silence_block_chunking_handles_large_gaps(tmp_path: Path) -> None:
    """Silence-fill for a gap larger than the internal block size
    (8192 frames) must still produce exactly the right byte count.
    Regression guard against the old per-recorder loop in case the
    new helper miscounts the tail."""
    target = tmp_path / "long-silence.wav"
    writer = AsyncWavWriter(
        target, channels=1, sample_width=2, sample_rate=16000,
    )
    writer.start()
    # 8192 * 3 + 100 -- multiple full blocks plus a tail
    writer.write_silence_frames(8192 * 3 + 100)
    writer.close()
    on_disk = _read_wav_bytes(target)
    assert len(on_disk) == (8192 * 3 + 100) * 2  # 1 ch x 2 bytes
    assert on_disk == b"\x00" * len(on_disk)


def test_close_drains_pending_writes(tmp_path: Path) -> None:
    """A burst of writes immediately before close() must all land on
    disk -- close() blocks until the writer thread has consumed the
    queue, otherwise the tail of any recording would be lost."""
    target = tmp_path / "drain.wav"
    writer = AsyncWavWriter(
        target, channels=1, sample_width=2, sample_rate=16000,
    )
    writer.start()
    # Push a lot of items in rapid succession then close immediately.
    for i in range(200):
        writer.write_frames((i & 0xFF).to_bytes(1, "little") * 64)
    writer.close()
    on_disk = _read_wav_bytes(target)
    # 200 chunks of 64 bytes each = 12800 bytes
    assert len(on_disk) == 200 * 64


def test_write_frames_after_close_returns_false(tmp_path: Path) -> None:
    """Pushing onto a closed writer must not block, raise, or
    corrupt anything -- just report the drop and move on. The
    PortAudio callback can race with stop(); this is the guard."""
    target = tmp_path / "after-close.wav"
    writer = AsyncWavWriter(
        target, channels=1, sample_width=2, sample_rate=16000,
    )
    writer.start()
    writer.write_frames(b"\x01\x00" * 10)
    writer.close()
    assert writer.write_frames(b"\x02\x00" * 10) is False
    assert writer.write_silence_frames(100) is False


def test_write_frames_does_not_block_when_writer_is_stuck(
    tmp_path: Path,
) -> None:
    """The whole reason this module exists. If the disk hangs, the
    audio callback must NOT block waiting for write_frames -- it
    must return False after the bounded queue fills.

    We simulate a stuck writer by patching wave.Wave_write.writeframes
    to block on an Event we never set. The queue fills (max 2048
    items per _MAX_QUEUED_ITEMS); subsequent write_frames calls
    return False without blocking. We measure that the call latency
    stays under a callback-budget threshold even when the queue is
    full.
    """
    target = tmp_path / "stuck.wav"
    writer = AsyncWavWriter(
        target, channels=1, sample_width=2, sample_rate=16000,
    )
    # Make the writer thread block forever on its first write.
    block = threading.Event()
    real_wf = writer  # captured for type clarity below

    writer.start()
    # Patch _wf.writeframes on the live writer's wave object so the
    # background thread blocks on its first dequeue.
    assert real_wf._wf is not None  # noqa: SLF001
    real_wf._wf.writeframes = lambda data: block.wait(timeout=30.0)  # noqa: SLF001, ARG005

    # Fill the queue past capacity. Some of these must succeed
    # (until the queue fills); the ones that follow must return
    # False non-blockingly.
    chunk = b"\x00\x00" * 256
    pushed = 0
    dropped = 0
    start = time.monotonic()
    for _ in range(3000):  # well past _MAX_QUEUED_ITEMS (2048)
        if writer.write_frames(chunk):
            pushed += 1
        else:
            dropped += 1
    elapsed = time.monotonic() - start

    # The full sequence of 3000 calls must complete in well under
    # the time it would take to write 3000 chunks to a real disk.
    # 100 ms is generous; on a healthy machine this is single-digit
    # milliseconds.
    assert elapsed < 0.5, (
        f"write_frames must not block when writer is stuck; "
        f"3000 calls took {elapsed * 1000:.0f}ms"
    )
    assert pushed > 0
    assert dropped > 0
    # Unblock the writer so close() can finish.
    block.set()
    writer.close()
