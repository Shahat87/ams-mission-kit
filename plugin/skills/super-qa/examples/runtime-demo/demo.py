#!/usr/bin/env python3
"""Run a real local browser comparison using an app's EXISTING Playwright.

python3 demo.py --playwright-project /absolute/app --output /new/evidence/folder
Writes two versions of one toy app to temporary directories. No installs/network.
"""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

SERVER = '''import http.server, pathlib
class App(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        page = pathlib.Path('index.html').read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(page)
    def log_message(self, *args): pass
http.server.HTTPServer(('127.0.0.1', int(__import__('sys').argv[1])), App).serve_forever()
'''
PAGE = '''<!doctype html><title>Runtime measurement demo</title>
<button id="run">Compute total</button><p role="status">Ready</p>
<script>document.querySelector('#run').onclick = () => {
const deadline = performance.now() + DELAY;
while (performance.now() < deadline) { Math.sqrt(Math.random()); }
const total = Array.from({length:100}, (_,i)=>i+1).reduce((a,b)=>a+b,0);
document.querySelector('[role=status]').textContent = 'Total: ' + total;
};</script>'''

def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--playwright-project', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--changed-delay', type=int, default=30, help='30 = faster demo; 180 = unchanged; 400 = slower')
    a = p.parse_args()
    here = Path(__file__).resolve().parent
    scripts = here.parents[1] / 'scripts'
    with tempfile.TemporaryDirectory(prefix='sb-runtime-demo-') as tmp:
        root = Path(tmp)
        sides = {}
        ports = [port(), port()]
        while ports[0] == ports[1]:
            ports[1] = port()
        for name, delay, number in [('baseline', 180, ports[0]), ('changed', a.changed_delay, ports[1])]:
            app = root / name
            app.mkdir()
            (app / 'server.py').write_text(SERVER)
            (app / 'index.html').write_text(PAGE.replace('DELAY', str(delay)))
            sides[name] = {'cwd': name, 'revision': f'demo-{name}-delay-{delay}',
                           'start_command': [sys.executable, 'server.py', str(number)],
                           'source_files': ['server.py', 'index.html'],
                           'url': f'http://127.0.0.1:{number}'}
        config = dict(sides, scenario_id='compute-total', data_id='integers-1-through-100',
                      environment_id='same-local-host-chromium', repetitions=5, warmups=1,
                      scenario_files=[str(here / 'scenario.mjs')],
                      environment={'SB_RUNTIME_PROJECT': str(Path(a.playwright_project).resolve())},
                      scenario_command=['node', str(scripts / 'runtime_browser.mjs'), str(here / 'scenario.mjs')])
        (root / 'runtime.json').write_text(json.dumps(config))
        return subprocess.run([sys.executable, str(scripts / 'runtime_compare.py'), '--config',
                               str(root / 'runtime.json'), '--output', str(Path(a.output).resolve())]).returncode

if __name__ == '__main__':
    sys.exit(main())
