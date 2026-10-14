"""Reconciliation pipeline (BRD sections 2 and 4).

Steps
  1. Load OPS and Finance files into raw tables.
  2. Aggregate each side to one row per FINREFERENCE + SCHDATE (REC_KEY).
  3. Bounce recon:  BCC_ADVISEAMOUNT (OPS) vs ADVISEAMOUNT (Finance)   -> remarks 1-5
  4. Penal recon:   TOTPENALTYAMT_LEVIED (OPS) vs TOTPENALTYAMT (Fin)  -> remarks 6-10
  5. Pull results back for the reports, and write a run-log row.
All heavy lifting runs as SQL inside the backend (the SQL warehouse in production).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

import pandas as pd

BOUNCE_REMARKS = {
    1: "Exact Match",
    2: "Refund as per Finance is less than OPS",
    3: "Refund as per Finance is greater than OPS",
    4: "Refund in Finance but not in OPS",
    5: "Refund in OPS but not in Finance",
}
PENAL_REMARKS = {
    6: "Exact Match",
    7: "Refund as per Finance is less than OPS",
    8: "Refund as per Finance is greater than OPS",
    9: "Refund in Finance but not in OPS",
    10: "Refund in OPS but not in Finance",
}
AMOUNT_DIFF_CODES = {2, 3, 7, 8}
UNMATCHED_CODES = {4, 5, 9, 10}


@dataclass
class Options:
    tolerance: float = 0.0          # |Finance - OPS| <= tolerance counts as Exact Match
    penal_agg: str = "SUM"          # how repeated rows for one key are combined for penal amounts: SUM or MAX
    per_run_tables: bool = False    # suffix table names with the run id instead of replacing
    table_prefix: str = "bp_refund"


@dataclass
class RunResult:
    run_id: str
    tables: dict[str, str]
    bounce: pd.DataFrame
    penal: pd.DataFrame
    summary: pd.DataFrame
    stats: dict


def _case(codes: dict[int, str], col: str = "REMARK_CODE") -> str:
    whens = " ".join(f"WHEN {k} THEN '{v}'" for k, v in codes.items())
    return f"CASE {col} {whens} END"


def _recon_sql(out: str, agg_ops: str, agg_fin: str, amt: str, codes: list[int],
               remarks: dict[int, str], tol: float, extra_o: list[str], extra_f: list[str]) -> str:
    exact, less, greater, fin_only, ops_only = codes
    extra = ",\n        ".join([f"o.{c} AS {c}_OPS" for c in extra_o] + [f"f.{c} AS {c}_FIN" for c in extra_f])
    extra = (",\n        " + extra) if extra else ""
    return f"""
CREATE OR REPLACE TABLE {out} AS
WITH o AS (SELECT * FROM {agg_ops} WHERE COALESCE({amt}, 0) <> 0),
     f AS (SELECT * FROM {agg_fin} WHERE COALESCE({amt}, 0) <> 0),
     j AS (
  SELECT COALESCE(f.REC_KEY, o.REC_KEY)           AS REC_KEY,
         COALESCE(f.FINREFERENCE, o.FINREFERENCE) AS REFERENCE,
         COALESCE(f.SCHDATE, o.SCHDATE)           AS SCHDATE,
         COALESCE(f.NPA_STAGEID, o.NPA_STAGEID)   AS NPA_STAGEID,
         f.PPG                                     AS PPG_DESC,
         o.{amt}                                   AS AMOUNT_OPS,
         f.{amt}                                   AS AMOUNT_FINANCE,
         o.REC_KEY IS NOT NULL                     AS IN_OPS,
         f.REC_KEY IS NOT NULL                     AS IN_FINANCE,
         f.LOAN_STATUS, f.PRODUCTFLAG,
         o.NPA_STAGEID                             AS NPA_STAGE_OPS{extra}
  FROM o FULL OUTER JOIN f ON o.REC_KEY = f.REC_KEY
), c AS (
  SELECT j.*,
         CAST(ROUND(COALESCE(AMOUNT_OPS, 0) - COALESCE(AMOUNT_FINANCE, 0), 2) AS DECIMAL(18,2)) AS DIFFERENCE,
         CASE
           WHEN NOT IN_OPS THEN {fin_only}
           WHEN NOT IN_FINANCE THEN {ops_only}
           WHEN ABS(AMOUNT_FINANCE - AMOUNT_OPS) <= {tol} THEN {exact}
           WHEN AMOUNT_FINANCE < AMOUNT_OPS THEN {less}
           ELSE {greater}
         END AS REMARK_CODE
  FROM j
)
SELECT c.*, {_case(remarks)} AS REMARKS
FROM c
"""


def run_pipeline(backend, ops_df: pd.DataFrame, fin_df: pd.DataFrame, opt: Options,
                 ops_name: str = "", fin_name: str = "",
                 progress: Callable[[str], None] = lambda m: None) -> RunResult:
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    sfx = f"_{run_id}" if opt.per_run_tables else ""
    p = opt.table_prefix
    names = {
        "raw_ops": f"{p}_ops_raw{sfx}",
        "raw_fin": f"{p}_finance_raw{sfx}",
        "agg_ops": f"{p}_ops_by_lan_sch{sfx}",
        "agg_fin": f"{p}_finance_by_lan_sch{sfx}",
        "bounce": f"{p}_bounce_recon{sfx}",
        "penal": f"{p}_penal_recon{sfx}",
        "log": f"{p}_run_log",
    }
    t = {k: backend.fqn(v) for k, v in names.items()}
    agg = "MAX" if opt.penal_agg.upper() == "MAX" else "SUM"
    str_agg = backend.dialect["str_agg"]

    progress("Preparing schema")
    backend.ensure_schema()

    progress(f"Uploading OPS data ({len(ops_df):,} rows) → {names['raw_ops']}")
    backend.load_dataframe(ops_df, names["raw_ops"], run_id)
    progress(f"Uploading Finance data ({len(fin_df):,} rows) → {names['raw_fin']}")
    backend.load_dataframe(fin_df, names["raw_fin"], run_id)

    key = "CONCAT(FINREFERENCE, '_', COALESCE(CAST(SCHDATE AS STRING), 'NO_DATE'))"
    amt = lambda f, c: f"CAST(ROUND({f}({c}), 2) AS DECIMAL(18,2))"  # noqa: E731

    progress("Aggregating to LAN + SCH date level")
    backend.execute(f"""
CREATE OR REPLACE TABLE {t['agg_ops']} AS
SELECT {key} AS REC_KEY, FINREFERENCE, SCHDATE,
       MAX(NPA_STAGEID)                          AS NPA_STAGEID,
       {amt('SUM', 'BCC_ADVISEAMOUNT')}          AS BOUNCE_AMT,
       {amt(agg, 'TOTPENALTYAMT_LEVIED')}        AS PENAL_AMT,
       {amt(agg, 'TOTPENALTYAMT_WAIVED')}        AS PENAL_WAIVED,
       {amt(agg, 'TOTPENALTYAMT_PAID')}          AS PENAL_PAID,
       COUNT(*)                                  AS ROW_COUNT
FROM {t['raw_ops']}
WHERE FINREFERENCE IS NOT NULL
GROUP BY FINREFERENCE, SCHDATE""")
    backend.execute(f"""
CREATE OR REPLACE TABLE {t['agg_fin']} AS
SELECT {key} AS REC_KEY, FINREFERENCE, SCHDATE,
       MAX(NPA_STAGEID)                          AS NPA_STAGEID,
       MAX(PPG)                                  AS PPG,
       MAX(LOAN_STATUS)                          AS LOAN_STATUS,
       MAX(PRODUCTFLAG)                          AS PRODUCTFLAG,
       {str_agg('ADVISEID')}                     AS ADVISEIDS,
       {amt('SUM', 'ADVISEAMOUNT')}              AS BOUNCE_AMT,
       {amt(agg, 'TOTPENALTYAMT')}               AS PENAL_AMT,
       {amt(agg, 'TOTWAIVED')}                   AS PENAL_WAIVED,
       {amt(agg, 'TOTPENALTYPAID')}              AS PENAL_PAID,
       COUNT(*)                                  AS ROW_COUNT
FROM {t['raw_fin']}
WHERE FINREFERENCE IS NOT NULL
GROUP BY FINREFERENCE, SCHDATE""")

    progress("Running Bounce charges reconciliation (remarks 1–5)")
    backend.execute(_recon_sql(t["bounce"], t["agg_ops"], t["agg_fin"], "BOUNCE_AMT",
                               [1, 2, 3, 4, 5], BOUNCE_REMARKS, opt.tolerance,
                               extra_o=["ROW_COUNT"], extra_f=["ADVISEIDS", "ROW_COUNT"]))
    progress("Running Penal charges reconciliation (remarks 6–10)")
    backend.execute(_recon_sql(t["penal"], t["agg_ops"], t["agg_fin"], "PENAL_AMT",
                               [6, 7, 8, 9, 10], PENAL_REMARKS, opt.tolerance,
                               extra_o=["PENAL_WAIVED", "PENAL_PAID"],
                               extra_f=["PENAL_WAIVED", "PENAL_PAID"]))

    progress("Fetching results")
    bounce = backend.query(f"SELECT * FROM {t['bounce']} ORDER BY REMARK_CODE, REFERENCE, SCHDATE")
    penal = backend.query(f"SELECT * FROM {t['penal']} ORDER BY REMARK_CODE, REFERENCE, SCHDATE")
    summary = backend.query(f"""
SELECT 'Bounce' AS CHARGE_TYPE, REMARK_CODE, REMARKS, COUNT(*) AS CASES,
       SUM(AMOUNT_OPS) AS AMOUNT_OPS, SUM(AMOUNT_FINANCE) AS AMOUNT_FINANCE, SUM(DIFFERENCE) AS DIFFERENCE
FROM {t['bounce']} GROUP BY REMARK_CODE, REMARKS
UNION ALL
SELECT 'Penal', REMARK_CODE, REMARKS, COUNT(*),
       SUM(AMOUNT_OPS), SUM(AMOUNT_FINANCE), SUM(DIFFERENCE)
FROM {t['penal']} GROUP BY REMARK_CODE, REMARKS
ORDER BY REMARK_CODE""")
    keys = backend.query(f"""
SELECT (SELECT COUNT(*) FROM {t['agg_ops']}) AS OPS_KEYS,
       (SELECT COUNT(*) FROM {t['agg_fin']}) AS FIN_KEYS,
       (SELECT COUNT(*) FROM {t['agg_ops']} WHERE ROW_COUNT > 1) AS OPS_REPEATED_KEYS,
       (SELECT COUNT(*) FROM {t['agg_fin']} WHERE ROW_COUNT > 1) AS FIN_REPEATED_KEYS""").iloc[0]

    for df in (bounce, penal, summary):
        df["REMARK_CODE"] = df["REMARK_CODE"].astype(int)
        for c in df.columns:
            if c.startswith(("AMOUNT_", "DIFFERENCE", "PENAL_")):
                df[c] = pd.to_numeric(df[c], errors="coerce")

    stats = {
        "ops_rows": len(ops_df), "fin_rows": len(fin_df),
        "ops_keys": int(keys["OPS_KEYS"]), "fin_keys": int(keys["FIN_KEYS"]),
        "ops_repeated_keys": int(keys["OPS_REPEATED_KEYS"]),
        "fin_repeated_keys": int(keys["FIN_REPEATED_KEYS"]),
        "bounce_cases": len(bounce), "penal_cases": len(penal),
        "user": backend.whoami(),
    }

    progress("Writing run log")
    backend.execute(f"""
CREATE TABLE IF NOT EXISTS {t['log']} (
  RUN_ID STRING, RUN_TS TIMESTAMP, RUN_BY STRING, OPS_FILE STRING, FINANCE_FILE STRING,
  OPS_ROWS BIGINT, FINANCE_ROWS BIGINT, BOUNCE_CASES BIGINT, PENAL_CASES BIGINT,
  BOUNCE_TABLE STRING, PENAL_TABLE STRING, TOLERANCE DOUBLE, PENAL_AGG STRING)""")
    esc = backend.dialect["quote"]
    backend.execute(f"""
INSERT INTO {t['log']} VALUES ('{run_id}', {backend.dialect['now']}, '{esc(stats['user'])}',
  '{esc(ops_name)}', '{esc(fin_name)}', {len(ops_df)}, {len(fin_df)}, {len(bounce)}, {len(penal)},
  '{names['bounce']}', '{names['penal']}', {float(opt.tolerance)}, '{agg}')""")

    return RunResult(run_id, names, bounce, penal, summary, stats)
