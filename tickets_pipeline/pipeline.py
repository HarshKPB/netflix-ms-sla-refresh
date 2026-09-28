"""Build web/tickets.json from Gmail thread dumps, deterministically.

group -> classify -> apply overrides + locked truth -> validate schema -> write.
Callers (local run, cloud routine) only provide the thread dumps; the rules and
the contract live in classify.py and schema.py.
"""
import json
import glob
import os
from datetime import date
from . import classify, schema


def _d(s):
    try:
        return date.fromisoformat((s or "")[:10])
    except Exception:
        return None


def load_threads(paths):
    """Accept get_thread dumps ({id, messages, viewUrl}) and search dumps ({threads:[...]})."""
    threads, seen = [], set()
    for p in paths:
        try:
            d = json.load(open(p))
        except Exception:
            continue
        items = d.get("threads") if isinstance(d, dict) and "threads" in d else \
            ([d] if isinstance(d, dict) and "messages" in d else [])
        for th in items:
            tid = th.get("id")
            if tid and tid not in seen:
                seen.add(tid)
                threads.append(th)
    return threads


def group_by_ticket(threads):
    groups = {}
    for th in threads:
        msgs = th.get("messages", [])
        tk = classify.ticket_number(msgs) or ("thread:" + str(th.get("id")))
        g = groups.setdefault(tk, {"msgs": [], "gmail": th.get("viewUrl", "")})
        g["msgs"].extend(msgs)
        if not g["gmail"]:
            g["gmail"] = th.get("viewUrl", "")
    return groups


def build_rows(threads, overrides=None, truth=None, asof=None):
    overrides = overrides or {}
    truth = truth or {}
    asof = asof or date.today()
    rows = []
    for tk, g in group_by_ticket(threads).items():
        if not g["msgs"]:
            continue
        row = classify.classify_case(g["msgs"], g["gmail"])
        cid = row["case_id"] or tk
        row["case_id"] = cid
        od, ld = _d(row["opened"]), _d(row["last_update"])
        row["days_open"] = (asof - od).days if od else 0
        row["days_idle"] = (asof - ld).days if ld else 0
        # locked reviewed truth first, then live user overrides win
        if cid in truth:
            row["status"], row["needs_review"] = truth[cid], False
        if cid in overrides:
            row["status"], row["source"], row["needs_review"] = overrides[cid], "Override", False
        rows.append(row)
    rows.sort(key=lambda r: r.get("days_open", 0), reverse=True)
    return rows


def _load_map(path):
    if path and os.path.exists(path):
        return {k: v for k, v in json.load(open(path)).items() if not k.startswith("_")}
    return {}


def run(thread_paths, out="web/tickets.json", base_path="web/tickets.json",
        overrides_path="overrides.json", truth_path="status_truth.json",
        asof=None, source_label=""):
    """Classify the pulled threads, MERGE onto the existing tickets.json (cases not
    seen this run are kept), then apply overrides + locked truth, validate, write.
    Merging lets a daily run pull only a recent window yet keep the full history."""
    from datetime import datetime, timezone
    threads = load_threads(thread_paths)
    overrides = _load_map(overrides_path)
    raw_truth = _load_map(truth_path)
    truth = {k: (v if isinstance(v, str) else v.get("status")) for k, v in raw_truth.items()}
    asof = asof or date.today()

    fresh = {r["case_id"]: r for r in build_rows(threads, {}, {}, asof)}
    merged = dict(fresh)
    if base_path and os.path.exists(base_path):
        for br in json.load(open(base_path)).get("rows", []):
            if br.get("case_id") and br["case_id"] not in merged:
                merged[br["case_id"]] = br  # keep a case we did not pull this run
    rows = list(merged.values())

    for r in rows:
        od, ld = _d(r.get("opened")), _d(r.get("last_update"))
        r["days_open"] = (asof - od).days if od else r.get("days_open", 0)
        r["days_idle"] = (asof - ld).days if ld else r.get("days_idle", 0)
        r.setdefault("source", "Auto")
        cid = r["case_id"]
        if cid in truth:
            r["status"], r["needs_review"] = truth[cid], False
        if cid in overrides:
            r["status"], r["source"], r["needs_review"] = overrides[cid], "Override", False
    rows.sort(key=lambda r: r.get("days_open", 0), reverse=True)

    schema.assert_valid(rows)  # blocks the build if any row breaks the contract

    # regression gate: every locked-truth case must match
    mism = [(k, truth[k], next((r["status"] for r in rows if r["case_id"] == k), "MISSING"))
            for k in truth if next((r["status"] for r in rows if r["case_id"] == k), "MISSING") != truth[k]]
    if mism:
        raise ValueError("regression vs status_truth.json: " +
                         "; ".join(f"{k} want {w} got {g}" for k, w, g in mism))

    payload = {
        "generated": asof.isoformat(),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "source": source_label or "Gmail: care@prod.sprinklrsupport.com",
        "rows": rows,
    }
    with open(out, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return rows
