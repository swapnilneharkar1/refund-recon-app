"""Create small OPS / Finance sample files that hit every remark code (1-10)."""
from pathlib import Path

import pandas as pd

out = Path(__file__).resolve().parent.parent / "sample_data"
out.mkdir(exist_ok=True)

ops = pd.DataFrame([
    # LAN,           NPA Stage,   SCHDATE,      levied, waived, paid, BCC advise
    ("0401LAP001", "Regular",   "05-04-2024",  500,   0,   0,  590),   # B1 exact, P6 exact
    ("0401LAP002", "Regular",   "05-04-2024",  700,   0,   0,  590),   # B2 fin less, P7 fin less
    ("0401LAP003", "Repo",      "05-05-2024",  300,  50,   0,  500),   # B3 fin greater, P8 fin greater
    ("0401LAP004", "Write off", "05-06-2024",  250,   0, 250,  590),   # B5 only OPS, P10 only OPS
    ("0401LAP006", "Regular",   "05-04-2024",    0,   0,   0, 1180),   # bounce exact (2 advises in Fin)
    ("0401LAP006", "Regular",   "05-05-2024",  120,   0,   0,    0),   # same LAN, other SCH date: penal only
])
ops.columns = ["FINREFERENCE", "NPA Stage", "SCHDATE", "TOTPENALTYAMT_LEVIED",
               "TOTPENALTYAMT_WAIVED", "TOTPENALTYAMT_PAID", "BCC_ADVISEAMOUNT"]

fin = pd.DataFrame([
    ("0401LAP001", "Regular",   "A", "PPG-HL", "HL", 500,  0,   0, "ADV1001",  590, "2024-04-05"),
    ("0401LAP002", "Regular",   "A", "PPG-HL", "HL", 650,  0,   0, "ADV1002",  500, "2024-04-05"),
    ("0401LAP003", "Repo",      "A", "PPG-LAP", "LAP", 350, 0,  0, "ADV1003",  600, "2024-05-05"),
    ("0401LAP005", "Settle",    "C", "PPG-PL", "PL", 400,  0,   0, "ADV1005",  590, "2024-07-05"),  # B4, P9
    ("0401LAP006", "Regular",   "A", "PPG-PL", "PL",   0,  0,   0, "ADV1006",  590, "2024-04-05"),
    ("0401LAP006", "Regular",   "A", "PPG-PL", "PL",   0,  0,   0, "ADV1007",  590, "2024-04-05"),
    ("0401LAP006", "Regular",   "A", "PPG-PL", "PL", 120,  0,   0, None,         0, "2024-05-05"),
], columns=["FINREFERENCE", "NPA_STAGEID", "LOAN_STATUS", "PPG", "PRODUCTFLAG", "TOTPENALTYAMT",
            "TOTWAIVED", "TOTPENALTYPAID", "ADVISEID", "ADVISEAMOUNT", "SCHDATE"])
fin["SCHDATE"] = pd.to_datetime(fin["SCHDATE"])  # real Excel dates on the Finance side

ops.to_excel(out / "sample_OPS.xlsx", index=False)
fin.to_excel(out / "sample_Finance.xlsx", index=False)
print("written to", out)
