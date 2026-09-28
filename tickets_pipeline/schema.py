"""The one data contract for a tickets.json row.

validate() is called on every build. If any row is missing a field or has a
wrong type or an out-of-range enum, the build raises and nothing is written or
deployed. This is what makes a field-name drift (the old days_open vs age_days
blank column) impossible to ship: the page reads these exact names, and the
build guarantees they exist.
"""

# field name -> expected python type
FIELDS = {
    "case_id": str,
    "title": str,
    "topic": str,
    "priority": str,
    "status": str,
    "owner": str,
    "agent": str,
    "opened": str,
    "last_update": str,
    "days_open": int,
    "days_idle": int,
    "n_msgs": int,
    "portal_link": str,
    "gmail_link": str,
    "evidence": str,
    "needs_review": bool,
    "source": str,
}
STATUSES = {"Awaiting us", "Awaiting Sprinklr", "Resolved"}
PRIORITIES = {"Urgent", "High", "Normal"}
SOURCES = {"Auto", "Override"}


def row_errors(r):
    errs = []
    for k, t in FIELDS.items():
        if k not in r:
            errs.append(f"missing '{k}'")
            continue
        v = r[k]
        # bool is a subclass of int; guard the int fields against bools
        if t is int and isinstance(v, bool):
            errs.append(f"'{k}' should be int, got bool")
        elif not isinstance(v, t):
            errs.append(f"'{k}' should be {t.__name__}, got {type(v).__name__}")
    if r.get("status") not in STATUSES:
        errs.append(f"status not one of {sorted(STATUSES)}: {r.get('status')!r}")
    if r.get("priority") not in PRIORITIES:
        errs.append(f"priority invalid: {r.get('priority')!r}")
    if r.get("source") not in SOURCES:
        errs.append(f"source invalid: {r.get('source')!r}")
    return errs


def validate(rows):
    """Return {case_id: [errors]} for every non-conforming row (empty = all good)."""
    bad = {}
    for r in rows:
        e = row_errors(r)
        if e:
            bad[r.get("case_id") or r.get("title") or "?"] = e
    return bad


def assert_valid(rows):
    bad = validate(rows)
    if bad:
        lines = "\n".join(f"  {k}: {', '.join(v)}" for k, v in bad.items())
        raise ValueError(f"schema validation failed for {len(bad)} row(s):\n{lines}")
