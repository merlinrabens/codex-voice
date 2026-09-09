#!/usr/bin/env python3
"""Isolated UI smoke test. Uses no Codex server, microphone or model connection.

Run with a Python environment containing Playwright. A local Chrome installation
is used when available; otherwise Playwright's installed Chromium is used.
"""
import json
import mimetypes
import os
from pathlib import Path
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parent
BOOT = r"""
window.uiFixture = {micCalls:0, contexts:[], tracks:[], peers:[], storageWrites:[], speakerConnections:0};
const nativeConnect = AudioNode.prototype.connect;
AudioNode.prototype.connect = function(destination, ...rest) {
  if (destination instanceof AudioDestinationNode) uiFixture.speakerConnections++;
  return nativeConnect.call(this, destination, ...rest);
};
const RealAudioContext = window.AudioContext;
window.AudioContext = class extends RealAudioContext {
  constructor(...args) { super(...args); uiFixture.contexts.push(this); }
};
const nativeSetItem = Storage.prototype.setItem;
Storage.prototype.setItem = function(key, value) {
  uiFixture.storageWrites.push({key, value});
  return nativeSetItem.call(this, key, value);
};
Object.defineProperty(navigator.mediaDevices, 'getUserMedia', {value: async () => {
  uiFixture.micCalls++;
  const context = new AudioContext();
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
  createDataChannel() { return {addEventListener() {}, close() {}, readyState:'open'}; }
  async createOffer() { return {type:'offer',sdp:'v=0 ui-test'}; }
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
        self.state = {'threadId': None, 'cwd': cwd, 'model': 'gpt-6-astra',
                      'effort': 'high', 'permissionMode': 'ask', 'voiceActive': False,
                      'activeTurnId': None}

    def emit(self, method, params):
        with self.condition:
            self.events.append({'method': method, 'params': params})
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

    def respond(self, body, kind='application/json'):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/api/state':
            return self.respond(dict(self.server.state))
        if self.path == '/api/events':
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
        if self.path == '/api/session':
            self.server.state.update(data, threadId=f'ui-thread-{len(self.server.requests)}')
            self.server.emit('state', dict(self.server.state))
        elif self.path == '/api/voice/start':
            self.server.state['voiceActive'] = True
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
                page.wait_for_function("uiFixture.contexts[1].state === 'closed'")
                assert page.evaluate('uiFixture.tracks[0].enabled') is False
                page.get_by_role('button', name='Mikro an', exact=True).click()
                expect(page.locator('#voice-control')).to_have_attribute('data-meter', 'active')
                page.wait_for_function("Number(document.getElementById('voice-control').style.getPropertyValue('--mic-level')) > .4")
                page.evaluate('uiFixture.gain.gain.value = 0')
                page.wait_for_function("Number(document.getElementById('voice-control').style.getPropertyValue('--mic-level')) < .01")
                page.get_by_role('button', name='Beenden', exact=True).click()
                expect(page.get_by_role('button', name='Gespräch starten')).to_be_enabled()
                page.wait_for_function("uiFixture.contexts.slice(1).every(context => context.state === 'closed')")
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
