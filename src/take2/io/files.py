"""Bounded, race-free reading of small text files given by the user."""

import os
import stat
from pathlib import Path


def read_text_file(
    path: Path,
    max_bytes: int,
    *,
    label: str,
    error: type[ValueError] = ValueError,
    preview_chars: int = 40,
) -> str:
    """Read a regular file of at most ``max_bytes`` bytes as UTF-8 text (BOM removed).

    The file is opened once and checked through its descriptor, so the path cannot be swapped
    for a device or FIFO between the check and the read. ``O_NONBLOCK`` keeps ``open`` from
    blocking on a FIFO, and the read is bounded even if the file grows meanwhile. Every
    problem raises ``error`` with a message that starts with ``label`` (e.g. "route file").
    """
    name = path.name if len(path.name) <= preview_chars else path.name[:preview_chars] + "..."
    try:
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise error(f"{label} not found: {name}") from exc
    except OSError as exc:
        raise error(f"cannot open {label} {name}: {exc.strerror}") from exc
    except ValueError as exc:  # NUL byte or unencodable characters in the path
        raise error(f"invalid {label} path: {name!r}") from exc
    try:
        # Check the descriptor before fdopen, which itself rejects directories.
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise error(f"{label} not found or not a regular file: {name}")
        os.set_blocking(fd, True)  # O_NONBLOCK was only for opening; read normally
        file = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with file:
        try:
            data = file.read(max_bytes + 1)
        except OSError as exc:
            raise error(f"cannot read {label} {name}: {exc.strerror}") from exc
    if len(data) > max_bytes:
        raise error(f"{label} is too large: more than {max_bytes:,} bytes")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise error(f"{name} is not a UTF-8 text file") from exc
