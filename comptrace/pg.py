"""A throwaway local PostgreSQL cluster (initdb in a temporary directory, Unix socket only, stopped on exit)."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import psycopg


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LocalPostgres:
    owner = "comp_owner"

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="comptrace-pg-"))
        self.data = self.dir / "data"
        self.port = _free_port()

    def __enter__(self):
        env = {**os.environ, "LC_ALL": "C"}
        subprocess.run(["initdb", "-D", str(self.data), "-U", self.owner, "-A", "trust", "-E", "UTF8", "--no-locale"],
                       check=True, capture_output=True, env=env)
        opts = f"-c listen_addresses='' -c unix_socket_directories='{self.dir}' -p {self.port}"
        subprocess.run(["pg_ctl", "-D", str(self.data), "-o", opts, "-l", str(self.dir / "log"), "-w", "start"],
                       check=True, capture_output=True, env=env)
        return self

    def __exit__(self, *exc):
        subprocess.run(["pg_ctl", "-D", str(self.data), "-m", "fast", "-w", "stop"], capture_output=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def connect(self, user: str | None = None, autocommit: bool = False) -> psycopg.Connection:
        return psycopg.connect(host=str(self.dir), port=self.port, user=user or self.owner, dbname="postgres", autocommit=autocommit)

    def version(self) -> str:
        with self.connect() as c:
            return c.execute("show server_version").fetchone()[0]
