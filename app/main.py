from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse


APP_TITLE = "Deepy Skill Market"
DEFAULT_DATA_DIR = Path("data")


def create_app(data_dir: Path | None = None, admin_token: str | None = None) -> FastAPI:
    base = data_dir or Path(os.environ.get("SKILL_MARKET_DATA_DIR", DEFAULT_DATA_DIR))
    token = admin_token if admin_token is not None else os.environ.get("SKILL_MARKET_ADMIN_TOKEN", "")
    db_path = base / "market.db"
    zips_dir = base / "zips"
    extracted_dir = base / "extracted"
    for path in (zips_dir, extracted_dir):
        path.mkdir(parents=True, exist_ok=True)
    init_db(db_path)

    app = FastAPI(title=APP_TITLE)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/skills")
    def list_skills(q: str = "") -> dict[str, list[dict[str, Any]]]:
        rows = query_skills(db_path, q)
        return {"skills": [public_skill(row) for row in rows]}

    @app.get("/api/skills/{name}")
    def get_skill(name: str) -> dict[str, Any]:
        row = get_skill_row(db_path, name)
        if row is None:
            raise HTTPException(status_code=404, detail="Skill not found")
        return public_skill(row)

    @app.get("/api/skills/{name}/download")
    def download_skill(name: str) -> FileResponse:
        row = get_skill_row(db_path, name)
        if row is None:
            raise HTTPException(status_code=404, detail="Skill not found")
        path = Path(row["zip_path"])
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Package file not found")
        return FileResponse(path, media_type="application/zip", filename=path.name)

    @app.post("/admin/skills/upload")
    async def upload_skill(
        package: UploadFile = File(...),
        x_admin_token: str = Header(default=""),
    ) -> dict[str, Any]:
        if token and x_admin_token != token:
            raise HTTPException(status_code=401, detail="Invalid admin token")
        blob = await package.read()
        if not blob:
            raise HTTPException(status_code=400, detail="Empty upload")
        digest = hashlib.sha256(blob).hexdigest()
        upload_id = uuid.uuid4().hex
        now = datetime.now(UTC).isoformat()
        with tempfile.TemporaryDirectory(prefix="deepy-market-") as tmp:
            tmp_path = Path(tmp)
            zip_path = tmp_path / "upload.zip"
            zip_path.write_bytes(blob)
            extract_path = tmp_path / "extract"
            extract_path.mkdir()
            try:
                safe_extract(zip_path, extract_path)
                skill_md = find_single_skill(extract_path)
                frontmatter, _body = split_frontmatter(skill_md.read_text(encoding="utf-8"))
                metadata = parse_metadata(frontmatter.get("metadata"))
                category = frontmatter.get("category", "")
                if not isinstance(category, str):
                    raise ValueError("category must be a string")
                category = category.strip()
            except (OSError, ValueError, zipfile.BadZipFile) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            name = clean_scalar(frontmatter.get("name")) or skill_md.parent.name
            description = clean_scalar(frontmatter.get("description"))
            if not name:
                raise HTTPException(status_code=400, detail="Skill name is required")
            version_value = frontmatter.get("version") or metadata.get("version")
            version = str(version_value).strip() if isinstance(version_value, (str, int, float)) else ""
            version = version or digest[:12]
            stored_zip = zips_dir / f"{name}-{upload_id}.zip"
            stored_extract = extracted_dir / f"{name}-{upload_id}"
            stored_zip.write_bytes(blob)
            shutil.copytree(skill_md.parent, stored_extract)
        skill_id, version_id = upsert_skill_version(
            db_path,
            name=name,
            description=description,
            license_value=clean_scalar(frontmatter.get("license")),
            compatibility=clean_scalar(frontmatter.get("compatibility")),
            metadata_json=json.dumps(metadata, ensure_ascii=False),
            category=category,
            version=version,
            filename=package.filename or stored_zip.name,
            zip_path=str(stored_zip),
            extracted_path=str(stored_extract),
            sha256=digest,
            size_bytes=len(blob),
            uploaded_at=now,
        )
        return {
            "skill": {
                "id": skill_id,
                "version_id": version_id,
                "name": name,
                "description": description,
                "metadata": metadata,
                "category": category,
                "version": version,
                "sha256": digest,
                "uploaded_at": now,
            }
        }

    return app


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS skills (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                license TEXT NOT NULL DEFAULT '',
                compatibility TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                latest_version_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(skills)")}
        if "category" not in columns:
            conn.execute("ALTER TABLE skills ADD COLUMN category TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS skill_versions (
                id TEXT PRIMARY KEY,
                skill_id TEXT NOT NULL,
                version TEXT NOT NULL,
                filename TEXT NOT NULL,
                zip_path TEXT NOT NULL,
                extracted_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                uploaded_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY(skill_id) REFERENCES skills(id)
            )
            """
        )


def upsert_skill_version(path: Path, **data: Any) -> tuple[str, str]:
    now = data["uploaded_at"]
    version_id = uuid.uuid4().hex
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        existing = conn.execute("SELECT id FROM skills WHERE name = ?", (data["name"],)).fetchone()
        if existing is None:
            skill_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO skills
                (id, name, description, license, compatibility, metadata_json, category, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    skill_id,
                    data["name"],
                    data["description"],
                    data["license_value"],
                    data["compatibility"],
                    data["metadata_json"],
                    data["category"],
                    now,
                    now,
                ),
            )
        else:
            skill_id = existing["id"]
            conn.execute(
                """
                UPDATE skills
                SET description = ?, license = ?, compatibility = ?, metadata_json = ?, category = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    data["description"],
                    data["license_value"],
                    data["compatibility"],
                    data["metadata_json"],
                    data["category"],
                    now,
                    skill_id,
                ),
            )
        conn.execute(
            """
            INSERT INTO skill_versions
            (id, skill_id, version, filename, zip_path, extracted_path, sha256, size_bytes, uploaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                skill_id,
                data["version"],
                data["filename"],
                data["zip_path"],
                data["extracted_path"],
                data["sha256"],
                data["size_bytes"],
                data["uploaded_at"],
            ),
        )
        conn.execute("UPDATE skills SET latest_version_id = ? WHERE id = ?", (version_id, skill_id))
    return skill_id, version_id


def query_skills(path: Path, query: str = "") -> list[sqlite3.Row]:
    sql = """
        SELECT s.*, v.version, v.sha256, v.size_bytes, v.uploaded_at, v.zip_path
        FROM skills s
        JOIN skill_versions v ON v.id = s.latest_version_id
        WHERE v.active = 1
    """
    params: tuple[str, ...] = ()
    if query:
        sql += " AND (s.name LIKE ? OR s.description LIKE ?)"
        like = f"%{query}%"
        params = (like, like)
    sql += " ORDER BY s.name"
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return list(conn.execute(sql, params))


def get_skill_row(path: Path, name: str) -> sqlite3.Row | None:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """
            SELECT s.*, v.version, v.sha256, v.size_bytes, v.uploaded_at, v.zip_path
            FROM skills s
            JOIN skill_versions v ON v.id = s.latest_version_id
            WHERE s.name = ? AND v.active = 1
            """,
            (name,),
        ).fetchone()


def public_skill(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "name": row["name"],
        "description": row["description"],
        "license": row["license"],
        "compatibility": row["compatibility"],
        "metadata": legacy_metadata(row["metadata_json"]),
        "category": row["category"],
        "version": row["version"],
        "sha256": row["sha256"],
        "size_bytes": row["size_bytes"],
        "uploaded_at": row["uploaded_at"],
    }


def safe_extract(zip_path: Path, dest: Path) -> None:
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            target = (dest / member.filename).resolve()
            if not target.is_relative_to(dest.resolve()):
                raise ValueError(f"Unsafe path in zip: {member.filename}")
        archive.extractall(dest)


def find_single_skill(root: Path) -> Path:
    candidates = [path for path in root.rglob("SKILL.md") if path.is_file()]
    if not candidates:
        raise ValueError("Package does not contain SKILL.md")
    if len(candidates) > 1:
        raise ValueError("Package contains multiple SKILL.md files")
    return candidates[0]


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return parse_simple_yaml(lines[1:index]), "\n".join(lines[index + 1 :])
    return {}, text


def parse_simple_yaml(lines: list[str]) -> dict[str, Any]:
    try:
        data = yaml.safe_load("\n".join(lines))
    except yaml.YAMLError as exc:
        raise ValueError("Invalid YAML frontmatter") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("Frontmatter must be an object")
    return data


def clean_scalar(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def parse_metadata(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        value = {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("metadata must be an object or a JSON object string") from exc
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("metadata must be an object with string keys")
    result = dict(value)
    source = result.get("source", "")
    if not isinstance(source, str):
        raise ValueError("metadata.source must be a string")
    result["source"] = source.strip()
    try:
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("metadata must contain JSON-compatible values") from exc
    return result


def legacy_metadata(value: str) -> dict[str, Any]:
    try:
        return parse_metadata(value)
    except ValueError:
        # Keep unstructured legacy content available without breaking list/detail APIs.
        return {"source": "", "legacy_raw": value}


app = create_app()
