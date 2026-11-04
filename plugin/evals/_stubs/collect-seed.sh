#!/usr/bin/env bash
# Shared scaffold for the super-collect evals (evals/collect-*/seed.sh sources this).
# Builds a small "shop" app repo with a super-board config, the pack's collect scripts in
# .claude/, and fully offline sources:
#   gh            evals/_stubs/bin/gh, answering from .git/gh-fixture/
#   Sentry/PostHog evals/_stubs/py/sitecustomize.py, answering from .git/collect-fixture/
#                 (a rendered copy of tests/fixtures/collect/)
# Usage from a seed:  source "$(dirname "$(command -v gh)")/../collect-seed.sh"
#                     collect_seed '<collect block JSON>'
set -euo pipefail

STUBS="$(cd "$(dirname "$(command -v gh)")" && pwd)"
[ -f "$STUBS/_real" ] || { echo "seed: put evals/_stubs/bin first on PATH (see evals/README.md)" >&2; exit 1; }
PACK="$(cd "$STUBS/../../.." && pwd)"
FIXTURES="$PACK/tests/fixtures/collect"
[ -f "$FIXTURES/fixture_http.py" ] || { echo "seed: $FIXTURES missing" >&2; exit 1; }

# render <src> <dst>: copy one fixture with {{ago:…}} timestamps made relative to now.
render() {
  python3 - "$FIXTURES" "$1" "$2" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import fixture_http
with open(sys.argv[2]) as f:
    text = fixture_http.render(f.read())
with open(sys.argv[3], "w") as f:
    f.write(text)
PY
}

# git_at <days ago> <message>: commit everything staged with a backdated date.
git_at() {
  local when
  when=$(python3 -c "import datetime as d,sys;print((d.datetime.now(d.timezone.utc)-d.timedelta(days=int(sys.argv[1]))).strftime('%Y-%m-%dT%H:%M:%SZ'))" "$1")
  git add -A && GIT_AUTHOR_DATE="$when" GIT_COMMITTER_DATE="$when" git commit -qm "$2"
}

collect_seed() {
  local collect="$1" WS="${WS:-$PWD}"
  cd "$WS"

  # Same PATH bootstrap as review-remembers/seed.sh, plus the offline HTTP shim.
  local BIN="$HOME/.eval-bin" PY="$HOME/.eval-py"
  mkdir -p "$BIN" "$PY"
  cp -P "$STUBS"/* "$BIN"/
  cp "$STUBS/../py/sitecustomize.py" "$PY"/
  for rc in .zshenv .zshrc .bashrc .bash_profile .profile; do
    printf 'export PATH="%s:$PATH"\nexport PYTHONPATH="%s"\nexport SC_FIXTURES="%s"\n' \
      "$BIN" "$PY" "$WS/.git/collect-fixture" >> "$HOME/$rc"
  done
  echo "seed: HOME=$HOME WS=$WS" >&2

  git init -q -b main .
  git config user.email eval@example.com
  git config user.name eval

  # --- the app ---------------------------------------------------------------
  mkdir -p src/checkout src/profile src/receipts src/reports src/budgets src/settings
  cat > package.json <<'JSON'
{ "name": "shop", "private": true, "scripts": { "test": "vitest run" },
  "dependencies": { "posthog-js": "1.160.0", "@sentry/react": "8.30.0" } }
JSON
  cat > src/routes.ts <<'TS'
export const routes = {
  signup: "/signup",
  checkout: "/checkout",
  profile: "/profile",
  receiptUpload: "/receipts/upload",
  reports: "/reports/monthly",
  budgets: "/budgets",
  settings: "/settings",
};
TS
  cat > src/analytics.ts <<'TS'
import posthog from "posthog-js";

posthog.init(import.meta.env.VITE_POSTHOG_KEY, {
  api_host: "https://us.i.posthog.com",
  autocapture: true,
  capture_exceptions: true,
  capture_performance: { web_vitals: true },
});

export const capture = (event: string, props?: Record<string, unknown>) => posthog.capture(event, props);
TS
  cat > src/signup.ts <<'TS'
import { capture } from "./analytics";
import { api } from "./api";

export async function signUp(email: string, password: string) {
  capture("signup_started");
  try {
    const user = await api.post("/users", { email, password });
    capture("signup_completed", { plan: user.plan });
    return user;
  } catch (e) {
    capture("signup_failed", { error_code: (e as { code?: string }).code ?? "unknown" });
    throw e;
  }
}
TS
  cat > src/receipts/upload.ts <<'TS'
import { api } from "../api";

// /receipts/upload — photo or PDF of a receipt, OCR runs server-side.
export async function uploadReceipt(file: File) {
  if (file.size > 10 * 1024 * 1024) throw new Error("too_large");
  const form = new FormData();
  form.append("file", file);
  const res = await api.post("/receipts", form);
  return res.receipt;
}
TS
  cat > src/checkout/summary.ts <<'TS'
import type { Cart } from "./types";

export function buildSummary(cart: Cart | undefined) {
  // cart is undefined for a moment after the last item is removed (reload in flight)
  const lines = cart.items.map((i) => ({ name: i.name, total: i.price * i.qty }));
  return { lines, total: lines.reduce((s, l) => s + l.total, 0) };
}
TS
  cat > src/checkout/CheckoutPage.tsx <<'TS'
import { capture } from "../analytics";
import { buildSummary } from "./summary";
import { useCart } from "./useCart";

export function CheckoutPage() {
  const { cart, removeItem, pay } = useCart();
  const summary = buildSummary(cart);
  async function onPay() {
    capture("checkout_started", { items: summary.lines.length });
    try {
      await pay();
      capture("checkout_completed", { total_cents: Math.round(summary.total * 100) });
    } catch (e) {
      capture("checkout_failed", { error_code: (e as { code?: string }).code ?? "unknown" });
    }
  }
  return <OrderSummary summary={summary} onRemove={removeItem} onPay={onPay} />;
}
TS
  cat > src/profile/Avatar.tsx <<'TS'
export function Avatar({ user }: { user: { name: string; avatar: { url: string } | null } }) {
  return <img src={user.avatar.url} alt={user.name} />;
}
TS
  cat > src/reports/monthly.ts <<'TS'
export function byMonth(rows: { date: string; amount: number }[]) {
  const out: Record<number, number> = {};
  for (const row of rows) {
    const m = new Date(row.date).getMonth();
    out[m] = (out[m] ?? 0) + row.amount;
  }
  return out;
}
TS
  cat > src/reports/yearly.ts <<'TS'
// Year-over-year view; buckets each row by the month of its date-only `date` field.
export function monthOf(row: { date: string }) {
  return new Date(row.date).getMonth();
}
TS
  printf '.gh-log\n.env\nnode_modules/\n__pycache__/\n' > .gitignore
  git_at 30 "shop: checkout, profile, signup, receipts, reports"

  # --- super-board install (scripts only, so the plugin's SKILL.md is the one in play) ----
  mkdir -p .claude/skills/super-collect .claude/skills/super-board .claude/bin .claude/super-board/configs
  cp -R "$PACK/skills/super-collect/scripts" .claude/skills/super-collect/
  cp -R "$PACK/skills/super-board/references" .claude/skills/super-board/
  cp "$PACK"/scripts/*.sh "$PACK"/scripts/*.py .claude/bin/
  echo shop > .claude/super-board/active
  jq -n --argjson collect "$collect" '{
    version: 1, variant: "full",
    project: {owner: "example", number: 1, title: "Shop"},
    repo: {path: ".", remote: "https://github.com/example/shop.git"},
    paths: {runs_dir: "docs/super-board/runs"},
    collect: $collect}' > .claude/super-board/configs/shop.json
  # Fake credentials: the HTTP shim never sends them anywhere.
  printf 'SENTRY_AUTH_TOKEN=sntrys_eval_offline_token\nPOSTHOG_PERSONAL_API_KEY=phx_eval_offline_key\n' > .env
  git_at 20 "chore: install super-board"

  # --- offline sources -------------------------------------------------------
  mkdir -p .git/collect-fixture .git/gh-fixture
  local f
  for f in "$FIXTURES"/*.json; do render "$f" ".git/collect-fixture/$(basename "$f")"; done
  cp "$FIXTURES/fixture_http.py" .git/collect-fixture/
  : > .gh-log
}

# gh_fixture <name> <<'JSON' … : write one rendered gh fixture from stdin.
gh_fixture() {
  local tmp
  tmp=$(mktemp)
  cat > "$tmp"
  render "$tmp" ".git/gh-fixture/$1"
  rm -f "$tmp"
  jq empty ".git/gh-fixture/$1"
}
