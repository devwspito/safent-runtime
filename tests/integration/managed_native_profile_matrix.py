"""Hermes 0.21.1 routing matrix, run only inside disposable --network none image.

Two real loopback servers and fresh subprocesses (test isolation only, not a
production IPC design). No monkeypatches to Hermes. All credentials/prompts fake.
"""
from __future__ import annotations

import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def worker():
    case = json.load(sys.stdin)
    from agent.auxiliary_client import call_llm, scoped_runtime_main, aux_interrupt_protection, AuxiliaryExplicitCancellation
    from hermes_cli.config import load_config_readonly
    config = load_config_readonly()
    route = config['model']
    key = case['key']
    cancelled = threading.Event()
    native_agent = None
    def cancel(_signum, _frame):
        cancelled.set()
        if native_agent is not None:
            native_agent.hard_interrupt('Fixture cancellation')
    signal.signal(signal.SIGUSR1, cancel)
    try:
        with scoped_runtime_main({'provider':'custom', 'model':route['default'], 'base_url':route['base_url'],
                                  'api_key':key, 'api_mode':'chat_completions'}):
            if case['task'] == 'chat':
                from hermes.runtime.nous_engine import GovernedAIAgent
                agent = GovernedAIAgent(model=route['default'], api_key=key, base_url=route['base_url'],
                    provider='custom', api_mode='chat_completions', max_iterations=1,
                    enabled_toolsets=[], quiet_mode=True, skip_memory=True, skip_context_files=True,
                    skip_background_review=True, save_trajectories=False,
                    ephemeral_system_prompt='Reply OK.', max_tokens=16)
                native_agent = agent._inner
                result = agent.run_conversation('Reply OK.')
                if result.get('final_response') != 'OK':
                    raise RuntimeError('No successful native response')
            else:
                with aux_interrupt_protection(cancel_event=cancelled):
                    result = call_llm(task=case['task'], messages=[{'role': 'user', 'content': 'Reply OK.'}],
                                      max_tokens=16, timeout=0.2)
                if result.choices[0].message.content != 'OK':
                    raise RuntimeError('No successful auxiliary response')
        print('RESULT:cancelled' if cancelled.is_set() else 'RESULT:success')
    except (Exception, KeyboardInterrupt, AuxiliaryExplicitCancellation) as exc:
        # Do not print SDK exception bodies/credentials. Failure is expected on
        # negative cases; the parent independently inspects every request.
        print('RESULT:' + type(exc).__name__)
    finally:
        if cancelled.is_set():
            print('CANCEL:native-requested')


def main():
    import yaml
    from hermes.runtime.model_config import ModelConfig
    from hermes.runtime.managed_llm_profile import build_profile, allowed_environment
    from hermes_cli.config import DEFAULT_CONFIG

    records = []
    started = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or '{}')
            records.append((self.server.server_port, self.path, self.headers.get('Authorization'), body))
            if self.path.endswith('/api/show'):
                self.send_response(404); self.end_headers(); return
            status = self.path.split('/')[2]
            if status in {'timeout', 'cancel'}:
                started.set()
                time.sleep(2)
            if status in {'401', '402', '429'}:
                self.send_response(int(status)); self.send_header('Content-Type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps({'error': {'message': 'fixture rejection', 'type': 'invalid_request_error'}}).encode())
                return
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream' if body.get('stream') else 'application/json')
                self.end_headers()
                if body.get('stream'):
                    for delta, finish in [({'role': 'assistant', 'content': 'OK'}, None), ({}, 'stop')]:
                        self.wfile.write(('data: '+json.dumps({'id':'test', 'object':'chat.completion.chunk', 'created':1,
                            'model':'company', 'choices':[{'index':0,'delta':delta,'finish_reason':finish}]})+'\n\n').encode())
                    self.wfile.write(b'data: [DONE]\n\n')
                else:
                    self.wfile.write(json.dumps({'id':'test','object':'chat.completion','created':1,'model':'company',
                        'choices':[{'index':0,'message':{'role':'assistant','content':'OK'},'finish_reason':'stop'}]}).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

    gateway = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    decoy = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    for server in (gateway, decoy):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    outcomes = []
    with tempfile.TemporaryDirectory(prefix='managed-profile-matrix-') as root:
        personal_home = Path(root) / 'personal-decoy'
        personal_home.mkdir()
        personal_url = f'http://127.0.0.1:{decoy.server_port}/decoy/success/v1'
        (personal_home / 'config.yaml').write_text(yaml.safe_dump({
            'model': {'provider':'custom', 'default':'personal', 'base_url':personal_url},
            'auxiliary': {'compression': {'provider':'custom', 'model':'personal', 'base_url':personal_url}},
        }))
        control_env = {**os.environ, 'HERMES_HOME':str(personal_home), 'HOME':str(personal_home),
                       'OPENAI_API_KEY':'test-personal-decoy', 'OPENAI_BASE_URL':personal_url}
        control = subprocess.run([sys.executable, __file__, '--worker'], env=control_env,
            input=json.dumps({'task':'compression', 'key':'test-personal-decoy'}),
            capture_output=True, text=True, timeout=20)
        assert 'RESULT:success' in control.stdout, control.stderr[-1200:]
        assert any(port == decoy.server_port and auth == 'Bearer test-personal-decoy'
                   for port, _, auth, _ in records), 'Personal decoy positive control failed'
        records.clear()
        for task in ('compression', 'vision', 'review', 'memory_query_rewrite', 'chat'):
            for status in ('success', '401', '402', '429', 'timeout', 'cancel'):
                home = Path(root) / (task + '-' + status)
                home.mkdir()
                endpoint = f'http://127.0.0.1:{gateway.server_port}/gateway/{status}/{task}/v1'
                binding = ModelConfig(model='custom/company', managed=True, native_provider='custom',
                    api_key='test-scoped-only', base_url='https://enterprise.invalid/v1/inference/test/v1')
                config = build_profile(binding, DEFAULT_CONFIG)
                # Loopback HTTP substitution belongs only to this transport
                # fixture. Production profile builder requires HTTPS.
                config['model']['base_url'] = endpoint
                config['providers'] = {'custom': {'request_timeout_seconds': 0.2}}
                config['agent'] = {'api_max_retries': 1}
                for route in config['auxiliary'].values():
                    if isinstance(route, dict):
                        route['base_url'] = endpoint
                (home / 'config.yaml').write_text(yaml.safe_dump(config))
                dirty_env = {**os.environ, 'OPENAI_API_KEY':'test-personal-decoy',
                    'OPENAI_BASE_URL':personal_url, 'HOME':str(personal_home),
                    'HERMES_MODEL':'personal', 'AWS_PROFILE':'personal', 'HERMES_HOME':str(personal_home)}
                env = allowed_environment(dirty_env, home)
                # Explicit test runner path and retry/time budgets; never
                # inherited SDK credentials or subprocess production wiring.
                env.update(PYTHONPATH='/review/src')
                started.clear()
                process = subprocess.Popen([sys.executable, __file__, '--worker'], env=env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                process.stdin.write(json.dumps({'task':task, 'key':'test-scoped-only'})); process.stdin.close()
                process.stdin = None
                if status == 'cancel':
                    assert started.wait(15), (task, 'request never reached gateway')
                    process.send_signal(signal.SIGUSR1)
                try:
                    stdout, stderr = process.communicate(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill(); stdout, stderr = process.communicate()
                    raise AssertionError((task, status, 'worker exceeded fixture deadline'))
                result = next((line for line in stdout.splitlines() if line.startswith('RESULT:')), 'RESULT:terminated')
                outcomes.append({'task':task,'case':status,'result':result,'exit':process.returncode})
                print(json.dumps(outcomes[-1]), flush=True)
                if status == 'success':
                    assert result == 'RESULT:success', (outcomes[-1], stderr[-1200:])
                else:
                    assert result != 'RESULT:success', outcomes[-1]
                    assert result != 'RESULT:terminated', (outcomes[-1], stderr[-1200:])
                if status == 'cancel':
                    assert 'CANCEL:native-requested' in stdout, outcomes[-1]
                    if task != 'chat':
                        assert result == 'RESULT:AuxiliaryExplicitCancellation', outcomes[-1]
                assert all(port != decoy.server_port for port, *_ in records), 'Personal decoy received inference'
                assert all(auth == 'Bearer test-scoped-only' for _, path, auth, _ in records if not path.endswith('/api/show'))
                assert all(body.get('model') == 'company' for _, path, _, body in records if not path.endswith('/api/show'))
    gateway.shutdown(); decoy.shutdown()
    print(json.dumps({'outcomes':outcomes, 'requests':len(records), 'decoy_requests':0}))


if __name__ == '__main__':
    worker() if '--worker' in sys.argv else main()
