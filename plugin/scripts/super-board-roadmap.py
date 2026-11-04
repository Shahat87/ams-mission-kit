#!/usr/bin/env python3
"""super-board-roadmap.py — keeps the pinned `🗺️ Roadmap` issue current.

One GitHub milestone = one phase (order = milestone number, ascending). The
script renders a Mermaid flowchart plus a progress table from the milestones
and the project board, and writes it between two markers in one pinned issue.
Text outside the markers is never edited.

Verbs:
  render             --config <cfg> [--data <file.json>] [--now <iso>] [--wave N]
                     Print the marker block (and the "yours" line). No writes.
  sync               --config <cfg> [--data <file.json>] [--now <iso>] [--wave N]
                     Render, then create/update the pinned issue. First runs
                     `super-board-github-read.py --check`; a halt exits 79.
  create-milestones  --config <cfg> --plan <plan.json>
                     Create the planned milestones (in order) and set the
                     milestone on the listed issues. Never deletes or renames.

`--data` is a fixture: {"milestones": [...], "issues": [...], "items": [...],
"reasons": {"61": "blocked — 🛡 dependency gate"}}. It replaces the live GitHub reads so
tests run offline.

Cross-platform: pure Python 3 stdlib + `gh` CLI.

Exit codes:
  0  ok
  3  no milestones (nothing to show, not an error)
  64 bad args / bad config
  66 config not found
  67 gh / network failure
  79 GitHub halt (from --check)
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

START = "<!-- sb:roadmap -->"
END = "<!-- /sb:roadmap -->"
YOURS = "✍️ Anything below this line is yours; super-board never edits it."
TITLE = "🗺️ Roadmap"
COLUMNS = ("Done", "Building", "QA", "Review", "Blocked", "Backlog")
MAX_PAGES = 20
GH_TIMEOUT = 120

REASON_TABLE: list[tuple[str, str]] = [
    ("🛡", "dependency gate"),
    ("🔐", "missing creds"),
    ("💳", "quota / billing"),
    ("❓", "ambiguous AC"),
    ("⚙", "infra / tooling"),
]


# ───────────────────────────── copied helpers (status.py style) ─────────────────────────────

_CTRL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_title(s: str) -> str:
    return _CTRL_CHARS_RE.sub("", s)


def reason_for(body: str) -> tuple[str, str]:
    for em, txt in REASON_TABLE:
        if em in body:
            return em, txt
    return "🚫", "other"


def field_status(node: dict[str, Any]) -> str:
    return str((node.get("status") or {}).get("name") or "Backlog")


def field_value(node: dict[str, Any], name: str) -> str:
    """Agent / Skills value of a board item (the `agent` / `skills` fieldValueByName aliases):
    a single-select `name` or a text field's `text`."""
    fv = node.get(name.lower()) or {}
    return str(fv.get("name") or fv.get("text") or "").strip()


class GhError(Exception):
    def __init__(self, msg: str, stderr: str = ""):
        super().__init__(msg)
        self.stderr = stderr


def gh(*args: str, input: str | None = None) -> str:
    """Run gh, return stdout. Raises GhError on any failure."""
    exe = shutil.which("gh")
    if not exe:
        raise GhError("gh CLI not found on PATH")
    # Bytes in/out: text mode rewrites line endings on Windows and would
    # corrupt the issue body outside the markers.
    try:
        proc = subprocess.run(
            [exe, *args], capture_output=True, timeout=GH_TIMEOUT,
            input=input.encode("utf-8") if input is not None else None, check=False,
        )
    except subprocess.TimeoutExpired:
        raise GhError(f"gh call timed out after {GH_TIMEOUT}s ({' '.join(args[:3])}…)")
    except OSError as e:
        raise GhError(f"gh call could not start: {e}")
    if proc.returncode != 0:
        raise GhError(f"gh call failed ({' '.join(args[:3])}…)",
                      proc.stderr.decode("utf-8", "replace").strip())
    return proc.stdout.decode("utf-8", "replace")


ITEMS_QUERY = """
query($owner:String!, $number:Int!, $after:String) {
  repositoryOwner(login:$owner) {
    ... on ProjectV2Owner {
      projectV2(number:$number) {
        items(first:100, after:$after) {
          pageInfo { endCursor hasNextPage }
          nodes {
            content { ... on Issue { number title state repository { nameWithOwner } milestone { number } labels(first:20){nodes{name}} } }
            status: fieldValueByName(name:"Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
            agent: fieldValueByName(name:"Agent") { ... on ProjectV2ItemFieldSingleSelectValue { name } ... on ProjectV2ItemFieldTextValue { text } }
            skills: fieldValueByName(name:"Skills") { ... on ProjectV2ItemFieldTextValue { text } }
          }
        }
      }
    }
  }
}
"""

MAX_ITEM_PAGES = 20


def paginate_items(
    fetch: Callable[[str | None], dict[str, Any]],
    max_pages: int = MAX_ITEM_PAGES,
) -> tuple[list[dict[str, Any]], bool]:
    all_nodes: list[dict[str, Any]] = []
    after: str | None = None
    for _ in range(max_pages):
        payload = fetch(after)
        items_section = (
            ((payload.get("data") or {})
                .get("repositoryOwner") or {})
                .get("projectV2") or {}
        ).get("items") or {}
        all_nodes.extend(items_section.get("nodes") or [])
        page_info = items_section.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            return all_nodes, False
        nxt = page_info.get("endCursor")
        if not nxt or nxt == after:
            return all_nodes, True  # hasNextPage but no cursor progress: incomplete
        after = nxt
    return all_nodes, True


# ───────────────────────────── config ─────────────────────────────


class ConfigError(Exception):
    pass


def normalize_slug(remote: str) -> str:
    """https / ssh:// / git@host:o/r / .git / trailing slash → `owner/repo`."""
    s = (remote or "").strip().rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    s = s.rstrip("/")
    if "://" in s:
        rest = s.split("://", 1)[1]
        path = rest.split("/", 1)[1] if "/" in rest else ""
    elif "@" in s and ":" in s:
        path = s.split(":", 1)[1]
    else:
        path = s
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise ConfigError(f"cannot read owner/repo from repo.remote: {remote!r}")
    return f"{parts[-2]}/{parts[-1]}"


def load_config(path_arg: str) -> tuple[Path, dict[str, Any], str]:
    path = Path(path_arg)
    if not path.is_file():
        print(f"config not found: {path}", file=sys.stderr)
        sys.exit(66)
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        slug = normalize_slug((cfg.get("repo") or {}).get("remote", ""))
    except (json.JSONDecodeError, ConfigError, AttributeError) as e:
        print(f"bad config {path}: {e}", file=sys.stderr)
        sys.exit(64)
    return path, cfg, slug


def write_config_issue(path: Path, number: int) -> None:
    """Set roadmap.issue atomically (temp file + os.replace), keeping every other
    key, the file's indent and its line endings. Raises OSError on failure."""
    raw = path.read_bytes().decode("utf-8")
    crlf = "\r\n" in raw
    cfg = json.loads(raw)
    m = re.search(r"^( +|\t+)\S", raw, re.M)
    indent: Any = m.group(1) if m else 2
    roadmap = cfg.get("roadmap")
    if not isinstance(roadmap, dict):
        roadmap = {}
    roadmap["issue"] = number
    cfg["roadmap"] = roadmap
    out = json.dumps(cfg, indent=indent, ensure_ascii=False)
    if raw.endswith("\n"):
        out += "\n"
    if crlf:
        out = out.replace("\r\n", "\n").replace("\n", "\r\n")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(out)
        try:
            shutil.copymode(path, tmp)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ───────────────────────────── data collection ─────────────────────────────


def rest_list(path: str) -> list[dict[str, Any]]:
    sep = "&" if "?" in path else "?"
    out: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        rows = json.loads(gh("api", f"{path}{sep}per_page=100&page={page}") or "[]")
        out.extend(rows)
        if len(rows) < 100:
            return out
    # Safety cap hit with a full last page: data is incomplete, never publish it.
    raise GhError(f"pagination stopped at the {MAX_PAGES}-page cap for {path}; data incomplete")


def items_from_nodes(nodes: list[dict[str, Any]], slug: str) -> list[dict[str, Any]]:
    """Board nodes -> items, dropping issues that belong to another repository."""
    items: list[dict[str, Any]] = []
    for n in nodes:
        c = n.get("content") or {}
        if not c.get("number"):
            continue
        repo = ((c.get("repository") or {}).get("nameWithOwner") or "").lower()
        if repo != slug.lower():
            continue
        items.append({
            "number": c["number"], "title": c.get("title") or "",
            "state": c.get("state") or "OPEN",
            "milestone": (c.get("milestone") or {}).get("number"),
            "status": field_status(n),
            "agent": field_value(n, "Agent"), "skills": field_value(n, "Skills"),
            "labels": [l["name"] for l in (c.get("labels", {}).get("nodes") or []) if l.get("name")],
        })
    return items


MAX_REASONS = 10


def collect_live(cfg: dict[str, Any], slug: str) -> dict[str, Any]:
    milestones = rest_list(f"repos/{slug}/milestones?state=all")
    data: dict[str, Any] = {"milestones": [], "issues": [], "items": [], "reasons": {}}
    for m in milestones:
        data["milestones"].append({
            "number": m["number"], "title": m.get("title") or "",
            "state": m.get("state") or "open", "html_url": m.get("html_url") or "",
        })
    # One paginated read for every issue that has a milestone, bucketed by milestone number.
    for i in rest_list(f"repos/{slug}/issues?state=all&milestone=*"):
        if "pull_request" in i:
            continue
        ms = (i.get("milestone") or {}).get("number")
        if ms is None:
            continue
        data["issues"].append({
            "number": i["number"], "title": i.get("title") or "",
            "state": i.get("state") or "open", "milestone": ms,
        })
    proj = cfg.get("project") or {}
    owner, number = proj.get("owner"), proj.get("number")
    if owner is not None and number is not None:
        def fetch_page(after: str | None) -> dict[str, Any]:
            args = ["api", "graphql", "-f", f"query={ITEMS_QUERY}",
                    "-F", f"owner={owner}", "-F", f"number={int(number)}"]
            if after:
                args += ["-F", f"after={after}"]
            payload = json.loads(gh(*args))
            if payload.get("errors"):
                raise GhError("graphql returned errors: " +
                              "; ".join(str(e.get("message")) for e in payload["errors"]))
            return payload

        nodes, truncated = paginate_items(fetch_page)
        if truncated:
            raise GhError("board items pagination incomplete; not publishing partial data")
        data["items"] = items_from_nodes(nodes, slug)
    # Blocked reasons only for the Blocked cards that will be shown (active phase), max 10.
    model = build_model(data)
    if model["active"] is not None:
        shown = [c for c in model["phases"][model["active"]]["cards"] if c["col"] == "Blocked"]
        for c in shown[:MAX_REASONS]:
            try:
                comments = json.loads(gh("issue", "view", str(c["number"]), "--repo", slug,
                                         "--json", "comments")).get("comments") or []
            except (GhError, json.JSONDecodeError):
                continue
            comments.sort(key=lambda x: x.get("createdAt", ""))
            body = (comments[-1].get("body") if comments else "") or ""
            data["reasons"][str(c["number"])] = body[:4000]
    return data


# ───────────────────────────── model ─────────────────────────────


# A card's type: the routing labels plus the ones that keep their own label chain. Same set as card.sh.
TYPE_LABELS = ("bug", "feature", "qa", "docs", "ux", "tests", "refactor", "tech-debt", "money")
LANE_COLS = ("Building", "QA", "Review", "Blocked")


def _item_fields(it: dict[str, Any]) -> dict[str, Any]:
    return {"agent": str(it.get("agent") or "").strip(), "skills": str(it.get("skills") or "").strip(),
            "labels": [str(l).lower() for l in (it.get("labels") or [])]}


def missing_fields(cards: list[dict[str, Any]]) -> list[str]:
    """One `#N Status missing: agent,skills,label` line per lane card (Building, QA, Review, Blocked) that
    lacks an Agent value, a Skills value or a type label. Same wording as the card.sh audit."""
    lines = []
    for c in cards:
        if c["col"] not in LANE_COLS or c.get("state") == "CLOSED" or "agent" not in c:
            continue
        miss = [k for k, bad in (("agent", not c["agent"]), ("skills", not c["skills"]),
                                 ("label", not any(l in TYPE_LABELS for l in c["labels"]))) if bad]
        if miss:
            lines.append(f"#{c['number']} {c['col']} missing: {','.join(miss)}")
    return lines


def build_cards(data: dict[str, Any]) -> list[dict[str, Any]]:
    cards: dict[int, dict[str, Any]] = {}
    for i in data.get("issues") or []:
        cards[int(i["number"])] = {
            "number": int(i["number"]), "title": sanitize_title(i.get("title") or ""),
            "state": str(i.get("state") or "open").upper(),
            "milestone": i.get("milestone"), "status": None,
        }
    for it in data.get("items") or []:
        n = int(it["number"])
        if n in cards:
            cards[n]["status"] = it.get("status")
            cards[n].update(_item_fields(it))
        else:
            cards[n] = {
                "number": n, "title": sanitize_title(it.get("title") or ""),
                "state": str(it.get("state") or "open").upper(),
                "milestone": it.get("milestone"), "status": it.get("status"),
                **_item_fields(it),
            }
    out = []
    for c in cards.values():
        if c["state"] == "CLOSED" or c["status"] == "Done":
            c["col"] = "Done"
        elif c["status"] in ("Building", "QA", "Review", "Blocked"):
            c["col"] = c["status"]
        else:
            c["col"] = "Backlog"
        out.append(c)
    return sorted(out, key=lambda c: c["number"])


def tally(cards: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {c: 0 for c in COLUMNS}
    for c in cards:
        counts[c["col"]] += 1
    total = len(cards)
    pct = counts["Done"] * 100 // total if total else 0  # floor: 100 only when all done
    return {"counts": counts, "total": total, "done": counts["Done"], "pct": pct}


def build_model(data: dict[str, Any]) -> dict[str, Any]:
    cards = build_cards(data)
    phases = []
    for m in sorted(data.get("milestones") or [], key=lambda m: int(m["number"])):
        mine = [c for c in cards if c["milestone"] == m["number"]]
        t = tally(mine)
        closed = str(m.get("state") or "open").lower() == "closed"
        phases.append({
            "number": m["number"], "title": sanitize_title(m.get("title") or ""),
            "url": m.get("html_url") or "", "cards": mine, **t,
            "finished": closed or (t["total"] > 0 and t["pct"] == 100),
        })
    known = {p["number"] for p in phases}
    loose = [c for c in cards if c["milestone"] not in known]
    active = next((i for i, p in enumerate(phases)
                   if not p["finished"] and p["total"] > 0), None)
    for i, p in enumerate(phases):
        p["klass"] = "done" if p["finished"] else ("active" if i == active else "todo")
    return {"phases": phases, "active": active, "loose": tally(loose) if loose else None,
            "missing": missing_fields(cards),
            "reasons": data.get("reasons") or {}}


# ───────────────────────────── rendering ─────────────────────────────


def mm(s: str) -> str:
    """Escape for a quoted Mermaid label."""
    return (s.replace("#", "#35;").replace('"', "#quot;").replace("<", "#lt;")
             .replace(">", "#gt;").replace("|", "#124;"))


def md(s: str) -> str:
    """Escape for a Markdown table cell / list line."""
    s = s.replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\\[\]()|*_`])", r"\\\1", s)


def bar(pct: int) -> str:
    n = pct // 10
    return "█" * n + "░" * (10 - n)


def stamp(now_iso: str | None) -> str:
    if now_iso:
        dt = datetime.datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(datetime.timezone.utc)
    else:
        dt = datetime.datetime.now(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M GMT")


def render_block(model: dict[str, Any], now_iso: str | None, wave: int | None) -> str:
    phases = model["phases"]
    active = model["active"]
    when = f"updated {stamp(now_iso)}" + (f" by wave {wave}" if wave is not None else "")
    lines = [START, ""]

    all_blocked = sum(p["counts"]["Blocked"] for p in phases) + (
        model["loose"]["counts"]["Blocked"] if model["loose"] else 0)
    if active is not None:
        a = phases[active]
        note = (f"We are in phase {active + 1} of {len(phases)}: {md(a['title'])} · {a['pct']}% done"
                f" · 🔴 {a['counts']['Blocked']} Blocked · {when}")
    elif all(p["finished"] for p in phases):
        note = f"All {len(phases)} phases done · 🔴 {all_blocked} Blocked · {when}"
    else:
        note = f"No active phase: the open milestones have no issues yet · 🔴 {all_blocked} Blocked · {when}"
    lines += ["> [!NOTE]", f"> {note}", ""]

    lines += ["```mermaid", "flowchart LR"]
    emoji = {"done": "✅", "active": "🔨", "todo": "⏳"}
    for i, p in enumerate(phases, 1):
        label = f"{emoji[p['klass']]} {mm(p['title'])}<br/>{p['pct']}% · {p['done']}/{p['total']}"
        lines.append(f'  P{i}["{label}"]')
    if len(phases) > 1:
        lines.append("  " + " --> ".join(f"P{i}" for i in range(1, len(phases) + 1)))
    lines += [
        "  classDef done fill:#d1fae5,stroke:#059669,color:#064e3b",
        "  classDef active fill:#fef3c7,stroke:#d97706,color:#78350f",
        "  classDef todo fill:#f3f4f6,stroke:#9ca3af,color:#374151",
    ]
    for k in ("done", "active", "todo"):
        ids = [f"P{i}" for i, p in enumerate(phases, 1) if p["klass"] == k]
        if ids:
            lines.append(f"  class {','.join(ids)} {k}")
    lines += ["```", ""]

    lines += ["| Phase | Progress | " + " | ".join(COLUMNS) + " |",
              "|---|---|" + "|".join(["--:"] * len(COLUMNS)) + "|"]

    def row(name: str, t: dict[str, Any]) -> str:
        cells = " | ".join(str(t["counts"][c]) for c in COLUMNS)
        return f"| {name} | `{bar(t['pct'])}` {t['pct']}% | {cells} |"

    for p in phases:
        name = f"[{md(p['title'])}]({p['url']})" if p["url"] else md(p["title"])
        lines.append(row(name, p))
    if model["loose"]:
        lines.append(row("⚪ No phase", model["loose"]))
    lines.append("")
    lines.append("<sub>Percent counts issues only. GitHub's milestone page also counts pull requests, "
                 "so its percent can differ.</sub>")
    lines.append("")

    if active is not None:
        a = phases[active]
        reasons = model["reasons"]
        open_cards = [c for c in a["cards"] if c["col"] != "Done"]
        flight = [c for c in open_cards if c["col"] in ("Building", "QA", "Review")]
        blocked = [c for c in open_cards if c["col"] == "Blocked"]
        nxt = [c for c in open_cards if c["col"] == "Backlog"][:3]
        glyph = {"Building": "🔨", "QA": "🔍", "Review": "✏️"}
        lines += [f"### Now — {md(a['title'])}", ""]
        lines.append("**In flight**")
        lines += [f"- {glyph[c['col']]} #{c['number']} {md(c['title'])} ({c['col']})" for c in flight] or ["- none"]
        lines += ["", "**🔴 Blocked**"]
        for c in blocked:
            em, txt = reason_for(reasons.get(str(c["number"]), ""))
            lines.append(f"- #{c['number']} {md(c['title'])} — {em} {txt}")
        if not blocked:
            lines.append("- none")
        lines += ["", "**Next up**"]
        lines += [f"- #{c['number']} {md(c['title'])}" for c in nxt] or ["- none"]
        lines.append("")
        done_cards = [c for c in a["cards"] if c["col"] == "Done"]
        lines += ["<details>", f"<summary>✅ Done in this phase ({len(done_cards)})</summary>", ""]
        lines += [f"- #{c['number']} {md(c['title'])}" for c in done_cards] or ["- none"]
        lines += ["", "</details>", ""]

    if model.get("missing"):
        lines += ["### ⚠️ Cards missing fields", ""]
        lines += [f"- {l}" for l in model["missing"]]
        lines.append("")

    lines.append(END)
    return "\n".join(lines)


def render_full(block: str) -> str:
    return block + "\n\n" + YOURS + "\n"


_UPDATED_RE = re.compile(r"· updated [^\r\n]*")


def normalize_for_compare(block: str) -> str:
    return _UPDATED_RE.sub("· updated <ts>", block).replace("\r\n", "\n")


def splice_body(body: str, block: str) -> tuple[str, bool]:
    """Return (new_body, changed). Only text between markers is replaced."""
    s = body.find(START)
    e = body.find(END, s + len(START)) if s != -1 else -1
    if s == -1 or e == -1:
        return render_full(block) + ("\n" + body if body else ""), True
    e_end = e + len(END)
    old = body[s:e_end]
    if normalize_for_compare(old) == normalize_for_compare(block):
        return body, False
    return body[:s] + block + body[e_end:], True


# ───────────────────────────── verbs ─────────────────────────────


def run_check() -> int:
    """0 ok · 79 only when the reader itself exits 79 · 67 for anything else."""
    reader = Path(__file__).resolve().parent / "super-board-github-read.py"
    if not reader.is_file():
        print(f"github reader not found: {reader}", file=sys.stderr)
        return 67
    try:
        proc = subprocess.run([sys.executable, str(reader), "--check"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=GH_TIMEOUT)
    except subprocess.TimeoutExpired:
        print(f"github reader --check timed out after {GH_TIMEOUT}s", file=sys.stderr)
        return 67
    except OSError as e:
        print(f"github reader --check could not run: {e}", file=sys.stderr)
        return 67
    if proc.returncode == 0:
        return 0
    if proc.stderr.strip():
        print(proc.stderr.strip(), file=sys.stderr)
    return 79 if proc.returncode == 79 else 67


def get_data(args: argparse.Namespace, cfg: dict[str, Any], slug: str) -> dict[str, Any]:
    if args.data:
        return json.loads(Path(args.data).read_text(encoding="utf-8"))
    return collect_live(cfg, slug)


def cmd_render(args: argparse.Namespace) -> int:
    _, cfg, slug = load_config(args.config)
    try:
        data = get_data(args, cfg, slug)
    except (GhError, ValueError, KeyError, TypeError, OSError) as e:
        print(f"roadmap read failed: {e}", file=sys.stderr)
        return 67
    if not data.get("milestones"):
        print("no milestones: nothing to show", file=sys.stderr)
        return 3
    print(render_full(render_block(build_model(data), args.now, args.wave)), end="")
    return 0


def issue_number_from_url(out: str) -> int:
    m = re.search(r"/issues/(\d+)\s*$", out.strip())
    if not m:
        raise GhError(f"cannot read issue number from: {out.strip()!r}")
    return int(m.group(1))


def issue_missing(stderr: str) -> bool:
    """True only when gh says the ISSUE is missing (not a transport error)."""
    low = (stderr or "").lower()
    return any(k in low for k in ("could not resolve to an issue", "http 404", "issue not found"))


def configured_issue(cfg: dict[str, Any]) -> int | None:
    issue = (cfg.get("roadmap") or {}).get("issue")
    if isinstance(issue, bool):
        return None
    if isinstance(issue, int):
        return issue if issue > 0 else None
    if isinstance(issue, str) and issue.isascii() and issue.isdigit() and int(issue) > 0:
        return int(issue)
    return None


def read_issue_body(slug: str, n: int) -> str | None:
    """Body of an OPEN issue; None when it is closed or does not exist.
    Any other gh failure (network, timeout, 5xx) raises GhError."""
    try:
        view = json.loads(gh("issue", "view", str(n), "--repo", slug, "--json", "state,body"))
    except GhError as e:
        if issue_missing(e.stderr):
            return None
        raise
    if str(view.get("state", "")).upper() == "CLOSED":
        return None
    return view.get("body") or ""


def find_existing_issue(slug: str) -> int | None:
    """Lowest-numbered open issue whose title is exactly TITLE."""
    rows = json.loads(gh("issue", "list", "--repo", slug, "--state", "open",
                         "--search", f"{TITLE} in:title", "--limit", "100",
                         "--json", "number,title") or "[]")
    nums = [int(r["number"]) for r in rows if r.get("title") == TITLE]
    return min(nums) if nums else None


def ensure_pinned(slug: str, n: int) -> None:
    """Pin the issue when it is not pinned. A failure is one stderr line, never an exit code."""
    try:
        view = json.loads(gh("issue", "view", str(n), "--repo", slug, "--json", "isPinned"))
        if not view.get("isPinned"):
            gh("issue", "pin", str(n), "--repo", slug)
    except (GhError, json.JSONDecodeError) as e:
        print(f"warning: could not pin issue #{n}: {getattr(e, 'stderr', '') or e}", file=sys.stderr)


def cmd_sync(args: argparse.Namespace) -> int:
    cfg_path, cfg, slug = load_config(args.config)
    rc = run_check()
    if rc != 0:
        return rc
    try:
        data = get_data(args, cfg, slug)
    except (GhError, ValueError, KeyError, TypeError, OSError) as e:
        print(f"roadmap read failed: {e}", file=sys.stderr)
        return 67
    if not data.get("milestones"):
        print("no milestones: nothing to show", file=sys.stderr)
        return 3
    block = render_block(build_model(data), args.now, args.wave)

    try:
        number = configured_issue(cfg)
        body: str | None = read_issue_body(slug, number) if number is not None else None
        if body is None:
            number = None
            found = find_existing_issue(slug)
            if found is not None:
                body = read_issue_body(slug, found)
                if body is not None:
                    number = found
                    try:
                        write_config_issue(cfg_path, number)
                    except OSError as e:
                        print(f"warning: could not save roadmap.issue={number} to the config: {e}",
                              file=sys.stderr)
        if number is None or body is None:
            out = gh("issue", "create", "--repo", slug, "--title", TITLE,
                     "--body-file", "-", input=render_full(block))
            created = issue_number_from_url(out)
            ensure_pinned(slug, created)
            try:
                write_config_issue(cfg_path, created)
            except (OSError, ValueError) as e:
                print(f"roadmap: created issue #{created} but could not save it to the config: {e}",
                      file=sys.stderr)
                return 67
            print(f"roadmap: created issue #{created}")
            return 0
        new_body, changed = splice_body(body, block)
        if not changed:
            print(f"roadmap: issue #{number} unchanged")
            ensure_pinned(slug, number)
            return 0
        # Re-read right before the edit: the owner may have typed in the meantime.
        if read_issue_body(slug, number) != body:
            print(f"roadmap: issue #{number} changed while syncing; edit skipped, the next wave retries",
                  file=sys.stderr)
            return 0
        gh("issue", "edit", str(number), "--repo", slug, "--body-file", "-", input=new_body)
        ensure_pinned(slug, number)
        print(f"roadmap: issue #{number} updated")
        return 0
    except (GhError, ValueError, KeyError, TypeError) as e:
        print(f"roadmap sync failed: {e}", file=sys.stderr)
        if isinstance(e, GhError) and e.stderr:
            print(e.stderr, file=sys.stderr)
        return 67


def validate_plan(plan: Any) -> str | None:
    """Return a problem description, or None when the plan is fine."""
    if not isinstance(plan, dict):
        return "plan must be a JSON object"
    entries = plan.get("milestones")
    if not isinstance(entries, list) or not entries:
        return "`milestones` must be a non-empty list"
    seen_titles: set[str] = set()
    seen_issues: dict[int, str] = {}
    for k, ent in enumerate(entries, 1):
        if not isinstance(ent, dict):
            return f"phase {k} must be an object"
        title = ent.get("title")
        if not isinstance(title, str) or not title.strip():
            return f"phase {k}: title must be a non-empty string"
        if title in seen_titles:
            return f"phase {k}: duplicate title {title!r}"
        seen_titles.add(title)
        if "description" in ent and not isinstance(ent["description"], str):
            return f"phase {k}: description must be a string"
        issues = ent.get("issues", [])
        if not isinstance(issues, list):
            return f"phase {k}: issues must be a list"
        for n in issues:
            if type(n) is not int or n <= 0:
                return f"phase {k}: issue {n!r} is not a positive integer"
            if n in seen_issues:
                return f"issue #{n} is in two phases ({seen_issues[n]!r} and {title!r})"
            seen_issues[n] = title
    return None


def cmd_create_milestones(args: argparse.Namespace) -> int:
    _, cfg, slug = load_config(args.config)
    try:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"bad plan {args.plan}: {e!r}", file=sys.stderr)
        return 64
    problem = validate_plan(plan)
    if problem:
        print(f"bad plan {args.plan}: {problem}", file=sys.stderr)
        return 64
    entries = plan["milestones"]
    rc = run_check()
    if rc != 0:
        return rc
    try:
        rows = rest_list(f"repos/{slug}/milestones?state=all")
        open_ms = {m["title"]: m["number"] for m in rows if str(m.get("state", "open")).lower() != "closed"}
        closed_titles = {m["title"] for m in rows if str(m.get("state", "open")).lower() == "closed"}
        for ent in entries:
            if ent["title"] not in open_ms and ent["title"] in closed_titles:
                print(f"bad plan {args.plan}: milestone {ent['title']!r} already exists and is closed; "
                      "rename the phase or reopen it yourself", file=sys.stderr)
                return 64
        for ent in entries:
            title = ent["title"]
            if title in open_ms:
                num = open_ms[title]
            else:
                created = json.loads(gh("api", f"repos/{slug}/milestones", "-X", "POST",
                                        "-f", f"title={title}",
                                        "-f", f"description={ent.get('description', '')}"))
                num = created["number"]
                open_ms[title] = num
                print(f"milestone #{num} created: {title}")
            for issue in ent.get("issues") or []:
                gh("api", f"repos/{slug}/issues/{issue}", "-X", "PATCH", "-F", f"milestone={num}")
        return 0
    except (GhError, ValueError, KeyError, TypeError) as e:
        print(f"create-milestones failed: {e}", file=sys.stderr)
        if isinstance(e, GhError) and e.stderr:
            print(e.stderr, file=sys.stderr)
        return 67


class Parser(argparse.ArgumentParser):
    def error(self, message: str):  # argparse exits 2 by default; the contract says 64
        print(f"error: {message}", file=sys.stderr)
        sys.exit(64)


def main(argv: list[str] | None = None) -> int:
    p = Parser(description="super-board roadmap issue")
    sub = p.add_subparsers(dest="verb", required=True, parser_class=Parser)
    for name in ("render", "sync"):
        s = sub.add_parser(name)
        s.add_argument("--config", required=True)
        s.add_argument("--data")
        s.add_argument("--now")
        s.add_argument("--wave", type=int)
    s = sub.add_parser("create-milestones")
    s.add_argument("--config", required=True)
    s.add_argument("--plan", required=True)
    args = p.parse_args(argv)
    if getattr(args, "now", None):
        try:
            datetime.datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        except ValueError:
            print(f"bad --now: {args.now!r}", file=sys.stderr)
            return 64
    return {"render": cmd_render, "sync": cmd_sync,
            "create-milestones": cmd_create_milestones}[args.verb](args)


if __name__ == "__main__":
    sys.exit(main())
