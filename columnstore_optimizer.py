#!/usr/bin/env python
"""
COLUMNSTORE OPTIMIZER FOR FACT TABLES
Migra dinámicamente las Fact Tables hacia Clustered Columnstore Indexes (CCI).
Preparación extrema para VertiPaq SSAS. Reduce storage 70-90%.
"""

import logging
import os
import pyodbc
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("ColumnstoreOptimizer")

CONN_STR = "DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost,1433;DATABASE=AnalyticsDB;UID=sa;PWD=SuperSecurePass123!"

FACT_TABLES = [
    "FactSales",
    "FactOnlineSales",
    "FactInventory",
    "FactSalesQuota",
    "FactStrategyPlan"
]

def generate_columnstore_migration_script():
    """Genera script SQL seguro para migrar Fact tables a Columnstore."""
    out_dir = os.path.join(os.path.dirname(__file__), "sql_scripts")
    os.makedirs(out_dir, exist_ok=True)
    sql_path = os.path.join(out_dir, "04_apply_columnstore.sql")
    
    script_lines = [
        "-- OPTIMIZACIÓN OLAP: CLUSTERED COLUMNSTORE INDEX (CCI)",
        f"-- Generado: {datetime.now().isoformat()}",
        "-- Target: AnalyticsDB",
        "-- Objetivo: Reducir storage 80% y optimizar full-scans para VertiPaq.",
        "",
        "USE [AnalyticsDB];",
        "GO",
        ""
    ]
    
    for table in FACT_TABLES:
        script_lines.extend([
            f"-- ==========================================",
            f"-- TABLA: {table}",
            f"-- ==========================================",
            f"IF OBJECT_ID('dbo.{table}', 'U') IS NOT NULL",
            f"BEGIN",
            f"    PRINT 'Iniciando migración Columnstore para {table}...';",
            "",
            f"    -- 1. Identificar si existe el Clustered Index/PK actual",
            f"    DECLARE @PKName NVARCHAR(255);",
            f"    SELECT @PKName = name FROM sys.key_constraints ",
            f"    WHERE type = 'PK' AND parent_object_id = OBJECT_ID('dbo.{table}');",
            "",
            f"    -- 2. Eliminar PK Clustered (si existe) para liberar el root",
            f"    IF @PKName IS NOT NULL",
            f"    BEGIN",
            f"        PRINT 'Dropping existing Clustered PK: ' + @PKName;",
            f"        EXEC('ALTER TABLE [dbo].[{table}] DROP CONSTRAINT [' + @PKName + '];');",
            f"        ",
            f"        -- 3. Recrear la PK como NONCLUSTERED para mantener integridad y no competir con CCI",
            f"        -- Asumimos que la columna suele ser FactSurrogatePK o similar.",
            f"        -- NOTA: Si es compuesto, esto requerirá ajuste manual, pero el estándar inyectó FactSurrogatePK.",
            f"        IF EXISTS(SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('dbo.{table}') AND name = 'FactSurrogatePK')",
            f"        BEGIN",
            f"            EXEC('ALTER TABLE [dbo].[{table}] ADD CONSTRAINT [' + @PKName + '] PRIMARY KEY NONCLUSTERED ([FactSurrogatePK]);');",
            f"            PRINT 'Recreada PK como NONCLUSTERED.';",
            f"        END",
            f"    END",
            "",
            f"    -- 4. Validar que no haya tipos incompatibles (varchar(max) no fue problema en SQL 2022+ para CCI,",
            f"    -- pero es buena práctica no tener max si no es necesario. PyArrow mapeó NVARCHAR(MAX) en el engine anterior).",
            f"    -- NOTA: SQL Server 2017+ soporta LOBs en Columnstore.",
            "",
            f"    -- 5. Crear el CLUSTERED COLUMNSTORE INDEX",
            f"    IF NOT EXISTS (SELECT * FROM sys.indexes WHERE object_id = OBJECT_ID('dbo.{table}') AND type = 5)",
            f"    BEGIN",
            f"        PRINT 'Creando Clustered Columnstore Index...';",
            f"        CREATE CLUSTERED COLUMNSTORE INDEX [CCI_{table}] ON [dbo].[{table}];",
            f"        PRINT '✅ CCI creado exitosamente en {table}.';",
            f"    END",
            f"    ELSE",
            f"    BEGIN",
            f"        PRINT 'CCI ya existe en {table}.';",
            f"    END",
            f"END",
            f"ELSE",
            f"    PRINT 'Advertencia: Tabla {table} no existe en AnalyticsDB.';",
            "GO",
            ""
        ])
        
    # Query de validación y medición
    script_lines.extend([
        "-- ==========================================",
        "-- AUDITORÍA DE STORAGE Y COMPRESIÓN",
        "-- ==========================================",
        "PRINT 'Ejecutando auditoría de espacio post-migración...';",
        "SELECT ",
        "    t.NAME AS TableName,",
        "    p.rows AS RowCounts,",
        "    SUM(a.total_pages) * 8 / 1024 AS TotalSpaceMB,",
        "    SUM(a.used_pages) * 8 / 1024 AS UsedSpaceMB,",
        "    i.type_desc AS IndexType",
        "FROM sys.tables t",
        "INNER JOIN sys.indexes i ON t.OBJECT_ID = i.object_id",
        "INNER JOIN sys.partitions p ON i.object_id = p.OBJECT_ID AND i.index_id = p.index_id",
        "INNER JOIN sys.allocation_units a ON p.partition_id = a.container_id",
        "WHERE t.NAME LIKE 'Fact%' AND i.type IN (1, 5) -- Clustered Rowstore or Columnstore",
        "GROUP BY t.NAME, p.Rows, i.type_desc",
        "ORDER BY TotalSpaceMB DESC;",
        "GO"
    ])

    with open(sql_path, "w", encoding='utf-8') as f:
        f.write("\n".join(script_lines))
        
    logger.info(f"Script de migración Columnstore generado en: {sql_path}")

def check_compatibility_and_run():
    """Ejecuta la migración directamente si el servidor está online."""
    logger.info("--- AUDITORÍA E INYECCIÓN COLUMNSTORE (CCI) ---")
    
    try:
        conn = pyodbc.connect(CONN_STR, autocommit=True)
    except Exception as e:
        logger.warning(f"SQL Server offline o inaccesible: {e}")
        logger.info("Solo se generó el script SQL. Por favor, ejécutalo manualmente.")
        generate_columnstore_migration_script()
        return

    # 1. Medir espacio actual
    cursor = conn.cursor()
    logger.info("Midiendo footprint de almacenamiento original (Rowstore)...")
    try:
        cursor.execute("""
            SELECT t.NAME, SUM(a.total_pages) * 8 / 1024 AS MB
            FROM sys.tables t
            INNER JOIN sys.partitions p ON t.OBJECT_ID = p.OBJECT_ID
            INNER JOIN sys.allocation_units a ON p.partition_id = a.container_id
            WHERE t.NAME LIKE 'Fact%'
            GROUP BY t.NAME
        """)
        sizes_before = {row[0]: row[1] for row in cursor.fetchall()}
        for t, s in sizes_before.items():
            logger.info(f"  {t}: {s} MB (Antes de CCI)")
    except Exception:
        pass

    # Aplicar DDL script externamente o esperar a que el usuario lo corra
    generate_columnstore_migration_script()
    logger.info("Ejecución directa en DB omitida para seguridad. Ejecuta el script 04_apply_columnstore.sql en tu IDE para controlar el lock time.")
    conn.close()

if __name__ == "__main__":
    check_compatibility_and_run()
