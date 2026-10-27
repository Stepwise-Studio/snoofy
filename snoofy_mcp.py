#!/usr/bin/env python3
"""Snoofy as an MCP server over standard input/output, for Claude Code, Claude Desktop, Codex, Cursor or
any MCP client: read and use this computer's apps and arrange its windows. Logs go to standard error.

    claude mcp add snoofy -- python3 /path/to/snoofy_mcp.py
"""
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import snoofy as D  # noqa: E402

VERSION = "0.1.0"
PROTOCOL = "2025-06-18"


def _status() -> str:
    from gi.repository import Gio
    a11y = Gio.Settings.new("org.gnome.desktop.interface").get_boolean("toolkit-accessibility")
    return json.dumps({"version": VERSION, "apps": D.available(), "windows": D.windows_state(),
                       "browsers_and_electron_readable": a11y}, indent=1)


def _setup() -> str:
    from gi.repository import Gio
    Gio.Settings.new("org.gnome.desktop.interface").set_boolean("toolkit-accessibility", True)
    return json.dumps({"windows": D.install_windows(), "browsers_and_electron_readable": True}, indent=1)


def _with_text(r: dict) -> str:
    head = " ".join(str(r[k]) for k in ("status", "did", "why") if r.get(k))
    if r.get("reacted") is False:
        head += " (the app showed no change)"
    return (head + "\n" if head else "") + (D.text(r) if "elements" in r else "")


def _windows() -> str:
    got = D.windows()
    mons = {m["index"]: m["name"] for m in got["monitors"]}
    return "\n".join(
        [f"monitor {m['index'] + 1}: {m['name']}{' (laptop screen)' if m['builtin'] else ''} {m['w']}x{m['h']}"
         for m in got["monitors"]]
        + [f"[{w['id']}] {w['app']} “{w['title'][:70]}” on {mons.get(w['monitor'], '?')} {w['w']}x{w['h']} at {w['x']},{w['y']}"
           + "".join(f" {k}" for k in ("maximized", "minimized", "focused") if w[k]) for w in reversed(got["windows"])])


S, B, ARR = {"type": "string"}, {"type": "boolean"}, {"type": "array", "items": {"type": "string"}}
TOOLS = {  # name: (description, properties, required, run)
    "status": ("What works on this computer: reading apps, window control, browsers/Electron readable.", {}, [],
               lambda a: _status()),
    "setup": ("Turn on what the driver needs: the window-control shell extension (works from the next log-in) and "
              "accessibility for browsers and Electron apps.", {}, [], lambda a: _setup()),
    "apps": ("Open apps and their windows (▶ = in front).", {}, [],
             lambda a: "\n".join(f"{x['app']}: " + ", ".join(("▶ " if w["active"] else "") + w["title"] for w in x["windows"])
                                 for x in D.apps())),
    "launch": ("Open an app by name (Calculator, Files, Firefox...) and read its window.", {"app": S}, ["app"],
               lambda a: _with_text(D.launch(a["app"]))),
    "observe": ("Read an app's window: every button, field, item and text on screen, each with an id and what can be "
                "done to it. window: part of a title when the app has several.", {"app": S, "window": S}, ["app"],
                lambda a: D.text(D.observe(a["app"], a.get("window", "")))),
    "act": ("Do one thing to one element of a snapshot, in the background: CLICK, DOUBLE_CLICK, RIGHT_CLICK, TYPE "
            "(a field's new text; in a document it's written at the cursor), REPLACE, SET_VALUE, FOCUS, READ, SCROLL_TO. "
            "If the app changed since the snapshot nothing happens and the new screen comes back. Answers with the "
            "screen once the app has settled.", {"snapshot": S, "element": S, "action": S, "value": S},
            ["snapshot", "element", "action"],
            lambda a: _with_text(D.act(a["snapshot"], a["element"], a["action"], str(a.get("value", ""))))),
    "keys": ("Press keys: chords like enter, ctrl+s, page_down, or text:words to type. app: the app to send them to "
             "(its window gets the keyboard for the keys, then yours gets it back).", {"keys": ARR, "app": S}, ["keys"],
             lambda a: _with_text(D.keys(list(a["keys"]), a.get("app", "")))),
    "windows": ("Monitors (which is the laptop's own screen) and windows: id, app, title, monitor, size, place.", {}, [],
                lambda a: _windows()),
    "place_window": ("Move or resize a window instantly. window: id, app or part of a title. monitor: laptop, external, "
                     "other, left, right, primary, 1.., or a name. where: left/right/top/bottom, top-left.., "
                     "left-third, center-third, right-third, left-two-thirds, right-two-thirds, center, full. size: "
                     "1280x800, 60%, 70%x80%. state: maximize, restore, minimize, fullscreen, unfullscreen. focus: bring it "
                     "to the front.", {"window": S, "monitor": S, "where": S, "size": S, "state": S, "focus": B}, ["window"],
                     lambda a: json.dumps(D.place(a["window"], a.get("monitor"), a.get("where", ""), a.get("size", ""),
                                                  a.get("state", ""), bool(a.get("focus"))))),
}


def handle(msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None  # a notification (initialized, cancelled): nothing to answer
    if method == "initialize":
        result = {"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "snoofy", "version": VERSION}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": n, "description": d, "inputSchema": {"type": "object", "properties": p, "required": r}}
                            for n, (d, p, r, _) in TOOLS.items()]}
    elif method == "tools/call":
        name, args = msg["params"]["name"], msg["params"].get("arguments") or {}
        if name not in TOOLS:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"No tool {name}"}}
        try:
            result = {"content": [{"type": "text", "text": TOOLS[name][3](args)}]}
        except Exception as e:  # the agent reads why and decides again
            print(traceback.format_exc(), file=sys.stderr)
            result = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"No method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            out = handle(json.loads(line))
        except Exception as e:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}}
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
