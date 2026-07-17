"""Browse endpoint: root listing, folder listing with UNC display paths,
and traversal protection."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from coordinator.main import create_app
from tests.conftest import make_config


@pytest.fixture
def share(tmp_path):
    root = tmp_path / "share"
    (root / "Brahma" / "SilverPeak" / "11Jun2026").mkdir(parents=True)
    (root / "Brahma" / "SilverPeak" / "11Jun2026" / "SINGLE_TLT.csv").write_text("gcp")
    (root / "Brahma" / "SilverPeak" / "11Jun2026" / "06131625.T02").write_bytes(b"x" * 32)
    (root / "Brahma" / "SilverPeak" / "11Jun2026" / "photo.JPG").write_bytes(b"j")
    (root / "@eaDir").mkdir()          # NAS junk — hidden from listings
    (root / ".hidden.txt").write_text("no")
    (root / "outside.txt")             # not created; the escape target below
    return root


@pytest.fixture
def browse_client(tmp_path, share):
    cfg = make_config(
        tmp_path,
        browse_roots={"3dData": {"path": str(share),
                                 "display": "\\\\192.168.35.25\\3dData"}},
    )
    with TestClient(create_app(cfg)) as c:
        yield c


def test_roots_listing(browse_client):
    data = browse_client.get("/api/v1/browse").json()
    assert data["roots"] == [
        {"label": "3dData", "display": "\\\\192.168.35.25\\3dData"}]


def test_roots_empty_when_unconfigured(client):
    assert client.get("/api/v1/browse").json() == {"roots": []}


def test_listing_root_and_nested(browse_client):
    top = browse_client.get("/api/v1/browse", params={"root": "3dData"}).json()
    assert top["display_path"] == "\\\\192.168.35.25\\3dData"
    assert top["parent"] is None
    assert [e["name"] for e in top["entries"]] == ["Brahma"]  # junk filtered

    deep = browse_client.get(
        "/api/v1/browse",
        params={"root": "3dData", "path": "Brahma/SilverPeak/11Jun2026"},
    ).json()
    assert deep["display_path"] == "\\\\192.168.35.25\\3dData\\Brahma\\SilverPeak\\11Jun2026"
    assert deep["parent"] == "Brahma/SilverPeak"
    assert deep["sep"] == "\\"
    names = [e["name"] for e in deep["entries"]]
    assert names == ["06131625.T02", "photo.JPG", "SINGLE_TLT.csv"]
    t02 = next(e for e in deep["entries"] if e["name"] == "06131625.T02")
    assert not t02["dir"] and t02["size"] == 32


def test_backslash_paths_accepted(browse_client):
    data = browse_client.get(
        "/api/v1/browse",
        params={"root": "3dData", "path": "Brahma\\SilverPeak"},
    ).json()
    assert [e["name"] for e in data["entries"]] == ["11Jun2026"]


def test_traversal_rejected(browse_client):
    r = browse_client.get(
        "/api/v1/browse", params={"root": "3dData", "path": "../outside"})
    assert r.status_code == 400

    r = browse_client.get(
        "/api/v1/browse", params={"root": "3dData", "path": "Brahma/../.."})
    assert r.status_code == 400


def test_unknown_root_and_missing_folder(browse_client):
    assert browse_client.get(
        "/api/v1/browse", params={"root": "nope"}).status_code == 404
    assert browse_client.get(
        "/api/v1/browse", params={"root": "3dData", "path": "NoSuch"}
    ).status_code == 404


def test_admin_token_required_when_configured(tmp_path, share):
    cfg = make_config(
        tmp_path,
        admin_token="secret",
        browse_roots={"3dData": {"path": str(share), "display": "X:"}},
    )
    with TestClient(create_app(cfg)) as c:
        assert c.get("/api/v1/browse").status_code == 401
        ok = c.get("/api/v1/browse",
                   headers={"Authorization": "Bearer secret"})
        assert ok.status_code == 200
