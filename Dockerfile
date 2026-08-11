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
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY listenbrainz_bot.py .
COPY --from=web /web/dist ./web/dist
EXPOSE 8899
CMD ["python", "-u", "listenbrainz_bot.py"]
