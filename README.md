# Deepy Skill Market

Minimal skill market service for Deepy. Administrators upload Agent Skill zip packages, and Deepy clients browse, download, install, uninstall, and update those packages.

## Run With Docker Compose

```bash
cp .env.example .env
docker compose up -d --build
```

The service listens on `http://localhost:8000`.

## Configuration

- `SKILL_MARKET_ADMIN_TOKEN`: token required for admin uploads.
- `SKILL_MARKET_DATA_DIR`: directory for SQLite database, uploaded zip files, and extracted snapshots. Defaults to `data` locally and `/data` in Docker.

## API

Public:

- `GET /api/health`
- `GET /api/skills`
- `GET /api/skills?q=<query>`
- `GET /api/skills/{name}`
- `GET /api/skills/{name}/download`

Admin:

```bash
curl -X POST http://localhost:8000/admin/skills/upload \
  -H "x-admin-token: $SKILL_MARKET_ADMIN_TOKEN" \
  -F "package=@demo-skill.zip"
```

## Package Rules

Each uploaded zip must contain exactly one valid `SKILL.md`. The service stores:

- metadata parsed from `SKILL.md`
- upload time
- original filename
- package size
- package SHA-256
- original zip file
- extracted skill snapshot

## Local Development

```bash
uv sync --group dev
uv run pytest
uv run uvicorn app.main:app --reload
```
