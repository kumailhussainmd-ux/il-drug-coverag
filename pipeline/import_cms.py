"""Load the CMS Part D quarterly public use file (SPUF), Illinois plans only, into Supabase.
Env: SUPABASE_DB_URL (required unless DRY_RUN=1), SPUF_URL or SPUF_ZIP (local path)."""
import os, sys, glob, zipfile, urllib.request
import duckdb

WORK = os.environ.get("WORK_DIR", "work")
os.makedirs(WORK, exist_ok=True)

def get_zip():
    local = os.environ.get("SPUF_ZIP")
    if local: return local
    url = os.environ["SPUF_URL"]
    dest = os.path.join(WORK, "spuf.zip")
    print("Downloading", url); urllib.request.urlretrieve(url, dest)
    return dest

def unpack(path):
    out = os.path.join(WORK, "spuf")
    with zipfile.ZipFile(path) as z: z.extractall(out)
    for inner in glob.glob(out + "/**/*.zip", recursive=True):  # unpack nested zips too
        with zipfile.ZipFile(inner) as z: z.extractall(os.path.dirname(inner))
    return [f for f in glob.glob(out + "/**/*", recursive=True)
            if os.path.isfile(f) and not f.lower().endswith(".zip")]

def find(files, must, nope=(), many=False):
    hit = [f for f in files if all(k in os.path.basename(f).lower() for k in must)
           and not any(k in os.path.basename(f).lower() for k in nope)
           and "sample" not in f.lower()]
    if not hit or (len(hit) != 1 and not many):
        sys.exit(f"Expected exactly one file matching {must}, found {hit}.\nAll files:\n" + "\n".join(files))
    return sorted(hit) if many else hit[0]

def main():
    files = unpack(get_zip())
    src = {"p": find(files, ["plan", "information"]), "f": find(files, ["basic", "drugs"]),
           "c": find(files, ["beneficiary", "cost"], ["insulin"]), "n": find(files, ["pharmacy", "network"], many=True),
           "pr": find(files, ["pricing"]), "i": find(files, ["insulin"])}
    print("Using files:", src)
    db = duckdb.connect(os.path.join(WORK, "build.duckdb"))
    for k, paths in src.items():  # read everything as text, whitespace trimmed; some files come in several parts
        paths = paths if isinstance(paths, list) else [paths]
        opts = "delim='|', all_varchar=true, quote=''"
        names = [d[0] for d in db.execute(f"SELECT * FROM read_csv('{paths[0]}', header=true, {opts}) LIMIT 0").description]
        plist = "[" + ", ".join(f"'{p}'" for p in paths) + "]"
        db.execute(f"""CREATE OR REPLACE TABLE {k}_raw AS
          SELECT trim(COLUMNS(*)) FROM read_csv({plist}, header=false, names={names!r}, {opts})
          WHERE upper(trim({names[0]})) <> '{names[0].upper()}'""")  # drops repeated header rows
    K = "contract_id, plan_id, segment_id"
    db.execute(f"""CREATE OR REPLACE TABLE plans AS SELECT DISTINCT contract_id, plan_id, segment_id,
        contract_name, plan_name, formulary_id, TRY_CAST(premium AS DOUBLE) AS premium,
        TRY_CAST(deductible AS DOUBLE) AS deductible, snp FROM p_raw
      WHERE coalesce(plan_suppressed_yn,'N') <> 'Y' AND (
        (left(contract_id,1)='S' AND pdp_region_code='17') OR
        (left(contract_id,1)='R' AND ma_region_code='14') OR
        (left(contract_id,1)='H' AND state='IL'))""")
    n = db.execute("SELECT count(*) FROM plans").fetchone()[0]
    if n == 0: sys.exit("No Illinois plans found. Check region/state codes against the Plan Information file.")
    print("Illinois plans:", n)
    yes = "IN ('Y','1')"
    db.execute(f"""CREATE OR REPLACE TABLE formulary AS SELECT formulary_id, rxcui,
        min(TRY_CAST(tier_level_value AS INT)) AS tier,
        bool_or(quantity_limit_yn {yes}) AS ql, max(TRY_CAST(quantity_limit_amount AS DOUBLE)) AS ql_amount,
        max(TRY_CAST(quantity_limit_days AS INT)) AS ql_days,
        bool_or(prior_authorization_yn {yes}) AS pa, bool_or(step_therapy_yn {yes}) AS st,
        bool_or(selected_drug_yn {yes}) AS selected
      FROM f_raw WHERE formulary_id IN (SELECT formulary_id FROM plans) GROUP BY 1,2""")
    db.execute("""CREATE OR REPLACE TABLE ndc_map AS SELECT ndc, min(rxcui) AS rxcui FROM f_raw
      WHERE formulary_id IN (SELECT formulary_id FROM plans) GROUP BY 1""")
    # one typical unit cost per plan, drug and days supply (median across package sizes)
    db.execute(f"""CREATE OR REPLACE TABLE pricing AS SELECT pr.contract_id, pr.plan_id, pr.segment_id, m.rxcui,
        TRY_CAST(pr.days_supply AS INT) AS days_supply, median(TRY_CAST(pr.unit_cost AS DOUBLE)) AS unit_cost
      FROM pr_raw pr JOIN plans USING ({K}) JOIN ndc_map m ON m.ndc = pr.ndc GROUP BY 1,2,3,4,5""")
    fees = ", ".join(f"TRY_CAST({k}_dispensing_fee_{d} AS DOUBLE) AS {k.replace('_drug','')}_fee_{d}"
                     for k in ["brand", "generic", "selected_drug"] for d in (30, 60, 90))
    db.execute(f"""CREATE OR REPLACE TABLE pharmacies AS SELECT n.contract_id, n.plan_id, n.segment_id, pharmacy_number,
        pharmacy_zipcode AS zip, preferred_status_retail='Y' AS pref_retail, preferred_status_mail='Y' AS pref_mail,
        pharmacy_retail='Y' AS is_retail, pharmacy_mail='Y' AS is_mail, TRY_CAST(floor_price AS DOUBLE) AS floor_price, {fees}
      FROM n_raw n JOIN plans USING ({K})
      WHERE pharmacy_mail='Y' OR (pharmacy_zipcode BETWEEN '60001' AND '62999')""")
    dbl = lambda c, a=None: f"TRY_CAST({c} AS DOUBLE) AS {a or c}"
    ints = lambda c: f"TRY_CAST({c} AS INT) AS {c}"
    cost_cols = []
    for s in ["pref", "nonpref", "mail_pref", "mail_nonpref"]:
        cost_cols += [ints(f"cost_type_{s}"), dbl(f"cost_amt_{s}"), dbl(f"cost_min_amt_{s}"), dbl(f"cost_max_amt_{s}")]
    db.execute(f"""CREATE OR REPLACE TABLE beneficiary_cost AS SELECT c.contract_id, c.plan_id, c.segment_id,
        {ints('coverage_level')}, {ints('tier')}, {ints('days_supply')}, {', '.join(cost_cols)},
        tier_specialty_yn, ded_applies_yn FROM c_raw c JOIN plans USING ({K})""")
    ins = []
    for kind, short in [("copay", "copay"), ("coin", "coin")]:
        for s in ["pref", "nonpref", "mail_pref", "mail_nonpref"]:
            ins.append(dbl(f"{kind}_amt_{s}_insln", f"{short}_{s}"))
    db.execute(f"""CREATE OR REPLACE TABLE insulin_cost AS SELECT i.contract_id, i.plan_id, i.segment_id,
        {ints('tier')}, {ints('days_supply')}, {', '.join(ins)} FROM i_raw i JOIN plans USING ({K})""")
    tables = ["plans", "formulary", "beneficiary_cost", "insulin_cost", "pharmacies", "pricing"]
    for t in tables:
        print(t, db.execute(f"SELECT count(*) FROM {t}").fetchone()[0], "rows")
        db.execute(f"COPY {t} TO '{WORK}/{t}.csv' (HEADER, DELIMITER ',')")
    if os.environ.get("DRY_RUN") == "1":
        print("Dry run: CSV files written to", WORK); return
    import psycopg
    schema = open(os.path.join(os.path.dirname(__file__), "..", "supabase", "schema.sql")).read()
    with psycopg.connect(os.environ["SUPABASE_DB_URL"]) as conn:  # single transaction: readers see old data until commit
        conn.execute(schema)
        for t in tables:
            cols = [r[0] for r in db.execute(f"DESCRIBE {t}").fetchall()]
            conn.execute(f"TRUNCATE {t}")
            with conn.cursor() as cur, cur.copy(f"COPY {t} ({', '.join(cols)}) FROM STDIN WITH (FORMAT csv, HEADER true)") as cp:
                with open(f"{WORK}/{t}.csv", "rb") as fh:
                    while chunk := fh.read(1 << 20): cp.write(chunk)
        name = os.path.basename(os.environ.get("SPUF_ZIP") or os.environ.get("SPUF_URL", ""))
        conn.execute("INSERT INTO data_info (id, spuf_file) VALUES (1, %s) ON CONFLICT (id) DO UPDATE SET spuf_file=excluded.spuf_file, loaded_at=now()", [name])
    print("Load complete.")

if __name__ == "__main__":
    main()
