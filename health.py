#!/usr/bin/env python3
"""
Heartbeat and alerting for the Sprinklr → Asana sync.

Deliberately free of credentials and heavy imports so the watchdog can use it
without pulling in the sync's secrets or playwright.
"""

import json, os, subprocess, tempfile, time
from datetime import datetime, timezone

HEALTH_FILE = os.path.expanduser("~/.netflix_sprinklr_health.json")
SESSION_FILE = os.path.expanduser("~/.netflix_sprinklr_session.json")

# A 15-minute failure loop would otherwise produce ~96 notifications a day.
NOTIFY_COOLDOWN_S = 6 * 3600

_wrote_heartbeat = False


def session_expires_at() -> int | None:
    """Earliest cookie expiry in the session file, as an epoch int.

    Useful for two things only: reporting under `--check`, and recognising a
    session whose cookies have already lapsed. It is NOT a predictor of when a
    live session will end — `connect.token` is short-lived and renewed on use,
    so a healthy session shows it hours from expiry, and on 2026-07-31 both
    `connect.sid` and the server's own `sess-exp-time` outlived the actual
    failure by 7.7 and 8 hours. See `_check_expiry` in health_watchdog.py.

    Reads timestamps only. Cookie values are credentials and never leave here.

    Returns None when the file is missing, unreadable, or carries no cookie with
    a real expiry — absence of a prediction is not a prediction of safety.
    """
    try:
        with open(SESSION_FILE) as fh:
            state = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return None

    expiries = []
    for cookie in state.get("cookies", []):
        exp = cookie.get("expires")
        # Playwright writes -1 for session cookies, which have no expiry to read.
        if isinstance(exp, (int, float)) and exp > 0:
            expiries.append(int(exp))
    return min(expiries) if expiries else None


def read_health() -> dict:
    try:
        with open(HEALTH_FILE) as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}


def save(health: dict) -> None:
    d = os.path.dirname(HEALTH_FILE)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".health-")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(health, fh, indent=2)
        os.replace(tmp, HEALTH_FILE)
    except BaseException:
        os.unlink(tmp)
        raise


def update(**fields) -> dict:
    """Read-modify-write the health file as one short operation.

    Callers must not hold a snapshot across slow work (a subprocess, a network
    call) and save it afterwards — anything another process wrote in the
    meantime would be silently reverted.
    """
    health = read_health()
    health.update(fields)
    save(health)
    return health


def wrote_heartbeat() -> bool:
    """Whether this process has recorded a heartbeat yet.

    Lets the entry point tell an already-reported failure from one that would
    otherwise exit silently.
    """
    return _wrote_heartbeat


def write_heartbeat(result: str, detail: str = "") -> None:
    """Record that a run happened, on success AND failure.

    A missing or stale entry is the only trustworthy signal that the sync is not
    running. Never infer liveness from the existence of the session or state file
    — those persist happily while the sync is dead.
    """
    global _wrote_heartbeat
    now = int(time.time())
    iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fields = {
        "lastRunTs": now,
        "lastRunIso": iso,
        "lastResult": result,
        "lastDetail": detail,
    }
    if result == "ok":
        fields["lastSuccessTs"] = now
        fields["lastSuccessIso"] = iso
    update(**fields)
    _wrote_heartbeat = True


def should_act(key: str, cooldown_s: int) -> bool:
    """Whether `key` is outside its cooldown. Records nothing.

    Split from mark() on purpose: recording before the action would mute a
    failed attempt for the whole cooldown, which is how the 2026-08-04 prompt
    failure stayed invisible.
    """
    sent = read_health().get("notifiedAt", {})
    return int(time.time()) - int(sent.get(key, 0)) >= cooldown_s


def mark(key: str) -> None:
    """Record that the action behind `key` actually succeeded, starting its cooldown."""
    health = read_health()
    health.setdefault("notifiedAt", {})[key] = int(time.time())
    save(health)


def notify(title: str, message: str, key: str) -> bool:
    """Rate-limited macOS notification. Returns True only if osascript succeeded.

    Never raises: an alerting failure must not replace the failure being
    alerted about. The cooldown is recorded only on success, so a broken
    notification path retries instead of muting itself for six hours.
    """
    sent_at = read_health().get("notifiedAt", {})
    now = int(time.time())
    if now - int(sent_at.get(key, 0)) < NOTIFY_COOLDOWN_S:
        return False

    def esc(s):
        return s.replace("\\", "\\\\").replace('"', '\\"')

    try:
        proc = subprocess.run(
            ["osascript", "-e",
             f'display notification "{esc(message)}" with title "{esc(title)}" sound name "Basso"'],
            capture_output=True, timeout=15, text=True,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"notify failed to run osascript: {type(exc).__name__}: {exc}")
        return False

    if proc.returncode != 0:
        print(f"notify: osascript exited {proc.returncode}: {proc.stderr.strip()}")
        return False

    health = read_health()
    health.setdefault("notifiedAt", {})[key] = now
    save(health)
    return True


if __name__ == "__main__":
    # `health.py --expiry-hours` prints whole hours until the session expires,
    # negative if it already has, and nothing at all when that is unknowable.
    # refresh_session.sh reads it; keeping the parsing here means the shell never
    # opens the session file itself.
    import sys
    if "--expiry-hours" in sys.argv:
        exp = session_expires_at()
        if exp is not None:
            print(int((exp - time.time()) // 3600))
    else:
        print("usage: health.py --expiry-hours")
        sys.exit(2)
