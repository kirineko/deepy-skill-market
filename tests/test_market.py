from __future__ import annotations

import io
import zipfile

from fastapi.testclient import TestClient

from app.main import create_app


def _skill_zip(name: str = "demo", description: str = "Demo skill") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{name}/SKILL.md",
            f"---\nname: {name}\ndescription: {description}\nlicense: MIT\n---\nUse demo.",
        )
    return buf.getvalue()


def test_upload_search_and_download(tmp_path):
    client = TestClient(create_app(tmp_path, admin_token="secret"))

    response = client.post(
        "/admin/skills/upload",
        headers={"x-admin-token": "secret"},
        files={"package": ("demo.zip", _skill_zip(), "application/zip")},
    )
    assert response.status_code == 200
    assert response.json()["skill"]["name"] == "demo"

    listing = client.get("/api/skills?q=demo")
    assert listing.status_code == 200
    assert listing.json()["skills"][0]["name"] == "demo"

    download = client.get("/api/skills/demo/download")
    assert download.status_code == 200
    assert download.content.startswith(b"PK")


def test_upload_rejects_invalid_zip(tmp_path):
    client = TestClient(create_app(tmp_path, admin_token="secret"))

    response = client.post(
        "/admin/skills/upload",
        headers={"x-admin-token": "secret"},
        files={"package": ("bad.zip", b"not a zip", "application/zip")},
    )
    assert response.status_code == 400


def test_upload_requires_admin_token(tmp_path):
    client = TestClient(create_app(tmp_path, admin_token="secret"))

    response = client.post(
        "/admin/skills/upload",
        headers={"x-admin-token": "wrong"},
        files={"package": ("demo.zip", _skill_zip(), "application/zip")},
    )
    assert response.status_code == 401


def _frontmatter_zip(frontmatter: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("demo/SKILL.md", f"---\nname: demo\n{frontmatter}\n---\nBody")
    return buf.getvalue()


def _upload(client, frontmatter):
    return client.post(
        "/admin/skills/upload",
        headers={"x-admin-token": "secret"},
        files={"package": ("demo.zip", _frontmatter_zip(frontmatter), "application/zip")},
    )


def test_metadata_and_category_roundtrip_and_update(tmp_path):
    client = TestClient(create_app(tmp_path, admin_token="secret"))
    response = _upload(client, 'category: development\nmetadata:\n  source: Vercel\n  version: "1.0"\n  author: Example\n  nested:\n    enabled: true')
    assert response.status_code == 200
    expected = {"source": "Vercel", "version": "1.0", "author": "Example", "nested": {"enabled": True}}
    for skill in [response.json()["skill"], client.get("/api/skills/demo").json(), client.get("/api/skills").json()["skills"][0]]:
        assert skill["metadata"] == expected
        assert skill["category"] == "development"
        assert skill["version"] == "1.0"
    response = _upload(client, 'category: documents\nmetadata: {"source": "Anthropic", "custom": "kept"}')
    assert response.status_code == 200
    detail = client.get("/api/skills/demo").json()
    assert detail["category"] == "documents"
    assert detail["metadata"] == {"source": "Anthropic", "custom": "kept"}


def test_json_string_metadata_and_defaults(tmp_path):
    client = TestClient(create_app(tmp_path, admin_token="secret"))
    response = _upload(client, '''metadata: '{"source":"Anthropic","author":"someone"}' ''')
    assert response.status_code == 200
    assert response.json()["skill"]["metadata"]["source"] == "Anthropic"
    assert response.json()["skill"]["category"] == ""
    response = _upload(client, "description: no metadata")
    assert response.status_code == 200
    assert response.json()["skill"]["metadata"] == {"source": ""}


def test_invalid_metadata_rejected_without_creating_skill(tmp_path):
    client = TestClient(create_app(tmp_path, admin_token="secret"))
    for frontmatter in ["metadata: [a, b]", "metadata: not-json", "metadata:\n  source: [bad]", "category: [bad]", "metadata: {broken", "metadata:\n  timestamp: 2026-01-01"]:
        response = _upload(client, frontmatter)
        assert response.status_code == 400, response.text
    assert client.get("/api/skills").json() == {"skills": []}
    assert not list((tmp_path / "zips").iterdir())


def test_legacy_database_migration_and_metadata(tmp_path):
    import sqlite3
    from app.main import init_db

    client = TestClient(create_app(tmp_path, admin_token="secret"))
    assert _upload(client, "description: old").status_code == 200
    db = tmp_path / "market.db"
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE skills DROP COLUMN category")
        conn.execute("UPDATE skills SET metadata_json = ?", ('{"source":"Vercel","author":"old"}',))
    init_db(db)
    init_db(db)  # Repeated startup must be safe.
    detail = client.get("/api/skills/demo").json()
    assert detail["category"] == ""
    assert detail["metadata"] == {"source": "Vercel", "author": "old"}
    for raw, expected in [("", {"source": ""}), ("unstructured legacy text", {"source": "", "legacy_raw": "unstructured legacy text"})]:
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE skills SET metadata_json = ?", (raw,))
        assert client.get("/api/skills/demo").json()["metadata"] == expected
    assert client.get("/api/skills/demo/download").status_code == 200
