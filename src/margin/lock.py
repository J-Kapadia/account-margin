"""Advisory warehouse lock (plan §6.3).

A non-blocking `flock` on `<warehouse>.lock`. The kernel drops it when the
holder dies, so a killed run needs no cleanup; the file's existence means
nothing and the PID written inside is for the error message only.
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import EXIT_INTERNAL, MarginError

LOCK_MESSAGE = "another margin process holds the warehouse"


@contextmanager
def warehouse_lock(lock_path: Path, *, exclusive: bool) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MarginError(EXIT_INTERNAL, LOCK_MESSAGE) from None
        if exclusive:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the flock
