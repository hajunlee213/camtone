// Every constructor prop and method prefs.js uses, checked against the real Adw/Gtk/Gdk classes.
import Adw from 'gi://Adw?version=1';
import Gtk from 'gi://Gtk?version=4.0';
import Gdk from 'gi://Gdk?version=4.0';
import GObject from 'gi://GObject';
Adw.init();
const bad = [];
const propsOf = cls => new Set(cls.list_properties().map(p => p.name));
const norm = n => n.replace(/_/g, '-');
const checkProps = (cls, names) => { const have = propsOf(cls); for (const n of names) if (!have.has(norm(n))) bad.push(`${cls.$gtype.name}: no property '${n}'`); };
const checkMethods = (obj, names) => { for (const n of names) if (typeof obj[n] !== 'function') bad.push(`${obj.constructor.name}: no method '${n}'`); };

// constructor props as used in prefs.js
checkProps(Adw.Toast, ['title', 'timeout']);
checkProps(Gtk.Button, ['label', 'valign', 'icon_name', 'tooltip_text', 'css_classes']);
checkProps(Adw.SwitchRow, ['title', 'subtitle']);
checkProps(Adw.SpinRow, ['title', 'subtitle', 'digits', 'climb_rate', 'adjustment']);
checkProps(Adw.PreferencesPage, ['title', 'icon_name']);
checkProps(Adw.PreferencesGroup, ['title', 'description']);
checkProps(Adw.ActionRow, ['title', 'subtitle']);
checkProps(Adw.EntryRow, ['title']);
checkProps(Gtk.Adjustment, ['lower', 'upper', 'step-increment', 'page-increment']);
// methods on instances
const win = new Adw.PreferencesWindow();
checkMethods(win, ['add', 'add_toast', 'set_search_enabled', 'set_default_size', 'get_clipboard', 'connect', 'close']);
const page = new Adw.PreferencesPage({title: 'x'});
checkMethods(page, ['add']);
const group = new Adw.PreferencesGroup({title: 'x'});
checkMethods(group, ['add', 'remove', 'set_header_suffix']);
const row = new Adw.ActionRow({title: 'x'});
checkMethods(row, ['add_suffix', 'set_subtitle', 'get_subtitle', 'set_title', 'get_title']);
const sw = new Adw.SwitchRow({title: 'x'});
checkMethods(sw, ['add_suffix', 'get_active']);
const sp = new Adw.SpinRow({title: 'x'});
checkMethods(sp, ['add_suffix', 'get_value', 'set_value', 'get_digits']);
const er = new Adw.EntryRow({title: 'x'});
checkMethods(er, ['add_suffix', 'get_text', 'set_text']);
const btn = new Gtk.Button({label: 'x'});
checkMethods(btn, ['set_sensitive', 'get_sensitive', 'add_css_class', 'set_valign', 'get_label', 'get_tooltip_text', 'emit']);
const lb = Gtk.LinkButton.new_with_label('https://example.org', 'x');
checkMethods(lb, ['set_valign']);
if (typeof Gtk.LinkButton.new_with_label !== 'function') bad.push('Gtk.LinkButton.new_with_label missing');
checkMethods(win, ['get_clipboard']);
const clip = win.get_clipboard();
checkMethods(clip, ['set_content']);
if (typeof Gdk.ContentProvider.new_for_value !== 'function') bad.push('Gdk.ContentProvider.new_for_value missing');
const v = new GObject.Value(); v.init(GObject.TYPE_STRING); v.set_string('x');
if (typeof GObject.TYPE_STRING !== 'object' && typeof GObject.TYPE_STRING !== 'number' && typeof GObject.TYPE_STRING !== 'string') bad.push('TYPE_STRING missing');
print(bad.length ? 'BAD:\n  ' + bad.join('\n  ') : 'PROPS_OK: every constructor prop and method exists');
