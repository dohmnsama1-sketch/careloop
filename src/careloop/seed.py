"""Create the demo household: the Haddad family caring for grandmother Nora.

    careloop-seed --now 2026-10-23T08:05 --out data/careloop.json

Builds 7 days of realistic dose history (two missed evening doses), a low Lisinopril supply,
tomorrow's cardiology appointment and last night's handoff note, all relative to --now.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .store import EMPTY

TZ = "America/New_York"


def build(now: datetime) -> dict:
    d = json.loads(json.dumps(EMPTY))
    d["household"] = {
        "name": "The Haddad family", "timezone": TZ, "recipient": "nora",
        "members": [
            {"id": "nora", "name": "Nora", "role": "recipient", "aliases": ["mom", "grandma", "teta"], "can_purchase": False},
            {"id": "sara", "name": "Sara", "role": "caregiver", "aliases": ["daughter"], "can_purchase": True},
            {"id": "omar", "name": "Omar", "role": "caregiver", "aliases": ["son"], "can_purchase": True},
            {"id": "leila", "name": "Leila", "role": "caregiver", "aliases": ["granddaughter"], "can_purchase": False},
        ]}
    d["settings"] = {"monthly_spend_cap_cents": 15000, "quote_ttl_minutes": 10}
    meds = [
        ("med_1", "Lisinopril", "10 mg", "once every morning", ["blood pressure pill", "blood pressure"], ["08:00"], 5, 20, "lisinopril-10mg"),
        ("med_2", "Metformin", "500 mg", "with breakfast and dinner", ["diabetes pill", "sugar pill", "diabetes"], ["08:00", "20:00"], 46, 6, "metformin-500mg"),
        ("med_3", "Atorvastatin", "20 mg", "in the evening", ["cholesterol pill", "statin", "cholesterol"], ["20:00"], 24, 12, "atorvastatin-20mg"),
        ("med_4", "Vitamin D3", "1000 IU", "with breakfast", ["vitamin d", "vitamin"], ["08:00"], 60, 4, "vitamin-d3-1000iu"),
    ]
    for mid, name, strength, instr, aliases, times, onhand, gap, item in meds:
        d["meds"].append({"id": mid, "name": name, "strength": strength, "instructions": instr, "aliases": aliases,
                          "times": times, "pills_on_hand": onhand, "pills_per_dose": 1, "min_interval_hours": gap,
                          "refill_threshold_days": 7, "item_id": item, "active": True, "added_by": "sara"})
    d["counters"]["med"] = len(meds)
    def logger(n: int, t: str) -> str:  # demo pattern: Sara does mornings, Omar evenings, Nora and Leila sometimes
        if n % 7 == 3:
            return "nora"
        if n % 11 == 5:
            return "leila"
        return "sara" if t < "12:00" else "omar"
    missed = {(2, "med_2", "20:00"), (5, "med_3", "20:00")}  # (days ago, med, time)
    n = 0
    for back in range(7, 0, -1):
        day = now - timedelta(days=back)
        for mid, *_rest in meds:
            times = next(m["times"] for m in d["meds"] if m["id"] == mid)
            for t in times:
                if (back, mid, t) in missed:
                    continue
                h, m = (int(x) for x in t.split(":"))
                slot = day.replace(hour=h, minute=m, second=0, microsecond=0)
                at = slot + timedelta(minutes=(n * 7) % 25)
                n += 1
                d["doses"].append({"id": f"dose_{n}", "med_id": mid, "status": "taken", "at": at.isoformat(),
                                   "slot": slot.isoformat(), "logged_by": logger(n, t), "extra": False})
    d["counters"]["dose"] = n
    appt = (now + timedelta(days=1)).replace(hour=10, minute=30, second=0, microsecond=0)
    d["appointments"].append({"id": "appt_1", "title": "Cardiology check-up with Dr. Patel", "when": appt.isoformat(),
                              "where": "Riverside Heart Clinic", "driver": "omar",
                              "notes": "Bring the blood pressure log", "created_by": "sara"})
    d["counters"]["appt"] = 1
    last_night = (now - timedelta(days=1)).replace(hour=21, minute=40, second=0, microsecond=0)
    d["notes"].append({"id": "note_1", "author": "omar", "at": last_night.isoformat(),
                       "text": "Mom slept well. She asked to see the wedding photos tomorrow."})
    d["counters"]["note"] = 1
    d["seen"] = {"sara": (now - timedelta(days=1)).replace(hour=19, minute=0, second=0, microsecond=0).isoformat()}
    return d


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="careloop-seed")
    p.add_argument("--now", default=None, help="demo 'now' in the household zone (default: real now)")
    p.add_argument("--out", default="data/careloop.json")
    a = p.parse_args(argv)
    tz = ZoneInfo(TZ)
    now = datetime.fromisoformat(a.now).replace(tzinfo=tz) if a.now else datetime.now(tz)
    from pathlib import Path
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(build(now), indent=2), encoding="utf-8")
    print(f"Demo household written to {out} (now = {now.isoformat()})")


if __name__ == "__main__":
    main()
