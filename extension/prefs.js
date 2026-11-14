// Preferences window (v0.1.7). Modeled on gnome-adaptive-tone's prefs.js: Adw pages and groups,
// one SpinRow/SwitchRow per GSettings key, and a per-row reset button.
//
// Every value is bound to the existing schema keys (extension/schemas/...). Ranges come from the
// schema itself. The calibration helpers talk to the daemon over D-Bus (io.github.hajunlee213.CamTone).
import Adw from 'gi://Adw';
import Gdk from 'gi://Gdk';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Gtk from 'gi://Gtk';
import {ExtensionPreferences, gettext as _} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

// Minimal %s substitution so translators can reorder the whole sentence (no string concatenation).
const sprintf = (fmt, ...args) => {
    let i = 0;
    return fmt.replace(/%s/g, () => String(args[i++]));
};

const DAEMON_NAME = 'io.github.hajunlee213.CamTone';
const DAEMON_PATH = '/io/github/hajunlee213/CamTone';
const DAEMON_IFACE = 'io.github.hajunlee213.CamTone';
const JOURNAL_CMD = 'journalctl --user -u camtone -f';
const REPO_URL = 'https://github.com/hajunlee213/camtone';

// ---- daemon D-Bus helpers ------------------------------------------------

// Calls a method or Properties.* on the daemon. Never auto-starts the daemon.
function daemonCall(iface, member, params, replyType) {
    return new Promise((resolve, reject) => {
        Gio.DBus.session.call(
            DAEMON_NAME, DAEMON_PATH, iface, member, params, replyType,
            Gio.DBusCallFlags.NO_AUTO_START, -1, null,
            (connection, res) => {
                try {
                    resolve(connection.call_finish(res));
                } catch (e) {
                    reject(e);
                }
            });
    });
}

// All daemon properties, or null when the daemon is not running.
async function readDaemonProps() {
    try {
        const reply = await daemonCall(
            'org.freedesktop.DBus.Properties', 'GetAll',
            new GLib.Variant('(s)', [DAEMON_IFACE]), new GLib.VariantType('(a{sv})'));
        // deepUnpack() leaves each a{sv} value as a GLib.Variant, so unwrap them one by one.
        const [raw] = reply.deepUnpack();
        const props = {};
        for (const name of Object.keys(raw))
            props[name] = raw[name]?.deepUnpack ? raw[name].deepUnpack() : raw[name];
        return props;
    } catch (_e) {
        return null;
    }
}

// Runs MeasureNow and returns the properties after it. Throws when the daemon is absent,
// paused, or the measurement did not complete (LastMeasured did not move).
async function measureNow() {
    const before = await readDaemonProps();
    if (!before)
        throw new Error(_('The daemon is not running. Turn it on in Quick Settings.'));
    await daemonCall(DAEMON_IFACE, 'MeasureNow', null, null);
    const after = await readDaemonProps();
    if (!after || Number(after.LastMeasured) === Number(before.LastMeasured))
        throw new Error(_('The measurement was not updated. Check whether measuring is paused.'));
    return after;
}

function errorText(e) {
    return String(e?.message ?? e).replace(/^GDBus\.Error:[^:]*:\s*/, '');
}

function stateText(state) {
    switch (state) {
    case 'running':
        return _('Running');
    case 'paused-locked':
        return _('Locked');
    case 'paused-sleep':
        return _('Suspended');
    case 'paused-battery':
        return _('Battery saving');
    case 'paused-display-off':
        return _('Display off');
    case 'camera-busy':
        return _('Camera in use');
    case 'starting':
        return _('Starting');
    default:
        return String(state);
    }
}

// BrightnessBackend values from the daemon. Unknown or missing values give null (nothing shown).
function backendText(backend) {
    switch (backend) {
    case 'shell':
        return 'GNOME';
    case 'logind':
        return _('Direct control (logind)');
    case 'none':
        return _('None');
    default:
        return null;
    }
}

// Adw/Gtk constructor props: leave subtitle out entirely when there is none.
const withSubtitle = (props, subtitle) => (subtitle ? {...props, subtitle} : props);

// Rough brightness % for an ev reading, using the same linear map as the daemon (offset ignored).
// Returns null when the reference pair is not usable.
export function estimateBrightnessPct(ev, evMin, evMax, minPct, maxPct) {
    if (!Number.isFinite(ev) || !(evMax > evMin))
        return null;
    const t = Math.min(1, Math.max(0, (ev - evMin) / (evMax - evMin)));
    return Math.round(minPct + t * (maxPct - minPct));
}

// Confirmation dialog. Adw.AlertDialog needs libadwaita 1.5; older systems get Adw.MessageDialog.
// `kind` ('alert' | 'message') is only overridden by the harness; production leaves it out.
export function confirmDialog({window, heading, body, cancelLabel, confirmLabel, onConfirm, kind = null}) {
    const useAlert = (kind ?? (typeof Adw.AlertDialog === 'function' ? 'alert' : 'message')) === 'alert';
    const dialog = useAlert
        ? new Adw.AlertDialog({heading, body})
        : new Adw.MessageDialog({transient_for: window, modal: true, heading, body});
    dialog.add_response('cancel', cancelLabel);
    dialog.add_response('apply', confirmLabel);
    dialog.set_response_appearance('apply', Adw.ResponseAppearance.SUGGESTED);
    dialog.set_default_response('cancel');
    dialog.set_close_response('cancel');
    dialog.connect('response', (_d, response) => {
        if (response === 'apply')
            onConfirm();
    });
    if (useAlert)
        dialog.present(window);
    else
        dialog.present();
    return dialog;
}

// Toast that stays for 10 s with a 되돌리기 (undo) button.
export function undoToast(title, onUndo) {
    const toast = new Adw.Toast({title, timeout: 10, button_label: _('Undo')});
    toast.connect('button-clicked', () => onUndo());
    return toast;
}

export default class CamTonePreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        this.initTranslations();

        window.set_default_size(680, 760);
        window.set_search_enabled(true);

        const settings = this.getSettings();
        const toast = text => window.add_toast(new Adw.Toast({title: text, timeout: 3}));

        // Min/max come from the schema <range>, never from hard-coded numbers here.
        const rangeOf = key => {
            const pair = settings.settings_schema.get_key(key).get_range().deepUnpack();
            const bounds = pair[1]?.deepUnpack ? pair[1].deepUnpack() : pair[1];
            return [Number(bounds[0]), Number(bounds[1])];
        };

        // Reset to the schema default; the button is disabled while the value is already the default.
        const resetButton = key => {
            const btn = new Gtk.Button({
                icon_name: 'edit-undo-symbolic',
                tooltip_text: _('Reset to default'),
                valign: Gtk.Align.CENTER,
                css_classes: ['flat', 'circular'],
            });
            const update = () => {
                const isDefault = settings.get_value(key).equal(settings.get_default_value(key));
                btn.set_sensitive(!isDefault);
            };
            btn.connect('clicked', () => settings.reset(key));
            settings.connect(`changed::${key}`, update);
            update();
            return btn;
        };

        const switchRow = (key, title, subtitle) => {
            const row = new Adw.SwitchRow(withSubtitle({title}, subtitle));
            settings.bind(key, row, 'active', Gio.SettingsBindFlags.DEFAULT);
            row.add_suffix(resetButton(key));
            return row;
        };

        const spinRow = (key, {title, subtitle, step = 1, page = 10, digits = 0}) => {
            const [lower, upper] = rangeOf(key);
            const row = new Adw.SpinRow(withSubtitle({
                title,
                digits,
                climb_rate: 1,
                adjustment: new Gtk.Adjustment({lower, upper, step_increment: step, page_increment: page}),
            }, subtitle));
            settings.bind(key, row, 'value', Gio.SettingsBindFlags.DEFAULT);
            row.add_suffix(resetButton(key));
            return row;
        };

        const addPage = (title, iconName) => {
            const page = new Adw.PreferencesPage({title, icon_name: iconName});
            window.add(page);
            return page;
        };

        const addGroup = (page, title, description) => {
            // PreferencesGroup has 'description', not 'subtitle'.
            const group = new Adw.PreferencesGroup(description ? {title, description} : {title});
            page.add(group);
            return group;
        };

        // A plain row with one button on the right.
        const buttonRow = (title, subtitle, label, onClick, cssClass = null) => {
            const row = new Adw.ActionRow(withSubtitle({title}, subtitle));
            const btn = new Gtk.Button({label, valign: Gtk.Align.CENTER});
            if (cssClass)
                btn.add_css_class(cssClass);
            btn.connect('clicked', onClick);
            row.add_suffix(btn);
            return {row, btn};
        };

        // =============================================================
        // 밝기 (brightness)
        // =============================================================
        const brightnessPage = addPage(_('Brightness'), 'display-brightness-symbolic');

        const serviceGroup = addGroup(brightnessPage, _('Service'),
            _('Same setting as the Quick Settings toggle. Turning it off stops measuring and adjusting, and restores Night Light.'));
        serviceGroup.add(switchRow('enabled', _('Enable service')));

        const brightnessGroup = addGroup(brightnessPage, _('Auto brightness'));
        brightnessGroup.add(switchRow('auto-brightness', _('Auto brightness'),
            _('Adjusts screen brightness to the ambient light')));
        brightnessGroup.add(spinRow('brightness-min-pct', {
            title: _('Minimum brightness (%)'), subtitle: _('Brightness used in dark surroundings'), step: 1, page: 5,
        }));
        brightnessGroup.add(spinRow('brightness-max-pct', {
            title: _('Maximum brightness (%)'), subtitle: _('Brightness used in bright surroundings'), step: 1, page: 5,
        }));
        brightnessGroup.add(spinRow('brightness-deadband-pct', {
            title: _('Brightness deadband (%)'), subtitle: _('Brightness is not changed unless it differs by at least this much'), step: 1, page: 5,
        }));

        // Learned offset: written by the daemon when the user adjusts brightness by hand.
        const learnedGroup = addGroup(brightnessPage, _('Learned brightness offset'),
            _('When you adjust brightness manually, the daemon learns the difference and adds it to this value.'));
        const offsetRow = new Adw.ActionRow({title: _('Learned offset')});
        const offsetReset = new Gtk.Button({label: _('Reset'), valign: Gtk.Align.CENTER});
        offsetReset.connect('clicked', () => settings.set_int('brightness-offset-pct', 0));
        offsetRow.add_suffix(offsetReset);
        learnedGroup.add(offsetRow);
        const updateOffset = () => {
            const v = settings.get_int('brightness-offset-pct');
            offsetRow.set_subtitle(`${v > 0 ? '+' : ''}${v} %`);
            offsetReset.set_sensitive(v !== 0);
        };
        settings.connect('changed::brightness-offset-pct', updateOffset);
        updateOffset();

        // Under the GNOME shell backend the slider does the fine adjustment, so the learned offset is hidden.
        const shellGroup = addGroup(brightnessPage, _('Brightness fine-tuning'),
            _('Fine-tune brightness with the GNOME brightness slider. The slider midpoint (50%) means no adjustment.'));
        shellGroup.set_visible(false);
        // Until the first daemon poll, the offset group stays visible (the logind behaviour).
        const applyBackend = props => {
            const isShell = props?.BrightnessBackend === 'shell';
            learnedGroup.set_visible(!isShell);
            shellGroup.set_visible(isShell);
        };

        // =============================================================
        // 색온도 (color temperature)
        // =============================================================
        const cctPage = addPage(_('Color temperature'), 'night-light-symbolic');

        const cctGroup = addGroup(cctPage, _('Auto color temperature'));
        cctGroup.add(switchRow('auto-color-temp', _('Auto color temperature'),
            _('Adjusts the Night Light temperature to the ambient color')));

        const boundsGroup = addGroup(cctPage, _('Display temperature range'),
            _('The ambient temperature range maps linearly onto the display range. Calibration is done on the Calibration page.'));
        boundsGroup.add(spinRow('display-cct-min', {
            title: _('Minimum temperature (K, warmest)'), step: 50, page: 200,
        }));
        boundsGroup.add(spinRow('display-cct-max', {
            title: _('Maximum temperature (K, coolest)'), step: 50, page: 200,
        }));
        boundsGroup.add(spinRow('cct-offset', {
            title: _('Temperature offset (K)'), subtitle: _('+ cooler, - warmer'), step: 25, page: 100,
        }));
        boundsGroup.add(spinRow('cct-deadband-k', {
            title: _('Temperature deadband (K)'), subtitle: _('Temperature is not changed unless it differs by at least this much'), step: 10, page: 50,
        }));

        const nightGroup = addGroup(cctPage, _('Fixed night schedule'),
            _('During the set hours the color temperature is fixed. Brightness is still adjusted automatically from the camera.'));
        nightGroup.add(switchRow('night-schedule-enabled', _('Use night schedule')));
        nightGroup.add(spinRow('night-schedule-temp', {title: _('Fixed temperature (K)'), step: 100, page: 500}));
        nightGroup.add(spinRow('night-schedule-from-hour', {title: _('Start hour')}));
        nightGroup.add(spinRow('night-schedule-from-minute', {title: _('Start minute'), step: 5, page: 15}));
        nightGroup.add(spinRow('night-schedule-to-hour', {title: _('End hour')}));
        nightGroup.add(spinRow('night-schedule-to-minute', {title: _('End minute'), step: 5, page: 15}));

        const psrGroup = addGroup(cctPage, _('Screen refresh'));
        psrGroup.add(switchRow('psr-refresh', _('PSR2 full refresh'),
            _('Forces a full redraw so temperature changes show on the whole screen at once on Intel PSR2 panels')));

        // =============================================================
        // 측정·전원 (measurement and power)
        // =============================================================
        const measurePage = addPage(_('Measuring & Power'), 'preferences-system-time-symbolic');

        const intervalGroup = addGroup(measurePage, _('Measurement interval'),
            _('When nothing changes, the interval grows automatically up to the maximum.'));
        intervalGroup.add(spinRow('interval-ac-sec', {
            title: _('Interval on AC power (s)'), step: 5, page: 30,
        }));
        intervalGroup.add(spinRow('interval-battery-sec', {
            title: _('Interval on battery (s)'), step: 10, page: 60,
        }));
        intervalGroup.add(spinRow('interval-max-sec', {
            title: _('Maximum adaptive interval (s)'), step: 30, page: 120,
        }));

        const powerGroup = addGroup(measurePage, _('Power'));
        powerGroup.add(switchRow('pause-on-battery', _('Pause measuring on battery'),
            _('When on, the camera is not used while running on battery')));

        const smoothGroup = addGroup(measurePage, _('Measurement smoothing'));
        smoothGroup.add(spinRow('ema-alpha', {
            title: _('Smoothing factor (EMA α)'),
            subtitle: _('Higher reacts faster, lower is smoother'),
            digits: 2, step: 0.05, page: 0.1,
        }));

        const indicatorGroup = addGroup(measurePage, _('Top bar'));
        indicatorGroup.add(switchRow('show-indicator', _('Show top bar icon'),
            _('Shows an icon in the top bar while the service is on')));

        // =============================================================
        // 보정 (calibration)
        // =============================================================
        const calPage = addPage(_('Calibration'), 'applications-science-symbolic');

        const evGroup = addGroup(calPage, _('Brightness references (ev)'),
            _('Two reference points that turn the ambient light reading into a brightness %. Press each button in dark and in bright surroundings to store the current reading.'));
        evGroup.add(spinRow('ev-log-min', {
            title: _('Dark reference ev'), subtitle: _('At or below this value, minimum brightness is used'), digits: 1, step: 0.1, page: 0.5,
        }));
        evGroup.add(spinRow('ev-log-max', {
            title: _('Bright reference ev'), subtitle: _('At or above this value, maximum brightness is used'), digits: 1, step: 0.1, page: 0.5,
        }));

        // Measures, checks, then asks before writing. Refuses a reference that would cross the other one
        // (min must stay below max). Nothing is written until the user confirms in the dialog.
        const setEvReference = async key => {
            try {
                const props = await measureNow();
                const ev = Number(props.EvLog);
                if (!Number.isFinite(ev))
                    throw new Error(_('Could not read the measurement.'));
                const lo = settings.get_double('ev-log-min');
                const hi = settings.get_double('ev-log-max');
                if (key === 'ev-log-min' && ev >= hi)
                    throw new Error(sprintf(_('The dark reference must be below the bright reference (%s). Current reading: %s'), hi.toFixed(1), ev.toFixed(1)));
                if (key === 'ev-log-max' && ev <= lo)
                    throw new Error(sprintf(_('The bright reference must be above the dark reference (%s). Current reading: %s'), lo.toFixed(1), ev.toFixed(1)));

                const isDark = key === 'ev-log-min';
                const oldValue = isDark ? lo : hi;
                const newLo = isDark ? ev : lo;
                const newHi = isDark ? hi : ev;
                const minPct = settings.get_int('brightness-min-pct');
                const maxPct = settings.get_int('brightness-max-pct');
                const oldPct = estimateBrightnessPct(ev, lo, hi, minPct, maxPct);
                const newPct = estimateBrightnessPct(ev, newLo, newHi, minPct, maxPct);
                const label = isDark ? _('Dark reference ev') : _('Bright reference ev');
                const pctLine = newPct === null
                    ? ''
                    : `\n${sprintf(_('With this value, brightness here would be about %s'), `${newPct}%`)}` +
                      (oldPct === null ? '' : ` (${sprintf(_('current setting: about %s'), `${oldPct}%`)})`) +
                      `\n${_('Estimate calculated without the calibration offset.')}`;

                confirmDialog({
                    window,
                    heading: isDark ? _('Change the dark reference?') : _('Change the bright reference?'),
                    body: `${label}: ${oldValue.toFixed(1)} → ${ev.toFixed(1)}${pctLine}`,
                    cancelLabel: _('Cancel'),
                    confirmLabel: _('Apply'),
                    onConfirm: () => applyEvReference(key, ev, oldValue),
                });
            } catch (e) {
                toast(errorText(e));
            }
        };

        const applyEvReference = (key, ev, previous) => {
            if (!settings.set_double(key, ev)) {
                toast(_('The value is out of the allowed range.'));
                return;
            }
            const isDark = key === 'ev-log-min';
            window.add_toast(undoToast(
                sprintf(isDark ? _('Dark reference set to %s') : _('Bright reference set to %s'), ev.toFixed(1)),
                () => {
                    if (!settings.set_double(key, previous))
                        toast(_('Could not undo.'));
                }));
        };
        evGroup.add(buttonRow(_('Use current surroundings as dark reference'),
            _('Measures now in dark surroundings and stores it as the dark reference ev'), _('Measure and set'),
            () => setEvReference('ev-log-min')).row);
        evGroup.add(buttonRow(_('Use current surroundings as bright reference'),
            _('Measures now in bright surroundings and stores it as the bright reference ev'), _('Measure and set'),
            () => setEvReference('ev-log-max')).row);

        const ambientGroup = addGroup(calPage, _('Ambient temperature range'),
            _('The measured ambient temperature is mapped linearly from this range to the display range.'));
        ambientGroup.add(spinRow('ambient-cct-min', {
            title: _('Minimum ambient temperature (K)'), step: 100, page: 500,
        }));
        ambientGroup.add(spinRow('ambient-cct-max', {
            title: _('Maximum ambient temperature (K)'), step: 100, page: 500,
        }));

        // Calibration points: each (measured K, actual K) pair. The list is rebuilt on every change.
        const calListGroup = addGroup(calPage, _('Temperature calibration points'),
            _('Measured ambient temperature → actual light temperature. One point shifts everything by that amount; two or more are joined linearly. With none, no correction is applied.'));
        calListGroup.set_header_suffix(resetButton('cct-calibration'));

        let calRows = [];
        const calPoints = () => settings.get_value('cct-calibration').deepUnpack().map(([m, a]) => [m, a]);
        const writeCalPoints = points => {
            settings.set_value('cct-calibration', new GLib.Variant('a(dd)', points));
        };

        const rebuildCalRows = () => {
            for (const row of calRows)
                calListGroup.remove(row);
            calRows = [];

            const points = calPoints();
            if (points.length === 0) {
                const empty = new Adw.ActionRow({
                    title: _('No calibration points'),
                    subtitle: _('The ambient temperature is used as measured'),
                });
                calListGroup.add(empty);
                calRows.push(empty);
                return;
            }

            points.forEach(([measured, actual], index) => {
                const row = new Adw.ActionRow({
                    title: `${Math.round(measured)} K → ${Math.round(actual)} K`,
                    subtitle: _('Measured ambient temperature → actual light temperature'),
                });
                const del = new Gtk.Button({
                    icon_name: 'user-trash-symbolic',
                    tooltip_text: _('Delete'),
                    valign: Gtk.Align.CENTER,
                    css_classes: ['flat'],
                });
                del.connect('clicked', () => {
                    const next = calPoints();
                    next.splice(index, 1);
                    writeCalPoints(next);
                });
                row.add_suffix(del);
                calListGroup.add(row);
                calRows.push(row);
            });
        };
        settings.connect('changed::cct-calibration', rebuildCalRows);
        rebuildCalRows();

        const addPointGroup = addGroup(calPage, _('Add point'),
            _('Enter the actual temperature of the current light and it is paired with the ambient temperature measured now. The daemon must be running.'));
        const trueKRow = new Adw.EntryRow({title: _('Actual light temperature (K)')});
        const addBtn = new Gtk.Button({label: _('Add'), valign: Gtk.Align.CENTER, css_classes: ['suggested-action']});
        trueKRow.add_suffix(addBtn);
        addPointGroup.add(trueKRow);

        addBtn.connect('clicked', async () => {
            const trueK = Number.parseFloat(trueKRow.get_text());
            if (!Number.isFinite(trueK) || trueK < 1000 || trueK > 15000) {
                toast(_('Enter a number between 1000 and 15000.'));
                return;
            }
            try {
                const props = await measureNow();
                const measured = Number(props.AmbientCct);
                const previous = calPoints();
                writeCalPoints([...previous, [measured, trueK]].sort((x, y) => x[0] - y[0]));
                trueKRow.set_text('');
                window.add_toast(undoToast(
                    `${_('Point added')}: ${Math.round(measured)} K → ${Math.round(trueK)} K`,
                    () => writeCalPoints(previous)));
            } catch (e) {
                toast(errorText(e));
            }
        });

        // =============================================================
        // 정보 (about)
        // =============================================================
        const infoPage = addPage(_('About'), 'help-about-symbolic');

        const versionGroup = addGroup(infoPage, _('Version'));
        const version = this.metadata?.['version-name'] ?? this.metadata?.version ?? '?';
        versionGroup.add(new Adw.ActionRow({
            title: _('Extension version'),
            subtitle: `${version} (metadata ${this.metadata?.version ?? '?'})`,
        }));

        const daemonGroup = addGroup(infoPage, _('Daemon'),
            _('Refreshed every 5 seconds.'));
        const stateRow = new Adw.ActionRow({title: _('Status'), subtitle: '…'});
        const lastRow = new Adw.ActionRow({title: _('Last measurement'), subtitle: '…'});
        daemonGroup.add(stateRow);
        daemonGroup.add(lastRow);

        const updateInfo = async () => {
            const props = await readDaemonProps();
            applyBackend(props);
            if (!props) {
                stateRow.set_subtitle(_('Not running'));
                lastRow.set_subtitle('-');
                return;
            }
            // BrightnessBackend is missing on daemons older than v0.1.8; show nothing extra then.
            const backend = backendText(props.BrightnessBackend);
            stateRow.set_subtitle(backend
                ? `${stateText(props.State)} · ${_('Brightness')}: ${backend}`
                : stateText(props.State));
            const last = Number(props.LastMeasured);
            if (!last) {
                lastRow.set_subtitle(_('None yet'));
                return;
            }
            const ago = Math.max(0, Math.round(Date.now() / 1000 - last));
            const agoText = ago < 60 ? sprintf(_('%s s ago'), ago) : sprintf(_('%s min ago'), Math.floor(ago / 60));
            lastRow.set_subtitle(
                `ev ${Number(props.EvLog).toFixed(1)} · ${Math.round(Number(props.AmbientCct))} K · ${agoText}`);
        };
        updateInfo();
        const infoTimerId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 5, () => {
            updateInfo();
            return GLib.SOURCE_CONTINUE;
        });
        window.connect('close-request', () => {
            GLib.source_remove(infoTimerId);
            return false;
        });

        const logGroup = addGroup(infoPage, _('Log'));
        const logRow = new Adw.ActionRow({title: _('Command to view the log'), subtitle: JOURNAL_CMD});
        const copyBtn = new Gtk.Button({label: _('Copy'), valign: Gtk.Align.CENTER});
        copyBtn.connect('clicked', () => {
            const value = new GObject.Value();
            value.init(GObject.TYPE_STRING);
            value.set_string(JOURNAL_CMD);
            window.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(value));
            toast(_('Command copied.'));
        });
        logRow.add_suffix(copyBtn);
        logGroup.add(logRow);

        const linkGroup = addGroup(infoPage, _('Project'));
        const repoRow = new Adw.ActionRow({title: _('Repository'), subtitle: REPO_URL});
        const repoBtn = Gtk.LinkButton.new_with_label(REPO_URL, _('Open'));
        repoBtn.set_valign(Gtk.Align.CENTER);
        repoRow.add_suffix(repoBtn);
        linkGroup.add(repoRow);
    }
}
