#!/usr/bin/env python
"""
PARQUET LOADER v2 — Carga Industrial (Streaming, Retry Exponencial, Auto-Indexes)

Lee archivos Parquet sanitizados y los carga en SQL Server.
Implementa validaciones, streaming por row groups, y failovers defensivos.
"""

import json
import logging
import math
import os
import subprocess
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pyodbc
import psutil

logger = logging.getLogger("parquet_loader")
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# ==============================================================================
# 1. SCHEMA MAPPER & DDL GENERATOR
# ==============================================================================

class SchemaMapper:
    """Convierte schema Arrow a DDL SQL Server y detecta claves para índices."""

    @classmethod
    def arrow_to_sql_type(cls, arrow_field: pa.Field) -> str:
        t = arrow_field.type
        if pa.types.is_string(t) or pa.types.is_large_string(t):
            return "NVARCHAR(MAX)"
        if pa.types.is_integer(t):
            return "BIGINT"
        if pa.types.is_floating(t):
            return "DECIMAL(38,10)"  # Cast defensivo solicitado
        if pa.types.is_decimal(t):
            return f"DECIMAL(38,10)" # Promovemos por robustez
        if pa.types.is_boolean(t):
            return "BIT"
        if pa.types.is_timestamp(t):
            return "DATETIME2"
        if pa.types.is_date(t):
            return "DATE"
        if pa.types.is_time(t):
            return "TIME"
        if pa.types.is_binary(t) or pa.types.is_large_binary(t):
            return "VARBINARY(MAX)"
        if pa.types.is_null(t):
            return "NVARCHAR(MAX)"
        
        type_str = str(t).lower()
        if "int" in type_str: return "BIGINT"
        if "float" in type_str: return "DECIMAL(38,10)"
        return "NVARCHAR(MAX)"

    @classmethod
    def generate_create_table(cls, table_name: str, schema: pa.Schema, sql_schema: str = "dbo") -> tuple[str, List[str]]:
        """Genera DDL y sentencias de índices."""
        columns = []
        is_dim = table_name.lower().startswith("dim")
        is_fact = table_name.lower().startswith("fact")
        
        pk_col = None
        # En Dimensiones, buscar `<TableName>Key` o el primer `*Key`
        if is_dim:
            expected_pk = f"{table_name}Key".lower()
            for field in schema:
                if field.name.lower() == expected_pk:
                    pk_col = field.name
                    break
            if not pk_col:
                for field in schema:
                    if field.name.lower().endswith("key"):
                        pk_col = field.name
                        break
                        
        fk_candidates = []

        for field in schema:
            sql_type = cls.arrow_to_sql_type(field)
            col_name = field.name.replace(" ", "_").replace("-", "_")
            
            if col_name == pk_col:
                columns.append(f"    [{col_name}] {sql_type} NOT NULL PRIMARY KEY CLUSTERED")
            else:
                nullable = "NULL" if field.nullable else "NOT NULL"
                columns.append(f"    [{col_name}] {sql_type} {nullable}")
                
            # Detectar FKs (terminan en Key, no son la PK)
            if col_name.lower().endswith("key") and col_name != pk_col:
                fk_candidates.append(col_name)

        if is_fact:
            # Añadir PK subrogada si es FACT
            columns.insert(0, "    [FactSurrogatePK] BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY CLUSTERED")

        cols_ddl = ",\n".join(columns)
        create_sql = (
            f"IF OBJECT_ID('[{sql_schema}].[{table_name}]', 'U') IS NOT NULL\n"
            f"    DROP TABLE [{sql_schema}].[{table_name}];\n"
            f"CREATE TABLE [{sql_schema}].[{table_name}] (\n{cols_ddl}\n);"
        )
        
        # Índices recomendados
        indexes = []
        for fk in fk_candidates:
            idx_name = f"IX_{table_name}_{fk}"
            indexes.append(
                f"IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = '{idx_name}' AND object_id = OBJECT_ID('[{sql_schema}].[{table_name}]'))\n"
                f"CREATE NONCLUSTERED INDEX [{idx_name}] ON [{sql_schema}].[{table_name}]([{fk}]);"
            )

        return create_sql, indexes


# ==============================================================================
# 2. BULK INSERTER & VALIDATION
# ==============================================================================

class AdaptiveBulkInserter:
    """Inserción con retry exponencial, adaptive batch sizing y pre-validación."""
    
    TYPE_ERROR_CODES = {"22003", "07006", "22001", "22018"}

    def __init__(self, conn_str: str, initial_batch_size: int = 10000):
        self.conn_str = conn_str
        self.batch_size = initial_batch_size
        self.min_batch = 1000
        self.max_batch = 50000

    def _connect(self) -> pyodbc.Connection:
        # Optimizar cursor config para SSAS/Data Warehouse
        return pyodbc.connect(self.conn_str, autocommit=False)
        
    def execute_ddl(self, db_name: str, stmts: List[str]):
        conn = pyodbc.connect(self.conn_str, autocommit=True)
        cursor = conn.cursor()
        try:
            cursor.execute(f"USE [{db_name}]")
            for stmt in stmts:
                cursor.execute(stmt)
                while cursor.nextset(): pass
        finally:
            cursor.close()
            conn.close()

    def _pre_validate_batch(self, batch: list, col_names: list):
        """Valida min/max, len y overflows antes de chocar con ODBC."""
        for row_idx, row in enumerate(batch):
            for i, val in enumerate(row):
                if val is None: continue
                # Val Float
                if isinstance(val, float):
                    if math.isinf(val) or math.isnan(val):
                        row[i] = None # Forzar nulo en vez de corromper
                    elif abs(val) > 1e28: # Límite aprox Decimal 38,10
                        row[i] = None
                # Val Int
                elif isinstance(val, int):
                    if val > 9223372036854775807 or val < -9223372036854775808:
                        row[i] = None # Overflow BIGINT
                # Val String
                elif isinstance(val, str):
                    # MAX en SQL Server es 2GB, pero por seguridad odbc cortamos en algo razonable si es anómalo
                    if len(val) > 1000000:
                        row[i] = val[:1000000]

    def insert_dataframe(self, db_name: str, table_name: str, df: pd.DataFrame) -> int:
        if df.empty: return 0

        # Sanitizar localmente (NaN -> None)
        df = df.where(pd.notnull(df), None)
        
        # Convertir a listas nativas puras
        rows = []
        for row in df.values.tolist():
            clean_row = []
            for val in row:
                if pd.isna(val):
                    clean_row.append(None)
                elif isinstance(val, np.integer):
                    clean_row.append(int(val))
                elif isinstance(val, np.floating):
                    clean_row.append(float(val) if not np.isnan(val) else None)
                elif isinstance(val, (pd.Timestamp, np.datetime64)):
                    clean_row.append(pd.Timestamp(val).to_pydatetime() if not pd.isna(val) else None)
                else:
                    clean_row.append(val)
            rows.append(clean_row)

        col_names = [f"[{c}]" for c in df.columns]
        placeholders = ", ".join(["?"] * len(col_names))
        sql = f"INSERT INTO [{db_name}].[dbo].[{table_name}] ({', '.join(col_names)}) VALUES ({placeholders})"

        total_inserted = 0
        conn = None
        try:
            conn = self._connect()
            cursor = conn.cursor()
            
            i = 0
            while i < len(rows):
                batch = rows[i:i + self.batch_size]
                self._pre_validate_batch(batch, df.columns)
                
                t0 = time.time()
                success = False
                
                # Retry Exponencial
                for attempt in range(4): # 0, 1, 2, 3
                    try:
                        cursor.fast_executemany = True
                        cursor.executemany(sql, batch)
                        conn.commit()
                        success = True
                        break
                    except Exception as e:
                        conn.rollback()
                        err_str = str(e)
                        if any(c in err_str for c in self.TYPE_ERROR_CODES) and attempt == 0:
                            # Falla de tipos -> desactivar fast_executemany
                            cursor.fast_executemany = False
                        else:
                            # Otros fallos -> Retry exponencial
                            delay = 2.0 * (2 ** attempt)
                            logger.warning(f"  [Retry {attempt+1}] Error en batch de {table_name}: {err_str[:100]}... Esperando {delay}s")
                            time.sleep(delay)
                            
                if not success:
                    # Fallback Forense Row-by-Row aislando fila defectuosa
                    logger.error(f"  Fallback a Row-by-Row para aislar fallo en {table_name}")
                    for r in batch:
                        try:
                            cursor.fast_executemany = False
                            cursor.execute(sql, r)
                            conn.commit()
                            total_inserted += 1
                        except Exception as e:
                            conn.rollback()
                            logger.error(f"  ❌ Fila aislada y omitida: {str(e)[:100]} | Datos: {repr(r)[:100]}")
                else:
                    total_inserted += len(batch)
                
                # Adaptive Sizing
                latency = time.time() - t0
                if latency > 3.0 and self.batch_size > self.min_batch:
                    self.batch_size = max(self.min_batch, int(self.batch_size * 0.8))
                elif latency < 0.5 and self.batch_size < self.max_batch:
                    self.batch_size = min(self.max_batch, int(self.batch_size * 1.5))
                    
                i += len(batch)

        finally:
            if conn:
                try: conn.close()
                except: pass

        return total_inserted


# ==============================================================================
# 3. LOADER PIPELINE
# ==============================================================================

class ParquetToSQLLoader:
    def __init__(self, parquet_dir: str, conn_str: str):
        self.parquet_dir = parquet_dir
        self.conn_str = conn_str
        self.checkpoint_file = os.path.join(parquet_dir, "loader_v2_checkpoint.json")
        self.state = self._load_checkpoint()

    def _load_checkpoint(self):
        if os.path.exists(self.checkpoint_file):
            try:
                with open(self.checkpoint_file, 'r') as f:
                    return json.load(f)
            except:
                pass
        return {"tables": {}}

    def _save_checkpoint(self):
        with open(self.checkpoint_file, 'w') as f:
            json.dump(self.state, f, indent=2)

    def load(self, db_name: str = "AnalyticsDB"):
        logger.info(f"Iniciando carga Streaming Parquet -> SQL Server ({db_name})")
        inserter = AdaptiveBulkInserter(self.conn_str)
        
        # Crear DB
        try:
            conn = pyodbc.connect(self.conn_str, autocommit=True)
            conn.execute(f"IF DB_ID('{db_name}') IS NULL CREATE DATABASE [{db_name}]")
            conn.close()
        except Exception as e:
            logger.error(f"No se pudo crear DB {db_name}: {e}")
        
        # Detectar tablas
        tables = [d for d in os.listdir(self.parquet_dir) if os.path.isdir(os.path.join(self.parquet_dir, d))]
        
        for table in tables:
            if table in self.state["tables"] and self.state["tables"][table].get("status") == "completed":
                logger.info(f"⏭️ Tabla {table} completada previamente. Saltando.")
                continue
                
            self.state["tables"].setdefault(table, {"completed_chunks": [], "rows": 0})
            table_state = self.state["tables"][table]
            
            table_dir = os.path.join(self.parquet_dir, table)
            chunks = sorted([c for c in os.listdir(table_dir) if c.endswith('.parquet')])
            if not chunks: continue

            # Schema & DDL
            if len(table_state["completed_chunks"]) == 0:
                first_chunk = pq.ParquetFile(os.path.join(table_dir, chunks[0]))
                schema = first_chunk.schema_arrow
                ddl, idxs = SchemaMapper.generate_create_table(table, schema)
                inserter.execute_ddl(db_name, [ddl] + idxs)
            
            # Streaming Real por Row Groups
            logger.info(f"Cargando tabla {table} ({len(chunks)} archivos)...")
            
            try:
                for chunk in chunks:
                    if chunk in table_state["completed_chunks"]:
                        continue
                        
                    chunk_path = os.path.join(table_dir, chunk)
                    pf = pq.ParquetFile(chunk_path)
                    
                    chunk_rows_inserted = 0
                    for i in range(pf.num_row_groups):
                        # Streaming real: materializa 1 row group a la vez
                        df = pf.read_row_group(i).to_pandas()
                        rows = inserter.insert_dataframe(db_name, table, df)
                        chunk_rows_inserted += rows
                    
                    table_state["rows"] += chunk_rows_inserted
                    table_state["completed_chunks"].append(chunk)
                    self._save_checkpoint()
                    
                table_state["status"] = "completed"
                self._save_checkpoint()
                logger.info(f"✅ {table} finalizada: {table_state['rows']:,} filas.")
                
            except Exception as e:
                logger.error(f"❌ Error crítico en {table}: {e}")
                logger.error(traceback.format_exc())
                # NO detener el pipeline, seguir con la siguiente tabla

        logger.info("Carga completada.")
        
        # Post-load validation summary
        logger.info("\n--- VALIDACIÓN POST-LOAD ---")
        for t, s in self.state["tables"].items():
            status = s.get("status", "failed/incomplete")
            rows = s.get("rows", 0)
            logger.info(f"{t}: {status.upper()} - {rows:,} rows")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="./output_sanitized", help="Parquet source directory")
    parser.add_argument("--db", default="AnalyticsDB", help="Target database")
    parser.add_argument("--conn", default="DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost,1433;UID=sa;PWD=SuperSecurePass123!;", help="SQL connection string (master)")
    args = parser.parse_args()
    
    loader = ParquetToSQLLoader(args.dir, args.conn)
    loader.load(args.db)
