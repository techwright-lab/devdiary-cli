"""Documented Codex fixtures; not a claim of stock-runtime qualification."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from typing import Any
from unittest.mock import patch

from devdiary import observer
import test_observer


class CodexObserverTest(test_observer.ObserverTest):
    def setUp(self):
        super().setUp()
        self.probe = patch.object(observer, 'discover', return_value=[{'support': 'fixture_not_qualified'}])
        self.probe.start()
        self.addCleanup(self.probe.stop)
    # Reuse all ownership, privacy, crash/CAS and fail-neutral tests per vendor.
    def plan(self, binding=None):
        return observer.plan(self.settings, self.state, self.repo, Path(sys.executable), binding, vendor='codex')

    def test_privacy_unknown_dedup_and_lifecycle(self):
        self.install()
        self.fire('SessionStart', source='startup', model='gpt-fixture')
        for _ in range(2):
            self.fire('Stop', turn_id='turn-1', prompt_id='ignored', prompt='SECRET',
                      last_assistant_message='SECRET', transcript_path='/SECRET')
        rows = observer.observations(self.state)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]['runtime'], 'codex')
        self.assertEqual(rows[-1]['source_tag'], 'codex')
        self.assertNotIn('prompt_id', rows[-1])
        self.assertEqual(rows[-1]['turn_id'], 'turn-1')
        self.assertIsNone(rows[-1]['actor_ref'])
        self.fire('SubagentStop', agent_id='child', turn_id='turn-1')
        self.fire('Interrupt', turn_id='turn-1')
        self.assertFalse(observer.health(self.state)['sessions'][0]['end_observed'])
        self.fire('SessionEnd', reason='other')
        self.assertTrue(observer.health(self.state)['sessions'][0]['end_observed'])
        for file in self.state.iterdir():
            if file.is_file():
                self.assertNotIn(b'SECRET', file.read_bytes())

    def test_codex_events_and_trust_not_fabricated(self):
        p = self.install()
        self.assertNotIn('PostToolUseFailure', p['groups'])
        self.assertIn('Interrupt', p['groups'])
        self.assertEqual(p['trust_action'], 'review_exact_definitions_in_codex_/hooks')
        self.fire('SessionStart')
        health = observer.health(self.state)
        self.assertFalse(health['runtime_qualified'])
        self.assertEqual(health['trust'], 'not_observable_review_in_codex_/hooks')
        self.assertEqual(health['vendor'], 'codex')

    def test_cli_codex_vendor_roundtrip(self):
        from devdiary.cli import main
        output = io.StringIO()
        args = ['observer', 'plan', '--vendor', 'codex', '--settings', str(self.settings),
                '--state-dir', str(self.state), '--repository', str(self.repo), '--executable', sys.executable]
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(args), 0)
        proposal = self.root / 'plan.json'
        proposal.write_text(output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['vendor'], 'codex')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['observer', 'apply', '--vendor', 'claude', '--plan', str(proposal), '--consent']), 2)
            self.assertFalse(self.settings.exists())
            self.assertEqual(main(['observer', 'apply', '--vendor', 'codex', '--plan', str(proposal), '--consent']), 0)
            self.assertEqual(main(['observer', 'remove', '--vendor', 'codex', '--state-dir', str(self.state), '--consent']), 0)
        self.assertFalse(self.settings.exists())

    def test_pinned_codex_wire_fields_are_allowlisted(self):
        # Input field names verified against rust-v0.153.4 generated schemas.
        self.install()
        common: dict[str, Any] = dict(model='gpt-fixture', permission_mode='default', transcript_path='/SECRET', turn_id='turn-1')
        self.fire('UserPromptSubmit', prompt='SECRET', **common)
        self.fire('PreToolUse', tool_name='Bash', tool_use_id='call-1', tool_input={'command': 'SECRET'}, **common)
        self.fire('PostToolUse', tool_name='Bash', tool_use_id='call-1', tool_input={'command': 'SECRET'}, tool_response='SECRET', **common)
        self.fire('SubagentStart', agent_id='child', agent_type='worker', **common)
        self.fire('SubagentStop', agent_id='child', agent_type='worker', agent_transcript_path='/SECRET', last_assistant_message='SECRET', stop_hook_active=False, **common)
        rows = observer.observations(self.state)
        self.assertEqual(len(rows), 5)
        self.assertNotIn('SECRET', json.dumps(rows))
        self.assertTrue(all(row['runtime'] == 'codex' and row['actor_ref'] is None for row in rows))
        before = len(rows)
        self.fire('PostToolUseFailure', tool_use_id='failure')
        self.assertEqual(len(observer.observations(self.state)), before)

    def test_codex_child_stop_can_repeat_across_turns(self):
        self.install()
        self.fire('SubagentStop', agent_id='child', turn_id='one')
        self.fire('SubagentStop', agent_id='child', turn_id='one')
        self.fire('SubagentStop', agent_id='child', turn_id='two')
        self.assertEqual(len(observer.observations(self.state)), 2)

    def test_vendor_rebinding_refused(self):
        self.install()
        with self.assertRaises(ValueError):
            observer.plan(self.settings, self.state, self.repo, Path(sys.executable), vendor='claude')
        with self.assertRaises(ValueError):
            observer.remove(self.state, consent=True, vendor='claude')
        observer.remove(self.state, consent=True, vendor='codex')

    def test_codex_never_writes_json_to_config_toml(self):
        self.settings = self.root / 'config.toml'
        with self.assertRaises(ValueError):
            self.plan()
        self.assertFalse(self.settings.exists())

    def test_codex_does_not_claim_claude_disable_flags(self):
        self.settings.write_text('{"disableAllHooks":true}')
        self.install()
        self.assertEqual(observer.health(self.state)['registration'], 'installed')

    def test_target_disabled_and_missing_owned_hooks_visible(self):
        # Codex feature/policy layers are not in hooks.json and aren't inspected.
        self.test_codex_does_not_claim_claude_disable_flags()


class DiscoveryTest(unittest.TestCase):
    state: Path
    settings: Path
    root: Path
    setUp = test_observer.ObserverTest.setUp
    plan = test_observer.ObserverTest.plan
    install = test_observer.ObserverTest.install
    def test_read_only_discovery_no_vendor_private_files(self):
        with patch('devdiary.observer_discovery.shutil.which', return_value=None):
            results = observer.discover()
        self.assertEqual([x['vendor'] for x in results], ['claude', 'codex'])
        self.assertTrue(all(x['support'] == 'not_installed' for x in results))
        self.assertFalse(self.state.exists())
        self.assertFalse(self.settings.exists())

    def test_unsupported_version_and_detected_interface(self):
        from devdiary.observer_discovery import classify
        old = classify('codex', 'codex-cli 0.1.0', 'usage: codex')
        self.assertEqual(old['support'], 'unsupported_interface')
        current = classify('codex', 'codex-cli 0.153.4', '--dangerously-bypass-hook-trust')
        self.assertEqual(current['version'], '0.153.4')
        self.assertEqual(current['support'], 'documented_interface_detected_runtime_unqualified')
        self.assertFalse(current['runtime_qualified'])
        self.assertEqual(classify('codex', 'SECRET', '')['support'], 'unknown_version')

    def test_script_wrapper_never_executed(self):
        wrapper = self.root / 'codex'
        marker = self.root / 'must-not-exist'
        wrapper.write_text('#!/bin/sh\ntouch ' + str(marker))
        wrapper.chmod(0o700)
        with patch('devdiary.observer_discovery.shutil.which', return_value=str(wrapper)):
            result = observer.discover('codex')[0]
        self.assertEqual(result['support'], 'script_wrapper_not_probed')
        self.assertFalse(marker.exists())

    def test_probe_credential_free_isolation_and_bounded_output(self):
        from devdiary.observer_discovery import probe
        import subprocess
        with patch('devdiary.observer_discovery.subprocess.Popen', wraps=subprocess.Popen) as spawn:
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'SECRET', 'ANTHROPIC_API_KEY': 'SECRET'}):
                self.assertIn('Python', probe(sys.executable, '--version', str(self.root)))
        passed = spawn.call_args.kwargs
        self.assertNotIn('OPENAI_API_KEY', passed['env'])
        self.assertNotIn('ANTHROPIC_API_KEY', passed['env'])
        self.assertEqual(passed['env']['HOME'], str(self.root))
        self.assertEqual(passed['cwd'], str(self.root))
        with patch('devdiary.observer_discovery.LIMIT', 1):
            with self.assertRaises(ValueError):
                probe(sys.executable, '--version', str(self.root))

    def test_windows_structured_unsupported(self):
        from devdiary.cli import main
        output = io.StringIO()
        with patch.object(observer, 'supported_platform', return_value=False), contextlib.redirect_stdout(output):
            self.assertEqual(main(['observer', 'health', '--state-dir', str(self.state)]), 2)
        self.assertEqual(json.loads(output.getvalue())['error_code'], 'observer_platform_unsupported')

    def test_insecure_state_health_not_ok(self):
        self.install()
        os.chmod(self.state, 0o755)
        self.assertEqual(observer.health(self.state)['storage'], 'unsafe_permissions')

    def test_legacy_claude_manifest_and_plan_remain_compatible(self):
        proposal = self.plan()
        proposal.pop('vendor')
        observer.apply(proposal, consent=True)
        manifest = observer.manifest(self.state)
        assert manifest is not None
        self.assertNotIn('vendor', manifest)
        before = self.settings.read_bytes()
        observer.apply(self.plan(), consent=True)
        self.assertEqual(before, self.settings.read_bytes())
        self.assertEqual(observer.health(self.state)['vendor'], 'claude')
        observer.remove(self.state, consent=True, vendor='claude')
