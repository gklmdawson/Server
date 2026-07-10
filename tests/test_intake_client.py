"""The intake payload builder against the real coordinator API."""
from intake.queue_client import build_project_payload


def _full_payload():
    return build_project_payload(
        client="Brahma", project="SilverPeak", date="10Jul2026",
        sensor_type="L3", date_folder="//UGREEN/Active/Brahma/SilverPeak/10Jul2026",
        ppk=dict(data_source="//nas/flight", terra_path="//nas/Terra",
                 ppk_path="//nas/PPK", epsg_h="6523", epsg_v="6360"),
        pix4d=dict(project_root="//nas/date", tat_path="//nas/tat.csv",
                   epsg_h="6523", epsg_v="6360"),
        lidar=dict(data_source="//nas/L3", project_location="//nas/Terra",
                   gcp_path="//nas/tat.csv", epsg_h="6523", epsg_v="6360"),
        classify_model="Heavy Construction UAV 2.0",
    )


def test_payload_shape():
    payload = _full_payload()
    assert payload["name"] == "SilverPeak"
    templates = [c["template"] for c in payload["chains"]]
    assert templates == ["photo_ppk", "lidar"]

    photo = payload["chains"][0]["parameters"]
    assert photo["TERRA_PPK"]["project_name"] == "Brahma_SilverPeak_10Jul2026"
    assert photo["PIX4D_MATIC"]["tat_path"] == "//nas/tat.csv"

    lidar = payload["chains"][1]["parameters"]
    assert lidar["CYCLONE_CLASSIFY"]["project_name"] == "Brahma_SilverPeak_10Jul2026_LiDAR"
    assert lidar["CYCLONE_CLASSIFY"]["terra_folder"] == "//nas/Terra"


def test_payload_accepted_by_coordinator(client):
    r = client.post("/api/v1/projects", json=_full_payload())
    assert r.status_code == 201, r.text
    jobs = r.json()["jobs"]
    assert [j["job_type"] for j in jobs] == [
        "TERRA_PPK", "PIX4D_MATIC", "TERRA_LIDAR", "CYCLONE_CLASSIFY"]

    by_type = {j["job_type"]: j for j in jobs}
    assert by_type["PIX4D_MATIC"]["depends_on"] == [by_type["TERRA_PPK"]["job_uuid"]]
    assert by_type["CYCLONE_CLASSIFY"]["depends_on"] == [by_type["TERRA_LIDAR"]["job_uuid"]]


def test_ppk_only_payload():
    payload = build_project_payload(
        client="C", project="P", date="D",
        ppk=dict(data_source="//nas/flight", terra_path="//nas/Terra",
                 ppk_path="//nas/PPK"),
    )
    assert len(payload["chains"]) == 1
    assert "PIX4D_MATIC" not in payload["chains"][0]["parameters"]
