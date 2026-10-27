// Snoofy's part inside GNOME Shell: on Wayland no app may move another app's window, take a global shortcut or draw
// over other apps, so the shell does those for it.
// D-Bus, on the shell's own name: org.gnome.Shell /studio/stepwise/Snoofy studio.stepwise.Snoofy
//   List() -> JSON {windows, monitors}            every normal window and every monitor's work area
//   Place(JSON) -> JSON {window}                  one window: monitor, size/place, state, focus, in one call
//   Hotkey(accel) -> ok                           take a global shortcut ("<Super>z", "" to let it go)
//   signal Summon()                               it was pressed
//   Pill(JSON)                                    the card by the pointer: {state, verb, turns: [{you, reply, detail}], level}
//                                                 (state "hide" closes it; its × emits Summon, like the shortcut)
// Geometry is worked out here so a move is one round trip.
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Pango from 'gi://Pango';
import Shell from 'gi://Shell';
import St from 'gi://St';
import * as Animation from 'resource:///org/gnome/shell/ui/animation.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const XML = `<node><interface name="studio.stepwise.Snoofy">
  <method name="List"><arg type="s" direction="out" name="json"/></method>
  <method name="Place"><arg type="s" direction="in" name="json"/><arg type="s" direction="out" name="json"/></method>
  <method name="Hotkey"><arg type="s" direction="in" name="accel"/><arg type="b" direction="out" name="ok"/></method>
  <method name="Pill"><arg type="s" direction="in" name="json"/></method>
  <signal name="Summon"/>
</interface></node>`;

// The card by the pointer while an agent listens and works: a blue gradient, a line that says what it's doing (a
// spinner while it works, a dot that breathes with the voice while it listens) and the whole conversation of the
// session, wrapped and scrollable, so nothing it says is cut off. × (or the shortcut) stops it.
const INK = '#ffffff';
const DIM = 'rgba(255,255,255,0.62)';
const VERB = {listening: 'Listening', thinking: 'Thinking', speaking: 'Speaking', done: '', error: ''};

function vbox(params) {  // GNOME 48 replaced `vertical` with `orientation`
    const box = new St.BoxLayout(params);
    if ('orientation' in box)
        box.orientation = Clutter.Orientation.VERTICAL;
    else
        box.vertical = true;
    return box;
}

function text(style) {
    const label = new St.Label({style, x_expand: true});
    label.clutter_text.line_wrap = true;
    label.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
    label.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
    return label;
}

class Pill {
    constructor(stop) {
        this.box = vbox({
            style: 'width: 380px; padding: 14px 16px 16px 16px; spacing: 10px; border-radius: 24px;' +
                   'background-gradient-direction: vertical; background-gradient-start: #3f7cff; background-gradient-end: #1f50e3;' +
                   'box-shadow: 0 12px 32px rgba(17,40,120,0.35);',
            reactive: true, opacity: 0,
        });
        const head = new St.BoxLayout({style: 'spacing: 10px;'});
        this.dot = new St.Widget({style: `width: 9px; height: 9px; border-radius: 99px; background-color: ${INK};`,
            y_align: Clutter.ActorAlign.CENTER});
        this.dot.set_pivot_point(0.5, 0.5);
        this.spinner = new Animation.Spinner(16, {animate: true});
        this.spinner.style = `color: ${INK};`;  // a symbolic icon: it takes the text colour (dark by default)
        this.spinner.hide();
        this.verb = new St.Label({style: `color: ${INK}; font-size: 13px; font-weight: 600;`, x_expand: true,
            y_align: Clutter.ActorAlign.CENTER});
        this.verb.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        const close = new St.Button({label: '×', style: `color: ${DIM}; font-size: 18px; padding: 0 2px;`, can_focus: false});
        close.connect('clicked', () => stop());
        for (const a of [this.dot, this.spinner, this.verb, close])
            head.add_child(a);
        this.turns = vbox({style: 'spacing: 12px;'});
        this.scroll = new St.ScrollView({style: 'max-height: 300px;', hscrollbar_policy: St.PolicyType.NEVER,
            vscrollbar_policy: St.PolicyType.AUTOMATIC, overlay_scrollbars: true});
        if (this.scroll.set_child)
            this.scroll.set_child(this.turns);
        else
            this.scroll.add_actor(this.turns);
        this.box.add_child(head);
        this.box.add_child(this.scroll);
        this.box.set_pivot_point(0.5, 0);
        Main.layoutManager.addTopChrome(this.box);  // takes input: × and scrolling a long answer
        const [x, y] = global.get_pointer();
        this.at = {x, y, m: Main.layoutManager.monitors.find(r => x >= r.x && x < r.x + r.width && y >= r.y && y < r.y + r.height)
            ?? Main.layoutManager.primaryMonitor};
        this.shown = '';
        this.box.scale_x = this.box.scale_y = 0.97;
        this.box.ease({opacity: 255, scale_x: 1, scale_y: 1, duration: 180, mode: Clutter.AnimationMode.EASE_OUT_QUAD});
    }

    // {state, verb?, turns?: [{you, reply, detail}], level?}
    set(a) {
        if (a.level !== undefined) {
            const k = 1 + Math.min(1, a.level) * 0.9;
            this.dot.ease({scale_x: k, scale_y: k, duration: 70, mode: Clutter.AnimationMode.EASE_OUT_QUAD});
        }
        if (a.state) {
            const busy = a.state === 'working' || a.state === 'thinking';
            this.spinner.visible = busy;
            this.dot.visible = !busy;
            if (busy)
                this.spinner.play();
            else
                this.spinner.stop();
            this.dot.opacity = a.state === 'listening' ? 255 : 150;
            this.verb.text = a.verb || VERB[a.state] || '';
        }
        if (a.turns && JSON.stringify(a.turns) !== this.shown) {
            this.shown = JSON.stringify(a.turns);
            this.turns.destroy_all_children();
            a.turns.forEach((t, i) => {
                const turn = vbox({style: 'spacing: 4px;', opacity: i === a.turns.length - 1 ? 255 : 140});
                for (const [said, style] of [[t.you, `color: ${DIM}; font-size: 13px;`],
                    [t.reply, `color: ${INK}; font-size: 15px; font-weight: 500;`],
                    [t.detail, 'color: rgba(255,255,255,0.86); font-size: 13px; padding-top: 4px;']]) {
                    if (!said)
                        continue;
                    const label = text(style);
                    label.text = said;
                    turn.add_child(label);
                }
                this.turns.add_child(turn);
            });
            this.scroll.visible = a.turns.length > 0;
            const adj = this.scroll.vadjustment ?? this.scroll.vscroll?.adjustment;
            GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {  // keep the latest words in view
                adj?.set_value(adj.upper);
                return GLib.SOURCE_REMOVE;
            });
        }
        this.place();
    }

    place() {  // below-right of the pointer, kept on its monitor
        const {x, y, m} = this.at;
        const [w, h] = [this.box.width, this.box.height];
        this.box.set_position(Math.min(Math.max(x + 18, m.x + 12), m.x + m.width - w - 12),
            Math.min(Math.max(y + 22, m.y + 12), m.y + m.height - h - 12));
    }

    close() {
        const box = this.box;
        this.spinner.stop();
        box.ease({opacity: 0, scale_x: 0.97, scale_y: 0.97, duration: 150, mode: Clutter.AnimationMode.EASE_IN_QUAD,
            onComplete: () => {
                Main.layoutManager.removeChrome(box);
                box.destroy();
            }});
    }
}

function maximized(w) {
    return typeof w.is_maximized === 'function' ? w.is_maximized() : w.get_maximized() === Meta.MaximizeFlags.BOTH;
}

function call(w, name) {  // GNOME 49 dropped the flags argument of maximize/unmaximize
    try {
        w[name]();
    } catch (e) {
        w[name](Meta.MaximizeFlags.BOTH);
    }
}

function info(w) {
    const r = w.get_frame_rect();
    return {
        id: String(w.get_id()), title: w.get_title() ?? '', app: w.get_wm_class() ?? '', pid: w.get_pid(),
        monitor: w.get_monitor(), x: r.x, y: r.y, w: r.width, h: r.height, maximized: maximized(w),
        fullscreen: w.is_fullscreen(), minimized: w.minimized, focused: w.has_focus(),
        resizable: w.resizeable ?? true,
    };
}

function windows() {
    return global.get_window_actors().map(a => a.meta_window)
        .filter(w => !w.skip_taskbar && [Meta.WindowType.NORMAL, Meta.WindowType.DIALOG].includes(w.get_window_type()));
}

function monitors() {
    const ws = global.workspace_manager.get_active_workspace();
    return Main.layoutManager.monitors.map((m, i) => {
        const a = ws.get_work_area_for_monitor(i);
        return {index: i, x: m.x, y: m.y, w: m.width, h: m.height, primary: i === Main.layoutManager.primaryIndex,
            area: {x: a.x, y: a.y, w: a.width, h: a.height}};
    });
}

const later = new Map();  // window id -> re-placements still to come (a newer Place cancels them)

function cancel(id) {
    for (const src of later.get(id) ?? [])
        GLib.source_remove(src);
    later.delete(id);
}

class Service {
    constructor(summon) {
        this._summon = summon;
        this._grab = 0;
        this._pill = null;
        this._pressed = global.display.connect('accelerator-activated', (_d, action) => {
            if (action === this._grab)
                this._summon();
        });
    }

    Hotkey(accel) {
        this.release();
        if (!accel)
            return true;
        const id = global.display.grab_accelerator(accel, Meta.KeyBindingFlags.NONE);
        if (id === Meta.KeyBindingAction.NONE)
            return false;  // malformed, or another app holds it
        Main.wm.allowKeybinding(Meta.external_binding_name_for_action(id),
            Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW | Shell.ActionMode.POPUP);
        this._grab = id;
        return true;
    }

    Pill(json) {
        const a = JSON.parse(json);
        if (a.state === 'hide') {
            this._pill?.close();
            this._pill = null;
            return;
        }
        this._pill ??= new Pill(() => this._summon());
        this._pill.set(a);
    }

    release() {
        if (this._grab)
            global.display.ungrab_accelerator(this._grab);
        this._grab = 0;
    }

    destroy() {
        this.release();
        global.display.disconnect(this._pressed);
        if (this._pill) {
            Main.layoutManager.removeChrome(this._pill.box);
            this._pill.box.destroy();
        }
    }

    List() {
        return JSON.stringify({windows: windows().map(info), monitors: monitors()});
    }

    // {id, monitor?, frac?: [x, y, w, h] of the work area, px?: [x, y, w, h] inside the work area,
    //  state?: maximize|unmaximize|minimize|restore|fullscreen|unfullscreen, focus?: bool}
    Place(json) {
        const a = JSON.parse(json);
        const w = windows().find(x => String(x.get_id()) === String(a.id));
        if (!w)
            throw new Error(`No window ${a.id}: list them again`);
        cancel(String(a.id));
        if (w.minimized && a.state !== 'minimize')
            w.unminimize();
        if (w.is_fullscreen() && (a.frac || a.px || a.state === 'unfullscreen' || a.state === 'maximize'))
            w.unmake_fullscreen();
        const n = Main.layoutManager.monitors.length;
        const mon = a.monitor ?? w.get_monitor();
        if (mon < 0 || mon >= n)
            throw new Error(`No monitor ${mon}: there are ${n}`);
        const box = a.frac || a.px;
        let target = null;  // the frame it should end up with, for the caller to wait on
        const wasMax = box && maximized(w);
        if (wasMax)
            call(w, 'unmaximize');
        if (mon !== w.get_monitor()) {
            const was = maximized(w);
            if (was)
                call(w, 'unmaximize');
            w.move_to_monitor(mon);
            if (was && !box)
                call(w, 'maximize');
        }
        if (box) {
            const r = global.workspace_manager.get_active_workspace().get_work_area_for_monitor(mon);
            const [x, y, ww, hh] = a.frac
                ? [r.x + a.frac[0] * r.width, r.y + a.frac[1] * r.height, a.frac[2] * r.width, a.frac[3] * r.height]
                : [r.x + a.px[0], r.y + a.px[1], a.px[2], a.px[3]];
            target = [Math.round(x), Math.round(y), Math.round(ww), Math.round(hh)];
            const put = () => w.move_resize_frame(true, Math.round(x), Math.round(y), Math.round(ww), Math.round(hh));
            put();
            if (wasMax) {  // un-maximizing restores the old size after this call: place it again once that's done
                const id = String(a.id);
                later.set(id, [
                    GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => { put(); return GLib.SOURCE_REMOVE; }),
                    GLib.timeout_add(GLib.PRIORITY_DEFAULT, 80, () => { put(); later.delete(id); return GLib.SOURCE_REMOVE; }),
                ]);
            }
        }
        if (a.state === 'maximize' || (maximized(w) && !box && a.monitor !== undefined)) {
            const r = global.workspace_manager.get_active_workspace().get_work_area_for_monitor(mon);
            target = [r.x, r.y, r.width, r.height];
        }
        if (a.state === 'maximize')
            call(w, 'maximize');
        else if (a.state === 'unmaximize')
            call(w, 'unmaximize');
        else if (a.state === 'minimize')
            w.minimize();
        else if (a.state === 'fullscreen')
            w.make_fullscreen();
        else if (a.state === 'unfullscreen')
            w.unmake_fullscreen();
        else if (a.state === 'close')
            w.delete(global.get_current_time());  // as its close button: the app may ask about unsaved work
        if (a.focus)
            Main.activateWindow(w);
        return JSON.stringify({window: info(w), target});
    }
}

export default class Snoofy extends Extension {
    enable() {
        this._service = new Service(() => this._dbus?.emit_signal('Summon', null));
        this._dbus = Gio.DBusExportedObject.wrapJSObject(XML, this._service);
        this._dbus.export(Gio.DBus.session, '/studio/stepwise/Snoofy');
    }

    disable() {
        for (const id of [...later.keys()])
            cancel(id);
        this._service?.destroy();
        this._service = null;
        this._dbus?.unexport();
        this._dbus = null;
    }
}
