"""
Board state for the AI whiteboard: element validation, size estimates,
auto-placement, undo history and persistence.

The browser viewer is the renderer, so the exact size of an element is only
known once a viewer has laid it out and reported its bounding box back. Until
then the server works with estimates (good enough for auto-placement).
"""

from __future__ import annotations

import copy
import json
import re
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

BOARD_WIDTH = 1600
BOARD_HEIGHT = 1000

ELEMENT_TYPES = (
    "text", "title", "rect", "ellipse", "circle", "diamond", "triangle", "hexagon",
    "star", "cloud", "cylinder", "parallelogram", "line", "arrow", "path", "highlight",
    "emoji", "icon", "image", "sticky", "panel", "bubble", "banner", "table", "chart",
    "code", "frame", "timeline", "mindmap", "flow", "button",
)

SHAPE_TYPES = (
    "rect", "ellipse", "circle", "diamond", "triangle", "hexagon", "star", "cloud",
    "cylinder", "parallelogram",
)

# alias -> (type, default props)
ALIASES: Dict[str, Tuple[str, Dict[str, Any]]] = {
    "note": ("sticky", {}),
    "sticky_note": ("sticky", {}),
    "postit": ("sticky", {}),
    "card": ("panel", {"style": "card"}),
    "box": ("rect", {}),
    "rectangle": ("rect", {}),
    "square": ("rect", {}),
    "oval": ("ellipse", {}),
    "connector": ("arrow", {}),
    "heading": ("title", {}),
    "header": ("title", {}),
    "label": ("text", {}),
    "callout": ("bubble", {}),
    "speech": ("bubble", {}),
    "speech_bubble": ("bubble", {}),
    "thought": ("bubble", {"kind": "thought"}),
    "ribbon": ("banner", {}),
    "freehand": ("line", {"smooth": True}),
    "polyline": ("line", {}),
    "pen": ("line", {"smooth": True}),
    "svg": ("path", {}),
    "graph": ("chart", {}),
    "grid": ("table", {}),
    "list": ("panel", {"style": "plain"}),
    "checklist": ("panel", {"bullets": "box"}),
    "section": ("frame", {}),
    "group": ("frame", {}),
    "flowchart": ("flow", {}),
    "steps": ("flow", {}),
    "mind_map": ("mindmap", {}),
    "picture": ("image", {}),
    "photo": ("image", {}),
    "choice": ("button", {}),
    "option": ("button", {}),
    "btn": ("button", {}),
    "marker": ("highlight", {}),
    "highlighter": ("highlight", {}),
}

BACKGROUNDS = ("whiteboard", "grid", "dots", "chalkboard", "blueprint")

# Element types that never take part in placement / overlap checks
NON_BLOCKING = {"highlight", "line", "arrow", "frame"}

MAX_ELEMENTS = 3000
MAX_ELEMENT_BYTES = 4_000_000
MAX_POINTS = 20_000
MAX_HISTORY = 100
ID_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,64}$")

# Average glyph width as a fraction of the font size (Kalam, the default hand font)
GLYPH_W = 0.47
MASCOT_ZONE = (1400, 770, 200, 230)

ALIGNMENTS = ("left", "center", "right", "top", "middle", "bottom")
DISTRIBUTIONS = ("horizontal", "vertical")

NUMERIC_FIELDS = (
    "x", "y", "w", "h", "cx", "cy", "r", "size", "width", "radius", "curve",
    "rotate", "opacity", "rough", "scale", "gap", "title_size", "label_size",
    "line_height", "thickness", "duration",
)


class BoardError(ValueError):
    """A request the board cannot carry out (reported back to the AI as-is)."""


# --------------------------------------------------------------------------
# Small geometry helpers
# --------------------------------------------------------------------------

def _num(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise BoardError(f"'{field}' must be a number, got {value!r}")
    if isinstance(value, (int, float)):
        out = float(value)
    else:
        try:
            out = float(str(value).strip().rstrip("px"))
        except ValueError:
            raise BoardError(f"'{field}' must be a number, got {value!r}") from None
    if out != out or abs(out) > 1e6:
        raise BoardError(f"'{field}' is out of range: {value!r}")
    return int(out) if out.is_integer() else round(out, 2)


def _point(value: Any, field: str) -> List[float]:
    if isinstance(value, dict) and "x" in value and "y" in value:
        return [_num(value["x"], field), _num(value["y"], field)]
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return [_num(value[0], field), _num(value[1], field)]
    raise BoardError(f"'{field}' must be a point [x, y]")


def _points(value: Any, field: str = "points") -> List[List[float]]:
    if not isinstance(value, (list, tuple)) or not value:
        raise BoardError(f"'{field}' must be a list of [x, y] points")
    if all(isinstance(v, (int, float)) for v in value):  # flat [x1, y1, x2, y2, ...]
        if len(value) % 2:
            raise BoardError(f"'{field}' flat list needs an even number of values")
        value = [value[i:i + 2] for i in range(0, len(value), 2)]
    if len(value) > MAX_POINTS:
        raise BoardError(f"'{field}' has too many points (max {MAX_POINTS})")
    return [_point(p, field) for p in value]


def rects_intersect(a, b, margin: float = 0.0) -> bool:
    return (a[0] < b[0] + b[2] + margin and b[0] < a[0] + a[2] + margin and
            a[1] < b[1] + b[3] + margin and b[1] < a[1] + a[3] + margin)


def rect_contains(outer, inner, tol: float = 4.0) -> bool:
    return (inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol and
            inner[0] + inner[2] <= outer[0] + outer[2] + tol and
            inner[1] + inner[3] <= outer[1] + outer[3] + tol)


def _overlap_area(a, b) -> float:
    w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return max(0.0, w) * max(0.0, h)


def _union(boxes: Iterable[List[float]]) -> Optional[List[float]]:
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return [x0, y0, x1 - x0, y1 - y0]


# --------------------------------------------------------------------------
# Text size estimates (the viewer reports exact sizes later)
# --------------------------------------------------------------------------

def plain_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "".join(plain_text(v.get("text", "") if isinstance(v, dict) else v) for v in value)
    return str(value).replace("**", "")


def text_size(text: Any, size: float, max_w: float = 0, line_h: float = 1.25,
              glyph: float = GLYPH_W) -> Tuple[float, float]:
    cw = size * glyph
    lines: List[int] = []
    for raw in plain_text(text).split("\n"):
        n = len(raw)
        if max_w and n * cw > max_w:
            per_line = max(1, int(max_w / cw))
            # word wrapping wastes a little of each line
            per_line = max(1, int(per_line * 0.9))
            while n > per_line:
                lines.append(per_line)
                n -= per_line
        lines.append(n)
    width = max(lines) * cw if lines else 0
    if max_w:
        width = min(width, max_w)
    return width, max(1, len(lines)) * size * line_h


def _item_list(items: Any) -> List[Any]:
    return items if isinstance(items, list) else []


def _items_height(items: List[Any], size: float, width: float, depth: int = 0) -> float:
    h = 0.0
    indent = size * 1.1 * (depth + 1)
    for it in items:
        txt = it.get("text", "") if isinstance(it, dict) else it
        h += text_size(txt, size, max(40, width - indent))[1] + size * 0.18
        if isinstance(it, dict):
            h += _items_height(_item_list(it.get("items")), size, width, depth + 1)
    return h


def estimate_size(el: Dict[str, Any], board: Dict[str, Any]) -> Tuple[float, float]:
    """Estimated (w, h) of an element before any viewer has measured it."""
    t = el["type"]
    size = el.get("size")
    if t == "text":
        size = size or 28
        return text_size(el.get("text"), size, el.get("width") or el.get("w") or 0,
                         el.get("line_height") or 1.25)
    if t == "title":
        size = size or 64
        w, h = text_size(el.get("text"), size, el.get("width") or 0, glyph=GLYPH_W * 1.05)
        return w, h + size * 0.25
    if t in SHAPE_TYPES:
        if t == "circle" and el.get("r") is not None:
            return el["r"] * 2, el["r"] * 2
        return el.get("w") or (140 if t == "circle" else 200), el.get("h") or (140 if t == "circle" else 120)
    if t in ("highlight", "frame"):
        return el.get("w") or 300, el.get("h") or 60
    if t == "emoji":
        s = size or 64
        return s * 1.15, s * 1.15
    if t == "icon":
        s = size or 80
        return s, s
    if t == "image":
        return el.get("w") or 320, el.get("h") or 220
    if t in ("sticky", "panel"):
        size = size or 26
        w = el.get("w") or (240 if t == "sticky" else 360)
        pad = size * 0.75
        inner = w - 2 * pad
        h = pad * 1.8
        if el.get("title"):
            h += text_size(el["title"], size * 1.2, inner)[1] + size * 0.45
        if el.get("text"):
            h += text_size(el["text"], size, inner)[1] + size * 0.3
        h += _items_height(_item_list(el.get("items")), size, inner)
        if t == "sticky":
            h = max(h, w * 0.72)
        return w, max(h, el.get("h") or 0)
    if t == "bubble":
        size = size or 28
        tw, th = text_size(el.get("text"), size, min(420, (el.get("w") or 600) * 0.72))
        return el.get("w") or tw / 0.72 + size, el.get("h") or th / 0.68 + size * 0.8
    if t == "banner":
        size = size or 40
        tw, _ = text_size(el.get("text"), size, glyph=GLYPH_W * 1.05)
        bw = el.get("w") or tw + size * 2.2
        return bw + size * 1.4, size * 1.6 * 1.35
    if t == "table":
        size = size or 22
        rows = el.get("rows") or []
        if not rows:
            return 200, 60
        cols = max(len(r) if isinstance(r, list) else 1 for r in rows)
        widths = [0.0] * cols
        for r in rows:
            cells = r if isinstance(r, list) else [r]
            for i, c in enumerate(cells):
                widths[i] = max(widths[i], min(360, text_size(c, size)[0]))
        return sum(widths) + cols * size * 1.2, len(rows) * size * 1.25 + len(rows) * size * 0.8
    if t == "chart":
        return el.get("w") or 480, el.get("h") or 320
    if t == "code":
        size = size or 18
        lines = plain_text(el.get("text")).split("\n")
        w = el.get("w") or max(len(line) for line in lines) * size * 0.6 + size * 2
        h = len(lines) * size * 1.4 + size * 1.6 + (size * 1.8 if el.get("title") else 0)
        return w, h
    if t == "button":
        size = size or 28
        tw, th = text_size(el.get("text"), size, glyph=GLYPH_W * 1.05)
        return el.get("w") or tw + size * 2.2, el.get("h") or th + size * 0.9
    if t == "timeline":
        size = size or 22
        return el.get("w") or 1000, size * 8
    if t == "mindmap":
        radius = el.get("radius") or 260
        return radius * 3.4, radius * 2.6
    if t == "flow":
        size = size or 24
        steps = _item_list(el.get("steps"))
        n = max(1, len(steps))
        longest = max([len(plain_text(s.get("text") if isinstance(s, dict) else s)) for s in steps] or [6])
        bw = el.get("w") or min(240, longest * size * GLYPH_W) + size * 1.6
        bh = el.get("h") or size * 2.8
        gap = el.get("gap") or 60
        if el.get("direction") == "down":
            return bw, n * bh + (n - 1) * gap
        return n * bw + (n - 1) * gap, bh
    return el.get("w") or 100, el.get("h") or 100


# --------------------------------------------------------------------------
# Board state
# --------------------------------------------------------------------------

class BoardState:
    """Thread-safe whiteboard model shared by the HTTP server and its viewers."""

    def __init__(self, save_path: Optional[Path] = None):
        self.lock = threading.RLock()
        self.save_path = Path(save_path) if save_path else None
        self.settings: Dict[str, Any] = {
            "width": BOARD_WIDTH, "height": BOARD_HEIGHT,
            "background": "whiteboard", "mascot": True,
        }
        self.elements: Dict[str, Dict[str, Any]] = {}
        self.bboxes: Dict[str, List[float]] = {}  # measured by a viewer
        self.history: List[Dict[str, Any]] = []
        self.version = 0
        self._pending_titles: set = set()
        self._save_timer: Optional[threading.Timer] = None
        if self.save_path and self.save_path.exists():
            try:
                self.load_dict(json.loads(self.save_path.read_text(encoding="utf-8")))
            except Exception:  # a corrupt save must not stop the board from starting
                pass

    # ---- serialisation ---------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "settings": dict(self.settings),
                "elements": [copy.deepcopy(e) for e in self.elements.values()],
                "version": self.version,
            }

    def load_dict(self, data: Dict[str, Any]) -> None:
        with self.lock:
            settings = data.get("settings") or {}
            self.settings.update({k: v for k, v in settings.items() if k in self.settings})
            self.elements = {}
            for e in data.get("elements") or []:
                if isinstance(e, dict) and e.get("id") and e.get("type") in ELEMENT_TYPES:
                    self.elements[e["id"]] = e
            self.bboxes = {}
            self.version += 1

    def schedule_save(self) -> None:
        if not self.save_path:
            return
        with self.lock:
            if self._save_timer:
                self._save_timer.cancel()
            self._save_timer = threading.Timer(0.4, self.save_now)
            self._save_timer.daemon = True
            self._save_timer.start()

    def save_now(self, path: Optional[Path] = None) -> Path:
        target = Path(path) if path else self.save_path
        if target is None:
            raise BoardError("no save path configured")
        data = json.dumps(self.to_dict(), ensure_ascii=False, indent=1)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(data, encoding="utf-8")
        tmp.replace(target)
        return target

    # ---- ids & validation ----------------------------------------------
    def _new_id(self, kind: str, taken: set) -> str:
        n = 1
        while f"{kind}{n}" in self.elements or f"{kind}{n}" in taken:
            n += 1
        return f"{kind}{n}"

    def normalize(self, raw: Any, taken: set) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            raise BoardError(f"each element must be a JSON object, got {type(raw).__name__}")
        el = dict(raw)
        kind = str(el.get("type", "")).strip().lower().replace("-", "_").replace(" ", "_")
        if kind in ALIASES:
            kind, defaults = ALIASES[kind]
            for k, v in defaults.items():
                el.setdefault(k, v)
        if kind not in ELEMENT_TYPES:
            raise BoardError(
                f"unknown element type {el.get('type')!r}. Known types: {', '.join(ELEMENT_TYPES)}")
        el["type"] = kind

        for field in NUMERIC_FIELDS:
            if field in el and el[field] is not None:
                if field == "width" and kind in ("text", "title") and el[field] == "auto":
                    del el[field]
                    continue
                el[field] = _num(el[field], field)
            elif field in el:
                del el[field]
        for field in ("w", "h", "size", "r"):
            if field in el and el[field] <= 0:
                raise BoardError(f"'{field}' must be positive")
        if "size" in el:
            el["size"] = min(el["size"], 400)
        for field in ("w", "h", "r"):
            if field in el:
                el[field] = min(el[field], 20000)

        if kind == "line":
            el["points"] = _points(el.get("points"))
        if kind == "arrow":
            for end in ("from", "to"):
                if end not in el:
                    raise BoardError(f"arrow needs '{end}' (an element id or [x, y])")
                v = el[end]
                if isinstance(v, str):
                    continue
                if isinstance(v, dict) and "id" in v:
                    el[end] = {"id": str(v["id"]), **{k: v[k] for k in ("side",) if k in v}}
                else:
                    el[end] = _point(v, end)
        if kind == "bubble" and "tail" in el and not isinstance(el["tail"], str):
            el["tail"] = _point(el["tail"], "tail")
        if kind == "path" and not isinstance(el.get("d"), str):
            raise BoardError("path needs 'd' (SVG path data)")
        if kind == "icon" and not el.get("name"):
            raise BoardError("icon needs 'name'")
        if kind == "image" and not isinstance(el.get("src"), str):
            raise BoardError("image needs 'src' (an http(s) URL or data: URI)")
        if kind == "button":
            if not plain_text(el.get("text")).strip():
                raise BoardError("button needs 'text'")
            if el.get("chosen"):
                el["chosen"] = True
            else:
                el.pop("chosen", None)
            if el.get("group") is not None:
                el["group"] = str(el["group"])[:64]
        if kind == "emoji" and not (el.get("text") or el.get("emoji") or el.get("char")):
            raise BoardError("emoji needs 'text'")

        eid = el.get("id")
        if eid is None or eid == "":
            eid = self._new_id(kind, taken)
        eid = str(eid)
        if not ID_RE.match(eid):
            raise BoardError(f"invalid id {eid!r} (letters, digits, _ - : . up to 64 chars)")
        if eid in taken:
            raise BoardError(f"duplicate id {eid!r} in the same request")
        el["id"] = eid

        if len(json.dumps(el, ensure_ascii=False)) > MAX_ELEMENT_BYTES:
            raise BoardError(f"element {eid!r} is too large")
        return el

    # ---- geometry -------------------------------------------------------
    def bbox_of(self, eid: str, pending: Optional[Dict[str, List[float]]] = None) -> Optional[List[float]]:
        if pending and eid in pending:
            return pending[eid]
        if eid in self.bboxes:
            return self.bboxes[eid]
        el = self.elements.get(eid)
        return self.estimate_bbox(el, pending) if el else None

    def _end_point(self, v: Any, pending) -> Optional[List[float]]:
        if isinstance(v, list):
            return v
        eid = v["id"] if isinstance(v, dict) else v
        b = self.bbox_of(str(eid), pending)
        if not b:
            return None
        return [b[0] + b[2] / 2, b[1] + b[3] / 2]

    def estimate_bbox(self, el: Dict[str, Any], pending=None) -> List[float]:
        t = el["type"]
        board = self.settings
        if t == "line":
            pts = el["points"]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            pad = (el.get("width") or 4) / 2
            return [min(xs) - pad, min(ys) - pad, max(xs) - min(xs) + 2 * pad, max(ys) - min(ys) + 2 * pad]
        if t == "arrow":
            a = self._end_point(el["from"], pending) or [0, 0]
            b = self._end_point(el["to"], pending) or a
            return [min(a[0], b[0]), min(a[1], b[1]), abs(b[0] - a[0]) or 1, abs(b[1] - a[1]) or 1]
        if t == "path":
            nums = [float(n) for n in re.findall(r"-?\d*\.?\d+(?:[eE][-+]?\d+)?", el["d"])]
            xs, ys = nums[0::2], nums[1::2]
            scale = el.get("scale") or 1
            if not xs or not ys:
                return [el.get("x", 0), el.get("y", 0), 10, 10]
            return [el.get("x", 0) + min(xs) * scale, el.get("y", 0) + min(ys) * scale,
                    (max(xs) - min(xs)) * scale or 1, (max(ys) - min(ys)) * scale or 1]

        w, h = estimate_size(el, board)
        if t == "title":
            align = el.get("align", "center")
            x = el.get("x", board["width"] / 2)
            left = x - w / 2 if align == "center" else x - w if align == "right" else x
            return [left, el.get("y", 24), w, h]
        if t == "text":
            align = el.get("align", "left")
            x = el.get("x", 0)
            left = x - w / 2 if align == "center" else x - w if align == "right" else x
            if "cx" in el:
                left = el["cx"] - w / 2
            top = el["cy"] - h / 2 if "cy" in el else el.get("y", 0)
            return [left, top, w, h]
        if t == "circle" and el.get("r") is not None:
            cx = el.get("cx", el.get("x", 0))
            cy = el.get("cy", el.get("y", 0))
            return [cx - w / 2, cy - h / 2, w, h]
        if t == "mindmap":
            cx = el.get("cx", el.get("x", board["width"] / 2))
            cy = el.get("cy", el.get("y", board["height"] / 2))
            return [cx - w / 2, cy - h / 2, w, h]
        left = el["cx"] - w / 2 if "cx" in el else el.get("x", 0)
        top = el["cy"] - h / 2 if "cy" in el else el.get("y", 0)
        return [left, top, w, h]

    def _set_top_left(self, el: Dict[str, Any], left: float, top: float, w: float, h: float) -> None:
        t = el["type"]
        el.pop("cx", None)
        el.pop("cy", None)
        if t == "circle" and el.get("r") is not None:
            el["x"], el["y"] = round(left + w / 2), round(top + h / 2)
        elif t == "mindmap":
            el["cx"], el["cy"] = round(left + w / 2), round(top + h / 2)
            el.pop("x", None)
            el.pop("y", None)
        elif t in ("text", "title") and el.get("align", "left" if t == "text" else "center") != "left":
            align = el.get("align", "center")
            el["x"] = round(left + w / 2) if align == "center" else round(left + w)
            el["y"] = round(top)
        else:
            el["x"], el["y"] = round(left), round(top)

    def _blocking_boxes(self, pending: Dict[str, List[float]], skip: str) -> List[List[float]]:
        boxes = []
        for eid, el in self.elements.items():
            if eid == skip or el["type"] in NON_BLOCKING:
                continue
            b = self.bbox_of(eid, pending)
            if b:
                boxes.append(b)
        for eid, b in pending.items():
            if eid != skip and eid not in self.elements:
                boxes.append(b)
        if self.settings.get("mascot"):
            boxes.append(list(MASCOT_ZONE))
        return boxes

    def find_free_spot(self, w: float, h: float, boxes: List[List[float]], margin: float = 24,
                       top: float = 30) -> Tuple[float, float]:
        bw, bh = self.settings["width"], self.settings["height"]
        step = 20
        y = top
        while y + h <= bh - 20:
            x = 30
            while x + w <= bw - 30:
                cand = [x, y, w, h]
                hit = next((b for b in boxes if rects_intersect(cand, b, margin)), None)
                if hit is None:
                    return x, y
                # jump past the obstacle
                x = max(x + step, hit[0] + hit[2] + margin)
            y += step
        # no room: stack below everything
        bottom = max([b[1] + b[3] for b in boxes] or [0])
        return 30, bottom + margin

    def _place(self, el: Dict[str, Any], pending: Dict[str, List[float]]) -> None:
        """Resolve relative placement (below/right_of/...) and auto-placement."""
        t = el["type"]
        rel = next((k for k in ("below", "above", "right_of", "left_of") if el.get(k)), None)
        has_pos = any(k in el for k in ("x", "y", "cx", "cy"))
        if t in ("line", "arrow", "path", "title"):
            for k in ("below", "above", "right_of", "left_of"):
                el.pop(k, None)
            return
        if not rel and has_pos:
            return
        w, h = estimate_size(el, self.settings)
        if t == "circle" and el.get("r") is not None:
            w = h = el["r"] * 2
        if rel:
            target_id = str(el.pop(rel))
            gap = el.pop("gap", 30)
            align = el.pop("align_to", el.get("align_with", "start"))
            el.pop("align_with", None)
            tb = self.bbox_of(target_id, pending)
            if not tb:
                raise BoardError(f"'{rel}' refers to unknown element {target_id!r}")
            if rel in ("below", "above"):
                top = tb[1] + tb[3] + gap if rel == "below" else tb[1] - gap - h
                left = tb[0] if align == "start" else tb[0] + (tb[2] - w) / 2 if align == "center" else tb[0] + tb[2] - w
            else:
                left = tb[0] + tb[2] + gap if rel == "right_of" else tb[0] - gap - w
                top = tb[1] if align == "start" else tb[1] + (tb[3] - h) / 2 if align == "center" else tb[1] + tb[3] - h
            self._set_top_left(el, left, top, w, h)
            return
        # content goes under the heading: start below any title on the board
        title_boxes = [self.bbox_of(i, pending) for i, e in self.elements.items() if e["type"] == "title"]
        title_boxes += [b for i, b in pending.items() if i not in self.elements and i in self._pending_titles]
        top = max([30.0] + [b[1] + b[3] + 16 for b in title_boxes if b])
        if top + h > self.settings["height"] - 20:
            top = 30.0  # a tall element still fits beside the title
        left, top = self.find_free_spot(w, h, self._blocking_boxes(pending, el["id"]), top=top)
        self._set_top_left(el, left, top, w, h)

    # ---- mutations ------------------------------------------------------
    def _record(self, before: Dict[str, Optional[Dict[str, Any]]], settings_before=None) -> None:
        self.history.append({
            "before": before,
            "order": list(self.elements.keys()),
            "settings": settings_before,
        })
        del self.history[:-MAX_HISTORY]

    def add(self, raws: List[Any], author: str = "ai", agent: Optional[str] = None
            ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Validate, place and add elements. Returns (elements, replaced_ids)."""
        if not isinstance(raws, list) or not raws:
            raise BoardError("nothing to draw: pass one element or a list of elements")
        with self.lock:
            taken: set = set()
            out: List[Dict[str, Any]] = []
            pending: Dict[str, List[float]] = {}
            self._pending_titles = set()
            for raw in raws:
                el = self.normalize(raw, taken)
                taken.add(el["id"])
                self._place(el, pending)
                for k in ("below", "above", "right_of", "left_of"):
                    el.pop(k, None)
                el["author"] = author
                if agent:
                    el["agent"] = agent
                pending[el["id"]] = self.estimate_bbox(el, pending)
                if el["type"] == "title":
                    self._pending_titles.add(el["id"])
                out.append(el)
            if len(self.elements) + len(out) > MAX_ELEMENTS:
                raise BoardError(f"the board is full ({MAX_ELEMENTS} elements); erase or clear first")
            before = {}
            replaced = []
            for el in out:
                before[el["id"]] = copy.deepcopy(self.elements.get(el["id"]))
                if el["id"] in self.elements:
                    replaced.append(el["id"])
                    del self.elements[el["id"]]  # re-adding moves it to the top
            self._record(before)
            for el in out:
                self.elements[el["id"]] = el
                self.bboxes.pop(el["id"], None)
            self.version += 1
            self.schedule_save()
            return copy.deepcopy(out), replaced

    def update(self, eid: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            if eid not in self.elements:
                raise BoardError(f"no element with id {eid!r}")
            if not isinstance(changes, dict) or not changes:
                raise BoardError("update needs some fields to change")
            old = self.elements[eid]
            merged = {**old, **{k: v for k, v in changes.items() if k not in ("id", "author")}}
            for k, v in changes.items():
                if v is None:
                    merged.pop(k, None)
            # moving an element by x/y drops any centre anchor and vice versa
            if ("x" in changes or "y" in changes) and not ("cx" in changes or "cy" in changes):
                if old["type"] != "mindmap":
                    merged.pop("cx", None)
                    merged.pop("cy", None)
            if "dx" in changes or "dy" in changes:
                self._shift(merged, _num(changes.get("dx", 0), "dx"), _num(changes.get("dy", 0), "dy"))
                merged.pop("dx", None)
                merged.pop("dy", None)
            merged["type"] = changes.get("type", old["type"])
            el = self.normalize(merged, set())
            el["author"] = old.get("author", "ai")
            self._record({eid: copy.deepcopy(old)})
            self.elements[eid] = el
            self.bboxes.pop(eid, None)
            self.version += 1
            self.schedule_save()
            return copy.deepcopy(el)

    def _shift(self, el: Dict[str, Any], dx: float, dy: float) -> None:
        t = el["type"]
        if t == "line":
            el["points"] = [[p[0] + dx, p[1] + dy] for p in el["points"]]
            return
        if t == "arrow":
            for end in ("from", "to"):
                if isinstance(el[end], list):
                    el[end] = [el[end][0] + dx, el[end][1] + dy]
            return
        if "cx" in el or "cy" in el:
            el["cx"] = el.get("cx", 0) + dx
            el["cy"] = el.get("cy", 0) + dy
        else:
            b = self.bbox_of(el["id"]) if el["id"] in self.elements else None
            if t == "title" and "x" not in el and b:  # a title's x is its anchor, centred by default
                align = el.get("align", "center")
                el["x"] = b[0] + b[2] / 2 if align == "center" else b[0] + b[2] if align == "right" else b[0]
            el["x"] = el.get("x", b[0] if b and t not in ("title",) else 0) + dx
            el["y"] = el.get("y", b[1] if b else 0) + dy

    def remove(self, ids: List[str]) -> List[str]:
        with self.lock:
            gone = [i for i in ids if i in self.elements]
            if not gone:
                return []
            self._record({i: copy.deepcopy(self.elements[i]) for i in gone})
            for i in gone:
                del self.elements[i]
                self.bboxes.pop(i, None)
            self.version += 1
            self.schedule_save()
            return gone

    def choose(self, eid: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Mark a button as the user's pick and unmark the rest of its group.

        Returns (the button, the elements that changed). A click is an answer, not an
        edit, so it stays out of the undo history.
        """
        with self.lock:
            el = self.elements.get(eid)
            if not el or el["type"] != "button":
                raise BoardError(f"{eid!r} is not a button on the board")
            group = el.get("group")
            before: Dict[str, Optional[Dict[str, Any]]] = {}
            for oid, other in self.elements.items():
                same = oid == eid
                if other["type"] != "button" or bool(other.get("chosen")) == same:
                    continue
                if not same and (group is None or other.get("group") != group):
                    continue
                before[oid] = other
            changed = []
            for oid, old in before.items():
                new = copy.deepcopy(old)
                if oid == eid:
                    new["chosen"] = True
                else:
                    new.pop("chosen", None)
                changed.append(new)
            if changed:
                for new in changed:
                    self.elements[new["id"]] = new  # same size, so measured bboxes stay valid
                self.version += 1
                self.schedule_save()
            return copy.deepcopy(self.elements[eid]), copy.deepcopy(changed)

    def arrange(self, ids: List[str], align: Optional[str] = None, distribute: Optional[str] = None,
                gap: Any = None) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Line elements up and/or space them evenly, as one undo step.

        align: left | center | right (x axis) or top | middle | bottom (y axis), against the
        box around all the listed elements. distribute: horizontal | vertical; without a gap
        the outermost edges stay put and the spaces in between are made equal, with a gap
        the elements are packed from the first one with exactly that space between them.
        Returns (changed elements, skipped ids). Arrows tied to elements are skipped: they
        follow whatever they connect.
        """
        if align is not None and align not in ALIGNMENTS:
            raise BoardError(f"align must be one of {', '.join(ALIGNMENTS)}")
        if distribute is not None and distribute not in DISTRIBUTIONS:
            raise BoardError(f"distribute must be one of {', '.join(DISTRIBUTIONS)}")
        if not align and not distribute:
            raise BoardError("arrange needs 'align' and/or 'distribute'")
        if gap is not None:
            gap = _num(gap, "gap")
            if not distribute:
                raise BoardError("'gap' only applies with 'distribute'")
        if isinstance(ids, str):
            ids = [ids]
        with self.lock:
            ids = list(dict.fromkeys(str(i) for i in ids or []))
            unknown = [i for i in ids if i not in self.elements]
            if unknown:
                raise BoardError(f"no element with id {', '.join(repr(i) for i in unknown)}")
            skipped = [i for i in ids if self.elements[i]["type"] == "arrow" and not (
                isinstance(self.elements[i]["from"], list) and isinstance(self.elements[i]["to"], list))]
            movable = [i for i in ids if i not in skipped]
            if len(movable) < 2:
                raise BoardError("arrange needs at least two elements to line up")
            old = {i: list(self.bbox_of(i)) for i in movable}
            new = {i: list(b) for i, b in old.items()}
            if align:
                u = _union(new.values())
                axis = 0 if align in ("left", "center", "right") else 1
                frac = {"left": 0, "top": 0, "center": 0.5, "middle": 0.5, "right": 1, "bottom": 1}[align]
                for b in new.values():
                    b[axis] = u[axis] + (u[axis + 2] - b[axis + 2]) * frac
            if distribute:
                axis = 0 if distribute == "horizontal" else 1
                order = sorted(movable, key=lambda i: new[i][axis])
                start = new[order[0]][axis]
                space = gap
                if space is None:
                    if len(order) < 3:
                        raise BoardError("distribute needs three or more elements, or a 'gap'")
                    end = max(new[i][axis] + new[i][axis + 2] for i in order)
                    space = (end - start - sum(new[i][axis + 2] for i in order)) / (len(order) - 1)
                for i in order:
                    new[i][axis] = start
                    start += new[i][axis + 2] + space
            before: Dict[str, Optional[Dict[str, Any]]] = {}
            changed: List[Dict[str, Any]] = []
            for i in movable:
                dx, dy = round(new[i][0] - old[i][0], 1), round(new[i][1] - old[i][1], 1)
                if not dx and not dy:
                    continue
                prev = self.elements[i]
                el = copy.deepcopy(prev)
                self._shift(el, dx, dy)
                el = self.normalize(el, set())
                before[i] = prev
                changed.append(el)
            if changed:
                self._record(before)
                for el in changed:
                    i = el["id"]
                    self.elements[i] = el
                    if i in self.bboxes:  # keep the measured size, at the new position
                        b = self.bboxes[i]
                        self.bboxes[i] = [round(b[0] + new[i][0] - old[i][0], 1),
                                          round(b[1] + new[i][1] - old[i][1], 1), b[2], b[3]]
                self.version += 1
                self.schedule_save()
            return copy.deepcopy(changed), skipped

    def reorder(self, ids: List[str], to: str = "front") -> List[str]:
        """Move elements to the front (drawn last) or the back. Returns the new stacking order."""
        if to not in ("front", "back"):
            raise BoardError("'to' must be \"front\" or \"back\"")
        if isinstance(ids, str):
            ids = [ids]
        with self.lock:
            picked = {str(i) for i in ids or []}
            if not picked:
                raise BoardError("reorder needs at least one element id")
            unknown = sorted(i for i in picked if i not in self.elements)
            if unknown:
                raise BoardError(f"no element with id {', '.join(repr(i) for i in unknown)}")
            moved = [i for i in self.elements if i in picked]  # keep their relative stacking
            rest = [i for i in self.elements if i not in picked]
            order = rest + moved if to == "front" else moved + rest
            if order != list(self.elements):
                self._record({})
                self.elements = {i: self.elements[i] for i in order}
                self.version += 1
                self.schedule_save()
            return order

    def ids_in_region(self, region: List[float], author: Optional[str] = None) -> List[str]:
        with self.lock:
            out = []
            for eid, el in self.elements.items():
                if author and el.get("author") != author:
                    continue
                b = self.bbox_of(eid)
                if b and rect_contains(region, b, tol=2) or (b and _overlap_area(region, b) > 0.5 * b[2] * b[3]):
                    out.append(eid)
            return out

    def clear(self) -> int:
        with self.lock:
            n = len(self.elements)
            if n:
                self._record({i: copy.deepcopy(e) for i, e in self.elements.items()})
            self.elements = {}
            self.bboxes = {}
            self.version += 1
            self.schedule_save()
            return n

    def set_settings(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            new = dict(self.settings)
            for k, v in changes.items():
                if k == "background":
                    if v not in BACKGROUNDS:
                        raise BoardError(f"background must be one of {', '.join(BACKGROUNDS)}")
                    new[k] = v
                elif k in ("width", "height"):
                    v = int(_num(v, k))
                    if not 400 <= v <= 6000:
                        raise BoardError(f"{k} must be between 400 and 6000")
                    new[k] = v
                elif k == "mascot":
                    new[k] = bool(v)
            if new != self.settings:
                self._record({}, settings_before=dict(self.settings))
                self.settings = new
                self.version += 1
                self.schedule_save()
            return dict(self.settings)

    def undo(self) -> bool:
        with self.lock:
            if not self.history:
                return False
            rec = self.history.pop()
            for eid, el in rec["before"].items():
                if el is None:
                    self.elements.pop(eid, None)
                else:
                    self.elements[eid] = el
                self.bboxes.pop(eid, None)
            order = [i for i in rec["order"] if i in self.elements]
            rest = [i for i in self.elements if i not in order]
            self.elements = {i: self.elements[i] for i in order + rest}
            if rec.get("settings"):
                self.settings = rec["settings"]
            self.version += 1
            self.schedule_save()
            return True

    def set_bboxes(self, boxes: Dict[str, Any]) -> int:
        n = 0
        with self.lock:
            for eid, b in (boxes or {}).items():
                if eid in self.elements and isinstance(b, list) and len(b) == 4:
                    try:
                        self.bboxes[eid] = [round(float(v), 1) for v in b]
                        n += 1
                    except (TypeError, ValueError):
                        continue
        return n

    # ---- reporting ------------------------------------------------------
    def summary(self, eid: str) -> Dict[str, Any]:
        el = self.elements[eid]
        out: Dict[str, Any] = {"id": eid, "type": el["type"]}
        measured = eid in self.bboxes
        b = self.bbox_of(eid)
        if b:
            out["bbox"] = [round(v) for v in b]
        if not measured:
            out["bbox_estimated"] = True
        label = el.get("title") or el.get("text") or el.get("label") or el.get("name") or el.get("center")
        if label:
            s = plain_text(label).replace("\n", " ")
            out["label"] = s if len(s) <= 60 else s[:57] + "..."
        if el.get("author") == "user":
            out["author"] = "user"
        return out

    def warnings_for(self, ids: List[str]) -> List[str]:
        warns: List[str] = []
        with self.lock:
            bw, bh = self.settings["width"], self.settings["height"]
            for eid in ids:
                if eid not in self.elements:
                    continue
                b = self.bbox_of(eid)
                if not b:
                    continue
                if b[0] < -2 or b[1] < -2 or b[0] + b[2] > bw + 2 or b[1] + b[3] > bh + 2:
                    warns.append(f"{eid} extends past the board edge (board is {bw}x{bh}, bbox {[round(v) for v in b]})")
                if self.elements[eid]["type"] in NON_BLOCKING:
                    continue
                for oid, other in self.elements.items():
                    if oid == eid or other["type"] in NON_BLOCKING:
                        continue
                    ob = self.bbox_of(oid)
                    if not ob or not rects_intersect(b, ob):
                        continue
                    if rect_contains(b, ob) or rect_contains(ob, b):
                        continue  # nesting (an icon inside a panel) is usually intended
                    area = _overlap_area(b, ob)
                    if area > 0.08 * min(b[2] * b[3], ob[2] * ob[3]):
                        warns.append(f"{eid} overlaps {oid}")
                if self.settings.get("mascot") and _overlap_area(b, MASCOT_ZONE) > 0.1 * b[2] * b[3]:
                    warns.append(f"{eid} is partly behind the mascot (bottom-right corner)")
        return warns[:20]

    def occupancy(self, cell: int = 100) -> str:
        with self.lock:
            bw, bh = self.settings["width"], self.settings["height"]
            cols, rows = int(bw // cell), int(bh // cell)
            grid = [["." for _ in range(cols)] for _ in range(rows)]
            for eid, el in self.elements.items():
                if el["type"] in ("highlight",):
                    continue
                b = self.bbox_of(eid)
                if not b:
                    continue
                for r in range(rows):
                    for c in range(cols):
                        cellr = [c * cell, r * cell, cell, cell]
                        if _overlap_area(cellr, b) > 0.15 * cell * cell:
                            grid[r][c] = "#"
            if self.settings.get("mascot"):
                m = MASCOT_ZONE
                for r in range(rows):
                    for c in range(cols):
                        if grid[r][c] == "." and _overlap_area([c * cell, r * cell, cell, cell], m) > 0.3 * cell * cell:
                            grid[r][c] = "M"
            header = "      " + "".join(f"{(c * cell) // 100 % 10}" for c in range(cols))
            lines = [f"x/100 {header[6:]}"]
            for r in range(rows):
                lines.append(f"y{r * cell:>4} " + "".join(grid[r]))
            return "\n".join(lines)

    def describe(self, include_props: bool = False) -> Dict[str, Any]:
        with self.lock:
            items = []
            for eid, el in self.elements.items():
                s = self.summary(eid)
                if include_props:
                    s["props"] = {k: v for k, v in el.items() if k not in ("id", "type") and
                                  not (k == "src" and isinstance(v, str) and len(v) > 200)}
                items.append(s)
            return {
                "board": dict(self.settings),
                "count": len(items),
                "elements": items,
                "occupancy": self.occupancy(),
                "occupancy_legend": "Each character is a 100x100 cell (column = x/100, row = y/100): '.' free, '#' used, 'M' mascot.",
            }
