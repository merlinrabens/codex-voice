#!/usr/bin/env python3
"""Temporary, paired HTTPS access to Codex Voice through Cloudflare Quick Tunnel."""
import argparse
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from control import request_pairing
from persona import load_persona

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cwd', default=str(Path.cwd()))
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--model', default='gpt-6-astra')
    parser.add_argument('--assistant-name', help='Assistant display and conversational name')
    parser.add_argument('--assistant-profile', help='Optional private JSON persona file outside this checkout')
    parser.add_argument('--voice', default='default', help='Initial Realtime voice selection; default preserves the provider choice')
    parser.add_argument('--yolo', action='store_true')
    parser.add_argument('--keep-awake', action='store_true')
    parser.add_argument('--memory-dir', help='Private conversation archive directory')
    parser.add_argument('--memory-index-script', help='Optional Python indexer to trigger after voice ends')
    parser.add_argument('--memory-index-python', default=sys.executable)
    parser.add_argument('--pair', action='store_true', help='Renew the running server\'s pairing link without restarting it')
    parser.add_argument('--copy', action='store_true', help='Copy the renewed pairing link to the Mac clipboard (requires --pair)')
    args = parser.parse_args()
    private = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'voice-access'
    if args.copy and not args.pair:
        parser.error('--copy requires --pair.')
    if args.pair:
        if args.copy and not shutil.which('pbcopy'):
            parser.error('Clipboard copying requires pbcopy on this Mac.')
        try:
            connection = request_pairing(private / 'control.sock')
        except (OSError, ValueError, RuntimeError) as exc:
            parser.exit(1, f'Cannot renew pairing: {exc}. Start or update the remote Voice server first.\n')
        if args.copy:
            subprocess.run(['pbcopy'], input=connection['pairing_url'], text=True, check=True)
            print('Fresh one-use pairing link copied. Open it on your phone within 30 minutes.')
        else:
            print(connection['pairing_url'])
        return
    if not shutil.which('cloudflared'):
        parser.error('Install cloudflared before using remote access.')
    load_persona(args.assistant_profile, args.assistant_name)
    if not Path(args.cwd).expanduser().is_dir():
        parser.error('The project directory does not exist.')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', args.port))
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    receipt = private / 'connection.json'
    children = []
    tunnel = None
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        tunnel = subprocess.Popen(
            ['cloudflared', 'tunnel', '--no-autoupdate', '--url', f'http://127.0.0.1:{args.port}'],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, bufsize=1)
        children.append(tunnel)
        output = queue.Queue()

        def consume():
            for line in tunnel.stderr:
                match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com\b', line)
                if match:
                    output.put(match.group(0))
            output.put(None)

        threading.Thread(target=consume, daemon=True).start()
        try:
            origin = output.get(timeout=50)
        except queue.Empty:
            raise RuntimeError('The HTTPS tunnel did not become ready in time.') from None
        if not origin:
            raise RuntimeError('The HTTPS tunnel could not start.')
        command = [sys.executable, str(ROOT / 'server.py'), '--port', str(args.port), '--cwd', args.cwd,
                   '--model', args.model, '--voice', args.voice, '--remote-origin', origin, '--pairing-file', str(receipt)]
        if args.assistant_name:
            command.extend(['--assistant-name', args.assistant_name])
        if args.assistant_profile:
            command.extend(['--assistant-profile', str(Path(args.assistant_profile).expanduser().resolve())])
        if args.yolo:
            command.append('--yolo')
        if args.memory_dir:
            command.extend(['--memory-dir', str(Path(args.memory_dir).expanduser().resolve())])
        if args.memory_index_script:
            command.extend(['--memory-index-script', str(Path(args.memory_index_script).expanduser().resolve())])
            command.extend(['--memory-index-python', args.memory_index_python])
        backend = subprocess.Popen(command)
        children.append(backend)
        if args.keep_awake and shutil.which('caffeinate'):
            children.append(subprocess.Popen(['caffeinate', '-i', '-w', str(os.getpid())]))
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if backend.poll() is not None or tunnel.poll() is not None:
                raise RuntimeError('The voice server or tunnel stopped during startup.')
            try:
                data = json.loads(receipt.read_text())
                if data.get('server_pid') == backend.pid and data.get('origin') == origin:
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError('The voice server did not become ready in time.')
        print(f'Remote Voice ready: {origin}', flush=True)
        print(f'Private one-use pairing link: open {receipt} locally.', flush=True)
        print('Stop with Ctrl+C. Restarting creates a new link and revokes old sessions.', flush=True)
        while backend.poll() is None and tunnel.poll() is None:
            time.sleep(0.5)
        raise RuntimeError('The voice server or HTTPS tunnel disconnected; restart remote.py.')
    except KeyboardInterrupt:
        pass
    finally:
        for process in reversed(children):
            if process.poll() is None:
                process.terminate()
        for process in reversed(children):
            try:
                process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


if __name__ == '__main__':
    main()
