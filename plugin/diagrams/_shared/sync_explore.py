#!/usr/bin/env python3
"""Keep the landing page's Explore cards in step with _shared/maps.json.
  python3 diagrams/_shared/sync_explore.py          rewrite the cards
  python3 diagrams/_shared/sync_explore.py --check  fail if out of sync, or a map page has no registry entry / thumb"""
import json, sys
from pathlib import Path
D = Path(__file__).resolve().parent.parent
reg = json.loads((Path(__file__).resolve().parent / "maps.json").read_text(encoding="utf-8"))

def cards():
    return "\n".join(f'    <a class="pcard" href="{m["path"]}"><img src="{m["thumb"]}" alt="" loading="lazy"><div class="b"><h3>{m["label"]}</h3><p>{m["blurb"]}</p><span class="go">Open →</span></div></a>' for m in reg)

def render(text):
    a = text.index("<!-- explore:start"); a = text.index("\n", a) + 1; b = text.index("    <!-- explore:end -->")
    return text[:a] + cards() + "\n" + text[b:]

def problems():
    out = []
    ids = {m["dir"] for m in reg}
    for d in sorted(D.iterdir()):
        if d.is_dir() and not d.name.startswith("_") and ((d / "map.json").exists() or (d / "map.py").exists()):
            if d.name not in ids: out.append(f"map page diagrams/{d.name}/ is not in _shared/maps.json (needs a nav + Explore entry)")
    for m in reg:
        if not (D / m["thumb"]).exists(): out.append(f'{m["id"]}: thumb {m["thumb"]} is missing')
        if m.get("logo") and not (D / m["logo"]).exists(): out.append(f'{m["id"]}: logo {m["logo"]} is missing')
    idx = D / "index.html"; t = idx.read_text(encoding="utf-8")
    if render(t) != t: out.append("diagrams/index.html Explore cards are out of sync: run python3 diagrams/_shared/sync_explore.py")
    return out

if __name__ == "__main__":
    if "--check" in sys.argv:
        p = problems()
        for x in p: print("EXPLORE CHECK:", x)
        sys.exit(1 if p else 0)
    idx = D / "index.html"; idx.write_text(render(idx.read_text(encoding="utf-8")), encoding="utf-8"); print("explore cards synced:", len(reg))
