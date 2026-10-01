# Open Trader
# Copyright (c) 2026 l2xl (l2xl/at/proton.me)
# Distributed under the Intellectual Property Reserve License, v2 (IPRL)

"""Local syngate editor UI: loopback HTTP server over syngatelib.

The app serves the tree model as JSON, writes per-field item edits and
drag-and-drop placements through the canonical writer, scaffolds and deletes
items, and drives review/clear through the CLI as a streamed subprocess -- all
gated by a per-session token and a loopback Host check."""

import fcntl
import http.client
import json
import os
import select
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest
import yaml

import syngate_ui
import syngatelib
import synthetic

# Stub CLI: echoes its argv tail and exits with the code named by an rc=N
# argument (charset-valid as a UID pattern), standing in for `syngate.py review/clear` runs.
STUB = [sys.executable, "-c",
        "import sys; print('stub:', *sys.argv[1:]); rc = [a for a in sys.argv if a.startswith('rc-')]; sys.exit(int(rc[0][3:]) if rc else 0)"]


@pytest.fixture
def app(tmp_path, syngate_tree):
    syngate_dir, make_item = syngate_tree
    make_item(syngate_dir, "ROOT", "Root branch\n", parents=())
    make_item(syngate_dir, "LEAF-001", "It shall leaf.\n", parents=("ROOT",), header="First leaf", tests=None)
    return syngate_ui.SyngateUIApp(root=tmp_path, syngate_dir=syngate_dir, cli_prefix=STUB)


@pytest.fixture
def server(app):
    httpd = syngate_ui.make_server(app, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", app
    httpd.shutdown()
    thread.join()


class Cdp:
    """Headless Chrome over --remote-debugging-pipe: NUL-delimited JSON on fds 3 (in) / 4 (out)."""

    def __init__(self, binary, profile):
        to_chrome, from_chrome = os.pipe(), os.pipe()

        def wire():
            source, sink = fcntl.fcntl(to_chrome[0], fcntl.F_DUPFD, 10), fcntl.fcntl(from_chrome[1], fcntl.F_DUPFD, 10)
            os.dup2(source, 3)
            os.dup2(sink, 4)

        argv = [binary, "--headless=new", "--remote-debugging-pipe", f"--user-data-dir={profile}", "--no-first-run", "--disable-gpu"]
        if os.geteuid() == 0:
            argv.append("--no-sandbox")
        self.process = subprocess.Popen(argv, preexec_fn=wire, pass_fds=(3, 4), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(to_chrome[0])
        os.close(from_chrome[1])
        self.sink, self.source, self.buffer, self.serial = to_chrome[1], from_chrome[0], b"", 0

    def call(self, method, params=None, session=None):
        self.serial += 1
        message = {"id": self.serial, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        os.write(self.sink, json.dumps(message).encode() + b"\0")
        while True:
            while b"\0" not in self.buffer:
                if not select.select([self.source], [], [], 20)[0]:
                    raise TimeoutError(f"no CDP reply to {method}")
                self.buffer += os.read(self.source, 1 << 16)
            raw, self.buffer = self.buffer.split(b"\0", 1)
            reply = json.loads(raw)
            if reply.get("id") == self.serial:
                if "error" in reply:
                    raise RuntimeError(reply["error"])
                return reply["result"]

    def close(self):
        self.process.kill()
        self.process.wait()
        os.close(self.sink)
        os.close(self.source)


class Page:
    def __init__(self, cdp, url):
        self.cdp = cdp
        target = cdp.call("Target.createTarget", {"url": url})["targetId"]
        self.session = cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]

    def eval(self, script):
        result = self.cdp.call("Runtime.evaluate", {"expression": script, "awaitPromise": True, "returnByValue": True}, self.session)
        assert "exceptionDetails" not in result, result["exceptionDetails"]
        return result["result"].get("value")

    def mouse(self, kind, x, y, clicks=1):
        self.cdp.call("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": clicks}, self.session)

    def wait(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while not self.eval(f"Boolean({predicate})"):
            assert time.monotonic() < deadline, f"page never reached: {predicate}"
            time.sleep(0.05)


@pytest.fixture(scope="session")
def chrome(tmp_path_factory):
    binary = next(filter(None, map(shutil.which, ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"))), None)
    if binary is None:
        pytest.skip("no headless Chrome")
    cdp = Cdp(binary, tmp_path_factory.mktemp("chrome-profile"))
    yield cdp
    cdp.close()


@pytest.fixture
def page(server, chrome):
    base, app = server
    opened = Page(chrome, f"{base}/?token={app.token}")
    opened.wait("document.querySelector('#tree [data-path]')")
    return opened


def call(base, app, path, payload=None, token=True, raw=False):
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method="POST" if payload is not None else "GET",
    )
    if token:
        request.add_header("X-Syngate-Token", app.token if token is True else token)
    with urllib.request.urlopen(request) as response:
        body = response.read()
        return response.status, body if raw else json.loads(body)


@pytest.mark.syngate("SYNGATE_UI-031", "model")
def test_tree_endpoint_serves_statuses_and_stamp_freshness(server, syngate_tree):
    base, app = server
    syngate_dir, make_item = syngate_tree
    stale = syngatelib.Item(uid="STALE-001", path=syngate_dir / "STALE-001.yml", header="h", description="It shall drift.\n", parents=["ROOT"], tests={None: "b" * 64})
    make_item(syngate_dir, "STALE-001", "It shall drift.\n", parents=("ROOT",), header="h", tests="b" * 64, reviewed=syngatelib.compute_stamp(stale))
    (syngate_dir / "STALE-001.yml").write_text((syngate_dir / "STALE-001.yml").read_text().replace("It shall drift.", "It shall have drifted."))
    status, tree = call(base, app, "/api/tree")
    assert status == 200
    assert tree["roots"] == ["ROOT"]
    assert tree["items"]["ROOT"]["children"] == ["LEAF-001", "STALE-001"]
    leaf = tree["items"]["LEAF-001"]
    assert leaf["is_leaf"] and leaf["status"] == "not_implemented" and leaf["stamp_fresh"] is None
    assert tree["items"]["STALE-001"]["stamp_fresh"] is False
    assert any("re-review" in p for p in tree["items"]["STALE-001"]["problems"])


@pytest.mark.syngate("SYNGATE_UI-010")
def test_requests_without_token_or_loopback_host_are_refused(server):
    base, app = server
    with pytest.raises(urllib.error.HTTPError) as missing:
        call(base, app, "/api/tree", token=False)
    assert missing.value.code == 401
    with pytest.raises(urllib.error.HTTPError) as forged:
        call(base, app, "/api/tree", token="forged")
    assert forged.value.code == 401
    port = int(base.rsplit(":", 1)[1])
    conn = http.client.HTTPConnection("127.0.0.1", port)  # DNS-rebinding defense: loopback Host names only
    conn.request("GET", "/api/tree", headers={"Host": "evil.example", "X-Syngate-Token": app.token})
    assert conn.getresponse().status == 403
    conn.close()
    status, _ = call(base, app, f"/api/tree?token={app.token}", token=False)
    assert status == 200  # EventSource cannot set headers; the query token is equivalent


@pytest.mark.syngate("SYNGATE_UI-039")
def test_edit_of_a_stamped_field_clears_the_review(server, syngate_tree):
    base, app = server
    syngate_dir, make_item = syngate_tree
    frozen = syngatelib.Item(uid="LEAF-002", path=syngate_dir / "LEAF-002.yml", header="Frozen", description="It shall freeze.\n", parents=["ROOT"], tests={None: "a" * 64})
    frozen.reviewed = syngatelib.compute_stamp(frozen)
    syngatelib.write_item(frozen)
    status, result = call(base, app, "/api/item/LEAF-002", {"order": 30})
    assert status == 200 and result["stamp_fresh"] is True  # order sits outside the stamp
    status, result = call(base, app, "/api/item/LEAF-002", {"header": "Frozen v2", "description": "It shall freeze harder."})
    assert status == 200 and result["stamp_fresh"] is None
    expected = syngatelib.Item(uid="LEAF-002", path=frozen.path, header="Frozen v2", description="It shall freeze harder.\n",
                           parents=["ROOT"], order=30, tests={None: None})
    assert (syngate_dir / "LEAF-002.yml").read_text() == syngatelib.dump_item(expected)  # canonical writer, stamp and sha dropped
    make_item(syngate_dir, "BRANCH-A", "Groups.\n", parents=("ROOT",))
    syngatelib.write_item(frozen)
    call(base, app, "/api/move/LEAF-002", {"from": "ROOT", "to": "BRANCH-A", "before": None})
    moved = yaml.safe_load((syngate_dir / "LEAF-002.yml").read_text())
    assert moved["parents"] == ["BRANCH-A"] and "review" not in moved and moved["test"] is None


@pytest.mark.syngate("SYNGATE_UI-032", "guard")
def test_save_rejects_unknown_parent_and_cycle(server, syngate_tree):
    base, app = server
    syngate_dir, _ = syngate_tree
    before = (syngate_dir / "ROOT.yml").read_text()
    for parents in (["NOPE"], ["LEAF-001"]):  # unknown parent; child-of-own-child cycle
        with pytest.raises(urllib.error.HTTPError) as denied:
            call(base, app, "/api/item/ROOT", {"header": "", "description": "Root branch\n", "parents": parents, "order": 0, "tests": None})
        assert denied.value.code == 400
    assert (syngate_dir / "ROOT.yml").read_text() == before


@pytest.mark.syngate("SYNGATE_UI-032", "cas")
def test_partial_save_touches_only_named_fields_and_refuses_to_clobber_disk_edits(server, syngate_tree):
    base, app = server
    syngate_dir, _ = syngate_tree
    path = syngate_dir / "LEAF-001.yml"
    status, result = call(base, app, "/api/item/LEAF-001", {"header": "Typed", "base": {"header": "First leaf"}})
    assert status == 200 and result["stored"]["header"] == "Typed"
    on_disk = yaml.safe_load(path.read_text())
    assert on_disk == {"header": "Typed", "description": "It shall leaf.\n", "parents": ["ROOT"], "test": None}
    path.write_text(path.read_text().replace("It shall leaf.", "It shall leaf, said the agent."))
    status, _ = call(base, app, "/api/item/LEAF-001", {"header": "Typed more", "base": {"header": "Typed"}})
    assert status == 200  # an on-disk edit of another field rebases silently
    assert yaml.safe_load(path.read_text())["description"] == "It shall leaf, said the agent.\n"
    with pytest.raises(urllib.error.HTTPError) as clash:
        call(base, app, "/api/item/LEAF-001", {"description": "It shall leaf, said the user.", "base": {"description": "It shall leaf.\n"}})
    assert clash.value.code == 409
    assert json.loads(clash.value.read())["current"] == {"description": "It shall leaf, said the agent.\n"}
    assert yaml.safe_load(path.read_text())["description"] == "It shall leaf, said the agent.\n"


def _orders(syngate_dir, *uids):
    return [yaml.safe_load((syngate_dir / f"{uid}.yml").read_text()).get("order", 0) for uid in uids]


@pytest.mark.syngate("SYNGATE_UI-033", "reorder")
def test_move_takes_a_free_order_key_else_renumbers_the_family(server, syngate_tree):
    base, app = server
    syngate_dir, make_item = syngate_tree
    for uid, order in (("KID-A", 10), ("KID-B", 11), ("KID-C", 30)):
        make_item(syngate_dir, uid, "It shall sort.\n", parents=("ROOT",), order=order, tests=None)
    _, moved = call(base, app, "/api/move/LEAF-001", {"from": "ROOT", "to": "ROOT", "before": "KID-C"})
    assert moved["written"] == ["LEAF-001"] and _orders(syngate_dir, "LEAF-001") == [20]
    _, tree = call(base, app, "/api/tree")
    assert tree["items"]["ROOT"]["children"] == ["KID-A", "KID-B", "LEAF-001", "KID-C"]
    _, unmoved = call(base, app, "/api/move/LEAF-001", {"from": "ROOT", "to": "ROOT", "before": "KID-C"})
    assert unmoved["written"] == []
    _, moved = call(base, app, "/api/move/KID-C", {"from": "ROOT", "to": "ROOT", "before": "KID-B"})  # no integer between 10 and 11
    assert moved["written"] == ["KID-B", "KID-C", "LEAF-001"]
    assert _orders(syngate_dir, "KID-A", "KID-C", "KID-B", "LEAF-001") == [10, 20, 30, 40]


@pytest.mark.syngate("SYNGATE_UI-033", "reparent")
def test_move_repoints_or_adds_the_parent_link_and_guards_the_dag(server, syngate_tree):
    base, app = server
    syngate_dir, make_item = syngate_tree
    make_item(syngate_dir, "BRANCH-A", "Groups.\n", parents=("ROOT",))
    make_item(syngate_dir, "BRANCH-B", "Groups too.\n", parents=("BRANCH-A",))
    call(base, app, "/api/move/LEAF-001", {"from": "ROOT", "to": "BRANCH-A", "before": "BRANCH-B"})
    _, tree = call(base, app, "/api/tree")
    assert tree["items"]["LEAF-001"]["parents"] == ["BRANCH-A"]
    assert tree["items"]["BRANCH-A"]["children"] == ["LEAF-001", "BRANCH-B"]
    call(base, app, "/api/move/LEAF-001", {"from": "BRANCH-A", "to": "BRANCH-B", "before": None, "link": True})
    _, tree = call(base, app, "/api/tree")
    assert tree["items"]["LEAF-001"]["parents"] == ["BRANCH-A", "BRANCH-B"]
    before = (syngate_dir / "BRANCH-A.yml").read_text()
    for target in ("BRANCH-B", "LEAF-001"):  # under its own descendant; under a test-bearing leaf
        with pytest.raises(urllib.error.HTTPError) as denied:
            call(base, app, "/api/move/BRANCH-A", {"from": "ROOT", "to": target, "before": None})
        assert denied.value.code == 400
    assert (syngate_dir / "BRANCH-A.yml").read_text() == before


@pytest.mark.syngate("SYNGATE_UI-034")
def test_new_and_delete_manage_item_files(server, syngate_tree):
    base, app = server
    syngate_dir, _ = syngate_tree
    status, _ = call(base, app, "/api/new", {"uid": "LEAF-003", "parents": ["ROOT"], "dir": "syngate/sub", "kind": "leaf"})
    assert status == 200
    created = yaml.safe_load((syngate_dir / "sub" / "LEAF-003.yml").read_text())
    assert created["parents"] == ["ROOT"] and created["test"] is None
    with pytest.raises(urllib.error.HTTPError) as duplicate:
        call(base, app, "/api/new", {"uid": "LEAF-003", "parents": ["ROOT"]})
    assert duplicate.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as populated:
        call(base, app, "/api/delete/ROOT", {})
    assert populated.value.code == 400  # has children
    status, _ = call(base, app, "/api/delete/LEAF-003", {})
    assert status == 200 and not (syngate_dir / "sub" / "LEAF-003.yml").exists()


@pytest.mark.syngate("SYNGATE_UI-035", "stream")
def test_review_runs_cli_and_streams_output_until_exit(server):
    base, app = server
    status, job = call(base, app, "/api/run", {"action": "review", "uids": ["rc-3", "LEAF-001"]})
    assert status == 200
    status, body = call(base, app, f"/api/job/{job['job']}/events?token={app.token}", token=False, raw=True)
    text = body.decode().replace("\r", "")
    assert "stub: review rc-3 LEAF-001" in text
    assert "event: done\ndata: 3\n" in text
    status, snapshot = call(base, app, f"/api/job/{job['job']}")
    assert snapshot["returncode"] == 3 and snapshot["running"] is False
    assert "--build-dir" in snapshot["argv"]  # review forwards the build dir


@pytest.mark.syngate("SYNGATE_UI-035", "single_flight")
def test_concurrent_runs_are_refused_while_busy(server):
    base, app = server
    app.cli_prefix = [sys.executable, "-c", "import sys, time; print('held'); sys.stdout.flush(); time.sleep(30)"]
    _, job = call(base, app, "/api/run", {"action": "clear", "uids": ["LEAF-001"]})
    try:
        with pytest.raises(urllib.error.HTTPError) as busy:
            call(base, app, "/api/run", {"action": "clear", "uids": ["LEAF-001"]})
        assert busy.value.code == 409
    finally:
        app.jobs.cancel()
    status, snapshot = call(base, app, f"/api/job/{job['job']}")
    assert snapshot["running"] is False


@pytest.mark.syngate("SYNGATE_UI-035", "branch")
def test_a_branch_run_addresses_every_leaf_below_it(server, syngate_tree):
    base, app = server
    syngate_dir, make_item = syngate_tree
    make_item(syngate_dir, "BRANCH-A", "Groups.\n", parents=("ROOT",))
    make_item(syngate_dir, "BRANCH-B", "Groups nothing yet.\n", parents=("ROOT",))
    make_item(syngate_dir, "LEAF-002", "It shall nest.\n", parents=("BRANCH-A",), tests=None)
    _, job = call(base, app, "/api/run", {"action": "test", "uids": ["ROOT"]})
    _, body = call(base, app, f"/api/job/{job['job']}/events?token={app.token}", token=False, raw=True)
    assert f"stub: test LEAF-002 LEAF-001 --build-dir {app.build_dir} --coverage-out {app.run_coverage}" in body.decode()
    with pytest.raises(urllib.error.HTTPError) as barren:
        call(base, app, "/api/run", {"action": "review", "uids": ["BRANCH-B"]})
    assert barren.value.code == 400


@pytest.mark.syngate("SYNGATE_UI-036", "read")
def test_linked_files_are_served_only_from_inside_the_project(server, tmp_path_factory):
    base, app = server
    (app.root / "docs").mkdir()
    (app.root / "docs" / "GUIDE.md").write_text("# Guide\n")
    (app.root / "key.txt").write_text("secret\n")
    outside = tmp_path_factory.mktemp("outside") / "LEAK.md"
    outside.write_text("# Leak\n")
    (app.root / "docs" / "LINK.md").symlink_to(outside)
    status, served = call(base, app, "/api/file?path=docs/GUIDE.md")
    assert status == 200 and served["text"] == "# Guide\n" and served["writable"] is True
    _, state = call(base, app, "/api/fingerprint?file=docs/GUIDE.md&file=docs/GONE.md")
    assert state["files"] == {"docs/GUIDE.md": served["sha"], "docs/GONE.md": None}
    for path, code in (("key.txt", 403), ("../GUIDE.md", 400), ("docs/../key.txt", 400), ("docs/LINK.md", 404), ("docs/GONE.md", 404)):  # untracked non-markdown; traversal; symlink escape; missing
        with pytest.raises(urllib.error.HTTPError) as denied:
            call(base, app, f"/api/file?path={path}")
        assert denied.value.code == code, path


@pytest.mark.syngate("SYNGATE_UI-036", "save")
def test_file_save_is_a_compare_and_swap_limited_to_markdown(server):
    base, app = server
    guide = app.root / "GUIDE.md"
    guide.write_text("# Guide\n")
    (app.root / "notes.txt").write_text("plain\n")
    status, saved = call(base, app, "/api/file", {"path": "GUIDE.md", "text": "# Guide\n\nTyped.\n", "base": "# Guide\n"})
    assert status == 200 and saved["stored"] == {"file": "# Guide\n\nTyped.\n"} and guide.read_text() == "# Guide\n\nTyped.\n"
    guide.write_text("# Guide\n\nSaid the agent.\n")
    with pytest.raises(urllib.error.HTTPError) as clash:
        call(base, app, "/api/file", {"path": "GUIDE.md", "text": "# Guide\n\nTyped more.\n", "base": "# Guide\n\nTyped.\n"})
    assert clash.value.code == 409 and json.loads(clash.value.read())["current"] == {"file": "# Guide\n\nSaid the agent.\n"}
    assert guide.read_text() == "# Guide\n\nSaid the agent.\n"
    with pytest.raises(urllib.error.HTTPError) as foreign:
        call(base, app, "/api/file", {"path": "notes.txt", "text": "edited\n", "base": "plain\n"})
    assert foreign.value.code == 403 and (app.root / "notes.txt").read_text() == "plain\n"


@pytest.mark.syngate("SYNGATE_UI-020", "csp")
def test_the_page_runs_only_its_own_nonced_script(server):
    base, app = server
    request = urllib.request.Request(base + "/", headers={"X-Syngate-Token": app.token})
    with urllib.request.urlopen(request) as response:
        policy, page = response.headers["Content-Security-Policy"], response.read().decode()
    nonce = policy.split("script-src 'nonce-")[1].split("'")[0]
    assert "default-src 'none'" in policy and f'<script nonce="{nonce}">' in page and "<script>" not in page


PATHS = "[...document.querySelectorAll('%s [data-path]')].map((el) => el.dataset.path)"


@pytest.mark.syngate("SYNGATE_UI-020", "render")
def test_the_page_renders_every_tree_item_from_the_served_model(page):
    assert page.eval(PATHS % "#tree") == ["ROOT", "ROOT/LEAF-001"]
    assert page.eval(PATHS % "#doc") == ["ROOT", "ROOT/LEAF-001"]


@pytest.mark.syngate("SYNGATE_UI-031", "rows")
def test_a_row_carries_uid_editable_header_and_the_review_mark(page, syngate_tree):
    syngate_dir, _ = syngate_tree
    frozen = syngatelib.Item(uid="LEAF-002", path=syngate_dir / "LEAF-002.yml", header="Frozen", description="It shall freeze.\n", parents=["ROOT"], tests={None: "a" * 64})
    frozen.reviewed = syngatelib.compute_stamp(frozen)
    syngatelib.write_item(frozen)
    page.wait("document.querySelector('#tree [data-uid=\"LEAF-002\"]')")  # picked up by the fingerprint poll
    row = "(() => { const row = document.querySelector('#tree [data-uid=\"%s\"]'), title = row.querySelector('.title');" \
          " return [row.querySelector('.node-uid').textContent, title.value, title.readOnly, row.querySelector('.status').className]; })()"
    assert page.eval(row % "LEAF-001") == ["LEAF-001", "First leaf", False, "status gray not_reviewed"]
    assert page.eval(row % "LEAF-002") == ["LEAF-002", "Frozen", False, "status gray review_violated"]  # reviewed, but its stamped routine is gone
    page.eval("(() => { select('LEAF-002', 'ROOT/LEAF-002'); startEditing('ROOT/LEAF-002'); })()")
    assert page.eval("(() => { const block = document.querySelector('#doc .block.editing'); return [block.querySelector('textarea').readOnly, Boolean(block.querySelector('.block-problems'))]; })()") == [False, False]


@pytest.fixture
def violated_page(page, syngate_tree):
    """The page with LEAF-002 selected: reviewed, but its stamped routine is gone -- test unknown, review violated."""
    syngate_dir, _ = syngate_tree
    frozen = syngatelib.Item(uid="LEAF-002", path=syngate_dir / "LEAF-002.yml", header="Frozen", description="It shall freeze.\n", parents=["ROOT"], tests={None: "a" * 64})
    frozen.reviewed = syngatelib.compute_stamp(frozen)
    syngatelib.write_item(frozen)
    page.wait("document.querySelector('#tree [data-uid=\"LEAF-002\"]')")
    page.eval("select('LEAF-002', 'ROOT/LEAF-002')")
    return page


LABEL = "(() => { const el = document.querySelector('%s'), mark = el.querySelector('.status');" \
        " return [mark.className, mark.nextElementSibling.textContent, [...mark.children].map((c) => c.className + ':' + c.textContent)]; })()"


@pytest.mark.syngate("SYNGATE_UI-041")
def test_row_and_panel_label_an_item_by_one_status_icon_and_id(violated_page, server, syngate_tree):
    page, (_, app), (syngate_dir, _) = violated_page, server, syngate_tree
    routines = app.root / "scripts" / "tests"
    routines.mkdir(parents=True)
    (routines / "test_leaf.py").write_text('import pytest\n\n\n@pytest.mark.syngate("LEAF-003")\ndef test_leaf():\n    pass\n')
    sha = syngatelib.routine_sha(syngatelib.discover_bindings(app.root)[("LEAF-003", None)][0])
    approved = syngatelib.Item(uid="LEAF-003", path=syngate_dir / "LEAF-003.yml", header="Approved", description="It shall pass.\n", parents=["ROOT"], tests={None: sha})
    approved.reviewed = syngatelib.compute_stamp(approved)
    syngatelib.write_item(approved)
    for uid in ("LEAF-001", "LEAF-003"):
        with open(app.run_coverage, "a") as coverage:
            coverage.write(json.dumps({"tags": [uid], "passed": True, "name": "", "log": ""}) + "\n")
    page.wait("document.querySelector('#tree [data-uid=\"LEAF-003\"] .status.green')")
    for place in ("#tree [data-uid=\"%s\"]", "#doc .block[data-uid=\"%s\"] legend"):
        assert page.eval(LABEL % (place % "LEAF-002")) == ["status gray review_violated", "LEAF-002", ["eyes:👀", "seal:❗"]]  # test unknown, review violated
        assert page.eval(LABEL % (place % "LEAF-001")) == ["status green not_reviewed", "LEAF-001", []]  # passed but not reviewed: empty square
        assert page.eval(LABEL % (place % "LEAF-003")) == ["status green reviewed", "LEAF-003", ["eyes:👀"]]  # passed and reviewed
    assert page.eval("document.querySelector('#tree [data-uid=\"LEAF-001\"] .status').title") == "test passed · not reviewed"


@pytest.mark.syngate("SYNGATE_UI-042")
def test_a_tree_row_shows_collapse_mark_label_title_and_menu_only_and_activates_its_panel_on_double_click(violated_page):
    page = violated_page
    parts = "[...document.querySelector('#tree [data-uid=\"LEAF-002\"]').querySelectorAll('.twist, .status, .node-uid, .dupmark, .title, .tools button:not([hidden])')]" \
            ".map((el) => el.dataset.act || el.className.split(' ')[0])"
    assert page.eval(parts) == ["twist", "status", "node-uid", "title", "tests", "menu"]
    assert page.eval("Boolean(document.querySelector('#tree .row-problems'))") is False
    page.eval("document.querySelector('#tree [data-uid=\"LEAF-002\"] [data-act=\"menu\"]').click()")
    entries = "[document.getElementById('status-menu').matches(':popover-open'), [...document.querySelectorAll('#status-menu [data-row-act]')].map((el) => el.dataset.rowAct)]"
    assert page.eval(entries) == [True, ["add-parent", "add", "remove"]]  # structure edits only behind the explicit menu
    page.eval("document.querySelector('#tree [data-uid=\"ROOT\"] [data-act=\"menu\"]').click()")
    assert page.eval(entries) == [True, ["add"]]  # the root has no parent to add and an item with children cannot be removed
    page.eval("document.querySelector('#status-menu [data-row-act=\"add\"]').click()")
    assert page.eval("Boolean(document.querySelector('#tree .row.adding .new-uid'))")
    page.eval("document.querySelector('#tree [data-uid=\"LEAF-001\"] [data-act=\"menu\"]').click()")
    page.eval("document.querySelector('#status-menu [data-row-act=\"add-parent\"]').click()")
    search = "(() => { const rows = [...document.querySelectorAll('#tree .row')], i = rows.findIndex((r) => r.dataset.uid === 'LEAF-001');" \
             " return [rows[i + 1].classList.contains('adding'), [...rows[i + 1].querySelectorAll('datalist option')].map((o) => o.value)]; })()"
    assert page.eval(search) == [True, ["LEAF-002"]]  # the search row sits right under the item and offers the items it may hang from
    page.eval("(() => { const input = document.querySelector('#tree .row.adding .new-uid'); input.value = 'LEAF-002'; input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })); })()")
    page.wait("state.model.items['LEAF-001'].parents.length === 2")
    assert page.eval("[state.model.items['LEAF-001'].parents, state.model.items['LEAF-002'].children]") == [["ROOT", "LEAF-002"], ["LEAF-001"]]  # linked, the old parent kept
    page.eval("document.querySelector('#tree [data-uid=\"LEAF-001\"] [data-act=\"menu\"]').click()")
    assert page.eval("document.querySelector('#status-menu [data-row-act=\"remove\"]').textContent") == "⊖ Remove from ROOT"
    page.eval("document.getElementById('status-menu').hidePopover()")
    page.eval("window.scrolled = []; Element.prototype.scrollIntoView = function () { window.scrolled.push(this.className.split(' ')[0]); }")
    page.eval("document.querySelector('#tree [data-uid=\"LEAF-001\"] .cell-title').click()")
    selection = "[state.selected, document.querySelector('#tree .row.selected').dataset.uid, [...document.querySelectorAll('#doc .block.selected')].map((b) => b.dataset.uid), window.scrolled.includes('block')]"
    assert page.eval(selection) == ["LEAF-001", "LEAF-001", [], False]  # a click selects the row only: no panel activated, none scrolled to
    dblclick = "document.querySelector('#tree [data-uid=\"%s\"] .cell-title').dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))"
    page.eval(dblclick % "LEAF-001")
    assert page.eval("[state.selected, document.querySelector('#doc .block.selected').dataset.uid, window.scrolled.includes('block')]") == ["LEAF-001", "LEAF-001", False]  # the panel is in view: activated, not scrolled
    page.eval("document.getElementById('doc-pane').style.height = '40px'; window.scrolled = []")
    page.eval(dblclick % "LEAF-002")
    assert page.eval("[state.selected, window.scrolled.includes('block')]") == ["LEAF-002", True]  # the panel is out of view: scrolled to


@pytest.mark.syngate("SYNGATE_UI-043")
def test_a_content_panel_shows_label_without_title_and_status_button_with_popup(violated_page):
    page = violated_page
    label = "(() => { const tag = document.querySelector('#doc .block[data-uid=\"LEAF-002\"] legend .tag'); return [[...tag.children].filter((e) => !e.classList.contains('status')).map((e) => e.textContent).join(''), tag.querySelectorAll('button').length]; })()"
    assert page.eval(label) == ["LEAF-002", 0]
    foot = "(() => { const foot = document.querySelector('#doc .block[data-uid=\"LEAF-002\"] .block-foot'), box = foot.getBoundingClientRect()," \
           " pill = foot.querySelector('.block-status .pill').getBoundingClientRect(); return box.right - pill.right < 2; })()"
    assert page.eval(foot) is True
    tip = "[...document.querySelectorAll('#doc .block[data-uid=\"LEAF-002\"] .block-status .tip > div')].map((el) => [el.className, el.textContent])"
    lines = page.eval(tip)
    assert lines[0] == ["tip-unknown", "unknown"] and lines[1][0] == "tip-unknown" and "no tagged routine yet" in lines[1][1]
    assert lines[-1][0] == "tip-review_violated" and "no test routine tagged [LEAF-002] found" in lines[-1][1]
    assert page.eval("getComputedStyle(document.querySelector('#doc .block[data-uid=\"LEAF-002\"] .tip')).display") == "none"  # shown on hover only


class EchoConnector:
    models, efforts, modes = ("model-a", "model-b"), ("low", "high"), {"edit": True, "internet": True, "workflows": False}

    def __init__(self, label="Echo"):
        self.label = label

    def settings(self):
        return {"label": self.label, "models": [{"id": model, "name": model.removeprefix("model-")} for model in self.models], "efforts": list(self.efforts),
                "modes": [{"id": mode, "label": mode.capitalize(), "hint": mode + " hint", "default": default} for mode, default in self.modes.items()]}

    def dispatch(self, context, text, model=None, effort=None, modes=(), session=None):
        return {"kind": "text", "text": f"{context}|{text}|{model}|{effort}|{','.join(modes)}|{session}"}, "session-1"


SETUP = "document.querySelector('.chat-setup').textContent"


def item_page(server, chrome, **connectors):
    base, app = server
    app.connectors = connectors or {"echo": EchoConnector()}
    page = Page(chrome, f"{base}/?token={app.token}")
    page.wait("document.querySelector('#tree [data-uid=\"LEAF-001\"]')")
    page.wait("document.querySelector('#doc .block[data-uid=\"LEAF-001\"] .chat-open')")
    return page


def open_chat(server, chrome, **connectors):
    page = item_page(server, chrome, **connectors)
    page.eval("document.querySelector('#doc .block[data-uid=\"LEAF-001\"] .chat-open').click()")
    return page


def edit_leaf(page, text):
    page.eval("(() => { select('LEAF-001', 'ROOT/LEAF-001'); startEditing('ROOT/LEAF-001'); })()")
    page.eval(f"(() => {{ const text = document.querySelector('#doc .block.editing textarea'); text.value = '{text}'; text.dispatchEvent(new Event('input', {{ bubbles: true }})); }})()")


LEAF_BLOCK = "document.querySelector('#doc .block[data-uid=\"LEAF-001\"]')"


def tree_item(server, uid):
    base, app = server
    return call(base, app, "/api/tree")[1]["items"][uid]


@pytest.mark.syngate("AI_CHAT-022")
def test_editing_the_description_offers_the_ai_controls_and_sends_the_saved_statement(server, chrome):
    page = item_page(server, chrome)
    edit_leaf(page, "It shall leaf better.")
    controls = f"[...{LEAF_BLOCK}.querySelectorAll('.block-ai .chat-send, .block-ai .chat-setup, .block-ai .chat-mode')].length"
    assert page.eval(controls) == 5  # send, setup and the three mode switches, before anything is sent
    pick(page, "model", "model-b")
    page.eval(f"{LEAF_BLOCK}.querySelector('.block-ai .chat-send').click()")
    page.wait(f"{LEAF_BLOCK}.querySelector('.chat-msg.ai')")
    assert tree_item(server, "LEAF-001")["description_raw"].strip() == "It shall leaf better."
    assert page.eval(f"{LEAF_BLOCK}.querySelector('.chat-msg.ai').textContent").endswith("It shall leaf better.|It shall leaf better.|model-b|low|edit,internet|None")
    assert page.eval(f"{LEAF_BLOCK}.querySelectorAll('.chat-msg.user').length") == 0  # the first message is the content itself


@pytest.mark.syngate("AI_CHAT-022", "ctrl_enter")
def test_ctrl_enter_in_the_editor_sends_the_edited_description(server, chrome):
    page = item_page(server, chrome)
    edit_leaf(page, "It shall leaf by keyboard.")
    page.eval("document.querySelector('#doc .block.editing textarea').dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true, bubbles: true }))")
    page.wait(f"{LEAF_BLOCK}.querySelector('.chat-msg.ai')")
    assert page.eval(f"{LEAF_BLOCK}.querySelector('.chat-msg.ai').textContent").endswith("|It shall leaf by keyboard.|model-a|low|edit,internet|None")


@pytest.mark.syngate("AI_CHAT-023")
def test_the_tool_button_on_a_selected_item_starts_the_chat_without_changing_the_item(server, chrome):
    page = open_chat(server, chrome)
    assert page.eval(f"[Boolean({LEAF_BLOCK}.querySelector('.chat-input')), {LEAF_BLOCK}.classList.contains('editing')]") == [True, False]
    send(page, "LEAF-001", "Just asking")
    page.wait(f"{LEAF_BLOCK}.querySelector('.chat-msg.ai')")
    assert tree_item(server, "LEAF-001")["description_raw"] == "It shall leaf.\n"


@pytest.mark.syngate("AI_CHAT-024")
def test_clicking_the_reply_field_after_an_edit_saves_the_item_and_waits_for_a_message(server, chrome):
    page = item_page(server, chrome)
    edit_leaf(page, "It shall leaf, saved by the reply field.")
    page.eval(f"{LEAF_BLOCK}.querySelector('.chat-input').click()")
    page.wait(f"!{LEAF_BLOCK}.classList.contains('editing')")
    assert tree_item(server, "LEAF-001")["description_raw"].strip() == "It shall leaf, saved by the reply field."
    assert page.eval(f"[document.activeElement === {LEAF_BLOCK}.querySelector('.chat-input'), {LEAF_BLOCK}.querySelectorAll('.chat-msg').length]") == [True, 0]
    send(page, "LEAF-001", "Now ask")
    page.wait(f"{LEAF_BLOCK}.querySelector('.chat-msg.ai')")
    assert page.eval(f"{LEAF_BLOCK}.querySelector('.chat-msg.ai').textContent").endswith("|Now ask|model-a|low|edit,internet|None")


@pytest.mark.syngate("AI_CHAT-025")
def test_user_messages_look_like_the_description_and_ai_replies_sit_right_highlighted(server, chrome):
    page = open_chat(server, chrome)
    send(page, "LEAF-001", "Styled?")
    page.wait(f"{LEAF_BLOCK}.querySelector('.chat-msg.ai')")
    look = f"(() => {{ const body = {LEAF_BLOCK}.querySelector('.block-body .block-text'), user = {LEAF_BLOCK}.querySelector('.chat-msg.user'), ai = {LEAF_BLOCK}.querySelector('.chat-msg.ai');" \
           " const css = (el) => getComputedStyle(el); return [css(user).backgroundColor === css(body).backgroundColor, css(user).alignSelf, css(ai).alignSelf, css(ai).backgroundColor !== css(body).backgroundColor]; })()"
    assert page.eval(look) == [True, "flex-start", "flex-end", True]


@pytest.mark.syngate("AI_CHAT-026")
def test_every_reply_carries_fold_and_keep_tools_at_its_left_edge(server, chrome):
    page = open_chat(server, chrome)
    send(page, "LEAF-001", "First")
    page.wait(f"{LEAF_BLOCK}.querySelector('.chat-msg.ai')")
    tools = f"[...{LEAF_BLOCK}.querySelectorAll('.chat-msg')].map((el) => [Boolean(el.querySelector('.msg-fold')), Boolean(el.querySelector('.msg-keep')), el.querySelector('.msg-keep')?.getAttribute('aria-checked')])"
    assert page.eval(tools) == [[True, True, "true"], [True, True, "true"]]
    place = f"(() => {{ const msg = {LEAF_BLOCK}.querySelector('.chat-msg.ai'), tools = msg.querySelector('.msg-tools').getBoundingClientRect(), box = msg.getBoundingClientRect();" \
            " return [tools.left - box.left < 4, Math.abs(tools.top - box.top) < parseFloat(getComputedStyle(msg).lineHeight)]; })()"
    assert page.eval(place) == [True, True]
    page.eval(f"{LEAF_BLOCK}.querySelector('.chat-msg.ai .msg-fold').click()")
    assert page.eval(f"{LEAF_BLOCK}.querySelector('.chat-msg.ai .msg-fold').getAttribute('aria-expanded')") == "false"
    assert page.eval(f"{LEAF_BLOCK}.querySelector('.chat-msg.ai').getBoundingClientRect().height <= 2 * parseFloat(getComputedStyle({LEAF_BLOCK}.querySelector('.chat-msg.ai')).lineHeight)")
    page.eval(f"{LEAF_BLOCK}.querySelector('.chat-msg.ai .msg-keep').click()")
    gray = css_color(page, "--dim")
    assert page.eval(f"(() => {{ const msg = {LEAF_BLOCK}.querySelector('.chat-msg.ai'); return [msg.classList.contains('dropped'), getComputedStyle(msg).color]; }})()") == [True, gray]


@pytest.mark.syngate("AI_CHAT-027")
def test_a_message_turns_into_a_child_or_next_sibling_item_while_the_chat_stays(server, chrome):
    page = open_chat(server, chrome, echo=CallsConnector("Leaf sentence\n\nIt shall be extracted."))
    send(page, "LEAF-001", "Say something")
    page.wait(f"{LEAF_BLOCK}.querySelector('.chat-msg.ai')")
    page.eval(f"{LEAF_BLOCK}.querySelector('.chat-msg.ai .msg-child').click()")
    page.wait("document.querySelector('#tree [data-path^=\"ROOT/LEAF-001/\"]')")
    page.eval(f"{LEAF_BLOCK}.querySelector('.chat-msg.ai .msg-sibling').click()")
    page.wait("document.querySelectorAll('#tree [data-path]').length === 4")
    paths = page.eval(PATHS % "#tree")
    child, sibling = paths[2].rsplit("/", 1)[1], paths[3].rsplit("/", 1)[1]
    assert paths[2].startswith("ROOT/LEAF-001/") and paths[3] == f"ROOT/{sibling}" and sibling != "LEAF-001"
    for uid in (child, sibling):
        extracted = tree_item(server, uid)
        assert (extracted["header"], extracted["description_raw"]) == ("Leaf sentence", "It shall be extracted.\n")
    assert page.eval(f"[...{LEAF_BLOCK}.querySelectorAll('.chat-msg')].map((el) => el.className)") == ["chat-msg user", "chat-msg ai"]


@pytest.mark.syngate("AI_CHAT-050")
def test_an_at_reference_brings_the_named_item_into_the_turn(server, chrome):
    page = open_chat(server, chrome)
    send(page, "LEAF-001", "Compare with @ROOT please")
    page.wait(f"{LEAF_BLOCK}.querySelector('.chat-msg.ai')")
    reply = page.eval(f"{LEAF_BLOCK}.querySelector('.chat-msg.ai').textContent")
    assert reply.endswith("|Compare with @ROOT please|model-a|low|edit,internet|None")  # the text stays as typed
    assert "Root branch" in reply.split("|Compare with")[0].split("It shall leaf.")[-1]  # the referenced statement follows the seed context


def pick(page, attribute, value):
    page.eval("document.querySelector('.chat-setup').click()")
    assert page.eval("document.getElementById('status-menu').matches(':popover-open')")
    page.eval(f"document.querySelector('#status-menu [data-chat-{attribute}=\"{value}\"]').click()")


@pytest.mark.syngate("AI_CHAT-010")
def test_the_connector_menu_selects_the_setup(server, chrome):
    page = open_chat(server, chrome, echo=EchoConnector(), other=EchoConnector("Other"))
    pick(page, "model", "model-b")
    assert page.eval(SETUP) == "Echo · b · low ▾"
    pick(page, "connector", "other")
    assert page.eval(SETUP) == "Other · a · low ▾"  # a new connector starts from its own defaults


def css_color(page, var):
    return page.eval(f"(() => {{ const probe = document.createElement('span'); probe.style.color = 'var({var})'; document.body.append(probe);"
                     " const color = getComputedStyle(probe).color; probe.remove(); return color; })()")


@pytest.mark.syngate("AI_CHAT-011")
def test_the_permission_switches_show_their_labels_defaults_and_state_colors(server, chrome):
    page = open_chat(server, chrome)
    modes = "[...document.querySelectorAll('.block-ai .chat-mode')].map((el) => [el.textContent, el.getAttribute('aria-checked')])"
    assert page.eval(modes) == [["Edit", "true"], ["Internet", "true"], ["Workflows", "false"]]
    look = "[...document.querySelectorAll('.block-ai .chat-mode')].map((el) => [el.className, getComputedStyle(el).color, getComputedStyle(el.querySelector('.ball')).backgroundColor])"
    green, gray = css_color(page, "--green"), css_color(page, "--gray")
    assert [entry[1:] for entry in page.eval(look)] == [[green, green], [green, green], [gray, gray]]
    assert all(entry[0].startswith("pill chat-mode st-") for entry in page.eval(look))  # the status label's own pill and tint classes
    page.eval("document.querySelector('.chat-mode[data-chat-mode=\"edit\"]').click()")
    page.eval("document.querySelector('.chat-mode[data-chat-mode=\"workflows\"]').click()")
    assert page.eval(modes) == [["Edit", "false"], ["Internet", "true"], ["Workflows", "true"]]
    page.eval("(() => { document.querySelector('.chat-input').value = 'Go.'; document.querySelector('.chat-send').click(); })()")
    page.wait("document.querySelector('.chat-msg.ai')")
    assert page.eval("document.querySelector('.chat-msg.ai').textContent").endswith("|Go.|model-a|low|internet,workflows|None")


@pytest.mark.syngate("AI_CHAT-020")
def test_the_input_field_takes_the_text_of_the_exchange(server, chrome):
    page = open_chat(server, chrome)
    assert page.eval("(() => { const input = document.querySelector('.block-chat textarea.chat-input'); return [document.activeElement === input, input.value]; })()") == [True, ""]
    page.eval("(() => { const input = document.querySelector('.chat-input'); input.value = 'Typed by the user';"
              " input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true, bubbles: true })); })()")
    page.wait("document.querySelector('.chat-msg.ai')")
    assert page.eval("[document.querySelector('.chat-msg.user').textContent, document.querySelector('.chat-input').value]") == ["Typed by the user", ""]
    page.eval("document.querySelector('.chat-send').click()")  # an empty field sends nothing
    assert page.eval("document.querySelectorAll('.chat-msg').length") == 2


@pytest.mark.syngate("AI_CHAT-021")
def test_the_history_shows_above_the_input_field_once_there_is_any(server, chrome):
    page = open_chat(server, chrome)
    log = "document.querySelector('.block-chat .chat-log')"
    assert page.eval(f"[{log}.children.length, getComputedStyle({log}).display]") == [0, "none"]
    page.eval("(() => { document.querySelector('.chat-input').value = 'First'; document.querySelector('.chat-send').click(); })()")
    page.wait("document.querySelector('.chat-msg.ai')")
    assert page.eval(f"getComputedStyle({log}).display") != "none"
    assert page.eval(f"{log}.getBoundingClientRect().bottom <= document.querySelector('.chat-input').getBoundingClientRect().top")
    assert page.eval(f"[...{log}.querySelectorAll('.chat-msg')].map((el) => el.classList.contains('user') ? 'user' : 'ai')") == ["user", "ai"]


@pytest.mark.syngate("AI_CHAT-030")
def test_the_green_check_mark_hides_the_chat_and_becomes_the_drop_down_reopening_it(server, chrome):
    page = open_chat(server, chrome)
    send(page, "LEAF-001", "Kept")
    page.wait(f"{LEAF_BLOCK}.querySelector('.chat-msg.ai')")
    closer = f"{LEAF_BLOCK}.querySelector('.block-ai .chat-close')"
    assert page.eval(f"[{closer}.textContent, getComputedStyle({closer}).color]") == ["✓", css_color(page, "--green")]
    page.eval(f"{closer}.click()")
    hidden = f"[Boolean({LEAF_BLOCK}.querySelector('.chat-input')), Boolean({LEAF_BLOCK}.querySelector('.block-ai .chat-open')), Boolean({LEAF_BLOCK}.querySelector('.block-ai .chat-close')), {LEAF_BLOCK}.querySelector('.block-ai .chat-reopen')?.textContent]"
    assert page.eval(hidden) == [False, True, False, "▾"]
    page.eval(f"{LEAF_BLOCK}.querySelector('.block-ai .chat-reopen').click()")
    assert page.eval(f"[...{LEAF_BLOCK}.querySelectorAll('.chat-msg')].map((el) => el.className)") == ["chat-msg user", "chat-msg ai"]


@pytest.mark.syngate("AI_CHAT-030", "replaced")
def test_a_new_chat_replaces_the_hidden_one_only_when_its_first_message_is_sent(server, chrome):
    page = open_chat(server, chrome)
    send(page, "LEAF-001", "Old")
    page.wait(f"{LEAF_BLOCK}.querySelector('.chat-msg.ai')")
    page.eval(f"{LEAF_BLOCK}.querySelector('.block-ai .chat-close').click()")
    page.eval(f"{LEAF_BLOCK}.querySelector('.block-ai .chat-open').click()")
    assert page.eval(f"[{LEAF_BLOCK}.querySelectorAll('.chat-msg').length, Boolean({LEAF_BLOCK}.querySelector('.block-ai .chat-reopen'))]") == [0, True]  # fresh field, the old history still reachable
    send(page, "LEAF-001", "New")
    page.wait(f"{LEAF_BLOCK}.querySelector('.chat-msg.ai')")
    assert page.eval(f"[[...{LEAF_BLOCK}.querySelectorAll('.chat-msg.user')].map((el) => el.textContent), Boolean({LEAF_BLOCK}.querySelector('.block-ai .chat-reopen'))]") == [["New"], False]
    assert page.eval(f"{LEAF_BLOCK}.querySelector('.chat-msg.ai').textContent").endswith("|New|model-a|low|edit,internet|None")  # a new session, not the old one


@pytest.mark.syngate("AI_CHAT")
def test_the_chat_window_opens_under_the_item_and_sends_through_the_chosen_setup(server, chrome):
    page = open_chat(server, chrome)
    assert page.eval(PATHS % "#doc") == ["ROOT", "ROOT/LEAF-001"]  # no separate element: the item's own block expands
    assert page.eval("Boolean(document.querySelector('.block[data-uid=\"LEAF-001\"] .block-chat .chat-input'))")
    assert page.eval(SETUP) == "Echo · a · low ▾"
    pick(page, "model", "model-b")
    pick(page, "effort", "high")
    assert page.eval(SETUP) == "Echo · b · high ▾"
    send(page, "LEAF-001", "What is missing?")
    page.wait("document.querySelector('.chat-msg.ai')")
    user, ai = page.eval("[...document.querySelectorAll('.chat-msg')].map((el) => el.textContent.trim())")
    assert user == "What is missing?"
    assert ai.startswith("Syngate tree calls") and ai.endswith("Root branch It shall leaf.|What is missing?|model-b|high|edit,internet|None")  # the default prompt heads the seed
    send(page, "LEAF-001", "And next?")
    page.wait("document.querySelectorAll('.chat-msg.ai').length === 2")
    assert page.eval("document.querySelectorAll('.chat-msg.ai')[1].textContent").endswith("|And next?|model-b|high|edit,internet|session-1")


def send(page, uid, text):
    page.eval(f"(() => {{ const box = document.querySelector('.block[data-uid=\"{uid}\"]'); box.querySelector('.chat-input').value = '{text}'; box.querySelector('.chat-send').click(); }})()")


CHATTING = "[...document.querySelectorAll('#doc .block.chatting')].map((el) => [el.dataset.uid, Boolean(el.querySelector('.block-chat .chat-input')), Boolean(el.querySelector('.block-ai .chat-close'))])"


@pytest.mark.syngate("AI_CHAT-001")
def test_several_chat_windows_are_open_at_once_each_under_its_own_item(server, chrome):
    page = open_chat(server, chrome)
    page.eval("document.querySelector('#doc .block[data-uid=\"ROOT\"] .chat-open').click()")
    assert page.eval(CHATTING) == [["ROOT", True, True], ["LEAF-001", True, True]]
    send(page, "ROOT", "Root?")
    page.wait("document.querySelector('.block[data-uid=\"ROOT\"] .chat-msg.ai')")
    assert page.eval("document.querySelector('.block[data-uid=\"ROOT\"] .chat-msg.ai').textContent").endswith("|Root?|model-a|low|edit,internet|None")
    assert page.eval("document.querySelectorAll('.block[data-uid=\"LEAF-001\"] .chat-msg').length") == 0
    page.eval("document.querySelector('.block[data-uid=\"ROOT\"] .chat-close').click()")
    assert page.eval(CHATTING) == [["LEAF-001", True, True]]


def reload(page):
    page.cdp.call("Page.reload", session=page.session)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if page.eval("Boolean(document.querySelector('#tree [data-uid=\"LEAF-001\"]'))"):
                return
        except (RuntimeError, AssertionError):
            pass
        time.sleep(0.05)
    raise AssertionError("the page never came back after reload")


@pytest.mark.syngate("AI_CHAT-MULTI")
def test_a_chat_keeps_its_history_while_closed_and_reopens_with_it_on_its_item(server, chrome):
    page = open_chat(server, chrome)
    send(page, "LEAF-001", "First")
    page.wait("document.querySelector('.chat-msg.ai')")
    page.eval("document.querySelector('.block-ai .chat-close').click()")
    assert page.eval("document.querySelectorAll('.chat-msg').length") == 0
    page.eval("document.querySelector('#doc .block[data-uid=\"LEAF-001\"] .chat-reopen').click()")
    kinds = "[...document.querySelectorAll('.block[data-uid=\"LEAF-001\"] .chat-msg')].map((el) => el.className)"
    assert page.eval(kinds) == ["chat-msg user", "chat-msg ai"]
    send(page, "LEAF-001", "Second")
    page.wait("document.querySelectorAll('.chat-msg.ai').length === 2")
    assert page.eval("document.querySelectorAll('.chat-msg.ai')[1].textContent").endswith("|Second|model-a|low|edit,internet|session-1")  # the session goes on
    reload(page)
    page.wait("document.querySelector('.block[data-uid=\"LEAF-001\"] .chat-msg.ai')")
    assert page.eval(kinds) == ["chat-msg user", "chat-msg ai", "chat-msg user", "chat-msg ai"]
    assert page.eval(CHATTING) == [["LEAF-001", True, True]]


class ParallelConnector(EchoConnector):
    """Answers only once two exchanges are in flight at the same time."""

    def __init__(self):
        super().__init__()
        self.gate = threading.Barrier(2, timeout=10)

    def dispatch(self, context, text, model=None, effort=None, modes=(), session=None):
        self.gate.wait()
        return f"reply to {text} in {session}", f"session-{text}"


@pytest.mark.syngate("AI_CHAT-MULTI-020")
def test_chats_of_different_items_run_their_exchanges_in_parallel(server, chrome):
    page = open_chat(server, chrome, echo=ParallelConnector())
    page.eval("document.querySelector('#doc .block[data-uid=\"ROOT\"] .chat-open').click()")
    reply = "[...document.querySelectorAll('.block[data-uid=\"%s\"] .chat-msg.ai')].map((el) => el.textContent.trim())"
    for turn in ("one", "two"):
        send(page, "ROOT", f"root-{turn}")
        send(page, "LEAF-001", f"leaf-{turn}")
        page.wait(f"document.querySelectorAll('.chat-msg.ai').length === {2 if turn == 'one' else 4}")
    assert page.eval(reply % "ROOT") == ["reply to root-one in None", "reply to root-two in session-root-one"]
    assert page.eval(reply % "LEAF-001") == ["reply to leaf-one in None", "reply to leaf-two in session-leaf-one"]


@pytest.mark.syngate("SYNGATE_UI-038")
def test_the_splitter_resizes_the_outline_and_double_click_resets_it(page):
    width = "Math.round(document.getElementById('tree-pane').getBoundingClientRect().width)"
    default = page.eval(width)
    x, y = page.eval("(() => { const r = document.getElementById('splitter').getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()")
    target = x - 150
    page.mouse("mousePressed", x, y)
    page.mouse("mouseMoved", target, y)
    page.mouse("mouseReleased", target, y)
    assert page.eval(width) == round(target) != default
    assert page.eval("sessionStorage.getItem('syngate-ui-tree-width')") == str(round(target))
    page.mouse("mousePressed", target + 2, y, clicks=2)
    page.mouse("mouseReleased", target + 2, y, clicks=2)
    assert page.eval(width) == default and page.eval("sessionStorage.getItem('syngate-ui-tree-width')") is None


class CallsConnector(EchoConnector):
    """Answers with one fixed reply, whatever the question."""

    def __init__(self, reply):
        super().__init__()
        self.reply = reply

    def dispatch(self, context, text, model=None, effort=None, modes=(), session=None):
        return self.reply, "session-2"


def _chat(server, reply, uid="ROOT"):
    base, app = server
    app.connectors = {"echo": CallsConnector(reply)}
    return call(base, app, "/api/chat", {"uid": uid, "connector": "echo", "model": "model-a", "effort": "low", "text": "Go."})


@pytest.mark.syngate("AI_CHAT-040")
def test_a_tree_call_answer_is_applied_to_the_tree_and_a_mixed_one_refused(server, syngate_tree):
    syngate_dir, _ = syngate_tree
    calls = [{"tool": "add", "uid": "NEW-001", "header": "New branch", "description": "It groups.", "kind": "branch"},
             {"tool": "add", "uid": "NEW-002", "parent": "NEW-001", "header": "New leaf", "description": "It shall be new.\n"},
             {"tool": "move", "uid": "LEAF-001", "to": "NEW-001", "before": "NEW-002"},
             {"tool": "remove", "uid": "NEW-002"}]
    status, result = _chat(server, "```syngate\n" + json.dumps(calls) + "\n```")
    assert status == 200 and result["session"] == "session-2" and result["calls"] == calls
    assert result["reply"] == "- added NEW-001 under ROOT\n- added NEW-002 under NEW-001\n- moved LEAF-001 under NEW-001\n- removed NEW-002"
    items, errors = syngatelib.load_tree(syngate_dir)
    assert errors == [] and sorted(items) == ["LEAF-001", "NEW-001", "ROOT"]
    assert (items["NEW-001"].header, items["NEW-001"].description, items["NEW-001"].parents, items["NEW-001"].tests) == ("New branch", "It groups.\n", ["ROOT"], None)
    assert items["LEAF-001"].parents == ["NEW-001"] and items["LEAF-001"].tests == {None: None}
    status, result = _chat(server, "Just talking.")
    assert (status, result["reply"]) == (200, "Just talking.")
    with pytest.raises(urllib.error.HTTPError) as refused:
        _chat(server, "Applying:\n```syngate\n" + json.dumps(calls[:1]) + "\n```")
    body = json.loads(refused.value.read())
    assert refused.value.code == 422 and body["session"] == "session-2" and "never both" in body["error"]
    with pytest.raises(urllib.error.HTTPError) as refused:
        _chat(server, "```syngate\n" + json.dumps([{"tool": "add", "uid": "NEW-003", "header": "H", "description": "D"}, {"tool": "remove", "uid": "@"}]) + "\n```")
    body = json.loads(refused.value.read())
    assert refused.value.code == 400 and body["error"].startswith("- added NEW-003 under ROOT\nremove ROOT refused: ROOT still has children")
    assert sorted(syngatelib.load_tree(syngate_dir)[0]) == ["LEAF-001", "NEW-001", "NEW-003", "ROOT"]


@pytest.mark.syngate("SYNGATE_UI-044")
def test_a_feature_switched_off_has_no_affordances_on_the_page(server, chrome, syngate_tree):
    base, app = server
    syngate_dir, make_item = syngate_tree
    make_item(syngate_dir, "ROOT", "Root branch\n", parents=(), features=())
    make_item(syngate_dir, "LEAF-001", "It shall leaf.\n", parents=("ROOT",), header="First leaf")
    page = Page(chrome, f"{base}/?token={app.token}")
    page.wait("document.querySelector('#tree [data-uid=\"LEAF-001\"]')")
    gone = "[Boolean(document.querySelector('#tree .status:not([hidden])')), Boolean(document.querySelector('#tree [data-act=\"tests\"]')), document.querySelectorAll('#tree .status .eyes').length]"
    assert page.eval(gone) == [False, False, 0]
    pill = "document.querySelector('#doc .block[data-uid=\"LEAF-001\"] .block-status .pill')"
    entries = "[...document.querySelectorAll('#status-menu [data-run]')].map((el) => el.dataset.run + ':' + el.textContent)"
    assert page.eval(f"{pill}.textContent.trim()") == "▶ run"  # the tree awaits no tests, but a run stays offered
    page.eval(f"{pill}.click()")
    assert page.eval(entries) == ["test:▶ Run test"]
    page.eval("document.getElementById('status-menu').hidePopover()")
    make_item(syngate_dir, "ROOT", "Root branch\n", parents=(), features=("review",))
    page.wait(f"{pill}.textContent.trim() === '○ not reviewed'")  # picked up by the fingerprint poll
    assert page.eval("[...document.querySelectorAll('#tree [data-uid=\"LEAF-001\"] .status, #tree [data-act=\"tests\"]')].map((el) => [el.className, el.hidden])") == [["status gray not_reviewed", False]]
    page.eval(f"{pill}.click()")
    assert page.eval(entries) == ["test:▶ Run test", "review:✓ Mark reviewed"]


class GatedConnector(EchoConnector):
    """Answers the scripted `first` replies at once, then only when released; interrupted, it fails as a killed process does."""

    def __init__(self, *first):
        super().__init__()
        self.first = list(first)
        self.release = threading.Event()
        self.interrupted = False

    def dispatch(self, context, text, model=None, effort=None, modes=(), session=None):
        if self.first:
            return self.first.pop(0), "session-early"
        self.release.wait(10)
        if self.interrupted:
            raise synthetic.connection_error("claude exited with -9")
        return {"kind": "text", "text": f"late reply to {text}"}, "session-late"

    def interrupt(self, ident):
        self.interrupted = True
        self.release.set()


LEAF = "document.querySelector('.block[data-uid=\"LEAF-001\"]')"
WAITING = f"Boolean({LEAF}.querySelector('.chat-msg.note .spinner'))"


@pytest.mark.syngate("AI_CHAT-060")
def test_a_turn_is_awaited_on_the_server_and_a_reloaded_page_re_attaches_to_it(server, chrome):
    base, app = server
    gated = GatedConnector()
    page = open_chat(server, chrome, echo=gated)
    send(page, "LEAF-001", "Slow one")
    page.wait(WAITING)
    page.wait("state.chats.get('LEAF-001').turn")
    turn = page.eval("state.chats.get('LEAF-001').turn")
    assert call(base, app, f"/api/turn/{turn}")[1] == {"turn": turn, "running": True}
    reload(page)
    page.wait(WAITING)
    assert page.eval(f"{LEAF}.querySelectorAll('.chat-msg.ai').length") == 0
    gated.release.set()
    page.wait(f"{LEAF}.querySelector('.chat-msg.ai')")
    assert page.eval(f"{LEAF}.querySelector('.chat-msg.ai').textContent").strip() == "late reply to Slow one"
    assert page.eval("[state.chats.get('LEAF-001').turn, state.chats.get('LEAF-001').session]") == [None, "session-late"]
    with pytest.raises(urllib.error.HTTPError) as handed_over:
        call(base, app, f"/api/turn/{turn}")
    assert handed_over.value.code == 404


@pytest.mark.syngate("AI_CHAT-061")
def test_while_a_turn_runs_the_close_mark_is_a_stop_button_that_interrupts_it(server, chrome):
    gated = GatedConnector({"kind": "calls", "calls": [{"tool": "query", "uid": "@"}]})
    page = open_chat(server, chrome, echo=gated)
    pills = f"[Boolean({LEAF}.querySelector('.block-ai .chat-close')), Boolean({LEAF}.querySelector('.block-ai .chat-stop')), Boolean({LEAF}.querySelector('.chat-pill.busy'))]"
    assert page.eval(pills) == [True, False, False]
    send(page, "LEAF-001", "Stuck one")
    page.wait(f"{LEAF}.querySelector('.block-ai .chat-stop')")
    assert page.eval(pills) == [False, True, True]
    page.eval(f"{LEAF}.querySelector('.block-ai .chat-stop').click()")
    page.wait(f"{LEAF}.querySelector('.chat-msg.error')")
    assert gated.interrupted
    stopped = page.eval(f"{LEAF}.querySelector('.chat-msg.error').textContent").strip()
    assert stopped.startswith("- LEAF-001: {") and stopped.endswith("\nturn stopped")
    assert page.eval(pills) == [True, False, False]
