# Snoofy

Computer use for Linux (GNOME on Wayland). An agent reads an app's window as a list of elements and acts on them **in
the background** (your pointer and the window in front stay where they are), and arranges windows across monitors
in one call. Use it over MCP from Claude Code, Claude Desktop, Codex, Cursor or any MCP client. No model, no API key,
nothing from PyPI: it uses the system's own accessibility stack (AT-SPI) and a small GNOME Shell extension.

## Set up

Needs `python3-gi` (installed on GNOME desktops) and GNOME 45 or newer.

```bash
claude mcp add snoofy -- python3 /path/to/snoofy_mcp.py
```

Then call the `setup` tool once (or run `python3 -c "import snoofy; print(snoofy.install_windows())"`)
and log out and back in: GNOME loads new shell extensions only at log-in. `status` shows what's working.

## Tools

| Tool | Does |
|---|---|
| `status`, `setup` | What works here; install window control and make browsers/Electron apps readable |
| `apps`, `launch` | Open apps and their windows; open an app by name |
| `observe` | A window as elements: `[e12] button “Save” (click)`, values, what can be done to each |
| `act` | CLICK, DOUBLE_CLICK, RIGHT_CLICK, TYPE, REPLACE, SET_VALUE, FOCUS, READ, SCROLL_TO on one element |
| `keys` | Chords (`ctrl+s`) and text, sent to a chosen app: its window is lent the keyboard and yours gets it back |
| `windows` | Monitors (which one is the laptop's) and every window's place and size |
| `place_window` | Move to `laptop` / `external` / `left` / a name, `left` half, thirds, corners, `full`, `1280x800`, `60%`, minimize… |

## How it works

- **Reading**: the window's accessibility tree is read straight off the accessibility bus with each level's calls in
  flight at once (GTK 4 has no bulk call): ~20–40 ms for a GTK window. Only what's showing is read. Elements keep their
  ids from one look to the next while they exist.
- **Checked when it acts**: if a window or dialog came or went since the snapshot, or the element changed, nothing
  happens and a fresh snapshot comes back (`changed` / `stale`).
- **Settling**: after acting it counts that app's accessibility events and waits for 0.15 s of quiet (0.6 s if the
  app says nothing, 2 s at most); `reacted: false` means the app announced nothing: the action likely did nothing.
  GTK 4 buttons take ~260 ms to fire when activated this way (their pressed animation), so a button click returns
  in ~450 ms; text and values are immediate.
- **Windows**: Wayland lets no app move another's window, so the extension does it inside the shell: 1–12 ms a
  call; `place_window` waits until the app has really taken the new size (≤ 300 ms) and says where it ended up.
- **Keys**: through GNOME's remote-desktop interface, the only way to type on Wayland.
- **For apps that build on it** (a voice assistant, say): the extension also takes a global shortcut
  (`Hotkey("<Super>z")`, signal `Summon` a few ms after the press) and draws a small status pill by the pointer
  (`Pill({"state": "working", "verb": "Typing in Zen", "turns": [{"you": "…", "reply": "…", "detail": "…"}]})`; its × emits `Dismiss`), on `org.gnome.Shell /studio/stepwise/Snoofy`.

## Develop

```bash
python3 -m pytest test_snoofy.py
```

## Measured (GNOME 50, laptop)

| | |
|---|---|
| Read Calculator (49 elements) | 32–39 ms (libatspi one call at a time: 87 ms) |
| Set 200 characters + settle | ~200 ms |
| Button click + settle + fresh read | ~450 ms (≈260 ms of it is GTK's own button delay) |
| Move/resize a window, confirmed | 20–90 ms (shell call 1–12 ms) |

## License

MIT, © Stepwise Studio. See [LICENSE](LICENSE).
