# BUILD_NOTES.md — Docker build time

Tracking the ~30-minute container build and how to bring it down. Companion to
`CLAUDE.md` (pipeline) and `UI_PLAN.md` (frontend).

## Dependency inventory

| Dependency | Where | Status |
| ---------- | ----- | ------ |
| `python-telegram-bot`, `httpx`, `requests`, `Flask` | `requirements.txt` | light, prebuilt wheels |
| `mutagen` | `requirements.txt` (now explicit) | light; tag read/write + work item A placement |
| `beets` | `Dockerfile` `pip install beets` | **heavy**; album-import path + `/api/beets/*` |
| `ffmpeg` | `Dockerfile` apt | **large**; only for beets' replaygain/convert plugins |
| ~~`pyacoustid`~~ | removed | was unused (0 references) |
| ~~`pylast`~~ | removed | was unused (0 references) |

`ffmpeg` is never called by lb-bot itself — the only references are
error-handling strings about beets plugins failing without it. It is coupled to
beets and goes away with beets.

## Tier 1 — applied

Done in this branch, safe, no behaviour change:

1. **Dropped `pyacoustid` and `pylast`.** Both had zero references in
   `listenbrainz_bot.py`. Removed from the `pip install` line.
2. **BuildKit pip cache mount.** `RUN --mount=type=cache,target=/root/.cache/pip`
   on the pip layers keeps the wheel cache across rebuilds, so packages are not
   re-downloaded/re-compiled each time. Needs BuildKit — default on Docker 23+;
   otherwise build with `DOCKER_BUILDKIT=1 docker build ...` (or
   `docker buildx build ...`). The `# syntax=docker/dockerfile:1` header enables
   the mount syntax.
3. **Declared `mutagen` explicitly** in `requirements.txt`. It was only present
   because beets pulled it in; work item A needs it directly, and it must survive
   beets removal.

### Note on clean vs. incremental builds

The Dockerfile orders layers so editing only `listenbrainz_bot.py` rebuilds just
the final `COPY` (seconds). A full ~30 min build therefore means the cache is not
being reused — typically a fresh builder with no persistent layer cache, a
`--no-cache` build, or a `requirements.txt` change. The pip cache mount above
specifically targets that case; for layer cache, build on a host/runner that
persists `/var/lib/docker` (or use `buildx` with `--cache-to/--cache-from`).

## Tier 2 — remove beets entirely (the real prize)

This is the largest reduction: dropping `beets` removes its whole dependency tree
**and** lets `ffmpeg` go with it. It is **not** covered by work items A/B as
scoped — A only removes beets from the *per-track repair placement*. The
*album-import path* still shells out to `beet`.

### Remaining beets call sites to migrate (album path)

Navigate by name; line numbers drift.

- `_run_beets_cmd` — the subprocess wrapper (all `beet` calls funnel through it)
- `_beets_import_task`, `_beets_import_result`, `_beets_import_selected_result`,
  `_beets_import_preview` — album import + preview
- `diagnose_beets_import`, `_beets_import_simulation_checks` — diagnostics
- `_trusted_pinned_merge`, `_verify_trusted_beets_import`,
  `_trusted_profile_preflight`, `_cleanup_stale_trusted_recordings` — the trusted
  merge chain (work item A supersedes the *repair* use of these)
- `_beets_profile_config`, `_beets_base_cmd` — config generation
- `/api/beets/folders`, `/api/beets/release-candidates`, `/api/beets/import`,
  `/api/beets/import-preview`, `/api/beets/diagnose` — web routes
- Config/env: `BEETS_ENABLED`, `BEETS_CMD`, `BEETS_CONFIG`, `BEETS_LIBRARY`,
  `_BEETS_MOVES`, `_BEETS_RELOCATES`

### Migration sketch

The album path can move to the same deterministic, mutagen-based placement work
item A introduces for single tracks: resolve target folder → tag with mutagen →
move → Navidrome verify, looped over an album's tracks. Once every call site
above is migrated and the `/api/beets/*` routes are removed (or repointed at the
deterministic path), drop from the Dockerfile:

```dockerfile
# remove:
RUN --mount=... pip install beets
# and the ffmpeg apt layer
```

Expected payoff: beets + transitive deps + the ffmpeg apt layer — the bulk of the
non-trivial build time and a meaningful image-size cut.

### Sequencing

Do work items **A** and **B** first (they make the tool work). Tier 2 is a natural
follow-on once A's deterministic placement is proven on the live stack — reuse it
for the album path, then delete beets.
