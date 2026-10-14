"""SQL execution backends.

DatabricksBackend  - the real thing: SQL warehouse + service principal.
LocalBackend       - DuckDB in-process, for trying the app / testing logic without Databricks.

Both run the *same* reconciliation SQL (see logic.py); the few functions whose
names differ between the two engines live in the `dialect` dicts.
"""
from __future__ import annotations

import io
import math
from datetime import date, datetime

import pandas as pd

from .config import Settings

DATABRICKS_DIALECT = {
    "str_agg": lambda col: f"array_join(sort_array(collect_set(CAST({col} AS STRING))), ', ')",
    "now": "current_timestamp()",
    "quote": lambda v: str(v).replace("\\", "\\\\").replace("'", "\\'"),
}
DUCKDB_DIALECT = {
    "str_agg": lambda col: f"string_agg(DISTINCT CAST({col} AS VARCHAR), ', ' ORDER BY CAST({col} AS VARCHAR))",
    "now": "current_timestamp",
    "quote": lambda v: str(v).replace("'", "''"),
}


def _sql_literal(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NA or v is pd.NaT:
        return "NULL"
    if isinstance(v, (int,)) and not isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, (datetime, pd.Timestamp)):
        return f"DATE'{v.date().isoformat()}'"
    if isinstance(v, date):
        return f"DATE'{v.isoformat()}'"
    s = str(v).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{s}'"


def _spark_type(series: pd.Series) -> str:
    if series.name == "SCHDATE":
        return "DATE"
    if pd.api.types.is_integer_dtype(series):
        return "BIGINT"
    if pd.api.types.is_float_dtype(series):
        return "DOUBLE"
    return "STRING"


class DatabricksBackend:
    name = "Databricks"
    dialect = DATABRICKS_DIALECT

    def __init__(self, s: Settings):
        from databricks import sql
        from databricks.sdk.core import Config

        self.s = s
        if s.auth_type == "azure":
            self.cfg = Config(
                host=f"https://{s.host}",
                azure_client_id=s.client_id,
                azure_client_secret=s.client_secret,
                azure_tenant_id=s.tenant_id,
                auth_type="azure-client-secret",
            )
        elif s.auth_type == "databricks":
            self.cfg = Config(
                host=f"https://{s.host}",
                client_id=s.client_id,
                client_secret=s.client_secret,
                auth_type="oauth-m2m",
            )
        else:  # "default": e.g. inside a Databricks App, env vars are injected for the app's SP
            self.cfg = Config(host=f"https://{s.host}") if s.host else Config()

        self.conn = sql.connect(
            server_hostname=s.host or self.cfg.host.replace("https://", ""),
            http_path=s.http_path,
            credentials_provider=lambda: self.cfg.authenticate,
        )
        self._ws = None

    # ---- helpers -------------------------------------------------------
    @property
    def ws(self):
        if self._ws is None:
            from databricks.sdk import WorkspaceClient
            self._ws = WorkspaceClient(config=self.cfg)
        return self._ws

    def fqn(self, table: str) -> str:
        return f"`{self.s.catalog}`.`{self.s.schema}`.`{table}`"

    def execute(self, statement: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(statement)

    def query(self, statement: str) -> pd.DataFrame:
        with self.conn.cursor() as cur:
            cur.execute(statement)
            return cur.fetchall_arrow().to_pandas()

    def whoami(self) -> str:
        return str(self.query("SELECT current_user() AS u").iloc[0, 0])

    def ensure_schema(self) -> None:
        self.execute(f"CREATE SCHEMA IF NOT EXISTS `{self.s.catalog}`.`{self.s.schema}`")
        if self.s.volume:
            self.execute(f"CREATE VOLUME IF NOT EXISTS `{self.s.catalog}`.`{self.s.schema}`.`{self.s.volume}`")

    # ---- loading -------------------------------------------------------
    def load_dataframe(self, df: pd.DataFrame, table: str, run_id: str) -> None:
        if self.s.volume:
            self._load_via_volume(df, table, run_id)
        else:
            self._load_via_insert(df, table)

    def _load_via_volume(self, df: pd.DataFrame, table: str, run_id: str) -> None:
        """Fast path: parquet -> UC volume -> CTAS. Handles lakhs of rows easily."""
        buf = io.BytesIO()
        df.to_parquet(buf, index=False)
        buf.seek(0)
        path = f"/Volumes/{self.s.catalog}/{self.s.schema}/{self.s.volume}/uploads/{run_id}/{table}.parquet"
        self.ws.files.upload(path, buf, overwrite=True)
        self.execute(f"CREATE OR REPLACE TABLE {self.fqn(table)} AS SELECT * FROM parquet.`{path}`")

    def _load_via_insert(self, df: pd.DataFrame, table: str, chunk: int = 1000) -> None:
        """Fallback when no volume is configured: batched INSERT ... VALUES."""
        cols = ", ".join(f"`{c}` {_spark_type(df[c])}" for c in df.columns)
        self.execute(f"CREATE OR REPLACE TABLE {self.fqn(table)} ({cols})")
        records = df.astype(object).where(df.notna(), None).itertuples(index=False, name=None)
        batch: list[str] = []
        for rec in records:
            batch.append("(" + ", ".join(_sql_literal(v) for v in rec) + ")")
            if len(batch) >= chunk:
                self.execute(f"INSERT INTO {self.fqn(table)} VALUES " + ", ".join(batch))
                batch = []
        if batch:
            self.execute(f"INSERT INTO {self.fqn(table)} VALUES " + ", ".join(batch))

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


class LocalBackend:
    """DuckDB stand-in so the app (and the logic) can be tried without Databricks."""

    name = "Local test (DuckDB)"
    dialect = DUCKDB_DIALECT

    def __init__(self, path: str = ":memory:"):
        import duckdb

        self.con = duckdb.connect(path)

    def fqn(self, table: str) -> str:
        return f'"{table}"'

    def execute(self, statement: str) -> None:
        self.con.execute(statement)

    def query(self, statement: str) -> pd.DataFrame:
        return self.con.execute(statement).df()

    def whoami(self) -> str:
        return "local-user"

    def ensure_schema(self) -> None:
        pass

    def load_dataframe(self, df: pd.DataFrame, table: str, run_id: str) -> None:
        tmp = df.copy()
        tmp["SCHDATE"] = pd.to_datetime(tmp["SCHDATE"])
        self.con.register("_incoming", tmp)
        self.con.execute(
            f"CREATE OR REPLACE TABLE {self.fqn(table)} AS "
            f"SELECT * REPLACE (CAST(SCHDATE AS DATE) AS SCHDATE) FROM _incoming")
        self.con.unregister("_incoming")

    def close(self):
        self.con.close()
