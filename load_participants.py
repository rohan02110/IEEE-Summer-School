"""Upsert participants.csv (placeholder, roll_no, name) into Supabase.
Safe to re-run any time you update names -- it upserts by placeholder,
the primary key, so existing attendance rows are untouched.

  python load_participants.py
"""
import csv
import os
from supabase import create_client

def _load_env():
    for fpath in (".env", ".env.example"):
        if os.path.exists(fpath):
            with open(fpath, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip().strip("'\"")
                        if k not in os.environ:
                            os.environ[k] = v

_load_env()

supabase_url = os.environ.get("SUPABASE_URL", "").replace("/rest/v1/", "").replace("/rest/v1", "").rstrip("/")
supabase_key = os.environ.get("SUPABASE_KEY", "")

if not supabase_url or not supabase_key:
    raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in the environment or .env file.")

sb = create_client(supabase_url, supabase_key)

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
