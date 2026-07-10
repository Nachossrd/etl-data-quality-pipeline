import subprocess
import time
import sys

STEPS = [
    ("1. Rescue Missing Dims", "missing_dims_remediator.py"),
    ("2. Flatten Snowflake Models", "snowflake_flattener.py"),
    ("3. Structural Canonization", "structural_normalizer.py"),
    ("4. MASIVE MEMORY LOAD (SQL)", "advanced_memory_loader.py"),
    ("5. Columnstore Optimization", "columnstore_optimizer.py"),
    ("6. Referential Integrity (FKs)", "sql_builder.py"),
    ("7. Semantic SSAS Model", "ssas_generator.py"),
]

print("🚀 INICIANDO PIPELINE INDUSTRIAL OLAP 🚀\n" + "="*50)

for desc, script in STEPS:
    print(f"\n[>>>] EJECUTANDO: {desc} ({script})")
    start = time.time()
    
    try:
        # Popen permite ver la salida en vivo en la consola
        process = subprocess.Popen([sys.executable, script], stdout=sys.stdout, stderr=sys.stderr)
        process.wait()
        
        if process.returncode != 0:
            print(f"\n❌ ERROR CRÍTICO EN {script}. ABORTANDO PIPELINE.")
            sys.exit(1)
            
    except Exception as e:
        print(f"\n❌ FALLO EJECUTANDO {script}: {e}")
        sys.exit(1)
        
    print(f"[✅] COMPLETADO EN {time.time() - start:.1f} segundos.\n" + "-"*50)

print("\n🎉 PIPELINE COMPLETADO EXITOSAMENTE. DATA LISTA PARA SSAS TABULAR. 🎉")
