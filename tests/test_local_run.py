"""End-to-end run of the pipeline on the sample files using the local DuckDB backend."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from recon import ingest, reports  # noqa: E402
sys.path.insert(0, str(ROOT / "tests"))
from local_backend import LocalBackend  # noqa: E402
from recon.logic import Options, run_pipeline  # noqa: E402


class F:
    def __init__(self, p): self.p = Path(p); self.name = self.p.name
    def getvalue(self): return self.p.read_bytes()
    def seek(self, _): pass


def main():
    ops = ingest.prepare(ingest.read_raw(F(ROOT / "sample_data/sample_OPS.xlsx")), "OPS")
    fin = ingest.prepare(ingest.read_raw(F(ROOT / "sample_data/sample_Finance.xlsx")), "FINANCE")
    assert ops.ok and fin.ok, (ops.missing_required, fin.missing_required)
    r = run_pipeline(LocalBackend(), ops.df, fin.df, Options(), "ops", "fin", progress=print)

    got_b = dict(zip(r.bounce["REFERENCE"], r.bounce["REMARK_CODE"]))
    got_p = {(a, str(d)[:10]): c for a, d, c in zip(r.penal["REFERENCE"], r.penal["SCHDATE"], r.penal["REMARK_CODE"])}
    exp_b = {"0401LAP001": 1, "0401LAP002": 2, "0401LAP003": 3, "0401LAP005": 4, "0401LAP004": 5, "0401LAP006": 1}
    exp_p = {("0401LAP001", "2024-04-05"): 6, ("0401LAP002", "2024-04-05"): 7, ("0401LAP003", "2024-05-05"): 8,
             ("0401LAP005", "2024-07-05"): 9, ("0401LAP004", "2024-06-05"): 10, ("0401LAP006", "2024-05-05"): 6}
    assert got_b == exp_b, got_b
    assert got_p == exp_p, got_p
    import openpyxl, io
    for kind, (label, fname, data) in reports.build_reports(r, "ops", "fin", {"mode": "test"}).items():
        (ROOT / "sample_data" / ("sample_" + fname.split("_2")[0] + ".xlsx")).write_bytes(data)
        wb = openpyxl.load_workbook(io.BytesIO(data))
        print(label, "->", {ws.title: ws.max_row - 1 for ws in wb.worksheets if ws.title != "Summary"})
    print("\nBOUNCE\n", reports.primary_bounce(r).iloc[:, :9].to_string(index=False))
    print("\nPENAL\n", reports.primary_penal(r).iloc[:, :9].to_string(index=False))
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
