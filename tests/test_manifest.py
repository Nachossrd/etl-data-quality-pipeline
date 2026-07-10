"""Tests del RunManifest: trazabilidad y reproducibilidad."""

import hashlib
import json
import time

import pytest

from config.settings import PipelineConfig
from core.manifest import RunManifest, _file_descriptor, _sha256_file


@pytest.fixture
def tmp_output(tmp_path, monkeypatch):
    monkeypatch.setattr(PipelineConfig, "OUTPUT_DIR", str(tmp_path))
    return tmp_path


def test_sha256_streaming_matches_hashlib_oneshot(tmp_path):
    p = tmp_path / "x.txt"
    payload = b"hola mundo" * 10_000
    p.write_bytes(payload)
    assert _sha256_file(str(p)) == hashlib.sha256(payload).hexdigest()


def test_file_descriptor_handles_missing_file():
    desc = _file_descriptor("/no/existe/jamas.tsv")
    assert desc["exists"] is False
    assert "sha256" not in desc


def test_file_descriptor_captures_metadata(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("col\n1\n", encoding="utf-8")
    desc = _file_descriptor(str(p))
    assert desc["exists"] is True
    assert desc["size_bytes"] > 0
    assert len(desc["sha256"]) == 64
    assert isinstance(desc["mtime_unix"], int)


def test_run_id_is_unique():
    """Dos manifests creados consecutivamente nunca colisionan (UUID4 suffix)."""
    ids = {RunManifest().run_id for _ in range(50)}
    assert len(ids) == 50  # cero colisiones


def test_run_id_format():
    """Formato `YYYYMMDD-HHMMSS-XXXXXXXX` para que sea legible y filtable."""
    m = RunManifest()
    parts = m.run_id.split("-")
    assert len(parts) == 3
    assert len(parts[0]) == 8  # YYYYMMDD
    assert len(parts[1]) == 6  # HHMMSS
    assert len(parts[2]) == 8  # 8 hex chars


def test_finalize_writes_manifest_json(tmp_path, monkeypatch):
    monkeypatch.setattr(PipelineConfig, "OUTPUT_DIR", str(tmp_path))

    src = tmp_path / "input.csv"
    src.write_text("col\n1\n", encoding="utf-8")

    m = RunManifest()
    m.register_input(str(src))
    m.attach_quality_summary({"extracted": 1}, {})
    data = m.finalize()

    assert (tmp_path / "manifest.json").exists()
    assert data["run_id"] == m.run_id
    assert data["input_files"][0]["sha256"]
    assert data["environment"]["python_version"]
    assert data["timestamps"]["duration_seconds"] >= 0


def test_auto_discover_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(PipelineConfig, "OUTPUT_DIR", str(tmp_path))
    (tmp_path / "a.csv").write_text("x", encoding="utf-8")
    (tmp_path / "b.parquet").write_bytes(b"\x00\x01")

    m = RunManifest()
    m.auto_discover_outputs()
    paths = [f["path"] for f in m.output_files]
    assert any("a.csv" in p for p in paths)
    assert any("b.parquet" in p for p in paths)


def test_errors_propagate_to_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(PipelineConfig, "OUTPUT_DIR", str(tmp_path))
    m = RunManifest()
    m.register_error("simulated failure X")
    data = m.finalize()
    assert "simulated failure X" in data["errors"]
