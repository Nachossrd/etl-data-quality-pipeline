"""Tests del RuleValidator declarativo."""

from pathlib import Path

import pandas as pd
import pytest
import yaml

from core.rule_validator import (DEFAULT_RULES_DIR, RuleValidator,
                                  ValidationOutcome)


# ─── Constructores ────────────────────────────────────────────────────────────
def test_loads_from_yaml(tmp_path):
    p = tmp_path / "x.yaml"
    p.write_text(yaml.dump({
        "dataset": "demo",
        "required_columns": ["a"],
        "rules": [{"name": "r1", "column": "a", "type": "range", "min": 0}],
    }))
    v = RuleValidator.from_yaml(p)
    assert v.dataset_name == "demo"
    assert len(v.rules) == 1


def test_for_dataset_returns_none_when_missing(tmp_path):
    assert RuleValidator.for_dataset("not_a_dataset", rules_dir=tmp_path) is None


def test_for_dataset_loads_existing():
    # car_prices.yaml fue creado como parte de Fase 1
    v = RuleValidator.for_dataset("car_prices")
    assert v is not None
    assert v.dataset_name == "car_prices"


# ─── required_columns es fatal ────────────────────────────────────────────────
def test_missing_required_columns_is_fatal():
    v = RuleValidator({"required_columns": ["a", "b"], "rules": []})
    out = v.validate(pd.DataFrame({"a": [1]}))  # falta b
    assert out.is_fatal
    assert "b" in out.fatal_violations[0]


# ─── range ────────────────────────────────────────────────────────────────────
def test_range_quarantines_out_of_bounds():
    v = RuleValidator({
        "required_columns": ["x"],
        "rules": [{"name": "x_pos", "column": "x", "type": "range",
                   "min": 0, "max": 100, "on_violation": "quarantine"}],
    })
    out = v.validate(pd.DataFrame({"x": [10, -5, 200, 50]}))
    assert out.violations_count["x_pos"] == 2
    assert len(out.valid_df) == 2
    assert len(out.quarantine_df) == 2


def test_range_ignores_nulls():
    v = RuleValidator({
        "rules": [{"name": "x_pos", "column": "x", "type": "range",
                   "min": 0, "on_violation": "quarantine"}],
    })
    out = v.validate(pd.DataFrame({"x": [1, None, 2]}))
    assert out.violations_count.get("x_pos", 0) == 0


# ─── allowed_values ───────────────────────────────────────────────────────────
def test_allowed_values_catches_outsiders():
    v = RuleValidator({
        "rules": [{"name": "tx", "column": "transmission", "type": "allowed_values",
                   "values": [0, 1], "on_violation": "fail"}],
    })
    out = v.validate(pd.DataFrame({"transmission": [0, 1, 2, 0]}))
    assert out.violations_count["tx"] == 1
    assert out.is_fatal


# ─── regex ────────────────────────────────────────────────────────────────────
def test_regex_validates_format():
    v = RuleValidator({
        "rules": [{"name": "tconst_fmt", "column": "tconst", "type": "regex",
                   "pattern": "^tt\\d+$", "on_violation": "quarantine"}],
    })
    out = v.validate(pd.DataFrame({"tconst": ["tt123", "nm456", "tt0", "x"]}))
    assert out.violations_count["tconst_fmt"] == 2
    assert sorted(out.quarantine_df["tconst"].tolist()) == ["nm456", "x"]


# ─── not_null ─────────────────────────────────────────────────────────────────
def test_not_null():
    v = RuleValidator({
        "rules": [{"name": "x_nn", "column": "x", "type": "not_null",
                   "on_violation": "quarantine"}],
    })
    out = v.validate(pd.DataFrame({"x": [1, None, 3, None]}))
    assert out.violations_count["x_nn"] == 2


# ─── unique ───────────────────────────────────────────────────────────────────
def test_unique_flags_duplicates():
    v = RuleValidator({
        "rules": [{"name": "pk", "column": "id", "type": "unique",
                   "on_violation": "quarantine"}],
    })
    out = v.validate(pd.DataFrame({"id": ["a", "b", "a", "c", "b"]}))
    # 'a','a','b','b' = 4 filas duplicadas (keep=False marca todas)
    assert out.violations_count["pk"] == 4


# ─── dtype ────────────────────────────────────────────────────────────────────
def test_dtype_int_catches_floats_with_decimals():
    v = RuleValidator({
        "rules": [{"name": "x_int", "column": "x", "type": "dtype",
                   "expected": "int", "on_violation": "quarantine"}],
    })
    out = v.validate(pd.DataFrame({"x": [1, 2.5, 3]}))
    assert out.violations_count["x_int"] == 1


def test_dtype_date_catches_non_dates():
    v = RuleValidator({
        "rules": [{"name": "d", "column": "d", "type": "dtype",
                   "expected": "date", "on_violation": "quarantine"}],
    })
    out = v.validate(pd.DataFrame({"d": ["2024-01-01", "no-fecha", "2024-02-02"]}))
    assert out.violations_count["d"] == 1


# ─── on_violation behavior ────────────────────────────────────────────────────
def test_warn_does_not_quarantine():
    v = RuleValidator({
        "rules": [{"name": "x", "column": "x", "type": "range",
                   "min": 0, "on_violation": "warn"}],
    })
    out = v.validate(pd.DataFrame({"x": [-1, 1]}))
    assert out.violations_count["x"] == 1
    assert len(out.valid_df) == 2
    assert len(out.quarantine_df) == 0


# ─── E2E real: car_prices.yaml ────────────────────────────────────────────────
def test_car_prices_rules_clean_dataset_passes():
    """Con datos limpios típicos no debe haber violaciones."""
    v = RuleValidator.for_dataset("car_prices")
    df = pd.DataFrame({
        "year": [2015, 2018],
        "transmission": [0, 1],
        "condition": [30, 45],
        "odometer": [50000, 30000],
        "mmr": [12000, 18000],
        "sellingprice": [11500, 17500],
    })
    out = v.validate(df)
    assert not out.is_fatal
    assert out.violations_count == {}


def test_car_prices_rules_catch_bad_data():
    v = RuleValidator.for_dataset("car_prices")
    df = pd.DataFrame({
        "year": [1800, 2020],            # 1 violation: year_range
        "transmission": [0, 2],          # 1 violation FATAL: transmission_binary
        "condition": [30, 45],
        "odometer": [50000, 30000],
        "mmr": [12000, 18000],
        "sellingprice": [11500, 17500],
    })
    out = v.validate(df)
    assert out.is_fatal
    assert out.violations_count["year_range"] == 1
    assert out.violations_count["transmission_binary"] == 1
