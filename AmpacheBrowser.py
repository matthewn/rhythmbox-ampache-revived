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

import collections
import faulthandler
import hashlib
import os
import re
import sqlite3
import time
import traceback
import xml.sax
import xml.sax.handler
from datetime import datetime

import gi
gi.require_version('Soup', '3.0')
from gi.repository import GLib, GObject, Gtk, Gio, Soup  # noqa: E402
from gi.repository import RB  # noqa: E402

faulthandler.enable()

# _ is injected as a builtin by Rhythmbox's plugin loader at runtime.
# This stub satisfies static analysers and degrades gracefully elsewhere.
_ = str

# Compiled once at import time; used by SongsHandler to rewrite auth tokens.
_RE_AUTH = re.compile(r'\b(auth|ssid)=[a-fA-F0-9]*')

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
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
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


def _read_meta(conn, key):
    """Return the value stored under key in the meta table, or None."""
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _write_meta(conn, key, value):
    """Upsert a key/value pair in the meta table."""
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def songs_to_rhythmdb(songs, albumart, db, entry_type, is_playlist, source,
                      entries, update_existing=False):
    """Write a list of song dicts into RhythmDB (or a playlist source).

    When update_existing is True, metadata on already-known URLs is refreshed
    rather than skipped.  This is used by the incremental update path.
    """
    for song in songs:
        try:
            if is_playlist:
                source.add_location(song['url'], -1)
            else:
                entry = db.entry_lookup_by_location(song['url'])
                if entry is None:
                    entry = RB.RhythmDBEntry.new(db, entry_type, song['url'])
                    entries.append(entry)
                elif not update_existing:
                    continue

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
            traceback.print_exc()
            print(f"Couldn't add {song['artist']} - {song['title']}", e)


# ---------------------------------------------------------------------------
# SAX handlers (used to parse server responses during download)
# ---------------------------------------------------------------------------

class HandshakeHandler(xml.sax.handler.ContentHandler):
    def __init__(self, handshake):
        super().__init__()
        self._handshake = handshake
        self._text = ''

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
        self._id = ''
        self._name = ''
        self._items = 0
        self._owner = ''
        self._type = ''
        self._text = ''

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
                    self._text = re.sub(_RE_AUTH, r'\1=' + self._auth, self._text)
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
                if self._auth:
                    # Art URLs only use auth=, not ssid=, so the replacement
                    # is intentionally hardcoded to 'auth=' rather than r'\1='.
                    self._text = re.sub(_RE_AUTH, 'auth=' + self._auth, self._text)
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

        self._limit = 750

        self._cache_directory = os.path.join(RB.user_cache_dir(), 'ampache')
        self._db_filename = os.path.join(self._cache_directory, '_ampache.sqlite')
        self._settings = Gio.Settings('org.gnome.rhythmbox.plugins.ampache')
        self._albumart = {}
        self._playlists = collections.deque()
        self._playlist_sources = {}   # {playlist_id: source}
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

        # download songs from Ampache server

        # conn is opened in playlists_cb (when we know a full download is needed)
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
                    try:
                        bad_line = contents.decode('utf-8').splitlines()[e.getLineNumber() - 1]
                    except (IndexError, UnicodeDecodeError):
                        bad_line = '<unavailable>'
                    print(f"error parsing songs: {e}: {bad_line}")

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
                        self._playlist_sources[str(playlist[0])] = playlist_source

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
                    # All playlists downloaded — write meta, seal the cache, and finish.
                    newest_time = int(time.mktime(self._handshake_newest.timetuple()))
                    _write_meta(conn, 'last_add', handshake['add'])
                    _write_meta(conn, 'last_update', handshake['update'])
                    _write_meta(conn, 'last_clean', handshake['clean'])
                    conn.commit()
                    conn.close()
                    conn = None
                    # change modification time to newest time
                    os.utime(self._db_filename, (newest_time, newest_time))
                    print(f"wrote cache db: {self._db_filename}")
                    print('no more playlists to process, refilter display page model')
                    self._shell.props.display_page_model.refilter()

            except Exception as e:
                traceback.print_exc()
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

        # load library from SQLite cache

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
                    self._playlist_sources[playlist['id']] = playlist_source

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

        # incremental update (add/update timestamps changed, clean unchanged)

        def incremental_update(new_add, new_update, new_clean, stored_add, stored_update):
            """Fetch only songs added/updated since the last sync, then diff playlists."""
            _conn = _open_db(self._db_filename)

            # Sequential chunk fetcher — used for both delta songs and playlist songs.
            # Fires one chunk at a time, stopping when the response is smaller than
            # the limit (no need to know the total count upfront).
            def fetch_songs_sequential(uri_base, on_done):
                offset = 0
                all_songs = []
                all_albumart = {}

                def fetch_one():
                    chunk_uri = f"{uri_base}&offset={offset}&limit={self._limit}"
                    cancel = Gio.Cancellable()
                    self._cancellables.add(cancel)
                    self._session.send_and_read_async(
                        Soup.Message.new('GET', chunk_uri),
                        GLib.PRIORITY_DEFAULT,
                        cancel, chunk_cb, cancel)

                def chunk_cb(session_obj, result, cancel):
                    nonlocal offset
                    self._cancellables.discard(cancel)
                    if not self._activated:
                        return
                    try:
                        contents = session_obj.send_and_read_finish(result).get_data()
                    except Exception as e:
                        print(f"incremental chunk error: {e}")
                        on_done(all_songs, all_albumart)
                        return

                    handler = SongsHandler(self._handshake_auth)
                    parser = xml.sax.make_parser()
                    parser.setContentHandler(handler)
                    try:
                        parser.feed(contents)
                    except xml.sax.SAXParseException as e:
                        print(f"error parsing incremental songs: {e}")

                    all_songs.extend(handler.songs)
                    all_albumart.update(handler.albumart)
                    offset += self._limit

                    if len(handler.songs) < self._limit:
                        on_done(all_songs, all_albumart)
                    else:
                        fetch_one()

                fetch_one()

            # Build the queue of delta-song fetches needed.
            base = f"{self._settings['url']}/server/xml.server.php"
            auth = self._handshake_auth
            fetch_queue = collections.deque()
            if new_add != stored_add:
                # Truncate to 19 chars (drop timezone suffix) for URL param
                fetch_queue.append((
                    f"{base}?action=songs&auth={auth}&add={stored_add[:19]}",
                    'new songs'))
            if new_update != stored_update:
                fetch_queue.append((
                    f"{base}?action=songs&auth={auth}&update={stored_update[:19]}",
                    'updated songs'))

            def run_next_delta():
                if not fetch_queue:
                    fetch_incremental_playlists()
                    return
                uri, label = fetch_queue.popleft()
                self._text = f'Fetching {label}...'
                self.notify_status_changed()
                print(f"incremental fetch: {uri}")

                def on_songs_done(songs, albumart):
                    print(f"incremental: {len(songs)} {label}")
                    songs_to_rhythmdb(
                        songs, self._albumart,
                        self._db, self._entry_type,
                        False, self, self._entries,
                        update_existing=True)
                    self._db.commit()
                    self._albumart.update(albumart)
                    _conn.executemany(_INSERT_SONG_SQL, songs)
                    _conn.commit()
                    run_next_delta()

                fetch_songs_sequential(uri, on_songs_done)

            def fetch_incremental_playlists():
                pl_uri = (f"{self._settings['url']}/server/xml.server.php"
                          f"?action=playlists&auth={self._handshake_auth}")
                cancel = Gio.Cancellable()
                self._cancellables.add(cancel)
                self._session.send_and_read_async(
                    Soup.Message.new('GET', pl_uri),
                    GLib.PRIORITY_DEFAULT,
                    cancel, playlists_fetched_cb, cancel)

            def playlists_fetched_cb(session_obj, result, cancel):
                self._cancellables.discard(cancel)
                if not self._activated:
                    return
                try:
                    contents = session_obj.send_and_read_finish(result).get_data()
                except Exception as e:
                    print(f"incremental playlists error: {e}")
                    finish_incremental()
                    return

                new_playlists_list = []
                parser = xml.sax.make_parser()
                parser.setContentHandler(PlaylistsHandler(
                    new_playlists_list, self._settings['username']))
                try:
                    parser.feed(contents)
                except xml.sax.SAXParseException as e:
                    print(f"error parsing incremental playlists: {e}")

                # id → [id, name, items]
                new_pl = {str(p[0]): p for p in new_playlists_list}
                stored_pl = {row['id']: row['name'] for row in
                             _conn.execute('SELECT id, name FROM playlists')}

                # Remove deleted playlists
                for pid in list(stored_pl.keys()):
                    if pid not in new_pl:
                        src = self._playlist_sources.pop(pid, None)
                        if src is not None:
                            src.delete_thyself()
                        _conn.execute('DELETE FROM playlists WHERE id = ?', (pid,))
                        _conn.execute(
                            'DELETE FROM playlist_songs WHERE playlist_id = ?', (pid,))

                # Determine which playlists need a song re-fetch (new or name-changed)
                to_fetch = collections.deque()
                for pid, pl in new_pl.items():
                    if pid not in stored_pl:
                        # New playlist — create source
                        pl_source = GObject.new(
                            AmpachePlaylist,
                            is_local=False,
                            shell=self._shell,
                            entry_type=self._entry_type,
                            name=_(pl[1]))
                        self._playlist_sources[pid] = pl_source
                        self._shell.append_display_page(pl_source, self)
                        _conn.execute(_INSERT_PLAYLIST_SQL, (pid, pl[1]))
                        to_fetch.append(pl)
                    elif stored_pl[pid] != pl[1]:
                        # Name changed — update stored name, clear and re-fetch songs
                        _conn.execute(_INSERT_PLAYLIST_SQL, (pid, pl[1]))
                        _conn.execute(
                            'DELETE FROM playlist_songs WHERE playlist_id = ?', (pid,))
                        to_fetch.append(pl)

                _conn.commit()

                def fetch_next_playlist():
                    if not to_fetch:
                        finish_incremental()
                        return
                    pl = to_fetch.popleft()
                    pid = str(pl[0])
                    source = self._playlist_sources.get(pid)
                    if source is None:
                        fetch_next_playlist()
                        return
                    pl_uri = (f"{self._settings['url']}/server/xml.server.php"
                              f"?action=playlist_songs&filter={pid}"
                              f"&auth={self._handshake_auth}")

                    def on_pl_songs_done(songs, albumart):
                        songs_to_rhythmdb(
                            songs, self._albumart,
                            self._db, self._entry_type,
                            True, source, self._entries)
                        _conn.executemany(
                            _INSERT_PLAYLIST_SONG_SQL,
                            [(pid, s['url']) for s in songs])
                        _conn.commit()
                        fetch_next_playlist()

                    fetch_songs_sequential(pl_uri, on_pl_songs_done)

                fetch_next_playlist()

            def finish_incremental():
                nonlocal _conn
                _write_meta(_conn, 'last_add', new_add)
                _write_meta(_conn, 'last_update', new_update)
                _write_meta(_conn, 'last_clean', new_clean)
                _conn.commit()
                newest_time = int(time.mktime(self._handshake_newest.timetuple()))
                _conn.close()
                _conn = None
                os.utime(self._db_filename, (newest_time, newest_time))
                print('incremental update complete')
                self._text = None
                self._busy = False
                self.notify_status_changed()
                self._shell.props.display_page_model.refilter()

            # Kick off: load existing cache into RhythmDB first so the
            # user sees their library immediately, then fetch the delta.
            load_from_cache()
            self._text = 'Checking for library updates...'
            self._busy = True
            self.notify_status_changed()
            run_next_delta()

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

            # find the most recent of the three server timestamps
            def _parse_ts(s):
                return datetime.strptime(s[0:19], '%Y-%m-%dT%H:%M:%S')
            self._handshake_newest = max(
                _parse_ts(handshake['update']),
                _parse_ts(handshake['add']),
                _parse_ts(handshake['clean']),
            )

            self._handshake_auth = handshake['auth']
            self._handshake_songs = int(handshake['songs'])

            new_add = handshake['add']
            new_update = handshake['update']
            new_clean = handshake['clean']

            # Read stored timestamps from the meta table (if the db exists)
            stored_add = stored_update = stored_clean = None
            if os.path.exists(self._db_filename):
                _meta_conn = _open_db(self._db_filename)
                stored_add = _read_meta(_meta_conn, 'last_add')
                stored_update = _read_meta(_meta_conn, 'last_update')
                stored_clean = _read_meta(_meta_conn, 'last_clean')
                _meta_conn.close()

            # Three-way decision:
            #   (a) full refetch  — clean changed, meta missing, or forced
            #   (b) incremental   — only add/update timestamps changed
            #   (c) load cache    — nothing changed
            needs_full = (
                force_download or
                not os.path.exists(self._db_filename) or
                stored_clean is None or stored_add is None or stored_update is None or
                new_clean != stored_clean
            )

            if needs_full:
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

            elif new_add == stored_add and new_update == stored_update:
                load_from_cache()

            else:
                incremental_update(new_add, new_update, new_clean,
                                   stored_add, stored_update)

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

        self._text = 'Checking for updates...'
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
                # 0o700: cache may contain auth tokens, so restrict to owner only
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
        for playlist_source in self._playlist_sources.values():
            # delete Playlist source
            playlist_source.delete_thyself()
        self._playlist_sources = {}
        self._entries = []

        self._db.entry_delete_by_type(self._entry_type)
        self._db.commit()

    def refetch_ampache(self, _parameter, _user_data):
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
            self._playlist_sources = {}
            self._entries = []

        RB.BrowserSource.do_delete_thyself(self)


GObject.type_register(AmpacheBrowser)
