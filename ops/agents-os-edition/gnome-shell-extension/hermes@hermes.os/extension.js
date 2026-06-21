/**
 * hermes@hermes.os/extension.js
 *
 * Minimal GNOME Shell extension for Hermes.
 *
 * Responsibilities (ONLY these — no agent logic in the compositor):
 *   1. Register a global keybinding (Super+Space) via Main.wm.addKeybinding
 *      backed by the org.hermes.os.overlay GSettings schema.
 *   2. Add a top-bar panel indicator (Hermes icon) that triggers the overlay
 *      on click — same action as the keybind.
 *   3. On trigger: call org.hermes.Runtime1 method OpenOverlay via D-Bus.
 *      If OpenOverlay is unavailable (T017 not yet merged), fall back to
 *      launching `python3 -m hermes.lumen.overlay` via Gio.Subprocess.
 *
 * Constitution §0.2 / DESIGN Decision 2:
 *   The overlay process is separate from gnome-shell. Zero chat/agent
 *   logic lives here. A crash of the overlay does NOT crash the desktop.
 *
 * Keybinding schema: org.hermes.os.overlay
 *   Key: toggle-overlay  Default: ['<Super>space']
 *   GSchema XML: schemas/org.hermes.os.overlay.gschema.xml
 *   Compiled to: /usr/share/glib-2.0/schemas/ at bake time.
 */

import GObject from 'gi://GObject';
import St from 'gi://St';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Clutter from 'gi://Clutter';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import {Extension, gettext as _} from 'resource:///org/gnome/shell/extensions/extension.js';

// ── D-Bus constants (must match dbus_client/runtime1_client.py) ────────────
const DBUS_SERVICE  = 'org.hermes.Runtime';
const DBUS_PATH     = '/org/hermes/Runtime';
const DBUS_IFACE    = 'org.hermes.Runtime1';

// Introspection XML for the OpenOverlay method (subset of contracts/hermes-overlay.dbus.xml).
// The extension only needs OpenOverlay; it does not need the full interface.
const RUNTIME1_XML = `
<node>
  <interface name="org.hermes.Runtime1">
    <method name="OpenOverlay">
      <arg name="trigger"    type="s" direction="in"/>
      <arg name="active_app" type="s" direction="in"/>
      <arg name="invocation_id" type="s" direction="out"/>
    </method>
  </interface>
</node>`;

// ── Indicator panel button ─────────────────────────────────────────────────

const HermesIndicator = GObject.registerClass(
class HermesIndicator extends PanelMenu.Button {
    _init(activateCb) {
        super._init(0.0, _('Hermes'), false);

        // Icon — use a symbolic icon name; fallback to text label if absent.
        // The horneado image should install hermes-symbolic.svg to
        // /usr/share/icons/hicolor/scalable/apps/ so St.Icon finds it.
        this._icon = new St.Icon({
            icon_name: 'hermes-symbolic',
            fallback_icon_name: 'user-available-symbolic',
            style_class: 'system-status-icon',
        });
        this.add_child(this._icon);

        // Accessible label (WCAG 2.1 SC 4.1.2)
        this.accessible_name = _('Hermes overlay');

        // Click → trigger overlay (same as keybind)
        this.connect('button-press-event', (_actor, event) => {
            if (event.get_button() === Clutter.BUTTON_PRIMARY) {
                activateCb('indicator');
                return Clutter.EVENT_STOP;
            }
            return Clutter.EVENT_PROPAGATE;
        });

        // Keyboard: Enter / Space on the indicator
        this.connect('key-press-event', (_actor, event) => {
            const sym = event.get_key_symbol();
            if (sym === Clutter.KEY_Return || sym === Clutter.KEY_space) {
                activateCb('indicator');
                return Clutter.EVENT_STOP;
            }
            return Clutter.EVENT_PROPAGATE;
        });
    }

    setConnected(connected) {
        // Subtle opacity shift: dimmed when daemon is unreachable.
        // Do NOT change icon shape — colour alone cannot convey state (WCAG 1.4.1).
        this._icon.opacity = connected ? 255 : 140;
        this.accessible_description = connected
            ? _('Hermes — en línea')
            : _('Hermes — sin conexión');
    }
});

// ── Extension class ────────────────────────────────────────────────────────

export default class HermesExtension extends Extension {
    enable() {
        this._indicator = null;
        this._keybindingAdded = false;
        this._proxy = null;
        this._proxyWatchId = 0;

        this._settings = this.getSettings('org.hermes.os.overlay');

        this._setupProxy();
        this._addIndicator();
        this._addKeybinding();
    }

    disable() {
        this._removeKeybinding();
        this._removeIndicator();
        this._teardownProxy();
    }

    // ------------------------------------------------------------------
    // D-Bus proxy to org.hermes.Runtime1
    // ------------------------------------------------------------------

    _setupProxy() {
        // Watch for the service appearing/disappearing on the system bus.
        this._proxyWatchId = Gio.bus_watch_name(
            Gio.BusType.SYSTEM,
            DBUS_SERVICE,
            Gio.BusNameWatcherFlags.NONE,
            () => this._onServiceAppeared(),
            () => this._onServiceVanished(),
        );
    }

    _onServiceAppeared() {
        const Runtime1 = Gio.DBusProxy.makeProxyWrapper(RUNTIME1_XML);
        try {
            this._proxy = new Runtime1(
                Gio.DBus.system,
                DBUS_SERVICE,
                DBUS_PATH,
                null,
                Gio.DBusProxyFlags.NONE,
            );
        } catch (e) {
            // Daemon present but introspection failed — fall back to subprocess.
            logError(e, '[hermes] D-Bus proxy creation failed; will use subprocess fallback');
            this._proxy = null;
        }
        if (this._indicator) this._indicator.setConnected(true);
    }

    _onServiceVanished() {
        this._proxy = null;
        if (this._indicator) this._indicator.setConnected(false);
    }

    _teardownProxy() {
        if (this._proxyWatchId) {
            Gio.bus_unwatch_name(this._proxyWatchId);
            this._proxyWatchId = 0;
        }
        this._proxy = null;
    }

    // ------------------------------------------------------------------
    // Panel indicator
    // ------------------------------------------------------------------

    _addIndicator() {
        this._indicator = new HermesIndicator((trigger) => this._triggerOverlay(trigger));
        Main.panel.addToStatusArea('hermes', this._indicator, 0, 'right');
    }

    _removeIndicator() {
        if (this._indicator) {
            this._indicator.destroy();
            this._indicator = null;
        }
    }

    // ------------------------------------------------------------------
    // Global keybinding (Super+Space via GSettings schema)
    // ------------------------------------------------------------------

    _addKeybinding() {
        // Main.wm.addKeybinding registers a Mutters global accelerator.
        // The binding name must match the key in the GSchema.
        try {
            Main.wm.addKeybinding(
                'toggle-overlay',
                this._settings,
                // Shell.ActionMode.ALL → fires in any mode (normal, overview, etc.)
                // Import Shell lazily to avoid top-level import failures on older shells.
                Meta.KeyBindingFlags.NONE,
                Shell.ActionMode.ALL,
                () => this._triggerOverlay('keybind'),
            );
            this._keybindingAdded = true;
        } catch (e) {
            // If Meta/Shell imports fail (version mismatch), log and continue.
            // The indicator click path still works.
            logError(e, '[hermes] Could not register keybinding; indicator still works');
            this._keybindingAdded = false;
        }
    }

    _removeKeybinding() {
        if (this._keybindingAdded) {
            try {
                Main.wm.removeKeybinding('toggle-overlay');
            } catch (_e) { /* ignore */ }
            this._keybindingAdded = false;
        }
    }

    // ------------------------------------------------------------------
    // Trigger the overlay
    // ------------------------------------------------------------------

    /**
     * _triggerOverlay(trigger)
     *
     * Called from keybind handler or indicator click.
     * trigger: 'keybind' | 'indicator'
     *
     * Strategy (in order):
     *   1. D-Bus call org.hermes.Runtime1.OpenOverlay — daemon shows the
     *      already-resident overlay process (T026 keeps it alive).
     *   2. Subprocess fallback: launch `python3 -m hermes.lumen.overlay`.
     *      This covers the case where T017 (OpenOverlay method) is not yet
     *      merged or the daemon is temporarily unreachable.
     *
     * ZERO agent logic here. The overlay process owns everything else.
     */
    _triggerOverlay(trigger) {
        // Attempt D-Bus path first.
        if (this._proxy) {
            try {
                this._proxy.OpenOverlayRemote(
                    trigger,
                    '',   // active_app: best-effort; '' is fine for now
                    (_result, error) => {
                        if (error) {
                            // D-Bus call failed — fall back to subprocess.
                            log(`[hermes] OpenOverlay D-Bus error: ${error.message}; spawning subprocess`);
                            this._spawnOverlay();
                        }
                        // On success the daemon signals the resident overlay to show itself.
                    },
                );
                return;
            } catch (e) {
                logError(e, '[hermes] OpenOverlay call threw; falling back to subprocess');
            }
        }

        // Fallback: spawn the overlay process if not running.
        this._spawnOverlay();
    }

    /**
     * _spawnOverlay()
     *
     * Launch `python3 -m hermes.lumen.overlay` as a user-session subprocess.
     * If a process is already running (T026 resident service), the launch is
     * a no-op because the service unit prevents duplicates (Type=simple,
     * Restart=on-failure with a cooldown, only one instance at a time).
     *
     * We use systemd-run --user to slot the process into the session unit
     * rather than a bare subprocess so it inherits the correct env.
     */
    _spawnOverlay() {
        try {
            // Prefer activating the pre-existing systemd user unit (T026).
            // If the unit is already active this is a no-op (idempotent).
            const argv = [
                'systemctl',
                '--user',
                'start',
                'hermes-overlay.service',
            ];
            const proc = Gio.Subprocess.new(
                argv,
                Gio.SubprocessFlags.NONE,
            );
            // Fire-and-forget. The overlay process manages its own lifecycle.
            proc.wait_async(null, (_p, _res) => { /* ignore exit status */ });
        } catch (e) {
            logError(e, '[hermes] Could not start hermes-overlay.service');
        }
    }
}
