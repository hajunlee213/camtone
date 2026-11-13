import Adw from 'gi://Adw?version=1';
import GLib from 'gi://GLib';
import Prefs, {confirmDialog, undoToast, estimateBrightnessPct} from './prefs.js';
const app = new Adw.Application({application_id: 'test.webcamals.prefs'});
const fail = msg => print('HARNESS_ERROR: ' + msg);
app.connect('activate', () => {
    const win = new Adw.PreferencesWindow({application: app});
    try { new Prefs().fillPreferencesWindow(win); print('FILL_OK'); }
    catch (e) { print('FILL_ERROR: ' + e + '\n' + e.stack); }
    win.present();

    // HARNESS-ONLY, enabled with HARNESS_DIALOGS=1. Builds the calibration confirm dialog (both kinds)
    // and the 10 s undo toast, shows the dialogs for 2 s, and never clicks anything. The apply and undo
    // callbacks report an error if they run, so no setting can change here.
    if (GLib.getenv('HARNESS_DIALOGS')) {
        try {
            // -6 in a -10..-2.3 range with 15..95 %: 15 + (4 / 7.7) * 80 = 56.6 -> 57
            const pct = estimateBrightnessPct(-6, -10, -2.3, 15, 95);
            if (pct !== 57) fail(`estimate expected 57, got ${pct}`);
            if (estimateBrightnessPct(-6, -2.3, -2.3, 15, 95) !== null) fail('estimate should be null for an empty range');
            if (estimateBrightnessPct(-20, -10, -2.3, 15, 95) !== 15) fail('estimate should clamp to the minimum');

            const kinds = typeof Adw.AlertDialog === 'function' ? ['alert', 'message'] : ['message'];
            const dialogs = kinds.map(kind => confirmDialog({
                window: win, heading: `dialog check (${kind})`, body: 'body', cancelLabel: 'cancel',
                confirmLabel: 'apply', kind, onConfirm: () => fail(`${kind} dialog confirm ran`),
            }));
            print(`DIALOGS_SHOWN: ${kinds.join(',')}`);
            GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 2, () => {
                for (const d of dialogs) d.close();
                return false;
            });

            const toast = undoToast('undo check', () => fail('undo callback ran'));
            if (toast.get_timeout() !== 10) fail(`toast timeout expected 10, got ${toast.get_timeout()}`);
            if (toast.get_button_label() !== '되돌리기') fail(`toast button label is ${toast.get_button_label()}`);
            win.add_toast(toast);
            print('DIALOGS_OK');
        } catch (e) {
            print('DIALOGS_ERROR: ' + e + '\n' + e.stack);
        }
    }

    GLib.timeout_add_seconds(0, 6, () => { app.quit(); return false; });
});
app.run([]);
