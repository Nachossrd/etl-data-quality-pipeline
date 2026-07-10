#!/usr/bin/env python
"""
EXTRACTION ENGINE v3 — Industrial ETL con Checkpointing, Paralelismo y Parquet.

Componentes:
  - RetryPolicy: clasificación de errores, backoff exponencial
  - CheckpointManager: estado persistente SQLite (WAL mode)
  - ExtractionPlanner: detección PK, planificación de chunks
  - ParquetWriter: streaming pyarrow, ZSTD, type mapping
  - MetricsCollector: throughput, ETA, observabilidad
  - ParallelExtractor: ThreadPoolExecutor, backpressure
  - ExtractionEngine: orquestador top-level
"""

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import psutil
from sqlalchemy import text
from sqlalchemy.pool import NullPool

logger = logging.getLogger("extraction_engine")


# ═══════════════════════════════════════════════════════════════════════════════
# RETRY POLICY
# ═══════════════════════════════════════════════════════════════════════════════

class RetryPolicy:
    """Clasifica errores pyodbc/SQLAlchemy y determina estrategia de retry."""

    RETRYABLE_SQLSTATES = {
        "08S01": "Communication link failure",
        "08001": "Connection failed",
        "40001": "Deadlock victim",
        "HYT00": "Timeout expired",
        "HY008": "Operation canceled",
    }

    NON_RETRYABLE_SQLSTATES = {
        "42000": "Syntax error",
        "42S02": "Table not found",
        "22003": "Numeric overflow",
        "23000": "Constraint violation",
    }

    def __init__(self, max_retries: int = 3, base_delay: float = 2.0, max_delay: float = 30.0):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay

    def should_retry(self, error: Exception, attempt: int) -> Tuple[bool, float]:
        """Retorna (should_retry, delay_seconds)."""
        if attempt >= self.max_retries:
            return False, 0

        sqlstate = getattr(error, "sqlstate", None)
        if sqlstate and sqlstate in self.NON_RETRYABLE_SQLSTATES:
            return False, 0

        # Errores de disco lleno — no retryable
        err_str = str(error).lower()
        if "disk full" in err_str or "no space" in err_str:
            return False, 0

        delay = min(self.base_delay * (2 ** attempt), self.max_delay)
        return True, delay

    def classify(self, error: Exception) -> str:
        """Clasifica tipo de error para métricas."""
        sqlstate = getattr(error, "sqlstate", None)
        if sqlstate in self.RETRYABLE_SQLSTATES:
            return f"retryable:{self.RETRYABLE_SQLSTATES[sqlstate]}"
        if sqlstate in self.NON_RETRYABLE_SQLSTATES:
            return f"fatal:{self.NON_RETRYABLE_SQLSTATES[sqlstate]}"
        return f"unknown:{type(error).__name__}"


# ═══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ChunkSpec:
    """Especificación de un chunk a extraer."""
    schema: str
    table: str
    chunk_idx: int
    strategy: str           # "pk_range" | "offset_fetch"
    pk_column: Optional[str]
    range_start: Optional[int]
    range_end: Optional[int]
    offset: Optional[int]
    limit: int
    estimated_rows: int

    @property
    def fqn(self) -> str:
        return f"[{self.schema}].[{self.table}]"

    @property
    def chunk_id(self) -> str:
        return f"{self.schema}.{self.table}.{self.chunk_idx:06d}"


@dataclass
class ExtractionPlan:
    """Plan de extracción para una tabla."""
    schema: str
    table: str
    total_rows: int
    pk_column: Optional[str]
    pk_type: Optional[str]
    strategy: str
    chunk_size: int
    chunks: List[ChunkSpec] = field(default_factory=list)

    @property
    def num_chunks(self) -> int:
        return len(self.chunks)


@dataclass
class ChunkResult:
    """Resultado de la extracción de un chunk."""
    chunk_spec: ChunkSpec
    rows_extracted: int
    parquet_path: Optional[str]
    checksum: Optional[str]
    sql_ms: float
    write_ms: float
    total_ms: float
    error: Optional[str] = None
    retry_count: int = 0


# ═══════════════════════════════════════════════════════════════════════════════
# CHECKPOINT MANAGER (SQLite WAL)
# ═══════════════════════════════════════════════════════════════════════════════

class CheckpointManager:
    """Estado persistente de extracción en SQLite. Crash-safe via WAL mode.

    Schema:
      job_runs: metadata del job (bak_file, db_name, timestamps)
      table_state: estado por tabla (status, PK info, strategy)
      chunk_state: estado por chunk (offset, rows, parquet file, checksum)
    """

    def __init__(self, checkpoint_path: str):
        self.db_path = checkpoint_path
        self._local = threading.local()
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        """Conexión thread-local para seguridad en ThreadPoolExecutor."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    def _init_schema(self):
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS job_runs (
                job_id TEXT PRIMARY KEY,
                bak_file TEXT,
                db_name TEXT,
                started_at TEXT,
                completed_at TEXT,
                status TEXT DEFAULT 'running'
            );
            CREATE TABLE IF NOT EXISTS table_state (
                job_id TEXT,
                schema_name TEXT,
                table_name TEXT,
                total_rows INTEGER,
                pk_column TEXT,
                strategy TEXT,
                chunk_size INTEGER,
                num_chunks INTEGER,
                status TEXT DEFAULT 'pending',
                started_at TEXT,
                completed_at TEXT,
                PRIMARY KEY (job_id, schema_name, table_name)
            );
            CREATE TABLE IF NOT EXISTS chunk_state (
                job_id TEXT,
                schema_name TEXT,
                table_name TEXT,
                chunk_idx INTEGER,
                offset_val INTEGER,
                limit_val INTEGER,
                range_start INTEGER,
                range_end INTEGER,
                rows_extracted INTEGER DEFAULT 0,
                parquet_file TEXT,
                checksum TEXT,
                status TEXT DEFAULT 'pending',
                started_at TEXT,
                completed_at TEXT,
                error_msg TEXT,
                retry_count INTEGER DEFAULT 0,
                sql_ms REAL DEFAULT 0,
                write_ms REAL DEFAULT 0,
                PRIMARY KEY (job_id, schema_name, table_name, chunk_idx)
            );
        """)
        conn.commit()

    # ── Job lifecycle ──

    def create_job(self, bak_file: str, db_name: str) -> str:
        job_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO job_runs (job_id, bak_file, db_name, started_at, status) VALUES (?,?,?,?,?)",
            (job_id, bak_file, db_name, datetime.now().isoformat(), "running"),
        )
        conn.commit()
        return job_id

    def find_resumable_job(self, bak_file: str, db_name: str) -> Optional[str]:
        """Encuentra un job previo que pueda ser reanudado."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT job_id FROM job_runs WHERE bak_file=? AND db_name=? AND status='running' "
            "ORDER BY started_at DESC LIMIT 1",
            (bak_file, db_name),
        ).fetchone()
        return row["job_id"] if row else None

    def complete_job(self, job_id: str, status: str = "completed"):
        conn = self._get_conn()
        conn.execute(
            "UPDATE job_runs SET completed_at=?, status=? WHERE job_id=?",
            (datetime.now().isoformat(), status, job_id),
        )
        conn.commit()

    # ── Table state ──

    def register_table(self, job_id: str, plan: "ExtractionPlan"):
        conn = self._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO table_state "
            "(job_id, schema_name, table_name, total_rows, pk_column, strategy, chunk_size, num_chunks, status, started_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (job_id, plan.schema, plan.table, plan.total_rows, plan.pk_column,
             plan.strategy, plan.chunk_size, plan.num_chunks, "extracting",
             datetime.now().isoformat()),
        )
        conn.commit()

    def complete_table(self, job_id: str, schema: str, table: str, status: str = "completed"):
        conn = self._get_conn()
        conn.execute(
            "UPDATE table_state SET completed_at=?, status=? WHERE job_id=? AND schema_name=? AND table_name=?",
            (datetime.now().isoformat(), status, job_id, schema, table),
        )
        conn.commit()

    # ── Chunk state ──

    def register_chunks(self, job_id: str, chunks: List[ChunkSpec]):
        conn = self._get_conn()
        for c in chunks:
            conn.execute(
                "INSERT OR IGNORE INTO chunk_state "
                "(job_id, schema_name, table_name, chunk_idx, offset_val, limit_val, "
                "range_start, range_end, status) VALUES (?,?,?,?,?,?,?,?,?)",
                (job_id, c.schema, c.table, c.chunk_idx, c.offset, c.limit,
                 c.range_start, c.range_end, "pending"),
            )
        conn.commit()

    def get_pending_chunks(self, job_id: str, schema: str, table: str) -> List[int]:
        """Retorna chunk_idx de chunks no completados."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT chunk_idx FROM chunk_state "
            "WHERE job_id=? AND schema_name=? AND table_name=? AND status != 'completed' "
            "ORDER BY chunk_idx",
            (job_id, schema, table),
        ).fetchall()
        return [r["chunk_idx"] for r in rows]

    def mark_chunk_started(self, job_id: str, chunk: ChunkSpec):
        conn = self._get_conn()
        conn.execute(
            "UPDATE chunk_state SET status='extracting', started_at=? "
            "WHERE job_id=? AND schema_name=? AND table_name=? AND chunk_idx=?",
            (datetime.now().isoformat(), job_id, chunk.schema, chunk.table, chunk.chunk_idx),
        )
        conn.commit()

    def mark_chunk_completed(self, job_id: str, result: ChunkResult):
        conn = self._get_conn()
        conn.execute(
            "UPDATE chunk_state SET status='completed', completed_at=?, rows_extracted=?, "
            "parquet_file=?, checksum=?, sql_ms=?, write_ms=?, retry_count=? "
            "WHERE job_id=? AND schema_name=? AND table_name=? AND chunk_idx=?",
            (datetime.now().isoformat(), result.rows_extracted, result.parquet_path,
             result.checksum, result.sql_ms, result.write_ms, result.retry_count,
             job_id, result.chunk_spec.schema, result.chunk_spec.table,
             result.chunk_spec.chunk_idx),
        )
        conn.commit()

    def mark_chunk_failed(self, job_id: str, chunk: ChunkSpec, error: str, retry_count: int):
        conn = self._get_conn()
        conn.execute(
            "UPDATE chunk_state SET status='failed', error_msg=?, retry_count=? "
            "WHERE job_id=? AND schema_name=? AND table_name=? AND chunk_idx=?",
            (error, retry_count, job_id, chunk.schema, chunk.table, chunk.chunk_idx),
        )
        conn.commit()

    def get_completed_count(self, job_id: str, schema: str, table: str) -> int:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM chunk_state "
            "WHERE job_id=? AND schema_name=? AND table_name=? AND status='completed'",
            (job_id, schema, table),
        ).fetchone()
        return row["cnt"]

    def get_extracted_rows(self, job_id: str, schema: str, table: str) -> int:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COALESCE(SUM(rows_extracted), 0) as total FROM chunk_state "
            "WHERE job_id=? AND schema_name=? AND table_name=? AND status='completed'",
            (job_id, schema, table),
        ).fetchone()
        return row["total"]

    def close(self):
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None


# ═══════════════════════════════════════════════════════════════════════════════
# EXTRACTION PLANNER
# ═══════════════════════════════════════════════════════════════════════════════

class ExtractionPlanner:
    """Inspecciona metadata de tablas SQL Server y construye planes de extracción.

    Detecta PKs via sys.indexes + sys.index_columns, elige estrategia
    (pk_range vs offset_fetch), y computa rangos de chunks.
    """

    DEFAULT_CHUNK_SIZE = 100_000

    def __init__(self, sql_server, db_name: str, chunk_size: int = None):
        self.sql_server = sql_server
        self.db_name = db_name
        self.chunk_size = chunk_size or self.DEFAULT_CHUNK_SIZE

    def plan_table(self, schema: str, table: str, row_count: int) -> ExtractionPlan:
        """Construye plan de extracción completo para una tabla."""
        pk_info = self._detect_primary_key(schema, table)

        if pk_info and len(pk_info) == 1 and pk_info[0][1] in ("int", "bigint", "smallint"):
            pk_col, pk_type = pk_info[0]
            return self._plan_pk_range(schema, table, row_count, pk_col, pk_type)
        else:
            return self._plan_offset_fetch(schema, table, row_count)

    def _detect_primary_key(self, schema: str, table: str) -> Optional[List[Tuple[str, str]]]:
        """Detecta PK via sys.indexes. Retorna [(col_name, type_name), ...]."""
        engine = self.sql_server.create_engine_isolated(database=self.db_name, timeout=15)
        try:
            with engine.connect() as conn:
                rows = conn.execute(text("""
                    SELECT c.name AS col_name, tp.name AS type_name
                    FROM sys.index_columns ic
                    JOIN sys.indexes i ON ic.object_id = i.object_id AND ic.index_id = i.index_id
                    JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id
                    JOIN sys.types tp ON c.system_type_id = tp.system_type_id AND tp.name != 'sysname'
                    WHERE i.is_primary_key = 1
                      AND ic.object_id = OBJECT_ID(:tbl)
                    ORDER BY ic.key_ordinal
                """), {"tbl": f"{schema}.{table}"}).fetchall()
                if rows:
                    result = [(r[0], r[1]) for r in rows]
                    logger.info(f"  PK detectada: {result}")
                    return result
                return None
        except Exception as e:
            logger.warning(f"  PK detection falló: {e}")
            return None
        finally:
            engine.dispose()

    def _plan_pk_range(self, schema: str, table: str, row_count: int,
                       pk_col: str, pk_type: str) -> ExtractionPlan:
        """Plan basado en rangos de PK numérica. Más eficiente y resumable."""
        pk_min, pk_max = self._get_pk_bounds(schema, table, pk_col)
        if pk_min is None or pk_max is None:
            return self._plan_offset_fetch(schema, table, row_count)

        plan = ExtractionPlan(
            schema=schema, table=table, total_rows=row_count,
            pk_column=pk_col, pk_type=pk_type, strategy="pk_range",
            chunk_size=self.chunk_size,
        )

        # Dividir rango de PK en chunks
        pk_range = pk_max - pk_min + 1
        # Ajustar chunk_size al rango real si hay gaps
        rows_per_pk = row_count / max(pk_range, 1)
        pk_chunk_size = int(self.chunk_size / max(rows_per_pk, 0.01))
        pk_chunk_size = max(pk_chunk_size, self.chunk_size)  # Al menos chunk_size PKs

        idx = 0
        current = pk_min
        while current <= pk_max:
            end = min(current + pk_chunk_size - 1, pk_max)
            plan.chunks.append(ChunkSpec(
                schema=schema, table=table, chunk_idx=idx,
                strategy="pk_range", pk_column=pk_col,
                range_start=current, range_end=end,
                offset=None, limit=pk_chunk_size,
                estimated_rows=int(pk_chunk_size * rows_per_pk),
            ))
            current = end + 1
            idx += 1

        logger.info(
            f"  Plan pk_range: {plan.num_chunks} chunks, PK={pk_col} "
            f"[{pk_min}..{pk_max}], chunk_pk_size={pk_chunk_size:,}"
        )
        return plan

    def _plan_offset_fetch(self, schema: str, table: str, row_count: int) -> ExtractionPlan:
        """Plan basado en OFFSET/FETCH. Para tablas sin PK numérica."""
        plan = ExtractionPlan(
            schema=schema, table=table, total_rows=row_count,
            pk_column=None, pk_type=None, strategy="offset_fetch",
            chunk_size=self.chunk_size,
        )

        for idx, offset in enumerate(range(0, max(row_count, 1), self.chunk_size)):
            plan.chunks.append(ChunkSpec(
                schema=schema, table=table, chunk_idx=idx,
                strategy="offset_fetch", pk_column=None,
                range_start=None, range_end=None,
                offset=offset, limit=self.chunk_size,
                estimated_rows=min(self.chunk_size, row_count - offset),
            ))

        logger.info(f"  Plan offset_fetch: {plan.num_chunks} chunks, size={self.chunk_size:,}")
        return plan

    def _get_pk_bounds(self, schema: str, table: str, pk_col: str) -> Tuple[Optional[int], Optional[int]]:
        """Obtiene MIN/MAX de la PK para calcular rangos."""
        engine = self.sql_server.create_engine_isolated(database=self.db_name, timeout=30)
        try:
            with engine.connect() as conn:
                row = conn.execute(text(
                    f"SELECT MIN([{pk_col}]), MAX([{pk_col}]) FROM [{schema}].[{table}] WITH (NOLOCK)"
                )).fetchone()
                if row and row[0] is not None:
                    return int(row[0]), int(row[1])
                return None, None
        except Exception as e:
            logger.warning(f"  PK bounds query falló: {e}")
            return None, None
        finally:
            engine.dispose()


# ═══════════════════════════════════════════════════════════════════════════════
# PARQUET WRITER
# ═══════════════════════════════════════════════════════════════════════════════

class ParquetWriter:
    """Escribe chunks individuales como archivos Parquet con ZSTD.

    Cada chunk se escribe como archivo independiente — no acumula en RAM.
    Layout: output_dir/{table_name}/chunk_{idx:06d}.parquet
    """

    COMPRESSION = "zstd"
    COMPRESSION_LEVEL = 3

    def __init__(self, output_dir: str):
        self.output_dir = output_dir

    def write_chunk(self, df: pd.DataFrame, chunk: ChunkSpec) -> Tuple[str, str]:
        """Escribe DataFrame como Parquet. Retorna (path, checksum)."""
        table_dir = os.path.join(self.output_dir, chunk.table)
        os.makedirs(table_dir, exist_ok=True)
        path = os.path.join(table_dir, f"chunk_{chunk.chunk_idx:06d}.parquet")

        # Convertir a Arrow Table preservando tipos
        arrow_table = pa.Table.from_pandas(df, preserve_index=False)

        # Escribir con ZSTD compresión
        pq.write_table(
            arrow_table, path,
            compression=self.COMPRESSION,
            compression_level=self.COMPRESSION_LEVEL,
            use_dictionary=True,
            write_statistics=True,
        )

        # Checksum del archivo resultante
        checksum = self._file_checksum(path)
        return path, checksum

    def cleanup_incomplete(self, chunk: ChunkSpec):
        """Elimina archivo Parquet incompleto (para re-extracción)."""
        table_dir = os.path.join(self.output_dir, chunk.table)
        path = os.path.join(table_dir, f"chunk_{chunk.chunk_idx:06d}.parquet")
        if os.path.exists(path):
            os.remove(path)
            logger.info(f"  Limpiado Parquet incompleto: {path}")

    @staticmethod
    def _file_checksum(path: str) -> str:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(8192), b""):
                h.update(block)
        return h.hexdigest()


# ═══════════════════════════════════════════════════════════════════════════════
# METRICS COLLECTOR
# ═══════════════════════════════════════════════════════════════════════════════

class MetricsCollector:
    """Recopila métricas de extracción en tiempo real. Thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self._table_name: str = ""
        self._total_rows: int = 0
        self._total_chunks: int = 0
        self._extracted_rows: int = 0
        self._completed_chunks: int = 0
        self._failed_chunks: int = 0
        self._skipped_chunks: int = 0
        self._retry_count: int = 0
        self._sql_ms_total: float = 0
        self._write_ms_total: float = 0
        self._start_time: float = 0
        self._last_report_time: float = 0
        self._report_interval: float = 30.0  # Reportar cada 30s
        self._report_chunk_interval: int = 10  # O cada 10 chunks

    def start_table(self, table: str, total_rows: int, total_chunks: int):
        with self._lock:
            self._table_name = table
            self._total_rows = total_rows
            self._total_chunks = total_chunks
            self._extracted_rows = 0
            self._completed_chunks = 0
            self._failed_chunks = 0
            self._skipped_chunks = 0
            self._retry_count = 0
            self._sql_ms_total = 0
            self._write_ms_total = 0
            self._start_time = time.time()
            self._last_report_time = time.time()

    def record_chunk(self, result: ChunkResult):
        with self._lock:
            self._completed_chunks += 1
            self._extracted_rows += result.rows_extracted
            self._sql_ms_total += result.sql_ms
            self._write_ms_total += result.write_ms
            self._retry_count += result.retry_count

            should_report = (
                self._completed_chunks % self._report_chunk_interval == 0
                or time.time() - self._last_report_time >= self._report_interval
            )

        if should_report:
            self._report_progress()

    def record_skip(self):
        with self._lock:
            self._skipped_chunks += 1

    def record_failure(self):
        with self._lock:
            self._failed_chunks += 1

    def _report_progress(self):
        with self._lock:
            elapsed = time.time() - self._start_time
            if elapsed < 0.1:
                return

            rows_per_sec = self._extracted_rows / elapsed
            done = self._completed_chunks + self._skipped_chunks
            remaining_chunks = self._total_chunks - done - self._failed_chunks
            eta = (remaining_chunks * (elapsed / max(done, 1))) if done > 0 else 0

            pct = (self._extracted_rows / max(self._total_rows, 1)) * 100
            mem_mb = psutil.Process().memory_info().rss / (1024 * 1024)

            avg_sql = self._sql_ms_total / max(self._completed_chunks, 1)
            avg_write = self._write_ms_total / max(self._completed_chunks, 1)

            self._last_report_time = time.time()

        logger.info(
            f"  [{self._table_name}] {done}/{self._total_chunks} chunks | "
            f"{self._extracted_rows:,}/{self._total_rows:,} rows ({pct:.1f}%)"
        )
        logger.info(
            f"    Throughput: {rows_per_sec:,.0f} rows/s | "
            f"ETA: {eta:.0f}s | Retries: {self._retry_count} | "
            f"Memory: {mem_mb:.0f} MB"
        )
        logger.info(
            f"    Avg SQL: {avg_sql:.0f}ms | Avg Write: {avg_write:.0f}ms | "
            f"Failed: {self._failed_chunks}"
        )

    def get_summary(self) -> Dict[str, Any]:
        with self._lock:
            elapsed = time.time() - self._start_time
            return {
                "table": self._table_name,
                "total_rows": self._total_rows,
                "extracted_rows": self._extracted_rows,
                "total_chunks": self._total_chunks,
                "completed_chunks": self._completed_chunks,
                "failed_chunks": self._failed_chunks,
                "skipped_chunks": self._skipped_chunks,
                "retry_count": self._retry_count,
                "rows_per_second": self._extracted_rows / max(elapsed, 0.1),
                "elapsed_seconds": elapsed,
                "avg_sql_ms": self._sql_ms_total / max(self._completed_chunks, 1),
                "avg_write_ms": self._write_ms_total / max(self._completed_chunks, 1),
                "peak_memory_mb": psutil.Process().memory_info().rss / (1024 * 1024),
            }


# ═══════════════════════════════════════════════════════════════════════════════
# PARALLEL EXTRACTOR
# ═══════════════════════════════════════════════════════════════════════════════

class ParallelExtractor:
    """Extracción paralela con ThreadPoolExecutor, backpressure y retry.

    Cada worker thread crea su propio engine SQLAlchemy (NullPool).
    Backpressure: máximo in_flight_limit futures concurrentes.
    """

    def __init__(self, sql_server, db_name: str, max_workers: int = 4,
                 checkpoint: CheckpointManager = None, metrics: MetricsCollector = None,
                 writer: ParquetWriter = None, retry_policy: RetryPolicy = None):
        self.sql_server = sql_server
        self.db_name = db_name
        self.max_workers = max_workers
        self.checkpoint = checkpoint
        self.metrics = metrics
        self.writer = writer
        self.retry_policy = retry_policy or RetryPolicy()
        self._shutdown = threading.Event()

    def extract_table(self, job_id: str, plan: ExtractionPlan) -> Dict[str, Any]:
        """Extrae todos los chunks de una tabla en paralelo."""
        pending_idxs = self.checkpoint.get_pending_chunks(
            job_id, plan.schema, plan.table
        )
        skipped = plan.num_chunks - len(pending_idxs)
        if skipped > 0:
            logger.info(f"  Resumiendo: {skipped} chunks ya completados, {len(pending_idxs)} pendientes")
            for _ in range(skipped):
                self.metrics.record_skip()

        if not pending_idxs:
            logger.info(f"  Tabla completamente extraída (resume)")
            return self.metrics.get_summary()

        # Filtrar chunks pendientes
        chunks_to_extract = [c for c in plan.chunks if c.chunk_idx in set(pending_idxs)]

        # Limpiar Parquet incompletos antes de re-extraer
        for chunk in chunks_to_extract:
            self.writer.cleanup_incomplete(chunk)

        in_flight_limit = self.max_workers * 2
        results = []

        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="extractor") as pool:
            futures: Dict[Future, ChunkSpec] = {}

            for chunk in chunks_to_extract:
                if self._shutdown.is_set():
                    break

                # Backpressure
                while len(futures) >= in_flight_limit:
                    self._collect_completed(futures, results, job_id)

                future = pool.submit(self._extract_chunk_with_retry, chunk)
                futures[future] = chunk

            # Recolectar todos los pendientes
            while futures:
                self._collect_completed(futures, results, job_id)

        return self.metrics.get_summary()

    def _collect_completed(self, futures: Dict[Future, ChunkSpec],
                           results: list, job_id: str):
        """Espera a que al menos un future complete y procesa resultado."""
        done_futures = []
        for f in list(futures.keys()):
            if f.done():
                done_futures.append(f)

        if not done_futures:
            time.sleep(0.1)
            for f in list(futures.keys()):
                if f.done():
                    done_futures.append(f)

        if not done_futures:
            # Esperar al primero
            for f in as_completed(list(futures.keys())):
                done_futures.append(f)
                break

        for f in done_futures:
            chunk = futures.pop(f)
            try:
                result = f.result()
                if result.error:
                    self.checkpoint.mark_chunk_failed(
                        job_id, chunk, result.error, result.retry_count
                    )
                    self.metrics.record_failure()
                else:
                    self.checkpoint.mark_chunk_completed(job_id, result)
                    self.metrics.record_chunk(result)
                results.append(result)
            except Exception as e:
                logger.error(f"  Worker exception para {chunk.chunk_id}: {e}")
                self.checkpoint.mark_chunk_failed(job_id, chunk, str(e), 0)
                self.metrics.record_failure()

    def _extract_chunk_with_retry(self, chunk: ChunkSpec) -> ChunkResult:
        """Extrae un chunk con retry automático."""
        last_error = None
        for attempt in range(self.retry_policy.max_retries + 1):
            try:
                return self._extract_single_chunk(chunk, attempt)
            except Exception as e:
                last_error = e
                should_retry, delay = self.retry_policy.should_retry(e, attempt)
                if should_retry and not self._shutdown.is_set():
                    logger.warning(
                        f"  Retry {attempt + 1}/{self.retry_policy.max_retries} "
                        f"para {chunk.chunk_id}: {e} (delay={delay:.1f}s)"
                    )
                    time.sleep(delay)
                else:
                    break

        return ChunkResult(
            chunk_spec=chunk, rows_extracted=0, parquet_path=None,
            checksum=None, sql_ms=0, write_ms=0, total_ms=0,
            error=str(last_error), retry_count=attempt,
        )

    def _extract_single_chunk(self, chunk: ChunkSpec, attempt: int) -> ChunkResult:
        """Extrae un chunk individual: SQL query → DataFrame → Parquet."""
        t_total = time.time()

        # Construir query según estrategia
        query = self._build_query(chunk)

        # Extraer de SQL Server
        engine = self.sql_server.create_engine_isolated(
            database=self.db_name, timeout=120
        )
        try:
            t_sql = time.time()
            df = pd.read_sql_query(query, engine)
            sql_ms = (time.time() - t_sql) * 1000
        finally:
            engine.dispose()

        if df.empty:
            return ChunkResult(
                chunk_spec=chunk, rows_extracted=0, parquet_path=None,
                checksum=None, sql_ms=sql_ms, write_ms=0,
                total_ms=(time.time() - t_total) * 1000, retry_count=attempt,
            )

        # Escribir Parquet
        t_write = time.time()
        path, checksum = self.writer.write_chunk(df, chunk)
        write_ms = (time.time() - t_write) * 1000

        total_ms = (time.time() - t_total) * 1000

        return ChunkResult(
            chunk_spec=chunk, rows_extracted=len(df), parquet_path=path,
            checksum=checksum, sql_ms=sql_ms, write_ms=write_ms,
            total_ms=total_ms, retry_count=attempt,
        )

    def _build_query(self, chunk: ChunkSpec) -> str:
        """Construye SQL query para el chunk según la estrategia."""
        fqn = f"[{chunk.schema}].[{chunk.table}]"

        if chunk.strategy == "pk_range" and chunk.pk_column:
            return (
                f"SELECT * FROM {fqn} WITH (NOLOCK) "
                f"WHERE [{chunk.pk_column}] >= {chunk.range_start} "
                f"AND [{chunk.pk_column}] <= {chunk.range_end} "
                f"ORDER BY [{chunk.pk_column}]"
            )
        else:
            # OFFSET/FETCH — requiere ORDER BY
            return (
                f"SELECT * FROM {fqn} WITH (NOLOCK) "
                f"ORDER BY (SELECT NULL) "
                f"OFFSET {chunk.offset} ROWS FETCH NEXT {chunk.limit} ROWS ONLY"
            )

    def shutdown(self):
        """Señal de shutdown graceful."""
        self._shutdown.set()


# ═══════════════════════════════════════════════════════════════════════════════
# EXTRACTION ENGINE (ORQUESTADOR TOP-LEVEL)
# ═══════════════════════════════════════════════════════════════════════════════

class ExtractionEngine:
    """Orquesta la extracción completa: planificación → paralelismo → Parquet.

    Uso:
        engine = ExtractionEngine(sql_server, db_name, output_dir)
        results = engine.extract(tables, resume=True, max_workers=4)
    """

    def __init__(self, sql_server, db_name: str, output_dir: str,
                 chunk_size: int = 100_000):
        self.sql_server = sql_server
        self.db_name = db_name
        self.output_dir = output_dir
        self.chunk_size = chunk_size

        # Sub-componentes
        os.makedirs(output_dir, exist_ok=True)
        checkpoint_path = os.path.join(output_dir, "checkpoint.db")
        self.checkpoint = CheckpointManager(checkpoint_path)
        self.planner = ExtractionPlanner(sql_server, db_name, chunk_size)
        self.writer = ParquetWriter(output_dir)
        self.metrics = MetricsCollector()
        self.retry_policy = RetryPolicy()

    def extract(self, tables: List[Tuple[str, str, int]],
                resume: bool = True, max_workers: int = 4,
                bak_file: str = "") -> Dict[str, Any]:
        """Extrae todas las tablas con checkpointing y paralelismo.

        Args:
            tables: Lista de (schema, table, row_count) del TableScanner
            resume: Si True, reanuda un job previo si existe
            max_workers: Threads concurrentes de extracción
            bak_file: Ruta al archivo .bak (para checkpoint matching)

        Returns:
            Resumen global del job con métricas
        """
        # ── Resolver job ID (nuevo o resumido) ──
        job_id = None
        if resume:
            job_id = self.checkpoint.find_resumable_job(bak_file, self.db_name)
            if job_id:
                logger.info(f"═══ RESUMIENDO JOB EXISTENTE: {job_id} ═══")

        if not job_id:
            job_id = self.checkpoint.create_job(bak_file, self.db_name)
            logger.info(f"═══ NUEVO JOB DE EXTRACCIÓN: {job_id} ═══")

        logger.info(f"  Workers: {max_workers} | Chunk size: {self.chunk_size:,}")
        logger.info(f"  Output: {self.output_dir}")
        logger.info(f"  Tablas: {len(tables)}")

        # ── Crear extractor paralelo ──
        extractor = ParallelExtractor(
            sql_server=self.sql_server, db_name=self.db_name,
            max_workers=max_workers, checkpoint=self.checkpoint,
            metrics=self.metrics, writer=self.writer,
            retry_policy=self.retry_policy,
        )

        all_summaries = []
        total_start = time.time()

        try:
            for i, (schema, table, row_count) in enumerate(tables, 1):
                logger.info(f"\n{'═' * 70}")
                logger.info(f"TABLA {i}/{len(tables)}: [{schema}].[{table}] ({row_count:,} filas)")
                logger.info(f"{'═' * 70}")

                # 1. Planificar
                plan = self.planner.plan_table(schema, table, row_count)

                # 2. Registrar en checkpoint
                self.checkpoint.register_table(job_id, plan)
                self.checkpoint.register_chunks(job_id, plan.chunks)

                # 3. Iniciar métricas
                self.metrics.start_table(table, row_count, plan.num_chunks)

                # 4. Extraer en paralelo
                summary = extractor.extract_table(job_id, plan)

                # 5. Marcar tabla como completada
                failed = summary.get("failed_chunks", 0)
                status = "completed" if failed == 0 else "partial"
                self.checkpoint.complete_table(job_id, schema, table, status)

                all_summaries.append(summary)
                logger.info(f"  ✅ [{table}] completada: {summary['extracted_rows']:,} filas en {summary['elapsed_seconds']:.1f}s")

            # ── Finalizar job ──
            self.checkpoint.complete_job(job_id)
            total_elapsed = time.time() - total_start

            # ── Resumen global ──
            global_summary = self._build_global_summary(all_summaries, total_elapsed, job_id)
            self._log_final_report(global_summary)
            self._save_manifest(global_summary)

            return global_summary

        except KeyboardInterrupt:
            logger.warning("\n⚠️ CTRL+C detectado — guardando estado para resume...")
            extractor.shutdown()
            logger.info(f"  Job {job_id} guardado. Reanuda con --resume")
            raise
        except Exception as e:
            logger.error(f"Error en extracción: {e}", exc_info=True)
            self.checkpoint.complete_job(job_id, status="failed")
            raise
        finally:
            self.checkpoint.close()

    def _build_global_summary(self, summaries: List[Dict], elapsed: float, job_id: str) -> Dict:
        total_rows = sum(s["extracted_rows"] for s in summaries)
        return {
            "job_id": job_id,
            "tables_processed": len(summaries),
            "total_rows_extracted": total_rows,
            "total_elapsed_seconds": elapsed,
            "overall_rows_per_second": total_rows / max(elapsed, 0.1),
            "peak_memory_mb": psutil.Process().memory_info().rss / (1024 * 1024),
            "output_dir": self.output_dir,
            "tables": summaries,
        }

    def _log_final_report(self, summary: Dict):
        logger.info(f"\n{'═' * 70}")
        logger.info("RESUMEN FINAL DE EXTRACCIÓN")
        logger.info(f"{'═' * 70}")
        logger.info(f"  Job ID:        {summary['job_id']}")
        logger.info(f"  Tablas:        {summary['tables_processed']}")
        logger.info(f"  Filas totales: {summary['total_rows_extracted']:,}")
        logger.info(f"  Tiempo total:  {summary['total_elapsed_seconds']:.1f}s")
        logger.info(f"  Throughput:    {summary['overall_rows_per_second']:,.0f} rows/s")
        logger.info(f"  Memoria pico:  {summary['peak_memory_mb']:.0f} MB")
        logger.info(f"  Output:        {summary['output_dir']}")
        logger.info(f"{'═' * 70}")

    def _save_manifest(self, summary: Dict):
        """Guarda manifiesto JSON del job."""
        meta_dir = os.path.join(self.output_dir, "_metadata")
        os.makedirs(meta_dir, exist_ok=True)
        path = os.path.join(meta_dir, "extraction_manifest.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False, default=str)
        logger.info(f"  Manifest: {path}")
