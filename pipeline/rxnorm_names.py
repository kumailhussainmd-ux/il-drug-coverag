"""Fill the drugs table with names for each RXCUI using the free NLM RxNav API (incremental, safe to re-run).
Env: SUPABASE_DB_URL, MAX_SECONDS (default 18000)."""
import os, json, time, urllib.request
import psycopg

API = "https://rxnav.nlm.nih.gov/REST/rxcui/{}/allrelated.json"

def lookup(rxcui):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(API.format(rxcui), timeout=30) as r:
                groups = json.load(r).get("allRelatedGroup", {}).get("conceptGroup", []) or []
            break
        except Exception:
            time.sleep(2 * (attempt + 1))
    else:
        return None
    own, tty, brands, ingredients = None, None, [], []
    for g in groups:
        for c in g.get("conceptProperties", []) or []:
            if c.get("rxcui") == rxcui: own, tty = c.get("name"), c.get("tty")
            if g.get("tty") == "BN": brands.append(c["name"])
            if g.get("tty") in ("IN", "MIN"): ingredients.append(c["name"])
    return own, tty, "; ".join(sorted(set(ingredients))), "; ".join(sorted(set(brands)))

def main():
    deadline = time.time() + int(os.environ.get("MAX_SECONDS", "18000"))
    with psycopg.connect(os.environ["SUPABASE_DB_URL"], autocommit=True) as conn:
        todo = [r[0] for r in conn.execute(
            "SELECT DISTINCT rxcui FROM formulary WHERE rxcui NOT IN (SELECT rxcui FROM drugs)").fetchall()]
        print(len(todo), "drug names to fetch")
        for i, rx in enumerate(todo):
            if time.time() > deadline: print("Time limit reached; re-run to continue."); break
            res = lookup(rx)
            if res:
                conn.execute("INSERT INTO drugs (rxcui, name, tty, generic_name, brand_names) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", [rx, *res])
            time.sleep(0.06)  # stay under RxNav's 20 requests/second limit
            if i % 500 == 0: print(i, "done")

if __name__ == "__main__":
    main()
