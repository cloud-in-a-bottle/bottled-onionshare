"""OpenHost OnionShare admin server.

A small stdlib HTTP server on port 8080 that lets the OpenHost owner
create, manage, and monitor OnionShare "shares". All routes are
authenticated by the OpenHost router in front of us, so we do no auth
of our own -- the assumption is that anything reaching this process is
the legitimate owner.

The share *itself* (the .onion endpoint that external users hit) is
served by the onionshare-cli subprocess and goes out over the Tor
network; it never touches this admin port.
"""

from __future__ import annotations

import cgi
import html
import http.server
import io
import json
import os
import socketserver
import urllib.parse
from typing import Any

import shares

ADMIN_PORT = 8080
TEMPLATES_DIR = "/app/templates"
STATIC_DIR = "/app/static"


class _BadRequest(Exception):
    """Raised by request helpers to short-circuit with HTTP 400."""


# One shared manager instance.
manager = shares.ShareManager()


# --------------------------------------------------------------------- utils


def _read_template(name: str) -> str:
    with open(os.path.join(TEMPLATES_DIR, name)) as f:
        return f.read()


def _render(title: str, content: str, flash: str = "") -> str:
    # We deliberately use str.replace() rather than str.format(): log
    # output and user-supplied filenames that end up in ``content`` may
    # contain literal ``{`` / ``}`` characters, which str.format would
    # attempt (and fail) to treat as placeholders.
    return (
        _read_template("base.html")
        .replace("{title}", html.escape(title))
        .replace("{flash}", flash)
        .replace("{content}", content)
    )


def _flash_html(message: str, kind: str = "success") -> str:
    return f'<div class="flash flash-{kind}">{html.escape(message)}</div>'


def _mode_label(mode: str) -> str:
    return {
        "share": "Share files",
        "receive": "Receive files",
        "website": "Website",
        "chat": "Chat",
    }.get(mode, mode)


def _status_badge(status: str) -> str:
    klass = {
        "running": "status-ok",
        "starting": "status-warn",
        "stopped": "status-idle",
        "error": "status-err",
    }.get(status, "status-idle")
    return f'<span class="status {klass}">{html.escape(status)}</span>'


def _share_list_html(share_list: list[shares.Share]) -> str:
    if not share_list:
        return (
            '<p class="muted">No shares yet. '
            '<a href="/shares/new">Create one</a>.</p>'
        )
    rows = []
    for s in share_list:
        onion = (
            f'<code class="onion">{html.escape(s.onion_url)}</code>'
            if s.onion_url
            else '<span class="muted">—</span>'
        )
        rows.append(
            "<tr>"
            f'<td><a href="/shares/{html.escape(s.id)}">{html.escape(s.title)}</a></td>'
            f"<td>{html.escape(_mode_label(s.mode))}</td>"
            f"<td>{_status_badge(s.status)}</td>"
            f"<td>{onion}</td>"
            "</tr>"
        )
    return (
        '<table class="shares"><thead><tr>'
        "<th>Title</th><th>Mode</th><th>Status</th><th>Onion URL</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


# --------------------------------------------------------------------- handler


class AdminHandler(http.server.BaseHTTPRequestHandler):
    # --- dispatch ---

    def do_GET(self) -> None:  # noqa: N802 (stdlib API)
        try:
            self._do_get()
        except _BadRequest as e:
            self._respond_html(400, _render("Bad request", f"<h2>{html.escape(str(e))}</h2>"))

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._do_post()
        except _BadRequest as e:
            self._respond_html(400, _render("Bad request", f"<h2>{html.escape(str(e))}</h2>"))

    def _do_get(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/health":
            self._health()
        elif path == "/":
            self._dashboard()
        elif path == "/shares":
            self._redirect("/")
        elif path == "/shares/new":
            self._new_share_form()
        elif path.startswith("/shares/") and path.endswith("/files"):
            sid = path[len("/shares/") : -len("/files")]
            self._share_files_json(sid)
        elif path.startswith("/shares/"):
            sid = path[len("/shares/") :]
            self._share_detail(sid)
        elif path.startswith("/static/"):
            self._serve_static(path[len("/static/") :])
        else:
            self._not_found()

    def _do_post(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/shares/new":
            self._new_share_submit()
            return
        if path.startswith("/shares/") and path.endswith("/upload"):
            sid = path[len("/shares/") : -len("/upload")]
            self._upload_file(sid)
            return
        if path.startswith("/shares/") and path.endswith("/delete-file"):
            sid = path[len("/shares/") : -len("/delete-file")]
            self._delete_file(sid)
            return
        if path.startswith("/shares/") and path.endswith("/start"):
            sid = path[len("/shares/") : -len("/start")]
            self._start_share(sid)
            return
        if path.startswith("/shares/") and path.endswith("/stop"):
            sid = path[len("/shares/") : -len("/stop")]
            self._stop_share(sid)
            return
        if path.startswith("/shares/") and path.endswith("/delete"):
            sid = path[len("/shares/") : -len("/delete")]
            self._delete_share(sid)
            return

        self._not_found()

    # --- helpers ---

    # Cap request bodies to something reasonable for forms (1 MiB). File
    # uploads use a different path with a higher cap; see
    # ``_upload_file``.
    _MAX_FORM_BODY = 1 * 1024 * 1024
    _MAX_UPLOAD_BODY = 2 * 1024 * 1024 * 1024  # 2 GiB per upload request

    def _parse_content_length(self, maximum: int) -> int:
        raw = self.headers.get("Content-Length", "0")
        try:
            length = int(raw)
        except (TypeError, ValueError):
            raise _BadRequest("invalid Content-Length")
        if length < 0:
            raise _BadRequest("negative Content-Length")
        if length > maximum:
            raise _BadRequest(f"request body too large (max {maximum} bytes)")
        return length

    def _read_body(self) -> bytes:
        length = self._parse_content_length(self._MAX_FORM_BODY)
        return self.rfile.read(length)

    def _form_params(self) -> dict[str, list[str]]:
        body = self._read_body().decode("utf-8", errors="replace")
        return urllib.parse.parse_qs(body, keep_blank_values=True)

    def _respond_html(self, code: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _respond_json(self, code: int, data: Any) -> None:
        encoded = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def _not_found(self) -> None:
        self._respond_html(404, _render("Not Found", "<h2>404 Not Found</h2>"))

    # --- routes: views ---

    def _health(self) -> None:
        self._respond_json(
            200,
            {
                "status": "healthy",
                "shares": len(manager.list_shares()),
            },
        )

    def _dashboard(self) -> None:
        share_list = manager.list_shares()
        tpl = _read_template("dashboard.html")
        content = tpl.replace("{share_list}", _share_list_html(share_list))
        self._respond_html(200, _render("Dashboard", content))

    def _new_share_form(self, flash: str = "") -> None:
        tpl = _read_template("new_share.html")
        self._respond_html(200, _render("New share", tpl, flash=flash))

    def _share_detail(self, share_id: str, flash: str = "") -> None:
        share = manager.get(share_id)
        if share is None:
            self._not_found()
            return
        tpl = _read_template("share_detail.html")

        # Private key is only rendered on the detail page while the
        # share is running.
        private_key_html = ""
        if share.private_key and share.status == "running":
            private_key_html = (
                '<div class="private-key">'
                "<label>Private key (give this to your recipient along "
                "with the URL):</label>"
                f"<code>{html.escape(share.private_key)}</code>"
                "</div>"
            )

        onion_html = (
            f'<code class="onion">{html.escape(share.onion_url)}</code>'
            if share.onion_url
            else '<span class="muted">Not running</span>'
        )

        if share.mode in ("share", "website"):
            file_list = _render_file_list(share)
            files_section = _read_template("share_files_section.html").replace(
                "{file_list}", file_list
            ).replace("{share_id}", html.escape(share.id))
        else:
            files_section = ""

        if share.mode == "receive":
            received = _render_received_list(share)
            received_section = (
                '<h3>Received files</h3>'
                f"<p class=\"muted\">Files uploaded by recipients land in "
                f"<code>{html.escape(share.data_dir)}</code>.</p>"
                f"{received}"
            )
        else:
            received_section = ""

        log_text = "\n".join(share.log_tail) or "(no output yet)"
        log_html = f'<pre class="log">{html.escape(log_text)}</pre>'

        error_html = (
            f'<div class="flash flash-error">{html.escape(share.error)}</div>'
            if share.error
            else ""
        )

        content = (
            tpl.replace("{share_id}", html.escape(share.id))
            .replace("{title}", html.escape(share.title))
            .replace("{mode}", html.escape(_mode_label(share.mode)))
            .replace("{status}", _status_badge(share.status))
            .replace("{onion}", onion_html)
            .replace("{private_key}", private_key_html)
            .replace("{files_section}", files_section)
            .replace("{received_section}", received_section)
            .replace("{error}", error_html)
            .replace("{log}", log_html)
            .replace(
                "{public}", "yes" if share.public else "no (private key required)"
            )
            .replace("{persistent}", "yes" if share.persistent else "no")
            .replace(
                "{start_stop_button}",
                _start_stop_button_html(share),
            )
        )
        self._respond_html(200, _render(share.title, content, flash=flash))

    def _share_files_json(self, share_id: str) -> None:
        share = manager.get(share_id)
        if share is None:
            self._respond_json(404, {"error": "not found"})
            return
        self._respond_json(200, {"files": share.files})

    # --- routes: actions ---

    def _new_share_submit(self) -> None:
        params = self._form_params()
        mode = (params.get("mode") or ["share"])[0]
        title = (params.get("title") or [""])[0].strip()
        public = "public" in params
        persistent = "persistent" in params

        if mode not in shares.VALID_MODES:
            self._new_share_form(flash=_flash_html("Invalid mode.", "error"))
            return

        try:
            share = manager.create(
                mode=mode, title=title, public=public, persistent=persistent
            )
        except ValueError as e:
            self._new_share_form(flash=_flash_html(str(e), "error"))
            return

        self._redirect(f"/shares/{share.id}")

    def _upload_file(self, share_id: str) -> None:
        share = manager.get(share_id)
        if share is None:
            self._not_found()
            return
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._share_detail(
                share_id,
                flash=_flash_html("Expected multipart upload.", "error"),
            )
            return
        try:
            length = self._parse_content_length(self._MAX_UPLOAD_BODY)
        except _BadRequest as e:
            self._respond_html(413, _render("Upload too large", f"<h2>{html.escape(str(e))}</h2>"))
            return

        # Let FieldStorage stream directly from the request body. For
        # fields larger than cgi's in-memory threshold it will spool to
        # a temp file on disk, which is what we want for multi-GiB
        # uploads -- buffering the full body in memory would OOM the
        # container.
        fs = cgi.FieldStorage(
            fp=self.rfile,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": content_type,
                "CONTENT_LENGTH": str(length),
            },
            keep_blank_values=True,
        )

        errors: list[str] = []
        added = 0
        # FieldStorage indexing returns either a single item or a list
        # of items for the same field name. Handle both.
        if "file" in fs:
            items = fs["file"]
            if not isinstance(items, list):
                items = [items]
            for item in items:
                if not getattr(item, "filename", None):
                    continue
                try:
                    # ``item.file`` is the streaming (possibly spooled)
                    # file-like object. ``add_file_stream`` copies it
                    # chunk-by-chunk so we never pull the whole upload
                    # into memory.
                    manager.add_file_stream(share_id, item.filename, item.file)
                    added += 1
                except (ValueError, RuntimeError, KeyError) as e:
                    errors.append(f"{item.filename}: {e}")

        if errors and not added:
            flash = _flash_html("; ".join(errors), "error")
        elif errors:
            flash = _flash_html(
                f"Added {added} file(s); errors: {'; '.join(errors)}", "warn"
            )
        else:
            flash = _flash_html(f"Added {added} file(s).")
        self._share_detail(share_id, flash=flash)

    def _delete_file(self, share_id: str) -> None:
        params = self._form_params()
        filename = (params.get("filename") or [""])[0]
        try:
            manager.remove_file(share_id, filename)
            flash = _flash_html(f"Removed {filename}.")
        except (KeyError, RuntimeError, ValueError) as e:
            flash = _flash_html(str(e), "error")
        self._share_detail(share_id, flash=flash)

    def _start_share(self, share_id: str) -> None:
        try:
            manager.start(share_id)
            flash = _flash_html("Starting… refresh in a few seconds for the URL.")
        except (KeyError, RuntimeError) as e:
            flash = _flash_html(str(e), "error")
        self._share_detail(share_id, flash=flash)

    def _stop_share(self, share_id: str) -> None:
        try:
            manager.stop(share_id)
            flash = _flash_html("Stopped.")
        except KeyError:
            self._not_found()
            return
        self._share_detail(share_id, flash=flash)

    def _delete_share(self, share_id: str) -> None:
        try:
            manager.delete(share_id)
        except KeyError:
            self._not_found()
            return
        self._redirect("/")

    # --- static ---

    def _serve_static(self, filename: str) -> None:
        # Strip any attempt at traversal.
        if "/" in filename or ".." in filename or filename.startswith("."):
            self.send_response(404)
            self.end_headers()
            return
        filepath = os.path.join(STATIC_DIR, filename)
        if not os.path.isfile(filepath):
            self.send_response(404)
            self.end_headers()
            return
        ext = os.path.splitext(filename)[1]
        content_type = {
            ".css": "text/css",
            ".js": "application/javascript",
            ".svg": "image/svg+xml",
            ".png": "image/png",
        }.get(ext, "application/octet-stream")
        with open(filepath, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # stdlib logs to stderr with ugly format; make it greppable.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        print(f"[admin] {self.address_string()} {format % args}", flush=True)


# --------------------------------------------------------------------- helpers


def _render_file_list(share: shares.Share) -> str:
    if not share.files:
        return '<p class="muted">No files uploaded yet.</p>'
    items = []
    for fname in share.files:
        items.append(
            "<li>"
            f'<span class="filename">{html.escape(fname)}</span>'
            f'<form method="POST" action="/shares/{html.escape(share.id)}/delete-file" '
            f'class="inline" onsubmit="return confirm(\'Remove this file?\');">'
            f'<input type="hidden" name="filename" value="{html.escape(fname)}">'
            f'<button class="btn btn-sm btn-danger" '
            f'{"disabled" if share.status in ("running", "starting") else ""}>Remove</button>'
            "</form></li>"
        )
    return '<ul class="filelist">' + "".join(items) + "</ul>"


def _render_received_list(share: shares.Share) -> str:
    if not os.path.isdir(share.data_dir):
        return '<p class="muted">No files received yet.</p>'
    entries: list[str] = []
    # onionshare writes each upload into its own subdirectory, so we
    # walk the full tree and show relative paths.
    for root, _dirs, files in os.walk(share.data_dir):
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), share.data_dir)
            entries.append(rel)
    if not entries:
        return '<p class="muted">No files received yet.</p>'
    entries.sort()
    return (
        '<ul class="filelist">'
        + "".join(f"<li>{html.escape(e)}</li>" for e in entries)
        + "</ul>"
    )


def _start_stop_button_html(share: shares.Share) -> str:
    sid = html.escape(share.id)
    if share.status == "running" or share.status == "starting":
        return (
            f'<form method="POST" action="/shares/{sid}/stop" class="inline">'
            '<button class="btn btn-primary">Stop</button></form>'
        )
    return (
        f'<form method="POST" action="/shares/{sid}/start" class="inline">'
        '<button class="btn btn-primary">Start</button></form>'
    )


# --------------------------------------------------------------------- main


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Threaded server so long uploads don't block other requests."""

    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    server = ThreadedTCPServer(("0.0.0.0", ADMIN_PORT), AdminHandler)
    print(f"[admin] listening on :{ADMIN_PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
