"""DuckDB stand-in for DatabricksBackend – used ONLY by tests/test_local_run.py, never by the app."""
import pandas as pd

DUCKDB_DIALECT = {
    "str_agg": lambda col: f"string_agg(DISTINCT CAST({col} AS VARCHAR), ', ' ORDER BY CAST({col} AS VARCHAR))",
    "now": "current_timestamp",
    "quote": lambda v: str(v).replace("'", "''"),
}


class LocalBackend:
    dialect = DUCKDB_DIALECT

    def __init__(self):
        import duckdb
        self.con = duckdb.connect(":memory:")
        self.log: list[str] = []

    def fqn(self, table): return f'"{table}"'
    def execute(self, sql): self.log.append(sql); self.con.execute(sql)
    def query(self, sql): return self.con.execute(sql).df()
    def whoami(self): return "test-user"
    def prepare_staging(self): return None
    def drop_table(self, table): self.execute(f"DROP TABLE IF EXISTS {self.fqn(table)}")

    def recreate_table(self, table, select_sql):
        self.drop_table(table)
        self.execute(f"CREATE TABLE {self.fqn(table)} AS\n{select_sql}")

    def load_dataframe(self, df, table, run_id):
        tmp = df.copy()
        tmp["SCHDATE"] = pd.to_datetime(tmp["SCHDATE"])
        self.con.register("_incoming", tmp)
        self.recreate_table(table, "SELECT * REPLACE (CAST(SCHDATE AS DATE) AS SCHDATE) FROM _incoming")
        self.con.unregister("_incoming")

    def close(self): self.con.close()
