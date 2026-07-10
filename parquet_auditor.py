#!/usr/bin/env python
"""
PARQUET STRUCTURAL AUDITOR v1
Streams through all Parquet chunks in ./output to detect:
- Schema drift between chunks
- NULL-only columns
- Type issues (overflow, mixed types, invalid dates)
- PK/FK candidates
- Relationship matrix
- Snowflake patterns
Outputs: markdown report + JSON technical report
"""

import json
import os
import sys
import hashlib
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pyarrow as pa
import pyarrow.parquet as pq
import numpy as np

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
REPORT_DIR = os.path.join(os.path.dirname(__file__), "audit_reports")
os.makedirs(REPORT_DIR, exist_ok=True)

# ═══════════════════════════════════════════════════════════════
# TABLE AUDITOR
# ═══════════════════════════════════════════════════════════════

class TableAudit:
    """Audit result for a single table."""
    def __init__(self, name: str, directory: str):
        self.name = name
        self.directory = directory
        self.chunks: List[str] = []
        self.total_rows = 0
        self.total_size_bytes = 0
        self.schema: Optional[pa.Schema] = None
        self.schema_str = ""
        self.columns: Dict[str, Dict[str, Any]] = {}
        self.schema_drift: List[str] = []
        self.pk_candidates: List[str] = []
        self.fk_candidates: List[str] = []
        self.issues: List[Dict[str, Any]] = []
        self.sample_values: Dict[str, list] = {}

    def to_dict(self):
        return {
            "name": self.name,
            "total_rows": self.total_rows,
            "total_size_bytes": self.total_size_bytes,
            "total_size_mb": round(self.total_size_bytes / (1024*1024), 2),
            "num_chunks": len(self.chunks),
            "num_columns": len(self.columns),
            "schema": {f.name: str(f.type) for f in self.schema} if self.schema else {},
            "columns": self.columns,
            "schema_drift": self.schema_drift,
            "pk_candidates": self.pk_candidates,
            "fk_candidates": self.fk_candidates,
            "issues": self.issues,
        }


def audit_table(name: str, directory: str) -> TableAudit:
    """Audit a single table by streaming through its Parquet chunks."""
    audit = TableAudit(name, directory)
    
    # Discover chunks
    chunks = sorted([f for f in os.listdir(directory) if f.endswith(".parquet")])
    audit.chunks = chunks
    
    if not chunks:
        audit.issues.append({"severity": "critical", "msg": "No parquet files found"})
        return audit
    
    # Track per-column stats
    col_stats = defaultdict(lambda: {
        "null_count": 0, "total_count": 0, "min_val": None, "max_val": None,
        "unique_approx": set(), "max_str_len": 0, "types_seen": set(),
        "has_negative": False, "has_overflow_int32": False, "has_inf": False,
        "has_invalid_date": False, "arrow_types_seen": [],
    })
    
    reference_schema = None
    total_rows = 0
    total_size = 0
    
    for ci, chunk_file in enumerate(chunks):
        chunk_path = os.path.join(directory, chunk_file)
        file_size = os.path.getsize(chunk_path)
        total_size += file_size
        
        try:
            pf = pq.ParquetFile(chunk_path)
            chunk_schema = pf.schema_arrow
            chunk_rows = pf.metadata.num_rows
            total_rows += chunk_rows
            
            # Schema drift detection
            if reference_schema is None:
                reference_schema = chunk_schema
            else:
                if chunk_schema != reference_schema:
                    ref_names = set(f.name for f in reference_schema)
                    cur_names = set(f.name for f in chunk_schema)
                    added = cur_names - ref_names
                    removed = ref_names - cur_names
                    if added:
                        audit.schema_drift.append(f"Chunk {ci}: columns added: {added}")
                    if removed:
                        audit.schema_drift.append(f"Chunk {ci}: columns removed: {removed}")
                    for f in chunk_schema:
                        ref_field = reference_schema.field(f.name) if f.name in [rf.name for rf in reference_schema] else None
                        if ref_field and ref_field.type != f.type:
                            audit.schema_drift.append(
                                f"Chunk {ci}: '{f.name}' type changed: {ref_field.type} → {f.type}"
                            )
            
            # Record arrow types per column
            for f in chunk_schema:
                col_stats[f.name]["arrow_types_seen"].append(str(f.type))
            
            # Read chunk in row groups to limit memory
            for rg_idx in range(pf.metadata.num_row_groups):
                rg = pf.read_row_group(rg_idx)
                
                for col_name in rg.column_names:
                    col = rg.column(col_name)
                    stats = col_stats[col_name]
                    stats["total_count"] += len(col)
                    stats["null_count"] += col.null_count
                    stats["types_seen"].add(str(col.type))
                    
                    # Sample values (first chunk only, max 5)
                    if ci == 0 and col_name not in audit.sample_values:
                        try:
                            vals = col.to_pylist()[:5]
                            audit.sample_values[col_name] = [str(v)[:100] if v is not None else None for v in vals]
                        except Exception:
                            audit.sample_values[col_name] = ["<error>"]
                    
                    # Uniqueness tracking (approximate - cap at 50K)
                    if len(stats["unique_approx"]) < 50000:
                        try:
                            for v in col.to_pylist()[:1000]:
                                if v is not None:
                                    stats["unique_approx"].add(v)
                        except Exception:
                            pass
                    
                    # Type-specific checks
                    try:
                        if pa.types.is_integer(col.type):
                            arr = col.drop_null()
                            if len(arr) > 0:
                                np_arr = arr.to_numpy()
                                local_min = int(np_arr.min())
                                local_max = int(np_arr.max())
                                if stats["min_val"] is None or local_min < stats["min_val"]:
                                    stats["min_val"] = local_min
                                if stats["max_val"] is None or local_max > stats["max_val"]:
                                    stats["max_val"] = local_max
                                if local_min < 0:
                                    stats["has_negative"] = True
                                if local_max > 2147483647 or local_min < -2147483648:
                                    stats["has_overflow_int32"] = True
                        
                        elif pa.types.is_floating(col.type):
                            arr = col.drop_null()
                            if len(arr) > 0:
                                np_arr = arr.to_numpy()
                                if np.any(np.isinf(np_arr)):
                                    stats["has_inf"] = True
                                finite = np_arr[np.isfinite(np_arr)]
                                if len(finite) > 0:
                                    local_min = float(finite.min())
                                    local_max = float(finite.max())
                                    if stats["min_val"] is None or local_min < stats["min_val"]:
                                        stats["min_val"] = local_min
                                    if stats["max_val"] is None or local_max > stats["max_val"]:
                                        stats["max_val"] = local_max
                        
                        elif pa.types.is_string(col.type) or pa.types.is_large_string(col.type):
                            arr = col.drop_null()
                            if len(arr) > 0:
                                lengths = [len(str(v)) for v in arr.to_pylist()[:500]]
                                local_max = max(lengths) if lengths else 0
                                if local_max > stats["max_str_len"]:
                                    stats["max_str_len"] = local_max
                        
                        elif pa.types.is_timestamp(col.type) or pa.types.is_date(col.type):
                            arr = col.drop_null()
                            if len(arr) > 0:
                                py_vals = arr.to_pylist()[:100]
                                for v in py_vals:
                                    try:
                                        if hasattr(v, 'year') and (v.year < 1900 or v.year > 2100):
                                            stats["has_invalid_date"] = True
                                            break
                                    except Exception:
                                        stats["has_invalid_date"] = True
                                        break
                    except Exception:
                        pass
                        
                del rg  # Free memory
                
        except Exception as e:
            audit.issues.append({
                "severity": "critical",
                "msg": f"Failed to read {chunk_file}: {e}"
            })
    
    audit.total_rows = total_rows
    audit.total_size_bytes = total_size
    audit.schema = reference_schema
    
    # Build column report
    for col_name, stats in col_stats.items():
        total = stats["total_count"]
        nulls = stats["null_count"]
        null_pct = round(nulls / max(total, 1) * 100, 2)
        approx_unique = len(stats["unique_approx"])
        capped = approx_unique >= 50000
        
        col_report = {
            "arrow_type": stats["arrow_types_seen"][0] if stats["arrow_types_seen"] else "unknown",
            "total_values": total,
            "null_count": nulls,
            "null_pct": null_pct,
            "approx_unique": approx_unique,
            "unique_capped": capped,
            "min": stats["min_val"],
            "max": stats["max_val"],
        }
        
        if stats["max_str_len"] > 0:
            col_report["max_str_len"] = stats["max_str_len"]
        if stats["has_negative"]:
            col_report["has_negative"] = True
        if stats["has_overflow_int32"]:
            col_report["has_overflow_int32"] = True
        if stats["has_inf"]:
            col_report["has_inf"] = True
        if stats["has_invalid_date"]:
            col_report["has_invalid_date"] = True
        
        # Mixed arrow types across chunks?
        unique_types = set(stats["arrow_types_seen"])
        if len(unique_types) > 1:
            col_report["mixed_types"] = list(unique_types)
        
        audit.columns[col_name] = col_report
        
        # ── Issue detection ──
        if null_pct == 100:
            audit.issues.append({
                "severity": "warning", "column": col_name,
                "msg": f"Column '{col_name}' is 100% NULL"
            })
        elif null_pct > 90:
            audit.issues.append({
                "severity": "info", "column": col_name,
                "msg": f"Column '{col_name}' is {null_pct}% NULL"
            })
        
        if len(unique_types) > 1:
            audit.issues.append({
                "severity": "high", "column": col_name,
                "msg": f"Schema drift: '{col_name}' has types {unique_types} across chunks"
            })
        
        if stats["has_overflow_int32"]:
            audit.issues.append({
                "severity": "info", "column": col_name,
                "msg": f"'{col_name}' exceeds INT32 range: [{stats['min_val']}, {stats['max_val']}]"
            })
        
        if stats["has_inf"]:
            audit.issues.append({
                "severity": "high", "column": col_name,
                "msg": f"'{col_name}' contains INF values"
            })
        
        if stats["has_invalid_date"]:
            audit.issues.append({
                "severity": "high", "column": col_name,
                "msg": f"'{col_name}' contains dates outside 1900-2100"
            })
        
        if stats["max_str_len"] > 4000:
            audit.issues.append({
                "severity": "warning", "column": col_name,
                "msg": f"'{col_name}' has strings up to {stats['max_str_len']} chars (>4000)"
            })
    
    # ── PK/FK detection ──
    for col_name, report in audit.columns.items():
        col_lower = col_name.lower()
        is_key = col_lower.endswith("key") or col_lower.endswith("_id") or col_lower == "id"
        approx_u = report["approx_unique"]
        null_pct = report["null_pct"]
        
        if is_key and null_pct == 0:
            if not report.get("unique_capped") and approx_u == audit.total_rows and audit.total_rows > 0:
                audit.pk_candidates.append(col_name)
            elif is_key:
                audit.fk_candidates.append(col_name)
        elif is_key:
            audit.fk_candidates.append(col_name)
    
    # Schema drift issues
    if audit.schema_drift:
        for drift in audit.schema_drift:
            audit.issues.append({"severity": "critical", "msg": f"Schema drift: {drift}"})
    
    return audit


# ═══════════════════════════════════════════════════════════════
# RELATIONSHIP MATRIX
# ═══════════════════════════════════════════════════════════════

def build_relationship_matrix(audits: List[TableAudit]) -> Dict:
    """Detect FK→PK relationships between tables based on column names and cardinality."""
    relationships = []
    key_columns = {}  # col_name → [(table, cardinality, is_pk)]
    
    for audit in audits:
        for col_name, col_info in audit.columns.items():
            col_lower = col_name.lower()
            if col_lower.endswith("key") or col_lower.endswith("_id"):
                is_pk = col_name in audit.pk_candidates
                key_columns.setdefault(col_name, []).append({
                    "table": audit.name,
                    "cardinality": col_info["approx_unique"],
                    "null_pct": col_info["null_pct"],
                    "is_pk": is_pk,
                    "rows": audit.total_rows,
                })
    
    # For each shared key column, detect relationships
    for col_name, tables in key_columns.items():
        if len(tables) < 2:
            continue
        
        # Find the dimension (smallest table where this is PK or high-uniqueness)
        dim_candidates = sorted(tables, key=lambda t: t["cardinality"])
        fact_candidates = sorted(tables, key=lambda t: -t["rows"])
        
        for dim in dim_candidates:
            for fact in fact_candidates:
                if dim["table"] == fact["table"]:
                    continue
                if dim["rows"] < fact["rows"]:
                    relationships.append({
                        "column": col_name,
                        "dimension": dim["table"],
                        "dim_cardinality": dim["cardinality"],
                        "fact": fact["table"],
                        "fact_cardinality": fact["cardinality"],
                        "fact_null_pct": fact["null_pct"],
                        "type": "many-to-one" if dim.get("is_pk") else "suspected",
                    })
    
    # Deduplicate
    seen = set()
    unique_rels = []
    for r in relationships:
        key = (r["column"], r["dimension"], r["fact"])
        if key not in seen:
            seen.add(key)
            unique_rels.append(r)
    
    return {"relationships": unique_rels, "shared_keys": {k: len(v) for k, v in key_columns.items() if len(v) > 1}}


def detect_snowflake(audits: List[TableAudit], relationships: List[Dict]) -> List[Dict]:
    """Detect snowflake patterns (Dim → Dim chains)."""
    dim_names = set()
    fact_names = set()
    
    for a in audits:
        if a.name.startswith("Dim"):
            dim_names.add(a.name)
        elif a.name.startswith("Fact"):
            fact_names.add(a.name)
    
    snowflakes = []
    for r in relationships:
        if r["dimension"] in dim_names and r["fact"] in dim_names:
            snowflakes.append({
                "parent_dim": r["dimension"],
                "child_dim": r["fact"],
                "join_column": r["column"],
                "recommendation": f"Flatten {r['dimension']} into {r['fact']} for star schema"
            })
    
    return snowflakes


# ═══════════════════════════════════════════════════════════════
# REPORT GENERATORS
# ═══════════════════════════════════════════════════════════════

def generate_markdown(audits: List[TableAudit], rel_matrix: Dict, snowflakes: List[Dict]) -> str:
    lines = []
    lines.append("# PARQUET STRUCTURAL AUDIT REPORT")
    lines.append(f"\n**Generated**: {datetime.now().isoformat()}")
    lines.append(f"**Directory**: `{OUTPUT_DIR}`")
    lines.append(f"**Tables scanned**: {len(audits)}")
    
    total_rows = sum(a.total_rows for a in audits)
    total_size = sum(a.total_size_bytes for a in audits)
    lines.append(f"**Total rows**: {total_rows:,}")
    lines.append(f"**Total size**: {total_size / (1024**2):.1f} MB")
    
    # Summary table
    lines.append("\n## Summary\n")
    lines.append("| Table | Rows | Cols | Chunks | Size (MB) | Issues | PKs | FKs |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for a in sorted(audits, key=lambda x: -x.total_rows):
        sz = a.total_size_bytes / (1024*1024)
        issues_count = len(a.issues)
        sev_icon = "🔴" if any(i["severity"] == "critical" for i in a.issues) else ("🟡" if any(i["severity"] == "high" for i in a.issues) else "✅")
        lines.append(
            f"| {sev_icon} {a.name} | {a.total_rows:,} | {len(a.columns)} | "
            f"{len(a.chunks)} | {sz:.1f} | {issues_count} | "
            f"{', '.join(a.pk_candidates) or '-'} | {', '.join(a.fk_candidates[:3]) or '-'} |"
        )
    
    # Per-table details
    for a in sorted(audits, key=lambda x: -x.total_rows):
        lines.append(f"\n---\n## {a.name}")
        lines.append(f"- **Rows**: {a.total_rows:,}")
        lines.append(f"- **Columns**: {len(a.columns)}")
        lines.append(f"- **Chunks**: {len(a.chunks)}")
        lines.append(f"- **Size**: {a.total_size_bytes / (1024*1024):.1f} MB")
        
        if a.pk_candidates:
            lines.append(f"- **PK candidates**: {', '.join(a.pk_candidates)}")
        if a.fk_candidates:
            lines.append(f"- **FK candidates**: {', '.join(a.fk_candidates)}")
        
        # Schema
        lines.append(f"\n### Schema\n")
        lines.append("| Column | Arrow Type | Nulls% | Unique≈ | Min | Max | Notes |")
        lines.append("|---|---|---|---|---|---|---|")
        for col_name, info in a.columns.items():
            notes = []
            if info.get("has_overflow_int32"):
                notes.append("⚠️INT32-overflow")
            if info.get("has_inf"):
                notes.append("🔴INF")
            if info.get("has_invalid_date"):
                notes.append("🔴bad-date")
            if info.get("mixed_types"):
                notes.append(f"🔴mixed:{info['mixed_types']}")
            if info["null_pct"] == 100:
                notes.append("⚠️ALL-NULL")
            if info.get("max_str_len", 0) > 4000:
                notes.append(f"⚠️strlen={info['max_str_len']}")
            if col_name in a.pk_candidates:
                notes.append("🔑PK")
            if col_name in a.fk_candidates:
                notes.append("🔗FK")
            
            min_v = info.get("min")
            max_v = info.get("max")
            min_s = str(min_v)[:20] if min_v is not None else "-"
            max_s = str(max_v)[:20] if max_v is not None else "-"
            uniq = f"{info['approx_unique']:,}{'+'  if info.get('unique_capped') else ''}"
            
            lines.append(
                f"| {col_name} | `{info['arrow_type']}` | {info['null_pct']}% | "
                f"{uniq} | {min_s} | {max_s} | {' '.join(notes)} |"
            )
        
        # Sample values
        if a.sample_values:
            lines.append(f"\n### Sample Values (first 5)\n")
            for col_name, vals in list(a.sample_values.items())[:10]:
                lines.append(f"- **{col_name}**: `{vals}`")
        
        # Issues
        if a.issues:
            lines.append(f"\n### Issues ({len(a.issues)})\n")
            for issue in a.issues:
                sev = issue["severity"]
                icon = {"critical": "🔴", "high": "🟡", "warning": "⚠️", "info": "ℹ️"}.get(sev, "•")
                lines.append(f"- {icon} **{sev.upper()}**: {issue['msg']}")
    
    # Relationship matrix
    lines.append("\n---\n## Relationship Matrix\n")
    rels = rel_matrix.get("relationships", [])
    if rels:
        lines.append("| Column | Dimension | Dim Card. | Fact | Fact Card. | Fact Null% | Type |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in rels:
            lines.append(
                f"| {r['column']} | {r['dimension']} | {r['dim_cardinality']:,} | "
                f"{r['fact']} | {r['fact_cardinality']:,} | {r['fact_null_pct']}% | {r['type']} |"
            )
    else:
        lines.append("No relationships detected.")
    
    # Shared keys
    shared = rel_matrix.get("shared_keys", {})
    if shared:
        lines.append("\n### Shared Key Columns\n")
        for k, count in sorted(shared.items(), key=lambda x: -x[1]):
            lines.append(f"- `{k}`: appears in {count} tables")
    
    # Snowflake detection
    if snowflakes:
        lines.append("\n---\n## Snowflake Patterns Detected\n")
        for sf in snowflakes:
            lines.append(f"- **{sf['child_dim']}** → **{sf['parent_dim']}** via `{sf['join_column']}`")
            lines.append(f"  - 💡 {sf['recommendation']}")
    
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    print("=" * 70)
    print("PARQUET STRUCTURAL AUDITOR v1")
    print(f"Scanning: {OUTPUT_DIR}")
    print("=" * 70)
    
    if not os.path.isdir(OUTPUT_DIR):
        print(f"ERROR: {OUTPUT_DIR} does not exist")
        sys.exit(1)
    
    # Discover tables
    table_dirs = []
    for entry in sorted(os.listdir(OUTPUT_DIR)):
        entry_path = os.path.join(OUTPUT_DIR, entry)
        if os.path.isdir(entry_path) and not entry.startswith("_"):
            parquets = [f for f in os.listdir(entry_path) if f.endswith(".parquet")]
            if parquets:
                table_dirs.append((entry, entry_path))
    
    print(f"Found {len(table_dirs)} tables\n")
    
    audits = []
    for i, (name, path) in enumerate(table_dirs, 1):
        print(f"[{i}/{len(table_dirs)}] Auditing {name}...", end=" ", flush=True)
        try:
            audit = audit_table(name, path)
            audits.append(audit)
            issues = len(audit.issues)
            print(f"{audit.total_rows:,} rows, {len(audit.columns)} cols, {len(audit.chunks)} chunks, {issues} issues")
        except Exception as e:
            print(f"ERROR: {e}")
    
    print(f"\n{'=' * 70}")
    print("Building relationship matrix...")
    rel_matrix = build_relationship_matrix(audits)
    rels = rel_matrix.get("relationships", [])
    print(f"  Found {len(rels)} relationships, {len(rel_matrix.get('shared_keys', {}))} shared keys")
    
    print("Detecting snowflake patterns...")
    snowflakes = detect_snowflake(audits, rels)
    print(f"  Found {len(snowflakes)} snowflake chains")
    
    # Generate reports
    print("\nGenerating reports...")
    
    md_report = generate_markdown(audits, rel_matrix, snowflakes)
    md_path = os.path.join(REPORT_DIR, "parquet_audit_report.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_report)
    print(f"  Markdown: {md_path}")
    
    json_data = {
        "generated_at": datetime.now().isoformat(),
        "source_dir": OUTPUT_DIR,
        "total_tables": len(audits),
        "total_rows": sum(a.total_rows for a in audits),
        "total_size_mb": round(sum(a.total_size_bytes for a in audits) / (1024*1024), 2),
        "tables": [a.to_dict() for a in audits],
        "relationships": rel_matrix,
        "snowflake_patterns": snowflakes,
        "critical_issues": [
            {"table": a.name, "issue": i}
            for a in audits for i in a.issues
            if i["severity"] in ("critical", "high")
        ],
    }
    json_path = os.path.join(REPORT_DIR, "parquet_audit_technical.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, indent=2, ensure_ascii=False, default=str)
    print(f"  JSON: {json_path}")
    
    # Print summary
    total_issues = sum(len(a.issues) for a in audits)
    critical = sum(1 for a in audits for i in a.issues if i["severity"] == "critical")
    high = sum(1 for a in audits for i in a.issues if i["severity"] == "high")
    
    print(f"\n{'=' * 70}")
    print(f"AUDIT COMPLETE")
    print(f"  Tables: {len(audits)}")
    print(f"  Total rows: {sum(a.total_rows for a in audits):,}")
    print(f"  Total issues: {total_issues} (critical={critical}, high={high})")
    print(f"  Relationships: {len(rels)}")
    print(f"  Snowflake chains: {len(snowflakes)}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
