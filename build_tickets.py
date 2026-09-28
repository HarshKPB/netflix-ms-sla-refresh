#!/usr/bin/env python3
"""Build web/tickets.json from Gmail dumps of the Sprinklr support desk.

One row = one Sprinklr support CASE (keyed by the 7-digit Ticket Number that
Sprinklr stamps in every email). A single case can span several Gmail threads,
so threads are grouped by ticket number.

Input : one or more JSON files in the Gmail `search_threads` shape. Each message
        must carry sender, toRecipients, ccRecipients, date, subject, snippet.
Output: web/tickets.json = {"generated","generated_at","source","rows":[...]}.

Per case we emit:
  case_id      Sprinklr Ticket Number (7-digit) or "" when none was found
  title        cleaned first subject
  topic        platform/function guess from the text
  priority     Urgent / High / Normal (from subject tags)
  status       Awaiting us / Awaiting Sprinklr / Resolved  (see below)
  owner        the Premium Blend / Netflix person on the case
  agent        latest Sprinklr support agent who replied
  opened       first message date
  last_update  latest message date
  age_days     days since last_update (as of the run date)
  n_msgs       total messages across the case
  portal_link  https://community.sprinklr.com/support/requests/<id>  (source of truth)
  gmail_link   Gmail thread link

Status model = "who holds the ball", read only from the LATEST message so that
resolution wording quoted in older history does not create false positives:
  - latest message top says survey/solved/resolved   -> "Resolved"
  - latest message is from Sprinklr                   -> "Awaiting us"
  - latest message is from us                          -> "Awaiting Sprinklr"
Status is a heuristic; the portal Review Link is authoritative. The MS owner
should treat "Awaiting us" as their action queue.

This is the sample/offline builder. The scheduled job will produce the same
input via the Gmail API, then call this transform unchanged.
"""
import json, re, sys
from collections import Counter
from datetime import date, datetime, timezone

SPRINKLR = ("sprinklrsupport.com", "sprinklr.com")
OURS = ("premium-blend.com", "netflix.com", "netflixcontractors.com")
GROUP_ALIAS = "netflix@premium-blend.com"

PLATFORMS = [
    ("TikTok", ("tiktok", " tt ", "tt smart")), ("YouTube", ("youtube", " yt ", "masthead")),
    ("Instagram", ("instagram", " ig ", "reels")),
    ("X/Twitter", ("twitter", " x ", "x/", "x campaign", "tweet", "4k video")),
    ("Meta", ("meta ", "facebook", " fb ")), ("Threads", ("threads",)),
    ("Bluesky", ("bluesky",)), ("LinkedIn", ("linkedin",)), ("Snapchat", ("snapchat",)),
]
FUNCTIONS = [
    ("Publishing", ("publish", "scheduled post", "post failure", "feeds", "feed")),
    ("Reporting", ("report", "dashboard", "widget")),
    ("Analytics", ("analytic", "metric", "reach", "frequency", "views", "engagement")),
    ("Paid/Spend", ("boost", "spend", "spent", "paid", "campaign", "ads manager", "smart+")),
    ("API", ("api",)), ("Export", ("export", "backfill")),
    ("Access", ("access", "sharing config", "person app")),
    ("Moderation", ("moderation", "activity trail", "inbound")),
    ("Data quality", ("data fetch", "date issue", "discrepancy", "inconsistent", "missing data", "tagging")),
]

WEEKDAY = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
RE_TICKET = re.compile(r"Ticket\s*Number[\s:]*?(\d{6,8})", re.I)
RE_REVIEW = re.compile(r"(https://community\.sprinklr\.com/support/requests/\d+)", re.I)
RE_AGENT = re.compile(r"requests/\d+\s+(.+?)\s+" + WEEKDAY + r",", re.I)
RESOLVED_HINTS = (
    "satisfaction survey", "assume the case is solved", "has been resolved",
    "marking this as resolved", "marking this resolved", "we are closing",
    "case is now closed", "case has been closed", "glad we could", "resolved your issue",
)


def domain(addr):
    return addr.split("@")[-1].strip().lower() if addr else ""


def is_sprinklr(addr):
    d = domain(addr)
    return d.endswith("sprinklr.com") or d.endswith("sprinklrsupport.com")


def is_ours(addr):
    d = domain(addr)
    return any(d == o or d.endswith("." + o) for o in OURS)


def clean_title(subj):
    s = subj or ""
    while re.match(r"^\s*(re|fwd|fw|automatic reply|accepted|declined|canceled)\s*:\s*", s, re.I):
        s = re.sub(r"^\s*(re|fwd|fw|automatic reply|accepted|declined|canceled)\s*:\s*", "", s, flags=re.I)
    s = re.sub(r"\s*\[(high priority|urgent|external)\]\s*", " ", s, flags=re.I)
    s = re.sub(r"\s{2,}", " ", s).strip()
    # drop the redundant partner prefix (every case here is Netflix)
    s = re.sub(r"^(netflix|nflx)\s*[:|–—-]\s*", "", s, flags=re.I)
    s = re.sub(r"\s{2,}", " ", s).strip(" -|:–—\t")
    return s.strip()


def detect_priority(text):
    t = text.lower()
    if "urgent" in t:
        return "Urgent"
    if "high priority" in t or "[high" in t:
        return "High"
    return "Normal"


def detect_topic(text):
    t = " " + text.lower() + " "
    for name, kws in PLATFORMS:
        if any(k in t for k in kws):
            return name
    for name, kws in FUNCTIONS:
        if any(k in t for k in kws):
            return name
    return "General"


def parse_dt(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except Exception:
        return None


def first_str(m, key):
    v = m.get(key)
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


def all_addrs(m):
    out = []
    for k in ("sender", "toRecipients", "ccRecipients"):
        v = m.get(k)
        if isinstance(v, list):
            out += v
        elif v:
            out.append(v)
    return out


def ticket_of(m):
    blob = (m.get("snippet") or "") + " " + (m.get("plaintextBody") or "")
    mt = RE_TICKET.search(blob)
    return mt.group(1) if mt else ""


def review_of(m):
    blob = (m.get("snippet") or "") + " " + (m.get("plaintextBody") or "")
    mr = RE_REVIEW.search(blob)
    return mr.group(1) if mr else ""


def agent_of(m):
    blob = m.get("snippet") or m.get("plaintextBody") or ""
    ma = RE_AGENT.search(blob)
    if not ma:
        return ""
    name = ma.group(1).strip()
    if name.lower() in ("sprinklrsystem", "sprinklr support", "s sprinklr support"):
        return ""  # automated, not a human agent
    # snippets sometimes prefix a one-letter avatar initial, e.g. "V Varsha"
    name = re.sub(r"^[A-Z]\s+(?=[A-Z][a-z])", "", name)
    return name[:40]


def main(argv):
    if len(argv) < 2:
        print("usage: build_tickets.py <gmail_search.json> [more.json ...] [-o out.json]", file=sys.stderr)
        return 2
    out, inputs, i = "web/tickets.json", [], 1
    while i < len(argv):
        if argv[i] == "-o":
            out = argv[i + 1]; i += 2; continue
        inputs.append(argv[i]); i += 1

    asof = date.today()

    # collect messages grouped by ticket number (fallback: thread id)
    groups = {}         # key -> {"msgs":[...], "threads":set(), "gmail":str}
    seen_threads = set()
    for path in inputs:
        with open(path) as f:
            data = json.load(f)
        for th in data.get("threads", []):
            tid = th.get("id")
            if tid in seen_threads:
                continue
            seen_threads.add(tid)
            msgs = th.get("messages", [])
            tnum = ""
            for m in msgs:
                tnum = ticket_of(m)
                if tnum:
                    break
            key = tnum or ("thread:" + str(tid))
            g = groups.setdefault(key, {"msgs": [], "threads": set(), "gmail": th.get("viewUrl", ""), "tnum": tnum})
            g["msgs"].extend(msgs)
            g["threads"].add(tid)
            if tnum and not g.get("tnum"):
                g["tnum"] = tnum
            if not g["gmail"]:
                g["gmail"] = th.get("viewUrl", "")

    rows = []
    for key, g in groups.items():
        msgs = sorted(g["msgs"], key=lambda m: m.get("date", ""))
        if not msgs:
            continue
        first, last = msgs[0], msgs[-1]
        subjects = " || ".join(m.get("subject", "") for m in msgs)

        # status from the latest message only
        last_top = (last.get("snippet") or "")[:400].lower()
        last_sender = first_str(last, "sender")
        if any(h in last_top for h in RESOLVED_HINTS):
            status = "Resolved"
        elif is_sprinklr(last_sender):
            status = "Awaiting us"
        elif is_ours(last_sender):
            status = "Awaiting Sprinklr"
        else:
            status = "Awaiting us"

        # owner = the Premium Blend / Netflix person on the case
        pb = [a for a in (x for m in msgs for x in all_addrs(m))
              if is_ours(a) and a.lower() != GROUP_ALIAS]
        if pb:
            owner = Counter(a.lower() for a in pb).most_common(1)[0][0].split("@")[0]
        else:
            owner = "netflix (group)"

        # latest human Sprinklr agent
        agent = ""
        for m in reversed(msgs):
            if is_sprinklr(first_str(m, "sender")):
                a = agent_of(m)
                if a:
                    agent = a; break

        portal = ""
        for m in msgs:
            portal = review_of(m)
            if portal:
                break

        opened_dt, last_dt = parse_dt(first.get("date", "")), parse_dt(last.get("date", ""))
        rows.append({
            "case_id": g.get("tnum") or "",
            "title": clean_title(first.get("subject", "")),
            "topic": detect_topic(subjects),
            "priority": detect_priority(subjects),
            "status": status,
            "owner": owner,
            "agent": agent,
            "opened": opened_dt.date().isoformat() if opened_dt else "",
            "last_update": last_dt.date().isoformat() if last_dt else "",
            "age_days": (asof - last_dt.date()).days if last_dt else None,
            "n_msgs": len(msgs),
            "portal_link": portal,
            "gmail_link": g["gmail"],
        })

    rows.sort(key=lambda r: r.get("last_update", ""), reverse=True)
    payload = {
        "generated": asof.isoformat(),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "source": "Gmail: care@prod.sprinklrsupport.com",
        "rows": rows,
    }
    with open(out, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    st, pr = Counter(r["status"] for r in rows), Counter(r["priority"] for r in rows)
    withid = sum(1 for r in rows if r["case_id"])
    print(f"wrote {out}: {len(rows)} cases ({withid} with a ticket number)", file=sys.stderr)
    print(f"  status:   {dict(st)}", file=sys.stderr)
    print(f"  priority: {dict(pr)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
