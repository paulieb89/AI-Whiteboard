"""
Tests for the AI whiteboard: board state, HTTP API, live events, snapshots,
the MCP server and the demo boards.
"""

import base64
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from whiteboard.demos import DEMOS
from whiteboard.reference import guide, icon_names
from whiteboard.server import make_server
from whiteboard.state import BoardError, BoardState, rects_intersect

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- helpers

@pytest.fixture()
def board_server(tmp_path):
    httpd = make_server(port=0, data_dir=tmp_path, persist=False)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield url, httpd.hub
    httpd.hub.close_all()
    httpd.shutdown()
    httpd.server_close()


def call(url, path, body=None, headers=None, method=None):
    data = None if body is None else json.dumps(body).encode()
    hdrs = {"Content-Type": "application/json"} if body is not None else {}
    hdrs.update(headers or {})
    req = urllib.request.Request(url + path, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            return resp.status, (json.loads(raw) if "json" in ctype else raw)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


class EventReader:
    """Minimal Server-Sent Events client that collects events on a thread."""

    def __init__(self, url):
        self.events = []
        self.resp = urllib.request.urlopen(url + "/api/events", timeout=10)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            for raw in self.resp:
                line = raw.decode("utf-8").strip()
                if line.startswith("data: "):
                    self.events.append(json.loads(line[6:]))
        except Exception:
            pass

    def wait_for(self, kind, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for ev in self.events:
                if ev.get("type") == kind:
                    return ev
            time.sleep(0.02)
        raise AssertionError(f"no {kind!r} event; got {[e.get('type') for e in self.events]}")

    def close(self, hub):
        hub.close_all()  # ends the stream server-side, so the reader thread returns
        self.thread.join(timeout=3)
        self.resp.close()


# ---------------------------------------------------------------- board state

def test_aliases_ids_and_unknown_types():
    st = BoardState()
    added, _ = st.add([{"type": "note", "text": "hi"}, {"type": "Box", "x": 10, "y": 10}])
    assert [e["type"] for e in added] == ["sticky", "rect"]
    assert [e["id"] for e in added] == ["sticky1", "rect1"]
    with pytest.raises(BoardError, match="unknown element type"):
        st.add([{"type": "hologram"}])
    with pytest.raises(BoardError, match="must be a number"):
        st.add([{"type": "rect", "x": "left"}])
    with pytest.raises(BoardError, match="arrow needs"):
        st.add([{"type": "arrow", "from": "rect1"}])


def test_auto_placement_avoids_existing_elements():
    st = BoardState()
    st.add([{"type": "rect", "x": 30, "y": 30, "w": 600, "h": 400}])
    added, _ = st.add([{"type": "panel", "title": "Auto", "items": ["one", "two"]}])
    new_box = st.bbox_of(added[0]["id"])
    assert not rects_intersect(new_box, st.bbox_of("rect1"))
    assert 0 <= new_box[0] and new_box[0] + new_box[2] <= 1600


def test_auto_placement_starts_below_a_title():
    st = BoardState()
    added, _ = st.add([{"type": "title", "text": "Sprint plan"}, {"type": "panel", "title": "To do", "items": ["API"]}])
    title_box, panel_box = st.bbox_of("title1"), st.bbox_of(added[1]["id"])
    assert panel_box[1] >= title_box[1] + title_box[3]
    assert st.warnings_for(["title1", added[1]["id"]]) == []


def test_relative_placement_below_and_right_of():
    st = BoardState()
    st.add([{"type": "rect", "id": "a", "x": 100, "y": 100, "w": 200, "h": 100}])
    added, _ = st.add([
        {"type": "rect", "id": "b", "below": "a", "gap": 20, "w": 200, "h": 50},
        {"type": "rect", "id": "c", "right_of": "a", "w": 80, "h": 80},
    ])
    b, c = added
    assert (b["x"], b["y"]) == (100, 220)
    assert c["x"] == 330 and c["y"] == 100
    assert "below" not in b
    with pytest.raises(BoardError, match="unknown element"):
        st.add([{"type": "rect", "below": "nope"}])


def test_update_shift_undo_and_clear():
    st = BoardState()
    st.add([{"type": "rect", "id": "a", "x": 10, "y": 20, "w": 50, "h": 50, "color": "blue"}])
    st.update("a", {"dx": 5, "dy": -5, "color": "red"})
    assert (st.elements["a"]["x"], st.elements["a"]["y"], st.elements["a"]["color"]) == (15, 15, "red")
    assert st.undo()
    assert st.elements["a"]["color"] == "blue"
    st.clear()
    assert not st.elements
    assert st.undo()
    assert "a" in st.elements


def test_replacing_an_id_moves_it_to_the_top():
    st = BoardState()
    st.add([{"type": "rect", "id": "a", "x": 0, "y": 0}, {"type": "rect", "id": "b", "x": 300, "y": 0}])
    _, replaced = st.add([{"type": "ellipse", "id": "a", "x": 0, "y": 0}])
    assert replaced == ["a"]
    assert list(st.elements) == ["b", "a"]
    assert st.elements["a"]["type"] == "ellipse"


def test_warnings_and_occupancy():
    st = BoardState()
    st.add([{"type": "rect", "id": "a", "x": 100, "y": 100, "w": 300, "h": 200},
            {"type": "rect", "id": "b", "x": 250, "y": 150, "w": 300, "h": 200},
            {"type": "rect", "id": "c", "x": 1500, "y": 50, "w": 300, "h": 100}])
    warns = st.warnings_for(["b", "c"])
    assert any("b overlaps a" in w for w in warns)
    assert any("c extends past the board edge" in w for w in warns)
    occ = st.occupancy()
    rows = occ.splitlines()
    assert len(rows) == 11  # header + 10 rows of 100px
    assert rows[2][6 + 1] == "#"  # x=100..200, y=100..200 is used
    assert "M" in rows[-1]  # the mascot corner


def test_persistence_round_trip(tmp_path):
    path = tmp_path / "board.json"
    st = BoardState(path)
    st.add([{"type": "title", "text": "Saved"}])
    st.save_now()
    again = BoardState(path)
    assert again.elements["title1"]["text"] == "Saved"


# ---------------------------------------------------------------- HTTP API

def test_draw_get_update_erase(board_server):
    url, _ = board_server
    status, res = call(url, "/api/draw", {"elements": [
        {"type": "title", "text": "Hello"},
        {"type": "panel", "id": "p", "title": "Goals", "items": ["a", {"text": "b", "done": True}]},
        {"type": "arrow", "from": "title1", "to": "p", "label": "see"},
    ], "agent": "pytest"})
    assert status == 200 and res["ok"]
    assert [e["id"] for e in res["elements"]] == ["title1", "p", "arrow1"]
    assert all(len(e["bbox"]) == 4 for e in res["elements"])

    status, board = call(url, "/api/board?props=1")
    assert board["count"] == 3
    assert board["elements"][1]["props"]["title"] == "Goals"
    assert "occupancy" in board

    status, res = call(url, "/api/update", {"id": "p", "changes": {"color": "red"}})
    assert status == 200
    status, res = call(url, "/api/erase", {"ids": ["arrow1", "ghost"]})
    assert res["erased"] == ["arrow1"] and res["unknown_ids"] == ["ghost"]
    status, res = call(url, "/api/undo", {})
    assert res["ok"]
    status, board = call(url, "/api/board")
    assert board["count"] == 3


def test_bad_requests_are_explained(board_server):
    url, _ = board_server
    status, res = call(url, "/api/draw", {"elements": [{"type": "nope"}]})
    assert status == 400 and "unknown element type" in res["error"]
    status, res = call(url, "/api/draw", {"elements": []})
    assert status == 400
    # form posts and foreign hosts are refused
    req = urllib.request.Request(url + "/api/clear", data=b"x=1", headers={"Content-Type": "text/plain"})
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req)
    assert err.value.code == 415
    status, res = call(url, "/api/health", headers={"Host": "evil.example.com"})
    assert status == 403


def test_static_viewer_and_guide(board_server):
    url, _ = board_server
    status, html = call(url, "/")
    assert status == 200 and b"AI Whiteboard" in html
    for name in ("app.js", "render.js", "icons.js", "style.css"):
        assert call(url, "/static/" + name)[0] == 200
    assert call(url, "/static/../server.py")[0] == 404
    status, res = call(url, "/api/guide?format=json")
    assert "rocket" in res["guide"] and "mindmap" in res["guide"]


def test_guide_lists_the_icon_library():
    names = icon_names()
    assert len(names) >= 80
    assert {"lightbulb", "rocket", "target", "calendar", "robot"} <= set(names)
    assert "Icons: lightbulb" in guide()


# ---------------------------------------------------------------- live events & snapshots

def test_events_stream_board_changes(board_server):
    url, hub = board_server
    reader = EventReader(url)
    try:
        hello = reader.wait_for("hello")
        assert hello["state"]["settings"]["width"] == 1600
        call(url, "/api/draw", {"elements": [{"type": "text", "text": "live!", "x": 5, "y": 5}]})
        add = reader.wait_for("add")
        assert add["elements"][0]["text"] == "live!"
        call(url, "/api/say", {"text": "Hi there"})
        assert reader.wait_for("say")["text"] == "Hi there"
        call(url, "/api/pause", {"seconds": 99})
        assert reader.wait_for("pause")["seconds"] == 30  # clamped
        call(url, "/api/clear", {"background": "chalkboard"})
        assert reader.wait_for("clear")["settings"]["background"] == "chalkboard"
    finally:
        reader.close(hub)


def test_viewer_layout_reports_replace_estimates(board_server):
    url, hub = board_server
    reader = EventReader(url)
    try:
        reader.wait_for("hello")

        def fake_viewer():
            add = reader.wait_for("add")
            call(url, "/api/layout", {"bboxes": {add["elements"][0]["id"]: [11, 22, 333, 44]}})

        threading.Thread(target=fake_viewer, daemon=True).start()
        status, res = call(url, "/api/draw", {"elements": [{"type": "text", "id": "t", "text": "measure me", "x": 11, "y": 22}]})
        assert res["elements"][0]["bbox"] == [11, 22, 333, 44]
        assert "bbox_estimated" not in res["elements"][0]
    finally:
        reader.close(hub)


def test_snapshot_round_trip_through_a_viewer(board_server):
    url, hub = board_server
    status, res = call(url, "/api/snapshot?timeout=1")
    assert status == 503 and "viewer" in res["error"]
    reader = EventReader(url)
    png = b"\x89PNG\r\n\x1a\nfake"
    try:
        reader.wait_for("hello")

        def fake_viewer():
            ev = reader.wait_for("snapshot")
            data = "data:image/png;base64," + base64.b64encode(png).decode()
            call(url, "/api/snapshot", {"req": ev["req"], "data": data})

        threading.Thread(target=fake_viewer, daemon=True).start()
        status, body = call(url, "/api/snapshot?timeout=5")
        assert status == 200 and body == png
    finally:
        reader.close(hub)


def test_save_and_load(board_server):
    url, _ = board_server
    call(url, "/api/draw", {"elements": [{"type": "title", "text": "Keep me"}]})
    assert call(url, "/api/save", {"name": "keeper"})[1]["ok"]
    call(url, "/api/clear", {})
    status, res = call(url, "/api/load", {"name": "keeper"})
    assert res["count"] == 1
    assert call(url, "/api/saves")[1]["saved"] == ["keeper"]
    status, res = call(url, "/api/load", {"name": "missing"})
    assert status == 400


# ---------------------------------------------------------------- demos

@pytest.mark.parametrize("name", sorted(DEMOS))
def test_demo_boards_are_valid(name, board_server):
    from whiteboard.__main__ import run_ops
    from whiteboard.client import Whiteboard

    url, hub = board_server
    wb = Whiteboard(url, agent="pytest", autostart=False, open_browser=False)
    results = run_ops(wb, DEMOS[name]())
    assert all(r.get("ok", True) for r in results if isinstance(r, dict))
    assert len(hub.state.elements) > 3


# ---------------------------------------------------------------- MCP server

def test_mcp_server_over_stdio(board_server):
    url, hub = board_server
    env = dict(os.environ, WHITEBOARD_URL=url, WHITEBOARD_NO_BROWSER="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen([sys.executable, str(ROOT / "whiteboard" / "mcp_server.py")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

    def rpc(msg):
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        proc.stdin.flush()
        if "id" not in msg:
            return None
        return json.loads(proc.stdout.readline())

    try:
        init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "claude-code", "version": "1"}}})
        assert init["result"]["serverInfo"]["name"] == "ai-whiteboard"
        assert init["result"]["protocolVersion"] == "2025-06-18"
        rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})

        tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        names = {t["name"] for t in tools}
        assert {"draw", "look", "say", "get_board", "erase", "update", "guide",
                "reorder", "arrange", "wait_for_choice"} <= names

        res = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "draw", "arguments": {"elements": [{"type": "sticky", "text": "from MCP"}]}}})["result"]
        assert not res["isError"]
        payload = json.loads(res["content"][0]["text"])
        assert payload["elements"][0]["id"] == "sticky1"
        assert hub.state.elements["sticky1"]["agent"] == "Claude"

        bad = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "draw", "arguments": {"elements": [{"type": "warp"}]}}})["result"]
        assert bad["isError"] and "unknown element type" in bad["content"][0]["text"]

        rpc({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {
            "name": "draw", "arguments": {"elements": [{"type": "button", "id": "ok", "text": "OK", "value": 1}]}}})
        call(url, "/api/choose", {"id": "ok"})
        res = rpc({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
            "name": "wait_for_choice", "arguments": {"timeout": 5}}})["result"]
        assert not res["isError"]
        assert json.loads(res["content"][0]["text"])["chosen"]["value"] == 1
        res = rpc({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {
            "name": "reorder", "arguments": {"ids": ["sticky1"], "to": "front"}}})["result"]
        assert json.loads(res["content"][0]["text"])["order"] == ["ok", "sticky1"]

        missing = rpc({"jsonrpc": "2.0", "id": 5, "method": "nope"})
        assert missing["error"]["code"] == -32601
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
        proc.stdout.close()
        proc.stderr.close()


# ---------------------------------------------------------------- stacking order

def test_reorder_front_back_and_undo():
    st = BoardState()
    st.add([{"type": "rect", "id": i, "x": n * 300, "y": 0} for n, i in enumerate("abc")])
    assert st.reorder(["a"], "front") == ["b", "c", "a"]
    assert st.reorder(["c", "a"], "back") == ["c", "a", "b"]
    assert list(st.elements) == ["c", "a", "b"]
    assert [e["id"] for e in st.to_dict()["elements"]] == ["c", "a", "b"]
    assert st.undo()
    assert list(st.elements) == ["b", "c", "a"]
    with pytest.raises(BoardError, match="no element with id 'zz'"):
        st.reorder(["a", "zz"], "front")
    with pytest.raises(BoardError, match="front"):
        st.reorder(["a"], "sideways")
    assert list(st.elements) == ["b", "c", "a"]


def test_order_endpoint_emits_an_event(board_server):
    url, hub = board_server
    reader = EventReader(url)
    try:
        reader.wait_for("hello")
        call(url, "/api/draw", {"elements": [{"type": "rect", "id": i, "x": n * 300, "y": 0}
                                             for n, i in enumerate("abc")]})
        status, res = call(url, "/api/order", {"ids": ["c"], "to": "back"})
        assert status == 200 and res["order"] == ["c", "a", "b"]
        ev = reader.wait_for("order")
        assert (ev["ids"], ev["to"]) == (["c"], "back")
        _, state = call(url, "/api/state")
        assert [e["id"] for e in state["elements"]] == ["c", "a", "b"]
        status, res = call(url, "/api/order", {"ids": ["nope"]})
        assert status == 400 and "nope" in res["error"]
    finally:
        reader.close(hub)


# ---------------------------------------------------------------- align & distribute

def _three_rects():
    st = BoardState()
    st.add([{"type": "rect", "id": "a", "x": 100, "y": 100, "w": 100, "h": 60},
            {"type": "rect", "id": "b", "x": 260, "y": 180, "w": 200, "h": 100},
            {"type": "rect", "id": "c", "x": 700, "y": 140, "w": 100, "h": 80}])
    return st


def test_arrange_aligns_edges_and_centres():
    st = _three_rects()
    changed, skipped = st.arrange(["a", "b", "c"], align="top")
    assert not skipped and {e["id"] for e in changed} == {"b", "c"}
    assert [st.bbox_of(i)[1] for i in "abc"] == [100, 100, 100]
    assert [st.bbox_of(i)[0] for i in "abc"] == [100, 260, 700]  # x untouched
    st.arrange(["a", "b", "c"], align="bottom")
    assert [st.bbox_of(i)[1] + st.bbox_of(i)[3] for i in "abc"] == [200, 200, 200]
    st.arrange(["a", "b", "c"], align="center")
    assert [st.bbox_of(i)[0] + st.bbox_of(i)[2] / 2 for i in "abc"] == [450, 450, 450]
    st.arrange(["a", "b", "c"], align="right")
    assert [st.bbox_of(i)[0] + st.bbox_of(i)[2] for i in "abc"] == [550, 550, 550]


def test_arrange_distributes_evenly_or_with_a_gap():
    st = _three_rects()
    st.arrange(["c", "a", "b"], distribute="horizontal")
    a, b, c = (st.bbox_of(i) for i in "abc")
    assert (a[0], c[0]) == (100, 700)  # the outer two stay put
    assert b[0] - (a[0] + a[2]) == c[0] - (b[0] + b[2]) == 150
    st.arrange(["a", "b", "c"], distribute="horizontal", gap=30)
    a, b, c = (st.bbox_of(i) for i in "abc")
    assert (a[0], b[0], c[0]) == (100, 230, 460)
    st.arrange(["a", "b", "c"], distribute="vertical", gap=10)
    a, b, c = (st.bbox_of(i) for i in "abc")
    assert (a[1], c[1], b[1]) == (100, 170, 260)
    with pytest.raises(BoardError, match="three or more"):
        st.arrange(["a", "b"], distribute="horizontal")
    with pytest.raises(BoardError, match="align"):
        st.arrange(["a", "b"])
    with pytest.raises(BoardError, match="no element"):
        st.arrange(["a", "ghost"], align="left")


def test_arrange_handles_other_anchors_and_is_one_undo_step():
    st = BoardState()
    st.add([{"type": "rect", "id": "r", "cx": 200, "cy": 200, "w": 100, "h": 100},
            {"type": "circle", "id": "o", "x": 500, "y": 400, "r": 40},
            {"type": "line", "id": "l", "points": [[700, 600], [800, 640]], "width": 4},
            {"type": "rect", "id": "p", "x": 900, "y": 50, "w": 80, "h": 80},
            {"type": "arrow", "id": "ar", "from": "r", "to": "o"}])
    before = {i: st.bbox_of(i) for i in "rolp"}
    changed, skipped = st.arrange(["r", "o", "l", "p", "ar"], align="top", distribute="horizontal", gap=20)
    assert skipped == ["ar"] and "ar" not in {e["id"] for e in changed}
    boxes = [st.bbox_of(i) for i in "rolp"]
    assert all(abs(b[1] - 50) < 0.01 for b in boxes)
    for left, right in zip(boxes, boxes[1:]):
        assert abs(right[0] - (left[0] + left[2]) - 20) < 0.01
    assert all(abs(st.bbox_of(i)[k] - before[i][k]) < 0.01 for i in "rolp" for k in (2, 3))  # sizes kept
    assert "cx" in st.elements["r"] and st.elements["o"]["r"] == 40
    assert st.undo()
    assert {i: st.bbox_of(i) for i in "rolp"} == before


def test_arrange_endpoint_clears_overlap_warnings(board_server):
    url, hub = board_server
    _, res = call(url, "/api/draw", {"elements": [
        {"type": "sticky", "id": f"s{n}", "x": 100 + n * 90, "y": 200 + n * 40, "w": 240, "text": f"Note {n}"}
        for n in range(3)]})
    assert any("overlaps" in w for w in res["warnings"])
    status, res = call(url, "/api/arrange", {"ids": ["s0", "s1", "s2"], "align": "top",
                                             "distribute": "horizontal", "gap": 40})
    assert status == 200 and "warnings" not in res
    assert res["moved"] == ["s1", "s2"]
    boxes = [e["bbox"] for e in res["elements"]]
    assert [b[1] for b in boxes] == [200, 200, 200]
    assert [boxes[i + 1][0] - boxes[i][0] - boxes[i][2] for i in range(2)] == [40, 40]
    status, res = call(url, "/api/arrange", {"ids": ["s0", "s1"], "gap": 5})
    assert status == 400


# ---------------------------------------------------------------- asking the user

BUTTONS = [{"type": "button", "id": "yes", "text": "Yes", "group": "q", "value": {"ship": True}},
           {"type": "choice", "id": "no", "text": "No", "group": "q"},
           {"type": "button", "id": "other", "text": "Unrelated"}]


def test_button_element_validates_and_marks_one_per_group():
    st = BoardState()
    els, _ = st.add(BUTTONS)
    assert [e["type"] for e in els] == ["button"] * 3
    w, h = st.bbox_of("yes")[2:]
    assert 60 < w < 200 and 30 < h < 80
    with pytest.raises(BoardError, match="button needs 'text'"):
        st.add([{"type": "button"}])
    steps = len(st.history)
    st.choose("other")
    el, changed = st.choose("yes")
    assert el["chosen"] is True and [e["id"] for e in changed] == ["yes"]
    _, changed = st.choose("no")
    assert {e["id"] for e in changed} == {"yes", "no"}
    assert "chosen" not in st.elements["yes"] and st.elements["no"]["chosen"]
    assert st.elements["other"]["chosen"]  # another question is left alone
    with pytest.raises(BoardError, match="not a button"):
        st.choose("ghost")
    # clicks are not undo steps: undo skips them and reverses the last real edit
    assert len(st.history) == steps
    st.update("other", {"text": "Renamed"})
    assert st.undo()
    assert st.elements["other"]["text"] == "Unrelated"
    assert st.elements["no"]["chosen"] and "chosen" not in st.elements["yes"]


def test_wait_for_choice_returns_the_click(board_server):
    url, hub = board_server
    call(url, "/api/draw", {"elements": BUTTONS + [{"type": "rect", "id": "r", "x": 900, "y": 600}]})
    got = {}
    waiter = threading.Thread(target=lambda: got.update(call(url, "/api/choice?timeout=8")[1]))
    waiter.start()
    time.sleep(0.3)
    assert not got  # still waiting
    status, res = call(url, "/api/choose", {"id": "yes"})
    assert status == 200 and res["chosen"]["id"] == "yes"
    waiter.join(5)
    assert got["chosen"]["id"] == "yes" and got["chosen"]["text"] == "Yes"
    assert got["chosen"]["value"] == {"ship": True} and got["chosen"]["group"] == "q"

    # a click made before anyone waits is kept; a change of mind means the last click wins
    call(url, "/api/choose", {"id": "yes"})
    call(url, "/api/choose", {"id": "no"})
    call(url, "/api/choose", {"id": "other"})
    _, res = call(url, "/api/choice?timeout=2&group=q")
    assert res["chosen"]["id"] == "no" and res["chosen"]["value"] == "No"
    _, res = call(url, "/api/choice?timeout=2&ids=other,yes")
    assert res["chosen"]["id"] == "other"

    started = time.time()
    _, res = call(url, "/api/choice?timeout=0.3")
    assert res["chosen"] is None and res["timed_out"] and time.time() - started < 3

    status, res = call(url, "/api/choose", {"id": "r"})
    assert status == 400 and "not a button" in res["error"]
    # clicks on a button that has since been erased are dropped
    call(url, "/api/choose", {"id": "other"})
    call(url, "/api/erase", {"ids": ["other"]})
    assert call(url, "/api/choice?timeout=0.2")[1]["chosen"] is None


def test_choosing_emits_events_and_marks_the_button(board_server):
    url, hub = board_server
    reader = EventReader(url)
    try:
        reader.wait_for("hello")
        call(url, "/api/draw", {"elements": BUTTONS})
        reader.wait_for("add")
        call(url, "/api/choose", {"id": "yes"})
        up = reader.wait_for("update")
        assert [(e["id"], e.get("chosen")) for e in up["elements"]] == [("yes", True)]
        ev = reader.wait_for("choice")
        assert (ev["id"], ev["text"], ev["group"]) == ("yes", "Yes", "q")
        reader.events.clear()  # wait_for returns the first match, so drop the first click's events
        call(url, "/api/choose", {"id": "no"})
        up = reader.wait_for("update")
        assert {e["id"]: e.get("chosen") for e in up["elements"]} == {"yes": None, "no": True}
        assert reader.wait_for("choice")["id"] == "no"
        assert "button" in guide() and "wait_for_choice" in guide()
    finally:
        reader.close(hub)


def test_clicks_do_not_outlive_their_buttons(board_server):
    url, hub = board_server
    # undo reverses the draw that made the buttons: the queued click goes with them
    call(url, "/api/draw", {"elements": BUTTONS})
    call(url, "/api/choose", {"id": "no"})
    call(url, "/api/undo", {})
    assert "no" not in hub.state.elements
    assert call(url, "/api/choice?timeout=0.2")[1]["chosen"] is None

    # an undo that leaves the button on the board keeps its click
    call(url, "/api/draw", {"elements": BUTTONS})
    call(url, "/api/draw", {"elements": [{"type": "rect", "id": "r", "x": 900, "y": 600}]})
    call(url, "/api/choose", {"id": "yes"})
    call(url, "/api/undo", {})
    assert call(url, "/api/choice?timeout=0.2")[1]["chosen"]["id"] == "yes"

    # loading a board drops clicks made on the previous one, even when it has the same ids
    call(url, "/api/save", {"name": "question"})
    call(url, "/api/choose", {"id": "no"})
    call(url, "/api/load", {"name": "question"})
    assert "no" in hub.state.elements
    assert call(url, "/api/choice?timeout=0.2")[1]["chosen"] is None


# ---------------------------------------------------------------- layout report validation

NAN, INF = float("nan"), float("inf")


@pytest.mark.parametrize("box", [
    [NAN, 0, 10, 10], [0, NAN, 10, 10], [0, 0, NAN, 10], [0, 0, 10, NAN],
    [INF, 0, 10, 10], [0, -INF, 10, 10], [0, 0, INF, 10], [0, 0, 10, -INF],
    ["nan", 0, 10, 10], [0, 0, "inf", 10],
    [0, 0, 0, 10], [0, 0, 10, 0], [0, 0, -10, 10], [0, 0, 10, -10],
    [0, 0, None, 10], [0, 0, "wide", 10], [0, 0, [1], 10], [10 ** 400, 0, 10, 10],
])
def test_set_bboxes_skips_invalid_boxes(box):
    st = BoardState()
    st.add([{"type": "rect", "id": "r", "x": 10, "y": 10, "w": 100, "h": 50}])
    estimate = st.bbox_of("r")
    assert st.set_bboxes({"r": box}) == 0
    assert "r" not in st.bboxes
    assert st.bbox_of("r") == estimate
    assert st.summary("r")["bbox_estimated"] is True

    assert st.set_bboxes({"r": [1, 2, 30, 40]}) == 1
    assert st.set_bboxes({"r": box}) == 0
    assert st.bboxes["r"] == [1.0, 2.0, 30.0, 40.0]
    st.describe()


def test_set_bboxes_keeps_valid_boxes_next_to_bad_ones():
    st = BoardState()
    st.add([{"type": "rect", "id": "a", "x": 10, "y": 10, "w": 100, "h": 50},
            {"type": "rect", "id": "b", "x": 300, "y": 10, "w": 100, "h": 50},
            {"type": "rect", "id": "c", "x": 600, "y": 10, "w": 100, "h": 50}])
    stored = st.set_bboxes({
        "a": [NAN, 0, 10, 10],
        "b": [1.26, "2.04", 30.15, True],
        "c": [0, 0, 0.04, 0.01],
        "ghost": [0, 0, 10, 10],
        "d": [0, 0, 10],
    })
    assert stored == 2
    assert "a" not in st.bboxes and "ghost" not in st.bboxes
    assert st.bboxes["b"] == [1.3, 2.0, 30.1, 1.0]
    assert st.bboxes["c"] == [0.0, 0.0, 0.04, 0.01]
    assert st.set_bboxes({"a": "not a list", "b": (0, 0, 5, 5)}) == 0
    board = st.describe()
    assert board["count"] == 3


def test_bad_layout_report_does_not_break_the_board_api(board_server):
    url, _ = board_server
    status, res = call(url, "/api/draw", {"elements": [{"type": "rect", "id": "r", "x": 10, "y": 10, "w": 100, "h": 50}]})
    assert status == 200 and res["ok"]

    for box in ([NAN, 0, 10, 10], [0, 0, INF, 10], [0, 0, -10, 10], [0, 0, 10, -10]):
        status, res = call(url, "/api/layout", {"bboxes": {"r": box}})
        assert status == 200 and res["stored"] == 0

    status, board = call(url, "/api/board")
    assert status == 200
    [el] = board["elements"]
    assert el["id"] == "r" and el["bbox_estimated"] is True
    assert len(el["bbox"]) == 4 and el["bbox"][2] > 0 and el["bbox"][3] > 0


# ---------------------------------------------------------------- bad input (issue #6)

def post_raw(url, path, data):
    req = urllib.request.Request(url + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_table_rows_must_be_a_list(board_server):
    url, hub = board_server
    status, res = call(url, "/api/draw", {"type": "table", "rows": 5})
    assert status == 400 and res["ok"] is False and "rows" in res["error"]
    assert hub.state.elements == {}

    # empty and scalar rows keep working
    status, res = call(url, "/api/draw", {"elements": [
        {"type": "table", "id": "t", "rows": [["a", "b"], "c"]},
        {"type": "table", "id": "e", "rows": []},
    ]})
    assert status == 200 and res["ok"]
    status, res = call(url, "/api/update", {"id": "t", "changes": {"rows": "oops"}})
    assert status == 400 and "rows" in res["error"]
    assert hub.state.elements["t"]["rows"] == [["a", "b"], "c"]


def test_erase_needs_an_object_body(board_server):
    url, _ = board_server
    status, res = call(url, "/api/erase", [1, 2])
    assert status == 400 and res["ok"] is False
    assert "object" in res["error"] and "array" in res["error"]
    status, res = post_raw(url, "/api/erase", b"null")
    assert status == 400 and "object" in res["error"]


def test_object_routes_reject_other_json(board_server):
    url, _ = board_server
    for path in ("/api/settings", "/api/point", "/api/load", "/api/layout", "/api/clear", "/api/say"):
        for body in ([1], "text", 3):
            status, res = call(url, path, body)
            assert status == 400 and res["ok"] is False and "object" in res["error"], (path, body)


def test_erase_region_must_be_numbers(board_server):
    url, _ = board_server
    status, res = call(url, "/api/erase", {"region": ["a", 0, 1, 1]})
    assert status == 400 and res["ok"] is False
    assert "region[0]" in res["error"] and "number" in res["error"]
    status, res = call(url, "/api/erase", {"ids": 5})
    assert status == 400 and "ids" in res["error"]


def test_point_coordinates_must_be_numbers(board_server):
    url, _ = board_server
    status, res = call(url, "/api/point", {"x": "abc", "y": 1})
    assert status == 400 and res["ok"] is False
    assert "'x'" in res["error"] and "number" in res["error"]
    status, res = call(url, "/api/point", {"x": 10, "y": 20})
    assert status == 200 and res["ok"]


def test_snapshot_width_must_be_a_number(board_server):
    url, hub = board_server
    assert hub.viewers() == 0
    status, res = call(url, "/api/snapshot?width=abc")
    assert status == 400 and res["ok"] is False and "width" in res["error"]
    status, res = call(url, "/api/snapshot?width=viewer&timeout=1")
    assert status == 400 and "width" in res["error"]
    status, res = call(url, "/api/snapshot?timeout=soon")
    assert status == 400 and "timeout" in res["error"]
    status, res = call(url, "/api/snapshot")  # valid parameters, no viewer
    assert status == 503


@pytest.mark.parametrize("content, expected", [
    ("{not json", "not valid JSON"),
    ("[1, 2, 3]", "expected a JSON object"),
    ('{"elements": {"a": 1}}', "'elements' must be an array"),
    ('{"settings": [1]}', "'settings' must be an object"),
])
def test_loading_a_corrupt_saved_board_is_explained(board_server, content, expected):
    url, hub = board_server
    call(url, "/api/draw", {"elements": [{"type": "title", "id": "keep", "text": "Keep me"}]})
    folder = hub.data_dir / "saved"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "broken.json").write_text(content, encoding="utf-8")
    before, history = hub.state.to_dict(), len(hub.state.history)

    status, res = call(url, "/api/load", {"name": "broken"})
    assert status == 400 and res["ok"] is False
    assert "broken" in res["error"] and expected in res["error"]
    assert hub.state.to_dict() == before
    assert len(hub.state.history) == history


def test_draw_and_update_still_take_arrays_and_objects(board_server):
    url, hub = board_server
    status, res = call(url, "/api/draw", [{"type": "rect", "id": "a", "x": 0, "y": 0, "w": 10, "h": 10}])
    assert status == 200 and res["ok"]
    status, res = call(url, "/api/draw", {"type": "rect", "id": "b", "x": 50, "y": 0, "w": 10, "h": 10})
    assert status == 200 and res["ok"]
    status, res = call(url, "/api/update", [{"id": "a", "changes": {"color": "red"}}])
    assert status == 200 and hub.state.elements["a"]["color"] == "red"
    status, res = call(url, "/api/update", {"id": "b", "color": "blue"})
    assert status == 200 and hub.state.elements["b"]["color"] == "blue"
    status, res = call(url, "/api/update", 7)
    assert status == 400 and res["ok"] is False
    status, res = call(url, "/api/undo", [])  # a route that ignores its body
    assert status == 200


@pytest.mark.parametrize("exc", [RuntimeError("boom"), ValueError("boom"), TypeError("boom"),
                                 ConnectionResetError("boom"), BrokenPipeError("boom"),
                                 ConnectionAbortedError("boom")])
def test_unexpected_errors_are_logged_500s(board_server, monkeypatch, capsys, exc):
    url, hub = board_server

    def broken(*args, **kwargs):
        raise exc

    monkeypatch.setattr(hub, "undo", broken)
    monkeypatch.setattr(hub, "saved", broken)
    for path, body in (("/api/undo", {}), ("/api/saves", None)):
        status, res = call(url, path, body)
        assert status == 500, path
        assert res["ok"] is False and res["error"]
        assert type(exc).__name__ in res["error"]
    err = capsys.readouterr().err
    assert "Traceback" in err and "boom" in err
    assert "/api/undo" in err and "/api/saves" in err
    # the server keeps answering
    assert call(url, "/api/health")[0] == 200


def test_client_reports_http_errors_without_retrying(board_server, monkeypatch):
    from whiteboard.client import Whiteboard, WhiteboardError

    url, hub = board_server
    calls = []
    real_erase = hub.erase

    def counting_erase(*args, **kwargs):
        calls.append(args)
        return real_erase(*args, **kwargs)

    monkeypatch.setattr(hub, "erase", counting_erase)
    wb = Whiteboard(url, autostart=False, open_browser=False)
    with pytest.raises(WhiteboardError, match=r"region\[0\]"):
        wb.erase(region=["a", 0, 1, 1])
    assert len(calls) == 1

    def broken_undo():
        calls.append("undo")
        raise RuntimeError("boom")

    monkeypatch.setattr(hub, "undo", broken_undo)
    with pytest.raises(WhiteboardError, match="internal server error"):
        wb.undo()
    assert calls.count("undo") == 1


def test_client_still_retries_a_lost_connection(board_server, monkeypatch):
    from whiteboard.client import Whiteboard

    url, _ = board_server
    wb = Whiteboard(url, autostart=False, open_browser=False)
    real_request = wb._request
    seen = []

    def flaky(method, path, body=None, **kw):
        if path != "/api/health":
            seen.append(path)
            if len(seen) == 1:
                raise ConnectionError("server went away")
        return real_request(method, path, body, **kw)

    monkeypatch.setattr(wb, "_request", flaky)
    assert wb.undo()["ok"] is False  # nothing to undo, but it answered
    assert seen == ["/api/undo", "/api/undo"]


@pytest.mark.parametrize("element, expected", [
    ({"type": "table", "id": "t", "rows": 5}, "rows"),
    ({"type": "rect", "id": ["a"], "x": 0, "y": 0, "w": 10, "h": 10}, "'id' must be a non-empty string"),
    ({"type": "rect", "x": 0, "y": 0, "w": 10, "h": 10}, "'id' must be a non-empty string"),
    ({"type": "rect", "id": "r", "x": "left", "y": 0, "w": 10, "h": 10}, "'x'"),
    ({"type": "nonsense", "id": "n"}, "unknown element type"),
    ("just text", "expected an object"),
])
def test_loading_a_saved_board_with_bad_elements_is_explained(board_server, element, expected):
    url, hub = board_server
    call(url, "/api/draw", {"elements": [{"type": "title", "id": "keep", "text": "Keep me"}]})
    folder = hub.data_dir / "saved"
    folder.mkdir(parents=True, exist_ok=True)
    good = {"type": "rect", "id": "ok", "x": 0, "y": 0, "w": 10, "h": 10}
    (folder / "broken.json").write_text(json.dumps({"elements": [good, element]}), encoding="utf-8")
    before, history = hub.state.to_dict(), len(hub.state.history)

    status, res = call(url, "/api/load", {"name": "broken"})
    assert status == 400 and res["ok"] is False
    assert "broken" in res["error"] and "index 1" in res["error"] and expected in res["error"]
    assert hub.state.to_dict() == before
    assert len(hub.state.history) == history
    status, res = call(url, "/api/board")
    assert status == 200 and [e["id"] for e in res["elements"]] == ["keep"]


def test_loading_a_saved_board_with_bad_settings_is_explained(board_server):
    url, hub = board_server
    folder = hub.data_dir / "saved"
    folder.mkdir(parents=True, exist_ok=True)
    for settings, expected in (({"width": "wide"}, "width"), ({"height": 10}, "height"),
                               ({"background": "lava"}, "background")):
        (folder / "broken.json").write_text(json.dumps({"settings": settings}), encoding="utf-8")
        before = hub.state.to_dict()
        status, res = call(url, "/api/load", {"name": "broken"})
        assert status == 400 and expected in res["error"], settings
        assert hub.state.to_dict() == before and hub.state.history == []


def test_saved_boards_still_load_and_undo(board_server):
    url, hub = board_server
    call(url, "/api/draw", {"elements": [
        {"type": "table", "id": "t", "x": 10, "y": 10, "rows": [["a", "b"], ["c", "d"]]},
        {"type": "arrow", "id": "ar", "from": [0, 0], "to": {"id": "t", "side": "left"}},
        {"type": "button", "id": "b", "text": "Go", "x": 300, "y": 300},
    ]})
    call(url, "/api/settings", {"background": "grid", "width": 2000})
    assert call(url, "/api/save", {"name": "demo"})[0] == 200
    saved = hub.state.to_dict()
    call(url, "/api/clear", {})
    status, res = call(url, "/api/load", {"name": "demo"})
    assert status == 200 and res["count"] == 3
    assert hub.state.to_dict()["elements"] == saved["elements"]
    assert hub.state.settings["background"] == "grid" and hub.state.settings["width"] == 2000
    assert call(url, "/api/board")[0] == 200
    assert call(url, "/api/undo", {})[1]["ok"] is True
    assert hub.state.elements == {}


def test_snapshot_timeout_must_be_a_finite_number(board_server):
    url, hub = board_server
    hub.connect()  # a viewer that never answers
    for value in ("nan", "inf", "-inf", "soon"):
        status, res = call(url, f"/api/snapshot?timeout={value}")
        assert status == 400, value
        assert "timeout" in res["error"] and "viewer" not in res["error"]
        assert hub.snapshots == {}
    for value in ("0", "-1"):  # as on main: no time to answer, so the viewer did not answer in time
        status, res = call(url, f"/api/snapshot?timeout={value}")
        assert status == 503, value
        assert "in time" in res["error"]
        assert hub.snapshots == {}


def _prompt_viewer(url, reader, png):
    def answer():
        ev = reader.wait_for("snapshot")
        data = "data:image/png;base64," + base64.b64encode(png).decode()
        call(url, "/api/snapshot", {"req": ev["req"], "data": data})

    threading.Thread(target=answer, daemon=True).start()


@pytest.mark.parametrize("value", ["601", "1e20"])
def test_a_long_snapshot_timeout_is_allowed(board_server, value):
    url, hub = board_server
    reader = EventReader(url)
    png = b"\x89PNG\r\n\x1a\nfake"
    try:
        reader.wait_for("hello")
        _prompt_viewer(url, reader, png)
        status, body = call(url, f"/api/snapshot?timeout={value}")
        assert status == 200 and body == png
        assert hub.snapshots == {}
    finally:
        reader.close(hub)


def test_a_huge_snapshot_timeout_does_not_overflow_the_wait(tmp_path):
    from whiteboard.server import Hub

    hub = Hub(tmp_path, persist=False)
    hub.connect()
    png = b"\x89PNG\r\n\x1a\nfake"
    waits = []

    def answering_wait(timeout=None):
        waits.append(timeout)
        threading.Lock().acquire(timeout=timeout)  # free lock: returns at once, but overflows like a real wait
        rid = next(iter(hub.snapshots))
        hub.snapshots[rid] = png
        return True

    hub.cond.wait = answering_wait
    assert hub.request_snapshot(timeout=1e20) == png
    assert waits and all(w <= threading.TIMEOUT_MAX for w in waits)
    assert hub.snapshots == {}


def test_a_failed_snapshot_wait_is_not_left_pending(tmp_path, monkeypatch):
    from whiteboard.server import Hub

    hub = Hub(tmp_path, persist=False)
    hub.connect()

    def broken_wait(timeout=None):
        raise RuntimeError("wait failed")

    monkeypatch.setattr(hub.cond, "wait", broken_wait)
    with pytest.raises(RuntimeError):
        hub.request_snapshot(timeout=1)
    assert hub.snapshots == {}
    monkeypatch.undo()
    with pytest.raises(BoardError, match="in time"):
        hub.request_snapshot(timeout=0.05)
    assert hub.snapshots == {}


@pytest.mark.parametrize("content, expected", [
    ('{"settings": [], "elements": {}}', "'settings' must be an object"),
    ('{"settings": []}', "'settings' must be an object"),
    ('{"settings": ""}', "'settings' must be an object"),
    ('{"elements": {}}', "'elements' must be an array"),
    ('{"elements": 0}', "'elements' must be an array"),
    ('{"elements": ""}', "'elements' must be an array"),
])
def test_saved_board_fields_of_the_wrong_type_are_not_defaulted(board_server, content, expected):
    url, hub = board_server
    call(url, "/api/draw", {"elements": [{"type": "title", "id": "keep", "text": "Keep me"}]})
    folder = hub.data_dir / "saved"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "broken.json").write_text(content, encoding="utf-8")
    before, history = hub.state.to_dict(), len(hub.state.history)

    status, res = call(url, "/api/load", {"name": "broken"})
    assert status == 400 and res["ok"] is False and expected in res["error"]
    assert hub.state.to_dict() == before
    assert len(hub.state.history) == history


def test_saved_board_fields_that_are_missing_or_null_get_defaults(board_server):
    url, hub = board_server
    folder = hub.data_dir / "saved"
    folder.mkdir(parents=True, exist_ok=True)
    for content in ('{}', '{"settings": null, "elements": null}'):
        call(url, "/api/draw", {"type": "rect", "id": "r", "x": 0, "y": 0, "w": 10, "h": 10})
        (folder / "empty.json").write_text(content, encoding="utf-8")
        status, res = call(url, "/api/load", {"name": "empty"})
        assert status == 200 and res["count"] == 0, content


@pytest.mark.parametrize("arrows", [
    [{"type": "arrow", "id": "a", "from": "a", "to": [0, 0]}],
    [{"type": "arrow", "id": "a", "from": [0, 0], "to": {"id": "a", "side": "left"}}],
    [{"type": "arrow", "id": "a", "from": "b", "to": [0, 0]},
     {"type": "arrow", "id": "b", "from": [0, 0], "to": "a"}],
    [{"type": "arrow", "id": "a", "from": "r", "to": "b"},
     {"type": "arrow", "id": "b", "from": "r", "to": {"id": "c"}},
     {"type": "arrow", "id": "c", "from": "a", "to": "r"}],
])
def test_saved_board_with_cyclic_arrows_is_rejected(board_server, arrows):
    url, hub = board_server
    call(url, "/api/draw", {"elements": [{"type": "title", "id": "keep", "text": "Keep me"}]})
    folder = hub.data_dir / "saved"
    folder.mkdir(parents=True, exist_ok=True)
    rect = {"type": "rect", "id": "r", "x": 0, "y": 0, "w": 10, "h": 10}
    (folder / "loop.json").write_text(json.dumps({"elements": [rect] + arrows}), encoding="utf-8")
    before, history = hub.state.to_dict(), len(hub.state.history)

    status, res = call(url, "/api/load", {"name": "loop"})
    assert status == 400 and res["ok"] is False
    assert "loop" in res["error"] and "point back to themselves" in res["error"]
    assert hub.state.to_dict() == before
    assert len(hub.state.history) == history
    status, res = call(url, "/api/board")
    assert status == 200 and [e["id"] for e in res["elements"]] == ["keep"]


def test_saved_board_with_chained_arrows_still_loads(board_server):
    url, hub = board_server
    folder = hub.data_dir / "saved"
    folder.mkdir(parents=True, exist_ok=True)
    elements = [{"type": "rect", "id": "r", "x": 0, "y": 0, "w": 10, "h": 10},
                {"type": "arrow", "id": "a", "from": "r", "to": [100, 100]},
                {"type": "arrow", "id": "b", "from": "a", "to": "r"},
                {"type": "arrow", "id": "c", "from": "a", "to": {"id": "b"}}]
    (folder / "chain.json").write_text(json.dumps({"elements": elements}), encoding="utf-8")
    status, res = call(url, "/api/load", {"name": "chain"})
    assert status == 200 and res["count"] == 4
    assert call(url, "/api/board")[0] == 200


def test_relative_placement_past_the_input_limit_saves_and_loads(board_server):
    url, hub = board_server
    status, _ = call(url, "/api/draw", {"type": "rect", "id": "a", "x": 1000000, "y": 0, "w": 10, "h": 10})
    assert status == 200
    status, _ = call(url, "/api/draw", {"type": "rect", "id": "b", "right_of": "a", "w": 10, "h": 10})
    assert status == 200 and hub.state.elements["b"]["x"] == 1000040
    assert call(url, "/api/save", {"name": "edge"})[0] == 200
    saved = hub.state.to_dict()["elements"]
    call(url, "/api/clear", {})

    status, res = call(url, "/api/load", {"name": "edge"})
    assert status == 200 and res["count"] == 2
    assert hub.state.to_dict()["elements"] == saved
    assert call(url, "/api/board")[0] == 200


def test_saved_board_numbers_must_still_be_finite(board_server):
    url, hub = board_server
    folder = hub.data_dir / "saved"
    folder.mkdir(parents=True, exist_ok=True)
    for x in ("Infinity", "NaN", "1e400", "1" + "0" * 400):
        (folder / "inf.json").write_text(
            '{"elements": [{"type": "rect", "id": "r", "x": %s, "y": 0}]}' % x, encoding="utf-8")
        status, res = call(url, "/api/load", {"name": "inf"})
        assert status == 400 and "'x'" in res["error"] and "out of range" in res["error"], x
        assert hub.state.elements == {} and hub.state.history == []


def test_a_failed_snapshot_broadcast_is_not_left_pending(board_server, monkeypatch, capsys):
    url, hub = board_server
    hub.connect()

    def broken_broadcast(event):
        raise RuntimeError("broadcast failed")

    monkeypatch.setattr(hub, "broadcast", broken_broadcast)
    status, res = call(url, "/api/snapshot?timeout=1")
    assert status == 500 and res["ok"] is False and "RuntimeError" in res["error"]
    assert hub.snapshots == {}
    assert "broadcast failed" in capsys.readouterr().err


def test_a_write_failure_on_the_fallback_500_is_contained(capsys):
    import io
    from types import SimpleNamespace

    from whiteboard.server import Handler

    class DeadSocket(io.BytesIO):
        def write(self, data):
            raise BrokenPipeError("client went away")

    handler = Handler.__new__(Handler)  # no socket: drive _guarded directly
    handler.server = SimpleNamespace(verbose=False)
    handler.command, handler.path = "GET", "/api/board"
    handler.request_version = "HTTP/1.1"
    handler.requestline = "GET /api/board HTTP/1.1"
    handler.close_connection = False
    handler.wfile = DeadSocket()

    def broken():
        raise RuntimeError("boom")

    handler._guarded(broken)  # must not raise
    assert handler.close_connection is True
    err = capsys.readouterr().err
    assert "boom" in err and "/api/board" in err


@pytest.mark.parametrize("value", [10 ** 400, -(10 ** 400), "1e400"], ids=["huge", "-huge", "1e400"])
def test_huge_settings_numbers_are_out_of_range(board_server, value):
    url, hub = board_server
    status, res = call(url, "/api/settings", {"width": value})
    assert status == 400 and res["ok"] is False
    assert "width" in res["error"] and "out of range" in res["error"]
    assert hub.state.settings["width"] == 1600 and hub.state.history == []


@pytest.mark.parametrize("value", [10 ** 400, -(10 ** 400)], ids=["huge", "-huge"])
def test_a_huge_pause_is_out_of_range(board_server, value):
    url, hub = board_server
    status, res = call(url, "/api/pause", {"seconds": value})
    assert status == 400 and res["ok"] is False
    assert "seconds" in res["error"] and "out of range" in res["error"]
    status, res = call(url, "/api/pause", {"seconds": 2})
    assert status == 200 and res == {"ok": True, "seconds": 2.0}


def test_the_largest_drawable_image_saves_and_loads_again(board_server):
    from whiteboard.state import MAX_ELEMENT_BYTES

    url, hub = board_server
    prefix = "data:image/png;base64,"

    def image(n):
        return {"type": "image", "id": "large-image", "x": 0, "y": 0, "w": 100, "h": 100,
                "src": prefix + "A" * n}

    base = len(json.dumps(BoardState().normalize(image(0), set()), ensure_ascii=False))
    largest = image(MAX_ELEMENT_BYTES - base)
    assert len(json.dumps(BoardState().normalize(largest, set()), ensure_ascii=False)) == MAX_ELEMENT_BYTES
    too_big = image(MAX_ELEMENT_BYTES - base + 1)
    status, res = call(url, "/api/draw", {"elements": [too_big]})
    assert status == 400 and "too large" in res["error"]

    status, res = call(url, "/api/draw", {"elements": [largest]})
    assert status == 200, res
    drawn = hub.state.to_dict()["elements"]
    assert call(url, "/api/save", {"name": "big"})[0] == 200
    assert call(url, "/api/clear", {})[0] == 200
    assert hub.state.elements == {}
    status, res = call(url, "/api/load", {"name": "big"})
    assert status == 200, res
    assert hub.state.to_dict()["elements"] == drawn
