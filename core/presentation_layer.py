import os
import pandas as pd
from openpyxl.utils import get_column_letter
from config.settings import PipelineConfig
from core.export_engine import ExportEngine
from core.logging_engine import setup_logger

logger = setup_logger("presentation_layer")

class ReportGenerator:
    @staticmethod
    def generate_reports(df: pd.DataFrame, table_name: str = "data", classifications: dict = None):
        """
        Generates the Forensic Audit and the Clean Business Report.
        """
        if df.empty:
            logger.warning("Empty dataframe received. No reports generated.")
            return

        # 1. El Registro Forense ({table_name}_audit.csv)
        # Anteriormente este nombre tenía hardcoded "infierno" (nombre del CSV
        # original con que se probó el pipeline). Quedaba "ventas_infierno_audit.csv"
        # para cualquier tabla. Removido.
        audit_filename = f"{table_name}_audit.csv"
        audit_path = os.path.join(PipelineConfig.OUTPUT_DIR, audit_filename)
        df.to_csv(audit_path, index=False, encoding='utf-8-sig')
        logger.info(f"Forensic Audit saved to {audit_path}")

        # 2. El Reporte de Negocio (reporte_jose_limpio.xlsx)
        from core.business_view_builder import BusinessViewBuilder

        # Detección de naturaleza transaccional: sólo dividimos en Legítimos /
        # Gastos cuando el SemanticEnricher dejó un *_domain_flag. Para datasets
        # no transaccionales (catálogos, encuestas, Metacritic) el killswitch
        # apagó el clasificador y exportamos una pestaña maestra.
        has_domain_flag = any(str(c).endswith('_domain_flag') for c in df.columns)

        excel_filename = f"{table_name}_reporte_jose_limpio.xlsx"
        excel_path = os.path.join(PipelineConfig.OUTPUT_DIR, excel_filename)

        if has_domain_flag:
            df_legit, df_foreign = BusinessViewBuilder.build_view(df, classifications=classifications)
            sheets = {
                'Movimientos Legítimos': df_legit,
                'Gastos No Relacionados': df_foreign,
            }
        else:
            df_clean, _ = BusinessViewBuilder.build_view(df, classifications=classifications)
            sheets = {'Datos Limpios': df_clean}

        try:
            with pd.ExcelWriter(excel_path, engine='openpyxl') as writer:
                for sheet_name, sheet_df in sheets.items():
                    sheet_df.to_excel(writer, sheet_name=sheet_name, index=False)

                # Auto-adjust column widths
                for sheet_name in writer.sheets:
                    worksheet = writer.sheets[sheet_name]
                    for col_idx, col_cells in enumerate(worksheet.columns, 1):
                        max_length = 0
                        col_letter = get_column_letter(col_idx)
                        for cell in col_cells:
                            try:
                                if cell.value:
                                    max_length = max(max_length, len(str(cell.value)))
                            except:
                                pass
                        adjusted_width = (max_length + 2)
                        worksheet.column_dimensions[col_letter].width = adjusted_width
            logger.info(f"Business Report saved to {excel_path} ({len(sheets)} sheet(s))")
        except Exception as e:
            logger.error(f"Failed to generate Business Report: {e}")
            
        # 3. La Capa Analítica (Exportación a AnalyticsDB para SQL y OLAP)
        #
        # Opt-in: sin servidor levantado, cada tabla se quedaba ~16 segundos
        # esperando el timeout de ODBC para terminar en un ERROR en el log. En
        # un run de 4 tablas eso era más de un minuto de espera para no
        # producir nada. Las bases consultables del run las genera ahora
        # `core/sql_export.py` (DuckDB + SQLite, sin servidor). Para volver a
        # exportar a SQL Server: PIPELINE_SQL_SERVER_EXPORT=1 en .env
        if not PipelineConfig.SQL_SERVER_EXPORT:
            logger.debug("Export a SQL Server deshabilitado "
                         "(PIPELINE_SQL_SERVER_EXPORT=0)")
            return audit_filename, excel_filename

        try:
            from sqlalchemy import create_engine, text
            from config.settings import get_sql_connection_string
            engine_url = get_sql_connection_string(PipelineConfig.SQL_DATABASE)
            master_url = get_sql_connection_string("master")
            
            engine = create_engine(engine_url, fast_executemany=True)
            for sheet_name, sheet_df in sheets.items():
                if sheet_df.empty:
                    continue
                
                # Nombres amigables para tablas SQL
                sql_table = table_name
                if "Legítimos" in sheet_name:
                    sql_table = f"Fact{table_name}" if "fact" in table_name.lower() else table_name
                elif "No Relacionados" in sheet_name:
                    sql_table = f"{table_name}_Foreign"
                elif "Datos Limpios" in sheet_name:
                    sql_table = f"Dim{table_name}" if "dim" in table_name.lower() else table_name
                    
                try:
                    sheet_df.to_sql(sql_table, engine, if_exists='replace', index=False)
                except Exception as db_e:
                    if "Cannot open database" in str(db_e):
                        logger.info("AnalyticsDB not found. Creating it for OLAP Engine...")
                        master_engine = create_engine(master_url)
                        with master_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                            conn.execute(text("CREATE DATABASE AnalyticsDB"))
                        logger.info("AnalyticsDB created. Retrying OLAP export...")
                        sheet_df.to_sql(sql_table, engine, if_exists='replace', index=False)
                    else:
                        raise db_e
            logger.info(f"✅ Data successfully exported to SQL Server (AnalyticsDB) for questions and OLAP cubes.")
        except Exception as e:
            logger.error(f"Failed to export to AnalyticsDB for OLAP: {e}")
            
        return audit_filename, excel_filename
