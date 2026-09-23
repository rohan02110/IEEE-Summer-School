"""Load participants.csv (placeholder, roll_no, name) into Supabase.
Upserts by placeholder -- safe to re-run any time you update names.

  python load_participants.py
"""
import csv
import os
import psycopg2

with psycopg2.connect(os.environ["SUPABASE_DB_URL"]) as con, con.cursor() as cur, \
     open("participants.csv", newline="", encoding="utf-8-sig") as f:
    n = 0
    for row in csv.DictReader(f):
        code = row["placeholder"].strip().upper()
        if not code:
            continue
        cur.execute("""INSERT INTO participants(placeholder, roll_no, name) VALUES(%s,%s,%s)
                       ON CONFLICT (placeholder) DO UPDATE SET roll_no=EXCLUDED.roll_no, name=EXCLUDED.name""",
                   (code, row.get("roll_no", "").strip(), row.get("name", "").strip()))
        n += 1
    con.commit()
print(f"loaded/updated {n} participants")
