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
     "Slack = came from the Slack channel. Sprinklr = came from the Sprinklr request form. No maths."],
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
HISTORY_TAB = os.environ.get("HISTORY_TAB", "History")
HISTORY_HEADERS = ["run_at", "total", "slack", "sprinklr", "open", "completed"]

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
            continue   # Sprinklr rows come from the scrape, not Asana, to avoid duplicates
        if s == "Other":
            continue   # only Slack and Sprinklr are real sources; drop anything else
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
    # Remove old SLA and Definitions tabs, but PRESERVE the History tab (it accumulates
    # run-over-run snapshots and must survive the rebuild).
    for other in ss.worksheets():
        if other.id != ws.id and other.title != HISTORY_TAB:
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

    format_sheet(ss, ws, dws, len(rows))
    return ss


def update_history(ss, rows, run_at):
    """Append one snapshot row to the History tab and return recent snapshots.

    The History tab is never wiped, so it accumulates one row per run over time.
    This is what makes trends real instead of bound to the rolling scrape window.
    """
    counts = {"Slack": 0, "Sprinklr": 0}
    done = 0
    for r in rows:
        counts[r["source"]] = counts.get(r["source"], 0) + 1
        if r["Request_fulfil_time"].strip():
            done += 1
    total = len(rows)
    snap = [run_at, total, counts.get("Slack", 0), counts.get("Sprinklr", 0),
            total - done, done]
    try:
        hws = ss.worksheet(HISTORY_TAB)
    except gspread.WorksheetNotFound:
        hws = ss.add_worksheet(title=HISTORY_TAB, rows=1000, cols=len(HISTORY_HEADERS))
        hws.update([HISTORY_HEADERS], value_input_option="RAW")
    hws.append_row(snap, value_input_option="RAW")
    values = hws.get_all_values()
    body = values[1:] if values and values[0] and values[0][0] == "run_at" else values
    recent = body[-120:]
    return [dict(zip(HISTORY_HEADERS, row)) for row in recent]


# Formatting is reapplied every run because the tabs are recreated each time.
HEADER_BG = {"red": 0.83, "green": 0.09, "blue": 0.13}   # Netflix-ish red
HEADER_FG = {"red": 1, "green": 1, "blue": 1}
BAND_BG = {"red": 0.96, "green": 0.96, "blue": 0.97}
DEFS_HEADER_BG = {"red": 0.17, "green": 0.24, "blue": 0.31}   # dark slate


def _hdr(sheet_id, ncols, bg):
    return {"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                  "startColumnIndex": 0, "endColumnIndex": ncols},
        "cell": {"userEnteredFormat": {
            "backgroundColor": bg,
            "horizontalAlignment": "LEFT", "verticalAlignment": "MIDDLE",
            "wrapStrategy": "WRAP",
            "textFormat": {"bold": True, "foregroundColor": HEADER_FG, "fontSize": 10}}},
        "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,wrapStrategy,textFormat)"}}


def _freeze(sheet_id, rows=1, cols=0):
    return {"updateSheetProperties": {
        "properties": {"sheetId": sheet_id,
                       "gridProperties": {"frozenRowCount": rows, "frozenColumnCount": cols}},
        "fields": "gridProperties(frozenRowCount,frozenColumnCount)"}}


def _width(sheet_id, start, end, px):
    return {"updateDimensionProperties": {
        "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                  "startIndex": start, "endIndex": end},
        "properties": {"pixelSize": px}, "fields": "pixelSize"}}


def _align(sheet_id, col, nrows, align, wrap=None):
    fmt = {"horizontalAlignment": align, "verticalAlignment": "MIDDLE"}
    fields = "horizontalAlignment,verticalAlignment"
    if wrap:
        fmt["wrapStrategy"] = wrap
        fields += ",wrapStrategy"
    return {"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": nrows + 1,
                  "startColumnIndex": col, "endColumnIndex": col + 1},
        "cell": {"userEnteredFormat": fmt},
        "fields": "userEnteredFormat(%s)" % fields}}


def _band(sheet_id, nrows, ncols):
    return {"addBanding": {"bandedRange": {
        "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": nrows + 1,
                  "startColumnIndex": 0, "endColumnIndex": ncols},
        "rowProperties": {"firstBandColor": {"red": 1, "green": 1, "blue": 1},
                          "secondBandColor": BAND_BG}}}}


def format_sheet(ss, ws, dws, n):
    sid, did = ws.id, dws.id
    reqs = [
        _hdr(sid, len(HEADERS), HEADER_BG),
        _freeze(sid, rows=1, cols=2),
        _band(sid, n, len(HEADERS)),
        _width(sid, 0, 1, 70),     # source
        _width(sid, 1, 2, 380),    # task_name
        _width(sid, 2, 3, 210),    # assignee
        _width(sid, 3, 4, 130),    # request_arrival_time
        _width(sid, 4, 5, 150),    # Assigned/Emoji initiated
        _width(sid, 5, 6, 120),    # SLA_Assignement
        _width(sid, 6, 7, 130),    # Request_fulfil_time
        _width(sid, 7, 8, 120),    # SLA_completion
        _align(sid, 1, n, "LEFT", wrap="CLIP"),    # task_name: one-line clip
        _align(sid, 3, n, "CENTER"), _align(sid, 4, n, "CENTER"),
        _align(sid, 5, n, "CENTER"), _align(sid, 6, n, "CENTER"),
        _align(sid, 7, n, "CENTER"),
        # Definitions tab
        _hdr(did, len(DEFS_HEADERS), DEFS_HEADER_BG),
        _freeze(did, rows=1, cols=1),
        _width(did, 0, 1, 200),    # Column
        _width(did, 1, 2, 320),    # meaning
        _width(did, 2, 3, 620),    # calculation
        _align(did, 1, len(DEFS), "LEFT", wrap="WRAP"),
        _align(did, 2, len(DEFS), "LEFT", wrap="WRAP"),
        {"repeatCell": {
            "range": {"sheetId": did, "startRowIndex": 1, "endRowIndex": len(DEFS) + 1,
                      "startColumnIndex": 0, "endColumnIndex": 1},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat(textFormat)"}},
    ]
    ss.batch_update({"requests": reqs})


def main():
    gc = gclient()
    intake = load_intake(gc)
    asana = fetch_asana()
    cases = p.fetch_cases_from_dashboard()
    rows = build_rows(intake, asana, cases)
    if not rows:
        print("no rows built, refusing to overwrite the sheet")
        sys.exit(1)
    ss = write_sheet(gc, rows)

    now = dt.datetime.now(IST)
    run_at = now.strftime("%Y-%m-%d %H:%M")
    history = update_history(ss, rows, run_at)

    # Optional: also emit data.json for the web dashboard. Only when WEB_DATA_DIR is set.
    web_dir = os.environ.get("WEB_DATA_DIR")
    if web_dir:
        os.makedirs(web_dir, exist_ok=True)
        payload = {"generated": now.strftime("%Y-%m-%d"),
                   "generated_at": run_at,
                   "headers": HEADERS,
                   "history": history,
                   "rows": [{h: r[h] for h in HEADERS} for r in rows]}
        with open(os.path.join(web_dir, "data.json"), "w") as fh:
            json.dump(payload, fh)
        print(f"wrote {web_dir}/data.json ({len(history)} history rows)")

    n = {"Slack": 0, "Sprinklr": 0, "Other": 0}
    for r in rows:
        n[r["source"]] = n.get(r["source"], 0) + 1
    print(f"wrote {len(rows)} rows: {n}")


if __name__ == "__main__":
    main()
