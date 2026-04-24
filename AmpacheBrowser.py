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

# Matches an auth/ssid query parameter (with any hex value, or empty). Used
# by strip_auth() and inject_auth() to normalise stream/art URLs so they can
# survive Ampache session expiry: stored URLs carry only 'ssid=' / 'auth=',
# and the current session's token is stitched in at playback-request time.
_RE_AUTH = re.compile(r'\b(auth|ssid)=[a-fA-F0-9]*')

# Ampache XML API path, appended to the server base URL.
_API_PATH = '/server/xml.server.php'


def strip_auth(url):
    """Return `url` with any auth/ssid token value removed (param name kept)."""
    return _RE_AUTH.sub(r'\1=', url)


def inject_auth(url, auth):
    """
    Return `url` with the auth/ssid token replaced by `auth`.

    When `auth` is falsy (e.g. the handshake hasn't completed yet), the URL
    is returned unchanged — the caller will see the stripped placeholder and
    playback will fail, which is the correct behaviour when we genuinely
    have no session token to offer.
    """
    if not auth:
        return url
    return _RE_AUTH.sub(r'\1=' + auth, url)


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
    """
    Write a list of song dicts into RhythmDB (or a playlist source).

    When update_existing is True, metadata on already-known URLs is refreshed
    rather than skipped. This is used by the incremental update path.

    When skip_lookup is True, the per-URL entry_lookup_by_location() call is
    bypassed — callers use this to promise that RhythmDB has no entries of
    this entry_type yet, so the lookup would always return None. If a URL
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
    """
    Parse a playlists XML response into a list of Playlist namedtuples.

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


def parse_songs(contents):
    """
    Parse a songs XML response into (songs, albumart).

    songs is a list of dicts matching the songs table columns; albumart
    maps artist+album to an art URL. Year is stored as a raw integer;
    conversion to Julian happens in songs_to_rhythmdb() when writing to
    RhythmDB. Auth tokens are stripped from url/art fields so stored URLs
    are session-independent; AmpacheEntryType.do_get_playback_uri()
    and _album_art_requested() inject the current session's token.
    """
    def int_or_default(el, name, default=-1):
        t = el.findtext(name, '')
        return int(t) if t.isdigit() else default

    songs, albumart = [], {}
    for s in ET.fromstring(contents).findall('song'):
        url = s.findtext('url', '')
        if not url:
            continue
        url = strip_auth(url)
        art = strip_auth(s.findtext('art', ''))
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
        self._handshake_add = ''
        self._handshake_update = ''
        self._handshake_clean = ''

        self._text = None
        self._busy = False

        self._activated = False
        self._force_download = False
        self._cache_loaded = False

        # SQLite connection for the active update cycle. Opened by either
        # the full-refetch or the incremental path; closed when that path
        # finishes sealing the cache.
        self._conn = None

        # Per-chunk state for the parallel full-refetch fetcher. Set by
        # _download_library_or_playlist(), mutated by _on_download_chunk().
        self._chunk_offsets = []
        self._chunk_remaining = 0
        self._chunk_songs_loaded = 0
        self._chunk_aborted = False
        self._chunk_items = 0
        self._chunk_is_playlist = False
        self._chunk_source = None
        self._chunk_playlist_id = None
        self._chunk_playlist_name = ''

        # Per-call state for the sequential fetcher used by the incremental
        # path. Set by _fetch_songs_sequential(), mutated by chunk callbacks.
        self._seq_uri_base = ''
        self._seq_offset = 0
        self._seq_all_songs = []
        self._seq_all_albumart = {}
        self._seq_on_done = None

        # Incremental-update state.
        self._delta_fetch_queue = collections.deque()
        self._delta_to_fetch = collections.deque()
        self._delta_label = ''
        self._incr_playlist = None
        self._incr_playlist_source = None

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

    def _api_url(self, action, auth=None, **params):
        """
        Build an Ampache API URL.

        Defaults `auth` to the current session token (self._handshake_auth);
        the handshake itself overrides this with the computed authkey or
        the configured API key.
        """
        if auth is None:
            auth = self._handshake_auth
        parts = [f"action={action}", f"auth={auth}"]
        parts.extend(f"{k}={v}" for k, v in params.items())
        return f"{self._settings['url']}{_API_PATH}?{'&'.join(parts)}"

    def _parse_or_log(self, parse_fn, contents, label, default=None, **kwargs):
        """
        Call `parse_fn(contents, **kwargs)`; on ET.ParseError, log + return default.

        On error, the offending source line is included in the log message
        when the parser exception carries position info.
        """
        try:
            return parse_fn(contents, **kwargs)
        except ET.ParseError as exc:
            bad_line = '<unavailable>'
            if hasattr(exc, 'position'):
                try:
                    bad_line = contents.decode('utf-8').splitlines()[exc.position[0] - 1]
                except (IndexError, UnicodeDecodeError):
                    pass
            print(f"error parsing {label}: {exc}: {bad_line}")
            return default

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
        """
        Start a cache-reconciliation cycle.

        Validates settings, optionally pre-loads the cached library so the
        user isn't blocked on the handshake round-trip, then fires the
        handshake. _on_handshake() picks one of three paths:
          (a) full refetch  — clean actually removed songs, meta missing, or forced
          (b) incremental   — add or update timestamp changed
          (c) load cache    — add and update timestamps both unchanged
        """
        if not self._settings['url']:
            _show_error_dialog(_('URL missing'))
            self._activated = False
            return

        if not self._settings['password']:
            _show_error_dialog(_('Password missing'))
            self._activated = False
            return

        self._force_download = force_download

        # Reset the playlist queue for this update cycle. Without this,
        # any un-consumed entry left from a previous cycle would accumulate
        # across calls and cause songs to be fetched multiple times.
        self._playlists = collections.deque([Playlist(0, 'library', 0)])

        self._cache_loaded = False
        if not force_download and os.path.exists(self._db_filename):
            self._load_from_cache()
            self._cache_loaded = True

        self._set_status('Checking for updates...')
        self._start_handshake()

    # ------------------------------------------------------------------
    # Handshake
    # ------------------------------------------------------------------

    def _start_handshake(self):
        if self._settings['username'] != '':
            # username/password provided
            timestamp = int(time.time())
            password = hashlib.sha256(self._settings['password'].encode('utf-8')).hexdigest()
            authkey = hashlib.sha256((str(timestamp) + password).encode('utf-8')).hexdigest()
            uri = self._api_url(
                'handshake', auth=authkey, timestamp=timestamp,
                user=self._settings['username'], version=350001)
        else:
            # api key provided
            uri = self._api_url(
                'handshake', auth=self._settings['password'], version=350001)
        cancel = Gio.Cancellable()
        self._async_get(cancel, uri, self._on_handshake, cancel)
        print(f"downloading handshake: {uri}")

    def _on_handshake(self, session_obj, result, cancel):
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

        handshake = self._parse_or_log(
            parse_handshake, contents, 'handshake', default={})

        # find the most recent of the three server timestamps
        self._handshake_newest = max(
            datetime.strptime(s[0:19], '%Y-%m-%dT%H:%M:%S')
            for s in (handshake['update'], handshake['add'], handshake['clean']))

        self._handshake_auth = handshake['auth']
        self._handshake_songs = int(handshake['songs'])
        self._handshake_add = handshake['add']
        self._handshake_update = handshake['update']
        self._handshake_clean = handshake['clean']

        # Publish the fresh token to the entry type so its
        # do_get_playback_uri override can stitch it into stripped URLs.
        self._entry_type.set_auth(self._handshake_auth)

        # read cached meta
        stored_add = stored_update = stored_clean = None
        stored_song_count = 0
        if os.path.exists(self._db_filename):
            meta_conn = _open_db(self._db_filename)
            stored_add = _read_meta(meta_conn, 'last_add')
            stored_update = _read_meta(meta_conn, 'last_update')
            stored_clean = _read_meta(meta_conn, 'last_clean')
            stored_song_count = meta_conn.execute(
                'SELECT COUNT(*) FROM songs').fetchone()[0]
            meta_conn.close()

        clean_removed_songs = (
            self._handshake_clean != stored_clean and
            self._handshake_songs < stored_song_count
        )

        needs_full = (
            self._force_download or
            not os.path.exists(self._db_filename) or
            stored_clean is None or stored_add is None or stored_update is None or
            clean_removed_songs
        )

        if needs_full:
            # If we pre-loaded the cache, wipe the stale entries — the
            # server has deleted songs and we don't know which URLs.
            if self._cache_loaded:
                self.clean_db()

            # delete the old cache database
            try:
                if os.path.exists(self._db_filename):
                    print(f"remove cache db: {self._db_filename}")
                    os.unlink(self._db_filename)
            except Exception as e:
                print(e)

            self._start_full_refetch()

        elif self._handshake_add == stored_add and self._handshake_update == stored_update:
            # Cache already loaded at update() start; just clear status.
            self._set_status(None, False)

        else:
            self._start_incremental(stored_add, stored_update)

    # ------------------------------------------------------------------
    # Full-refetch path
    # ------------------------------------------------------------------

    def _start_full_refetch(self):
        uri = self._api_url('playlists')
        cancel = Gio.Cancellable()
        self._async_get(cancel, uri, self._on_playlists_for_refetch, cancel)
        print(f"downloading playlists: {uri}")

    def _on_playlists_for_refetch(self, session_obj, result, cancel):
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

        self._playlists.extend(self._parse_or_log(
            parse_playlists, contents, 'playlists', default=[],
            user=self._settings['username']))

        # Open the cache database now that we know a download is needed.
        self._conn = _open_db(self._db_filename)
        self._advance_download_queue()

    def _advance_download_queue(self):
        """
        Pop the next playlist off the queue and fetch its songs.
        When the queue is empty, seal the cache and finish.
        """
        try:
            if self._playlists:
                playlist = self._playlists.popleft()
                print(f'process playlist: {playlist.name}')
                if playlist.id == 0:
                    self._download_library_or_playlist(
                        self._api_url('songs'),
                        self._handshake_songs,
                        False, self, None, playlist.name)
                else:
                    playlist_source = self._create_playlist_source(
                        playlist.id, playlist.name)
                    self._conn.execute(_INSERT_PLAYLIST_SQL,
                                       (str(playlist.id), playlist.name))
                    self._conn.commit()
                    self._download_library_or_playlist(
                        self._api_url('playlist_songs', filter=playlist.id),
                        playlist.items, True, playlist_source,
                        str(playlist.id), playlist.name)
            else:
                self._finish_full_refetch()
        except Exception as e:
            traceback.print_exc()
            print(f'Exception: {e}')

    def _finish_full_refetch(self):
        # All playlists downloaded — write meta, seal the cache, and finish.
        newest_time = int(time.mktime(self._handshake_newest.timetuple()))
        _write_meta(self._conn, 'last_add', self._handshake_add)
        _write_meta(self._conn, 'last_update', self._handshake_update)
        _write_meta(self._conn, 'last_clean', self._handshake_clean)
        self._conn.commit()
        self._conn.close()
        self._conn = None
        # change modification time to newest time
        os.utime(self._db_filename, (newest_time, newest_time))
        print(f"wrote cache db: {self._db_filename}")
        print('no more playlists to process, refilter display page model')
        self._shell.props.display_page_model.refilter()

    def _download_library_or_playlist(self, uri, items, is_playlist, source,
                                      playlist_id, playlist_name):
        """Fire all chunk requests for one library/playlist in parallel."""
        if items <= 0:
            self._set_status(None, False)
            self._advance_download_queue()
            return

        # Calculate all chunk offsets up front so we can fire
        # all requests simultaneously rather than sequentially.
        self._chunk_offsets = list(range(0, items, self._limit))
        self._chunk_remaining = len(self._chunk_offsets)
        self._chunk_songs_loaded = 0
        self._chunk_aborted = False
        self._chunk_items = items
        self._chunk_is_playlist = is_playlist
        self._chunk_source = source
        self._chunk_playlist_id = playlist_id
        self._chunk_playlist_name = playlist_name

        self._set_status(f'Fetching {playlist_name}... (0 / {items} songs)', True)

        # Fire all chunk requests in parallel via Soup so the
        # per-host connection limit applies to our session, not GIO's.
        for i, offset in enumerate(self._chunk_offsets):
            chunk_uri = f"{uri}&offset={offset}&limit={self._limit}"
            cancel = Gio.Cancellable()
            self._async_get(cancel, chunk_uri, self._on_download_chunk, (cancel, i))
            print(f"download {playlist_name}[{offset}]: {chunk_uri}")

    def _on_download_chunk(self, session_obj, result, user_data):
        cancel, chunk_index = user_data
        self._cancellables.discard(cancel)
        # Always call finish() to free the GLib result, even
        # when cancelled or when we intend to discard the data.
        try:
            contents = session_obj.send_and_read_finish(result).get_data()
        except Exception as e:
            if self._activated and not self._chunk_aborted:
                self._chunk_aborted = True
                _show_error_dialog(_('Songs response: %s') % e)
                self._activated = False
                self._set_status(None, False)
            return
        if self._chunk_aborted or not self._activated:
            return

        playlist_name = self._chunk_playlist_name
        offset = self._chunk_offsets[chunk_index]
        print(f"parse chunk {playlist_name}[{offset}]...")
        songs, albumart = self._parse_or_log(
            parse_songs, contents, 'songs', default=([], {}))

        # Write parsed songs to RhythmDB. Full-refetch guarantees
        # a fresh entry_type in RhythmDB (clean_db preceded this
        # path), so entry_lookup_by_location is skippable.
        songs_to_rhythmdb(
            songs, self._albumart,
            self._db, self._entry_type,
            self._chunk_is_playlist, self._chunk_source, self._entries,
            skip_lookup=True)
        if not self._chunk_is_playlist:
            self._db.commit()
        self._albumart.update(albumart)

        # Write parsed songs to SQLite cache
        if not self._chunk_is_playlist:
            self._conn.executemany(_INSERT_SONG_SQL, songs)
        else:
            self._conn.executemany(
                _INSERT_PLAYLIST_SONG_SQL,
                [(self._chunk_playlist_id, song['url']) for song in songs])
        self._conn.commit()

        self._chunk_songs_loaded += min(self._limit, self._chunk_items - offset)
        loaded = min(self._chunk_songs_loaded, self._chunk_items)
        self._set_status(
            f'Fetching {playlist_name}... ({loaded} / {self._chunk_items} songs)')

        self._chunk_remaining -= 1
        if self._chunk_remaining == 0:
            self._set_status(None, False)
            self._advance_download_queue()

    # ------------------------------------------------------------------
    # Cache load
    # ------------------------------------------------------------------

    def _load_from_cache(self):
        self._set_status('Loading from cache...', True)
        try:
            db_conn = _open_db(self._db_filename)

            # Load main song library. Legacy caches may contain URLs
            # with baked-in auth tokens; strip them so RhythmDB entry
            # LOCATIONs are session-independent and match the stripped
            # URLs produced by parse_songs() on subsequent deltas.
            songs = [dict(row) for row in db_conn.execute('SELECT * FROM songs')]
            for song in songs:
                song['url'] = strip_auth(song['url'])
                if song['art']:
                    song['art'] = strip_auth(song['art'])
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
                    playlist_source.add_location(strip_auth(url), -1)

            db_conn.close()
        except Exception as e:
            print(f'error loading from cache: {e}')

        self._set_status(None, False)
        self._shell.props.display_page_model.refilter()

    # ------------------------------------------------------------------
    # Incremental path
    # ------------------------------------------------------------------

    def _start_incremental(self, stored_add, stored_update):
        """Fetch only songs added/updated since the last sync, then diff playlists."""
        self._conn = _open_db(self._db_filename)

        self._delta_fetch_queue = collections.deque()
        if self._handshake_add != stored_add:
            # Truncate to 19 chars (drop timezone suffix) for URL param
            self._delta_fetch_queue.append((
                self._api_url('songs', add=stored_add[:19]),
                'new songs'))
        if self._handshake_update != stored_update:
            self._delta_fetch_queue.append((
                self._api_url('songs', update=stored_update[:19]),
                'updated songs'))

        # Cache was already loaded in update() before the handshake.
        self._set_status('Checking for library updates...', True)
        self._run_next_delta()

    def _run_next_delta(self):
        if not self._delta_fetch_queue:
            self._fetch_incremental_playlists()
            return
        uri, label = self._delta_fetch_queue.popleft()
        self._delta_label = label
        self._set_status(f'Fetching {label}...')
        print(f"incremental fetch: {uri}")
        self._fetch_songs_sequential(uri, self._on_delta_songs_done)

    def _on_delta_songs_done(self, songs, albumart):
        print(f"incremental: {len(songs)} {self._delta_label}")
        songs_to_rhythmdb(
            songs, self._albumart,
            self._db, self._entry_type,
            False, self, self._entries,
            update_existing=True)
        self._db.commit()
        self._albumart.update(albumart)
        self._conn.executemany(_INSERT_SONG_SQL, songs)
        self._conn.commit()
        self._run_next_delta()

    def _fetch_incremental_playlists(self):
        cancel = Gio.Cancellable()
        self._async_get(
            cancel, self._api_url('playlists'),
            self._on_incremental_playlists, cancel)

    def _on_incremental_playlists(self, session_obj, result, cancel):
        self._cancellables.discard(cancel)
        if not self._activated:
            return
        try:
            contents = session_obj.send_and_read_finish(result).get_data()
        except Exception as e:
            print(f"incremental playlists error: {e}")
            self._finish_incremental()
            return

        new_playlists_list = self._parse_or_log(
            parse_playlists, contents, 'incremental playlists', default=[],
            user=self._settings['username'])

        # id → Playlist namedtuple
        new_pl = {p.id: p for p in new_playlists_list}
        stored_pl = {row['id']: row['name'] for row in
                     self._conn.execute('SELECT id, name FROM playlists')}

        # Remove deleted playlists
        for pid in list(stored_pl.keys()):
            if pid not in new_pl:
                src = self._playlist_sources.pop(pid, None)
                if src is not None:
                    src.delete_thyself()
                self._conn.execute('DELETE FROM playlists WHERE id = ?', (pid,))
                self._conn.execute(
                    'DELETE FROM playlist_songs WHERE playlist_id = ?', (pid,))

        # Determine which playlists need a song re-fetch (new or name-changed)
        self._delta_to_fetch = collections.deque()
        for pid, pl in new_pl.items():
            if pid not in stored_pl:
                # New playlist — create source
                self._create_playlist_source(pid, pl.name)
                self._conn.execute(_INSERT_PLAYLIST_SQL, (pid, pl.name))
                self._delta_to_fetch.append(pl)
            elif stored_pl[pid] != pl.name:
                # Name changed — update stored name, clear and re-fetch songs
                self._conn.execute(_INSERT_PLAYLIST_SQL, (pid, pl.name))
                self._conn.execute(
                    'DELETE FROM playlist_songs WHERE playlist_id = ?', (pid,))
                self._delta_to_fetch.append(pl)

        self._conn.commit()
        self._fetch_next_incremental_playlist()

    def _fetch_next_incremental_playlist(self):
        if not self._delta_to_fetch:
            self._finish_incremental()
            return
        pl = self._delta_to_fetch.popleft()
        source = self._playlist_sources.get(pl.id)
        if source is None:
            self._fetch_next_incremental_playlist()
            return
        self._incr_playlist = pl
        self._incr_playlist_source = source
        pl_uri = self._api_url('playlist_songs', filter=pl.id)
        self._fetch_songs_sequential(
            pl_uri, self._on_incremental_playlist_songs_done)

    def _on_incremental_playlist_songs_done(self, songs, albumart):
        pl = self._incr_playlist
        source = self._incr_playlist_source
        songs_to_rhythmdb(
            songs, self._albumart,
            self._db, self._entry_type,
            True, source, self._entries)
        self._conn.executemany(
            _INSERT_PLAYLIST_SONG_SQL,
            [(pl.id, s['url']) for s in songs])
        self._conn.commit()
        self._fetch_next_incremental_playlist()

    def _finish_incremental(self):
        _write_meta(self._conn, 'last_add', self._handshake_add)
        _write_meta(self._conn, 'last_update', self._handshake_update)
        _write_meta(self._conn, 'last_clean', self._handshake_clean)
        self._conn.commit()
        newest_time = int(time.mktime(self._handshake_newest.timetuple()))
        self._conn.close()
        self._conn = None
        os.utime(self._db_filename, (newest_time, newest_time))
        print('incremental update complete')
        self._set_status(None, False)
        self._shell.props.display_page_model.refilter()

    # ------------------------------------------------------------------
    # Sequential fetcher (used by the incremental path).
    # Fires one chunk at a time, stopping when the response is smaller
    # than the limit (no need to know the total count upfront).
    # ------------------------------------------------------------------

    def _fetch_songs_sequential(self, uri_base, on_done):
        self._seq_uri_base = uri_base
        self._seq_offset = 0
        self._seq_all_songs = []
        self._seq_all_albumart = {}
        self._seq_on_done = on_done
        self._fetch_next_sequential_chunk()

    def _fetch_next_sequential_chunk(self):
        chunk_uri = (f"{self._seq_uri_base}"
                     f"&offset={self._seq_offset}&limit={self._limit}")
        cancel = Gio.Cancellable()
        self._async_get(cancel, chunk_uri, self._on_sequential_chunk, cancel)

    def _on_sequential_chunk(self, session_obj, result, cancel):
        self._cancellables.discard(cancel)
        if not self._activated:
            return
        try:
            contents = session_obj.send_and_read_finish(result).get_data()
        except Exception as e:
            print(f"incremental chunk error: {e}")
            self._seq_on_done(self._seq_all_songs, self._seq_all_albumart)
            return

        songs, albumart = self._parse_or_log(
            parse_songs, contents, 'incremental songs', default=([], {}))

        self._seq_all_songs.extend(songs)
        self._seq_all_albumart.update(albumart)
        self._seq_offset += self._limit

        if len(songs) < self._limit:
            self._seq_on_done(self._seq_all_songs, self._seq_all_albumart)
        else:
            self._fetch_next_sequential_chunk()

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
                # 0o700: per-user cache, restrict to owner
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
            # albumart URLs are stored stripped; inject the current token
            # so the server honours the fetch. ExtDB caches the returned
            # image bytes, so this only matters on the first fetch.
            uri = inject_auth(uri, self._handshake_auth)
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

            # Drop references. Do NOT call playlist_source.delete_thyself()
            # here — Rhythmbox removes child display pages automatically when
            # the parent is deleted; doing it ourselves causes a double-free.
            #
            # Do NOT call entry_delete_by_type here either. It emits
            # entry-deleted signals that Rhythmbox's rb_entry_view tries to
            # handle, but the entry view inside RBBrowserSource is already
            # invalid by the time do_delete_thyself is called, producing a
            # SIGSEGV in rb_entry_view_have_selection. Since entries are
            # registered with save_to_disk=False they never persist to disk,
            # so skipping this call is safe on exit.
            self._playlist_sources = {}
            self._entries = []

        RB.BrowserSource.do_delete_thyself(self)


GObject.type_register(AmpacheBrowser)
