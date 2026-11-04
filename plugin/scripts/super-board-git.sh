#!/usr/bin/env bash
# super-board-git.sh — run git with every config-driven program switched off.
#
# Usage:
#   super-board-git.sh [--robot|--robot-read] -- <git args...>
#     e.g. super-board-git.sh -- -C . status --porcelain
#          super-board-git.sh --robot -- -C DIR push origin FULLSHA:refs/heads/feature-x
#   --robot  exactly one argv shape is accepted: -C DIR push origin SHA:refs/heads/NAME (a full sha, a plain
#            branch name that is not HEAD, main, master, staging or listed in the comma-separated
#            SB_PROTECTED_BRANCHES). It pushes over https as the robot: GIT_ALLOW_PROTOCOL=https, GH_CONFIG_DIR
#            taken from SB_ROBOT_GH_CONFIG_DIR (else GH_CONFIG_DIR), credential helper = the absolute path of
#            `gh` running `auth git-credential`. Without --robot there is no credential helper and only the
#            read/object subcommands below are allowed; -C DIR is the only global option.
#   --robot-read  the read twin of --robot, for a private https origin: same env, same credential helper, same gh requirements,
#            and exactly three argv shapes: -C DIR ls-remote origin refs/heads/NAME | -C DIR fetch origin SHA |
#            -C DIR fetch origin refs/heads/NAME. NAME follows the same name rules, but a protected name is fine
#            (a read is harmless). Anything else, push included, exits 64.
#
# Exit codes: 0 ok (otherwise git's own exit code, because git is exec'd) | 64 forbidden git arg, subcommand or
# bad usage | 66 --robot without a gh config dir or without a usable absolute gh path.
#
# Threat model: this runs on repos that may hold objects a tool wrote (lane output). A hostile
# .gitattributes, a global/system gitconfig, an LFS filter, a hook or an external diff driver must
# never get to run a program. So: no global or system config, no hooks, no fsmonitor, no external
# diff or textconv, no attributes file, no LFS filters, no terminal prompts, and no way for the
# caller to turn any of that back on (-c, --config-env, --exec-path, --upload-pack, --receive-pack,
# -O, --open-files-in-pager and --output are refused).
set -euo pipefail

case "$(uname -s 2>/dev/null)" in
  # lowercase: git for Windows rejects GIT_CONFIG_GLOBAL=NUL ("unable to access 'NUL'") but accepts nul.
  MINGW*|MSYS*|CYGWIN*) NULLDEV=nul ;;
  *) NULLDEV=/dev/null ;;
esac

ROBOT=0
ROBOT_READ=0
if [ "${1:-}" = "--robot" ]; then ROBOT=1; shift
elif [ "${1:-}" = "--robot-read" ]; then ROBOT_READ=1; shift; fi
if [ "${1:-}" != "--" ]; then
  echo "usage: super-board-git.sh [--robot|--robot-read] -- <git args...>" >&2
  exit 64
fi
shift
if [ "$#" -eq 0 ]; then
  echo "super-board-git.sh: no git arguments" >&2
  exit 64
fi

ARGS=("$@")

refuse() {
  echo "super-board-git.sh: refused: $1" >&2
  exit 64
}

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# Global options: only `-C DIR` pairs. The subcommand is the first word after them.
i=0
while [ "$i" -lt "${#ARGS[@]}" ] && [ "${ARGS[$i]}" = "-C" ]; do
  [ $((i + 1)) -lt "${#ARGS[@]}" ] || refuse "-C needs a directory"
  i=$((i + 2))
done
SUBIDX=$i
[ "$SUBIDX" -lt "${#ARGS[@]}" ] || refuse "no git subcommand"
SUB="${ARGS[$SUBIDX]}"
case "$SUB" in
  -*) refuse "global git option $SUB (only -C DIR is allowed)" ;;
esac

for a in "${ARGS[@]}"; do
  case "$a" in
    -c|--config-env*|--exec*|--upload*|--receive*|--output*|-O*|--open-files-in-pager*)
      refuse "git argument $a" ;;
    -S*|--gpg-sign*|--show-signature)
      refuse "git argument $a (signature checks can run gpg)" ;;
    --ext-diff*|--textconv*)
      refuse "git argument $a (it would run a diff driver)" ;;
    --format=*%G*|--pretty=*%G*)
      refuse "git format string with %G (it runs gpg)" ;;
  esac
done

NSUB=$(( ${#ARGS[@]} - SUBIDX - 1 ))   # arguments after the subcommand
SUBARGS=("${ARGS[@]:$((SUBIDX + 1))}")

# BEGIN ROBOT SHAPE
if [ "$ROBOT" -eq 1 ]; then
  # exactly: -C DIR push origin SHA:refs/heads/NAME
  [ "${#ARGS[@]}" -eq 5 ] && [ "${ARGS[0]}" = "-C" ] && [ "$SUB" = "push" ] && [ "${ARGS[3]}" = "origin" ] \
    || refuse "--robot accepts only: -C DIR push origin SHA:refs/heads/NAME"
  SPEC="${ARGS[4]}"
  SPEC_RE='^([0-9A-Fa-f]{40}|[0-9A-Fa-f]{64}):refs/heads/(.+)$'
  [[ "$SPEC" =~ $SPEC_RE ]] || refuse "--robot refspec must be SHA:refs/heads/NAME with a full sha"
  NAME="${BASH_REMATCH[2]}"
  NAME_RE='^[A-Za-z0-9._/-]+$'
  [[ "$NAME" =~ $NAME_RE ]] || refuse "branch name"
  case "$NAME" in
    -*|/*|*..*|*//*) refuse "branch name $NAME" ;;
  esac
  norm() {   # trim, lowercase, strip refs/remotes/origin/, refs/heads/, origin/ (repeatedly)
    local n; n=$(lower "$1")
    n="${n#"${n%%[![:space:]]*}"}"; n="${n%"${n##*[![:space:]]}"}"
    local again=1
    while [ "$again" -eq 1 ]; do
      again=0
      case "$n" in
        refs/remotes/origin/*) n="${n#refs/remotes/origin/}"; again=1 ;;
        refs/heads/*) n="${n#refs/heads/}"; again=1 ;;
        origin/*) n="${n#origin/}"; again=1 ;;
      esac
    done
    printf '%s' "$n"
  }
  N=$(norm "$NAME")
  PROT=(head main master staging)
  IFS=',' read -r -a EXTRA <<< "${SB_PROTECTED_BRANCHES:-}"
  for e in ${EXTRA[@]+"${EXTRA[@]}"}; do
    pe=$(norm "$e")
    [ -z "$pe" ] || PROT+=("$pe")
  done
  for pn in "${PROT[@]}"; do
    [ "$N" != "$pn" ] || refuse "push to protected branch $NAME"
  done
fi
if [ "$ROBOT_READ" -eq 1 ]; then
  # exactly: -C DIR ls-remote origin refs/heads/NAME | -C DIR fetch origin SHA | -C DIR fetch origin refs/heads/NAME
  [ "${#ARGS[@]}" -eq 5 ] && [ "${ARGS[0]}" = "-C" ] && [ "${ARGS[3]}" = "origin" ]     && { [ "$SUB" = "ls-remote" ] || [ "$SUB" = "fetch" ]; }     || refuse "--robot-read accepts only: -C DIR ls-remote origin refs/heads/NAME | fetch origin SHA | fetch origin refs/heads/NAME"
  RSPEC="${ARGS[4]}"
  SHA_ONLY_RE='^([0-9A-Fa-f]{40}|[0-9A-Fa-f]{64})$'
  REF_RE='^refs/heads/(.+)$'
  if [[ "$RSPEC" =~ $SHA_ONLY_RE ]]; then
    [ "$SUB" = "fetch" ] || refuse "--robot-read ls-remote needs refs/heads/NAME"
  elif [[ "$RSPEC" =~ $REF_RE ]]; then
    RNAME="${BASH_REMATCH[1]}"
    RNAME_RE='^[A-Za-z0-9._/-]+$'
    [[ "$RNAME" =~ $RNAME_RE ]] || refuse "branch name"
    case "$RNAME" in
      -*|/*|*..*|*//*) refuse "branch name $RNAME" ;;
    esac
  else
    refuse "--robot-read ref must be a full sha (fetch) or refs/heads/NAME"
  fi
fi
# END ROBOT SHAPE

if [ "$ROBOT" -eq 0 ] && [ "$ROBOT_READ" -eq 0 ]; then
  case "$SUB" in
    read-tree|write-tree|commit-tree|rev-parse|log|show|diff|merge-base|merge-tree|ls-remote|status|cat-file|ls-tree|rev-list|for-each-ref) ;;
    fetch) ;;
    apply) : ;;
    worktree) ;;
    *) refuse "git subcommand $SUB" ;;
  esac
  case "$SUB" in
    apply)
      has=0
      for a in ${SUBARGS[@]+"${SUBARGS[@]}"}; do [ "$a" != "--cached" ] || has=1; done
      [ "$has" -eq 1 ] || refuse "git apply without --cached" ;;
    worktree)
      [ "${SUBARGS[0]:-}" = "list" ] || refuse "git worktree other than list" ;;
    fetch)
      for a in ${SUBARGS[@]+"${SUBARGS[@]}"}; do
        case "$a" in -u|-[A-Za-z]*u*) refuse "git fetch $a" ;; esac
      done ;;
    read-tree)
      for a in ${SUBARGS[@]+"${SUBARGS[@]}"}; do
        case "$a" in -u|--reset|-m|--prefix*|-[A-Za-z]*u*) refuse "git read-tree $a (it would touch a working tree)" ;; esac
      done ;;
    cat-file)
      for a in ${SUBARGS[@]+"${SUBARGS[@]}"}; do
        case "$a" in --textconv|--filters) refuse "git cat-file $a" ;; esac
      done ;;
  esac
fi

# Scrub the environment: only the GIT_* this wrapper sets itself and the ones objects.py passes on purpose survive.
for v in $(compgen -v | grep '^GIT_' || true); do
  case "$v" in
    GIT_INDEX_FILE|GIT_AUTHOR_NAME|GIT_AUTHOR_EMAIL|GIT_COMMITTER_NAME|GIT_COMMITTER_EMAIL) ;;
    *) unset "$v" ;;
  esac
done
unset GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GH_HOST SSH_ASKPASS GIT_ASKPASS 2>/dev/null || true
export GIT_PAGER=cat

# BEGIN OVERRIDES — copy of OVERRIDES at scripts/super-board-lane-delegate.py:78-80;
# tests/test-git-wrapper.sh parses both files and asserts the two lists match.
OVERRIDES=("core.fsmonitor=false" "core.hooksPath=$NULLDEV" "core.attributesFile=$NULLDEV" "diff.external=" "color.ui=never" "core.quotePath=false")
# END OVERRIDES

CFG=()
for o in "${OVERRIDES[@]}"; do CFG+=(-c "$o"); done
CFG+=(-c "filter.lfs.process=" -c "filter.lfs.smudge=" -c "filter.lfs.clean=" -c "filter.lfs.required=false")
CFG+=(-c "core.askPass=" -c "core.sshCommand=false" -c "core.pager=cat")
CFG+=(-c "log.showSignature=false" -c "gpg.program=false" -c "gpg.ssh.program=false" -c "gpg.x509.program=false")
CFG+=(-c "fetch.recurseSubmodules=false" -c "gc.auto=0" -c "maintenance.auto=false" -c "protocol.ext.allow=never")

export GIT_CONFIG_GLOBAL="$NULLDEV"
export GIT_CONFIG_NOSYSTEM=1
export GIT_TERMINAL_PROMPT=0

# Repo-local program-running keys: a hostile .gitattributes can only NAME a driver, the program lives in this repo's config.
# Read every key (-z, any subsection name) and blank it through GIT_CONFIG_COUNT/KEY/VALUE, which take the key verbatim
# (no "=" split, so names like x+y, "a b" and k=v work). Exported after the env scrub above. `git config` only reads.
DRIVER_RE='^(filter|diff|merge)\..+\.(clean|smudge|process|textconv|command|driver|required)$'
PROG_RE='^(core\.alternaterefscommand|uploadpack\.packobjectshook)$'
REMOTE_PROG_RE='^remote\..+\.(uploadpack|receivepack)$'
PUSHURL_RE='^remote\.origin\.pushurl$'
HTTPCFG_RE='^(http\..+|remote\.origin\.(proxy|proxyauthmethod))$'
REWRITE_RE='^url\..+\.(insteadof|pushinsteadof)$'
CKEYS=(); CVALS=(); REWRITES=0; REMOTE_UP=0; REMOTE_RP=0; PUSHURL=0; HTTPCFG=0
while IFS= read -r -d '' key; do
  if [[ "$key" =~ $DRIVER_RE ]]; then
    CKEYS+=("$key")
    case "$key" in *.required) CVALS+=("false") ;; *) CVALS+=("") ;; esac
  elif [[ "$key" =~ $PROG_RE ]]; then
    CKEYS+=("$key"); CVALS+=("")
  elif [[ "$key" =~ $REMOTE_PROG_RE ]]; then
    # git keeps the FIRST remote.<name>.uploadpack/receivepack it reads ("more than one given, using the first"), so a
    # command-scope blank cannot override the repo's value. Refuse the commands that would run it instead.
    case "$key" in *.uploadpack) REMOTE_UP=1 ;; *) REMOTE_RP=1 ;; esac
  elif [[ "$key" =~ $REWRITE_RE ]]; then
    REWRITES=1
  elif [[ "$key" =~ $PUSHURL_RE ]]; then
    PUSHURL=1
  elif [[ "$key" =~ $HTTPCFG_RE ]]; then
    HTTPCFG=1
  fi
done < <(git "${ARGS[@]:0:$SUBIDX}" config -z --name-only --get-regexp '^(filter|diff|merge|remote|core|uploadpack|url|http)\.' 2>/dev/null || true)
case "$SUB" in
  fetch|ls-remote|pull) [ "$REMOTE_UP" -eq 0 ] || refuse "git $SUB with remote.*.uploadpack in the repo config (it would run that program)" ;;
  push) [ "$REMOTE_RP" -eq 0 ] || refuse "git push with remote.*.receivepack in the repo config (it would run that program)" ;;
esac
if [ "${#CKEYS[@]}" -gt 0 ]; then
  export GIT_CONFIG_COUNT="${#CKEYS[@]}"
  for ((j = 0; j < ${#CKEYS[@]}; j++)); do
    export "GIT_CONFIG_KEY_$j=${CKEYS[$j]}" "GIT_CONFIG_VALUE_$j=${CVALS[$j]}"
  done
fi

if [ "$ROBOT" -eq 1 ] || [ "$ROBOT_READ" -eq 1 ]; then
  # an empty remote.origin.pushurl is read as a URL (push dies), and a command-scope value only ADDS to the repo's list, so a
  # repo-defined pushurl cannot be overridden: refuse it, like url.*.insteadOf. Same for http.* keys and remote.origin.proxy:
  # a blank command-scope http key breaks the transport and loses to http.<url>.* repo keys.
  [ "$PUSHURL" -eq 0 ] || refuse "--robot with remote.origin.pushurl in the repo config"
  [ "$REWRITES" -eq 0 ] || refuse "--robot with a url.*.insteadOf / pushInsteadOf key in the repo config"
  [ "$HTTPCFG" -eq 0 ] || refuse "--robot with an http.* or remote.origin.proxy key in the repo config"
  unset HTTPS_PROXY HTTP_PROXY https_proxy http_proxy ALL_PROXY all_proxy SSL_CERT_FILE SSL_CERT_DIR GIT_SSL_CAINFO GIT_SSL_NO_VERIFY CURL_CA_BUNDLE
  CFG+=(-c "push.followTags=false" -c "push.recurseSubmodules=no" -c "http.sslVerify=true")
  export GIT_ALLOW_PROTOCOL=https
  GHDIR="${SB_ROBOT_GH_CONFIG_DIR:-${GH_CONFIG_DIR:-}}"
  if [ -z "$GHDIR" ]; then
    echo "super-board-git.sh: --robot/--robot-read needs SB_ROBOT_GH_CONFIG_DIR or GH_CONFIG_DIR" >&2
    exit 66
  fi
  GH_ABS=$(type -P gh 2>/dev/null || true)
  case "$GH_ABS" in
    /*) ;;
    *) echo "super-board-git.sh: --robot/--robot-read needs gh as an absolute path on PATH" >&2; exit 66 ;;
  esac
  case "$GH_ABS" in
    *"'"*) echo "super-board-git.sh: gh path contains a quote" >&2; exit 66 ;;
  esac
  export GH_CONFIG_DIR="$GHDIR"
  CFG+=(-c "credential.helper=" -c "credential.helper=!'$GH_ABS' auth git-credential")
else
  export GIT_ALLOW_PROTOCOL=file:https:ssh
  CFG+=(-c "credential.helper=")
fi

case "$SUB" in
  diff|show|log)
    exec git "${CFG[@]}" "${ARGS[@]:0:$((SUBIDX + 1))}" --no-ext-diff --no-textconv ${SUBARGS[@]+"${SUBARGS[@]}"} ;;
esac
exec git "${CFG[@]}" "${ARGS[@]}"
