"""Refund of Bounce & Penal for Excess-Available Cases – OPS vs Finance reconciliation.

Run:  streamlit run app.py
"""
from __future__ import annotations

import traceback
from dataclasses import replace

import pandas as pd
import streamlit as st

from recon import ingest, reports
from recon.config import Settings
from recon.logic import BOUNCE_REMARKS, PENAL_REMARKS, Options, run_pipeline

# Works on both older Streamlit (preinstalled on Databricks Apps) and newer versions.
try:
    _ver = tuple(int(x) for x in st.__version__.split(".")[:2])
except Exception:  # noqa: BLE001
    _ver = (1, 0)
FULL = {"width": "stretch"} if _ver >= (1, 46) else {"use_container_width": True}

st.set_page_config(page_title="Bounce & Penal Refund Recon", page_icon="📊", layout="wide")

st.markdown("""
<style>
  .block-container {padding-top: 1.6rem;}
  .chip {display:inline-block;padding:2px 9px;margin:2px 4px 2px 0;border-radius:12px;font-size:0.78rem;
         font-family:ui-monospace,monospace;}
  .ok   {background:#E6F4EA;color:#1E6B34;}
  .miss {background:#FDE7E7;color:#A12626;}
  .opt  {background:#FFF4DE;color:#8A5A00;}
  div[data-testid="stMetricValue"] {font-size:1.6rem;}
</style>
""", unsafe_allow_html=True)

# --------------------------------------------------------------------------- sidebar
base = Settings.load()

settings = base
missing = settings.missing_for_databricks()
TARGET = f"{settings.catalog}.{settings.schema}"
P = settings.table_prefix
TABLE_LIST = [f"{P}_finance_raw", f"{P}_ops_raw", f"{P}_finance_by_lan_sch", f"{P}_ops_by_lan_sch",
              f"{P}_bounce_recon", f"{P}_penal_recon"]

with st.sidebar:
    st.header("Databricks")
    st.markdown(f"**Target:** `{TARGET}`")
    st.caption("Every run drops these tables and recreates them from the uploaded files:")
    st.markdown("\n".join(f"- `{t}`" for t in TABLE_LIST))
    st.caption(f"Run history is kept in `{P}_run_log`.")
    if missing:
        st.error("Missing settings: " + ", ".join(missing) + ". Check app.yaml / the app's resources.")
    if st.button("Test connection", **FULL, disabled=bool(missing)):
        try:
            from recon.backends import DatabricksBackend
            b = DatabricksBackend(settings)
            who = b.whoami()
            b.close()
            st.success(f"Connected as {who}")
        except Exception as e:  # noqa: BLE001
            st.error(f"Connection failed: {e}")

    st.divider()
    st.caption("Reconciliation options")
    tolerance = st.number_input("Match tolerance (₹)", min_value=0.0, value=0.0, step=0.5,
                                help="Differences up to this amount are treated as Exact Match.")
    penal_agg = st.selectbox(
        "Penal amount when a LAN + SCH date repeats",
        ["SUM", "MAX"],
        help="SUM adds the rows up. Choose MAX if the file repeats the same penal total on every "
             "bounce-advise row of that LAN + SCH date.")

# --------------------------------------------------------------------------- header
st.title("Bounce & Penal Charges Refund – OPS vs Finance Reconciliation")
st.caption("Cases where unadjusted excess ≥ EMI outstanding on the due date. "
           "Compared at FINREFERENCE + SCH date level; remarks per BRD codes 1–10.")


def current_user() -> str | None:
    """Logged-in user's email when running as a Databricks App (its proxy adds this header)."""
    try:
        h = st.context.headers
        return h.get("X-Forwarded-Email") or h.get("X-Forwarded-Preferred-Username")
    except Exception:  # noqa: BLE001
        return None


@st.cache_data(show_spinner=False)
def _read(name: str, data: bytes, sheet: str | None) -> pd.DataFrame:
    class _F:  # minimal stand-in for UploadedFile so the cache key is the bytes
        def __init__(self): self.name = name
        def getvalue(self): return data
        def seek(self, _): pass
    return ingest.read_raw(_F(), sheet)


def file_panel(col, side: str, label: str, spec: dict, hard: list[str]):
    with col:
        st.subheader(label)
        up = st.file_uploader(f"{label} (.xlsx / .csv)", type=["xlsx", "xlsm", "xls", "csv"],
                              key=f"up_{side}", label_visibility="collapsed")
        if not up:
            st.markdown("Expected columns: " + "".join(
                f"<span class='chip {'miss' if c in hard else 'opt'}'>{c}</span>" for c in spec),
                unsafe_allow_html=True)
            return None, None
        sheet = None
        sheets = ingest.list_sheets(up)
        if len(sheets) > 1:
            sheet = st.selectbox("Sheet", sheets, key=f"sheet_{side}")
        try:
            with st.spinner(f"Reading {up.name}…"):
                raw = _read(up.name, up.getvalue(), sheet)
            auto = ingest.prepare(raw, side)
        except Exception as e:  # noqa: BLE001
            st.error(f"Could not read file: {e}")
            return None, None

        # Column mapping: auto-detected by name; user can re-point any field if headers changed.
        cols = [str(c) for c in raw.columns]
        opts = [ingest.NOT_MAPPED] + cols
        overrides = {}
        with st.expander(f"Column mapping ({len(cols)} columns in file)",
                         expanded=bool(auto.missing_required)):
            st.caption("Fields are matched by column name, so column order and extra columns don't matter. "
                       "If a header was renamed, pick the right column here. Extra columns are kept as-is "
                       "in the Databricks raw table.")
            mc = st.columns(2)
            for i, c in enumerate(spec):
                found = auto.mapping.get(c)
                req = " *" if c in hard else ""
                pick = mc[i % 2].selectbox(f"{c}{req}", opts, index=opts.index(found) if found in opts else 0,
                                           key=f"map_{side}_{up.name}_{c}")
                if pick != (found or ingest.NOT_MAPPED):
                    overrides[c] = pick
            st.caption("Fields marked \\* are required.")
        prep = ingest.prepare(raw, side, overrides) if overrides else auto

        chips = []
        for c in spec:
            src = prep.mapping.get(c)
            cls = "ok" if src else ("miss" if c in hard else "opt")
            tip = f"from '{src}'" if src else "not found"
            chips.append(f"<span class='chip {cls}' title=\"{tip}\">{'✓' if src else '✗'} {c}</span>")
        st.markdown("".join(chips), unsafe_allow_html=True)

        if prep.missing_required:
            other_side, other_label = ("OPS", "OPS") if side == "FINANCE" else ("FINANCE", "Finance")
            if not overrides and ingest.prepare(raw, other_side).ok:
                st.error(f"This looks like the **{other_label}** file – please upload it in the "
                         f"{other_label} box instead.")
            else:
                st.error("Required column(s) not found: " + ", ".join(prep.missing_required) +
                         ". Map them in **Column mapping** above.")
        else:
            st.caption(f"{len(prep.df):,} rows · {prep.df['FINREFERENCE'].nunique():,} LANs")
            if prep.missing_optional:
                st.warning("Optional column(s) not found, left blank: " + ", ".join(prep.missing_optional))
            for w in prep.warnings:
                st.info(w)
            with st.expander("Preview (cleaned)"):
                st.dataframe(prep.df.head(50), **FULL, hide_index=True)
        return up, prep


c1, c2 = st.columns(2, gap="large")
fin_up, fin_prep = file_panel(c1, "FINANCE", "1. Finance file", ingest.FIN_COLUMNS, ingest.FIN_HARD_REQUIRED)
ops_up, ops_prep = file_panel(c2, "OPS", "2. OPS file", ingest.OPS_COLUMNS, ingest.OPS_HARD_REQUIRED)

ready = bool(ops_prep and ops_prep.ok and fin_prep and fin_prep.ok and not missing)
st.divider()
go = st.button("▶ Execute reconciliation", type="primary", disabled=not ready, **FULL)
if not ready:
    st.caption("Upload both files (1. Finance, 2. OPS) with all required columns to enable Execute.")

# --------------------------------------------------------------------------- run
if go:
    opt = Options(tolerance=float(tolerance), penal_agg=penal_agg, table_prefix=P)
    backend = None
    with st.status("Running reconciliation…", expanded=True) as status:
        try:
            from recon.backends import DatabricksBackend
            status.write("Connecting to Databricks SQL warehouse")
            backend = DatabricksBackend(replace(settings))
            result = run_pipeline(backend, ops_prep.df, fin_prep.df, opt,
                                  ops_name=ops_up.name, fin_name=fin_up.name, run_by=current_user(),
                                  progress=status.write)
            status.write("Building the 3 Excel reports")
            where = TARGET
            xlsx = reports.build_reports(result, ops_up.name, fin_up.name, {
                "Ran against": where,
                "Match tolerance (₹)": tolerance,
                "Penal aggregation for repeated keys": opt.penal_agg,
            })
            st.session_state["result"] = (result, xlsx, where, ops_up.name, fin_up.name)
            status.update(label=f"Done – run {result.run_id}", state="complete", expanded=False)
        except Exception as e:  # noqa: BLE001
            status.update(label="Run failed", state="error", expanded=True)
            st.error(f"{type(e).__name__}: {e}")
            with st.expander("Details"):
                st.code(traceback.format_exc())
        finally:
            if backend is not None:
                backend.close()

# --------------------------------------------------------------------------- results
if "result" in st.session_state:
    result, xlsx, where, ops_name, fin_name = st.session_state["result"]
    st.header("Results")
    st.caption(f"Run `{result.run_id}` · {where} · tables: "
               + ", ".join(f"`{v}`" for k, v in result.tables.items() if k != "log"))

    def metric_row(df: pd.DataFrame, codes: list[int], title: str):
        n = len(df)
        cnt = df["REMARK_CODE"].value_counts()
        exact, less, more, fin_only, ops_only = (int(cnt.get(c, 0)) for c in codes)
        st.markdown(f"**{title}**")
        m = st.columns(5)
        m[0].metric("Cases", f"{n:,}")
        m[1].metric("Exact match", f"{exact:,}")
        m[1].caption(f"{exact / n:.1%} of cases" if n else "")
        m[2].metric("Amount mismatch", f"{less + more:,}")
        m[2].caption(f"Finance lower: {less:,} · Finance higher: {more:,}")
        m[3].metric("Only in Finance", f"{fin_only:,}")
        m[4].metric("Only in OPS", f"{ops_only:,}")

    metric_row(result.bounce, [1, 2, 3, 4, 5], "Bounce charges")
    metric_row(result.penal, [6, 7, 8, 9, 10], "Penal charges")

    st.subheader("Download reports")
    sheet_desc = {
        "primary": "Summary · Bounce_Recon · Penal_Recon (all cases, remarks 1–10)",
        "exceptions": "Summary · Bounce_Amount_Diff · Penal_Amount_Diff (remarks 2, 3, 7, 8)",
        "unmatched": "Summary · Bounce_Unmatched · Penal_Unmatched (remarks 4, 5, 9, 10)",
    }
    dl = st.columns(3)
    for col, (kind, (label, fname, data)) in zip(dl, xlsx.items()):
        with col:
            st.download_button(f"⬇ {label}", data, file_name=fname, type="primary", **FULL,
                               key=f"dl_{kind}",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            st.caption(sheet_desc[kind])

    views = {
        "Summary": reports.summary_table(result),
        "Bounce recon": reports.primary_bounce(result),
        "Penal recon": reports.primary_penal(result),
        "Exceptions – amount differences": reports.exceptions_amount_diff(result),
        "Unmatched records": reports.unmatched_records(result),
    }
    tabs = st.tabs(list(views))
    for tab, (name, df) in zip(tabs, views.items()):
        with tab:
            show = df
            if name in ("Bounce recon", "Penal recon"):
                labels = BOUNCE_REMARKS if name == "Bounce recon" else PENAL_REMARKS
                pick = st.multiselect("Filter remarks", [f"{k} – {v}" for k, v in labels.items()],
                                      key=f"f_{name}")
                if pick:
                    show = df[df["Remark Code"].isin([int(p.split(" – ")[0]) for p in pick])]
            st.caption(f"{len(show):,} rows")
            st.dataframe(show, **FULL, hide_index=True, height=420)
            if name != "Summary":
                st.download_button(f"CSV – {name}", show.to_csv(index=False).encode("utf-8-sig"),
                                   file_name=f"{name.replace(' ', '_').replace('–', '')}_{result.run_id}.csv",
                                   mime="text/csv", key=f"csv_{name}")
