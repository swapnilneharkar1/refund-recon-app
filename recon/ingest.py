"""Reading and normalising the OPS and Finance input files (BRD section 3)."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import pandas as pd

# canonical column -> accepted header spellings (after normalisation: upper case,
# non-alphanumerics collapsed to "_"). First entry is the BRD name.
OPS_COLUMNS: dict[str, list[str]] = {
    "FINREFERENCE": ["FINREFERENCE", "FIN_REFERENCE", "LAN", "LOAN_ACCOUNT_NUMBER"],
    "NPA_STAGEID": ["NPA_STAGE", "NPA_STAGEID", "NPA_STAGE_ID", "STAGE"],
    "SCHDATE": ["SCHDATE", "SCH_DATE", "SCHEDULE_DATE", "SCHEDULED_DATE"],
    "TOTPENALTYAMT_LEVIED": ["TOTPENALTYAMT_LEVIED", "TOT_PENALTY_AMT_LEVIED"],
    "TOTPENALTYAMT_WAIVED": ["TOTPENALTYAMT_WAIVED", "TOT_PENALTY_AMT_WAIVED"],
    "TOTPENALTYAMT_PAID": ["TOTPENALTYAMT_PAID", "TOT_PENALTY_AMT_PAID"],
    "BCC_ADVISEAMOUNT": ["BCC_ADVISEAMOUNT", "BCC_ADVISE_AMOUNT"],
}

FIN_COLUMNS: dict[str, list[str]] = {
    "FINREFERENCE": ["FINREFERENCE", "FIN_REFERENCE", "LAN", "LOAN_ACCOUNT_NUMBER"],
    "NPA_STAGEID": ["NPA_STAGEID", "NPA_STAGE_ID", "NPA_STAGE", "STAGE"],
    "LOAN_STATUS": ["LOAN_STATUS", "LOANSTATUS"],
    "PPG": ["PPG", "PPG_DESC", "PPG_DESCRIPTION"],
    "PRODUCTFLAG": ["PRODUCTFLAG", "PRODUCT_FLAG"],
    "TOTPENALTYAMT": ["TOTPENALTYAMT", "TOT_PENALTY_AMT"],
    "TOTWAIVED": ["TOTWAIVED", "TOT_WAIVED"],
    "TOTPENALTYPAID": ["TOTPENALTYPAID", "TOT_PENALTY_PAID"],
    "ADVISEID": ["ADVISEID", "ADVISE_ID"],
    "ADVISEAMOUNT": ["ADVISEAMOUNT", "ADVISE_AMOUNT"],
    "SCHDATE": ["SCHDATE", "SCH_DATE", "SCHEDULE_DATE", "SCHEDULED_DATE"],
}

# Without these the reconciliation cannot run at all.
OPS_HARD_REQUIRED = ["FINREFERENCE", "SCHDATE", "BCC_ADVISEAMOUNT", "TOTPENALTYAMT_LEVIED"]
FIN_HARD_REQUIRED = ["FINREFERENCE", "SCHDATE", "ADVISEAMOUNT", "TOTPENALTYAMT"]

OPS_AMOUNTS = ["TOTPENALTYAMT_LEVIED", "TOTPENALTYAMT_WAIVED", "TOTPENALTYAMT_PAID", "BCC_ADVISEAMOUNT"]
FIN_AMOUNTS = ["TOTPENALTYAMT", "TOTWAIVED", "TOTPENALTYPAID", "ADVISEAMOUNT"]


def norm_header(h: object) -> str:
    s = re.sub(r"[^0-9A-Za-z]+", "_", str(h).strip()).strip("_").upper()
    return s or "COL"


@dataclass
class PreparedFile:
    side: str                       # "OPS" or "FINANCE"
    df: pd.DataFrame
    mapping: dict[str, str | None]  # canonical -> original header (None if not found)
    missing_required: list[str]
    missing_optional: list[str]
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing_required


def list_sheets(uploaded) -> list[str]:
    name = uploaded.name.lower()
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        uploaded.seek(0)
        return pd.ExcelFile(io.BytesIO(uploaded.getvalue())).sheet_names
    return []


def read_raw(uploaded, sheet: str | None = None) -> pd.DataFrame:
    """Read every column as text so LANs keep leading zeros; types are fixed later."""
    name = uploaded.name.lower()
    data = io.BytesIO(uploaded.getvalue())
    if name.endswith((".csv", ".txt")):
        return pd.read_csv(data, dtype=str, keep_default_na=False, na_values=[""], encoding_errors="replace")
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        try:  # calamine is ~10x faster than openpyxl on large workbooks
            return pd.read_excel(data, sheet_name=sheet or 0, dtype=str, engine="calamine")
        except ImportError:
            data.seek(0)
            return pd.read_excel(data, sheet_name=sheet or 0, dtype=str)
    raise ValueError(f"Unsupported file type: {uploaded.name} (use .xlsx or .csv)")


def _clean_lan(s: pd.Series) -> pd.Series:
    s = s.astype("string").str.strip()
    s = s.str.replace(r"\.0$", "", regex=True)  # 12345.0 -> 12345 if Excel stored it as a number
    return s.replace({"": pd.NA, "nan": pd.NA, "None": pd.NA})


def _clean_amount(s: pd.Series) -> pd.Series:
    s = s.astype("string").str.replace(",", "", regex=False).str.replace("₹", "", regex=False).str.strip()
    s = s.str.replace(r"^\((.*)\)$", r"-\1", regex=True)  # (100) -> -100
    return pd.to_numeric(s, errors="coerce")


def _clean_date(s: pd.Series) -> pd.Series:
    raw = s.astype("string").str.strip()
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    # Excel serial numbers (e.g. 45412)
    num = pd.to_numeric(raw, errors="coerce")
    serial = num.between(20000, 80000).fillna(False).astype(bool)
    if serial.any():
        out.loc[serial] = pd.to_datetime(num[serial], unit="D", origin="1899-12-30")
    # ISO (also what Excel dates look like when read as text: 2024-05-05 00:00:00)
    iso = raw.str.match(r"^\d{4}-\d{2}-\d{2}").fillna(False).astype(bool) & ~serial
    if iso.any():
        out.loc[iso] = pd.to_datetime(raw[iso].str[:10], format="%Y-%m-%d", errors="coerce")
    # anything else, e.g. 05/04/2024 or 05-Apr-2024 -> Indian day-first convention
    other = raw.notna() & ~serial & ~iso
    if other.any():
        out.loc[other] = pd.to_datetime(raw[other], dayfirst=True, errors="coerce", format="mixed")
    return out.dt.normalize()


NOT_MAPPED = "— not in file —"


def prepare(raw: pd.DataFrame, side: str, overrides: dict[str, str] | None = None) -> PreparedFile:
    """Map the file's columns onto the BRD fields.

    Columns are found by *name* (any order, any number of extra columns). `overrides`
    lets the user point a BRD field at a differently named column, or mark it NOT_MAPPED.
    """
    spec, hard, amounts = (
        (OPS_COLUMNS, OPS_HARD_REQUIRED, OPS_AMOUNTS) if side == "OPS"
        else (FIN_COLUMNS, FIN_HARD_REQUIRED, FIN_AMOUNTS)
    )
    raw = raw.dropna(how="all")
    norm_to_orig: dict[str, str] = {}
    for c in raw.columns:
        norm_to_orig.setdefault(norm_header(c), c)

    mapping: dict[str, str | None] = {}
    for canon, aliases in spec.items():
        mapping[canon] = next((norm_to_orig[a] for a in aliases if a in norm_to_orig), None)
    for canon, src in (overrides or {}).items():
        if canon in mapping:
            mapping[canon] = None if src == NOT_MAPPED else (src if src in raw.columns else mapping[canon])

    missing_required = [c for c in hard if mapping[c] is None]
    missing_optional = [c for c in spec if mapping[c] is None and c not in hard]
    if missing_required:
        return PreparedFile(side, raw, mapping, missing_required, missing_optional)

    used = {v for v in mapping.values() if v is not None}
    out = pd.DataFrame(index=raw.index)
    for canon in spec:
        out[canon] = raw[mapping[canon]] if mapping[canon] is not None else pd.NA

    out["FINREFERENCE"] = _clean_lan(out["FINREFERENCE"])
    out["SCHDATE"] = _clean_date(out["SCHDATE"])
    for a in amounts:
        out[a] = _clean_amount(out[a])
    for c in spec:
        if c not in amounts and c not in ("FINREFERENCE", "SCHDATE"):
            out[c] = out[c].astype("string").str.strip()

    # carry every other column through as text, for audit in the raw table
    taken = set(out.columns)
    for c in raw.columns:
        if c in used:
            continue
        n = norm_header(c)
        base, i = n, 1
        while n in taken:
            i += 1
            n = f"{base}_{i}"
        taken.add(n)
        out[n] = raw[c].astype("string")

    out.insert(0, "SOURCE_ROW_NO", range(2, len(out) + 2))  # matches the Excel row number
    out = out.reset_index(drop=True)

    warnings = []
    if (n := int(out["FINREFERENCE"].isna().sum())):
        warnings.append(f"{n:,} row(s) have a blank FINREFERENCE.")
    if (n := int(out["SCHDATE"].isna().sum())):
        warnings.append(f"{n:,} row(s) have a blank or unreadable SCHDATE.")
    for a in amounts:
        bad = raw[mapping[a]].notna().reset_index(drop=True) & out[a].isna() if mapping[a] else None
        if bad is not None and (n := int(bad.sum())):
            warnings.append(f"{n:,} non-numeric value(s) in {a} were treated as blank.")
    dup = int(out.duplicated(["FINREFERENCE", "SCHDATE"], keep=False).sum())
    if dup:
        warnings.append(f"{dup:,} row(s) share a FINREFERENCE + SCHDATE with another row; "
                        f"they are aggregated per key before comparison.")

    # parquet-friendly date objects
    out["SCHDATE"] = out["SCHDATE"].dt.date.astype(object).where(out["SCHDATE"].notna(), None)
    return PreparedFile(side, out, mapping, [], missing_optional, warnings)
