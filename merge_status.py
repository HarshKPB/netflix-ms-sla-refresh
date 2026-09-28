#!/usr/bin/env python3
"""Correct web/tickets.json status + dates from body-derived classification.

Merges two status sources keyed by Sprinklr ticket number:
  1. classify_status.py output over the saved thread bodies (--map)
  2. reviewed_status.json for cases whose body came inline (--reviewed), which wins.

For each ticket row it overrides `status`, fills `agent`, sets `opened` to the
earliest date seen in the body (so the age reflects true open time, not just the
first email in the pull window), and recomputes:
  days_open = today - opened
  days_idle = today - last_update
`age_days` is set to days_open so the table's Age column reads as days open.
"""
import json, sys, argparse
from datetime import date

def d(s):
    try:
        return date.fromisoformat((s or "")[:10])
    except Exception:
        return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickets", default="web/tickets.json")
    ap.add_argument("--map", default="/tmp/status_map.json")
    ap.add_argument("--reviewed", default="reviewed_status.json")
    a = ap.parse_args()

    tj = json.load(open(a.tickets))
    smap = json.load(open(a.map))
    rev = {k: v for k, v in json.load(open(a.reviewed)).items() if not k.startswith("_")}
    full = {**smap, **rev}  # reviewed wins

    today = date.today()
    unknown = []
    for r in tj["rows"]:
        cid = r.get("case_id") or ""
        # the Analyst Access row carries no id in the search data; its body ticket is 4073352
        if not cid and "analyst access" in (r.get("title", "").lower()):
            cid = "4073352"
            r["case_id"] = cid
            r["portal_link"] = r.get("portal_link") or "https://community.sprinklr.com/support/requests/4073352"
        s = full.get(cid)
        if s:
            r["status"] = s["status"]
            if s.get("agent"):
                r["agent"] = s["agent"]
            op = s.get("opened_est")
            if op and (not r.get("opened") or op < r["opened"]):
                r["opened"] = op
        else:
            r["status"] = "Unknown (check portal)"
            unknown.append(cid or r.get("title"))
        od, ld = d(r.get("opened")), d(r.get("last_update"))
        r["days_open"] = (today - od).days if od else None
        r["days_idle"] = (today - ld).days if ld else None
        r["age_days"] = r["days_open"]  # Age column = days open

    tj["rows"].sort(key=lambda r: (r.get("days_open") or -1), reverse=True)
    json.dump(tj, open(a.tickets, "w"), indent=2, ensure_ascii=False)

    from collections import Counter
    c = Counter(r["status"] for r in tj["rows"])
    print("status counts:", dict(c), file=sys.stderr)
    if unknown:
        print("UNKNOWN (no body classification):", unknown, file=sys.stderr)
    else:
        print("all rows classified", file=sys.stderr)

if __name__ == "__main__":
    main()
