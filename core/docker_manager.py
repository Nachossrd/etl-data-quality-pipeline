import subprocess
import time
import os
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool
import pyodbc
import socket
from core.logging_engine import setup_logger
from config.settings import PipelineConfig

logger = setup_logger("docker_manager")

class EphemeralSQLServer:
    """Manages an ephemeral SQL Server container for safe data processing."""
    
    def __init__(self, bak_path: str):
        self.bak_path = os.path.abspath(bak_path)
        self.container_port = PipelineConfig.SQL_PORT
        self.container_name = "datacleaner_sql_ephemeral"
        self.container_id = None
        self.engine = None
        
    def _build_connection_url(self, database: str = "master") -> str:
        return (
            f"mssql+pyodbc://sa:{PipelineConfig.SQL_SA_PASSWORD}@localhost:{self.container_port}/{database}"
            "?driver=ODBC+Driver+17+for+SQL+Server"
        )
        
    def create_engine_isolated(self, database: str = "master", timeout: int = 30):
        return create_engine(
            self._build_connection_url(database),
            poolclass=NullPool,
            connect_args={"timeout": timeout, "attrs_before": {113: timeout}},
        )
        
    def create_raw_connection(self, database: str = "master", timeout: int = 900):
        conn_str = (
            f"DRIVER={{ODBC Driver 17 for SQL Server}};"
            f"SERVER=localhost,{self.container_port};"
            f"DATABASE={database};"
            f"UID=sa;PWD={PipelineConfig.SQL_SA_PASSWORD};"
            f"Connection Timeout={timeout};"
        )
        conn = pyodbc.connect(conn_str, autocommit=True)
        conn.timeout = timeout
        return conn

    def _find_free_port(self, start=1433, end=1500) -> int:
        for port in range(start, end + 1):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(('127.0.0.1', port)) != 0:
                    return port
        raise RuntimeError(f"No free ports found between {start} and {end}")

    def _cleanup_previous_container(self):
        logger.info(f"Cleaning up previous container {self.container_name} if exists...")
        subprocess.run(["docker", "rm", "-f", self.container_name], capture_output=True)

    def start_sql_container(self, timeout: int = 120) -> dict:
        self._cleanup_previous_container()
        self.container_port = self._find_free_port()
        
        logger.info(f"Starting ephemeral SQL Server on dynamic port {self.container_port}...")
        try:
            result = subprocess.run(
                [
                    "docker", "run", "-d",
                    "--name", self.container_name,
                    "--memory", PipelineConfig.DOCKER_MEMORY,
                    "-e", "ACCEPT_EULA=Y",
                    "-e", f"SA_PASSWORD={PipelineConfig.SQL_SA_PASSWORD}",
                    "-p", f"{self.container_port}:1433",
                    "-v", f"{self.bak_path}:/var/opt/mssql/backup/{os.path.basename(self.bak_path)}",
                    "mcr.microsoft.com/mssql/server:2022-latest"
                ],
                capture_output=True, text=True, timeout=300
            )
            if result.returncode != 0:
                logger.error(f"Docker failed: {result.stderr}")
                return None
                
            self.container_id = result.stdout.strip()[:12]
            logger.info(f"Container created: {self.container_id} ({self.container_name})")
            
            if self._wait_for_sql_ready(self.container_port, timeout):
                return {
                    'port': self.container_port,
                    'host': '127.0.0.1',
                    'container_name': self.container_name,
                    'password': PipelineConfig.SQL_SA_PASSWORD,
                    'user': 'sa'
                }
            return None
        except Exception as e:
            logger.error(f"Failed to start SQL Server: {e}")
            return None

    def _wait_for_sql_ready(self, port: int, timeout: int = 90) -> bool:
        logger.info("Waiting for SQL Server to be ready (Polling with sqlcmd)...")
        start = time.time()
        interval = 2
        while time.time() - start < timeout:
            try:
                result = subprocess.run(
                    ["docker", "exec", self.container_name, "/opt/mssql-tools18/bin/sqlcmd", "-S", "localhost", "-U", "sa", "-P", PipelineConfig.SQL_SA_PASSWORD, "-C", "-Q", "SELECT 1"],
                    capture_output=True
                )
                if result.returncode == 0:
                    self.engine = self.create_engine_isolated(timeout=30)
                    logger.info("✅ SQL Server is ready")
                    return True
            except Exception as e:
                pass
            time.sleep(interval)
            interval = min(interval * 2, 16)
            
        logger.error("❌ Timeout waiting for SQL Server")
        return False

    def destroy_container(self):
        if self.container_name:
            logger.info(f"Destroying SQL Server container {self.container_name}...")
            subprocess.run(["docker", "rm", "-f", self.container_name], capture_output=True)
            logger.info(f"Container {self.container_name} removed")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.destroy_container()
