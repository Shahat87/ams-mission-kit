#!/usr/bin/env python3
"""Same-host collector serialization and private evidence receipts; no scheduler.

The lock is inherited by the real shell filer and its synchronous children.
A pending write survives interruption and blocks retry until explicitly reconciled.
"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import time

spec = importlib.util.spec_from_file_location('github_read', Path(__file__).with_name('super-board-github-read.py'))
gh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gh)


def paths(repo):
    root = Path(os.environ.get('SB_COLLECT_STATE_DIR') or gh.halt_path().parent / 'collect')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    ignore = root / '.gitignore'
    if not ignore.exists():
        ignore.write_text('*\n')
    key = hashlib.sha256(repo.lower().encode()).hexdigest()
    return key, root / (key + '.lock'), root / (key + '.json')


def load(path):
    if not path.exists():
        return {'receipts': {}}
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or not isinstance(value.get('receipts'), dict):
        raise ValueError('collector state is malformed; inspect it before retrying')
    return value


def save(path, value):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as stream:
        os.chmod(tmp, 0o600)
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo', required=True)
    p.add_argument('--lock', action='store_true')
    p.add_argument('--action', choices=['seen','begin','success','known-failure','reconcile'])
    p.add_argument('--fingerprint', default='')
    p.add_argument('--digest', default='')
    p.add_argument('--issue', default='')
    p.add_argument('command', nargs=argparse.REMAINDER)
    a = p.parse_args()
    key, lock_path, state_path = paths(a.repo)
    args = a.command[1:] if a.command[:1] == ['--'] else a.command
    try:
        if a.lock:
            limit = float(os.environ.get('SB_COLLECT_LOCK_TIMEOUT', '15'))
            if not math.isfinite(limit) or not 0 <= limit <= 300:
                raise ValueError('SB_COLLECT_LOCK_TIMEOUT must be between 0 and 300 seconds')
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            deadline = time.monotonic() + limit
            while True:
                gh.check_halt()
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        print('collector busy; no writes attempted', file=sys.stderr)
                        return 4
                    time.sleep(.05)
            state = load(state_path)
            if state.get('pending'):
                gh.halt('Collector write outcome unresolved; reconcile pending receipt before retrying', 1)
            if not args:
                raise ValueError('--lock needs the existing filer command')
            os.set_inheritable(fd, True)
            env = dict(os.environ, SB_COLLECT_LOCK_HELD=key, SB_COLLECT_LOCK_FD=str(fd))
            os.execvpe(args[0], args, env)
        elif a.action:
            fd = int(os.environ.get('SB_COLLECT_LOCK_FD', '-1'))
            held = os.environ.get('SB_COLLECT_LOCK_HELD') == key
            if held:
                held = os.fstat(fd).st_ino == lock_path.stat().st_ino
            # Explicit manual reconciliation acquires the same lock; never clears a live writer.
            stream = None
            if a.action == 'reconcile' and not held:
                stream = lock_path.open('a+')
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return 4
                held = True
            if not held:
                raise ValueError('collector receipt operations require the held repository lock')
            state = load(state_path)
            if a.action == 'seen':
                receipt = state['receipts'].get(a.fingerprint, {})
                print('unchanged' if receipt.get('digest') == a.digest and receipt.get('issue') == a.issue else 'changed')
            elif a.action == 'begin':
                state['pending'] = {'fingerprint': a.fingerprint, 'digest': a.digest, 'issue': a.issue, 'at': int(time.time())}
                save(state_path, state)
            elif a.action == 'success':
                if not a.issue.isdigit():
                    raise ValueError('successful receipt requires an issue number')
                state['receipts'][a.fingerprint] = {'digest': a.digest, 'issue': a.issue, 'at': int(time.time())}
                state.pop('pending', None)
                save(state_path, state)
            elif a.action in ('known-failure','reconcile'):
                if state.get('pending', {}).get('fingerprint') != a.fingerprint:
                    raise ValueError('pending fingerprint differs; inspect state before resolving')
                state.pop('pending', None)
                save(state_path, state)
            return 0
        raise ValueError('choose --lock or --action')
    except gh.ReadHalted as exc:
        print(str(exc), file=sys.stderr)
        return 79
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 70

if __name__ == '__main__':
    sys.exit(main())
