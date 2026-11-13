import Adw from 'gi://Adw?version=1';
import GLib from 'gi://GLib';
import Prefs from './prefs.js';
const app = new Adw.Application({application_id: 'test.webcamals.prefs'});
app.connect('activate', () => {
    const win = new Adw.PreferencesWindow({application: app});
    try { new Prefs().fillPreferencesWindow(win); print('FILL_OK'); }
    catch (e) { print('FILL_ERROR: ' + e + '\n' + e.stack); }
    win.present();
    GLib.timeout_add_seconds(0, 6, () => { app.quit(); return false; });
});
app.run([]);
