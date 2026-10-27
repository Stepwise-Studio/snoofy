"""Snoofy's own logic that needs no desktop: how monitors, sizes and windows are named, and the MCP handshake.
(Run: python3 -m pytest test_snoofy.py. The real desktop is exercised by the app that embeds it.)"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
D = pytest.importorskip("snoofy")  # needs the system's PyGObject
M = pytest.importorskip("snoofy_mcp")

MONS = [{"index": 0, "x": 0, "y": 0, "name": "LG 27\"", "connector": "HDMI-1", "builtin": False, "primary": True},
        {"index": 1, "x": 2560, "y": 0, "name": "Built-in display", "connector": "eDP-1", "builtin": True, "primary": False}]
WINS = [{"id": "1", "app": "firefox", "title": "Docs", "focused": False},
        {"id": "2", "app": "org.gnome.TextEditor", "title": "notes.txt", "focused": False},
        {"id": "3", "app": "firefox", "title": "Mail", "focused": True}]


def test_monitors_by_what_people_call_them():
    assert D._monitor("laptop", MONS, 0) == 1 and D._monitor("external", MONS, 1) == 0
    assert D._monitor("other", MONS, 0) == 1 and D._monitor("left", MONS, 1) == 0 and D._monitor("2", MONS, 0) == 1
    assert D._monitor("lg", MONS, 1) == 0 and D._monitor("primary", MONS, 1) == 0
    with pytest.raises(LookupError):
        D._monitor("tv", MONS, 0)


def test_sizes_are_centred_parts_of_the_work_area():
    assert D._size("50%", {"w": 1000, "h": 800}) == (0.25, 0.25, 0.5, 0.5)
    assert D._size("500x400", {"w": 1000, "h": 800}) == (0.25, 0.25, 0.5, 0.5)
    assert D._size("4000x4000", {"w": 1000, "h": 800}) == (0, 0, 1, 1)  # never bigger than the screen


def test_windows_by_id_app_or_title():
    assert D._window("2", WINS)["id"] == "2" and D._window("notes", WINS)["id"] == "2"
    assert D._window("firefox", WINS)["id"] == "3"  # the focused one when several match
    with pytest.raises(LookupError):
        D._window("slack", WINS)


def test_mcp_lists_and_calls_tools():
    assert M.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})["result"]["serverInfo"]
    assert M.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = {t["name"] for t in M.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
    assert {"observe", "act", "keys", "windows", "place_window", "setup"} <= names
    bad = M.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "act", "arguments": {
        "snapshot": "s999", "element": "e1", "action": "CLICK"}}})["result"]
    assert bad["isError"] and "Snapshot s999 is gone" in bad["content"][0]["text"]


def test_risky_labels():
    assert D.risky("Delete Draft") and D.risky("Send") and D.risky("Place order")
    assert not D.risky("Clear") and not D.risky("Close") and not D.risky("Sender")
