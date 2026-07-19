"""Config env overrides — the Docker image pins /data paths so uploads and the
DB land on the mounted volume, not the container's ephemeral /app."""
import os

from coordinator.config import CoordinatorConfig, load_config


def test_upload_dir_env_overrides_default_and_yaml(tmp_path, monkeypatch):
    # Default is a dev-friendly relative path.
    assert CoordinatorConfig().upload_dir == "data/uploads"

    # The Docker image sets DATA_INTAKE_UPLOAD_DIR=/data/uploads so the
    # coordinator writes to the mounted volume the INTAKE_COPY worker also sees.
    monkeypatch.setenv("DATA_INTAKE_UPLOAD_DIR", "/data/uploads")
    assert load_config().upload_dir == "/data/uploads"

    # Env wins over a YAML value (same rule as DB_PATH).
    yaml_path = tmp_path / "coordinator.yaml"
    yaml_path.write_text("upload_dir: some/relative/uploads\n")
    assert CoordinatorConfig.from_yaml(yaml_path).upload_dir == "/data/uploads"
