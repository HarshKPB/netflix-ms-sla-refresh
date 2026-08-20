#!/usr/bin/env python3
"""
Netflix Sprinklr → Asana  |  Automation
─────────────────────────────────────────────────────────────────────────────
Every 15 minutes (via macOS launchd), loads the Sprinklr Request Form
dashboard in a headless browser, detects new and changed submissions, and
creates or updates Asana tasks accordingly.

Syncs:
  • New submissions   → creates Asana task with all fields populated
  • Assignee change   → updates Asana assignee
  • Status change     → updates notes; marks complete if COMPLETED in Sprinklr
  • Due date change   → updates Asana due date

Commands — always via the project venv, never bare `python3`, which resolves to
a Homebrew build with no playwright. Run from the project directory:

  .venv/bin/python3 sprinklr_to_asana.py          — normal sync
  .venv/bin/python3 sprinklr_to_asana.py --test   — connectivity check, no writes
  .venv/bin/python3 setup_session.py              — interactive login (session expired)
  .venv/bin/python3 health_watchdog.py --check    — is it actually running? (read-only)

No venv yet? `bash setup.sh` builds it from requirements.txt.
─────────────────────────────────────────────────────────────────────────────
"""

import sys, os, json, logging, argparse, requests, hashlib, re as _re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

import health

# ─── SECRETS ──────────────────────────────────────────────────────────────────
# Credentials live outside the project directory so they are never committed.
SECRETS_FILE = os.path.expanduser("~/.netflix_automation.env")


def require_secret(name: str) -> str:
    if name in os.environ:
        return os.environ[name]
    try:
        with open(SECRETS_FILE) as fh:
            for line in fh:
                key, sep, val = line.strip().partition("=")
                if sep and key.strip() == name:
                    return val.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    # This runs at import time, before logging is configured and before the
    # entry point's handlers exist, so record the failure here or it is silent.
    health.write_heartbeat("missing_secret", name)
    health.notify("Sprinklr → Asana cannot start", f"Missing secret {name}.", "missing_secret")
    sys.exit(f"Missing secret {name!r}. Add a line  {name}=<value>  to {SECRETS_FILE}")


# ─── CONFIGURATION ────────────────────────────────────────────────────────────

DASHBOARD_URL     = "https://netflix.sprinklr.com/social/engagement/dashboard/665a42eb0f76ce53e5fd151e"

ASANA_PAT         = require_secret("ASANA_PAT")
ASANA_PROJECT_GID = "1214152562106900"
ASANA_BASE_URL    = "https://app.asana.com/api/1.0"

SESSION_FILE      = os.path.expanduser("~/.netflix_sprinklr_session.json")
STATE_FILE        = os.path.expanduser("~/.netflix_sprinklr_state.json")
LOG_FILE          = os.path.expanduser("~/Library/Logs/netflix_sprinklr_asana.log")

# Recovery instruction, built so it is copy-pasteable. sys.executable is by
# definition an interpreter that could import this module, unlike bare `python3`
# which resolves to a Homebrew build with no playwright.
_SETUP_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "setup_session.py")
RELOGIN_CMD   = f'"{sys.executable}" "{_SETUP_SCRIPT}"'

# ── Form field labels ─────────────────────────────────────────────────────────
# Maps Sprinklr's relativeKey (field_N) → human-readable label.
# Confirmed from Sprinklr form UI screenshots.
FIELD_LABELS: dict = {
    "field_1":  "Agency Name",
    "field_3":  "Region",
    "field_4":  "Country",
    "field_6":  "Users to Activate",
    "field_7":  "Users to Deactivate",   # deactivation textarea
}

# Maps Sprinklr's fieldName (PICKLIST_ID) → the field's question label.
PICKLIST_FIELD_LABELS: dict = {
    "PICKLIST_8357": "Request Type",
    "PICKLIST_288":  "Employee Type",
    "PICKLIST_1275": "Sprinklr Use Case",
}

# Runtime cache populated during browser session: picklist option UUID → label.
# Sprinklr returns these when the dashboard loads form detail views.
_PICKLIST_OPTION_CACHE: dict = {}


# ─── LOGGING ──────────────────────────────────────────────────────────────────

log = logging.getLogger(__name__)
SESSION_EXPIRED = "SESSION_EXPIRED"


def configure_logging(log_file: str = LOG_FILE) -> None:
    """Attach handlers. Call from __main__ only, never at import.

    A RotatingFileHandler renames the file it owns, so two processes pointed at
    one path will roll it out from under each other. backfill_last_week.py
    imports this module, so configuring at import time gave the hourly backfill
    a second rotator on the sync's log — hence the explicit log_file argument.
    """
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    handlers: list = [RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3)]
    # Under launchd stdout is redirected to a file of its own, so a stream
    # handler would only duplicate records. Attach one for interactive runs.
    if sys.stdout.isatty():
        handlers.append(logging.StreamHandler(sys.stdout))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )


# ─── PICKLIST OPTION RESOLVER ─────────────────────────────────────────────────

def _is_uuid(val: str) -> bool:
    return isinstance(val, str) and len(val) == 36 and val.count("-") == 4


def _try_cache_picklist_options(body):
    """
    Inspect any Sprinklr API response and extract picklist option definitions
    into _PICKLIST_OPTION_CACHE (uuid → label).  We look for any list of
    objects that carry both a UUID-shaped id and a name/label/value string.
    """
    candidates = []
    if isinstance(body, list):
        candidates = body
    elif isinstance(body, dict):
        for key in ("options", "entities", "data", "items", "values",
                    "fieldValues", "enumValues", "choices"):
            v = body.get(key)
            if isinstance(v, list):
                candidates = v
                break
        # Recurse one level into common wrappers
        if not candidates:
            for key in ("data", "result"):
                v = body.get(key)
                if isinstance(v, dict):
                    _try_cache_picklist_options(v)

    for item in candidates:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or item.get("uuid") or item.get("value") or "")
        label   = (item.get("label") or item.get("name") or
                   item.get("displayName") or item.get("title") or "")
        if _is_uuid(item_id) and label and len(label) < 120:
            _PICKLIST_OPTION_CACHE[item_id] = label


def _resolve_picklist_value(uuid_val: str) -> str:
    """Return a human label for a picklist UUID, or the UUID if unknown."""
    return _PICKLIST_OPTION_CACHE.get(uuid_val, uuid_val)


# ─── BROWSER / DATA FETCHING ──────────────────────────────────────────────────

def fetch_cases_from_dashboard() -> list:
    """
    Load the Sprinklr dashboard headlessly using the saved session.
    Intercepts JSON responses to:
      1. Extract case/task entities from stream feed endpoints.
      2. Populate _PICKLIST_OPTION_CACHE with any picklist option definitions.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        # Naming this interpreter matters: pip3 on PATH installs into the
        # Homebrew build, which is not the one launchd runs.
        log.error("Playwright not installed. Run: \"%s\" -m pip install playwright "
                  "&& \"%s\" -m playwright install chromium", sys.executable, sys.executable)
        sys.exit(1)

    if not os.path.exists(SESSION_FILE):
        log.error("No session file. Run:  %s", RELOGIN_CMD)
        raise RuntimeError(SESSION_EXPIRED)

    captured = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            storage_state=SESSION_FILE,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        def on_response(response):
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            url = response.url
            if any(x in url for x in [".js", "analytics", "google", "sprcdn",
                                        "segment", "mixpanel", "amplitude"]):
                return
            try:
                body = response.json()
            except Exception:
                return

            # Always try to harvest picklist option definitions
            _try_cache_picklist_options(body)

            # Extract task/case entities from stream feed responses
            items = _extract_items(body, url)
            if items:
                log.info("  → %d items from: %s", len(items), url)
                captured.extend(items)

        page.on("response", on_response)

        log.info("Loading dashboard …")
        try:
            page.goto(DASHBOARD_URL, wait_until="networkidle", timeout=35_000)
        except Exception:
            pass

        current_url = page.url
        log.info("Post-load URL: %s", current_url)

        if ("login" in current_url
                or "netflix-app.sprinklr.com" in current_url
                or "tfa" in current_url):
            browser.close()
            log.error("SESSION EXPIRED — run:  %s", RELOGIN_CMD)
            raise RuntimeError(SESSION_EXPIRED)

        log.info("Waiting for stream data (20 s) …")
        page.wait_for_timeout(20_000)

        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(3_000)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(2_000)
        except Exception:
            pass

        context.storage_state(path=SESSION_FILE)
        browser.close()

    log.info("Picklist options resolved: %d", len(_PICKLIST_OPTION_CACHE))
    log.info("Total items captured: %d", len(captured))
    return captured


def _extract_items(body, url: str) -> list:
    if "outbound/stream" in url and isinstance(body, dict):
        return body.get("entities", [])

    candidates = []
    if isinstance(body, list):
        candidates = body
    elif isinstance(body, dict):
        for key in ("entities", "data", "items", "rows", "results",
                    "cases", "messages", "content", "records"):
            val = body.get(key)
            if isinstance(val, list):
                candidates = val
                break
        if not candidates and isinstance(body.get("data"), dict):
            for key in ("entities", "rows", "items", "cases", "messages"):
                val = body["data"].get(key)
                if isinstance(val, list):
                    candidates = val
                    break

    case_keys = {
        "caseId", "messageId", "subject", "caseType", "caseStatus",
        "createdTime", "publishedTime", "userProfile", "author",
        "description", "assignedTo", "spaceId",
        "universalMessageId", "snCreatedTime", "accountId",
    }
    return [
        item for item in candidates
        if isinstance(item, dict) and (
            case_keys.intersection(item.keys())
            or ("status" in item and "message" in item)
        )
    ]


# ─── ENTITY HELPERS ───────────────────────────────────────────────────────────

def extract_case_id(item: dict) -> str:
    for key in ("id", "caseId", "messageId", "externalId"):
        val = item.get(key)
        if val:
            return str(val)
    return ""


def _fmt_ts(ts) -> str:
    if not ts:
        return "N/A"
    try:
        return datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(ts)


def _due_date_str(item: dict):
    ts = item.get("dueDate")
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return None


def _form_responses_text(responses: list) -> str:
    """
    Convert Sprinklr intake form responses into clean, labelled text.
    - Text / textarea fields: labelled via FIELD_LABELS
    - Picklist fields: labelled via PICKLIST_FIELD_LABELS; values resolved
      from _PICKLIST_OPTION_CACHE (UUID → human label)
    - Fields with no readable value are skipped
    """
    lines = []
    for r in responses:
        field_name   = r.get("fieldName", "")    # e.g. "PICKLIST_8357", "TEXT_2846"
        relative_key = r.get("relativeKey", "")  # e.g. "field_1"
        raw          = r.get("rawData", "")
        summary      = r.get("summary", [])

        is_picklist = field_name.startswith("PICKLIST_")

        if is_picklist:
            # Resolve UUID(s) → human label
            raw_vals = [raw] if isinstance(raw, str) else (raw or [])
            resolved = [
                _resolve_picklist_value(v)
                for v in raw_vals
                if v and not v.startswith("Not Set")
            ]
            # Skip if still all UUIDs (unresolved)
            if not resolved or all(_is_uuid(v) for v in resolved):
                continue
            val   = ", ".join(resolved)
            label = PICKLIST_FIELD_LABELS.get(field_name, field_name)

        else:
            # Text / textarea: use summary if available, else rawData
            if summary:
                val = summary[0] if len(summary) == 1 else "\n    ".join(str(s) for s in summary)
            elif isinstance(raw, list):
                readable = [str(v) for v in raw if v and not _is_uuid(str(v))]
                if not readable:
                    continue
                val = ", ".join(readable)
            elif isinstance(raw, str):
                if _is_uuid(raw) or not raw.strip():
                    continue
                val = raw
            else:
                val = str(raw) if raw else ""

            if not val.strip() or val.strip() == "Not Set":
                continue

            label = (
                FIELD_LABELS.get(relative_key)
                or FIELD_LABELS.get(field_name)
                or relative_key
                or field_name
                or "field"
            )

        val_indented = val.replace("\n", "\n    ")
        lines.append(f"  {label}: {val_indented}")

    return "\n".join(lines) if lines else "  (no form responses)"


# ─── TASK CONTENT BUILDERS ────────────────────────────────────────────────────

def build_task_name(item: dict) -> str:
    request_num = (item.get("taskObject") or {}).get("intakeResponseId")
    prefix = f"#{request_num} — " if request_num else ""
    for key in ("assetDescription", "title", "subject", "name"):
        val = (item.get(key) or "").strip()
        if val:
            return f"{prefix}{val}"
    return f"{prefix}Sprinklr Request – {extract_case_id(item) or 'Unknown'}"


def build_task_notes(item: dict) -> str:
    item_id        = extract_case_id(item)
    request_num    = (item.get("taskObject") or {}).get("intakeResponseId", "")
    request_type   = item.get("assetDescription") or item.get("taskType") or "N/A"
    task_status    = item.get("taskStatus", "N/A").replace("_", " ").title()
    request_status = (
        (item.get("assetDetail") or {})
        .get("mappedCustomProperties", {})
        .get("request_status", ["N/A"])[0]
    )
    submitted  = _fmt_ts(item.get("createdTime"))
    responses  = (item.get("taskObject") or {}).get("responses", [])
    form_lines = _form_responses_text(responses)
    deep_link  = f"{DASHBOARD_URL}?selectedTask={item_id}"

    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Source:           Sprinklr – Request Form\n"
        f"Request #:        {request_num}\n"
        f"Case ID:          {item_id}\n"
        f"Request Type:     {request_type}\n"
        f"Sprinklr Status:  {task_status}  |  Request: {request_status}\n"
        f"Submitted:        {submitted}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"\nForm Responses:\n{form_lines}\n"
        f"\nView in Sprinklr:\n{deep_link}"
    )


# ─── ASANA ────────────────────────────────────────────────────────────────────

def _asana_headers() -> dict:
    return {
        "Authorization": f"Bearer {ASANA_PAT}",
        "Content-Type":  "application/json",
    }


_USER_CACHE: dict = {}

def _load_user_cache():
    global _USER_CACHE
    if _USER_CACHE:
        return
    try:
        r = requests.get(
            f"{ASANA_BASE_URL}/projects/{ASANA_PROJECT_GID}",
            headers=_asana_headers(),
            params={"opt_fields": "workspace.gid"},
            timeout=15,
        )
        r.raise_for_status()
        workspace_gid = r.json()["data"]["workspace"]["gid"]

        r2 = requests.get(
            f"{ASANA_BASE_URL}/workspaces/{workspace_gid}/users",
            headers=_asana_headers(),
            params={"opt_fields": "gid,name"},
            timeout=15,
        )
        r2.raise_for_status()
        for u in r2.json().get("data", []):
            _USER_CACHE[u["name"].lower()] = u["gid"]

        log.info("Asana user cache: %d members", len(_USER_CACHE))
    except Exception as e:
        log.warning("Could not load Asana user cache: %s", e)


def _lookup_user(display_name: str):
    return _USER_CACHE.get((display_name or "").lower())


def _build_payload(item: dict, new_task: bool = False) -> dict:
    data: dict = {
        "name":      build_task_name(item),
        "notes":     build_task_notes(item),
        "completed": item.get("taskStatus", "") == "COMPLETED",
    }
    due = _due_date_str(item)
    if due:
        data["due_on"] = due
    gid = _lookup_user(item.get("assigneeName", ""))
    if gid:
        data["assignee"] = gid
    if new_task:
        data["projects"] = [ASANA_PROJECT_GID]
    return {"data": data}


def create_asana_task(item: dict) -> dict:
    r = requests.post(
        f"{ASANA_BASE_URL}/tasks",
        headers=_asana_headers(),
        json=_build_payload(item, new_task=True),
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Create failed [{r.status_code}]: {r.text[:300]}")
    return r.json()["data"]


def update_asana_task(gid: str, item: dict) -> dict:
    r = requests.put(
        f"{ASANA_BASE_URL}/tasks/{gid}",
        headers=_asana_headers(),
        json=_build_payload(item),
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Update failed [{r.status_code}]: {r.text[:300]}")
    return r.json()["data"]


def fetch_all_asana_tasks() -> list:
    tasks, offset = [], None
    while True:
        params = {
            "project":    ASANA_PROJECT_GID,
            "opt_fields": "gid,notes",
            "limit":      100,
        }
        if offset:
            params["offset"] = offset
        r = requests.get(
            f"{ASANA_BASE_URL}/tasks",
            headers=_asana_headers(),
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        tasks.extend(data["data"])
        nxt = data.get("next_page")
        if nxt and nxt.get("offset"):
            offset = nxt["offset"]
        else:
            break
    return tasks


# ─── STATE ────────────────────────────────────────────────────────────────────
#
# Schema v2:
# {
#   "cases": {
#     "<case_id>": { "asana_gid": "<gid>", "last_hash": "<md5>" }
#   },
#   "last_run": "<iso>"
# }

def _case_hash(item: dict) -> str:
    key = {
        "assigneeName":     item.get("assigneeName", ""),
        "taskStatus":       item.get("taskStatus", ""),
        "request_status":   (item.get("assetDetail") or {})
                            .get("mappedCustomProperties", {})
                            .get("request_status", [""])[0],
        "assetDescription": item.get("assetDescription", ""),
        "dueDate":          str(item.get("dueDate", "")),
    }
    return hashlib.md5(json.dumps(key, sort_keys=True).encode()).hexdigest()


def _case_id_from_notes(notes: str) -> str:
    m = _re.search(r"Case ID:\s*(\S+)", notes or "")
    return m.group(1) if m else ""


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"cases": {}}
    with open(STATE_FILE) as f:
        data = json.load(f)
    # Migrate old flat list format → v2
    if "processed_ids" in data and "cases" not in data:
        log.info("Migrating state to v2 — fetching Asana GIDs …")
        gid_map = {
            _case_id_from_notes(t.get("notes", "")): t["gid"]
            for t in fetch_all_asana_tasks()
            if _case_id_from_notes(t.get("notes", ""))
        }
        cases = {
            cid: {"asana_gid": gid_map.get(cid), "last_hash": None}
            for cid in data.get("processed_ids", [])
        }
        log.info("  ✓  Migrated %d cases", len(cases))
        return {"cases": cases}
    return data


def save_state(state: dict):
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ─── MAIN SYNC ────────────────────────────────────────────────────────────────

def run_sync():
    log.info("══════════════════════════════════════════════")
    log.info("  Netflix Sprinklr → Asana  |  %s",
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    log.info("══════════════════════════════════════════════")

    state       = load_state()
    cases_state = state.setdefault("cases", {})
    log.info("Previously tracked: %d cases", len(cases_state))

    _load_user_cache()

    try:
        cases = fetch_cases_from_dashboard()
    except RuntimeError as e:
        if SESSION_EXPIRED in str(e):
            log.error("Session expired. Run:  %s", RELOGIN_CMD)
            health.write_heartbeat("session_expired")
            # Recovery needs a human for 2FA, so say so out loud rather than
            # failing quietly every 15 minutes.
            health.notify(
                "Sprinklr → Asana is down",
                # A notification banner truncates, so point at the one-click fix
                # rather than a long absolute path. The log line above has the
                # exact command for anyone who wants it.
                "Session expired — double-click restart.command to log back in.",
                "session_expired",
            )
            sys.exit(1)
        raise

    # ── Deduplication guard ───────────────────────────────────────────────────
    # Before creating anything, scan Asana for tasks that already exist.
    # This prevents duplicates if the state file was deleted or reset.
    untracked = [c for c in cases
                 if extract_case_id(c) and extract_case_id(c) not in cases_state]
    if untracked:
        log.info("Checking Asana for pre-existing tasks (%d untracked) …", len(untracked))
        try:
            existing = fetch_all_asana_tasks()
            gid_map  = {
                _case_id_from_notes(t.get("notes", "")): t["gid"]
                for t in existing
                if _case_id_from_notes(t.get("notes", ""))
            }
            imported = 0
            for case in untracked:
                cid = extract_case_id(case)
                if cid in gid_map:
                    cases_state[cid] = {"asana_gid": gid_map[cid], "last_hash": None}
                    imported += 1
            if imported:
                log.info("  ✓  Linked %d existing tasks — will update, not duplicate", imported)
        except Exception as e:
            log.warning("Could not pre-check Asana: %s", e)

    new_count = updated_count = error_count = 0

    for case in cases:
        case_id = extract_case_id(case)
        if not case_id:
            continue

        current_hash = _case_hash(case)

        # ── New submission ────────────────────────────────────────────────────
        if case_id not in cases_state:
            try:
                task = create_asana_task(case)
                cases_state[case_id] = {
                    "asana_gid": task["gid"],
                    "last_hash": current_hash,
                }
                new_count += 1
                log.info("  ✓  NEW  %s → '%s'  assignee=%s  due=%s  complete=%s",
                         case_id, task["name"][:45],
                         case.get("assigneeName", "—"),
                         _due_date_str(case) or "—",
                         case.get("taskStatus") == "COMPLETED")
            except Exception as exc:
                error_count += 1
                log.error("  ✗  NEW  %s failed: %s", case_id, exc)
            continue

        # ── Existing — check for changes ──────────────────────────────────────
        record    = cases_state[case_id]
        asana_gid = record.get("asana_gid")

        if record.get("last_hash") == current_hash:
            continue  # nothing changed

        if not asana_gid:
            log.warning("  ⚠  %s changed but no Asana GID — skipping", case_id)
            continue

        try:
            update_asana_task(asana_gid, case)
            record["last_hash"] = current_hash
            updated_count += 1
            log.info("  ↻  UPD  %s  assignee=%s  status=%s  due=%s",
                     case_id,
                     case.get("assigneeName", "—"),
                     case.get("taskStatus", "—"),
                     _due_date_str(case) or "—")
        except Exception as exc:
            error_count += 1
            log.error("  ✗  UPD  %s failed: %s", case_id, exc)

    save_state(state)
    log.info("Done — %d new, %d updated, %d errors", new_count, updated_count, error_count)
    log.info("══════════════════════════════════════════════\n")

    detail = f"{new_count} new, {updated_count} updated, {error_count} errors"
    health.write_heartbeat("errors" if error_count else "ok", detail)
    if error_count:
        health.notify("Sprinklr → Asana had errors", detail, "sync_errors")


# ─── DIAGNOSTIC ───────────────────────────────────────────────────────────────

def run_test():
    print("\n🔍  DIAGNOSTIC MODE — no Asana writes\n")
    if not os.path.exists(SESSION_FILE):
        print(f"  ✗  No session file.  Run:  {RELOGIN_CMD}\n")
        sys.exit(1)

    logging.getLogger().setLevel(logging.WARNING)
    print("Step 1/3 — Loading dashboard …")
    try:
        cases = fetch_cases_from_dashboard()
    except RuntimeError as e:
        if SESSION_EXPIRED in str(e):
            print(f"  ✗  Session expired.  Run:  {RELOGIN_CMD}\n")
            sys.exit(1)
        raise
    finally:
        logging.getLogger().setLevel(logging.INFO)

    print(f"  ✓  {len(cases)} items  |  {len(_PICKLIST_OPTION_CACHE)} picklist options resolved\n")
    if cases:
        print("  Sample task preview:\n")
        print(f"  Name:  {build_task_name(cases[0])}")
        print(f"  Notes preview:\n")
        for line in build_task_notes(cases[0]).splitlines()[:12]:
            print(f"    {line}")
        print("    …")

    print("\nStep 2/3 — Asana …")
    try:
        r = requests.get(
            f"{ASANA_BASE_URL}/projects/{ASANA_PROJECT_GID}",
            headers=_asana_headers(), timeout=15,
        )
        r.raise_for_status()
        print(f"  ✓  Project: '{r.json()['data']['name']}'")
    except Exception as e:
        print(f"  ✗  {e}")
        sys.exit(1)

    print("\nStep 3/3 — Last run …")
    # Session file age says nothing about whether the sync is running — it
    # persists happily while the job is dead. Report the heartbeat instead.
    import time as _t
    h = health.read_health()
    if h.get("lastRunTs"):
        age_m = int((_t.time() - int(h["lastRunTs"])) // 60)
        print(f"  ✓  {age_m}m ago — result={h.get('lastResult')}, {h.get('lastDetail', '')}")
        if h.get("lastSuccessTs"):
            ok_m = int((_t.time() - int(h["lastSuccessTs"])) // 60)
            print(f"      last success {ok_m}m ago")
        else:
            print("      no successful run recorded")
    else:
        print("  ?  No heartbeat yet — scheduled run status unknown")
    print("\n✅  Connectivity checks passed.\n")


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sprinklr → Asana sync")
    parser.add_argument("--test", action="store_true", help="Connectivity check, no writes")
    args = parser.parse_args()
    configure_logging()
    if args.test:
        run_test()
    else:
        try:
            run_sync()
        except SystemExit as exc:
            # sys.exit() from deeper code (missing playwright, for one) would
            # otherwise leave no heartbeat, so the watchdog would keep reporting
            # the last good result while every run died.
            if exc.code and not health.wrote_heartbeat():
                health.write_heartbeat("exit", f"exited with code {exc.code}")
                health.notify(
                    "Sprinklr → Asana exited early",
                    f"Exit code {exc.code}. Check the log.",
                    "early_exit",
                )
            raise
        except Exception as exc:
            health.write_heartbeat("crash", f"{type(exc).__name__}: {exc}")
            health.notify("Sprinklr → Asana crashed", f"{type(exc).__name__}: {exc}"[:180], "crash")
            raise
