"""The one place the classification rules live.

classify_case(messages) takes all messages of a single Sprinklr case (already
grouped by ticket number, any order) and returns the derived fields. Every rule
that used to be scattered across build_tickets.py, classify_status.py,
merge_status.py and the routine prompt is consolidated here. See METHODOLOGY.md
for the plain-language statement of these rules.
"""
import re
from datetime import datetime

SPRINKLR = ("sprinklr.com", "sprinklrsupport.com")
OURS = ("premium-blend.com", "netflix.com", "netflixcontractors.com")
GROUP_ALIAS = "netflix@premium-blend.com"

WEEKDAY = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
MON = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
RE_TICKET = re.compile(r"Ticket\s*Number[\s:]*?(\d{6,8})", re.I)
RE_REVIEW = re.compile(r"(https://community\.sprinklr\.com/support/requests/\d+)", re.I)
RE_AGENT = re.compile(r"requests/\d+\s+(.+?)\s+" + WEEKDAY, re.I)
RE_DT = re.compile(WEEKDAY + r",?\s+(\d{1,2})\s+([A-Z][a-z]{2})\s+(\d{4})\s+\d{2}:\d{2}", re.I)
RE_PBAUTH = re.compile(r"([\w.\-]+@premium-blend\.com)\s+" + WEEKDAY +
                       r",?\s+(\d{1,2})\s+([A-Z][a-z]{2})\s+(\d{4})\s+(\d{2}):(\d{2}):(\d{2})", re.I)

# The auto follow-up nudge IS the close signal (Sprinklr auto-solves on inactivity).
NUDGE = ("as an initial follow-up to this case, we are waiting to hear back",
         "assume the case is solved")
RESOLVED = ("closing this case", "closing this ticket", "closing the ticket", "closing case",
            "we are closing", "we will close", "close this ticket", "close it",
            "proceed with closing", "proceed with the closure", "closure of this ticket",
            "proceed to mark the case", "mark the case as solved", "mark this case as solved",
            "mark it as solved", "mark this thread as closed", "mark this case as closed",
            "marking case closed", "marking this case closed", "we are marking",
            "we are setting the case", "go forward and mark", "has been resolved",
            "is now resolved", "marking this resolved", "has now been corrected",
            "has been corrected", "job has been rerun", "feel free to reopen", "reopen the case",
            "do contact sprinklr support for any further queries", "reach out to us for any issues",
            "glad to hear", "glad we could", "upon further review")
ASK = ("could you", "can you please", "please confirm", "please provide", "please share",
       "please suggest", "kindly provide", "kindly confirm", "kindly share", "let us know your",
       "available time", "time slots", "hopping on a call", "hoping on a call",
       "request you to provide", "please help us with", "share a screenshot", "provide us with")
INVESTIGATING = ("we are investigating", "being investigated", "will get back", "will update you",
                 "keep you updated", "keep you posted", "still pending", "with our engineering",
                 "with our product", "with the channel team", "raised this", "raised with",
                 "escalat", "being verified", "allow us some time", "shall get back",
                 "continuing to follow", "reached out to our engineering",
                 "we will review your ticket", "working diligently", "checking further",
                 "look into this", "look into it")
ANSWER = ("hope this helps", "hope this clarifies", "we verified", "we have verified",
          "expected behaviour", "expected behavior", "please refer", "api limitation",
          "we hope this", "we have reviewed", "as a workaround", "you can use", "kindly use",
          "as designed", "working as expected")
THANKS = ("thank you", "thanks", "\U0001f44d", "amazing", "appreciate it", "perfect",
          "reacted via gmail", "great, thank", "much appreciated")

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
    ("Data quality", ("data fetch", "date issue", "discrepancy", "inconsistent",
                      "missing data", "tagging")),
]


def domain(a):
    return (a or "").split("@")[-1].strip().lower()


def is_sprinklr(a):
    d = domain(a)
    return any(d == s or d.endswith("." + s) for s in SPRINKLR)


def is_ours(a):
    d = domain(a)
    return any(d == o or d.endswith("." + o) for o in OURS)


def first_str(m, key):
    v = m.get(key)
    return (v[0] if v else "") if isinstance(v, list) else (v or "")


def recipients(m):
    out = []
    for k in ("toRecipients", "ccRecipients"):
        v = m.get(k)
        out += v if isinstance(v, list) else ([v] if v else [])
    return out


def clean_title(subj):
    s = subj or ""
    while re.match(r"^\s*(re|fwd|fw|automatic reply|accepted|declined|canceled)\s*:\s*", s, re.I):
        s = re.sub(r"^\s*(re|fwd|fw|automatic reply|accepted|declined|canceled)\s*:\s*", "", s, flags=re.I)
    s = re.sub(r"\s*\[(high priority|urgent|external)\]\s*", " ", s, flags=re.I)
    s = re.sub(r"\s{2,}", " ", s).strip()
    s = re.sub(r"^(netflix|nflx)\s*[:|–—-]\s*", "", s, flags=re.I)
    return re.sub(r"\s{2,}", " ", s).strip(" -|:–—\t")


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


def own_text(body):
    b = body or ""
    m = re.search(r"UTC\s", b)
    if m and m.start() < 400:
        b = b[m.end():]
    cuts = []
    for pat in (r"##\s*Please Reply above this line", r"\bS\s+Sprinklr Support\s+care@",
                r"%%\[Conversation", r"\bOn\s+\w{3},?\s+\w{3}\s+\d", r"\n>", RE_DT.pattern):
        mm = re.search(pat, b)
        if mm:
            cuts.append(mm.start())
    if cuts:
        b = b[:min(cuts)]
    b = re.sub(r"https?://\S+", " ", b)
    return b.strip()[:900]


def classify_status(side, text):
    t = re.sub(r"\s+", " ", text.lower())
    if any(k in t for k in RESOLVED):
        return "Resolved"
    if side == "us":
        if "?" not in t and any(k in t for k in THANKS):
            return "Resolved"
        return "Awaiting Sprinklr"
    if any(k in t for k in ASK):
        return "Awaiting us"
    if any(k in t for k in INVESTIGATING):
        return "Awaiting Sprinklr"
    if any(k in t for k in ANSWER):
        return "Resolved"
    return "Awaiting Sprinklr"


def agent_of(body):
    m = RE_AGENT.search(body or "")
    if not m:
        return ""
    name = re.sub(r"^[A-Z]\s+(?=[A-Z])", "", m.group(1).strip())
    if name.lower() in ("sprinklrsystem", "sprinklr support", "s sprinklr support"):
        return ""
    return name[:40]


def initiator(bodies):
    best = None
    for b in bodies:
        for m in RE_PBAUTH.finditer(b or ""):
            email = m.group(1).lower()
            if email == GROUP_ALIAS:
                continue
            try:
                dt = datetime(int(m.group(4)), MON[m.group(3).title()], int(m.group(2)),
                              int(m.group(5)), int(m.group(6)), int(m.group(7)))
            except Exception:
                continue
            if best is None or dt < best[0]:
                best = (dt, email)
    return best[1].split("@")[0] if best else ""


def min_date_iso(texts):
    best = None
    for t in texts:
        for m in RE_DT.finditer(t or ""):
            try:
                d = datetime(int(m.group(3)), MON[m.group(2).title()], int(m.group(1)))
            except Exception:
                continue
            if best is None or d < best:
                best = d
    return best.date().isoformat() if best else ""


def ticket_number(messages):
    for m in messages:
        x = RE_TICKET.search((m.get("plaintextBody") or m.get("snippet") or ""))
        if x:
            return x.group(1)
    return ""


def review_link(messages):
    for m in messages:
        x = RE_REVIEW.search((m.get("plaintextBody") or m.get("snippet") or ""))
        if x:
            return x.group(1)
    return ""


def classify_case(messages, gmail_link=""):
    """Derive the classifier fields for one case from its messages."""
    msgs = sorted(messages, key=lambda m: m.get("date", ""))
    bodies = [m.get("plaintextBody", "") or m.get("snippet", "") for m in msgs]
    subjects = " || ".join(m.get("subject", "") for m in msgs)
    ticket = ticket_number(msgs)

    # owner = earliest PB author in the bodies (initiator); fall back to metadata
    owner = initiator(bodies)
    if not owner:
        for m in msgs:
            if domain(first_str(m, "sender")).endswith("premium-blend.com") and \
                    first_str(m, "sender").lower() != GROUP_ALIAS:
                owner = first_str(m, "sender").split("@")[0]
                break
    if not owner:
        m0 = msgs[0]
        tos = [a for a in recipients(m0)
               if domain(a).endswith("premium-blend.com") and a.lower() != GROUP_ALIAS]
        owner = tos[0].split("@")[0] if tos else "unassigned"

    # status from the latest message (nudge = resolved), plus evidence + confidence
    last = msgs[-1]
    last_body = (last.get("plaintextBody") or last.get("snippet") or "")
    if any(n in last_body.lower() for n in NUDGE):
        status = "Resolved"
        note = "auto follow-up nudge (Sprinklr auto-closing the case)"
    else:
        side = "us" if is_ours(first_str(last, "sender")) else "sprinklr"
        note = own_text(last_body)
        status = classify_status(side, note)
    hard = ("clos", "solved", "corrected", "rerun", "could you", "please ", "kindly",
            "time slot", "thank", "\U0001f44d", "waiting to hear back",
            "assume the case is solved", "glad", "reopen", "do contact", "hope this")
    needs_review = not any(h in note.lower() for h in hard)

    # latest human Sprinklr agent
    agent = ""
    for m in reversed(msgs):
        if is_sprinklr(first_str(m, "sender")):
            a = agent_of(m.get("plaintextBody", "") or m.get("snippet", ""))
            if a:
                agent = a
                break

    opened = min_date_iso(bodies) or (msgs[0].get("date", "")[:10])
    last_update = msgs[-1].get("date", "")[:10]
    return {
        "case_id": ticket,
        "title": clean_title(msgs[0].get("subject", "")),
        "topic": detect_topic(subjects),
        "priority": detect_priority(subjects),
        "status": status,
        "owner": owner,
        "agent": agent,
        "opened": opened,
        "last_update": last_update,
        "n_msgs": len(msgs),
        "portal_link": review_link(msgs) or (
            f"https://community.sprinklr.com/support/requests/{ticket}" if ticket else ""),
        "gmail_link": gmail_link,
        "evidence": re.sub(r"\s+", " ", note)[:160],
        "needs_review": needs_review,
        "source": "Auto",
    }
