"""Settings loader.

Values come from environment variables (app.yaml on Databricks Apps), or from Streamlit
secrets (Streamlit Cloud "Secrets" box / local .streamlit/secrets.toml). Secrets are only read
when they can exist – older Streamlit
versions print a "No secrets found" error box whenever st.secrets is touched without one.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _secrets_file_exists() -> bool:
    candidates = [Path.home() / ".streamlit" / "secrets.toml",
                  Path.cwd() / ".streamlit" / "secrets.toml",
                  Path(__file__).resolve().parent.parent / ".streamlit" / "secrets.toml"]
    return any(p.is_file() for p in candidates)


# Streamlit Community Cloud keeps app secrets (Settings -> Secrets) under /mount/src.
_ON_STREAMLIT_CLOUD = os.path.isdir("/mount/src")
_HAS_SECRETS = _secrets_file_exists() or _ON_STREAMLIT_CLOUD


def _get(key: str, default: str | None = None) -> str | None:
    if key in os.environ:
        return os.environ[key]
    if _HAS_SECRETS:
        try:
            import streamlit as st
            if key in st.secrets:
                return str(st.secrets[key])
        except Exception:  # noqa: BLE001
            pass
    return default


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
        if not host and (_get("DATABRICKS_AUTH_TYPE") or "").lower() == "default":
            try:  # Databricks Apps / notebooks: let the SDK find the workspace
                from databricks.sdk.core import Config
                host = Config().host
            except Exception:  # noqa: BLE001
                host = None
        if host:
            host = host.replace("https://", "").replace("http://", "").strip("/")
        return cls(
            host=host,
            http_path=_get("DATABRICKS_HTTP_PATH") or (
                f"/sql/1.0/warehouses/{_get('DATABRICKS_WAREHOUSE_ID')}" if _get("DATABRICKS_WAREHOUSE_ID") else None),
            auth_type=(_get("DATABRICKS_AUTH_TYPE", "azure") or "azure").lower(),
            # ARM_* first: inside a Databricks App, DATABRICKS_CLIENT_* belong to the app's own
            # principal, while ARM_* are used when the app should run as RPADLAKEFLOW-PROD.
            client_id=_get("ARM_CLIENT_ID") or _get("DATABRICKS_CLIENT_ID"),
            client_secret=_get("ARM_CLIENT_SECRET") or _get("DATABRICKS_CLIENT_SECRET"),
            tenant_id=_get("ARM_TENANT_ID") or _get("DATABRICKS_TENANT_ID"),
            catalog=_get("RECON_CATALOG", "bfl_std_lake") or "bfl_std_lake",
            schema=_get("RECON_SCHEMA", "financerpa") or "financerpa",
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
