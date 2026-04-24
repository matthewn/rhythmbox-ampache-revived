#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unit tests for AmpacheBrowser XML parsers, songs_to_rhythmdb(), and the
SQLite cache roundtrip.

Run with:
    python3 -m unittest test_handlers
or:
    python3 test_handlers.py
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Stub out gi and all GObject/Rhythmbox dependencies before importing
# AmpacheBrowser.  The module imports gi at the top level, so we must satisfy
# those imports first.
# ---------------------------------------------------------------------------

_RB = MagicMock()
_GObject = MagicMock()
_Gtk = MagicMock()
_Gio = MagicMock()
_GLib = MagicMock()
_Soup = MagicMock()

_gi = MagicMock()
_gi.require_version = MagicMock()

_gi_repository = MagicMock()
_gi_repository.RB = _RB
_gi_repository.GObject = _GObject
_gi_repository.Gtk = _Gtk
_gi_repository.Gio = _Gio
_gi_repository.GLib = _GLib
_gi_repository.Soup = _Soup

sys.modules['gi'] = _gi
sys.modules['gi.repository'] = _gi_repository

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from AmpacheBrowser import (  # noqa: E402
    parse_handshake, parse_playlists, parse_songs,
    songs_to_rhythmdb, strip_auth, inject_auth,
    _open_db, _read_meta, _write_meta,
    _INSERT_SONG_SQL, _INSERT_PLAYLIST_SQL, _INSERT_PLAYLIST_SONG_SQL,
)


# ---------------------------------------------------------------------------
# parse_handshake
# ---------------------------------------------------------------------------

class TestHandshakeHandler(unittest.TestCase):

    HANDSHAKE_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<root>
<auth>abc123def456abc1</auth>
<api>5.6.1</api>
<update>2024-03-01T12:00:00+00:00</update>
<add>2024-03-02T08:00:00+00:00</add>
<clean>2024-02-28T06:00:00+00:00</clean>
<songs>1234</songs>
<artists>56</artists>
<albums>78</albums>
</root>
"""

    def setUp(self):
        self.handshake = parse_handshake(self.HANDSHAKE_XML)

    def test_auth(self):
        self.assertEqual(self.handshake['auth'], 'abc123def456abc1')

    def test_songs_count(self):
        self.assertEqual(self.handshake['songs'], '1234')

    def test_update_timestamp(self):
        self.assertEqual(self.handshake['update'], '2024-03-01T12:00:00+00:00')

    def test_add_timestamp(self):
        self.assertEqual(self.handshake['add'], '2024-03-02T08:00:00+00:00')

    def test_clean_timestamp(self):
        self.assertEqual(self.handshake['clean'], '2024-02-28T06:00:00+00:00')

    def test_api_version(self):
        self.assertEqual(self.handshake['api'], '5.6.1')

    def test_long_text_node(self):
        """A long text value must be correctly parsed (no multi-chunk splitting
        artefacts as could happen with SAX)."""
        long_auth = 'abcdef' * 50
        xml = f'<?xml version="1.0"?><root><auth>{long_auth}</auth></root>'
        self.assertEqual(parse_handshake(xml)['auth'], long_auth)


# ---------------------------------------------------------------------------
# parse_playlists
# ---------------------------------------------------------------------------

class TestPlaylistsHandler(unittest.TestCase):

    PLAYLISTS_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<root>
<playlist id="101">
<name>My Favourites</name>
<items>42</items>
<owner>alice</owner>
<type>private</type>
</playlist>
<playlist id="202">
<name>Public Mix</name>
<items>10</items>
<owner>bob</owner>
<type>public</type>
</playlist>
<playlist id="303">
<name>Other User Private</name>
<items>5</items>
<owner>charlie</owner>
<type>private</type>
</playlist>
</root>
"""

    def setUp(self):
        self.playlists = parse_playlists(self.PLAYLISTS_XML, 'alice')

    def _playlist(self, id_):
        result = next((p for p in self.playlists if p[0] == id_), None)
        self.assertIsNotNone(result, f"no playlist with id {id_!r}")
        return result

    def test_own_private_playlist_included(self):
        self.assertIn('101', [p[0] for p in self.playlists])

    def test_public_playlist_included(self):
        self.assertIn('202', [p[0] for p in self.playlists])

    def test_other_users_private_playlist_excluded(self):
        self.assertNotIn('303', [p[0] for p in self.playlists])

    def test_total_count(self):
        self.assertEqual(len(self.playlists), 2)

    def test_playlist_name(self):
        self.assertEqual(self._playlist('101')[1], 'My Favourites')

    def test_playlist_items(self):
        self.assertEqual(self._playlist('101')[2], 42)

    def test_public_playlist_name(self):
        self.assertEqual(self._playlist('202')[1], 'Public Mix')

    def test_non_digit_items_ignored(self):
        xml = """\
<?xml version="1.0"?>
<root>
<playlist id="1">
<name>Bad Items</name>
<items>notanumber</items>
<owner>alice</owner>
<type>private</type>
</playlist>
</root>"""
        playlists = parse_playlists(xml, 'alice')
        # items should fall back to 0
        self.assertEqual(playlists[0][2], 0)


# ---------------------------------------------------------------------------
# parse_songs
# ---------------------------------------------------------------------------

# Auth tokens must be hex strings to match the regex [a-fA-F0-9]*
# Token present in the test XML before parse_songs strips it.  parse_songs
# must preserve the ssid=/auth= parameter name while dropping the value.
OLD_AUTH = 'aabbccddeeff0011'

SONGS_XML = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<root>
<song id="1001">
<title>Test Song</title>
<artist>Test Artist</artist>
<album>Test Album</album>
<tag>Rock</tag>
<track>3</track>
<year>2020</year>
<time>245</time>
<size>8192000</size>
<rating>4</rating>
<url>http://example.com/song.mp3?ssid={OLD_AUTH}&amp;type=song</url>
<art>http://example.com/art.jpg?auth={OLD_AUTH}</art>
</song>
<song id="1002">
<title>Minimal Song</title>
<artist></artist>
<album></album>
<url>http://example.com/minimal.mp3</url>
</song>
</root>
"""


class TestSongsHandler(unittest.TestCase):

    def setUp(self):
        self.songs, self.albumart = parse_songs(SONGS_XML)

    def test_two_songs_collected(self):
        self.assertEqual(len(self.songs), 2)

    def test_title(self):
        self.assertEqual(self.songs[0]['title'], 'Test Song')

    def test_artist(self):
        self.assertEqual(self.songs[0]['artist'], 'Test Artist')

    def test_album(self):
        self.assertEqual(self.songs[0]['album'], 'Test Album')

    def test_genre(self):
        self.assertEqual(self.songs[0]['tag'], 'Rock')

    def test_track_number(self):
        self.assertEqual(self.songs[0]['track'], 3)

    def test_year_stored_as_integer(self):
        self.assertEqual(self.songs[0]['year'], 2020)

    def test_duration(self):
        self.assertEqual(self.songs[0]['time'], 245)

    def test_file_size(self):
        self.assertEqual(self.songs[0]['size'], 8192000)

    def test_rating(self):
        self.assertEqual(self.songs[0]['rating'], 4)

    def test_auth_stripped_from_url(self):
        url = self.songs[0]['url']
        self.assertIn('ssid=', url)           # param name preserved
        self.assertNotIn(OLD_AUTH, url)       # token value removed

    def test_auth_stripped_from_art(self):
        art_url = self.albumart.get('Test ArtistTest Album', '')
        self.assertIn('auth=', art_url)
        self.assertNotIn(OLD_AUTH, art_url)

    def test_albumart_stored(self):
        self.assertIn('Test ArtistTest Album', self.albumart)

    def test_empty_artist_stored_as_empty_string(self):
        self.assertEqual(self.songs[1]['artist'], '')

    def test_empty_album_stored_as_empty_string(self):
        self.assertEqual(self.songs[1]['album'], '')

    def test_minimal_song_has_default_track(self):
        self.assertEqual(self.songs[1]['track'], -1)

    def test_year_out_of_range_ignored(self):
        xml = """\
<?xml version="1.0"?>
<root>
<song id="1">
<title>Bad Year</title>
<url>http://example.com/bad-year.mp3</url>
<year>0</year>
</song>
</root>"""
        songs, _ = parse_songs(xml)
        self.assertEqual(songs[0]['year'], -1)

    def test_year_boundary_min(self):
        xml = ('<?xml version="1.0"?><root><song id="1">'
               '<title>Min Year</title>'
               '<url>http://example.com/min.mp3</url>'
               '<year>1</year></song></root>')
        songs, _ = parse_songs(xml)
        self.assertEqual(songs[0]['year'], 1)

    def test_year_boundary_max(self):
        xml = ('<?xml version="1.0"?><root><song id="1">'
               '<title>Max Year</title>'
               '<url>http://example.com/max.mp3</url>'
               '<year>9999</year></song></root>')
        songs, _ = parse_songs(xml)
        self.assertEqual(songs[0]['year'], 9999)

    def test_year_above_max_ignored(self):
        xml = ('<?xml version="1.0"?><root><song id="1">'
               '<title>Too High</title>'
               '<url>http://example.com/toohigh.mp3</url>'
               '<year>10000</year></song></root>')
        songs, _ = parse_songs(xml)
        self.assertEqual(songs[0]['year'], -1)

    def test_song_with_no_url_skipped(self):
        """A <song> element with no <url> child must not appear in songs."""
        xml = ('<?xml version="1.0"?><root>'
               '<song id="1"><title>No URL</title></song>'
               '<song id="2"><title>Has URL</title>'
               '<url>http://example.com/ok.mp3</url></song>'
               '</root>')
        songs, _ = parse_songs(xml)
        self.assertEqual(len(songs), 1)
        self.assertEqual(songs[0]['title'], 'Has URL')


# ---------------------------------------------------------------------------
# strip_auth / inject_auth
# ---------------------------------------------------------------------------

class TestStripAuth(unittest.TestCase):

    def test_strips_ssid_value(self):
        self.assertEqual(
            strip_auth('http://example.com/song.mp3?ssid=aabbccdd&type=song'),
            'http://example.com/song.mp3?ssid=&type=song')

    def test_strips_auth_value(self):
        self.assertEqual(
            strip_auth('http://example.com/art.jpg?auth=aabbccdd'),
            'http://example.com/art.jpg?auth=')

    def test_is_idempotent(self):
        url = 'http://example.com/song.mp3?ssid=&type=song'
        self.assertEqual(strip_auth(url), url)

    def test_url_without_token_unchanged(self):
        url = 'http://example.com/plain.mp3'
        self.assertEqual(strip_auth(url), url)


class TestInjectAuth(unittest.TestCase):

    def test_injects_into_stripped_url(self):
        self.assertEqual(
            inject_auth('http://example.com/song.mp3?ssid=&type=song', 'newtok'),
            'http://example.com/song.mp3?ssid=newtok&type=song')

    def test_replaces_existing_token(self):
        # Real Ampache tokens are hex, which is what _RE_AUTH matches.
        self.assertEqual(
            inject_auth('http://example.com/art.jpg?auth=aabbcc', 'ddeeff'),
            'http://example.com/art.jpg?auth=ddeeff')

    def test_preserves_parameter_name(self):
        # ssid stays ssid, auth stays auth
        self.assertEqual(
            inject_auth('x?ssid=', 'T'), 'x?ssid=T')
        self.assertEqual(
            inject_auth('x?auth=', 'T'), 'x?auth=T')

    def test_none_auth_returns_unchanged(self):
        url = 'http://example.com/song.mp3?ssid=&type=song'
        self.assertEqual(inject_auth(url, None), url)

    def test_empty_auth_returns_unchanged(self):
        url = 'http://example.com/song.mp3?ssid=&type=song'
        self.assertEqual(inject_auth(url, ''), url)

    def test_url_without_token_unchanged(self):
        self.assertEqual(inject_auth('http://x/plain.mp3', 'T'), 'http://x/plain.mp3')


# ---------------------------------------------------------------------------
# songs_to_rhythmdb()
# ---------------------------------------------------------------------------

_SONG_DICTS = [
    {
        'url':    'http://example.com/song.mp3',
        'artist': 'Artist',
        'album':  'Album',
        'title':  'Song',
        'tag':    'Rock',
        'track':  1,
        'year':   2020,
        'time':   200,
        'size':   5000000,
        'rating': 4,
        'art':    'http://example.com/art.jpg',
    },
    {
        'url':    'http://example.com/minimal.mp3',
        'artist': '',
        'album':  '',
        'title':  'Minimal',
        'tag':    '',
        'track': -1,
        'year': -1,
        'time': -1,
        'size': -1,
        'rating': -1,
        'art':    '',
    },
]


class TestSongsToRhythmdb(unittest.TestCase):

    def setUp(self):
        self.db = MagicMock()
        self.db.entry_lookup_by_location.return_value = None
        self.entry1 = MagicMock()
        self.entry2 = MagicMock()
        _RB.RhythmDBEntry.new.reset_mock()
        _RB.RhythmDBEntry.new.side_effect = [self.entry1, self.entry2]
        _GLib.Date.new_dmy.return_value.get_julian.return_value = 737060
        self.albumart = {}
        self.entries = []
        songs_to_rhythmdb(
            _SONG_DICTS, self.albumart, self.db, MagicMock(),
            False, None, self.entries)

    def _props(self, entry):
        result = {}
        for c in self.db.entry_set.call_args_list:
            if c.args[0] is entry:
                result[c.args[1]] = c.args[2]
        return result

    def test_two_entries_created(self):
        self.assertEqual(len(self.entries), 2)

    def test_title(self):
        self.assertEqual(self._props(self.entry1)[_RB.RhythmDBPropType.TITLE], 'Song')

    def test_artist(self):
        self.assertEqual(self._props(self.entry1)[_RB.RhythmDBPropType.ARTIST], 'Artist')

    def test_album(self):
        self.assertEqual(self._props(self.entry1)[_RB.RhythmDBPropType.ALBUM], 'Album')

    def test_genre(self):
        self.assertEqual(self._props(self.entry1)[_RB.RhythmDBPropType.GENRE], 'Rock')

    def test_track_number(self):
        self.assertEqual(self._props(self.entry1)[_RB.RhythmDBPropType.TRACK_NUMBER], 1)

    def test_year_converted_to_julian(self):
        self.assertIn(_RB.RhythmDBPropType.DATE, self._props(self.entry1))
        self.assertEqual(self._props(self.entry1)[_RB.RhythmDBPropType.DATE], 737060)

    def test_duration(self):
        self.assertEqual(self._props(self.entry1)[_RB.RhythmDBPropType.DURATION], 200)

    def test_file_size(self):
        self.assertEqual(self._props(self.entry1)[_RB.RhythmDBPropType.FILE_SIZE], 5000000)

    def test_rating(self):
        self.assertEqual(self._props(self.entry1)[_RB.RhythmDBPropType.RATING], 4)

    def test_albumart_stored(self):
        self.assertIn('ArtistAlbum', self.albumart)

    def test_empty_artist_not_set(self):
        props = self._props(self.entry2)
        self.assertNotIn(_RB.RhythmDBPropType.ARTIST, props)

    def test_empty_album_not_set(self):
        props = self._props(self.entry2)
        self.assertNotIn(_RB.RhythmDBPropType.ALBUM, props)

    def test_neg1_year_not_set(self):
        props = self._props(self.entry2)
        self.assertNotIn(_RB.RhythmDBPropType.DATE, props)

    def test_duplicate_entry_skipped(self):
        db = MagicMock()
        db.entry_lookup_by_location.return_value = MagicMock()  # already in db
        _RB.RhythmDBEntry.new.reset_mock()
        entries = []
        songs_to_rhythmdb(_SONG_DICTS, {}, db, MagicMock(), False, None, entries)
        self.assertEqual(len(entries), 0)
        _RB.RhythmDBEntry.new.assert_not_called()

    def test_update_existing_refreshes_metadata(self):
        """With update_existing=True, metadata is written to already-known entries."""
        existing_entry = MagicMock()
        db = MagicMock()
        db.entry_lookup_by_location.return_value = existing_entry
        _RB.RhythmDBEntry.new.reset_mock()
        entries = []
        songs_to_rhythmdb(
            _SONG_DICTS[:1], {}, db, MagicMock(),
            False, None, entries, update_existing=True)
        # No new entry should be created
        _RB.RhythmDBEntry.new.assert_not_called()
        self.assertEqual(len(entries), 0)
        # Metadata should have been written to the existing entry
        set_props = {c.args[1] for c in db.entry_set.call_args_list}
        self.assertIn(_RB.RhythmDBPropType.TITLE, set_props)
        self.assertIn(_RB.RhythmDBPropType.ARTIST, set_props)

    def test_update_existing_false_skips_existing_metadata(self):
        """With update_existing=False (default), no metadata is set on existing entries."""
        db = MagicMock()
        db.entry_lookup_by_location.return_value = MagicMock()
        entries = []
        songs_to_rhythmdb(
            _SONG_DICTS[:1], {}, db, MagicMock(),
            False, None, entries, update_existing=False)
        db.entry_set.assert_not_called()

    def test_playlist_mode_calls_add_location(self):
        source = MagicMock()
        _RB.RhythmDBEntry.new.side_effect = None
        songs_to_rhythmdb(_SONG_DICTS, {}, MagicMock(), MagicMock(), True, source, [])
        self.assertEqual(source.add_location.call_count, 2)


# ---------------------------------------------------------------------------
# SQLite cache roundtrip
# ---------------------------------------------------------------------------

_CACHE_SONGS = [
    {
        'url':    'http://example.com/cache.mp3',
        'artist': 'Cache Artist',
        'album':  'Cache Album',
        'title':  'Roundtrip Song',
        'tag':    'Jazz',
        'track':  7,
        'year':   2019,
        'time':   180,
        'size':   6000000,
        'rating': 5,
        'art':    'http://example.com/cache-art.jpg',
    },
    {
        'url':    'http://example.com/second.mp3',
        'artist': 'Second Artist',
        'album':  'Second Album',
        'title':  'Second Song',
        'tag':    '',
        'track':  2,
        'year': -1,
        'time':   210,
        'size': -1,
        'rating': -1,
        'art':    '',
    },
]


class TestSQLiteRoundtrip(unittest.TestCase):
    """
    Verify that song data survives a write-then-read cycle through the SQLite
    cache.  The 'write' side uses _open_db() and _INSERT_SONG_SQL; the 'read'
    side mirrors what load_from_cache() does, then calls songs_to_rhythmdb().
    """

    def setUp(self):
        fd, self.tmp_path = tempfile.mkstemp(suffix='.sqlite')
        os.close(fd)
        # _open_db / sqlite3.connect treats the empty file as a new database.

        # Write
        db_conn = _open_db(self.tmp_path)
        db_conn.executemany(_INSERT_SONG_SQL, _CACHE_SONGS)
        db_conn.commit()
        db_conn.close()

        # Read back (ORDER BY url for deterministic ordering)
        db_conn = _open_db(self.tmp_path)
        songs = [dict(row) for row in
                 db_conn.execute('SELECT * FROM songs ORDER BY url')]
        db_conn.close()

        self.db = MagicMock()
        self.db.entry_lookup_by_location.return_value = None
        self.entry_cache = MagicMock()
        self.entry_second = MagicMock()
        _RB.RhythmDBEntry.new.reset_mock()
        # ORDER BY url: cache.mp3 < second.mp3 alphabetically
        _RB.RhythmDBEntry.new.side_effect = [self.entry_cache, self.entry_second]
        _GLib.Date.new_dmy.return_value.get_julian.return_value = 736695

        self.albumart = {}
        self.entries = []
        songs_to_rhythmdb(
            songs, self.albumart, self.db, MagicMock(),
            False, None, self.entries)

    def tearDown(self):
        if os.path.exists(self.tmp_path):
            os.unlink(self.tmp_path)

    def _props(self, entry):
        result = {}
        for c in self.db.entry_set.call_args_list:
            if c.args[0] is entry:
                result[c.args[1]] = c.args[2]
        return result

    def test_both_entries_created(self):
        self.assertEqual(len(self.entries), 2)

    def test_title_survives(self):
        self.assertEqual(
            self._props(self.entry_cache)[_RB.RhythmDBPropType.TITLE],
            'Roundtrip Song')

    def test_artist_survives(self):
        self.assertEqual(
            self._props(self.entry_cache)[_RB.RhythmDBPropType.ARTIST],
            'Cache Artist')

    def test_album_survives(self):
        self.assertEqual(
            self._props(self.entry_cache)[_RB.RhythmDBPropType.ALBUM],
            'Cache Album')

    def test_genre_survives(self):
        self.assertEqual(
            self._props(self.entry_cache)[_RB.RhythmDBPropType.GENRE],
            'Jazz')

    def test_track_survives(self):
        self.assertEqual(
            self._props(self.entry_cache)[_RB.RhythmDBPropType.TRACK_NUMBER], 7)

    def test_duration_survives(self):
        self.assertEqual(
            self._props(self.entry_cache)[_RB.RhythmDBPropType.DURATION], 180)

    def test_file_size_survives(self):
        self.assertEqual(
            self._props(self.entry_cache)[_RB.RhythmDBPropType.FILE_SIZE], 6000000)

    def test_rating_survives(self):
        self.assertEqual(
            self._props(self.entry_cache)[_RB.RhythmDBPropType.RATING], 5)

    def test_url_survives(self):
        url = _RB.RhythmDBEntry.new.call_args_list[0].args[2]
        self.assertEqual(url, 'http://example.com/cache.mp3')

    def test_albumart_survives(self):
        self.assertIn('Cache ArtistCache Album', self.albumart)
        self.assertEqual(
            self.albumart['Cache ArtistCache Album'],
            'http://example.com/cache-art.jpg')

    def test_second_song_title(self):
        self.assertEqual(
            self._props(self.entry_second)[_RB.RhythmDBPropType.TITLE],
            'Second Song')

    def test_second_song_url(self):
        url = _RB.RhythmDBEntry.new.call_args_list[1].args[2]
        self.assertEqual(url, 'http://example.com/second.mp3')

    def test_playlist_roundtrip(self):
        """Playlist IDs and song URLs survive a write-then-read cycle."""
        db_conn = _open_db(self.tmp_path)
        db_conn.execute(_INSERT_PLAYLIST_SQL, ('42', 'My Playlist'))
        db_conn.execute(_INSERT_PLAYLIST_SONG_SQL,
                        ('42', 'http://example.com/cache.mp3'))
        db_conn.execute(_INSERT_PLAYLIST_SONG_SQL,
                        ('42', 'http://example.com/second.mp3'))
        db_conn.commit()

        playlists = [dict(row) for row in
                     db_conn.execute('SELECT * FROM playlists')]
        urls = [row[0] for row in db_conn.execute(
            'SELECT url FROM playlist_songs WHERE playlist_id = ?', ('42',))]
        db_conn.close()

        self.assertEqual(len(playlists), 1)
        self.assertEqual(playlists[0]['id'], '42')
        self.assertEqual(playlists[0]['name'], 'My Playlist')
        self.assertEqual(sorted(urls), [
            'http://example.com/cache.mp3',
            'http://example.com/second.mp3',
        ])


# ---------------------------------------------------------------------------
# meta table helpers (_read_meta, _write_meta, _open_db schema migration)
# ---------------------------------------------------------------------------

class TestMeta(unittest.TestCase):
    """Tests for _read_meta, _write_meta, and meta table creation in _open_db."""

    def setUp(self):
        fd, self.tmp_path = tempfile.mkstemp(suffix='.sqlite')
        os.close(fd)
        self.conn = _open_db(self.tmp_path)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.tmp_path):
            os.unlink(self.tmp_path)

    def test_meta_table_exists_after_open_db(self):
        tables = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn('meta', tables)

    def test_read_meta_missing_key_returns_none(self):
        self.assertIsNone(_read_meta(self.conn, 'nonexistent'))

    def test_write_and_read_meta(self):
        _write_meta(self.conn, 'last_add', '2024-03-01T12:00:00+00:00')
        self.conn.commit()
        self.assertEqual(
            _read_meta(self.conn, 'last_add'),
            '2024-03-01T12:00:00+00:00')

    def test_write_meta_upserts(self):
        _write_meta(self.conn, 'last_clean', 'first')
        _write_meta(self.conn, 'last_clean', 'second')
        self.conn.commit()
        self.assertEqual(_read_meta(self.conn, 'last_clean'), 'second')

    def test_multiple_keys_independent(self):
        _write_meta(self.conn, 'last_add', 'add_val')
        _write_meta(self.conn, 'last_update', 'update_val')
        _write_meta(self.conn, 'last_clean', 'clean_val')
        self.conn.commit()
        self.assertEqual(_read_meta(self.conn, 'last_add'), 'add_val')
        self.assertEqual(_read_meta(self.conn, 'last_update'), 'update_val')
        self.assertEqual(_read_meta(self.conn, 'last_clean'), 'clean_val')

    def test_meta_persists_across_connections(self):
        _write_meta(self.conn, 'last_add', 'persistent')
        self.conn.commit()
        self.conn.close()
        conn2 = _open_db(self.tmp_path)
        self.assertEqual(_read_meta(conn2, 'last_add'), 'persistent')
        conn2.close()
        self.conn = _open_db(self.tmp_path)  # keep tearDown happy

    def test_open_db_adds_meta_to_legacy_db(self):
        """Simulate a pre-meta-table database: _open_db must create the table."""
        import sqlite3
        fd, legacy_path = tempfile.mkstemp(suffix='.sqlite')
        os.close(fd)
        try:
            # Create a db with only the old tables (no meta)
            legacy_conn = sqlite3.connect(legacy_path)
            legacy_conn.executescript("""
                CREATE TABLE IF NOT EXISTS songs (url TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS playlists (id TEXT PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS playlist_songs (playlist_id TEXT, url TEXT);
            """)
            legacy_conn.commit()
            legacy_conn.close()

            # _open_db should add the meta table without destroying existing data
            conn = _open_db(legacy_path)
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn('meta', tables)
            self.assertIn('songs', tables)
            conn.close()
        finally:
            if os.path.exists(legacy_path):
                os.unlink(legacy_path)


if __name__ == '__main__':
    unittest.main()
