// Snoofy's part inside GNOME Shell: on Wayland no app may move another app's window, take a global shortcut or draw
// over other apps, so the shell does those for it.
// D-Bus, on the shell's own name: org.gnome.Shell /studio/stepwise/Snoofy studio.stepwise.Snoofy
//   List() -> JSON {windows, monitors}            every normal window and every monitor's work area
//   Place(JSON) -> JSON {window}                  one window: monitor, size/place, state, focus, in one call
//   Hotkey(accel) -> ok                           take a global shortcut ("<Super>z", "" to let it go)
//   signal Summon()                               it was pressed
//   Pill(JSON)                                    the small status pill by the pointer: {state, text, level}
// Geometry is worked out here so a move is one round trip.
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Pango from 'gi://Pango';
import Shell from 'gi://Shell';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const XML = `<node><interface name="studio.stepwise.Snoofy">
  <method name="List"><arg type="s" direction="out" name="json"/></method>
  <method name="Place"><arg type="s" direction="in" name="json"/><arg type="s" direction="out" name="json"/></method>
  <method name="Hotkey"><arg type="s" direction="in" name="accel"/><arg type="b" direction="out" name="ok"/></method>
  <method name="Pill"><arg type="s" direction="in" name="json"/></method>
  <signal name="Summon"/>
</interface></node>`;

// The pill: quiet zinc capsule, one dot that breathes with the voice, a line of text. States set the dot's colour.
const DOT = {listening: '#7dd3fc', thinking: '#a1a1aa', working: '#fbbf24', speaking: '#e4e4e7', done: '#86efac', error: '#fca5a5'};

class Pill {
    constructor() {
        this.box = new St.BoxLayout({
            style: 'background-color: rgba(24,24,27,0.94); border: 1px solid rgba(255,255,255,0.08); border-radius: 999px;' +
                   'padding: 9px 16px 9px 13px; spacing: 10px; box-shadow: 0 8px 24px rgba(0,0,0,0.35);',
            reactive: false, opacity: 0,
        });
        this.dot = new St.Widget({style: 'width: 9px; height: 9px; border-radius: 99px;', y_align: Clutter.ActorAlign.CENTER});
        this.dot.set_pivot_point(0.5, 0.5);
        this.label = new St.Label({style: 'color: #fafafa; font-size: 13px; font-weight: 500; max-width: 420px;',
            y_align: Clutter.ActorAlign.CENTER});
        this.label.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        this.box.add_child(this.dot);
        this.box.add_child(this.label);
        this.box.set_pivot_point(0.5, 0.5);
        Main.uiGroup.add_child(this.box);
        const [x, y] = global.get_pointer();
        const m = Main.layoutManager.monitors.find(r => x >= r.x && x < r.x + r.width && y >= r.y && y < r.y + r.height)
            ?? Main.layoutManager.primaryMonitor;
        this.at = {x, y, m};
        this.box.scale_x = this.box.scale_y = 0.96;
        this.box.ease({opacity: 255, scale_x: 1, scale_y: 1, duration: 160, mode: Clutter.AnimationMode.EASE_OUT_QUAD});
    }

    set(a) {
        if (a.state)
            this.dot.style = `width: 9px; height: 9px; border-radius: 99px; background-color: ${DOT[a.state] ?? DOT.thinking};`;
        if (a.text !== undefined)
            this.label.text = a.text;
        if (a.level !== undefined) {
            const k = 1 + Math.min(1, a.level) * 0.9;
            this.dot.ease({scale_x: k, scale_y: k, duration: 70, mode: Clutter.AnimationMode.EASE_OUT_QUAD});
        }
        const {x, y, m} = this.at;  // below-right of the pointer, kept on its monitor
        const [w, h] = [this.box.width, this.box.height];
        this.box.set_position(Math.min(Math.max(x + 18, m.x + 8), m.x + m.width - w - 8),
            Math.min(Math.max(y + 22, m.y + 8), m.y + m.height - h - 8));
    }

    close() {
        const box = this.box;
        box.ease({opacity: 0, scale_x: 0.96, scale_y: 0.96, duration: 140, mode: Clutter.AnimationMode.EASE_IN_QUAD,
            onComplete: () => box.destroy()});
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
        this._pill ??= new Pill();
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
        this._pill?.box.destroy();
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
