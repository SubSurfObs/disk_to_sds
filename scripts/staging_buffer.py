"""Buffer-and-drain pattern for staging-bound SDS ingest.

Both ingest paths (Gecko SD pull, EchoPro USB convert) write SDS day-files to an
SMB-mounted staging archive. The mount is flaky -- a 30-min drop stalls the
producer (USB read + SUDS conversion + miniSEED encode) for 30 min of wasted
work. This module decouples production from staging:

  producer -> LOCAL buffer (always succeeds, local-disk speed)
                 |
                 v
            drain to staging  (on every successful day, opportunistic; the
                               buffer survives mount drops with zero data loss)

If the buffer hits a configurable cap (default ~8 GB), the producer pauses
its read loop until drain catches up. So the mount can be down for as long as
the buffer can hold (~90 days of EchoPro output, ~5 GB of Gecko-day output)
without stalling the producer.

Public API:
    BufferedStaging(local_root, remote_root, cap_mb=8000, mount_script=None)
        .write_root               -> Path; hand this to your SDS writer
        .has_in_buffer(rel_path)  -> bool; for resume / day_already_done checks
        .has_in_remote(rel_path)  -> bool; same on the remote side
        .promote_pending()        -> (n_promoted, n_failed, mb_done)
        .wait_for_capacity()      -> block while buffer >= cap, with drain
        .current_mb               -> int; size of pending files
        .drain_blocking()         -> drain everything, blocking until success

The module surfaces two highly-visible terminal lines on every state change:

  >>> [staging 12:34:56] DROPPED: mount /Volumes/foo unreachable; ...
  >>> [staging 12:36:01] RESUMED: mount back after 65 s; drained 14 files ...
"""
from __future__ import annotations

import errno
import os
import shutil
import subprocess
import time
from pathlib import Path

# OSError errnos that almost always mean "SMB mount dropped" rather than a
# real programming/permissions bug. ENOTCONN is the one macOS raises first
# when an in-flight write hits a stale CIFS connection.
MOUNT_DROP_ERRNOS = {errno.ENOTCONN, errno.EIO, errno.ENODEV, errno.ENOENT,
                     errno.EPIPE, errno.ETIMEDOUT, errno.EHOSTUNREACH}


def is_mount_drop_error(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and exc.errno in MOUNT_DROP_ERRNOS


def probe_mount(path: Path) -> bool:
    """True iff `path` is reachable AND writable (cheap touch-and-unlink)."""
    try:
        p = Path(path)
        if not p.is_dir():
            return False
        probe = p / f".mount_probe.{os.getpid()}"
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def _banner(tag: str, msg: str) -> None:
    """Visually distinct status line for mount events (prefix + timestamp so
    it's easy to spot in the log)."""
    t = time.strftime("%H:%M:%S")
    print(f"  >>> [staging {t}] {tag}: {msg}", flush=True)


class BufferedStaging:
    def __init__(self, local_root, remote_root, cap_mb: int = 8000,
                 mount_script=None, poll_seconds: int = 30):
        self.local: Path = Path(local_root)
        self.remote: Path = Path(remote_root)
        self.cap_mb = int(cap_mb)
        self.mount_script = Path(mount_script) if mount_script else None
        self.poll_seconds = poll_seconds
        self.local.mkdir(parents=True, exist_ok=True)
        self._down_since: float | None = None
        self._announced_down = False

    @property
    def write_root(self) -> Path:
        return self.local

    def _pending_files(self) -> list[Path]:
        return sorted(p for p in self.local.rglob("*")
                      if p.is_file() and not p.name.endswith(".partial"))

    @property
    def current_mb(self) -> float:
        return sum(p.stat().st_size for p in self._pending_files()) / (1024 * 1024)

    def has_in_buffer(self, rel_path) -> bool:
        f = self.local / rel_path
        return f.is_file() and f.stat().st_size > 0

    def has_in_remote(self, rel_path) -> bool:
        try:
            f = self.remote / rel_path
            return f.is_file() and f.stat().st_size > 0
        except OSError:
            return False  # mount probably down; treat as not-there

    def _mount_down(self) -> None:
        if not self._announced_down:
            self._down_since = time.time()
            self._announced_down = True
            _banner("DROPPED",
                    f"mount {self.remote.parent} unreachable; producer "
                    f"continues to local buffer "
                    f"({self.current_mb:.0f} / {self.cap_mb} MB used)")

    def _mount_up(self, promoted: int, mb_done: float) -> None:
        if self._announced_down:
            dt = time.time() - (self._down_since or time.time())
            self._announced_down = False
            self._down_since = None
            _banner("RESUMED",
                    f"mount back after {dt:.0f} s; drained {promoted} file(s) "
                    f"({mb_done:.0f} MB), buffer now "
                    f"{self.current_mb:.0f} MB")

    def promote_pending(self) -> tuple[int, int, float]:
        """Copy every pending local file to the remote (atomic .partial -> rename),
        then delete the local copy on success. Returns (promoted, failed, mb_done).
        Stops cleanly on the first mount-drop error -- the rest stay in the buffer
        for the next attempt."""
        pending = self._pending_files()
        if not pending:
            return 0, 0, 0.0
        if not probe_mount(self.remote):
            self._mount_down()
            return 0, 0, 0.0
        promoted = failed = 0
        mb_done = 0.0
        for src in pending:
            try:
                rel = src.relative_to(self.local)
            except ValueError:
                continue
            dst = self.remote / rel
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                if is_mount_drop_error(e):
                    self._mount_down()
                    break
                failed += 1
                continue
            dst_partial = dst.with_suffix(dst.suffix + ".partial")
            local_size = src.stat().st_size
            try:
                shutil.copyfile(str(src), str(dst_partial))
                if dst_partial.stat().st_size != local_size:
                    dst_partial.unlink(missing_ok=True)
                    failed += 1
                    continue
                os.replace(dst_partial, dst)
                src.unlink(missing_ok=True)
                promoted += 1
                mb_done += local_size / (1024 * 1024)
            except OSError as e:
                try:
                    dst_partial.unlink()
                except OSError:
                    pass
                if is_mount_drop_error(e):
                    self._mount_down()
                    break
                failed += 1
        if promoted and self._announced_down is False and self._down_since is not None:
            # Drained but never had a "down" event in this cycle -- nothing to log
            pass
        if promoted:
            self._mount_up(promoted, mb_done)
        return promoted, failed, mb_done

    def _try_remount(self, attempt: int) -> None:
        if not self.mount_script:
            return
        _banner("REMOUNT", f"running {self.mount_script.name} (attempt {attempt})")
        try:
            subprocess.run([str(self.mount_script)], check=False,
                           capture_output=True, timeout=60)
        except Exception:
            pass

    def wait_for_capacity(self) -> None:
        """Block while buffer is at or above cap. Continuously tries to drain
        (with auto-remount every other poll). Returns once buffer is below cap."""
        attempts = 0
        while self.current_mb >= self.cap_mb:
            self.promote_pending()
            if self.current_mb < self.cap_mb:
                return
            attempts += 1
            _banner("BLOCKED",
                    f"buffer at cap ({self.current_mb:.0f} / {self.cap_mb} MB); "
                    f"sleeping {self.poll_seconds} s before retry")
            if attempts % 2 == 1:
                self._try_remount(attempts)
            time.sleep(self.poll_seconds)

    def drain_blocking(self, max_wait_seconds: int = 3600) -> bool:
        """Drain everything pending; block (with remount attempts) until done
        or until max_wait_seconds elapses. Returns True iff buffer is empty."""
        t0 = time.time()
        attempts = 0
        while self.current_mb > 0:
            promoted, _, _ = self.promote_pending()
            if self.current_mb == 0:
                return True
            if promoted == 0:
                attempts += 1
                if attempts % 2 == 1:
                    self._try_remount(attempts)
                if time.time() - t0 > max_wait_seconds:
                    return False
                _banner("DRAINING",
                        f"{self.current_mb:.0f} MB still in buffer; "
                        f"sleeping {self.poll_seconds} s")
                time.sleep(self.poll_seconds)
        return True
