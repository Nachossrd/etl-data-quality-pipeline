"""Test E2E del pipeline orquestador: input -> manifest + quality_report."""

import json
from pathlib import Path

import pandas as pd
import pytest

from auto_pipeline import run_pipeline


@pytest.fixture
def csv_with_quality_issues(tmp_path):
    """CSV pequeño con problemas realistas: nulls, IDs vacíos, fechas inválidas."""
    p = tmp_path / "ventas_test.csv"
    p.write_text(
        "id_venta,fecha_venta,monto,producto,estado\n"
        "TRX-001,2024-01-15,1500.50,Laptop,vendido\n"
        "TRX-002,2024-01-16,,Mouse,vendido\n"
        ",2024-01-17,250.00,Teclado,pendiente\n"
        "TRX-004,fecha-invalida,800.00,Monitor,vendido\n"
        "TRX-005,2024-01-19,1200.00,Mouse,devuelto\n",
        encoding="utf-8",
    )
    return p


def _run_and_get_dir(input_path: str, parent_out: Path) -> Path:
    """Helper: corre el pipeline y devuelve la subcarpeta del run."""
    # Threshold alto para que la quarantine no haga fallar el test
    result = run_pipeline(str(input_path), str(parent_out),
                           quarantine_threshold=1.0)
    return Path(result["run_dir"])


def test_e2e_csv_produces_manifest_and_quality_report(csv_with_quality_issues, tmp_path):
    run_dir = _run_and_get_dir(csv_with_quality_issues, tmp_path / "out")

    manifest_path = run_dir / "manifest.json"
    quality_path = run_dir / "quality_report.json"
    assert manifest_path.exists(), "manifest.json no fue generado"
    assert quality_path.exists(), "quality_report.json no fue generado"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    quality = json.loads(quality_path.read_text(encoding="utf-8"))

    # Manifest verificaciones de auditoría
    assert manifest["input_files"][0]["sha256"], "Falta SHA256 del input"
    assert manifest["input_files"][0]["size_bytes"] > 0
    assert manifest["timestamps"]["duration_seconds"] >= 0
    assert manifest["environment"]["python_version"]
    assert manifest["config_snapshot"]["CHUNK_SIZE"]

    # Quality report tiene per-column metrics
    table = next(iter(quality["tables"].values()))
    assert "schema_fingerprint" in table
    assert "per_column" in table
    assert table["row_counts"]["extracted"] == 5


def test_e2e_quality_report_captures_nulls(csv_with_quality_issues, tmp_path):
    run_dir = _run_and_get_dir(csv_with_quality_issues, tmp_path / "out")
    quality = json.loads((run_dir / "quality_report.json").read_text(encoding="utf-8"))
    table = next(iter(quality["tables"].values()))

    # Buscar columna que sea aproximadamente "monto" (puede haber sufijo _original)
    monto_col = next(
        (c for c in table["per_column"]
         if "monto" in c.lower() and not c.endswith(("_currency", "_original"))),
        None,
    )
    assert monto_col, f"No se encontró columna monto en {list(table['per_column'])}"
    assert isinstance(table["per_column"][monto_col]["null_count"], int)


def test_e2e_manifest_lists_all_outputs(csv_with_quality_issues, tmp_path):
    run_dir = _run_and_get_dir(csv_with_quality_issues, tmp_path / "out")
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    output_names = {Path(f["path"]).name for f in manifest["output_files"]}
    assert any(n.endswith("_clean.csv") for n in output_names)
    assert "quality_report.json" in output_names
    assert "quality_report.html" in output_names
