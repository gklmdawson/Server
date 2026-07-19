"""HTTP surface for the NAS helper: /intake/probe, /intake/upload,
/intake/parse-ecef. Path-jailing mirrors /browse."""
import os

import pytest
from fastapi.testclient import TestClient

from coordinator.main import create_app
from tests.conftest import make_config


@pytest.fixture
def helper_client(tmp_path):
    share = tmp_path / "share"
    (share / "flight").mkdir(parents=True)
    (share / "flight" / "notes.txt").write_text("not an image")
    uploads = tmp_path / "uploads"
    cfg = make_config(
        tmp_path,
        browse_roots={"share": {"path": str(share), "display": "\\\\NAS\\share"}},
        upload_dir=str(uploads),
        max_upload_bytes=1024,
    )
    with TestClient(create_app(cfg)) as c:
        yield c, share


def test_probe_no_images_defaults_sensor(helper_client):
    client, _ = helper_client
    r = client.get("/api/v1/intake/probe", params={"root": "share", "path": "flight"})
    assert r.status_code == 200
    assert r.json()["sensor"] == "R3ProMobile"


def test_probe_rejects_unknown_root_and_traversal(helper_client):
    client, _ = helper_client
    assert client.get("/api/v1/intake/probe", params={"root": "nope"}).status_code == 404
    assert client.get("/api/v1/intake/probe",
                      params={"root": "share", "path": "../.."}).status_code == 400


def test_upload_stores_file_and_returns_path(helper_client):
    client, _ = helper_client
    r = client.post("/api/v1/intake/upload",
                    files={"file": ("1234.T02", b"basedata", "application/octet-stream")})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "1234.T02"
    assert body["size"] == len(b"basedata")
    assert os.path.isfile(body["stored_path"])


def test_upload_enforces_size_limit(helper_client):
    client, _ = helper_client
    r = client.post("/api/v1/intake/upload",
                    files={"file": ("big.bin", b"x" * 2048, "application/octet-stream")})
    assert r.status_code == 413


def test_parse_ecef_valid_and_invalid(helper_client):
    client, _ = helper_client
    good = b"Point ID,X (ECEF),Y (ECEF),Z (ECEF)\nB,-1878522.21,-4599428.34,4001432.17\n"
    r = client.post("/api/v1/intake/parse-ecef", files={"file": ("base.csv", good, "text/csv")})
    assert r.status_code == 200
    assert r.json()["ecef"] == [-1878522.21, -4599428.34, 4001432.17]

    bad = b"a,b,c,d\n1,2,3,4\n"
    r = client.post("/api/v1/intake/parse-ecef", files={"file": ("bad.csv", bad, "text/csv")})
    assert r.status_code == 400


def test_extract_tlt_from_uploaded_targets(helper_client):
    client, _ = helper_client
    # Upload a targets csv, then preview the TLT extraction on the stored copy.
    targets = b"p1,1,2,3,TLT\np2,4,5,6,CHK\np3,7,8,9,tlt\n"
    up = client.post("/api/v1/intake/upload",
                     files={"file": ("targets.csv", targets, "text/csv")})
    assert up.status_code == 201, up.text
    stored = up.json()["stored_path"]

    r = client.post("/api/v1/intake/extract-tlt", json={"stored_path": stored})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tlt_count"] == 2 and body["total_rows"] == 3
    assert os.path.basename(body["stored_path"]) == "SINGLE_TLT.csv"
    assert os.path.isfile(body["stored_path"])


def test_extract_tlt_no_tlt_rows_is_400(helper_client):
    client, _ = helper_client
    up = client.post("/api/v1/intake/upload",
                     files={"file": ("targets.csv", b"p1,1,2,3,CHK\n", "text/csv")})
    stored = up.json()["stored_path"]
    r = client.post("/api/v1/intake/extract-tlt", json={"stored_path": stored})
    assert r.status_code == 400


def test_extract_tlt_rejects_path_outside_uploads(helper_client, tmp_path):
    client, _ = helper_client
    outside = tmp_path / "outside.csv"
    outside.write_text("p1,1,2,3,TLT\n")
    r = client.post("/api/v1/intake/extract-tlt", json={"stored_path": str(outside)})
    assert r.status_code == 400
