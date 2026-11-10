"""
HTTP server for the AI whiteboard (standard library only).

* Serves the browser viewer from ./static
* REST API that AIs (or anything else) call to draw: /api/draw, /api/update, ...
* Server-Sent Events stream (/api/events) that pushes every change to viewers
* Snapshot broker: /api/snapshot asks a connected viewer to render a PNG/JPEG

Bound to 127.0.0.1 by default. POST bodies must be JSON (so a random web page
cannot draw on the board without a CORS preflight, which is never granted) and
the Host header must be a local name (blocks DNS rebinding).
"""

from __future__ import annotations

import base64
import copy
import json
import mimetypes
import os
import queue
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from . import __version__
from .reference import guide
from .state import BACKGROUNDS, BoardError, BoardState, plain_text

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_PORT = 8765
MAX_BODY = 30 * 1024 * 1024
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


def static_build() -> str:
    """Fingerprint of the viewer files, so open pages can reload after an upgrade."""
    stamp = 0
    for f in STATIC_DIR.glob("*"):
        try:
            stamp = max(stamp, int(f.stat().st_mtime))
        except OSError:
            continue
    return f"{__version__}-{stamp}"


def default_data_dir() -> Path:
    """Where boards are saved: $WHITEBOARD_DATA, ./boards in a source checkout, else ~/.ai-whiteboard."""
    env = os.environ.get("WHITEBOARD_DATA")
    if env:
        return Path(env)
    checkout = Path(__file__).resolve().parent.parent
    if (checkout / "pyproject.toml").exists() or (checkout / ".git").exists() or (checkout / "aipainter").exists():
        return checkout / "boards"
    return Path.home() / ".ai-whiteboard"


class Hub:
    """Board state plus the set of connected viewers."""

    def __init__(self, data_dir: Optional[Path] = None, persist: bool = True):
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.state = BoardState(self.data_dir / "current.json" if persist else None)
        self.persist = persist
        self.clients: Dict[str, "queue.Queue[Optional[dict]]"] = {}
        self.clients_lock = threading.Lock()
        self.cond = threading.Condition()
        self.snapshots: Dict[str, Optional[bytes]] = {}
        self.choices: List[Dict[str, Any]] = []  # button clicks nobody has collected yet
        self.activity: Dict[str, Any] = {}
        self.started = time.time()

    # ---- viewers --------------------------------------------------------
    def connect(self) -> tuple:
        cid = uuid.uuid4().hex[:10]
        q: "queue.Queue[Optional[dict]]" = queue.Queue()
        with self.clients_lock:
            self.clients[cid] = q
        return cid, q

    def disconnect(self, cid: str) -> None:
        with self.clients_lock:
            self.clients.pop(cid, None)

    def viewers(self) -> int:
        with self.clients_lock:
            return len(self.clients)

    def broadcast(self, event: Dict[str, Any]) -> None:
        with self.clients_lock:
            targets = list(self.clients.values())
        for q in targets:
            q.put(event)

    def close_all(self) -> None:
        with self.clients_lock:
            for q in self.clients.values():
                q.put(None)

    # ---- layout feedback --------------------------------------------------
    def report_layout(self, boxes: Dict[str, Any]) -> int:
        n = self.state.set_bboxes(boxes)
        with self.cond:
            self.cond.notify_all()
        return n

    def wait_for_layout(self, ids: List[str], timeout: float = 1.5) -> bool:
        if not ids or not self.viewers():
            return False
        deadline = time.time() + timeout
        with self.cond:
            while True:
                if all(i in self.state.bboxes or i not in self.state.elements for i in ids):
                    return True
                left = deadline - time.time()
                if left <= 0:
                    return False
                self.cond.wait(left)

    # ---- snapshots --------------------------------------------------------
    def request_snapshot(self, fmt: str = "png", width: int = 1600, timeout: float = 8.0) -> bytes:
        if not self.viewers():
            raise BoardError("no viewer is open, so nothing can render the board. "
                             "Open the whiteboard in a browser first (open_whiteboard).")
        rid = uuid.uuid4().hex[:12]
        with self.cond:
            self.snapshots[rid] = None
        self.broadcast({"type": "snapshot", "req": rid, "format": fmt, "width": width})
        deadline = time.time() + timeout
        with self.cond:
            while self.snapshots.get(rid) is None:
                left = deadline - time.time()
                if left <= 0:
                    self.snapshots.pop(rid, None)
                    raise BoardError("the viewer did not answer the snapshot request in time")
                self.cond.wait(left)
            return self.snapshots.pop(rid)

    def deliver_snapshot(self, rid: str, data_url: str) -> bool:
        if "," not in data_url:
            return False
        raw = base64.b64decode(data_url.split(",", 1)[1])
        with self.cond:
            if rid not in self.snapshots or self.snapshots[rid] is not None:
                return False
            self.snapshots[rid] = raw
            self.cond.notify_all()
        return True

    # ---- asking the user ----------------------------------------------------
    def choose(self, eid: Any, agent: Optional[str] = None) -> Dict[str, Any]:
        """The user clicked a button: mark it and hand the click to whoever is waiting."""
        el, changed = self.state.choose(str(eid or ""))
        if changed:
            self.broadcast({"type": "update", "elements": changed, "agent": agent})
        text = plain_text(el.get("text")).strip()
        click = {"id": el["id"], "text": text, "value": el.get("value", text), "at": round(time.time(), 3)}
        if el.get("group") is not None:
            click["group"] = el["group"]
        with self.cond:
            self.choices.append(click)
            del self.choices[:-50]
            self.cond.notify_all()
        self.broadcast({"type": "choice", **click})
        return {"ok": True, "chosen": click}

    def wait_for_choice(self, ids: Optional[List[str]] = None, group: Optional[str] = None,
                        timeout: Any = 60.0) -> Dict[str, Any]:
        """Block until the user clicks a button (optionally one of `ids` or of `group`).

        A click made before the call still counts. If the user changed their mind and
        clicked several, the last click wins. Returns chosen: None on timeout.
        """
        try:
            secs = max(0.0, min(float(timeout), 600.0))
        except (TypeError, ValueError):
            raise BoardError("timeout must be a number of seconds") from None
        wanted = {str(i) for i in ids} if ids else None

        def match(c: Dict[str, Any]) -> bool:
            return (wanted is None or c["id"] in wanted) and (group is None or c.get("group") == str(group))

        deadline = time.time() + secs
        with self.cond:
            while True:
                hits = [c for c in self.choices if match(c)]
                if hits:
                    self.choices = [c for c in self.choices if not match(c)]
                    return {"ok": True, "chosen": hits[-1]}
                left = deadline - time.time()
                if left <= 0:
                    return {"ok": True, "chosen": None, "timed_out": True,
                            "note": "nobody clicked a button yet; call again to keep waiting"}
                self.cond.wait(left)

    def _forget_choices(self, ids: Optional[List[str]] = None) -> None:
        """Drop uncollected clicks on buttons that were erased or redrawn."""
        with self.cond:
            self.choices = [c for c in self.choices if c["id"] in self.state.elements
                            and not (ids and c["id"] in ids)]

    # ---- operations shared by the REST API and the MCP server ------------
    def draw(self, elements: Any, author: str = "ai", agent: Optional[str] = None,
             origin: Optional[str] = None, wait: bool = True) -> Dict[str, Any]:
        if isinstance(elements, dict):
            elements = [elements]
        added, replaced = self.state.add(elements, author=author, agent=agent)
        if replaced:
            self._forget_choices(replaced)
        self.broadcast({"type": "add", "elements": added, "replaced": replaced,
                        "agent": agent, "origin": origin})
        self._note_activity(agent, f"drew {len(added)} element(s)")
        ids = [e["id"] for e in added]
        measured = self.wait_for_layout(ids) if wait else False
        return self._result(ids, measured)

    def update(self, updates: List[Dict[str, Any]], agent: Optional[str] = None) -> Dict[str, Any]:
        changed = []
        for u in updates:
            if not isinstance(u, dict) or not u.get("id"):
                raise BoardError("each update needs an 'id'")
            changes = u.get("changes") if isinstance(u.get("changes"), dict) else {
                k: v for k, v in u.items() if k != "id"}
            changed.append(self.state.update(str(u["id"]), changes))
        self.broadcast({"type": "update", "elements": changed, "agent": agent})
        self._note_activity(agent, f"updated {len(changed)} element(s)")
        ids = [e["id"] for e in changed]
        measured = self.wait_for_layout(ids)
        return self._result(ids, measured)

    def erase(self, ids: Optional[List[str]] = None, region: Optional[List[float]] = None,
              author: Optional[str] = None, agent: Optional[str] = None,
              origin: Optional[str] = None) -> Dict[str, Any]:
        targets: List[str] = [str(i) for i in (ids or [])]
        if region is not None:
            if not isinstance(region, list) or len(region) != 4:
                raise BoardError("region must be [x, y, w, h]")
            targets += self.state.ids_in_region([float(v) for v in region], author)
        elif author and not ids:
            targets += [i for i, e in self.state.elements.items() if e.get("author") == author]
        unknown = [i for i in targets if i not in self.state.elements]
        gone = self.state.remove(list(dict.fromkeys(targets)))
        if gone:
            self._forget_choices()
            self.broadcast({"type": "remove", "ids": gone, "agent": agent, "origin": origin})
        self._note_activity(agent, f"erased {len(gone)} element(s)")
        out: Dict[str, Any] = {"ok": True, "erased": gone}
        if unknown and ids:
            out["unknown_ids"] = unknown
        return out

    def arrange(self, ids: Any, align: Optional[str] = None, distribute: Optional[str] = None,
                gap: Any = None, agent: Optional[str] = None) -> Dict[str, Any]:
        if not isinstance(ids, list):
            raise BoardError("arrange needs 'ids' (a list of element ids)")
        changed, skipped = self.state.arrange(ids, align, distribute, gap)
        if changed:
            self.broadcast({"type": "update", "elements": changed, "agent": agent})
        self._note_activity(agent, f"arranged {len(ids)} element(s)")
        kept = [str(i) for i in ids if str(i) not in skipped]
        out = self._result(kept, self.wait_for_layout(kept))
        out["moved"] = [e["id"] for e in changed]
        if skipped:
            out["skipped"] = skipped
        return out

    def reorder(self, ids: Any, to: str = "front", agent: Optional[str] = None) -> Dict[str, Any]:
        if isinstance(ids, str):
            ids = [ids]
        if not isinstance(ids, list):
            raise BoardError("reorder needs 'ids' (a list of element ids)")
        ids = [str(i) for i in ids]
        order = self.state.reorder(ids, to)
        self.broadcast({"type": "order", "ids": ids, "to": to, "agent": agent})
        self._note_activity(agent, f"moved {len(ids)} element(s) to the {to}")
        return {"ok": True, "to": to, "order": order}

    def clear(self, background: Optional[str] = None, agent: Optional[str] = None) -> Dict[str, Any]:
        n = self.state.clear()
        self._forget_choices()
        if background:
            self.state.set_settings({"background": background})
        self.broadcast({"type": "clear", "settings": self.state.settings, "agent": agent})
        self._note_activity(agent, "cleared the board")
        return {"ok": True, "erased": n, "board": self.state.settings}

    def settings(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        new = self.state.set_settings(changes)
        self.broadcast({"type": "settings", "settings": new})
        return {"ok": True, "board": new}

    def undo(self) -> Dict[str, Any]:
        ok = self.state.undo()
        if ok:
            self._forget_choices()  # the undo may have removed buttons with uncollected clicks
            self.broadcast({"type": "sync", "state": self.state.to_dict()})
        return {"ok": ok, "message": "undone" if ok else "nothing to undo"}

    def say(self, text: str, duration: Optional[float] = None, agent: Optional[str] = None,
            pause: Optional[float] = None) -> Dict[str, Any]:
        text = str(text or "").strip()
        if not text:
            raise BoardError("say needs some text")
        self.broadcast({"type": "say", "text": text[:400], "duration": duration, "agent": agent})
        if pause:
            self.pause(pause)
        return {"ok": True, "viewers": self.viewers()}

    def pause(self, seconds: Any) -> Dict[str, Any]:
        """Hold the viewers' drawing queue for a moment (pacing a presentation)."""
        try:
            secs = max(0.0, min(float(seconds), 30.0))
        except (TypeError, ValueError):
            raise BoardError("pause needs a number of seconds") from None
        self.broadcast({"type": "pause", "seconds": secs})
        return {"ok": True, "seconds": secs}

    def point(self, x: Optional[float] = None, y: Optional[float] = None, eid: Optional[str] = None,
              label: Optional[str] = None, agent: Optional[str] = None) -> Dict[str, Any]:
        if eid:
            b = self.state.bbox_of(eid)
            if not b:
                raise BoardError(f"no element with id {eid!r}")
            ev = {"type": "point", "id": eid, "bbox": b}
        elif x is not None and y is not None:
            ev = {"type": "point", "x": float(x), "y": float(y)}
        else:
            raise BoardError("point_at needs an element id or x and y")
        ev["label"] = label
        ev["agent"] = agent
        self.broadcast(ev)
        return {"ok": True}

    def save(self, name: str) -> Dict[str, Any]:
        safe = "".join(c for c in str(name) if c.isalnum() or c in "-_ ").strip()
        if not safe:
            raise BoardError("save needs a name (letters, digits, - and _)")
        path = self.state.save_now(self.data_dir / "saved" / f"{safe}.json")
        return {"ok": True, "name": safe, "path": str(path)}

    def load(self, name: str) -> Dict[str, Any]:
        safe = "".join(c for c in str(name) if c.isalnum() or c in "-_ ").strip()
        path = self.data_dir / "saved" / f"{safe}.json"
        if not safe or not path.exists():
            raise BoardError(f"no saved board named {name!r}; saved: {', '.join(self.saved()) or 'none'}")
        data = json.loads(path.read_text(encoding="utf-8"))
        with self.state.lock:
            # one undo step restores the board as it was before the load
            before = {eid: copy.deepcopy(e) for eid, e in self.state.elements.items()}
            for e in data.get("elements") or []:
                if isinstance(e, dict) and e.get("id"):
                    before.setdefault(e["id"], None)
            self.state._record(before, settings_before=dict(self.state.settings))
            self.state.load_dict(data)
        with self.cond:
            self.choices = []  # a different board: clicks on the old one answer nothing, even if ids match
        self.state.schedule_save()
        self.broadcast({"type": "sync", "state": self.state.to_dict(), "animate": True})
        return {"ok": True, "name": safe, "count": len(self.state.elements)}

    def saved(self) -> List[str]:
        folder = self.data_dir / "saved"
        return sorted(p.stem for p in folder.glob("*.json")) if folder.exists() else []

    def _result(self, ids: List[str], measured: bool) -> Dict[str, Any]:
        with self.state.lock:
            items = [self.state.summary(i) for i in ids if i in self.state.elements]
            warnings = self.state.warnings_for(ids)
        out: Dict[str, Any] = {"ok": True, "elements": items, "viewers": self.viewers()}
        if not measured:
            out["note"] = ("bboxes are estimates (no viewer has measured them yet)"
                           if not self.viewers() else "some bboxes are still estimates")
        if warnings:
            out["warnings"] = warnings
        return out

    def _note_activity(self, agent: Optional[str], what: str) -> None:
        self.activity = {"agent": agent, "what": what, "at": time.time()}


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = f"AIWhiteboard/{__version__}"
    protocol_version = "HTTP/1.1"

    @property
    def hub(self) -> Hub:
        return self.server.hub  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet unless --verbose
        if getattr(self.server, "verbose", False):
            sys.stderr.write("[whiteboard] " + (fmt % args) + "\n")

    # ---- helpers ----------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str, extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: Any) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"ok": False, "error": message})

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return True
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        allowed = LOCAL_HOSTS | set(getattr(self.server, "extra_hosts", ()))
        return name in allowed

    def _read_raw(self) -> bytes:
        """Read the whole request body (always, so keep-alive connections stay in sync)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            self.close_connection = True
            raise BoardError("request body too large")
        return self.rfile.read(length) if length > 0 else b""

    def _body(self) -> Any:
        raw = self._raw
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BoardError(f"body is not valid JSON: {exc}") from None

    def _agent(self, body: Any) -> Optional[str]:
        name = (body.get("agent") if isinstance(body, dict) else None) or self.headers.get("X-Agent")
        return str(name)[:40] if name else None

    # ---- verbs ------------------------------------------------------------
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._error(403, "host not allowed")
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        path = url.path
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/favicon.ico":
                return self._static("favicon.svg")
            if path == "/api/health":
                return self._json(200, {"ok": True, "app": "ai-whiteboard", "version": __version__,
                                        "viewers": self.hub.viewers(),
                                        "elements": len(self.hub.state.elements)})
            if path == "/api/board":
                props = qs.get("props", ["0"])[0] in ("1", "true", "yes")
                return self._json(200, {"ok": True, **self.hub.state.describe(include_props=props),
                                        "viewers": self.hub.viewers()})
            if path == "/api/state":
                return self._json(200, self.hub.state.to_dict())
            if path == "/api/guide":
                if qs.get("format", ["text"])[0] == "json":
                    return self._json(200, {"guide": guide()})
                return self._send(200, guide().encode("utf-8"), "text/markdown; charset=utf-8")
            if path == "/api/saves":
                return self._json(200, {"saved": self.hub.saved()})
            if path == "/api/info":
                return self._json(200, self._info())
            if path == "/api/snapshot":
                fmt = qs.get("format", ["png"])[0]
                fmt = "jpeg" if fmt in ("jpg", "jpeg") else "png"
                width = int(qs.get("width", ["1600"])[0])
                data = self.hub.request_snapshot(fmt, max(200, min(width, 4000)),
                                                 float(qs.get("timeout", ["8"])[0]))
                return self._send(200, data, f"image/{fmt}")
            if path == "/api/choice":
                ids = [i for i in ",".join(qs.get("ids", [])).split(",") if i]
                return self._json(200, self.hub.wait_for_choice(
                    ids or None, qs.get("group", [None])[0], qs.get("timeout", ["60"])[0]))
            if path == "/api/events":
                return self._events()
            return self._error(404, f"no route {path}")
        except BoardError as exc:
            return self._error(400 if "viewer" not in str(exc) else 503, str(exc))

    def do_POST(self) -> None:
        try:
            self._raw = self._read_raw()
        except BoardError as exc:
            return self._error(413, str(exc))
        if not self._host_ok():
            return self._error(403, "host not allowed")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._error(415, "POST bodies must be application/json")
        path = urlparse(self.path).path
        try:
            body = self._body()
            hub = self.hub
            agent = self._agent(body)
            if path == "/api/draw":
                els = body.get("elements", body.get("element")) if isinstance(body, dict) else body
                if els is None and isinstance(body, dict) and "type" in body:
                    els = body
                author = "user" if isinstance(body, dict) and body.get("author") == "user" else "ai"
                origin = body.get("origin") if isinstance(body, dict) else None
                return self._json(200, hub.draw(els, author=author, agent=agent, origin=origin,
                                                 wait=author != "user"))
            if path == "/api/update":
                ups = body.get("updates") if isinstance(body, dict) and "updates" in body else body
                if isinstance(ups, dict):
                    ups = [ups]
                return self._json(200, hub.update(ups, agent=agent))
            if path == "/api/erase":
                return self._json(200, hub.erase(body.get("ids"), body.get("region"),
                                                 body.get("author"), agent=agent,
                                                 origin=body.get("origin")))
            if path == "/api/choose":
                return self._json(200, hub.choose(body.get("id"), agent=agent))
            if path == "/api/arrange":
                return self._json(200, hub.arrange(body.get("ids"), body.get("align"),
                                                   body.get("distribute"), body.get("gap"), agent=agent))
            if path == "/api/order":
                return self._json(200, hub.reorder(body.get("ids", body.get("id")),
                                                   body.get("to", "front"), agent=agent))
            if path == "/api/clear":
                bg = body.get("background")
                if bg and bg not in BACKGROUNDS:
                    raise BoardError(f"background must be one of {', '.join(BACKGROUNDS)}")
                return self._json(200, hub.clear(bg, agent=agent))
            if path == "/api/settings":
                return self._json(200, hub.settings(body))
            if path == "/api/undo":
                return self._json(200, hub.undo())
            if path == "/api/say":
                return self._json(200, hub.say(body.get("text", ""), body.get("duration"), agent=agent,
                                                pause=body.get("pause")))
            if path == "/api/pause":
                return self._json(200, hub.pause(body.get("seconds", 1)))
            if path == "/api/point":
                return self._json(200, hub.point(body.get("x"), body.get("y"), body.get("id"),
                                                 body.get("label"), agent=agent))
            if path == "/api/save":
                return self._json(200, hub.save(body.get("name", "")))
            if path == "/api/load":
                return self._json(200, hub.load(body.get("name", "")))
            if path == "/api/layout":
                return self._json(200, {"ok": True, "stored": hub.report_layout(body.get("bboxes") or {})})
            if path == "/api/snapshot":
                # a late answer from a second viewer is normal, not an error
                ok = hub.deliver_snapshot(str(body.get("req", "")), str(body.get("data", "")))
                return self._json(200, {"ok": ok})
            if path == "/api/shutdown":
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                hub.close_all()
                return self._json(200, {"ok": True, "message": "shutting down"})
            return self._error(404, f"no route {path}")
        except BoardError as exc:
            return self._error(400, str(exc))

    # ---- static files -----------------------------------------------------
    def _static(self, rel: str) -> None:
        root = STATIC_DIR.resolve()
        target = (root / rel).resolve()
        if root not in target.parents and target != root or not target.is_file():
            return self._error(404, "not found")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix == ".js":
            ctype = "text/javascript"
        if ctype.startswith("text/") or ctype.endswith("javascript") or ctype.endswith("svg+xml"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    def _info(self) -> Dict[str, Any]:
        pkg = Path(__file__).resolve().parent
        host, port = self.server.server_address[:2]
        return {
            "url": f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}",
            "version": __version__,
            "python": sys.executable,
            "project_dir": str(pkg.parent),
            "mcp_command": [sys.executable, str(pkg / "mcp_server.py")],
            "data_dir": str(self.hub.data_dir),
        }

    # ---- server-sent events -------------------------------------------------
    def _events(self) -> None:
        cid, q = self.hub.connect()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True
            hello = {"type": "hello", "client": cid, "state": self.hub.state.to_dict(),
                     "version": __version__, "build": static_build()}
            self._push(hello)
            while True:
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if ev is None:
                    break
                self._push(ev)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            self.hub.disconnect(cid)

    def _push(self, ev: Dict[str, Any]) -> None:
        data = json.dumps(ev, ensure_ascii=False)
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()


class WhiteboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, addr, hub: Hub, verbose: bool = False, extra_hosts=()):
        super().__init__(addr, Handler)
        self.hub = hub
        self.verbose = verbose
        self.extra_hosts = tuple(h.lower() for h in extra_hosts)


def make_server(host: str = "127.0.0.1", port: int = DEFAULT_PORT, data_dir: Optional[Path] = None,
                persist: bool = True, verbose: bool = False) -> WhiteboardHTTPServer:
    hub = Hub(data_dir, persist=persist)
    extra = (host,) if host not in ("0.0.0.0", "::") else ()
    return WhiteboardHTTPServer((host, port), hub, verbose=verbose, extra_hosts=extra)


def serve(host: str = "127.0.0.1", port: int = DEFAULT_PORT, open_browser: bool = False,
          verbose: bool = False) -> None:
    httpd = make_server(host, port, verbose=verbose)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{httpd.server_address[1]}"
    sys.stderr.write(f"AI Whiteboard {__version__} live at {url}\n")
    if open_browser:
        import webbrowser
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.hub.close_all()
        if httpd.hub.state.save_path:
            try:
                httpd.hub.state.save_now()
            except Exception:
                pass
        httpd.server_close()
