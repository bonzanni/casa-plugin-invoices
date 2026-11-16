"""Plugin data on disk: paths, atomic durable writes, and the write lock.

Every file lives under `CLAUDE_PLUGIN_DATA`. Every write is a whole-file
replace that is durable before it returns (temp file in the same directory,
fsync, `os.replace`, fsync the directory), and every delete fsyncs its
directory, so a crash or power loss leaves either the old file or the new
one, and never keeps a later step while losing an earlier one (spec §5.3).

Every tool that writes holds `write_lock()` for its whole read-check-write:
an exclusive flock on `<data>/state.lock`, shared by every process in the
Casa container. The kernel drops it when the holder dies, so there is no
stale-lock handling. Readers take no lock; whole-file replaces never show a
torn file.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time

#: Seconds `write_lock()` waits for another process before refusing as busy;
#: read at call time so tests can lower it.
LOCK_WAIT_S = 30.0
_LOCK_POLL_S = 0.05
_LOCK_FILE = "state.lock"
BUSY = "busy: another invoices call is running; try again"


class StoreError(Exception):
    """The data root is missing or unusable, a path escapes it, a stored file
    is unreadable, or the write lock is busy."""


def data_root() -> str:
    """The raw `CLAUDE_PLUGIN_DATA`, created 0o700 when missing."""
    root = os.environ.get("CLAUDE_PLUGIN_DATA") or ""
    if not root:
        raise StoreError("CLAUDE_PLUGIN_DATA is not set; refusing to touch "
                         "plugin data")
    try:
        os.makedirs(root, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise StoreError(f"cannot create the data directory: {exc}") from exc
    return root


def path(*parts: str) -> str:
    """Join `parts` under the data root. Each part may hold `/`-separated
    components; an absolute part or a `..` component is refused."""
    comps = []
    for part in parts:
        if not isinstance(part, str) or "\x00" in part or os.path.isabs(part):
            raise StoreError(f"refused path outside the data root: {part!r}")
        for comp in part.split("/"):
            if comp == "..":
                raise StoreError(
                    f"refused path outside the data root: {part!r}")
            if comp not in ("", "."):
                comps.append(comp)
    return os.path.join(data_root(), *comps)


def _fsync_dir(dirpath: str) -> None:
    fd = os.open(dirpath, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_dir(dirpath: str) -> None:
    """Create `dirpath` and any missing parents 0o700, fsyncing each new
    directory's parent so the new entry survives a power loss too."""
    if os.path.isdir(dirpath):
        return
    parent = os.path.dirname(dirpath)
    _ensure_dir(parent)
    try:
        os.mkdir(dirpath, 0o700)
    except FileExistsError:
        if not os.path.isdir(dirpath):
            raise
        return
    _fsync_dir(parent)


def write_bytes(rel: str, data: bytes) -> None:
    """Atomically and durably replace `rel` with `data`."""
    target = path(rel)
    parent, name = os.path.split(target)
    _ensure_dir(parent)
    fd, tmp = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    _fsync_dir(parent)


def write_json(rel: str, obj) -> None:
    """Atomically and durably replace `rel` with `obj` as UTF-8 JSON."""
    text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    write_bytes(rel, text.encode("utf-8"))


def read_json(rel: str, default=None):
    """The JSON stored at `rel`, or `default` when there is no such file."""
    try:
        with open(path(rel), "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return default
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise StoreError(f"unreadable JSON in {rel}: {exc}") from exc


def exists(rel: str) -> bool:
    return os.path.exists(path(rel))


def listdir(rel: str) -> list[str]:
    """Sorted entry names of directory `rel`; `[]` when it does not exist."""
    try:
        return sorted(os.listdir(path(rel)))
    except FileNotFoundError:
        return []


def delete(rel: str) -> None:
    """Unlink `rel` if present and fsync its directory."""
    target = path(rel)
    try:
        os.unlink(target)
    except FileNotFoundError:
        return
    _fsync_dir(os.path.dirname(target))


@contextlib.contextmanager
def write_lock(wait_s: float | None = None):
    """Hold an exclusive flock on `<data>/state.lock` for the `with` body.

    Polls a non-blocking flock every 50 ms for `wait_s` seconds (default
    `LOCK_WAIT_S`, read now) and raises `StoreError(BUSY)` when it stays
    taken. Closing the descriptor releases the lock."""
    wait = LOCK_WAIT_S if wait_s is None else wait_s
    lock_path = path(_LOCK_FILE)
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise StoreError(f"cannot open the write lock: {exc}") from exc
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StoreError(BUSY) from None
                time.sleep(_LOCK_POLL_S)
            except OSError as exc:
                raise StoreError(f"cannot take the write lock: {exc}") from exc
        yield
    finally:
        os.close(fd)
