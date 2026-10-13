"""An exclusive local lease, released when its file handle closes."""
import errno
import os
import time

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def lock_file(handle, *, blocking=True):
    if os.name != "nt":
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    if os.fstat(handle.fileno()).st_size == 0:
        handle.write("\0")
        handle.flush()
    while True:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            if not blocking:
                raise BlockingIOError(exc.errno, "Exclusive file lease is already held") from exc
            time.sleep(0.05)
