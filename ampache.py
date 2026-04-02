# -*- Mode: python; coding: utf-8; tab-width: 8; indent-tabs-mode: t; -*-
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
from gi.repository import RB
from gi.repository import GObject, Peas, Gtk, Gio, GdkPixbuf

from AmpacheConfigDialog import AmpacheConfigDialog  # noqa: F401 (Peas discovers this class by name)
from AmpacheBrowser import AmpacheBrowser

# _ is injected as a builtin by Rhythmbox's plugin loader at runtime.
_ = str


class AmpacheEntryType(RB.RhythmDBEntryType):
    def __init__(self):
        RB.RhythmDBEntryType.__init__(
                self,
                name='AmpacheEntryType',
                save_to_disk=False)

    def can_sync_metadata(self, entry):
        return True

    def sync_metadata(self, entry, changes):
        return


class Ampache(GObject.Object, Peas.Activatable):
    __gtype_name__ = 'AmpachePlugin'
    object = GObject.property(type=GObject.Object)

    def do_activate(self):
        shell = self.object
        db = shell.props.db

        # load icon
        _ok, width, height = Gtk.icon_size_lookup(Gtk.IconSize.LARGE_TOOLBAR)
        ico_path = os.path.join(self.plugin_info.get_data_dir(), 'ampache.ico')
        if not os.path.exists(ico_path):
            ico_path = os.path.join(os.path.dirname(__file__), 'ampache.ico')
        icon = GdkPixbuf.Pixbuf.new_from_file_at_size(ico_path, width, height) if os.path.exists(ico_path) else None

        # register AmpacheEntryType
        self._entry_type = AmpacheEntryType()
        db.register_entry_type(self._entry_type)

        # fetch plugin settings
        settings = Gio.Settings("org.gnome.rhythmbox.plugins.ampache")

        menu = Gio.Menu()
        menu.append('Refetch Ampache Library', 'app.refetch-ampache')

        # create AmpacheBrowser source
        self._source = GObject.new(
            AmpacheBrowser,
            shell=shell,
            entry_type=self._entry_type,
            icon=icon,
            plugin=self,
            settings=settings.get_child("source"),
            toolbar_menu=menu,
            name=_("Ampache")
        )
        # assign AmpacheEntryType to AmpacheBrowser source
        shell.register_entry_type_for_source(
            self._source,
            self._entry_type)

        # insert AmpacheBrowser source into Shared group
        shell.append_display_page(
            self._source,
            RB.DisplayPageGroup.get_by_id("shared"))

    def do_deactivate(self):
        # destroy AmpacheBrowser source
        self._source.delete_thyself()
        self._source = None

        # destroy entry type
        self._entry_type = None
