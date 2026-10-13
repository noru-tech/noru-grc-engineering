#!/usr/bin/env python3
"""Exercise privacy diff/push against current canonical evidence, not a parsed cache."""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

from test_collectors import SQL_FIXTURE, accepted_datamap_text, datamap_scan, seed_fixture_classification, write_files
from test_idempotency import write_state

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / 'plugins/privacy-datamap/scripts'


class PrivacyDeliveryGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = pathlib.Path(self.temp.name)
        write_files(self.repo, {'db/schema.sql': SQL_FIXTURE, 'writer.ts': 'export const producerVersion = 1;\n', '.gitignore': '.noru/\n.fides/\n'})
        self.command(['git', 'init', '-q', '-b', 'main'])
        self.command(['git', 'add', '.'])
        self.command(['git', '-c', 'user.name=Synthetic Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture'])
        self.head = self.command(['git', 'rev-parse', 'HEAD']).stdout.strip()
        summary, _ = datamap_scan(self.repo)
        (self.repo / '.noru/privacy-datamap.yml').write_text(accepted_datamap_text(summary['derived_digest']))
        seed_fixture_classification(self.repo)
        self.command([sys.executable, str(SCRIPTS / 'reconcile.py'), f'--repo={self.repo}', '--seal', '--quiet'])
        write_state(self.repo, {'fetched_at': '2026-10-12T09:00:00Z'})
        self.plan = json.loads(self.script('diff.mjs').stdout)
        self.calls_path = self.repo / '.noru/.cache/privacy-datamap.calls.json'
        self.parsed_path = self.repo / '.noru/.cache/privacy-datamap.parsed.json'

    def command(self, args, expected=0):
        result = subprocess.run(args, cwd=self.repo, capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, expected, result.stderr or result.stdout)
        return result

    def script(self, name, expected=0, *args):
        return self.command(['node', str(SCRIPTS / name), f'--repo={self.repo}', '--output=json', '--quiet', *args], expected)

    def test_valid_current_review_emits_the_validated_payload(self):
        result = json.loads(self.script('push.mjs', 0, '--confirm').stdout)
        self.assertEqual(result['calls'][0]['arguments'], self.plan['operations'][0]['arguments'])
        self.assertTrue(self.calls_path.is_file())

    def test_uncommitted_writer_change_blocks_diff_and_push(self):
        self.script('push.mjs', 0, '--confirm')
        (self.repo / 'writer.ts').write_text('export const producerVersion = 2;\n')
        self.assertEqual(self.command(['git', 'rev-parse', 'HEAD']).stdout.strip(), self.head)
        diff = self.script('diff.mjs', 1)
        self.assertIn('classification', diff.stderr)
        pushed = self.script('push.mjs', 1, '--confirm')
        self.assertIn('classification', pushed.stderr)
        self.assertFalse(self.calls_path.exists(), 'Previously emitted calls cannot survive a rejected classification gate')

    def test_fresh_unresolved_review_blocks_a_previously_accepted_plan(self):
        self.script('push.mjs', 0, '--confirm')
        self.command([sys.executable, str(SCRIPTS / 'field_review.py'), f'--repo={self.repo}', '--fresh'])
        self.script('diff.mjs', 1)
        result = self.script('push.mjs', 1, '--confirm')
        self.assertIn('classification', result.stderr)
        self.assertFalse(self.calls_path.exists())

    def test_diff_discards_tampered_parsed_cache(self):
        parsed = json.loads(self.parsed_path.read_text())
        parsed['dataset'][0]['collections'][0]['fields'][1]['data_categories'] = ['user.health_and_medical']
        self.parsed_path.write_text(json.dumps(parsed))
        rebuilt = json.loads(self.script('diff.mjs').stdout)
        self.assertEqual(rebuilt['operations'], self.plan['operations'])
        self.assertNotIn('user.health_and_medical', self.parsed_path.read_text())

    def test_push_rejects_tampered_plan_payload(self):
        plan_path = self.repo / '.noru/.cache/privacy-datamap.plan.json'
        plan = json.loads(plan_path.read_text())
        plan['operations'][0]['arguments']['manifest']['dataset'][0]['name'] = 'Unreviewed payload'
        plan_path.write_text(json.dumps(plan))
        result = self.script('push.mjs', 1, '--confirm')
        self.assertIn('planned payload differs', result.stderr)
        self.assertFalse(self.calls_path.exists())


if __name__ == '__main__':
    unittest.main()
