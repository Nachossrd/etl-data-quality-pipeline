"""
Carga, limpieza y exportación de los datasets IMDb TSV para PySpark SQL.

Soporta los 7 archivos del PDF (UAB):
    title.basics.tsv, title.ratings.tsv, title.principals2.tsv (o principals),
    title.crew.tsv, title.akas.tsv, title.episode.tsv, name.basics.tsv

Pipeline:
    raw TSV  ──load_raw──▶  DataFrame tipado  ──clean──▶  DataFrame limpio
    DataFrame limpio  ──export_parquet──▶  Parquet (10x más rápido al re-consultar)
    Parquet  ──register_views──▶  vistas SQL listas para spark.sql(...)

Reglas de limpieza aplicadas:
    1. \\N → NULL  (marcador IMDb estándar)
    2. Cast a tipos correctos según schema oficial
    3. Filtrado opcional de filas corruptas (tconst/nconst nulos)
    4. Sin transformaciones destructivas: preserva todos los registros válidos
"""

from pathlib import Path
from typing import Dict, List, Optional

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql.types import (FloatType, IntegerType, StringType,
                               StructField, StructType)

from core.logging_engine import setup_logger

logger = setup_logger("imdb_dataset")


# ─── Esquemas oficiales IMDb ──────────────────────────────────────────────────
SCHEMAS: Dict[str, StructType] = {
    "title_basics": StructType([
        StructField("tconst", StringType()),
        StructField("titleType", StringType()),
        StructField("primaryTitle", StringType()),
        StructField("originalTitle", StringType()),
        StructField("isAdult", StringType()),
        StructField("startYear", IntegerType()),
        StructField("endYear", IntegerType()),
        StructField("runtimeMinutes", IntegerType()),
        StructField("genres", StringType()),
    ]),
    "title_ratings": StructType([
        StructField("tconst", StringType()),
        StructField("averageRating", FloatType()),
        StructField("numVotes", IntegerType()),
    ]),
    "title_principals": StructType([
        StructField("tconst", StringType()),
        StructField("ordering", IntegerType()),
        StructField("nconst", StringType()),
        StructField("category", StringType()),
        StructField("job", StringType()),
        StructField("characters", StringType()),
    ]),
    "title_crew": StructType([
        StructField("tconst", StringType()),
        StructField("directors", StringType()),
        StructField("writers", StringType()),
    ]),
    "title_akas": StructType([
        StructField("titleId", StringType()),
        StructField("ordering", IntegerType()),
        StructField("title", StringType()),
        StructField("region", StringType()),
        StructField("language", StringType()),
        StructField("types", StringType()),
        StructField("attributes", StringType()),
        StructField("isOriginalTitle", StringType()),
    ]),
    "title_episode": StructType([
        StructField("tconst", StringType()),
        StructField("parentTconst", StringType()),
        StructField("seasonNumber", IntegerType()),
        StructField("episodeNumber", IntegerType()),
    ]),
    "name_basics": StructType([
        StructField("nconst", StringType()),
        StructField("primaryName", StringType()),
        StructField("birthYear", IntegerType()),
        StructField("deathYear", IntegerType()),
        StructField("primaryProfession", StringType()),
        StructField("knownForTitles", StringType()),
    ]),
}

# Mapea nombres de archivo (con variantes) → nombre lógico de vista
FILE_TO_VIEW: Dict[str, str] = {
    "title.basics.tsv": "title_basics",
    "title.ratings.tsv": "title_ratings",
    "title.principals.tsv": "title_principals",
    "title.principals2.tsv": "title_principals",   # variante .ova UAB
    "title.crew.tsv": "title_crew",
    "title.akas.tsv": "title_akas",
    "title.episode.tsv": "title_episode",
    "name.basics.tsv": "name_basics",
}

# Identidad de fila por tabla (para filtrar corrupciones)
PK = {
    "title_basics": "tconst",
    "title_ratings": "tconst",
    "title_principals": "tconst",
    "title_crew": "tconst",
    "title_akas": "titleId",
    "title_episode": "tconst",
    "name_basics": "nconst",
}


class ImdbDataset:
    """Pipeline reusable para preparar los datasets IMDb para consultas SQL."""

    READ_OPTS = {"sep": "\t", "nullValue": "\\N", "header": "true"}

    # ─── Carga ────────────────────────────────────────────────────────────────
    @classmethod
    def load_raw(cls, spark: SparkSession, file_path: Path, view: str) -> DataFrame:
        """Lee un TSV crudo con su schema oficial. Sin limpieza aún."""
        if view not in SCHEMAS:
            raise ValueError(f"Vista desconocida: {view}. Válidas: {list(SCHEMAS)}")
        df = spark.read.options(**cls.READ_OPTS).schema(SCHEMAS[view]).csv(str(file_path))
        logger.info(f"[raw] {view} <- {file_path.name}")
        return df

    # ─── Limpieza ─────────────────────────────────────────────────────────────
    @staticmethod
    def clean(df: DataFrame, view: str) -> DataFrame:
        """Limpieza estándar: descarta filas sin clave primaria."""
        pk_col = PK.get(view)
        if pk_col and pk_col in df.columns:
            n0 = None  # evitamos count() porque es costoso; reportamos al exportar
            df = df.filter(df[pk_col].isNotNull())
        return df

    # ─── Exportación a Parquet ────────────────────────────────────────────────
    @staticmethod
    def export_parquet(df: DataFrame, output_path: Path,
                       partition_by: Optional[List[str]] = None) -> None:
        """Exporta a Parquet con snappy. ~6x menos tamaño y queries 5-10x más rápidas."""
        writer = df.write.mode("overwrite").option("compression", "snappy")
        if partition_by:
            writer = writer.partitionBy(*partition_by)
        writer.parquet(str(output_path))
        logger.info(f"[parquet] exportado -> {output_path}")

    # ─── Pipeline completo ────────────────────────────────────────────────────
    @classmethod
    def prepare_all(cls, spark: SparkSession, source_dir: Path,
                    output_dir: Path) -> Dict[str, int]:
        """Recorre source_dir, identifica archivos IMDb, limpia y exporta a Parquet.

        Devuelve dict {view: row_count} para auditoría.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        stats: Dict[str, int] = {}

        for filename, view in FILE_TO_VIEW.items():
            src = source_dir / filename
            if not src.exists():
                continue
            df = cls.load_raw(spark, src, view)
            df = cls.clean(df, view)
            out = output_dir / view
            cls.export_parquet(df, out)
            n = spark.read.parquet(str(out)).count()
            stats[view] = n
            logger.info(f"[stats] {view}: {n:,} filas válidas")

        if not stats:
            raise FileNotFoundError(
                f"Ningún TSV reconocido en {source_dir}. Esperados: {list(FILE_TO_VIEW)}"
            )
        return stats

    # ─── Registro de vistas SQL ───────────────────────────────────────────────
    @classmethod
    def register_views(cls, spark: SparkSession, parquet_dir: Path) -> List[str]:
        """Registra cada subcarpeta Parquet como vista temporal SQL.

        Después de esto: spark.sql("SELECT * FROM title_basics WHERE ...")
        """
        registered: List[str] = []
        for view in SCHEMAS:
            p = parquet_dir / view
            if p.exists() and any(p.iterdir()):
                spark.read.parquet(str(p)).createOrReplaceTempView(view)
                registered.append(view)
        logger.info(f"[views] registradas: {registered}")
        return registered

    @classmethod
    def register_from_tsv(cls, spark: SparkSession, source_dir: Path) -> List[str]:
        """Atajo: registra vistas directamente desde TSVs (sin exportar Parquet).

        Útil para queries one-shot. Más lento que parquet en re-consultas.
        """
        registered: List[str] = []
        for filename, view in FILE_TO_VIEW.items():
            src = source_dir / filename
            if not src.exists():
                continue
            df = cls.clean(cls.load_raw(spark, src, view), view)
            df.createOrReplaceTempView(view)
            registered.append(view)
        logger.info(f"[views from TSV] registradas: {registered}")
        return registered
