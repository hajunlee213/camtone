import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import {Extension, gettext as _} from 'resource:///org/gnome/shell/extensions/extension.js';
import {ExtensionState} from 'resource:///org/gnome/shell/misc/extensionUtils.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {QuickMenuToggle, SystemIndicator} from 'resource:///org/gnome/shell/ui/quickSettings.js';

// Contract with the Python daemon (INT-07) and the systemd unit (INT-06).
const UNIT_NAME = 'webcam-als-rgb.service';
const DAEMON_NAME = 'io.github.hajunlee213.WebcamAlsRgb';
const DAEMON_PATH = '/io/github/hajunlee213/WebcamAlsRgb';
const DAEMON_IFACE = 'io.github.hajunlee213.WebcamAlsRgb';

const SYSTEMD_NAME = 'org.freedesktop.systemd1';
const SYSTEMD_PATH = '/org/freedesktop/systemd1';
const SYSTEMD_MANAGER_IFACE = 'org.freedesktop.systemd1.Manager';
const SYSTEMD_UNIT_IFACE = 'org.freedesktop.systemd1.Unit';

// Extensions that touch the same Night Light / brightness state. Warned about, never disabled.
const CONFLICT_UUIDS = [
    'night-light-slider-updated@vilsbeg.codeberg.org',
    'adaptive-brightness@dmy3k.github.io',
    'adaptivetone@hajun.github.io',
];

// GNOME 45-47 export ExtensionState.ENABLED; GNOME 48+ exports ExtensionState.ACTIVE.
const EXTENSION_ACTIVE_STATE = ExtensionState.ACTIVE ?? ExtensionState.ENABLED;

/**
 * Starts and stops the systemd user unit over the session bus, and follows its ActiveState
 * (e.g. 'failed' after a crash loop). All calls are asynchronous; changes are reported through onChange.
 */
class UnitController {
    constructor(onChange) {
        this._onChange = onChange;
        this._cancellable = new Gio.Cancellable();
        this._unitProxy = null;
        this._unitPropsId = 0;
        this.pending = false;
        this.error = null;
        this.activeState = null;
    }

    start() {
        this._call('StartUnit', true);
    }

    stop() {
        this._call('StopUnit', false);
    }

    // LoadUnit gives the unit's object path; a proxy on that path reports ActiveState changes.
    // Safe to call repeatedly: an unchanged path only re-reads the cached state.
    watch() {
        if (!this._onChange)
            return;
        Gio.DBus.session.call(
            SYSTEMD_NAME, SYSTEMD_PATH, SYSTEMD_MANAGER_IFACE, 'LoadUnit',
            new GLib.Variant('(s)', [UNIT_NAME]), new GLib.VariantType('(o)'),
            Gio.DBusCallFlags.NONE, -1, this._cancellable,
            (connection, res) => {
                let path;
                try {
                    [path] = connection.call_finish(res).deepUnpack();
                } catch (e) {
                    if (!this._cancellable.is_cancelled())
                        console.error(`webcam-als-rgb: LoadUnit ${UNIT_NAME} failed: ${e.message}`);
                    return;
                }
                if (this._cancellable.is_cancelled())
                    return;
                if (this._unitProxy?.get_object_path() === path) {
                    this._readState();
                    return;
                }
                this._dropProxy();
                Gio.DBusProxy.new_for_bus(
                    Gio.BusType.SESSION, Gio.DBusProxyFlags.DO_NOT_AUTO_START, null,
                    SYSTEMD_NAME, path, SYSTEMD_UNIT_IFACE, this._cancellable,
                    (source, proxyRes) => {
                        let proxy;
                        try {
                            proxy = Gio.DBusProxy.new_for_bus_finish(proxyRes);
                        } catch (e) {
                            if (!this._cancellable.is_cancelled())
                                console.error(`webcam-als-rgb: cannot watch ${UNIT_NAME}: ${e.message}`);
                            return;
                        }
                        this._unitProxy = proxy;
                        this._unitPropsId = proxy.connect('g-properties-changed', () => this._readState());
                        this._readState();
                    });
            });
    }

    _readState() {
        const state = this._unitProxy?.get_cached_property('ActiveState')?.deepUnpack() ?? null;
        if (state === this.activeState)
            return;
        this.activeState = state;
        this._notify();
    }

    _dropProxy() {
        if (this._unitProxy) {
            if (this._unitPropsId)
                this._unitProxy.disconnect(this._unitPropsId);
            this._unitPropsId = 0;
            this._unitProxy = null;
        }
        this.activeState = null;
    }

    detach() {
        this._cancellable.cancel();
        this._dropProxy();
        this._onChange = null;
    }

    _call(method, isStart) {
        if (isStart) {
            this.pending = true;
            this._notify();
        }
        Gio.DBus.session.call(
            SYSTEMD_NAME, SYSTEMD_PATH, SYSTEMD_MANAGER_IFACE, method,
            new GLib.Variant('(ss)', [UNIT_NAME, 'replace']),
            null, Gio.DBusCallFlags.NONE, -1, null,
            (connection, res) => {
                let error = null;
                try {
                    connection.call_finish(res);
                } catch (e) {
                    error = e;
                    console.error(`webcam-als-rgb: ${method} ${UNIT_NAME} failed: ${e.message}`);
                }
                if (isStart) {
                    this.pending = false;
                    this.error = error;
                }
                this._notify();
                this.watch();
            });
    }

    _notify() {
        if (this._onChange)
            this._onChange();
    }
}

/**
 * PSR (Panel Self Refresh) Global Color Transition Refresh Controller
 *
 * Background:
 * On laptops with Intel PSR2 (Selective Fetch) and static screens,
 * changing Night Light temperature only updates the hardware CTM/LUT without
 * generating compositor damage. Consequently, PSR2 only selectively updates
 * small moving areas (e.g. blinking text cursor), causing partial screen yellowing.
 *
 * Mechanism (4-step 1.2s Transition Pulse):
 * GNOME's Night Light smoothly interpolates hardware LUT over ~1.2s.
 * When night-light-temperature changes, this controller pulses 4 times
 * at 300ms intervals (t = 300ms, 600ms, 900ms, 1200ms).
 * Each pulse toggles an invisible full-screen St.Widget (opacity 0 <-> 1/255)
 * and calls global.stage.queue_redraw(). This forces Mutter KMS to emit full-frame
 * damage clips, smoothly applying the color temperature across the entire panel.
 *
 * Zero-Wakeup & Stability Guarantee:
 * - D-Bus traffic: 0 (Pure in-process Clutter / Mutter calls)
 * - Timer self-destructs after 4 ticks (GLib.SOURCE_REMOVE). Zero steady-state wakeups.
 */
class PsrRefreshController {
    constructor() {
        this._colorSettings = new Gio.Settings({ schema_id: 'org.gnome.settings-daemon.plugins.color' });
        this._timerId = null;
        this._stepCount = 0;
        this._maxSteps = 4;
        this._intervalMs = 300;

        // Create invisible full-screen actor to guarantee Mutter KMS Full Damage
        this._fullDamageActor = new St.Widget({
            name: 'webcam-als-rgb-psr-refresher',
            reactive: false,
            opacity: 0,
            visible: false,
            x: 0,
            y: 0,
        });
        this._updateActorSize();
        global.stage.add_child(this._fullDamageActor);

        // Keep actor size in sync with stage in case of resolution / scaling changes
        this._sizeChangedId = global.stage.connect('notify::size', () => {
            this._updateActorSize();
        });

        // Listen for color temperature shifts
        this._colorChangedId = this._colorSettings.connect('changed::night-light-temperature', () => {
            this._startTransitionPulse();
        });
    }

    _updateActorSize() {
        if (this._fullDamageActor && global.stage) {
            this._fullDamageActor.set_size(global.stage.width, global.stage.height);
        }
    }

    _startTransitionPulse() {
        // Reset any existing pulse (debounce)
        if (this._timerId) {
            GLib.source_remove(this._timerId);
            this._timerId = null;
        }

        this._fullDamageActor.show();
        this._stepCount = 0;
        // Trigger immediate 1st frame redraw
        this._triggerFullRefresh();

        this._timerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, this._intervalMs, () => {
            this._stepCount++;
            this._triggerFullRefresh();

            if (this._stepCount >= this._maxSteps) {
                this._timerId = null;
                if (this._fullDamageActor) {
                    this._fullDamageActor.opacity = 0;
                    this._fullDamageActor.hide();
                }
                return GLib.SOURCE_REMOVE;
            }
            return GLib.SOURCE_CONTINUE;
        });
    }

    _triggerFullRefresh() {
        if (!this._fullDamageActor) return;
        // Micro-toggle opacity between 0 and 1 (1/255 is imperceptible to human eye)
        // to force Mutter DRM to emit full-screen damage clips.
        this._fullDamageActor.opacity = (this._fullDamageActor.opacity === 0) ? 1 : 0;
        global.stage.queue_redraw();
    }

    destroy() {
        if (this._timerId) {
            GLib.source_remove(this._timerId);
            this._timerId = null;
        }
        if (this._colorChangedId) {
            this._colorSettings.disconnect(this._colorChangedId);
            this._colorChangedId = null;
        }
        if (this._sizeChangedId) {
            global.stage.disconnect(this._sizeChangedId);
            this._sizeChangedId = null;
        }
        if (this._fullDamageActor) {
            global.stage.remove_child(this._fullDamageActor);
            this._fullDamageActor.destroy();
            this._fullDamageActor = null;
        }
        this._colorSettings = null;
    }
}

const WebcamAlsRgbToggle = GObject.registerClass(
class WebcamAlsRgbToggle extends QuickMenuToggle {
    _init(extension, settings, unit) {
        super._init({
            title: _('Webcam ALS RGB'),
            iconName: 'night-light-symbolic',
            toggleMode: true,
        });

        this._extension = extension;
        this._settings = settings;
        this._unit = unit;
        this._destroyed = false;
        this._proxy = null;
        this._proxyPropsId = 0;
        this._proxyOwnerId = 0;

        // Quick Settings toggle is the 'enabled' key; the extension starts/stops the unit on change.
        this._settings.bind('enabled', this, 'checked', Gio.SettingsBindFlags.DEFAULT);

        this.menu.setHeader('night-light-symbolic', _('Webcam ALS RGB'));

        // Warning line for conflicting extensions (hidden unless one is enabled)
        this._warnItem = new PopupMenu.PopupMenuItem(_('충돌 가능'), {reactive: false, can_focus: false});
        this._warnItem.label.add_style_class_name('webcam-als-rgb-warning');
        this.menu.addMenuItem(this._warnItem);

        // Automatic adjustment switches (keys are read from the same GSettings schema as the daemon)
        this._autoBrightnessItem = new PopupMenu.PopupSwitchMenuItem(_('자동 밝기'), false);
        this._settings.bind('auto-brightness', this._autoBrightnessItem, 'state', Gio.SettingsBindFlags.DEFAULT);
        this.menu.addMenuItem(this._autoBrightnessItem);

        this._autoCctItem = new PopupMenu.PopupSwitchMenuItem(_('자동 색온도'), false);
        this._settings.bind('auto-color-temp', this._autoCctItem, 'state', Gio.SettingsBindFlags.DEFAULT);
        this.menu.addMenuItem(this._autoCctItem);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // Last measurement (read-only)
        this._infoItem = new PopupMenu.PopupMenuItem('', {reactive: false, can_focus: false});
        this._infoItem.label.add_style_class_name('webcam-als-rgb-info');
        this.menu.addMenuItem(this._infoItem);

        this._measureItem = new PopupMenu.PopupMenuItem(_('지금 측정'));
        this._measureItem.connect('activate', () => this._measureNow());
        this.menu.addMenuItem(this._measureItem);

        // Preferences only exists from v0.1.7 on; hide the entry when prefs.js is absent.
        this._prefsItem = new PopupMenu.PopupMenuItem(_('설정…'));
        this._prefsItem.connect('activate', () => {
            this._extension.openPreferences();
            Main.panel.closeQuickSettings();
        });
        this._prefsItem.visible = this._hasPrefs();
        this.menu.addMenuItem(this._prefsItem);

        this._settingsChangedId = this._settings.connect('changed', () => this._refresh());
        this._menuOpenId = this.menu.connect('open-state-changed', (menu, open) => {
            if (open) {
                this._refreshConflicts();
                this._refresh();
            }
        });

        this._createProxy();
        this._refreshConflicts();
        this._refresh();
    }

    _hasPrefs() {
        const file = Gio.File.new_for_path(GLib.build_filenamev([this._extension.path, 'prefs.js']));
        return file.query_exists(null);
    }

    _refreshConflicts() {
        const names = CONFLICT_UUIDS.filter(uuid => {
            const ext = Main.extensionManager?.lookup(uuid);
            return ext?.state === EXTENSION_ACTIVE_STATE;
        });
        this._warnItem.visible = names.length > 0;
        if (names.length > 0)
            this._warnItem.label.text = `${_('충돌 가능한 확장이 켜져 있음')}: ${names.join(', ')}`;
    }

    // The daemon may be absent (not installed or not running). The proxy is created with
    // DO_NOT_AUTO_START, so a missing daemon only means no name owner, never an error here.
    _createProxy() {
        Gio.DBusProxy.new_for_bus(
            Gio.BusType.SESSION,
            Gio.DBusProxyFlags.DO_NOT_AUTO_START,
            null,
            DAEMON_NAME,
            DAEMON_PATH,
            DAEMON_IFACE,
            null,
            (source, res) => {
                if (this._destroyed)
                    return;
                try {
                    this._proxy = Gio.DBusProxy.new_for_bus_finish(res);
                } catch (e) {
                    console.error(`webcam-als-rgb: cannot create daemon proxy: ${e.message}`);
                    return;
                }
                this._proxyPropsId = this._proxy.connect('g-properties-changed', () => this._refresh());
                this._proxyOwnerId = this._proxy.connect('notify::g-name-owner', () => {
                    if (this._proxy?.get_name_owner())
                        this._unit.pending = false;
                    this._refresh();
                });
                this._refresh();
            });
    }

    _prop(name) {
        return this._proxy?.get_cached_property(name)?.deepUnpack() ?? null;
    }

    _measureNow() {
        if (!this._proxy?.get_name_owner())
            return;
        this._proxy.call('MeasureNow', null, Gio.DBusCallFlags.NONE, -1, null, (proxy, res) => {
            try {
                proxy.call_finish(res);
            } catch (e) {
                console.error(`webcam-als-rgb: MeasureNow failed: ${e.message}`);
            }
        });
    }

    _runningSubtitle(state) {
        switch (state) {
        case 'running': {
            const parts = [];
            if (this._settings.get_boolean('auto-brightness'))
                parts.push(`${_('밝기')} ${this._prop('TargetBrightness') ?? '--'}%`);
            if (this._settings.get_boolean('auto-color-temp'))
                parts.push(`${this._prop('TargetCct') ?? '----'}K`);
            return parts.length > 0 ? parts.join(' · ') : _('측정 중');
        }
        case 'paused-locked':
            return _('잠김');
        case 'paused-sleep':
            return _('절전 중');
        case 'paused-battery':
            return _('배터리 절약');
        case 'paused-display-off':
            return _('화면 꺼짐');
        case 'camera-busy':
            return _('카메라 사용 중');
        default:
            return _('대기 중');
        }
    }

    _infoText(owner) {
        const ev = this._prop('EvLog');
        const cct = this._prop('AmbientCct');
        const last = this._prop('LastMeasured');
        if (!owner || ev === null || cct === null || !last)
            return `${_('주변')}: ${_('측정 전')}`;

        const ago = Math.max(0, Math.round(Date.now() / 1000 - last));
        const agoText = ago < 60 ? `${ago}${_('초 전')}` : `${Math.floor(ago / 60)}${_('분 전')}`;
        return `${_('주변')}: ev ${ev.toFixed(1)} · ${Math.round(cct)}K · ${agoText}`;
    }

    _refresh() {
        if (this._destroyed)
            return;

        const enabled = this._settings.get_boolean('enabled');
        const owner = this._proxy?.get_name_owner() ?? null;

        let subtitle;
        if (!enabled)
            subtitle = _('꺼짐');
        else if (this._unit.error)
            subtitle = _('설치 오류');
        else if (!owner) {
            if (this._unit.activeState === 'failed')
                subtitle = _('오류');
            else
                subtitle = this._unit.pending ? _('시작 중') : _('대기 중');
        }
        else
            subtitle = this._runningSubtitle(this._prop('State'));

        this.subtitle = subtitle;
        this.iconName = enabled ? 'night-light-symbolic' : 'night-light-disabled-symbolic';
        this._infoItem.label.text = this._infoText(enabled && owner);
        this._measureItem.setSensitive(enabled && !!owner);
    }

    // Called by the indicator when the unit state changes.
    syncState() {
        this._refresh();
    }

    destroy() {
        this._destroyed = true;

        if (this._menuOpenId) {
            this.menu.disconnect(this._menuOpenId);
            this._menuOpenId = 0;
        }
        if (this._settingsChangedId) {
            this._settings.disconnect(this._settingsChangedId);
            this._settingsChangedId = 0;
        }
        this._settings.unbind(this, 'checked');
        this._settings.unbind(this._autoBrightnessItem, 'state');
        this._settings.unbind(this._autoCctItem, 'state');

        if (this._proxy) {
            if (this._proxyPropsId)
                this._proxy.disconnect(this._proxyPropsId);
            if (this._proxyOwnerId)
                this._proxy.disconnect(this._proxyOwnerId);
            this._proxyPropsId = 0;
            this._proxyOwnerId = 0;
            this._proxy = null;
        }

        super.destroy();
    }
});

const WebcamAlsRgbIndicator = GObject.registerClass(
class WebcamAlsRgbIndicator extends SystemIndicator {
    _init(extension, settings, unit) {
        super._init();

        this._settings = settings;

        // Optional top bar icon (show-indicator), shown only while the service is enabled.
        this._indicator = this._addIndicator();
        this._indicator.icon_name = 'night-light-symbolic';

        this._toggle = new WebcamAlsRgbToggle(extension, settings, unit);
        this.quickSettingsItems.push(this._toggle);

        this._indicatorChangedIds = [
            settings.connect('changed::show-indicator', () => this._syncIndicator()),
            settings.connect('changed::enabled', () => this._syncIndicator()),
        ];
        this._syncIndicator();
    }

    _syncIndicator() {
        this._indicator.visible =
            this._settings.get_boolean('show-indicator') && this._settings.get_boolean('enabled');
    }

    syncState() {
        this._toggle?.syncState();
    }

    destroy() {
        for (const id of this._indicatorChangedIds ?? [])
            this._settings.disconnect(id);
        this._indicatorChangedIds = [];
        if (this._toggle) {
            this._toggle.destroy();
            this._toggle = null;
        }
        super.destroy();
    }
});

export default class WebcamAlsRgbExtension extends Extension {
    enable() {
        this.initTranslations();

        this._settings = this.getSettings();
        this._unit = new UnitController(() => this._indicator?.syncState());
        this._indicator = new WebcamAlsRgbIndicator(this, this._settings, this._unit);
        Main.panel.statusArea.quickSettings.addExternalIndicator(this._indicator);
        this._unit.watch();

        this._enabledChangedId = this._settings.connect('changed::enabled', () => this._syncUnit());
        this._psrChangedId = this._settings.connect('changed::psr-refresh', () => this._syncPsr());

        this._syncPsr();
        this._syncUnit();
    }

    disable() {
        // GNOME disables extensions on lock. Stopping the daemon then would restore Night Light
        // and re-apply it after unlock, so the daemon handles the lock itself (ScreenSaver signal).
        const locked = Main.sessionMode.isLocked || Main.sessionMode.currentMode === 'unlock-dialog';

        if (this._enabledChangedId) {
            this._settings.disconnect(this._enabledChangedId);
            this._enabledChangedId = 0;
        }
        if (this._psrChangedId) {
            this._settings.disconnect(this._psrChangedId);
            this._psrChangedId = 0;
        }

        if (this._psrRefresher) {
            this._psrRefresher.destroy();
            this._psrRefresher = null;
        }

        if (this._indicator) {
            this._indicator.destroy();
            this._indicator = null;
        }

        if (this._unit) {
            if (!locked)
                this._unit.stop();
            this._unit.detach();
            this._unit = null;
        }

        this._settings = null;
    }

    // enabled = true → StartUnit, false → StopUnit. Both are idempotent in systemd.
    _syncUnit() {
        if (!this._unit || !this._settings)
            return;
        if (this._settings.get_boolean('enabled'))
            this._unit.start();
        else
            this._unit.stop();
    }

    _syncPsr() {
        if (!this._settings)
            return;
        const want = this._settings.get_boolean('psr-refresh');
        if (want && !this._psrRefresher)
            this._psrRefresher = new PsrRefreshController();
        else if (!want && this._psrRefresher) {
            this._psrRefresher.destroy();
            this._psrRefresher = null;
        }
    }
}
