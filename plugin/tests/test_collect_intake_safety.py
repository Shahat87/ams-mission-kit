"""Offline end-to-end filer regressions with real router, child filers and guard."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import fcntl
import hashlib
import time

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT/'skills/super-collect/scripts/super-collect-file.sh'
BODY = '''## Problem
PRIVATE_NOTE_DO_NOT_STORE: the request fails.
## Context
- **Where:** /example
## Fix
Repair the request.
## Acceptance Criteria
- [ ] The request returns the expected result.
## Risk
Small isolated change.
'''
STUB = r'''#!/usr/bin/env python3
import json,os,pathlib,sys,time
root=pathlib.Path(os.environ['FIXTURE_ROOT']); args=sys.argv[1:]
def flag(name): return args[args.index(name)+1]
def emit(v): print(json.dumps(v))
def issues(): return json.loads((root/'issues.json').read_text())
with (root/'calls.jsonl').open('a') as f: f.write(json.dumps(args)+'\n')
kind=args[:2]
if kind==['repo','view']: print('acme/app' if 'nameWithOwner' in ' '.join(args) else 'acme')
elif kind==['api','graphql']:
 p=root/'history-count'; n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n))
 if os.environ.get('HISTORY_OUTAGE') or (os.environ.get('FAIL_HISTORY_AFTER') and n>=int(os.environ['FAIL_HISTORY_AFTER'])):
  print('HTTP 503',file=sys.stderr); sys.exit(1)
 rows=issues(); pages=[]
 if os.environ.get('MALFORMED_HISTORY') and rows: rows[0].pop('state',None)
 for i in range(0,max(1,len(rows)),100):
  next_page=i+100<len(rows) or bool(os.environ.get('TRUNCATED_HISTORY'))
  pages.append({'data':{'repository':{'issues':{'totalCount':len(rows),'nodes':rows[i:i+100],
    'pageInfo':{'hasNextPage':next_page,'endCursor':str(i+100) if next_page else None}}}}})
 emit(pages)
elif kind==['project','field-list']:
 if os.environ.get('MALFORMED_FIELDS'):
  emit({'fields':[{'id':'F','name':'Status','options':[{'name':'Backlog'}]}]});sys.exit(0)
 emit({'fields':[{'id':'F','name':'Status','options':[{'id':'Backlog','name':'Backlog'},{'id':'Ready','name':'Ready'}]}]})
elif kind==['project','item-list']:
 rows=json.loads((root/'board.json').read_text()); emit({'items':rows,'totalCount':len(rows)})
elif kind==['project','list']: emit({'projects':[{'title':'Board','number':7}]})
elif kind==['project','view']: print('PVT')
elif kind==['issue','view']: print('https://github.com/acme/app/issues/'+args[2])
elif kind==['issue','create']:
 time.sleep(float(os.environ.get('CREATE_DELAY','0')))
 rows=issues(); n=max([r['number'] for r in rows]+[1000])+1
 body=pathlib.Path(flag('--body-file')).read_text()
 rows.append({'number':n,'state':'OPEN','body':body}); (root/'issues.json').write_text(json.dumps(rows))
 if os.environ.get('FAIL_CREATE_AFTER_STORE'): print('connection dropped',file=sys.stderr);sys.exit(1)
 print('https://github.com/acme/app/issues/'+str(n))
elif kind==['issue','comment']:
 with (root/'comments.jsonl').open('a') as f:f.write(json.dumps({'number':args[2],'body':flag('--body')})+'\n')
 if os.environ.get('FAIL_COMMENT_AFTER_STORE'): print('connection dropped',file=sys.stderr);sys.exit(1)
elif kind==['project','item-add']: print('ITEM')
elif kind==['project','item-edit']: pass
elif kind in (['issue','edit'],['label','create']): pass
else: print('unsupported fixture command '+repr(args),file=sys.stderr);sys.exit(1)
'''

class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        (self.root/'gh').write_text(STUB);(self.root/'gh').chmod(0o755)
        (self.root/'issues.json').write_text('[]');(self.root/'board.json').write_text('[]')
        (self.root/'body.md').write_text(BODY)
        cfg={'project':{'owner':'acme','number':7,'title':'Board'},'repo':{'remote':'https://github.com/acme/app.git','path':'.'}}
        (self.root/'config.json').write_text(json.dumps(cfg));(self.root/'other.json').write_text(json.dumps(cfg))
        self.env=dict(os.environ,PATH=str(self.root)+':'+os.environ['PATH'],FIXTURE_ROOT=str(self.root),
            SUPER_BOARD_BIN=str(ROOT/'scripts'),SB_GITHUB_HALT_FILE=str(self.root/'halt.json'),
            SB_GITHUB_RETRY_DELAY='0',SB_COLLECT_STATE_DIR=str(self.root/'collector-state'))
        self.argv=[str(ROUTER),'--config',str(self.root/'config.json'),'--type','feature','--source','custom',
            '--title','Fix request','--body-file',str(self.root/'body.md'),'--fingerprint','custom|demo|one','--yes']
    def tearDown(self):self.tmp.cleanup()
    def run_filer(self,**env):
        return subprocess.run(self.argv,env=dict(self.env,**env),cwd=self.root,capture_output=True,text=True,timeout=15)
    def calls(self):
        p=self.root/'calls.jsonl';return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
    def mutations(self):
        return [a for a in self.calls() if a[:2] in [['issue','create'],['issue','comment'],['issue','edit'],['label','create'],['project','item-add'],['project','item-edit']]]
    def seed(self,n=601,hit=2,state='OPEN'):
        rows=[{'number':i,'state':'OPEN','body':'unrelated'} for i in range(1,n+1)]
        rows[hit-1]['body']='custom|demo|one';rows[hit-1]['state']=state
        (self.root/'issues.json').write_text(json.dumps(rows))
    def test_failed_or_truncated_history_never_mutates(self):
        for flag in ['HISTORY_OUTAGE','TRUNCATED_HISTORY']:
            with self.subTest(flag=flag):
                self.seed(); (self.root/'halt.json').unlink(missing_ok=True)
                (self.root/'history-count').unlink(missing_ok=True)
                result=self.run_filer(**{flag:'1'})
                self.assertEqual(result.returncode,79,result.stderr)
                self.assertEqual(self.mutations(),[])
    def test_malformed_history_or_holding_metadata_never_mutates(self):
        for flag in ['MALFORMED_HISTORY','MALFORMED_FIELDS']:
            with self.subTest(flag=flag):
                self.seed();(self.root/'halt.json').unlink(missing_ok=True)
                result=self.run_filer(**{flag:'1'})
                self.assertEqual(result.returncode,79,result.stderr)
                self.assertEqual(self.mutations(),[])
    def test_bounded_lock_wait_does_not_mutate_and_can_run_after_release(self):
        state=self.root/'collector-state';state.mkdir()
        key=hashlib.sha256(b'acme/app').hexdigest()
        with (state/(key+'.lock')).open('w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            before=time.monotonic();result=self.run_filer(SB_COLLECT_LOCK_TIMEOUT='.05')
            self.assertEqual(result.returncode,4,result.stderr)
            self.assertLess(time.monotonic()-before,3)
            self.assertEqual(self.mutations(),[])
        self.assertEqual(self.run_filer().returncode,0)
    def test_older_open_fingerprint_is_not_lost_after_500(self):
        self.seed(hit=2)
        result=self.run_filer();self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'2')
        self.assertFalse(any(a[:2]==['issue','create'] for a in self.calls()))
    def test_closed_history_still_creates_one_recurrence_through_child(self):
        self.seed(hit=2,state='CLOSED')
        result=self.run_filer();self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'1001')
        body=json.loads((self.root/'issues.json').read_text())[-1]['body']
        self.assertIn('Recurrence:',body);self.assertIn('#2',body)
        self.assertGreaterEqual(int((self.root/'history-count').read_text()),2)
        self.assertTrue(any('Backlog' in a and a[:2]==['project','item-edit'] for a in self.calls()))
        self.assertFalse(any('Ready' in a for a in self.mutations()))
    def test_child_history_failure_does_not_write_or_tag(self):
        result=self.run_filer(FAIL_HISTORY_AFTER='2')
        self.assertEqual(result.returncode,79,result.stderr)
        self.assertEqual(self.mutations(),[])
    def test_concurrent_configs_create_once(self):
        env=dict(self.env,CREATE_DELAY='.25')
        a=subprocess.Popen(self.argv,env=env,cwd=self.root,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        other=list(self.argv);other[2]=str(self.root/'other.json')
        b=subprocess.Popen(other,env=env,cwd=self.root,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        ao,ae=a.communicate(timeout=15);bo,be=b.communicate(timeout=15)
        self.assertEqual(a.returncode,0,ae);self.assertEqual(b.returncode,0,be)
        self.assertEqual(ao.strip(),bo.strip())
        self.assertEqual(sum(c[:2]==['issue','create'] for c in self.calls()),1)
    def test_unchanged_sighting_is_quiet_new_evidence_comments_once(self):
        self.seed(n=3,hit=2)
        self.assertEqual(self.run_filer().returncode,0)
        self.assertEqual(self.run_filer().returncode,0)
        (self.root/'body.md').write_text(BODY+'\nNew evidence: failed for three users.\n')
        self.assertEqual(self.run_filer().returncode,0)
        self.assertEqual(self.run_filer().returncode,0)
        comments=(self.root/'comments.jsonl').read_text().splitlines()
        self.assertEqual(len(comments),2)
    def test_ambiguous_create_is_not_blindly_retried(self):
        first=self.run_filer(FAIL_CREATE_AFTER_STORE='1')
        self.assertEqual(first.returncode,79,first.stderr)
        second=self.run_filer();self.assertEqual(second.returncode,79)
        self.assertEqual(sum(c[:2]==['issue','create'] for c in self.calls()),1)
    def test_already_on_board_adopt_never_moves_active_card(self):
        (self.root/'board.json').write_text(json.dumps([{'id':'item','content':{'type':'Issue','title':'Active','number':5,'url':'https://github.com/acme/app/issues/5'},'status':'Building'}]))
        self.argv=[str(ROUTER),'--config',str(self.root/'config.json'),'--adopt','5','--type','feature','--yes']
        result=self.run_filer();self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.mutations(),[])
    def test_missing_board_url_cannot_reset_matching_active_card(self):
        (self.root/'board.json').write_text(json.dumps([{'id':'item','content':{'type':'Issue','title':'Active','number':5},'status':'Building'}]))
        self.argv=[str(ROUTER),'--config',str(self.root/'config.json'),'--adopt','5','--type','feature','--yes']
        result=self.run_filer()
        self.assertEqual(result.returncode,79,result.stderr)
        self.assertEqual(self.mutations(),[])
    def test_explicit_repository_is_used_for_every_issue_and_label_write(self):
        result=self.run_filer();self.assertEqual(result.returncode,0,result.stderr)
        writes=[a for a in self.mutations() if a[0] in ('issue','label')]
        self.assertTrue(any(a[:2]==['label','create'] for a in writes))
        for args in writes:
            self.assertIn('-R',args,args)
            self.assertEqual(args[args.index('-R')+1],'acme/app')
    def test_lock_release_after_read_failure_and_private_ignored_receipts(self):
        result=self.run_filer(HISTORY_OUTAGE='1');self.assertEqual(result.returncode,79)
        (self.root/'halt.json').unlink();(self.root/'history-count').unlink()
        result=self.run_filer();self.assertEqual(result.returncode,0,result.stderr)
        state=next((self.root/'collector-state').glob('*.json'))
        self.assertNotIn('PRIVATE_NOTE_DO_NOT_STORE',state.read_text())
        subprocess.run(['git','init','-q'],cwd=self.root,check=True)
        ignored=subprocess.run(['git','check-ignore','-q',str(state)],cwd=self.root)
        self.assertEqual(ignored.returncode,0)

if __name__=='__main__':unittest.main()
