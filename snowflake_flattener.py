#!/usr/bin/env python
"""
SNOWFLAKE FLATTENER
Desnormaliza DimProduct, DimProductSubcategory y DimProductCategory en una
sola DimProductFlat perfecta para Star Schema (optimización VertiPaq).
"""

import json
import logging
import os
import pyarrow as pa
import pyarrow.parquet as pq
import pandas as pd
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("SnowflakeFlattener")

SANITIZED_DIR = "./output_sanitized"
OUT_DIR = os.path.join(SANITIZED_DIR, "DimProductFlat")

def _load_parquet_table(table_name):
    """Carga todos los chunks de una tabla dimensional a un solo DataFrame."""
    dir_path = os.path.join(SANITIZED_DIR, table_name)
    if not os.path.exists(dir_path):
        logger.warning(f"No se encontró {table_name} en {SANITIZED_DIR}.")
        return pd.DataFrame()
        
    files = [os.path.join(dir_path, f) for f in os.listdir(dir_path) if f.endswith(".parquet")]
    if not files: return pd.DataFrame()
    
    logger.info(f"Cargando {len(files)} chunks de {table_name}...")
    df = pd.concat([pq.read_table(f).to_pandas() for f in files], ignore_index=True)
    return df

def flatten_product_hierarchy():
    logger.info("--- INICIANDO FLATTENING DIMENSIONAL (SNOWFLAKE -> STAR) ---")
    
    # 1. Cargar Tablas
    df_prod = _load_parquet_table("DimProduct")
    df_subcat = _load_parquet_table("DimProductSubcategory")
    df_cat = _load_parquet_table("DimProductCategory")
    
    if df_prod.empty:
        logger.error("DimProduct está vacía o no existe. Abortando.")
        return
        
    initial_rows = len(df_prod)
    logger.info(f"DimProduct original: {initial_rows} filas.")
    
    # 2. Auditar PKs y Duplicados antes de Merge
    if 'ProductKey' in df_prod.columns:
        dups = df_prod['ProductKey'].duplicated().sum()
        if dups > 0:
            logger.critical(f"PK ProductKey tiene {dups} duplicados. Corrupción detectada.")
            return
            
    # 3. Flattener (Merge Left seguro)
    # DimProduct -> DimProductSubcategory (via ProductSubcategoryKey)
    if not df_subcat.empty and 'ProductSubcategoryKey' in df_prod.columns:
        logger.info("Uniendo DimProductSubcategory...")
        df_prod = pd.merge(df_prod, df_subcat, on='ProductSubcategoryKey', how='left', suffixes=('', '_subcat'))
        
    # DimProductSubcategory -> DimProductCategory (via ProductCategoryKey)
    if not df_cat.empty and 'ProductCategoryKey' in df_prod.columns:
        logger.info("Uniendo DimProductCategory...")
        df_prod = pd.merge(df_prod, df_cat, on='ProductCategoryKey', how='left', suffixes=('', '_cat'))

    # 4. Validar Cartesian Explosion
    final_rows = len(df_prod)
    if final_rows != initial_rows:
        logger.critical(f"❌ EXPLOSIÓN CARTESIANA DETECTADA: Filas pasaron de {initial_rows} a {final_rows}.")
        logger.critical("El merge generó duplicados por relaciones Many-to-Many anómalas. Abortando.")
        return
    else:
        logger.info(f"✅ Row Count Consistente: {final_rows} filas mantenidas perfectamente.")
        
    # 5. Limpieza de Keys intermedias (Opcional pero recomendado para VertiPaq)
    # Conservamos ProductKey. Podemos dropear las keys internas de jerarquía si lo deseamos.
    cols_to_drop = [c for c in df_prod.columns if c in ('ProductSubcategoryKey', 'ProductCategoryKey') or c.endswith('_subcat') or c.endswith('_cat')]
    df_flat = df_prod.drop(columns=cols_to_drop, errors='ignore')
    
    logger.info(f"Esquema consolidado (DimProductFlat): {len(df_flat.columns)} columnas.")
    
    # 6. Escribir output consolidado
    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, "DimProductFlat_001.parquet")
    table = pa.Table.from_pandas(df_flat)
    pq.write_table(table, out_path, compression='snappy')
    
    logger.info(f"✅ DimProductFlat generada exitosamente en: {out_path}")
    
    # 7. Generar SQL Migration Plan
    _generate_migration_sql(df_flat.columns)

def _generate_migration_sql(columns):
    out_dir = os.path.join(os.path.dirname(__file__), "sql_scripts")
    os.makedirs(out_dir, exist_ok=True)
    sql_path = os.path.join(out_dir, "03_migrate_product_flat.sql")
    
    script = """-- MIGRACIÓN STAR SCHEMA: DimProductFlat
-- Generado: {date}
-- Target: AnalyticsDB

USE [AnalyticsDB];
GO

-- 1. Crear DimProductFlat
IF OBJECT_ID('dbo.DimProductFlat', 'U') IS NOT NULL
    DROP TABLE dbo.DimProductFlat;
GO

PRINT 'Creando tabla DimProductFlat (Desnormalizada)...';
CREATE TABLE dbo.DimProductFlat (
    ProductKey BIGINT PRIMARY KEY CLUSTERED,
    -- (El loader dinámico mapeará el resto de columnas desde el parquet automagicamente)
);
GO

-- 2. Migrar las Fact Tables para apuntar a DimProductFlat en lugar de DimProduct
-- Se asume que las FK constraints previas son reemplazadas
PRINT 'Nota: Ejecutar SQL Relationship Builder (sql_builder.py) nuevamente para auto-asignar las FKs.';
GO
"""
    with open(sql_path, "w") as f:
        f.write(script.replace("{date}", datetime.now().isoformat()))
        
    logger.info(f"✅ Script de migración SQL generado: {sql_path}")

if __name__ == "__main__":
    flatten_product_hierarchy()
