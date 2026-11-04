#!/usr/bin/env bash
# One headless lane. --codex is the run owner's explicit opt-in; without it,
# usage fallback may build/test but must never own a review or merge.
# Without --codex the board config must say "usage_fallback": "<tool>" (exit 78 otherwise).
# --tool codex|opencode|pi picks the delegate CLI (default codex). OpenCode and Pi are
# build/qa fallback lanes only: a delegate never owns Review or a merge (review exits 78).
# Exit 77 = tool unavailable. Codex: only a missing/unlaunchable binary. OpenCode/Pi: also a startup failure
# (credit/quota/auth error, or, as a usage_fallback delegate only, exit 0 with no output at all) before the
# tool produced any work. Anything later is failed (1). The wave releases the card for a Claude lane.
# OpenCode/Pi have no filesystem sandbox: they run only with "usage_fallback_unsandboxed": true.
# Usage: --config f --card '<JSON>' --lane build|qa|review [--tool codex|opencode|pi] [--codex[=model]]
#        [--tier low|medium|high] [--complexity low|medium|high]
# Model: --codex=<model>, else codex.model, else CODEX_LADDERS[tier][complexity]
# when --tier is given (an ungraded card takes the medium cell), else Codex's default.
set -euo pipefail
exec python3 - "${BASH_SOURCE[0]}" "$@" <<'PY'
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shlex
import re
import shutil
import threading
import time
import collections
import signal
import subprocess
import sys
import tempfile

def fail(message, code=64):
    print(message, file=sys.stderr)
    sys.exit(code)

UNAVAILABLE = 77  # the one "tool unavailable, fall back to a Claude lane" exit code
# Per tool: label, binary, default model, extra env vars the tool needs, and the ANCHORED error line (start of
# line, the tool's real error prefix) that means credit or auth is gone. Consulted for OpenCode and Pi only,
# against their own stderr (prompt echo excluded) and, for OpenCode, a stdout {"type":"error"} event, and only
# while the tool has done no work yet. Codex has none: `codex exec` prints its whole session, tool output
# included, to stderr, so for Codex 77 means exactly "binary missing or failed to launch"; every other Codex
# failure, usage limit and auth included, is failed (1) as it always was.
TOOLS = {
    'codex': dict(label='Codex', binary='codex', model=None, env=(), unavailable=None),
    'opencode': dict(label='OpenCode', binary='opencode', model='opencode-go/kimi-k2.7-code', env=(), unavailable=(
        r"(?:ERROR|Error):\s.*(?:insufficient[_ ](?:balance|credits?|quota)|credit balance|usage limit|"
        r"not authenticated|authentication failed|invalid api key|unauthori[sz]ed)")),
    'pi': dict(label='Pi', binary='pi', model='runinfra/glm-5-3-flash', env=('RUNINFRA_GATEWAY_KEY',), unavailable=(
        r"(?:No API key found for \S+|(?:ERROR|Error):\s.*(?:insufficient[_ ](?:balance|credits?|quota)|usage limit|"
        r"invalid api key|unauthori[sz]ed))")),
}

parser = argparse.ArgumentParser(description='Run one delegate board lane (no Claude sub-agents).')
parser.add_argument('--tool', choices=list(TOOLS), default='codex', help='delegate CLI (default codex)')
parser.add_argument('--config', required=True)
parser.add_argument('--card', required=True, help='card JSON or a JSON file')
parser.add_argument('--lane', required=True, choices=['build', 'qa', 'review'])
parser.add_argument('--tier', choices=['low', 'medium', 'high'], help='run ladder (--low / default / --high)')
parser.add_argument('--complexity', choices=['low', 'medium', 'high'], help="router's grade for this card")
raw = sys.argv[2:]
explicit = False
model = None
rest = []
for arg in raw:
    if arg == '--codex':
        explicit = True
    elif arg.startswith('--codex='):
        explicit = True
        model = arg.partition('=')[2]
        if not model.strip():
            fail('--codex=<model> requires a non-empty model')
    else:
        rest.append(arg)
args = parser.parse_args(rest)
script_path = Path(sys.argv[1]).resolve()
tool = args.tool
spec_ = TOOLS[tool]
label = spec_['label']
if tool != 'codex' and (explicit or model is not None):
    fail(f'--codex applies only to --tool codex, not {tool}')
if args.lane == 'review' and tool != 'codex':
    fail(f'{label} review refused: a delegate never owns Review or a merge.', 78)
if args.lane == 'review' and not explicit:
    fail('Codex review refused: run must be started with explicit --codex; usage fallback cannot review.', 78)
try:
    config_path = Path(args.config).resolve()
    config = json.loads(config_path.read_text())
    card = json.loads(args.card if args.card.lstrip().startswith('{') else Path(args.card).read_text())
    if not isinstance(config, dict) or not isinstance(card, dict):
        raise ValueError('config/card must be objects')
    if type(card.get('number')) is not int or card['number'] < 1 or not isinstance(card.get('status'), str):
        raise ValueError('card needs a positive issue number and status')
    configured = config.get(tool, {}).get('model') if config.get(tool) else None
    if configured is not None and not isinstance(configured, str):
        raise ValueError(f'{tool}.model must be a string')
    model = model if model is not None else (configured if configured and configured.strip() else None)
    # Codex run ladders, the one copy: run tier x card complexity -> model.
    # Mirrors the Claude LADDERS in workflows/super-board-wave.js. gpt-6.1-sol
    # ties gpt-6-astra on DeepSWE v1.1 (75.2% vs 74.1-74.8%) at a fifth of the
    # price, so it carries the default; Astra takes hard cards and --high.
    CODEX_LADDERS = {
        'low': {'low': 'gpt-6-luna', 'medium': 'gpt-6.1-sol', 'high': 'gpt-6.1-sol'},
        'medium': {'low': 'gpt-6.1-sol', 'medium': 'gpt-6.1-sol', 'high': 'gpt-6-astra'},
        'high': {'low': 'gpt-6-astra', 'medium': 'gpt-6-astra', 'high': 'gpt-6-astra'},
    }
    if model is None and args.tier and tool == 'codex':
        model = CODEX_LADDERS[args.tier][args.complexity or 'medium']
    if model is None:
        model = spec_['model']  # opencode and pi have no safe CLI default; codex keeps its own
except (OSError, ValueError, AttributeError) as error:
    fail(f'Invalid {label} lane input: {error}')
# npm `.cmd` shims run through cmd.exe, so shell metacharacters in the argv matter: no odd model names.
if tool != 'codex' and not re.fullmatch(r'[A-Za-z0-9._/:-]+', model):
    fail(f'Invalid {label} lane input: model {model!r} must match [A-Za-z0-9._/:-]+')
# Without --codex this is the usage fallback, and that is opt-in per board.
if not explicit and config.get('usage_fallback', 'none') != tool:
    fail(f'{label} fallback refused: usage_fallback is not "{tool}" in {config_path}' +
         ('; pass --codex for an explicit Codex run.' if tool == 'codex' else '.'), 78)

if tool != 'codex' and config.get('usage_fallback_unsandboxed') is not True:
    fail(f'{label} refused: it has no filesystem sandbox; set "usage_fallback_unsandboxed": true in {config_path} to opt in.', 78)

column = card['status']
def result(status, detail):
    print(json.dumps({'status': status, 'column': column, 'detail': detail}))
def unavailable(reason):
    result('unavailable', f'{label} unavailable: {reason}')
    print(f'{label} unavailable: {reason}', file=sys.stderr)
    sys.exit(UNAVAILABLE)
resolved = shutil.which(spec_['binary'])  # resolved once; the launch below reuses it
if resolved is None:
    unavailable(f'{spec_["binary"]} binary not found on PATH')

readiness = script_path.with_name('super-board-verification-ready.py')
def require_verification_ready():
    if subprocess.run([sys.executable, str(readiness), '--config', str(config_path)]).returncode:
        result('halted', 'verification-readiness: repair verify_commands; preserve claims, worktrees and card state, then restart explicitly')
        sys.exit(9)
require_verification_ready()

def git_path(option):
    return Path(subprocess.check_output(['git', 'rev-parse', option], text=True).strip()).resolve()

try:
    root = git_path('--show-toplevel')
    git_dir = git_path('--git-dir')
    common_dir = git_path('--git-common-dir')
except subprocess.CalledProcessError:
    fail(f'{label} lane must start inside the target git repository')
os.chdir(root)
helper = script_path.with_name('super-board-github-read.py')
if not helper.exists() or subprocess.run([sys.executable, str(helper), '--check']).returncode:
    result('halted', 'required GitHub reads halted or helper missing; preserve claims and worktrees')
    sys.exit(79)

resolver_path = script_path.with_name('super-board-skills-ready.py')
try:
    spec = importlib.util.spec_from_file_location('skills_ready', resolver_path)
    resolver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(resolver)
    resolver.hydrate([card], root)
    skills = resolver.report(root, 'codex', [card], lane=args.lane,
                             home=os.environ.get('SUPER_BOARD_SKILLS_HOME'), catalog=os.environ.get('SUPER_BOARD_SKILLS_CATALOG'))
except SystemExit as error:
    result('halted', 'required GitHub reads halted while hydrating skills; preserve claims and worktrees')
    sys.exit(error.code)
except (OSError, ValueError, TypeError, KeyError) as error:
    result('halted', 'skills-readiness: ' + str(error))
    sys.exit(64)
if not skills['ready']:
    result('halted', 'skills-readiness: missing ' + ', '.join(dict.fromkeys(e['name'] for e in skills['entries'] if e['status'] == 'missing')))
    sys.exit(10)

skill = {'build': 'super-build', 'qa': 'super-qa', 'review': 'super-review'}[args.lane]
section = {'build': 'Builder', 'qa': 'Tester', 'review': 'Reviewer'}[args.lane]
prompt = f'''Run {skill} for issue #{card['number']} as a {label} lane worker.
First operation: recheck .claude/bin/super-board-skills-ready.py --root {root} --host codex --cards - --lane {args.lane} with the Card JSON below as a one-element stdin array, before worktree creation, classification or lifecycle writes. Exit 10 means status=halted, detail=skills-readiness:, preserve all state. Load resolved_path files and only declared fallback IDs from the report. Before activating any conditional helper, repeat with --require <exact-name>.
Resolved skill report: {json.dumps(skills)}
Config: {config_path}
Card: {json.dumps(card)}
Read .claude/skills/{skill}/SKILL.md and .claude/skills/super-board/references/run.md → {section} lifecycle; follow all its board, branch, worktree, comments, test and merge requirements.
Source .claude/bin/super-board-gh-guard.sh and use its quota checks. Required GitHub reads go through .claude/bin/super-board-github-read.py; a read that misses its --kind shape exits 64 for that call only and prints the valid form, so fix the read. Check --check before new writes, migrations and merges; exit 79 means status=halted, no more GitHub calls, preserve claims/worktree/card/approval. Never retry a mutation.
Use .claude/bin/super-board-card.sh --config {config_path} for board reads/moves (a move into Done needs that config: it must have base_branch); every move passes --lane {args.lane} --model {label} --host codex (a move into Done also passes --pr with the merged PR number, e.g. --pr 50: card.sh verifies the merge and closes the issue; moves into Building, QA, Review, Blocked or Done refuse without --lane/--model; add --kind bug|feature|qa on the first move of a card that has no type label: bug, feature, qa, docs, ux, tests, refactor, tech-debt or money). Use .claude/bin/super-board-pr-body.sh for owned PR blocks; follow writing-standard.md and pr-author-notes.md.
First check taint (run.md → Tainted cards); on a tainted card never check out the issue branch. Use your own worktree under .claude/worktrees/; never edit the main checkout. Clean up on ordinary exit, preserve work on halt/interruption.
{label} adaptation: do not launch Claude or other sub-agents. Perform preflight, classification, independent review and truth-check duties inline. This changes the execution host, never the evidence requirements, confidence threshold or merge policy.
Return JSON: status=advanced|bounced|blocked|human-gate|failed|halted; column=current board column; detail=one line; prUrl/branch when known, otherwise null; priorFindings=review counts or null. A non-advanced exit ends this card's lane chain. On merge-gate exit 9, return status=halted with detail starting exactly "verification-readiness:"; preserve work and never bounce/retry. On merge-gate exit 11 (acting gh login != notifications.bot_identity) also return status=halted, detail starting "identity-mismatch:"; preserve card, approval, claims and worktree, and do not retry.
'''
if args.lane == 'build':
    prompt += '''Before creating any worktree or changing code, perform run.md's Builder pre-flight inline: call super-board-preflight.sh --config ''' + shlex.quote(str(config_path)) + ''', judge its candidates semantically, and act on proceed/hold/sequence/skipped. Blind preflight leaves Ready and returns failed. Your own issue branch PR is not a duplicate. Read the issue and all comments; preserve qa/bug/feature labels. Existing docs/ux/tests/refactor/tech-debt rows keep their label chain: classify complexity without adding feature/bug. If truly untyped, classify feature or bug, re-run skills-ready on the proposed labelled card before adding that label, never qa. Then follow the Builder lifecycle.
'''
elif args.lane == 'qa' and card['status'] == 'Ready':
    prompt += 'This qa-labelled Ready card skips Building: move Ready → QA and create the issue branch from `origin/<base>` after `git -C <main repo> fetch --quiet origin <base>` (never from the local base); test the existing product per run.md qa cards.\n'
elif args.lane == 'review':
    prompt += '''The owner explicitly chose run --codex: you may review and own the merge. First load the latest <!-- super-review:report -->; grade prior R-ids fixed/not fixed/no longer applies, bounce any not fixed and edit the same report in place. Form hypotheses from the issue AC and diff before reading the Builder narrative.
Rerun the Tester's tests (tainted: exec --sandbox --ref). When truth_gate triggers, apply super-review's fixed Adversarial mode brief inline in two distinct passes (code-grounder and historian); collect evidence and aggregate confidence against truth_threshold. Over-engineering is Should fix, never Blocker alone and never lowers confidence. Missing evidence fails closed.
Merge ONLY through the unchanged .claude/bin/super-board-merge-gate.sh, pinning the exact head you reviewed at review pass using --expect-head "$HEAD" --config <config> --pr <number> --subject <title> --body-file <msg.md>. Never run bare gh pr merge or bypass the gate. Follow run.md Merge protocol and route every gate exit exactly: 2/5 rebase pass (on a tainted card, gate exits 2/5 follow run.md → Tainted cards rule 4), 3 nothing merged (PR closed or base != base_branch: card back to Review with a [reviewer] comment naming the PR state/base, Blocked with a question if the PR stays closed), 4 stay Review, 6 fresh review, 7/8 human gate with pinned approval-request; 11 identity/config halt (acting gh login != notifications.bot_identity, or bot_identity is also a trusted approver) means halt and preserve card/worktree/approval/claims, no retry; 9 verification-readiness means halt and preserve card/worktree/approval/claims until config repair and explicit restart, never a Build bounce or rebase retry. Respect human_approves_merge and merge_policy. Confirm MERGED <sha>, fetch base and prove merge-base --is-ancestor <sha> origin/<base> before closing issue or moving Done.
'''

counts = {key: {'type': 'integer'} for key in ['fixed', 'notFixed', 'noLongerApplies']}
properties = {
    'status': {'type': 'string', 'enum': ['advanced', 'bounced', 'blocked', 'human-gate', 'failed', 'halted']},
    'column': {'type': 'string'}, 'detail': {'type': 'string'},
    'prUrl': {'type': ['string', 'null']}, 'branch': {'type': ['string', 'null']},
    'priorFindings': {'anyOf': [{'type': 'null'}, {'type': 'object', 'properties': counts,
                      'required': list(counts), 'additionalProperties': False}]},
}
schema = {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}
child = None
tree_unconfirmed = False  # Windows: taskkill failed, so the launcher's descendants may still run
def stop_child(force):
    global tree_unconfirmed
    # On Windows the direct child is often only a launcher (node for codex.js, cmd.exe for a .cmd shim) and the real
    # worker is a grandchild that terminate()/kill() would leave running, so kill the whole tree. SUPER_BOARD_TEST_WINDOWS
    # lets the tests simulate that branch on POSIX.
    if os.name == 'nt' or os.environ.get('SUPER_BOARD_TEST_WINDOWS') == '1':
        try:
            if subprocess.run(['taskkill', '/T', '/F', '/PID', str(child.pid)], capture_output=True).returncode == 0:
                tree_unconfirmed = False
                return
        except OSError:
            pass  # taskkill missing or failed (Cygwin/MSYS Python, access denied): fall through to the direct kill
        tree_unconfirmed = True  # the direct kill below reaches the launcher only, never its descendants
    child.kill() if force else child.terminate()
def worker_alive(wait):
    # child.poll()/wait() cannot be used here on POSIX: the interrupted main thread is inside child.wait() holding
    # Popen's waitpid lock, so poll() would report None for a worker that is long dead. Ask the OS directly.
    deadline = time.monotonic() + wait
    while True:
        if os.name == 'nt':
            alive = child.poll() is None
        else:
            try:
                alive = os.waitpid(child.pid, os.WNOHANG)[0] == 0
            except ChildProcessError:
                alive = False
        if not alive or time.monotonic() >= deadline:
            return alive
        time.sleep(0.05)
def interrupted(signum, frame):
    alive = False
    if child is not None:
        stop_child(False)
        alive = worker_alive(2)
        if alive:
            stop_child(True)
            alive = worker_alive(3)
    detail = 'lane interrupted; preserve claims and worktrees'
    if child is not None and (alive or tree_unconfirmed):
        detail = f'lane interrupted; worker pid {child.pid} may still be alive; preserve claims and worktrees'
    result('halted', detail)
    sys.exit(128 + signum)
signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGINT, interrupted)
def strict_lane_result(text):
    """The WHOLE message, trimmed and optionally inside one json fence, as a single JSON object with a status; else None."""
    text = (text or '').strip()
    fenced = re.fullmatch(r'```(?:json)?[ \t]*\n(.*?)\n?```', text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) and 'status' in value else None

def read_events(path):
    for line in path.read_text(errors='replace').splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event

def final_assistant_message(path):
    """Text of the session's FINAL assistant message only (earlier turns and tool output never count).
    Pi: the last assistant message_end. OpenCode: the text parts of the LATEST messageID seen on any part
    event (tool_use, step_start, step_finish, text, ...); a latest message without text means no result."""
    pi_last = None
    texts, latest = {}, None
    for event in read_events(path):
        part, message = event.get('part'), event.get('message')
        if event.get('type') == 'message_end' and isinstance(message, dict) and message.get('role') == 'assistant':
            content = message.get('content')
            pi_last = content if isinstance(content, str) else ''.join(
                p['text'] for p in content or [] if isinstance(p, dict) and p.get('type') == 'text' and isinstance(p.get('text'), str))
        elif isinstance(part, dict):
            is_text = event.get('type') == 'text' and part.get('type') == 'text'
            key = part.get('messageID') or (part.get('id') if is_text else None)
            if key is None:
                continue
            latest = key
            if is_text:
                texts.setdefault(key, {})[part.get('id') or f'p{len(texts.get(key, {}))}'] = part.get('text') or ''
    if pi_last is not None:
        return pi_last
    return ''.join(texts.get(latest, {}).values()) if latest is not None else ''

def is_work(event):
    """Assistant output or a tool call. A user-role message_end (Pi echoing the brief) is not work."""
    kind = event.get('type')
    if kind == 'message_end':
        message = event.get('message')
        return isinstance(message, dict) and message.get('role') == 'assistant'
    return kind == 'text' or 'tool' in str(kind)

def events_show_work(path):
    return any(is_work(event) for event in read_events(path))

def startup_error_event(path):
    """Message of an OpenCode {"type":"error"} stdout event that precedes any work, else None.
    (The opencode-delegate relay/docs do not show this shape; it is the event type OpenCode's json format uses.)"""
    for event in read_events(path):
        if is_work(event):
            return None
        if event.get('type') == 'error':
            detail = event.get('error', event.get('message', ''))
            if isinstance(detail, dict):
                inner = detail.get('data') if isinstance(detail.get('data'), dict) else {}
                detail = inner.get('message') or detail.get('message') or detail.get('name') or json.dumps(detail)
            return str(detail) or 'error'
    return None

if tool != 'codex':
    prompt += ('Your final message must be exactly one JSON object and nothing else (no prose, no code fence) '
               'with keys status, column, detail, prUrl, branch, priorFindings: ' + json.dumps(schema) + '\n')
class BestEffortTemporaryDirectory(tempfile.TemporaryDirectory):
    # Removing the scratch dir must never replace the lane's intended exit code (Windows keeps a file locked
    # while a failed launch's handle is still open): swallow OSError, let the in-flight SystemExit through.
    def __exit__(self, *exc_info):
        try:
            super().__exit__(*exc_info)
        except OSError as error:
            print(f'warning: could not remove {self.name}: {error}', file=sys.stderr)
with BestEffortTemporaryDirectory(prefix='super-board-codex-') as temporary:
    schema_path = Path(temporary) / 'schema.json'
    output = Path(temporary) / 'result.json'
    events = Path(temporary) / 'events.jsonl'
    schema_path.write_text(json.dumps(schema))
    if tool == 'codex':
        env = dict(os.environ)  # Codex keeps the environment it always had
    else:
        # Delegates without a sandbox get an allowlist, never the orchestrator's whole environment: the lane-env
        # ALLOW list + home/config dirs + the names this repo sets + the tool's own key vars. Names compare
        # case-insensitively on Windows; anything secret-looking is dropped except the tool's own key vars.
        lane_env_path = script_path.with_name('super-board-lane-env.py')
        lane_env_spec = importlib.util.spec_from_file_location('lane_env', lane_env_path)
        lane_env = importlib.util.module_from_spec(lane_env_spec)
        lane_env_spec.loader.exec_module(lane_env)
        fold = str.upper if os.name == 'nt' else str
        base = {fold(n) for n in [*lane_env.ALLOW, 'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'XDG_CONFIG_HOME',
                                  'XDG_DATA_HOME', 'GH_CONFIG_DIR', 'SUPER_BOARD_SKILLS_HOME', 'SUPER_BOARD_SKILLS_CATALOG',
                                  'SUPER_BOARD_HOST', 'SUPER_BOARD_RUN', 'SUPER_BOARD_BIN', 'SUPER_BOARD_PACK']}
        own_keys = {fold(n) for n in spec_['env']}
        env = {key: value for key, value in os.environ.items()
               if fold(key) in own_keys or (fold(key) in base and not lane_env.SECRET_NAME.search(key))}
    if tool == 'codex':
        # A custom profile deliberately does NOT extend :workspace, which protects
        # .git as read-only. Explicit git/common-dir grants also cover submodules.
        # https://learn.chatgpt.com/docs/permissions → File access limited to workspace
        filesystem = {':root': 'read', ':slash_tmp': 'write', ':tmpdir': 'write',
                      str(root): 'write', str(git_dir): 'write', str(common_dir): 'write'}
        grants = '{' + ', '.join(json.dumps(key) + '=' + json.dumps(value) for key, value in filesystem.items()) + '}'
        command = ['codex', 'exec', '-C', str(root), '-c', 'approval_policy="never"',
                   '-c', 'default_permissions="super-board-lane"',
                   '-c', 'permissions.super-board-lane.filesystem=' + grants,
                   '-c', 'permissions.super-board-lane.network.enabled=true',
                   '--output-schema', str(schema_path), '--output-last-message', str(output), '-']
        if model and model.strip():
            command[2:2] = ['-m', model]
    elif tool == 'opencode':
        # opencode 1.2.10 (verified 2026-11-02 against the installed binary): `opencode run` has no --auto or any
        # approve flag (passing --auto prints usage and exits 1). Permissions are config: OPENCODE_CONFIG_CONTENT
        # holds inline JSON, and `opencode debug config` / `opencode debug agent build` show this block resolved
        # (edit and bash allow, webfetch deny); `opencode run` with it gets past argument parsing. Brief on stdin.
        command = ['opencode', 'run', '--format', 'json', '--agent', 'build', '--model', model]
        env['OPENCODE_CONFIG_CONTENT'] = json.dumps({'permission': {'edit': 'allow', 'bash': 'allow', 'webfetch': 'deny'}},
                                                    separators=(',', ':'))
        env['PWD'] = str(root)  # opencode can resolve its project root from the inherited PWD
    else:
        # pi-delegate relay.mjs buildArgv: --mode json --model <provider/model> --no-approve; brief on stdin.
        command = ['pi', '--mode', 'json', '--model', model, '--no-approve']
    # Launch the full path shutil.which resolved, never the bare name: Windows Popen does not resolve an npm
    # `.cmd` shim (pi, opencode) by name and raises WinError 2. Never shell=True.
    shim_note = ''
    if tool == 'codex' and resolved.lower().endswith('.cmd'):
        # Codex's argv holds -c JSON full of quotes that cmd.exe would mangle if the .cmd shim ran it, so run the
        # shim's JS entry under node directly (a plain CreateProcess, no cmd.exe).
        entry = Path(resolved).parent / 'node_modules' / '@openai' / 'codex' / 'bin' / 'codex.js'
        node = shutil.which('node')
        if node and entry.is_file():
            command[:1] = [node, str(entry)]
        else:
            shim_note = (f' (codex.cmd resolved but not used: node was not found)' if not node
                         else f' (codex.cmd resolved but not used: {entry} does not exist)')
        # else keep the bare name: Popen cannot start it on Windows, which ends in exit 77 (tool unavailable).
    else:
        command[0] = resolved
    events_handle = None
    try:
        # Each tool's stderr is read on its own (it is the only thing scanned for credit/auth errors); Codex's
        # stdout streams to our stderr too, opencode/pi stdout is the JSON event stream kept in a file.
        events_handle = events.open('w') if tool != 'codex' else None
        child = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.PIPE, text=True, errors='replace',
                                 stdout=subprocess.PIPE if tool == 'codex' else events_handle, stderr=subprocess.PIPE)
    except OSError as error:
        if events_handle:
            events_handle.close()
        unavailable(f'could not launch {spec_["binary"]}: {error}{shim_note}')
    err_lines, out_lines = collections.deque(maxlen=2000), collections.deque(maxlen=2000)
    def drain(stream, sink):
        for line in stream:
            sys.stderr.write(line)
            sys.stderr.flush()
            sink.append(line)
    def feed():
        try:
            child.stdin.write(prompt)
            child.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    workers = [threading.Thread(target=feed, daemon=True), threading.Thread(target=drain, args=(child.stderr, err_lines), daemon=True)]
    if tool == 'codex':
        workers.append(threading.Thread(target=drain, args=(child.stdout, out_lines), daemon=True))
    for worker in workers:
        worker.start()
    try:
        child.wait()
        for worker in workers:
            worker.join(timeout=5)
    finally:
        if events_handle:  # a SIGTERM/SIGINT exit mid-wait must still release the file
            events_handle.close()
    require_verification_ready()
    code = child.returncode
    if code in (9, 10, 11, 79):
        result('halted', 'verification-readiness: repair verify_commands and restart explicitly; preserve work' if code == 9
               else 'skills-readiness: required skills became unavailable; preserve work' if code == 10
               else 'identity-mismatch: delegate exited 11; preserve card, approval, claims and worktree' if code == 11
               else f'{tool} exec exited {code}')
        sys.exit(code)
    # "Startup failure" (OpenCode/Pi only): the tool produced nothing yet, so it cannot have pushed, moved the
    # card or commented. Its own stderr, minus any line that merely echoes our prompt, or an OpenCode stdout
    # error event before any work, is matched against the tool's anchored credit/auth regex.
    startup = False
    credit = None
    event_error = None
    if tool != 'codex':
        echoed = {line.strip() for line in prompt.splitlines()}
        own = [line.strip() for line in err_lines if line.strip() and line.strip() not in echoed]
        startup = not events_show_work(events) and len(own) <= 40
        if startup:
            event_error = startup_error_event(events) if tool == 'opencode' else None
            candidates = own + ([f'Error: {event_error}'] if event_error else [])
            credit = next((line for line in candidates if re.match(spec_['unavailable'], line, re.I)), None)
    if code:
        if credit:
            unavailable(f'credit/quota/auth failure on startup ({credit[:120]}), exit {code}')
        result('failed', f'{tool} exec exited {code}')
        sys.exit(1)
    # Exit 0 proves nothing: these CLIs return 0 on API errors. Only a valid result counts.
    if tool == 'codex':
        raw = output.read_text(errors='replace') if output.exists() else ''
        try:
            parsed = json.loads(raw) if raw.strip() else None
        except ValueError:
            parsed = None
    else:
        raw = final_assistant_message(events)
        parsed = strict_lane_result(raw)
    if not raw.strip():
        if not explicit and startup and (credit or event_error is None):  # usage-fallback delegate that did nothing at all
            unavailable(f'credit/quota/auth failure on startup ({credit[:120]}), exit 0 with empty output' if credit
                        else 'exit 0 with empty output')
        result('failed', f'{label} returned no valid lane result: empty output')
        sys.exit(1)
    if (not isinstance(parsed, dict) or parsed.get('status') not in properties['status']['enum']
            or not all(isinstance(parsed.get(key), str) for key in ['column', 'detail'])):
        result('failed', f'{label} returned no valid lane result: the final message is not one lane-result JSON object')
        sys.exit(1)
    print(json.dumps(parsed))
    sys.exit(10 if parsed['status'] == 'halted' and parsed['detail'].startswith('skills-readiness:')
             else 9 if parsed['status'] == 'halted' and parsed['detail'].startswith('verification-readiness:')
             else 11 if parsed['status'] == 'halted' and parsed['detail'].startswith('identity-mismatch:')
             else 79 if parsed['status'] == 'halted' else 0)
PY
