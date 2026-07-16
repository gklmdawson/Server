"""POST /api/v1/intake — the web form's server-side submission builder."""
from tests.helpers import get_job, report, sync


def _body(**overrides):
    body = {
        "root_path": "Z:/Survey/Projects",
        "client": "Brahma",
        "project": "SilverPeak",
        "date": "10Jul2026",
        "sensor_type": "L3",
        "source_folders": ["D:/ingest/DJI_202607100915_002_SilverPeak"],
        "base_data_paths": ["D:/ingest/base/1234.T02"],
        "run_photo_chain": True,
        "run_lidar_chain": True,
        "gcp_path": "Z:/Survey/Projects/targets.csv",
        "epsg_h": "6341",
        "epsg_v": "8228",
        "classify_model": "Heavy Construction UAV 2.0",
    }
    body.update(overrides)
    return body


def _jobs_by_type(resp):
    return {j["job_type"]: j for j in resp["jobs"]}


def test_full_submission_creates_intake_plus_both_chains(client):
    r = client.post("/api/v1/intake", json=_body())
    assert r.status_code == 201, r.text
    jobs = _jobs_by_type(r.json())
    assert set(jobs) == {"INTAKE", "TERRA_PPK", "PIX4D_MATIC",
                         "TERRA_LIDAR", "CYCLONE_CLASSIFY"}

    intake_uuid = jobs["INTAKE"]["job_uuid"]
    assert jobs["INTAKE"]["depends_on"] == []
    assert jobs["TERRA_PPK"]["depends_on"] == [intake_uuid]
    assert jobs["TERRA_LIDAR"]["depends_on"] == [intake_uuid]
    assert jobs["PIX4D_MATIC"]["depends_on"] == [jobs["TERRA_PPK"]["job_uuid"]]
    assert jobs["CYCLONE_CLASSIFY"]["depends_on"] == [jobs["TERRA_LIDAR"]["job_uuid"]]


def test_parameters_match_handle_complete_contract(client):
    r = client.post("/api/v1/intake", json=_body())
    jobs = _jobs_by_type(r.json())

    ppk = get_job(client, jobs["TERRA_PPK"]["job_uuid"])["parameters"]
    date_path = "Z:\\Survey\\Projects\\Brahma\\SilverPeak\\10Jul2026"
    assert ppk["project_name"] == "Brahma_SilverPeak_10Jul2026"
    assert ppk["project_location"] == date_path + "\\PPK"
    assert ppk["terra_path"] == date_path + "\\Terra"
    # Single source folder -> DJI gets that specific flight subfolder.
    assert ppk["data_source"] == date_path + "\\L3\\DJI_202607100915_002_SilverPeak"

    pix = get_job(client, jobs["PIX4D_MATIC"]["job_uuid"])["parameters"]
    assert pix["project_root"] == date_path
    assert pix["tat_path"] == "Z:/Survey/Projects/targets.csv"

    lidar = get_job(client, jobs["TERRA_LIDAR"]["job_uuid"])["parameters"]
    assert lidar["project_location"] == date_path + "\\Terra"
    assert lidar["data_source"] == date_path + "\\L3"

    cyc = get_job(client, jobs["CYCLONE_CLASSIFY"]["job_uuid"])["parameters"]
    assert cyc["project_name"] == "Brahma_SilverPeak_10Jul2026_LiDAR"
    assert cyc["terra_folder"] == date_path + "\\Terra"

    intake = get_job(client, jobs["INTAKE"]["job_uuid"])["parameters"]
    assert intake["source_folders"] == ["D:/ingest/DJI_202607100915_002_SilverPeak"]
    assert intake["base_data_is_rinex"] is False


def test_multiple_sources_use_sensor_folder_as_data_source(client):
    r = client.post("/api/v1/intake", json=_body(
        source_folders=["D:/ingest/flight_a", "D:/ingest/flight_b"]))
    jobs = _jobs_by_type(r.json())
    ppk = get_job(client, jobs["TERRA_PPK"]["job_uuid"])["parameters"]
    assert ppk["data_source"].endswith("\\10Jul2026\\L3")


def test_no_classify_model_skips_cyclone_step(client):
    r = client.post("/api/v1/intake", json=_body(classify_model="",
                                                 run_photo_chain=False))
    jobs = _jobs_by_type(r.json())
    assert set(jobs) == {"INTAKE", "TERRA_LIDAR"}


def test_intake_only_submission(client):
    r = client.post("/api/v1/intake", json=_body(
        sensor_type="R3Pro", run_photo_chain=False, run_lidar_chain=False))
    assert r.status_code == 201
    assert [j["job_type"] for j in r.json()["jobs"]] == ["INTAKE"]


def test_validation_errors(client):
    assert client.post("/api/v1/intake", json=_body(date="2026-07-10")).status_code == 400
    assert client.post("/api/v1/intake", json=_body(sensor_type="XX")).status_code == 400
    assert client.post("/api/v1/intake", json=_body(source_folders=[])).status_code == 400
    # LiDAR chain needs an L2/L3 sensor.
    assert client.post("/api/v1/intake", json=_body(
        sensor_type="M3E", run_photo_chain=False)).status_code == 400
    # Chains need base data.
    assert client.post("/api/v1/intake", json=_body(base_data_paths=[])).status_code == 400


def test_chain_is_gated_on_intake_success(client):
    r = client.post("/api/v1/intake", json=_body(run_photo_chain=False))
    jobs = _jobs_by_type(r.json())
    intake_uuid = jobs["INTAKE"]["job_uuid"]

    # Terra box sees nothing while intake hasn't run.
    assert sync(client, node="TERRA-01", caps=("TERRA_LIDAR",)).json()["assign"] is None

    # Intake machine takes the INTAKE job and finishes it.
    assign = sync(client, node="INTAKE-01", caps=("INTAKE",)).json()["assign"]
    assert assign["job_uuid"] == intake_uuid
    report(client, intake_uuid, "started")
    report(client, intake_uuid, "succeeded")

    # Now the LiDAR job is eligible for the Terra box.
    assign = sync(client, node="TERRA-01", caps=("TERRA_LIDAR",)).json()["assign"]
    assert assign is not None and assign["job_type"] == "TERRA_LIDAR"


def test_intake_options_endpoint(client):
    r = client.get("/api/v1/intake/options")
    assert r.status_code == 200
    assert "L3" in r.json()["sensors"]
    assert isinstance(r.json()["defaults"], dict)
