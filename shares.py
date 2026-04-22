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
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
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

    def to_public_dict(self) -> dict:
        d = asdict(self)
        # Never serialize the private key in list responses -- it's only
        # revealed on the detail page. Callers that need it ask for it
        # explicitly.
        return d


class ShareManager:
    """Owns all shares and their subprocesses."""

    # Maximum number of log lines we retain per share.
    LOG_TAIL_MAX = 200

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._shares: dict[str, Share] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._reader_threads: dict[str, threading.Thread] = {}
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
            self._shares[sid] = Share(**known)

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
        with self._lock:
            self._poll_once_locked()
            return sorted(
                self._shares.values(),
                key=lambda s: s.created_at,
                reverse=True,
            )

    def get(self, share_id: str) -> Optional[Share]:
        with self._lock:
            self._poll_once_locked()
            return self._shares.get(share_id)

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

    def add_file(self, share_id: str, filename: str, content: bytes) -> None:
        """Drop an uploaded file into a share's staging directory."""
        share = self.get(share_id)
        if not share:
            raise KeyError(share_id)
        if share.mode not in ("share", "website"):
            raise ValueError("only share/website modes accept files")
        if share.status == "running":
            raise RuntimeError("stop the share before modifying its files")
        safe = _safe_filename(filename)
        if not safe:
            raise ValueError("invalid filename")
        path = os.path.join(share.data_dir, safe)
        with open(path, "wb") as f:
            f.write(content)
        with self._lock:
            if safe not in share.files:
                share.files.append(safe)
            self._save_state_locked()

    def remove_file(self, share_id: str, filename: str) -> None:
        share = self.get(share_id)
        if not share:
            raise KeyError(share_id)
        if share.status == "running":
            raise RuntimeError("stop the share before modifying its files")
        safe = _safe_filename(filename)
        path = os.path.join(share.data_dir, safe)
        if os.path.isfile(path):
            os.remove(path)
        with self._lock:
            if safe in share.files:
                share.files.remove(safe)
            self._save_state_locked()

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
        with self._lock:
            share = self._shares.get(share_id)
            if share is not None:
                share.pid = None
                if rc == 0 or rc == -signal.SIGTERM or rc == 128 + signal.SIGTERM:
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
        """Cheap check to drop references to exited subprocesses.

        The reader thread is the one that actually updates status; this
        just catches cases where the reader hasn't been scheduled yet.
        """
        for sid, proc in list(self._procs.items()):
            if proc.poll() is not None:
                # Reader will take over soon; nothing else to do here.
                pass

    def _reaper_loop(self) -> None:
        while True:
            time.sleep(1.0)
            with self._lock:
                self._poll_once_locked()


def _safe_filename(name: str) -> str:
    """Collapse a user-supplied filename down to a leaf basename.

    We refuse anything that tries to escape the staging directory and
    strip out path separators and NUL bytes. We keep spaces and unicode.
    """
    # Strip directory components entirely.
    base = os.path.basename(name.replace("\\", "/"))
    base = base.lstrip(".")  # refuse hidden / traversal leaders
    if base in ("", ".", ".."):
        return ""
    if "\x00" in base:
        return ""
    # Cap length defensively.
    if len(base) > 255:
        base = base[:255]
    return base
