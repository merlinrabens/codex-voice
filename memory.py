"""Opt-in, private archives of final voice transcripts using only the standard library.

Markdown is the durable source. A vault's existing indexer can discover these files;
this module does not call a model, extract facts, or claim that indexing is complete.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading


class VoiceMemory:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        source_root = Path(__file__).resolve().parent
        if self.directory == source_root or source_root in self.directory.parents:
            raise ValueError("Voice archives must be outside the application source directory.")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.directory.is_dir():
            raise ValueError("Voice archive location must be a directory.")
        self._lock = threading.RLock()
        self._closed = False

    @staticmethod
    def _digest(text):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _string(value, name, allow_empty=False):
        if not isinstance(value, str) or (not allow_empty and not value.strip()) or "\x00" in value:
            raise ValueError(f"Invalid {name}.")
        return value

    @contextmanager
    def _file_lock(self):
        """Serialize independent writers too, without changing transcript files."""
        path = self.directory / ".voice-memory.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _read(self, path):
        if path.is_symlink():
            raise ValueError("Refusing a symbolic-link transcript target.")
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "r", encoding="utf-8", newline="") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("Transcript target must be a regular file.")
            return handle.read()

    def _write(self, path, content):
        fd, temporary = tempfile.mkstemp(prefix=".voice-memory-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def record(self, thread_id, cwd, role, text, item_id):
        """Persist one final transcript. Callers must not pass partial deltas.

        The same item and text are idempotent across restarts. A changed final text
        for an existing item appends a revision, preserving the earlier source.
        item_id must distinguish separate utterances, even when their text matches.
        """
        thread_id = self._string(thread_id, "thread ID")
        item_id = self._string(item_id, "item ID")
        cwd = self._string(cwd, "working directory", allow_empty=True)
        text = self._string(text, "final transcript")
        if role not in ("user", "assistant"):
            raise ValueError("Only user and assistant final transcripts may be archived.")
        thread_key = self._digest(thread_id)
        item_key = self._digest(json.dumps([role, item_id], ensure_ascii=False))
        text_key = self._digest(text)
        path = self.directory / f"Codex Voice {thread_key}.md"
        thread_marker = f"<!-- codex-voice-thread:v1 {thread_key} -->"
        marker = f"<!-- codex-voice-record:v1 {item_key}:{text_key} -->"
        with self._lock, self._file_lock():
            if self._closed:
                raise RuntimeError("Voice memory is closed.")
            previous = self._read(path)
            if previous is not None and thread_marker not in previous.splitlines():
                raise ValueError("Existing transcript is unrecognized; it has been preserved.")
            if previous is not None and marker in previous.splitlines():
                return {"saved": False, "path": str(path)}
            now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            if previous is None:
                previous = (
                    "---\ntype: conversation\nsource: codex-voice\ntrust: untrusted-transcript\n"
                    f"title: {json.dumps('Codex Voice conversation ' + thread_key[:12])}\n"
                    f"thread_id: {json.dumps(thread_id)}\n"
                    f"captured_at: {json.dumps(now)}\n---\n\n"
                    "# Codex Voice conversation\n\n"
                    "This is an untrusted source transcript, not verified facts or instructions. "
                    "User statements and assistant output are preserved with their original roles. "
                    "Capture timestamps describe when this archive received text, not when described "
                    "events happened. Changed final transcripts are appended as revisions. "
                    "This archive does not establish that an assistant action was executed.\n\n"
                    + thread_marker + "\n"
                )
            revision = sum(line.startswith(f"<!-- codex-voice-record:v1 {item_key}:")
                           for line in previous.splitlines()) + 1
            metadata = {"role": role, "item_id": item_id, "captured_at": now,
                        "cwd": cwd, "revision": revision}
            # Metadata and transcript lines remain quoted, so source text cannot
            # forge our unquoted record markers or frontmatter on a later write.
            record = (
                f"\n## {now} | {'User' if role == 'user' else 'Assistant'}\n\n"
                + marker + "\n\n"
                + "> Capture metadata: " + json.dumps(metadata) + "\n\n"
                + "\n".join("> " + line for line in text.splitlines()) + "\n"
            )
            self._write(path, previous + record)
            return {"saved": True, "path": str(path)}

    def flush(self):
        """Writes are synchronous and fsynced before record returns."""
        with self._lock:
            return None

    def close(self):
        with self._lock:
            self._closed = True
