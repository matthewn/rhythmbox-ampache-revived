#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unit tests for AmpacheBrowser SAX handlers and the XML cache roundtrip.

Run with:
    python3 -m unittest test_handlers
or:
    python3 test_handlers.py
"""

import os
import sys
import tempfile
import unittest
import xml.sax
from unittest.mock import MagicMock
from AmpacheBrowser import HandshakeHandler, PlaylistsHandler, SongsHandler

# ---------------------------------------------------------------------------
# Stub out gi and all GObject/Rhythmbox dependencies before importing handlers.
# The handlers themselves have no GObject logic, but AmpacheBrowser.py imports
# gi at module level, so we must satisfy those imports first.
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


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def parse_xml(handler, xml_string):
    """Parse an XML string with the given SAX content handler."""
    parser = xml.sax.make_parser()
    parser.setContentHandler(handler)
    if isinstance(xml_string, str):
        xml_string = xml_string.encode('utf-8')
    parser.feed(xml_string)


# ---------------------------------------------------------------------------
# HandshakeHandler
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
        self.handshake = {}
        parse_xml(HandshakeHandler(self.handshake), self.HANDSHAKE_XML)

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

    def test_multipart_text_node(self):
        """SAX may split a text node across multiple characters() calls;
        the value must be correctly reassembled."""
        # Simulate a very long auth value that expat might split
        long_auth = 'abcdef' * 50
        xml = f'<?xml version="1.0"?><root><auth>{long_auth}</auth></root>'
        handshake = {}
        parse_xml(HandshakeHandler(handshake), xml)
        self.assertEqual(handshake['auth'], long_auth)


# ---------------------------------------------------------------------------
# PlaylistsHandler
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
        self.playlists = []
        parse_xml(PlaylistsHandler(self.playlists, 'alice'), self.PLAYLISTS_XML)

    def _playlist(self, id_):
        return next(p for p in self.playlists if p[0] == id_)

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
        playlists = []
        parse_xml(PlaylistsHandler(playlists, 'alice'), xml)
        # items should remain at default 0
        self.assertEqual(playlists[0][2], 0)


# ---------------------------------------------------------------------------
# SongsHandler
# ---------------------------------------------------------------------------

# Auth tokens must be hex strings to match the regex [a-fA-F0-9]*
OLD_AUTH = 'aabbccddeeff0011'
NEW_AUTH = '1122334455667788'

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
        self.db = MagicMock()
        self.db.entry_lookup_by_location.return_value = None

        self.entry1 = MagicMock()
        self.entry2 = MagicMock()
        _RB.RhythmDBEntry.new.reset_mock()
        _RB.RhythmDBEntry.new.side_effect = [self.entry1, self.entry2]

        _GLib.Date.valid_year.return_value = True
        _GLib.Date.new_dmy.return_value.get_julian.return_value = 737060

        self.albumart = {}
        self.entries = []

        parse_xml(
            SongsHandler(
                False, None, self.db, MagicMock(),
                self.albumart, NEW_AUTH, self.entries),
            SONGS_XML)

    def _props(self, entry):
        """Return {prop_type: value} dict for all entry_set calls on entry."""
        result = {}
        for c in self.db.entry_set.call_args_list:
            if c.args[0] is entry:
                result[c.args[1]] = c.args[2]
        return result

    def test_two_entries_created(self):
        self.assertEqual(len(self.entries), 2)

    def test_title(self):
        self.assertEqual(self._props(self.entry1)[_RB.RhythmDBPropType.TITLE], 'Test Song')

    def test_artist(self):
        self.assertEqual(self._props(self.entry1)[_RB.RhythmDBPropType.ARTIST], 'Test Artist')

    def test_album(self):
        self.assertEqual(self._props(self.entry1)[_RB.RhythmDBPropType.ALBUM], 'Test Album')

    def test_genre(self):
        self.assertEqual(self._props(self.entry1)[_RB.RhythmDBPropType.GENRE], 'Rock')

    def test_track_number(self):
        self.assertEqual(self._props(self.entry1)[_RB.RhythmDBPropType.TRACK_NUMBER], 3)

    def test_duration(self):
        self.assertEqual(self._props(self.entry1)[_RB.RhythmDBPropType.DURATION], 245)

    def test_file_size(self):
        self.assertEqual(self._props(self.entry1)[_RB.RhythmDBPropType.FILE_SIZE], 8192000)

    def test_rating(self):
        self.assertEqual(self._props(self.entry1)[_RB.RhythmDBPropType.RATING], 4)

    def test_auth_token_replaced_in_url(self):
        url = _RB.RhythmDBEntry.new.call_args_list[0].args[2]
        self.assertIn(f'ssid={NEW_AUTH}', url)
        self.assertNotIn(OLD_AUTH, url)

    def test_auth_token_replaced_in_art(self):
        art_url = self.albumart.get('Test ArtistTest Album', '')
        self.assertIn(f'auth={NEW_AUTH}', art_url)
        self.assertNotIn(OLD_AUTH, art_url)

    def test_albumart_stored(self):
        self.assertIn('Test ArtistTest Album', self.albumart)

    def test_empty_artist_not_set(self):
        # entry2 (Minimal Song) has empty artist — entry_set should not be called for ARTIST
        props = self._props(self.entry2)
        self.assertNotIn(_RB.RhythmDBPropType.ARTIST, props)

    def test_empty_album_not_set(self):
        props = self._props(self.entry2)
        self.assertNotIn(_RB.RhythmDBPropType.ALBUM, props)

    def test_duplicate_entry_skipped(self):
        """If entry_lookup_by_location returns an existing entry, no new
        entry should be created and no properties should be set."""
        db = MagicMock()
        db.entry_lookup_by_location.return_value = MagicMock()
        _RB.RhythmDBEntry.new.reset_mock()
        entries = []
        parse_xml(
            SongsHandler(False, None, db, MagicMock(), {}, NEW_AUTH, entries),
            SONGS_XML)
        self.assertEqual(len(entries), 0)
        _RB.RhythmDBEntry.new.assert_not_called()

    def test_playlist_mode_calls_add_location(self):
        source = MagicMock()
        _RB.RhythmDBEntry.new.side_effect = None
        parse_xml(
            SongsHandler(True, source, self.db, MagicMock(), {}, NEW_AUTH, []),
            SONGS_XML)
        self.assertEqual(source.add_location.call_count, 2)

    def test_no_auth_leaves_url_unchanged(self):
        """With auth=None, URLs should pass through unmodified."""
        db = MagicMock()
        db.entry_lookup_by_location.return_value = None
        _RB.RhythmDBEntry.new.reset_mock()
        _RB.RhythmDBEntry.new.side_effect = [MagicMock(), MagicMock()]
        parse_xml(
            SongsHandler(False, None, db, MagicMock(), {}, None, []),
            SONGS_XML)
        url = _RB.RhythmDBEntry.new.call_args_list[0].args[2]
        self.assertIn(OLD_AUTH, url)


# ---------------------------------------------------------------------------
# Cache roundtrip
# ---------------------------------------------------------------------------

CACHE_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<root>
<song id="5001">
<title>Roundtrip Song</title>
<artist>Cache Artist</artist>
<album>Cache Album</album>
<tag>Jazz</tag>
<track>7</track>
<year>2019</year>
<time>180</time>
<size>6000000</size>
<rating>5</rating>
<url>http://example.com/cache.mp3</url>
<art>http://example.com/cache-art.jpg</art>
</song>
<song id="5002">
<title>Second Song</title>
<artist>Second Artist</artist>
<album>Second Album</album>
<track>2</track>
<time>210</time>
<url>http://example.com/second.mp3</url>
</song>
</root>
"""


class TestCacheRoundtrip(unittest.TestCase):
    """
    Verify that song data survives a write-then-read cycle through the XML
    cache format.  The 'write' side is a temporary file containing XML in the
    format that all_chunks_done produces; the 'read' side uses SongsHandler
    directly, mirroring what songs_loaded_cb does.
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            suffix='.xml', mode='wb', delete=False)
        self.tmp.write(CACHE_XML.encode('utf-8'))
        self.tmp.close()

        self.db = MagicMock()
        self.db.entry_lookup_by_location.return_value = None

        self.entry1 = MagicMock()
        self.entry2 = MagicMock()
        _RB.RhythmDBEntry.new.reset_mock()
        _RB.RhythmDBEntry.new.side_effect = [self.entry1, self.entry2]

        _GLib.Date.valid_year.return_value = True
        _GLib.Date.new_dmy.return_value.get_julian.return_value = 736695

        self.albumart = {}
        self.entries = []

        with open(self.tmp.name, 'rb') as f:
            contents = f.read()

        parser = xml.sax.make_parser()
        parser.setContentHandler(SongsHandler(
            False, None, self.db, MagicMock(),
            self.albumart, None, self.entries))
        parser.feed(contents)

    def tearDown(self):
        os.unlink(self.tmp.name)

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
            self._props(self.entry1)[_RB.RhythmDBPropType.TITLE],
            'Roundtrip Song')

    def test_artist_survives(self):
        self.assertEqual(
            self._props(self.entry1)[_RB.RhythmDBPropType.ARTIST],
            'Cache Artist')

    def test_album_survives(self):
        self.assertEqual(
            self._props(self.entry1)[_RB.RhythmDBPropType.ALBUM],
            'Cache Album')

    def test_genre_survives(self):
        self.assertEqual(
            self._props(self.entry1)[_RB.RhythmDBPropType.GENRE],
            'Jazz')

    def test_track_survives(self):
        self.assertEqual(
            self._props(self.entry1)[_RB.RhythmDBPropType.TRACK_NUMBER], 7)

    def test_duration_survives(self):
        self.assertEqual(
            self._props(self.entry1)[_RB.RhythmDBPropType.DURATION], 180)

    def test_file_size_survives(self):
        self.assertEqual(
            self._props(self.entry1)[_RB.RhythmDBPropType.FILE_SIZE], 6000000)

    def test_rating_survives(self):
        self.assertEqual(
            self._props(self.entry1)[_RB.RhythmDBPropType.RATING], 5)

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
            self._props(self.entry2)[_RB.RhythmDBPropType.TITLE],
            'Second Song')

    def test_second_song_url(self):
        url = _RB.RhythmDBEntry.new.call_args_list[1].args[2]
        self.assertEqual(url, 'http://example.com/second.mp3')


if __name__ == '__main__':
    unittest.main()
