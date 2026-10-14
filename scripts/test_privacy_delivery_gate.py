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
        write_files(self.repo, {'db/schema.sql': SQL_FIXTURE, 'writer.ts': 'export const producerVersion = 1;\n', 'storage.toml': 'schema = "db/schema.sql"\nmigrations = "history/migrations"\n', '.gitignore': '.noru/\n.fides/\n'})
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

    def test_investigated_migration_gap_reaches_seal_export_and_preflight(self):
        sys.path.insert(0, str(SCRIPTS))
        import analysis_storage
        import field_review
        import gap_resolution
        import review
        migration = self.repo / 'history/migrations/001.sql'
        migration.parent.mkdir(parents=True)
        migration.write_text('CREATE TABLE obsolete_history (old_value TEXT);\n')
        (self.repo/'storage.toml').write_text('schema = "db/schema.sql"\nmigrations = "history/migrations"\n')
        self.command(['git', 'add', 'history', 'storage.toml'])
        datamap_scan(self.repo)
        self.command([sys.executable, str(SCRIPTS / 'reconcile.py'), f'--repo={self.repo}', '--output=json', '--quiet'])
        document = seed_fixture_classification(self.repo)
        derived = field_review.derived_for(document, self.repo)
        gaps = gap_resolution.calculate(document, derived, self.repo)['active_blockers']
        self.assertEqual(len(gaps), 1)
        self.command([sys.executable, str(SCRIPTS / 'reconcile.py'), f'--repo={self.repo}', '--seal', '--quiet'], 1)
        self.script('collect.mjs', 2, '--export')
        rows = list(field_review.all_rows(document['classification_review']['fields']))
        binding_span = field_review.span_from_ref(self.repo, 'storage.toml:1')
        binding_span['end_line'] = 2
        binding_span['end_column'] = len((self.repo/'storage.toml').read_text().splitlines()[1])+1
        binding_id = 'evidence:' + field_review.digest(binding_span)
        decisions = [{'gap_id': gaps[0]['gap_id'], 'outcome':'modeled',
            'covered_occurrence_ids':gap_resolution.occurrence_ids(gaps[0]),
            'field_ids':[row['entity_id'] for row in rows],
            'evidence_ids': sorted({key for row in rows for key in row['evidence_ids']}),
            'rationale':'The current declarative schema is the complete represented storage contract.',
            'identity_rationale':'The explicit storage.toml contract links the current db/schema.sql and history/migrations paths.',
            'additional_evidence':[binding_span],
            'coverage_binding':{'datastore':'db','migration_paths':['history/migrations/001.sql'],
                'field_ids':[row['entity_id'] for row in rows], 'evidence_ids':[binding_id]},
            'coverage_rationale':'Inspected all current schema declarations and represented each current column; historical columns are not used.',
            'investigation':[{'question':'Is current storage represented independently of historical DDL?', 'outcome':'All current definitions were checked against the map.'}]}]
        decision_path = self.repo / '.noru/.cache/gap-decisions.json'
        decision_path.write_text(json.dumps(decisions))
        self.command([sys.executable, str(SCRIPTS / 'investigate.py'), f'--repo={self.repo}', f'--gaps={decision_path}'])
        document=analysis_storage.load(self.repo/'.noru/.cache/privacy-datamap.analysis.json')
        self.assertEqual(review.structural_blockers(document,{},derived), [])
        self.command([sys.executable, str(SCRIPTS / 'validate_manifest.py'), str(self.repo/'.noru/privacy-datamap.yml'), '--quiet'])
        self.command([sys.executable, str(SCRIPTS / 'reconcile.py'), f'--repo={self.repo}', '--seal', '--quiet'])
        self.script('collect.mjs', 0, '--export')
        self.script('diff.mjs')
        partition = field_review.gap_partition({},derived,self.repo)
        self.assertEqual(len(partition['resolved_diagnostics']),1)
        self.assertEqual(partition['active_blockers'],[])
        migration.write_text('CREATE TABLE different_history (different_value TEXT);\n')
        self.assertTrue(field_review.accepted_certificate_errors(derived,self.repo), 'Deleted-cache acceptance must also reject changed diagnostic evidence')
        self.command([sys.executable, str(SCRIPTS / 'validate_manifest.py'), str(self.repo/'.noru/privacy-datamap.yml'), '--quiet'], 1)
        self.script('collect.mjs', 2, '--export')
        self.script('diff.mjs', 1)

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
