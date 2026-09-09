#!/usr/bin/env python3
"""Local WebRTC voice frontend for Codex's experimental app-server."""
import argparse
import collections
import json
from pathlib import Path
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
EFFORTS = {'low', 'medium', 'high', 'xhigh', 'ultra'}
PERMISSION_MODES = {
    'ask': {'approvalPolicy': 'on-request', 'sandbox': 'workspace-write'},
    'yolo': {'approvalPolicy': 'never', 'sandbox': 'danger-full-access'},
}
COMPUTER_USE_INSTRUCTIONS = (
    'Computer interaction preference for this voice client: For viewing or controlling desktop apps '
    'or existing browser windows, use only OpenAI native Computer Use tools that are actually available. '
    'If those tools or their native service are unavailable, explain that limitation and stop that part '
    'of the task. Do not substitute shell screenshots, screen recording, AppleScript, OS scripting, '
    'third-party GUI automation, or Playwright/CDP for desktop/window access. Do not request permission '
    'to use such substitutes. Ordinary project coding, terminal work, API calls and web research remain '
    'available. Never claim to see a window without a successful native tool result. This preference '
    'also applies in YOLO mode. Follow the existing global and project instructions for all other work.'
)


class RequestError(Exception):
    pass


class Codex:
    def __init__(self, cwd, model='gpt-6-astra', permission_mode='ask'):
        if permission_mode not in PERMISSION_MODES:
            raise RequestError('Unbekannter Freigabemodus.')
        self.default_permission_mode = permission_mode
        self.condition = threading.Condition(threading.RLock())
        self.write_lock = threading.Lock()
        self.session_lock = threading.RLock()
        self.sequence = 0
        self.pending = {}
        self.approvals = {}
        self.file_changes = {}
        self.events = collections.deque(maxlen=600)
        self.event_id = 0
        self.answer = None
        self.voice_error = None
        self.closed = False
        self.state = {'threadId': None, 'cwd': str(Path(cwd).expanduser().resolve()),
                      'model': model, 'effort': 'high', 'voiceActive': False,
                      'permissionMode': permission_mode,
                      'activeTurnId': None, 'voiceStatus': 'idle'}
        self.process = subprocess.Popen(
            ['codex', 'app-server', '--enable', 'realtime_conversation'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        threading.Thread(target=self.read, daemon=True).start()
        try:
            self.rpc('initialize', {'clientInfo': {'name': 'codex-voice-local', 'version': '0.2.0'},
                                    'capabilities': {'experimentalApi': True}})
            self.send({'method': 'initialized', 'params': {}})
        except Exception:
            self.close()
            raise

    def send(self, message):
        with self.write_lock:
            if self.process.poll() is not None:
                raise RequestError('Codex ist nicht mehr verbunden. Bitte den lokalen Server neu starten.')
            self.process.stdin.write(json.dumps(message) + '\n')
            self.process.stdin.flush()

    def rpc(self, method, params, timeout=50):
        with self.condition:
            self.sequence += 1
            ident = self.sequence
            entry = {'done': False}
            self.pending[ident] = entry
        try:
            self.send({'id': ident, 'method': method, 'params': params})
            with self.condition:
                if not self.condition.wait_for(lambda: entry['done'] or self.closed, timeout):
                    raise RequestError('Codex antwortet noch nicht. Bitte den Status prüfen und erneut versuchen.')
                if not entry['done']:
                    raise RequestError('Die Verbindung zu Codex wurde geschlossen.')
                response = entry['response']
            if 'error' in response:
                raise RequestError(str(response['error'].get('message', 'Codex-Anfrage fehlgeschlagen.')))
            return response.get('result', {})
        finally:
            with self.condition:
                self.pending.pop(ident, None)

    def emit(self, method, params):
        with self.condition:
            self.event_id += 1
            self.events.append((self.event_id, {'method': method, 'params': params}))
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return dict(self.state)

    def read(self):
        try:
            for line in self.process.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                method = message.get('method')
                params = message.get('params') or {}
                if not method and 'id' in message:
                    with self.condition:
                        entry = self.pending.get(message['id'])
                        if entry is not None:
                            entry.update(done=True, response=message)
                            self.condition.notify_all()
                    continue
                if 'id' in message and method:
                    if params.get('threadId') and params['threadId'] != self.state['threadId']:
                        self.send({'id': message['id'], 'error': {'code': -32601,
                                   'message': 'Die angefragte Sitzung ist nicht mehr aktiv.'}})
                        continue
                    if method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval'):
                        if method == 'item/fileChange/requestApproval':
                            params['changes'] = self.file_changes.get(params.get('itemId'))
                        with self.condition:
                            self.approvals[str(message['id'])] = message
                        self.emit('approval/request', {'id': message['id'], 'method': method, 'params': params})
                    else:
                        # Unsupported interactive tools must fail closed, never auto-approve.
                        self.send({'id': message['id'], 'error': {'code': -32601,
                                   'message': 'Diese Anfrage benötigt einen vollständigen Codex-Client.'}})
                        self.emit('client/notice', {'message': 'Eine interaktive Tool-Anfrage benötigt die Terminaloberfläche.'})
                    continue
                # Subagent and previous-thread notifications must not change this session.
                if params.get('threadId') and params['threadId'] != self.state['threadId']:
                    continue
                changed = False
                with self.condition:
                    if method == 'item/started' and (params.get('item') or {}).get('type') == 'fileChange':
                        item = params['item']
                        self.file_changes[item['id']] = item.get('changes')
                    elif method == 'serverRequest/resolved':
                        self.approvals.pop(str(params.get('requestId')), None)
                    elif method == 'thread/realtime/sdp':
                        self.answer = params.get('sdp')
                        self.condition.notify_all()
                    elif method == 'thread/realtime/error':
                        self.voice_error = params.get('message') or params.get('error') or 'Sprachverbindung fehlgeschlagen.'
                        self.state.update(voiceActive=False, voiceStatus='error')
                        self.condition.notify_all()
                        changed = True
                    elif method == 'thread/realtime/started':
                        self.state.update(voiceActive=True, voiceStatus='connecting')
                        changed = True
                    elif method in ('thread/realtime/stopped', 'thread/realtime/closed'):
                        self.state.update(voiceActive=False, voiceStatus='idle')
                        changed = True
                    elif method == 'turn/started':
                        self.state['activeTurnId'] = (params.get('turn') or {}).get('id')
                        changed = True
                    elif method == 'turn/completed':
                        self.state['activeTurnId'] = None
                        changed = True
                if method and method != 'thread/realtime/sdp':
                    if method.startswith(('thread/realtime/', 'turn/', 'item/agentMessage/', 'item/started', 'item/completed', 'serverRequest/resolved', 'error')):
                        self.emit(method, params)
                if changed:
                    self.emit('state', self.snapshot())
        finally:
            with self.condition:
                self.closed = True
                self.condition.notify_all()
            self.emit('client/notice', {'message': 'Codex wurde beendet. Der lokale Server muss neu gestartet werden.'})

    def session(self, data):
        cwd = Path(data.get('cwd') or self.state['cwd']).expanduser().resolve()
        if not cwd.is_dir():
            raise RequestError('Der Projektordner existiert nicht.')
        effort = data.get('effort', 'high')
        if effort not in EFFORTS:
            raise RequestError('Unbekannte Denkstufe.')
        permission_mode = data.get('permissionMode', self.default_permission_mode)
        if not isinstance(permission_mode, str) or permission_mode not in PERMISSION_MODES:
            raise RequestError('Unbekannter Freigabemodus.')
        with self.session_lock:
            if self.state['voiceActive'] or self.state['activeTurnId']:
                raise RequestError('Bitte zuerst die aktuelle Sprachverbindung bzw. Aufgabe beenden.')
            if self.approvals:
                raise RequestError('Bitte zuerst die offene Freigabe beantworten.')
            params = {'cwd': str(cwd), 'model': self.state['model'], **PERMISSION_MODES[permission_mode],
                      'config': {'model_reasoning_effort': effort},
                      'developerInstructions': COMPUTER_USE_INSTRUCTIONS}
            thread_id = data.get('threadId')
            if thread_id:
                params.update(threadId=str(thread_id), excludeTurns=True)
                result = self.rpc('thread/resume', params)
            else:
                result = self.rpc('thread/start', params)
            with self.condition:
                self.state.update(threadId=result['thread']['id'], cwd=str(cwd), effort=effort,
                                  activeTurnId=None, voiceActive=False, voiceStatus='idle',
                                  permissionMode=permission_mode)
                self.file_changes.clear()
            self.emit('state', self.snapshot())
            return self.snapshot()

    def require_thread(self):
        ident = self.state['threadId']
        if not ident:
            raise RequestError('Bitte zuerst eine Aufgabe öffnen.')
        return ident

    def start_voice(self, data):
        sdp = data.get('sdp')
        if not isinstance(sdp, str) or not sdp.startswith('v=0') or len(sdp) > 100000:
            raise RequestError('Ungültiges Verbindungsangebot.')
        with self.session_lock:
            ident = self.require_thread()
            if self.state['voiceActive']:
                raise RequestError('Eine Sprachverbindung ist bereits aktiv.')
            with self.condition:
                self.answer = self.voice_error = None
                self.state.update(voiceActive=True, voiceStatus='connecting')
            try:
                self.rpc('thread/realtime/start', {'threadId': ident, 'outputModality': 'audio', 'version': 'v3',
                         'transport': {'type': 'webrtc', 'sdp': sdp}})
                with self.condition:
                    ready = self.condition.wait_for(lambda: self.answer or self.voice_error or self.closed, 45)
                    answer, error = self.answer, self.voice_error
                if not ready or error or not answer:
                    raise RequestError(str(error or 'Die Sprachverbindung konnte nicht hergestellt werden.'))
            except Exception:
                try:
                    self.stop_voice()
                except Exception:
                    pass
                raise
            return {'sdp': answer, 'threadId': ident}

    def stop_voice(self):
        with self.session_lock:
            ident = self.state['threadId']
            if ident and (self.state['voiceActive'] or self.state['voiceStatus'] != 'idle') and not self.closed:
                self.rpc('thread/realtime/stop', {'threadId': ident}, timeout=15)
            with self.condition:
                self.state.update(voiceActive=False, voiceStatus='idle')
            self.emit('state', self.snapshot())
            return self.snapshot()

    def interrupt(self):
        if self.state['threadId'] and self.state['activeTurnId']:
            self.rpc('turn/interrupt', {'threadId': self.state['threadId'], 'turnId': self.state['activeTurnId']})
        return self.snapshot()

    def text(self, data):
        text = data.get('text', '')
        if not isinstance(text, str):
            raise RequestError('Ungültige Nachricht.')
        text = text.strip()
        if not text or len(text) > 30000:
            raise RequestError('Bitte eine Nachricht mit höchstens 30.000 Zeichen eingeben.')
        params = {'threadId': self.require_thread(), 'input': [{'type': 'text', 'text': text}]}
        if self.state['activeTurnId']:
            params['expectedTurnId'] = self.state['activeTurnId']
            self.rpc('turn/steer', params)
        else:
            params['effort'] = self.state['effort']
            self.rpc('turn/start', params)
        return self.snapshot()

    def approve(self, data):
        decision = data.get('decision')
        if decision not in ('accept', 'acceptForSession', 'always', 'decline', 'cancel'):
            raise RequestError('Ungültige Freigabe.')
        with self.condition:
            request = self.approvals.get(str(data.get('id')))
            if request is None:
                raise RequestError('Diese Freigabe ist nicht mehr offen.')
            params = request.get('params', {})
            if decision == 'always':
                proposal = params.get('proposedExecpolicyAmendment')
                if (request['method'] != 'item/commandExecution/requestApproval'
                        or not isinstance(proposal, list) or not proposal
                        or not all(isinstance(part, str) and part for part in proposal)):
                    raise RequestError('Für diese Anfrage gibt es keine dauerhafte Befehlsregel.')
                # The browser chooses a scope, never the contents of a persistent rule.
                decision = {'acceptWithExecpolicyAmendment': {'execpolicy_amendment': proposal}}
            available = params.get('availableDecisions')
            if available is not None and decision not in available:
                raise RequestError('Diese Antwort ist für die Freigabe nicht verfügbar.')
            if decision in ('accept', 'acceptForSession') and request['method'] == 'item/fileChange/requestApproval' and not params.get('changes'):
                raise RequestError('Dateiänderungen müssen zuerst im vollständigen Codex-Client geprüft werden.')
            self.send({'id': request['id'], 'result': {'decision': decision}})
            self.approvals.pop(str(data.get('id')), None)
        return {'ok': True}

    def close(self):
        if self.state['voiceActive'] and not self.closed:
            try:
                self.stop_voice()
            except Exception:
                pass
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass  # Never log SDP, conversation content or request bodies.

    def authorized(self, mutation=False):
        port = self.server.server_port
        hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        if self.headers.get('Host') not in hosts:
            return False
        origin = self.headers.get('Origin')
        return not mutation or origin in {f'http://{host}' for host in hosts}

    def reply(self, status, data, mime='application/json'):
        raw = json.dumps(data, ensure_ascii=False).encode() if mime == 'application/json' else data
        self.send_response(status)
        self.send_header('Content-Type', mime + '; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; media-src 'self' blob:; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self.authorized():
            return self.reply(403, {'error': 'Unzulässiger Host.'})
        path = urlsplit(self.path).path
        if path == '/api/state':
            return self.reply(200, self.server.codex.snapshot())
        if path == '/api/events':
            return self.stream()
        names = {'/': 'index.html', '/index.html': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}
        name = names.get(path)
        if name is None:
            return self.reply(404, {'error': 'Nicht gefunden.'})
        p = ROOT / 'static' / name
        if not p.is_file():
            return self.reply(503, {'error': 'Oberfläche noch nicht verfügbar.'})
        mime = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css'}[p.suffix]
        self.reply(200, p.read_bytes(), mime)

    def stream(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        codex = self.server.codex
        try:
            last = int(self.headers.get('Last-Event-ID', '0'))
        except ValueError:
            last = 0
        try:
            while not codex.closed:
                with codex.condition:
                    codex.condition.wait_for(lambda: codex.event_id > last or codex.closed, timeout=12)
                    events = [(i, event) for i, event in codex.events if i > last]
                if not events:
                    self.wfile.write(b': keepalive\n\n')
                for ident, event in events:
                    self.wfile.write(f'id: {ident}\ndata: {json.dumps(event)}\n\n'.encode())
                    last = ident
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if not self.authorized(mutation=True):
            return self.reply(403, {'error': 'Nur die lokale Sprachoberfläche darf diese Aktion auslösen.'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 <= length <= 150000:
                return self.reply(413, {'error': 'Anfrage zu groß.'})
            data = json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(data, dict):
                raise ValueError()
            codex = self.server.codex
            actions = {'/api/session': lambda: codex.session(data),
                       '/api/voice/start': lambda: codex.start_voice(data),
                       '/api/voice/stop': codex.stop_voice, '/api/interrupt': codex.interrupt,
                       '/api/text': lambda: codex.text(data), '/api/approval': lambda: codex.approve(data)}
            action = actions.get(urlsplit(self.path).path)
            if action is None:
                return self.reply(404, {'error': 'Nicht gefunden.'})
            self.reply(200, action())
        except (ValueError, TypeError):
            self.reply(400, {'error': 'Ungültige Anfrage.'})
        except RequestError as exc:
            self.reply(409, {'error': str(exc)[:1000]})
        except Exception:
            self.reply(500, {'error': 'Die lokale Codex-Verbindung ist fehlgeschlagen.'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--cwd', default=str(Path.cwd()))
    parser.add_argument('--model', default='gpt-6-astra', help='Codex model for tasks (voice uses its own model)')
    parser.add_argument('--yolo', action='store_true', help='Default new sessions to full access without approval prompts')
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    try:
        codex = Codex(args.cwd, model=args.model, permission_mode='yolo' if args.yolo else 'ask')
    except Exception:
        server.server_close()
        raise
    server.codex = codex
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    print(f'Codex Voice: http://127.0.0.1:{server.server_port}', flush=True)
    print('Mikrofon startet erst nach Klick im Browser. Beenden mit Ctrl+C.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        codex.close()


if __name__ == '__main__':
    main()
