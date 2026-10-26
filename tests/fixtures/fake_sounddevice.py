"""A stand-in for the sounddevice module: counts stream opens, opens no device, writes at no cost."""

from __future__ import annotations

import types

import numpy as np


class FakeOutputStream:
    """Records what was written, so a test can check order and content without a sound card."""

    def __init__(self, samplerate=None, channels=None, dtype=None, **kwargs) -> None:
        self.samplerate = samplerate
        self.channels = channels
        self.dtype = dtype
        self.written: list[np.ndarray] = []
        self.started = False
        self.stopped = False
        self.aborted = False
        self.closed = False
        self.dry_on_write = 0  # how many writes to report the device as having run dry

    def start(self) -> None:
        self.started = True

    def write(self, data) -> bool:
        if self.closed:
            raise RuntimeError("write on a closed stream")
        self.written.append(np.array(data, copy=True))
        if self.dry_on_write > 0:
            self.dry_on_write -= 1
            return True
        return False

    def stop(self, ignore_errors: bool = True) -> None:
        self.stopped = True

    def abort(self, ignore_errors: bool = True) -> None:
        self.aborted = True

    def close(self, ignore_errors: bool = True) -> None:
        self.closed = True


def make_sd() -> types.SimpleNamespace:
    """A fake sd module that records every stream it opens, in order."""
    streams: list[FakeOutputStream] = []

    def OutputStream(**kwargs):
        stream = FakeOutputStream(**kwargs)
        streams.append(stream)
        return stream

    return types.SimpleNamespace(OutputStream=OutputStream, streams=streams)
