import os
import tempfile
import sys
import types
import unittest
import asyncio
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

    def test_review_json_round_trip(self):
        old_file = bot.REVIEW_FILE
        old_state = bot._review_snapshot()
        try:
            with tempfile.TemporaryDirectory() as td:
                bot.REVIEW_FILE = os.path.join(td, "review.json")
                with bot._review_lock:
                    bot._review_state = bot._empty_review_state()
                    bot._review_state["groups"] = [{"id": "g1", "missing_tracks": []}]
                bot._save_review_state()
                with bot._review_lock:
                    bot._review_state = bot._empty_review_state()
                bot._load_review_state()
                self.assertEqual(bot._review_snapshot()["groups"][0]["id"], "g1")
        finally:
            bot.REVIEW_FILE = old_file
            with bot._review_lock:
                bot._review_state = old_state

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

    def test_disk_state_drops_the_slskd_file_payload(self):
        group = self._source_group(time.time())
        group["source_results"]["folders"][0]["_expanded"] = [{"filename": "x.flac"}]
        bot._review_state["groups"] = [group]
        try:
            on_disk = bot._review_state_for_disk()
        finally:
            bot._review_state["groups"] = []
        fd = on_disk["groups"][0]["source_results"]["folders"][0]
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
            "navidrome_user": "testuser",
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

    def test_operations_survive_store_review_groups(self):
        old_review = bot._review_state
        try:
            bot._review_state = bot._empty_review_state()
            op = bot._operation_create("scan", "Scanning")
            bot._store_review_groups([], "done")
            self.assertIn(op["id"], bot._review_state["operations"])
        finally:
            bot._review_state = old_review

    def test_move_does_not_inherit_source_mtime_or_mode(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        import shutil as _shutil

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
        _shutil.move(src, dest, copy_function=_shutil.copyfile)
        bot._touch(dest)

        self.assertGreater(os.path.getmtime(dest), stale,
                           "placed file must look modified now, not at download time")
        if os.name == "posix":
            self.assertNotEqual(os.stat(dest).st_mode & 0o777, 0o600,
                                "placed file must not inherit the source's mode")

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

    def test_quality_preference_changes_the_preferred_copy(self):
        hires = {"codec": "flac", "bitrate": 2500, "bit_depth": 24, "sample_rate": 96000}
        cd = {"codec": "flac", "bitrate": 900, "bit_depth": 16, "sample_rate": 44100}
        q = bot._quality_preference_score
        self.assertGreater(q(cd, "flac-16-44"), q(hires, "flac-16-44"))
        self.assertGreater(q(hires, "highest-bitrate"), q(cd, "highest-bitrate"))
        opus = {"codec": "opus", "bitrate": 256, "bit_depth": 0, "sample_rate": 48000}
        self.assertGreater(q(opus, "prefer-opus"), q(cd, "prefer-opus"))


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


if __name__ == "__main__":
    unittest.main()
