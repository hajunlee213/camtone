// Preferences window (v0.1.7). Modeled on gnome-adaptive-tone's prefs.js: Adw pages and groups,
// one SpinRow/SwitchRow per GSettings key, and a per-row reset button.
//
// Every value is bound to the existing schema keys (extension/schemas/...). Ranges come from the
// schema itself. The calibration helpers talk to the daemon over D-Bus (io.github.hajunlee213.WebcamAlsRgb).
import Adw from 'gi://Adw';
import Gdk from 'gi://Gdk';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Gtk from 'gi://Gtk';
import {ExtensionPreferences, gettext as _} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

const DAEMON_NAME = 'io.github.hajunlee213.WebcamAlsRgb';
const DAEMON_PATH = '/io/github/hajunlee213/WebcamAlsRgb';
const DAEMON_IFACE = 'io.github.hajunlee213.WebcamAlsRgb';
const JOURNAL_CMD = 'journalctl --user -u webcam-als-rgb -f';
const REPO_URL = 'https://github.com/hajunlee213/webcam-as-als-rgb';

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
        throw new Error(_('데몬이 실행 중이 아닙니다. 퀵 설정에서 켜 주세요.'));
    await daemonCall(DAEMON_IFACE, 'MeasureNow', null, null);
    const after = await readDaemonProps();
    if (!after || Number(after.LastMeasured) === Number(before.LastMeasured))
        throw new Error(_('측정 결과가 갱신되지 않았습니다. 일시정지 상태인지 확인하세요.'));
    return after;
}

function errorText(e) {
    return String(e?.message ?? e).replace(/^GDBus\.Error:[^:]*:\s*/, '');
}

function stateText(state) {
    switch (state) {
    case 'running':
        return _('실행 중');
    case 'paused-locked':
        return _('잠김');
    case 'paused-sleep':
        return _('절전 중');
    case 'paused-battery':
        return _('배터리 절약');
    case 'paused-night':
        return _('야간 고정');
    case 'camera-busy':
        return _('카메라 사용 중');
    case 'starting':
        return _('시작 중');
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
        return _('직접 제어 (logind)');
    case 'none':
        return _('없음');
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
    const toast = new Adw.Toast({title, timeout: 10, button_label: _('되돌리기')});
    toast.connect('button-clicked', () => onUndo());
    return toast;
}

export default class WebcamAlsRgbPreferences extends ExtensionPreferences {
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
                tooltip_text: _('기본값으로'),
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
        const brightnessPage = addPage(_('밝기'), 'display-brightness-symbolic');

        const serviceGroup = addGroup(brightnessPage, _('서비스'),
            _('퀵 설정 토글과 같은 값입니다. 끄면 측정과 조정을 멈추고 야간 모드를 원래대로 되돌립니다.'));
        serviceGroup.add(switchRow('enabled', _('서비스 사용')));

        const brightnessGroup = addGroup(brightnessPage, _('자동 밝기'));
        brightnessGroup.add(switchRow('auto-brightness', _('자동 밝기'),
            _('주변 밝기에 맞춰 화면 밝기를 조정합니다')));
        brightnessGroup.add(spinRow('brightness-min-pct', {
            title: _('최소 밝기 (%)'), subtitle: _('어두운 곳에서 쓰는 밝기'), step: 1, page: 5,
        }));
        brightnessGroup.add(spinRow('brightness-max-pct', {
            title: _('최대 밝기 (%)'), subtitle: _('밝은 곳에서 쓰는 밝기'), step: 1, page: 5,
        }));
        brightnessGroup.add(spinRow('brightness-deadband-pct', {
            title: _('밝기 데드밴드 (%)'), subtitle: _('이만큼 변하지 않으면 밝기를 바꾸지 않음'), step: 1, page: 5,
        }));

        // Learned offset: written by the daemon when the user adjusts brightness by hand.
        const learnedGroup = addGroup(brightnessPage, _('학습된 밝기 오프셋'),
            _('수동으로 밝기를 조절하면 데몬이 그 차이를 학습해 이 값에 더합니다.'));
        const offsetRow = new Adw.ActionRow({title: _('학습된 오프셋')});
        const offsetReset = new Gtk.Button({label: _('초기화'), valign: Gtk.Align.CENTER});
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
        const shellGroup = addGroup(brightnessPage, _('밝기 미세 조정'),
            _('밝기 미세 조정은 GNOME 밝기 슬라이더로 합니다. 슬라이더 가운데(50%)가 보정 없음입니다.'));
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
        const cctPage = addPage(_('색온도'), 'night-light-symbolic');

        const cctGroup = addGroup(cctPage, _('자동 색온도'));
        cctGroup.add(switchRow('auto-color-temp', _('자동 색온도'),
            _('주변 색을 보고 야간 모드 색온도를 조정합니다')));

        const boundsGroup = addGroup(cctPage, _('화면 색온도 범위'),
            _('주변 색온도 범위가 화면 색온도 범위에 선형으로 대응합니다. 보정은 보정 쪽에서 합니다.'));
        boundsGroup.add(spinRow('display-cct-min', {
            title: _('최소 색온도 (K, 가장 따뜻함)'), step: 50, page: 200,
        }));
        boundsGroup.add(spinRow('display-cct-max', {
            title: _('최대 색온도 (K, 가장 차가움)'), step: 50, page: 200,
        }));
        boundsGroup.add(spinRow('cct-offset', {
            title: _('색온도 오프셋 (K)'), subtitle: _('+ 차갑게, - 따뜻하게'), step: 25, page: 100,
        }));
        boundsGroup.add(spinRow('cct-deadband-k', {
            title: _('색온도 데드밴드 (K)'), subtitle: _('이만큼 변하지 않으면 색온도를 바꾸지 않음'), step: 10, page: 50,
        }));

        const psrGroup = addGroup(cctPage, _('화면 갱신'));
        psrGroup.add(switchRow('psr-refresh', _('PSR2 전체 갱신'),
            _('Intel PSR2 패널에서 색온도 변경이 화면 전체에 바로 반영되도록 강제로 다시 그립니다')));

        // =============================================================
        // 측정·전원 (measurement and power)
        // =============================================================
        const measurePage = addPage(_('측정·전원'), 'preferences-system-time-symbolic');

        const intervalGroup = addGroup(measurePage, _('측정 간격'),
            _('변화가 없으면 간격이 최대 간격까지 자동으로 늘어납니다.'));
        intervalGroup.add(spinRow('interval-ac-sec', {
            title: _('AC 전원 간격 (초)'), step: 5, page: 30,
        }));
        intervalGroup.add(spinRow('interval-battery-sec', {
            title: _('배터리 간격 (초)'), step: 10, page: 60,
        }));
        intervalGroup.add(spinRow('interval-max-sec', {
            title: _('최대 적응 간격 (초)'), step: 30, page: 120,
        }));

        const powerGroup = addGroup(measurePage, _('전원'));
        powerGroup.add(switchRow('pause-on-battery', _('배터리에서 측정 일시정지'),
            _('켜면 배터리 사용 중에는 카메라를 켜지 않습니다')));

        const nightGroup = addGroup(measurePage, _('야간 고정 스케줄'),
            _('지정한 시간대에는 카메라를 켜지 않고 색온도를 고정합니다. 밝기는 GNOME이 관리합니다.'));
        nightGroup.add(switchRow('night-schedule-enabled', _('야간 스케줄 사용')));
        nightGroup.add(spinRow('night-schedule-temp', {title: _('고정 색온도 (K)'), step: 100, page: 500}));
        nightGroup.add(spinRow('night-schedule-from-hour', {title: _('시작 시')}));
        nightGroup.add(spinRow('night-schedule-from-minute', {title: _('시작 분'), step: 5, page: 15}));
        nightGroup.add(spinRow('night-schedule-to-hour', {title: _('종료 시')}));
        nightGroup.add(spinRow('night-schedule-to-minute', {title: _('종료 분'), step: 5, page: 15}));

        const smoothGroup = addGroup(measurePage, _('측정 안정화'));
        smoothGroup.add(spinRow('ema-alpha', {
            title: _('평활화 계수 (EMA α)'),
            subtitle: _('클수록 빠르게 반응하고, 작을수록 부드럽습니다'),
            digits: 2, step: 0.05, page: 0.1,
        }));

        const indicatorGroup = addGroup(measurePage, _('상단바'));
        indicatorGroup.add(switchRow('show-indicator', _('상단바 아이콘 표시'),
            _('서비스가 켜져 있을 때 상단바에 아이콘을 표시합니다')));

        // =============================================================
        // 보정 (calibration)
        // =============================================================
        const calPage = addPage(_('보정'), 'applications-science-symbolic');

        const evGroup = addGroup(calPage, _('밝기 기준 (ev)'),
            _('주변 밝기 측정값을 밝기 %로 바꾸는 두 기준점입니다. 어두운 곳과 밝은 곳에서 각각 버튼을 누르면 지금 측정값이 들어갑니다.'));
        evGroup.add(spinRow('ev-log-min', {
            title: _('어두운 기준 ev'), subtitle: _('이 값 이하는 최소 밝기'), digits: 1, step: 0.1, page: 0.5,
        }));
        evGroup.add(spinRow('ev-log-max', {
            title: _('밝은 기준 ev'), subtitle: _('이 값 이상은 최대 밝기'), digits: 1, step: 0.1, page: 0.5,
        }));

        // Measures, checks, then asks before writing. Refuses a reference that would cross the other one
        // (min must stay below max). Nothing is written until the user confirms in the dialog.
        const setEvReference = async key => {
            try {
                const props = await measureNow();
                const ev = Number(props.EvLog);
                if (!Number.isFinite(ev))
                    throw new Error(_('측정값을 읽지 못했습니다.'));
                const lo = settings.get_double('ev-log-min');
                const hi = settings.get_double('ev-log-max');
                if (key === 'ev-log-min' && ev >= hi)
                    throw new Error(`${_('어두운 기준은 밝은 기준')} (${hi.toFixed(1)}) ${_('보다 작아야 합니다. 현재 측정값')}: ${ev.toFixed(1)}`);
                if (key === 'ev-log-max' && ev <= lo)
                    throw new Error(`${_('밝은 기준은 어두운 기준')} (${lo.toFixed(1)}) ${_('보다 커야 합니다. 현재 측정값')}: ${ev.toFixed(1)}`);

                const isDark = key === 'ev-log-min';
                const oldValue = isDark ? lo : hi;
                const newLo = isDark ? ev : lo;
                const newHi = isDark ? hi : ev;
                const minPct = settings.get_int('brightness-min-pct');
                const maxPct = settings.get_int('brightness-max-pct');
                const oldPct = estimateBrightnessPct(ev, lo, hi, minPct, maxPct);
                const newPct = estimateBrightnessPct(ev, newLo, newHi, minPct, maxPct);
                const label = isDark ? _('어두운 기준 ev') : _('밝은 기준 ev');
                const pctLine = newPct === null
                    ? ''
                    : `\n${_('이 값을 쓰면 지금 환경에서 밝기가 약')} ${newPct}%${_('가 됩니다')}` +
                      (oldPct === null ? '' : ` (${_('현재 설정')}: ${_('약')} ${oldPct}%)`) +
                      `\n${_('보정 오프셋은 빼고 계산한 추정치입니다.')}`;

                confirmDialog({
                    window,
                    heading: isDark ? _('어두운 기준을 바꿀까요?') : _('밝은 기준을 바꿀까요?'),
                    body: `${label}: ${oldValue.toFixed(1)} → ${ev.toFixed(1)}${pctLine}`,
                    cancelLabel: _('취소'),
                    confirmLabel: _('적용'),
                    onConfirm: () => applyEvReference(key, ev, oldValue),
                });
            } catch (e) {
                toast(errorText(e));
            }
        };

        const applyEvReference = (key, ev, previous) => {
            if (!settings.set_double(key, ev)) {
                toast(_('값이 허용 범위를 벗어났습니다.'));
                return;
            }
            const isDark = key === 'ev-log-min';
            window.add_toast(undoToast(
                `${isDark ? _('어두운') : _('밝은')} ${_('기준을')} ${ev.toFixed(1)}${_('으로 설정했습니다')}`,
                () => {
                    if (!settings.set_double(key, previous))
                        toast(_('되돌리지 못했습니다.'));
                }));
        };
        evGroup.add(buttonRow(_('현재 환경을 어두운 기준으로'),
            _('지금 어두운 곳에서 측정해 어두운 기준 ev로 넣습니다'), _('측정해서 설정'),
            () => setEvReference('ev-log-min')).row);
        evGroup.add(buttonRow(_('현재 환경을 밝은 기준으로'),
            _('지금 밝은 곳에서 측정해 밝은 기준 ev로 넣습니다'), _('측정해서 설정'),
            () => setEvReference('ev-log-max')).row);

        const ambientGroup = addGroup(calPage, _('주변 색온도 범위'),
            _('측정한 주변 색온도를 이 범위에서 화면 색온도 범위로 선형 변환합니다.'));
        ambientGroup.add(spinRow('ambient-cct-min', {
            title: _('주변 최소 색온도 (K)'), step: 100, page: 500,
        }));
        ambientGroup.add(spinRow('ambient-cct-max', {
            title: _('주변 최대 색온도 (K)'), step: 100, page: 500,
        }));

        // Calibration points: each (measured K, actual K) pair. The list is rebuilt on every change.
        const calListGroup = addGroup(calPage, _('색온도 보정 점'),
            _('측정 주변 색온도 → 실제 조명 색온도. 1개면 그만큼 평행 이동하고, 2개 이상이면 점 사이를 선형으로 잇습니다. 비우면 보정하지 않습니다.'));
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
                    title: _('보정 점 없음'),
                    subtitle: _('주변 색온도를 그대로 사용합니다'),
                });
                calListGroup.add(empty);
                calRows.push(empty);
                return;
            }

            points.forEach(([measured, actual], index) => {
                const row = new Adw.ActionRow({
                    title: `${Math.round(measured)} K → ${Math.round(actual)} K`,
                    subtitle: _('측정 주변 색온도 → 실제 조명 색온도'),
                });
                const del = new Gtk.Button({
                    icon_name: 'user-trash-symbolic',
                    tooltip_text: _('삭제'),
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

        const addPointGroup = addGroup(calPage, _('점 추가'),
            _('지금 조명의 실제 색온도를 입력하면, 지금 측정한 주변 색온도와 짝지어 추가합니다. 데몬이 실행 중이어야 합니다.'));
        const trueKRow = new Adw.EntryRow({title: _('실제 조명 색온도 (K)')});
        const addBtn = new Gtk.Button({label: _('추가'), valign: Gtk.Align.CENTER, css_classes: ['suggested-action']});
        trueKRow.add_suffix(addBtn);
        addPointGroup.add(trueKRow);

        addBtn.connect('clicked', async () => {
            const trueK = Number.parseFloat(trueKRow.get_text());
            if (!Number.isFinite(trueK) || trueK < 1000 || trueK > 15000) {
                toast(_('1000~15000 사이의 숫자를 입력하세요.'));
                return;
            }
            try {
                const props = await measureNow();
                const measured = Number(props.AmbientCct);
                const previous = calPoints();
                writeCalPoints([...previous, [measured, trueK]].sort((x, y) => x[0] - y[0]));
                trueKRow.set_text('');
                window.add_toast(undoToast(
                    `${_('점 추가됨')}: ${Math.round(measured)} K → ${Math.round(trueK)} K`,
                    () => writeCalPoints(previous)));
            } catch (e) {
                toast(errorText(e));
            }
        });

        // =============================================================
        // 정보 (about)
        // =============================================================
        const infoPage = addPage(_('정보'), 'help-about-symbolic');

        const versionGroup = addGroup(infoPage, _('버전'));
        const version = this.metadata?.['version-name'] ?? this.metadata?.version ?? '?';
        versionGroup.add(new Adw.ActionRow({
            title: _('확장 버전'),
            subtitle: `${version} (metadata ${this.metadata?.version ?? '?'})`,
        }));

        const daemonGroup = addGroup(infoPage, _('데몬'),
            _('5초마다 갱신합니다.'));
        const stateRow = new Adw.ActionRow({title: _('상태'), subtitle: '…'});
        const lastRow = new Adw.ActionRow({title: _('마지막 측정'), subtitle: '…'});
        daemonGroup.add(stateRow);
        daemonGroup.add(lastRow);

        const updateInfo = async () => {
            const props = await readDaemonProps();
            applyBackend(props);
            if (!props) {
                stateRow.set_subtitle(_('실행 중이 아님'));
                lastRow.set_subtitle('-');
                return;
            }
            // BrightnessBackend is missing on daemons older than v0.1.8; show nothing extra then.
            const backend = backendText(props.BrightnessBackend);
            stateRow.set_subtitle(backend
                ? `${stateText(props.State)} · ${_('밝기')}: ${backend}`
                : stateText(props.State));
            const last = Number(props.LastMeasured);
            if (!last) {
                lastRow.set_subtitle(_('아직 없음'));
                return;
            }
            const ago = Math.max(0, Math.round(Date.now() / 1000 - last));
            const agoText = ago < 60 ? `${ago}${_('초 전')}` : `${Math.floor(ago / 60)}${_('분 전')}`;
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

        const logGroup = addGroup(infoPage, _('로그'));
        const logRow = new Adw.ActionRow({title: _('로그 보기 명령'), subtitle: JOURNAL_CMD});
        const copyBtn = new Gtk.Button({label: _('복사'), valign: Gtk.Align.CENTER});
        copyBtn.connect('clicked', () => {
            const value = new GObject.Value();
            value.init(GObject.TYPE_STRING);
            value.set_string(JOURNAL_CMD);
            window.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(value));
            toast(_('명령을 복사했습니다.'));
        });
        logRow.add_suffix(copyBtn);
        logGroup.add(logRow);

        const linkGroup = addGroup(infoPage, _('프로젝트'));
        const repoRow = new Adw.ActionRow({title: _('저장소'), subtitle: REPO_URL});
        const repoBtn = Gtk.LinkButton.new_with_label(REPO_URL, _('열기'));
        repoBtn.set_valign(Gtk.Align.CENTER);
        repoRow.add_suffix(repoBtn);
        linkGroup.add(repoRow);
    }
}
