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
