"""Catches bank changes, implicit cwd fallback, and policy/filter loss."""
import json
import os
from pathlib import Path
import tempfile
import unittest

try:
    import policy as p
except ModuleNotFoundError:
    p = None


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(p, 'shared policy implementation is missing')
        self.policy = p.normalize_policy({})

    def identity(self, **kw):
        return p.resolve_identity(session_id='chat-a', cwd='/srv/team-a/project',
                                  selected_cwd='/srv/team-a/project', policy=self.policy, **kw)

    def test_bank_ids_match_frozen_legacy_outputs(self):
        cases = json.loads((Path(__file__).parent / 'fixtures/bank-ids.json').read_text())
        for case in cases:
            cwd = str(Path.home()) if case['cwd'] == '$HOME' else case['cwd']
            with self.subTest(cwd=cwd):
                self.assertEqual(p.resolve_workspace(case['explicit'], cwd), case['workspace'])
                self.assertEqual(p.resolve_identity(session_id='a', cwd=cwd, selected_cwd=cwd,
                                                   policy=self.policy, explicit=case['explicit'])['bank'], case['bank'])

    def test_unknown_or_relative_cwd_never_falls_back_even_with_explicit_bank(self):
        for cwd in (None, '', '.', '~/project', 'relative', '/bad\x00path', '/bad\npath', b'/bytes'):
            with self.subTest(cwd=cwd), self.assertRaises(ValueError):
                p.resolve_identity(session_id='a', cwd=cwd, selected_cwd=cwd,
                                   policy=self.policy, explicit='previous-bank')

    def test_native_home_is_valid_without_creating_chat_folders(self):
        cwd = str(Path.home())
        self.assertEqual(p.resolve_identity(session_id='home-chat', cwd=cwd, selected_cwd=cwd,
                                           policy=self.policy)['bank'], 'home')

    def test_selected_path_mismatch_and_missing_session_refused(self):
        for sid, cwd, selected in [('a', '/wrong', '/selected'), ('', '/p', '/p'),
                                   ('a\n', '/p', '/p'), ('a', '/p', None)]:
            with self.subTest(sid=sid, cwd=cwd), self.assertRaises(ValueError):
                p.resolve_identity(session_id=sid, cwd=cwd, selected_cwd=selected, policy=self.policy)

    def test_lexical_normalization_and_session_independent_bank(self):
        first = self.identity()
        second = p.resolve_identity(session_id='chat-b', cwd='/srv/team-a/./project/',
                                    selected_cwd='/srv/team-a/project', policy=self.policy)
        self.assertNotEqual(first['session_id'], second['session_id'])
        self.assertEqual(first['bank'], second['bank'])
        self.assertEqual(first['cwd'], second['cwd'])

    def test_projection_has_agreed_defaults_and_no_repository_override(self):
        projection = p.hermes_projection(self.policy)
        self.assertEqual(projection['recall_max_input_chars'], 2000)
        self.assertEqual(projection['recall_max_tokens'], 4096)
        self.assertEqual(projection['recall_budget'], 'mid')
        self.assertEqual(projection['recall_types'], ['world', 'experience', 'observation'])
        self.assertEqual(projection['observation_scopes'], 'shared')
        self.assertEqual(projection['timeout'], 120)
        self.assertEqual(projection['retain_every_n_turns'], 1)
        self.assertEqual(projection['bank_id_template'], '{workspace}')
        self.assertEqual(projection['trusted_project_dirs'], [])
        self.assertNotIn('workspace_from_cwd', projection)

    def test_read_write_order_and_read_only_exclusion(self):
        policy = p.normalize_policy({'mirror_to_own_bank': True, 'bank_id': 'own',
                                     'additional_banks': ['extra', 'own', 'primary'],
                                     'recall_additional_banks': 'reference,extra,reference'})
        self.assertEqual(p.bank_sets(policy, 'primary'),
                         (['primary', 'own', 'extra'], ['primary', 'own', 'extra', 'reference']))

    def test_filters_round_trip_and_reflect_does_not_inherit_recall(self):
        policy = p.normalize_policy({'recall_tags': 'team:a, reviewed', 'recall_tags_match': 'all',
                                     'recall_min_scores': {'world': 0.3}, 'retain_tags': ['common'],
                                     'retain_strategy': 'custom', 'retain_context': 'Shared conversation'})
        projection = p.hermes_projection(policy)
        self.assertEqual(projection['recall_tags'], ['team:a', 'reviewed'])
        self.assertEqual(projection['recall_tags_match'], 'all')
        self.assertEqual(projection['recall_min_scores'], {'world': 0.3})
        self.assertEqual(projection['retain_strategy'], 'custom')
        self.assertEqual(projection['retain_context'], 'Shared conversation')
        self.assertEqual(projection['reflect_tags'], [])
        self.assertEqual(projection['reflect_tags_match'], 'any')

    def test_policy_change_invalidates_binding(self):
        pin = self.identity()
        p.validate_binding(pin, self.policy, session_id='chat-a', cwd='/srv/team-a/project')
        for delta in ({'api_url': 'http://127.0.0.1:9000'}, {'recall_budget': 'high'},
                      {'reflect_tags': ['other']}, {'recall_max_input_chars': 1500}):
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                p.validate_binding(pin, p.normalize_policy(delta), session_id='chat-a', cwd=pin['cwd'])
        for field, value in [('session_id', 'other'), ('bank', 'wrong'), ('cwd', '/other')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                p.validate_binding({**pin, field:value}, self.policy, session_id='chat-a', cwd=pin['cwd'])

    def test_secrets_are_not_in_policy_or_binding(self):
        policy = p.normalize_policy({'api_key': 'SYNTHETIC_SECRET_A'})
        same = p.normalize_policy({'api_key': 'SYNTHETIC_SECRET_B'})
        self.assertEqual(policy, same)
        self.assertNotIn('SYNTHETIC_SECRET', json.dumps(policy))
        self.assertEqual(p.policy_hash(policy), p.policy_hash(same))

    def test_native_api_key_alias_is_also_secret_free(self):
        try:
            result = p.normalize_policy({'apiKey': 'SYNTHETIC_NATIVE_KEY'})
        except ValueError:
            self.fail('native apiKey credential alias must remain supported without export')
        self.assertEqual(result, self.policy)

    def test_conflicting_aliases_and_unsupported_settings_fail_explicitly(self):
        invalid = [{'bank_id_template': '{project}'}, {'trusted_project_dirs': ['/repo']},
                   {'future_functional_setting': True}, {'budget': 'high', 'recall_budget': 'low'},
                   {'mode': 'local_embedded'}, {'workspace_from_cwd': 'true'},
                   {'observation_scopes': 'not-a-scope'}, {'retain_async': 'false'}, {'retain_async': False},
                   {'recall_tags_match': 'typo'}, {'api_url': 'http://user:pass@localhost/'},
                   {'timeout': -1}, {'recall_min_scores': {'world': float('nan')}},
                   {'additional_banks': ['../bank']}, {'manageBankConfig': True}]
        for settings in invalid:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                p.normalize_policy(settings)

    def test_load_requires_explicit_path_and_is_non_mutating(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as temp:
            path = Path(temp) / 'config.json'
            raw = b'{"api_key":"SYNTHETIC_TOKEN","recall_max_input_chars":2000}'
            path.write_bytes(raw)
            self.assertEqual(p.load_policy(path), self.policy)
            self.assertEqual(path.read_bytes(), raw)
        with self.assertRaises(ValueError):
            p.load_policy(Path('relative.json'))


if __name__ == '__main__':
    unittest.main()
