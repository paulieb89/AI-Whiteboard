"""
Python client for the whiteboard server. Starts a server in the background if
none is running, so `draw()` just works from scripts, the CLI and the MCP server.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlparse

DEFAULT_URL = os.environ.get("WHITEBOARD_URL", "http://127.0.0.1:8765").rstrip("/")
PROJECT_DIR = Path(__file__).resolve().parent.parent


class WhiteboardError(RuntimeError):
    pass


class Whiteboard:
    def __init__(self, url: str = DEFAULT_URL, agent: Optional[str] = None, autostart: bool = True,
                 open_browser: Optional[bool] = None):
        self.url = url.rstrip("/")
        self.agent = agent
        self.autostart = autostart
        if open_browser is None:
            open_browser = os.environ.get("WHITEBOARD_NO_BROWSER", "") in ("", "0", "false")
        self.open_browser = open_browser
        self._checked = False
        self._opened = False

    # ---- transport --------------------------------------------------------
    def _request(self, method: str, path: str, body: Any = None, timeout: float = 15.0,
                 raw: bool = False) -> Any:
        data = None
        headers = {"Accept": "application/json"}
        if self.agent:
            headers["X-Agent"] = self.agent
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
                return payload if raw else json.loads(payload.decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("error", detail)
            except (json.JSONDecodeError, AttributeError):
                pass
            raise WhiteboardError(detail) from None
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            raise ConnectionError(str(exc)) from None

    def _call(self, method: str, path: str, body: Any = None, **kw) -> Any:
        self.ensure_server()
        try:
            return self._request(method, path, body, **kw)
        except ConnectionError:
            # the server went away (e.g. its host session ended); start a new one once
            self._checked = False
            self.ensure_server()
            return self._request(method, path, body, **kw)

    # ---- server lifecycle ---------------------------------------------------
    def health(self) -> Optional[Dict[str, Any]]:
        try:
            info = self._request("GET", "/api/health", timeout=2.0)
            return info if info.get("app") == "ai-whiteboard" else None
        except (ConnectionError, WhiteboardError, json.JSONDecodeError):
            return None

    def is_local(self) -> bool:
        return (urlparse(self.url).hostname or "") in ("127.0.0.1", "localhost", "::1")

    def ensure_server(self) -> None:
        if self._checked:
            return
        if self.health():
            self._checked = True
            return
        if not self.autostart or not self.is_local():
            raise WhiteboardError(f"no whiteboard server at {self.url}. Start one with: python -m whiteboard serve")
        start_background_server(urlparse(self.url).port or 8765)
        deadline = time.time() + 12
        while time.time() < deadline:
            if self.health():
                self._checked = True
                return
            time.sleep(0.2)
        raise WhiteboardError(f"started a whiteboard server but it did not answer at {self.url}")

    def ensure_viewer(self) -> bool:
        """Open the board in the default browser when nobody is watching yet."""
        info = self.health() or {}
        if info.get("viewers"):
            return False
        if self.open_browser and not self._opened:
            self._opened = True
            webbrowser.open(self.url)
            for _ in range(25):  # give the page a moment to connect
                time.sleep(0.2)
                if (self.health() or {}).get("viewers"):
                    break
            return True
        return False

    def open(self) -> str:
        self.ensure_server()
        webbrowser.open(self.url)
        self._opened = True
        return self.url

    # ---- drawing API --------------------------------------------------------
    def draw(self, *elements: Any) -> Dict[str, Any]:
        items: List[Any] = []
        for e in elements:
            items.extend(e if isinstance(e, list) else [e])
        return self._call("POST", "/api/draw", {"elements": items, "agent": self.agent})

    def update(self, element_id: str, **changes: Any) -> Dict[str, Any]:
        return self._call("POST", "/api/update", {"id": element_id, "changes": changes})

    def update_many(self, updates: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self._call("POST", "/api/update", {"updates": updates})

    def erase(self, ids: Optional[List[str]] = None, region: Optional[List[float]] = None,
              author: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if ids:
            body["ids"] = ids
        if region:
            body["region"] = region
        if author:
            body["author"] = author
        return self._call("POST", "/api/erase", body)

    def arrange(self, ids: List[str], align: Optional[str] = None, distribute: Optional[str] = None,
                gap: Optional[float] = None) -> Dict[str, Any]:
        """Line elements up (align) and/or space them evenly (distribute, optional fixed gap)."""
        body: Dict[str, Any] = {"ids": list(ids)}
        if align:
            body["align"] = align
        if distribute:
            body["distribute"] = distribute
        if gap is not None:
            body["gap"] = gap
        return self._call("POST", "/api/arrange", body)

    def reorder(self, ids: Any, to: str = "front") -> Dict[str, Any]:
        """Change stacking: move elements to the "front" (on top) or the "back"."""
        return self._call("POST", "/api/order", {"ids": [ids] if isinstance(ids, str) else list(ids), "to": to})

    def clear(self, background: Optional[str] = None) -> Dict[str, Any]:
        return self._call("POST", "/api/clear", {"background": background} if background else {})

    def settings(self, **changes: Any) -> Dict[str, Any]:
        return self._call("POST", "/api/settings", changes)

    def undo(self) -> Dict[str, Any]:
        return self._call("POST", "/api/undo", {})

    def say(self, text: str, duration: Optional[float] = None, pause: Optional[float] = None) -> Dict[str, Any]:
        return self._call("POST", "/api/say", {"text": text, "duration": duration, "pause": pause})

    def pause(self, seconds: float = 1.0) -> Dict[str, Any]:
        """Make the board wait before drawing what comes next."""
        return self._call("POST", "/api/pause", {"seconds": seconds})

    def point(self, x: Optional[float] = None, y: Optional[float] = None, id: Optional[str] = None,
              label: Optional[str] = None) -> Dict[str, Any]:
        return self._call("POST", "/api/point", {"x": x, "y": y, "id": id, "label": label})

    def choose(self, id: str) -> Dict[str, Any]:
        """Click a button on the user's behalf (what the viewer sends on a real click)."""
        return self._call("POST", "/api/choose", {"id": id})

    def wait_for_choice(self, ids: Optional[List[str]] = None, group: Optional[str] = None,
                        timeout: float = 60.0) -> Dict[str, Any]:
        """Wait until the user clicks a button; "chosen" is None if nobody did in time."""
        q: Dict[str, Any] = {"timeout": timeout}
        if ids:
            q["ids"] = ",".join(ids)
        if group:
            q["group"] = group
        return self._call("GET", f"/api/choice?{urlencode(q)}", timeout=float(timeout) + 10)

    def board(self, props: bool = False) -> Dict[str, Any]:
        return self._call("GET", "/api/board" + ("?props=1" if props else ""))

    def snapshot(self, fmt: str = "png", width: int = 1600, timeout: float = 8.0) -> bytes:
        q = urlencode({"format": fmt, "width": width, "timeout": timeout})
        return self._call("GET", f"/api/snapshot?{q}", raw=True, timeout=timeout + 5)

    def save(self, name: str) -> Dict[str, Any]:
        return self._call("POST", "/api/save", {"name": name})

    def load(self, name: str) -> Dict[str, Any]:
        return self._call("POST", "/api/load", {"name": name})

    def saved(self) -> List[str]:
        return self._call("GET", "/api/saves").get("saved", [])

    def guide(self) -> str:
        return self._call("GET", "/api/guide?format=json")["guide"]

    def shutdown(self) -> Dict[str, Any]:
        return self._request("POST", "/api/shutdown", {})


def start_background_server(port: int) -> None:
    """Launch `python -m whiteboard serve` detached from this process."""
    from .server import default_data_dir

    log_dir = default_data_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / "server.log", "ab")
    cmd = [sys.executable, "-m", "whiteboard", "serve", "--port", str(port)]
    kwargs: Dict[str, Any] = {
        "cwd": str(PROJECT_DIR), "stdin": subprocess.DEVNULL, "stdout": log, "stderr": log,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                   | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)
    log.close()
