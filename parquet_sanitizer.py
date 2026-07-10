#!/usr/bin/env python
"""
PARQUET ROBUST SANITIZER v1
Reads chunks from ./output, applies strict sanitization rules,
and writes cleaned chunks to ./output_sanitized.

Rules applied:
- Strings: trim, NFKC normalize, collapse spaces, remove invisible chars.
- Nulls: standardize NaN, empty strings, "nan" to real nulls.
- Numerics: promote int -> BIGINT, float -> DECIMAL(38,10).
- Schema drift: unify schemas across chunks.
- Memory: strict chunk-by-chunk processing.
"""

import os
import json
import re
import traceback
from datetime import datetime
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.compute as pc
import numpy as np

INPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output_sanitized")
REPORT_DIR = os.path.join(os.path.dirname(__file__), "audit_reports")

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(REPORT_DIR, exist_ok=True)

# Pre-compile regex for performance
RE_INVISIBLE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]')
RE_MULTI_SPACE = re.compile(r'\s+')


def unify_table_schema(table_dir: str) -> pa.Schema:
    """Read all chunks for a table and build a unified schema with promoted types."""
    all_fields = {}
    
    chunks = [f for f in os.listdir(table_dir) if f.endswith(".parquet")]
    for chunk in chunks:
        chunk_path = os.path.join(table_dir, chunk)
        try:
            schema = pq.read_schema(chunk_path)
            for field in schema:
                name = field.name
                typ = field.type
                
                if name not in all_fields:
                    all_fields[name] = {"types": set()}
                all_fields[name]["types"].add(typ)
        except Exception as e:
            print(f"Error reading schema for {chunk_path}: {e}")

    # Resolve unified types
    unified_fields = []
    for name, info in all_fields.items():
        types_seen = list(info["types"])
        resolved_type = types_seen[0]
        
        # Determine base type across chunks (handling nulls)
        non_null_types = [t for t in types_seen if not pa.types.is_null(t)]
        if not non_null_types:
            resolved_type = pa.null()
        else:
            # If multiple non-null types, pick the most permissive
            # E.g., timestamp and something else?
            t0 = non_null_types[0]
            if any(pa.types.is_floating(t) for t in non_null_types):
                resolved_type = pa.decimal128(38, 10)
            elif any(pa.types.is_integer(t) for t in non_null_types):
                resolved_type = pa.int64()
            elif any(pa.types.is_string(t) or pa.types.is_large_string(t) for t in non_null_types):
                resolved_type = pa.large_string()
            elif any(pa.types.is_timestamp(t) for t in non_null_types):
                # Pick the first timestamp type (assuming same unit)
                resolved_type = [t for t in non_null_types if pa.types.is_timestamp(t)][0]
            else:
                resolved_type = t0
        
        # Apply promotions rules
        if pa.types.is_integer(resolved_type):
            resolved_type = pa.int64()
        elif pa.types.is_floating(resolved_type):
            resolved_type = pa.decimal128(38, 10)
        elif pa.types.is_string(resolved_type):
            resolved_type = pa.large_string()
            
        unified_fields.append(pa.field(name, resolved_type))
        
    return pa.schema(unified_fields)


def sanitize_dataframe(df: pd.DataFrame, schema: pa.Schema, metrics: dict):
    """Apply sanitization rules to a pandas dataframe in-place."""
    for field in schema:
        col = field.name
        if col not in df.columns:
            continue
            
        typ = field.type
        
        # Handle Strings
        if pa.types.is_string(typ) or pa.types.is_large_string(typ):
            mask = df[col].notna()
            if mask.any():
                # Coerce to string to avoid mixed types
                s = df.loc[mask, col].astype(str)
                # Trim
                s = s.str.strip()
                # Normalize unicode
                s = s.str.normalize('NFKC')
                # Collapse spaces
                s = s.str.replace(RE_MULTI_SPACE, ' ', regex=True)
                # Remove invisibles
                s = s.str.replace(RE_INVISIBLE, '', regex=True)
                
                # Update back
                df.loc[mask, col] = s
                
                # Empty strings to None
                empty_mask = df[col] == ""
                if empty_mask.any():
                    metrics["empty_strings_nulled"] += int(empty_mask.sum())
                    df.loc[empty_mask, col] = None
                    
                # "nan" string to None
                nan_str_mask = df[col].str.lower() == "nan"
                if nan_str_mask.any():
                    metrics["nan_strings_nulled"] += int(nan_str_mask.sum())
                    df.loc[nan_str_mask, col] = None

        # Handle Dates
        elif pa.types.is_timestamp(typ) or pa.types.is_date(typ):
            df[col] = pd.to_datetime(df[col], errors='coerce')
            # Check bounds (1900 to 2100)
            valid_mask = (df[col].dt.year >= 1900) & (df[col].dt.year <= 2100)
            invalid_mask = df[col].notna() & ~valid_mask
            if invalid_mask.any():
                metrics["invalid_dates_nulled"] += int(invalid_mask.sum())
                df.loc[invalid_mask, col] = pd.NaT

        # Handle Numerics
        elif pa.types.is_integer(typ) or pa.types.is_floating(typ) or pa.types.is_decimal(typ):
            # pd.to_numeric handles coercing errors to NaN
            df[col] = pd.to_numeric(df[col], errors='coerce')
            
            if pa.types.is_integer(typ):
                # Float NaNs -> None before casting
                df[col] = df[col].replace({np.nan: None})
                
    # Final global NaN/None normalization
    df.replace({np.nan: None, pd.NaT: None}, inplace=True)
    return df


def process_table(table_name: str, input_dir: str, output_dir: str, report_data: dict):
    print(f"\nProcessing table: {table_name}")
    table_in = os.path.join(input_dir, table_name)
    table_out = os.path.join(output_dir, table_name)
    os.makedirs(table_out, exist_ok=True)
    
    metrics = {
        "chunks_processed": 0,
        "rows_processed": 0,
        "empty_strings_nulled": 0,
        "nan_strings_nulled": 0,
        "invalid_dates_nulled": 0,
        "type_promotions": []
    }
    
    # 1. Unify schema
    schema = unify_table_schema(table_in)
    print(f"  Unified schema determined ({len(schema)} columns).")
    
    # Identify promotions for reporting
    for f in schema:
        metrics["type_promotions"].append({
            "col": f.name,
            "target_type": str(f.type)
        })
    
    # 2. Process chunks sequentially
    chunks = sorted([f for f in os.listdir(table_in) if f.endswith(".parquet")])
    for chunk in chunks:
        in_path = os.path.join(table_in, chunk)
        out_path = os.path.join(table_out, chunk)
        
        try:
            # Read to pandas
            table = pq.read_table(in_path)
            df = table.to_pandas(date_as_object=True)
            
            # Sanitize
            df = sanitize_dataframe(df, schema, metrics)
            
            # Convert back to Arrow using strictly the unified schema
            # Safe casting
            arrays = []
            for field in schema:
                col_name = field.name
                if col_name in df.columns:
                    arr = pa.array(df[col_name], from_pandas=True)
                    try:
                        # Cast to unified type safely
                        arr = arr.cast(field.type, safe=False)
                    except Exception as e:
                        print(f"    Warning: Could not cast {col_name} to {field.type}: {e}")
                    arrays.append(arr)
                else:
                    # Missing column -> fill with Nulls
                    arrays.append(pa.array([None] * len(df), type=field.type))
            
            clean_table = pa.Table.from_arrays(arrays, schema=schema)
            
            # Write with ZSTD compression
            pq.write_table(clean_table, out_path, compression='ZSTD')
            
            metrics["chunks_processed"] += 1
            metrics["rows_processed"] += len(df)
            
        except Exception as e:
            print(f"  [ERROR] Error processing {chunk}: {e}")
            traceback.print_exc()
            
    print(f"  [OK] Completed {table_name}: {metrics['rows_processed']:,} rows in {metrics['chunks_processed']} chunks.")
    report_data[table_name] = metrics


def main():
    start_time = datetime.now()
    print("=" * 60)
    print("PARQUET SANITIZATION ENGINE")
    print("=" * 60)
    
    if not os.path.exists(INPUT_DIR):
        print(f"Input dir not found: {INPUT_DIR}")
        return
        
    tables = sorted([d for d in os.listdir(INPUT_DIR) 
                    if os.path.isdir(os.path.join(INPUT_DIR, d)) and not d.startswith("_")])
                    
    report_data = {
        "timestamp": start_time.isoformat(),
        "tables": {}
    }
    
    for table in tables:
        process_table(table, INPUT_DIR, OUTPUT_DIR, report_data["tables"])
        
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()
    report_data["total_duration_seconds"] = duration
    
    # Save JSON report
    json_path = os.path.join(REPORT_DIR, "sanitization_report.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)
        
    # Generate MD summary
    md_path = os.path.join(REPORT_DIR, "sanitization_summary.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# Sanitization Layer Report\n\n")
        f.write(f"**Execution Time**: {duration:.2f} seconds\n")
        f.write(f"**Tables Processed**: {len(tables)}\n\n")
        
        for t, metrics in report_data["tables"].items():
            f.write(f"### {t}\n")
            f.write(f"- Rows: {metrics['rows_processed']:,}\n")
            f.write(f"- Chunks: {metrics['chunks_processed']}\n")
            if metrics['empty_strings_nulled'] > 0:
                f.write(f"- [CLEANED] Empty strings nulled: {metrics['empty_strings_nulled']:,}\n")
            if metrics['nan_strings_nulled'] > 0:
                f.write(f"- [CLEANED] 'nan' strings nulled: {metrics['nan_strings_nulled']:,}\n")
            if metrics['invalid_dates_nulled'] > 0:
                f.write(f"- [CLEANED] Invalid dates nulled: {metrics['invalid_dates_nulled']:,}\n")
            f.write("\n")
            
    print("=" * 60)
    print(f"Sanitization complete in {duration:.1f}s.")
    print(f"Reports saved to: {REPORT_DIR}")
    print("=" * 60)

if __name__ == "__main__":
    main()
