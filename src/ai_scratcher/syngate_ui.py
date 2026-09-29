# Open Trader
# Copyright (c) 2026 l2xl (l2xl/at/proton.me)
# Distributed under the Intellectual Property Reserve License, v2 (IPRL)

"""Local Synergy Context Gate (syngate) tree editor: a loopback web UI over syngatelib.

`syngate ui` (or `python scripts/syngate_ui.py`) serves a single-page editor for the
tree under `syngate/`: the DAG with live status rollup, per-field item edits
(autosave) and drag-and-drop placement through the canonical writer, and
review / clear runs driven through `syngate.py` subprocesses with output streamed
to the browser over SSE. Stamping stays in the CLI code path, so the browser
button and the terminal command are the same user action.

The server binds 127.0.0.1 only. Every request must carry the per-session
token from the URL printed at startup (`X-Syngate-Token` header or `?token=`),
and the Host header must be a loopback name -- the jupyter-style defense
against CSRF and DNS rebinding for localhost tools that run subprocesses.
"""

import argparse
import hashlib
import hmac
import http.server
import json
import re
import secrets
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import syngatelib
from . import synthetic

DEFAULT_PORT = 8712
DEFAULT_BUILD_DIR = "cmake-build-debug-clang"
# UIDs and the glob patterns syngate.py accepts for batch review/clear.
RUN_UID_RE = re.compile(r"^[A-Za-z0-9_\-?*\[\]!]+$")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
# Coverage files picked up automatically when none are given explicitly.
DEFAULT_COVERAGE = ("pytest-coverage.jsonl", "syngate_coverage.jsonl", "build-ci/syngate_coverage.jsonl")
# Test / review runs started from the page fold their records in here, so a run recolors the statuses.
RUN_COVERAGE = "syngate_coverage.jsonl"
# Project files opened from a link: git-tracked files are readable, only markdown is written.
FILE_LIMIT = 1 << 20


class ApiError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


def _call(run):
    """Run a core tree call, mapping its refusal onto the HTTP answer: an
    unknown item is 404, a lost compare-and-swap 409, anything else a 400."""
    try:
        return run()
    except syngatelib.stale_edit as err:
        raise ApiError(409, str(err), current=err.current) from None
    except syngatelib.unknown_uid as err:
        raise ApiError(404, str(err)) from None
    except syngatelib.tree_error as err:
        raise ApiError(400, str(err)) from None


class Job:
    def __init__(self, job_id, argv):
        self.id = job_id
        self.argv = argv
        self.lines = []
        self.returncode = None
        self.proc = None
        self.cond = threading.Condition()

    def snapshot(self):
        with self.cond:
            return {"id": self.id, "argv": list(self.argv), "running": self.returncode is None,
                    "returncode": self.returncode, "lines": list(self.lines)}

    def stream(self):
        """Yield output lines as they arrive; return the exit code when done."""
        index = 0
        while True:
            with self.cond:
                while index >= len(self.lines) and self.returncode is None:
                    self.cond.wait(0.2)
                fresh = self.lines[index:]
                index += len(fresh)
                done = self.returncode if index >= len(self.lines) else None
            yield from fresh
            if done is not None:
                return done


class JobRunner:
    """Single-flight subprocess runner: review runs stamp files and share the
    build tree, so exactly one job may be live at a time."""

    def __init__(self):
        self._lock = threading.Lock()
        self._counter = 0
        self.current = None

    def start(self, argv, cwd):
        with self._lock:
            if self.current and self.current.returncode is None:
                raise ApiError(409, f"a run is already in progress (job {self.current.id})")
            self._counter += 1
            job = Job(self._counter, argv)
            try:
                job.proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True, bufsize=1)
            except OSError as exc:
                raise ApiError(500, f"failed to start {argv[0]}: {exc}")
            self.current = job
        threading.Thread(target=self._pump, args=(job,), daemon=True).start()
        return job

    def _pump(self, job):
        for line in job.proc.stdout:
            with job.cond:
                job.lines.append(line.rstrip("\n"))
                job.cond.notify_all()
        job.proc.wait()
        with job.cond:
            job.returncode = job.proc.returncode
            job.cond.notify_all()

    def get(self, job_id):
        job = self.current
        return job if job and job.id == job_id else None

    def cancel(self):
        job = self.current
        if not job:
            return
        if job.returncode is None and job.proc:
            job.proc.terminate()
            try:
                job.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                job.proc.kill()
        with job.cond:
            while job.returncode is None:
                job.cond.wait(0.2)


class SyngateUIApp:
    def __init__(self, root=None, syngate_dir=None, coverage=(), cli_prefix=None, build_dir=DEFAULT_BUILD_DIR):
        self.root = syngatelib.find_root(root)
        self.syngate_dir = Path(syngate_dir) if syngate_dir else self.root / "syngate"
        self.run_coverage = str(self.root / RUN_COVERAGE)
        self.coverage = list(dict.fromkeys([*(str(p) for p in coverage), self.run_coverage]))
        self.cli_prefix = cli_prefix or [sys.executable, "-m", "ai_scratcher.syngate", "--root", str(self.root)]
        self.build_dir = build_dir
        self.token = secrets.token_urlsafe(24)
        self.jobs = JobRunner()
        self.connectors = {name: connector(cwd=self.root) for name, connector in synthetic.CONNECTORS.items()}

    # -- model ------------------------------------------------------------

    def _coverage_files(self):
        return [p for p in self.coverage if Path(p).is_file()]

    def fingerprint(self):
        stat = hashlib.sha256()
        paths = sorted(self.syngate_dir.rglob("*.yml")) + [Path(p) for p in self._coverage_files()]
        for path in paths:
            try:
                meta = path.stat()
            except OSError:
                continue
            stat.update(f"{path}\0{meta.st_mtime_ns}\0{meta.st_size}\n".encode())
        return stat.hexdigest()

    def build_model(self):
        items, load_errors = syngatelib.load_tree(self.syngate_dir)
        discovered = syngatelib.discover_bindings(self.root)
        records, coverage_errors = syngatelib.load_coverage(self._coverage_files())
        problems = syngatelib.item_problems(items, discovered)
        report = syngatelib.compute_status(items, records, problems)
        axes = syngatelib.compute_axes(items, records, problems)
        tree_errors = [message for uid, message in syngatelib.layout_problems(items) if uid is None]

        payload = {}
        for uid, entry in report.items():
            item = items[uid]
            bindings = None
            if item.is_leaf:
                bindings = [{
                    "name": name or "",
                    "sha": sha,
                    "locations": [{"path": loc.path, "line": loc.line, "name": loc.name}
                                  for loc in discovered.get((uid, name), [])],
                    "records": len(records.get((uid, name), [])),
                } for name, sha in item.tests.items()]
            payload[uid] = dict(entry, **axes[uid],
                                is_leaf=item.is_leaf,
                                description_raw=item.description,
                                path=str(item.path.relative_to(self.root)) if item.path.is_relative_to(self.root) else str(item.path),
                                stamp_fresh=(syngatelib.item_stamp(items, uid) == item.reviewed) if item.reviewed else None,
                                bindings=bindings)

        roots = sorted(uid for uid, item in items.items() if not item.parents)
        reachable, queue = set(), list(roots)
        while queue:
            uid = queue.pop()
            if uid in reachable:
                continue
            reachable.add(uid)
            queue.extend(payload[uid]["children"])
        counts = {}
        for entry in payload.values():
            counts[entry["test"]] = counts.get(entry["test"], 0) + 1
        job = self.jobs.current
        return {
            "fingerprint": self.fingerprint(),
            "items": payload,
            "roots": roots,
            "unreachable": sorted(set(payload) - reachable),
            "counts": counts,
            "load_errors": load_errors + tree_errors,
            "coverage": {"files": self._coverage_files(), "errors": coverage_errors},
            "build_dir": self.build_dir,
            "features": syngatelib.features(items),
            "connectors": {name: connector.settings() for name, connector in self.connectors.items()},
            "job": job.id if job and job.returncode is None else None,
        }

    # -- mutations --------------------------------------------------------

    def _load(self):
        items, _ = syngatelib.load_tree(self.syngate_dir)
        return items

    # Every mutation from the page is the user's own act, so it passes `clear`:
    # the stamps it moves are dropped, where an AI-proxied call leaves them
    # standing over changed content and the review reads as violated.

    def save_item(self, uid, data):
        """Partial update through the core edit call; `base` carries the values
        the editor started from, so an autosave never silently overwrites an
        edit made on disk (agent, git, IDE) since the page loaded the item."""
        items = self._load()
        stored = _call(lambda: syngatelib.edit_item(items, uid, data, data.get("base"), clear=True))
        item = items[uid]
        return {"ok": True, "stamp_fresh": (syngatelib.item_stamp(items, uid) == item.reviewed) if item.reviewed else None, "stored": stored}

    def move_item(self, uid, data):
        items = self._load()
        written = _call(lambda: syngatelib.move_item(items, uid, data.get("to"), before=data.get("before"),
                                                     source=data.get("from"), link=bool(data.get("link")), clear=True))
        return {"ok": True, "written": written}

    def create_item(self, data):
        items = self._load()
        item = _call(lambda: syngatelib.add_item(items, str(data.get("uid") or ""), data.get("parents") or [],
                                                 header=data.get("header"), description=data.get("description"),
                                                 kind=data.get("kind"), folder=data.get("dir"),
                                                 order=data.get("order", 0), syngate_dir=self.syngate_dir, clear=True))
        return {"ok": True, "path": str(item.path.relative_to(self.root))}

    def delete_item(self, uid):
        items = self._load()
        _call(lambda: syngatelib.delete_item(items, uid, clear=True))
        return {"ok": True}

    # -- linked project files ---------------------------------------------

    def _file_at(self, rel):
        """(absolute path, writable) for a normalised project-relative path;
        secrets sit untracked next to the tree (keys, databases), so anything
        outside git is served only when it is markdown."""
        if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts or rel != Path(rel).as_posix() or rel.startswith("-"):
            raise ApiError(400, f"'{rel}' is not a normalised project-relative path")
        path = (self.root / rel).resolve()
        if not path.is_relative_to(self.root.resolve()) or not path.is_file():
            raise ApiError(404, f"no file {rel}")
        writable = path.suffix == ".md"
        if not writable and not self._tracked(rel):
            raise ApiError(403, f"{rel} is neither tracked by git nor markdown")
        return path, writable

    def _tracked(self, rel):
        try:
            return subprocess.run(["git", "ls-files", "--error-unmatch", "--", rel], cwd=self.root, capture_output=True).returncode == 0
        except OSError:
            return False

    @staticmethod
    def _text(path, rel):
        if path.stat().st_size > FILE_LIMIT:
            raise ApiError(413, f"{rel} exceeds {FILE_LIMIT >> 10} KiB")
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ApiError(415, f"{rel} is not UTF-8 text")

    def read_file(self, rel):
        path, writable = self._file_at(rel)
        text = self._text(path, rel)
        return {"path": rel, "text": text, "sha": hashlib.sha256(text.encode()).hexdigest(), "writable": writable}

    def file_shas(self, rels):
        shas = {}
        for rel in rels:
            try:
                shas[rel] = self.read_file(rel)["sha"]
            except ApiError:
                shas[rel] = None
        return shas

    def save_file(self, data):
        """Whole-file compare-and-swap against the text the page loaded, the
        file twin of `save_item`."""
        rel = str(data.get("path") or "")
        path, writable = self._file_at(rel)
        if not writable:
            raise ApiError(403, f"{rel}: only markdown is written from the page")
        text, base, before = str(data.get("text") or ""), data.get("base"), self._text(path, rel)
        if isinstance(base, str) and before != base and before != text:
            raise ApiError(409, f"{rel} changed on disk since the page loaded it", current={"file": before})
        if text != before:
            path.write_text(text, encoding="utf-8")
        return {"ok": True, "stored": {"file": text}, "sha": hashlib.sha256(text.encode()).hexdigest()}

    def chat(self, data):
        """One exchange anchored at `uid`: the item's seed context plus the user's text go to the chosen connector.
        A plain answer comes back as the reply; an answer of tree calls is applied to the tree and the reply lists what was applied."""
        uid = data.get("uid")
        try:
            reply, session = synthetic.query(self.connectors, self._load(), uid, str(data.get("text") or ""), data.get("connector"),
                                             data.get("model"), data.get("effort"), modes=data.get("modes") or (), session=data.get("session"))
        except synthetic.query_error as err:
            raise ApiError(400, str(err)) from None
        except synthetic.connection_error as err:
            raise ApiError(502, str(err)) from None
        try:
            kind, payload = synthetic.parse_reply(reply)
        except synthetic.protocol_error as err:
            raise ApiError(422, f"answer refused: {err}", session=session) from None
        if kind == "chat":
            return {"ok": True, "reply": reply, "session": session}
        items, applied = self._load(), []
        for call in payload:
            try:
                applied.append(_call(lambda: synthetic.apply_call(items, uid, call, syngate_dir=self.syngate_dir)))
            except ApiError as err:
                named = uid if call["uid"] == "@" else call["uid"]
                raise ApiError(err.status, "\n".join([*applied, f"{call['tool']} {named} refused: {err}"]), session=session) from None
        return {"ok": True, "reply": "\n".join(applied), "session": session, "calls": payload}

    def start_run(self, data):
        action = data.get("action")
        if action not in ("test", "review", "clear"):
            raise ApiError(400, "'action' must be 'test', 'review' or 'clear'")
        uids = data.get("uids")
        if not isinstance(uids, list) or not uids or not all(isinstance(u, str) and RUN_UID_RE.match(u) for u in uids):
            raise ApiError(400, "'uids' must be a non-empty list of UIDs or glob patterns")
        items = self._load()
        if action != "test" and not syngatelib.features(items)["review"]:
            raise ApiError(400, str(syngatelib.feature_off("review")))
        argv = [*self.cli_prefix, action]
        if action == "clear":
            argv += uids
        else:
            leaves = list(dict.fromkeys(leaf for uid in uids for leaf in syngatelib.leaves_under(items, uid)))
            if not leaves:
                raise ApiError(400, f"no test-bearing leaf under {', '.join(uids)}")
            argv += [*leaves, "--build-dir", str(data.get("build_dir") or self.build_dir), "--coverage-out", self.run_coverage]
        job = self.jobs.start(argv, cwd=self.root)
        return {"job": job.id}


def _page_bytes(nonce):
    page = (Path(__file__).resolve().parent / "syngate_ui.html").read_bytes()
    return page.replace(b"<script>", b'<script nonce="' + nonce + b'">').replace(b"<style>", b'<style nonce="' + nonce + b'">')


def _csp(nonce):
    return (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src-elem 'nonce-{nonce}'; style-src-attr 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'")


# Served without the token: browsers request it on their own, and it reveals nothing.
FAVICON = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect width="16" height="16" rx="3" fill="#2e5c6e"/>'
           b'<path d="M5 5.5v5M6 5.2l4 1.3M6 10.8l4-1.3" stroke="#fff" stroke-width="1.1" fill="none"/>'
           b'<circle cx="5" cy="4.5" r="1.7" fill="#d9a63d"/><circle cx="5" cy="11.5" r="1.7" fill="#7a7f87"/>'
           b'<circle cx="11" cy="7.2" r="1.7" fill="#3f7d52"/><circle cx="11" cy="9.8" r="1.7" fill="#a8482e"/></svg>')


class _Handler(http.server.BaseHTTPRequestHandler):
    app = None  # bound by make_server
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # keep the terminal for syngate.py output
        pass

    # -- plumbing ---------------------------------------------------------

    def _send(self, status, content_type, body, extra=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, "application/json; charset=utf-8", json.dumps(obj).encode())

    def _authorized(self):
        host = urlsplit("//" + (self.headers.get("Host") or "")).hostname
        if host not in LOOPBACK_HOSTS:
            self._json(403, {"error": "requests must address a loopback host"})
            return False
        query = parse_qs(urlsplit(self.path).query)
        supplied = self.headers.get("X-Syngate-Token") or (query.get("token") or [""])[0]
        if not hmac.compare_digest(supplied, self.app.token):
            self._json(401, {"error": "missing or invalid session token"})
            return False
        return True

    def _payload(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            raise ApiError(400, "request body must be JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "request body must be a JSON object")
        return data

    # -- routes -----------------------------------------------------------

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/favicon.ico":
            self._send(200, "image/svg+xml", FAVICON, extra=[("Cache-Control", "max-age=86400")])
            return
        if not self._authorized():
            return
        try:
            if path == "/":
                nonce = secrets.token_urlsafe(16)
                self._send(200, "text/html; charset=utf-8", _page_bytes(nonce.encode()), extra=[("Content-Security-Policy", _csp(nonce))])
            elif path == "/api/tree":
                self._json(200, self.app.build_model())
            elif path == "/api/fingerprint":
                job = self.app.jobs.current
                self._json(200, {"fingerprint": self.app.fingerprint(),
                                 "job": job.id if job and job.returncode is None else None,
                                 "files": self.app.file_shas(parse_qs(urlsplit(self.path).query).get("file", []))})
            elif path == "/api/file":
                self._json(200, self.app.read_file((parse_qs(urlsplit(self.path).query).get("path") or [""])[0]))
            elif (match := re.fullmatch(r"/api/job/(\d+)", path)):
                self._job_snapshot(int(match.group(1)))
            elif (match := re.fullmatch(r"/api/job/(\d+)/events", path)):
                self._job_events(int(match.group(1)))
            else:
                self._json(404, {"error": f"no route for GET {path}"})
        except ApiError as exc:
            self._json(exc.status, {"error": str(exc), **exc.extra})
        except BrokenPipeError:
            pass

    def do_POST(self):
        if not self._authorized():
            return
        path = urlsplit(self.path).path
        try:
            payload = self._payload()
            if (match := re.fullmatch(r"/api/item/([A-Za-z0-9_\-]+)", path)):
                self._json(200, self.app.save_item(match.group(1), payload))
            elif (match := re.fullmatch(r"/api/move/([A-Za-z0-9_\-]+)", path)):
                self._json(200, self.app.move_item(match.group(1), payload))
            elif path == "/api/new":
                self._json(200, self.app.create_item(payload))
            elif path == "/api/file":
                self._json(200, self.app.save_file(payload))
            elif (match := re.fullmatch(r"/api/delete/([A-Za-z0-9_\-]+)", path)):
                self._json(200, self.app.delete_item(match.group(1)))
            elif path == "/api/run":
                self._json(200, self.app.start_run(payload))
            elif path == "/api/chat":
                self._json(200, self.app.chat(payload))
            else:
                self._json(404, {"error": f"no route for POST {path}"})
        except ApiError as exc:
            self._json(exc.status, {"error": str(exc), **exc.extra})

    def _job_snapshot(self, job_id):
        job = self.app.jobs.get(job_id)
        if job is None:
            raise ApiError(404, f"no such job {job_id}")
        self._json(200, job.snapshot())

    def _job_events(self, job_id):
        job = self.app.jobs.get(job_id)
        if job is None:
            raise ApiError(404, f"no such job {job_id}")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        stream = job.stream()
        returncode = None
        while True:
            try:
                line = next(stream)
            except StopIteration as stop:
                returncode = stop.value
                break
            self.wfile.write(f"data: {line}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(f"event: done\ndata: {returncode}\n\n".encode())
        self.wfile.flush()


def make_server(app, port=DEFAULT_PORT):
    handler = type("BoundHandler", (_Handler,), {"app": app})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def serve(port=DEFAULT_PORT, coverage=(), build_dir=DEFAULT_BUILD_DIR, open_browser=True, root=None):
    root = syngatelib.find_root(root)
    coverage = list(coverage) or [str(Path(root) / name) for name in DEFAULT_COVERAGE if (Path(root) / name).is_file()]
    app = SyngateUIApp(root=root, coverage=coverage, build_dir=build_dir)
    try:
        server = make_server(app, port)
    except OSError as exc:
        print(f"syngate ui: cannot bind 127.0.0.1:{port}: {exc}", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}/?token={app.token}"
    print(f"syngate ui: serving the syngate editor at {url}", flush=True)
    if coverage:
        print(f"syngate ui: coloring statuses from {', '.join(coverage)}")
    else:
        print("syngate ui: no coverage files found; leaf statuses show as not implemented until tests are run from the page (or pass --coverage FILE)")
    print("syngate ui: loopback only; the URL token is this session's key. Ctrl+C stops the server.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nsyngate ui: stopped")
    finally:
        app.jobs.cancel()
        server.server_close()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"listen port on 127.0.0.1 (default {DEFAULT_PORT}, 0 = ephemeral)")
    parser.add_argument("--coverage", action="append", default=[], help="syngate_coverage.jsonl file(s) to color statuses; repeatable (default: well-known local files)")
    parser.add_argument("--build-dir", default=DEFAULT_BUILD_DIR, help="build tree containing the Catch2 test binaries for review runs")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    parser.add_argument("--root", help="managed project root (default: SYNGATE_ROOT, else the nearest ancestor of the working directory holding syngate/)")
    args = parser.parse_args()
    return serve(port=args.port, coverage=args.coverage, build_dir=args.build_dir, open_browser=not args.no_browser, root=args.root)


if __name__ == "__main__":
    sys.exit(main())
