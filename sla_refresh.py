#!/usr/bin/env python3
"""
Laptop-independent SLA refresh. Runs in GitHub Actions on a schedule.

Joins three sources into one coherent table and writes it to the Google sheet:
  Sprinklr rows  from the dashboard scrape (playwright), using a stored session.
  Slack rows     from Asana plus the intake sheet (reactor, message time).
  Other rows     from Asana.

All credentials come from environment variables set by the workflow from repo secrets.
Nothing secret is stored in this file or the repo.

Columns: source, task_name, assignee, request_arrival_time, Assigned/Emoji initiated,
SLA_Assignement, Request_fulfil_time, SLA_completion.
"""
import datetime as dt
import json
import os
import re
import sys
import logging

logging.basicConfig(level=logging.CRITICAL)

import requests
import gspread
from google.oauth2.service_account import Credentials

import sprinklr_to_asana as p   # scrape lives here; imports cleanly with ASANA_PAT set

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ASANA_BASE = "https://app.asana.com/api/1.0"
NETFLIX_PROJECT = os.environ.get("ASANA_PROJECT_GID", "1214792401514445")
SLA_SHEET_ID = os.environ["SLA_SHEET_ID"]
SLA_TAB = os.environ.get("SLA_TAB", "SLA")
INTAKE_SHEET_ID = os.environ["INTAKE_SHEET_ID"]
INTAKE_TAB = os.environ.get("INTAKE_TAB", "Requests")

HEADERS = ["source", "task_name", "assignee", "request_arrival_time",
           "Assigned/Emoji initiated", "SLA_Assignement", "Request_fulfil_time", "SLA_completion"]

# Plain-language data dictionary written to a second tab. Rebuilt every run so it
# survives the tab wipe in write_sheet. Columns: name, meaning, how it is calculated.
DEFS_HEADERS = ["Column", "What it means (simple)", "How it is worked out"]
DEFS = [
    ["source",
     "Where the request came from.",
     "Slack = came from the Slack channel. Sprinklr = came from the Sprinklr request form. Other = anything else. No maths."],
    ["task_name",
     "Short title of the request.",
     "Sprinklr: the case number and subject. Slack: the sender name and first line of their message. No maths."],
    ["assignee",
     "Person handling the request.",
     "Sprinklr: the person the case is assigned to. Slack: the Asana assignee, or if none, the person who added the ticket emoji. No maths."],
    ["request_arrival_time",
     "When the request first came in.",
     "Sprinklr: the time the case was created. Slack: the time of the first Slack message. Shown in IST as yyyy-mm-dd HH:MM."],
    ["Assigned/Emoji initiated",
     "When work was picked up.",
     "Sprinklr: the time the case was assigned to a person. Slack: the time the ticket emoji was added (which creates the Asana task). IST, yyyy-mm-dd HH:MM. Blank if it never got picked up."],
    ["SLA_Assignement",
     "How long from arrival to pick-up.",
     "Assigned/Emoji initiated time minus request_arrival_time. Shown as Xh Ym (hours and minutes). Blank if either time is missing."],
    ["Request_fulfil_time",
     "When the request was finished.",
     "Sprinklr: the time the status became COMPLETED. Slack: the time the Asana task was marked complete. IST, yyyy-mm-dd HH:MM. Blank if the task is still open."],
    ["SLA_completion",
     "How long from pick-up to finish.",
     "Request_fulfil_time minus Assigned/Emoji initiated time. Shown as Xh Ym. Blank if the task is not finished yet, or if there is no pick-up time to measure from."],
    ["", "", ""],
    ["Note on blank cells", "A blank Request_fulfil_time or SLA_completion is not an error.",
     "It means the task is still open. It fills in automatically once the task is completed. Verified 2026-08-20: every blank matched an open (not completed) task in Asana or Sprinklr."],
]

DEFS_TAB = os.environ.get("DEFS_TAB", "Definitions")

_TEST_RE = re.compile(r"\bTEST\b|diagnostic from curl|^Task [123]$", re.I)


def gclient():
    info = json.loads(os.environ["GOOGLE_SA_JSON"])
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    return gspread.authorize(Credentials.from_service_account_info(info, scopes=scopes))


def ms_to_ist(ms):
    return dt.datetime.fromtimestamp(int(ms) / 1000, IST).strftime("%Y-%m-%d %H:%M") if ms else ""


def hm(a, b):
    if not a or not b:
        return ""
    m = round((int(b) - int(a)) / 60000)
    return "" if m < 0 else "%dh %02dm" % (m // 60, m % 60)


def classify(name):
    if name.startswith("["):
        return "Slack"
    if name.startswith("#") or name.startswith("Review Task"):
        return "Sprinklr"
    return "Other"


def is_test(name):
    return bool(_TEST_RE.search(name or ""))


def iso_ms(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000 if s else None


def fetch_asana():
    H = {"Authorization": f"Bearer {p.require_secret('ASANA_PAT')}"}
    out, off = [], None
    while True:
        q = {"project": NETFLIX_PROJECT, "limit": 100,
             "opt_fields": "name,created_at,completed,completed_at,assignee.name"}
        if off:
            q["offset"] = off
        r = requests.get(f"{ASANA_BASE}/tasks", headers=H, params=q, timeout=60)
        r.raise_for_status()
        j = r.json()
        out += j["data"]
        off = (j.get("next_page") or {}).get("offset")
        if not off:
            return out


def load_intake(gc):
    """gid -> {ms, reactor} read live from the intake Google sheet via the service account.

    Locates columns by header name so an inserted column cannot shift the mapping. Prefers
    the GID parsed from the Asana Task URL, which is full text; the Asana Task GID column is
    sometimes numeric and loses precision.
    """
    ws = gc.open_by_key(INTAKE_SHEET_ID).worksheet(INTAKE_TAB)
    values = ws.get_all_values()
    if len(values) < 2:
        return {}
    header = [h.strip().lower() for h in values[0]]
    url_c = header.index("asana task url") if "asana task url" in header else -1
    gid_c = header.index("asana task gid") if "asana task gid" in header else -1
    ts_c = header.index("message ts") if "message ts" in header else -1
    re_c = header.index("reactor (slack)") if "reactor (slack)" in header else -1
    from_url = re.compile(r"/task/(\d{6,})")
    slack_ts = re.compile(r"^\d{10}(\.\d+)?$")
    out = {}
    for row in values[1:]:
        def cell(i):
            return row[i].strip() if 0 <= i < len(row) else ""
        raw = cell(ts_c)
        if not slack_ts.match(raw):
            continue
        gid = ""
        if url_c != -1:
            m = from_url.search(cell(url_c))
            if m:
                gid = m.group(1)
        if not gid and gid_c != -1 and re.fullmatch(r"\d{12,}", cell(gid_c)):
            gid = cell(gid_c)
        if not gid:
            continue
        out[gid] = {"ms": int(round(float(raw) * 1000)), "reactor": cell(re_c)}
    return out


def build_rows(intake, asana, cases):
    rows = []
    for c in cases:
        try:
            name = p.build_task_name(c)
        except Exception:
            name = c.get("title") or "Sprinklr task"
        if is_test(name):
            continue
        created = c.get("createdTime")
        assigned = (c.get("userAssignmentDetails") or {}).get("assignmentTime")
        done = c.get("statusLastModifiedDate") if str(c.get("taskStatus")).upper() == "COMPLETED" else None
        rows.append({
            "source": "Sprinklr", "task_name": name, "assignee": c.get("assigneeName") or "",
            "request_arrival_time": ms_to_ist(created),
            "Assigned/Emoji initiated": ms_to_ist(assigned),
            "SLA_Assignement": hm(created, assigned),
            "Request_fulfil_time": ms_to_ist(done),
            "SLA_completion": hm(assigned, done),
            "_s": int(created) if created else 0,
        })
    for t in asana:
        if is_test(t["name"]):
            continue
        s = classify(t["name"])
        if s == "Sprinklr":
            continue
        created = iso_ms(t.get("created_at"))
        done = iso_ms(t.get("completed_at")) if (t.get("completed") and t.get("completed_at")) else None
        info = intake.get(t["gid"])
        arr = info["ms"] if info else None
        asg = (t["assignee"]["name"] if (t.get("assignee") and t["assignee"].get("name"))
               else (info["reactor"] if info and info.get("reactor") else ""))
        rows.append({
            "source": s, "task_name": t["name"], "assignee": asg,
            "request_arrival_time": ms_to_ist(arr),
            "Assigned/Emoji initiated": ms_to_ist(created),
            "SLA_Assignement": hm(arr, created),
            "Request_fulfil_time": ms_to_ist(done),
            "SLA_completion": hm(created, done),
            "_s": int(created) if created else 0,
        })
    rows.sort(key=lambda r: r["_s"], reverse=True)
    for r in rows:
        del r["_s"]
    return rows


def write_sheet(gc, rows):
    ss = gc.open_by_key(SLA_SHEET_ID)
    # Add the new tab FIRST under a temp title. Google forbids deleting the last
    # remaining sheet, so the replacement must exist before we remove the old ones.
    tmp = "SLA_new"
    try:
        ss.del_worksheet(ss.worksheet(tmp))   # clear a leftover temp from a failed run
    except gspread.WorksheetNotFound:
        pass
    ws = ss.add_worksheet(title=tmp, rows=len(rows) + 10, cols=len(HEADERS))
    # Now remove every other tab (old SLA + any leftovers).
    for other in ss.worksheets():
        if other.id != ws.id:
            try:
                ss.del_worksheet(other)
            except Exception:
                pass
    ws.update_title(SLA_TAB)
    grid = [HEADERS] + [[r[h] for h in HEADERS] for r in rows]
    # USER_ENTERED would let Sheets re-type; RAW keeps our exact text.
    ws.update(grid, value_input_option="RAW")

    # Second tab: plain-language column definitions, rebuilt fresh every run.
    dws = ss.add_worksheet(title=DEFS_TAB, rows=len(DEFS) + 5, cols=len(DEFS_HEADERS))
    dws.update([DEFS_HEADERS] + DEFS, value_input_option="RAW")


def main():
    gc = gclient()
    intake = load_intake(gc)
    asana = fetch_asana()
    cases = p.fetch_cases_from_dashboard()
    rows = build_rows(intake, asana, cases)
    if not rows:
        print("no rows built, refusing to overwrite the sheet")
        sys.exit(1)
    write_sheet(gc, rows)
    n = {"Slack": 0, "Sprinklr": 0, "Other": 0}
    for r in rows:
        n[r["source"]] = n.get(r["source"], 0) + 1
    print(f"wrote {len(rows)} rows: {n}")


if __name__ == "__main__":
    main()
