#!/usr/bin/env python3
"""super-board-events.py — trusted event triggers that start exactly one lane (issue #66).

A trusted event — a card move onto a routable column of the project board, or a comment whose
first line is exactly the configured command — starts exactly one lane on one card through the
normal wave. This script is the deterministic dispatcher between GitHub and the runbook:

  poll   read GitHub (read-only, at most 4 calls per tick), decide candidates, keep a queue
  next   print the oldest dispatchable queued event as JSON; exit 3 when nothing is dispatchable
  claim <id>   fence the start: wave lock + live permission + budget, record the start, move to claimed/
  done <id> --result ...    the lane finished; move to done/
  fail <id> --reason ...   the lane (or the card) refused; move to failed/

Trust (all three; the two offline checks at poll/enqueue time, the live permission at
claim time — the freshest moment before any start):
  * the actor is in `events.initiators` in the config, is not `notifications.bot_identity`
    (which must never be 'auto': auto disables events entirely), and is not a `[bot]` login.
    These are cheap name checks needing no API call: at enqueue (poll) an untrusted actor's
    event is dropped with `reason: untrusted-actor` and is never queued;
  * the actor holds a live admin, maintain or write permission on the repo
    (checked through super-board-github-read.py at claim time — the freshest moment); a
    queued event failing it is dropped and its claim exits 5 with no start recorded;
  * the event's source is enabled in `events.sources` (a disabled source's candidate is
    marked seen and dropped with `reason: source-disabled`, never queued).

Everything is deterministic and local: stdlib only, no model calls, no `gh` write verbs, no
`node`. Every GitHub read goes through super-board-github-read.py with SB_GITHUB_READ_STRICT=1
(fixed commands; a shape error is a plugin bug and halts), so an outage exits 79 and leaves the
queue untouched, and the run-level halt file is honored. Comment bodies are never stored: the
queue files carry identities and routing only.

State lives under `<repo>/.claude/super-board/events/<slug>/` (gitignored):
  cursor.json    the poll watermark (a `since` cursor overlapping the last tick by 120 s)
  seen.jsonl     every decided event key `<source>:<node_id>`; pruned after 7 days
  queue/         decided, dispatchable events (one per card, newest supersedes)
  claimed/       events dispatched into a lane (a stale claim is an orphan, never re-run)
  done/ failed/ expired/ dropped/   terminal folders
  starts.jsonl   the budget ledger: one line per lane start

Budget: at most `max_starts_per_hour` (default 3) starts in the last hour and
`max_starts_per_day` (default 10) in the last day. A missing or unreadable ledger counts as
over budget (fail closed). The overflow is reported once per queued event (budget_reported).

Bounce guard: while the wave lock (workflow-wave.lock) is held, or the card has an inflight
lock (inflight/issue-<number>), new events are dropped (decided, not retried) — a wave is
already running; `claim` in turn refuses to run without the wave lock.

Exit codes: 0 ok · 3 nothing dispatchable (or events disabled, or over budget) · 5 refused
(unknown id, no lock, untrusted, over budget at claim) · 64 usage/config error · 79 a GitHub
read halted (outage or run pause).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
READ_HELPER = SCRIPTS_DIR / "super-board-github-read.py"

OVERLAP_SECONDS = 120          # the watermark overlaps the last tick by this much
MAX_CALLS_PER_TICK = 4         # board + issue comments + PR review comments + correlation batch
CORRELATION_BATCH = 8          # PR numbers resolved per tick (the rest wait for the next tick)
SEEN_DAYS = 7                  # seen.jsonl pruning
DEFAULT_COMMAND = "/super-board go"
DEFAULT_POLL_SECONDS = 120
DEFAULT_MAX_STARTS_PER_HOUR = 3
DEFAULT_MAX_STARTS_PER_DAY = 10
DEFAULT_QUEUE_TTL_HOURS = 24
TRUSTED_PERMISSIONS = ("admin", "maintain", "write")
SOURCE_KEYS = ("ready_move", "issue_comment", "pr_comment", "review_comment")
FOLDERS = ("queue", "claimed", "done", "failed", "expired", "dropped")
EVENT_FIELDS = ("id", "key", "source", "actor", "actor_permission", "issue", "pr", "lane",
                "card_status", "node_id", "url", "created_at", "enqueued_at", "state",
                "reason", "budget_reported")
WAVE_LOCK = ("inflight", "workflow-wave.lock")
REMOTE_RE = re.compile(r"^(?:https?://|git@|ssh://git@)[^/:]+[:/]?([^/]+)/([^/]+?)(?:\.git)?/?$|"
                       r"^([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)$")


class EventsError(Exception):
    """Usage or config problem: exit 64."""


class NotFoundError(Exception):
    """Unknown event id: exit 5."""


class RefusedError(Exception):
    """The requested transition is refused: exit 5."""


class NothingDispatchable(Exception):
    """`next` has nothing to dispatch: exit 3."""


class ReadHalted(Exception):
    """A GitHub read halted (helper exit 79): exit 79."""


# ───────────────────────────── small pure helpers ─────────────────────────────


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def first_line(body):
    """The exact first line of a comment body (CRLF normalized)."""
    if not isinstance(body, str):
        return ""
    return body.replace("\r\n", "\n").replace("\r", "\n").split("\n", 1)[0]


def command_match(body, command):
    """True only when the first line equals the command exactly (no quoting, no case games)."""
    return first_line(body) == command


def parse_remote(remote):
    """(owner, name) out of a git remote URL, or None."""
    if not isinstance(remote, str):
        return None
    m = REMOTE_RE.match(remote.strip())
    if not m:
        return None
    if m.group(1):
        return m.group(1), m.group(2)
    return m.group(3), m.group(4)


def event_id(key):
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def offline_trust_reason(actor, bot_identity, initiators):
    """Why the actor fails offline trust, or None when it passes. Names only — no bodies."""
    login = (actor or "").strip().lower()
    if not login:
        return "the event carries no actor"
    if login.endswith("[bot]"):
        return "actor is a bot login"
    bot = (bot_identity or "").strip().lower()
    if bot and login == bot:
        return "actor is the bot_identity account"
    allowed = {str(i).strip().lower() for i in initiators if isinstance(i, str)}
    if login not in allowed:
        return "actor is not in events.initiators"
    return None


def permission_level(payload):
    """Every live permission spelling out of the collaborator-permission payload."""
    if not isinstance(payload, dict):
        return []
    values = []
    for field in ("permission", "role_name"):
        value = payload.get(field)
        if isinstance(value, str) and value.strip() and value.strip().lower() not in values:
            values.append(value.strip().lower())
    return values


def route_lane(status, labels):
    """The lane the card's column routes to, or None (a column we never start from)."""
    column = (status or "").strip()
    if column == "Ready":
        return "qa" if "qa" in {str(l).strip().lower() for l in labels or []} else "build"
    if column == "QA":
        return "qa"
    if column == "Review":
        return "review"
    return None


def new_event(key, source, actor, issue, pr, lane, card_status, node_id, url, created_at, now):
    return {
        "id": event_id(key),
        "key": key,
        "source": source,
        "actor": actor,
        "actor_permission": None,
        "issue": issue,
        "pr": pr,
        "lane": lane,
        "card_status": card_status,
        "node_id": node_id,
        "url": url,
        "created_at": created_at,
        "enqueued_at": iso(now),
        "state": "queued",
        "reason": None,
        "budget_reported": False,
    }


# ───────────────────────────── config ─────────────────────────────


def load_config(path):
    cfg_path = Path(path)
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise EventsError("config unreadable: %s" % error) from None
    except ValueError as error:
        raise EventsError("config is not valid JSON: %s" % error) from None
    if not isinstance(cfg, dict):
        raise EventsError("config must be a JSON object")
    resolved = cfg_path.resolve()
    parts = resolved.parents
    if len(parts) < 4 or parts[0].name != "configs" or parts[1].name != "super-board" \
            or parts[2].name != ".claude":
        raise EventsError("the config must live at <repo>/.claude/super-board/configs/<slug>.json")
    return cfg, resolved.stem, parts[3]


def events_cfg(cfg):
    """The events block with defaults; wrong-typed values fail closed (exit 64)."""
    block = cfg.get("events")
    if block is None:
        return None
    if not isinstance(block, dict):
        raise EventsError("config events must be an object")
    out = {
        "enabled": bool(block.get("enabled", False)),
        "initiators": block.get("initiators", []),
        "poll_seconds": block.get("poll_seconds", DEFAULT_POLL_SECONDS),
        "max_starts_per_hour": block.get("max_starts_per_hour", DEFAULT_MAX_STARTS_PER_HOUR),
        "max_starts_per_day": block.get("max_starts_per_day", DEFAULT_MAX_STARTS_PER_DAY),
        "queue_ttl_hours": block.get("queue_ttl_hours", DEFAULT_QUEUE_TTL_HOURS),
        "command": block.get("command", DEFAULT_COMMAND),
        "sources": dict(block.get("sources") or {}),
    }
    if not isinstance(out["initiators"], list) or not all(isinstance(i, str) for i in out["initiators"]):
        raise EventsError("config events.initiators must be a list of logins")
    for field in ("poll_seconds", "max_starts_per_hour", "max_starts_per_day"):
        value = out[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise EventsError("config events.%s must be a positive integer" % field)
    ttl = out["queue_ttl_hours"]
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 0:
        raise EventsError("config events.queue_ttl_hours must be a positive number")
    if not isinstance(out["command"], str) or not out["command"].strip():
        raise EventsError("config events.command must be a non-empty string")
    sources = out["sources"]
    if not isinstance(sources, dict):
        raise EventsError("config events.sources must be an object")
    for key in SOURCE_KEYS:
        value = sources.setdefault(key, True)
        if not isinstance(value, bool):
            raise EventsError("config events.sources.%s must be a boolean" % key)
    return out


def bot_identity(cfg):
    """The configured bot account, or None when events are disabled by it ('auto' or empty)."""
    notifications = cfg.get("notifications")
    value = notifications.get("bot_identity") if isinstance(notifications, dict) else None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or value.lower() == "auto":
        return None
    return value


def parse_sources(cfg_events):
    return {key: bool(cfg_events["sources"].get(key, True)) for key in SOURCE_KEYS}


# ───────────────────────────── state layout ─────────────────────────────


class Store:
    """The event state tree for one slug under <repo>/.claude/super-board/events/<slug>/."""

    def __init__(self, root, slug):
        self.root = root
        self.slug = slug
        self.base = root / ".claude" / "super-board"
        self.dir = self.base / "events" / slug
        self.inflight = self.base / "inflight"

    def folder(self, name):
        return self.dir / name

    def ensure(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        for name in FOLDERS:
            self.folder(name).mkdir(exist_ok=True)
        ledger = self.folder("starts.jsonl")
        if not ledger.exists():
            ledger.touch()

    def wave_lock_held(self):
        return (self.inflight / WAVE_LOCK[1]).exists()

    def card_inflight(self, number):
        return (self.inflight / ("issue-%s" % number)).exists()

    # -- cursor

    def read_cursor(self):
        path = self.dir / "cursor.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"watermark": None, "last_poll": None, "last_error": None}
        if not isinstance(data, dict):
            return {"watermark": None, "last_poll": None, "last_error": None}
        return {"watermark": data.get("watermark"), "last_poll": data.get("last_poll"),
                "last_error": data.get("last_error")}

    def write_cursor(self, data):
        write_atomic(self.dir / "cursor.json", data)

    # -- seen keys

    def read_seen(self):
        path = self.dir / "seen.jsonl"
        keys = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return keys
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            key = row.get("key")
            if isinstance(key, str):
                keys[key] = row.get("ts")
        return keys

    def write_seen(self, keys, now):
        cutoff = now - timedelta(days=SEEN_DAYS)
        rows = [{"key": key, "ts": ts} for key, ts in sorted(keys.items())
                if not isinstance(ts, str) or (parse_iso(ts) or now) >= cutoff]
        write_atomic(self.dir / "seen.jsonl", "".join(json.dumps(r) + "\n" for r in rows))

    # -- events

    def read_event(self, folder, event_id_value):
        path = self.folder(folder) / ("%s.json" % event_id_value)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise NotFoundError("unknown event id %s in %s" % (event_id_value, folder)) from None
        except ValueError as error:
            raise EventsError("event file %s is corrupt: %s" % (path, error)) from None

    def write_event(self, folder, event):
        write_atomic(self.folder(folder) / ("%s.json" % event["id"]), event)

    def move_event(self, event, from_folder, to_folder, state=None, reason=None):
        event = dict(event)
        if state is not None:
            event["state"] = state
        if reason is not None:
            event["reason"] = reason
        write_atomic(self.folder(to_folder) / ("%s.json" % event["id"]), event)
        path = self.folder(from_folder) / ("%s.json" % event["id"])
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return event

    # -- budget ledger

    def ledger_counts(self, now):
        """(last_hour, last_day) starts; a missing or unreadable ledger fails closed."""
        path = self.dir / "starts.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            raise RefusedError("events budget ledger missing: %s" % path) from None
        except OSError as error:
            raise RefusedError("events budget ledger unreadable: %s" % error) from None
        hour_ago = now - timedelta(hours=1)
        day_ago = now - timedelta(days=1)
        hour = day = 0
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                stamp = parse_iso(row.get("ts"))
                if stamp is None:
                    raise ValueError("no timestamp")
            except (ValueError, AttributeError):
                raise RefusedError("events budget ledger unreadable: %s" % path) from None
            if stamp >= hour_ago:
                hour += 1
            if stamp >= day_ago:
                day += 1
        return hour, day

    def record_start(self, event_id_value, now):
        path = self.dir / "starts.jsonl"
        try:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"id": event_id_value, "ts": iso(now)}) + "\n")
        except OSError as error:
            raise RefusedError("events budget ledger unwritable: %s" % error) from None


def write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data, indent=2) + "\n"
    tmp = path.with_name(path.name + ".tmp-%d" % time.time_ns())
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ───────────────────────────── the single read boundary ─────────────────────────────


def run_read(kind, args, repo=None):
    """One GitHub read through super-board-github-read.py. Returns the parsed payload.

    Halts (helper exit 79) raise ReadHalted. This is the only place this script touches
    GitHub, and every command it builds is a fixed read (SB_GITHUB_READ_STRICT=1).
    """
    env = dict(os.environ)
    env["SB_GITHUB_READ_STRICT"] = "1"
    if repo is not None:
        env["SB_REPO_PATH"] = str(repo)
    try:
        proc = subprocess.run([sys.executable, str(READ_HELPER), "--kind", kind, "--", *args],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120, env=env)
    except (OSError, subprocess.SubprocessError) as error:
        raise ReadHalted("the GitHub read helper could not run: %s" % error) from None
    if proc.returncode == 79:
        raise ReadHalted((proc.stderr or "").strip() or "GitHub reads are halted")
    if proc.returncode != 0:
        raise ReadHalted("the GitHub read helper failed (exit %s): %s"
                         % (proc.returncode, (proc.stderr or "").strip()[:200]))
    try:
        return json.loads(proc.stdout)
    except ValueError as error:
        raise ReadHalted("the GitHub read helper returned output that is not JSON: %s" % error) from None


def read_board(owner, name, project_number, repo):
    """All project items with their Status column, plus every Ready/QA/Review move event."""
    query = " ".join("""
        query($owner:String!,$name:String!,$number:Int!){
          repositoryOwner(login:$owner){ ... on ProjectV2Owner {
            projectV2(number:$number){ items(first:50){
              nodes{
                fieldValues(first:8){ nodes{ ... on ProjectV2ItemFieldSingleSelectValue{
                  name field{ ... on ProjectV2SingleSelectField{ name } } } } }
                content{
                  __typename
                  ... on Issue{ number url labels(first:20){ nodes{ name } }
                    timelineItems(last:10, itemTypes:[PROJECT_V2_ITEM_STATUS_CHANGED_EVENT]){
                      nodes{ ... on ProjectV2ItemStatusChangedEvent{
                        id createdAt actor{ login } previousStatus status project{ number } } } } }
                  ... on PullRequest{ number url closingIssuesReferences(first:10){ nodes{ number } } }
                }
              } } } } } }
    """.split())
    payload = run_read("json", ["api", "graphql", "-f", "query=" + query,
                                "-F", "owner=" + owner, "-F", "name=" + name,
                                "-F", "number=%d" % project_number], repo)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ReadHalted("the board read returned no data object")
    items, moves = {}, []
    nodes = (((payload["data"].get("repositoryOwner") or {}).get("projectV2") or {})
             .get("items") or {}).get("nodes")
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        content = node.get("content")
        if not isinstance(content, dict) or not isinstance(content.get("number"), int):
            continue
        number = content["number"]
        status = None
        for field_value in ((node.get("fieldValues") or {}).get("nodes") or []):
            if isinstance(field_value, dict) and isinstance(field_value.get("field"), dict) \
                    and field_value["field"].get("name") == "Status":
                status = field_value.get("name")
                break
        if content.get("__typename") == "PullRequest":
            links = (content.get("closingIssuesReferences") or {}).get("nodes") or []
            items[number] = {"status": status, "labels": [], "pr": True,
                             "closing": [n.get("number") for n in links if isinstance(n, dict)],
                             "url": content.get("url") or ""}
            continue
        labels = [n.get("name") for n in (content.get("labels") or {}).get("nodes") or []
                  if isinstance(n, dict)]
        items[number] = {"status": status, "labels": labels, "pr": False, "closing": [],
                         "url": content.get("url") or ""}
        timeline = (content.get("timelineItems") or {}).get("nodes") or []
        for event in timeline:
            if isinstance(event, dict) and event.get("__typename") == "ProjectV2ItemStatusChangedEvent":
                moves.append({"issue": number, "event": event})
    return items, moves


def read_issue_comments(owner, name, since, repo):
    """Conversation comments updated since the cursor, newest last."""
    payload = run_read("comments", ["api", "repos/%s/%s/issues/comments?since=%s&per_page=100"
                                    % (owner, name, since), "--paginate", "--slurp"], repo)
    if not isinstance(payload, list):
        raise ReadHalted("the conversation-comment read returned no page list")
    return [c for page in payload for c in page if isinstance(c, dict)]


def read_review_comments(owner, name, since, repo):
    """PR review comments updated since the cursor."""
    payload = run_read("json", ["api", "repos/%s/%s/pulls/comments?since=%s&per_page=100"
                                % (owner, name, since)], repo)
    if not isinstance(payload, list):
        raise ReadHalted("the review-comment read returned no list")
    return [c for c in payload if isinstance(c, dict)]


def read_correlations(owner, name, pr_numbers, repo):
    """Which issues each pull request closes (a batched read; PRs missing here are not PRs)."""
    if not pr_numbers:
        return {}
    aliases = []
    for index, number in enumerate(sorted(pr_numbers)):
        aliases.append("p%d: pullRequest(number:%d){ number closingIssuesReferences(first:10)"
                       "{ nodes{ number } } }" % (index, number))
    query = " ".join(("query($owner:String!,$name:String!){ repository(owner:$owner,name:$name){ %s } }"
                      % " ".join(aliases)).split())
    payload = run_read("json", ["api", "graphql", "-f", "query=" + query,
                                "-F", "owner=" + owner, "-F", "name=" + name], repo)
    out = {}
    data = payload.get("data") if isinstance(payload, dict) else None
    repository = (data or {}).get("repository") if isinstance(data, dict) else None
    for node in (repository or {}).values() if isinstance(repository, dict) else ():
        if not isinstance(node, dict):
            continue
        links = (node.get("closingIssuesReferences") or {}).get("nodes") or []
        out[node.get("number")] = [n.get("number") for n in links if isinstance(n, dict)]
    return out


def read_permission(owner, name, actor, repo):
    """The actor's live collaborator permission."""
    quoted = urllib.parse.quote(actor, safe="")
    return run_read("permission", ["api", "repos/%s/%s/collaborators/%s/permission" % (owner, name, quoted)], repo)


# ───────────────────────────── poll ─────────────────────────────


def parse_board_candidate(move, items, project_number):
    event = move["event"]
    if event.get("project", {}).get("number") != project_number:
        return None
    status = event.get("status")
    node_id = event.get("id") or ("move-%s-%s-%s" % (move["issue"], event.get("createdAt"),
                                                     event.get("status")))
    key = "ready_move:%s" % node_id
    stamp = parse_iso(event.get("createdAt"))
    if stamp is None:
        return None
    return {"source": "ready_move", "key": key, "node_id": str(node_id),
            "actor": (event.get("actor") or {}).get("login"),
            "issue": move["issue"], "pr": None, "card_status": status,
            "labels": (items.get(move["issue"]) or {}).get("labels") or [],
            "url": (items.get(move["issue"]) or {}).get("url") or "",
            "created_at": iso(stamp), "sort": (stamp, 0, str(node_id)),
            "needs": None}


def parse_comment_candidate(raw, source, items, pr_items, command):
    node_id = raw.get("node_id") or raw.get("id")
    stamp = parse_iso(raw.get("created_at"))
    if node_id is None or stamp is None:
        return None
    if source == "review_comment":
        pr_number = pr_number_of(raw.get("pull_request_url"))
        issue_number = None
        known = pr_number in pr_items
    else:
        issue_number = issue_number_of(raw.get("issue_url"))
        pr_number = None
        known = issue_number in items
    if known and source != "review_comment":
        if items[issue_number]["pr"]:
            source, pr_number, issue_number = "pr_comment", issue_number, None
    needs = None if known else pr_number if source == "review_comment" else issue_number
    key = "%s:%s" % (source, node_id)
    return {"source": source, "key": key, "node_id": str(node_id),
            "actor": (raw.get("user") or {}).get("login"),
            "issue": issue_number, "pr": pr_number, "card_status": None,
            "labels": [], "url": raw.get("html_url") or "",
            "created_at": iso(stamp), "sort": (stamp, 1 if source != "review_comment" else 2, str(node_id)),
            "needs": needs, "body_first_line": first_line(raw.get("body")),
            "command_ok": command_match(raw.get("body"), command)}


def issue_number_of(issue_url):
    m = re.search(r"/issues/(\d+)/?$", issue_url or "")
    return int(m.group(1)) if m else None


def pr_number_of(pull_url):
    m = re.search(r"/pulls/(\d+)/?$", pull_url or "")
    return int(m.group(1)) if m else None


def sweep(store, ttl_hours, now):
    """Orphaned claims go to failed/ (never re-run); past-TTL queued events expire."""
    ttl = timedelta(hours=ttl_hours)
    for event_id_value in folder_ids(store.folder("claimed")):
        event = store.read_event("claimed", event_id_value)
        claimed_at = parse_iso(event.get("claimed_at"))
        if claimed_at is not None and now - claimed_at > ttl:
            store.move_event(event, "claimed", "failed", state="failed",
                             reason="orphaned claim (never re-run)")
            print("orphaned claim -> failed: %s" % event_id_value, file=sys.stderr)
    for event_id_value in folder_ids(store.folder("queue")):
        event = store.read_event("queue", event_id_value)
        enqueued_at = parse_iso(event.get("enqueued_at"))
        if enqueued_at is not None and now - enqueued_at > ttl:
            store.move_event(event, "queue", "expired", state="expired", reason="ttl expired")


def folder_ids(folder):
    if not folder.exists():
        return []
    return sorted(p.stem for p in folder.glob("*.json"))


def poll(ctx):
    store, cfg_events, cfg = ctx.store, ctx.cfg_events, ctx.cfg
    if cfg_events is None or not cfg_events["enabled"] or bot_identity(cfg) is None:
        print("events disabled: no dispatch", file=sys.stderr)
        return 0
    owner_name = parse_remote((cfg.get("repo") or {}).get("remote"))
    if owner_name is None:
        raise EventsError("config repo.remote is missing or unparseable")
    owner, name = owner_name
    project_number = (cfg.get("project") or {}).get("number")
    if not isinstance(project_number, int):
        raise EventsError("config project.number must be an integer")
    store.ensure()
    now = datetime.now(timezone.utc)
    sweep(store, cfg_events["queue_ttl_hours"], now)

    cursor = store.read_cursor()
    watermark = parse_iso(cursor.get("watermark"))
    since_dt = ((watermark or now) - timedelta(seconds=OVERLAP_SECONDS))
    since = iso(since_dt)
    try:
        items, moves = read_board(owner, name, project_number, ctx.repo)
        conversations = read_issue_comments(owner, name, since, ctx.repo)
        reviews = read_review_comments(owner, name, since, ctx.repo)
    except ReadHalted as error:
        store.write_cursor({"watermark": cursor.get("watermark"), "last_poll": cursor.get("last_poll"),
                            "last_error": "%s" % error})
        print("poll halted: %s" % error, file=sys.stderr)
        return 79

    pr_items = {number: item for number, item in items.items() if item["pr"]}
    candidates = []
    for move in moves:
        candidate = parse_board_candidate(move, items, project_number)
        if candidate is not None:
            candidates.append(candidate)
    for raw in conversations:
        candidate = parse_comment_candidate(raw, "issue_comment", items, pr_items, cfg_events["command"])
        if candidate is not None:
            candidates.append(candidate)
    for raw in reviews:
        candidate = parse_comment_candidate(raw, "review_comment", items, pr_items, cfg_events["command"])
        if candidate is not None:
            candidates.append(candidate)
    candidates.sort(key=lambda c: c["sort"])

    # Correlation: conversation comments and review comments whose target is not a known
    # issue card may still be pull requests closing exactly one project issue.
    needing = {c["needs"] for c in candidates
               if c["source"] != "ready_move" and isinstance(c.get("needs"), int)}
    needs = sorted(needing)[:CORRELATION_BATCH]
    batch = set(needs)
    try:
        closing = read_correlations(owner, name, needs, ctx.repo)
    except ReadHalted as error:
        store.write_cursor({"watermark": cursor.get("watermark"), "last_poll": cursor.get("last_poll"),
                            "last_error": "correlation: %s" % error})
        print("poll halted: %s" % error, file=sys.stderr)
        return 79

    seen = store.read_seen()
    newest = watermark
    deferred = set()
    for candidate in candidates:
        if candidate["source"] != "ready_move" and isinstance(candidate.get("needs"), int) \
                and candidate["needs"] not in batch:
            deferred.add(candidate["sort"])
    lock_held = store.wave_lock_held()
    for candidate in candidates:
        stamp = parse_iso(candidate["created_at"])
        if candidate["sort"] in deferred:
            continue  # over the correlation batch: undecided, retried next tick
        if candidate["key"] in seen:
            continue
        decided = decide_event(store, cfg_events, candidate, items, pr_items, closing,
                               lock_held, now, bot_identity(ctx.cfg))
        if decided:
            seen[candidate["key"]] = candidate["created_at"]
        if stamp is not None and (newest is None or stamp > newest):
            newest = stamp
    if newest is not None and newest > now:
        newest = now
    store.write_seen(seen, now)
    store.write_cursor({"watermark": iso(newest) if newest else cursor.get("watermark"),
                        "last_poll": iso(now), "last_error": None})
    return 0


def decide_event(store, cfg_events, candidate, items, pr_items, closing, lock_held, now, bot):
    """One candidate → queued / dropped. Returns True when the event was decided."""
    def drop(reason):
        event = new_event(candidate["key"], candidate["source"], candidate["actor"],
                          candidate["issue"], candidate["pr"], None, candidate["card_status"],
                          candidate["node_id"], candidate["url"], candidate["created_at"], now)
        store.write_event("dropped", dict(event, state="dropped", reason=reason))
        print("dropped: %s (%s)" % (event["id"], reason), file=sys.stderr)
        return True

    if not parse_sources(cfg_events).get(candidate["source"], True):
        return drop("source-disabled (events.sources.%s=false)" % candidate["source"])
    if candidate["source"] != "ready_move" and not candidate.get("command_ok"):
        return drop("command first line mismatch (exact match required)")
    reason = offline_trust_reason(candidate["actor"], bot, cfg_events["initiators"])
    if reason:
        return drop("untrusted-actor: %s" % reason)

    if candidate["source"] == "ready_move":
        card = items.get(candidate["issue"])
        if card is None or card["pr"]:
            return drop("no issue card on the project")
        card_status = candidate["card_status"]
        labels = card["labels"]
        url = card["url"] or candidate["url"]
        issue, pr = candidate["issue"], None
    else:
        issue, pr = candidate["issue"], candidate["pr"]
        if issue is None or issue not in items or (pr is None and issue in pr_items):
            # the comment target is not (yet) a known issue card: correlate its pull request
            pr_number = candidate["pr"] if candidate["pr"] is not None else candidate["needs"]
            if pr_number is None:
                return drop("no issue card on the project")
            if pr_number in pr_items:
                links = pr_items[pr_number]["closing"]  # a board PR item carries its links on the board
            else:
                links = closing.get(pr_number)
                if links is None:
                    return drop("no pull request found for the comment target")
            linked = [n for n in links if isinstance(n, int) and n in items]
            if len(linked) != 1:
                return drop("correlation: %d linked issues on the project (exactly one required)"
                            % len(linked))
            issue, pr = linked[0], pr_number
        card = items.get(issue)
        if card is None or card["pr"]:
            return drop("no issue card on the project")
        card_status = card["status"]
        labels = card["labels"]
        url = candidate["url"] or card["url"]

    lane = route_lane(card_status, labels)
    if lane is None:
        return drop("column %r is not a lane start" % (card_status or "",))

    if lock_held:
        return drop("wave lock held (a wave is running)")
    if store.card_inflight(issue):
        return drop("card inflight (inflight/issue-%s)" % issue)

    event = new_event(candidate["key"], candidate["source"], candidate["actor"], issue, pr,
                      lane, card_status, candidate["node_id"], url, candidate["created_at"], now)
    # one queued event per card: the newest supersedes the older ones
    for other_id in folder_ids(store.folder("queue")):
        other = store.read_event("queue", other_id)
        if other.get("issue") != issue:
            continue
        other_stamp = parse_iso(other.get("created_at"))
        mine = parse_iso(event["created_at"])
        if other_stamp is not None and mine is not None and other_stamp > mine:
            store.write_event("dropped", dict(event, state="dropped",
                                              reason="superseded by %s" % other_id))
            print("dropped: %s (superseded by %s)" % (event["id"], other_id), file=sys.stderr)
            return True
        store.move_event(other, "queue", "dropped", state="dropped",
                         reason="superseded by %s" % event["id"])
        print("dropped: %s (superseded by %s)" % (other_id, event["id"]), file=sys.stderr)
    store.write_event("queue", event)
    print("queued: %s (#%s -> %s)" % (event["id"], issue, lane), file=sys.stderr)
    return True


# ───────────────────────────── next / claim / done / fail / status ─────────────────────────────


def check_dispatch_enabled(cfg, cfg_events, for_next):
    if cfg_events is None or not cfg_events["enabled"]:
        raise NothingDispatchable("events disabled: config")
    if bot_identity(cfg) is None:
        raise NothingDispatchable("events disabled: bot_identity")
    if for_next:
        return
    # (claim-time checks live in claim())


def budget_blocked(store, cfg_events, now, for_next):
    try:
        hour, day = store.ledger_counts(now)
    except RefusedError as error:
        # fail closed: at next this is 'nothing dispatchable'; at claim it is a refusal
        if for_next:
            raise NothingDispatchable(str(error)) from None
        raise
    if hour >= cfg_events["max_starts_per_hour"]:
        return "budget exceeded: %d starts in the last hour (cap %d), %d in the last day (cap %d)" \
               % (hour, cfg_events["max_starts_per_hour"], day, cfg_events["max_starts_per_day"])
    if day >= cfg_events["max_starts_per_day"]:
        return "budget exceeded: %d starts in the last day (cap %d)" % (day, cfg_events["max_starts_per_day"])
    return None


def next_command(ctx):
    store, cfg_events, cfg = ctx.store, ctx.cfg_events, ctx.cfg
    check_dispatch_enabled(cfg, cfg_events, for_next=True)
    now = datetime.now(timezone.utc)
    sweep(store, cfg_events["queue_ttl_hours"], now)
    ids = folder_ids(store.folder("queue"))
    if not ids:
        raise NothingDispatchable("no queued events")
    events = [store.read_event("queue", i) for i in ids]
    events.sort(key=lambda e: (e.get("enqueued_at") or "", e.get("created_at") or "", e.get("id") or ""))
    event = events[0]
    blocked = budget_blocked(store, cfg_events, now, for_next=True)
    if blocked:
        already = bool(event.get("budget_reported"))
        if not already:
            store.write_event("queue", dict(event, budget_reported=True))
        raise NothingDispatchable(blocked + (" (reported)" if already else ""))
    print(json.dumps(event))
    return 0


def claim_command(ctx, event_id_value):
    store, cfg_events, cfg = ctx.store, ctx.cfg_events, ctx.cfg
    check_dispatch_enabled(cfg, cfg_events, for_next=False)
    if not store.wave_lock_held():
        raise RefusedError("no wave lock held; take workflow-wave.lock first "
                           "(super-board-orch.sh lock-acquire)")
    now = datetime.now(timezone.utc)
    blocked = budget_blocked(store, cfg_events, now, for_next=False)
    event = store.read_event("queue", event_id_value)
    if blocked:
        if not event.get("budget_reported"):
            store.write_event("queue", dict(event, budget_reported=True))
        raise RefusedError(blocked)
    reason = offline_trust_reason(event.get("actor"), bot_identity(cfg), cfg_events["initiators"])
    if reason:
        store.move_event(event, "queue", "dropped", state="dropped", reason=reason)
        raise RefusedError("actor not trusted at claim time: %s" % reason)
    owner_name = parse_remote((cfg.get("repo") or {}).get("remote"))
    if owner_name is None:
        raise EventsError("config repo.remote is missing or unparseable")
    owner, name = owner_name
    try:
        payload = read_permission(owner, name, event.get("actor") or "", ctx.repo)
    except ReadHalted:
        print("claim halted: the live permission read failed; the event stays queued", file=sys.stderr)
        return 79
    levels = permission_level(payload)
    trusted = [level for level in levels if level in TRUSTED_PERMISSIONS]
    if not trusted:
        store.move_event(event, "queue", "dropped", state="dropped",
                         reason="live permission %s at claim time (trusted needs admin, maintain or write)"
                                % (", ".join(levels) or "none"))
        raise RefusedError("actor %s holds live permission %s; trusted needs admin, maintain or write"
                           % (event.get("actor"), ", ".join(levels) or "none"))
    event = store.move_event(event, "queue", "claimed", state="claimed")
    event["actor_permission"] = trusted[0]
    event["claimed_at"] = iso(now)
    store.write_event("claimed", event)
    store.record_start(event_id_value, now)
    print(json.dumps(event))
    return 0


def done_command(ctx, event_id_value, result):
    store = ctx.store
    event = store.read_event("claimed", event_id_value)
    store.move_event(event, "claimed", "done", state="done")
    done_event = store.read_event("done", event_id_value)
    done_event["result"] = result
    done_event["done_at"] = iso(datetime.now(timezone.utc))
    store.write_event("done", done_event)
    return 0


def fail_command(ctx, event_id_value, reason):
    store = ctx.store
    if (store.folder("claimed") / ("%s.json" % event_id_value)).exists():
        event = store.read_event("claimed", event_id_value)
        store.move_event(event, "claimed", "failed", state="failed", reason=reason)
        return 0
    event = store.read_event("queue", event_id_value)
    store.move_event(event, "queue", "failed", state="failed", reason=reason)
    return 0


def status_command(ctx):
    store, cfg_events, cfg = ctx.store, ctx.cfg_events, ctx.cfg
    counts = {name.replace("queue", "queued", 1): len(folder_ids(store.folder(name)))
              for name in FOLDERS}
    cursor = store.read_cursor()
    now = datetime.now(timezone.utc)
    try:
        hour, day = store.ledger_counts(now)
        ledger_ok = True
    except RefusedError:
        hour = day = 0
        ledger_ok = False
    print(json.dumps({
        "slug": store.slug,
        "enabled": bool(cfg_events and cfg_events["enabled"] and bot_identity(cfg) is not None),
        "bot_identity": bot_identity(cfg),
        "command": (cfg_events or {}).get("command"),
        "cursor": cursor,
        "counts": counts,
        "budget": {"last_hour": hour, "last_day": day,
                   "max_starts_per_hour": (cfg_events or {}).get("max_starts_per_hour"),
                   "max_starts_per_day": (cfg_events or {}).get("max_starts_per_day")},
        "ledger_ok": ledger_ok,
    }))
    return 0


# ───────────────────────────── entry point ─────────────────────────────


class Ctx:
    def __init__(self, cfg, slug, repo, cfg_events, store):
        self.cfg = cfg
        self.slug = slug
        self.repo = repo
        self.cfg_events = cfg_events
        self.store = store


def parse_argv(argv):
    config = None
    rest = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--config":
            if index + 1 >= len(argv):
                raise EventsError("--config needs a path")
            config = argv[index + 1]
            index += 2
            continue
        if arg.startswith("--config="):
            config = arg.split("=", 1)[1]
            index += 1
            continue
        rest.append(arg)
        index += 1
    return config, rest


USAGE = ("usage: super-board-events.py --config <config> poll|next|status\n"
         "       super-board-events.py --config <config> claim <id>\n"
         "       super-board-events.py --config <config> done <id> --result <text>\n"
         "       super-board-events.py --config <config> fail <id> --reason <text>")


def main(argv):
    try:
        config, rest = parse_argv(argv)
        if config is None:
            raise EventsError("no --config given")
        if not rest:
            raise EventsError(USAGE)
        cfg, slug, repo = load_config(config)
        cfg_events = events_cfg(cfg)
        ctx = Ctx(cfg, slug, repo, cfg_events, Store(repo, slug))
        command, args = rest[0], rest[1:]
        if command == "poll" and not args:
            return poll(ctx)
        if command == "next" and not args:
            return next_command(ctx)
        if command == "claim" and len(args) == 1:
            return claim_command(ctx, args[0])
        if command == "done" and args:
            return done_command(ctx, args[0], require_flag(args[1:], "--result"))
        if command == "fail" and args:
            return fail_command(ctx, args[0], require_flag(args[1:], "--reason"))
        if command == "status" and not args:
            return status_command(ctx)
        raise EventsError(USAGE)
    except EventsError as error:
        print("super-board-events: %s" % error, file=sys.stderr)
        return 64
    except NotFoundError as error:
        print("super-board-events: %s" % error, file=sys.stderr)
        return 5
    except RefusedError as error:
        print("super-board-events: %s" % error, file=sys.stderr)
        return 5
    except NothingDispatchable as error:
        print("super-board-events: %s" % error, file=sys.stderr)
        return 3
    except ReadHalted as error:
        print("super-board-events: %s" % error, file=sys.stderr)
        return 79


def require_flag(args, flag):
    for index, arg in enumerate(args):
        if arg == flag and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith(flag + "="):
            return arg.split("=", 1)[1]
    raise EventsError("%s is required (e.g. %s <text>)" % (flag, flag))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
