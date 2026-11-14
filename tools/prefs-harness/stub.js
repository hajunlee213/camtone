import Gio from 'gi://Gio';
export const gettext = s => s;
export class ExtensionPreferences {
    constructor() { this.metadata = {version: 3}; }
    initTranslations() {}
    getSettings() { return new Gio.Settings({schema_id: 'org.gnome.shell.extensions.camtone'}); }
}
