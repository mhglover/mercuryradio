"""_BufferedOpus state machine: buffer -> serve in order -> end, and underrun -> silence.
Uses a fake underlying source so no ffmpeg is needed."""

import os
import threading
import time

os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("MUSIC_DIR", "/tmp")
os.environ.setdefault("DB_PATH", "/tmp/mrbuf.db")

import bot  # noqa: E402


class _FakeSrc:
    """Yields the given frames, then b"" (ffmpeg EOF)."""

    def __init__(self, frames):
        self._f = list(frames)

    def read(self):
        return self._f.pop(0) if self._f else b""

    def cleanup(self):
        pass


class _BlockingSrc:
    """First read blocks until released — to force an underrun."""

    def __init__(self):
        self.gate = threading.Event()

    def read(self):
        self.gate.wait()
        return b""

    def cleanup(self):
        self.gate.set()


def test_serves_frames_in_order_then_ends():
    b = bot._BufferedOpus(None, None, None, source=_FakeSrc([b"a", b"b", b"c"]))
    b.prebuffer(3, timeout=2.0)
    assert b.read() == b"a"
    assert b.read() == b"b"
    assert b.read() == b"c"
    time.sleep(0.1)  # let the fill thread hit EOF
    assert b.read() == b""  # end -> player stops
    b.cleanup()


def test_underrun_returns_silence_not_a_stall():
    src = _BlockingSrc()
    b = bot._BufferedOpus(None, None, None, source=src)
    # buffer is empty and ffmpeg hasn't ended -> read must return promptly with silence,
    # never block the player (blocking is what causes the speed-up burst).
    assert b.read() == bot.OPUS_SILENCE
    src.gate.set()
    b.cleanup()
