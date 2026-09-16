"""Run in the pinned runtime image with --network none and a fresh HERMES_HOME.

Real Hermes 0.21.1 resolver, GovernedAIAgent and OpenAI SSE transport; only the
HTTP endpoint is a loopback fixture. No credentials, network or live account.
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version

from hermes.runtime.model_config import ModelConfig
from hermes.runtime.nous_engine import _resolve_hermes_runtime, GovernedAIAgent, NousReasoningEngine
from hermes.runtime.model_config import ManagedProviderUnavailableError


def main():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append((self.path, self.headers.get('Authorization'), body))
            if self.path.endswith('/api/show'):
                self.send_response(404)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"error":"unsupported_endpoint"}')
                return
            if body.get('stream'):
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                for delta, finish in [({'role': 'assistant', 'content': 'OK'}, None), ({}, 'stop')]:
                    chunk = {'id': 'test', 'object': 'chat.completion.chunk', 'created': 1,
                             'model': 'test-model', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
                    self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
            else:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'id': 'test', 'object': 'chat.completion', 'created': 1,
                    'model': 'test-model', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'OK'}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 1, 'total_tokens': 11}}).encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f'http://127.0.0.1:{server.server_port}/v1/inference/test/v1'
    # HTTP loopback is only for this isolated transport fixture; signed managed
    # policy validation requires the associated Enterprise HTTPS origin.
    config = ModelConfig(model='custom/test-model', native_provider='custom', managed=True,
                         api_key='test-scoped-only', base_url=endpoint)
    runtime, model = _resolve_hermes_runtime(config)
    assert runtime['provider'] == 'custom'
    assert runtime['api_mode'] == 'chat_completions'
    assert model == 'test-model'
    engine = object.__new__(NousReasoningEngine)
    try:
        engine._build_governed_agent(config, '', None, None)
    except ManagedProviderUnavailableError as exc:
        assert 'isolate auxiliary credentials' in str(exc)
    else:
        raise AssertionError('Production managed execution must remain disabled')
    # Diagnostic only: bypass the production gate with a disposable fake token
    # in this isolated process to reproduce why the upstream SDK is unsafe.
    agent = GovernedAIAgent(model=model, api_key=runtime['api_key'], base_url=runtime['base_url'],
        provider=runtime['provider'], api_mode=runtime['api_mode'], max_iterations=1,
        enabled_toolsets=[], quiet_mode=True, skip_memory=True, skip_context_files=True,
        skip_background_review=True, save_trajectories=False, ephemeral_system_prompt='Reply OK.')
    result = agent.run_conversation('Reply OK.')
    assert result.get('final_response') == 'OK', result.keys()
    from agent import auxiliary_client
    assert auxiliary_client._RUNTIME_MAIN_API_KEY == 'test-scoped-only'
    # Exercise actual streaming SDK tool-schema serialization at that exact
    # resolved endpoint as well; never substitute a mock resolver or client.
    from openai import OpenAI
    client = OpenAI(api_key=runtime['api_key'], base_url=runtime['base_url'])
    stream = client.chat.completions.create(model=model, messages=[{'role': 'user', 'content': 'OK'}],
        tools=[{'type': 'function', 'function': {'name': 'test_read', 'description': 'No execution', 'parameters': {'type': 'object', 'properties': {}}}}], stream=True)
    assert ''.join(chunk.choices[0].delta.content or '' for chunk in stream) == 'OK'
    assert all(path == '/v1/inference/test/v1/chat/completions' and auth == 'Bearer test-scoped-only'
               and body['model'] == 'test-model' for path, auth, body in seen if not path.endswith('/api/show')), [
                   {'path': path, 'scoped_auth': auth == 'Bearer test-scoped-only', 'model': body.get('model')}
                   for path, auth, body in seen]
    assert any(body.get('stream') and body.get('tools') for _, _, body in seen)
    if os.environ.get('SAFENT_SMOKE_PRINT_REQUEST') == '1':
        print(json.dumps([body for path, _, body in seen if path.endswith('/chat/completions')]))
    client.close()
    server.shutdown()
    print(f'PASS Hermes {version("hermes-agent")}: native custom + GovernedAIAgent + tools/SSE transport; requests={len(seen)}; global auxiliary credential exposure reproduced; production managed execution blocked')


if __name__ == '__main__':
    main()
