import os
import time
from sqlalchemy import text
from core.logging_engine import setup_logger
import re

logger = setup_logger("sql_restore")

class BackupRestorer:
    RESTORE_TIMEOUT_SECONDS = 900
    ONLINE_TIMEOUT_SECONDS = 900
    POLL_BASE_INTERVAL = 2
    POLL_MAX_INTERVAL = 16

    def __init__(self, sql_server, bak_path: str, host: str, port: int):
        self.sql_server = sql_server
        self.bak_path = bak_path
        self.host = host
        self.port = port
        self.db_name = None

    def _create_dynamic_connection(self, timeout: int = 30):
        import pyodbc
        from config.settings import PipelineConfig
        conn_str = (
            f"DRIVER={{ODBC Driver 17 for SQL Server}};"
            f"SERVER={self.host},{self.port};"
            f"DATABASE=master;"
            f"UID=sa;PWD={PipelineConfig.SQL_SA_PASSWORD};"
            f"Connection Timeout={timeout};"
        )
        conn = pyodbc.connect(conn_str, autocommit=True)
        conn.timeout = timeout
        return conn

    def detect_db_name(self) -> str:
        bak_linux = f"/var/opt/mssql/backup/{os.path.basename(self.bak_path)}"
        conn = None
        cursor = None
        try:
            conn = self._create_dynamic_connection(timeout=30)
            cursor = conn.cursor()
            cursor.execute(f"RESTORE HEADERONLY FROM DISK = N'{bak_linux}'")
            row = cursor.fetchone()
            if row:
                raw_name = str(row[0]).strip()
                self.db_name = re.sub(r'[^a-zA-Z0-9_]', '_', raw_name) or "RestoredDB"
            else:
                self.db_name = "RestoredDB"
            while cursor.nextset():
                pass
        except Exception as e:
            self.db_name = "RestoredDB"
            logger.warning(f"HEADERONLY failed: {e}")
        finally:
            if cursor:
                try: cursor.close()
                except: pass
            if conn:
                try: conn.close()
                except: pass
        return self.db_name

    def restore(self) -> bool:
        if not self.db_name:
            self.detect_db_name()

        logger.info(f"Starting RESTORE for DB: {self.db_name}")
        
        if not self._execute_restore():
            return False
            
        if not self._wait_for_online():
            return False
            
        return self._verify_database_health()

    def _execute_restore(self) -> bool:
        bak_linux = f"/var/opt/mssql/backup/{os.path.basename(self.bak_path)}"
        
        conn_flist = None
        cursor_flist = None
        try:
            conn_flist = self._create_dynamic_connection(timeout=60)
            cursor_flist = conn_flist.cursor()
            cursor_flist.execute(f"RESTORE FILELISTONLY FROM DISK = N'{bak_linux}'")
            archivos = cursor_flist.fetchall()
            while cursor_flist.nextset(): pass
            
            data_logical = [f[0] for f in archivos if f[2] == 'D'][0]
            log_logical = [f[0] for f in archivos if f[2] == 'L'][0]
        except Exception as e:
            logger.error(f"FILELISTONLY failed: {e}")
            return False
        finally:
            if cursor_flist:
                try: cursor_flist.close()
                except: pass
            if conn_flist:
                try: conn_flist.close()
                except: pass

        restore_query = f"""
            RESTORE DATABASE [{self.db_name}]
            FROM DISK = N'{bak_linux}'
            WITH REPLACE,
            MOVE N'{data_logical}' TO N'/var/opt/mssql/data/{self.db_name}.mdf',
            MOVE N'{log_logical}' TO N'/var/opt/mssql/data/{self.db_name}.ldf',
            RECOVERY
        """
        
        conn_restore = None
        cursor_restore = None
        try:
            conn_restore = self._create_dynamic_connection(timeout=self.RESTORE_TIMEOUT_SECONDS)
            cursor_restore = conn_restore.cursor()
            cursor_restore.execute(restore_query)
            while cursor_restore.nextset(): pass
            return True
        except Exception as e:
            logger.error(f"RESTORE failed: {e}")
            return False
        finally:
            if cursor_restore:
                try: cursor_restore.close()
                except: pass
            if conn_restore:
                try: conn_restore.close()
                except: pass

    def _wait_for_online(self) -> bool:
        start = time.time()
        interval = self.POLL_BASE_INTERVAL
        while time.time() - start < self.ONLINE_TIMEOUT_SECONDS:
            poll_engine = self.sql_server.create_engine_isolated(timeout=10)
            try:
                with poll_engine.connect() as conn:
                    status = conn.execute(text(
                        f"SELECT state_desc FROM sys.databases WHERE name = '{self.db_name}'"
                    )).scalar()
                    if status == "ONLINE":
                        return True
            except Exception as e:
                pass
            finally:
                poll_engine.dispose()
            time.sleep(interval)
            interval = min(interval * 2, self.POLL_MAX_INTERVAL)
        logger.error("Timeout waiting for ONLINE status")
        return False

    def _verify_database_health(self) -> bool:
        health_engine = self.sql_server.create_engine_isolated(timeout=15)
        try:
            with health_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                row = conn.execute(text(f"""
                    SELECT state_desc, user_access_desc
                    FROM sys.databases WHERE name = '{self.db_name}'
                """)).fetchone()
                if not row or row[0] != "ONLINE":
                    return False
                if row[1] == "SINGLE_USER":
                    conn.execute(text(f"ALTER DATABASE [{self.db_name}] SET MULTI_USER"))
            return True
        except Exception as e:
            logger.error(f"Health verification failed: {e}")
            return False
        finally:
            health_engine.dispose()
