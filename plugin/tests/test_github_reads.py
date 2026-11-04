#!/usr/bin/env python3
"""Offline CLI contract: bounded required reads, validated evidence, local stop."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'scripts/super-board-github-read.py'


class RequiredReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / 'calls'
        self.halt = self.root / 'halt.json'
        gh = self.root / 'gh'
        gh.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
p = pathlib.Path(os.environ['READ_CALLS'])
n = int(p.read_text()) if p.exists() else 0
p.write_text(str(n + 1))
if os.environ.get('READ_TOUCH'):
    pathlib.Path(os.environ['READ_TOUCH']).write_text('{}')
responses = json.loads(os.environ['READ_RESPONSES'])
r = responses[min(n, len(responses)-1)]
print(r.get('out', ''), end='')
print(r.get('err', ''), file=sys.stderr)
sys.exit(r.get('rc', 0))
''')
        gh.chmod(0o755)
        self.env = dict(os.environ, PATH=f'{self.root}:{os.environ["PATH"]}',
                        READ_CALLS=str(self.log), SB_GITHUB_HALT_FILE=str(self.halt),
                        SB_GITHUB_RETRY_DELAY='0')

    def tearDown(self):
        self.temp.cleanup()

    def read(self, responses, kind='json', command=None):
        self.env['READ_RESPONSES'] = json.dumps(responses)
        return subprocess.run(['python3', str(HELPER), '--kind', kind, '--',
                               *(command or ['api', 'rate_limit'])],
                              env=self.env, text=True, capture_output=True, cwd=self.root)

    def test_three_failed_attempts_halt_without_fabricated_output(self):
        r = self.read([{'rc': 1, 'err': 'HTTP 503 unavailable'}])
        self.assertEqual(r.returncode, 79, r.stderr)
        self.assertEqual(self.log.read_text(), '3')
        self.assertEqual(r.stdout, '')
        self.assertEqual(json.loads(self.halt.read_text())['attempts'], 3)
        again = self.read([{'out': '{}'}])
        self.assertEqual(again.returncode, 79)
        self.assertEqual(self.log.read_text(), '3', 'halted run must not read again')

    def test_recovery_resets_each_logical_read(self):
        response = [{'rc': 1}, {'out': '{}'}, {'rc': 1}, {'rc': 1}, {'out': '{}'}]
        self.assertEqual(self.read(response).returncode, 0)
        self.assertEqual(self.read(response).returncode, 0)
        self.assertEqual(self.log.read_text(), '5')
        self.assertFalse(self.halt.exists())

    def test_permanent_graphql_error_is_not_retried(self):
        self.env['SB_GITHUB_READ_STRICT'] = '1'  # fixed-command scripts: a bad query is a plugin bug
        r = self.read([{'rc': 1, 'err': 'GraphQL: Cannot query field wrong on type Query'}],
                      command=['api', 'graphql', '-f', 'query=query { wrong }'])
        self.assertEqual(r.returncode, 79)
        self.assertEqual(self.log.read_text(), '1')

    def test_successful_exit_with_malformed_or_partial_payload_halts(self):
        for out, kind in [('{"resources":{"graphql":null,"core":null}}', 'quota'), ('[{}]', 'prs-open'), ('{"items":[{"id":"I","content":{}}],"totalCount":1}', 'items'), ('{"items":[]}', 'items'),
                          ('{"items":[],"totalCount":1}', 'items'),
                          ('{"data":{},"errors":[{"message":"partial"}]}', 'json')]:
            with self.subTest(out=out):
                self.halt.unlink(missing_ok=True)
                self.log.unlink(missing_ok=True)
                r = self.read([{'out': out}], kind)
                self.assertEqual(r.returncode, 79)
                self.assertEqual(self.log.read_text(), '3')
                self.assertEqual(r.stdout, '')

    QUERY_SHAPE_ERR = "GraphQL: Field 'body' doesn't exist on type 'PullRequestReviewThread' (undefinedField)"
    BAD_QUERY = ['api', 'graphql', '-f', 'query=query { repository { x } }']

    def test_graphql_validation_error_is_caller_error_not_halt(self):
        for err in (self.QUERY_SHAPE_ERR, 'GraphQL: Cannot query field "body" on type "X"',
                    'GRAPHQL_VALIDATION_FAILED', 'Parse error on "}" (Syntax Error)'):
            with self.subTest(err=err):
                self.log.unlink(missing_ok=True)
                self.halt.unlink(missing_ok=True)
                r = self.read([{'rc': 1, 'err': err}], command=self.BAD_QUERY)
                self.assertEqual(r.returncode, 64, r.stderr)
                self.assertEqual(self.log.read_text(), '1', 'caller error: no retry')
                self.assertFalse(self.halt.exists(), 'caller error must not halt other lanes')
                self.assertIn('[github-read] malformed query (caller error, not a GitHub failure): ', r.stderr)
                self.assertIn('; fix the query and retry', r.stderr)
                self.assertEqual(r.stdout, '')

    def test_query_shape_text_in_stdout_is_not_a_caller_error(self):
        # --paginate stdout can hold earlier pages' issue bodies mentioning these words.
        r = self.read([{'rc': 1, 'err': 'HTTP 503 unavailable', 'out': 'body says undefinedField Syntax Error'}],
                      command=self.BAD_QUERY)
        self.assertEqual(r.returncode, 79, r.stderr)
        self.assertEqual(self.log.read_text(), '3', 'normal retry path, not caller error')

    def test_access_error_with_shape_text_halts(self):
        r = self.read([{'rc': 1, 'err': 'HTTP 401: Bad credentials ' + self.QUERY_SHAPE_ERR}],
                      command=self.BAD_QUERY)
        self.assertEqual(r.returncode, 79, r.stderr)
        self.assertTrue(self.halt.exists())

    def test_shape_error_in_payload_errors_with_rc0_is_caller_error(self):
        out = json.dumps({'errors': [{'type': 'undefinedField', 'message': "Field 'body' doesn't exist"}]})
        r = self.read([{'rc': 0, 'out': out}], command=self.BAD_QUERY)
        self.assertEqual(r.returncode, 64, r.stderr)
        self.assertFalse(self.halt.exists())
        self.assertEqual(self.log.read_text(), '1')

    def test_issue_history_is_strict_even_when_env_unset(self):
        self.env['READ_RESPONSES'] = json.dumps([{'rc': 1, 'err': self.QUERY_SHAPE_ERR}])
        r = subprocess.run(['python3', str(HELPER), '--issue-history', 'o/r'],
                           env=self.env, text=True, capture_output=True, cwd=self.root)
        self.assertEqual(r.returncode, 79, r.stderr)
        self.assertTrue(self.halt.exists())

    def test_graphql_validation_error_still_halts_when_strict(self):
        self.env['SB_GITHUB_READ_STRICT'] = '1'
        r = self.read([{'rc': 1, 'err': self.QUERY_SHAPE_ERR}], command=self.BAD_QUERY)
        self.assertEqual(r.returncode, 79, r.stderr)
        self.assertTrue(self.halt.exists())
        self.assertEqual(self.log.read_text(), '1')

    def test_bad_credentials_still_halts_non_strict(self):
        r = self.read([{'rc': 1, 'err': 'HTTP 401: Bad credentials'}], command=self.BAD_QUERY)
        self.assertEqual(r.returncode, 79, r.stderr)
        self.assertTrue(self.halt.exists())
        self.assertEqual(self.log.read_text(), '1')

    def test_rate_limit_text_is_never_a_query_shape_error(self):
        r = self.read([{'rc': 1, 'err': 'secondary rate limit; Cannot query field'}], command=self.BAD_QUERY)
        self.assertEqual(r.returncode, 79, r.stderr)
        self.assertEqual(self.log.read_text(), '3', 'rate limit retries then halts')

    def test_agent_shape_mismatch_fails_only_that_read(self):
        # #32: a malformed agent read is not an outage; zero comments is a valid read.
        r = self.read([{'out': '[[]]'}], 'comments')
        self.assertEqual((r.returncode, r.stdout), (0, '[[]]'), r.stderr)
        for out, kind in [('not json', 'json'), ('{"comments":[]}', 'issue'), ('[]', 'comments'),
                          ('0123456789abcdef0123456789abcdef01234567', 'head')]:
            with self.subTest(out=out):
                self.log.unlink(missing_ok=True)
                r = self.read([{'out': out}], kind)
                self.assertEqual(r.returncode, 64, r.stderr)
                self.assertEqual(r.stdout, '')
                self.assertIn(f'Valid form: --kind {kind} -- ', r.stderr)
                self.assertFalse(self.halt.exists(), 'shape mismatch must not halt other lanes')
        self.env['SB_GITHUB_READ_STRICT'] = '1'
        self.assertEqual(self.read([{'out': 'not json'}]).returncode, 79, 'fixed-command scripts still halt')

    def test_no_mutation_can_be_retried(self):
        for command in [['pr', 'merge', '1'], ['issue', 'comment', '1'],
                        ['api', 'graphql', '-f', 'query=mutation { deleteIssue }'],
                        ['api', 'graphql', '-f', 'query=query Safe { viewer { login } } mutation Unsafe { deleteIssue }', '-f', 'operationName=Unsafe'],
                        ['api', 'repos/x/y/issues', '-XPOST'],
                        ['api', 'repos/x/y/issues', '-f', 'title=hi']]:
            with self.subTest(command=command):
                r = self.read([{'out': '{}'}], command=command)
                self.assertEqual(r.returncode, 64)
                self.assertFalse(self.log.exists())

    def test_clean_empty_board_is_valid(self):
        r = self.read([{'out': '{"items":[],"totalCount":0}'}], 'items')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.log.read_text(), '1')

    def test_incomplete_dependency_pages_are_not_a_graph(self):
        page = {'data': {'repository': {'issues': {
            'nodes': [], 'pageInfo': {'hasNextPage': True, 'endCursor': 'next'}}}}}
        r = self.read([{'out': json.dumps([page])}], 'issues')
        self.assertEqual(r.returncode, 79)
        self.assertEqual(self.log.read_text(), '3')

    def test_failed_recovery_keeps_original_reason(self):
        self.assertEqual(self.read([{'rc': 1}]).returncode, 79)
        original = self.halt.read_text()
        result = subprocess.run(['python3', str(HELPER), '--resume'], env=self.env, capture_output=True)
        self.assertEqual(result.returncode, 79)
        self.assertEqual(self.halt.read_text(), original)

    def test_legacy_read_failure_preserves_snapshot_and_prevents_dispatch(self):
        self.env['READ_RESPONSES'] = json.dumps([{'rc': 1}])
        script = """SB_LIB_ONLY=1 . "$1"
set +e
PROJECT_OWNER=acme PROJECT_NUMBER=1
PROJECT_ITEMS_JSON='{"items":[{"id":"preserved"}]}'
fetch_project_items; rc=$?
printf '%s %s\n' "$rc" "$PROJECT_ITEMS_JSON"
dispatch_lane build 7
"""
        result = subprocess.run(['bash', '-c', script, 'test', str(ROOT / 'scripts/super-board-run.sh')],
                                cwd=self.root, env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 79, result.stderr)
        self.assertIn('79 {"items":[{"id":"preserved"}]}', result.stdout)
        self.assertEqual(self.log.read_text(), '3', 'no claim/worker or extra API call after halt')

    def test_unknown_issue_state_never_means_open(self):
        self.env['READ_RESPONSES'] = json.dumps([{'rc': 1}])
        script = 'SB_LIB_ONLY=1 . "$1"; issue_is_open 7'
        result = subprocess.run(['bash', '-c', script, 'test', str(ROOT / 'scripts/super-board-run.sh')],
                                cwd=self.root, env=self.env, capture_output=True)
        self.assertEqual(result.returncode, 79)
        self.assertEqual(self.log.read_text(), '3')

    def test_missing_quota_never_means_full_allowance(self):
        self.env['READ_RESPONSES'] = json.dumps([{'rc': 1}])
        result = subprocess.run(['bash', '-c', '. "$1"; sb_gh_guard_check 200', 'test',
                                 str(ROOT / 'scripts/super-board-gh-guard.sh')], cwd=self.root,
                                env=self.env, capture_output=True)
        self.assertEqual(result.returncode, 79)
        self.assertEqual(self.log.read_text(), '3')

    def test_truncated_diff_is_not_complete_evidence(self):
        metadata = self.root / 'meta.json'
        metadata.write_text(json.dumps({'files':[{'path':'x.sql'}], 'additions':2,'deletions':0}))
        self.env['READ_RESPONSES'] = json.dumps([{'out':'diff --git a/x.sql b/x.sql\n@@ -0,0 +1,2 @@\n+one\n'}])
        result = subprocess.run(['python3', str(HELPER), '--kind', 'diff', '--meta', str(metadata),
                                 '--', 'pr', 'diff', '1'], env=self.env, cwd=self.root, capture_output=True)
        self.assertEqual(result.returncode, 79)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(self.log.read_text(), '3')

    def test_corrected_query_can_resume_without_erasing_original_failure(self):
        bad = ['api', 'graphql', '-f', 'query=query { wrong }']
        self.env['SB_GITHUB_READ_STRICT'] = '1'  # need a halt to resume from
        self.assertEqual(self.read([{'rc':1,'err':'Cannot query field wrong'}], command=bad).returncode,79)
        original = self.halt.read_text()
        self.env['READ_RESPONSES'] = json.dumps([{'out':'{"resources":{"graphql":{"remaining":5000,"reset":0},"core":{"remaining":5000,"reset":0}}}'}])
        result = subprocess.run(['python3',str(HELPER),'--resume','--kind','json','--',
                                 'api','graphql','-f','query=query { viewer { login } }'],
                                env=self.env,cwd=self.root,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(self.halt.exists())
        records = list(self.root.glob('github-halt-*.json'))
        self.assertEqual(len(records),1)
        self.assertEqual(records[0].read_text(),original)

    def test_failed_closed_card_reconcile_keeps_claim_and_lock(self):
        lock = self.root / 'inflight'; lock.mkdir()
        # No lock initially: the card is eligible for the closed-card reconcile path.
        self.env['READ_RESPONSES'] = json.dumps([{'out':'CLOSED'}, {'rc':1}])
        script = """SB_LIB_ONLY=1 . "$1"
INFLIGHT_DIR="$2/inflight"; RUN_MANIFEST="$2/run.log"; BOT_LOGIN=robot
STATUS_FIELD_ID=field; STATUS_OPTIONS_JSON='[{"id":"done","name":"Done"}]'
PROJECT_ITEMS_JSON='{"items":[{"id":"item","status":"Review","content":{"number":7,"type":"Issue","assignees":[]}}]}'
top_card_in_column Review
"""
        result = subprocess.run(['bash','-c',script,'test',str(ROOT / 'scripts/super-board-run.sh'),str(self.root)],
                                env=self.env,cwd=self.root,capture_output=True)
        self.assertEqual(result.returncode,79,result.stderr)
        self.assertEqual(self.log.read_text(),'4','one state read, three failed project reads, no claim release')

    def test_dedupe_capped_or_malformed_data_is_never_no_match(self):
        for data in [[{}], [{'number': n+1, 'body':'old'} for n in range(200)]]:
            self.halt.unlink(missing_ok=True); self.log.unlink(missing_ok=True)
            result = self.read([{'out':json.dumps(data)}],'dedupe', ['issue','list','--json','number,body'])
            self.assertEqual(result.returncode,79)
            self.assertEqual(result.stdout,'')
            self.assertEqual(self.log.read_text(),'3')

    def test_caller_read_missing_required_fields_is_64_without_gh_or_halt(self):
        bad = ['pr', 'list', '--repo', 'x/y', '--state', 'open', '--limit', '100', '--json', 'number,title,headRefName,files']
        r = self.read([{'out': '[]'}], 'prs-open', bad)
        self.assertEqual(r.returncode, 64, r.stderr)
        self.assertIn('add body,url to --json', r.stderr)
        self.assertFalse(self.log.exists(), 'no gh call for a caller-shape error')
        self.assertFalse(self.halt.exists(), 'caller mistake must not halt the board')
        self.assertEqual(r.stdout, '')
        for kind, command, missing in [
                ('prs-merged', ['pr', 'list', '--json=number,title'], 'body,url'),
                ('issues-closed', ['issue', 'list', '--json', 'number,title,body'], 'stateReason'),
                ('issue', ['issue', 'view', '1', '--json', 'number', '--json', 'title'], 'body'),
                ('body', ['pr', 'view', '1', '--json', 'body'], 'headRefOid'),
                ('merge-state', ['pr', 'view', '1', '--json=state,headRefOid'], 'mergeCommit'),
                ('dedupe', ['issue', 'list', '--json', 'number'], 'body')]:
            r = self.read([{'out': '[]'}], kind, command)
            self.assertEqual(r.returncode, 64, (kind, r.stderr))
            self.assertIn(f'add {missing} to --json', r.stderr, kind)
            self.assertFalse(self.log.exists(), (kind, 'no gh call for a caller-shape error'))
            self.assertFalse(self.halt.exists(), kind)

    def test_bare_json_flag_is_64_without_gh_or_halt(self):
        for command in (['pr', 'list', '--json'], ['pr', 'list', '--json', '--limit', '5']):
            r = self.read([{'out': '[]'}], 'prs-open', command)
            self.assertEqual(r.returncode, 64, r.stderr)
            self.assertIn('needs a comma-separated field list', r.stderr)
            self.assertFalse(self.log.exists())
            self.assertFalse(self.halt.exists())

    def test_gh_rejecting_json_fields_is_64_once_without_halt(self):
        for err in ('Unknown JSON field: "bogus"\nAvailable fields:\n  number', 'Specify one or more comma-separated fields for `--json`:'):
            self.log.unlink(missing_ok=True)
            r = self.read([{'rc': 1, 'err': err}], 'prs-open',
                          ['pr', 'list', '--json', 'number,title,body,url,headRefName,files'])
            self.assertEqual(r.returncode, 64, r.stderr)
            self.assertIn('Valid form', r.stderr)
            self.assertEqual(self.log.read_text(), '1', 'caller error: no retry')
            self.assertFalse(self.halt.exists(), 'caller error must not halt the board')
            self.assertEqual(r.stdout, '')

    SLURP_CMD = ['api', 'repos/o/r/pulls/1/comments', '--paginate', '--slurp', '--jq', '[.[][] | {id}]']
    SLURP_ERR = 'the `--slurp` option is not supported with `--jq` or `--template`\n\nUsage:  gh api <endpoint> [flags]'

    def test_gh_usage_error_is_64_once_without_halt(self):
        for err in (self.SLURP_ERR, 'unknown flag: --foo'):
            with self.subTest(err=err):
                self.log.unlink(missing_ok=True)
                self.halt.unlink(missing_ok=True)
                r = self.read([{'rc': 1, 'err': err}], command=self.SLURP_CMD)
                self.assertEqual(r.returncode, 64, r.stderr)
                self.assertEqual(self.log.read_text(), '1', 'caller error: no retry')
                self.assertFalse(self.halt.exists(), 'caller error must not halt the board')
                self.assertIn('gh rejected the command line (caller error; no retry, the run continues)', r.stderr)
                self.assertEqual(r.stdout, '')

    def test_gh_usage_error_still_halts_when_strict(self):
        self.env['SB_GITHUB_READ_STRICT'] = '1'
        r = self.read([{'rc': 1, 'err': self.SLURP_ERR}], command=self.SLURP_CMD)
        self.assertEqual(r.returncode, 79, r.stderr)
        self.assertTrue(self.halt.exists())

    def test_access_error_with_usage_text_still_halts(self):
        r = self.read([{'rc': 1, 'err': 'HTTP 404: Not Found\nunknown flag: --foo'}], command=self.SLURP_CMD)
        self.assertEqual(r.returncode, 79, r.stderr)
        self.assertTrue(self.halt.exists())

    def test_usage_text_only_in_stdout_is_not_a_usage_error(self):
        r = self.read([{'rc': 1, 'err': 'HTTP 503 unavailable', 'out': 'issue body: unknown flag: --foo'}],
                      command=self.SLURP_CMD)
        self.assertEqual(r.returncode, 79, r.stderr)
        self.assertEqual(self.log.read_text(), '3', 'normal retry path, not caller error')

    def test_every_usage_phrase_is_64(self):
        for err in ("unknown shorthand flag: 'z' in -z", 'unknown command "foo" for "gh"',
                    'accepts at most 1 arg(s), received 2', 'accepts 1 arg(s), received 0',
                    'requires at least 1 arg(s), only received 0', 'flag needs an argument: --jq',
                    'invalid argument "x" for "--limit" flag', 'flags --a and --b cannot be used together',
                    'if any flags in the group [a b] are set none of the others can be; [a b] were all set'):
            with self.subTest(err=err):
                self.log.unlink(missing_ok=True)
                self.halt.unlink(missing_ok=True)
                r = self.read([{'rc': 1, 'err': err}], command=self.SLURP_CMD)
                self.assertEqual(r.returncode, 64, r.stderr)
                self.assertEqual(self.log.read_text(), '1')

    def test_outage_with_usage_phrase_still_retries_and_halts(self):
        for err in ('HTTP 502: requires at least one', 'error connecting to api.github.com: not supported with proxy'):
            with self.subTest(err=err):
                self.log.unlink(missing_ok=True)
                self.halt.unlink(missing_ok=True)
                r = self.read([{'rc': 1, 'err': err}], command=self.SLURP_CMD)
                self.assertEqual(r.returncode, 79, r.stderr)
                self.assertEqual(self.log.read_text(), '3', 'outage: normal retry path')
                self.assertTrue(self.halt.exists())

    def test_real_gh_degradation_still_halts(self):
        r = self.read([{'rc': 1, 'err': 'HTTP 502 bad gateway'}], 'prs-open',
                      ['pr', 'list', '--json', 'number,title,body,url,headRefName,files'])
        self.assertEqual(r.returncode, 79, r.stderr)
        self.assertEqual(self.log.read_text(), '3')
        self.assertTrue(self.halt.exists())

    def test_caller_read_with_all_required_fields_passes(self):
        good = ['pr', 'list', '--repo', 'x/y', '--state', 'open', '--limit', '100', '--json',
                'number,title,body,url,headRefName,files']
        entry = {'number': 1, 'title': 't', 'body': 'b', 'url': 'u', 'headRefName': 'h', 'files': [{'path': 'a'}]}
        r = self.read([{'out': json.dumps([entry])}], 'prs-open', good)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.log.read_text(), '1')

    def test_response_missing_a_requested_field_still_retries_and_halts(self):
        good = ['pr', 'list', '--json', 'number,title,body,url,headRefName,files']
        r = self.read([{'out': json.dumps([{'number': 1, 'title': 't', 'url': 'u', 'headRefName': 'h', 'files': []}])}],
                      'prs-open', good)
        self.assertEqual(r.returncode, 79, r.stderr)
        self.assertEqual(self.log.read_text(), '3')
        self.assertTrue(self.halt.exists())

    def test_legacy_mutation_failure_pauses_without_retry_or_lock_removal(self):
        inflight = self.root / 'inflight'; inflight.mkdir()
        lock = inflight / '7'; lock.write_text('PID=\nLANE=build\n')
        self.env['READ_RESPONSES'] = json.dumps([{'rc':1}])
        script = 'SB_LIB_ONLY=1 . "$1"; INFLIGHT_DIR="$2/inflight"; RUN_MANIFEST="$2/run.log"; BOT_LOGIN=robot; reap_finished_locks'
        result = subprocess.run(['bash','-c',script,'test',str(ROOT / 'scripts/super-board-run.sh'),str(self.root)],
                                env=self.env,cwd=self.root,capture_output=True)
        self.assertEqual(result.returncode,79)
        self.assertEqual(self.log.read_text(),'1')
        self.assertTrue(self.halt.exists())
        self.assertTrue(lock.exists())

    def test_worktrees_share_halt_and_explicit_restart_preserves_reason(self):
        self.env.pop('SB_GITHUB_HALT_FILE')
        main = self.root / 'main'; main.mkdir()
        subprocess.run(['git', 'init', '-q', str(main)], check=True)
        subprocess.run(['git', '-C', str(main), '-c', 'user.name=test', '-c', 'user.email=test@test',
                        'commit', '--allow-empty', '-qm', 'initial'], check=True)
        linked = self.root / 'linked'
        subprocess.run(['git', '-C', str(main), 'worktree', 'add', '-q', '--detach', str(linked)], check=True)
        self.env['SB_REPO_PATH'] = str(linked)
        self.assertEqual(self.read([{'rc': 1}]).returncode, 79)
        marker = main / '.claude/super-board/github-halt.json'
        self.assertTrue(marker.exists())
        self.env['SB_REPO_PATH'] = str(main)
        self.assertEqual(self.read([{'out': '{}'}]).returncode, 79)
        self.env['READ_RESPONSES'] = json.dumps([{'out': '{"resources":{"graphql":{"remaining":5000,"reset":0},"core":{"remaining":5000,"reset":0}}}'}])
        subprocess.run(['python3', str(HELPER), '--resume'], env=self.env, check=True)
        self.assertFalse(marker.exists())
        self.assertEqual(len(list(marker.parent.glob('github-halt-*.json'))), 1)
        self.assertEqual(self.read([{'out': '{}'}]).returncode, 0)

    NO_CHECKS = "no checks reported on the 'issue-6-negative-amounts-parentheses' branch"

    def test_no_checks_reported_is_zero_checks_not_a_read_failure(self):
        for command, expected in [(['pr', 'checks', '12'], ''),
                                  (['pr', 'checks', '12', '--json', 'name,state,bucket'], '[]')]:
            with self.subTest(command=command):
                self.log.unlink(missing_ok=True)
                r = self.read([{'rc': 1, 'err': self.NO_CHECKS}], command=command)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout.strip(), expected)
                self.assertEqual(self.log.read_text(), '1', 'no retry')
                self.assertFalse(self.halt.exists(), 'no halt')

    def test_no_checks_message_on_other_commands_still_fails(self):
        r = self.read([{'rc': 1, 'err': self.NO_CHECKS}], command=['pr', 'view', '12'])
        self.assertEqual(r.returncode, 79, r.stderr)
        self.assertEqual(self.log.read_text(), '3')

    def test_checks_real_failures_still_fail_closed(self):
        for err in ['HTTP 401: Bad credentials', 'HTTP 503 unavailable',
                    'GraphQL: Could not resolve to a PullRequest with the number of 999.',
                    'HTTP 403: API rate limit exceeded', 'connection reset by peer']:
            with self.subTest(err=err):
                self.halt.unlink(missing_ok=True)
                r = self.read([{'rc': 1, 'err': err}], command=['pr', 'checks', '12'])
                self.assertEqual(r.returncode, 79, r.stderr)
                self.assertTrue(self.halt.exists())

    def test_failing_and_pending_checks_are_never_zero(self):
        failing = 'build\tfail\t1m2s\thttps://example/1\n'
        pending = 'build\tpending\t0\thttps://example/1\n'
        for rc, out in [(1, failing), (8, pending), (1, failing + self.NO_CHECKS + '\n')]:
            with self.subTest(rc=rc, out=out):
                self.halt.unlink(missing_ok=True)
                r = self.read([{'rc': rc, 'out': out}], command=['pr', 'checks', '12'])
                self.assertNotEqual(r.returncode, 0, r.stdout)
                self.assertEqual(r.stdout, '')  # no fabricated zero-checks success


    def test_no_checks_short_circuit_edge_cases(self):
        no = self.NO_CHECKS
        zero = [(['pr', 'checks', '12', '-R', 'owner/repo'], ''),
                (['pr', 'checks', '12', '--required'], ''),
                (['pr', 'checks', '12', '--json', 'name', '-R', 'o/r'], '[]')]
        for command, expected in zero:
            with self.subTest(command=command):
                r = self.read([{'rc': 1, 'err': no}], command=command)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout.strip(), expected)
                self.assertIn('gh reported zero checks on issue-6-negative-amounts-parentheses', r.stderr)
        r = self.read([{'rc': 1, 'err': no.replace('no checks', 'no required checks')}],
                      command=['pr', 'checks', '12', '--required'])
        self.assertEqual(r.returncode, 0, r.stderr)
        for command in (['pr', 'checks', '12', '--json', 'name', '--jq', '.[]'],
                        ['pr', 'checks', '12', '--json', 'name', '-q', '.[]'],
                        ['pr', 'checks', '12', '--json', 'name', '--template', 'x'],
                        ['pr', 'checks', '12', '--json', 'name', '-t', 'x']):
            with self.subTest(command=command):
                self.halt.unlink(missing_ok=True)
                self.assertEqual(self.read([{'rc': 1, 'err': no}], command=command).returncode, 79)
        for resp in ({'rc': 1, 'err': no + ' (extra)'},
                     {'rc': 1, 'err': no + '\nsecond line'},
                     {'rc': 1, 'err': no, 'out': 'build\tfail\n'}):
            with self.subTest(resp=resp):
                self.halt.unlink(missing_ok=True)
                self.assertEqual(self.read([resp], command=['pr', 'checks', '12']).returncode, 79)

    def test_no_checks_short_circuit_respects_halt_set_by_another_worker(self):
        self.env['READ_TOUCH'] = str(self.halt)
        r = self.read([{'rc': 1, 'err': self.NO_CHECKS}], command=['pr', 'checks', '12'])
        self.assertEqual(r.returncode, 79, r.stderr)
        self.assertEqual(r.stdout, '')


if __name__ == '__main__':
    unittest.main()
