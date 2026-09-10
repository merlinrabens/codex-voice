#!/usr/bin/env python3
"""Isolated UI smoke test. Uses no Codex server, microphone or model connection.

Run with a Python environment containing Playwright. A local Chrome installation
is used when available; otherwise Playwright's installed Chromium is used.
"""
import json
import mimetypes
import os
import re
from pathlib import Path
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parent
VOICE_OPTIONS = ['juniper', 'maple', 'spruce', 'ember', 'vale', 'breeze',
                 'arbor', 'sol', 'cove']
LEGACY_VOICE_OPTIONS = ['alloy', 'arbor', 'ash', 'ballad', 'breeze', 'cedar',
                        'coral', 'cove', 'echo', 'ember', 'juniper', 'maple',
                        'marin', 'sage', 'shimmer', 'sol', 'spruce', 'vale', 'verse']
BOOT = r"""
window.uiFixture = {micCalls:0, contexts:[], inputContexts:[], tracks:[], peers:[], channels:[], storageWrites:[], speakerConnections:0, audioPlays:[], actions:[], wakeLocks:[], visibility:'visible'};
const nativeFetch = window.fetch;
window.fetch = (...args) => { uiFixture.actions.push(String(args[0])); return nativeFetch(...args); };
const nativePlay = HTMLMediaElement.prototype.play;
HTMLMediaElement.prototype.play = function() {
  uiFixture.actions.push('audio.play');
  uiFixture.audioPlays.push({gesture:navigator.userActivation.isActive, source:this.src});
  return nativePlay.call(this);
};
Object.defineProperty(document, 'visibilityState', {get:() => uiFixture.visibility});
Object.defineProperty(navigator, 'wakeLock', {value:{request:async () => {
  const lock = new EventTarget(); lock.released = false;
  lock.release = async () => { lock.released = true; lock.dispatchEvent(new Event('release')); };
  uiFixture.wakeLocks.push(lock); return lock;
}}});
const nativeConnect = AudioNode.prototype.connect;
AudioNode.prototype.connect = function(destination, ...rest) {
  if (destination instanceof AudioDestinationNode) uiFixture.speakerConnections++;
  return nativeConnect.call(this, destination, ...rest);
};
const RealAudioContext = window.AudioContext;
window.AudioContext = class extends RealAudioContext {
  constructor(...args) { super(...args); uiFixture.contexts.push(this); uiFixture.actions.push('AudioContext'); }
};
const nativeSetItem = Storage.prototype.setItem;
Storage.prototype.setItem = function(key, value) {
  uiFixture.storageWrites.push({key, value});
  return nativeSetItem.call(this, key, value);
};
Object.defineProperty(navigator.mediaDevices, 'getUserMedia', {value: async () => {
  uiFixture.micCalls++;
  const context = new AudioContext();
  uiFixture.inputContexts.push(context);
  const oscillator = context.createOscillator();
  const gain = context.createGain();
  const destination = context.createMediaStreamDestination();
  oscillator.frequency.value = 220;
  gain.gain.value = 0;
  oscillator.connect(gain); gain.connect(destination); oscillator.start();
  await context.resume();
  uiFixture.gain = gain;
  uiFixture.inputContext = context;
  uiFixture.tracks.push(...destination.stream.getTracks());
  return destination.stream;
}});
window.RTCPeerConnection = class {
  constructor() { this.connectionState = 'new'; uiFixture.peers.push(this); }
  addTrack() {}
  createDataChannel() {
    const channel = new EventTarget(); channel.readyState = 'open';
    channel.close = () => { channel.readyState = 'closed'; };
    uiFixture.channels.push(channel); return channel;
  }
  async createOffer() { await uiFixture.offerGate; return {type:'offer',sdp:'v=0 ui-test'}; }
  async setLocalDescription(value) { this.localDescription = value; }
  async setRemoteDescription() { this.connectionState = 'connected'; this.onconnectionstatechange?.(); }
  close() { this.connectionState = 'closed'; }
};
"""


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, cwd):
        super().__init__(('127.0.0.1', 0), FixtureHandler)
        self.condition = threading.Condition()
        self.events = []
        self.requests = []
        self.running = True
        self.expired = False
        self.poll_requests = []
        self.poll_duplicate = None
        self.sse_requests = 0
        self.memory_failures = 0
        self.memory_records = []
        self.state = {'threadId': None, 'cwd': cwd, 'model': 'gpt-6-astra',
                      'effort': 'high', 'permissionMode': 'ask', 'voiceActive': False,
                      'activeTurnId': None, 'assistantName': 'Jerry',
                      'voice': 'default', 'voiceOptions': VOICE_OPTIONS}

    def emit(self, method, params):
        with self.condition:
            self.events.append({'id': len(self.events) + 1, 'method': method, 'params': params})
            self.condition.notify_all()


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def handle(self):
        try:
            super().handle()
        except ConnectionResetError:
            pass  # The isolated browser may close an idle keep-alive socket.

    def respond(self, body, kind='application/json', status=200):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith('/api/') and self.server.expired:
            return self.respond({'error': 'Pairing expired'}, status=401)
        if self.path == '/api/state':
            return self.respond(dict(self.server.state))
        if self.path.startswith('/api/events/poll?'):
            query = parse_qs(urlparse(self.path).query)
            after = int(query['after'][0])
            self.server.poll_requests.append(after)
            with self.server.condition:
                self.server.condition.wait_for(lambda: len(self.server.events) > after or not self.server.running or self.server.expired, .25)
                pending = self.server.events[after:]
                duplicate = self.server.poll_duplicate
                self.server.poll_duplicate = None
                cursor = len(self.server.events)
            if self.server.expired:
                return self.respond({'error': 'Pairing expired'}, status=401)
            if duplicate:
                pending = [duplicate] + pending
            try:
                return self.respond({'events': pending, 'cursor': cursor, 'state': dict(self.server.state)})
            except (BrokenPipeError, ConnectionResetError):
                return
        if self.path == '/api/events':
            self.server.sse_requests += 1
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            index = len(self.server.events)
            try:
                self.wfile.write(b': connected\n\n')
                self.wfile.flush()
                while self.server.running:
                    with self.server.condition:
                        self.server.condition.wait_for(
                            lambda: len(self.server.events) > index or not self.server.running, 1)
                        pending = self.server.events[index:]
                        index += len(pending)
                    for event in pending:
                        self.wfile.write(b'data: ' + json.dumps(event).encode() + b'\n\n')
                    if not pending:
                        self.wfile.write(b': heartbeat\n\n')
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        filename = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}.get(self.path)
        if not filename:
            self.send_error(404)
            return
        body = (ROOT / 'static' / filename).read_bytes()
        if filename == 'app.js':
            body = BOOT.encode() + b'\n' + body
        self.respond(body, mimetypes.guess_type(filename)[0] or 'text/plain')

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
        self.server.requests.append((self.path, data))
        if self.path == '/api/memory/transcript':
            with self.server.condition:
                if self.server.memory_failures:
                    self.server.memory_failures -= 1
                    return self.respond({'error': 'Temporary archive outage'}, status=503)
                self.server.memory_records.append(data)
                self.server.condition.notify_all()
            return self.respond({'saved': True})
        elif self.path == '/api/session':
            self.server.state.update(data, threadId=f'ui-thread-{len(self.server.requests)}')
            self.server.emit('state', dict(self.server.state))
        elif self.path == '/api/voice/start':
            self.server.state['voiceActive'] = True
            if 'voice' in data:
                self.server.state['voice'] = data['voice']
            return self.respond({'sdp': 'v=0 ui-answer', 'threadId': self.server.state['threadId']})
        elif self.path == '/api/voice/stop':
            self.server.state['voiceActive'] = False
            self.server.emit('thread/realtime/closed', {})
        self.respond(dict(self.server.state))


def run():
    with tempfile.TemporaryDirectory(prefix='codex-voice-ui-') as directory:
        server = FixtureServer(directory)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        report = []
        try:
            with sync_playwright() as playwright:
                chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
                launch = {'headless': True, 'args': ['--autoplay-policy=no-user-gesture-required']}
                if chrome.exists():
                    launch['executable_path'] = str(chrome)
                browser = playwright.chromium.launch(**launch)
                url = f'http://127.0.0.1:{server.server_port}'
                named_context = browser.new_context(viewport={'width': 390, 'height': 844})
                named = named_context.new_page()
                named_errors = []
                named.on('pageerror', lambda error: named_errors.append(str(error)))
                named.goto(url)
                expect(named.locator('#connection-status')).to_contain_text('Lokal verbunden')
                expect(named).to_have_title('Jerry · Voice')
                expect(named.locator('.wordmark')).to_have_attribute('aria-label', 'Jerry Voice home')
                expect(named.locator('#message')).to_have_attribute('placeholder', 'Or write to Jerry…')
                expect(named.locator('#interrupt')).to_have_text('Interrupt Jerry')
                expect(named.locator('#voice-field')).to_be_visible()
                expect(named.locator('#voice option')).to_have_count(10)
                expect(named.locator('#voice')).to_have_value('default')
                assert named.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert named.locator('#voice').evaluate('element => getComputedStyle(element).fontSize') == '16px'
                assert named.evaluate('uiFixture.micCalls') == 0
                assert named.evaluate('uiFixture.storageWrites.length') == 0
                server.emit('item/agentMessage/delta', {'itemId': 'name-test', 'delta': 'An assistant response.'})
                expect(named.locator('[data-assistant-message]')).to_have_text('Jerry')
                server.emit('approval/request', {'id': 'name-approval', 'params': {'command': 'git status'}})
                expect(named.locator('.approval-card h3')).to_have_text('Jerry needs your approval')
                injected_name = '<img src=x onerror="window.nameExecuted=true">'
                server.state.update(assistantName=injected_name, activeTurnId='name-test-turn')
                server.emit('state', dict(server.state))
                expect(named).to_have_title(f'{injected_name} · Voice')
                expect(named.locator('[data-assistant-message]')).to_have_text(injected_name)
                expect(named.locator('.approval-card h3')).to_have_text(f'{injected_name} needs your approval')
                expect(named.locator('img')).to_have_count(0)
                assert named.evaluate('window.nameExecuted') is None
                server.emit('serverRequest/resolved', {'requestId': 'name-approval'})
                expect(named.locator('#work-status')).to_have_text(f'{injected_name} is working')
                server.state.update(assistantName='Jerry', activeTurnId=None)
                server.emit('state', dict(server.state))
                expect(named).to_have_title('Jerry · Voice')
                expect(named.locator('#model-label')).to_have_text('gpt-6-astra · high')
                report.append('custom assistant name updates accessible labels, existing messages and approvals without executing markup or changing model IDs')

                def last_voice_start():
                    return [body for path, body in server.requests if path == '/api/voice/start'][-1]

                sessions_before = len([path for path, _ in server.requests if path == '/api/session'])
                named.locator('#start-voice').click()
                expect(named.locator('#voice-control')).to_have_attribute('data-phase', 'connected')
                assert last_voice_start()['voice'] == 'default'
                original_thread = server.state['threadId']
                expect(named.locator('#voice')).to_be_disabled()
                named.locator('#mute-voice').click()
                expect(named.locator('#voice-detail')).to_have_text('You can still hear Jerry.')
                expect(named.locator('#voice')).to_be_disabled()
                named.locator('#stop-voice').click()
                expect(named.locator('#voice')).to_be_enabled()
                named.locator('#voice').select_option('ember')
                assert named.evaluate('uiFixture.storageWrites') == [{'key': 'codex-voice.voice', 'value': 'ember'}]
                named.evaluate('() => { uiFixture.offerGate = new Promise(resolve => { uiFixture.releaseOffer = resolve; }); }')
                named.locator('#start-voice').click()
                expect(named.locator('#voice-control')).to_have_attribute('data-phase', 'connecting')
                expect(named.locator('#voice')).to_be_disabled()
                named.evaluate('uiFixture.releaseOffer()')
                expect(named.locator('#voice-control')).to_have_attribute('data-phase', 'connected')
                assert last_voice_start()['voice'] == 'ember'
                assert server.state['threadId'] == original_thread
                named.locator('#stop-voice').click()
                expect(named.locator('#voice')).to_be_enabled()
                named.locator('#voice').select_option('default')
                named.locator('#start-voice').click()
                expect(named.locator('#voice-control')).to_have_attribute('data-phase', 'connected')
                assert last_voice_start()['voice'] == 'default'
                assert server.state['threadId'] == original_thread
                assert len([path for path, _ in server.requests if path == '/api/session']) == sessions_before + 1
                assert all('voice' not in body for path, body in server.requests if path == '/api/session')
                named.locator('#stop-voice').click()
                expect(named.locator('#voice')).to_be_enabled()
                named.reload()
                expect(named.locator('#voice')).to_have_value('default')
                named.locator('#voice').select_option('cove')
                named.reload()
                expect(named.locator('#voice')).to_have_value('cove')
                assert named.evaluate('uiFixture.storageWrites.length') == 0
                assert named.evaluate('uiFixture.micCalls') == 0
                screenshot_dir = os.environ.get('CODEX_VOICE_UI_SCREENSHOTS')
                if screenshot_dir:
                    destination = Path(screenshot_dir)
                    destination.mkdir(parents=True, exist_ok=True)
                    named.screenshot(path=str(destination / 'voice-choice-mobile.png'), full_page=True)
                report.append('voice choices preserve provider default, persist explicit selection, lock during connecting and active voice, and restart without creating another Codex thread')

                server.state.update(voice='alloy', voiceOptions=LEGACY_VOICE_OPTIONS)
                named.evaluate("localStorage.setItem('codex-voice.voice', 'alloy')")
                named.reload()
                expect(named.locator('#voice')).to_have_value('default')
                expect(named.locator('#voice option')).to_have_count(10)
                assert set(named.locator('#voice option').evaluate_all('options => options.map(option => option.value)')) == {'default', *VOICE_OPTIONS}
                expect(named.locator('#voice option[value="alloy"]')).to_have_count(0)
                expect(named.locator('#voice-hint')).to_be_visible()
                expect(named.locator('#voice-hint')).to_contain_text(re.compile(r'unsupported|unavailable|not (?:available|supported)', re.IGNORECASE))
                expect(named.locator('#voice-hint')).to_contain_text(re.compile(r'default|reset', re.IGNORECASE))
                assert named.evaluate("localStorage.getItem('codex-voice.voice')") in (None, 'default')
                legacy_sessions_before = len([path for path, _ in server.requests if path == '/api/session'])
                named.locator('#start-voice').click()
                expect(named.locator('#voice-control')).to_have_attribute('data-phase', 'connected')
                assert last_voice_start()['voice'] == 'default'
                assert server.state['threadId'] == original_thread
                assert len([path for path, _ in server.requests if path == '/api/session']) == legacy_sessions_before
                named.locator('#stop-voice').click()
                expect(named.locator('#voice')).to_be_enabled()
                named.locator('#voice').select_option('cove')
                server.state['voice'] = 'alloy'
                named.reload()
                expect(named.locator('#voice')).to_have_value('cove')
                assert named.evaluate("localStorage.getItem('codex-voice.voice')") == 'cove'
                named.locator('#start-voice').click()
                expect(named.locator('#voice-control')).to_have_attribute('data-phase', 'connected')
                assert last_voice_start()['voice'] == 'cove'
                assert server.state['threadId'] == original_thread
                named.locator('#stop-voice').click()
                expect(named.locator('#start-voice')).to_be_enabled()
                report.append('legacy 19-voice advertisements and saved Alloy reset to provider default with an explanation and an explicit override; valid saved Cove survives without replacing the Codex thread')

                server.state.pop('assistantName')
                server.state.pop('voiceOptions')
                server.state.pop('voice')
                named.reload()
                expect(named.locator('#connection-status')).to_contain_text('Lokal verbunden')
                expect(named).to_have_title('Astra · Voice')
                expect(named.locator('#voice-field')).to_be_hidden()
                named.locator('#start-voice').click()
                expect(named.locator('#voice-control')).to_have_attribute('data-phase', 'connected')
                assert 'voice' not in last_voice_start()
                named.locator('#stop-voice').click()
                expect(named.locator('#start-voice')).to_be_enabled()
                server.state.update(assistantName='Jerry', voice='juniper', voiceOptions=VOICE_OPTIONS)
                named.evaluate("localStorage.setItem('codex-voice.voice', 'unknown-voice')")
                named.reload()
                expect(named.locator('#voice')).to_have_value('juniper')
                expect(named.locator('#voice-hint')).to_contain_text(re.compile(r'unsupported|unavailable|not (?:available|supported)', re.IGNORECASE))
                named.locator('#start-voice').click()
                expect(named.locator('#voice-control')).to_have_attribute('data-phase', 'connected')
                assert last_voice_start()['voice'] == 'juniper'
                named.locator('#stop-voice').click()
                expect(named.locator('#start-voice')).to_be_enabled()
                assert not named_errors, named_errors
                named_context.close()
                server.state.update(threadId=None, voice='default', voiceActive=False)
                report.append('older servers hide voice settings and receive no voice override; invalid saved voices fall back to the server choice')

                server.state['permissionMode'] = 'yolo'
                fresh_context = browser.new_context()
                fresh_page = fresh_context.new_page()
                fresh_page.goto(url)
                expect(fresh_page.locator('#connection-status')).to_contain_text('Lokal verbunden')
                expect(fresh_page.locator('#permission-mode')).to_have_value('yolo')
                expect(fresh_page.locator('#yolo-active')).to_be_hidden()
                expect(fresh_page.locator('.capability-note')).to_have_text('Computer Use: nur mit OpenAIs nativem Dienst.')
                assert fresh_page.evaluate('uiFixture.storageWrites.length') == 0
                fresh_page.locator('#permission-mode').select_option('ask')
                server.emit('state', dict(server.state))
                expect(fresh_page.locator('#permission-mode')).to_have_value('ask')
                fresh_context.close()
                server.state['permissionMode'] = 'ask'
                report.append('server YOLO default respected until explicit browser choice; native-only Computer Use copy')
                context = browser.new_context(viewport={'width': 1380, 'height': 1080})
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(url)
                expect(page.locator('#connection-status')).to_contain_text('Lokal verbunden')
                expect(page.locator('#permission-mode')).to_have_value('ask')
                assert page.evaluate('uiFixture.micCalls') == 0
                assert page.evaluate('uiFixture.contexts.length') == 0
                assert page.evaluate('uiFixture.storageWrites.length') == 0
                page.locator('#permission-mode').select_option('yolo')
                expect(page.locator('#permission-hint')).to_be_visible()
                assert page.evaluate('uiFixture.storageWrites.length') == 1
                server.emit('state', dict(server.state))
                page.get_by_role('button', name='Neue Sitzung', exact=True).click()
                expect(page.locator('#yolo-active')).to_be_visible()
                assert server.requests[-1][1]['permissionMode'] == 'yolo'
                assert page.evaluate('uiFixture.storageWrites.length') == 1
                page.reload()
                expect(page.locator('#connection-status')).to_contain_text('Lokal verbunden')
                expect(page.locator('#permission-mode')).to_have_value('yolo')
                assert page.evaluate('uiFixture.storageWrites.length') == 0
                page.locator('#permission-mode').select_option('ask')
                page.get_by_role('button', name='Neue Sitzung', exact=True).click()
                expect(page.locator('#yolo-active')).to_be_hidden()
                report.append('permission defaults, explicit storage, session mode and active badge')

                def approval(identifier, params, method='item/commandExecution/requestApproval'):
                    server.emit('approval/request', {'id': identifier, 'method': method, 'params': params})

                approval(10, {'command': 'git status --short', 'availableDecisions': ['accept', 'decline', 'acceptForSession']})
                expect(page.locator('.approval-card pre')).to_have_text('git status --short')
                page.get_by_role('button', name='Für diese Sitzung', exact=True).click()
                expect(page.locator('.approval-card')).to_have_count(0)
                assert server.requests[-1][1] == {'id': 10, 'decision': 'acceptForSession'}
                proposal = ['git', 'status']
                approval(11, {'command': 'git status --short', 'proposedExecpolicyAmendment': proposal,
                              'availableDecisions': ['accept', 'decline', {'acceptWithExecpolicyAmendment': {'execpolicy_amendment': proposal}}]})
                expect(page.locator('.approval-scope pre')).to_have_text('"git" "status"')
                page.get_by_role('button', name='Dauerhaft erlauben', exact=True).click()
                expect(page.locator('.approval-card')).to_have_count(0)
                assert server.requests[-1][1] == {'id': 11, 'decision': 'always'}
                approval(12, {'command': 'git status', 'proposedExecpolicyAmendment': ['git', 'status'],
                              'availableDecisions': ['accept', 'decline', {'acceptWithExecpolicyAmendment': {'execpolicy_amendment': ['git', 'push']}}]})
                expect(page.locator('.approval-card')).to_have_count(1)
                expect(page.get_by_role('button', name='Dauerhaft erlauben', exact=True)).to_have_count(0)
                page.get_by_role('button', name='Ablehnen', exact=True).click()
                expect(page.locator('.approval-card')).to_have_count(0)
                approval(13, {'command': 'git status', 'proposedExecpolicyAmendment': proposal})
                expect(page.get_by_role('button', name='Dauerhaft erlauben', exact=True)).to_be_enabled()
                server.emit('serverRequest/resolved', {'requestId': 13})
                expect(page.locator('.approval-card')).to_have_count(0)
                approval(14, {'itemId': 'missing-diff'}, 'item/fileChange/requestApproval')
                expect(page.get_by_role('button', name='Für diese Sitzung', exact=True)).to_be_disabled()
                expect(page.get_by_role('button', name='Einmal erlauben', exact=True)).to_be_disabled()
                server.emit('serverRequest/resolved', {'requestId': 14})
                expect(page.locator('.approval-card')).to_have_count(0)
                report.append('session and persistent approvals, exact prefix matching and missing-diff refusal')

                page.get_by_role('button', name='Gespräch starten').click()
                expect(page.locator('#voice-control')).to_have_attribute('data-phase', 'connected')
                expect(page.locator('#voice-control')).to_have_attribute('data-meter', 'active')
                page.wait_for_function("Number(document.getElementById('voice-control').style.getPropertyValue('--mic-level')) < .01")
                page.evaluate('uiFixture.gain.gain.value = .2')
                page.wait_for_function("Number(document.getElementById('voice-control').style.getPropertyValue('--mic-level')) > .4")
                assert page.evaluate('uiFixture.speakerConnections') == 0
                screenshot_dir = os.environ.get('CODEX_VOICE_UI_SCREENSHOTS')
                if screenshot_dir:
                    destination = Path(screenshot_dir)
                    destination.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(destination / 'voice-rms-desktop.png'), full_page=True)
                    page.set_viewport_size({'width': 390, 'height': 844})
                    page.screenshot(path=str(destination / 'voice-rms-mobile.png'), full_page=True)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.set_viewport_size({'width': 1380, 'height': 1080})
                page.get_by_role('button', name='Mikro aus', exact=True).click()
                expect(page.locator('#voice-control')).to_have_attribute('data-meter', 'idle')
                page.wait_for_function("uiFixture.contexts.filter(context => !uiFixture.inputContexts.includes(context)).every(context => context.state === 'closed')")
                assert page.evaluate('uiFixture.tracks[0].enabled') is False
                page.get_by_role('button', name='Mikro an', exact=True).click()
                expect(page.locator('#voice-control')).to_have_attribute('data-meter', 'active')
                page.wait_for_function("Number(document.getElementById('voice-control').style.getPropertyValue('--mic-level')) > .4")
                page.evaluate('uiFixture.gain.gain.value = 0')
                page.wait_for_function("Number(document.getElementById('voice-control').style.getPropertyValue('--mic-level')) < .01")
                page.get_by_role('button', name='Beenden', exact=True).click()
                expect(page.get_by_role('button', name='Gespräch starten')).to_be_enabled()
                page.wait_for_function("uiFixture.contexts.filter(context => !uiFixture.inputContexts.includes(context)).every(context => context.state === 'closed')")
                assert page.evaluate("uiFixture.tracks.every(track => track.readyState === 'ended')")
                assert page.evaluate("uiFixture.peers.every(peer => peer.connectionState === 'closed')")
                report.append('real RMS reacts to synthetic audio and silence without speaker routing; mute and stop clean up')

                page.emulate_media(reduced_motion='reduce')
                contexts_before = page.evaluate('uiFixture.contexts.length')
                page.get_by_role('button', name='Gespräch starten').click()
                expect(page.locator('#voice-control')).to_have_attribute('data-phase', 'connected')
                expect(page.locator('#voice-control')).to_have_attribute('data-meter', 'idle')
                assert page.evaluate('uiFixture.contexts.length') == contexts_before + 1
                page.emulate_media(reduced_motion='no-preference')
                expect(page.locator('#voice-control')).to_have_attribute('data-meter', 'active')
                page.evaluate("window.dispatchEvent(new Event('pagehide'))")
                page.wait_for_function("uiFixture.contexts.at(-1).state === 'closed'")
                assert page.evaluate("uiFixture.tracks.every(track => track.readyState === 'ended')")
                assert page.evaluate("uiFixture.peers.every(peer => peer.connectionState === 'closed')")
                assert page.evaluate('uiFixture.speakerConnections') == 0
                report.append('reduced motion skips analyser; pagehide closes own audio')
                assert not errors, errors
                context.close()

                with server.condition:
                    server.events.clear()
                    server.state.update(threadId=None, voiceActive=False, activeTurnId=None, permissionMode='ask', clientTransport='poll')
                remote_context = browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
                remote = remote_context.new_page()
                remote.on('pageerror', lambda error: errors.append(str(error)))
                sse_before = server.sse_requests
                remote.goto(url)
                expect(remote.locator('#connection-status')).to_contain_text('Mit deinem Mac verbunden')
                assert server.sse_requests == sse_before
                assert remote.evaluate('uiFixture.micCalls') == 0
                assert remote.locator('#remote-audio').get_attribute('playsinline') == ''
                expect(remote.locator('.mobile-note')).to_be_visible()
                assert remote.locator('#message').evaluate("element => getComputedStyle(element).fontSize") == '16px'
                assert remote.evaluate('document.documentElement.scrollWidth <= innerWidth')
                remote.get_by_role('button', name='Gespräch starten').click()
                expect(remote.locator('#voice-control')).to_have_attribute('data-phase', 'connected')
                expect(remote.locator('#wake-status')).to_be_visible()
                assert remote.evaluate("uiFixture.audioPlays[0].gesture && uiFixture.audioPlays[0].source.startsWith('blob:')")
                assert remote.evaluate("uiFixture.actions.indexOf('audio.play') < uiFixture.actions.indexOf('/api/session')")
                assert remote.evaluate("uiFixture.actions.indexOf('AudioContext') < uiFixture.actions.indexOf('/api/session')")
                if screenshot_dir:
                    remote.screenshot(path=str(destination / 'voice-remote-mobile.png'), full_page=True)

                def speech_event(payload):
                    remote.evaluate("payload => uiFixture.channels.at(-1).dispatchEvent(new MessageEvent('message', {data:JSON.stringify(payload)}))", payload)

                server.state.update(memoryEnabled=True, memoryServerTranscripts=True, memoryStatus='saving')
                server.emit('state', dict(server.state))
                expect(remote.locator('#memory-status')).to_have_text('Gespräch wird gespeichert …')
                server.emit('thread/realtime/transcript/done', {'role': 'user', 'text': 'Serverseitig gespeicherter Satz.'})
                speech_event({'type': 'conversation.item.input_audio_transcription.completed', 'item_id': 'server-owned', 'transcript': 'Serverseitig gespeicherter Satz.'})
                assert not [request for request in server.requests if request[0] == '/api/memory/transcript']
                server.state.update(memoryStatus='saved')
                server.emit('state', dict(server.state))
                expect(remote.locator('#memory-status')).to_have_text('Gesprächsprotokoll gespeichert.')
                server.state.update(memoryServerTranscripts=False, memoryStatus='ready')
                server.emit('state', dict(server.state))
                expect(remote.locator('#memory-status')).to_have_text('Gesprächsprotokoll aktiv.')
                server.memory_failures = 1
                speech_event({'type': 'conversation.item.input_audio_transcription.completed', 'item_id': 'spoken-user-1', 'transcript': 'Bitte bewahre diese Entscheidung auf.'})
                expect(remote.locator('#memory-status')).to_contain_text('erneut versucht')
                speech_event({'type': 'response.output_audio_transcript.delta', 'item_id': 'spoken-assistant-1', 'delta': 'Unfertig'})
                speech_event({'type': 'response.output_audio_transcript.done', 'item_id': 'spoken-assistant-1', 'transcript': 'Die Entscheidung ist vermerkt.'})
                speech_event({'type': 'response.done', 'response': {'id': 'response-1', 'output': [{'id': 'spoken-assistant-1', 'role': 'assistant', 'content': [{'type': 'audio', 'transcript': 'Die Entscheidung ist vermerkt.'}]}]}})
                with server.condition:
                    assert server.condition.wait_for(lambda: len(server.memory_records) == 2, 6)
                assert {record['itemId'] for record in server.memory_records} == {'spoken-user-1', 'spoken-assistant-1'}
                assert all(record['threadId'] == server.state['threadId'] for record in server.memory_records)
                assert all(record['text'] != 'Unfertig' for record in server.memory_records)
                assert remote.evaluate('uiFixture.storageWrites.length') == 0
                report.append('server transcript ownership avoids duplicates; final speech fallback retries with stable IDs and ignores interim text')
                server.emit('thread/realtime/transcript/delta', {'role': 'assistant', 'delta': 'Hallo '})
                expect(remote.locator('.message-content').filter(has_text='Hallo ')).to_have_count(1)
                with server.condition:
                    server.poll_duplicate = server.events[-1]
                server.emit('thread/realtime/transcript/delta', {'role': 'assistant', 'delta': 'Welt'})
                expect(remote.locator('.message-content').filter(has_text='Hallo Welt')).to_have_count(1)
                assert 'Hallo Hallo' not in remote.locator('#conversation').inner_text()
                approval(90, {'command': 'git status', 'availableDecisions': ['accept', 'decline']})
                expect(remote.locator('.approval-card pre')).to_have_text('git status')
                remote.get_by_role('button', name='Einmal erlauben', exact=True).click()
                expect(remote.locator('.approval-card')).to_have_count(0)
                assert server.poll_requests == sorted(server.poll_requests)
                report.append('remote polling uses monotonic cursor, ignores duplicates, renders transcripts and approvals without SSE')

                remote.evaluate("uiFixture.visibility='hidden'; document.dispatchEvent(new Event('visibilitychange'))")
                expect(remote.locator('#voice-control')).to_have_attribute('data-phase', 'background')
                expect(remote.locator('#wake-status')).to_be_hidden()
                assert remote.evaluate('uiFixture.wakeLocks.every(lock => lock.released)')
                remote.evaluate("uiFixture.visibility='visible'; document.dispatchEvent(new Event('visibilitychange'))")
                expect(remote.locator('#voice-control')).to_have_attribute('data-phase', 'connected')
                expect(remote.locator('#wake-status')).to_be_visible()
                assert remote.evaluate('uiFixture.micCalls') == 1
                remote.evaluate("uiFixture.tracks[0].dispatchEvent(new Event('mute'))")
                expect(remote.locator('#voice-control')).to_have_attribute('data-phase', 'error')
                expect(remote.get_by_role('button', name='Gespräch starten')).to_be_enabled()
                assert remote.evaluate('uiFixture.tracks.every(track => track.readyState === "ended")')
                remote.get_by_role('button', name='Gespräch starten').click()
                expect(remote.locator('#voice-control')).to_have_attribute('data-phase', 'connected')
                report.append('audio play and context unlock precede async session creation; wake lock release/reacquire and interrupted microphone restart')

                server.memory_failures = 1
                speech_event({'type': 'conversation.item.input_audio_transcription.completed', 'item_id': 'pagehide-last-sentence', 'transcript': 'Der letzte Satz bleibt erhalten.'})
                expect(remote.locator('#memory-status')).to_contain_text('erneut versucht')
                remote.evaluate("window.dispatchEvent(new Event('pagehide'))")
                expect(remote.locator('#voice-control')).to_have_attribute('data-phase', 'error')
                assert remote.evaluate('uiFixture.tracks.every(track => track.readyState === "ended")')
                polls_at_hide = remote.evaluate("uiFixture.actions.filter(action => action.startsWith('/api/events/poll?')).length")
                remote.wait_for_timeout(450)
                assert remote.evaluate("uiFixture.actions.filter(action => action.startsWith('/api/events/poll?')).length") == polls_at_hide
                with server.condition:
                    assert server.condition.wait_for(lambda: any(record['itemId'] == 'pagehide-last-sentence' for record in server.memory_records), 3)
                remote.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}))")
                expect(remote.get_by_role('button', name='Gespräch starten')).to_be_enabled()
                remote.get_by_role('button', name='Gespräch starten').click()
                expect(remote.locator('#voice-control')).to_have_attribute('data-phase', 'connected')
                report.append('pagehide aborts remote polling and media; browser history restore reconnects events without reopening microphone')

                with server.condition:
                    server.expired = True
                    server.condition.notify_all()
                expect(remote.get_by_role('button', name='Gerät erneut anmelden')).to_be_visible()
                expect(remote.locator('#connection-status')).to_contain_text('Anmeldung abgelaufen')
                expect(remote.get_by_role('button', name='Gespräch starten')).to_be_disabled()
                assert remote.evaluate('uiFixture.tracks.every(track => track.readyState === "ended")')
                assert remote.evaluate('uiFixture.peers.every(peer => peer.connectionState === "closed")')
                assert remote.evaluate('uiFixture.wakeLocks.every(lock => lock.released)')
                remote_context.close()
                server.expired = False
                server.state['voiceActive'] = False
                insecure_context = browser.new_context()
                insecure_context.add_init_script("Object.defineProperty(window, 'isSecureContext', {value:false})")
                insecure = insecure_context.new_page()
                insecure.goto(url)
                expect(insecure.locator('#connection-status')).to_contain_text('Mit deinem Mac verbunden')
                insecure.get_by_role('button', name='Gespräch starten').click()
                expect(insecure.locator('#notice-text')).to_contain_text('HTTPS-Link')
                assert insecure.evaluate('uiFixture.micCalls') == 0
                assert insecure.evaluate('uiFixture.audioPlays.length') == 0
                insecure_context.close()
                report.append('expired pairing stops local media and asks to sign in; insecure context explains HTTPS before microphone access')
                assert not errors, errors
                browser.close()
                print(json.dumps({'result': 'passed', 'checks': report, 'javascript_errors': errors,
                                  'real_microphone_used': False, 'production_server_used': False}, indent=2))
        finally:
            server.running = False
            with server.condition:
                server.condition.notify_all()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == '__main__':
    run()
