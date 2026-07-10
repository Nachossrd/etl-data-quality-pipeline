"""Tests del registry de cleaners y la facade CleaningEngine."""

import pandas as pd
import pytest

from core.cleaners import REGISTRY, Cleaner
from core.cleaning_engine import CleaningEngine


# ─── Registry estado base ────────────────────────────────────────────────────
def test_default_cleaners_registered():
    names = REGISTRY.list_names()
    assert "transactional_es" in names
    assert "car_prices" in names
    assert "imdb_title_basics" in names
    assert "imdb_title_ratings" in names


def test_transactional_es_is_fallback():
    """El fallback debe ser el último en la lista de prioridad."""
    assert REGISTRY.list_names()[-1] == "transactional_es"


# ─── Auto-detección ──────────────────────────────────────────────────────────
def test_autodetect_car_prices():
    df = pd.DataFrame({
        "year": [2020], "mmr": [10000], "sellingprice": [12000],
        "odometer": [50000], "transmission": ["manual"],
    })
    cleaner = REGISTRY.auto_detect(df)
    assert cleaner.name == "car_prices"


def test_autodetect_imdb_title_basics():
    df = pd.DataFrame({
        "tconst": ["tt1"], "titleType": ["movie"],
        "primaryTitle": ["X"], "startYear": [2020],
    })
    cleaner = REGISTRY.auto_detect(df)
    assert cleaner.name == "imdb_title_basics"


def test_autodetect_imdb_title_ratings():
    df = pd.DataFrame({
        "tconst": ["tt1"], "averageRating": [7.5], "numVotes": [1000],
    })
    cleaner = REGISTRY.auto_detect(df)
    assert cleaner.name == "imdb_title_ratings"


def test_autodetect_falls_back_to_transactional():
    df = pd.DataFrame({
        "id_venta": ["TRX-1"], "monto": [1500], "fecha_venta": ["2024-01-01"],
    })
    cleaner = REGISTRY.auto_detect(df)
    assert cleaner.name == "transactional_es"


def test_autodetect_returns_none_for_unrecognized():
    df = pd.DataFrame({"random_col": [1, 2, 3]})
    cleaner = REGISTRY.auto_detect(df)
    assert cleaner is None


# ─── Hint mechanism ──────────────────────────────────────────────────────────
def test_hint_substring_match_wins_over_autodetect():
    """Si el hint contiene un nombre de cleaner registrado, se prefiere."""
    df = pd.DataFrame({"id_venta": ["X"], "monto": [100]})  # parece transactional
    cleaner = REGISTRY.auto_detect(df, hint="car_prices_train.csv")
    assert cleaner.name == "car_prices"


def test_hint_exact_match():
    df = pd.DataFrame({"x": [1]})
    cleaner = REGISTRY.auto_detect(df, hint="imdb_title_basics")
    assert cleaner.name == "imdb_title_basics"


# ─── Facade integration ─────────────────────────────────────────────────────
def test_cleaning_engine_car_prices_applies_three_rules():
    """Las 3 reglas del PDF: drop cols, dropna, binariza transmission."""
    df = pd.DataFrame({
        "_c0": [1, 2, 3],
        "make": ["BMW", "Honda", "Toyota"],
        "model": ["X", "Y", "Z"],
        "trim": ["", "", ""],
        "body": ["Sedan", "Sedan", "Sedan"],
        "transmission": ["manual", "automatic", None],
        "vin": ["a", "b", "c"],
        "state": ["ca", "nv", "tx"],
        "year": [2020, 2018, 2021],
        "condition": [40.0, 35.0, 28.0],
        "odometer": [10000, 50000, 30000],
        "color": ["black", "white", "red"],
        "interior": ["gray", "black", "tan"],
        "seller": ["s1", "s2", "s3"],
        "mmr": [20000, 12000, 15000],
        "sellingprice": [19500, 11500, 14500],
        "saledate": ["Thu", "Fri", "Sat"],
    })
    result = CleaningEngine.clean_chunk(df, cleaner_name="car_prices")
    assert list(result.columns) == [
        "year", "transmission", "condition", "odometer", "mmr", "sellingprice"
    ]
    assert len(result) == 2  # fila con transmission=None se drop-NA
    assert result["transmission"].tolist() == [1, 0]


def test_cleaning_engine_explicit_cleaner_name_overrides_autodetect():
    df = pd.DataFrame({
        "tconst": ["tt1", "tt2"],
        "titleType": ["movie", "tvSeries"],
        "primaryTitle": ["X", "Y"],
        "startYear": ["2020", "\\N"],
        "endYear": ["\\N", "\\N"],
        "runtimeMinutes": ["120", "\\N"],
    })
    result = CleaningEngine.clean_chunk(df, cleaner_name="imdb_title_basics")
    # \N convertido a NA, startYear casteado a int
    assert pd.isna(result.loc[1, "startYear"])
    assert result.loc[0, "startYear"] == 2020


def test_cleaning_engine_empty_chunk_passes_through():
    result = CleaningEngine.clean_chunk(pd.DataFrame())
    assert result.empty


def test_cleaning_engine_unknown_cleaner_falls_back_to_autodetect():
    df = pd.DataFrame({
        "id_venta": ["TRX-001"], "fecha_venta": ["2024-01-01"], "monto": [1500],
    })
    # Cleaner inexistente → debe autodetectar transactional_es
    result = CleaningEngine.clean_chunk(df, cleaner_name="cleaner_inexistente")
    # transactional_es inserta data_quality_flags (list, plural)
    assert "data_quality_flags" in result.columns
    # Y debe ser lista, no string
    assert all(isinstance(v, list) for v in result["data_quality_flags"])
