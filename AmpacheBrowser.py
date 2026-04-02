# -*- Mode: python; coding: utf-8; tab-width: 4; indent-tabs-mode: nil; -*-
# vim: expandtab shiftwidth=4 softtabstop=4 tabstop=4
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
import sqlite3
import collections

import xml.sax
import xml.sax.handler

# ---------------------------------------------------------------------------
# SQLite schema and helpers
# ---------------------------------------------------------------------------

_CREATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS songs (
    url    TEXT PRIMARY KEY,
    artist TEXT NOT NULL DEFAULT '',
    album  TEXT NOT NULL DEFAULT '',
    title  TEXT NOT NULL DEFAULT '',
    tag    TEXT NOT NULL DEFAULT '',
    track  INTEGER NOT NULL DEFAULT -1,
    year   INTEGER NOT NULL DEFAULT -1,
    time   INTEGER NOT NULL DEFAULT -1,
    size   INTEGER NOT NULL DEFAULT -1,
    rating INTEGER NOT NULL DEFAULT -1,
    art    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS playlists (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist_songs (
    playlist_id TEXT NOT NULL,
    url         TEXT NOT NULL
);
"""

_INSERT_SONG_SQL = """
INSERT OR REPLACE INTO songs
    (url, artist, album, title, tag, track, year, time, size, rating, art)
VALUES
    (:url, :artist, :album, :title, :tag, :track, :year, :time, :size, :rating, :art)
"""

_INSERT_PLAYLIST_SQL = \
    "INSERT OR REPLACE INTO playlists (id, name) VALUES (?, ?)"

_INSERT_PLAYLIST_SONG_SQL = \
    "INSERT INTO playlist_songs (playlist_id, url) VALUES (?, ?)"


def _open_db(path):
    """Open (or create) the SQLite cache database and return a connection."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_CREATE_SCHEMA_SQL)
    conn.commit()
    return conn


def songs_to_rhythmdb(songs, albumart, db, entry_type, is_playlist, source, entries):
    """Write a list of song dicts into RhythmDB (or a playlist source)."""
    for song in songs:
        try:
            if is_playlist:
                source.add_location(song['url'], -1)
            else:
                # add the track to the database if it doesn't exist
                entry = db.entry_lookup_by_location(song['url'])
                if entry is None:
                    entry = RB.RhythmDBEntry.new(db, entry_type, song['url'])
                    entries.append(entry)

                    if song['artist']:
                        db.entry_set(entry, RB.RhythmDBPropType.ARTIST, song['artist'])
                    if song['album']:
                        db.entry_set(entry, RB.RhythmDBPropType.ALBUM, song['album'])
                    if song['title']:
                        db.entry_set(entry, RB.RhythmDBPropType.TITLE, song['title'])
                    if song['tag']:
                        db.entry_set(entry, RB.RhythmDBPropType.GENRE, song['tag'])
                    if song['track'] != -1:
                        db.entry_set(entry, RB.RhythmDBPropType.TRACK_NUMBER, song['track'])
                    if song['year'] != -1:
                        julian = GLib.Date.new_dmy(1, 1, song['year']).get_julian()
                        db.entry_set(entry, RB.RhythmDBPropType.DATE, julian)
                    if song['time'] != -1:
                        db.entry_set(entry, RB.RhythmDBPropType.DURATION, song['time'])
                    if song['size'] != -1:
                        db.entry_set(entry, RB.RhythmDBPropType.FILE_SIZE, song['size'])
                    if song['rating'] != -1:
                        db.entry_set(entry, RB.RhythmDBPropType.RATING, song['rating'])

                    if song['art']:
                        albumart[song['artist'] + song['album']] = song['art']

        except Exception as e:  # This happens on duplicate uris being added
            sys.excepthook(*sys.exc_info())
            print(f"Couldn't add {song['artist']} - {song['title']}", e)


# ---------------------------------------------------------------------------
# SAX handlers (used to parse server responses during download)
# ---------------------------------------------------------------------------

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
    """Parse an Ampache songs XML response into a list of song dicts.

    After parsing, self.songs is a list of dicts with keys matching the
    songs table columns, and self.albumart maps artist+album to art URL.
    Year is stored as a raw integer (not Julian); conversion happens in
    songs_to_rhythmdb() when writing to RhythmDB.
    """

    def __init__(self, auth):
        super().__init__()
        self._auth = auth
        self._re_auth = re.compile(r'\b(auth|ssid)=[a-fA-F0-9]*')
        self.songs = []
        self.albumart = {}
        self._default()

    def startElement(self, name, attrs):
        if name == 'song':
            self._id = attrs['id']
        self._text = ''

    def endElement(self, name):
        # Process the song container unconditionally; only guard field elements
        # on self._text to avoid acting on empty/whitespace-only nodes.
        if name == 'song':
            if self._url:
                self.songs.append({
                    'url':    self._url,
                    'artist': self._artist,
                    'album':  self._album,
                    'title':  self._title,
                    'tag':    self._tag,
                    'track':  self._track,
                    'year':   self._year,
                    'time':   self._time,
                    'size':   self._size,
                    'rating': self._rating,
                    'art':    self._art,
                })
                if self._art:
                    self.albumart[self._artist + self._album] = self._art
            self._default()

        elif self._text:
            if name == 'url':
                if self._auth:  # replace ssid/auth string with new auth string
                    self._text = re.sub(self._re_auth, r'\1=' + self._auth, self._text)
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
                year = int(self._text)
                if 1 <= year <= 9999:
                    self._year = year
            elif name == 'time' and self._text.isdigit():
                self._time = int(self._text)
            elif name == 'size' and self._text.isdigit():
                self._size = int(self._text)
            elif name == 'rating' and self._text.isdigit():
                self._rating = int(self._text)
            elif name == 'art':
                if self._auth:  # replace auth string with new auth string
                    self._text = re.sub(self._re_auth, 'auth=' + self._auth, self._text)
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


# ---------------------------------------------------------------------------
# Rhythmbox source classes
# ---------------------------------------------------------------------------

class AmpachePlaylist(RB.StaticPlaylistSource):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

GObject.type_register(AmpachePlaylist)

class AmpacheBrowser(RB.BrowserSource):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self._limit = 500

        self._cache_directory = os.path.join(RB.user_cache_dir(), 'ampache')
        self._db_filename = os.path.join(self._cache_directory, '_ampache.sqlite')
        self._settings = Gio.Settings('org.gnome.rhythmbox.plugins.ampache')
        self._albumart = {}
        self._playlists = collections.deque()
        self._playlist_sources = []
        self._entries = []
        self._cancellables = set()
        self._session = Soup.Session(max_conns_per_host=20)

        self._shell = None
        self._db = None
        self._entry_type = None
        self._art_store = None
        self._art_request = None
        self._handshake_auth = None
        self._handshake_newest = None
        self._handshake_songs = None

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
        # any un-consumed entry left from a previous cycle would accumulate
        # across calls and cause songs to be fetched multiple times.
        self._playlists = collections.deque([[0, 'library']])

        ### download songs from Ampache server

        # conn is opened in playlists_cb (when we know a download is needed)
        # and closed in download_iterate (when the queue is exhausted).
        conn = None

        def download_songs(uri, items, is_playlist, source, playlist_id, playlist_name):

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
            remaining = num_chunks
            songs_loaded = 0
            aborted = False

            self._text = f'Fetching {playlist_name}... (0 / {items} songs)'
            self._busy = True
            self.notify_status_changed()

            def songs_downloaded_cb(session_obj, result, user_data):
                nonlocal aborted, remaining, songs_loaded
                cancel, chunk_index = user_data
                self._cancellables.discard(cancel)
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
                handler = SongsHandler(self._handshake_auth)
                parser = xml.sax.make_parser()
                parser.setContentHandler(handler)
                try:
                    parser.feed(contents)
                except xml.sax.SAXParseException as e:
                    print(f"error parsing songs: {e}: "
                        f"{contents.decode('utf-8').splitlines()[e.getLineNumber()]}")

                # Write parsed songs to RhythmDB
                songs_to_rhythmdb(
                    handler.songs, self._albumart,
                    self._db, self._entry_type,
                    is_playlist, source, self._entries)
                if not is_playlist:
                    self._db.commit()
                self._albumart.update(handler.albumart)

                # Write parsed songs to SQLite cache
                if not is_playlist:
                    conn.executemany(_INSERT_SONG_SQL, handler.songs)
                else:
                    conn.executemany(
                        _INSERT_PLAYLIST_SONG_SQL,
                        [(playlist_id, song['url']) for song in handler.songs])
                conn.commit()

                songs_loaded += min(self._limit, items - offsets[chunk_index])
                self._text = f'Fetching {playlist_name}... ({min(songs_loaded, items)} / {items} songs)'
                self.notify_status_changed()

                remaining -= 1
                if remaining == 0:
                    self._text = None
                    self._busy = False
                    self.notify_status_changed()
                    download_iterate()

            # Fire all chunk requests in parallel via Soup so the
            # per-host connection limit applies to our session, not GIO's.
            for i, offset in enumerate(offsets):
                chunk_uri = f"{uri}&offset={offset}&limit={self._limit}"
                cancel = Gio.Cancellable()
                self._cancellables.add(cancel)
                self._session.send_and_read_async(
                    Soup.Message.new('GET', chunk_uri),
                    GLib.PRIORITY_DEFAULT,
                    cancel,
                    songs_downloaded_cb,
                    (cancel, i))
                print(f"download {playlist_name}[{offset}]: {chunk_uri}")

        def download_iterate():
            nonlocal conn
            try:
                if self._playlists:
                    playlist = self._playlists.popleft()
                    print(f'process playlist: {playlist[1]}')
                    if playlist[0] == 0:
                        download_songs(
                            f"{self._settings['url']}/server/xml.server.php"
                            f"?action=songs&auth={self._handshake_auth}",
                            self._handshake_songs,
                            False,
                            self,
                            None,
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

                        # record this playlist in the cache before downloading its songs
                        conn.execute(_INSERT_PLAYLIST_SQL, (str(playlist[0]), playlist[1]))
                        conn.commit()

                        download_songs(
                            f"{self._settings['url']}/server/xml.server.php"
                            f"?action=playlist_songs&filter={playlist[0]}"
                            f"&auth={self._handshake_auth}",
                            playlist[2],
                            True,
                            playlist_source,
                            str(playlist[0]),
                            playlist[1])

                else:
                    # All playlists downloaded — seal the cache and finish.
                    newest_time = int(time.mktime(self._handshake_newest.timetuple()))
                    conn.close()
                    conn = None
                    # change modification time to newest time
                    os.utime(self._db_filename, (newest_time, newest_time))
                    print(f"wrote cache db: {self._db_filename}")
                    print('no more playlists to process, refilter display page model')
                    self._shell.props.display_page_model.refilter()

            except Exception as e:
                print(f'Exception: {e}')
                return


        def playlists_cb(session_obj, result, cancel):
            nonlocal conn
            self._cancellables.discard(cancel)
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

            if not contents:
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

            # Open the cache database now that we know a download is needed.
            conn = _open_db(self._db_filename)

            download_iterate()

        ### load library from SQLite cache

        def load_from_cache():
            self._text = 'Loading from cache...'
            self._busy = True
            self.notify_status_changed()

            try:
                db_conn = _open_db(self._db_filename)

                # Load main song library
                songs = [dict(row) for row in db_conn.execute('SELECT * FROM songs')]
                songs_to_rhythmdb(
                    songs, self._albumart,
                    self._db, self._entry_type,
                    False, self, self._entries)
                self._db.commit()

                # Load playlists
                playlists = [dict(row) for row in db_conn.execute('SELECT * FROM playlists')]
                for playlist in playlists:
                    # create AmpachePlaylist source
                    playlist_source = GObject.new(
                        AmpachePlaylist,
                        is_local=False,
                        shell=self._shell,
                        entry_type=self._entry_type,
                        name=_(playlist['name'])
                    )
                    self._playlist_sources.append(playlist_source)

                    # insert AmpachePlaylist source into AmpacheBrowser source
                    self._shell.append_display_page(playlist_source, self)

                    urls = [row[0] for row in db_conn.execute(
                        'SELECT url FROM playlist_songs WHERE playlist_id = ?',
                        (playlist['id'],))]
                    for url in urls:
                        playlist_source.add_location(url, -1)

                db_conn.close()

            except Exception as e:
                print(f'error loading from cache: {e}')

            self._text = None
            self._busy = False
            self.notify_status_changed()
            self._shell.props.display_page_model.refilter()

        def handshake_cb(session_obj, result, user_data):
            cancel, parser = user_data
            self._cancellables.discard(cancel)
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

            if not contents:
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

            # cache db mtime >= handshake newest time: load cached
            if not force_download and \
                os.path.exists(self._db_filename) and \
                datetime.fromtimestamp(os.path.getmtime(
                self._db_filename)) >= \
                self._handshake_newest:
                load_from_cache()
            else:
                # delete the old cache database
                try:
                    if os.path.exists(self._db_filename):
                        print(f"remove cache db: {self._db_filename}")
                        os.unlink(self._db_filename)
                except Exception as e:
                    print(e)

                # download playlists
                ampache_server_uri = (
                    f"{self._settings['url']}/server/xml.server.php"
                    f"?action=playlists&auth={self._handshake_auth}")
                cancel = Gio.Cancellable()
                self._cancellables.add(cancel)
                self._session.send_and_read_async(
                    Soup.Message.new('GET', ampache_server_uri),
                    GLib.PRIORITY_DEFAULT,
                    cancel,
                    playlists_cb,
                    cancel)
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
        self._cancellables.add(cancel)
        self._session.send_and_read_async(
            Soup.Message.new('GET', ampache_server_uri),
            GLib.PRIORITY_DEFAULT,
            cancel,
            handshake_cb,
            (cancel, parser))
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
            if not os.path.exists(self._cache_directory):
                os.mkdir(self._cache_directory, 0o700)

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
            self._cancellables = set()

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
