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
        {"label": "3dData", "display": "\\\\192.168.35.25\\3dData",
         "ejectable": False}]


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


@pytest.fixture
def ingest(tmp_path):
    """A removable-media root the way UGOS leaves it: one folder per USB
    device it has ever seen, only one of which has a drive mounted now."""
    root = tmp_path / "ingest"
    for dev in ("sda1", "sdb1", "sdc1", "sdd1"):
        (root / dev).mkdir(parents=True)
    (root / "sdb1" / "DCIM").mkdir()
    (root / "leftover.txt").write_text("junk")
    return root


def _fake_st_dev(root, mounted: set[str]):
    """st_dev stand-in: the root and stale dirs share device 100; each mounted
    device dir gets its own id (tests can't create real mount points)."""
    import os

    def fake(path: str):
        name = os.path.basename(os.path.realpath(path))
        if name in mounted:
            return 200 + sorted(mounted).index(name)
        return 100

    return fake


@pytest.fixture
def ingest_client(tmp_path, ingest):
    cfg = make_config(
        tmp_path,
        browse_roots={"ingest": {"path": str(ingest), "display": "/mnt/ingest",
                                 "mounted_only": True}},
    )
    with TestClient(create_app(cfg)) as c:
        yield c


def test_mounted_only_hides_stale_device_dirs(ingest_client, ingest, monkeypatch):
    from coordinator import api

    monkeypatch.setattr(api, "_st_dev", _fake_st_dev(ingest, mounted={"sdb1"}))
    top = ingest_client.get("/api/v1/browse", params={"root": "ingest"}).json()
    # Only the device with a drive behind it survives; loose files untouched.
    assert [e["name"] for e in top["entries"]] == ["sdb1", "leftover.txt"]

    # Inside the mounted device everything shares its st_dev — never filtered.
    inside = ingest_client.get(
        "/api/v1/browse", params={"root": "ingest", "path": "sdb1"}).json()
    assert [e["name"] for e in inside["entries"]] == ["DCIM"]


def test_mounted_only_with_nothing_mounted_lists_no_devices(
        ingest_client, ingest, monkeypatch):
    from coordinator import api

    monkeypatch.setattr(api, "_st_dev", _fake_st_dev(ingest, mounted=set()))
    top = ingest_client.get("/api/v1/browse", params={"root": "ingest"}).json()
    assert [e["name"] for e in top["entries"]] == ["leftover.txt"]


def test_ejectable_implies_mounted_only(tmp_path, ingest, monkeypatch):
    from coordinator import api

    cfg = make_config(
        tmp_path,
        eject_spool_dir=str(tmp_path / "eject"),
        browse_roots={"ingest": {"path": str(ingest), "display": "/mnt/ingest",
                                 "ejectable": True}},
    )
    monkeypatch.setattr(api, "_st_dev", _fake_st_dev(ingest, mounted={"sdc1"}))
    with TestClient(create_app(cfg)) as c:
        top = c.get("/api/v1/browse", params={"root": "ingest"}).json()
        assert [e["name"] for e in top["entries"]] == ["sdc1", "leftover.txt"]


def test_plain_roots_never_filter_by_mount(browse_client):
    # 3dData has no mounted_only/ejectable flag: normal folders on the share
    # (same st_dev as the root) must all keep showing up.
    top = browse_client.get("/api/v1/browse", params={"root": "3dData"}).json()
    assert [e["name"] for e in top["entries"]] == ["Brahma"]


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
