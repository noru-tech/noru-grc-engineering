"""Recoverable local candidate application; applying never seals or approves a map."""
import copy
import json
import os
import pathlib
import tempfile
import subprocess
from runtime_context import NODE
import analysis_storage


def refresh_digest(repo, candidate):
    """Observe the exact proposed inputs without installing or accepting them first."""
    import dataset_inputs
    import relationships
    inputs = dataset_inputs.extract(repo, document=candidate)
    graph = candidate.get('relationship_graph')
    mapping = {'bindings': {}, 'flows': {}, 'questions': [], 'errors': []}
    if graph is not None:
        errors = relationships.validate(graph, candidate.get('evidence_dependencies', []))
        if errors:
            raise ValueError('Cannot apply invalid relationships: ' + '; '.join(errors))
        mapping = {**relationships.project(graph), 'graph': graph, 'errors': []}
    script = """import {readFileSync} from 'node:fs';
const {collectFacts, digestOf} = await import(process.argv[1]);
const input = JSON.parse(readFileSync(0, 'utf8'));
console.log(digestOf(collectFacts(process.argv[2], input)));"""
    result = subprocess.run([NODE, '--input-type=module', '-e', script,
        pathlib.Path(__file__).with_name('collect.mjs').as_uri(), str(repo)],
        input=json.dumps({'inputDefinitions': inputs, 'relationshipDefinitions': mapping}), capture_output=True, text=True)
    if result.returncode:
        raise ValueError('Cannot collect proposed manifest: ' + result.stderr.strip())
    candidate.setdefault('source', {})['derived_digest'] = result.stdout.strip()


def atomic_text(path, text):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
            temporary = stream.name
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary): os.unlink(temporary)


def finish(repo, journal):
    path = repo / '.noru/.cache/privacy-datamap.apply.json'
    targets = [repo / '.noru/privacy-datamap.yml', repo / '.noru/.cache/privacy-datamap.analysis.json']
    # Check both before writing either; recovery must not overwrite intervening edits.
    for target, record in zip(targets, journal['files']):
        current = target.read_text() if target.exists() else None
        if current not in (record['before'], record['after']):
            raise ValueError('Candidate application interrupted by another edit; retain the journal and reconcile the edited file.')
    for target, record in zip(targets, journal['files']):
        atomic_text(target, record['after'])
    path.unlink()


def apply(repo, candidate, document, render):
    repo = pathlib.Path(repo)
    candidate = copy.deepcopy(candidate)
    refresh_digest(repo, candidate)
    journal_path = repo / '.noru/.cache/privacy-datamap.apply.json'
    updated = copy.deepcopy(document)
    updated['applied_proposals'] = {key: updated.pop(key) for key in ('structural_proposal', 'relationship_proposal') if key in updated}
    updated['collection_mode'] = 'manifest'
    updated.pop('preview', None)
    targets = [(repo / '.noru/privacy-datamap.yml', render(candidate)),
               (repo / '.noru/.cache/privacy-datamap.analysis.json', json.dumps(analysis_storage.encode(updated), sort_keys=True) + '\n')]
    journal = {'files': [{'before': target.read_text() if target.exists() else None, 'after': content} for target, content in targets]}
    atomic_text(journal_path, json.dumps(journal))
    finish(repo, journal)


def recover(repo):
    path = repo / '.noru/.cache/privacy-datamap.apply.json'
    if path.exists():
        finish(repo, json.loads(path.read_text()))
        return True
    return False
