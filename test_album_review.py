import contextlib
import io
import os
import tempfile
import textwrap
import sys
import types
import unittest
import asyncio
import inspect
import json
import threading
import time
from unittest.mock import AsyncMock, patch

requests_stub = types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None,
                                      delete=lambda *a, **k: None)
telegram_stub = types.ModuleType("telegram")
telegram_stub.InlineKeyboardButton = object
telegram_stub.InlineKeyboardMarkup = object
telegram_stub.Update = object
telegram_ext_stub = types.ModuleType("telegram.ext")
telegram_ext_stub.Application = types.SimpleNamespace(builder=lambda: None)
telegram_ext_stub.CallbackQueryHandler = object
telegram_ext_stub.CommandHandler = object
telegram_ext_stub.MessageHandler = object
telegram_ext_stub.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)
telegram_ext_stub.filters = types.SimpleNamespace(COMMAND=object())
sys.modules.setdefault("requests", requests_stub)
sys.modules.setdefault("telegram", telegram_stub)
sys.modules.setdefault("telegram.ext", telegram_ext_stub)

import listenbrainz_bot as bot


def swap_index(path, conn=None, *, close_old=True):
    """Point the shared library index at `path` and `conn`; return the (path, conn) it displaced.

    Every test that redirects or reopens library_index.db goes through here (Q-022). The file
    and the connection change in one `_index_lock` hold, and the displaced connection is closed
    inside it, because the review flusher is a daemon thread that can fire in the middle of any
    test and it uses the connection under that lock (`_review_groups_write`). Done piecemeal,
    without the lock, a teardown closed the connection mid-statement (an intermittent SIGSEGV),
    or the flusher opened a fresh connection between a reset connection and a restored path —
    into a temp dir about to be removed ("Directory not empty"), and leaked it to later tests.

    `close_old=False` keeps the displaced connection open, for entering a scratch index that
    will put the outer one back. A simulated restart (drop the connection, reopen the same
    file on the next `_index_db()`) is `swap_index(bot.LIBRARY_INDEX_FILE)`.
    """
    with bot._index_lock:
        old = bot.LIBRARY_INDEX_FILE, bot._index_conn
        bot.LIBRARY_INDEX_FILE, bot._index_conn = path, conn
        if close_old and old[1] is not None and old[1] is not conn:
            old[1].close()
    return old


def scratch_index(test, path):
    """Point the shared index at `path` for one test; the cleanup puts the old one back."""
    test.addCleanup(swap_index, *swap_index(path, close_old=False))


@contextlib.contextmanager
def isolated_review():
    """An empty review with its own JSON file and its own library_index.db.

    Review groups are rows in library_index.db, so a test that exercises the
    review's persistence needs both redirected — otherwise it writes into
    /config (absent here, so the group write fails) or into the real index.
    Yields the temp dir.
    """
    old_file = bot.REVIEW_FILE
    old_state = bot._review_snapshot()
    with tempfile.TemporaryDirectory() as td:
        bot.REVIEW_FILE = os.path.join(td, "review.json")
        old_index = swap_index(os.path.join(td, "index.db"), close_old=False)
        with bot._review_lock:
            bot._review_state = bot._empty_review_state()
            bot._review_dirty_groups.clear()
        bot._review_dirty.clear()
        try:
            yield td
        finally:
            # In this order (Q-022), because the flusher may fire mid-teardown:
            # aim its JSON write away from the temp dir, leave it no group to
            # write, and only then move the index — under the lock it writes under.
            bot.REVIEW_FILE = old_file
            with bot._review_lock:
                bot._review_dirty_groups.clear()
                bot._review_state = old_state
            bot._review_dirty.clear()
            swap_index(*old_index)


class IsolatedIndexTests(unittest.TestCase):
    """The scratch-index helpers themselves (Q-022)."""

    def test_teardown_waits_for_a_writer_holding_the_index_lock(self):
        """The review flusher is a daemon thread that can fire inside any test's
        teardown, and it holds `_index_lock` while it uses the connection
        (`_review_groups_write`). A teardown that closed the connection without
        that lock pulled it out from under the write: an intermittent SIGSEGV."""
        seen = {}
        held = threading.Event()

        def writer(conn):
            with bot._index_lock:
                held.set()
                time.sleep(0.2)          # the teardown runs meanwhile
                try:
                    conn.execute("SELECT 1").fetchone()
                    seen["ok"] = True
                except Exception as e:   # closed under us
                    seen["error"] = repr(e)

        with isolated_review():
            t = threading.Thread(target=writer, args=(bot._index_db(),))
            t.start()
            held.wait(5)
        t.join(5)
        self.assertEqual(seen, {"ok": True})


class AlbumReviewTests(unittest.TestCase):
    def test_conservative_duplicate_grouping(self):
        albums = [
            {"id": "a1", "artist": "Talk Talk", "name": "Spirit of Eden"},
            {"id": "a2", "artist": "Talk Talk", "name": "Spirit of Eden"},
            {"id": "a3", "artist": "Talk Talk", "name": "Laughing Stock"},
        ]
        groups = bot._bucket_duplicate_albums(albums, fuzzy=False)
        self.assertEqual([[a["id"] for a in g] for g in groups], [["a1", "a2"]])

    def test_fuzzy_duplicate_grouping_is_opt_in(self):
        albums = [
            {"id": "a1", "artist": "Artist", "name": "Album"},
            {"id": "a2", "artist": "Artist", "name": "Album Expanded Edition"},
        ]
        self.assertEqual(bot._bucket_duplicate_albums(albums, fuzzy=False), [])
        groups = bot._bucket_duplicate_albums(albums, fuzzy=True)
        self.assertEqual(len(groups), 1)

    @patch("listenbrainz_bot.mbz_release_tracks")
    def test_missing_tracks_use_union_of_duplicate_album_tracks(self, mock_tracks):
        mock_tracks.return_value = [
            {"title": "One", "mbid": "r1", "position": 1},
            {"title": "Two", "mbid": "r2", "position": 2},
            {"title": "Three", "mbid": "r3", "position": 3},
        ]
        records = [
            {"tracks": [{"title": "One", "musicBrainzId": "r1"}]},
            {"tracks": [{"title": "Two", "musicBrainzId": "r2"}]},
        ]
        info = bot._missing_for_album_records(records, "rel1", "Artist")
        self.assertEqual(info["present"], 2)
        self.assertEqual([t["title"] for t in info["missing"]], ["Three"])

    def test_retag_preview_blocks_paths_outside_music_mount(self):
        group = {
            "canonical_album_id": "canon",
            "canonical_mbid": "rel1",
            "albums": [
                {"id": "canon", "tracks": [{"path": "/music/Artist/Album/01.flac"}]},
                {"id": "dup", "tracks": [{"path": "/tmp/Other/02.flac"}]},
            ],
        }
        with patch.object(bot, "MUSIC_LIBRARY_PATH", "/music"):
            preview = bot.preview_group_retag(group)
        self.assertFalse(preview["ok"])
        self.assertIn("outside /music", preview["blocked"][0])

    def test_review_round_trip(self):
        """A group survives a save/restart: rows out of SQLite, the rest out of
        the JSON. The two halves have to come back together."""
        with isolated_review():
            with bot._review_lock:
                bot._review_state["groups"] = [
                    {"id": "g1", "artist": "A", "album": "B", "missing_tracks": []}]
                bot._review_state["searches"] = {"s1": {"id": "s1"}}
            bot._mark_review_groups_dirty(bot._review_state["groups"])
            # urgent: ordinary saves are coalesced by the background flusher,
            # so a round-trip assertion has to ask for the synchronous write.
            bot._save_review_state(urgent=True)
            with bot._review_lock:
                bot._review_state = bot._empty_review_state()
            bot._load_review_state()
            self.assertEqual(bot._review_snapshot()["groups"][0]["id"], "g1")
            self.assertIn("s1", bot._review_snapshot()["searches"])

    def test_groups_are_rows_not_json(self):
        """The JSON file carries no groups at all — that 18 MB blob, rewritten
        whole every couple of seconds, is what gave every group one blast
        radius when a scan replaced the list wrongly."""
        with isolated_review():
            with bot._review_lock:
                bot._review_state["groups"] = [
                    {"id": "g1", "artist": "A", "album": "B", "missing_tracks": []}]
            bot._mark_review_groups_dirty(bot._review_state["groups"])
            bot._save_review_state(urgent=True)
            with open(bot.REVIEW_FILE, encoding="utf-8") as fh:
                on_disk = json.load(fh)
            self.assertNotIn("groups", on_disk)
            self.assertNotIn("duplicate_groups", on_disk)
            self.assertEqual(bot._review_groups_count(), 1)

    def test_a_finished_scan_payload_is_not_written_to_disk(self):
        """A completed artist-discography task carries its whole scan result —
        12 MB for one prolific artist, 17.6 MB across the 109 of them in the
        live file. It is a hand-off to one polling browser tab, so it must not
        be rewritten to disk forever, nor ride the collection-wide
        /api/tasks response past the hub's 4 MB cap."""
        with isolated_review():
            with bot._review_lock:
                bot._review_state["tasks"] = {
                    "t1": {"id": "t1", "kind": "artist-discography",
                           "status": "complete", "summary": "done",
                           "result": {"releases": [{"rgid": "x"}] * 100}},
                    "t2": {"id": "t2", "kind": "source-search",
                           "status": "complete"},
                }
            on_disk = bot._review_state_for_disk()
            self.assertNotIn("result", on_disk["tasks"]["t1"])
            self.assertEqual(on_disk["tasks"]["t1"]["summary"], "done")
            self.assertNotIn("result", bot._tasks_snapshot()["t1"])
            # The live row keeps it, and the single-task read still serves it.
            self.assertIn("result", bot._review_state["tasks"]["t1"])
            served = bot._tasks_snapshot(include_result=True)["t1"]
            self.assertEqual(len(served["result"]["releases"]), 100)
            # ...as a copy: a caller must not reach back into live state.
            served["result"]["releases"].clear()
            self.assertEqual(
                len(bot._review_state["tasks"]["t1"]["result"]["releases"]), 100)

    def test_migration_from_a_pre_sqlite_review_file_runs_once(self):
        """A review file written before 2026-09-22 carries its groups inline.
        They are imported once and the key is dropped from the file, so a review
        the user has since emptied is not refilled on the next restart."""
        with isolated_review():
            legacy = dict(bot._empty_review_state())
            legacy["groups"] = [{"id": "old1", "artist": "A", "album": "B",
                                 "group_type": "incomplete", "missing_tracks": []}]
            legacy["duplicate_groups"] = [{"id": "dup1", "artist": "C", "album": "D",
                                           "group_type": "duplicate"}]
            with open(bot.REVIEW_FILE, "w", encoding="utf-8") as fh:
                json.dump(legacy, fh)

            bot._load_review_state()
            self.assertEqual([g["id"] for g in bot._review_state["groups"]], ["old1"])
            self.assertEqual([g["id"] for g in bot._review_state["duplicate_groups"]],
                             ["dup1"])
            self.assertEqual(bot._review_group_origin(bot._review_state["groups"][0]),
                             "library")
            self.assertEqual(bot._review_groups_count(), 2)

            # Restart with the review deliberately emptied.
            with bot._review_lock:
                bot._review_state["groups"] = []
                bot._review_state["duplicate_groups"] = []
            bot._mark_review_groups_dirty(["old1", "dup1"])
            bot._save_review_state(urgent=True)
            bot._load_review_state()
            self.assertEqual(bot._review_state["groups"], [])
            self.assertEqual(bot._review_groups_count(), 0)

    def test_review_save_is_coalesced_but_stamps_immediately(self):
        """An ordinary save defers the write; updated_at and the memo do not defer.

        The write is what costs ~110ms of _review_lock plus a multi-MB fsync, so
        it is coalesced. `updated_at` is read by the views and must reflect
        mutation time rather than flush time, so it stays eager — as does
        dropping the per-request snapshot memo.
        """
        with isolated_review():
            with bot._review_lock:
                bot._review_state["groups"] = [
                    {"id": "g9", "artist": "A", "album": "B", "missing_tracks": []}]
            bot._mark_review_groups_dirty(bot._review_state["groups"])
            bot._review_dirty.clear()

            # No flusher thread: prove the deferral without racing it.
            with patch.object(bot, "_ensure_review_flusher", lambda: None):
                bot._save_review_state()

            self.assertTrue(bot._review_dirty.is_set(),
                            "an ordinary save must mark the state dirty")
            self.assertFalse(os.path.exists(bot.REVIEW_FILE),
                             "an ordinary save must not write synchronously")
            self.assertEqual(bot._review_groups_count(), 0,
                             "an ordinary save must not write the group row either")
            self.assertGreater(bot._review_state["updated_at"], 0,
                               "updated_at must be stamped eagerly")

            # The flusher's body writes it, and clears the flag first so a
            # mutation arriving mid-dump is not dropped.
            bot._review_dirty.clear()
            bot._review_flush_now()
            self.assertTrue(os.path.exists(bot.REVIEW_FILE))
            self.assertEqual(bot._review_groups_count(), 1)

    def test_review_snapshot_is_a_copy_not_a_reference(self):
        """The read path releases _review_lock before parsing, and must still
        hand back an isolated copy — mutating the snapshot cannot reach the
        live state, and a later live mutation cannot reach a handed-out snap."""
        old_state = bot._review_snapshot()
        try:
            with bot._review_lock:
                bot._review_state = bot._empty_review_state()
                bot._review_state["groups"] = [{"id": "g1", "missing_tracks": []}]
            snap = bot._review_snapshot()
            snap["groups"][0]["id"] = "mutated"
            with bot._review_lock:
                self.assertEqual(bot._review_state["groups"][0]["id"], "g1")
                bot._review_state["groups"][0]["id"] = "changed-later"
            self.assertEqual(snap["groups"][0]["id"], "mutated")
        finally:
            with bot._review_lock:
                bot._review_state = old_state

    # ---- origin-scoped scans -------------------------------------------
    #
    # The bug these exist for: on 2026-09-20 a ListenBrainz playlist scan
    # replaced the whole review list with its own 97 groups, dropping a
    # ~3000-group library review. _store_review_groups documented that as
    # intentional ("callers are full-library rebuilds where groups is the
    # complete truth"), which was true of scan-all and of neither other caller.

    @staticmethod
    def _origin_group(gid, origin, artist="A", album="B", **extra):
        g = {"id": gid, "origin": origin, "artist": artist, "album": album,
             "artist_key": artist.lower(), "album_key": album.lower(),
             "canonical_album_id": "", "canonical_mbid": "", "merge_mode": "",
             "match_mode": "auto", "missing_tracks": [], "messages": []}
        g.update(extra)
        return g

    def test_playlist_scan_does_not_touch_library_groups(self):
        with isolated_review():
            with bot._review_lock:
                bot._review_state["groups"] = [
                    self._origin_group(f"lib{i}", "library", album=f"Album {i}")
                    for i in range(3)]
            bot._replace_review_groups(
                "playlist", [self._origin_group("pl1", "playlist", artist="P", album="Q")],
                "Playlist scan found 1 missing track(s)")
            groups = bot._review_state["groups"]
            self.assertEqual([g["id"] for g in groups],
                             ["lib0", "lib1", "lib2", "pl1"])

    def test_library_scan_does_not_touch_playlist_groups(self):
        with isolated_review():
            with bot._review_lock:
                bot._review_state["groups"] = [
                    self._origin_group("pl1", "playlist", artist="P", album="Q"),
                    self._origin_group("lib0", "library", album="Stale"),
                ]
            bot._replace_review_groups(
                "library", [self._origin_group("lib1", "library", album="Fresh")],
                "Found 1 album review group(s)")
            groups = bot._review_state["groups"]
            # The playlist row survives; the library row this scan did NOT
            # re-find is gone, which is what replace-within-an-origin means.
            self.assertEqual([g["id"] for g in groups], ["pl1", "lib1"])

    def test_a_scan_replaces_only_its_own_origin_repeatedly(self):
        """Two playlist scans in a row leave one playlist row, not two."""
        with isolated_review():
            for album in ("First", "Second"):
                bot._replace_review_groups(
                    "playlist", [self._origin_group("pl", "playlist", album=album)], "x")
            self.assertEqual([g["album"] for g in bot._review_state["groups"]],
                             ["Second"])

    def test_a_playlist_album_the_library_already_lists_folds_in(self):
        """Group ids are built per origin, so the same album reached two ways
        has two ids. Without folding, the rail shows it twice."""
        with isolated_review():
            lib = self._origin_group("lib0", "library", canonical_mbid="rel-1")
            lib["missing_tracks"] = [{"mbid": "t1", "title": "One",
                                      "decision": "approved"}]
            with bot._review_lock:
                bot._review_state["groups"] = [lib]
            incoming = self._origin_group("pl1", "playlist", canonical_mbid="rel-1")
            incoming["missing_tracks"] = [{"mbid": "t1", "title": "One"},
                                          {"mbid": "t2", "title": "Two"}]
            bot._replace_review_groups("playlist", [incoming], "x")

            groups = bot._review_state["groups"]
            self.assertEqual([g["id"] for g in groups], ["lib0"])
            self.assertEqual([t["title"] for t in groups[0]["missing_tracks"]],
                             ["One", "Two"])
            # The track the library group already had keeps the user's decision.
            self.assertEqual(groups[0]["missing_tracks"][0]["decision"], "approved")

    def test_a_scan_carries_the_hidden_decision_across(self):
        with isolated_review():
            with bot._review_lock:
                bot._review_state["groups"] = [
                    self._origin_group("lib0", "library", hidden=True)]
            bot._replace_review_groups(
                "library", [self._origin_group("lib0", "library")], "x")
            self.assertTrue(bot._review_state["groups"][0]["hidden"])

    def test_a_library_scan_keeps_stored_searches(self):
        """scan-all used to rebuild the state from _empty_review_state() and
        re-attach tasks/operations by hand — `searches` was not on that list."""
        with isolated_review():
            with bot._review_lock:
                bot._review_state["searches"] = {"s1": {"id": "s1", "query": "q"}}
            bot._replace_review_groups("library", [], "x")
            self.assertIn("s1", bot._review_state["searches"])

    def test_origin_is_inferred_for_groups_that_predate_the_field(self):
        infer = bot._review_group_origin
        self.assertEqual(infer({"group_type": "incomplete"}), "library")
        self.assertEqual(infer({"group_type": "duplicate"}), "library")
        self.assertEqual(infer({"group_type": "playlist"}), "playlist")
        self.assertEqual(infer({"group_type": "spotify"}), "spotify")
        self.assertEqual(infer({"group_type": "repair_job"}), "repair")
        self.assertEqual(infer({"group_type": "tracks",
                                "last_action": "spotify"}), "spotify")
        # An explicit field always wins over the inference.
        self.assertEqual(infer({"group_type": "incomplete",
                                "origin": "playlist"}), "playlist")

    def test_finding_a_group_marks_it_for_the_next_flush(self):
        """Every mutation site gets its group from _find_review_group and
        modifies it in place, so that fetch is what has to mark it."""
        with isolated_review():
            with bot._review_lock:
                bot._review_state["groups"] = [self._origin_group("g1", "library")]
                bot._review_dirty_groups.clear()
            group = bot._find_review_group("g1")
            group["hidden"] = True
            self.assertIn("g1", bot._review_dirty_groups)
            bot._save_review_state(urgent=True)

            with bot._review_lock:
                bot._review_state = bot._empty_review_state()
            bot._load_review_state()
            self.assertTrue(bot._review_state["groups"][0]["hidden"])

    def test_groups_survive_closing_the_database(self):
        """A real restart drops the SQLite connection too. WAL means the bytes
        are only in -wal until then, so reopening is the honest round trip."""
        with isolated_review():
            bot._replace_review_groups(
                "library", [self._origin_group("lib0", "library", album="Kept")], "x")
            bot._find_review_group("lib0")["hidden"] = True
            bot._save_review_state(urgent=True)

            swap_index(bot.LIBRARY_INDEX_FILE)
            with bot._review_lock:
                bot._review_state = bot._empty_review_state()
            bot._load_review_state()

            groups = bot._review_state["groups"]
            self.assertEqual([g["album"] for g in groups], ["Kept"])
            self.assertTrue(groups[0]["hidden"])
            self.assertEqual(groups[0]["origin"], "library")

    def test_a_group_dropped_from_the_review_loses_its_row(self):
        with isolated_review():
            bot._replace_review_groups(
                "library", [self._origin_group("lib0", "library")], "x")
            bot._save_review_state(urgent=True)
            self.assertEqual(bot._review_groups_count(), 1)
            bot._replace_review_groups("library", [], "x")
            bot._save_review_state(urgent=True)
            self.assertEqual(bot._review_groups_count(), 0)

    def test_a_scan_only_rewrites_rows_it_changed(self):
        """A five-album playlist scan must not rewrite the library's ~3000
        rows just because it keeps them."""
        with isolated_review():
            bot._replace_review_groups(
                "library",
                [self._origin_group(f"lib{i}", "library", album=f"L{i}",
                                    canonical_mbid=f"rel{i}") for i in range(50)],
                "x")
            bot._save_review_state(urgent=True)
            with bot._review_lock:
                bot._review_dirty_groups.clear()

            bot._replace_review_groups(
                "playlist",
                [self._origin_group("pl1", "playlist", artist="P", album="Q",
                                    canonical_mbid="plrel")], "x")
            # The one new playlist row, and nothing else.
            self.assertEqual(bot._review_dirty_groups, {"pl1"})

    def test_a_fold_marks_the_row_it_changed(self):
        with isolated_review():
            lib = self._origin_group("lib0", "library", canonical_mbid="rel-1")
            bot._replace_review_groups("library", [lib], "x")
            bot._save_review_state(urgent=True)
            with bot._review_lock:
                bot._review_dirty_groups.clear()

            incoming = self._origin_group("pl1", "playlist", canonical_mbid="rel-1")
            incoming["missing_tracks"] = [{"mbid": "t2", "title": "Two"}]
            bot._replace_review_groups("playlist", [incoming], "x")
            # Folded into lib0, so lib0 is what has to be rewritten.
            self.assertEqual(bot._review_dirty_groups, {"lib0"})
            bot._save_review_state(urgent=True)
            swap_index(bot.LIBRARY_INDEX_FILE)
            with bot._review_lock:
                bot._review_state = bot._empty_review_state()
            bot._load_review_state()
            self.assertEqual(
                [t["title"] for t in bot._review_state["groups"][0]["missing_tracks"]],
                ["Two"])

    # ---- playlist-scan grouping cost ------------------------------------
    #
    # Measured on the live stack 2026-09-22: ~13 s per track, for ~180 tracks
    # across 16 albums — a 40-minute playlist scan. Two compounding causes,
    # both present since the first commit, neither a rate limit:
    # nd_track_present was evaluated twice for every track, and each miss paid
    # _nd_search's 3 s warm-up retry on both the MBID and the text probe.

    @staticmethod
    def _rel(n):
        return [{"title": f"T{i}", "mbid": f"m{i}", "position": i}
                for i in range(1, n + 1)]

    def test_warm_probe_asks_for_something_the_library_actually_has(self):
        """The first version probed for "a" and got zero hits against a warm
        index — Navidrome does not match single-character searches — which
        would have left the slow path on forever with nothing to show for it.
        The term has to come from the library."""
        searched = []

        class _Resp:
            @staticmethod
            def json():
                return {"subsonic-response": {"albumList2": {
                    "album": [{"name": "Megapearl", "artist": "Reggie Pearl"}]}}}

        def fake_search(u, p, query, count=5, _retry=True):
            searched.append(query)
            return [{"title": "x"}] if query == "Megapearl" else []

        with patch.object(bot._http, "get", lambda *a, **k: _Resp()), \
             patch.object(bot, "_nd_search", fake_search):
            self.assertTrue(bot._nd_index_is_warm("u", "p"))
        self.assertEqual(searched, ["Megapearl"])

    def test_warm_probe_reports_cold_when_the_index_answers_nothing(self):
        class _Resp:
            @staticmethod
            def json():
                return {"subsonic-response": {"albumList2": {
                    "album": [{"name": "Megapearl"}]}}}

        with patch.object(bot._http, "get", lambda *a, **k: _Resp()), \
             patch.object(bot, "_nd_search", lambda *a, **k: []):
            self.assertFalse(bot._nd_index_is_warm("u", "p"))

    def test_warm_probe_on_an_empty_library_does_not_ask_for_retries(self):
        """No albums means every miss is honest; retrying each one buys
        nothing but 3 s."""
        class _Resp:
            @staticmethod
            def json():
                return {"subsonic-response": {"albumList2": {}}}

        with patch.object(bot._http, "get", lambda *a, **k: _Resp()):
            self.assertTrue(bot._nd_index_is_warm("u", "p"))

    def test_warm_probe_failure_keeps_the_slow_correct_path(self):
        def boom(*a, **k):
            raise RuntimeError("connection refused")

        with patch.object(bot._http, "get", boom):
            self.assertFalse(bot._nd_index_is_warm("u", "p"))

    def test_grouping_asks_navidrome_once_per_track(self):
        calls = []

        def fake_present(artist, title, mbid, nd_user, nd_pass, retry=True):
            calls.append((title, retry))
            return False

        tracks = [{"artist": "A", "title": "T1", "mbid": "m1"}]
        with patch.object(bot, "mbz_best_release",
                          lambda mbid: {"id": "rel1", "title": "Alb"}), \
             patch.object(bot, "mbz_release_tracks", lambda rid: self._rel(10)), \
             patch.object(bot, "_nd_index_is_warm", lambda u, p: True), \
             patch.object(bot, "nd_track_present", fake_present):
            groups, solo = bot.group_missing_by_album(tracks, "u", "p")

        # Ten tracks, ten lookups — not twenty.
        self.assertEqual(len(calls), 10)
        # And a warm index means no miss pays the 3 s warm-up retry.
        self.assertTrue(all(retry is False for _, retry in calls))
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["missing_tracks"]), 10)
        self.assertEqual(groups[0]["present_count"], 0)

    def test_grouping_keeps_retrying_when_the_index_looks_cold(self):
        """The retry exists for a warming index. If the probe finds nothing,
        misses are untrustworthy and the slow path is the correct one."""
        seen = []

        def fake_present(artist, title, mbid, nd_user, nd_pass, retry=True):
            seen.append(retry)
            return False

        with patch.object(bot, "mbz_best_release",
                          lambda mbid: {"id": "rel1", "title": "Alb"}), \
             patch.object(bot, "mbz_release_tracks", lambda rid: self._rel(2)), \
             patch.object(bot, "_nd_index_is_warm", lambda u, p: False), \
             patch.object(bot, "nd_track_present", fake_present):
            bot.group_missing_by_album([{"artist": "A", "title": "T1",
                                         "mbid": "m1"}], "u", "p")
        self.assertTrue(all(retry is True for retry in seen))

    def test_grouping_reuses_a_lookup_across_releases(self):
        calls = []

        def fake_present(artist, title, mbid, nd_user, nd_pass, retry=True):
            calls.append(title)
            return False

        # Two playlist tracks resolving to two different releases that share a
        # recording — the shared track must be looked up once.
        rel_of = {"m1": "relA", "m2": "relB"}
        tracks = [{"artist": "A", "title": "T1", "mbid": "m1"},
                  {"artist": "A", "title": "T2", "mbid": "m2"}]
        with patch.object(bot, "mbz_best_release",
                          lambda mbid: {"id": rel_of[mbid], "title": "Alb"}), \
             patch.object(bot, "mbz_release_tracks", lambda rid: self._rel(3)), \
             patch.object(bot, "_nd_index_is_warm", lambda u, p: True), \
             patch.object(bot, "nd_track_present", fake_present):
            bot.group_missing_by_album(tracks, "u", "p")
        # Two releases of the same three tracks: three lookups, not six.
        self.assertEqual(len(calls), 3)

    def test_a_present_track_is_not_reported_missing(self):
        """The refactor reads the present count and the missing list off one
        answer — they must still disagree in the right direction."""
        def fake_present(artist, title, mbid, nd_user, nd_pass, retry=True):
            return title in ("T1", "T2")

        with patch.object(bot, "mbz_best_release",
                          lambda mbid: {"id": "rel1", "title": "Alb"}), \
             patch.object(bot, "mbz_release_tracks", lambda rid: self._rel(4)), \
             patch.object(bot, "_nd_index_is_warm", lambda u, p: True), \
             patch.object(bot, "nd_track_present", fake_present):
            groups, _ = bot.group_missing_by_album(
                [{"artist": "A", "title": "T1", "mbid": "m1"}], "u", "p")
        self.assertEqual(groups[0]["present_count"], 2)
        self.assertEqual(groups[0]["total_tracks"], 4)
        self.assertEqual([t["title"] for t in groups[0]["missing_tracks"]],
                         ["T3", "T4"])

    def test_a_fully_present_album_is_skipped(self):
        with patch.object(bot, "mbz_best_release",
                          lambda mbid: {"id": "rel1", "title": "Alb"}), \
             patch.object(bot, "mbz_release_tracks", lambda rid: self._rel(3)), \
             patch.object(bot, "_nd_index_is_warm", lambda u, p: True), \
             patch.object(bot, "nd_track_present",
                          lambda *a, **k: True):
            groups, _ = bot.group_missing_by_album(
                [{"artist": "A", "title": "T1", "mbid": "m1"}], "u", "p")
        self.assertEqual(groups, [])

    def test_gaps_view_counts_per_origin(self):
        with isolated_review():
            with bot._review_lock:
                bot._review_state["groups"] = [
                    self._origin_group("lib0", "library", album="L0"),
                    self._origin_group("lib1", "library", album="L1"),
                    self._origin_group("pl1", "playlist", album="P1"),
                ]
            with patch.object(bot, "_nd_album_artist_map", lambda: {}):
                view = bot._gaps_view()
                self.assertEqual(view["origins"], {"library": 2, "playlist": 1})
                self.assertEqual(view["counts"]["all"], 3)
                only = bot._gaps_view(origin_filter="playlist")
                self.assertEqual([i["id"] for i in only["items"]], ["pl1"])
                # Counts stay whole-corpus so the chips can show every total.
                self.assertEqual(only["counts"]["all"], 3)

    def test_union_review_groups_preserves_dedups_and_carries_fields(self):
        old_file = bot.REVIEW_FILE
        old_state = bot._review_snapshot()

        def mk(gid, **extra):
            g = {"id": gid, "canonical_album_id": "", "merge_mode": "",
                 "match_mode": "auto", "missing_tracks": [], "messages": []}
            g.update(extra)
            return g

        try:
            with tempfile.TemporaryDirectory() as td:
                bot.REVIEW_FILE = os.path.join(td, "review.json")
                with bot._review_lock:
                    bot._review_state = bot._empty_review_state()
                    bot._review_state["groups"] = [
                        mk("A"),
                        mk("B", canonical_album_id="keepme", hidden=True),
                    ]
                bot._union_review_groups([mk("B"), mk("C"), mk("C")])
                groups = bot._review_snapshot()["groups"]
                # A untouched, B updated in place, C appended once (duplicate dropped)
                self.assertEqual([g["id"] for g in groups], ["A", "B", "C"])
                self.assertEqual(groups[1]["canonical_album_id"], "keepme")
                self.assertTrue(groups[1].get("hidden"))
        finally:
            bot.REVIEW_FILE = old_file
            with bot._review_lock:
                bot._review_state = old_state

    def test_union_leaves_a_group_it_did_not_cover_alone(self):
        """A union for one artist must not rebuild OTHER groups.

        Until B-022 the merge appended a repair-job projection for every group
        the scan did not cover, and the union swapped it in over the live group,
        so a source search lost its results within seconds of finishing whenever
        the auto-index worker unioned another artist (beabadoobee / Loveworm,
        2026-09-24). The jobs are gone; the property stays pinned.
        """
        old_file = bot.REVIEW_FILE
        old_state = bot._review_snapshot()
        live = {"id": "G", "origin": "library", "artist": "Band", "album": "Record",
                "canonical_album_id": "al1", "merge_mode": "", "match_mode": "auto",
                "missing_tracks": [{"title": "t1", "decision": "source_pending"}],
                "messages": [], "albums": [{"id": "al1"}],
                "source_results": {"folders": [{"username": "u", "folder": "f"}],
                                   "created_at": time.time()},
                "last_action": "source_search"}
        try:
            with tempfile.TemporaryDirectory() as td:
                bot.REVIEW_FILE = os.path.join(td, "review.json")
                with bot._review_lock:
                    bot._review_state = bot._empty_review_state()
                    bot._review_state["groups"] = [live]
                bot._union_review_groups([{
                    "id": "OTHER", "canonical_album_id": "", "merge_mode": "",
                    "match_mode": "auto", "missing_tracks": [], "messages": []}])
                groups = {g["id"]: g for g in bot._review_snapshot()["groups"]}
                self.assertEqual(set(groups), {"G", "OTHER"})
                self.assertEqual(groups["G"]["origin"], "library")
                self.assertEqual(groups["G"]["last_action"], "source_search")
                self.assertEqual(len(groups["G"]["source_results"]["folders"]), 1)
                self.assertEqual(groups["G"]["albums"], [{"id": "al1"}])
        finally:
            bot.REVIEW_FILE = old_file
            with bot._review_lock:
                bot._review_state = old_state

    def test_library_index_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            old_index = swap_index(os.path.join(td, "index.db"), close_old=False)
            try:
                result = {"artist_mbid": "amb", "artist_name": "Artist",
                          "releases": [
                              {"rgid": "rg1", "title": "A", "year": "2000",
                               "primary_type": "album", "status": "incomplete",
                               "group_id": "g1", "present": 8, "total": 10,
                               "match_method": "mbid", "match_score": 1.0},
                              {"rgid": "rg2", "title": "B", "year": "2001",
                               "primary_type": "ep", "status": "missing"},
                          ]}
                bot._index_store_artist(result, nd_artist_id="nd9")
                got = bot._index_get_artist("amb")
                self.assertTrue(got)
                self.assertFalse(got["stale"])
                rows = {r["rgid"]: r for r in got["releases"]}
                self.assertEqual(rows["rg1"]["present"], 8)
                self.assertEqual(rows["rg1"]["group_id"], "g1")
                self.assertEqual(rows["rg2"]["status"], "missing")
                # Navidrome-id lookup resolves the same artist.
                self.assertIsNotNone(bot._index_get_artist(nd_artist_id="nd9"))
                # Re-store is idempotent (delete+reinsert, no row growth).
                bot._index_store_artist(result, nd_artist_id="nd9")
                self.assertEqual(len(bot._index_get_artist("amb")["releases"]), 2)
                self.assertIsNone(bot._index_get_artist("unknown"))
            finally:
                swap_index(*old_index)

    # ── discography matching (MBID-first + safe fuzzy) ──────────────────────
    def _disco(self, rgs, albums, rg_of=None):
        """Run build_artist_discography with all network layers mocked.
        rg_of maps a release mbid -> its release-group id."""
        with patch("listenbrainz_bot.mbz_artist_release_groups", return_value=rgs), \
             patch("listenbrainz_bot.nd_get_all_albums", return_value=albums), \
             patch("listenbrainz_bot.nd_get_album_tracks", return_value=[]), \
             patch("listenbrainz_bot.mbz_release_tracks", return_value=[]), \
             patch("listenbrainz_bot.mbz_release_group_of",
                   side_effect=lambda m: (rg_of or {}).get(m, "")), \
             patch("listenbrainz_bot.mbz_resolve_album",
                   return_value={"release_mbid": ""}):
            result = bot.build_artist_discography("amb", "Artist", "u", "p")
        return {r["rgid"]: r for r in result["releases"]}

    @staticmethod
    def _rg(rgid, title):
        return {"rgid": rgid, "title": title, "year": "2000",
                "primary_type": "album", "secondary_types": [],
                "first_release_date": "2000-01-01"}

    def test_disco_mbid_match_wins_despite_title_divergence(self):
        rows = self._disco(
            [self._rg("rg1", "Completely Different Title")],
            [{"id": "a1", "artist": "Artist", "name": "Weird Local Name",
              "musicBrainzId": "rel1", "songCount": 10}],
            rg_of={"rel1": "rg1"})
        self.assertEqual(rows["rg1"]["status"], "complete")
        self.assertEqual(rows["rg1"]["match_method"], "mbid")

    def test_disco_sibling_titles_do_not_double_claim(self):
        rows = self._disco(
            [self._rg("rg1", "X"), self._rg("rg2", "X Live")],
            [{"id": "a1", "artist": "Artist", "name": "X", "musicBrainzId": ""}])
        self.assertEqual(rows["rg1"]["status"], "untagged")   # claimed by title
        self.assertEqual(rows["rg2"]["status"], "missing")    # not stolen

    def test_disco_no_cross_artist_fallback(self):
        # Library has only another artist's albums with an identical title —
        # nothing may match; everything is honestly "missing".
        rows = self._disco(
            [self._rg("rg1", "Selfsame Album")],
            [{"id": "a1", "artist": "Other Guy", "name": "Selfsame Album",
              "musicBrainzId": "rel1"}])
        self.assertEqual(rows["rg1"]["status"], "missing")

    def test_disco_title_threshold(self):
        long = "abcdefghijklmnopqrst"
        near = long[:-1] + "x"          # ratio 0.95 -> matches
        rows = self._disco(
            [self._rg("rg1", long), self._rg("rg2", "zzzz")],
            [{"id": "a1", "artist": "Artist", "name": near, "musicBrainzId": ""},
             {"id": "a2", "artist": "Artist", "name": "zzqqqq", "musicBrainzId": ""}])
        self.assertEqual(rows["rg1"]["status"], "untagged")
        self.assertEqual(rows["rg2"]["status"], "missing")

    def test_disco_known_foreign_mbid_never_title_matches(self):
        # Album's mbid resolves to a release-group outside this discography
        # (e.g. a compilation) — identical title must NOT claim the studio RG.
        rows = self._disco(
            [self._rg("rg1", "Greatest Hits")],
            [{"id": "a1", "artist": "Artist", "name": "Greatest Hits",
              "musicBrainzId": "rel-comp"}],
            rg_of={"rel-comp": "some-other-rg"})
        self.assertEqual(rows["rg1"]["status"], "missing")

    @patch("listenbrainz_bot.mbz_release_tracks")
    def test_all_album_review_includes_single_incomplete_album(self, mock_tracks):
        mock_tracks.return_value = [
            {"title": "One", "mbid": "r1", "position": 1},
            {"title": "Two", "mbid": "r2", "position": 2},
        ]
        albums = [{"id": "a1", "artist": "Artist", "name": "Album", "musicBrainzId": "rel1"}]
        songs = [{"title": "One", "musicBrainzId": "r1", "path": "Artist/Album/01.flac"}]
        with patch("listenbrainz_bot.nd_get_all_albums", return_value=albums), \
             patch("listenbrainz_bot.nd_get_album_tracks", return_value=songs):
            groups = bot.build_all_incomplete_album_review("u", "p")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["group_type"], "incomplete")
        self.assertEqual(groups[0]["missing_tracks"][0]["title"], "Two")

    def test_source_page_slices_ten_results(self):
        group = {
            "source_results": {
                "mode": "album",
                "query": "Artist Album",
                "created_at": 1,
                "folders": [
                    {"username": f"user{i}", "folder": f"Artist/Album {i}",
                     "files": [{"filename": f"{i}.flac"}]}
                    for i in range(25)
                ],
            }
        }
        page = bot._group_source_page(group, page=2)
        self.assertEqual(page["page"], 2)
        self.assertEqual(page["pages"], 3)
        self.assertEqual(len(page["sources"]), 5)
        self.assertEqual(page["sources"][0]["index"], 20)

    def _source_group(self, created_at, folders=1):
        return {
            "id": "g1", "artist": "Artist", "album": "Album",
            "canonical_mbid": "rel1",
            "missing_tracks": [{"title": "One", "artist": "Artist",
                                "decision": "approved"}],
            "source_results": {
                "mode": "album", "query": "Artist Album",
                "created_at": created_at,
                "folders": [{"username": "u", "folder": "Artist/Album",
                             "files": [{"filename": "One.flac"}]}] * folders,
            },
        }

    def test_recent_source_results_are_reusable(self):
        group = self._source_group(time.time() - 5)
        self.assertIsNotNone(bot._reusable_source_results(group))

    def test_source_results_expire_after_ttl(self):
        group = self._source_group(time.time() - bot.SOURCE_RESULTS_TTL - 1)
        self.assertIsNone(bot._reusable_source_results(group))

    def test_empty_source_results_are_not_reusable(self):
        group = self._source_group(time.time(), folders=0)
        self.assertIsNone(bot._reusable_source_results(group))

    @patch("listenbrainz_bot.slskd_search_album_folders")
    def test_fresh_results_skip_the_slskd_search(self, mock_search):
        """The whole point of the TTL: coming back to an album must not re-search."""
        group = self._source_group(time.time() - 5)
        bot._review_state["groups"] = [group]
        try:
            result = bot._run_group_source_search("t1", "g1")
        finally:
            bot._review_state["groups"] = []
        self.assertTrue(result["ok"])
        self.assertTrue(result["cached"])
        mock_search.assert_not_called()

    @patch("listenbrainz_bot.slskd_search_album_folders")
    def test_force_re_runs_the_search_despite_fresh_results(self, mock_search):
        mock_search.return_value = [
            {"username": "v", "folder": "Other/Album", "files": [{"filename": "One.flac"}]}
        ]
        group = self._source_group(time.time() - 5)
        bot._review_state["groups"] = [group]
        try:
            result = bot._run_group_source_search("t1", "g1", force=True)
        finally:
            bot._review_state["groups"] = []
        self.assertTrue(result["ok"])
        self.assertFalse(result.get("cached"))
        mock_search.assert_called_once()

    @patch("listenbrainz_bot.slskd_search_album_folders")
    def test_results_discarded_when_tracklist_changes_mid_search(self, mock_search):
        """A rescan during the search invalidates the coverage it was ranked on."""
        group = {
            "id": "g1", "artist": "Artist", "album": "Album",
            "canonical_mbid": "rel1", "updated_at": 1,
            "missing_tracks": [{"title": "One", "artist": "Artist",
                                "decision": "approved"}],
        }
        bot._review_state["groups"] = [group]

        def rescan(*a, **k):
            # Stands in for _merge_review_groups landing a new tracklist while
            # the search was in flight.
            group["missing_tracks"].append({"title": "Two", "decision": "approved"})
            group["updated_at"] = 2
            return [{"username": "u", "folder": "Artist/Album",
                     "files": [{"filename": "One.flac"}]}]

        mock_search.side_effect = rescan
        try:
            result = bot._run_group_source_search("t1", "g1")
        finally:
            bot._review_state["groups"] = []
        self.assertFalse(result["ok"])
        self.assertIn("changed during the search", result["message"])
        self.assertNotIn("source_results", group)

    def test_source_search_claim_is_exclusive_per_group(self):
        with bot._source_search_claim("g1") as first:
            self.assertTrue(first)
            with bot._source_search_claim("g1") as second:
                self.assertFalse(second)
            # A different album is unaffected.
            with bot._source_search_claim("g2") as other:
                self.assertTrue(other)
        # Released on exit, so the next search can run.
        with bot._source_search_claim("g1") as again:
            self.assertTrue(again)

    def test_stored_group_drops_the_slskd_file_payload(self):
        group = self._source_group(time.time())
        group["source_results"]["folders"][0]["_expanded"] = [{"filename": "x.flac"}]
        fd = bot._review_group_for_disk(group)["source_results"]["folders"][0]
        self.assertNotIn("files", fd)
        self.assertNotIn("_expanded", fd)
        # Ranking metadata survives, and the live object is untouched.
        self.assertEqual(fd["username"], "u")
        self.assertEqual(len(group["source_results"]["folders"][0]["files"]), 1)

    def test_peer_wide_locked_count_does_not_zero_a_folder(self):
        """slskd's lockedFileCount is peer-wide and a response's `files` are the
        unlocked ones, so comparing the two zeroed every peer that locks any part
        of its share — against the handful of files it returned for this query.
        That is a search coming back empty from peers offering what was asked."""
        folder = {"files": [{"filename": f"{i:02d}.flac", "size": 1} for i in range(10)],
                  "raw_file_count": 10, "upload_speed": 1_000_000,
                  "has_free_upload_slot": True, "queue_length": 0,
                  "locked_file_count": 4000}   # peer-wide: irrelevant here
        self.assertGreater(bot._score_folder(folder, 10), 0)

    def test_locked_files_in_this_folder_still_count(self):
        folder = {"files": [{"filename": f"{i:02d}.flac", "size": 1} for i in range(10)],
                  "raw_file_count": 10, "upload_speed": 1_000_000,
                  "has_free_upload_slot": True, "queue_length": 0,
                  "locked_file_count": 4000, "locked_in_folder": 9}
        self.assertEqual(bot._score_folder(folder, 10), 0)

    def test_poll_waits_for_completion_because_partial_reads_return_nothing(self):
        """Traced against the live instance: /responses and ?includeResponses
        both returned an empty array at 5s, 16s, 26s and 35s while responseCount
        climbed to 143, and both returned all 142 the instant the state reached
        "Completed, TimedOut". There is no partial result to leave early with, so
        the poll must not exit on peer count alone."""
        peer = {"username": "good", "uploadSpeed": 1_000_000,
                "hasFreeUploadSlot": True, "queueLength": 0,
                "files": [{"filename": f"m\\\\Artist - Album\\\\{i:02d}.flac", "size": 1}
                          for i in range(10)]}
        state = {"polls": 0}

        class _R:
            ok = True
            status_code = 200
            text = ""

            def __init__(self, payload):
                self._p = payload

            def json(self):
                return self._p

        def _get(url, **k):
            if url.endswith("/responses"):
                # Empty until complete, exactly as slskd behaves.
                return _R([peer] if state["polls"] >= 4 else [])
            state["polls"] += 1
            return _R({"state": "InProgress" if state["polls"] < 4 else "Completed, TimedOut",
                       "isComplete": state["polls"] >= 4,
                       "responseCount": 143, "fileCount": 1320})

        with patch.object(bot, "_http") as http:
            http.post.return_value = _R({"id": "s1"})
            http.get.side_effect = _get
            http.put.return_value = _R({})
            http.delete.return_value = _R({})
            with patch.object(bot, "SEARCH_MIN_WAIT", 0), \
                 patch.object(bot, "SEARCH_POLL_INT", 0), \
                 patch.object(bot, "SEARCH_TIMEOUT", 30):
                stats = {}
                folders = bot.slskd_run_search("Artist Album", 10, stats=stats)
        # 143 peers were visible from the first poll; exiting there would have
        # read an empty array. It kept polling to completion instead.
        self.assertGreaterEqual(state["polls"], 4)
        self.assertEqual([f["username"] for f in folders], ["good"])
        # ...and asked slskd to wrap the search up rather than just waiting.
        http.put.assert_called()

    def test_responses_are_refetched_when_slskd_has_not_published_them(self):
        """responseCount ticks up live, but the responses endpoint only serves
        them once the search has finished. Exiting the poll as soon as enough
        peers had answered asked for results slskd had counted but not yet
        published, and got an empty array — 140 peers, then no sources."""
        calls = {"responses": 0, "status": 0}
        peer = {"username": "good", "uploadSpeed": 1_000_000,
                "hasFreeUploadSlot": True, "queueLength": 0,
                "files": [{"filename": f"m\\\\Artist - Album\\\\{i:02d}.flac", "size": 1}
                          for i in range(10)]}

        class _R:
            ok = True
            status_code = 200
            text = ""

            def __init__(self, payload):
                self._p = payload

            def json(self):
                return self._p

        def _get(url, **k):
            if url.endswith("/responses"):
                calls["responses"] += 1
                # Empty until the search completes, exactly as slskd behaves.
                return _R([] if calls["responses"] == 1 else [peer])
            calls["status"] += 1
            # In progress with peers counted, then Completed on the next look.
            return _R({"state": "InProgress" if calls["status"] == 1 else "Completed",
                       "responseCount": 140, "fileCount": 2000})

        with patch.object(bot, "_http") as http:
            http.post.return_value = _R({"id": "s1"})
            http.get.side_effect = _get
            http.delete.return_value = _R({})
            with patch.object(bot, "SEARCH_MIN_WAIT", 0), \
                 patch.object(bot, "SEARCH_POLL_INT", 0), \
                 patch.object(bot, "SEARCH_SETTLE_TIMEOUT", 5):
                stats = {}
                folders = bot.slskd_run_search("Artist Album", 10, stats=stats)
        self.assertEqual(calls["responses"], 2)          # asked again after settling
        self.assertEqual([f["username"] for f in folders], ["good"])
        self.assertEqual(stats["peers"], 1)

    def test_counted_but_unpublished_peers_are_named_as_such(self):
        r = bot._no_source_reason({"peers": 0, "counted_peers": 140})
        self.assertIn("140", r)
        self.assertNotIn("No peer answered", r)
        self.assertEqual(bot._no_source_reason({"peers": 0}),
                         "No peer answered the search")

    def test_one_peer_with_null_files_does_not_sink_the_whole_search(self):
        """slskd sends "files": null for a peer whose only hits were locked.
        `for f in None` raised out of the response loop, so a single such peer
        discarded every other peer's results and the search reported nothing."""
        responses = [
            {"username": "locked_only", "files": None, "lockedFiles": [
                {"filename": "x\\\\Album\\\\01.flac", "size": 1}]},
            {"username": "good", "uploadSpeed": 1_000_000,
             "hasFreeUploadSlot": True, "queueLength": 0, "lockedFileCount": 4000,
             "files": [{"filename": f"m\\\\Artist - Album\\\\{i:02d}.flac", "size": 1}
                       for i in range(10)]},
            "not even a dict",
        ]

        class _R:
            ok = True
            status_code = 200
            text = ""

            def __init__(self, payload):
                self._p = payload

            def json(self):
                return self._p

        posted = _R({"id": "s1"})
        status = _R({"state": "Completed", "responseCount": 2, "fileCount": 10})
        with patch.object(bot, "_http") as http:
            http.post.return_value = posted
            http.get.side_effect = lambda url, **k: (
                _R(responses) if url.endswith("/responses") else status)
            http.delete.return_value = _R({})
            stats = {}
            folders = bot.slskd_run_search("Artist Album", 10, stats=stats)
        self.assertEqual(stats["error"], "")
        self.assertEqual([f["username"] for f in folders], ["good"])

    def test_a_failed_call_is_not_reported_as_an_empty_library(self):
        """Every early return in slskd_run_search left `stats` untouched, so a
        search that had just counted 132 peers and then failed to fetch the
        results was reported as "No peer answered the search"."""
        r = bot._no_source_reason({"peers": 132, "files": 4001,
                                   "error": "slskd counted 132 peer(s) but returned "
                                            "HTTP 500 for the results"})
        self.assertIn("132 peer(s) had answered", r)
        self.assertIn("HTTP 500", r)
        self.assertNotIn("No peer answered", r)
        # No peers *and* an error still leads with the error, not the count.
        r = bot._no_source_reason({"peers": 0, "error": "ReadTimeout: timed out"})
        self.assertIn("ReadTimeout", r)

    def test_empty_search_says_why(self):
        """"No source found" right after "103 peers, 2,047 files" reads as a bug
        in the bot. The reason is known at the point the result is discarded."""
        r = bot._no_source_reason({"peers": 0})
        self.assertIn("No peer answered", r)
        r = bot._no_source_reason({"peers": 103, "files": 0})
        self.assertIn("none offered any files", r)
        # Everything rejected on format — the common case, and the one the MP3
        # opt-in exists for.
        r = bot._no_source_reason({"peers": 103, "files": 2047, "folders": 0,
                                   "rejected_formats": ["m4a", "mp3"],
                                   "accepted_formats": ["flac", "opus"]})
        self.assertIn("2,047", r)
        self.assertIn("FLAC, OPUS", r)
        self.assertIn("mp3", r)
        # Folders survived the format filter but not the locked/availability cut.
        r = bot._no_source_reason({"peers": 103, "files": 2047, "folders": 41,
                                   "usable": 0, "accepted_formats": ["flac"]})
        self.assertIn("locked", r)

    def test_no_source_reason_picks_the_most_informative_pass_whole(self):
        """Q-013 (Q-017(b) follow-up): `stats` only ever carries the one pass
        that "won" _publish's peer-count comparison. A narrow first pass that
        found real (rejected) mp3 evidence used to vanish the moment a wider,
        emptier pass took over the top-level fields -- the reason then claimed
        no files were even offered, and mp3_would_help read False for an album
        that plainly had an mp3 copy on the network.

        The fix reads one whole pass (peers, files, folders, rejected_formats
        together), not the max of each field independently -- so the sentence
        never states a peer count from one pass alongside a file count from
        another."""
        stats = {
            # The "winning" pass: more peers, but they offered nothing at all.
            "peers": 8, "files": 0, "folders": 0, "rejected_formats": [],
            "accepted_formats": ["flac", "opus"],
            "pass_stats": [
                {"peers": 5, "files": 5, "folders": 0,
                 "rejected_formats": ["mp3"]},
                {"peers": 8, "files": 0, "folders": 0, "rejected_formats": []},
            ],
        }
        r = bot._no_source_reason(stats)
        self.assertIn("5 file(s)", r)
        self.assertIn("mp3", r)
        self.assertNotIn("none offered any files", r)
        # The mix this test exists to catch: independently-widened fields
        # would report the *winning* pass's 8 peers alongside the *other*
        # pass's 5 files. The chosen pass's own peer count must appear, and
        # the winning pass's must not.
        self.assertIn("5 peer(s)", r)
        self.assertNotIn("8 peer(s)", r)

    def test_no_source_reason_widens_folders_to_the_locked_branch(self):
        """A narrower pass found folders that were locked/unavailable; a wider
        pass that "won" on peer count saw none at all. Reporting the winner's
        bare `folders: 0` said "none in an accepted format" -- wrong, some
        were, they were just locked. And the sentence must quote that pass's
        own peer count, not the winner's."""
        stats = {
            "peers": 10, "files": 20, "folders": 0, "rejected_formats": [],
            "accepted_formats": ["flac"],
            "pass_stats": [
                {"peers": 4, "files": 20, "folders": 6, "rejected_formats": []},
                {"peers": 10, "files": 20, "folders": 0, "rejected_formats": []},
            ],
        }
        r = bot._no_source_reason(stats)
        self.assertIn("locked", r)
        self.assertIn("4 peer(s)", r)
        self.assertNotIn("10 peer(s)", r)

    def test_pass_stats_accumulates_every_pass_without_losing_the_winner(self):
        """slskd_run_search's _publish keeps whichever pass "got further" for
        the top-level fields (unchanged), but must not drop the other passes'
        own accounting -- that per-pass list is what _no_source_reason now
        reads to stay truthful across a widening search."""
        stats = {}

        class _R:
            ok = True
            status_code = 200
            text = ""

            def __init__(self, payload):
                self._p = payload

            def json(self):
                return self._p

        def make_http(peer_count, file_ext):
            peer = {"username": "u", "uploadSpeed": 1_000_000,
                    "hasFreeUploadSlot": True, "queueLength": 0,
                    "files": [{"filename": f"m\\\\A\\\\{i:02d}.{file_ext}", "size": 1}
                              for i in range(peer_count)]}

            def _get(url, **k):
                if url.endswith("/responses"):
                    return _R([peer])
                return _R({"state": "Completed", "responseCount": 1, "fileCount": peer_count})

            http = types.SimpleNamespace(
                post=lambda *a, **k: _R({"id": "s1"}),
                get=_get,
                put=lambda *a, **k: _R({}),
                delete=lambda *a, **k: _R({}))
            return http

        with patch.object(bot, "_http", make_http(5, "mp3")), \
             patch.object(bot, "SEARCH_MIN_WAIT", 0), \
             patch.object(bot, "SEARCH_POLL_INT", 0), \
             patch.object(bot, "SEARCH_TIMEOUT", 5):
            bot.slskd_run_search("Artist Album", 10, stats=stats)
        first_pass_count = len(stats["pass_stats"])
        self.assertEqual(first_pass_count, 1)
        self.assertEqual(stats["pass_stats"][0]["rejected_formats"], ["mp3"])

        with patch.object(bot, "_http", make_http(0, "mp3")), \
             patch.object(bot, "SEARCH_MIN_WAIT", 0), \
             patch.object(bot, "SEARCH_POLL_INT", 0), \
             patch.object(bot, "SEARCH_TIMEOUT", 5):
            bot.slskd_run_search("Artist Album broader", 10, stats=stats)
        # A second, emptier pass must not erase the first pass's history.
        self.assertEqual(len(stats["pass_stats"]), 2)
        self.assertEqual(stats["pass_stats"][0]["rejected_formats"], ["mp3"])

    @patch("listenbrainz_bot.slskd_search_album_folders")
    def test_mp3_optin_agrees_with_the_reason_across_passes(self, mock_search):
        """Q-013 follow-up: `_apply_group_sources` computed `mp3_would_help`
        straight off the top-level `rejected_formats`, so it disagreed with
        `_no_source_reason` (which now reads `pass_stats`) the moment an
        earlier pass was the only one to see an mp3 copy -- the reason said
        "(they were mp3)" while mp3_would_help stayed False and the caller had
        no Allow-MP3 retry to offer for exactly the case it exists for."""
        def search(artist, album, expected, progress=None, stats=None, **kw):
            # The "winning" top-level fields see nothing at all; only an
            # earlier, narrower pass (preserved in pass_stats) saw the mp3s.
            stats.update({
                "peers": 8, "files": 0, "folders": 0, "rejected_formats": [],
                "accepted_formats": ["flac", "opus"],
                "pass_stats": [
                    {"peers": 5, "files": 5, "folders": 0,
                     "rejected_formats": ["mp3"]},
                    {"peers": 8, "files": 0, "folders": 0, "rejected_formats": []},
                ],
            })
            return []

        mock_search.side_effect = search
        group = {"id": "g1", "artist": "Artist", "album": "Album",
                 "canonical_mbid": "rel1",
                 "missing_tracks": [{"title": "One", "decision": "approved"}]}
        bot._review_state["groups"] = [group]
        try:
            result = bot._run_group_source_search("t1", "g1")
        finally:
            bot._review_state["groups"] = []
        self.assertFalse(result["ok"])
        self.assertIn("mp3", group["no_source_reason"])
        # The reason and the opt-in must not contradict each other.
        self.assertTrue(group["mp3_would_help"])

    @patch("listenbrainz_bot.slskd_search_album_folders")
    def test_empty_search_offers_the_mp3_optin(self, mock_search):
        def search(artist, album, expected, progress=None, stats=None, **kw):
            stats.update({"peers": 12, "files": 300, "folders": 0,
                          "rejected_formats": ["mp3"],
                          "accepted_formats": ["flac", "opus"]})
            return []

        mock_search.side_effect = search
        group = {"id": "g1", "artist": "Artist", "album": "Album",
                 "canonical_mbid": "rel1",
                 "missing_tracks": [{"title": "One", "decision": "approved"}]}
        bot._review_state["groups"] = [group]
        try:
            result = bot._run_group_source_search("t1", "g1")
        finally:
            bot._review_state["groups"] = []
        self.assertFalse(result["ok"])
        self.assertIn("none in FLAC, OPUS", result["message"])
        self.assertTrue(group["mp3_would_help"])
        self.assertIn("mp3", group["no_source_reason"])

    @patch("listenbrainz_bot.slskd_search_album_folders")
    def test_successful_search_clears_the_previous_reason(self, mock_search):
        mock_search.return_value = [
            {"username": "u", "folder": "Artist/Album", "files": [{"filename": "One.flac"}]}
        ]
        group = {"id": "g1", "artist": "Artist", "album": "Album",
                 "canonical_mbid": "rel1",
                 "no_source_reason": "stale", "mp3_would_help": True,
                 "missing_tracks": [{"title": "One", "decision": "approved"}]}
        bot._review_state["groups"] = [group]
        try:
            self.assertTrue(bot._run_group_source_search("t1", "g1")["ok"])
        finally:
            bot._review_state["groups"] = []
        self.assertNotIn("no_source_reason", group)
        self.assertNotIn("mp3_would_help", group)

    def test_source_pending_tracks_are_re_approvable(self):
        """A finished search leaves every track source_pending. If that state is
        not re-approvable, an album whose results were later lost (a restart, the
        TTL) can never be searched again: the plan finds nothing approved and the
        album is stranded on "choosing a source" with no sources to choose."""
        group = {"id": "g1", "artist": "Artist", "album": "Album",
                 "canonical_mbid": "rel1",
                 "missing_tracks": [{"title": "One", "decision": "source_pending"}]}
        self.assertEqual(bot._approve_pending_missing_tracks(group), 1)
        self.assertEqual(group["missing_tracks"][0]["decision"], "approved")
        self.assertTrue(bot._group_source_plan(group)["ok"])

    def test_settled_and_inflight_decisions_are_still_left_alone(self):
        group = {"id": "g1",
                 "missing_tracks": [{"title": "A", "decision": "placed"},
                                    {"title": "B", "decision": "verified"},
                                    {"title": "C", "decision": "skipped"}]}
        self.assertEqual(bot._approve_pending_missing_tracks(group), 0)

    @patch("listenbrainz_bot.slskd_search_album_folders")
    def test_search_progress_reaches_the_task(self, mock_search):
        """The search is a background task; without progress on the task row the
        screen has nothing to show for the 30-90s slskd takes."""
        seen = []

        def search(artist, album, expected, progress=None, stats=None, **kw):
            progress("2 peer(s)")
            return [{"username": "u", "folder": "Artist/Album",
                     "files": [{"filename": "One.flac"}]}]

        mock_search.side_effect = search
        group = {"id": "g1", "artist": "Artist", "album": "Album",
                 "canonical_mbid": "rel1",
                 "missing_tracks": [{"title": "One", "decision": "approved"}]}
        bot._review_state["groups"] = [group]
        try:
            with patch("listenbrainz_bot._task_update",
                       side_effect=lambda tid, **kw: seen.append(kw)):
                result = bot._run_group_source_search("t1", "g1")
        finally:
            bot._review_state["groups"] = []
        self.assertTrue(result["ok"])
        self.assertIn("2 peer(s)", [kw.get("current") for kw in seen])

    def test_running_source_search_is_reported_per_group(self):
        bot._review_state["tasks"] = {
            "t1": {"id": "t1", "kind": "source-search", "status": "running",
                   "group_id": "g1", "started_at": 1, "current": "3 peer(s)"},
            "t0": {"id": "t0", "kind": "source-search", "status": "complete",
                   "group_id": "g1", "started_at": 0},
            "t2": {"id": "t2", "kind": "scan-all", "status": "running",
                   "group_id": "g2", "started_at": 1},
        }
        try:
            self.assertEqual(bot._groups_with_running_source_search(), {"g1"})
            view = bot._group_source_task_view("g1")
            self.assertEqual(view["id"], "t1")          # newest wins
            self.assertEqual(view["current"], "3 peer(s)")
            self.assertIsNone(bot._group_source_task_view("g3"))
        finally:
            bot._review_state["tasks"] = {}

    @patch("listenbrainz_bot.slskd_search_album_folders")
    def test_prepare_sources_marks_approved_tracks_source_pending(self, mock_search):
        mock_search.return_value = [
            {"username": "u", "folder": "Artist/Album", "files": [{"filename": "One.flac"}]}
        ]
        group = {
            "id": "g1",
            "artist": "Artist",
            "album": "Album",
            "canonical_mbid": "rel1",
            "missing_tracks": [
                {"title": "One", "artist": "Artist", "decision": "approved"},
                {"title": "Two", "artist": "Artist", "decision": "skipped"},
            ],
        }
        # The search is now three steps so the slow middle one can run with
        # _review_lock released; drive them the way _run_group_source_search does.
        plan = bot._group_source_plan(group)
        self.assertTrue(plan["ok"])
        folders = bot._search_group_sources(plan)
        result = bot._apply_group_sources(group, plan, folders)
        self.assertTrue(result["ok"])
        self.assertEqual(group["missing_tracks"][0]["decision"], "source_pending")
        self.assertEqual(group["missing_tracks"][1]["decision"], "skipped")
        self.assertEqual(len(group["source_results"]["folders"]), 1)

    @patch("listenbrainz_bot._default_web_user")
    @patch("listenbrainz_bot.slskd_enqueue")
    @patch("listenbrainz_bot.slskd_expand_directory")
    def test_choose_source_queues_only_approved_tracks(
            self, mock_expand, mock_enqueue, mock_user):
        mock_user.return_value = {"telegram_token": "tok", "chat_id": "chat"}
        mock_expand.return_value = [
            {"filename": "01 One.flac", "size": 1},
            {"filename": "02 Two.flac", "size": 1},
        ]
        mock_enqueue.return_value = True
        group = {
            "id": "g1",
            "artist": "Artist",
            "album": "Album",
            "canonical_mbid": "rel1",
            "match_mode": "auto",
            "missing_tracks": [
                {"title": "One", "artist": "Artist", "decision": "source_pending"},
                {"title": "Two", "artist": "Artist", "decision": "skipped"},
            ],
            "source_results": {
                "folders": [
                    {"username": "u", "folder": "Artist/Album",
                     "files": [{"filename": "01 One.flac"}],
                     "upload_speed": 0}
                ]
            },
        }
        result = bot._enqueue_group_source(group, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(mock_enqueue.call_count, 1)
        self.assertEqual(group["missing_tracks"][0]["decision"], "queued")
        self.assertEqual(group["missing_tracks"][1]["decision"], "skipped")

    def test_batch_decision_shape_can_update_multiple_tracks(self):
        group = {
            "id": "g1",
            "missing_tracks": [
                {"title": "One", "decision": "pending"},
                {"title": "Two", "decision": "pending"},
            ],
        }
        for idx in [0, 1]:
            group["missing_tracks"][idx]["decision"] = "approved"
        self.assertEqual([t["decision"] for t in group["missing_tracks"]],
                         ["approved", "approved"])

    def test_source_summary_reports_coverage(self):
        group = {
            "missing_tracks": [
                {"title": "One", "artist": "Artist", "position": 1,
                 "decision": "source_pending"},
                {"title": "Two", "artist": "Artist", "position": 2,
                 "decision": "source_pending"},
            ]
        }
        folder = {"username": "u", "folder": "Artist/Album", "files": [
            {"filename": "01 One.flac", "bitDepth": 24, "sampleRate": 96000},
        ]}
        summary = bot._source_summary(folder, 0, group)
        self.assertEqual(summary["coverage"]["label"], "1/2")
        self.assertEqual(summary["coverage"]["unmatched_tracks"][0]["title"], "Two")
        self.assertIn("filename mismatch",
                      [f["label"] for f in summary["risk_flags"]])

    def test_source_recommendation_labels_risky_live_source(self):
        group = {
            "missing_tracks": [
                {"title": "One", "artist": "Artist", "decision": "source_pending"},
            ]
        }
        folder = {"username": "u", "folder": "Artist Live Bootleg", "files": [
            {"filename": "One.flac"},
        ]}
        summary = bot._source_summary(folder, 1, group)
        self.assertEqual(summary["recommendation"], "Risky live source")
        self.assertIn("live", [f["code"] for f in summary["risk_flags"]])

    def test_action_center_buckets_next_actions(self):
        review = {"groups": [
            {"id": "g1", "artist": "A", "album": "Needs review",
             "albums": [{"id": "a1"}], "missing_tracks": []},
            {"id": "g2", "artist": "A", "album": "Needs source",
             "canonical_album_id": "a1",
             "missing_tracks": [{"decision": "source_pending"}]},
            {"id": "g3", "artist": "A", "album": "Failed",
             "canonical_album_id": "a1",
             "missing_tracks": [{"decision": "failed"}]},
        ], "tasks": {}}
        snap = bot._action_center_snapshot(review, {"bot_pending": [], "album_groups": [], "review": []},
                                           {"checks": [{"ok": False, "label": "slskd", "detail": "down"}]})
        counts = {c["key"]: c["count"] for c in snap["cards"]}
        self.assertEqual(counts["albums_needing_review"], 1)
        self.assertEqual(counts["tracks_needing_source"], 1)
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["diagnostics_failures"], 1)

    def test_settings_cards_redact_secrets_and_private_hosts(self):
        user = {
            "navidrome_user": "icher",
            "navidrome_password": "secret-pass",
            "listenbrainz_user": "lbz",
            "playlist_sources": {"weekly": "Weekly"},
        }
        old_url = bot.NAVIDROME_URL
        old_key = bot.SLSKD_API_KEY
        try:
            bot.NAVIDROME_URL = "http://192.168.1.50:4533"
            bot.SLSKD_API_KEY = "super-secret-key"
            payload = bot._settings_cards(user, [])
            text = str(payload)
        finally:
            bot.NAVIDROME_URL = old_url
            bot.SLSKD_API_KEY = old_key
        self.assertNotIn("secret-pass", text)
        self.assertNotIn("super-secret-key", text)
        self.assertNotIn("192.168.1.50", text)
        self.assertIn("[redacted", text)

    def test_manual_match_payload_shape(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "01 One.flac")
            with open(path, "wb") as fh:
                fh.write(b"x")
            group = {"id": "g1", "canonical_mbid": "rel1"}
            rec = {"id": "d1", "label": "Artist - Album",
                   "album_dir": td, "status": "needs_match"}
            candidates = [{
                "release_mbid": "rel1",
                "title": "Album",
                "track_count": 1,
                "tracks": [{"position": 1, "title": "One", "mbid": "rec1"}],
            }]
            payload = bot._manual_match_payload(group, rec, candidates)
        self.assertEqual(payload["actions"][0], "import_selected_release")
        self.assertEqual(payload["candidates"][0]["release_mbid"], "rel1")
        self.assertTrue(payload["comparison"][0]["matched"])
        self.assertEqual(payload["downloaded_files"][0]["name"], "01 One.flac")

    def test_recovery_record_marks_downloaded_not_imported(self):
        old_albums = bot._albums.copy()
        old_uid = bot._uid_to_token.copy()
        try:
            bot._albums.clear()
            recid = bot._record_import_recovery(
                "tok", "chat", "Artist - Album", "/downloads/A",
                "Artist", "Album", "rel1", "needs_review", "", "auto",
                {"raw_tail": "Skipping.", "recommended_action": "retry"})
            rec = bot._albums[recid]
            self.assertEqual(rec["import_state"], "downloaded_not_imported")
            self.assertEqual(rec["raw_tail"], "Skipping.")
        finally:
            bot._albums.clear()
            bot._albums.update(old_albums)
            bot._uid_to_token.clear()
            bot._uid_to_token.update(old_uid)

    def test_atomic_json_write_survives_concurrent_writers(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "state.json")
            workers = [threading.Thread(
                target=bot._atomic_json_write, args=(path, {"writer": i, "rows": list(range(20))}))
                for i in range(20)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            with open(path) as fh:
                saved = json.load(fh)
            self.assertIn(saved["writer"], range(20))
            self.assertEqual(saved["rows"], list(range(20)))
            self.assertFalse([name for name in os.listdir(td) if name.endswith(".tmp")])

    def test_reconcile_downloaded_files_marks_failed_track_downloaded(self):
        old_download_dir = bot.SLSKD_DOWNLOAD_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                folder = os.path.join(td, "Artist - Album")
                os.mkdir(folder)
                path = os.path.join(folder, "01 One.flac")
                with open(path, "wb") as fh:
                    fh.write(b"x")
                bot.SLSKD_DOWNLOAD_DIR = td
                group = {
                    "id": "g1",
                    "artist": "Artist",
                    "album": "Album",
                    "status": "failed",
                    "missing_tracks": [{
                        "position": 1,
                        "artist": "Artist",
                        "title": "One",
                        "decision": "failed",
                        "download_error": "old failure",
                    }],
                    "match_items": [],
                }
                result = bot._reconcile_group_downloaded_files(group)
        finally:
            bot.SLSKD_DOWNLOAD_DIR = old_download_dir
        self.assertEqual(result["matched_count"], 1)
        self.assertEqual(group["missing_tracks"][0]["decision"], "downloaded")
        self.assertEqual(group["missing_tracks"][0]["local_path"], path)
        self.assertNotIn("download_error", group["missing_tracks"][0])
        self.assertEqual(bot._review_group_next_action(group)["bucket"], "downloaded")

    @patch("listenbrainz_bot.mbz_resolve_album")
    @patch("listenbrainz_bot.mbz_search_release_groups")
    @patch("listenbrainz_bot._manual_match_candidate")
    def test_release_candidates_search_without_manual_mbid(self, mock_candidate,
                                                           mock_search,
                                                           mock_resolve):
        mock_search.return_value = [{"rgid": "rg1", "artist": "Artist",
                                     "title": "Album"}]
        mock_resolve.return_value = {"release_mbid": "rel1"}
        mock_candidate.return_value = {"release_mbid": "rel1", "title": "Album"}
        cands = bot._release_candidates_for_download(
            path="/downloads/Artist - Album", artist="Artist", album="Album")
        self.assertEqual(cands[0]["release_mbid"], "rel1")
        mock_search.assert_called()

    def test_a_state_file_with_repair_jobs_is_loaded_ignored_and_kept(self):
        """B-022: the retired pipeline's `repair_jobs` key still loads without
        complaint, drives nothing — an active job no longer comes back as an
        origin-`repair` group — and is written back unchanged, so the previous
        release still finds its jobs after a rollback. A file without the key
        does not grow one."""
        jobs = {"job1": {"id": "job1", "group_id": "rg1", "artist": "A", "album": "B",
                         "status": "downloaded_unmatched",
                         "tracks": [{"id": "t1", "title": "One", "status": "downloaded",
                                     "local_path": "/downloads/A/01 One.flac"}]}}
        old = bot._retired_repair_jobs
        self.addCleanup(setattr, bot, "_retired_repair_jobs", old)
        with tempfile.TemporaryDirectory() as td:
            self._state_files(td)
            bot._save_state()
            with open(bot.STATE_FILE, encoding="utf-8") as fh:
                state = json.load(fh)
            self.assertNotIn("repair_jobs", state)
            state["repair_jobs"] = jobs
            with open(bot.STATE_FILE, "w", encoding="utf-8") as fh:
                json.dump(state, fh)

            bot._load_state()
            with isolated_review():
                bot._replace_review_groups("library", [], "x")
                self.assertEqual(bot._review_state["groups"], [])
                bot._replace_review_groups(
                    "library", [self._origin_group("lib0", "library", album="L")], "x")
                self.assertEqual([g["id"] for g in bot._review_state["groups"]], ["lib0"])
            bot._save_state()
            with open(bot.STATE_FILE, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["repair_jobs"], jobs)

    def _state_files(self, td):
        """Point both persistence paths at a temp dir and restore them after."""
        old = (bot.STATE_FILE, bot.MBZ_CACHE_FILE,
               bot._mbz_cache.copy(), bot._mbz_cache_rev, bot._mbz_cache_saved_rev)
        bot.STATE_FILE = os.path.join(td, "state.json")
        bot.MBZ_CACHE_FILE = os.path.join(td, "mbz_cache.json")

        def restore():
            (bot.STATE_FILE, bot.MBZ_CACHE_FILE, cache,
             bot._mbz_cache_rev, bot._mbz_cache_saved_rev) = old
            bot._mbz_cache.clear()
            bot._mbz_cache.update(cache)

        self.addCleanup(restore)

    def test_mbz_cache_is_a_sidecar_not_part_of_the_state_file(self):
        """The MB cache was 97% of a 26.8 MB state file and is rebuildable, so
        the ~31 eager _save_state() sites must not pay to re-serialize it."""
        with tempfile.TemporaryDirectory() as td:
            self._state_files(td)
            bot._mbz_cache.clear()
            bot._mbz_cache["release/abc?inc=recordings"] = {"id": "abc"}
            bot._mbz_cache["release?query=foo"] = {"searched": True}
            bot._mbz_cache_rev += 1

            bot._save_state()
            with open(bot.STATE_FILE, encoding="utf-8") as fh:
                written = json.load(fh)
            self.assertNotIn("mbz_cache", written,
                             "_save_state must not carry the MB cache any more")
            self.assertFalse(os.path.exists(bot.MBZ_CACHE_FILE),
                             "_save_state must not write the sidecar either")

            bot._save_mbz_cache()
            with open(bot.MBZ_CACHE_FILE, encoding="utf-8") as fh:
                cached = json.load(fh)
            # Entity lookups persist; search queries deliberately do not.
            self.assertIn("release/abc?inc=recordings", cached)
            self.assertNotIn("release?query=foo", cached)

    def test_mbz_cache_save_skips_when_unchanged(self):
        """An idle bot must not re-serialize 26 MB every STATE_FLUSH_INT."""
        with tempfile.TemporaryDirectory() as td:
            self._state_files(td)
            bot._mbz_cache.clear()
            bot._mbz_cache["release/abc"] = {"id": "abc"}
            bot._mbz_cache_rev += 1

            bot._save_mbz_cache()
            first = os.path.getmtime(bot.MBZ_CACHE_FILE)

            writes = []
            real = bot._atomic_json_write
            with patch.object(bot, "_atomic_json_write",
                              lambda *a, **k: (writes.append(a[0]), real(*a, **k))[1]):
                bot._save_mbz_cache()          # nothing changed -> no write
                self.assertEqual(writes, [], "unchanged cache must not be rewritten")
                bot._mbz_cache_put("release/def", {"id": "def"})
                bot._save_mbz_cache()          # a real write bumped the rev
                self.assertEqual(writes, [bot.MBZ_CACHE_FILE])
            self.assertGreaterEqual(os.path.getmtime(bot.MBZ_CACHE_FILE), first)

    def test_legacy_inline_mbz_cache_is_migrated_not_lost(self):
        """Upgrading past the split must keep entries already paid for at 1 req/s."""
        with tempfile.TemporaryDirectory() as td:
            self._state_files(td)
            # A state file in the old combined format.
            with open(bot.STATE_FILE, "w", encoding="utf-8") as fh:
                json.dump({"uid_counter": 0,
                           "mbz_cache": {"release/legacy?inc=x": {"id": "legacy"}}}, fh)
            bot._mbz_cache.clear()
            bot._mbz_cache_rev = 0
            bot._mbz_cache_saved_rev = -1

            bot._load_state()
            self.assertIn("release/legacy?inc=x", bot._mbz_cache)
            # and the migration must leave a write pending, so the entries
            # actually reach the sidecar rather than sitting in memory until
            # some unrelated MusicBrainz call happens to dirty the cache.
            self.assertNotEqual(bot._mbz_cache_rev, bot._mbz_cache_saved_rev)
            bot._save_mbz_cache()
            with open(bot.MBZ_CACHE_FILE, encoding="utf-8") as fh:
                self.assertIn("release/legacy?inc=x", json.load(fh))

    def test_sidecar_wins_over_legacy_inline_copy(self):
        """The sidecar is the newer copy; the legacy blob only fills gaps."""
        with tempfile.TemporaryDirectory() as td:
            self._state_files(td)
            with open(bot.MBZ_CACHE_FILE, "w", encoding="utf-8") as fh:
                json.dump({"release/k": {"v": "new"}}, fh)
            with open(bot.STATE_FILE, "w", encoding="utf-8") as fh:
                json.dump({"uid_counter": 0,
                           "mbz_cache": {"release/k": {"v": "old"},
                                         "release/only-legacy": {"v": "kept"}}}, fh)
            bot._mbz_cache.clear()
            bot._load_state()
            self.assertEqual(bot._mbz_cache["release/k"], {"v": "new"})
            self.assertEqual(bot._mbz_cache["release/only-legacy"], {"v": "kept"})

    def _fill_ledger(self):
        """Isolate the album-fill ledger and restore it afterwards."""
        old = bot._album_fill_status.copy()

        def restore():
            bot._album_fill_status.clear()
            bot._album_fill_status.update(old)

        self.addCleanup(restore)
        bot._album_fill_status.clear()

    def _isolated_transfers(self, forbid_auto_retry=True):
        """Isolate the in-flight transfer tables, stub slskd and persistence."""
        old_groups, old_pending = bot.pending_album_groups.copy(), bot.pending_downloads.copy()

        def restore():
            bot.pending_album_groups.clear()
            bot.pending_album_groups.update(old_groups)
            bot.pending_downloads.clear()
            bot.pending_downloads.update(old_pending)

        self.addCleanup(restore)
        bot.pending_album_groups.clear()
        bot.pending_downloads.clear()
        cancels = []
        # transfer_failed legitimately schedules its one auto-retry; a cancel never may.
        retry = ((lambda *a, **k: self.fail("a cancelled fill must never auto-retry"))
                 if forbid_auto_retry else (lambda *a, **k: None))
        for name, value in (("_slskd_cancel", lambda u, f, *a, **k: cancels.append((u, f))),
                            ("_slskd_fetch_all_downloads", lambda: []),
                            ("_save_state", lambda *a, **k: None),
                            # The cancel route hands its slskd DELETEs to a thread;
                            # run them inline so a test can count them.
                            ("_abandon_transfers_async",
                             lambda entries: bot._abandon_transfers(entries)),
                            ("_schedule_album_fill_retry", retry)):
            patcher = patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return cancels

    def _group(self, completed=0):
        bot.pending_album_groups["ag1"] = {
            "label": "Artist - Album", "total": 3, "completed": completed, "failed": 0,
            "local_dirs": {}, "token": "tok", "chat_id": "chat",
            "release_mbid": "rel1", "artist": "Artist", "album": "Album",
            "source_user": "slowpeer", "ts": time.time() - 3600}
        for n in (1, 2, 3):
            bot.pending_downloads[("slowpeer", f"Album\\0{n}.flac")] = {
                "album_group_id": "ag1", "token": "tok", "chat_id": "chat",
                "track": {"artist": "Artist", "title": f"T{n}"}, "candidates": [],
                "queued_at": time.time() - 3600}

    def test_slskd_cancel_addresses_the_transfer_by_id(self):
        """slskd's DELETE takes the transfer GUID. Sending the filename was a
        silent 404, so a cancelled album went on downloading in slskd."""
        calls = []

        class _Resp:
            status_code = 204
            text = ""

        listing = [{"_username": "peer", "id": "guid-1",
                    "filename": r"@@x\Music\Album\01 Song.flac"}]
        with patch.object(bot._http, "delete",
                          lambda url, **k: calls.append(url) or _Resp()),                 patch.object(bot, "_slskd_fetch_all_downloads", lambda: listing):
            # Enqueued under a different path prefix: matched by basename.
            self.assertTrue(bot._slskd_cancel("peer", r"Album\01 Song.flac"))
            self.assertTrue(calls[-1].endswith("/downloads/peer/guid-1"))
            # A known id skips the lookup entirely.
            self.assertTrue(bot._slskd_cancel("peer", "whatever", "guid-9", []))
            self.assertTrue(calls[-1].endswith("/downloads/peer/guid-9"))
            # Nothing to address: reported, not pretended.
            self.assertFalse(bot._slskd_cancel("other", r"Album\01 Song.flac"))
            _Resp.status_code = 404
            self.assertFalse(bot._slskd_cancel("peer", "x", "guid-1", []))

    def test_cancel_stops_an_in_flight_album_and_offers_retry(self):
        """There was no cancel at all: a slow download stopped in slskd left every
        client on "downloading", with no failure to hang a Retry button on."""
        self._fill_ledger()
        cancels = self._isolated_transfers()
        self._group()
        bot._album_fill_set("rel1", "downloading", artist="Artist", album="Album")

        self.assertTrue(bot._cancel_album_fill("rel1", "Cancelled"))

        self.assertNotIn("ag1", bot.pending_album_groups)
        self.assertEqual(bot.pending_downloads, {})
        self.assertEqual(len(cancels), 3, "every outstanding transfer is cancelled in slskd")
        view = bot._album_fill_view("rel1")
        # A cancel is its own terminal state, not a failure kind: a Retry
        # button on a row the user just stopped was the "restart it from
        # lb-bot" confusion, and `failed` counted cancels as failures.
        self.assertEqual((view["state"], view["failureKind"], view["retryable"]),
                         ("cancelled", "", False))
        self.assertFalse(view["cancellable"])
        # Nothing in flight any more: a second cancel is a no-op, not an error.
        self.assertFalse(bot._cancel_album_fill("rel1", "Cancelled"))

    def test_cancelled_row_is_refused_over_until_a_new_fill_begins(self):
        """Every writer on the placement path used to overwrite a cancel — the
        row flipped cancelled → placed while the files landed anyway."""
        self._fill_ledger()
        with patch.object(bot, "_save_state", lambda *a, **k: None):
            bot._album_fill_set("rel1", "cancelled", reason="Cancelled")
            self.assertFalse(bot._album_fill_set("rel1", "placing"))
            self.assertFalse(bot._album_fill_set("rel1", "placed"))
            bot._album_fill_fail("rel1", "transfer_failed", "late failover")
            self.assertEqual(bot._album_fill_view("rel1")["state"], "cancelled")
            # Only a new fill replaces the verdict.
            bot._album_fill_begin("rel1", artist="A", album="B", total=3)
            view = bot._album_fill_view("rel1")
            self.assertEqual(view["state"], "searching")
            self.assertEqual(view["attempts"], 1)

    def test_cancel_answers_before_slskd_is_asked(self):
        """Each slskd DELETE is a 10 s timeout and the route sits behind the
        hub's 20 s proxy timeout: a twelve-file album could not be cancelled."""
        self._fill_ledger()
        cancels = []
        old_groups, old_pending = bot.pending_album_groups.copy(), bot.pending_downloads.copy()
        self.addCleanup(lambda: (bot.pending_album_groups.clear(),
                                 bot.pending_album_groups.update(old_groups),
                                 bot.pending_downloads.clear(),
                                 bot.pending_downloads.update(old_pending)))
        bot.pending_album_groups.clear()
        bot.pending_downloads.clear()
        self._group()

        def slow_cancel(u, f, *a, **k):
            time.sleep(0.3)
            cancels.append((u, f))
            return True

        with patch.object(bot, "_slskd_cancel", slow_cancel), \
                patch.object(bot, "_slskd_fetch_all_downloads", lambda: []), \
                patch.object(bot, "_save_state", lambda *a, **k: None):
            bot._album_fill_set("rel1", "downloading")
            t0 = time.time()
            self.assertTrue(bot._cancel_album_fill("rel1", "Cancelled"))
            self.assertLess(time.time() - t0, 0.25, "the route must not wait on slskd")
            self.assertEqual(bot._album_fill_view("rel1")["state"], "cancelled")
            self.assertNotIn("ag1", bot.pending_album_groups)
            self.assertEqual(bot.pending_downloads, {})
            for th in threading.enumerate():
                if th.name == "slskd-abandon":
                    th.join(5)
        self.assertEqual(len(cancels), 3)

    def test_placement_claims_the_row_atomically_against_a_cancel(self):
        """Finalize and the cancel route run on different threads; whichever
        claims the row first wins, and the loser finds out rather than
        overwriting."""
        self._fill_ledger()
        self._isolated_transfers()
        with patch.object(bot, "_save_state", lambda *a, **k: None):
            bot._album_fill_set("rel1", "downloading")
            self.assertTrue(bot._album_fill_transition("rel1", "placing", unless=("cancelled",)))
            # Too late to cancel: the files are going into the library.
            self.assertFalse(bot._cancel_album_fill("rel1", "Cancelled"))
            self.assertEqual(bot._album_fill_view("rel1")["state"], "placing")

            bot._album_fill_set("rel2", "downloading")
            self.assertTrue(bot._cancel_album_fill("rel2", "Cancelled"))
            self.assertFalse(bot._album_fill_transition("rel2", "placing", unless=("cancelled",)))

    def test_finalize_never_places_a_cancelled_group(self):
        self._fill_ledger()
        self._isolated_transfers()
        self._group(completed=3)
        bot.pending_album_groups["ag1"]["local_dirs"] = {"/downloads/Album": 3}
        bot.pending_album_groups["ag1"]["cancelled"] = True
        with patch.object(bot, "_deterministic_album_import",
                          lambda *a, **k: self.fail("a cancelled group must not be placed")), \
                patch.object(bot, "_tg_send", new_callable=AsyncMock):
            bot._album_fill_set("rel1", "cancelled")
            asyncio.run(bot._finalize_group(None, "ag1"))
        self.assertNotIn("ag1", bot.pending_album_groups)
        self.assertEqual(bot._album_fill_view("rel1")["state"], "cancelled")

    def test_a_file_finishing_after_its_group_was_cancelled_is_left_alone(self):
        """It is not a loose track: tagging it, scanning Navidrome and
        announcing "✅ Download completed" is how a cancelled album arrived."""
        self._fill_ledger()
        self._isolated_transfers()
        bot.pending_downloads[("slowpeer", "Album\\01.flac")] = {
            "album_group_id": "ag-gone", "token": "tok", "chat_id": "chat",
            "track": {"artist": "A", "title": "T"}, "candidates": []}
        sends = AsyncMock()
        with patch.object(bot, "slskd_get_all_downloads",
                          lambda force=False: [{"_username": "slowpeer",
                                               "filename": "Album\\01.flac",
                                               "state": "Completed, Succeeded"}]), \
                patch.object(bot, "_tg_send", sends), \
                patch.object(bot, "_resolve_local_path", lambda f: "/downloads/Album/01.flac"), \
                patch.object(bot, "_mutagen_write_tags",
                             lambda *a, **k: self.fail("must not tag a cancelled album's file")), \
                patch.object(bot, "_nd_scan_after_import",
                             lambda *a, **k: self.fail("must not scan for it either")):
            app = type("App", (), {"bot": AsyncMock()})()
            asyncio.run(bot._poll_downloads_once({"tok": app}))
        self.assertEqual(bot.pending_downloads, {})
        sends.assert_not_called()

    def test_source_switch_stops_enqueueing_when_cancelled_during_an_enqueue(self):
        """Fix round 1 (M5): with the enqueue awaited off the loop (Q-027(c)),
        a cancel can land during it — and the loop went on POSTing the rest of
        the folder, each acceptance writing `queued` over a cancelled row,
        until the check after the loop."""
        self._fill_ledger()
        self._isolated_transfers()
        self._group()
        ag = bot.pending_album_groups["ag1"]
        ag["alt_sources"] = [{"username": "peer2",
                              "files": [{"filename": f"{n}.flac"} for n in "abc"]}]
        enqueued = []

        def enqueue_then_cancel(username, f, **k):
            enqueued.append(f["filename"])
            if len(enqueued) == 1:
                bot._cancel_album_fill("rel1", "Cancelled")
            return True

        with patch.object(bot, "slskd_expand_directory", lambda u, fd, ref: fd["files"]), \
                patch.object(bot, "slskd_enqueue", enqueue_then_cancel), \
                patch.object(bot, "_tg_send", new_callable=AsyncMock):
            asyncio.run(bot._switch_album_source(None, "ag1"))
        self.assertEqual(enqueued, ["a.flac"])
        self.assertNotIn("ag1", bot.pending_album_groups)
        self.assertEqual(bot._album_fill_view("rel1")["state"], "cancelled")

    def _poll_once_with(self, states, **patches):
        """One poll of the `_group()` fixture's three files, in `states`."""
        listing = [{"_username": "slowpeer", "filename": f"Album\\0{n}.flac", "state": st}
                   for n, st in zip((1, 2, 3), states)]
        app = type("App", (), {"bot": AsyncMock()})()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(bot, "slskd_get_all_downloads",
                                             lambda force=False: listing))
            stack.enter_context(patch.object(bot, "_tg_send", new_callable=AsyncMock))
            for name, value in patches.items():
                stack.enter_context(patch.object(bot, name, value))
            asyncio.run(bot._poll_downloads_once({"tok": app}))

    def test_the_poller_skips_a_finished_file_whose_transfer_was_let_go_of(self):
        """Fix round 1 (M3): a rescan that drops a row lets go of its transfer
        (Q-027(a)) from another thread, possibly while the poller walks
        /downloads for that very file. `del pending_downloads[key]` then raised
        and aborted the tick; a bare pop would have counted the file towards a
        `total` it had just been taken out of."""
        self._fill_ledger()
        self._isolated_transfers()
        self._group()
        ag = bot.pending_album_groups["ag1"]
        key = ("slowpeer", "Album\\01.flac")

        def resolve_and_lose_it(filename):
            bot._detach_transfer(key, bot.pending_downloads[key])
            ag["total"] -= 1
            return "/downloads/Album/01.flac"

        self._poll_once_with(
            ["Completed, Succeeded", "InProgress", "InProgress"],
            _resolve_local_path=resolve_and_lose_it,
            _finalize_group=AsyncMock(side_effect=AssertionError("must not finalize")))
        self.assertEqual((ag["completed"], ag["total"]), (0, 2))
        self.assertEqual(ag["local_dirs"], {})
        self.assertNotIn(key, bot.pending_downloads)

    def test_the_poller_never_fails_over_a_file_whose_transfer_was_let_go_of(self):
        """The failure branch's twin: the transfer went while the poller waited
        on the review lock to write the row — failing it over would re-download
        a track the rescan says is no longer missing."""
        self._fill_ledger()
        self._isolated_transfers()
        self._group()
        ag = bot.pending_album_groups["ag1"]
        ag["alt_sources"] = [{"username": "peer2", "files": [{"filename": "a.flac"}]}]
        key = ("slowpeer", "Album\\01.flac")
        bot.pending_downloads[key].update(review_group_id="g1", review_track_index=0)

        def write_but_lose_it(gid, idx, decision, **k):
            bot.pending_downloads.pop(key, None)
            return False

        self._poll_once_with(
            ["Completed, Errored", "InProgress", "InProgress"],
            _set_review_track_state=write_but_lose_it,
            _switch_album_source=AsyncMock(side_effect=AssertionError("must not fail over")),
            _retry_file_from_alt_source=lambda *a, **k: self.fail("must not retry the file"))
        self.assertEqual((ag["completed"], ag["failed"]), (0, 0))

    def test_source_switch_stops_when_the_fill_is_cancelled_mid_walk(self):
        """A cancel landing while the switch is parked on an await used to let
        it enqueue the next peer's files under a group nothing tracked."""
        self._fill_ledger()
        self._isolated_transfers()
        self._group()
        ag = bot.pending_album_groups["ag1"]
        ag["alt_sources"] = [{"username": "peer2", "files": [{"filename": "a.flac"}]},
                             {"username": "peer3", "files": [{"filename": "b.flac"}]}]

        def expand_then_cancel(username, fd, ref):
            bot._cancel_album_fill("rel1", "Cancelled")
            return fd["files"]

        with patch.object(bot, "slskd_expand_directory", expand_then_cancel), \
                patch.object(bot, "slskd_enqueue",
                             lambda *a, **k: self.fail("must not enqueue after a cancel")), \
                patch.object(bot, "_tg_send", new_callable=AsyncMock):
            asyncio.run(bot._switch_album_source(None, "ag1"))
        self.assertNotIn("ag1", bot.pending_album_groups)
        self.assertEqual(bot._album_fill_view("rel1")["state"], "cancelled")

    def test_source_switch_keeps_its_slskd_calls_off_the_event_loop(self):
        """Q-027(c): the failover ran `slskd_enqueue` (a 30 s POST per file)
        and `_abandon_group_downloads` (synchronous DELETEs) on the poller's
        event loop, stalling every other transfer's bookkeeping meanwhile."""
        self._fill_ledger()
        self._isolated_transfers()
        self._group()
        ag = bot.pending_album_groups["ag1"]
        ag["alt_sources"] = [{"username": "peer2", "files": [{"filename": "a.flac"}]},
                             {"username": "peer3", "files": [{"filename": "b.flac"}]}]
        ag["allow_mp3"] = True
        calls, on_loop, mp3 = [], [], []

        def off_loop(name, answer=None):
            def stub(*a, **k):
                try:
                    asyncio.get_running_loop()
                    on_loop.append(name)
                except RuntimeError:
                    pass
                calls.append(name)
                # The fill's MP3 opt-in is a contextvar; the worker must see it.
                mp3.append(bot._mp3_fallback_on.get())
                return answer(*a) if answer else None
            return stub

        with patch.object(bot, "slskd_expand_directory", lambda u, fd, ref: fd["files"]), \
                patch.object(bot, "slskd_enqueue",
                             off_loop("enqueue", lambda username, f: username == "peer3")), \
                patch.object(bot, "_abandon_group_downloads", off_loop("abandon")), \
                patch.object(bot, "_tg_send", new_callable=AsyncMock):
            asyncio.run(bot._switch_album_source(None, "ag1"))
        # The switch's own abandon, peer2 refusing, its abandon, peer3 accepting.
        self.assertEqual(calls, ["abandon", "enqueue", "abandon", "enqueue"])
        self.assertEqual(on_loop, [])
        self.assertEqual(mp3[1:], [True, True, True])
        self.assertFalse(ag["switching"])

    def test_gap_cancel_pops_the_album_group_and_records_the_cancel(self):
        """The gap route cancelled by filename, never popped the album group and
        wrote nothing — so the orphan sweep finalized and PLACED it minutes
        later, under a row every client still read as downloading."""
        self._fill_ledger()
        cancels = self._isolated_transfers()
        self._group()
        bot.pending_album_groups["ag1"]["review_group_id"] = "g1"
        for info in bot.pending_downloads.values():
            info["review_group_id"] = "g1"
            info["review_track_index"] = 0
        marks = []
        with patch.object(bot, "_set_review_track_state",
                          lambda gid, idx, decision, **k: marks.append(decision)):
            bot._album_fill_set("rel1", "downloading")
            self.assertEqual(bot._gap_cancel("g1"), 1)
        self.assertNotIn("ag1", bot.pending_album_groups)
        self.assertEqual(bot.pending_downloads, {})
        self.assertEqual(len(cancels), 3)
        self.assertEqual(set(marks), {"cancelled"})
        self.assertEqual(bot._album_fill_view("rel1")["state"], "cancelled")

    def test_gap_cancel_track_rows_read_cancelled_on_the_wire(self):
        """B-004, seen live 2026-09-23: after a gap cancel every track row read
        `failed` with no error text. The cancel did mark each track — the gap
        view then mapped decision `cancelled` to wire state `failed`, and read
        only `download_error`, while every per-track state write puts its text
        in `error`. The text is the one every user-initiated cancel records,
        rows and ledger alike (Q-029: it was "cancelled by user" here and
        "Cancelled" on the album route)."""
        self._fill_ledger()
        self._isolated_transfers()
        self._group()
        bot.pending_album_groups["ag1"]["review_group_id"] = "g1"
        for n, info in enumerate(bot.pending_downloads.values()):
            info["review_group_id"] = "g1"
            info["review_track_index"] = n
            # A transfer carries a copy of its review row (the enqueue's
            # `dict(track)`), mbid included — the key a write finds it by.
            info["track"]["mbid"] = f"r{n + 1}"
        group = {"id": "g1", "artist": "Artist", "album": "Album",
                 "missing_tracks": [
                     {"title": f"T{n}", "mbid": f"r{n}", "position": n,
                      "decision": "downloading", "download_state": "InProgress"}
                     for n in (1, 2, 3)]}
        with isolated_review(), patch.object(bot, "_push_gap", lambda *a, **k: None):
            with bot._review_lock:
                bot._review_state["groups"] = [group]
            bot._album_fill_set("rel1", "downloading")
            self.assertEqual(bot._gap_cancel("g1"), 1)
            with bot._review_lock:
                snapshot = json.loads(json.dumps(bot._find_review_group("g1")))
        view = bot._gap_detail_view(snapshot)
        self.assertEqual([t["state"] for t in view["tracks"]], ["cancelled"] * 3)
        self.assertEqual({t["downloadError"] for t in view["tracks"]}, {"Cancelled"})
        self.assertEqual(bot._album_fill_get("rel1").get("reason"), "Cancelled")

    def test_cancel_album_fill_marks_review_tracks_cancelled(self):
        """B-021: `_cancel_album_fill` detached the transfers but, unlike
        `_gap_cancel`, never touched the review track behind them — a track
        cancelled from the album route or the sweep kept reading
        queued/downloading with no error text while the group-level status
        quietly recovered on its own."""
        self._fill_ledger()
        self._isolated_transfers()
        self._group()
        bot.pending_album_groups["ag1"]["review_group_id"] = "g1"
        for n, info in enumerate(bot.pending_downloads.values()):
            info["review_group_id"] = "g1"
            info["review_track_index"] = n
        marks = []
        with patch.object(bot, "_set_review_track_state",
                          lambda gid, idx, decision, **k:
                          marks.append((gid, idx, decision, k.get("error")))):
            bot._album_fill_set("rel1", "downloading")
            self.assertTrue(bot._cancel_album_fill("rel1", "Removed from slskd"))
        self.assertEqual(len(marks), 3)
        self.assertEqual({m[2] for m in marks}, {"cancelled"})
        self.assertEqual({m[3] for m in marks}, {"Removed from slskd"},
                         "the caller's own reason text, not a hardcoded one")

    def test_cancel_album_fill_leaves_a_settled_track_alone_after_a_losing_cas(self):
        """The detach happens before the ledger CAS, so a CAS that loses
        (the row already `placing`/`placed`/`verified`/`needs_match`) still
        leaves the detached transfers gone. Their still-open tracks must read
        `cancelled`; a track that already settled (e.g. `placed`) must not be
        dragged back to `cancelled` just because it shared the batch."""
        self._fill_ledger()
        self._isolated_transfers()
        self._group()
        bot.pending_album_groups["ag1"]["review_group_id"] = "g1"
        infos = list(bot.pending_downloads.values())
        for n, info in enumerate(infos):
            info["review_group_id"] = "g1"
            info["review_track_index"] = n
        group = {"id": "g1", "artist": "Artist", "album": "Album", "missing_tracks": [
            {"title": "T1", "decision": "downloading"},
            {"title": "T2", "decision": "placed"},
            {"title": "T3", "decision": "downloading"},
        ]}
        with isolated_review(), patch.object(bot, "_push_gap", lambda *a, **k: None):
            with bot._review_lock:
                bot._review_state["groups"] = [group]
            # Simulate the losing race directly: the ledger already claimed
            # for placement by the time the CAS below runs.
            bot._album_fill_set("rel1", "placing")
            result = bot._cancel_album_fill("rel1", "Cancelled")
            with bot._review_lock:
                snapshot = json.loads(json.dumps(bot._find_review_group("g1")))
        self.assertFalse(result, "the ledger CAS lost to the placement claim")
        self.assertNotIn("ag1", bot.pending_album_groups, "the group is detached either way")
        self.assertEqual(bot.pending_downloads, {}, "and so are its transfers")
        decisions = {t["title"]: t["decision"] for t in snapshot["missing_tracks"]}
        self.assertEqual(decisions["T1"], "cancelled")
        self.assertEqual(decisions["T3"], "cancelled")
        self.assertEqual(decisions["T2"], "placed",
                         "a settled track must not be dragged back to cancelled")

    def test_gap_view_error_text_only_on_a_failed_or_cancelled_track(self):
        """A retried track keeps its old `error` until something overwrites it;
        a row that is queued again must not still show the last failure."""
        group = {"id": "g1", "artist": "A", "album": "B", "missing_tracks": [
            {"title": "One", "mbid": "r1", "decision": "failed", "error": "Completed, Errored"},
            {"title": "Two", "mbid": "r2", "decision": "queued", "error": "Completed, Errored"},
            {"title": "Three", "mbid": "r3", "decision": "failed",
             "error": "Completed, Errored", "download_error": "placement failed: x"}]}
        rows = {t["title"]: t for t in bot._gap_detail_view(group)["tracks"]}
        self.assertEqual(rows["One"]["downloadError"], "Completed, Errored")
        self.assertEqual(rows["Two"]["downloadError"], "")
        self.assertEqual(rows["Three"]["downloadError"], "placement failed: x")

    def test_dismissing_a_live_album_group_ends_its_fill(self):
        """The SPA's ✕ dropped tracking and left the ledger on `queued` forever."""
        self._fill_ledger()
        self._isolated_transfers()
        self._group()
        bot._album_fill_set("rel1", "queued")
        self.assertEqual(bot._dismiss_transfer("ag1"), "album")
        view = bot._album_fill_view("rel1")
        self.assertEqual((view["state"], view["failureKind"], view["retryable"]),
                         ("failed", "transfer_failed", True))
        self.assertEqual(bot._dismiss_transfer("nope"), "")

    def test_begin_resets_per_fill_fields_and_counts_fills(self):
        """A new `searching` row merged over the old one, so a retry inherited
        the previous attempt's counters, reason and MP3 verdict."""
        self._fill_ledger()
        with patch.object(bot, "_save_state", lambda *a, **k: None):
            bot._album_fill_set("rel1", "failed", done=3, failed=1, reason="old",
                                failureKind="format_rejected", mp3WouldHelp=True,
                                ndAlbumIds=["nd1"], attempts=1, autoRetries=1,
                                excludedUsers=["slow"], source="slow")
            bot._album_fill_begin("rel1", artist="A", album="B", total=12,
                                  excluded_users=("other",))
        view = bot._album_fill_view("rel1")
        self.assertEqual(view["state"], "searching")
        self.assertEqual((view["done"], view["failed"], view["reason"],
                          view["failureKind"], view["mp3WouldHelp"], view["ndAlbumIds"]),
                         (0, 0, "", "", False, []))
        self.assertEqual(view["attempts"], 2)
        row = bot._album_fill_get("rel1")
        self.assertEqual(row["autoRetries"], 0, "a user-initiated fill re-arms the auto-retry")
        self.assertEqual(row["excludedUsers"], ["slow", "other"])

    def test_auto_retry_excludes_the_peer_that_failed(self):
        self._fill_ledger()
        schedule = bot._schedule_album_fill_retry   # the real one, before isolation stubs it
        self._isolated_transfers(forbid_auto_retry=False)
        self._group()
        captured = []
        with patch.object(bot, "_save_state", lambda *a, **k: None), \
                patch.object(bot, "_task_run",
                             lambda kind, label, target, **k: captured.append(target) or "t1"), \
                patch.object(bot, "_album_download_task",
                             lambda *a, **k: captured.append((a, k))):
            bot._album_fill_begin("rel1", artist="A", album="B", total=3)
            bot._album_fill_fail("rel1", "transfer_failed", "peer went away")
            row = bot._album_fill_get("rel1")
            self.assertEqual(row["lastSource"], "slowpeer")
            self.assertGreater(row["retryAt"], time.time())
            bot.pending_album_groups.clear()
            schedule("rel1", row, delay=0)
            deadline = time.time() + 3
            while len(captured) < 1 and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(len(captured), 1, "the retry task was scheduled")
            captured[0]("tid")
        args, kwargs = captured[1]
        self.assertEqual(args[8], ("slowpeer",))
        self.assertFalse(kwargs["user_initiated"])

    def test_cancel_inside_the_retry_backoff_stops_the_retry(self):
        """A cancel inside the 45 s back-off used to be written as `failed` too,
        so the worker could not tell it from the failure and fired anyway."""
        self._fill_ledger()
        schedule = bot._schedule_album_fill_retry
        self._isolated_transfers(forbid_auto_retry=False)
        ran = []
        with patch.object(bot, "_save_state", lambda *a, **k: None), \
                patch.object(bot, "_task_run", lambda *a, **k: ran.append(a) or "t"):
            bot._album_fill_begin("rel1", artist="A", album="B", total=3)
            bot._album_fill_fail("rel1", "transfer_failed", "peer went away")
            view = bot._album_fill_view("rel1")
            self.assertTrue(view["cancellable"], "a pending auto-retry is cancellable")
            self.assertGreater(view["retryAt"], 0)
            self.assertTrue(bot._cancel_album_fill("rel1", "Cancelled"))
            self.assertEqual(bot._album_fill_view("rel1")["retryAt"], 0)
            schedule("rel1", bot._album_fill_get("rel1"), delay=0)
            time.sleep(0.2)
        self.assertEqual(ran, [], "a cancelled fill must not retry itself")

    def test_cancel_during_search_stops_the_enqueue(self):
        self._fill_ledger()
        self._isolated_transfers()
        enqueued = []
        with patch.object(bot, "slskd_search_album_folders",
                          lambda *a, **k: (bot._album_fill_cancel_requested.add("rel1")
                                           or [{"username": "p", "files": [{}]}])), \
                patch.object(bot, "slskd_enqueue_folder",
                             lambda *a, **k: enqueued.append(a) or (1, 1, "ag")), \
                patch.object(bot, "_task_finish", lambda *a, **k: None):
            bot._album_download_search_and_enqueue("t1", "rel1", "A", "B", 3, None, "")
        self.assertEqual(enqueued, [], "a cancel that lands mid-search must not be overtaken")

    def test_try_another_source_excludes_the_previous_peer(self):
        self._fill_ledger()
        self._isolated_transfers(forbid_auto_retry=False)
        chosen = []
        folders = [{"username": "SlowPeer", "files": [{}]}, {"username": "fast", "files": [{}]}]
        with patch.object(bot, "slskd_search_album_folders", lambda *a, **k: list(folders)), \
                patch.object(bot, "slskd_expand_directory", lambda *a, **k: []), \
                patch.object(bot, "_default_web_user", lambda: {}), \
                patch.object(bot, "slskd_enqueue_folder",
                             lambda user, *a, **k: chosen.append(user) or (0, 0, "")), \
                patch.object(bot, "_task_finish", lambda *a, **k: None):
            bot._album_download_search_and_enqueue("t1", "rel1", "A", "B", 3, None, "",
                                                   ("slowpeer",))
        self.assertEqual(chosen, ["fast"])

    def test_fill_path_mp3_optin_agrees_with_reason_across_passes(self):
        """Q-013 follow-up: the fill task's own mp3_would_help/format_rejected
        classification (listenbrainz_bot.py, in
        _album_download_search_and_enqueue) read only the winning pass's
        rejected_formats, same bug as _apply_group_sources. A search where
        only an earlier pass saw mp3 must fail `format_rejected` with
        `mp3WouldHelp: true`, not `no_source` with no retry offered."""
        self._fill_ledger()
        self._isolated_transfers()

        def search(artist, album, expected, progress=None, stats=None, **kw):
            stats.update({
                "peers": 8, "files": 0, "folders": 0, "rejected_formats": [],
                "accepted_formats": ["flac", "opus"],
                "pass_stats": [
                    {"peers": 5, "files": 5, "folders": 0,
                     "rejected_formats": ["mp3"]},
                    {"peers": 8, "files": 0, "folders": 0, "rejected_formats": []},
                ],
            })
            return []

        with patch.object(bot, "slskd_search_album_folders", search), \
                patch.object(bot, "_task_finish", lambda *a, **k: None):
            bot._album_download_search_and_enqueue("t1", "rel1", "A", "B", 3, None, "")
        view = bot._album_fill_view("rel1")
        self.assertIn("mp3", view["reason"])
        self.assertEqual(view["failureKind"], "format_rejected")
        self.assertTrue(view["mp3WouldHelp"])

    def _poll_once(self, downloads):
        app = type("App", (), {"bot": AsyncMock()})()
        with patch.object(bot, "slskd_get_all_downloads", lambda force=False: downloads), \
                patch.object(bot, "_tg_send", new_callable=AsyncMock), \
                patch.object(bot, "_update_group_progress", new_callable=AsyncMock), \
                patch.object(bot, "_resolve_local_path", lambda f: ""):
            asyncio.run(bot._poll_downloads_once({"tok": app}))

    def test_cancel_of_one_file_in_slskd_loses_that_file_and_keeps_the_album(self):
        """A user-cancelled transfer used to be failed over to another peer — the
        download the user had just stopped, re-queued. Then it cancelled the
        WHOLE album on one Cancelled row, throwing away 11 landed tracks when
        the twelfth was stopped. One file is one file."""
        self._fill_ledger()
        cancels = self._isolated_transfers()
        self._group(completed=1)
        bot._album_fill_set("rel1", "downloading")
        with patch.object(bot, "_retry_file_from_alt_source",
                          lambda *a, **k: self.fail("must not fail a user cancel over")):
            self._poll_once([{"_username": "slowpeer", "filename": "Album\\01.flac",
                              "state": "Completed, Cancelled"},
                             {"_username": "slowpeer", "filename": "Album\\02.flac",
                              "state": "InProgress"}])
        self.assertIn("ag1", bot.pending_album_groups)
        self.assertEqual(bot.pending_album_groups["ag1"]["failed"], 1)
        self.assertNotIn(("slowpeer", "Album\\01.flac"), bot.pending_downloads)
        self.assertEqual(bot._album_fill_view("rel1")["state"], "downloading")
        self.assertEqual(cancels, [], "the album's other transfers keep going")

    def test_cancelling_every_file_in_slskd_cancels_the_album(self):
        self._fill_ledger()
        self._isolated_transfers()
        self._group(completed=0)
        bot._album_fill_set("rel1", "downloading")
        with patch.object(bot, "_retry_file_from_alt_source",
                          lambda *a, **k: self.fail("must not fail a user cancel over")):
            self._poll_once([{"_username": "slowpeer", "filename": f"Album\\0{n}.flac",
                              "state": "Completed, Cancelled"} for n in (1, 2, 3)])
        self.assertNotIn("ag1", bot.pending_album_groups)
        self.assertEqual(bot._album_fill_view("rel1")["state"], "cancelled")

    def test_watchdog_cancel_still_fails_over(self):
        self._fill_ledger()
        self._isolated_transfers()
        self._group(completed=1)
        bot.pending_downloads[("slowpeer", "Album\\01.flac")]["_watchdog_cancelled"] = True
        retried = []
        with patch.object(bot, "_retry_file_from_alt_source",
                          lambda ag, ag_id, info, user: retried.append(user) or "other"):
            self._poll_once([{"_username": "slowpeer", "filename": "Album\\01.flac",
                              "state": "Completed, Cancelled"},
                             {"_username": "slowpeer", "filename": "Album\\02.flac",
                              "state": "InProgress"}])
        self.assertEqual(retried, ["slowpeer"])
        self.assertIn("ag1", bot.pending_album_groups)

    def test_source_failing_before_any_track_records_a_failure(self):
        """The group was dropped and the ledger left on `queued` — "downloading"
        in every client, forever."""
        self._fill_ledger()
        self._isolated_transfers(forbid_auto_retry=False)
        self._group(completed=0)
        bot._album_fill_set("rel1", "queued")
        with patch.object(bot, "InlineKeyboardButton", lambda *a, **k: None), \
                patch.object(bot, "InlineKeyboardMarkup", lambda *a, **k: None):
            self._poll_once([{"_username": "slowpeer", "filename": "Album\\01.flac",
                              "state": "Completed, Rejected"}])
        view = bot._album_fill_view("rel1")
        self.assertEqual((view["state"], view["failureKind"], view["retryable"]),
                         ("failed", "transfer_failed", True))

    def test_transfers_removed_from_slskd_cancel_the_album(self):
        """Judged by time missing, not by poll count: at the 5 s active cadence
        two ticks would cancel an album on a ten-second slskd hiccup."""
        self._fill_ledger()
        self._isolated_transfers()
        self._group(completed=0)
        bot._album_fill_set("rel1", "downloading")
        other = [{"_username": "someone", "filename": "x.flac", "state": "InProgress"}]
        self._poll_once(other)
        self.assertIn("ag1", bot.pending_album_groups, "one missing poll is not enough")
        self._poll_once(other)
        self.assertIn("ag1", bot.pending_album_groups, "nor is a second one seconds later")
        for info in bot.pending_downloads.values():
            info["_missing_since"] = time.time() - bot.VANISHED_TRANSFER_SECS - 1
        self._poll_once(other)
        self.assertNotIn("ag1", bot.pending_album_groups)
        self.assertEqual(bot._album_fill_view("rel1")["state"], "cancelled")

    def test_status_view_progress_is_honest(self):
        """`done` used to be completed+failed and was written once at `queued`
        and never again, so a placed album reported percent 0; bytes were on
        the transfer records and never read."""
        self._fill_ledger()
        self._isolated_transfers()
        self._group(completed=1)
        ag = bot.pending_album_groups["ag1"]
        ag["failed"] = 1
        ag["completed_bytes"] = 1000
        ag["progress_at"] = time.time() + 5
        with patch.object(bot, "_save_state", lambda *a, **k: None):
            bot._album_fill_set("rel1", "queued", done=0, total=3)
        p1 = bot.pending_downloads[("slowpeer", "Album\\01.flac")]
        p1.update(latest_state="InProgress",
                  raw_transfer={"size": 2000, "bytesTransferred": 500, "averageSpeed": 100})
        view = bot._album_fill_view("rel1")
        self.assertEqual(view["state"], "downloading")
        self.assertEqual((view["done"], view["failed"], view["total"]), (1, 1, 3))
        self.assertEqual((view["bytesDone"], view["bytesTotal"], view["speedBps"],
                          view["activeFiles"]), (1500, 3000, 100, 1))
        self.assertEqual(view["percent"], 50)
        self.assertTrue(view["cancellable"])
        self.assertEqual(view["updatedAt"], ag["progress_at"], "progress moves updatedAt")
        self.assertGreater(view["serverTime"], 0)
        self.assertEqual(len(view["files"]), 3)
        self.assertNotIn("files", bot._album_fill_view("rel1", include_files=False))
        # Terminal: 100 % and the final counts, never the counters from `queued`.
        bot.pending_album_groups.clear()
        with patch.object(bot, "_save_state", lambda *a, **k: None):
            bot._album_fill_set("rel1", "placed", done=2, failed=1, total=3)
        view = bot._album_fill_view("rel1")
        self.assertEqual((view["percent"], view["done"], view["cancellable"]), (100, 2, False))

    def test_zombie_rows_are_swept_to_a_terminal_state(self):
        """A restart mid-search left `searching` forever: a live fill with a
        Cancel button that did nothing, in every client."""
        self._fill_ledger()
        self._isolated_transfers(forbid_auto_retry=False)
        now = time.time()
        with patch.object(bot, "_save_state", lambda *a, **k: None), \
                patch.object(bot, "_running_album_download_task", lambda m: ""):
            bot._album_fill_set("stale-search", "searching", artist="A", album="B")
            bot._album_fill_set("fresh-search", "searching", artist="A", album="B")
            bot._album_fill_set("stale-placing", "placing")
            bot._album_fill_set("stale-placed", "placed")
            bot._album_fill_status["stale-search"]["updated_at"] = now - bot.SEARCH_TIMEOUT - 120
            bot._album_fill_status["stale-placing"]["updated_at"] = now - 16 * 60
            bot._album_fill_status["stale-placed"]["updated_at"] = now - bot.ALBUM_FILL_VERIFY_TIMEOUT - 120
            self.assertEqual(bot._sweep_album_fill_zombies(), 3)
        self.assertEqual(bot._album_fill_view("stale-search")["failureKind"], "transfer_failed")
        self.assertEqual(bot._album_fill_view("fresh-search")["state"], "searching")
        self.assertEqual(bot._album_fill_view("stale-placing")["failureKind"], "placement_failed")
        placed = bot._album_fill_view("stale-placed")
        self.assertEqual((placed["state"], placed["verifyGaveUp"]), ("placed", True))

    def test_gap_fill_rows_reach_verified(self):
        self._fill_ledger()
        with patch.object(bot, "_save_state", lambda *a, **k: None):
            bot._album_fill_set("canon-1", "placed")
            bot._album_fill_set("other", "placed", groupId="g1")
            bot._album_fill_set("unrelated", "placed")
            bot._album_fill_mark_group_verified("g1", {"canon-1"}, ["nd-9"])
        self.assertEqual(bot._album_fill_view("canon-1")["state"], "verified")
        self.assertEqual(bot._album_fill_view("other")["ndAlbumIds"], ["nd-9"])
        self.assertEqual(bot._album_fill_view("unrelated")["state"], "placed")

    def _push_capture(self):
        """Route the hub push at a recorder, with the coalescing window shrunk."""
        posts = []
        for name, value in (("HUB_NOTIFY_URL", "http://hub"), ("HUB_NOTIFY_TOKEN", "tok"),
                            ("PUSH_COALESCE_SECS", 0.05)):
            patcher = patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(bot.requests, "post",
                               lambda url, json=None, **k: posts.append((url, json)))
        patcher.start()
        self.addCleanup(patcher.stop)
        return posts

    def _drain_push(self, posts, want, timeout=3.0):
        deadline = time.time() + timeout
        while len(posts) < want and time.time() < deadline:
            time.sleep(0.02)
        return posts

    def test_every_ledger_transition_is_pushed_to_the_hub_as_a_fill_frame(self):
        """A landing had a push (`/lb/notify`); a failure, a cancel and progress
        had none, so the OTHER client kept polling `downloading`."""
        self._fill_ledger()
        self._isolated_transfers()
        posts = self._push_capture()
        with patch.object(bot, "_save_state", lambda *a, **k: None):
            bot._album_fill_begin("rel1", artist="A", album="B", total=3, rgid="rg-1")
            self._drain_push(posts, 1)
            bot._cancel_album_fill("rel1", "Cancelled")
            self._drain_push(posts, 2)
        self.assertEqual([u for u, _ in posts], ["http://hub/lb/fill"] * 2)
        first, last = posts[0][1], posts[-1][1]
        self.assertEqual((first["kind"], first["key"], first["state"]), ("album", "rg-1", "searching"))
        self.assertEqual((last["state"], last["cancellable"]), ("cancelled", False))
        self.assertNotIn("files", last, "the frame never carries the per-file list")

    def test_push_coalesces_a_burst_and_never_pushes_unknown(self):
        self._fill_ledger()
        posts = self._push_capture()
        with patch.object(bot, "_save_state", lambda *a, **k: None):
            for _ in range(5):
                bot._push_fill("rel-burst")          # no row: unknown, never sent
            bot._album_fill_set("rel1", "queued", rgid="rg-1", total=3)
            bot._album_fill_set("rel1", "downloading", rgid="rg-1", total=3)
            bot._album_fill_set("rel1", "downloading", rgid="rg-1", total=3)
            self._drain_push(posts, 1)
            time.sleep(0.2)
        self.assertEqual(len(posts), 1, "three writes inside the window are one push")
        self.assertEqual(posts[0][1]["state"], "downloading")

    def test_progress_push_is_throttled(self):
        self._fill_ledger()
        self._isolated_transfers()
        self._group(completed=0)
        sent = []
        with patch.object(bot, "_push_enqueue", lambda kind, key, payload=None: sent.append(key)):
            ag = bot.pending_album_groups["ag1"]
            bot._push_fill_progress("ag1")           # first: always
            bot._push_fill_progress("ag1")           # same percent, same second: no
            ag["completed"] = 1                      # 33 %: moved ≥ 5 points
            bot._push_fill_progress("ag1")
            ag["_pushed_at"] = time.time() - bot.PUSH_PROGRESS_MIN_SECS - 1
            bot._push_fill_progress("ag1")           # old enough: yes even unmoved
        self.assertEqual(sent, ["rel1"] * 3)

    def test_fills_view_answers_every_watched_row_in_one_read(self):
        self._fill_ledger()
        self._isolated_transfers()
        self._group(completed=1)
        with patch.object(bot, "_save_state", lambda *a, **k: None):
            bot._album_fill_set("rel1", "queued", total=3)
            bot._album_fill_set("rel2", "verified", total=9, done=9)
        out = bot._fills_view(["rel1", "rel2", "rel1", "nope"] + [f"x{i}" for i in range(40)], [])
        self.assertEqual(set(out["albums"]) >= {"rel1", "rel2", "nope"}, True)
        self.assertLessEqual(len(out["albums"]), 32)
        self.assertEqual(out["albums"]["rel1"]["state"], "downloading")
        self.assertEqual(out["albums"]["rel2"]["percent"], 100)
        self.assertEqual(out["albums"]["nope"]["state"], "unknown")
        self.assertNotIn("files", out["albums"]["rel1"])
        self.assertGreater(out["serverTime"], 0)
        self.assertEqual(out["gaps"], {})

    def test_gap_fill_frame_counts_follow_the_progress_rule(self):
        """Q-025, PROTOCOL §15.2 "Progress counts" (B-024): finished is
        downloaded|done|skipped|cancelled, failed is counted apart, and total is
        every track that isn't present. The frame counted downloaded|placed|
        verified — but the gap view never emits placed/verified (both are
        `done`), so a placed track counted as neither done nor failed."""
        titles = ["Have", "Also", "Dl", "Placed", "Verified", "Extra", "Skip",
                  "Cancel", "Fail", "Queued", "Missing"]
        decisions = {"Dl": "downloaded", "Placed": "placed", "Verified": "verified",
                     "Extra": "filed_extra", "Skip": "skipped", "Cancel": "cancelled",
                     "Fail": "failed", "Queued": "queued", "Missing": "pending"}
        mbid = {t: f"r{i}" for i, t in enumerate(titles)}
        group = {"id": "g1", "artist": "A", "album": "B", "canonical_album_id": "al1",
                 "present": 2, "total": len(titles),
                 "albums": [{"id": "al1", "tracks": [
                     {"title": t, "musicBrainzId": mbid[t]} for t in titles]}],
                 "missing_tracks": [{"title": t, "mbid": mbid[t], "decision": d}
                                    for t, d in decisions.items()]}
        frame = bot._gap_fill_frame(group)
        self.assertEqual((frame["done"], frame["failed"], frame["total"]), (6, 1, 9))

        # /api/fills copies the frame's counts onto the gap summary.
        with isolated_review():
            with bot._review_lock:
                bot._review_state["groups"] = [group]
            summary = bot._fills_view([], ["g1"])["gaps"]["g1"]
        self.assertEqual((summary["done"], summary["failed"]), (6, 1))

        # No track rows at all: the group's own counts, still without the present ones.
        bare = {"id": "g2", "artist": "A", "album": "C", "present": 7, "total": 10,
                "missing_tracks": []}
        self.assertEqual(bot._gap_fill_frame(bare)["total"], 3)

    def test_fill_ledger_survives_a_restart(self):
        """The whole point of §1: pending_album_groups and pending_downloads are
        restored, so the download really does resume — only its ledger row used
        to be lost, leaving clients polling album/status on `unknown` forever."""
        with tempfile.TemporaryDirectory() as td:
            self._state_files(td)
            self._fill_ledger()
            bot._album_fill_set("rel-1", "queued", artist="A", album="B",
                                rgid="rg-1", quality="flac", total=9)
            bot._save_state()

            bot._album_fill_status.clear()
            bot._load_state()
            row = bot._album_fill_get("rel-1")
            self.assertEqual(row.get("state"), "queued")
            # rgid is the field _finalize_group flips the index with, and the
            # one whose absence made the flip a silent no-op after a restart.
            self.assertEqual(row.get("rgid"), "rg-1")
            self.assertEqual(row.get("quality"), "flac")

    def test_fill_ledger_prunes_only_aged_out_terminal_rows(self):
        """A stale `verified` is history; an interrupted transfer is not, however
        old it looks — pending_album_groups came back with it."""
        self._fill_ledger()
        now = time.time()
        bot._album_fill_restore({
            "old-verified": {"state": "verified", "updated_at": now - 7 * 3600},
            "old-failed":   {"state": "failed",   "updated_at": now - 2 * 86400},
            "recent-failed": {"state": "failed",  "updated_at": now - 60},
            "stale-queued": {"state": "queued",   "updated_at": now - 30 * 86400},
        })
        self.assertNotIn("old-verified", bot._album_fill_status)
        self.assertNotIn("old-failed", bot._album_fill_status)
        self.assertIn("recent-failed", bot._album_fill_status)
        self.assertIn("stale-queued", bot._album_fill_status)

    def test_failure_kind_and_retryable_are_on_the_status_view(self):
        """Clients used to infer the button from the sentence. §4 gives them
        fields — and a format rejection MP3 would fix is deliberately NOT a
        plain-retry, because that re-runs the identical rejected search."""
        self._fill_ledger()
        with patch.object(bot, "_save_state", lambda: None), \
             patch.object(bot, "_schedule_album_fill_retry", lambda *a, **k: None):
            bot._album_fill_fail("rel-fmt", "format_rejected",
                                 "103 peers offered 2,047 files, none in FLAC",
                                 mp3WouldHelp=True)
            view = bot._album_fill_view("rel-fmt")
            self.assertEqual(view["failureKind"], "format_rejected")
            self.assertFalse(view["retryable"])
            self.assertTrue(view["mp3WouldHelp"])
            self.assertEqual(view["attempts"], 1)
            # The free-text reason stays verbatim: it is the evidence.
            self.assertIn("2,047 files", view["reason"])

            bot._album_fill_fail("rel-none", "no_source", "Nobody had it")
            self.assertTrue(bot._album_fill_view("rel-none")["retryable"])

    def test_only_transient_kinds_auto_retry_and_only_once(self):
        """no_source must never auto-retry: lb-bot already walked its whole
        ranked source list, so an automatic retry re-runs the same search."""
        self._fill_ledger()
        scheduled = []
        with patch.object(bot, "_save_state", lambda: None), \
             patch.object(bot, "_schedule_album_fill_retry",
                          lambda mbid, prior, **k: scheduled.append(mbid)):
            bot._album_fill_fail("a", "no_source", "none")
            bot._album_fill_fail("b", "format_rejected", "wrong format")
            self.assertEqual(scheduled, [])

            bot._album_fill_fail("c", "transfer_failed", "peer went away",
                                 artist="A", album="B")
            self.assertEqual(scheduled, ["c"])
            # The retry's own failure is past FILL_AUTO_RETRY_MAX and stops
            # rather than looping forever...
            bot._album_fill_begin("c", artist="A", album="B", total=3, user_initiated=False)
            bot._album_fill_fail("c", "transfer_failed", "peer went away again")
            self.assertEqual(scheduled, ["c"])
            self.assertEqual(bot._album_fill_view("c")["attempts"], 2)
            # ...but the user asking again re-arms it: `attempts` counts fills,
            # and it used to count failures and never reset, so the one retry
            # only ever fired on a row's first failure.
            bot._album_fill_begin("c", artist="A", album="B", total=3)
            bot._album_fill_fail("c", "transfer_failed", "and again")
            self.assertEqual(scheduled, ["c", "c"])
            self.assertEqual(bot._album_fill_view("c")["attempts"], 3)

    def test_status_view_reports_failure_fields_on_a_healthy_fill(self):
        """A client reads these unconditionally, so they must be present and
        falsey on anything that has not failed."""
        self._fill_ledger()
        with patch.object(bot, "_save_state", lambda: None):
            bot._album_fill_set("rel-ok", "placed")
        view = bot._album_fill_view("rel-ok")
        self.assertEqual(view["failureKind"], "")
        self.assertFalse(view["retryable"])
        self.assertEqual(view["attempts"], 0)

    def _index_file(self):
        """Point the library index at a scratch DB. Cleanups are LIFO, so the
        directory removal is registered first and therefore runs last — on
        Windows the file cannot be unlinked while the connection is open."""
        import shutil
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, True)
        scratch_index(self, os.path.join(td, "index.db"))

    def test_strict_mbz_get_retries_then_raises_instead_of_answering_empty(self):
        """A failed browse used to come back as {}, which a scan read as "no
        releases" — and inside the cooldown it came back {} without asking."""
        calls = []

        def boom(*a, **k):
            calls.append(1)
            raise type("ReadTimeout", (Exception,), {})("slow")

        key = bot._mbz_cache_key("release-group", {"artist": "strict-a"})
        self.addCleanup(bot._mbz_fail_until.pop, key, None)
        with patch.object(bot._http, "get", boom), patch.object(bot.time, "sleep", lambda s: None):
            with self.assertRaises(bot.MusicBrainzUnavailable) as ctx:
                bot.mbz_get("release-group", {"artist": "strict-a"}, strict=True)
            self.assertIn("timed out", str(ctx.exception))
            self.assertEqual(len(calls), 1 + len(bot.MBZ_STRICT_RETRY_BACKOFF))
            # Non-strict keeps its old contract: cooldown, {} and no request.
            self.assertEqual(bot.mbz_get("release-group", {"artist": "strict-a"}), {})
            self.assertEqual(len(calls), 1 + len(bot.MBZ_STRICT_RETRY_BACKOFF))
            # ...but a strict caller asks again despite the cooldown.
            with self.assertRaises(bot.MusicBrainzUnavailable):
                bot.mbz_get("release-group", {"artist": "strict-a"}, strict=True)

    def test_strict_browse_refuses_a_discography_cut_short(self):
        page = [{"id": f"rg{n}", "title": f"T{n}", "primary-type": "Album"} for n in range(100)]

        def fake_get(path, params=None, strict=False, bypass_cache=False):
            if params.get("offset") == "0":
                return {"release-groups": page, "release-group-count": 150}
            if strict:
                raise bot.MusicBrainzUnavailable("MusicBrainz did not answer (HTTP 503)")
            return {}

        with patch.object(bot, "mbz_get", fake_get):
            self.assertEqual(len(bot.mbz_artist_release_groups("a")), 100)
            with self.assertRaises(bot.MusicBrainzUnavailable):
                bot.mbz_artist_release_groups("a", strict=True)

    def test_strict_browse_bypasses_a_positive_cache_hit(self):
        """Task 6: a strict rescan must not answer from an earlier scan's (or
        the not-owned browse path's) cache entry, or auto-refresh could never
        see a release MusicBrainz added since. Non-strict keeps using it."""
        key = bot._mbz_cache_key("release-group", {"artist": "bypass-a"})
        bot._mbz_cache_put(key, {"release-groups": [{"id": "old"}], "release-group-count": 1})
        self.addCleanup(bot._mbz_cache.pop, key, None)
        calls = []

        def fake_get(url, params=None, **k):
            calls.append(dict(params or {}))
            return _FakeMbzResponse({"release-groups": [{"id": "new"}], "release-group-count": 1})

        with patch.object(bot._http, "get", fake_get), patch.object(bot.time, "sleep", lambda s: None):
            self.assertEqual(bot.mbz_get("release-group", {"artist": "bypass-a"}),
                             {"release-groups": [{"id": "old"}], "release-group-count": 1})
            self.assertEqual(calls, [], "non-strict: the cache hit is honoured")
            data = bot.mbz_get("release-group", {"artist": "bypass-a"},
                               strict=True, bypass_cache=True)
            self.assertEqual(data["release-groups"][0]["id"], "new")
            self.assertEqual(len(calls), 1, "strict + bypass_cache: one fresh request")
            self.assertEqual(bot._mbz_cache[key]["release-groups"][0]["id"], "new",
                             "the fresh answer overwrites the stale cache entry")

    def test_strict_artist_release_groups_rescans_past_a_cached_browse(self):
        """The integration point: strict=True on mbz_artist_release_groups
        itself (not just mbz_get) must see a release added since a previous,
        cached, browse of this artist."""
        mbid = "artist-bypass"
        params = {"artist": mbid, "type": "album|ep|single", "limit": "100", "offset": "0"}
        key = bot._mbz_cache_key("release-group", params)
        bot._mbz_cache_put(key, {"release-groups": [
            {"id": "rg-old", "title": "Old", "primary-type": "Album"}],
            "release-group-count": 1})
        self.addCleanup(bot._mbz_cache.pop, key, None)
        calls = []

        def fake_get(url, params=None, **k):
            calls.append(dict(params or {}))
            return _FakeMbzResponse({"release-groups": [
                {"id": "rg-old", "title": "Old", "primary-type": "Album"},
                {"id": "rg-new", "title": "New", "primary-type": "Album"}],
                "release-group-count": 2})

        with patch.object(bot._http, "get", fake_get), patch.object(bot.time, "sleep", lambda s: None):
            self.assertEqual(len(bot.mbz_artist_release_groups(mbid)), 1,
                             "non-strict (e.g. the not-owned browse path) still uses the cache")
            self.assertEqual(calls, [])
            rgs = bot.mbz_artist_release_groups(mbid, strict=True)
        self.assertEqual({r["rgid"] for r in rgs}, {"rg-old", "rg-new"})
        self.assertEqual(len(calls), 1, "one combined page, not one call per missed cache")

    def test_permanent_4xx_raises_no_such_entity_not_generic_unavailable(self):
        """R14: a strict caller must be able to tell a bad/merged mbid (a
        durable answer, cached like any other) apart from an outage (a
        transient one) — the auto-index worker's per-artist vs whole-worker
        backoff depends on telling them apart."""
        key = bot._mbz_cache_key("artist-bad", {"inc": "release-groups"})
        self.addCleanup(bot._mbz_cache.pop, key, None)

        class _Resp404:
            status_code = 404
            def raise_for_status(self):
                pass
            def json(self):
                return {}

        with patch.object(bot._http, "get", lambda *a, **k: _Resp404()), \
                patch.object(bot.time, "sleep", lambda s: None), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(bot.MusicBrainzNoSuchEntity):
                bot.mbz_get("artist-bad", {"inc": "release-groups"}, strict=True)
            self.assertEqual(bot._mbz_cache[key], {}, "cached durably, like any permanent fail")

        # The second strict call must raise from the cache, with no request at all.
        with patch.object(bot._http, "get",
                          lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request"))):
            with self.assertRaises(bot.MusicBrainzNoSuchEntity):
                bot.mbz_get("artist-bad", {"inc": "release-groups"}, strict=True)
            # Non-strict keeps its old, quiet contract: {} and no exception.
            self.assertEqual(bot.mbz_get("artist-bad", {"inc": "release-groups"}), {})

    def test_mbz_get_refuses_empty_entity_id_no_request_no_cache(self):
        """B-010: the live-observed failure was `release/` -- a release lookup
        with an empty id (most likely an album whose Navidrome mbid came back
        blank). Answering it through the ordinary permanent-4xx path
        negative-cached the *shared, id-less* key, poisoning it for every
        other empty-id caller with the same `inc`, and spent a request
        finding out MusicBrainz agrees. Neither should happen."""
        key = bot._mbz_cache_key("release/", {"inc": "recordings"})
        self.addCleanup(bot._mbz_cache.pop, key, None)
        self.addCleanup(bot._mbz_fail_until.pop, key, None)
        with patch.object(bot._http, "get",
                          lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request"))):
            self.assertEqual(bot.mbz_get("release/", {"inc": "recordings"}), {})
        self.assertNotIn(key, bot._mbz_cache, "must not negative-cache an empty id")
        self.assertNotIn(key, bot._mbz_fail_until)

    def test_mbz_get_refuses_double_slash_empty_id(self):
        """`<entity>//…` is the same bug shaped differently — the id segment
        between the slashes is still empty."""
        with patch.object(bot._http, "get",
                          lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request"))):
            self.assertEqual(bot.mbz_get("recording//"), {})

    def test_mbz_get_strict_empty_entity_id_raises_no_such_entity(self):
        with patch.object(bot._http, "get",
                          lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request"))):
            with self.assertRaises(bot.MusicBrainzNoSuchEntity):
                bot.mbz_get("artist/", strict=True)

    def test_mbz_get_refuses_a_blank_search_query_no_request_no_cache(self):
        """Q-030: the same caller bug as `release/`, shaped as a search — a
        blank `query` used to spend a request and cache the failure under the
        shared, query-less key."""
        no_request = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request"))
        for path, params in (("release-group", {"query": "", "limit": "5"}),
                             ("artist", {"query": "   ", "limit": "8"})):
            key = bot._mbz_cache_key(path, params)
            self.addCleanup(bot._mbz_cache.pop, key, None)
            self.addCleanup(bot._mbz_fail_until.pop, key, None)
            with patch.object(bot._http, "get", no_request), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(bot.mbz_get(path, params), {})
                with self.assertRaises(bot.MusicBrainzNoSuchEntity):
                    bot.mbz_get(path, params, strict=True)
            self.assertNotIn(key, bot._mbz_cache, "must not negative-cache a blank query")
            self.assertNotIn(key, bot._mbz_fail_until)

    def test_mbz_get_refuses_a_blank_browse_id_no_request_no_cache(self):
        """A browse names its linked entity in a parameter (`release-group?artist=`);
        a blank one is the empty-id bug again."""
        params = {"artist": "", "type": "album", "limit": "100", "offset": "0"}
        key = bot._mbz_cache_key("release-group", params)
        self.addCleanup(bot._mbz_cache.pop, key, None)
        self.addCleanup(bot._mbz_fail_until.pop, key, None)
        with patch.object(bot._http, "get",
                          lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request"))), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bot.mbz_get("release-group", params), {})
            with self.assertRaises(bot.MusicBrainzNoSuchEntity):
                bot.mbz_get("release-group", params, strict=True)
        self.assertNotIn(key, bot._mbz_cache)
        self.assertNotIn(key, bot._mbz_fail_until)

    def test_mbz_get_search_and_browse_with_real_values_are_unaffected(self):
        calls = []

        def fake_get(url, params=None, **k):
            calls.append(url.rsplit("/", 1)[-1])
            return _FakeMbzResponse({"count": 1})

        searches = (("release-group", {"query": "Discovery", "limit": "5"}),
                    ("release-group", {"artist": "amb", "limit": "100", "offset": "0"}),
                    ("recording", {"isrcs": "GBAYE0000001"}))
        for path, params in searches:
            self.addCleanup(bot._mbz_cache.pop, bot._mbz_cache_key(path, params), None)
        with patch.object(bot._http, "get", fake_get), \
                patch.object(bot.time, "sleep", lambda s: None):
            for path, params in searches:
                self.assertEqual(bot.mbz_get(path, params), {"count": 1})
        self.assertEqual(calls, ["release-group", "release-group", "recording"])

    def test_mbz_get_normal_path_with_a_real_id_is_unaffected(self):
        """The guard must not touch an ordinary, well-formed request."""
        key = bot._mbz_cache_key("release/real-id", {"inc": "recordings"})
        self.addCleanup(bot._mbz_cache.pop, key, None)
        with patch.object(bot._http, "get",
                          lambda url, **k: _FakeMbzResponse({"id": "ok"})), \
                patch.object(bot.time, "sleep", lambda s: None):
            data = bot.mbz_get("release/real-id", {"inc": "recordings"})
        self.assertEqual(data, {"id": "ok"})
        self.assertEqual(bot._mbz_cache[key], {"id": "ok"})

    def test_mbz_best_release_skips_lookup_for_empty_recording_id(self):
        with patch.object(bot._http, "get",
                          lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request"))):
            self.assertEqual(bot.mbz_best_release(""), {})

    def test_mbz_release_tracks_skips_lookup_for_empty_release_id(self):
        with patch.object(bot._http, "get",
                          lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request"))):
            self.assertEqual(bot.mbz_release_tracks(""), [])

    def test_canonical_release_fields_skips_lookup_for_empty_release_id(self):
        with patch.object(bot._http, "get",
                          lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request"))):
            self.assertEqual(bot._canonical_release_fields(""), {})

    def test_atomic_json_write_leaves_no_temp_file_when_replace_fails(self):
        """B-028: `_atomic_json_write`'s own `finally` must clean up any temp
        file it created when a later step (here, `os.replace`) raises -- the
        in-process half of the orphaned-tmp-file fix. (The other half, a
        process that is killed outright between creating the temp file and
        that `finally` running, is what the startup sweep exists for -- no
        in-process code can catch that.)"""
        import shutil
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, True)
        path = os.path.join(td, "state.json")

        def boom_replace(a, b):
            raise OSError("simulated replace failure")

        with patch.object(bot.os, "replace", boom_replace):
            with self.assertRaises(OSError):
                bot._atomic_json_write(path, {"a": 1})
        self.assertEqual(os.listdir(td), [], "no leftover temp file")

    def test_sweep_removes_old_orphaned_tmp_files_keeps_fresh_and_unrelated(self):
        import shutil
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, True)
        state_path = os.path.join(td, "lb_bot_state.json")
        old_tmp     = os.path.join(td, ".lb_bot_state.json.abc123.tmp")
        fresh_tmp   = os.path.join(td, ".lb_bot_state.json.def456.tmp")
        unrelated   = os.path.join(td, ".mbz_cache.json.xyz789.tmp")
        for fn in (old_tmp, fresh_tmp, unrelated):
            with open(fn, "w") as fh:
                fh.write("{}")
        old_ts = time.time() - (2 * bot.STATE_TMP_SWEEP_MIN_AGE)
        os.utime(old_tmp, (old_ts, old_ts))
        # fresh_tmp keeps "now" as its mtime -- another writer could be mid-write.

        bot._sweep_orphaned_state_tmp_files(paths=(state_path,))

        self.assertFalse(os.path.exists(old_tmp), "old orphan removed")
        self.assertTrue(os.path.exists(fresh_tmp), "fresh temp file left alone")
        self.assertTrue(os.path.exists(unrelated),
                        "a different file's temp pattern is untouched")

    def test_sweep_matches_its_own_path_literally(self):
        """Final review M7: the directory and the basename were pasted into a
        glob pattern as they were, so a `[` in either (a config dir like
        `/config[1]`, or `state[old].json`) turned into a character class
        and the sweep silently matched nothing — or another file's temps."""
        import shutil
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        td = os.path.join(root, "config[1]")
        os.makedirs(td)
        state_path = os.path.join(td, "state[a].json")
        orphan = os.path.join(td, ".state[a].json.abc123.tmp")
        # What the unescaped pattern `.state[a].json.*.tmp` matches instead.
        decoy = os.path.join(td, ".statea.json.abc123.tmp")
        old_ts = time.time() - (2 * bot.STATE_TMP_SWEEP_MIN_AGE)
        for fn in (orphan, decoy):
            with open(fn, "w") as fh:
                fh.write("{}")
            os.utime(fn, (old_ts, old_ts))

        with contextlib.redirect_stdout(io.StringIO()):
            bot._sweep_orphaned_state_tmp_files(paths=(state_path,))

        self.assertFalse(os.path.exists(orphan), "its own orphan removed")
        self.assertTrue(os.path.exists(decoy), "another name's temp file left alone")

    def test_sweep_covers_state_review_and_mbz_cache_files_by_default(self):
        import shutil
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, True)
        state_path  = os.path.join(td, "lb_bot_state.json")
        review_path = os.path.join(td, "missing_album_review.json")
        mbz_path    = os.path.join(td, "mbz_cache.json")
        old_ts = time.time() - (2 * bot.STATE_TMP_SWEEP_MIN_AGE)
        orphans = []
        for base in (state_path, review_path, mbz_path):
            fn = os.path.join(td, f".{os.path.basename(base)}.orphan.tmp")
            with open(fn, "w") as fh:
                fh.write("{}")
            os.utime(fn, (old_ts, old_ts))
            orphans.append(fn)

        with patch.object(bot, "STATE_FILE", state_path), \
             patch.object(bot, "REVIEW_FILE", review_path), \
             patch.object(bot, "MBZ_CACHE_FILE", mbz_path):
            bot._sweep_orphaned_state_tmp_files()

        for fn in orphans:
            self.assertFalse(os.path.exists(fn), f"{fn} should have been swept")

    def test_a_stop_signal_flushes_and_then_exits(self):
        """B-032: as PID 1 the handler's old re-raise was dropped by the kernel,
        so `docker stop` ended in SIGKILL. It must exit explicitly, after the flush."""
        calls = []
        with patch.object(bot, "_flush_all_state",
                          lambda reason="": calls.append(("flush", reason))), \
             patch.object(bot.os, "_exit", lambda code: calls.append(("exit", code))):
            bot._on_shutdown_signal(15, None)
        self.assertEqual(calls, [("flush", "signal 15"), ("exit", 0)])

    def test_a_stop_signal_exits_even_when_the_flush_hangs(self):
        """A signal can land while the main thread holds a lock the flush needs;
        the exit must not wait on it past the timeout."""
        release = threading.Event()
        exits = []
        with patch.object(bot, "_flush_all_state", lambda reason="": release.wait(5)), \
             patch.object(bot, "SHUTDOWN_FLUSH_TIMEOUT", 0.05), \
             patch.object(bot.os, "_exit", exits.append):
            started = time.monotonic()
            bot._on_shutdown_signal(15, None)
            elapsed = time.monotonic() - started
        release.set()
        self.assertEqual(exits, [0])
        self.assertLess(elapsed, 2)

    def test_mbz_release_full_fills_both_cache_keys_from_one_request(self):
        """Task 6: the release-group lookup and the tracklist of the same
        release are two different cache keys even though MusicBrainz answers
        both from one release document. One combined call must fill both, so
        neither mbz_release_group_of nor mbz_release_tracks spends another
        request on a release this already fetched — and each must read
        exactly what a direct request for its own narrower key would have."""
        release_mbid = "rel-combo"
        combined = {
            "release-group": {"id": "rg-combo"},
            "media": [{"tracks": [
                {"title": "T1", "position": 1, "length": 200000,
                 "recording": {"id": "rec-1", "title": "T1"}}]}],
        }
        calls = []

        def fake_get(url, params=None, **k):
            calls.append(dict(params or {}))
            return _FakeMbzResponse(combined)

        rg_key = bot._mbz_cache_key(f"release/{release_mbid}", {"inc": "release-groups"})
        tr_key = bot._mbz_cache_key(f"release/{release_mbid}", {"inc": "recordings"})
        combo_key = bot._mbz_cache_key(f"release/{release_mbid}",
                                       {"inc": "release-groups recordings"})
        for k in (rg_key, tr_key, combo_key):
            self.addCleanup(bot._mbz_cache.pop, k, None)

        with patch.object(bot._http, "get", fake_get), patch.object(bot.time, "sleep", lambda s: None):
            data = bot.mbz_release_full(release_mbid)
            self.assertEqual(data, combined)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].get("inc"), "release-groups recordings")

            # Shape equivalence: what a direct request for JUST that inc would
            # have cached, per mbz_release_group_of / mbz_release_tracks's own
            # readers — the release-group field and the media/tracks field are
            # each exactly what a narrower request would have shaped, since
            # MusicBrainz merges incs into one document rather than reshaping
            # shared fields.
            direct_release_groups_only = {"release-group": {"id": "rg-combo"}}
            direct_recordings_only = {"media": combined["media"]}
            self.assertEqual((bot._mbz_cache[rg_key].get("release-group") or {}),
                             direct_release_groups_only["release-group"])
            self.assertEqual(bot._mbz_cache[tr_key].get("media"),
                             direct_recordings_only["media"])

            # Halved cost: both narrower lookups now hit the primed cache.
            self.assertEqual(bot.mbz_release_group_of(release_mbid), "rg-combo")
            tracks = bot.mbz_release_tracks(release_mbid)
            self.assertEqual(len(calls), 1, "both answers came from the primed cache")
        self.assertEqual(tracks, [{"title": "T1", "mbid": "rec-1",
                                   "position": 1, "duration": 200.0}])

    def test_mbz_release_full_answers_from_both_cached_single_inc_keys(self):
        """M1: with the release-group key and the tracklist key already
        cached (every owned album after its first scan), the combined request
        was one NEW MusicBrainz request per rescan for nothing."""
        release_mbid = "rel-cached"
        rg_key = bot._mbz_cache_key(f"release/{release_mbid}", {"inc": "release-groups"})
        tr_key = bot._mbz_cache_key(f"release/{release_mbid}", {"inc": "recordings"})
        for k in (rg_key, tr_key):
            self.addCleanup(bot._mbz_cache.pop, k, None)
        bot._mbz_cache_put(rg_key, {"id": release_mbid, "release-group": {"id": "rg-c"}})
        bot._mbz_cache_put(tr_key, {"id": release_mbid, "media": [{"tracks": []}]})
        with patch.object(bot._http, "get",
                          lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request"))):
            data = bot.mbz_release_full(release_mbid)
        self.assertEqual(data["release-group"], {"id": "rg-c"})
        self.assertEqual(data["media"], [{"tracks": []}])

    def test_mbz_release_full_caches_one_projection_per_key(self):
        """M1: the combined document used to be stored under three keys at
        once (the combined one by mbz_get, both narrower ones as the same
        object) — the 8000-entry FIFO filled three times faster and the
        on-disk cache carried the tracklist three times."""
        release_mbid = "rel-proj"
        combined = {"id": release_mbid, "title": "T",
                    "release-group": {"id": "rg-p"},
                    "media": [{"tracks": [{"title": "T1", "position": 1,
                                           "recording": {"id": "rec-1"}}]}]}
        rg_key = bot._mbz_cache_key(f"release/{release_mbid}", {"inc": "release-groups"})
        tr_key = bot._mbz_cache_key(f"release/{release_mbid}", {"inc": "recordings"})
        combo_key = bot._mbz_cache_key(f"release/{release_mbid}",
                                       {"inc": "release-groups recordings"})
        for k in (rg_key, tr_key, combo_key):
            self.addCleanup(bot._mbz_cache.pop, k, None)
        with patch.object(bot._http, "get", lambda *a, **k: _FakeMbzResponse(combined)), \
                patch.object(bot.time, "sleep", lambda s: None):
            self.assertEqual(bot.mbz_release_full(release_mbid), combined)
        self.assertNotIn(combo_key, bot._mbz_cache)
        self.assertNotIn("media", bot._mbz_cache[rg_key])
        self.assertNotIn("release-group", bot._mbz_cache[tr_key])
        self.assertIsNot(bot._mbz_cache[rg_key], bot._mbz_cache[tr_key])
        self.assertEqual(bot.mbz_release_group_of(release_mbid), "rg-p")
        self.assertEqual(len(bot.mbz_release_tracks(release_mbid)), 1)

    def _scan_harness(self):
        self._index_file()
        bot._index_store_artist({"artist_mbid": "art-1", "artist_name": "Artist",
                                 "releases": [{"rgid": "rg-a", "title": "A", "status": "complete"},
                                              {"rgid": "rg-b", "title": "B", "status": "missing"}]},
                                nd_artist_id="nd-1")
        finished, notes = [], []
        self.addCleanup(bot._artist_scans.clear)
        for name, value in (("_task_update", lambda *a, **k: None),
                            ("_task_finish", lambda tid, summary="", error="", **k:
                                finished.append((summary, error))),
                            ("_notify_hub_library_change", lambda *a, **k: notes.append(k))):
            patcher = patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return finished, notes

    def test_a_failed_scan_keeps_the_stored_discography_and_says_why(self):
        """The scan stored whatever came back, so a MusicBrainz hiccup emptied the
        artist's discography, and nothing told the client why."""
        finished, notes = self._scan_harness()
        user = {"navidrome_user": "u", "navidrome_password": "p"}

        def fail(*a, **k):
            raise bot.MusicBrainzUnavailable("MusicBrainz did not answer (HTTP 503)")

        with patch.object(bot, "build_artist_discography", fail):
            bot._artist_discography_task("t1", "art-1", "Artist", user, "nd-1")
        self.assertEqual(len(bot._index_get_artist("art-1", "nd-1")["releases"]), 2)
        self.assertIn("HTTP 503", finished[-1][1])
        scan = bot._artist_scan_get("", "nd-1")
        self.assertEqual(scan["state"], "failed")
        self.assertIn("HTTP 503", scan["error"])
        self.assertEqual(notes[-1].get("event"), "artistScanned")
        self.assertEqual(notes[-1].get("nd_artist_id"), "nd-1")

        # MusicBrainz answering with nothing must not wipe a real discography either.
        empty = {"artist_mbid": "art-1", "artist_name": "Artist",
                 "releases": [], "review_groups": []}
        with patch.object(bot, "build_artist_discography", lambda *a, **k: empty):
            bot._artist_discography_task("t2", "art-1", "Artist", user, "nd-1")
        self.assertEqual(len(bot._index_get_artist("art-1", "nd-1")["releases"]), 2)
        self.assertEqual(bot._artist_scan_get("art-1", "")["state"], "failed")

        # A good scan replaces it and says done.
        good = {"artist_mbid": "art-1", "artist_name": "Artist", "review_groups": [],
                "releases": [{"rgid": "rg-c", "title": "C", "status": "missing"}]}
        with patch.object(bot, "build_artist_discography", lambda *a, **k: good):
            bot._artist_discography_task("t3", "art-1", "Artist", user, "nd-1")
        self.assertEqual([r["rgid"] for r in bot._index_get_artist("art-1", "nd-1")["releases"]],
                         ["rg-c"])
        self.assertEqual(bot._artist_scan_get("art-1", "nd-1")["state"], "done")

    def test_mark_release_present_inserts_when_there_is_no_row(self):
        """It was UPDATE-only, so a download of a release the artist's index
        predates changed nothing and the album kept listing as missing."""
        self._index_file()
        bot._index_upsert_release("artist-key", {
            "rgid": "rg-known", "title": "Known", "status": "missing"})

        self.assertEqual(
            bot._index_mark_release_present(rgid="rg-known"), 1,
            "an existing row must still be flipped by the UPDATE path")

        # No row at all, and no artist to attach one to: nothing to do, and
        # saying so is the point of the return value.
        self.assertEqual(bot._index_mark_release_present(rgid="rg-new"), 0)

        # With the artist key the row is inserted instead.
        self.assertEqual(
            bot._index_mark_release_present(
                rgid="rg-new", artist_key="artist-key", title="Brand New"), 1)
        with bot._index_lock:
            row = bot._index_db().execute(
                "SELECT status, title FROM release_groups WHERE rgid = ?",
                ("rg-new",)).fetchone()
        self.assertEqual(row["status"], "present")
        self.assertEqual(row["title"], "Brand New")

    def test_present_row_backfill_resolves_an_album_id_by_title(self):
        """_index_mark_release_present cannot write nd_album_ids — nothing on the
        placement path knows them — so a filled album badged "In library" had no
        id for any client to open, and every "open the real album" path fell
        through to the download page instead."""
        self._index_file()
        bot._index_ensure_artist("ak", artist_mbid="ak", name="An Artist")
        bot._index_mark_release_present(rgid="rg-filled", artist_key="ak",
                                        title="The Filled One")
        bot._index_mark_release_present(rgid="rg-elsewhere", artist_key="ak",
                                        title="Not Scanned Yet")

        albums = [{"id": "nd-1", "name": "the filled one!", "artist": "An Artist"},
                  {"id": "nd-2", "name": "Someone Else's", "artist": "Other Artist"}]
        with patch.object(bot, "_nd_album_index", lambda force=False: albums):
            self.assertEqual(bot._index_backfill_present_album_ids("ak"), 1)

        stored = bot._index_get_artist("ak", "")
        by_rgid = {r["rgid"]: r for r in stored["releases"]}
        self.assertEqual(by_rgid["rg-filled"]["navidrome_album_ids"], ["nd-1"])
        # Unmatched rows stay `present` with no ids: Navidrome may genuinely not
        # have scanned the files yet, and that is what pendingSync is for.
        self.assertNotIn("navidrome_album_ids", by_rgid["rg-elsewhere"])
        self.assertEqual(by_rgid["rg-elsewhere"]["status"], "present")

    def _run_threads_inline(self):
        class Inline:
            def __init__(self, target=None, args=(), kwargs=None, **_kw):
                self._target, self._args, self._kwargs = target, args, kwargs or {}

            def start(self):
                self._target(*self._args, **self._kwargs)
        patcher = patch.object(bot.threading, "Thread", Inline)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_album_fill_verifier_waits_for_scan_then_announces_album_ids(self):
        """The clients show a landed album the moment this verifier says so. It
        used to sleep 30 s between probes, announce nothing, and leave the album's
        Navidrome id to a title guess against a 300 s cached index — so a filled
        album kept opening the download page for minutes."""
        self._index_file()
        self._run_threads_inline()
        bot._index_ensure_artist("ak", artist_mbid="ak", name="An Artist")
        bot._index_mark_release_present(rgid="rg-1", artist_key="ak", title="Landed")

        scans = iter([{"scanning": True}, {"scanning": False}])
        searches = []

        def search(user, pw, query, count=5, _retry=True):
            searches.append(_retry)
            return [{"id": "s1", "title": "Song", "artist": "An Artist",
                     "albumId": "nd-al-1", "artistId": "nd-ar-1"}]

        notified = []
        sleeps = []
        with patch.object(bot, "_default_web_user",
                          lambda: {"navidrome_user": "u", "navidrome_password": "p"}), \
                patch.object(bot, "nd_get_scan_status", lambda u, p: next(scans)), \
                patch.object(bot, "_nd_search", search), \
                patch.object(bot.time, "sleep", lambda s: sleeps.append(s)), \
                patch.object(bot, "_notify_hub_library_change",
                             lambda *a, **k: notified.append((a, k))), \
                patch.object(bot, "_album_fill_set", lambda *a, **k: None):
            bot._ND_ALBUM_INDEX["ts"] = time.time()
            bot._start_album_fill_verification(
                "rel-1", {"per_file": [{"status": "matched", "title": "Song",
                                        "recording_mbid": "rec-1"}]},
                artist="An Artist", rgid="rg-1", album="Landed")

        self.assertEqual(sleeps, [bot.PLACEMENT_VERIFY_FAST_INTERVAL],
                         "one fast wait while scanning, then a successful probe")
        self.assertTrue(searches and not any(searches),
                        "verifier probes must skip the 3 s warm-up retry")
        self.assertEqual(len(notified), 1)
        args, kw = notified[0]
        self.assertEqual(args[0], "rel-1")
        self.assertEqual(kw["event"], "albumIndexed")
        self.assertEqual(kw["nd_album_ids"], ["nd-al-1"])
        self.assertEqual(kw["nd_artist_id"], "nd-ar-1")
        self.assertEqual(kw["row"]["rgid"], "rg-1")
        self.assertEqual(kw["row"]["navidrome_album_ids"], ["nd-al-1"])
        self.assertEqual(bot._ND_ALBUM_INDEX["ts"], 0.0)
        stored = {r["rgid"]: r for r in bot._index_get_artist("ak", "")["releases"]}
        self.assertEqual(stored["rg-1"]["navidrome_album_ids"], ["nd-al-1"])

    def test_verifier_ids_never_overwrite_a_scans_own_ids(self):
        self._index_file()
        bot._index_ensure_artist("ak", artist_mbid="ak", name="An Artist")
        bot._index_upsert_release("ak", {"rgid": "rg-2", "title": "Partial",
                                         "status": "incomplete", "group_id": "g2",
                                         "navidrome_album_ids": ["scan-id"]})
        rows = bot._index_set_release_album_ids(group_id="g2", album_ids=["probe-id"])
        self.assertEqual(rows[0]["navidrome_album_ids"], ["scan-id"])

    def test_placement_verify_delay_is_fast_then_backs_off(self):
        now = time.time()
        self.assertEqual(bot._placement_verify_delay(now),
                         bot.PLACEMENT_VERIFY_FAST_INTERVAL)
        self.assertEqual(bot._placement_verify_delay(now - bot.PLACEMENT_VERIFY_FAST_WINDOW - 1),
                         bot.PLACEMENT_VERIFY_INTERVAL)

    def test_a_row_placed_into_a_running_verifier_gets_the_fast_window(self):
        """Q-024(3): one worker runs per group and a later placement joins it,
        so the fast window counted from the worker's start had long closed for
        a row placed minutes in — it was polled every 30 s from the outset. The
        window counts from the newest outstanding row's placement."""
        now = time.time()
        old_start = now - bot.PLACEMENT_VERIFY_FAST_WINDOW - 60
        rows = [(0, {"imported_at": old_start}), (1, {"imported_at": now - 5})]
        clock = bot._placement_verify_clock(old_start, rows)
        self.assertEqual(clock, now - 5)
        self.assertEqual(bot._placement_verify_delay(clock),
                         bot.PLACEMENT_VERIFY_FAST_INTERVAL)
        # Rows older than the worker (a resumed pass): the worker's own start.
        self.assertEqual(bot._placement_verify_clock(now, [(0, {"imported_at": old_start})]), now)
        self.assertEqual(bot._placement_verify_clock(now, [(0, {})]), now)

    def test_the_mbid_probe_wants_the_recording_and_prefers_the_groups_album(self):
        """Q-024(2): the probe took Navidrome's first hit for the MBID — which
        can be the same recording on a compilation, or not that recording at
        all — and the verifier then announced that hit's album."""
        asked = []
        hits = [{"id": "s0", "musicBrainzId": "other", "albumId": "x", "title": "T"},
                {"id": "s1", "musicBrainzId": "m1", "albumId": "comp", "title": "T"},
                {"id": "s2", "musicBrainzId": "M1", "albumId": "al1", "title": "T"}]

        def search(u, p, query, count=5, _retry=True):
            asked.append((query, count))
            return hits if query == "m1" else []

        with patch.object(bot, "_nd_search", search):
            self.assertEqual(bot.nd_track_match("A", "T", "m1", "u", "p",
                                                prefer_album_ids={"al1"})["id"], "s2")
            self.assertEqual(bot.nd_track_match("A", "T", "m1", "u", "p")["id"], "s1")
            self.assertGreater(asked[0][1], 1, "more than one hit to choose from")
            # A hit that is another recording is no evidence; with no title
            # match either, nothing is found.
            hits[:] = [{"id": "s0", "musicBrainzId": "other", "albumId": "x"}]
            self.assertIsNone(bot.nd_track_match("A", "T", "m1", "u", "p"))
        # nd_track_present stays sane: the text probe still finds the song.
        texts = {"m1": [{"id": "s0", "musicBrainzId": "other"}],
                 "T A": [{"id": "s3", "title": "T", "artist": "A"}]}
        with patch.object(bot, "_nd_search",
                          lambda u, p, q, count=5, _retry=True: texts.get(q, [])):
            self.assertTrue(bot.nd_track_present("A", "T", "m1", "u", "p"))

    def test_present_row_backfill_never_asks_navidrome_with_nothing_to_do(self):
        """Called from the post-`albumIndexed` kick and the periodic sweep
        (task 4, off the discography read path now), so the no-op case must
        cost one SELECT and no library fetch."""
        self._index_file()
        bot._index_ensure_artist("ak", artist_mbid="ak", name="An Artist")
        bot._index_upsert_release("ak", {"rgid": "rg", "title": "T",
                                         "status": "complete",
                                         "navidrome_album_ids": ["nd-1"]})

        def _boom(force=False):
            raise AssertionError("the Navidrome index must not be fetched")

        with patch.object(bot, "_nd_album_index", _boom):
            self.assertEqual(bot._index_backfill_present_album_ids("ak"), 0)
            self.assertEqual(bot._index_backfill_present_album_ids(""), 0)

    def test_backfill_sweep_skips_an_artist_it_can_never_match(self):
        """M2: an adopted orphan stub has no name and no Navidrome id, so its
        `present` rows can never match an album — yet the sweep listed it
        every 300 s and walked the whole Navidrome album index for it."""
        self._index_file()
        bot._index_ensure_artist("mb-orphan", artist_mbid="mb-orphan")
        bot._index_upsert_release("mb-orphan", {"rgid": "rg-o", "title": "O",
                                                "status": "present"})
        bot._index_ensure_artist("ak", artist_mbid="ak", name="An Artist")
        bot._index_upsert_release("ak", {"rgid": "rg", "title": "T", "status": "present"})
        with bot._index_lock, bot._index_db() as conn:
            conn.execute("INSERT INTO release_groups (artist_key, rgid, title, status) "
                         "VALUES ('parentless', 'rg-q', 'Q', 'present')")
        self.assertEqual(bot._index_artists_needing_backfill(), ["ak"])

        def _boom(force=False):
            raise AssertionError("the Navidrome index must not be fetched")

        with patch.object(bot, "_nd_album_index", _boom):
            self.assertEqual(bot._index_backfill_present_album_ids("mb-orphan"), 0)

    def test_discography_read_view_does_not_run_the_backfill(self):
        """Task 4: the backfill used to run on every GET, guessing Navidrome
        album ids by title against a 300 s-cached index on the read path. It
        now runs only from `_announce_album_indexed` and the periodic sweep —
        the read must be pure SQLite."""
        self._index_file()
        bot._index_ensure_artist("ak", artist_mbid="ak", name="An Artist")
        bot._index_upsert_release("ak", {"rgid": "rg", "title": "T",
                                         "status": "present"})

        with patch.object(bot, "_index_backfill_present_album_ids") as mock_backfill:
            result = bot._artist_discography_read_view("ak", "")

        mock_backfill.assert_not_called()
        self.assertTrue(result["indexed"])
        self.assertEqual(result["releases"][0]["rgid"], "rg")

    def test_announce_album_indexed_kicks_the_backfill_for_its_artist(self):
        """The per-artist trigger the read-path call moved to: promptly after
        the notify, on a background thread, for the artist the placed album
        belongs to — not for the placed release itself, which already has its
        ids from the write just above (see `test_verifier_ids_never_overwrite_a_scans_own_ids`
        and the verifier test above)."""
        self._index_file()
        self._run_threads_inline()
        bot._index_ensure_artist("mb-ar", artist_mbid="mb-ar",
                                 nd_artist_id="nd-77", name="An Artist")

        calls = []
        with patch.object(bot, "_notify_hub_library_change", lambda *a, **k: None), \
                patch.object(bot, "_index_backfill_present_album_ids",
                             lambda key: calls.append(key)):
            bot._announce_album_indexed(
                "rel-1", "rg-1", "",
                [{"albumId": "nd-al-1", "artistId": "nd-77"}],
                artist="An Artist", album="Landed")

        self.assertEqual(calls, ["mb-ar"])

    def test_announce_album_indexed_skips_the_backfill_with_no_artist_id(self):
        """No song carried an artistId (an odd Navidrome answer): nothing to
        key the backfill on, so it must not run rather than guess."""
        self._index_file()
        self._run_threads_inline()

        calls = []
        with patch.object(bot, "_notify_hub_library_change", lambda *a, **k: None), \
                patch.object(bot, "_index_backfill_present_album_ids",
                             lambda key: calls.append(key)):
            bot._announce_album_indexed("rel-1", "rg-1", "", [{"albumId": "nd-al-1"}],
                                        artist="An Artist", album="Landed")

        self.assertEqual(calls, [])

    def test_library_index_task_reuses_stored_mbid_instead_of_an_empty_stub(self):
        """The empty `nd:` regression (task 4). A stale artist whose Navidrome
        tags carry no mbid this time used to be re-searched by name; when
        MusicBrainz answered nothing, an empty `nd:<id>` row landed with a
        newer `scanned_at` than the real `mb:<mbid>` row sharing that nd id —
        and the nd-id lookup orders by `scanned_at DESC`, so the empty row
        outranked and hid the real discography. The fix: fall back to the
        already-resolved mbid `_index_get_artist` finds via the nd id, no name
        search needed, and never write the stub over a row that already exists."""
        self._index_file()
        bot._index_store_artist(
            {"artist_mbid": "mb-1", "artist_name": "Artist",
             "releases": [{"rgid": "rg-1", "title": "A", "status": "complete"}]},
            nd_artist_id="77")
        with bot._index_lock:
            bot._index_db().execute(
                "UPDATE artists SET scanned_at = 0 WHERE artist_key = 'mb-1'")

        searches = []

        def fail_search(name, limit=1):
            searches.append(name)
            return []

        discog_calls = []

        def fake_discog(mbid, name, *a, **k):
            discog_calls.append(mbid)
            return {"artist_mbid": mbid, "artist_name": name, "review_groups": [],
                    "releases": [{"rgid": "rg-1", "title": "A", "status": "complete"}]}

        user = {"navidrome_user": "u", "navidrome_password": "p"}
        with patch.object(bot, "_artist_index_rows",
                          lambda: [{"id": "77", "name": "Artist", "mbid": ""}]), \
                patch.object(bot, "nd_get_all_albums", lambda *a, **k: []), \
                patch.object(bot, "mbz_search_artists", fail_search), \
                patch.object(bot, "build_artist_discography", fake_discog), \
                patch.object(bot, "_task_update", lambda *a, **k: None), \
                patch.object(bot, "_task_finish", lambda *a, **k: None):
            bot._library_index_task("t1", user)

        self.assertEqual(searches, [], "the stored mbid must be reused, no name search")
        self.assertEqual(discog_calls, ["mb-1"], "the recovered mbid must drive the rescan")
        with bot._index_lock:
            keys = {r["artist_key"] for r in
                    bot._index_db().execute("SELECT artist_key FROM artists").fetchall()}
        self.assertNotIn("nd:77", keys, "must not write an empty stub over a real row")
        stored = bot._index_get_artist("", "77")
        self.assertEqual([r["rgid"] for r in stored["releases"]], ["rg-1"])

    def test_library_index_task_still_stubs_a_genuinely_unresolvable_new_artist(self):
        """Existing behaviour kept: with no stored row and no tag mbid, a failed
        name search still records the miss under the Navidrome id, so the next
        run doesn't burn a MusicBrainz search on it again until it goes stale."""
        self._index_file()

        searches = []

        def fail_search(name, limit=1):
            searches.append(name)
            return []

        user = {"navidrome_user": "u", "navidrome_password": "p"}
        with patch.object(bot, "_artist_index_rows",
                          lambda: [{"id": "99", "name": "Nobody", "mbid": ""}]), \
                patch.object(bot, "nd_get_all_albums", lambda *a, **k: []), \
                patch.object(bot, "mbz_search_artists", fail_search), \
                patch.object(bot, "_task_update", lambda *a, **k: None), \
                patch.object(bot, "_task_finish", lambda *a, **k: None):
            bot._library_index_task("t1", user)

        self.assertEqual(searches, ["Nobody"])
        with bot._index_lock:
            row = bot._index_db().execute(
                "SELECT artist_key FROM artists WHERE artist_key = 'nd:99'").fetchone()
        self.assertIsNotNone(row, "a genuinely never-seen artist still gets its miss recorded")

    def test_library_index_task_refreshes_its_own_miss_stub_past_the_ttl(self):
        """Fix round 1 (review): the write guard is by KEY -- "no row, or this
        artist's own nd:<id> stub" -- not "no row at all". An unconditional
        "never write when any row exists" also blocks refreshing the miss's
        OWN stub once it goes stale, so a permanently unresolvable artist would
        re-enter this branch and re-search MusicBrainz on every single run
        forever with `scanned_at` never moving -- exactly the constant-load
        failure mode task 4 exists to prevent before the auto-index worker."""
        self._index_file()

        searches = []

        def fail_search(name, limit=1):
            searches.append(name)
            return []

        user = {"navidrome_user": "u", "navidrome_password": "p"}

        def run(task_id):
            with patch.object(bot, "_artist_index_rows",
                              lambda: [{"id": "99", "name": "Nobody", "mbid": ""}]), \
                    patch.object(bot, "nd_get_all_albums", lambda *a, **k: []), \
                    patch.object(bot, "mbz_search_artists", fail_search), \
                    patch.object(bot, "_task_update", lambda *a, **k: None), \
                    patch.object(bot, "_task_finish", lambda *a, **k: None):
                bot._library_index_task(task_id, user)

        run("t1")
        self.assertEqual(searches, ["Nobody"], "first run: one search, stub written")

        # The stub is fresh now, so a second run (still within the TTL) must
        # skip it entirely -- no second search.
        run("t2")
        self.assertEqual(searches, ["Nobody"], "still fresh: skipped, no second search")

        # Force the TTL to have expired on the stub itself, exactly as it
        # would after LB_BOT_INDEX_TTL_DAYS, and run again: the artist is
        # genuinely still unresolvable, so this must search once MORE and,
        # critically, must still be ALLOWED to refresh its own stub's
        # `scanned_at` -- not silently skip the write because "a row exists".
        with bot._index_lock:
            bot._index_db().execute(
                "UPDATE artists SET scanned_at = 0 WHERE artist_key = 'nd:99'")

        run("t3")
        self.assertEqual(searches, ["Nobody", "Nobody"],
                         "one more search for the now-stale stub, not zero forever")
        with bot._index_lock:
            row = bot._index_db().execute(
                "SELECT scanned_at FROM artists WHERE artist_key = 'nd:99'").fetchone()
        self.assertGreater(row["scanned_at"], 0,
                           "the stub's own scanned_at must be refreshed, or it can never "
                           "go fresh again and every future run pays another search")

        # And with the refresh in place, an immediate fourth run must go back
        # to being skipped -- one search per TTL window, not per run.
        run("t4")
        self.assertEqual(searches, ["Nobody", "Nobody"],
                         "fresh again after the refresh: skipped, no fourth search")

    def test_fresh_row_album_id_comes_from_the_index(self):
        """A Fresh tile that says "in library" has to be able to open it. The
        mapping is stored album id -> rgid; the row needs the inverse."""
        self._index_file()
        bot._index_upsert_release("ak", {"rgid": "rg1", "title": "One",
                                         "status": "complete",
                                         "navidrome_album_ids": ["nd-a", "nd-b"]})
        bot._index_upsert_release("ak", {"rgid": "rg2", "title": "Two",
                                         "status": "missing"})
        mapping = bot._index_rgid_album_ids()
        self.assertEqual(mapping.get("rg1"), "nd-a")
        self.assertNotIn("rg2", mapping)

    def test_artist_release_classifies_from_caller_metadata(self):
        """mbz_release_group_row parks a transient failure for five minutes and
        answers {} without asking again, so one hiccup was a hard 502 on that
        album for everyone who asked next. Every caller reaches the route from a
        row that already names the release."""
        rg = bot._release_group_row_override("rg1", {
            "title": "Album", "artist": "An Artist", "type": "Album",
            "year": "2026-03-04", "mbid": "artist-mbid"})
        self.assertEqual(rg["rgid"], "rg1")
        self.assertEqual(rg["primary_type"], "album")
        self.assertEqual(rg["year"], "2026")
        self.assertEqual(rg["artist_name"], "An Artist")
        # Classifies by the same rules as a real row.
        release, group = bot._classify_release_group(rg, None, {}, "", "")
        self.assertEqual(release["status"], "missing")
        self.assertEqual(release["title"], "Album")
        self.assertIsNone(group)

    def test_artist_release_override_needs_a_title(self):
        """A title is the one field classification cannot do without, so a
        caller supplying none must still get the 502 rather than a blank row."""
        self.assertEqual(
            bot._release_group_row_override("rg1", {"artist": "An Artist"}), {})
        self.assertEqual(
            bot._release_group_row_override("", {"title": "Album"}), {})

    def test_fresh_releases_cap_never_drops_an_owned_artist(self):
        """The cut is ownership-aware on purpose. An obscure artist you own has
        few listens site-wide, so a plain popularity cut would drop exactly the
        row this page exists for."""
        rows = [{"releaseName": f"r{i}", "artistMbids": [], "listenCount": i,
                 "releaseGroupMbid": f"rg{i}"} for i in range(50)]
        # The one owned row is also the least-listened.
        rows[0]["artistOwned"] = True
        out = bot._fresh_apply_limit(rows, limit=5)
        self.assertEqual(len(out), 5)
        self.assertIn("rg0", [r["releaseGroupMbid"] for r in out],
                      "an owned row must survive the cut whatever its listen count")
        # The rest are the most-listened, and the feed's own date order is kept.
        self.assertEqual([r["releaseGroupMbid"] for r in out],
                         sorted((r["releaseGroupMbid"] for r in out),
                                key=lambda k: int(k[2:])),
                         "the cut must not leave the rows in popularity order")

    def test_fresh_releases_cap_is_a_noop_under_the_limit(self):
        rows = [{"releaseGroupMbid": f"rg{i}", "listenCount": i, "artistOwned": False}
                for i in range(3)]
        self.assertEqual(bot._fresh_apply_limit(rows, limit=10), rows)

    def test_fresh_releases_cap_keeps_every_owned_row_past_the_limit(self):
        """Owned rows are kept even when there are more of them than the limit —
        losing one is the failure the cap exists to avoid, not a tradeoff."""
        rows = [{"releaseGroupMbid": f"rg{i}", "listenCount": 0, "artistOwned": True}
                for i in range(8)]
        out = bot._fresh_apply_limit(rows, limit=3)
        self.assertEqual(len(out), 8)

    def test_release_override_accepts_the_spa_field_names(self):
        """The SPA reuses its /api/album/sources query-string names (album/total)
        for the download body, while both remote clients post title/total_tracks.
        Reading `resolved["title"]` blind meant the SPA's download answered `ok`,
        queued a task, and died inside it with KeyError: 'title' — no slskd
        search, no ledger row, nothing for the client to poll."""
        out = bot._normalize_release_override(
            {"release_mbid": "r1", "artist": "A", "album": "B", "total": 15})
        self.assertEqual(out["title"], "B")
        self.assertEqual(out["total_tracks"], 15)

    def test_release_override_prefers_the_canonical_names(self):
        out = bot._normalize_release_override(
            {"release_mbid": "r1", "artist": "A", "title": "Canon",
             "album": "Alias", "total_tracks": 9, "total": 3})
        self.assertEqual((out["title"], out["total_tracks"]), ("Canon", 9))

    def test_release_override_survives_a_non_numeric_total(self):
        out = bot._normalize_release_override(
            {"release_mbid": "r1", "artist": "A", "album": "B", "total": "twelve"})
        self.assertEqual(out["total_tracks"], 0)

    def test_single_release_add_is_readable_back(self):
        """The write used to store a release_groups row with no artists parent —
        and _index_get_artist selects artists FIRST, so the row was invisible and
        the client re-read answered `indexed: false`, sending it off to start the
        very whole-artist scan this route exists to avoid."""
        self._index_file()
        key = bot._index_artist_key("artist-mbid", "")
        bot._index_ensure_artist(key, artist_mbid="artist-mbid", name="An Artist")
        bot._index_upsert_release(key, {"rgid": "rg1", "title": "Album",
                                        "status": "missing"})

        stored = bot._index_get_artist("artist-mbid", "")
        self.assertIsNotNone(stored, "the release must be readable back")
        self.assertEqual([r["rgid"] for r in stored["releases"]], ["rg1"])

    def test_ensure_artist_never_clobbers_a_real_scan(self):
        """A real scan owns name/scanned_at/scan_version. Stamping this cheap
        path's values over them would make a scanned artist look freshly scanned
        and suppress the rescan prompt."""
        self._index_file()
        bot._index_store_artist(
            {"artist_mbid": "m1", "artist_name": "Real Name", "releases": []}, "")
        bot._index_ensure_artist("m1", artist_mbid="m1", name="Wrong Name")
        with bot._index_lock:
            row = bot._index_db().execute(
                "SELECT name, scanned_at, scan_version FROM artists "
                "WHERE artist_key = ?", ("m1",)).fetchone()
        self.assertEqual(row["name"], "Real Name")
        self.assertGreater(row["scanned_at"], 0)
        self.assertEqual(row["scan_version"], bot.INDEX_SCAN_VERSION)

    def test_existing_artist_key_wins_over_a_minted_one(self):
        """The reader finds an artist three ways; _index_artist_key only mints
        two of them. A scan started from an nd id whose tags had no mbid stores
        artist_key=<resolved mbid>, so a later write that knows only the nd id
        would mint `nd:<id>` and file the release where nothing reads."""
        self._index_file()
        bot._index_store_artist(
            {"artist_mbid": "resolved-mbid", "artist_name": "A", "releases": []},
            "nd-123")
        self.assertEqual(
            bot._index_existing_artist_key("", "nd-123"), "resolved-mbid")
        self.assertEqual(
            bot._index_existing_artist_key("resolved-mbid", ""), "resolved-mbid")
        # Nothing stored for this artist: no key to reuse, and the caller mints.
        self.assertEqual(bot._index_existing_artist_key("", "nd-unknown"), "")

    def test_upsert_release_replaces_a_stale_classification(self):
        """§3's route re-classifies; a row still saying `missing` for a release
        the library now holds has to lose."""
        self._index_file()
        bot._index_upsert_release("ak", {"rgid": "rg", "title": "T",
                                         "status": "missing"})
        bot._index_upsert_release("ak", {"rgid": "rg", "title": "T",
                                         "status": "incomplete",
                                         "group_id": "g1", "present": 9,
                                         "total": 12,
                                         "navidrome_album_ids": ["nd1"]})
        with bot._index_lock:
            rows = bot._index_db().execute(
                "SELECT * FROM release_groups WHERE rgid = ?", ("rg",)).fetchall()
        self.assertEqual(len(rows), 1, "upsert must not duplicate the row")
        self.assertEqual(rows[0]["status"], "incomplete")
        self.assertEqual(rows[0]["group_id"], "g1")
        self.assertEqual(json.loads(rows[0]["nd_album_ids"]), ["nd1"])

    def test_classify_release_group_is_shared_by_scan_and_single_refresh(self):
        """The extraction exists so one album cannot be classified two ways."""
        rg = {"rgid": "rg1", "title": "Album", "year": "2020",
              "primary_type": "album", "secondary_types": []}
        release, group = bot._classify_release_group(rg, None, {}, "", "")
        self.assertEqual(release["status"], "missing")
        self.assertIsNone(group)
        self.assertEqual(release["effective_type"],
                         bot._effective_release_type("album", []))

        untagged, group = bot._classify_release_group(
            rg, [{"id": "nd1", "name": "Album"}], {"method": "title"}, "", "")
        self.assertEqual(untagged["status"], "untagged")
        self.assertEqual(untagged["navidrome_album_ids"], ["nd1"])
        self.assertIsNone(group)

    def test_operation_create_finish_and_payload(self):
        old_review = bot._review_state
        try:
            bot._review_state = bot._empty_review_state()
            op = bot._operation_create("match_files", "Matching")
            self.assertEqual(op["status"], "running")
            done = bot._operation_finish(op["id"], True, "Matched")
            self.assertEqual(done["status"], "success")
            payload = bot._with_operation({"ok": True}, op)
            self.assertEqual(payload["operation_id"], op["id"])
            self.assertEqual(payload["operation"]["status"], "success")
        finally:
            bot._review_state = old_review

    def test_operation_error_redacts_secret_text(self):
        old_review = bot._review_state
        old_key = bot.SLSKD_API_KEY
        try:
            bot.SLSKD_API_KEY = "secret-key"
            bot._review_state = bot._empty_review_state()
            op = bot._operation_create("download", "Queued")
            bot._operation_finish(op["id"], False, "failed secret-key", "secret-key leaked")
            stored = bot._find_operation(op["id"])
            self.assertNotIn("secret-key", str(stored))
            self.assertIn("[redacted", str(stored))
        finally:
            bot.SLSKD_API_KEY = old_key
            bot._review_state = old_review

    def test_operations_survive_a_scan_replacing_its_origin(self):
        with isolated_review():
            op = bot._operation_create("scan", "Scanning")
            bot._replace_review_groups("library", [], "done")
            self.assertIn(op["id"], bot._review_state["operations"])

    def test_move_does_not_inherit_source_mtime_or_mode(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))

        src_dir = os.path.join(tmp, "downloads")
        dst_dir = os.path.join(tmp, "music")
        os.makedirs(src_dir)
        os.makedirs(dst_dir)
        src = os.path.join(src_dir, "track.flac")
        with open(src, "wb") as fh:
            fh.write(b"audio")
        # An old download: stale mtime, and a mode the library must not inherit.
        stale = 10_000_000
        os.utime(src, (stale, stale))
        os.chmod(src, 0o600)

        dest = os.path.join(dst_dir, "01 - track.flac")
        # Go through the real placement helper. Doing the move inline here is what
        # made this test vacuous for so long: on Windows /downloads and /music were
        # different volumes, so the copy happened to reset the mode and the
        # assertion passed without _place_file's explicit chmod ever being
        # exercised. Inside one filesystem shutil.move is an os.rename, which keeps
        # mode 0o600 — so the chmod is the only thing that can make this pass.
        bot._place_file(src, dest)

        self.assertFalse(os.path.exists(src), "the source must be gone after a move")
        self.assertGreater(os.path.getmtime(dest), stale,
                           "placed file must look modified now, not at download time")
        if os.name == "posix":
            self.assertEqual(os.stat(dest).st_mode & 0o777, 0o664,
                             "placed file must carry the library's mode, not the source's")

    def test_touch_survives_a_missing_path(self):
        # A failed touch must never fail an otherwise-good placement.
        self.assertFalse(bot._touch("/nonexistent/path/for/sure"))

    # ── Placement identity guard ────────────────────────────────────────────
    #
    # The reported failure: an 11-track album, 9 gaps, came back with 8 filled,
    # 1 failed, and two copies of one song. Placement matched slot "Sing" to
    # "05 - Singularity.flac", rewrote that file's title and MBID to Sing's, and
    # filed it next to the Singularity the library already had — so the gap
    # stayed open and no tag-based duplicate scan could see the pair.

    def test_same_audio_needs_proof_not_a_duration_guess(self):
        # md5 is proof either way.
        self.assertTrue(bot._same_audio_exact({"md5": "aa"}, {"md5": "aa"}))
        self.assertFalse(bot._same_audio_exact({"md5": "aa"}, {"md5": "bb"}))
        # Identical sample count at the same rate is equally exact.
        self.assertTrue(bot._same_audio_exact(
            {"samples": 9535488, "sample_rate": 44100},
            {"samples": 9535488, "sample_rate": 44100}))
        self.assertFalse(bot._same_audio_exact(
            {"samples": 9535488, "sample_rate": 44100},
            {"samples": 9535489, "sample_rate": 44100}))
        # Duration + size alone must NOT count: two different 3:47 tracks off one
        # CD would pair and a legitimate placement would be refused.
        self.assertFalse(bot._same_audio_exact(
            {"length": 227.0, "sample_rate": 44100, "channels": 2, "size": 30_000_000},
            {"length": 227.4, "sample_rate": 44100, "channels": 2, "size": 30_100_000}))
        self.assertFalse(bot._same_audio_exact({}, {"md5": "aa"}))

    def _placement_fixture(self, download_names, existing_names=()):
        """A temp /downloads folder and a temp library with an album folder."""
        import shutil as _shutil
        tmp = tempfile.mkdtemp()
        self.addCleanup(_shutil.rmtree, tmp, True)
        dl = os.path.join(tmp, "downloads", "peer folder")
        lib = os.path.join(tmp, "music")
        album = os.path.join(lib, "Artist", "Album")
        os.makedirs(dl)
        os.makedirs(album)
        for name in download_names:
            with open(os.path.join(dl, name), "wb") as fh:
                fh.write(b"\0" * 16)
        for name in existing_names:
            with open(os.path.join(album, name), "wb") as fh:
                fh.write(b"\0" * 16)
        return tmp, dl, lib, album

    @patch("listenbrainz_bot.rgid_from_release", lambda *a, **k: "")
    @patch("listenbrainz_bot.mbz_release_display", lambda *a, **k: {})
    @patch("listenbrainz_bot._default_web_user", lambda *a, **k: None)
    @patch("listenbrainz_bot.mbz_release_tracks")
    def test_placement_refuses_the_wrong_file_for_a_loose_title_match(self, mock_tracks):
        # "Sing" is the gap; "Singularity" is already in the library. The peer
        # folder holds only Singularity, so the correct outcome is "no file
        # matched" -- not a forged copy of Singularity tagged as Sing.
        mock_tracks.return_value = [
            {"title": "Sing", "mbid": "r-sing", "position": 3},
            {"title": "Singularity", "mbid": "r-singularity", "position": 5},
        ]
        tmp, dl, lib, album = self._placement_fixture(["05 - Singularity.flac"])
        with patch.object(bot, "MUSIC_LIBRARY_PATH", lib):
            result = bot._deterministic_album_import(dl, "rel1", "Artist", "Album")
        by_slot = {r.get("title"): r for r in result["per_file"]}
        self.assertEqual(by_slot["Sing"]["status"], "unmatched")
        self.assertIn("no downloaded file matched", by_slot["Sing"]["reason"])
        # The refusal names what it turned down. Bare "no file matched" reads
        # as "the source was empty" when the truth is that the one candidate
        # belongs to another track — which is the difference between a
        # diagnosable failure and a mystery.
        self.assertIn("Singularity", by_slot["Sing"]["reason"])
        # The file belongs to Singularity and is placed there, once.
        self.assertEqual(by_slot["Singularity"]["status"], "matched")
        self.assertEqual(result["moved"], 1)

    @patch("listenbrainz_bot.rgid_from_release", lambda *a, **k: "")
    @patch("listenbrainz_bot.mbz_release_display", lambda *a, **k: {})
    @patch("listenbrainz_bot._default_web_user", lambda *a, **k: None)
    @patch("listenbrainz_bot.mbz_release_tracks")
    def test_placement_refuses_audio_already_in_the_album(self, mock_tracks):
        mock_tracks.return_value = [{"title": "One", "mbid": "r1", "position": 1}]
        tmp, dl, lib, album = self._placement_fixture(
            ["01 - One.flac"], existing_names=["already there.flac"])

        def fake_sig(path):
            # Both files carry the same audio md5 under different names.
            return {"md5": "deadbeef", "own_title": "", "own_title_key": "",
                    "own_mbids": set(), "size": 16}

        with patch.object(bot, "MUSIC_LIBRARY_PATH", lib), \
             patch.object(bot, "_audio_signature", fake_sig):
            result = bot._deterministic_album_import(dl, "rel1", "Artist", "Album")
        self.assertEqual(result["moved"], 0)
        row = result["per_file"][0]
        self.assertEqual(row["status"], "rejected")
        self.assertIn("already in the album", row["reason"])
        self.assertIn("already in the album", result["error"])
        # The download is left where it was for a manual source pick.
        self.assertTrue(os.path.exists(os.path.join(dl, "01 - One.flac")))

    @patch("listenbrainz_bot.rgid_from_release", lambda *a, **k: "")
    @patch("listenbrainz_bot.mbz_release_display", lambda *a, **k: {})
    @patch("listenbrainz_bot._default_web_user", lambda *a, **k: None)
    @patch("listenbrainz_bot.mbz_release_tracks")
    def test_placement_refuses_a_file_tagged_as_another_track_of_the_release(self, mock_tracks):
        mock_tracks.return_value = [
            {"title": "Sing", "mbid": "r-sing", "position": 3},
            {"title": "Singularity", "mbid": "r-singularity", "position": 5},
        ]
        # Filename says Sing, the file's own tags say Singularity.
        tmp, dl, lib, album = self._placement_fixture(["03 - Sing.flac"])

        def fake_sig(path):
            return {"own_title": "Singularity",
                    "own_title_key": bot._match_key("Singularity"),
                    "own_mbids": set(), "size": 16}

        with patch.object(bot, "MUSIC_LIBRARY_PATH", lib), \
             patch.object(bot, "_audio_signature", fake_sig):
            result = bot._deterministic_album_import(dl, "rel1", "Artist", "Album")
        row = [r for r in result["per_file"] if r.get("title") == "Sing"][0]
        self.assertEqual(row["status"], "rejected")
        self.assertIn("Singularity", row["reason"])
        self.assertEqual(result["moved"], 0)

    @patch("listenbrainz_bot.rgid_from_release", lambda *a, **k: "")
    @patch("listenbrainz_bot.mbz_release_display", lambda *a, **k: {})
    @patch("listenbrainz_bot._default_web_user", lambda *a, **k: None)
    @patch("listenbrainz_bot.mbz_release_tracks")
    def test_placement_still_files_a_completely_untagged_download(self, mock_tracks):
        # The regression that matters most: most Soulseek files carry nothing to
        # contradict the slot, and the guard must not turn a working fill into a
        # no-op.
        mock_tracks.return_value = [{"title": "One", "mbid": "r1", "position": 1}]
        tmp, dl, lib, album = self._placement_fixture(["01 - One.flac"])
        with patch.object(bot, "MUSIC_LIBRARY_PATH", lib), \
             patch.object(bot, "_audio_signature", lambda path: {}):
            result = bot._deterministic_album_import(dl, "rel1", "Artist", "Album")
        self.assertTrue(result["ok"])
        self.assertEqual(result["moved"], 1)
        self.assertEqual(result["per_file"][0]["status"], "matched")

    @patch("listenbrainz_bot.rgid_from_release", lambda *a, **k: "")
    @patch("listenbrainz_bot.mbz_release_display", lambda *a, **k: {})
    @patch("listenbrainz_bot._default_web_user", lambda *a, **k: None)
    @patch("listenbrainz_bot.mbz_release_tracks")
    def test_placement_only_fills_the_slots_the_group_is_missing(self, mock_tracks):
        # A slot already in the library must not claim a file downloaded for a gap.
        mock_tracks.return_value = [
            {"title": "One", "mbid": "r1", "position": 1},
            {"title": "Two", "mbid": "r2", "position": 2},
        ]
        tmp, dl, lib, album = self._placement_fixture(["01 - One.flac", "02 - Two.flac"])
        group = {"id": "g1", "missing_tracks": [
            {"title": "Two", "recording_mbid": "r2", "decision": "downloaded"}]}
        with patch.object(bot, "MUSIC_LIBRARY_PATH", lib), \
             patch.object(bot, "_audio_signature", lambda path: {}), \
             patch.object(bot, "_find_review_group", lambda gid: group):
            result = bot._deterministic_album_import(dl, "rel1", "Artist", "Album",
                                                     group_id="g1")
        slots = [r.get("title") for r in result["per_file"] if r.get("title")]
        self.assertEqual(slots, ["Two"])
        # "01 - One.flac" had no slot, so it goes through the bonus pass -- which
        # is guarded too, and carries no slot identity for the bookkeeping.
        bonus = [r for r in result["per_file"] if not r.get("title")]
        self.assertEqual([r["status"] for r in bonus], ["bonus"])
        self.assertEqual(bot._placement_outcomes(result).keys(),
                         {"r2", bot._match_key("Two")})

    def test_placement_honours_a_selected_file_subset(self):
        tmp, dl, lib, album = self._placement_fixture(["01 - One.flac", "02 - Two.flac"])
        with patch.object(bot, "MUSIC_LIBRARY_PATH", lib), \
             patch.object(bot, "mbz_release_tracks", lambda *a, **k: [
                 {"title": "One", "mbid": "r1", "position": 1},
                 {"title": "Two", "mbid": "r2", "position": 2}]), \
             patch.object(bot, "mbz_release_display", lambda *a, **k: {}), \
             patch.object(bot, "rgid_from_release", lambda *a, **k: ""), \
             patch.object(bot, "_default_web_user", lambda *a, **k: None), \
             patch.object(bot, "_audio_signature", lambda path: {}):
            result = bot._deterministic_album_import(
                dl, "rel1", "Artist", "Album", only_relpaths=["02 - Two.flac"])
        self.assertEqual(result["moved"], 1)
        self.assertTrue(os.path.exists(os.path.join(dl, "01 - One.flac")))

    # ── Manual file pick ────────────────────────────────────────────────────

    def _manual_pair_fixture(self):
        """Two downloaded files whose names cross-claim two slots."""
        tmp, dl, lib, album = self._placement_fixture(
            ["01 - Singularity.flac", "02 - Sing.flac"])
        return tmp, dl, lib, album

    def test_manual_pair_beats_the_matcher_and_the_tag_contradiction(self):
        # The user picked "01 - Singularity.flac" for the slot "Sing". The
        # matcher would pair it with Singularity, and _reject_reason would refuse
        # it for the slot's own tags — both are exactly the judgement overridden.
        tmp, dl, lib, album = self._manual_pair_fixture()
        picked = os.path.join(dl, "01 - Singularity.flac")
        sigs = {picked: {"own_title": "Singularity",
                         "own_title_key": bot._match_key("Singularity"),
                         "own_mbids": {"r-singularity"}}}
        with patch.object(bot, "MUSIC_LIBRARY_PATH", lib), \
             patch.object(bot, "mbz_release_tracks", lambda *a, **k: [
                 {"title": "Sing", "mbid": "r-sing", "position": 1},
                 {"title": "Singularity", "mbid": "r-singularity", "position": 5}]), \
             patch.object(bot, "mbz_release_display", lambda *a, **k: {}), \
             patch.object(bot, "rgid_from_release", lambda *a, **k: ""), \
             patch.object(bot, "_default_web_user", lambda *a, **k: None), \
             patch.object(bot, "_audio_signature", lambda path: sigs.get(path, {})):
            result = bot._deterministic_album_import(
                dl, "rel1", "Artist", "Album",
                manual_pairs={"r-sing": picked})
        rows = {r.get("title"): r for r in result["per_file"] if r.get("title")}
        self.assertEqual(rows["Sing"]["confidence"], "manual")
        self.assertEqual(rows["Sing"]["status"], "matched")
        # And the file it was picked for is not also claimed by its "own" slot.
        self.assertNotEqual(rows["Singularity"]["status"], "matched")

    def test_manual_pair_still_refuses_audio_already_in_the_album(self):
        tmp, dl, lib, album = self._placement_fixture(
            ["01 - One.flac"], existing_names=["already there.flac"])
        picked = os.path.join(dl, "01 - One.flac")
        same = {"md5": "deadbeef"}
        with patch.object(bot, "MUSIC_LIBRARY_PATH", lib), \
             patch.object(bot, "mbz_release_tracks", lambda *a, **k: [
                 {"title": "One", "mbid": "r1", "position": 1}]), \
             patch.object(bot, "mbz_release_display", lambda *a, **k: {}), \
             patch.object(bot, "rgid_from_release", lambda *a, **k: ""), \
             patch.object(bot, "_default_web_user", lambda *a, **k: None), \
             patch.object(bot, "_audio_signature", lambda path: same):
            result = bot._deterministic_album_import(
                dl, "rel1", "Artist", "Album", manual_pairs={"r1": picked})
        self.assertFalse(result["ok"])
        self.assertIn("already in the album", result["error"])
        # The refusal reaches the review track as a forceable one.
        group = {"id": "g1", "missing_tracks": [
            {"title": "One", "recording_mbid": "r1", "decision": "downloaded",
             "manual_pick": {"username": "peer", "filename": "01 - One.flac"}}]}
        with patch.object(bot, "_pop_album_groups_for_review_group", lambda gid: None):
            bot._mark_group_tracks_placed(group, result)
        track = group["missing_tracks"][0]
        self.assertTrue(track["can_force_place"])
        self.assertIn("already there.flac", track["force_place_conflict"])

    def test_place_anyway_overrides_the_audio_guard(self):
        tmp, dl, lib, album = self._placement_fixture(
            ["01 - One.flac"], existing_names=["already there.flac"])
        picked = os.path.join(dl, "01 - One.flac")
        group = {"id": "g1", "artist": "Artist", "album": "Album",
                 "canonical_mbid": "rel1", "missing_tracks": [
                     {"title": "One", "recording_mbid": "r1",
                      "decision": "failed", "local_path": picked,
                      "can_force_place": True,
                      "manual_pick": {"username": "peer",
                                      "filename": "01 - One.flac"}}]}
        with patch.object(bot, "MUSIC_LIBRARY_PATH", lib), \
             patch.object(bot, "mbz_release_tracks", lambda *a, **k: [
                 {"title": "One", "mbid": "r1", "position": 1}]), \
             patch.object(bot, "mbz_release_display", lambda *a, **k: {}), \
             patch.object(bot, "rgid_from_release", lambda *a, **k: ""), \
             patch.object(bot, "_default_web_user", lambda *a, **k: None), \
             patch.object(bot, "_find_review_group", lambda gid: group), \
             patch.object(bot, "_pop_album_groups_for_review_group", lambda gid: None), \
             patch.object(bot, "_start_placement_verification", lambda gid: None), \
             patch.object(bot, "_nd_scan_after_import", lambda token: False), \
             patch.object(bot, "_audio_signature", lambda path: {"md5": "deadbeef"}):
            result = bot._place_track_anyway(group, 0)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(group["missing_tracks"][0]["decision"], "placed")

    def test_source_files_view_emits_the_full_peer_filename(self):
        fd = {"username": "peer", "folder": "Artist - Album",
              "files": [{"filename": "@@dir\\Artist - Album\\01 - One.flac",
                         "size": 30_000_000}]}
        rows = bot._source_files_view(fd, {"matched_tracks": []})
        self.assertEqual(rows[0]["filename"], "01 - One.flac")
        self.assertEqual(rows[0]["peerFilename"], "@@dir\\Artist - Album\\01 - One.flac")

    def test_expanding_a_source_re_pairs_against_the_full_listing(self):
        # The search only hit one file; the peer's folder holds both. Coverage
        # computed over search hits alone reported a track the source really has
        # as missing.
        hit = {"filename": "dir\\01 - One.flac", "size": 1}
        rest = [hit, {"filename": "dir\\02 - Two.flac", "size": 1}]
        fd = {"username": "peer", "folder": "dir", "raw_folder": "dir", "files": [hit]}
        group = {"id": "g1", "missing_tracks": [
            {"title": "One", "position": 1, "decision": "approved"},
            {"title": "Two", "position": 2, "decision": "approved"}]}
        with patch.object(bot, "slskd_expand_directory", lambda u, f, r: rest), \
             patch.object(bot, "_release_track_titles", lambda g: []):
            before = bot._source_coverage_summary(fd, group)
            payload = bot._expanded_source_payload(group, fd, 0)
        self.assertEqual(before["matched"], 1)
        self.assertTrue(payload["expanded"])
        self.assertEqual(payload["coverageDetail"]["haveTracks"], 2)
        self.assertEqual(fd["_expanded"], rest, "the listing is cached on the folder")

    # ── Deletion safety ─────────────────────────────────────────────────────

    def _trash_fixture(self):
        import shutil as _shutil
        tmp = tempfile.mkdtemp()
        self.addCleanup(_shutil.rmtree, tmp, True)
        lib = os.path.join(tmp, "music")
        album = os.path.join(lib, "Artist", "Album")
        os.makedirs(album)
        return tmp, lib, album

    def test_delete_refuses_when_no_other_copy_survives(self):
        tmp, lib, album = self._trash_fixture()
        only = os.path.join(album, "01 - One.flac")
        open(only, "wb").write(b"\0" * 16)
        stored = [{"files": [{"path": only},
                             {"path": os.path.join(album, "gone.flac")}]}]
        with patch.object(bot, "MUSIC_LIBRARY_PATH", lib), \
             patch.dict(bot._review_state, {"duplicate_files": stored}):
            self.assertIn("last one", bot._last_copy_refusal(only))
        # A surviving sibling makes it deletable again.
        sibling = os.path.join(album, "01 - One (1).flac")
        open(sibling, "wb").write(b"\0" * 16)
        stored = [{"files": [{"path": only}, {"path": sibling}]}]
        with patch.object(bot, "MUSIC_LIBRARY_PATH", lib), \
             patch.dict(bot._review_state, {"duplicate_files": stored}):
            self.assertEqual(bot._last_copy_refusal(only), "")

    def test_delete_moves_to_trash_and_restore_puts_it_back(self):
        tmp, lib, album = self._trash_fixture()
        target = os.path.join(album, "01 - One.flac")
        open(target, "wb").write(b"\0" * 16)
        trash = os.path.join(tmp, "trash")
        with patch.object(bot, "MUSIC_LIBRARY_PATH", lib), \
             patch.object(bot, "LB_BOT_TRASH_DIR", trash):
            entry = bot._move_to_trash(target)
            self.assertFalse(os.path.exists(target))
            self.assertTrue(os.path.isfile(entry["trash_path"]))
            # Laid out as <date>/<path relative to the library>.
            self.assertIn(os.path.join("Artist", "Album"), entry["trash_path"])
            listed = bot._trash_manifest_read()
            self.assertEqual([r["original"] for r in listed], [target])
            self.assertTrue(bot._restore_from_trash(entry["trash_path"])["ok"])
            self.assertTrue(os.path.isfile(target))
            self.assertEqual(bot._trash_manifest_read(), [])

    def test_restore_refuses_an_occupied_original_path(self):
        tmp, lib, album = self._trash_fixture()
        target = os.path.join(album, "01 - One.flac")
        open(target, "wb").write(b"\0" * 16)
        trash = os.path.join(tmp, "trash")
        with patch.object(bot, "MUSIC_LIBRARY_PATH", lib), \
             patch.object(bot, "LB_BOT_TRASH_DIR", trash):
            entry = bot._move_to_trash(target)
            open(target, "wb").write(b"\0" * 32)   # something took the slot back
            result = bot._restore_from_trash(entry["trash_path"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "occupied")

    # ── Duplicate detection ─────────────────────────────────────────────────

    def test_duplicate_files_group_on_audio_when_tags_were_forged(self):
        # The exact reported case. Placement rewrote the mis-slotted file's title
        # and MBID to the slot it guessed, so the two copies share neither tag
        # key. Tag-only grouping returns nothing; the audio md5 catches it.
        record = {"id": "alb", "name": "Album", "artist": "Artist", "tracks": [
            {"id": "s1", "title": "Singularity", "musicBrainzId": "r-singularity",
             "track": 5, "path": "/music/Artist/Album/05 - Singularity.flac",
             "suffix": "flac", "size": 30_000_000},
            {"id": "s2", "title": "Sing", "musicBrainzId": "r-sing",
             "track": 3, "path": "/music/Artist/Album/03 - Sing.flac",
             "suffix": "flac", "size": 30_000_000},
        ]}
        sigs = {"/music/Artist/Album/05 - Singularity.flac": {"md5": "same", "size": 30_000_000},
                "/music/Artist/Album/03 - Sing.flac": {"md5": "same", "size": 30_000_000}}
        with patch.object(bot, "_album_tracks_with_disk", lambda r, stats=None: r["tracks"]), \
             patch.object(bot, "_file_signature_cached", lambda p: sigs.get(p, {})):
            sets = bot._duplicate_file_sets(record)
        self.assertEqual(len(sets), 1)
        self.assertEqual(sets[0]["matchBasis"], "audio")
        self.assertEqual(len(sets[0]["files"]), 2)

    def test_duplicate_files_still_group_on_tags_without_signatures(self):
        record = {"id": "alb", "name": "Album", "artist": "Artist", "tracks": [
            {"id": "s1", "title": "One", "musicBrainzId": "", "track": 1,
             "path": "/music/A/B/01 - One.flac", "suffix": "flac"},
            {"id": "s2", "title": "One", "musicBrainzId": "", "track": 1,
             "path": "/music/A/B/01 - One (1).flac", "suffix": "flac"},
        ]}
        with patch.object(bot, "_album_tracks_with_disk", lambda r, stats=None: r["tracks"]), \
             patch.object(bot, "_file_signature_cached", lambda p: {}):
            sets = bot._duplicate_file_sets(record)
        self.assertEqual(len(sets), 1)
        self.assertEqual(sets[0]["matchBasis"], "tags")

    def test_duplicate_files_no_longer_group_on_stream_shape(self):
        # The removed rule. Rate and channels are constant across a rip, so it
        # was duration alone, and the +-2% size gate is a no-op for CBR: two
        # different songs of equal length grouped, and union-find chained them
        # into sets of three and more. A guess must not feed a delete button.
        record = {"id": "alb", "name": "Album", "artist": "Artist", "tracks": [
            {"id": "s1", "title": "Alpha", "musicBrainzId": "r1", "track": 1,
             "path": "/music/A/B/01.opus", "suffix": "opus"},
            {"id": "s2", "title": "Beta", "musicBrainzId": "r2", "track": 2,
             "path": "/music/A/B/02.opus", "suffix": "opus"},
        ]}
        sigs = {"/music/A/B/01.opus": {"length": 227.2, "sample_rate": 48000,
                                       "channels": 2, "size": 5_000_000},
                "/music/A/B/02.opus": {"length": 227.4, "sample_rate": 48000,
                                       "channels": 2, "size": 5_050_000}}
        with patch.object(bot, "_album_tracks_with_disk", lambda r, stats=None: r["tracks"]), \
             patch.object(bot, "_file_signature_cached", lambda p: sigs.get(p, {})):
            self.assertEqual(bot._duplicate_file_sets(record), [])

    def test_duplicate_file_basis_is_only_audio_or_tags(self):
        record = {"id": "alb", "name": "Album", "artist": "Artist", "tracks": [
            {"id": "s1", "title": "One", "musicBrainzId": "", "track": 1,
             "path": "/music/A/B/01.flac", "suffix": "flac"},
            {"id": "s2", "title": "One", "musicBrainzId": "", "track": 1,
             "path": "/music/A/B/01 (1).flac", "suffix": "flac"},
        ]}
        with patch.object(bot, "_album_tracks_with_disk", lambda r, stats=None: r["tracks"]), \
             patch.object(bot, "_file_signature_cached", lambda p: {}):
            sets = bot._duplicate_file_sets(record)
        self.assertTrue(sets)
        for s in sets:
            self.assertIn(s["matchBasis"], ("audio", "tags"))

    def test_duplicate_set_never_lists_one_file_twice(self):
        # The reported regression, forced the way the real bug produced it: a
        # Navidrome row and a disk row naming the *same file* by two different
        # strings. They carried the same stream md5 (they are one file), grouped
        # as "audio", and the disk row — hardcoded bitRate 0 — always sorted
        # second, i.e. was always the deletable row.
        import shutil as _shutil
        tmp = tempfile.mkdtemp()
        self.addCleanup(_shutil.rmtree, tmp, True)
        album = os.path.join(tmp, "Artist", "Album")
        os.makedirs(album)
        real = os.path.join(album, "01 - One.flac")
        open(real, "wb").write(b"\0" * 16)
        # A second name for the very same file — the portable stand-in for the
        # real causes (NFC vs NFD, case, symlinks, bind mounts). normpath sees
        # two different strings; one os.stat sees one file.
        alias = os.path.join(album, "01 - One (alias).flac")
        try:
            os.link(real, alias)
        except (OSError, AttributeError, NotImplementedError):
            self.skipTest("filesystem does not support hard links")
        record = {"id": "alb", "name": "Album", "artist": "Artist", "tracks": [
            {"id": "s1", "title": "One", "musicBrainzId": "r1", "track": 1,
             "path": real, "suffix": "flac", "bitRate": 900}]}
        with patch.object(bot, "_audio_files_in_folder",
                          lambda folder, limit=80, recursive=True: [
                              {"name": "01 - One.flac", "path": alias,
                               "relpath": "01 - One.flac", "size": 16}]), \
             patch.object(bot, "_audio_file_tags", lambda p: {"title": "One"}):
            rows = bot._album_tracks_with_disk(record)
        self.assertEqual(len(rows), 1, "one file must not produce two rows")
        self.assertFalse(rows[0].get("onlyOnDisk"))

    def test_duplicate_sets_never_share_a_file_across_albums(self):
        # build_duplicate_file_review iterates per Navidrome album id while the
        # disk walk is per folder, so two album ids over one folder each emitted
        # sets over the other's files with no cross-set dedupe anywhere.
        tracks = [
            {"id": "s1", "title": "One", "musicBrainzId": "", "track": 1,
             "path": "/music/A/B/01.flac", "suffix": "flac"},
            {"id": "s2", "title": "One", "musicBrainzId": "", "track": 1,
             "path": "/music/A/B/01 (1).flac", "suffix": "flac"},
        ]
        albums = [{"id": "alb1", "name": "Album", "artist": "Artist", "songCount": 2},
                  {"id": "alb2", "name": "Album", "artist": "Artist", "songCount": 2}]
        record = {"name": "Album", "artist": "Artist", "tracks": tracks}
        with patch.object(bot, "nd_get_all_albums", lambda u, p, stats=None: albums), \
             patch.object(bot, "_album_record", lambda a, u, p: {**record, "id": a["id"]}), \
             patch.object(bot, "_album_tracks_with_disk", lambda r, stats=None: r["tracks"]), \
             patch.object(bot, "_file_signature_cached", lambda p: {}), \
             patch.object(bot, "_flush_file_signatures", lambda: None):
            sets = bot.build_duplicate_file_review("u", "p")
        self.assertEqual(len(sets), 1)
        seen = [f["path"] for s in sets for f in s["files"]]
        self.assertEqual(len(seen), len(set(seen)))

    def test_disk_walk_is_scoped_to_the_album_directory(self):
        import shutil as _shutil
        tmp = tempfile.mkdtemp()
        self.addCleanup(_shutil.rmtree, tmp, True)
        album = os.path.join(tmp, "Artist", "Album")
        other = os.path.join(album, "Disc 2 of another album")
        os.makedirs(other)
        known = os.path.join(album, "01 - One.flac")
        nested = os.path.join(other, "01 - Elsewhere.flac")
        for p in (known, nested):
            open(p, "wb").write(b"\0" * 16)
        record = {"id": "alb", "tracks": [
            {"id": "s1", "title": "One", "path": known, "suffix": "flac"}]}
        with patch.object(bot, "_audio_file_tags", lambda p: {}):
            rows = bot._album_tracks_with_disk(record)
        self.assertEqual([os.path.normpath(r["path"]) for r in rows],
                         [os.path.normpath(known)])

    def test_disk_walk_skips_the_library_root(self):
        import shutil as _shutil
        tmp = tempfile.mkdtemp()
        self.addCleanup(_shutil.rmtree, tmp, True)
        loose = os.path.join(tmp, "01 - One.flac")
        stray = os.path.join(tmp, "02 - Unrelated.flac")
        for p in (loose, stray):
            open(p, "wb").write(b"\0" * 16)
        record = {"id": "alb", "tracks": [
            {"id": "s1", "title": "One", "path": loose, "suffix": "flac"}]}
        stats = {}
        with patch.object(bot, "MUSIC_LIBRARY_PATH", tmp), \
             patch.object(bot, "_audio_file_tags", lambda p: {}):
            rows = bot._album_tracks_with_disk(record, stats=stats)
        self.assertEqual(len(rows), 1)
        self.assertEqual(stats.get("disk_walk_skipped_root"), 1)

    def test_album_tracks_include_files_navidrome_has_not_indexed(self):
        import shutil as _shutil
        tmp = tempfile.mkdtemp()
        self.addCleanup(_shutil.rmtree, tmp, True)
        album = os.path.join(tmp, "Artist", "Album")
        os.makedirs(album)
        known = os.path.join(album, "01 - One.flac")
        fresh = os.path.join(album, "01 - One_new.flac")
        for p in (known, fresh):
            open(p, "wb").write(b"\0" * 16)
        record = {"id": "alb", "tracks": [
            {"id": "s1", "title": "One", "path": known, "suffix": "flac"}]}
        rows = bot._album_tracks_with_disk(record)
        by_path = {os.path.normpath(r["path"]): r for r in rows}
        self.assertEqual(len(by_path), 2)
        self.assertTrue(by_path[os.path.normpath(fresh)]["onlyOnDisk"])
        self.assertFalse(by_path[os.path.normpath(known)].get("onlyOnDisk"))

    def test_song_path_rebases_a_foreign_navidrome_root(self):
        import shutil as _shutil
        tmp = tempfile.mkdtemp()
        self.addCleanup(_shutil.rmtree, tmp, True)
        album = os.path.join(tmp, "Artist", "Album")
        os.makedirs(album)
        target = os.path.join(album, "01 - One.flac")
        open(target, "wb").write(b"\0")
        with patch.object(bot, "MUSIC_LIBRARY_PATH", tmp), \
             patch.dict(bot._nd_path_prefix, {"strip": None}):
            # Navidrome reports the path as *its own* container sees it.
            got = bot._song_abs_path({"path": "/data/music/Artist/Album/01 - One.flac"})
            self.assertEqual(os.path.normpath(got), os.path.normpath(target))
            # The learned prefix serves the next lookup without another walk.
            self.assertEqual(bot._nd_path_prefix["strip"], "/data/music")

    def test_song_path_handles_backslashes_and_encoding(self):
        import shutil as _shutil
        tmp = tempfile.mkdtemp()
        self.addCleanup(_shutil.rmtree, tmp, True)
        album = os.path.join(tmp, "Artist", "Album Name")
        os.makedirs(album)
        target = os.path.join(album, "01 - One.flac")
        open(target, "wb").write(b"\0")
        with patch.object(bot, "MUSIC_LIBRARY_PATH", tmp), \
             patch.dict(bot._nd_path_prefix, {"strip": None}):
            self.assertEqual(
                os.path.normpath(bot._song_abs_path(
                    {"path": "Artist\\Album Name\\01 - One.flac"})),
                os.path.normpath(target))
            self.assertEqual(
                os.path.normpath(bot._song_abs_path(
                    {"path": "Artist/Album%20Name/01%20-%20One.flac"})),
                os.path.normpath(target))

    def test_song_path_never_rebases_onto_a_bare_filename(self):
        # Matching on the filename alone would pair unrelated albums that happen
        # to share a track name.
        import shutil as _shutil
        tmp = tempfile.mkdtemp()
        self.addCleanup(_shutil.rmtree, tmp, True)
        open(os.path.join(tmp, "01 - One.flac"), "wb").write(b"\0")
        with patch.object(bot, "MUSIC_LIBRARY_PATH", tmp), \
             patch.dict(bot._nd_path_prefix, {"strip": None}):
            got = bot._song_abs_path({"path": "/elsewhere/Other Album/01 - One.flac"})
        self.assertEqual(os.path.normpath(got),
                         os.path.normpath("/elsewhere/Other Album/01 - One.flac"))
        self.assertIsNone(bot._nd_path_prefix["strip"])

    @patch("listenbrainz_bot.mbz_release_tracks")
    def test_duplicate_copies_are_visible_in_the_present_count(self, mock_tracks):
        mock_tracks.return_value = [
            {"title": "One", "mbid": "r1", "position": 1},
            {"title": "Two", "mbid": "r2", "position": 2},
        ]
        # Two files for "One", none for "Two": present must not silently absorb
        # the duplicate copy.
        records = [{"tracks": [
            {"title": "One", "musicBrainzId": "r1", "path": "/music/A/B/01.flac"},
            {"title": "One", "musicBrainzId": "r1", "path": "/music/A/B/01_new.flac"},
        ]}]
        info = bot._missing_for_album_records(records, "rel1", "Artist")
        self.assertEqual(info["present"], 1)
        self.assertEqual(info["total"], 2)
        self.assertEqual(info["extra"], 1)
        self.assertEqual([t["title"] for t in info["missing"]], ["Two"])

    # ── Cross-language duplicate matching ───────────────────────────────────

    def test_cross_language_pair_confirmed_without_any_mbid(self):
        # A Japanese release and its English-titled copy share no text at all, and
        # the untagged rip has no MBID -- which used to reject the pair outright,
        # since confirmation required a release-group from *both* sides.
        albums = [
            {"id": "a1", "artist": "ビートルズ", "name": "リボルバー",
             "songCount": 3, "duration": 600, "musicBrainzId": ""},
            {"id": "a2", "artist": "The Beatles", "name": "Revolver",
             "songCount": 3, "duration": 603, "musicBrainzId": "rel-2"},
        ]
        tracks = {"a1": [{"duration": 200}, {"duration": 200}, {"duration": 200}],
                  "a2": [{"duration": 201}, {"duration": 201}, {"duration": 201}]}
        with patch.object(bot, "nd_get_album_tracks",
                          lambda u, p, aid: tracks.get(aid, [])), \
             patch.object(bot, "_index_album_rgids", lambda: {}), \
             patch.object(bot, "mbz_release_group_of", lambda mbid: "rg-x"):
            groups = bot._duplicate_albums_by_signature(
                albums, set(), deep=True, nd_user="u", nd_pass="p")
        self.assertEqual([[a["id"] for a in g] for g in groups], [["a1", "a2"]])

    def test_cross_language_pass_is_off_without_deep(self):
        albums = [
            {"id": "a1", "artist": "ビートルズ", "name": "リボルバー",
             "songCount": 3, "duration": 600, "musicBrainzId": ""},
            {"id": "a2", "artist": "The Beatles", "name": "Revolver",
             "songCount": 3, "duration": 603, "musicBrainzId": "rel-2"},
        ]
        self.assertEqual(
            bot._duplicate_albums_by_signature(albums, set(), deep=False,
                                               nd_user="u", nd_pass="p"),
            [])

    def test_track_shape_disagreement_rejects_the_pair(self):
        # Same track count and near-identical total, but the runtimes don't line
        # up track for track -- two different albums.
        albums = [
            {"id": "a1", "artist": "One", "name": "Alpha",
             "songCount": 3, "duration": 600, "musicBrainzId": ""},
            {"id": "a2", "artist": "Two", "name": "Beta",
             "songCount": 3, "duration": 600, "musicBrainzId": ""},
        ]
        tracks = {"a1": [{"duration": 100}, {"duration": 200}, {"duration": 300}],
                  "a2": [{"duration": 190}, {"duration": 200}, {"duration": 210}]}
        with patch.object(bot, "nd_get_album_tracks",
                          lambda u, p, aid: tracks.get(aid, [])), \
             patch.object(bot, "_index_album_rgids", lambda: {}):
            groups = bot._duplicate_albums_by_signature(
                albums, set(), deep=True, nd_user="u", nd_pass="p")
        self.assertEqual(groups, [])

    def test_tagged_albums_that_disagree_are_not_rescued_by_durations(self):
        # Both sides are MB-tagged and resolve to *different* release-groups. That
        # is a real answer; falling back to durations would override it.
        albums = [
            {"id": "a1", "artist": "One", "name": "Alpha",
             "songCount": 2, "duration": 400, "musicBrainzId": "rel-1"},
            {"id": "a2", "artist": "Two", "name": "Beta",
             "songCount": 2, "duration": 400, "musicBrainzId": "rel-2"},
        ]
        rgids = {"rel-1": "rg-1", "rel-2": "rg-2"}
        with patch.object(bot, "nd_get_album_tracks",
                          lambda u, p, aid: [{"duration": 200}, {"duration": 200}]), \
             patch.object(bot, "_index_album_rgids", lambda: {}), \
             patch.object(bot, "mbz_release_group_of", lambda mbid: rgids[mbid]):
            groups = bot._duplicate_albums_by_signature(
                albums, set(), deep=True, nd_user="u", nd_pass="p")
        self.assertEqual(groups, [])

    def test_duration_candidate_gate_scales_with_track_count(self):
        # 5s summed over a 12-track album is tighter than two rips of one CD ever
        # agree; per-track rounding alone drifts further than that.
        albums = [
            {"id": "a1", "artist": "ビートルズ", "name": "リボルバー",
             "songCount": 12, "duration": 2400, "musicBrainzId": ""},
            {"id": "a2", "artist": "The Beatles", "name": "Revolver",
             "songCount": 12, "duration": 2412, "musicBrainzId": ""},
        ]
        tracks = {"a1": [{"duration": 200}] * 12, "a2": [{"duration": 201}] * 12}
        with patch.object(bot, "nd_get_album_tracks",
                          lambda u, p, aid: tracks.get(aid, [])), \
             patch.object(bot, "_index_album_rgids", lambda: {}):
            groups = bot._duplicate_albums_by_signature(
                albums, set(), deep=True, nd_user="u", nd_pass="p")
        self.assertEqual([[a["id"] for a in g] for g in groups], [["a1", "a2"]])

    # ── Merge honesty ───────────────────────────────────────────────────────

    def test_retag_preview_names_a_missing_canonical_mbid(self):
        group = {"canonical_album_id": "canon", "canonical_mbid": "",
                 "albums": [{"id": "canon", "tracks": []},
                            {"id": "other", "artist": "A", "name": "B",
                             "tracks": [{"path": "/music/A/B/01.flac"}]}]}
        with patch.object(bot, "MUSIC_LIBRARY_PATH", "/music"):
            preview = bot.preview_group_retag(group)
        self.assertFalse(preview["ok"])
        self.assertTrue(any("MusicBrainz release id" in b for b in preview["blocked"]))

    def test_retag_reports_why_it_refused_instead_of_a_bare_failure(self):
        group = {"canonical_album_id": "canon", "canonical_mbid": "",
                 "albums": [{"id": "canon", "tracks": []},
                            {"id": "other", "artist": "A", "name": "B",
                             "tracks": [{"path": "/music/A/B/01.flac"}]}]}
        with patch.object(bot, "MUSIC_LIBRARY_PATH", "/music"):
            result = bot.apply_group_retag(group)
        self.assertFalse(result["ok"])
        self.assertIn("Cannot merge:", result["output"])
        self.assertIn("MusicBrainz release id", result["output"])

    def test_merge_fails_when_no_tag_could_be_written(self):
        # An MP3 album (or an unwritable one) used to report "tagged 0/12", a
        # success, a Navidrome rescan -- and change nothing.
        files = [{"path": f"/music/A/B/0{i}.mp3", "name": f"0{i}.mp3"} for i in (1, 2)]
        with patch.object(bot, "_canonical_release_fields",
                          lambda mbid: {"album": "B", "albumartist": "A",
                                        "mb_releasegroupid": "rg", "year": "2020"}), \
             patch.object(bot, "_audio_files_in_folder", lambda folder, limit=80: files), \
             patch.object(bot, "_audio_file_tags", lambda path: {}), \
             patch.object(bot, "_mutagen_write_tags", lambda path, tags: False):
            ok, output = bot.beets_merge_album_folders(["/music/A/B"], "rel1")
        self.assertFalse(ok)
        self.assertIn("could not write 2 file(s)", output)

    def test_merge_succeeds_when_every_tag_was_written(self):
        files = [{"path": f"/music/A/B/0{i}.flac", "name": f"0{i}.flac"} for i in (1, 2)]
        with patch.object(bot, "_canonical_release_fields",
                          lambda mbid: {"album": "B", "albumartist": "A",
                                        "mb_releasegroupid": "rg", "year": "2020"}), \
             patch.object(bot, "_audio_files_in_folder", lambda folder, limit=80: files), \
             patch.object(bot, "_audio_file_tags", lambda path: {}), \
             patch.object(bot, "_mutagen_write_tags", lambda path, tags: True):
            ok, output = bot.beets_merge_album_folders(["/music/A/B"], "rel1")
        self.assertTrue(ok)
        self.assertIn("tagged 2/2", output)

    def test_retag_covers_every_folder_of_a_split_copy(self):
        group = {"canonical_album_id": "canon", "canonical_mbid": "rel1",
                 "albums": [
                     {"id": "canon", "tracks": []},
                     {"id": "other", "artist": "A", "name": "B", "tracks": [
                         {"path": "/music/A/B/CD1/01.flac"},
                         {"path": "/music/A/B/CD2/01.flac"}]}]}
        seen = []
        with patch.object(bot, "MUSIC_LIBRARY_PATH", "/music"), \
             patch.object(bot, "beets_merge_album_folders",
                          lambda folders, mbid, albums=None: (seen.extend(folders), (True, ""))[1]):
            bot.apply_group_retag(group)
        self.assertEqual(sorted(os.path.basename(f) for f in seen), ["CD1", "CD2"])

    # ── Count invalidation after a fill ─────────────────────────────────────

    @patch("listenbrainz_bot.mbz_release_tracks")
    def test_group_counts_refresh_from_navidrome_without_a_full_scan(self, mock_tracks):
        mock_tracks.return_value = [
            {"title": "One", "mbid": "r1", "position": 1},
            {"title": "Two", "mbid": "r2", "position": 2},
        ]
        # Stored snapshot: only "One" was in the library when the group was built.
        group = {
            "id": "g1", "artist": "Artist", "album": "Album",
            "canonical_album_id": "a1", "canonical_mbid": "rel1",
            "albums": [{"id": "a1", "name": "Album", "artist": "Artist",
                        "musicBrainzId": "rel1", "songCount": 1,
                        "tracks": [{"title": "One", "musicBrainzId": "r1",
                                    "path": "/music/A/B/01.flac"}]}],
            "missing_tracks": [{"title": "Two", "mbid": "r2", "decision": "placed"}],
            "present": 1, "total": 2,
        }
        # Navidrome now has both -- the fill landed.
        live_tracks = [
            {"id": "s1", "title": "One", "musicBrainzId": "r1", "track": 1,
             "path": "/music/A/B/01.flac", "suffix": "flac"},
            {"id": "s2", "title": "Two", "musicBrainzId": "r2", "track": 2,
             "path": "/music/A/B/02.flac", "suffix": "flac"},
        ]
        with patch.object(bot, "_default_web_user",
                          lambda: {"navidrome_user": "u", "navidrome_password": "p"}), \
             patch.object(bot, "_nd_album_index",
                          lambda force=False: [{"id": "a1", "name": "Album",
                                                "artist": "Artist",
                                                "musicBrainzId": "rel1",
                                                "songCount": 2, "duration": 400}]), \
             patch.object(bot, "nd_get_album_tracks", lambda u, p, aid: live_tracks), \
             patch.object(bot, "_find_review_group", lambda gid: group):
            self.assertTrue(bot.refresh_group_albums_from_navidrome("g1"))
        self.assertEqual(group["present"], 2)
        self.assertEqual(group["total"], 2)
        self.assertEqual(group["missing_tracks"], [])

    @patch("listenbrainz_bot.mbz_release_tracks")
    def test_group_count_refresh_keeps_the_old_snapshot_when_navidrome_fails(self, mock_tracks):
        mock_tracks.return_value = [{"title": "One", "mbid": "r1", "position": 1}]
        group = {
            "id": "g1", "canonical_album_id": "a1", "canonical_mbid": "rel1",
            "albums": [{"id": "a1", "tracks": [{"title": "One", "musicBrainzId": "r1"}]}],
            "missing_tracks": [], "present": 1, "total": 1,
        }

        def boom(*a, **k):
            raise RuntimeError("Navidrome timed out")

        with patch.object(bot, "_default_web_user",
                          lambda: {"navidrome_user": "u", "navidrome_password": "p"}), \
             patch.object(bot, "_nd_album_index", lambda force=False: []), \
             patch.object(bot, "nd_get_album_tracks", boom), \
             patch.object(bot, "_find_review_group", lambda gid: group):
            self.assertFalse(bot.refresh_group_albums_from_navidrome("g1"))
        # A partial refresh would under-report present and reopen filled gaps.
        self.assertEqual(group["present"], 1)

    # ── Source file list ────────────────────────────────────────────────────

    def test_album_source_coverage_pairs_against_the_tracklist(self):
        # The artist/album page had no pairing at all: coverage was
        # min(fileCount, total)/total, so a folder of unrelated files with the
        # right count read as a complete match.
        tracklist = [{"title": "Alpha", "position": 1, "artist": "Artist"},
                     {"title": "Beta", "position": 2, "artist": "Artist"}]
        right = {"files": [{"filename": "01 - Alpha.flac", "size": 1},
                           {"filename": "02 - Beta.flac", "size": 1}]}
        wrong = {"files": [{"filename": "01 - Unrelated.flac", "size": 1},
                           {"filename": "02 - Nothing.flac", "size": 1}]}
        good = bot._source_coverage_summary(right, None, tracklist)
        bad = bot._source_coverage_summary(wrong, None, tracklist)
        self.assertEqual((good["matched"], good["total"]), (2, 2))
        self.assertEqual((bad["matched"], bad["total"]), (0, 2))
        self.assertEqual([t["title"] for t in bad["unmatched_tracks"]],
                         ["Alpha", "Beta"])

    def test_source_files_view_tags_each_file_with_its_slot(self):
        fd = {"files": [{"filename": "path/01 - Alpha.flac", "size": 10 * 1024 * 1024},
                        {"filename": "path/02 - Beta.flac", "size": 1},
                        {"filename": "path/cover.jpg", "size": 1},
                        {"filename": "path/99 - Bonus.flac", "size": 1}]}
        tracklist = [{"title": "Alpha", "position": 1}, {"title": "Beta", "position": 2}]
        coverage = bot._source_coverage_summary(fd, None, tracklist)
        rows = bot._source_files_view(fd, coverage)
        by_name = {r["filename"]: r for r in rows}
        # Basename only, and the matched rows name the slot they fill — plus
        # how the pairing was reached, so the picker can distinguish a title
        # that agreed from a duration that merely didn't disagree.
        self.assertEqual(by_name["01 - Alpha.flac"]["matchedTo"],
                         {"position": 1, "title": "Alpha", "basis": "exact"})
        self.assertEqual(by_name["01 - Alpha.flac"]["sizeMb"], 10.0)
        self.assertEqual(by_name["02 - Beta.flac"]["matchedTo"]["title"], "Beta")
        # Extras are surfaced as extras, not silently dropped.
        self.assertIsNone(by_name["99 - Bonus.flac"]["matchedTo"])
        self.assertFalse(by_name["cover.jpg"]["accepted"])

    def test_source_files_view_truncates_a_huge_folder(self):
        fd = {"files": [{"filename": f"{i:03d} - Track.flac", "size": 1}
                        for i in range(200)]}
        summary = bot._source_summary(fd, 0, tracks=[])
        self.assertEqual(len(summary["files"]), bot._SOURCE_FILE_LIMIT)
        self.assertTrue(summary["files_truncated"])
        view = bot._source_view(summary)
        self.assertTrue(view["filesTruncated"])
        self.assertEqual(len(view["files"]), bot._SOURCE_FILE_LIMIT)

    def test_source_view_carries_the_unmatched_slots(self):
        fd = {"files": [{"filename": "01 - Alpha.flac", "size": 1}]}
        tracklist = [{"title": "Alpha", "position": 1}, {"title": "Beta", "position": 2}]
        view = bot._source_view(bot._source_summary(fd, 0, tracks=tracklist))
        self.assertEqual(view["missingTracks"], [{"position": 2, "title": "Beta"}])

    def test_placement_imports_every_directory_a_failover_touched(self):
        # A file that failed over to another peer lands in that peer's own
        # download dir; importing only the majority dir stranded it there while
        # its track reported as still missing.
        import shutil as _shutil
        tmp = tempfile.mkdtemp()
        self.addCleanup(_shutil.rmtree, tmp, True)
        lib = os.path.join(tmp, "music")
        os.makedirs(os.path.join(lib, "Artist", "Album"))
        dirs = []
        for peer, names in (("peerA", ["01 - One.flac", "02 - Two.flac"]),
                            ("peerB", ["03 - Three.flac"])):
            d = os.path.join(tmp, "downloads", peer)
            os.makedirs(d)
            for name in names:
                open(os.path.join(d, name), "wb").write(b"\0" * 16)
            dirs.append(d)
        with patch.object(bot, "MUSIC_LIBRARY_PATH", lib), \
             patch.object(bot, "mbz_release_tracks", lambda *a, **k: [
                 {"title": "One", "mbid": "r1", "position": 1},
                 {"title": "Two", "mbid": "r2", "position": 2},
                 {"title": "Three", "mbid": "r3", "position": 3}]), \
             patch.object(bot, "mbz_release_display", lambda *a, **k: {}), \
             patch.object(bot, "rgid_from_release", lambda *a, **k: ""), \
             patch.object(bot, "_default_web_user", lambda *a, **k: None), \
             patch.object(bot, "_audio_signature", lambda path: {}):
            result = bot._deterministic_album_import(dirs, "rel1", "Artist", "Album")
        self.assertEqual(result["moved"], 3)
        self.assertEqual(
            {r["title"] for r in result["per_file"] if r["status"] == "matched"},
            {"One", "Two", "Three"})


# ── Download reliability: query building, ranking, matching ──────────────────
# Downloads failed roughly half the time on first try and had to be rescued by
# hand. These pin the three fixes: queries that survive canonical MusicBrainz
# titles, ranking that knows which album it asked for, and a matcher that pairs
# files it can actually identify — without reopening the mis-pairing the old
# strictness was built to prevent.

class QueryBuilderTests(unittest.TestCase):
    def test_self_titled_album_does_not_repeat_the_artist(self):
        """"Led Zeppelin Led Zeppelin" asks slskd for the whole discography."""
        queries = bot._album_search_queries(
            "Led Zeppelin", "Led Zeppelin", "1969", ["Good Times Bad Times"])
        self.assertNotIn("led zeppelin led zeppelin",
                         [q.lower() for q in queries])
        # The year is the one term that separates the debut from the rest.
        self.assertEqual(queries[0], "Led Zeppelin 1969")

    def test_self_titled_falls_back_to_a_distinctive_track(self):
        """The album name alone is the artist name, so it buys nothing."""
        queries = bot._album_search_queries(
            "Weezer", "Weezer", "", ["Buddy Holly", "Undone - The Sweater Song"])
        self.assertTrue(any("Sweater" in q for q in queries), queries)

    def test_edition_packaging_is_stripped(self):
        queries = bot._album_search_queries("Radiohead", "Kid A (2014 Remaster)", "2000", [])
        self.assertEqual(queries[0], "Radiohead Kid A")

    def test_wordy_title_gets_a_distinctive_words_variant(self):
        queries = bot._album_search_queries(
            "Pink Floyd", "The Dark Side of the Moon", "1973", [])
        self.assertIn("Pink Floyd dark side moon", queries)

    def test_never_more_than_the_pass_cap(self):
        queries = bot._album_search_queries(
            "Artist", "A Very Long Album Title Indeed", "1999", ["T1", "T2"])
        self.assertLessEqual(len(queries), bot.MAX_SEARCH_PASSES)

    def test_self_titled_discography_query_is_the_last_resort(self):
        """Bare "Led Zeppelin" asks for the whole discography; with a year and
        a tracklist in hand it must run after the queries that can only match
        the debut, not before them."""
        queries = bot._album_search_queries(
            "Led Zeppelin", "Led Zeppelin", "1969", ["Good Times Bad Times"])
        self.assertEqual(queries[-1], "Led Zeppelin")
        self.assertIn("Good Times Bad Times", queries[1])

    def test_a_title_that_is_only_noise_survives_cleaning(self):
        """"Remastered" is a real album name; stripping it to "" would query
        for everything."""
        self.assertEqual(bot._clean_album_title("Remastered"), "Remastered")


class FolderRankingTests(unittest.TestCase):
    def _folder(self, path, speed, files=9):
        return {"username": "u", "folder": path, "upload_speed": speed,
                "raw_file_count": files, "locked_in_folder": 0, "queue_length": 0,
                "has_free_upload_slot": True,
                "files": [{"filename": f"{path}/{i:02d} - Track.flac",
                           "size": 30 * 1024 * 1024, "bitRate": 900}
                          for i in range(files)]}

    def test_the_album_asked_for_outranks_a_faster_unrelated_one(self):
        """The Led Zeppelin case: for a self-titled album the whole discography
        comes back, and ranking on peer metrics alone sorted it by upload
        speed."""
        want = self._folder("Music/Led Zeppelin/Led Zeppelin (1969)", 200_000)
        other = self._folder("Music/Led Zeppelin/Physical Graffiti", 5_000_000, 15)
        for fd in (want, other):
            bot._annotate_folder_match(fd, "Led Zeppelin", "Led Zeppelin", "1969")
            fd["score"] = bot._score_folder(fd, 9)
        self.assertTrue(want["album_match_ok"])
        self.assertFalse(other["album_match_ok"])
        self.assertGreater(want["score"], other["score"])

    def test_artist_folder_alone_is_not_an_album_match(self):
        """`partial_ratio` scores any superstring at 100, so "Led Zeppelin"
        matched "Led Zeppelin/Physical Graffiti" perfectly and every album in
        the discography tied with the one being searched for."""
        fd = self._folder("Music/Led Zeppelin/Physical Graffiti", 1)
        bot._annotate_folder_match(fd, "Led Zeppelin", "Led Zeppelin", "")
        self.assertLess(fd["album_match"], bot.ALBUM_MATCH_THRESHOLD)

    def test_peer_naming_conventions_still_match(self):
        """"Artist - Album [1969 FLAC]" is how a large share of peers file
        things; the format tag and the repeated artist must not sink it."""
        fd = self._folder("Shared/Led Zeppelin - Led Zeppelin [1969 FLAC]", 1)
        bot._annotate_folder_match(fd, "Led Zeppelin", "Led Zeppelin", "1969")
        self.assertTrue(fd["album_match_ok"])
        self.assertTrue(fd["year_in_path"])

    def test_a_short_title_is_not_someone_elses_album(self):
        """The Zone case: `partial_ratio` with the old extra<=1 allowance gave
        any "X Zone" folder a perfect score, so a wrong-artist copy tied with
        the record actually asked for."""
        self.assertLess(bot._folder_name_score("Zone", "Culture Zone"),
                        bot.ALBUM_MATCH_THRESHOLD)

    def test_a_short_title_still_matches_its_own_artist_prefix(self):
        """"Future - Zone" is the right folder; the artist run is not an extra
        word once the caller says who the artist is."""
        self.assertGreaterEqual(
            bot._folder_name_score("Zone", "Future Zone", artist="Future"),
            bot.ALBUM_MATCH_THRESHOLD)

    def test_a_terse_right_folder_is_not_sunk_by_year_and_format(self):
        """Pinned regression: "Zone [2016] FLAC" scored 44 while a wrong-artist
        "Culture Zone" scored 100 — the wrong folder outranked the right one
        twice over."""
        self.assertGreaterEqual(
            bot._folder_name_score("Zone", "Zone [2016] FLAC"),
            bot.ALBUM_MATCH_THRESHOLD)

    def test_self_titled_sequels_do_not_match_the_debut(self):
        """token_sort alone puts "Led Zeppelin IV" at ~89 against the debut, so
        II/III/IV all counted as the album asked for."""
        for cand in ("Led Zeppelin IV", "Led Zeppelin II", "Led Zeppelin III",
                     "Led Zeppelin 2"):
            self.assertLess(
                bot._folder_name_score("Led Zeppelin", cand, artist="Led Zeppelin"),
                bot.ALBUM_MATCH_THRESHOLD, cand)
        # Accepted limitation, documented: a peer's "Led Zeppelin I" is capped
        # too (a numeral is a numeral). The debut still ranks via the artist
        # tier, track corroboration and the year query.
        self.assertLess(
            bot._folder_name_score("Led Zeppelin", "Led Zeppelin I", artist="Led Zeppelin"),
            bot.ALBUM_MATCH_THRESHOLD)

    def test_a_numeric_title_needs_more_than_a_year_in_the_folder(self):
        """Review regression: with every candidate token ignorable, the empty
        kept-list fell back to the raw candidate and partial_ratio scored any
        digits at 100 — "1" matched "The Beatles 1962-1966"."""
        self.assertLess(bot._folder_name_score("1", "The Beatles 1962-1966",
                                               artist="The Beatles"),
                        bot.ALBUM_MATCH_THRESHOLD)
        self.assertLess(bot._folder_name_score("21", "Adele [2021]",
                                               artist="Adele"),
                        bot.ALBUM_MATCH_THRESHOLD)
        # ...while a title that IS a year still matches itself.
        self.assertGreaterEqual(
            bot._folder_name_score("1989", "Taylor Swift 1989",
                                   artist="Taylor Swift"),
            bot.ALBUM_MATCH_THRESHOLD)

    def test_spaced_disc_markers_are_packaging_not_siblings(self):
        """"CD 1" / "Disc 2" written with a space must read like the joined
        "cd1" the noise list already covers."""
        self.assertGreaterEqual(bot._folder_name_score(
            "Mellon Collie and the Infinite Sadness",
            "Mellon Collie and the Infinite Sadness CD 1"),
            bot.ALBUM_MATCH_THRESHOLD)
        self.assertGreaterEqual(bot._folder_name_score(
            "Physical Graffiti", "Physical Graffiti Disc 2"),
            bot.ALBUM_MATCH_THRESHOLD)

    def test_the_sibling_guard_is_symmetric(self):
        """Searching FOR the sequel must not match the debut, and a numbered
        series must not collapse onto the quality-number exemption."""
        self.assertLess(bot._folder_name_score(
            "Led Zeppelin II", "Led Zeppelin (1969)", artist="Led Zeppelin"),
            bot.ALBUM_MATCH_THRESHOLD)
        self.assertLess(bot._folder_name_score(
            "Chicago 17", "Chicago - Chicago 16", artist="Chicago"),
            bot.ALBUM_MATCH_THRESHOLD)
        self.assertLess(bot._folder_name_score("NOW 47", "NOW 48"),
                        bot.ALBUM_MATCH_THRESHOLD)
        # The right numbered entry still matches, year and all.
        self.assertGreaterEqual(bot._folder_name_score(
            "Chicago 17", "Chicago - Chicago 17 [1984]", artist="Chicago"),
            bot.ALBUM_MATCH_THRESHOLD)

    def test_quality_preference_changes_the_preferred_copy(self):
        hires = {"codec": "flac", "bitrate": 2500, "bit_depth": 24, "sample_rate": 96000}
        cd = {"codec": "flac", "bitrate": 900, "bit_depth": 16, "sample_rate": 44100}
        q = bot._quality_preference_score
        self.assertGreater(q(cd, "flac-16-44"), q(hires, "flac-16-44"))
        self.assertGreater(q(hires, "highest-bitrate"), q(cd, "highest-bitrate"))
        opus = {"codec": "opus", "bitrate": 256, "bit_depth": 0, "sample_rate": 48000}
        self.assertGreater(q(opus, "prefer-opus"), q(cd, "prefer-opus"))


class ArtistEvidenceTests(unittest.TestCase):
    """A folder is only auto-downloadable when something ties it to the artist
    asked for — the folder path, the filenames, or the tracklist itself."""

    @staticmethod
    def _files(*names):
        return [{"filename": n} for n in names]

    def test_track_corroboration_counts_each_title_once(self):
        files = self._files("01 - Buddy Holly.flac", "02 - Undone.flac",
                            "03 - Buddy Holly (reprise).flac")
        n = bot._folder_track_matches(
            files, ["Buddy Holly", "Undone", "My Name Is Jonas"])
        self.assertEqual(n, 2)

    def test_track_corroboration_ignores_unrelated_files(self):
        files = self._files("01 - Draco.flac", "02 - Zoom.flac",
                            "03 - Xanny Family.flac")
        self.assertEqual(bot._folder_track_matches(
            files, ["Completely different", "Titles here"]), 0)

    @staticmethod
    def _fd(folder, *filenames):
        return {"folder": folder,
                "files": [{"filename": n} for n in filenames]}

    def test_artist_high_in_the_path_is_evidence(self):
        """Peers file as Music/FLAC/Artist/Album/CD1 — the artist sits above
        the two components the album comparison looks at."""
        fd = self._fd("Music/FLAC/Future/Zone/CD1", "01 - Track.flac")
        bot._annotate_folder_match(fd, "Zone", "Future")
        self.assertTrue(fd["artist_verified"])
        self.assertEqual(fd["artist_evidence"], "path")

    def test_artist_in_filenames_is_evidence(self):
        fd = self._fd("Shared/Zone", "01 - Future - Draco.flac")
        bot._annotate_folder_match(fd, "Zone", "Future")
        self.assertTrue(fd["artist_verified"])
        self.assertEqual(fd["artist_evidence"], "files")

    def test_tracklist_corroboration_is_evidence(self):
        """A terse folder whose filenames line up with the canonical tracklist
        is identified by its contents."""
        fd = self._fd("Shared/Zone [2016]", "01 - Draco.flac",
                      "02 - Xanny Family.flac", "03 - Zoom.flac")
        bot._annotate_folder_match(
            fd, "Zone", "Future",
            track_titles=["Draco", "Xanny Family", "Zoom", "Lil Haiti Baby"])
        self.assertTrue(fd["artist_verified"])
        self.assertEqual(fd["artist_evidence"], "tracks")
        self.assertEqual(fd["track_matches"], 3)

    def test_one_coincidental_title_is_not_evidence(self):
        fd = self._fd("Shared/Culture Zone", "01 - Intro.flac")
        bot._annotate_folder_match(
            fd, "Zone", "Future",
            track_titles=["Intro", "Draco", "Zoom", "Xanny Family",
                          "Lil Haiti Baby", "Used to This"])
        self.assertFalse(fd["artist_verified"])

    def test_a_wrong_artist_folder_is_unverified(self):
        fd = self._fd("Shared/Culture Zone", "01 - Something Else.flac")
        bot._annotate_folder_match(fd, "Zone", "Future",
                                   track_titles=["Draco", "Zoom"])
        self.assertFalse(fd["artist_verified"])
        self.assertEqual(fd["artist_evidence"], "")

    def test_various_artists_and_no_artist_are_never_gated(self):
        """The gate must not brick VA or unknown-artist downloads."""
        fd = self._fd("Shared/Now Thats What I Call Music 100", "x.flac")
        bot._annotate_folder_match(fd, "Now 100", "Various Artists")
        self.assertTrue(fd["artist_verified"])
        fd2 = self._fd("Shared/Whatever", "x.flac")
        bot._annotate_folder_match(fd2, "Whatever", "")
        self.assertTrue(fd2["artist_verified"])

    def test_a_collaboration_credit_matches_on_one_complete_sub_artist(self):
        """"Future & Juice WRLD" filed under "Juice WRLD" is still the right
        artist — peers rarely write out a full collaboration credit."""
        fd = self._fd("Music/Juice WRLD/WRLD ON DRUGS", "01 - Fine China.flac")
        bot._annotate_folder_match(fd, "WRLD ON DRUGS", "Future & Juice WRLD")
        self.assertTrue(fd["artist_verified"])
        self.assertEqual(fd["artist_evidence"], "path")

    def test_a_shared_word_is_not_the_artist(self):
        """Review finding: majority-of-tokens let "Music/Pink/Funhouse" verify
        Pink Floyd and a genre folder verify Daft Punk. A sub-artist has to
        appear whole."""
        fd = self._fd("Music/Pink/Funhouse", "01 - Track.flac")
        bot._annotate_folder_match(fd, "Funhouse", "Pink Floyd")
        self.assertFalse(fd["artist_verified"])
        fd2 = self._fd("Music/Punk/Ramones/Discovery", "01 - Track.flac")
        bot._annotate_folder_match(fd2, "Discovery", "Daft Punk")
        self.assertFalse(fd2["artist_verified"])

    def test_a_song_title_word_is_not_filename_evidence(self):
        """"The Future Is Now.flac" inside a wrong-artist folder must not
        verify Future — only the artist segment of "Artist - Title" names."""
        fd = self._fd("Shared/Culture Zone", "01 - The Future Is Now.flac")
        bot._annotate_folder_match(fd, "Zone", "Future")
        self.assertFalse(fd["artist_verified"])

    def test_a_non_latin_artist_verifies_by_path(self):
        """_significant_words strips to ASCII, so Кино produced no tokens and
        could never be path-verified — every unattended download refused it."""
        fd = self._fd("Music/Кино/Группа крови", "01 - Track.flac")
        bot._annotate_folder_match(fd, "Группа крови", "Кино")
        self.assertTrue(fd["artist_verified"])
        self.assertEqual(fd["artist_evidence"], "path")

    def test_a_stopword_only_artist_still_verifies_its_own_folder(self):
        fd = self._fd("Music/The The/Soul Mining", "01 - Track.flac")
        bot._annotate_folder_match(fd, "Soul Mining", "The The")
        self.assertTrue(fd["artist_verified"])
        fd2 = self._fd("Music/The Deluxe Edition/Soul Mining", "01 - Track.flac")
        bot._annotate_folder_match(fd2, "Soul Mining", "The The")
        self.assertFalse(fd2["artist_verified"])

    def test_a_whitespace_artist_is_degenerate_not_unverifiable(self):
        fd = self._fd("Shared/Whatever", "x.flac")
        bot._annotate_folder_match(fd, "Whatever", "   ")
        self.assertTrue(fd["artist_verified"])


class AlbumSearchPassTests(unittest.TestCase):
    """The multi-pass album search: the release context actually reaches the
    per-pass search, each folder remembers which pass found it, and a wider
    pass's flood never outranks an artist-verified result."""

    def test_year_and_track_titles_reach_every_pass(self):
        """The year bonus was dead on every album path: slskd_run_search took
        `year` and slskd_search_album_folders never forwarded it."""
        calls = []

        def fake(query, expected, progress=None, stats=None, album="",
                 artist="", year="", track_titles=None):
            calls.append({"year": year, "track_titles": track_titles})
            return []

        with patch.object(bot, "slskd_run_search", side_effect=fake):
            bot.slskd_search_album_folders(
                "Future", "Zone", 12, year="2016", track_titles=["Draco"])
        self.assertTrue(calls)
        for c in calls:
            self.assertEqual(c["year"], "2016")
            self.assertEqual(c["track_titles"], ["Draco"])

    def test_each_folder_remembers_the_first_pass_that_found_it(self):
        seq = [
            [{"username": "u", "folder": "A", "score": 10}],
            [{"username": "u", "folder": "A", "score": 50},
             {"username": "u", "folder": "B", "score": 40}],
            [],
        ]

        def fake(query, expected, **kw):
            return seq.pop(0) if seq else []

        with patch.object(bot, "slskd_run_search", side_effect=fake):
            folders = bot.slskd_search_album_folders("Artist", "Album", 10)
        by_folder = {fd["folder"]: fd for fd in folders}
        # A was found first in pass 1; the rescored pass-2 copy wins on score
        # but keeps the earlier provenance.
        self.assertEqual(by_folder["A"]["search_pass"], 1)
        self.assertEqual(by_folder["A"]["score"], 50)
        self.assertEqual(by_folder["B"]["search_pass"], 2)

    def test_verified_results_outrank_higher_scored_unverified(self):
        """The Zone failure: a fast wrong-artist folder from the album-only
        pass beat the real album on peer metrics."""
        def fake(query, expected, **kw):
            return [
                {"username": "fast", "folder": "Culture Zone", "score": 9000,
                 "artist_verified": False},
                {"username": "slow", "folder": "Future/Zone", "score": 3000,
                 "artist_verified": True},
            ] if not fake.done else []

        fake.done = False
        with patch.object(bot, "slskd_run_search", side_effect=fake):
            folders = bot.slskd_search_album_folders("Future", "Zone", 12)
        self.assertEqual(folders[0]["folder"], "Future/Zone")

    def test_folders_without_the_flag_still_sort_by_score(self):
        """Legacy dicts (tests, the single-track path) carry no
        artist_verified; absent must mean verified, not demoted."""
        def fake(query, expected, **kw):
            return [{"username": "u", "folder": "X", "score": 100},
                    {"username": "u", "folder": "Y", "score": 200,
                     "artist_verified": False}]

        with patch.object(bot, "slskd_run_search", side_effect=fake):
            folders = bot.slskd_search_album_folders("A", "B", 5)
        self.assertEqual(folders[0]["folder"], "X")

    def test_early_stop_needs_verified_matches(self):
        """Two wrong-artist "Zone" folders used to end the walk before the
        narrower artist+track query ever ran."""
        calls = {"n": 0}

        def fake(query, expected, **kw):
            calls["n"] += 1
            return [{"username": "u", "folder": f"F{calls['n']}a", "score": 10,
                     "album_match_ok": True, "artist_verified": False},
                    {"username": "u", "folder": f"F{calls['n']}b", "score": 9,
                     "album_match_ok": True, "artist_verified": False}]

        with patch.object(bot, "slskd_run_search", side_effect=fake):
            bot.slskd_search_album_folders("Future", "Zone", 12, year="2016",
                                           track_titles=["Draco"])
        self.assertGreater(calls["n"], 1)

    def test_early_stop_still_fires_on_verified_matches(self):
        calls = {"n": 0}

        def fake(query, expected, **kw):
            calls["n"] += 1
            return [{"username": "u", "folder": "Fa", "score": 10,
                     "album_match_ok": True, "artist_verified": True},
                    {"username": "u", "folder": "Fb", "score": 9,
                     "album_match_ok": True, "artist_verified": True}]

        with patch.object(bot, "slskd_run_search", side_effect=fake):
            bot.slskd_search_album_folders("Future", "Zone", 12, year="2016",
                                           track_titles=["Draco"])
        self.assertEqual(calls["n"], 1)

    def test_auto_download_refuses_an_unverified_top_folder(self):
        """Decision A: unattended paths never guess. Verified folders sort
        first, so an unverified folders[0] means no verified candidate exists."""
        self.assertIsNone(bot._auto_download_choice(
            [{"folder": "Culture Zone", "artist_verified": False}]))
        self.assertIsNone(bot._auto_download_choice([]))

    def test_auto_download_takes_a_verified_or_legacy_top_folder(self):
        fd = {"folder": "Future/Zone", "artist_verified": True}
        self.assertIs(bot._auto_download_choice([fd]), fd)
        legacy = {"folder": "X"}
        self.assertIs(bot._auto_download_choice([legacy]), legacy)

    def test_search_with_context_resolves_year_and_titles(self):
        captured = {}

        def fake(query, expected, progress=None, stats=None, album="",
                 artist="", year="", track_titles=None):
            captured.setdefault("year", year)
            captured.setdefault("track_titles", track_titles)
            return []

        with patch.object(bot, "_album_search_context",
                          return_value=("2016", ["Draco", "Zoom"])), \
             patch.object(bot, "slskd_run_search", side_effect=fake):
            bot._search_album_with_context("Future", "Zone", 12,
                                           release_mbid="r-1")
        self.assertEqual(captured["year"], "2016")
        self.assertEqual(captured["track_titles"], ["Draco", "Zoom"])

    def test_api_album_download_refuses_unverified_only_results(self):
        """Decision A on the API path: report no_source (wishlist-compatible)
        instead of downloading a guess."""
        fd = {"username": "fast", "folder": "Culture Zone", "score": 9000,
              "artist_verified": False, "files": [{"filename": "x.flac"}]}
        fails = []
        with patch.object(bot, "slskd_search_album_folders",
                          return_value=[fd]), \
             patch.object(bot, "_album_search_context",
                          return_value=("", [])), \
             patch.object(bot, "_album_fill_fail",
                          side_effect=lambda *a, **k: fails.append((a, k))), \
             patch.object(bot, "_task_finish"), \
             patch.object(bot, "slskd_expand_directory") as expand, \
             patch.object(bot, "slskd_enqueue_folder") as enq:
            bot._album_download_search_and_enqueue(
                "t1", "rmbid-1", "Future", "Zone", 12, None, "")
        enq.assert_not_called()
        expand.assert_not_called()
        self.assertTrue(fails)
        self.assertEqual(fails[0][0][1], "no_source")

    def test_a_manual_pick_bypasses_the_api_gate(self):
        """`chosen` is the user's explicit verdict on a listed source."""
        fd = {"username": "fast", "folder": "Culture Zone", "score": 9000,
              "artist_verified": False, "files": [{"filename": "x.flac"}]}
        with patch.object(bot, "slskd_search_album_folders",
                          return_value=[fd]), \
             patch.object(bot, "_album_search_context",
                          return_value=("", [])), \
             patch.object(bot, "_album_fill_fail") as fail, \
             patch.object(bot, "_album_fill_set", return_value=True), \
             patch.object(bot, "_task_finish"), \
             patch.object(bot, "_default_web_user", return_value={}), \
             patch.object(bot, "slskd_expand_directory", return_value=[]), \
             patch.object(bot, "slskd_enqueue_folder",
                          return_value=(1, 1, "ag1")) as enq:
            bot._album_download_search_and_enqueue(
                "t1", "rmbid-1", "Future", "Zone", 12,
                {"username": "fast", "folder": "Culture Zone"}, "")
        enq.assert_called()
        fail.assert_not_called()

    def test_merge_keeps_evidence_from_the_pass_that_had_it(self):
        """A later pass returns a different hit-file subset for the same
        folder; its higher-scored copy must not drop the earlier copy's
        verification and fall a tier."""
        seq = [
            [{"username": "u", "folder": "A", "score": 10,
              "artist_verified": True, "artist_evidence": "files",
              "track_matches": 3}],
            [{"username": "u", "folder": "A", "score": 50,
              "artist_verified": False, "artist_evidence": "",
              "track_matches": 0}],
            [],
        ]

        def fake(query, expected, **kw):
            return seq.pop(0) if seq else []

        with patch.object(bot, "slskd_run_search", side_effect=fake):
            folders = bot.slskd_search_album_folders("Artist", "Album", 10)
        self.assertEqual(folders[0]["score"], 50)
        self.assertTrue(folders[0]["artist_verified"])
        self.assertEqual(folders[0]["artist_evidence"], "files")

    def test_context_titles_beat_partial_caller_titles(self):
        """The canonical tracklist is the superset; a playlist group's one or
        two missing titles must not replace it (they can never reach the
        track-evidence floor)."""
        captured = {}

        def fake(query, expected, progress=None, stats=None, album="",
                 artist="", year="", track_titles=None):
            captured["titles"] = track_titles
            return []

        with patch.object(bot, "_album_search_context",
                          return_value=("2016", ["A", "B", "C"])), \
             patch.object(bot, "slskd_run_search", side_effect=fake):
            bot._search_album_with_context("X", "Y", 3, release_mbid="r",
                                           track_titles=["B"])
        self.assertEqual(captured["titles"], ["A", "B", "C"])
        with patch.object(bot, "_album_search_context",
                          return_value=("", [])), \
             patch.object(bot, "slskd_run_search", side_effect=fake):
            bot._search_album_with_context("X", "Y", 3, release_mbid="",
                                           track_titles=["B"])
        self.assertEqual(captured["titles"], ["B"])

    def test_track_evidence_is_bounded_to_the_top_folders(self):
        """Review finding: corroboration ran on every folder that failed the
        cheap checks — exactly the wrong-artist flood — for seconds per pass.
        It only matters where it could change a pick."""
        folders = [{"username": f"u{i}", "folder": f"F{i}", "score": 100 - i,
                    "artist_verified": False, "artist_evidence": "",
                    "track_matches": 0,
                    "files": [{"filename": "01 - Draco.flac"},
                              {"filename": "02 - Zoom.flac"}]}
                   for i in range(bot._TRACK_EVIDENCE_FOLDER_CAP + 20)]
        calls = []
        real = bot._folder_track_matches
        with patch.object(bot, "_folder_track_matches",
                          side_effect=lambda f, t: calls.append(1) or real(f, t)):
            bot._apply_track_evidence_pass(folders, ["Draco", "Zoom"])
        self.assertEqual(len(calls), bot._TRACK_EVIDENCE_FOLDER_CAP)
        self.assertTrue(folders[0]["artist_verified"])
        self.assertEqual(folders[0]["artist_evidence"], "tracks")

    def test_gap_detail_does_not_recommend_an_unverified_rank_one(self):
        fd = {"username": "u", "folder": "F", "files": [],
              "artist_verified": False}
        group = {"id": "g1", "artist": "Future", "album": "Zone",
                 "missing_tracks": [],
                 "source_results": {"mode": "album", "query": "q",
                                    "created_at": time.time(),
                                    "folders": [fd],
                                    "summaries": [bot._source_summary(fd, 0)]}}
        view = bot._gap_detail_view(group)
        self.assertFalse(view["sources"][0]["recommended"])

    def test_source_switch_skips_unverified_alternates(self):
        """Review Critical: the stall failover popped alt_sources ungated, so
        a parked transfer handed the album to a wrong-artist folder with no
        user involved."""
        ag_id = "agtest1"
        ag = {"alt_sources": [{"username": "bad", "folder": "Culture Zone",
                               "artist_verified": False,
                               "files": [{"filename": "x.flac"}]}],
              "chat_id": "c", "token": "t", "label": "Future - Zone",
              "artist": "Future", "album": "Zone", "release_mbid": "",
              "retry_query": "", "completed": 0, "failed": 0}
        bot.pending_album_groups[ag_id] = ag
        try:
            from unittest.mock import MagicMock
            with patch.object(bot, "_tg_send", new=AsyncMock()), \
                 patch.object(bot, "InlineKeyboardButton", MagicMock()), \
                 patch.object(bot, "InlineKeyboardMarkup", MagicMock()), \
                 patch.object(bot, "slskd_expand_directory") as expand:
                asyncio.run(bot._switch_album_source_inner(None, ag_id, ag))
            expand.assert_not_called()
            self.assertNotIn(ag_id, bot.pending_album_groups)
        finally:
            bot.pending_album_groups.pop(ag_id, None)

    def test_source_views_carry_artist_evidence(self):
        """Additive wire fields; absent flags read as verified so an old
        folder dict renders as before."""
        fd = {"username": "u", "folder": "F", "files": [],
              "artist_verified": False, "artist_evidence": "",
              "search_pass": 3, "track_matches": 0}
        s = bot._source_summary(fd, 0)
        self.assertIs(s["artist_verified"], False)
        self.assertEqual(s["search_pass"], 3)
        v = bot._source_view(s)
        self.assertIs(v["artistVerified"], False)
        self.assertEqual(v["artistEvidence"], "")
        self.assertEqual(v["searchPass"], 3)
        legacy = bot._source_summary({"username": "u", "folder": "F",
                                      "files": []}, 0)
        self.assertIs(legacy["artist_verified"], True)

    def test_debug_album_search_view_reports_rank_inputs(self):
        """Live verification is blind without a view that shows why each
        folder ranked where it did."""
        fd = {"username": "u", "folder": "F", "score": 5, "search_pass": 2,
              "album_match": 90.0, "album_match_ok": True,
              "artist_verified": False, "artist_evidence": "",
              "track_matches": 0, "files": [{"filename": "x.flac"}],
              "upload_speed": 3}
        with patch.object(bot, "slskd_search_album_folders",
                          return_value=[fd]) as search, \
             patch.object(bot, "_album_search_context",
                          return_value=("2016", ["Draco"])):
            view = bot._debug_album_search_view("Future", "Zone",
                                                release_mbid="r1")
        self.assertEqual(view["plannedQueries"],
                         bot._album_search_queries("Future", "Zone", "2016",
                                                   ["Draco"]))
        self.assertEqual(search.call_args.kwargs.get("year"), "2016")
        row = view["folders"][0]
        self.assertEqual(row["searchPass"], 2)
        self.assertIs(row["artistVerified"], False)
        self.assertEqual(row["albumMatch"], 90.0)

    def test_group_sources_store_the_query_that_ran(self):
        """source_results["query"] was plan["query"], which is built once and
        never executed — the queries that ran live in stats."""
        group = {"id": "g1", "artist": "Future", "album": "Zone",
                 "missing_tracks": []}
        plan = {"mode": "album", "query": "planned but never run"}
        folders = [{"username": "u", "folder": "F", "score": 1, "files": []}]
        stats = {"queries": ["Future Zone 2016", "Future Zone"]}
        res = bot._apply_group_sources(group, plan, folders, stats)
        self.assertTrue(res["ok"])
        self.assertEqual(group["source_results"]["query"], "Future Zone 2016")
        self.assertEqual(group["source_results"]["queries"], stats["queries"])


class MatcherTests(unittest.TestCase):
    @staticmethod
    def _f(name, **kw):
        return {"filename": name, **kw}

    def test_reverse_containment(self):
        """The MusicBrainz title is longer than the filename — containment was
        only ever tested one way, so this never matched at all."""
        track = {"title": "Everlong (Acoustic Version)", "position": 4}
        files = [self._f("04 - Everlong.flac")]
        hit, basis, _ = bot._best_file_match(track, files, set())
        self.assertIsNotNone(hit)
        self.assertEqual(basis, "contained")

    def test_part_and_roman_numeral_variants(self):
        track = {"title": "Sister Ray, Pt. 2", "position": 7}
        files = [self._f("07 - Sister Ray Part II.flac")]
        hit, basis, _ = bot._best_file_match(track, files, set())
        self.assertIsNotNone(hit)
        self.assertIn(basis, ("exact", "contained", "fuzzy"))

    def test_fuzzy_tolerates_a_typo(self):
        track = {"title": "Paranoid Android", "position": 2}
        files = [self._f("02 - Paranoid Andriod.flac")]
        hit, basis, _ = bot._best_file_match(track, files, set())
        self.assertIsNotNone(hit)
        self.assertEqual(basis, "fuzzy")

    def test_sing_does_not_steal_singularity(self):
        """Pinned regression. A loose hit on a filename another track of the
        release claims more tightly is that track's file: matching it pulled
        down a song already in the library and left the real gap open."""
        track = {"title": "Sing", "position": 2}
        files = [self._f("05 - Singularity.flac")]
        hit, _basis, note = bot._best_file_match(
            track, files, set(), siblings=["Singularity", "Sing"])
        self.assertIsNone(hit)
        # ...and the refusal explains itself rather than reading as "no file".
        self.assertIn("Singularity", note)

    def test_duration_matches_only_when_unambiguous(self):
        track = {"title": "Untitled Track", "position": 3, "duration": 200}
        alone = [self._f("03 - Foreign Name.flac", length=201)]
        hit, basis, _ = bot._best_file_match(track, alone, set())
        self.assertIsNotNone(hit)
        self.assertEqual(basis, "duration")

    def test_duration_refuses_when_two_files_are_equally_close(self):
        track = {"title": "Untitled Track", "position": 3, "duration": 200}
        both = [self._f("03 - Foreign.flac", length=201),
                self._f("04 - Other.flac", length=199)]
        hit, _basis, _ = bot._best_file_match(track, both, set())
        self.assertIsNone(hit)

    def test_duration_refuses_when_another_track_is_the_same_length(self):
        """Two songs on one album within a few seconds of each other is
        completely normal, so duration alone identifies nothing."""
        track = {"title": "Untitled Track", "position": 3, "duration": 200}
        siblings = [track, {"title": "Another", "position": 4, "duration": 202}]
        files = [self._f("03 - Foreign Name.flac", length=201)]
        hit, _basis, _ = bot._best_file_match(track, files, set(),
                                              release_tracks=siblings)
        self.assertIsNone(hit)

    def test_positional_fallback_on_a_full_localized_folder(self):
        """Localized filenames: the folder plainly is the album — right name,
        right file count — but no filename resembles a MusicBrainz title."""
        missing = [{"title": "Alpha", "position": 1},
                   {"title": "Beta", "position": 2},
                   {"title": "Gamma", "position": 3}]
        files = [self._f("01 - アルファ.flac"),
                 self._f("02 - ベータ.flac"),
                 self._f("03 - ガンマ.flac")]
        pairs = bot._album_file_pairs_for_missing_tracks(
            files, missing, folder={"album_match_ok": True},
            release_total=3, release_tracks=missing)
        self.assertEqual(len(pairs), 3)
        self.assertEqual(
            {t["position"]: bot._filename_track_number(f["filename"])
             for f, t in pairs},
            {1: 1, 2: 2, 3: 3})

    def test_positional_fallback_refuses_a_partial_folder(self):
        """A folder that isn't the whole album is not evidence — a blind zip is
        how files get filed as the wrong song."""
        missing = [{"title": "Alpha", "position": 1}, {"title": "Beta", "position": 2}]
        files = [self._f("07 - アルファ.flac")]
        pairs = bot._album_file_pairs_for_missing_tracks(
            files, missing, folder={"album_match_ok": True},
            release_total=12, release_tracks=missing)
        self.assertEqual(pairs, [])

    def test_positional_fallback_refuses_an_unrecognised_folder(self):
        missing = [{"title": "Alpha", "position": 1}, {"title": "Beta", "position": 2}]
        files = [self._f("01 - アルファ.flac"),
                 self._f("02 - ベータ.flac")]
        pairs = bot._album_file_pairs_for_missing_tracks(
            files, missing, folder={"album_match_ok": False},
            release_total=2, release_tracks=missing)
        # Falls through to the pre-existing exact-count zip, which is fine, but
        # the *track-number* path must not fire for an unrecognised folder.
        self.assertEqual(len(pairs), 2)

    def test_exact_title_still_wins_over_a_fuzzy_one(self):
        track = {"title": "Alpha", "position": 1}
        files = [self._f("07 - Alpha Beta.flac"), self._f("01 - Alpha.flac")]
        hit, basis, _ = bot._best_file_match(track, files, set())
        self.assertEqual(hit["filename"], "01 - Alpha.flac")
        self.assertEqual(basis, "exact")


class SimilarAlbumsArtistResolutionTests(unittest.TestCase):
    """`/api/album/similar` must work for a caller that only has a name.

    ListenBrainz's similar-artists endpoint is keyed by MBID and answers
    nothing for a bare name, so a name-only call returned an empty shelf every
    time — which is exactly what both clients send, because Navidrome's
    `albumArtists` rows carry only an id and a name. Caught by calling the live
    route after deploying, not by any test.
    """

    def test_a_name_only_call_resolves_the_mbid_from_the_library_index(self):
        rows = [{"id": "nd1", "name": "Radiohead",
                 "mbid": "a74b1b7f-71a5-4011-9441-d0b5e4122711"},
                {"id": "nd2", "name": "Muse", "mbid": "m2"}]
        seen = {}

        def fake_similar(artist_mbid, artist_name, limit=40):
            seen["mbid"] = artist_mbid
            return []

        with patch("listenbrainz_bot._artist_index_rows", return_value=rows), \
             patch("listenbrainz_bot.similar_artists", side_effect=fake_similar):
            bot._similar_albums_for_artist("", "Radiohead")
            # Without the route's resolution step the helper is handed "".
            self.assertEqual(seen["mbid"], "")

        # The route is what resolves it, so drive the resolution the route does.
        resolved = ""
        wanted = "radiohead"
        for row in rows:
            if row.get("mbid") and row["name"].strip().lower() == wanted:
                resolved = row["mbid"]
                break
        self.assertEqual(resolved, "a74b1b7f-71a5-4011-9441-d0b5e4122711")

    def test_the_route_passes_a_resolved_mbid_through(self):
        """The whole point: a name-only request must reach `similar_artists`
        carrying an MBID."""
        import re
        src = inspect.getsource(bot.start_web_dashboard)
        body = re.search(r"def api_album_similar\(\):.*?(?=\n    @app\.)", src, re.S)
        self.assertIsNotNone(body, "api_album_similar not found")
        self.assertIn("_artist_index_rows()", body.group(0),
                      "a name-only call must resolve the MBID from the library "
                      "index — ListenBrainz answers nothing for a bare name")


class SimilarAlbumRoutingTests(unittest.TestCase):
    """Every row of the similar-albums shelf is an album the library HOLDS.

    `_SIMILAR_ALBUM_STATUS_RANK` excludes `missing` by construction, so a row
    that does not carry its Navidrome album id sends the client to the virtual
    album page — which offers to *download* a record already on disk. The Fresh
    feed carries `releaseAlbumId` for exactly this reason; this shelf did not.
    """

    CAND = [{"mbid": "mb-sim", "name": "Similar Act", "score": 1.0,
             "sources": ["listenbrainz"]}]
    ROWS = [{"id": "nd-sim", "name": "Similar Act", "mbid": "mb-sim"}]

    def _shelf(self, releases, rgid_map=None):
        indexed = {"artist_mbid": "mb-sim", "artist_name": "Similar Act",
                   "releases": releases}
        with patch("listenbrainz_bot.similar_artists", return_value=self.CAND), \
             patch("listenbrainz_bot._artist_index_rows", return_value=self.ROWS), \
             patch("listenbrainz_bot._index_get_artist", return_value=indexed), \
             patch("listenbrainz_bot._index_rgid_album_ids",
                   return_value=rgid_map if rgid_map is not None else {}):
            return bot._similar_albums_for_artist("mb-seed", "Seed")

    def test_the_row_carries_the_navidrome_album_id(self):
        out = self._shelf([{"rgid": "rg1", "title": "A Record", "year": "2001",
                            "status": "complete",
                            "navidrome_album_ids": ["nd-album-1"]}])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["albumId"], "nd-album-1",
                         "a tap on an owned album must open the album, not its "
                         "download page")

    def test_a_row_without_ids_falls_back_to_the_rgid_map(self):
        """`_index_mark_release_present` leaves `nd_album_ids` alone, so a row
        filled by lb-bot itself can reach here empty."""
        out = self._shelf([{"rgid": "rg2", "title": "B Record", "year": "2002",
                            "status": "complete"}],
                          rgid_map={"rg2": "nd-album-2"})
        self.assertEqual(out[0]["albumId"], "nd-album-2")

    def test_an_unresolvable_row_keeps_an_empty_id_rather_than_failing(self):
        out = self._shelf([{"rgid": "rg3", "title": "C Record", "year": "2003",
                            "status": "complete"}], rgid_map={})
        self.assertEqual(out[0]["albumId"], "")

    def test_the_rgid_map_is_never_built_when_no_row_needs_it(self):
        """It is a full-table scan, and it used to run on every call — including
        calls that return nothing, which is what broke the suite: the map was
        built before the early return, against an index DB the test never had.
        """
        calls = []
        indexed = {"artist_mbid": "mb-sim", "artist_name": "Similar Act",
                   "releases": [{"rgid": "rg1", "title": "A", "year": "2001",
                                 "status": "complete",
                                 "navidrome_album_ids": ["nd-1"]}]}
        with patch("listenbrainz_bot.similar_artists", return_value=self.CAND), \
             patch("listenbrainz_bot._artist_index_rows", return_value=self.ROWS), \
             patch("listenbrainz_bot._index_get_artist", return_value=indexed), \
             patch("listenbrainz_bot._index_rgid_album_ids",
                   side_effect=lambda: calls.append(1) or {}):
            bot._similar_albums_for_artist("mb-seed", "Seed")
        self.assertEqual(calls, [])


class SimilarArtistsMarkedTests(unittest.TestCase):
    """`/api/artist/similar` marks ownership; it does not filter on it.

    The merge has always produced unowned candidates with real MBIDs and
    `_similar_albums_for_artist` has always thrown them away — that shelf is
    "more of what you already have". The Discover row wants exactly the rows
    that shelf discards, so the one thing these tests defend is that an unowned
    candidate survives.
    """

    CANDIDATES = [
        {"mbid": "mb-owned", "name": "Owned Indexed", "score": 1.5,
         "sources": ["listenbrainz", "lastfm"]},
        {"mbid": "mb-unowned", "name": "Stranger", "score": 1.2,
         "sources": ["listenbrainz"]},
        {"mbid": "mb-owned-cold", "name": "Owned Cold", "score": 0.9,
         "sources": ["lastfm"]},
    ]
    ROWS = [{"id": "nd1", "name": "Owned Indexed", "mbid": "mb-owned"},
            {"id": "nd2", "name": "Owned Cold", "mbid": "mb-owned-cold"}]

    def _marked(self, **kw):
        with patch("listenbrainz_bot.similar_artists", return_value=self.CANDIDATES), \
             patch("listenbrainz_bot._artist_index_rows", return_value=self.ROWS), \
             patch("listenbrainz_bot._index_indexed_artist_mbids",
                   return_value={"mb-owned"}):
            return bot._similar_artists_marked(kw.get("mbid", "seed"),
                                               kw.get("name", "Seed Artist"))

    def test_an_unowned_candidate_survives(self):
        out = self._marked()
        by_name = {a["name"]: a for a in out["artists"]}
        self.assertEqual(len(out["artists"]), 3,
                         "every candidate must survive — this route marks, "
                         "it does not filter")
        self.assertFalse(by_name["Stranger"]["owned"])
        self.assertEqual(by_name["Stranger"]["artistId"], "")

    def test_owned_and_indexed_are_separate_facts(self):
        by_name = {a["name"]: a for a in self._marked()["artists"]}
        self.assertTrue(by_name["Owned Indexed"]["owned"])
        self.assertTrue(by_name["Owned Indexed"]["indexed"])
        self.assertEqual(by_name["Owned Indexed"]["artistId"], "nd1")
        # Owned, but lb-bot has never walked their discography — "what am I
        # missing from them" still needs a scan, and the client shows that
        # differently from either extreme.
        self.assertTrue(by_name["Owned Cold"]["owned"])
        self.assertFalse(by_name["Owned Cold"]["indexed"])

    def test_a_name_only_call_resolves_the_mbid_from_the_library_index(self):
        """ListenBrainz is MBID-keyed and answers nothing for a bare name."""
        seen = {}

        def fake_similar(artist_mbid, artist_name, limit=20):
            seen["mbid"] = artist_mbid
            return []

        with patch("listenbrainz_bot.similar_artists", side_effect=fake_similar), \
             patch("listenbrainz_bot._artist_index_rows", return_value=self.ROWS), \
             patch("listenbrainz_bot._index_indexed_artist_mbids", return_value=set()):
            bot._similar_artists_marked("", "Owned Indexed")
        self.assertEqual(seen["mbid"], "mb-owned")

    def test_a_cold_index_degrades_to_no_badges_rather_than_raising(self):
        with patch("listenbrainz_bot.similar_artists", return_value=self.CANDIDATES), \
             patch("listenbrainz_bot._artist_index_rows",
                   side_effect=RuntimeError("no such column: artist_mbid")), \
             patch("listenbrainz_bot._index_indexed_artist_mbids", return_value=set()):
            out = bot._similar_artists_marked("seed", "Seed Artist")
        self.assertEqual(len(out["artists"]), 3)
        self.assertTrue(all(not a["owned"] and not a["indexed"]
                            for a in out["artists"]))

    def test_the_seed_is_named_so_the_row_can_say_why(self):
        self.assertEqual(self._marked()["because"], "Seed Artist")
        self.assertIn("ListenBrainz", self._marked()["sources"])

    def test_it_never_spends_the_musicbrainz_budget(self):
        """The 1 req/sec `_mbz_lock` is shared with any running discography
        scan. A Discover row that queued behind it would render when the scan
        finished, which is not a discovery surface."""
        called = []
        with patch("listenbrainz_bot.similar_artists", return_value=self.CANDIDATES), \
             patch("listenbrainz_bot._artist_index_rows", return_value=self.ROWS), \
             patch("listenbrainz_bot._index_indexed_artist_mbids", return_value=set()), \
             patch("listenbrainz_bot.mbz_get",
                   side_effect=lambda *a, **k: called.append(a) or {}):
            bot._similar_artists_marked("", "Owned Indexed")
        self.assertEqual(called, [], "this route must not touch MusicBrainz")

    def test_indexed_mbids_come_from_the_index_db(self):
        with isolated_review():
            bot._index_ensure_artist("mb-walked", artist_mbid="mb-walked",
                                     nd_artist_id="nd9", name="Walked")
            self.assertIn("mb-walked", bot._index_indexed_artist_mbids())
            self.assertNotIn("mb-never", bot._index_indexed_artist_mbids())


class EditorialMetadataTests(unittest.TestCase):
    """The MusicBrainz -> Wikidata -> Wikipedia chain, its caches, and the
    additive `meta` table migration."""

    def setUp(self):
        import shutil
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, True)
        self.db_path = os.path.join(td, "index.db")
        scratch_index(self, self.db_path)
        bot._wiki_cache.clear()
        self.addCleanup(bot._wiki_cache.clear)

    # -- the resolution chain ------------------------------------------------

    URL_RELS = {
        "name": "Radiohead",
        "disambiguation": "",
        "type": "Group",
        "country": "GB",
        "relations": [
            {"type": "wikidata",
             "url": {"resource": "https://www.wikidata.org/wiki/Q45188"}},
            {"type": "official homepage",
             "url": {"resource": "https://radiohead.com/"}},
            {"type": "not a link type we render",
             "url": {"resource": "https://example.invalid/"}},
        ],
    }

    def _wiki_responses(self, *, has_article=True, description="English rock band"):
        """A _wiki_get stand-in that answers both Wikimedia endpoints."""
        def fake(url, params):
            if params.get("action") == "wbgetentities":
                ent = {"descriptions": {}, "sitelinks": {}}
                if description:
                    ent["descriptions"] = {"en": {"value": description}}
                if has_article:
                    ent["sitelinks"] = {"enwiki": {"title": "Radiohead"}}
                return {"entities": {"Q45188": ent}}
            if params.get("action") == "query":
                if not has_article:
                    return {"query": {"pages": {"-1": {"missing": ""}}}}
                return {"query": {"pages": {"1": {
                    "title": "Radiohead",
                    "extract": "Radiohead are an English rock band.\n"
                               "== Career ==\n"
                               "They formed in 1985.\n",
                    "thumbnail": {"source": "https://upload.example/rh.jpg"},
                }}}}
            return {}
        return fake

    def test_full_chain_returns_text_attribution_and_links(self):
        with patch("listenbrainz_bot.mbz_get") as mbz, \
             patch("listenbrainz_bot._wiki_get", side_effect=self._wiki_responses()):
            mbz.side_effect = lambda path, params=None, **kw: (
                self.URL_RELS if params and params.get("inc") == "url-rels" else {})
            meta = bot.meta_for_artist("a-mbid")
        self.assertEqual(meta["summary"], "Radiohead are an English rock band.")
        # Section headings must not survive into the prose.
        self.assertEqual(meta["paragraphs"],
                         ["Radiohead are an English rock band.", "They formed in 1985."])
        self.assertEqual(meta["wikidataDescription"], "English rock band")
        self.assertEqual(meta["wikidataQid"], "Q45188")
        self.assertEqual(meta["source"]["name"], "Wikipedia")
        self.assertEqual(meta["source"]["license"], "CC BY-SA 4.0")
        self.assertIn("en.wikipedia.org/wiki/Radiohead", meta["source"]["url"])
        self.assertEqual(meta["imageUrl"], "https://upload.example/rh.jpg")
        # Only known url-rel types are rendered, official site first.
        self.assertEqual([l["type"] for l in meta["links"]],
                         ["official homepage", "wikidata"])

    def test_wikidata_description_survives_a_missing_article(self):
        """A3: the one-liner is the whole point — it exists for entities that
        have no Wikipedia article at all."""
        with patch("listenbrainz_bot.mbz_get") as mbz, \
             patch("listenbrainz_bot._wiki_get",
                   side_effect=self._wiki_responses(has_article=False)):
            mbz.side_effect = lambda path, params=None, **kw: (
                self.URL_RELS if params and params.get("inc") == "url-rels" else {})
            meta = bot.meta_for_artist("a-mbid")
        self.assertEqual(meta["summary"], "")
        self.assertEqual(meta["paragraphs"], [])
        self.assertEqual(meta["wikidataDescription"], "English rock band")
        self.assertEqual(meta["source"], {})

    def test_a_bare_wikipedia_url_rel_is_used_when_wikidata_is_absent(self):
        rels = {"name": "X", "relations": [
            {"type": "wikipedia",
             "url": {"resource": "https://en.wikipedia.org/wiki/Some_Band"}}]}
        seen = {}

        def fake_wiki(url, params):
            seen.update(params)
            return {"query": {"pages": {"1": {
                "title": "Some Band", "extract": "A band.\n"}}}}

        with patch("listenbrainz_bot.mbz_get") as mbz, \
             patch("listenbrainz_bot._wiki_get", side_effect=fake_wiki):
            mbz.side_effect = lambda path, params=None, **kw: (
                rels if params and params.get("inc") == "url-rels" else {})
            meta = bot.meta_for_artist("a-mbid")
        self.assertEqual(seen.get("titles"), "Some Band")
        self.assertEqual(meta["summary"], "A band.")

    def test_no_wikipedia_article_answers_empty_rather_than_raising(self):
        with patch("listenbrainz_bot.mbz_get", return_value={}), \
             patch("listenbrainz_bot._wiki_get", return_value={}):
            meta = bot.meta_for_artist("obscure-mbid")
        self.assertEqual(meta["summary"], "")
        self.assertEqual(meta["paragraphs"], [])
        self.assertEqual(meta["links"], [])
        self.assertEqual(meta["mbid"], "obscure-mbid")

    def test_paragraphs_are_capped_for_the_hub_response_ceiling(self):
        long_extract = "\n".join(f"Paragraph {i}." for i in range(30))

        def fake_wiki(url, params):
            if params.get("action") == "wbgetentities":
                return {"entities": {"Q45188": {
                    "sitelinks": {"enwiki": {"title": "T"}}, "descriptions": {}}}}
            return {"query": {"pages": {"1": {"title": "T",
                                              "extract": long_extract}}}}

        with patch("listenbrainz_bot.mbz_get") as mbz, \
             patch("listenbrainz_bot._wiki_get", side_effect=fake_wiki):
            mbz.side_effect = lambda path, params=None, **kw: (
                self.URL_RELS if params and params.get("inc") == "url-rels" else {})
            meta = bot.meta_for_artist("a-mbid")
        self.assertEqual(len(meta["paragraphs"]), bot.META_MAX_PARAGRAPHS)

    # -- relations and credits ----------------------------------------------

    def test_artist_relations_are_bucketed_and_name_the_other_end(self):
        rels_by_inc = {
            "url-rels": self.URL_RELS,
            "artist-rels": {"relations": [
                {"type": "member of band", "direction": "backward",
                 "begin": "1985-01-01", "ended": False,
                 "artist": {"id": "m1", "name": "Thom Yorke"}},
                {"type": "collaboration",
                 "artist": {"id": "c1", "name": "Atoms for Peace"}},
                {"type": "wikidata", "artist": {"id": "x", "name": "Ignored"}},
            ]},
        }
        with patch("listenbrainz_bot.mbz_get") as mbz, \
             patch("listenbrainz_bot._wiki_get", side_effect=self._wiki_responses()):
            mbz.side_effect = lambda path, params=None, **kw: rels_by_inc.get(
                (params or {}).get("inc", ""), {})
            meta = bot.meta_for_artist("a-mbid")
        self.assertEqual([m["name"] for m in meta["relations"]["members"]],
                         ["Thom Yorke"])
        self.assertEqual(meta["relations"]["members"][0]["begin"], "1985")
        self.assertEqual([r["name"] for r in meta["relations"]["related"]],
                         ["Atoms for Peace"])

    def test_band_members_are_collapsed_to_one_row_each(self):
        """MusicBrainz states one relation per instrument and per stint, so a
        live run showed "Colin Greenwood" four times in a row."""
        rels_by_inc = {
            "url-rels": self.URL_RELS,
            "artist-rels": {"relations": [
                {"type": "member of band", "begin": "1991", "end": "1995",
                 "ended": True, "attributes": ["bass guitar"],
                 "artist": {"id": "m1", "name": "Colin Greenwood"}},
                {"type": "member of band", "begin": "1985", "ended": False,
                 "attributes": ["keyboard"],
                 "artist": {"id": "m1", "name": "Colin Greenwood"}},
                {"type": "member of band", "begin": "1985", "ended": False,
                 "attributes": ["guitar"],
                 "artist": {"id": "m2", "name": "Jonny Greenwood"}},
            ]},
        }
        with patch("listenbrainz_bot.mbz_get") as mbz, \
             patch("listenbrainz_bot._wiki_get", side_effect=self._wiki_responses()):
            mbz.side_effect = lambda path, params=None, **kw: rels_by_inc.get(
                (params or {}).get("inc", ""), {})
            members = bot.meta_for_artist("a-mbid")["relations"]["members"]
        self.assertEqual([m["name"] for m in members],
                         ["Colin Greenwood", "Jonny Greenwood"])
        colin = members[0]
        self.assertEqual(colin["attributes"], ["bass guitar", "keyboard"])
        # Earliest stint wins, and an open one means they have not left.
        self.assertEqual(colin["begin"], "1985")
        self.assertFalse(colin["ended"])
        self.assertEqual(colin["end"], "")

    def test_links_are_capped_per_label(self):
        """A well-tagged artist carries a purchase relation per storefront; a
        live run rendered five identical `Buy` chips in a row.

        Capped on the *label*, not the relation type: `free streaming` and
        `streaming` are two types sharing one label, and a per-type cap still
        let `Stream` through four times."""
        rels = {"name": "X", "relations": [
            {"type": "purchase for download",
             "url": {"resource": f"https://shop{i}.example/"}}
            for i in range(5)
        ] + [
            {"type": "free streaming", "url": {"resource": "https://s1.example/"}},
            {"type": "free streaming", "url": {"resource": "https://s2.example/"}},
            {"type": "streaming", "url": {"resource": "https://s3.example/"}},
        ] + [
            {"type": "official homepage", "url": {"resource": "https://x.example/"}},
            # The same URL stated twice must also collapse.
            {"type": "discogs", "url": {"resource": "https://discogs.example/x"}},
            {"type": "discogs", "url": {"resource": "https://discogs.example/x"}},
        ]}
        with patch("listenbrainz_bot.mbz_get") as mbz, \
             patch("listenbrainz_bot._wiki_get", return_value={}):
            mbz.side_effect = lambda path, params=None, **kw: (
                rels if params and params.get("inc") == "url-rels" else {})
            links = bot.meta_for_artist("a-mbid")["links"]
        self.assertEqual(
            [l["label"] for l in links],
            ["Official site", "Discogs", "Buy", "Buy", "Stream", "Stream"])

    def test_album_credits_collapse_roles_per_person(self):
        release_rels = {"relations": [
            {"type": "producer", "artist": {"id": "p1", "name": "Nigel Godrich"}},
            {"type": "engineer", "artist": {"id": "p1", "name": "Nigel Godrich"}},
            {"type": "recording", "artist": {"id": "", "name": ""}},
        ]}

        def fake_mbz(path, params=None, **kw):
            if path.startswith("release-group/"):
                return {"title": "OK Computer", "relations": [],
                        "first-release-date": "1997-05-21"}
            if path.startswith("release/"):
                return release_rels
            return {}

        with patch("listenbrainz_bot.mbz_get", side_effect=fake_mbz), \
             patch("listenbrainz_bot._wiki_get", return_value={}):
            meta = bot.meta_for_album("rg-1", release_mbid="rel-1")
        self.assertEqual(meta["credits"],
                         [{"mbid": "p1", "name": "Nigel Godrich",
                           "roles": ["producer", "engineer"]}])
        self.assertEqual(meta["title"], "OK Computer")
        self.assertEqual(meta["firstReleased"], "1997-05-21")

    def test_album_credits_failure_degrades_to_empty(self):
        def fake_mbz(path, params=None, **kw):
            if path.startswith("release-group/"):
                return {"title": "T", "relations": []}
            raise RuntimeError("MusicBrainz is having a bad afternoon")

        with patch("listenbrainz_bot.mbz_get", side_effect=fake_mbz), \
             patch("listenbrainz_bot._wiki_get", return_value={}):
            meta = bot.meta_for_album("rg-1")
        self.assertEqual(meta["credits"], [])
        self.assertEqual(meta["title"], "T")

    # -- caching -------------------------------------------------------------

    def test_a_hit_is_cached_and_the_second_call_asks_nobody(self):
        with patch("listenbrainz_bot.mbz_get") as mbz, \
             patch("listenbrainz_bot._wiki_get",
                   side_effect=self._wiki_responses()) as wiki:
            mbz.side_effect = lambda path, params=None, **kw: (
                self.URL_RELS if params and params.get("inc") == "url-rels" else {})
            first = bot.meta_for_artist("a-mbid")
            calls_after_first = (mbz.call_count, wiki.call_count)
            second = bot.meta_for_artist("a-mbid")
            self.assertEqual((mbz.call_count, wiki.call_count), calls_after_first)
        self.assertEqual(first["summary"], second["summary"])

    def test_refresh_bypasses_the_cache(self):
        with patch("listenbrainz_bot.mbz_get") as mbz, \
             patch("listenbrainz_bot._wiki_get", side_effect=self._wiki_responses()):
            mbz.side_effect = lambda path, params=None, **kw: (
                self.URL_RELS if params and params.get("inc") == "url-rels" else {})
            bot.meta_for_artist("a-mbid")
            before = mbz.call_count
            bot.meta_for_artist("a-mbid", refresh=True)
            self.assertGreater(mbz.call_count, before)

    def test_a_miss_is_cached_too_but_expires_sooner(self):
        """"No article" must not cost a MusicBrainz second on every page open —
        and must still be re-asked well before a hit would be."""
        with patch("listenbrainz_bot.mbz_get", return_value={}) as mbz, \
             patch("listenbrainz_bot._wiki_get", return_value={}):
            bot.meta_for_artist("obscure")
            self.assertEqual(mbz.call_count, 2)   # url-rels + artist-rels
            bot.meta_for_artist("obscure")
            self.assertEqual(mbz.call_count, 2)   # served from the negative cache

        conn = bot._index_db()
        row = conn.execute(
            "SELECT ok, fetched_at FROM meta WHERE kind='artist' AND mbid='obscure'"
        ).fetchone()
        self.assertEqual(row["ok"], 0)
        # Age it past the negative TTL but well inside the positive one.
        aged = time.time() - (bot.META_TTL_EMPTY + 60)
        self.assertLess(bot.META_TTL_EMPTY + 60, bot.META_TTL_OK)
        conn.execute("UPDATE meta SET fetched_at = ? WHERE mbid = 'obscure'", (aged,))
        conn.commit()
        self.assertIsNone(bot._meta_cache_read("artist", "obscure"))

    def test_a_hit_survives_past_the_negative_ttl(self):
        with patch("listenbrainz_bot.mbz_get") as mbz, \
             patch("listenbrainz_bot._wiki_get", side_effect=self._wiki_responses()):
            mbz.side_effect = lambda path, params=None, **kw: (
                self.URL_RELS if params and params.get("inc") == "url-rels" else {})
            bot.meta_for_artist("a-mbid")
        conn = bot._index_db()
        conn.execute("UPDATE meta SET fetched_at = ?",
                     (time.time() - (bot.META_TTL_EMPTY + 60),))
        conn.commit()
        self.assertIsNotNone(bot._meta_cache_read("artist", "a-mbid"))

    # -- migration -----------------------------------------------------------

    def test_meta_table_is_added_to_an_existing_index_db(self):
        """The additive migration on a DB built before the meta table existed —
        and INDEX_SCAN_VERSION is not what gates it, so no rescan is forced."""
        import sqlite3
        old = sqlite3.connect(self.db_path)
        old.executescript("""
            CREATE TABLE artists (
              artist_key TEXT PRIMARY KEY, artist_mbid TEXT NOT NULL DEFAULT '',
              nd_artist_id TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '',
              scanned_at REAL NOT NULL DEFAULT 0,
              scan_version INTEGER NOT NULL DEFAULT 1);
            INSERT INTO artists (artist_key, name, scan_version)
              VALUES ('nd:1', 'Pre-existing', 2);
        """)
        old.commit()
        old.close()
        swap_index(self.db_path)

        conn = bot._index_db()
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("meta", tables)
        # The pre-existing rows, and their scan_version, are untouched.
        row = conn.execute("SELECT name, scan_version FROM artists").fetchone()
        self.assertEqual((row["name"], row["scan_version"]), ("Pre-existing", 2))

        bot._meta_cache_write("artist", "m1", {"summary": "hi"}, True)
        self.assertEqual(bot._meta_cache_read("artist", "m1"), {"summary": "hi"})

    def test_wiki_get_never_touches_the_musicbrainz_rate_limit_budget(self):
        """Constraint that will bite if ignored: `_mbz_lock` is a hard global
        1 req/sec that is the discography scanner's entire budget. Two Wikimedia
        calls behind it would cost every artist page two scan-seconds."""
        import ast
        import inspect
        src = inspect.getsource(bot._wiki_get)
        tree = ast.parse(textwrap.dedent(src))
        # The docstring explains the rule and names both, so assert against the
        # body rather than the source text.
        body = ast.get_docstring(tree.body[0], clean=False)
        code = textwrap.dedent(src).replace(body or "", "")
        self.assertNotIn("_mbz_lock", code)
        self.assertNotIn("mbz_get(", code)
        self.assertIn("User-Agent", code)



class AlbumLookupOwnershipTests(unittest.TestCase):
    """`/api/album/lookup` marks ownership; it does not filter on it.

    The route was MusicBrainz's ranking and nothing else, which is right for the
    SPA's own Library panel — that is a download form. Put the same rows in a
    client's search box and the unanswered question becomes a wrong answer: a
    row captioned "not in your library" about a record the library holds, whose
    tap opens a download page for an album already on disk. That is the Fresh
    tab's `releaseAlbumId` bug and the similar-albums shelf's bug, twice paid
    for. These tests defend the pair of fields that answer it, and the promise
    that adding them costs no MusicBrainz request.
    """

    CANDIDATES = [
        {"rgid": "rg-single", "title": "A Single", "artist": "Band",
         "primary_type": "single", "year": "2001", "score": 100},
        {"rgid": "rg-owned", "title": "Owned Record", "artist": "Band",
         "primary_type": "album", "year": "1999", "score": 90},
        {"rgid": "rg-unowned", "title": "Stranger", "artist": "Band",
         "primary_type": "album", "year": "2004", "score": 95},
    ]

    def _looked_up(self, **kw):
        with patch("listenbrainz_bot.mbz_search_release_groups",
                   return_value=[dict(c) for c in self.CANDIDATES]), \
             patch("listenbrainz_bot._index_owned_rgids",
                   return_value=kw.get("owned", {"rg-owned"})), \
             patch("listenbrainz_bot._index_rgid_album_ids",
                   return_value=kw.get("album_ids", {"rg-owned": "nd-42"})):
            return bot._album_lookup_marked(kw.get("q", "band owned record"))

    def test_an_owned_release_group_names_the_album_to_open(self):
        by_rgid = {c["rgid"]: c for c in self._looked_up()}
        self.assertTrue(by_rgid["rg-owned"]["releaseOwned"])
        # The id is the whole point: without it a client has only the virtual
        # album page's redirect, which cannot fire for an album lb-bot filled
        # itself until the present-row backfill has run.
        self.assertEqual(by_rgid["rg-owned"]["releaseAlbumId"], "nd-42")

    def test_an_unowned_candidate_survives_and_names_no_album(self):
        out = self._looked_up()
        by_rgid = {c["rgid"]: c for c in out}
        self.assertEqual(len(out), 3,
                         "every candidate must survive — this route marks, "
                         "it does not filter")
        self.assertFalse(by_rgid["rg-unowned"]["releaseOwned"])
        self.assertEqual(by_rgid["rg-unowned"]["releaseAlbumId"], "")

    def test_an_owned_row_with_no_resolved_album_id_stays_owned(self):
        """`_index_owned_rgids` counts any non-`missing` row, and a row lb-bot
        flipped to `present` at placement carries no Navidrome ids until the
        backfill resolves them. Owned with nowhere to send the tap is a real
        state — it must not read as unowned, which would offer to fetch a record
        already on disk."""
        by_rgid = {c["rgid"]: c for c in self._looked_up(album_ids={})}
        self.assertTrue(by_rgid["rg-owned"]["releaseOwned"])
        self.assertEqual(by_rgid["rg-owned"]["releaseAlbumId"], "")

    def test_albums_still_sort_ahead_of_singles(self):
        """The sort moved out of the route and into the helper; the SPA's
        Library panel auto-selects `candidates[0]`, so changing it would change
        which record that form is pointed at."""
        self.assertEqual([c["rgid"] for c in self._looked_up()],
                         ["rg-unowned", "rg-owned", "rg-single"])

    def test_the_existing_keys_are_untouched(self):
        """`primary_type` is snake_case and out of step with the rest of the
        API, but `web/src/panels/Library.jsx` reads it. This is additive."""
        first = self._looked_up()[0]
        for key in ("rgid", "title", "artist", "primary_type", "year", "score"):
            self.assertIn(key, first)
        self.assertEqual(first["primary_type"], "album")

    def test_cover_art_comes_from_the_archive_not_from_navidrome(self):
        """lb-bot's own `/api/cover` is Navidrome art keyed by a Navidrome album
        id, so it has nothing to serve for a release the library lacks."""
        by_rgid = {c["rgid"]: c for c in self._looked_up()}
        self.assertEqual(by_rgid["rg-unowned"]["coverUrl"],
                         bot.caa_front_url("rg-unowned"))

    def test_a_cold_index_degrades_to_no_badges_rather_than_raising(self):
        with patch("listenbrainz_bot.mbz_search_release_groups",
                   return_value=[dict(c) for c in self.CANDIDATES]), \
             patch("listenbrainz_bot._index_owned_rgids",
                   side_effect=RuntimeError("no such table: release_groups")), \
             patch("listenbrainz_bot._index_rgid_album_ids", return_value={}):
            out = bot._album_lookup_marked("band")
        self.assertEqual(len(out), 3)
        self.assertTrue(all(not c["releaseOwned"] and not c["releaseAlbumId"]
                            for c in out))

    def test_marking_spends_no_extra_musicbrainz_budget(self):
        """This route always cost one search against the process-wide 1 req/sec
        `_mbz_lock`, shared with any running discography scan. Ownership is read
        from the library index, so it must still cost exactly that one."""
        called = []
        with patch("listenbrainz_bot.mbz_get",
                   side_effect=lambda *a, **k: called.append(a) or {}), \
             patch("listenbrainz_bot._index_owned_rgids", return_value=set()), \
             patch("listenbrainz_bot._index_rgid_album_ids", return_value={}):
            bot._album_lookup_marked("band")
        self.assertEqual(len(called), 1,
                         "the search itself, and nothing more")
        self.assertEqual(called[0][0], "release-group")


class DeezerBrowseOwnershipTests(unittest.TestCase):
    """The Deezer browse rows mark ownership by name, conservatively.

    Deezer publishes no MBIDs, so every resolution here is a name match against
    the library index. That is precisely the case where a guess is worse than a
    blank: a chart row wrongly badged "in library" sends the tap to an album
    that is not there, and one wrongly badged unowned offers to download a
    record already on disk — the same pair of mistakes `/api/album/lookup` and
    the Fresh tab each paid for once.

    The other thing these defend is the budget. A browse row must never take
    `_mbz_lock`: it is the discography scanner's whole 1 req/sec allowance, and
    a Discover screen that queued behind it would render when the scan finished.
    """

    CHART = {
        "albums": [
            {"deezerId": "1", "title": "Owned Record", "artist": "Band",
             "imageUrl": "https://dz/1.jpg", "recordType": "album"},
            {"deezerId": "2", "title": "Known But Missing", "artist": "Band",
             "imageUrl": "https://dz/2.jpg", "recordType": "album"},
            {"deezerId": "3", "title": "Never Heard Of", "artist": "Stranger",
             "imageUrl": "https://dz/3.jpg", "recordType": "album"},
        ],
        "artists": [
            {"deezerId": "10", "name": "Band", "imageUrl": "https://dz/a.jpg"},
            {"deezerId": "11", "name": "Stranger", "imageUrl": "https://dz/b.jpg"},
        ],
    }
    DIRECTORY = {
        "band|owned record": {"rgid": "rg-owned", "status": "complete",
                              "albumId": "nd-42", "artistMbid": "mb-band",
                              "artistName": "Band"},
        "band|known but missing": {"rgid": "rg-missing", "status": "missing",
                                   "albumId": "", "artistMbid": "mb-band",
                                   "artistName": "Band"},
    }
    ROWS = [{"id": "nd1", "name": "Band", "mbid": "mb-band"}]

    @contextlib.contextmanager
    def _stack(self, directory=None, rows=None):
        with patch("listenbrainz_bot.deezer_chart", return_value=self.CHART), \
             patch("listenbrainz_bot._index_release_group_directory",
                   return_value=self.DIRECTORY if directory is None else directory), \
             patch("listenbrainz_bot._artist_index_rows",
                   return_value=self.ROWS if rows is None else rows), \
             patch("listenbrainz_bot._index_indexed_artist_mbids",
                   return_value={"mb-band"}):
            yield

    def _marked(self):
        with self._stack():
            return bot._deezer_chart_marked(20)

    def test_an_owned_album_names_the_navidrome_album_to_open(self):
        by_id = {a["deezerId"]: a for a in self._marked()["albums"]}
        self.assertTrue(by_id["1"]["releaseOwned"])
        self.assertEqual(by_id["1"]["releaseAlbumId"], "nd-42")
        self.assertEqual(by_id["1"]["rgid"], "rg-owned")

    def test_a_known_but_missing_release_group_keeps_its_rgid(self):
        """`missing` rows are the most useful rows in the directory: they give
        an unowned tile a real release-group id, which is what the one-tap
        acquire needs. Dropping them would leave the row un-actionable."""
        by_id = {a["deezerId"]: a for a in self._marked()["albums"]}
        self.assertFalse(by_id["2"]["releaseOwned"])
        self.assertEqual(by_id["2"]["rgid"], "rg-missing")
        self.assertEqual(by_id["2"]["releaseAlbumId"], "")

    def test_an_unresolved_album_guesses_nothing(self):
        by_id = {a["deezerId"]: a for a in self._marked()["albums"]}
        self.assertFalse(by_id["3"]["releaseOwned"])
        self.assertEqual(by_id["3"]["rgid"], "")
        self.assertEqual(by_id["3"]["releaseAlbumId"], "")
        # Deezer's own cover is the only art there is when no release-group
        # resolved — the Archive is keyed by rgid and lb-bot's /api/cover by a
        # Navidrome album id, so neither has anything to serve.
        self.assertEqual(by_id["3"]["coverUrl"], "https://dz/3.jpg")

    def test_a_resolved_album_prefers_the_cover_art_archive(self):
        by_id = {a["deezerId"]: a for a in self._marked()["albums"]}
        self.assertEqual(by_id["1"]["coverUrl"], bot.caa_front_url("rg-owned"))

    def test_an_edition_suffix_still_matches_the_record_on_disk(self):
        """`_fuzzy_album_text` strips "(Deluxe Edition)" and friends. For
        *ownership* that conflation is correct — you hold the record — which is
        why the fuzzy key is a fallback here and is not used anywhere that
        picks a concrete release."""
        chart = {"albums": [{"deezerId": "9", "title": "Owned Record (Deluxe Edition)",
                             "artist": "Band", "imageUrl": ""}],
                 "artists": []}
        with patch("listenbrainz_bot.deezer_chart", return_value=chart), \
             patch("listenbrainz_bot._index_release_group_directory",
                   return_value=self.DIRECTORY), \
             patch("listenbrainz_bot._artist_index_rows", return_value=self.ROWS), \
             patch("listenbrainz_bot._index_indexed_artist_mbids", return_value=set()):
            out = bot._deezer_chart_marked(20)
        self.assertTrue(out["albums"][0]["releaseOwned"])
        self.assertEqual(out["albums"][0]["rgid"], "rg-owned")

    def test_artists_carry_the_owned_indexed_pair_the_similar_row_uses(self):
        by_name = {a["name"]: a for a in self._marked()["artists"]}
        self.assertTrue(by_name["Band"]["owned"])
        self.assertTrue(by_name["Band"]["indexed"])
        self.assertEqual(by_name["Band"]["artistId"], "nd1")
        self.assertEqual(by_name["Band"]["mbid"], "mb-band")
        self.assertFalse(by_name["Stranger"]["owned"])
        self.assertEqual(by_name["Stranger"]["artistId"], "")
        self.assertEqual(by_name["Stranger"]["mbid"], "")

    def test_a_cold_index_degrades_to_no_badges_rather_than_raising(self):
        with patch("listenbrainz_bot.deezer_chart", return_value=self.CHART), \
             patch("listenbrainz_bot._index_release_group_directory", return_value={}), \
             patch("listenbrainz_bot._artist_index_rows",
                   side_effect=RuntimeError("no such column: artist_mbid")), \
             patch("listenbrainz_bot._index_indexed_artist_mbids", return_value=set()):
            out = bot._deezer_chart_marked(20)
        self.assertEqual(len(out["albums"]), 3)
        self.assertEqual(len(out["artists"]), 2)
        self.assertTrue(all(not a["releaseOwned"] for a in out["albums"]))
        self.assertTrue(all(not a["owned"] for a in out["artists"]))

    def test_it_never_spends_the_musicbrainz_budget(self):
        called = []
        with self._stack(), \
             patch("listenbrainz_bot.mbz_get",
                   side_effect=lambda *a, **k: called.append(a) or {}):
            bot._deezer_chart_marked(20)
        self.assertEqual(called, [], "a browse row must not touch MusicBrainz")

    def test_the_editorial_row_marks_the_same_way(self):
        editorial = {"albums": self.CHART["albums"], "section": "selection"}
        called = []
        with patch("listenbrainz_bot.deezer_editorial", return_value=editorial), \
             patch("listenbrainz_bot._index_release_group_directory",
                   return_value=self.DIRECTORY), \
             patch("listenbrainz_bot.mbz_get",
                   side_effect=lambda *a, **k: called.append(a) or {}):
            out = bot._deezer_editorial_marked(20)
        self.assertEqual(out["section"], "selection")
        self.assertTrue(out["albums"][0]["releaseOwned"])
        self.assertEqual(called, [])

    def test_the_related_row_resolves_the_deezer_id_from_the_name(self):
        seen = {}

        def fake_search(name):
            seen["name"] = name
            return "dz-99"

        with patch("listenbrainz_bot.deezer_search_artist_id", side_effect=fake_search), \
             patch("listenbrainz_bot.deezer_related_artists",
                   return_value=[{"deezerId": "10", "name": "Band", "imageUrl": ""},
                                 {"deezerId": "11", "name": "Stranger", "imageUrl": ""}]), \
             patch("listenbrainz_bot._artist_index_rows", return_value=self.ROWS), \
             patch("listenbrainz_bot._index_indexed_artist_mbids", return_value={"mb-band"}):
            out = bot._deezer_related_marked("", "Seed Artist", 20)
        self.assertEqual(seen["name"], "Seed Artist")
        self.assertEqual(out["because"], "Seed Artist")
        self.assertEqual(out["sources"], ["Deezer"])
        by_name = {a["name"]: a for a in out["artists"]}
        self.assertTrue(by_name["Band"]["owned"])
        self.assertFalse(by_name["Stranger"]["owned"])
        # Deezer publishes no score, so rank is normalized into one — the same
        # treatment `similar_artists` gives its two sources.
        self.assertGreater(by_name["Band"]["score"], by_name["Stranger"]["score"])

    def test_the_related_row_finds_the_name_from_an_mbid_without_musicbrainz(self):
        called = []
        with patch("listenbrainz_bot.deezer_search_artist_id", return_value="") as search, \
             patch("listenbrainz_bot._artist_index_rows", return_value=self.ROWS), \
             patch("listenbrainz_bot._index_indexed_artist_mbids", return_value=set()), \
             patch("listenbrainz_bot.mbz_get",
                   side_effect=lambda *a, **k: called.append(a) or {}):
            out = bot._deezer_related_marked("mb-band", "", 20)
        search.assert_called_once_with("Band")
        self.assertEqual(out["artists"], [])
        self.assertEqual(called, [])

    def test_an_artist_deezer_does_not_know_yields_an_empty_row_not_a_wrong_one(self):
        """`deezer_search_artist_id` is exact-match only on purpose: Deezer's
        search happily answers a tribute band for a misspelling, and a near miss
        is a whole shelf about the wrong artist."""
        with patch("listenbrainz_bot.deezer_search_artist_id", return_value=""), \
             patch("listenbrainz_bot.deezer_related_artists") as related, \
             patch("listenbrainz_bot._artist_index_rows", return_value=self.ROWS), \
             patch("listenbrainz_bot._index_indexed_artist_mbids", return_value=set()):
            out = bot._deezer_related_marked("", "Nobody At All", 20)
        related.assert_not_called()
        self.assertEqual(out["artists"], [])


class ReleaseGroupDirectoryTests(unittest.TestCase):
    """`_index_release_group_directory` against a real index DB, not a mock.

    This is the map that lets a browse source with **no MBIDs at all** mark
    ownership without a MusicBrainz request. `_index_owned_rgids` and
    `_index_rgid_album_ids` both start from an rgid the caller already has; a
    Deezer chart row has only "Artist" and "Title", so it needs the index keyed
    the other way round — which means a real JOIN against `artists`, and a
    mocked test would not have exercised it.
    """

    def _seed(self):
        bot._index_ensure_artist("mb-band", artist_mbid="mb-band",
                                 nd_artist_id="nd1", name="The Band")
        bot._index_upsert_release("mb-band", {
            "rgid": "rg-owned", "title": "Owned Record", "status": "complete",
            "navidrome_album_ids": ["nd-42", "nd-43"]})
        bot._index_upsert_release("mb-band", {
            "rgid": "rg-missing", "title": "Known But Missing", "status": "missing",
            "navidrome_album_ids": []})

    def test_a_row_is_found_by_its_artist_and_title(self):
        with isolated_review():
            self._seed()
            directory = bot._index_release_group_directory()
        entry = directory["the band|owned record"]
        self.assertEqual(entry["rgid"], "rg-owned")
        self.assertEqual(entry["status"], "complete")
        self.assertEqual(entry["artistMbid"], "mb-band")
        # First id wins, as `_index_rgid_album_ids` does: a release-group held
        # as several Navidrome albums has no one right answer.
        self.assertEqual(entry["albumId"], "nd-42")

    def test_a_missing_release_group_is_kept_and_keeps_its_rgid(self):
        """These are the most useful rows here: an unowned tile still gets a
        real release-group id, which is what the one-tap acquire needs."""
        with isolated_review():
            self._seed()
            directory = bot._index_release_group_directory()
        entry = directory["the band|known but missing"]
        self.assertEqual(entry["rgid"], "rg-missing")
        self.assertEqual(entry["status"], "missing")
        self.assertEqual(entry["albumId"], "")

    def test_an_edition_suffix_resolves_through_the_fuzzy_key(self):
        with isolated_review():
            self._seed()
            directory = bot._index_release_group_directory()
        self.assertIn("the band|owned record", directory)
        self.assertEqual(
            directory[f"{bot._fuzzy_album_text('The Band')}|"
                      f"{bot._fuzzy_album_text('Owned Record (Deluxe Edition)')}"]["rgid"],
            "rg-owned")

    def test_an_artist_with_no_row_contributes_nothing(self):
        """The JOIN is inner on purpose: a release-group whose artist row is
        gone has no name to key on, and inventing one would mis-mark."""
        with isolated_review():
            bot._index_upsert_release("mb-orphan", {
                "rgid": "rg-orphan", "title": "Orphan", "status": "complete"})
            self.assertEqual(bot._index_release_group_directory(), {})

    def test_a_broken_index_is_an_empty_map_not_a_raise(self):
        with patch("listenbrainz_bot._index_db",
                   side_effect=RuntimeError("no such table: release_groups")):
            self.assertEqual(bot._index_release_group_directory(), {})


class DeezerClientTests(unittest.TestCase):
    """The transport: cache keying, Deezer's in-body errors, and the editorial
    fallback."""

    def setUp(self):
        bot._deezer_cache.clear()
        self.addCleanup(bot._deezer_cache.clear)

    @staticmethod
    def _response(payload, ok=True, status=200):
        return types.SimpleNamespace(ok=ok, status_code=status, text=json.dumps(payload),
                                     json=lambda: payload)

    def test_a_second_call_is_served_from_the_cache(self):
        calls = []

        def fake_get(url, **kw):
            calls.append(url)
            return self._response({"data": [{"id": 1, "title": "T",
                                             "artist": {"name": "A"}}]})

        with patch.object(bot._http, "get", side_effect=fake_get):
            bot._deezer_get("editorial/0/selection", {"limit": "5"})
            bot._deezer_get("editorial/0/selection", {"limit": "5"})
        self.assertEqual(len(calls), 1)

    def test_an_error_inside_a_200_body_is_an_error(self):
        """Deezer reports quota and bad-id failures *inside* a 200. Reading only
        the status would cache "no rows" as the permanent answer."""
        with patch.object(bot._http, "get",
                          return_value=self._response({"error": {"message": "Quota limit exceeded"}})):
            with self.assertRaises(bot.DeezerError):
                bot._deezer_get("chart", {"limit": "5"})
        self.assertEqual(bot._deezer_cache, {}, "a failure must not be cached")

    def test_a_transport_failure_is_a_deezer_error_not_a_crash(self):
        """A read timeout escaped as a bare requests exception, which no caller
        catches — /api/artist/related answered 500 whenever Deezer was slow."""
        with patch.object(bot._http, "get", side_effect=TimeoutError("read timed out")):
            with self.assertRaises(bot.DeezerError):
                bot._deezer_get("search/artist", {"q": "Radiohead"})
        with patch.object(bot._http, "get", side_effect=TimeoutError("read timed out")):
            self.assertEqual(bot.deezer_search_artist_id("Radiohead"), "")

    def test_the_chart_keeps_only_what_ownership_can_be_marked_on(self):
        payload = {
            "albums": {"data": [{"id": 5, "title": "Rec", "artist": {"id": 7, "name": "A"},
                                 "cover_medium": "u", "record_type": "album"}]},
            "artists": {"data": [{"id": 7, "name": "A", "picture_medium": "p"}]},
            "tracks": {"data": [{"id": 9, "title": "Song"}]},
            "playlists": {"data": [{"id": 3, "title": "Mix"}]},
        }
        with patch.object(bot._http, "get", return_value=self._response(payload)):
            out = bot.deezer_chart(10)
        self.assertEqual(set(out), {"albums", "artists"})
        self.assertEqual(out["albums"][0]["deezerId"], "5")
        self.assertEqual(out["albums"][0]["artist"], "A")

    def test_editorial_falls_back_when_the_selection_is_empty(self):
        """An empty editorial row is indistinguishable from a broken one, so
        `selection` coming back empty falls through to `releases` and the
        answer says which one served it."""
        seen = []

        def fake_get(url, **kw):
            seen.append(url)
            if "selection" in url:
                return self._response({"data": []})
            return self._response({"data": [{"id": 1, "title": "New",
                                             "artist": {"name": "A"}}]})

        with patch.object(bot._http, "get", side_effect=fake_get):
            out = bot.deezer_editorial(10)
        self.assertEqual(out["section"], "releases")
        self.assertEqual(len(out["albums"]), 1)
        self.assertEqual(len(seen), 2)


class MusicLinkParsingTests(unittest.TestCase):
    """`_parse_music_link` — pure, so every provider form is testable without a
    single request. Before this the module's only URL parser was
    `spotify_playlist_id`, a `re.search` for `playlist/<id>`."""

    CASES = [
        ("https://musicbrainz.org/artist/f4a31f0a-51dd-4fa7-986d-3095c40c5ed9",
         "musicbrainz", "artist", "f4a31f0a-51dd-4fa7-986d-3095c40c5ed9"),
        ("https://musicbrainz.org/release-group/11111111-2222-3333-4444-555555555555",
         "musicbrainz", "album", "11111111-2222-3333-4444-555555555555"),
        ("https://musicbrainz.org/release/11111111-2222-3333-4444-555555555555",
         "musicbrainz", "release", "11111111-2222-3333-4444-555555555555"),
        ("https://musicbrainz.org/recording/11111111-2222-3333-4444-555555555555",
         "musicbrainz", "track", "11111111-2222-3333-4444-555555555555"),
        ("https://open.spotify.com/album/4m2880jivSbbyEGAKfITCa",
         "spotify", "album", "4m2880jivSbbyEGAKfITCa"),
        ("https://open.spotify.com/intl-de/track/2takcwOaAZWiXQijPHIx7B?si=abc",
         "spotify", "track", "2takcwOaAZWiXQijPHIx7B"),
        ("spotify:artist:4tZwfgrHOc3mvqYlEYSvVi",
         "spotify", "artist", "4tZwfgrHOc3mvqYlEYSvVi"),
        ("https://www.deezer.com/en/album/302127", "deezer", "album", "302127"),
        ("https://www.deezer.com/artist/27", "deezer", "artist", "27"),
        ("https://www.deezer.com/fr/track/3135556", "deezer", "track", "3135556"),
        ("https://music.apple.com/us/album/random-access-memories/617154241",
         "apple", "album", "617154241"),
        ("https://music.youtube.com/watch?v=dQw4w9WgXcQ",
         "ytmusic", "track", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "ytmusic", "track", "dQw4w9WgXcQ"),
        ("https://tidal.com/browse/album/77640617", "tidal", "album", "77640617"),
        ("https://open.qobuz.com/album/0060254712345", "qobuz", "album", "0060254712345"),
    ]

    def test_every_provider_form_parses(self):
        for url, provider, kind, ident in self.CASES:
            with self.subTest(url=url):
                parsed = bot._parse_music_link(url)
                self.assertEqual(parsed["provider"], provider)
                self.assertEqual(parsed["kind"], kind)
                self.assertEqual(parsed["id"], ident)

    def test_apple_keeps_the_slug_as_a_name_of_last_resort(self):
        parsed = bot._parse_music_link(
            "https://music.apple.com/us/album/random-access-memories/617154241")
        self.assertEqual(parsed["slug"], "random-access-memories")

    def test_something_that_is_not_a_music_link_is_unknown_not_an_error(self):
        for url in ("", "hello", "https://example.com/album/1",
                    "https://news.site/story/spotify-album-review"):
            with self.subTest(url=url):
                parsed = bot._parse_music_link(url)
                self.assertEqual(parsed["provider"], "")
                self.assertEqual(parsed["kind"], "unknown")

    def test_a_page_title_splits_into_artist_and_title(self):
        self.assertEqual(
            bot._split_artist_title("Random Access Memories by Daft Punk on Apple Music"),
            ("Daft Punk", "Random Access Memories"))
        self.assertEqual(bot._split_artist_title("Daft Punk - Discovery | TIDAL"),
                         ("Daft Punk", "Discovery"))
        self.assertEqual(bot._split_artist_title("Just A Title"), ("", "Just A Title"))


class ResolveLinkTests(unittest.TestCase):
    """`resolve_music_link` — the three tiers, and the promise that a link it
    cannot place is an answer rather than a 500."""

    def test_a_musicbrainz_url_answers_without_a_single_request(self):
        called = []
        with patch("listenbrainz_bot.mbz_get",
                   side_effect=lambda *a, **k: called.append(a) or {}), \
             patch.object(bot._http, "get",
                          side_effect=AssertionError("no HTTP for a MusicBrainz URL")):
            out = bot.resolve_music_link(
                "https://musicbrainz.org/release-group/11111111-2222-3333-4444-555555555555")
        self.assertEqual(out["kind"], "album")
        self.assertEqual(out["rgid"], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(out["confidence"], 1.0)
        self.assertEqual(called, [])

    def test_a_musicbrainz_release_url_resolves_up_to_its_release_group(self):
        """The acquire path is release-group-shaped, so a concrete release has
        to be lifted one level or the answer is unusable."""
        with patch("listenbrainz_bot.mbz_release_group_of", return_value="rg-77"):
            out = bot.resolve_music_link(
                "https://musicbrainz.org/release/11111111-2222-3333-4444-555555555555")
        self.assertEqual(out["kind"], "album")
        self.assertEqual(out["rgid"], "rg-77")
        self.assertEqual(out["mbid"], "11111111-2222-3333-4444-555555555555")

    def test_a_deezer_album_link_resolves_through_deezers_own_api(self):
        with patch("listenbrainz_bot.deezer_album",
                   return_value={"title": "Discovery", "artist": {"name": "Daft Punk"}}), \
             patch("listenbrainz_bot.mbz_search_release_groups",
                   return_value=[{"rgid": "rg-disc", "title": "Discovery",
                                  "artist": "Daft Punk", "primary_type": "album",
                                  "year": "2001", "score": 100}]):
            out = bot.resolve_music_link("https://www.deezer.com/en/album/302127")
        self.assertEqual(out["kind"], "album")
        self.assertEqual(out["provider"], "deezer")
        self.assertEqual(out["rgid"], "rg-disc")
        self.assertEqual(out["artist"], "Daft Punk")
        # Capped below 1.0: the names are exact but MusicBrainz's match is not.
        self.assertEqual(out["confidence"], 0.9)

    def test_a_scraped_link_is_capped_lower_than_an_api_one(self):
        """Apple, TIDAL, YouTube Music and Qobuz publish no free metadata API,
        so their artist and title come out of a page title that a redesign
        upstream breaks silently. `confidence` says so."""
        with patch("listenbrainz_bot._fetch_page_head",
                   return_value="<title>Discovery by Daft Punk on Apple Music</title>"), \
             patch("listenbrainz_bot.mbz_search_release_groups",
                   return_value=[{"rgid": "rg-disc", "title": "Discovery",
                                  "artist": "Daft Punk", "primary_type": "album",
                                  "year": "2001", "score": 100}]):
            out = bot.resolve_music_link(
                "https://music.apple.com/us/album/discovery/697194953")
        self.assertEqual(out["rgid"], "rg-disc")
        self.assertEqual(out["confidence"], 0.7)

    def test_an_isrc_beats_a_text_search_for_a_track(self):
        """An ISRC identifies the *recording*; a name is shared by every cover
        version ever recorded. So the ISRC leg runs first and scores higher."""
        searched = []
        with patch("listenbrainz_bot.deezer_track",
                   return_value={"title": "One More Time", "isrc": "GBDUW0000059",
                                 "artist": {"name": "Daft Punk"},
                                 "album": {"title": "Discovery"}}), \
             patch("listenbrainz_bot._mbid_from_isrc", return_value="rec-1"), \
             patch("listenbrainz_bot._mbid_from_search",
                   side_effect=lambda *a: searched.append(a) or "rec-wrong"), \
             patch("listenbrainz_bot.mbz_search_release_groups",
                   return_value=[{"rgid": "rg-disc", "title": "Discovery",
                                  "artist": "Daft Punk", "primary_type": "album",
                                  "year": "2001", "score": 100}]):
            out = bot.resolve_music_link("https://www.deezer.com/track/3135556")
        self.assertEqual(out["kind"], "track")
        self.assertEqual(out["mbid"], "rec-1")
        self.assertEqual(out["rgid"], "rg-disc")
        self.assertEqual(out["confidence"], 0.95)
        self.assertEqual(searched, [], "the text search is the fallback, not the first try")

    def test_a_provider_being_down_falls_back_to_the_page_rather_than_raising(self):
        with patch("listenbrainz_bot.deezer_album",
                   side_effect=bot.DeezerError("503")), \
             patch("listenbrainz_bot._fetch_page_head",
                   return_value="<title>Discovery by Daft Punk</title>"), \
             patch("listenbrainz_bot.mbz_search_release_groups", return_value=[]):
            out = bot.resolve_music_link("https://www.deezer.com/en/album/302127")
        self.assertEqual(out["kind"], "album")
        self.assertEqual(out["rgid"], "")
        self.assertEqual(out["confidence"], 0.0)
        self.assertTrue(out["reason"])

    def test_a_failed_isrc_lookup_does_not_borrow_its_confidence(self):
        """The ISRC merely *existing* says nothing. Caught live on 2026-09-23:
        MusicBrainz 400'd the ISRC leg, the text search supplied the recording,
        and the answer still claimed 0.95 — an exact-match score for a name
        match."""
        with patch("listenbrainz_bot.deezer_track",
                   return_value={"title": "One More Time", "isrc": "GBDUW0000059",
                                 "artist": {"name": "Daft Punk"},
                                 "album": {"title": "Discovery"}}), \
             patch("listenbrainz_bot._mbid_from_isrc", return_value=""), \
             patch("listenbrainz_bot._mbid_from_search", return_value="rec-2"), \
             patch("listenbrainz_bot.mbz_search_release_groups",
                   return_value=[{"rgid": "rg-disc", "title": "Discovery",
                                  "artist": "Daft Punk", "primary_type": "album",
                                  "year": "2001", "score": 100}]):
            out = bot.resolve_music_link("https://www.deezer.com/track/3135556")
        self.assertEqual(out["mbid"], "rec-2")
        self.assertLess(out["confidence"], 0.95)

    def test_mbid_from_isrc_sends_no_empty_inc_param(self):
        """B-010: an empty `inc=""` reached MusicBrainz verbatim -- harmless,
        but not what the route meant to ask, and the caller reads only the
        recording id, so there is nothing worth including."""
        with patch("listenbrainz_bot.mbz_get",
                   return_value={"recordings": [{"id": "rec-1"}]}) as mock_get:
            mbid = bot._mbid_from_isrc("GBDUW0000059")
        self.assertEqual(mbid, "rec-1")
        mock_get.assert_called_once_with("recording", {"isrcs": "GBDUW0000059"})

    def test_an_unrecognised_url_is_a_plain_answer(self):
        out = bot.resolve_music_link("https://example.com/not-music")
        self.assertEqual(out["kind"], "unknown")
        self.assertEqual(out["confidence"], 0.0)
        self.assertEqual(out["provider"], "")

    def test_the_answer_always_carries_the_frozen_contract_keys(self):
        """Four agents share this shape and only this shape; a branch that
        omits a key makes a client's optional-chaining silently render blank."""
        with patch("listenbrainz_bot.mbz_release_group_of", return_value=""):
            answers = [
                bot.resolve_music_link("nonsense"),
                bot.resolve_music_link(
                    "https://musicbrainz.org/artist/11111111-2222-3333-4444-555555555555"),
                bot.resolve_music_link(
                    "https://musicbrainz.org/release/11111111-2222-3333-4444-555555555555"),
            ]
        for answer in answers:
            for key in ("kind", "mbid", "rgid", "artist", "title", "provider",
                        "confidence"):
                self.assertIn(key, answer)
            self.assertIn(answer["kind"], ("artist", "album", "track", "unknown"))


class ReleaseGroupSearchTests(unittest.TestCase):
    """`_mbz_release_group_for` — fielded, because free text ranks the wrong record.

    Measured against live MusicBrainz on 2026-09-23: the free-text query
    "Daft Punk Discovery" scored *"Daft Punk's Discovery but it's in the SM64
    Soundfont"* by Pignickel at 100 and returned it first, because term density
    beats the record you meant. When the caller already knows which half is the
    artist — which every `resolve-link` caller does — the artist belongs in a
    field, as a constraint, not in the bag of words.
    """

    NOVELTY = {"rgid": "rg-novelty",
               "title": "Daft Punk\u2019s Discovery but it\u2019s in the SM64 Soundfont",
               "artist": "Pignickel", "primary_type": "album",
               "year": "2021", "score": 100}
    REAL = {"rgid": "rg-disc", "title": "Discovery", "artist": "Daft Punk",
            "primary_type": "album", "year": "2001", "score": 92}

    def test_the_artist_becomes_a_field_not_another_search_term(self):
        queries = []
        with patch("listenbrainz_bot.mbz_search_release_groups",
                   side_effect=lambda q, n: queries.append(q) or [self.REAL]):
            bot._mbz_release_group_for("Daft Punk", "Discovery")
        self.assertEqual(len(queries), 1)
        self.assertIn('releasegroup:"Discovery"', queries[0])
        self.assertIn('artist:"Daft Punk"', queries[0])

    def test_free_text_is_the_fallback_when_the_fielded_query_finds_nothing(self):
        """A fielded query finds nothing at all when the store's spelling of the
        artist and MusicBrainz's disagree, so free text has to stay reachable."""
        queries = []

        def fake(query, limit):
            queries.append(query)
            return [] if "releasegroup:" in query else [self.REAL]

        with patch("listenbrainz_bot.mbz_search_release_groups", side_effect=fake):
            out = bot._mbz_release_group_for("Daft Punk", "Discovery")
        self.assertEqual(len(queries), 2)
        self.assertEqual(out[0]["rgid"], "rg-disc")

    def test_the_record_that_matches_outranks_the_higher_scoring_novelty(self):
        """MusicBrainz's own `score` cannot be trusted to do this — which is the
        whole reason this function exists rather than a bare search call."""
        with patch("listenbrainz_bot.mbz_search_release_groups",
                   return_value=[self.NOVELTY, self.REAL]):
            out = bot._mbz_release_group_for("Daft Punk", "Discovery")
        self.assertEqual(out[0]["rgid"], "rg-disc")
        self.assertEqual(out[1]["rgid"], "rg-novelty")

    def test_a_title_with_a_quote_in_it_does_not_break_the_query(self):
        queries = []
        with patch("listenbrainz_bot.mbz_search_release_groups",
                   side_effect=lambda q, n: queries.append(q) or []):
            bot._mbz_release_group_for('The "Best" Of', 'Say "Hello"')
        self.assertNotIn('"Say "Hello""', queries[0])

    def test_no_title_is_no_search(self):
        with patch("listenbrainz_bot.mbz_search_release_groups") as search:
            self.assertEqual(bot._mbz_release_group_for("Daft Punk", ""), [])
        search.assert_not_called()


class WishlistTests(unittest.TestCase):
    """The home `no_source` gets instead of an auto-retry.

    `no_source` deliberately never auto-retries — lb-bot walks its entire ranked
    source list before reporting it, so re-running the same search against the
    same peers is the same failure. But "don't retry now" was argued for and
    "forget about it" never was: Soulseek's population turns over on the scale
    of days, so the retry worth running is a slow one against a list the user
    curates.
    """

    def test_adding_is_idempotent_and_does_not_reset_the_clock(self):
        """A double-tap must not re-search immediately. Re-adding something
        already listed is not new information about who is sharing it."""
        with isolated_review():
            bot._wishlist_add("rg-1", "Band", "Record")
            bot._wishlist_mark_tried("rg-1", "nobody sharing")
            before = bot._wishlist_list()[0]
            result = bot._wishlist_add("rg-1", "Band", "Record")
            rows = bot._wishlist_list()
            after = rows[0]
        self.assertFalse(result["added"])
        self.assertEqual(len(rows), 1, "re-adding must not make a second row")
        self.assertEqual(after["lastTriedAt"], before["lastTriedAt"])
        self.assertEqual(after["attempts"], 1)

    def test_re_adding_fills_in_names_it_was_missing(self):
        with isolated_review():
            bot._wishlist_add("rg-1")
            bot._wishlist_add("rg-1", "Band", "Record")
            row = bot._wishlist_list()[0]
            self.assertEqual(row["artist"], "Band")
            self.assertEqual(row["title"], "Record")
            # …and an add with no names does not blank the ones it has.
            bot._wishlist_add("rg-1")
            self.assertEqual(bot._wishlist_list()[0]["artist"], "Band")

    def test_removing_reports_whether_there_was_anything_to_remove(self):
        with isolated_review():
            bot._wishlist_add("rg-1", "Band", "Record")
            self.assertTrue(bot._wishlist_remove("rg-1")["removed"])
            self.assertFalse(bot._wishlist_remove("rg-1")["removed"])
            self.assertEqual(bot._wishlist_list(), [])

    def test_an_rgid_is_required(self):
        with isolated_review():
            self.assertFalse(bot._wishlist_add("")["ok"])
            self.assertFalse(bot._wishlist_remove("")["ok"])

    def test_the_list_is_capped_and_evicts_the_stalest_claim(self):
        with isolated_review(), \
             patch("listenbrainz_bot.WISHLIST_MAX", 3):
            for n in range(5):
                bot._wishlist_add(f"rg-{n}", "Band", f"Record {n}")
                time.sleep(0.001)
            rgids = [r["rgid"] for r in bot._wishlist_list()]
        self.assertEqual(len(rgids), 3)
        self.assertEqual(set(rgids), {"rg-4", "rg-3", "rg-2"})

    def test_only_rows_past_their_cooldown_are_due(self):
        """Without a per-row cooldown, adding one row would re-search every row."""
        with isolated_review():
            bot._wishlist_add("rg-cold", "Band", "Cold")
            bot._wishlist_add("rg-hot", "Band", "Hot")
            bot._wishlist_mark_tried("rg-hot", "just tried")
            due = {r["rgid"] for r in bot._wishlist_due()}
        self.assertIn("rg-cold", due)
        self.assertNotIn("rg-hot", due)

    def test_a_landing_removes_the_row_and_tells_the_hub(self):
        """A client showing the wishlist has no other way to learn that what it
        is displaying is now in the library. It is a `fill` frame, not a second
        `albumPlaced`: the landing was already announced, and a library notify
        makes every client refetch its whole album list."""
        pushed = []
        with isolated_review(), \
             patch("listenbrainz_bot._notify_hub_library_change",
                   side_effect=lambda *a, **k: self.fail("no library notify for a wishlist row")), \
             patch("listenbrainz_bot._push_enqueue",
                   side_effect=lambda kind, key, payload=None: pushed.append((kind, key, payload))):
            bot._wishlist_add("rg-1", "Band", "Record")
            bot._wishlist_landed("rg-1", "Band", "Record")
            self.assertEqual(bot._wishlist_list(), [])
        self.assertEqual(len(pushed), 1)
        self.assertEqual(pushed[0][:2], ("wishlist", "rg-1"))
        self.assertEqual(pushed[0][2]["state"], "landed")

    def test_a_landing_for_something_never_wished_for_says_nothing(self):
        pushed = []
        with isolated_review(), \
             patch("listenbrainz_bot._push_enqueue",
                   side_effect=lambda *a, **k: pushed.append(a)):
            bot._wishlist_landed("rg-never", "Band", "Record")
        self.assertEqual(pushed, [])

    def test_the_re_search_outcome_is_recorded_on_the_row(self):
        """`_wishlist_retry_one` wrote "re-searching" and nothing ever replaced
        it, so the row said that forever."""
        with isolated_review(), \
             patch("listenbrainz_bot._save_state", lambda *a, **k: None), \
             patch("listenbrainz_bot._schedule_album_fill_retry", lambda *a, **k: None):
            bot._wishlist_add("rg-1", "Band", "Record")
            bot._wishlist_mark_tried("rg-1", "re-searching")
            bot._album_fill_status.pop("rel-w", None)
            bot._album_fill_set("rel-w", "searching", rgid="rg-1", artist="Band", album="Record")
            bot._album_fill_fail("rel-w", "no_source", "Nobody is sharing it today")
            row = bot._wishlist_list()[0]
            bot._album_fill_status.pop("rel-w", None)
        self.assertEqual(row["lastReason"], "Nobody is sharing it today")

    def test_a_retry_does_not_start_a_second_fill_for_the_same_release(self):
        """The same guard `_schedule_album_fill_retry` keeps: if anything is
        already filling this release, that is the answer."""
        started = []
        with isolated_review(), \
             patch("listenbrainz_bot.mbz_resolve_album",
                   return_value={"release_mbid": "rel-1", "artist": "Band",
                                 "title": "Record", "total_tracks": 10}), \
             patch("listenbrainz_bot._album_group_for_release",
                   return_value=("ag-live", {})), \
             patch("listenbrainz_bot._task_run",
                   side_effect=lambda *a, **k: started.append(a)):
            bot._wishlist_add("rg-1", "Band", "Record")
            self.assertFalse(bot._wishlist_retry_one(bot._wishlist_list()[0]))
        self.assertEqual(started, [])

    def test_a_retry_records_why_it_could_not_run(self):
        with isolated_review(), \
             patch("listenbrainz_bot.mbz_resolve_album",
                   side_effect=RuntimeError("503 Service Unavailable")):
            bot._wishlist_add("rg-1", "Band", "Record")
            self.assertFalse(bot._wishlist_retry_one(bot._wishlist_list()[0]))
            row = bot._wishlist_list()[0]
        self.assertEqual(row["attempts"], 1)
        self.assertIn("MusicBrainz unavailable", row["lastReason"])


class SourceFailoverTests(unittest.TestCase):
    """The ranked-list walk, shared by `/api/gaps/<id>/fetch` and `_gap_auto_task`.

    Until now the fetch route enqueued once and, on refusal, handed the client
    the next candidate to click. But a refusal is the *normal* answer from a
    peer whose free-slot flag went stale in the seconds since the search, so the
    common case was a user clicking through four sources by hand to reach the
    one that would have worked. One definition of the order, because the two
    entry points disagreeing about it is how "auto picked a source manual
    wouldn't" happens.
    """

    def test_the_chosen_source_is_tried_first(self):
        self.assertEqual(bot._source_failover_order(5, 2)[0], 2)

    def test_the_rest_follow_in_rank_order(self):
        self.assertEqual(bot._source_failover_order(5, 2), [2, 0, 1, 3, 4])

    def test_the_walk_is_bounded(self):
        """By the seventh rejection the ranking itself is stale and a fresh
        search is the better answer than an eighth peer from it."""
        order = bot._source_failover_order(50, 0)
        self.assertEqual(len(order), bot.SOURCE_FAILOVER_MAX)
        self.assertEqual(order, list(range(bot.SOURCE_FAILOVER_MAX)))

    def test_no_sources_is_an_empty_walk_not_an_index_error(self):
        self.assertEqual(bot._source_failover_order(0, 0), [])

    def test_an_out_of_range_start_is_clamped_rather_than_dropped(self):
        self.assertEqual(bot._source_failover_order(3, 99)[0], 2)
        self.assertEqual(sorted(bot._source_failover_order(3, -5)), [0, 1, 2])

    def test_unverified_sources_are_skipped_in_the_auto_walk(self):
        """The failover walk is unattended, so it obeys the same rule as the
        first pick: never guess at a folder with no artist evidence."""
        folders = [{"artist_verified": False}, {"artist_verified": True}, {}]
        self.assertEqual(
            bot._source_failover_order(3, 0, folders=folders), [1, 2])

    def test_an_explicit_pick_of_an_unverified_source_is_consent(self):
        """A user who taps an unverified source has looked at it — the walk
        starts there, but does not extend the consent to other unverified
        ones."""
        folders = [{"artist_verified": False}, {"artist_verified": False},
                   {"artist_verified": True}]
        self.assertEqual(
            bot._source_failover_order(3, 0, folders=folders,
                                       start_is_choice=True), [0, 2])


class AcoustIdVerificationTests(unittest.TestCase):
    """The one identity check the rest of the module cannot make.

    `_audio_signature`'s md5 is FLAC StreamInfo's hash of the *unencoded* audio:
    it proves two files are the same encode of the same master and says nothing
    at all about a different *recording*. A live take, a radio edit, a cover or
    simply the wrong track under the right filename all have perfectly good,
    perfectly different md5s and sail through. Closing that gap is what a
    fingerprint is for — and what these defend is that it only ever speaks when
    it has something positive to say.
    """

    def test_no_key_configured_means_no_opinion(self):
        with patch("listenbrainz_bot.ACOUSTID_API_KEY", ""):
            self.assertEqual(bot._acoustid_contradiction("/x.flac", "rec-1"), "")

    def test_a_slot_with_no_recording_mbid_has_nothing_to_contradict(self):
        """An untagged release, or a MusicBrainz entry without a recording id,
        is not evidence that anything is wrong."""
        called = []
        with patch("listenbrainz_bot.ACOUSTID_API_KEY", "key"), \
             patch("listenbrainz_bot._acoustid_recording_mbids",
                   side_effect=lambda p: called.append(p) or (set(), 0.0)):
            self.assertEqual(bot._acoustid_contradiction("/x.flac", ""), "")
        self.assertEqual(called, [], "and it must not spend a lookup finding out")

    def test_an_unknown_fingerprint_is_not_evidence(self):
        """A guard that turns a working fill into a no-op because AcoustID has
        never seen a rare pressing is worse than no guard."""
        with patch("listenbrainz_bot.ACOUSTID_API_KEY", "key"), \
             patch("listenbrainz_bot._acoustid_recording_mbids",
                   return_value=(set(), 0.0)):
            self.assertEqual(bot._acoustid_contradiction("/x.flac", "rec-1"), "")

    def test_the_expected_recording_among_the_matches_is_no_objection(self):
        with patch("listenbrainz_bot.ACOUSTID_API_KEY", "key"), \
             patch("listenbrainz_bot._acoustid_recording_mbids",
                   return_value=({"rec-1", "rec-2"}, 0.97)):
            self.assertEqual(bot._acoustid_contradiction("/x.flac", "rec-1"), "")

    def test_a_confident_different_recording_is_refused(self):
        with patch("listenbrainz_bot.ACOUSTID_API_KEY", "key"), \
             patch("listenbrainz_bot._acoustid_recording_mbids",
                   return_value=({"rec-other"}, 0.95)):
            reason = bot._acoustid_contradiction("/x.flac", "rec-1")
        self.assertIn("different recording", reason)
        self.assertIn("rec-1", reason)

    def test_a_weak_match_is_discarded_before_it_can_reject_anything(self):
        """Below `ACOUSTID_MIN_SCORE` the answer is treated as no opinion, not
        as evidence against the file."""
        payload = {"status": "ok", "results": [
            {"score": 0.2, "recordings": [{"id": "rec-other"}]}]}
        fake = types.SimpleNamespace(
            fingerprint_file=lambda p: (200, b"fp"),
            lookup=lambda *a, **k: payload)
        with patch("listenbrainz_bot.ACOUSTID_API_KEY", "key"), \
             patch.dict(sys.modules, {"acoustid": fake}):
            mbids, score = bot._acoustid_recording_mbids("/x.flac")
        self.assertEqual(mbids, set())
        self.assertEqual(score, 0.0)

    def test_a_missing_fpcalc_is_no_opinion_rather_than_an_exception(self):
        def boom(_path):
            raise OSError("fpcalc not found")

        fake = types.SimpleNamespace(fingerprint_file=boom,
                                     lookup=lambda *a, **k: {})
        with patch("listenbrainz_bot.ACOUSTID_API_KEY", "key"), \
             patch.dict(sys.modules, {"acoustid": fake}):
            self.assertEqual(bot._acoustid_recording_mbids("/x.flac"), (set(), 0.0))

    def test_a_rejection_is_remembered_against_the_peer_not_the_path(self):
        """The file is about to be deleted or moved; what must not happen again
        is fetching that same file from that same peer on the next attempt,
        which is exactly what the ranked source list would otherwise do."""
        with isolated_review():
            bot._remember_download_origin("/downloads/x.flac", "peer1", "music\\x.flac")
            self.assertTrue(bot._reject_source_file("/downloads/x.flac", "wrong take",
                                                    expected="rec-1"))
            self.assertTrue(bot._source_is_rejected("peer1", "music\\x.flac"))
            self.assertFalse(bot._source_is_rejected("peer2", "music\\x.flac"))
            self.assertFalse(bot._source_is_rejected("peer1", "music\\other.flac"))

    def test_a_file_whose_provenance_was_lost_still_refuses_this_placement(self):
        """In-memory provenance means a restart mid-cycle costs the peer memory.
        The worst outcome is one more chance for one bad peer — never a wrong
        file placed."""
        with isolated_review():
            self.assertFalse(bot._reject_source_file("/downloads/unknown.flac", "wrong"))

    def test_a_broken_index_never_blocks_acquisition(self):
        with patch("listenbrainz_bot._index_db",
                   side_effect=RuntimeError("attempt to write a readonly database")):
            self.assertFalse(bot._source_is_rejected("peer1", "x.flac"))


class IndexSeqTests(unittest.TestCase):
    """The change sequence the index mirror syncs on: SQLite triggers stamp
    `artists.seq` from one `index_meta` counter on every write, deletions leave
    tombstones, and an epoch plus an on-disk high-water mark make a DB that went
    backwards visible to clients. Every test runs against a real temp SQLite DB
    so the triggers themselves are what is under test."""

    def setUp(self):
        import shutil
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, True)
        self.db_path = os.path.join(td, "index.db")
        scratch_index(self, self.db_path)

    # -- helpers -------------------------------------------------------------

    def seq_of(self, key):
        row = bot._index_db().execute(
            "SELECT seq FROM artists WHERE artist_key = ?", (key,)).fetchone()
        return None if row is None else row["seq"]

    def tombstones(self):
        return {r["artist_key"]: r["seq"] for r in bot._index_db().execute(
            "SELECT artist_key, seq FROM index_tombstones")}

    def head(self):
        return bot._index_head()[0]

    def reboot(self):
        """Close the shared connection so the next _index_db() is a fresh boot."""
        swap_index(bot.LIBRARY_INDEX_FILE)

    @staticmethod
    def rel(rgid, title="T", status="missing", **extra):
        return {"rgid": rgid, "title": title, "status": status, **extra}

    def store(self, mbid, name, releases, nd_id=""):
        bot._index_store_artist(
            {"artist_mbid": mbid, "artist_name": name, "releases": releases}, nd_id)

    def assert_values_unique(self):
        """Every seq value belongs to exactly one key — artists and tombstones
        together — which is what lets a page boundary never split one."""
        conn = bot._index_db()
        vals = [r[0] for r in conn.execute(
            "SELECT seq FROM artists UNION ALL SELECT seq FROM index_tombstones")]
        self.assertEqual(len(vals), len(set(vals)), vals)

    # -- constants and the head ----------------------------------------------

    def test_wire_version_and_epoch_shape(self):
        self.assertEqual(bot.WIRE_VERSION, 1)
        seq, epoch = bot._index_head()
        self.assertEqual(seq, 0)
        prefix, _, rand = epoch.partition("-")
        self.assertEqual(prefix, str(bot.WIRE_VERSION))
        self.assertEqual(len(rand), 16)
        int(rand, 16)
        # The row is created once, not re-minted on every open.
        self.reboot()
        self.assertEqual(bot._index_head()[1], epoch)

    def test_a_stored_epoch_from_another_wire_version_is_reminted(self):
        bot._index_db().execute("UPDATE index_meta SET epoch = '0-deadbeef' WHERE id = 0")
        bot._index_db().commit()
        self.reboot()
        epoch = bot._index_head()[1]
        self.assertNotEqual(epoch, "0-deadbeef")
        self.assertTrue(epoch.startswith(f"{bot.WIRE_VERSION}-"))

    # -- every writer bumps seq ----------------------------------------------

    def test_store_artist_stamps_the_artist_and_a_rescan_moves_it_forward(self):
        self.store("mb-a", "A", [self.rel("rg1"), self.rel("rg2")])
        first = self.seq_of("mb-a")
        self.assertGreater(first, 0)
        self.assertEqual(first, self.head())
        # A rescan of an existing artist goes through INSERT OR REPLACE: the
        # hidden delete must not leave a tombstone, and the seq must move.
        self.store("mb-a", "A", [self.rel("rg1"), self.rel("rg3")])
        self.assertGreater(self.seq_of("mb-a"), first)
        self.assertEqual(self.tombstones(), {})
        self.assert_values_unique()

    def test_backfill_present_album_ids_bumps_seq(self):
        self.store("mb-a", "A", [self.rel("rg1", "Album One")])
        self.assertEqual(bot._index_mark_release_present(rgid="rg1"), 1)
        before = self.seq_of("mb-a")
        albums = [{"id": "al-1", "name": "Album One", "artist": "A", "artistId": ""}]
        with patch("listenbrainz_bot._nd_album_index", return_value=albums):
            self.assertEqual(bot._index_backfill_present_album_ids("mb-a"), 1)
        self.assertGreater(self.seq_of("mb-a"), before)

    def test_mark_present_by_rgid_stamps_every_artist_the_release_spans(self):
        """A collaboration release sits under two artists and the writer
        updates by rgid without knowing either key — both must move, and to
        different values."""
        self.store("mb-a", "A", [self.rel("rg-collab")])
        self.store("mb-b", "B", [self.rel("rg-collab")])
        a0, b0 = self.seq_of("mb-a"), self.seq_of("mb-b")
        self.assertEqual(bot._index_mark_release_present(rgid="rg-collab"), 2)
        a1, b1 = self.seq_of("mb-a"), self.seq_of("mb-b")
        self.assertGreater(a1, a0)
        self.assertGreater(b1, b0)
        self.assertNotEqual(a1, b1)
        self.assert_values_unique()

    def test_mark_present_by_group_id_bumps_seq(self):
        self.store("mb-a", "A", [self.rel("rg1", group_id="g-1")])
        before = self.seq_of("mb-a")
        self.assertEqual(bot._index_mark_release_present(group_id="g-1"), 1)
        self.assertGreater(self.seq_of("mb-a"), before)

    def test_mark_present_insert_path_bumps_seq_and_never_leaves_an_orphan(self):
        self.store("mb-a", "A", [self.rel("rg1")])
        before = self.seq_of("mb-a")
        self.assertEqual(bot._index_mark_release_present(
            rgid="rg-new", artist_key="mb-a", title="New"), 1)
        self.assertGreater(self.seq_of("mb-a"), before)
        # A key with no `artists` row gets its parent in the same transaction.
        self.assertEqual(bot._index_mark_release_present(
            rgid="rg-x", artist_key="nd:77", title="X"), 1)
        row = bot._index_db().execute(
            "SELECT * FROM artists WHERE artist_key = 'nd:77'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual((row["artist_mbid"], row["nd_artist_id"], row["name"],
                          row["scanned_at"], row["scan_version"]), ("", "77", "", 0, 0))
        self.assertGreater(row["seq"], 0)
        orphans = bot._index_db().execute(
            "SELECT COUNT(*) FROM release_groups rg LEFT JOIN artists a "
            "ON a.artist_key = rg.artist_key WHERE a.artist_key IS NULL").fetchone()[0]
        self.assertEqual(orphans, 0)

    def test_set_release_album_ids_bumps_seq_by_rgid_and_by_group_id(self):
        self.store("mb-a", "A", [self.rel("rg1", group_id="g-1"), self.rel("rg2")])
        before = self.seq_of("mb-a")
        rows = bot._index_set_release_album_ids(rgid="rg2", album_ids=["al-2"])
        self.assertEqual(rows[0]["navidrome_album_ids"], ["al-2"])
        mid = self.seq_of("mb-a")
        self.assertGreater(mid, before)
        rows = bot._index_set_release_album_ids(group_id="g-1", album_ids=["al-1"])
        self.assertEqual(rows[0]["navidrome_album_ids"], ["al-1"])
        self.assertGreater(self.seq_of("mb-a"), mid)

    def test_upsert_release_bumps_seq_without_a_tombstone(self):
        self.store("mb-a", "A", [self.rel("rg1")])
        before = self.seq_of("mb-a")
        self.assertTrue(bot._index_upsert_release("mb-a", self.rel("rg1", status="complete")))
        mid = self.seq_of("mb-a")
        self.assertGreater(mid, before)
        self.assertTrue(bot._index_upsert_release("mb-a", self.rel("rg9")))
        self.assertGreater(self.seq_of("mb-a"), mid)
        self.assertEqual(self.tombstones(), {})

    def test_ensure_artist_stamps_a_new_artist_and_ignores_an_existing_one(self):
        bot._index_ensure_artist("mb-new", artist_mbid="mb-new", name="New")
        first = self.seq_of("mb-new")
        self.assertGreater(first, 0)
        # INSERT OR IGNORE on an existing row writes nothing, so nothing moves.
        bot._index_ensure_artist("mb-new", artist_mbid="mb-new", name="New")
        self.assertEqual(self.seq_of("mb-new"), first)

    def test_artist_metadata_update_bumps_seq_but_the_seq_write_itself_does_not(self):
        bot._index_ensure_artist("mb-a", artist_mbid="mb-a", name="A")
        conn = bot._index_db()
        head0 = self.head()
        conn.execute("UPDATE artists SET name = 'A2' WHERE artist_key = 'mb-a'")
        conn.commit()
        self.assertEqual(self.head(), head0 + 1)
        self.assertEqual(self.seq_of("mb-a"), head0 + 1)
        # A seq-only UPDATE is outside the trigger's column list: no loop.
        conn.execute("UPDATE artists SET seq = seq WHERE artist_key = 'mb-a'")
        conn.commit()
        self.assertEqual(self.head(), head0 + 1)

    def test_a_release_moved_between_artists_stamps_both(self):
        self.store("mb-a", "A", [self.rel("rg1")])
        self.store("mb-b", "B", [self.rel("rg2")])
        a0, b0 = self.seq_of("mb-a"), self.seq_of("mb-b")
        conn = bot._index_db()
        conn.execute("UPDATE release_groups SET artist_key = 'mb-b' WHERE rgid = 'rg1'")
        conn.commit()
        self.assertGreater(self.seq_of("mb-a"), a0)
        self.assertGreater(self.seq_of("mb-b"), b0)
        self.assert_values_unique()

    def test_a_release_row_with_no_parent_still_bumps_the_counter(self):
        head0 = self.head()
        conn = bot._index_db()
        conn.execute("INSERT INTO release_groups (artist_key, rgid) VALUES ('mb-ghost', 'rg')")
        conn.commit()
        self.assertEqual(self.head(), head0 + 1)

    # -- tombstones ----------------------------------------------------------

    def test_nd_to_mbid_swap_tombstones_the_nd_key_and_reinsert_clears_it(self):
        self.store("", "A", [self.rel("rg1")], nd_id="7")
        self.assertIsNotNone(self.seq_of("nd:7"))
        self.store("mb-a", "A", [self.rel("rg1")], nd_id="7")
        self.assertIsNone(self.seq_of("nd:7"))
        tomb = self.tombstones()
        self.assertIn("nd:7", tomb)
        self.assertGreater(tomb["nd:7"], 0)
        self.assert_values_unique()
        # The key coming back supersedes its own tombstone.
        bot._index_ensure_artist("nd:7", nd_artist_id="7", name="A")
        self.assertNotIn("nd:7", self.tombstones())
        self.assertGreater(self.seq_of("nd:7"), tomb["nd:7"])

    # -- acquisition safety --------------------------------------------------

    def test_acquisition_bookkeeping_still_changes_rows_with_triggers_installed(self):
        """Both writers swallow exceptions; a trigger that raised would lose the
        `present` mark silently and the album would show twice."""
        self.store("mb-a", "A", [self.rel("rg1", group_id="g-1"), self.rel("rg2")])
        triggers = {r[0] for r in bot._index_db().execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        self.assertTrue(triggers)
        self.assertEqual(bot._index_mark_release_present(rgid="rg1"), 1)
        self.assertEqual(bot._index_mark_release_present(group_id="g-1"), 0)  # already present
        self.assertEqual(bot._index_mark_release_present(rgid="rg2"), 1)
        status = {r["rgid"]: r["status"] for r in bot._index_db().execute(
            "SELECT rgid, status FROM release_groups")}
        self.assertEqual(status, {"rg1": "present", "rg2": "present"})
        rows = bot._index_set_release_album_ids(rgid="rg1", album_ids=["al-1"])
        self.assertEqual([r["navidrome_album_ids"] for r in rows], [["al-1"]])
        self.assertEqual(bot._index_owned_rgids(), {"rg1", "rg2"})

    # -- epoch and the high-water mark ---------------------------------------

    def hwm_path(self):
        return self.db_path + ".hwm.json"

    def test_persist_hwm_only_ever_moves_forward(self):
        seq, epoch = bot._index_head()
        bot._index_persist_hwm(10, epoch)
        with open(self.hwm_path()) as fh:
            self.assertEqual(json.load(fh), {"epoch": epoch, "hwm": 10})
        bot._index_persist_hwm(4, epoch)
        bot._index_persist_hwm(10, epoch)
        with open(self.hwm_path()) as fh:
            self.assertEqual(json.load(fh)["hwm"], 10)
        bot._index_persist_hwm(11, epoch)
        with open(self.hwm_path()) as fh:
            self.assertEqual(json.load(fh)["hwm"], 11)
        # A different epoch supersedes whatever the file held.
        bot._index_persist_hwm(2, "1-other")
        with open(self.hwm_path()) as fh:
            self.assertEqual(json.load(fh), {"epoch": "1-other", "hwm": 2})

    def test_head_behind_the_hwm_at_boot_mints_a_new_epoch(self):
        """A DB restored without its -wal, or one that lost the tail of its
        commits to synchronous=NORMAL, comes back with a lower seq under the
        same epoch. A client's cursor is then ahead of values that will be
        reissued for different changes — so the epoch must rotate."""
        self.store("mb-a", "A", [self.rel("rg1")])
        seq, epoch = bot._index_head()
        bot._index_persist_hwm(seq + 5, epoch)
        self.reboot()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            seq2, epoch2 = bot._index_head()
        self.assertIn("rotating the epoch", out.getvalue())
        self.assertEqual(seq2, seq)
        self.assertNotEqual(epoch2, epoch)
        self.assertTrue(epoch2.startswith(f"{bot.WIRE_VERSION}-"))
        with open(self.hwm_path()) as fh:
            self.assertEqual(json.load(fh), {"epoch": epoch2, "hwm": seq})

    def test_hwm_at_or_below_head_or_from_another_epoch_keeps_the_epoch(self):
        self.store("mb-a", "A", [self.rel("rg1")])
        seq, epoch = bot._index_head()
        bot._index_persist_hwm(seq, epoch)
        self.reboot()
        self.assertEqual(bot._index_head()[1], epoch)
        # A file left by a previous DB (another epoch) says nothing about this one.
        bot._index_persist_hwm(seq + 100, "1-previousdb")
        self.reboot()
        self.assertEqual(bot._index_head()[1], epoch)

    def test_an_unreadable_hwm_file_does_not_stop_the_index_opening(self):
        bot._index_head()
        with open(self.hwm_path(), "w") as fh:
            fh.write("{not json")
        self.reboot()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(bot._index_head()[0], 0)
        self.assertIn("unreadable high-water mark", out.getvalue())

    # -- migration -----------------------------------------------------------

    def test_migration_seeds_seq_in_artist_order_and_adopts_orphans(self):
        import sqlite3
        old = sqlite3.connect(self.db_path)
        old.executescript("""
            CREATE TABLE artists (
              artist_key TEXT PRIMARY KEY, artist_mbid TEXT NOT NULL DEFAULT '',
              nd_artist_id TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '',
              scanned_at REAL NOT NULL DEFAULT 0,
              scan_version INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE release_groups (
              artist_key TEXT NOT NULL, rgid TEXT NOT NULL,
              title TEXT NOT NULL DEFAULT '', primary_type TEXT NOT NULL DEFAULT '',
              year TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'missing',
              group_id TEXT NOT NULL DEFAULT '', present INTEGER NOT NULL DEFAULT 0,
              total INTEGER NOT NULL DEFAULT 0, nd_album_ids TEXT NOT NULL DEFAULT '[]',
              match_method TEXT NOT NULL DEFAULT '', match_score REAL NOT NULL DEFAULT 0,
              updated_at REAL NOT NULL DEFAULT 0, PRIMARY KEY (artist_key, rgid));
            INSERT INTO artists (artist_key, artist_mbid, name, scan_version)
              VALUES ('mb-c', 'mb-c', 'C', 2), ('mb-a', 'mb-a', 'A', 2);
            INSERT INTO release_groups (artist_key, rgid, status) VALUES
              ('mb-a', 'rg-a', 'complete'),
              ('mb-orphan', 'rg-o', 'present'),
              ('nd:55', 'rg-n', 'complete'),
              ('mb-c', 'rg-c', 'missing');
        """)
        old.commit()
        old.close()
        owned_before = {"rg-a", "rg-o", "rg-n"}
        swap_index(self.db_path)

        conn = bot._index_db()
        seqs = {r["artist_key"]: r["seq"] for r in conn.execute(
            "SELECT artist_key, seq FROM artists")}
        # Numbered 1..N in artist_key order, adopted stubs included.
        self.assertEqual(seqs, {"mb-a": 1, "mb-c": 2, "mb-orphan": 3, "nd:55": 4})
        self.assertEqual(bot._index_head()[0], 4)
        stub = conn.execute("SELECT * FROM artists WHERE artist_key = 'mb-orphan'").fetchone()
        self.assertEqual((stub["artist_mbid"], stub["nd_artist_id"], stub["name"],
                          stub["scanned_at"], stub["scan_version"]),
                         ("mb-orphan", "", "", 0, 0))
        stub = conn.execute("SELECT * FROM artists WHERE artist_key = 'nd:55'").fetchone()
        self.assertEqual((stub["artist_mbid"], stub["nd_artist_id"]), ("", "55"))
        # Pre-existing rows keep their scan_version: INDEX_SCAN_VERSION is not
        # what gates this migration, so no rescan is forced.
        self.assertEqual(conn.execute(
            "SELECT scan_version FROM artists WHERE artist_key = 'mb-a'").fetchone()[0], 2)
        self.assertEqual(bot.INDEX_SCAN_VERSION, 2)
        # Ownership answers exactly as before the adoption.
        self.assertEqual(bot._index_owned_rgids(), owned_before)
        self.assertEqual(self.tombstones(), {})
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertIn("idx_artists_seq", indexes)
        # Seeding is once: a second boot numbers nothing again.
        self.reboot()
        self.assertEqual(bot._index_head()[0], 4)
        self.assertEqual(self.seq_of("mb-a"), 1)


class IndexChangesFeedTests(unittest.TestCase):
    """`GET /api/index/changes`, `/api/index/keys`, `/api/health`
    (PLAN-lbbot-index-mirror-2026-09-23, contract §1a), exercised through the
    module-level view functions (`_index_changes_view` etc.) rather than
    Flask, since the routes live inside `start_web_dashboard()` and can't be
    unit-tested directly. Every test runs against a real temp SQLite DB."""

    def setUp(self):
        import shutil
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, True)
        self.db_path = os.path.join(td, "index.db")
        scratch_index(self, self.db_path)
        # B-013's truncation-warning dedup is process-global; a leftover entry
        # from one test's artist key/seq must not silence another's warning.
        old_warned = bot._index_truncation_warned.copy()
        bot._index_truncation_warned.clear()
        self.addCleanup(bot._index_truncation_warned.update, old_warned)

    @staticmethod
    def rel(rgid, title="T", status="missing", **extra):
        return {"rgid": rgid, "title": title, "status": status, **extra}

    def store(self, mbid, name, releases, nd_id=""):
        bot._index_store_artist(
            {"artist_mbid": mbid, "artist_name": name, "releases": releases}, nd_id)

    def head(self):
        return bot._index_head()

    def pull_all(self, since=0, epoch=""):
        """Every item across every page, plus the final page's envelope."""
        items, page = [], None
        while True:
            page, status = bot._index_changes_view(since, epoch)
            self.assertEqual(status, 200)
            self.assertNotIn("resync", page)
            items.extend(page["items"])
            since = page["nextSince"]
            if not page["more"]:
                break
        return items, page

    # -- normal paging ---------------------------------------------------

    def test_full_pull_returns_every_artist_since_zero(self):
        self.store("mb-a", "A", [self.rel("rg1"), self.rel("rg2")])
        self.store("mb-b", "B", [self.rel("rg3")])
        items, page = self.pull_all()
        self.assertEqual({i["key"] for i in items}, {"mb-a", "mb-b"})
        artist_a = next(i for i in items if i["key"] == "mb-a")
        self.assertEqual(artist_a["type"], "artist")
        self.assertEqual({r["rgid"] for r in artist_a["rows"]}, {"rg1", "rg2"})
        self.assertEqual(artist_a["mbid"], "mb-a")
        self.assertEqual(artist_a["name"], "A")
        self.assertIn("scannedAt", artist_a)
        self.assertIn("scanVersion", artist_a)
        self.assertIn("seq", artist_a)
        self.assertEqual(page["artistCount"], 2)
        self.assertEqual(page["scanVersion"], bot.INDEX_SCAN_VERSION)
        self.assertEqual(page["ttlDays"], bot.LB_BOT_INDEX_TTL_DAYS)

    def test_next_since_equals_head_seq_on_the_last_page(self):
        self.store("mb-a", "A", [self.rel("rg1")])
        page, status = bot._index_changes_view(0, "")
        self.assertEqual(status, 200)
        self.assertFalse(page["more"])
        self.assertEqual(page["nextSince"], page["headSeq"])
        self.assertEqual(page["nextSince"], self.head()[0])

    def test_since_filters_out_everything_already_seen(self):
        self.store("mb-a", "A", [self.rel("rg1")])
        cursor = self.head()[0]
        self.store("mb-b", "B", [self.rel("rg2")])
        page, _ = bot._index_changes_view(cursor, "")
        self.assertEqual({i["key"] for i in page["items"]}, {"mb-b"})

    def test_artist_count_and_seq_sum_match_a_full_pull(self):
        self.store("mb-a", "A", [self.rel("rg1")])
        self.store("mb-b", "B", [self.rel("rg2"), self.rel("rg3")])
        self.store("mb-c", "C", [])
        _, page = self.pull_all()
        with bot._index_lock:
            conn = bot._index_db()
            expected_count = conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0]
            expected_sum = conn.execute("SELECT COALESCE(SUM(seq),0) FROM artists").fetchone()[0]
        self.assertEqual(page["artistCount"], expected_count)
        self.assertEqual(page["seqSum"], expected_sum)

    def test_tombstones_appear_in_seq_order_alongside_artists(self):
        self.store("mb-a", "A", [self.rel("rg1")])
        self.store("", "B", [self.rel("rg2")], nd_id="7")   # artist_key nd:7
        self.store("mb-c", "C", [self.rel("rg3")])
        # The nd: -> mbid swap tombstones nd:7 and inserts mb-b under its mbid.
        self.store("mb-b", "B", [self.rel("rg2")], nd_id="7")
        items, _ = self.pull_all()
        seqs = [i["seq"] for i in items]
        self.assertEqual(seqs, sorted(seqs))
        tomb = [i for i in items if i["type"] == "tombstone"]
        self.assertEqual(len(tomb), 1)
        self.assertEqual(tomb[0]["key"], "nd:7")

    # -- byte cap ----------------------------------------------------------

    def test_byte_cap_still_returns_at_least_one_artist(self):
        big_releases = [self.rel(f"rg{i}", title="T" * 200) for i in range(50)]
        self.store("mb-big", "Big", big_releases)
        self.store("mb-small", "Small", [self.rel("rgx")])
        with patch.object(bot, "INDEX_CHANGES_MAX_BYTES", 10):
            page, status = bot._index_changes_view(0, "")
        self.assertEqual(status, 200)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["items"][0]["key"], "mb-big")
        self.assertTrue(page["more"])
        self.assertEqual(page["nextSince"], page["items"][0]["seq"])

    def test_an_oversized_first_artist_has_its_own_rows_bounded(self):
        """B-013: the "always at least one item" exemption used to be
        unconditional, so a single prolific artist's release-groups alone
        could make the page (and the hub's PROXY_MAX_RESPONSE above it)
        arbitrarily large -- a deterministic 502 tooLarge no retry could
        clear. The oversized artist must still be the page's one item, but
        its own rows are now bounded to the same page budget, with
        rowsTruncated/rowsTotal saying so rather than silently handing back
        a partial "every release_groups row"."""
        big_releases = [self.rel(f"rg{i}", title="T" * 200) for i in range(50)]
        self.store("mb-big", "Big", big_releases)
        self.store("mb-small", "Small", [self.rel("rgx")])
        with patch.object(bot, "INDEX_CHANGES_MAX_BYTES", 4000):
            page, status = bot._index_changes_view(0, "")
        self.assertEqual(status, 200)
        self.assertEqual(len(page["items"]), 1)
        item = page["items"][0]
        self.assertEqual(item["key"], "mb-big")
        self.assertTrue(item["rowsTruncated"])
        self.assertEqual(item["rowsTotal"], 50)
        self.assertLess(len(item["rows"]), 50)
        # The bound is real, not cosmetic: the artist alone must fit the cap.
        self.assertLessEqual(len(json.dumps(item).encode("utf-8")),
                             bot.INDEX_CHANGES_MAX_BYTES)
        self.assertTrue(page["more"])
        self.assertEqual(page["nextSince"], item["seq"])

    def test_oversized_first_artist_truncation_logs_a_warning(self):
        """R13: the truncation is silent otherwise -- nothing else flags a
        client's mirror missing rows for this artist (artistCount/seqSum
        still match). One WARNING line, naming the artist key/name and how
        many rows were kept out of the total."""
        big_releases = [self.rel(f"rg{i}", title="T" * 200) for i in range(50)]
        self.store("mb-big", "Big", big_releases)
        out = io.StringIO()
        with patch.object(bot, "INDEX_CHANGES_MAX_BYTES", 4000), \
                contextlib.redirect_stdout(out):
            page, status = bot._index_changes_view(0, "")
        self.assertEqual(status, 200)
        kept = len(page["items"][0]["rows"])
        logged = out.getvalue()
        self.assertIn("Warning", logged)
        self.assertIn("mb-big", logged)
        self.assertIn("Big", logged)
        self.assertIn(f"{kept}/50", logged)

    def test_truncation_warning_is_not_repeated_for_the_same_build(self):
        """A client that hasn't consumed this artist yet re-requests the same
        page on every poll; the warning must fire once per seq (per "build"
        of the item), not once per poll."""
        big_releases = [self.rel(f"rg{i}", title="T" * 200) for i in range(50)]
        self.store("mb-big", "Big", big_releases)
        with patch.object(bot, "INDEX_CHANGES_MAX_BYTES", 4000):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                bot._index_changes_view(0, "")
            self.assertIn("Warning", out.getvalue())

            # Same underlying row (same seq): re-polling from since=0 again
            # must not print a second warning.
            out2 = io.StringIO()
            with contextlib.redirect_stdout(out2):
                bot._index_changes_view(0, "")
            self.assertNotIn("Warning", out2.getvalue())

            # The artist changes (a rescan bumps its seq): the new version is
            # a different "build" and is warned about again.
            self.store("mb-big", "Big", big_releases + [self.rel("rg-new")])
            out3 = io.StringIO()
            with contextlib.redirect_stdout(out3):
                bot._index_changes_view(0, "")
            self.assertIn("Warning", out3.getvalue())

    def test_a_normal_sized_first_artist_is_not_marked_truncated(self):
        self.store("mb-a", "A", [self.rel("rg1"), self.rel("rg2")])
        page, status = bot._index_changes_view(0, "")
        self.assertEqual(status, 200)
        self.assertNotIn("rowsTruncated", page["items"][0])
        self.assertNotIn("rowsTotal", page["items"][0])
        self.assertEqual(len(page["items"][0]["rows"]), 2)

    def test_byte_cap_splits_across_pages_and_a_full_pull_still_gets_everything(self):
        self.store("mb-a", "A", [self.rel(f"rg{i}", title="T" * 100) for i in range(20)])
        self.store("mb-b", "B", [self.rel(f"rg{i}", title="T" * 100) for i in range(20)])
        self.store("mb-c", "C", [self.rel(f"rg{i}", title="T" * 100) for i in range(20)])
        with patch.object(bot, "INDEX_CHANGES_MAX_BYTES", 4000):
            items, page = self.pull_all()
        self.assertEqual({i["key"] for i in items}, {"mb-a", "mb-b", "mb-c"})
        self.assertFalse(page["more"])

    def test_a_write_mid_sync_to_a_delivered_and_an_undelivered_artist_is_not_lost(self):
        """Page 1 delivers mb-a; a write lands on mb-a (already delivered) and
        on mb-b (not yet delivered) before page 2 is pulled. The union of both
        pages must hold the LATEST version of every artist — a re-fetch of an
        already-seen key is harmless (contract: "an artist is applied only if
        its seq is greater than the local one"), losing one is not."""
        self.store("mb-a", "A", [self.rel("rg1", title="v1")])
        self.store("mb-b", "B", [self.rel("rg2")])
        with patch.object(bot, "INDEX_CHANGES_MAX_BYTES", 10):
            page1, status = bot._index_changes_view(0, "")
        self.assertEqual(status, 200)
        self.assertEqual(len(page1["items"]), 1)
        self.assertTrue(page1["more"])
        # A write to the already-delivered artist, and one to the artist still
        # waiting in the queue.
        self.store("mb-a", "A", [self.rel("rg1", title="v2")])
        self.store("mb-b", "B", [self.rel("rg2"), self.rel("rg4")])
        page2, status = bot._index_changes_view(page1["nextSince"], "")
        self.assertEqual(status, 200)
        by_key = {i["key"]: i for i in page1["items"] + page2["items"]}
        self.assertEqual(set(by_key), {"mb-a", "mb-b"})
        latest_a = max((i for i in page1["items"] + page2["items"] if i["key"] == "mb-a"),
                       key=lambda i: i["seq"])
        self.assertEqual([r["title"] for r in latest_a["rows"]], ["v2"])
        latest_b = max((i for i in page1["items"] + page2["items"] if i["key"] == "mb-b"),
                       key=lambda i: i["seq"])
        self.assertEqual({r["rgid"] for r in latest_b["rows"]}, {"rg2", "rg4"})

    # -- resync --------------------------------------------------------------

    def test_since_greater_than_head_seq_answers_resync(self):
        self.store("mb-a", "A", [self.rel("rg1")])
        head_seq, head_epoch = self.head()
        page, status = bot._index_changes_view(head_seq + 100, "")
        self.assertEqual(status, 200)
        self.assertEqual(page, {"resync": True, "epoch": head_epoch, "headSeq": head_seq,
                                "scanVersion": bot.INDEX_SCAN_VERSION,
                                "ttlDays": bot.LB_BOT_INDEX_TTL_DAYS})

    def test_a_wrong_epoch_answers_resync(self):
        self.store("mb-a", "A", [self.rel("rg1")])
        page, status = bot._index_changes_view(0, "1-notthisdb")
        self.assertEqual(status, 200)
        self.assertTrue(page["resync"])
        self.assertNotIn("items", page)

    def test_an_empty_epoch_string_never_triggers_resync(self):
        """`since` defaults to 0 and `epoch` defaults to "" — a first-ever pull
        from a brand new client must be a normal answer, not a resync."""
        self.store("mb-a", "A", [self.rel("rg1")])
        page, status = bot._index_changes_view(0, "")
        self.assertEqual(status, 200)
        self.assertNotIn("resync", page)

    def test_the_matching_epoch_never_triggers_resync(self):
        self.store("mb-a", "A", [self.rel("rg1")])
        _, epoch = self.head()
        page, status = bot._index_changes_view(0, epoch)
        self.assertNotIn("resync", page)

    # -- the HWM gate: no seq leaves the process unrecorded ------------------

    def test_changes_answers_503_when_the_hwm_cannot_be_persisted(self):
        self.store("mb-a", "A", [self.rel("rg1")])
        with patch.object(bot, "_atomic_json_write", side_effect=OSError("read-only /config")):
            page, status = bot._index_changes_view(0, "")
        self.assertEqual(status, 503)
        self.assertIn("error", page)

    def test_keys_answers_503_when_the_hwm_cannot_be_persisted(self):
        self.store("mb-a", "A", [self.rel("rg1")])
        with patch.object(bot, "_atomic_json_write", side_effect=OSError("read-only /config")):
            page, status = bot._index_keys_view()
        self.assertEqual(status, 503)
        self.assertIn("error", page)

    def test_health_stays_200_with_the_persisted_head_when_the_hwm_cannot_be_written(self):
        """M3: `/api/health` is the hub's liveness probe. Answering 503 on an
        HWM write failure made the hub broadcast lb-bot as unavailable, and
        every client hid every lb-bot feature over a disk problem that
        affects only the mirror. It reports the persisted head — never a
        higher, unrecorded one — and does not even try to write."""
        self.store("mb-a", "A", [self.rel("rg1")])
        bot._index_changes_view(0, "")                  # records the head
        recorded, epoch = self.head()
        self.store("mb-b", "B", [self.rel("rg2")])      # head moves past the HWM
        self.assertGreater(self.head()[0], recorded)
        with patch.object(bot, "_atomic_json_write",
                          side_effect=AssertionError("health must not write")):
            page, status = bot._index_health_view()
        self.assertEqual(status, 200)
        self.assertEqual(page, {"ok": True, "epoch": epoch, "headSeq": recorded})

    def test_health_reports_zero_before_any_head_was_recorded(self):
        self.store("mb-a", "A", [self.rel("rg1")])
        page, status = bot._index_health_view()
        self.assertEqual((status, page["headSeq"]), (200, 0))

    def test_changes_persists_the_hwm_before_answering(self):
        self.store("mb-a", "A", [self.rel("rg1")])
        head_seq, head_epoch = self.head()
        bot._index_changes_view(0, "")
        with open(self.db_path + ".hwm.json") as fh:
            self.assertEqual(json.load(fh), {"epoch": head_epoch, "hwm": head_seq})

    # -- /api/index/keys -------------------------------------------------

    def test_keys_lists_every_artist_key_and_seq_but_no_tombstones(self):
        self.store("mb-a", "A", [self.rel("rg1")])
        self.store("", "B", [self.rel("rg2")], nd_id="7")
        self.store("mb-c", "C", [self.rel("rg2")], nd_id="7")  # tombstones nd:7
        page, status = bot._index_keys_view()
        self.assertEqual(status, 200)
        keys = {k["key"]: k["seq"] for k in page["keys"]}
        self.assertEqual(set(keys), {"mb-a", "mb-c"})
        self.assertEqual(page["headSeq"], self.head()[0])
        self.assertEqual(page["epoch"], self.head()[1])

    # -- /api/health -----------------------------------------------------

    def test_health_reports_ok_epoch_and_head_seq_only(self):
        self.store("mb-a", "A", [self.rel("rg1")])
        head_seq, head_epoch = self.head()
        bot._index_persist_hwm(head_seq, head_epoch)
        page, status = bot._index_health_view()
        self.assertEqual(status, 200)
        self.assertEqual(page, {"ok": True, "epoch": head_epoch, "headSeq": head_seq})

    # -- the artist_key tiebreak in _index_get_artist -------------------

    def test_index_get_artist_nd_lookup_orders_by_artist_key_after_scanned_at(self):
        """Contract §1a: the server's own nd-id lookup must resolve the same
        way a client mirror's would during the brief two-rows state an nd: ->
        mbid swap leaves. Two rows tied on scanned_at must break the tie the
        same deterministic way every time: artist_key ascending."""
        with bot._index_lock:
            conn = bot._index_db()
            conn.execute(
                "INSERT INTO artists (artist_key, nd_artist_id, name, scanned_at) "
                "VALUES ('nd:7', '7', 'B', 5)")
            conn.execute(
                "INSERT INTO artists (artist_key, artist_mbid, nd_artist_id, name, scanned_at) "
                "VALUES ('mb-a', 'mb-a', '7', 'A', 5)")
            conn.commit()
        result = bot._index_get_artist(nd_artist_id="7")
        self.assertEqual(result["artist_mbid"], "mb-a")


class IndexHwmBootBestEffortTests(unittest.TestCase):
    """Ruling R12: a boot-time HWM write that raises must not stop `_index_db()`
    from returning a working connection, so a caller like
    `_index_mark_release_present` (which swallows the error) doesn't silently
    lose a placement's "present" mark for a reason that has nothing to do with
    the placement itself."""

    def setUp(self):
        import shutil
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, True)
        self.db_path = os.path.join(td, "index.db")
        scratch_index(self, self.db_path)

    def test_unwritable_hwm_path_at_boot_still_yields_a_working_index_db(self):
        bot._index_store_artist(
            {"artist_mbid": "mb-a", "artist_name": "A",
             "releases": [{"rgid": "rg1", "title": "T", "status": "missing"}]}, "")
        seq, epoch = bot._index_head()
        # Force the boot-time epoch rotation to fire on the next open: a
        # persisted HWM strictly above the current head, under the same epoch.
        bot._index_persist_hwm(seq + 5, epoch)
        swap_index(bot.LIBRARY_INDEX_FILE)
        with patch.object(bot, "_atomic_json_write", side_effect=OSError("read-only /config")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                conn = bot._index_db()
            self.assertIsNotNone(conn)
            self.assertIn("could not persist the high-water mark after rotating",
                          out.getvalue())
            # The epoch did rotate in memory even though the file write failed.
            new_seq, new_epoch = bot._index_head()
            self.assertEqual(new_seq, seq)
            self.assertNotEqual(new_epoch, epoch)
            # And the caller that swallows this function's own errors still
            # changes its row — the R12 regression this guards against.
            self.assertEqual(bot._index_mark_release_present(rgid="rg1"), 1)
            row = bot._index_db().execute(
                "SELECT status FROM release_groups WHERE rgid = 'rg1'").fetchone()
            self.assertEqual(row["status"], "present")


class IndexPushSenderTests(unittest.TestCase):
    """The `index-push` thread's throttle decision (PLAN-lbbot-index-mirror-
    2026-09-23, contract §1a): leading edge, gap-since-attempt (not
    since-success), retry-on-failure, send-once-at-boot. Exercised entirely
    through `_index_push_tick` — a pure function of an explicit state, a fake
    clock and a fake poster — so none of this needs a real thread or a real
    sleep."""

    def setUp(self):
        import shutil
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, True)
        scratch_index(self, os.path.join(td, "index.db"))

    @staticmethod
    def _state():
        return {"acked": None, "attempted_at": None}

    @staticmethod
    def _recording_poster(posts, ok=True):
        def poster(seq, epoch):
            posts.append((seq, epoch))
            return ok
        return poster

    def test_sends_once_at_boot_even_with_no_change(self):
        posts = []
        state = bot._index_push_tick(self._state(), 100.0, (0, "e1"),
                                     self._recording_poster(posts))
        self.assertEqual(posts, [(0, "e1")])
        self.assertEqual(state["acked"], (0, "e1"))

    def test_burst_of_writes_produces_one_send_then_one_more_after_the_gap(self):
        posts = []
        poster = self._recording_poster(posts)
        state = bot._index_push_tick(self._state(), 0.0, (1, "e1"), poster)  # boot send
        self.assertEqual(len(posts), 1)
        for t, head in ((0.3, (2, "e1")), (0.6, (3, "e1")), (0.9, (4, "e1"))):
            state = bot._index_push_tick(state, t, head, poster)
        self.assertEqual(len(posts), 1, "writes inside the 2 s gap produce no extra send")
        state = bot._index_push_tick(state, 2.1, (5, "e1"), poster)
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[-1], (5, "e1"), "sends the now-current head, not a queued one")

    def test_unchanged_head_after_a_success_sends_nothing(self):
        posts = []
        poster = self._recording_poster(posts)
        bot._index_push_tick(self._state(), 0.0, (1, "e1"), poster)
        self.assertEqual(len(posts), 1)
        state = bot._index_push_tick({"acked": (1, "e1"), "attempted_at": 0.0}, 100.0,
                                     (1, "e1"), poster)
        self.assertEqual(len(posts), 1)
        self.assertEqual(state["acked"], (1, "e1"))

    def test_a_failed_post_is_retried_on_the_next_eligible_tick_until_2xx(self):
        results = iter([False, False, True])
        calls = []
        def poster(seq, epoch):
            calls.append((seq, epoch))
            return next(results)
        state = bot._index_push_tick(self._state(), 0.0, (1, "e1"), poster)
        self.assertIsNone(state["acked"], "a non-2xx leaves acked alone")
        self.assertEqual(len(calls), 1)
        state = bot._index_push_tick(state, 1.0, (1, "e1"), poster)  # still inside the gap
        self.assertEqual(len(calls), 1, "no retry before the gap since the last attempt elapses")
        state = bot._index_push_tick(state, 2.0, (1, "e1"), poster)  # gap elapsed: retried
        self.assertEqual(len(calls), 2)
        self.assertIsNone(state["acked"])
        state = bot._index_push_tick(state, 4.0, (1, "e1"), poster)
        self.assertEqual(len(calls), 3)
        self.assertEqual(state["acked"], (1, "e1"))

    def test_an_exception_from_the_poster_is_treated_like_a_non_2xx(self):
        calls = []
        def poster(seq, epoch):
            calls.append((seq, epoch))
            raise RuntimeError("network down")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            state = bot._index_push_tick(self._state(), 0.0, (1, "e1"), poster)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(state["acked"])
        self.assertIn("index push failed", out.getvalue())

    def test_a_new_epoch_at_the_same_seq_counts_as_a_change(self):
        posts = []
        poster = self._recording_poster(posts)
        state = bot._index_push_tick(self._state(), 0.0, (5, "e1"), poster)
        bot._index_push_tick(state, 10.0, (5, "e2"), poster)
        self.assertEqual(posts, [(5, "e1"), (5, "e2")])

    def test_hwm_persisted_before_the_post_and_a_persist_failure_sends_nothing(self):
        posts = []
        poster = self._recording_poster(posts)
        with patch.object(bot, "_atomic_json_write", side_effect=OSError("read-only /config")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                state = bot._index_push_tick(self._state(), 0.0, (5, "e1"), poster)
        self.assertEqual(posts, [], "a persist failure must not POST that seq")
        self.assertIsNone(state["attempted_at"], "a persist failure is not a send attempt")
        self.assertIn("not sending", out.getvalue())
        # HWM writes work again, but the persist backs off (M3/T3): nothing
        # is retried inside the first 2 s, and the retry after it sends.
        state = bot._index_push_tick(state, 0.1, (5, "e1"), poster)
        self.assertEqual(posts, [])
        with contextlib.redirect_stdout(io.StringIO()):
            state = bot._index_push_tick(state, 2.0, (5, "e1"), poster)
        self.assertEqual(posts, [(5, "e1")])

    def test_persist_failures_back_off_doubling_to_a_minute_and_log_once(self):
        """M3/T3: the sender used to retry a failing HWM write every 1 s
        forever, with a log line each time. It doubles from 2 s to 60 s,
        resets on success, and logs once per change of state."""
        attempts, posts = [], []
        poster = self._recording_poster(posts)
        clock = {"t": 0.0}

        def failing_write(path, data):
            attempts.append(clock["t"])
            raise OSError("read-only /config")

        state, out = self._state(), io.StringIO()
        with patch.object(bot, "_atomic_json_write", failing_write), \
                contextlib.redirect_stdout(out):
            while clock["t"] < 250:
                state = bot._index_push_tick(state, clock["t"], (5, "e1"), poster)
                clock["t"] = round(clock["t"] + 0.5, 1)
        gaps = [b - a for a, b in zip(attempts, attempts[1:])]
        self.assertEqual(gaps[:7], [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0])
        self.assertEqual(posts, [])
        self.assertEqual(out.getvalue().count("could not persist"), 1, out.getvalue())

        # Recovery: sends at the next retry, says so once, and a later
        # failure starts the ladder from 2 s again.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for _ in range(130):
                state = bot._index_push_tick(state, clock["t"], (5, "e1"), poster)
                clock["t"] = round(clock["t"] + 0.5, 1)
        self.assertEqual(posts, [(5, "e1")])
        self.assertEqual(out.getvalue().count("recorded again"), 1, out.getvalue())
        attempts.clear()
        with patch.object(bot, "_atomic_json_write", failing_write), \
                contextlib.redirect_stdout(io.StringIO()):
            start = clock["t"]
            for _ in range(20):
                state = bot._index_push_tick(state, clock["t"], (6, "e1"), poster)
                clock["t"] = round(clock["t"] + 0.5, 1)
        self.assertEqual([round(a - start, 1) for a in attempts[:2]], [0.0, 2.0])

    def test_the_hwm_is_actually_persisted_before_the_post(self):
        posts = []
        poster = self._recording_poster(posts)
        bot._index_push_tick(self._state(), 0.0, (7, "e1"), poster)
        self.assertEqual(bot._index_read_hwm(), {"epoch": "e1", "hwm": 7})

    def test_worker_loop_polls_and_applies_the_same_throttle_via_push_enabled_gate(self):
        """`_index_push_worker` is a thin wrapper: real clock/sleeper/poster
        swapped for fakes, and `_push_enabled`/`_index_head` patched so the
        loop never touches the network or a real DB. No real sleep anywhere —
        the fake `sleeper` raises to stop the loop instead of blocking."""
        heads = iter([(0, "e1"), (0, "e1"), (1, "e1")])
        times = iter([0.0, 0.5, 1.0])
        posts = []
        poster = self._recording_poster(posts)
        sleep_calls = []
        def sleeper(secs):
            sleep_calls.append(secs)
            if len(sleep_calls) >= 3:
                raise SystemExit("stop the loop")
        with patch.object(bot, "_index_head", lambda: next(heads)), \
             patch.object(bot, "_push_enabled", lambda: True):
            with self.assertRaises(SystemExit):
                bot._index_push_worker(clock=lambda: next(times), sleeper=sleeper, poster=poster)
        # tick 1: (0,"e1") vs acked=None -> sends; tick 2: same head -> no
        # send; tick 3: (1,"e1") only 0.5 s after the first attempt -> still
        # inside the 2 s gap -> no send either.
        self.assertEqual(posts, [(0, "e1")])
        self.assertEqual(sleep_calls, [bot.INDEX_PUSH_POLL_SECS] * 3)

    def test_worker_loop_does_nothing_while_push_is_disabled(self):
        calls = {"head": 0}
        def head():
            calls["head"] += 1
            return (0, "e1")
        sleep_calls = []
        def sleeper(secs):
            sleep_calls.append(secs)
            if len(sleep_calls) >= 2:
                raise SystemExit("stop the loop")
        with patch.object(bot, "_index_head", head), \
             patch.object(bot, "_push_enabled", lambda: False):
            with self.assertRaises(SystemExit):
                bot._index_push_worker(clock=lambda: 0.0, sleeper=sleeper, poster=lambda *a: True)
        self.assertEqual(calls["head"], 0, "never even polls the head while disabled")


class _FakeMbzResponse:
    status_code = 200

    def __init__(self, data=None):
        self._data = data or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class MbzPriorityTests(unittest.TestCase):
    """The auto-index worker yields the MusicBrainz budget (task 5): a call
    made under `mbz_background()` waits while any interactive caller is
    waiting for, or holding, `_mbz_lock`. Real threads, but every wait is an
    Event or a bounded join — nothing here sleeps for real beyond a fraction
    of a second. `time.sleep` is stubbed so mbz_get's 1 req/s pacing does not
    add a second per call; the priority waits use a Condition, not sleep."""

    def setUp(self):
        sleep_patch = patch.object(bot.time, "sleep", lambda s: None)
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        grace = patch.object(bot, "MBZ_BACKGROUND_GRACE", 0.0)
        grace.start()
        self.addCleanup(grace.stop)
        self.calls = []
        self.calls_lock = threading.Lock()
        self.hold = {}   # query -> Event the fake http waits on before answering
        self.entered = {}  # query -> Event set when the fake http is reached

        def fake_get(url, params=None, **k):
            q = (params or {}).get("query", "")
            with self.calls_lock:
                self.calls.append(q)
            if q in self.entered:
                self.entered[q].set()
            if q in self.hold:
                self.hold[q].wait(5)
            return _FakeMbzResponse({})

        http_patch = patch.object(bot._http, "get", fake_get)
        http_patch.start()
        self.addCleanup(http_patch.stop)

    def _thread(self, query, background=False):
        def run():
            if background:
                with bot.mbz_background():
                    bot.mbz_get("artist", {"query": query})
            else:
                bot.mbz_get("artist", {"query": query})
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    def _waiters(self):
        with bot._mbz_priority:
            return bot._mbz_interactive_waiters

    def _await(self, pred, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            threading.Event().wait(0.005)
        return pred()

    def test_background_call_waits_while_an_interactive_waiter_is_registered(self):
        """An interactive caller that has registered but not yet taken the
        lock (the window between the two) must still hold the worker off —
        the lock itself is free, so only the counter can."""
        with bot._mbz_priority:
            bot._mbz_interactive_waiters += 1
        try:
            b = self._thread("prio-bg-1", background=True)
            b.join(0.2)
            self.assertTrue(b.is_alive(), "the worker must wait while a waiter is registered")
            self.assertEqual(self.calls, [], "the worker made no request")
        finally:
            with bot._mbz_priority:
                bot._mbz_interactive_waiters -= 1
                bot._mbz_priority.notify_all()
        b.join(2)
        self.assertFalse(b.is_alive())
        self.assertEqual(self.calls, ["prio-bg-1"], "the waiter left, the worker went")

    def test_every_queued_interactive_caller_goes_before_the_worker(self):
        """One interactive request in flight, a second queued behind it, and
        the worker arriving last: the queued page must not have to race the
        worker for the lock when the first request finishes."""
        self.hold["prio-i1"] = threading.Event()
        self.entered["prio-i1"] = threading.Event()
        i1 = self._thread("prio-i1")
        self.assertTrue(self.entered["prio-i1"].wait(2))
        i2 = self._thread("prio-i2")
        self.assertTrue(self._await(lambda: self._waiters() == 2),
                        "the second page registered as a waiter")
        b = self._thread("prio-bg-2", background=True)
        b.join(0.1)
        self.assertTrue(b.is_alive())
        self.hold["prio-i1"].set()
        for t in (i1, i2, b):
            t.join(2)
            self.assertFalse(t.is_alive())
        self.assertEqual(self.calls, ["prio-i1", "prio-i2", "prio-bg-2"])
        self.assertEqual(self._waiters(), 0)

    def test_waiter_count_is_restored_when_a_request_raises(self):
        """Exception-safe: a counter left raised by a failed request would
        park the worker forever."""
        class Boom(BaseException):
            pass

        def boom(*a, **k):
            raise Boom()

        with patch.object(bot._http, "get", boom):
            with self.assertRaises(Boom):
                bot.mbz_get("artist", {"query": "prio-boom"})
        self.assertEqual(self._waiters(), 0)
        # An ordinary failure (swallowed by mbz_get) too.
        key = bot._mbz_cache_key("artist", {"query": "prio-fail"})
        self.addCleanup(bot._mbz_fail_until.pop, key, None)
        with patch.object(bot._http, "get",
                          lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(bot.mbz_get("artist", {"query": "prio-fail"}), {})
        self.assertEqual(self._waiters(), 0)

    def test_the_worker_leaves_a_grace_gap_after_an_interactive_call(self):
        """A page usually makes its calls in a quick burst; the grace keeps the
        worker from slipping one request in between two of them."""
        with patch.object(bot, "MBZ_BACKGROUND_GRACE", 0.2):
            bot.mbz_get("artist", {"query": "prio-page"})
            started = time.monotonic()
            with bot.mbz_background():
                bot.mbz_get("artist", {"query": "prio-bg-3"})
            self.assertGreaterEqual(time.monotonic() - started, 0.15)
        self.assertEqual(self.calls, ["prio-page", "prio-bg-3"])

    def test_the_background_flag_is_thread_local_and_scoped(self):
        self.assertFalse(bot._mbz_is_background())
        with bot.mbz_background():
            self.assertTrue(bot._mbz_is_background())
            seen = []
            t = threading.Thread(target=lambda: seen.append(bot._mbz_is_background()))
            t.start()
            t.join(2)
            self.assertEqual(seen, [False], "another thread is interactive")
        self.assertFalse(bot._mbz_is_background())


class AutoIndexWorkerTests(unittest.TestCase):
    """The auto-index worker (PLAN-lbbot-index-mirror-2026-09-23 §2.5, task
    5): which Navidrome artists it picks, how a failure backs off, how it
    stays out of a manual build's way, and that an outage is never written
    down as "no such artist" (ruling R13). Driven through
    `_auto_index_tick` against a real temp index DB; no real thread."""

    USER = {"navidrome_user": "u", "navidrome_password": "p"}
    DAY = 86400.0

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(isolated_review())
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.now = 1_000_000_000.0

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def nd(nd_id, name, mbid=""):
        return {"id": nd_id, "name": name, "musicBrainzId": mbid}

    @staticmethod
    def meta(key, nd_id="", mbid=None, scanned_at=0.0, scan_version=None):
        return {"artist_key": key,
                "artist_mbid": (key if not key.startswith("nd:") else "") if mbid is None else mbid,
                "nd_artist_id": nd_id, "scanned_at": scanned_at,
                "scan_version": bot.INDEX_SCAN_VERSION if scan_version is None else scan_version}

    def keys(self):
        with bot._index_lock:
            return {r["artist_key"] for r in
                    bot._index_db().execute("SELECT artist_key FROM artists")}

    def patched(self, **fns):
        stack = contextlib.ExitStack()
        for name, fn in fns.items():
            stack.enter_context(patch.object(bot, name, fn))
        return stack

    def state_with(self, artists):
        state = bot._auto_index_new_state()
        state["nd_artists"] = list(artists)
        state["nd_fetched_at"] = self.now
        state["scan_polled_at"] = self.now
        return state

    # -- source selection ----------------------------------------------------

    def test_select_picks_missing_then_stub_then_stale_and_skips_fresh(self):
        fresh_at = self.now - self.DAY
        # Past the widest jittered TTL (final-review I2: up to TTL x 1.25).
        old_at = self.now - (bot.LB_BOT_INDEX_TTL_DAYS * 1.25 + 5) * self.DAY
        older_at = self.now - (bot.LB_BOT_INDEX_TTL_DAYS * 1.25 + 50) * self.DAY
        artists = [self.nd("1", "Fresh", "mb-fresh"), self.nd("2", "Missing"),
                   self.nd("3", "Stub", "mb-stub"), self.nd("4", "Stale"),
                   self.nd("5", "Older"), self.nd("6", "Old scanner", "mb-v1")]
        meta = [self.meta("mb-fresh", "1", scanned_at=fresh_at),
                self.meta("mb-stub", "", scanned_at=0, scan_version=0),
                self.meta("mb-stale", "4", scanned_at=old_at),
                self.meta("nd:5", "5", scanned_at=older_at),
                self.meta("mb-v1", "6", scanned_at=fresh_at, scan_version=1)]
        picked = bot._auto_index_select(artists, meta, self.now, {})
        self.assertEqual([(c["nd_id"], c["reason"]) for c in picked],
                         [("2", "missing"), ("3", "stub"),
                          ("5", "stale"), ("4", "stale"), ("6", "stale")],
                         "missing, then stubs, then stale oldest-first; "
                         "a scan_version bump is stale; fresh is skipped")
        self.assertEqual(picked[3]["stored"]["artist_mbid"], "mb-stale",
                         "the matched row travels with the candidate")

    def test_select_skips_a_failed_artist_until_its_backoff_expires(self):
        artists = [self.nd("1", "Flaky"), self.nd("2", "Other")]
        failures = {"1": {"count": 1, "retry_at": self.now + 10, "error": "x"}}
        picked = bot._auto_index_select(artists, [], self.now, failures)
        self.assertEqual([c["nd_id"] for c in picked], ["2"])
        picked = bot._auto_index_select(artists, [], self.now + 10, failures)
        self.assertEqual([c["nd_id"] for c in picked], ["1", "2"])

    def test_backoff_doubles_per_consecutive_failure_and_caps(self):
        base, cap = bot.AUTO_INDEX_FAIL_BACKOFF_BASE, bot.AUTO_INDEX_FAIL_BACKOFF_MAX
        self.assertEqual(bot._auto_index_backoff(1), base)
        self.assertEqual(bot._auto_index_backoff(2), base * 2)
        self.assertEqual(bot._auto_index_backoff(3), base * 4)
        self.assertEqual(bot._auto_index_backoff(60), cap)

    def test_select_treats_a_stub_as_stale_once_navidrome_gains_a_tag_mbid(self):
        """R15: the stub itself is fresh (scanned an hour ago, current scan
        version) — only the fact that Navidrome's row now carries a tag mbid
        the index has never scanned under makes it stale. Without this a
        resolvable artist stays hidden behind its own miss-stub for up to
        LB_BOT_INDEX_TTL_DAYS."""
        artists = [self.nd("100", "Resolved", "mb-100")]
        meta = [self.meta("nd:100", "100", mbid="", scanned_at=self.now - 3600)]
        picked = bot._auto_index_select(artists, meta, self.now, {})
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["reason"], "stale")
        self.assertEqual(picked[0]["row"]["mbid"], "mb-100",
                         "the worker must scan it through the new mbid")

        # Once actually scanned under that mbid, it is no longer picked.
        meta.append(self.meta("mb-100", "100", scanned_at=self.now))
        self.assertEqual(bot._auto_index_select(artists, meta, self.now, {}), [])

    def test_select_never_picks_a_stub_shadowed_by_an_mbid_row(self):
        """The fill race can leave an `nd:X` stub beside the real mbid row that
        holds Navidrome id X. The mbid row is the artist's row: fresh means no
        work, and the stub is never rescanned by name."""
        artists = [self.nd("7", "Swapped")]
        meta = [self.meta("mb-7", "7", scanned_at=self.now - self.DAY),
                self.meta("nd:7", "7", scanned_at=0, scan_version=0)]
        self.assertEqual(bot._auto_index_select(artists, meta, self.now, {}), [])
        # Stale mbid row: rescanned through its mbid, not the stub's empty one.
        meta[0]["scanned_at"] = 0
        picked = bot._auto_index_select(artists, meta, self.now, {})
        self.assertEqual(picked[0]["stored"]["artist_key"], "mb-7")

    # -- the tick ------------------------------------------------------------

    def test_tick_scans_a_missing_artist_in_the_background_role(self):
        roles = []

        def fake_discog(mbid, name, *a, **k):
            roles.append(bot._mbz_is_background())
            return {"artist_mbid": mbid, "artist_name": name, "review_groups": [],
                    "releases": [{"rgid": "rg-1", "title": "A", "status": "complete"}]}

        state = self.state_with([self.nd("10", "New", "mb-10")])
        with self.patched(build_artist_discography=fake_discog):
            outcome = bot._auto_index_tick(state, self.now, self.USER)
        self.assertEqual(outcome, "indexed")
        self.assertEqual(roles, [True], "every MusicBrainz call of the scan is background")
        self.assertEqual(bot._index_get_artist("mb-10", "10")["releases"][0]["rgid"], "rg-1")
        with self.patched(build_artist_discography=fake_discog):
            self.assertEqual(bot._auto_index_tick(state, self.now, self.USER), "idle",
                             "now fresh: nothing left to do")

    def test_tick_a_musicbrainz_failure_writes_nothing_and_backs_off(self):
        """R13: a strict name search that raises is an outage, not a no-match.
        Nothing is written (an `nd:` miss-stub would hide the artist as
        unresolvable for LB_BOT_INDEX_TTL_DAYS), the artist backs off, and the
        worker pauses rather than hammering the next artist."""
        seen_strict = []

        def search(name, limit=8, strict=False):
            seen_strict.append(strict)
            raise bot.MusicBrainzUnavailable("MusicBrainz did not answer (HTTP 503)")

        state = self.state_with([self.nd("20", "Tagless")])
        with self.patched(mbz_search_artists=search):
            outcome = bot._auto_index_tick(state, self.now, self.USER)
        self.assertEqual(outcome, "failed")
        self.assertEqual(seen_strict, [True])
        self.assertEqual(self.keys(), set(), "no miss-stub on an outage")
        self.assertEqual(state["failures"]["20"]["count"], 1)
        self.assertEqual(state["failures"]["20"]["retry_at"],
                         self.now + bot.AUTO_INDEX_FAIL_BACKOFF_BASE)
        self.assertEqual(state["paused_until"], self.now + bot.AUTO_INDEX_MB_OUTAGE_PAUSE)
        with self.patched(mbz_search_artists=search):
            self.assertEqual(bot._auto_index_tick(state, self.now + 1, self.USER), "outage")
        self.assertEqual(seen_strict, [True], "no request during the outage pause")

    def test_tick_a_bad_mbid_backs_off_only_that_artist_not_the_worker(self):
        """R14: a permanent 4xx (a bad or merged mbid) is this one artist's
        problem, not an outage — it must not pause the whole worker the way
        test_tick_a_musicbrainz_failure_writes_nothing_and_backs_off's genuine
        outage does."""
        def fail_discog(mbid, name, *a, **k):
            raise bot.MusicBrainzNoSuchEntity(
                "MusicBrainz 404 for release-group — no such entity")

        state = self.state_with([self.nd("90", "Bad", "mb-bad")])
        with self.patched(build_artist_discography=fail_discog):
            outcome = bot._auto_index_tick(state, self.now, self.USER)
        self.assertEqual(outcome, "failed")
        self.assertEqual(state["failures"]["90"]["count"], 1)
        self.assertEqual(state["paused_until"], 0.0,
                         "a bad mbid must not pause the whole worker")

        # A second, unrelated artist is picked on the very next tick.
        state["nd_artists"].append(self.nd("91", "Fine", "mb-fine"))
        with self.patched(build_artist_discography=lambda mbid, name, *a, **k: {
                "artist_mbid": mbid, "artist_name": name, "review_groups": [],
                "releases": []}):
            self.assertEqual(bot._auto_index_tick(state, self.now, self.USER), "indexed")

    def test_tick_a_genuine_no_match_records_the_miss_stub(self):
        state = self.state_with([self.nd("30", "Nobody")])
        with self.patched(mbz_search_artists=lambda name, limit=8, strict=False: []):
            outcome = bot._auto_index_tick(state, self.now, self.USER)
        self.assertEqual(outcome, "unresolved")
        self.assertEqual(self.keys(), {"nd:30"})
        self.assertNotIn("30", state["failures"], "a no-match is an answer, not a failure")

    def test_a_success_clears_the_failure_record(self):
        state = self.state_with([self.nd("40", "Back", "mb-40")])
        state["failures"]["40"] = {"count": 3, "retry_at": self.now - 1, "error": "x"}
        fake = lambda mbid, name, *a, **k: {"artist_mbid": mbid, "artist_name": name,
                                             "review_groups": [], "releases": []}
        with self.patched(build_artist_discography=fake):
            self.assertEqual(bot._auto_index_tick(state, self.now, self.USER), "indexed")
        self.assertNotIn("40", state["failures"])

    def test_an_empty_answer_never_replaces_a_stored_discography(self):
        bot._index_store_artist({"artist_mbid": "mb-50", "artist_name": "Kept",
                                 "releases": [{"rgid": "rg-k", "title": "K",
                                               "status": "complete"}]}, "50")
        with bot._index_lock, bot._index_db() as conn:
            conn.execute("UPDATE artists SET scanned_at = 0 WHERE artist_key = 'mb-50'")
        state = self.state_with([self.nd("50", "Kept", "mb-50")])
        fake = lambda mbid, name, *a, **k: {"artist_mbid": mbid, "artist_name": name,
                                             "review_groups": [], "releases": []}
        with self.patched(build_artist_discography=fake):
            self.assertEqual(bot._auto_index_tick(state, self.now, self.USER), "failed")
        self.assertEqual(len(bot._index_get_artist("mb-50", "50")["releases"]), 1)
        self.assertIn("50", state["failures"])

    def test_a_running_manual_build_pauses_the_worker(self):
        calls = []
        fake = lambda *a, **k: calls.append(a) or {
            "artist_mbid": "mb-60", "artist_name": "X", "review_groups": [], "releases": []}
        task_id = bot._task_create("library-index", "Build library index")
        state = self.state_with([self.nd("60", "X", "mb-60")])
        with self.patched(build_artist_discography=fake):
            self.assertEqual(bot._auto_index_tick(state, self.now, self.USER), "paused")
            self.assertEqual(calls, [])
            bot._task_finish(task_id, "done")
            self.assertEqual(bot._auto_index_tick(state, self.now, self.USER), "indexed")
        self.assertEqual(len(calls), 1)

    def test_tick_refreshes_navidrome_artists_every_15_min_and_after_a_scan(self):
        fetches, statuses = [], iter([
            {"scanning": False, "lastScan": "t0"},   # first poll: a baseline, not a finish
            {"scanning": True, "lastScan": "t0"},
            {"scanning": False, "lastScan": "t1"},   # the scan finished
        ])

        def fetch(force=False):
            fetches.append(force)
            return [self.nd("70", "Fresh", "mb-70")]

        bot._index_store_artist({"artist_mbid": "mb-70", "artist_name": "Fresh",
                                 "releases": []}, "70")
        old_ts = bot._ND_ALBUM_INDEX["ts"]
        self.addCleanup(bot._ND_ALBUM_INDEX.__setitem__, "ts", old_ts)

        def tick(at):
            """Run a tick; say whether it expired the cached album index."""
            bot._ND_ALBUM_INDEX["ts"] = 123.0
            bot._auto_index_tick(state, at, self.USER)
            return bot._ND_ALBUM_INDEX["ts"] == 0.0

        state = bot._auto_index_new_state()
        with self.patched(_nd_artist_index=fetch,
                          nd_get_scan_status=lambda u, p: next(statuses)):
            t = time.time()
            self.assertTrue(tick(t), "new artists: classify them against a fresh album index")
            self.assertEqual(fetches, [True], "first tick: fetch")
            self.assertFalse(tick(t + bot.AUTO_INDEX_SCAN_POLL_SECS))
            self.assertEqual(len(fetches), 1, "scanning, not finished: no refetch")
            self.assertTrue(tick(t + 2 * bot.AUTO_INDEX_SCAN_POLL_SECS),
                            "a finished scan also expires the album index")
            self.assertEqual(len(fetches), 2, "a finished Navidrome scan refetches")
        with self.patched(_nd_artist_index=fetch,
                          nd_get_scan_status=lambda u, p: {"scanning": False, "lastScan": "t1"}):
            self.assertFalse(tick(t + 3 * bot.AUTO_INDEX_SCAN_POLL_SECS))
            self.assertEqual(len(fetches), 2)
            self.assertFalse(tick(t + 2 * bot.AUTO_INDEX_SCAN_POLL_SECS + bot.AUTO_INDEX_DIFF_SECS),
                             "an unchanged list leaves the album cache alone")
            self.assertEqual(len(fetches), 3, "the 15-minute diff")

    def test_scan_finished_detection(self):
        f = bot._auto_index_scan_finished
        self.assertFalse(f(None, {"scanning": False, "lastScan": "a"}), "no baseline yet")
        self.assertTrue(f({"scanning": True, "lastScan": "a"}, {"scanning": False, "lastScan": "a"}))
        self.assertTrue(f({"scanning": False, "lastScan": "a"}, {"scanning": False, "lastScan": "b"}),
                        "a whole scan between two polls")
        self.assertFalse(f({"scanning": False, "lastScan": "a"}, {"scanning": True, "lastScan": "a"}))
        self.assertFalse(f({"scanning": False, "lastScan": "a"}, {}), "an unreadable status")

    # -- the nd: invariant and the duplicate stub --------------------------------

    def test_store_unless_claimed_never_writes_nd_over_another_row_holding_the_id(self):
        bot._index_store_artist({"artist_mbid": "mb-80", "artist_name": "Real",
                                 "releases": []}, "80")
        wrote = bot._index_store_artist({"artist_mbid": "", "artist_name": "Real",
                                         "releases": []}, "80", unless_claimed=True)
        self.assertFalse(wrote)
        self.assertEqual(self.keys(), {"mb-80"})
        # Its own stub may be refreshed, and an unclaimed id may be written.
        self.assertTrue(bot._index_store_artist({"artist_mbid": "", "artist_name": "N",
                                                 "releases": []}, "81", unless_claimed=True))
        self.assertTrue(bot._index_store_artist({"artist_mbid": "", "artist_name": "N",
                                                 "releases": []}, "81", unless_claimed=True))
        self.assertEqual(self.keys(), {"mb-80", "nd:81"})

    def test_a_shadowed_stub_is_dropped_only_when_the_mbid_row_has_its_present_rows(self):
        bot._index_store_artist({"artist_mbid": "mb-90", "artist_name": "A",
                                 "releases": [{"rgid": "rg-p", "title": "P",
                                               "status": "missing"}]}, "90")
        bot._index_mark_release_present(rgid="rg-p")
        bot._index_ensure_artist("nd:90", nd_artist_id="90")
        bot._index_mark_release_present(rgid="rg-p2", artist_key="nd:91", title="x")  # unrelated
        with bot._index_lock, bot._index_db() as conn:
            conn.execute("INSERT INTO release_groups (artist_key, rgid, status) "
                         "VALUES ('nd:90', 'rg-p', 'present')")
            conn.execute("INSERT INTO artists (artist_key, nd_artist_id, scan_version) "
                         "VALUES ('nd:92', '92', 0)")
            conn.execute("INSERT INTO release_groups (artist_key, rgid, status) "
                         "VALUES ('nd:92', 'rg-only-here', 'present')")
        bot._index_store_artist({"artist_mbid": "mb-92", "artist_name": "B",
                                 "releases": []}, "")
        with bot._index_lock, bot._index_db() as conn:
            conn.execute("UPDATE artists SET nd_artist_id = '92' WHERE artist_key = 'mb-92'")
        dropped = bot._index_drop_shadowed_nd_stubs()
        self.assertEqual(dropped, ["nd:90"])
        self.assertNotIn("nd:90", self.keys())
        self.assertIn("nd:92", self.keys(), "its present row is not on the mbid row: kept")
        self.assertIn("nd:91", self.keys(), "no mbid row holds 91: not a duplicate")
        with bot._index_lock:
            tomb = bot._index_db().execute(
                "SELECT artist_key FROM index_tombstones").fetchall()
        self.assertIn("nd:90", {r["artist_key"] for r in tomb}, "the mirrors hear of it")

    # -- final review: bounded rescans, jittered staleness, no review writes --

    @staticmethod
    def discog_with(releases=None, review_groups=None, calls=None):
        def fake(mbid, name, *a, **k):
            if calls is not None:
                calls.append(name)
            return {"artist_mbid": mbid, "artist_name": name,
                    "review_groups": list(review_groups or []),
                    "releases": list(releases if releases is not None else
                                     [{"rgid": "rg-1", "title": "A", "status": "complete"}])}
        return fake

    def test_two_untagged_artists_on_one_mbid_are_scanned_a_bounded_number_of_times(self):
        """C1: "Beyonce" and "Beyoncé", neither tagged, both name-search to
        the same mbid. Scanning A stored mb-M holding A's Navidrome id; C then
        found no row holding ITS id, ranked "missing" (always first), searched,
        rescanned mb-M and moved it to C — which made A missing again. Every
        tick after that was a live MusicBrainz scan and a head move, so every
        client pulled every 2 s, forever."""
        scans, heads, outcomes = [], [], []
        search = lambda name, limit=8, strict=False: [{"mbid": "mb-M"}]
        state = self.state_with([self.nd("a", "Beyonce"), self.nd("c", "Beyoncé")])
        with self.patched(mbz_search_artists=search,
                          build_artist_discography=self.discog_with(calls=scans)):
            for i in range(8):
                outcomes.append(bot._auto_index_tick(state, self.now + i, self.USER))
                heads.append(bot._index_head()[0])
        self.assertEqual(len(scans), 1, f"one scan for one discography, got {outcomes}")
        self.assertEqual(outcomes[-4:], ["idle"] * 4)
        self.assertEqual(len(set(heads[1:])), 1, f"the head must stop moving: {heads}")
        with bot._index_lock:
            holder = bot._index_db().execute(
                "SELECT nd_artist_id FROM artists WHERE artist_key = 'mb-M'").fetchone()
        self.assertEqual(holder["nd_artist_id"], "a", "the covered artist never rewrites the row")

        # A restart forgets the worker's memory: the covered check alone
        # still keeps it at zero scans (one name search, then nothing).
        state = self.state_with([self.nd("a", "Beyonce"), self.nd("c", "Beyoncé")])
        scans.clear()
        with self.patched(mbz_search_artists=search,
                          build_artist_discography=self.discog_with(calls=scans)):
            for i in range(4):
                bot._auto_index_tick(state, self.now + 10 + i, self.USER)
        self.assertEqual(scans, [])

    def test_a_row_held_by_an_id_no_longer_in_the_library_is_not_covered(self):
        """The covered check needs a LIVE holder: a row still pointing at a
        Navidrome id that left the library (a retag gives an artist a new id)
        must be claimed by the artist that is here, not treated as someone
        else's for the whole TTL."""
        bot._index_store_artist({"artist_mbid": "mb-M", "artist_name": "Old",
                                 "releases": []}, "gone")
        calls = []
        state = self.state_with([self.nd("c", "Beyoncé")])
        with self.patched(mbz_search_artists=lambda name, limit=8, strict=False: [{"mbid": "mb-M"}],
                          build_artist_discography=self.discog_with(calls=calls)):
            self.assertEqual(bot._auto_index_tick(state, self.now, self.USER), "indexed")
        self.assertEqual(calls, ["Beyoncé"])

    def test_a_scanned_artist_is_not_repicked_within_the_ttl_whatever_the_db_says(self):
        """C1 (b), the general guard: whatever a scan left in the DB, the
        same Navidrome id is not scanned again inside the TTL."""
        calls = []
        state = self.state_with([self.nd("10", "New", "mb-10")])
        with self.patched(build_artist_discography=self.discog_with(calls=calls)):
            self.assertEqual(bot._auto_index_tick(state, self.now, self.USER), "indexed")
            with bot._index_lock, bot._index_db() as conn:
                conn.execute("DELETE FROM release_groups WHERE artist_key = 'mb-10'")
                conn.execute("DELETE FROM artists WHERE artist_key = 'mb-10'")
            self.assertEqual(bot._auto_index_tick(state, self.now + 1, self.USER), "idle",
                             "missing again in the DB, but scanned a second ago")
        self.assertEqual(calls, ["New"])
        later = self.now + bot.LB_BOT_INDEX_TTL_DAYS * self.DAY + 1
        with self.patched(build_artist_discography=self.discog_with(calls=calls),
                          _nd_artist_index=lambda force=False: [self.nd("10", "New", "mb-10")],
                          nd_get_scan_status=lambda u, p: {}):
            self.assertEqual(bot._auto_index_tick(state, later, self.USER), "indexed")

    def test_a_new_tag_mbid_is_not_held_back_by_the_rescan_guard(self):
        """R15 survives the guard: a miss recorded a minute ago is rescanned
        at once when Navidrome's row gains a tag mbid."""
        state = self.state_with([self.nd("100", "Nobody")])
        with self.patched(mbz_search_artists=lambda name, limit=8, strict=False: []):
            self.assertEqual(bot._auto_index_tick(state, self.now, self.USER), "unresolved")
        state["nd_artists"] = [self.nd("100", "Nobody", "mb-100")]
        with self.patched(build_artist_discography=self.discog_with()):
            self.assertEqual(bot._auto_index_tick(state, self.now + 60, self.USER), "indexed")
        self.assertIn("mb-100", self.keys())

    def test_stale_ttl_jitter_is_deterministic_bounded_and_spread(self):
        """I2: rows from one bulk build all passed the TTL on the same day and
        stayed aligned after the rescan — a monthly multi-hour burst. The
        effective TTL is TTL x (1 + (crc32(key) % 1000) / 4000): 0-25 %
        longer, stable across restarts (never Python's randomized hash())."""
        import zlib
        ttl = bot.LB_BOT_INDEX_TTL_DAYS * self.DAY
        keys = [f"mb-{i}" for i in range(400)]
        vals = [bot._auto_index_stale_after(k) for k in keys]
        self.assertEqual(vals, [bot._auto_index_stale_after(k) for k in keys])
        for v in vals:
            self.assertTrue(ttl <= v <= ttl * 1.25, v / ttl)
        self.assertGreater(max(vals) - min(vals), ttl * 0.2, "actually spread")
        self.assertEqual(bot._auto_index_stale_after("mb-x"),
                         ttl * (1 + (zlib.crc32(b"mb-x") % 1000) / 4000))

    def test_select_applies_the_jitter_to_age_only(self):
        ttl = bot.LB_BOT_INDEX_TTL_DAYS * self.DAY
        key = next(k for k in (f"mb-j{i}" for i in range(1000))
                   if bot._auto_index_stale_after(k) > ttl * 1.1)
        artists = [self.nd("1", "Jittered", key), self.nd("2", "Missing"),
                   self.nd("3", "Stub", "mb-stub"), self.nd("4", "Old scanner", "mb-v1")]
        meta = [self.meta(key, "1", scanned_at=self.now - ttl * 1.05),
                self.meta("mb-stub", "", scanned_at=0, scan_version=0),
                self.meta("mb-v1", "4", scanned_at=self.now - self.DAY, scan_version=1)]
        picked = bot._auto_index_select(artists, meta, self.now, {})
        self.assertEqual([(c["nd_id"], c["reason"]) for c in picked],
                         [("2", "missing"), ("3", "stub"), ("4", "stale")],
                         "past the plain TTL but inside its own jitter: not yet; "
                         "missing, stubs and a scan_version bump at full speed")
        meta[0]["scanned_at"] = self.now - ttl * 1.26
        self.assertIn("1", [c["nd_id"] for c in bot._auto_index_select(artists, meta, self.now, {})])

    def test_the_workers_scan_reaches_the_review_so_the_group_id_resolves(self):
        """R36 (reverting R32): a partly-owned album's index row carries a
        `group_id` that the clients open as `/lb/gap`. A worker that stored
        the row without unioning its review group left that handle answering
        404 "Group not found" until a manual build — so the worker unions its
        scan's review groups exactly like the manual build does."""
        group = {"id": "g-auto-1", "artist": "Has Gaps", "album": "X",
                 "missing_tracks": [{"title": "T3", "mbid": "rec-3"}]}
        releases = [{"rgid": "rg-inc", "title": "X", "status": "incomplete",
                     "group_id": "g-auto-1", "present": 2, "total": 3}]
        state = self.state_with([self.nd("11", "Has Gaps", "mb-11")])
        with self.patched(build_artist_discography=self.discog_with(
                releases=releases, review_groups=[group])), \
                patch.object(bot, "_save_review_state", lambda *a, **k: None):
            self.assertEqual(bot._auto_index_tick(state, self.now, self.USER), "indexed")
        row = bot._index_get_artist("mb-11", "11")["releases"][0]
        with bot._review_lock:
            found = bot._find_review_group(row["group_id"])
        self.assertIsNotNone(found, "the row's group_id must resolve in the review")
        self.assertEqual(found["album"], "X")

    def test_a_miss_stub_refresh_keeps_its_own_present_rows(self):
        """M4: a fill under an unresolvable artist marks `present` on its
        `nd:` stub. The stub's refresh (no releases) was DELETE-then-insert,
        and `kept` only merged into rows the scan produced — so the filled
        album was un-owned and offered for download again."""
        bot._index_store_unresolved_artist("Nobody", "30")
        bot._index_mark_release_present(rgid="rg-f", artist_key="nd:30", title="Filled")
        self.assertTrue(bot._index_store_unresolved_artist("Nobody", "30"))
        rows = {r["rgid"]: r["status"] for r in bot._index_get_artist("", "30")["releases"]}
        self.assertEqual(rows, {"rg-f": "present"})

    def test_a_rescan_that_no_longer_lists_a_filled_release_keeps_it(self):
        bot._index_store_artist({"artist_mbid": "mb-95", "artist_name": "A",
                                 "releases": [{"rgid": "rg-a", "title": "A",
                                               "status": "missing"}]}, "95")
        bot._index_mark_release_present(rgid="rg-x", artist_key="mb-95", title="X")
        bot._index_store_artist({"artist_mbid": "mb-95", "artist_name": "A",
                                 "releases": [{"rgid": "rg-a", "title": "A",
                                               "status": "missing"}]}, "95")
        rows = {r["rgid"]: r["status"] for r in bot._index_get_artist("mb-95", "95")["releases"]}
        self.assertEqual(rows, {"rg-a": "missing", "rg-x": "present"})

    # -- the thread ----------------------------------------------------------

    def test_worker_is_a_noop_when_disabled_or_navidrome_unconfigured(self):
        ticks = []
        sleeper = lambda s: (_ for _ in ()).throw(AssertionError("must not loop"))
        with self.patched(_auto_index_tick=lambda *a: ticks.append(a) or "idle"):
            with patch.object(bot, "LB_BOT_AUTO_INDEX", False):
                bot._auto_index_worker(clock=lambda: 0.0, sleeper=sleeper)
            with patch.object(bot, "USERS", [{"navidrome_user": "", "navidrome_password": ""}]):
                bot._auto_index_worker(clock=lambda: 0.0, sleeper=sleeper)
        self.assertEqual(ticks, [])

    def test_worker_loop_sleeps_only_when_there_is_nothing_to_scan(self):
        outcomes = iter(["indexed", "unresolved", "idle", "paused", "indexed"])
        sleeps = []

        def sleeper(s):
            sleeps.append(s)
            if len(sleeps) >= 2:
                raise SystemExit("stop")

        with self.patched(_auto_index_tick=lambda *a: next(outcomes)), \
                patch.object(bot, "LB_BOT_AUTO_INDEX", True), \
                patch.object(bot, "USERS", [self.USER]):
            with self.assertRaises(SystemExit):
                bot._auto_index_worker(clock=lambda: 0.0, sleeper=sleeper)
        self.assertEqual(sleeps, [bot.AUTO_INDEX_IDLE_SECS] * 2)



class UiTruthfulnessTests(unittest.TestCase):
    """The web UI rendered these states as something they were not: a months-old
    unplaced download as "downloading", every waiting album as "source ready",
    and artist-only name overlap as a one-tap placement suggestion."""

    NOW = 2_000_000_000.0

    def _group(self, decisions, updated_at, **extra):
        return {"id": "g", "canonical_album_id": "al", "albums": [{"id": "al"}],
                "updated_at": updated_at,
                "missing_tracks": [{"decision": d} for d in decisions], **extra}

    def test_fresh_downloaded_group_still_reads_as_working(self):
        g = self._group(["placed"], self.NOW - 60)
        g["missing_tracks"].append({"decision": "downloaded",
                                    "downloaded_at": self.NOW - 60})
        self.assertEqual(bot._gap_status_for_group(g, now=self.NOW), "downloading")

    def test_touching_the_group_does_not_unstick_it(self):
        """Skip, Unhide, allow-MP3, a rescan and a source search all bump the
        group's updated_at without placing anything; the album used to read
        "working" for another 30 minutes after each."""
        g = self._group(["placed"], self.NOW - 1)
        g["missing_tracks"].append({"decision": "downloaded",
                                    "downloaded_at": self.NOW - 2 * bot.DOWNLOADED_STALE_SECS})
        self.assertEqual(bot._gap_status_for_group(g, now=self.NOW), "failed")

    def test_re_marking_a_downloaded_track_is_not_progress(self):
        old = self.NOW - 3600
        track = {"decision": "downloaded", "downloaded_at": old}
        bot._stamp_downloaded(track, "downloaded", at=self.NOW)
        self.assertEqual(track["downloaded_at"], old)
        fresh = {"decision": "downloaded"}
        bot._stamp_downloaded(fresh, "downloading", at=self.NOW)
        self.assertEqual(fresh["downloaded_at"], self.NOW)

    def test_stale_downloaded_group_reads_as_failed(self):
        g = self._group(["downloaded", "placed"],
                        self.NOW - bot.DOWNLOADED_STALE_SECS - 1)
        self.assertEqual(bot._gap_status_for_group(g, now=self.NOW), "failed")

    def test_a_recently_reconciled_track_keeps_an_old_group_in_flight(self):
        # The reconcile pass stamps the track, not the group.
        g = self._group(["placed"], self.NOW - 30 * 86400)
        g["missing_tracks"].append({"decision": "downloaded",
                                    "downloaded_at": self.NOW - 60})
        self.assertEqual(bot._gap_status_for_group(g, now=self.NOW), "downloading")

    def test_attention_is_everything_but_complete(self):
        for status in ("ready", "picking", "downloading", "failed"):
            self.assertTrue(bot._group_needs_attention(status))
        self.assertFalse(bot._group_needs_attention("complete"))

    def test_album_view_reports_source_count_from_either_shape(self):
        with patch.object(bot, "_nd_album_artist_map", return_value={}):
            listed = bot._album_view({"id": "g", "source_count": 3}, {})
            full = bot._album_view(
                {"id": "g", "source_results": {"folders": [{}, {}]}}, {})
            none = bot._album_view({"id": "g"}, {})
        self.assertEqual((listed["sourceCount"], full["sourceCount"],
                          none["sourceCount"]), (3, 2, 0))

    def _suggest(self, folder_name, groups, tags=None):
        folder = {"name": folder_name, "path": "/downloads/" + folder_name}
        files = [{"path": "/x.flac"}] if tags else []
        with patch.object(bot, "_audio_files_in_folder", return_value=files), \
                patch.object(bot, "_audio_file_tags", return_value=tags or {}):
            return bot._suggest_review_group_for_folder(folder, groups)

    def _grp(self, artist, album, missing=3, gid="g1"):
        return {"id": gid, "artist": artist, "album": album,
                "canonical_mbid": "mb-" + gid,
                "missing_tracks": [{"decision": "pending"}] * missing}

    def test_artist_only_overlap_is_not_a_suggestion(self):
        self.assertIsNone(self._suggest(
            "Led Zeppelin IV", [self._grp("Led Zeppelin", "Coda")]))

    def test_one_shared_word_in_a_long_folder_name_is_not_a_suggestion(self):
        self.assertIsNone(self._suggest(
            "Speakerboxxx _ The Love Below", [self._grp("Love", "Love")]))

    def test_the_right_album_is_still_suggested_and_is_likely(self):
        s = self._suggest("The Great Escape (1995)",
                          [self._grp("Blur", "The Great Escape")],
                          tags={"albumartist": "Blur", "album": "The Great Escape"})
        self.assertEqual(s["group_id"], "g1")
        self.assertEqual(s["basis"], "tags")
        self.assertEqual(bot._suggestion_confidence(s, file_count=3), "likely")

    def test_a_folder_name_only_partial_match_is_possible_not_likely(self):
        s = self._suggest("Blur - Great Escape", [self._grp("Blur", "The Great Escape")])
        self.assertIsNotNone(s)
        self.assertEqual(bot._suggestion_confidence(s, file_count=3), "possible")

    def test_a_folder_far_bigger_than_the_gap_is_only_possible(self):
        s = {"score": 1.2, "missing": 10, "basis": "tags"}
        self.assertEqual(bot._suggestion_confidence(s, file_count=49), "possible")
        self.assertEqual(bot._suggestion_confidence(s, file_count=12), "likely")
        self.assertEqual(bot._suggestion_confidence(None), "unknown")


class GapsHiddenViewTests(unittest.TestCase):
    """A skip used to be irreversible from the web UI: hidden groups were dropped
    from every list and nothing could list them."""

    def _view(self, **kw):
        groups = [
            {"id": "a", "artist": "A", "album": "One", "missing_tracks": [{"decision": "pending"}]},
            {"id": "b", "artist": "B", "album": "Two", "hidden": True,
             "missing_tracks": [{"decision": "pending"}]},
        ]
        snap = {"groups": groups, "status": "", "message": "", "updated_at": 0, "tasks": {}}
        with patch.object(bot, "_review_list_snapshot", return_value=snap), \
                patch.object(bot, "_nd_album_artist_map", return_value={}), \
                patch.object(bot, "_groups_with_running_source_search", return_value=set()), \
                patch.object(bot, "_active_scan_task_view", return_value=None):
            return bot._gaps_view(**kw)

    def test_default_list_leaves_hidden_out_but_counts_them(self):
        v = self._view()
        self.assertEqual([i["id"] for i in v["items"]], ["a"])
        self.assertEqual(v["counts"]["hidden"], 1)
        self.assertEqual(v["counts"]["all"], 1)

    def test_hidden_list_has_only_hidden_and_the_same_counts(self):
        v = self._view(hidden=True)
        self.assertEqual([i["id"] for i in v["items"]], ["b"])
        self.assertTrue(v["items"][0]["hidden"])
        self.assertEqual(v["counts"]["all"], 1)
        self.assertEqual(v["counts"]["hidden"], 1)


class LogTeeTests(unittest.TestCase):
    def test_complete_lines_reach_the_ring_and_web_echo_does_not(self):
        sink = io.StringIO()
        tee = bot._LogTee(sink)
        with patch.object(bot, "_stdout_events", []) as ring, \
                patch.object(bot, "_web_events", []) as web, \
                patch.object(sys, "stdout", tee):
            tee.write("  placement: 2 track(s) did not land\npartial")
            tee.write(" line\n\n")
            bot._web_log("already recorded\nsecond line of it")
            msgs = [e["msg"] for e in ring]
            web_msgs = [e["msg"] for e in web]
        self.assertEqual(sink.getvalue(),
                         "  placement: 2 track(s) did not land\npartial line\n\n"
                         "  web: already recorded\nsecond line of it\n")
        # Every line of a multi-line _web_log echo is skipped, not just the first.
        self.assertEqual(msgs, ["placement: 2 track(s) did not land", "partial line"])
        self.assertEqual(web_msgs, ["already recorded\nsecond line of it"])

    def test_lines_from_two_threads_are_not_glued(self):
        """print() is two writes; with one shared buffer another thread's text
        landed between them, and a glued line that began with a _web_log echo
        was dropped whole, taking the other thread's error with it."""
        sink = io.StringIO()
        tee = bot._LogTee(sink)
        other_wrote = threading.Event()

        def other():
            tee.write("  slskd enqueue exception: peer refused")
            tee.write("\n")
            other_wrote.set()

        with patch.object(bot, "_stdout_events", []) as ring:
            tee.write("  first half of a line")
            t = threading.Thread(target=other)
            t.start()
            other_wrote.wait(5)
            t.join(5)
            tee.write("\n")
            msgs = [e["msg"] for e in ring]
        self.assertEqual(sorted(msgs), ["first half of a line",
                                        "slskd enqueue exception: peer refused"])

    def test_ansi_colour_codes_are_stripped_before_recording(self):
        sink = io.StringIO()
        tee = bot._LogTee(sink)
        with patch.object(bot, "_stdout_events", []) as ring:
            tee.write("\x1b[32mplacement: 2 track(s) landed\x1b[0m\n")
            msgs = [e["msg"] for e in ring]
        # The raw stream is untouched -- only the recorded copy is cleaned.
        self.assertEqual(sink.getvalue(), "\x1b[32mplacement: 2 track(s) landed\x1b[0m\n")
        self.assertEqual(msgs, ["placement: 2 track(s) landed"])

    def test_stderr_access_log_lines_are_skipped(self):
        tee = bot._LogTee(io.StringIO(), src="stderr")
        with patch.object(bot, "_stdout_events", []) as ring:
            tee.write('127.0.0.1 - - [26/Sep/2026 12:00:00] "GET /api/summary HTTP/1.1" 200 -\n')
            tee.write("Traceback (most recent call last):\n")
            rows = [(e["msg"], e["severity"], e["src"]) for e in ring]
        self.assertEqual(rows, [("Traceback (most recent call last):", "error", "stderr")])

    def test_ring_is_bounded(self):
        with patch.object(bot, "_stdout_events", []) as ring, \
                patch.object(bot, "WEB_LOG_MAX", 5):
            for i in range(12):
                bot._web_event_append(f"line {i}")
            self.assertEqual([e["msg"] for e in ring],
                             [f"line {i}" for i in range(7, 12)])

    def test_printed_lines_do_not_evict_deliberate_events(self):
        with patch.object(bot, "_stdout_events", []), \
                patch.object(bot, "_web_events", []), \
                patch.object(bot, "WEB_LOG_MAX", 5):
            bot._web_event_append("placement refused", src="web")
            for i in range(50):
                bot._web_event_append(f"Checking: track {i}")
            self.assertEqual([e["msg"] for e in bot._web_log_events()],
                             ["placement refused"])
            merged = bot._log_ring_merged()
        self.assertEqual(merged[0]["msg"], "placement refused")
        self.assertEqual(len(merged), 6)

    def test_progress_lines_are_not_errors_and_tags_survive_redaction(self):
        with patch.object(bot, "_stdout_events", []) as ring:
            bot._web_event_append("Checking: Refused - New Noise")
            bot._web_event_append("Incomplete: The Exceptions - Album (3/10)")
            bot._web_event_append(
                "poll_downloads_loop iteration error: HTTPConnectionPool(host='slskd', port=5030)")
        self.assertEqual([(e["tag"], e["severity"]) for e in ring],
                         [("app", "info"), ("app", "info"), ("slskd", "error")])
        self.assertNotIn("host='slskd'", ring[2]["msg"])

    def test_severity_is_derived(self):
        with patch.object(bot, "_stdout_events", []) as ring:
            bot._web_event_append("slskd enqueue failed: 500")
            bot._web_event_append("WARNING: no downloads dir")
            bot._web_event_append("Navidrome scan started")
        self.assertEqual([(e["tag"], e["severity"]) for e in ring],
                         [("slskd", "error"), ("download", "warn"),
                          ("navidrome", "info")])


class RedactionTests(unittest.TestCase):
    def test_subsonic_query_auth_is_redacted(self):
        line = ("nd_get_all_artists error: HTTPConnectionPool(host='nd', port=4533): "
                "Max retries exceeded with url: /rest/getArtists?u=icher&t=52b47d7e8cc8&s=dc8ab8e7"
                "&p=enc:6869&v=1.16.1&c=listenbrainz-bot&f=json")
        out = bot._redact_secrets(line)
        for secret in ("icher", "52b47d7e8cc8", "dc8ab8e7", "6869"):
            self.assertNotIn(secret, out)
        self.assertIn("v=1.16.1", out)
        self.assertIn("c=listenbrainz-bot", out)

    def test_connection_pool_hosts_are_redacted(self):
        out = bot._redact_secrets("HTTPSConnectionPool(host='music.example.org', port=443): Read timed out.")
        self.assertNotIn("music.example.org", out)
        self.assertIn("host='[redacted-host]'", out)
        self.assertIn("host='localhost'", bot._redact_secrets("HTTPConnectionPool(host='localhost', port=1)"))

    def test_error_envelopes_tail_only_deliberate_events(self):
        with patch.object(bot, "_web_events", []):
            bot._web_event_append("some unrelated stdout line")
            bot._web_event_append("placement refused", src="web")
            tail = [e["msg"] for e in bot._web_log_events()]
        self.assertEqual(tail, ["placement refused"])


class SettingsCardsTests(unittest.TestCase):
    def test_library_path_failure_does_not_flag_navidrome_credentials(self):
        checks = [(True, "Navidrome", "200"),
                  (False, "Library path ↔ Navidrome", "stale")]
        with patch.object(bot, "_default_web_user", return_value={}):
            cards = {c["key"]: c for c in bot._settings_cards({}, checks)["cards"]}
        self.assertTrue(cards["navidrome"]["ok"])
        checks = [(False, "Navidrome", "401")]
        cards = {c["key"]: c for c in bot._settings_cards({}, checks)["cards"]}
        self.assertFalse(cards["navidrome"]["ok"])



class SystemStatusViewTests(unittest.TestCase):
    def test_reports_workers_and_integrations_without_secrets(self):
        with patch.object(bot, "_default_web_user", return_value={
                    "listenbrainz_user": "u",
                    "playlist_sources": {"a": "Weekly Jams"}}), \
                patch.object(bot, "_index_head", return_value=(42, "1-x")), \
                patch.object(bot, "_rejected_sources_count", return_value=3), \
                patch.object(bot, "ACOUSTID_API_KEY", "secret-key"), \
                patch.object(bot, "HUB_NOTIFY_URL", "http://hub.lan:4790"), \
                patch.object(bot, "HUB_NOTIFY_TOKEN", "tok"), \
                patch.dict(bot._auto_index_public, {"outcome": "idle"}, clear=True):
            v = bot._system_status_view()
        self.assertEqual(v["hubPush"]["headSeq"], 42)
        self.assertTrue(v["hubPush"]["configured"])
        self.assertEqual(v["acoustid"]["rejectedSources"], 3)
        self.assertTrue(v["acoustid"]["keySet"])
        self.assertEqual(v["autoIndex"]["outcome"], "idle")
        self.assertEqual(v["listenbrainz"]["playlists"], ["Weekly Jams"])
        self.assertNotIn("secret-key", json.dumps(v))
        self.assertNotIn("hub.lan", json.dumps(v))


class ReviewFixRoundTests(unittest.TestCase):
    """The fixes from the 2026-09-26 max review of the ui-polish round."""

    # ── placement suggestions ───────────────────────────────────────────────
    def _suggest(self, folder_name, groups, tags=None):
        folder = {"name": folder_name, "path": "/downloads/" + folder_name,
                  "file_count": 8}
        files = [{"path": "/x.flac"}] if tags else []
        with patch.object(bot, "_audio_files_in_folder", return_value=files), \
                patch.object(bot, "_audio_file_tags", return_value=tags or {}), \
                patch.dict(bot._folder_tag_tokens_cache, {}, clear=True):
            return bot._suggest_review_group_for_folder(folder, groups)

    def _grp(self, artist, album, gid, missing=3):
        return {"id": gid, "artist": artist, "album": album,
                "canonical_mbid": "mb-" + gid,
                "missing_tracks": [{"decision": "pending"}] * missing}

    def test_a_self_titled_group_is_never_a_one_tap_for_another_album(self):
        s = self._suggest("Led Zeppelin IV", [self._grp("Led Zeppelin", "Led Zeppelin", "st")],
                          tags={"albumartist": "Led Zeppelin", "album": "Led Zeppelin IV"})
        self.assertTrue(s is None or bot._suggestion_confidence(s, 8) == "possible")

    def test_the_right_album_beats_a_self_titled_one_on_a_tie(self):
        groups = [self._grp("Weezer", "Weezer", "st"), self._grp("Weezer", "Pinkerton", "pk")]
        s = self._suggest("Weezer - Pinkerton", groups)
        self.assertEqual(s["group_id"], "pk")

    def test_a_folder_name_only_match_is_never_likely(self):
        s = self._suggest("Radiohead - OK Computer", [self._grp("Radiohead", "OK Computer", "ok")])
        self.assertEqual(s["basis"], "folder name")
        self.assertEqual(bot._suggestion_confidence(s, 8), "possible")

    def test_edition_and_format_words_do_not_block_a_match(self):
        s = self._suggest("Blur - Parklife (1994) [FLAC 24bit 96kHz]",
                          [self._grp("Blur", "Parklife", "pl")],
                          tags={"albumartist": "Blur",
                                "album": "Parklife (Special Edition) [2012 Remaster]"})
        self.assertEqual(s["group_id"], "pl")
        self.assertEqual(bot._suggestion_confidence(s, 8), "likely")
        s = self._suggest("Adele - 25 (Deluxe Edition) [FLAC]", [self._grp("Adele", "25", "a25")])
        self.assertEqual(s["group_id"], "a25")

    # ── review merges ───────────────────────────────────────────────────────
    @contextlib.contextmanager
    def _review(self, groups):
        old_file, old_state = bot.REVIEW_FILE, bot._review_snapshot()
        try:
            with tempfile.TemporaryDirectory() as td:
                bot.REVIEW_FILE = os.path.join(td, "review.json")
                with bot._review_lock:
                    bot._review_state = bot._empty_review_state()
                    bot._review_state["groups"] = groups
                with patch.object(bot, "_index_db", side_effect=RuntimeError("no db")):
                    yield
        finally:
            bot.REVIEW_FILE = old_file
            with bot._review_lock:
                bot._review_state = old_state

    @staticmethod
    def _g(gid, **extra):
        g = {"id": gid, "canonical_album_id": "", "merge_mode": "", "match_mode": "auto",
             "missing_tracks": [], "messages": []}
        g.update(extra)
        return g

    def test_a_rescan_keeps_the_users_decisions(self):
        prev = self._g("L", origin="library", allow_mp3=True, no_source_reason="only mp3",
                       missing_tracks=[{"mbid": "r1", "title": "t1", "decision": "failed",
                                        "manual_pick": {"username": "u", "filename": "f"},
                                        "can_force_place": True}])
        with self._review([prev]):
            bot._union_review_groups([self._g(
                "L", missing_tracks=[{"mbid": "r1", "title": "t1", "decision": "pending"}])])
            g = bot._review_snapshot()["groups"][0]
        self.assertTrue(g["allow_mp3"])
        self.assertEqual(g["no_source_reason"], "only mp3")
        self.assertEqual(g["missing_tracks"][0]["manual_pick"]["username"], "u")
        self.assertTrue(g["missing_tracks"][0]["can_force_place"])

    def test_a_union_folds_a_second_origin_into_the_richer_row(self):
        playlist = self._g("P", origin="playlist", canonical_mbid="rel-1",
                           missing_tracks=[{"mbid": "a", "title": "A", "decision": "pending"}])
        library = self._g("L", origin="library", canonical_mbid="rel-1",
                          albums=[{"id": "al"}], canonical_album_id="al",
                          missing_tracks=[{"mbid": "b", "title": "B", "decision": "pending"}])
        with self._review([playlist]):
            bot._union_review_groups([library])
            groups = bot._review_snapshot()["groups"]
        self.assertEqual([g["id"] for g in groups], ["L"])
        self.assertEqual({t["title"] for t in groups[0]["missing_tracks"]}, {"A", "B"})

    def test_groups_that_name_no_album_are_not_folded_together(self):
        with self._review([self._g("X", origin="playlist")]):
            bot._union_review_groups([self._g("Y")])
            ids = [g["id"] for g in bot._review_snapshot()["groups"]]
        self.assertEqual(ids, ["X", "Y"])

    def test_refreshing_a_group_without_albums_changes_nothing(self):
        g = {"id": "R", "albums": [], "canonical_mbid": "rel-1", "artist": "A",
             "album": "B", "missing_tracks": [{"title": "t", "decision": "downloaded"}]}
        before = json.loads(json.dumps(g))
        self.assertEqual(bot.refresh_group_missing(g), before)

    def test_a_stuck_group_can_be_downloaded_again(self):
        g = {"id": "S", "missing_tracks": [
            {"title": "t", "decision": "downloaded", "local_path": "/downloads/x.flac",
             "downloaded_at": time.time() - 2 * bot.DOWNLOADED_STALE_SECS}]}
        with patch.object(bot, "_review_group_next_action",
                          return_value={"bucket": "downloaded"}):
            self.assertEqual(bot._approve_pending_missing_tracks(g), 1)
        self.assertEqual(g["missing_tracks"][0]["decision"], "approved")
        self.assertNotIn("local_path", g["missing_tracks"][0])

    def test_a_fresh_download_is_left_alone(self):
        g = {"id": "S", "missing_tracks": [
            {"title": "t", "decision": "downloaded", "downloaded_at": time.time()}]}
        with patch.object(bot, "_review_group_next_action",
                          return_value={"bucket": "downloaded"}):
            self.assertEqual(bot._approve_pending_missing_tracks(g), 0)
        self.assertEqual(g["missing_tracks"][0]["decision"], "downloaded")

    # ── small ones ──────────────────────────────────────────────────────────
    def test_missing_sort_breaks_ties_a_to_z(self):
        key, reverse = bot._LIBRARY_SORTS["missing"]
        rows = [{"artist": "Zed", "total": 10, "present": 5},
                {"artist": "Abba", "total": 10, "present": 5},
                {"artist": "Mid", "total": 10, "present": 1}]
        rows.sort(key=key, reverse=reverse)
        self.assertEqual([r["artist"] for r in rows], ["Mid", "Abba", "Zed"])

    def test_a_short_password_is_not_redacted_out_of_paths(self):
        with patch.object(bot, "USERS", [{"navidrome_password": "music"}]):
            out = bot._redact_secrets("placed /music/A/B/01.flac; login music failed")
        self.assertIn("/music/A/B/01.flac", out)
        self.assertNotIn("login music", out)

    def test_task_snapshot_copies_one_task(self):
        old = bot._review_snapshot()
        try:
            with bot._review_lock:
                bot._review_state = bot._empty_review_state()
                bot._review_state["tasks"] = {"a": {"id": "a", "result": {"x": [1]}},
                                              "b": {"id": "b", "result": {"y": 2}}}
            t = bot._task_snapshot("a")
            t["result"]["x"].append(2)
            self.assertEqual(bot._review_state["tasks"]["a"]["result"]["x"], [1])
            self.assertIsNone(bot._task_snapshot("missing"))
        finally:
            with bot._review_lock:
                bot._review_state = old


def _web_app():
    """The real Flask app `start_web_dashboard` builds, without serving it or
    starting its background threads. The app is local to that function, so the
    Flask class is swapped for one that records the instance."""
    import flask

    apps = []

    class _Recording(flask.Flask):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            apps.append(self)

    class _NoThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    with patch.object(flask, "Flask", _Recording), \
            patch.object(bot, "WEB_UI_ENABLED", True), \
            patch.object(bot.threading, "Thread", _NoThread):
        bot.start_web_dashboard()
    return apps[0]


class DownloadsCancelRouteTests(unittest.TestCase):
    """B-021: `/api/downloads/cancel` detached the transfer but never touched
    the review track behind it, so a track cancelled from the Downloads page
    kept reading queued/downloading on the wire."""

    @classmethod
    def setUpClass(cls):
        cls.client = _web_app().test_client()

    def setUp(self):
        old_pending = bot.pending_downloads.copy()

        def restore():
            bot.pending_downloads.clear()
            bot.pending_downloads.update(old_pending)

        self.addCleanup(restore)
        bot.pending_downloads.clear()
        for name, value in (("_slskd_cancel", lambda *a, **k: True),
                            ("_save_state", lambda *a, **k: None)):
            patcher = patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_cancel_marks_the_review_track_cancelled(self):
        bot.pending_downloads[("peer", "Album\\01.flac")] = {
            "review_group_id": "g1", "review_track_index": 0}
        group = {"id": "g1", "artist": "A", "album": "B",
                 "missing_tracks": [{"title": "T1", "decision": "downloading"}]}
        with isolated_review():
            with bot._review_lock:
                bot._review_state["groups"] = [group]
            r = self.client.post("/api/downloads/cancel",
                                 json={"username": "peer", "filename": "Album\\01.flac"})
            self.assertEqual(r.status_code, 200)
            with bot._review_lock:
                snapshot = json.loads(json.dumps(bot._find_review_group("g1")))
        self.assertEqual(snapshot["missing_tracks"][0]["decision"], "cancelled")
        self.assertEqual(snapshot["missing_tracks"][0]["error"], "Cancelled",
                         "one text for every user-initiated cancel (Q-029)")
        self.assertNotIn(("peer", "Album\\01.flac"), bot.pending_downloads)

    def test_album_cancel_records_the_same_text(self):
        reasons = []
        with patch.object(bot, "_cancel_album_fill",
                          lambda mbid, reason: reasons.append(reason) or True), \
                patch.object(bot, "_album_fill_view", lambda mbid: {}):
            r = self.client.post("/api/album/cancel", json={"release_mbid": "rel1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(reasons, ["Cancelled"])

    def test_cancel_with_no_review_link_still_works(self):
        """Most Downloads-page rows carry no review group at all — the fix
        must not require one."""
        bot.pending_downloads[("peer", "solo.flac")] = {}
        r = self.client.post("/api/downloads/cancel",
                             json={"username": "peer", "filename": "solo.flac"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(("peer", "solo.flac"), bot.pending_downloads)


class DecisionsRouteTests(unittest.TestCase):
    """B-022: re-approving a track by hand must shed its old failure, or the
    retry reads as already failed. The repair-job rebuild used to do that as a
    side effect of every route that touched a job; the route does it itself."""

    @classmethod
    def setUpClass(cls):
        cls.client = _web_app().test_client()

    def test_reapproving_a_failed_track_clears_its_error(self):
        group = {"id": "g1", "artist": "A", "album": "B", "missing_tracks": [
            {"title": "T1", "decision": "failed", "download_error": "peer rejected"},
            {"title": "T2", "decision": "failed", "download_error": "peer rejected"}]}
        with isolated_review(), \
                patch.object(bot, "_save_state", lambda *a, **k: None):
            with bot._review_lock:
                bot._review_state["groups"] = [group]
            r = self.client.post("/api/groups/g1/decisions",
                                 json={"tracks": [{"index": 0, "decision": "approved"}]})
            self.assertEqual(r.status_code, 200)
            with bot._review_lock:
                tracks = json.loads(json.dumps(
                    bot._find_review_group("g1")["missing_tracks"]))
        self.assertEqual(tracks[0]["decision"], "approved")
        self.assertNotIn("download_error", tracks[0])
        # The track that was not touched keeps its failure.
        self.assertEqual(tracks[1]["download_error"], "peer rejected")

class ArtistReleaseRouteTests(unittest.TestCase):
    """B-018: `/api/artist/release` resolved the artist key by mbid, then by the
    Navidrome id — and Navidrome keys artists by name, so a same-name library
    artist's nd id filed the release under the other artist's key."""

    @classmethod
    def setUpClass(cls):
        cls.client = _web_app().test_client()

    def setUp(self):
        import shutil
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, True)
        scratch_index(self, os.path.join(td, "index.db"))
        for name, value in (
                ("mbz_release_group_row",
                 lambda rgid: {"rgid": rgid, "title": "Bleach",
                               "artist_mbid": "mbid-us"}),
                ("_classify_release_group",
                 lambda rg, *a: ({"rgid": rg["rgid"], "title": rg["title"],
                                  "status": "missing"}, None))):
            patcher = patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _stored_under(self, rgid):
        with bot._index_lock:
            rows = bot._index_db().execute(
                "SELECT artist_key FROM release_groups WHERE rgid = ?",
                (rgid,)).fetchall()
        return [r["artist_key"] for r in rows]

    def test_refuses_a_same_name_artist_with_a_different_mbid(self):
        # The library's "Nirvana" (nd1) is the UK band; the release is the US one's.
        bot._index_ensure_artist("mbid-uk", artist_mbid="mbid-uk",
                                 nd_artist_id="nd1", name="Nirvana")
        r = self.client.post("/api/artist/release", json={
            "rgid": "rg1", "mbid": "mbid-us", "nd_id": "nd1", "external": True})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["code"], "artist_conflict")
        self.assertIn("error", r.get_json())
        self.assertEqual(self._stored_under("rg1"), [])

    def test_the_same_artist_is_still_filed(self):
        bot._index_ensure_artist("mbid-us", artist_mbid="mbid-us",
                                 nd_artist_id="nd1", name="Nirvana")
        r = self.client.post("/api/artist/release", json={
            "rgid": "rg1", "mbid": "mbid-us", "nd_id": "nd1", "external": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._stored_under("rg1"), ["mbid-us"])

    def test_an_nd_only_artist_with_no_mbid_on_record_is_no_conflict(self):
        bot._index_ensure_artist("nd:nd1", nd_artist_id="nd1", name="Nirvana")
        r = self.client.post("/api/artist/release", json={
            "rgid": "rg1", "mbid": "mbid-us", "nd_id": "nd1", "external": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._stored_under("rg1"), ["nd:nd1"])

    def test_no_mbid_in_the_request_is_not_checked(self):
        """The release-group's credit is not the caller's claim — a
        collaboration credits someone else first."""
        bot._index_ensure_artist("mbid-uk", artist_mbid="mbid-uk",
                                 nd_artist_id="nd1", name="Nirvana")
        r = self.client.post("/api/artist/release", json={
            "rgid": "rg1", "nd_id": "nd1", "external": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._stored_under("rg1"), ["mbid-uk"])


class ArtistReindexReconcileTests(unittest.TestCase):
    """B-040: a re-index only unions the groups it produces, so the artist's
    other groups were never looked at again — one completed by hand kept its
    missing track, one whose album was deleted stayed listed forever."""

    RESULT = {"artist_name": "The Beatles", "review_groups": [{"id": "made"}],
              "releases": [{"status": "complete", "navidrome_album_ids": ["al-1"]}]}

    def setUp(self):
        review = isolated_review()
        review.__enter__()
        self.addCleanup(review.__exit__, None, None, None)
        self.fresh, self.live, self.refreshed = ["al-1", "al-2"], {}, []

        def refresh(gid, live_albums=None):
            self.refreshed.append((gid, sorted(live_albums or {})))
            return True
        for name, value in (
                ("_nd_album_index", lambda force=False: [{"id": i} for i in self.fresh]),
                ("_live_transfer_indexes_by_group", lambda: self.live),
                ("refresh_group_albums_from_navidrome", refresh)):
            patcher = patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _g(gid, album_id, artist="The Beatles", origin="library", decision="pending"):
        return {"id": gid, "origin": origin, "artist": artist, "album": gid,
                "albums": [{"id": album_id}],
                "missing_tracks": [{"title": "t", "decision": decision}]}

    def _ids(self):
        return [g["id"] for g in bot._review_snapshot()["groups"]]

    def _seed(self, *groups):
        with bot._review_lock:
            bot._review_state["groups"] = list(groups)

    def test_existing_albums_are_refreshed_and_gone_ones_retired(self):
        self._seed(self._g("stale", "al-1"),
                   self._g("dead", "al-gone", decision="downloaded"),
                   self._g("made", "al-2"),               # the scan just rebuilt it
                   self._g("other", "al-gone2", artist="Someone Else"),
                   self._g("list", "al-gone3", origin="playlist"))
        out = bot._reconcile_artist_review_groups(self.RESULT)
        self.assertEqual(out, {"dropped": ["dead"], "refreshed": ["stale"]})
        self.assertEqual(self._ids(), ["stale", "made", "other", "list"])
        # One fresh album list, handed to every refresh.
        self.assertEqual(self.refreshed, [("stale", ["al-1", "al-2"])])
        # The flusher deletes a marked id that is no longer live.
        self.assertIn("dead", bot._review_dirty_groups)

    def test_the_scans_complete_verdict_never_drops_a_group(self):
        """Revolver (Super Deluxe) on the NAS: the index called its album complete
        while Navidrome held 1 of 63 tracks. The group's own matcher decides."""
        self._seed(self._g("deluxe", "al-1"))
        out = bot._reconcile_artist_review_groups(self.RESULT)
        self.assertEqual(out["dropped"], [])
        self.assertEqual(self._ids(), ["deluxe"])

    def test_no_album_list_changes_nothing(self):
        self.fresh = []
        self._seed(self._g("dead", "al-gone"), self._g("stale", "al-1"))
        self.assertEqual(bot._reconcile_artist_review_groups(self.RESULT),
                         {"dropped": [], "refreshed": []})
        self.assertEqual(self._ids(), ["dead", "stale"])

    def test_work_in_flight_is_left_alone(self):
        self.live = {"moving": {0}}
        self._seed(self._g("moving", "al-gone"),
                   self._g("queued", "al-gone2", decision="downloading"))
        self.assertEqual(bot._reconcile_artist_review_groups(self.RESULT),
                         {"dropped": [], "refreshed": []})
        self.assertEqual(self._ids(), ["moving", "queued"])

    def test_only_a_library_scan_reconciles(self):
        calls = []
        user = {"navidrome_user": "u", "navidrome_password": "p"}
        for name, value in (
                ("build_artist_discography", lambda *a, **k: dict(self.RESULT)),
                ("_reconcile_artist_review_groups", calls.append),
                ("_union_review_groups", lambda *a, **k: None),
                ("_index_store_artist", lambda *a, **k: None),
                ("_artist_scan_set", lambda *a, **k: None),
                ("_task_update", lambda *a, **k: None),
                ("_task_finish", lambda *a, **k: None),
                ("_notify_hub_library_change", lambda *a, **k: None)):
            patcher = patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        bot._artist_discography_task("t1", "mbid", "The Beatles", user, "mb:x",
                                     skip_library=True)
        self.assertEqual(calls, [])
        bot._artist_discography_task("t2", "mbid", "The Beatles", user, "nd-1")
        self.assertEqual(len(calls), 1)


class PlaylistLooseGroupIdTests(unittest.TestCase):
    """B-033: the playlist scan's "Loose tracks" group id was salted with the
    clock, so each rescan built a fresh all-`pending` group and deleted the old
    one — every carry keyed by group id (decisions, verified/filed_extra rows,
    live transfers) was lost."""

    SOLO = [{"title": "One", "artist": "A", "mbid": "r1"},
            {"title": "Two", "artist": "B", "mbid": "r2"}]

    def setUp(self):
        review = isolated_review()
        review.__enter__()
        self.addCleanup(review.__exit__, None, None, None)
        for name, value in (
                ("_default_web_user",
                 lambda: {"listenbrainz_user": "lbuser", "navidrome_user": "u",
                          "navidrome_password": "p"}),
                ("scan_user", lambda user: list(self.SOLO)),
                ("group_missing_by_album",
                 lambda missing, *a, **k: ([], [dict(t) for t in self.SOLO])),
                ("_task_update", lambda *a, **k: None),
                ("_task_finish", lambda *a, **k: None)):
            patcher = patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _loose(self):
        groups = [g for g in bot._review_snapshot()["groups"]
                  if g.get("group_type") == "tracks"]
        self.assertEqual(len(groups), 1)
        return groups[0]

    def _scan_at(self, clock):
        with patch.object(bot.time, "time", return_value=clock):
            bot._playlist_scan_task("t1")

    def test_a_rescan_keeps_the_loose_group_and_its_settled_rows(self):
        self._scan_at(1000.0)
        first = self._loose()
        with bot._review_lock:
            live = bot._find_review_group(first["id"])
            live["missing_tracks"][0]["decision"] = "verified"
            live["missing_tracks"][1]["decision"] = "filed_extra"
        self._scan_at(2000.0)
        second = self._loose()
        self.assertEqual(second["id"], first["id"])
        self.assertEqual([t["decision"] for t in second["missing_tracks"]],
                         ["verified", "filed_extra"])

    def test_a_group_minted_under_the_old_clock_id_keeps_its_id(self):
        with bot._review_lock:
            bot._review_state["groups"] = [{
                "id": "0ldc10ck5a1t3d00", "group_type": "tracks", "origin": "playlist",
                "artist": "Loose tracks", "album": "Playlist tracks",
                "canonical_album_id": "", "canonical_mbid": "", "albums": [],
                "merge_mode": "logical", "match_mode": "auto", "messages": [],
                "missing_tracks": [{**self.SOLO[0], "decision": "verified"},
                                   {**self.SOLO[1], "decision": "pending"}]}]
        self._scan_at(3000.0)
        g = self._loose()
        self.assertEqual(g["id"], "0ldc10ck5a1t3d00")
        self.assertEqual(g["missing_tracks"][0]["decision"], "verified")


class LooseTrackPlacementTests(unittest.TestCase):
    """B-017: a playlist's "Loose tracks" row (no recording MBID, or one with no
    MusicBrainz release) downloaded per track and then sat in /downloads. Filing
    its folder by hand through "Pick the release" worked, but nothing moved the
    row; and nothing ever filed it automatically, even when the file's own tags
    named its release."""

    RELEASE = "0b0b0b0b-1111-2222-3333-444455556666"

    @classmethod
    def setUpClass(cls):
        cls.client = _web_app().test_client()

    def setUp(self):
        import shutil
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.downloads = os.path.join(tmp, "downloads")
        self.folder = os.path.join(self.downloads, "peer folder")
        self.other_folder = os.path.join(self.downloads, "another peer")
        self.lib = os.path.join(tmp, "music")
        for d in (self.folder, self.other_folder, self.lib):
            os.makedirs(d)
        self.file = os.path.join(self.folder, "01 - One.flac")
        self.other_file = os.path.join(self.other_folder, "Other.flac")
        for path in (self.file, self.other_file):
            with open(path, "wb") as fh:
                fh.write(b"\0" * 16)
        review = isolated_review()
        review.__enter__()
        self.addCleanup(review.__exit__, None, None, None)
        self.verified = []
        for name, value in (
                ("SLSKD_DOWNLOAD_DIR", self.downloads),
                ("MUSIC_LIBRARY_PATH", self.lib),
                ("_default_web_user", lambda *a, **k: {}),
                ("mbz_release_tracks",
                 lambda mbid, *a, **k: [{"title": "One", "mbid": "r1", "position": 1},
                                        {"title": "Two", "mbid": "r2", "position": 2}]),
                ("mbz_release_display", lambda *a, **k: {}),
                ("rgid_from_release", lambda *a, **k: ""),
                ("mbz_get", lambda *a, **k: {"title": "Album",
                                             "artist-credit": [{"name": "Artist"}]}),
                ("_audio_signature", lambda path: {}),
                ("_nd_scan_after_import", lambda *a, **k: False),
                ("_push_gap", lambda *a, **k: None),
                ("_start_placement_verification", self.verified.append),
                # Placement runs as a background task; run it inline.
                ("_task_run", lambda kind, label, target, **k: target("t1") or "t1")):
            patcher = patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        now = time.time()
        with bot._review_lock:
            bot._review_state["groups"] = [{
                "id": "loose1", "group_type": "tracks", "origin": "playlist",
                "artist": "Loose tracks", "album": "Playlist tracks",
                "canonical_mbid": "", "canonical_album_id": "", "albums": [],
                "missing_tracks": [
                    {"title": "One", "artist": "Artist", "mbid": "r1",
                     "decision": "downloaded", "local_path": self.file,
                     "downloaded_at": now - 2 * bot.DOWNLOADED_STALE_SECS},
                    {"title": "Other", "artist": "Someone Else", "mbid": "r9",
                     "decision": "downloaded", "local_path": self.other_file,
                     "downloaded_at": now - 2 * bot.DOWNLOADED_STALE_SECS},
                ]}]

    def _rows(self):
        with bot._review_lock:
            return json.loads(json.dumps(
                bot._find_review_group("loose1")["missing_tracks"]))

    # --- the manual path: "Pick the release" ---------------------------------

    def test_pick_the_release_moves_the_loose_row_to_placed(self):
        """The SPA's Pick-the-release posts the folder and a release — no group."""
        r = self.client.post("/api/place-folder", json={
            "path": self.folder, "release_mbid": self.RELEASE,
            "artist": "Artist", "album": "Album"})
        self.assertEqual(r.status_code, 200)
        rows = self._rows()
        self.assertEqual(rows[0]["decision"], "placed")
        self.assertTrue(os.path.isfile(os.path.join(self.lib, "Artist", "Album", "01 - One.flac")))
        # The row in the folder nobody placed is untouched.
        self.assertEqual(rows[1]["decision"], "downloaded")
        self.assertEqual(self.verified, ["loose1"])

    def test_confirm_with_the_loose_group_moves_only_the_placed_row(self):
        """With the group id sent, the album path's _mark_group_tracks_placed
        judged every in-flight row against this one folder's result and marked
        the unrelated download failed "not included in placement result"."""
        import hashlib
        pid = hashlib.sha1(self.folder.encode("utf-8")).hexdigest()[:12]
        with patch.object(bot, "_download_folders_cached",
                          lambda force=False: [{"path": self.folder, "name": "peer folder",
                                                "file_count": 1}]):
            r = self.client.post(f"/api/placements/{pid}/confirm", json={
                "releaseMbid": self.RELEASE, "groupId": "loose1"})
        self.assertEqual(r.status_code, 200)
        rows = self._rows()
        self.assertEqual(rows[0]["decision"], "placed")
        self.assertEqual(rows[1]["decision"], "downloaded")
        self.assertNotIn("download_error", rows[1])

    def test_a_refused_file_keeps_its_row_and_says_why(self):
        with patch.object(bot, "_audio_signature",
                          lambda path: {"md5": "same", "own_mbids": set(),
                                        "own_title": "", "own_title_key": ""}):
            os.makedirs(os.path.join(self.lib, "Artist", "Album"))
            with open(os.path.join(self.lib, "Artist", "Album", "one.flac"), "wb") as fh:
                fh.write(b"\0" * 16)
            self.client.post("/api/place-folder", json={
                "path": self.folder, "release_mbid": self.RELEASE,
                "artist": "Artist", "album": "Album"})
        row = self._rows()[0]
        self.assertEqual(row["decision"], "downloaded")
        self.assertIn("already in the album", row["download_error"])
        self.assertTrue(os.path.isfile(self.file))

    def test_an_early_failure_with_no_per_file_still_stamps_the_row(self):
        """Q-017(d): the linker returns 0 for every early failure that produces
        an empty `per_file` (no MusicBrainz tracklist for the tagged release,
        here). Only the task got the error and the review row kept its old
        text -- worse, no text at all, since this row had never failed before.
        The row must stay `downloaded` (still pickable/placeable) but say why."""
        with patch.object(bot, "mbz_release_tracks_insisting", lambda *a, **k: []):
            r = self.client.post("/api/place-folder", json={
                "path": self.folder, "release_mbid": self.RELEASE,
                "artist": "Artist", "album": "Album"})
        self.assertEqual(r.status_code, 200)
        rows = self._rows()
        self.assertEqual(rows[0]["decision"], "downloaded")
        self.assertIn("tracklist", rows[0]["download_error"])
        self.assertTrue(os.path.isfile(self.file))
        # The row in the other folder, never touched by this placement, is
        # left exactly as it was.
        self.assertNotIn("download_error", rows[1])

    def test_an_early_failure_stamps_only_the_selected_file_when_narrowed(self):
        """`only_relpaths` (the loose-track "Pick the release" case) narrows
        which rows the early failure is written onto, same as a normal
        per-file refusal does."""
        stranger = self._add_stranger()
        with patch.object(bot, "mbz_release_tracks_insisting", lambda *a, **k: []):
            r = self.client.post("/api/place-folder", json={
                "path": self.folder, "release_mbid": self.RELEASE,
                "artist": "Artist", "album": "Album",
                "only_relpaths": ["01 - One.flac"]})
        self.assertEqual(r.status_code, 200)
        rows = self._rows()
        self.assertEqual(rows[0]["decision"], "downloaded")
        self.assertIn("tracklist", rows[0]["download_error"])
        # Row 2 (Stranger, same folder but not selected) is untouched.
        self.assertNotIn("download_error", rows[2])
        self.assertTrue(os.path.isfile(stranger))

    def test_an_early_failure_stamps_every_downloaded_row_of_an_album_group(self):
        """An album (non-loose) group's failure used to write nothing on any
        row -- the group's whole point of tracking several tracks toward one
        release means every `downloaded` row of that release was left stale."""
        album_dir = os.path.join(self.downloads, "empty peer")
        os.makedirs(album_dir)
        with bot._review_lock:
            bot._review_state["groups"].append({
                "id": "album1", "group_type": "", "origin": "library",
                "artist": "Artist", "album": "Album",
                "canonical_mbid": self.RELEASE, "canonical_album_id": "",
                "albums": [{"release_mbid": self.RELEASE}],
                "missing_tracks": [
                    {"title": "One", "mbid": "r1", "decision": "downloaded",
                     "local_path": os.path.join(album_dir, "01.flac")},
                    {"title": "Two", "mbid": "r2", "decision": "downloaded",
                     "local_path": os.path.join(album_dir, "02.flac")},
                    {"title": "Three", "mbid": "r3", "decision": "placed"},
                ]})
        # No audio files at all in the download folder -- an early failure
        # with a genuinely empty per_file.
        bot._deterministic_import_task("t1", album_dir, self.RELEASE,
                                       "Artist", "Album", group_id="album1")
        with bot._review_lock:
            tracks = json.loads(json.dumps(
                bot._find_review_group("album1")["missing_tracks"]))
        self.assertEqual(tracks[0]["decision"], "downloaded")
        # Q-021: the folder's own name, never its /downloads path.
        self.assertEqual(tracks[0]["download_error"], "No audio files found in empty peer")
        self.assertEqual(tracks[1]["decision"], "downloaded")
        self.assertEqual(tracks[1]["download_error"], "No audio files found in empty peer")
        # The already-placed row is untouched.
        self.assertNotIn("download_error", tracks[2])

    def test_an_album_groups_early_failure_stamps_only_rows_from_that_folder(self):
        """Final review M4: an album gap's downloaded rows can come from more
        than one download folder (a rescued leftover, a hand-picked file from
        another peer). A failure placing one folder said nothing about a row
        whose file sits in another — and stamping it pointed the user at the
        wrong folder's problem. And the stamp reached clients only on their
        next poll: one push per affected group now."""
        album_dir = os.path.join(self.downloads, "empty peer")
        os.makedirs(album_dir)
        with bot._review_lock:
            bot._review_state["groups"].append({
                "id": "album1", "group_type": "", "origin": "library",
                "artist": "Artist", "album": "Album",
                "canonical_mbid": self.RELEASE, "canonical_album_id": "",
                "albums": [{"release_mbid": self.RELEASE}],
                "missing_tracks": [
                    {"title": "One", "mbid": "r1", "decision": "downloaded",
                     "local_path": os.path.join(album_dir, "01.flac")},
                    {"title": "Two", "mbid": "r2", "decision": "downloaded",
                     "local_path": os.path.join(album_dir, "CD2", "02.flac")},
                    {"title": "Three", "mbid": "r3", "decision": "downloaded",
                     "local_path": os.path.join(self.other_folder, "03.flac")},
                    {"title": "Four", "mbid": "r4", "decision": "needs_match"},
                ]})
        pushed = []
        with patch.object(bot, "_push_gap", pushed.append):
            bot._deterministic_import_task("t1", album_dir, self.RELEASE,
                                           "Artist", "Album", group_id="album1")
        with bot._review_lock:
            tracks = json.loads(json.dumps(
                bot._find_review_group("album1")["missing_tracks"]))
        self.assertIn("No audio files found", tracks[0]["download_error"])
        # A subfolder of the failed folder (a disc) is that folder's.
        self.assertIn("No audio files found", tracks[1]["download_error"])
        # Another peer's folder, and a row with no file at all: untouched.
        self.assertNotIn("download_error", tracks[2])
        self.assertNotIn("download_error", tracks[3])
        self.assertEqual(pushed.count("album1"), 1, pushed)

    # --- Q-019: every file refused, so `per_file` is full but nothing placed ---

    @staticmethod
    def _sig_by_name(md5s):
        """`_audio_signature` with an md5 per file name: a download whose md5
        equals a library file's is refused as audio already in the album."""
        return lambda path: {"md5": md5s.get(os.path.basename(path), ""),
                             "own_mbids": set(), "own_title": "", "own_title_key": ""}

    def _duplicate_album(self, dirs):
        """Album gap `album1`: One and Two downloaded into `dirs` (one or two
        download folders), each a copy of a different file already in the
        album; Three in another peer's folder; Four with no file at all."""
        album_lib = os.path.join(self.lib, "Artist", "Album")
        os.makedirs(album_lib)
        for name in ("one.flac", "two.flac"):
            with open(os.path.join(album_lib, name), "wb") as fh:
                fh.write(b"\0" * 16)
        one = os.path.join(dirs[0], "01 - One.flac")
        two = os.path.join(dirs[-1], "CD2", "02 - Two.flac")
        for path in (one, two):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"\0" * 16)
        with bot._review_lock:
            bot._review_state["groups"].append({
                "id": "album1", "group_type": "", "origin": "library",
                "artist": "Artist", "album": "Album",
                "canonical_mbid": self.RELEASE, "canonical_album_id": "",
                "albums": [{"release_mbid": self.RELEASE}],
                "missing_tracks": [
                    {"title": "One", "mbid": "r1", "decision": "downloaded",
                     "local_path": one},
                    {"title": "Two", "mbid": "r2", "decision": "downloaded",
                     "local_path": two},
                    {"title": "Three", "mbid": "r3", "decision": "downloaded",
                     "local_path": os.path.join(self.other_folder, "03.flac")},
                    {"title": "Four", "mbid": "r4", "decision": "needs_match"},
                ]})
        return self._sig_by_name({"one.flac": "a", "01 - One.flac": "a",
                                  "two.flac": "b", "02 - Two.flac": "b"})

    def _album1_tracks(self):
        with bot._review_lock:
            return json.loads(json.dumps(
                bot._find_review_group("album1")["missing_tracks"]))

    def _assert_each_row_says_its_own_refusal(self, tracks):
        # Each row its own reason — Two's names two.flac, which the result's
        # one-line error ("Nothing placed: <first reason>") never does.
        self.assertEqual(tracks[0]["download_error"],
                         "placement failed: this audio is already in the album as one.flac")
        self.assertEqual(tracks[1]["download_error"],
                         "placement failed: this audio is already in the album as two.flac")
        # The decision is the user's to act on, unchanged (Q-017(d)).
        self.assertEqual([t["decision"] for t in tracks[:2]], ["downloaded", "downloaded"])
        self.assertNotIn("can_force_place", tracks[0])
        # Another peer's folder, and a row with no file: untouched.
        self.assertNotIn("download_error", tracks[2])
        self.assertNotIn("download_error", tracks[3])

    def test_an_album_groups_full_refusal_stamps_each_row_with_its_own_reason(self):
        """Q-019: an album placement whose every file was refused (the
        duplicate-audio guard here) fails with a FULL `per_file`, so the
        Q-017(d) stamp — gated on an empty one — never ran, and no row said
        why its file was still in /downloads."""
        album_dir = os.path.join(self.downloads, "dup peer")
        sig = self._duplicate_album([album_dir])
        pushed = []
        with patch.object(bot, "_audio_signature", sig), \
                patch.object(bot, "_push_gap", pushed.append):
            bot._deterministic_import_task("t1", album_dir, self.RELEASE,
                                           "Artist", "Album", group_id="album1")
        self._assert_each_row_says_its_own_refusal(self._album1_tracks())
        self.assertEqual(pushed.count("album1"), 1, pushed)

    def test_a_finalized_album_fill_that_places_nothing_stamps_its_rows(self):
        """Q-019, the poller's path: `_finalize_group`'s failure branch wrote
        only the ledger. One stamp per import dir it tried — a file that failed
        over to another peer lands in that peer's folder."""
        dirs = [os.path.join(self.downloads, "dup peer"),
                os.path.join(self.downloads, "second peer")]
        sig = self._duplicate_album(dirs)
        saved = (bot.pending_album_groups.copy(), bot._album_fill_status.copy(),
                 dict(bot._albums))

        def restore():
            for live, old in zip((bot.pending_album_groups, bot._album_fill_status,
                                  bot._albums), saved):
                live.clear()
                live.update(old)

        self.addCleanup(restore)
        bot.pending_album_groups.clear()
        bot._album_fill_status.clear()
        bot.pending_album_groups["ag1"] = {
            "label": "Artist - Album", "total": 2, "completed": 2, "failed": 0,
            "local_dirs": {dirs[0]: 1, dirs[1]: 1}, "token": "tok", "chat_id": "chat",
            "release_mbid": self.RELEASE, "artist": "Artist", "album": "Album",
            "review_group_id": "album1", "match_mode": "auto"}
        with patch.object(bot, "_audio_signature", sig), \
                patch.object(bot, "_save_state", lambda *a, **k: None), \
                patch.object(bot, "_album_action_markup", lambda *a, **k: None), \
                patch.object(bot, "_tg_send", new_callable=AsyncMock):
            bot._album_fill_set(self.RELEASE, "downloading")
            asyncio.run(bot._finalize_group(None, "ag1"))
        self.assertEqual(bot._album_fill_get(self.RELEASE)["state"], "failed")
        self._assert_each_row_says_its_own_refusal(self._album1_tracks())

    def test_a_loose_early_failure_pushes_the_rows_group(self):
        pushed = []
        with patch.object(bot, "mbz_release_tracks_insisting", lambda *a, **k: []), \
                patch.object(bot, "_push_gap", pushed.append):
            r = self.client.post("/api/place-folder", json={
                "path": self.folder, "release_mbid": self.RELEASE,
                "artist": "Artist", "album": "Album"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("tracklist", self._rows()[0]["download_error"])
        self.assertEqual(pushed.count("loose1"), 1, pushed)

    def _add_stranger(self):
        """A second peer's unrelated loose track, dropped by slskd into the same
        download folder as row 0's file."""
        stranger = os.path.join(self.folder, "Stranger.flac")
        with open(stranger, "wb") as fh:
            fh.write(b"\1" * 16)
        with bot._review_lock:
            bot._find_review_group("loose1")["missing_tracks"].append(
                {"title": "Stranger", "artist": "Nobody Related", "mbid": "r7",
                 "decision": "downloaded", "local_path": stranger,
                 "downloaded_at": time.time()})
        return stranger

    def test_pick_the_release_for_one_loose_file_files_only_that_file(self):
        """Review A3 #2: picking release A for track A used to file the whole
        folder — the other peer's track B too, retagged as A's."""
        stranger = self._add_stranger()
        r = self.client.post("/api/place-folder", json={
            "path": self.folder, "release_mbid": self.RELEASE,
            "artist": "Artist", "album": "Album",
            "only_relpaths": ["01 - One.flac"]})
        self.assertEqual(r.status_code, 200)
        rows = self._rows()
        self.assertEqual(rows[0]["decision"], "placed")
        self.assertTrue(os.path.isfile(stranger), "the unrelated file stays put")
        self.assertEqual(rows[2]["decision"], "downloaded")
        self.assertNotIn("download_error", rows[2])

    def test_place_folder_refuses_a_selection_outside_the_folder(self):
        r = self.client.post("/api/place-folder", json={
            "path": self.folder, "release_mbid": self.RELEASE,
            "only_relpaths": ["../another peer/Other.flac"]})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(os.path.isfile(self.other_file))
        self.assertEqual(self._rows()[1]["decision"], "downloaded")

    def test_a_bonus_file_is_never_linked_as_placed(self):
        """Filing the whole folder files the stranger as a bonus track of the
        picked release. That is a misfile, and must not read as a placement the
        verifier then confirms by title — but the file IS in the library now.
        B-025 (c): it used to stay `downloaded` with its `local_path` pointing
        at a moved file, and the next fetch re-approved and downloaded it again.
        It settles instead, `done` on the wire, with a note naming the release
        (ruling R-A: artist – album, never a path)."""
        self._add_stranger()
        self.client.post("/api/place-folder", json={
            "path": self.folder, "release_mbid": self.RELEASE,
            "artist": "Artist", "album": "Album"})
        rows = self._rows()
        self.assertEqual(rows[0]["decision"], "placed")
        self.assertEqual(rows[2]["decision"], "filed_extra")
        self.assertIn("as an extra track", rows[2]["download_error"])
        self.assertIn("Artist – Album", rows[2]["download_error"])
        self.assertNotIn(self.lib, rows[2]["download_error"])
        self.assertNotIn(os.sep, rows[2]["download_error"])
        self.assertEqual(self.verified, ["loose1"], "a bonus filing starts no verifier")

        with bot._review_lock:
            group = bot._find_review_group("loose1")
            view = bot._gap_detail_view(json.loads(json.dumps(group)))
            by_title = {t["title"]: t for t in view["tracks"]}
            self.assertEqual(by_title["Stranger"]["state"], "done")
            self.assertIn("Artist – Album", by_title["Stranger"]["downloadError"])
            self.assertEqual(by_title["Stranger"]["placeFolder"], "")
            # Not re-approvable, even on a stalled group (row 1 is stuck).
            self.assertTrue(bot._group_placement_stalled(group))
            bot._approve_pending_missing_tracks(group)
            self.assertEqual(group["missing_tracks"][2]["decision"], "filed_extra")
            # Settled: a group of settled rows is complete.
            settled = json.loads(json.dumps(group))
            settled["missing_tracks"][1]["decision"] = "verified"
            self.assertEqual(bot._review_group_next_action(settled)["bucket"], "completed")

        # And the verifier leaves it alone, while resetting the unconfirmed row 0.
        with patch.object(bot, "PLACEMENT_VERIFY_TIMEOUT", 0), \
                patch.object(bot, "_default_web_user",
                             lambda: {"navidrome_user": "u", "navidrome_password": "p"}), \
                patch.object(bot, "nd_get_scan_status", lambda u, p: {"scanning": False}), \
                patch.object(bot, "_nd_search", lambda *a, **k: []), \
                patch.object(bot, "refresh_group_albums_from_navidrome", lambda gid: False):
            bot._verify_placement_worker("loose1")
        rows = self._rows()
        self.assertEqual(rows[0]["decision"], "pending")
        self.assertEqual(rows[2]["decision"], "filed_extra")

    def test_stuck_loose_group_points_at_pick_the_release(self):
        with bot._review_lock:
            group = json.loads(json.dumps(bot._find_review_group("loose1")))
        view = bot._gap_detail_view(group)
        self.assertTrue(view["stalledPlacement"])
        self.assertIn("Pick the release", view["failDetail"])
        self.assertEqual([t["placeFolder"] for t in view["tracks"]],
                         [self.folder, self.other_folder])
        self.assertEqual([t["placeFile"] for t in view["tracks"]],
                         ["01 - One.flac", "Other.flac"])
        # An album group's stuck card still says Reconcile, with no folder hint.
        group.update({"group_type": "", "canonical_mbid": "rel-x"})
        view = bot._gap_detail_view(group)
        self.assertIn("Reconcile", view["failDetail"])
        self.assertEqual({t["placeFolder"] for t in view["tracks"]}, {""})

    def test_stuck_loose_group_with_files_gone_says_so_not_pick_the_release(self):
        """Q-017(e): `placeFolder` requires the file to still exist; `failDetail`
        always said "Use Pick the release on each track" regardless. When every
        downloaded row's file is gone there is no folder left to pick a release
        for -- the message must say the file is gone and to fetch it again, not
        point at a step that cannot work."""
        os.remove(self.file)
        os.remove(self.other_file)
        with bot._review_lock:
            group = json.loads(json.dumps(bot._find_review_group("loose1")))
        view = bot._gap_detail_view(group)
        self.assertTrue(view["stalledPlacement"])
        self.assertNotIn("Pick the release", view["failDetail"])
        self.assertIn("no longer", view["failDetail"])
        self.assertEqual([t["placeFolder"] for t in view["tracks"]], ["", ""])

    def test_stuck_loose_group_with_files_gone_says_where_to_fetch(self):
        """Final review M3: `failDetail` reaches Feishin and Navic too, and on a
        stalled gap they offer no fetch (R10/R15/R17) — "Fetch them again" has
        to say where."""
        os.remove(self.file)
        os.remove(self.other_file)
        with bot._review_lock:
            group = json.loads(json.dumps(bot._find_review_group("loose1")))
        self.assertIn("in lb-bot", bot._gap_detail_view(group)["failDetail"])

    def test_the_gap_view_says_whether_the_group_is_loose(self):
        """Final review M1: the SPA hid Reconcile on an empty `releaseMbid` as a
        stand-in for "loose" — so an album gap with no MBID lost the button its
        own `failDetail` tells the user to press. The server says which it is."""
        with bot._review_lock:
            group = json.loads(json.dumps(bot._find_review_group("loose1")))
        view = bot._gap_detail_view(group)
        self.assertIs(view["loose"], True)
        self.assertEqual(view["releaseMbid"], "")
        # An album gap with no release MBID on record: not loose.
        group.update({"group_type": "", "canonical_mbid": ""})
        view = bot._gap_detail_view(group)
        self.assertIs(view["loose"], False)
        self.assertEqual(view["releaseMbid"], "")
        self.assertIn("Reconcile", view["failDetail"])

    def test_stuck_loose_group_mixed_files_still_offers_pick_the_release(self):
        """One row's file survives; the other's is gone. As long as one row can
        still be fixed with Pick the release, the message stays that one."""
        os.remove(self.other_file)
        with bot._review_lock:
            group = json.loads(json.dumps(bot._find_review_group("loose1")))
        view = bot._gap_detail_view(group)
        self.assertIn("Pick the release", view["failDetail"])
        self.assertEqual([t["placeFolder"] for t in view["tracks"]],
                         [self.folder, ""])

    def test_fills_view_drops_host_paths_from_gap_summaries(self):
        """Q-017(c2): both clients poll `/lb/fills` (via `/api/fills`) with
        group_ids every 30s; `_fills_view` answered each gap with the full
        `_gap_detail_view` minus `sources`, so an absolute `/downloads/...`
        path (placeFolder) and a peer's filename (placeFile) reached devices
        unstripped on the most-polled route. The SPA's own `/api/gaps/<id>`
        route still needs them."""
        out = bot._fills_view([], ["loose1"])
        tracks = out["gaps"]["loose1"]["tracks"]
        self.assertTrue(tracks, "fixture has tracks")
        for t in tracks:
            self.assertNotIn("placeFolder", t)
            self.assertNotIn("placeFile", t)
            # Other fields survive untouched.
            self.assertIn("title", t)
            self.assertIn("state", t)
        with bot._review_lock:
            group = json.loads(json.dumps(bot._find_review_group("loose1")))
        direct = bot._gap_detail_view(group)["tracks"]
        self.assertEqual(direct[0]["placeFolder"], self.folder)
        self.assertEqual(direct[0]["placeFile"], "01 - One.flac")

    def test_host_path_prefixes_come_off_stored_error_text(self):
        """Q-021: the read-side strip, on every shape a stored text has — a
        prefix configured with or without a trailing slash, a path under it, a
        path that IS it — and nothing else touched."""
        cases = {
            "No audio files found in /downloads/peer folder/CD1":
                "No audio files found in peer folder/CD1",
            "No audio files found in /downloads": "No audio files found in downloads",
            "No audio files found in /downloads/": "No audio files found in downloads",
            "Could not create /music/A/B: [Errno 13] Permission denied: '/music/A/B'":
                "Could not create A/B: [Errno 13] Permission denied: 'A/B'",
            "/music": "music",
            # Unrelated text: other folders that merely start or end alike,
            # the words themselves, and a mount nested under another path.
            "/downloadsx/a, /music-videos/b, /music.old/c and /mnt/downloads/d":
                "/downloadsx/a, /music-videos/b, /music.old/c and /mnt/downloads/d",
            "music from downloads": "music from downloads",
            "Completed, Errored": "Completed, Errored",
            "": "",
        }
        for dl, lib in (("/downloads", "/music"), ("/downloads/", "/music/")):
            with patch.object(bot, "SLSKD_DOWNLOAD_DIR", dl), \
                    patch.object(bot, "MUSIC_LIBRARY_PATH", lib):
                for text, want in cases.items():
                    self.assertEqual(bot._strip_host_paths(text), want, (dl, text))
        # Nested mounts: the longer prefix wins, the shorter still applies.
        with patch.object(bot, "SLSKD_DOWNLOAD_DIR", "/data/downloads"), \
                patch.object(bot, "MUSIC_LIBRARY_PATH", "/data"):
            self.assertEqual(bot._strip_host_paths("/data/downloads/x and /data/y"),
                             "x and y")
        self.assertEqual(bot._strip_host_paths(None), "")

    def test_placement_error_texts_name_no_host_path(self):
        """Q-021 (b): the three texts that carried a host path say the folder's
        own name, or the OS's reason, instead."""
        empty = os.path.join(self.downloads, "empty peer")
        os.makedirs(empty)
        result = bot._deterministic_album_import(empty, self.RELEASE, "Artist", "Album")
        self.assertEqual(result["error"], "No audio files found in empty peer")

        # A file where the artist folder should be: makedirs fails for real.
        with open(os.path.join(self.lib, "Artist"), "wb"):
            pass
        result = bot._deterministic_album_import(self.folder, self.RELEASE,
                                                 "Artist", "Album")
        self.assertEqual(result["error"],
                         "Could not create album folder Album: Not a directory")
        os.remove(os.path.join(self.lib, "Artist"))

        def refuse(src, dest):
            raise PermissionError(13, "Permission denied", dest)

        with patch.object(bot, "_place_file", refuse):
            result = bot._deterministic_album_import(self.folder, self.RELEASE,
                                                     "Artist", "Album")
        self.assertEqual(result["error"],
                         "Nothing placed: could not move into the library: Permission denied")
        for text in [result["error"]] + [r.get("reason", "") for r in result["per_file"]]:
            self.assertNotIn(self.lib, text)
            self.assertNotIn(self.downloads, text)

    def test_an_error_with_no_os_reason_keeps_its_text_without_paths(self):
        """Final review M-3: `shutil.Error` (an OSError with no `strerror`) and
        other exceptions read as their bare class name — "could not move into
        the library: Error". Their own text now, with the mounts taken off."""
        import shutil
        exc = shutil.Error(f"Destination path '{self.lib}/Artist/Album/01.flac' already exists")
        self.assertEqual(bot._os_error_text(exc),
                         "Destination path 'Artist/Album/01.flac' already exists")
        self.assertEqual(bot._os_error_text(PermissionError(13, "Permission denied",
                                                            f"{self.lib}/x")),
                         "Permission denied")
        self.assertEqual(bot._os_error_text(ValueError()), "ValueError")

    def test_no_host_path_reaches_a_wire_error_text(self):
        """Q-021: `download_error` and the ledger's `reason` could carry
        "/downloads/<peer folder>" or "/music/..." — stamped verbatim, then
        served as `tracks[].downloadError` (gap view, `/api/fills`) and as an
        album fill's `reason` (`/api/album/status`, `/api/fills`). Rows stored
        before the rewording keep their text, so the views strip it."""
        old = bot._album_fill_status.copy()
        self.addCleanup(lambda: (bot._album_fill_status.clear(),
                                 bot._album_fill_status.update(old)))
        bot._album_fill_status.clear()
        with bot._review_lock:
            rows = bot._find_review_group("loose1")["missing_tracks"]
            rows[0]["download_error"] = f"No audio files found in {self.folder}"
            rows[1].update(decision="failed", download_error=(
                f"placement failed: Could not create {self.lib}/Artist/Album: "
                f"[Errno 13] Permission denied: '{self.lib}/Artist/Album'"))
            group = json.loads(json.dumps(bot._find_review_group("loose1")))
        with patch.object(bot, "_save_state", lambda *a, **k: None):
            bot._album_fill_set(self.RELEASE, "failed", reason=(
                f"Nothing placed: could not move into the library: [Errno 13] "
                f"Permission denied: '{self.lib}/Artist/Album/01 - One.flac'"))
        fills = bot._fills_view([self.RELEASE], ["loose1"])
        texts = ([t["downloadError"] for t in bot._gap_detail_view(group)["tracks"]]
                 + [t["downloadError"] for t in fills["gaps"]["loose1"]["tracks"]]
                 + [bot._album_fill_view(self.RELEASE)["reason"],
                    fills["albums"][self.RELEASE]["reason"]])
        for text in texts:
            self.assertNotIn(self.downloads, text)
            self.assertNotIn(self.lib, text)
        self.assertEqual(texts[0], "No audio files found in peer folder")
        self.assertEqual(texts[1], "placement failed: Could not create Artist/Album: "
                                   "[Errno 13] Permission denied: 'Artist/Album'")
        self.assertEqual(texts[-1], "Nothing placed: could not move into the library: "
                                    "[Errno 13] Permission denied: 'Artist/Album/01 - One.flac'")

    # --- the automatic path: the poller's loose-track branch -----------------

    def _finish_download(self, tags):
        """Run one poll in which the loose row's transfer completes."""
        with bot._review_lock:
            track = bot._find_review_group("loose1")["missing_tracks"][0]
            track.update({"decision": "downloading", "local_path": ""})
            # What the enqueue registered: a copy of the row as it stood.
            copy = {k: track.get(k) for k in ("artist", "title", "mbid")}
        old_pending = bot.pending_downloads.copy()
        self.addCleanup(lambda: (bot.pending_downloads.clear(),
                                 bot.pending_downloads.update(old_pending)))
        bot.pending_downloads.clear()
        bot.pending_downloads[("peer", "Music\\01 - One.flac")] = {
            "token": "tok", "chat_id": "chat", "candidates": [],
            "album_group_id": None, "review_group_id": "loose1",
            "review_track_index": 0, "track": copy}
        written = []
        app = type("App", (), {"bot": AsyncMock()})()
        with patch.object(bot, "slskd_get_all_downloads",
                          lambda force=False: [{"_username": "peer",
                                               "filename": "Music\\01 - One.flac",
                                               "state": "Completed, Succeeded"}]), \
                patch.object(bot, "_tg_send", new_callable=AsyncMock) as tg, \
                patch.object(bot, "_save_state", lambda *a, **k: None), \
                patch.object(bot, "_resolve_local_path", lambda f: self.file), \
                patch.object(bot, "_audio_file_tags",
                             tags if callable(tags) else (lambda path: dict(tags))), \
                patch.object(bot, "_mutagen_write_tags",
                             lambda path, t: written.append((path, t)) or True):
            asyncio.run(bot._poll_downloads_once({"tok": app}))
        # _tg_send(bot, chat_id, text): what Telegram was told.
        self.telegram = [c.args[2] for c in tg.call_args_list]
        return written

    def test_embedded_release_tag_files_the_loose_track(self):
        self._finish_download({"musicbrainz_albumid": self.RELEASE, "title": "One"})
        row = self._rows()[0]
        self.assertEqual(row["decision"], "placed")
        self.assertFalse(os.path.exists(self.file))
        self.assertTrue(os.path.isfile(os.path.join(self.lib, "Artist", "Album", "01 - One.flac")))
        self.assertEqual(self.verified, ["loose1"])

    def test_musicbrainz_failure_does_not_scatter_the_auto_placed_file(self):
        """Review A3 #1: with no album/artist of its own, the import filled them
        from a non-strict MusicBrainz fetch that answers {} on an outage — and
        the file landed unattended in Unknown Artist/Unknown Album."""
        def down(*a, **k):
            raise bot.MusicBrainzUnavailable("503")

        with patch.object(bot, "mbz_get", down):
            self._finish_download({"musicbrainz_albumid": self.RELEASE, "title": "One"})
        row = self._rows()[0]
        self.assertEqual(row["decision"], "downloaded")
        self.assertTrue(os.path.isfile(self.file))
        self.assertFalse(os.path.exists(os.path.join(self.lib, "Unknown Artist")))
        self.assertIn("MusicBrainz didn't name", row["download_error"])

    def test_the_files_own_album_tags_file_it_without_musicbrainz(self):
        def down(*a, **k):
            raise bot.MusicBrainzUnavailable("503")

        with patch.object(bot, "mbz_get", down):
            self._finish_download({"musicbrainz_albumid": self.RELEASE, "title": "One",
                                   "albumartist": "Tagged Artist",
                                   "album": "Tagged Album"})
        self.assertEqual(self._rows()[0]["decision"], "placed")
        self.assertTrue(os.path.isfile(os.path.join(
            self.lib, "Tagged Artist", "Tagged Album", "01 - One.flac")))

    def test_no_release_tag_leaves_the_file_and_says_why(self):
        written = self._finish_download({"title": "One"})
        row = self._rows()[0]
        self.assertEqual(row["decision"], "downloaded")
        self.assertTrue(os.path.isfile(self.file))
        self.assertIn("no single MusicBrainz release tag", row["download_error"])
        # Today's behaviour, unchanged: the file is tagged where it lies.
        self.assertEqual(written, [(self.file, {"title": "One", "artist": "Artist",
                                                "mb_trackid": "r1"})])

    def test_a_miss_is_written_on_its_own_row_after_a_rescan_moved_it(self):
        """B-022 final review I1: the poller deletes the transfer before it
        auto-places, so a rescan during the placement (a tag read, a strict
        MusicBrainz lookup) re-points nothing — and the miss, written by the
        index read before, landed on whichever row a rescan put there: here a
        track never downloaded, which then read `downloaded`."""
        def tags(path):
            with bot._review_lock:
                fresh = json.loads(json.dumps(bot._find_review_group("loose1")))
            fresh["missing_tracks"].insert(0, {"title": "New", "artist": "X",
                                               "mbid": "r0", "decision": "pending"})
            fresh.setdefault("merge_mode", "")
            bot._union_review_groups([fresh], "playlist")
            return {"title": "One"}

        self._finish_download(tags)
        rows = {t["title"]: t for t in self._rows()}
        self.assertEqual(rows["New"]["decision"], "pending")
        self.assertNotIn("download_error", rows["New"])
        self.assertNotIn("placement_missed_at", rows["New"])
        self.assertEqual(rows["One"]["decision"], "downloaded")
        self.assertIn("no single MusicBrainz release tag", rows["One"]["download_error"])
        self.assertTrue(rows["One"].get("placement_missed_at"))

    def test_a_release_tag_that_will_not_place_leaves_the_file_and_says_why(self):
        # MusicBrainz answers no tracklist for the tagged release.
        with patch.object(bot, "mbz_release_tracks_insisting", lambda *a, **k: []):
            self._finish_download({"musicbrainz_albumid": self.RELEASE})
        row = self._rows()[0]
        self.assertEqual(row["decision"], "downloaded")
        self.assertTrue(os.path.isfile(self.file))
        self.assertIn("not filed under its tagged release", row["download_error"])
        self.assertIn("tracklist", row["download_error"])

    def test_an_album_groups_single_file_is_not_auto_placed(self):
        """A hand-picked or rescued file for an album gap takes the same branch;
        it belongs to the album's own placement, not to this one."""
        with bot._review_lock:
            bot._find_review_group("loose1").update(
                {"group_type": "", "canonical_mbid": "rel-x"})
        with patch.object(bot, "_deterministic_album_import",
                          lambda *a, **k: self.fail("an album gap's file must not be auto-placed")):
            self._finish_download({"musicbrainz_albumid": self.RELEASE})
        row = self._rows()[0]
        self.assertEqual(row["decision"], "downloaded")
        self.assertNotIn("download_error", row)

    # --- B-025 (b): a miss is not "working" ----------------------------------

    def _make_row_1_pending(self):
        with bot._review_lock:
            row = bot._find_review_group("loose1")["missing_tracks"][1]
            for field in ("local_path", "downloaded_at"):
                row.pop(field, None)
            row["decision"] = "pending"

    def test_an_auto_place_miss_stalls_the_group_at_once(self):
        """A miss left the row `downloaded`, so the group read "downloading" for
        DOWNLOADED_STALE_SECS — and meanwhile fetch and auto answered
        `alreadyActive` and a finished source search was thrown away, so the
        group's other rows could not be fetched either."""
        self._make_row_1_pending()
        self._finish_download({"title": "One"})          # no release tag: a miss
        self.assertEqual(self._rows()[0]["decision"], "downloaded")
        with bot._review_lock:
            group = json.loads(json.dumps(bot._find_review_group("loose1")))
        self.assertEqual(bot._gap_status_for_group(group), "failed")
        view = bot._gap_detail_view(group)
        self.assertTrue(view["stalledPlacement"])
        self.assertIn("Pick the release", view["failDetail"])
        # The list rail reads the same, from its own snapshot.
        listed = {g["id"]: g for g in bot._review_list_snapshot()["groups"]}
        self.assertEqual(bot._gap_status_for_group(listed["loose1"]), "failed")

        # And the pending row is fetchable now — without touching the missed
        # row: its file is still in /downloads and "Pick the release" is its
        # fix (review fix round 1). Re-approving it cleared its local_path and
        # downloaded the file a second time.
        r, enqueued = self._fetch_loose_group()
        body = r.get_json()
        self.assertNotIn("alreadyActive", body)
        self.assertEqual(r.status_code, 200, body)
        self.assertEqual(enqueued, [["downloaded", "approved"]])
        row = self._rows()[0]
        self.assertEqual(row["decision"], "downloaded")
        self.assertEqual(row["local_path"], self.file)
        self.assertTrue(os.path.isfile(self.file))
        self.assertIn("Pick the release", row["download_error"])

    def _fetch_loose_group(self):
        """POST the fetch route with one source on offer; returns the response
        and each enqueue's row decisions."""
        with bot._review_lock:
            bot._find_review_group("loose1")["source_results"] = {
                "folders": [{"username": "p", "folder": "f", "files": []}]}
        enqueued = []

        def enqueue(group, idx):
            enqueued.append([t.get("decision") for t in group["missing_tracks"]])
            return {"ok": True, "message": "Source queued"}

        with patch.object(bot, "_enqueue_group_source", enqueue), \
                patch.object(bot, "_save_state", lambda *a, **k: None):
            r = self.client.post("/api/gaps/loose1/fetch", json={"sourceId": 0})
        return r, enqueued

    def test_a_miss_whose_file_is_gone_is_fetched_with_the_rest(self):
        """Final review M2: a missed row is held back so its file in /downloads
        keeps its "Pick the release" — but once that file is gone there is
        nothing to keep, and holding it back made "Fetch them again" take two
        clicks: this fetch for the other rows, the next for the miss."""
        self._make_row_1_pending()
        self._finish_download({"title": "One"})          # no release tag: a miss
        os.remove(self.file)
        r, enqueued = self._fetch_loose_group()
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(enqueued, [["approved", "approved"]])
        self.assertNotIn("local_path", self._rows()[0])

    def test_download_again_still_refetches_a_miss_with_nothing_else_to_get(self):
        """When the missed row is the only thing to fetch, the Stuck card's
        "Download again from another source" re-approves it, as before."""
        with bot._review_lock:
            bot._find_review_group("loose1")["missing_tracks"][1]["decision"] = "verified"
        self._finish_download({"title": "One"})          # no release tag: a miss
        r, enqueued = self._fetch_loose_group()
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(enqueued, [["approved", "verified"]])
        self.assertNotIn("local_path", self._rows()[0])

    def test_a_loose_row_still_placing_keeps_the_group_working(self):
        """Only a miss is immediate: a download that has not been through
        auto-placement yet (or was re-downloaded since an older miss) still
        reads as work in progress."""
        now = time.time()
        with bot._review_lock:
            rows = bot._find_review_group("loose1")["missing_tracks"]
            rows[0].update({"downloaded_at": now})
            rows[1].update({"downloaded_at": now - 60,
                            "placement_missed_at": now - 120})   # an older miss
            group = json.loads(json.dumps(bot._find_review_group("loose1")))
        self.assertEqual(bot._gap_status_for_group(group), "downloading")
        self.assertFalse(bot._gap_detail_view(group)["stalledPlacement"])
        # A current miss on one row does not stall a row still being placed.
        group["missing_tracks"][1]["placement_missed_at"] = now
        self.assertEqual(bot._gap_status_for_group(group), "downloading")
        # Once that row is placed, the miss is all that is left.
        group["missing_tracks"][0]["decision"] = "placed"
        self.assertEqual(bot._gap_status_for_group(group), "failed")

    # --- B-025 (a): the verifier, after a loose placement --------------------

    def _nd(self, songs_by_query):
        """Patches for one verifier run against a fake Navidrome."""
        return [patch.object(bot, "_default_web_user",
                             lambda: {"navidrome_user": "u", "navidrome_password": "p"}),
                patch.object(bot, "nd_get_scan_status", lambda u, p: {"scanning": False}),
                # Navidrome answers an MBID query with songs carrying that
                # recording MBID, which the probe requires since Q-024; the
                # fixtures below name songs by the query that finds them.
                patch.object(bot, "_nd_search",
                             lambda u, p, query, count=5, _retry=True:
                             [{"musicBrainzId": query, **song}
                              for song in songs_by_query(query)]),
                patch.object(bot, "refresh_group_albums_from_navidrome", lambda gid: False),
                patch.object(bot, "_notify_hub_library_change",
                             lambda *a, **k: self.notified.append(k))]

    def _run_verifier(self, songs_by_query, timeout=0, clock=None):
        self.notified = []
        patches = self._nd(songs_by_query) + [
            patch.object(bot, "PLACEMENT_VERIFY_TIMEOUT", timeout)]
        if clock is not None:
            patches.append(patch.object(bot, "time", clock))
        else:
            patches.append(patch.object(bot.time, "sleep", lambda s: None))
        for p in patches:
            p.start()
        try:
            bot._verify_placement_worker("loose1")
        finally:
            for p in reversed(patches):
                p.stop()

    def test_the_link_records_the_slot_the_file_was_placed_into(self):
        """The import rewrites the file's title and recording MBID to the
        release slot's; the row has only its playlist spelling."""
        with bot._review_lock:
            bot._find_review_group("loose1")["missing_tracks"][0].update(
                {"title": "One (2011 Remaster)", "mbid": ""})
        self._finish_download({"musicbrainz_albumid": self.RELEASE, "title": "One"})
        row = self._rows()[0]
        self.assertEqual(row["decision"], "placed")
        self.assertEqual(row["placed_title"], "One")
        self.assertEqual(row["placed_recording_mbid"], "r1")

    def test_the_verifier_matches_a_loose_row_by_its_placed_slot(self):
        """It searched by the row's own title and MBID, found neither — the
        import retagged the file — and at the deadline reset the row to
        `pending`, which the next fetch re-downloaded into the library again."""
        with bot._review_lock:
            bot._find_review_group("loose1")["missing_tracks"][0].update(
                {"title": "One (2011 Remaster)", "mbid": "", "decision": "placed",
                 "imported_at": time.time(), "placed_title": "One",
                 "placed_recording_mbid": "r1"})
        song = {"id": "s1", "title": "One", "artist": "Artist", "album": "Album",
                "albumId": "al-1", "artistId": "ar-1"}
        self._run_verifier(lambda q: [song] if q == "r1" else [])
        row = self._rows()[0]
        self.assertEqual(row["decision"], "verified")
        self.assertEqual(row.get("download_error", ""), "")

    def test_a_row_placed_late_in_a_running_verifier_gets_its_own_window(self):
        """The deadline was the worker's: a row placed 30 s before it (joining
        the running worker, since a second one never starts) was reset to
        `pending` 30 s after placement."""
        placed_at = 1000.0
        timeout = 600

        class Clock:
            def __init__(clock):
                clock.now = placed_at

            def time(clock):
                return clock.now

            def sleep(clock, secs):
                clock.now += secs
                with bot._review_lock:
                    row = bot._find_review_group("loose1")["missing_tracks"][1]
                    if row["decision"] == "downloaded" and \
                            clock.now >= placed_at + timeout - 30:
                        row.update({"decision": "placed", "imported_at": clock.now,
                                    "placed_title": "Other",
                                    "placed_recording_mbid": "r9"})

            def __getattr__(clock, name):
                return getattr(time, name)

        clock = Clock()
        with bot._review_lock:
            bot._find_review_group("loose1")["missing_tracks"][0].update(
                {"decision": "placed", "imported_at": placed_at})
        other = {"id": "s9", "title": "Other", "artist": "Someone Else",
                 "album": "Elsewhere", "albumId": "al-9", "artistId": "ar-9"}
        # Navidrome indexes row 1 a minute after it was placed; row 0 never.
        self._run_verifier(
            lambda q: [other] if q == "r9" and clock.now >= placed_at + timeout + 30 else [],
            timeout=timeout, clock=clock)
        rows = self._rows()
        self.assertEqual(rows[0]["decision"], "pending")
        self.assertIn("never appeared in Navidrome", rows[0]["download_error"])
        self.assertEqual(rows[1]["decision"], "verified")

    def test_each_loose_song_is_announced_under_its_own_album(self):
        """One `albumIndexed` said artist "Loose tracks", album "Playlist
        tracks", and carried the ids of two unrelated albums."""
        now = time.time()
        with bot._review_lock:
            rows = bot._find_review_group("loose1")["missing_tracks"]
            rows[0].update({"decision": "placed", "imported_at": now,
                            "placed_title": "One", "placed_recording_mbid": "r1"})
            rows[1].update({"decision": "placed", "imported_at": now,
                            "placed_title": "Other", "placed_recording_mbid": "r9"})
        songs = {"r1": {"id": "s1", "title": "One", "artist": "Artist",
                        "album": "Album", "albumId": "al-1", "artistId": "ar-1"},
                 "r9": {"id": "s9", "title": "Other", "artist": "Someone Else",
                        "album": "Elsewhere", "albumId": "al-9", "artistId": "ar-9"}}
        self._run_verifier(lambda q: [songs[q]] if q in songs else [])
        self.assertEqual([r["decision"] for r in self._rows()], ["verified", "verified"])
        announced = sorted((k["artist"], k["album"], tuple(k["nd_album_ids"]),
                            k["nd_artist_id"])
                           for k in self.notified if k.get("event") == "albumIndexed")
        self.assertEqual(announced, [("Artist", "Album", ("al-1",), "ar-1"),
                                     ("Someone Else", "Elsewhere", ("al-9",), "ar-9")])

    def test_an_album_group_is_still_announced_once_under_its_own_name(self):
        """Regression guard: only the Loose-tracks announcement changed."""
        now = time.time()
        with bot._review_lock:
            group = bot._find_review_group("loose1")
            group.update({"group_type": "", "artist": "Band", "album": "Record",
                          "canonical_mbid": "rel-x"})
            for row in group["missing_tracks"]:
                row.update({"decision": "placed", "imported_at": now})
        songs = {"r1": {"id": "s1", "title": "One", "artist": "Band",
                        "album": "Record", "albumId": "al-1", "artistId": "ar-1"},
                 "r9": {"id": "s9", "title": "Other", "artist": "Band",
                        "album": "Record", "albumId": "al-1", "artistId": "ar-1"}}
        self._run_verifier(lambda q: [songs[q]] if q in songs else [])
        announced = [(k["artist"], k["album"], k["nd_album_ids"])
                     for k in self.notified if k.get("event") == "albumIndexed"]
        self.assertEqual(announced, [("Band", "Record", ["al-1"])])

    # --- Q-017(f): what Telegram is told --------------------------------------

    def test_a_linker_failure_is_never_reported_as_filed(self):
        """The import had already moved the file, so `not os.path.isfile(...)`
        read the exception as success: "filed under its tagged release", and a
        scan, for a row that was never linked."""
        def boom(*a, **k):
            raise RuntimeError("linker broke")

        with patch.object(bot, "_link_loose_track_placements", boom):
            self._finish_download({"musicbrainz_albumid": self.RELEASE, "title": "One"})
        self.assertEqual(len(self.telegram), 1)
        self.assertNotIn("filed under", self.telegram[0])
        self.assertIn("failed", self.telegram[0])
        # Q-026: the row says why and carries the miss stamp — and, the file
        # having moved, it no longer points at a path with nothing there.
        self.assertFalse(os.path.exists(self.file))
        row = self._rows()[0]
        self.assertEqual(row["decision"], "downloaded")
        self.assertEqual(row["download_error"],
                         "not filed: auto-placement failed (RuntimeError) after the "
                         "file was moved — it was not linked to this row")
        self.assertEqual(row["local_path"], "")
        self.assertTrue(bot._placement_missed(row))

    def test_an_auto_place_that_raises_before_the_move_says_why(self):
        """Q-026: the poller's except branch left the row `downloaded` with no
        reason and no miss stamp, so it read as in progress for 30 minutes."""
        def boom(*a, **k):
            raise OSError("disk went away")

        with patch.object(bot, "_deterministic_album_import", boom):
            self._finish_download({"musicbrainz_albumid": self.RELEASE, "title": "One"})
        row = self._rows()[0]
        self.assertEqual(row["decision"], "downloaded")
        self.assertEqual(row["download_error"],
                         "not filed: auto-placement failed (OSError) — use Pick the release")
        self.assertEqual(row["local_path"], self.file)
        self.assertTrue(os.path.isfile(self.file))
        self.assertTrue(bot._placement_missed(row))

    def test_a_bonus_only_auto_place_says_extra_track(self):
        """A file matching no slot of its tagged release is filed as an extra
        track — not "under its tagged release" as if it had landed in a slot."""
        with patch.object(bot, "mbz_release_tracks",
                          lambda mbid, *a, **k: [{"title": "Two", "mbid": "r2",
                                                  "position": 5}]):
            self._finish_download({"musicbrainz_albumid": self.RELEASE, "title": "One"})
        row = self._rows()[0]
        self.assertEqual(row["decision"], "filed_extra")
        self.assertIn("Artist – Album", row["download_error"])
        self.assertEqual(self.verified, [])
        self.assertEqual(len(self.telegram), 1)
        self.assertNotIn("under its tagged release", self.telegram[0])
        self.assertIn("extra track", self.telegram[0])


class RescanKeepsLiveTransfersTests(unittest.TestCase):
    """B-022: a rescan resets queued/downloading to pending (ba8b83e — a row
    with nothing behind it must be retryable), and the repair-job projection
    was the only thing that put a row with a *live* transfer back. Without it a
    rescan dropped both in-flight rows of a fill to pending: the gap read
    `ready`, the fetch route's alreadyActive dedupe stopped firing and
    `_approve_pending_missing_tracks` re-approved both transfers for a second
    download. The rescan paths now ask the transfer registry itself.

    Each runs through all three rebuild paths."""

    GID = "lib0"

    @classmethod
    def setUpClass(cls):
        cls.client = _web_app().test_client()

    def setUp(self):
        # Persistence is not what these tests are about, and a deferred save
        # wakes the background review flusher, which then races
        # isolated_review's teardown (it closes the index connection the
        # flusher may be writing through — an intermittent SIGSEGV in the suite).
        for patcher in (patch.object(bot, "_push_gap", lambda gid: None),
                        patch.object(bot, "_save_review_state", lambda **k: None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _row(title, decision="pending"):
        return {"mbid": "m-" + title, "title": title, "decision": decision}

    def _group(self, rows, present=()):
        g = AlbumReviewTests._origin_group(
            self.GID, "library", canonical_mbid="rel-1", canonical_album_id="al1",
            albums=[{"id": "al1", "musicBrainzId": "rel-1", "artist": "A", "name": "B",
                     "tracks": [{"title": t, "musicBrainzId": "m-" + t}
                                for t in present]}])
        g["missing_tracks"] = rows
        return g

    def _transfer(self, title, index):
        """A live transfer for one row, registered the way slskd_enqueue does."""
        track = {"title": title, "mbid": "m-" + title,
                 "_review_group_id": self.GID, "_review_track_index": index}
        info = {"review_group_id": self.GID, "review_track_index": index,
                "track": track, "album_group_id": "ag1"}
        bot.pending_downloads[("peer", f"x/{title}.flac")] = info
        return info

    @contextlib.contextmanager
    def _world(self, rows):
        """Each subtest gets its own review, transfers and album fills."""
        with isolated_review(), \
                patch.dict(bot.pending_downloads, {}, clear=True), \
                patch.dict(bot.pending_album_groups, {}, clear=True):
            with bot._review_lock:
                bot._review_state["groups"] = [self._group(rows)]
            yield

    def _rescan(self, path, titles, present=()):
        """Rebuild the group through one of the three rescan paths, the scan
        reporting `titles` (in that order) still missing."""
        if path == "refresh":
            tracklist = [{"title": t, "mbid": "m-" + t, "position": i + 1}
                         for i, t in enumerate(list(present) + list(titles))]
            with patch.object(bot, "mbz_release_tracks", lambda *a, **k: tracklist), \
                    bot._review_lock:
                group = bot._find_review_group(self.GID)
                group["albums"] = self._group([], present)["albums"]
                bot.refresh_group_missing(group)
            return
        fresh = self._group([self._row(t) for t in titles], present)
        if path == "replace":
            bot._replace_review_groups("library", [fresh], "x")
        else:
            bot._union_review_groups([fresh], "library")

    def _decisions(self):
        with bot._review_lock:
            return {t["title"]: t["decision"]
                    for t in bot._find_review_group(self.GID)["missing_tracks"]}

    PATHS = ("replace", "union", "refresh")

    def test_rows_with_live_transfers_stay_in_flight(self):
        for path in self.PATHS:
            with self.subTest(path=path), \
                    self._world([self._row("One", "queued"),
                                 self._row("Two", "downloading")]):
                self._transfer("One", 0)
                self._transfer("Two", 1)
                self._rescan(path, ["One", "Two"])

                self.assertEqual(self._decisions(),
                                 {"One": "queued", "Two": "downloading"})
                with bot._review_lock:
                    group = bot._find_review_group(self.GID)
                    self.assertEqual(bot._gap_status_for_group(group), "downloading")
                r = self.client.post(f"/api/gaps/{self.GID}/fetch", json={})
                self.assertTrue(r.get_json().get("alreadyActive"), r.get_json())
                with bot._review_lock:
                    flipped = bot._approve_pending_missing_tracks(
                        bot._find_review_group(self.GID))
                self.assertEqual(flipped, 0)
                self.assertEqual(self._decisions(),
                                 {"One": "queued", "Two": "downloading"})

    def test_a_live_row_keeps_its_transfer_fields(self):
        """The projection used to restore these from the job row; a rescan
        builds the row fresh, so the merge has to carry them itself."""
        for path in self.PATHS:
            with self.subTest(path=path), \
                    self._world([self._row("One", "downloading")]):
                with bot._review_lock:
                    bot._find_review_group(self.GID)["missing_tracks"][0].update(
                        download_percent=40, source_user="peer",
                        filename="x/One.flac", download_state="InProgress")
                self._transfer("One", 0)
                self._rescan(path, ["One"])
                with bot._review_lock:
                    row = bot._find_review_group(self.GID)["missing_tracks"][0]
                self.assertEqual((row["decision"], row.get("download_percent"),
                                  row.get("source_user"), row.get("filename")),
                                 ("downloading", 40, "peer", "x/One.flac"))

    def test_a_row_with_no_transfer_still_goes_stale(self):
        """ba8b83e's rule stands: queued/downloading with nothing behind it is a
        phantom and a rescan makes it retryable. (Before B-022 the repair-job
        projection put phantoms back.)"""
        for path in self.PATHS:
            with self.subTest(path=path), \
                    self._world([self._row("One", "queued"),
                                 self._row("Two", "downloading")]):
                self._transfer("Two", 1)
                self._rescan(path, ["One", "Two"])
                self.assertEqual(self._decisions(),
                                 {"One": "pending", "Two": "downloading"})

    def test_placed_goes_stale_but_verified_and_filed_extra_are_kept(self):
        """ba8b83e's rule is for claims nobody has checked: a `placed` row the
        scan still reports missing resets to pending (outside its verify
        window). A `verified` row was matched in Navidrome and a `filed_extra`
        row is a file already in the library; when the scan disagrees it is the
        scan's matching that missed (an album split, a playlist spelling), and
        resetting them re-downloaded a file the library has. The repair-job
        projection kept both settled in every group that could hold them, so
        that is the behaviour production has had since July."""
        for path in self.PATHS:
            with self.subTest(path=path), \
                    self._world([self._row("One", "placed"),
                                 self._row("Two", "verified"),
                                 self._row("Three", "filed_extra"),
                                 self._row("Four", "skipped"),
                                 self._row("Five", "navidrome_verified")]):
                self._rescan(path, ["One", "Two", "Three", "Four", "Five"])
                self.assertEqual(self._decisions(),
                                 {"One": "pending", "Two": "verified",
                                  "Three": "filed_extra", "Four": "skipped",
                                  "Five": "navidrome_verified"})

    @staticmethod
    @contextlib.contextmanager
    def _recheck_by_hand():
        """Run the verified re-check only when the test drains it: a queue and a
        wake event of its own, so a worker an earlier test started stays asleep."""
        with patch.object(bot, "_verified_recheck_queue", {}), \
                patch.object(bot, "_verified_recheck_wake", threading.Event()), \
                patch.object(bot, "_ensure_verified_rechecker", lambda: None):
            yield

    def test_a_disputed_verified_row_is_rechecked_and_reset_only_when_gone(self):
        """Q-023: a `verified` row was kept whatever a rescan said, so a file
        deleted after verification read done forever. The verifier now records
        the Navidrome song it matched; a rescan that still lists the row hands
        it to one re-check worker, which asks Navidrome for that song off the
        lock and resets the row only on a positive "no such song" (Subsonic
        error 70). Present, or Navidrome down: kept. A legacy row with no song
        id, and `filed_extra`, are kept unchecked, as before."""
        answers = {"s-gone": None, "s-here": {"id": "s-here"},
                   "s-down": OSError("Navidrome did not answer")}
        asked = []

        def get_song(u, p, song_id):
            asked.append(song_id)
            answer = answers[song_id]
            if isinstance(answer, Exception):
                raise answer
            return answer

        rows = lambda: [dict(self._row("Gone", "verified"), nd_song_id="s-gone"),
                        dict(self._row("Here", "verified"), nd_song_id="s-here"),
                        dict(self._row("Down", "verified"), nd_song_id="s-down"),
                        self._row("Legacy", "verified"),
                        self._row("Extra", "filed_extra")]
        titles = ["Gone", "Here", "Down", "Legacy", "Extra"]
        for path in self.PATHS:
            asked.clear()
            with self.subTest(path=path), self._world(rows()), \
                    self._recheck_by_hand(), \
                    patch.object(bot, "nd_get_song", get_song), \
                    patch.object(bot, "nd_track_match", lambda *a, **k: None), \
                    patch.object(bot, "nd_get_scan_status", lambda u, p: {"scanning": False}), \
                    patch.object(bot, "_default_web_user",
                                 lambda *a, **k: {"navidrome_user": "u",
                                                  "navidrome_password": "p"}), \
                    contextlib.redirect_stdout(io.StringIO()):
                self._rescan(path, titles)
                # The rescan itself still keeps every settled row ...
                self.assertEqual(self._decisions(),
                                 {"Gone": "verified", "Here": "verified",
                                  "Down": "verified", "Legacy": "verified",
                                  "Extra": "filed_extra"})
                # ... and the re-check, off the rescan, resets only the gone one.
                bot._drain_verified_rechecks()
                self.assertEqual(sorted(asked), ["s-down", "s-gone", "s-here"])
                self.assertEqual(self._decisions(),
                                 {"Gone": "pending", "Here": "verified",
                                  "Down": "verified", "Legacy": "verified",
                                  "Extra": "filed_extra"})
                with bot._review_lock:
                    gone = bot._find_review_group(self.GID)["missing_tracks"][0]
                self.assertFalse(gone.get("nd_song_id"))
                self.assertIn("Navidrome", gone["download_error"])

    def test_a_recheck_never_resets_a_row_that_moved_on(self):
        """The re-check writes after a Navidrome round trip: a row re-fetched
        (or re-verified as another song) in the meantime is not its to reset."""
        with self._world([dict(self._row("One", "verified"), nd_song_id="s-old")]), \
                self._recheck_by_hand(), \
                patch.object(bot, "_default_web_user",
                             lambda *a, **k: {"navidrome_user": "u",
                                              "navidrome_password": "p"}), \
                contextlib.redirect_stdout(io.StringIO()):
            self._rescan("union", ["One"])

            def gone_but_meanwhile(u, p, song_id):
                with bot._review_lock:
                    bot._find_review_group(self.GID)["missing_tracks"][0].update(
                        decision="queued")
                return None

            with patch.object(bot, "nd_get_song", gone_but_meanwhile):
                bot._drain_verified_rechecks()
            self.assertEqual(self._decisions(), {"One": "queued"})

    def _gone_by_id(self, match, status):
        """One verified row whose song id Navidrome no longer knows; drain with
        the library-wide match answering `match` and the scan status `status`."""
        with self._world([dict(self._row("One", "verified"), nd_song_id="s-old",
                               artist="A")]), \
                self._recheck_by_hand(), \
                patch.object(bot, "_default_web_user",
                             lambda *a, **k: {"navidrome_user": "u",
                                              "navidrome_password": "p"}), \
                contextlib.redirect_stdout(io.StringIO()):
            self._rescan("union", ["One"])
            asked = []
            with patch.object(bot, "nd_get_song", lambda u, p, song_id: None), \
                    patch.object(bot, "nd_track_match",
                                 lambda *a, **k: asked.append((a[:3], k)) or match), \
                    patch.object(bot, "nd_get_scan_status", lambda u, p: status):
                reset = bot._drain_verified_rechecks()
            with bot._review_lock:
                row = dict(bot._find_review_group(self.GID)["missing_tracks"][0])
        return reset, row, asked

    def test_a_song_navidrome_re_ided_is_followed_not_reset(self):
        """Fix round 1 (M2): error 70 means "no song with that id", not "the
        recording is gone" — a re-id (a rescan of moved files) answers it too,
        and resetting then fetched a duplicate. The verifier's own library-wide
        match runs first; finding the song re-records its new id."""
        reset, row, asked = self._gone_by_id({"id": "s-new", "albumId": "al1"},
                                             {"scanning": False})
        self.assertEqual(reset, 0)
        self.assertEqual((row["decision"], row["nd_song_id"]), ("verified", "s-new"))
        # The verifier's own match: the row's title and recording MBID (the
        # harness's rescanned rows carry no artist), preferring the group's albums.
        self.assertEqual(asked[0][0][1:], ("One", "m-One"))
        self.assertEqual(asked[0][1].get("prefer_album_ids"), {"al1"})

    def test_a_gone_song_the_library_match_cannot_find_is_reset(self):
        reset, row, _asked = self._gone_by_id(None, {"scanning": False})
        self.assertEqual(reset, 1)
        self.assertEqual(row["decision"], "pending")
        self.assertFalse(row.get("nd_song_id"))

    def test_a_gone_song_is_kept_when_the_match_could_not_really_look(self):
        """The text/MBID match swallows a failed search as "no hits", so an
        empty answer counts only while Navidrome is answering and not mid-scan
        (ids and the search index are in flux during one)."""
        for status in ({}, {"scanning": True}):
            with self.subTest(status=status):
                reset, row, _asked = self._gone_by_id(None, status)
                self.assertEqual(reset, 0)
                self.assertEqual((row["decision"], row["nd_song_id"]), ("verified", "s-old"))

    def test_the_recheck_stops_at_the_first_transport_error(self):
        """Fix round 1 (M8): with Navidrome down, every queued row waited out
        its own 10 s timeout. The pass stops at the first transport error and
        keeps that row and the rest queued for the next pass; an answered
        refusal (a bad id, say) only skips its own row."""
        rows = [dict(self._row(t, "verified"), nd_song_id="s-" + t)
                for t in ("One", "Two", "Three")]
        with self._world(rows), self._recheck_by_hand(), \
                patch.object(bot, "_default_web_user",
                             lambda *a, **k: {"navidrome_user": "u",
                                              "navidrome_password": "p"}), \
                contextlib.redirect_stdout(io.StringIO()):
            self._rescan("union", ["One", "Two", "Three"])
            asked = []

            def refuse_then_time_out(u, p, song_id):
                asked.append(song_id)
                if song_id == "s-One":
                    raise bot.NavidromeRefusal("error 0")
                raise OSError("timed out")

            with patch.object(bot, "nd_get_song", refuse_then_time_out):
                self.assertEqual(bot._drain_verified_rechecks(), 0)
            self.assertEqual(asked, ["s-One", "s-Two"])
            self.assertEqual(sorted(song for _i, song in bot._verified_recheck_queue.values()),
                             ["s-Three", "s-Two"])
            self.assertEqual(self._decisions(),
                             {"One": "verified", "Two": "verified", "Three": "verified"})

    def test_nd_get_song_says_gone_only_for_subsonic_error_70(self):
        class Resp:
            def __init__(self, body):
                self.body = body

            def json(self):
                if isinstance(self.body, Exception):
                    raise self.body
                return self.body

        def answer(body):
            return patch.object(bot._http, "get", lambda *a, **k: Resp(body))

        ok = {"subsonic-response": {"status": "ok", "song": {"id": "s1"}}}
        with answer(ok):
            self.assertEqual(bot.nd_get_song("u", "p", "s1"), {"id": "s1"})
        gone = {"subsonic-response": {"status": "failed",
                                      "error": {"code": 70, "message": "not found"}}}
        with answer(gone):
            self.assertIsNone(bot.nd_get_song("u", "p", "s1"))
        for body in ({"subsonic-response": {"status": "failed",
                                            "error": {"code": 40, "message": "bad auth"}}},
                     {"subsonic-response": {"status": "ok"}},
                     {}, ValueError("not json")):
            with self.subTest(body=body), answer(body), \
                    self.assertRaises(bot.NavidromeRefusal):
                bot.nd_get_song("u", "p", "s1")
        # No answer at all is not a refusal: the drain stops on it (M8).
        with patch.object(bot._http, "get",
                          lambda *a, **k: (_ for _ in ()).throw(OSError("timed out"))):
            with self.assertRaises(OSError) as raised:
                bot.nd_get_song("u", "p", "s1")
        self.assertNotIsInstance(raised.exception, bot.NavidromeRefusal)

    def test_the_verifiers_closing_refresh_keeps_what_it_verified_after_a_split(self):
        """The verifier matches library-wide; its closing
        `_refresh_group_counts_after_fill` re-reads only the group's own album
        ids. When Navidrome files the placed tracks under a different album
        record, that refresh still sees them missing — and used to reset the
        rows it had just verified to pending, for the next fetch to download
        again."""
        tracklist = [{"title": t, "mbid": "m-" + t, "position": i + 1}
                     for i, t in enumerate(["Zero", "One", "Two"])]
        # The group's own album record: only the track that was always there.
        record = {"id": "al1", "musicBrainzId": "rel-1", "artist": "A", "name": "B",
                  "tracks": [{"title": "Zero", "musicBrainzId": "m-Zero"}]}
        with self._world([self._row("One", "placed"), self._row("Two", "placed")]), \
                patch.object(bot, "_default_web_user",
                             lambda *a, **k: {"navidrome_user": "u",
                                              "navidrome_password": "p"}), \
                patch.object(bot, "_nd_scanning", lambda *a, **k: False), \
                patch.object(bot, "nd_track_match",
                             # Found — but on the split-off album record.
                             lambda artist, title, *a, **k: {"id": "s-" + title,
                                                            "albumId": "al-split"}), \
                patch.object(bot, "_album_fill_mark_group_verified", lambda *a, **k: None), \
                patch.object(bot, "_announce_album_indexed", lambda *a, **k: None), \
                patch.object(bot, "_placement_verify_delay", lambda started: 0), \
                patch.object(bot, "_nd_album_index", lambda *a, **k: [{"id": "al1"}]), \
                patch.object(bot, "_album_record", lambda *a, **k: record), \
                patch.object(bot, "mbz_release_tracks", lambda *a, **k: tracklist), \
                self._recheck_by_hand():
            bot._verify_placement_worker(self.GID)
            self.assertEqual(self._decisions(), {"One": "verified", "Two": "verified"})
            # Q-023: the refresh disputes both rows, so both are re-checked —
            # and Navidrome still has the songs (on the split record), so both
            # stay verified.
            self.assertEqual(len(bot._verified_recheck_queue), 2)
            with patch.object(bot, "nd_get_song",
                              lambda u, p, song_id: {"id": song_id, "albumId": "al-split"}):
                self.assertEqual(bot._drain_verified_rechecks(), 0)
            self.assertEqual(self._decisions(), {"One": "verified", "Two": "verified"})

    def test_a_verifier_that_verifies_everything_refreshes_the_counts_once(self):
        """Q-024(1): the pass that verified the last row refreshed the counts,
        and the next loop found nothing outstanding and refreshed them again —
        two forced crawls of Navidrome's whole album list per completion.
        Q-024(2): the probe is told which albums are the group's own."""
        refreshes, prefer = [], []

        def match(artist, title, mbid, *a, **k):
            prefer.append(k.get("prefer_album_ids"))
            return {"id": "s-" + title, "albumId": "al1", "title": title}

        with self._world([self._row("One", "placed"), self._row("Two", "placed")]), \
                patch.object(bot, "_default_web_user",
                             lambda *a, **k: {"navidrome_user": "u",
                                              "navidrome_password": "p"}), \
                patch.object(bot, "_nd_scanning", lambda *a, **k: False), \
                patch.object(bot, "nd_track_match", match), \
                patch.object(bot, "_refresh_group_counts_after_fill", refreshes.append), \
                patch.object(bot, "_album_fill_mark_group_verified", lambda *a, **k: None), \
                patch.object(bot, "_announce_album_indexed", lambda *a, **k: None), \
                patch.object(bot, "_placement_verify_delay", lambda started: 0):
            bot._verify_placement_worker(self.GID)
            self.assertEqual(self._decisions(), {"One": "verified", "Two": "verified"})
            # Q-023: the song it matched, for a later re-check.
            with bot._review_lock:
                self.assertEqual([t.get("nd_song_id") for t in
                                  bot._find_review_group(self.GID)["missing_tracks"]],
                                 ["s-One", "s-Two"])
        self.assertEqual(refreshes, [self.GID])
        self.assertEqual(prefer, [{"al1"}, {"al1"}])

    def test_a_row_placed_after_the_refresh_gets_its_own_refresh(self):
        """Fix round 1 (M1): `refreshed` was never reset, so a row placed into
        the running worker after its in-pass refresh was verified with no
        count refresh at all."""
        refreshes, placed_two = [], []

        def delay_and_place_two(clock):
            if not placed_two:
                placed_two.append(True)
                with bot._review_lock:
                    bot._find_review_group(self.GID)["missing_tracks"][1].update(
                        decision="placed", imported_at=time.time())
            return 0

        with self._world([self._row("One", "placed"), self._row("Two")]), \
                patch.object(bot, "_default_web_user",
                             lambda *a, **k: {"navidrome_user": "u",
                                              "navidrome_password": "p"}), \
                patch.object(bot, "_nd_scanning", lambda *a, **k: False), \
                patch.object(bot, "nd_track_match",
                             lambda artist, title, *a, **k: {"id": "s-" + title,
                                                            "albumId": "al1"}), \
                patch.object(bot, "_refresh_group_counts_after_fill", refreshes.append), \
                patch.object(bot, "_album_fill_mark_group_verified", lambda *a, **k: None), \
                patch.object(bot, "_announce_album_indexed", lambda *a, **k: None), \
                patch.object(bot, "_placement_verify_delay", delay_and_place_two):
            bot._verify_placement_worker(self.GID)
            self.assertEqual(self._decisions(), {"One": "verified", "Two": "verified"})
        self.assertEqual(refreshes, [self.GID, self.GID])

    def test_resumed_verifiers_run_a_few_at_a_time_but_all_hold_their_groups(self):
        """Q-024(4): a restart resumed one verifier thread per group with
        placed rows, all at once — each polling Navidrome every 2 s. They now
        take turns (PLACEMENT_VERIFY_RESUME_CONCURRENCY at a time), and every
        group is registered from the start, so a rescan keeps its placed rows
        whether its turn has come or not."""
        gate = threading.Event()
        running, peak, done = [], [], []
        lock = threading.Lock()

        def pass_(gid):
            with lock:
                running.append(gid)
                peak.append(len(running))
            gate.wait(10)
            with lock:
                running.remove(gid)
                done.append(gid)
            with bot._placement_verify_lock:
                bot._placement_verifiers.discard(gid)

        groups = []
        for n in range(4):
            g = AlbumReviewTests._origin_group(f"g{n}", "library", album=f"A{n}")
            g["missing_tracks"] = [self._row("One", "placed")]
            groups.append(g)
        with isolated_review(), \
                patch.object(bot, "_placement_verifiers", set()), \
                patch.object(bot, "_verify_placement_worker", pass_), \
                contextlib.redirect_stdout(io.StringIO()):
            with bot._review_lock:
                bot._review_state["groups"] = groups
            self.assertEqual(bot._resume_placement_verification(), 4)
            try:
                deadline = time.time() + 5
                while len(running) < bot.PLACEMENT_VERIFY_RESUME_CONCURRENCY \
                        and time.time() < deadline:
                    time.sleep(0.01)
                time.sleep(0.1)
                self.assertEqual(len(running), bot.PLACEMENT_VERIFY_RESUME_CONCURRENCY)
                self.assertEqual(bot._placement_verifiers, {"g0", "g1", "g2", "g3"})
            finally:
                gate.set()
                for t in threading.enumerate():
                    if t.name.startswith("verify-g"):
                        t.join(5)
        self.assertEqual(sorted(done), ["g0", "g1", "g2", "g3"])
        self.assertLessEqual(max(peak), bot.PLACEMENT_VERIFY_RESUME_CONCURRENCY)

    def test_a_placed_row_its_verifier_is_watching_stays_placed(self):
        """The same hole as a live transfer, one step later: until Navidrome
        indexes a placed file (PLACEMENT_VERIFY_TIMEOUT), a rescan still reports
        the track missing, and resetting the row to pending took it away from
        the verifier and offered it for a second download. The verifier is the
        authority while it runs — it verifies the row or resets it itself at
        the deadline — so a placed row survives a rescan exactly while one is
        running for its group. (The projection used to restore it.)"""
        for path in self.PATHS:
            with self.subTest(path=path), \
                    self._world([self._row("One", "placed"),
                                 self._row("Two", "verified")]), \
                    patch.object(bot, "_placement_verifiers", {self.GID}):
                self._rescan(path, ["One", "Two"])
                self.assertEqual(self._decisions(),
                                 {"One": "placed", "Two": "verified"})

    def test_a_moved_row_takes_its_transfer_with_it(self):
        """The poller, the source switch and the cancel paths address a row by
        index. A rescan that lists a new track first moves every row down one;
        left alone, the poller's "downloaded" for One would land on the new
        track and One would sit on queued forever."""
        for path in self.PATHS:
            with self.subTest(path=path), \
                    self._world([self._row("One", "queued"),
                                 self._row("Two", "downloading")]):
                one, two = self._transfer("One", 0), self._transfer("Two", 1)
                # The album fill's own bookkeeping holds the same track
                # dicts the transfers do — a re-point must move each once.
                bot.pending_album_groups["ag1"] = {
                    "review_group_id": self.GID, "review_track_indexes": [0, 1],
                    "missing_tracks": [one["track"], two["track"]]}
                self._rescan(path, ["New", "One", "Two"])

                self.assertEqual((one["review_track_index"], two["review_track_index"]),
                                 (1, 2))
                ag = bot.pending_album_groups["ag1"]
                self.assertEqual(ag["review_track_indexes"], [1, 2])
                self.assertEqual([t["_review_track_index"] for t in ag["missing_tracks"]],
                                 [1, 2])
                # What the poller does next, with what it now reads.
                bot._set_review_track_state(self.GID, one["review_track_index"],
                                            "downloaded", local_path="/d/One.flac")
                self.assertEqual(self._decisions(),
                                 {"New": "pending", "One": "downloaded",
                                  "Two": "downloading"})

    def test_a_row_the_rescan_drops_lets_go_of_its_transfer(self):
        """A row the rescan no longer lists (the track turned up in the
        library) has no index any more; keeping the old one would aim the
        poller at whichever track now sits there.

        Q-027(a): and the transfer itself goes. It used to keep downloading
        into /downloads with nothing pointing at it, and its album fill kept
        waiting for it. It is detached, cancelled in slskd and taken out of
        the fill's total — never below what already completed. The transfer
        that merely moved keeps going."""
        for path in self.PATHS:
            for total, completed, want in ((3, 1, 2), (2, 2, 2)):
                abandoned = []
                with self.subTest(path=path, total=total, completed=completed), \
                        self._world([self._row("One", "queued"),
                                     self._row("Two", "downloading")]), \
                        patch.object(bot, "_abandon_transfers_async", abandoned.extend):
                    one, two = self._transfer("One", 0), self._transfer("Two", 1)
                    bot.pending_album_groups["ag1"] = {
                        "review_group_id": self.GID, "review_track_indexes": [0, 1],
                        "missing_tracks": [one["track"], two["track"]],
                        "total": total, "completed": completed, "failed": 0}
                    self._rescan(path, ["Two"], present=["One"])

                    self.assertIsNone(one["review_track_index"])
                    self.assertEqual(two["review_track_index"], 0)
                    self.assertEqual(bot.pending_album_groups["ag1"]["review_track_indexes"], [0])
                    bot._set_review_track_state(self.GID, one["review_track_index"], "downloaded")
                    self.assertEqual(self._decisions(), {"Two": "downloading"})

                    self.assertNotIn(("peer", "x/One.flac"), bot.pending_downloads)
                    self.assertIs(bot.pending_downloads[("peer", "x/Two.flac")], two)
                    self.assertEqual([(e["username"], e["filename"]) for e in abandoned],
                                     [("peer", "x/One.flac")])
                    self.assertEqual(bot.pending_album_groups["ag1"]["total"], want)

    def test_an_empty_tracklist_read_leaves_the_group_and_its_fill_alone(self):
        """Fix round 1: `_missing_for_album_records` answers `missing=[]` when
        the tracklist read comes back empty — a MusicBrainz outage inside the
        5-minute cooldown, an evicted cache entry, a canonical record with no
        MBID — and `refresh_group_missing` took that as "nothing is missing":
        every row dropped, and with Q-027(a) every transfer of the running fill
        was cancelled. An empty read says nothing; the group is left as it was
        (the rule `_reconcile_artist_review_groups` already follows)."""
        abandoned = []
        with self._world([self._row("One", "queued"), self._row("Two", "downloading")]), \
                patch.object(bot, "_abandon_transfers_async", abandoned.extend), \
                contextlib.redirect_stdout(io.StringIO()):
            one, two = self._transfer("One", 0), self._transfer("Two", 1)
            bot.pending_album_groups["ag1"] = {
                "review_group_id": self.GID, "review_track_indexes": [0, 1],
                "missing_tracks": [one["track"], two["track"]],
                "total": 2, "completed": 0, "failed": 0}
            with bot._review_lock:
                before = json.loads(json.dumps(bot._find_review_group(self.GID)))
            self._rescan("refresh", [])           # the tracklist read is empty
            with bot._review_lock:
                after = json.loads(json.dumps(bot._find_review_group(self.GID)))
            for field in ("missing_tracks", "present", "total", "canonical_mbid"):
                self.assertEqual(after.get(field), before.get(field), field)
            self.assertEqual(abandoned, [])
            self.assertEqual(set(bot.pending_downloads),
                             {("peer", "x/One.flac"), ("peer", "x/Two.flac")})
            self.assertEqual((one["review_track_index"], two["review_track_index"]), (0, 1))
            self.assertEqual(bot.pending_album_groups["ag1"]["total"], 2)

    def _pinned_world(self, rows):
        """The group the discography classifier pinned to `rel-pinned` (a
        release in the matched release-group), while its album record's own
        tag points at `rel-wrong` — and a two-file fill of the pinned release
        in flight."""
        stack = contextlib.ExitStack()
        stack.enter_context(self._world(rows))
        with bot._review_lock:
            g = bot._find_review_group(self.GID)
            g["canonical_mbid"] = "rel-pinned"
            g["albums"] = [{"id": "al1", "musicBrainzId": "rel-wrong",
                            "artist": "A", "name": "B", "tracks": []}]
        one, two = self._transfer("One", 0), self._transfer("Two", 1)
        bot.pending_album_groups["ag1"] = {
            "review_group_id": self.GID, "review_track_indexes": [0, 1],
            "missing_tracks": [one["track"], two["track"]],
            "total": 2, "completed": 0, "failed": 0}
        lists = {"rel-pinned": [{"title": t, "mbid": "m-" + t, "position": i + 1}
                                for i, t in enumerate(["One", "Two"])],
                 "rel-wrong": [{"title": t, "mbid": "m-" + t, "position": i + 1}
                               for i, t in enumerate(["Live1", "Live2", "Live3"])]}
        stack.enter_context(patch.object(bot, "mbz_release_tracks",
                                          lambda mbid, *a, **k: lists.get(mbid, [])))
        return stack

    def test_a_refresh_never_swaps_a_pinned_release_under_a_running_fill(self):
        """Final review I-1: the wrong-release guard pins `canonical_mbid` to a
        release in the matched release-group, but `refresh_group_missing`
        rebuilt from the record's own (wrong) tag — every in-flight row of the
        pinned release read as dropped, and Q-027(a) cancelled its transfer.
        While a fill is running, a refresh that would switch releases leaves
        the group as it was."""
        abandoned = []
        with self._pinned_world([self._row("One", "queued"),
                                 self._row("Two", "downloading")]), \
                patch.object(bot, "_abandon_transfers_async", abandoned.extend), \
                contextlib.redirect_stdout(io.StringIO()):
            with bot._review_lock:
                bot.refresh_group_missing(bot._find_review_group(self.GID))
                g = bot._find_review_group(self.GID)
                self.assertEqual(g["canonical_mbid"], "rel-pinned")
                self.assertEqual([(t["title"], t["decision"]) for t in g["missing_tracks"]],
                                 [("One", "queued"), ("Two", "downloading")])
            self.assertEqual(abandoned, [])
            self.assertEqual(set(bot.pending_downloads),
                             {("peer", "x/One.flac"), ("peer", "x/Two.flac")})
            self.assertEqual(bot.pending_album_groups["ag1"]["total"], 2)

    def test_a_canonical_pick_that_cannot_be_applied_is_refused_whole(self):
        """Final review M-1: the route set the new `canonical_album_id` and
        then the refresh left everything else as it was (a record with no
        MBID, a MusicBrainz cooldown) — the new id beside the old release's
        MBID, name and rows, reported as "Canonical album updated". The pick
        is refused, the previous id kept, and the reply says why."""
        with self._world([self._row("One")]), \
                patch.object(bot, "_save_state", lambda *a, **k: None), \
                contextlib.redirect_stdout(io.StringIO()):
            with bot._review_lock:
                bot._find_review_group(self.GID)["albums"].append(
                    {"id": "al2", "musicBrainzId": "", "artist": "A",
                     "name": "B (another copy)", "tracks": []})
            r = self.client.post(f"/api/groups/{self.GID}/canonical",
                                 json={"album_id": "al2"})
            self.assertEqual(r.status_code, 409)
            body = r.get_json()
            self.assertIn("not changed", body["error"])
            self.assertIn("no MusicBrainz release", body["error"])
            self.assertEqual(body["operation"]["status"], "error")
            with bot._review_lock:
                g = bot._find_review_group(self.GID)
                self.assertEqual((g["canonical_album_id"], g["canonical_mbid"]),
                                 ("al1", "rel-1"))
                self.assertEqual([t["title"] for t in g["missing_tracks"]], ["One"])
                self.assertNotEqual(g.get("last_action"), "canonical")

    def test_a_missing_rescan_that_could_not_run_says_so(self):
        """M-1's other route: "Missing tracks rescanned" for a refresh that
        read no tracklist and changed nothing."""
        with self._world([self._row("One")]), \
                patch.object(bot, "_save_state", lambda *a, **k: None), \
                patch.object(bot, "mbz_release_tracks", lambda *a, **k: []), \
                contextlib.redirect_stdout(io.StringIO()):
            r = self.client.post(f"/api/groups/{self.GID}/missing", json={})
            self.assertEqual(r.status_code, 409)
            self.assertIn("not rescanned", r.get_json()["error"])
            self.assertIn("no tracklist", r.get_json()["error"])
            with bot._review_lock:
                g = bot._find_review_group(self.GID)
                self.assertEqual([t["title"] for t in g["missing_tracks"]], ["One"])
                self.assertNotEqual(g.get("last_action"), "missing_scan")
            # And a refresh that does run still answers as before.
            tracklist = [{"title": "One", "mbid": "m-One", "position": 1}]
            with patch.object(bot, "mbz_release_tracks", lambda *a, **k: tracklist):
                r = self.client.post(f"/api/groups/{self.GID}/missing", json={})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["message"], "Missing tracks rescanned")

    def test_with_nothing_in_flight_a_refresh_still_follows_the_record(self):
        """The guard is only for a running fill: with no live transfer the
        refresh behaves as it always did (the root cause is backlog work)."""
        with self._world([self._row("One"), self._row("Two")]), \
                contextlib.redirect_stdout(io.StringIO()):
            with bot._review_lock:
                g = bot._find_review_group(self.GID)
                g["canonical_mbid"] = "rel-pinned"
                g["albums"] = [{"id": "al1", "musicBrainzId": "rel-wrong",
                                "artist": "A", "name": "B", "tracks": []}]
            with patch.object(bot, "mbz_release_tracks",
                              lambda mbid, *a, **k: [{"title": "Live1", "mbid": "m-Live1",
                                                      "position": 1}]
                              if mbid == "rel-wrong" else []), bot._review_lock:
                bot.refresh_group_missing(bot._find_review_group(self.GID))
                g = bot._find_review_group(self.GID)
                self.assertEqual(g["canonical_mbid"], "rel-wrong")
                self.assertEqual([t["title"] for t in g["missing_tracks"]], ["Live1"])

    def test_a_dropped_twin_lets_go_of_its_own_transfer_only(self):
        """Q-027(a) with (b)'s twins: two rows under one key, the rescan lists
        one. Paired in order, the second twin is the dropped one — its transfer
        goes, the first's stays where it was."""
        for path in self.PATHS:
            abandoned = []
            with self.subTest(path=path), \
                    self._world([self._row("One", "queued"),
                                 self._row("One", "downloading")]), \
                    patch.object(bot, "_abandon_transfers_async", abandoned.extend):
                first = self._transfer("One", 0)
                second = {"review_group_id": self.GID, "review_track_index": 1,
                          "track": {"title": "One", "mbid": "m-One"}}
                bot.pending_downloads[("peer2", "y/One.flac")] = second
                self._rescan(path, ["One"])
                self.assertEqual(first["review_track_index"], 0)
                self.assertIsNone(second["review_track_index"])
                self.assertEqual(list(bot.pending_downloads), [("peer", "x/One.flac")])
                self.assertEqual([e["username"] for e in abandoned], ["peer2"])
                with bot._review_lock:
                    self.assertEqual(
                        [t["decision"] for t in
                         bot._find_review_group(self.GID)["missing_tracks"]], ["queued"])

    # --- final review I1: a write that held its index across a slow step -----

    def test_an_identity_checked_write_follows_its_row_or_is_dropped(self):
        """The re-point reaches indexes held in the transfer registry and the
        album fills. A writer that read an index and then waited (the verifier
        on Navidrome, the poller on the disk, a cancel whose transfer is already
        detached) names its row too; when the row at the index is not that row
        any more, the write lands on the row that is, or nowhere."""
        with self._world([self._row("New"), self._row("One", "queued"),
                          self._row("Two", "queued")]):
            # Index 0 held One before a rescan listed New first.
            bot._set_review_track_state(self.GID, 0, "downloaded",
                                        expect_key=("m-One", "One"),
                                        local_path="/d/One.flac")
            self.assertEqual(self._decisions(),
                             {"New": "pending", "One": "downloaded", "Two": "queued"})
            with bot._review_lock:
                rows = bot._find_review_group(self.GID)["missing_tracks"]
                self.assertEqual(rows[1].get("local_path"), "/d/One.flac")
                self.assertNotIn("expect_key", rows[0])
                self.assertNotIn("local_path", rows[0])

            # A row the group no longer lists: nothing is written, said once.
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                for _ in range(3):
                    bot._set_review_track_state(self.GID, 2, "downloaded",
                                                expect_key=("m-Gone", "Gone"))
            self.assertEqual(self._decisions(),
                             {"New": "pending", "One": "downloaded", "Two": "queued"})
            self.assertEqual(out.getvalue().count("Gone"), 1, out.getvalue())

            # The index still holding its row is written as before.
            bot._set_review_track_state(self.GID, 2, "downloading",
                                        expect_key=("m-Two", "Two"))
            self.assertEqual(self._decisions()["Two"], "downloading")

    def test_twin_rows_each_keep_their_own_state_across_a_rescan(self):
        """Q-027(b): two missing rows sharing one (mbid, title) — the previous
        rows were a dict on that key, last wins, so both twins inherited the
        second's state: the live one lost its flag and went pending, or the
        idle one took the live one's `queued` with nothing behind it. Paired in
        order now, like `_repoint_transfer_indexes` pairs their transfers."""
        for path in self.PATHS:
            for decisions, live_at in ((["queued", "pending"], 0),
                                       (["pending", "downloading"], 1)):
                with self.subTest(path=path, decisions=decisions), \
                        self._world([self._row("One", d) for d in decisions]):
                    info = self._transfer("One", live_at)
                    self._rescan(path, ["One", "One"])
                    with bot._review_lock:
                        rows = bot._find_review_group(self.GID)["missing_tracks"]
                        self.assertEqual([t["decision"] for t in rows], decisions)
                    self.assertEqual(info["review_track_index"], live_at)

    def test_an_identity_checked_write_never_guesses_between_twins(self):
        """Two rows under one key (M5) and the index on neither: which one the
        writer meant is unknowable, and a guess is someone else's row."""
        with self._world([self._row("New"), self._row("One"), self._row("One")]):
            with contextlib.redirect_stdout(io.StringIO()):
                bot._set_review_track_state(self.GID, 0, "downloaded",
                                            expect_key=("m-One", "One"))
            with bot._review_lock:
                self.assertEqual(
                    [t["decision"] for t in
                     bot._find_review_group(self.GID)["missing_tracks"]],
                    ["pending", "pending", "pending"])

    def test_the_verifier_never_verifies_the_row_a_rescan_moved_under_it(self):
        """The reviewer's probe. The verifier snapshots (index, row), asks
        Navidrome, then writes `verified` by index. When the auto-index worker's
        union drops two now-present tracks in between, index 0 holds a track
        that was never downloaded — and it read `verified`, the group
        `completed`, for good: nothing ever revisits a verified row."""
        for path in self.PATHS:
            with self.subTest(path=path), \
                    self._world([self._row("A", "placed"), self._row("B", "placed"),
                                 self._row("C")]), \
                    patch.object(bot, "_default_web_user",
                                 lambda *a, **k: {"navidrome_user": "u",
                                                  "navidrome_password": "p"}), \
                    patch.object(bot, "_nd_scanning", lambda *a, **k: False), \
                    patch.object(bot, "_album_fill_mark_group_verified",
                                 lambda *a, **k: None), \
                    patch.object(bot, "_announce_album_indexed", lambda *a, **k: None), \
                    patch.object(bot, "_refresh_group_counts_after_fill",
                                 lambda gid: None), \
                    patch.object(bot, "_placement_verify_delay", lambda started: 0):
                rebuilt = []

                def match(artist, title, mbid, *a, **k):
                    # Navidrome has indexed A and B; while the verifier is on
                    # the network a rescan sees them present.
                    if not rebuilt:
                        rebuilt.append(True)
                        self._rescan(path, ["C"], present=["A", "B"])
                    return {"id": "s-" + title, "albumId": "al1", "title": title}

                with patch.object(bot, "nd_track_match", match), \
                        contextlib.redirect_stdout(io.StringIO()):
                    bot._verify_placement_worker(self.GID)
                self.assertEqual(self._decisions(), {"C": "pending"})
                with bot._review_lock:
                    group = bot._find_review_group(self.GID)
                    self.assertNotEqual(
                        bot._review_group_next_action(group)["bucket"], "completed")

    def _poll(self, listing, resolve):
        """One poll of `_poll_downloads_once` against `listing`, with
        `_resolve_local_path` replaced by `resolve`."""
        with patch.object(bot, "slskd_get_all_downloads", lambda force=False: listing), \
                patch.object(bot, "_resolve_local_path", resolve), \
                patch.object(bot, "_remember_download_origin", lambda *a, **k: None), \
                patch.object(bot, "_push_fill_progress", lambda *a, **k: None), \
                patch.object(bot, "_update_group_progress", new_callable=AsyncMock), \
                patch.object(bot, "_tg_send", new_callable=AsyncMock), \
                patch.object(bot, "_save_state", lambda *a, **k: None), \
                patch.object(bot, "_sweep_album_fill_zombies", lambda *a, **k: 0):
            asyncio.run(bot._poll_downloads_once({}))

    def test_the_pollers_downloaded_lands_on_the_row_a_rescan_moved(self):
        """The poller reads the transfer's index, then awaits
        `_resolve_local_path` (an os.walk of /downloads, seconds on the NAS). A
        rescan in that await re-points the transfer — but the poller wrote
        "downloaded" to the index it had read, i.e. onto the neighbour."""
        for path in self.PATHS:
            with self.subTest(path=path), \
                    self._world([self._row("One", "downloading"),
                                 self._row("Two", "downloading")]):
                one, two = self._transfer("One", 0), self._transfer("Two", 1)
                for info in (one, two):
                    info.update(token="tok", chat_id="chat", candidates=[])
                    info["track"]["artist"] = "A"
                bot.pending_album_groups["ag1"] = {
                    "review_group_id": self.GID, "review_track_indexes": [0, 1],
                    "missing_tracks": [one["track"], two["track"]],
                    "token": "tok", "chat_id": "chat", "label": "B",
                    "total": 2, "completed": 0, "failed": 0, "local_dirs": {},
                    "ts": time.time()}

                def resolve(filename):
                    self._rescan(path, ["New", "One", "Two"])
                    return "/d/Two.flac"

                self._poll([{"_username": "peer", "filename": "x/Two.flac",
                             "state": "Completed, Succeeded"}], resolve)
                self.assertEqual(self._decisions(),
                                 {"New": "pending", "One": "downloading",
                                  "Two": "downloaded"})
                with bot._review_lock:
                    rows = bot._find_review_group(self.GID)["missing_tracks"]
                    self.assertEqual(rows[2].get("local_path"), "/d/Two.flac")
                    self.assertFalse(rows[1].get("local_path"))

    def test_a_cancel_marks_the_row_its_detached_transfer_was_for(self):
        """A cancel detaches its transfers first (out of the registry, so out
        of the re-point's reach) and marks their rows after. A rescan between
        the two marked the neighbour `cancelled` and left the cancelled track
        reading `downloading`."""
        with self._world([self._row("One", "downloading"),
                          self._row("Two", "downloading")]):
            self._transfer("One", 0)
            key = ("peer", "x/One.flac")
            entries = [bot._detach_transfer(key, bot.pending_downloads[key])]
            self._rescan("union", ["New", "One", "Two"])
            bot._mark_detached_entries_cancelled(entries, bot.USER_CANCEL_REASON)
            self.assertEqual(self._decisions(),
                             {"New": "pending", "One": "cancelled",
                              "Two": "pending"})

    def test_a_transfer_registers_at_the_index_its_row_has_after_the_enqueue(self):
        """`slskd_enqueue` registers the transfer only after slskd answers (up
        to 30 s). A rescan in that call re-points everything registered — not
        this one, which then registered the index read before the call."""
        with self._world([self._row("One", "approved"),
                          self._row("Two", "approved")]):
            track = {"title": "Two", "mbid": "m-Two",
                     "_review_group_id": self.GID, "_review_track_index": 1}

            class _Resp:
                ok, status_code, text = True, 200, ""

            def post(*a, **k):
                self._rescan("union", ["New", "One", "Two"])
                return _Resp()

            with patch.object(bot._http, "post", post), \
                    patch.object(bot, "_source_is_rejected", lambda *a, **k: False):
                self.assertTrue(bot.slskd_enqueue(
                    "peer", {"filename": "x/Two.flac", "size": 1}, track=track,
                    token="tok", chat_id="chat", review_group_id=self.GID,
                    review_track_index=1))
            info = bot.pending_downloads[("peer", "x/Two.flac")]
            self.assertEqual(info["review_track_index"], 2)
            # The caller's follow-up "queued" reads the track's own index.
            self.assertEqual(track["_review_track_index"], 2)

    # --- final review I3: a restart ends every verifier ----------------------

    def test_a_restart_resumes_a_verifier_for_each_group_with_placed_rows(self):
        """Verifiers are threads a placement starts, so a restart ends them all
        and nothing started them again: the rescan's "keep placed while a
        verifier watches" rule (f7409a2) then protected nothing, and the first
        rescan after a deploy reset every placed row to pending."""
        started = []
        with isolated_review(), \
                patch.object(bot, "_start_placement_verification",
                             lambda gid, **k: started.append((gid, k.get("resumed")))):
            groups = []
            for gid, rows in (("g1", [self._row("One", "placed"), self._row("Two")]),
                              ("g2", [self._row("One", "verified"), self._row("Two")]),
                              ("g3", [self._row("One", "placed"),
                                      self._row("Two", "placed")]),
                              ("g4", [])):
                g = AlbumReviewTests._origin_group(gid, "library", album=gid)
                g["missing_tracks"] = rows
                groups.append(g)
            with bot._review_lock:
                bot._review_state["groups"] = groups
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(bot._resume_placement_verification(), 2)
        # Resumed: each takes its turn at the resume gate (Q-024).
        self.assertEqual(started, [("g1", True), ("g3", True)])

    def test_a_rescan_in_the_resumed_window_keeps_placed_rows(self):
        release = threading.Event()

        def watching(gid):
            # A verifier still inside its window: it holds the group.
            release.wait(10)
            with bot._placement_verify_lock:
                bot._placement_verifiers.discard(gid)

        for path in self.PATHS:
            release.clear()
            with self.subTest(path=path), \
                    self._world([self._row("One", "placed"),
                                 self._row("Two", "verified"), self._row("Three")]), \
                    patch.object(bot, "_placement_verifiers", set()), \
                    patch.object(bot, "_verify_placement_worker", watching), \
                    contextlib.redirect_stdout(io.StringIO()):
                bot._resume_placement_verification()
                try:
                    self._rescan(path, ["One", "Two", "Three"])
                    self.assertEqual(self._decisions(),
                                     {"One": "placed", "Two": "verified",
                                      "Three": "pending"})
                finally:
                    release.set()
                    for t in threading.enumerate():
                        if t.name == f"verify-{self.GID[:16]}":
                            t.join(5)

    def test_main_resumes_verifiers_once_the_review_is_loaded(self):
        """Before anything can rescan: the auto-index worker, the scheduler and
        the routes all start later in main()."""
        src = inspect.getsource(bot.main)
        loaded = src.index("_load_review_state()")
        resumed = src.index("_resume_placement_verification()")
        self.assertLess(loaded, resumed)
        self.assertLess(resumed, src.index("start_web_dashboard()"))
        self.assertLess(resumed, src.index("tuesday_scheduler"))

    def test_the_linker_writes_the_rows_it_matched_even_after_a_rescan(self):
        """The loose-track linker collects (index, outcome) under the lock and
        writes after releasing it; a rescan in between moved the outcome onto
        another row."""
        with self._world([]):
            loose = self._group([dict(self._row("One", "downloaded"),
                                      local_path="/d/f/One.flac")])
            loose.update(group_type="tracks", origin="playlist",
                         canonical_mbid="", canonical_album_id="", albums=[])
            with bot._review_lock:
                bot._review_state["groups"] = [loose]
            real = bot._set_review_track_state
            moved = []

            def racing(*a, **k):
                if not moved:
                    moved.append(True)
                    fresh = self._group([self._row("New"), self._row("One")])
                    fresh.update(group_type="tracks", origin="playlist",
                                 canonical_mbid="", canonical_album_id="", albums=[])
                    bot._union_review_groups([fresh], "playlist")
                return real(*a, **k)

            with patch.object(bot, "_set_review_track_state", racing), \
                    patch.object(bot, "_start_placement_verification", lambda gid: None), \
                    contextlib.redirect_stdout(io.StringIO()):
                bot._link_loose_track_placements("/d/f", {"per_file": [
                    {"status": "matched", "file": "One.flac", "title": "One",
                     "recording_mbid": "m-One"}]})
            with bot._review_lock:
                rows = {t["title"]: t["decision"] for t in
                        bot._find_review_group(self.GID)["missing_tracks"]}
            self.assertEqual(rows, {"New": "pending", "One": "placed"})



class RetiredRepairGroupsMigrationTests(unittest.TestCase):
    """B-022: the groups the repair-job resurrection left behind (origin
    `repair`, no albums, nothing that can clear them — 58 on the NAS) are
    hidden once at startup, not deleted: unhide is one tap, and their files
    stay reachable from Placement."""

    @staticmethod
    def _g(gid, origin, hidden=False):
        g = AlbumReviewTests._origin_group(gid, origin, album=gid, hidden=hidden)
        g["missing_tracks"] = [{"mbid": "m", "title": "T", "decision": "downloaded"}]
        return g

    def _seed(self, groups):
        with bot._review_lock:
            bot._review_state["groups"] = groups
        bot._mark_review_groups_dirty(groups)
        bot._save_review_state(urgent=True)

    def _reload(self):
        swap_index(bot.LIBRARY_INDEX_FILE)
        with bot._review_lock:
            bot._review_state = bot._empty_review_state()
        bot._load_review_state()
        return {g["id"]: g for g in bot._review_state["groups"]}

    def _run(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            n = bot._hide_retired_repair_groups()
        return n, out.getvalue()

    def test_hides_only_visible_repair_groups_and_logs_the_count(self):
        with isolated_review():
            self._seed([self._g("r1", "repair"), self._g("r2", "repair"),
                        self._g("r3", "repair", hidden=True),
                        self._g("lib0", "library"), self._g("pl0", "playlist")])
            n, log = self._run()
            self.assertEqual(n, 2)
            self.assertIn("2 ", log)
            groups = self._reload()
            self.assertEqual({gid: bool(g.get("hidden")) for gid, g in groups.items()},
                             {"r1": True, "r2": True, "r3": True,
                              "lib0": False, "pl0": False})
            # Hidden, not deleted: the rows and their downloaded tracks are all there.
            self.assertEqual(groups["r1"]["missing_tracks"][0]["decision"], "downloaded")
            # The column the gap list filters on says so too, not just the payload.
            with bot._index_lock:
                rows = dict(bot._index_db().execute(
                    "SELECT id, hidden FROM review_groups").fetchall())
            self.assertEqual(rows, {"r1": 1, "r2": 1, "r3": 1, "lib0": 0, "pl0": 0})

    def test_runs_once_so_an_unhide_sticks(self):
        with isolated_review():
            self._seed([self._g("r1", "repair")])
            self.assertEqual(self._run()[0], 1)
            # The user unhides it; the next start must not hide it again.
            bot._find_review_group("r1")["hidden"] = False
            bot._save_review_state(urgent=True)
            n, log = self._run()
            self.assertEqual((n, log), (0, ""))
            self.assertFalse(self._reload()["r1"].get("hidden"))

    def test_a_failed_write_records_nothing_and_retries(self):
        """The flag is written only after the rows landed, like the JSON import:
        a start whose flush fails must try again at the next one."""
        import sqlite3
        with isolated_review():
            self._seed([self._g("r1", "repair")])

            def broken(writes, drops):
                raise sqlite3.OperationalError("attempt to write a readonly database")

            with patch.object(bot, "_review_groups_write", broken):
                self.assertEqual(self._run()[0], 0)
            with bot._review_lock:
                bot._review_dirty_groups.clear()
                bot._find_review_group("r1")["hidden"] = False
            self.assertEqual(self._run()[0], 1)
            self.assertTrue(self._reload()["r1"].get("hidden"))

    def test_a_review_with_no_repair_groups_is_a_no_op(self):
        with isolated_review():
            self._seed([self._g("lib0", "library")])
            n, log = self._run()
            self.assertEqual(n, 0)
            self.assertIn("0 ", log)
            self.assertFalse(self._reload()["lib0"].get("hidden"))
            self.assertEqual(self._run(), (0, ""))



class RetiredRepairRowsDoNotShadowTests(unittest.TestCase):
    """B-022 fix round 1: a retired origin-`repair` row carries the id of the
    group its job was for (`_review_group_from_repair_job` used
    `job["group_id"]`) and, since the migration, `hidden=True`. When a scan
    produces that album again the real group must come back visible — not
    inherit the zombie's `hidden` (union), not be dropped behind it (replace),
    and not be folded into it by identity."""

    def setUp(self):
        for patcher in (patch.object(bot, "_push_gap", lambda gid: None),
                        patch.object(bot, "_save_review_state", lambda **k: None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _zombie(gid="rg1"):
        g = AlbumReviewTests._origin_group(gid, "repair", album="Record",
                                           hidden=True, group_type="repair_job",
                                           canonical_mbid="rel-1", albums=[])
        g["missing_tracks"] = [{"mbid": "m1", "title": "One", "decision": "downloaded"}]
        return g

    @staticmethod
    def _real(gid, origin="library", albums=True):
        g = AlbumReviewTests._origin_group(gid, origin, album="Record",
                                           canonical_mbid="rel-1")
        if albums:
            g.update(albums=[{"id": "al1"}], canonical_album_id="al1")
        g["missing_tracks"] = [{"mbid": "m1", "title": "One", "decision": "pending"},
                               {"mbid": "m2", "title": "Two", "decision": "pending"}]
        return g

    def _rows(self):
        with bot._review_lock:
            return sorted((g["id"], bot._review_group_origin(g), bool(g.get("hidden")),
                           tuple(t["title"] for t in g.get("missing_tracks", [])))
                          for g in bot._review_state["groups"])

    def _scan(self, path, group):
        if path == "union":
            bot._union_review_groups([group], group["origin"])
        else:
            bot._replace_review_groups(group["origin"], [group], "x")

    def test_the_real_group_reclaims_the_zombies_id_visibly(self):
        for path in ("union", "replace"):
            with self.subTest(path=path), isolated_review():
                with bot._review_lock:
                    bot._review_state["groups"] = [self._zombie()]
                self._scan(path, self._real("rg1"))
                self.assertEqual(self._rows(),
                                 [("rg1", "library", False, ("One", "Two"))])

    def test_a_zombie_never_absorbs_or_hides_an_album_by_identity(self):
        """Same album under another id (the release drifted, or another
        origin): folding into the hidden zombie hid the album, and the zombie's
        tracks folded into the real row would list a placed track as missing."""
        for path, origin, albums in (("union", "library", True),
                                     ("replace", "library", True),
                                     ("replace", "playlist", False)):
            with self.subTest(path=path, origin=origin), isolated_review():
                with bot._review_lock:
                    bot._review_state["groups"] = [self._zombie()]
                self._scan(path, self._real("rg2", origin, albums))
                self.assertEqual(self._rows(),
                                 [("rg1", "repair", True, ("One",)),
                                  ("rg2", origin, False, ("One", "Two"))])

    def test_a_hidden_real_group_stays_hidden(self):
        """The control: only a zombie's `hidden` is not the user's statement."""
        for path in ("union", "replace"):
            with self.subTest(path=path), isolated_review():
                hidden = self._real("rg1")
                hidden["hidden"] = True
                with bot._review_lock:
                    bot._review_state["groups"] = [hidden]
                self._scan(path, self._real("rg1"))
                self.assertEqual(self._rows(),
                                 [("rg1", "library", True, ("One", "Two"))])

if __name__ == "__main__":
    unittest.main()
