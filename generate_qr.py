"""Generate 80 placeholder QR codes (P001-P080) + a mapping CSV to fill in later.

  python generate_qr.py

Produces:
  qr_codes/P001.png ... P080.png   (each encodes BASE_URL/t/P0xx)
  participants.csv                 (placeholder, roll_no, name -- fill roll_no/name in later)
"""
import csv
import os
import qrcode

BASE_URL = os.getenv("BASE_URL", "https://yourdomain.com").rstrip("/")
N = 80

os.makedirs("qr_codes", exist_ok=True)
rows = []
for i in range(1, N + 1):
    code = f"P{i:03d}"
    url = f"{BASE_URL}/t/{code}"
    img = qrcode.make(url, box_size=10, border=4)
    img.save(f"qr_codes/{code}.png")
    rows.append([code, "", ""])  # roll_no, name left blank to fill in

with open("participants.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["placeholder", "roll_no", "name"])
    w.writerows(rows)

print(f"generated {N} QR codes in qr_codes/ and participants.csv")
