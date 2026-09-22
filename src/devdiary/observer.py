"""Consented, local-only stock-tool hooks and observation inspection."""
from __future__ import annotations

import copy
try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import json
import os
from pathlib import Path
import shlex
import sqlite3
import stat
import tempfile
import uuid
from contextlib import contextmanager

from devdiary import observer_hook
from devdiary.secure_paths import reject_symlink_components

HOOK_SCRIPT = str(Path(observer_hook.__file__).resolve())
LIMIT = 1024 * 1024


def supported_platform():
    return os.name == 'posix' and fcntl is not None


def require_platform():
    if not supported_platform():
        raise ValueError('observer_platform_unsupported')


def discover(vendor=None):
    from devdiary.observer_discovery import discover as probe
    return probe(vendor)


def vendor_name(vendor):
    if vendor not in observer_hook.VENDORS:
        raise ValueError('unsupported_vendor')
    return vendor


def digest(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def read(path):
    reject_symlink_components(path)
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > LIMIT:
        raise ValueError('invalid_control_file')
    return path.read_bytes()


def document(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_settings_key')
            result[key] = value
        return result
    result = json.loads(raw, object_pairs_hook=unique) if raw is not None else {}
    if not isinstance(result, dict) or not isinstance(result.get('hooks', {}), dict):
        raise ValueError('invalid_settings')
    for groups in result.get('hooks', {}).values():
        if not isinstance(groups, list):
            raise ValueError('invalid_hooks')
    return result


def absolute(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('absolute_path_required')
    reject_symlink_components(path)
    return path


def trusted_executable(path, *, executable=True):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('absolute_executable_required')
    path = path.resolve(strict=True)
    for item in (path, *path.parents):
        info = item.stat()
        if info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022:
            # A sticky /tmp is an acceptable fixture ancestor, not executable.
            if item != path and stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX:
                continue
            raise ValueError('untrusted_executable_path')
    if not path.is_file() or (executable and not os.access(path, os.X_OK)):
        raise ValueError('executable_required')
    return str(path)


def encode(value):
    return (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()


def atomic(path, raw, expected):
    """Optimistic CAS + atomic replace; cooperating writers hold state lock.

    Non-cooperating vendor editors cannot be locked by a portable filesystem CAS;
    recheck immediately before replace and require a quiescent host for setup.
    """
    if digest(read(path)) != expected:
        raise ValueError('settings_changed_replan')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.devdiary-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        if path.exists():
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        if digest(read(path)) != expected:
            raise ValueError('settings_changed_replan')
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked(state):
    require_platform()
    assert fcntl is not None
    absolute(state)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    observer_hook.safe_path(state)
    lock = state / 'registration.lock'
    reject_symlink_components(lock)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def manifest(state):
    raw = read(state / 'manifest.json')
    return json.loads(raw) if raw else None


def save_manifest(state, value):
    path = state / 'manifest.json'
    atomic(path, encode(value), digest(read(path)))


def groups_for(executable, state, installation_id, vendor='claude'):
    # The script is data to Python, not a PATH-resolved console script; -I
    # excludes cwd, PYTHONPATH and user site packages. stderr is private too.
    trusted_executable(Path(HOOK_SCRIPT), executable=False)
    command = shlex.join([executable, '-I', HOOK_SCRIPT, '--state-dir', str(state), '--installation-id', installation_id]) + ' >/dev/null 2>&1 || :'
    return {event: {'hooks': [{'type': 'command', 'command': command, 'timeout': 1}]} for event in observer_hook.VENDORS[vendor_name(vendor)][1]}


def plan(settings, state, repository, executable, binding=None, *, vendor='claude'):
    require_platform()
    vendor_name(vendor)
    settings, state, repository = map(absolute, (settings, state, repository))
    if vendor == 'codex' and settings.suffix == '.toml':
        raise ValueError('codex_hooks_json_required')
    if not repository.is_dir() or settings == state or state in settings.parents:
        raise ValueError('invalid_scope_paths')
    executable = trusted_executable(executable)
    old = manifest(state)
    if old and old['status'] != 'removed':
        if old.get('vendor', 'claude') != vendor:
            raise ValueError('existing_installation_remove_before_rebinding')
        if any(old[key] != value for key, value in [('settings', str(settings)), ('repository', str(repository)), ('executable', executable), ('binding', binding)]):
            raise ValueError('existing_installation_remove_before_rebinding')
        installation_id = old['installation_id']
    else:
        installation_id = str(uuid.uuid4())
    if binding is not None:
        if set(binding) != {'session_id', 'agent_id', 'actor_ref'} or not all(observer_hook.token(binding[k]) for k in ('session_id', 'actor_ref')) or (binding['agent_id'] is not None and not observer_hook.token(binding['agent_id'])):
            raise ValueError('invalid_exact_binding')
    raw = read(settings)
    document(raw)
    return {'schema_version': 1, 'vendor': vendor, 'settings': str(settings), 'state_dir': str(state),
            'repository': str(repository), 'executable': executable,
            'installation_id': installation_id, 'binding': binding,
            'expected_settings_sha256': digest(raw),
            'expected_manifest_sha256': digest(read(state / 'manifest.json')),
            'groups': groups_for(executable, state, installation_id, vendor),
            'trust_action': 'review_exact_definitions_in_codex_/hooks' if vendor == 'codex' else 'review_in_claude',
            'privacy': 'metadata_only', 'delivery': 'local_only',
            'compatibility': 'POSIX_documented_hooks_runtime_unqualified'}


def owned_counts(config, owned):
    return [config.get('hooks', {}).get(event, []).count(group) for event, group in owned.items()]


def apply(proposal, *, consent=False, vendor=None):
    require_platform()
    selected = vendor_name(proposal.get('vendor', 'claude'))
    if vendor is not None and vendor != selected:
        raise ValueError('vendor_mismatch')
    if not consent:
        raise ValueError('consent_required')
    state = absolute(proposal['state_dir'])
    settings = absolute(proposal['settings'])
    if selected == 'codex' and settings.suffix == '.toml':
        raise ValueError('codex_hooks_json_required')
    with locked(state):
        old = manifest(state)
        if old and old['status'] == 'installed' and all(old[k] == proposal[k] for k in ('settings', 'repository', 'executable', 'binding', 'installation_id', 'groups')):
            if old.get('vendor', 'claude') != selected:
                raise ValueError('vendor_mismatch')
            if owned_counts(document(read(settings)), old['groups']) == [1] * len(old['groups']):
                return {'registration': 'installed', 'delivery': 'local_only'}
        if digest(read(settings)) != proposal['expected_settings_sha256'] or digest(read(state / 'manifest.json')) != proposal['expected_manifest_sha256']:
            raise ValueError('stale_plan_replan')
        executable = trusted_executable(proposal['executable'])
        if proposal['groups'] != groups_for(executable, state, proposal['installation_id'], selected):
            raise ValueError('invalid_plan_commands')
        config = document(read(settings))
        if old and old['status'] in ('installed', 'prepared'):
            if old.get('vendor', 'claude') != selected:
                raise ValueError('vendor_mismatch')
            if any(old[k] != proposal[k] for k in ('settings', 'repository', 'executable', 'binding', 'installation_id', 'groups')):
                raise ValueError('manifest_conflict')
            if owned_counts(config, old['groups']) == [1] * len(old['groups']):
                old['status'] = 'installed'
                save_manifest(state, old)
                return {'registration': 'installed', 'delivery': 'local_only'}
            if old['status'] != 'prepared' or any(owned_counts(config, old['groups'])):
                raise ValueError('owned_registration_conflict')
        elif any(owned_counts(config, proposal['groups'])):
            raise ValueError('unowned_registration_conflict')
        updated = copy.deepcopy(config)
        for event, group in proposal['groups'].items():
            updated.setdefault('hooks', {}).setdefault(event, []).append(group)
        record = dict(proposal, status='prepared', settings_existed=read(settings) is not None,
                      hooks_existed='hooks' in config,
                      original_events=list(config.get('hooks', {})))
        save_manifest(state, record)
        db = observer_hook.connect(state)
        db.close()
        atomic(settings, encode(updated), proposal['expected_settings_sha256'])
        record['status'] = 'installed'
        save_manifest(state, record)
        return {'registration': 'installed', 'delivery': 'local_only'}


def remove(state, *, consent=False, vendor=None):
    require_platform()
    if not consent:
        raise ValueError('consent_required')
    state = absolute(state)
    with locked(state):
        old = manifest(state)
        if not old or old['status'] == 'removed':
            return {'registration': 'removed', 'retention': 'local_observations_retained'}
        if vendor is not None and old.get('vendor', 'claude') != vendor:
            raise ValueError('vendor_mismatch')
        settings = absolute(old['settings'])
        raw = read(settings)
        config = document(raw)
        if old['status'] == 'removing' and (digest(raw) == old.get('removed_settings_sha256') or (raw is None and not old['settings_existed'])):
            old['status'] = 'removed'
            save_manifest(state, old)
            return {'registration': 'removed', 'retention': 'local_observations_retained'}
        counts = owned_counts(config, old['groups'])
        if old['status'] == 'prepared' and not any(counts):
            old['status'] = 'removed'
            save_manifest(state, old)
            return {'registration': 'removed', 'retention': 'local_observations_retained'}
        if any(n != 1 for n in counts):
            raise ValueError('owned_registration_conflict')
        for event, group in old['groups'].items():
            config['hooks'][event].remove(group)
            if not config['hooks'][event] and event not in old['original_events']:
                del config['hooks'][event]
        if not config.get('hooks') and not old['hooks_existed']:
            config.pop('hooks', None)
        # Disable before editing host settings; stale host commands become no-ops.
        old['status'] = 'removing'
        old['removed_settings_sha256'] = digest(encode(config))
        save_manifest(state, old)
        atomic(settings, encode(config), digest(raw))
        if not config and not old['settings_existed']:
            # Only remove our exact empty replacement, not a customer edit.
            if read(settings) == encode({}):
                settings.unlink()
        old['status'] = 'removed'
        save_manifest(state, old)
        return {'registration': 'removed', 'retention': 'local_observations_retained'}


def observations(state, limit=100):
    require_platform()
    observer_hook.safe_path(absolute(state))
    path = absolute(state) / 'observations.sqlite3'
    if not path.exists():
        return []
    observer_hook.safe_path(path)
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.05) as db:
        return [json.loads(row[0]) for row in db.execute('SELECT metadata FROM observations ORDER BY seq DESC LIMIT ?', (min(max(int(limit), 1), observer_hook.MAX_ROWS),))][::-1]


def health(state):
    require_platform()
    state = absolute(state)
    old = manifest(state)
    result = {'delivery': 'local_only', 'runtime_qualified': False,
              'trust': 'not_observable_verify_in_host', 'registration': 'not_installed',
              'storage': 'unavailable', 'dropped': 0, 'sessions': [],
              'limitations': ['no_remote_upload', 'no_git_attribution_or_human_minutes',
                              'no_duration_inference', 'no_global_settings_or_policy_visibility',
                              'failures_before_storage_are_not_countable']}
    if not old:
        return result
    vendor = vendor_name(old.get('vendor', 'claude'))
    result.update(vendor=vendor, tool=discover(vendor)[0])
    if vendor == 'codex':
        result['trust'] = 'not_observable_review_in_codex_/hooks'
        result['limitations'].extend(['codex_feature_and_managed_policy_not_inspected',
                                     'hosted_tools_not_observed'])
    database = state / 'observations.sqlite3'
    if database.exists() and not database.is_file():
        result['registration'] = old['status']
        return result
    try:
        observer_hook.safe_path(state)
        observer_hook.safe_path(state / 'manifest.json')
        observer_hook.safe_path(state / 'registration.lock')
        observer_hook.safe_path(state / 'observations.sqlite3')
    except (OSError, ValueError):
        result.update(storage='unsafe_permissions', registration=old['status'])
        return result
    config = document(read(Path(old['settings'])))
    result['registration'] = old['status']
    if old['status'] == 'installed':
        if owned_counts(config, old['groups']) != [1] * len(old['groups']):
            result['registration'] = 'conflict'
        elif vendor == 'claude' and (config.get('disableAllHooks') is True or config.get('allowManagedHooksOnly') is True):
            result['registration'] = 'disabled_by_target_settings'
        elif not Path(old['executable']).is_file() or not Path(HOOK_SCRIPT).is_file():
            result['registration'] = 'executable_missing'
    try:
        rows = observations(state, observer_hook.MAX_ROWS)
        database = state / 'observations.sqlite3'
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=0.05) as db:
            count = db.execute("SELECT value FROM counters WHERE name='dropped'").fetchone()
            result['dropped'] = count[0] if count else 0
        result.update(storage='ok', observation_count=len(rows), coverage='observed' if rows else 'awaiting_events')
        sessions = {}
        for row in rows:
            key = (row['installation_id'], row['session_id'])
            session = sessions.setdefault(key, {'session_id': row['session_id'], 'installation_id': row['installation_id'], 'start_observed': False, 'end_observed': False, 'turn_stops': 0})
            if row['event'] == 'SessionStart':
                session.update(start_observed=True, end_observed=False)
            elif row['event'] == 'SessionEnd':
                session['end_observed'] = True
            elif row['event'] == 'Stop':
                session['turn_stops'] += 1
        for session in sessions.values():
            session['lifecycle'] = ('end_observed' if session['start_observed'] else 'incomplete_missing_start') if session['end_observed'] else ('open_or_incomplete' if session['start_observed'] else 'incomplete_missing_start_and_end')
        result['sessions'] = list(sessions.values())
        from devdiary import observer_upload
        result['upload'] = observer_upload.health(state)
        if (state / observer_upload.CONNECTION).exists():
            result['delivery'] = 'explicit_sync_only'
            result['limitations'].remove('no_remote_upload')
    except (OSError, ValueError, sqlite3.Error):
        pass
    return result
