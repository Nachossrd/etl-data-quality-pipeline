#!/usr/bin/env python
"""
STRUCTURAL NORMALIZER & FK VALIDATOR
Normaliza esquemas Parquet (Zero-Copy) para resolver inconsistencias case-sensitive.
Implementa un FK Consistency Checker y Orphan Detector pre-ingestión.
"""

import json
import logging
import os
import re
import pyarrow as pa
import pyarrow.parquet as pq

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("StructuralNormalizer")

INPUT_DIR = "./output_sanitized"
CANONICAL_DIR = "./output_canonical"

def _to_pascal_case_key(col_name: str) -> str:
    """Convierte product_key, Datekey, date_key a ProductKey, DateKey."""
    if not col_name.lower().endswith("key"):
        return col_name
        
    # Limpiar snake_case si existe
    clean_name = re.sub(r'[^a-zA-Z0-9]', '', col_name)
    base = clean_name[:-3] # quitar "key"
    
    # Capitalizar primera letra si no lo está (manejo básico PascalCase)
    if base and base[0].islower():
        base = base[0].upper() + base[1:]
        
    return f"{base}Key"

def normalize_schemas():
    """Renombra columnas Parquet usando Zero-Copy schema replacement."""
    logger.info("--- INICIANDO NORMALIZACIÓN ESTRUCTURAL (Zero-Copy) ---")
    os.makedirs(CANONICAL_DIR, exist_ok=True)
    
    tables = [d for d in os.listdir(INPUT_DIR) if os.path.isdir(os.path.join(INPUT_DIR, d))]
    
    schema_changes = {}
    
    for table in tables:
        in_t_dir = os.path.join(INPUT_DIR, table)
        out_t_dir = os.path.join(CANONICAL_DIR, table)
        os.makedirs(out_t_dir, exist_ok=True)
        
        files = [f for f in os.listdir(in_t_dir) if f.endswith(".parquet")]
        if not files: continue
        
        logger.info(f"Normalizando esquema para {table}...")
        
        for f in files:
            in_path = os.path.join(in_t_dir, f)
            out_path = os.path.join(out_t_dir, f)
            
            try:
                # Leer metadata sin cargar toda la data si es posible
                table_pa = pq.read_table(in_path)
                original_cols = table_pa.schema.names
                
                # Crear Naming Canonical
                new_cols = [_to_pascal_case_key(c) for c in original_cols]
                
                # Registrar cambios para logs
                for o, n in zip(original_cols, new_cols):
                    if o != n:
                        logger.info(f"  [Renombrado] {table}.{o} -> {n}")
                        if table not in schema_changes: schema_changes[table] = []
                        schema_changes[table].append((o, n))
                
                # Renombrar zero-copy
                renamed_table = table_pa.rename_columns(new_cols)
                pq.write_table(renamed_table, out_path, compression='snappy')
                
            except Exception as e:
                logger.error(f"Fallo normalizando {in_path}: {e}")

    # Guardar reporte de migraciones estructurales
    with open(os.path.join(CANONICAL_DIR, "schema_migration_report.json"), "w") as f:
        json.dump(schema_changes, f, indent=2)
        
    logger.info("✅ Normalización completa. Data intacta (Reversible).")

def fk_consistency_checker():
    """Valida Integridad Referencial leyendo keys de los Parquets canonizados."""
    logger.info("\n--- EJECUTANDO FK CONSISTENCY & ORPHAN DETECTOR ---")
    
    # 1. Recolectar PKs de las Dimensiones
    dim_pks = {}
    tables = [d for d in os.listdir(CANONICAL_DIR) if os.path.isdir(os.path.join(CANONICAL_DIR, d))]
    
    for table in tables:
        if not table.startswith("Dim"): continue
        
        t_dir = os.path.join(CANONICAL_DIR, table)
        files = [f for f in os.listdir(t_dir) if f.endswith(".parquet")]
        if not files: continue
        
        # Asumimos PK canónica = DimNameKey
        pk_name = f"{table.replace('Dim', '')}Key"
        
        # Chequear si existe la PK en el schema del primer file
        schema = pq.ParquetFile(os.path.join(t_dir, files[0])).schema.names
        if pk_name not in schema:
            # Fallback a la primera que termine en Key
            keys = [c for c in schema if c.endswith("Key")]
            if keys: pk_name = keys[0]
            else: continue
            
        logger.info(f"Recolectando PK {pk_name} de {table}...")
        pk_set = set()
        for f in files:
            pf = pq.ParquetFile(os.path.join(t_dir, f))
            if pk_name in pf.schema.names:
                for i in range(pf.num_row_groups):
                    df = pf.read_row_group(i, columns=[pk_name]).to_pandas()
                    pk_set.update(df[pk_name].dropna().unique())
                    
        dim_pks[pk_name] = {"table": table, "keys": pk_set}
        logger.info(f"  -> {len(pk_set)} keys únicas registradas.")

    # 2. Auditar Fact Tables
    for table in tables:
        if not table.startswith("Fact"): continue
        
        t_dir = os.path.join(CANONICAL_DIR, table)
        files = [f for f in os.listdir(t_dir) if f.endswith(".parquet")]
        if not files: continue
        
        schema = pq.ParquetFile(os.path.join(t_dir, files[0])).schema.names
        fk_columns = [c for c in schema if c.endswith("Key") and "Surrogate" not in c]
        
        logger.info(f"\nAuditando orfandad en {table} (FKs detectadas: {fk_columns})")
        
        # Mapear FK -> Dim
        for fk in fk_columns:
            if fk not in dim_pks:
                logger.warning(f"  ⚠️ FK {fk} no tiene una dimensión detectada en caché. Ignorando.")
                continue
                
            target_dim = dim_pks[fk]["table"]
            valid_keys = dim_pks[fk]["keys"]
            
            total_checked = 0
            orphans_detected = 0
            
            for f in files:
                pf = pq.ParquetFile(os.path.join(t_dir, f))
                if fk in pf.schema.names:
                    for i in range(pf.num_row_groups):
                        df = pf.read_row_group(i, columns=[fk]).to_pandas()
                        fact_keys = df[fk].dropna().unique()
                        
                        # Set difference para hallar huérfanos
                        orphans = set(fact_keys) - valid_keys
                        
                        total_checked += len(df)
                        orphans_detected += len(orphans)
            
            ratio = (orphans_detected / total_checked) if total_checked > 0 else 0
            
            if orphans_detected == 0:
                logger.info(f"  ✅ {fk} -> {target_dim} | 100% Integridad. (0 huérfanos en {total_checked} filas).")
            else:
                logger.critical(f"  ❌ {fk} -> {target_dim} | RUPTURA REFERENCIAL DETECTADA.")
                logger.critical(f"     Huérfanos: {orphans_detected} | Orphan Ratio: {ratio:.4f}")

if __name__ == "__main__":
    normalize_schemas()
    fk_consistency_checker()
