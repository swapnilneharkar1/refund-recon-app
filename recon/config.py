"""Settings loader.

Values are read (in order) from Streamlit secrets (.streamlit/secrets.toml) and
then environment variables. Nothing secret is ever hard-coded in this repo.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _get(key: str, default: str | None = None) -> str | None:
    try:
        import streamlit as st

        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:  # no secrets file / not running under streamlit
        pass
    return os.environ.get(key, default)


@dataclass
class Settings:
    # Workspace + SQL warehouse
    host: str | None            # e.g. adb-1234567890123456.7.azuredatabricks.net
    http_path: str | None       # e.g. /sql/1.0/warehouses/abcdef1234567890

    # Service principal auth
    #   "azure"      -> Entra ID (Azure AD) SP: client id + client secret + tenant id
    #   "databricks" -> Databricks-managed OAuth secret for the SP
    #   "default"    -> let the Databricks SDK pick up env auth (e.g. when deployed as a Databricks App)
    auth_type: str
    client_id: str | None
    client_secret: str | None
    tenant_id: str | None

    # Where tables / staging files go
    catalog: str
    schema: str
    volume: str | None          # UC volume used to stage uploaded files (recommended)
    table_prefix: str

    @classmethod
    def load(cls) -> "Settings":
        host = _get("DATABRICKS_HOST")
        if host:
            host = host.replace("https://", "").replace("http://", "").strip("/")
        return cls(
            host=host,
            http_path=_get("DATABRICKS_HTTP_PATH") or (
                f"/sql/1.0/warehouses/{_get('DATABRICKS_WAREHOUSE_ID')}" if _get("DATABRICKS_WAREHOUSE_ID") else None),
            auth_type=(_get("DATABRICKS_AUTH_TYPE", "azure") or "azure").lower(),
            client_id=_get("DATABRICKS_CLIENT_ID") or _get("ARM_CLIENT_ID"),
            client_secret=_get("DATABRICKS_CLIENT_SECRET") or _get("ARM_CLIENT_SECRET"),
            tenant_id=_get("DATABRICKS_TENANT_ID") or _get("ARM_TENANT_ID"),
            catalog=_get("RECON_CATALOG", "main") or "main",
            schema=_get("RECON_SCHEMA", "refund_recon") or "refund_recon",
            volume=_get("RECON_VOLUME", "recon_uploads"),
            table_prefix=_get("RECON_TABLE_PREFIX", "bp_refund") or "bp_refund",
        )

    def missing_for_databricks(self) -> list[str]:
        missing = []
        if not self.host:
            missing.append("DATABRICKS_HOST")
        if not self.http_path:
            missing.append("DATABRICKS_HTTP_PATH")
        if self.auth_type in ("azure", "databricks"):
            if not self.client_id:
                missing.append("DATABRICKS_CLIENT_ID")
            if not self.client_secret:
                missing.append("DATABRICKS_CLIENT_SECRET")
        if self.auth_type == "azure" and not self.tenant_id:
            missing.append("DATABRICKS_TENANT_ID")
        return missing
