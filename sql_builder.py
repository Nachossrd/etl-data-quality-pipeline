#!/usr/bin/env python
"""
SQL RELATIONSHIP BUILDER v1
Analiza la base de datos AnalyticsDB, detecta claves primarias y foráneas,
calcula orphan ratios y genera scripts ALTER TABLE para establecer la integridad
referencial y la arquitectura Star Schema.
"""

import logging
import os
import pyodbc
import pandas as pd
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("SQLBuilder")

CONN_STR = "DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost,1433;DATABASE=AnalyticsDB;UID=sa;PWD=SuperSecurePass123!"

# Jerarquías conocidas a validar
HIERARCHIES = {
    "Product": ["DimProduct", "DimProductSubcategory", "DimProductCategory"],
    "Geography": ["DimCustomer", "DimSalesTerritory", "DimGeography"],
    "Calendar": ["DimDate"]
}

def get_tables(conn) -> list:
    cursor = conn.cursor()
    cursor.execute("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE'")
    tables = [row[0] for row in cursor.fetchall()]
    return tables

def get_columns(conn, table: str) -> list:
    cursor = conn.cursor()
    cursor.execute(f"SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = '{table}'")
    return [row[0] for row in cursor.fetchall()]

def infer_pk(table: str, columns: list) -> str:
    """Infiere la clave primaria de una tabla (ej. DimProduct -> ProductKey)."""
    # 1. Búsqueda exacta
    expected_pk = table.replace("Dim", "").replace("Fact", "") + "Key"
    for col in columns:
        if col.lower() == expected_pk.lower():
            return col
            
    # 2. Fact surrogate
    if table.startswith("Fact") and "FactSurrogatePK" in columns:
        return "FactSurrogatePK"
        
    # 3. Terminación en Key genérica
    keys = [c for c in columns if c.lower().endswith("key")]
    if len(keys) == 1:
        return keys[0]
        
    return None

def analyze_orphan_ratio(conn, fact_table: str, fk_col: str, dim_table: str, pk_col: str):
    """Calcula el porcentaje de registros huérfanos."""
    cursor = conn.cursor()
    
    # Total rows in Fact
    cursor.execute(f"SELECT COUNT_BIG(*) FROM [{fact_table}]")
    total_fact = cursor.fetchone()[0]
    if total_fact == 0:
        return 0, 0.0, 1.0  # orphans, ratio, confidence
        
    # Valid matching rows
    # Usamos COUNT(1) con un LEFT JOIN o NOT IN. NOT IN es lento, LEFT JOIN es mejor
    query = f"""
        SELECT COUNT_BIG(f.[{fk_col}]) 
        FROM [{fact_table}] f
        LEFT JOIN [{dim_table}] d ON f.[{fk_col}] = d.[{pk_col}]
        WHERE d.[{pk_col}] IS NULL AND f.[{fk_col}] IS NOT NULL
    """
    cursor.execute(query)
    orphans = cursor.fetchone()[0]
    
    ratio = orphans / total_fact
    confidence = 1.0 - ratio
    return orphans, ratio, confidence

def generate_relationships():
    logger.info("Conectando a SQL Server para análisis referencial...")
    try:
        conn = pyodbc.connect(CONN_STR, autocommit=True)
    except Exception as e:
        logger.error(f"Error de conexión: {e}")
        return

    tables = get_tables(conn)
    schema = {}
    
    # 1. Catalogar PKs
    for t in tables:
        cols = get_columns(conn, t)
        pk = infer_pk(t, cols)
        schema[t] = {"columns": cols, "pk": pk, "type": "Fact" if t.startswith("Fact") else "Dim"}
        logger.info(f"[{t}] Type: {schema[t]['type']}, PK: {pk}")

    # 2. Buscar FKs e inferir relaciones
    relations = []
    
    for t in tables:
        t_info = schema[t]
        cols = t_info["columns"]
        
        # Todas las columnas que terminen en Key y no sean la PK
        fk_candidates = [c for c in cols if c.lower().endswith("key") and c != t_info["pk"]]
        
        for fk in fk_candidates:
            # Buscar tabla destino (Dim que tenga esta FK como PK)
            target_dim = None
            target_pk = None
            
            # Intento directo
            expected_dim = "Dim" + fk.replace("Key", "").replace("key", "")
            
            if expected_dim in schema and schema[expected_dim]["pk"] and schema[expected_dim]["pk"].lower() == fk.lower():
                target_dim = expected_dim
                target_pk = schema[expected_dim]["pk"]
            else:
                # Búsqueda exhaustiva en otras Dims
                for dim_name, dim_info in schema.items():
                    if dim_info["type"] == "Dim" and dim_info["pk"] and dim_info["pk"].lower() == fk.lower():
                        target_dim = dim_name
                        target_pk = dim_info["pk"]
                        break
            
            if target_dim:
                logger.info(f"Analizando relación: {t}.{fk} -> {target_dim}.{target_pk}")
                orphans, ratio, confidence = analyze_orphan_ratio(conn, t, fk, target_dim, target_pk)
                
                relations.append({
                    "source_table": t,
                    "fk_column": fk,
                    "target_table": target_dim,
                    "pk_column": target_pk,
                    "orphans": orphans,
                    "confidence": confidence
                })
                logger.info(f"  Confidence: {confidence:.2%} (Orphans: {orphans})")

    # 3. Filtrar y Generar DDL
    ddl_statements = []
    logger.info("\n--- RESUMEN DE INTEGRIDAD REFERENCIAL ---")
    for r in relations:
        status = "✅ ACEPTADA" if r["confidence"] >= 0.9 else "❌ RECHAZADA"
        logger.info(f"{status} | {r['source_table']}.{r['fk_column']} -> {r['target_table']}.{r['pk_column']} | Confidence: {r['confidence']:.2%}")
        
        if r["confidence"] >= 0.9:
            fk_name = f"FK_{r['source_table']}_{r['fk_column']}"
            stmt = (
                f"ALTER TABLE [dbo].[{r['source_table']}] WITH NOCHECK\n"
                f"ADD CONSTRAINT [{fk_name}] FOREIGN KEY ([{r['fk_column']}])\n"
                f"REFERENCES [dbo].[{r['target_table']}] ([{r['pk_column']}]);\n"
                f"ALTER TABLE [dbo].[{r['source_table']}] CHECK CONSTRAINT [{fk_name}];"
            )
            ddl_statements.append(stmt)
            
    # Guardar scripts
    out_dir = os.path.join(os.path.dirname(__file__), "sql_scripts")
    os.makedirs(out_dir, exist_ok=True)
    
    with open(os.path.join(out_dir, "01_apply_fks.sql"), "w") as f:
        f.write("-- AUTO-GENERATED REFERENTIAL INTEGRITY SCRIPT\n")
        f.write(f"-- Generated at {datetime.now().isoformat()}\n\n")
        for stmt in ddl_statements:
            f.write(stmt + "\n\n")
            
    logger.info(f"Script DDL generado en: {out_dir}/01_apply_fks.sql")

if __name__ == "__main__":
    generate_relationships()
