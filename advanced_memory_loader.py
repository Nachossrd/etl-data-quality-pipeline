#!/usr/bin/env python
"""
ADVANCED MEMORY-BOUNDED LOADER
Carga masiva multi-millón de Parquet a SQL Server con estrictos límites de RAM,
backpressure, profiling exhaustivo y adaptive chunk sizing.
"""

import cProfile
import gc
import json
import logging
import math
import os
import pstats
import time
import traceback
from collections import deque
from datetime import datetime

import numpy as np
import pandas as pd
import psutil
import pyarrow.parquet as pq
import pyodbc

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(process)d] %(message)s')
logger = logging.getLogger("MemoryBoundedLoader")

# Configuraciones Críticas
CONN_STR = "DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost,1433;DATABASE=AnalyticsDB;UID=sa;PWD=SuperSecurePass123!"
MAX_RAM_PERCENT = 85.0       # Threshold para backpressure (pausa)
ABORT_RAM_PERCENT = 92.0     # Threshold para aborto de emergencia
MIN_THROUGHPUT_RPS = 50      # Filas por segundo mínimas antes de abortar por colapso
MAX_RETRIES = 5              # Retries máximos por chunk antes de abortar

class ProfilerManager:
    def __init__(self, out_dir):
        self.profiler = cProfile.Profile()
        self.out_dir = out_dir
        os.makedirs(out_dir, exist_ok=True)
        
    def start(self):
        self.profiler.enable()
        
    def stop_and_save(self, name):
        self.profiler.disable()
        path = os.path.join(self.out_dir, f"{name}_profile.prof")
        stats = pstats.Stats(self.profiler).sort_stats('tottime')
        stats.dump_stats(path)
        
        # Dump legíble
        with open(os.path.join(self.out_dir, f"{name}_profile.txt"), "w") as f:
            stats = pstats.Stats(self.profiler, stream=f).sort_stats('cumtime')
            stats.print_stats(50)
        logger.info(f"Profiling guardado en: {path}")

class MemoryBoundedInserter:
    def __init__(self, conn_str):
        self.conn_str = conn_str
        self.batch_size = 10000
        self.throughput_window = deque(maxlen=10)
        self.total_rows_inserted = 0
        
        # Metrics
        self.metrics = {
            "peak_memory_mb": 0,
            "total_retries": 0,
            "avg_latency_ms": 0,
            "total_time_s": 0
        }
        
    def _check_memory_pressure(self):
        """Monitoreo residente de RAM con backpressure y aborto."""
        mem = psutil.virtual_memory()
        process = psutil.Process(os.getpid())
        process_mb = process.memory_info().rss / (1024 * 1024)
        
        if process_mb > self.metrics["peak_memory_mb"]:
            self.metrics["peak_memory_mb"] = process_mb
            
        if mem.percent > ABORT_RAM_PERCENT:
            logger.critical(f"🚨 RAM en {mem.percent}%. Aborto de emergencia activado.")
            raise MemoryError("Exceeded critical RAM threshold.")
            
        if mem.percent > MAX_RAM_PERCENT:
            logger.warning(f"⚠️ Presión de RAM detectada ({mem.percent}%). Aplicando backpressure (GC y sleep 5s)...")
            gc.collect()
            time.sleep(5)
            
    def _check_throughput_collapse(self, latency_s, rows):
        """Valida que el throughput no colapse silenciosamente."""
        if latency_s > 0:
            rps = rows / latency_s
            self.throughput_window.append(rps)
            
            # Evaluar colapso después de tener algo de historia
            if len(self.throughput_window) == 10:
                avg_rps = sum(self.throughput_window) / 10
                if avg_rps < MIN_THROUGHPUT_RPS:
                    logger.critical(f"🚨 Throughput colapsado: {avg_rps:.1f} rows/sec. Abortando por seguridad.")
                    raise RuntimeError("Throughput collapse detected.")

    def _ensure_db_and_table(self, db_name, table_name, df):
        # Ensure DB
        master_conn_str = self.conn_str.replace("DATABASE=" + db_name, "DATABASE=master")
        conn_master = pyodbc.connect(master_conn_str, autocommit=True)
        cursor_m = conn_master.cursor()
        try:
            cursor_m.execute(f"IF NOT EXISTS (SELECT * FROM sys.databases WHERE name = '{db_name}') CREATE DATABASE [{db_name}]")
        except Exception as e:
            logger.error(f"Error checking/creating DB: {e}")
        finally:
            cursor_m.close()
            conn_master.close()
            
        # Ensure Table
        conn = pyodbc.connect(self.conn_str, autocommit=True)
        cursor = conn.cursor()
        
        # Check if table exists
        cursor.execute(f"SELECT OBJECT_ID('[{db_name}].[dbo].[{table_name}]')")
        if cursor.fetchone()[0] is None:
            logger.info(f"Creando tabla {table_name}...")
            cols_def = []
            for col, dtype in df.dtypes.items():
                sql_type = "NVARCHAR(4000)"
                if pd.api.types.is_integer_dtype(dtype): sql_type = "BIGINT"
                elif pd.api.types.is_float_dtype(dtype): sql_type = "FLOAT"
                elif pd.api.types.is_bool_dtype(dtype): sql_type = "BIT"
                elif pd.api.types.is_datetime64_any_dtype(dtype): sql_type = "DATETIME2"
                cols_def.append(f"[{col}] {sql_type}")
            
            create_sql = f"CREATE TABLE [{db_name}].[dbo].[{table_name}] ({', '.join(cols_def)})"
            cursor.execute(create_sql)
        cursor.close()
        conn.close()

    def insert_chunk(self, db_name: str, table_name: str, df: pd.DataFrame):
        self._check_memory_pressure()
        self._ensure_db_and_table(db_name, table_name, df)
        
        # Zero-copy approach mitigation: Transform to native Python lists via Arrow-Pandas fast unboxing
        df = df.astype(object).where(pd.notnull(df), None)
        columns = [f"[{c}]" for c in df.columns]
        placeholders = ", ".join(["?"] * len(columns))
        sql = f"INSERT INTO [{db_name}].[dbo].[{table_name}] ({', '.join(columns)}) VALUES ({placeholders})"
        
        records = df.values.tolist()
        # Explicit free of DataFrame
        del df
        gc.collect()
        
        conn = pyodbc.connect(self.conn_str, autocommit=False)
        cursor = conn.cursor()
        
        i = 0
        chunk_t0 = time.time()
        
        try:
            while i < len(records):
                batch = records[i:i + self.batch_size]
                success = False
                batch_t0 = time.time()
                
                for attempt in range(MAX_RETRIES):
                    try:
                        cursor.fast_executemany = True
                        cursor.executemany(sql, batch)
                        conn.commit()
                        success = True
                        break
                    except Exception as e:
                        conn.rollback()
                        self.metrics["total_retries"] += 1
                        
                        # Type coercion fallback check
                        if "22003" in str(e) or "07006" in str(e) or "22018" in str(e) or "HY090" in str(e):
                            cursor.fast_executemany = False # Degrade engine
                            
                        # Exponential backoff
                        delay = 1.0 * (2 ** attempt)
                        logger.warning(f"  Retry {attempt+1}/{MAX_RETRIES} - {str(e)[:80]}. Sleep {delay}s")
                        time.sleep(delay)
                        
                if not success:
                    logger.critical(f"❌ Fallo definitivo en chunk tras {MAX_RETRIES} retries. Abortando.")
                    raise RuntimeError("Max retries exceeded on batch.")
                    
                batch_latency = time.time() - batch_t0
                self._check_throughput_collapse(batch_latency, len(batch))
                
                # Adaptive Batch Sizing
                if batch_latency > 2.0 and self.batch_size > 2000:
                    self.batch_size = int(self.batch_size * 0.75)
                elif batch_latency < 0.5 and self.batch_size < 50000:
                    self.batch_size = int(self.batch_size * 1.5)
                    
                i += len(batch)
                self.total_rows_inserted += len(batch)
                
        finally:
            cursor.close()
            conn.close()
            del records
            gc.collect()
            
        return time.time() - chunk_t0

def run_memory_bounded_load(parquet_dir: str, target_db: str):
    logger.info("Iniciando Advanced Memory-Bounded Loader")
    profiler = ProfilerManager("./olap_artifacts/profiling")
    profiler.start()
    
    inserter = MemoryBoundedInserter(CONN_STR)
    total_t0 = time.time()
    
    try:
        tables = [d for d in os.listdir(parquet_dir) if os.path.isdir(os.path.join(parquet_dir, d))]
        
        for table in tables:
            table_dir = os.path.join(parquet_dir, table)
            files = sorted([f for f in os.listdir(table_dir) if f.endswith('.parquet')])
            if not files: continue
            
            logger.info(f"Procesando {table}...")
            
            for f in files:
                file_path = os.path.join(table_dir, f)
                
                # 1. STREAMING ROW GROUPS
                pf = pq.ParquetFile(file_path)
                logger.info(f"  {f}: {pf.num_row_groups} row groups detectados.")
                
                for rg in range(pf.num_row_groups):
                    # Zero-copy load a memoria acotada
                    rg_df = pf.read_row_group(rg).to_pandas()
                    rows_in_rg = len(rg_df)
                    
                    latency = inserter.insert_chunk(target_db, table, rg_df)
                    
                    if latency > 0:
                        rps = rows_in_rg / latency
                        logger.info(f"    [RG {rg}] {rows_in_rg} filas | {latency:.2f}s | {rps:.0f} rps | Batch adaptado a {inserter.batch_size} | Peak RAM {inserter.metrics['peak_memory_mb']:.0f}MB")
                        
    except Exception as e:
        logger.error(f"PIPELINE ABORTADO AUTOMÁTICAMENTE: {e}")
        logger.error(traceback.format_exc())
    finally:
        total_time = time.time() - total_t0
        profiler.stop_and_save("memory_loader")
        
        logger.info("\n--- RESUMEN DE PROFILING Y EJECUCIÓN ---")
        logger.info(f"Filas insertadas: {inserter.total_rows_inserted:,}")
        logger.info(f"Tiempo Total:     {total_time:.1f}s")
        if total_time > 0:
            logger.info(f"Avg Throughput:   {inserter.total_rows_inserted/total_time:.0f} rps")
        logger.info(f"Peak RAM:         {inserter.metrics['peak_memory_mb']:.0f} MB")
        logger.info(f"Total Retries:    {inserter.metrics['total_retries']}")

if __name__ == "__main__":
    run_memory_bounded_load("./output_canonical", "AnalyticsDB")
