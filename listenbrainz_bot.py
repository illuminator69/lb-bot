"""
ListenBrainz -> slskd Telegram bot

Features:
- Runs automatically every Tuesday
- Multi-user support
- MBID-first Navidrome lookup, text-search fallback
- Album-first download: groups missing tracks by MusicBrainz release,
  searches for full album when enough tracks are missing
- slskd format filtering (FLAC/Opus), live/compilation scoring, upload-speed ranking
- Approval prompts for live-only and low-quality results
- Retry button for not-found and error tracks
- Download completion polling: auto-retry on failure, beets import on success
- Duplicate detection before downloading
- Cross-playlist deduplication
- /search: manual slskd search with per-file and per-album download buttons
- /checkalbums: find incomplete albums in Navidrome library via MusicBrainz
"""

import re
import os
import json
import time
import hashlib
import datetime
import requests
import asyncio
import sys
import threading
import uuid
import difflib
import shutil
import sqlite3
import tempfile
import unicodedata
import urllib.parse
import contextlib
import contextvars
import concurrent.futures
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    MessageHandler, ContextTypes, filters,
)

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

NAVIDROME_URL = os.environ.get("NAVIDROME_URL", "http://navidrome:4533")
SLSKD_URL     = os.environ.get("SLSKD_URL", "http://slskd:5030")
SLSKD_API_KEY = os.environ.get("SLSKD_API_KEY", "")        # Options > API Keys in the slskd UI

# MusicBrainz — contact email required by their API ToS
MBZ_CONTACT   = os.environ.get("MBZ_CONTACT", "")  # required by MusicBrainz API ToS

# Spotify — Client Credentials flow (no user login needed for public playlists)
# Create an app at https://developer.spotify.com/dashboard
SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

SLSKD_DOWNLOAD_DIR = os.environ.get("SLSKD_DOWNLOAD_DIR", "/downloads")  # path inside the container where slskd saves files

# slskd search settings
# Must outlast slskd's own straggler timeout, because results are unreadable
# until the search completes: measured at ~40s on a live instance, and cutting
# the poll off at 30s was the difference between 142 sources and none.
SEARCH_TIMEOUT  = int(os.environ.get("SLSKD_SEARCH_TIMEOUT", "75"))    # hard deadline for a search
SEARCH_POLL_INT = float(os.environ.get("SLSKD_SEARCH_POLL_INT", "1"))  # seconds between state polls
# Early exit. Waiting for slskd to declare a search "Completed" means waiting
# for the slowest peer that will ever answer — usually the full SEARCH_TIMEOUT,
# long after every source worth ranking has replied. Once MIN_RESPONSES peers
# have answered and MIN_WAIT has elapsed, stop. The wait floor matters:
# _score_folder ranks a source against its peers, and the first responder is
# often a slow one, so exiting on the very first hit would bias the pick.
SEARCH_MIN_RESPONSES = int(os.environ.get("SLSKD_SEARCH_MIN_RESPONSES", "5"))
SEARCH_MIN_WAIT      = float(os.environ.get("SLSKD_SEARCH_MIN_WAIT", "5"))
# How long to wait for slskd to publish responses it has already counted, when
# the early exit asked for them before the search finished.
SEARCH_SETTLE_TIMEOUT = float(os.environ.get("SLSKD_SEARCH_SETTLE", "25"))
SEARCH_HTTP_TIMEOUT  = 10  # per-request timeout; unrelated to the search deadline
# How long a group's slskd source list stays usable without re-searching.
# Peer queue lengths and free-slot flags rot within seconds — this is not a
# cache for its own sake, only enough to survive navigating away and back.
# "Search again" always bypasses it.
SOURCE_RESULTS_TTL = float(os.environ.get("SOURCE_RESULTS_TTL", "180"))
DOWNLOAD_POLL_INT = 60 # seconds between download status checks
# Stall watchdog. STALL_TIMEOUT only applies once a transfer is actually
# *transferring* (InProgress): Soulseek peers serve one or two files at a time,
# so the tail of a 12-track album legitimately sits at 0 bytes in the peer's
# remote queue for many minutes. Cancelling those was the main reason a fill
# ended 2-3 tracks short. Files still sitting in the remote queue get the far
# longer QUEUE_TIMEOUT instead, purely as a dead-peer safety net.
STALL_TIMEOUT     = 120 # seconds of no byte progress once a transfer is running
QUEUE_TIMEOUT     = 900 # seconds a transfer may sit in the peer's queue at 0 bytes
MAX_FILE_RETRIES  = 2   # per-file alt-source retries before giving up on a track

# Persistence — survives container restarts. Lives in the appdata mount.
STATE_FILE        = os.environ.get("LB_BOT_STATE", "lb_bot_state.json")
STATE_FLUSH_INT   = 30    # seconds between background state saves

# Local web dashboard. The dashboard is intentionally served by the bot process
# so it can reuse the same Navidrome, slskd, MusicBrainz and beets helpers.
WEB_UI_ENABLED = os.environ.get("LB_BOT_WEB", "1").lower() not in ("0", "false", "no")
WEB_UI_HOST    = os.environ.get("LB_BOT_WEB_HOST", "0.0.0.0")
WEB_UI_PORT    = int(os.environ.get("LB_BOT_WEB_PORT", "8899"))
MUSIC_LIBRARY_PATH = os.environ.get("LB_BOT_MUSIC_DIR", "/music")
LB_BOT_STAGING_DIR = os.environ.get(
    "LB_BOT_STAGING_DIR",
    os.path.join(os.path.dirname(STATE_FILE) or ".", "staging"))
REVIEW_FILE = os.environ.get("LB_BOT_REVIEW_FILE", "/config/missing_album_review.json")
LIBRARY_INDEX_FILE = os.environ.get("LB_BOT_LIBRARY_INDEX", "/config/library_index.db")
LB_BOT_INDEX_TTL_DAYS = float(os.environ.get("LB_BOT_INDEX_TTL_DAYS", "30"))
# Where a "deleted" duplicate actually goes. Inside the library by default so the
# move is a rename rather than a copy of a whole FLAC, and dot-prefixed so
# Navidrome skips it. Point it outside MUSIC_LIBRARY_PATH if your Navidrome does
# index dot-directories.
LB_BOT_TRASH_DIR = os.environ.get(
    "LB_BOT_TRASH_DIR", os.path.join(MUSIC_LIBRARY_PATH, ".lb-bot-trash"))
FUZZY_DUPLICATES_DEFAULT = os.environ.get(
    "LB_BOT_FUZZY_DUPES", "0").lower() in ("1", "true", "yes")
LB_BOT_REPAIR_JOBS = os.environ.get("LB_BOT_REPAIR_JOBS", "1").lower() not in ("0", "false", "no")
# Optional: the navi-connect hub, told when an album lands so its clients can
# refresh a page they already have open. Purely a nicety — every client re-reads
# the index on its own soon enough — so an unset URL, a wrong token or a hub that
# is down all mean "no ping", never a failed placement.
HUB_NOTIFY_URL   = os.environ.get("LB_BOT_HUB_URL", "").rstrip("/")
HUB_NOTIFY_TOKEN = os.environ.get("LB_BOT_HUB_TOKEN", "")

# Placement file mode. Nothing in this image reads a UMASK env var on our behalf
# (python:3.11-slim, bare `python` CMD — no s6/linuxserver init), so apply it
# ourselves. Must run before the first os.makedirs. "002" -> 0775 dirs, 0664
# files, which is what the `user: "99:100"` in docker-compose.yml expects.
LB_BOT_UMASK = os.environ.get("LB_BOT_UMASK", "") or "002"
if LB_BOT_UMASK:
    try:
        os.umask(int(LB_BOT_UMASK, 8))
    except ValueError:
        print(f"  config: ignoring bad LB_BOT_UMASK={LB_BOT_UMASK!r} (want octal, e.g. 002)")

WEB_BUILD = "2026-06-21-scan-recovery-trusted-move-p13"

# ---------------------------------------------------------------------------
# Pooled HTTP
# ---------------------------------------------------------------------------
# Every outbound call used to go through the bare `requests` module functions,
# which build and discard a connection per call. Navidrome and slskd are on the
# LAN so that is only a handshake each, but MusicBrainz and Cover Art Archive
# are not, and the poll loop plus per-release metadata calls make a lot of them.
# One Session per host, so connections are kept alive and reused.
#
# Sessions are shared across threads, which is safe as long as nothing mutates
# them after construction — pass per-request headers via `headers=`, as the
# call sites already do. No Retry: several callers (MusicBrainz 503 handling,
# the slskd search delete) have their own retry semantics.
_http_sessions = {}
_http_sessions_lock = threading.Lock()

class _PooledHTTP:
    """`requests`-shaped façade that routes each call to its host's Session."""

    @staticmethod
    def _session_for(url: str):
        try:
            host = urllib.parse.urlsplit(url).netloc or "default"
        except Exception:
            host = "default"
        with _http_sessions_lock:
            sess = _http_sessions.get(host)
            if sess is None:
                sess = requests.Session()
                adapter = requests.adapters.HTTPAdapter(
                    pool_connections=4, pool_maxsize=20, max_retries=0)
                sess.mount("http://", adapter)
                sess.mount("https://", adapter)
                _http_sessions[host] = sess
            return sess

    def __getattr__(self, verb):
        def call(url, **kwargs):
            return getattr(self._session_for(url), verb)(url, **kwargs)
        return call

_http = _PooledHTTP()

# Startup behaviour
RUN_SCAN_ON_START = True  # run a scan when the bot boots...
STARTUP_SCAN_THROTTLE = 6 * 3600  # ...but skip it if a scan ran in the last N seconds

# /album behaviour
# False → always show a confirmation card (with cover) before downloading.
# True  → when the album resolves with high confidence, download immediately.
ALBUM_AUTO_DOWNLOAD = False

# Housekeeping
UID_TTL          = 24 * 3600   # forget untouched button state after N seconds
ALBUM_GROUP_TTL  = 6 * 3600    # finalize stale album groups after N seconds
# How long a group with no transfers left in flight is given before it counts as
# orphaned rather than merely new. Covers the gap between creating the group and
# registering its files, and any brief window where slskd has not yet listed them.
ALBUM_GROUP_ORPHAN_GRACE = 180
SWEEP_INT        = 3600        # seconds between housekeeping sweeps

# Accepted formats and their priority (lower = better)
FORMAT_PRIORITY = {"flac": 0, "opus": 1}

# ── Source-selection preferences (System → Source preferences) ───────────────
# User-tunable overrides for the ranking/guard behavior below. Defaults
# preserve historical behavior exactly (all guards off, fallback = pick best);
# overrides persist to prefs.json next to STATE_FILE and survive restarts.
_PREFS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(STATE_FILE)) or ".", "prefs.json")
# Which copy of an album to prefer when several are on offer. Scored against a
# folder's dominant codec/bitrate/bit-depth in `_quality_preference_score`.
QUALITY_PREFERENCES = ("flac-any", "flac-16-44", "highest-bitrate", "prefer-opus")
_PREFS_DEFAULTS = {
    "ranks": sorted(FORMAT_PRIORITY, key=FORMAT_PRIORITY.get),
    "fallback": "best",              # best | ask | skip
    # Which copy of an album to prefer when several are available. Formerly
    # implicit and unchangeable ("higher bitrate always wins", buried in the
    # file-level score), which is wrong for anyone who wants a standard CD rip
    # rather than the largest possible file.
    "quality": "flac-any",           # see QUALITY_PREFERENCES
    "guards": {
        "requireFullCoverage": False,  # reject folders visibly short of the tracklist
        "maxAlbumSizeMB": 0,           # 0 = off
        "minSpeedMbps": 0.0,           # 0 = off
        "maxQueueLength": 0,           # 0 = off
    },
}
_prefs_overrides: dict = {}

def _effective_prefs() -> dict:
    g = dict(_PREFS_DEFAULTS["guards"])
    g.update(_prefs_overrides.get("guards", {}))
    ranks = [f for f in _prefs_overrides.get("ranks", []) if f in FORMAT_PRIORITY]
    ranks += [f for f in _PREFS_DEFAULTS["ranks"] if f not in ranks]
    fallback = _prefs_overrides.get("fallback", _PREFS_DEFAULTS["fallback"])
    if fallback not in ("best", "ask", "skip"):
        fallback = "best"
    quality = _prefs_overrides.get("quality", _PREFS_DEFAULTS["quality"])
    if quality not in QUALITY_PREFERENCES:
        quality = _PREFS_DEFAULTS["quality"]
    return {"ranks": ranks, "fallback": fallback, "quality": quality, "guards": g}

def _apply_prefs_ranks() -> None:
    """Rebuild FORMAT_PRIORITY in place from the effective rank order so every
    consumer (search scoring, file acceptance) follows the user's ordering."""
    ranks = _effective_prefs()["ranks"]
    FORMAT_PRIORITY.clear()
    FORMAT_PRIORITY.update({fmt: i for i, fmt in enumerate(ranks)})

def _save_prefs() -> None:
    try:
        _atomic_json_write(_PREFS_FILE, _prefs_overrides)
    except Exception as e:
        print(f"  prefs save failed: {e}")

def _load_prefs() -> None:
    global _prefs_overrides
    if not os.path.exists(_PREFS_FILE):
        return
    try:
        with open(_PREFS_FILE) as fh:
            _prefs_overrides = json.load(fh) or {}
        _apply_prefs_ranks()
        print(f"  prefs restored from {_PREFS_FILE}")
    except Exception as e:
        print(f"  prefs load failed: {e}")

LIVE_RE = re.compile(
    r"\b(live|concert|bootleg|in.concert|at.the|unplugged|acoustic)\b",
    re.IGNORECASE,
)
COMPILATION_RE = re.compile(
    r"\b(compilation|various.artists|soundtrack|best.of|greatest.hits|"
    r"collection|anthology|va\b|ost\b)\b",
    re.IGNORECASE,
)

USERS = [
    {
        "telegram_token":     os.environ.get("TELEGRAM_TOKEN", ""),
        "chat_id":            os.environ.get("TELEGRAM_CHAT_ID", ""),
        "navidrome_user":     os.environ.get("NAVIDROME_USER", ""),
        "navidrome_password": os.environ.get("NAVIDROME_PASSWORD", ""),
        "listenbrainz_user":  os.environ.get("LISTENBRAINZ_USER", ""),
        "playlist_sources": {
            "weekly-exploration": "Weekly Exploration",
            "weekly-jams":        "Weekly Jams",
        },
    },
    # {
    #     "telegram_token":     "OTHER_TOKEN",
    #     "chat_id":            "OTHER_CHAT_ID",
    #     "navidrome_user":     "alice",
    #     "navidrome_password": "alicepass",
    #     "listenbrainz_user":  "alice_lbz",
    #     "playlist_sources":   {"weekly-jams": "Weekly Jams"},
    # },
]

# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------

missing_by_playlist:  dict = {}  # token -> {playlist_name -> [track]}
pending_approvals:    dict = {}  # token -> {aid -> {username, file, label, track}}
pending_retries:      dict = {}  # token -> {rid -> track}
manual_search_results:dict = {}  # token -> {sid -> [candidate]}
# (username, filename) -> {track, token, chat_id, candidates, album_group_id}
pending_downloads:    dict = {}

# album_group_id -> {label, token, chat_id, total, completed, failed, ts}
pending_album_groups: dict = {}

# bid -> {chat_id, folders}  (/beets folder picker)
_pending_beets:       dict = {}
# cid -> {token, chat_id, artist, album, tracks}  (/checkalbums download)
_pending_checkdl:     dict = {}
# dlid -> {token, chat_id, playlist_name}  (Download-all mode picker)
_pending_dlall:       dict = {}
# alid -> resolution {token, chat_id, query, candidates} OR
#         resolved   {token, chat_id, rgid, release_mbid, title, artist, year, total_tracks}
_pending_album:       dict = {}
# recid -> completed-album record for post-import control (Adjust / review)
#   {token, chat_id, label, artist, album, release_mbid, rgid, album_dir, status, ts}
_albums:              dict = {}
# pickid -> {recid, kind: 'release'|'art', candidates:[...]}  (Adjust sub-pickers)
_pending_pick:        dict = {}
# job_id -> durable repair state. Album Review is a projection over these jobs.
repair_jobs:          dict = {}

_uid_to_token   = {}   # uid -> token
_uid_to_playlist = {}  # uid -> playlist_name
_uid_ts          = {}  # uid -> created timestamp (for TTL housekeeping)

_imported_folders: set = set()   # absolute paths already imported via /beets
_dismissed_folders: set = set()  # download folders hidden from Needs placement
# Download folders the bot did NOT queue (grabbed through slskd by hand) carry
# no release MBID, so they have to be identified from their tags/name before
# they can be placed. Identification costs MusicBrainz calls at 1 req/sec, so
# results are cached by path and invalidated by _folder_fingerprint.
_folder_identity: dict = {}      # abs folder path -> identity record
_identify_lock = threading.Lock()  # one identify sweep at a time
_last_scan_ts:    dict = {}      # listenbrainz_user -> last scan epoch
_scans_running:   set = set()    # listenbrainz_users with a scan in flight
_review_lock = threading.RLock()
_review_scan_lock = threading.Lock()
_active_review_scan_task_id = ""
_review_state = {
    "version": 1,
    "updated_at": 0,
    "status": "idle",
    "message": "",
    "fuzzy": FUZZY_DUPLICATES_DEFAULT,
    "deep": False,
    "groups": [],
    "tasks": {},
    "searches": {},
    "operations": {},
}
_web_events: list = []

_uid_counter = 0
_atomic_write_locks = {}
_atomic_write_locks_guard = threading.Lock()
_state_save_lock = threading.Lock()

def _uid(kind: str) -> str:
    """Return a process-globally-unique short id, prefixed by kind for readability."""
    global _uid_counter
    _uid_counter += 1
    uid = f"{kind}{_uid_counter}"
    _uid_ts[uid] = time.time()
    return uid

def _token_for(uid: str):
    return _uid_to_token.get(uid)

def _register_playlist(token: str, name: str) -> str:
    uid = _uid("p")
    _uid_to_token[uid]    = token
    _uid_to_playlist[uid] = name
    return uid

# ---------------------------------------------------------------------------
# Persistence — best-effort JSON snapshot so buttons survive a restart
# ---------------------------------------------------------------------------

def _atomic_json_write(path: str, snapshot, *, indent=None,
                       sort_keys: bool = False, text: str = None) -> None:
    """Durably replace JSON without sharing a racy fixed temporary path.

    `text` is an already-serialized body. Callers holding a lock over shared
    mutable state should serialize under that lock and pass the string here, so
    the pretty-print and the fsync happen after the lock is released — a
    multi-MB `json.dump` plus a disk sync is not something to hold a global
    lock across (see _save_review_state).
    """
    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, exist_ok=True)
    with _atomic_write_locks_guard:
        lock = _atomic_write_locks.setdefault(os.path.abspath(path), threading.Lock())
    tmp = ""
    with lock:
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=parent,
                    prefix=f".{os.path.basename(path)}.", suffix=".tmp",
                    delete=False) as fh:
                tmp = fh.name
                if text is None:
                    json.dump(snapshot, fh, indent=indent, sort_keys=sort_keys)
                else:
                    fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            # mkstemp hands back 0600 regardless of umask; the state files have
            # to stay group-writable so a later run under a different uid in the
            # `users` group can still rewrite them.
            try:
                os.chmod(tmp, 0o664)
            except Exception:
                pass
            os.replace(tmp, path)
            tmp = ""
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass

def _save_state_unlocked() -> None:
    """Write durable state to STATE_FILE. Called periodically and on shutdown."""
    try:
        snapshot = {
            "uid_counter":          _uid_counter,
            "uid_to_token":         _uid_to_token,
            "uid_to_playlist":      _uid_to_playlist,
            "uid_ts":               _uid_ts,
            "missing_by_playlist":  missing_by_playlist,
            "pending_approvals":    pending_approvals,
            "pending_retries":      pending_retries,
            "manual_search_results": manual_search_results,
            "pending_album_groups": pending_album_groups,
            "pending_beets":        _pending_beets,
            "pending_checkdl":      _pending_checkdl,
            "pending_dlall":        _pending_dlall,
            "pending_album":        _pending_album,
            "albums":               _albums,
            "pending_pick":         _pending_pick,
            "repair_jobs":          repair_jobs,
            "imported_folders":     sorted(_imported_folders),
            "dismissed_folders":    sorted(_dismissed_folders),
            "folder_identity":      _folder_identity,
            "last_scan_ts":         _last_scan_ts,
            # MB entity lookups are immutable — persist them (skip search queries)
            "mbz_cache":            {k: v for k, v in _mbz_cache.items()
                                     if "/" in k.split("?", 1)[0]},
            # tuple-keyed dict -> list of [ [username, filename], value ]
            "pending_downloads":    [[list(k), v] for k, v in pending_downloads.items()],
        }
        _atomic_json_write(STATE_FILE, snapshot)
    except Exception as e:
        print(f"  state save failed: {e}")

def _save_state() -> None:
    with _state_save_lock:
        _save_state_unlocked()

def _load_state() -> None:
    """Restore state from STATE_FILE if present. Safe to call once at startup."""
    global _uid_counter
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as fh:
            s = json.load(fh)
    except Exception as e:
        print(f"  state load failed: {e}")
        return
    _uid_counter = s.get("uid_counter", 0)
    _uid_to_token.update(s.get("uid_to_token", {}))
    _uid_to_playlist.update(s.get("uid_to_playlist", {}))
    _uid_ts.update(s.get("uid_ts", {}))
    missing_by_playlist.update(s.get("missing_by_playlist", {}))
    pending_approvals.update(s.get("pending_approvals", {}))
    pending_retries.update(s.get("pending_retries", {}))
    manual_search_results.update(s.get("manual_search_results", {}))
    pending_album_groups.update(s.get("pending_album_groups", {}))
    _pending_beets.update(s.get("pending_beets", {}))
    _pending_checkdl.update(s.get("pending_checkdl", {}))
    _pending_dlall.update(s.get("pending_dlall", {}))
    _pending_album.update(s.get("pending_album", {}))
    _albums.update(s.get("albums", {}))
    _pending_pick.update(s.get("pending_pick", {}))
    repair_jobs.update(s.get("repair_jobs", {}))
    _imported_folders.update(s.get("imported_folders", []))
    _dismissed_folders.update(s.get("dismissed_folders", []))
    _folder_identity.update(s.get("folder_identity", {}))
    _last_scan_ts.update(s.get("last_scan_ts", {}))
    _mbz_cache.update(s.get("mbz_cache", {}))
    for k, v in s.get("pending_downloads", []):
        pending_downloads[tuple(k)] = v
    print(f"  state restored from {STATE_FILE} "
          f"({len(pending_downloads)} active download(s), "
          f"{len(pending_album_groups)} album group(s))")

_LOG_TAG_WORDS = ("slskd", "navidrome", "beets", "telegram", "spotify",
                  "musicbrainz", "scan", "task", "import", "placement",
                  "download", "search")

def _web_log(message: str, tag: str = "", severity: str = "") -> None:
    """Structured log entry. Callers may pass just a message — tag/severity are
    derived from its text so the UI's filter chips work without touching every
    call site."""
    safe = _redact_secrets(str(message))
    low = safe.lower()
    if not severity:
        severity = ("error" if any(w in low for w in ("error", "failed", "refused",
                                                      "unreachable", "exception"))
                    else "info")
    if not tag:
        tag = next((w for w in _LOG_TAG_WORDS if w in low), "app")
    print(f"  web: {safe}")
    _web_events.append({
        "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "epoch": time.time(),
        "tag": tag,
        "severity": severity,
        "msg": safe,
    })
    del _web_events[:-200]

def _web_event_line(entry) -> str:
    """Render a structured entry back to the legacy string format."""
    if isinstance(entry, dict):
        return f"{entry.get('ts', '')}  {entry.get('msg', '')}"
    return str(entry)

def _empty_review_state() -> dict:
    return {
        "version": 1,
        "updated_at": time.time(),
        "status": "idle",
        "message": "",
        "fuzzy": FUZZY_DUPLICATES_DEFAULT,
        "deep": False,
        "groups": [],
        "duplicate_groups": [],
        # Sets of files inside one album that are the same song — the residue of
        # a mis-matched gap fill, or of two rips of one album sharing a folder.
        "duplicate_files": [],
        # Counters from the walk that produced duplicate_files. An empty result
        # is ambiguous on its own — these say whether we actually looked.
        "duplicate_file_stats": {},
        "duplicate_files_scanned_at": 0,
        "tasks": {},
        "searches": {},
        "operations": {},
    }

def _load_review_state() -> None:
    global _review_state
    if not REVIEW_FILE or not os.path.exists(REVIEW_FILE):
        return
    try:
        with open(REVIEW_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("review file root is not an object")
        with _review_lock:
            base = _empty_review_state()
            base.update(data)
            base.setdefault("groups", [])
            base.setdefault("duplicate_groups", [])
            base.setdefault("tasks", {})
            base.setdefault("searches", {})
            base.setdefault("operations", {})
            # Source results are not persisted with their file payload (see
            # _review_state_for_disk), so a restored list can rank and enqueue
            # nothing. Drop it and let the album ask for a fresh search rather
            # than showing sources that turn out to be empty.
            for group in base["groups"]:
                if (group.get("source_results") or {}).get("folders"):
                    group.pop("source_results", None)
            # Threads do not survive a process/container restart. Never let a
            # persisted "running" flag permanently block the next scan.
            if base.get("status") == "running":
                base["status"] = "error"
                base["message"] = "Previous scan was interrupted by an app restart; it can be started again."
            now = time.time()
            for task in base["tasks"].values():
                if task.get("status") == "running":
                    task.update({
                        "status": "error",
                        "error": "Interrupted by an app restart",
                        "summary": "Interrupted by an app restart",
                        "finished_at": now,
                        "percent": 100,
                    })
            for operation in base["operations"].values():
                if operation.get("status") in ("queued", "running"):
                    operation.update({
                        "status": "error",
                        "error": "Interrupted by an app restart",
                        "updated_at": now,
                    })
            _review_state = base
        print(f"  review state restored from {REVIEW_FILE} "
              f"({len(_review_state.get('groups', []))} group(s))")
    except Exception as e:
        print(f"  review state load failed: {e}")

def _save_review_state() -> None:
    """Persist the review state, holding _review_lock only to serialize.

    Called from ~50 places, several of them on the 2s poll path. It used to
    serialize the (multi-MB) state three times — dumps + loads for a deep copy,
    then a pretty-printed sorted json.dump — and fsync it, all inside
    _review_lock, which every /api/gaps and /api/summary request also wants.
    One dump to a string under the lock *is* the snapshot: nothing can mutate a
    str, so the write happens safely outside. The file is machine-only, so
    indent/sort_keys buy nothing and roughly double the bytes written.
    """
    _invalidate_review_snapshot()
    if not REVIEW_FILE:
        return
    try:
        with _review_lock:
            _review_state["updated_at"] = time.time()
            text = json.dumps(_review_state_for_disk())
        _atomic_json_write(REVIEW_FILE, None, text=text)
    except Exception as e:
        print(f"  review state save failed: {e}")

# Keys on a source_results folder holding the raw slskd payload: the peer's
# search hits, the expanded directory listing, and the per-file claim ledger.
# They are working data for the life of a search — the enqueue and placement
# paths read them off the live object — but persisting them turned the review
# file into megabytes of peer filenames rewritten on every one of the ~50
# saves. A restart drops them, which SOURCE_RESULTS_TTL already implies.
_SOURCE_FOLDER_TRANSIENT = ("files", "_expanded", "_claimed")

def _review_state_for_disk() -> dict:
    """The review state without the per-search slskd payload.

    Shallow copies along the one path that needs rewriting, so this stays
    cheap — the whole point is to write less, not to deep-copy first. Caller
    must hold _review_lock.
    """
    groups = []
    for group in _review_state.get("groups", []) or []:
        src = group.get("source_results") or {}
        folders = src.get("folders") or []
        if not folders:
            groups.append(group)
            continue
        thin = dict(group)
        thin["source_results"] = dict(src, folders=[
            {k: v for k, v in fd.items() if k not in _SOURCE_FOLDER_TRANSIENT}
            for fd in folders])
        groups.append(thin)
    return dict(_review_state, groups=groups)

def _request_scope():
    """Flask's per-request `g`, or None outside a request.

    Imported lazily: this module is importable (and the Telegram half runs)
    without the web UI enabled.
    """
    try:
        from flask import g, has_request_context
    except Exception:
        return None
    return g if has_request_context() else None

def _invalidate_review_snapshot() -> None:
    scope = _request_scope()
    if scope is not None:
        scope.__dict__.pop("_lb_review_snap", None)
        scope.__dict__.pop("_lb_review_list_snap", None)

def _review_snapshot() -> dict:
    """Deep copy of the review state, memoized for the duration of a web request.

    /api/summary alone used to serialize the whole multi-MB review state five
    times per request (gaps, transfers, needs-placement, and two inline reads)
    while holding the global review lock. Within one request nothing changes
    under us unless the request itself saves — and _save_review_state drops the
    memo when it does — so one copy is enough.
    """
    scope = _request_scope()
    if scope is not None:
        snap = scope.__dict__.get("_lb_review_snap")
        if snap is not None:
            return snap
    with _review_lock:
        snap = json.loads(json.dumps(_review_state))
    if scope is not None:
        scope.__dict__["_lb_review_snap"] = snap
    return snap

# The only group fields the gap *list* reads, via _album_view and
# _review_group_next_action. Everything else on a group — above all
# source_results.folders, which carries every peer's whole file listing — is
# dead weight for this view, and _review_snapshot deep-copies all of it on a
# 2s poll. Keep this in sync with those two functions; a field missing here
# shows up as a wrong status badge, not an error.
_GAP_LIST_GROUP_FIELDS = ("id", "canonical_album_id", "artist", "album",
                          "present", "total", "extra", "updated_at",
                          "hidden", "status")

def _review_list_snapshot() -> dict:
    """A small copy of the review state carrying only what the list views read.

    _gaps_view and _summary_view together render a few scalars per group, and
    used to pay a whole-state deep copy under _review_lock for the privilege.
    """
    scope = _request_scope()
    if scope is not None:
        snap = scope.__dict__.get("_lb_review_list_snap")
        if snap is not None:
            return snap
    # Taken before _review_lock: nothing else nests these two, and reading it
    # inside would invent a lock order for no reason.
    with _review_scan_lock:
        tid = _active_review_scan_task_id
    with _review_lock:
        groups = []
        for group in _review_state.get("groups", []) or []:
            # Only keys that are actually present: writing a None for an absent
            # one would satisfy the `group.get(k, default)` in _album_view and
            # put a null on the wire where it expects "" or 0.
            row = {k: group[k] for k in _GAP_LIST_GROUP_FIELDS if k in group}
            # Only `decision` is read off a track, only `status` off a match
            # item, and `albums` only for truthiness.
            row["missing_tracks"] = [{"decision": t.get("decision", "pending")}
                                     for t in (group.get("missing_tracks") or [])]
            row["match_items"] = [{"status": (m or {}).get("status")}
                                  for m in (group.get("match_items") or [])]
            row["albums"] = [{}] * len(group.get("albums") or [])
            groups.append(row)
        task = (_review_state.get("tasks", {}) or {}).get(tid) if tid else None
        snap = {
            "groups":     groups,
            "status":     _review_state.get("status", ""),
            "message":    _review_state.get("message", ""),
            "updated_at": _review_state.get("updated_at", 0),
            "tasks":      {tid: json.loads(json.dumps(task))} if task else {},
        }
    if scope is not None:
        scope.__dict__["_lb_review_list_snap"] = snap
    return snap

REPAIR_JOB_ACTIVE_STATUSES = {
    "needs_review", "needs_source", "source_selected", "downloading",
    "downloaded_unmatched", "matching", "matched_ready_to_import", "staging",
    "importing", "imported_unverified", "verifying", "blocked_no_source",
    "blocked_no_match", "blocked_ambiguous_files", "blocked_slskd_error",
    "blocked_slskd_timeout", "blocked_permission",
}

def _find_repair_job(job_id: str) -> dict | None:
    return repair_jobs.get(job_id)

def _repair_job_for_group(group_id: str) -> dict | None:
    for job in repair_jobs.values():
        if job.get("group_id") == group_id and job.get("status") != "archived":
            return job
    return None

def _save_repair_jobs() -> None:
    _save_state()

def _repair_job_touch(job: dict, message=None) -> dict:
    now = time.time()
    job["updated_at"] = now
    if message:
        msg = message if isinstance(message, dict) else {"kind": "info", "message": str(message)}
        msg.setdefault("ts", now)
        if msg.get("message"):
            msg["message"] = _redact_secrets(msg["message"])
        job.setdefault("messages", []).append(msg)
    return job

def _repair_track_id(group_id: str, idx: int, track: dict) -> str:
    raw = "|".join([
        group_id or "",
        str(track.get("mbid", "") or track.get("recording_mbid", "")),
        str(track.get("position", "")),
        track.get("title", ""),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

def _repair_track_status_from_decision(decision: str) -> str:
    return {
        "pending": "missing",
        "approved": "approved",
        "source_pending": "approved",
        "queued": "queued",
        "downloading": "downloading",
        "downloaded": "downloaded",
        "failed": "download_error",
        "cancelled": "cancelled",
        "skipped": "skipped",
        "needs_match": "downloaded",
        "placed": "moved",
        "verified": "navidrome_verified",
    }.get(decision or "pending", decision or "missing")

def _review_decision_from_repair_track(status: str) -> str:
    return {
        "missing": "pending",
        "approved": "approved",
        "queued": "queued",
        "downloading": "downloading",
        "downloaded": "downloaded",
        "download_error": "failed",
        "download_timeout": "failed",
        "cancelled": "cancelled",
        "file_matched": "downloaded",
        "match_ambiguous": "failed",
        "match_missing": "failed",
        "staged": "downloaded",
        "tagged": "downloaded",
        "moved": "placed",
        "navidrome_pending": "placed",
        "navidrome_verified": "verified",
        "deferred": "pending",
        "skipped": "skipped",
        "error": "failed",
    }.get(status or "missing", "pending")

def _canonical_album_for_group(group: dict) -> dict:
    return next((a for a in group.get("albums", [])
                 if a.get("id") == group.get("canonical_album_id")), None) or (
        (group.get("albums") or [{}])[0])

def _canonical_tracklist_from_group(group: dict) -> list:
    canonical = _canonical_album_for_group(group)
    rows = []
    seen = set()
    for idx, song in enumerate(canonical.get("tracks", []) or []):
        title = song.get("title", "")
        mbid = song.get("musicBrainzId", "") or song.get("mbid", "")
        key = (mbid, title)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "track_id": _repair_track_id(group.get("id", ""), idx, {
                "mbid": mbid, "title": title, "position": idx + 1}),
            "recording_mbid": mbid,
            "title": title,
            "artist": song.get("artist", "") or group.get("artist", ""),
            "position": song.get("trackNumber") or song.get("position") or idx + 1,
            "duration": song.get("duration") or song.get("durationMs") or 0,
        })
    for idx, track in enumerate(group.get("missing_tracks", []) or []):
        mbid = track.get("mbid", "")
        title = track.get("title", "")
        key = (mbid, title)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "track_id": _repair_track_id(group.get("id", ""), idx, track),
            "recording_mbid": mbid,
            "title": title,
            "artist": track.get("artist", "") or group.get("artist", ""),
            "position": track.get("position", idx + 1),
            "duration": track.get("duration", 0),
        })
    return rows

def _repair_job_track_from_group(group: dict, idx: int, track: dict,
                                 previous: dict = None) -> dict:
    previous = previous or {}
    decision = track.get("decision", "pending")
    # A re-approved track must shed its old failure: keeping the previous status
    # left it stuck at download_error/timeout, which then projected straight back
    # over the fresh "approved" decision and made the retry look already-failed.
    if decision in ("approved", "source_pending", "pending"):
        status = _repair_track_status_from_decision(decision)
        track.pop("download_error", None)
        track.pop("download_timeout", None)
        previous = {k: v for k, v in previous.items() if k not in ("error",)}
    else:
        status = previous.get("status") or _repair_track_status_from_decision(decision)
    row = {
        "id": previous.get("id") or _repair_track_id(group.get("id", ""), idx, track),
        "group_track_index": idx,
        "recording_mbid": track.get("mbid", "") or previous.get("recording_mbid", ""),
        "artist": track.get("artist", "") or group.get("artist", ""),
        "title": track.get("title", ""),
        "position": track.get("position", idx + 1),
        "duration": track.get("duration", previous.get("duration", 0)),
        "status": status,
        "source_user": track.get("source_user", previous.get("source_user", "")),
        "source_folder": track.get("source_folder", previous.get("source_folder", "")),
        "local_path": track.get("local_path", previous.get("local_path", "")),
        "error": track.get("error") or track.get("download_error") or previous.get("error", ""),
        "updated_at": previous.get("updated_at", time.time()),
    }
    for key in ("download_percent", "download_state", "filename", "matched_relpath"):
        if track.get(key) is not None or previous.get(key) is not None:
            row[key] = track.get(key, previous.get(key))
    return row

def _repair_job_status_from_tracks(job: dict) -> str:
    statuses = [t.get("status", "missing") for t in job.get("tracks", [])]
    if not statuses:
        return "needs_review"
    if any(s == "match_ambiguous" for s in statuses):
        return "blocked_ambiguous_files"
    if any(s == "match_missing" for s in statuses):
        return "blocked_no_match"
    if any(s == "download_timeout" for s in statuses):
        return "blocked_slskd_timeout"
    if any(s == "download_error" for s in statuses):
        return "blocked_slskd_error"
    if any(s == "cancelled" for s in statuses):
        return "cancelled"
    if all(s in ("moved", "navidrome_pending", "navidrome_verified",
                 "skipped", "deferred") for s in statuses):
        return ("verified_complete"
                if all(s in ("navidrome_verified", "skipped", "deferred")
                       for s in statuses) else "placed")
    if all(s in ("file_matched", "staged", "skipped", "deferred") for s in statuses):
        return "matched_ready_to_import"
    if any(s == "downloaded" for s in statuses):
        return "downloaded_unmatched"
    if any(s == "downloading" for s in statuses):
        return "downloading"
    if any(s == "queued" for s in statuses):
        return "downloading"
    if any(s == "approved" for s in statuses):
        return "needs_source"
    return "needs_review"

def _create_or_update_repair_job_from_group(group: dict, approved_tracks=None) -> dict:
    if not LB_BOT_REPAIR_JOBS or not group:
        return {}
    now = time.time()
    job = _repair_job_for_group(group.get("id", ""))
    if not job:
        job_id = _uid("job")
        job = {
            "id": job_id,
            "group_id": group.get("id", ""),
            "artist": group.get("artist", ""),
            "album": group.get("album", ""),
            "canonical_album_id": group.get("canonical_album_id", ""),
            "canonical_release_mbid": group.get("canonical_mbid", ""),
            "canonical_release_group_mbid": "",
            "canonical_tracklist": [],
            "status": "needs_review",
            "tracks": [],
            "downloads": [],
            "source_pools": [],
            "file_matches": [],
            "import_attempts": [],
            "verification": {},
            "messages": [],
            "created_at": now,
            "updated_at": now,
        }
        repair_jobs[job_id] = job
        _repair_job_touch(job, {"kind": "created", "message": "Repair job created"})
    old_release = job.get("canonical_release_mbid", "")
    new_release = group.get("canonical_mbid", "")
    if old_release and new_release and old_release != new_release:
        _repair_job_touch(job, {
            "kind": "canonical_release_changed",
            "message": f"Canonical release changed from {old_release} to {new_release}",
            "from": old_release,
            "to": new_release,
        })
    job.update({
        "group_id": group.get("id", job.get("group_id", "")),
        "artist": group.get("artist", job.get("artist", "")),
        "album": group.get("album", job.get("album", "")),
        "canonical_album_id": group.get("canonical_album_id", job.get("canonical_album_id", "")),
        "canonical_release_mbid": new_release or job.get("canonical_release_mbid", ""),
        "canonical_tracklist": _canonical_tracklist_from_group(group),
    })
    previous = {t.get("id"): t for t in job.get("tracks", [])}
    approved_keys = None
    if approved_tracks is not None:
        approved_keys = {
            (t.get("mbid") or t.get("recording_mbid", ""), t.get("title", ""))
            for t in approved_tracks
        }
    tracks = []
    for idx, track in enumerate(group.get("missing_tracks", []) or []):
        tid = _repair_track_id(group.get("id", ""), idx, track)
        row = _repair_job_track_from_group(group, idx, track, previous.get(tid))
        if approved_keys and (row.get("recording_mbid", ""), row.get("title", "")) in approved_keys:
            if row["status"] == "missing":
                row["status"] = "approved"
        tracks.append(row)
        track["repair_job_id"] = job["id"]
        track["repair_track_id"] = row["id"]
    job["tracks"] = tracks
    if job.get("status") not in ("archived", "verified_complete"):
        job["status"] = _repair_job_status_from_tracks(job)
    _repair_job_touch(job)
    return job

def _apply_repair_job_projection_to_group(group: dict) -> dict:
    if not LB_BOT_REPAIR_JOBS or not group:
        return group
    job = _repair_job_for_group(group.get("id", ""))
    if not job:
        return group
    group["repair_job_id"] = job.get("id", "")
    group["repair_status"] = job.get("status", "")
    by_id = {t.get("id"): t for t in job.get("tracks", [])}
    by_key = {(t.get("recording_mbid", ""), t.get("title", "")): t
              for t in job.get("tracks", [])}
    for idx, track in enumerate(group.get("missing_tracks", []) or []):
        tid = track.get("repair_track_id") or _repair_track_id(group.get("id", ""), idx, track)
        jt = by_id.get(tid) or by_key.get((track.get("mbid", ""), track.get("title", "")))
        if not jt:
            continue
        track["repair_job_id"] = job.get("id", "")
        track["repair_track_id"] = jt.get("id", "")
        projected = _review_decision_from_repair_track(jt.get("status", "missing"))
        # One-directional: a track the user has just re-approved is a *newer*
        # statement than the job row it was built from, and overwriting it with
        # the job's stale "failed" was what made a failed track un-retryable —
        # every re-approve was undone by the next projection pass.
        if track.get("decision") in ("approved", "source_pending"):
            projected = track["decision"]
            track.pop("download_error", None)
        track["decision"] = projected
        for key in ("local_path", "source_user", "source_folder", "download_percent",
                    "download_state", "filename", "matched_relpath"):
            if jt.get(key):
                track[key] = jt[key]
        if jt.get("error") and projected not in ("approved", "source_pending"):
            track["download_error"] = jt["error"]
    return group

def _placement_outcomes(result: dict) -> dict:
    """Index a _deterministic_album_import result's per_file rows by the track
    they were about, so placement can be reported per track instead of assumed.

    Keys are the recording MBID when the row has one, plus a normalized title,
    so a review track can be looked up either way.
    """
    out = {}
    for row in (result or {}).get("per_file", []) or []:
        status = (row.get("status") or "")
        placed = status.startswith("matched")
        entry = {"placed": placed,
                 "reason": row.get("reason", "") or status}
        for key in (row.get("recording_mbid", ""), _match_key(row.get("title", ""))):
            if not key:
                continue
            # A placed row always wins over a failed one for the same key: the
            # bonus-track fallback pass can emit a second row for a slot that
            # already landed.
            if key not in out or (placed and not out[key]["placed"]):
                out[key] = entry
    return out

def _mark_group_tracks_placed(group: dict, result: dict = None) -> int:
    """
    After a deterministic import, advance in-flight track decisions so the group
    stops reporting as downloading. The repair-job track statuses advance too —
    otherwise _apply_repair_job_projection_to_group would overwrite the decision
    back to "downloaded" on the next refresh.

    When the import `result` is supplied, each track is marked from its own
    per_file outcome; tracks that were ambiguous, unmatched or failed to move
    are marked "failed" with the reason rather than being reported as placed.
    Without a result (callers that don't have one) every in-flight track is
    flipped to placed, the historical behaviour.
    """
    if not group:
        return 0
    job = _repair_job_for_group(group.get("id", "")) if LB_BOT_REPAIR_JOBS else None
    outcomes = _placement_outcomes(result) if result else None
    flipped = 0
    failed_tracks = 0
    for idx, track in enumerate(group.get("missing_tracks", []) or []):
        if track.get("decision") not in ("downloaded", "needs_match"):
            continue
        placed, reason = True, ""
        if outcomes is not None:
            hit = (outcomes.get(track.get("recording_mbid", ""))
                   or outcomes.get(_match_key(track.get("title", ""))))
            if hit is None:
                placed, reason = False, "not included in placement result"
            else:
                placed, reason = hit["placed"], hit["reason"]
        if not placed:
            track["decision"] = "failed"
            track["download_error"] = f"placement failed: {reason}"
            # A hand-picked file refused because its audio is already in the
            # album is the one case worth offering an override for: the user
            # chose this file deliberately, so name what it duplicates and let
            # them decide, rather than silently stranding it in /downloads.
            if track.get("manual_pick") and "already in the album" in reason:
                track["can_force_place"] = True
                track["force_place_conflict"] = reason.rsplit(" as ", 1)[-1]
            else:
                track.pop("can_force_place", None)
                track.pop("force_place_conflict", None)
            failed_tracks += 1
            if job:
                tid = track.get("repair_track_id") or _repair_track_id(
                    group.get("id", ""), idx, track)
                jt = _repair_track_by_id(job, tid)
                if jt:
                    jt["status"] = _repair_track_status_from_decision("failed")
                    jt["error"] = track["download_error"]
                    jt["updated_at"] = time.time()
            continue
        track["decision"] = "placed"
        track["imported_at"] = time.time()
        track.pop("download_error", None)
        flipped += 1
        if job:
            tid = track.get("repair_track_id") or _repair_track_id(
                group.get("id", ""), idx, track)
            jt = _repair_track_by_id(job, tid)
            if jt:
                jt["status"] = "moved"
                jt["updated_at"] = time.time()
    if failed_tracks:
        print(f"  placement: {failed_tracks} track(s) in group "
              f"{group.get('id', '')} did not land in the library")
    if flipped:
        group["status"] = "placed"
        group["updated_at"] = time.time()
        if job:
            job["status"] = _repair_job_status_from_tracks(job)
            _repair_job_touch(job, {"kind": "placed",
                                    "message": f"Placed {flipped} track(s) into library"})
    # The album is in the library — its transfer rows are done regardless of
    # whether the download watcher ever observed slskd finishing (it misses
    # completions after restarts or when slskd's history was cleared, which
    # left album groups stuck "downloading" forever).
    _pop_album_groups_for_review_group(group.get("id", ""))
    if flipped:
        _start_placement_verification(group.get("id", ""))
    return flipped

# ---------------------------------------------------------------------------
# Post-placement verification
#
# Navidrome is the success oracle, but until now nothing ever consulted it after
# a placement: a track went to "placed" and stayed there, so a file that landed
# in the wrong folder, failed to tag, or never got indexed looked identical to a
# successful fill. This makes the navidrome_pending -> navidrome_verified states
# real and resets anything that never showed up, so the next fill retries it.
# ---------------------------------------------------------------------------

PLACEMENT_VERIFY_TIMEOUT  = 600  # give Navidrome's scan this long to index
PLACEMENT_VERIFY_INTERVAL = 30

_placement_verifiers: set = set()
_placement_verify_lock = threading.Lock()

def _start_placement_verification(group_id: str) -> None:
    """Kick off (at most one) background verification pass for a group."""
    if not group_id:
        return
    with _placement_verify_lock:
        if group_id in _placement_verifiers:
            return
        _placement_verifiers.add(group_id)
    threading.Thread(target=_verify_placement_worker, args=(group_id,),
                     name=f"verify-{group_id[:16]}", daemon=True).start()

def _placed_track_indexes(group_id: str) -> list:
    """(index, track-copy) for tracks claiming to be placed but not yet verified."""
    with _review_lock:
        group = _find_review_group(group_id)
        if not group:
            return []
        return [(idx, dict(t))
                for idx, t in enumerate(group.get("missing_tracks", []) or [])
                if t.get("decision") == "placed"]

def _refresh_group_counts_after_fill(group_id: str) -> None:
    """Bring the album's present/total/missing counts back in line with reality.

    Runs at the end of the verification pass — by then Navidrome has had the whole
    `PLACEMENT_VERIFY_TIMEOUT` window (and the rescan `_nd_scan_after_import`
    triggered) to index what was placed, which is exactly when a re-read returns
    something new. Best-effort: failing to refresh a count must never break the
    fill that already succeeded.
    """
    try:
        if refresh_group_albums_from_navidrome(group_id):
            print(f"  group {group_id}: counts refreshed from Navidrome after fill")
    except Exception as e:
        print(f"  group {group_id}: count refresh failed: {e}")

def _verify_placement_worker(group_id: str) -> None:
    try:
        user = _default_web_user()
        if not user:
            return
        nd_user = user.get("navidrome_user", "")
        nd_pass = user.get("navidrome_password", "")
        deadline = time.time() + PLACEMENT_VERIFY_TIMEOUT
        while True:
            outstanding = _placed_track_indexes(group_id)
            if not outstanding:
                _refresh_group_counts_after_fill(group_id)
                _save_review_state()
                return
            for idx, track in outstanding:
                try:
                    present = nd_track_present(
                        track.get("artist", ""), track.get("title", ""),
                        track.get("mbid", ""), nd_user, nd_pass)
                except Exception as e:
                    print(f"  placement verify: Navidrome check failed: {e}")
                    present = False
                if present:
                    _set_review_track_state(group_id, idx, "verified",
                                            download_error="")
            if time.time() >= deadline:
                break
            time.sleep(PLACEMENT_VERIFY_INTERVAL)
        stranded = _placed_track_indexes(group_id)
        for idx, track in stranded:
            # Back to pending, not failed: nothing is known to be broken, the
            # track simply isn't in the library — which is exactly the state a
            # fresh fill should act on.
            _set_review_track_state(
                group_id, idx, "pending",
                download_error="placed but never appeared in Navidrome — "
                               "the file may have landed in the wrong folder")
        if stranded:
            print(f"  placement verify: {len(stranded)} track(s) in group "
                  f"{group_id} never appeared in Navidrome — reset to pending")
        _refresh_group_counts_after_fill(group_id)
        _save_review_state()
    finally:
        with _placement_verify_lock:
            _placement_verifiers.discard(group_id)

# ---------------------------------------------------------------------------
# Album fill status — the one-tap-download lifecycle, keyed by release MBID
#
# The remote clients (Feishin / Navic, through the navi-connect hub) fire a
# download from an artist page and then need to know what became of it. The
# task returned by /api/album/download cannot answer that: _album_download_task
# finishes the moment slskd accepts the enqueue, ~40-75s in, while the transfer,
# the placement and the Navidrome scan are all still ahead. A client polling the
# task would flip to "done" with nothing in the library.
#
# So the lifecycle is recorded here instead, at the points that actually know:
# the download task (searching / queued / failed-with-reason), _finalize_group
# (placing / placed / failed) and the verifier below (verified). Live transfer
# progress is *not* written here — it is read straight off pending_album_groups
# when the status endpoint is called, so the hot download-poll path pays nothing.
#
# In memory only, and deliberately so: an in-flight download does not survive a
# restart either (slskd source results are dropped on load for the same reason),
# and a client that finds no entry falls back to "unknown", which renders as the
# plain "not in your library" state.
# ---------------------------------------------------------------------------

_album_fill_status: dict = {}          # release_mbid -> status dict
_album_fill_lock = threading.Lock()
_ALBUM_FILL_STATUS_MAX = 200
ALBUM_FILL_VERIFY_TIMEOUT = PLACEMENT_VERIFY_TIMEOUT
ALBUM_FILL_VERIFY_INTERVAL = PLACEMENT_VERIFY_INTERVAL

def _album_fill_set(release_mbid: str, state: str, **fields) -> None:
    """Record where one release-group's fill has got to. Last writer wins."""
    if not release_mbid:
        return
    with _album_fill_lock:
        entry = _album_fill_status.setdefault(release_mbid, {})
        entry.update(fields)
        entry["state"] = state
        entry["updated_at"] = time.time()
        entry.setdefault("created_at", entry["updated_at"])
        if len(_album_fill_status) > _ALBUM_FILL_STATUS_MAX:
            oldest = sorted(_album_fill_status.items(),
                            key=lambda kv: kv[1].get("updated_at", 0))
            for key, _ in oldest[:len(_album_fill_status) - _ALBUM_FILL_STATUS_MAX]:
                _album_fill_status.pop(key, None)

def _album_fill_get(release_mbid: str) -> dict:
    with _album_fill_lock:
        return dict(_album_fill_status.get(release_mbid) or {})

def _album_group_for_release(release_mbid: str) -> tuple:
    """(group_id, group) of the in-flight transfer group for this release, if any."""
    if not release_mbid:
        return "", None
    for gid, ag in list(pending_album_groups.items()):
        if release_mbid in (ag.get("target_release_mbid"), ag.get("release_mbid")):
            return gid, ag
    return "", None

def _start_album_fill_verification(release_mbid: str, result: dict,
                                   artist: str = "") -> None:
    """Confirm a placed album actually reached Navidrome, then flip to `verified`.

    The group-scoped _verify_placement_worker cannot do this job: an artist-page
    download has no review group, so nothing owns the per-track decisions it
    polls. This verifies the placement's *own* per_file rows instead — one cheap
    `search3` per representative track, not a full library page-through, which is
    what rules out _nd_album_index(force=True) on a 30s loop.

    A per_file row identifies its tracklist slot (recording mbid, position,
    title) but carries no artist, so the album's own artist is passed in — a
    bare title search matches far too much on a real library.
    """
    rows = [row for row in (result or {}).get("per_file", []) or []
            if (row.get("status") or "").startswith("matched")
            and (row.get("title") or row.get("recording_mbid"))]
    if not release_mbid or not rows:
        return
    probes = rows[:3]

    def _worker():
        try:
            user = _default_web_user()
            if not user:
                return
            nd_user = user.get("navidrome_user", "")
            nd_pass = user.get("navidrome_password", "")
            deadline = time.time() + ALBUM_FILL_VERIFY_TIMEOUT
            while True:
                seen = 0
                for row in probes:
                    try:
                        if nd_track_present(artist, row.get("title", ""),
                                            row.get("recording_mbid", ""),
                                            nd_user, nd_pass):
                            seen += 1
                    except Exception as e:
                        print(f"  album fill verify: Navidrome check failed: {e}")
                if seen:
                    _album_fill_set(release_mbid, "verified", verifiedTracks=seen)
                    return
                if time.time() >= deadline:
                    break
                time.sleep(ALBUM_FILL_VERIFY_INTERVAL)
            # Placed but never indexed. Not a failure of the fill — say exactly
            # that rather than claiming success or inventing an error.
            _album_fill_set(release_mbid, "placed",
                            reason="Placed, but Navidrome has not indexed it yet")
        except Exception as e:  # noqa: BLE001 — a verifier must never kill the process
            print(f"  album fill verify: {e}")

    threading.Thread(target=_worker, daemon=True,
                     name=f"album-verify-{release_mbid[:8]}").start()

def _running_album_download_task(release_mbid: str) -> str:
    """Id of the album-download task already working on this release, or "".

    Reads the task rows under the lock rather than through `_review_snapshot`,
    which deep-copies the entire multi-MB review state — far too expensive for
    something both the download POST and the status poll call.
    """
    if not release_mbid:
        return ""
    with _review_lock:
        for tid, task in (_review_state.get("tasks") or {}).items():
            if (task.get("kind") == "album-download"
                    and task.get("status") == "running"
                    and task.get("release_mbid") == release_mbid):
                return tid
    return ""

def _album_fill_view(release_mbid: str) -> dict:
    """The wire shape of a fill's progress, for /api/album/status.

    `state` walks: unknown → searching → queued → downloading → placing →
    placed → verified, with needs_match and failed as the two side exits.
    `unknown` means nothing is or was in flight for this release in this
    process, which is the normal state of every album a client merely looked
    at — it must not render as an error.

    Live transfer counts are read here off `pending_album_groups` rather than
    written into the ledger by the download poller: the poller runs every couple
    of seconds per file and must stay free of bookkeeping nobody may be watching.
    """
    entry = _album_fill_get(release_mbid)
    gid, ag = _album_group_for_release(release_mbid)
    view = {
        "releaseMbid": release_mbid,
        "rgid": entry.get("rgid", ""),
        "quality": entry.get("quality", ""),
        "state": entry.get("state") or "unknown",
        "artist": entry.get("artist", ""),
        "album": entry.get("album", ""),
        "done": int(entry.get("done") or 0),
        "total": int(entry.get("total") or 0),
        "failed": int(entry.get("failed") or 0),
        "percent": 0,
        "reason": entry.get("reason", ""),
        "mp3WouldHelp": bool(entry.get("mp3WouldHelp")),
        "groupId": entry.get("groupId", "") or gid,
        "taskId": entry.get("taskId", ""),
        "updatedAt": entry.get("updated_at", 0),
    }
    if ag:
        total = int(ag.get("total") or 0)
        done = int(ag.get("completed") or 0) + int(ag.get("failed") or 0)
        view.update(state="downloading", done=done, total=total,
                    failed=int(ag.get("failed") or 0),
                    artist=view["artist"] or ag.get("artist", ""),
                    album=view["album"] or ag.get("album", ""))
    elif view["state"] == "unknown":
        # No ledger row survived (restart, or the entry aged out) but a search is
        # still running — reporting "unknown" there would hide a live download.
        running = _running_album_download_task(release_mbid)
        if running:
            view.update(state="searching", taskId=running)
    if view["total"]:
        view["percent"] = min(100, int(view["done"] * 100 / view["total"]))
    elif view["state"] in ("placed", "verified"):
        view["percent"] = 100
    return view

def _notify_hub_library_change(release_mbid: str, rgid: str = "",
                               artist: str = "", album: str = "") -> None:
    """Tell the navi-connect hub an album just landed, so it can fan the news out
    to whichever clients are connected.

    Fire-and-forget on its own thread: this is called from the placement path,
    which the user is waiting on, and the hub is optional infrastructure that may
    well be down. Nothing reads the response.
    """
    if not HUB_NOTIFY_URL or not HUB_NOTIFY_TOKEN or not release_mbid:
        return

    def _worker():
        try:
            requests.post(
                f"{HUB_NOTIFY_URL}/lb/notify",
                json={"event": "albumPlaced", "release_mbid": release_mbid,
                      "rgid": rgid, "artist": artist, "album": album},
                headers={"Authorization": f"Bearer {HUB_NOTIFY_TOKEN}"},
                timeout=5)
        except Exception as e:  # noqa: BLE001 — the hub is a nicety, never a dependency
            print(f"  hub notify failed: {e}")

    threading.Thread(target=_worker, daemon=True, name="hub-notify").start()

def _pop_album_groups_for_review_group(review_group_id: str) -> int:
    """Retire album transfer groups (and their per-track records) tied to a
    review group whose tracks were just placed into the library."""
    if not review_group_id:
        return 0
    removed = 0
    for gid, ag in list(pending_album_groups.items()):
        if ag.get("review_group_id") != review_group_id:
            continue
        pending_album_groups.pop(gid, None)
        for key, info in list(pending_downloads.items()):
            if info.get("album_group_id") == gid:
                pending_downloads.pop(key, None)
        removed += 1
    if removed:
        _save_state()
    return removed

def _review_group_from_repair_job(job: dict) -> dict:
    missing = []
    for idx, track in enumerate(job.get("tracks", []) or []):
        missing.append({
            "artist": track.get("artist", job.get("artist", "")),
            "title": track.get("title", ""),
            "mbid": track.get("recording_mbid", ""),
            "position": track.get("position", idx + 1),
            "decision": _review_decision_from_repair_track(track.get("status", "missing")),
            "repair_job_id": job.get("id", ""),
            "repair_track_id": track.get("id", ""),
            "local_path": track.get("local_path", ""),
            "source_user": track.get("source_user", ""),
            "source_folder": track.get("source_folder", ""),
            "download_error": track.get("error", ""),
        })
    return {
        "id": job.get("group_id", job.get("id", "")),
        "group_type": "repair_job",
        "artist": job.get("artist", ""),
        "album": job.get("album", ""),
        "artist_key": _norm_album_text(job.get("artist", "")),
        "album_key": _norm_album_text(job.get("album", "")),
        "created_at": job.get("created_at", time.time()),
        "updated_at": job.get("updated_at", time.time()),
        "status": job.get("status", "needs_review"),
        "repair_job_id": job.get("id", ""),
        "repair_status": job.get("status", ""),
        "merge_mode": "logical",
        "match_mode": "auto",
        "canonical_album_id": job.get("canonical_album_id", ""),
        "canonical_mbid": job.get("canonical_release_mbid", ""),
        "albums": [],
        "missing_tracks": missing,
        "present": 0,
        "total": len(missing),
        "last_action": "repair_job",
        "messages": job.get("messages", []),
    }

def _repair_track_by_id(job: dict, track_id: str) -> dict | None:
    return next((t for t in job.get("tracks", []) if t.get("id") == track_id), None)

def _repair_download_id(job_id: str, username: str, filename: str) -> str:
    return hashlib.sha1(f"{job_id}|{username}|{filename}".encode("utf-8")).hexdigest()[:16]

def _repair_download_for_file(job: dict, username: str, filename: str) -> dict | None:
    did = _repair_download_id(job.get("id", ""), username, filename)
    return next((d for d in job.get("downloads", []) if d.get("id") == did), None)

def _repair_record_download_queued(job_id: str, track_id: str, username: str,
                                   file: dict, source_folder: str = "") -> dict:
    job = _find_repair_job(job_id)
    if not job:
        return {}
    filename = file.get("filename", "")
    now = time.time()
    rec = _repair_download_for_file(job, username, filename)
    if not rec:
        rec = {
            "id": _repair_download_id(job_id, username, filename),
            "job_id": job_id,
            "track_ids": [track_id] if track_id else [],
            "source_user": username,
            "source_folder": source_folder or _folder(filename),
            "files": [file],
            "status": "queued",
            "error": "",
            "timeout_kind": "",
            "retry_available": True,
            "created_at": now,
            "updated_at": now,
        }
        job.setdefault("downloads", []).append(rec)
    else:
        if track_id and track_id not in rec.setdefault("track_ids", []):
            rec["track_ids"].append(track_id)
        rec["status"] = "queued"
        rec["error"] = ""
        rec["updated_at"] = now
    track = _repair_track_by_id(job, track_id)
    if track:
        track["status"] = "queued"
        track["source_user"] = username
        track["source_folder"] = rec.get("source_folder", "")
        track["filename"] = filename
        track["updated_at"] = now
    job["status"] = "downloading"
    _repair_job_touch(job, {"kind": "download_queued", "message": f"Queued {filename}"})
    return rec

def _repair_update_download(job_id: str, username: str, filename: str,
                            status: str, error: str = "", timeout_kind: str = "",
                            local_path: str = "", percent=None, raw_state: str = "") -> dict:
    job = _find_repair_job(job_id)
    if not job:
        return {}
    rec = _repair_download_for_file(job, username, filename)
    if not rec:
        rec = {
            "id": _repair_download_id(job_id, username, filename),
            "job_id": job_id,
            "track_ids": [],
            "source_user": username,
            "source_folder": _folder(filename),
            "files": [{"filename": filename}],
            "status": status,
            "error": "",
            "timeout_kind": "",
            "retry_available": True,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        job.setdefault("downloads", []).append(rec)
    rec["status"] = status
    rec["updated_at"] = time.time()
    rec["error"] = _redact_secrets(error or "")
    rec["timeout_kind"] = timeout_kind or ""
    rec["retry_available"] = status in ("error", "timeout", "cancelled")
    if local_path:
        rec["local_path"] = local_path
        rec.setdefault("files", [{"filename": filename}])[0]["local_path"] = local_path
        pool_path = os.path.dirname(local_path)
        if pool_path and pool_path not in [p.get("path") for p in job.setdefault("source_pools", [])]:
            job["source_pools"].append({
                "id": hashlib.sha1(pool_path.encode("utf-8")).hexdigest()[:16],
                "path": pool_path,
                "status": "downloaded",
                "created_at": time.time(),
            })
    if raw_state:
        rec["last_slskd_state"] = raw_state
    for track_id in rec.get("track_ids", []):
        track = _repair_track_by_id(job, track_id)
        if not track:
            continue
        if status == "complete":
            track["status"] = "downloaded"
            track["local_path"] = local_path or track.get("local_path", "")
        elif status == "downloading":
            track["status"] = "downloading"
        elif status == "error":
            track["status"] = "download_error"
            track["error"] = rec["error"] or raw_state
        elif status == "timeout":
            track["status"] = "download_timeout"
            track["error"] = rec["error"] or timeout_kind
        elif status == "cancelled":
            track["status"] = "cancelled"
            track["error"] = rec["error"] or "cancelled"
        if percent is not None:
            track["download_percent"] = percent
        if raw_state:
            track["download_state"] = raw_state
        track["updated_at"] = time.time()
    job["status"] = _repair_job_status_from_tracks(job)
    _repair_job_touch(job, {"kind": f"download_{status}", "message": error or raw_state or status})
    return rec

def _nd_find_album_folder_by(album_name: str, release_mbid: str, nd_user: str, nd_pass: str) -> str:
    """
    Resolve the on-disk folder for an album by querying Navidrome.
    Searches by album name, then filters by musicBrainzId when available.
    For split/duplicate albums picks the folder holding the most tracks.
    Returns the container-side directory path, or "" if not found.
    """
    if not album_name:
        return ""
    try:
        r = _http.get(f"{NAVIDROME_URL}/rest/search3", params={
            **_nd_auth_params(nd_user, nd_pass),
            "query": album_name,
            "albumCount": "30", "songCount": "0", "artistCount": "0",
        }, timeout=10)
        candidates = (r.json().get("subsonic-response", {})
                               .get("searchResult3", {})
                               .get("album", []))
    except Exception as e:
        print(f"  _nd_find_album_folder_by search error: {e}")
        return ""
    if release_mbid:
        mbid_matches = [a for a in candidates if (a.get("musicBrainzId") or "") == release_mbid]
        if mbid_matches:
            candidates = mbid_matches
    if not candidates:
        return ""
    candidates.sort(key=lambda a: int(a.get("songCount") or 0), reverse=True)
    for album in candidates:
        songs = nd_get_album_tracks(nd_user, nd_pass, album.get("id", ""))
        for song in songs:
            # Navidrome returns library-relative paths over Subsonic; map onto
            # our mount and refuse anything that doesn't resolve to a real
            # directory inside the library, otherwise a raw relative path
            # becomes a phantom folder under the container CWD.
            path = _song_abs_path(song)
            if not path or not _path_in_library(path):
                continue
            folder = os.path.dirname(path)
            if os.path.isdir(folder):
                return folder
    return ""


def _nd_find_album_folder(job: dict, nd_user: str, nd_pass: str) -> str:
    return _nd_find_album_folder_by(job.get("album", ""), job.get("canonical_release_mbid", ""), nd_user, nd_pass)


def _mutagen_write_tags(path: str, tags: dict) -> bool:
    """
    Write canonical tags to a FLAC, Opus or MP3 file using mutagen.
    Returns True on success.

    MP3 is handled through ID3 rather than Vorbis comments. It used to fall
    through to "unsupported format" and return False — and because the merge path
    counted successes without checking them, an MP3 album reported "tagged 0/12
    file(s)", a success, and a Navidrome rescan, while nothing had changed.
    """
    VORBIS_MAP = {
        "title": "title",
        "track": "tracknumber",
        "tracktotal": "tracktotal",
        "disc": "discnumber",
        "disctotal": "disctotal",
        "album": "album",
        "albumartist": "albumartist",
        "artist": "artist",
        "mb_albumid": "musicbrainz_albumid",
        "mb_releasegroupid": "musicbrainz_releasegroupid",
        "mb_trackid": "musicbrainz_trackid",
        "year": "date",
    }
    ext = _file_ext(path)
    if ext == MP3_FALLBACK_EXT:
        return _id3_write_tags(path, tags)
    try:
        if ext == "flac":
            from mutagen.flac import FLAC
            audio = FLAC(path)
        elif ext == "opus":
            from mutagen.oggopus import OggOpus
            audio = OggOpus(path)
        else:
            print(f"  _mutagen_write_tags: unsupported format {ext!r} for {path}")
            return False
    except Exception as e:
        print(f"  _mutagen_write_tags open error {path}: {e}")
        return False
    for our_key, vorbis_key in VORBIS_MAP.items():
        value = tags.get(our_key, "")
        if value not in (None, ""):
            audio[vorbis_key] = [str(value)]
    try:
        audio.save()
        return True
    except Exception as e:
        print(f"  _mutagen_write_tags save error {path}: {e}")
        return False


def _id3_write_tags(path: str, tags: dict) -> bool:
    """The MP3 half of `_mutagen_write_tags`.

    ID3 has no native frame for MusicBrainz ids; the convention Picard, beets and
    Navidrome all read is a TXXX frame with a specific description, plus UFID for
    the recording id. Track/disc numbers are "n/total" in one frame.
    """
    try:
        from mutagen.id3 import (ID3, ID3NoHeaderError, TIT2, TALB, TPE1, TPE2,
                                 TRCK, TPOS, TDRC, TXXX)
    except Exception as e:
        print(f"  _id3_write_tags: mutagen.id3 unavailable: {e}")
        return False
    try:
        try:
            audio = ID3(path)
        except ID3NoHeaderError:
            audio = ID3()
    except Exception as e:
        print(f"  _id3_write_tags open error {path}: {e}")
        return False

    def _pair(value, total):
        value, total = str(value or ""), str(total or "")
        if not value:
            return ""
        return f"{value}/{total}" if total else value

    frames = [
        (TIT2, tags.get("title", "")),
        (TALB, tags.get("album", "")),
        (TPE1, tags.get("artist", "")),
        (TPE2, tags.get("albumartist", "")),
        (TRCK, _pair(tags.get("track"), tags.get("tracktotal"))),
        (TPOS, _pair(tags.get("disc"), tags.get("disctotal"))),
        (TDRC, tags.get("year", "")),
    ]
    for frame, value in frames:
        if value not in (None, ""):
            audio.setall(frame.__name__, [frame(encoding=3, text=[str(value)])])
    for desc, key in (("MusicBrainz Album Id", "mb_albumid"),
                      ("MusicBrainz Release Group Id", "mb_releasegroupid"),
                      ("MusicBrainz Release Track Id", "mb_trackid")):
        value = tags.get(key, "")
        if value:
            audio.add(TXXX(encoding=3, desc=desc, text=[str(value)]))
    try:
        # v2.3 as well as v2.4: some players (and older Navidrome builds) only
        # read 2.3, and writing both costs nothing.
        audio.save(path, v2_version=3)
        return True
    except Exception as e:
        print(f"  _id3_write_tags save error {path}: {e}")
        return False


def _task_create(kind: str, label: str, total: int = 0, **extra) -> str:
    """`extra` is stamped onto the task row — `group_id` is what lets the album
    screen find the search running for the album being looked at, instead of the
    user having to guess whether anything is happening at all."""
    task_id = uuid.uuid4().hex[:12]
    now = time.time()
    with _review_lock:
        tasks = _review_state.setdefault("tasks", {})
        tasks[task_id] = {
            "id": task_id,
            "kind": kind,
            "label": label,
            "status": "running",
            "started_at": now,
            "finished_at": 0,
            "done": 0,
            "total": total,
            "percent": 0,
            "current": "",
            "summary": "",
            "error": "",
            "cancellable": False,
        }
        tasks[task_id].update(extra)
        # Keep the task list compact enough for the appdata JSON to stay tidy.
        if len(tasks) > 200:
            old = sorted(tasks.values(), key=lambda t: t.get("started_at", 0))[:-200]
            for item in old:
                tasks.pop(item["id"], None)
    _save_review_state()
    return task_id

_task_persist_last: dict = {}
TASK_PERSIST_INT = 5.0

def _task_update(task_id: str, persist: bool = True, **updates) -> None:
    """Update a task; `persist=False` keeps it in memory between flushes.

    _save_review_state deep-copies and rewrites the whole review file, so a
    per-album progress tick that persisted was costing a full serialization of
    every group found so far — thousands of them across one scan. /api/tasks
    reads the in-memory state, so throttling the write costs nothing visible;
    the only thing lost is progress granularity if the process dies mid-scan,
    which is meaningless anyway since the scan does not resume.
    """
    with _review_lock:
        task = _review_state.setdefault("tasks", {}).get(task_id)
        if not task:
            return
        task.update(updates)
        total = int(task.get("total") or 0)
        done = int(task.get("done") or 0)
        if task.get("status") in ("complete", "error", "cancelled") and not task.get("finished_at"):
            task["finished_at"] = time.time()
        if task.get("status") in ("complete", "error", "cancelled"):
            task["percent"] = 100
        else:
            task["percent"] = int(min(100, max(0, done * 100 / total))) if total else 0
        terminal = task.get("status") in ("complete", "error", "cancelled")
    if not persist and not terminal:
        # Still flush occasionally so a crashed process leaves a plausible
        # last-known progress behind, and so the file's mtime shows life.
        now = time.time()
        if now - _task_persist_last.get(task_id, 0.0) < TASK_PERSIST_INT:
            return
        _task_persist_last[task_id] = now
    else:
        _task_persist_last[task_id] = time.time()
    _save_review_state()

def _task_finish(task_id: str, summary: str = "", error: str = "", **extra) -> None:
    _task_update(task_id, status="error" if error else "complete",
                 summary=summary, error=error, percent=100, **extra)

def _operation_create(kind: str, message: str = "", job_id: str = "",
                      status: str = "running", **extra) -> dict:
    op_id = _uid("op")
    now = time.time()
    op = {
        "id": op_id,
        "job_id": job_id or "",
        "kind": kind,
        "status": status,
        "message": message or kind.replace("_", " "),
        "error": "",
        "created_at": now,
        "updated_at": now,
    }
    op.update(extra)
    with _review_lock:
        ops = _review_state.setdefault("operations", {})
        ops[op_id] = op
        if len(ops) > 300:
            old = sorted(ops.values(), key=lambda o: o.get("created_at", 0))[:-300]
            for item in old:
                ops.pop(item.get("id", ""), None)
    return op

def _operation_update(operation_id: str, status: str = None,
                      message: str = None, error: str = None, **extra) -> dict:
    with _review_lock:
        op = _review_state.setdefault("operations", {}).get(operation_id)
        if not op:
            return {}
        if status:
            op["status"] = status
        if message is not None:
            op["message"] = _redact_secrets(message)
        if error is not None:
            op["error"] = _redact_secrets(error)
        op.update(extra)
        op["updated_at"] = time.time()
        return json.loads(json.dumps(op))

def _operation_finish(operation_id: str, ok: bool,
                      message: str = "", error: str = "", **extra) -> dict:
    return _operation_update(
        operation_id,
        status="success" if ok else "error",
        message=message or ("Done" if ok else "Action failed"),
        error="" if ok else (error or message or "Action failed"),
        **extra)

def _operation_snapshot() -> dict:
    with _review_lock:
        return json.loads(json.dumps(_review_state.setdefault("operations", {})))

def _find_operation(operation_id: str) -> dict | None:
    with _review_lock:
        op = _review_state.setdefault("operations", {}).get(operation_id)
        return json.loads(json.dumps(op)) if op else None

def _with_operation(payload: dict, operation: dict) -> dict:
    payload = dict(payload or {})
    op_id = operation.get("id", "") if operation else ""
    latest = _find_operation(op_id) if op_id else operation
    if latest:
        payload["operation_id"] = latest.get("id", "")
        payload["operation"] = latest
        payload.setdefault("job_id", latest.get("job_id", ""))
        payload.setdefault("message", latest.get("message", ""))
    return payload

def _task_run(kind: str, label: str, target, total: int = 0, **extra) -> str:
    task_id = _task_create(kind, label, total, **extra)

    def _runner():
        try:
            target(task_id)
        except Exception as e:
            _web_log(f"task {kind} failed: {e}")
            _task_finish(task_id, error=str(e))

    threading.Thread(target=_runner, daemon=True, name=f"web-task-{kind}").start()
    return task_id

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _file_ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

_APPS: list = []   # populated in main(); lets command handlers reach every bot

def _progress_bar(done: int, total: int, width: int = 10) -> str:
    """Render a simple text progress bar like [█████·····]."""
    if total <= 0:
        return "[" + "·" * width + "]"
    filled = int(width * min(done, total) / total)
    return "[" + "█" * filled + "·" * (width - filled) + "]"

def _folder_quality(files: list) -> str:
    """
    Derive quality string matching Tubifarry's FormatQualityInfo:
    - Most common extension among accepted formats -> codec
    - Most common bitRate, bitDepth, sampleRate across files
    - "FLAC 24bit/96kHz", "MP3 320kbps", "OPUS", etc.
    """
    if not files:
        return "?"
    exts = [_file_ext(f.get("filename", "")) for f in files
            if _file_ext(f.get("filename", "")) in _accepted_formats()]
    if not exts:
        return "?"
    codec = max(set(exts), key=exts.count).upper()

    bitrates = [f.get("bitRate") or f.get("bitrate") for f in files]
    bitrates = [b for b in bitrates if b]
    bitrate  = max(set(bitrates), key=bitrates.count) if bitrates else None

    depths  = [f.get("bitDepth") for f in files if f.get("bitDepth")]
    samples = [f.get("sampleRate") for f in files if f.get("sampleRate")]
    depth   = max(set(depths),  key=depths.count)  if depths  else None
    sample  = max(set(samples), key=samples.count) if samples else None

    if codec == "MP3" and bitrate:
        return f"MP3 {bitrate}kbps"
    if depth and sample:
        return f"{codec} {depth}bit/{sample // 1000}kHz"
    if bitrate:
        return f"{codec} {bitrate}kbps"
    return codec

def _format_info(file: dict) -> str:
    ext     = _file_ext(file.get("filename", "")).upper() or "?"
    bitrate = file.get("bitRate") or file.get("bitrate")
    size_mb = round(file.get("size", 0) / 1024 / 1024, 1)
    parts   = [ext]
    if bitrate:
        parts.append(f"{bitrate} kbps")
    if size_mb:
        parts.append(f"{size_mb} MB")
    return ", ".join(parts)

def _folder(filename: str) -> str:
    f = filename.replace("\\", "/")
    return f.rsplit("/", 1)[0] if "/" in f else ""

def _user_for_token(token: str):
    return next((u for u in USERS if u["telegram_token"] == token), None)

def _resolve_token(context) -> str:
    token = next(
        (u["telegram_token"] for u in USERS
         if u["telegram_token"].split(":")[0] == str(context.bot.id)),
        None,
    )
    return token or USERS[0]["telegram_token"]

# ---------------------------------------------------------------------------
# MusicBrainz
# ---------------------------------------------------------------------------

MBZ_API       = "https://musicbrainz.org/ws/2"
_mbz_last_req = 0.0
_mbz_cache: dict = {}          # "path?sortedparams" -> response json
MBZ_CACHE_MAX = 8000           # in-memory cap
# A missing/invalid mbid is a permanent answer and is cached as {}. Anything
# else (503, timeout) is temporary and only earns a cooldown, never a cache
# entry — see mbz_get.
MBZ_PERMANENT_FAIL_STATUSES = {400, 404, 410}
MBZ_FAIL_COOLDOWN = 300.0
# Backoff between placement's tracklist retries. Deliberately far shorter than
# MBZ_FAIL_COOLDOWN: a placement is holding already-downloaded files hostage, so
# waiting out the full scan-grade cooldown is worse than spending three requests.
MBZ_PLACEMENT_RETRY_BACKOFF = (5.0, 20.0)
_mbz_fail_until: dict = {}     # key -> retry-after timestamp (not persisted)

def _mbz_cache_put(key: str, data: dict) -> None:
    if len(_mbz_cache) >= MBZ_CACHE_MAX:
        _mbz_cache.pop(next(iter(_mbz_cache)))   # drop oldest insertion
    _mbz_cache[key] = data
# The web UI runs Flask with threaded=True, so request threads and background
# scan/index tasks call mbz_get concurrently. Without this the 1/sec pacing was
# advisory: every thread read _mbz_last_req, computed the same "no wait needed",
# and fired together -- MusicBrainz answered 503, and since 503s are not
# durably cached, the caller retried forever. An album page opened
# during an index build could sit on a skeleton indefinitely. Held across the
# sleep and the request so the budget is actually serialized; cache hits never
# take it.
_mbz_lock = threading.Lock()

def _mbz_cache_key(path: str, params: dict) -> str:
    items = sorted((params or {}).items())
    return path + "?" + "&".join(f"{k}={v}" for k, v in items)

def mbz_get(path: str, params: dict = None) -> dict:
    """
    Rate-limited (1/sec) MusicBrainz request with a persistent result cache.

    Entity lookups (recording/{id}, release/{id}, release-group/{id}) are
    effectively immutable, so a cache hit skips both the network call and the
    1-second rate-limit sleep — which is what makes repeat scans fast.

    Failures are cached too, but differently by kind. A 400/404/410 means the
    mbid is wrong or gone — a permanent answer, cached like any other, so a
    library carrying stale tags stops re-burning a second per bad tag on every
    scan forever. A 503/timeout means MusicBrainz is busy, which is temporary:
    those get a short cooldown in _mbz_fail_until and are never written to the
    durable cache, so a bad afternoon can't poison it.
    """
    global _mbz_last_req
    key    = _mbz_cache_key(path, params)
    cached = _mbz_cache.get(key)
    if cached is not None:
        return cached
    if time.time() < _mbz_fail_until.get(key, 0):
        return {}
    with _mbz_lock:
        # Re-check: a thread that queued behind an identical request should use
        # its answer rather than spend another second of the budget on it.
        cached = _mbz_cache.get(key)
        if cached is not None:
            return cached
        if time.time() < _mbz_fail_until.get(key, 0):
            return {}
        wait = 1.0 - (time.time() - _mbz_last_req)
        if wait > 0:
            time.sleep(wait)
        try:
            r = _http.get(
                f"{MBZ_API}/{path}",
                params={"fmt": "json", **(params or {})},
                headers={"User-Agent": f"listenbrainz-bot/1.0 ({MBZ_CONTACT})"},
                timeout=10,
            )
            _mbz_last_req = time.time()
            status = r.status_code
            if status in MBZ_PERMANENT_FAIL_STATUSES:
                print(f"  MBZ {status} [{path}] — caching as unresolvable")
                _mbz_cache_put(key, {})
                return {}
            r.raise_for_status()
            data = r.json()
            if data:
                _mbz_cache_put(key, data)
            return data
        except Exception as e:
            print(f"  MBZ error [{path}]: {e}")
            _mbz_fail_until[key] = time.time() + MBZ_FAIL_COOLDOWN
            return {}

def mbz_best_release(recording_mbid: str) -> dict:
    """
    Given a recording MBID, return the best official non-compilation album release.
    Prefers: official status > album primary type > earliest date.
    """
    data     = mbz_get(f"recording/{recording_mbid}", {"inc": "releases release-groups"})
    releases = data.get("releases", [])

    def _score(rel):
        rg        = rel.get("release-group") or {}
        rg_type   = (rg.get("primary-type") or "").lower()
        sec_raw   = rg.get("secondary-types") or []
        sec_types = [(t if isinstance(t, str) else (t.get("name") or "")).lower()
                     for t in sec_raw]
        status   = (rel.get("status") or "").lower()
        is_comp  = 1 if ("compilation" in sec_types or "various" in rg_type) else 0
        type_prio = 0 if rg_type == "album" else (1 if rg_type == "single" else 2)
        stat_prio = 0 if status == "official" else 1
        date      = rel.get("date") or "9999"
        return (is_comp, type_prio, stat_prio, date)

    releases.sort(key=_score)
    return releases[0] if releases else {}

def mbz_release_tracks_insisting(release_mbid: str, attempts: int = 3) -> list:
    """`mbz_release_tracks`, but not willing to take a transient no for an answer.

    A 503 or a timeout parks that exact request in `_mbz_fail_until` for
    MBZ_FAIL_COOLDOWN (five minutes), and every call inside that window returns
    {} *without even trying*. That is the right policy for a scan — burning the
    rate-limit budget on a service that is currently unhappy helps nobody — and
    exactly the wrong one for placement, which happens once, has already
    downloaded the files, and refuses to write anything without a tracklist to
    tag against. The result was an album that downloaded fine, failed to place,
    reported `failed` to every client, and then got placed by a later sweep with
    nothing left watching to notice.

    So: clear the cooldown for this one key and ask again, a couple of times,
    with a short backoff. A genuine 404 is cached durably by mbz_get and returns
    the same empty answer on every attempt, so a bad MBID still fails fast — it
    just costs two extra requests to find that out, once, at placement time.
    """
    key = _mbz_cache_key(f"release/{release_mbid}", {"inc": "recordings"})
    for attempt in range(max(1, attempts)):
        tracks = mbz_release_tracks(release_mbid)
        if tracks:
            return tracks
        if attempt == attempts - 1:
            break
        _mbz_fail_until.pop(key, None)
        delay = MBZ_PLACEMENT_RETRY_BACKOFF[min(attempt, len(MBZ_PLACEMENT_RETRY_BACKOFF) - 1)]
        print(f"  MBZ: no tracklist for {release_mbid}, retrying in {delay}s")
        time.sleep(delay)
    return []

def mbz_release_tracks(release_mbid: str) -> list:
    """Return [{title, mbid, position}] for all tracks in a release."""
    data   = mbz_get(f"release/{release_mbid}", {"inc": "recordings"})
    tracks = []
    for medium in data.get("media", []):
        for t in medium.get("tracks", []):
            rec = t.get("recording") or {}
            tracks.append({
                "title":    t.get("title") or rec.get("title") or "",
                "mbid":     rec.get("id", ""),
                "position": t.get("position", 0),
                "duration": float(t.get("length") or rec.get("length") or 0) / 1000.0,
            })
    return tracks

def mbz_release_display(release_mbid: str) -> dict:
    """Return compact release details useful for picking duplicate albums."""
    if not release_mbid:
        return {}
    data = mbz_get(f"release/{release_mbid}", {"inc": "release-groups media"})
    rg = data.get("release-group") or {}
    formats = []
    for medium in data.get("media", []):
        fmt = medium.get("format") or ""
        if fmt and fmt not in formats:
            formats.append(fmt)
    secondary = rg.get("secondary-types") or []
    release_type = rg.get("primary-type", "")
    if secondary:
        release_type = " / ".join([release_type] + secondary) if release_type else " / ".join(secondary)
    return {
        "release_title": data.get("title", ""),
        "release_format": ", ".join(formats),
        "release_type": release_type,
        "release_date": data.get("date", ""),
        "release_country": data.get("country", ""),
        "release_status": data.get("status", ""),
        "release_packaging": data.get("packaging", ""),
        "cover_url": f"https://coverartarchive.org/release/{release_mbid}/front-250",
        "release_detail_loaded": True,
    }

def _artist_credit_str(ac_list) -> str:
    """Flatten a MusicBrainz artist-credit array into a display string."""
    parts = []
    for ac in ac_list or []:
        if isinstance(ac, str):
            parts.append(ac)
            continue
        parts.append(ac.get("name") or (ac.get("artist") or {}).get("name", ""))
        parts.append(ac.get("joinphrase", "") or "")
    return "".join(parts).strip()

def mbz_search_release_groups(query: str, limit: int = 5) -> list:
    """
    Free-text search of MusicBrainz release-groups (albums).
    Returns [{rgid, title, artist, primary_type, year, score}] best-first.
    """
    data = mbz_get("release-group", {"query": query, "limit": str(limit)})
    out  = []
    for rg in data.get("release-groups", []):
        out.append({
            "rgid":         rg.get("id", ""),
            "title":        rg.get("title", ""),
            "artist":       _artist_credit_str(rg.get("artist-credit")) or "?",
            "primary_type": (rg.get("primary-type") or "").lower(),
            "year":         (rg.get("first-release-date") or "")[:4],
            "score":        int(rg.get("score", 0) or 0),
        })
    return out

def mbz_resolve_album(rgid: str) -> dict:
    """
    For a release-group, pick the best concrete release (official, earliest)
    and return {rgid, release_mbid, title, artist, year, total_tracks}.
    Costs 2 MusicBrainz calls (release-group + release tracklist).
    """
    data     = mbz_get(f"release-group/{rgid}", {"inc": "releases artist-credits"})
    releases = data.get("releases", [])

    def _score(rel):
        status    = (rel.get("status") or "").lower()
        stat_prio = 0 if status == "official" else 1
        return (stat_prio, rel.get("date") or "9999")

    releases.sort(key=_score)
    best     = releases[0] if releases else {}
    rel_mbid = best.get("id", "")
    year     = (best.get("date") or data.get("first-release-date") or "")[:4]
    total    = len(mbz_release_tracks(rel_mbid)) if rel_mbid else 0
    return {
        "rgid":         rgid,
        "release_mbid": rel_mbid,
        "title":        data.get("title", ""),
        "artist":       _artist_credit_str(data.get("artist-credit")) or "?",
        "year":         year,
        "total_tracks": total,
    }

def caa_front_url(rgid: str, size: int = 500) -> str:
    """Cover Art Archive front-cover thumbnail URL for a release-group."""
    return f"https://coverartarchive.org/release-group/{rgid}/front-{size}"

def caa_release_front_url(release_mbid: str, size: int = 500) -> str:
    """Front-cover URL for one concrete *release*.

    A release-group's art is whichever release the Archive picked, which is how
    a vinyl pressing ends up showing a photograph of the disc where the album
    sleeve belongs. Keyed at the release level, each edition shows its own
    sleeve — and the caller can fall back to the release-group URL when a
    particular pressing has no art of its own."""
    return f"https://coverartarchive.org/release/{release_mbid}/front-{size}"

def mbz_search_artists(query: str, limit: int = 8) -> list:
    """
    Free-text search of MusicBrainz artists.
    Returns [{mbid, name, disambiguation, type, country, area, score}] best-first.
    """
    data = mbz_get("artist", {"query": query, "limit": str(limit)})
    out  = []
    for a in data.get("artists", []):
        out.append({
            "mbid":           a.get("id", ""),
            "name":           a.get("name", ""),
            "disambiguation": a.get("disambiguation", ""),
            "type":           a.get("type", ""),
            "country":        a.get("country", ""),
            "area":           (a.get("area") or {}).get("name", ""),
            "score":          int(a.get("score", 0) or 0),
        })
    return out

# Release-group secondary types excluded from discography browsing by default —
# these aren't "albums" in the sense a user browsing a discography expects.
#
# "compilation" is deliberately *not* here. A greatest-hits set is a real part
# of a discography and the Artist screen gives it its own section; excluding it
# meant an artist's best-known release simply did not exist in the UI.
ARTIST_DISCOGRAPHY_EXCLUDE_DEFAULT = {
    "live", "remix", "dj-mix", "mixtape/street",
    "interview", "audiobook", "spokenword",
}

# Secondary types that describe what the release *is*, rather than annotating an
# album — these become the release's displayed type, so the Artist screen can
# group them without a lookup table of its own. Everything else keeps the
# primary type ("Album" for a live album that slipped past the exclusions).
_TYPE_DEFINING_SECONDARY = ("compilation", "soundtrack", "live", "remix", "demo")

def _effective_release_type(primary: str, secondary: list) -> str:
    """The type label the UI groups by: a type-defining secondary type when the
    release has one, else the primary type. Never collapses to "album" — the
    frontend groups on this string as-is."""
    sec = {(s or "").lower() for s in (secondary or [])}
    for candidate in _TYPE_DEFINING_SECONDARY:
        if candidate in sec:
            return candidate
    return (primary or "").lower()

def mbz_artist_release_groups(artist_mbid: str, *,
                              types: tuple = ("album", "ep", "single"),
                              exclude_secondary: set = None,
                              limit_pages: int = 10) -> list:
    """
    Paginated browse of an artist's release-groups (release-group?artist=...).
    Release-groups already dedupe regional/format pressings of the same work, so
    no separate "editions" dedup is needed here (mbz_resolve_album handles that
    per-release-group, later, only for the release the user actually acts on).

    Filters out any release-group whose secondary types intersect
    exclude_secondary (default: ARTIST_DISCOGRAPHY_EXCLUDE_DEFAULT; pass an empty
    set to disable filtering). limit_pages bounds MusicBrainz call volume for
    unusually prolific artists (limit_pages * 100 release-groups, worst case).

    Returns [{rgid, title, primary_type, secondary_types, year, first_release_date}]
    sorted by first_release_date.
    """
    exclude = ARTIST_DISCOGRAPHY_EXCLUDE_DEFAULT if exclude_secondary is None else exclude_secondary
    out, offset, page_size = [], 0, 100
    for _ in range(max(1, limit_pages)):
        data = mbz_get("release-group", {
            "artist": artist_mbid,
            "type":   "|".join(types),
            "limit":  str(page_size),
            "offset": str(offset),
        })
        rgs = data.get("release-groups", [])
        if not rgs:
            break
        for rg in rgs:
            sec_raw = rg.get("secondary-types") or []
            sec     = [(t if isinstance(t, str) else (t.get("name") or "")).lower()
                       for t in sec_raw]
            if exclude and any(s in exclude for s in sec):
                continue
            out.append({
                "rgid":               rg.get("id", ""),
                "title":              rg.get("title", ""),
                "primary_type":       (rg.get("primary-type") or "").lower(),
                "secondary_types":    sec,
                "year":               (rg.get("first-release-date") or "")[:4],
                "first_release_date": rg.get("first-release-date") or "",
            })
        total = int(data.get("release-group-count", 0) or 0)
        offset += page_size
        if offset >= total or len(rgs) < page_size:
            break
    out.sort(key=lambda r: (r["first_release_date"] or "9999", r["title"].lower()))
    return out

_spotify_token: dict = {"access_token": None, "expires_at": 0.0}

def _spotify_auth_header() -> dict:
    """Return a valid Bearer token header, refreshing if expired."""
    import base64
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        raise RuntimeError("SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET not configured")
    if time.time() >= _spotify_token["expires_at"] - 30:
        creds = base64.b64encode(
            f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
        r = _http.post("https://accounts.spotify.com/api/token",
                          headers={"Authorization": f"Basic {creds}"},
                          data={"grant_type": "client_credentials"}, timeout=10)
        r.raise_for_status()
        data = r.json()
        _spotify_token["access_token"] = data["access_token"]
        _spotify_token["expires_at"]   = time.time() + data["expires_in"]
    return {"Authorization": f"Bearer {_spotify_token['access_token']}"}

def spotify_playlist_id(url_or_id: str) -> str:
    """Extract the playlist ID from a Spotify URL or return the raw ID."""
    import re
    m = re.search(r"playlist/([A-Za-z0-9]+)", url_or_id)
    return m.group(1) if m else url_or_id.strip()

class SpotifyError(Exception):
    """Spotify API returned an error (often an account/policy issue)."""

def spotify_get_playlist_tracks(playlist_id: str) -> list:
    """
    Fetch all tracks from a Spotify playlist using Client Credentials.
    Returns a list of track dicts: {artist, title, album, mbid}
    MBID is looked up via MusicBrainz using artist+title for best matching.
    Only works for public playlists (added to owner's profile).
    Raises SpotifyError with the API's reason on a hard failure (e.g. 403).
    """
    tracks  = []
    url     = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks"
    params  = {"limit": 100, "fields":
               "next,items(track(name,artists,album(name),external_ids))"}
    headers = _spotify_auth_header()

    while url:
        r = _http.get(url, headers=headers, params=params, timeout=10)
        if not r.ok:
            reason = ""
            try:
                reason = r.json().get("error", {}).get("message", "")
            except Exception:
                reason = r.text[:200]
            print(f"  Spotify playlist fetch failed ({r.status_code}): {reason}")
            # On the very first page, a failure means we got nothing — surface it.
            if not tracks:
                raise SpotifyError(f"{r.status_code}: {reason}")
            break
        data = r.json()
        for item in data.get("items", []):
            track = item.get("track")
            if not track or track.get("type") == "episode":
                continue
            artist = ", ".join(a["name"] for a in track.get("artists", []))
            title  = track.get("name", "")
            album  = track.get("album", {}).get("name", "")
            # Spotify sometimes provides ISRC; we use MBZ to get recording MBID
            isrc   = track.get("external_ids", {}).get("isrc", "")
            mbid   = _mbid_from_isrc(isrc) if isrc else ""
            if not mbid:
                mbid = _mbid_from_search(artist, title)
            tracks.append({
                "artist":   artist,
                "title":    title,
                "album":    album,
                "mbid":     mbid,
                "playlist": "Spotify",
            })
        url    = data.get("next")
        params = {}  # next URL already has params baked in
        headers = _spotify_auth_header()  # refresh if needed

    print(f"  Spotify: fetched {len(tracks)} tracks from playlist {playlist_id}")
    return tracks

def _mbid_from_isrc(isrc: str) -> str:
    """Look up a MusicBrainz recording MBID by ISRC (most reliable)."""
    data = mbz_get("recording", {"isrcs": isrc, "inc": ""})
    recs = data.get("recordings", [])
    return recs[0]["id"] if recs else ""

def _mbid_from_search(artist: str, title: str) -> str:
    """Fall back to MusicBrainz text search for a recording MBID."""
    data = mbz_get("recording",
                   {"query": f'recording:"{title}" AND artist:"{artist}"', "limit": 1})
    recs = data.get("recordings", [])
    return recs[0]["id"] if recs else ""

# ---------------------------------------------------------------------------
# ListenBrainz
# ---------------------------------------------------------------------------

LBZ_EXT_KEY  = "https://musicbrainz.org/doc/jspf#playlist"
LBZ_ENDPOINT = "https://api.listenbrainz.org/1"

class LBZError(Exception):
    """ListenBrainz was unreachable / returned an error after retries."""

def _lbz_get(path: str, timeout: int = 25, retries: int = 3) -> dict:
    """
    GET a ListenBrainz endpoint with retries + backoff. Raises LBZError if all
    attempts fail, so callers can tell 'API down' apart from 'no data'.
    Uses a (connect, read) timeout tuple and identifies the client, which some
    hosts require before they will respond.
    """
    last = None
    headers = {"User-Agent": f"listenbrainz-bot/1.0 ({MBZ_CONTACT})"}
    for attempt in range(retries):
        try:
            r = _http.get(f"{LBZ_ENDPOINT}/{path}",
                             headers=headers, timeout=(10, timeout))
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            print(f"  LBZ request failed ({path}) "
                  f"attempt {attempt + 1}/{retries}: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)   # 1s, 2s
    raise LBZError(f"{path}: {last}")

def _source_patch(pl: dict) -> str:
    try:
        return (pl.get("extension", {})
                  .get(LBZ_EXT_KEY, {})
                  .get("additional_metadata", {})
                  .get("algorithm_metadata", {})
                  .get("source_patch", ""))
    except Exception:
        return ""

def lbz_get_playlist_ids(lbz_user: str, sources: dict) -> dict:
    """
    Resolve configured source_patches to playlist UUIDs.
    Raises LBZError if the ListenBrainz request itself fails (vs. returning an
    empty dict only when the request succeeded but no patches matched).
    """
    found = {}
    data  = _lbz_get(f"user/{lbz_user}/playlists/createdfor")  # raises on failure
    for wrap in data.get("playlists", []):
        pl    = wrap.get("playlist", {})
        patch = _source_patch(pl)
        title = pl.get("title", "(no title)")
        print(f"  LBZ: '{title}' [source_patch={patch!r}]")
        if patch in sources and sources[patch] not in found:
            uuid = pl["identifier"].rstrip("/").split("/")[-1]
            found[sources[patch]] = uuid
            print(f"    -> {sources[patch]} (uuid={uuid})")
    for patch, name in sources.items():
        if name not in found:
            print(f"  Warning: source_patch='{patch}' not found for {lbz_user}")
    return found

def lbz_get_tracks(uuid: str, playlist_name: str) -> list:
    """Fetch tracks for a playlist. Raises LBZError if the request fails."""
    pl = _lbz_get(f"playlist/{uuid}").get("playlist", {})   # raises on failure

    def _mbid(t):
        for ident in t.get("identifier", []):
            parts = ident.rstrip("/").split("/")
            if len(parts) >= 2 and len(parts[-1]) == 36:
                return parts[-1]
        return ""

    tracks = [
        {"artist": t.get("creator", ""), "title": t.get("title", ""),
         "mbid": _mbid(t), "playlist": playlist_name}
        for t in pl.get("track", [])
    ]
    print(f"  {len(tracks)} tracks in '{playlist_name}'.")
    return tracks

_FRESH_CACHE = {"ts": 0.0, "days": 0, "rows": []}
_FRESH_TTL = 3600   # the fresh-releases feed moves slowly

# ── Similar artists ─────────────────────────────────────────────────────────
# Two independent sources, because each alone has a characteristic blind spot:
# ListenBrainz's session-based similarity is strong on what people actually
# listen to together but thin for artists with little listening data, and
# Last.fm's is broad but noisier. An artist both agree on is ranked first.
#
# Last.fm is optional: without LASTFM_API_KEY the ListenBrainz half runs alone.
LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY", "")
LBZ_LABS_ENDPOINT = "https://labs.api.listenbrainz.org"
# The published default algorithm for the similar-artists dataset. Kept as a
# constant because it is part of the query, not a tuning knob to guess at.
_LBZ_SIMILAR_ALGORITHM = ("session_based_days_7500_session_300"
                          "_contribution_5_threshold_10_limit_100"
                          "_filter_True_skip_30")
_SIMILAR_TTL = 24 * 3600   # artist similarity is stable; don't re-ask per view
_similar_cache: dict = {}  # artist_mbid -> (ts, [{mbid, name, score, sources}])

def _lbz_similar_artists(artist_mbid: str) -> list:
    """[{mbid, name, score}] from the ListenBrainz labs similar-artists set."""
    r = _http.get(f"{LBZ_LABS_ENDPOINT}/similar-artists/json",
                  params={"artist_mbids": artist_mbid,
                          "algorithm": _LBZ_SIMILAR_ALGORITHM},
                  headers={"User-Agent": f"listenbrainz-bot/1.0 ({MBZ_CONTACT})"},
                  timeout=(10, 20))
    r.raise_for_status()
    data = r.json()
    # The labs endpoints have returned both a bare list and a {"data": [...]}
    # envelope over their lifetime; accept either rather than break on a shape.
    rows = data if isinstance(data, list) else (data.get("data") or [])
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mbid = row.get("artist_mbid") or row.get("mbid") or ""
        name = row.get("name") or row.get("artist_name") or ""
        if not mbid and not name:
            continue
        out.append({"mbid": mbid, "name": name,
                    "score": float(row.get("score") or 0)})
    return out

def _lastfm_similar_artists(artist_mbid: str, artist_name: str) -> list:
    """[{mbid, name, score}] from Last.fm, or [] when no key is configured."""
    if not LASTFM_API_KEY:
        return []
    params = {"method": "artist.getsimilar", "api_key": LASTFM_API_KEY,
              "format": "json", "limit": "50", "autocorrect": "1"}
    # mbid is the precise lookup; the name is the fallback for artists Last.fm
    # has under a different (or no) MBID.
    if artist_mbid:
        params["mbid"] = artist_mbid
    else:
        params["artist"] = artist_name
    r = _http.get("https://ws.audioscrobbler.com/2.0/", params=params, timeout=(10, 20))
    r.raise_for_status()
    rows = ((r.json().get("similarartists") or {}).get("artist") or [])
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append({"mbid": row.get("mbid") or "",
                    "name": row.get("name") or "",
                    "score": float(row.get("match") or 0)})
    return out

def similar_artists(artist_mbid: str, artist_name: str = "", limit: int = 20) -> list:
    """Merged, cached similar artists: [{mbid, name, score, sources[]}].

    Neither source is fatal. If both fail the answer is an empty list — a shelf
    that isn't shown is a better outcome than an album page that errors.
    """
    key = (artist_mbid or artist_name or "").lower()
    if not key:
        return []
    hit = _similar_cache.get(key)
    if hit and time.time() - hit[0] < _SIMILAR_TTL:
        return hit[1][:limit]

    merged: dict = {}
    for source, fetch in (("listenbrainz", lambda: _lbz_similar_artists(artist_mbid)),
                          ("lastfm", lambda: _lastfm_similar_artists(artist_mbid, artist_name))):
        if source == "listenbrainz" and not artist_mbid:
            continue
        try:
            rows = fetch()
        except Exception as e:
            print(f"  similar artists: {source} lookup failed: {e}")
            continue
        # Each source scores on its own scale, so rank position is the only
        # comparable signal — normalize to it before merging.
        for rank, row in enumerate(rows):
            norm = 1.0 - (rank / max(1, len(rows)))
            entry_key = (row["mbid"] or row["name"].lower())
            entry = merged.setdefault(entry_key, {
                "mbid": row["mbid"], "name": row["name"],
                "score": 0.0, "sources": []})
            if not entry["mbid"]:
                entry["mbid"] = row["mbid"]
            entry["score"] += norm
            entry["sources"].append(source)

    out = sorted(merged.values(),
                 key=lambda e: (-len(set(e["sources"])), -e["score"], e["name"]))
    _similar_cache[key] = (time.time(), out)
    return out[:limit]
# MusicBrainz's reserved "Various Artists" special-purpose artist.
_VARIOUS_ARTISTS_MBID = "89ad4ac3-39f7-470e-963a-56509c546377"

def lbz_fresh_releases(days: int = 30) -> list:
    """
    Site-wide ListenBrainz fresh releases (recent + upcoming), normalized for
    the Fresh tab. Uses the public /explore/fresh-releases/ JSON endpoint — no
    per-user token needed. Cached for an hour. Raises LBZError if the request
    fails.
    """
    now = time.time()
    if (_FRESH_CACHE["rows"] and _FRESH_CACHE["days"] == days
            and now - _FRESH_CACHE["ts"] < _FRESH_TTL):
        return _FRESH_CACHE["rows"]
    data = _lbz_get(f"explore/fresh-releases/?days={int(days)}"
                    "&past=true&future=true&sort=release_date")
    releases = (data.get("payload", {}) or {}).get("releases", []) or []
    rows = []
    for r in releases:
        rgid = r.get("release_group_mbid", "") or ""
        # ListenBrainz's own fresh-releases page hides compilations credited to
        # "Various Artists" — they're noise for a library-gap tool. Match the
        # site: drop the VA meta-artist by name and by its reserved MBID.
        artist_name = r.get("artist_credit_name", "") or ""
        artist_mbids = r.get("artist_mbids", []) or []
        if (artist_name.strip().lower() in ("various artists", "various")
                or _VARIOUS_ARTISTS_MBID in artist_mbids):
            continue
        rows.append({
            "releaseName": r.get("release_name", ""),
            "artist": r.get("artist_credit_name", ""),
            "artistMbids": r.get("artist_mbids", []) or [],
            "releaseMbid": r.get("release_mbid", ""),
            "releaseGroupMbid": rgid,
            "releaseDate": r.get("release_date", ""),
            "type": r.get("release_group_primary_type", ""),
            "secondaryType": r.get("release_group_secondary_type", ""),
            "coverUrl": (f"https://coverartarchive.org/release-group/{rgid}/front-250"
                         if rgid else ""),
            "listenCount": int(r.get("listen_count") or 0),
        })
    _FRESH_CACHE.update({"ts": now, "days": days, "rows": rows})
    return rows

# ---------------------------------------------------------------------------
# Navidrome
# ---------------------------------------------------------------------------

def _nd_auth_params(nd_user: str, nd_pass: str) -> dict:
    """
    Subsonic salted-token auth (token = md5(password + salt)) so the password
    is never sent in the clear or written to URLs/logs.
    """
    salt  = hashlib.md5(os.urandom(16)).hexdigest()[:12]
    token = hashlib.md5((nd_pass + salt).encode()).hexdigest()
    return {
        "u": nd_user, "t": token, "s": salt, "v": "1.16.1",
        "c": "listenbrainz-bot", "f": "json",
    }

def _nd_search(nd_user, nd_pass, query, song_count=5, _retry=True) -> list:
    """
    Search Navidrome via search3. Retries once after 3s if result is empty,
    because the search index can return nothing while warming up or rebuilding.
    """
    params = {
        **_nd_auth_params(nd_user, nd_pass),
        "query": query, "songCount": str(song_count),
        "albumCount": "0", "artistCount": "0",
    }
    try:
        r     = _http.get(f"{NAVIDROME_URL}/rest/search3", params=params, timeout=5)
        songs = (r.json().get("subsonic-response", {})
                         .get("searchResult3", {})
                         .get("song", []))
        if not songs and _retry:
            # Index may be warming — wait and try once more
            time.sleep(3)
            return _nd_search(nd_user, nd_pass, query, song_count, _retry=False)
        return songs
    except Exception as e:
        print(f"  Navidrome error: {e}")
        return []

def nd_has_track(track: dict, nd_user: str, nd_pass: str) -> bool:
    title  = track.get("title") or ""
    artist = track.get("artist") or ""
    label  = f"{artist} - {title}"

    # Stage 1: exact MBID match (Navidrome v0.57+ detects UUID queries directly)
    if track.get("mbid"):
        songs = _nd_search(nd_user, nd_pass, track["mbid"], 1)
        if songs:
            print(f"    + MBID match: {label}")
            return True

    # Stage 2: title + artist text search with strict validation
    title_q  = title.lower().strip()
    artist_q = artist.lower().strip()
    songs    = _nd_search(nd_user, nd_pass, f"{title} {artist}", 10)
    for song in songs:
        st = song.get("title",  "").lower().strip()
        sa = song.get("artist", "").lower().strip()
        if st == title_q and (artist_q in sa or sa in artist_q):
            print(f"    + Text match: {label}")
            return True

    return False

def nd_find_duplicate(track: dict, nd_user: str, nd_pass: str) -> dict | None:
    """
    Check if a different recording of this track (same title+artist, different MBID)
    already exists in Navidrome. Returns the existing song dict if found, else None.
    """
    track_mbid = track.get("mbid", "")
    title      = track.get("title") or ""
    artist     = track.get("artist") or ""
    title_q    = title.lower().strip()
    artist_q   = artist.lower().strip()
    for song in _nd_search(nd_user, nd_pass, f"{title} {artist}", 10):
        st        = song.get("title",  "").lower().strip()
        sa        = song.get("artist", "").lower().strip()
        song_mbid = song.get("musicBrainzId", "") or ""
        if st == title_q and (artist_q in sa or sa in artist_q):
            if track_mbid and song_mbid and track_mbid != song_mbid:
                return song  # Different version exists in library
    return None

def nd_track_present(artist: str, title: str, mbid: str,
                     nd_user: str, nd_pass: str) -> bool:
    """
    Is this track present in Navidrome? MBID match first, then a validated
    title+artist text match — so libraries with untagged files (no MBIDs)
    are not undercounted.
    """
    if mbid and _nd_search(nd_user, nd_pass, mbid, 1):
        return True
    title_q  = (title or "").lower().strip()
    artist_q = (artist or "").lower().strip()
    if not title_q:
        return False
    for song in _nd_search(nd_user, nd_pass, f"{title} {artist}".strip(), 10):
        st = song.get("title",  "").lower().strip()
        sa = song.get("artist", "").lower().strip()
        if st == title_q and (not artist_q or artist_q in sa or sa in artist_q):
            return True
    return False

def nd_get_all_albums(nd_user: str, nd_pass: str, stats: dict = None) -> list:
    """Page through Navidrome getAlbumList2 and return all albums.

    A failed page breaks out and returns what we have — callers that care that
    the answer is incomplete (rather than "the library really is this size")
    pass `stats` and get `partial`/`error`/`pages` back.
    """
    albums = []
    offset = 0
    size   = 500
    pages  = 0
    if stats is not None:
        stats.update({"partial": False, "error": "", "pages": 0})
    while True:
        try:
            r = _http.get(f"{NAVIDROME_URL}/rest/getAlbumList2", params={
                **_nd_auth_params(nd_user, nd_pass),
                "type": "alphabeticalByName",
                "size": str(size), "offset": str(offset),
            }, timeout=15)
            batch = (r.json().get("subsonic-response", {})
                             .get("albumList2", {})
                             .get("album", []))
            albums.extend(batch)
            pages += 1
            if stats is not None:
                stats["pages"] = pages
            if len(batch) < size:
                break
            offset += size
        except Exception as e:
            print(f"  nd_get_all_albums error: {e}")
            if stats is not None:
                stats["partial"] = True
                stats["error"] = str(e)
            break
    return albums

def nd_get_all_artists(nd_user: str, nd_pass: str) -> list:
    """Flatten Navidrome getArtists (ID3 index buckets) into one artist list."""
    try:
        r = _http.get(f"{NAVIDROME_URL}/rest/getArtists",
                         params=_nd_auth_params(nd_user, nd_pass), timeout=15)
        indexes = (r.json().get("subsonic-response", {})
                           .get("artists", {})
                           .get("index", []))
        return [a for ix in indexes for a in ix.get("artist", [])]
    except Exception as e:
        print(f"  nd_get_all_artists error: {e}")
        return []

def nd_get_album_tracks(nd_user: str, nd_pass: str, album_id: str) -> list:
    try:
        r = _http.get(f"{NAVIDROME_URL}/rest/getAlbum", params={
            **_nd_auth_params(nd_user, nd_pass), "id": album_id,
        }, timeout=10)
        return (r.json().get("subsonic-response", {})
                        .get("album", {})
                        .get("song", []))
    except Exception as e:
        print(f"  nd_get_album_tracks error: {e}")
        return []

def nd_start_scan(nd_user: str, nd_pass: str, full: bool = False) -> bool:
    """
    Trigger a Navidrome library scan via the Subsonic API — the same action as
    the "Quick Scan" button in the UI (or "Full Scan" with full=True). Used
    after beets imports so new folders show up without manual intervention.
    """
    try:
        r = _http.get(f"{NAVIDROME_URL}/rest/startScan", params={
            **_nd_auth_params(nd_user, nd_pass),
            "fullScan": "true" if full else "false",
        }, timeout=10)
        resp = r.json().get("subsonic-response", {})
        ok   = resp.get("status") == "ok"
        if ok:
            st = resp.get("scanStatus", {})
            print(f"  Navidrome {'full' if full else 'quick'} scan triggered "
                  f"(scanning={st.get('scanning')}, count={st.get('count')})")
        else:
            print(f"  Navidrome startScan refused: {resp}")
        return ok
    except Exception as e:
        print(f"  nd_start_scan error: {e}")
        return False

def nd_get_scan_status(nd_user: str, nd_pass: str) -> dict:
    """Return Navidrome's scanStatus dict ({scanning, count, ...}) or {}."""
    try:
        r = _http.get(f"{NAVIDROME_URL}/rest/getScanStatus",
                         params=_nd_auth_params(nd_user, nd_pass), timeout=10)
        return (r.json().get("subsonic-response", {})
                        .get("scanStatus", {}))
    except Exception as e:
        print(f"  nd_get_scan_status error: {e}")
        return {}

def _nd_scan_after_import(token: str) -> bool:
    """Best-effort quick scan for the user behind `token` (post-import hook)."""
    u = _user_for_token(token)
    if not u:
        return False
    return nd_start_scan(u["navidrome_user"], u["navidrome_password"])

def _touch(path: str) -> bool:
    """Best-effort 'modified just now'. Never raises — a failed touch must not
    fail an otherwise-good placement. Placement uses this so a filled track (and
    its album/artist folders) carries a current mtime, letting Navidrome's
    newest-by-modtime sort — and any tool reading it, e.g. AudioMuse — surface
    the album. See CLAUDE.md for the required ND_RECENTLYADDEDBYMODTIME setting."""
    try:
        os.utime(path, None)
        return True
    except Exception as e:
        print(f"  touch failed for {path}: {e}")
        return False

def _norm_album_text(value: str) -> str:
    value = (value or "").lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()

def _fuzzy_album_text(value: str) -> str:
    value = _norm_album_text(value)
    value = re.sub(r"\b(deluxe|expanded|remaster(?:ed)?|anniversary|edition|"
                   r"version|mono|stereo|bonus|explicit)\b", " ", value)
    return re.sub(r"\s+", " ", value).strip()

def _album_group_key(album: dict) -> tuple:
    return (_norm_album_text(album.get("artist") or album.get("albumArtist") or ""),
            _norm_album_text(album.get("name", "")))

def _review_group_id(artist_key: str, album_key: str, album_ids: list) -> str:
    raw = "|".join([artist_key, album_key] + sorted(album_ids))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

def _song_relpath(song: dict) -> str:
    return song.get("path") or song.get("file") or song.get("filename") or ""

# Navidrome's music root as *it* sees it, learned from the first path that
# reconciles, so the suffix walk below runs once per process rather than per file.
_nd_path_prefix = {"strip": None}

def _normalize_song_path(raw: str) -> str:
    """Canonical form of a path string Navidrome handed us.

    Backslashes (a Windows-side Navidrome, or an SMB share), percent-encoding,
    and NFD/NFC differences all produce a path that `os.path.exists` rejects even
    though the file is right there.
    """
    if not raw:
        return ""
    if "%" in raw:
        try:
            raw = urllib.parse.unquote(raw)
        except Exception:
            pass
    raw = raw.replace("\\", "/")
    return unicodedata.normalize("NFC", raw)

def _song_abs_path(song: dict) -> str:
    """Where a Navidrome-reported track actually lives under LB_BOT_MUSIC_DIR.

    Navidrome reports paths as *its own* container sees them. When its music root
    differs from lb-bot's `/music` — a different mount point, a second music
    folder, `/data/music` vs `/music` — the old code returned the foreign absolute
    path verbatim, so every on-disk check failed. That emptied the duplicate-file
    list, blocked merges with "outside /music", and turned the diagnostics probe
    red while the mount was perfectly correct.

    Nothing here knows Navidrome's root, and nothing needs to: walk the trailing
    components of the reported path against `MUSIC_LIBRARY_PATH` until one
    resolves, then remember the prefix that worked.
    """
    raw = _normalize_song_path(_song_relpath(song))
    if not raw:
        return ""
    lib = MUSIC_LIBRARY_PATH

    def _exists(path: str) -> str:
        if os.path.exists(path):
            return os.path.normpath(path)
        # A folder name stored NFD on disk (macOS, some SMB shares) against an
        # NFC path from Navidrome, or the reverse.
        alt = unicodedata.normalize("NFD", path)
        if alt != path and os.path.exists(alt):
            return os.path.normpath(alt)
        return ""

    # "Rooted" rather than os.path.isabs: a POSIX Navidrome reports
    # "/data/music/...", and os.path.isabs says False for that on Windows, which
    # sent the whole thing down the relative branch.
    rooted = raw.startswith("/") or os.path.isabs(raw)

    if rooted:
        hit = _exists(raw)
        if hit:
            return hit
    else:
        hit = _exists(os.path.join(lib, raw))
        if hit:
            return hit

    # A prefix we already learned this process.
    strip = _nd_path_prefix["strip"]
    if strip and raw.startswith(strip):
        hit = _exists(os.path.join(lib, raw[len(strip):].lstrip("/")))
        if hit:
            return hit

    parts = [p for p in raw.split("/") if p]
    # Longest suffix first: the more path components that have to line up, the
    # less ambiguous the rebase. Stop before a bare filename — matching on that
    # alone would pair unrelated albums that happen to share a track name.
    for start in range(1, len(parts) - 1):
        candidate = _exists(os.path.join(lib, *parts[start:]))
        if candidate:
            _nd_path_prefix["strip"] = "/" + "/".join(parts[:start])
            print(f"  Navidrome paths rebased: stripping "
                  f"{_nd_path_prefix['strip']!r} -> {lib}")
            return candidate
    # Nothing resolved. Report the path we were given (rebased under the library
    # if it was relative) so the diagnostics can name it.
    return os.path.normpath(raw if rooted else os.path.join(lib, raw))

def _path_in_library(path: str) -> bool:
    if not path:
        return False
    try:
        lib = os.path.abspath(MUSIC_LIBRARY_PATH)
        candidate = os.path.abspath(path)
        return os.path.commonpath([lib, candidate]) == lib
    except Exception:
        return False

def _path_inside(path: str, root: str) -> bool:
    if not path or not root:
        return False
    try:
        candidate = os.path.abspath(path)
        base = os.path.abspath(root)
        return os.path.commonpath([base, candidate]) == base
    except Exception:
        return False

# ---------------------------------------------------------------------------
# Trash
#
# "Delete this duplicate" used to be an os.remove. When the grouping was wrong
# — and it was, systematically, for every accented or case-variant filename —
# that unlinked good files with no way back. Deletes are now moves into
# LB_BOT_TRASH_DIR, recorded in a manifest that lives beside the files so a
# restart or a lost review state can't strand them.
# ---------------------------------------------------------------------------

_trash_lock = threading.Lock()

def _trash_manifest_path() -> str:
    return os.path.join(LB_BOT_TRASH_DIR, "manifest.json")

def _trash_manifest_read() -> list:
    try:
        with open(_trash_manifest_path(), "r", encoding="utf-8") as fh:
            rows = json.load(fh)
        return rows if isinstance(rows, list) else []
    except Exception:
        return []

def _trash_manifest_write(rows: list) -> None:
    os.makedirs(LB_BOT_TRASH_DIR, exist_ok=True)
    tmp = _trash_manifest_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=1)
    os.replace(tmp, _trash_manifest_path())

def _move_to_trash(real: str) -> dict:
    """Move a library file into the trash, preserving its path under the root.

    Same filesystem by default, so this is a rename rather than a copy of a
    whole FLAC; a cross-device destination falls back to copy+unlink.
    """
    try:
        lib = os.path.realpath(MUSIC_LIBRARY_PATH)
    except Exception:
        lib = MUSIC_LIBRARY_PATH
    rel = os.path.relpath(real, lib) if _path_inside(real, lib) else os.path.basename(real)
    dest = os.path.join(LB_BOT_TRASH_DIR, time.strftime("%Y-%m-%d"), rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    stem, ext = os.path.splitext(dest)
    n = 1
    while os.path.exists(dest):
        dest = f"{stem}_{n}{ext}"
        n += 1
    try:
        size = os.path.getsize(real)
    except OSError:
        size = 0
    try:
        os.replace(real, dest)
    except OSError:
        # Different device (a trash dir outside the library mount).
        shutil.copyfile(real, dest)
        os.remove(real)
    entry = {"original": real, "trash_path": dest,
             "deleted_at": time.time(), "size": size}
    with _trash_lock:
        rows = _trash_manifest_read()
        rows.append(entry)
        _trash_manifest_write(rows)
    return entry

def _restore_from_trash(trash_path: str) -> dict:
    """Put a trashed file back where it came from. Refuses an occupied slot."""
    with _trash_lock:
        rows = _trash_manifest_read()
        entry = next((r for r in rows
                      if os.path.normpath(r.get("trash_path", "")) == os.path.normpath(trash_path)),
                     None)
        if not entry:
            return {"ok": False, "code": "not_found", "error": "Not in the trash manifest"}
        src = entry.get("trash_path", "")
        dest = entry.get("original", "")
        if not os.path.isfile(src):
            return {"ok": False, "code": "not_found", "error": f"{src} is gone"}
        if not dest or not _path_in_library(dest):
            return {"ok": False, "code": "bad_path",
                    "error": "Original path is not inside the music library"}
        if os.path.exists(dest):
            return {"ok": False, "code": "occupied",
                    "error": f"Something is already at {dest}"}
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            os.replace(src, dest)
        except OSError:
            shutil.copyfile(src, dest)
            os.remove(src)
        _trash_manifest_write([r for r in rows if r is not entry])
    return {"ok": True, "restored": dest}

def _album_record(album: dict, nd_user: str, nd_pass: str,
                  include_release_detail: bool = False) -> dict:
    tracks = nd_get_album_tracks(nd_user, nd_pass, album.get("id", ""))
    paths = sorted({p for p in (_song_abs_path(s) for s in tracks) if p})
    record = {
        "id": album.get("id", ""),
        "name": album.get("name", ""),
        "artist": album.get("artist") or album.get("albumArtist") or "",
        "musicBrainzId": album.get("musicBrainzId", ""),
        "coverArt": album.get("coverArt", ""),
        "songCount": album.get("songCount") or len(tracks),
        "duration": album.get("duration", 0),
        "tracks": [{
            "id": s.get("id", ""),
            "title": s.get("title", ""),
            "artist": s.get("artist", ""),
            "musicBrainzId": s.get("musicBrainzId", ""),
            "track": s.get("track", 0),
            "path": _song_abs_path(s),
            # Carried so duplicate-file sets can be ranked without a second
            # round trip: which copy to keep is a question about format,
            # bitrate and size.
            "suffix": s.get("suffix", ""),
            "bitRate": s.get("bitRate", 0),
            "size": s.get("size", 0),
            "duration": s.get("duration", 0),
        } for s in tracks],
        "paths": paths,
    }
    if include_release_detail:
        record.update(mbz_release_display(record.get("musicBrainzId", "")))
    return record

def _enrich_review_release_details(group: dict) -> bool:
    changed = False
    for album in group.get("albums", []):
        mbid = album.get("musicBrainzId", "")
        if not mbid or album.get("release_detail_loaded"):
            continue
        album.update(mbz_release_display(mbid))
        album["release_detail_loaded"] = True
        changed = True
    return changed

# A whole album's runtime is a strong fingerprint: two copies of the same
# release agree to within a few seconds even across different rips.
DUPLICATE_DURATION_TOLERANCE = 5   # seconds, summed over the album

def _album_artist_key(album: dict) -> str:
    return _norm_album_text(album.get("artist") or album.get("albumArtist") or "")

DUPLICATE_TRACK_TOLERANCE = 3      # seconds, per track

def _album_duration_fingerprint(album: dict, nd_user: str, nd_pass: str,
                                cache: dict) -> tuple:
    """The album's per-track runtimes, sorted — a language-independent shape.

    Two copies of one release agree track-for-track even when their title *and*
    artist text share nothing, which is exactly the cross-language case. One
    getAlbum against the local Navidrome per candidate album, cached.
    """
    album_id = album.get("id", "")
    if album_id in cache:
        return cache[album_id]
    fingerprint = ()
    try:
        durations = [int(t.get("duration") or 0)
                     for t in nd_get_album_tracks(nd_user, nd_pass, album_id)]
        if all(d > 0 for d in durations) and durations:
            fingerprint = tuple(sorted(durations))
    except Exception as e:
        print(f"  duplicate scan: track durations unavailable for {album_id}: {e}")
    cache[album_id] = fingerprint
    return fingerprint

def _same_track_shape(left: tuple, right: tuple) -> bool:
    """Do two duration fingerprints agree track-for-track?"""
    if not left or not right or len(left) != len(right):
        return False
    return all(abs(a - b) <= DUPLICATE_TRACK_TOLERANCE
               for a, b in zip(left, right))

def _duplicate_albums_by_signature(albums: list, used_ids: set,
                                   deep: bool = False, progress=None,
                                   cancel=None, nd_user: str = "",
                                   nd_pass: str = "") -> list:
    """Duplicate albums whose titles share no text at all.

    A Japanese release and its English-titled copy have nothing in common for
    _norm_album_text or difflib to compare, so title matching can never find
    them however fuzzy it gets. They are found structurally instead: the same
    track count and near-identical total runtime.

    That is only a candidate. It is confirmed either by the artist tag agreeing
    (the common case — the album title was localized, the artist wasn't), or,
    with `deep`, by both albums resolving to the same MusicBrainz release-group,
    which is language-independent and settles it exactly. The cheap signature
    runs first precisely so `deep` costs a couple of rate-limited MusicBrainz
    calls per candidate pair rather than one per album in the library.
    """
    by_count = {}
    for album in albums:
        if album.get("id") in used_ids:
            continue
        count = int(album.get("songCount") or 0)
        if count < 2 or int(album.get("duration") or 0) <= 0:
            continue
        by_count.setdefault(count, []).append(album)

    rgid_cache = {}
    # The library index already resolved release-group ids for every album it
    # claimed; each one taken from here is a rate-limited MusicBrainz second we
    # don't spend. Empty (or stale-by-omission) just means more live lookups.
    indexed_rgids = {}
    if deep:
        try:
            indexed_rgids = _index_album_rgids()
        except Exception as e:
            print(f"  duplicate scan: library index unavailable ({e})")
    rgid_sources = {"index": 0, "musicbrainz": 0}

    def rgid(album):
        mbid = album.get("musicBrainzId", "")
        if not mbid:
            return ""
        if mbid not in rgid_cache:
            from_index = indexed_rgids.get(album.get("id", ""))
            if from_index:
                rgid_cache[mbid] = from_index
                rgid_sources["index"] += 1
            else:
                # A cache miss is a rate-limited second of sleeping; check for a
                # cancel here or the user waits out the rest of the bucket.
                if cancel and cancel():
                    raise _ScanCancelled()
                rgid_cache[mbid] = mbz_release_group_of(mbid)
                rgid_sources["musicbrainz"] += 1
        return rgid_cache[mbid]

    fingerprints = {}

    def same_release(left, right):
        if _album_artist_key(left) and _album_artist_key(left) == _album_artist_key(right):
            return True
        if not deep:
            return False
        # Both MB-tagged: the release-group settles it exactly.
        left_rg = rgid(left)
        if left_rg and left_rg == rgid(right):
            return True
        # Otherwise fall back to the track shape. Requiring an rgid from *both*
        # sides (`bool(left_rg) and left_rg == rgid(right)`) meant one untagged
        # copy killed the pair — and an untagged copy is the normal case for the
        # localized rip this feature is for, so the deep pass could only ever
        # confirm pairs the title passes had already caught.
        if not nd_user:
            return False
        if left_rg and rgid(right) and left_rg != rgid(right):
            # Both are tagged and they disagree. That is a real answer, not a gap
            # to paper over with durations.
            return False
        if cancel and cancel():
            raise _ScanCancelled()
        return _same_track_shape(
            _album_duration_fingerprint(left, nd_user, nd_pass, fingerprints),
            _album_duration_fingerprint(right, nd_user, nd_pass, fingerprints))

    groups = []
    buckets = list(by_count.values())
    for done, rows in enumerate(buckets, 1):
        if progress:
            try:
                progress(done, len(buckets))
            except _ScanCancelled:
                raise
            except Exception:
                pass
        rows.sort(key=lambda a: int(a.get("duration") or 0))
        # Navidrome's album duration is a sum of per-track whole seconds, so
        # rounding alone drifts with the track count — 5s over a 12-track album was
        # tighter than two rips of the same CD ever agree. Widen the *candidate*
        # gate and let the per-track comparison in same_release do the precise
        # work.
        tolerance = max(DUPLICATE_DURATION_TOLERANCE,
                        DUPLICATE_TRACK_TOLERANCE * int(rows[0].get("songCount") or 1))
        for i, left in enumerate(rows):
            if left.get("id") in used_ids:
                continue
            # Progress only ticks once per track-count bucket, and a bucket in a
            # large library holds hundreds of albums — without this the pair
            # loop below is minutes of uninterruptible work.
            if cancel and cancel():
                raise _ScanCancelled()
            cluster = [left]
            for right in rows[i + 1:]:
                if abs(int(right.get("duration") or 0) - int(left.get("duration") or 0)) \
                        > tolerance:
                    break      # sorted by duration — everything after is worse
                if right.get("id") in used_ids:
                    continue
                if same_release(left, right):
                    cluster.append(right)
            if len(cluster) > 1:
                groups.append(cluster)
                used_ids.update(a.get("id") for a in cluster)
    if deep and (rgid_sources["index"] or rgid_sources["musicbrainz"]):
        print(f"  deep duplicate pass: {rgid_sources['index']} release-group id(s) "
              f"from the library index, {rgid_sources['musicbrainz']} from MusicBrainz")
    return groups

def _bucket_duplicate_albums(albums: list, fuzzy: bool = False,
                             deep: bool = False, progress=None,
                             cancel=None, nd_user: str = "",
                             nd_pass: str = "") -> list:
    buckets = {}
    for album in albums:
        key = _album_group_key(album)
        if not key[0] or not key[1]:
            continue
        buckets.setdefault(key, []).append(album)
    groups = [v for v in buckets.values() if len(v) > 1]
    if not fuzzy and not deep:
        return groups

    used_ids = {a.get("id") for g in groups for a in g}
    if not fuzzy:
        # deep without fuzzy: skip the title passes, go straight to the
        # structural one.
        groups.extend(_duplicate_albums_by_signature(
            albums, used_ids, deep=True, progress=progress, cancel=cancel,
            nd_user=nd_user, nd_pass=nd_pass))
        return groups

    by_artist = {}
    for album in albums:
        if album.get("id") in used_ids:
            continue
        artist_key = _norm_album_text(album.get("artist") or album.get("albumArtist") or "")
        album_key = _fuzzy_album_text(album.get("name", ""))
        if artist_key and album_key:
            by_artist.setdefault(artist_key, []).append((album_key, album))
    for artist_key, rows in by_artist.items():
        fuzzy_group = []
        for i, (left_key, left) in enumerate(rows):
            if left.get("id") in used_ids:
                continue
            fuzzy_group = [left]
            for right_key, right in rows[i + 1:]:
                if right.get("id") in used_ids:
                    continue
                score = difflib.SequenceMatcher(None, left_key, right_key).ratio()
                if score >= 0.88:
                    fuzzy_group.append(right)
            if len(fuzzy_group) > 1:
                groups.append(fuzzy_group)
                used_ids.update(a.get("id") for a in fuzzy_group)
    # Last: the albums no amount of title comparison can pair up.
    groups.extend(_duplicate_albums_by_signature(
        albums, used_ids, deep=deep, progress=progress, cancel=cancel,
        nd_user=nd_user, nd_pass=nd_pass))
    return groups

def _file_identity(path: str) -> tuple:
    """What the *filesystem* calls this file: `(st_dev, st_ino)`, `()` if unknown.

    Path strings are not identity. `os.path.normpath` collapses `..`, `.` and
    duplicate separators and nothing else — it does not case-fold, resolve
    symlinks, or unicode-normalize. `_song_abs_path` forces NFC on Navidrome's
    path while `os.walk` returns the filesystem's own spelling, so on any
    accented name, any case difference, or any bind mount, one file compared
    unequal to itself and was listed twice in a duplicate set — with the same
    stream md5, because it *was* one file. Asking the filesystem instead of
    comparing strings collapses every one of those divergences at once.
    """
    if not path:
        return ()
    try:
        st = os.stat(path)
    except OSError:
        return ()
    return (st.st_dev, st.st_ino)

def _file_dedupe_key(path: str):
    """`_file_identity` with a path fallback for files that can't be stat'ed."""
    ident = _file_identity(path)
    return ident if ident else ("path", os.path.normpath(path or ""))

def _identity_str(path: str) -> str:
    """The identity as something JSON (and the SPA) can carry around."""
    ident = _file_identity(path)
    return f"{ident[0]}:{ident[1]}" if ident else ""

def _album_tracks_with_disk(record: dict, stats: dict = None) -> list:
    """The album's files as Navidrome reports them, plus whatever is on disk.

    Navidrome alone is not enough. A file placed a minute ago isn't indexed yet;
    a file Navidrome filed under a different album id doesn't appear in this
    record; and a Navidrome row with no `path` used to be dropped silently. Since
    the duplicate that a bad fill produces is *newly placed*, the disk half is
    exactly the half that matters.

    The two halves are reconciled on `_file_identity`, not on the path string:
    the string comparison is what let one file appear as two rows and made the
    best copy deletable. The walk is also deliberately narrow — non-recursive,
    skipped at the library root, and skipped for a directory holding
    implausibly more audio than the album has tracks — because an album whose
    files sit at an artist or Various Artists level would otherwise swallow the
    whole subtree as `onlyOnDisk` rows.
    """
    tracks = [dict(t) for t in (record.get("tracks") or []) if t.get("path")]
    by_key = {}
    for t in tracks:
        by_key.setdefault(_file_dedupe_key(t["path"]), t)
    nd_count = len(by_key)
    folders = {os.path.dirname(os.path.normpath(t["path"])) for t in tracks}
    try:
        library_root = os.path.realpath(MUSIC_LIBRARY_PATH)
    except Exception:
        library_root = MUSIC_LIBRARY_PATH
    for folder in sorted(f for f in folders if f):
        try:
            if os.path.realpath(folder) == library_root:
                if stats is not None:
                    stats["disk_walk_skipped_root"] = stats.get("disk_walk_skipped_root", 0) + 1
                continue
        except Exception:
            pass
        rows = _audio_files_in_folder(folder, 400, recursive=False)
        # Not an album folder: loose files at an artist / Various Artists level.
        plausible = max(2 * nd_count, nd_count + 10) if nd_count else 0
        if plausible and len(rows) > plausible:
            if stats is not None:
                stats["disk_walk_skipped_crowded"] = stats.get("disk_walk_skipped_crowded", 0) + 1
            continue
        for row in rows:
            key = _file_dedupe_key(row["path"])
            if key in by_key:
                continue
            # Not in Navidrome (yet). Read what the file itself says.
            tags = _audio_file_tags(row["path"])
            by_key[key] = {
                "id": "",
                "title": tags.get("title", "") or os.path.splitext(row["name"])[0],
                "artist": tags.get("artist", ""),
                "musicBrainzId": next(iter(_tag_recording_mbids(tags)), ""),
                "track": _tag_track_number(tags),
                "path": row["path"],
                "suffix": _file_ext(row["name"]),
                "bitRate": 0,
                "size": row.get("size", 0),
                "duration": tags.get("duration", 0),
                "onlyOnDisk": True,
            }
    return list(by_key.values())

def _duplicate_file_sets(record: dict, stats: dict = None,
                         claimed: set = None) -> list:
    """Files inside one album that are the same song.

    Two kinds of evidence, unioned, because each alone misses cases:

    - **audio** — an identical FLAC stream md5 (or sample count at the same rate).
      This is the one that catches a bad fill: placement rewrites the mis-slotted
      file's title *and* recording MBID to the slot it guessed, so the two copies
      end up sharing neither tag key. Grouping on tags alone could never see the
      duplicate it had just created.
    - **tags** — titles normalize alike, or the same recording MBID: an untagged
      rip and a tagged one share only the title, while "Sing" and "Sing (Album
      Version)" share only the MBID.

    There used to be a third, `stream`: same rounded duration, sample rate and
    channel count for lossy files carrying no md5. Rate and channels are constant
    across a rip, so it degenerated to *duration alone*, and the ±2% size gate is
    a no-op for CBR — two different songs of equal length grouped, and union-find
    chained them into sets of three and more. A guess has no business feeding a
    delete button.

    `claimed` is a caller-owned set of identities already emitted by an earlier
    set: one file belongs to at most one duplicate set, library-wide.

    Each set carries `matchBasis` (the strongest evidence that grouped it) and is
    ordered best-copy-first (lossless before lossy, then bitrate, then size), so
    the UI can propose which to keep without deciding for the user.
    """
    tracks = _album_tracks_with_disk(record, stats=stats)
    parent = list(range(len(tracks)))
    # Strength of the evidence that merged each set, best (lowest) wins.
    _BASIS_RANK = {"audio": 0, "tags": 1}
    basis = {i: "" for i in range(len(tracks))}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b, kind):
        ra, rb = find(a), find(b)
        for root in (ra, rb):
            current = basis.get(root, "")
            if not current or _BASIS_RANK[kind] < _BASIS_RANK[current]:
                basis[root] = kind
        if ra != rb:
            merged = min((basis.get(ra, kind), basis.get(rb, kind), kind),
                         key=lambda k: _BASIS_RANK[k])
            parent[rb] = ra
            basis[ra] = merged

    sigs = [_file_signature_cached(t["path"]) for t in tracks]
    first_seen = {}
    for i, track in enumerate(tracks):
        sig = sigs[i] or {}
        keys = [("audio", f"md5:{sig['md5']}" if sig.get("md5") else ""),
                ("audio", (f"pcm:{sig['samples']}:{sig['sample_rate']}"
                           if sig.get("samples") and sig.get("sample_rate") else "")),
                ("tags", _match_key(track.get("title", ""))),
                ("tags", track.get("musicBrainzId", ""))]
        for kind, value in keys:
            if not value:
                continue
            slot = (kind, value)
            if slot in first_seen:
                union(first_seen[slot], i, kind)
            else:
                first_seen[slot] = i

    buckets = {}
    for i, track in enumerate(tracks):
        buckets.setdefault(find(i), []).append(track)

    sets = []
    for root, members in buckets.items():
        # Distinct *files* only, on filesystem identity rather than the path
        # string: Navidrome can list one file under two ids, and the disk walk
        # can name the same file with a different spelling. A set that lists one
        # file twice always ranks the alias second — i.e. as the deletable row —
        # so this is the check that stops a "duplicate" delete destroying the
        # only copy.
        by_ident = {}
        for track in members:
            key = _file_dedupe_key(track["path"])
            if claimed is not None and key in claimed:
                continue
            by_ident.setdefault(key, track)
        if len(by_ident) < 2:
            continue
        if claimed is not None:
            claimed.update(by_ident)
        files = sorted(by_ident.values(), key=_duplicate_file_rank)
        sets.append({
            "title": files[0].get("title", ""),
            "track": files[0].get("track", 0),
            "albumId": record.get("id", ""),
            "album": record.get("name", ""),
            "artist": record.get("artist", ""),
            "matchBasis": basis.get(root, "") or "tags",
            "files": [{
                "songId": f.get("id", ""),
                "path": f.get("path", ""),
                # The filesystem's own name for this file. The SPA and the delete
                # endpoint compare on this rather than on the path string.
                "identity": _identity_str(f.get("path", "")),
                "title": f.get("title", ""),
                "format": (f.get("suffix", "")
                           or os.path.splitext(f.get("path", ""))[1].lstrip(".")).lower(),
                "bitRate": f.get("bitRate", 0),
                "size": f.get("size", 0),
                "duration": f.get("duration", 0),
                "musicBrainzId": f.get("musicBrainzId", ""),
                "onlyOnDisk": bool(f.get("onlyOnDisk")),
                "recommendedKeep": f is files[0],
            } for f in files],
        })
    sets.sort(key=lambda s: (int(s.get("track") or 0), s.get("title", "").lower()))
    return sets

def _duplicate_file_rank(track: dict) -> tuple:
    """Sort key for copies of one song — lower is the better copy to keep."""
    fmt = (track.get("suffix", "")
           or os.path.splitext(track.get("path", ""))[1].lstrip(".")).lower()
    return (FORMAT_PRIORITY.get(fmt, 50),
            -int(track.get("bitRate") or 0),
            -int(track.get("size") or 0),
            track.get("path", ""))

def _last_copy_refusal(real: str) -> str:
    """Why deleting this file would destroy the only copy — "" to go ahead.

    The backstop that would have prevented the loss even with the grouping bug
    intact: whatever a set claims, a delete only proceeds when some *other* file
    that still exists is genuinely the same song. Two independent sources of
    that evidence, in order:

    1. the stored duplicate set the file belongs to — the survivors of it;
    2. failing that (a live per-album scan's sets are never stored), the file's
       own directory, re-checked with the same rules `_duplicate_file_sets`
       groups on: exact audio identity, or an agreeing title / recording MBID.
    """
    ident = _file_dedupe_key(real)
    in_a_stored_set = False
    with _review_lock:
        stored = list(_review_state.get("duplicate_files", []) or [])
    for dup in stored:
        members = dup.get("files", []) or []
        if not any(_file_dedupe_key(f.get("path", "")) == ident for f in members):
            continue
        in_a_stored_set = True
        for f in members:
            other = f.get("path", "")
            if not other or _file_dedupe_key(other) == ident:
                continue
            if os.path.isfile(other):
                return ""
    if in_a_stored_set:
        return ("every other copy in this set is already gone — this is the "
                "last one, so deleting it would lose the song")
    sig = _audio_signature(real)
    folder = os.path.dirname(real)
    for row in _audio_files_in_folder(folder, 400, recursive=False):
        other = row["path"]
        if _file_dedupe_key(other) == ident:
            continue
        other_sig = _file_signature_cached(other)
        if _same_audio_exact(sig, other_sig):
            return ""
        if sig.get("own_title_key") and sig["own_title_key"] == other_sig.get("own_title_key"):
            return ""
        shared = (sig.get("own_mbids") or set()) & (other_sig.get("own_mbids") or set())
        if shared:
            return ""
    return ("no other copy of this song is visible in its folder — refusing to "
            "delete what looks like the only one")

def _missing_for_album_records(records: list, canonical_mbid: str,
                               canonical_artist: str) -> dict:
    mbz_tracks = mbz_release_tracks(canonical_mbid)
    present_titles = set()
    present_mbids = set()
    # Distinct *files*, kept separately from the title/MBID sets. Collapsing
    # everything into those sets and deriving present as total-minus-missing made
    # duplicate copies arithmetically invisible: an album with three copies of
    # track 1 and nothing else read as "1 present".
    present_paths = set()
    for rec in records:
        for song in rec.get("tracks", []):
            title = (song.get("title") or "").lower().strip()
            if title:
                present_titles.add(title)
            mbid = song.get("musicBrainzId") or ""
            if mbid:
                present_mbids.add(mbid)
            path = song.get("path") or ""
            if path:
                present_paths.add(os.path.normpath(path))
    missing = []
    for rt in mbz_tracks:
        title = (rt.get("title") or "").lower().strip()
        mbid = rt.get("mbid") or ""
        if (mbid and mbid in present_mbids) or (title and title in present_titles):
            continue
        missing.append({
            "artist": canonical_artist,
            "title": rt.get("title", ""),
            "mbid": mbid,
            "position": rt.get("position", 0),
            "decision": "pending",
        })
    covered = max(0, len(mbz_tracks) - len(missing))
    return {
        "present": covered,
        "total": len(mbz_tracks),
        # Files the tracklist doesn't account for: duplicate copies, bonus tracks,
        # or the residue of a fill that placed the wrong file. Surfaced so an album
        # reading 11/11 can still say something is off.
        "extra": max(0, len(present_paths) - covered),
        "missing": missing,
    }

def _make_review_group(records: list, group_type: str = "duplicate",
                       canonical_mbid: str = "") -> dict:
    records = sorted(records, key=lambda r: (-(int(r.get("songCount") or 0)), r["name"]))
    canonical = records[0]
    artist_key, album_key = _album_group_key({
        "artist": canonical["artist"],
        "name": canonical["name"],
    })
    gid = _review_group_id(artist_key, album_key, [r["id"] for r in records])
    if group_type != "duplicate":
        gid = hashlib.sha1(f"{group_type}|{gid}".encode("utf-8")).hexdigest()[:16]
    # canonical_mbid override: callers that know the intended MB release
    # (e.g. the discography classifier's wrong-release guard) can pin the
    # canonical tracklist instead of trusting the album's own tag.
    canonical_mbid = canonical_mbid or canonical["musicBrainzId"]
    missing_info = _missing_for_album_records(
        records, canonical_mbid, canonical["artist"])
    return {
        "id": gid,
        "group_type": group_type,
        "artist": canonical["artist"],
        "album": canonical["name"],
        "artist_key": artist_key,
        "album_key": album_key,
        "created_at": time.time(),
        "updated_at": time.time(),
        "status": "needs_review",
        "merge_mode": "logical",
        "match_mode": "auto",
        "canonical_album_id": canonical["id"],
        "canonical_mbid": canonical_mbid,
        "albums": records,
        "missing_tracks": missing_info["missing"],
        "present": missing_info["present"],
        "total": missing_info["total"],
        "extra": missing_info.get("extra", 0),
        "last_action": "scan",
        "messages": [],
    }

def build_duplicate_album_review(nd_user: str, nd_pass: str,
                                 fuzzy: bool = False,
                                 deep: bool = False,
                                 progress=None,
                                 cancel=None,
                                 records_out: dict = None) -> list:
    # Every album, tagged or not. This used to drop everything without a
    # musicBrainzId before any bucketing — which threw away exactly the albums the
    # cross-language pass exists for, since a hand-ripped Japanese copy of a
    # release you already own is almost never MB-tagged. An MBID is now required
    # only at the release-group lookup that genuinely needs one.
    all_albums = nd_get_all_albums(nd_user, nd_pass)
    duplicate_sets = _bucket_duplicate_albums(all_albums, fuzzy=fuzzy, deep=deep,
                                              progress=progress, cancel=cancel,
                                              nd_user=nd_user, nd_pass=nd_pass)
    groups = []
    for done, albums in enumerate(duplicate_sets, 1):
        if progress:
            try:
                progress(done, len(duplicate_sets))
            except _ScanCancelled:
                raise
            except Exception:
                pass
        records = [_album_record(a, nd_user, nd_pass) for a in albums]
        # Phase two walks the whole library and would re-fetch these same
        # albums from Navidrome; hand them over instead.
        if records_out is not None:
            for rec in records:
                if rec.get("id"):
                    records_out[rec["id"]] = rec
        groups.append(_make_review_group(records, "duplicate"))
    groups.sort(key=lambda g: (g["artist"].lower(), g["album"].lower()))
    return groups

def build_duplicate_file_review(nd_user: str, nd_pass: str,
                                progress=None, cancel=None,
                                stats: dict = None,
                                records: dict = None) -> list:
    """Every album's intra-album duplicate files.

    Unlike duplicate *albums*, these can only be found by looking inside each
    album, so this walks the whole library — one getAlbum per album against the
    local Navidrome. It runs as the second phase of the duplicate scan.

    `stats` is filled in place. Finding nothing is the expected result for a
    clean library, but it is also what a timed-out getAlbum, a truncated album
    listing and a path that does not reconcile all look like — the counters are
    the only way to tell those apart afterwards.
    """
    listing = {}
    all_albums = nd_get_all_albums(nd_user, nd_pass, stats=listing)
    if stats is not None:
        stats.update({
            "albums_total": len(all_albums),
            "albums_scanned": 0,
            "albums_empty": 0,
            "albums_short": 0,
            "tracks_missing_path": 0,
            "sets_found": 0,
            "records_reused": 0,
            "sets_by_audio": 0,
            "files_only_on_disk": 0,
            "disk_walk_skipped_root": 0,
            "disk_walk_skipped_crowded": 0,
            "listing_partial": bool(listing.get("partial")),
            "listing_error": listing.get("error", ""),
        })
    sets = []
    # Identities already emitted in an earlier set. `build_duplicate_file_review`
    # iterates per Navidrome *album id* while the disk walk is per *folder*, so
    # two album ids over one folder — exactly what the duplicate-albums feature
    # is about — each used to emit sets over the other's files, with nothing
    # anywhere deduping across sets.
    claimed = set()
    for done, album in enumerate(all_albums, 1):
        if progress:
            try:
                progress(done, len(all_albums), album.get("artist", ""), album.get("name", ""))
            except _ScanCancelled:
                raise
            except TypeError:
                progress(done, len(all_albums))
            except Exception:
                pass
        if cancel and cancel():
            raise _ScanCancelled()
        album_id = album.get("id", "")
        record = (records or {}).get(album_id)
        reused = record is not None
        if record is None:
            record = _album_record(album, nd_user, nd_pass)
        found = _duplicate_file_sets(record, stats=stats, claimed=claimed)
        sets.extend(found)
        if stats is not None:
            tracks = record.get("tracks") or []
            stats["albums_scanned"] += 1
            stats["sets_found"] += len(found)
            stats["records_reused"] += 1 if reused else 0
            # Grouped on decoded-audio identity rather than tags — the kind a bad
            # fill produces, and the kind the old tag-only scan could not see.
            stats["sets_by_audio"] += sum(1 for s in found
                                          if s.get("matchBasis") == "audio")
            stats["files_only_on_disk"] += sum(
                1 for s in found for f in s.get("files", []) if f.get("onlyOnDisk"))
            if not tracks:
                stats["albums_empty"] += 1
            # getAlbumList2 already told us how many songs the album has, so a
            # short tracklist here means the getAlbum call failed or truncated
            # — without this it is indistinguishable from "no duplicates".
            elif len(tracks) < int(album.get("songCount") or 0):
                stats["albums_short"] += 1
            stats["tracks_missing_path"] += sum(1 for t in tracks if not t.get("path"))
    _flush_file_signatures()
    sets.sort(key=lambda s: (s.get("artist", "").lower(), s.get("album", "").lower(),
                             int(s.get("track") or 0)))
    return sets

def build_all_incomplete_album_review(nd_user: str, nd_pass: str,
                                      fuzzy: bool = False,
                                      progress=None) -> list:
    all_albums = [a for a in nd_get_all_albums(nd_user, nd_pass)
                  if a.get("musicBrainzId")]
    duplicate_ids = set()
    groups = []
    duplicate_sets = _bucket_duplicate_albums(all_albums, fuzzy=fuzzy)
    for albums in duplicate_sets:
        duplicate_ids.update(a.get("id") for a in albums)
    work = duplicate_sets + [[a] for a in all_albums if a.get("id") not in duplicate_ids]
    for done, albums in enumerate(work, 1):
        if progress:
            try:
                progress(done, len(work), albums[0].get("artist", ""), albums[0].get("name", ""))
            except _ScanCancelled:
                raise
            except TypeError:
                progress(done, len(work))
            except Exception:
                pass
        records = [_album_record(a, nd_user, nd_pass) for a in albums]
        group = _make_review_group(records, "duplicate" if len(records) > 1 else "incomplete")
        if group.get("missing_tracks"):
            groups.append(group)
    groups.sort(key=lambda g: (g["artist"].lower(), g["album"].lower()))
    return groups

def mbz_release_group_of(release_mbid: str) -> str:
    """
    Release-group id owning a release. Immutable lookup → cached forever in
    _mbz_cache, so it costs one rate-limited call per release, once ever.
    Returns "" when the mbid doesn't resolve (bad tag, or it's not a release
    mbid at all).
    """
    if not release_mbid:
        return ""
    data = mbz_get(f"release/{release_mbid}", {"inc": "release-groups"})
    return ((data.get("release-group") or {}).get("id")) or ""

# Title fallback threshold for discography matching. Deliberately stricter
# than the old 0.88: at 0.88, sibling titles ("X" vs "X Live") and even
# unrelated albums cross-matched, and a wrong match is worse than "missing".
DISCOGRAPHY_TITLE_THRESHOLD = 0.92

def _match_albums_to_release_groups(rgs: list, albums: list) -> tuple:
    """
    Match one artist's Navidrome albums to their MusicBrainz release-groups.

    Pass 1 — MBID: each album's musicBrainzId (a release mbid; release-group
    mbids in tags are honored too) resolves to its release-group and claims
    it exactly. Albums whose mbid resolves to a release-group *outside* this
    discography (compilations filtered from the browse, other artists) are
    excluded from further matching — their identity is known, they must not
    title-match something else.

    Pass 2 — conservative title fallback for untagged/unresolvable albums:
    greedy best-first assignment on _fuzzy_album_text similarity at
    DISCOGRAPHY_TITLE_THRESHOLD, one fuzzy-title key per release-group and
    vice versa (duplicate copies sharing the same title stay grouped so the
    duplicate-merge machinery still sees them).

    Returns (claims, meta): claims = {rgid: [albums]},
    meta = {rgid: {"method": "mbid"|"title", "score": float}}.
    """
    rg_ids = {rg["rgid"] for rg in rgs}
    claims: dict = {}
    meta: dict = {}
    fallback = []
    for album in albums:
        rel_mbid = album.get("musicBrainzId") or ""
        if not rel_mbid:
            fallback.append(album)
            continue
        rgid = rel_mbid if rel_mbid in rg_ids else mbz_release_group_of(rel_mbid)
        if rgid in rg_ids:
            claims.setdefault(rgid, []).append(album)
            meta[rgid] = {"method": "mbid", "score": 1.0}
        elif not rgid:
            # Tag didn't resolve — treat like an untagged album.
            fallback.append(album)
        # else: resolves to a release-group not in this discography — known
        # identity, deliberately not matched by title.
    by_title: dict = {}
    for album in fallback:
        by_title.setdefault(_fuzzy_album_text(album.get("name", "")), []).append(album)
    pairs = []
    for rg in rgs:
        if rg["rgid"] in claims:
            continue
        rg_key = _fuzzy_album_text(rg["title"])
        for title_key in by_title:
            score = difflib.SequenceMatcher(None, rg_key, title_key).ratio()
            if score >= DISCOGRAPHY_TITLE_THRESHOLD:
                pairs.append((score, rg["rgid"], title_key))
    taken_rgs, taken_titles = set(), set()
    for score, rgid, title_key in sorted(pairs, key=lambda p: -p[0]):
        if rgid in taken_rgs or title_key in taken_titles:
            continue
        taken_rgs.add(rgid)
        taken_titles.add(title_key)
        claims[rgid] = by_title[title_key]
        meta[rgid] = {"method": "title", "score": round(score, 3)}
    return claims, meta

def build_artist_discography(artist_mbid: str, artist_name: str,
                             nd_user: str, nd_pass: str, progress=None,
                             skip_library: bool = False,
                             all_albums: list = None) -> dict:
    """
    Resolve one artist's MusicBrainz discography against the user's Navidrome
    library. Classifies every release-group as:
      - "complete"   : matched Navidrome album(s), no missing tracks
      - "incomplete" : matched Navidrome album(s), missing_tracks non-empty —
                       built via _missing_for_album_records + _make_review_group,
                       the same machinery build_all_incomplete_album_review uses,
                       so these merge into the existing Fill Gaps pipeline for free
      - "untagged"   : matched by title but the Navidrome album has no
                       musicBrainzId — completeness can't be verified, so this is
                       flagged distinctly rather than guessed
      - "missing"    : no Navidrome match at all — rgid + light metadata only.
                       mbz_resolve_album is NOT called here (only later, on demand,
                       when the user opens that album's detail view) to keep
                       MusicBrainz call volume O(release-groups), not
                       O(release-groups * missing).

    Returns {"artist_mbid", "artist_name", "releases": [...], "review_groups": [...]}
    where review_groups is the "incomplete" subset, ready for _merge_review_groups.
    """
    rgs = mbz_artist_release_groups(artist_mbid)

    # Not-owned browse (MusicBrainz search picks): the caller already knows the
    # user has nothing by this artist, so skip the whole-library album fetch
    # (8+ sequential Navidrome pages on a large library — the slow part of
    # opening the page) and classify every release-group as missing.
    if skip_library:
        releases = [
            {"rgid": rg["rgid"], "title": rg["title"], "year": rg["year"],
             "primary_type": rg["primary_type"],
             "secondary_types": rg.get("secondary_types") or [],
             "effective_type": _effective_release_type(
                 rg["primary_type"], rg.get("secondary_types")),
             "status": "missing"}
            for rg in rgs
        ]
        if progress:
            try: progress(len(rgs), len(rgs), artist_name, "")
            except Exception: pass
        return {"artist_mbid": artist_mbid, "artist_name": artist_name,
                "releases": releases, "review_groups": []}

    # The whole album catalog, one artist's worth of use. Callers that scan many
    # artists pass it in once (_library_index_task) — this used to re-page the
    # entire library per artist. For a single artist, the shared 5-minute index
    # is the same data /api/library and /api/summary already hold, so only a
    # non-default Navidrome user has to fetch its own.
    if all_albums is None:
        default = _default_web_user() or {}
        all_albums = (_nd_album_index()
                      if nd_user and nd_user == default.get("navidrome_user")
                      else nd_get_all_albums(nd_user, nd_pass))
    artist_norm = _norm_album_text(artist_name)

    # Library bucket: exact artist-name normalization only. A cross-artist
    # fuzzy fallback used to live here — when this artist's tags didn't
    # normalize to an exact match it handed the scan an entirely different
    # artist's catalog, and every later title match produced confidently
    # wrong ownership verdicts. A wrong match is worse than "missing".
    by_artist = [a for a in all_albums
                 if _norm_album_text(a.get("artist") or a.get("albumArtist") or "") == artist_norm]

    claims, claim_meta = _match_albums_to_release_groups(rgs, by_artist)

    releases, review_groups = [], []
    total = len(rgs)
    for done, rg in enumerate(rgs, 1):
        if progress:
            try:
                progress(done, total, artist_name, rg["title"])
            except Exception:
                pass
        matches = claims.get(rg["rgid"])
        match_info = claim_meta.get(rg["rgid"], {})

        if not matches:
            releases.append({
                "rgid": rg["rgid"], "title": rg["title"], "year": rg["year"],
                "primary_type": rg["primary_type"],
                "secondary_types": rg.get("secondary_types") or [],
                "effective_type": _effective_release_type(
                    rg["primary_type"], rg.get("secondary_types")),
                "status": "missing",
            })
            continue

        tagged = [a for a in matches if a.get("musicBrainzId")]
        if not tagged:
            releases.append({
                "rgid": rg["rgid"], "title": rg["title"], "year": rg["year"],
                "primary_type": rg["primary_type"],
                "secondary_types": rg.get("secondary_types") or [],
                "effective_type": _effective_release_type(
                    rg["primary_type"], rg.get("secondary_types")),
                "status": "untagged",
                "match_method": match_info.get("method", ""),
                "match_score": match_info.get("score", 0),
                "navidrome_album_ids": [a.get("id", "") for a in matches],
            })
            continue

        # Wrong-release guard: a title-matched album's own mbid may belong to
        # a different release-group (or not resolve at all) — computing
        # completeness from that tag would judge this release-group by some
        # other album's tracklist. Pin the canonical tracklist to a release
        # actually inside this release-group instead.
        canonical_override = ""
        if match_info.get("method") == "title":
            records_sorted = sorted(
                tagged, key=lambda r: (-(int(r.get("songCount") or 0)), r.get("name", "")))
            own_mbid = records_sorted[0].get("musicBrainzId") or ""
            own_rg = own_mbid if own_mbid == rg["rgid"] else mbz_release_group_of(own_mbid)
            if own_rg != rg["rgid"]:
                canonical_override = mbz_resolve_album(rg["rgid"]).get("release_mbid", "")

        records = [_album_record(a, nd_user, nd_pass) for a in tagged]
        group   = _make_review_group(records, "duplicate" if len(records) > 1 else "incomplete",
                                     canonical_mbid=canonical_override)
        if group.get("missing_tracks"):
            group["source"] = "artist_discography"
            group["rgid"]   = rg["rgid"]
            # Group ids derive from the matched album records; single-claim
            # matching should make collisions impossible, but never emit two
            # groups with the same id regardless.
            if not any(g["id"] == group["id"] for g in review_groups):
                review_groups.append(group)
            releases.append({
                "rgid": rg["rgid"], "title": rg["title"], "year": rg["year"],
                "primary_type": rg["primary_type"],
                "secondary_types": rg.get("secondary_types") or [],
                "effective_type": _effective_release_type(
                    rg["primary_type"], rg.get("secondary_types")),
                "status": "incomplete",
                "match_method": match_info.get("method", ""),
                "match_score": match_info.get("score", 0),
                "group_id": group["id"], "present": group["present"], "total": group["total"],
                # Carried so the album detail page can ask for per-track
                # presence without first resolving the review group.
                "navidrome_album_ids": [a.get("id", "") for a in tagged],
            })
        else:
            releases.append({
                "rgid": rg["rgid"], "title": rg["title"], "year": rg["year"],
                "primary_type": rg["primary_type"],
                "secondary_types": rg.get("secondary_types") or [],
                "effective_type": _effective_release_type(
                    rg["primary_type"], rg.get("secondary_types")),
                "status": "complete",
                "match_method": match_info.get("method", ""),
                "match_score": match_info.get("score", 0),
                "navidrome_album_ids": [a.get("id", "") for a in tagged],
            })

    return {"artist_mbid": artist_mbid, "artist_name": artist_name,
            "releases": releases, "review_groups": review_groups}

# ---------------------------------------------------------------------------
# Library index — persistent per-artist discography classification (SQLite,
# Lidarr-style). Artist pages read this instantly; scans write through it.
# Missing DB file → created empty → every artist behaves like today
# (unindexed → scan once). Bump INDEX_SCAN_VERSION when the matcher changes
# so old rows count as stale.
# ---------------------------------------------------------------------------

# Bumped to 2: compilations are no longer excluded from a discography scan, so
# every index built before this is missing rows rather than merely stale.
INDEX_SCAN_VERSION = 2

_index_conn = None
_index_lock = threading.Lock()

def _index_db():
    """Shared SQLite connection (WAL, thread-safe via _index_lock)."""
    global _index_conn
    if _index_conn is None:
        conn = sqlite3.connect(LIBRARY_INDEX_FILE, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS artists (
              artist_key   TEXT PRIMARY KEY,   -- artist mbid, or 'nd:'+navidrome id
              artist_mbid  TEXT NOT NULL DEFAULT '',
              nd_artist_id TEXT NOT NULL DEFAULT '',
              name         TEXT NOT NULL DEFAULT '',
              scanned_at   REAL NOT NULL DEFAULT 0,
              scan_version INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_artists_nd ON artists(nd_artist_id);
            CREATE TABLE IF NOT EXISTS release_groups (
              artist_key   TEXT NOT NULL,
              rgid         TEXT NOT NULL,
              title        TEXT NOT NULL DEFAULT '',
              primary_type TEXT NOT NULL DEFAULT '',
              -- JSON list of MusicBrainz secondary types. Kept raw rather than
              -- pre-collapsed to a label so the display grouping can change
              -- without re-scanning every artist against MusicBrainz.
              secondary_types TEXT NOT NULL DEFAULT '[]',
              year         TEXT NOT NULL DEFAULT '',
              status       TEXT NOT NULL DEFAULT 'missing',
              group_id     TEXT NOT NULL DEFAULT '',
              present      INTEGER NOT NULL DEFAULT 0,
              total        INTEGER NOT NULL DEFAULT 0,
              nd_album_ids TEXT NOT NULL DEFAULT '[]',
              match_method TEXT NOT NULL DEFAULT '',
              match_score  REAL NOT NULL DEFAULT 0,
              updated_at   REAL NOT NULL DEFAULT 0,
              PRIMARY KEY (artist_key, rgid)
            );
            -- Audio identity per file, so the duplicate scan costs one stat()
            -- per file on a re-run instead of a stream-header read. Invalidated
            -- on (size, mtime), which is what changes when a file is replaced.
            CREATE TABLE IF NOT EXISTS file_signatures (
              path        TEXT PRIMARY KEY,
              size        INTEGER NOT NULL DEFAULT 0,
              mtime       REAL NOT NULL DEFAULT 0,
              md5         TEXT NOT NULL DEFAULT '',
              samples     INTEGER NOT NULL DEFAULT 0,
              length      REAL NOT NULL DEFAULT 0,
              sample_rate INTEGER NOT NULL DEFAULT 0,
              channels    INTEGER NOT NULL DEFAULT 0,
              title_key   TEXT NOT NULL DEFAULT '',
              mbids       TEXT NOT NULL DEFAULT '',
              scanned_at  REAL NOT NULL DEFAULT 0
            );
            PRAGMA user_version = 1;
        """)
        # CREATE TABLE IF NOT EXISTS never adds a column to a DB that already
        # exists, so columns added after the first release need an explicit
        # ALTER. Guarded on table_info rather than swallowing the "duplicate
        # column" error, so a real failure still surfaces.
        have = {r["name"] for r in conn.execute("PRAGMA table_info(release_groups)")}
        if "secondary_types" not in have:
            conn.execute("ALTER TABLE release_groups "
                         "ADD COLUMN secondary_types TEXT NOT NULL DEFAULT '[]'")
        conn.commit()
        _index_conn = conn
    return _index_conn

def _index_artist_key(artist_mbid: str = "", nd_artist_id: str = "") -> str:
    return artist_mbid or (f"nd:{nd_artist_id}" if nd_artist_id else "")

def _index_owned_rgids() -> set:
    """Release-group MBIDs the library already holds (fully or partly), drawn
    from whatever artists have been indexed. `missing` release-groups are
    excluded — everything else (complete / incomplete / untagged) is on disk.
    Used to badge Fresh releases as reliably owned without fuzzy title matching;
    un-indexed artists simply don't appear, so the default is `not owned`."""
    with _index_lock:
        conn = _index_db()
        rows = conn.execute(
            "SELECT DISTINCT rgid FROM release_groups "
            "WHERE status != 'missing' AND rgid != ''").fetchall()
    return {r["rgid"] for r in rows}

def _index_album_rgids() -> dict:
    """Navidrome album id -> release-group MBID, for whatever is indexed.

    The deep duplicate pass needs exactly this mapping and was re-deriving it
    from MusicBrainz one rate-limited second at a time, even though the index
    build already resolved it for every album it claimed. Un-indexed albums
    simply aren't in the dict and fall back to the live lookup.
    """
    out = {}
    with _index_lock:
        conn = _index_db()
        rows = conn.execute(
            "SELECT rgid, nd_album_ids FROM release_groups "
            "WHERE rgid != '' AND nd_album_ids != '[]'").fetchall()
    for row in rows:
        try:
            album_ids = json.loads(row["nd_album_ids"]) or []
        except Exception:
            continue
        for album_id in album_ids:
            if album_id:
                out[album_id] = row["rgid"]
    return out

def _index_store_artist(result: dict, nd_artist_id: str = "") -> None:
    """Write one discography scan result into the index (idempotent:
    delete+reinsert the artist's release-group rows)."""
    key = _index_artist_key(result.get("artist_mbid", ""), nd_artist_id)
    if not key:
        return
    now = time.time()
    rows = [(key,
             r.get("rgid", ""), r.get("title", ""), r.get("primary_type", ""),
             json.dumps(r.get("secondary_types") or []),
             r.get("year", ""), r.get("status", "missing"), r.get("group_id", ""),
             int(r.get("present") or 0), int(r.get("total") or 0),
             json.dumps(r.get("navidrome_album_ids") or []),
             r.get("match_method", ""), float(r.get("match_score") or 0), now)
            for r in result.get("releases", [])]
    with _index_lock:
        conn = _index_db()
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO artists "
                "(artist_key, artist_mbid, nd_artist_id, name, scanned_at, scan_version) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, result.get("artist_mbid", ""), nd_artist_id,
                 result.get("artist_name", ""), now, INDEX_SCAN_VERSION))
            # A previously nd:-keyed row for the same artist is superseded
            # once we know the mbid.
            if result.get("artist_mbid") and nd_artist_id:
                conn.execute(
                    "DELETE FROM artists WHERE artist_key = ?", (f"nd:{nd_artist_id}",))
                conn.execute(
                    "DELETE FROM release_groups WHERE artist_key = ?", (f"nd:{nd_artist_id}",))
            conn.execute("DELETE FROM release_groups WHERE artist_key = ?", (key,))
            conn.executemany(
                "INSERT INTO release_groups (artist_key, rgid, title, primary_type, "
                "secondary_types, year, status, group_id, present, total, nd_album_ids, "
                "match_method, match_score, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

def _index_mark_release_present(rgid: str = "", group_id: str = "") -> int:
    """Flip an indexed release-group to `present` after its files were placed.

    Without this the index keeps saying `missing` until the artist is rescanned
    against MusicBrainz, so a filled album shows up twice in the clients: once in
    the library and once in the not-in-your-library list. A rescan is far too
    expensive to trigger per download (one MusicBrainz request per second), and
    it is not needed — placement already proves the release-group is held.

    Matched by rgid for an artist-page download, or by the review group id for a
    Fill-gaps completion, which is the only handle that path has. `present`/
    `total` and the Navidrome album ids are left alone: nothing here knows the
    real numbers, and the next full scan will fill them in.

    Returns the number of index rows changed.
    """
    if not rgid and not group_id:
        return 0
    now = time.time()
    try:
        with _index_lock:
            conn = _index_db()
            with conn:
                if rgid:
                    cur = conn.execute(
                        "UPDATE release_groups SET status = 'present', updated_at = ? "
                        "WHERE rgid = ? AND status != 'present'", (now, rgid))
                else:
                    cur = conn.execute(
                        "UPDATE release_groups SET status = 'present', updated_at = ? "
                        "WHERE group_id = ? AND status != 'present'", (now, group_id))
                return cur.rowcount or 0
    except Exception as e:  # noqa: BLE001 — a bookkeeping write must not fail a placement
        print(f"  index: could not mark release present: {e}")
        return 0

def _index_get_artist(artist_mbid: str = "", nd_artist_id: str = "") -> dict | None:
    """Stored discography for an artist, or None if not indexed. Shape matches
    the scan-task result so the frontend renders both identically."""
    with _index_lock:
        conn = _index_db()
        artist = None
        if artist_mbid:
            artist = conn.execute(
                "SELECT * FROM artists WHERE artist_key = ?", (artist_mbid,)).fetchone()
        if artist is None and nd_artist_id:
            artist = conn.execute(
                "SELECT * FROM artists WHERE artist_key = ? OR nd_artist_id = ? "
                "ORDER BY scanned_at DESC LIMIT 1",
                (f"nd:{nd_artist_id}", nd_artist_id)).fetchone()
        if artist is None:
            return None
        rg_rows = conn.execute(
            "SELECT * FROM release_groups WHERE artist_key = ? ORDER BY year, title",
            (artist["artist_key"],)).fetchall()
    releases = []
    for r in rg_rows:
        try:
            secondary = json.loads(r["secondary_types"] or "[]")
        except (ValueError, IndexError, KeyError):
            secondary = []
        row = {"rgid": r["rgid"], "title": r["title"], "year": r["year"],
               "primary_type": r["primary_type"], "secondary_types": secondary,
               "effective_type": _effective_release_type(r["primary_type"], secondary),
               "status": r["status"],
               "match_method": r["match_method"], "match_score": r["match_score"]}
        if r["status"] == "incomplete":
            row.update(group_id=r["group_id"], present=r["present"], total=r["total"])
        nd_ids = json.loads(r["nd_album_ids"] or "[]")
        if nd_ids:
            row["navidrome_album_ids"] = nd_ids
        releases.append(row)
    scanned_at = float(artist["scanned_at"] or 0)
    stale = (time.time() - scanned_at > LB_BOT_INDEX_TTL_DAYS * 86400
             or int(artist["scan_version"] or 0) != INDEX_SCAN_VERSION)
    return {"artist_mbid": artist["artist_mbid"], "artist_name": artist["name"],
            "scanned_at": scanned_at, "stale": stale, "releases": releases}

# Decisions that claim the track is handled. A fresh scan that still reports the
# track missing from Navidrome proves otherwise, so they must not be inherited —
# a placement that silently failed used to leave the track "placed" forever, and
# nothing would ever try again. The user's own skipped/dismissed always survive:
# those are statements of intent, not claims about the library.
_STALE_ON_RESCAN_DECISIONS = ("placed", "queued", "downloading", "verified",
                              "navidrome_pending", "navidrome_verified")

def _inherited_decision(previous: dict, fallback: str = "pending") -> str:
    decision = previous.get("decision", fallback) or fallback
    if decision in _STALE_ON_RESCAN_DECISIONS:
        return "pending"
    return decision

def _merge_review_groups(new_groups: list, source_key: str = "groups",
                         include_jobs: bool = True) -> list:
    old = {g.get("id"): g for g in _review_snapshot().get(source_key, [])}
    seen = set()
    for group in new_groups:
        seen.add(group.get("id"))
        prev = old.get(group["id"])
        if not prev:
            _apply_repair_job_projection_to_group(group)
            continue
        group["canonical_album_id"] = prev.get("canonical_album_id", group["canonical_album_id"])
        # Carry the user's skip/hide decision across rescans — without this a
        # rescan resurrects every album the user explicitly dismissed.
        group["hidden"] = prev.get("hidden", group.get("hidden", False))
        group["merge_mode"] = prev.get("merge_mode", group["merge_mode"])
        group["match_mode"] = prev.get("match_mode", group.get("match_mode", "auto"))
        group["source_results"] = prev.get("source_results", group.get("source_results", {}))
        group["match_items"] = prev.get("match_items", group.get("match_items", []))
        group["messages"] = prev.get("messages", [])
        previous_tracks = {(t.get("mbid"), t.get("title")): dict(t)
                     for t in prev.get("missing_tracks", [])}
        for track in group.get("missing_tracks", []):
            previous = previous_tracks.get((track.get("mbid"), track.get("title")), {})
            track["decision"] = _inherited_decision(
                previous, track.get("decision", "pending"))
            for field in ("local_path", "download_state", "download_error",
                          "downloaded_at", "imported_at", "matched_relpath",
                          "repair_job_id", "repair_track_id"):
                if previous.get(field):
                    track[field] = previous[field]
        _apply_repair_job_projection_to_group(group)
    if LB_BOT_REPAIR_JOBS and include_jobs:
        for job in repair_jobs.values():
            if job.get("status") not in REPAIR_JOB_ACTIVE_STATUSES:
                continue
            gid = job.get("group_id", "")
            if gid and gid not in seen:
                new_groups.append(_review_group_from_repair_job(job))
    return new_groups

def _find_review_group(group_id: str) -> dict | None:
    # Duplicate-scan groups live in their own list but share the same group
    # actions (canonical pick, retag/merge, hide), so look in both.
    for group in _review_state.get("groups", []):
        if group.get("id") == group_id:
            return group
    for group in _review_state.get("duplicate_groups", []):
        if group.get("id") == group_id:
            return group
    return None

def _group_generation(group: dict):
    """A token identifying the tracklist a source search was run against.

    The search now runs with _review_lock released, so a rescan can replace the
    group underneath it. Object identity is no use — _merge_review_groups carries
    `source_results` across a rescan by reference — and what actually invalidates
    a result is the *tracklist*: coverage summaries and the ranking's
    expected_track_count are both computed from it.

    Deliberately not `updated_at`: a dozen unrelated paths bump it (download
    state, placement bookkeeping), several of them while a search on this very
    group is in flight, and each would throw away a good 30s result.
    """
    if not group:
        return None
    return tuple((t.get("recording_mbid") or t.get("title") or "")
                 for t in (group.get("missing_tracks") or []))

def _reusable_source_results(group: dict) -> dict | None:
    """The group's existing slskd results, if they are young enough to trust."""
    src = group.get("source_results") or {}
    if not (src.get("folders") or []):
        return None
    age = time.time() - (src.get("created_at") or 0)
    return src if 0 <= age < SOURCE_RESULTS_TTL else None

# Group ids with a source search in flight. The search no longer holds
# _review_lock, so nothing else stops two of them racing on one group.
_source_search_inflight = set()
_source_search_lock = threading.Lock()

@contextlib.contextmanager
def _source_search_claim(group_id: str):
    """Yields True if this caller got the search slot for `group_id`."""
    with _source_search_lock:
        got = group_id not in _source_search_inflight
        if got:
            _source_search_inflight.add(group_id)
    try:
        yield got
    finally:
        if got:
            with _source_search_lock:
                _source_search_inflight.discard(group_id)

def refresh_group_albums_from_navidrome(group_id: str) -> bool:
    """Re-read one group's albums from Navidrome and recompute its counts.

    `refresh_group_missing` alone cannot do this: it recomputes from the cached
    `record["tracks"]` snapshots stored *inside* the group, which were taken when
    the group was built. So after a fill the album kept reporting its pre-fill
    present/total and `missingCount` until a full `POST /api/scan` rebuilt every
    group from Navidrome — the "I have to rescan the whole library before the song
    count updates" complaint.

    The Navidrome round trips happen outside `_review_lock`; only the swap is
    inside it.
    """
    user = _default_web_user()
    if not user or not group_id:
        return False
    with _review_lock:
        group = _find_review_group(group_id)
        if not group:
            return False
        album_ids = [a.get("id", "") for a in (group.get("albums") or []) if a.get("id")]
        canonical_mbid = group.get("canonical_mbid", "")
    if not album_ids:
        return False

    # Fresh album metadata (songCount, duration) as well as fresh tracks — the
    # stored records carry the counts from when the group was built, and no call
    # site ever passed force=True, so /api/library and /api/summary lagged by up
    # to the index TTL on top of Navidrome's own scan latency.
    live = {a.get("id", ""): a for a in _nd_album_index(force=True)}
    # Warm the MusicBrainz tracklist here so the in-lock recompute below hits the
    # cache instead of a rate-limited request while holding the lock.
    if canonical_mbid:
        try:
            mbz_release_tracks(canonical_mbid)
        except Exception:
            pass
    records = []
    for album_id in album_ids:
        album = live.get(album_id) or {"id": album_id}
        try:
            records.append(_album_record(album, user["navidrome_user"],
                                         user["navidrome_password"]))
        except Exception as e:
            # A partial refresh would under-report `present` and reopen gaps that
            # are actually filled. Leave the old snapshot alone instead.
            print(f"  group refresh: Navidrome read failed for {album_id}: {e}")
            return False
    if not any(r.get("tracks") for r in records):
        print(f"  group refresh: Navidrome returned no tracks for group {group_id}")
        return False

    with _review_lock:
        group = _find_review_group(group_id)
        if not group:
            return False
        group["albums"] = records
        refresh_group_missing(group)
    return True

def refresh_group_missing(group: dict) -> dict:
    canonical = next((a for a in group.get("albums", [])
                      if a.get("id") == group.get("canonical_album_id")), None)
    if not canonical:
        canonical = (group.get("albums") or [{}])[0]
        group["canonical_album_id"] = canonical.get("id", "")
    group["canonical_mbid"] = canonical.get("musicBrainzId", "")
    group["artist"] = canonical.get("artist", group.get("artist", ""))
    group["album"] = canonical.get("name", group.get("album", ""))
    existing = {(t.get("mbid"), t.get("title")): dict(t)
                for t in group.get("missing_tracks", [])}
    missing_info = _missing_for_album_records(
        group.get("albums", []), group["canonical_mbid"], group["artist"])
    for track in missing_info["missing"]:
        key = (track.get("mbid"), track.get("title"))
        previous = existing.get(key, {})
        track["decision"] = _inherited_decision(previous)
        for field in ("local_path", "download_state", "download_error",
                      "downloaded_at", "imported_at", "matched_relpath"):
            if previous.get(field):
                track[field] = previous[field]
    group["missing_tracks"] = missing_info["missing"]
    _apply_repair_job_projection_to_group(group)
    group["present"] = missing_info["present"]
    group["total"] = missing_info["total"]
    group["extra"] = missing_info.get("extra", 0)
    group["updated_at"] = time.time()
    return group

def preview_group_retag(group: dict) -> dict:
    canonical_id = group.get("canonical_album_id")
    affected = [a for a in group.get("albums", []) if a.get("id") != canonical_id]
    paths = []
    bad = []
    for album in affected:
        for track in album.get("tracks", []):
            path = track.get("path", "")
            if not path:
                bad.append(f"{album.get('artist')} - {album.get('name')}: missing path")
            elif not _path_in_library(path):
                bad.append(f"{path} is outside {MUSIC_LIBRARY_PATH}")
            else:
                paths.append(path)
    folders = sorted({os.path.dirname(p) for p in paths if p})
    if not group.get("canonical_mbid"):
        # Named, because "Retag safety check failed" with no canonical MBID looked
        # identical to a permissions problem.
        bad.append("the album you're keeping has no MusicBrainz release id — "
                   "pick a canonical copy that does")
    if not folders and not bad:
        bad.append("no on-disk folder could be resolved for the other copies")
    return {
        "ok": bool(folders) and not bad,
        "canonical_mbid": group.get("canonical_mbid", ""),
        "affected_album_count": len(affected),
        "track_count": len(paths),
        "folders": folders,
        "blocked": bad,
    }

def _canonical_release_fields(release_mbid: str) -> dict:
    data = mbz_get(f"release/{release_mbid}", {"inc": "release-groups artist-credits"})
    rg = data.get("release-group") or {}
    artist = _artist_credit_str(data.get("artist-credit")) or group_artist_fallback(data)
    return {
        "album": data.get("title", ""),
        "albumartist": artist,
        "mb_albumid": release_mbid,
        "mb_releasegroupid": rg.get("id", ""),
        "year": (data.get("date") or "")[:4],
    }

def group_artist_fallback(data: dict) -> str:
    ac = (data.get("artist-credit") or [])
    if ac:
        return _artist_credit_str(ac)
    return ""

def beets_merge_album_folders(folders: list, release_mbid: str,
                              albums: list = None) -> tuple:
    """Retag affected album folders with canonical metadata using mutagen (no beets)."""
    fields = {k: v for k, v in _canonical_release_fields(release_mbid).items() if v}
    if not fields.get("album") or not fields.get("albumartist"):
        return False, "Could not resolve canonical release metadata from MusicBrainz"
    artist = fields.get("albumartist", "")
    album  = fields.get("album", "")
    rgid   = fields.get("mb_releasegroupid", "")
    year   = fields.get("year", "")
    outputs = []
    ok_all  = True
    for folder in (folders or []):
        files = _audio_files_in_folder(folder, 200)
        if not files:
            outputs.append(f"{folder}: no audio files found")
            ok_all = False
            continue
        tagged = 0
        failed = []
        for f in files:
            existing = _audio_file_tags(f["path"])
            track_num = _tag_track_number(existing)
            tag_updates = {
                "album":             album,
                "albumartist":       artist,
                "artist":            existing.get("artist", artist) or artist,
                "mb_albumid":        release_mbid,
                "mb_releasegroupid": rgid,
                "year":              year or existing.get("date", "") or "",
                "track":             str(track_num) if track_num else "",
            }
            if _mutagen_write_tags(f["path"], tag_updates):
                tagged += 1
            else:
                failed.append(os.path.basename(f["path"]))
        outputs.append(f"{folder}: tagged {tagged}/{len(files)} file(s)")
        # A write that fails writes *nothing* — an unsupported format, or a file
        # the container can't write (the root-owned case in CLAUDE.md). Counting
        # successes without checking them reported "tagged 0/12", ok_all=True,
        # status "retagged" and a Navidrome rescan, so a merge that changed
        # nothing looked like it had worked.
        if tagged < len(files):
            ok_all = False
            outputs.append(
                f"{folder}: could not write {len(files) - tagged} file(s) "
                f"({', '.join(failed[:5])}{'…' if len(failed) > 5 else ''}) — "
                f"check the format is flac/opus/mp3 and that the files are "
                f"writable by the container's uid")
    return ok_all, "\n".join(outputs)

def apply_group_retag(group: dict) -> dict:
    preview = preview_group_retag(group)
    if not preview["ok"]:
        # The blocked list is the whole point — reporting only "Retag safety check
        # failed" gave the user nothing to act on, and the UI never showed even
        # that.
        reasons = "; ".join(preview.get("blocked") or []) or "unknown reason"
        return {"ok": False, "preview": preview,
                "output": f"Cannot merge: {reasons}"}
    canonical_id = group.get("canonical_album_id")
    affected_albums = [a for a in group.get("albums", [])
                       if a.get("id") != canonical_id]
    affected_folders = []
    for album in affected_albums:
        album_paths = [
            track.get("path", "") for track in album.get("tracks", [])
            if track.get("path", "") and _path_in_library(track.get("path", ""))
        ]
        # Every distinct folder, not just the first. A multi-disc or split copy
        # left its other folders untagged and still reported success.
        for folder in sorted({os.path.dirname(p) for p in album_paths}):
            if folder not in affected_folders:
                affected_folders.append(folder)
    ok_all, output = beets_merge_album_folders(affected_folders,
                                               group.get("canonical_mbid", ""),
                                               affected_albums)
    group["merge_mode"] = "retag"
    group["status"] = "retagged" if ok_all else "retag_failed"
    group["last_action"] = "retag"
    group["updated_at"] = time.time()
    group.setdefault("messages", []).append({
        "ts": time.time(),
        "kind": "retag",
        "ok": ok_all,
        "output": output[-4000:],
    })
    token = USERS[0]["telegram_token"] if USERS else ""
    if ok_all and token:
        _nd_scan_after_import(token)
    return {"ok": ok_all, "preview": preview, "output": output}

# ---------------------------------------------------------------------------
# Album grouping (Feature 2)
# ---------------------------------------------------------------------------

def group_missing_by_album(tracks: list, nd_user: str, nd_pass: str,
                           progress=None) -> tuple:
    """
    Group ALL missing tracks by their best MusicBrainz release, then check how
    many tracks from that release are already present in Navidrome.

    Strategy:
    - If the album is fully present (present_count == total_tracks): skip — you
      already have it. This track may just be tagged differently.
    - Otherwise: always treat as an album group regardless of how many tracks
      are missing. Downloading the full album folder gives consistent metadata.

    `progress` (optional) is called as progress(done, total) as releases are
    resolved, so callers can surface a live progress bar.

    Returns:
      album_groups: list of {release_mbid, artist, album, year,
                             total_tracks, present_count, missing_tracks}
      solo_tracks:  tracks with no MBID or no resolvable release.
    """
    print("  Grouping missing tracks by album (querying MusicBrainz)...")
    release_map      = {}
    no_mbid_count    = sum(1 for t in tracks if not t.get("mbid"))
    no_release_count = 0

    mbid_tracks = [t for t in tracks if t.get("mbid")]
    for done, track in enumerate(mbid_tracks, 1):
        release = mbz_best_release(track["mbid"])
        if progress:
            try:
                progress(done, len(mbid_tracks))
            except Exception:
                pass
        rel_id  = release.get("id", "")
        if not rel_id:
            no_release_count += 1
            print(f"    No release found: {track['artist']} - {track['title']}")
            continue
        if rel_id not in release_map:
            release_map[rel_id] = {
                "release_mbid":   rel_id,
                "artist":         track["artist"],
                "album":          release.get("title", ""),
                "year":           (release.get("date") or "")[:4],
                "missing_tracks": [],
                "total_tracks":   0,
                "present_count":  0,
            }
        release_map[rel_id]["missing_tracks"].append(track)

    album_groups    = []
    qualified_mbids = set()
    skipped_full    = []

    for rel_id, group in release_map.items():
        rel_tracks            = mbz_release_tracks(rel_id)
        group["total_tracks"] = len(rel_tracks)

        # Count how many tracks from this release are already in Navidrome.
        # MBID match first, then validated title+artist text match, so an
        # untagged library is not wrongly treated as missing the album.
        present = sum(
            1 for rt in rel_tracks
            if nd_track_present(group["artist"], rt["title"], rt["mbid"],
                                nd_user, nd_pass)
        )
        group["present_count"] = present

        if present >= group["total_tracks"] > 0:
            # Already have the full album — skip
            skipped_full.append(group)
            print(f"    Already complete: {group['artist']} - {group['album']} "
                  f"({present}/{group['total_tracks']})")
            continue

        group["missing_tracks"] = [
            {
                "artist": group["artist"],
                "title": rt["title"],
                "mbid": rt["mbid"],
                "position": rt.get("position", 0),
            }
            for rt in rel_tracks
            if not nd_track_present(group["artist"], rt["title"], rt["mbid"],
                                    nd_user, nd_pass)
        ]

        album_groups.append(group)
        for t in group["missing_tracks"]:
            qualified_mbids.add(t.get("mbid", ""))
        print(f"    Album group: {group['artist']} - {group['album']} "
              f"({len(group['missing_tracks'])} missing, "
              f"{present}/{group['total_tracks']} present)")

    solo_tracks = [t for t in tracks
                   if not t.get("mbid") or t.get("mbid") not in qualified_mbids]

    print(f"  Grouping summary: {len(album_groups)} album group(s), "
          f"{len(solo_tracks)} solo track(s) (no release), "
          f"{len(skipped_full)} already-complete album(s) skipped, "
          f"no MBID: {no_mbid_count}, no release: {no_release_count}")
    return album_groups, solo_tracks

# ---------------------------------------------------------------------------
# slskd
# ---------------------------------------------------------------------------

def _slskd_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if SLSKD_API_KEY:
        h["X-API-Key"] = SLSKD_API_KEY
    return h

# ---------------------------------------------------------------------------
# MP3 as a last resort, per album
#
# FORMAT_PRIORITY is flac/opus, globally. Some albums — bonus tracks, obscure
# singles — simply aren't seeded in either, and the fill can never complete. A
# review group can set allow_mp3, which widens the accepted formats *for that
# group's search and enqueue only*, with mp3 ranked strictly last so it is
# picked only when no flac/opus candidate covers the track.
#
# Scoped with a ContextVar rather than a parameter because the acceptance test
# happens six levels down (search -> folder scoring -> file pairing -> enqueue)
# and threading a flag through all of it would touch far more code than the
# feature is worth. A ContextVar, not a thread-local: asyncio.to_thread copies
# the calling context, so a scope opened in a coroutine still covers the
# blocking slskd calls it offloads — a thread-local would not. Plain
# threading.Thread workers get no context copy, which is why the album group
# also carries allow_mp3 explicitly for the poller's failover path.
# ---------------------------------------------------------------------------

MP3_FALLBACK_EXT = "mp3"
_mp3_fallback_on = contextvars.ContextVar("lb_allow_mp3", default=False)

def _accepted_formats() -> dict:
    """Extension -> priority for the operation running in this context."""
    if _mp3_fallback_on.get():
        last = max(FORMAT_PRIORITY.values(), default=0) + 1
        return {**FORMAT_PRIORITY, MP3_FALLBACK_EXT: last}
    return FORMAT_PRIORITY

def _placeable_formats() -> dict:
    """Formats placement will pick up off /downloads.

    Always wider than FORMAT_PRIORITY by mp3, regardless of the per-group flag: a
    group with allow_mp3 deliberately downloads mp3s, and by placement time the
    thread-local scope is long gone. Refusing to place a file we asked for would
    strand the fill with the audio sitting in /downloads. Acquisition stays
    flac/opus unless a group opts in — this only governs what is already on disk.
    """
    return {**FORMAT_PRIORITY,
            MP3_FALLBACK_EXT: max(FORMAT_PRIORITY.values(), default=0) + 1}

@contextlib.contextmanager
def _mp3_fallback(enabled: bool):
    token = _mp3_fallback_on.set(bool(enabled) or _mp3_fallback_on.get())
    try:
        yield
    finally:
        _mp3_fallback_on.reset(token)

# ── Per-fill quality preference ──────────────────────────────────────────────
# The Source-preferences `quality` setting is global, but "which copy do I want"
# is really a per-album decision: a 24/96 transfer is welcome for a record worth
# the disk and a waste for a single. So one download may override it, scoped the
# same way and for the same reason as the MP3 opt-in above — the preference is
# read six levels down, in file scoring and folder ranking.
# ---------------------------------------------------------------------------

_quality_override = contextvars.ContextVar("lb_quality_pref", default="")

def _effective_quality() -> str:
    """The quality preference governing the operation running in this context."""
    override = _quality_override.get()
    if override in QUALITY_PREFERENCES:
        return override
    return _effective_prefs()["quality"]

@contextlib.contextmanager
def _quality_preference(value: str):
    token = _quality_override.set(value if value in QUALITY_PREFERENCES else "")
    try:
        yield
    finally:
        _quality_override.reset(token)

# ── Text normalization & fuzzy matching ──────────────────────────────────────
# Shared by the query builder (§1), folder ranking (§2) and the file↔track
# matcher (§3), so all three agree on what two strings "being the same" means.
#
# rapidfuzz is a declared dependency, but it is imported defensively for the
# same reason mutagen is: this sits on the acquisition hot path, and an
# ImportError at module load would take the whole bot down over a lagging image
# build. The fallback is a real (if slower) implementation, not a stub that
# quietly disables matching — a silently degraded matcher is the failure mode
# this whole plan exists to fix.
try:
    from rapidfuzz import fuzz as _fuzz
except Exception:  # pragma: no cover - exercised only without the wheel
    import difflib

    class _fuzz:  # noqa: N801 - mirrors rapidfuzz's module-level API
        @staticmethod
        def ratio(a, b):
            return difflib.SequenceMatcher(None, a, b).ratio() * 100.0

        @staticmethod
        def token_sort_ratio(a, b):
            return _fuzz.ratio(" ".join(sorted(a.split())), " ".join(sorted(b.split())))

        @staticmethod
        def partial_ratio(a, b):
            short, long = (a, b) if len(a) <= len(b) else (b, a)
            if not short:
                return 0.0
            n = len(short)
            return max((_fuzz.ratio(short, long[i:i + n])
                        for i in range(max(1, len(long) - n + 1))), default=0.0)

    print("  rapidfuzz not installed — using the slower difflib fallback for matching")

# Bracketed metadata peers and MusicBrainz both bolt onto a title:
# "(Deluxe Edition)", "[2014 Remaster]", "{FLAC}", "(Disc 1)". Stripping these
# is what stops a search for the canonical MB title missing every peer folder
# that named the plain album.
_EDITION_NOISE_RE = re.compile(
    r"[\(\[\{]\s*(?:"
    r"\d{4}\s+)?(?:"
    r"deluxe|expanded|special|limited|collector'?s|anniversary|legacy|"
    r"remaster(?:ed)?|remastered\s+version|reissue|re-?issue|bonus(?:\s+track)?s?|"
    r"explicit|clean|mono|stereo|edition|version|disc\s*\d+|cd\s*\d+|"
    r"flac|mp3|aac|opus|vinyl|web|cd|24\s*bit|16\s*bit|\d{2,3}\s*kbps"
    r")[^\)\]\}]*[\)\]\}]",
    re.IGNORECASE)
# The same noise when a peer wrote it bare, after a dash: "Album - 2014 Remaster".
_TRAILING_NOISE_RE = re.compile(
    r"\s*[-–—]\s*(?:\d{4}\s+)?"
    r"(?:remaster(?:ed)?|reissue|deluxe|expanded|mono|stereo|explicit|clean)"
    r"(?:\s+(?:edition|version))?\s*$",
    re.IGNORECASE)

# "Part II" / "Pt. 2" / "Vol. Three" all name the same thing. Mapped to digits
# so the fuzzy tier sees one string rather than two unrelated ones.
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7,
          "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12}
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                 "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
                 "twelve": 12}
# Words that carry no identifying weight, so they must not decide a fuzzy score
# or survive into a "distinctive words" query.
_STOPWORDS = {"the", "a", "an", "and", "of", "feat", "ft", "featuring", "with",
              "vs", "versus"}

def _strip_diacritics(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value or "")
    return "".join(c for c in folded if not unicodedata.combining(c))

def _clean_album_title(album: str) -> str:
    """An album title with edition/format packaging removed.

    MusicBrainz canonical titles carry disambiguation a peer's folder name never
    will ("Album (Deluxe Edition)"), and searching the full string is a reliable
    way to match nothing at all.
    """
    out = _EDITION_NOISE_RE.sub(" ", album or "")
    out = _TRAILING_NOISE_RE.sub("", out)
    out = re.sub(r"\s+", " ", out).strip(" -–—_")
    # Never strip the title down to nothing: a release genuinely called
    # "Remastered" would otherwise become an empty query matching everything.
    return out or (album or "").strip()

def _match_words(value: str) -> str:
    """Word-preserving normalized form: same folding as `_match_key`, but with
    single spaces kept between tokens so the token-based fuzzy ratios have
    tokens to work with. Roman numerals and number words become digits."""
    folded = _strip_diacritics(value or "").lower()
    folded = re.sub(r"\b(?:pt|part|vol|volume|no|nr)\b\.?", " part ", folded)
    tokens = [t for t in re.split(r"[^a-z0-9]+", folded) if t]
    out = []
    for t in tokens:
        if t in _ROMAN and out and out[-1] == "part":
            out.append(str(_ROMAN[t]))
        elif t in _NUMBER_WORDS:
            out.append(str(_NUMBER_WORDS[t]))
        else:
            out.append(t)
    return " ".join(out)

def _significant_words(value: str) -> list:
    """Normalized tokens with stopwords and bare numbers dropped — what is left
    is what actually identifies the thing."""
    return [t for t in _match_words(value).split()
            if t not in _STOPWORDS and not t.isdigit()]

def _fuzzy_score(a: str, b: str) -> float:
    """Best of token-sort and partial ratio over word-preserving forms, 0-100.

    Two ratios because they fail differently: token-sort handles reordering and
    punctuation drift, partial handles one string being a fragment of the other
    (a peer's folder name inside a longer path component).
    """
    wa, wb = _match_words(a), _match_words(b)
    if not wa or not wb:
        return 0.0
    return max(float(_fuzz.token_sort_ratio(wa, wb)),
               float(_fuzz.partial_ratio(wa, wb)))

def _folder_name_score(target: str, candidate: str) -> float:
    """How much a peer's path component names `target`, 0-100.

    Not `_fuzzy_score`: `partial_ratio` scores any superstring at 100, so
    "Led Zeppelin" matched the folder "Led Zeppelin/Physical Graffiti" perfectly
    and every album in the discography tied with the one being searched for.
    Token-sort is the base because it *does* pay for extra words; the partial
    arm is allowed back in only when the candidate adds at most one token —
    room for a year or an edition word, not for a different album's title.
    """
    # The candidate gets the same treatment the query does. Peers label folders
    # "Artist - Album [1969 FLAC]", and both the format tag and the repeated
    # artist run are extra tokens that would otherwise sink a perfect match.
    wt = _dedupe_terms(_match_words(_clean_album_title(target)))
    wc = _dedupe_terms(_match_words(_clean_album_title(candidate)))
    if not wt or not wc:
        return 0.0
    score = float(_fuzz.token_sort_ratio(wt, wc))
    target_tokens = set(wt.split())
    extra = len([t for t in wc.split() if t not in target_tokens])
    if extra <= 1:
        score = max(score, float(_fuzz.partial_ratio(wt, wc)))
    return score

def _tokens_aligned(a: str, b: str) -> bool:
    """Whether the shorter string's tokens all appear as *whole* tokens in the
    longer one.

    This is the guard rail on `partial_ratio`, which scores any substring at
    100: "sing" sits inside "singularity" and would otherwise pass the fuzzy
    tier outright — the exact mis-pairing that put a second copy of a song into
    an album while the real gap stayed open. "song" against "song feat x" is a
    whole token and still passes, which is the case the looser matcher is for.
    """
    ta, tb = _match_words(a).split(), _match_words(b).split()
    if not ta or not tb:
        return False
    short, long = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return set(short) <= set(long)

def _score_file(f: dict, upload_speed: int):
    """Score a single file for format/live/compilation filtering only.

    Lower is better (it is a sort key, not a points total). The bitrate term
    follows the user's quality preference rather than always favouring the
    largest file: "highest-bitrate" still prefers more, but a CD-standard
    preference should not be talked out of a 16/44 FLAC by a 24/96 one.
    """
    filename = f.get("filename", "")
    ext      = _file_ext(filename)
    accepted = _accepted_formats()
    if ext not in accepted:
        return None
    bitrate = f.get("bitRate", 0) or 0
    preference = _effective_quality()
    if preference == "flac-16-44":
        depth = int(f.get("bitDepth") or 0)
        rate = int(f.get("sampleRate") or 0)
        # 0 = unknown, and unknown is much more common than hi-res, so it is
        # treated as the standard case rather than penalized.
        quality_rank = 0 if (depth in (0, 16) and rate in (0, 44100)) else 1
    elif preference == "prefer-opus":
        quality_rank = 0 if ext == "opus" else 1
    else:
        quality_rank = 0
    return (
        1 if COMPILATION_RE.search(filename) else 0,
        1 if LIVE_RE.search(filename) else 0,
        accepted[ext],
        quality_rank,
        -(upload_speed or 0),
        -bitrate,
    )

# A folder name has to look at least this much like the album before it counts
# as "the album we asked for" rather than a coincidence.
ALBUM_MATCH_THRESHOLD = 80.0

def _annotate_folder_match(fd: dict, album: str, artist: str, year: str = "") -> None:
    """Score a peer folder's *name* against the album and artist we searched for.

    The last two path components are what carry the album name — peers file
    things as `Artist/Album/` or `.../Artist - Album (1969)/` — so both the leaf
    and the leaf-plus-parent are tried and the best score kept. Sets
    `album_match` / `artist_match` (0-100), `album_match_ok`, and
    `year_in_path`; all default to 0/False, so a caller with no album to
    compare against (the single-track search path) simply gets no bonus.
    """
    fd["album_match"] = 0.0
    fd["artist_match"] = 0.0
    fd["album_match_ok"] = False
    fd["year_in_path"] = False
    path = (fd.get("folder") or "").replace("\\", "/").strip("/")
    if not path:
        return
    parts = [p for p in path.split("/") if p]
    candidates = [parts[-1]] if parts else []
    if len(parts) >= 2:
        candidates.append(f"{parts[-2]} {parts[-1]}")
    if album:
        fd["album_match"] = round(max((_folder_name_score(album, c) for c in candidates),
                                      default=0.0), 1)
        fd["album_match_ok"] = fd["album_match"] >= ALBUM_MATCH_THRESHOLD
    if artist:
        fd["artist_match"] = round(max((_folder_name_score(artist, c) for c in candidates),
                                       default=0.0), 1)
    if year and re.search(rf"\b{re.escape(str(year))}\b", path):
        fd["year_in_path"] = True

def _folder_quality_profile(files: list) -> dict:
    """Dominant codec / bitrate / bit-depth / sample-rate across a folder."""
    audio = [f for f in files or []
             if _file_ext(f.get("filename", "")) in _accepted_formats()]
    if not audio:
        return {}
    def _mode(values):
        vals = [v for v in values if v]
        return max(set(vals), key=vals.count) if vals else 0
    exts = [_file_ext(f.get("filename", "")) for f in audio]
    return {
        "codec": max(set(exts), key=exts.count),
        "bitrate": _mode(f.get("bitRate") or f.get("bitrate") for f in audio),
        "bit_depth": _mode(f.get("bitDepth") for f in audio),
        "sample_rate": _mode(f.get("sampleRate") for f in audio),
    }

def _quality_preference_score(profile: dict, preference: str) -> int:
    """0-600, how well a folder's dominant quality suits the user's preference."""
    if not profile:
        return 0
    codec = profile.get("codec", "")
    bitrate = int(profile.get("bitrate") or 0)
    depth = int(profile.get("bit_depth") or 0)
    rate = int(profile.get("sample_rate") or 0)
    if preference == "flac-16-44":
        # Archival-standard CD rip. A 24/96 copy is not "better" here — it is
        # bigger, and the user asked for the standard one.
        if codec != "flac":
            return 0
        if depth in (0, 16) and rate in (0, 44100):
            return 600
        return 300
    if preference == "prefer-opus":
        if codec == "opus":
            return 600
        return 200 if codec == "flac" else 0
    if preference == "highest-bitrate":
        if codec == "flac":
            return 600 if depth >= 24 else 500
        return min(500, int(bitrate * 500 / 320)) if bitrate else 100
    # flac-any (default): any lossless copy, bit depth irrelevant.
    return 600 if codec == "flac" else 200

def _score_folder(folder: dict, expected_track_count: int = 0,
                  ignore_guards: bool = False) -> int:
    """
    Score a folder (peer+directory group) using Tubifarry's CalculatePriority logic.
    Higher = better. Returns 0 for locked/unusable sources or sources rejected
    by the user's guard preferences (System → Source preferences).
    """
    files         = folder["files"]
    file_count    = len(files)
    upload_speed  = folder.get("upload_speed", 0) or 0
    queue_length  = folder.get("queue_length", 0) or 0
    free_slot     = folder.get("has_free_upload_slot", False)

    if file_count == 0:
        return 0

    # Availability is measured against everything the peer offered in this
    # folder, not against the flac/opus files that survived _score_file:
    # `files` is already format-filtered, so scoring the ratio on file_count
    # meant a peer with locked files elsewhere in its share zeroed out a folder
    # holding perfectly good FLACs.
    #
    # `locked_count` must be folder-local for the same reason. slskd's
    # lockedFileCount is peer-WIDE, and a search response's `files` are by
    # definition the unlocked ones (locked hits arrive separately, in
    # `lockedFiles`). Comparing the two zeroed every peer that locks any part of
    # its share against the handful of files it returned for this query — and
    # because this check sits above the guard block, the "pick best" fallback
    # could not rescue it either. That is a whole search coming back empty from
    # peers that were offering exactly what was asked for. Count only the locked
    # files slskd attributed to *this* folder; when it attributes none, the
    # folder is fully available.
    raw_count    = int(folder.get("raw_file_count") or 0) or file_count
    locked_count = min(int(folder.get("locked_in_folder") or 0), raw_count)
    if locked_count >= raw_count:
        return 0

    avail_ratio = (raw_count - locked_count) / raw_count
    if avail_ratio <= 0.5:
        return 0

    if not ignore_guards:
        g = _effective_prefs()["guards"]
        if g.get("minSpeedMbps") and upload_speed:
            speed_mbps = upload_speed / (1024.0 * 1024.0 / 8.0)
            if speed_mbps < float(g["minSpeedMbps"]):
                return 0
        if g.get("maxQueueLength") and queue_length > int(g["maxQueueLength"]):
            return 0
        if g.get("maxAlbumSizeMB"):
            total_mb = sum((f.get("size") or 0) for f in files) / (1024.0 * 1024.0)
            if total_mb > float(g["maxAlbumSizeMB"]):
                return 0
        # Coverage guard: slskd only returns files matching the query, so a
        # complete album can look short here — this guard trades recall for
        # certainty, which is why it defaults off.
        if g.get("requireFullCoverage") and expected_track_count > 0:
            audio = [f for f in files
                     if _file_ext(f.get("filename", "")) in _accepted_formats()]
            if len(audio) < expected_track_count:
                return 0

    score = 0

    # Track count match (0–2500)
    # NOTE: slskd search only returns files whose names match the query, so a
    # complete 12-track album can legitimately show as 3 files here. We must NOT
    # reject "too few files" — expand_directory fetches the full folder later.
    # We only award a bonus when the visible count is close to expected.
    if expected_track_count > 0:
        audio_files = [f for f in files
                       if _file_ext(f.get("filename", "")) in _accepted_formats()]
        actual      = len(audio_files)
        if actual > 0:
            diff = actual - expected_track_count
            if diff < 0:
                score += int(2500 * (2.718 ** (-(abs(diff) ** 2) * 5)))
            elif diff == 0:
                score += 2500
            else:
                score += int(2500 * (2.718 ** (-(diff ** 2) * 1.5)))

    # Availability ratio (0–2000)
    score += int((avail_ratio ** 2.0) * 2000)

    # Upload speed (0–1800) — log scale
    if upload_speed > 0:
        speed_mbps = upload_speed / (1024.0 * 1024.0 / 8.0)
        import math
        score += min(1800, int(math.log10(max(0.1, speed_mbps) + 1) * 1100))

    # Queue length (50–1500) — exponential decay
    import math
    queue_factor = math.pow(0.94, min(queue_length, 40))
    score += int(queue_factor * 1500)

    # Free upload slot (+800)
    score += 800 if free_slot else 0

    # Collection size (0–300)
    score += min(300, int(math.log10(max(1, file_count) + 1) * 150))

    # Album-name match (0–3000) — the largest single term, deliberately.
    # Everything above this point is a property of the *peer*, not of the
    # album: for an ambiguous query (a self-titled album is the worst case,
    # since "Artist Album" collapses to the artist's name) the whole
    # discography came back and sorted itself by upload speed. A folder whose
    # name doesn't resemble the album now earns nothing here, so it cannot
    # outrank one that does on peer metrics alone.
    album_match = float(folder.get("album_match") or 0)
    if album_match >= ALBUM_MATCH_THRESHOLD:
        span = max(1.0, 100.0 - ALBUM_MATCH_THRESHOLD)
        score += int(3000 * min(1.0, (album_match - ALBUM_MATCH_THRESHOLD) / span))
    # The artist agreeing is weaker evidence — a discography folder matches the
    # artist perfectly — so it is worth a fraction of the album term.
    artist_match = float(folder.get("artist_match") or 0)
    if artist_match >= ALBUM_MATCH_THRESHOLD:
        score += 250

    # Year in the path (+200): weak on its own, decisive between two pressings.
    if folder.get("year_in_path"):
        score += 200

    # Desired quality (0–600)
    score += _quality_preference_score(
        _folder_quality_profile(files), _effective_quality())

    return max(0, min(score, 14000))

def _no_source_reason(stats: dict) -> str:
    """Why a search that saw peers and files still yielded nothing to pick.

    "No source found on slskd" after a progress line that just said "103 peers,
    2047 files" reads as a bug in the bot. It usually isn't: the files were all
    in a format we reject, or every peer had them locked. Both facts are known
    at the point the result is thrown away, so say which one it was.
    """
    peers = int(stats.get("peers") or 0)
    files = int(stats.get("files") or 0)
    folders = int(stats.get("folders") or 0)
    rejected = [f for f in (stats.get("rejected_formats") or []) if f]
    accepted = ", ".join(stats.get("accepted_formats") or ()).upper() or "an accepted format"
    # A failed call is not an empty library. Say what broke, and keep the peer
    # count next to it so the sentence agrees with the progress line the user
    # just watched.
    if stats.get("error"):
        seen = f"{peers} peer(s) had answered, but " if peers else ""
        return f"{seen}the search failed — {stats['error']}"
    if not peers:
        # The progress bar counts slskd's live responseCount; the sources come
        # from the responses endpoint. When those disagree, saying "no peer
        # answered" contradicts what the user just watched, and blames the wrong
        # thing — slskd had the peers, it just didn't hand them over.
        counted = int(stats.get("counted_peers") or 0)
        if counted:
            return (f"slskd counted {counted} peer(s) but returned none of their "
                    f"responses — it had not finished publishing them")
        return "No peer answered the search"
    if not files:
        return f"{peers} peer(s) answered, but none offered any files"
    if not folders:
        seen = ", ".join(rejected[:6]) or "unknown"
        return (f"{peers} peer(s) offered {files:,} file(s), but none in {accepted} "
                f"(they were {seen})")
    return (f"{peers} peer(s) offered {files:,} file(s) in {folders} folder(s), but every "
            f"one was locked or less than half available")

def slskd_run_search(query: str, expected_track_count: int = 0,
                     progress=None, stats: dict = None,
                     album: str = "", artist: str = "", year: str = "") -> list:
    """
    Run a slskd search and return a list of FOLDER-level result dicts,
    one per (username, directory) pair, sorted best-first by _score_folder.

    `album`/`artist`/`year` are what the folder is scored *against*. Without
    them the ranking knew only peer metrics — speed, queue, free slots — so for
    an ambiguous query (a self-titled album, say) an unrelated album from a fast
    peer outranked the album actually being looked for. They are optional
    because the single-track path has no album to compare with.

    Each entry: {username, folder, files, upload_speed, has_free_upload_slot,
                 queue_length, locked_file_count, score, best_file_score,
                 album_match, artist_match, album_match_ok}

    `progress(text)` is called once per poll with what has arrived so far. This
    is the only honest progress a slskd search has — there is no total to count
    towards — and without it the UI could say nothing for the 30-90s this takes.
    """
    def _say(text):
        if progress:
            try:
                progress(text)
            except Exception:
                pass

    # Accounting, kept current as the search goes rather than written once at the
    # end. Every `return []` below is a real failure mode, and reporting an empty
    # dict for those made the UI say "No peer answered the search" about a search
    # that had just counted 132 of them.
    acc = {"query": query, "peers": 0, "files": 0, "folders": 0, "usable": 0,
           "rejected_formats": [], "error": "",
           # Captured here because _accepted_formats() is scoped by _mp3_fallback,
           # and whatever renders this runs long after that scope has closed.
           "accepted_formats": sorted(_accepted_formats(), key=_accepted_formats().get)}

    def _publish():
        if stats is None:
            return
        # slskd_search_album_folders hands the same dict to both passes; keep
        # whichever got further into the network rather than letting a fallback
        # that died on the POST erase what the first pass actually saw.
        if not stats or acc["peers"] >= int(stats.get("peers") or 0):
            stats.clear()
            stats.update(acc)

    _say(f"Asking slskd for “{query}”…")
    try:
        r = _http.post(f"{SLSKD_URL}/api/v0/searches",
                          headers=_slskd_headers(),
                          json={"searchText": query}, timeout=SEARCH_HTTP_TIMEOUT)
        if not r.ok:
            print(f"  slskd POST failed ({r.status_code}): {r.text[:200]}")
            acc["error"] = f"slskd refused the search (HTTP {r.status_code})"
            _publish()
            return []
        search_id = r.json().get("id")
        if not search_id:
            print(f"  slskd no id for '{query}'")
            acc["error"] = "slskd accepted the search but returned no search id"
            _publish()
            return []

        # Poll until slskd says the search is complete. Not optional, and not a
        # tuning choice: slskd publishes NOTHING until then. Traced against the
        # live instance, /searches/{id}/responses and ?includeResponses=true both
        # returned an empty array at 5s, 16s, 26s and 35s while responseCount
        # climbed to 143 — and both returned all 142 the instant the state
        # flipped to "Completed, TimedOut" at 39.4s.
        #
        # So there is no partial result to leave early with. What the early exit
        # can still do is end the search *sooner*: once peers have stopped
        # arriving, ask slskd to stop it rather than waiting out its own
        # straggler timeout, then read the results it publishes on completion.
        started  = time.time()
        deadline = started + SEARCH_TIMEOUT
        last_files, plateau = -1, 0
        stop_sent = False
        while True:
            s = _http.get(f"{SLSKD_URL}/api/v0/searches/{search_id}",
                             headers=_slskd_headers(), timeout=SEARCH_HTTP_TIMEOUT)
            body      = s.json() if s.ok else {}
            elapsed   = time.time() - started
            responses = int(body.get("responseCount") or 0)
            files     = int(body.get("fileCount") or 0)
            # Before the completion check, not after: it exits the loop, and a
            # failure further down still has to say how many peers had answered.
            acc["peers"], acc["files"] = responses, files
            if body.get("isComplete") or str(body.get("state", "")).startswith("Completed"):
                break
            if not stop_sent and elapsed >= SEARCH_MIN_WAIT and responses:
                enough = responses >= SEARCH_MIN_RESPONSES
                plateau = plateau + 1 if files == last_files else 0
                if enough or plateau >= 2:
                    # Best effort. If slskd won't stop it, the loop simply keeps
                    # polling to the deadline as it otherwise would.
                    stop_sent = True
                    _say(f"“{query}”: {responses} peer(s) in — asking slskd to wrap up…")
                    try:
                        _http.put(f"{SLSKD_URL}/api/v0/searches/{search_id}",
                                  headers=_slskd_headers(), timeout=SEARCH_HTTP_TIMEOUT)
                    except Exception:
                        pass
            last_files = files
            if not stop_sent:
                _say(f"“{query}”: {responses} peer(s), {files} file(s) — {elapsed:.0f}s")
            if time.time() + SEARCH_POLL_INT >= deadline:
                break
            time.sleep(SEARCH_POLL_INT)
        _say(f"“{query}”: ranking {responses} peer(s)…")
        # The count the *status* endpoint reported. The results come from a
        # different endpoint, and the two can disagree — see below.
        counted = responses
        acc["counted_peers"] = counted

        # slskd's responseCount ticks up live, but /responses serves them from
        # the finished search. Breaking out of the poll as soon as enough peers
        # had answered — the whole point of the early exit — therefore asks for
        # results slskd has counted but not yet published, and gets an empty
        # array. The search reports 140 peers and then no sources, which is
        # exactly what was happening on every album whose stragglers kept the
        # search in progress past the deadline.
        #
        # Recovery is tried cheapest-first, and only when the array came back
        # *empty* — a short-but-non-empty result is a normal ranking outcome and
        # must not cost anyone 25s.
        last_status = {"code": 0, "text": ""}

        def _fetch_responses():
            """The dedicated endpoint. Returns a list, or None if unreadable."""
            r = _http.get(f"{SLSKD_URL}/api/v0/searches/{search_id}/responses",
                          headers=_slskd_headers(), timeout=SEARCH_TIMEOUT + 10)
            last_status["code"], last_status["text"] = r.status_code, r.text[:200]
            if not r.ok:
                return None
            try:
                parsed = r.json()
            except Exception:
                return None
            return parsed if isinstance(parsed, list) else None

        def _fetch_inline_responses():
            """The search object with its responses attached, which slskd will
            serve for a search that is still running. Returns a list or None."""
            try:
                r = _http.get(f"{SLSKD_URL}/api/v0/searches/{search_id}",
                              headers=_slskd_headers(),
                              params={"includeResponses": "true"},
                              timeout=SEARCH_TIMEOUT + 10)
                if not r.ok:
                    return None
                parsed = r.json()
            except Exception:
                return None
            if isinstance(parsed, dict) and isinstance(parsed.get("responses"), list):
                return parsed["responses"]
            return None

        responses = _fetch_responses()

        if counted and not responses:
            inline = _fetch_inline_responses()
            if inline:
                responses = inline
                acc["recovered_via"] = "inline"

        if counted and not responses:
            _say(f"“{query}”: waiting for slskd to publish {counted} response(s)…")
            settle_until = time.time() + SEARCH_SETTLE_TIMEOUT
            while time.time() < settle_until:
                time.sleep(SEARCH_POLL_INT)
                st = _http.get(f"{SLSKD_URL}/api/v0/searches/{search_id}",
                               headers=_slskd_headers(), timeout=SEARCH_HTTP_TIMEOUT)
                stb = st.json() if st.ok else {}
                counted = max(counted, int(stb.get("responseCount") or 0))
                if str(stb.get("state", "")).startswith("Completed"):
                    break
            acc["counted_peers"] = counted
            settled = _fetch_responses()
            if not settled:
                # None (unreadable) or [] (still nothing) — the inline form is
                # the last thing worth trying before giving up.
                settled = _fetch_inline_responses() or settled
            if settled is not None:
                responses = settled
                if settled:
                    acc["recovered_via"] = "settled"

        # Always delete the search to release slskd's one-at-a-time lock
        try:
            _http.delete(f"{SLSKD_URL}/api/v0/searches/{search_id}",
                            headers=_slskd_headers(), timeout=10)
        except Exception:
            pass

        if responses is None:
            print(f"  slskd responses failed ({last_status['code']}): {last_status['text']}")
            acc["error"] = (f"slskd counted {counted} peer(s) but returned "
                            f"HTTP {last_status['code']} for the results")
            _publish()
            return []

        responses     = [p for p in responses if isinstance(p, dict)]
        total_peers   = len(responses)
        total_files   = sum(len(p.get("files") or []) for p in responses)
        rejected_fmts = set()
        folder_map    = {}  # (username, folder_path) -> folder dict

        for peer in responses:
            # `or []`, not just a default: slskd sends `"files": null` for a peer
            # whose only hits were locked, and `for f in None` raised out of the
            # whole loop. One such peer discarded every other peer's results --
            # 139 answered, nothing surfaced -- and the exception was swallowed
            # into a bare "no sources". Peers that lock part of their share are
            # exactly the ones likely to answer this way.
            peer_files   = peer.get("files") or []
            locked_files = peer.get("lockedFiles") or []
            username          = peer.get("username", "")
            upload_speed      = peer.get("uploadSpeed", 0) or 0
            has_free_slot     = peer.get("hasFreeUploadSlot", False)
            queue_length      = peer.get("queueLength", 0) or 0
            locked_file_count = peer.get("lockedFileCount", 0) or 0

            # Every file the peer offered in each folder, before the format
            # filter below throws most of them away. _score_folder needs this:
            # lockedFileCount is peer-wide, so dividing it by the surviving
            # flac/opus count compares two different populations.
            raw_counts = {}
            for f in peer_files:
                raw_key = (username, _folder(f.get("filename", "")))
                raw_counts[raw_key] = raw_counts.get(raw_key, 0) + 1

            # Locked hits, attributed to the folder they are actually in. The
            # peer-wide lockedFileCount cannot be used for this — see the
            # availability note in _score_folder.
            locked_counts = {}
            for f in locked_files:
                lk = (username, _folder(f.get("filename", "")))
                locked_counts[lk] = locked_counts.get(lk, 0) + 1

            for f in peer_files:
                file_score = _score_file(f, upload_speed)
                if file_score is None:
                    rejected_fmts.add(_file_ext(f.get("filename", "")))
                    continue

                folder_path = _folder(f.get("filename", ""))
                key = (username, folder_path)

                if key not in folder_map:
                    # Preserve original backslash path for the directory API
                    orig = f.get("filename", "")
                    raw_fp = (orig.rsplit("\\", 1)[0] if "\\" in orig
                              else orig.replace("/", "\\").rsplit("\\", 1)[0])
                    folder_map[key] = {
                        "username":             username,
                        "folder":               folder_path,   # forward slashes (display)
                        "raw_folder":           raw_fp,        # backslashes (API)
                        "files":                [],
                        "raw_file_count":       raw_counts.get(key, 0),
                        "upload_speed":         upload_speed,
                        "has_free_upload_slot": has_free_slot,
                        "queue_length":         queue_length,
                        "locked_file_count":    locked_file_count,   # peer-wide, display only
                        "locked_in_folder":     locked_counts.get(key, 0),
                        "best_file_score":      file_score,
                        "score":                0,
                    }
                folder_map[key]["files"].append(f)
                if file_score < folder_map[key]["best_file_score"]:
                    folder_map[key]["best_file_score"] = file_score

        # How much each folder's own name looks like the album we asked for.
        # Computed once here rather than inside _score_folder, because the
        # picker and the positional matcher both read it back later.
        for fd in folder_map.values():
            _annotate_folder_match(fd, album, artist, year)

        # Score each folder and sort best-first
        folders = list(folder_map.values())
        for fd in folders:
            fd["score"] = _score_folder(fd, expected_track_count)

        folders = [fd for fd in folders if fd["score"] > 0]
        if not folders and folder_map and _effective_prefs()["fallback"] == "best":
            # Guards rejected every candidate; "Pick best" fallback rescores
            # without guards rather than leaving the gap stuck. "ask"/"skip"
            # intentionally return nothing so the decision surfaces in the UI.
            folders = list(folder_map.values())
            for fd in folders:
                fd["score"] = _score_folder(fd, expected_track_count, ignore_guards=True)
            folders = [fd for fd in folders if fd["score"] > 0]
        folders.sort(key=lambda x: -x["score"])

        # `folders` here is the count *after* scoring; acc["folders"] is
        # folder_map, before it. The gap between the two is exactly the
        # locked/availability rejection, which is what makes an empty result
        # attributable to a stage rather than just empty.
        acc.update({"peers": total_peers, "files": total_files,
                    "folders": len(folder_map), "usable": len(folders),
                    "rejected_formats": sorted(f for f in rejected_fmts if f)})
        _publish()

        print(f"  slskd '{query}': {total_peers} peers, {total_files} files, "
              f"{len(folders)} folder results in {time.time() - started:.1f}s, "
              f"rejected formats: {rejected_fmts or 'none'}")
        return folders
    except Exception as e:
        print(f"  slskd exception for '{query}': {e}")
        acc["error"] = f"{type(e).__name__}: {e}"
        _publish()
        # Attempt cleanup even on error so the lock is released
        try:
            if "search_id" in dir():
                _http.delete(f"{SLSKD_URL}/api/v0/searches/{search_id}",
                                headers=_slskd_headers(), timeout=5)
        except Exception:
            pass
        return []

def slskd_search_probe(query: str, wait: float = 45.0, stop_at: float = 0.0) -> dict:
    """Trace one slskd search end to end, reporting raw API shapes.

    Reasoning from the outside about why a search that counts 143 peers returns
    none of them has been wrong twice. This runs the same lifecycle
    slskd_run_search does and reports exactly what each endpoint said, including
    slskd's own effective search options — which can filter responses server-side
    while still counting them. Read-only apart from creating and deleting one
    search, and it takes the API key the bot already holds so no credential has
    to be handled by hand.
    """
    trace = {"query": query, "polls": [], "probes": [], "options": {}, "error": ""}

    def _probe(search_id, elapsed):
        entry = {"at": round(elapsed, 1)}
        try:
            r = _http.get(f"{SLSKD_URL}/api/v0/searches/{search_id}/responses",
                          headers=_slskd_headers(), timeout=SEARCH_HTTP_TIMEOUT)
            entry["responses_code"] = r.status_code
            body = r.json() if r.ok else None
            entry["responses_type"] = type(body).__name__
            entry["responses_len"] = len(body) if isinstance(body, (list, dict)) else None
            if isinstance(body, list) and body:
                first = body[0] if isinstance(body[0], dict) else {}
                entry["sample_keys"] = sorted(first.keys())[:12]
                entry["sample_file_count"] = len(first.get("files") or [])
        except Exception as e:
            entry["responses_error"] = f"{type(e).__name__}: {e}"
        try:
            r = _http.get(f"{SLSKD_URL}/api/v0/searches/{search_id}",
                          headers=_slskd_headers(),
                          params={"includeResponses": "true"},
                          timeout=SEARCH_HTTP_TIMEOUT)
            entry["inline_code"] = r.status_code
            body = r.json() if r.ok else {}
            entry["inline_keys"] = sorted(body.keys())[:16] if isinstance(body, dict) else None
            inline = body.get("responses") if isinstance(body, dict) else None
            entry["inline_len"] = len(inline) if isinstance(inline, list) else None
        except Exception as e:
            entry["inline_error"] = f"{type(e).__name__}: {e}"
        trace["probes"].append(entry)

    try:
        # slskd serves its running configuration here; the search filters live
        # under it and are the "is this tweakable in slskd" answer.
        try:
            r = _http.get(f"{SLSKD_URL}/api/v0/options",
                          headers=_slskd_headers(), timeout=SEARCH_HTTP_TIMEOUT)
            opts = r.json() if r.ok else {}
            trace["options"] = {k: v for k, v in (opts or {}).items()
                                if k in ("searches", "filters", "global", "soulseek")}
        except Exception as e:
            trace["options_error"] = f"{type(e).__name__}: {e}"

        r = _http.post(f"{SLSKD_URL}/api/v0/searches", headers=_slskd_headers(),
                       json={"searchText": query}, timeout=SEARCH_HTTP_TIMEOUT)
        trace["post_code"] = r.status_code
        if not r.ok:
            trace["error"] = r.text[:300]
            return trace
        search_id = r.json().get("id")
        trace["search_id"] = search_id

        started = time.time()
        next_probe = 5.0
        stopped = False
        while True:
            elapsed = time.time() - started
            # Does asking slskd to end the search early make it publish sooner?
            # That is the whole speed argument for the early exit now that
            # partial reads are known to be impossible.
            if stop_at and not stopped and elapsed >= stop_at:
                stopped = True
                try:
                    r = _http.put(f"{SLSKD_URL}/api/v0/searches/{search_id}",
                                  headers=_slskd_headers(), timeout=SEARCH_HTTP_TIMEOUT)
                    trace["stop"] = {"at": round(elapsed, 1), "code": r.status_code,
                                     "body": r.text[:200]}
                except Exception as e:
                    trace["stop"] = {"at": round(elapsed, 1),
                                     "error": f"{type(e).__name__}: {e}"}
            s = _http.get(f"{SLSKD_URL}/api/v0/searches/{search_id}",
                          headers=_slskd_headers(), timeout=SEARCH_HTTP_TIMEOUT)
            body = s.json() if s.ok else {}
            trace["polls"].append({"at": round(elapsed, 1),
                                   "state": body.get("state", ""),
                                   "responseCount": body.get("responseCount"),
                                   "fileCount": body.get("fileCount")})
            if elapsed >= next_probe:
                _probe(search_id, elapsed)
                next_probe += 10.0
            if str(body.get("state", "")).startswith("Completed") or elapsed >= wait:
                break
            time.sleep(1.0)
        _probe(search_id, time.time() - started)
    except Exception as e:
        trace["error"] = f"{type(e).__name__}: {e}"
    finally:
        try:
            if trace.get("search_id"):
                _http.delete(f"{SLSKD_URL}/api/v0/searches/{trace['search_id']}",
                             headers=_slskd_headers(), timeout=10)
        except Exception:
            pass
    return trace

def slskd_pick(folders: list) -> dict:
    """
    Pick the best single file from the best folder.
    Folders are already sorted by score. Within the best folder,
    pick the best file by its file-level score tuple.
    """
    if not folders:
        return {"status": "not_found"}
    best_folder = folders[0]
    best_file   = min(best_folder["files"], key=lambda f: _score_file(f, best_folder["upload_speed"]) or (99,))
    file_score  = _score_file(best_file, best_folder["upload_speed"])

    if file_score and file_score[1]:  # is_live
        return {"status": "live_only", "username": best_folder["username"],
                "file": best_file, "folder": best_folder}
    return {"status": "ok", "username": best_folder["username"],
            "file": best_file, "folder": best_folder}

def slskd_search_and_pick(track: dict) -> dict:
    query   = f"{track['artist']} {track['title']}"
    folders = slskd_run_search(query, expected_track_count=1)
    result  = slskd_pick(folders)
    result["folders"] = folders
    return result

MAX_SEARCH_PASSES = 3
# A pass that found this many folders whose name actually resembles the album
# is good enough; further passes would only cost another slskd round trip.
_ENOUGH_ALBUM_MATCHES = 2

def _dedupe_terms(text: str) -> str:
    """Collapse an immediately repeated run of words.

    A self-titled album makes "{artist} {album}" read "Led Zeppelin Led
    Zeppelin", which slskd treats as a literal phrase and which effectively
    asks for the artist's whole discography. Tubifarry's QueryBuilder does the
    same collapse.
    """
    words = (text or "").split()
    n = len(words)
    for size in range(n // 2, 0, -1):
        if [w.lower() for w in words[:size]] == [w.lower() for w in words[size:size * 2]]:
            return " ".join(words[:size] + words[size * 2:])
    return " ".join(words)

def _album_search_queries(artist: str, album: str, year: str = "",
                          track_titles: list = None) -> list:
    """Ordered, deduped search variants for one album, widest-recall last.

    Two naive variants ("{artist} {album}", then bare "{album}") were the whole
    strategy, and they fail in predictable ways: MusicBrainz canonical titles
    carry edition packaging no peer folder has, and a self-titled album
    degenerates into an artist-wide query. Each variant below fixes one of
    those, and `slskd_search_album_folders` walks them until it has enough.
    """
    artist = (artist or "").strip()
    clean = _clean_album_title(album)
    self_titled = bool(artist and clean
                       and _match_key(artist) == _match_key(clean))
    out = []

    def add(q):
        # Dedupe repeated runs on every variant, not just the first: the
        # punctuation-stripped form of a self-titled album is just as prone to
        # reading "Led Zeppelin Led Zeppelin" as the original was.
        q = _dedupe_terms(re.sub(r"\s+", " ", (q or "")).strip())
        if q and not any(q.lower() == e.lower() for e in out):
            out.append(q)

    # 1. Artist + cleaned album, with a repeated run collapsed. A self-titled
    #    album gains the year instead, which is the one term that separates the
    #    debut from everything else the artist ever released.
    primary = _dedupe_terms(f"{artist} {clean}".strip())
    if self_titled and year:
        add(f"{primary} {year}")
        add(primary)
    else:
        add(primary)

    # 2. A punctuation-free form, or — for a wordy title — just its distinctive
    #    words. Peers name folders every possible way; dropping the noise is
    #    what makes "Sgt. Pepper's" find "Sgt Peppers", and dropping the
    #    stopwords is what makes "The Dark Side of the Moon" find "Dark Side of
    #    the Moon". Word *order* is preserved — slskd matches terms, but a
    #    human-readable query is far easier to debug from a log line.
    words = _significant_words(clean)
    if len(clean.split()) >= 4 and len(words) >= 2:
        keep = sorted(sorted(words, key=len, reverse=True)[:3], key=words.index)
        add(f"{artist} {' '.join(keep)}")
    else:
        add(f"{_strip_diacritics(artist)} {_strip_diacritics(clean)}".replace("'", ""))

    # 3. Last resort. For a self-titled album the album name alone is the
    #    artist name, so it buys nothing — ask for the most distinctive missing
    #    track instead, which is a phrase no other album shares.
    if self_titled:
        best_track = ""
        for title in (track_titles or []):
            if len(_significant_words(title)) > len(_significant_words(best_track)):
                best_track = title
        if best_track:
            add(f"{artist} {best_track}")
    else:
        add(clean)
    return out[:MAX_SEARCH_PASSES]

def slskd_search_album_folders(artist: str, album: str,
                                expected_track_count: int = 0,
                                progress=None, stats: dict = None,
                                year: str = "", track_titles: list = None) -> list:
    """
    Search slskd for a full album, walking up to MAX_SEARCH_PASSES query
    variants and merging what they find.

    Merging rather than replacing matters: the variants trade precision for
    recall, so a later, wider pass routinely finds the folder an earlier one
    missed *and* loses one the earlier one had. Keyed on (username, folder),
    keeping the better-scored copy.

    Returns folder-level results sorted by _score_folder (best first).
    """
    queries = _album_search_queries(artist, album, year, track_titles)
    if not queries:
        return []
    merged = {}
    clean_album = _clean_album_title(album)
    for i, query in enumerate(queries):
        if i:
            # The extra passes are why an album search can take a multiple of
            # SEARCH_TIMEOUT. Say so, rather than leaving the screen on a stale
            # first-pass line.
            print(f"  Album search pass {i + 1}/{len(queries)}: '{query}'")
            if progress:
                progress(f"Widening the search — “{query}”…")
        # Each pass overwrites `stats`, so the accounting reflects the pass that
        # ran last. That is deliberate and unchanged: it is the widest pass, and
        # therefore the one that explains an empty result.
        for fd in slskd_run_search(query, expected_track_count, progress=progress,
                                   stats=stats, album=clean_album, artist=artist):
            key = (fd.get("username", ""), fd.get("folder", ""))
            prev = merged.get(key)
            if prev is None or (fd.get("score") or 0) > (prev.get("score") or 0):
                merged[key] = fd
        # Enough folders that actually look like this album — more passes would
        # only add noise and another 30-90s of peer waiting.
        strong = [fd for fd in merged.values() if fd.get("album_match_ok")]
        if len(strong) >= _ENOUGH_ALBUM_MATCHES:
            break
    folders = sorted(merged.values(), key=lambda x: -(x.get("score") or 0))
    if stats is not None:
        stats["passes"] = i + 1
        stats["queries"] = queries[:i + 1]
        stats["merged_folders"] = len(folders)
    return folders

def slskd_enqueue(username: str, file: dict,
                  track: dict = None, token: str = None,
                  chat_id: str = None, candidates: list = None,
                  album_group_id: str = None,
                  review_group_id: str = "",
                  review_track_index: int = None,
                  match_mode: str = "auto",
                  target_release_mbid: str = "",
                  repair_job_id: str = "",
                  repair_track_id: str = "") -> bool:
    try:
        download_record = {}
        if repair_job_id:
            download_record = _repair_record_download_queued(
                repair_job_id, repair_track_id, username, file)
        r = _http.post(
            f"{SLSKD_URL}/api/v0/transfers/downloads/{username}",
            headers=_slskd_headers(),
            json=[{"filename": file["filename"], "size": file.get("size", 0)}],
            timeout=30,
        )
        if not r.ok:
            print(f"  slskd enqueue failed ({r.status_code}): {r.text[:200]}")
            if repair_job_id:
                _repair_update_download(
                    repair_job_id, username, file.get("filename", ""),
                    "error", f"slskd enqueue failed ({r.status_code}): {r.text[:200]}")
            return False
        # Register for polling if context provided
        if token is not None and chat_id is not None:
            key = (username, file["filename"])
            pending_downloads[key] = {
                "track":          track,
                "token":          token,
                "chat_id":        chat_id or "",
                "candidates":     candidates or [],
                "album_group_id": album_group_id,
                "review_group_id": review_group_id or (track or {}).get("_review_group_id", ""),
                "review_track_index": (
                    review_track_index if review_track_index is not None
                    else (track or {}).get("_review_track_index")
                ),
                "match_mode": match_mode or (track or {}).get("_match_mode", "auto"),
                "target_release_mbid": (
                    target_release_mbid or (track or {}).get("_target_release_mbid", "")
                ),
                "repair_job_id": repair_job_id or (track or {}).get("repair_job_id", ""),
                "repair_track_id": repair_track_id or (track or {}).get("repair_track_id", ""),
                "download_record_id": download_record.get("id", ""),
                "queued_at": time.time(),
                "latest_state": "Queued",
                "percent": 0,
                "error": "",
                "local_path": "",
            }
        return True
    except Exception as e:
        print(f"  slskd enqueue exception: {e}")
        if repair_job_id:
            _repair_update_download(
                repair_job_id, username, file.get("filename", ""),
                "timeout" if "timeout" in str(e).lower() else "error",
                str(e), "queue_timeout" if "timeout" in str(e).lower() else "")
        return False

def slskd_enqueue_folder(username: str, files: list,
                         token: str = None, chat_id: str = None,
                         label: str = "", release_mbid: str = "",
                         alt_sources: list = None, artist: str = "",
                         album: str = "", missing_tracks: list = None,
                         review_group_id: str = "",
                         match_mode: str = "auto",
                         repair_job_id: str = "",
                         allow_mp3: bool = False,
                         quality: str = "",
                         siblings: list = None) -> tuple:
    """Enqueue matching files from an album folder. Returns (ok, total, album_group_id)."""
    if not files:
        return 0, 0, None
    file_pairs = _album_file_pairs_for_missing_tracks(files, missing_tracks or [],
                                                     siblings=siblings or [])
    if not file_pairs:
        print(f"  no album folder files matched missing tracks for {artist} - {album}")
        return 0, 0, None
    album_group_id = None
    if token and chat_id:
        album_group_id = _uid("ag")
        pending_album_groups[album_group_id] = {
            "label":           label or username,
            "token":           token,
            "chat_id":         chat_id,
            "total":           len(file_pairs),
            "completed":       0,
            "failed":          0,
            "ts":              time.time(),
            "release_mbid":    release_mbid or "",  # for --search-id pinning
            "local_dirs":      {},                  # dirname -> count (find album dir)
            "progress_msg_id": None,                # editable Telegram progress line
            "alt_sources":     alt_sources or [],   # remaining folders to try on failure
            "source_user":     username,            # current peer
            "artist":          artist,
            "album":           album,
            "missing_tracks":  missing_tracks or [],
            "review_group_id": review_group_id,
            "match_mode":      match_mode or "auto",
            "target_release_mbid": release_mbid or "",
            "review_track_indexes": [
                t.get("_review_track_index") for t in (missing_tracks or [])
                if t.get("_review_track_index") is not None
            ],
            "switching":       False,               # guard against double-switch
            # Carried so the poller's failover path keeps honouring the album's
            # MP3 opt-in — it runs on a different thread, long after the
            # _mp3_fallback scope around the original enqueue has exited.
            "allow_mp3":       bool(allow_mp3),
            # And for the same reason: a fill that asked for CD-standard FLAC
            # must not have the failover quietly fetch a 24/96 copy instead.
            "quality":         quality if quality in QUALITY_PREFERENCES else "",
            # Same reason: the failover path re-matches files against an alt
            # source and needs the release's other titles to reject a file
            # that belongs to a track we already have.
            "siblings":        list(siblings or []),
        }
    ok = 0
    for f, track in file_pairs:
        if slskd_enqueue(username, f, track=track, token=token, chat_id=chat_id,
                         album_group_id=album_group_id,
                         review_group_id=review_group_id,
                         review_track_index=(track or {}).get("_review_track_index"),
                         match_mode=match_mode,
                         target_release_mbid=release_mbid,
                         repair_job_id=repair_job_id or (track or {}).get("repair_job_id", ""),
                         repair_track_id=(track or {}).get("repair_track_id", "")):
            ok += 1
        elif album_group_id:
            pending_album_groups[album_group_id]["failed"] += 1
    if album_group_id and ok == 0:
        pending_album_groups.pop(album_group_id, None)
        album_group_id = None
    return ok, len(file_pairs), album_group_id

def _album_file_pairs_for_missing_tracks(files: list, missing_tracks: list,
                                         siblings=(), folder: dict = None,
                                         release_total: int = 0,
                                         release_tracks=()) -> list:
    audio = [f for f in files if _file_ext(f.get("filename", "")) in _accepted_formats()]
    if not missing_tracks:
        return [(f, None) for f in audio]

    used = set()
    pairs = []
    for track in sorted(missing_tracks, key=_missing_track_sort_key):
        match = _best_file_for_missing_track(track, audio, used, siblings=siblings,
                                             release_tracks=release_tracks)
        if match:
            used.add(match.get("filename", ""))
            pairs.append((match, track))

    if len(pairs) == len(missing_tracks):
        return pairs

    # Positional fallback for a clean folder holding exactly the missing tracks
    # — but only when *nothing* matched by title. It used to apply to partial
    # matches too, throwing away good title matches in favour of a blind zip,
    # which silently reassigned files to the wrong tracks.
    if not pairs and len(audio) == len(missing_tracks):
        ordered_files = sorted(audio, key=lambda f: _album_file_sort_key(f.get("filename", "")))
        ordered_tracks = sorted(missing_tracks, key=_missing_track_sort_key)
        return list(zip(ordered_files, ordered_tracks))

    # Track-number fallback for a *whole-album* folder. The case this is for is
    # localized or transliterated filenames: the folder plainly is the album —
    # it holds exactly as many audio files as the release has tracks, and its
    # name matches — but not one filename resembles a MusicBrainz title, so
    # every title tier misses. The track number is then the only evidence
    # there is, and in a complete folder it is good evidence.
    #
    # Guarded hard, because a blind positional zip is how files get filed as
    # the wrong song: the folder must be the full album (not the zip above,
    # which covers a folder holding only the gaps), and it must either match
    # the album name or have already proved itself by matching a majority of
    # the release's titles.
    leftover = [t for t in missing_tracks
                if not any(t is paired for _f, paired in pairs)]
    if leftover and release_total and len(audio) == release_total:
        trusted = bool((folder or {}).get("album_match_ok"))
        if not trusted and release_tracks:
            named = sum(1 for rt in release_tracks
                        if any(_title_match_rank(_match_key(rt.get("title", "")),
                                                 _file_title_key(f.get("filename", "")),
                                                 rt.get("title", ""),
                                                 _file_title_text(f.get("filename", ""))) >= 0
                               for f in audio))
            trusted = named * 2 > len(release_tracks)
        if trusted:
            by_number = {}
            for f in audio:
                if f.get("filename", "") in used:
                    continue
                by_number.setdefault(_filename_track_number(f.get("filename", "")), []).append(f)
            for track in sorted(leftover, key=_missing_track_sort_key):
                pos = int(track.get("position") or 0)
                # Exactly one unclaimed file at that number, or it is not
                # evidence — two files numbered 04 mean we know nothing.
                hits = by_number.get(pos) or []
                if pos and len(hits) == 1:
                    used.add(hits[0].get("filename", ""))
                    by_number.pop(pos, None)
                    pairs.append((hits[0], track))

    print(f"  matched {len(pairs)}/{len(missing_tracks)} missing track(s) in album folder")
    return pairs

def _file_title_text(filename: str) -> str:
    """The title part of a filename, still readable as words.

    Same stripping as `_file_title_key` — extension, disc-track prefix, leading
    track number — but stopping before the key is squashed into one run of
    characters. The token-based fuzzy ratios need the word boundaries that
    `_match_key` deliberately throws away.
    """
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", base)               # extension
    base = re.sub(r"^\s*\d{1,2}[\s._-]+\d{1,3}(?=\D|$)", "", base)  # "1-04" disc-track
    base = re.sub(r"^\s*\d{1,3}\s*[.)\]_-]*\s*", "", base)        # leading track number
    return base.strip()

def _file_title_key(filename: str) -> str:
    """Comparison key for a *filename*, with the packaging stripped.

    Peers name files "04 - Title.flac" or "1-04 Title.opus"; keying the raw
    basename left the track number and extension in the key, so a title only
    ever matched as a substring and every match was a loose one.
    """
    return _match_key(_file_title_text(filename))

# Match tiers, tightest first. The number is the sort key: a tighter tier
# always beats a looser one, and format/bitrate only break ties within a tier.
MATCH_EXACT, MATCH_PREFIX, MATCH_CONTAINED, MATCH_FUZZY, MATCH_DURATION = 0, 1, 2, 3, 4
MATCH_BASIS = {
    MATCH_EXACT: "exact", MATCH_PREFIX: "prefix", MATCH_CONTAINED: "contained",
    MATCH_FUZZY: "fuzzy", MATCH_DURATION: "duration",
}
# Thresholds for the fuzzy tier. Deliberately high: this tier exists for
# punctuation drift and typos, not for guessing.
FUZZY_TOKEN_SORT_MIN = 87.0
FUZZY_PARTIAL_MIN = 92.0
# The shortest reverse-containment worth trusting. "Song" inside
# "Song (feat. X)" is fine; a three-letter title inside anything is not.
REVERSE_CONTAIN_MIN = 5

def _title_match_rank(title_key: str, file_key: str,
                      title: str = "", file_title: str = "") -> int:
    """How tightly a title sits in a filename. -1 means no match.

    Tiers, tightest first: 0 exact, 1 prefix, 2 containment (either
    direction), 3 fuzzy. Only tier 0 is self-evidently the right file; every
    looser tier is a guess that has to survive the sibling tighter-claim check
    in `_best_file_for_missing_track`.

    Containment is bidirectional. It used to test only `title_key in file_key`,
    so a MusicBrainz title *longer* than the filename — "Song (feat. X)" against
    `04 - Song.flac`, which is how peers routinely name files — never matched at
    all. That is a large share of the "every file is right there and the bot
    still won't pair them" reports.

    `title`/`file_title` are the un-keyed forms; without them the fuzzy tier is
    skipped, because the keys have had their word boundaries removed and
    token-based ratios need them.
    """
    if not title_key or not file_key:
        return -1
    if title_key == file_key:
        return MATCH_EXACT
    if file_key.startswith(title_key):
        return MATCH_PREFIX
    if title_key in file_key:
        return MATCH_CONTAINED
    if file_key in title_key and len(file_key) >= REVERSE_CONTAIN_MIN:
        return MATCH_CONTAINED
    if title and file_title:
        wt, wf = _match_words(title), _match_words(file_title)
        if wt and wf:
            if float(_fuzz.token_sort_ratio(wt, wf)) >= FUZZY_TOKEN_SORT_MIN:
                return MATCH_FUZZY
            # partial_ratio scores any substring at 100, which is how "Sing"
            # would claim "Singularity". Requiring whole-token alignment keeps
            # the tier useful without reopening that.
            if (float(_fuzz.partial_ratio(wt, wf)) >= FUZZY_PARTIAL_MIN
                    and _tokens_aligned(wt, wf)):
                return MATCH_FUZZY
    return -1

def _release_track_titles(group: dict) -> list:
    """Every title on the release — the tracks we have as well as the gaps.

    The present ones are the important half: they are what a loose match on a
    missing track's title has to be checked against.
    """
    if not group:
        return []
    titles = []
    for album in (group.get("albums") or []):
        for song in (album.get("tracks") or []):
            if song.get("title"):
                titles.append(song["title"])
    for track in (group.get("missing_tracks") or []):
        if track.get("title"):
            titles.append(track["title"])
    return titles

# How close a file's own duration has to be to a canonical track's before it is
# evidence, and how far every *other* candidate has to be before that evidence
# is unambiguous.
DURATION_MATCH_SEC = 3
DURATION_AMBIGUOUS_SEC = 5

def _file_duration(f: dict) -> int:
    """A file's playing time in seconds, 0 when the peer didn't say."""
    for key in ("length", "duration", "durationSec"):
        try:
            v = int(f.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if v > 0:
            return v
    return 0

def _duration_match(track: dict, files: list, used: set,
                    release_tracks=()) -> dict:
    """The one file whose duration identifies this track, or None.

    Last resort, and held to a much higher bar than the title tiers because
    duration is weak evidence: two songs on one album being within a few
    seconds of each other is completely normal. It counts only when the pairing
    is unambiguous in *both* directions — no other track of the release is
    close to this file, and no other unclaimed file is close to this track.
    """
    want = int(track.get("duration") or track.get("length") or 0)
    if want <= 0:
        return None
    near = [f for f in files
            if f.get("filename", "") not in used
            and _file_duration(f)
            and abs(_file_duration(f) - want) <= DURATION_MATCH_SEC]
    if len(near) != 1:
        return None            # nothing close, or several files equally close
    hit = near[0]
    hit_len = _file_duration(hit)
    # Would some other track of this release claim the same file just as well?
    for other in release_tracks or ():
        if other is track:
            continue
        other_len = int(other.get("duration") or other.get("length") or 0)
        if other_len and abs(other_len - hit_len) <= DURATION_AMBIGUOUS_SEC:
            return None
    return hit

def _sibling_claims_tighter(file_key: str, file_title: str, rank: int,
                            title_key: str, sibling_pairs) -> bool:
    """Whether another title on the release has a better claim on this file.

    The safety net the looser tiers rest on. A file matched at any tier below
    exact is only ours if no *other* track of the release matches it more
    tightly — or matches it equally tightly with a longer, more specific title.
    Missing "Sing" pairing with `05 - Singularity.flac` is the case this
    exists for: the release's own "Singularity" matches that file exactly, so
    "Sing" loses it.
    """
    for sibling, key in sibling_pairs:
        other = _title_match_rank(key, file_key, sibling, file_title)
        if other < 0:
            continue
        if other < rank or (other == rank and len(key) > len(title_key)):
            return True
    return False

def _best_file_match(track: dict, files: list, used: set, siblings=(),
                     release_tracks=()) -> tuple:
    """Pair one missing track with one file. Returns (file, basis, note).

    `basis` is how the pairing was reached — see MATCH_BASIS — so callers can
    report an honest confidence rather than presenting a duration guess and a
    recording-MBID hit as the same thing. `note` describes the nearest
    *rejected* candidate when nothing matched, which is what makes a residual
    failure diagnosable from the UI instead of only from the debug endpoint.
    """
    title = track.get("title", "")
    title_key = _match_key(title)
    if not title_key:
        return None, "", ""
    # (title, key) pairs, because the fuzzy tier needs the un-keyed form: the
    # keys have had their word boundaries stripped, and token ratios need words.
    sibling_pairs = [(s, _match_key(s)) for s in (siblings or [])]
    sibling_pairs = [(s, k) for s, k in sibling_pairs if k and k != title_key]

    candidates = []
    near_miss = ("", 0.0)
    for f in files:
        name = f.get("filename", "")
        if name in used:
            continue
        file_key = _file_title_key(name)
        file_title = _file_title_text(name)
        rank = _title_match_rank(title_key, file_key, title, file_title)
        if rank < 0:
            # Remember the closest thing we turned down, for the error message.
            score = _fuzzy_score(title, file_title)
            if score > near_miss[1]:
                near_miss = (name, score)
            continue
        # A loose hit on a filename that some *other* track of the release
        # claims at least as tightly is that track's file, not ours. Without
        # this, missing "Sing" matched "05 - Singularity.flac" and pulled down
        # a song already in the library, while the real gap stayed unfilled.
        # An exact hit is never second-guessed. This applies to the fuzzy tier
        # too — it is what lets the tier be loose without being dangerous.
        if rank > MATCH_EXACT and _sibling_claims_tighter(
                file_key, file_title, rank, title_key, sibling_pairs):
            # Deliberately turned down, and that is the most useful thing to
            # report: "closest: 05 - Singularity.flac" tells you the file was
            # seen and rejected, where silence reads as "the source is empty".
            score = _fuzzy_score(title, file_title)
            if score > near_miss[1]:
                near_miss = (name, score)
            continue
        score = 0
        pos = int(track.get("position") or 0)
        if pos and _filename_track_number(name) == pos:
            score -= 10
        score += _accepted_formats().get(_file_ext(name), 99)
        score -= int(f.get("bitRate") or 0) // 1000
        # Rank leads the sort: a tighter title match beats a better-scoring
        # file, so format and bitrate only break ties within one tier.
        candidates.append((rank, score, name, f))
    if candidates:
        candidates.sort(key=lambda x: (x[0], x[1], x[2]))
        best = candidates[0]
        return best[3], MATCH_BASIS.get(best[0], "fuzzy"), ""

    # Every title tier missed. Duration is the last thing that can identify a
    # file, and only when it does so unambiguously.
    hit = _duration_match(track, files, used, release_tracks)
    if hit:
        return hit, MATCH_BASIS[MATCH_DURATION], ""
    note = ""
    if near_miss[0]:
        note = (f"closest: '{near_miss[0].replace(chr(92), '/').rsplit('/', 1)[-1]}' "
                f"({near_miss[1]:.0f}%)")
    return None, "", note

def _best_file_for_missing_track(track: dict, files: list, used: set, siblings=(),
                                 release_tracks=()):
    """`_best_file_match` without the basis — the shape most callers want."""
    return _best_file_match(track, files, used, siblings, release_tracks)[0]

def _match_key(value: str) -> str:
    """Normalized comparison key for a title, filename or album name.

    NFKD-decompose and drop combining marks first, so "Björk" and "Bjork" —
    or a peer's transliterated filename against MusicBrainz's accented title —
    produce the same key. Titles that are entirely non-Latin (CJK, Cyrillic)
    strip to nothing under the [a-z0-9] filter; for those fall back to a
    casefolded, punctuation-stripped form rather than returning "" and matching
    everything (or, in _best_file_for_missing_track, nothing).
    """
    raw = (value or "")
    folded = unicodedata.normalize("NFKD", raw)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    key = re.sub(r"[^a-z0-9]+", "", folded.lower())
    if key:
        return key
    return re.sub(r"[\s\W_]+", "", raw.casefold(), flags=re.UNICODE)

def _filename_track_number(filename: str) -> int:
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    m = re.match(r"^\D*(\d{1,3})(?:\D|$)", base)
    return int(m.group(1)) if m else 0

def _missing_track_sort_key(track: dict) -> tuple:
    return (int(track.get("position") or 0), (track.get("title") or "").lower())

def _album_file_sort_key(filename: str) -> tuple:
    return (_filename_track_number(filename), filename.replace("\\", "/").rsplit("/", 1)[-1].lower())

def slskd_expand_directory(username: str, folder: dict,
                            reference_file: dict) -> list:
    """
    Fetch the complete file listing for a folder from a peer using
    POST /api/v0/users/{username}/directory.

    slskd expects the ORIGINAL backslash-separated path (raw_folder).
    The directory API returns bare filenames — we reconstruct full paths
    as raw_folder + backslash + filename, matching Tubifarry's ExpandDirectory.

    Returns a list of file dicts ready for slskd_enqueue, or [] on failure.
    """
    import urllib.parse
    raw_path     = folder.get("raw_folder", "")
    display_path = folder.get("folder", raw_path)

    if not raw_path:
        print(f"  expand_directory: no raw_folder available")
        return []

    try:
        url = (f"{SLSKD_URL}/api/v0/users/"
               f"{urllib.parse.quote(username, safe='')}/directory")
        r   = _http.post(url, headers=_slskd_headers(),
                            json={"directory": raw_path}, timeout=15)
        if not r.ok:
            print(f"  expand_directory failed ({r.status_code}) for "
                  f"{username}:{raw_path}: {r.text[:200]}")
            return []

        data  = r.json()
        if isinstance(data, list):
            data = data[0] if data else {}
        files = data.get("files", [])
        print(f"  expand_directory raw response: {len(files)} files from "
              f"{username}:{raw_path}")

        ref_ext = _file_ext(reference_file.get("filename", ""))
        result  = []
        for f in files:
            fname = f.get("filename", "")
            if not fname:
                continue
            ext = _file_ext(fname)
            if ext not in _accepted_formats():
                continue
            # Bare basename from directory API — prepend the backslash folder path
            full_path = raw_path.rstrip("\\") + "\\" + fname.lstrip("\\")
            # Prefer this file's own metadata; only borrow the reference file's
            # values when the directory API omits them AND the codec matches.
            same_codec = (ext == ref_ext)
            result.append({
                "filename": full_path,
                "size":     f.get("size", 0),
                "bitRate":  f.get("bitRate")
                            or (reference_file.get("bitRate") if same_codec else None),
                "bitDepth": f.get("bitDepth")
                            or (reference_file.get("bitDepth") if same_codec else None),
                "sampleRate": f.get("sampleRate"),
            })

        print(f"  expand_directory: {len(result)} audio files "
              f"in {display_path} (@{username})")
        return result
    except Exception as e:
        print(f"  expand_directory exception for {username}:{display_path}: {e}")
        return []

# slskd transfer states arrive combined, e.g. "Completed, Succeeded" or
# "Completed, Errored". The substate after the comma is what matters.
SLSKD_FAIL_SUBSTATES = ("Errored", "Cancelled", "TimedOut", "Rejected", "Aborted")

def _slskd_succeeded(state: str) -> bool:
    return "Succeeded" in (state or "")

def _slskd_failed(state: str) -> bool:
    return any(sub in (state or "") for sub in SLSKD_FAIL_SUBSTATES)

_DOWNLOADS_CACHE = {"value": [], "ts": 0.0}
_DOWNLOADS_CACHE_TTL = 3.0
_downloads_cache_lock = threading.Lock()

def slskd_get_all_downloads(force: bool = False) -> list:
    """Poll slskd for all current transfer statuses.

    Cached for a few seconds. /api/summary, /api/transfers and /api/downloads all
    call this, and the SPA polls them every 2-5s on every tab — without the cache
    that is roughly one 10s-timeout HTTP round trip to slskd per web request, in
    the request thread. The background poller passes force=True so its own view
    is never stale.
    """
    now = time.time()
    if not force:
        with _downloads_cache_lock:
            if now - _DOWNLOADS_CACHE["ts"] < _DOWNLOADS_CACHE_TTL:
                return _DOWNLOADS_CACHE["value"]
    value = _slskd_fetch_all_downloads()
    with _downloads_cache_lock:
        _DOWNLOADS_CACHE["value"] = value
        _DOWNLOADS_CACHE["ts"] = time.time()
    return value

def _slskd_fetch_all_downloads() -> list:
    try:
        r = _http.get(f"{SLSKD_URL}/api/v0/transfers/downloads",
                         headers=_slskd_headers(), timeout=10)
        if not r.ok:
            return []
        result = []
        for user_dl in r.json():
            username = user_dl.get("username", "")
            for directory in user_dl.get("directories", []):
                for f in directory.get("files", []):
                    f["_username"] = username
                    result.append(f)
        return result
    except Exception as e:
        print(f"  slskd transfers poll error: {e}")
        return []

def _slskd_transfer_percent(dl: dict) -> int:
    for key in ("percentComplete", "percent", "percentage", "progress"):
        val = dl.get(key)
        if isinstance(val, (int, float)):
            return int(max(0, min(100, val)))
    size = dl.get("size") or dl.get("fileSize") or 0
    done = (dl.get("bytesDownloaded") or dl.get("bytesTransferred") or
            dl.get("downloaded") or 0)
    try:
        return int(max(0, min(100, float(done) * 100 / float(size)))) if size else 0
    except Exception:
        return 0


def _dir_has_audio(d: str) -> bool:
    """True if the directory still contains any accepted audio file."""
    try:
        for root, _, files in os.walk(d):
            for f in files:
                if _file_ext(f) in _placeable_formats():
                    return True
    except Exception:
        pass
    return False

def caa_list_front_images(rgid: str, limit: int = 4) -> list:
    """
    Return up to `limit` front-cover candidates for a release-group from the
    Cover Art Archive: [{thumb, full}]. Empty list if none / on error.
    """
    if not rgid:
        return []
    try:
        r = _http.get(f"https://coverartarchive.org/release-group/{rgid}",
                         timeout=10)
        if not r.ok:
            return []
        out = []
        for img in r.json().get("images", []):
            if not img.get("front"):
                continue
            thumbs = img.get("thumbnails", {})
            out.append({
                "thumb": thumbs.get("250") or thumbs.get("small") or img.get("image", ""),
                "full":  img.get("image", ""),
            })
            if len(out) >= limit:
                break
        return out
    except Exception as e:
        print(f"  CAA list error: {e}")
        return []

def rgid_from_release(release_mbid: str) -> str:
    """Resolve a release MBID to its release-group id (cached via mbz_get)."""
    if not release_mbid:
        return ""
    data = mbz_get(f"release/{release_mbid}", {"inc": "release-groups"})
    return (data.get("release-group") or {}).get("id", "")

def _resolve_local_path(remote_filename: str) -> str | None:
    """
    Map a slskd *remote* transfer filename to the actual file on disk.

    slskd does not recreate the peer's full directory tree locally, so naively
    joining SLSKD_DOWNLOAD_DIR + remote_path is usually wrong. We try that as a
    fast guess, then fall back to locating the file by basename anywhere under
    the downloads directory.
    """
    base = remote_filename.replace("\\", "/").rstrip("/").split("/")[-1]
    if not base:
        return None
    guess = os.path.join(SLSKD_DOWNLOAD_DIR,
                         remote_filename.replace("\\", "/").lstrip("/"))
    if os.path.isfile(guess):
        return guess
    try:
        for root, _, files in os.walk(SLSKD_DOWNLOAD_DIR):
            if base in files:
                return os.path.join(root, base)
    except Exception as e:
        print(f"  _resolve_local_path error: {e}")
    return None

# ---------------------------------------------------------------------------
# Download completion polling (Feature 3)
# ---------------------------------------------------------------------------

def _slskd_cancel(username: str, filename: str):
    """Best-effort cancel/remove of a single slskd transfer."""
    import urllib.parse
    try:
        _http.delete(
            f"{SLSKD_URL}/api/v0/transfers/downloads/"
            f"{urllib.parse.quote(username, safe='')}/"
            f"{urllib.parse.quote(filename, safe='')}",
            headers=_slskd_headers(), timeout=10)
    except Exception as e:
        print(f"  slskd cancel error: {e}")

def _abandon_group_downloads(ag_id: str):
    """Drop all still-pending transfers belonging to an album group."""
    for key in [k for k, v in pending_downloads.items()
                if v.get("album_group_id") == ag_id]:
        username, filename = key
        info = pending_downloads.get(key, {})
        if info.get("repair_job_id"):
            _repair_update_download(info.get("repair_job_id", ""), username, filename,
                                    "cancelled", "abandoned pending transfer",
                                    raw_state="Cancelled")
        _slskd_cancel(username, filename)
        pending_downloads.pop(key, None)

def _retry_file_from_alt_source(ag: dict, ag_id: str, info: dict,
                                failed_username: str) -> str:
    """
    One file failed inside an album group that is otherwise making progress.

    `_switch_album_source` is the wrong tool here — it re-enqueues the *whole*
    album from scratch, throwing away everything already downloaded. Instead
    find just this track in the next alternative source and re-enqueue that one
    file into the same group, so the group's total still adds up and the album
    doesn't finalize 2-3 tracks short.

    Returns the peer name a retry was queued from, or "" if none was available.
    Blocking (slskd HTTP) — call via asyncio.to_thread.
    """
    track = info.get("track") or {}
    if not track or not ag_id:
        return ""
    tried = list(info.get("_tried_users") or [])
    if failed_username and failed_username not in tried:
        tried.append(failed_username)
    if len(tried) > MAX_FILE_RETRIES:
        return ""

    with _mp3_fallback(ag.get("allow_mp3")), _quality_preference(ag.get("quality", "")):
        return _retry_file_from_alt_source_inner(ag, ag_id, info, track, tried)

def _retry_file_from_alt_source_inner(ag: dict, ag_id: str, info: dict,
                                      track: dict, tried: list) -> str:
    for fd in (ag.get("alt_sources") or []):
        username = fd.get("username", "")
        if not username or username in tried:
            continue
        # Search results only carry the files that matched the query; ask the
        # peer for the real folder listing once, then reuse it for every other
        # file that has to fail over to this same source.
        files = fd.get("_expanded")
        if files is None:
            ref = (fd.get("files") or [{}])[0]
            files = slskd_expand_directory(username, fd, ref) or fd.get("files") or []
            fd["_expanded"] = files
        audio = [f for f in files
                 if _file_ext(f.get("filename", "")) in _accepted_formats()]
        # Claims are per (peer, folder) and have to persist across retries: with a
        # fresh `used` set here, two tracks failing over to the same peer each
        # picked their own best file independently and could pick the *same* one —
        # pending_downloads is keyed (username, filename), so the second enqueue
        # silently overwrote the first one's bookkeeping. A list, not a set: `ag`
        # is persisted to lb_bot_state.json as JSON (same reason _tried_users is).
        claimed = fd.setdefault("_claimed", [])
        match = _best_file_for_missing_track(track, audio, claimed,
                                             siblings=ag.get("siblings") or [])
        if not match:
            continue
        claimed.append(match.get("filename", ""))
        if not slskd_enqueue(username, match, track=track,
                             token=ag.get("token"), chat_id=ag.get("chat_id"),
                             album_group_id=ag_id,
                             review_group_id=info.get("review_group_id", ""),
                             review_track_index=info.get("review_track_index"),
                             match_mode=info.get("match_mode", "auto"),
                             target_release_mbid=info.get("target_release_mbid", ""),
                             repair_job_id=info.get("repair_job_id", ""),
                             repair_track_id=info.get("repair_track_id", "")):
            # Nothing was queued from this peer, so the file isn't ours after all.
            if match.get("filename", "") in claimed:
                claimed.remove(match.get("filename", ""))
            continue
        # Carry the attempt history onto the new transfer so a peer that keeps
        # failing this one file can't be tried forever.
        new_info = pending_downloads.get((username, match["filename"]))
        if new_info is not None:
            new_info["_tried_users"] = tried
        gid = info.get("review_group_id", "")
        if gid:
            _set_review_track_state(gid, info.get("review_track_index"),
                                    "downloading", download_percent=0,
                                    download_state="Queued", error="",
                                    source_user=username,
                                    filename=match["filename"])
        return username
    return ""

async def _switch_album_source(bot, ag_id: str):
    """
    The current peer is failing to serve this album (rejected/aborted/etc).
    Abandon it and re-enqueue the whole album from the next candidate source.
    If no candidates remain, offer the user a Retry button (fresh search).
    """
    ag = pending_album_groups.get(ag_id)
    if not ag or ag.get("switching"):
        return
    ag["switching"] = True
    _abandon_group_downloads(ag_id)

    with _mp3_fallback(ag.get("allow_mp3")), _quality_preference(ag.get("quality", "")):
        await _switch_album_source_inner(bot, ag_id, ag)

async def _switch_album_source_inner(bot, ag_id: str, ag: dict):
    while ag.get("alt_sources"):
        fd       = ag["alt_sources"].pop(0)
        username = fd["username"]
        await _tg_send(bot, ag["chat_id"],
            f"🔁 Source for {ag['label']} failed — trying @{username}…")
        ref_file = fd["files"][0] if fd["files"] else {}
        full     = await asyncio.to_thread(slskd_expand_directory, username, fd, ref_file)
        if not full:
            full = fd["files"]
        if not full:
            continue
        # Reset counters and re-enqueue into the SAME group id
        ag.update({"total": len(full), "completed": 0, "failed": 0,
                   "source_user": username, "local_dirs": {}, "ts": time.time()})
        ok = 0
        for f in full:
            if slskd_enqueue(username, f, token=ag["token"], chat_id=ag["chat_id"],
                             album_group_id=ag_id):
                ok += 1
            else:
                ag["failed"] += 1
        if ok:
            await _update_group_progress(bot, ag)
            ag["switching"] = False
            return
        _abandon_group_downloads(ag_id)   # that source enqueued nothing — keep going

    # Exhausted every known source — let the user retry a fresh search.
    pending_album_groups.pop(ag_id, None)
    rid = _uid("aretry")
    _uid_to_token[rid] = ag["token"]
    _pending_album[rid] = {
        "token": ag["token"], "chat_id": ag["chat_id"],
        "artist": ag.get("artist", ""), "title": ag.get("album", ""),
        "release_mbid": ag.get("release_mbid", ""), "total_tracks": 0,
        "query": ag.get("retry_query", ""),
    }
    await _tg_send(bot, ag["chat_id"],
        f"⚠️ Every source for {ag['label']} failed to queue.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔁 Retry (search again)", callback_data=f"ARETRY|{rid}")]]))

async def _update_group_progress(bot, ag: dict):
    """Edit the album's progress line as files complete (best-effort)."""
    pmid = ag.get("progress_msg_id")
    if not pmid:
        return
    done = ag["completed"] + ag["failed"]
    try:
        await bot.edit_message_text(
            chat_id=ag["chat_id"], message_id=pmid,
            text=f"⬇️ Downloading {ag['label']}: {done}/{ag['total']} files "
                 f"{_progress_bar(done, ag['total'])}")
    except Exception:
        pass  # unchanged text / edit too frequent — ignore

def _album_action_markup(recid: str, status: str):
    """Buttons attached to a finished album, depending on import outcome."""
    rows = []
    if status == "needs_review":
        rows.append([InlineKeyboardButton("📌 Pin MBID & retry", callback_data=f"RIMP|{recid}"),
                     InlineKeyboardButton("📥 Import as-is",       callback_data=f"IMPAS|{recid}")])
        rows.append([InlineKeyboardButton("🎯 Pick release",      callback_data=f"PREL|{recid}")])
    elif status == "failed":
        rows.append([InlineKeyboardButton("♻️ Re-import",         callback_data=f"RIMP|{recid}"),
                     InlineKeyboardButton("📥 Import as-is",       callback_data=f"IMPAS|{recid}")])
    rows.append([InlineKeyboardButton("🎚 Adjust",               callback_data=f"ADJ|{recid}")])
    return InlineKeyboardMarkup(rows)

def _adjust_menu_markup(recid: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("♻️ Re-import (pinned)", callback_data=f"RIMP|{recid}")],
        [InlineKeyboardButton("🎯 Pick release",        callback_data=f"PREL|{recid}")],
        [InlineKeyboardButton("🖼 Choose cover",        callback_data=f"ART|{recid}")],
        [InlineKeyboardButton("⬇️ Re-download album",   callback_data=f"ADDL|{recid}")],
    ])

def _render_release_picker(pickid: str, cands: list, label: str, page: int, per: int = 5):
    """(text, markup) for one page of MusicBrainz release candidates."""
    import math
    pages = max(1, math.ceil(len(cands) / per))
    page  = max(0, min(page, pages - 1))
    start = page * per
    chunk = cands[start:start + per]
    lines = [f"Pick the correct release for {label}  (page {page+1}/{pages}):"]
    rows  = []
    for i, c in enumerate(chunk):
        gidx = start + i
        yr   = f" ({c['year']})" if c["year"] else ""
        typ  = f" · {c['primary_type']}" if c.get("primary_type") else ""
        lines.append(f"{gidx+1}. {c['title']} — {c['artist']}{yr}{typ}")
        rows.append([InlineKeyboardButton(f"{gidx+1}. {c['title'][:30]}",
                                          callback_data=f"RPK|{pickid}|{gidx}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Back", callback_data=f"RPGN|{pickid}|{page-1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("More ➡️", callback_data=f"RPGN|{pickid}|{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("✖️ Cancel", callback_data=f"RPKC|{pickid}")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)

def _group_missing_slot_keys(group_id: str) -> set:
    """Recording MBIDs and normalized titles of the tracks a group is filling.

    Lets placement restrict itself to the slots this fill is actually about, so a
    track already sitting in the library can't claim a file downloaded for a gap.
    """
    with _review_lock:
        group = _find_review_group(group_id)
        if not group:
            return set()
        keys = set()
        for track in group.get("missing_tracks", []) or []:
            if track.get("decision") in ("skipped", "dismissed"):
                continue
            if track.get("recording_mbid"):
                keys.add(track["recording_mbid"])
            if track.get("title"):
                keys.add(_match_key(track["title"]))
        return keys

def _group_manual_pairs(group: dict) -> dict:
    """Slot key → absolute local path for tracks the user paired by hand.

    Keyed on both the recording MBID and `_match_key(title)`, because a slot is
    identified by whichever of those the tracklist actually carries.
    """
    pairs = {}
    for track in (group or {}).get("missing_tracks", []) or []:
        if not track.get("manual_pick"):
            continue
        local = track.get("local_path", "")
        if not local or not os.path.isfile(local):
            continue
        for key in (track.get("recording_mbid", "") or track.get("mbid", ""),
                    _match_key(track.get("title", ""))):
            if key:
                pairs[key] = local
    return pairs

def _deterministic_album_import(album_dir, release_mbid: str,
                                artist: str, album: str,
                                group_id: str = "",
                                only_relpaths=None,
                                manual_pairs: dict = None,
                                skip_audio_guard_for=()) -> dict:
    """
    Tag and place a downloaded album folder into the music library using mutagen.

    Matches each file to its tracklist slot with the *same* ranked matcher the
    acquisition side uses (`_best_file_for_missing_track` + the sibling
    tighter-claim discard), and places into the album's *existing* on-disk folder
    (resolved via Navidrome) when there is one, instead of always creating a fresh
    Artist/Album path -- avoids spawning duplicate album folders when filling
    missing tracks into an album that's already in the library.

    Placement used to run its own weaker substring matcher, which happily undid
    acquisition's hardening: slot "Sing" claimed "05 - Singularity.flac", `_place`
    rewrote that file's title and MBID to Sing's, and the album gained a second
    copy of Singularity that no tag-based duplicate scan could ever see. `_place`
    now refuses a file whose own tags contradict the slot, or whose audio is
    already in the album.

    `album_dir` may be a list: a file that failed over to another peer downloads
    into *that* peer's directory, and importing only the majority directory left
    it stranded. `only_relpaths` restricts placement to a chosen subset.
    `group_id` narrows the tracklist to the slots being filled.

    `manual_pairs` (slot key → absolute local path) is the user's own pairing,
    made through "Pick a file…". It wins over the matcher outright, and it
    suppresses the "the file's tags say it is a different track" refusal — that
    judgement is precisely what the user overrode. The "this audio is already in
    the album" refusal still applies, since that one is what stops a duplicate
    being created; `skip_audio_guard_for` is the deliberate, per-file override
    behind "Place anyway".

    Requires a release MBID: without a tracklist to match against there are no
    canonical tags to write, and the destination degrades to
    "Unknown Artist/Unknown Album" -- untagged files scattered into the library
    are far worse than a refused placement.
    """
    if not release_mbid:
        return {"ok": False, "dest_folder": "", "moved": 0, "per_file": [],
                "error": "No MusicBrainz release specified — refusing to place "
                         "untagged files"}
    album_dirs = [album_dir] if isinstance(album_dir, str) else [d for d in album_dir if d]
    primary_dir = album_dirs[0] if album_dirs else ""
    raw_files = []
    seen_paths = set()
    for one_dir in album_dirs:
        for row in _audio_files_in_folder(one_dir, 200):
            if row["path"] in seen_paths:
                continue
            seen_paths.add(row["path"])
            if len(album_dirs) > 1:
                # relpath is the identity used for `only_relpaths` and for the
                # matcher's pool — two download dirs can hold the same relative
                # name, so qualify it with the folder it came from.
                row = {**row,
                       "relpath": os.path.join(os.path.basename(one_dir), row["relpath"])}
            raw_files.append(row)
    if not raw_files:
        return {"ok": False, "dest_folder": "", "moved": 0, "per_file": [],
                "error": f"No audio files found in {primary_dir}"}
    if only_relpaths:
        wanted = {os.path.normpath(p) for p in only_relpaths}
        raw_files = [f for f in raw_files if os.path.normpath(f["relpath"]) in wanted]
        if not raw_files:
            return {"ok": False, "dest_folder": "", "moved": 0, "per_file": [],
                    "error": "None of the selected files are in the download folder"}
    files = [{**f, "tags": _audio_file_tags(f["path"])} for f in raw_files]

    mb_tracks: list = []
    year = ""
    rgid = ""
    if release_mbid:
        for idx, t in enumerate(mbz_release_tracks_insisting(release_mbid)):
            mb_tracks.append({
                "recording_mbid": t.get("mbid", ""),
                "title": t.get("title", ""),
                "position": int(t.get("position") or idx + 1),
                "duration": t.get("duration", 0),
            })
        # A pinned release whose tracklist won't load (MusicBrainz down or the
        # MBID is bogus) leaves nothing to tag against and lands the files in
        # Unknown Artist/Unknown Album — the same hazard as passing no release
        # at all. Fail instead; the caller can retry once MB answers.
        if not mb_tracks:
            return {"ok": False, "dest_folder": "", "moved": 0, "per_file": [],
                    "retryable": True,
                    "error": "MusicBrainz didn't return a tracklist for this "
                             "release, so the files can't be tagged. They are "
                             "still in your downloads folder — try placing again "
                             "in a few minutes."}
        rel = mbz_release_display(release_mbid)
        year = (rel.get("release_date", "") or "")[:4]
        rgid = rgid_from_release(release_mbid)
        if not artist or not album:
            try:
                raw = mbz_get(f"release/{release_mbid}", {"inc": "artist-credits"})
                if not artist:
                    artist = _artist_credit_str(raw.get("artist-credit", []))
                if not album:
                    album = raw.get("title", "")
            except Exception:
                pass
    tracktotal = len(mb_tracks) or len(files)

    # Slots this import may fill. Walking the *whole* tracklist let a track
    # already present in the library claim a file downloaded for a gap, and then
    # the real gap reported "no downloaded file matched".
    slots = mb_tracks
    if group_id:
        wanted_keys = _group_missing_slot_keys(group_id)
        if wanted_keys:
            picked = [t for t in mb_tracks
                      if t.get("recording_mbid", "") in wanted_keys
                      or _match_key(t.get("title", "")) in wanted_keys]
            if picked:
                slots = picked

    # Sibling titles for the tighter-claim discard come from the whole release,
    # including the tracks we already have — those are the ones a loose match on
    # a gap's title has to be checked against.
    sibling_titles = [t.get("title", "") for t in mb_tracks if t.get("title")]
    slot_by_title_key = {}
    for _t in mb_tracks:
        _k = _match_key(_t.get("title", ""))
        if _k:
            slot_by_title_key.setdefault(_k, _t)

    def _safe(s):
        return re.sub(r'[\\/:*?"<>|]', "_", (s or "").strip())

    user = _default_web_user()
    dest_folder = ""
    if user:
        dest_folder = _nd_find_album_folder_by(
            album, release_mbid,
            user.get("navidrome_user", ""), user.get("navidrome_password", ""))
    if dest_folder and not (os.path.isabs(dest_folder) and _path_in_library(dest_folder)):
        print(f"  _deterministic_album_import: ignoring dest outside library: {dest_folder!r}")
        dest_folder = ""
    if not dest_folder:
        dest_folder = os.path.join(MUSIC_LIBRARY_PATH,
                                   _safe(artist) or "Unknown Artist",
                                   _safe(album) or "Unknown Album")
    try:
        os.makedirs(dest_folder, exist_ok=True)
    except Exception as e:
        return {"ok": False, "dest_folder": dest_folder, "moved": 0, "per_file": [],
                "error": f"Could not create {dest_folder}: {e}"}
    try:
        os.chmod(dest_folder, 0o775)
    except Exception as e:
        print(f"  _deterministic_album_import chmod {dest_folder}: {e}")

    # Signatures of what is *already* in the destination folder, built once and
    # kept current as files land, so two downloaded copies of one song can't both
    # be placed either.
    _dest_sigs = {"built": False, "by_path": {}}

    def _dest_signatures() -> dict:
        if not _dest_sigs["built"]:
            for row in _audio_files_in_folder(dest_folder, 400):
                sig = _audio_signature(row["path"])
                if sig:
                    _dest_sigs["by_path"][row["path"]] = sig
            _dest_sigs["built"] = True
        return _dest_sigs["by_path"]

    # Derived from the group rather than demanded of every caller: five routes
    # place a folder, and a hand-picked file must survive all of them.
    if manual_pairs is None and group_id:
        with _review_lock:
            manual_pairs = _group_manual_pairs(_find_review_group(group_id))
    manual_pairs = {k: v for k, v in (manual_pairs or {}).items() if k and v}
    manual_paths = {os.path.normpath(p) for p in manual_pairs.values()}
    force_paths = {os.path.normpath(p) for p in (skip_audio_guard_for or ()) if p}

    def _reject_reason(path: str, slot: dict, title: str) -> str:
        """Why this file must not be placed into this slot — "" to go ahead.

        Two refusals, both requiring positive evidence. A file with no usable
        tags and no md5 — the common Soulseek case — has nothing to contradict
        and places exactly as before; the guard must never turn a working fill
        into a no-op.

        A hand-picked file keeps only the first refusal, and even that can be
        overridden through "Place anyway".
        """
        manual = os.path.normpath(path) in manual_paths
        sig = _audio_signature(path)
        if not sig:
            return ""
        if os.path.normpath(path) not in force_paths:
            for other, other_sig in _dest_signatures().items():
                if _same_audio_exact(sig, other_sig):
                    return (f"this audio is already in the album as "
                            f"{os.path.basename(other)}")
        if not slot or manual:
            return ""
        slot_mbid = slot.get("recording_mbid", "")
        own_mbids = sig.get("own_mbids") or set()
        if slot_mbid and own_mbids and slot_mbid not in own_mbids:
            return (f"source file is a different recording"
                    + (f' ("{sig["own_title"]}")' if sig.get("own_title") else ""))
        own_key = sig.get("own_title_key", "")
        slot_key = _match_key(title)
        if own_key and slot_key and own_key != slot_key:
            # Only a *confident* contradiction: the file says it is a different
            # track that is itself on this release. A merely unfamiliar title
            # (alternate spelling, a version suffix) is not evidence of a
            # mis-slot, so it still places.
            other_slot = slot_by_title_key.get(own_key)
            if other_slot is not None and other_slot is not slot:
                return (f'source file is tagged "{sig["own_title"]}", '
                        f'which is track {other_slot.get("position", 0)} '
                        f'of this release, not "{title}"')
        return ""

    def _place(path: str, name: str, track_num: int, title: str, mb_trackid: str,
               slot: dict = None) -> tuple:
        refusal = _reject_reason(path, slot, title)
        if refusal:
            print(f"  _deterministic_album_import refusing {name}: {refusal}")
            return False, refusal
        existing = _audio_file_tags(path)
        tags = {
            "title":             title,
            "track":             str(track_num) if track_num else "",
            "tracktotal":        str(tracktotal),
            "disc":              existing.get("disc", "1") or "1",
            "disctotal":         existing.get("disctotal", "1") or "1",
            "album":             album,
            "albumartist":       artist,
            "artist":            existing.get("artist", artist) or artist,
            "mb_albumid":        release_mbid,
            "mb_releasegroupid": rgid,
            "mb_trackid":        mb_trackid,
            "year":              year or existing.get("date", "") or existing.get("year", ""),
        }
        _mutagen_write_tags(path, tags)
        ext = _file_ext(path)
        fname = (f"{track_num:02d} - {_safe(title)}.{ext}"
                 if track_num else f"{_safe(title)}.{ext}")
        dest = os.path.join(dest_folder, fname)
        if os.path.exists(dest):
            # A name collision alone is not proof of a duplicate — the audio check
            # above already refused the cases where it is one — so a distinct
            # recording that merely wants the same filename still gets placed.
            stem, dot_ext = os.path.splitext(dest)
            dest = f"{stem}_new{dot_ext}"
        try:
            # copy_function=copyfile, not the default copy2: /downloads and
            # /music are separate mounts, so this move is always a copy, and
            # copy2's copystat would stamp slskd's mode bits and mtime onto the
            # new file — defeating the umask and leaving the track looking as
            # old as its download. copyfile creates it fresh instead.
            shutil.move(path, dest, copy_function=shutil.copyfile)
            # Explicit, because a same-filesystem move is an os.rename, which
            # preserves the source mtime *and mode* whatever the copy_function
            # says — slskd writes 0644/0444, so without this the placed track is
            # read-only to the users group.
            try:
                os.chmod(dest, 0o664)
            except Exception as e:
                print(f"  _deterministic_album_import chmod {dest}: {e}")
            _touch(dest)
            # Keep the destination index current so a second copy of this song in
            # the same batch is refused rather than filed alongside it.
            if _dest_sigs["built"]:
                placed_sig = _audio_signature(dest)
                if placed_sig:
                    _dest_sigs["by_path"][dest] = placed_sig
            return True, ""
        except Exception as e:
            print(f"  _deterministic_album_import move error {path}: {e}")
            return False, f"could not move into the library: {e}"

    moved = 0
    per_file = []
    used: set = set()

    def _slot_ident(mb_track: dict) -> dict:
        """Identity of the tracklist slot a per_file row is about, so callers can
        map the outcome back onto the review track they asked us to place."""
        return {"recording_mbid": mb_track.get("recording_mbid", ""),
                "position": mb_track.get("position", 0),
                "title": mb_track.get("title", "")}

    def _match_slot(mb_track: dict) -> tuple:
        """Pair a tracklist slot with one downloaded file, or explain why not.

        Same matcher as acquisition: a recording-MBID tag is proof, otherwise the
        ranked filename match with the sibling tighter-claim discard. The old
        substring-and-duration matcher here is what let a loose hit on the wrong
        file through — a file agreeing on neither title nor MBID is not evidence
        of a slot, however close its duration is.
        """
        avail = [f for f in files if f["path"] not in used]
        mbid = mb_track.get("recording_mbid", "")
        # The user's own pairing, made through "Pick a file…". Without this,
        # placement re-matches on its own and quietly undoes the choice — and a
        # file picked for one slot must not be claimed by another, which is the
        # same tighter-claim mistake in a new place.
        if manual_pairs:
            wanted = (manual_pairs.get(mbid)
                      or manual_pairs.get(_match_key(mb_track.get("title", ""))))
            want_norm = os.path.normpath(wanted) if wanted else ""
            if want_norm:
                hit = next((f for f in avail
                            if os.path.normpath(f["path"]) == want_norm), None)
                if hit:
                    return hit, "manual", "you picked this file for this track"
            avail = [f for f in avail
                     if os.path.normpath(f["path"]) not in manual_paths
                     or os.path.normpath(f["path"]) == want_norm]
        if mbid:
            tagged = [f for f in avail if mbid in _tag_recording_mbids(f["tags"])]
            if len(tagged) == 1:
                return tagged[0], "exact", "recording MBID tag matched"
            if len(tagged) > 1:
                return None, "", f"{len(tagged)} files carry this recording MBID"
        pool = [{"filename": f["relpath"], "length": f.get("duration") or 0, "_row": f}
                for f in avail]
        best, basis, note = _best_file_match(mb_track, pool, set(),
                                             siblings=sibling_titles,
                                             release_tracks=mb_tracks)
        if best:
            # Name the evidence honestly. A duration or track-number pairing is
            # a real match but a weaker one than a title agreeing, and the
            # placement row should not present the two as equally certain.
            confidence, why = {
                "exact":     ("high",   "filename matched the track title exactly"),
                "prefix":    ("high",   "filename started with the track title"),
                "contained": ("high",   "filename contained the track title"),
                "fuzzy":     ("medium", "filename closely resembled the track title"),
                "duration":  ("medium", "no title matched; the duration did, unambiguously"),
            }.get(basis, ("medium", "filename matched the track"))
            return best["_row"], confidence, why
        return None, "", (f"no downloaded file matched — {note}" if note
                          else "no downloaded file matched")

    if mb_tracks:
        for mb_track in slots:
            file_row, confidence, reason = _match_slot(mb_track)
            if file_row is None:
                per_file.append({"file": mb_track.get("title", "") or f"track {mb_track.get('position', 0)}",
                                 "status": "ambiguous" if "carry this recording" in reason
                                           else "unmatched",
                                 "confidence": confidence, "reason": reason,
                                 **_slot_ident(mb_track)})
                continue
            path = file_row["path"]
            used.add(path)
            name = file_row.get("name", os.path.basename(path))
            title = mb_track.get("title") or os.path.splitext(name)[0]
            ok, why = _place(path, name, mb_track.get("position", 0), title,
                             mb_track.get("recording_mbid", ""), slot=mb_track)
            if ok:
                moved += 1
            per_file.append({"file": file_row.get("relpath", name),
                             "status": "matched" if ok else "rejected",
                             "confidence": confidence, "reason": why or reason,
                             **_slot_ident(mb_track)})
        # Files left over have no tracklist slot (bonus/extra tracks) -- place
        # them best-effort using their own tags rather than leaving them behind.
        # They go through the same guard: this pass used to be a second way to
        # add a copy of a song the album already had.
        for f in files:
            if f["path"] in used:
                continue
            existing = f["tags"]
            track_num = _tag_track_number(existing)
            title = existing.get("title", "") or os.path.splitext(f["name"])[0]
            ok, why = _place(f["path"], f["name"], track_num, title,
                             existing.get("musicbrainz_trackid", ""))
            if ok:
                moved += 1
            # No _slot_ident: a bonus row is about no tracklist slot, so
            # _placement_outcomes must not be able to key a review track to it.
            per_file.append({"file": f.get("relpath", f["name"]),
                             "status": "bonus" if ok else "rejected",
                             "confidence": "none",
                             "reason": why or "no tracklist slot; placed using own tags"})
    else:
        for f in files:
            existing = f["tags"]
            track_num = _tag_track_number(existing)
            title = existing.get("title", "") or os.path.splitext(f["name"])[0]
            ok, why = _place(f["path"], f["name"], track_num, title,
                             existing.get("musicbrainz_trackid", ""))
            if ok:
                moved += 1
            per_file.append({"file": f.get("relpath", f["name"]),
                             "status": "matched" if ok else "rejected",
                             "confidence": "none",
                             "reason": why or "no release tracklist available"})

    if moved:
        # The album folder's mtime moves on its own when a file lands in it, but
        # the artist dir above it does not — bump both so anything reading the
        # library by modification time sees this album as touched today. This is
        # what lets an externally-run AudioMuse analysis pick up the filled album
        # (with ND_RECENTLYADDEDBYMODTIME=true on Navidrome — see CLAUDE.md).
        _touch(dest_folder)
        _touch(os.path.dirname(dest_folder))

    error = ""
    if not moved:
        # Name the first refusal rather than the generic failure: "already in the
        # album" and "tagged as a different track" are the two outcomes a user
        # can actually act on, and both used to read as a mystery move failure.
        reasons = [r.get("reason", "") for r in per_file
                   if r.get("status") == "rejected" and r.get("reason")]
        error = (f"Nothing placed: {reasons[0]}" if reasons
                 else "No files could be moved to destination")
    return {
        "ok": moved > 0,
        "dest_folder": dest_folder,
        "moved": moved,
        "per_file": per_file,
        "error": error,
    }


def _deterministic_import_task(task_id: str, album_dir: str, release_mbid: str,
                                artist: str = "", album: str = "",
                                group_id: str = "") -> None:
    _task_update(task_id, current=album_dir)
    result = _deterministic_album_import(album_dir, release_mbid, artist, album,
                                         group_id=group_id)
    if result["ok"]:
        _imported_folders.add(album_dir)
        _nd_scan_after_import(_default_web_user().get("telegram_token", ""))
        if group_id:
            with _review_lock:
                group = _find_review_group(group_id)
                if group:
                    _mark_group_tracks_placed(group, result)
            _save_review_state()
    moved = result.get("moved", 0)
    dest = result.get("dest_folder", "")
    msg = (f"Placed {moved} file(s) into {os.path.basename(dest)}"
           if result["ok"] else result.get("error", ""))
    _task_finish(task_id, msg, "" if result["ok"] else msg, per_file=result.get("per_file", []))


def _place_track_anyway(group: dict, track_index) -> dict:
    """Re-run placement for one hand-picked file with the audio guard disabled.

    Reachable only from a track that reported `can_force_place` — i.e. the user
    has already been told, by name, which file in the album this one duplicates.
    """
    tracks = group.get("missing_tracks", []) or []
    try:
        idx = int(track_index)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Bad track index"}
    if not (0 <= idx < len(tracks)):
        return {"ok": False, "error": "Track index out of range"}
    track = tracks[idx]
    local = track.get("local_path", "")
    if not local or not os.path.isfile(local):
        return {"ok": False,
                "error": "The downloaded file is no longer in the download folder"}
    if not track.get("can_force_place"):
        return {"ok": False,
                "error": "This track has no refused placement to override"}
    keys = [k for k in (track.get("recording_mbid", "") or track.get("mbid", ""),
                        _match_key(track.get("title", ""))) if k]
    result = _deterministic_album_import(
        os.path.dirname(local), group.get("canonical_mbid", ""),
        group.get("artist", ""), group.get("album", ""),
        group.get("id", ""),
        only_relpaths=[os.path.basename(local)],
        manual_pairs={k: local for k in keys},
        skip_audio_guard_for=[local])
    if result.get("ok"):
        track.pop("can_force_place", None)
        track.pop("force_place_conflict", None)
        # The track is "downloaded" as far as bookkeeping goes; let the normal
        # per_file accounting flip it to placed (or back to failed).
        track["decision"] = "downloaded"
        _mark_group_tracks_placed(group, result)
        _nd_scan_after_import((_default_web_user() or {}).get("telegram_token", ""))
    return {"ok": bool(result.get("ok")),
            "error": result.get("error", ""),
            "import_result": result}


async def _finalize_group(bot, ag_id: str):
    """
    Finalize a completed (or timed-out) album group: import the whole folder
    once in album mode, pinned to the resolved MusicBrainz release, then report.
    """
    ag = pending_album_groups.pop(ag_id, None)
    if not ag:
        return
    label   = ag["label"]
    errored = ag["total"] - ag["completed"]
    fails   = ag["failed"] + max(0, errored - ag["failed"])

    import_note = ""
    status      = "downloaded"
    album_dir   = ""
    # Remote clients watch this release through /api/album/status; the group is
    # about to be gone from pending_album_groups, so every exit below has to say
    # where the fill ended up.
    fill_mbid = ag.get("target_release_mbid") or ag.get("release_mbid", "")
    if ag["completed"] <= 0:
        _album_fill_set(fill_mbid, "failed",
                        reason=f"No file downloaded ({fails} failed)")
    if ag["completed"] > 0 and not ag["local_dirs"]:
        import_note = (
            f"\n⚠️ Downloaded files could not be located under {SLSKD_DOWNLOAD_DIR}. "
            "Check the shared downloads volume and place them manually.")
        _album_fill_set(fill_mbid, "failed",
                        reason="Downloaded, but the files could not be found in "
                               "the downloads volume")
    if ag["completed"] > 0 and ag["local_dirs"]:
        album_dir = max(ag["local_dirs"].items(), key=lambda kv: kv[1])[0]
        if ag.get("match_mode") == "manual" and ag.get("review_group_id"):
            status = "needs_match"
            recid = _record_import_recovery(
                ag["token"], ag["chat_id"], label, album_dir,
                ag.get("artist", ""), ag.get("album", ""),
                ag.get("release_mbid", ""), status,
                ag.get("review_group_id", ""), "manual",
                {"recommended_action": "manual_match", "still_in_downloads": True})
            _save_state()
            _save_review_state()
            _album_fill_set(fill_mbid, "needs_match", groupId=recid)
            await _tg_send(bot, ag["chat_id"],
                           f"Downloaded {label}. Waiting for web match/import review.",
                           reply_markup=_album_action_markup(recid, "needs_review"))
            return
        _album_fill_set(fill_mbid, "placing")
        await _tg_send(bot, ag["chat_id"], f"📚 Placing {label} into your library…")
        # Every directory that received a file, busiest first — not just the
        # majority one. A file that failed over to another peer downloads into
        # *that* peer's directory, and importing only the majority directory left
        # it stranded in /downloads while its track reported as still missing.
        import_dirs = [d for d, _n in sorted(ag["local_dirs"].items(),
                                             key=lambda kv: kv[1], reverse=True)]
        result = await asyncio.to_thread(
            _deterministic_album_import, import_dirs,
            ag.get("release_mbid", ""), ag.get("artist", ""), ag.get("album", ""),
            ag.get("review_group_id", ""))
        if result["ok"]:
            status = "imported"
            _imported_folders.add(album_dir)
            dest_name = os.path.basename(result["dest_folder"])
            import_note = f"\n📚 Placed {result['moved']} file(s) into {dest_name}."
            if await asyncio.to_thread(_nd_scan_after_import, ag["token"]):
                import_note += "\n🔄 Navidrome quick scan triggered."
            if ag.get("review_group_id"):
                with _review_lock:
                    group = _find_review_group(ag.get("review_group_id", ""))
                    if group:
                        _mark_group_tracks_placed(group, result)
                _save_review_state()
            _album_fill_set(fill_mbid, "placed", moved=result.get("moved", 0),
                            destFolder=os.path.basename(result.get("dest_folder", "")),
                            reason="")
            # The library index still says this release-group is missing, and
            # every client's "not in your library" list reads that row — so a
            # freshly filled album would sit in both lists until the next scan.
            _index_mark_release_present(
                rgid=_album_fill_get(fill_mbid).get("rgid", ""),
                group_id=ag.get("review_group_id", ""))
            _notify_hub_library_change(
                fill_mbid, rgid=_album_fill_get(fill_mbid).get("rgid", ""),
                artist=ag.get("artist", ""), album=ag.get("album", ""))
            # A review group already has _verify_placement_worker polling its
            # tracks; an artist-page download has no group, so nothing else would
            # ever turn "placed" into "it is really in your library".
            if not ag.get("review_group_id"):
                _start_album_fill_verification(fill_mbid, result,
                                               artist=ag.get("artist", ""))
        else:
            status = "failed"
            import_note = f"\n⚠️ Placement failed: {result['error']}"
            _album_fill_set(fill_mbid, "failed", reason=result.get("error", ""))

    if fails:
        head = (f"⚠️ Album finished with errors: {label}\n"
                f"{ag['completed']}/{ag['total']} files downloaded, {fails} failed")
    else:
        head = f"✅ Album complete: {label}\n{ag['completed']}/{ag['total']} files"
    text = head + import_note

    markup = None
    if album_dir:
        recid = _uid("rec")
        _uid_to_token[recid] = ag["token"]
        _albums[recid] = {
            "token":           ag["token"],
            "chat_id":         ag["chat_id"],
            "label":           label,
            "artist":          ag.get("artist", ""),
            "album":           ag.get("album", ""),
            "release_mbid":    ag.get("release_mbid", ""),
            "rgid":            "",
            "album_dir":       album_dir,
            "status":          status,
            "import_state":    "imported" if status == "imported" else "downloaded_not_imported",
            "review_group_id": ag.get("review_group_id", ""),
            "match_mode":      ag.get("match_mode", "auto"),
            "ts":              time.time(),
        }
        if ag.get("review_group_id") and status != "imported":
            with _review_lock:
                group = _find_review_group(ag.get("review_group_id", ""))
                if group is not None:
                    group.setdefault("match_items", []).append({
                        "id":           recid,
                        "label":        label,
                        "album_dir":    album_dir,
                        "release_mbid": ag.get("release_mbid", ""),
                        "status":       status,
                        "import_state": "downloaded_not_imported",
                        "created_at":   time.time(),
                    })
                    group["updated_at"] = time.time()
        markup = _album_action_markup(recid, status)
        if len(_albums) > 500:
            _albums.pop(next(iter(_albums)), None)

    pmid = ag.get("progress_msg_id")
    if pmid:
        try:
            await bot.edit_message_text(chat_id=ag["chat_id"], message_id=pmid,
                                        text=text, reply_markup=markup)
            return
        except Exception:
            pass
    await _tg_send(bot, ag["chat_id"], text, reply_markup=markup)

async def poll_downloads_loop(apps: list):
    """
    Background task: poll slskd transfer statuses.
    On completion: run beets, send Telegram confirmation.
    On failure: try next candidate or send retry prompt.

    Matching is resilient: slskd may report a transfer filename that differs
    slightly from what we enqueued (slash/case normalization), so we fall back
    to matching by (username, basename) when the exact key misses. Album groups
    that never reach their total are finalized by a time-based sweep.
    """
    token_to_app = {u["telegram_token"]: app for u, app in zip(USERS, apps)}

    while True:
        await asyncio.sleep(DOWNLOAD_POLL_INT)
        try:
            await _poll_downloads_once(token_to_app)
        except Exception as e:
            # The poll loop must never die — downloads would stop finalizing.
            import traceback
            traceback.print_exc()
            print(f"  poll_downloads_loop iteration error: {e}")

async def _poll_downloads_once(token_to_app: dict):
        def _match_key(username: str, filename: str):
            key = (username, filename)
            if key in pending_downloads:
                return key
            base = filename.replace("\\", "/").rstrip("/").split("/")[-1].lower()
            for (u, fn) in list(pending_downloads.keys()):
                if u != username:
                    continue
                if fn.replace("\\", "/").rstrip("/").split("/")[-1].lower() == base:
                    return (u, fn)
            return None

        # Sweep: finalize album groups that have gone quiet for too long, so a
        # never-matched file can't leave a group dangling forever.
        #
        # Two triggers, not one. The TTL catches a group still waiting on a
        # transfer; the orphan check catches the commoner failure, where slskd
        # has forgotten every file of a group whose counters never reached
        # `total` — a peer that vanished mid-album, a row cleared by hand. That
        # group can make no further progress by definition, and leaving it in
        # pending_album_groups strands the downloaded files in /downloads, keeps
        # the album on the Downloads tab, and never resolves the fill any client
        # is watching. The grace period is only there so a group cannot be
        # judged orphaned in the moments between its creation and its files
        # being registered.
        now = time.time()
        for ag_id in list(pending_album_groups.keys()):
            ag = pending_album_groups.get(ag_id)
            if not ag:
                continue
            if ag.get("switching"):
                continue  # mid-failover: its files are being re-enqueued
            age = now - ag.get("ts", now)
            orphaned = (age > ALBUM_GROUP_ORPHAN_GRACE
                        and not any(info.get("album_group_id") == ag_id
                                    for info in pending_downloads.values()))
            if age > ALBUM_GROUP_TTL or orphaned:
                # A bot-less finalize is fine — _tg_send no-ops on a falsy bot.
                # Refusing to finalize without a Telegram app was what left
                # web-started fills dangling until the 6h TTL, on every poll.
                app = token_to_app.get(ag["token"])
                print(f"  album group {ag_id} "
                      f"{'has no live transfers' if orphaned else 'timed out'}"
                      f" — finalizing")
                await _finalize_group(app.bot if app else None, ag_id)

        if not pending_downloads:
            return

        downloads = await asyncio.to_thread(slskd_get_all_downloads, True)

        for dl in downloads:
            username = dl.get("_username", "")
            filename = dl.get("filename", "")
            state    = dl.get("state", "")
            key      = _match_key(username, filename)
            if key is None:
                continue

            info    = pending_downloads[key]
            token   = info["token"]
            chat_id = info["chat_id"]
            track   = info["track"]
            percent = _slskd_transfer_percent(dl)
            info["latest_state"] = state
            info["percent"] = percent
            info["updated_at"] = time.time()
            info["raw_transfer"] = {
                k: dl.get(k) for k in (
                    "bytesDownloaded", "bytesTransferred", "downloaded",
                    "size", "fileSize", "averageSpeed", "speed", "state"
                ) if k in dl
            }
            review_gid = info.get("review_group_id", "")
            review_idx = info.get("review_track_index")
            repair_job_id = info.get("repair_job_id", "")
            if review_gid and not _slskd_succeeded(state) and not _slskd_failed(state):
                _set_review_track_state(review_gid, review_idx, "downloading",
                                        download_percent=percent,
                                        download_state=state,
                                        source_user=username,
                                        filename=filename)
            if repair_job_id and not _slskd_succeeded(state) and not _slskd_failed(state):
                _repair_update_download(repair_job_id, username, key[1],
                                        "downloading", percent=percent,
                                        raw_state=state)
            label   = (f"{track['artist']} - {track['title']}"
                       if track else key[1].split('/')[-1].split('\\')[-1])
            app     = token_to_app.get(token)
            if not app:
                continue
            bot = app.bot

            # Stall watchdog: cancel if no byte progress for too long. slskd will
            # set the state to Cancelled on the next poll, which fires the
            # existing failure-branch failover naturally.
            #
            # A file the peer hasn't started serving yet ("Queued, Remotely" /
            # "Initializing") is not stalled — it is waiting its turn, which for
            # the tail of an album is routine and can take many minutes. Those
            # get QUEUE_TIMEOUT; only a transfer that has actually been running
            # gets the short STALL_TIMEOUT.
            if not _slskd_succeeded(state) and not _slskd_failed(state):
                raw = info.get("raw_transfer", {})
                cur_bytes = (raw.get("bytesDownloaded") or raw.get("bytesTransferred") or 0)
                st = state or ""
                waiting = ("Queued" in st) or ("Initializ" in st)
                if cur_bytes != info.get("_stall_bytes", -1):
                    info["_stall_bytes"] = cur_bytes
                    info["_stall_at"] = now
                elif info.get("_was_waiting", True) and not waiting:
                    # Just promoted out of the peer's queue — give it a fresh
                    # clock rather than judging it on its time spent waiting.
                    info["_stall_at"] = now
                info["_was_waiting"] = waiting
                limit = QUEUE_TIMEOUT if waiting else STALL_TIMEOUT
                stuck_for = now - info.get("_stall_at", now)
                if stuck_for > limit:
                    print(f"  Stall watchdog: {label} stuck at {cur_bytes} bytes for "
                          f"{int(stuck_for)}s in state {st!r} — cancelling")
                    await asyncio.to_thread(_slskd_cancel, username, filename)
                    info["_stall_at"] = now  # prevent re-trigger before Cancelled state arrives

            if _slskd_succeeded(state):
                ag_id = info.get("album_group_id")
                local_path = await asyncio.to_thread(_resolve_local_path, filename)
                info["local_path"] = local_path or ""
                if repair_job_id:
                    _repair_update_download(repair_job_id, username, key[1],
                                            "complete", local_path=local_path or "",
                                            percent=100, raw_state=state)
                if review_gid:
                    _set_review_track_state(review_gid, review_idx, "downloaded",
                                            download_percent=100,
                                            download_state=state,
                                            local_path=local_path or "",
                                            source_user=username,
                                            filename=filename)
                del pending_downloads[key]
                if ag_id and ag_id in pending_album_groups:
                    # Part of an album — do NOT import per file. Record where it
                    # landed and import the whole folder once on finalize.
                    ag = pending_album_groups[ag_id]
                    ag["completed"] += 1
                    ag["ts"] = time.time()
                    if local_path:
                        d = os.path.dirname(local_path)
                        ag["local_dirs"][d] = ag["local_dirs"].get(d, 0) + 1
                    await _update_group_progress(bot, ag)
                    if ag["completed"] + ag["failed"] >= ag["total"]:
                        await _finalize_group(bot, ag_id)
                else:
                    # Loose individual track — tag with mutagen and trigger rescan.
                    if local_path:
                        info_track = track or {}
                        tag_updates = {k: v for k, v in {
                            "title":      info_track.get("title", ""),
                            "artist":     info_track.get("artist", ""),
                            "mb_trackid": info_track.get("mbid", ""),
                        }.items() if v}
                        if tag_updates:
                            _mutagen_write_tags(local_path, tag_updates)
                        file_msg = "completed"
                        if await asyncio.to_thread(_nd_scan_after_import, token):
                            file_msg += " · 🔄 Navidrome scan triggered"
                    else:
                        file_msg = "completed (file not found on disk)"
                        print(f"  could not locate {filename} under {SLSKD_DOWNLOAD_DIR}")
                    await _tg_send(bot, chat_id, f"✅ Download {file_msg}: {label}")

            elif _slskd_failed(state):
                ag_id = info.get("album_group_id")
                is_cancelled = "Cancelled" in (state or "")
                is_timeout = "TimedOut" in (state or "") or "Timeout" in (state or "")
                if repair_job_id:
                    _repair_update_download(
                        repair_job_id, username, key[1],
                        "cancelled" if is_cancelled else ("timeout" if is_timeout else "error"),
                        state, "transfer_timeout" if is_timeout else "",
                        percent=percent, raw_state=state)
                if review_gid:
                    decision = "cancelled" if is_cancelled else "failed"
                    _set_review_track_state(review_gid, review_idx, decision,
                                            download_percent=percent,
                                            download_state=state,
                                            error=state,
                                            source_user=username,
                                            filename=filename)
                del pending_downloads[key]
                if ag_id and ag_id in pending_album_groups:
                    ag = pending_album_groups[ag_id]
                    # If nothing has downloaded yet, the source is bad. Don't
                    # auto-switch — abandon it and let the user pick the next
                    # source manually (or retry a fresh search if none remain).
                    if ag["completed"] == 0 and not ag.get("switching"):
                        ag["switching"] = True
                        await asyncio.to_thread(_abandon_group_downloads, ag_id)
                        pending_album_groups.pop(ag_id, None)
                        alts  = ag.get("alt_sources") or []
                        label = ag["label"]
                        # Update the now-stale progress line so it doesn't sit
                        # there claiming the album is still downloading.
                        pmid = ag.get("progress_msg_id")
                        if pmid:
                            try:
                                await bot.edit_message_text(
                                    chat_id=ag["chat_id"], message_id=pmid,
                                    text=f"❌ Source @{ag.get('source_user','?')} "
                                         f"failed for {label}.")
                            except Exception:
                                pass
                        if alts:
                            group = {"release_mbid": ag.get("release_mbid", ""),
                                     "artist": ag.get("artist", ""),
                                     "album": ag.get("album", ""),
                                     "total_tracks": 0,
                                     "missing_tracks": ag.get("missing_tracks", [])}
                            await _tg_send(bot, ag["chat_id"],
                                f"❌ Source for {label} failed to queue. "
                                f"Pick another source:")
                            await _send_album_prompt(bot, ag["chat_id"], ag["token"],
                                                     label, group, alts)
                        else:
                            rid = _uid("aretry")
                            _uid_to_token[rid] = ag["token"]
                            _pending_album[rid] = {
                                "token": ag["token"], "chat_id": ag["chat_id"],
                                "artist": ag.get("artist", ""), "title": ag.get("album", ""),
                                "release_mbid": ag.get("release_mbid", ""),
                                "total_tracks": 0, "query": ag.get("retry_query", "")}
                            await _tg_send(bot, ag["chat_id"],
                                f"❌ {label} failed to queue and there are no other "
                                f"sources.",
                                reply_markup=InlineKeyboardMarkup([[
                                    InlineKeyboardButton("🔁 Search again",
                                                         callback_data=f"ARETRY|{rid}")]]))
                    else:
                        # The group is otherwise progressing, so the source isn't
                        # globally bad — just bad for this file. Fail this one
                        # file over to another peer rather than counting it lost
                        # and finalizing the album short.
                        retry_user = await asyncio.to_thread(
                            _retry_file_from_alt_source, ag, ag_id, info, username)
                        if retry_user:
                            ag["ts"] = time.time()
                            print(f"  {label}: {username} failed ({state}) — "
                                  f"retrying from @{retry_user}")
                        else:
                            ag["failed"] += 1
                            ag["ts"] = time.time()
                            await _update_group_progress(bot, ag)
                            if ag["completed"] + ag["failed"] >= ag["total"]:
                                await _finalize_group(bot, ag_id)
                else:
                    # `candidates` holds the remaining alternative sources
                    # (never including the one that just failed).
                    remaining = info.get("candidates", [])
                    if remaining:
                        next_c    = remaining[0]
                        best_file = min(next_c["files"],
                                        key=lambda f: _score_file(f, next_c["upload_speed"]) or (99,))
                        if await asyncio.to_thread(
                                slskd_enqueue, next_c["username"], best_file,
                                track=track, token=token, chat_id=chat_id,
                                candidates=remaining[1:],
                                repair_job_id=info.get("repair_job_id", ""),
                                repair_track_id=info.get("repair_track_id", "")):
                            await _tg_send(bot, chat_id,
                                f"🔁 Download failed for {label} ({state}) — "
                                f"retrying with next source…")
                        else:
                            await _tg_send(bot, chat_id,
                                f"Download failed and retry also failed: {label}")
                    else:
                        await _tg_send(bot, chat_id,
                            f"❌ Download failed, no more sources: {label} ({state})")

# ---------------------------------------------------------------------------
# Library completeness check (Feature 6)
# ---------------------------------------------------------------------------

def find_incomplete_albums(nd_user: str, nd_pass: str, progress=None) -> list:
    """
    Compare every album in Navidrome against its MusicBrainz tracklist.
    Returns incomplete albums sorted by completion ratio (least complete first).

    Rather than comparing raw counts (which mis-fires on multi-disc releases,
    bonus tracks, or a different pressing), we check each MusicBrainz track for
    presence in the library by title, and record exactly which titles are
    missing so the result can drive a download.
    """
    print("  Fetching all albums from Navidrome...")
    all_albums = nd_get_all_albums(nd_user, nd_pass)
    print(f"  Checking {len(all_albums)} albums against MusicBrainz...")
    incomplete = []
    candidates = [a for a in all_albums if a.get("musicBrainzId")]
    buckets = {}
    for album in candidates:
        buckets.setdefault(_album_group_key(album), []).append(album)
    grouped_candidates = list(buckets.values())

    for done, albums in enumerate(grouped_candidates, 1):
        if progress:
            try:
                progress(done, len(grouped_candidates))
            except Exception:
                pass
        records = [_album_record(a, nd_user, nd_pass) for a in albums]
        records.sort(key=lambda r: (-(int(r.get("songCount") or 0)), r["name"]))
        album = records[0]
        mbz_id     = album.get("musicBrainzId", "")
        artist     = album.get("artist", "")
        mbz_tracks = mbz_release_tracks(mbz_id)
        mbz_count  = len(mbz_tracks)
        if mbz_count == 0:
            continue
        # Build a union of present tracks across duplicate/split Navidrome album
        # rows. This avoids treating two accidental versions as two incomplete
        # albums and downloading the same missing songs twice.
        present_titles = set()
        present_mbids  = set()
        for rec in records:
            for song in rec.get("tracks", []):
                title = (song.get("title") or "").lower().strip()
                if title:
                    present_titles.add(title)
                song_mbid = song.get("musicBrainzId", "") or ""
                if song_mbid:
                    present_mbids.add(song_mbid)
        missing = [
            {"artist": artist, "title": rt["title"], "mbid": rt["mbid"],
             "position": rt.get("position", 0)}
            for rt in mbz_tracks
            if not ((rt.get("mbid") and rt.get("mbid") in present_mbids) or
                    ((rt["title"] or "").lower().strip() in present_titles))
        ]
        if missing:
            incomplete.append({
                "artist":         artist,
                "album":          album.get("name", ""),
                "mbz_id":         mbz_id,
                "album_ids":      [r["id"] for r in records],
                "duplicate_count": len(records),
                "present":        mbz_count - len(missing),
                "total":          mbz_count,
                "missing":        len(missing),
                "missing_tracks": missing,
            })
            dup_note = f", {len(records)} local versions" if len(records) > 1 else ""
            print(f"    Incomplete: {artist} - {album.get('name')} "
                  f"({mbz_count - len(missing)}/{mbz_count}{dup_note})")

    incomplete.sort(key=lambda x: x["present"] / max(x["total"], 1))
    return incomplete

# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def _nd_wait_ready(nd_user: str, nd_pass: str,
                    max_wait: int = 60, interval: int = 5) -> bool:
    """
    Wait until Navidrome's search index is responsive.
    Polls with a benign single-character query; returns True when results come
    back (index is warm), or False after max_wait seconds.
    A single-char query reliably returns results on a non-empty library.
    """
    deadline = time.time() + max_wait
    attempt  = 0
    while time.time() < deadline:
        attempt += 1
        try:
            r = _http.get(f"{NAVIDROME_URL}/rest/search3", params={
                **_nd_auth_params(nd_user, nd_pass),
                "query": "a", "songCount": "1",
                "albumCount": "0", "artistCount": "0",
            }, timeout=5)
            resp  = r.json().get("subsonic-response", {})
            songs = resp.get("searchResult3", {}).get("song", [])
            # Also accept an explicit empty result (status=ok, 0 songs) as ready —
            # distinguishes "index ready but no match" from "index not initialised"
            status = resp.get("status", "")
            if status == "ok":
                if songs:
                    print(f"  Navidrome ready (attempt {attempt}, {len(songs)} result(s))")
                else:
                    print(f"  Navidrome ready (attempt {attempt}, index empty or warming)")
                return True
        except Exception as e:
            print(f"  Navidrome readiness check error (attempt {attempt}): {e}")
        print(f"  Waiting for Navidrome search index... ({attempt})")
        time.sleep(interval)
    print(f"  Navidrome search index not ready after {max_wait}s — proceeding anyway")
    return False

def scan_user(user: dict) -> list:
    lbz_user = user["listenbrainz_user"]
    nd_user  = user["navidrome_user"]
    nd_pass  = user["navidrome_password"]
    sources  = user["playlist_sources"]

    print(f"\n=== Scanning for {lbz_user} ===")
    _nd_wait_ready(nd_user, nd_pass)
    playlist_ids = lbz_get_playlist_ids(lbz_user, sources)  # raises LBZError on API failure
    if not playlist_ids:
        # Genuinely no matching playlists (the request succeeded) — nothing to do.
        _last_scan_ts[lbz_user] = time.time()
        return []

    seen        = set()
    all_missing = []

    for name, uuid in playlist_ids.items():
        print(f"Processing '{name}'...")
        for track in lbz_get_tracks(uuid, name):   # raises LBZError on API failure
            key = track["mbid"] if track["mbid"] else (track["artist"].lower(), track["title"].lower())
            if key in seen:
                continue
            seen.add(key)
            print(f"  Checking: {track['artist']} - {track['title']}")
            if not nd_has_track(track, nd_user, nd_pass):
                all_missing.append(track)

    # Only reached on a fully successful scan — safe to record for the throttle.
    _last_scan_ts[lbz_user] = time.time()
    return all_missing

# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

async def _tg_send(bot, chat_id: str, text: str, reply_markup=None, retries: int = 3):
    """Send a Telegram message with automatic retry on timeout/network errors
    and flood control. Truncates to Telegram's 4096-char message limit so an
    oversized text can never make the send fail.

    A falsy `bot` is a no-op, not an error: album groups started from the web UI
    or a remote client have to be able to finalize on a bot-less path, and
    placement must never hinge on there being a Telegram chat to narrate it to."""
    if not bot:
        return None
    import telegram.error
    if len(text) > 4096:
        text = text[:4093] + "…"
    for attempt in range(retries):
        try:
            return await bot.send_message(
                chat_id=chat_id, text=text, reply_markup=reply_markup)
        except telegram.error.RetryAfter as e:
            # Flood control — wait exactly as long as Telegram asks, then retry.
            wait = getattr(e, "retry_after", 5) or 5
            print(f"  Telegram flood control: waiting {wait}s")
            await asyncio.sleep(wait + 0.5)
        except (telegram.error.TimedOut, telegram.error.NetworkError) as e:
            if attempt < retries - 1:
                print(f"  Telegram send timeout (attempt {attempt+1}/{retries}): {e}")
                await asyncio.sleep(2 ** attempt)
            else:
                print(f"  Telegram send failed after {retries} attempts: {e}")
        except telegram.error.BadRequest as e:
            # e.g. malformed markup — log it, don't crash the caller.
            print(f"  Telegram send rejected: {e}")
            return None
    return None

async def _send_approval(bot, chat_id, token, label, reason, file, username, track,
                         candidates: list = None):
    """`candidates` = remaining folder-level sources to fail over to (excluding
    the one this file came from)."""
    aid = _uid("a")
    pending_approvals.setdefault(token, {})[aid] = {
        "username": username, "file": file, "label": label, "track": track,
        "candidates": candidates or [],
    }
    _uid_to_token[aid] = token
    emoji = "🎤" if "live" in reason.lower() else "⚠️"
    await bot.send_message(
        chat_id=chat_id,
        text=(f"{emoji} Approval needed\n"
              f"Track: {label}\n"
              f"Reason: {reason}\n"
              f"Best result: {_format_info(file)}\n"
              f"Download anyway?"),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes", callback_data=f"AY|{aid}"),
            InlineKeyboardButton("No",  callback_data=f"AN|{aid}"),
        ]]),
    )

async def _send_retry(bot, chat_id, token, label, track, reason):
    rid = _uid("r")
    pending_retries.setdefault(token, {})[rid] = track
    _uid_to_token[rid] = token
    await bot.send_message(
        chat_id=chat_id,
        text=f"Not found: {label}\nReason: {reason}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Retry", callback_data=f"RT|{rid}"),
            InlineKeyboardButton("Skip",  callback_data=f"SK|{rid}"),
        ]]),
    )

async def _send_dup_prompt(bot, chat_id, token, label, track, existing, folders):
    """`folders` are folder-level search results (best first); pick the best
    file from the best folder and keep the rest as failover candidates."""
    did = _uid("d")
    _uid_to_token[did] = token
    username, best_file = "", {}
    if folders:
        fd        = folders[0]
        username  = fd["username"]
        best_file = min(fd["files"],
                        key=lambda f: _score_file(f, fd.get("upload_speed", 0)) or (99,))
    pending_approvals.setdefault(token, {})[did] = {
        "username":   username,
        "file":       best_file,
        "label":      label,
        "track":      track,
        "candidates": folders[1:] if folders else [],   # failover sources
    }
    ext     = existing.get("suffix", "?").upper()
    bitrate = existing.get("bitRate", "?")
    await bot.send_message(
        chat_id=chat_id,
        text=(f"Duplicate detected: {label}\n"
              f"Already in library: {ext}, {bitrate} kbps\n"
              f"New result: {_format_info(best_file) if best_file else '?'}\n"
              f"Download anyway?"),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes", callback_data=f"AY|{did}"),
            InlineKeyboardButton("No",  callback_data=f"AN|{did}"),
        ]]),
    )

# ---------------------------------------------------------------------------
# Missing tracks message
# ---------------------------------------------------------------------------

async def send_missing_tracks(app, user: dict, tracks: list):
    token   = user["telegram_token"]
    chat_id = user["chat_id"]
    bot     = app.bot

    grouped = {}
    for t in tracks:
        grouped.setdefault(t.get("playlist", "Unknown"), []).append(t)

    missing_by_playlist.setdefault(token, {}).update(grouped)

    for playlist_name, playlist_tracks in grouped.items():
        pl_uid = _register_playlist(token, playlist_name)
        header = f"Missing tracks - {playlist_name} ({len(playlist_tracks)} tracks)"
        chunks, cur, cur_len = [], [header], len(header)
        for t in playlist_tracks:
            line = f"- {t['artist']} - {t['title']}"
            if cur_len + len(line) + 1 > 3900:
                chunks.append("\n".join(cur))
                cur, cur_len = [f"{playlist_name} (continued)"], 0
            cur.append(line)
            cur_len += len(line) + 1
        chunks.append("\n".join(cur))
        kb = [[InlineKeyboardButton(f"Download all ({len(playlist_tracks)})",
                                    callback_data=f"dlall|{pl_uid}")]]
        for i, chunk in enumerate(chunks):
            await bot.send_message(
                chat_id=chat_id, text=chunk,
                reply_markup=InlineKeyboardMarkup(kb) if i == len(chunks) - 1 else None,
            )

# ---------------------------------------------------------------------------
# Callback handler
# ---------------------------------------------------------------------------

def _user_nd(token):
    u = _user_for_token(token)
    return (u["navidrome_user"], u["navidrome_password"]) if u else ("", "")

async def _edit_or_send(query, context, text, reply_markup=None):
    """Edit the message the button hangs off; if Telegram refuses the edit
    (message >48h old, deleted, or not editable) send a new message instead of
    dying silently."""
    import telegram.error
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except telegram.error.TelegramError as e:
        print(f"  edit_message_text failed ({e}) — sending a new message instead")
        chat_id = query.message.chat_id if query.message else None
        if chat_id is None:
            raise
        await context.bot.send_message(chat_id=chat_id, text=text,
                                       reply_markup=reply_markup)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import telegram.error
    query  = update.callback_query
    print(f"  callback: {query.data}", flush=True)
    try:
        await query.answer()
    except telegram.error.BadRequest as e:
        if "query is too old" in str(e).lower() or "query id is invalid" in str(e).lower():
            print(f"  Stale callback query ignored: {e}")
            try:
                chat_id = query.message.chat_id if query.message else None
                if chat_id is not None:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="⚠️ That button expired (the bot restarted or too much "
                             "time passed since it was sent) — please re-run the "
                             "command that created it (e.g. /scan) to get a fresh one.")
            except Exception as notify_err:
                print(f"  Failed to notify about stale callback: {notify_err}")
            return
        raise
    except (telegram.error.TimedOut, telegram.error.NetworkError) as e:
        # Answering the callback is cosmetic (clears the spinner) — a transient
        # network failure here must not abort the action itself.
        print(f"  callback answer() failed ({e}) — continuing with the action")
    parts  = query.data.split("|")
    action = parts[0]

    # Album pick: user chose a specific folder
    if action == "AP":
        aid, idx = parts[1], int(parts[2])
        token    = _token_for(aid)
        pending  = pending_approvals.get(token, {}).pop(aid, None)
        if not pending:
            await query.edit_message_text("Already handled.")
            return
        folders = pending.get("folders", [])
        if idx >= len(folders):
            await query.edit_message_text("Result expired.")
            return
        fd        = folders[idx]
        label     = pending["label"]
        username  = fd["username"]
        folder    = fd["folder"]
        chat_id_s = str(query.message.chat_id)

        await query.edit_message_text(
            f"Expanding directory from @{username}...\n{folder.replace(chr(92), '/').split('/')[-1]}")

        # Expand the directory to get ALL tracks, not just those in the search result
        ref_file   = fd["files"][0] if fd["files"] else {}
        full_files = await asyncio.to_thread(
            slskd_expand_directory, username, fd, ref_file)

        if not full_files:
            # Fall back to whatever the search returned
            print(f"  expand_directory returned nothing — falling back to search results")
            full_files = fd["files"]

        folder_name = fd["folder"].replace(chr(92), "/").split("/")[-1]
        grp          = pending.get("group") or {}
        release_mbid = grp.get("release_mbid", "")
        alts         = [f for j, f in enumerate(folders) if j != idx][:6]
        ok, total, ag_id = await asyncio.to_thread(
            slskd_enqueue_folder,
            username, full_files, token=token, chat_id=chat_id_s,
            label=folder_name, release_mbid=release_mbid,
            alt_sources=alts, artist=grp.get("artist", ""),
            album=grp.get("album", "") or label,
            missing_tracks=grp.get("missing_tracks", []))
        msg = await context.bot.send_message(
            chat_id=chat_id_s,
            text=f"⬇️ Downloading {label}: 0/{total} files {_progress_bar(0, total)}\n"
                 f"from @{username}: {folder_name}")
        if ag_id and ag_id in pending_album_groups:
            pending_album_groups[ag_id]["progress_msg_id"] = msg.message_id
        return

    # Beets: import specific folder
    if action == "BI":
        bid, idx = parts[1], int(parts[2])
        pending  = _pending_beets.pop(bid, None)
        if not pending:
            await query.edit_message_text("Already handled or expired.")
            return
        folders = pending["folders"]
        if idx >= len(folders):
            await query.edit_message_text("Folder index out of range.")
            return
        fd      = folders[idx]
        chat_id = pending["chat_id"]
        await query.edit_message_text(f"Queuing beets import for:\n{fd['name']}")
        asyncio.create_task(_beets_worker(context.bot, chat_id, fd["path"],
                                          fd["name"], _token_for(bid)))
        return

    # Beets: import all folders
    if action == "BA":
        bid     = parts[1]
        pending = _pending_beets.pop(bid, None)
        if not pending:
            await query.edit_message_text("Already handled or expired.")
            return
        folders = pending["folders"]
        chat_id = pending["chat_id"]
        await query.edit_message_text(
            f"Queuing beets import for all {len(folders)} folder(s)...")
        for fd in folders:
            asyncio.create_task(
                _beets_worker(context.bot, chat_id, fd["path"], fd["name"],
                              _token_for(bid)))
        return

    # Beets: cancel
    if action == "BC":
        bid = parts[1]
        _pending_beets.pop(bid, None)
        await query.edit_message_text("Cancelled.")
        return

    # Album skip
    if action == "AS":
        aid   = parts[1]
        token = _token_for(aid)
        pending_approvals.get(token, {}).pop(aid, None)
        await query.edit_message_text("Album skipped.")
        return

    # Approval yes
    if action == "AY":
        aid     = parts[1]
        token   = _token_for(aid)
        pending = pending_approvals.get(token, {}).pop(aid, None)
        if not pending:
            await query.edit_message_text("Already handled.")
            return
        if not pending.get("username") or not pending.get("file"):
            await query.edit_message_text(
                f"No source available anymore for {pending['label']}.")
            return
        if await asyncio.to_thread(
                slskd_enqueue, pending["username"], pending["file"],
                track=pending.get("track"), token=token,
                chat_id=str(query.message.chat_id),
                candidates=pending.get("candidates", [])):
            await query.edit_message_text(
                f"Queued: {pending['label']}\n{_format_info(pending['file'])}")
        else:
            await query.edit_message_text(f"Enqueue failed: {pending['label']}")
        return

    # Approval no
    if action == "AN":
        aid = parts[1]
        pending_approvals.get(_token_for(aid), {}).pop(aid, None)
        await query.edit_message_text("Skipped.")
        return

    # Retry
    if action == "RT":
        rid     = parts[1]
        token   = _token_for(rid)
        track   = pending_retries.get(token, {}).pop(rid, None)
        if not track:
            await query.edit_message_text("Already handled.")
            return
        label   = f"{track['artist']} - {track['title']}"
        user    = _user_for_token(token)
        chat_id = user["chat_id"] if user else str(query.message.chat_id)
        await query.edit_message_text(f"Retrying: {label}")
        result  = await asyncio.to_thread(slskd_search_and_pick, track)
        if result["status"] == "ok":
            if await asyncio.to_thread(
                    slskd_enqueue, result["username"], result["file"],
                    track=track, token=token, chat_id=chat_id,
                    candidates=result.get("folders", [])[1:]):
                await context.bot.send_message(chat_id=chat_id,
                    text=f"Retry queued: {label}\n{_format_info(result['file'])}")
            else:
                await context.bot.send_message(chat_id=chat_id,
                    text=f"Retry enqueue failed: {label}")
        elif result["status"] in ("live_only", "low_quality"):
            reason = ("Only live recording found" if result["status"] == "live_only"
                      else "Only low-quality format (no FLAC/Opus)")
            await _send_approval(context.bot, chat_id, token, label, reason,
                                 result["file"], result["username"], track,
                                 candidates=result.get("folders", [])[1:])
        else:
            await context.bot.send_message(chat_id=chat_id,
                text=f"Retry also failed: {label}")
        return

    # Skip
    if action == "SK":
        rid = parts[1]
        pending_retries.get(_token_for(rid), {}).pop(rid, None)
        await query.edit_message_text("Skipped.")
        return

    # Manual search: page navigation
    if action == "SPG":
        sid, page       = parts[1], int(parts[2])
        token           = _token_for(sid)
        folders, squery = _search_entry(token, sid)
        if not folders:
            await query.edit_message_text("Those results expired — run /search again.")
            return
        text, markup = _render_search_results(sid, folders, squery, page)
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except Exception:
            pass
        return

    # Manual search: download best file from folder
    if action == "DF":
        sid, idx    = parts[1], int(parts[2])
        token       = _token_for(sid)
        folders, _  = _search_entry(token, sid)
        if not folders or idx >= len(folders):
            await query.edit_message_text("Result expired.")
            return
        fd        = folders[idx]
        best_file = min(fd["files"], key=lambda f: _score_file(f, fd["upload_speed"]) or (99,))
        if await asyncio.to_thread(
                slskd_enqueue, fd["username"], best_file, token=token,
                chat_id=str(query.message.chat_id),
                candidates=[f for j, f in enumerate(folders) if j != idx][:6]):
            fname = best_file["filename"].replace("\\", "/").split("/")[-1]
            await context.bot.send_message(chat_id=str(query.message.chat_id),
                text=f"Queued: {fname}\n{_format_info(best_file)}")
        else:
            await context.bot.send_message(chat_id=str(query.message.chat_id),
                text="Enqueue failed.")
        return

    # Manual search: download full album folder
    if action == "DA":
        sid, idx       = parts[1], int(parts[2])
        token          = _token_for(sid)
        folders, squery = _search_entry(token, sid)
        if not folders or idx >= len(folders):
            await query.edit_message_text("Result expired.")
            return
        fd        = folders[idx]
        username  = fd["username"]
        chat_id_s = str(query.message.chat_id)

        await context.bot.send_message(chat_id=chat_id_s,
            text=f"Expanding directory from @{username}...")

        ref_file   = fd["files"][0] if fd["files"] else {}
        full_files = await asyncio.to_thread(
            slskd_expand_directory, username, fd, ref_file)
        if not full_files:
            full_files = fd["files"]

        folder_name = fd["folder"].replace(chr(92), "/").split("/")[-1]
        alt = [f for j, f in enumerate(folders) if j != idx][:6]
        ok, total, ag_id = await asyncio.to_thread(
            slskd_enqueue_folder,
            username, full_files, token=token, chat_id=chat_id_s,
            label=folder_name, alt_sources=alt, album=folder_name)
        msg = await context.bot.send_message(chat_id=chat_id_s,
            text=f"⬇️ Downloading {folder_name}: 0/{total} files {_progress_bar(0, total)}\n"
                 f"from @{username}")
        if ag_id and ag_id in pending_album_groups:
            pending_album_groups[ag_id]["progress_msg_id"] = msg.message_id
            pending_album_groups[ag_id]["retry_query"]     = squery  # re-search on exhaustion
        return

    # Download all for a playlist — show an estimate + mode choice first
    if action == "dlall":
        pl_uid        = parts[1]
        token         = _token_for(pl_uid)
        playlist_name = _uid_to_playlist.get(pl_uid, "")
        tracks        = missing_by_playlist.get(token, {}).get(playlist_name, [])
        if not tracks:
            await _edit_or_send(query, context,
                "No tracks found (state may have expired). Run /scan again.")
            return
        n      = len(tracks)
        # Rough estimate: MBZ grouping (~1s/track) + ~30s per album search.
        est_min = max(1, round((n * 1 + n * 0.6 * 30) / 60))
        dlid = _uid("dl")
        _uid_to_token[dlid] = token
        _pending_dlall[dlid] = {"token": token, "playlist_name": playlist_name}
        await _edit_or_send(query, context,
            f"Download all — {playlist_name} ({n} tracks)\n"
            f"Estimated time: ~{est_min} min.\n\n"
            f"• Auto: pick the best Soulseek source for each album automatically.\n"
            f"• Choose: prompt you to pick a source per album ({n} taps).\n",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚡ Auto (recommended)", callback_data=f"DLA|{dlid}")],
                [InlineKeyboardButton("👆 Choose each", callback_data=f"DLM|{dlid}")],
                [InlineKeyboardButton("Cancel", callback_data=f"DLC|{dlid}")],
            ]))
        return

    # Download-all: Auto mode (DLA) or Manual/choose mode (DLM)
    if action in ("DLA", "DLM"):
        dlid    = parts[1]
        token   = _token_for(dlid)
        pending = _pending_dlall.pop(dlid, None)
        if not pending:
            await _edit_or_send(query, context, "Already handled or expired.")
            return
        playlist_name = pending["playlist_name"]
        tracks  = missing_by_playlist.get(token, {}).get(playlist_name, [])
        if not tracks:
            await _edit_or_send(query, context, "No tracks found. Run /scan again.")
            return
        user    = _user_for_token(token)
        chat_id = user["chat_id"] if user else str(query.message.chat_id)
        nd_user, nd_pass = _user_nd(token)
        auto    = (action == "DLA")
        await _edit_or_send(query, context,
            f"Starting {'auto' if auto else 'manual'} download for "
            f"{len(tracks)} tracks from {playlist_name}...\n"
            f"Querying MusicBrainz and searching slskd — progress will follow.")
        asyncio.create_task(_dlall_worker(
            context.bot, chat_id, token, playlist_name, tracks,
            nd_user, nd_pass, auto=auto))
        return

    if action == "DLC":
        _pending_dlall.pop(parts[1], None)
        await _edit_or_send(query, context, "Cancelled.")
        return

    # /checkalbums: download missing tracks for one flagged album
    if action == "CD":
        cid     = parts[1]
        token   = _token_for(cid)
        pending = _pending_checkdl.pop(cid, None)
        if not pending:
            await query.edit_message_text("Already handled or expired.")
            return
        chat_id = pending["chat_id"]
        tracks  = pending["tracks"]
        nd_user, nd_pass = _user_nd(token)
        await query.edit_message_text(
            f"Downloading {len(tracks)} missing track(s) from "
            f"{pending['artist']} - {pending['album']} (auto)...")
        asyncio.create_task(_dlall_worker(
            context.bot, chat_id, token,
            f"{pending['artist']} - {pending['album']}", tracks,
            nd_user, nd_pass, auto=True))
        return

    # /album: user picked which album they meant (ambiguous resolution)
    if action == "ALG":
        rid, idx = parts[1], int(parts[2])
        token    = _token_for(rid)
        pending  = _pending_album.pop(rid, None)
        if not pending or "candidates" not in pending:
            await query.edit_message_text("Already handled or expired.")
            return
        cands = pending["candidates"]
        if idx >= len(cands):
            await query.edit_message_text("Result expired.")
            return
        chat_id = pending["chat_id"]
        c       = cands[idx]
        await query.edit_message_text(
            f"📀 {c['title']} — {c['artist']}. Resolving tracklist…")
        resolved = await asyncio.to_thread(mbz_resolve_album, c["rgid"])
        if not resolved.get("release_mbid"):
            await query.edit_message_text(
                "Couldn't resolve a release for that album. Try a more specific query.")
            return
        await _send_album_card(context.bot, chat_id, token, resolved)
        return

    # /album: download the confirmed album (auto best source, or choose source)
    if action in ("ALD", "ALM"):
        aid     = parts[1]
        token   = _token_for(aid)
        pending = _pending_album.pop(aid, None)
        if not pending or "release_mbid" not in pending:
            await query.edit_message_text("Already handled or expired.")
            return
        chat_id = pending["chat_id"]
        manual  = (action == "ALM")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        asyncio.create_task(_album_download_worker(
            context.bot, chat_id, token, pending["artist"], pending["title"],
            pending["release_mbid"], pending.get("total_tracks", 0), manual=manual))
        return

    if action == "ALC":
        _pending_album.pop(parts[1], None)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    # /album: retry after every source failed to queue (fresh Soulseek search)
    if action == "ARETRY":
        rid     = parts[1]
        token   = _token_for(rid)
        pending = _pending_album.pop(rid, None)
        if not pending:
            await query.edit_message_text("Already handled or expired.")
            return
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        if pending.get("query"):
            # Manual /search origin — re-run the exact query with fresh sources.
            await _do_search_and_render(
                context.bot, pending["chat_id"], token, pending["query"])
        else:
            asyncio.create_task(_album_download_worker(
                context.bot, pending["chat_id"], token,
                pending.get("artist", ""), pending.get("title", ""),
                pending.get("release_mbid", ""), pending.get("total_tracks", 0),
                manual=False))
        return

    # ----- Step 3: post-import Adjust / review actions -----
    if action == "ADJ":
        recid = parts[1]
        rec   = _albums.get(recid)
        if not rec:
            await query.edit_message_text("That album record has expired.")
            return
        try:
            await query.edit_message_reply_markup(reply_markup=_adjust_menu_markup(recid))
        except Exception:
            await context.bot.send_message(
                chat_id=rec["chat_id"], text=f"Adjust: {rec['label']}",
                reply_markup=_adjust_menu_markup(recid))
        return

    if action in ("RIMP", "IMPAS"):
        recid = parts[1]
        rec   = _albums.get(recid)
        if not rec:
            await query.edit_message_text("That album record has expired.")
            return
        autotag = (action == "RIMP")
        await query.edit_message_text(
            f"{'Re-importing (pinned)' if autotag else 'Importing as-is'}: {rec['label']}…")
        asyncio.create_task(_reimport_worker(
            context.bot, recid, autotag=autotag, mbid=rec.get("release_mbid", "")))
        return

    if action == "PREL":
        recid = parts[1]
        rec   = _albums.get(recid)
        if not rec:
            await query.edit_message_text("That album record has expired.")
            return
        await query.edit_message_text(f"Searching MusicBrainz for releases of {rec['label']}…")
        cands = await asyncio.to_thread(
            mbz_search_release_groups, f"{rec['artist']} {rec['album']}", 15)
        if not cands:
            await context.bot.send_message(chat_id=rec["chat_id"],
                text="No MusicBrainz releases found for that album.",
                reply_markup=_adjust_menu_markup(recid))
            return
        pickid = _uid("pick")
        _uid_to_token[pickid] = rec["token"]
        _pending_pick[pickid] = {"recid": recid, "kind": "release",
                                 "candidates": cands, "label": rec["label"]}
        text, markup = _render_release_picker(pickid, cands, rec["label"], 0)
        await context.bot.send_message(chat_id=rec["chat_id"], text=text,
                                       reply_markup=markup)
        return

    if action == "RPGN":   # release picker: page nav
        pickid, page = parts[1], int(parts[2])
        pend = _pending_pick.get(pickid)
        if not pend or pend.get("kind") != "release":
            await query.edit_message_text("That picker expired — open Adjust again.")
            return
        text, markup = _render_release_picker(
            pickid, pend["candidates"], pend.get("label", ""), page)
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except Exception:
            pass
        return

    if action == "RPKC":   # release picker: cancel -> back to Adjust menu
        pickid = parts[1]
        pend   = _pending_pick.pop(pickid, None)
        recid  = pend.get("recid") if pend else None
        if recid and recid in _albums:
            try:
                await query.edit_message_text(
                    f"Adjust: {_albums[recid]['label']}",
                    reply_markup=_adjust_menu_markup(recid))
            except Exception:
                pass
        else:
            try:
                await query.edit_message_text("Cancelled.")
            except Exception:
                pass
        return

    if action == "RPK":
        pickid, idx = parts[1], int(parts[2])
        pend = _pending_pick.pop(pickid, None)
        if not pend or pend.get("kind") != "release":
            await query.edit_message_text("Already handled or expired.")
            return
        rec = _albums.get(pend["recid"])
        if not rec:
            await query.edit_message_text("That album record has expired.")
            return
        cands = pend["candidates"]
        if idx >= len(cands):
            await query.edit_message_text("Result expired.")
            return
        await query.edit_message_text(
            f"Resolving {cands[idx]['title']} and re-importing…")
        resolved = await asyncio.to_thread(mbz_resolve_album, cands[idx]["rgid"])
        rec["release_mbid"] = resolved.get("release_mbid", "")
        rec["rgid"]         = cands[idx]["rgid"]
        asyncio.create_task(_reimport_worker(
            context.bot, pend["recid"], autotag=True,
            mbid=rec["release_mbid"]))
        return

    if action == "ART":
        recid = parts[1]
        rec   = _albums.get(recid)
        if not rec:
            await query.edit_message_text("That album record has expired.")
            return
        rgid = rec.get("rgid") or await asyncio.to_thread(
            rgid_from_release, rec.get("release_mbid", ""))
        rec["rgid"] = rgid
        images = await asyncio.to_thread(caa_list_front_images, rgid, 4)
        if not images:
            await context.bot.send_message(chat_id=rec["chat_id"],
                text="No cover candidates found on the Cover Art Archive for this album.")
            return
        pickid = _uid("pick")
        _uid_to_token[pickid] = rec["token"]
        _pending_pick[pickid] = {"recid": recid, "kind": "art", "candidates": images}
        # Send each candidate as a photo with a Use button.
        for i, img in enumerate(images):
            try:
                await context.bot.send_photo(
                    chat_id=rec["chat_id"], photo=img["thumb"],
                    caption=f"Cover option {i+1}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                        f"Use cover {i+1}", callback_data=f"ARTSET|{pickid}|{i}")]]))
            except Exception:
                pass
        return

    if action == "ARTSET":
        pickid, idx = parts[1], int(parts[2])
        pend = _pending_pick.pop(pickid, None)
        if not pend or pend.get("kind") != "art":
            await query.edit_message_text("Already handled or expired.")
            return
        rec = _albums.get(pend["recid"])
        if not rec:
            await query.edit_message_text("That album record has expired.")
            return
        imgs = pend["candidates"]
        if idx >= len(imgs):
            return
        await context.bot.send_message(chat_id=rec["chat_id"],
            text=f"Embedding the chosen cover into {rec['label']}…")
        asyncio.create_task(_set_cover_worker(
            context.bot, pend["recid"], imgs[idx]["full"]))
        return

    if action == "ADDL":
        recid = parts[1]
        rec   = _albums.get(recid)
        if not rec:
            await query.edit_message_text("That album record has expired.")
            return
        await query.edit_message_text(f"Re-downloading {rec['label']} (choose source)…")
        asyncio.create_task(_album_download_worker(
            context.bot, rec["chat_id"], rec["token"], rec["artist"], rec["album"],
            rec.get("release_mbid", ""), 0, manual=True))
        return

    # Unrecognized prefix (stale button from an older version, corrupted data…)
    # — tell the user instead of silently doing nothing.
    print(f"  unhandled callback action: {query.data!r}")
    try:
        await query.edit_message_text(
            "This button has expired or is no longer supported — "
            "run the command again.")
    except Exception:
        pass

async def _send_album_prompt(bot, chat_id, token, label, group, folders):
    """
    Send a Telegram message showing top album folder results with
    Download / Skip buttons — one button row per folder result.
    """
    aid = _uid("a")
    _uid_to_token[aid] = token
    top = folders[:10]

    # Store all folders so the handler can enqueue the chosen one
    pending_approvals.setdefault(token, {})[aid] = {
        "type":    "album_pick",
        "label":   label,
        "group":   group,
        "folders": folders,
    }

    lines = [f"Album results for: {label}\nPick a source or skip:"]
    buttons = []
    for i, fd in enumerate(top):
        folder_name = fd["folder"].replace("\\", "/").split("/")[-1][:60]
        file_count  = len(fd["files"])
        speed_mb    = round(fd["upload_speed"] / 1024 / 1024, 1)
        queue       = fd.get("queue_length", 0)
        free        = "⚡" if fd.get("has_free_upload_slot") else " "
        comp        = "(compilation) " if COMPILATION_RE.search(fd["folder"]) else ""
        live        = "(live) " if LIVE_RE.search(fd["folder"]) else ""
        quality = _folder_quality(fd["files"])
        lines.append(
            f"{i+1}. {free}{comp}{live}{folder_name}\n"
            f"   {quality} | {file_count} files | {speed_mb} MB/s"
            f"{f' | queue: {queue}' if queue else ''} | @{fd['username']}"
        )
        buttons.append([
            InlineKeyboardButton(f"Download #{i+1}", callback_data=f"AP|{aid}|{i}"),
        ])
    buttons.append([InlineKeyboardButton("Skip album", callback_data=f"AS|{aid}")])

    text = "\n".join(lines)
    await _tg_send(bot, chat_id, text, reply_markup=InlineKeyboardMarkup(buttons))

async def _auto_enqueue_album(bot, chat_id, token, label, folders,
                              release_mbid="", missing_tracks=None):
    """
    Pick the best folder for an album, expand it to the full tracklist, and
    enqueue everything — no user interaction. Returns (ok, total).
    Sends progress messages and seeds the album's editable progress line.
    """
    fd       = folders[0]   # already sorted best-first
    username = fd["username"]
    artist, _, album = label.partition(" - ")
    quality  = _folder_quality(fd["files"])
    speed_mb = round((fd.get("upload_speed", 0) or 0) / 1024 / 1024, 1)
    await _tg_send(bot, chat_id,
        f"📂 Best source for {label}: @{username} "
        f"({quality}, {speed_mb} MB/s). Expanding folder…")

    ref_file = fd["files"][0] if fd["files"] else {}
    full     = await asyncio.to_thread(slskd_expand_directory, username, fd, ref_file)
    if not full:
        full = fd["files"]
    folder_name = fd["folder"].replace(chr(92), "/").split("/")[-1]
    ok, total, ag_id = await asyncio.to_thread(
        slskd_enqueue_folder,
        username, full, token=token, chat_id=chat_id,
        label=folder_name, release_mbid=release_mbid,
        alt_sources=folders[1:6], artist=artist, album=album,
        missing_tracks=missing_tracks or [])
    if ok and ag_id:
        msg = await _tg_send(bot, chat_id,
            f"⬇️ Downloading {label}: 0/{total} files {_progress_bar(0, total)}")
        if msg and ag_id in pending_album_groups:
            pending_album_groups[ag_id]["progress_msg_id"] = msg.message_id
    return ok, total

async def _send_album_card(bot, chat_id, token, resolved: dict):
    """Send the album confirmation card (cover + details + action buttons)."""
    aid = _uid("ald")
    _uid_to_token[aid]  = token
    _pending_album[aid] = {"token": token, "chat_id": chat_id, **resolved}

    yr = f" · {resolved['year']}" if resolved.get("year") else ""
    tt = (f"{resolved['total_tracks']} tracks" if resolved.get("total_tracks")
          else "tracklist unknown")
    caption = (f"🎵 {resolved['title']}\n{resolved['artist']}{yr} · {tt}\n\n"
               f"Download this album?")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ Download (best source)", callback_data=f"ALD|{aid}")],
        [InlineKeyboardButton("👆 Choose source",          callback_data=f"ALM|{aid}")],
        [InlineKeyboardButton("Cancel",                    callback_data=f"ALC|{aid}")],
    ])
    cover = caa_front_url(resolved.get("rgid", ""))
    try:
        await bot.send_photo(chat_id=chat_id, photo=cover,
                             caption=caption, reply_markup=kb)
    except Exception:
        # No cover art available / fetch failed — fall back to a text card
        await bot.send_message(chat_id=chat_id, text=caption, reply_markup=kb)

async def _reimport_worker(bot, recid: str, autotag: bool, mbid: str = ""):
    """Re-place a recorded album dir into the library using deterministic placement."""
    rec = _albums.get(recid)
    if not rec:
        return
    album_dir = rec.get("album_dir", "")
    if not album_dir:
        await _tg_send(bot, rec["chat_id"], "No download folder recorded for that album.")
        return
    # "Place as-is" used to mean "no release pinned", which placed untagged
    # files under Unknown Artist/Unknown Album. Identify the folder instead so
    # as-is means "don't make me pick", not "don't tag".
    release_mbid = mbid if autotag else ""
    artist, album = rec.get("artist", ""), rec.get("album", "")
    if not release_mbid:
        ident = await asyncio.to_thread(_identity_for_path, album_dir)
        release_mbid = ident.get("release_mbid", "")
        artist = artist or ident.get("artist", "")
        album = album or ident.get("album", "")
    result = await asyncio.to_thread(
        _deterministic_album_import, album_dir, release_mbid, artist, album)
    rec["import_state"] = "imported" if result["ok"] else "downloaded_not_imported"
    if result["ok"]:
        rec["status"] = "imported"
        _imported_folders.add(album_dir)
        dest = os.path.basename(result["dest_folder"])
        msg = f"📚 Placed {result['moved']} file(s) into {dest}."
        if await asyncio.to_thread(_nd_scan_after_import, rec["token"]):
            msg += "\n🔄 Navidrome quick scan triggered."
    else:
        rec["status"] = "failed"
        msg = f"⚠️ Placement failed: {result['error']}"
    await _tg_send(bot, rec["chat_id"], f"{rec['label']}\n{msg}",
                   reply_markup=_album_action_markup(recid, rec["status"]))
    await asyncio.to_thread(_save_state)

async def _set_cover_worker(bot, recid: str, image_url: str):
    """Download the chosen cover into the album dir and embed it via beets."""
    rec = _albums.get(recid)
    if not rec:
        return
    album_dir = rec.get("album_dir", "")
    try:
        r = await asyncio.to_thread(lambda: _http.get(image_url, timeout=20))
        if not r.ok:
            await _tg_send(bot, rec["chat_id"], "Couldn't download that cover image.")
            return
        cover_path = os.path.join(album_dir, "cover.jpg")
        await asyncio.to_thread(lambda: open(cover_path, "wb").write(r.content))
    except Exception as e:
        await _tg_send(bot, rec["chat_id"], f"Couldn't save the cover: {e}")
        return
    await _tg_send(bot, rec["chat_id"],
        f"🖼 Saved cover.jpg for {rec['label']}. Navidrome will pick it up on next scan.")
    await asyncio.to_thread(_save_state)

async def _album_download_worker(bot, chat_id, token, artist, album,
                                 release_mbid, total_tracks, manual=False):
    """
    Search Soulseek for one album and either auto-enqueue the best source or
    send the source picker. Reuses the step-1 album pipeline, so completion
    triggers a single MBID-pinned, --flat beets import.
    """
    label = f"{artist} - {album}"
    try:
        await _tg_send(bot, chat_id, f"🔍 Searching Soulseek for {label}…")
        folders = await asyncio.to_thread(
            slskd_search_album_folders, artist, album, total_tracks)
        if not folders:
            await _tg_send(bot, chat_id,
                f"❌ Couldn't find {label} on Soulseek. "
                f"Try /search for individual files.")
            return
        if manual:
            await _tg_send(bot, chat_id, f"📂 Found {len(folders)} source(s) for {label}.")
            group = {"release_mbid": release_mbid, "artist": artist,
                     "album": album, "total_tracks": total_tracks,
                     "missing_tracks": []}
            await _send_album_prompt(bot, chat_id, token, label, group, folders)
        else:
            ok, total = await _auto_enqueue_album(
                bot, chat_id, token, label, folders, release_mbid=release_mbid)
            if not ok:
                await _tg_send(bot, chat_id,
                    f"⚠️ Couldn't queue {label}. Tap \u201cChoose source\u201d next time, "
                    f"or use /search.")
    except Exception as e:
        import traceback
        traceback.print_exc()
        await _tg_send(bot, chat_id, f"Album download error: {e}")

async def _dlall_worker(bot, chat_id, token, playlist_name, tracks,
                        nd_user, nd_pass, auto: bool = False):
    """
    Background task: album grouping + searches without blocking the event loop.

    auto=True  → pick the best source per album automatically (no per-album taps);
                 only low-confidence individual tracks still prompt.
    auto=False → send a per-album source picker as before.
    """
    try:
        # Live progress while MusicBrainz grouping runs (one editable message).
        status_msg = await _tg_send(bot, chat_id,
            f"Grouping {len(tracks)} tracks by album… {_progress_bar(0, len(tracks))}")
        loop      = asyncio.get_running_loop()
        last_edit = [0.0]

        async def _edit_progress(done, total):
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=status_msg.message_id,
                    text=f"Grouping {total} tracks by album… "
                         f"{_progress_bar(done, total)} {done}/{total}")
            except Exception:
                pass  # message unchanged / too-frequent edit — ignore

        def _on_progress(done, total):
            # Called from the worker thread. Throttle to ~1 edit / 3s.
            now = time.time()
            if status_msg is None:
                return
            if now - last_edit[0] < 3 and done < total:
                return
            last_edit[0] = now
            asyncio.run_coroutine_threadsafe(_edit_progress(done, total), loop)

        album_groups, solo_tracks = await asyncio.to_thread(
            group_missing_by_album, tracks, nd_user, nd_pass, _on_progress)

        await _tg_send(bot, chat_id,
            f"Found {len(album_groups)} album group(s), "
            f"{len(solo_tracks)} individual track(s). "
            f"{'Auto-downloading' if auto else 'Searching'} — updates will follow.")

        queued       = []
        auto_albums  = 0
        n_albums     = len(album_groups)
        # Album-first downloads
        for idx, group in enumerate(album_groups, 1):
            await asyncio.sleep(1)  # let slskd release its one-at-a-time lock
            artist  = group["artist"]
            album   = group["album"]
            label   = f"{artist} - {album}"
            await _tg_send(bot, chat_id,
                f"🔍 [{idx}/{n_albums}] Searching Soulseek for {label}…")
            folders = await asyncio.to_thread(
                slskd_search_album_folders, artist, album, group["total_tracks"])
            if not folders:
                await _tg_send(bot, chat_id,
                    f"❌ Album not found on Soulseek: {label}. "
                    f"Falling back to individual tracks.")
                solo_tracks.extend(group["missing_tracks"])
                continue
            if auto:
                ok, total = await _auto_enqueue_album(
                    bot, chat_id, token, label, folders,
                    release_mbid=group.get("release_mbid", ""),
                    missing_tracks=group.get("missing_tracks", []))
                if ok:
                    auto_albums += 1
                    queued.append(f"{label} (album: {ok}/{total} files)")
                else:
                    await _tg_send(bot, chat_id,
                        f"⚠️ Could not queue album: {label}. "
                        f"Falling back to individual tracks.")
                    solo_tracks.extend(group["missing_tracks"])
            else:
                await _tg_send(bot, chat_id,
                    f"📂 Found {len(folders)} source(s) for {label}.")
                await _send_album_prompt(bot, chat_id, token, label, group, folders)

        # Individual track downloads
        for track in solo_tracks:
            await asyncio.sleep(1)
            label      = f"{track['artist']} - {track['title']}"
            nd_u, nd_p = _user_nd(token)
            dup        = await asyncio.to_thread(nd_find_duplicate, track, nd_u, nd_p)
            result     = await asyncio.to_thread(slskd_search_and_pick, track)
            status     = result["status"]

            if status == "ok":
                # In auto mode, skip the duplicate prompt only when it's the
                # SAME recording; a genuinely different version still asks.
                if dup:
                    await _send_dup_prompt(bot, chat_id, token, label, track,
                                           dup, result.get("folders", []))
                elif await asyncio.to_thread(
                        slskd_enqueue, result["username"], result["file"],
                        track=track, token=token, chat_id=chat_id,
                        candidates=result.get("folders", [])[1:]):
                    queued.append(f"{label} [{_format_info(result['file'])}]")
                else:
                    await _send_retry(bot, chat_id, token, label, track,
                                      "Enqueue failed after successful search")
            elif status in ("live_only", "low_quality"):
                reason = ("Only live recording found" if status == "live_only"
                          else "Only low-quality format (no FLAC/Opus)")
                await _send_approval(bot, chat_id, token, label, reason,
                                     result["file"], result["username"], track,
                                     candidates=result.get("folders", [])[1:])
            elif status == "not_found":
                await _send_retry(bot, chat_id, token, label, track,
                                  "No FLAC/Opus found on Soulseek — check log")
            else:
                await _send_retry(bot, chat_id, token, label, track, "Search error")

        if auto:
            summary = (f"Done. {auto_albums} album(s) auto-queued, "
                       f"{len(queued) - auto_albums} individual track(s) queued. "
                       f"Use /status to watch progress.")
        else:
            summary = (f"Done searching. {len(queued)} track(s) auto-queued. "
                       f"Album prompts sent above.")
        lines = [summary] + [f"- {q}" for q in queued]
        text  = "\n".join(lines)
        for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
            await _tg_send(bot, chat_id, chunk)
        await asyncio.to_thread(_save_state)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"  _dlall_worker exception: {e}")
        await _tg_send(bot, chat_id,
            f"Download worker error: {e}\nCheck the log for details.")

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def album_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Download a whole album by name: resolve on MusicBrainz, then confirm."""
    token   = _resolve_token(context)
    chat_id = str(update.effective_chat.id)
    query   = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text(
            "Usage: /album Artist - Album   (or just the album name)")
        return

    await update.message.reply_text(f"🔎 Looking up \u201c{query}\u201d on MusicBrainz…")
    mb_query   = query.replace(" - ", " ")
    candidates = await asyncio.to_thread(mbz_search_release_groups, mb_query, 5)
    # Prefer proper albums, then by MB relevance score
    candidates.sort(key=lambda c: (0 if c["primary_type"] == "album" else 1, -c["score"]))
    if not candidates:
        await update.message.reply_text(
            "Couldn't find that album on MusicBrainz. "
            "Try '/album Artist - Album' with the exact name.")
        return

    top       = candidates[0]
    confident = (top["score"] >= 88 and
                 (len(candidates) == 1 or top["score"] - candidates[1]["score"] >= 12))

    if confident:
        await update.message.reply_text(
            f"📀 Found: {top['title']} — {top['artist']}. Resolving tracklist…")
        resolved = await asyncio.to_thread(mbz_resolve_album, top["rgid"])
        if not resolved.get("release_mbid"):
            await update.message.reply_text(
                "Found the album but couldn't resolve a release. "
                "Try a more specific query.")
            return
        if ALBUM_AUTO_DOWNLOAD:
            asyncio.create_task(_album_download_worker(
                context.bot, chat_id, token, resolved["artist"], resolved["title"],
                resolved["release_mbid"], resolved["total_tracks"], manual=False))
        else:
            await _send_album_card(context.bot, chat_id, token, resolved)
        await asyncio.to_thread(_save_state)
        return

    # Ambiguous → small picker (the one place a tap earns its keep)
    rid = _uid("alg")
    _uid_to_token[rid]  = token
    _pending_album[rid] = {"token": token, "chat_id": chat_id,
                           "query": query, "candidates": candidates[:4]}
    lines   = [f"Which album did you mean? (\u201c{query}\u201d)"]
    buttons = []
    for i, c in enumerate(candidates[:4]):
        yr  = f" ({c['year']})" if c["year"] else ""
        typ = f" · {c['primary_type']}" if c["primary_type"] else ""
        lines.append(f"{i+1}. {c['title']} — {c['artist']}{yr}{typ}")
        buttons.append([InlineKeyboardButton(
            f"{i+1}. {c['title'][:32]}", callback_data=f"ALG|{rid}|{i}")])
    buttons.append([InlineKeyboardButton("Cancel", callback_data=f"ALC|{rid}")])
    await update.message.reply_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))
    await asyncio.to_thread(_save_state)

def _search_entry(token, sid):
    """Return (folders, query) for a stored manual search; handles legacy list form."""
    e = manual_search_results.get(token, {}).get(sid)
    if e is None:
        return None, ""
    if isinstance(e, dict):
        return e.get("folders", []), e.get("query", "")
    return e, ""   # legacy: bare list

def _render_search_results(sid, folders, query, page, per=10):
    """Build (text, InlineKeyboardMarkup) for one page of search results."""
    import math
    pages = max(1, math.ceil(len(folders) / per))
    page  = max(0, min(page, pages - 1))
    start = page * per
    chunk = folders[start:start + per]
    lines = [f"Results for: {query}  (page {page+1}/{pages}, {len(folders)} total)\n"]
    buttons = []
    for i, fd in enumerate(chunk):
        gidx        = start + i
        quality     = _folder_quality(fd["files"])
        speed_mb    = round((fd.get("upload_speed", 0) or 0) / 1024 / 1024, 1)
        file_count  = len(fd["files"])
        queue       = fd.get("queue_length", 0)
        free_slot   = "⚡" if fd.get("has_free_upload_slot") else " "
        folder_name = fd["folder"].replace("\\", "/").split("/")[-1][:60]
        comp_flag   = "(compilation) " if COMPILATION_RE.search(fd["folder"]) else ""
        live_flag   = "(live) " if LIVE_RE.search(fd["folder"]) else ""
        lines.append(
            f"{gidx+1}. {free_slot}{comp_flag}{live_flag}{folder_name}\n"
            f"   {quality} | {file_count} files | {speed_mb} MB/s"
            f"{f' | queue: {queue}' if queue else ''} | @{fd['username']}")
        buttons.append([
            InlineKeyboardButton(f"Best file #{gidx+1}",  callback_data=f"DF|{sid}|{gidx}"),
            InlineKeyboardButton(f"Full album #{gidx+1}", callback_data=f"DA|{sid}|{gidx}"),
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"SPG|{sid}|{page-1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"SPG|{sid}|{page+1}"))
    if nav:
        buttons.append(nav)
    return "\n".join(lines), InlineKeyboardMarkup(buttons)

async def _do_search_and_render(bot, chat_id, token, args):
    """Run a Soulseek search and send page 1 of results with pagination."""
    folders = await asyncio.to_thread(slskd_run_search, args, 0)
    if not folders:
        await _tg_send(bot, chat_id, f"No FLAC/Opus results found for: {args}")
        return
    sid = _uid("s")
    _uid_to_token[sid] = token
    manual_search_results.setdefault(token, {})[sid] = {"folders": folders, "query": args}
    text, markup = _render_search_results(sid, folders, args, 0)
    await _tg_send(bot, chat_id, text, reply_markup=markup)

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token   = _resolve_token(context)
    chat_id = str(update.effective_chat.id)
    args    = " ".join(context.args).strip()
    if not args:
        await update.message.reply_text("Usage: /search Artist - Title")
        return
    await update.message.reply_text(
        f"Searching slskd for: {args}\n(may take up to {SEARCH_TIMEOUT}s...)")
    await _do_search_and_render(context.bot, chat_id, token, args)

def _list_download_folders() -> list:
    """
    Return immediate subdirectories of SLSKD_DOWNLOAD_DIR that contain
    at least one audio file anywhere inside them, sorted alphabetically.
    Each entry: {name, path, file_count, formats}
    """
    import os
    result = []
    try:
        base = SLSKD_DOWNLOAD_DIR
        for entry in sorted(os.scandir(base), key=lambda e: e.name.lower()):
            if not entry.is_dir():
                continue
            audio_files = []
            for root, _, files in os.walk(entry.path):
                for f in files:
                    if _file_ext(f) in _placeable_formats():
                        audio_files.append(f)
            if not audio_files:
                continue
            exts = [_file_ext(f) for f in audio_files]
            fmt  = max(set(exts), key=exts.count).upper() if exts else "?"
            result.append({
                "name":       entry.name,
                "path":       entry.path,
                "file_count": len(audio_files),
                "formats":    fmt,
            })
    except PermissionError as e:
        print(f"  _list_download_folders permission error: {e}")
    except Exception as e:
        print(f"  _list_download_folders error: {e}")
    return result

def _tokenize_for_match(text: str) -> set:
    return {t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(t) > 1}

def _suggest_review_group_for_folder(folder: dict, review_groups: list) -> dict | None:
    """
    Best-guess review-group match for a downloaded folder, for the Import tab's
    one-click suggestion. Compares embedded tags first (more reliable than the
    folder name when present), then falls back to the folder name. No fuzzy-match
    library -- plain token-overlap scoring against artist+album, same spirit as
    the _match_key normalization used elsewhere in this file.
    """
    folder_tokens = _tokenize_for_match(folder.get("name", ""))
    tag_tokens: set = set()
    try:
        files = _audio_files_in_folder(folder.get("path", ""), 1)
        if files:
            tags = _audio_file_tags(files[0]["path"])
            tag_tokens = _tokenize_for_match(
                f"{tags.get('albumartist', '') or tags.get('artist', '')} {tags.get('album', '')}")
    except Exception:
        pass

    best = None
    best_score = 0.0
    for g in review_groups:
        if g.get("hidden") or not (g.get("missing_tracks") and (g.get("canonical_mbid") or g.get("release_mbid"))):
            continue
        group_tokens = _tokenize_for_match(f"{g.get('artist', '')} {g.get('album', '')}")
        if not group_tokens:
            continue
        for source_tokens, weight in ((tag_tokens, 1.2), (folder_tokens, 1.0)):
            if not source_tokens:
                continue
            overlap = len(source_tokens & group_tokens)
            if overlap < max(1, len(group_tokens) - 1):
                continue
            score = weight * overlap / len(group_tokens)
            if score > best_score:
                best_score = score
                best = g
    if best and best_score >= 0.6:
        return {
            "group_id": best.get("id", ""),
            "artist": best.get("artist", ""),
            "album": best.get("album", ""),
            "missing": len(best.get("missing_tracks") or []),
        }
    return None

def _selected_paths_under_album(album_dir: str, relpaths: list) -> list:
    if not album_dir or not os.path.isdir(album_dir):
        raise ValueError("Download folder is not available")
    base = os.path.abspath(album_dir or "")
    selected = []
    for rel in relpaths or []:
        raw = str(rel or "")
        candidate = raw if os.path.isabs(raw) else os.path.join(base, raw)
        candidate = os.path.abspath(candidate)
        if not _path_inside(candidate, base):
            raise ValueError(f"Selected file is outside the download folder: {raw}")
        if _file_ext(candidate) not in _placeable_formats():
            raise ValueError(f"Selected file is not an accepted audio file: {raw}")
        if not os.path.isfile(candidate):
            raise ValueError(f"Selected file does not exist: {raw}")
        selected.append(candidate)
    return selected

def _album_status_from_import_result(result: dict, manual_match: bool = False) -> str:
    if result.get("imported"):
        return "imported"
    if manual_match:
        return "needs_match"
    if result.get("permission_errors"):
        return "failed"
    return "needs_review"

def _record_import_recovery(token: str, chat_id, label: str, album_dir: str,
                            artist: str = "", album: str = "",
                            release_mbid: str = "", status: str = "needs_review",
                            review_group_id: str = "", match_mode: str = "auto",
                            import_result: dict = None) -> str:
    import_result = import_result or {}
    recid = _uid("rec")
    _uid_to_token[recid] = token
    _albums[recid] = {
        "token": token,
        "chat_id": chat_id,
        "label": label,
        "artist": artist,
        "album": album,
        "release_mbid": release_mbid,
        "rgid": "",
        "album_dir": album_dir,
        "status": status,
        "import_state": "imported" if status == "imported" else "downloaded_not_imported",
        "raw_tail": import_result.get("raw_tail", ""),
        "recommended_action": import_result.get("recommended_action", ""),
        "still_in_downloads": bool(import_result.get("still_in_downloads")),
        "ts": time.time(),
        "review_group_id": review_group_id,
        "match_mode": match_mode,
    }
    if review_group_id:
        with _review_lock:
            group = _find_review_group(review_group_id)
            if group is not None:
                items = group.setdefault("match_items", [])
                if not any(item.get("id") == recid for item in items):
                    items.append({
                        "id": recid,
                        "label": label,
                        "album_dir": album_dir,
                        "release_mbid": release_mbid,
                        "status": status,
                        "import_state": _albums[recid]["import_state"],
                        "raw_tail": import_result.get("raw_tail", ""),
                        "recommended_action": import_result.get("recommended_action", ""),
                        "created_at": time.time(),
                    })
                group["updated_at"] = time.time()
    if len(_albums) > 500:
        _albums.pop(next(iter(_albums)), None)
    return recid

def _import_result_message(result: dict) -> str:
    if result.get("permission_errors"):
        return ("Permission denied while beets tried to move files. Check write "
                "access to /music and /downloads.")
    if result.get("skipped"):
        extra = f"\nPreview:\n{result.get('preview_tail')}" if result.get("preview_tail") else ""
        return ("beets skipped the import. The folder is downloaded but not in "
                "the library yet. Retry forced import, import as-is, or pick "
                f"another release.{extra}")
    if result.get("still_in_downloads"):
        return ("beets returned without moving the files out of /downloads. "
                "A stale beets DB entry may need removal before retrying.")
    if result.get("imported"):
        dest = result.get("dest_paths", [""])
        return "Imported to your library" + (f":\n{dest[0]}" if dest and dest[0] else ".")
    return f"beets import failed: {result.get('raw_tail', '')[:500]}"

async def beets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /beets
    Lists all folders in the downloads directory containing audio files.
    Presents each as a button to import with beets, plus an "Import all" option.
    """
    chat_id = str(update.effective_chat.id)
    token   = _resolve_token(context)

    await update.message.reply_text("Scanning downloads folder...")

    folders = await asyncio.to_thread(_list_download_folders)

    if not folders:
        if not os.path.isdir(SLSKD_DOWNLOAD_DIR):
            await update.message.reply_text(
                f"⚠️ The downloads folder isn't visible to the bot:\n"
                f"{SLSKD_DOWNLOAD_DIR}\n\n"
                f"This path doesn't exist inside the bot container, so beets "
                f"can't see anything and completed downloads can't be imported. "
                f"Mount slskd's downloads directory into this container at that "
                f"path (the bot and slskd must share the same volume). Run /diag "
                f"to check.")
        else:
            await update.message.reply_text(
                f"No audio folders found in:\n{SLSKD_DOWNLOAD_DIR}")
        return

    # Store for callback handler
    bid = _uid("b")
    _uid_to_token[bid] = token
    _pending_beets[bid] = {"chat_id": chat_id, "folders": folders}

    # Telegram allows at most 100 buttons per message — cap the per-folder
    # buttons and let "Import ALL" cover the rest.
    MAX_FOLDER_BTNS = 96
    shown = folders[:MAX_FOLDER_BTNS]

    lines   = [f"Found {len(folders)} folder(s) in downloads. Pick one to import:"]
    buttons = []
    for i, fd in enumerate(shown):
        done = "  ✓ imported" if fd["path"] in _imported_folders else ""
        lines.append(f"{i+1}. {fd['name']}  ({fd['file_count']} {fd['formats']} files){done}")
        label = ("Re-import" if fd["path"] in _imported_folders else "Import")
        buttons.append([InlineKeyboardButton(
            f"{label} #{i+1}: {fd['name'][:28]}",
            callback_data=f"BI|{bid}|{i}")])
    if len(folders) > MAX_FOLDER_BTNS:
        lines.append(f"…and {len(folders) - MAX_FOLDER_BTNS} more (buttons capped — "
                     f"use Import ALL, or re-run /beets after importing some)")

    buttons.append([InlineKeyboardButton(
        f"Import ALL ({len(folders)} folders)", callback_data=f"BA|{bid}")])
    buttons.append([InlineKeyboardButton(
        "Cancel", callback_data=f"BC|{bid}")])

    text = "\n".join(lines)
    for chunk_i, chunk in enumerate(
            [text[i:i+3800] for i in range(0, len(text), 3800)]):
        is_last = (chunk_i == (len(text) - 1) // 3800)
        await update.message.reply_text(
            chunk,
            reply_markup=InlineKeyboardMarkup(buttons) if is_last else None)

def _register_review_album(token, chat_id, folder_path, folder_name, status):
    """Register a manually-imported folder so it gets Adjust/Import-as-is buttons."""
    # Best-effort split "Artist - Album" if the folder name has it; else album only.
    artist, _, album = folder_name.partition(" - ")
    if not album:
        artist, album = "", folder_name
    recid = _uid("rec")
    _uid_to_token[recid] = token
    _albums[recid] = {
        "token": token, "chat_id": chat_id, "label": folder_name,
        "artist": artist.strip(), "album": album.strip(),
        "release_mbid": "", "rgid": "", "album_dir": folder_path,
        "status": status, "ts": time.time(),
    }
    if len(_albums) > 500:
        _albums.pop(next(iter(_albums)), None)
    return recid

async def _beets_worker(bot, chat_id: str, target: str, label: str = "", token: str = ""):
    """Place a downloaded album folder into the music library (deterministic)."""
    try:
        display = label or target
        await _tg_send(bot, chat_id,
            f"📚 Placing {display} into your library…")
        artist, _, album_name = (label or os.path.basename(target)).partition(" - ")
        if not album_name:
            artist, album_name = "", artist
        # Placement needs a MusicBrainz release to tag against, and this flow
        # has no bot-side context to draw one from -- identify the folder from
        # its tags/name first.
        ident = await asyncio.to_thread(_identity_for_path, target)
        result = await asyncio.to_thread(
            _deterministic_album_import, target, ident.get("release_mbid", ""),
            ident.get("artist", "") or artist.strip(),
            ident.get("album", "") or album_name.strip())
        if result["ok"]:
            _imported_folders.add(target)
            dest = os.path.basename(result["dest_folder"])
            msg = f"✅ Placed {result['moved']} file(s) into {dest}."
            if token and await asyncio.to_thread(_nd_scan_after_import, token):
                msg += "\n🔄 Navidrome quick scan triggered."
        else:
            recid = _register_review_album(token, chat_id, target, display, "failed")
            msg = (f"⚠️ Placement failed for {display}: {result['error']}\n"
                   "Use the Adjust buttons to retry with a MusicBrainz release pinned.")
            await _tg_send(bot, chat_id, msg,
                           reply_markup=_album_action_markup(recid, "failed"))
            return
        await _tg_send(bot, chat_id, msg)
    except Exception as e:
        await _tg_send(bot, chat_id, f"Import worker error: {e}")

async def spplaylist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /spplaylist <spotify_url_or_id>
    Fetch a public Spotify playlist, check which tracks are missing from
    Navidrome, then offer album-first download exactly like the weekly scan.
    """
    token = _resolve_token(context)
    user  = _user_for_token(token)
    if not user:
        await update.message.reply_text("Could not identify user.")
        return
    if not SPOTIFY_CLIENT_ID:
        await update.message.reply_text(
            "Spotify not configured. Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET.")
        return

    args = " ".join(context.args).strip()
    if not args:
        await update.message.reply_text("Usage: /spplaylist <spotify_playlist_url>")
        return

    pl_id = spotify_playlist_id(args)
    await update.message.reply_text(
        f"Fetching Spotify playlist {pl_id}...\n"
        f"Looking up MusicBrainz IDs and checking Navidrome — "
        f"this may take a few minutes for large playlists.")

    nd_user = user["navidrome_user"]
    nd_pass = user["navidrome_password"]
    chat_id = str(update.effective_chat.id)

    try:
        tracks = await asyncio.to_thread(spotify_get_playlist_tracks, pl_id)
    except SpotifyError as e:
        msg = str(e)
        if "403" in msg or "premium" in msg.lower():
            await update.message.reply_text(
                "⚠️ Spotify refused the request (403). This is a Spotify-side "
                "account issue, not the bot: the Spotify app whose Client ID/Secret "
                "you configured must be owned by an account with active Premium, and "
                "the playlist must be public.\n\n"
                f"Spotify said: {msg}")
        else:
            await update.message.reply_text(f"Spotify error — {msg}")
        return
    except RuntimeError as e:
        await update.message.reply_text(f"Spotify error: {e}")
        return
    except Exception as e:
        await update.message.reply_text(f"Failed to fetch playlist: {e}")
        return

    if not tracks:
        await update.message.reply_text(
            "No tracks found. Make sure the playlist is public (added to the owner's profile).")
        return

    await update.message.reply_text(
        f"Fetched {len(tracks)} tracks. Checking Navidrome...")

    # Reuse the same scan logic as the weekly flow
    missing = await asyncio.to_thread(_check_missing, tracks, nd_user, nd_pass)

    if not missing:
        await update.message.reply_text(
            f"All {len(tracks)} tracks already in your library!")
        return

    await update.message.reply_text(
        f"{len(missing)} missing track(s). Grouping by album...")

    # Register under a synthetic playlist name so dlall can find them
    pl_name = f"Spotify:{pl_id[:8]}"
    pl_uid  = _register_playlist(token, pl_name)
    missing_by_playlist.setdefault(token, {})[pl_name] = missing

    lines = [f"Missing from Spotify playlist ({len(missing)} tracks)"]
    for t in missing:
        lines.append(f"- {t['artist']} - {t['title']}")
    text = "\n".join(lines)
    kb   = [[InlineKeyboardButton(
        f"Download all ({len(missing)})", callback_data=f"dlall|{pl_uid}")]]
    for i, chunk in enumerate([text[j:j+3900] for j in range(0, len(text), 3900)]):
        is_last = (i == (len(text)-1)//3900)
        await update.message.reply_text(
            chunk,
            reply_markup=InlineKeyboardMarkup(kb) if is_last else None)

def _check_missing(tracks: list, nd_user: str, nd_pass: str) -> list:
    """Run nd_has_track for each track, return those not in Navidrome."""
    missing = []
    seen    = set()
    for track in tracks:
        key = track["mbid"] if track["mbid"] else (
              track["artist"].lower(), track["title"].lower())
        if key in seen:
            continue
        seen.add(key)
        if not nd_has_track(track, nd_user, nd_pass):
            missing.append(track)
    return missing

async def checkalbums_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = _resolve_token(context)
    user  = _user_for_token(token)
    if not user:
        await update.message.reply_text("Could not identify user.")
        return
    nd_user, nd_pass = user["navidrome_user"], user["navidrome_password"]
    chat_id = str(update.effective_chat.id)

    status_msg = await update.message.reply_text(
        "Checking library for incomplete albums…\n"
        "Querying MusicBrainz for every album with an MBID — may take several minutes.")
    loop      = asyncio.get_running_loop()
    last_edit = [0.0]

    async def _edit(done, total):
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=status_msg.message_id,
                text=f"Checking albums against MusicBrainz… "
                     f"{_progress_bar(done, total)} {done}/{total}")
        except Exception:
            pass

    def _progress(done, total):
        now = time.time()
        if now - last_edit[0] < 3 and done < total:
            return
        last_edit[0] = now
        asyncio.run_coroutine_threadsafe(_edit(done, total), loop)

    incomplete = await asyncio.to_thread(
        find_incomplete_albums, nd_user, nd_pass, _progress)
    if not incomplete:
        await update.message.reply_text("No incomplete albums found!")
        return

    await update.message.reply_text(
        f"Found {len(incomplete)} incomplete album(s). "
        f"Tap to download the missing tracks for any of them:")

    # One message per album so each can carry its own Download button.
    # Paced + flood-aware: a large library can produce dozens of messages.
    for a in incomplete:
        cid = _uid("cd")
        _uid_to_token[cid] = token
        _pending_checkdl[cid] = {
            "token":   token, "chat_id": chat_id,
            "artist":  a["artist"], "album": a["album"],
            "tracks":  a["missing_tracks"],
        }
        text = (f"{a['artist']} - {a['album']}\n"
                f"{a['present']}/{a['total']} tracks present — {a['missing']} missing")
        await _tg_send(
            context.bot, chat_id, text,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(f"Download {a['missing']} missing",
                                     callback_data=f"CD|{cid}")]]))
        await asyncio.sleep(0.35)
    await asyncio.to_thread(_save_state)

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await help_command(update, context)

async def rescan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trigger a Navidrome library scan: /rescan (quick) or /rescan full."""
    token = _resolve_token(context)
    user  = _user_for_token(token)
    if not user:
        await update.message.reply_text("Could not identify user.")
        return
    full = bool(context.args) and context.args[0].lower() == "full"
    nd_u, nd_p = user["navidrome_user"], user["navidrome_password"]

    status = await asyncio.to_thread(nd_get_scan_status, nd_u, nd_p)
    if status.get("scanning"):
        await update.message.reply_text(
            f"Navidrome is already scanning "
            f"({status.get('count', '?')} items so far) — no need to start another.")
        return

    ok = await asyncio.to_thread(nd_start_scan, nd_u, nd_p, full)
    if not ok:
        await update.message.reply_text(
            "⚠️ Couldn't trigger the Navidrome scan — check /diag.")
        return

    # Brief poll so the reply can usually say "done" instead of "started".
    kind = "Full" if full else "Quick"
    for _ in range(6):
        await asyncio.sleep(2)
        status = await asyncio.to_thread(nd_get_scan_status, nd_u, nd_p)
        if status and not status.get("scanning"):
            await update.message.reply_text(
                f"✅ {kind} scan finished — {status.get('count', '?')} items scanned.")
            return
    await update.message.reply_text(
        f"🔄 {kind} scan running ({status.get('count', '?')} items so far) — "
        f"it'll keep going in the background.")

HELP_TEXT = (
    "🎵 lb-bot — ListenBrainz → Soulseek\n\n"
    "/scan — scan your playlists now for missing tracks\n"
    "/status — show active downloads and album progress\n"
    "/pending — albums in progress or needing review\n"
    "/diag — check what the bot can reach\n"
    "/album Artist - Album — download a whole album by name\n"
    "/search Artist - Title — manual Soulseek search\n"
    "/spplaylist <spotify_url> — import a public Spotify playlist\n"
    "/checkalbums — find incomplete albums in your library\n"
    "/beets — import downloaded folders with beets\n"
    "/rescan — trigger a Navidrome quick scan (/rescan full for a full scan)\n"
    "/help — this message\n\n"
    "Weekly scans run automatically Tuesday 09:00."
)

def _run_diagnostics(user: dict) -> list:
    """Reachability/config checks. Returns list of (ok, label, detail).

    The probes run concurrently: sequentially they cost up to ~40s of timeouts
    when a service is down, and /api/settings and /api/action-center used to pay
    that inline on every request.
    """
    def _downloads_dir():
        if not os.path.isdir(SLSKD_DOWNLOAD_DIR):
            return (False, "Downloads folder",
                    f"{SLSKD_DOWNLOAD_DIR} NOT mounted — beets/import cannot work")
        try:
            n = sum(1 for _ in os.scandir(SLSKD_DOWNLOAD_DIR))
            return (True, "Downloads folder", f"{SLSKD_DOWNLOAD_DIR} ({n} entries)")
        except Exception as e:
            return (False, "Downloads folder", f"exists but unreadable: {e}")

    def _slskd():
        try:
            r = _http.get(f"{SLSKD_URL}/api/v0/session",
                             headers=_slskd_headers(), timeout=8)
            return (r.ok, "slskd", f"{SLSKD_URL} ({r.status_code})")
        except Exception as e:
            return (False, "slskd", f"{SLSKD_URL} unreachable: {str(e)[:80]}")

    def _navidrome():
        try:
            r = _http.get(
                f"{NAVIDROME_URL}/rest/ping",
                params=_nd_auth_params(user["navidrome_user"], user["navidrome_password"]),
                timeout=8)
            return (r.ok, "Navidrome", f"{NAVIDROME_URL} ({r.status_code})")
        except Exception as e:
            return (False, "Navidrome", f"{NAVIDROME_URL} unreachable: {str(e)[:80]}")

    def _listenbrainz():
        try:
            _lbz_get(f"user/{user['listenbrainz_user']}/playlists/createdfor",
                     timeout=15, retries=1)
            return (True, "ListenBrainz", "reachable")
        except Exception as e:
            return (False, "ListenBrainz", f"unreachable: {str(e)[:80]}")

    def _library_paths():
        """Does a path Navidrome reports actually exist under LB_BOT_MUSIC_DIR?

        Placement, duplicate-file deletion and the duplicate-file list all resolve
        Navidrome's reported paths against LB_BOT_MUSIC_DIR. When the two don't
        line up nothing errors — files just silently appear not to exist, and the
        duplicate-file list empties itself. Check it explicitly.

        Sampled across several albums and reported as a ratio. The first version
        let the *first* path-carrying song decide for the whole library, so one
        stale Navidrome row — a file deleted or renamed since its last scan — read
        as "the mount is wrong" while the mount was fine. A per-file miss and a
        systematic mismatch are different problems and now say so.
        """
        label = "Library path ↔ Navidrome"
        if not os.path.isdir(MUSIC_LIBRARY_PATH):
            return (False, label, f"{MUSIC_LIBRARY_PATH} NOT mounted")
        try:
            albums = nd_get_all_albums(user["navidrome_user"],
                                       user["navidrome_password"])
            if not albums:
                return (False, label, "Navidrome returned no albums to check")
            checked = 0
            resolved = 0
            folders_ok = 0
            first_miss = ""
            # Spread the sample across the library rather than taking the first
            # few alphabetically — a second music folder or an odd mount usually
            # isn't at the front.
            step = max(1, len(albums) // 8)
            for album in albums[::step][:8]:
                songs = nd_get_album_tracks(user["navidrome_user"],
                                            user["navidrome_password"],
                                            album.get("id", ""))
                for song in songs:
                    raw = _song_relpath(song)
                    if not raw:
                        continue
                    path = _song_abs_path(song)
                    checked += 1
                    if path and os.path.exists(path):
                        resolved += 1
                    else:
                        # Does at least the *folder* resolve? If it does, this is
                        # one missing file, not a broken mount.
                        if path and os.path.isdir(os.path.dirname(path)):
                            folders_ok += 1
                        first_miss = first_miss or f"{raw!r} → {path}"
                    break   # one song per album is enough
            if not checked:
                return (False, label, "no album track carried a path to check")
            if resolved == checked:
                detail = f"{resolved}/{checked} sampled files resolved under {MUSIC_LIBRARY_PATH}"
                if _nd_path_prefix["strip"]:
                    detail += (f" (rebased: Navidrome's {_nd_path_prefix['strip']} "
                               f"= {MUSIC_LIBRARY_PATH})")
                return (True, label, detail)
            if resolved:
                return (True, label,
                        f"{resolved}/{checked} sampled files resolved; "
                        f"{checked - resolved} missing (e.g. {first_miss}) — "
                        f"likely stale Navidrome rows, not the mount")
            if folders_ok:
                return (False, label,
                        f"album folders resolve but no sampled file does "
                        f"(e.g. {first_miss}) — Navidrome's index looks stale; "
                        f"trigger a rescan")
            return (False, label,
                    f"none of {checked} sampled files resolved under "
                    f"{MUSIC_LIBRARY_PATH} (e.g. {first_miss})")
        except Exception as e:
            return (False, label, f"check failed: {str(e)[:80]}")

    def _spotify():
        try:
            _spotify_auth_header()
            return (True, "Spotify auth", "client-credentials token OK "
                    "(playlist access still needs a Premium app owner)")
        except Exception as e:
            return (False, "Spotify auth", str(e)[:80])

    probes = [_downloads_dir, _slskd, _navidrome, _listenbrainz, _library_paths]
    if SPOTIFY_CLIENT_ID:
        probes.append(_spotify)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(probes)) as pool:
        futures = [pool.submit(p) for p in probes]
        checks = []
        for probe, fut in zip(probes, futures):
            try:
                checks.append(fut.result())
            except Exception as e:
                checks.append((False, probe.__name__.strip("_"), str(e)[:80]))
    return checks

async def diag_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Report what the bot can and can't reach, to localise setup problems."""
    token = _resolve_token(context)
    user  = _user_for_token(token)
    if not user:
        await update.message.reply_text("Could not identify user.")
        return
    await update.message.reply_text("Running diagnostics…")
    checks = await asyncio.to_thread(_run_diagnostics, user)
    lines  = ["Diagnostics:"]
    for ok, label, detail in checks:
        lines.append(f"{'✅' if ok else '❌'} {label}: {detail}")
    await update.message.reply_text("\n".join(lines))

# ---------------------------------------------------------------------------
# Web dashboard
# ---------------------------------------------------------------------------

def _default_web_user() -> dict:
    return USERS[0] if USERS else {}

class _ScanCancelled(BaseException):
    """Raised inside a scan's progress callback when the user cancels it.

    Deliberately a BaseException, not an Exception. Every scan phase wraps its
    progress call in `except Exception: pass` — a progress callback must never
    be able to kill a scan — and that swallowed the cancellation too, so the
    handler in _scan_review_worker was unreachable and a cancelled scan ran to
    completion while the UI showed it as cancelled at 100%.
    """

def _scan_review_worker(fuzzy: bool, task_id: str = "", scan_all: bool = False,
                        deep: bool = False) -> None:
    global _review_state, _active_review_scan_task_id
    user = _default_web_user()
    if not user:
        return
    with _review_lock:
        _review_state["status"] = "running"
        _review_state["message"] = "Scanning all albums" if scan_all else "Scanning duplicate albums"
        _review_state["fuzzy"] = fuzzy
        # Persisted alongside fuzzy so the "match across languages" checkbox
        # survives a reload instead of silently resetting to off.
        _review_state["deep"] = deep
    _save_review_state()
    try:
        def _cancel() -> bool:
            """Read-only cancellation check, cheap enough for an inner loop."""
            return bool(task_id) and _task_cancelled(task_id)

        def _progress(done, total, artist="", album=""):
            if task_id:
                label = f"{artist} - {album}".strip(" -")
                # persist=False: a tick used to rewrite the entire review state
                # to disk, once per album. _task_update flushes on its own
                # schedule; the in-memory task is what /api/tasks serves.
                _task_update(task_id, done=done, total=total, current=label,
                             persist=False)
                # Cooperative cancel: the /api/tasks/<id>/cancel endpoint flips
                # the task status; abort at the next album boundary.
                if _task_cancelled(task_id):
                    raise _ScanCancelled()

        if scan_all:
            groups = build_all_incomplete_album_review(
                user["navidrome_user"], user["navidrome_password"],
                fuzzy=fuzzy, progress=_progress)
            groups = _merge_review_groups(groups)
            with _review_lock:
                tasks = _review_state.get("tasks", {})
                operations = _review_state.get("operations", {})
                duplicate_groups = _review_state.get("duplicate_groups", [])
                duplicate_files = _review_state.get("duplicate_files", [])
                # This scan does not recompute duplicate files, so the list is
                # carried over — with its stats and timestamp, so the UI can
                # say how old it is instead of implying it is fresh.
                duplicate_file_stats = _review_state.get("duplicate_file_stats", {})
                duplicate_files_scanned_at = _review_state.get("duplicate_files_scanned_at", 0)
                _review_state = _empty_review_state()
                _review_state.update({
                    "status": "complete",
                    "message": f"Found {len(groups)} album review group(s)",
                    "fuzzy": fuzzy,
                    "groups": groups,
                    "duplicate_groups": duplicate_groups,
                    "duplicate_files": duplicate_files,
                    "duplicate_file_stats": duplicate_file_stats,
                    "duplicate_files_scanned_at": duplicate_files_scanned_at,
                    "tasks": tasks,
                    "operations": operations,
                })
            summary = f"Found {len(groups)} album review group(s)"
        else:
            # Duplicates get their own list: a duplicates scan must not reset
            # the missing-tracks scan results (and vice versa).
            phase_one_records = {}
            groups = build_duplicate_album_review(
                user["navidrome_user"], user["navidrome_password"],
                fuzzy=fuzzy, deep=deep, progress=lambda d, t: _progress(d, t),
                cancel=_cancel, records_out=phase_one_records)
            groups = _merge_review_groups(groups, source_key="duplicate_groups",
                                          include_jobs=False)
            with _review_lock:
                _review_state["duplicate_groups"] = groups
                _review_state["message"] = (
                    f"Found {len(groups)} duplicate album group(s) — "
                    "checking for duplicate files")
            # Phase two: duplicate *files* within a single album. Same scan,
            # because the user's question is one question ("what's doubled up
            # in my library?") even though the two halves need different walks.
            dup_stats = {}
            dup_files = build_duplicate_file_review(
                user["navidrome_user"], user["navidrome_password"],
                progress=_progress, cancel=_cancel, stats=dup_stats,
                records=phase_one_records)
            with _review_lock:
                _review_state["status"] = "complete"
                _review_state["message"] = (
                    f"Found {len(groups)} duplicate album group(s) and "
                    f"{len(dup_files)} duplicate file set(s)")
                _review_state["fuzzy"] = fuzzy
                _review_state["deep"] = deep
                _review_state["duplicate_files"] = dup_files
                _review_state["duplicate_file_stats"] = dup_stats
                _review_state["duplicate_files_scanned_at"] = time.time()
            summary = (f"Found {len(groups)} duplicate album group(s), "
                       f"{len(dup_files)} duplicate file set(s)")
        _web_log(f"{'all-album' if scan_all else 'duplicate'} scan complete: {len(groups)} group(s)")
        if task_id:
            _task_finish(task_id, summary)
    except _ScanCancelled:
        with _review_lock:
            _review_state["status"] = "cancelled"
            _review_state["message"] = "Scan cancelled"
        _web_log("album scan cancelled by user")
        if task_id:
            _task_update(task_id, status="cancelled", summary="Scan cancelled")
    except Exception as e:
        with _review_lock:
            _review_state["status"] = "error"
            _review_state["message"] = str(e)
        _web_log(f"album scan failed: {e}")
        if task_id:
            _task_finish(task_id, error=str(e))
    finally:
        _save_review_state()
        with _review_scan_lock:
            if _active_review_scan_task_id == task_id:
                _active_review_scan_task_id = ""

def _start_review_scan(fuzzy: bool, scan_all: bool, deep: bool = False) -> dict:
    """Start one in-process review scan; persisted state is display-only."""
    global _active_review_scan_task_id
    with _review_scan_lock:
        if _active_review_scan_task_id:
            return {
                "ok": False,
                "error": "Scan already running",
                "task_id": _active_review_scan_task_id,
            }
        kind = "all-album-scan" if scan_all else "duplicate-scan"
        label = "Scan missing tracks for all albums" if scan_all else "Scan duplicate albums"
        task_id = _task_create(kind, label)
        # The scan's progress callback honours cancellation, so offer the
        # button — _task_create defaults every task to not-cancellable.
        _task_update(task_id, cancellable=True)
        _active_review_scan_task_id = task_id
        with _review_lock:
            _review_state["status"] = "running"
            _review_state["message"] = "Scanning all albums" if scan_all else "Scanning duplicate albums"
            _review_state["fuzzy"] = fuzzy
            _review_state["deep"] = deep
        _save_review_state()
        thread = threading.Thread(
            target=_scan_review_worker, args=(fuzzy, task_id, scan_all, deep),
            daemon=True, name="all-album-review-scan" if scan_all else "album-review-scan")
        try:
            thread.start()
        except Exception as e:
            _active_review_scan_task_id = ""
            _task_finish(task_id, error=str(e))
            with _review_lock:
                _review_state["status"] = "error"
                _review_state["message"] = str(e)
            _save_review_state()
            return {"ok": False, "error": str(e), "task_id": task_id}
        return {"ok": True, "task_id": task_id}

def _set_review_track_state(group_id: str, track_index, decision: str,
                            **updates) -> None:
    if track_index is None:
        return
    try:
        idx = int(track_index)
    except Exception:
        return
    with _review_lock:
        group = _find_review_group(group_id)
        if not group:
            return
        tracks = group.get("missing_tracks", [])
        if 0 <= idx < len(tracks):
            tracks[idx]["decision"] = decision
            tracks[idx].update({k: v for k, v in updates.items() if v is not None})
            job = _repair_job_for_group(group_id)
            if job:
                tid = tracks[idx].get("repair_track_id") or _repair_track_id(group_id, idx, tracks[idx])
                jt = _repair_track_by_id(job, tid)
                if jt:
                    jt["status"] = _repair_track_status_from_decision(decision)
                    jt["updated_at"] = time.time()
                    for src, dest in (
                        ("local_path", "local_path"),
                        ("source_user", "source_user"),
                        ("source_folder", "source_folder"),
                        ("download_percent", "download_percent"),
                        ("download_state", "download_state"),
                        ("filename", "filename"),
                        ("error", "error"),
                        ("download_error", "error"),
                        ("matched_relpath", "matched_relpath"),
                    ):
                        if updates.get(src) is not None:
                            jt[dest] = updates[src]
                    job["status"] = _repair_job_status_from_tracks(job)
                    _repair_job_touch(job)
            group["updated_at"] = time.time()

def _approved_review_tracks(group: dict) -> list:
    approved = []
    for idx, track in enumerate(group.get("missing_tracks", [])):
        if track.get("decision") == "approved":
            t = dict(track)
            t["_review_group_id"] = group.get("id", "")
            t["_review_track_index"] = idx
            t["_match_mode"] = group.get("match_mode", "auto")
            t["_target_release_mbid"] = group.get("canonical_mbid", "")
            approved.append(t)
    return approved

def _source_coverage_summary(fd: dict, group: dict = None,
                             tracks: list = None) -> dict:
    """How much of what we want this peer's folder actually holds.

    `tracks` lets a caller with no review group — the artist/album page, which is
    downloading a *new* album — pair against the canonical MusicBrainz tracklist
    instead. Without it that path had no pairing at all and fell back to comparing
    file *counts*, so any folder with enough files read as a full match, however
    unrelated its contents.
    """
    if tracks is None:
        tracks = []
        if group:
            tracks = [
                t for t in group.get("missing_tracks", [])
                if t.get("decision") in ("approved", "source_pending", "queued",
                                          "downloading", "failed")
            ]
    files = fd.get("files", []) or []
    siblings = _release_track_titles(group) or [t.get("title", "") for t in tracks]
    used = set()
    matched = []
    unmatched = []
    for track in tracks:
        best, basis, note = _best_file_match(track, files, used, siblings=siblings,
                                             release_tracks=tracks)
        row = {
            "position": track.get("position", ""),
            "artist": track.get("artist", ""),
            "title": track.get("title", ""),
        }
        if best:
            used.add(best.get("filename", ""))
            row["filename"] = best.get("filename", "")
            # How the pairing was reached, so the picker can distinguish a
            # title that agreed from a duration that merely didn't disagree.
            row["basis"] = basis
            matched.append(row)
        else:
            if note:
                row["note"] = note
            unmatched.append(row)
    total = len(tracks)
    return {
        "matched": len(matched),
        "total": total,
        "unmatched": len(unmatched),
        "matched_tracks": matched,
        "unmatched_tracks": unmatched,
        "label": f"{len(matched)}/{total}" if total else "unknown",
    }

def _source_risk_and_recommendation(fd: dict, coverage: dict) -> tuple:
    files = fd.get("files", []) or []
    folder = fd.get("folder", "")
    audio_count = len([f for f in files
                       if _file_ext(f.get("filename", "")) in _accepted_formats()])
    total = int(coverage.get("total") or 0)
    matched = int(coverage.get("matched") or 0)
    queue_length = int(fd.get("queue_length") or 0)
    # Folder-local, not the peer-wide lockedFileCount: a peer locking something
    # in a different album says nothing about this one, and flagging it as a
    # risk here just taught the eye to ignore the badge.
    locked = int(fd.get("locked_in_folder") or 0)
    risks = []
    if LIVE_RE.search(folder):
        risks.append({"code": "live", "label": "live recording"})
    if COMPILATION_RE.search(folder):
        risks.append({"code": "compilation", "label": "compilation"})
    if total and audio_count and audio_count < total:
        risks.append({"code": "too_few_files", "label": "too few visible files"})
    if total and matched < total:
        risks.append({"code": "poor_filename_match", "label": "filename mismatch"})
    if queue_length >= 5:
        risks.append({"code": "queue", "label": f"queue {queue_length}"})
    if locked:
        risks.append({"code": "locked", "label": f"{locked} locked"})
    if not fd.get("has_free_upload_slot") and queue_length:
        risks.append({"code": "no_free_slot", "label": "no free slot"})

    risk_codes = {r["code"] for r in risks}
    if total and matched == total and not risk_codes:
        recommendation = "Best match"
    elif total and matched == total and risk_codes <= {"queue", "no_free_slot"}:
        recommendation = "Complete but queued"
    elif "live" in risk_codes:
        recommendation = "Risky live source"
    elif total and matched and matched < total:
        recommendation = "Fast but incomplete" if (fd.get("upload_speed") or 0) else "Incomplete match"
    elif risk_codes:
        recommendation = "Review before download"
    else:
        recommendation = "Possible match"
    return risks, recommendation

_SOURCE_FILE_LIMIT = 60
# The expanded listing is the peer's *whole* folder, asked for deliberately by
# opening the disclosure, so it is allowed to be long.
_SOURCE_FILE_LIMIT_EXPANDED = 400

def _source_files_view(fd: dict, coverage: dict, limit: int = 0) -> list:
    """Every file in the peer's folder, each tagged with the track it would fill.

    The pairing is already computed for the coverage count and was then thrown
    away, so there was no way to answer the only question that matters before
    committing to a download: is this even the right album? A folder whose files
    match no slot at all is now visibly wrong instead of reading as a full match
    on file count alone.
    """
    claimed = {}
    for row in coverage.get("matched_tracks", []) or []:
        if row.get("filename"):
            claimed[row["filename"]] = {"position": row.get("position", ""),
                                        "title": row.get("title", ""),
                                        "basis": row.get("basis", "")}
    accepted = _accepted_formats()
    out = []
    for f in (fd.get("files", []) or [])[:(limit or _SOURCE_FILE_LIMIT)]:
        name = f.get("filename", "")
        size = int(f.get("size") or 0)
        out.append({
            "filename": name.replace("\\", "/").rsplit("/", 1)[-1],
            # The peer's own (backslash-separated) path. `filename` is the
            # basename for display; this is the value the frontend names back to
            # the server when picking a file for a track, and the only one that
            # identifies the file unambiguously to slskd.
            "peerFilename": name,
            "ext": _file_ext(name),
            "accepted": _file_ext(name) in accepted,
            "sizeMb": round(size / 1024 / 1024, 1) if size else 0,
            "bitrate": f.get("bitRate") or f.get("bitrate") or 0,
            "durationSec": int(f.get("length") or 0),
            "matchedTo": claimed.get(name),
        })
    return out

def _source_summary(fd: dict, idx: int, group: dict = None,
                    tracks: list = None) -> dict:
    coverage = _source_coverage_summary(fd, group, tracks)
    risks, recommendation = _source_risk_and_recommendation(fd, coverage)
    files = fd.get("files", [])
    bitrates = [f.get("bitRate") or f.get("bitrate") for f in files]
    bitrates = [b for b in bitrates if b]
    return {
        "index": idx,
        "username": fd.get("username", ""),
        "folder": fd.get("folder", ""),
        "raw_folder": fd.get("raw_folder", ""),
        "quality": _folder_quality(files),
        "bitrate_kbps": (max(set(bitrates), key=bitrates.count) if bitrates else None),
        "size_mb": round(sum((f.get("size") or 0) for f in files) / 1024 / 1024),
        "coverage": coverage,
        "risk_flags": risks,
        "recommendation": recommendation,
        "files": _source_files_view(fd, coverage),
        "files_truncated": len(files) > _SOURCE_FILE_LIMIT,
        "file_count": len(fd.get("files", [])),
        "upload_speed": fd.get("upload_speed", 0),
        "speed_mbps": round((fd.get("upload_speed", 0) or 0) / 1024 / 1024, 1),
        "has_free_upload_slot": bool(fd.get("has_free_upload_slot")),
        "queue_length": fd.get("queue_length", 0),
        "locked_file_count": fd.get("locked_file_count", 0),
        "score": fd.get("score", 0),
        # How much the folder's own name looks like the album asked for, so the
        # picker can say "matches album title" / "different album?" rather than
        # leaving the user to read peer paths.
        "album_match": round(float(fd.get("album_match") or 0)),
        "album_match_ok": bool(fd.get("album_match_ok")),
        "artist_match": round(float(fd.get("artist_match") or 0)),
        "year_in_path": bool(fd.get("year_in_path")),
        "quality_profile": _folder_quality_profile(files),
        "is_live": bool(LIVE_RE.search(fd.get("folder", ""))),
        "is_compilation": bool(COMPILATION_RE.search(fd.get("folder", ""))),
    }

def _expanded_source_payload(group: dict, fd: dict, idx: int) -> dict:
    """The peer's *real* folder listing for one source, re-paired against the gaps.

    The picker only ever showed the peer's search **hits** — `fd["files"]` comes
    solely from `slskd_run_search`, while the folder listing was fetched at
    download time and thrown away. A file that exists in the source but didn't
    match the search query was invisible to the UI and to the matcher, so a
    source that really does have the track reported it missing. Expanding on
    open fixes the numbers as well as the list: coverage, `matchedTo` and the
    risk flags are all recomputed over the full listing.

    Cached on `fd["_expanded"]` — the field the failover path already uses.
    """
    ref = fd.get("files", [{}])[0] if fd.get("files") else {}
    expanded = fd.get("_expanded")
    if not expanded:
        expanded = slskd_expand_directory(fd.get("username", ""), fd, ref)
        if expanded:
            fd["_expanded"] = expanded
    view_fd = {**fd, "files": expanded or fd.get("files", []) or []}
    coverage = _source_coverage_summary(view_fd, group)
    files = _source_files_view(view_fd, coverage, limit=_SOURCE_FILE_LIMIT_EXPANDED)
    return {
        "ok": True,
        "index": idx,
        # False means the expand call failed or the peer is offline; the caller
        # is looking at search hits and the UI says so rather than implying the
        # folder really holds only this.
        "expanded": bool(expanded),
        "files": files,
        "filesTruncated": len(view_fd["files"]) > _SOURCE_FILE_LIMIT_EXPANDED,
        "fileCount": len(view_fd["files"]),
        "coverage": coverage.get("label", "unknown"),
        "coverageDetail": {
            "haveTracks": coverage.get("matched", 0),
            "totalTracks": coverage.get("total", 0),
        },
        "missingTracks": [{"position": t.get("position", ""),
                           "title": t.get("title", "")}
                          for t in coverage.get("unmatched_tracks", [])],
    }

def _group_source_page(group: dict, page: int = 0, per: int = 10) -> dict:
    src = group.get("source_results") or {}
    folders = src.get("folders") or []
    pages = max(1, (len(folders) + per - 1) // per)
    page = max(0, min(int(page or 0), pages - 1))
    start = page * per
    return {
        "ok": True,
        "mode": src.get("mode", "album"),
        "query": src.get("query", ""),
        "created_at": src.get("created_at", 0),
        "total": len(folders),
        "page": page,
        "pages": pages,
        "per": per,
        "sources": [_source_summary(fd, start + i, group)
                    for i, fd in enumerate(folders[start:start + per])],
    }

# The source search used to be one function, _prepare_group_sources, called with
# _review_lock held. It is now three, so the slow middle one can run with the
# lock released: plan (under the lock) -> search (network, no shared state) ->
# apply (under the lock again). _run_group_source_search drives the sequence.

def _group_source_plan(group: dict) -> dict:
    """What to search for, plus everything the search needs from the group.

    Read under _review_lock; the returned dict is self-contained so the search
    itself can run with the lock released.
    """
    approved = _approved_review_tracks(group)
    if not approved:
        return {"ok": False, "message": "No approved tracks"}
    job = _create_or_update_repair_job_from_group(group, approved)
    if job:
        job["status"] = "needs_source"
        _repair_job_touch(job, {"kind": "source_search", "message": "Searching for repair sources"})
    if group.get("group_type") == "tracks" or not group.get("canonical_mbid"):
        first = approved[0]
        # The same edition-noise cleaning the album path gets: a track titled
        # "Song (2011 Remaster)" is filed by peers as plain "Song".
        return {"ok": True, "mode": "track", "allow_mp3": group.get("allow_mp3"),
                "query": _dedupe_terms(
                    f"{first.get('artist', '')} "
                    f"{_clean_album_title(first.get('title', ''))}").strip(),
                "artist": first.get("artist", ""),
                "expected": 1}
    return {"ok": True, "mode": "album", "allow_mp3": group.get("allow_mp3"),
            "query": f"{group.get('artist', '')} {group.get('album', '')}".strip(),
            "artist": group.get("artist", ""), "album": group.get("album", ""),
            # Year and the missing titles feed the query variants: the year is
            # what disambiguates a self-titled album, and a distinctive track
            # title is the last-resort query when the album name cannot be.
            "year": _group_release_year(group),
            "track_titles": [t.get("title", "") for t in approved if t.get("title")],
            "expected": len(approved) or group.get("total", 0)}

def _group_release_year(group: dict) -> str:
    """A 4-digit year for the group's release, from whatever carries one."""
    for album in (group.get("albums") or []):
        for key in ("year", "originalYear", "date", "originalDate"):
            m = re.search(r"\b(1[89]\d{2}|20\d{2})\b", str(album.get(key) or ""))
            if m:
                return m.group(1)
    return ""

def _search_group_sources(plan: dict, progress=None, stats: dict = None) -> list:
    """The network half: slskd only, no shared state touched.

    This is the part that takes tens of seconds, and the reason the whole
    sequence was split — it must not run while _review_lock is held.
    """
    with _mp3_fallback(plan.get("allow_mp3")):
        if plan.get("mode") == "track":
            return slskd_run_search(plan["query"], plan.get("expected", 1),
                                    progress=progress, stats=stats,
                                    artist=plan.get("artist", ""))
        return slskd_search_album_folders(plan.get("artist", ""), plan.get("album", ""),
                                          plan.get("expected", 0), progress=progress,
                                          stats=stats, year=plan.get("year", ""),
                                          track_titles=plan.get("track_titles") or [])

def _apply_group_sources(group: dict, plan: dict, folders: list,
                         stats: dict = None) -> dict:
    """Write search results onto the group. Caller must hold _review_lock."""
    approved = _approved_review_tracks(group)
    # Idempotent: _group_source_plan already created it before the search.
    job = _create_or_update_repair_job_from_group(group, approved)
    mode, query = plan.get("mode", "album"), plan.get("query", "")
    if not folders:
        reason = _no_source_reason(stats or {})
        message = f"No usable source: {reason}"
        # Whether relaxing the format policy for this album would have found
        # something, so the opt-in can be offered on the evidence rather than
        # only after a download has already failed for want of a file.
        group["no_source_reason"] = reason
        group["mp3_would_help"] = bool(
            not group.get("allow_mp3")
            and MP3_FALLBACK_EXT in (stats or {}).get("rejected_formats", ()))
        if job:
            job["status"] = "blocked_no_source"
            _repair_job_touch(job, {"kind": "blocked_no_source", "message": message})
        return {"ok": False, "message": message}
    group.pop("no_source_reason", None)
    group.pop("mp3_would_help", None)
    now = time.time()
    group["source_results"] = {
        "mode": mode,
        "query": query,
        "created_at": now,
        "folders": folders,
        "summaries": [_source_summary(fd, idx, group)
                      for idx, fd in enumerate(folders)],
    }
    group["last_action"] = "source_search"
    group["updated_at"] = now
    for idx, track in enumerate(group.get("missing_tracks", [])):
        if track.get("decision") == "approved":
            track["decision"] = "source_pending"
    if job:
        job["source_pools"] = [{
            "id": hashlib.sha1(f"{fd.get('username','')}|{fd.get('folder','')}".encode("utf-8")).hexdigest()[:16],
            "source_user": fd.get("username", ""),
            "source_folder": fd.get("folder", ""),
            "status": "candidate",
            # Not the file list: this was a second full copy of the peer's
            # listing (source_results.folders already holds it) persisted into
            # every review-state write, and nothing ever read it back.
            "file_count": len(fd.get("files") or []),
            "created_at": now,
        } for fd in folders[:20]]
        for jt in job.get("tracks", []):
            if jt.get("status") == "approved":
                jt["status"] = "approved"
        _repair_job_touch(job, {"kind": "sources_found", "message": f"Found {len(folders)} source(s)"})
    return {"ok": True, "message": f"Found {len(folders)} source(s)",
            "sources": len(folders)}

def _enqueue_group_source(group: dict, source_index: int) -> dict:
    with _mp3_fallback(group.get("allow_mp3")):
        return _enqueue_group_source_inner(group, source_index)

def _enqueue_group_source_inner(group: dict, source_index: int) -> dict:
    approved = []
    for idx, track in enumerate(group.get("missing_tracks", [])):
        if track.get("decision") in ("approved", "source_pending"):
            t = dict(track)
            t["_review_group_id"] = group.get("id", "")
            t["_review_track_index"] = idx
            t["_match_mode"] = group.get("match_mode", "auto")
            t["_target_release_mbid"] = group.get("canonical_mbid", "")
            approved.append(t)
    if not approved:
        return {"ok": False, "message": "No approved/source-pending tracks"}
    folders = (group.get("source_results") or {}).get("folders") or []
    if not (0 <= int(source_index) < len(folders)):
        return {"ok": False, "message": "Source index out of range"}
    job = _create_or_update_repair_job_from_group(group, approved)
    if job:
        job["status"] = "source_selected"
        _repair_job_touch(job, {"kind": "source_selected", "message": f"Selected source #{int(source_index) + 1}"})
        by_index = {t.get("group_track_index"): t for t in job.get("tracks", [])}
        for track in approved:
            jt = by_index.get(track.get("_review_track_index"))
            if jt:
                track["repair_job_id"] = job.get("id", "")
                track["repair_track_id"] = jt.get("id", "")
    user = _default_web_user()
    fd = folders[int(source_index)]
    ref_file = fd["files"][0] if fd.get("files") else {}
    full = slskd_expand_directory(fd["username"], fd, ref_file) or fd.get("files", [])
    # Every title on the release, so a loose filename match can be rejected when
    # it plainly belongs to a track we already have.
    siblings = _release_track_titles(group)
    if group.get("group_type") == "tracks" or not group.get("canonical_mbid"):
        ok = 0
        total = len(approved)
        ag_id = None
        # Ordered candidate list: selected source first, then alternatives.
        # On immediate rejection, advance through the list without bouncing to the UI.
        ordered_sources = [fd] + [f for j, f in enumerate(folders) if j != int(source_index)][:6]
        # One claim ledger per candidate source, hoisted out of the track loop: a
        # fresh set per track let two different missing tracks be paired to the
        # identical file in the same folder.
        claimed_by_source = [[] for _ in ordered_sources]
        closest = ""
        for track in approved:
            enqueued = False
            for try_pos, fd_try in enumerate(ordered_sources):
                if try_pos == 0:
                    f_full = full
                else:
                    ref = fd_try["files"][0] if fd_try.get("files") else {}
                    f_full = slskd_expand_directory(fd_try["username"], fd_try, ref) or fd_try.get("files", [])
                claimed = claimed_by_source[try_pos]
                best, _basis, note = _best_file_match(track, f_full, claimed,
                                                      siblings=siblings,
                                                      release_tracks=approved)
                if not best:
                    # Keep the nearest thing any source offered, so the failure
                    # says what it nearly matched instead of just "no file".
                    closest = closest or note
                    continue
                claimed.append(best.get("filename", ""))
                if slskd_enqueue(
                        fd_try["username"], best, track=track,
                        token=user.get("telegram_token", ""),
                        chat_id=str(user.get("chat_id", "")),
                        candidates=ordered_sources[try_pos + 1:],
                        review_group_id=group.get("id", ""),
                        review_track_index=track.get("_review_track_index"),
                        match_mode=group.get("match_mode", "auto"),
                        target_release_mbid=group.get("canonical_mbid", ""),
                        repair_job_id=(job or {}).get("id", ""),
                        repair_track_id=track.get("repair_track_id", "")):
                    enqueued = True
                    _set_review_track_state(group.get("id", ""),
                                            track.get("_review_track_index"), "queued",
                                            source_user=fd_try.get("username", ""),
                                            source_folder=fd_try.get("folder", ""))
                    break
                # Not queued after all — release the claim so another track can
                # still be paired with this file from this source.
                if best.get("filename", "") in claimed:
                    claimed.remove(best.get("filename", ""))
            if enqueued:
                ok += 1
            else:
                _set_review_track_state(
                    group.get("id", ""), track.get("_review_track_index"), "failed",
                    error=("Could not match file in selected source"
                           + (f" — {closest}" if closest else "")))
        group["last_action"] = "download"
        group["updated_at"] = time.time()
        return {"ok": ok > 0, "message": f"Queued {ok}/{total} track(s)",
                "album_group_id": ag_id}
    ok, total, ag_id = slskd_enqueue_folder(
        fd["username"], full, token=user.get("telegram_token", ""),
        chat_id=str(user.get("chat_id", "")), label=group.get("album", ""),
        release_mbid=group.get("canonical_mbid", ""),
        alt_sources=[f for j, f in enumerate(folders) if j != int(source_index)][:6],
        artist=group.get("artist", ""), album=group.get("album", ""),
        missing_tracks=approved, review_group_id=group.get("id", ""),
        match_mode=group.get("match_mode", "auto"),
        repair_job_id=(job or {}).get("id", ""),
        allow_mp3=bool(group.get("allow_mp3")),
        siblings=siblings)
    if ok:
        # slskd_enqueue_folder silently drops approved tracks it couldn't pair
        # with a file in the folder. Marking those "queued" anyway created
        # phantoms: nothing was ever downloading for them, yet the group stayed
        # "downloading" forever and every retry path short-circuited on
        # alreadyActive. Only mark the tracks that actually got a file.
        matched_idx = {
            (t or {}).get("_review_track_index")
            for _f, t in _album_file_pairs_for_missing_tracks(
                full, approved, siblings=siblings, folder=fd,
                release_total=int(group.get("total") or 0),
                release_tracks=approved)
            if t is not None
        }
        # No track left behind. A track the chosen folder has no file for used
        # to fail on the spot, even though the search had found other sources
        # that might. The per-track path has always failed over like this; the
        # album path simply never did, which is a large share of the fills that
        # finish two or three tracks short.
        leftovers = [t for t in approved
                     if t.get("_review_track_index") not in matched_idx]
        rescued, rescue_notes = _rescue_leftover_tracks(
            group, leftovers, folders, int(source_index), siblings, job, user)
        matched_idx |= set(rescued)
        for idx, track in enumerate(group.get("missing_tracks", [])):
            if track.get("decision") not in ("approved", "source_pending"):
                continue
            if idx in matched_idx:
                track["decision"] = "queued"
                # A rescued track came from a different peer, and saying it
                # came from the chosen one would make the failover invisible.
                src = rescued.get(idx) if isinstance(rescued, dict) else None
                track["source_user"] = (src or fd).get("username", "")
                track["source_folder"] = (src or fd).get("folder", "")
                track.pop("download_error", None)
            else:
                track["decision"] = "failed"
                note = rescue_notes.get(idx, "")
                track["download_error"] = ("no matching file in any source"
                                           + (f" — {note}" if note else ""))
        group["last_action"] = "download"
        group["updated_at"] = time.time()
        queued = len(matched_idx)
        return {"ok": True, "album_group_id": ag_id,
                "message": (f"Queued {queued}/{len(approved)} track(s)"
                            + (f" ({len(rescued)} from an alternate source)"
                               if rescued else ""))}
    return {"ok": False, "message": "Could not queue selected missing tracks"}

def _rescue_leftover_tracks(group: dict, leftovers: list, folders: list,
                            source_index: int, siblings: list,
                            job: dict, user: dict) -> tuple:
    """Try the alternate sources for tracks the chosen folder had no file for.

    Mirrors the per-track branch: walk the other ranked folders, expand each
    one's real listing, and enqueue the first file that matches — with one
    claim ledger per source so two leftovers can't both be paired to the same
    file. Returns ({review_track_index: source_folder_dict}, {index: note}).
    """
    if not leftovers:
        return {}, {}
    alts = [f for j, f in enumerate(folders) if j != source_index][:6]
    if not alts:
        return {}, {t.get("_review_track_index"): "" for t in leftovers}
    listings, claims = {}, {}
    rescued, notes = {}, {}
    for track in leftovers:
        idx = track.get("_review_track_index")
        note = ""
        for pos, alt in enumerate(alts):
            if pos not in listings:
                ref = alt["files"][0] if alt.get("files") else {}
                listings[pos] = (slskd_expand_directory(alt["username"], alt, ref)
                                 or alt.get("files", []))
                claims[pos] = []
            best, _basis, miss = _best_file_match(track, listings[pos], claims[pos],
                                                  siblings=siblings,
                                                  release_tracks=leftovers)
            if not best:
                note = note or miss
                continue
            claims[pos].append(best.get("filename", ""))
            if slskd_enqueue(
                    alt["username"], best, track=track,
                    token=user.get("telegram_token", ""),
                    chat_id=str(user.get("chat_id", "")),
                    candidates=alts[pos + 1:],
                    review_group_id=group.get("id", ""),
                    review_track_index=idx,
                    match_mode=group.get("match_mode", "auto"),
                    target_release_mbid=group.get("canonical_mbid", ""),
                    repair_job_id=(job or {}).get("id", ""),
                    repair_track_id=track.get("repair_track_id", "")):
                rescued[idx] = alt
                break
            # Not queued after all — release the claim so another leftover can
            # still be paired with this file.
            if best.get("filename", "") in claims[pos]:
                claims[pos].remove(best.get("filename", ""))
        if idx not in rescued:
            notes[idx] = note
    if rescued:
        print(f"  album enqueue: rescued {len(rescued)} leftover track(s) "
              f"from alternate sources")
    return rescued, notes

def _pick_file_for_track(group: dict, track_index: int, source_index: int,
                         filename: str) -> dict:
    """Download one specific peer file for one specific missing track.

    Everything downstream already exists: `slskd_enqueue` takes `track=`,
    `review_group_id=` and `review_track_index=`, and the poller carries them
    through to `decision="downloaded"` + `local_path` on that exact slot. Only
    the *file choice* was automatic, with no way for a user to correct it.

    The binding is recorded on the track as `manual_pick` so placement honours it
    (`manual_pairs` in `_deterministic_album_import`) rather than re-matching and
    quietly undoing the choice.
    """
    tracks = group.get("missing_tracks", []) or []
    try:
        idx = int(track_index)
    except (TypeError, ValueError):
        return {"ok": False, "message": "Bad track index"}
    if not (0 <= idx < len(tracks)):
        return {"ok": False, "message": "Track index out of range"}
    folders = (group.get("source_results") or {}).get("folders") or []
    if not (0 <= int(source_index) < len(folders)):
        return {"ok": False, "message": "Source index out of range"}
    fd = folders[int(source_index)]
    pool = fd.get("_expanded") or fd.get("files") or []
    want = (filename or "").strip()
    base_want = want.replace("\\", "/").rsplit("/", 1)[-1].lower()
    chosen = next((f for f in pool if f.get("filename", "") == want), None)
    if chosen is None:
        chosen = next(
            (f for f in pool
             if f.get("filename", "").replace("\\", "/").rsplit("/", 1)[-1].lower() == base_want),
            None)
    if chosen is None:
        return {"ok": False, "message": "That file is not in this source's listing"}

    track = tracks[idx]
    t = dict(track)
    t["_review_group_id"] = group.get("id", "")
    t["_review_track_index"] = idx
    t["_match_mode"] = "manual"
    t["_target_release_mbid"] = group.get("canonical_mbid", "")
    job = _repair_job_for_group(group.get("id", "")) or \
        _create_or_update_repair_job_from_group(group, [t])
    if job:
        jt = next((r for r in job.get("tracks", [])
                   if r.get("group_track_index") == idx), None)
        if jt:
            t["repair_job_id"] = job.get("id", "")
            t["repair_track_id"] = jt.get("id", "")
    user = _default_web_user() or {}
    if not slskd_enqueue(
            fd.get("username", ""), chosen, track=t,
            token=user.get("telegram_token", ""),
            chat_id=str(user.get("chat_id", "")),
            review_group_id=group.get("id", ""),
            review_track_index=idx,
            match_mode="manual",
            target_release_mbid=group.get("canonical_mbid", ""),
            repair_job_id=(job or {}).get("id", ""),
            repair_track_id=t.get("repair_track_id", "")):
        return {"ok": False, "message": "slskd refused the download"}

    track["manual_pick"] = {
        "username": fd.get("username", ""),
        "filename": chosen.get("filename", ""),
        "picked_at": time.time(),
    }
    # A hand-picked file must not be silently replaced by a later auto pass.
    track["match_mode"] = "manual"
    track.pop("can_force_place", None)
    track.pop("force_place_conflict", None)
    _set_review_track_state(group.get("id", ""), idx, "queued",
                            source_user=fd.get("username", ""),
                            source_folder=fd.get("folder", ""),
                            filename=chosen.get("filename", ""))
    track.pop("download_error", None)
    group["last_action"] = "manual_pick"
    group["updated_at"] = time.time()
    return {"ok": True,
            "message": f"Queued {os.path.basename(chosen.get('filename', '').replace(chr(92), '/'))} "
                       f"for “{track.get('title', '')}”",
            "filename": chosen.get("filename", "")}

def _review_group_from_album_download(group: dict, source: str) -> dict:
    release_mbid = group.get("release_mbid") or group.get("canonical_mbid", "")
    artist = group.get("artist", "")
    album = group.get("album", "")
    missing = []
    for track in group.get("missing_tracks", []):
        t = dict(track)
        t.setdefault("decision", "pending")
        missing.append(t)
    gid = hashlib.sha1(f"{source}|{release_mbid}|{artist}|{album}".encode("utf-8")).hexdigest()[:16]
    return {
        "id": gid,
        "group_type": source,
        "artist": artist,
        "album": album,
        "artist_key": _norm_album_text(artist),
        "album_key": _norm_album_text(album),
        "created_at": time.time(),
        "updated_at": time.time(),
        "status": "needs_review",
        "merge_mode": "logical",
        "match_mode": "auto",
        "canonical_album_id": "",
        "canonical_mbid": release_mbid,
        "albums": [],
        "missing_tracks": missing,
        "present": group.get("present_count", group.get("present", 0)),
        "total": group.get("total_tracks", group.get("total", len(missing))),
        "last_action": source,
        "messages": [],
    }

def _union_review_groups(new_groups: list) -> None:
    """
    Merge scan output into _review_state["groups"] WITHOUT discarding groups
    the scan didn't cover. Single-artist scans must use this; full-library
    rebuilds use _store_review_groups (replace semantics) because their list
    is the complete truth. De-dups identical ids inside new_groups, updates
    existing groups in place (order preserved) and appends genuinely new ones.
    """
    deduped, seen = [], set()
    for g in new_groups:
        gid = g.get("id")
        if gid in seen:
            continue
        seen.add(gid)
        deduped.append(g)
    merged = _merge_review_groups(deduped)
    with _review_lock:
        by_id = {g.get("id"): g for g in merged}
        out = [by_id.pop(g.get("id"), g) for g in _review_state.get("groups", []) or []]
        out.extend(by_id.values())
        _review_state["groups"] = out
    _save_review_state()

def _store_review_groups(groups: list, message: str, fuzzy: bool = None) -> None:
    # REPLACE semantics by design: callers are full-library rebuilds
    # (scan-all / playlist / Spotify) where `groups` is the complete truth.
    # Single-artist scans must union via _union_review_groups instead.
    with _review_lock:
        tasks = _review_state.get("tasks", {})
        searches = _review_state.get("searches", {})
        operations = _review_state.get("operations", {})
        _review_state.update({
            "status": "complete",
            "message": message,
            "groups": _merge_review_groups(groups),
            "tasks": tasks,
            "searches": searches,
            "operations": operations,
        })
        if fuzzy is not None:
            _review_state["fuzzy"] = fuzzy
    _save_review_state()

def _playlist_scan_task(task_id: str) -> None:
    user = _default_web_user()
    _task_update(task_id, current="ListenBrainz playlists")
    missing = scan_user(user)
    if not missing:
        _store_review_groups([], "Playlist scan found no missing tracks")
        _task_finish(task_id, "No missing tracks")
        return
    nd_user, nd_pass = user["navidrome_user"], user["navidrome_password"]
    album_groups, solo_tracks = group_missing_by_album(
        missing, nd_user, nd_pass,
        progress=lambda d, t: _task_update(task_id, done=d, total=t,
                                           current="Grouping missing tracks"))
    review_groups = [_review_group_from_album_download(g, "playlist")
                     for g in album_groups]
    if solo_tracks:
        review_groups.append({
            "id": hashlib.sha1(f"playlist|solo|{time.time()}".encode("utf-8")).hexdigest()[:16],
            "group_type": "tracks",
            "artist": "Loose tracks",
            "album": "Playlist tracks",
            "artist_key": "loose tracks",
            "album_key": "playlist tracks",
            "created_at": time.time(),
            "updated_at": time.time(),
            "status": "needs_review",
            "merge_mode": "logical",
            "match_mode": "auto",
            "canonical_album_id": "",
            "canonical_mbid": "",
            "albums": [],
            "missing_tracks": [{**t, "decision": "pending"} for t in solo_tracks],
            "present": 0,
            "total": len(solo_tracks),
            "last_action": "playlist",
            "messages": [],
        })
    _store_review_groups(review_groups, f"Playlist scan found {len(missing)} missing track(s)")
    _task_finish(task_id, f"Found {len(missing)} missing track(s)")

def _spotify_scan_task(task_id: str, playlist: str) -> None:
    user = _default_web_user()
    _task_update(task_id, current="Fetching Spotify playlist")
    tracks = spotify_get_playlist_tracks(spotify_playlist_id(playlist))
    missing = _check_missing(tracks, user["navidrome_user"], user["navidrome_password"])
    album_groups, solo_tracks = group_missing_by_album(
        missing, user["navidrome_user"], user["navidrome_password"],
        progress=lambda d, t: _task_update(task_id, done=d, total=t,
                                           current="Grouping Spotify tracks"))
    groups = [_review_group_from_album_download(g, "spotify") for g in album_groups]
    if solo_tracks:
        groups.append({
            "id": hashlib.sha1(f"spotify|solo|{playlist}".encode("utf-8")).hexdigest()[:16],
            "group_type": "tracks",
            "artist": "Loose tracks",
            "album": "Spotify tracks",
            "artist_key": "loose tracks",
            "album_key": "spotify tracks",
            "created_at": time.time(),
            "updated_at": time.time(),
            "status": "needs_review",
            "merge_mode": "logical",
            "match_mode": "auto",
            "canonical_album_id": "",
            "canonical_mbid": "",
            "albums": [],
            "missing_tracks": [{**t, "decision": "pending"} for t in solo_tracks],
            "present": 0,
            "total": len(solo_tracks),
            "last_action": "spotify",
            "messages": [],
        })
    _store_review_groups(groups, f"Spotify scan found {len(missing)} missing track(s)")
    _task_finish(task_id, f"Found {len(missing)} missing track(s)")

def _search_task(task_id: str, query: str) -> None:
    _task_update(task_id, current=query)
    folders = slskd_run_search(query, 0)
    sid = uuid.uuid4().hex[:12]
    with _review_lock:
        _review_state.setdefault("searches", {})[sid] = {
            "id": sid,
            "query": query,
            "created_at": time.time(),
            "folders": folders,
        }
    _save_review_state()
    _task_finish(task_id, f"Found {len(folders)} folder result(s)")

def _album_download_task(task_id: str, release_mbid: str, artist: str,
                         album: str, total_tracks: int, chosen: dict = None,
                         rgid: str = "", quality: str = "") -> None:
    _task_update(task_id, current=f"{artist} - {album}", total=total_tracks)
    # The rgid rides along purely so a successful placement can flip the library
    # index row for this release-group (see _index_mark_release_present) — the
    # transfer machinery below is all keyed on the release, not the group.
    _album_fill_set(release_mbid, "searching", artist=artist, album=album,
                    taskId=task_id, total=total_tracks, rgid=rgid,
                    quality=quality or "")
    # An empty quality is "whatever Source preferences say"; the scope covers the
    # search and the enqueue, and the group below carries it for the failover.
    with _quality_preference(quality):
        _album_download_search_and_enqueue(
            task_id, release_mbid, artist, album, total_tracks, chosen, quality)

def _album_download_search_and_enqueue(task_id: str, release_mbid: str, artist: str,
                                       album: str, total_tracks: int, chosen: dict,
                                       quality: str) -> None:
    # Same stats dict the gaps path fills, so a fruitless search can say *why*
    # rather than "No album source found on slskd" — which reads as a bug right
    # after a progress line that counted a hundred peers. This is also what
    # decides whether offering the MP3 opt-in is worth the user's time.
    stats: dict = {}
    folders = slskd_search_album_folders(artist, album, total_tracks, stats=stats)
    if not folders:
        reason = _no_source_reason(stats)
        mp3_would_help = MP3_FALLBACK_EXT in (stats.get("rejected_formats") or ())
        _album_fill_set(release_mbid, "failed", reason=reason,
                        mp3WouldHelp=bool(mp3_would_help))
        _task_finish(task_id, error=f"No usable source: {reason}",
                     no_source_reason=reason, mp3_would_help=bool(mp3_would_help))
        return
    # Honor a manually-picked source: float the matching peer/folder to the front
    # (the rest stay as alt_sources for auto-failover). Falls back to the best
    # ranked folder if that peer has since vanished.
    if chosen and chosen.get("username"):
        cu = chosen.get("username")
        cf = chosen.get("folder") or ""
        idx = next((i for i, f in enumerate(folders)
                    if f.get("username") == cu
                    and (not cf or cf in (f.get("folder"), f.get("raw_folder")))), -1)
        if idx > 0:
            folders = [folders[idx]] + folders[:idx] + folders[idx + 1:]
    fd = folders[0]
    full = slskd_expand_directory(fd["username"], fd, fd["files"][0] if fd.get("files") else {})
    ok, total, ag_id = slskd_enqueue_folder(
        fd["username"], full or fd.get("files", []),
        token=_default_web_user().get("telegram_token", ""),
        chat_id=str(_default_web_user().get("chat_id", "")),
        label=album, release_mbid=release_mbid, alt_sources=folders[1:6],
        artist=artist, album=album, quality=quality)
    # The task ends here — at "accepted by slskd", not "in your library". The
    # transfer, the placement and the Navidrome scan are all still ahead, and
    # they report through the fill-status ledger instead (see _finalize_group).
    if ok:
        _album_fill_set(release_mbid, "queued", groupId=ag_id or "",
                        done=0, total=total, failed=0,
                        source=fd.get("username", ""))
    else:
        _album_fill_set(release_mbid, "failed",
                        reason="slskd accepted none of the album's files")
    _task_finish(task_id, f"Queued {ok}/{total} file(s)" if ok else "Could not queue album",
                 "" if ok else "Could not queue album")

def _artist_discography_task(task_id: str, artist_mbid: str, artist_name: str,
                             user: dict, nd_artist_id: str = "",
                             skip_library: bool = False) -> None:
    def _progress(done, total, artist="", album=""):
        _task_update(task_id, done=done, total=total,
                     current=f"{artist} - {album}".strip(" -"))
    result = build_artist_discography(
        artist_mbid, artist_name,
        user["navidrome_user"], user["navidrome_password"], progress=_progress,
        skip_library=skip_library)
    if result["review_groups"]:
        # Union, never replace: a single-artist scan must not wipe the gaps
        # accumulated from other artists / full-library scans.
        _union_review_groups(result["review_groups"])
    _index_store_artist(result, nd_artist_id)
    counts = {}
    for r in result["releases"]:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    summary = (f"{counts.get('complete', 0)} complete, "
               f"{counts.get('incomplete', 0)} incomplete, "
               f"{counts.get('missing', 0)} missing, "
               f"{counts.get('untagged', 0)} untagged")
    _task_finish(task_id, summary, result=result)

def _task_cancelled(task_id: str) -> bool:
    """Read one task's status directly.

    Deliberately not _review_snapshot(): its memo is keyed on the Flask request
    scope, and a scan worker thread has none — so every check deep-copied the
    entire review state. This runs in inner loops.
    """
    with _review_lock:
        task = (_review_state.get("tasks", {}) or {}).get(task_id) or {}
        return task.get("status") == "cancelled"

def _library_index_task(task_id: str, user: dict) -> None:
    """
    Bulk-build the library index: scan every Navidrome artist's discography
    and store it. Resumable/idempotent — artists with a fresh index entry are
    skipped, so a cancelled or crashed run fast-forwards on the next start.
    MB pacing rides mbz_get's built-in 1 req/s limiter; repeat runs are
    mostly cache hits.
    """
    rows = _artist_index_rows()
    _task_update(task_id, total=len(rows), cancellable=True)
    # Fetched once for the whole run. Each build_artist_discography used to page
    # the entire album catalog for itself, so this loop re-read the library
    # once per artist.
    all_albums = nd_get_all_albums(user["navidrome_user"], user["navidrome_password"])
    indexed = skipped = unresolved = 0
    for done, row in enumerate(rows, 1):
        if _task_cancelled(task_id):
            _task_update(task_id, done=done - 1,
                         summary=f"Cancelled — {indexed} indexed, {skipped} skipped")
            return
        name = row.get("name", "")
        _task_update(task_id, done=done - 1, current=name)
        stored = _index_get_artist(row.get("mbid", ""), row.get("id", ""))
        if stored and not stored["stale"]:
            skipped += 1
            continue
        mbid = row.get("mbid", "")
        if not mbid:
            candidates = mbz_search_artists(name, 1)
            mbid = (candidates[0].get("mbid", "") if candidates else "")
        if not mbid:
            # Record the miss under the Navidrome id so the next run doesn't
            # burn a MusicBrainz search on it again until it goes stale.
            _index_store_artist({"artist_mbid": "", "artist_name": name,
                                 "releases": []}, nd_artist_id=row.get("id", ""))
            unresolved += 1
            continue
        try:
            result = build_artist_discography(
                mbid, name, user["navidrome_user"], user["navidrome_password"],
                all_albums=all_albums)
            if result["review_groups"]:
                _union_review_groups(result["review_groups"])
            _index_store_artist(result, nd_artist_id=row.get("id", ""))
            indexed += 1
        except Exception as e:
            print(f"  index build failed for {name}: {e}")
            unresolved += 1
    _task_finish(task_id, f"Indexed {indexed} artist(s), "
                          f"{skipped} already fresh, {unresolved} unresolved")

def _retag_task(task_id: str, group_id: str) -> None:
    with _review_lock:
        group = _find_review_group(group_id)
        if not group:
            _task_finish(task_id, error="Group not found")
            return
        _task_update(task_id, total=len(preview_group_retag(group).get("folders", [])),
                     current=f"{group.get('artist')} - {group.get('album')}")
        result = apply_group_retag(group)
    _save_review_state()
    _task_finish(task_id, result.get("output", "")[-800:],
                 "" if result.get("ok") else result.get("output", "Retag failed"))

def _download_group_task(task_id: str, group_id: str) -> None:
    # Same slskd search as _group_sources_task, so it goes through the same
    # path — the lock must not be held across it.
    with _source_search_claim(group_id) as claimed:
        if not claimed:
            _task_finish(task_id, error="A source search is already running for this album")
            return
        result = _run_group_source_search(task_id, group_id)
    if result.get("ok"):
        result = {"ok": True, "message": result.get("message", "Sources ready"),
                  "cached": result.get("cached")}
    if not result.get("cached"):
        _save_review_state()
    _task_finish(task_id, result.get("message", ""),
                 "" if result.get("ok") else result.get("message", "Download failed"))

def _run_group_source_search(task_id: str, group_id: str, force: bool = False) -> dict:
    """Search slskd for a group's sources without holding _review_lock across it.

    The lock is process-wide and every /api/summary, /api/gaps and
    /api/gaps/<id> poll wants it, so holding it for the tens of seconds slskd
    takes froze the entire UI — including the poll that was meant to show this
    search's own progress. The shape is the one api_gap_detail already uses:
    read what's needed under the lock, do the slow part outside it, then
    re-find the group and merge.
    """
    with _review_lock:
        group = _find_review_group(group_id)
        if not group:
            return {"ok": False, "message": "Group not found"}
        if not force:
            reusable = _reusable_source_results(group)
            if reusable:
                age = int(time.time() - (reusable.get("created_at") or 0))
                n = len(reusable.get("folders") or [])
                return {"ok": True, "cached": True, "sources": n,
                        "message": f"Using the {n} source(s) found {age}s ago"}
        approved = len([t for t in group.get("missing_tracks", [])
                        if t.get("decision") == "approved"])
        _task_update(task_id, total=approved,
                     current=f"{group.get('artist')} - {group.get('album')}")
        plan = _group_source_plan(group)
        generation = _group_generation(group)
    if not plan.get("ok"):
        return plan

    # persist=False: this ticks once a second and the write is the whole review
    # state. TASK_PERSIST_INT still flushes it periodically, and /api/gaps/<id>
    # reads the in-memory task, which is what the screen is watching.
    stats = {}
    folders = _search_group_sources(
        plan, progress=lambda msg: _task_update(task_id, persist=False, current=msg),
        stats=stats)

    with _review_lock:
        group = _find_review_group(group_id)
        if not group:
            return {"ok": False, "message": "Group no longer exists"}
        if _group_generation(group) != generation:
            # A rescan changed the tracklist while we searched. Coverage and the
            # ranking's expected track count were both computed against the old
            # one, so these results would mis-rank sources rather than merely be
            # stale. Throw them away and say so.
            return {"ok": False,
                    "message": "Album changed during the search — search again"}
        if _gap_status_for_group(group) == "downloading":
            # Someone started a transfer off the previous results; replacing
            # source_results underneath it would strand the running download.
            return {"ok": False,
                    "message": "A download started during the search — search again when it finishes"}
        return _apply_group_sources(group, plan, folders, stats)

def _group_sources_task(task_id: str, group_id: str, force: bool = False) -> None:
    with _source_search_claim(group_id) as claimed:
        if not claimed:
            _task_finish(task_id, error="A source search is already running for this album")
            return
        result = _run_group_source_search(task_id, group_id, force)
    if not result.get("cached"):
        _save_review_state()
    _task_finish(task_id, result.get("message", ""),
                 "" if result.get("ok") else result.get("message", "Source search failed"))

def _gap_auto_task(task_id: str, group_id: str) -> None:
    """Headless 'Auto-select best source': search slskd, apply the existing
    ranking, and enqueue the top folder in one shot. This is _group_sources_task
    followed by the enqueue the UI would otherwise wait for a human to click;
    the ranking is the source search's own, so auto and manual picks agree.
    """
    with _review_lock:
        group = _find_review_group(group_id)
        if not group:
            _task_finish(task_id, error="Group not found")
            return
        label = f"{group.get('artist')} - {group.get('album')}"
        _approve_pending_missing_tracks(group)
        _task_update(task_id, current=f"Searching sources: {label}")
    with _source_search_claim(group_id) as claimed:
        if not claimed:
            _task_finish(task_id, error="A source search is already running for this album")
            return
        result = _run_group_source_search(task_id, group_id)
    if not result.get("ok"):
        _save_review_state()
        _task_finish(task_id, error=result.get("message", "Source search failed"))
        return

    # Walk the ranked list rather than trusting rank #1: search-time peer state
    # goes stale within seconds, so an immediate rejection is normal and means
    # "try the next one", not "give up".
    with _review_lock:
        group = _find_review_group(group_id)
        folders = ((group or {}).get("source_results") or {}).get("folders") or []
    if not folders:
        _task_finish(task_id, error="No sources found on slskd")
        return
    _task_update(task_id, total=len(folders))
    last_msg = ""
    for idx in range(len(folders)):
        _task_update(task_id, done=idx,
                     current=f"Trying source #{idx + 1} of {len(folders)}")
        # One attempt per lock hold: each enqueue talks to slskd, and the
        # whole UI reads through this lock.
        with _review_lock:
            group = _find_review_group(group_id)
            if not group:
                _task_finish(task_id, error="Group disappeared mid-search")
                return
            enq = _enqueue_group_source(group, idx)
        if enq.get("ok"):
            _save_review_state()
            _save_state()
            _task_finish(task_id,
                         f"Queued from source #{idx + 1}: "
                         f"{enq.get('message', 'source queued')}")
            return
        last_msg = enq.get("message", "enqueue failed")
    _save_review_state()
    _task_finish(task_id, error=f"Every source rejected the request ({last_msg})")

def _live_transfer_track_indexes(group_id: str) -> set:
    """Review-track indexes of this group that have a real in-flight transfer.

    A track can be marked queued/downloading with nothing behind it — a source
    that never enqueued it, or a restart that dropped pending_downloads. Those
    phantoms used to pin the group at "downloading" forever, which made every
    retry entry point answer alreadyActive and left the gap permanently unfillable.
    """
    if not group_id:
        return set()
    out = set()
    for info in list(pending_downloads.values()):
        if info.get("review_group_id") != group_id:
            continue
        idx = info.get("review_track_index")
        if idx is None:
            continue
        try:
            out.add(int(idx))
        except (TypeError, ValueError):
            pass
    return out

def _review_group_next_action(group: dict) -> dict:
    tracks = group.get("missing_tracks", []) or []
    decisions = [t.get("decision", "pending") for t in tracks]
    has_failed = any(d == "failed" for d in decisions) or group.get("status") in ("retag_failed", "failed")
    if not group.get("canonical_album_id") and group.get("albums"):
        return {"bucket": "needs_canonical", "label": "Pick canonical album",
                "action": "Review albums"}
    if has_failed:
        return {"bucket": "failed", "label": "Review failure", "action": "Open group"}
    pending_matches = [m for m in group.get("match_items") or []
                       if (m or {}).get("status") != "imported"]
    if any(d == "needs_match" for d in decisions) or pending_matches:
        return {"bucket": "needs_match", "label": "Match downloaded files",
                "action": "Match manually"}
    inflight = [i for i, d in enumerate(decisions) if d in ("queued", "downloading")]
    if inflight:
        live = _live_transfer_track_indexes(group.get("id", ""))
        if any(i in live for i in inflight):
            return {"bucket": "downloading", "label": "Downloading", "action": "Open downloads"}
        # Nothing is actually transferring — fall through so the group is
        # offered for retry rather than claiming to be busy forever.
    if any(d == "downloaded" for d in decisions):
        return {"bucket": "downloaded", "label": "Import or verify", "action": "Open downloads"}
    if any(d in ("approved", "source_pending") for d in decisions):
        return {"bucket": "source_pending", "label": "Choose reliable sources",
                "action": "Choose sources"}
    if tracks and all(d in ("placed", "verified", "skipped") for d in decisions):
        return {"bucket": "completed", "label": "Placed in library",
                "action": "Rescan"}
    if tracks:
        return {"bucket": "has_missing", "label": "Review missing tracks",
                "action": "Review tracks"}
    return {"bucket": "completed", "label": "Verify library", "action": "Rescan"}

def _action_center_snapshot(review_state: dict = None, downloads: dict = None,
                            diagnostics: dict = None) -> dict:
    review_state = review_state or _review_snapshot()
    downloads = downloads or {"bot_pending": [], "album_groups": [], "review": []}
    diagnostics = diagnostics or {"checks": []}
    buckets = {
        "albums_needing_review": [],
        "tracks_needing_source": [],
        "active_downloads": [],
        "downloads_needing_match": [],
        "downloaded_not_merged": [],
        "failed": [],
        "completed": [],
    }
    group_rows = []
    for group in review_state.get("groups", []) or []:
        next_action = _review_group_next_action(group)
        row = {
            "id": group.get("id", ""),
            "artist": group.get("artist", ""),
            "album": group.get("album", ""),
            "status": group.get("status", ""),
            "next_action": next_action,
            "missing": len(group.get("missing_tracks", []) or []),
        }
        group_rows.append(row)
        bucket = next_action["bucket"]
        if bucket in ("needs_canonical", "has_missing"):
            buckets["albums_needing_review"].append(row)
        elif bucket == "source_pending":
            buckets["tracks_needing_source"].append(row)
        elif bucket == "downloading":
            buckets["active_downloads"].append(row)
        elif bucket == "needs_match":
            buckets["downloads_needing_match"].append(row)
        elif bucket == "failed":
            buckets["failed"].append(row)
        elif bucket == "completed":
            buckets["completed"].append(row)

    for row in downloads.get("album_groups", []) or []:
        buckets["active_downloads"].append(row)
    for row in downloads.get("bot_pending", []) or []:
        state = (row.get("state") or "").lower()
        if any(word in state for word in ("fail", "abort", "reject", "cancel")):
            buckets["failed"].append(row)
        else:
            buckets["active_downloads"].append(row)
    for row in downloads.get("review", []) or []:
        if row.get("import_state") == "downloaded_not_imported":
            buckets["downloaded_not_merged"].append(row)
        elif row.get("status") == "needs_match":
            buckets["downloads_needing_match"].append(row)
        elif row.get("status") == "failed":
            buckets["failed"].append(row)

    failed_checks = [c for c in diagnostics.get("checks", []) if not c.get("ok")]
    tasks = sorted((review_state.get("tasks") or {}).values(),
                   key=lambda t: t.get("started_at", 0), reverse=True)
    last_error = next((t.get("error") for t in tasks if t.get("error")), "")
    last_scan = next((t for t in tasks if "scan" in (t.get("kind", "") or "")), {})
    last_import = next((t for t in tasks if "import" in (t.get("kind", "") or "")), {})
    cards = [
        {"key": "albums_needing_review", "title": "Albums needing review",
         "count": len(buckets["albums_needing_review"]), "action": "Review albums",
         "tab": "Album Review"},
        {"key": "tracks_needing_source", "title": "Approved tracks need sources",
         "count": len(buckets["tracks_needing_source"]), "action": "Choose sources",
         "tab": "Album Review"},
        {"key": "active_downloads", "title": "Active downloads",
         "count": len(buckets["active_downloads"]), "action": "Open downloads",
         "tab": "Downloads"},
        {"key": "downloads_needing_match", "title": "Downloads need manual match",
         "count": len(buckets["downloads_needing_match"]), "action": "Match manually",
         "tab": "Downloads"},
        {"key": "downloaded_not_merged", "title": "Downloaded but not merged",
         "count": len(buckets["downloaded_not_merged"]), "action": "Fix import",
         "tab": "Downloads"},
        {"key": "failed", "title": "Failed imports or downloads",
         "count": len(buckets["failed"]), "action": "Review failures",
         "tab": "Downloads"},
        {"key": "diagnostics_failures", "title": "Diagnostics failures",
         "count": len(failed_checks), "action": "Fix config", "tab": "Diagnostics"},
    ]
    return {
        "cards": cards,
        "buckets": buckets,
        "groups": group_rows,
        "diagnostics_failures": failed_checks,
        "summary": {
            "last_scan": last_scan,
            "last_import": last_import,
            "last_error": last_error,
            "review_message": review_state.get("message", ""),
            "review_updated_at": review_state.get("updated_at", 0),
        },
    }

def _redact_secrets(value) -> str:
    text = str(value or "")
    secrets = [
        SLSKD_API_KEY, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, MBZ_CONTACT,
        *(u.get("telegram_token", "") for u in USERS),
        *(u.get("navidrome_password", "") for u in USERS),
    ]
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "[redacted]")
    text = re.sub(r"(?i)(token|password|secret|api[_-]?key)(=|:)[^\s,;}]+",
                  r"\1\2[redacted]", text)
    text = re.sub(r"https?://(?!localhost|127\.0\.0\.1)([^/:@\s]+)",
                  lambda m: m.group(0).replace(m.group(1), "[redacted-host]"),
                  text)
    return text

def _redacted_url(url: str) -> str:
    return _redact_secrets(url)

def _config_card(key: str, title: str, values: dict, ok: bool,
                 fix: str = "") -> dict:
    return {
        "key": key,
        "title": title,
        "ok": bool(ok),
        "values": {k: _redact_secrets(v) for k, v in values.items()},
        "fix": fix,
    }

def _settings_cards(user: dict = None, diagnostics: list = None) -> dict:
    user = user or (_default_web_user() or {})
    checks = diagnostics or []
    failed_text = " ".join(label for ok, label, _detail in checks if not ok).lower()
    examples = {
        "docker_run": (
            "docker run -d --name lb-bot --restart unless-stopped -p 8899:8899 "
            "-e LB_BOT_WEB=1 -e LB_BOT_WEB_HOST=0.0.0.0 -e LB_BOT_WEB_PORT=8899 "
            "-e LB_BOT_MUSIC_DIR=/music "
            "-e SLSKD_DOWNLOAD_DIR=/downloads "
            "-v /mnt/user/appdata/lb-bot:/bot-config:rw "
            "-v /mnt/user/appdata/slskd/downloads:/downloads:rw "
            "-v /mnt/user/Music:/music:rw lb-bot:local"
        ),
        "paths": "/music, /downloads, /config, /bot-config, web port 8899",
    }
    cards = [
        _config_card("navidrome", "Navidrome", {
            "url": _redacted_url(NAVIDROME_URL),
            "user": user.get("navidrome_user", ""),
            "password": "[redacted]" if user.get("navidrome_password") else "not set",
        }, "navidrome" not in failed_text,
            "Check NAVIDROME_URL and keep credentials in environment variables."),
        _config_card("slskd", "slskd", {
            "url": _redacted_url(SLSKD_URL),
            "api_key": "[redacted]" if SLSKD_API_KEY else "not set",
            "downloads": SLSKD_DOWNLOAD_DIR,
        }, "slskd" not in failed_text,
            "Set SLSKD_URL, SLSKD_API_KEY, and mount slskd downloads at /downloads."),
        _config_card("listenbrainz", "ListenBrainz", {
            "user": user.get("listenbrainz_user", ""),
            "playlists": ", ".join((user.get("playlist_sources") or {}).values()),
        }, bool(user.get("listenbrainz_user")),
            "Set listenbrainz_user and playlist_sources for the dashboard user."),
        _config_card("spotify", "Spotify", {
            "client_id": "[redacted]" if SPOTIFY_CLIENT_ID else "not set",
            "client_secret": "[redacted]" if SPOTIFY_CLIENT_SECRET else "not set",
        }, bool(SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET),
            "Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET for playlist imports."),
        _config_card("musicbrainz", "MusicBrainz contact", {
            "contact": "[redacted]" if MBZ_CONTACT else "not set",
        }, bool(MBZ_CONTACT),
            "Set MBZ_CONTACT to a contact email required by MusicBrainz."),
        _config_card("docker_mounts", "Docker mounts", {
            "music": MUSIC_LIBRARY_PATH,
            "downloads": SLSKD_DOWNLOAD_DIR,
            "config": "/config",
            "bot_config": os.path.dirname(REVIEW_FILE) or "/bot-config",
        }, MUSIC_LIBRARY_PATH.startswith("/") and SLSKD_DOWNLOAD_DIR.startswith("/"),
            "Mount /music, /downloads, /config, and /bot-config into the container."),
        _config_card("web", "Web dashboard", {
            "host": WEB_UI_HOST,
            "port": WEB_UI_PORT,
            "build": WEB_BUILD,
        }, bool(WEB_UI_ENABLED),
            "Set LB_BOT_WEB=1 and publish port 8899 on your LAN host."),
    ]
    return {"cards": cards, "examples": examples}

def _audio_files_in_folder(folder: str, limit: int = 80,
                           recursive: bool = True) -> list:
    """Audio files under `folder`.

    `recursive=False` is for callers that mean "this album's own directory" —
    the duplicate scan, where an unbounded walk let an album whose tracks sit at
    an artist folder attach hundreds of unrelated files as duplicates.
    """
    rows = []
    if not folder or not os.path.isdir(folder):
        return rows
    walk = os.walk(folder)
    if not recursive:
        walk = [next(walk, (folder, [], []))]
    for root, _dirs, files in walk:
        for name in files:
            if _file_ext(name) not in _placeable_formats():
                continue
            path = os.path.join(root, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            rows.append({
                "name": name,
                "path": path,
                "relpath": os.path.relpath(path, folder),
                "size": size,
            })
            if len(rows) >= limit:
                return rows
    return rows

def _audio_file_tags(path: str) -> dict:
    try:
        from mutagen import File as MutagenFile
    except Exception:
        return {}
    try:
        audio = MutagenFile(path, easy=True)
    except Exception:
        return {}
    if not audio:
        return {}
    tags = {}
    raw_tags = getattr(audio, "tags", {}) or {}
    for key, value in raw_tags.items():
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        tags[str(key).lower()] = str(value or "")
    info = getattr(audio, "info", None)
    if info and getattr(info, "length", None):
        tags["duration"] = float(info.length)
    return tags

def _audio_signature(path: str) -> dict:
    """Identity of the *audio* in a file, independent of what its tags claim.

    Placement rewrites a file's title and recording MBID to whatever slot it
    decided the file belongs to, so tags cannot answer "is this the same song?"
    after the fact — which is why a fill that placed the wrong file produced a
    duplicate no tag-based scan could see.

    FLAC carries `md5_signature`, an MD5 of the *unencoded* audio, in its
    StreamInfo block: exact identity, free, no decoding. `_audio_file_tags` opens
    files with `easy=True`, which hides StreamInfo, hence a second reader here.

    Everything is best-effort — `{}` on any failure, and every caller must
    degrade to its old behaviour rather than block on a missing signature.
    """
    try:
        from mutagen import File as MutagenFile
    except Exception:
        return {}
    try:
        audio = MutagenFile(path)
    except Exception:
        return {}
    if not audio:
        return {}
    sig = {}
    info = getattr(audio, "info", None)
    # Some encoders leave the md5 field zeroed; that is "unknown", not a value.
    md5 = getattr(info, "md5_signature", 0) or 0
    if md5:
        sig["md5"] = f"{md5:032x}"
    for attr, key in (("total_samples", "samples"), ("length", "length"),
                      ("sample_rate", "sample_rate"), ("channels", "channels")):
        val = getattr(info, attr, None)
        if val:
            sig[key] = val
    tags = _audio_file_tags(path)
    sig["own_title"] = tags.get("title", "")
    sig["own_title_key"] = _match_key(sig["own_title"])
    sig["own_mbids"] = _tag_recording_mbids(tags)
    try:
        sig["size"] = os.path.getsize(path)
    except OSError:
        sig["size"] = 0
    return sig

def _same_audio_exact(a: dict, b: dict) -> bool:
    """Are these two signatures certainly the same audio?

    Deliberately strict, because this gates a *refusal to place a file*: only an
    md5 match or an identical sample count at the same rate counts. A
    duration+size heuristic would pair two different 3:47 tracks off the same CD
    and turn a working fill into a no-op — that looser comparison belongs in the
    duplicate *report*, where the user confirms before anything is deleted.
    """
    if not a or not b:
        return False
    if a.get("md5") and b.get("md5"):
        return a["md5"] == b["md5"]
    if (a.get("samples") and b.get("samples")
            and a.get("sample_rate") and a.get("sample_rate") == b.get("sample_rate")):
        return a["samples"] == b["samples"]
    return False

def _file_signature_cached(path: str) -> dict:
    """`_audio_signature` with the SQLite index in front of it.

    A library-wide duplicate scan reads every file in every album; without this
    that is a stream-header read per file on every scan. Keyed on (size, mtime),
    which is exactly what changes when a file is replaced or retagged.
    """
    if not path:
        return {}
    try:
        st = os.stat(path)
    except OSError:
        return {}
    size, mtime = st.st_size, st.st_mtime
    try:
        with _index_lock:
            row = _index_db().execute(
                "SELECT * FROM file_signatures WHERE path = ?", (path,)).fetchone()
    except Exception:
        row = None
    if row and int(row["size"] or 0) == size and abs(float(row["mtime"] or 0) - mtime) < 1:
        return {
            "md5": row["md5"] or "",
            "samples": int(row["samples"] or 0),
            "length": float(row["length"] or 0),
            "sample_rate": int(row["sample_rate"] or 0),
            "channels": int(row["channels"] or 0),
            "own_title_key": row["title_key"] or "",
            "own_mbids": set(json.loads(row["mbids"] or "[]")),
            "size": size,
        }
    sig = _audio_signature(path)
    if not sig:
        return {}
    try:
        with _index_lock:
            conn = _index_db()
            conn.execute(
                "INSERT OR REPLACE INTO file_signatures "
                "(path, size, mtime, md5, samples, length, sample_rate, channels, "
                " title_key, mbids, scanned_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (path, size, mtime, sig.get("md5", ""), int(sig.get("samples") or 0),
                 float(sig.get("length") or 0), int(sig.get("sample_rate") or 0),
                 int(sig.get("channels") or 0), sig.get("own_title_key", ""),
                 json.dumps(sorted(sig.get("own_mbids") or [])), time.time()))
            # Batched: a first full-library scan writes one row per file, and a
            # commit each would be tens of thousands of fsyncs. Losing the tail of
            # a batch to a crash only costs a recompute.
            _sig_cache_pending[0] += 1
            if _sig_cache_pending[0] >= 200:
                conn.commit()
                _sig_cache_pending[0] = 0
    except Exception as e:
        # A read-only index (the root-owned-file case in CLAUDE.md) must not stop
        # the scan — it just means no caching.
        print(f"  file signature cache write failed: {e}")
    return sig

_sig_cache_pending = [0]

def _flush_file_signatures() -> None:
    """Commit whatever `_file_signature_cached` has batched up."""
    try:
        with _index_lock:
            if _sig_cache_pending[0]:
                _index_db().commit()
                _sig_cache_pending[0] = 0
    except Exception as e:
        print(f"  file signature cache flush failed: {e}")

def _tag_recording_mbids(tags: dict) -> set:
    mbids = set()
    for key in (
        "musicbrainz_trackid",
        "musicbrainz recording id",
        "musicbrainz_recordingid",
        "musicbrainz_releasetrackid",
        "musicbrainz release track id",
    ):
        val = tags.get(key, "")
        if val:
            mbids.add(val)
    return mbids

def _tag_track_number(tags: dict) -> int:
    raw = tags.get("tracknumber", "") or tags.get("track", "")
    m = re.search(r"\d+", str(raw))
    return int(m.group(0)) if m else 0

def _manual_match_candidate(release_mbid: str, label: str = "", artist: str = "") -> dict:
    if not release_mbid:
        return {}
    detail = mbz_release_display(release_mbid) or {}
    tracks = mbz_release_tracks(release_mbid) or []
    return {
        "release_mbid": release_mbid,
        "label": label or detail.get("release_title", "") or release_mbid,
        "title": detail.get("release_title", ""),
        "artist": artist,
        "cover_url": detail.get("cover_url", ""),
        "date": detail.get("release_date", ""),
        "country": detail.get("release_country", ""),
        "format": detail.get("release_format", ""),
        "type": detail.get("release_type", ""),
        "packaging": detail.get("release_packaging", ""),
        "track_count": len(tracks),
        "tracks": tracks,
    }

def _release_candidates_for_download(path: str = "", artist: str = "",
                                     album: str = "", group: dict = None,
                                     rec: dict = None, query: str = "",
                                     limit: int = 6) -> list:
    candidates = []
    seen = set()

    def add_mbid(mbid: str, label: str = "", artist: str = ""):
        if not mbid or mbid in seen:
            return
        seen.add(mbid)
        try:
            cand = _manual_match_candidate(mbid, label, artist)
        except Exception as e:
            print(f"  release candidate lookup failed for {mbid}: {e}")
            cand = {}
        if cand:
            candidates.append(cand)

    rec = rec or {}
    group = group or {}
    add_mbid(rec.get("release_mbid", ""), "download target", rec.get("artist", ""))
    add_mbid(group.get("canonical_mbid", ""), "album review canonical", group.get("artist", ""))

    folder = os.path.basename(os.path.normpath(path or rec.get("album_dir", "")))
    search_texts = []
    if query:
        search_texts.append(query)
    if artist and album:
        search_texts.append(f'{artist} "{album}"')
    if group.get("artist") and group.get("album"):
        search_texts.append(f'{group.get("artist")} "{group.get("album")}"')
    if rec.get("artist") and rec.get("album"):
        search_texts.append(f'{rec.get("artist")} "{rec.get("album")}"')
    if folder:
        search_texts.append(folder.replace("_", " "))

    searched = set()
    for text in search_texts:
        q = " ".join(str(text).split())
        if not q or q.lower() in searched:
            continue
        searched.add(q.lower())
        try:
            rows = mbz_search_release_groups(q, limit=limit)
        except Exception as e:
            print(f"  release-group search failed for {q}: {e}")
            rows = []
        for row in rows:
            if len(candidates) >= limit:
                break
            try:
                resolved = mbz_resolve_album(row.get("rgid", ""))
            except Exception as e:
                print(f"  release-group resolve failed for {row.get('rgid', '')}: {e}")
                continue
            add_mbid(resolved.get("release_mbid", ""),
                     f"{row.get('artist', '')} - {row.get('title', '')}".strip(" -"),
                     row.get("artist", ""))
        if len(candidates) >= limit:
            break
    return candidates[:limit]

# Only these two clear the bar for a one-click "Confirm & file"; anything weaker
# is shown as a suggestion but routes the user through the release picker.
_CONFIRMABLE_CONFIDENCE = ("exact", "likely")
# Auto-identify pass: _release_candidates_for_download costs 1 search + ~4 MB
# calls per candidate at 1 req/sec, so 3 candidates is ~13s per folder. The
# on-demand picker still uses the default 6.
_IDENTIFY_CANDIDATE_LIMIT = 3

def _identity_candidate_score(cand: dict, file_count: int, text: str) -> float:
    """Token overlap between a candidate release and the folder's own text
    (embedded artist/album tags when present, else the folder name), nudged by
    how well the release's track count matches what's on disk."""
    want = _tokenize_for_match(text)
    if not want:
        return 0.0
    have = _tokenize_for_match(f"{cand.get('artist', '')} {cand.get('title', '')}")
    if not have:
        return 0.0
    score = len(want & have) / len(want)
    tracks = int(cand.get("track_count") or 0)
    if tracks and file_count:
        if tracks == file_count:
            score += 0.25
        elif abs(tracks - file_count) <= 1:
            score += 0.10
    return score

def _identify_download_folder(folder: dict) -> dict:
    """
    Resolve a download folder to a MusicBrainz release without any bot-side
    context, for albums grabbed through slskd by hand.

    Tiers, strongest first: an embedded release MBID is unambiguous and costs
    two cached lookups; a release-group MBID needs one resolve; otherwise fall
    back to searching MB by the embedded artist/album tags, then by the folder
    name. Returns an identity record (see _folder_identity); release_mbid is ""
    when nothing clears the bar.
    """
    path = folder.get("path", "")
    file_count = int(folder.get("file_count") or 0)
    base = {
        "path": path,
        "fingerprint": _folder_fingerprint(folder),
        "ts": time.time(),
        "tier": "none",
        "confidence": "unknown",
        "release_mbid": "",
        "artist": "",
        "album": "",
        "cover_url": "",
        "track_count": 0,
        "file_count": file_count,
        "score": 0.0,
        "error": "",
    }

    def _release_artist(release_mbid: str, fallback: str = "") -> str:
        """_manual_match_candidate only echoes back the artist it was handed, so
        an MBID-tagged folder has no artist to show unless we go get one."""
        if fallback:
            return fallback
        try:
            raw = mbz_get(f"release/{release_mbid}", {"inc": "artist-credits"})
            return _artist_credit_str(raw.get("artist-credit", []))
        except Exception:
            return ""

    def _resolved(cand: dict) -> bool:
        """_manual_match_candidate returns a truthy dict even when the release
        lookup failed -- title empty, no tracks, label falling back to the raw
        MBID. Treating that as a match would file an album against a release we
        know nothing about, so require the lookup to have actually landed."""
        return bool(cand.get("release_mbid") and cand.get("title")
                    and cand.get("track_count"))

    def _from_candidate(cand: dict, tier: str, confidence: str,
                        score: float = 0.0) -> dict:
        tracks = int(cand.get("track_count") or 0)
        return {**base, "tier": tier, "confidence": confidence,
                "release_mbid": cand.get("release_mbid", ""),
                "artist": cand.get("artist", "") or "",
                "album": cand.get("title", "") or cand.get("label", "") or "",
                "cover_url": cand.get("cover_url", ""),
                "track_count": tracks, "score": round(score, 3)}

    try:
        sample = _audio_files_in_folder(path, 3)
    except Exception as e:
        return {**base, "error": f"Could not read folder: {e}"}
    if not sample:
        return {**base, "error": "No audio files in folder"}

    tags = []
    for f in sample:
        try:
            tags.append(_audio_file_tags(f["path"]))
        except Exception:
            pass

    def _first_tag(*keys) -> str:
        for t in tags:
            for k in keys:
                v = (t.get(k) or "").strip()
                if v:
                    return v
        return ""

    try:
        # Tier 1: embedded release MBID -> exact release, no guessing.
        album_mbid = _first_tag("musicbrainz_albumid")
        if album_mbid:
            cand = _manual_match_candidate(
                album_mbid, artist=_release_artist(
                    album_mbid, _first_tag("albumartist", "artist")))
            if _resolved(cand):
                tracks = int(cand.get("track_count") or 0)
                return _from_candidate(
                    cand, "tag_mbid",
                    "exact" if tracks == file_count else "likely", 1.0)
            if cand.get("release_mbid"):
                return {**base, "tier": "tag_mbid",
                        "error": "MusicBrainz lookup for the embedded release "
                                 "MBID returned nothing — try again later"}

        # Tier 1b: embedded release-group MBID -> its canonical release.
        rgid = _first_tag("musicbrainz_releasegroupid")
        if rgid:
            resolved = mbz_resolve_album(rgid)
            cand = _manual_match_candidate(
                resolved.get("release_mbid", ""),
                artist=resolved.get("artist", "")
                or _first_tag("albumartist", "artist"))
            if _resolved(cand):
                return _from_candidate(cand, "tag_rgid", "likely", 0.9)

        # Tiers 2-3: search MB by embedded artist/album tags, else folder name.
        artist = _first_tag("albumartist", "artist")
        album = _first_tag("album")
        if artist and album:
            tier, text = "tag_text", f"{artist} {album}"
            cands = _release_candidates_for_download(
                path=path, artist=artist, album=album,
                limit=_IDENTIFY_CANDIDATE_LIMIT)
        else:
            tier, text = "folder_name", folder.get("name", "")
            cands = _release_candidates_for_download(
                path=path, limit=_IDENTIFY_CANDIDATE_LIMIT)

        best, best_score = {}, 0.0
        for cand in cands:
            if not _resolved(cand):
                continue
            score = _identity_candidate_score(cand, file_count, text)
            if score > best_score:
                best, best_score = cand, score
        if not best:
            return {**base, "tier": tier}
        tracks = int(best.get("track_count") or 0)
        if best_score >= 0.8 and tracks and tracks == file_count:
            confidence = "likely"
        elif best_score >= 0.6:
            confidence = "possible"
        else:
            return {**base, "tier": tier, "score": round(best_score, 3)}
        return _from_candidate(best, tier, confidence, best_score)
    except Exception as e:
        return {**base, "error": str(e)}

def _identify_folders_task(task_id: str, force: bool = False) -> None:
    """
    Identify every download folder that still needs placing. User-initiated
    only: MusicBrainz allows 1 req/sec, so a sweep is slow by construction and
    must never run inside a Flask request thread.
    """
    if not _identify_lock.acquire(blocking=False):
        _task_finish(task_id, "Identify already running")
        return
    try:
        folders = _download_folders_cached(force=True)
        # Drop identities for folders that are gone so the cache can't grow
        # without bound across the state file.
        live = {f["path"] for f in folders}
        for path in [p for p in _folder_identity if p not in live]:
            _folder_identity.pop(path, None)

        todo = []
        for folder in folders:
            path = folder.get("path", "")
            if path in _imported_folders or path in _dismissed_folders:
                continue
            if not force:
                # Only a cached *match* is worth keeping. A folder that came
                # back unmatched (or errored -- MusicBrainz was down) is worth
                # another look; this button is the only way to retry it.
                cached = _folder_identity_for(folder)
                if cached.get("release_mbid") and not cached.get("error"):
                    continue
            todo.append(folder)

        _task_update(task_id, total=len(todo), done=0)
        identified = 0
        for i, folder in enumerate(todo):
            _task_update(task_id, done=i, current=folder.get("name", ""))
            ident = _identify_download_folder(folder)
            _folder_identity[ident["path"]] = ident
            if ident.get("release_mbid"):
                identified += 1
            _web_log(
                f"identify: {folder.get('name', '')} -> "
                f"{ident.get('tier', 'none')}/{ident.get('confidence', 'unknown')}"
                + (f" {ident['artist']} - {ident['album']}"
                   if ident.get("release_mbid") else "")
                + (f" ({ident['error']})" if ident.get("error") else ""),
                "musicbrainz")
            if i % 5 == 4:
                _save_state()
        _save_state()
        _task_update(task_id, done=len(todo), current="")
        _task_finish(
            task_id,
            f"Identified {identified} of {len(todo)} folder(s)" if todo
            else "Nothing to identify")
    finally:
        _identify_lock.release()

def _manual_match_payload(group: dict, rec: dict, candidates: list = None) -> dict:
    downloaded = _audio_files_in_folder(rec.get("album_dir", ""))
    release_ids = []
    for release_mbid in (
            rec.get("release_mbid", ""), group.get("canonical_mbid", "")):
        if release_mbid and release_mbid not in release_ids:
            release_ids.append(release_mbid)
    if candidates is None:
        candidates = _release_candidates_for_download(
            rec.get("album_dir", ""), group=group, rec=rec)
        if not candidates:
            candidates = [_manual_match_candidate(mbid) for mbid in release_ids]
    candidates = [c for c in candidates if c]
    target_tracks = candidates[0].get("tracks", []) if candidates else []
    file_keys = [_match_key(f.get("name", "")) for f in downloaded]
    comparison = []
    for track in target_tracks:
        key = _match_key(track.get("title", ""))
        match = next((downloaded[i] for i, fkey in enumerate(file_keys)
                      if key and key in fkey), None)
        comparison.append({
            "position": track.get("position", ""),
            "title": track.get("title", ""),
            "recording_mbid": track.get("mbid", ""),
            "file": match.get("relpath", "") if match else "",
            "matched": bool(match),
        })
    return {
        "id": rec.get("id", ""),
        "group_id": group.get("id", ""),
        "label": rec.get("label", ""),
        "album_dir": rec.get("album_dir", ""),
        "status": rec.get("status", ""),
        "downloaded_files": downloaded,
        "candidates": candidates,
        "comparison": comparison,
        "actions": ["import_selected_release", "import_as_is",
                    "search_another_release", "skip_for_now"],
    }

def _candidate_download_folders_for_group(group: dict, extra_paths: list = None) -> list:
    paths = []

    def add(path: str):
        if path and os.path.isdir(path):
            ap = os.path.abspath(path)
            if ap not in paths:
                paths.append(ap)

    for path in extra_paths or []:
        add(path)
    for item in group.get("match_items", []) or []:
        add(item.get("album_dir", ""))
    for track in group.get("missing_tracks", []) or []:
        local = track.get("local_path", "")
        if local:
            add(local if os.path.isdir(local) else os.path.dirname(local))

    album_key = _match_key(group.get("album", ""))
    artist_key = _match_key(group.get("artist", ""))
    if os.path.isdir(SLSKD_DOWNLOAD_DIR):
        for folder in _list_download_folders():
            name_key = _match_key(folder.get("name", ""))
            if (album_key and album_key in name_key) or (artist_key and artist_key in name_key):
                add(folder.get("path", ""))
    return paths

def _reconcile_group_downloaded_files(group: dict, folder_paths: list = None) -> dict:
    job = _create_or_update_repair_job_from_group(group)
    folders = _candidate_download_folders_for_group(group, folder_paths)
    files = []
    seen = set()
    for folder in folders:
        for row in _audio_files_in_folder(folder, limit=500):
            path = row.get("path", "")
            if path in seen:
                continue
            seen.add(path)
            files.append({
                **row,
                "filename": row.get("relpath") or row.get("name", ""),
                "folder": folder,
            })
    used = set()
    matched = []
    siblings = _release_track_titles(group)
    eligible = {"pending", "approved", "source_pending", "queued",
                "downloading", "failed", "needs_match", "downloaded"}
    for idx, track in enumerate(group.get("missing_tracks", []) or []):
        if track.get("decision", "pending") not in eligible:
            continue
        best = _best_file_for_missing_track(track, files, used, siblings=siblings)
        if not best:
            continue
        used.add(best.get("filename", ""))
        track["decision"] = "downloaded"
        track["local_path"] = best.get("path", "")
        track["matched_relpath"] = best.get("relpath") or best.get("name", "")
        track["download_state"] = "reconciled"
        track["downloaded_at"] = time.time()
        track.pop("download_error", None)
        if job:
            tid = track.get("repair_track_id") or _repair_track_id(group.get("id", ""), idx, track)
            jt = _repair_track_by_id(job, tid)
            if jt:
                jt["status"] = "downloaded"
                jt["local_path"] = best.get("path", "")
                jt["matched_relpath"] = best.get("relpath") or best.get("name", "")
                jt["updated_at"] = time.time()
                jt.pop("error", None)
            pool_path = best.get("folder", "") or os.path.dirname(best.get("path", ""))
            if pool_path and pool_path not in [p.get("path") for p in job.setdefault("source_pools", [])]:
                job["source_pools"].append({
                    "id": hashlib.sha1(pool_path.encode("utf-8")).hexdigest()[:16],
                    "path": pool_path,
                    "status": "downloaded",
                    "created_at": time.time(),
                })
        matched.append({
            "index": idx,
            "title": track.get("title", ""),
            "path": best.get("path", ""),
            "relpath": best.get("relpath") or best.get("name", ""),
        })
    if matched:
        group["status"] = "review"
        group["updated_at"] = time.time()
        if job:
            job["status"] = _repair_job_status_from_tracks(job)
            _repair_job_touch(job, {"kind": "reconciled", "message": f"Reconciled {len(matched)} downloaded file(s)"})
    return {
        "ok": True,
        "folders": folders,
        "file_count": len(files),
        "matched": matched,
        "matched_count": len(matched),
    }

def _downloads_snapshot() -> dict:
    active = slskd_get_all_downloads()
    active_by_key = {}
    for dl in active:
        username = dl.get("_username", "")
        filename = dl.get("filename", "")
        active_by_key[(username, filename)] = dl
        base = filename.replace("\\", "/").rstrip("/").split("/")[-1].lower()
        active_by_key[(username, base)] = dl
    bot_pending = []
    for (username, filename), info in pending_downloads.items():
        track = info.get("track") or {}
        base = filename.replace("\\", "/").rstrip("/").split("/")[-1].lower()
        dl = active_by_key.get((username, filename)) or active_by_key.get((username, base)) or {}
        state = dl.get("state") or info.get("latest_state", "Queued")
        percent = _slskd_transfer_percent(dl) if dl else int(info.get("percent") or 0)
        bot_pending.append({
            "username": username,
            "filename": filename,
            "artist": track.get("artist", ""),
            "title": track.get("title", ""),
            "album_group_id": info.get("album_group_id", ""),
            "review_group_id": info.get("review_group_id", ""),
            "review_track_index": info.get("review_track_index"),
            "match_mode": info.get("match_mode", "auto"),
            "target_release_mbid": info.get("target_release_mbid", ""),
            "state": state,
            "percent": percent,
            "queued_at": info.get("queued_at", 0),
            "local_path": info.get("local_path", ""),
            "error": info.get("error", ""),
        })
    album_groups = []
    for gid, ag in pending_album_groups.items():
        total = int(ag.get("total") or 0)
        done = int(ag.get("completed") or 0) + int(ag.get("failed") or 0)
        album_groups.append({
            "id": gid,
            "label": ag.get("label", ""),
            "done": done,
            "total": total,
            "failed": ag.get("failed", 0),
            "percent": int(done * 100 / total) if total else 0,
            "source_user": ag.get("source_user", ""),
            "review_group_id": ag.get("review_group_id", ""),
            "match_mode": ag.get("match_mode", "auto"),
            "target_release_mbid": ag.get("target_release_mbid", ""),
            "track_indexes": ag.get("review_track_indexes", []),
        })
    review = [{"id": rid, **r} for rid, r in _albums.items()
              if r.get("status") in ("needs_review", "needs_match", "failed")]
    slskd_rows = []
    for dl in active:
        slskd_rows.append({
            "username": dl.get("_username", ""),
            "filename": dl.get("filename", ""),
            "state": dl.get("state", ""),
            "percent": _slskd_transfer_percent(dl),
            "size": dl.get("size") or dl.get("fileSize") or 0,
            "speed": dl.get("averageSpeed") or dl.get("speed") or 0,
        })
    return {
        "slskd": slskd_rows,
        "bot_pending": bot_pending,
        "album_groups": album_groups,
        "review": review,
        "repair_jobs": [
            {
                "id": job.get("id", ""),
                "group_id": job.get("group_id", ""),
                "artist": job.get("artist", ""),
                "album": job.get("album", ""),
                "status": job.get("status", ""),
                "downloads": len(job.get("downloads", []) or []),
                "matches": len(job.get("file_matches", []) or []),
            }
            for job in repair_jobs.values()
            if job.get("status") != "archived"
        ],
    }

# ---------------------------------------------------------------------------
# Screen-shaped API views for the redesigned web UI
# (Fill gaps / Downloads / Library / System). These are read-only projections
# over the same stores the Telegram bot uses — never mutate here.
# ---------------------------------------------------------------------------

_ND_ALBUM_INDEX = {"albums": [], "ts": 0.0}
_ND_ALBUM_INDEX_TTL = 300  # seconds; /api/library and /api/summary share it
_ND_ALBUM_INDEX_LOCK = threading.Lock()
# Serializes the fetch itself, not just the cache read/write. The fetch pages
# the whole album catalog; with only the cache guarded, every thread that
# arrived on a cold cache started its own copy of it.
_ND_ALBUM_INDEX_FETCH_LOCK = threading.Lock()

_DL_FOLDERS_CACHE = {"folders": [], "ts": 0.0}
_DL_FOLDERS_TTL = 30  # os.walk over the downloads dir — don't do it per poll

_HEALTH_CACHE = {"checks": [], "raw": [], "ts": 0.0}
_HEALTH_TTL = 60
_health_cache_lock = threading.Lock()

def _nd_album_index(force: bool = False) -> list:
    """All Navidrome albums, cached with a short TTL."""
    user = _default_web_user()
    if not user:
        return []
    now = time.time()
    with _ND_ALBUM_INDEX_LOCK:
        if (not force and _ND_ALBUM_INDEX["albums"]
                and now - _ND_ALBUM_INDEX["ts"] < _ND_ALBUM_INDEX_TTL):
            return _ND_ALBUM_INDEX["albums"]
    with _ND_ALBUM_INDEX_FETCH_LOCK:
        # Someone else may have filled it while we queued here.
        now = time.time()
        with _ND_ALBUM_INDEX_LOCK:
            if (not force and _ND_ALBUM_INDEX["albums"]
                    and now - _ND_ALBUM_INDEX["ts"] < _ND_ALBUM_INDEX_TTL):
                return _ND_ALBUM_INDEX["albums"]
        albums = nd_get_all_albums(user["navidrome_user"], user["navidrome_password"])
        if albums:
            with _ND_ALBUM_INDEX_LOCK:
                _ND_ALBUM_INDEX["albums"] = albums
                _ND_ALBUM_INDEX["ts"] = now
    return albums or _ND_ALBUM_INDEX["albums"]

_ND_ARTIST_INDEX = {"artists": [], "ts": 0.0}
_ND_ARTIST_INDEX_LOCK = threading.Lock()

def _nd_artist_index(force: bool = False) -> list:
    """All Navidrome artists, cached on the same cadence as the album index."""
    user = _default_web_user()
    if not user:
        return []
    now = time.time()
    with _ND_ARTIST_INDEX_LOCK:
        if (not force and _ND_ARTIST_INDEX["artists"]
                and now - _ND_ARTIST_INDEX["ts"] < _ND_ALBUM_INDEX_TTL):
            return _ND_ARTIST_INDEX["artists"]
    artists = nd_get_all_artists(user["navidrome_user"], user["navidrome_password"])
    if artists:
        with _ND_ARTIST_INDEX_LOCK:
            _ND_ARTIST_INDEX["artists"] = artists
            _ND_ARTIST_INDEX["ts"] = now
    return artists or _ND_ARTIST_INDEX["artists"]

def _artist_index_rows() -> list:
    """
    View rows for GET /api/artists: {id, name, releaseCount, plays?, coverUrl?,
    mbid?}. plays sums the per-album playCount from the album index (an
    OpenSubsonic field) and is omitted entirely when Navidrome reports no play
    data. mbid passes through Navidrome's musicBrainzId when the library tags
    carry one, so the Artist screen can skip a MusicBrainz text lookup.
    """
    artists = _nd_artist_index()
    plays_by_artist: dict = {}
    cover_by_artist: dict = {}   # artistId -> (playCount, albumId)
    have_play_data = False
    for a in _nd_album_index():
        aid = a.get("artistId") or ""
        if not aid:
            continue
        if "playCount" in a:
            have_play_data = True
            plays_by_artist[aid] = (plays_by_artist.get(aid, 0)
                                    + int(a.get("playCount") or 0))
        # Tile art: the artist's most-played album, else the first one seen.
        cand = (int(a.get("playCount") or 0), a.get("id", ""))
        cur = cover_by_artist.get(aid)
        if cur is None or cand[0] > cur[0]:
            cover_by_artist[aid] = cand
    rows = []
    for art in artists:
        aid = art.get("id", "")
        row = {
            "id": aid,
            "name": art.get("name", ""),
            "releaseCount": int(art.get("albumCount", 0) or 0),
        }
        cover = cover_by_artist.get(aid)
        if cover and cover[1]:
            row["coverUrl"] = f"/api/cover/{cover[1]}?size=160"
        if have_play_data:
            row["plays"] = plays_by_artist.get(aid, 0)
        mbid = art.get("musicBrainzId") or ""
        if mbid:
            row["mbid"] = mbid
        rows.append(row)
    rows.sort(key=lambda r: r["name"].lower())
    return rows

# Status preference when picking one album to represent a similar artist:
# something whole first, then something you partly own, then something the
# scan couldn't verify. A `missing` release-group is never a candidate — the
# shelf is "more of what you already have", not a shopping list.
_SIMILAR_ALBUM_STATUS_RANK = {"complete": 0, "incomplete": 1, "untagged": 2}

def _similar_albums_for_artist(artist_mbid: str, artist_name: str,
                               exclude_rgid: str = "", limit: int = 6) -> list:
    """One album per similar artist, drawn from the user's own library.

    The attribution rule the artist-level shelf already follows applies here:
    every entry is justified by the artist being viewed, never presented as a
    free-floating popularity claim. Each row carries the artist it came from
    so the UI can say so.

    Only indexed artists can appear, because routing to the entry needs the
    release-group id the discography index holds. An unindexed library is not
    an error — the shelf simply doesn't render.
    """
    similar = similar_artists(artist_mbid, artist_name, limit=40)
    if not similar:
        return []

    library = _artist_index_rows()
    by_mbid = {a["mbid"]: a for a in library if a.get("mbid")}
    by_name = {a["name"].strip().lower(): a for a in library if a.get("name")}

    out = []
    for cand in similar:
        if len(out) >= limit:
            break
        owned = by_mbid.get(cand["mbid"]) or by_name.get((cand["name"] or "").strip().lower())
        if not owned:
            continue
        indexed = _index_get_artist(cand["mbid"], owned["id"])
        if not indexed:
            continue
        pick = None
        for rel in indexed.get("releases", []):
            if rel.get("status") not in _SIMILAR_ALBUM_STATUS_RANK:
                continue
            if exclude_rgid and rel.get("rgid") == exclude_rgid:
                continue
            try:
                year = int(rel.get("year") or 0)
            except (TypeError, ValueError):
                year = 0
            key = (_SIMILAR_ALBUM_STATUS_RANK[rel["status"]], -year)
            if pick is None or key < pick[0]:
                pick = (key, rel)
        if not pick:
            continue
        rel = pick[1]
        out.append({
            "artistId": owned["id"],
            "artist": owned["name"],
            "rgid": rel.get("rgid", ""),
            "title": rel.get("title", ""),
            "year": rel.get("year", ""),
            "status": rel.get("status", ""),
            "coverUrl": caa_front_url(rel.get("rgid", ""), 250),
            "because": artist_name,
            "sources": sorted(set(cand.get("sources") or [])),
        })
    return out

def _download_folders_cached(force: bool = False) -> list:
    now = time.time()
    if force or now - _DL_FOLDERS_CACHE["ts"] > _DL_FOLDERS_TTL:
        _DL_FOLDERS_CACHE["folders"] = _list_download_folders()
        _DL_FOLDERS_CACHE["ts"] = now
    return _DL_FOLDERS_CACHE["folders"]

def _folder_fingerprint(folder: dict) -> str:
    """Cheap change-detector for a cached folder identity: file count plus the
    folder's mtime. One stat call, so it is safe on the /api/transfers poll
    path. A still-downloading folder changes mtime as files land, which is what
    we want -- its identity is re-derived once it settles."""
    try:
        mtime = int(os.path.getmtime(folder.get("path", "")))
    except OSError:
        mtime = 0
    return f"{folder.get('file_count', 0)}:{mtime}"

def _folder_identity_for(folder: dict) -> dict:
    """Cached identity for a folder, or {} when absent or stale."""
    ident = _folder_identity.get(folder.get("path", "")) or {}
    return ident if ident.get("fingerprint") == _folder_fingerprint(folder) else {}

def _folder_entry_for_path(path: str) -> dict:
    """The {name, path, file_count, formats} entry for a download folder,
    synthesized when the path isn't one of SLSKD_DOWNLOAD_DIR's immediate
    subdirs (the telegram flows can target any folder)."""
    for f in _download_folders_cached():
        if f.get("path") == path:
            return f
    return {"name": os.path.basename(os.path.normpath(path)), "path": path,
            "file_count": len(_audio_files_in_folder(path, 200)), "formats": ""}

def _identity_for_path(path: str) -> dict:
    """Cached-or-computed identity for a folder path. Blocking (MusicBrainz at
    1 req/sec) -- call from a thread, never a request handler."""
    folder = _folder_entry_for_path(path)
    ident = _folder_identity_for(folder)
    if ident and not ident.get("error"):
        return ident
    ident = _identify_download_folder(folder)
    _folder_identity[ident["path"]] = ident
    _save_state()
    return ident

# The mock's per-album state machine: ready → picking → downloading → failed,
# with "complete" for albums whose gaps are gone.
_GAP_STATUS_BY_BUCKET = {
    "downloading": "downloading",
    "downloaded": "downloading",   # import/verify still in flight → "working"
    "failed": "failed",
    "needs_match": "picking",
    "source_pending": "picking",
    "has_missing": "ready",
    "needs_canonical": "ready",
    "completed": "complete",
}
_GAP_FILTER_STATUSES = {
    "needs": {"ready", "picking", "failed"},
    "working": {"downloading"},
    "done": {"complete"},
}
_TRACK_STATE_BY_DECISION = {
    "pending": "missing",
    "approved": "picked",
    "source_pending": "picked",
    "queued": "queued",
    "downloading": "downloading",
    "downloaded": "downloaded",
    "needs_match": "downloaded",
    "failed": "failed",
    "cancelled": "failed",
    "skipped": "skipped",
    "placed": "done",
    "verified": "done",
}

def _gap_status_for_group(group: dict) -> str:
    bucket = _review_group_next_action(group).get("bucket", "")
    return _GAP_STATUS_BY_BUCKET.get(bucket, "ready")

def rescan_group_from_disk(group_id: str) -> dict:
    """Re-check one album, without a full library scan.

    Two passes, because Navidrome alone can be behind in two different ways.
    First `refresh_group_albums_from_navidrome` re-reads the album from
    Navidrome (force=True, so its own index cache doesn't answer). Then the
    album's folder is walked for audio files Navidrome has not indexed yet —
    the case this endpoint exists for, a file dropped in by hand or placed a
    moment ago — and those rows are folded into the record before the counts
    are recomputed. `_album_tracks_with_disk` is the same narrow walk the
    duplicate-file report uses: non-recursive, never the library root, and
    skipped for a directory holding implausibly more audio than the album has.

    Navidrome is still the oracle in the end, so a scan is nudged whenever the
    walk turned up something Navidrome had not seen. Returns the counts the UI
    reconciles against; raises nothing — a Navidrome hiccup just means the
    refresh half is skipped and the disk half still runs.
    """
    refreshed = refresh_group_albums_from_navidrome(group_id)

    # Copy the records out, walk the folders outside the lock, merge back in.
    # The walk is up to 400 stats per folder and `_review_lock` is process-wide
    # and wanted by every 2s poll — holding it across the walk freezes the page.
    with _review_lock:
        group = _find_review_group(group_id)
        if not group:
            return {}
        records = json.loads(json.dumps(group.get("albums") or []))

    found_on_disk = 0
    merged_by_album = {}
    for record in records:
        known = {_file_dedupe_key(t["path"])
                 for t in (record.get("tracks") or []) if t.get("path")}
        merged = _album_tracks_with_disk(record)
        new = [t for t in merged if _file_dedupe_key(t.get("path", "")) not in known]
        if new:
            found_on_disk += len(new)
            merged_by_album[record.get("id", "")] = merged

    with _review_lock:
        group = _find_review_group(group_id)
        if not group:
            return {}
        for record in group.get("albums") or []:
            merged = merged_by_album.get(record.get("id", ""))
            if merged:
                record["tracks"] = merged
        refresh_group_missing(group)
        snapshot = json.loads(json.dumps(group))
    payload = {
        "ok": True,
        "refreshedFromNavidrome": refreshed,
        "foundOnDisk": found_on_disk,
        "present": snapshot.get("present", 0),
        "total": snapshot.get("total", 0),
        "status": _gap_status_for_group(snapshot),
        "gap": _gap_detail_view(snapshot),
    }
    if found_on_disk:
        # Files we can see and Navidrome can't will keep reappearing as
        # "only on disk" until it indexes them, so ask it to.
        user = _default_web_user()
        if user:
            try:
                nd_start_scan(user["navidrome_user"], user["navidrome_password"], False)
            except Exception as e:
                print(f"  album rescan: Navidrome scan nudge failed: {e}")
    return payload

def _nd_album_artist_map() -> dict:
    """albumId -> {artistId, artistMbid} from the Navidrome indexes, so gap items
    can deep-link to the artist page (the Artist screen routes by nd artist id)."""
    artist_mbid_by_id = {a.get("id", ""): (a.get("musicBrainzId") or "")
                         for a in _nd_artist_index()}
    out = {}
    for a in _nd_album_index():
        aid = a.get("id", "")
        if not aid:
            continue
        art_id = a.get("artistId", "") or ""
        out[aid] = {"artistId": art_id,
                    "artistMbid": artist_mbid_by_id.get(art_id, "")}
    return out

def _album_view(group: dict, artist_map: dict = None) -> dict:
    album_id = group.get("canonical_album_id", "")
    if artist_map is None:
        artist_map = _nd_album_artist_map()
    art = artist_map.get(album_id, {})
    return {
        "id": group.get("id", ""),
        "albumId": album_id,
        "artist": group.get("artist", ""),
        "artistId": art.get("artistId", ""),
        "artistMbid": art.get("artistMbid", ""),
        "album": group.get("album", ""),
        "present": int(group.get("present") or 0),
        "total": int(group.get("total") or 0),
        # Files the tracklist can't account for — the signal that a fill left a
        # duplicate behind even though the album now reads as complete.
        "extra": int(group.get("extra") or 0),
        "missingCount": len(group.get("missing_tracks") or []),
        "status": _gap_status_for_group(group),
        "coverUrl": f"/api/cover/{album_id}?size=160" if album_id else "",
        "updatedAt": group.get("updated_at", 0),
    }

def _source_view(summary: dict) -> dict:
    """Rename `_source_summary` output into the UI's Source shape."""
    coverage = summary.get("coverage") or {}
    return {
        "id": summary.get("index", 0),
        "peer": summary.get("username", ""),
        "folder": summary.get("folder", ""),
        "format": summary.get("quality", ""),
        "bitrate": (f"{summary['bitrate_kbps']} kbps" if summary.get("bitrate_kbps") else ""),
        "size": (f"{summary['size_mb']} MB" if summary.get("size_mb") else ""),
        "fileCount": summary.get("file_count", 0),
        "speedMbps": summary.get("speed_mbps", 0),
        "queueLength": summary.get("queue_length", 0),
        "freeSlot": bool(summary.get("has_free_upload_slot")),
        "coverage": coverage.get("label", "unknown"),
        "coverageDetail": {
            "haveTracks": coverage.get("matched", 0),
            "totalTracks": coverage.get("total", 0),
            "unmatched": [t.get("title", "") for t in coverage.get("unmatched_tracks", [])],
        },
        # The peer's actual file list, each row tagged with the track it would
        # fill, plus the tracks nothing here covers. This is what lets the user
        # see whether a source is the right album before downloading it.
        "files": summary.get("files", []),
        "filesTruncated": bool(summary.get("files_truncated")),
        "missingTracks": [{"position": t.get("position", ""),
                           "title": t.get("title", "")}
                          for t in coverage.get("unmatched_tracks", [])],
        "flags": [r.get("label", "") for r in summary.get("risk_flags", [])],
        "recommendation": summary.get("recommendation", ""),
        # How much the folder's own name reads as this album. The picker badges
        # it, because for an ambiguous query the peer's whole discography comes
        # back and peer speed is no way to tell them apart.
        "albumMatch": summary.get("album_match", 0),
        "albumMatchOk": bool(summary.get("album_match_ok")),
        "yearInPath": bool(summary.get("year_in_path")),
        "score": summary.get("score", 0),
    }

def _gaps_view(status_filter: str = "") -> dict:
    snap = _review_list_snapshot()
    counts = {"needs": 0, "working": 0, "done": 0, "all": 0}
    wanted = _GAP_FILTER_STATUSES.get(status_filter or "", None)
    artist_map = _nd_album_artist_map()
    searching = _groups_with_running_source_search()
    items = []
    for group in snap.get("groups", []) or []:
        if group.get("hidden"):
            continue
        view = _album_view(group, artist_map)
        # So the rail says which album is mid-search — the whole point of
        # letting a search run while you look at a different album.
        view["searching"] = view["id"] in searching
        counts["all"] += 1
        for key, statuses in _GAP_FILTER_STATUSES.items():
            if view["status"] in statuses:
                counts[key] += 1
        if wanted is not None and view["status"] not in wanted:
            continue
        items.append(view)
    return {
        "items": items,
        "counts": counts,
        "scanStatus": snap.get("status", ""),
        "scanMessage": snap.get("message", ""),
        "scanTask": _active_scan_task_view(snap),
        "updatedAt": snap.get("updated_at", 0),
    }

def _active_scan_task_view(snap: dict = None) -> dict:
    """The running full-library scan-all task ({id,done,total,current}) or None,
    so Fill gaps can show a live progress bar + Cancel regardless of which client
    started it (survives a page refresh)."""
    with _review_scan_lock:
        tid = _active_review_scan_task_id
    if not tid:
        return None
    task = ((snap or _review_snapshot()).get("tasks", {}) or {}).get(tid) or {}
    if task.get("status") != "running":
        return None
    return {
        "id": tid,
        "done": int(task.get("done") or 0),
        "total": int(task.get("total") or 0),
        "current": task.get("current", ""),
    }

def _groups_with_running_source_search() -> set:
    with _review_lock:
        return {t.get("group_id") for t in (_review_state.get("tasks", {}) or {}).values()
                if t.get("kind") == "source-search" and t.get("status") == "running"
                and t.get("group_id")}

def _group_source_task_view(group_id: str) -> dict | None:
    """The most recent source search for this album, running or not.

    A source search is a background task: the POST that starts it returns
    immediately, so without this the screen had nothing to show while slskd took
    30-90s, and a task that ended in "No approved tracks" or "No source found on
    slskd" failed completely silently. The album detail is polled, so reading the
    task here means the answer survives navigating away and back.
    """
    if not group_id:
        return None
    with _review_lock:
        tasks = [t for t in (_review_state.get("tasks", {}) or {}).values()
                 if t.get("group_id") == group_id and t.get("kind") == "source-search"]
        if not tasks:
            return None
        task = max(tasks, key=lambda t: t.get("started_at", 0))
        return {"id": task.get("id", ""),
                "status": task.get("status", ""),
                "label": task.get("label", ""),
                "current": task.get("current", ""),
                "summary": task.get("summary", ""),
                "error": task.get("error", ""),
                "startedAt": task.get("started_at", 0),
                "finishedAt": task.get("finished_at", 0)}

def _gap_detail_view(group: dict, source_page: int = 0) -> dict:
    view = _album_view(group)
    missing = group.get("missing_tracks") or []
    # Index in `missing_tracks` — the same value `review_track_index` carries and
    # the id the per-track endpoints take. Track rows used to leave the wire with
    # no identifier at all, so nothing could be said *about* one track.
    index_of = {id(t): i for i, t in enumerate(missing)}
    by_mbid = {t.get("mbid"): t for t in missing if t.get("mbid")}
    by_title = {(t.get("title") or "").lower().strip(): t for t in missing}
    tracks = []
    for row in _canonical_tracklist_from_group(group):
        m = (by_mbid.get(row.get("recording_mbid"))
             or by_title.get((row.get("title") or "").lower().strip()))
        decision = (m or {}).get("decision", "")
        pick = (m or {}).get("manual_pick") or {}
        tracks.append({
            "index": index_of.get(id(m)) if m is not None else None,
            "recordingMbid": (row.get("recording_mbid", "")
                              or (m or {}).get("recording_mbid", "")
                              or (m or {}).get("mbid", "")),
            "position": row.get("position", 0),
            "title": row.get("title", ""),
            "artist": row.get("artist", ""),
            "state": (_TRACK_STATE_BY_DECISION.get(decision, "missing")
                      if m else "present"),
            "downloadError": (m or {}).get("download_error", ""),
            "manualPick": ({"peer": pick.get("username", ""),
                            "filename": pick.get("filename", "")}
                           if pick else None),
            # Set when placement refused a hand-picked file because its audio is
            # already in the album — the one refusal a manual pick doesn't
            # override on its own.
            "canForcePlace": bool((m or {}).get("can_force_place")),
            "forcePlaceConflict": (m or {}).get("force_place_conflict", ""),
        })
    page = _group_source_page(group, source_page, 10)
    sources = []
    for i, summary in enumerate(page.get("sources", [])):
        src = _source_view(summary)
        src["rank"] = page.get("page", 0) * page.get("per", 10) + i + 1
        src["recommended"] = (src["rank"] == 1)
        sources.append(src)
    fail_msg = next((m for m in reversed(group.get("messages", []) or [])
                     if m.get("kind") in ("error", "blocked_no_source", "download_failed")),
                    None)
    # Tracks the chosen source had no accepted-format file for. This is the
    # observed reason to consider MP3, so the UI can offer the opt-in with a
    # real number instead of a vague suggestion — no extra search needed.
    no_file_count = sum(
        1 for t in missing
        if t.get("decision") == "failed"
        and "no matching file in source" in (t.get("download_error", "") or ""))
    view.update({
        "tracks": tracks,
        "sources": sources,
        "sourcesTotal": page.get("total", 0),
        "sourcesPage": page.get("page", 0),
        "sourcesPages": page.get("pages", 1),
        "sourceQuery": page.get("query", ""),
        # When these results were found, so the UI can say how old the list it
        # is showing is rather than implying it was just searched for.
        "sourcesFoundAt": page.get("created_at", 0),
        # The background source search, so the screen can show it running and
        # report how it ended instead of looking inert.
        "sourceTask": _group_source_task_view(group.get("id", "")),
        "failReason": (fail_msg or {}).get("kind", ""),
        "failDetail": (fail_msg or {}).get("message", ""),
        "allowMp3": bool(group.get("allow_mp3")),
        # Which MusicBrainz release the gap is measured against. It comes from the
        # canonical album's own tag, so a library tagged as a 17-track deluxe
        # reports 17 slots even when the pressing everyone is sharing has 12 —
        # which reads as a bug unless the client can say which edition it means.
        "canonicalMbid": group.get("canonical_mbid", ""),
        "noFileInSourceCount": no_file_count,
        # Why the last search came back empty, and whether relaxing the format
        # policy would have changed that.
        "noSourceReason": group.get("no_source_reason", ""),
        "mp3WouldHelp": bool(group.get("mp3_would_help")),
    })
    return view

def _transfer_state_class(state: str) -> str:
    s = (state or "").lower()
    if any(w in s for w in ("fail", "abort", "reject", "error", "cancel", "timed")):
        return "failed"
    if "succeeded" in s or "completed" in s:
        return "done"
    if "inprogress" in s or "transferring" in s:
        return "active"
    return "queued"

def _transfers_view() -> dict:
    snap = _downloads_snapshot()
    slskd_by_key = {}
    for row in snap.get("slskd", []):
        slskd_by_key[(row.get("username", ""), row.get("filename", ""))] = row
    # review_group_id → {artist, album}, so per-track fills can be grouped under
    # one collapsible album header on the Downloads tab (the track dict itself
    # rarely carries the album name).
    group_meta = {g.get("id"): g for g in _review_snapshot().get("groups", []) or []}
    transfers = []
    for row in snap.get("bot_pending", []):
        key = (row.get("username", ""), row.get("filename", ""))
        raw = slskd_by_key.get(key, {})
        size = int(raw.get("size") or 0)
        speed = int(raw.get("speed") or 0)
        pct = int(row.get("percent") or 0)
        remaining = size * max(0, 100 - pct) / 100 if size else 0
        grp = group_meta.get(row.get("review_group_id") or "") or {}
        album = row.get("album") or grp.get("album", "")
        artist = row.get("artist") or grp.get("artist", "")
        # Track title fallback: explicit row title → the downloaded file's base
        # name (minus extension) → empty. slskd filenames are Windows-style
        # (backslash-separated), so normalize before taking the basename. This is
        # what stops active rows from rendering as "(unnamed)".
        fname = row.get("filename", "") or ""
        base = os.path.basename(fname.replace("\\", "/")) if fname else ""
        base_noext = os.path.splitext(base)[0] if base else ""
        track_title = row.get("title", "") or base_noext
        display_title = " — ".join(x for x in (artist, track_title) if x) or track_title
        display_sub = f"from @{row.get('username', '')}"
        transfers.append({
            "id": hashlib.sha1("|".join(key).encode("utf-8")).hexdigest()[:12],
            "kind": "track",
            # `title` stays the composed "artist — track" line (back-compat); the
            # normalized fields below are what the UI reads first.
            "title": display_title,
            "displayTitle": display_title,
            "trackTitle": track_title,
            "artist": artist,
            "album": album,
            "sub": display_sub,
            "displaySubtitle": display_sub,
            "state": _transfer_state_class(row.get("state", "")),
            "stateDetail": row.get("state", ""),
            "pct": pct,
            "bytesTotal": size,
            "bytesDone": int(size * pct / 100) if size else 0,
            "rate": speed,
            "etaSeconds": int(remaining / speed) if speed else None,
            "error": row.get("error", ""),
            "groupId": row.get("review_group_id", ""),
            "username": row.get("username", ""),
            "filename": row.get("filename", ""),
        })
    for ag in snap.get("album_groups", []):
        failed = int(ag.get("failed") or 0)
        done = int(ag.get("done") or 0)
        total = int(ag.get("total") or 0)
        finished = total and done >= total
        transfers.append({
            "id": ag.get("id", ""),
            "kind": "album",
            "title": ag.get("label", ""),
            "sub": f"from @{ag.get('source_user', '')}",
            # done was previously unreachable for album groups — every
            # finished-but-not-popped group rendered as "downloading" forever.
            "state": ("failed" if finished and failed >= total
                      else "done" if finished
                      else "active"),
            "stateDetail": f"{ag.get('done', 0)}/{ag.get('total', 0)} files"
                           + (f", {failed} failed" if failed else ""),
            "pct": int(ag.get("percent") or 0),
            "groupId": ag.get("review_group_id", ""),
        })
    placements = _needs_placement_view()
    counts = {
        "active": sum(1 for t in transfers if t["state"] == "active"),
        "queued": sum(1 for t in transfers if t["state"] == "queued"),
        "failed": sum(1 for t in transfers if t["state"] == "failed"),
        "done": sum(1 for t in transfers if t["state"] == "done"),
        "needsPlacement": len(placements),
        "unidentified": _unidentified_count(placements),
    }
    # The Downloads tab polls only this endpoint, so carry the identify sweep's
    # progress here rather than making it poll /api/tasks too.
    task = next(
        (t for t in (_review_snapshot().get("tasks", {}) or {}).values()
         if t.get("kind") == "identify" and t.get("status") in ("queued", "running")),
        None)
    identify = {
        "running": bool(task),
        "done": int((task or {}).get("done") or 0),
        "total": int((task or {}).get("total") or 0),
        "current": (task or {}).get("current", "") or "",
    }
    return {"transfers": transfers, "needsPlacement": placements,
            "counts": counts, "identify": identify}

def _unidentified_count(placements: list) -> int:
    """Folders that still need a release picked for them -- what the Identify
    button offers to work on. A folder that was swept but matched nothing still
    counts: the sweep is retryable and MusicBrainz may simply have been down."""
    return sum(1 for p in placements
               if not p.get("match") and not p.get("releaseMbid"))

def _needs_placement_view() -> list:
    folders = _download_folders_cached()
    groups = _review_snapshot().get("groups", []) or []
    rows = []
    for folder in folders:
        # Already-imported and user-dismissed folders are not "waiting to be
        # filed" — only this view filters them; the Import tab and telegram
        # /beets listing deliberately keep showing everything on disk.
        path = folder.get("path", "")
        if path in _imported_folders or path in _dismissed_folders:
            continue
        suggestion = _suggest_review_group_for_folder(folder, groups)
        # A review group is higher-signal than anything we can infer from the
        # folder itself, so it keeps precedence; the cached MusicBrainz identity
        # only speaks for folders the bot never queued.
        ident = {} if suggestion else _folder_identity_for(folder)
        confidence = "likely" if suggestion else (ident.get("confidence") or "unknown")
        file_count = folder.get("file_count", 0)
        track_count = int(ident.get("track_count") or 0)
        rows.append({
            "id": hashlib.sha1(folder["path"].encode("utf-8")).hexdigest()[:12],
            "name": folder.get("name", ""),
            "path": folder.get("path", ""),
            "fileCount": file_count,
            "formats": folder.get("formats", ""),
            "match": suggestion,
            "matchLabel": (f"{suggestion['artist']} — {suggestion['album']}"
                           if suggestion
                           else (f"{ident['artist']} — {ident['album']}"
                                 if ident.get("release_mbid") else "")),
            "confidence": confidence,
            "identified": bool(ident),
            "matchSource": "group" if suggestion else (ident.get("tier") or ""),
            "releaseMbid": ident.get("release_mbid", ""),
            "coverUrl": ident.get("cover_url", ""),
            "identifyError": ident.get("error", ""),
            "canConfirm": bool(suggestion) or bool(
                ident.get("release_mbid")
                and confidence in _CONFIRMABLE_CONFIDENCE),
            "diff": {
                "filesFound": file_count,
                "trackCount": track_count,
                "willFill": (min(file_count, suggestion["missing"]) if suggestion
                             else min(file_count, track_count or file_count)),
            },
        })
    return rows

def _library_view(status_filter: str = "", query: str = "",
                  page: int = 0, per: int = 50) -> dict:
    albums = _nd_album_index()
    groups = _review_snapshot().get("groups", []) or []
    group_by_album_id = {}
    for group in groups:
        if group.get("hidden"):
            continue
        for rec in group.get("albums", []) or []:
            group_by_album_id[rec.get("id", "")] = group
    q = (query or "").lower().strip()
    rows = []
    with_gaps = 0
    for album in albums:
        group = group_by_album_id.get(album.get("id", ""))
        status = _gap_status_for_group(group) if group else "complete"
        if group and group.get("missing_tracks"):
            with_gaps += 1
        if q and q not in (album.get("artist", "") or "").lower() \
                and q not in (album.get("name", "") or "").lower():
            continue
        if status_filter and status_filter != "all":
            if status_filter == "gaps" and not (group and group.get("missing_tracks")):
                continue
            if status_filter == "working" and status != "downloading":
                continue
            if status_filter == "decision" and status != "picking":
                continue
            if status_filter == "failed" and status != "failed":
                continue
            if status_filter == "complete" and status != "complete":
                continue
        rows.append({
            "id": album.get("id", ""),
            "artist": album.get("artist", ""),
            "album": album.get("name", ""),
            "year": album.get("year", 0),
            "trackCount": album.get("songCount", 0),
            "present": int(group.get("present") or 0) if group else album.get("songCount", 0),
            "total": int(group.get("total") or 0) if group else album.get("songCount", 0),
            "status": status,
            "groupId": group.get("id", "") if group else "",
            "coverUrl": f"/api/cover/{album.get('id', '')}?size=96",
        })
    pages = max(1, (len(rows) + per - 1) // per)
    page = max(0, min(int(page or 0), pages - 1))
    start = page * per
    return {
        "items": rows[start:start + per],
        "total": len(rows),
        "page": page,
        "pages": pages,
        "per": per,
        "libraryTotals": {"albums": len(albums), "withGaps": with_gaps},
    }

def _diagnostics_cached(force: bool = False) -> list:
    """Raw (ok, label, detail) checks, behind the shared _HEALTH_TTL cache.

    Every caller of _run_diagnostics that sits on a polled web route goes through
    here: run inline the probes cost up to ~40s of stacked timeouts when a
    service is down, and /api/settings, /api/diagnostics and /api/action-center
    were each paying that on every request.
    """
    with _health_cache_lock:
        now = time.time()
        if force or now - _HEALTH_CACHE["ts"] > _HEALTH_TTL or not _HEALTH_CACHE.get("raw"):
            user = _default_web_user()
            _HEALTH_CACHE["raw"] = _run_diagnostics(user) if user else []
            _HEALTH_CACHE["checks"] = _health_check_rows(_HEALTH_CACHE["raw"])
            _HEALTH_CACHE["ts"] = now
        return list(_HEALTH_CACHE["raw"])

def _health_fix_hint(ok, detail: str) -> str:
    if ok:
        return ""
    if "http" in (detail or "").lower():
        return "Check the matching URL, credentials, and container networking."
    if any(p in (detail or "") for p in ("/music", "/downloads", "/config")):
        return "Verify the Unraid/Docker mount and container path."
    return "Review the related setting and rerun diagnostics."

def _health_check_rows(checks: list) -> list:
    return [
        {
            "id": re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-"),
            "ok": bool(ok),
            "label": label,
            "detail": _redact_secrets(detail),
            "howToFix": _health_fix_hint(ok, detail),
        }
        for ok, label, detail in checks
    ]

def _health_view(force: bool = False) -> dict:
    _diagnostics_cached(force)
    with _health_cache_lock:
        return {"checks": _HEALTH_CACHE["checks"], "lastRun": _HEALTH_CACHE["ts"]}

def _prefs_view() -> dict:
    """Effective source-selection preferences (defaults merged with the
    user's persisted overrides). PUT /api/prefs updates them."""
    p = _effective_prefs()
    return {
        "readOnly": False,
        "ranks": [{"key": fmt, "label": fmt.upper(), "priority": i}
                  for i, fmt in enumerate(p["ranks"])],
        "fallback": p["fallback"],
        "quality": p["quality"],
        "qualityOptions": [
            {"key": "flac-any", "label": "Any lossless",
             "detail": "Prefer FLAC at any bit depth"},
            {"key": "flac-16-44", "label": "CD standard",
             "detail": "Prefer 16-bit / 44.1 kHz FLAC over hi-res"},
            {"key": "highest-bitrate", "label": "Highest bitrate",
             "detail": "Prefer the largest, highest-resolution copy"},
            {"key": "prefer-opus", "label": "Prefer Opus",
             "detail": "Smaller files; falls back to FLAC"},
        ],
        "guards": p["guards"],
        "fixed": {
            "minAvailabilityRatio": 0.5,
            "stallTimeoutSeconds": STALL_TIMEOUT,
            "albumMatchThreshold": ALBUM_MATCH_THRESHOLD,
            "maxSearchPasses": MAX_SEARCH_PASSES,
        },
    }

_COVER_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(STATE_FILE)) or ".", "covers")
_COVER_SIZES = (96, 160, 512)

def _image_mimetype(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"

def _cover_art_bytes(album_id: str, size: int):
    """Fetch + disk-cache Navidrome cover art. Returns (bytes, mimetype);
    (None, "") when the album has no art (negative result is cached too, as an
    empty file, so misses don't hammer Navidrome on every poll)."""
    user = _default_web_user()
    if not user or not album_id:
        return None, ""
    key = hashlib.sha1(f"{album_id}|{size}".encode("utf-8")).hexdigest()
    path = os.path.join(_COVER_CACHE_DIR, key)
    if os.path.isfile(path):
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except Exception:
            data = b""
        return (data, _image_mimetype(data)) if data else (None, "")
    content = None
    ctype = ""
    try:
        r = _http.get(f"{NAVIDROME_URL}/rest/getCoverArt", params={
            **_nd_auth_params(user["navidrome_user"], user["navidrome_password"]),
            "id": album_id, "size": str(size),
        }, timeout=10)
        header = r.headers.get("Content-Type", "")
        if r.ok and header.startswith("image/") and r.content:
            content, ctype = r.content, header
    except Exception as e:
        print(f"  cover art fetch error for {album_id}: {e}")
        return None, ""  # transient — don't negative-cache network failures
    try:
        os.makedirs(_COVER_CACHE_DIR, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(content or b"")
    except Exception as e:
        print(f"  cover cache write error: {e}")
    return content, ctype

def _api_error_payload(code: str, reason: str, detail: str = "",
                       next_source: dict = None, log_tail: list = None) -> dict:
    """Structured error the redesigned UI's failure cards render. Keeps the
    legacy `error` key so existing clients still find a message."""
    return {
        "ok": False,
        "error": reason,
        "code": code,
        "reason": reason,
        "detail": detail,
        "nextSource": next_source,
        "logTail": log_tail if log_tail is not None
                   else [_web_event_line(e) for e in _web_events[-5:]],
    }

# Decisions that mean "this track is still missing and nobody is working on
# it". "failed"/"cancelled" belong here: a dead peer is a fact about the
# source, not about the track. Leaving them out stranded every track of a
# failed download -- the next source pick found nothing approved and died on
# "No approved/source-pending tracks". In-flight (queued/downloading) and
# settled (placed/verified/skipped) decisions are deliberately absent.
#
# "source_pending" belongs here too, and its absence was a trap: a finished
# source search flips every approved track to source_pending, so an album whose
# results were then lost -- a restart drops them (see _load_review_state), the
# TTL lets them age out -- had no approved track left. Every later "Find
# sources" died on "No approved tracks" with nothing on screen to say so, and
# the album sat on "choosing a source" forever with no sources to choose from.
# Re-approving is what a fresh search means; the enqueue path already treats the
# two decisions identically.
_RETRYABLE_DECISIONS = ("pending", "failed", "cancelled", "source_pending")

def _approve_pending_missing_tracks(group: dict) -> int:
    """The redesigned UI has no separate approve step — 'Get N tracks' means
    all still-missing tracks. Flip them to approved in place."""
    changed = 0
    live = None
    for idx, track in enumerate(group.get("missing_tracks", []) or []):
        decision = track.get("decision", "pending")
        if decision not in _RETRYABLE_DECISIONS:
            # queued/downloading with no transfer behind it is a phantom — it
            # will never progress and must be re-approvable. Anything genuinely
            # in flight, and anything settled, is left alone.
            if decision not in ("queued", "downloading"):
                continue
            if live is None:
                live = _live_transfer_track_indexes(group.get("id", ""))
            if idx in live:
                continue
        track["decision"] = "approved"
        track.pop("download_error", None)
        changed += 1
    return changed

def _next_source_view(group: dict, after_index: int) -> dict | None:
    folders = (group.get("source_results") or {}).get("folders") or []
    nxt = after_index + 1
    if 0 <= nxt < len(folders):
        view = _source_view(_source_summary(folders[nxt], nxt, group))
        view["rank"] = nxt + 1
        return view
    return None

def _summary_view() -> dict:
    gaps = _gaps_view()
    transfers = _transfers_view()
    albums = _nd_album_index()
    return {
        "gaps": gaps["counts"],
        "scan": {"status": gaps["scanStatus"], "message": gaps["scanMessage"]},
        "transfers": transfers["counts"],
        "activeTransfers": [
            {"id": t["id"], "title": t["title"], "state": t["state"], "pct": t.get("pct", 0)}
            for t in transfers["transfers"] if t["state"] in ("active", "queued")
        ],
        "library": {
            "albums": len(albums),
            "withGaps": sum(1 for g in _review_list_snapshot().get("groups", []) or []
                            if not g.get("hidden") and g.get("missing_tracks")),
        },
        "ts": time.time(),
    }

def start_web_dashboard() -> None:
    if not WEB_UI_ENABLED:
        return
    try:
        from flask import Flask, jsonify, request, send_from_directory, abort
    except Exception as e:
        print(f"  web UI disabled: Flask import failed: {e}")
        return

    _DIST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "dist")

    app = Flask(__name__, static_folder=None)

    from werkzeug.exceptions import HTTPException

    @app.errorhandler(Exception)
    def _json_errors_for_api(e):
        """Any /api/* failure returns a JSON envelope, never an HTML error page.
        Without this, an unhandled exception (or a 404) hands the SPA a
        `<!doctype html>` body that its `res.json()` can't parse — surfacing as
        the cryptic "Unexpected token '<'" instead of the real error."""
        code = e.code if isinstance(e, HTTPException) else 500
        if request.path.startswith("/api/"):
            if not isinstance(e, HTTPException):
                import traceback
                print(f"  web API error {request.path}: {e}")
                traceback.print_exc()
            msg = getattr(e, "description", None) or str(e) or "Internal error"
            return jsonify({"error": msg}), code
        if isinstance(e, HTTPException):
            return e          # non-API: keep Flask's normal handling (SPA/assets)
        raise e

    _NO_BUILD_PAGE = (
        "<!doctype html><meta charset=utf-8><title>lb-bot</title>"
        "<body style=\"font:15px/1.6 system-ui;max-width:34rem;margin:4rem auto;padding:0 1rem\">"
        "<h1>Web UI not built</h1>"
        "<p>The React SPA is missing from <code>web/dist</code>. Build it with:</p>"
        "<pre><code>cd web &amp;&amp; npm ci &amp;&amp; npm run build</code></pre>"
        "<p>The Docker image builds it automatically; this page means you are "
        "running from a source checkout. The JSON API under <code>/api/</code> "
        "works either way.</p></body>")

    @app.get("/")
    def web_index():
        if os.path.isdir(_DIST_DIR):
            return send_from_directory(_DIST_DIR, "index.html")
        return _NO_BUILD_PAGE

    @app.get("/assets/<path:filename>")
    def web_assets(filename):
        return send_from_directory(os.path.join(_DIST_DIR, "assets"), filename)

    @app.get("/<path:path>")
    def spa_fallback(path):
        if path.startswith("api/"):
            abort(404)
        if os.path.isdir(_DIST_DIR):
            return send_from_directory(_DIST_DIR, "index.html")
        abort(404)

    @app.get("/api/review")
    def api_review():
        snap = _review_snapshot()
        for group in snap.get("groups", []) or []:
            group["next_action"] = _review_group_next_action(group)
        for group in snap.get("duplicate_groups", []) or []:
            group["next_action"] = _review_group_next_action(group)
        return jsonify(snap)

    @app.get("/api/duplicates")
    def api_duplicates():
        """Lean payload for the Library duplicates view: just the duplicate
        scan's groups plus enough scan state to drive the button/progress."""
        snap = _review_snapshot()
        groups = snap.get("duplicate_groups", []) or []
        for group in groups:
            group["next_action"] = _review_group_next_action(group)
        return jsonify({
            "groups": groups,
            "status": snap.get("status", ""),
            "message": snap.get("message", ""),
            "fuzzy": bool(snap.get("fuzzy", False)),
            "deep": bool(snap.get("deep", False)),
        })

    @app.get("/api/duplicate-files")
    def api_duplicate_files():
        """Sets of files inside one album that are the same song, from the last
        duplicate scan. Filtered to what still exists on disk, so a set the user
        has already resolved stops being offered."""
        snap = _review_snapshot()
        stored = snap.get("duplicate_files", []) or []
        sets = []
        # The on-disk filter is the single most opaque failure in this path: if
        # LB_BOT_MUSIC_DIR and Navidrome's reported paths don't reconcile, every
        # file fails os.path.exists and the whole list silently empties. Count
        # what it dropped and keep one example path to make that diagnosable.
        missing_files = 0
        sample_missing = ""
        for dup in stored:
            files = []
            for f in dup.get("files", []):
                path = f.get("path", "")
                if os.path.exists(path):
                    files.append(f)
                else:
                    missing_files += 1
                    sample_missing = sample_missing or path
            if len(files) > 1:
                sets.append({**dup, "files": files})
        return jsonify({
            "sets": sets,
            "status": snap.get("status", ""),
            "message": snap.get("message", ""),
            "scanned_at": snap.get("duplicate_files_scanned_at", 0),
            "stats": {
                **(snap.get("duplicate_file_stats", {}) or {}),
                "sets_stored": len(stored),
                "sets_dropped_missing_files": len(stored) - len(sets),
                "files_missing_on_disk": missing_files,
                "sample_missing_path": sample_missing,
                "music_dir": MUSIC_LIBRARY_PATH,
            },
        })

    @app.get("/api/gaps/<group_id>/duplicate-files")
    def api_group_duplicate_files(group_id):
        """Duplicate files in just this album, resolved live rather than from
        the last scan — this is where a mis-matched fill leaves them, so it has
        to be answerable the moment it happens."""
        group = _find_review_group(group_id)
        if not group:
            return jsonify(_api_error_payload("not_found", "Album not found")), 404
        user = _default_web_user()
        if not user:
            return jsonify(_api_error_payload(
                "no_user", "No Navidrome credentials configured")), 400
        sets = []
        claimed = set()
        seen_folders = set()
        for album in group.get("albums", []) or []:
            record = _album_record(album, user["navidrome_user"],
                                   user["navidrome_password"])
            # A duplicate-album group is, by construction, several album ids that
            # may share one folder. Scanning each of them over the same directory
            # emitted the same files as several sets.
            folders = frozenset(
                os.path.normpath(os.path.dirname(t["path"]))
                for t in (record.get("tracks") or []) if t.get("path"))
            if folders and folders in seen_folders:
                continue
            seen_folders.add(folders)
            sets.extend(_duplicate_file_sets(record, claimed=claimed))
        _flush_file_signatures()
        return jsonify({"sets": sets})

    @app.post("/api/library/delete-file")
    def api_library_delete_file():
        """Delete one file from the music library.

        Requires {"path": ..., "confirm": true}. The path must resolve strictly
        inside LB_BOT_MUSIC_DIR — this is the only endpoint that removes
        anything from the library, so it refuses everything else rather than
        trusting the caller.

        Two further guards, both added after a grouping bug turned this endpoint
        into a way to destroy good files: it refuses when no *other* copy of the
        song survives (409), and it moves the file to LB_BOT_TRASH_DIR rather
        than unlinking it, so a mistake is recoverable through
        POST /api/library/trash/restore.
        """
        data = request.get_json(silent=True) or {}
        path = (data.get("path") or "").strip()
        if not data.get("confirm"):
            return jsonify(_api_error_payload(
                "confirm_required", "Pass {\"confirm\": true} to delete a file")), 400
        if not path:
            return jsonify(_api_error_payload("bad_path", "path is required")), 400
        real = os.path.realpath(path)
        root = os.path.realpath(MUSIC_LIBRARY_PATH)
        if real == root or not _path_inside(real, root):
            return jsonify(_api_error_payload(
                "bad_path", "File is outside the music library")), 400
        if not os.path.isfile(real):
            return jsonify(_api_error_payload(
                "not_found", "File does not exist", real)), 404
        if _path_inside(real, os.path.realpath(LB_BOT_TRASH_DIR)):
            return jsonify(_api_error_payload(
                "bad_path", "That file is already in the trash")), 400
        refusal = _last_copy_refusal(real)
        if refusal:
            return jsonify(_api_error_payload("last_copy", refusal, real)), 409
        # Captured before the move: afterwards there is nothing at `real` to stat.
        ident = _file_dedupe_key(real)
        try:
            entry = _move_to_trash(real)
        except Exception as e:
            return jsonify(_api_error_payload("delete_failed", str(e))), 500
        # Drop it from the stored scan results so the set stops being offered
        # even before the next scan. By identity: the old prune compared
        # os.path.realpath against un-canonicalized stored strings and therefore
        # matched nothing.
        with _review_lock:
            for dup in _review_state.get("duplicate_files", []) or []:
                dup["files"] = [f for f in dup.get("files", [])
                                if _file_dedupe_key(f.get("path", "")) != ident]
            _review_state["duplicate_files"] = [
                d for d in (_review_state.get("duplicate_files", []) or [])
                if len(d.get("files", [])) > 1]
        _save_review_state()
        user = _default_web_user()
        if user:
            nd_start_scan(user["navidrome_user"], user["navidrome_password"])
        _web_log(f"library: moved duplicate file to trash {real} "
                 f"→ {entry.get('trash_path', '')}", "library")
        return jsonify({"ok": True, "deleted": real,
                        "trashPath": entry.get("trash_path", "")})

    @app.get("/api/library/trash")
    def api_library_trash():
        """What is recoverable, newest first."""
        rows = []
        for entry in _trash_manifest_read():
            path = entry.get("trash_path", "")
            if not os.path.isfile(path):
                continue
            rows.append({
                "trashPath": path,
                "originalPath": entry.get("original", ""),
                "deletedAt": entry.get("deleted_at", 0),
                "size": entry.get("size", 0),
                "restorable": bool(entry.get("original")
                                   and not os.path.exists(entry.get("original", ""))),
            })
        rows.sort(key=lambda r: -(r.get("deletedAt") or 0))
        return jsonify({"files": rows, "trashDir": LB_BOT_TRASH_DIR})

    @app.post("/api/library/trash/restore")
    def api_library_trash_restore():
        data = request.get_json(silent=True) or {}
        path = (data.get("path") or "").strip()
        if not path:
            return jsonify(_api_error_payload("bad_path", "path is required")), 400
        try:
            result = _restore_from_trash(path)
        except Exception as e:
            return jsonify(_api_error_payload("restore_failed", str(e))), 500
        if not result.get("ok"):
            code = 409 if result.get("code") == "occupied" else 400
            return jsonify(_api_error_payload(result.get("code", "restore_failed"),
                                              result.get("error", ""))), code
        user = _default_web_user()
        if user:
            nd_start_scan(user["navidrome_user"], user["navidrome_password"])
        _web_log(f"library: restored {result.get('restored', '')} from trash", "library")
        return jsonify(result)

    @app.post("/api/library/trash/empty")
    def api_library_trash_empty():
        """Permanently remove trashed files. `older_than_days` keeps recent ones."""
        data = request.get_json(silent=True) or {}
        if not data.get("confirm"):
            return jsonify(_api_error_payload(
                "confirm_required", "Pass {\"confirm\": true} to empty the trash")), 400
        try:
            older = float(data.get("older_than_days") or 0)
        except (TypeError, ValueError):
            older = 0.0
        cutoff = time.time() - older * 86400 if older else None
        removed = 0
        with _trash_lock:
            rows = _trash_manifest_read()
            keep = []
            for entry in rows:
                if cutoff is not None and (entry.get("deleted_at") or 0) > cutoff:
                    keep.append(entry)
                    continue
                try:
                    os.remove(entry.get("trash_path", ""))
                    removed += 1
                except OSError:
                    pass
            _trash_manifest_write(keep)
        _web_log(f"library: emptied trash ({removed} file(s))", "library")
        return jsonify({"ok": True, "removed": removed})

    @app.get("/api/tasks")
    def api_tasks():
        return jsonify(_review_snapshot().get("tasks", {}))

    @app.get("/api/operations")
    def api_operations():
        return jsonify(_operation_snapshot())

    @app.get("/api/operations/<operation_id>")
    def api_operation(operation_id):
        op = _find_operation(operation_id)
        if not op:
            return jsonify({"error": "Operation not found"}), 404
        return jsonify(op)

    @app.get("/api/tasks/<task_id>")
    def api_task(task_id):
        task = _review_snapshot().get("tasks", {}).get(task_id)
        if not task:
            return jsonify({"error": "Task not found"}), 404
        return jsonify(task)

    @app.post("/api/tasks/<task_id>/cancel")
    def api_task_cancel(task_id):
        op = _operation_create("cancel_task", "Cancelling task")
        _task_update(task_id, status="cancelled", summary="Cancellation requested")
        _operation_finish(op["id"], True, "Cancellation requested", task_id=task_id)
        _save_review_state()
        return jsonify(_with_operation({"ok": True, "task_id": task_id}, op))

    @app.get("/api/downloads")
    def api_downloads():
        return jsonify(_downloads_snapshot())

    @app.get("/api/action-center")
    def api_action_center():
        diag = {"checks": [
            {"ok": ok, "label": label, "detail": _redact_secrets(detail)}
            for ok, label, detail in _diagnostics_cached(_fresh_requested())
        ]}
        return jsonify(_action_center_snapshot(_review_snapshot(),
                                               _downloads_snapshot(), diag))

    # --- Screen-shaped endpoints for the redesigned UI ---------------------

    @app.get("/api/summary")
    def api_summary():
        return jsonify(_summary_view())

    @app.get("/api/gaps")
    def api_gaps():
        return jsonify(_gaps_view(request.args.get("filter", "")))

    @app.get("/api/gaps/<group_id>")
    def api_gap_detail(group_id):
        # Copy the one group under the lock, then build the (heavy) view outside
        # it — _gap_detail_view walks sources and Navidrome indexes, and holding
        # the global review lock for that stalls every other poll on the page.
        with _review_lock:
            group = _find_review_group(group_id)
            if not group:
                return jsonify({"error": "Group not found"}), 404
            group = json.loads(json.dumps(group))
        page = request.args.get("sourcePage", 0)
        try:
            page = int(page)
        except (TypeError, ValueError):
            page = 0
        return jsonify(_gap_detail_view(group, page))

    @app.get("/api/transfers")
    def api_transfers():
        return jsonify(_transfers_view())

    @app.get("/api/library")
    def api_library():
        try:
            page = int(request.args.get("page", 0))
        except (TypeError, ValueError):
            page = 0
        return jsonify(_library_view(
            request.args.get("filter", ""),
            request.args.get("q", ""),
            page))

    def _fresh_requested() -> bool:
        """?fresh=1 bypasses a TTL cache — the explicit "re-check" affordances."""
        return (request.args.get("fresh", "") or "").lower() in ("1", "true", "yes")

    @app.get("/api/system/health")
    def api_system_health():
        return jsonify(_health_view(_fresh_requested()))

    @app.post("/api/system/recheck")
    def api_system_recheck():
        return jsonify(_health_view(force=True))

    @app.get("/api/prefs")
    def api_prefs():
        return jsonify(_prefs_view())

    @app.put("/api/prefs")
    def api_prefs_put():
        """Persist source-selection preference overrides and echo the new
        effective prefs. Accepts any subset of {ranks, fallback, quality,
        guards}."""
        data = request.get_json(silent=True) or {}
        if "ranks" in data:
            ranks = data["ranks"]
            if isinstance(ranks, list) and ranks and all(isinstance(r, str) for r in ranks):
                known = [r.lower() for r in ranks if r.lower() in _PREFS_DEFAULTS["ranks"]]
                if sorted(known) != sorted(_PREFS_DEFAULTS["ranks"]):
                    return jsonify(_api_error_payload(
                        "bad_ranks", "ranks must be a permutation of " +
                        ", ".join(_PREFS_DEFAULTS["ranks"]))), 400
                _prefs_overrides["ranks"] = known
            else:
                return jsonify(_api_error_payload("bad_ranks", "ranks must be a list of format names")), 400
        if "fallback" in data:
            if data["fallback"] not in ("best", "ask", "skip"):
                return jsonify(_api_error_payload("bad_fallback", "fallback must be best | ask | skip")), 400
            _prefs_overrides["fallback"] = data["fallback"]
        if "quality" in data:
            if data["quality"] not in QUALITY_PREFERENCES:
                return jsonify(_api_error_payload(
                    "bad_quality",
                    "quality must be one of " + ", ".join(QUALITY_PREFERENCES))), 400
            _prefs_overrides["quality"] = data["quality"]
        if "guards" in data:
            g = data["guards"]
            if not isinstance(g, dict):
                return jsonify(_api_error_payload("bad_guards", "guards must be an object")), 400
            clean = dict(_prefs_overrides.get("guards", {}))
            for key, default in _PREFS_DEFAULTS["guards"].items():
                if key not in g:
                    continue
                val = g[key]
                try:
                    clean[key] = bool(val) if isinstance(default, bool) else type(default)(val)
                except (TypeError, ValueError):
                    return jsonify(_api_error_payload("bad_guards", f"invalid value for {key}")), 400
                if not isinstance(default, bool) and clean[key] < 0:
                    return jsonify(_api_error_payload("bad_guards", f"{key} must be >= 0")), 400
            _prefs_overrides["guards"] = clean
        _apply_prefs_ranks()
        _save_prefs()
        return jsonify({"ok": True, "prefs": _prefs_view()})

    @app.get("/api/cover/<album_id>")
    def api_cover(album_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", album_id or ""):
            return ("", 204)
        try:
            size = int(request.args.get("size", 160))
        except (TypeError, ValueError):
            size = 160
        size = min(_COVER_SIZES, key=lambda s: abs(s - size))
        data, mimetype = _cover_art_bytes(album_id, size)
        if not data:
            return ("", 204)  # no art — frontend shows its gradient fallback
        return (data, 200, {
            "Content-Type": mimetype or "image/jpeg",
            "Cache-Control": "public, max-age=86400",
        })

    @app.post("/api/gaps/<group_id>/allow-mp3")
    def api_gap_allow_mp3(group_id):
        """Per-album MP3 opt-in. Off by default and never global: FORMAT_PRIORITY
        stays flac/opus, and this only widens the accepted formats for *this*
        album's searches and enqueues, with mp3 ranked last."""
        data = request.get_json(silent=True) or {}
        allow = bool(data.get("allow", True))
        with _review_lock:
            group = _find_review_group(group_id)
            if not group:
                return jsonify(_api_error_payload("not_found", "Group not found")), 404
            group["allow_mp3"] = allow
            group["updated_at"] = time.time()
            payload = {"ok": True, "gap": _gap_detail_view(group)}
        _save_review_state()
        return jsonify(payload)

    @app.post("/api/gaps/<group_id>/rescan")
    def api_gap_rescan(group_id):
        """Re-check one album — distinct from the bulk /api/scan-all.

        Re-reads it from Navidrome and walks its folder for files Navidrome
        hasn't indexed, then returns the updated counts so the UI reconciles
        without paying for a full library scan."""
        with _review_lock:
            if not _find_review_group(group_id):
                return jsonify(_api_error_payload("not_found", "Group not found")), 404
        op = _operation_create("rescan_album", "Rescanning album")
        try:
            payload = rescan_group_from_disk(group_id)
        except Exception as e:
            _operation_finish(op["id"], False, f"Album rescan failed: {e}")
            return jsonify(_with_operation(
                _api_error_payload("rescan_failed", "Album rescan failed", str(e)), op)), 500
        if not payload:
            _operation_finish(op["id"], False, "Group not found")
            return jsonify(_api_error_payload("not_found", "Group not found")), 404
        found = payload.get("foundOnDisk", 0)
        _operation_finish(op["id"], True,
                          f"Found {found} file(s) Navidrome hadn't indexed" if found
                          else "Album re-checked")
        _save_review_state()
        return jsonify(_with_operation(payload, op))

    @app.post("/api/gaps/<group_id>/fetch")
    def api_gap_fetch(group_id):
        data = request.get_json(silent=True) or {}
        with _review_lock:
            group = _find_review_group(group_id)
            if not group:
                return jsonify(_api_error_payload("not_found", "Group not found")), 404
            # Active-job dedupe: a double-click must not double-enqueue.
            if _gap_status_for_group(group) == "downloading":
                return jsonify({"ok": True, "alreadyActive": True,
                                "gap": _gap_detail_view(group)})
            folders = (group.get("source_results") or {}).get("folders") or []
            if not folders:
                return jsonify(_api_error_payload(
                    "no_sources", "No sources searched yet",
                    "Run find-sources for this album first.")), 400
            try:
                idx = int(data.get("sourceId", 0) or 0)
            except (TypeError, ValueError):
                idx = 0
            if not (0 <= idx < len(folders)):
                return jsonify(_api_error_payload(
                    "bad_source", "Source index out of range")), 400
            _approve_pending_missing_tracks(group)
            job = (_repair_job_for_group(group_id)
                   or _create_or_update_repair_job_from_group(group))
            op = _operation_create("select_source", "Queueing selected source",
                                   (job or {}).get("id", ""))
            result = _enqueue_group_source(group, idx)
            _operation_finish(op["id"], bool(result.get("ok")),
                              result.get("message", "Source queued"),
                              result.get("message", ""))
            if result.get("ok"):
                payload = _with_operation({"ok": True, "gap": _gap_detail_view(group)}, op)
            else:
                payload = _with_operation(_api_error_payload(
                    "enqueue_failed", result.get("message", "Enqueue failed"),
                    next_source=_next_source_view(group, idx)), op)
        _save_review_state()
        _save_state()
        return jsonify(payload), (200 if payload.get("ok") else 400)

    @app.post("/api/gaps/<group_id>/auto")
    def api_gap_auto(group_id):
        """Search, rank and download the best source without a manual pick.
        Backs the Fill-gaps 'Auto-select best source' button. Source search is
        slow (slskd fans out to peers), so this runs as a task and returns its
        id — the same shape /api/groups/<id>/sources returns."""
        with _review_lock:
            group = _find_review_group(group_id)
            if not group:
                return jsonify(_api_error_payload("not_found", "Group not found")), 404
            if _gap_status_for_group(group) == "downloading":
                return jsonify({"ok": True, "alreadyActive": True,
                                "gap": _gap_detail_view(group)})
            label = f"Auto-select source: {group.get('artist')} - {group.get('album')}"
            job = (_repair_job_for_group(group_id)
                   or _create_or_update_repair_job_from_group(group))
        op = _operation_create("auto_select_source", label,
                               (job or {}).get("id", ""), status="queued")
        task_id = _task_run("source-search", label,
                            lambda tid: _gap_auto_task(tid, group_id),
                            group_id=group_id)
        _operation_update(op["id"], status="running", task_id=task_id)
        _save_review_state()
        return jsonify(_with_operation({"ok": True, "task_id": task_id}, op))

    @app.post("/api/gaps/<group_id>/cancel")
    def api_gap_cancel(group_id):
        with _review_lock:
            group = _find_review_group(group_id)
            if not group:
                return jsonify(_api_error_payload("not_found", "Group not found")), 404
        op = _operation_create("cancel_gap_downloads", "Cancelling gap downloads")
        cancelled = 0
        for (username, filename), info in list(pending_downloads.items()):
            if info.get("review_group_id") != group_id:
                continue
            _slskd_cancel(username, filename)
            if info.get("repair_job_id"):
                _repair_update_download(info.get("repair_job_id", ""), username,
                                        filename, "cancelled", "cancelled by user",
                                        raw_state="Cancelled")
            pending_downloads.pop((username, filename), None)
            cancelled += 1
        for ag_id, ag in list(pending_album_groups.items()):
            if ag.get("review_group_id") == group_id:
                _abandon_group_downloads(ag_id)
                cancelled += 1
        _operation_finish(op["id"], True, f"Cancelled {cancelled} download(s)")
        _save_state()
        _save_review_state()
        with _review_lock:
            group = _find_review_group(group_id)
            payload = {"ok": True, "cancelled": cancelled,
                       "gap": _gap_detail_view(group) if group else None}
        return jsonify(_with_operation(payload, op))

    @app.get("/api/placements")
    def api_placements():
        items = _needs_placement_view()
        return jsonify({"items": items, "unidentified": _unidentified_count(items)})

    @app.post("/api/placements/identify")
    def api_placements_identify():
        """Resolve download folders to MusicBrainz releases in the background.
        User-initiated: the sweep is rate-limited to 1 MB req/sec."""
        data = request.get_json(silent=True) or {}
        tasks = _review_snapshot().get("tasks", {}) or {}
        running = next(
            (tid for tid, t in tasks.items()
             if t.get("kind") == "identify" and t.get("status") in ("queued", "running")),
            None)
        if running:
            return jsonify({"ok": True, "alreadyActive": True, "task_id": running})
        task_id = _task_run(
            "identify", "Identify download folders",
            lambda tid: _identify_folders_task(tid, bool(data.get("force"))))
        return jsonify({"ok": True, "task_id": task_id})

    @app.post("/api/placements/<placement_id>/confirm")
    def api_placement_confirm(placement_id):
        data = request.get_json(silent=True) or {}
        folders = _download_folders_cached(force=True)
        folder = next(
            (f for f in folders
             if hashlib.sha1(f["path"].encode("utf-8")).hexdigest()[:12] == placement_id),
            None)
        if not folder:
            return jsonify(_api_error_payload(
                "not_found", "Download folder not found",
                "It may already have been placed or removed.")), 404
        release_mbid = data.get("releaseMbid", "") or data.get("release_mbid", "")
        artist = data.get("artist", "")
        album = data.get("album", "")
        group_id = data.get("groupId", "") or data.get("group_id", "")
        if not release_mbid:
            suggestion = _suggest_review_group_for_folder(
                folder, _review_snapshot().get("groups", []) or [])
            if suggestion:
                with _review_lock:
                    group = _find_review_group(suggestion.get("group_id", ""))
                    if group:
                        group_id = group_id or group.get("id", "")
                        release_mbid = group.get("canonical_mbid", "")
                        artist = artist or group.get("artist", "")
                        album = album or group.get("album", "")
        if not release_mbid:
            # No review group for this folder — fall back to its cached identity
            # (the hand-downloaded-album case), but only when identification was
            # confident enough to file without a human looking at it.
            ident = _folder_identity_for(folder)
            if ident.get("release_mbid") \
                    and ident.get("confidence") in _CONFIRMABLE_CONFIDENCE:
                release_mbid = ident["release_mbid"]
                artist = artist or ident.get("artist", "")
                album = album or ident.get("album", "")
        if not release_mbid:
            return jsonify(_api_error_payload(
                "no_release", "No release match for this folder",
                "Run Identify, or pick the release manually.")), 400
        # Active-job dedupe: one placement task per folder at a time.
        tasks = _review_snapshot().get("tasks", {})
        running = next(
            (tid for tid, t in tasks.items()
             if t.get("kind") == "placement"
             and folder["path"] in (t.get("label") or "")
             and t.get("status") in ("queued", "running")),
            None)
        if running:
            return jsonify({"ok": True, "alreadyActive": True, "task_id": running})
        task_id = _task_run("placement", f"Place: {folder['path']}",
                            lambda tid: _deterministic_import_task(
                                tid, folder["path"], release_mbid, artist, album,
                                group_id))
        return jsonify({"ok": True, "task_id": task_id,
                        "placementId": placement_id, "path": folder["path"]})

    def _placement_folder(placement_id: str) -> dict | None:
        return next(
            (f for f in _download_folders_cached(force=True)
             if hashlib.sha1(f["path"].encode("utf-8")).hexdigest()[:12] == placement_id),
            None)

    @app.post("/api/placements/<placement_id>/dismiss")
    def api_placement_dismiss(placement_id):
        """Hide a download folder from Needs placement (persisted). Files
        stay on disk; the Import tab still lists the folder."""
        folder = _placement_folder(placement_id)
        if not folder:
            return jsonify(_api_error_payload(
                "not_found", "Download folder not found",
                "It may already have been placed or removed.")), 404
        _dismissed_folders.add(folder["path"])
        _save_state()
        return jsonify({"ok": True, "path": folder["path"]})

    @app.post("/api/placements/<placement_id>/delete")
    def api_placement_delete(placement_id):
        """Delete a download folder's files from the slskd downloads dir.
        Requires {"confirm": true}; the folder must resolve strictly inside
        SLSKD_DOWNLOAD_DIR."""
        data = request.get_json(silent=True) or {}
        if not data.get("confirm"):
            return jsonify(_api_error_payload(
                "confirm_required", "Pass {\"confirm\": true} to delete files")), 400
        folder = _placement_folder(placement_id)
        if not folder:
            return jsonify(_api_error_payload(
                "not_found", "Download folder not found")), 404
        real = os.path.realpath(folder["path"])
        root = os.path.realpath(SLSKD_DOWNLOAD_DIR)
        if real == root or not _path_inside(real, root):
            return jsonify(_api_error_payload(
                "bad_path", "Folder is outside the downloads directory")), 400
        try:
            shutil.rmtree(real)
        except Exception as e:
            return jsonify(_api_error_payload("delete_failed", str(e))), 500
        _imported_folders.discard(folder["path"])
        _dismissed_folders.discard(folder["path"])
        _download_folders_cached(force=True)
        _save_state()
        _web_log(f"placement: deleted downloaded folder {real}", "placement")
        return jsonify({"ok": True, "deleted": folder["path"]})

    @app.post("/api/scan")
    def api_scan():
        data = request.get_json(silent=True) or {}
        fuzzy = bool(data.get("fuzzy", FUZZY_DUPLICATES_DEFAULT))
        # deep: also confirm same-runtime candidates against MusicBrainz, which
        # is what finds duplicates whose titles are in different languages.
        # Costs rate-limited MB calls, so it is opt-in per scan.
        result = _start_review_scan(fuzzy, False, bool(data.get("deep")))
        return jsonify(result), (200 if result.get("ok") else 409)

    @app.post("/api/scan-all")
    def api_scan_all():
        data = request.get_json(silent=True) or {}
        fuzzy = bool(data.get("fuzzy", FUZZY_DUPLICATES_DEFAULT))
        result = _start_review_scan(fuzzy, True)
        return jsonify(result), (200 if result.get("ok") else 409)

    @app.post("/api/playlists/scan")
    def api_playlist_scan():
        task_id = _task_run("playlist-scan", "Scan ListenBrainz playlists",
                            _playlist_scan_task)
        return jsonify({"ok": True, "task_id": task_id})

    @app.post("/api/spotify/scan")
    def api_spotify_scan():
        data = request.get_json(silent=True) or {}
        playlist = data.get("playlist", "")
        if not playlist:
            return jsonify({"error": "playlist is required"}), 400
        task_id = _task_run("spotify-scan", "Scan Spotify playlist",
                            lambda tid: _spotify_scan_task(tid, playlist))
        return jsonify({"ok": True, "task_id": task_id})

    @app.get("/api/debug/slskd-search")
    def api_debug_slskd_search():
        """Raw slskd search trace for one query — see slskd_search_probe."""
        query = (request.args.get("q", "") or "").strip()
        if not query:
            return jsonify({"error": "q is required"}), 400
        try:
            wait = min(float(request.args.get("wait", 45)), 120.0)
        except (TypeError, ValueError):
            wait = 45.0
        try:
            stop_at = max(0.0, float(request.args.get("stopAt", 0)))
        except (TypeError, ValueError):
            stop_at = 0.0
        return jsonify(slskd_search_probe(query, wait, stop_at))

    @app.post("/api/search")
    def api_search():
        data = request.get_json(silent=True) or {}
        query = data.get("query", "").strip()
        if not query:
            return jsonify({"error": "query is required"}), 400
        task_id = _task_run("search", f"Search: {query}",
                            lambda tid: _search_task(tid, query))
        return jsonify({"ok": True, "task_id": task_id})

    @app.get("/api/searches")
    def api_searches():
        return jsonify(_review_snapshot().get("searches", {}))

    @app.post("/api/searches/<sid>/download")
    def api_search_download(sid):
        data = request.get_json(silent=True) or {}
        idx = int(data.get("index", 0))
        mode = data.get("mode", "folder")
        snap = _review_snapshot()
        search = snap.get("searches", {}).get(sid)
        if not search:
            return jsonify({"error": "Search not found"}), 404
        folders = search.get("folders", [])
        if idx < 0 or idx >= len(folders):
            return jsonify({"error": "Result index out of range"}), 400
        fd = folders[idx]
        user = _default_web_user()
        if mode == "file":
            file = min(fd.get("files", []), key=lambda f: _score_file(f, fd.get("upload_speed", 0)) or (99,))
            ok = slskd_enqueue(fd["username"], file, token=user.get("telegram_token", ""),
                               chat_id=str(user.get("chat_id", "")))
            return jsonify({"ok": ok})
        full = slskd_expand_directory(fd["username"], fd, fd["files"][0] if fd.get("files") else {})
        ok, total, ag_id = slskd_enqueue_folder(
            fd["username"], full or fd.get("files", []), token=user.get("telegram_token", ""),
            chat_id=str(user.get("chat_id", "")), label=fd.get("folder", ""))
        return jsonify({"ok": bool(ok), "queued": ok, "total": total, "album_group_id": ag_id})

    @app.get("/api/album/lookup")
    def api_album_lookup():
        query = request.args.get("q", "").strip()
        if not query:
            return jsonify({"error": "q is required"}), 400
        candidates = mbz_search_release_groups(query.replace(" - ", " "), 8)
        candidates.sort(key=lambda c: (0 if c["primary_type"] == "album" else 1, -c["score"]))
        return jsonify({"candidates": candidates})

    @app.get("/api/album/releases")
    def api_album_releases():
        """Releases of a release-group, as variant × edition.

        Two axes, because they answer different questions. A **variant** is
        Original / Remaster / Deluxe — it changes the tracklist. An **edition**
        is CD / Digital / Vinyl — same tracklist, different pressing, and
        crucially its *own* cover art: the Archive's release-group art is
        whichever release it happened to pick, which is how a vinyl-tagged copy
        ends up showing a photo of the disc instead of the sleeve.

        Each variant's default edition is the digital one where there is one —
        it is the likeliest to carry a correct front cover — and the variant
        carries that edition's `releaseMbid`, so callers that don't care about
        the second axis behave exactly as before."""
        rgid = request.args.get("rgid", "").strip()
        if not rgid:
            return jsonify({"error": "rgid is required"}), 400
        data = mbz_get(f"release-group/{rgid}", {"inc": "releases artist-credits media"})
        # Coarse buckets: MusicBrainz has dozens of format strings ('12" Vinyl',
        # 'Digital Media', 'Enhanced CD') and the picker offers three choices.
        def _edition_label(fmt: str) -> str:
            f = (fmt or "").lower()
            if "digital" in f or "file" in f:
                return "Digital"
            if "vinyl" in f:
                return "Vinyl"
            if "cassette" in f:
                return "Cassette"
            if "cd" in f:
                return "CD"
            return fmt or "Other"
        _EDITION_ORDER = {"Digital": 0, "CD": 1, "Vinyl": 2, "Cassette": 3}

        variants = {}
        for rel in data.get("releases", []):
            status = (rel.get("status") or "").lower()
            if status and status != "official":
                continue
            media = rel.get("media", []) or []
            track_count = sum(int(m.get("track-count", 0) or 0) for m in media)
            fmt = next((m.get("format") for m in media if m.get("format")), "")
            edition = {
                "releaseMbid": rel.get("id", ""),
                "label": _edition_label(fmt),
                "format": fmt or "",
                "packaging": rel.get("packaging") or "",
                "country": rel.get("country", ""),
                "year": (rel.get("date") or "")[:4],
                "coverUrl": caa_release_front_url(rel.get("id", ""), 250),
            }
            # The variant key is what changes the tracklist; the format is
            # deliberately not part of it, so CD and Digital of one edition
            # collapse into one entry with two editions rather than two rows.
            key = (rel.get("title", ""), rel.get("disambiguation", ""), track_count)
            variant = variants.setdefault(key, {
                "title": rel.get("title", ""),
                "disambiguation": rel.get("disambiguation", ""),
                "year": edition["year"],
                "country": rel.get("country", ""),
                "trackCount": track_count,
                "editions": [],
            })
            if not any(e["label"] == edition["label"] for e in variant["editions"]):
                variant["editions"].append(edition)
            if edition["year"] and (not variant["year"] or edition["year"] < variant["year"]):
                variant["year"] = edition["year"]

        out = []
        for variant in variants.values():
            variant["editions"].sort(key=lambda e: (_EDITION_ORDER.get(e["label"], 9), e["label"]))
            default = variant["editions"][0] if variant["editions"] else {}
            variant["releaseMbid"] = default.get("releaseMbid", "")
            variant["coverUrl"] = default.get("coverUrl", "") or caa_front_url(rgid, 250)
            out.append(variant)
        out.sort(key=lambda r: (r["year"] or "9999", -r["trackCount"]))
        return jsonify({
            "rgid": rgid,
            "title": data.get("title", ""),
            "artist": _artist_credit_str(data.get("artist-credit")) or "?",
            "coverUrl": caa_front_url(rgid, 250),
            "releases": out[:8],
        })

    @app.get("/api/album/sources")
    def api_album_sources():
        """Ranked slskd folders for a release-group, shaped like the Fill-gaps
        source list so the Artist album page can offer a manual picker."""
        rgid = request.args.get("rgid", "").strip()
        if not rgid:
            return jsonify({"error": "rgid is required"}), 400
        # A caller that has already resolved this release is believed.
        #
        # Not an optimisation — a fix. `mbz_resolve_album` parks a transient 503
        # or timeout in `_mbz_fail_until` for five minutes, and every call inside
        # that window returns {} *without asking MusicBrainz again*. So one
        # hiccup turned this route into a hard 400 for the same album for the
        # next five minutes, with nothing the user could do but wait without
        # being told what they were waiting for. The clients reach this screen
        # from /api/album/releases, which already told them the release, its
        # artist and its track count, so the answer is in the caller's hand.
        override = request.args.get("release_mbid", "").strip()
        try:
            override_total = int(request.args.get("total") or 0)
        except ValueError:
            override_total = 0
        resolved = ({"release_mbid": override,
                     "artist": request.args.get("artist", "").strip(),
                     "title": request.args.get("album", "").strip(),
                     "total_tracks": override_total}
                    if override else mbz_resolve_album(rgid))
        if not resolved.get("release_mbid"):
            return jsonify({"error": "Could not resolve album"}), 400
        artist = resolved.get("artist", "")
        album = resolved.get("title", "")
        total = int(resolved.get("total_tracks") or 0)
        folders = slskd_search_album_folders(artist, album, total)
        # Pair against the canonical tracklist. Coverage used to be
        # min(fileCount, total)/total with coverageFull = fileCount >= total — a
        # pure file count, so a folder holding a completely different album with
        # enough files in it reported as a complete match.
        tracklist = [{"title": t.get("title", ""),
                      "position": t.get("position", 0),
                      "artist": artist}
                     for t in mbz_release_tracks(resolved["release_mbid"])]
        sources = []
        for i, fd in enumerate(folders[:12]):
            view = _source_view(_source_summary(fd, i, tracks=tracklist))
            view["rank"] = i + 1
            view["recommended"] = (i == 0)
            if tracklist:
                detail = view.get("coverageDetail") or {}
                have = int(detail.get("haveTracks") or 0)
                view["coverage"] = f"{have}/{len(tracklist)} tracks"
                view["coverageFull"] = have >= len(tracklist)
            elif total:
                # No tracklist to compare against (MusicBrainz down): say so
                # rather than implying a verified match.
                view["coverage"] = f"{view.get('fileCount') or 0} files, tracklist unknown"
                view["coverageFull"] = False
            sources.append(view)
        return jsonify({"sources": sources, "artist": artist, "album": album,
                        "query": f"{artist} - {album}"})

    @app.post("/api/album/download")
    def api_album_download():
        """Fetch a whole release-group from Soulseek.

        **Idempotent per release.** The remote clients are thin triggers on top of
        lb-bot's own state, and two of them (plus this bot's SPA) can be looking
        at the same album; a second tap used to start a second search, a second
        set of transfers and a second placement pass over one album folder. A
        fill already in flight is the answer to "download this", so return it.

        An optional `quality` overrides the global Source preference for this
        album alone (see `_quality_preference`): the right copy of a record and
        the right copy of a b-side single are rarely the same choice, and the
        global setting is buried three screens away from where the user is when
        they decide. An unknown value is rejected rather than ignored — silently
        fetching 24/96 after being asked not to is the failure that matters.
        """
        data = request.get_json(silent=True) or {}
        quality = str(data.get("quality") or "").strip()
        if quality and quality not in QUALITY_PREFERENCES:
            return jsonify({"error": "quality must be one of "
                                     + ", ".join(QUALITY_PREFERENCES)}), 400
        rgid = data.get("rgid", "")
        # A caller carrying its own `release_mbid` outranks re-resolving the
        # group, for two reasons. It survives `mbz_resolve_album`'s five-minute
        # failure cooldown, which otherwise turns one transient MusicBrainz 503
        # into a hard 400 on this album for anyone who asks next — the download
        # button dead, with no way for the user to tell why or to try anything
        # else. And it is the only way an edition picker can mean anything: the
        # resolver picks "official, earliest" on its own, so a client that let
        # its user choose a pressing was overruled without either of them
        # hearing about it. `rgid` still rides along, because placement uses it
        # to flip this release-group's index row.
        resolved = (data if data.get("release_mbid")
                    else (mbz_resolve_album(rgid) if rgid else data))
        if not resolved.get("release_mbid"):
            return jsonify({"error": "Could not resolve album"}), 400
        release_mbid = resolved["release_mbid"]

        existing_gid, _ag = _album_group_for_release(release_mbid)
        running = _running_album_download_task(release_mbid)
        if existing_gid or running:
            return jsonify({"ok": True, "existing": True,
                            "task_id": running,
                            "album_group_id": existing_gid,
                            "status": _album_fill_view(release_mbid),
                            "resolved": resolved})

        chosen = None
        if data.get("sourceUsername"):
            chosen = {"username": data.get("sourceUsername", ""),
                      "folder": data.get("sourceFolder", "")}
        task_id = _task_run(
            "album-download",
            f"Download album: {resolved.get('artist')} - {resolved.get('title')}",
            lambda tid: _album_download_task(
                tid, release_mbid, resolved["artist"],
                resolved["title"], int(resolved.get("total_tracks") or 0), chosen,
                rgid, quality),
            total=int(resolved.get("total_tracks") or 0),
            release_mbid=release_mbid)
        return jsonify({"ok": True, "existing": False, "task_id": task_id,
                        "resolved": resolved})

    @app.get("/api/album/status")
    def api_album_status():
        """Where one release's fill has got to — the endpoint remote clients poll.

        Deliberately *not* `/api/tasks/<id>`: that answers "did slskd accept the
        enqueue", which is true a minute before anything reaches the library, and
        it pays a whole-review-state deep copy per call (`_review_snapshot`) on
        what is by definition a polled path.
        """
        release_mbid = request.args.get("release_mbid", "").strip()
        rgid = request.args.get("rgid", "").strip()
        if not release_mbid and rgid:
            # Convenience for callers holding only the release-group: resolution
            # is a MusicBrainz call, so prefer release_mbid on a polled path.
            release_mbid = (mbz_resolve_album(rgid) or {}).get("release_mbid", "")
        if not release_mbid:
            return jsonify({"error": "release_mbid or rgid is required"}), 400
        return jsonify(_album_fill_view(release_mbid))

    @app.get("/api/album/tracklist")
    def api_album_tracklist():
        """Tracklist for a concrete release — feeds the Artist page's per-album
        detail view.

        With `album_ids` (comma-separated Navidrome album ids) or `group_id`
        (a review group), every track also carries its own `present` flag. The
        album page used to get only `{present, total}` counts and had to guess
        *which* tracks were missing; on a real album the gaps are rarely the
        trailing tracks, so the guess was visibly wrong.
        """
        release_mbid = request.args.get("release_mbid", "").strip()
        if not release_mbid:
            return jsonify({"error": "release_mbid is required"}), 400
        tracks = [dict(t) for t in mbz_release_tracks(release_mbid)]

        album_ids = [a for a in
                     (request.args.get("album_ids", "") or "").split(",") if a.strip()]
        group_id = request.args.get("group_id", "").strip()
        if group_id and not album_ids:
            with _review_lock:
                group = _find_review_group(group_id)
                album_ids = [a.get("id", "") for a in ((group or {}).get("albums") or [])
                             if a.get("id")]
        if not album_ids:
            return jsonify({"release_mbid": release_mbid, "tracks": tracks,
                            "presenceKnown": False})

        # Same evidence the gap detector uses (recording MBID, else normalized
        # title), so the detail page and the Fill-gaps count can never disagree.
        user = _default_web_user() or {}
        owned_titles, owned_mbids = set(), set()
        for album_id in album_ids:
            try:
                record = _album_record({"id": album_id}, user.get("navidrome_user"),
                                       user.get("navidrome_password"))
            except Exception as e:
                print(f"  tracklist presence: Navidrome read failed for {album_id}: {e}")
                return jsonify({"release_mbid": release_mbid, "tracks": tracks,
                                "presenceKnown": False})
            for song in record.get("tracks", []):
                title = (song.get("title") or "").lower().strip()
                if title:
                    owned_titles.add(title)
                if song.get("musicBrainzId"):
                    owned_mbids.add(song["musicBrainzId"])
        for t in tracks:
            title = (t.get("title") or "").lower().strip()
            t["present"] = bool((t.get("mbid") and t["mbid"] in owned_mbids)
                                or (title and title in owned_titles))
        return jsonify({"release_mbid": release_mbid, "tracks": tracks,
                        "presenceKnown": True,
                        "present": sum(1 for t in tracks if t["present"]),
                        "total": len(tracks)})

    @app.get("/api/album/similar")
    def api_album_similar():
        """"Similar albums" shelf for an album page: one representative album
        per similar artist, taken from the user's own library.

        Similarity comes from the same stack the artist shelf uses
        (ListenBrainz, cross-checked with Last.fm when a key is configured),
        rolled up one album per artist. Attribution is explicit — every row
        names the artist that justifies it."""
        artist_mbid = request.args.get("artist_mbid", "").strip()
        artist_name = request.args.get("artist_name", "").strip()
        if not artist_mbid and not artist_name:
            return jsonify({"error": "artist_mbid or artist_name is required"}), 400
        try:
            limit = max(1, min(12, int(request.args.get("limit", 6))))
        except (TypeError, ValueError):
            limit = 6
        albums = _similar_albums_for_artist(
            artist_mbid, artist_name,
            exclude_rgid=request.args.get("rgid", "").strip(), limit=limit)
        return jsonify({"albums": albums,
                        "because": artist_name,
                        "sources": ["ListenBrainz"] + (["Last.fm"] if LASTFM_API_KEY else [])})

    @app.get("/api/artists")
    def api_artists():
        """Artist index for the Artist screen — Navidrome-sourced, cached on
        the shared 300s Navidrome index TTL (the summary refresh cadence)."""
        return jsonify(_artist_index_rows())

    @app.get("/api/artist/lookup")
    def api_artist_lookup():
        query = request.args.get("q", "").strip()
        if not query:
            return jsonify({"error": "q is required"}), 400
        candidates = mbz_search_artists(query, 8)
        return jsonify({"candidates": candidates})

    @app.get("/api/fresh-releases")
    def api_fresh_releases():
        """Site-wide ListenBrainz fresh releases. Each row carries two distinct
        ownership signals so the UI never conflates them:
          - `artistOwned` (+`artistId`): the release's artist is in the library.
            Drives the "Your artists" scope and artist deep-links.
          - `releaseOwned`: this exact release-group is already on disk (from the
            library index). Drives the "in library" chip and hides the download.
        `owned` is kept as a backward-compatible alias for `artistOwned`."""
        try:
            days = int(request.args.get("days", "30"))
        except ValueError:
            days = 30
        days = max(1, min(90, days))
        try:
            rows = lbz_fresh_releases(days)
        except LBZError as e:
            return jsonify({"error": f"ListenBrainz unavailable: {e}"}), 502
        # Ownership badges (artist in library / release on disk) are a best-effort
        # enrichment layered on top of the feed. A Navidrome or library-index
        # hiccup — e.g. a stale index DB missing a column — must degrade to
        # "no badges", not sink the whole Fresh tab.
        id_by_mbid = {}
        owned_rgids: set = set()
        try:
            for a in _nd_artist_index():
                mbid = a.get("musicBrainzId") or ""
                if mbid:
                    id_by_mbid[mbid] = a.get("id", "")
            owned_rgids = _index_owned_rgids()
        except Exception as e:
            print(f"  fresh-releases: ownership enrichment unavailable: {e}")
        out = []
        for r in rows:
            artist_id = next((id_by_mbid[m] for m in r.get("artistMbids", [])
                              if m in id_by_mbid), "")
            release_owned = bool(r.get("releaseGroupMbid")
                                 and r["releaseGroupMbid"] in owned_rgids)
            row = dict(r)
            row["artistOwned"] = bool(artist_id)
            row["artistId"] = artist_id
            row["releaseOwned"] = release_owned
            row["owned"] = bool(artist_id)   # compat alias for artistOwned
            out.append(row)
        return jsonify({"releases": out, "days": days})

    @app.get("/api/artist/discography")
    def api_artist_discography_read():
        """Instant read from the library index. {indexed: false} means the
        caller should start a scan (POST below); a stored result is served
        immediately even when stale — refresh is explicit (Rescan / bulk
        build), never a surprise multi-minute wait on page open."""
        mbid = request.args.get("mbid", "").strip()
        nd_id = request.args.get("nd_id", "").strip()
        if not mbid and not nd_id:
            return jsonify({"error": "mbid or nd_id is required"}), 400
        stored = _index_get_artist(mbid, nd_id)
        if not stored:
            return jsonify({"indexed": False})
        return jsonify({"indexed": True, **stored})

    @app.post("/api/artist/discography")
    def api_artist_discography():
        data = request.get_json(silent=True) or {}
        mbid = data.get("mbid", "").strip()
        name = data.get("name", "").strip()
        nd_id = data.get("nd_id", "").strip()
        if not mbid or not name:
            return jsonify({"error": "mbid and name are required"}), 400
        user = _default_web_user()
        if not user:
            return jsonify({"error": "No configured user"}), 400
        # `mb:` ids come from the MusicBrainz search (artists not in the
        # library), so there's nothing to match against — skip the library fetch.
        skip_library = bool(data.get("external")) or nd_id.startswith("mb:")
        task_id = _task_run(
            "artist-discography",
            f"Scan discography: {name}",
            lambda tid: _artist_discography_task(tid, mbid, name, user, nd_id,
                                                 skip_library=skip_library))
        return jsonify({"ok": True, "task_id": task_id})

    @app.post("/api/library-index/build")
    def api_library_index_build():
        user = _default_web_user()
        if not user:
            return jsonify({"error": "No configured user"}), 400
        tasks = _review_snapshot().get("tasks", {}) or {}
        running = next(
            (tid for tid, t in tasks.items()
             if t.get("kind") == "library-index" and t.get("status") == "running"),
            None)
        if running:
            return jsonify({"error": "already running", "task_id": running}), 409
        task_id = _task_run(
            "library-index", "Build library index",
            lambda tid: _library_index_task(tid, user))
        return jsonify({"ok": True, "task_id": task_id})

    @app.get("/api/library-index/status")
    def api_library_index_status():
        rows = _artist_index_rows()
        stale_cutoff = time.time() - LB_BOT_INDEX_TTL_DAYS * 86400
        with _index_lock:
            conn = _index_db()
            meta = conn.execute(
                "SELECT artist_key, nd_artist_id, scanned_at, scan_version "
                "FROM artists").fetchall()
        by_key = {m["artist_key"]: m for m in meta}
        by_nd = {m["nd_artist_id"]: m for m in meta if m["nd_artist_id"]}
        indexed = stale = 0
        for r in rows:
            m = by_key.get(r.get("mbid") or "") \
                or by_key.get(f"nd:{r.get('id', '')}") \
                or by_nd.get(r.get("id", ""))
            if not m:
                continue
            indexed += 1
            if (float(m["scanned_at"] or 0) < stale_cutoff
                    or int(m["scan_version"] or 0) != INDEX_SCAN_VERSION):
                stale += 1
        tasks = _review_snapshot().get("tasks", {}) or {}
        build = next(
            (t for t in tasks.values()
             if t.get("kind") == "library-index" and t.get("status") == "running"),
            None)
        return jsonify({
            "artistsTotal": len(rows),
            "artistsIndexed": indexed,
            "artistsStale": stale,
            "building": bool(build),
            "task": ({"id": build.get("id"), "done": build.get("done"),
                      "total": build.get("total"), "current": build.get("current")}
                     if build else None),
        })

    @app.get("/api/beets/folders")
    def api_beets_folders():
        # Cached: a fresh os.walk plus a mutagen read per file on every request
        # is the single most expensive thing the Import tab does. ?fresh=1 for
        # the explicit refresh. Copy the rows — the cache is shared.
        folders = [dict(f) for f in _download_folders_cached(_fresh_requested())]
        with _review_lock:
            groups = list(_review_state.get("groups", []) or [])
        for f in folders:
            f["suggested_match"] = _suggest_review_group_for_folder(f, groups)
            # Seeds the release picker's search box, so arriving there from a
            # failed/low-confidence identification isn't an empty query.
            ident = _folder_identity_for(f)
            f["suggested_release_label"] = (
                f"{ident['artist']} {ident['album']}".strip()
                if ident.get("release_mbid") else "")
        return jsonify({"folders": folders})

    @app.post("/api/beets/release-candidates")
    def api_beets_release_candidates():
        data = request.get_json(silent=True) or {}
        path = data.get("path", "")
        recid = data.get("download_id", "") or data.get("recid", "")
        group_id = data.get("group_id", "")
        rec = _albums.get(recid, {}) if recid else {}
        with _review_lock:
            group = _find_review_group(group_id) if group_id else None
            if not group and rec.get("review_group_id"):
                group = _find_review_group(rec.get("review_group_id"))
            group = json.loads(json.dumps(group)) if group else {}
            rec = dict(rec)
        candidates = _release_candidates_for_download(
            path or rec.get("album_dir", ""),
            data.get("artist", "") or rec.get("artist", ""),
            data.get("album", "") or rec.get("album", ""),
            group, rec, data.get("query", ""))
        return jsonify({"ok": True, "candidates": candidates})

    @app.post("/api/beets/import")
    def api_beets_import():
        data = request.get_json(silent=True) or {}
        path = data.get("path", "")
        if not path:
            return jsonify({"error": "path is required"}), 400
        release_mbid = data.get("release_mbid", "")
        if not release_mbid:
            return jsonify(_api_error_payload(
                "no_release", "No release selected",
                "Pick a MusicBrainz release before placing this folder.")), 400
        artist = data.get("artist", "")
        album = data.get("album", "")
        group_id = data.get("group_id", "")
        task_id = _task_run("placement", f"Place: {path}",
                            lambda tid: _deterministic_import_task(
                                tid, path, release_mbid, artist, album, group_id))
        return jsonify({"ok": True, "task_id": task_id})

    @app.post("/api/beets/import-preview")
    def api_beets_import_preview():
        data = request.get_json(silent=True) or {}
        path = data.get("path", "")
        if not path:
            return jsonify({"error": "path is required"}), 400
        files = _audio_files_in_folder(path, 100)
        return jsonify({
            "ok": bool(files),
            "output": f"{len(files)} audio file(s) ready for placement from {path}",
            "needs_release_pick": not bool(data.get("release_mbid")),
        })

    @app.get("/api/diagnostics")
    def api_diagnostics():
        rows = [
            {"ok": ok, "label": label, "detail": _redact_secrets(detail),
             "how_to_fix": _health_fix_hint(ok, detail)}
            for ok, label, detail in _diagnostics_cached(_fresh_requested())
        ]
        return jsonify({"checks": rows})

    @app.post("/api/rescan")
    def api_rescan():
        data = request.get_json(silent=True) or {}
        op = _operation_create("navidrome_rescan", "Triggering Navidrome scan")
        user = _default_web_user()
        ok = nd_start_scan(user["navidrome_user"], user["navidrome_password"],
                           bool(data.get("full", False)))
        _operation_finish(op["id"], bool(ok),
                          "Navidrome scan triggered" if ok else "Could not trigger Navidrome scan")
        _save_review_state()
        return jsonify(_with_operation({"ok": ok}, op)), (200 if ok else 500)

    @app.get("/api/settings")
    def api_settings():
        user = _default_web_user()
        checks = _diagnostics_cached(_fresh_requested())
        payload = _settings_cards(user, checks)
        payload.update({
            "build": WEB_BUILD,
            "state_file": _redact_secrets(STATE_FILE),
            "review_file": _redact_secrets(REVIEW_FILE),
        })
        return jsonify(payload)

    @app.get("/api/logs")
    def api_logs():
        severity = (request.args.get("severity", "") or "").lower()
        subsystem = (request.args.get("subsystem", "") or "").lower()
        tag = (request.args.get("tag", "") or "").lower() or subsystem
        entries = []
        for e in _web_events[-200:]:
            if not isinstance(e, dict):  # pre-restructure string entries
                e = {"ts": "", "epoch": 0, "tag": "app", "severity": "info", "msg": str(e)}
            if severity and severity not in (e.get("severity", "") or "").lower() \
                    and severity not in (e.get("msg", "") or "").lower():
                continue
            if tag and tag not in (e.get("tag", "") or "").lower() \
                    and tag not in (e.get("msg", "") or "").lower():
                continue
            entries.append(e)
        return jsonify({"entries": entries})

    @app.post("/api/groups/<group_id>/sources")
    def api_group_sources(group_id):
        # Results younger than SOURCE_RESULTS_TTL are reused unless the caller
        # asks for a fresh search ("Search again").
        force = bool((request.get_json(silent=True) or {}).get("force"))
        with _review_lock:
            group = _find_review_group(group_id)
            if not group:
                return jsonify({"error": "Group not found"}), 404
            label = f"Find sources: {group.get('artist')} - {group.get('album')}"
            # The redesigned UI has no separate approve step — searching for
            # sources implies wanting all missing tracks (mirrors the fetch route).
            _approve_pending_missing_tracks(group)
            job = _repair_job_for_group(group_id) or _create_or_update_repair_job_from_group(group)
        op = _operation_create("find_sources", label, (job or {}).get("id", ""), status="queued")
        task_id = _task_run(
            "source-search",
            label,
            lambda tid: _group_sources_task(tid, group_id, force),
            group_id=group_id)
        _operation_update(op["id"], status="running", task_id=task_id)
        _save_review_state()
        return jsonify(_with_operation({"ok": True, "task_id": task_id}, op))

    @app.get("/api/groups/<group_id>/sources")
    def api_group_sources_page(group_id):
        page = int(request.args.get("page", 0))
        with _review_lock:
            group = _find_review_group(group_id)
            if not group:
                return jsonify({"error": "Group not found"}), 404
            return jsonify(_group_source_page(group, page))

    @app.get("/api/groups/<group_id>/sources/<int:source_index>/files")
    def api_group_source_files(group_id, source_index):
        """The peer's full folder listing for one source, re-paired against the
        gaps. Triggered by opening the file disclosure in the picker."""
        with _review_lock:
            group = _find_review_group(group_id)
            if not group:
                return jsonify(_api_error_payload("not_found", "Group not found")), 404
            folders = (group.get("source_results") or {}).get("folders") or []
            if not (0 <= source_index < len(folders)):
                return jsonify(_api_error_payload(
                    "bad_index", "Source index out of range")), 404
            fd = folders[source_index]
        payload = _expanded_source_payload(group, fd, source_index)
        _save_review_state()
        return jsonify(payload)

    @app.post("/api/groups/<group_id>/tracks/<int:track_index>/pick-file")
    def api_group_track_pick_file(group_id, track_index):
        """Assign one specific file from one specific source to one missing
        track, overriding the automatic matcher."""
        data = request.get_json(silent=True) or {}
        with _review_lock:
            group = _find_review_group(group_id)
            if not group:
                return jsonify(_api_error_payload("not_found", "Group not found")), 404
            op = _operation_create("pick_file", "Queueing hand-picked file")
            result = _pick_file_for_track(group, track_index,
                                          data.get("sourceIndex", 0),
                                          data.get("filename", ""))
            _operation_finish(op["id"], bool(result.get("ok")),
                              result.get("message", ""),
                              "" if result.get("ok") else result.get("message", ""))
        _save_review_state()
        _save_state()
        return jsonify(_with_operation(result, op)), (200 if result.get("ok") else 400)

    @app.post("/api/groups/<group_id>/tracks/<int:track_index>/place-anyway")
    def api_group_track_place_anyway(group_id, track_index):
        """Place a hand-picked file the audio guard refused.

        Only reachable after the refusal has been reported on the track row with
        the name of the file it duplicates, so overriding is explicit rather than
        incidental — this is the one guard a manual pick does not silently win.
        """
        with _review_lock:
            group = _find_review_group(group_id)
            if not group:
                return jsonify(_api_error_payload("not_found", "Group not found")), 404
            result = _place_track_anyway(group, track_index)
        _save_review_state()
        _save_state()
        return jsonify(result), (200 if result.get("ok") else 400)

    @app.get("/api/groups/<group_id>/downloads/<download_id>/match")
    def api_group_download_match_payload(group_id, download_id):
        rec = _albums.get(download_id)
        with _review_lock:
            group = _find_review_group(group_id)
        if not group or not rec or rec.get("review_group_id") != group_id:
            return jsonify({"error": "Download match item not found"}), 404
        payload = _manual_match_payload(group, {"id": download_id, **rec})
        return jsonify(payload)

    @app.post("/api/groups/<group_id>/reconcile-downloads")
    def api_group_reconcile_downloads(group_id):
        data = request.get_json(silent=True) or {}
        with _review_lock:
            group = _find_review_group(group_id)
            if not group:
                return jsonify({"error": "Group not found"}), 404
            job = _repair_job_for_group(group_id) or _create_or_update_repair_job_from_group(group)
            op = _operation_create("reconcile_downloads", "Reconciling downloaded files",
                                   (job or {}).get("id", ""))
            result = _reconcile_group_downloaded_files(
                group, data.get("paths", []) or data.get("folders", []))
            group["last_action"] = "reconcile_downloads"
            _operation_finish(op["id"], bool(result.get("ok")),
                              f"Reconciled {result.get('matched_count', 0)} file(s)",
                              result.get("error", ""))
        _save_review_state()
        _save_state()
        return jsonify(_with_operation(result, op))

    @app.post("/api/groups/<group_id>/downloads/<download_id>/match-preview")
    def api_group_download_match_preview(group_id, download_id):
        rec = _albums.get(download_id)
        if not rec or rec.get("review_group_id") != group_id:
            return jsonify({"error": "Download match item not found"}), 404
        album_dir = rec.get("album_dir", "")
        files = _audio_files_in_folder(album_dir, 100)
        return jsonify({
            "ok": bool(files),
            "output": f"{len(files)} audio file(s) ready to place from {album_dir}",
        })

    @app.post("/api/groups/<group_id>/sources/<int:source_index>/download")
    def api_group_source_download(group_id, source_index):
        with _review_lock:
            group = _find_review_group(group_id)
            if not group:
                return jsonify({"error": "Group not found"}), 404
            job = _repair_job_for_group(group_id) or _create_or_update_repair_job_from_group(group)
            op = _operation_create("select_source", "Queueing selected source",
                                   (job or {}).get("id", ""))
            result = _enqueue_group_source(group, source_index)
            _operation_finish(op["id"], bool(result.get("ok")),
                              result.get("message", "Source queued"),
                              result.get("message", ""))
        _save_review_state()
        _save_state()
        return jsonify(_with_operation(result, op)), (200 if result.get("ok") else 400)

    @app.post("/api/downloads/cancel")
    def api_download_cancel():
        data = request.get_json(silent=True) or {}
        username = data.get("username", "")
        filename = data.get("filename", "")
        if not username or not filename:
            return jsonify({"error": "username and filename are required"}), 400
        _slskd_cancel(username, filename)
        info = pending_downloads.pop((username, filename), None)
        op = _operation_create("cancel_download", "Cancelling download",
                               (info or {}).get("repair_job_id", ""))
        if info and info.get("repair_job_id"):
            _repair_update_download(info.get("repair_job_id", ""), username, filename,
                                    "cancelled", "cancelled by user",
                                    raw_state="Cancelled")
        _operation_finish(op["id"], True, "Download cancelled")
        _save_state()
        _save_review_state()
        return jsonify(_with_operation({"ok": True}, op))

    @app.post("/api/downloads/clear")
    def api_downloads_clear():
        """Drop finished transfers from slskd's history and the bot's records."""
        op = _operation_create("clear_downloads", "Clearing finished transfers")
        try:
            _http.delete(f"{SLSKD_URL}/api/v0/transfers/downloads/all/completed",
                            headers=_slskd_headers(), timeout=15)
        except Exception as e:
            print(f"  slskd clear-completed error: {e}")
        removed = 0
        for key, info in list(pending_downloads.items()):
            state = str(info.get("latest_state", "") or "")
            if _slskd_succeeded(state) or _slskd_failed(state) or "Completed" in state:
                pending_downloads.pop(key, None)
                removed += 1
        # Album groups whose counters ran to completion but were never popped
        # (e.g. finished while the bot was down) count as finished too.
        for gid, ag in list(pending_album_groups.items()):
            total = int(ag.get("total") or 0)
            done = int(ag.get("completed") or 0) + int(ag.get("failed") or 0)
            if total and done >= total:
                pending_album_groups.pop(gid, None)
                removed += 1
        _save_state()
        _operation_finish(op["id"], True,
                          f"Cleared finished transfers ({removed} bot record(s))")
        return jsonify(_with_operation({"ok": True, "removed": removed}, op))

    @app.post("/api/transfers/<transfer_id>/dismiss")
    def api_transfer_dismiss(transfer_id):
        """Remove one transfer row (album group or single track) from the
        bot's tracking. Never cancels or deletes anything in slskd — this is
        the escape hatch for rows whose real-world work already happened."""
        if pending_album_groups.pop(transfer_id, None) is not None:
            for key, info in list(pending_downloads.items()):
                if info.get("album_group_id") == transfer_id:
                    pending_downloads.pop(key, None)
            _save_state()
            return jsonify({"ok": True, "kind": "album"})
        for key in list(pending_downloads.keys()):
            row_id = hashlib.sha1("|".join(key).encode("utf-8")).hexdigest()[:12]
            if row_id == transfer_id:
                pending_downloads.pop(key, None)
                _save_state()
                return jsonify({"ok": True, "kind": "track"})
        return jsonify({"error": "Transfer not found"}), 404

    @app.post("/api/imports/<recid>/retry")
    def api_import_retry(recid):
        rec = _albums.get(recid)
        if not rec:
            return jsonify({"error": "Import record not found"}), 404
        data = request.get_json(silent=True) or {}
        release_mbid = data.get("release_mbid") or rec.get("release_mbid", "")
        result = _deterministic_album_import(
            rec.get("album_dir", ""), release_mbid,
            rec.get("artist", ""), rec.get("album", ""),
            rec.get("review_group_id", ""))
        imported = result.get("ok", False)
        rec["status"] = "imported" if imported else "failed"
        rec["import_state"] = "imported" if imported else "downloaded_not_imported"
        if imported:
            _imported_folders.add(rec.get("album_dir", ""))
            _nd_scan_after_import(rec.get("token", ""))
            if rec.get("review_group_id"):
                with _review_lock:
                    group = _find_review_group(rec.get("review_group_id"))
                    if group:
                        _reconcile_group_downloaded_files(group, [rec.get("album_dir", "")])
                        _mark_group_tracks_placed(group, result)
        _save_state()
        if rec.get("review_group_id"):
            _save_review_state()
        return jsonify({"ok": imported, "status": rec["status"], "import_result": result})

    @app.post("/api/imports/<recid>/remove-stale-and-retry")
    def api_import_remove_stale_and_retry(recid):
        rec = _albums.get(recid)
        if not rec:
            return jsonify({"error": "Import record not found"}), 404
        album_dir = rec.get("album_dir", "")
        result = _deterministic_album_import(
            album_dir, rec.get("release_mbid", ""),
            rec.get("artist", ""), rec.get("album", ""),
            rec.get("review_group_id", ""))
        imported = result.get("ok", False)
        rec["status"] = "imported" if imported else "failed"
        rec["import_state"] = "imported" if imported else "downloaded_not_imported"
        if imported:
            _imported_folders.add(album_dir)
            _nd_scan_after_import(rec.get("token", ""))
            if rec.get("review_group_id"):
                with _review_lock:
                    group = _find_review_group(rec.get("review_group_id"))
                    if group:
                        _reconcile_group_downloaded_files(group, [album_dir])
                        _mark_group_tracks_placed(group, result)
        _save_state()
        if rec.get("review_group_id"):
            _save_review_state()
        return jsonify({"ok": imported, "status": rec["status"], "import_result": result})

    @app.post("/api/imports/<recid>/merge-retry")
    def api_import_merge_retry(recid):
        rec = _albums.get(recid)
        if not rec:
            return jsonify({"error": "Import record not found"}), 404
        data = request.get_json(silent=True) or {}
        album_dir = rec.get("album_dir", "")
        release_mbid = data.get("release_mbid") or rec.get("release_mbid", "")
        if not album_dir or not os.path.abspath(album_dir).startswith(os.path.abspath(SLSKD_DOWNLOAD_DIR)):
            return jsonify({"error": "Refusing to merge import outside downloads"}), 400
        if not release_mbid:
            return jsonify({"error": "release_mbid is required for merge retry",
                            "needs_release_pick": True}), 400
        result = _deterministic_album_import(
            album_dir, release_mbid, rec.get("artist", ""), rec.get("album", ""),
            rec.get("review_group_id", ""))
        imported = result.get("ok", False)
        rec["release_mbid"] = release_mbid
        rec["status"] = "imported" if imported else "failed"
        rec["import_state"] = "imported" if imported else "downloaded_not_imported"
        if imported:
            _imported_folders.add(album_dir)
            _nd_scan_after_import(rec.get("token", ""))
            if rec.get("review_group_id"):
                with _review_lock:
                    group = _find_review_group(rec.get("review_group_id"))
                    if group:
                        _reconcile_group_downloaded_files(group, [album_dir])
                        _mark_group_tracks_placed(group, result)
        _save_state()
        if rec.get("review_group_id"):
            _save_review_state()
        return jsonify({"ok": imported, "status": rec["status"], "import_result": result})

    @app.post("/api/groups/<group_id>/match-mode")
    def api_group_match_mode(group_id):
        data = request.get_json(silent=True) or {}
        mode = data.get("mode", "auto")
        if mode not in ("auto", "manual"):
            return jsonify({"error": "mode must be auto or manual"}), 400
        with _review_lock:
            group = _find_review_group(group_id)
            if not group:
                return jsonify({"error": "Group not found"}), 404
            group["match_mode"] = mode
            group["updated_at"] = time.time()
        _save_review_state()
        return jsonify({"ok": True, "mode": mode})

    @app.post("/api/groups/<group_id>/downloads/<download_id>/match-selected")
    def api_group_download_match_selected(group_id, download_id):
        data = request.get_json(silent=True) or {}
        rec = _albums.get(download_id)
        if not rec or rec.get("review_group_id") != group_id:
            return jsonify({"error": "Download match item not found"}), 404
        job = _repair_job_for_group(group_id)
        op = _operation_create("manual_match_selected", "Matching selected downloaded files",
                               (job or {}).get("id", ""))
        release_mbid = data.get("release_mbid") or rec.get("release_mbid", "")
        autotag = bool(data.get("autotag", True))
        relpaths = data.get("files", [])
        if autotag and not release_mbid:
            _operation_finish(op["id"], False, "Pick a MusicBrainz release before matching selected files",
                              "Pick a MusicBrainz release before matching selected files")
            _save_review_state()
            return jsonify({"error": "Pick a MusicBrainz release before matching selected files"}), 400
        if not relpaths:
            _operation_finish(op["id"], False, "Select at least one downloaded file",
                              "Select at least one downloaded file")
            _save_review_state()
            return jsonify({"error": "Select at least one downloaded file"}), 400
        # The selection was validated and then ignored: every file in the folder
        # got placed regardless, which is one more way an album picked up extra
        # copies. Honour it.
        result = _deterministic_album_import(
            rec.get("album_dir", ""), release_mbid,
            rec.get("artist", ""), rec.get("album", ""),
            group_id, only_relpaths=relpaths)
        ok = result.get("ok", False)
        rec["status"] = "imported" if ok else "failed"
        rec["release_mbid"] = release_mbid
        rec["import_state"] = "imported" if ok else "downloaded_not_imported"
        reconcile = {}
        if ok:
            _imported_folders.add(rec.get("album_dir", ""))
            _nd_scan_after_import(rec.get("token", ""))
        with _review_lock:
            group = _find_review_group(group_id)
            if group:
                if ok:
                    reconcile = _reconcile_group_downloaded_files(
                        group, [rec.get("album_dir", "")])
                    _mark_group_tracks_placed(group, result)
                for item in group.get("match_items", []):
                    if item.get("id") == download_id:
                        item["status"] = rec["status"]
                        item["import_state"] = rec["import_state"]
                        item["release_mbid"] = release_mbid
                group["updated_at"] = time.time()
        _operation_finish(op["id"], ok, "Selected files placed" if ok else result.get("error", "Failed"),
                          result.get("error", ""))
        _save_state()
        _save_review_state()
        return jsonify(_with_operation({"ok": ok, "status": rec["status"],
                        "reconcile": reconcile,
                        "import_result": result}, op))

    @app.post("/api/groups/<group_id>/downloads/<download_id>/match")
    def api_group_download_match(group_id, download_id):
        data = request.get_json(silent=True) or {}
        rec = _albums.get(download_id)
        if not rec or rec.get("review_group_id") != group_id:
            return jsonify({"error": "Download match item not found"}), 404
        job = _repair_job_for_group(group_id)
        op = _operation_create("manual_match_folder", "Importing downloaded folder",
                               (job or {}).get("id", ""))
        release_mbid = data.get("release_mbid") or rec.get("release_mbid", "")
        result = _deterministic_album_import(
            rec.get("album_dir", ""), release_mbid,
            rec.get("artist", ""), rec.get("album", ""), group_id)
        ok = result.get("ok", False)
        rec["status"] = "imported" if ok else "failed"
        rec["release_mbid"] = release_mbid
        rec["import_state"] = "imported" if ok else "downloaded_not_imported"
        if ok:
            _imported_folders.add(rec.get("album_dir", ""))
            _nd_scan_after_import(rec.get("token", ""))
        reconcile = {}
        with _review_lock:
            group = _find_review_group(group_id)
            if group:
                if ok:
                    reconcile = _reconcile_group_downloaded_files(
                        group, [rec.get("album_dir", "")])
                    _mark_group_tracks_placed(group, result)
                for item in group.get("match_items", []):
                    if item.get("id") == download_id:
                        item["status"] = rec["status"]
                        item["import_state"] = rec["import_state"]
                group["updated_at"] = time.time()
        _operation_finish(op["id"], ok, "Folder placed" if ok else result.get("error", "Failed"),
                          result.get("error", ""))
        _save_state()
        _save_review_state()
        return jsonify(_with_operation({"ok": ok, "status": rec["status"],
                        "reconcile": reconcile,
                        "import_result": result}, op))

    @app.post("/api/groups/<group_id>/release-details")
    def api_release_details(group_id):
        with _review_lock:
            group = _find_review_group(group_id)
            if not group:
                return jsonify({"error": "Group not found"}), 404
            changed = _enrich_review_release_details(group)
            snapshot = json.loads(json.dumps(group))
        if changed:
            _save_review_state()
        return jsonify({"ok": True, "group": snapshot})

    @app.get("/api/groups/<group_id>/preview")
    def api_preview(group_id):
        with _review_lock:
            group = _find_review_group(group_id)
            if not group:
                return jsonify({"error": "Group not found"}), 404
            return jsonify(preview_group_retag(group))

    @app.post("/api/groups/<group_id>/canonical")
    def api_canonical(group_id):
        data = request.get_json(silent=True) or {}
        with _review_lock:
            group = _find_review_group(group_id)
            if not group:
                return jsonify({"error": "Group not found"}), 404
            job = _repair_job_for_group(group_id) or _create_or_update_repair_job_from_group(group)
            op = _operation_create("set_canonical", "Updating canonical album",
                                   (job or {}).get("id", ""))
            album_id = data.get("album_id", "")
            if not any(a.get("id") == album_id for a in group.get("albums", [])):
                _operation_finish(op["id"], False, "Unknown album id", "Unknown album id")
                return jsonify({"error": "Unknown album id"}), 400
            group["canonical_album_id"] = album_id
            refresh_group_missing(group)
            group["last_action"] = "canonical"
            _create_or_update_repair_job_from_group(group)
            _operation_finish(op["id"], True, "Canonical album updated")
        _save_review_state()
        _save_state()
        return jsonify(_with_operation({"ok": True, "group": group}, op))

    # POST /api/groups/<id>/merge is gone: it only flipped a `merge_mode` flag and
    # nothing in the SPA ever called it. The merge affordance posts to /retag,
    # which is the operation that actually does something.
    @app.post("/api/groups/<group_id>/hide")
    def api_hide_group(group_id):
        data = request.get_json(silent=True) or {}
        hidden = bool(data.get("hidden", True))
        with _review_lock:
            group = _find_review_group(group_id)
            if not group:
                return jsonify({"error": "Group not found"}), 404
            group["hidden"] = hidden
            group["updated_at"] = time.time()
        _save_review_state()
        return jsonify({"ok": True, "group_id": group_id, "hidden": hidden})

    @app.post("/api/groups/<group_id>/missing")
    def api_missing(group_id):
        with _review_lock:
            group = _find_review_group(group_id)
            if not group:
                return jsonify({"error": "Group not found"}), 404
            job = _repair_job_for_group(group_id) or _create_or_update_repair_job_from_group(group)
            op = _operation_create("rescan_missing", "Rescanning missing tracks",
                                   (job or {}).get("id", ""))
            refresh_group_missing(group)
            group["last_action"] = "missing_scan"
            _create_or_update_repair_job_from_group(group)
            _operation_finish(op["id"], True, "Missing tracks rescanned")
        _save_review_state()
        _save_state()
        return jsonify(_with_operation({"ok": True, "group": group}, op))

    @app.post("/api/groups/<group_id>/retag")
    def api_retag(group_id):
        if not _find_review_group(group_id):
            return jsonify({"error": "Group not found"}), 404
        job = _repair_job_for_group(group_id)
        op = _operation_create("retag_group", f"Retag group {group_id}",
                               (job or {}).get("id", ""), status="queued")
        task_id = _task_run("retag", f"Retag group {group_id}",
                            lambda tid: _retag_task(tid, group_id))
        _operation_update(op["id"], status="running", task_id=task_id)
        _save_review_state()
        return jsonify(_with_operation({"ok": True, "task_id": task_id}, op))

    @app.post("/api/groups/<group_id>/decisions")
    def api_decisions(group_id):
        data = request.get_json(silent=True) or {}
        with _review_lock:
            group = _find_review_group(group_id)
            if not group:
                return jsonify({"error": "Group not found"}), 404
            job = _repair_job_for_group(group_id) or _create_or_update_repair_job_from_group(group)
            op = _operation_create("track_decisions", "Updating track decisions",
                                   (job or {}).get("id", ""))
            tracks = group.get("missing_tracks", [])
            for item in data.get("tracks", []):
                indexes = item.get("indexes")
                if indexes is None:
                    indexes = [item.get("index", -1)]
                decision = item.get("decision", "pending")
                if decision not in ("pending", "approved", "source_pending",
                                    "queued", "downloading", "skipped",
                                    "downloaded", "failed", "cancelled"):
                    _operation_finish(op["id"], False, f"Invalid decision: {decision}",
                                      f"Invalid decision: {decision}")
                    return jsonify({"error": f"Invalid decision: {decision}"}), 400
                for raw_idx in indexes:
                    idx = int(raw_idx)
                    if 0 <= idx < len(tracks):
                        tracks[idx]["decision"] = decision
            group["updated_at"] = time.time()
            job = _create_or_update_repair_job_from_group(group)
            if job:
                _repair_job_touch(job, {"kind": "track_decision", "message": "Track decisions updated"})
            _operation_finish(op["id"], True, "Track decisions updated")
        _save_review_state()
        _save_state()
        return jsonify(_with_operation({"ok": True}, op))

    @app.post("/api/groups/<group_id>/download")
    def api_download(group_id):
        if not _find_review_group(group_id):
            return jsonify({"error": "Group not found"}), 404
        job = _repair_job_for_group(group_id)
        op = _operation_create("download_approved", f"Download approved tracks {group_id}",
                               (job or {}).get("id", ""), status="queued")
        task_id = _task_run("download-approved", f"Download approved tracks {group_id}",
                            lambda tid: _download_group_task(tid, group_id))
        _operation_update(op["id"], status="running", task_id=task_id)
        _save_review_state()
        return jsonify(_with_operation({"ok": True, "task_id": task_id}, op))

    @app.get("/api/status")
    def api_web_status():
        return jsonify({
            "review_file": REVIEW_FILE,
            "music_library_path": MUSIC_LIBRARY_PATH,
            "downloads": len(pending_downloads),
            "album_groups": len(pending_album_groups),
            "events": [_web_event_line(e) for e in _web_events[-100:]],
        })

    def _run():
        _web_log(f"dashboard listening on http://{WEB_UI_HOST}:{WEB_UI_PORT}")
        app.run(host=WEB_UI_HOST, port=WEB_UI_PORT, threaded=True, use_reloader=False)

    threading.Thread(target=_run, daemon=True, name="album-review-web").start()

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trigger a scan on demand for the bot that received the command."""
    token = _resolve_token(context)
    user  = _user_for_token(token)
    app   = next((a for a in _APPS if a.bot.token == token), None)
    if not user or app is None:
        await update.message.reply_text("Could not identify user.")
        return
    lbz_user = user["listenbrainz_user"]
    if lbz_user in _scans_running:
        await update.message.reply_text(
            "A scan is already running for you — results will arrive when it "
            "finishes.")
        return
    _scans_running.add(lbz_user)
    await update.message.reply_text(
        f"Scanning playlists for {lbz_user}... "
        f"this can take a few minutes.")
    try:
        missing = await asyncio.to_thread(scan_user, user)
    except LBZError as e:
        print(f"  /scan LBZ error: {e}")
        await update.message.reply_text(
            "⚠️ Couldn't reach ListenBrainz just now (timed out after retries). "
            "Nothing was scanned — please try /scan again in a moment.")
        return
    except Exception as e:
        print(f"  /scan error: {e}")
        await update.message.reply_text(
            f"⚠️ Scan failed: {e}\nRun /diag to check connectivity.")
        return
    finally:
        _scans_running.discard(lbz_user)
    if missing:
        await send_missing_tracks(app, user, missing)
    else:
        await update.message.reply_text("Everything is up to date — nothing missing.")
    await asyncio.to_thread(_save_state)

async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List in-progress albums and any finished albums needing attention."""
    token = _resolve_token(context)
    groups = [ag for ag in pending_album_groups.values() if ag.get("token") == token]
    review = [(rid, r) for rid, r in _albums.items()
              if r.get("token") == token and r.get("status") in ("needs_review", "failed")]

    if groups:
        for ag in groups:
            done = ag["completed"] + ag["failed"]
            await update.message.reply_text(
                f"⬇️ {ag['label']} — {done}/{ag['total']} "
                f"{_progress_bar(done, ag['total'])} (from @{ag.get('source_user','?')})")
    if review:
        for rid, r in review[:20]:
            tag = "⚠️ needs review" if r["status"] == "needs_review" else "❌ import failed"
            await update.message.reply_text(
                f"{tag}: {r['label']}",
                reply_markup=_album_action_markup(rid, r["status"]))
    if not groups and not review:
        await update.message.reply_text(
            "Nothing pending. Albums needing attention show up here after download.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show active transfers and album-group progress for this user."""
    token = _resolve_token(context)
    mine  = [v for v in pending_downloads.values() if v.get("token") == token]
    groups = [ag for ag in pending_album_groups.values() if ag.get("token") == token]

    lines = []
    if groups:
        lines.append(f"Albums in progress ({len(groups)}):")
        for ag in groups:
            done = ag["completed"] + ag["failed"]
            bar  = _progress_bar(done, ag["total"])
            fail = f", {ag['failed']} failed" if ag["failed"] else ""
            lines.append(f"  {bar} {done}/{ag['total']}{fail} — {ag['label']}")
    indiv = [v for v in mine if not v.get("album_group_id")]
    if indiv:
        lines.append(f"\nIndividual tracks downloading ({len(indiv)}):")
        for v in indiv[:20]:
            t = v.get("track") or {}
            lines.append(f"  • {t.get('artist','?')} - {t.get('title','?')}")
        if len(indiv) > 20:
            lines.append(f"  …and {len(indiv) - 20} more")
    if not lines:
        await update.message.reply_text(
            "Nothing in progress. Use /scan to look for missing tracks.")
        return
    await update.message.reply_text("\n".join(lines))

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

async def scheduled_scan(apps: list, force: bool = False):
    print(f"\n[{datetime.datetime.now():%Y-%m-%d %H:%M}] Running weekly scan...")
    for user, app in zip(USERS, apps):
        lbz_user = user["listenbrainz_user"]
        last     = _last_scan_ts.get(lbz_user, 0)
        if lbz_user in _scans_running:
            print(f"  Skipping {lbz_user}: a scan is already in flight")
            continue
        if not force and (time.time() - last) < STARTUP_SCAN_THROTTLE:
            mins = int((time.time() - last) / 60)
            print(f"  Skipping {lbz_user}: scanned {mins} min ago "
                  f"(within {STARTUP_SCAN_THROTTLE // 3600}h throttle)")
            continue
        _scans_running.add(lbz_user)
        try:
            missing = await asyncio.to_thread(scan_user, user)
        except LBZError as e:
            # Don't claim "up to date" and don't record the scan — retry next cycle.
            print(f"  ListenBrainz unavailable for {lbz_user}: {e} — skipping this run")
            continue
        except Exception as e:
            # Any other failure (Navidrome down, MBZ hiccup…) must not kill the
            # scheduler task or block the remaining users.
            import traceback
            traceback.print_exc()
            print(f"  scan failed for {lbz_user}: {e} — skipping this run")
            continue
        finally:
            _scans_running.discard(lbz_user)
        if missing:
            print(f"  {len(missing)} missing tracks for {lbz_user}")
            await send_missing_tracks(app, user, missing)
        else:
            print(f"  Everything up to date for {lbz_user}")
    _save_state()

async def tuesday_scheduler(apps: list):
    if RUN_SCAN_ON_START:
        # Throttled: a crash-loop or routine restart won't re-hammer MusicBrainz
        # or re-spam the missing-tracks list.
        try:
            await scheduled_scan(apps, force=False)
        except Exception as e:
            print(f"  startup scan failed: {e}")
    while True:
        now        = datetime.datetime.now()
        days_ahead = (1 - now.weekday()) % 7
        if days_ahead == 0 and now.hour >= 9:
            days_ahead = 7
        next_run = (now.replace(hour=9, minute=0, second=0, microsecond=0)
                    + datetime.timedelta(days=days_ahead))
        wait_sec = (next_run - now).total_seconds()
        print(f"Next scan: {next_run:%Y-%m-%d %H:%M} ({wait_sec/3600:.1f}h away)")
        await asyncio.sleep(wait_sec)
        try:
            await scheduled_scan(apps, force=True)  # the weekly run always fires
        except Exception as e:
            # Never let one bad week kill the scheduler permanently.
            import traceback
            traceback.print_exc()
            print(f"  weekly scan failed: {e} — will retry next week")

async def state_flush_loop():
    """Periodically snapshot durable state so a restart loses as little as possible."""
    while True:
        await asyncio.sleep(STATE_FLUSH_INT)
        await asyncio.to_thread(_save_state)

async def housekeeping_loop(apps: list):
    """Drop untouched button state so memory doesn't grow without bound."""
    while True:
        await asyncio.sleep(SWEEP_INT)
        try:
            now     = time.time()
            # Playlist "Download all" buttons (uid ^p\d+$) are exempt: their
            # backing tracks in missing_by_playlist are never swept, and the
            # weekly scan's buttons must outlive the 24h TTL or they'd be dead
            # for 6 of 7 days. They get replaced on each new scan anyway.
            stale   = [uid for uid, ts in list(_uid_ts.items())
                       if now - ts > UID_TTL and not re.fullmatch(r"p\d+", uid)]
            removed = 0
            for uid in stale:
                tok = _uid_to_token.pop(uid, None)
                _uid_to_playlist.pop(uid, None)
                _uid_ts.pop(uid, None)
                for store in (pending_approvals, pending_retries, manual_search_results):
                    if tok in store and uid in store[tok]:
                        store[tok].pop(uid, None)
                for store in (_pending_beets, _pending_checkdl, _pending_dlall,
                              _pending_album, _pending_pick):
                    store.pop(uid, None)
                removed += 1
            if removed:
                print(f"  housekeeping: dropped {removed} stale button state(s)")
                await asyncio.to_thread(_save_state)
        except Exception as e:
            print(f"  housekeeping error: {e}")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

BOT_COMMANDS = [
    ("scan",        "Scan playlists now for missing tracks"),
    ("status",      "Show in-progress downloads and album groups"),
    ("pending",     "Albums in progress or needing review"),
    ("diag",        "Check what the bot can reach (downloads, slskd, APIs)"),
    ("album",       "Download a whole album by name"),
    ("search",      "Search Soulseek: /search Artist - Title"),
    ("spplaylist",  "Import a public Spotify playlist"),
    ("checkalbums", "Find incomplete albums in your library"),
    ("beets",       "Import downloaded folders with beets"),
    ("rescan",      "Trigger a Navidrome quick scan"),
    ("help",        "Show available commands"),
]

async def on_telegram_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Global PTB error handler: without one, a handler exception dies silently
    and the user sees a button that does nothing."""
    import traceback
    err = context.error
    tb  = "".join(traceback.format_exception(type(err), err, err.__traceback__))
    print(f"  Telegram handler error: {err}\n{tb}")
    _web_log(f"telegram handler error: {err}")
    chat_id = None
    try:
        if isinstance(update, Update):
            if update.effective_chat is not None:
                chat_id = update.effective_chat.id
            elif (update.callback_query is not None
                  and update.callback_query.message is not None):
                chat_id = update.callback_query.message.chat_id
        if chat_id is not None:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ Something went wrong handling that action:\n{err}")
    except Exception as notify_err:
        print(f"  Failed to notify chat about handler error: {notify_err}")

async def main():
    print("Bot starting...")
    _load_state()
    _load_review_state()
    _load_prefs()
    print(f"  config: STATE_FILE={STATE_FILE}")
    print(f"  config: REVIEW_FILE={REVIEW_FILE}")
    print(f"  config: SLSKD_DOWNLOAD_DIR={SLSKD_DOWNLOAD_DIR}")
    print(f"  config: MUSIC_LIBRARY_PATH={MUSIC_LIBRARY_PATH}")
    # Placement ownership comes from the process identity, so print it — this is
    # the fastest way to confirm the compose `user:` took effect.
    try:
        print(f"  config: running as uid={os.getuid()} gid={os.getgid()} "
              f"umask={LB_BOT_UMASK or 'inherited'}")
    except AttributeError:
        pass  # os.getuid is POSIX-only; harmless when developing on Windows
    print(f"  config: WEB_UI={'on' if WEB_UI_ENABLED else 'off'} "
          f"{WEB_UI_HOST}:{WEB_UI_PORT}")
    print(f"  config: WEB_BUILD={WEB_BUILD}")
    if not os.path.isdir(SLSKD_DOWNLOAD_DIR):
        print(f"  WARNING: SLSKD_DOWNLOAD_DIR '{SLSKD_DOWNLOAD_DIR}' does not exist "
              f"inside this container. Completed downloads cannot be imported and "
              f"/beets will be empty until you mount slskd's downloads volume here.")
    apps = []
    for user in USERS:
        app = Application.builder().token(user["telegram_token"]).build()
        app.add_error_handler(on_telegram_error)
        app.add_handler(CallbackQueryHandler(callback_handler))
        app.add_handler(CommandHandler("start", help_command))
        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(CommandHandler("scan", scan_command))
        app.add_handler(CommandHandler("status", status_command))
        app.add_handler(CommandHandler("pending", pending_command))
        app.add_handler(CommandHandler("diag", diag_command))
        app.add_handler(CommandHandler("album", album_command))
        app.add_handler(CommandHandler("search", search_command))
        app.add_handler(CommandHandler("checkalbums", checkalbums_command))
        app.add_handler(CommandHandler("spplaylist", spplaylist_command))
        app.add_handler(CommandHandler("beets", beets_command))
        app.add_handler(CommandHandler("rescan", rescan_command))
        app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
        await app.initialize()
        await app.start()
        # Explicit allowed_updates: Telegram persists the previous setting
        # server-side when omitted, so a past poller with a restricted set
        # would silently drop callback_query updates (dead buttons).
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        # Register the Telegram command menu for autocomplete + the "/" menu
        try:
            await app.bot.set_my_commands(BOT_COMMANDS)
        except Exception as e:
            print(f"  set_my_commands failed: {e}")
        apps.append(app)
        print(f"  Bot started for {user['listenbrainz_user']}")

    # Stash apps so on-demand commands (/scan) can reach every bot instance
    global _APPS
    _APPS = apps
    start_web_dashboard()

    asyncio.create_task(tuesday_scheduler(apps))
    asyncio.create_task(poll_downloads_loop(apps))
    asyncio.create_task(state_flush_loop())
    asyncio.create_task(housekeeping_loop(apps))

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        _save_state()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _save_state()
        sys.exit(0)
