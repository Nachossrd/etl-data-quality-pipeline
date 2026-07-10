"""Regresión E2E con datasets sintéticos por cleaner.

Cada test:
    1. Carga el fixture (CSV/TSV pequeño con casos conocidos)
    2. Pasa por el cleaner correspondiente
    3. Verifica propiedades específicas del output

Estos tests son los que cazan cambios accidentales en la limpieza: si alguien
modifica un cleaner y rompe el formato esperado, estos tests se ponen rojos
ANTES de que llegue a producción.
"""

import json
from pathlib import Path

import pandas as pd
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


# ─── Helper: lee CSV con la misma config que el pipeline ─────────────────────
def _read_csv(path):
    return pd.read_csv(path, sep=";" if path.suffix == ".csv" and "car_prices" in path.name
                       else None, engine="python")


# ═══════════════════════════════════════════════════════════════════════════
# car_prices
# ═══════════════════════════════════════════════════════════════════════════
class TestCarPricesRegression:
    """car_prices_mini.csv tiene 9 filas: 7 buenas (4 limpias + 3 con NA en
    columnas críticas) + 2 con NA dropeable. Verifica las 3 reglas del PDF."""

    @pytest.fixture
    def fixture_df(self):
        return pd.read_csv(FIXTURES / "car_prices_mini.csv", sep=";")

    def test_drops_unnecessary_columns(self, fixture_df):
        from core.cleaners.car_prices import CarPricesPipelineCleaner
        result = CarPricesPipelineCleaner.clean_chunk(fixture_df)
        assert set(result.columns) == {
            "year", "transmission", "condition", "odometer", "mmr", "sellingprice"
        }

    def test_drops_rows_with_any_na(self, fixture_df):
        from core.cleaners.car_prices import CarPricesPipelineCleaner
        result = CarPricesPipelineCleaner.clean_chunk(fixture_df)
        # No debe haber NA en ninguna celda
        assert not result.isna().any().any()

    def test_binarizes_transmission_manual_to_1(self, fixture_df):
        from core.cleaners.car_prices import CarPricesPipelineCleaner
        result = CarPricesPipelineCleaner.clean_chunk(fixture_df)
        # Solo valores 0/1
        assert set(result["transmission"].unique()).issubset({0, 1})
        # Al menos 1 manual y 1 no-manual
        assert (result["transmission"] == 1).sum() >= 1
        assert (result["transmission"] == 0).sum() >= 1

    def test_canonical_column_order(self, fixture_df):
        from core.cleaners.car_prices import CarPricesPipelineCleaner
        result = CarPricesPipelineCleaner.clean_chunk(fixture_df)
        assert list(result.columns) == [
            "year", "transmission", "condition", "odometer", "mmr", "sellingprice"
        ]

    def test_rule_validator_passes_on_clean_output(self, fixture_df):
        """El output del cleaner NO debe violar las reglas declarativas YAML."""
        from core.cleaners.car_prices import CarPricesPipelineCleaner
        from core.rule_validator import RuleValidator
        result = CarPricesPipelineCleaner.clean_chunk(fixture_df)
        validator = RuleValidator.for_dataset("car_prices")
        outcome = validator.validate(result)
        assert not outcome.is_fatal
        assert outcome.violations_count == {}


# ═══════════════════════════════════════════════════════════════════════════
# IMDb title.basics
# ═══════════════════════════════════════════════════════════════════════════
class TestImdbTitleBasicsRegression:
    """title_basics_mini.tsv tiene 9 filas: 8 con tconst válido + casos con \\N."""

    @pytest.fixture
    def fixture_df(self):
        return pd.read_csv(FIXTURES / "title_basics_mini.tsv", sep="\t")

    def test_n_marker_converted_to_na(self, fixture_df):
        from core.cleaners.imdb_basics import ImdbTitleBasicsCleaner
        result = ImdbTitleBasicsCleaner.clean_chunk(fixture_df)
        # endYear era \N en TODAS las filas → todo NaN
        assert result["endYear"].isna().all()

    def test_runtime_minutes_casted_to_int(self, fixture_df):
        from core.cleaners.imdb_basics import ImdbTitleBasicsCleaner
        result = ImdbTitleBasicsCleaner.clean_chunk(fixture_df)
        # Int64 nullable dtype
        assert str(result["runtimeMinutes"].dtype) == "Int64"
        # Movie No Runtime tenía \N → debe quedar NA
        no_runtime = result[result["primaryTitle"] == "Movie No Runtime"]
        assert no_runtime["runtimeMinutes"].isna().all()

    def test_filters_rows_without_pk(self, fixture_df):
        """Si tconst es NA, la fila se descarta."""
        from core.cleaners.imdb_basics import ImdbTitleBasicsCleaner
        df_with_null = fixture_df.copy()
        df_with_null.loc[0, "tconst"] = None
        result = ImdbTitleBasicsCleaner.clean_chunk(df_with_null)
        assert len(result) == len(fixture_df) - 1

    def test_rule_validator_passes_on_clean_output(self, fixture_df):
        from core.cleaners.imdb_basics import ImdbTitleBasicsCleaner
        from core.rule_validator import RuleValidator
        result = ImdbTitleBasicsCleaner.clean_chunk(fixture_df)
        validator = RuleValidator.for_dataset("title_basics")
        outcome = validator.validate(result)
        assert not outcome.is_fatal


# ═══════════════════════════════════════════════════════════════════════════
# IMDb title.ratings
# ═══════════════════════════════════════════════════════════════════════════
class TestImdbTitleRatingsRegression:

    @pytest.fixture
    def fixture_df(self):
        return pd.read_csv(FIXTURES / "title_ratings_mini.tsv", sep="\t")

    def test_dtypes_casted(self, fixture_df):
        from core.cleaners.imdb_basics import ImdbTitleRatingsCleaner
        result = ImdbTitleRatingsCleaner.clean_chunk(fixture_df)
        assert str(result["averageRating"].dtype) == "Float64"
        assert str(result["numVotes"].dtype) == "Int64"

    def test_known_top_movie_present(self, fixture_df):
        """Shawshank con su rating real debe sobrevivir."""
        from core.cleaners.imdb_basics import ImdbTitleRatingsCleaner
        result = ImdbTitleRatingsCleaner.clean_chunk(fixture_df)
        shawshank = result[result["tconst"] == "tt0111161"]
        assert len(shawshank) == 1
        assert float(shawshank["averageRating"].iloc[0]) == 9.3


# ═══════════════════════════════════════════════════════════════════════════
# transactional_es
# ═══════════════════════════════════════════════════════════════════════════
class TestTransactionalEsRegression:
    """ventas_mini.csv prueba el path transaccional completo: IDs, fechas,
    parser de montos (incluido el bug que arreglamos), magnitudes."""

    @pytest.fixture
    def fixture_df(self):
        return pd.read_csv(FIXTURES / "ventas_mini.csv")

    def test_money_multidot_parsed_correctly(self, fixture_df):
        """REGRESIÓN del bug parse_price '$1.200.000' = 1.2 que arreglamos."""
        from core.cleaners.transactional_es import TransactionalEsCleaner
        result = TransactionalEsCleaner.clean_chunk(fixture_df)
        montos = result["monto"].tolist()
        assert 1_200_000.0 in montos, f"$1.200.000 mal parseado. Resultados: {montos}"

    def test_money_word_magnitude_parsed(self, fixture_df):
        """REGRESIÓN: '40 mil' debe ser 40_000, no 40 ni 40_000_000."""
        from core.cleaners.transactional_es import TransactionalEsCleaner
        result = TransactionalEsCleaner.clean_chunk(fixture_df)
        assert 40_000.0 in result["monto"].tolist()

    def test_data_quality_flags_is_list(self, fixture_df):
        """data_quality_flags es list[str], no string."""
        from core.cleaners.transactional_es import TransactionalEsCleaner
        result = TransactionalEsCleaner.clean_chunk(fixture_df)
        assert "data_quality_flags" in result.columns
        for v in result["data_quality_flags"]:
            assert isinstance(v, list)

    def test_invalid_dates_flagged(self, fixture_df):
        """La fila con 'fecha-invalida' debe tener un flag invalid_date_*."""
        from core.cleaners.transactional_es import TransactionalEsCleaner
        result = TransactionalEsCleaner.clean_chunk(fixture_df)
        # Buscar fila con TRX-004 (la que tenía fecha mala)
        # Tras restore_id_prefix, el id queda como TRX-0004
        row = result[result["id_venta"] == "TRX-0004"]
        assert len(row) == 1
        flags = row["data_quality_flags"].iloc[0]
        assert any("invalid_date" in f for f in flags), f"flags fueron: {flags}"

    def test_quality_score_present(self, fixture_df):
        from core.cleaners.transactional_es import TransactionalEsCleaner
        result = TransactionalEsCleaner.clean_chunk(fixture_df)
        assert "quality_score" in result.columns
        assert result["quality_score"].between(0, 100).all()


# ═══════════════════════════════════════════════════════════════════════════
# E2E completo: orquestador + manifest + quality_report sobre cada fixture
# ═══════════════════════════════════════════════════════════════════════════
class TestE2ERegression:
    """Corre el orquestador completo sobre cada fixture y valida invariantes."""

    def test_e2e_ventas_produces_artifacts(self, tmp_path):
        from auto_pipeline import run_pipeline
        # Threshold alto para no fallar por la fila con ID vacío
        result = run_pipeline(
            str(FIXTURES / "ventas_mini.csv"),
            str(tmp_path / "out"),
            quarantine_threshold=1.0,
        )
        run_dir = Path(result["run_dir"])

        # Artefactos esperados
        assert (run_dir / "manifest.json").exists()
        assert (run_dir / "quality_report.json").exists()
        assert (run_dir / "quality_report.html").exists()

        # Manifest tiene SHA256 del input
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["input_files"][0]["sha256"]

        # Quality report con per-column metrics
        quality = json.loads((run_dir / "quality_report.json").read_text(encoding="utf-8"))
        table = next(iter(quality["tables"].values()))
        assert "schema_fingerprint" in table
        assert "monto" in table["per_column"]
