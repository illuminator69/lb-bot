"""Dev stub: serves web/dist plus canned /api responses in the new shapes.

Lets the redesigned SPA be exercised without the live Navidrome/slskd stack.
Run: python stub_server.py  → http://127.0.0.1:8898

POST /api/gaps/<id>/fetch succeeds for every album except g1 (the failed one),
which returns the structured error envelope so the recovery card is demoable.
PUT /api/prefs is stateful for the session.
"""
import json
import os
import time
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

DIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist")

GAP_ITEMS = [
    {"id": "g1", "albumId": "al1", "artist": "Massive Attack", "album": "Mezzanine",
     "present": 9, "total": 11, "missingCount": 2, "status": "failed",
     "coverUrl": "/api/cover/al1?size=160", "updatedAt": time.time()},
    {"id": "g2", "albumId": "al2", "artist": "Kiasmos", "album": "Kiasmos",
     "present": 7, "total": 10, "missingCount": 3, "status": "ready",
     "coverUrl": "/api/cover/al2?size=160", "updatedAt": time.time()},
    {"id": "g3", "albumId": "al3", "artist": "Bonobo", "album": "Migration",
     "present": 10, "total": 12, "missingCount": 2, "status": "downloading",
     "coverUrl": "/api/cover/al3?size=160", "updatedAt": time.time()},
    {"id": "g4", "albumId": "al4", "artist": "Rival Consoles", "album": "Persona",
     "present": 8, "total": 8, "missingCount": 0, "status": "complete",
     "coverUrl": "/api/cover/al4?size=160", "updatedAt": time.time()},
]

SOURCES = [
    {"id": 0, "peer": "vinyl_rip_guy", "folder": "M/Mezzanine [FLAC]", "format": "FLAC",
     "bitrate": "16-bit 44.1 kHz", "size": "78.4 MB",
     "fileCount": 11, "speedMbps": 4.2, "queueLength": 0, "freeSlot": True,
     "coverage": "2/2", "coverageFull": True,
     "coverageDetail": {"haveTracks": 2, "totalTracks": 2, "unmatched": []},
     "flags": [], "recommendation": "Best match", "score": 9800, "rank": 1, "recommended": True},
    {"id": 1, "peer": "slowpoke99", "folder": "Mezzanine (1998)", "format": "OPUS",
     "bitrate": "256 kbps", "size": "31.0 MB",
     "fileCount": 11, "speedMbps": 0.4, "queueLength": 7, "freeSlot": False,
     "coverage": "1/2", "coverageFull": False,
     "coverageDetail": {"haveTracks": 1, "totalTracks": 2, "unmatched": ["Group Four"]},
     "flags": ["slow peer", "missing track"], "recommendation": "Complete but queued",
     "score": 5200, "rank": 2, "recommended": False},
    {"id": 2, "peer": "archivist_x", "folder": "MA - Mezzanine 24bit", "format": "FLAC",
     "bitrate": "24-bit 96 kHz", "size": "612.9 MB",
     "fileCount": 11, "speedMbps": 2.1, "queueLength": 1, "freeSlot": True,
     "coverage": "2/2", "coverageFull": True, "flags": ["very large"],
     "recommendation": "", "score": 7100, "rank": 3, "recommended": False},
    {"id": 3, "peer": "mp3_marauder", "folder": "Mezzanine 320", "format": "MP3",
     "bitrate": "320 kbps", "size": "104.2 MB",
     "fileCount": 11, "speedMbps": 6.8, "queueLength": 0, "freeSlot": True,
     "coverage": "2/2", "coverageFull": True, "flags": ["not flac"],
     "recommendation": "", "score": 3300, "rank": 4, "recommended": False},
    {"id": 4, "peer": "tape_ghost", "folder": "trip hop/mezzanine", "format": "FLAC",
     "bitrate": "16-bit 44.1 kHz", "size": "80.1 MB",
     "fileCount": 9, "speedMbps": 1.4, "queueLength": 3, "freeSlot": False,
     "coverage": "1/2", "coverageFull": False, "flags": ["missing track"],
     "recommendation": "", "score": 4100, "rank": 5, "recommended": False},
]

PREFS = {
    "readOnly": False,
    "ranks": [{"key": "flac", "label": "FLAC", "priority": 0},
              {"key": "opus", "label": "OPUS", "priority": 1}],
    "fallback": "best",
    "guards": {"requireFullCoverage": False, "maxAlbumSizeMB": 0,
               "minSpeedMbps": 0.0, "maxQueueLength": 0},
    "fixed": {"minAvailabilityRatio": 0.5, "stallTimeoutSeconds": 120},
}

SEARCHES = {}  # sid -> search snapshot; populated by POST /api/search

def gap_detail(gid):
    row = next((g for g in GAP_ITEMS if g["id"] == gid), GAP_ITEMS[0])
    d = dict(row)
    d.update({
        "tracks": [
            {"position": 1, "title": "Angel", "artist": row["artist"], "state": "present", "downloadError": ""},
            {"position": 4, "title": "Inertia Creeps", "artist": row["artist"], "state": "missing", "downloadError": ""},
            {"position": 7, "title": "Group Four", "artist": row["artist"], "state": "failed",
             "downloadError": "peer rejected transfer"},
        ],
        "sources": SOURCES, "sourcesTotal": len(SOURCES), "sourcesPage": 0, "sourcesPages": 1,
        "sourceQuery": f"{row['artist']} {row['album']}",
        "failReason": "watchdog_cancelled" if row["status"] == "failed" else "",
        "failDetail": "No bytes received for 120s — transfer cancelled, next source available."
                      if row["status"] == "failed" else "",
    })
    return d

ROUTES = {
    "/api/summary": lambda q: {
        "gaps": {"needs": 2, "working": 1, "done": 1, "all": 4},
        "scan": {"status": "complete", "message": "Found 4 album review group(s)"},
        "transfers": {"active": 1, "queued": 1, "failed": 1, "done": 2, "needsPlacement": 1},
        "activeTransfers": [{"id": "t1", "title": "Bonobo — Kerala", "state": "active", "pct": 46}],
        "library": {"albums": 3142, "withGaps": 187},
        "ts": time.time(),
    },
    "/api/gaps": lambda q: {"items": GAP_ITEMS,
                            "counts": {"needs": 2, "working": 1, "done": 1, "all": 4},
                            "scanStatus": "complete", "scanMessage": "Found 4 group(s)",
                            "updatedAt": time.time()},
    "/api/transfers": lambda q: {
        "transfers": [
            {"id": "t1", "kind": "track", "title": "Bonobo — Kerala", "sub": "from @beatfiend",
             "state": "active", "stateDetail": "InProgress", "pct": 46,
             "bytesTotal": 38000000, "bytesDone": 17480000, "rate": 2400000, "etaSeconds": 9,
             "error": "", "groupId": "g3", "username": "beatfiend", "filename": "kerala.flac"},
            {"id": "t2", "kind": "album", "title": "Kiasmos — Kiasmos", "sub": "from @nordic_waves",
             "state": "active", "stateDetail": "3/10 files", "pct": 30, "groupId": "g2"},
            {"id": "t3", "kind": "track", "title": "Massive Attack — Group Four", "sub": "from @slowpoke99",
             "state": "failed", "stateDetail": "Cancelled", "pct": 0,
             "error": "stall watchdog: no bytes in 120s", "groupId": "g1",
             "username": "slowpoke99", "filename": "group four.flac"},
        ],
        "needsPlacement": [
            {"id": "p1", "name": "Rival Consoles - Persona (2018) FLAC", "path": "/downloads/Persona",
             "fileCount": 8, "formats": "FLAC",
             "match": {"group_id": "g4", "artist": "Rival Consoles", "album": "Persona", "missing": 8},
             "matchLabel": "Rival Consoles — Persona", "confidence": "likely",
             "diff": {"filesFound": 8, "willFill": 8}},
        ],
        "counts": {"active": 2, "queued": 0, "failed": 1, "done": 0, "needsPlacement": 1},
    },
    "/api/library": lambda q: {
        "items": [
            {"id": "al1", "artist": "Massive Attack", "album": "Mezzanine", "year": 1998,
             "trackCount": 11, "present": 9, "total": 11, "status": "failed", "groupId": "g1",
             "coverUrl": "/api/cover/al1?size=96"},
            {"id": "al2", "artist": "Kiasmos", "album": "Kiasmos", "year": 2014,
             "trackCount": 10, "present": 7, "total": 10, "status": "ready", "groupId": "g2",
             "coverUrl": "/api/cover/al2?size=96"},
            {"id": "al5", "artist": "Air", "album": "Moon Safari", "year": 1998,
             "trackCount": 10, "present": 10, "total": 10, "status": "complete", "groupId": "",
             "coverUrl": "/api/cover/al5?size=96"},
        ],
        "total": 3, "page": 0, "pages": 1, "per": 50,
        "libraryTotals": {"albums": 3142, "withGaps": 187},
    },
    "/api/system/health": lambda q: {"checks": [
        {"id": "navidrome", "ok": True, "label": "Navidrome", "detail": "http://navidrome:4533 (200)", "howToFix": ""},
        {"id": "slskd", "ok": False, "label": "slskd", "detail": "http://slskd:5030 unreachable",
         "howToFix": "Check the matching URL, credentials, and container networking."},
    ], "lastRun": time.time()},
    "/api/prefs": lambda q: PREFS,
    "/api/logs": lambda q: {"entries": [
        {"ts": "2026-07-05 11:02:11", "epoch": time.time(), "tag": "slskd", "severity": "error",
         "msg": "stall watchdog: Group Four stuck at 0 bytes for 122s — cancelling"},
        {"ts": "2026-07-05 11:02:14", "epoch": time.time(), "tag": "task", "severity": "info",
         "msg": "advancing to next source @vinyl_rip_guy"},
        {"ts": "2026-07-05 11:02:20", "epoch": time.time(), "tag": "navidrome", "severity": "info",
         "msg": "rescan requested for /music/Massive Attack/Mezzanine"},
    ], "events": []},
    "/api/album/lookup": lambda q: {"candidates": [
        {"rgid": "rg-mez", "title": "Mezzanine", "artist": "Massive Attack",
         "primary_type": "album", "year": "1998", "score": 100},
        {"rgid": "rg-mez-v", "title": "Mezzanine (The Remixes)", "artist": "Massive Attack",
         "primary_type": "album", "year": "1999", "score": 71},
    ]},
    "/api/album/releases": lambda q: {
        "rgid": "rg-mez", "title": "Mezzanine", "artist": "Massive Attack",
        "coverUrl": "", "releases": [
            {"releaseMbid": "rel-1", "title": "Mezzanine", "disambiguation": "",
             "year": "1998", "country": "GB", "trackCount": 11},
            {"releaseMbid": "rel-2", "title": "Mezzanine", "disambiguation": "remastered",
             "year": "2018", "country": "GB", "trackCount": 11},
            {"releaseMbid": "rel-3", "title": "Mezzanine", "disambiguation": "deluxe edition",
             "year": "2019", "country": "XW", "trackCount": 26},
        ]},
    "/api/searches": lambda q: SEARCHES,
    "/api/review": lambda q: {"groups": [], "tasks": {}, "operations": {}, "status": "idle", "message": ""},
    "/api/tasks": lambda q: {},
    "/api/operations": lambda q: {},
    "/api/downloads": lambda q: {"slskd": [], "bot_pending": [], "album_groups": [], "review": [], "repair_jobs": []},
    "/api/action-center": lambda q: {"cards": [], "buckets": {}, "groups": [], "summary": {}},
}

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=DIST, **kw)

    def log_message(self, *a):
        pass

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n)) if n else {}
        except Exception:
            return {}

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/api/cover/"):
            self.send_response(204)
            self.end_headers()
            return
        if path.startswith("/api/gaps/"):
            return self._json(gap_detail(path.rsplit("/", 1)[-1]))
        if path in ROUTES:
            return self._json(ROUTES[path](None))
        if path.startswith("/api/"):
            return self._json({"error": "not stubbed"}, 404)
        if not os.path.exists(os.path.join(DIST, path.lstrip("/"))):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        path = self.path.split("?")[0]
        if path.endswith("/fetch"):
            gid = path.split("/")[-2]
            if gid == "g1":
                # Keep one album on the failure branch so the recovery card
                # (try next / pick manually / retry) stays demoable.
                return self._json({"ok": False, "code": "enqueue_failed",
                                   "reason": "peer rejected the transfer",
                                   "detail": "hasFreeUploadSlot was stale at enqueue time",
                                   "nextSource": SOURCES[1],
                                   "logTail": ["11:02:11  slskd rejected enqueue"],
                                   "error": "peer rejected the transfer"}, 400)
            detail = gap_detail(gid)
            detail["status"] = "downloading"
            return self._json({"ok": True, "gap": detail})
        if path == "/api/search":
            body = self._body()
            sid = f"s{len(SEARCHES) + 1}"
            SEARCHES[sid] = {"id": sid, "query": body.get("query", ""), "state": "complete",
                             "folders": [
                                 {"folder": "music/Massive Attack - Mezzanine [FLAC]",
                                  "username": "vinyl_rip_guy", "upload_speed": 4400000,
                                  "queue_length": 0,
                                  "files": [{"filename": f"{i:02d} track.flac", "size": 30000000}
                                            for i in range(1, 12)]},
                                 {"folder": "MA/Mezzanine opus",
                                  "username": "slowpoke99", "upload_speed": 400000,
                                  "queue_length": 7,
                                  "files": [{"filename": f"{i:02d} track.opus", "size": 4000000}
                                            for i in range(1, 9)]},
                             ]}
            return self._json({"ok": True, "task_id": sid})
        return self._json({"ok": True})

    def do_PUT(self):
        path = self.path.split("?")[0]
        if path == "/api/prefs":
            body = self._body()
            if "fallback" in body:
                PREFS["fallback"] = body["fallback"]
            if "guards" in body and isinstance(body["guards"], dict):
                PREFS["guards"].update(body["guards"])
            if "ranks" in body and isinstance(body["ranks"], list):
                PREFS["ranks"] = [{"key": k, "label": k.upper(), "priority": i}
                                  for i, k in enumerate(body["ranks"])]
            return self._json({"ok": True, "prefs": PREFS})
        return self._json({"error": "not stubbed"}, 404)

if __name__ == "__main__":
    print("stub on http://127.0.0.1:8898")
    # Threading: the SPA fires parallel keep-alive requests; a single-threaded
    # server wedges on the first stalled connection.
    ThreadingHTTPServer(("127.0.0.1", 8898), Handler).serve_forever()
