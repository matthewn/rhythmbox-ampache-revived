# -*- Mode: python; coding: utf-8; tab-width: 8; indent-tabs-mode: t;
# -*- vim: expandtab shiftwidth=8 softtabstop=8 tabstop=8
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

import gi
gi.require_version('Soup', '3.0')
from gi.repository import RB
from gi.repository import GObject, Gtk, Gio, GLib, Soup

import faulthandler
faulthandler.enable()

import time
from datetime import datetime
import re
import hashlib
import os
import sys
import collections

import xml.sax
import xml.sax.handler

class HandshakeHandler(xml.sax.handler.ContentHandler):
        def __init__(self, handshake):
                super().__init__()
                self._handshake = handshake

        def startElement(self, name, attrs):
                self._text = ''

        def endElement(self, name):
                self._handshake[name] = self._text

        def characters(self, content):
                self._text = self._text + content

class PlaylistsHandler(xml.sax.handler.ContentHandler):
        def __init__(self, playlists, user):
                super().__init__()
                self._playlists = playlists
                self._user = user

        def startElement(self, name, attrs):
                if name == 'playlist':
                        self._id = attrs['id']
                        self._name = ''
                        self._items = 0
                        self._owner = ''
                        self._type = ''
                self._text = ''

        def endElement(self, name):
                if name == 'playlist':
                        # only private playlists owned by this user, or public ones
                        if self._owner == self._user or self._type == 'public':
                                self._playlists.append([
                                        self._id,
                                        self._name,
                                        self._items])
                elif name == 'name':
                        self._name = self._text
                elif name == 'items' and self._text.isdigit():
                        self._items = int(self._text)
                elif name == 'owner':
                        self._owner = self._text
                elif name == 'type':
                        self._type = self._text

        def characters(self, content):
                self._text = self._text + content

class SongsHandler(xml.sax.handler.ContentHandler):
        def __init__(self, is_playlist, source, db, entry_type, albumart, auth, entries):
                super().__init__()
                self._is_playlist = is_playlist
                self._source = source
                self._db = db
                self._entry_type = entry_type
                self._albumart = albumart
                self._auth = auth
                self._entries = entries
                self._default()
                self._re_auth = re.compile('\\b(auth|ssid)=[a-fA-F0-9]*')

        def startElement(self, name, attrs):
                if name == 'song':
                        self._id = attrs['id']
                self._text = ''

        def endElement(self, name):
                # Process the song container unconditionally; only guard field elements
                # on self._text to avoid acting on empty/whitespace-only nodes.
                if name == 'song':
                        try:
                                if self._is_playlist:
                                        self._source.add_location(self._url, -1)
                                else:
                                        # add the track to the database if it doesn't exist
                                        entry = self._db.entry_lookup_by_location(self._url)
                                        if entry is None:
                                                entry = RB.RhythmDBEntry.new(
                                                        self._db, self._entry_type, self._url)
                                                self._entries.append(entry)

                                                if self._artist != '':
                                                        self._db.entry_set(entry, RB.RhythmDBPropType.ARTIST, self._artist)
                                                if self._album != '':
                                                        self._db.entry_set(entry, RB.RhythmDBPropType.ALBUM, self._album)
                                                if self._title != '':
                                                        self._db.entry_set(entry, RB.RhythmDBPropType.TITLE, self._title)
                                                if self._tag != '':
                                                        self._db.entry_set(entry, RB.RhythmDBPropType.GENRE, self._tag)
                                                if self._track != -1:
                                                        self._db.entry_set(entry, RB.RhythmDBPropType.TRACK_NUMBER, self._track)
                                                if self._year != -1:
                                                        self._db.entry_set(entry, RB.RhythmDBPropType.DATE, self._year)
                                                if self._time != -1:
                                                        self._db.entry_set(entry, RB.RhythmDBPropType.DURATION, self._time)
                                                if self._size != -1:
                                                        self._db.entry_set(entry, RB.RhythmDBPropType.FILE_SIZE, self._size)
                                                if self._rating != -1:
                                                        self._db.entry_set(entry, RB.RhythmDBPropType.RATING, self._rating)

                                                if self._art != '':
                                                        self._albumart[self._artist + self._album] = self._art

                        except Exception as e: # This happens on duplicate uris being added
                                sys.excepthook(*sys.exc_info())
                                print(f"Couldn't add {self._artist} - {self._title}", e)

                        self._default()

                elif self._text:
                        if name == 'url':
                                if self._auth: # replace ssid string with new auth string
                                        self._text = re.sub(self._re_auth, r'\1='+self._auth, self._text)
                                self._url = self._text
                        elif name == 'artist':
                                self._artist = self._text
                        elif name == 'album':
                                self._album = self._text
                        elif name == 'title':
                                self._title = self._text
                        elif name == 'tag':
                                self._tag = self._text
                        elif name == 'track' and self._text.isdigit():
                                self._track = int(self._text)
                        elif name == 'year' and self._text.isdigit():
                                if (GLib.Date.valid_year(int(self._text))):
                                        self._year = GLib.Date.new_dmy(1, 1, int(self._text)).get_julian()
                        elif name == 'time' and self._text.isdigit():
                                self._time = int(self._text)
                        elif name == 'size' and self._text.isdigit():
                                self._size = int(self._text)
                        elif name == 'rating' and self._text.isdigit():
                                self._rating = int(self._text)
                        elif name == 'art':
                                if self._auth: # replace auth string with new auth string
                                        self._text = re.sub(self._re_auth, 'auth='+self._auth, self._text)
                                self._art = self._text

        def characters(self, content):
                self._text = self._text + content

        def _default(self):
                self._id = -1
                self._url = ''
                self._artist = ''
                self._album = ''
                self._title = ''
                self._tag = ''
                self._track = -1
                self._year = -1
                self._time = -1
                self._size = -1
                self._rating = -1
                self._art = ''

class AmpachePlaylist(RB.StaticPlaylistSource):
        def __init__(self, **kwargs):
                super().__init__(**kwargs)

GObject.type_register(AmpachePlaylist)

class AmpacheBrowser(RB.BrowserSource):

        def __init__(self, **kwargs):
                super().__init__(**kwargs)

                self._limit = 500

                self._songs_cache = '_songs'
                self._cache_directory = os.path.join(
                        RB.user_cache_dir(),
                        'ampache')
                self._songs_cache_filename = os.path.join(
                        self._cache_directory,
                        f"{self._songs_cache}.xml")
                self._settings = Gio.Settings('org.gnome.rhythmbox.plugins.ampache')
                self._albumart = {}
                self._playlists = collections.deque()
                self._caches = collections.deque()
                self._playlist_sources = []
                self._entries = []
                self._cancellables = []
                self._session = Soup.Session(max_conns_per_host=20)

                self._text = None
                self._busy = False

                self._activated = False

                # add action RefetchAmpache and assign callback refetch_ampache
                app = Gio.Application.get_default()
                action = Gio.SimpleAction(name='refetch-ampache')
                action.connect('activate', self.refetch_ampache)
                app.add_action(action)

        def update(self, force_download):

                # Reset the playlist queue for this update cycle.  Without this,
                # any un-consumed entry left from a cache-load path (which never
                # calls download_iterate) would accumulate across calls and cause
                # songs to be fetched multiple times.
                self._playlists = collections.deque([[0, self._songs_cache]])

                ### download songs from Ampache server

                def download_songs(uri, items, is_playlist, source, cache_filename, playlist_name):

                        if items <= 0:
                                self._text = None
                                self._busy = False
                                self.notify_status_changed()
                                download_iterate()
                                return

                        # Calculate all chunk offsets up front so we can fire
                        # all requests simultaneously rather than sequentially.
                        offsets = list(range(0, items, self._limit))
                        num_chunks = len(offsets)
                        chunk_contents = [None] * num_chunks
                        remaining = num_chunks
                        songs_loaded = 0
                        aborted = False

                        self._text = f'Fetching {playlist_name}... (0 / {items} songs)'
                        self._busy = True
                        self.notify_status_changed()

                        def songs_downloaded_cb(session_obj, result, chunk_index):
                                nonlocal aborted, remaining, songs_loaded
                                # Always call finish() to free the GLib result, even
                                # when cancelled or when we intend to discard the data.
                                try:
                                        contents = session_obj.send_and_read_finish(result).get_data()
                                except Exception as e:
                                        if self._activated and not aborted:
                                                aborted = True
                                                edlg = Gtk.MessageDialog(
                                                        message_type=Gtk.MessageType.ERROR,
                                                        buttons=Gtk.ButtonsType.OK,
                                                        text=_('Songs response: %s') % e)
                                                edlg.run()
                                                edlg.destroy()
                                                self._activated = False
                                                self._text = None
                                                self._busy = False
                                                self.notify_status_changed()
                                        return
                                if aborted or not self._activated:
                                        return

                                print(f"parse chunk {playlist_name}[{offsets[chunk_index]}]...")
                                # instantiate songs parser and parse XML
                                parser = xml.sax.make_parser()
                                parser.setContentHandler(SongsHandler(
                                        is_playlist,
                                        source,
                                        self._db,
                                        self._entry_type,
                                        self._albumart,
                                        self._handshake_auth,
                                        self._entries))
                                try:
                                        parser.feed(contents)
                                except xml.sax.SAXParseException as e:
                                        print(f"error parsing songs: {e}: "
                                                f"{contents.decode('utf-8').splitlines()[e.getLineNumber()]}")

                                # Commit and update the UI after each chunk so songs
                                # appear progressively rather than all at once.
                                if not is_playlist:
                                        self._db.commit()
                                songs_loaded += min(self._limit, items - offsets[chunk_index])
                                self._text = f'Fetching {playlist_name}... ({min(songs_loaded, items)} / {items} songs)'
                                self.notify_status_changed()

                                chunk_contents[chunk_index] = contents
                                remaining -= 1

                                if remaining == 0:
                                        all_chunks_done()

                        def all_chunks_done():

                                # Remove enveloping <?xml> and <root> tags from intermediate
                                # chunks as needed to reassemble all chunks into one full .xml.
                                lines = []
                                for i, contents in enumerate(chunk_contents):
                                        chunk_lines = contents.decode('utf-8').splitlines(True)
                                        if i > 0:
                                                del chunk_lines[:2]   # strip <?xml?> and opening root tag
                                        if i < num_chunks - 1:
                                                del chunk_lines[-2:]  # strip closing root tag
                                        lines.extend(chunk_lines)

                                try:
                                        with open(cache_filename, 'wb') as f:
                                                f.write(''.join(lines).encode('utf-8'))
                                        newest_time = int(time.mktime(self._handshake_newest.timetuple()))
                                        # change modification time to newest time
                                        os.utime(cache_filename, (newest_time, newest_time))
                                        print(f"wrote cache file: {cache_filename}")
                                except Exception as e:
                                        print(f"error writing cache {cache_filename}: {e}")

                                self._text = None
                                self._busy = False
                                self.notify_status_changed()
                                download_iterate()

                        # Fire all chunk requests in parallel via Soup so the
                        # per-host connection limit applies to our session, not GIO's.
                        for i, offset in enumerate(offsets):
                                chunk_uri = f"{uri}&offset={offset}&limit={self._limit}"
                                cancel = Gio.Cancellable()
                                self._cancellables.append(cancel)
                                self._session.send_and_read_async(
                                        Soup.Message.new('GET', chunk_uri),
                                        GLib.PRIORITY_DEFAULT,
                                        cancel,
                                        songs_downloaded_cb,
                                        i)
                                print(f"download {playlist_name}[{offset}]: {chunk_uri}")

                def download_iterate():
                        try:
                                if len(self._playlists) > 0:
                                        playlist = self._playlists.popleft()
                                        print(f'process playlist: {playlist[1]}')
                                        if playlist[0] == 0:
                                                download_songs(
                                                        f"{self._settings['url']}/server/xml.server.php"
                                                        f"?action=songs&auth={self._handshake_auth}",
                                                        self._handshake_songs,
                                                        False,
                                                        self,
                                                        self._songs_cache_filename,
                                                        playlist[1])
                                        else:
                                                # create AmpachePlaylist source
                                                playlist_source = GObject.new(
                                                        AmpachePlaylist,
                                                        is_local=False,
                                                        shell=self._shell,
                                                        entry_type=self._entry_type,
                                                        name=_(playlist[1])
                                                )
                                                self._playlist_sources.append(playlist_source)

                                                # insert AmpachePlaylist source into AmpacheBrowser source
                                                self._shell.append_display_page(playlist_source, self)

                                                download_songs(
                                                        f"{self._settings['url']}/server/xml.server.php"
                                                        f"?action=playlist_songs&filter={playlist[0]}"
                                                        f"&auth={self._handshake_auth}",
                                                        playlist[2],
                                                        True,
                                                        playlist_source,
                                                        os.path.join(
                                                                self._cache_directory,
                                                                f"{playlist[1]}.xml"),
                                                        playlist[1])

                                else:
                                        print('no more playlists to process, refilter display page model')
                                        self._shell.props.display_page_model.refilter()

                        except Exception as e:
                                print(f'Exception: {e}')
                                return


                def playlists_cb(session_obj, result, param):
                        try:
                                contents = session_obj.send_and_read_finish(result).get_data()
                        except Exception as e:
                                if self._activated:
                                        edlg = Gtk.MessageDialog(
                                                message_type=Gtk.MessageType.ERROR,
                                                buttons=Gtk.ButtonsType.OK,
                                                text=_('Playlists response: %s') % e)
                                        edlg.run()
                                        edlg.destroy()
                                        self._activated = False
                                return
                        if not self._activated:
                                return

                        if len(contents) <= 0:
                                edlg = Gtk.MessageDialog(
                                        message_type=Gtk.MessageType.ERROR,
                                        buttons=Gtk.ButtonsType.OK,
                                        text=_("Playlists response size: 0\nCheck ampache server logs for cause."))
                                edlg.run()
                                edlg.destroy()
                                self._activated = False

                                self._text = ''
                                self.notify_status_changed()
                                return

                        # instantiate playlists parser
                        parser = xml.sax.make_parser()
                        parser.setContentHandler(PlaylistsHandler(
                                self._playlists,
                                self._settings['username']))

                        try:
                                parser.feed(contents)
                        except xml.sax.SAXParseException as e:
                                print(f"error parsing playlists: {e}")

                        download_iterate()

                ### load songs from cache

                def load_songs(filename, is_playlist, source):
                        def songs_loaded_cb(file, result, data):
                                try:
                                        (ok, contents, etag) = file.load_contents_finish(result)
                                except Exception as e:
                                        if self._activated:
                                                RB.error_dialog(
                                                        title=_("Unable to load songs"),
                                                        message=_("Rhythmbox could not load the Ampache songs."))
                                        return
                                if not self._activated:
                                        return

                                try:
                                        # instantiate songs parser
                                        parser = xml.sax.make_parser()
                                        parser.setContentHandler(
                                                SongsHandler(
                                                        is_playlist,
                                                        source,
                                                        self._db,
                                                        self._entry_type,
                                                        self._albumart,
                                                        self._handshake_auth,
                                                        self._entries))

                                        parser.feed(contents)
                                except xml.sax.SAXParseException as e:
                                        print(f"error parsing songs: {e}")

                                # Commit all DB writes for this cache file in one batch
                                if not is_playlist:
                                        self._db.commit()

                                self._text = None
                                self._busy = False
                                self.notify_status_changed()

                                # load next cache
                                load_iterate()

                        self._text = f'Load from cache "{filename}"...'
                        self._busy = True
                        self.notify_status_changed()

                        cancel = Gio.Cancellable()
                        self._cancellables.append(cancel)
                        Gio.file_new_for_path(filename).load_contents_async(
                                cancel,
                                songs_loaded_cb,
                                None)

                def load_iterate():
                        try:
                                cache = self._caches.popleft()

                                print(f'process playlist: {cache}')

                                if cache == self._songs_cache:
                                        load_songs(
                                                self._songs_cache_filename,
                                                False,
                                                self)
                                else:
                                        # create AmpachePlaylist source
                                        playlist_source = GObject.new(
                                                AmpachePlaylist,
                                                is_local=False,
                                                shell=self._shell,
                                                entry_type=self._entry_type,
                                                name=_(cache)
                                        )
                                        self._playlist_sources.append(playlist_source)

                                        # insert AmpachePlaylist source into AmpacheBrowser source
                                        self._shell.append_display_page(playlist_source, self)

                                        load_songs(
                                                os.path.join(
                                                        self._cache_directory,
                                                        f"{cache}.xml"),
                                                True,
                                                playlist_source)

                        except Exception as e:
                                print('no more playlists to process, refilter display page model')
                                self._shell.props.display_page_model.refilter()
                                return

                def enumerate_cache_files():
                        self._caches = collections.deque()
                        for filename in os.listdir(
                                os.path.join(RB.user_cache_dir(), 'ampache')):
                                name = os.path.splitext(filename)[0]
                                if name == self._songs_cache:
                                        self._caches.appendleft(name)
                                else:
                                        self._caches.append(name)

                        print(f'caches: {self._caches}')

                        # start processing first cache
                        load_iterate()

                def handshake_cb(session_obj, result, parser):
                        try:
                                contents = session_obj.send_and_read_finish(result).get_data()
                        except Exception as e:
                                if self._activated:
                                        edlg = Gtk.MessageDialog(
                                                message_type=Gtk.MessageType.ERROR,
                                                buttons=Gtk.ButtonsType.OK,
                                                text=_('Handshake response: %s') % e)
                                        edlg.run()
                                        edlg.destroy()
                                        self._activated = False
                                return
                        if not self._activated:
                                return

                        if len(contents) <= 0:
                                edlg = Gtk.MessageDialog(
                                        message_type=Gtk.MessageType.ERROR,
                                        buttons=Gtk.ButtonsType.OK,
                                        text=_("Handshake response size: 0\nCheck ampache server logs for cause."))
                                edlg.run()
                                edlg.destroy()
                                self._activated = False

                                self._text = ''
                                self.notify_status_changed()
                                return

                        try:
                                parser.feed(contents)
                        except xml.sax.SAXParseException as e:
                                print(f"error parsing handshake: {e}")

                        # convert handshake update time into datetime
                        handshake_update = datetime.strptime(
                                handshake['update'][0:18],
                                '%Y-%m-%dT%H:%M:%S')
                        self._handshake_newest = handshake_update
                        handshake_add = datetime.strptime(
                                handshake['add'][0:18],
                                '%Y-%m-%dT%H:%M:%S')
                        if handshake_add > self._handshake_newest:
                                self._handshake_newest = handshake_add
                        handshake_clean = datetime.strptime(
                                handshake['clean'][0:18],
                                '%Y-%m-%dT%H:%M:%S')
                        if handshake_clean > self._handshake_newest:
                                self._handshake_newest = handshake_clean

                        self._handshake_auth = handshake['auth']
                        self._handshake_songs = int(handshake['songs'])

                        # cache file mtime >= handshake newest time: load cached
                        if not force_download and \
                                os.path.exists(self._songs_cache_filename) and \
                                datetime.fromtimestamp(os.path.getmtime(
                                self._songs_cache_filename)) >= \
                                self._handshake_newest:
                                enumerate_cache_files()
                        else:
                                # delete all cache files
                                for filename in os.listdir(self._cache_directory):
                                        abs_filename = os.path.join(
                                                self._cache_directory,
                                                filename)
                                        try:
                                                if os.path.isfile(abs_filename):
                                                        print(f"remove cache file: {abs_filename}")
                                                        os.unlink(abs_filename)
                                        except Exception as e:
                                                print(e)

                                # download playlists
                                ampache_server_uri = (
                                        f"{self._settings['url']}/server/xml.server.php"
                                        f"?action=playlists&auth={self._handshake_auth}")
                                cancel = Gio.Cancellable()
                                self._cancellables.append(cancel)
                                self._session.send_and_read_async(
                                        Soup.Message.new('GET', ampache_server_uri),
                                        GLib.PRIORITY_DEFAULT,
                                        cancel,
                                        playlists_cb,
                                        None)
                                print(f"downloading playlists: {ampache_server_uri}")

                # check for errors
                if not self._settings['url']:
                        edlg = Gtk.MessageDialog(
                                message_type=Gtk.MessageType.ERROR,
                                buttons=Gtk.ButtonsType.OK,
                                text=_('URL missing'))
                        edlg.run()
                        edlg.destroy()
                        self._activated = False
                        return

                if not self._settings['password']:
                        edlg = Gtk.MessageDialog(
                                message_type=Gtk.MessageType.ERROR,
                                buttons=Gtk.ButtonsType.OK,
                                text=_('Password missing'))
                        edlg.run()
                        edlg.destroy()
                        self._activated = False
                        return

                self._text = 'Update songs...'
                self.notify_status_changed()

                handshake = {}

                # instantiate handshake parser
                parser = xml.sax.make_parser()
                parser.setContentHandler(HandshakeHandler(handshake))

                # build handshake url
                if self._settings['username'] != '':
                        # username/password provided
                        timestamp = int(time.time())
                        password = hashlib.sha256(self._settings['password'].encode('utf-8')).hexdigest()
                        authkey = hashlib.sha256((str(timestamp) + password).encode('utf-8')).hexdigest()

                        ampache_server_uri = (
                                f"{self._settings['url']}/server/xml.server.php"
                                f"?action=handshake&auth={authkey}&timestamp={timestamp}"
                                f"&user={self._settings['username']}&version=350001")
                else:
                        # api key provided
                        ampache_server_uri = (
                                f"{self._settings['url']}/server/xml.server.php"
                                f"?action=handshake&auth={self._settings['password']}&version=350001")

                # execute handshake
                cancel = Gio.Cancellable()
                self._cancellables.append(cancel)
                self._session.send_and_read_async(
                        Soup.Message.new('GET', ampache_server_uri),
                        GLib.PRIORITY_DEFAULT,
                        cancel,
                        handshake_cb,
                        parser)
                print(f"downloading handshake: {ampache_server_uri}")

        # Source is activated
        def do_activate(self):
                if not self._activated:
                        self._activated = True

                        self._shell = self.props.shell
                        self._db = self._shell.props.db
                        self._entry_type = self.props.entry_type

                        self._art_store = RB.ExtDB(name="album-art")
                        self._art_request = self._art_store.connect("request", self._album_art_requested)

                        # create cache directory if it doesn't exist
                        cache_path = os.path.dirname(self._songs_cache_filename)
                        if not os.path.exists(cache_path):
                                os.mkdir(cache_path, 0o700)

                        self.update(False)

        # Shortcut for single click
        def do_selected(self):
                self.do_activate()

        def _album_art_requested(self, store, key, last_time):
                artist = key.get_field('artist')
                album = key.get_field('album')
                uri = self._albumart.get(artist + album)
                print(f'album art uri: {uri}')
                if uri:
                        storekey = RB.ExtDBKey.create_storage('album', album)
                        storekey.add_field('artist', artist)
                        store.store_uri(storekey, RB.ExtDBSourceType.SEARCH, uri)

        def do_get_status(self, text, busy):
                return (self._text, self._busy)

        def clean_db(self):
                # remove playlists
                for playlist_source in self._playlist_sources:
                        # delete Playlist source
                        playlist_source.delete_thyself()
                self._playlist_sources = []
                self._entries = []

                self._db.entry_delete_by_type(self._entry_type)
                self._db.commit()

        def refetch_ampache(self, parameter, user_data):
                self.clean_db()
                self.update(True)

        def do_delete_thyself(self):

                if self._activated:
                        self._activated = False

                        # Cancel all pending async operations so their callbacks
                        # see the False flag and return without touching GObjects.
                        for cancel in self._cancellables:
                                cancel.cancel()
                        self._cancellables = []

                        if self._art_store is not None:
                                self._art_store.disconnect(self._art_request)
                                self._art_store = None

                        # Drop references.  Do NOT call playlist_source.delete_thyself()
                        # here — Rhythmbox removes child display pages automatically when
                        # the parent is deleted; doing it ourselves causes a double-free.
                        #
                        # Do NOT call entry_delete_by_type here either.  It emits
                        # entry-deleted signals that Rhythmbox's rb_entry_view tries to
                        # handle, but the entry view inside RBBrowserSource is already
                        # invalid by the time do_delete_thyself is called, producing a
                        # SIGSEGV in rb_entry_view_have_selection.  Since entries are
                        # registered with save_to_disk=False they never persist to disk,
                        # so skipping this call is safe on exit.
                        self._playlist_sources = []
                        self._entries = []

                RB.BrowserSource.do_delete_thyself(self)

GObject.type_register(AmpacheBrowser)
