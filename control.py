"""Private local pairing recovery; no privileged recovery route is exposed over HTTP."""
import errno
from contextlib import suppress
import json
import os
from pathlib import Path
import socket
import socketserver
import stat
import tempfile
import threading


def private_parent(path):
    parent = Path(path).parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError('Pairing recovery requires a private directory owned by this user (mode 0700).')


def write_private_json(path, data):
    path = Path(path)
    private_parent(path)
    if path.is_symlink():
        raise ValueError('The private pairing receipt must not be a symlink.')
    fd, temporary = tempfile.mkstemp(prefix='.pairing-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(data, handle, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        # Once replacement succeeds, callers must activate the invitation now
        # visible in the receipt. Directory fsync is best effort: a restart
        # revokes these process-local invitations anyway. Do not report a failed
        # rotation after publishing new bytes and leave an unusable receipt.
        with suppress(OSError):
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class LocalPairingControl:
    """A mode-0600 Unix socket in a mode-0700 directory authorizes the local owner."""
    def __init__(self, path, renew):
        self.path = Path(path)
        private_parent(self.path)
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError('Refusing to replace an unrelated pairing control path.')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(1)
                try:
                    probe.connect(str(self.path))
                except OSError as exc:
                    if exc.errno != errno.ECONNREFUSED:
                        raise
                else:
                    raise RuntimeError('A pairing control service is already running.')
            self.path.unlink()

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(3)
                try:
                    line = self.rfile.readline(1025)
                    if len(line) > 1024 or not line.endswith(b'\n'):
                        raise ValueError('Invalid request.')
                    data = json.loads(line)
                    if data != {'action': 'pair'}:
                        raise ValueError('Invalid request.')
                    response = {'ok': True, 'connection': renew()}
                except Exception:
                    response = {'ok': False, 'error': 'The local pairing invitation could not be renewed.'}
                try:
                    self.wfile.write(json.dumps(response).encode() + b'\n')
                except OSError:
                    pass

        self.server = socketserver.UnixStreamServer(str(self.path), Handler)
        os.chmod(self.path, 0o600)
        self.inode = self.path.stat().st_ino
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.05}, daemon=True)
        self.thread.start()

    def close(self):
        if self.thread:
            self.server.shutdown()
            self.thread.join(timeout=4)
        self.server.server_close()
        if self.path.exists() and self.path.lstat().st_ino == self.inode:
            self.path.unlink()


def request_pairing(path):
    path = Path(path)
    private_parent(path)
    info = path.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError('The pairing control socket is not private to this user.')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(path))
        client.sendall(b'{"action":"pair"}\n')
        with client.makefile('rb') as stream:
            line = stream.readline(16385)
        if len(line) > 16384 or not line.endswith(b'\n'):
            raise RuntimeError('Invalid response from the local pairing service.')
        response = json.loads(line)
    if not isinstance(response, dict):
        raise RuntimeError('Invalid response from the local pairing service.')
    if not response.get('ok'):
        raise RuntimeError(response.get('error', 'Pairing recovery failed.'))
    connection = response.get('connection')
    if not isinstance(connection, dict) or not isinstance(connection.get('pairing_url'), str):
        raise RuntimeError('The local pairing service returned no invitation.')
    return connection
