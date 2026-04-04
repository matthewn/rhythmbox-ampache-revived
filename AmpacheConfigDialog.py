# vim: expandtab shiftwidth=8 softtabstop=8 tabstop=8
#
# (c) 2010
#       envyseapets@gmail.com
#       grindlay@gmail.com
#       langdalepl@gmail.com
#       massimo.mund@googlemail.com
#       bethebunny@gmail.com,
# 2012-2015 lotan_rm@gmx.de
#
# This file is part of the Rhythmbox Ampache plugin.
#
# The Rhythmbox Ampache plugin is free software; you can redistribute it
# and/or modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version.
#
# The Rhythmbox Ampache plugin is distributed in the hope that it will
# be useful, but WITHOUT ANY WARRANTY; without even the implied warranty
# of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with the Rhythmbox Ampache plugin.  If not, see
# <http://www.gnu.org/licenses/>.

import os
import rb
from gi.repository import GObject, Gtk, Gio, PeasGtk


class AmpacheConfigDialog(GObject.Object, PeasGtk.Configurable):
    __gtype_name__ = 'AmpacheConfigDialog'
    object = GObject.property(type=GObject.Object)

    def do_create_configure_widget(self):

        self.settings = Gio.Settings('org.gnome.rhythmbox.plugins.ampache')
        self.ui = Gtk.Builder()
        ui_file = rb.find_plugin_file(self, 'ampache-prefs.ui') or \
            os.path.join(os.path.dirname(__file__), 'ampache-prefs.ui')
        self.ui.add_from_file(ui_file)
        self.config_dialog = self.ui.get_object('config')

        fields = [
            ('url', 'url_entry', False),
            ('username', 'username_entry', False),
            ('password', 'password_entry', True),
        ]
        self._signal_pairs = []
        for key, widget_name, hide in fields:
            w = self.ui.get_object(widget_name)
            if hide:
                w.set_visibility(False)
            w.set_text(self.settings[key])
            self._signal_pairs.append(
                (w, w.connect('changed', self._on_entry_changed, key)))
        self.config_dialog.connect('destroy', self._on_destroy)

        return self.config_dialog

    def _on_destroy(self, widget):
        for obj, hid in self._signal_pairs:
            obj.disconnect(hid)
        self._signal_pairs = []

    def _on_entry_changed(self, widget, key):
        self.settings[key] = widget.get_text()
