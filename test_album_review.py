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


@contextlib.contextmanager
def isolated_review():
    """An empty review with its own JSON file and its own library_index.db.

    Review groups are rows in library_index.db, so a test that exercises the
    review's persistence needs both redirected — otherwise it writes into
    /config (absent here, so the group write fails) or into the real index.
    Yields the temp dir.
    """
    old_file = bot.REVIEW_FILE
    old_index, old_conn = bot.LIBRARY_INDEX_FILE, bot._index_conn
    old_state = bot._review_snapshot()
    with tempfile.TemporaryDirectory() as td:
        bot.REVIEW_FILE = os.path.join(td, "review.json")
        bot.LIBRARY_INDEX_FILE = os.path.join(td, "index.db")
        bot._index_conn = None
        with bot._review_lock:
            bot._review_state = bot._empty_review_state()
            bot._review_dirty_groups.clear()
        bot._review_dirty.clear()
        try:
            yield td
        finally:
            if bot._index_conn is not None:
                bot._index_conn.close()
            bot._index_conn = old_conn
            bot.REVIEW_FILE = old_file
            bot.LIBRARY_INDEX_FILE = old_index
            bot._review_dirty.clear()
            with bot._review_lock:
                bot._review_state = old_state
                bot._review_dirty_groups.clear()


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
        with isolated_review(), patch.object(bot, "repair_jobs", {}):
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
        with isolated_review(), patch.object(bot, "repair_jobs", {}):
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
        with isolated_review(), patch.object(bot, "repair_jobs", {}):
            for album in ("First", "Second"):
                bot._replace_review_groups(
                    "playlist", [self._origin_group("pl", "playlist", album=album)], "x")
            self.assertEqual([g["album"] for g in bot._review_state["groups"]],
                             ["Second"])

    def test_a_playlist_album_the_library_already_lists_folds_in(self):
        """Group ids are built per origin, so the same album reached two ways
        has two ids. Without folding, the rail shows it twice."""
        with isolated_review(), patch.object(bot, "repair_jobs", {}):
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

    def test_an_active_repair_job_survives_a_library_scan_as_one_row(self):
        """_merge_review_groups appends a group for every active repair job, so
        a library scan both keeps the existing repair row and re-synthesizes
        one. They must collapse — two rows for one album is the thing origin
        folding exists to prevent."""
        job = {"id": "job1", "group_id": "rg1", "artist": "A", "album": "B",
               "status": "needs_review", "tracks": [], "downloads": [],
               "source_pools": [], "file_matches": [], "import_attempts": [],
               "verification": {}, "messages": [], "created_at": 1,
               "updated_at": 1, "canonical_album_id": "",
               "canonical_release_mbid": ""}
        with isolated_review(), patch.object(bot, "repair_jobs", {"job1": job}):
            bot._replace_review_groups("library", [], "x")
            first = list(bot._review_state["groups"])
            self.assertEqual([g["id"] for g in first], ["rg1"])
            self.assertEqual(bot._review_group_origin(first[0]), "repair")

            # A second library scan must not add a second row for it.
            bot._replace_review_groups(
                "library", [self._origin_group("lib0", "library", album="L")], "x")
            ids = [g["id"] for g in bot._review_state["groups"]]
            self.assertEqual(sorted(ids), ["lib0", "rg1"])

    def test_a_scan_carries_the_hidden_decision_across(self):
        with isolated_review(), patch.object(bot, "repair_jobs", {}):
            with bot._review_lock:
                bot._review_state["groups"] = [
                    self._origin_group("lib0", "library", hidden=True)]
            bot._replace_review_groups(
                "library", [self._origin_group("lib0", "library")], "x")
            self.assertTrue(bot._review_state["groups"][0]["hidden"])

    def test_a_library_scan_keeps_stored_searches(self):
        """scan-all used to rebuild the state from _empty_review_state() and
        re-attach tasks/operations by hand — `searches` was not on that list."""
        with isolated_review(), patch.object(bot, "repair_jobs", {}):
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
        with isolated_review(), patch.object(bot, "repair_jobs", {}):
            bot._replace_review_groups(
                "library", [self._origin_group("lib0", "library", album="Kept")], "x")
            bot._find_review_group("lib0")["hidden"] = True
            bot._save_review_state(urgent=True)

            bot._index_conn.close()
            bot._index_conn = None
            with bot._review_lock:
                bot._review_state = bot._empty_review_state()
            bot._load_review_state()

            groups = bot._review_state["groups"]
            self.assertEqual([g["album"] for g in groups], ["Kept"])
            self.assertTrue(groups[0]["hidden"])
            self.assertEqual(groups[0]["origin"], "library")

    def test_a_group_dropped_from_the_review_loses_its_row(self):
        with isolated_review(), patch.object(bot, "repair_jobs", {}):
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
        with isolated_review(), patch.object(bot, "repair_jobs", {}):
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
        with isolated_review(), patch.object(bot, "repair_jobs", {}):
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
            bot._index_conn.close(); bot._index_conn = None
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
        with isolated_review(), patch.object(bot, "repair_jobs", {}):
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
                # repair_jobs is module-global; isolate from other tests'
                # leftovers (active jobs get appended by _merge_review_groups).
                with patch.object(bot, "repair_jobs", {}):
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

    def test_union_leaves_a_group_with_an_active_repair_job_alone(self):
        """A union for one artist must not rebuild OTHER groups from their repair jobs.

        _merge_review_groups appends a projection of every active repair job whose
        group the scan did not cover — right for a full replace, where that group
        would otherwise vanish, and wrong for a union, which keeps uncovered groups
        as they are. The union swapped the projection in over the live group, so a
        source search (which opens a repair job) lost its results within seconds of
        finishing whenever the auto-index worker unioned another artist
        (beabadoobee / Loveworm, 2026-09-24).
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
        job = {"id": "job-G", "group_id": "G", "status": "needs_source",
               "artist": "Band", "album": "Record", "tracks": []}
        try:
            with tempfile.TemporaryDirectory() as td:
                bot.REVIEW_FILE = os.path.join(td, "review.json")
                with bot._review_lock:
                    bot._review_state = bot._empty_review_state()
                    bot._review_state["groups"] = [live]
                with patch.object(bot, "repair_jobs", {"job-G": job}):
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
            old_file, old_conn = bot.LIBRARY_INDEX_FILE, bot._index_conn
            bot.LIBRARY_INDEX_FILE = os.path.join(td, "index.db")
            bot._index_conn = None
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
                if bot._index_conn is not None:
                    bot._index_conn.close()
                bot._index_conn = old_conn
                bot.LIBRARY_INDEX_FILE = old_file

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

    @patch("listenbrainz_bot._canonical_release_fields")
    @patch("listenbrainz_bot._run_beets_cmd")
    def test_beets_merge_fails_when_beets_skips(self, mock_run, mock_fields):
        mock_fields.return_value = {
            "album": "Album",
            "albumartist": "Artist",
            "mb_albumid": "rel1",
        }
        mock_run.return_value = (False, "/music/Artist/Album (8 items)\nSkipping.")
        ok, output = bot.beets_merge_album_folders(["/music/Artist/Album"], "rel1")
        self.assertFalse(ok)
        self.assertIn("Skipping", output)

    @patch("listenbrainz_bot._canonical_release_fields")
    @patch("listenbrainz_bot._beets_query_for_folder")
    @patch("listenbrainz_bot._run_beets_cmd")
    def test_beets_merge_registers_unmatched_folder_before_modify(
            self, mock_run, mock_query, mock_fields):
        mock_fields.return_value = {
            "album": "Album",
            "albumartist": "Artist",
            "mb_albumid": "rel1",
        }
        mock_query.side_effect = [
            (None, ""),
            (["album:Album", "albumartist:Artist"], "matched"),
        ]
        mock_run.return_value = (True, "ok")
        ok, output = bot.beets_merge_album_folders(["/music/Artist/Album"], "rel1")
        self.assertTrue(ok)
        commands = [" ".join(call.args[0]) for call in mock_run.call_args_list]
        self.assertTrue(any("import" in c and "-A" in c for c in commands))
        self.assertTrue(any("beets_merge_register" in c for c in commands))
        self.assertTrue(any("modify" in c for c in commands))
        modify_cmd = next(c for c in commands if "modify" in c)
        self.assertNotIn(" -a ", f" {modify_cmd} ")
        self.assertIn("album:Album", modify_cmd)
        self.assertIn("import", output)

    @patch("listenbrainz_bot._canonical_release_fields")
    @patch("listenbrainz_bot._beets_ls")
    @patch("listenbrainz_bot._run_beets_cmd")
    def test_beets_merge_finds_imported_album_by_current_mbid_when_path_differs(
            self, mock_run, mock_ls, mock_fields):
        mock_fields.return_value = {
            "album": "Canonical",
            "albumartist": "Artist",
            "mb_albumid": "target-rel",
        }

        def fake_ls(query=""):
            if query == "path:/music/Artist/Old":
                return False, ""
            if query == ["mb_albumid:old-rel"]:
                return True, "Artist - Old - Track"
            return False, ""

        mock_ls.side_effect = fake_ls
        mock_run.return_value = (True, "ok")
        ok, output = bot.beets_merge_album_folders(
            ["/music/Artist/Old"], "target-rel",
            [{"name": "Old", "artist": "Artist", "musicBrainzId": "old-rel"}])
        self.assertTrue(ok)
        commands = [" ".join(call.args[0]) for call in mock_run.call_args_list]
        self.assertTrue(any("modify -y mb_albumid:old-rel" in c for c in commands))
        self.assertIn("mb_albumid:old-rel", output)

    @patch("listenbrainz_bot._canonical_release_fields")
    @patch("listenbrainz_bot._beets_ls")
    @patch("listenbrainz_bot._run_beets_cmd")
    def test_beets_merge_prefers_album_metadata_when_duplicates_share_folder(
            self, mock_run, mock_ls, mock_fields):
        mock_fields.return_value = {
            "album": "Stereotype A",
            "albumartist": "Cibo Matto",
            "mb_albumid": "target-rel",
        }

        def fake_ls(query=""):
            if query in (["mb_albumid:old-rel-a"], ["mb_albumid:old-rel-b"]):
                return True, "Cibo Matto - Stereotype A - Track"
            if query == "path:/music/Cibo Matto/Stereotype A":
                return True, "Cibo Matto - Stereotype A - Already Canonical"
            return False, ""

        mock_ls.side_effect = fake_ls
        mock_run.return_value = (True, "ok")
        ok, output = bot.beets_merge_album_folders(
            [
                "/music/Cibo Matto/Stereotype A",
                "/music/Cibo Matto/Stereotype A",
            ],
            "target-rel",
            [
                {"name": "Stereotype A", "artist": "Cibo Matto",
                 "musicBrainzId": "old-rel-a"},
                {"name": "Stereotype A", "artist": "Cibo Matto",
                 "musicBrainzId": "old-rel-b"},
            ],
        )
        self.assertTrue(ok)
        commands = [" ".join(call.args[0]) for call in mock_run.call_args_list]
        self.assertTrue(any("modify -y mb_albumid:old-rel-a" in c for c in commands))
        self.assertTrue(any("modify -y mb_albumid:old-rel-b" in c for c in commands))
        self.assertFalse(any("modify -y path:/music/Cibo Matto/Stereotype A" in c
                             for c in commands))
        self.assertIn("mb_albumid:old-rel-a", output)
        self.assertIn("mb_albumid:old-rel-b", output)

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

    def test_beets_skipping_structured_result_is_not_imported(self):
        result = bot._beets_result_from_output(
            "/downloads/Artist - Album", True,
            "/downloads/Artist - Album (10 items)\nSkipping.",
            "rel1", True, preview_on_skip=False)
        self.assertTrue(result["skipped"])
        self.assertFalse(result["imported"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["recommended_action"],
                         "retry_forced_import_or_import_as_is")

    @patch("listenbrainz_bot._album_action_markup", return_value=None)
    @patch("listenbrainz_bot._save_review_state")
    @patch("listenbrainz_bot._save_state")
    @patch("listenbrainz_bot._nd_scan_after_import")
    @patch("listenbrainz_bot._tg_send", new_callable=AsyncMock)
    @patch("listenbrainz_bot.beets_import")
    @patch("listenbrainz_bot._beets_import_preview")
    def test_auto_reviewed_album_import_forces_reimport_and_records_skip(
            self, mock_preview, mock_import, mock_send, mock_scan, _save_state,
            _save_review, _markup):
        mock_import.return_value = (True, "/downloads/Artist - Album (10 items)\nSkipping.")
        mock_preview.return_value = (True, "preview says no confident match")
        mock_scan.return_value = True
        old_groups = bot.pending_album_groups.copy()
        old_albums = bot._albums.copy()
        old_uid = bot._uid_to_token.copy()
        try:
            bot.pending_album_groups.clear()
            bot._albums.clear()
            bot._uid_to_token.clear()
            bot.pending_album_groups["ag1"] = {
                "label": "Artist - Album",
                "total": 10,
                "completed": 10,
                "failed": 0,
                "local_dirs": {"/downloads/Artist - Album": 10},
                "token": "tok",
                "chat_id": "chat",
                "release_mbid": "rel1",
                "artist": "Artist",
                "album": "Album",
                "match_mode": "auto",
                "review_group_id": "g1",
            }
            asyncio.run(bot._finalize_group(object(), "ag1"))
            args = mock_import.call_args.args
            self.assertEqual(args[:5], ("/downloads/Artist - Album", "rel1", False, True, True))
            self.assertTrue(args[5])
            self.assertTrue(args[6])
            self.assertFalse(mock_scan.called)
            self.assertEqual(len(bot._albums), 1)
            rec = next(iter(bot._albums.values()))
            self.assertIn(rec["status"], ("needs_review", "needs_match"))
            self.assertEqual(rec["import_state"], "downloaded_not_imported")
            self.assertIn("Skipping", rec["raw_tail"])
        finally:
            bot.pending_album_groups.clear()
            bot.pending_album_groups.update(old_groups)
            bot._albums.clear()
            bot._albums.update(old_albums)
            bot._uid_to_token.clear()
            bot._uid_to_token.update(old_uid)

    def test_manual_match_import_as_is_structured_result_can_import(self):
        with patch("listenbrainz_bot.beets_import",
                   return_value=(True, "Importing /downloads/A -> /music/A")) as mock_import:
            result = bot._beets_import_result(
                "/downloads/A", "", False, True, False, True, True, 900)
        self.assertTrue(result["imported"])
        self.assertTrue(result["ok"])
        self.assertFalse(mock_import.call_args.args[4])
        self.assertTrue(mock_import.call_args.args[6])

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

    def test_beets_base_cmd_merge_profile_writes_duplicate_merge(self):
        cmd = bot._beets_base_cmd("merge")
        cfg = cmd[cmd.index("-c") + 1]
        with open(cfg) as fh:
            text = fh.read()
        self.assertIn("duplicate_action: merge", text)
        self.assertIn("incremental: no", text)

    def test_beets_trusted_profile_forces_only_pinned_candidate_confidence(self):
        cmd = bot._beets_base_cmd("trusted")
        cfg = cmd[cmd.index("-c") + 1]
        with open(cfg) as fh:
            text = fh.read()
        self.assertIn("duplicate_action: merge", text)
        self.assertIn("strong_rec_thresh: 1.0", text)

    @patch("listenbrainz_bot._run_beets_cmd")
    def test_beets_input_preview_is_non_mutating_pretend(self, mock_run):
        mock_run.return_value = (True, "/downloads/A/01.flac")
        bot._beets_import_preview("/downloads/A", "rel1", True, True)
        cmd = mock_run.call_args.args[0]
        self.assertIn("--pretend", cmd)
        self.assertNotIn("-p", cmd)

    @patch("listenbrainz_bot._audio_file_tags")
    @patch("listenbrainz_bot.mbz_release_tracks")
    def test_pinned_validation_maps_each_file_once(self, mock_tracks, mock_tags):
        old_downloads = bot.SLSKD_DOWNLOAD_DIR
        mock_tracks.return_value = [
            {"title": "One", "mbid": "rec1", "position": 1},
            {"title": "Two", "mbid": "rec2", "position": 2},
        ]
        mock_tags.return_value = {"title": "One", "tracknumber": "1"}
        with tempfile.TemporaryDirectory() as td:
            try:
                bot.SLSKD_DOWNLOAD_DIR = td
                path = os.path.join(td, "01 One.flac")
                with open(path, "wb") as fh:
                    fh.write(b"x")
                result = bot._validate_pinned_import_paths(
                    [path], "rel1", expected_tracks=[{"title": "One", "mbid": "rec1", "position": 1}])
                self.assertTrue(result["ok"])
                self.assertEqual(result["mappings"][0]["recording_mbid"], "rec1")
            finally:
                bot.SLSKD_DOWNLOAD_DIR = old_downloads

    @patch("listenbrainz_bot._audio_file_tags", return_value={"title": "One"})
    @patch("listenbrainz_bot.mbz_release_tracks",
           return_value=[{"title": "One", "mbid": "rec1", "position": 1}])
    def test_pinned_validation_rejects_title_only_guess(self, _tracks, _tags):
        old_downloads = bot.SLSKD_DOWNLOAD_DIR
        with tempfile.TemporaryDirectory() as td:
            try:
                bot.SLSKD_DOWNLOAD_DIR = td
                path = os.path.join(td, "mystery.flac")
                with open(path, "wb") as fh:
                    fh.write(b"x")
                result = bot._validate_pinned_import_paths([path], "rel1")
                self.assertFalse(result["ok"])
                self.assertEqual(result["error_code"], "match_validation_failed")
            finally:
                bot.SLSKD_DOWNLOAD_DIR = old_downloads

    def test_diagnostic_profile_disables_all_real_file_mutations(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = os.path.join(td, "diag.yaml")
            bot._diagnostic_profile_config(
                cfg, os.path.join(td, "library.db"), os.path.join(td, "music"), True)
            with open(cfg) as fh:
                text = fh.read()
        self.assertIn("move: no", text)
        self.assertIn("copy: no", text)
        self.assertIn("write: no", text)
        self.assertIn("delete: no", text)
        self.assertIn("plugins: []", text)
        self.assertIn("strong_rec_thresh: 1.0", text)

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

    @patch("listenbrainz_bot.subprocess.run")
    def test_beets_import_merge_duplicates_uses_merge_profile(self, mock_run):
        mock_run.return_value = types.SimpleNamespace(returncode=0, stdout="Imported", stderr="")
        bot.beets_import("/downloads/A", "rel1", False, True, True, True, True, 900)
        cmd = mock_run.call_args.args[0]
        cfg = cmd[cmd.index("-c") + 1]
        with open(cfg) as fh:
            text = fh.read()
        self.assertIn("duplicate_action: merge", text)

    @patch("listenbrainz_bot._beets_ls")
    def test_skip_output_classifies_duplicate_skip_when_existing_album_matches(self, mock_ls):
        mock_ls.return_value = (True, "Artist - Album - One")
        result = bot._beets_result_from_output(
            "/downloads/Artist - Album", True, "Skipping.",
            "rel1", True, preview_on_skip=False, merge_duplicates=True,
            artist="Artist", album="Album")
        self.assertEqual(result["skipped_reason"], "duplicate_skip")
        self.assertEqual(result["duplicate_mode"], "merge")
        self.assertTrue(result["merge_attempted"])

    @patch("listenbrainz_bot._beets_ls")
    def test_existing_album_queries_order(self, _mock_ls):
        self.assertEqual(
            bot._beets_existing_album_queries("rel1", "Artist", "Album"),
            [["mb_albumid:rel1"],
             ["album:Album", "albumartist:Artist"],
             ["album:Album", "artist:Artist"]])

    def test_import_result_exposes_merge_fields(self):
        result = bot._beets_result_from_output(
            "/downloads/A", True, "Importing /downloads/A -> /music/A",
            "", True, preview_on_skip=False, merge_duplicates=True)
        self.assertEqual(result["duplicate_mode"], "merge")
        self.assertTrue(result["merge_attempted"])
        self.assertIn("skipped_reason", result)

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

    @patch("listenbrainz_bot._trusted_pinned_merge")
    def test_selected_import_uses_only_checked_files_and_rejects_escape(self, mock_import):
        mock_import.return_value = {"ok": True, "imported": True, "raw_tail": "Imported"}
        with tempfile.TemporaryDirectory() as td:
            album = os.path.join(td, "Album")
            os.mkdir(album)
            one = os.path.join(album, "01 One.flac")
            two = os.path.join(album, "02 Two.flac")
            with open(one, "wb") as fh:
                fh.write(b"x")
            with open(two, "wb") as fh:
                fh.write(b"x")
            result = bot._beets_import_selected_result(
                album, ["01 One.flac"], "rel1", True, True, 900,
                "Artist", "Album")
            self.assertTrue(result["imported"])
            self.assertEqual(mock_import.call_args.args[0], [one])
            self.assertTrue(mock_import.call_args.kwargs["allow_partial"])
            with self.assertRaises(ValueError):
                bot._selected_paths_under_album(album, ["../escape.flac"])

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

    def test_create_repair_job_from_review_group_and_projection(self):
        old_jobs = bot.repair_jobs.copy()
        try:
            bot.repair_jobs.clear()
            group = {
                "id": "g1",
                "artist": "Artist",
                "album": "Album",
                "canonical_album_id": "alb1",
                "canonical_mbid": "rel1",
                "albums": [{"id": "alb1", "artist": "Artist", "name": "Album",
                            "musicBrainzId": "rel1", "tracks": []}],
                "missing_tracks": [{
                    "artist": "Artist", "title": "One", "mbid": "rec1",
                    "position": 1, "decision": "approved",
                }],
            }
            job = bot._create_or_update_repair_job_from_group(group)
            self.assertEqual(job["group_id"], "g1")
            self.assertEqual(job["canonical_release_mbid"], "rel1")
            self.assertEqual(job["tracks"][0]["status"], "approved")
            group["missing_tracks"][0]["decision"] = "pending"
            bot._apply_repair_job_projection_to_group(group)
            self.assertEqual(group["missing_tracks"][0]["decision"], "approved")
            self.assertEqual(group["repair_job_id"], job["id"])
        finally:
            bot.repair_jobs.clear()
            bot.repair_jobs.update(old_jobs)

    def test_repair_job_survives_review_group_rebuild(self):
        old_jobs = bot.repair_jobs.copy()
        try:
            bot.repair_jobs.clear()
            job = {
                "id": "jobx",
                "group_id": "missing-group",
                "artist": "Artist",
                "album": "Album",
                "canonical_album_id": "alb1",
                "canonical_release_mbid": "rel1",
                "canonical_release_group_mbid": "",
                "canonical_tracklist": [],
                "status": "downloaded_unmatched",
                "tracks": [{"id": "t1", "group_track_index": 0,
                            "recording_mbid": "rec1", "artist": "Artist",
                            "title": "One", "position": 1,
                            "status": "downloaded",
                            "local_path": "/downloads/A/01 One.flac"}],
                "downloads": [],
                "source_pools": [],
                "file_matches": [],
                "import_attempts": [],
                "verification": {},
                "messages": [],
                "created_at": 1,
                "updated_at": 2,
            }
            bot.repair_jobs[job["id"]] = job
            merged = bot._merge_review_groups([])
            self.assertEqual(len(merged), 1)
            self.assertEqual(merged[0]["id"], "missing-group")
            self.assertEqual(merged[0]["missing_tracks"][0]["decision"], "downloaded")
        finally:
            bot.repair_jobs.clear()
            bot.repair_jobs.update(old_jobs)

    def test_repair_job_save_load_round_trip(self):
        old_state = bot.STATE_FILE
        old_jobs = bot.repair_jobs.copy()
        with tempfile.TemporaryDirectory() as td:
            try:
                bot.STATE_FILE = os.path.join(td, "state.json")
                bot.repair_jobs.clear()
                bot.repair_jobs["job1"] = {
                    "id": "job1", "group_id": "g1", "artist": "A",
                    "album": "B", "status": "needs_review", "tracks": [],
                    "downloads": [], "source_pools": [], "file_matches": [],
                    "import_attempts": [], "verification": {}, "messages": [],
                    "created_at": 1, "updated_at": 1,
                }
                bot._save_state()
                bot.repair_jobs.clear()
                bot._load_state()
                self.assertIn("job1", bot.repair_jobs)
            finally:
                bot.STATE_FILE = old_state
                bot.repair_jobs.clear()
                bot.repair_jobs.update(old_jobs)

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
                    "filename": "@@x\Music\Album\01 Song.flac"}]
        with patch.object(bot._http, "delete",
                          lambda url, **k: calls.append(url) or _Resp()),                 patch.object(bot, "_slskd_fetch_all_downloads", lambda: listing):
            # Enqueued under a different path prefix: matched by basename.
            self.assertTrue(bot._slskd_cancel("peer", "Album\01 Song.flac"))
            self.assertTrue(calls[-1].endswith("/downloads/peer/guid-1"))
            # A known id skips the lookup entirely.
            self.assertTrue(bot._slskd_cancel("peer", "whatever", "guid-9", []))
            self.assertTrue(calls[-1].endswith("/downloads/peer/guid-9"))
            # Nothing to address: reported, not pretended.
            self.assertFalse(bot._slskd_cancel("other", "Album\01 Song.flac"))
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
        old_path, old_conn = bot.LIBRARY_INDEX_FILE, bot._index_conn
        bot.LIBRARY_INDEX_FILE = os.path.join(td, "index.db")
        bot._index_conn = None

        def restore():
            try:
                if bot._index_conn is not None:
                    bot._index_conn.close()
            except Exception:
                pass
            bot.LIBRARY_INDEX_FILE, bot._index_conn = old_path, old_conn

        self.addCleanup(restore)

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

    def test_normalized_download_error_timeout_and_cancel_states(self):
        old_jobs = bot.repair_jobs.copy()
        try:
            bot.repair_jobs.clear()
            job = {"id": "job1", "group_id": "g1", "artist": "A",
                   "album": "B", "status": "needs_source",
                   "tracks": [{"id": "t1", "title": "One",
                               "recording_mbid": "rec1", "status": "approved"}],
                   "downloads": [], "source_pools": [], "file_matches": [],
                   "import_attempts": [], "verification": {}, "messages": [],
                   "created_at": 1, "updated_at": 1}
            bot.repair_jobs["job1"] = job
            bot._repair_record_download_queued(
                "job1", "t1", "user", {"filename": "A/01 One.flac"})
            bot._repair_update_download(
                "job1", "user", "A/01 One.flac", "timeout",
                "transfer timed out", "transfer_timeout")
            self.assertEqual(job["tracks"][0]["status"], "download_timeout")
            self.assertEqual(job["status"], "blocked_slskd_timeout")
            self.assertTrue(job["downloads"][0]["retry_available"])
            bot._repair_update_download(
                "job1", "user", "A/01 One.flac", "cancelled",
                "cancelled by user")
            self.assertEqual(job["tracks"][0]["status"], "cancelled")
            bot._repair_update_download(
                "job1", "user", "A/01 One.flac", "error",
                "slskd rejected")
            self.assertEqual(job["tracks"][0]["status"], "download_error")
            self.assertEqual(job["status"], "blocked_slskd_error")
        finally:
            bot.repair_jobs.clear()
            bot.repair_jobs.update(old_jobs)

    def test_match_downloaded_files_to_job_by_track_number_and_title(self):
        old_jobs = bot.repair_jobs.copy()
        with tempfile.TemporaryDirectory() as td:
            try:
                bot.repair_jobs.clear()
                path = os.path.join(td, "01 One.flac")
                with open(path, "wb") as fh:
                    fh.write(b"x")
                job = {"id": "job1", "group_id": "g1", "artist": "Artist",
                       "album": "Album", "status": "downloaded_unmatched",
                       "tracks": [{"id": "t1", "title": "One",
                                   "artist": "Artist", "position": 1,
                                   "recording_mbid": "rec1",
                                   "status": "downloaded"}],
                       "downloads": [], "source_pools": [{
                           "id": "pool1", "path": td, "status": "downloaded"}],
                       "file_matches": [], "import_attempts": [],
                       "verification": {}, "messages": [],
                       "created_at": 1, "updated_at": 1}
                bot.repair_jobs["job1"] = job
                result = bot.match_downloaded_files_to_job("job1")
                self.assertTrue(result["ok"])
                self.assertEqual(job["status"], "matched_ready_to_import")
                self.assertEqual(job["tracks"][0]["status"], "file_matched")
                self.assertEqual(job["file_matches"][0]["source_path"], path)
            finally:
                bot.repair_jobs.clear()
                bot.repair_jobs.update(old_jobs)

    @patch("listenbrainz_bot._audio_file_tags")
    def test_match_downloaded_files_to_job_by_mbid_and_ambiguous(self, mock_tags):
        old_jobs = bot.repair_jobs.copy()
        with tempfile.TemporaryDirectory() as td:
            try:
                bot.repair_jobs.clear()
                one = os.path.join(td, "x.flac")
                two = os.path.join(td, "y.flac")
                for p in (one, two):
                    with open(p, "wb") as fh:
                        fh.write(b"x")
                mock_tags.return_value = {"musicbrainz_trackid": "rec1"}
                job = {"id": "job1", "group_id": "g1", "artist": "Artist",
                       "album": "Album", "status": "downloaded_unmatched",
                       "tracks": [{"id": "t1", "title": "One",
                                   "artist": "Artist", "position": 1,
                                   "recording_mbid": "rec1",
                                   "status": "downloaded"}],
                       "downloads": [], "source_pools": [{
                           "id": "pool1", "path": td, "status": "downloaded"}],
                       "file_matches": [], "import_attempts": [],
                       "verification": {}, "messages": [],
                       "created_at": 1, "updated_at": 1}
                bot.repair_jobs["job1"] = job
                result = bot.match_downloaded_files_to_job("job1")
                self.assertFalse(result["ok"])
                self.assertEqual(result["ambiguous"], 1)
                self.assertEqual(job["status"], "blocked_ambiguous_files")
                self.assertEqual(job["tracks"][0]["status"], "match_ambiguous")
            finally:
                bot.repair_jobs.clear()
                bot.repair_jobs.update(old_jobs)

    def test_match_downloaded_files_ignores_unapproved_missing_tracks(self):
        old_jobs = bot.repair_jobs.copy()
        with tempfile.TemporaryDirectory() as td:
            try:
                bot.repair_jobs.clear()
                path = os.path.join(td, "01 One.flac")
                with open(path, "wb") as fh:
                    fh.write(b"x")
                job = {"id": "job1", "group_id": "g1", "artist": "Artist",
                       "album": "Album", "status": "needs_review",
                       "tracks": [{"id": "t1", "title": "One",
                                   "artist": "Artist", "position": 1,
                                   "recording_mbid": "rec1",
                                   "status": "missing"}],
                       "downloads": [], "source_pools": [{
                           "id": "pool1", "path": td, "status": "downloaded"}],
                       "file_matches": [], "import_attempts": [],
                       "verification": {}, "messages": [],
                       "created_at": 1, "updated_at": 1}
                bot.repair_jobs["job1"] = job
                result = bot.match_downloaded_files_to_job("job1")
                self.assertTrue(result["ok"])
                self.assertEqual(result["matched"], 0)
                self.assertEqual(job["tracks"][0]["status"], "missing")
            finally:
                bot.repair_jobs.clear()
                bot.repair_jobs.update(old_jobs)

    def test_manual_repair_file_match_assigns_visible_download(self):
        old_jobs = bot.repair_jobs.copy()
        old_downloads = bot.SLSKD_DOWNLOAD_DIR
        with tempfile.TemporaryDirectory() as td:
            try:
                downloads = os.path.join(td, "downloads")
                os.mkdir(downloads)
                path = os.path.join(downloads, "01 One.flac")
                with open(path, "wb") as fh:
                    fh.write(b"x")
                bot.SLSKD_DOWNLOAD_DIR = downloads
                bot.repair_jobs.clear()
                job = {"id": "job1", "group_id": "g1", "artist": "Artist",
                       "album": "Album", "status": "blocked_no_match",
                       "tracks": [{"id": "t1", "title": "One",
                                   "artist": "Artist", "position": 1,
                                   "recording_mbid": "rec1",
                                   "status": "match_missing"}],
                       "downloads": [], "source_pools": [{
                           "id": "pool1", "path": downloads, "status": "downloaded"}],
                       "file_matches": [], "import_attempts": [],
                       "verification": {}, "messages": [],
                       "created_at": 1, "updated_at": 1}
                bot.repair_jobs["job1"] = job
                candidates = bot.repair_job_candidate_files("job1")
                self.assertEqual(candidates["files"][0]["path"], path)
                result = bot.manually_match_repair_job_files(
                    "job1", [{"track_id": "t1", "source_path": path}])
                self.assertTrue(result["ok"])
                self.assertEqual(job["tracks"][0]["status"], "file_matched")
                self.assertEqual(job["file_matches"][0]["confidence"], "manual")
                self.assertEqual(job["status"], "matched_ready_to_import")
            finally:
                bot.repair_jobs.clear()
                bot.repair_jobs.update(old_jobs)
                bot.SLSKD_DOWNLOAD_DIR = old_downloads

    def test_manual_repair_file_match_rejects_hidden_path(self):
        old_jobs = bot.repair_jobs.copy()
        old_downloads = bot.SLSKD_DOWNLOAD_DIR
        with tempfile.TemporaryDirectory() as td:
            try:
                downloads = os.path.join(td, "downloads")
                outside = os.path.join(td, "outside")
                os.mkdir(downloads)
                os.mkdir(outside)
                hidden = os.path.join(outside, "01 One.flac")
                with open(hidden, "wb") as fh:
                    fh.write(b"x")
                bot.SLSKD_DOWNLOAD_DIR = downloads
                bot.repair_jobs.clear()
                bot.repair_jobs["job1"] = {
                    "id": "job1", "group_id": "g1", "artist": "Artist",
                    "album": "Album", "status": "blocked_no_match",
                    "tracks": [{"id": "t1", "title": "One", "position": 1,
                                "recording_mbid": "rec1", "status": "match_missing"}],
                    "downloads": [], "source_pools": [{
                        "id": "pool1", "path": downloads, "status": "downloaded"}],
                    "file_matches": [], "import_attempts": [],
                    "verification": {}, "messages": [],
                    "created_at": 1, "updated_at": 1}
                result = bot.manually_match_repair_job_files(
                    "job1", [{"track_id": "t1", "source_path": hidden}])
                self.assertFalse(result["ok"])
                self.assertEqual(bot.repair_jobs["job1"]["tracks"][0]["status"],
                                 "match_missing")
            finally:
                bot.repair_jobs.clear()
                bot.repair_jobs.update(old_jobs)
                bot.SLSKD_DOWNLOAD_DIR = old_downloads

    def test_stage_matched_files_copies_only_safe_download_sources(self):
        old_jobs = bot.repair_jobs.copy()
        old_downloads = bot.SLSKD_DOWNLOAD_DIR
        old_staging = bot.LB_BOT_STAGING_DIR
        with tempfile.TemporaryDirectory() as td:
            try:
                downloads = os.path.join(td, "downloads")
                staging = os.path.join(td, "staging")
                os.mkdir(downloads)
                src = os.path.join(downloads, "01 One.flac")
                with open(src, "wb") as fh:
                    fh.write(b"x")
                bot.SLSKD_DOWNLOAD_DIR = downloads
                bot.LB_BOT_STAGING_DIR = staging
                bot.repair_jobs.clear()
                job = {"id": "job1", "group_id": "g1", "artist": "Artist",
                       "album": "Album", "status": "matched_ready_to_import",
                       "tracks": [{"id": "t1", "title": "One",
                                   "artist": "Artist", "position": 1,
                                   "recording_mbid": "rec1",
                                   "status": "file_matched"}],
                       "downloads": [], "source_pools": [], "import_attempts": [],
                       "verification": {}, "messages": [],
                       "file_matches": [{"track_id": "t1", "recording_mbid": "rec1",
                                         "source_path": src, "confidence": "high",
                                         "status": "matched", "reason": "test"}],
                       "created_at": 1, "updated_at": 1}
                bot.repair_jobs["job1"] = job
                result = bot.stage_matched_files("job1")
                self.assertTrue(result["ok"])
                staged = result["staged"][0]["staged_path"]
                self.assertTrue(os.path.exists(src))
                self.assertTrue(os.path.exists(staged))
                self.assertTrue(bot._path_inside(staged, staging))
                self.assertEqual(job["tracks"][0]["status"], "staged")
            finally:
                bot.repair_jobs.clear()
                bot.repair_jobs.update(old_jobs)
                bot.SLSKD_DOWNLOAD_DIR = old_downloads
                bot.LB_BOT_STAGING_DIR = old_staging

    def test_stage_matched_files_blocks_source_outside_downloads(self):
        old_jobs = bot.repair_jobs.copy()
        old_downloads = bot.SLSKD_DOWNLOAD_DIR
        old_staging = bot.LB_BOT_STAGING_DIR
        with tempfile.TemporaryDirectory() as td:
            try:
                downloads = os.path.join(td, "downloads")
                staging = os.path.join(td, "staging")
                outside = os.path.join(td, "outside")
                os.mkdir(downloads)
                os.mkdir(outside)
                src = os.path.join(outside, "01 One.flac")
                with open(src, "wb") as fh:
                    fh.write(b"x")
                bot.SLSKD_DOWNLOAD_DIR = downloads
                bot.LB_BOT_STAGING_DIR = staging
                bot.repair_jobs.clear()
                bot.repair_jobs["job1"] = {
                    "id": "job1", "group_id": "g1", "artist": "Artist",
                    "album": "Album", "status": "matched_ready_to_import",
                    "tracks": [{"id": "t1", "title": "One", "position": 1,
                                "recording_mbid": "rec1", "status": "file_matched"}],
                    "downloads": [], "source_pools": [], "import_attempts": [],
                    "verification": {}, "messages": [],
                    "file_matches": [{"track_id": "t1", "source_path": src,
                                      "status": "matched"}],
                    "created_at": 1, "updated_at": 1}
                result = bot.stage_matched_files("job1")
                self.assertFalse(result["ok"])
                self.assertEqual(bot.repair_jobs["job1"]["status"], "blocked_permission")
            finally:
                bot.repair_jobs.clear()
                bot.repair_jobs.update(old_jobs)
                bot.SLSKD_DOWNLOAD_DIR = old_downloads
                bot.LB_BOT_STAGING_DIR = old_staging

    def test_defer_unresolved_allows_matched_subset_and_resume(self):
        old_jobs = bot.repair_jobs.copy()
        try:
            bot.repair_jobs.clear()
            bot.repair_jobs["job1"] = {
                "id": "job1", "status": "blocked_slskd_error",
                "tracks": [
                    {"id": "t1", "title": "Ready", "status": "file_matched"},
                    {"id": "t2", "title": "Missing", "status": "download_error",
                     "error": "rejected"},
                ],
                "file_matches": [{"track_id": "t1", "status": "matched",
                                  "source_path": "/downloads/01.flac"}],
                "messages": [],
            }
            result = bot.defer_unresolved_repair_tracks("job1")
            self.assertTrue(result["ok"])
            self.assertEqual(bot.repair_jobs["job1"]["tracks"][1]["status"], "deferred")
            self.assertEqual(bot.repair_jobs["job1"]["status"], "matched_ready_to_import")
            resumed = bot.resume_deferred_repair_tracks("job1")
            self.assertTrue(resumed["ok"])
            self.assertEqual(bot.repair_jobs["job1"]["tracks"][1]["status"], "missing")
        finally:
            bot.repair_jobs.clear()
            bot.repair_jobs.update(old_jobs)

    @patch("listenbrainz_bot._verify_trusted_beets_import")
    @patch("listenbrainz_bot.mbz_release_tracks")
    @patch("listenbrainz_bot._run_beets_cmd")
    def test_repair_import_uses_staged_folder_and_canonical_metadata(
            self, mock_run, mock_tracks, mock_verify):
        old_jobs = bot.repair_jobs.copy()
        old_staging = bot.LB_BOT_STAGING_DIR
        mock_run.return_value = (True, "ok")
        mock_tracks.return_value = [{"title": "One", "mbid": "rec1", "position": 1}]
        mock_verify.return_value = {"ok": True, "recordings": []}
        with tempfile.TemporaryDirectory() as td:
            try:
                bot.LB_BOT_STAGING_DIR = os.path.join(td, "staging")
                bot.repair_jobs.clear()
                staged_dir = os.path.join(bot.LB_BOT_STAGING_DIR, "job1")
                os.makedirs(staged_dir)
                staged = os.path.join(staged_dir, "01 One.flac")
                with open(staged, "wb") as fh:
                    fh.write(b"x")
                bot.repair_jobs["job1"] = {
                    "id": "job1", "group_id": "g1", "artist": "Artist",
                    "album": "Album", "canonical_release_mbid": "rel1",
                    "canonical_release_group_mbid": "",
                    "canonical_tracklist": [{"title": "One"}],
                    "status": "matched_ready_to_import",
                    "staging_dir": staged_dir,
                    "tracks": [{"id": "t1", "title": "One", "artist": "Artist",
                                "position": 1, "recording_mbid": "rec1",
                                "status": "staged"}],
                    "downloads": [], "source_pools": [], "import_attempts": [],
                    "verification": {}, "messages": [],
                    "file_matches": [{"track_id": "t1", "source_path": "/downloads/A/01.flac",
                                      "staged_path": staged, "status": "matched"}],
                    "created_at": 1, "updated_at": 1}
                result = bot.repair_import_matched_tracks("job1")
                self.assertTrue(result["ok"])
                calls = [c.args[0] for c in mock_run.call_args_list]
                self.assertEqual(len(calls), 1)
                self.assertIn("import", calls[0])
                self.assertIn("--flat", calls[0])
                self.assertIn("--search-id", calls[0])
                self.assertIn("rel1", calls[0])
                self.assertEqual(calls[0][-1], staged)
                self.assertTrue(all("/downloads/A" not in " ".join(cmd) for cmd in calls))
                self.assertEqual(result["attempt"]["commands"][0]["kind"], "merge_import")
                self.assertEqual(bot.repair_jobs["job1"]["tracks"][0]["status"],
                                 "navidrome_pending")
            finally:
                bot.repair_jobs.clear()
                bot.repair_jobs.update(old_jobs)
                bot.LB_BOT_STAGING_DIR = old_staging

    @patch("listenbrainz_bot.mbz_release_tracks")
    @patch("listenbrainz_bot._run_beets_cmd")
    def test_repair_import_treats_beets_skipping_as_failure(self, mock_run, mock_tracks):
        old_jobs = bot.repair_jobs.copy()
        old_staging = bot.LB_BOT_STAGING_DIR
        mock_run.return_value = (True, "Skipping.")
        mock_tracks.return_value = [{"title": "One", "mbid": "rec1", "position": 1}]
        with tempfile.TemporaryDirectory() as td:
            try:
                bot.LB_BOT_STAGING_DIR = os.path.join(td, "staging")
                bot.repair_jobs.clear()
                staged_dir = os.path.join(bot.LB_BOT_STAGING_DIR, "job1")
                os.makedirs(staged_dir)
                staged = os.path.join(staged_dir, "01 One.flac")
                with open(staged, "wb") as fh:
                    fh.write(b"x")
                bot.repair_jobs["job1"] = {
                    "id": "job1", "group_id": "g1", "artist": "Artist",
                    "album": "Album", "canonical_release_mbid": "rel1",
                    "canonical_tracklist": [], "status": "matched_ready_to_import",
                    "staging_dir": staged_dir,
                    "tracks": [{"id": "t1", "title": "One", "position": 1,
                                "recording_mbid": "rec1", "status": "staged"}],
                    "downloads": [], "source_pools": [], "import_attempts": [],
                    "verification": {}, "messages": [],
                    "file_matches": [{"track_id": "t1", "staged_path": staged,
                                      "status": "matched"}],
                    "created_at": 1, "updated_at": 1}
                result = bot.repair_import_matched_tracks("job1")
                self.assertFalse(result["ok"])
                self.assertEqual(bot.repair_jobs["job1"]["status"], "blocked_beets_error")
                self.assertEqual(result["attempt"]["error_code"], "beets_skipped")
            finally:
                bot.repair_jobs.clear()
                bot.repair_jobs.update(old_jobs)
                bot.LB_BOT_STAGING_DIR = old_staging

    def test_selected_file_import_ignores_unselected_download_leftovers(self):
        old_downloads = bot.SLSKD_DOWNLOAD_DIR
        old_relocates = bot._BEETS_RELOCATES
        old_moves = bot._BEETS_MOVES
        with tempfile.TemporaryDirectory() as td:
            try:
                downloads = os.path.join(td, "downloads")
                os.mkdir(downloads)
                selected = os.path.join(downloads, "01 One.flac")
                leftover = os.path.join(downloads, "02 Two.flac")
                with open(selected, "wb") as fh:
                    fh.write(b"x")
                with open(leftover, "wb") as fh:
                    fh.write(b"x")
                os.remove(selected)
                bot.SLSKD_DOWNLOAD_DIR = downloads
                bot._BEETS_RELOCATES = True
                bot._BEETS_MOVES = True
                result = bot._beets_result_from_output(
                    downloads, True, "imported", "rel1", True, True, True,
                    source_paths=[selected])
                self.assertTrue(result["imported"])
                self.assertFalse(result["still_in_downloads"])
            finally:
                bot.SLSKD_DOWNLOAD_DIR = old_downloads
                bot._BEETS_RELOCATES = old_relocates
                bot._BEETS_MOVES = old_moves

    @patch("listenbrainz_bot._nd_search")
    @patch("listenbrainz_bot.nd_get_scan_status")
    @patch("listenbrainz_bot.nd_start_scan")
    def test_verify_repair_job_requires_navidrome_visibility(
            self, mock_scan, mock_status, mock_search):
        old_jobs = bot.repair_jobs.copy()
        mock_scan.return_value = True
        mock_status.return_value = {"scanning": False}
        mock_search.return_value = [{
            "id": "song1", "musicBrainzId": "rec1", "title": "One",
            "album": "Album", "albumArtist": "Artist"}]
        try:
            bot.repair_jobs.clear()
            bot.repair_jobs["job1"] = {
                "id": "job1", "group_id": "g1", "artist": "Artist",
                "album": "Album", "status": "imported_unverified",
                "tracks": [{"id": "t1", "title": "One", "artist": "Artist",
                            "recording_mbid": "rec1",
                            "status": "navidrome_pending"}],
                "downloads": [], "source_pools": [], "file_matches": [],
                "import_attempts": [], "verification": {}, "messages": [],
                "created_at": 1, "updated_at": 1}
            user = {"navidrome_user": "u", "navidrome_password": "p"}
            result = bot.verify_repair_job_in_navidrome(
                "job1", user=user, poll_attempts=1, poll_interval=0)
            self.assertTrue(result["ok"])
            self.assertEqual(bot.repair_jobs["job1"]["status"], "verified_complete")
            self.assertEqual(bot.repair_jobs["job1"]["tracks"][0]["status"],
                             "navidrome_verified")
        finally:
            bot.repair_jobs.clear()
            bot.repair_jobs.update(old_jobs)

    @patch("listenbrainz_bot._nd_search")
    @patch("listenbrainz_bot.nd_get_scan_status")
    @patch("listenbrainz_bot.nd_start_scan")
    def test_verify_repair_job_marks_deferred_run_partial(
            self, mock_scan, mock_status, mock_search):
        old_jobs = bot.repair_jobs.copy()
        mock_scan.return_value = True
        mock_status.return_value = {"scanning": False}
        mock_search.return_value = [{
            "id": "song1", "musicBrainzId": "rec1", "title": "One",
            "album": "Album", "albumArtist": "Artist"}]
        try:
            bot.repair_jobs.clear()
            bot.repair_jobs["job1"] = {
                "id": "job1", "artist": "Artist", "album": "Album",
                "status": "imported_unverified",
                "tracks": [
                    {"id": "t1", "title": "One", "recording_mbid": "rec1",
                     "status": "navidrome_pending"},
                    {"id": "t2", "title": "Two", "recording_mbid": "rec2",
                     "status": "deferred"},
                ],
                "verification": {}, "messages": [],
            }
            result = bot.verify_repair_job_in_navidrome(
                "job1", user={"navidrome_user": "u", "navidrome_password": "p"},
                poll_attempts=1, poll_interval=0)
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "verified_partial")
            self.assertFalse(bot.repair_jobs["job1"]["verification"]["complete"])
            self.assertTrue(bot.repair_jobs["job1"]["verification"]["subset_complete"])
        finally:
            bot.repair_jobs.clear()
            bot.repair_jobs.update(old_jobs)

    @patch("listenbrainz_bot.nd_start_scan")
    def test_verify_repair_job_does_not_scan_before_moved_files(self, mock_scan):
        old_jobs = bot.repair_jobs.copy()
        try:
            bot.repair_jobs.clear()
            bot.repair_jobs["job1"] = {
                "id": "job1", "group_id": "g1", "artist": "Artist",
                "album": "Album", "status": "matched_ready_to_import",
                "tracks": [{"id": "t1", "title": "One",
                            "recording_mbid": "rec1", "status": "staged"}],
                "downloads": [], "source_pools": [], "file_matches": [],
                "import_attempts": [], "verification": {}, "messages": [],
                "created_at": 1, "updated_at": 1}
            user = {"navidrome_user": "u", "navidrome_password": "p"}
            result = bot.verify_repair_job_in_navidrome(
                "job1", user=user, poll_attempts=0, poll_interval=0)
            self.assertFalse(result["ok"])
            self.assertFalse(mock_scan.called)
        finally:
            bot.repair_jobs.clear()
            bot.repair_jobs.update(old_jobs)

    def test_operation_create_finish_and_payload(self):
        old_review = bot._review_state
        try:
            bot._review_state = bot._empty_review_state()
            op = bot._operation_create("match_files", "Matching", "job1")
            self.assertEqual(op["status"], "running")
            done = bot._operation_finish(op["id"], True, "Matched")
            self.assertEqual(done["status"], "success")
            payload = bot._with_operation({"ok": True}, op)
            self.assertEqual(payload["operation_id"], op["id"])
            self.assertEqual(payload["operation"]["status"], "success")
            self.assertEqual(payload["job_id"], "job1")
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
            with patch.object(bot, "repair_jobs", {}):
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
        with patch.object(bot, "_repair_job_for_group", lambda gid: None), \
             patch.object(bot, "_pop_album_groups_for_review_group", lambda gid: None):
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
             patch.object(bot, "_repair_job_for_group", lambda gid: None), \
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
        with patch.object(bot, "LB_BOT_REPAIR_JOBS", False):
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
        old_path, old_conn = bot.LIBRARY_INDEX_FILE, bot._index_conn
        bot.LIBRARY_INDEX_FILE = self.db_path
        bot._index_conn = None

        def restore():
            try:
                if bot._index_conn is not None:
                    bot._index_conn.close()
            except Exception:
                pass
            bot.LIBRARY_INDEX_FILE, bot._index_conn = old_path, old_conn

        self.addCleanup(restore)
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
        bot._index_conn = None

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
        old_path, old_conn = bot.LIBRARY_INDEX_FILE, bot._index_conn
        bot.LIBRARY_INDEX_FILE = self.db_path
        bot._index_conn = None

        def restore():
            try:
                if bot._index_conn is not None:
                    bot._index_conn.close()
            except Exception:
                pass
            bot.LIBRARY_INDEX_FILE, bot._index_conn = old_path, old_conn

        self.addCleanup(restore)

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
        bot._index_conn.close()
        bot._index_conn = None

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
        bot._index_conn = None

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
        old_path, old_conn = bot.LIBRARY_INDEX_FILE, bot._index_conn
        bot.LIBRARY_INDEX_FILE = self.db_path
        bot._index_conn = None

        def restore():
            try:
                if bot._index_conn is not None:
                    bot._index_conn.close()
            except Exception:
                pass
            bot.LIBRARY_INDEX_FILE, bot._index_conn = old_path, old_conn

        self.addCleanup(restore)

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
        old_path, old_conn = bot.LIBRARY_INDEX_FILE, bot._index_conn
        bot.LIBRARY_INDEX_FILE = self.db_path
        bot._index_conn = None

        def restore():
            try:
                if bot._index_conn is not None:
                    bot._index_conn.close()
            except Exception:
                pass
            bot.LIBRARY_INDEX_FILE, bot._index_conn = old_path, old_conn

        self.addCleanup(restore)

    def test_unwritable_hwm_path_at_boot_still_yields_a_working_index_db(self):
        bot._index_store_artist(
            {"artist_mbid": "mb-a", "artist_name": "A",
             "releases": [{"rgid": "rg1", "title": "T", "status": "missing"}]}, "")
        seq, epoch = bot._index_head()
        # Force the boot-time epoch rotation to fire on the next open: a
        # persisted HWM strictly above the current head, under the same epoch.
        bot._index_persist_hwm(seq + 5, epoch)
        bot._index_conn.close()
        bot._index_conn = None
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
        old_path = bot.LIBRARY_INDEX_FILE
        bot.LIBRARY_INDEX_FILE = os.path.join(td, "index.db")
        self.addCleanup(lambda: setattr(bot, "LIBRARY_INDEX_FILE", old_path))

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
    def _review(self, groups, jobs=None):
        old_file, old_state = bot.REVIEW_FILE, bot._review_snapshot()
        try:
            with tempfile.TemporaryDirectory() as td:
                bot.REVIEW_FILE = os.path.join(td, "review.json")
                with bot._review_lock:
                    bot._review_state = bot._empty_review_state()
                    bot._review_state["groups"] = groups
                with patch.object(bot, "repair_jobs", jobs or {}), \
                        patch.object(bot, "_index_db", side_effect=RuntimeError("no db")):
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

    def test_a_replace_does_not_duplicate_a_group_another_origin_holds(self):
        live = self._g("G", origin="library", canonical_mbid="rel-1",
                       albums=[{"id": "al"}], canonical_album_id="al")
        job = {"id": "job-G", "group_id": "G", "status": "needs_source",
               "canonical_release_mbid": "rel-drifted", "tracks": []}
        with self._review([live], {"job-G": job}), \
                patch.object(bot, "LB_BOT_REPAIR_JOBS", True):
            bot._replace_review_groups("playlist", [], "done")
            ids = [g["id"] for g in bot._review_snapshot()["groups"]]
        self.assertEqual(ids, ["G"])

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


if __name__ == "__main__":
    unittest.main()
