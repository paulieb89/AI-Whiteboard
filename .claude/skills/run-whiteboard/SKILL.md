---
name: run-whiteboard
description: Launch an isolated copy of the whiteboard (own port, own data dir, headless Chrome viewer), draw on it and read back a snapshot. Use to run, verify or screenshot the app, or to confirm that a change to the Python server or the browser viewer works in the real app rather than only in tests.
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/board.sh *)
---

Run the app with `${CLAUDE_SKILL_DIR}/board.sh`. It serves on a free port with a throwaway data dir, so it never touches the live board on 8765 or `./boards`.

```bash
${CLAUDE_SKILL_DIR}/board.sh start                  # fresh server + headless viewer, prints the URL
${CLAUDE_SKILL_DIR}/board.sh wb draw '[{"type":"title","text":"Hi"}]'
${CLAUDE_SKILL_DIR}/board.sh wb look <scratch>/board.png
${CLAUDE_SKILL_DIR}/board.sh stop
```

`wb` passes its arguments to `python -m whiteboard` pointed at the dev board, so every CLI command works: `draw`, `run script.json`, `update`, `erase`, `board`, `demo all`, `settings`.

## Checking a change

1. `start` again after every edit, Python or `whiteboard/static/`. It restarts the server and the viewer, so both run the code on disk. The port changes on each start; `wb` follows it.
2. Draw elements that exercise the change. Read the JSON reply: `warnings` lists overlaps and off-board elements, and `bbox_estimated` means the viewer has not measured that element.
3. Save a snapshot with `wb look` and read the image. The reply JSON says what the server believes, the image says what the user sees. Check both.
4. `stop` when finished.

## What this does not cover

- The drawing animation and timing. A snapshot shows the finished board only, so say so rather than claiming the animation works.
- Keyboard, mouse and toolbar behaviour. Open the printed URL with a browser automation tool (Playwright or Chrome DevTools MCP) as a second viewer and drive it there.
- The `mcp__whiteboard__*` tools. They talk to the live board through a long-running process that still has the old code, so do not use them to verify a change.

If `start` fails, it prints the server or Chrome log. Set `CHROME=/path/to/chrome` if no browser is found. `status` prints the current URL.
