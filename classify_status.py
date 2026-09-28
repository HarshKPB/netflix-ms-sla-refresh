#!/usr/bin/env python3
"""Classify Sprinklr support-case status from full email bodies.

Reads a directory of Gmail `get_thread` JSON dumps (one per thread, each message
carrying sender/date/plaintextBody) and prints JSON:
  { "<ticket>": {status, opened_est, last_update, agent, latest_note, last_side} }

Status is read from the latest SUBSTANTIVE message (Sprinklr's automated
"waiting to hear back" nudges are ignored):
  - closing / close wording (either side)      -> "Resolved"
  - last real message from us                  -> "Awaiting Sprinklr"
  - Sprinklr asks us for something / answered  -> "Awaiting us"
  - Sprinklr investigating / with engineering  -> "Awaiting Sprinklr"
opened_est = earliest timestamp seen anywhere in the thread text (captures the
original creation date even when it sits in quoted history).
"""
import json, re, sys, glob, os
from datetime import datetime

OURS = ("premium-blend.com", "netflix.com", "netflixcontractors.com")

AUTO = ("this is an automated response",
        "as an initial follow-up to this case, we are waiting to hear back")
RESOLVED = ("proceed with closing", "proceeding with closing", "we will close",
            "we can go ahead and close", "go ahead and close", "please close",
            "closing this ticket", "closing the ticket", "closing case",
            "has been resolved", "mark this as resolved", "marking this resolved",
            "is now resolved", "close this ticket", "we are closing")
ASK = ("could you", "can you please", "please confirm", "please provide",
       "please share", "please suggest", "kindly provide", "kindly confirm",
       "kindly share", "waiting to hear back from you", "let us know your",
       "available time", "time slots", "hopping on a call", "hoping on a call",
       "request you to provide", "please help us with", "share a screenshot",
       "provide us with")
ANSWER = ("hope this helps", "hope this clarifies", "we verified", "we have verified",
          "expected behaviour", "expected behavior", "please refer", "api limitation",
          "we observed", "we have checked", "we hope this", "any further questions",
          "shared our findings", "shared below", "as per the")
INVESTIGATING = ("investigat", "looking into", "will get back", "will update",
                 "keep you updated", "keep you posted", "still pending",
                 "with our engineering", "with our product", "with the channel team",
                 "raised this", "raised with", "escalat", "being verified",
                 "allow us some time", "shall get back", "continuing to follow",
                 "allow us to check", "we will review your ticket", "hereby acknowledge",
                 "acknowledge your request", "working diligently", "we are working",
                 "checking further", "check further", "look into this")

WEEKDAY = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
DT = re.compile(WEEKDAY + r",?\s+(\d{1,2})\s+([A-Z][a-z]{2})\s+(\d{4})\s+\d{2}:\d{2}", re.I)
MON = {m: i for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], 1)}


def dom(a):
    return (a or "").split("@")[-1].strip().lower()


def is_ours(a):
    d = dom(a)
    return any(d == o or d.endswith("." + o) for o in OURS)


def own_text(body):
    """Leading text of a single message, before quoted history."""
    b = body or ""
    # care@ messages start with a boilerplate header ending at the first 'UTC '
    m = re.search(r"UTC\s", b)
    if m and m.start() < 400:
        b = b[m.end():]
    # cut at the next quoted message marker
    cuts = []
    for pat in (r"##\s*Please Reply above this line", r"\bS\s+Sprinklr Support\s+care@",
                r"%%\[Conversation", r"\bOn\s+\w{3},?\s+\w{3}\s+\d", r"\n>",
                DT.pattern):
        mm = re.search(pat, b)
        if mm:
            cuts.append(mm.start())
    if cuts:
        b = b[:min(cuts)]
    return b.strip()[:900]


def classify(side, text):
    t = re.sub(r"\s+", " ", text.lower())
    if any(k in t for k in RESOLVED):
        return "Resolved"
    if side == "us":
        return "Awaiting Sprinklr"
    # Sprinklr side
    if any(k in t for k in ASK):
        return "Awaiting us"
    if any(k in t for k in ANSWER):
        return "Awaiting us"
    if any(k in t for k in INVESTIGATING):
        return "Awaiting Sprinklr"
    return "Awaiting Sprinklr"


def agent_of(body):
    m = re.search(r"requests/\d+\s+(.+?)\s+" + WEEKDAY, body or "")
    if not m:
        return ""
    name = re.sub(r"^[A-Z]\s+(?=[A-Z])", "", m.group(1).strip())
    if name.lower() in ("sprinklrsystem", "sprinklr support", "s sprinklr support"):
        return ""
    return name[:40]


def min_date(texts):
    best = None
    for t in texts:
        for m in DT.finditer(t or ""):
            try:
                d = datetime(int(m.group(3)), MON[m.group(2).title()], int(m.group(1)))
            except Exception:
                continue
            if best is None or d < best:
                best = d
    return best.date().isoformat() if best else ""


def process(path):
    with open(path) as f:
        d = json.load(f)
    msgs = sorted(d.get("messages", []), key=lambda m: m.get("date", ""))
    if not msgs:
        return None
    bodies = [m.get("plaintextBody", "") for m in msgs]
    ticket = ""
    for b in bodies:
        m = re.search(r"Ticket\s*Number[\s:]*?(\d{6,8})", b)
        if m:
            ticket = m.group(1); break
    # latest substantive (skip auto nudges)
    chosen = None
    for m in reversed(msgs):
        b = (m.get("plaintextBody") or "").lower()
        if any(a in b for a in AUTO):
            continue
        chosen = m; break
    if chosen is None:
        chosen = msgs[-1]
    side = "us" if is_ours(chosen.get("sender", "")) else "sprinklr"
    ot = own_text(chosen.get("plaintextBody", ""))
    status = classify(side, ot)
    last = msgs[-1]
    return ticket, {
        "status": status,
        "opened_est": min_date(bodies) or (msgs[0].get("date", "")[:10]),
        "last_update": last.get("date", "")[:10],
        "last_side": side,
        "agent": agent_of(chosen.get("plaintextBody", "")) if side == "sprinklr" else "",
        "latest_note": re.sub(r"\s+", " ", ot)[:160],
    }


def main(argv):
    d = argv[1] if len(argv) > 1 else "."
    out = {}
    for p in glob.glob(os.path.join(d, "*get_thread*.txt")):
        try:
            r = process(p)
        except Exception as e:
            print(f"skip {p}: {e}", file=sys.stderr); continue
        if r and r[0]:
            out[r[0]] = r[1]
    print(json.dumps(out, indent=2))
    print(f"classified {len(out)} cases", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
