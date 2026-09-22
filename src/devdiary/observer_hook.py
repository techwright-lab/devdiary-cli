"""Stock-tool metadata observer. Standalone stdlib entrypoint, never declares work.

Executed with a trusted absolute Python -I path: no project imports, HTTP, or logs.
"""
from __future__ import annotations

import argparse
try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import sys
import time
import uuid

EVENTS = ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse',
          'PostToolUseFailure', 'Stop', 'SubagentStart', 'SubagentStop', 'SessionEnd')
CODEX_EVENTS = ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse',
                'Stop', 'SubagentStart', 'SubagentStop', 'SessionEnd', 'Interrupt')
VENDORS = {'claude': ('claude-code', EVENTS), 'codex': ('codex', CODEX_EVENTS)}
MAX_INPUT = 65536
MAX_ROWS = 10000
MAX_DB = 16 * 1024 * 1024
TOKEN = re.compile(r'[A-Za-z0-9_:./@+\-]{1,200}\Z')


def token(value):
    return value if isinstance(value, str) and TOKEN.fullmatch(value) else None


def safe_path(path):
    if not path.is_absolute():
        raise ValueError('absolute_path_required')
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError('symlink_path')
    if path.exists():
        info = path.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077 or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
            raise ValueError('private_state_required')


def connect(state):
    safe_path(state)
    database = state / 'observations.sqlite3'
    safe_path(database)
    for suffix in ('-journal', '-wal', '-shm'):
        safe_path(Path(str(database) + suffix))
    if database.exists() and (not database.is_file() or database.stat().st_size > MAX_DB):
        raise ValueError('spool_unavailable')
    db = sqlite3.connect(database, timeout=0.05)
    os.chmod(database, 0o600)
    db.execute('PRAGMA synchronous=FULL')
    db.execute('PRAGMA journal_mode=DELETE')
    db.execute('PRAGMA max_page_count=4096')
    db.execute('CREATE TABLE IF NOT EXISTS observations (seq INTEGER PRIMARY KEY, dedup TEXT UNIQUE, metadata TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL)')
    db.commit()
    return db


def dropped(db):
    db.execute("INSERT INTO counters VALUES ('dropped', 1) ON CONFLICT(name) DO UPDATE SET value=value+1")
    db.commit()


def collect(state, installation_id, stream):
    if fcntl is None:
        return
    safe_path(state)
    lock = state / 'registration.lock'
    safe_path(lock)
    fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        _collect(state, installation_id, stream)
    finally:
        os.close(fd)


def _collect(state, installation_id, stream):
    manifest_path = state / 'manifest.json'
    safe_path(manifest_path)
    if not manifest_path.is_file() or manifest_path.stat().st_size > 65536:
        raise ValueError('invalid_manifest')
    manifest = json.loads(manifest_path.read_bytes())
    vendor = manifest.get('vendor', 'claude')
    if vendor not in VENDORS:
        return
    runtime, events = VENDORS[vendor]
    if manifest['status'] != 'installed' or manifest['installation_id'] != installation_id:
        return
    db = connect(state)
    try:
        raw = stream.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            dropped(db)
            return
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError('invalid_event')
            event, session = data.get('hook_event_name'), token(data.get('session_id'))
            if event not in events or not session:
                raise ValueError('invalid_event')
            cwd = data.get('cwd')
            if not isinstance(cwd, str) or len(cwd) > 4096 or not Path(cwd).is_absolute():
                raise ValueError('invalid_scope')
            # Consent covers only this exact repository root and its descendants;
            # sibling worktrees need their own installation. Never inspect Git.
            resolved = Path(cwd).resolve()
            repository = Path(manifest['repository'])
            if resolved != repository and repository not in resolved.parents:
                return
            result = {'schema_version': 1, 'observation_id': str(uuid.uuid4()),
                      'installation_id': installation_id, 'runtime': runtime,
                      'source_tag': runtime,
                      'session_id': session, 'event': event,
                      'observed_at': time.time(), 'repository': str(repository),
                      'actor_ref': None, 'attribution_basis': 'unknown',
                      'delivery': 'local_only'}
            keys = ('turn_id', 'tool_use_id', 'agent_id', 'agent_type') if vendor == 'codex' else ('prompt_id', 'tool_use_id', 'agent_id', 'agent_type')
            for key in keys:
                if key in data:
                    value = token(data[key])
                    if value is None:
                        raise ValueError('invalid_metadata')
                    result[key] = value
            if event == 'SessionStart':
                if data.get('source') in ('startup', 'resume', 'clear', 'compact', 'fork'):
                    result['source'] = data['source']
                if token(data.get('model')):
                    result['model'] = data['model']
            if event == 'SessionEnd' and data.get('reason') in ('clear', 'resume', 'logout', 'prompt_input_exit', 'other'):
                result['reason'] = data['reason']
            if event in ('PreToolUse', 'PostToolUse', 'PostToolUseFailure') and token(data.get('tool_name')):
                result['tool_name'] = data['tool_name']
            binding = manifest.get('binding')
            if binding and binding['session_id'] == session and binding['agent_id'] == result.get('agent_id'):
                result.update(actor_ref=binding['actor_ref'], attribution_basis='explicit_local_binding')
            # Only vendor correlation IDs support dedup. No content hashing and
            # no collapsing repeated starts/resumes with the same session ID.
            identity = None
            if event in ('PreToolUse', 'PostToolUse', 'PostToolUseFailure'):
                identity = result.get('tool_use_id')
            elif event in ('UserPromptSubmit', 'Stop', 'Interrupt'):
                identity = result.get('turn_id' if vendor == 'codex' else 'prompt_id')
            elif event in ('SubagentStart', 'SubagentStop'):
                identity = result.get('agent_id')
                if vendor == 'codex':
                    # A resumed child may stop in more than one parent turn.
                    identity = [identity, result['turn_id']] if identity and result.get('turn_id') else None
            dedup = hashlib.sha256(json.dumps([installation_id, session, result.get('agent_id'), event, identity]).encode()).hexdigest() if identity else None
            result['deduplication'] = 'vendor_correlation' if identity else 'unavailable'
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT count(*) FROM observations').fetchone()[0] >= MAX_ROWS:
                dropped(db)
                return
            db.execute('INSERT OR IGNORE INTO observations(dedup, metadata) VALUES (?, ?)', (dedup, json.dumps(result, sort_keys=True)))
            db.commit()
        except (ValueError, TypeError, RecursionError):
            dropped(db)
    finally:
        db.close()


def main():
    # POSIX-only pilot. Deadline also covers an input pipe that never closes.
    if os.name != 'posix' or fcntl is None:
        return 0
    os.umask(0o077)
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError()))
    signal.setitimer(signal.ITIMER_REAL, 0.7)
    try:
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument('--state-dir', type=Path, required=True)
        parser.add_argument('--installation-id', required=True)
        args = parser.parse_args()
        collect(args.state_dir, args.installation_id, sys.stdin.buffer)
    except BaseException:
        # Neutral for every event, including Stop; never emit decisions/context.
        pass
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    return 0


if __name__ == '__main__':
    sys.exit(main())
