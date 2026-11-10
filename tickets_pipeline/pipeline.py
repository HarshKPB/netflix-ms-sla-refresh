"""Build web/tickets.json from Gmail thread dumps, deterministically.

group -> classify -> apply overrides + locked truth -> validate schema -> write.
Callers (local run, cloud routine) only provide the thread dumps; the rules and
the contract live in classify.py and schema.py.
"""
import json
import glob
import os
import re
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
    """Group a case's threads under one key, strongest identifier first:
      1. the ticket number ("Ticket Number NNNNN" or the review-link URL);
      2. the Sprinklr ConversationId, mapped to a number learned from threads
         that carry both (handles a number-less opener/reopen/CSAT thread);
      3. an exact, UNIQUE subject match to a numbered thread (handles a scheduling
         fragment Gmail split off with its own ConversationId and no number).
    Only a thread with none of these falls to a conv:/thread: stub key."""
    from collections import defaultdict
    meta, conv2num, subj2nums = [], {}, defaultdict(set)
    for th in threads:
        msgs = th.get("messages", [])
        num = classify.ticket_number(msgs)
        convs = classify.conversation_ids(msgs)
        first = sorted(msgs, key=lambda m: m.get("date", ""))[0] if msgs else {}
        subj = classify.clean_title(first.get("subject", ""))
        meta.append((th, msgs, num, convs, subj))
        if num:
            for c in convs:
                conv2num.setdefault(c, num)
            if len(subj) >= 12:
                subj2nums[subj].add(num)

    def subj_num(subj):  # only when the subject maps to exactly one ticket number
        s = subj2nums.get(subj)
        return next(iter(s)) if s and len(s) == 1 else None

    groups = {}
    for th, msgs, num, convs, subj in meta:
        conv_hit = next((conv2num[c] for c in convs if c in conv2num), None)
        key = (num or conv_hit or subj_num(subj)
               or (("conv:" + convs[0]) if convs else ("thread:" + str(th.get("id")))))
        g = groups.setdefault(key, {"msgs": [], "gmail": th.get("viewUrl", "")})
        g["msgs"].extend(msgs)
        if not g["gmail"]:
            g["gmail"] = th.get("viewUrl", "")
    return groups


def _thread_hex(gmail_link):
    m = re.search(r"thread-f:(\d+)", gmail_link or "")
    try:
        return format(int(m.group(1)), "x") if m else ""
    except Exception:
        return ""


def dedupe_rows(rows):
    """Drop stub rows (case_id 'thread:<hex>' / 'conv:<hex>') that are really a
    segment of a numbered ticket, matched by a shared ConversationId, Gmail thread
    id, or an exact UNIQUE title. The title match catches stub rows kept from an
    older tickets.json that predates the conversation_id field (they carry no conv
    to match on, but their title still equals the numbered row's). Numbered rows
    always win and are kept. Returns (kept, dropped_case_ids)."""
    from collections import defaultdict
    conv_owner, hex_owner, title_nums = {}, {}, defaultdict(set)
    for r in rows:
        if str(r.get("case_id", "")).isdigit():
            if r.get("conversation_id"):
                conv_owner.setdefault(r["conversation_id"], r["case_id"])
            h = _thread_hex(r.get("gmail_link"))
            if h:
                hex_owner.setdefault(h, r["case_id"])
            t = classify.clean_title(r.get("title", ""))
            if len(t) >= 12:
                title_nums[t].add(r["case_id"])
    title_owner = {t: next(iter(s)) for t, s in title_nums.items() if len(s) == 1}
    kept, dropped = [], []
    for r in rows:
        cid = str(r.get("case_id", ""))
        if cid.isdigit():
            kept.append(r)
            continue
        conv = r.get("conversation_id") or ""
        h = cid[len("thread:"):] if cid.startswith("thread:") else _thread_hex(r.get("gmail_link"))
        t = classify.clean_title(r.get("title", ""))
        if (conv and conv in conv_owner) or (h and h in hex_owner) or (t and t in title_owner):
            dropped.append(cid)
            continue
        kept.append(r)
    return kept, dropped


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
        r.setdefault("region", "Global/Unknown")
        r.setdefault("request_type", "Other")
        r.setdefault("initial_sla_hours", None)
        r.setdefault("resolution_sla_hours", None)
        r.setdefault("conversation_id", "")  # absent on rows from an older tickets.json
        cid = r["case_id"]
        if cid in truth:
            r["status"], r["needs_review"] = truth[cid], False
        if cid in overrides:
            r["status"], r["source"], r["needs_review"] = overrides[cid], "Override", False

    # collapse stub rows that are really a segment of a numbered ticket (same
    # ConversationId or Gmail thread), including stale thread:<hex> rows kept from
    # an earlier run before that thread could be numbered.
    rows, dropped = dedupe_rows(rows)
    rows.sort(key=lambda r: r.get("days_open", 0), reverse=True)

    schema.assert_valid(rows)  # blocks the build if any row breaks the contract

    # regression gate: every locked-truth case must match, EXCEPT cases a human has
    # since overridden from the tab. An override is the newest human decision and
    # supersedes the older truth lock, so it is exempt (otherwise a live correction
    # that disagrees with status_truth.json would fail every build).
    mism = [(k, truth[k], next((r["status"] for r in rows if r["case_id"] == k), "MISSING"))
            for k in truth if k not in overrides
            and next((r["status"] for r in rows if r["case_id"] == k), "MISSING") != truth[k]]
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
