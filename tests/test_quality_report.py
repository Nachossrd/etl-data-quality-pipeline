"""Tests del QualityReportGenerator: lo que un dashboard de negocio consume."""

import json
from pathlib import Path

import pandas as pd
import pytest

from config.settings import PipelineConfig
from core.quality_report import (QualityReportGenerator, UNIQUE_TRACKING_CAP,
                                  _TableAccumulator)


@pytest.fixture
def tmp_output(tmp_path, monkeypatch):
    monkeypatch.setattr(PipelineConfig, "OUTPUT_DIR", str(tmp_path))
    return tmp_path


def test_register_chunk_aggregates_nulls_across_chunks():
    acc = _TableAccumulator("t")
    acc.ingest_chunk(pd.DataFrame({"a": [1, None, 3]}))
    acc.ingest_chunk(pd.DataFrame({"a": [None, None, 6]}))
    assert acc.null_counts["a"] == 3
    assert acc.non_null_counts["a"] == 3


def test_schema_fingerprint_is_deterministic():
    df = pd.DataFrame({"x": [1], "y": ["a"]})
    a, b = _TableAccumulator("t"), _TableAccumulator("t")
    a.ingest_chunk(df)
    b.ingest_chunk(df)
    assert a.schema_fingerprint() == b.schema_fingerprint()
    assert len(a.schema_fingerprint()) == 16


def test_schema_fingerprint_differs_on_dtype_change():
    a = _TableAccumulator("t")
    b = _TableAccumulator("t")
    a.ingest_chunk(pd.DataFrame({"x": [1, 2]}))         # int64
    b.ingest_chunk(pd.DataFrame({"x": [1.0, 2.0]}))     # float64
    assert a.schema_fingerprint() != b.schema_fingerprint()


def test_flag_breakdown_counts_list_tags():
    """data_quality_flags es ahora list[str], no pipe-string."""
    acc = _TableAccumulator("t")
    acc.ingest_chunk(pd.DataFrame({
        "data_quality_flags": [
            ["id_fixed"],
            ["id_fixed", "missing_date"],
            [],
            None,
        ]
    }))
    assert acc.flag_counter["id_fixed"] == 2
    assert acc.flag_counter["missing_date"] == 1


def test_flag_breakdown_legacy_pipe_string_still_works():
    """Backwards-compat: si recibimos el string legacy, lo seguimos parseando."""
    acc = _TableAccumulator("t")
    acc.ingest_chunk(pd.DataFrame({
        "data_quality_flags": ["|id_fixed", "|id_fixed|missing_date", "", None]
    }))
    assert acc.flag_counter["id_fixed"] == 2
    assert acc.flag_counter["missing_date"] == 1


def test_high_cardinality_triggers_above_cap():
    acc = _TableAccumulator("t")
    big = pd.DataFrame({"id": [f"x{i}" for i in range(UNIQUE_TRACKING_CAP + 100)]})
    acc.ingest_chunk(big)
    assert "id" in acc.high_cardinality_cols
    assert "id" not in acc.uniques  # set descartado para liberar memoria


def test_quality_score_distribution():
    acc = _TableAccumulator("t")
    acc.ingest_chunk(pd.DataFrame({"quality_score": [10, 50, 80, 100]}))
    s = acc.serialize()
    qs = s["quality_score_distribution"]
    assert qs["min"] == 10.0
    assert qs["max"] == 100.0
    assert qs["below_50_ratio"] == 0.25


def test_violations_accumulate():
    acc = _TableAccumulator("t")
    acc.add_violations({"year_range": 3})
    acc.add_violations({"year_range": 2, "odometer": 5})
    s = acc.serialize()
    assert s["rule_violations"]["year_range"] == 5
    assert s["rule_violations"]["odometer"] == 5


def test_finalize_produces_valid_json(tmp_output):
    qr = QualityReportGenerator()
    qr.register_chunk("ventas", pd.DataFrame({"a": [1, 2, None]}))
    qr.add_table_metrics("ventas", 3, 3, 0)
    metrics = qr.finalize()

    report_file = tmp_output / "quality_report.json"
    assert report_file.exists()
    loaded = json.loads(report_file.read_text(encoding="utf-8"))
    assert loaded["tables"]["ventas"]["per_column"]["a"]["null_count"] == 1
    assert loaded["totals"]["extracted"] == 3


def test_finalize_produces_html(tmp_output):
    qr = QualityReportGenerator()
    qr.register_chunk("t", pd.DataFrame({"x": [1, 2, 3]}))
    qr.add_table_metrics("t", 3, 3, 0)
    qr.finalize()
    html = (tmp_output / "quality_report.html").read_text(encoding="utf-8")
    assert "schema_fingerprint" in html
    assert "Por columna" in html
