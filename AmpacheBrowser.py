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
import xml.etree.ElementTree as ET
from datetime import datetime

import gi
gi.require_version('Soup', '3.0')
from gi.repository import GLib, GObject, Gtk, Gio, Soup  # noqa: E402
from gi.repository import RB  # noqa: E402

faulthandler.enable()

# _ is injected as a builtin by Rhythmbox's plugin loader at runtime.
# This stub satisfies static analysers and degrades gracefully elsewhere.
_ = str

# Used by parse_songs() to rewrite auth tokens in song and art URLs.
_RE_AUTH = re.compile(r'\b(auth|ssid)=[a-fA-F0-9]*')

# Ampache XML API path, appended to the server base URL.
_API_PATH = '/server/xml.server.php'

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


def _album_key(artist, album):
    return artist + album


def _show_error_dialog(message):
    dlg = Gtk.MessageDialog(
        message_type=Gtk.MessageType.ERROR,
        buttons=Gtk.ButtonsType.OK,
        text=message)
    dlg.run()
    dlg.destroy()


def songs_to_rhythmdb(songs, albumart, db, entry_type, is_playlist, source,
                      entries, update_existing=False, skip_lookup=False):
    """Write a list of song dicts into RhythmDB (or a playlist source).

    When update_existing is True, metadata on already-known URLs is refreshed
    rather than skipped.  This is used by the incremental update path.

    When skip_lookup is True, the per-URL entry_lookup_by_location() call is
    bypassed — callers use this to promise that RhythmDB has no entries of
    this entry_type yet, so the lookup would always return None.  If a URL
    already exists (another plugin, unexpected state), RhythmDBEntry.new()
    returns None and we skip the song.
    """
    for song in songs:
        try:
            if is_playlist:
                source.add_location(song['url'], -1)
                continue

            if skip_lookup:
                entry = RB.RhythmDBEntry.new(db, entry_type, song['url'])
                if entry is None:
                    continue
                entries.append(entry)
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
                albumart[_album_key(song['artist'], song['album'])] = song['art']

        except Exception as e:  # This happens on duplicate uris being added
            traceback.print_exc()
            print(f"Couldn't add {song['artist']} - {song['title']}", e)


# ---------------------------------------------------------------------------
# XML parsing (server responses)
# ---------------------------------------------------------------------------

Playlist = collections.namedtuple('Playlist', ['id', 'name', 'items'])


def parse_handshake(contents):
    """Parse a handshake XML response into a dict of child-tag → text."""
    return {el.tag: (el.text or '') for el in ET.fromstring(contents)}


def parse_playlists(contents, user):
    """Parse a playlists XML response into a list of Playlist namedtuples.

    Only returns playlists owned by `user` or explicitly public.
    """
    out = []
    for pl in ET.fromstring(contents).findall('playlist'):
        if pl.findtext('owner', '') != user and pl.findtext('type', '') != 'public':
            continue
        items = pl.findtext('items', '0')
        out.append(Playlist(
            pl.attrib['id'],
            pl.findtext('name', ''),
            int(items) if items.isdigit() else 0))
    return out


def parse_songs(contents, auth):
    """
    Parse a songs XML response into (songs, albumart).

    songs is a list of dicts matching the songs table columns; albumart
    maps artist+album to an art URL.  Year is stored as a raw integer;
    conversion to Julian happens in songs_to_rhythmdb() when writing to
    RhythmDB. If `auth` is truthy, url and art fields have their
    auth/ssid tokens rewritten to the new auth value.
    """
    def int_or_default(el, name, default=-1):
        t = el.findtext(name, '')
        return int(t) if t.isdigit() else default

    songs, albumart = [], {}
    for s in ET.fromstring(contents).findall('song'):
        url = s.findtext('url', '')
        if not url:
            continue
        if auth:
            url = _RE_AUTH.sub(r'\1=' + auth, url)
        art = s.findtext('art', '')
        if art and auth:
            # Art URLs only use auth=, not ssid=, so the replacement
            # is intentionally hardcoded to 'auth=' rather than r'\1='.
            art = _RE_AUTH.sub('auth=' + auth, art)
        year = int_or_default(s, 'year')
        if not (1 <= year <= 9999):
            year = -1
        artist = s.findtext('artist', '')
        album = s.findtext('album', '')
        songs.append({
            'url':    url,
            'artist': artist,
            'album':  album,
            'title':  s.findtext('title', ''),
            'tag':    s.findtext('tag', ''),
            'track':  int_or_default(s, 'track'),
            'year':   year,
            'time':   int_or_default(s, 'time'),
            'size':   int_or_default(s, 'size'),
            'rating': int_or_default(s, 'rating'),
            'art':    art,
        })
        if art:
            albumart[_album_key(artist, album)] = art
    return songs, albumart


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

    def _set_status(self, text, busy=None):
        self._text = text
        if busy is not None:
            self._busy = busy
        self.notify_status_changed()

    def _async_get(self, cancel, uri, callback, user_data):
        self._cancellables.add(cancel)
        self._session.send_and_read_async(
            Soup.Message.new('GET', uri),
            GLib.PRIORITY_DEFAULT, cancel, callback, user_data)

    def _create_playlist_source(self, pid, name):
        source = GObject.new(
            AmpachePlaylist,
            is_local=False,
            shell=self._shell,
            entry_type=self._entry_type,
            name=_(name))
        self._playlist_sources[str(pid)] = source
        self._shell.append_display_page(source, self)
        return source

    def update(self, force_download):

        # Reset the playlist queue for this update cycle.  Without this,
        # any un-consumed entry left from a previous cycle would accumulate
        # across calls and cause songs to be fetched multiple times.
        self._playlists = collections.deque([Playlist(0, 'library', 0)])

        # download songs from Ampache server

        # conn is opened in playlists_cb (when we know a full download is needed)
        # and closed in download_iterate (when the queue is exhausted).
        conn = None

        def download_songs(uri, items, is_playlist, source, playlist_id, playlist_name):

            if items <= 0:
                self._set_status(None, False)
                download_iterate()
                return

            # Calculate all chunk offsets up front so we can fire
            # all requests simultaneously rather than sequentially.
            offsets = list(range(0, items, self._limit))
            remaining = len(offsets)
            songs_loaded = 0
            aborted = False

            self._set_status(f'Fetching {playlist_name}... (0 / {items} songs)', True)

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
                        _show_error_dialog(_('Songs response: %s') % e)
                        self._activated = False
                        self._set_status(None, False)
                    return
                if aborted or not self._activated:
                    return

                print(f"parse chunk {playlist_name}[{offsets[chunk_index]}]...")
                try:
                    songs, albumart = parse_songs(contents, self._handshake_auth)
                except ET.ParseError as exc:
                    songs, albumart = [], {}
                    try:
                        bad_line = contents.decode('utf-8').splitlines()[exc.position[0] - 1]
                    except (IndexError, UnicodeDecodeError):
                        bad_line = '<unavailable>'
                    print(f"error parsing songs: {exc}: {bad_line}")

                # Write parsed songs to RhythmDB.  Full-refetch guarantees
                # a fresh entry_type in RhythmDB (clean_db preceded this
                # path), so entry_lookup_by_location is skippable.
                songs_to_rhythmdb(
                    songs, self._albumart,
                    self._db, self._entry_type,
                    is_playlist, source, self._entries,
                    skip_lookup=True)
                if not is_playlist:
                    self._db.commit()
                self._albumart.update(albumart)

                # Write parsed songs to SQLite cache
                if not is_playlist:
                    conn.executemany(_INSERT_SONG_SQL, songs)
                else:
                    conn.executemany(
                        _INSERT_PLAYLIST_SONG_SQL,
                        [(playlist_id, song['url']) for song in songs])
                conn.commit()

                songs_loaded += min(self._limit, items - offsets[chunk_index])
                self._set_status(
                    f'Fetching {playlist_name}... ({min(songs_loaded, items)} / {items} songs)')

                remaining -= 1
                if remaining == 0:
                    self._set_status(None, False)
                    download_iterate()

            # Fire all chunk requests in parallel via Soup so the
            # per-host connection limit applies to our session, not GIO's.
            for i, offset in enumerate(offsets):
                chunk_uri = f"{uri}&offset={offset}&limit={self._limit}"
                cancel = Gio.Cancellable()
                self._async_get(cancel, chunk_uri, songs_downloaded_cb, (cancel, i))
                print(f"download {playlist_name}[{offset}]: {chunk_uri}")

        def download_iterate():
            nonlocal conn
            try:
                if self._playlists:
                    playlist = self._playlists.popleft()
                    print(f'process playlist: {playlist.name}')
                    if playlist.id == 0:
                        download_songs(
                            f"{self._settings['url']}{_API_PATH}"
                            f"?action=songs&auth={self._handshake_auth}",
                            self._handshake_songs,
                            False, self, None, playlist.name)
                    else:
                        playlist_source = self._create_playlist_source(
                            playlist.id, playlist.name)
                        conn.execute(_INSERT_PLAYLIST_SQL,
                                     (str(playlist.id), playlist.name))
                        conn.commit()
                        download_songs(
                            f"{self._settings['url']}{_API_PATH}"
                            f"?action=playlist_songs&filter={playlist.id}"
                            f"&auth={self._handshake_auth}",
                            playlist.items, True, playlist_source,
                            str(playlist.id), playlist.name)

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
                    _show_error_dialog(_('Playlists response: %s') % e)
                    self._activated = False
                return
            if not self._activated:
                return

            if not contents:
                _show_error_dialog(
                    _("Playlists response size: 0\nCheck ampache server logs for cause."))
                self._activated = False
                self._set_status('')
                return

            try:
                self._playlists.extend(
                    parse_playlists(contents, self._settings['username']))
            except ET.ParseError as exc:
                print(f"error parsing playlists: {exc}")

            # Open the cache database now that we know a download is needed.
            conn = _open_db(self._db_filename)

            download_iterate()

        # load library from SQLite cache

        def load_from_cache():
            self._set_status('Loading from cache...', True)

            try:
                db_conn = _open_db(self._db_filename)

                # Load main song library
                songs = [dict(row) for row in db_conn.execute('SELECT * FROM songs')]
                songs_to_rhythmdb(
                    songs, self._albumart,
                    self._db, self._entry_type,
                    False, self, self._entries,
                    skip_lookup=True)
                self._db.commit()

                # Load playlists
                playlists = [dict(row) for row in db_conn.execute('SELECT * FROM playlists')]
                for playlist in playlists:
                    playlist_source = self._create_playlist_source(
                        playlist['id'], playlist['name'])
                    urls = [row[0] for row in db_conn.execute(
                        'SELECT url FROM playlist_songs WHERE playlist_id = ?',
                        (playlist['id'],))]
                    for url in urls:
                        playlist_source.add_location(url, -1)

                db_conn.close()

            except Exception as e:
                print(f'error loading from cache: {e}')

            self._set_status(None, False)
            self._shell.props.display_page_model.refilter()

        # incremental update (add or update timestamp changed)

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
                    self._async_get(cancel, chunk_uri, chunk_cb, cancel)

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

                    try:
                        songs, albumart = parse_songs(contents, self._handshake_auth)
                    except ET.ParseError as exc:
                        songs, albumart = [], {}
                        print(f"error parsing incremental songs: {exc}")

                    all_songs.extend(songs)
                    all_albumart.update(albumart)
                    offset += self._limit

                    if len(songs) < self._limit:
                        on_done(all_songs, all_albumart)
                    else:
                        fetch_one()

                fetch_one()

            # Build the queue of delta-song fetches needed.
            base = f"{self._settings['url']}{_API_PATH}"
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
                self._set_status(f'Fetching {label}...')
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
                cancel = Gio.Cancellable()
                self._async_get(
                    cancel,
                    f"{self._settings['url']}{_API_PATH}"
                    f"?action=playlists&auth={self._handshake_auth}",
                    playlists_fetched_cb, cancel)

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

                try:
                    new_playlists_list = parse_playlists(
                        contents, self._settings['username'])
                except ET.ParseError as exc:
                    new_playlists_list = []
                    print(f"error parsing incremental playlists: {exc}")

                # id → Playlist namedtuple
                new_pl = {p.id: p for p in new_playlists_list}
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
                        self._create_playlist_source(pid, pl.name)
                        _conn.execute(_INSERT_PLAYLIST_SQL, (pid, pl.name))
                        to_fetch.append(pl)
                    elif stored_pl[pid] != pl.name:
                        # Name changed — update stored name, clear and re-fetch songs
                        _conn.execute(_INSERT_PLAYLIST_SQL, (pid, pl.name))
                        _conn.execute(
                            'DELETE FROM playlist_songs WHERE playlist_id = ?', (pid,))
                        to_fetch.append(pl)

                _conn.commit()

                def fetch_next_playlist():
                    if not to_fetch:
                        finish_incremental()
                        return
                    pl = to_fetch.popleft()
                    source = self._playlist_sources.get(pl.id)
                    if source is None:
                        fetch_next_playlist()
                        return
                    pl_uri = (f"{self._settings['url']}{_API_PATH}"
                              f"?action=playlist_songs&filter={pl.id}"
                              f"&auth={self._handshake_auth}")

                    def on_pl_songs_done(songs, albumart):
                        songs_to_rhythmdb(
                            songs, self._albumart,
                            self._db, self._entry_type,
                            True, source, self._entries)
                        _conn.executemany(
                            _INSERT_PLAYLIST_SONG_SQL,
                            [(pl.id, s['url']) for s in songs])
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
                self._set_status(None, False)
                self._shell.props.display_page_model.refilter()

            # Cache was already loaded in update() before the handshake.
            self._set_status('Checking for library updates...', True)
            run_next_delta()

        def handshake_cb(session_obj, result, cancel):
            self._cancellables.discard(cancel)
            try:
                contents = session_obj.send_and_read_finish(result).get_data()
            except Exception as e:
                if self._activated:
                    _show_error_dialog(_('Handshake response: %s') % e)
                    self._activated = False
                return
            if not self._activated:
                return

            if not contents:
                _show_error_dialog(
                    _("Handshake response size: 0\nCheck ampache server logs for cause."))
                self._activated = False
                self._set_status('')
                return

            try:
                handshake.update(parse_handshake(contents))
            except ET.ParseError as exc:
                print(f"error parsing handshake: {exc}")

            # find the most recent of the three server timestamps
            self._handshake_newest = max(
                datetime.strptime(s[0:19], '%Y-%m-%dT%H:%M:%S')
                for s in (handshake['update'], handshake['add'], handshake['clean']))

            self._handshake_auth = handshake['auth']
            self._handshake_songs = int(handshake['songs'])

            new_add = handshake['add']
            new_update = handshake['update']
            new_clean = handshake['clean']

            # read cached data
            stored_add = stored_update = stored_clean = None
            stored_song_count = 0
            if os.path.exists(self._db_filename):
                _meta_conn = _open_db(self._db_filename)
                stored_add = _read_meta(_meta_conn, 'last_add')
                stored_update = _read_meta(_meta_conn, 'last_update')
                stored_clean = _read_meta(_meta_conn, 'last_clean')
                stored_song_count = _meta_conn.execute(
                    'SELECT COUNT(*) FROM songs').fetchone()[0]
                _meta_conn.close()

            clean_removed_songs = (
                new_clean != stored_clean and
                self._handshake_songs < stored_song_count
            )

            # Decide what (if anything) to fetch/re-fetch from the server:
            #   (a) full refetch  — clean actually removed songs, meta missing, or forced
            #   (b) incremental   — add or update timestamp changed
            #   (c) load cache    — add and update timestamps both unchanged
            needs_full = (
                force_download or
                not os.path.exists(self._db_filename) or
                stored_clean is None or stored_add is None or stored_update is None or
                clean_removed_songs
            )

            if needs_full:
                # If we pre-loaded the cache, wipe the stale entries — the
                # server has deleted songs and we don't know which URLs.
                if cache_loaded:
                    self.clean_db()

                # delete the old cache database
                try:
                    if os.path.exists(self._db_filename):
                        print(f"remove cache db: {self._db_filename}")
                        os.unlink(self._db_filename)
                except Exception as e:
                    print(e)

                # download playlists
                uri = (f"{self._settings['url']}{_API_PATH}"
                       f"?action=playlists&auth={self._handshake_auth}")
                cancel = Gio.Cancellable()
                self._async_get(cancel, uri, playlists_cb, cancel)
                print(f"downloading playlists: {uri}")

            elif new_add == stored_add and new_update == stored_update:
                # Cache already loaded at update() start; just clear status.
                self._set_status(None, False)

            else:
                incremental_update(new_add, new_update, new_clean,
                                   stored_add, stored_update)

        # check for errors
        if not self._settings['url']:
            _show_error_dialog(_('URL missing'))
            self._activated = False
            return

        if not self._settings['password']:
            _show_error_dialog(_('Password missing'))
            self._activated = False
            return

        # Show cached library immediately so the user isn't blocked on the
        # handshake round-trip. handshake_cb then reconciles: no-op if
        # nothing changed, delta fetch if add/update advanced, or clean_db()
        # + full refetch if the server deleted songs.
        cache_loaded = False
        if not force_download and os.path.exists(self._db_filename):
            load_from_cache()
            cache_loaded = True

        self._set_status('Checking for updates...')

        handshake = {}

        # build handshake url
        if self._settings['username'] != '':
            # username/password provided
            timestamp = int(time.time())
            password = hashlib.sha256(self._settings['password'].encode('utf-8')).hexdigest()
            authkey = hashlib.sha256((str(timestamp) + password).encode('utf-8')).hexdigest()

            ampache_server_uri = (
                f"{self._settings['url']}{_API_PATH}"
                f"?action=handshake&auth={authkey}&timestamp={timestamp}"
                f"&user={self._settings['username']}&version=350001")
        else:
            # api key provided
            ampache_server_uri = (
                f"{self._settings['url']}{_API_PATH}"
                f"?action=handshake&auth={self._settings['password']}&version=350001")

        # execute handshake
        cancel = Gio.Cancellable()
        self._async_get(cancel, ampache_server_uri, handshake_cb, cancel)
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
        uri = self._albumart.get(_album_key(artist or '', album or ''))
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
