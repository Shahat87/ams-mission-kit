// Generates the README skill-map image (light + dark SVG, optional PNG) from skill-map.json.
// Run: node plugin/diagrams/skill-map-image/gen.ts [--png]   (Node 24 type-stripping, no deps)
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";

const HERE = dirname(fileURLToPath(import.meta.url));
const PLUGIN = resolve(HERE, "..", "..");
const ASSETS = join(PLUGIN, "docs", "assets");
const W = 1280;
const H = 760;

type Kind = "public" | "lane" | "board" | "hook" | "script";
interface JNode { id: string; label: string; kind: string; what?: string; sublabel?: string; author?: string }
interface JEdge { id: string; from: string; to: string; label: string }
interface Pt extends Array<number> { 0: number; 1: number }

const IDS = ["super-board", "super-collect", "ui-refine-loop", "visual", "super-build", "super-qa",
  "super-review", "gh-board", "sb-config", "base-branch", "hooks", "install-sh"];
// Root nodes deliberately left off the picture; a root edge to any other id fails the build.
const EXCLUDED = ["git-sync"];

const TAGLINE: Record<string, string> = {
  "super-build": "worktree · test-first · draft PR",
  "super-qa": "test plan · evidence · hand-off",
  "super-review": "own pass · truth-check · merge gate",
  "gh-board": "7 columns · the only loop state",
  "sb-config": "board · base branch · lanes",
  "base-branch": "merge commit = Done",
  "hooks": "guards · cleanup-wt",
  "install-sh": "skills · scripts · hooks",
};

const ICON: Record<string, string> = {
  "super-board": "term", "super-collect": "term", "ui-refine-loop": "term", "visual": "term",
  "super-build": "hammer", "super-qa": "flask", "super-review": "lens",
  "gh-board": "kanban", "sb-config": "sliders", "base-branch": "branch",
  "hooks": "shield", "install-sh": "download",
};

// 24x24 stroke-only glyphs
const GLYPH: Record<string, string> = {
  term: '<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M7 10l3 2.5-3 2.5M13 15.5h4"/>',
  hammer: '<path d="M14 4.5l5.5 5.5-3 3-5.5-5.5z"/><path d="M12.5 11.5L4.5 19.5"/><path d="M10 6l3-2.5"/>',
  flask: '<path d="M9.5 3.5h5M10.5 3.5v5.5l-5 8.5a2 2 0 0 0 1.7 3h9.6a2 2 0 0 0 1.7-3l-5-8.5V3.5"/><path d="M7.7 15h8.6"/>',
  lens: '<circle cx="10.5" cy="10.5" r="6"/><path d="M15 15l6 6"/>',
  kanban: '<rect x="3" y="4" width="5" height="11" rx="1"/><rect x="9.5" y="4" width="5" height="16" rx="1"/><rect x="16" y="4" width="5" height="7" rx="1"/>',
  sliders: '<path d="M4 7h9M19 7h1M4 17h1M11 17h9"/><circle cx="16" cy="7" r="2.2"/><circle cx="8" cy="17" r="2.2"/>',
  branch: '<circle cx="6" cy="5.5" r="2.2"/><circle cx="6" cy="18.5" r="2.2"/><circle cx="18" cy="8.5" r="2.2"/><path d="M6 7.7v8.6M18 10.7c0 5-6 4-12 7"/>',
  shield: '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M8.8 12l2.3 2.3 4.2-4.6"/>',
  download: '<path d="M12 4v11M7 11l5 5 5-5M5 20h14"/>',
};

const TAGS: Record<Kind, string> = { public: "COMMAND", lane: "LANE", board: "STATE", hook: "HOOK", script: "SCRIPT" };
const LEGEND: Record<Kind, string> = {
  public: "Command you type", lane: "Lane the board runs", board: "Board / repo state", hook: "Guard hook", script: "Script",
};

interface Palette {
  dark: boolean; text: string; muted: string; mono: string; panel: string; panelOp: number;
  rule: string; pillFill: string; pillStroke: string; edgeOp: number;
  acc: Record<Kind, string>; glow: Record<Kind, string>;
}
// Dark = repo electric-blue identity (tokens from docs/assets/board-flow.svg)
const DARK: Palette = {
  dark: true, text: "#e9f1ff", muted: "#8fb0e0", mono: "#9cc8ff", panel: "#071a3f", panelOp: 0.85,
  rule: "#1d3f7a", pillFill: "#071a3f", pillStroke: "#2f78ff", edgeOp: 1,
  acc: { public: "#3b86ff", lane: "#5ce1ff", board: "#7fb8ff", hook: "#ffb547", script: "#8fb0e0" },
  glow: { public: "#2f78ff", lane: "#24c8f5", board: "#3b86ff", hook: "#ff9a1f", script: "#5a7bb0" },
};
// Light = white page, same blues, darker for contrast, no glow
const LIGHT: Palette = {
  dark: false, text: "#071a3f", muted: "#4f6fa6", mono: "#3f5f96", panel: "#f5f9ff", panelOp: 1,
  rule: "#d3def5", pillFill: "#ffffff", pillStroke: "#b9cbee", edgeOp: 0.85,
  acc: { public: "#2f78ff", lane: "#0b9fcf", board: "#3b6fd8", hook: "#e08a00", script: "#5a7bb0" },
  glow: { public: "#2f78ff", lane: "#0b9fcf", board: "#3b6fd8", hook: "#e08a00", script: "#5a7bb0" },
};

const SANS = "-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif";
const MONO = "ui-monospace, Consolas, monospace";

// Hand-placed boxes: x, y, w (height fixed)
const NH = 92;
const POS: Record<string, { x: number; y: number; w: number }> = {
  "super-board": { x: 24, y: 150, w: 230 },
  "super-collect": { x: 24, y: 560, w: 230 },
  "gh-board": { x: 24, y: 330, w: 230 },
  "sb-config": { x: 320, y: 330, w: 230 },
  "super-build": { x: 400, y: 150, w: 180 },
  "super-qa": { x: 690, y: 150, w: 180 },
  "super-review": { x: 980, y: 150, w: 180 },
  "ui-refine-loop": { x: 400, y: 580, w: 220 },
  "visual": { x: 840, y: 580, w: 200 },
  "base-branch": { x: 770, y: 400, w: 220 },
  "hooks": { x: 1100, y: 330, w: 156 },
  "install-sh": { x: 1100, y: 490, w: 156 },
};

const cx = (id: string): number => POS[id].x + POS[id].w / 2;
const R = (id: string): Pt => [POS[id].x + POS[id].w, POS[id].y + NH / 2];
const L = (id: string): Pt => [POS[id].x, POS[id].y + NH / 2];

// Orthogonal routes, hand-planned so no edge crosses a node or another edge.
const ROUTE: Record<string, { pts: Pt[]; label: Pt }> = {
  "r-board-build": { pts: [R("super-board"), L("super-build")], label: [327, 196] },
  "r-board-qa": { pts: [[170, 150], [170, 122], [cx("super-qa"), 122], [cx("super-qa"), 150]], label: [500, 122] },
  "r-board-review": { pts: [[110, 150], [110, 104], [1070, 104], [1070, 150]], label: [900, 104] },
  "r-board-gh": { pts: [[100, 242], [100, 330]], label: [100, 286] },
  "r-board-config": { pts: [[220, 242], [220, 300], [440, 300], [440, 330]], label: [330, 300] },
  "r-collect-gh": { pts: [[100, 560], [100, 422]], label: [100, 491] },
  "r-collect-config": { pts: [R("super-collect"), [350, 606], [350, 422]], label: [350, 510] },
  "r-build-qa": { pts: [R("super-build"), L("super-qa")], label: [635, 196] },
  "r-qa-review": { pts: [R("super-qa"), L("super-review")], label: [925, 196] },
  "r-review-build": { pts: [[1000, 242], [1000, 275], [490, 275], [490, 242]], label: [745, 275] },
  "r-review-base": { pts: [[1030, 242], [1030, 446], [990, 446]], label: [1030, 350] },
  "r-review-hooks": { pts: [[1140, 242], [1140, 330]], label: [1140, 286] },
  "r-refine-base": { pts: [R("ui-refine-loop"), [700, 626], [700, 446], [770, 446]], label: [700, 540] },
  "r-visual-base": { pts: [[940, 580], [940, 492]], label: [940, 536] },
  "r-refine-config": { pts: [[500, 580], [500, 422]], label: [500, 500] },
  "r-install-hooks": { pts: [[1178, 490], [1178, 422]], label: [1178, 456] },
};

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
const f = (n: number): string => String(Math.round(n * 10) / 10);

function roundedPath(pts: Pt[], r = 10): string {
  let d = `M${f(pts[0][0])} ${f(pts[0][1])}`;
  for (let i = 1; i < pts.length - 1; i++) {
    const [px, py] = pts[i - 1];
    const [x, y] = pts[i];
    const [nx, ny] = pts[i + 1];
    const l1 = Math.hypot(x - px, y - py);
    const l2 = Math.hypot(nx - x, ny - y);
    const r1 = Math.min(r, l1 / 2);
    const r2 = Math.min(r, l2 / 2);
    const ax = x - ((x - px) / l1) * r1;
    const ay = y - ((y - py) / l1) * r1;
    const bx = x + ((nx - x) / l2) * r2;
    const by = y + ((ny - y) / l2) * r2;
    d += ` L${f(ax)} ${f(ay)} Q${f(x)} ${f(y)} ${f(bx)} ${f(by)}`;
  }
  const last = pts[pts.length - 1];
  return d + ` L${f(last[0])} ${f(last[1])}`;
}

function wrap(text: string, maxChars: number): string[] {
  const parts = text.split(" · ");
  const lines: string[] = [];
  let cur = "";
  for (const p of parts) {
    const next = cur ? `${cur} · ${p}` : p;
    if (cur && next.length > maxChars) { lines.push(cur); cur = p; } else cur = next;
  }
  if (cur) lines.push(cur);
  return lines;
}

function die(msg: string): never {
  console.error("gen.ts: " + msg);
  process.exit(1);
}

// ---- data ----
const data = JSON.parse(readFileSync(join(PLUGIN, "diagrams", "skill-map.json"), "utf8"));
const version = readFileSync(join(PLUGIN, "VERSION"), "utf8").trim();
const nodes = new Map<string, JNode>();
for (const n of data.nodes as JNode[]) if (IDS.includes(n.id)) nodes.set(n.id, n);
if (nodes.size !== IDS.length) die(`expected ${IDS.length} nodes, found ${nodes.size}`);
const edges: JEdge[] = (data.edges as JEdge[]).filter((e) => e.id.startsWith("r-") && nodes.has(e.from) && nodes.has(e.to));
for (const e of data.edges as JEdge[]) {
  if (!e.id.startsWith("r-") || edges.includes(e)) continue;
  if (![e.from, e.to].some((x) => EXCLUDED.includes(x))) die(`r- edge ${e.id} (${e.from} -> ${e.to}) is not drawn; add its nodes or exclude them`);
}
for (const e of edges) if (!ROUTE[e.id]) die(`no route for ${e.id}`);

const kindOf = (id: string): Kind => {
  const k = nodes.get(id)!.kind;
  if (!(k in TAGS)) die(`unknown kind ${k}`);
  return k as Kind;
};

// ---- avatars (cached, embedded once as data URIs) ----
const AV_DIR = join(HERE, "avatars");
const refresh = process.argv.includes("--refresh-avatars");
function sniff(buf: Buffer): string | null {
  if (buf.length > 8 && buf[0] === 0x89 && buf[1] === 0x50 && buf[2] === 0x4e && buf[3] === 0x47) return "image/png";
  if (buf.length > 3 && buf[0] === 0xff && buf[1] === 0xd8 && buf[2] === 0xff) return "image/jpeg";
  return null;
}
const authors = [...new Set([...nodes.values()].map((n) => n.author).filter((a): a is string => !!a))].sort();
const avatars = new Map<string, string>();
mkdirSync(AV_DIR, { recursive: true });
for (const a of authors) {
  if (!/^[A-Za-z0-9-]+$/.test(a)) die(`bad author name ${a}`);
  const file = join(AV_DIR, a + ".png");
  let buf: Buffer | null = null;
  if (!refresh && existsSync(file)) buf = readFileSync(file);
  if (!buf) {
    try {
      const res = await fetch(`https://github.com/${a}.png?size=96`, { redirect: "follow" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      buf = Buffer.from(await res.arrayBuffer());
      if (!sniff(buf)) throw new Error("response is not a PNG/JPEG");
      writeFileSync(file, buf);
    } catch (err) {
      if (existsSync(file)) buf = readFileSync(file);
      else die(`could not fetch avatar for ${a} and no cache at ${file}: ${(err as Error).message}`);
    }
  }
  const mime = sniff(buf!) ?? die(`cached avatar for ${a} is not a PNG/JPEG: ${file}`);
  avatars.set(a, `data:${mime};base64,${buf!.toString("base64")}`);
}

function render(p: Palette): string {
  const o: string[] = [];
  const dark = p.dark;
  const flow = edges.map((e) => `${nodes.get(e.from)!.label} ${e.label} ${nodes.get(e.to)!.label}`).join("; ");
  o.push(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-labelledby="t d">`);
  o.push(`<title id="t">super-board skill map</title>`);
  o.push(`<desc id="d">${esc("What each skill touches. Flow: " + flow + ".")}</desc>`);
  o.push(`<defs>`);
  if (dark) {
    o.push(`<radialGradient id="bg" cx="640" cy="300" r="960" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#0c2656"/><stop offset=".32" stop-color="#071636"/><stop offset=".7" stop-color="#030a1c"/><stop offset="1" stop-color="#01040b"/></radialGradient>`);
    o.push(`<filter id="glowB" filterUnits="userSpaceOnUse" x="-200" y="-200" width="1700" height="1200"><feGaussianBlur stdDeviation="6"/></filter>`);
    o.push(`<filter id="glowA" filterUnits="userSpaceOnUse" x="-20" y="-20" width="60" height="60"><feGaussianBlur stdDeviation="1.6"/></filter>`);
  }
  for (const k of Object.keys(TAGS) as Kind[]) {
    const c = p.acc[k];
    const halo = dark ? `<path d="M1 1 L9 5 L1 9 Z" fill="${c}" opacity=".7" filter="url(#glowA)" stroke="${c}" stroke-width="1.5"/>` : "";
    o.push(`<marker id="ar-${k}" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">${halo}<path d="M1 1 L9 5 L1 9 Z" fill="${c}"/></marker>`);
  }
  for (const [a, uri] of avatars) o.push(`<image id="av-${a}" width="96" height="96" href="${uri}"/>`);
  for (const id of IDS) {
    const b = POS[id];
    o.push(`<clipPath id="cp-${id}"><rect x="${b.x}" y="${b.y}" width="${b.w}" height="${NH}" rx="10"/></clipPath>`);
    if (nodes.get(id)!.author) o.push(`<clipPath id="av-cp-${id}"><circle cx="${b.x + b.w - 26}" cy="${b.y + 26}" r="14"/></clipPath>`);
  }
  o.push(`</defs>`);
  o.push(dark ? `<rect width="${W}" height="${H}" rx="24" fill="url(#bg)"/>` : `<rect width="${W}" height="${H}" fill="#ffffff"/>`);

  // title block
  o.push(`<text x="24" y="50" font-family="${SANS}" font-size="30" font-weight="700" fill="${p.text}">super-board skill map</text>`);
  o.push(`<text x="24" y="76" font-family="${SANS}" font-size="14" fill="${p.muted}">What each skill touches. Click for the interactive map.</text>`);

  // edges
  for (const e of edges) {
    const k = kindOf(e.from);
    o.push(`<path d="${roundedPath(ROUTE[e.id].pts)}" fill="none" stroke="${p.acc[k]}" stroke-opacity="${p.edgeOp}" stroke-width="1.5" stroke-linecap="round" marker-end="url(#ar-${k})"/>`);
  }

  // nodes
  for (const id of IDS) {
    const n = nodes.get(id)!;
    const k = kindOf(id);
    const b = POS[id];
    const a = p.acc[k];
    o.push(`<g>`);
    if (dark) o.push(`<rect x="${b.x}" y="${b.y}" width="${b.w}" height="${NH}" rx="10" fill="none" stroke="${p.glow[k]}" stroke-width="8" opacity=".55" filter="url(#glowB)"/>`);
    o.push(`<rect x="${b.x}" y="${b.y}" width="${b.w}" height="${NH}" rx="10" fill="${p.panel}" fill-opacity="${p.panelOp}" stroke="${a}" stroke-width="1.5"/>`);
    o.push(`<rect x="${b.x}" y="${b.y}" width="4" height="${NH}" fill="${a}" clip-path="url(#cp-${id})"/>`);
    o.push(`<g transform="translate(${b.x + 16} ${b.y + 12}) scale(0.8)" fill="none" stroke="${a}" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">${GLYPH[ICON[id]]}</g>`);
    o.push(`<text x="${b.x + 40}" y="${b.y + 26}" font-family="${SANS}" font-size="10" font-weight="700" letter-spacing="1.3" fill="${p.muted}">${TAGS[k]}</text>`);
    o.push(`<text x="${b.x + 16}" y="${b.y + 52}" font-family="${SANS}" font-size="16" font-weight="700" fill="${p.text}">${esc(n.label)}</text>`);
    const sub = n.sublabel ?? TAGLINE[id] ?? die(`no tagline for ${id}`);
    const lines = wrap(sub, Math.floor((b.w - 30) / 6.4));
    lines.forEach((ln, i) => {
      o.push(`<text x="${b.x + 16}" y="${b.y + 71 + i * 13}" font-family="${MONO}" font-size="10.5" fill="${p.mono}">${esc(ln)}</text>`);
    });
    if (n.author) {
      const ax = b.x + b.w - 26;
      const ay = b.y + 26;
      o.push(`<g clip-path="url(#av-cp-${id})"><use href="#av-${n.author}" transform="translate(${ax - 14} ${ay - 14}) scale(${f(28 / 96)})"/></g>`);
      o.push(`<circle cx="${ax}" cy="${ay}" r="14.5" fill="none" stroke="${a}" stroke-width="1.5"/>`);
    }
    o.push(`</g>`);
  }

  // edge labels on solid pills
  for (const e of edges) {
    const [lx, ly] = ROUTE[e.id].label;
    const w = Math.round(e.label.length * 6.4 + 16);
    o.push(`<rect x="${f(lx - w / 2)}" y="${ly - 10}" width="${w}" height="20" rx="10" fill="${p.pillFill}" stroke="${p.pillStroke}" stroke-width="1"/>`);
    o.push(`<text x="${lx}" y="${ly + 4}" text-anchor="middle" font-family="${SANS}" font-size="12" font-weight="600" fill="${p.dark ? "#cfe0ff" : p.muted}">${esc(e.label)}</text>`);
  }

  // legend + footer
  o.push(`<line x1="24" y1="706" x2="${W - 24}" y2="706" stroke="${p.rule}" stroke-width="1"/>`);
  let lx = 24;
  for (const k of Object.keys(TAGS) as Kind[]) {
    o.push(`<rect x="${lx}" y="721" width="14" height="14" rx="3" fill="${p.panel}" fill-opacity="${p.panelOp}" stroke="${p.acc[k]}" stroke-width="1.5"/>`);
    o.push(`<rect x="${lx}" y="721" width="3.5" height="14" rx="1.5" fill="${p.acc[k]}"/>`);
    o.push(`<text x="${lx + 22}" y="733" font-family="${SANS}" font-size="12.5" fill="${p.muted}">${esc(LEGEND[k])}</text>`);
    lx += 22 + LEGEND[k].length * 6.4 + 26;
  }
  o.push(`<text x="${W - 24}" y="733" text-anchor="end" font-family="${MONO}" font-size="11" fill="${p.muted}">super-board ${esc(version)} · generated from skill-map.json</text>`);
  o.push(`</svg>`);
  return o.join("\n") + "\n";
}

mkdirSync(ASSETS, { recursive: true });
const outs = {
  light: join(ASSETS, "skill-map.svg"),
  dark: join(ASSETS, "skill-map-dark.svg"),
  png: join(ASSETS, "skill-map.png"),
};
writeFileSync(outs.light, render(LIGHT));
writeFileSync(outs.dark, render(DARK));
console.log(outs.light);
console.log(outs.dark);

// Default: the full canvas at 2x. With a frame: fit the canvas to that width at 1x and centre it vertically.
function shot(svgPath: string, bg: string, pngPath: string, scratch: string, frame?: [number, number]): void {
  const [fw, fh] = frame ?? [W, H];
  const ih = Math.round((fw * H) / W);
  const html = join(scratch, "wrap-" + (svgPath.includes("dark") ? "dark" : "light") + (frame ? "-thumb" : "") + ".html");
  writeFileSync(html, `<!doctype html><html><body style="margin:0;background:${bg}"><img src="${pathToFileURL(svgPath).href}" width="${fw}" height="${ih}" style="display:block;margin-top:${Math.floor((fh - ih) / 2)}px"></body></html>`);
  const chrome = process.env.CHROME ?? "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
  const r = spawnSync(chrome, ["--headless=new", "--disable-gpu", "--hide-scrollbars", `--force-device-scale-factor=${frame ? 1 : 2}`,
    `--window-size=${fw},${fh}`, `--screenshot=${pngPath}`, pathToFileURL(html).href], { stdio: "inherit" });
  if (r.status !== 0 || !existsSync(pngPath)) die(`chrome failed for ${pngPath}`);
}

if (process.argv.includes("--png")) {
  const scratch = process.env.SCRATCH ?? join(tmpdir(), "skill-map-image");
  mkdirSync(scratch, { recursive: true });
  shot(outs.light, "#ffffff", outs.png, scratch);
  // Overview page card + og:image (diagrams/_shared/maps.json thumb, 960x600)
  const thumb = join(PLUGIN, "diagrams", "assets", "skill-map.png");
  shot(outs.light, "#ffffff", thumb, scratch, [960, 600]);
  console.log(thumb);
  const darkPng = join(scratch, "skill-map-dark-preview.png");
  shot(outs.dark, "#01040b", darkPng, scratch);
  console.log(outs.png);
  console.log(darkPng);
}
