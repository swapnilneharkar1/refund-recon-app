"""Databricks backend: every table, load and calculation runs on the Databricks SQL warehouse.

Each run DROPs the previous tables and CREATEs them again from the newly uploaded files.
(tests/local_backend.py has a DuckDB stand-in used only by the automated tests.)
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


def build_config(s: Settings, quick: bool = False):
    """Databricks SDK config for the chosen auth method (shared by SQL + REST calls).
    quick=True gives up after ~30 s instead of retrying for minutes (used by Test connection)."""
    from databricks.sdk.core import Config as _Config

    def Config(**kw):  # noqa: N802
        if quick:
            kw.update(http_timeout_seconds=20, retry_timeout_seconds=30)
        return _Config(**kw)

    if s.auth_type == "azure":
        return Config(host=f"https://{s.host}", azure_client_id=s.client_id,
                      azure_client_secret=s.client_secret, azure_tenant_id=s.tenant_id,
                      auth_type="azure-client-secret")
    if s.auth_type == "databricks":
        return Config(host=f"https://{s.host}", client_id=s.client_id,
                      client_secret=s.client_secret, auth_type="oauth-m2m")
    # "default": e.g. inside a Databricks App, env vars are injected for the app's SP
    return Config(host=f"https://{s.host}") if s.host else Config()


def _err(e: Exception) -> str:
    """Full error text including wrapped causes (the SQL driver hides the real reason)."""
    parts, seen = [], set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        msg = f"{type(e).__name__}: {e}".strip()
        ctx = getattr(e, "context", None)
        if ctx:
            msg += f" | context: {ctx}"
        parts.append(msg[:600])
        e = e.__cause__ or e.__context__
    return "  ←  ".join(parts)


def _hint(text: str) -> str:
    t = text.lower()
    if "ip acl" in t or "ip access list" in t or ("source ip" in t and "blocked" in t):
        return (" → The workspace only accepts connections from approved networks (IP access list). "
                "Ask the Databricks admin to allow the network this app runs from.")
    if any(k in t for k in ("timed out", "invalid url 'none'", "max retries", "connectionerror",
                            "name or service not known", "failed to resolve", "connection refused")):
        return (" → The app could not reach the Databricks workspace at all. Usually the workspace is "
                "restricted to the company network (private link / firewall), so a server outside it "
                "(e.g. Streamlit Cloud) is blocked. Needs IT/Databricks admin to allow it.")
    if "invalid_client" in t or "client authentication failed" in t or "invalid client secret" in t:
        return " → The client ID / secret pair was rejected. Check the secret and DATABRICKS_AUTH_TYPE."
    if "can_use" in t or "permission_denied" in t or "does not have" in t or "not authorized" in t:
        return " → Signed in, but RPADLAKEFLOW-PROD lacks a permission here (see the message)."
    return ""


def diagnose(s: Settings) -> list[tuple[str, bool, str]]:
    """Check the connection step by step so the failing step and its real error are visible."""
    steps: list[tuple[str, bool, str]] = []
    try:
        cfg = build_config(s, quick=True)
        hdr = cfg.authenticate()
        ok = bool(hdr and hdr.get("Authorization"))
        steps.append(("1. Get access token", ok, f"auth type '{s.auth_type}'" if ok else "no token returned"))
        if not ok:
            return steps
    except Exception as e:  # noqa: BLE001
        m = _err(e); steps.append(("1. Get access token", False, m + _hint(m)))
        return steps
    try:
        from databricks.sdk import WorkspaceClient
        w = WorkspaceClient(config=cfg)
        me = w.current_user.me()
        steps.append(("2. Reach workspace", True, f"signed in as {me.display_name or me.user_name}"))
    except Exception as e:  # noqa: BLE001
        m = _err(e); steps.append(("2. Reach workspace", False, m + _hint(m)))
        return steps
    wid = (s.http_path or "").rstrip("/").split("/")[-1]
    try:
        wh = w.warehouses.get(wid)
        state = getattr(wh.state, "value", wh.state)
        steps.append(("3. SQL warehouse", True, f"{wh.name} ({wid}) – state {state}"))
    except Exception as e:  # noqa: BLE001
        m = _err(e); steps.append(("3. SQL warehouse", False, f"{wid}: {m}{_hint(m)}"))
    try:
        b = DatabricksBackend(s)
        b.query("SELECT 1")
        b.close()
        steps.append(("4. Run SQL", True, "SELECT 1 ok"))
    except Exception as e:  # noqa: BLE001
        m = _err(e); steps.append(("4. Run SQL", False, m + _hint(m)))
        return steps
    try:
        b = DatabricksBackend(s)
        b.query(f"SHOW TABLES IN `{s.catalog}`.`{s.schema}` LIKE 'zz_none'")
        b.close()
        steps.append((f"5. Access {s.catalog}.{s.schema}", True, "schema visible"))
    except Exception as e:  # noqa: BLE001
        m = _err(e); steps.append((f"5. Access {s.catalog}.{s.schema}", False, m + _hint(m)))
    return steps


class DatabricksBackend:
    name = "Databricks"
    dialect = DATABRICKS_DIALECT

    def __init__(self, s: Settings):
        from databricks import sql

        self.s = s
        self.cfg = build_config(s)

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

    def prepare_staging(self) -> str | None:
        """Make sure the staging volume exists. Returns a notice if we had to fall back."""
        if not self.s.volume:
            return None
        cat, sch, vol = self.s.catalog, self.s.schema, self.s.volume
        try:
            found = self.query(f"SHOW VOLUMES IN `{cat}`.`{sch}` LIKE '{vol}'")
            if len(found):
                return None
            self.execute(f"CREATE VOLUME IF NOT EXISTS `{cat}`.`{sch}`.`{vol}`")
            return None
        except Exception as e:  # noqa: BLE001
            self.s.volume = None
            return (f"Staging volume {cat}.{sch}.{vol} not available ({str(e).splitlines()[0][:150]}). "
                    f"Loading rows directly instead – slower for big files.")

    # ---- table management ----------------------------------------------
    def drop_table(self, table: str) -> None:
        self.execute(f"DROP TABLE IF EXISTS {self.fqn(table)}")

    def recreate_table(self, table: str, select_sql: str) -> None:
        """Drop the previous version of the table, then create it fresh from the SELECT."""
        self.drop_table(table)
        self.execute(f"CREATE TABLE {self.fqn(table)} AS\n{select_sql}")

    # ---- loading -------------------------------------------------------
    def load_dataframe(self, df: pd.DataFrame, table: str, run_id: str) -> None:
        if self.s.volume:
            self._load_via_volume(df, table, run_id)
        else:
            self._load_via_insert(df, table)

    def _load_via_volume(self, df: pd.DataFrame, table: str, run_id: str) -> None:
        """Fast path: parquet -> UC volume -> CREATE TABLE AS SELECT. Staged file is deleted afterwards."""
        buf = io.BytesIO()
        df.to_parquet(buf, index=False)
        buf.seek(0)
        path = f"/Volumes/{self.s.catalog}/{self.s.schema}/{self.s.volume}/uploads/{run_id}/{table}.parquet"
        self.ws.files.upload(path, buf, overwrite=True)
        try:
            self.recreate_table(table, f"SELECT * FROM parquet.`{path}`")
        finally:
            try:
                self.ws.files.delete(path)
                self.ws.files.delete_directory(path.rsplit("/", 1)[0])
            except Exception:  # noqa: BLE001
                pass

    def _load_via_insert(self, df: pd.DataFrame, table: str, chunk: int = 2000) -> None:
        """Fallback when no volume is available: batched INSERT ... VALUES."""
        cols = ", ".join(f"`{c}` {_spark_type(df[c])}" for c in df.columns)
        self.drop_table(table)
        self.execute(f"CREATE TABLE {self.fqn(table)} ({cols})")
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
        except Exception:  # noqa: BLE001
            pass
