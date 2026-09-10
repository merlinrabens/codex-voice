#!/usr/bin/env python3
"""Local WebRTC voice frontend for Codex's experimental app-server."""
import argparse
import collections
import hashlib
import hmac
import json
import os
from pathlib import Path
import queue
import secrets
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie, CookieError
from urllib.parse import urlsplit, parse_qs
from memory import VoiceMemory
from control import LocalPairingControl, write_private_json
from persona import load_persona, persona_instructions

ROOT = Path(__file__).resolve().parent
EFFORTS = {'low', 'medium', 'high', 'xhigh', 'ultra'}
# Voices accepted by the Realtime V3 backend used by this client.
# The shared Codex RealtimeVoice enum also includes voices unsupported by V3.
VOICE_OPTIONS = ('arbor', 'breeze', 'cove', 'ember', 'juniper', 'maple',
                 'sol', 'spruce', 'vale')
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


class RemoteAccess:
    """One-use pairing and process-local sessions; credentials never enter Codex."""
    cookie_name = '__Host-codex_voice'
    session_seconds = 12 * 60 * 60
    remembered_seconds = 30 * 24 * 60 * 60

    def __init__(self, origin, pairing_code=None):
        parsed = urlsplit(origin)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                or parsed.port not in (None, 443)):
            raise ValueError('Remote origin must be an exact HTTPS origin.')
        self.origin = 'https://' + parsed.netloc
        self.host = parsed.netloc
        self.pairing_code = pairing_code or secrets.token_urlsafe(24)
        self.pairing_expires = time.monotonic() + 30 * 60
        self.sessions = {}
        self.last_activity = time.monotonic()
        self.attempts = collections.deque()
        self.lock = threading.Lock()

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def session_key(self, cookie_header):
        try:
            cookie = SimpleCookie()
            cookie.load(cookie_header or '')
            value = cookie[self.cookie_name].value if self.cookie_name in cookie else ''
            return self.digest(value) if value else None
        except (CookieError, ValueError):
            return None

    def valid(self, cookie_header):
        key = self.session_key(cookie_header)
        with self.lock:
            now = time.monotonic()
            self.sessions = {k: expiry for k, expiry in self.sessions.items() if expiry > now}
            if key in self.sessions:
                self.last_activity = now
                return True
            return False

    def voice_lease_active(self):
        with self.lock:
            now = time.monotonic()
            return any(expiry > now for expiry in self.sessions.values()) and now - self.last_activity < 90

    def pair(self, code, remember=False):
        if not isinstance(remember, bool):
            raise ValueError('Remember-device preference must be a boolean.')
        with self.lock:
            now = time.monotonic()
            while self.attempts and self.attempts[0] <= now - 60:
                self.attempts.popleft()
            if len(self.attempts) >= 12:
                return None
            self.attempts.append(now)
            if (not isinstance(code, str) or not self.pairing_code or now >= self.pairing_expires
                    or not hmac.compare_digest(code.encode(), self.pairing_code.encode())):
                return None
            self.pairing_code = None
            token = secrets.token_urlsafe(32)
            duration = self.remembered_seconds if remember else self.session_seconds
            self.sessions[self.digest(token)] = now + duration
            return token

    def renew_pairing(self, write_receipt):
        """Issue a new local pairing invitation without revoking browser sessions."""
        with self.lock:
            code = secrets.token_urlsafe(24)
            # A failed receipt write must leave the existing invitation usable.
            connection = write_receipt(code)
            self.pairing_code = code
            self.pairing_expires = time.monotonic() + 30 * 60
            self.attempts.clear()
            return connection

    def revoke(self, cookie_header):
        with self.lock:
            self.sessions.pop(self.session_key(cookie_header), None)


class Codex:
    memory_index_retry_seconds = 1.0
    memory_index_coalesce_seconds = 0.15

    def __init__(self, cwd, model='gpt-6-astra', permission_mode='ask', memory=None, memory_index_command=None,
                 assistant=None, voice='default'):
        if permission_mode not in PERMISSION_MODES:
            raise RequestError('Unbekannter Freigabemodus.')
        if not isinstance(voice, str) or voice not in ('default', *VOICE_OPTIONS):
            raise RequestError('Unknown voice selection.')
        self.assistant = assistant if assistant is not None else load_persona()
        self.persona_instructions = persona_instructions(self.assistant)
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
        self.memory = memory
        self.memory_index_command = memory_index_command
        self.memory_index_process = None
        self.memory_index_dirty = False
        self.memory_index_requested = False
        self.memory_index_not_before = 0
        self.memory_index_shutdown = False
        self.memory_index_thread = None
        self.memory_queue = queue.Queue()
        self.memory_failed = []
        self.memory_thread = None
        self.known_threads = {}
        self.state = {'threadId': None, 'cwd': str(Path(cwd).expanduser().resolve()),
                      'model': model, 'effort': 'high', 'voiceActive': False,
                      'permissionMode': permission_mode,
                      'assistantName': self.assistant['name'],
                      'assistantAge': self.assistant.get('persona_age'),
                      'voice': voice, 'voiceOptions': list(VOICE_OPTIONS),
                      'activeTurnId': None, 'voiceStatus': 'idle'}
        if memory:
            self.state.update(memoryEnabled=True, memoryServerTranscripts=True, memoryStatus='ready')
            self.memory_thread = threading.Thread(target=self.memory_worker, daemon=True)
            self.memory_thread.start()
            if memory_index_command:
                self.state['memoryIndexStatus'] = 'ready'
                self.memory_index_thread = threading.Thread(target=self.memory_index_worker, daemon=True)
                self.memory_index_thread.start()
        self.process = subprocess.Popen(
            ['codex', 'app-server', '--enable', 'realtime_conversation'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        self.reader_thread = threading.Thread(target=self.read, daemon=True)
        self.reader_thread.start()
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

    def memory_worker(self):
        while True:
            item = self.memory_queue.get()
            try:
                if item is None:
                    return
                self.memory_recorded(self.memory.record(*item), item[0])
            except Exception:
                with self.condition:
                    self.memory_failed.append(item)
                    self.state['memoryStatus'] = 'error'
                self.emit('state', self.snapshot())
            finally:
                self.memory_queue.task_done()

    def memory_recorded(self, result, thread_id):
        with self.condition:
            if result.get('saved'):
                if not self.memory_index_dirty:
                    self.memory_index_not_before = time.monotonic() + self.memory_index_coalesce_seconds
                self.memory_index_dirty = True
                if thread_id != self.state['threadId'] or not self.state['voiceActive']:
                    # Completed conversations must remain eligible even if another
                    # voice session starts before the coalescing delay expires.
                    self.memory_index_requested = True
                if self.memory_index_command and self.state.get('memoryIndexStatus') != 'indexing':
                    self.state['memoryIndexStatus'] = 'pending'
            self.state['memoryStatus'] = 'error' if self.memory_failed else 'saved'
            self.condition.notify_all()
        self.emit('state', self.snapshot())

    def memory_index_worker(self):
        """Serialize indexing and retain a follow-up for writes made during a run."""
        failures = 0
        while True:
            with self.condition:
                while True:
                    if self.memory_index_shutdown and not self.memory_index_dirty:
                        return
                    remaining = self.memory_index_not_before - time.monotonic()
                    eligible = self.memory_index_requested or not self.state['voiceActive'] or self.memory_index_shutdown
                    if self.memory_index_dirty and eligible and remaining <= 0:
                        break
                    self.condition.wait(timeout=max(0.01, min(1.0, remaining)) if remaining > 0 else 1.0)
                # Clear only the generation being started. Later writes set dirty again.
                self.memory_index_dirty = False
                self.memory_index_requested = False
                self.state['memoryIndexStatus'] = 'indexing'
            self.emit('state', self.snapshot())
            try:
                process = subprocess.Popen(self.memory_index_command, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                with self.condition:
                    self.memory_index_process = process
                while process.poll() is None:
                    with self.condition:
                        self.condition.wait(timeout=0.1)
                succeeded = process.wait() == 0
            except Exception:
                succeeded = False
            with self.condition:
                self.memory_index_process = None
                if succeeded:
                    failures = 0
                    # A new conversation can already be active. Its pending writes
                    # still need the follow-up promised by the previous flush.
                    self.memory_index_requested = self.memory_index_dirty
                    self.state['memoryIndexStatus'] = 'pending' if self.memory_index_dirty else 'indexed'
                else:
                    failures += 1
                    self.memory_index_dirty = True
                    self.memory_index_requested = True
                    delay = min(60.0, self.memory_index_retry_seconds * 2 ** min(failures - 1, 6))
                    self.memory_index_not_before = time.monotonic() + delay
                    self.state['memoryIndexStatus'] = 'error'
                stopping_after_failure = self.memory_index_shutdown and not succeeded
                self.condition.notify_all()
            self.emit('state', self.snapshot())
            if stopping_after_failure:
                return

    def capture(self, role, text, item_id, thread_id=None):
        if not self.memory or not isinstance(text, str) or not text.strip():
            return
        ident = thread_id or self.state['threadId']
        if role not in ('user', 'assistant') or ident not in self.known_threads:
            return
        with self.condition:
            self.state['memoryStatus'] = 'saving'
        self.memory_queue.put((ident, self.known_threads[ident], role, text, str(item_id)))

    def flush_memory(self):
        if not self.memory:
            return
        with self.condition:
            failed, self.memory_failed = self.memory_failed, []
        for item in failed:
            self.memory_queue.put(item)
        deadline = time.monotonic() + 3
        while self.memory_queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self.memory_queue.unfinished_tasks and not self.memory_failed:
            self.memory.flush()
            with self.condition:
                if self.memory_index_dirty:
                    self.memory_index_requested = True
                    self.memory_index_not_before = 0
                self.condition.notify_all()

    def save_transcript(self, data):
        ident, role, text, item_id = (data.get(k) for k in ('threadId', 'role', 'text', 'itemId'))
        if (not self.memory or ident not in self.known_threads or role not in ('user', 'assistant')
                or not isinstance(text, str) or not text.strip() or len(text) > 60000
                or not isinstance(item_id, str) or not item_id or len(item_id) > 256):
            raise RequestError('Ungültiger Gesprächseintrag.')
        # Browser fallbacks are acknowledged only after their durable write.
        try:
            self.memory_recorded(self.memory.record(ident, self.known_threads[ident], role, text, item_id), ident)
        except Exception:
            with self.condition:
                self.state['memoryStatus'] = 'error'
            self.emit('state', self.snapshot())
            raise
        return {'saved': True}

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
                # Archive known original threads before filtering UI notifications.
                # A finalized transcript can arrive after a new session is opened.
                source_thread = params.get('threadId')
                if method == 'thread/realtime/transcript/done':
                    if source_thread in self.known_threads:
                        self.capture(params.get('role'), params.get('text'),
                                     params.get('itemId') or f'voice-{time.time_ns()}', source_thread)
                elif method == 'item/completed' and (params.get('item') or {}).get('type') == 'agentMessage':
                    item = params['item']
                    if source_thread in self.known_threads:
                        self.capture('assistant', item.get('text'), item.get('id') or f'task-{time.time_ns()}', source_thread)
                # Subagent and previous-thread notifications must not change this session.
                if source_thread and source_thread != self.state['threadId']:
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
                      'developerInstructions': COMPUTER_USE_INSTRUCTIONS + '\n\n' + self.persona_instructions}
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
                self.known_threads[self.state['threadId']] = str(cwd)
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
        selected_voice = data.get('voice', self.state.get('voice', 'default'))
        if not isinstance(selected_voice, str) or selected_voice not in ('default', *VOICE_OPTIONS):
            raise RequestError('Unknown voice selection. Choose a listed voice or the provider default.')
        with self.session_lock:
            ident = self.require_thread()
            if self.state['voiceActive']:
                raise RequestError('Eine Sprachverbindung ist bereits aktiv.')
            with self.condition:
                self.answer = self.voice_error = None
                self.state.update(voiceActive=True, voiceStatus='connecting', voice=selected_voice)
            try:
                params = {'threadId': ident, 'outputModality': 'audio', 'version': 'v3',
                          'transport': {'type': 'webrtc', 'sdp': sdp},
                          'initialItems': [{'role': 'developer', 'text': self.persona_instructions}],
                          'realtimeStartInstructions': self.persona_instructions}
                if selected_voice != 'default':
                    params['voice'] = selected_voice
                self.rpc('thread/realtime/start', params)
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
            self.flush_memory()
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
        self.capture('user', text, f'text-{time.time_ns()}', params['threadId'])
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
        # Drain final protocol notifications before closing the archive worker.
        if threading.current_thread() is not self.reader_thread:
            self.reader_thread.join(timeout=3)
        if self.memory_thread and self.memory_thread.is_alive():
            self.flush_memory()
            self.memory_queue.put(None)
            self.memory_thread.join(timeout=3)
            if not self.memory_thread.is_alive():
                self.memory.close()
        if self.memory_index_thread and self.memory_index_thread.is_alive():
            with self.condition:
                self.memory_index_shutdown = True
                self.memory_index_requested = True
                self.memory_index_not_before = 0
                self.condition.notify_all()
            self.memory_index_thread.join(timeout=3)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def handle(self):
        try:
            super().handle()
        except (TimeoutError, BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def log_message(self, *_):
        pass  # Never log SDP, conversation content or request bodies.

    def authorized(self, mutation=False):
        port = self.server.server_port
        hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        remote = getattr(self.server, 'remote_access', None)
        host = self.headers.get('Host')
        if remote:
            hosts.add(remote.host)
        if len(self.headers.get_all('Host', [])) != 1 or host not in hosts:
            return False
        if remote and host == remote.host and self.headers.get('X-Forwarded-Proto', 'https') != 'https':
            return False
        origin = self.headers.get('Origin')
        expected = remote.origin if remote and host == remote.host else f'http://{host}'
        return not mutation or (len(self.headers.get_all('Origin', [])) == 1 and origin == expected)

    def authenticated(self):
        remote = getattr(self.server, 'remote_access', None)
        return not remote or remote.valid(self.headers.get('Cookie'))

    def reply(self, status, data, mime='application/json', headers=None):
        raw = json.dumps(data, ensure_ascii=False).encode() if mime == 'application/json' else data
        self.send_response(status)
        self.send_header('Content-Type', mime + '; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Permissions-Policy', 'camera=(), microphone=(self)')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; media-src 'self' blob:; frame-ancestors 'none'")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self.authorized():
            return self.reply(403, {'error': 'Unzulässiger Host.'})
        path = urlsplit(self.path).path
        # A cross-site navigation can withhold a SameSite=Strict cookie for the
        # HTML request, then include it on same-origin assets. These public
        # assets must load in either state so pair.js can recover the session.
        pair_assets = {'/pair.js': ('pair.js', 'text/javascript'),
                       '/pair.css': ('pair.css', 'text/css')}
        if path in pair_assets:
            name, mime = pair_assets[path]
            return self.reply(200, (ROOT / 'static' / name).read_bytes(), mime)
        if not self.authenticated():
            public = {'/': ('pair.html', 'text/html'), '/index.html': ('pair.html', 'text/html'),
                      '/pair.js': ('pair.js', 'text/javascript'), '/pair.css': ('pair.css', 'text/css')}
            if path in public:
                name, mime = public[path]
                return self.reply(200, (ROOT / 'static' / name).read_bytes(), mime)
            return self.reply(401, {'error': 'Bitte dieses Gerät erneut koppeln.'})
        if path == '/api/state':
            state = self.server.codex.snapshot()
            if getattr(self.server, 'remote_access', None):
                state.update(clientTransport='poll')
            return self.reply(200, state)
        if path == '/api/events/poll':
            return self.poll_events()
        if path == '/api/events':
            if getattr(self.server, 'remote_access', None):
                return self.reply(400, {'error': 'Diese Verbindung verwendet Ereignis-Polling.'})
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

    def poll_events(self):
        try:
            query = parse_qs(urlsplit(self.path).query)
            after = max(0, int(query.get('after', ['0'])[0]))
            wait = min(20, max(0, int(query.get('wait', ['20'])[0])))
        except (ValueError, TypeError):
            return self.reply(400, {'error': 'Ungültiger Ereignis-Cursor.'})
        codex = self.server.codex
        with codex.condition:
            codex.condition.wait_for(lambda: codex.event_id > after or codex.closed, timeout=wait)
            events = [{'id': i, **event} for i, event in codex.events if i > after]
            cursor = codex.event_id
        # A cookie may have expired or been revoked while the poll was waiting.
        if not self.authenticated():
            return self.reply(401, {'error': 'Bitte dieses Gerät erneut koppeln.'})
        return self.reply(200, {'events': events, 'cursor': cursor, 'state': codex.snapshot()})

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
            return self.reply(403, {'error': 'Nur die zugehörige Sprachoberfläche darf diese Aktion auslösen.'})
        path = urlsplit(self.path).path
        remote = getattr(self.server, 'remote_access', None)
        if not self.authenticated() and not (remote and path == '/api/pair'):
            self.close_connection = True
            return self.reply(401, {'error': 'Bitte dieses Gerät erneut koppeln.'})
        try:
            if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) > 1:
                self.close_connection = True
                return self.reply(400, {'error': 'Ungültige Anfrage.'})
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 <= length <= (4096 if path == '/api/pair' else 150000):
                self.close_connection = True
                return self.reply(413, {'error': 'Anfrage zu groß.'})
            data = json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(data, dict):
                raise ValueError()
            if remote and path == '/api/pair':
                remember = data.get('remember', False)
                if not isinstance(remember, bool):
                    raise ValueError('Remember-device preference must be a boolean.')
                token = remote.pair(data.get('code'), remember=remember)
                if not token:
                    return self.reply(403, {'error': 'Kopplungscode ungültig, abgelaufen oder bereits verwendet.'})
                duration = remote.remembered_seconds if remember else remote.session_seconds
                cookie = f'{remote.cookie_name}={token}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={duration}'
                return self.reply(200, {'ok': True}, headers={'Set-Cookie': cookie})
            if remote and path == '/api/logout':
                remote.revoke(self.headers.get('Cookie'))
                cookie = f'{remote.cookie_name}=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0'
                return self.reply(200, {'ok': True}, headers={'Set-Cookie': cookie})
            codex = self.server.codex
            actions = {'/api/session': lambda: codex.session(data),
                       '/api/voice/start': lambda: codex.start_voice(data),
                       '/api/voice/stop': codex.stop_voice, '/api/interrupt': codex.interrupt,
                       '/api/text': lambda: codex.text(data), '/api/approval': lambda: codex.approve(data),
                       '/api/memory/transcript': lambda: codex.save_transcript(data)}
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
    parser.add_argument('--assistant-name', help='Assistant display and conversational name')
    parser.add_argument('--assistant-profile', help='Optional private JSON persona file outside this checkout')
    parser.add_argument('--voice', default='default', choices=('default', *VOICE_OPTIONS), help='Initial Realtime voice selection')
    parser.add_argument('--yolo', action='store_true', help='Default new sessions to full access without approval prompts')
    parser.add_argument('--remote-origin', help='Exact HTTPS tunnel origin; enables pairing on every route')
    parser.add_argument('--pairing-file', help='Private connection receipt outside the source checkout')
    parser.add_argument('--memory-dir', help='Opt-in private Markdown conversation archive outside this checkout')
    parser.add_argument('--memory-index-script', help='Optional local Python indexer to trigger after voice ends')
    parser.add_argument('--memory-index-python', default=sys.executable, help='Python interpreter for the optional indexer')
    args = parser.parse_args()
    if bool(args.remote_origin) != bool(args.pairing_file):
        parser.error('--remote-origin and --pairing-file must be used together')
    assistant = load_persona(args.assistant_profile, args.assistant_name)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.daemon_threads = True
    if args.remote_origin:
        server.remote_access = RemoteAccess(args.remote_origin)
    try:
        memory = VoiceMemory(args.memory_dir) if args.memory_dir else None
        index_command = [args.memory_index_python, str(Path(args.memory_index_script).expanduser().resolve())] if args.memory_index_script else None
        codex = Codex(args.cwd, model=args.model, permission_mode='yolo' if args.yolo else 'ask',
                      memory=memory, memory_index_command=index_command, assistant=assistant, voice=args.voice)
    except Exception:
        server.server_close()
        raise
    server.codex = codex
    if args.remote_origin:
        def watch_voice_lease():
            while not codex.closed:
                time.sleep(5)
                if codex.snapshot()['voiceActive'] and not server.remote_access.voice_lease_active():
                    try:
                        codex.stop_voice()
                    except Exception:
                        pass
        threading.Thread(target=watch_voice_lease, daemon=True).start()
    pairing_control = None
    if args.remote_origin:
        receipt_argument = Path(args.pairing_file).expanduser().absolute()
        if receipt_argument.is_symlink():
            codex.close()
            server.server_close()
            raise ValueError('The private pairing receipt must not be a symlink.')
        receipt = receipt_argument.parent.resolve() / receipt_argument.name
        if receipt == ROOT or ROOT in receipt.parents:
            codex.close()
            server.server_close()
            raise ValueError('Keep the private pairing receipt outside the source checkout.')
        receipt.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        def write_receipt(code):
            connection = {'origin': server.remote_access.origin,
                          'pairing_url': server.remote_access.origin + '/#pair=' + code,
                          'pairing_valid_seconds': 1800, 'server_pid': os.getpid(),
                          'issued_at': time.time(), 'permission_mode': codex.default_permission_mode}
            write_private_json(receipt, connection)
            return connection
        try:
            pairing_control = LocalPairingControl(
                receipt.parent / 'control.sock',
                lambda: server.remote_access.renew_pairing(write_receipt))
            # Reserve local control before replacing another service's receipt.
            write_receipt(server.remote_access.pairing_code)
            pairing_control.start()
        except Exception:
            if pairing_control:
                pairing_control.close()
            codex.close()
            server.server_close()
            raise
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    print(f'Codex Voice: http://127.0.0.1:{server.server_port}', flush=True)
    print('Mikrofon startet erst nach Klick im Browser. Beenden mit Ctrl+C.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if pairing_control:
            pairing_control.close()
        server.server_close()
        codex.close()


if __name__ == '__main__':
    main()
