#!/usr/bin/env python3
"""PreToolUse(Bash|PowerShell|Read|Grep) guard: refuse to read or pipe secrets.

Stops secrets getting OUT -- `cat .env`, `Get-Content .env`, `source .env.local`,
`grep KEY .env | curl`, the Read tool opening a dotenv file, or a dump of the process
environment (`printenv`, bare `env`, `export -p`, `Get-ChildItem Env:`,
`[Environment]::GetEnvironmentVariable`, `$env:API_KEY`) -- so a key never lands in the
transcript. `.env.example` and friends are deliberately allowed: that file is how an agent
learns which keys exist.

Fails CLOSED: any exception while judging a call exits 2 with a message on stderr
(Claude Code treats exit 2 as a block). A hook that cannot decide must not wave a call
through in a session that runs in bypassPermissions mode.

Also denies `powershell|pwsh -e|-ec|-enc|-EncodedCommand` (a base64 command cannot be inspected). Hooks
filter command text; the server-side rulesets are the real control, these guards are defense in depth.

Known residual bypasses (a regex guard cannot parse every shell): a filename assembled at
run time (`$f = ".en" + "v"`), an interpreter reading the environment (`python -c
"print(os.environ)"`), a secret already copied somewhere under another name, and a secret
file reached through a wildcard this guard does not list. Documented in hooks/README.md.

Adapted from ai-builder-starter-kit's .agents/hooks/guard-secrets.sh.
"""
import json
import re
import sys

B = r"(?:^|[^A-Za-z0-9._-])"   # boundary before a name
E = r"(?:[^A-Za-z0-9._-]|$)"   # boundary after a name
SECRET_RE = re.compile("|".join([
    B + r"\.env" + E,
    r"\.env\.(?:local|production|prod|staging|development|dev|test)" + E,
    B + r"\.e(?:n)?[*?\[]",                       # .e* / .en? wildcards that expand to .env
    B + r"id_(?:rsa|ed25519|ecdsa)" + E,
    B + r"\.npmrc" + E,
    B + r"credentials(?:\.json)?" + E,
    r"service-account[A-Za-z0-9._-]*\.json",
    r"\.pem" + E,
]), re.IGNORECASE)

# Names of secret-looking environment variables.
SENS = (r"(?:[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSW(?:OR)?D|CREDENTIAL)[A-Za-z0-9_]*"
        r"|(?:[A-Za-z0-9]+_)*(?:AUTH|PAT|CONNECTION)(?:_[A-Za-z0-9_]+)?|[A-Za-z0-9_]*(?:DATABASE_URL|DB_URL))")
SEP = r"(?:^|[;&|(`\n'\"]|\$\()\s*"          # start of a simple command
PS_LIST_CMD = (r"\b(?:Get-ChildItem|gci|ls|dir|Get-Item|gi|Get-Content|gc|cat|type|Set-Location|cd|sl"
               r"|Get-PSDrive)\s+(?:-\w+\s+)*[\"']?env:")

# Environment reads. (name, regex); all case-insensitive.
ENV_RULES = [(n, re.compile(rx, re.IGNORECASE)) for n, rx in [
    ("printenv", r"\bprintenv\b"),
    ("bare env (lists the environment)",
     SEP + r"(?:sudo\s+|command\s+|exec\s+)?(?:\S*/)?env(?:\.exe)?\s*(?:-[A-Za-z0-9-]+\s*)*(?:$|[;&|)`\n<>'\"])"),
    ("bare set (lists the environment)", SEP + r"set\s*(?:$|[;&|)`\n<>'\"])"),
    ("cmd /c set", r"\bcmd(?:\.exe)?\s+/[ckrs]\s+[\"']?set\b"),
    ("export -p / declare -x / compgen -e",
     SEP + r"(?:export\s*(?:-p\b|$|[;&|)`\n])|declare\s+-[A-Za-z]*[xp]|typeset\s+-[A-Za-z]*[xp]|compgen\s+-[ev]\b)"),
    ("/proc environ", r"/proc/(?:self|\d+|\$\$)/environ"),
    ("PowerShell Env: provider", PS_LIST_CMD),
    ("PowerShell Env: provider path", r"(?<![\w$:{])env:(?:\\|/)?[A-Za-z_*?\[$]"),
    ("[Environment]::GetEnvironmentVariable(s)", r"\bEnvironment\]\s*::\s*GetEnvironmentVariables?\b"),
    ("sensitive $env: read", r"\$\{?env:\s*\{?" + SENS + r"(?![A-Za-z0-9])"),
    ("dynamic $env: name", r"\$\{?env:\s*\{?\$"),
    ("echo of a secret-looking variable",
     r"\b(?:echo|printf|Write-Host|Write-Output|Write-Error|Out-String)\b[^\n;|&]*\$\{?" + SENS + r"\b"),
]]

# The one sanctioned way to ask "is KEY set?": super-board-env-check.sh prints
# present / empty / missing per key and never a value. Allowed only as a single
# plain call -- no pipe, redirect, chain or substitution can ride along.
ENV_CHECK = re.compile(
    r"^\s*(?:bash\s+|sh\s+)?(?:\S*/)?super-board-env-check\.sh"
    r"(?:\s+[A-Za-z0-9_./=-]+)*\s*$")

REASON = ("Blocked by guard-secrets (%s): this touches a secret file or the process environment. "
          "Read the checked-in example env file (.env.example) for the key names, use "
          "`bash .claude/bin/super-board-env-check.sh KEY...` to ask whether a key is set, or ask "
          "the user to run the command themselves with the ! prefix.")
SHELL_TOOLS = ("Bash", "PowerShell")
ENC_RE = re.compile(r"\b(?:pwsh|powershell)(?:\.exe)?\b[^\n;|&]*?\s-(?:e|ec|en[a-z]*)(?=\s|$)", re.IGNORECASE)


def subject(payload):
    """The text this tool call can expose: a shell command or a file path."""
    tool = payload.get("tool_name") or ""
    ti = payload.get("tool_input") or {}
    if tool in SHELL_TOOLS:
        return ti.get("command") or ""
    if tool == "Read":
        return ti.get("file_path") or ""
    if tool == "Grep":
        return " ".join(str(ti.get(k) or "") for k in ("path", "glob"))
    return ""


def judge(payload):
    """None when allowed, else the rule that blocks it."""
    text = subject(payload)
    tool = payload.get("tool_name")
    if tool in SHELL_TOOLS and ENV_CHECK.match(text):
        return None
    if not text:
        return None
    if SECRET_RE.search(text):
        return "secret file name"
    if tool in SHELL_TOOLS and ENC_RE.search(text):
        return "powershell -EncodedCommand (the command cannot be inspected)"
    if tool in SHELL_TOOLS:
        for name, rx in ENV_RULES:
            if rx.search(text):
                return name
    return None


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        rule = judge(payload)
        if rule:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                     "permissionDecision": "deny",
                                                     "permissionDecisionReason": REASON % rule}}))
    except Exception as e:  # fail CLOSED: a guard that cannot decide blocks
        sys.stderr.write("guard-secrets failed closed (%s: %s); the call was blocked. "
                         "Fix the hook or ask the user to run the command with the ! prefix.\n"
                         % (type(e).__name__, e))
        sys.exit(2)


if __name__ == "__main__":
    main()
