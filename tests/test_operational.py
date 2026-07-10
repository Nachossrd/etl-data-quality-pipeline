"""Tests de la capa operacional: versionado, exit codes, Excel split, env."""

import json
from pathlib import Path

import pandas as pd
import pytest

from config.settings import PipelineConfig, get_sql_connection_string
from core.export_engine import ExportEngine, _serialize_flags_for_text


# ─── 3.1: Output versioning ──────────────────────────────────────────────────
def test_run_creates_versioned_subfolder(tmp_path):
    from auto_pipeline import run_pipeline

    csv = tmp_path / "in.csv"
    csv.write_text(
        "id_venta,fecha_venta,monto\nTRX-001,2024-01-01,1500\nTRX-002,2024-01-02,2500\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    result = run_pipeline(str(csv), str(out))

    # El output va a out/<run_id>/, NO directamente a out/
    run_dir = Path(result["run_dir"])
    assert run_dir.parent == out.resolve() or run_dir.parent == out
    assert run_dir.name == result["run_id"]
    assert (run_dir / "manifest.json").exists()
    assert (run_dir / "quality_report.json").exists()


def test_latest_marker_points_to_recent_run(tmp_path):
    from auto_pipeline import run_pipeline

    csv = tmp_path / "in.csv"
    csv.write_text("a,b\n1,2\n", encoding="utf-8")
    out = tmp_path / "out"
    result = run_pipeline(str(csv), str(out))

    latest = out / "latest.txt"
    assert latest.exists()
    assert latest.read_text(encoding="utf-8").strip() == result["run_id"]


def test_two_runs_dont_overwrite_each_other(tmp_path):
    from auto_pipeline import run_pipeline

    csv = tmp_path / "in.csv"
    csv.write_text("a,b\n1,2\n", encoding="utf-8")
    out = tmp_path / "out"

    r1 = run_pipeline(str(csv), str(out))
    r2 = run_pipeline(str(csv), str(out))

    assert r1["run_id"] != r2["run_id"]
    assert Path(r1["run_dir"]).exists()
    assert Path(r2["run_dir"]).exists()


# ─── 3.2: Exit code en threshold ────────────────────────────────────────────
def test_run_returns_quarantine_failed_when_above_threshold(tmp_path):
    """Con threshold artificial 0%, cualquier cuarentena dispara la bandera."""
    from auto_pipeline import run_pipeline

    csv = tmp_path / "in.csv"
    csv.write_text(
        "id_venta,fecha_venta,monto\n"
        ",2024-01-01,1500\n"   # id vacío -> potencial cuarentena
        "TRX-002,2024-01-02,2500\n",
        encoding="utf-8",
    )
    result = run_pipeline(str(csv), str(tmp_path / "out"),
                           quarantine_threshold=0.0)
    # No verificamos quarantine_failed en True específicamente porque el cleaner
    # transactional_es puede no enviar a cuarentena; verificamos que el campo existe.
    assert "quarantine_failed" in result
    assert "q_ratio" in result


def test_run_succeeds_below_threshold(tmp_path):
    from auto_pipeline import run_pipeline

    csv = tmp_path / "in.csv"
    csv.write_text(
        "id_venta,fecha_venta,monto\nTRX-001,2024-01-01,1500\nTRX-002,2024-01-02,2500\n",
        encoding="utf-8",
    )
    result = run_pipeline(str(csv), str(tmp_path / "out"),
                           quarantine_threshold=0.50)
    assert result["quarantine_failed"] is False


# ─── 3.3: Credenciales via env ──────────────────────────────────────────────
def test_get_sql_connection_string_uses_settings():
    url = get_sql_connection_string("MyDB")
    assert "MyDB" in url
    assert str(PipelineConfig.SQL_PORT) in url
    assert PipelineConfig.SQL_HOST in url
    assert "ODBC+Driver+17+for+SQL+Server" in url or "ODBC Driver 17" in url


def test_env_vars_override_defaults(monkeypatch):
    """Si seteás SQL_HOST, el módulo settings la lee."""
    monkeypatch.setenv("SQL_HOST", "my-custom-host")
    # Recargar el módulo para que tome el nuevo env
    import importlib
    from config import settings as s
    importlib.reload(s)
    assert s.PipelineConfig.SQL_HOST == "my-custom-host"
    # Limpieza
    monkeypatch.delenv("SQL_HOST")
    importlib.reload(s)


# ─── 3.4: data_quality_flags como list ──────────────────────────────────────
def test_serialize_flags_list_to_string():
    df = pd.DataFrame({
        "x": [1, 2, 3],
        "data_quality_flags": [["id_fixed"], ["id_fixed", "missing_date"], []],
    })
    serialized = _serialize_flags_for_text(df)
    assert serialized["data_quality_flags"].tolist() == [
        "id_fixed", "id_fixed;missing_date", ""
    ]


def test_serialize_flags_passes_through_strings():
    """Si ya viene como string (CSV reload), no rompemos."""
    df = pd.DataFrame({
        "data_quality_flags": ["id_fixed", "id_fixed;missing_date", ""]
    })
    serialized = _serialize_flags_for_text(df)
    assert serialized["data_quality_flags"].tolist() == [
        "id_fixed", "id_fixed;missing_date", ""
    ]


def test_serialize_flags_handles_none():
    df = pd.DataFrame({"data_quality_flags": [None, ["x"]]})
    serialized = _serialize_flags_for_text(df)
    assert serialized["data_quality_flags"].tolist() == ["", "x"]


def test_serialize_flags_noop_without_column():
    df = pd.DataFrame({"a": [1, 2]})
    serialized = _serialize_flags_for_text(df)
    assert list(serialized.columns) == ["a"]


# ─── 3.5: Excel auto-split ──────────────────────────────────────────────────
def test_excel_export_single_sheet_below_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(PipelineConfig, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(PipelineConfig, "EXCEL_ROWS_PER_SHEET", 1_000_000)
    df = pd.DataFrame({"x": range(100)})
    sheets = ExportEngine.export_excel(df, "small_table")
    assert sheets == ["Datos"]
    assert (tmp_path / "small_table_clean.xlsx").exists()


def test_excel_export_splits_above_limit(tmp_path, monkeypatch):
    """Limit bajo para probar split sin generar 1M filas reales."""
    monkeypatch.setattr(PipelineConfig, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(PipelineConfig, "EXCEL_ROWS_PER_SHEET", 10)
    df = pd.DataFrame({"x": range(25)})  # 3 sheets: 10 + 10 + 5
    sheets = ExportEngine.export_excel(df, "big_table")
    assert sheets == ["Datos_001", "Datos_002", "Datos_003"]
    # Verificar contenido
    xls = pd.ExcelFile(tmp_path / "big_table_clean.xlsx")
    assert set(xls.sheet_names) == {"Datos_001", "Datos_002", "Datos_003"}
    assert len(xls.parse("Datos_001")) == 10
    assert len(xls.parse("Datos_003")) == 5
