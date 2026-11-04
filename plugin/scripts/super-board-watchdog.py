#!/usr/bin/env python3
"""super-board-watchdog.py — external, scheduled checker for a stuck board (#68).

A deterministic checker that runs OUTSIDE the run loop. One poll detects stuck
or looping cards and stalled runs on one board, reports each incident once
(`incidents.jsonl` plus one best-effort toast), and then stays silent until the
state changes. Read-only by contract: it never writes to GitHub, never calls a
model, and never stops, kills or restarts anything.

Split, per spec:

  evaluate(snapshot, state, now, cfg)   pure: no files, no subprocesses, no clocks
  everything else                       thin I/O around it (lock, reads, writes)

One poll, step by step:

  1. overlap lock   mkdir .claude/super-board/watchdog/<slug>/lock; a lock younger
                    than 2x poll_minutes means another poll is running -> exit 0.
                    An older lock is stale and is replaced. Removed when done.
  2. local reads    the wave lock (.claude/super-board/inflight/workflow-wave.lock:
                    exists, mtime, STARTED), whether the shared GitHub halt file
                    exists, `super-board-usage.sh check`, today's run manifest mtime.
  3. halt check     if the halt file exists, no GitHub call is made at all, and
                    the poll reports ONE `operator-queue` incident whose waits
                    carry reason `github-halt` (constant key: reported once
                    while the halt persists, and state is not pruned during it).
  4. github read    ONE paginated GraphQL query (<= max_pages pages): viewer,
                    rateLimit, project items (updatedAt + Status) and per issue the
                    labels, assignees, last 8 comments and the linked PR's last
                    commit. Stops at the rate-limit floor. A failure produces one
                    `watchdog-blind` incident and exits 0 WITHOUT pruning state.
  5. trusted filter comments by anyone outside bot_identity + trusted_approvers +
                    viewer + watchdog.trusted_authors are dropped before anything
                    is parsed; the filtered payload goes to
                    `super-board-deps.sh --from` (offline).

Rules: card activity is the newest of the project item's updatedAt, the newest
trusted comment and the linked PR's last commit (the issue's own updatedAt is
ignored on purpose: outsiders can bump it). Ready cards are checked only while
the wave lock exists. Kinds: card-stall, card-loop, run-stall, unknown-state,
operator-queue, watchdog-blind. Wait reasons: human-gated, needs-you,
depends-on:#N, no-run, github-halt, usage-pause.

Incident keys are the first 12 hex chars of a sha256 over the kind plus the
fields that define the incident's state, so a change of state is a new key and
gets reported again. After a successful full read, keys no longer present are
pruned from state.json.

Only numbers, columns from the fixed set, 12-hex hashes, timestamps and the
fixed template words ever reach incidents.jsonl, the toast or a shell. Titles
and bodies are passed to deps.sh (offline) and dropped everywhere else.

Usage:
  python super-board-watchdog.py --config <repo>/.claude/super-board/configs/<slug>.json [--once]

`--once` (a single poll) is the default; the schedule comes from the installed
task (super-board-watchdog-install.sh), never from this process.

Exit codes: 0 ok (including a blind read, a halt, or disabled); 64 usage or
invalid config; 66 config file not found.

Stdlib only. State lives under gitignored .claude/super-board/watchdog/<slug>/.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent

SLUG_RE = re.compile(r"\A[A-Za-z0-9._-]+\Z")
ROOT_CAUSE_RE = re.compile(r"root-cause-hash:\s*([0-9a-fA-F]{12})")

# The seven canonical columns (v3.0.0 keeps all seven; the config's own list is
# used for spelling, matched case-insensitively).
CANON_COLUMNS = ("Backlog", "Ready", "Building", "QA", "Review", "Blocked", "Done")
# Columns whose cards are checked for stalls and loops. Blocked cards are never
# stall-checked (they wait on people, sometimes for weeks); they get the
# unknown-state check instead. Done and Backlog cards are nobody's alarm.
STALL_COLUMNS = ("Ready", "Building", "QA", "Review")
EVALUATED_COLUMNS = STALL_COLUMNS + ("Blocked",)
# A card in an active lane means the run is working on something; the
# operator-queue report only fires when every lane is empty.
ACTIVE_COLUMNS = ("Building", "QA", "Review")

DEFAULTS = {
    "enabled": False,
    "poll_minutes": 10,
    "card_stall_minutes": 90,
    "run_stall_minutes": 45,
    "gh_points_floor": 500,
    "max_pages": 5,
    "trusted_authors": [],
    "notify": "toast",
}

# One query, one resource: the project's items with everything the rules need.
# `viewer` feeds the trusted-author set; `rateLimit` feeds the points floor.
# The first page omits `-F after=` entirely (null default), matching
# super-board-status.py's pagination pattern. Page cost is dominated by
# items(first:50); the floor and max_pages bound the rest.
QUERY = """
query($owner:String!, $number:Int!, $after:String) {
  viewer { login }
  rateLimit { remaining }
  repositoryOwner(login:$owner) {
    ... on ProjectV2Owner {
      projectV2(number:$number) {
        items(first:50, after:$after) {
          pageInfo { hasNextPage endCursor }
          nodes {
            updatedAt
            fieldValues(first:8) {
              nodes {
                ... on ProjectV2ItemFieldSingleSelectValue {
                  name
                  field { ... on ProjectV2SingleSelectField { name } }
                }
              }
            }
            content {
              __typename
              ... on Issue {
                number
                title
                state
                body
                labels(first:10) { nodes { name } }
                assignees(first:10) { nodes { login } }
                comments(last:8) { nodes { author { login } createdAt body } }
                timelineItems(last:10, itemTypes:[CONNECTED_EVENT, CROSS_REFERENCED_EVENT]) {
                  nodes {
                    __typename
                    ... on ConnectedEvent {
                      subject { __typename ... on PullRequest { commits(last:1) { nodes { committedDate } } } }
                      source { __typename ... on PullRequest { commits(last:1) { nodes { committedDate } } } }
                    }
                    ... on CrossReferencedEvent {
                      source { __typename ... on PullRequest { commits(last:1) { nodes { committedDate } } } }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

# The default notifier. The dynamic part travels ONLY through the
# SB_WATCHDOG_TEXT environment variable, never as an argument.
POWERSHELL_TOAST = (
    "Add-Type -AssemblyName System.Windows.Forms; "
    "Add-Type -AssemblyName System.Drawing; "
    "$n = New-Object System.Windows.Forms.NotifyIcon; "
    "$n.Icon = [System.Drawing.SystemIcons]::Warning; "
    "$n.Visible = $true; "
    "$n.ShowBalloonTip(10000, 'super-board', $env:SB_WATCHDOG_TEXT, "
    "[System.Windows.Forms.ToolTipIcon]::Warning); "
    "Start-Sleep -Seconds 11; $n.Dispose()"
)


class WatchdogReadError(Exception):
    """The GitHub read failed: the poll goes blind, never silent."""


# ───────────────────────────── pure helpers ─────────────────────────────


def watchdog_cfg(cfg: dict) -> dict:
    """The watchdog block with defaults; bad values fall back, never crash."""
    raw = cfg.get("watchdog")
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in raw:
            out[key] = raw[key]
    for key, minimum in (("poll_minutes", 1), ("card_stall_minutes", 1),
                         ("run_stall_minutes", 1), ("gh_points_floor", 0),
                         ("max_pages", 1)):
        try:
            value = int(out[key])
        except (TypeError, ValueError):
            value = DEFAULTS[key]
        out[key] = value if value >= minimum else DEFAULTS[key]
    authors = out.get("trusted_authors")
    if isinstance(authors, list):
        out["trusted_authors"] = [a for a in authors if isinstance(a, str) and a.strip()]
    else:
        out["trusted_authors"] = []
    out["enabled"] = bool(out.get("enabled"))
    out["notify"] = str(out.get("notify") or "")
    return out


def parse_iso(text):
    """GitHub timestamps -> aware UTC datetimes; junk -> None (alertable)."""
    if not isinstance(text, str) or not text.strip():
        return None
    candidate = text.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso(moment) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def incident_key(kind: str, *parts) -> str:
    """First 12 hex of a sha256 over the kind plus the state-defining fields."""
    material = "|".join([kind] + ["" if p is None else str(p) for p in parts])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def activity_stamps(card: dict) -> list:
    """Every trusted activity time of one card. The issue's own updatedAt is
    deliberately absent: outsiders can bump it with a comment."""
    stamps = []
    item_updated = parse_iso(card.get("item_updated_at"))
    if item_updated:
        stamps.append(item_updated)
    for entry in card.get("comments") or []:
        moment = parse_iso(entry.get("at"))
        if moment:
            stamps.append(moment)
    pr_commit = parse_iso(card.get("pr_last_commit"))
    if pr_commit:
        stamps.append(pr_commit)
    return stamps


def latest_of(stamps: list):
    return max(stamps) if stamps else None


def wait_reason(card: dict, deps_entry, *, run_active: bool, paused: bool):
    """Why this card is waiting. Precedence: people first, then machine states."""
    entry = deps_entry if isinstance(deps_entry, dict) else {}
    if entry.get("humanGated") is True:
        return "human-gated"
    if entry.get("needsYou") is True:
        return "needs-you"
    open_blockers = entry.get("openBlockers")
    if isinstance(open_blockers, list):
        numbers = [b for b in open_blockers if isinstance(b, int) and not isinstance(b, bool) and b > 0]
        if numbers:
            return "depends-on:#%d" % min(numbers)
    if card.get("column") == "Ready" and not run_active:
        return "no-run"
    if paused:
        return "usage-pause"
    return None


def loop_hash(card: dict, rebuild_cap: int):
    """The dominant repeated root-cause hash, if it crossed the cap.

    Returns (hash, first_seen, count) or None. Each comment counts once per
    hash, whatever the lane wrote inside it.
    """
    counts = {}
    first_seen = {}
    for entry in card.get("comments") or []:
        body = entry.get("body") or ""
        moment = parse_iso(entry.get("at"))
        for found in set(m.group(1).lower() for m in ROOT_CAUSE_RE.finditer(body)):
            counts[found] = counts.get(found, 0) + 1
            if moment and (found not in first_seen or moment < first_seen[found]):
                first_seen[found] = moment
    best = None
    for found, count in counts.items():
        if count < rebuild_cap + 1:
            continue
        if best is None or count > counts[best] or (count == counts[best] and found < best):
            best = found
    if best is None:
        return None
    return best, first_seen.get(best), counts[best]


def make_incident(state: dict, now, kind: str, *, key: str, card, column, since,
                  run_active: bool, minutes, hash_value, lock_started, waits) -> dict:
    return {
        "v": 1,
        "at": iso(now),
        "slug": state.get("slug") or "",
        "key": key,
        "kind": kind,
        "card": card,
        "column": column,
        "since": iso(since) if since else None,
        "run_active": bool(run_active),
        "evidence": {"minutes": minutes, "hash": hash_value, "lock_started": lock_started},
        "waits": waits,
    }


# ───────────────────────────── pure core ─────────────────────────────


def evaluate(snapshot: dict, state: dict, now, cfg: dict):
    """Pure. Returns (incidents, new_state).

    snapshot keys: halted, blind, wave_lock {exists, mtime, started},
    manifest_mtime, usage {state, resets_at}, cards [...], deps {...}.
    `now` is an aware UTC datetime. `state` is never mutated.
    """
    wd = watchdog_cfg(cfg)
    rebuild_cap = cfg.get("rebuild_cap")
    if not isinstance(rebuild_cap, int) or isinstance(rebuild_cap, bool) or rebuild_cap < 0:
        rebuild_cap = 2
    cards = snapshot.get("cards") or []
    deps = snapshot.get("deps") or {}
    lock = snapshot.get("wave_lock") or {}
    run_active = bool(lock.get("exists"))
    lock_started = lock.get("started")
    lock_mtime = parse_iso(lock.get("mtime"))
    manifest_at = parse_iso(snapshot.get("manifest_mtime"))
    blind = bool(snapshot.get("blind"))
    halted = bool(snapshot.get("halted"))
    usage = snapshot.get("usage") or {}
    full_read = not (blind or halted)

    # A usage pause suppresses ONLY the stall clocks (card-stall, run-stall)
    # until resets_at + run_stall_minutes: the reset should clear it, and only
    # a stall that outlives that is real. card-loop and unknown-state are
    # still evaluated while paused — a card looping on the same root cause is
    # a run defect, not a usage problem.
    paused = False
    if usage.get("state") == "pause" and isinstance(usage.get("resets_at"), (int, float)) \
            and not isinstance(usage.get("resets_at"), bool):
        paused = now.timestamp() < float(usage["resets_at"]) + wd["run_stall_minutes"] * 60

    waits = []
    for card in cards:
        if card.get("column") not in EVALUATED_COLUMNS:
            continue
        reason = wait_reason(card, deps.get(str(card.get("number"))),
                             run_active=run_active, paused=paused)
        if reason:
            waits.append({"card": card["number"], "reason": reason})
    waits.sort(key=lambda w: w["card"])

    # Freshest life sign anywhere on the board (any column, any state) — one
    # input to the run-stall clock.
    newest_activity = None
    for card in cards:
        for stamp in activity_stamps(card):
            if newest_activity is None or stamp > newest_activity:
                newest_activity = stamp

    incidents = []
    if blind:
        # The watchdog could not see the board. One incident, and state is NOT
        # pruned: whatever was reported before the blindness stays reported.
        incidents.append(make_incident(
            state, now, "watchdog-blind",
            key=incident_key("watchdog-blind"),
            card=None, column=None, since=None, run_active=run_active,
            minutes=None, hash_value=None, lock_started=lock_started,
            waits=waits))
    if halted:
        # The board is halted: zero gh calls happened, and the operator still
        # hears why. One incident, one constant key, reported once; state is
        # not pruned while the halt lasts.
        incidents.append(make_incident(
            state, now, "operator-queue",
            key=incident_key("operator-queue", "github-halt"),
            card=None, column=None, since=None, run_active=run_active,
            minutes=None, hash_value=None, lock_started=lock_started,
            waits=[{"card": None, "reason": "github-halt"}]))
    if full_read:
        for card in cards:
            column = card.get("column")
            number = card.get("number")
            if column not in EVALUATED_COLUMNS:
                continue
            entry = deps.get(str(number))
            entry = entry if isinstance(entry, dict) else {}
            if column == "Blocked":
                # A Blocked card deps.sh cannot read confidently stays alertable.
                if entry.get("parseable") is False or not entry:
                    incidents.append(make_incident(
                        state, now, "unknown-state",
                        key=incident_key("unknown-state", number, column),
                        card=number, column=column, since=None, run_active=run_active,
                        minutes=None, hash_value=None, lock_started=lock_started,
                        waits=waits))
                continue
            if column == "Ready" and not run_active:
                continue  # wait reason no-run only; a parked board is not a stall
            if not paused:
                # the stall clock is the only thing a usage pause pauses
                activity = latest_of(activity_stamps(card))
                stalled = activity is None or \
                    (now - activity).total_seconds() > wd["card_stall_minutes"] * 60
                if stalled:
                    minutes = int((now - activity).total_seconds() // 60) if activity else None
                    incidents.append(make_incident(
                        state, now, "card-stall",
                        key=incident_key("card-stall", number, column,
                                         iso(activity) if activity else ""),
                        card=number, column=column, since=activity, run_active=run_active,
                        minutes=minutes, hash_value=None, lock_started=lock_started,
                        waits=waits))
            # the loop check runs even while paused: only stalls are suppressed
            loop = loop_hash(card, rebuild_cap)
            if loop:
                found, first_seen, _count = loop
                minutes = int((now - first_seen).total_seconds() // 60) if first_seen else None
                incidents.append(make_incident(
                    state, now, "card-loop",
                    key=incident_key("card-loop", number, found),
                    card=number, column=column, since=first_seen, run_active=run_active,
                    minutes=minutes, hash_value=found, lock_started=lock_started,
                    waits=waits))

        if run_active and not paused:
            life_candidates = [t for t in (lock_mtime, manifest_at, newest_activity) if t]
            life = max(life_candidates) if life_candidates else None
            if life is None or (now - life).total_seconds() > wd["run_stall_minutes"] * 60:
                minutes = int((now - life).total_seconds() // 60) if life else None
                incidents.append(make_incident(
                    state, now, "run-stall",
                    key=incident_key("run-stall", lock_started or "",
                                     iso(life) if life else ""),
                    card=None, column=None, since=life, run_active=True,
                    minutes=minutes, hash_value=None, lock_started=lock_started,
                    waits=waits))

        alertable = any(i["kind"] in ("card-stall", "card-loop", "run-stall", "unknown-state")
                        for i in incidents)
        active_lanes = any(c.get("column") in ACTIVE_COLUMNS for c in cards)
        # "Only waiting on you" requires EVERY evaluated open card to have a
        # wait reason: one card without one (a fresh Ready card during a run,
        # say) vetoes the report — the board is not purely waiting on the
        # operator yet.
        evaluated = [c for c in cards if c.get("column") in EVALUATED_COLUMNS]
        if not alertable and not active_lanes and waits and not paused \
                and len(waits) == len(evaluated):
            fingerprint = ";".join("%d:%s" % (w["card"], w["reason"]) for w in waits)
            incidents.append(make_incident(
                state, now, "operator-queue",
                key=incident_key("operator-queue", fingerprint),
                card=None, column=None, since=None, run_active=run_active,
                minutes=None, hash_value=None, lock_started=lock_started,
                waits=waits))

    reported = dict(state.get("reported") or {})
    for incident in incidents:
        reported.setdefault(incident["key"], {"kind": incident["kind"], "first": iso(now)})
    if full_read:
        present = {incident["key"] for incident in incidents}
        reported = {k: v for k, v in reported.items() if k in present}
    blind_since = state.get("blind_since")
    if full_read:
        blind_since = None
    elif blind and not blind_since:
        blind_since = iso(now)
    new_state = {
        "version": 1,
        "slug": state.get("slug") or "",
        "last_poll": iso(now),
        "last_ok": iso(now) if full_read else state.get("last_ok"),
        "blind_since": blind_since,
        "reported": reported,
    }
    return incidents, new_state


def toast_text(slug: str, incident: dict) -> str:
    """Fixed template; only whitelisted values (kind, number, column, minutes)."""
    text = "super-board {0}: {1}".format(slug, incident["kind"])
    if incident.get("card") is not None:
        text += " #{0}".format(incident["card"])
    context = []
    if incident.get("column"):
        context.append(str(incident["column"]))
    minutes = (incident.get("evidence") or {}).get("minutes")
    if minutes is not None:
        context.append("{0}m".format(minutes))
    if context:
        text += " ({0})".format(", ".join(context))
    return text


# ───────────────────────────── I/O layer ─────────────────────────────


def repo_root_for(cfg_path: Path):
    """<repo>/.claude/super-board/configs/<slug>.json -> <repo>, else None."""
    try:
        resolved = cfg_path.resolve()
        configs_dir, board_dir, dot_dir = resolved.parents[0], resolved.parents[1], resolved.parents[2]
        repo = resolved.parents[3]
    except IndexError:
        return None
    if configs_dir.name != "configs" or board_dir.name != "super-board" or dot_dir.name != ".claude":
        return None
    return repo


def load_state(path: Path, slug: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict) and data.get("version") == 1 and isinstance(data.get("reported"), dict):
        data.setdefault("slug", slug)
        data.setdefault("last_poll", None)
        data.setdefault("last_ok", None)
        data.setdefault("blind_since", None)
        return data
    return {"version": 1, "slug": slug, "last_poll": None, "last_ok": None,
            "blind_since": None, "reported": {}}


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_wave_lock(repo: Path) -> dict:
    """The workflow backend's wave marker: exists, mtime, STARTED (run-workflow.md)."""
    path = repo / ".claude" / "super-board" / "inflight" / "workflow-wave.lock"
    if not path.is_file():
        return {"exists": False, "mtime": None, "started": None}
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        started = None
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("STARTED="):
                started = line[len("STARTED="):].strip()
        return {"exists": True, "mtime": iso(mtime), "started": started}
    except OSError:
        return {"exists": True, "mtime": None, "started": None}


def halt_file(repo: Path) -> Path:
    """The shared halt file, resolved the same way the run loop resolves it."""
    override = os.environ.get("SB_GITHUB_HALT_FILE")
    if override:
        return Path(override)
    root = repo
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=5, cwd=str(repo))
        if proc.returncode == 0 and proc.stdout.strip():
            common = Path(proc.stdout.strip())
            if not common.is_absolute():
                common = repo / common
            root = common.resolve().parent
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return root / ".claude" / "super-board" / "github-halt.json"


def local_day(now, tz_name):
    if isinstance(tz_name, str) and tz_name.strip():
        try:
            from zoneinfo import ZoneInfo
            return now.astimezone(ZoneInfo(tz_name.strip())).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001 - any tz failure falls back to local time
            pass
    return now.astimezone().strftime("%Y-%m-%d")


def manifest_mtime(repo: Path, cfg: dict, slug: str, now):
    """Today's run manifest (docs/super-board/runs/YYYY-MM-DD-SLUG.md), if any."""
    paths = cfg.get("paths") or {}
    runs_dir = paths.get("runs_dir")
    if not isinstance(runs_dir, str) or not runs_dir:
        runs_dir = "docs/super-board/runs"
    day = local_day(now, cfg.get("timezone"))
    path = repo / runs_dir / "{0}-{1}.md".format(day, slug)
    try:
        if path.is_file():
            return iso(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))
    except OSError:
        pass
    return None


def usage_check(cfg_path: Path) -> dict:
    """`super-board-usage.sh check` — exit 10 means pause; anything else does not."""
    script = SCRIPTS_DIR / "super-board-usage.sh"
    if not script.exists():
        return {"state": None, "resets_at": None}
    bash = shutil.which("bash") or "bash"
    try:
        proc = subprocess.run(
            [bash, str(script), "check", "--config", str(cfg_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return {"state": None, "resets_at": None}
    line = ""
    for candidate in proc.stdout.splitlines():
        if candidate.strip():
            line = candidate.strip()
    try:
        data = json.loads(line)
    except ValueError:
        return {"state": None, "resets_at": None}
    if not isinstance(data, dict):
        return {"state": None, "resets_at": None}
    state = data.get("state")
    if proc.returncode == 10:
        state = "pause"
    resets = data.get("resets_at")
    if not isinstance(resets, (int, float)) or isinstance(resets, bool):
        resets = None
    return {"state": state if isinstance(state, str) else None, "resets_at": resets}


def gh_read(cfg: dict, wd: dict):
    """The one paginated read. Returns (nodes, viewer_login).

    Stops early at the rate-limit floor; any failure raises WatchdogReadError
    and the poll goes blind.
    """
    project = cfg.get("project") or {}
    owner = project.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        raise WatchdogReadError("config project.owner is missing")
    raw_number = project.get("number")
    try:
        number = int(raw_number)
    except (TypeError, ValueError):
        raise WatchdogReadError("config project.number must be an integer") from None
    gh = shutil.which("gh") or "gh"
    # One line: GraphQL is whitespace-insensitive, and a single-line value is
    # the safest thing to hand to gh on every platform (no multiline argv).
    query = " ".join(QUERY.split())
    nodes = []
    viewer = None
    remaining = None
    after = None
    for _page in range(max(1, int(wd["max_pages"]))):
        args = [gh, "api", "graphql",
                "-f", "query=" + query,
                "-F", "owner=" + owner.strip(),
                "-F", "number=%d" % number]
        if after:
            args += ["-F", "after=" + after]
        try:
            proc = subprocess.run(args, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=60)
        except subprocess.TimeoutExpired as error:
            raise WatchdogReadError("gh timed out") from error
        except OSError as error:
            raise WatchdogReadError("gh could not run: {0}".format(error)) from error
        if proc.returncode != 0:
            raise WatchdogReadError("gh exited {0}".format(proc.returncode))
        try:
            payload = json.loads(proc.stdout)
        except ValueError as error:
            raise WatchdogReadError("gh output was not JSON") from error
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise WatchdogReadError("the response had no data object")
        login = (data.get("viewer") or {}).get("login")
        if isinstance(login, str) and login:
            viewer = login
        limit = (data.get("rateLimit") or {}).get("remaining")
        if isinstance(limit, (int, float)) and not isinstance(limit, bool):
            remaining = int(limit)
        items = ((data.get("repositoryOwner") or {}).get("projectV2") or {}).get("items")
        if not isinstance(items, dict):
            raise WatchdogReadError("the response had no project items")
        page_nodes = items.get("nodes")
        if isinstance(page_nodes, list):
            nodes.extend(node for node in page_nodes if isinstance(node, dict))
        info = items.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        after = info.get("endCursor")
        if not after:
            break
        if remaining is not None and remaining < int(wd["gh_points_floor"]):
            break
    return nodes, viewer


def trusted_set(cfg: dict, viewer, wd: dict) -> set:
    """bot_identity + trusted_approvers + viewer + watchdog.trusted_authors.

    Compared lowercase; a "[bot]" identity also matches its bare app name.
    """
    trusted = set()
    bot = (cfg.get("notifications") or {}).get("bot_identity")
    if isinstance(bot, str):
        bot = bot.strip().lower()
        if bot and bot != "auto":
            trusted.add(bot)
            if bot.endswith("[bot]"):
                trusted.add(bot[:-len("[bot]")])
    approvers = (cfg.get("merge_policy") or {}).get("trusted_approvers")
    if isinstance(approvers, list):
        for approver in approvers:
            if isinstance(approver, str) and approver.strip():
                trusted.add(approver.strip().lower())
    if isinstance(viewer, str) and viewer:
        trusted.add(viewer.lower())
    for author in wd.get("trusted_authors"):
        trusted.add(author.strip().lower())
    return trusted


def pr_last_commit(content: dict):
    dates = []
    for event in (content.get("timelineItems") or {}).get("nodes") or []:
        if not isinstance(event, dict):
            continue
        for side in ("subject", "source"):
            holder = event.get(side)
            if not isinstance(holder, dict):
                continue
            for commit in (holder.get("commits") or {}).get("nodes") or []:
                if isinstance(commit, dict) and isinstance(commit.get("committedDate"), str):
                    dates.append(commit["committedDate"])
    parsed = [moment for moment in (parse_iso(d) for d in dates) if moment]
    return iso(max(parsed)) if parsed else None


def status_column(node: dict, columns: list):
    """The Status single-select value, canonicalized against the config's columns."""
    wanted = {}
    for name in columns:
        if isinstance(name, str):
            wanted[name.lower()] = name
    for value in (node.get("fieldValues") or {}).get("nodes") or []:
        if not isinstance(value, dict):
            continue
        field = value.get("field")
        if isinstance(field, dict) and field.get("name") == "Status":
            name = value.get("name")
            if isinstance(name, str) and name.lower() in wanted:
                return wanted[name.lower()]
            return None
    return None


def build_issue_views(nodes: list, trusted: set, cfg: dict):
    """One traversal -> (cards for evaluate, deps payload for deps.sh).

    The cards never carry titles or bodies. The payload does (deps.sh reads the
    `## Blocked by` section offline) and is deleted right after the call.
    """
    columns = cfg.get("columns")
    columns = columns if isinstance(columns, list) and columns else list(CANON_COLUMNS)
    cards = []
    payload = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        content = node.get("content") or {}
        if content.get("__typename") != "Issue":
            continue
        number = content.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        trusted_comments = []
        for raw in (content.get("comments") or {}).get("nodes") or []:
            if not isinstance(raw, dict):
                continue
            login = (raw.get("author") or {}).get("login") or ""
            if not isinstance(login, str) or login.lower() not in trusted:
                continue
            trusted_comments.append({
                "author": login,
                "at": raw.get("createdAt"),
                "body": raw.get("body") or "",
            })
        labels = [entry.get("name") for entry in (content.get("labels") or {}).get("nodes") or []
                  if isinstance(entry, dict) and isinstance(entry.get("name"), str)]
        assignees = [entry.get("login") for entry in (content.get("assignees") or {}).get("nodes") or []
                     if isinstance(entry, dict) and isinstance(entry.get("login"), str)]
        cards.append({
            "number": number,
            "column": status_column(node, columns),
            "state": content.get("state") or "OPEN",
            "item_updated_at": node.get("updatedAt"),
            "comments": trusted_comments,
            "pr_last_commit": pr_last_commit(content),
            "labels": labels,
            "assignees": assignees,
        })
        payload.append({
            "number": number,
            "title": content.get("title") or "",
            "state": content.get("state") or "OPEN",
            "body": content.get("body") or "",
            "labels": {"nodes": [{"name": name} for name in labels]},
            "comments": {"nodes": [{"body": c["body"]} for c in trusted_comments]},
        })
    return cards, payload


def run_deps(payload_issues: list, cfg_path: Path, wd_dir: Path):
    """Offline dependency state via `super-board-deps.sh --from`. None = blind."""
    script = SCRIPTS_DIR / "super-board-deps.sh"
    if not script.exists():
        print("watchdog: super-board-deps.sh not found beside the watchdog script", file=sys.stderr)
        return None
    bash = shutil.which("bash") or "bash"
    payload_path = wd_dir / "deps-payload.json"
    try:
        wd_dir.mkdir(parents=True, exist_ok=True)
        payload_path.write_text(json.dumps(payload_issues), encoding="utf-8")
        proc = subprocess.run(
            [bash, str(script), "--from", str(payload_path), "--config", str(cfg_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        if proc.returncode != 0:
            print("watchdog: super-board-deps.sh exited {0} (its stderr is not "
                  "echoed: it can quote issue text)".format(proc.returncode), file=sys.stderr)
            return None
        graph = json.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        print("watchdog: the dependency read failed ({0})".format(type(error).__name__),
              file=sys.stderr)
        return None
    finally:
        try:
            payload_path.unlink()
        except OSError:
            pass
    if not isinstance(graph, dict):
        print("watchdog: the dependency read was not a JSON object", file=sys.stderr)
        return None
    return graph


def split_command(value: str) -> list:
    """Whitespace split honouring double quotes (for SB_WATCHDOG_NOTIFY)."""
    parts = []
    current = []
    in_quotes = False
    for ch in value:
        if ch == '"':
            in_quotes = not in_quotes
        elif ch in " \t" and not in_quotes:
            if current:
                parts.append("".join(current))
                current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


def send_notify(incident: dict, slug: str, wd: dict) -> str:
    """One best-effort toast. Never retried; failure is recorded, nothing more."""
    if wd.get("notify", "").strip().lower() != "toast":
        return "off"
    env = dict(os.environ)
    env["SB_WATCHDOG_TEXT"] = toast_text(slug, incident)
    override = os.environ.get("SB_WATCHDOG_NOTIFY", "").strip()
    if override:
        cmd = split_command(override)
        if not cmd:
            return "failed"
    else:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            return "failed"
        cmd = [powershell, "-NoProfile", "-NonInteractive", "-Command", POWERSHELL_TOAST]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=45, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return "failed"
    return "sent" if proc.returncode == 0 else "failed"


def append_incident(path: Path, incident: dict, notify_status: str) -> None:
    record = dict(incident)
    record["notify"] = notify_status
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=True) + "\n")


def poll(cfg: dict, cfg_path: Path, slug: str, repo: Path, wd_dir: Path, wd: dict) -> int:
    now = datetime.now(timezone.utc)
    state = load_state(wd_dir / "state.json", slug)
    snapshot = {
        "halted": halt_file(repo).exists(),
        "blind": False,
        "wave_lock": read_wave_lock(repo),
        "manifest_mtime": manifest_mtime(repo, cfg, slug, now),
        "usage": usage_check(cfg_path),
        "cards": [],
        "deps": {},
    }
    if not snapshot["halted"]:
        try:
            nodes, viewer = gh_read(cfg, wd)
        except WatchdogReadError as error:
            snapshot["blind"] = True
            print("watchdog: GitHub read failed: {0}".format(error), file=sys.stderr)
        else:
            trusted = trusted_set(cfg, viewer, wd)
            cards, payload = build_issue_views(nodes, trusted, cfg)
            graph = run_deps(payload, cfg_path, wd_dir)
            if graph is None:
                snapshot["blind"] = True
                print("watchdog: the dependency read failed; the poll stays blind", file=sys.stderr)
            else:
                snapshot["cards"] = cards
                snapshot["deps"] = graph
    incidents, new_state = evaluate(snapshot, state, now, cfg)
    already = state.get("reported") or {}
    for incident in incidents:
        if incident["key"] in already:
            continue
        status = send_notify(incident, slug, wd)
        append_incident(wd_dir / "incidents.jsonl", incident, status)
    write_state(wd_dir / "state.json", new_state)
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    config_value = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--config":
            if index + 1 >= len(argv):
                print("--config needs a path", file=sys.stderr)
                return 64
            config_value = argv[index + 1]
            index += 2
        elif arg == "--once":
            index += 1  # a single poll is already the only mode
        else:
            print("unknown argument: {0}".format(arg), file=sys.stderr)
            return 64
    if not config_value:
        print("usage: super-board-watchdog.py --config "
              "<repo>/.claude/super-board/configs/<slug>.json [--once]", file=sys.stderr)
        return 64
    cfg_path = Path(config_value)
    if not cfg_path.is_file():
        print("watchdog: config not found: {0}".format(cfg_path), file=sys.stderr)
        return 66
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print("watchdog: unreadable config: {0}".format(error), file=sys.stderr)
        return 64
    if not isinstance(cfg, dict):
        print("watchdog: the config must be a JSON object", file=sys.stderr)
        return 64
    slug = cfg_path.stem
    if slug in (".", "..") or not SLUG_RE.match(slug):
        print("watchdog: invalid config slug: {0}".format(slug), file=sys.stderr)
        return 64
    wd = watchdog_cfg(cfg)
    if not wd["enabled"]:
        print("watchdog: disabled (watchdog.enabled is false); nothing to do", file=sys.stderr)
        return 0
    repo = repo_root_for(cfg_path)
    if repo is None:
        print("watchdog: --config must live at "
              "<repo>/.claude/super-board/configs/<slug>.json", file=sys.stderr)
        return 64

    wd_dir = repo / ".claude" / "super-board" / "watchdog" / slug
    lock_dir = wd_dir / "lock"
    if lock_dir.exists():
        try:
            age = time.time() - lock_dir.stat().st_mtime
        except OSError:
            age = 0.0
        if age < wd["poll_minutes"] * 120:  # younger than 2x poll_minutes
            return 0
        shutil.rmtree(lock_dir, ignore_errors=True)  # stale: replace it
    try:
        wd_dir.mkdir(parents=True, exist_ok=True)
        os.mkdir(lock_dir)
    except FileExistsError:
        return 0
    except OSError as error:
        print("watchdog: cannot create the overlap lock: {0}".format(error), file=sys.stderr)
        return 64
    try:
        try:
            (lock_dir / "owner.json").write_text(json.dumps(
                {"pid": os.getpid(), "started": iso(datetime.now(timezone.utc))}),
                encoding="utf-8")
        except OSError:
            pass
        return poll(cfg, cfg_path, slug, repo, wd_dir, wd)
    finally:
        shutil.rmtree(lock_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
