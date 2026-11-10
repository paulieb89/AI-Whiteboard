"""
The AI-facing reference for the whiteboard: used for the MCP tool
descriptions, the /api/guide endpoint and `python -m whiteboard guide`.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

STATIC_DIR = Path(__file__).parent / "static"


@lru_cache(maxsize=1)
def icon_names() -> list:
    """Icon names, read from the viewer's icon library so the docs never drift."""
    try:
        src = (STATIC_DIR / "icons.js").read_text(encoding="utf-8")
    except OSError:
        return []
    body = src.split("const ICONS = {", 1)[-1].split("\n  };", 1)[0]
    return re.findall(r"^  ([a-z][a-z0-9_]*):", body, re.M)


@lru_cache(maxsize=1)
def icon_aliases() -> list:
    try:
        src = (STATIC_DIR / "icons.js").read_text(encoding="utf-8")
    except OSError:
        return []
    body = src.split("const ICON_ALIASES = {", 1)[-1].split("};", 1)[0]
    return re.findall(r"([a-z][a-z0-9_]*):", body)


OVERVIEW = """\
A live whiteboard the user is watching in their browser. Everything you draw
appears with a marker-drawing animation, in the order you send it, so draw the
way a person would explain at a whiteboard: title first, then sections, then
details and arrows.

Coordinates: the board is 1600 x 1000 (x to the right, y down, origin top-left).
Most elements use x,y = top-left corner and w,h = size; give cx,cy instead to
centre an element on a point. Exceptions: circle with r (x,y = centre),
text/title with align "center" (x = centre line), mindmap (cx,cy = centre).
Leave out x/y and the board auto-places the element in free space, or place it
relative to another one with "below": "<id>" (also "above", "right_of",
"left_of", plus optional "gap" and "align_to": "start" | "center" | "end").
Every reply lists each element's id and bbox [x, y, w, h]: reuse ids for arrows,
updates and erasing. The robot mascot sits in the bottom-right corner
(x > 1400, y > 770) while it is shown."""

ELEMENTS = """\
Common fields: id, color, size (font size), font ("hand" default, "marker",
"neat", "script", "mono"), rotate (degrees), opacity (0-1), rough (0 neat,
1 default, 2 sketchy). Colours: black, blue, red, green, orange, purple, brown,
pink, teal, gray, yellow, navy, white, or any CSS colour. Text supports **bold**
and can be a list of segments: ["Our Plans ", {"text": "& Goals!", "color": "navy"}].

title      {"type":"title","text":"Our Plans & Goals!","size":64,"color":"blue","underline":"green"}  (centred at the top unless x/y given)
text       {"type":"text","x":100,"y":200,"text":"Hello **world**","size":28,"width":400,"align":"left","underline":true,"highlight":"yellow"}
shapes     rect, ellipse, circle, diamond, triangle, hexagon, star, cloud, cylinder, parallelogram:
           {"type":"rect","id":"a","x":100,"y":100,"w":220,"h":120,"color":"blue","fill":"yellow","label":"Step 1","radius":14,"dash":false,"width":3}
           fill is soft by default; "fill_style":"solid" or "hachure". circle also takes r with x,y as the centre.
line       {"type":"line","points":[[100,100],[300,160],[420,120]],"color":"red","width":4,"smooth":true,"arrow":"end","dash":false}
arrow      {"type":"arrow","from":"a","to":"b","label":"yes","curve":0.2,"route":"straight|curve|elbow","head":"end|both|none","color":"black"}
           from/to: an element id, [x,y] or {"id":"a","side":"right"}. Arrows follow their elements when they move.
sticky     {"type":"sticky","x":80,"y":200,"w":260,"title":"Today's Tasks","items":["Research","Draft"],"bullets":"number","color":"yellow"}  (color = paper: yellow, pink, blue, green, orange, purple)
panel      {"type":"panel","x":900,"y":220,"w":420,"title":"Long-Term Goals","items":["Learn new skills",{"text":"Start a podcast","done":true}],"color":"purple","style":"box"}
           style: box | card | cloud | sticky | plain;  bullets: dot | number | check | box | arrow | star | dash | none.
           Optional "text" paragraph. Items nest: {"text":"Parent","items":["child"]}. Height grows to fit.
list       like panel with style "plain" (no frame).
bubble     {"type":"bubble","x":1100,"y":480,"text":"Let's do this!","tail":[1250,640],"kind":"speech|thought|shout"}  (tail may be an element id)
banner     {"type":"banner","x":520,"y":900,"text":"Teamwork! ❤","color":"blue"}  (ribbon)
icon       {"type":"icon","name":"rocket","x":1300,"y":250,"size":120,"color":"red"}  (hand-drawn, coloured; names below)
emoji      {"type":"emoji","text":"🚀","x":100,"y":100,"size":72}
table      {"type":"table","x":100,"y":300,"rows":[["Name","Score"],["Ana","9"]],"header":true,"header_color":"blue","size":22}
chart      {"type":"chart","kind":"bar|line|pie|donut","x":800,"y":300,"w":480,"h":300,"title":"Sales","data":[{"label":"Q1","value":12},{"label":"Q2","value":19}]}
           or "labels":["Q1","Q2"],"series":[{"name":"2025","values":[3,5],"color":"blue"}]. Optional "unit", "max".
code       {"type":"code","x":100,"y":100,"text":"def hi():\\n    print('hi')","title":"hello.py","size":18}
frame      {"type":"frame","x":60,"y":160,"w":700,"h":500,"title":"Section","color":"gray"}  (dashed section outline)
timeline   {"type":"timeline","x":100,"y":700,"w":1200,"items":[{"label":"Jan","text":"Kickoff"},{"label":"Mar","text":"Launch"}],"color":"blue"}
mindmap    {"type":"mindmap","cx":800,"cy":520,"center":"Ideas","branches":[{"text":"Podcast","children":["Topics","Guests"]},{"text":"Website"}],"radius":260}
flow       {"type":"flow","x":100,"y":400,"steps":["Idea","Draft","Review","Ship"],"direction":"right|down","shape":"rect|ellipse|diamond","color":"blue"}
button     {"type":"button","id":"yes","x":300,"y":600,"text":"Yes, ship it","group":"q1","value":"ship","color":"green"}
           (the user can click it; wait_for_choice returns the click. "chosen": true draws it as picked)
highlight  {"type":"highlight","x":95,"y":195,"w":300,"h":40,"color":"yellow"}  (translucent highlighter stroke over anything)
path       {"type":"path","d":"M0 0 C 40 -30, 80 30, 120 0","x":400,"y":300,"scale":2,"color":"black","fill":"orange","width":3}  (any SVG path data)
image      {"type":"image","src":"https://... or data:image/png;base64,...","x":100,"y":100,"w":300,"frame":"tape|polaroid|none"}"""

TIPS = """\
Tips
- Send related elements in one draw call; they animate one after another.
- Use auto-placement or below/right_of when unsure of free space; the board
  avoids overlaps. get_board shows an occupancy map of free cells.
- Call look after a drawing to see it the way the user does, then fix overlaps
  with update (move with x/y or dx/dy) or erase.
- arrange tidies a set of ids in one call: "align" (left, center, right, top,
  middle, bottom) lines them up, "distribute" (horizontal, vertical) evens the
  spacing, and a "gap" sets the exact space between them. Use it when a reply
  warns about overlaps.
- Elements stack in drawing order (get_board lists them back to front). Use
  reorder to bring ids to the front or send them to the back without moving them.
- To ask the user something, draw the question and two to five button
  elements sharing a "group", then call wait_for_choice: it returns the id, text
  and value of the button they click, and the board shows it as chosen.
- Keep words short: whiteboards are for headlines, not paragraphs.
- Use say for one-line commentary from the mascot and point_at to direct the
  user's attention while you explain. To pace a presentation, give say a
  "pause" (seconds) or call pause: the board waits before drawing what comes next."""


def guide() -> str:
    names = icon_names()
    aliases = icon_aliases()
    icon_part = "Icons: " + ", ".join(names) if names else "Icons: (icon library not found)"
    if aliases:
        icon_part += "\nIcon aliases: " + ", ".join(aliases)
    return "\n\n".join([
        "# AI Whiteboard guide", OVERVIEW, "Elements\n" + ELEMENTS, icon_part, TIPS,
    ])


DRAW_DESCRIPTION = (
    "Draw one or more elements on the shared whiteboard the user is watching live.\n\n"
    + OVERVIEW + "\n\n" + ELEMENTS + "\n\nCall the guide tool for the icon names and more tips."
)
