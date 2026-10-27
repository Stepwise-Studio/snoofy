"""Snoofy: computer use for Linux (GNOME). An agent reads this computer's apps and works in them, and arranges windows.

Apps are read through the desktop's accessibility layer (AT-SPI): a window becomes a snapshot of what's on screen in
it, each element with an id, a role, a name, a value and what can be done to it. An agent acts on an element by id:
click, type or set its text, set a value, focus, read its text. Those go straight to the app (in the background: the
pointer and the window in front stay as they are). Keys go to the window in front, through GNOME's remote-desktop
interface (Wayland lets nothing else type).

Every action is checked against the app as it is when it runs: if the element moved or changed, or a dialog came or
went since the snapshot, nothing happens and a fresh snapshot comes back instead. After acting it waits for the app
to settle (until its accessibility events go quiet), then returns the new snapshot.

Window control, the global hotkey and the status pill live in Snoofy's GNOME Shell extension (snoofy-shell.js).
"""
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import gi
except ImportError:  # in a uv or pipx environment: the system's own PyGObject (it can't be pip-installed without a compiler)
    sys.path.append("/usr/lib/python3/dist-packages")
    import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, Gio, GLib  # noqa: E402

MAX_ELEMENTS = 260
MAX_DEPTH = 40
MAX_TEXT = 300
SKIP_APPS = {"gnome-shell", "ibus-extension-gtk3", "evolution-alarm-notify", "xdg-desktop-portal-gtk",
             "xdg-desktop-portal-gnome", "update-notifier", "mutter-x11-frames", "gsd-media-keys", "tubelight"}
WINDOW_ROLES = {"frame", "window", "dialog", "alert", "file chooser"}
# Containers worth showing even without a name (they give the list its shape).
SHAPE_ROLES = {"dialog", "alert", "menu bar", "menu", "tool bar", "list", "list box", "tree", "tree table", "table",
               "page tab list", "tab list", "combo box", "popup menu", "document web", "document frame", "form"}
FOCUS_ROLES = {"text", "entry", "list item", "tree item", "table cell", "terminal", "document web", "document frame",
               "document text", "link", "paragraph", "image", "canvas", "icon"}
CLICKS = ("click", "press", "activate", "jump", "toggle", "open", "select", "expand or contract")

_lock = threading.RLock()
_snaps: dict[str, dict] = {}
_ids: dict[tuple, str] = {}
_count = [0, 0]  # snapshots, element ids


def available() -> bool:
    try:
        return Atspi.get_desktop(0).get_child_count() >= 0
    except Exception:
        return False


def _s(f, default=""):
    try:
        v = f()
        return default if v is None else v
    except Exception:
        return default


def _states(acc) -> set:
    st = _s(acc.get_state_set, None)
    if st is None:
        return set()
    names = {Atspi.StateType.SHOWING: "showing", Atspi.StateType.VISIBLE: "visible", Atspi.StateType.FOCUSED: "focused",
             Atspi.StateType.CHECKED: "checked", Atspi.StateType.SELECTED: "selected", Atspi.StateType.ENABLED: "enabled",
             Atspi.StateType.EXPANDED: "expanded", Atspi.StateType.ACTIVE: "active", Atspi.StateType.EDITABLE: "editable",
             Atspi.StateType.FOCUSABLE: "focusable", Atspi.StateType.CHECKABLE: "checkable", Atspi.StateType.PRESSED: "pressed",
             Atspi.StateType.MULTI_LINE: "multi line"}
    return {n for t, n in names.items() if st.contains(t)}


# ------------------------------------------------------------------ apps and windows
def _apps():
    desk = Atspi.get_desktop(0)
    for i in range(_s(desk.get_child_count, 0)):
        app = _s(lambda: desk.get_child_at_index(i), None)
        if app is None:
            continue
        name = _s(app.get_name)
        if not name or name in SKIP_APPS:
            continue
        wins = []
        for j in range(min(_s(app.get_child_count, 0), 30)):
            w = _s(lambda: app.get_child_at_index(j), None)
            if w is not None and _s(w.get_role_name) in WINDOW_ROLES:
                wins.append((j, w))
        if wins:
            yield app, name, wins


def apps() -> list[dict]:
    """Apps with windows: name, pid, each window's title and whether it's the one in front."""
    out = []
    for app, name, wins in _apps():
        out.append({"app": name, "pid": _s(app.get_process_id, 0),
                    "windows": [{"title": _s(w.get_name) or "(untitled)", "active": "active" in _states(w)} for _, w in wins]})
    return out


def _find(app_name: str, window: str = ""):
    """The app (by name, or a window title it has) and one window: the named one, else its active one, else its first."""
    want, wtitle = app_name.strip().lower(), window.strip().lower()
    flat = re.sub(r"[^a-z0-9]", "", want)  # the window list's "org.gnome.TextEditor" is AT-SPI's "gnome-text-editor"
    hits = []
    for app, name, wins in _apps():
        titles = [(j, w, _s(w.get_name).lower()) for j, w in wins]
        named = re.sub(r"[^a-z0-9]", "", name.lower())
        if want and want not in name.lower() and not (flat and named and (flat in named or named in flat)) \
                and not any(want in t or wtitle and wtitle in t for _, _, t in titles):
            continue
        hits.append((app, name, titles))
    if not hits:
        raise LookupError(f"No open app called {app_name!r}. Open apps: " + ", ".join(a["app"] for a in apps()) + ".")
    hits.sort(key=lambda h: (want != h[1].lower(), not any("active" in _states(w) for _, w, _ in h[2])))
    app, name, titles = hits[0]
    pick = next(((j, w) for j, w, t in titles if wtitle and wtitle in t), None)
    pick = pick or next(((j, w) for j, w, _ in titles if "active" in _states(w)), None) or (titles[0][0], titles[0][1])
    return app, name, pick[0], pick[1]


# ------------------------------------------------------------------ snapshots
# The window is read straight off the accessibility bus with every call of a level in flight at once (libatspi asks
# one property at a time, and GTK 4 has no bulk Collection call): ~20 ms for a GTK window instead of ~90. Elements
# are named by their accessibility object, so an element keeps its id from one snapshot to the next while it exists.
ACC, PROPS = "org.a11y.atspi.Accessible", "org.freedesktop.DBus.Properties"
I_ACTION, I_TEXT, I_EDIT, I_VALUE, I_COMP = (f"org.a11y.atspi.{i}" for i in ("Action", "Text", "EditableText", "Value", "Component"))
VALUE_ROLES = ("slider", "spin button", "progress bar", "scroll bar", "level bar", "dial")
TEXT_ROLES = ("text", "entry", "password text", "terminal", "paragraph", "editbar", "spin button", "combo box")
CHECK_ROLES = ("check box", "radio button", "toggle button", "check menu item", "radio menu item")
MAX_NODES = 4000  # read at most this many showing nodes (a browser page can have tens of thousands)
_BITS = {n: int(getattr(Atspi.StateType, n.upper().replace(" ", "_"))) for n in
         ("showing", "visible", "focused", "checked", "selected", "enabled", "expanded", "active", "editable",
          "focusable", "checkable", "pressed", "multi line")}
_bus: list = []


def _a11y():
    """Our own connection to the accessibility bus (libatspi's is private)."""
    if not _bus:
        sess = Gio.bus_get_sync(Gio.BusType.SESSION)
        addr = sess.call_sync("org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress", None,
                              GLib.VariantType("(s)"), 0, 3000, None).unpack()[0]
        _bus.append(Gio.DBusConnection.new_for_address_sync(
            addr, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None))
    return _bus[0]


def _get(dest: str, path: str, method: str, args=None, iface: str = ACC):
    """One call, waited for (acting and checking: a handful of calls, not a tree)."""
    return _a11y().call_sync(dest, path, iface, method, args, None, 0, 3000, None).unpack()


def _prop(dest: str, path: str, iface: str, name: str):
    return _get(dest, path, "Get", GLib.Variant("(ss)", (iface, name)), PROPS)[0]


def _role(n: int) -> str:
    """The role as libatspi names it ("text", not the bus's "text box")."""
    return _s(lambda: Atspi.role_get_name(Atspi.Role(n)), "unknown")


def _state_names(st) -> set:
    bits = st[0] | st[1] << 32
    return {n for n, b in _BITS.items() if bits >> b & 1}


def _actions(names: list[str], ifaces: set, role: str, states: set) -> list[str]:
    out = []
    if any(n in CLICKS for n in names):
        out.append("CLICK")
    if I_EDIT in ifaces and "editable" in states:
        out.append("TYPE")
    if I_VALUE in ifaces and role in ("slider", "spin button", "scroll bar", "dial"):
        out.append("SET_VALUE")
    if "focusable" in states and "CLICK" not in out and "TYPE" not in out and role in FOCUS_ROLES:
        out.append("FOCUS")
    return out


def _value(n: dict) -> str:
    if "checkable" in n["states"] or n["role"] in CHECK_ROLES:
        return "on" if "checked" in n["states"] or "pressed" in n["states"] else "off"
    if "current" in n:
        return f"{n['current']:g}"
    if "count" in n:
        if n["role"] == "password text":
            return "•" * min(n["count"], 8)
        return n.get("text", "") + ("…" if n["count"] > MAX_TEXT else "")
    return ""


def _read(dest: str, root: str) -> list[dict]:
    """Every showing node under `root`, in tree order, read with the calls of each level in flight together."""
    ctx = GLib.MainContext.new()
    ctx.push_thread_default()
    try:
        loop, bus, pending, nodes = GLib.MainLoop.new(ctx, False), _a11y(), [0], {}

        def call(path, iface, method, args, then) -> None:
            pending[0] += 1

            def done(conn, res) -> None:
                try:
                    v = conn.call_finish(res).unpack()
                except GLib.Error:
                    v = None
                try:
                    then(v)
                finally:
                    pending[0] -= 1
                    if not pending[0]:
                        loop.quit()
            bus.call(dest, path, iface, method, args, None, 0, 3000, None, done)

        def visit(path: str, order: tuple, depth: int, parent: str) -> None:
            if len(nodes) >= MAX_NODES or depth > MAX_DEPTH:
                return
            n = nodes[path] = {"path": path, "order": order, "depth": depth, "parent": parent}
            got: dict = {}

            def part(key):
                def then(v) -> None:
                    got[key] = v
                    if len(got) == 5:
                        basics(n, got)
                return then
            call(path, ACC, "GetState", None, part("state"))
            call(path, ACC, "GetRole", None, part("role"))
            call(path, PROPS, "Get", GLib.Variant("(ss)", (ACC, "Name")), part("name"))
            call(path, ACC, "GetInterfaces", None, part("ifaces"))
            call(path, ACC, "GetChildren", None, part("kids"))  # with the rest: hidden ones aren't followed

        def basics(n: dict, got: dict) -> None:
            states = _state_names(got["state"][0]) if got["state"] else set()
            if n["depth"] and ("showing" not in states or "visible" not in states) or not got["role"]:
                n["skip"] = True
                return
            role, ifaces = _role(got["role"][0]), set(got["ifaces"][0] if got["ifaces"] else ())
            n.update(role=role, states=states, ifaces=ifaces, names=[],
                     name=" ".join(str(got["name"][0] if got["name"] else "").split())[:120])
            if n["depth"]:
                path = n["path"]
                if I_ACTION in ifaces:
                    call(path, I_ACTION, "GetActions", None,
                         lambda v: n.__setitem__("names", [a[0].lower() for a in v[0]] if v else []))
                if I_VALUE in ifaces and role in VALUE_ROLES:
                    call(path, PROPS, "Get", GLib.Variant("(ss)", (I_VALUE, "CurrentValue")),
                         lambda v: v and n.__setitem__("current", float(v[0])))
                if I_TEXT in ifaces and role in TEXT_ROLES:
                    def count(v) -> None:
                        n["count"] = int(v[0]) if v else 0
                        if n["count"] and role != "password text":
                            call(n["path"], I_TEXT, "GetText", GLib.Variant("(ii)", (0, min(n["count"], MAX_TEXT))),
                                 lambda t: t and n.__setitem__("text", t[0]))
                    call(path, PROPS, "Get", GLib.Variant("(ss)", (I_TEXT, "CharacterCount")), count)
            for i, (d, p) in enumerate((got["kids"][0] if got["kids"] else [])[:400]):
                if d == dest and p not in nodes:
                    visit(p, n["order"] + (i,), n["depth"] + 1, n["name"])

        visit(root, (), 0, "")
        stop = GLib.timeout_source_new(5000)
        stop.set_callback(lambda *_: loop.quit() or False)
        stop.attach(ctx)
        if pending[0]:
            loop.run()
        stop.destroy()
    finally:
        ctx.pop_thread_default()
    return sorted((n for n in nodes.values() if "role" in n), key=lambda n: n["order"])


def _walk(dest: str, root: str, pid: int) -> list[dict]:
    out: list[dict] = []
    for n in _read(dest, root):
        if not n["depth"]:
            continue
        acts, value = _actions(n["names"], n["ifaces"], n["role"], n["states"]), _value(n)
        echo = not acts and not value and n["name"] == n["parent"]  # a label repeating its button's name
        if echo or not (n["name"] or acts or value or n["role"] in SHAPE_ROLES):
            continue
        key = (pid, n["path"])
        with _lock:
            if key not in _ids:
                _count[1] += 1
                _ids[key] = f"e{_count[1]}"
        out.append({"id": _ids[key], "path": n["path"], "role": n["role"], "name": n["name"], "value": value,
                    "actions": acts, "depth": n["depth"], "focused": "focused" in n["states"]})
        if len(out) >= MAX_ELEMENTS:
            break
    return out


def _signature(app) -> tuple:
    """The app's shape: its windows and dialogs. A change means the screen isn't what a snapshot showed."""
    sig = []
    for j in range(min(_s(app.get_child_count, 0), 30)):
        w = _s(lambda: app.get_child_at_index(j), None)
        if w is not None:
            sig.append((_s(w.get_role_name), _s(w.get_name)[:60], "showing" in _states(w)))
    return tuple(sig)


def observe(app_name: str, window: str = "") -> dict:
    app, name, index, win = _find(app_name, window)
    pid = _s(app.get_process_id, 0)
    dest = win.app.bus_name
    elements = _walk(dest, win.path, pid)
    with _lock:
        _count[0] += 1
        sid = f"s{_count[0]}"
        _snaps[sid] = {"app": name, "pid": pid, "dest": dest, "window": index, "title": _s(win.get_name), "sig": _signature(app),
                       "elements": {e["id"]: e for e in elements}, "at": time.time()}
        for old in [k for k, v in _snaps.items() if time.time() - v["at"] > 900]:
            _snaps.pop(old, None)
    return {"snapshot": sid, "app": name, "window": _s(win.get_name), "elements": elements,
            "truncated": len(elements) >= MAX_ELEMENTS}


def text(snap: dict) -> str:
    """A snapshot as an agent reads it: one line per element, indented by depth."""
    lines = [f"Snapshot {snap['snapshot']}: {snap['app']}, window “{snap['window']}”"]
    base = min((e["depth"] for e in snap["elements"]), default=1)
    for e in snap["elements"]:
        bits = [f"[{e['id']}]", e["role"]]
        if e["name"]:
            bits.append(f"“{e['name']}”")
        if e["value"]:
            bits.append(f"= {e['value']!r}" if len(e["value"]) < 80 else f"= {e['value'][:80]!r}…")
        if e["actions"]:
            bits.append("(" + ", ".join(a.lower() for a in e["actions"]) + ")")
        if e["focused"]:
            bits.append("*focused*")
        lines.append("  " * min(e["depth"] - base, 8) + " ".join(bits))
    if snap.get("truncated"):
        lines.append(f"(only the first {MAX_ELEMENTS} elements; act on what's here or look at another window)")
    return "\n".join(lines)


# ------------------------------------------------------------------ acting
class _Events:
    """Counts the apps' accessibility events while an action runs, to tell when they've settled."""

    def __init__(self) -> None:
        self.n = 0
        self.last = 0.0
        self.loop: GLib.MainLoop | None = None
        self.listener = None
        self.dest: str | None = None

    def start(self) -> None:
        if self.loop:
            return
        ready = threading.Event()

        def run() -> None:
            ctx = GLib.MainContext.default()
            self.loop = GLib.MainLoop.new(ctx, False)
            ready.set()
            self.loop.run()
        threading.Thread(target=run, daemon=True, name="atspi-events").start()
        ready.wait(2)

    def _hit(self, event) -> None:
        if self.dest and _s(lambda: event.source.app.bus_name, None) not in (self.dest, None):
            return  # another app talking (a browser streaming text) says nothing about this one
        self.n += 1
        self.last = time.time()

    def on(self, dest: str | None):
        """Listen to one app only (by its accessibility bus name)."""
        self.dest = dest
        return self

    def __enter__(self):
        self.start()
        self.n, self.last = 0, 0.0
        self.listener = Atspi.EventListener.new(self._hit)
        for kind in ("object:children-changed", "object:state-changed", "object:property-change", "object:text-changed",
                     "object:value-changed", "window:create", "window:activate", "focus:"):
            _s(lambda k=kind: self.listener.register(k), None)
        return self

    def settle(self, quiet: float = 0.15, idle: float = 0.6, most: float = 2.0) -> bool:
        """Waits for the app to finish reacting; False when it announced nothing (the action most likely did nothing)."""
        began = time.time()
        while time.time() - began < most:
            time.sleep(0.01)
            if not self.n and time.time() - began >= idle:
                return False
            if self.n and time.time() - self.last >= quiet:
                return True
        return bool(self.n)

    def __exit__(self, *exc) -> None:
        for kind in ("object:children-changed", "object:state-changed", "object:property-change", "object:text-changed",
                     "object:value-changed", "window:create", "window:activate", "focus:"):
            _s(lambda k=kind: self.listener.deregister(k), None)


_events = _Events()


def act(sid: str, element: str, action: str, value: str = "") -> dict:
    """Do one thing to one element of a snapshot. Returns status done | changed | stale, and a fresh snapshot."""
    snap = _snaps.get(sid)
    if not snap:
        raise LookupError(f"Snapshot {sid} is gone; look again.")
    e = snap["elements"].get(element)
    if not e:
        raise LookupError(f"No element {element} in snapshot {sid}.")
    app, name, index, win = _find(snap["app"])
    if _s(app.get_process_id, 0) != snap["pid"]:
        return {"status": "changed", "why": f"{snap['app']} was restarted", **observe(snap["app"])}
    if _signature(app) != snap["sig"]:
        return {"status": "changed", "why": "a window or dialog came or went since that snapshot", **observe(snap["app"])}
    dest, path = snap["dest"], e["path"]
    try:
        role, name_ = _role(_get(dest, path, "GetRole")[0]), " ".join(str(_prop(dest, path, ACC, "Name")).split())[:120]
    except GLib.Error:
        role = name_ = None
    if role != e["role"] or name_ != e["name"]:
        return {"status": "stale", "why": "that element changed or went away", **observe(snap["app"])}
    action = action.upper()
    with _events.on(dest) as ev:
        did = _do(dest, path, e, action, value)
        reacted = ev.settle()
    return {"status": "done", "did": did, "reacted": reacted, **observe(snap["app"], snap["title"])}


# Labels that read like something the user would lose or couldn't take back (arc-cua's risk words, minus the routine
# "close" and "clear"). Callers ask the user before pressing one: act() itself doesn't refuse, the caller decides.
RISKY = re.compile(r"\b(delete|remove|erase|trash|discard|clear all|empty trash|permanently|send|post|publish|share|"
                   r"reply all|forward|tweet|buy|purchase|pay|checkout|check out|place order|order now|subscribe|donate|"
                   r"quit|sign out|log out|logout|shut down|restart|uninstall|reset)\b", re.I)


def risky(name: str) -> bool:
    return bool(RISKY.search(name or ""))


def element(sid: str, eid: str) -> dict:
    """One element of a snapshot, as observe() returned it."""
    snap = _snaps.get(sid)
    if not snap:
        raise LookupError(f"Snapshot {sid} is gone; look again.")
    if eid not in snap["elements"]:
        raise LookupError(f"No element {eid} in snapshot {sid}.")
    return snap["elements"][eid]


def _ok(v) -> bool:
    return bool(v and v[0])


def _do(dest: str, path: str, e: dict, action: str, value: str) -> str:
    """One action on one element, through the app's own accessibility calls (no pointer, no focus change)."""
    what = f"{e['role']} “{e['name']}”"
    ifaces = set(_get(dest, path, "GetInterfaces")[0])

    def do_action(want: tuple[str, ...]) -> bool:
        names = [a[0].lower() for a in _get(dest, path, "GetActions", iface=I_ACTION)[0]] if I_ACTION in ifaces else []
        i = next((i for i, n in enumerate(names) if n in want), None)
        return i is not None and _ok(_get(dest, path, "DoAction", GLib.Variant("(i)", (i,)), I_ACTION))

    def text() -> str:
        n = int(_prop(dest, path, I_TEXT, "CharacterCount"))
        return _get(dest, path, "GetText", GLib.Variant("(ii)", (0, min(n, 12000))), I_TEXT)[0]

    if action == "CLICK":
        if not do_action(CLICKS):
            raise ValueError(f"{what} can't be clicked; try FOCUS then a key.")
        return f"clicked {what}"
    if action == "DOUBLE_CLICK":  # what a double click means to an app: open/activate the item
        if not (do_action(("activate", "open")) or do_action(CLICKS) and do_action(CLICKS)):
            raise ValueError(f"{what} can't be opened or activated.")
        return f"activated {what}"
    if action == "RIGHT_CLICK":
        if not do_action(("show menu", "showmenu", "menu", "popup", "context menu")):
            raise ValueError(f"{what} has no menu of its own; FOCUS it and press shift+f10.")
        return f"opened the menu of {what}"
    if action in ("TYPE", "TYPE_TEXT", "SET_TEXT", "REPLACE"):
        if I_EDIT not in ifaces:
            raise ValueError(f"{what} doesn't take text.")
        _s(lambda: _get(dest, path, "GrabFocus", None, I_COMP), None)
        states = _state_names(_get(dest, path, "GetState")[0])
        if "multi line" in states and action != "REPLACE":
            # A document: write at the cursor, never over what's there (REPLACE does that, on purpose).
            at = int(_s(lambda: _prop(dest, path, I_TEXT, "CaretOffset"), -1))
            at = at if at >= 0 else int(_s(lambda: _prop(dest, path, I_TEXT, "CharacterCount"), 0))
            if not _ok(_get(dest, path, "InsertText", GLib.Variant("(isi)", (at, value, len(value.encode()))), I_EDIT)):
                raise ValueError(f"{what} didn't take the text.")
            return f"typed into {what} at the cursor"
        if not _ok(_get(dest, path, "SetTextContents", GLib.Variant("(s)", (value,)), I_EDIT)):
            raise ValueError(f"{what} didn't take the text.")
        return f"set the text of {what}"
    if action == "SET_VALUE":
        if I_VALUE not in ifaces:
            raise ValueError(f"{what} has no value to set.")
        _get(dest, path, "Set", GLib.Variant("(ssv)", (I_VALUE, "CurrentValue", GLib.Variant("d", float(value)))), PROPS)
        return f"set {what} to {value}"
    if action == "FOCUS":
        if not _ok(_s(lambda: _get(dest, path, "GrabFocus", None, I_COMP), None)):
            raise ValueError(f"{what} can't take focus.")
        return f"focused {what}"
    if action == "READ":
        return text() if I_TEXT in ifaces else e["name"] or e["value"]
    if action == "SCROLL_TO":
        if not _ok(_s(lambda: _get(dest, path, "ScrollTo", GLib.Variant("(u)", (int(Atspi.ScrollType.ANYWHERE),)), I_COMP), None)):
            raise ValueError("It can't be scrolled into view.")
        return f"scrolled to {what}"
    raise ValueError("action is CLICK, DOUBLE_CLICK, RIGHT_CLICK, TYPE, REPLACE, SET_VALUE, FOCUS, READ or SCROLL_TO "
                     "(keys: use the keys tool)")


# ------------------------------------------------------------------ keys, through GNOME's remote-desktop interface
KEYSYMS = {
    "enter": 0xff0d, "return": 0xff0d, "tab": 0xff09, "escape": 0xff1b, "esc": 0xff1b, "backspace": 0xff08,
    "delete": 0xffff, "up": 0xff52, "down": 0xff54, "left": 0xff51, "right": 0xff53, "home": 0xff50, "end": 0xff57,
    "pageup": 0xff55, "page_up": 0xff55, "pagedown": 0xff56, "page_down": 0xff56, "space": 0x20, "insert": 0xff63,
    "ctrl": 0xffe3, "control": 0xffe3, "shift": 0xffe1, "alt": 0xffe9, "super": 0xffeb, "meta": 0xffeb, "mod": 0xffe3,
    **{f"f{i}": 0xffbd + i for i in range(1, 13)},
}


def _keysym(token: str) -> int:
    t = token.strip()
    if t.lower() in KEYSYMS:
        return KEYSYMS[t.lower()]
    if len(t) == 1:
        cp = ord(t.lower()) if t.isalpha() else ord(t)
        return cp if cp < 0x100 else 0x01000000 + cp
    raise ValueError(f"Unknown key {token!r}")


class _Remote:
    """One remote-desktop session at a time, opened when keys are needed and closed soon after."""

    def __init__(self) -> None:
        self.path: str | None = None
        self.used = 0.0
        self.bus = None

    def _open(self) -> None:
        self.bus = self.bus or Gio.bus_get_sync(Gio.BusType.SESSION)
        if self.path:
            return
        res = self.bus.call_sync("org.gnome.Mutter.RemoteDesktop", "/org/gnome/Mutter/RemoteDesktop",
                                 "org.gnome.Mutter.RemoteDesktop", "CreateSession", None, GLib.VariantType("(o)"), 0, 5000, None)
        self.path = res.unpack()[0]
        self._call("Start")
        threading.Thread(target=self._reaper, daemon=True).start()

    def _call(self, method: str, args=None) -> None:
        self.bus.call_sync("org.gnome.Mutter.RemoteDesktop", self.path, "org.gnome.Mutter.RemoteDesktop.Session",
                           method, args, None, 0, 5000, None)

    def _reaper(self) -> None:  # no session (and no "remote" indicator) left open when idle
        while self.path:
            time.sleep(5)
            if time.time() - self.used > 20:
                with _lock:
                    _s(lambda: self._call("Stop"), None)
                    self.path = None

    def keys(self, combo: str) -> None:
        with _lock:
            self._open()
            self.used = time.time()
            syms = [_keysym(k) for k in combo.split("+") if k.strip()] if len(combo) > 1 else [_keysym(combo)]
            for s in syms:
                self._call("NotifyKeyboardKeysym", GLib.Variant("(ub)", (s, True)))
            for s in reversed(syms):
                self._call("NotifyKeyboardKeysym", GLib.Variant("(ub)", (s, False)))
            self.used = time.time()

    def type(self, text_: str) -> None:
        with _lock:
            self._open()
            for ch in text_:
                s = 0xff0d if ch == "\n" else 0xff09 if ch == "\t" else (ord(ch) if ord(ch) < 0x100 else 0x01000000 + ord(ch))
                self._call("NotifyKeyboardKeysym", GLib.Variant("(ub)", (s, True)))
                self._call("NotifyKeyboardKeysym", GLib.Variant("(ub)", (s, False)))
            self.used = time.time()


_remote = _Remote()


def keys(sequence: list[str], app_name: str = "") -> dict:
    """Presses each chord ("ctrl+s", "enter", "page_down") or types text ("text:hello"). Keys only ever reach the
    window in front, so with window control on, `app_name`'s window is lent the focus for the keys and the user's
    window gets it back after; without it they go to whatever is in front."""
    lent = _lend(app_name) if app_name else None
    try:
        with _events.on(None) as ev:
            for k in sequence:
                if k.startswith("text:"):
                    _remote.type(k[5:])
                else:
                    _remote.keys(k)
                time.sleep(0.03)
            reacted = ev.settle()
    finally:
        if lent:
            _s(lambda: _shell("Place", {"id": lent, "focus": True}), None)
    front = next((a["app"] for a in apps() if any(w["active"] for w in a["windows"])), "")
    out = {"status": "done", "did": f"pressed {', '.join(sequence)}", "reacted": reacted, "front": front}
    target = app_name or front
    if target:
        try:
            out.update(observe(target))
        except LookupError:
            pass
    return out


# ------------------------------------------------------------------ launching
def _flat(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


def launch(name: str) -> dict:
    """Opens an app by name (as its menu entry calls it), or brings its window to the front if it's already open
    (an app like Firefox or Zen hands a second start to the window it has). Never waits more than 4 s: a slow app
    comes back as "starting"."""
    hits = [h for group in Gio.DesktopAppInfo.search(name) for h in group]
    info = next((Gio.DesktopAppInfo.new(h) for h in hits if Gio.DesktopAppInfo.new(h)), None)
    keys = {_flat(name)} | ({_flat(info.get_id().removesuffix(".desktop")), _flat(info.get_startup_wm_class() or ""),
                             _flat(info.get_name())} if info else set())
    keys.discard("")
    same = lambda *labels: any(k and l and (k in l or l in k) for k in keys for l in map(_flat, labels))  # noqa: E731
    try:
        wins = _shell("List")["windows"]
    except RuntimeError:
        wins = None  # no window control here: AT-SPI's app names only
    mine = [w for w in wins or [] if same(w["app"], w["title"].rsplit("—", 1)[-1], w["title"].rsplit(" - ", 1)[-1])]
    running = mine or [a for a in apps() if same(a["app"])]
    if running:
        if mine:
            _shell("Place", {"id": mine[-1]["id"], "focus": True})
        if mine:  # browsers draw windows in one process and talk to accessibility from another: match by name too
            app = next((a["app"] for a in apps() if a["pid"] == mine[-1]["pid"] or same(a["app"])), None)
            return {"status": "running", **(_s(lambda: observe(app), {"app": app}) if app else {"app": mine[-1]["app"]})}
        return {"status": "running", **_look_pid(running[0]["pid"])}
    if not info:
        raise LookupError(f"No app called {name!r} is installed.")
    before_wins = {w["id"] for w in wins or []}
    before_apps = {a["pid"] for a in apps()}
    subprocess.Popen(["gtk-launch", info.get_id()], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)  # its own session: it outlives this call
    for _ in range(40):
        time.sleep(0.1)
        new = [w["pid"] for w in _s(lambda: _shell("List")["windows"], []) if w["id"] not in before_wins] if wins is not None \
            else [a["pid"] for a in apps() if a["pid"] not in before_apps]
        if new:
            time.sleep(0.3)  # let its first window fill in
            return {"status": "opened", **_look_pid(new[0])}
    return {"status": "starting", "app": info.get_name(), "why": "it's still starting"}


def _look_pid(pid: int) -> dict:
    """A look at the app with this process, if accessibility knows it yet (else just its name)."""
    app = next((a["app"] for a in apps() if a["pid"] == pid), None)
    try:
        return observe(app) if app else {"app": str(pid)}
    except LookupError:
        return {"app": app}


# ------------------------------------------------------------------ windows (Snoofy, a GNOME Shell extension)
# On Wayland no app may move another's window: the shell does it for us, one D-Bus call per move (1-12 ms).
EXT = "snoofy@stepwise.studio"
EXT_DIR = Path.home() / ".local/share/gnome-shell/extensions" / EXT
HERE = Path(__file__).resolve().parent
PRESETS = {  # where: x, y, w, h as parts of the monitor's work area
    "left": (0, 0, .5, 1), "right": (.5, 0, .5, 1), "top": (0, 0, 1, .5), "bottom": (0, .5, 1, .5),
    "top-left": (0, 0, .5, .5), "top-right": (.5, 0, .5, .5), "bottom-left": (0, .5, .5, .5), "bottom-right": (.5, .5, .5, .5),
    "left-third": (0, 0, 1 / 3, 1), "center-third": (1 / 3, 0, 1 / 3, 1), "right-third": (2 / 3, 0, 1 / 3, 1),
    "left-two-thirds": (0, 0, 2 / 3, 1), "right-two-thirds": (1 / 3, 0, 2 / 3, 1),
}
STATES = {"maximize", "unmaximize", "minimize", "fullscreen", "unfullscreen", "close"}


def _shell(method: str, arg=None) -> dict:
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    try:
        r = bus.call_sync("org.gnome.Shell", "/studio/stepwise/Snoofy", "studio.stepwise.Snoofy",
                          method, GLib.Variant("(s)", (json.dumps(arg),)) if arg is not None else None,
                          GLib.VariantType("(s)"), 0, 3000, None)
    except GLib.Error as e:
        if any(k in e.message for k in ("UnknownMethod", "No such interface", "doesn't exist", "does not exist", "UnknownObject")):
            raise RuntimeError(windows_state()["message"]) from None
        raise RuntimeError(e.message.split(": ", 1)[-1]) from None
    return json.loads(r.unpack()[0])


def windows_state() -> dict:
    """Whether window control works here: ready, not installed, or installed but waiting for the next log-in."""
    try:
        Gio.bus_get_sync(Gio.BusType.SESSION).call_sync(
            "org.gnome.Shell", "/studio/stepwise/Snoofy", "studio.stepwise.Snoofy", "List",
            None, GLib.VariantType("(s)"), 0, 2000, None)
        return {"state": "ready", "message": "Window control is on."}
    except GLib.Error:
        pass
    if not (EXT_DIR / "extension.js").exists():
        return {"state": "missing", "message": "Window control isn't installed: run the setup (install_windows)."}
    return {"state": "relogin", "message": "Window control is installed and switches on the next time you log in "
                                            "(GNOME only loads new extensions at log-in)."}


def install_windows() -> dict:
    """Puts the Snoofy extension in place and turns it on (it starts working at the next log-in)."""
    src = {n: HERE / n for n in ("snoofy-shell.js", "snoofy-shell.json")}
    if not all(p.exists() for p in src.values()):
        raise FileNotFoundError("The extension's files aren't next to the driver.")
    EXT_DIR.mkdir(parents=True, exist_ok=True)
    changed = False
    for name, dst in (("snoofy-shell.js", "extension.js"), ("snoofy-shell.json", "metadata.json")):
        data = src[name].read_bytes()
        if not (EXT_DIR / dst).exists() or (EXT_DIR / dst).read_bytes() != data:
            (EXT_DIR / dst).write_bytes(data)
            changed = True
    settings = Gio.Settings.new("org.gnome.shell")
    on = list(settings.get_strv("enabled-extensions"))
    if EXT not in on:
        settings.set_strv("enabled-extensions", on + [EXT])
    if changed and shutil.which("gnome-extensions"):  # already loaded (an update): reload it now
        subprocess.run(["gnome-extensions", "disable", EXT], capture_output=True, timeout=5)
        subprocess.run(["gnome-extensions", "enable", EXT], capture_output=True, timeout=5)
    return windows_state()


def _monitors(raw: list[dict]) -> list[dict]:
    """The shell's monitors with what GNOME calls them and which one is the laptop's own screen."""
    names = {}
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        _serial, mons, logical, _props = bus.call_sync(
            "org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig", "org.gnome.Mutter.DisplayConfig",
            "GetCurrentState", None, None, 0, 3000, None).unpack()
        info = {conn: (props.get("display-name") or conn, bool(props.get("is-builtin"))) for (conn, *_), _m, props in mons}
        for x, y, _scale, _t, _primary, owners, _p in logical:
            names[(x, y)] = (owners[0][0], *info.get(owners[0][0], (owners[0][0], False)))
    except Exception:
        pass
    out = []
    for m in raw:
        conn, label, builtin = names.get((m["x"], m["y"]), ("", f"Monitor {m['index'] + 1}", False))
        out.append({**m, "name": "Built-in display" if builtin and label == conn else label, "connector": conn,
                    "builtin": builtin})
    return out


def windows() -> dict:
    """Every window (stacked bottom to top) and every monitor."""
    got = _shell("List")
    return {"windows": got["windows"], "monitors": _monitors(got["monitors"])}


def _window(spec: str, wins: list[dict]) -> dict:
    """A window by id, else by app or title (the one on top when several match)."""
    spec = str(spec).strip()
    hit = [w for w in wins if w["id"] == spec]
    if not hit:
        low = spec.lower()
        hit = [w for w in wins if low in w["app"].lower() or low in w["title"].lower()]
    if not hit:
        raise LookupError(f"No window matches {spec!r}. Windows: " + "; ".join(f"{w['app']} “{w['title'][:40]}”" for w in wins))
    return next((w for w in hit if w["focused"]), hit[-1])


def _monitor(spec, mons: list[dict], current: int) -> int:
    """laptop / built-in, external / other monitor, left / right, primary, next, a number from 1, or a name."""
    if isinstance(spec, int) or str(spec).strip().isdigit():
        i = int(spec) - 1
        if not 0 <= i < len(mons):
            raise LookupError(f"There are {len(mons)} monitors.")
        return i
    s = str(spec).strip().lower()
    order = sorted(mons, key=lambda m: (m["x"], m["y"]))
    if s in ("laptop", "built-in", "builtin", "internal", "laptop screen"):
        hit = [m for m in mons if m["builtin"]]
    elif s in ("external", "monitor", "external monitor", "big screen"):
        hit = [m for m in mons if not m["builtin"]]
        hit = [m for m in hit if m["index"] != current] if len(hit) > 1 else hit
    elif s in ("other", "next", "the other one"):
        hit = [mons[(current + 1) % len(mons)]]
    elif s in ("left", "leftmost"):
        hit = order[:1]
    elif s in ("right", "rightmost"):
        hit = order[-1:]
    elif s == "primary":
        hit = [m for m in mons if m["primary"]]
    else:
        hit = [m for m in mons if s in m["name"].lower() or s == m["connector"].lower()]
    if not hit:
        raise LookupError(f"No monitor matches {spec!r}. Monitors: " + "; ".join(
            f"{m['index'] + 1} {m['name']}{' (laptop)' if m['builtin'] else ''}" for m in mons))
    return hit[0]["index"]


def _size(size: str, area: dict) -> tuple:
    """"1280x800", "60%" or "70%x80%", centred in the work area, as parts of it."""
    parts = [p.strip() for p in str(size).lower().replace("×", "x").split("x")]
    parts = parts * 2 if len(parts) == 1 else parts[:2]
    w, h = (float(p[:-1]) / 100 if p.endswith("%") else float(p) / dim for p, dim in zip(parts, (area["w"], area["h"])))
    w, h = min(w, 1), min(h, 1)
    return ((1 - w) / 2, (1 - h) / 2, w, h)


def place(window: str, monitor=None, where: str = "", size: str = "", state: str = "", focus: bool = False) -> dict:
    """Moves and/or resizes one window in a single shell call, then reports where it really ended up.
    where: left/right/top/bottom halves, corners, thirds, center, full; size: 1280x800 / 60% (centred)."""
    got = windows()
    w = _window(window, got["windows"])
    mons = got["monitors"]
    arg: dict = {"id": w["id"], "focus": bool(focus)}
    if monitor not in (None, ""):
        arg["monitor"] = _monitor(monitor, mons, w["monitor"])
    where, state = str(where or "").strip().lower().replace(" ", "-").replace("half", "").strip("-"), str(state or "").lower()
    if where in ("full", "fill", "maximize", "maximized", "max"):
        state = "maximize"
    elif where in PRESETS:
        arg["frac"] = PRESETS[where]
    elif where and where not in ("center", "centre", "middle"):
        raise ValueError(f"where is one of {', '.join(PRESETS)}, center, full")
    if size:
        area = mons[arg.get("monitor", w["monitor"])]["area"]
        arg["frac"] = _size(size, area)
    elif where in ("center", "centre", "middle"):
        area = mons[arg.get("monitor", w["monitor"])]["area"]
        arg["frac"] = ((1 - min(w["w"] / area["w"], 1)) / 2, (1 - min(w["h"] / area["h"], 1)) / 2,
                       min(w["w"] / area["w"], 1), min(w["h"] / area["h"], 1))
    if state:
        if state not in STATES | {"restore"}:
            raise ValueError(f"state is one of {', '.join(sorted(STATES))}, restore")
        arg["state"] = "unmaximize" if state == "restore" else state
    got = _shell("Place", arg)
    now, target = got["window"], got.get("target")
    # The app answers its new size a frame or two later: wait until it has the size asked for (an app with a minimum
    # size may stop short: then until it holds still), 300 ms at most.
    rect = lambda x: (x["x"], x["y"], x["w"], x["h"], x["maximized"], x["minimized"])  # noqa: E731
    last, still = rect(now), 0
    for _ in range(20):
        if target and all(abs(a - b) <= 2 for a, b in zip(rect(now), target)):
            break
        time.sleep(0.015)
        now = next((x for x in _shell("List")["windows"] if x["id"] == w["id"]), now)
        still = still + 1 if rect(now) == last else 0
        last = rect(now)
        if still >= (8 if target else 5):
            break
    centred = bool(size) or where in ("center", "centre", "middle")
    if centred and target and (now["w"], now["h"]) != tuple(target[2:]):
        # The app has a minimum or fixed size, and the window manager dropped the move with the resize: centre it
        # again at the size it took.
        area = mons[now["monitor"]]["area"]
        _shell("Place", {"id": w["id"], "px": [(area["w"] - now["w"]) // 2, (area["h"] - now["h"]) // 2, now["w"], now["h"]]})
        time.sleep(0.03)
        now = next((x for x in _shell("List")["windows"] if x["id"] == w["id"]), now)
    m = mons[now["monitor"]] if 0 <= now["monitor"] < len(mons) else {"name": "?"}
    return {**now, "monitor_name": m["name"]}


def _lend(app_name: str) -> str | None:
    """Gives `app_name`'s window the keyboard focus; returns the window that had it (to give it back), or None
    when there's nothing to lend (no window control, or the app is already in front)."""
    try:
        app, *_ = _find(app_name)
        pid = _s(app.get_process_id, 0)
        wins = _shell("List")["windows"]
    except Exception:
        return None
    mine = [w for w in wins if w["pid"] == pid and not w["minimized"]]
    front = next((w for w in wins if w["focused"]), None)
    if not mine or (front and front["pid"] == pid):
        return None
    _shell("Place", {"id": mine[-1]["id"], "focus": True})
    time.sleep(0.04)  # the app hears it has the keyboard
    return front["id"] if front else None


def close_windows(spec: str) -> str:
    """Closes an app's windows the way their close buttons would (by app name: all of them; by title: that one)."""
    wins = _shell("List")["windows"]
    low = spec.strip().lower()
    hit = [w for w in wins if low in w["app"].lower()] or [_window(spec, wins)]
    for w in hit:
        _shell("Place", {"id": w["id"], "state": "close"})
    return f"Closed {len(hit)} window{'s' if len(hit) != 1 else ''} of {hit[0]['app']}."
