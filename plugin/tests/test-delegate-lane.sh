#!/usr/bin/env bash
# Fake codex/opencode/pi only: the OpenCode and Pi fallback lanes (opt-in, review refusal, env allowlist,
# strict final-message result) and the exit-77 "tool unavailable" contract (missing binary, or a startup
# failure before the tool did any work) for all three tools, plus the wave's handling of 77. No network.
set -euo pipefail
KIT=$(cd "$(dirname "$0")/.." && pwd)
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/repo"
git init -q "$T/repo"
python3 "$KIT/tests/fixtures/install_worker_skills.py" "$T/repo"
export SUPER_BOARD_SKILLS_HOME="$T/home"; mkdir -p "$SUPER_BOARD_SKILLS_HOME"
cat > "$T/bin/gh" <<'PYGH'
#!/usr/bin/env python3
import json,sys
args=sys.argv[1:]
if args[:2]==['issue','view']:
    print(json.dumps({'labels':[]} if args[args.index('--json')+1] == 'labels' else {'number':int(args[2]),'title':'fixture','body':''}))
else:
    sys.exit('unexpected gh fixture call: '+str(args))
PYGH
chmod +x "$T/bin/gh"
LOG="$T/calls.jsonl"
# A sitecustomize on PYTHONPATH (the lane script's own interpreter loads it) records what the lane passes to
# Popen as argv[0], and can make temp-dir removal fail, to prove the launch path and the exit-code contract.
mkdir -p "$T/hook"
cat > "$T/hook/sitecustomize.py" <<'PYHOOK'
import json, os, shutil, subprocess
_popen_init = subprocess.Popen.__init__
def _logged_init(self, args, *a, **k):
    if isinstance(args, (list, tuple)) and args and os.environ.get('POPEN_LOG'):
        with open(os.environ['POPEN_LOG'], 'a') as log:
            log.write(json.dumps(str(args[0])) + '\n')
        if os.environ.get('POPEN_ARGV_LOG'):
            with open(os.environ['POPEN_ARGV_LOG'], 'a') as log:
                log.write(json.dumps([str(x) for x in args]) + '\n')
    _popen_init(self, args, *a, **k)
subprocess.Popen.__init__ = _logged_init
if os.environ.get('WHICH_ONCE'):   # a second lookup of the same name finds nothing: the lane must resolve once
    _which, _seen = shutil.which, set()
    def _once(cmd, *a, **k):
        found = _which(cmd, *a, **k)
        if cmd in _seen:
            return None
        _seen.add(cmd)
        return found
    shutil.which = _once
if os.environ.get('FAKE_WHICH_CODEX'):   # POSIX which() never finds codex.cmd; pretend it resolved to this path
    _real_which = shutil.which
    def _fake_which(cmd, *a, **k):
        if cmd == 'codex':
            return os.environ['FAKE_WHICH_CODEX']
        return None if cmd == 'node' and os.environ.get('NO_NODE') else _real_which(cmd, *a, **k)
    shutil.which = _fake_which
if os.environ.get('TASKKILL_LOG'):   # simulated Windows: record the taskkill argv, then really kill the pid
    _run = subprocess.run
    def _taskkill_run(args, *a, **k):
        if args and args[0] == 'taskkill':
            with open(os.environ['TASKKILL_LOG'], 'a') as log:
                log.write(json.dumps(args) + '\n')
            if os.environ.get('FAKE_TASKKILL_RC'):   # a taskkill that fails and kills nothing
                return subprocess.CompletedProcess(args, int(os.environ['FAKE_TASKKILL_RC']), b'', b'')
            os.kill(int(args[-1]), 9)
            return subprocess.CompletedProcess(args, 0, b'', b'')
        return _run(args, *a, **k)
    subprocess.run = _taskkill_run
if os.environ.get('RMTREE_FAIL'):
    def _denied(*a, **k):
        raise PermissionError(13, 'simulated: file still open')
    shutil.rmtree = _denied
PYHOOK
export PYTHONPATH="$T/hook" POPEN_LOG="$T/popen.log"
export GH_CONFIG_DIR="$T/robot-gh"   # a path only; nothing under it is ever read
export FAKE_SECRET=must-not-leak RUNINFRA_GATEWAY_KEY=dummy-not-a-real-key SUPER_BOARD_FAKE_TOKEN=must-not-leak
export PATHEXT=.COM:.EXE APPDATA="$T/appdata"
# Fakes only: the PATH holds nothing but the fakes and system tools, so a missing fake can never reach a real CLI.
export PATH="$T/bin:/usr/local/bin:/usr/bin:/bin"
# One fake for all three tools; the file $T/mode picks the behaviour (env vars would be filtered out, rightly).
cat > "$T/bin/fake-delegate" <<'PY'
#!/usr/bin/env python3
import json, os, re, sys
tool = os.path.basename(sys.argv[0])
prompt = sys.stdin.read()
card = json.loads(re.search(r'^Card: (.+)$', prompt, re.M).group(1))
lane = re.search(r'Run super-(build|qa|review)', prompt).group(1)
root = os.path.dirname(os.path.abspath(sys.argv[0])) + '/..'
with open(root + '/calls.jsonl', 'a') as log:
    log.write(json.dumps(dict(tool=tool, args=sys.argv[1:], prompt=prompt, number=card['number'], lane=lane,
                              gh_config_dir=os.environ.get('GH_CONFIG_DIR'), pwd=os.environ.get('PWD'), cwd=os.getcwd(),
                              has_fake_secret='FAKE_SECRET' in os.environ, has_gateway_key='RUNINFRA_GATEWAY_KEY' in os.environ,
                              has_fake_token='SUPER_BOARD_FAKE_TOKEN' in os.environ, has_pathext='PATHEXT' in os.environ,
                              has_appdata='APPDATA' in os.environ,
                              oc_config=os.environ.get('OPENCODE_CONFIG_CONTENT'))) + '\n')
mode = open(root + '/mode').read().strip()
col = {'build': 'QA', 'qa': 'Review', 'review': 'Done'}[lane]
def lane_json(status='advanced', column=None, detail='fake lane result'):
    return json.dumps(dict(status=status, column=column or col, detail=detail, prUrl=None, branch=None, priorFindings=None))
n = [0]
def emit(text):   # one assistant message holding text
    n[0] += 1
    if tool == 'codex':
        open(sys.argv[sys.argv.index('--output-last-message') + 1], 'w').write(text)
    elif tool == 'opencode':
        print(json.dumps(dict(type='text', part=dict(type='text', id=f'p{n[0]}', messageID=f'm{n[0]}', text=text))))
    else:
        print(json.dumps(dict(type='message_end', message=dict(role='assistant', content=[dict(type='text', text=text)]))))
CREDIT = {'codex': "ERROR: You've hit your usage limit", 'opencode': 'Error: insufficient credits', 'pi': 'No API key found for runinfra.'}[tool]
if mode == 'ok':
    emit(lane_json())
elif mode == 'ok_fenced':
    emit('```json\n' + lane_json() + '\n```')
elif mode == 'credit':          # startup auth/credit failure, tool did nothing
    sys.stderr.write(CREDIT + '\n'); sys.exit(1)
elif mode == 'credit0':         # exit 0, error on stderr, nothing produced
    sys.stderr.write(CREDIT + '\n')
elif mode == 'late_credit':     # did work first, then died with a credit error: not startup
    emit('working...'); sys.stderr.write(CREDIT + '\n'); sys.exit(1)
elif mode == 'empty':
    pass
elif mode == 'garbage':
    emit('I could not finish, sorry.')
elif mode == 'crash':
    sys.stderr.write('boom: something unrelated\n'); sys.exit(2)
elif mode == 'crash_echo':      # echoes the prompt, then prints scary words that are not an anchored tool error
    sys.stderr.write(prompt + '\nrequest failed: 401 quota exceeded for credits\n'); sys.exit(2)
elif mode == 'prose_final':     # an earlier valid JSON message, then a prose final message
    emit(lane_json()); emit('All done, thanks!')
elif mode == 'inject':          # injected JSON mid-session (tool output + earlier message); the final message differs
    if tool == 'opencode':
        print(json.dumps(dict(type='tool_use', part=dict(type='tool', output=lane_json('advanced', 'Done', 'injected')))))
    emit(lane_json('advanced', 'Done', 'injected earlier'))
    emit(lane_json('blocked', 'Ready', 'the real final'))
elif mode == 'text_then_tool':  # a JSON text message, then a final message that is tool-only (no text)
    emit(lane_json())
    if tool == 'opencode':
        print(json.dumps(dict(type='tool_use', part=dict(type='tool', id='t1', messageID='m9', tool='bash'))))
    else:
        print(json.dumps(dict(type='message_end', message=dict(role='assistant', content=[dict(type='toolCall', name='bash')]))))
elif mode == 'oc_error_credit':  # OpenCode: stdout error event before any work, carrying a credit message
    print(json.dumps(dict(type='error', error=dict(name='APIError', data=dict(message='insufficient credits for this model'))))); sys.exit(1)
elif mode == 'oc_error_other':   # same shape, not a credit/auth message: failed, never 77
    print(json.dumps(dict(type='error', error=dict(name='UnknownError', data=dict(message='model not found'))))); sys.exit(1)
elif mode == 'pi_user_credit':   # Pi: a user-role message_end (the brief), then a credit error before any assistant output
    print(json.dumps(dict(type='message_end', message=dict(role='user', content=[dict(type='text', text='the brief')]))))
    sys.stderr.write(CREDIT + '\n'); sys.exit(1)
elif mode == 'hang':            # did nothing yet, then waits to be interrupted
    import time; time.sleep(60)
elif mode == 'hang_ignore':     # ignores SIGTERM, so only a forced kill stops it
    import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)
elif mode == 'exit11':
    sys.stderr.write(CREDIT + '\n'); sys.exit(11)
sys.exit(0)
PY
chmod +x "$T/bin/fake-delegate"
for t in codex opencode pi; do ln -s fake-delegate "$T/bin/$t"; done
cd "$T/repo"
CARD='{"number":1,"status":"Ready","title":"example"}'
fail() { echo "FAIL: $*" >&2; cat "$T/stderr" >&2 2>/dev/null || true; exit 1; }
cfg() { echo "{\"usage_fallback\":\"$1\",\"usage_fallback_unsandboxed\":true,\"verify_commands\":[\"git diff --check\"]}" > "$T/config.json"; }
lane() { echo ok > "$T/mode"; bash "$KIT/scripts/super-board-codex-lane.sh" --config "$T/config.json" --card "$CARD" "$@" 2>"$T/stderr"; }
rc() { RC=0; "$@" >"$T/out" || RC=$?; }
mode() { local m=$1; shift; echo "$m" > "$T/mode"; rc bash "$KIT/scripts/super-board-codex-lane.sh" --config "$T/config.json" --card "$CARD" "$@" 2>"$T/stderr"; }
last() { tail -1 "$LOG" | jq -r "$1"; }
labelof() { case $1 in codex) echo Codex;; opencode) echo OpenCode;; pi) echo Pi;; esac; }

for TOOL in opencode pi; do
  cfg $TOOL; : > "$LOG"
  # A build lane succeeds; the result is the session's final assistant message (bare or in one json fence).
  lane --tool $TOOL --lane build >"$T/out"
  jq -e '.status == "advanced" and .column == "QA" and .detail == "fake lane result"' "$T/out" >/dev/null || fail "$TOOL build result"
  [ "$(last .tool)" = "$TOOL" ] && [ "$(last .lane)" = build ] || fail "$TOOL fake not invoked as the build lane"
  last '.prompt' | grep -q -- "--lane build --model $(labelof $TOOL) --host codex" || fail "$TOOL prompt must carry the move flags"
  mode ok_fenced --tool $TOOL --lane qa
  [ "$RC" -eq 0 ] && jq -e '.status == "advanced" and .column == "Review"' "$T/out" >/dev/null || fail "$TOOL fenced qa result"
  # Exact invocation, from the delegate relays' buildArgv.
  if [ $TOOL = opencode ]; then
    # opencode 1.2.10 has no --auto; its permissions come from a config block in OPENCODE_CONFIG_CONTENT.
    [ "$(last '.args | join(" ")')" = "run --format json --agent build --model opencode-go/kimi-k2.7-code" ] || fail "opencode argv: $(last '.args | join(" ")')"
    [ "$(last .oc_config)" = '{"permission":{"edit":"allow","bash":"allow","webfetch":"deny"}}' ] || fail "opencode permission config: $(last .oc_config)"
  else
    [ "$(last '.args | join(" ")')" = "--mode json --model runinfra/glm-5-3-flash --no-approve" ] || fail "pi argv: $(last '.args | join(" ")')"
    [ "$(last .oc_config)" = null ] || fail "pi must not get OPENCODE_CONFIG_CONTENT"
  fi
  # The lane launches the full path shutil.which resolved, never the bare name (Windows Popen cannot resolve a .cmd shim by name).
  : > "$T/popen.log"; lane --tool $TOOL --lane build >/dev/null
  grep -Fxq "\"$T/bin/$TOOL\"" "$T/popen.log" || fail "$TOOL must be launched via its resolved path $T/bin/$TOOL, Popen got: $(cat "$T/popen.log")"
  ! grep -Fxq "\"$TOOL\"" "$T/popen.log" || fail "$TOOL was launched by its bare name"
  # The bot identity env reaches the delegate untouched; the repo is its cwd.
  [ "$(last .gh_config_dir)" = "$GH_CONFIG_DIR" ] || fail "$TOOL lost GH_CONFIG_DIR"
  [ "$(last .cwd)" = "$(cd "$T/repo" && pwd -P)" ] || fail "$TOOL not started in the repo"
  # Env allowlist: the orchestrator's secrets never reach an unsandboxed delegate; only its own key var does.
  [ "$(last .has_fake_secret)" = false ] || fail "$TOOL saw FAKE_SECRET"
  if [ $TOOL = opencode ]; then [ "$(last .has_gateway_key)" = false ] || fail "opencode saw RUNINFRA_GATEWAY_KEY"
  else [ "$(last .has_gateway_key)" = true ] || fail "pi must get its own RUNINFRA_GATEWAY_KEY"; fi
  [ "$(last .has_fake_token)" = false ] || fail "$TOOL saw SUPER_BOARD_FAKE_TOKEN (only the explicit SUPER_BOARD_* names pass)"
  [ "$(last .has_pathext)" = true ] && [ "$(last .has_appdata)" = true ] || fail "$TOOL must keep PATHEXT and APPDATA"
  # A configured model wins over the default.
  echo "{\"usage_fallback\":\"$TOOL\",\"usage_fallback_unsandboxed\":true,\"$TOOL\":{\"model\":\"cfg/model-x\"},\"verify_commands\":[\"git diff --check\"]}" > "$T/config.json"
  lane --tool $TOOL --lane build >"$T/out"
  last '.args | join(" ")' | grep -q -- "--model cfg/model-x" || fail "$TOOL configured model"
  cfg $TOOL
  # The binary is resolved once: a later lookup that finds nothing must not turn into a TypeError (1).
  WHICH_ONCE=1 mode ok --tool $TOOL --lane build
  [ "$RC" -eq 0 ] || fail "$TOOL must launch from the one resolved path even if a second lookup fails, got $RC"
  # The model reaches a .cmd shim's argv: anything outside [A-Za-z0-9._/:-] is refused (64) before launch.
  for BAD in 'm;calc' 'a b' 'x&y' 'm"q' 'a|b' 'a%PATH%' 'a^b' '<x>'; do
    echo "{\"usage_fallback\":\"$TOOL\",\"usage_fallback_unsandboxed\":true,\"$TOOL\":{\"model\":\"$(printf %s "$BAD" | sed 's/"/\\"/g')\"},\"verify_commands\":[\"git diff --check\"]}" > "$T/config.json"
    : > "$LOG"; rc lane --tool $TOOL --lane build
    [ "$RC" -eq 64 ] && [ ! -s "$LOG" ] || fail "$TOOL model '$BAD' must exit 64 before launching, got $RC"
    grep -q 'model' "$T/stderr" || fail "$TOOL bad model must say why"
  done
  cfg $TOOL
  # Review is refused before the tool runs: 78 plain; with --codex the flag itself is invalid (64).
  : > "$LOG"
  rc lane --tool $TOOL --lane review
  [ "$RC" -eq 78 ] && [ ! -s "$LOG" ] || fail "$TOOL review must refuse (78) before launching, got $RC"
  rc lane --tool $TOOL --lane review --codex
  [ "$RC" -eq 64 ] && [ ! -s "$LOG" ] || fail "$TOOL review --codex must exit 64 before launching, got $RC"
  # usage_fallback must name this tool.
  echo '{"usage_fallback":"none","usage_fallback_unsandboxed":true,"verify_commands":["git diff --check"]}' > "$T/config.json"
  rc lane --tool $TOOL --lane build
  [ "$RC" -eq 78 ] && [ ! -s "$LOG" ] || fail "$TOOL fallback without usage_fallback must refuse (78)"
  echo '{"usage_fallback":"codex","usage_fallback_unsandboxed":true,"verify_commands":["git diff --check"]}' > "$T/config.json"
  rc lane --tool $TOOL --lane build
  [ "$RC" -eq 78 ] || fail "usage_fallback codex must not authorize $TOOL"
  # No filesystem sandbox: opt-in only, even when usage_fallback names the tool.
  for V in '' ',"usage_fallback_unsandboxed":false' ',"usage_fallback_unsandboxed":"true"'; do
    echo "{\"usage_fallback\":\"$TOOL\"$V,\"verify_commands\":[\"git diff --check\"]}" > "$T/config.json"
    rc lane --tool $TOOL --lane build
    [ "$RC" -eq 78 ] && [ ! -s "$LOG" ] || fail "$TOOL without usage_fallback_unsandboxed:true must refuse (78), got $RC [$V]"
  done
  cfg $TOOL
  # Result extraction: only the final assistant message, whole, as one JSON object.
  mode prose_final --tool $TOOL --lane build
  [ "$RC" -eq 1 ] && jq -e '.status == "failed"' "$T/out" >/dev/null || fail "$TOOL earlier JSON then prose final must be failed (1), got $RC"
  mode inject --tool $TOOL --lane build
  [ "$RC" -eq 0 ] && jq -e '.status == "blocked" and .detail == "the real final"' "$T/out" >/dev/null || fail "$TOOL injected mid-session JSON must be ignored: $(cat "$T/out")"
  mode garbage --tool $TOOL --lane build
  [ "$RC" -eq 1 ] && jq -e '.status == "failed"' "$T/out" >/dev/null || fail "$TOOL garbage final message must be failed (1), got $RC"
  mode text_then_tool --tool $TOOL --lane build
  [ "$RC" -eq 1 ] && jq -e '.status == "failed"' "$T/out" >/dev/null || fail "$TOOL JSON text then a tool-only final message must be failed (1), got $RC: $(cat "$T/out")"
done
# OpenCode: a stdout error event before any work is a startup error (credit message -> 77, anything else -> failed).
cfg opencode
mode oc_error_credit --tool opencode --lane build
[ "$RC" -eq 77 ] && jq -e '.status == "unavailable"' "$T/out" >/dev/null || fail "opencode stdout credit error event must be 77, got $RC: $(cat "$T/out")"
mode oc_error_other --tool opencode --lane build
[ "$RC" -eq 1 ] && jq -e '.status == "failed"' "$T/out" >/dev/null || fail "opencode non-credit error event must be failed (1), got $RC"
# Pi: a user-role message_end is not work; a credit error after it is still a startup error.
cfg pi
mode pi_user_credit --tool pi --lane build
[ "$RC" -eq 77 ] || fail "pi user-role message_end then credit error must be 77, got $RC"

# Exit 77 = tool unavailable. OpenCode/Pi: only before the tool did any work. Codex: ONLY a missing binary.
for TOOL in codex opencode pi; do
  cfg $TOOL; LABEL=$(labelof $TOOL)
  if [ $TOOL = codex ]; then
    # codex exec prints its whole session to stderr, so a usage-limit/auth/empty run stays failed (1), as before.
    FAILED_MODES="credit credit0 empty crash crash_echo late_credit garbage"
  else
    for MODE in credit credit0 empty; do
      mode $MODE --tool $TOOL --lane build
      [ "$RC" -eq 77 ] || fail "$TOOL/$MODE must exit 77, got $RC"
      jq -e --arg p "$LABEL unavailable" '.status == "unavailable" and .column == "Ready" and (.detail | startswith($p))' "$T/out" >/dev/null || fail "$TOOL/$MODE result JSON: $(cat "$T/out")"
      grep -q 'unavailable' "$T/stderr" || fail "$TOOL/$MODE must log the reason"
    done
    # Everything else is a plain failure, never 77: unrelated crash; a prompt echo plus scary words; a credit error after work.
    FAILED_MODES="crash crash_echo late_credit garbage"
  fi
  for MODE in $FAILED_MODES; do
    mode $MODE --tool $TOOL --lane build
    [ "$RC" -eq 1 ] && jq -e '.status == "failed"' "$T/out" >/dev/null || fail "$TOOL/$MODE must be failed (1), got $RC: $(cat "$T/out")"
  done
  # Identity-mismatch (11) is a halt, never unavailable, even with a credit-looking stderr.
  mode exit11 --tool $TOOL --lane build
  [ "$RC" -eq 11 ] && jq -e '.status == "halted" and (.detail | startswith("identity-mismatch:"))' "$T/out" >/dev/null || fail "$TOOL exit 11 must stay a halt (11), got $RC"
  # Missing binary.
  mv "$T/bin/$TOOL" "$T/$TOOL.away"
  rc bash "$KIT/scripts/super-board-codex-lane.sh" --config "$T/config.json" --card "$CARD" --lane build --tool $TOOL
  mv "$T/$TOOL.away" "$T/bin/$TOOL"
  [ "$RC" -eq 77 ] || fail "$TOOL missing binary must exit 77, got $RC"
done
# Temp-dir cleanup is best effort: a launch failure whose cleanup also raises still exits 77, never a traceback (1).
for TOOL in opencode pi; do
  cfg $TOOL
  mv "$T/bin/$TOOL" "$T/$TOOL.away"
  printf '#!/nonexistent/interpreter
' > "$T/bin/$TOOL"; chmod +x "$T/bin/$TOOL"   # found on PATH, cannot be started
  RMTREE_FAIL=1 mode ok --tool $TOOL --lane build
  rm -f "$T/bin/$TOOL"; mv "$T/$TOOL.away" "$T/bin/$TOOL"
  [ "$RC" -eq 77 ] || fail "$TOOL launch failure with failing cleanup must exit 77, got $RC"
  jq -e '.status == "unavailable"' "$T/out" >/dev/null || fail "$TOOL launch failure result: $(cat "$T/out")"
  grep -q 'could not launch' "$T/stderr" || fail "$TOOL must log could not launch"
  grep -q 'Traceback' "$T/stderr" && fail "$TOOL cleanup error leaked a traceback" || true
  grep -q 'warning: could not remove .*super-board-codex-.*: .*simulated' "$T/stderr" || fail "$TOOL failed cleanup must warn on stderr: $(cat "$T/stderr")"
done
# Under an explicit --codex run, "exit 0 with nothing" is a plain failure, not a fallback signal.
cfg codex
mode empty --tool codex --lane build --codex
[ "$RC" -eq 1 ] && jq -e '.status == "failed"' "$T/out" >/dev/null || fail "explicit --codex exit 0 with empty output must stay failed (1), got $RC"
# Codex keeps its whole environment. A POSIX (or real .exe) resolution launches through the resolved path.
: > "$T/popen.log"
mode ok --tool codex --lane build
grep -Fxq "\"$T/bin/codex\"" "$T/popen.log" || fail "codex must be launched via its resolved path, Popen got: $(cat "$T/popen.log")"
! grep -Fxq '"codex"' "$T/popen.log" || fail "codex was launched by its bare name"
mode ok --tool codex --lane build
[ "$RC" -eq 0 ] && [ "$(last .has_fake_secret)" = true ] || fail "codex environment must be untouched"
# Windows npm shim: codex resolves to codex.cmd; the lane runs [node, codex.js, ...] directly (no cmd.exe) with the
# -c JSON args byte-identical to the plain run. The suffix alone drives this, so it runs here on POSIX.
export POPEN_ARGV_LOG="$T/argv.log"; : > "$T/argv.log"; : > "$LOG"
codexargv() { jq -c 'select(any(.[]; . == "exec"))' "$T/argv.log" | tail -1; }   # the codex launch among the lane's other Popen calls
norm() { sed -E 's/super-board-codex-[A-Za-z0-9_]+/super-board-codex-X/g'; }        # the per-run temp dir name is random
mode ok --tool codex --lane build
[ "$RC" -eq 0 ] || fail "plain codex run rc $RC"
PLAIN_ARGS=$(last '.args | tojson' | norm); PLAIN_ARGV=$(codexargv | norm)
[ "$(echo "$PLAIN_ARGV" | jq -r '.[1]')" = exec ] || fail "plain codex argv not captured: $PLAIN_ARGV"
SHIM="$T/npm"; mkdir -p "$SHIM/node_modules/@openai/codex/bin" "$T/nodebin"
: > "$SHIM/codex.cmd"; : > "$SHIM/node_modules/@openai/codex/bin/codex.js"
cat > "$T/nodebin/node" <<SH
#!/bin/sh
shift   # drop codex.js; the fake codex then logs the rest as usual
exec "$T/bin/codex" "\$@"
SH
chmod +x "$T/nodebin/node"
export FAKE_WHICH_CODEX="$SHIM/codex.cmd"
: > "$T/argv.log"; : > "$LOG"; OLDPATH=$PATH; PATH="$T/nodebin:$PATH"
mode ok --tool codex --lane build
PATH=$OLDPATH
[ "$RC" -eq 0 ] || fail "codex via .cmd shim rc $RC"
[ "$(codexargv | jq -r '.[0:2] | join("|")')" = "$T/nodebin/node|$SHIM/node_modules/@openai/codex/bin/codex.js" ] || fail "shim must launch [node, codex.js], got: $(codexargv)"
[ "$(codexargv | norm | jq -c '.[2:]')" = "$(echo "$PLAIN_ARGV" | jq -c '.[1:]')" ] || fail "shim -c args differ from the plain run"
[ "$(last '.args | tojson' | norm)" = "$PLAIN_ARGS" ] || fail "shim run args differ from the plain run"
codexargv | grep -q 'approval_policy=\\"never\\"' || fail "-c JSON quotes lost"
# A .cmd shim without codex.js, or without node, keeps the bare-name launch.
mv "$SHIM/node_modules/@openai/codex/bin/codex.js" "$SHIM/codex.js.away"
: > "$T/popen.log"; PATH="$T/nodebin:$PATH"
mode ok --tool codex --lane build
PATH=$OLDPATH
grep -Fxq '"codex"' "$T/popen.log" || fail ".cmd without codex.js must keep the bare name, Popen got: $(cat "$T/popen.log")"
mv "$SHIM/codex.js.away" "$SHIM/node_modules/@openai/codex/bin/codex.js"
: > "$T/popen.log"
NO_NODE=1 mode ok --tool codex --lane build   # which('node') finds nothing
grep -Fxq '"codex"' "$T/popen.log" || fail ".cmd without node must keep the bare name, Popen got: $(cat "$T/popen.log")"
# A shim run with a configured model: -m sits right after exec.
PATH="$T/nodebin:$PATH"; : > "$T/argv.log"
mode ok --tool codex --lane build --codex=m-x
PATH=$OLDPATH
[ "$RC" -eq 0 ] || fail "shim run with a model rc $RC"
[ "$(codexargv | jq -c '.[2:5]')" = '["exec","-m","m-x"]' ] || fail "shim argv[2:5] must be exec -m <model>, got: $(codexargv)"
# When the shim cannot be used and the bare name then fails to launch, unavailable (77) says why.
mv "$T/bin/codex" "$T/codex.away"
mv "$SHIM/node_modules/@openai/codex/bin/codex.js" "$SHIM/codex.js.away"
PATH="$T/nodebin:$PATH"; mode ok --tool codex --lane build; PATH=$OLDPATH
[ "$RC" -eq 77 ] && jq -e --arg p "$SHIM/node_modules/@openai/codex/bin/codex.js" '.detail | contains($p)' "$T/out" >/dev/null || fail "missing codex.js must be named in the 77 detail, got $RC: $(cat "$T/out")"
mv "$SHIM/codex.js.away" "$SHIM/node_modules/@openai/codex/bin/codex.js"
NO_NODE=1 mode ok --tool codex --lane build
[ "$RC" -eq 77 ] && jq -e '.detail | contains("node was not found")' "$T/out" >/dev/null || fail "missing node must be named in the 77 detail, got $RC: $(cat "$T/out")"
mv "$T/codex.away" "$T/bin/codex"
unset FAKE_WHICH_CODEX POPEN_ARGV_LOG
# Interrupting the lane. Simulated Windows (SIM=1) kills the child tree with taskkill /T /F; if taskkill fails (rc 1) or
# POSIX is in play, the direct terminate()/kill() runs, and a worker that ignores SIGTERM is still force-killed. Always rc 143.
interrupt_case() {   # <name> <hang mode> <SUPER_BOARD_TEST_WINDOWS> <fake taskkill rc or ''>
  local NAME=$1 MODE=$2 SIM=$3 TKRC=$4 START LPID CPID
  : > "$T/taskkill.log"; : > "$LOG"; echo "$MODE" > "$T/mode"
  SUPER_BOARD_TEST_WINDOWS=$SIM FAKE_TASKKILL_RC=$TKRC TASKKILL_LOG="$T/taskkill.log" bash "$KIT/scripts/super-board-codex-lane.sh" --config "$T/config.json" --card "$CARD" --lane build --tool codex >"$T/out" 2>"$T/stderr" &
  LPID=$!
  for _ in $(seq 100); do [ -s "$LOG" ] && break; sleep 0.1; done
  [ -s "$LOG" ] || fail "$NAME: hang fake never started"
  sleep 0.3   # let the fake install its SIGTERM handling
  CPID=$(pgrep -P $LPID | head -1); START=$SECONDS
  kill -TERM $LPID; RC=0; wait $LPID || RC=$?
  [ "$RC" -eq 143 ] || fail "$NAME: interrupted lane must exit 143, got $RC"
  [ $((SECONDS - START)) -le 8 ] || fail "$NAME: interrupt took $((SECONDS - START))s"
  ! kill -0 "$CPID" 2>/dev/null || fail "$NAME: worker $CPID survived the interrupt"
  # A failed taskkill leaves the launcher's descendants unconfirmed, so the halt must say the worker may survive.
  if [ -n "$TKRC" ]; then WANT_ALIVE=true; else WANT_ALIVE=false; fi
  jq -e --argjson w "$WANT_ALIVE" '.status == "halted" and ((.detail | contains("may still be alive")) == $w)' "$T/out" >/dev/null || fail "$NAME: halted result: $(cat "$T/out")"
  case "$SIM" in
    1) [ "$(jq -c . "$T/taskkill.log" | head -1)" = "[\"taskkill\",\"/T\",\"/F\",\"/PID\",\"$CPID\"]" ] || fail "$NAME: taskkill argv: $(cat "$T/taskkill.log") (child $CPID)";;
    *) [ ! -s "$T/taskkill.log" ] || fail "$NAME: POSIX must not call taskkill";;
  esac
}
interrupt_case "windows" hang 1 ""
interrupt_case "posix" hang "" ""
interrupt_case "taskkill fails, falls back to kill" hang 1 1
interrupt_case "worker ignores SIGTERM, force-killed" hang_ignore "" ""
interrupt_case "taskkill fails, worker ignores SIGTERM" hang_ignore 1 1

# Wave: 77 is "unavailable", not failed and not halted; the lock is released and the reason logged.
echo '{"max_workers":1,"verify_commands":["git diff --check"]}' > "$T/config.json"
CARDS='[{"number":1,"status":"Ready"},{"number":2,"status":"Review"}]'
# Codex 77 is a missing binary only, so the wave case removes the binary.
echo ok > "$T/mode"; mv "$T/bin/codex" "$T/codex.away"
RC=0; bash "$KIT/scripts/super-board-codex-wave.sh" --config "$T/config.json" --cards "$CARDS" --codex=pinned >"$T/out" 2>"$T/stderr" || RC=$?
[ "$RC" -eq 0 ] || fail "unavailable must not halt the wave (rc $RC)"
jq -e '.halted == false and .cards[0].finalStatus == "unavailable" and .cards[0].lanesRun == "build:unavailable" and .cards[0].column == "Ready" and .cards[1].finalStatus == "unavailable"' "$T/out" >/dev/null || fail "wave summary: $(cat "$T/out")"
mv "$T/codex.away" "$T/bin/codex"
grep -q 'Codex unavailable' "$T/stderr" || fail 'wave must log "<tool> unavailable: <reason>"'
[ -z "$(ls -A .claude/super-board/inflight 2>/dev/null)" ] || fail 'unavailable lane leaves no lock behind'
echo 'PASS: test-delegate-lane.sh (opencode/pi opt-in lanes, review refusal, env allowlist, strict result, exit 77 startup-only for codex/opencode/pi, wave release)'
