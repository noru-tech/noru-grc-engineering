"""Shared extraction dispositions. Observations are never removed by a decision."""
import copy
import hashlib
import json
import pathlib
import uncertainty

VERSION = 1
COVERAGE_KEYS = ('unparsed_candidates', 'migration_gaps', 'schema_conflicts')
MUTABLE = {'status', 'resolution', 'resolution_binding', 'resolution_history', 'reopened_reason', 'blocker_reasons'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def agent_blockers(partition):
    """Processing meaning questions remain publication blockers while awaiting human review."""
    return [gap for gap in partition['active_blockers'] if gap.get('kind') != 'processing_uncertainty']


def identity(item):
    return item.get('diagnostic_id') or item.get('gap_id') or 'gap:' + digest({
        'kind': item.get('kind', item.get('format')), 'scope': item.get('scope', item.get('subject', item.get('ref', sorted({span.get('path', '') for span in item.get('evidence', [])})))),
        'rule': item.get('code', item.get('message', item.get('reason')))})


def observations(derived, document=None):
    result = []
    for key in COVERAGE_KEYS:
        for raw in derived.get('coverage', {}).get(key, []):
            item = copy.deepcopy(raw)
            item.update(gap_id=identity(raw), kind=raw.get('kind', raw.get('format', key)),
                        message=raw.get('message', raw.get('reason', 'Structural coverage requires investigation.')),
                        coverage_key=key, collector_observation_digest=digest(raw), status='unresolved')
            result.append(item)
    for question in (derived.get('relationship_mapping') or {}).get('questions', []):
        raw = {**copy.deepcopy(question), 'kind': 'relationship_uncertainty',
               'scope': {'connection': question.get('id')}, 'code': 'RELATIONSHIP_QUESTION',
               'reason': question.get('unresolved_question', 'Connection destination is unknown.')}
        raw.update(gap_id=identity(raw), message=raw['reason'], coverage_key='unparsed_candidates',
                   collector_observation_digest=digest(raw), status='unresolved')
        result.append(raw)
    for system in (document or {}).get('system_proposals', []):
        for item in [system] + system.get('processing_activities', []):
            question = item.get('business_context_question') or item.get('unresolved_question')
            if not question:
                continue
            raw = {'kind': 'processing_uncertainty', 'code': 'PROCESSING_QUESTION',
                   'scope': {'system': system.get('entity_id'), 'activity': item.get('activity_id')},
                   'reason': question, 'ref': next(iter(item.get('refs', [])), ''),
                   'details': copy.deepcopy(item), 'resolution_requirement': item.get('resolution_needed')}
            raw.update(gap_id=identity(raw), message=question, coverage_key='unparsed_candidates',
                       collector_observation_digest=digest(raw), status='unresolved')
            result.append(raw)
    for finding in (document or {}).get('store_investigation', {}).get('findings', []):
        if finding.get('status') != 'gap':
            continue
        raw = copy.deepcopy(finding)
        raw.update(kind='enrichment_coverage', scope={'store': finding.get('store')},
                   code='ENRICHMENT_COVERAGE', ref=next(iter(finding.get('refs', [])), ''),
                   reason=finding.get('unresolved_question') or finding.get('rationale') or 'Store structure requires investigation.')
        raw.update(gap_id=identity(raw), message=raw['reason'], coverage_key='enrichment_gaps',
                   collector_observation_digest=digest(raw), status='unresolved',
                   resolution_owner='agent', resolution_requirement=finding.get('resolution_needed'))
        result.append(raw)
    return result


def occurrence_ids(gap):
    occurrences = gap.get('occurrences') or [{k:v for k,v in gap.items() if k not in MUTABLE | {'evidence_ids', 'coverage_key'}}]
    return sorted(set('occurrence:' + digest(item) for item in occurrences))


def binding(gap, contract):
    resolution = gap.get('resolution', {})
    rows = {row['entity_id']: row for row in walk(contract.get('fields', []))}
    return digest({'observation': {k:v for k,v in gap.items() if k not in MUTABLE},
                   'resolution': resolution, 'structure': contract.get('binding', {}).get('structural_digest'),
                   'evidence': {key:contract.get('evidence', {}).get(key) for key in resolution.get('evidence_ids', [])},
                   'targets': {key:rows.get(key, {}).get('observation_digest') for key in resolution.get('field_ids', [])},
                   'runtime_targets': {key:contract.get('runtime_definitions', {}).get(key, {}).get('observation_digest') for key in resolution.get('system_ids', [])}})


def walk(rows):
    for row in rows:
        yield row
        yield from walk(row.get('nested_members', []))


def observation_errors(gap, repo):
    errors = []
    if repo is not None:
        root = pathlib.Path(repo).resolve()
        for name, expected in gap.get('observation_files', {}).items():
            path = root / name
            if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                errors.append('diagnostic source changed: ' + name)
    return errors


def resolution_errors(gap, contract, repo=None):
    import field_review
    decision = gap.get('resolution', {})
    errors = []
    if gap.get('status') != 'resolved' or decision.get('outcome') not in {'modeled', 'not_applicable', 'accepted_uncertainty', 'accepted_certificate'}:
        return ['unresolved']
    errors.extend(observation_errors(gap, repo))
    if gap.get('resolution_binding') != binding(gap, contract):
        errors.append('resolution is unbound or its evidence/structure changed')
    if decision.get('outcome') == 'accepted_certificate':
        if not contract.get('accepted_certificate') or decision.get('certificate_digest') != contract.get('accepted_certificate'):
            errors.append('missing sealed certificate')
        return errors
    provenance = decision.get('provenance', {})
    if not isinstance(provenance, dict) or not provenance.get('method') or provenance.get('evidence') != {key:contract.get('evidence', {}).get(key) for key in decision.get('evidence_ids', [])}:
        errors.append('resolution provenance must retain the cited evidence')
    if not decision.get('rationale') or not decision.get('investigation') or not decision.get('evidence_ids'):
        errors.append('rationale, investigation and evidence are required')
    if decision.get('covered_occurrence_ids') != occurrence_ids(gap):
        errors.append('resolution must explicitly cover every observed occurrence')
    for key in decision.get('evidence_ids', []):
        span = contract.get('evidence', {}).get(key)
        if not span:
            errors.append('missing resolution evidence')
        elif repo is not None:
            errors.extend(field_review.evidence_errors(span, repo, contract.get('migration_exclusions')))
    if decision.get('outcome') == 'accepted_uncertainty':
        errors.extend(uncertainty.errors(decision.get('accepted_uncertainty'), gap['gap_id'], decision.get('evidence_ids', [])))
        targets = decision.get('export_targets')
        known = {'dataset:' + row['entity_id'].split('/')[0] for row in walk(contract.get('fields', []))}
        known.update('system:' + key for key in contract.get('runtime_definitions', {}))
        if not isinstance(targets, list) or not targets or any(not isinstance(key, str) or key not in known for key in targets):
            errors.append('accepted uncertainty requires existing dataset/system export targets')
    if decision.get('outcome') == 'not_applicable'  and not str(decision.get('scope_rationale', '')).strip():
        errors.append('explain why the finding does not affect represented data')
    if decision.get('outcome') == 'modeled':
        rows = {row['entity_id']: row for row in walk(contract.get('fields', []))}
        targets = decision.get('field_ids', [])
        systems = decision.get('system_ids', [])
        runtimes = contract.get('runtime_definitions', {})
        if not (targets or systems) or any(key not in rows or not rows[key].get('evidence_ids') for key in targets):
            errors.append('modeled resolution requires evidenced structural definitions')
        for key in systems:
            runtime = runtimes.get(key, {})
            if not runtime.get('evidence_ids') or not set(runtime['evidence_ids']) <= set(decision.get('evidence_ids', [])):
                errors.append('modeled runtime requires a current definition and cited runtime evidence')
        if systems and not decision.get('runtime_rationale'):
            errors.append('explain how runtime definitions and flows cover the finding')
        if systems and not gap.get('required_system_ids') and gap.get('scope', {}).get('kind') not in {'runtime', 'provisioned_runtime'}:
            errors.append('runtime targets require an explicitly runtime-scoped finding')
        if not set(gap.get('required_system_ids', [])) <= set(systems):
            errors.append('modeled definitions omit required runtimes')
        for key in targets:
            row = rows.get(key, {})
            if not row.get('open_content') or (row.get('open_content_resolution') or {}).get('outcome') in field_review.CONTENT_OUTCOMES:
                continue
            shape = decision.get('open_structure', {}).get(key, {})
            observed = row.get('type', {})
            if (observed.get('kind') != 'record' or not observed.get('key_type') or not observed.get('value_type')
                    or shape.get('additional_entries') is not True or not shape.get('rationale')
                    or shape.get('key_type') != observed['key_type'] or shape.get('value_type') != observed['value_type']
                    or not shape.get('evidence_ids') or not set(shape['evidence_ids']) <= set(decision.get('evidence_ids', []))):
                errors.append('open structure requires an evidenced record contract with known key/value types; classification remains separate')
        scope = gap.get('scope', {})
        dataset = scope.get('datastore') or scope.get('dataset')
        if dataset and (not targets or any(key.split('/')[0] != dataset for key in targets)):
            errors.append('modeled definitions do not cover the affected datastore')
        required = gap.get('required_field_ids', [])
        if not set(required) <= set(targets):
            errors.append('modeled definitions omit required structure')
        if gap.get('coverage_key') == 'migration_gaps':
            if not decision.get('coverage_rationale') or not decision.get('identity_rationale'):
                errors.append('migration resolution requires separate datastore identity and schema completeness evidence')
            coverage = decision.get('coverage_binding', {})
            migration_path = gap.get('coverage', {}).get('migration') or gap.get('ref', '').rsplit(':', 1)[0]
            if (not coverage.get('datastore') or any(key.split('/')[0] != coverage['datastore'] for key in targets)
                    or migration_path not in coverage.get('migration_paths', [])
                    or set(coverage.get('field_ids', [])) != set(targets)
                    or not coverage.get('evidence_ids') or not set(coverage['evidence_ids']) <= set(decision.get('evidence_ids', []))):
                errors.append('migration resolution requires an evidenced binding of this migration, datastore and complete target definitions')
    return sorted(set(errors))


def calculate(document, derived, repo=None):
    """All gates consume this partition; a raw gap absent from review remains blocking."""
    import field_review
    contract = document.get('classification_review', {})
    observed = copy.deepcopy(contract.get('gaps', []))
    known = {identity(gap) for gap in observed}
    for item in observations(derived, document):
        if identity(item) not in known:
            observed.append(item)
        elif any(gap.get('collector_observation_digest') != item['collector_observation_digest'] for gap in observed if identity(gap)==identity(item)):
            observed.append(item)
    stale = bool(contract and (contract.get('binding', {}).get('structural_digest') != field_review.structure_digest(derived)))
    if repo is not None and contract:
        stale = stale or contract.get('binding', {}).get('source_inventory') != field_review.source_inventory(repo, contract.get('migration_exclusions'))
    resolved, active = [], []
    groups = {}
    for gap in observed:
        groups.setdefault(identity(gap), []).append(gap)
    for key, group in groups.items():
        distinct = {digest({k:v for k,v in gap.items() if k not in MUTABLE}) for gap in group}
        for gap in group:
            errors = resolution_errors(gap, contract, repo)
            current_systems = {system['fides_key']:system for system in derived.get('systems', [])}
            for target in gap.get('resolution', {}).get('system_ids', []):
                if contract.get('runtime_definitions', {}).get(target, {}).get('observation_digest') != digest(current_systems.get(target)):
                    errors.append('represented runtime or flows changed')
            if stale: errors.append('review source or structure is stale')
            if len(distinct) != 1: errors.append('conflicting observations share a diagnostic identity')
            if errors:
                active.append({**gap, 'blocker_reasons': sorted(set(errors))})
            else:
                resolved.append(gap)
    return {'version':VERSION, 'observed_diagnostics':observed, 'resolved_diagnostics':resolved, 'active_blockers':active}


def fallback_template():
    return {'source_review': '', 'evidence_ids': [], 'supplemental_attempt': '',
            'remaining_reason': 'evidence_unavailable', 'missing_fact': '',
            'required_evidence': '', 'decision_impact': '', 'next_evidence_action': ''}


def fallback_errors(record, cited):
    if not isinstance(record, dict):
        return ['fallback_review must be an investigation record']
    required = ('source_review', 'supplemental_attempt', 'missing_fact', 'required_evidence', 'decision_impact', 'next_evidence_action')
    errors = ['fallback_review: explain ' + name for name in required
              if not isinstance(record.get(name), str) or not record[name].strip()]
    ids = record.get('evidence_ids')
    if not isinstance(ids, list) or not ids or any(not isinstance(key, str) for key in ids) or not set(ids) <= set(cited):
        errors.append('fallback_review requires cited evidence of the investigation')
    if record.get('remaining_reason') != 'evidence_unavailable':
        errors.append('fallback_review must identify unresolved evidence and the next evidence-gathering action; implementation limitations are not investigation outcomes')
    return errors


def agent_task(gap, contract=None, repo=None):
    """An extractor diagnostic always enters agent work, including legacy unresolved records."""
    decision = gap.get('resolution', {})
    record = decision.get('fallback_review')
    import field_review
    contract = contract or {}
    current = (gap.get('blocker_reasons') == ['unresolved']
               and gap.get('resolution_binding') == binding(gap, contract)
               and all(key in contract.get('evidence', {}) and (repo is None or not field_review.evidence_errors(contract['evidence'][key], repo, contract.get('migration_exclusions'))) for key in decision.get('evidence_ids', [])))
    state = (record['remaining_reason'] if current and decision.get('outcome') == 'unresolved'
             and record and not fallback_errors(record, decision.get('evidence_ids', [])) else 'investigation_required')
    return {'resolution_owner': 'agent', 'investigation_state': state,
            'next_evidence_action': record['next_evidence_action'] if state == 'evidence_unavailable' else 'Inspect source, producers, consumers and available contracts; trace the missing evidence and record the next concrete action.',
            'investigation_steps': [
                'Read the cited source, direct producers/consumers, configuration and relevant available framework contracts; the collector limitation is not an absence-of-evidence conclusion.',
                'Use supported supplemental datasets, nested shape refinements, runtime definitions and relationship proposals to represent evidence the collector missed. Register dependencies and rebuild the candidate before resolving the diagnostic.',
                'Submit an evidence-bound modeled or not_applicable resolution through investigate.py --gaps. Keep privacy meaning and human acceptance separate.',
                'If still blocked, record fallback_review with inspected evidence, the supplemental attempt, the exact missing fact and map impact. Record the next evidence-gathering action. Never delegate investigation to the plugin or halt evidence gathering for development work.',
                'Continue independent queued investigation. Revisit a blocked attempt when its evidence changes; never clear gaps or acceptance flags to report completion.'
            ], 'fallback_review_template': fallback_template()}
