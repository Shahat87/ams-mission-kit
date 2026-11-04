"""Impeccable map data file for diagrams/_shared/build_skillmap.py."""
import html, json, re
from data import *
import gen
e = html.escape
commands = {c[0]: c for c in C}
CMDS = "|".join(sorted(commands, key=len, reverse=True))
def chipify(t):  # command names in plain NEXT text -> [[chip]]
    return re.sub(r"\b(" + CMDS + r")\b", r"[[\1]]", t) if t not in ("Done. Ship it",) else t
def step(cid, t):  # bold the first verb; one highlight per section
    h = HOWH.get(cid)
    if h and h in t: t = t.replace(h, "==" + h + "==", 1)
    elif len(t.split(" ")[0]) > 3: t = "**" + t.split(" ")[0] + "**" + t[len(t.split(" ")[0]):]
    return t
def name(cid): return "/impeccable" if cid == "menu" else commands[cid][1]
MODELS = {}
for cid, c in commands.items():
    B = [{"label": "When to use it", "key": "when", "type": "when", "v": WHENL[cid]},
         {"label": "What it is", "key": "is", "type": "p", "v": ONE[cid]},
         {"label": "How it works", "key": "how", "type": "ol", "v": [step(cid, t) for t in HOW[cid]]}]
    sit, typ, steps, get = EX2[cid]
    B.append({"label": "Example", "key": "ex", "type": "example", "note": "Example. A realistic case, written from the reference file.",
              "v": [["Situation", sit], ["You type", typ, "code"], ["What happens", steps], ["You get", get]]})
    if cid == "menu":
        B.append({"label": "How it picks", "key": "picks", "type": "flows", "v": ROUTE_PICKS})
        B.append({"label": "Or just say it in words", "key": "picks", "type": "flows", "v": ROUTE_WORDS})
    else:
        B.append({"label": "Behind the scenes", "key": "bts", "type": "flows", "v": FLOWS[cid]})
    if cid in CALLS:
        B.append({"label": "Calls these", "key": "calls", "type": "flows", "v": [["/" + name(cid), ["@" + k for k, _ in CALLS[cid]]]]})
    B.append({"label": "What you get", "key": "get", "type": "p", "v": GET[cid]})
    if NEXT[cid]: B.append({"label": "Next", "key": "next", "type": "p", "v": chipify(NEXT[cid])})
    MODELS[cid] = {"title": name(cid), "accent": "g-rt" if cid == "menu" else "g-" + c[2], "tags": [[TRIG[cid], 1], ["router" if cid == "menu" else dict((g[0], g[1]) for g in GROUPS)[c[2]].split(" · ")[0]]], "blocks": B}
for group, title, subtitle, ids in STAGES:
    x = STAGEX[group]; sit, typ, steps, get = STAGE_EX2[group]
    MODELS["stage_" + group] = {"title": title, "accent": "g-" + group, "tags": [["STAGE · GROUP", 1]], "blocks": [
        {"label": "What this stage is for", "key": "grp", "type": "p", "v": x["for"]},
        {"label": "Not a command", "key": "warn", "type": "note", "v": '**You cannot run "' + title + '".** It is a **group label**. You run the **commands inside it**.'},
        {"label": "When X → pick Y", "key": "when", "type": "when", "v": STAGE_WHEN[group]},
        {"label": "No router for this stage yet", "key": "picks", "type": "p", "v": "Pick by the symptom above. " + x["run"]},
        {"label": "Example", "key": "ex", "type": "example", "note": "Example. A realistic case.", "v": [["Situation", sit], ["You type", typ, "code"], ["What happens", steps], ["You get", get]]}]}

COLORS = "".join(f".g-{g[0]}{{--c:var(--{g[0]})}}" for g in GROUPS) + ".g-rt{--c:var(--rt)}"
LIGHT = "--ctx:#ea580c;--plan:#c2410c;--diag:#e11d48;--fix:#059669;--style:#0891b2;--motion:#0e7490;--fin:#475569;--sys:#7c3aed;--rt:#7c3aed;--util:#d97706;"
DARK = "--ctx:#fbbf24;--plan:#fb923c;--diag:#fb7185;--fix:#34d399;--style:#22d3ee;--motion:#22d3ee;--fin:#94a3b8;--util:#a78bfa;--rt:#a78bfa;"
PAGECSS = (":root{" + LIGHT + "}\n@media (prefers-color-scheme:dark){:root:not([data-theme=\"light\"]){" + DARK + "}}\n:root[data-theme=\"dark\"]{" + DARK + "}\n" + COLORS + "\n" + r'''
svg.map text{font-family:inherit;fill:var(--fg)}
.r rect{fill:none;stroke:var(--c);stroke-opacity:.55;stroke-dasharray:5 5}
.r text{font-size:12px;font-weight:700;fill:var(--c);text-transform:uppercase;letter-spacing:.04em}
.n{cursor:pointer;outline:none}
.n rect{fill:color-mix(in srgb,var(--c) 13%,var(--card));stroke:var(--c);stroke-width:1.5}
.n.stage rect{fill:color-mix(in srgb,var(--c) 24%,var(--card));stroke-width:2}
.n:hover rect,.n:focus-visible rect{stroke-width:3} .n.sel rect{stroke-width:3.5;fill:color-mix(in srgb,var(--c) 30%,var(--card))}
.t1{font:600 14px ui-monospace,Menlo,monospace} .t2{font-size:10.5px;fill:var(--muted)!important}
.arrow{stroke:var(--muted);stroke-width:1.6;fill:none} .ahead{fill:var(--muted)}
.stagehead{cursor:pointer;outline:none} .stagehead .s1{font-size:9.5px;letter-spacing:.08em;font-weight:700;fill:var(--muted)!important}
.stagehead .s2{font-size:17px;font-weight:800;fill:var(--c)!important} .stagehead .s3{font-size:11px;fill:var(--muted)!important;font-weight:400}
.stagehead .bar{fill:var(--c);opacity:.8} .stagehead:hover .s2,.stagehead:focus-visible .s2,.stagehead.sel .s2{text-decoration:underline}
.router{cursor:pointer;outline:none} .router rect{fill:color-mix(in srgb,var(--rt) 18%,var(--card));stroke:var(--rt);stroke-width:3} .router .s1{font-size:10px;letter-spacing:.1em;font-weight:800;fill:var(--rt)!important}
.router:hover rect,.router:focus-visible rect,.router.sel rect{stroke-width:4.5} .bus{stroke:var(--rt);stroke-width:1.6;fill:none;stroke-dasharray:4 3;opacity:.8}
''')

def build():
    models = MODELS
    words = {cid: " ".join([commands[cid][1], TRIG[cid], ONE[cid], DEF[cid]]) for cid in commands}
    search = {cid: words[cid] for cid in commands}
    for group, title, subtitle, ids in STAGES:
        search["stage_" + group] = title + " " + subtitle + " " + " ".join(ids)
    js = "var MODELS=" + json.dumps(models) + ";var NAMES=" + json.dumps({k: name(k) for k in commands}) + ";SMPAGE={model:function(id){return MODELS[id]},label:function(id){return NAMES[id]||id}};window.SMDATA={search:" + json.dumps(search) + ",filt:{}};"
    order = [("1", "Setup", "init · document · extract", "g-ctx"), ("2", "Plan / build", "shape · craft", "g-plan"), ("3", "Diagnose", "critique + audit", "g-diag"), ("4", "Fix & style", "harden … bolder, animate", "g-fix"), ("5", "Finish", "polish, always last", "g-fin")]
    return dict(key="impeccable", title="Impeccable map", version="v" + VERSION,
        subtitle=f"{len(C) - 1} design commands plus the /impeccable router. Read left to right, then tap anything to see how it works.",
        svg=gen.svg, mapw=gen.MAPW, css=PAGECSS, js=js, order=order, filter=None, legend="",
        footer='Mapped from <a href="https://github.com/pbakaus/impeccable">pbakaus/impeccable</a> skill v' + VERSION + '. <code>teach</code> = init; <code>craft</code> is deprecated (a plain build request runs the same flow).')
