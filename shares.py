"""Share lifecycle management.

Each share is a single ``onionshare-cli`` subprocess. We capture stdout
to scrape the .onion URL and (for non-public shares) the auth private
key. Shares are persisted to ``state.json`` so we can re-list them in
the UI across restarts of the admin server. The actual onion itself
only exists while the subprocess is alive, unless the user marks the
share as persistent, in which case onionshare writes a session file to
``persistent_sessions/`` and will bring up the same .onion address the
next time it's started.

Supported modes (a subset of upstream OnionShare):

* ``share``   -- one-shot file transfer. Given a list of file/dir paths
                 on disk (under ``shares/<id>/``), generates a .onion
                 where a recipient can download them.
* ``receive`` -- anonymous dropbox. Incoming files land in
                 ``received/<id>/``.
* ``website`` -- serves a static directory as a .onion website.
* ``chat``    -- ephemeral chat room, no persistence.

Concurrency model: a single ``threading.Lock`` guards the in-memory
shares dict and the state file. Subprocesses are reaped lazily by the
``_poll_once`` helper, which is called by every request handler and by
a background thread every second.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

APP_DATA = os.environ.get("OPENHOST_APP_DATA_DIR", "/data/app_data/onionshare")
APP_TEMP = os.environ.get("OPENHOST_APP_TEMP_DIR", "/data/app_temp_data/onionshare")

SHARES_ROOT = os.path.join(APP_DATA, "shares")
RECEIVE_ROOT = os.path.join(APP_DATA, "received")
PERSIST_ROOT = os.path.join(APP_DATA, "persistent_sessions")
RUN_ROOT = os.path.join(APP_TEMP, "run")
STATE_FILE = os.path.join(APP_DATA, "state.json")

VALID_MODES = ("share", "receive", "website", "chat")

# Regexes for parsing onionshare-cli stdout.
ONION_URL_RE = re.compile(r"http://[a-z2-7]{56}\.onion(?:/\S*)?")
PRIVATE_KEY_RE = re.compile(r"Private key:\s*(\S+)")


@dataclass
class Share:
    """Persisted + runtime state for a single share."""

    id: str
    mode: str
    title: str
    public: bool
    persistent: bool
    # Filenames relative to the share's staging dir, for share/website modes.
    files: list[str] = field(default_factory=list)
    # Absolute path to the share's data directory (staging for outbound,
    # landing zone for receive).
    data_dir: str = ""
    # Populated once onionshare-cli prints it.
    onion_url: Optional[str] = None
    private_key: Optional[str] = None
    status: str = "stopped"  # stopped | starting | running | error
    error: Optional[str] = None
    pid: Optional[int] = None
    # Rolling tail of stdout/stderr for display.
    log_tail: list[str] = field(default_factory=list)
    created_at: float = 0.0
    last_started_at: Optional[float] = None


class ShareManager:
    """Owns all shares and their subprocesses."""

    # Maximum number of log lines we retain per share.
    LOG_TAIL_MAX = 200

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._shares: dict[str, Share] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._reader_threads: dict[str, threading.Thread] = {}
        # Count of in-flight uploads per share id. We refuse to
        # ``start`` a share while one of its uploads is still streaming
        # to disk, to eliminate the race where the status check in
        # ``add_file_stream`` passes but the share starts before the
        # write completes. Multiple concurrent uploads to the same
        # share are fine (different filenames) -- hence a counter
        # rather than a plain set.
        self._uploading: dict[str, int] = {}
        os.makedirs(SHARES_ROOT, exist_ok=True)
        os.makedirs(RECEIVE_ROOT, exist_ok=True)
        os.makedirs(PERSIST_ROOT, exist_ok=True)
        os.makedirs(RUN_ROOT, exist_ok=True)
        self._load_state()

        # Background reaper. Daemonized -- dies with the process.
        t = threading.Thread(target=self._reaper_loop, daemon=True)
        t.start()

    # ------------------------------------------------------------------ state

    def _load_state(self) -> None:
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE) as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[shares] WARN: failed to load {STATE_FILE}: {e}", flush=True)
            return
        for sid, data in raw.get("shares", {}).items():
            if not isinstance(data, dict):
                print(f"[shares] WARN: skipping non-dict entry {sid!r}", flush=True)
                continue
            # Every share comes back up as stopped -- we don't inherit
            # subprocesses from the previous process.
            data["status"] = "stopped"
            data["onion_url"] = None
            data["private_key"] = None
            data["pid"] = None
            data["log_tail"] = []
            data["error"] = None
            # Filter to only known fields in case schema changed.
            known = {k: v for k, v in data.items() if k in Share.__annotations__}
            try:
                self._shares[sid] = Share(**known)
            except TypeError as e:
                print(
                    f"[shares] WARN: skipping malformed share {sid!r}: {e}",
                    flush=True,
                )

    def _save_state_locked(self) -> None:
        # Caller must hold self._lock.
        raw = {
            "shares": {
                sid: {
                    "id": s.id,
                    "mode": s.mode,
                    "title": s.title,
                    "public": s.public,
                    "persistent": s.persistent,
                    "files": s.files,
                    "data_dir": s.data_dir,
                    "created_at": s.created_at,
                }
                for sid, s in self._shares.items()
            }
        }
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(raw, f, indent=2)
        os.replace(tmp, STATE_FILE)

    # ------------------------------------------------------------------ CRUD

    def list_shares(self) -> list[Share]:
        import copy

        with self._lock:
            self._poll_once_locked()
            snaps = []
            for live in self._shares.values():
                snap = copy.copy(live)
                snap.log_tail = list(live.log_tail)
                snap.files = list(live.files)
                snaps.append(snap)
            snaps.sort(key=lambda s: s.created_at, reverse=True)
            return snaps

    def get(self, share_id: str) -> Optional[Share]:
        with self._lock:
            self._poll_once_locked()
            return self._shares.get(share_id)

    def snapshot(self, share_id: str) -> Optional[Share]:
        """Return a deep-enough copy of a share for safe rendering.

        The manager mutates the live ``Share`` object from the reader
        thread (appending to ``log_tail``, updating ``status``, etc).
        Render-side code that iterates over ``log_tail`` needs a stable
        snapshot to avoid 'list changed size during iteration' or
        half-updated status strings. We dataclasses.replace() the share
        under the lock, explicitly copying mutable fields.
        """
        import copy

        with self._lock:
            self._poll_once_locked()
            live = self._shares.get(share_id)
            if live is None:
                return None
            # shallow copy of the Share + copies of mutable list fields
            snap = copy.copy(live)
            snap.log_tail = list(live.log_tail)
            snap.files = list(live.files)
            return snap

    def create(
        self,
        mode: str,
        title: str,
        public: bool,
        persistent: bool,
    ) -> Share:
        if mode not in VALID_MODES:
            raise ValueError(f"invalid mode: {mode!r}")
        sid = uuid.uuid4().hex[:12]
        # share/website stage uploads in shares/<id>/; receive lands
        # incoming files in received/<id>/.
        if mode == "receive":
            data_dir = os.path.join(RECEIVE_ROOT, sid)
        else:
            data_dir = os.path.join(SHARES_ROOT, sid)
        os.makedirs(data_dir, exist_ok=True)
        share = Share(
            id=sid,
            mode=mode,
            title=title or f"{mode} {sid}",
            public=public,
            persistent=persistent,
            data_dir=data_dir,
            created_at=time.time(),
        )
        with self._lock:
            self._shares[sid] = share
            self._save_state_locked()
        return share

    # Copy buffer size for streaming uploads. Small enough to keep
    # memory use bounded; large enough to be efficient.
    _COPY_CHUNK = 256 * 1024

    def add_file(self, share_id: str, filename: str, content: bytes) -> None:
        """Drop an in-memory blob into a share's staging directory.

        For large uploads prefer :meth:`add_file_stream` which does not
        buffer the whole payload in memory.
        """
        import io as _io

        self.add_file_stream(share_id, filename, _io.BytesIO(content))

    def add_file_stream(self, share_id: str, filename: str, source) -> None:
        """Stream an uploaded file into a share's staging directory.

        ``source`` is any readable binary file-like object. The contents
        are copied to ``<data_dir>/<safe_filename>`` in chunks, so we
        never buffer the whole upload in memory.
        """
        safe = _safe_filename(filename)
        if not safe:
            raise ValueError("invalid filename")

        # Validate + reserve under the lock. The lock guards against a
        # concurrent ``start()`` racing with us: while our id is in
        # ``_uploading``, ``start()`` will refuse to launch, and while
        # the share is running/starting, we refuse to upload. The file
        # write itself happens *outside* the lock (uploads can be
        # multi-GiB), but correctness is preserved because neither
        # state transition can happen until we remove ourselves from
        # ``_uploading`` below.
        with self._lock:
            share = self._shares.get(share_id)
            if not share:
                raise KeyError(share_id)
            if share.mode not in ("share", "website"):
                raise ValueError("only share/website modes accept files")
            if share.status in ("running", "starting"):
                raise RuntimeError("stop the share before modifying its files")
            self._uploading[share_id] = self._uploading.get(share_id, 0) + 1
            data_dir = share.data_dir

        path = os.path.join(data_dir, safe)
        # Use a unique temp name (not just "<path>.part") so concurrent
        # uploads of the same filename don't clobber each other's temp
        # files. The final rename is still atomic -- whichever upload
        # finishes last wins, which is the same semantics as
        # overwriting a regular file.
        tmp: str | None = None
        tmp_fd = -1
        try:
            # mkstemp is inside the try so that if it raises -- e.g. the
            # temp name (".<safe>.<rand>.part") would exceed the
            # filesystem's NAME_MAX -- the ``finally`` still releases the
            # upload slot. Otherwise a single failed upload would wedge
            # the share: start() refuses to launch while a slot is held.
            tmp_fd, tmp = tempfile.mkstemp(
                prefix=f".{safe}.", suffix=".part", dir=data_dir
            )
            # Write to a tmp file first and atomically rename, so a
            # partial upload doesn't leave a half-written file that
            # would later be served to recipients as though it were
            # complete.
            with os.fdopen(tmp_fd, "wb") as out:
                tmp_fd = -1  # fdopen takes ownership of the fd
                while True:
                    chunk = source.read(self._COPY_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
            os.replace(tmp, path)
            with self._lock:
                share = self._shares.get(share_id)
                if share is not None and safe not in share.files:
                    share.files.append(safe)
                    self._save_state_locked()
        except BaseException:
            if tmp_fd >= 0:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
            if tmp is not None and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            raise
        finally:
            with self._lock:
                self._release_upload_slot(share_id)

    def _release_upload_slot(self, share_id: str) -> None:
        """Decrement the uploading counter; caller holds ``self._lock``."""
        n = self._uploading.get(share_id, 0) - 1
        if n <= 0:
            self._uploading.pop(share_id, None)
        else:
            self._uploading[share_id] = n

    def remove_file(self, share_id: str, filename: str) -> None:
        safe = _safe_filename(filename)
        if not safe:
            raise ValueError("invalid filename")

        # Same pattern as add_file_stream: reserve under the lock so
        # start() can't race us, then do the filesystem work outside
        # the lock, then update share.files under the lock.
        with self._lock:
            share = self._shares.get(share_id)
            if not share:
                raise KeyError(share_id)
            if share.status in ("running", "starting"):
                raise RuntimeError("stop the share before modifying its files")
            self._uploading[share_id] = self._uploading.get(share_id, 0) + 1
            data_dir = share.data_dir

        try:
            path = os.path.join(data_dir, safe)
            if os.path.isfile(path):
                os.remove(path)
            with self._lock:
                share = self._shares.get(share_id)
                if share is not None and safe in share.files:
                    share.files.remove(safe)
                    self._save_state_locked()
        finally:
            with self._lock:
                self._release_upload_slot(share_id)

    def delete(self, share_id: str) -> None:
        self.stop(share_id)
        with self._lock:
            share = self._shares.pop(share_id, None)
            if share is not None:
                # Clean up staging/received data.
                if share.data_dir and os.path.isdir(share.data_dir):
                    shutil.rmtree(share.data_dir, ignore_errors=True)
                # And the persistent session file, if any.
                ps = os.path.join(PERSIST_ROOT, f"{share_id}.json")
                if os.path.exists(ps):
                    os.remove(ps)
            self._save_state_locked()

    # ------------------------------------------------------------------ lifecycle

    def start(self, share_id: str) -> None:
        with self._lock:
            share = self._shares.get(share_id)
            if share is None:
                raise KeyError(share_id)
            if share_id in self._procs:
                # Already running or start in progress.
                return
            if share_id in self._uploading:
                raise RuntimeError(
                    "upload in progress; wait for it to finish before starting"
                )
            if share.mode in ("share", "website") and not share.files:
                raise RuntimeError("add at least one file before starting")

            cmd = self._build_cli_cmd(share)
            env = os.environ.copy()
            # onionshare-cli writes some things to ~/.config/onionshare;
            # point HOME at a per-container scratch dir so it doesn't
            # touch the unknown host HOME.
            env["HOME"] = os.path.join(RUN_ROOT, "home")
            os.makedirs(env["HOME"], exist_ok=True)

            print(
                f"[shares] launching {share_id} mode={share.mode}: {' '.join(cmd)}",
                flush=True,
            )
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=share.data_dir,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=1,
                    text=True,
                    # New process group so we can SIGTERM the whole tree
                    # (onionshare-cli + its spawned tor).
                    start_new_session=True,
                )
            except FileNotFoundError as e:
                share.status = "error"
                share.error = f"onionshare-cli not installed: {e}"
                return

            self._procs[share_id] = proc
            share.pid = proc.pid
            share.status = "starting"
            share.error = None
            share.onion_url = None
            share.private_key = None
            share.log_tail = []
            share.last_started_at = time.time()

            reader = threading.Thread(
                target=self._reader_loop,
                args=(share_id, proc),
                daemon=True,
            )
            reader.start()
            self._reader_threads[share_id] = reader

    def stop(self, share_id: str) -> None:
        with self._lock:
            proc = self._procs.get(share_id)
            share = self._shares.get(share_id)
            if proc is None:
                if share is not None and share.status != "stopped":
                    share.status = "stopped"
                    share.pid = None
                return

        # SIGTERM the process group (outside the lock so the reader can
        # finish and mark status).
        self._terminate_proc(proc)

        # Wait briefly for the reader to report completion.
        for _ in range(30):
            with self._lock:
                if share_id not in self._procs:
                    return
            time.sleep(0.1)

        # If still alive, SIGKILL.
        with self._lock:
            proc = self._procs.get(share_id)
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        with self._lock:
            self._procs.pop(share_id, None)
            self._reader_threads.pop(share_id, None)
            if share is not None:
                share.status = "stopped"
                share.pid = None

    @staticmethod
    def _terminate_proc(proc: subprocess.Popen) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    # ------------------------------------------------------------------ internals

    def _build_cli_cmd(self, share: Share) -> list[str]:
        cmd = ["onionshare-cli"]
        if share.mode == "receive":
            cmd.append("--receive")
        elif share.mode == "website":
            cmd.append("--website")
        elif share.mode == "chat":
            cmd.append("--chat")
        # default (no flag) is share mode

        if share.public:
            cmd.append("--public")
        if share.title:
            cmd += ["--title", share.title]

        if share.persistent:
            ps = os.path.join(PERSIST_ROOT, f"{share.id}.json")
            cmd += ["--persistent", ps]

        # Don't auto-stop share mode after one download -- too surprising.
        if share.mode == "share":
            cmd.append("--no-autostop-sharing")

        if share.mode == "receive":
            cmd += ["--data-dir", share.data_dir]

        # Positional filenames for share/website modes.
        if share.mode in ("share", "website"):
            for fname in share.files:
                cmd.append(os.path.join(share.data_dir, fname))

        return cmd

    def _reader_loop(self, share_id: str, proc: subprocess.Popen) -> None:
        """Scrape stdout for the onion URL + private key, accumulate log."""
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            with self._lock:
                share = self._shares.get(share_id)
                if share is None:
                    continue
                share.log_tail.append(line)
                if len(share.log_tail) > self.LOG_TAIL_MAX:
                    share.log_tail = share.log_tail[-self.LOG_TAIL_MAX :]

                m = ONION_URL_RE.search(line)
                if m and share.onion_url is None:
                    share.onion_url = m.group(0)
                    if share.status == "starting":
                        share.status = "running"

                m = PRIVATE_KEY_RE.search(line)
                if m and share.private_key is None:
                    share.private_key = m.group(1)

        # stdout closed => process is (or is about to be) done.
        rc = proc.wait()
        # We call stop() with SIGTERM first and escalate to SIGKILL if
        # the process doesn't exit quickly; either case is a legitimate
        # user-initiated shutdown, not an error.
        clean_exit_codes = {
            0,
            -signal.SIGTERM,
            128 + signal.SIGTERM,
            -signal.SIGKILL,
            128 + signal.SIGKILL,
        }
        with self._lock:
            share = self._shares.get(share_id)
            if share is not None:
                share.pid = None
                if rc in clean_exit_codes:
                    share.status = "stopped"
                    share.error = None
                else:
                    share.status = "error"
                    if not share.error:
                        share.error = f"onionshare-cli exited with code {rc}"
                share.onion_url = None
                share.private_key = None
            self._procs.pop(share_id, None)
            self._reader_threads.pop(share_id, None)

    def _poll_once_locked(self) -> None:
        """Defensive cleanup for exited subprocesses.

        Normally the reader thread updates status and drops the proc
        from ``_procs`` when the child's stdout closes. If the reader
        thread has died or is otherwise wedged, this catches that: for
        any proc whose ``poll()`` has returned but the corresponding
        reader thread is no longer alive, we forcibly mark the share
        stopped and drop references.
        """
        for sid, proc in list(self._procs.items()):
            if proc.poll() is None:
                continue
            reader = self._reader_threads.get(sid)
            if reader is not None and reader.is_alive():
                # Reader is still draining stdout; let it finish.
                continue
            # Reader is gone (or never registered) and proc exited.
            # Clean up to avoid leaking the reference forever.
            share = self._shares.get(sid)
            if share is not None and share.status not in ("stopped", "error"):
                rc = proc.returncode
                share.status = "stopped" if (rc == 0 or rc is None) else "error"
                if share.status == "error" and not share.error:
                    share.error = f"onionshare-cli exited with code {rc}"
                share.pid = None
                share.onion_url = None
                share.private_key = None
            self._procs.pop(sid, None)
            self._reader_threads.pop(sid, None)

    def _reaper_loop(self) -> None:
        while True:
            time.sleep(1.0)
            with self._lock:
                self._poll_once_locked()


def _safe_filename(name: str) -> str:
    """Reduce a user-supplied filename to a safe leaf basename.

    We keep spaces, unicode, and leading-dot filenames (``.env``,
    ``.gitignore``) but strip directory components and refuse anything
    that would be or resolve to the traversal tokens ``.`` / ``..`` or
    contains NUL bytes. The returned string is always a plain filename
    with no path separators, so joining it onto ``data_dir`` cannot
    escape that directory.
    """
    if not name:
        return ""
    # Normalise Windows-style separators and strip directory components.
    base = os.path.basename(name.replace("\\", "/"))
    if base in ("", ".", ".."):
        return ""
    if "\x00" in base or "/" in base:
        return ""
    # Cap length so that both the final filename AND the temporary upload
    # name (".<safe>.<rand>.part" adds ~15 bytes of affixes) stay within
    # the filesystem's NAME_MAX (255 bytes on ext4). Budget by bytes, not
    # characters, and don't split a multibyte UTF-8 sequence.
    max_name_bytes = 200
    encoded = base.encode("utf-8")
    if len(encoded) > max_name_bytes:
        base = encoded[:max_name_bytes].decode("utf-8", errors="ignore")
    return base
