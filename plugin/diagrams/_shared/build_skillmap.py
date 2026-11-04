#!/usr/bin/env python3
"""Build one skill-map page: python3 diagrams/_shared/build_skillmap.py <map>
Reads diagrams/<map>/map.py (function build() -> dict) and writes diagrams/<map>/index.html as one self-contained file.
See diagrams/_shared/README.md for the dict contract and how to add a map."""
import html, importlib.util, json, sys
from pathlib import Path
SH = Path(__file__).resolve().parent
name = sys.argv[1]
mdir = SH.parent / name
sys.path.insert(0, str(mdir))
if (mdir / "map.json").exists():  # data-only map: rendered by a shared layout
    sys.path.insert(0, str(SH))
    data = json.loads((mdir / "map.json").read_text(encoding="utf-8"))
    import layout_sdlc
    m = layout_sdlc.build(data, mdir)
    if m.get("out"): mdir = SH.parent / m["out"]; mdir.mkdir(exist_ok=True)
else:
    spec = importlib.util.spec_from_file_location(name + "_map", mdir / "map.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    m = mod.build()
rd = lambda f: (SH / f).read_text(encoding="utf-8")
e = html.escape
import base64
sys.path.insert(0, str(SH))
import sync_explore
REG = sync_explore.reg
_bad = sync_explore.problems()
if _bad:
    sys.exit("\n".join("EXPLORE CHECK: " + x for x in _bad))
def uri(rel):
    raw = (SH.parent / rel).read_bytes(); mime = "image/jpeg" if raw[:2] == b"\xff\xd8" else "image/png"
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode())
LOGOS = {r["id"]: r["logo"] for r in REG if r.get("logo")}
SKILL_ICON = '<svg class="ni" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="2.2"/><circle cx="18" cy="6" r="2.2"/><circle cx="12" cy="18" r="2.2"/><path d="M8 7l3 9M16 7l-3 9M8.2 6h7.6"/></svg>'
def navitem(r):
    ic = '<img class="ni nimg" alt="" src="%s">' % uri(r["logo"]) if r.get("logo") else SKILL_ICON
    lab = r["short"] + ('<span class="hide-s"> map</span>' if r["label"].endswith(" map") and r["label"] != r["short"] else "")
    cur = ' aria-current="page"' if r["id"] == m["key"] else ""
    return '<a class="lk" href="../%s"%s>%s<span class="nl">%s</span></a>' % (r["path"], cur, ic, lab)
NAV = rd("sitenav.html").replace("%%MAPNAV%%", "\n  ".join(navitem(r) for r in REG if r.get("nav")))
GENERIC = '<svg class="plogo" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l8 4-8 4-8-4z"/><path d="M4 12l8 4 8-4M4 16.5l8 4 8-4"/></svg>'
logo_rel = m.get("logo") or LOGOS.get(m["key"])
PLOGO = ('<img class="plogo" alt="" src="%s">' % uri(logo_rel)) if logo_rel else GENERIC
FAV = ('<link rel="icon" href="%s">' % uri(logo_rel)) if logo_rel else ""
flt = ""
if m.get("filter"):
    f = m["filter"]
    flt = f'<div class="seg" role="group" aria-label="{e(f["label"])}">' + "".join(
        f'<button type="button" data-f="{e(v)}" aria-pressed="{"true" if i == 0 else "false"}" aria-label="{e(l)}" title="{e(l)}">{icon}</button>' for i, (v, l, icon) in enumerate(f["options"])) + "</div>"
order = "".join(f'<div class="step {c}"><i>{e(n)}</i><span><b>{e(t)}</b><small>{e(s)}</small></span></div>' for n, t, s, c in m["order"])
# compare data: skills of this pack that sit in a comparison group
_cmp = json.loads(rd("compare.json"))["groups"]
PACKS = {"pstack": ("pstack", "assets/logos/poteto.png", "../pstack/"), "mattpocock": ("Matt Pocock", "assets/logos/mattpocock.png", "../mattpocock/"),
         "impeccable": ("Impeccable", "assets/logos/impeccable.png", "../impeccable/"), "superpowers": ("Superpowers", "assets/logos/obra.png", None)}
_by = {}
for g in _cmp:
    for mem in g["members"]:
        if mem["pack"] == m["key"]: _by.setdefault(mem["skill"], []).append(g)
CMPJS = "window.SMCOMPARE=" + json.dumps({"pack": m["key"], "groups": _by, "packs": {k: {"name": v[0], "logo": uri(v[1]), "path": v[2]} for k, v in PACKS.items()}}, ensure_ascii=False).replace("</", "<\\/") + ";\n" if _by else ""
t = rd("shell.html")
for k, v in {"%%TITLE%%": e(m["title"]), "%%PLOGO%%": PLOGO, "%%FAV%%": FAV, "%%VERSION%%": e(m["version"]), "%%SUBTITLE%%": m["subtitle"], "%%NAV%%": NAV, "%%FILTER%%": flt,
             "%%TOOLS%%": m.get("tools", ""), "%%SVG%%": m["svg"], "%%LEGEND%%": m.get("legend", ""), "%%ORDER%%": order, "%%FOOTER%%": m["footer"],
             "%%SHELLCSS%%": rd("shell.css") + "\n:root{--mapw:%dpx}" % m["mapw"], "%%SMCSS%%": rd("skillmap.css"), "%%PAGECSS%%": m["css"],
             "%%SMJS%%": rd("skillmap.js"), "%%PAGEJS%%": ("window.SMLOGO=" + json.dumps(uri(logo_rel)) + ";\n" if logo_rel else "") + CMPJS + m["js"], "%%SHELLJS%%": rd("shell.js"), "%%CANVASJS%%": rd("canvas.js")}.items():
    t = t.replace(k, v)
(mdir / "index.html").write_text(t, encoding="utf-8")
print(mdir / "index.html", len(t))
