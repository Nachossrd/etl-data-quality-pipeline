#!/usr/bin/env python
"""
MISSING DIMENSIONS REMEDIATOR
Recuperación forense e incremental de tablas huérfanas (DimGeography, DimStore).
"""

import json
import logging
import os
import time
import pyodbc
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("DimRemediator")

# Configuración
RAW_DB_CONN = "DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost,1433;DATABASE=ContosoRetailDW;UID=sa;PWD=SuperSecurePass123!" # Ajustar a DB origen
RAW_DB_ALT = "DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost,1433;DATABASE=master;UID=sa;PWD=SuperSecurePass123!" # Master fallback

OUTPUT_DIR = "./output"
SANITIZED_DIR = "./output_sanitized"
TARGET_TABLES = ["DimGeography", "DimStore"]

def audit_original_extraction():
    """1. Auditar extracción original."""
    logger.info("--- AUDITANDO EXTRACCIÓN ORIGINAL ---")
    manifest_path = os.path.join(OUTPUT_DIR, "_metadata", "extraction_manifest.json")
    
    if os.path.exists(manifest_path):
        with open(manifest_path, 'r') as f:
            manifest = json.load(f)
            
        tables_info = manifest.get("tables", [])
        if isinstance(tables_info, dict):
            tables_info = tables_info.values()
            
        for t in TARGET_TABLES:
            info = next((i for i in tables_info if i.get('table') == t or i.get('name') == t), {})
            logger.warning(f"Auditoría {t}: Extraído = {info.get('rows_extracted', 0)} filas. Status = {info.get('status', 'Unknown')}")
            if "error" in info:
                logger.error(f"  Error original reportado: {info['error']}")
    else:
        logger.warning("Manifest no encontrado.")

    # Verificar Parquet files crudos
    for t in TARGET_TABLES:
        t_dir = os.path.join(OUTPUT_DIR, t)
        if os.path.exists(t_dir):
            files = [f for f in os.listdir(t_dir) if f.endswith(".parquet")]
            logger.info(f"Directorio crudo {t}: {len(files)} archivos Parquet.")
            for f in files:
                p_path = os.path.join(t_dir, f)
                try:
                    pf = pq.ParquetFile(p_path)
                    logger.info(f"  Archivo {f} tiene {pf.metadata.num_rows} filas.")
                except Exception as e:
                    logger.error(f"  Archivo corrupto {f}: {e}")
        else:
            logger.error(f"Directorio crudo {t} no existe.")

def try_extract_from_source():
    """2. Re-extraer SOLO tablas faltantes desde backup original."""
    logger.info("\n--- INTENTANDO EXTRACCIÓN DESDE SQL SERVER ORIGEN ---")
    
    extracted_tables = []
    
    try:
        # Intento de conectar al source
        conn = pyodbc.connect(RAW_DB_CONN, timeout=10)
    except Exception as e:
        logger.error(f"No se pudo conectar a la DB Origen con {RAW_DB_CONN}.")
        logger.error(f"Error: {e}")
        logger.info("Probando si la base cruda existe en la instancia...")
        try:
            conn = pyodbc.connect(RAW_DB_ALT, timeout=5)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sys.databases")
            dbs = [row[0] for row in cursor.fetchall()]
            logger.info(f"Bases de datos disponibles en origen: {dbs}")
        except:
            pass
        return extracted_tables

    for t in TARGET_TABLES:
        try:
            logger.info(f"Extrayendo {t} desde origen...")
            query = f"SELECT * FROM [dbo].[{t}]"
            df = pd.read_sql(query, conn)
            
            if len(df) > 0:
                logger.info(f"✅ {t} extraída exitosamente: {len(df)} filas.")
                
                # Sanitización on-the-fly
                df = df.where(pd.notnull(df), None) # Nulls
                # Normalización string case-sensitivity y trim
                for col in df.select_dtypes(include=['object']):
                    df[col] = df[col].astype(str).str.strip().replace({'nan': None, '': None})
                    if col.lower().endswith("key") and df[col].dtype == object:
                        try:
                            df[col] = pd.to_numeric(df[col], errors='coerce').astype('Int64')
                        except: pass
                
                out_dir = os.path.join(SANITIZED_DIR, t)
                os.makedirs(out_dir, exist_ok=True)
                out_path = os.path.join(out_dir, f"{t}_recovered.parquet")
                
                table = pa.Table.from_pandas(df)
                pq.write_table(table, out_path, compression='snappy')
                extracted_tables.append(t)
                logger.info(f"  Guardado limpio en: {out_path}")
                
                # Validate
                _validate_extracted(df, t)
                
            else:
                logger.error(f"La tabla {t} existe en el origen pero está vacía (0 rows).")
        except Exception as e:
            logger.error(f"Fallo al extraer {t} del origen: {e}")

    conn.close()
    return extracted_tables

def _validate_extracted(df: pd.DataFrame, table_name: str):
    """Validaciones estrictas post-extracción."""
    logger.info(f"  Validando {table_name}...")
    
    # 1. PK Uniqueness
    pk_col = f"{table_name.replace('Dim', '')}Key"
    if pk_col in df.columns:
        dups = df[pk_col].duplicated().sum()
        if dups == 0:
            logger.info(f"    ✅ PK '{pk_col}' validada: 100% Unique.")
        else:
            logger.critical(f"    ❌ PK '{pk_col}' falló: {dups} duplicados.")
    else:
        logger.warning(f"    ⚠️ No se encontró la PK esperada '{pk_col}'. Columnas: {list(df.columns)}")
        
    # 2. Null Ratios
    nulls = df.isnull().sum() / len(df)
    high_nulls = nulls[nulls > 0.5]
    if not high_nulls.empty:
        logger.warning(f"    ⚠️ Columnas con >50% nulos: {high_nulls.to_dict()}")

def recover_from_facts():
    """3. Fallback: Inferencia matemática desde FactOnlineSales si no hay Origen."""
    logger.warning("\n--- EJECUTANDO FALLBACK: INFERENCIA DESDE FACT TABLES ---")
    logger.info("Si no pudimos extraer las dimensiones del origen, crearemos 'Stubs' para mantener Integridad Referencial.")
    
    fact_dir = os.path.join(SANITIZED_DIR, "FactOnlineSales")
    if not os.path.exists(fact_dir):
        logger.error("FactOnlineSales no existe en output_sanitized. Fallback cancelado.")
        return

    # Escanear FactOnlineSales para recolectar Keys únicas
    files = [os.path.join(fact_dir, f) for f in os.listdir(fact_dir) if f.endswith(".parquet")]
    
    geo_keys = set()
    store_keys = set()
    
    logger.info(f"Escaneando {len(files)} archivos FactOnlineSales...")
    for f in files:
        pf = pq.ParquetFile(f)
        for i in range(pf.num_row_groups):
            df = pf.read_row_group(i, columns=['StoreKey'] if 'StoreKey' in pf.schema.names else []).to_pandas()
            if 'StoreKey' in df.columns: store_keys.update(df['StoreKey'].dropna().unique())
            # GeographyKey raramente está directo en SalesFact (usualmente pasa por Store o Customer)
            # Pero si está, lo atrapamos
            
    # Intentar sacar GeographyKey desde DimCustomer
    cust_dir = os.path.join(SANITIZED_DIR, "DimCustomer")
    if os.path.exists(cust_dir):
        logger.info("Escaneando DimCustomer para inferir GeographyKeys...")
        cust_files = [os.path.join(cust_dir, f) for f in os.listdir(cust_dir) if f.endswith(".parquet")]
        for f in cust_files:
            pf = pq.ParquetFile(f)
            if 'GeographyKey' in pf.schema.names:
                for i in range(pf.num_row_groups):
                    df = pf.read_row_group(i, columns=['GeographyKey']).to_pandas()
                    geo_keys.update(df['GeographyKey'].dropna().unique())

    # Generar Stub DimStore
    if store_keys and "DimStore" in TARGET_TABLES:
        logger.info(f"Generando Stub DimStore con {len(store_keys)} keys inferidas.")
        df_store = pd.DataFrame({
            "StoreKey": list(store_keys),
            "StoreName": ["Unknown Store"] * len(store_keys),
            "IsRecoveredStub": [True] * len(store_keys)
        })
        out_dir = os.path.join(SANITIZED_DIR, "DimStore")
        os.makedirs(out_dir, exist_ok=True)
        pq.write_table(pa.Table.from_pandas(df_store), os.path.join(out_dir, "DimStore_stub.parquet"))
        
    # Generar Stub DimGeography
    if geo_keys and "DimGeography" in TARGET_TABLES:
        logger.info(f"Generando Stub DimGeography con {len(geo_keys)} keys inferidas.")
        df_geo = pd.DataFrame({
            "GeographyKey": list(geo_keys),
            "ContinentName": ["Unknown"] * len(geo_keys),
            "IsRecoveredStub": [True] * len(geo_keys)
        })
        out_dir = os.path.join(SANITIZED_DIR, "DimGeography")
        os.makedirs(out_dir, exist_ok=True)
        pq.write_table(pa.Table.from_pandas(df_geo), os.path.join(out_dir, "DimGeography_stub.parquet"))

def generate_migration_script():
    """Genera SQL script seguro e incremental para cargar las tablas remediadas."""
    out_dir = os.path.join(os.path.dirname(__file__), "sql_scripts")
    os.makedirs(out_dir, exist_ok=True)
    sql_path = os.path.join(out_dir, "02_remediate_dims_incremental.sql")
    
    script = """-- MIGRACIÓN INCREMENTAL: Dimensiones Faltantes
-- Generado: {date}
-- Target: AnalyticsDB

USE [AnalyticsDB];
GO

-- 1. DimGeography
IF OBJECT_ID('dbo.DimGeography', 'U') IS NULL
BEGIN
    PRINT 'Creando tabla DimGeography...';
    -- DDL básico. El engine ParquetLoader actualizará el esquema con el parquet real.
    CREATE TABLE dbo.DimGeography (
        GeographyKey BIGINT PRIMARY KEY CLUSTERED,
        City NVARCHAR(MAX) NULL,
        StateProvinceName NVARCHAR(MAX) NULL,
        ContinentName NVARCHAR(MAX) NULL
    );
END
ELSE
    PRINT 'DimGeography ya existe. Procediendo a carga incremental.';
GO

-- 2. DimStore
IF OBJECT_ID('dbo.DimStore', 'U') IS NULL
BEGIN
    PRINT 'Creando tabla DimStore...';
    CREATE TABLE dbo.DimStore (
        StoreKey BIGINT PRIMARY KEY CLUSTERED,
        StoreName NVARCHAR(MAX) NULL,
        Status NVARCHAR(50) NULL
    );
END
ELSE
    PRINT 'DimStore ya existe. Procediendo a carga incremental.';
GO

-- REINICIAR CHECKPOINT LOADER PARA ESTAS TABLAS:
-- Elimine o actualice loader_v2_checkpoint.json para forzar carga de DimStore y DimGeography
"""
    with open(sql_path, "w") as f:
        f.write(script.replace("{date}", datetime.now().isoformat()))
    
    logger.info(f"\n✅ Script de migración generado en: {sql_path}")

if __name__ == "__main__":
    audit_original_extraction()
    extracted = try_extract_from_source()
    if len(extracted) < len(TARGET_TABLES):
        # Si falló la extracción de alguna, intentamos fallback
        recover_from_facts()
        
    generate_migration_script()
    logger.info("Remediación completada. Listo para re-cargar incrementalmente.")
