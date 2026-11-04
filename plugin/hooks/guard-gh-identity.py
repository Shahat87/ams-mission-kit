#!/usr/bin/env python3
"""PreToolUse(Bash|PowerShell|Read|Grep|Glob) guard: keep the session on the robot GitHub identity.

Hardened over time. Three layers, all fast and offline:

 1. Deny rules on the FULL command text (so `bash -c '...'`, `python -c` and `$( )` are covered):
    `gh auth token|switch|login|...`, any mention of GH_CONFIG_DIR / GH_TOKEN / GITHUB_TOKEN /
    GH_HOST, git credential config, cmdkey, credential-store APIs, the bot's gh config dir.
 2. Dynamic-name refusal: a command that BUILDS a variable name to reach those variables is
    refused -- string concatenation (`"GH_"+"TOKEN"`, `'GITHUB_' + $x`), `${!name}` indirection,
    `eval`/`Invoke-Expression` with expansions, name stems (`"GH_"`), `printenv`, bare `env`,
    bare `set`, `export -p`, `declare -x`, `Get-ChildItem Env:`, `Get-Item Env:X`,
    `[Environment]::GetEnvironmentVariable(s)`, `/proc/*/environ`.
 3. A POSITIVE rule for `gh`: a `gh` invocation is allowed only if
      - its subcommand is on the board allowlist (issue, api, project, run, workflow, label, search,
        pr create|view|list|comment|edit (no base change)|ready|checks|diff, repo view, release view);
        auth, alias, extension, config, secret, ssh-key, gpg-key, codespace, ... are refused. Matching
        is quote-insensitive (`gh 'alias' set` is the same command). It carries no `--hostname`, no
        `-H Authorization:` header;
      - no GH_TOKEN / GITHUB_TOKEN / GH_ENTERPRISE_TOKEN is set in the session environment;
      - the session environment carries a GH_CONFIG_DIR at all: unset means gh would use the default
        profile (the human's login), so every `gh` is refused until `env.GH_CONFIG_DIR` is set in
        .claude/settings.local.json and Claude Code is restarted;
      - when the robot's GH_CONFIG_DIR is configured (settings.local.json or settings.json
        `env.GH_CONFIG_DIR`, or SB_ROBOT_GH_CONFIG_DIR), the session environment carries exactly
        that value. A mismatch means the session left the robot identity: every `gh` is refused
        until it is restarted with the configured env.
    `mc-identity-check.sh` (SessionStart) checks the login behind that config dir.

Read/Grep/Glob are checked against the path rules only (the bot's gh config dir, hosts.yml).

Active only in a project that HAS a robot identity (robot_mode). Activation does not rely only on files
the agent can edit: GH_CONFIG_DIR in the hook's own environment, SB_GH_IDENTITY_GUARD=1 (environment or
settings `env`; the wizard writes it when bot_identity is set), a robot GH_CONFIG_DIR in settings, or a
board config with `notifications.bot_identity` other than "auto". An unreadable settings file or board
config counts as configured, and then every `gh` is refused (it cannot be verified). Elsewhere
`gh auth status` and friends stay allowed. Also denies `powershell|pwsh -e|-ec|-enc|-EncodedCommand`.
Hooks filter command text; the server-side rulesets are the real control, these guards are defense in depth.

Fails CLOSED: any exception exits 2 with a message on stderr. The session may run in
bypassPermissions mode, so a guard that cannot decide must block.

Known residual bypasses: a program that reads the environment or hosts.yml on its own
(`python -c "print(os.environ)"`, a script that is not in the command text), a command name
assembled at run time (`& $g auth token`), the credential helper reached through a git alias,
and a robot GH_CONFIG_DIR that is not configured in settings (layer 3 cannot compare then).
"""
import glob
import json
import os
import re
import sys

PATH_RULES = [
    ("GitHub CLI config path", r"GitHub CLI"),
    ("gh-mcbot config dir", r"gh-mcbot"),
    ("hosts.yml", r"hosts\.yml"),
]

SEP = r"(?:^|[;&|(`\n'\"]|\$\()\s*"

CMD_RULES = [
    ("gh auth token/switch/login/logout/refresh/setup-git/git-credential",
     r"\bgh(?:\.exe)?[\"']?\s+[\"']?auth[\"']?\s+[\"']?"
     r"(?:token|switch|login|logout|refresh|setup-git|git-credential)\b"),
    ("gh auth status --show-token",
     r"\bgh(?:\.exe)?[\"']?\s+auth\s+status\b[^\n|;&]*(?:--show-token|\s-[a-z]*t\b)"),
    ("gh alias set/import", r"\bgh(?:\.exe)?\s+alias\s+(?:set|import)\b"),
    ("obfuscated gh auth subcommand",
     r"\A(?=[\s\S]*\bgh\b)[\s\S]*?\bauth['\",\s]{1,6}"
     r"(?:token|switch|login|logout|refresh|setup-git|git-credential)\b"),
    ("GH_CONFIG_DIR mention", r"\bGH_CONFIG_DIR\b"),
    ("GH/GITHUB token env var", r"\b(?:GH|GITHUB)_(?:ENTERPRISE_)?TOKEN\b"),
    ("GH_HOST mention", r"\bGH_HOST\b"),
    ("env -i/-u", r"\benv\b[^\n|;&]*\s-(?:i|u|-ignore-environment|-unset)\b"),
    ("git config credential", r"\bgit\s+config\b[^\n]*\bcredential"),
    ("git credential", r"\bgit\s+credential\b"),
    ("git -c credential", r"\bgit\b[^\n|;&]*\s-c\s*[\"']?credential"),
    ("git-credential helper", r"\bgit-credential"),
    ("GIT_CONFIG_* override",
     r"\bGIT_CONFIG_(?:COUNT|KEY_|VALUE_|PARAMETERS|GLOBAL|SYSTEM|NOSYSTEM)"),
    ("GIT_ASKPASS", r"\bGIT_ASKPASS\b"),
    ("cmdkey", r"\bcmdkey\b"),
    ("credential store API",
     r"\b(?:keyring|CredRead\w*|PasswordVault|Get-StoredCredential|vaultcmd)\b"),
    ("gh Authorization header",
     r"\bgh(?:\.exe)?\b[^\n|;&]*(?:-H|--header)[\s=]*[\"']?\s*authorization\b"),
    ("gh --hostname", r"\bgh(?:\.exe)?\b[^\n|;&]*--hostname\b"),
] + PATH_RULES

# --- layer 2: names built at run time, and environment dumps -------------------------------
NAMES = ["GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_HOST"]
# Any split of a name leaves a prefix of >= 4 chars (or ending in "_") or a suffix of >= 5 chars, so
# these two lists catch every `"part1" + "part2"` build; shorter pieces ("git", "_dir") stay legal.
PREFIXES = sorted({n[:k] for n in NAMES for k in range(3, len(n)) if k >= 4 or n[k - 1] == "_"},
                  key=len, reverse=True)
SUFFIXES = sorted({n[k:] for n in NAMES if n != "GH_HOST" for k in range(1, len(n) - 4)}, key=len, reverse=True)
_P = "|".join(re.escape(p) for p in PREFIXES)
_S = "|".join(re.escape(x) for x in SUFFIXES)
Q = "[\"']"
DYN_RULES = [
    ("name built by concatenation (prefix)", r"%s(?:%s)%s\s*(?:\+|\.|,|-join|-f\b)" % (Q, _P, Q)),
    ("name built by concatenation (suffix)", r"(?:\+|\.|,|-join|-f\b)\s*%s(?:%s)%s" % (Q, _S, Q)),
    ("env-name stem literal", r"%s(?:GH|GITHUB)_%s|\b[A-Za-z_]\w*=(?:GH|GITHUB)_(?=[\s;|&)]|$)" % (Q, Q)),
    ("${!name} indirection", r"\$\{!"),
    # eval as a command word: not inside a path or name such as an eval-fixture directory or tools/eval.py
    ("eval with expansion", r"(?<![\w./-])eval(?![\w./\\-])[^\n]*\$\{?[A-Za-z_]"),
    ("Invoke-Expression with expansion", r"\b(?:Invoke-Expression|iex)\b[^\n]*[$+]"),
    ("printenv", r"\bprintenv\b"),
    ("bare env", SEP + r"(?:sudo\s+|command\s+|exec\s+)?(?:\S*/)?env(?:\.exe)?\s*(?:-[A-Za-z0-9-]+\s*)*(?:$|[;&|)`\n<>'\"])"),
    ("bare set", SEP + r"set\s*(?:$|[;&|)`\n<>'\"])"),
    ("cmd /c set", r"\bcmd(?:\.exe)?\s+/[ckrs]\s+[\"']?set\b"),
    ("export -p / declare -x / compgen -e",
     SEP + r"(?:export\s*(?:-p\b|$|[;&|)`\n])|declare\s+-[A-Za-z]*[xp]|typeset\s+-[A-Za-z]*[xp]|compgen\s+-[ev]\b)"),
    ("/proc environ", r"/proc/(?:self|\d+|\$\$)/environ"),
    ("PowerShell Env: provider",
     r"\b(?:Get-ChildItem|gci|ls|dir|Get-Item|gi|Get-Content|gc|cat|type|Set-Location|cd|sl|Get-PSDrive)"
     r"\s+(?:-\w+\s+)*[\"']?env:"),
    ("PowerShell Env: provider path", r"(?<![\w$:{])env:(?:\\|/)?[A-Za-z_*?\[$]"),
    ("[Environment]::GetEnvironmentVariable(s)", r"\bEnvironment\]\s*::\s*GetEnvironmentVariables?\b"),
    ("dynamic $env: name", r"\$\{?env:\s*\{?\$"),
]

SHELL_TOOLS = ("Bash", "PowerShell")
ENC_RE = re.compile(r"\b(?:pwsh|powershell)(?:\.exe)?\b[^\n;|&]*?\s-(?:e|ec|en[a-z]*)(?=\s|$)", re.IGNORECASE)
ROBOT_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")


def compile_rules(rules):
    return [(name, re.compile(rx, re.IGNORECASE)) for name, rx in rules]


CMD_COMPILED = compile_rules(CMD_RULES + DYN_RULES)
PATH_COMPILED = compile_rules(PATH_RULES)


def first_match(text, compiled):
    for name, rx in compiled:
        if rx.search(text):
            return name
    return None


def check(command):
    """Return the name of the first matching command rule, or None."""
    return first_match(command, CMD_COMPILED)


# --- tokenising, for the positive rule ----------------------------------------------------
def segments(command, ps=False):
    """Split a shell command into simple-command token lists. Quotes are honoured; nothing runs."""
    esc = "`" if ps else "\\"
    out, tokens, cur, has, quote = [], [], "", False, None
    i = 0

    def push_token():
        nonlocal cur, has
        if has:
            tokens.append(cur)
        cur, has = "", False

    while i < len(command):
        c = command[i]
        if quote:
            if c == quote:
                quote = None
            elif c == esc and quote == '"' and i + 1 < len(command):
                i += 1
                cur += command[i]
            else:
                cur += c
        elif c in "'\"":
            quote, has = c, True
        elif c == esc and i + 1 < len(command):
            i += 1
            cur += command[i]
            has = True
        elif c in " \t":
            push_token()
        elif c in ";\n|&()":
            push_token()
            if tokens:
                out.append(tokens)
            tokens = []
        else:
            cur += c
            has = True
        i += 1
    push_token()
    if tokens:
        out.append(tokens)
    return out


def all_segments(command, ps, depth=0):
    """Segments of the command plus those inside quoted strings (`bash -c "gh ..."`)."""
    for tokens in segments(command, ps):
        yield tokens
        if depth < 3:
            for t in tokens:
                if re.search(r"\s", t) and re.search(r"\bgh\b", t):
                    yield from all_segments(t, ps, depth + 1)


def gh_calls(command, ps):
    """[args] for every `gh` invocation (env assignments and wrappers skipped)."""
    calls = []
    for tokens in all_segments(command, ps):
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens = tokens[1:]
        while tokens and tokens[0].lower() in ("sudo", "command", "env", "call", "time", "exec", "nohup"):
            tokens = tokens[1:]
        if tokens and re.sub(r"\.exe$", "", os.path.basename(tokens[0].replace("\\", "/")).lower()) == "gh":
            calls.append(tokens[1:])
    return calls


def norm(p):
    return os.path.normcase(os.path.normpath(os.path.expanduser(p)))


def settings_state(project, env):
    """(robot config dir or None, unparseable settings file or None).
    The robot GH_CONFIG_DIR comes from SB_ROBOT_GH_CONFIG_DIR or settings `env.GH_CONFIG_DIR`."""
    want, broken = env.get("SB_ROBOT_GH_CONFIG_DIR") or None, None
    for name in ("settings.local.json", "settings.json"):
        path = os.path.join(project, ".claude", name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            val = ((data.get("env") if isinstance(data, dict) else None) or {}).get("GH_CONFIG_DIR")
        except (OSError, ValueError, AttributeError):
            broken = broken or name
            continue
        if not want and isinstance(val, str) and val:
            want = val
    return want, broken


def settings_flag(project):
    """True when settings `env.SB_GH_IDENTITY_GUARD` is "1" (the wizard writes it)."""
    for name in ("settings.local.json", "settings.json"):
        try:
            with open(os.path.join(project, ".claude", name), encoding="utf-8") as fh:
                data = json.load(fh)
            if str(((data.get("env") if isinstance(data, dict) else None) or {}).get("SB_GH_IDENTITY_GUARD")) == "1":
                return True
        except (OSError, ValueError, AttributeError):
            continue
    return False


def robot_mode(project, env):
    """True when this project has a robot identity to protect. Activation must not depend only on
    files the agent can edit, so any of these turns it on: GH_CONFIG_DIR in the hook's own environment
    (the session was started on a robot config dir), SB_GH_IDENTITY_GUARD=1 (environment or settings
    env; the wizard writes it when bot_identity is set), a robot GH_CONFIG_DIR in settings, a board config
    whose notifications.bot_identity is not "auto". An unreadable settings file or board config counts
    as configured (fail closed)."""
    if env.get("GH_CONFIG_DIR") or env.get("SB_GH_IDENTITY_GUARD") == "1" or settings_flag(project):
        return True
    want, broken = settings_state(project, env)
    if want or broken:
        return True
    for p in sorted(glob.glob(os.path.join(project, ".claude", "super-board", "configs", "*.json"))):
        try:
            with open(p, encoding="utf-8") as fh:
                v = ((json.load(fh).get("notifications") or {}).get("bot_identity") or "")
        except (OSError, ValueError, AttributeError):
            return True
        if not isinstance(v, str) or (v.strip() and v.strip().lower() != "auto"):
            return True
    return False


# The gh subcommands a board lane uses. Everything else (alias, extension, config, auth, secret,
# ssh-key, gpg-key, codespace, ruleset, gist, ...) is refused. value None = any action; a set = only those.
GH_ALLOWED = {
    "issue": None, "api": None, "project": None, "run": None, "workflow": None, "label": None, "search": None,
    "pr": {"create", "view", "list", "comment", "edit", "ready", "checks", "diff"},
    "repo": {"view"},
    "release": {"view"},
    "auth": {"status"},  # super-build checks it; --show-token is refused below
}


def gh_subcommand_denied(args):
    """None when the gh argument list is a board subcommand, else the reason."""
    positional = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-R", "--repo"):
            i += 1
        elif not a.startswith("-"):
            positional.append(a)
        i += 1
    if not positional:
        return None  # `gh --version`, `gh --help`
    sub, sub2 = (positional + [""])[:2]
    sub = sub.lower()
    if sub not in GH_ALLOWED:
        return "gh %s is not a board subcommand" % sub
    actions = GH_ALLOWED[sub]
    if actions is not None and sub2.lower() not in actions:
        return "gh %s %s is not a board subcommand" % (sub, sub2)
    if sub == "auth" and any(a in ("-t", "--show-token") or a.startswith("--show-token=") for a in args):
        return "gh auth status --show-token prints the token"
    if sub == "pr" and sub2.lower() == "edit" and any(
            a in ("--base", "-B") or a.startswith("--base=") or (a.startswith("-B") and len(a) > 2) for a in args):
        return "gh pr edit with a base change"
    return None


def positive_gh(command, ps, project, env):
    """None when every gh invocation in the command is allowed, else the reason."""
    calls = gh_calls(command, ps)
    if not calls:
        return None
    for args in calls:
        if any(a == "--hostname" or a.startswith("--hostname=") for a in args):
            return "gh --hostname"
        why = gh_subcommand_denied(args)
        if why:
            return why
    marker = os.path.join(project, ".claude", "super-board", "identity-halt")
    if os.path.exists(marker):
        return ("identity mismatch: the SessionStart identity check found a different gh account than the robot "
                "(marker .claude/super-board/identity-halt). Fix the account, restart, and delete the marker "
                "(a matching login at the next SessionStart removes it)")
    for var in ROBOT_VARS:
        if env.get(var):
            return "%s is set in the session environment (it overrides the robot identity)" % var
    want, broken = settings_state(project, env)
    if broken:
        return "settings file %s is unparseable, so the robot GH_CONFIG_DIR cannot be verified" % broken
    have = env.get("GH_CONFIG_DIR")
    if not have:
        # a robot-identity project never falls back to the default gh profile (the human's login)
        return ("GH_CONFIG_DIR is not set in the session environment, so gh would run as the default profile "
                "instead of the robot: set env.GH_CONFIG_DIR in .claude/settings.local.json to the robot's gh "
                "config dir and restart Claude Code")
    if want and norm(have) != norm(want):
        return ("the session's GH_CONFIG_DIR is not the configured robot config dir "
                "(restart Claude Code with the env from .claude/settings.json)")
    return None


# --- tool dispatch ---------------------------------------------------------------------------
def subject(payload):
    """(text, compiled rules) for this tool call, or (None, None) if not covered."""
    tool = payload.get("tool_name") or ""
    ti = payload.get("tool_input") or {}
    if tool in SHELL_TOOLS:
        return ti.get("command") or "", CMD_COMPILED
    if tool == "Read":
        return ti.get("file_path") or "", PATH_COMPILED
    if tool in ("Grep", "Glob"):
        return " ".join(str(ti.get(k) or "") for k in ("path", "glob", "file_path", "pattern")), PATH_COMPILED
    return None, None


def judge(payload, env=None):
    env = os.environ if env is None else env
    text, compiled = subject(payload)
    if not text:
        return None
    project = env.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    if not robot_mode(project, env):
        return None
    rule = first_match(text, compiled)
    if rule:
        return rule
    if payload.get("tool_name") in SHELL_TOOLS and ENC_RE.search(text):
        return "powershell -EncodedCommand (the command cannot be inspected)"
    if payload.get("tool_name") in SHELL_TOOLS:
        return positive_gh(text, payload.get("tool_name") == "PowerShell", project, env)
    return None


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        rule = judge(payload)
        if rule:
            reason = ("Blocked by guard-gh-identity (rule: %s): the session must stay on the "
                      "robot GitHub identity. Do not read, switch or override GitHub "
                      "credentials; ask the operator if the identity looks wrong." % rule)
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                     "permissionDecision": "deny",
                                                     "permissionDecisionReason": reason}}))
    except Exception as e:  # fail CLOSED: a guard that cannot decide blocks
        sys.stderr.write("guard-gh-identity failed closed (%s: %s); the call was blocked. "
                         "Fix the hook or ask the user to run the command with the ! prefix.\n"
                         % (type(e).__name__, e))
        sys.exit(2)


if __name__ == "__main__":
    main()
