"""One-time setup for Feit/Tuya lights. Run again after adding or re-pairing bulbs.

Needs TUYA_ACCESS_ID, TUYA_ACCESS_SECRET and TUYA_REGION in .env (from your Tuya
cloud project's Overview page) and your Smart Life account linked to that project.

It downloads each bulb's name and local key from Tuya, finds the bulbs on your
Wi-Fi, and saves everything to devices.json (which is kept out of git).
Keys are never printed.
"""

import json
import os
from pathlib import Path

import tinytuya
from dotenv import load_dotenv

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

access_id = os.getenv("TUYA_ACCESS_ID", "").strip()
secret = os.getenv("TUYA_ACCESS_SECRET", "").strip()
region = os.getenv("TUYA_REGION", "us").strip() or "us"
if not (access_id and secret):
    raise SystemExit("Add TUYA_ACCESS_ID and TUYA_ACCESS_SECRET to .env first.")

print(f"Asking Tuya ({region}) for your devices...")
cloud = tinytuya.Cloud(apiRegion=region, apiKey=access_id, apiSecret=secret)
if getattr(cloud, "error", None):
    raise SystemExit(f"Tuya login failed: {cloud.error}")
devices = cloud.getdevices(verbose=False)
if isinstance(devices, dict):  # an error came back
    raise SystemExit(f"Tuya error: {devices.get('Payload') or devices}")
if not devices:
    raise SystemExit(
        "Tuya returned no devices. Check that your Smart Life account is linked to the\n"
        "project (Devices > Link App Account) and that the data center matches TUYA_REGION."
    )

print("Looking for them on your Wi-Fi (about 20 seconds)...")
found = tinytuya.deviceScan(verbose=False, maxretry=None)
by_id = {d.get("gwId") or d.get("id"): d for d in found.values()}

saved = []
for d in devices:
    local = by_id.get(d["id"], {})
    saved.append({
        "name": d.get("name") or d["id"],
        "id": d["id"],
        "key": d.get("key", ""),
        "ip": local.get("ip", ""),
        "version": local.get("version", ""),  # blank = detect when first used
        "category": d.get("category", ""),
        "product": d.get("product_name", ""),
    })

(HERE / "devices.json").write_text(json.dumps(saved, indent=2), encoding="utf-8")
print(f"\nSaved {len(saved)} device(s) to devices.json:")
for d in saved:
    where = d["ip"] or "not found on Wi-Fi right now (switched off at the wall?)"
    print(f"  - {d['name']}  [{d['product'] or d['category']}]  {where}")
