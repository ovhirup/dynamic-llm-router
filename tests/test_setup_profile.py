"""Setup profile storage never reads or overwrites legacy settings (#210)."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from helpers import load_policy
from llm_router import config

NOW = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)


def fragment():
    from llm_router.setup_wizard import build_profile
    tools = {'codex': {'installed': True, 'auth': 'verified',
                       'requested_capabilities': {'worker': True, 'orchestrator': True},
                       'smoke': {'passed': True, 'status': 'passed', 'auth': 'verified'}}}
    return build_profile(tools, reserves={'codex': 15}, preferred_orchestrator='codex')


class SetupProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.path = self.base / 'setup.json'

    def test_preferred_path_explicit_env_setup_then_legacy(self):
        from llm_router import setup_profile
        with mock.patch.object(config, 'config_dir', return_value=self.base):
            with mock.patch.object(config, 'config_file', return_value=self.base / 'config.json'):
                self.assertEqual(setup_profile.profile_path(), self.path)
                self.assertEqual(setup_profile.preferred_config_path('chosen.json', {'LLM_ROUTER_CONFIG': 'env.json'}),
                                 Path('chosen.json'))
                self.assertEqual(setup_profile.preferred_config_path(env={'LLM_ROUTER_CONFIG': 'env.json'}), Path('env.json'))
                self.assertEqual(setup_profile.preferred_config_path(env={}), self.base / 'config.json')
                self.path.write_text('{}')
                self.assertEqual(setup_profile.preferred_config_path(env={}), self.path)

    def test_valid_fragment_is_independent_and_hard_threshold_stays_85(self):
        from llm_router import setup_profile
        data = fragment()
        before = deepcopy(data)
        validated = setup_profile.validate_profile(data)
        self.assertEqual(data, before)
        self.assertEqual(config.validate(config.deep_merge(config.defaults(), validated))['quota']['codex']['usage_threshold_percent'], 85)
        validated['setup']['enabled_workers'].clear()
        self.assertEqual(data['setup']['enabled_workers'], ['codex'])

    def test_atomic_private_write_and_unique_backup_preserve_previous_bytes(self):
        from llm_router import setup_profile
        data = fragment()
        first = setup_profile.write_profile(data, self.path, now=NOW)
        self.assertIsNone(first['backup'])
        old = self.path.read_bytes()
        second = setup_profile.write_profile(data, self.path, now=NOW)
        third = setup_profile.write_profile(data, self.path, now=NOW)
        self.assertNotEqual(second['backup'], third['backup'])
        self.assertEqual(Path(second['backup']).read_bytes(), old)
        for path in (self.path, Path(second['backup']), Path(third['backup'])):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.path.read_text()), data)
        self.assertEqual(list(self.base.glob('*.tmp')), [])

    def test_forbidden_paths_rejected_before_reading_and_symlinks_refused(self):
        from llm_router import setup_profile
        data = fragment()
        targets = [self.base / 'config.json', self.base / 'repos.json', self.base / '.env',
                   self.base / '.env.local', self.base / 'private_terms.txt', self.base / 'secrets.json',
                   Path.home() / '.codex' / 'setup.json']
        with mock.patch.object(Path, 'read_bytes', side_effect=AssertionError('forbidden read')):
            for target in targets:
                with self.subTest(target=target.name), self.assertRaises(config.ConfigError):
                    setup_profile.write_profile(data, target, now=NOW)
        self.path.write_text('previous')
        alias = self.base / 'alias.json'
        alias.symlink_to(self.path)
        with self.assertRaises(config.ConfigError):
            setup_profile.write_profile(data, alias)
        self.assertEqual(self.path.read_text(), 'previous')
        protected = self.base / '.codex'
        protected.mkdir()
        parent_link = self.base / 'linked'
        parent_link.symlink_to(protected, target_is_directory=True)
        with self.assertRaises(config.ConfigError):
            setup_profile.write_profile(data, parent_link / 'setup.json')

    def test_invalid_profile_does_not_change_destination(self):
        from llm_router import setup_profile
        data = fragment()
        self.path.write_text('previous')
        invalid = []
        duplicate = deepcopy(data)
        duplicate['setup']['enabled_workers'].append('codex')
        invalid.append(duplicate)
        bad_pin = deepcopy(data)
        bad_pin['setup']['worker_overrides']['default'] = {'worker': 'cursor', 'reason': 'disabled'}
        invalid.append(bad_pin)
        no_reason = deepcopy(data)
        no_reason['setup']['orchestrator_overrides']['roles']['judge'] = {'orchestrator': 'codex', 'reason': ''}
        invalid.append(no_reason)
        fixed_disabled = deepcopy(data)
        fixed_disabled['setup']['switch_policy'] = {'mode': 'always_use', 'target': 'cursor'}
        invalid.append(fixed_disabled)
        bad_reserve = deepcopy(data)
        bad_reserve['allocation']['providers']['codex']['reserve_percent'] = 100
        invalid.append(bad_reserve)
        for value in invalid:
            with self.subTest(value=invalid.index(value)), self.assertRaises(config.ConfigError):
                setup_profile.write_profile(value, self.path)
            self.assertEqual(self.path.read_text(), 'previous')

    def test_unverified_tools_stay_enabled_but_labels_must_match_the_smoke(self):
        from llm_router import setup_profile
        data = fragment()
        data['setup']['tools']['codex']['smoke'] = {'passed': False, 'status': 'unverified', 'auth': 'unknown',
                                                    'reason': 'consent_declined'}
        data['setup']['tools']['codex']['auth'] = 'unknown'
        self.assertEqual(setup_profile.validate_profile(data)['setup']['enabled_workers'], ['codex'])
        claimed = deepcopy(data)
        claimed['setup']['tools']['codex']['auth'] = 'verified'
        with self.assertRaises(config.ConfigError):
            setup_profile.validate_profile(claimed)

    def test_failed_backup_prevents_replacement(self):
        from llm_router import setup_profile
        data = fragment()
        setup_profile.write_profile(data, self.path, now=NOW)
        old = self.path.read_bytes()
        real_open = os.open
        def fail_backup(path, flags, *args, **kwargs):
            if '.backup-' in str(path):
                raise OSError('backup unavailable')
            return real_open(path, flags, *args, **kwargs)
        with mock.patch('llm_router.setup_profile.os.open', side_effect=fail_backup):
            with self.assertRaises(OSError):
                setup_profile.write_profile(data, self.path, now=NOW)
        self.assertEqual(self.path.read_bytes(), old)
