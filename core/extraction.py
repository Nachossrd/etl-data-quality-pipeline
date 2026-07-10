import pandas as pd
from sqlalchemy import text
from typing import Iterator
from core.logging_engine import setup_logger
from config.settings import PipelineConfig

logger = setup_logger("extraction_engine")

class ExtractionEngine:
    def __init__(self, sql_server):
        self.sql_server = sql_server

    def detect_tables(self) -> list:
        logger.info("Detecting tables...")
        engine = self.sql_server.engine
        query = text("""
            SELECT t.name AS TableName, s.name AS SchemaName, p.rows AS RowCounts
            FROM sys.tables t
            INNER JOIN sys.indexes i ON t.object_id = i.object_id
            INNER JOIN sys.partitions p ON i.object_id = p.object_id AND i.index_id = p.index_id
            INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
            WHERE t.is_ms_shipped = 0 AND i.type <= 1
            ORDER BY p.rows DESC
        """)
        
        tables = []
        with engine.connect() as conn:
            result = conn.execute(query)
            for row in result:
                schema_name = row[1]
                table_name = row[0]
                row_count = row[2]
                
                if row_count > 0:
                    tables.append({"schema": schema_name, "table": table_name, "rows": row_count})
                    
        logger.info(f"Detected {len(tables)} tables with data.")
        return tables

    def extract_table_chunked(self, schema: str, table_name: str) -> Iterator[pd.DataFrame]:
        logger.info(f"Extracting table [{schema}].[{table_name}] in chunks of {PipelineConfig.CHUNK_SIZE}")
        engine = self.sql_server.engine
        query = f"SELECT * FROM [{schema}].[{table_name}]"
        
        try:
            for chunk in pd.read_sql(query, engine, chunksize=PipelineConfig.CHUNK_SIZE):
                yield chunk
        except Exception as e:
            logger.error(f"Error extracting [{schema}].[{table_name}]: {e}")
