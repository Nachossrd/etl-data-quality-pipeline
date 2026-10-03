"""Ingesta genérica: cualquier archivo -> una o más tablas en chunks.

El pipeline original asumía "un archivo = una tabla". Eso alcanza para un CSV,
pero no para la realidad: un `.zip` con seis archivos, una base SQLite con
doce tablas o un Excel con varias hojas son **varias tablas en una sola
entrega**, y cada una merece su propia limpieza, su propio reporte de calidad
y su propia tabla en el SQL de salida.

Este módulo resuelve eso con una abstracción única:

    for source in discover_tables(ruta):
        for chunk in source.chunks():
            ...

`discover_tables` acepta un archivo o una carpeta y devuelve N `TableSource`.
Cada `TableSource` sabe su nombre, de qué archivo salió y cómo entregarse en
chunks. El orquestador no necesita saber si detrás hay un CSV, una hoja de
Excel o una tabla SQLite.

Formatos soportados:

    .csv .tsv .txt .dat    delimitado (delimitador, encoding y header detectados)
    .xlsx .xls .xlsm       Excel — une las hojas de esquema compatible
    .json .jsonl .ndjson   JSON y JSON-lines, con aplanado de objetos anidados
    .parquet .pq           Parquet
    .db .sqlite .sqlite3   SQLite — cada tabla es un TableSource
    .sql                   dump SQL — se materializa en SQLite temporal
    .bak                   backup SQL Server (lo maneja el orquestador vía Docker)
    .zip                   se descomprime y se procesa cada archivo soportado
    <carpeta>              se procesa cada archivo soportado que contenga

Lo que NO hace: adivinar relaciones entre tablas. Descubre y limpia; el modelo
dimensional es una decisión de negocio, no de parsing.
"""

import csv
import json
import os
import re
import sqlite3
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Iterator, List, Optional

import pandas as pd

from config.settings import PipelineConfig
from core.file_extraction import FileExtractor
from core.logging_engine import setup_logger

logger = setup_logger("ingest")

DELIMITED_EXT = {".csv", ".tsv", ".txt", ".dat", ".psv"}
EXCEL_EXT = {".xlsx", ".xls", ".xlsm"}
JSON_EXT = {".json", ".jsonl", ".ndjson"}
PARQUET_EXT = {".parquet", ".pq"}
SQLITE_EXT = {".db", ".sqlite", ".sqlite3"}
SQLDUMP_EXT = {".sql"}
ARCHIVE_EXT = {".zip"}
BACKUP_EXT = {".bak"}

SUPPORTED_EXT = (DELIMITED_EXT | EXCEL_EXT | JSON_EXT | PARQUET_EXT
                 | SQLITE_EXT | SQLDUMP_EXT | ARCHIVE_EXT | BACKUP_EXT)

# Basura que traen los ZIP y las carpetas exportadas; nunca es un dataset.
_IGNORED_NAMES = {".ds_store", "thumbs.db", "__macosx"}
_IGNORED_PREFIXES = ("~$", ".")


class UnsupportedFormatError(ValueError):
    """El archivo no tiene un lector conocido."""


def table_name_from(path: str, suffix: str = "") -> str:
    """Nombre de tabla estable y seguro para SQL a partir de una ruta."""
    base = os.path.splitext(os.path.basename(path))[0]
    if suffix:
        base = f"{base}_{suffix}"
    nfkd = unicodedata.normalize("NFKD", base)
    base = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    base = re.sub(r"[^0-9A-Za-z]+", "_", base).strip("_")
    if not base:
        base = "tabla"
    if base[0].isdigit():           # SQL no acepta identificadores que empiecen en dígito
        base = f"t_{base}"
    return base[:120]


@dataclass
class TableSource:
    """Una tabla lista para el pipeline: nombre + generador de chunks."""

    name: str
    reader: Callable[[], Iterator[pd.DataFrame]]
    origin: str = ""                 # archivo del que salió
    kind: str = ""                   # csv / excel / json / parquet / sqlite / bak
    meta: dict = field(default_factory=dict)

    def chunks(self) -> Iterator[pd.DataFrame]:
        return self.reader()


# ─────────────────────────── lectores por formato ──────────────────────────
def _read_json(path: str) -> Iterator[pd.DataFrame]:
    """JSON y JSON-lines. Aplana objetos anidados a columnas `a.b.c`.

    Un JSON de API real casi nunca es una tabla plana: viene envuelto en
    `{"data": [...]}` o con objetos adentro. Aquí se desenvuelve la lista más
    prometedora y se aplana, porque una columna con un dict adentro no sirve
    ni para SQL ni para un gráfico.
    """
    ext = os.path.splitext(path.lower())[1]

    if ext in (".jsonl", ".ndjson"):
        for chunk in pd.read_json(path, lines=True,
                                  chunksize=PipelineConfig.CHUNK_SIZE):
            yield pd.json_normalize(chunk.to_dict(orient="records"))
        return

    with open(path, "r", encoding="utf-8-sig") as f:
        try:
            payload = json.load(f)
        except json.JSONDecodeError:
            # Un .json que en realidad es JSON-lines: caso más común de todos.
            f.seek(0)
            records = [json.loads(line) for line in f if line.strip()]
            yield pd.json_normalize(records)
            return

    if isinstance(payload, dict):
        # Envoltorios típicos: {"data": [...]}, {"results": [...]}, {"items": [...]}
        lists = {k: v for k, v in payload.items()
                 if isinstance(v, list) and v and isinstance(v[0], (dict, list))}
        if lists:
            key = max(lists, key=lambda k: len(lists[k]))
            if len(lists) > 1:
                logger.info(f"JSON con {len(lists)} listas; se usa '{key}' "
                            f"({len(lists[key])} registros)")
            payload = lists[key]
        else:
            payload = [payload]

    yield pd.json_normalize(payload)


def _read_parquet(path: str) -> Iterator[pd.DataFrame]:
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(path)
    for batch in pf.iter_batches(batch_size=PipelineConfig.CHUNK_SIZE):
        yield batch.to_pandas()


def _read_delimited(path: str) -> Iterator[pd.DataFrame]:
    """Delimitado con delimitador y encoding detectados.

    Reusa `FileExtractor.extract_csv_chunked`, que ya resuelve encoding,
    sniffing de delimitador y detección de fila de header. Para `.tsv` se
    fuerza el tabulador porque el sniffer se confunde cuando el contenido
    trae comas dentro de los campos.
    """
    if path.lower().endswith(".tsv"):
        for enc in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                for chunk in pd.read_csv(path, sep="\t", encoding=enc,
                                          on_bad_lines="warn",
                                          chunksize=PipelineConfig.CHUNK_SIZE):
                    yield chunk
                return
            except UnicodeDecodeError:
                continue
    yield from FileExtractor.extract_csv_chunked(path)


def _sqlite_tables(path: str) -> List[str]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        con.close()


def _read_sqlite_table(path: str, table: str) -> Iterator[pd.DataFrame]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        quoted = table.replace('"', '""')
        for chunk in pd.read_sql_query(f'SELECT * FROM "{quoted}"', con,
                                        chunksize=PipelineConfig.CHUNK_SIZE):
            yield chunk
    finally:
        con.close()


def _materialize_sql_dump(path: str) -> str:
    """Ejecuta un dump .sql en una SQLite temporal y devuelve su ruta.

    Funciona con dumps de sintaxis SQLite/ANSI. Los dumps de MySQL o Postgres
    con sintaxis propietaria (`ENGINE=InnoDB`, `COPY ... FROM stdin`) fallan:
    se avisa con el error real en vez de entregar una tabla a medias.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        script = f.read()

    tmp_dir = tempfile.mkdtemp(prefix="dataclean_sqldump_")
    db_path = os.path.join(tmp_dir, "dump.db")
    con = sqlite3.connect(db_path)
    try:
        con.executescript(script)
        con.commit()
    except sqlite3.Error as e:
        con.close()
        raise UnsupportedFormatError(
            f"No se pudo ejecutar el dump SQL '{os.path.basename(path)}': {e}. "
            "Si viene de MySQL/Postgres, exportarlo como CSV o SQLite."
        ) from e
    con.close()
    return db_path


# ───────────────────────────── descubrimiento ──────────────────────────────
def _is_ignorable(name: str) -> bool:
    lower = name.lower()
    return (lower in _IGNORED_NAMES
            or lower.startswith(_IGNORED_PREFIXES)
            or lower.endswith((".md", ".pdf", ".png", ".jpg", ".jpeg", ".gif",
                               ".docx", ".pptx", ".log", ".yaml", ".yml", ".ini")))


def _sources_from_file(path: str, prefix: str = "") -> List[TableSource]:
    ext = os.path.splitext(path.lower())[1]
    name = table_name_from(path)
    if prefix:
        name = f"{prefix}_{name}"

    if ext in DELIMITED_EXT:
        return [TableSource(name, lambda p=path: _read_delimited(p), path, "delimited")]

    if ext in EXCEL_EXT:
        return [TableSource(name, lambda p=path: FileExtractor.extract_excel(p),
                            path, "excel")]

    if ext in JSON_EXT:
        return [TableSource(name, lambda p=path: _read_json(p), path, "json")]

    if ext in PARQUET_EXT:
        return [TableSource(name, lambda p=path: _read_parquet(p), path, "parquet")]

    if ext in SQLITE_EXT or ext in SQLDUMP_EXT:
        db_path = path if ext in SQLITE_EXT else _materialize_sql_dump(path)
        tables = _sqlite_tables(db_path)
        if not tables:
            logger.warning(f"'{os.path.basename(path)}' no contiene tablas")
            return []
        logger.info(f"{os.path.basename(path)}: {len(tables)} tabla(s) — "
                    f"{', '.join(tables)}")
        def _sqlite_name(t: str) -> str:
            if len(tables) == 1:
                return name
            base = table_name_from(path, suffix=t)
            # El prefijo del ZIP/carpeta también aplica aquí: si no, dos bases
            # con una tabla `clientes` colisionan entre orígenes distintos.
            return f"{prefix}_{base}" if prefix else base

        return [
            TableSource(
                _sqlite_name(t),
                lambda p=db_path, t=t: _read_sqlite_table(p, t),
                path, "sqlite", {"source_table": t},
            )
            for t in tables
        ]

    if ext in BACKUP_EXT:
        # El .bak necesita Docker + restore; lo maneja el orquestador.
        return [TableSource(name, lambda: iter(()), path, "bak")]

    raise UnsupportedFormatError(
        f"Formato no soportado: '{ext or os.path.basename(path)}'. "
        f"Soportados: {', '.join(sorted(SUPPORTED_EXT))}"
    )


def _sources_from_zip(path: str) -> List[TableSource]:
    tmp_dir = tempfile.mkdtemp(prefix="dataclean_zip_")
    with zipfile.ZipFile(path) as zf:
        # Zip-slip: nunca extraer fuera del directorio temporal.
        for member in zf.namelist():
            target = os.path.realpath(os.path.join(tmp_dir, member))
            if not target.startswith(os.path.realpath(tmp_dir) + os.sep):
                raise UnsupportedFormatError(
                    f"El ZIP contiene una ruta que escapa del destino: {member}")
        zf.extractall(tmp_dir)

    prefix = table_name_from(path)
    sources = _sources_from_dir(tmp_dir, prefix=prefix)
    if not sources:
        raise UnsupportedFormatError(
            f"El ZIP '{os.path.basename(path)}' no contiene archivos soportados")
    logger.info(f"ZIP {os.path.basename(path)}: {len(sources)} tabla(s)")
    return sources


def _sources_from_dir(path: str, prefix: str = "") -> List[TableSource]:
    sources: List[TableSource] = []
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not _is_ignorable(d)]
        for fname in sorted(files):
            if _is_ignorable(fname):
                continue
            fpath = os.path.join(root, fname)
            ext = os.path.splitext(fname.lower())[1]
            if ext not in SUPPORTED_EXT:
                continue
            try:
                if ext in ARCHIVE_EXT:
                    sources.extend(_sources_from_zip(fpath))
                else:
                    sources.extend(_sources_from_file(fpath, prefix=prefix))
            except Exception as e:
                # Un archivo ilegible no puede abortar la carpeta completa,
                # pero tiene que quedar dicho: silenciarlo sería peor.
                logger.error(f"No se pudo leer '{fname}': {e}")
    return sources


def discover_tables(input_path: str) -> List[TableSource]:
    """Punto de entrada: archivo o carpeta -> lista de tablas a procesar."""
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"No existe: {input_path}")

    if os.path.isdir(input_path):
        sources = _sources_from_dir(input_path)
        if not sources:
            raise UnsupportedFormatError(
                f"La carpeta '{input_path}' no contiene archivos soportados")
        logger.info(f"Carpeta {input_path}: {len(sources)} tabla(s) detectada(s)")
    else:
        ext = os.path.splitext(input_path.lower())[1]
        sources = (_sources_from_zip(input_path) if ext in ARCHIVE_EXT
                   else _sources_from_file(input_path))

    _dedupe_names(sources)
    return sources


def _dedupe_names(sources: List[TableSource]) -> None:
    """Dos archivos distintos pueden producir el mismo nombre de tabla.

    Si se dejan colisionar, el segundo pisa el CSV de salida del primero y se
    pierden datos en silencio. Se desambigua con sufijo numérico.
    """
    seen: dict = {}
    for src in sources:
        if src.name not in seen:
            seen[src.name] = 1
            continue
        seen[src.name] += 1
        src.name = f"{src.name}_{seen[src.name]}"


def describe_support() -> dict:
    """Mapa formato -> descripción, para la UI y los mensajes de error."""
    return {
        "Delimitado": sorted(DELIMITED_EXT),
        "Excel": sorted(EXCEL_EXT),
        "JSON": sorted(JSON_EXT),
        "Parquet": sorted(PARQUET_EXT),
        "SQLite": sorted(SQLITE_EXT),
        "Dump SQL": sorted(SQLDUMP_EXT),
        "Backup SQL Server": sorted(BACKUP_EXT),
        "Comprimido": sorted(ARCHIVE_EXT),
        "Carpeta": ["<directorio con cualquiera de los anteriores>"],
    }
