import textwrap
"""Lay out the left-to-right map as an inline SVG (diagram.svg). No external tools needed."""
import html
from pathlib import Path
from data import *

e = html.escape
W, H = 136, 62
GAP_X, GAP_Y = 206, 86
HEAD_Y, CMD_Y, UTIL_Y, BUS_Y = 130, 218, 762, 56
commands = {c[0]: c for c in C}
gtype = {g[0]: g for g in GROUPS}
parts = []

def node(nid, label, sub, group, x, y, cls="n"):
    badge = ""
    if nid in CALLS:
        badge = f'<g class="badge"><rect x="{x+W-62}" y="{y-9}" width="66" height="17" rx="8.5"/><text x="{x+W-29}" y="{y+3}" text-anchor="middle">calls {len(CALLS[nid])}</text></g>'
    return (f'<g class="{cls} g-{group}" data-id="{nid}" data-grp="{group}" tabindex="0" role="button" aria-label="{e(label)}: {e(sub)}">'
            f'<rect x="{x}" y="{y}" width="{W}" height="{H}" rx="10"/>'
            f'<text class="t1" x="{x+W/2}" y="{y+23}" text-anchor="middle">{e(label)}</text>'
            + "".join(f'<text class="t2" x="{x+W/2}" y="{y+39+i*11}" text-anchor="middle">{e(l)}</text>' for i, l in enumerate(textwrap.wrap(sub, 21)[:2])) + badge + '</g>')

def stage_head(nid, title, group, x, y, n):
    return (f'<g class="stagehead g-{group}" data-id="{nid}" data-grp="{group}" data-head="1" tabindex="0" role="button" aria-label="{e(title)} stage (a group, not a command)">'
            f'<rect class="hit" x="{x}" y="{y}" width="{W}" height="{H}" fill="transparent"/>'
            f'<text class="s1" x="{x+4}" y="{y+12}">STAGE {n} · GROUP</text>'
            f'<text class="s2" x="{x+4}" y="{y+36}">{e(title)} <tspan class="s3">ⓘ</tspan></text>'
            f'<rect class="bar" x="{x+4}" y="{y+46}" width="{W-8}" height="3" rx="1.5"/></g>')

def region(label, group, x, y, w, h):
    return (f'<g class="r g-{group}" data-region="{group}"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14"/>'
            f'<text x="{x+12}" y="{y+19}">{e(label)}</text></g>')

for col, (group, title, subtitle, ids) in enumerate(STAGES):
    x = 40 + (col + 1) * GAP_X
    bottom = CMD_Y + (len(ids) - 1) * GAP_Y + H
    parts.append(region(gtype[group][1].replace("choose as needed", "as needed"), group, x - 16, HEAD_Y - 40, W + 32, bottom - HEAD_Y + 40 + 16))
    parts.append(stage_head("stage_" + group, title, group, x, HEAD_Y, col + 1))
    for row, cid in enumerate(ids):
        parts.append(node(cid, commands[cid][1], TRIG[cid], group, x, CMD_Y + row * GAP_Y))
    if True:
        ax = x - 16
        parts.append(f'<path class="arrow" data-edge="{"router" if col == 0 else STAGES[col-1][0]} {group}" d="M{ax-14} {HEAD_Y+H/2} H{ax+14}" marker-end="url(#ah)"/>')
rx = 40
parts.append(f'<g class="router" data-id="menu" data-grp="router" data-head="1" tabindex="0" role="button" aria-label="/impeccable router: reads your project and picks the right command">'
             f'<text class="s1" x="{rx+W/2}" y="{HEAD_Y-8}" text-anchor="middle">ROUTER</text>'
             f'<rect x="{rx}" y="{HEAD_Y}" width="{W}" height="{H+10}" rx="30"/>'
             f'<text class="t1" x="{rx+W/2}" y="{HEAD_Y+27}" text-anchor="middle">/impeccable</text>'
             f'<text class="t2" x="{rx+W/2}" y="{HEAD_Y+45}" text-anchor="middle">picks the right one</text></g>')
cx = [40 + (c + 1) * GAP_X + W / 2 for c in range(len(STAGES))]
parts.append(f'<path class="bus" data-trunk="router" d="M{rx+W/2} {HEAD_Y-26} V{BUS_Y} H{cx[-1]}"/>')
parts += [f'<path class="bus" data-edge="router {STAGES[i][0]}" d="M{x} {BUS_Y} V{HEAD_Y-44}" marker-end="url(#ah)"/>' for i, x in enumerate(cx)]
uw = (len(UTILITIES) - 1) * GAP_X + W + 32
parts.append(region("Utilities · use any time, in any order", "util", 24, UTIL_Y - 40, uw, H + 40 + 16))
for col, cid in enumerate(UTILITIES):
    parts.append(node(cid, commands[cid][1], TRIG[cid], "util", 40 + col * GAP_X, UTIL_Y))
VW, VH = 40 + 7 * GAP_X + W + 40, UTIL_Y + H + 32
svg = (f'<svg class="map" id="map" viewBox="0 0 {VW} {VH}" role="group" aria-label="impeccable command map">'
       '<defs><marker id="ah" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto"><path d="M0 0L7 4L0 8z" class="ahead"/></marker></defs>'
       + "".join(parts) + "</svg>")
MAPW = VW
