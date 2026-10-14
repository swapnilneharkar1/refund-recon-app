# Bounce & Penal Charges Refund – OPS vs Finance Reconciliation (Databricks App)

Implements the BRD *"Refund of Bounce and Penal for Excess available cases"*.
Runs entirely inside Databricks: the app on **Databricks Apps**, all tables and logic on the **SQL warehouse**
in **`bfl_std_lake.financerpa`**.

## What happens when the user clicks Execute
1. **Drop** the previous run's tables.
2. **Create** `bp_refund_finance_raw` and `bp_refund_ops_raw` from the uploaded files
   (file staged as parquet in volume `bfl_std_lake.financerpa.recon_uploads`, then deleted;
   if the volume can't be used, rows are inserted directly).
3. **Create** `bp_refund_finance_by_lan_sch` / `bp_refund_ops_by_lan_sch` – one row per FINREFERENCE + SCHDATE.
4. **Create** `bp_refund_bounce_recon` – `ADVISEAMOUNT` (Finance) vs `BCC_ADVISEAMOUNT` (OPS), remarks 1–5.
5. **Create** `bp_refund_penal_recon` – `TOTPENALTYAMT` (Finance) vs `TOTPENALTYAMT_LEVIED` (OPS), remarks 6–10.
6. Append one row to `bp_refund_run_log` (who, when, files, counts) – the only table kept across runs.
7. Three Excel downloads: **Primary Reconciliation Report**, **Exception Report – Amount Differences**
   (2, 3, 7, 8), **Unmatched Records Report** (4, 5, 9, 10). Each has a Summary sheet + Bounce and Penal sheets.

Remarks: 1/6 Exact Match · 2/7 Finance < OPS · 3/8 Finance > OPS · 4/9 only in Finance · 5/10 only in OPS.
`Difference = Amount (Ops) − Amount (Finance)`.

**Input files** – 1. Finance, 2. OPS (.xlsx or .csv). Columns are found by name, so order / extra / missing
optional columns don't matter; renamed headers can be mapped in the **Column mapping** panel.

### Interpretation choices (confirm with the business)
1. A LAN + SCH date is "in" a file for a charge type when its amount for that charge is non-zero.
2. Repeated LAN + SCH date rows: bounce amounts summed; penal summed by default (sidebar: switch to MAX
   if Finance repeats the penal total on every advise row).
3. Excess ≥ EMI O/s eligibility is assumed already applied in both input files.

## Deploy (Databricks Apps)
1. Workspace → **Create → Git folder** → `https://github.com/swapnilneharkar1/refund-recon-app`.
2. **Compute → Apps → Create app → Custom app**. Resource: **SQL warehouse** `0e207ff8aa2fb4ba`,
   permission *Can use*, key **`sql-warehouse`**.
3. **Deploy** from the Git folder. For updates: pull in the Git folder → Deploy again.
4. Grant the app's service principal (app page → Authorization) access – run as an admin:
   ```sql
   GRANT USE CATALOG ON CATALOG bfl_std_lake TO `<app-sp-application-id>`;
   GRANT USE SCHEMA, CREATE TABLE, SELECT, MODIFY ON SCHEMA bfl_std_lake.financerpa TO `<app-sp-application-id>`;
   -- staging volume (recommended for big files):
   CREATE VOLUME IF NOT EXISTS bfl_std_lake.financerpa.recon_uploads;
   GRANT READ VOLUME, WRITE VOLUME ON VOLUME bfl_std_lake.financerpa.recon_uploads TO `<app-sp-application-id>`;
   ```
   To run as **RPADLAKEFLOW-PROD** instead (if it already has these rights), see the commented block in `app.yaml`.
5. App page → **Permissions** → give users *Can use*.

## Layout
```
app.py              Streamlit UI (Databricks App)
app.yaml            Databricks App config: target bfl_std_lake.financerpa, warehouse resource
recon/config.py     settings from app.yaml env
recon/ingest.py     file reading, column mapping, cleaning
recon/backends.py   Databricks SQL warehouse: drop/create tables, volume staging
recon/logic.py      reconciliation SQL + remark rules
recon/reports.py    the 3 Excel reports
tests/              sample data + offline test (pip install -r tests/requirements-test.txt; python tests/test_local_run.py)
```
