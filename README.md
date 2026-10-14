# Bounce & Penal Charges Refund – OPS vs Finance Reconciliation (Streamlit + Databricks)

Implements the BRD *"Refund of Bounce and Penal for Excess available cases"*.

User flow: upload **1. Finance** file and **2. OPS** file → click **Execute** → the app
loads both into Databricks tables, runs the reconciliation as SQL on the warehouse,
and shows/downloads the reports.

## What it does

| Step | Where | Detail |
|---|---|---|
| Read & clean | app | Columns are found **by name, not position**: column order, extra columns and missing optional columns are all fine (extra columns are kept in the raw table). If a header is renamed, the **Column mapping** panel lets the user point the field at the right column. A file put in the wrong upload box is detected. Column names matched case/space-insensitively (`NPA Stage` → `NPA_STAGEID`). LANs kept as text (leading zeros safe). Dates: Excel dates, `yyyy-mm-dd`, or day-first `dd-mm-yyyy`. Amounts: commas/₹ stripped. |
| Load | Databricks | `<prefix>_ops_raw`, `<prefix>_finance_raw` (all uploaded columns kept for audit). Staged as parquet in a UC volume, then `CREATE OR REPLACE TABLE … AS SELECT`. |
| Aggregate | Databricks | One row per **FINREFERENCE + SCHDATE** (`REC_KEY`, BRD §2.3) → `<prefix>_ops_by_lan_sch`, `<prefix>_finance_by_lan_sch`. |
| Bounce recon | Databricks | `BCC_ADVISEAMOUNT` (OPS) vs `ADVISEAMOUNT` (Finance) → remarks 1–5 → `<prefix>_bounce_recon` |
| Penal recon | Databricks | `TOTPENALTYAMT_LEVIED` (OPS) vs `TOTPENALTYAMT` (Finance) → remarks 6–10 → `<prefix>_penal_recon` |
| Log | Databricks | one row per run in `<prefix>_run_log` (who, when, files, counts) |
| Reports | app | Three separate Excel downloads (BRD §5): **Primary Reconciliation Report** (all cases), **Exception Report – Amount Differences** (remarks 2,3,7,8), **Unmatched Records Report** (4,5,9,10). Each has a Summary sheet plus separate Bounce and Penal sheets. CSV per tab too. |

Remarks: 1/6 Exact Match · 2/7 Finance < OPS · 3/8 Finance > OPS · 4/9 only in Finance · 5/10 only in OPS.
`Difference = Amount (Ops) − Amount (Finance)`.

### Interpretation choices (please confirm with the business)
1. **"Exists in file"** for a charge type means the LAN+SCH date has a **non-zero** amount for that charge.
   So a key with a bounce amount but zero penal appears only in the bounce recon.
2. **Repeated LAN+SCH date rows**: bounce advise amounts are **summed** (several advises per date).
   Penal amounts are summed by default; switch the sidebar option to **MAX** if Finance repeats the
   same penal total on every advise row.
3. The excess ≥ EMI O/s eligibility (Regular via LVD, other stages via Ops data) is assumed to be
   **already applied** in both uploaded computation files — the BRD's mandatory fields carry no
   excess/EMI columns. If you want the app to apply it, add those columns and the rule can go into `logic.py`.
4. `NPA_STAGEID` shown is Finance's, falling back to OPS'; OPS' own stage is kept in `NPA_STAGE_OPS`.

## Setup

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # fill in host, warehouse, secret
streamlit run app.py
```

Use **Local test** mode in the sidebar to try files without Databricks (`sample_data/` has a set
that hits all 10 remark codes; `python tests/test_local_run.py` checks them).

### Service principal (RPADLAKEFLOW-PROD) permissions
The SP authenticates with Entra ID client-credentials (`DATABRICKS_AUTH_TYPE = "azure"`). In Databricks it needs:
- to be added to the workspace (Admin → Identity → Service principals, using the Application ID)
- **CAN USE** on the SQL warehouse
- `USE CATALOG` on the catalog; `USE SCHEMA`, `CREATE TABLE`, `CREATE VOLUME` (or an existing volume with
  `READ VOLUME`/`WRITE VOLUME`) on the schema — or have an admin pre-create the schema and volume.

```sql
GRANT USE CATALOG ON CATALOG main TO `6fb42e7e-346b-4c3b-b640-78138109275f`;
GRANT USE SCHEMA, CREATE TABLE, CREATE VOLUME ON SCHEMA main.refund_recon TO `6fb42e7e-346b-4c3b-b640-78138109275f`;
GRANT READ VOLUME, WRITE VOLUME ON VOLUME main.refund_recon.recon_uploads TO `6fb42e7e-346b-4c3b-b640-78138109275f`;
```

No volume? Set `RECON_VOLUME = ""` — rows are inserted in batches instead (fine up to tens of thousands of rows).

### Deploying as a Databricks App (optional)
`app.yaml` is included. Add the SQL warehouse as an app resource named `sql-warehouse`; the app's own SP is
used automatically (`DATABRICKS_AUTH_TYPE=default`), so grant the permissions above to that SP instead.

**Never commit `.streamlit/secrets.toml`** (it's in `.gitignore`). In production prefer a secret store
(Databricks secret scope, Azure Key Vault, or the hosting platform's env vars).

## Layout
```
app.py                 Streamlit UI
recon/config.py        settings from secrets/env
recon/ingest.py        file reading, column mapping, cleaning
recon/backends.py      Databricks (SP auth, volume upload) and local DuckDB backends
recon/logic.py         reconciliation SQL + remark rules
recon/reports.py       report shaping + Excel export
tests/                 sample-data generator + end-to-end check
```
