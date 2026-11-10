"""
Command line for the AI whiteboard.

    python -m whiteboard serve [--port 8765] [--open]   run the board server
    python -m whiteboard open                           open the board in a browser
    python -m whiteboard draw '{"type":"title","text":"Hi"}'   (or @file.json, or - for stdin)
    python -m whiteboard run script.json                run a list of ops / elements
    python -m whiteboard say "Hello!"
    python -m whiteboard board                          what is on the board
    python -m whiteboard look out.png                   save a picture of the board
    python -m whiteboard wait [--timeout 60] [ID...]    wait for the user to click a button
    python -m whiteboard arrange --align top --distribute horizontal --gap 40 ID ID [ID...]
    python -m whiteboard order front|back ID [ID...]    change what is drawn on top
    python -m whiteboard erase ID [ID...] | clear | undo
    python -m whiteboard demo [plans|explain|all]       draw a demo board
    python -m whiteboard guide                          element reference for AIs
    python -m whiteboard mcp                            run the MCP server (stdio)
    python -m whiteboard stop                           stop the background server
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from .client import DEFAULT_URL, Whiteboard, WhiteboardError


def _load_json(arg: str) -> Any:
    if arg == "-":
        return json.loads(sys.stdin.read())
    if arg.startswith("@"):
        return json.loads(Path(arg[1:]).read_text(encoding="utf-8"))
    return json.loads(arg)


def _print(obj: Any) -> None:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=1)
    sys.stdout.buffer.write(text.encode("utf-8") + b"\n")
    sys.stdout.flush()


def run_ops(wb: Whiteboard, ops: Any, pause: float = 0.0) -> List[Any]:
    """Run a script: a list of elements, or ops like {"op": "say", "text": "..."}."""
    if isinstance(ops, dict):
        ops = ops.get("ops") or ops.get("elements") or [ops]
    results = []
    batch: List[Dict[str, Any]] = []

    def flush():
        if batch:
            results.append(wb.draw(list(batch)))
            batch.clear()

    for op in ops:
        kind = op.get("op") if isinstance(op, dict) else None
        if kind is None:
            batch.append(op)
            continue
        flush()
        args = {k: v for k, v in op.items() if k != "op"}
        if kind == "draw":
            results.append(wb.draw(args.get("elements") or []))
        elif kind == "say":
            results.append(wb.say(args.get("text", ""), args.get("duration"), args.get("pause")))
        elif kind == "pause":
            results.append(wb.pause(args.get("seconds", 1)))
        elif kind == "point":
            results.append(wb.point(args.get("x"), args.get("y"), args.get("id"), args.get("label")))
        elif kind == "update":
            results.append(wb.update_many(args.get("updates") or [args]))
        elif kind == "erase":
            results.append(wb.erase(args.get("ids"), args.get("region"), args.get("author")))
        elif kind == "arrange":
            results.append(wb.arrange(args.get("ids") or [], args.get("align"), args.get("distribute"),
                                      args.get("gap")))
        elif kind == "order":
            results.append(wb.reorder(args.get("ids") or [args.get("id")], args.get("to", "front")))
        elif kind == "clear":
            results.append(wb.clear(args.get("background")))
        elif kind == "settings":
            results.append(wb.settings(**args))
        elif kind == "wait_for_choice":
            results.append(wb.wait_for_choice(args.get("ids"), args.get("group"), args.get("timeout", 60)))
        elif kind == "wait":
            time.sleep(float(args.get("seconds", 1)))
        else:
            raise WhiteboardError(f"unknown op {kind!r}")
        if pause:
            time.sleep(pause)
    flush()
    return results


def main(argv: List[str] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m whiteboard", description="AI Whiteboard")
    p.add_argument("--url", default=DEFAULT_URL, help=f"board server (default {DEFAULT_URL})")
    p.add_argument("--agent", default="CLI", help="name shown while drawing")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="run the whiteboard server")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open", action="store_true", help="open the board in the browser")
    s.add_argument("--verbose", action="store_true")

    sub.add_parser("open", help="open the board in the browser")
    d = sub.add_parser("draw", help="draw elements (JSON, @file.json or - for stdin)")
    d.add_argument("json")
    r = sub.add_parser("run", help="run a JSON script of elements and ops")
    r.add_argument("file")
    r.add_argument("--pause", type=float, default=0.0)
    u = sub.add_parser("update", help="update an element: update ID '{\"x\": 10}'")
    u.add_argument("id")
    u.add_argument("json")
    e = sub.add_parser("erase", help="erase elements by id")
    e.add_argument("ids", nargs="*")
    e.add_argument("--region", nargs=4, type=float, metavar=("X", "Y", "W", "H"))
    e.add_argument("--user", action="store_true", help="erase everything the user drew")
    wt = sub.add_parser("wait", help="wait for the user to click a button, print which one")
    wt.add_argument("ids", nargs="*", help="only these buttons (default: any)")
    wt.add_argument("--group")
    wt.add_argument("--timeout", type=float, default=60.0)
    ch = sub.add_parser("choose", help="click a button as if the user had (for scripts and tests)")
    ch.add_argument("id")
    ar = sub.add_parser("arrange", help="line elements up and/or space them evenly")
    ar.add_argument("ids", nargs="+")
    ar.add_argument("--align", choices=["left", "center", "right", "top", "middle", "bottom"])
    ar.add_argument("--distribute", choices=["horizontal", "vertical"])
    ar.add_argument("--gap", type=float, help="exact space between elements when distributing")
    od = sub.add_parser("order", help="bring elements to the front or send them to the back")
    od.add_argument("to", choices=["front", "back"])
    od.add_argument("ids", nargs="+")
    c = sub.add_parser("clear", help="wipe the board")
    c.add_argument("--background", choices=["whiteboard", "grid", "dots", "chalkboard", "blueprint"])
    sub.add_parser("undo", help="undo the last change")
    sy = sub.add_parser("say", help="mascot speech bubble")
    sy.add_argument("text")
    pt = sub.add_parser("point", help="point at an element or spot")
    pt.add_argument("--id")
    pt.add_argument("--x", type=float)
    pt.add_argument("--y", type=float)
    pt.add_argument("--label")
    b = sub.add_parser("board", help="list what is on the board")
    b.add_argument("--props", action="store_true")
    lk = sub.add_parser("look", help="save a picture of the board")
    lk.add_argument("out", nargs="?", default="whiteboard.png")
    lk.add_argument("--width", type=int, default=1600)
    sv = sub.add_parser("save", help="save the board")
    sv.add_argument("name")
    ld = sub.add_parser("load", help="load a saved board")
    ld.add_argument("name")
    sub.add_parser("saves", help="list saved boards")
    st = sub.add_parser("settings", help="board settings, e.g. settings '{\"background\":\"grid\"}'")
    st.add_argument("json")
    dm = sub.add_parser("demo", help="draw a demo board")
    dm.add_argument("name", nargs="?", default="plans")
    sub.add_parser("guide", help="print the element reference for AIs")
    sub.add_parser("mcp", help="run the MCP server on stdio")
    sub.add_parser("stop", help="stop the whiteboard server")

    args = p.parse_args(argv)
    if args.cmd in (None, "serve"):
        from .server import serve
        if args.cmd is None:
            return serve(open_browser=True) or 0
        wb = Whiteboard(f"http://127.0.0.1:{args.port}", autostart=False)
        if args.host in ("127.0.0.1", "localhost") and wb.health():
            print(f"A whiteboard is already running at {wb.url}", file=sys.stderr)
            if args.open:
                wb.open()
            return 0
        serve(args.host, args.port, open_browser=args.open, verbose=args.verbose)
        return 0
    if args.cmd == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main()
        return 0
    if args.cmd == "guide":
        from .reference import guide
        _print(guide())
        return 0

    wb = Whiteboard(args.url, agent=args.agent)
    try:
        if args.cmd == "open":
            _print(wb.open())
        elif args.cmd == "draw":
            wb.ensure_server()
            wb.ensure_viewer()
            data = _load_json(args.json)
            _print(wb.draw(data.get("elements", data) if isinstance(data, dict) and "elements" in data else data))
        elif args.cmd == "run":
            wb.ensure_server()
            wb.ensure_viewer()
            _print(run_ops(wb, _load_json("@" + args.file), args.pause))
        elif args.cmd == "update":
            _print(wb.update(args.id, **_load_json(args.json)))
        elif args.cmd == "erase":
            _print(wb.erase(args.ids or None, args.region, "user" if args.user else None))
        elif args.cmd == "wait":
            _print(wb.wait_for_choice(args.ids or None, args.group, args.timeout))
        elif args.cmd == "choose":
            _print(wb.choose(args.id))
        elif args.cmd == "arrange":
            _print(wb.arrange(args.ids, args.align, args.distribute, args.gap))
        elif args.cmd == "order":
            _print(wb.reorder(args.ids, args.to))
        elif args.cmd == "clear":
            _print(wb.clear(args.background))
        elif args.cmd == "undo":
            _print(wb.undo())
        elif args.cmd == "say":
            wb.ensure_server()
            wb.ensure_viewer()
            _print(wb.say(args.text))
        elif args.cmd == "point":
            _print(wb.point(args.x, args.y, args.id, args.label))
        elif args.cmd == "board":
            info = wb.board(args.props)
            occupancy = info.pop("occupancy", "")
            _print(info)
            _print(occupancy)
        elif args.cmd == "look":
            fmt = "jpeg" if args.out.lower().endswith((".jpg", ".jpeg")) else "png"
            Path(args.out).write_bytes(wb.snapshot(fmt, args.width))
            _print(f"saved {args.out}")
        elif args.cmd == "save":
            _print(wb.save(args.name))
        elif args.cmd == "load":
            _print(wb.load(args.name))
        elif args.cmd == "saves":
            _print(wb.saved())
        elif args.cmd == "settings":
            _print(wb.settings(**_load_json(args.json)))
        elif args.cmd == "demo":
            from .demos import DEMOS
            if args.name not in DEMOS:
                raise WhiteboardError(f"unknown demo {args.name!r}; demos: {', '.join(DEMOS)}")
            wb.ensure_server()
            wb.ensure_viewer()
            run_ops(wb, DEMOS[args.name]())
            _print(f"drew the {args.name!r} demo at {wb.url}")
        elif args.cmd == "stop":
            if wb.health():
                _print(wb.shutdown())
            else:
                _print("no whiteboard server running")
    except (WhiteboardError, ConnectionError, json.JSONDecodeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
