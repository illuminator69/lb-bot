# ── Stage 1: build the React SPA ─────────────────────────────────────────────
FROM node:22-slim AS web
WORKDIR /web
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ── Stage 2: Python runtime ───────────────────────────────────────────────────
FROM python:3.11-slim
WORKDIR /app
# fpcalc (Chromaprint) — the fingerprinter behind the AcoustID placement check.
# A few MB, and pyacoustid shells out to it, so pip alone is not enough.
# Without it `_acoustid_recording_mbids` reports "no opinion" and placement is
# unchanged, so the image still builds and runs if this line ever fails.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libchromaprint-tools \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY listenbrainz_bot.py .
COPY --from=web /web/dist ./web/dist
EXPOSE 8899
CMD ["python", "-u", "listenbrainz_bot.py"]
