import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

class PsrRefreshController {
    constructor() {
        this._colorSettings = new Gio.Settings({
            schema_id: 'org.gnome.settings-daemon.plugins.color',
        });
        this._timerId = null;
        this._stepCount = 0;
        this._maxSteps = 4;
        this._intervalMs = 300;
        this._toggleState = false;

        this._fullDamageActor = new St.Widget({
            name: 'webcam-als-rgb-psr-refresher',
            reactive: false,
            can_focus: false,
            opacity: 0,
            visible: false,
            x: 0,
            y: 0,
        });
        this._syncStageSize();
        global.stage.add_child(this._fullDamageActor);

        this._stageSizeChangedId = global.stage.connect(
            'notify::size',
            () => this._syncStageSize()
        );
        this._nightLightChangedId = this._colorSettings.connect(
            'changed::night-light-temperature',
            () => this._startTransitionPulse()
        );
    }

    _syncStageSize() {
        if (this._fullDamageActor)
            this._fullDamageActor.set_size(global.stage.width, global.stage.height);
    }

    _triggerFullRefresh() {
        if (!this._fullDamageActor)
            return;
        this._toggleState = !this._toggleState;
        this._fullDamageActor.opacity = this._toggleState ? 1 : 0;
        global.stage.queue_redraw();
    }

    _startTransitionPulse() {
        if (this._timerId !== null) {
            GLib.source_remove(this._timerId);
            this._timerId = null;
        }
        if (!this._fullDamageActor)
            return;

        this._fullDamageActor.show();
        this._stepCount = 0;
        this._triggerFullRefresh();

        this._timerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, this._intervalMs, () => {
            this._stepCount++;
            if (this._stepCount >= this._maxSteps) {
                this._timerId = null;
                if (this._fullDamageActor) {
                    this._fullDamageActor.opacity = 0;
                    this._fullDamageActor.hide();
                }
                return GLib.SOURCE_REMOVE;
            }
            this._triggerFullRefresh();
            return GLib.SOURCE_CONTINUE;
        });
    }

    destroy() {
        if (this._timerId !== null) {
            GLib.source_remove(this._timerId);
            this._timerId = null;
        }
        if (this._nightLightChangedId) {
            this._colorSettings.disconnect(this._nightLightChangedId);
            this._nightLightChangedId = null;
        }
        if (this._stageSizeChangedId) {
            global.stage.disconnect(this._stageSizeChangedId);
            this._stageSizeChangedId = null;
        }
        if (this._fullDamageActor) {
            this._fullDamageActor.hide();
            global.stage.remove_child(this._fullDamageActor);
            this._fullDamageActor.destroy();
            this._fullDamageActor = null;
        }
    }
}

export default class WebcamAlsRgbPsrExtension extends Extension {
    enable() {
        this._controller = new PsrRefreshController();
    }

    disable() {
        if (this._controller) {
            this._controller.destroy();
            this._controller = null;
        }
    }
}
