"""Recoverable local candidate application; applying never seals or approves a map."""
import copy
import hashlib
import json
import os
import pathlib
import tempfile
import analysis_storage


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
