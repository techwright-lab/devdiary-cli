import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from devdiary import observer


class ObserverTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo's space"
        self.repo.mkdir()
        self.settings = self.root / 'settings.json'
        self.state = self.root / 'private'

    def plan(self, binding=None):
        return observer.plan(self.settings, self.state, self.repo, Path(sys.executable), binding)

    def install(self, binding=None):
        p = self.plan(binding)
        observer.apply(p, consent=True)
        return p

    def fire(self, event='SessionStart', read_back=True, **extra):
        manifest = json.loads((self.state / 'manifest.json').read_text())
        data = dict(session_id='session-1', cwd=str(self.repo), hook_event_name=event)
        data.update(extra)
        result = subprocess.run([sys.executable, '-I', observer.HOOK_SCRIPT, '--state-dir', str(self.state), '--installation-id', manifest['installation_id']], input=json.dumps(data).encode(), capture_output=True, timeout=2)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'', b''))
        return observer.observations(self.state) if read_back else []

    def test_fresh_consent_idempotent_remove(self):
        p = self.plan()
        self.assertFalse(self.state.exists())
        self.assertFalse(self.settings.exists())
        with self.assertRaises(ValueError):
            observer.apply(p, consent=False)
        observer.apply(p, consent=True)
        before = self.settings.read_bytes()
        observer.apply(self.plan(), consent=True)
        self.assertEqual(before, self.settings.read_bytes())
        observer.remove(self.state, consent=True)
        self.assertFalse(self.settings.exists())
        self.assertEqual(observer.health(self.state)['registration'], 'removed')
        self.assertEqual(self.fire(), [])
        observer.remove(self.state, consent=True)

    def test_preserve_existing_and_customer_additions(self):
        original = b'{ "custom": 42, "hooks": {"Stop": [{"hooks": [{"type":"command","command":"customer"}]}]}}\n'
        self.settings.write_bytes(original)
        self.install()
        observer.remove(self.state, consent=True)
        # Unrelated values survive semantically; no credential-bearing backup.
        self.assertEqual(json.loads(self.settings.read_bytes()), json.loads(original))
        self.install()
        config = json.loads(self.settings.read_text())
        config['another'] = {'key': True}
        config['hooks']['Stop'].append({'hooks': [{'type': 'command', 'command': 'later'}]})
        self.settings.write_text(json.dumps(config))
        observer.remove(self.state, consent=True)
        config = json.loads(self.settings.read_text())
        self.assertEqual(config['another'], {'key': True})
        self.assertEqual(len(config['hooks']['Stop']), 2)

    def test_stale_plan_and_edited_owned_group_conflict(self):
        p = self.plan()
        self.settings.write_text('{}')
        with self.assertRaises(ValueError):
            observer.apply(p, consent=True)
        self.install()
        config = json.loads(self.settings.read_text())
        config['hooks']['Stop'][-1]['hooks'][0]['timeout'] = 10
        changed = json.dumps(config)
        self.settings.write_text(changed)
        with self.assertRaises(ValueError):
            observer.remove(self.state, consent=True)
        self.assertEqual(self.settings.read_text(), changed)
        self.assertEqual(observer.health(self.state)['registration'], 'conflict')

    def test_privacy_unknown_dedup_and_lifecycle(self):
        self.install()
        events = self.fire('Stop', prompt_id='prompt-1', last_assistant_message='SECRET', transcript_path='/SECRET', prompt='SECRET', tool_input={'secret': 'SECRET'})
        self.assertIsNone(events[0]['actor_ref'])
        self.assertEqual(events[0]['attribution_basis'], 'unknown')
        self.fire('Stop', prompt_id='prompt-1')
        self.assertEqual(len(observer.observations(self.state)), 1)
        h = observer.health(self.state)
        self.assertEqual(h['sessions'][0]['lifecycle'], 'incomplete_missing_start_and_end')
        self.fire('SessionStart', source='resume')
        self.fire('Stop', prompt_id='prompt-2')
        self.assertEqual(observer.health(self.state)['sessions'][0]['lifecycle'], 'open_or_incomplete')
        self.fire('SessionEnd', reason='other')
        self.assertEqual(observer.health(self.state)['sessions'][0]['lifecycle'], 'end_observed')
        self.fire('SessionStart', source='resume')
        self.assertEqual(observer.health(self.state)['sessions'][0]['lifecycle'], 'open_or_incomplete')
        for file in self.state.iterdir():
            if file.is_file():
                self.assertNotIn(b'SECRET', file.read_bytes())

    def test_exact_binding_no_child_or_other_session_inheritance(self):
        self.install({'session_id': 'session-1', 'agent_id': None, 'actor_ref': 'agent:known'})
        self.assertEqual(self.fire()[0]['actor_ref'], 'agent:known')
        self.assertIsNone(self.fire('PreToolUse', agent_id='child', tool_use_id='tool-1')[-1]['actor_ref'])
        self.assertIsNone(self.fire('Stop', session_id='other')[-1]['actor_ref'])
        self.assertEqual(observer.observations(self.state)[0]['attribution_basis'], 'explicit_local_binding')

    def test_disabled_scope_oversize_malformed_failure_neutral(self):
        self.install()
        self.assertEqual(self.fire(cwd=str(self.root)), [])
        self.assertEqual(self.fire(prompt='x' * 70000), [])
        self.assertEqual(self.fire(session_id={'bad': 'SECRET'}), [])
        self.assertGreater(observer.health(self.state)['dropped'], 0)
        (self.state / 'observations.sqlite3').unlink()
        (self.state / 'observations.sqlite3').mkdir()
        self.fire(read_back=False)
        self.assertEqual(observer.health(self.state)['storage'], 'unavailable')

    def test_executable_and_symlink_safety(self):
        with self.assertRaises(ValueError):
            observer.plan(self.settings, self.state, self.repo, Path('python'))
        target = self.root / 'other'
        target.write_text('{}')
        self.settings.symlink_to(target)
        with self.assertRaises((ValueError, RuntimeError)):
            self.plan()
        self.assertEqual(target.read_text(), '{}')

    def test_saved_plan_reapply_is_byte_idempotent(self):
        p = self.install()
        raw = self.settings.read_bytes()
        observer.apply(p, consent=True)
        self.assertEqual(self.settings.read_bytes(), raw)

    def test_settings_secret_not_copied_to_plan_or_manifest(self):
        self.settings.write_text(json.dumps({'env': {'TOKEN': 'PRIVATE_SENTINEL'}}))
        p = self.install()
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(p))
        self.assertNotIn('PRIVATE_SENTINEL', (self.state / 'manifest.json').read_text())
        observer.remove(self.state, consent=True)
        self.assertEqual(json.loads(self.settings.read_text())['env']['TOKEN'], 'PRIVATE_SENTINEL')

    def test_missing_end_and_subagent_stop_do_not_complete_parent(self):
        self.install()
        self.fire('SessionStart', source='startup')
        self.fire('SubagentStart', agent_id='child', agent_type='Explore')
        self.fire('SubagentStop', agent_id='child', last_assistant_message='PRIVATE')
        self.assertEqual(observer.health(self.state)['sessions'][0]['lifecycle'], 'open_or_incomplete')
        self.fire('SessionStart', source='resume')
        self.assertEqual(len(observer.observations(self.state)), 4)
        self.assertEqual(observer.observations(self.state)[-1]['deduplication'], 'unavailable')

    def test_tool_duplicates_and_concurrent_sessions_are_separate(self):
        self.install()
        self.fire('PreToolUse', tool_use_id='one', tool_input={'command': 'SECRET'})
        self.fire('PreToolUse', tool_use_id='one')
        self.fire('PostToolUse', tool_use_id='one', tool_response='SECRET')
        self.fire('PreToolUse', tool_use_id='one', session_id='other')
        self.assertEqual(len(observer.observations(self.state)), 3)
        self.assertEqual(len(observer.health(self.state)['sessions']), 2)

    def test_target_disabled_and_missing_owned_hooks_visible(self):
        self.settings.write_text('{"disableAllHooks":true}')
        self.install()
        self.assertEqual(observer.health(self.state)['registration'], 'disabled_by_target_settings')
        config = json.loads(self.settings.read_text())
        del config['hooks']['SessionEnd']
        self.settings.write_text(json.dumps(config))
        self.assertEqual(observer.health(self.state)['registration'], 'conflict')
        with self.assertRaises(ValueError):
            observer.remove(self.state, consent=True)

    def test_deadline_for_stdin_that_never_closes(self):
        self.install()
        m = observer.manifest(self.state)
        assert m is not None
        child = subprocess.Popen([sys.executable, '-I', observer.HOOK_SCRIPT, '--state-dir', str(self.state), '--installation-id', m['installation_id']], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert child.stdin is not None and child.stdout is not None and child.stderr is not None
        try:
            self.assertEqual(child.wait(timeout=1.5), 0)
            self.assertEqual(child.stdout.read(), b'')
            self.assertEqual(child.stderr.read(), b'')
        finally:
            child.kill() if child.poll() is None else None
            child.stdin.close()
            child.stdout.close()
            child.stderr.close()
            child.wait()

    def test_spool_capacity_and_database_lock_are_neutral(self):
        from devdiary import observer_hook
        from unittest.mock import patch
        import io
        import sqlite3
        self.install()
        data = json.dumps({'hook_event_name': 'Stop', 'session_id': 'one', 'cwd': str(self.repo)}).encode()
        m = observer.manifest(self.state)
        assert m is not None
        with patch.object(observer_hook, 'MAX_ROWS', 0):
            observer_hook.collect(self.state, m['installation_id'], io.BytesIO(data))
        self.assertEqual(observer.health(self.state)['dropped'], 1)
        with sqlite3.connect(self.state / 'observations.sqlite3') as db:
            db.execute('BEGIN EXCLUSIVE')
            self.fire(read_back=False)
        self.assertEqual(observer.observations(self.state), [])

    def test_crash_recovery_after_settings_apply_and_remove(self):
        from unittest.mock import patch
        original = observer.save_manifest
        def crash_install(state, record):
            if record['status'] == 'installed':
                raise OSError('fixture crash')
            original(state, record)
        with patch.object(observer, 'save_manifest', side_effect=crash_install):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual(observer.health(self.state)['registration'], 'prepared')
        observer.apply(self.plan(), consent=True)
        self.assertEqual(observer.health(self.state)['registration'], 'installed')
        def crash_remove(state, record):
            if record['status'] == 'removed':
                raise OSError('fixture crash')
            original(state, record)
        with patch.object(observer, 'save_manifest', side_effect=crash_remove):
            with self.assertRaises(OSError):
                observer.remove(self.state, consent=True)
        self.assertEqual(self.fire(), [])
        observer.remove(self.state, consent=True)
        self.assertEqual(observer.health(self.state)['registration'], 'removed')

    def test_cas_recheck_rejects_edit_while_preparing(self):
        from unittest.mock import patch
        p = self.plan()
        real = observer.save_manifest
        def edit_settings(state, record):
            real(state, record)
            self.settings.write_text('{"customer":"concurrent"}')
        with patch.object(observer, 'save_manifest', side_effect=edit_settings):
            with self.assertRaises(ValueError):
                observer.apply(p, consent=True)
        self.assertEqual(json.loads(self.settings.read_text()), {'customer': 'concurrent'})
        observer.apply(self.plan(), consent=True)
        self.assertEqual(json.loads(self.settings.read_text())['customer'], 'concurrent')

    def test_cli_plan_apply_health_and_exact_actor_validation(self):
        from devdiary.cli import main
        import contextlib
        import io
        args = ['observer', 'plan', '--settings', str(self.settings), '--state-dir', str(self.state), '--repository', str(self.repo), '--executable', sys.executable]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(args), 0)
        p = self.root / 'plan.json'
        p.write_text(output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['observer', 'apply', '--plan', str(p)]), 2)
            self.assertEqual(main(['observer', 'apply', '--plan', str(p), '--consent']), 0)
            self.assertEqual(main(['observer', 'health', '--state-dir', str(self.state)]), 0)
            self.assertEqual(main(args + ['--actor-ref', 'claude-code']), 2)

    def test_cli_binding_requires_exact_registered_actor_not_alias(self):
        from devdiary.cli import main
        import contextlib
        import io
        registry = self.root / 'registry.json'
        prefix = ['--config', str(registry)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(prefix + ['init', '--namespace', 'urn:fixture', '--principal-ref', 'urn:fixture:human:owner']), 0)
            self.assertEqual(main(prefix + ['actor', 'add', '--ref', 'urn:fixture:actor:one', '--display-name', 'One', '--attester-ref', 'urn:fixture:attester:local', '--alias', 'alias-one']), 0)
        args = prefix + ['observer', 'plan', '--settings', str(self.settings), '--state-dir', str(self.state), '--repository', str(self.repo), '--executable', sys.executable, '--binding-session', 'session-1']
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(args + ['--actor-ref', 'alias-one']), 2)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(args + ['--actor-ref', 'urn:fixture:actor:one']), 0)
        p = self.root / 'plan.json'
        p.write_text(output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(prefix + ['observer', 'apply', '--plan', str(p), '--consent']), 0)
        self.assertEqual(self.fire()[0]['actor_ref'], 'urn:fixture:actor:one')

    def test_duplicate_json_keys_and_tampered_command_are_rejected(self):
        self.settings.write_text('{"customer":1,"customer":2}')
        with self.assertRaises(ValueError):
            self.plan()
        self.settings.unlink()
        p = self.plan()
        p['groups']['Stop']['hooks'][0]['command'] = 'not-approved'
        with self.assertRaises(ValueError):
            observer.apply(p, consent=True)
        self.assertFalse(self.settings.exists())

    def test_generated_shell_command_runs_isolated_handler(self):
        self.install()
        command = json.loads(self.settings.read_text())['hooks']['SessionStart'][0]['hooks'][0]['command']
        result = subprocess.run(command, shell=True, input=json.dumps({'hook_event_name': 'SessionStart', 'session_id': 'shell', 'cwd': str(self.repo)}).encode(), capture_output=True, timeout=2, env={'PATH': '/nonexistent'})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'', b''))
        self.assertEqual(observer.observations(self.state)[0]['session_id'], 'shell')


if __name__ == '__main__':
    unittest.main()
