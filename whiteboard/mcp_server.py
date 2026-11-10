"""
MCP (Model Context Protocol) server for the AI whiteboard, over stdio.

Any MCP client (Claude Code, Claude Desktop, other agents) can launch it:

    python -m whiteboard mcp
    python C:/path/to/whiteboard/mcp_server.py

It talks to the whiteboard HTTP server, starting one in the background (and
opening the board in the browser) the first time it is needed.
Standard library only: JSON-RPC 2.0, one message per line.
"""

from __future__ import annotations

import base64
import json
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

if __package__ in (None, ""):  # run as a script: make the package importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "whiteboard"

from whiteboard import __version__  # noqa: E402
from whiteboard.client import Whiteboard, WhiteboardError  # noqa: E402
from whiteboard.reference import DRAW_DESCRIPTION, guide  # noqa: E402

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "This server controls a live whiteboard the user watches in their browser. Use it to show "
    "things visually: plans, diagrams, explanations, sketches, charts. Draw with `draw` (call "
    "`guide` once for the full element and icon reference), check the result with `look`, fix it "
    "with `update`/`erase`, and talk to the user through the mascot with `say`. To ask the user "
    "something, draw `button` elements and call `wait_for_choice` to get the one they click. "
    "The board is 1600x1000, origin top-left."
)

ELEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "description": "Element type, e.g. title, text, panel, sticky, rect, "
                 "arrow, icon, chart, table, mindmap, flow, bubble, banner, line, path"},
        "id": {"type": "string"},
    },
    "required": ["type"],
    "additionalProperties": True,
}


def _text(obj: Any) -> Dict[str, Any]:
    return {"type": "text", "text": obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=1)}


class WhiteboardMCP:
    def __init__(self, board: Optional[Whiteboard] = None):
        self.board = board or Whiteboard(agent="AI")
        self.tools: Dict[str, Dict[str, Any]] = {}
        self.handlers: Dict[str, Callable[[Dict[str, Any]], List[Dict[str, Any]]]] = {}
        self._register_tools()

    # ---- tool table -------------------------------------------------------
    def tool(self, name: str, description: str, properties: Dict[str, Any],
             required: Optional[List[str]] = None, read_only: bool = False):
        def wrap(fn):
            schema: Dict[str, Any] = {"type": "object", "properties": properties}
            if required:
                schema["required"] = required
            self.tools[name] = {
                "name": name, "description": description, "inputSchema": schema,
                "annotations": {"readOnlyHint": read_only, "openWorldHint": False},
            }
            self.handlers[name] = fn
            return fn
        return wrap

    def _register_tools(self) -> None:
        b = self.board

        @self.tool("draw", DRAW_DESCRIPTION, {
            "elements": {"type": "array", "items": ELEMENT_SCHEMA,
                         "description": "Elements to draw, in drawing order."},
        }, ["elements"])
        def draw(args):
            els = args.get("elements")
            if isinstance(els, str):  # some clients send the array as a JSON string
                els = json.loads(els)
            if isinstance(els, dict):
                els = [els]
            b.ensure_server()
            opened = b.ensure_viewer()
            res = b.draw(els)
            if opened:
                res["note_browser"] = "Opened the whiteboard in the user's browser."
            return [_text(res)]

        @self.tool("update", "Change existing elements: move them (x/y, or dx/dy to shift), "
                   "restyle them or change their text. Pass only the fields to change; null removes a field.", {
            "updates": {"type": "array", "items": {
                "type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"],
                "additionalProperties": True}, "description": 'e.g. [{"id": "panel1", "dx": 40, "color": "red"}]'},
        }, ["updates"])
        def update(args):
            ups = args.get("updates")
            if isinstance(ups, dict):
                ups = [ups]
            return [_text(b.update_many(ups))]

        @self.tool("erase", "Erase elements by id, everything inside a region [x, y, w, h], "
                   "or everything drawn by the user (author: \"user\").", {
            "ids": {"type": "array", "items": {"type": "string"}},
            "region": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
            "author": {"type": "string", "enum": ["user", "ai"]},
        })
        def erase(args):
            return [_text(b.erase(args.get("ids"), args.get("region"), args.get("author")))]

        @self.tool("arrange", "Tidy up in one call: line elements up (align) and/or space them evenly "
                   "(distribute). Fixes overlap warnings: distribute with a gap puts exactly that much space "
                   "between the elements. Arrows follow the elements they connect.", {
            "ids": {"type": "array", "items": {"type": "string"}, "description": "Two or more element ids."},
            "align": {"type": "string", "enum": ["left", "center", "right", "top", "middle", "bottom"],
                      "description": "Edge or centre line to share, taken from the box around all the ids."},
            "distribute": {"type": "string", "enum": ["horizontal", "vertical"],
                           "description": "Make the spaces between the elements equal along this axis."},
            "gap": {"type": "number", "description": "With distribute: exact space between neighbours, "
                    "packed from the first element. Without it the outer edges stay where they are."},
        }, ["ids"])
        def arrange(args):
            return [_text(b.arrange(args.get("ids") or [], args.get("align"), args.get("distribute"),
                                    args.get("gap")))]

        @self.tool("reorder", "Change stacking: bring elements to the front (on top of everything) or "
                   "send them to the back (behind everything). Their position does not change.", {
            "ids": {"type": "array", "items": {"type": "string"}},
            "to": {"type": "string", "enum": ["front", "back"], "description": "Default front."},
        }, ["ids"])
        def reorder(args):
            return [_text(b.reorder(args.get("ids") or [], args.get("to") or "front"))]

        @self.tool("clear_board", "Wipe the whole board (an eraser sweeps across it). Optionally switch "
                   "the background.", {
            "background": {"type": "string", "enum": ["whiteboard", "grid", "dots", "chalkboard", "blueprint"]},
        })
        def clear_board(args):
            return [_text(b.clear(args.get("background")))]

        @self.tool("get_board", "List what is on the board: ids, types, labels and bboxes, plus an "
                   "occupancy map of free space. Includes anything the user drew by hand (author: user).", {
            "props": {"type": "boolean", "description": "Include every element's full properties."},
        }, read_only=True)
        def get_board(args):
            return [_text(b.board(bool(args.get("props"))))]

        @self.tool("look", "See the board: returns an image of it as the user sees it. Use it after "
                   "drawing to check layout and legibility. Needs the board open in a browser.", {
            "width": {"type": "integer", "description": "Image width in pixels (default 1280)."},
        }, read_only=True)
        def look(args):
            b.ensure_server()
            b.ensure_viewer()
            width = int(args.get("width") or 1280)
            data = b.snapshot("jpeg", max(320, min(width, 2400)))
            return [{"type": "image", "data": base64.b64encode(data).decode("ascii"),
                     "mimeType": "image/jpeg"}]

        @self.tool("say", "Make the robot mascot say something to the user in a speech bubble "
                   "(short, one or two sentences).", {
            "text": {"type": "string"},
            "duration": {"type": "number", "description": "Seconds to show it (default: based on length)."},
            "pause": {"type": "number", "description": "Seconds to hold further drawing so the user can read it."},
        }, ["text"])
        def say(args):
            b.ensure_server()
            b.ensure_viewer()
            return [_text(b.say(args["text"], args.get("duration"), args.get("pause")))]

        @self.tool("pause", "Hold the drawing for a few seconds before the next elements appear, to pace "
                   "an explanation or presentation. Drawing calls still return immediately.", {
            "seconds": {"type": "number", "description": "0 to 30 seconds."},
        }, ["seconds"])
        def pause(args):
            return [_text(b.pause(args["seconds"]))]

        @self.tool("point_at", "Point at an element or spot with an animated pointer, to direct the "
                   "user's attention while explaining.", {
            "id": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"},
            "label": {"type": "string", "description": "Optional short caption next to the pointer."},
        })
        def point_at(args):
            return [_text(b.point(args.get("x"), args.get("y"), args.get("id"), args.get("label")))]

        @self.tool("wait_for_choice", "Ask the user: first draw a question and a few `button` elements "
                   "(give them ids and a shared \"group\"), then call this. It waits until the user clicks one "
                   "and returns its id, text and value. The clicked button is shown as chosen on the board. "
                   "If nobody clicks in time, chosen is null: call again to keep waiting.", {
            "ids": {"type": "array", "items": {"type": "string"},
                    "description": "Only accept these buttons (default: any button)."},
            "group": {"type": "string", "description": "Only accept buttons of this group."},
            "timeout": {"type": "number", "description": "Seconds to wait (default 60, max 600)."},
        })
        def wait_for_choice(args):
            b.ensure_server()
            b.ensure_viewer()
            timeout = args.get("timeout")
            return [_text(b.wait_for_choice(args.get("ids"), args.get("group"),
                                            60 if timeout is None else timeout))]

        @self.tool("undo", "Reverse the last change to the board, including a clear or load. Call again "
                   "to go further back. It also undoes the user's changes, so prefer erase or update for "
                   "your own elements.", {})
        def undo(args):
            return [_text(b.undo())]

        @self.tool("board_settings", "Change the board: background (whiteboard, grid, dots, chalkboard, "
                   "blueprint), size (width/height) or hide the mascot.", {
            "background": {"type": "string", "enum": ["whiteboard", "grid", "dots", "chalkboard", "blueprint"]},
            "width": {"type": "integer"}, "height": {"type": "integer"}, "mascot": {"type": "boolean"},
        })
        def board_settings(args):
            return [_text(b.settings(**{k: v for k, v in args.items() if v is not None}))]

        @self.tool("save_board", "Save the current board under a name.", {"name": {"type": "string"}}, ["name"])
        def save_board(args):
            return [_text(b.save(args["name"]))]

        @self.tool("load_board", "Load a saved board (replaces the current one; undo restores it). "
                   "Call with no name to list saved boards.", {"name": {"type": "string"}})
        def load_board(args):
            if not args.get("name"):
                return [_text({"saved": b.saved()})]
            return [_text(b.load(args["name"]))]

        @self.tool("open_whiteboard", "Open the whiteboard in the user's web browser and return its URL.", {})
        def open_whiteboard(args):
            return [_text({"url": b.open()})]

        @self.tool("guide", "Full reference: every element type with its fields, all icon names, "
                   "layout tips.", {}, read_only=True)
        def guide_tool(args):
            return [_text(guide())]

    # ---- JSON-RPC -----------------------------------------------------------
    def handle(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = msg.get("method")
        mid = msg.get("id")
        if method is None:  # a response to something we never send
            return None
        if mid is None:  # notification (initialized, cancelled, ...)
            return None
        params = msg.get("params") or {}
        try:
            if method == "initialize":
                requested = params.get("protocolVersion")
                version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                client = (params.get("clientInfo") or {}).get("name")
                if client:
                    self.board.agent = _agent_name(client)
                result = {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "ai-whiteboard", "title": "AI Whiteboard", "version": __version__},
                    "instructions": INSTRUCTIONS,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": list(self.tools.values())}
            elif method == "tools/call":
                result = self.call_tool(params.get("name"), params.get("arguments") or {})
            elif method in ("resources/list", "prompts/list"):
                result = {method.split("/")[0]: []}
            else:
                return {"jsonrpc": "2.0", "id": mid,
                        "error": {"code": -32601, "message": f"method not found: {method}"}}
        except Exception as exc:  # protocol-level failure
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(exc)}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def call_tool(self, name: Any, args: Dict[str, Any]) -> Dict[str, Any]:
        fn = self.handlers.get(str(name))
        if fn is None:
            return {"content": [_text(f"unknown tool {name!r}")], "isError": True}
        try:
            return {"content": fn(args), "isError": False}
        except (WhiteboardError, ConnectionError, ValueError, KeyError, TypeError) as exc:
            return {"content": [_text(f"Error: {exc}")], "isError": True}
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            return {"content": [_text(f"Error: {type(exc).__name__}: {exc}")], "isError": True}


def _agent_name(client: str) -> str:
    low = client.lower()
    if "claude" in low:
        return "Claude"
    if "cursor" in low:
        return "Cursor"
    if "codex" in low or "openai" in low:
        return "Codex"
    if "gemini" in low:
        return "Gemini"
    return client[:24]


def main() -> None:
    server = WhiteboardMCP()
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        line = stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            if isinstance(msg, list):  # JSON-RPC batch (older protocol versions)
                replies = [r for r in (server.handle(m) for m in msg if isinstance(m, dict)) if r]
                reply = replies or None
            else:
                reply = server.handle(msg) if isinstance(msg, dict) else None
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
            stdout.flush()


if __name__ == "__main__":
    main()
