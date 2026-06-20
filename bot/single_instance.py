"""Cross-platform process lock for the trading runtime."""
from __future__ import annotations

import atexit
import os
from pathlib import Path


class AlreadyRunningError(RuntimeError):
    """Raised when another process owns the runtime lock."""


class SingleInstance:
    """Hold an OS-level file lock until the process exits."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.pid_path = self.path.with_name(self.path.name + ".pid")
        self._file = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0\n")
            handle.flush()
        handle.seek(0)

        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise AlreadyRunningError(
                f"another trading runtime owns {self.path}"
            ) from exc

        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n".encode("ascii"))
        handle.flush()
        temp_pid_path = self.pid_path.with_suffix(self.pid_path.suffix + ".tmp")
        temp_pid_path.write_text(f"{os.getpid()}\n", encoding="ascii")
        os.replace(temp_pid_path, self.pid_path)
        self._file = handle
        atexit.register(self.release)

    def release(self) -> None:
        if self._file is None:
            return
        try:
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._file.close()
        finally:
            self._file = None
        try:
            if int(self.pid_path.read_text(encoding="ascii").strip()) == os.getpid():
                self.pid_path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
