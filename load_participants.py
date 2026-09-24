"""Upsert participants.csv (placeholder, roll_no, name) into Supabase.
Safe to re-run any time you update names -- it upserts by placeholder,
the primary key, so existing attendance rows are untouched.

  python load_participants.py
"""
import csv
import os
from supabase import create_client

sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

rows = []
with open("participants.csv", newline="", encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        code = row["placeholder"].strip().upper()
        if code:
            rows.append({"placeholder": code, "roll_no": row.get("roll_no", "").strip(),
                        "name": row.get("name", "").strip()})

if rows:
    sb.table("participants").upsert(rows, on_conflict="placeholder").execute()
print(f"upserted {len(rows)} participants")
