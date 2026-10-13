"""Evidence-backed structural enrichment; never infers privacy categories."""
import copy

TYPE_KINDS = {'scalar', 'array', 'object', 'record', 'union', 'unknown', 'unknown_object', 'literal'}
COMPOSITE_KINDS = {'array', 'object', 'record', 'union', 'unknown_object'}
DECISION_KEYS = {'categories', 'data_categories', 'personal_data', 'status', 'decision_binding'}


def validate_type(value, depth=0):
    if depth > 32 or not isinstance(value, dict) or value.get('kind') not in TYPE_KINDS:
        raise ValueError('Every shape/member needs an explicit supported type.')
    if set(value) - {'kind', 'name', 'open', 'optional', 'items', 'variants', 'additional_types', 'value'}:
        raise ValueError('Shape types contain unsupported properties; declare members in the member list and classify them separately.')
    if value['kind'] in COMPOSITE_KINDS and not isinstance(value.get('open'), bool):
        raise ValueError('Composite shape observations must explicitly state boolean openness.')
    if 'open' in value and not isinstance(value['open'], bool):
        raise ValueError('Shape openness must be a boolean.')
    if 'name' in value and not isinstance(value['name'], str):
        raise ValueError('Shape type names must be strings.')
    for key in ('items', 'variants', 'additional_types'):
        if key in value:
            if not isinstance(value[key], list):
                raise ValueError('Nested shape types must be lists.')
            for child in value[key]:
                validate_type(child, depth + 1)


def normalize(proposal, review, repo):
    import field_review as fr
    def node(value, inherited=None, depth=0):
        if depth > 32 or not isinstance(value, dict):
            raise ValueError('Shape observations must be bounded object trees.')
        if DECISION_KEYS & set(value):
            raise ValueError('Shape evidence cannot supply classification decisions.')
        typ = value.get('type')
        validate_type(typ)
        refs = value.get('refs', inherited or [])
        if not isinstance(refs, list) or not refs:
            raise ValueError('Every shape/member needs current source references.')
        spans = [fr.span_from_ref(repo, ref, review.get('migration_exclusions')) for ref in refs]
        if not all(spans):
            raise ValueError('Shape references must identify valid current source lines.')
        for span in spans:
            errors = fr.evidence_errors(span, repo, review.get('migration_exclusions'))
            if errors:
                raise ValueError('; '.join(errors))
        members = value.get('members', [])
        if not isinstance(members, list):
            raise ValueError('Shape members must be a list.')
        names = [member.get('name') for member in members if isinstance(member, dict)]
        if len(names) != len(members) or any(not isinstance(name, str) or not name or '.' in name or '/' in name for name in names) or len(names) != len(set(names)):
            raise ValueError('Shape members need unique single-component names.')
        result = {'type': copy.deepcopy(typ), 'evidence': spans,
                  'members': [dict(node(member, refs, depth + 1), name=member['name']) for member in members]}
        if 'shape' in value:
            if not isinstance(value['shape'], str):
                raise ValueError('Shape descriptions must be strings.')
            result['shape'] = value['shape']
        if members and typ.get('kind') not in COMPOSITE_KINDS:
            raise ValueError('Scalar shape observations cannot contain members.')
        return result
    if not proposal.get('rationale') or not isinstance(proposal.get('rationale'), str):
        raise ValueError('Explain the investigated source meaning of each shape refinement.')
    return {'entity_id': proposal['entity_id'], 'base_observation_digest': proposal['observation_digest'],
            'rationale': proposal['rationale'], 'structure': node(proposal),
            'binding': copy.deepcopy(review['binding'])}


def current(observations, binding):
    if not isinstance(observations, dict):
        raise ValueError('Shape observations must be an object.')
    return {key: value for key, value in observations.items() if isinstance(value, dict) and value.get('binding') == binding}


def combine(existing, patch, observation=None):
    """Keep persisted observations separate from transient extraction annotations."""
    out = copy.deepcopy(existing)
    original = out.get('type')
    if out.get('members') and patch['type']['kind'] not in COMPOSITE_KINDS:
        raise ValueError('Shape enrichment cannot remove observed members.')
    out['type'] = copy.deepcopy(patch['type'])
    out['evidence'] = out.get('evidence', []) + copy.deepcopy(patch['evidence'])
    if 'shape' in patch:
        out['shape'] = patch['shape']
    members = {member['name']: copy.deepcopy(member) for member in out.get('members', [])}
    for child in patch.get('members', []):
        members[child['name']] = combine(members.get(child['name'], {}), child, observation)
        members[child['name']]['name'] = child['name']
    out['members'] = list(members.values())
    if observation is not None:
        if out['members']:
            out['type']['members'] = [{'name': member['name'], 'optional': member.get('optional', False)} for member in out['members']]
        out['_investigated_shape'] = True
        out['_shape_observation'] = observation
        out['_original_type'] = original
    return out


def merge(extracted, observation):
    """Add known members without losing compiler members or unresolved writer facts."""
    return combine(extracted, observation['structure'], observation)


def validate(observations, contract, repo):
    import field_review as fr
    errors = []
    if not isinstance(observations, dict):
        return ['Shape observations must be an object.']
    row_ids = {row['entity_id'] for row in fr.all_rows(contract.get('fields', []))}
    for identity, observation in observations.items():
        try:
            if not isinstance(observation, dict) or observation.get('entity_id') != identity or observation.get('binding') != contract.get('binding'):
                raise ValueError('Stale or inconsistent shape evidence: ' + str(identity))
            if not isinstance(observation.get('rationale'), str) or not observation['rationale'].strip() or not observation.get('base_observation_digest'):
                raise ValueError('Shape evidence requires its rationale and base observation.')
            def visit(node, subject, depth=0):
                if subject not in row_ids:
                    raise ValueError('Shape member is missing from classification coverage: ' + str(subject))
                if depth > 32 or not isinstance(node, dict) or set(node) - {'name', 'type', 'evidence', 'members', 'shape'}:
                    raise ValueError('Persisted shape evidence must contain only bounded structural observations.')
                validate_type(node.get('type'))
                spans = node.get('evidence')
                if not isinstance(spans, list) or not spans:
                    raise ValueError('Shape evidence requires current source spans.')
                for span in spans:
                    errors.extend(fr.evidence_errors(span, repo, contract.get('migration_exclusions')))
                members = node.get('members')
                if not isinstance(members, list):
                    raise ValueError('Shape members must be a list.')
                names = [member.get('name') for member in members if isinstance(member, dict)]
                if len(names) != len(members) or any(not isinstance(name, str) or not name or '.' in name or '/' in name for name in names) or len(names) != len(set(names)):
                    raise ValueError('Shape members need unique single-component names.')
                if members and node['type']['kind'] not in COMPOSITE_KINDS:
                    raise ValueError('Scalar shape observations cannot contain members.')
                for member in members:
                    visit(member, subject + '.' + member['name'], depth + 1)
            visit(observation.get('structure'), identity)
        except (ValueError, TypeError, KeyError) as error:
            errors.append(str(error))
    return errors


def apply(document, proposals, repo):
    import field_review as fr
    derived = fr.derived_for(document, repo)
    errors = fr.validate_contract(document, derived, repo, require_complete=False)
    if errors:
        return errors
    if not isinstance(proposals, list) or any(not isinstance(item, dict) for item in proposals):
        return ['Shape observations must be a list of objects.']
    updated = copy.deepcopy(document)
    contract = updated['classification_review']
    rows = {row['entity_id']: row for row in fr.all_rows(contract['fields'])}
    observations = copy.deepcopy(contract.get('shape_observations', {}))
    seen = set()
    try:
        for proposal in proposals:
            identity = proposal.get('entity_id')
            if identity not in rows or identity in seen:
                raise ValueError('Unknown or duplicate shape target: ' + str(identity))
            seen.add(identity)
            row = rows[identity]
            if row['observation_digest'] != proposal.get('observation_digest'):
                raise ValueError('Shape evidence is stale: ' + identity)
            observation = normalize(proposal, contract, repo)
            if row.get('nested_members') and observation['structure']['type']['kind'] not in COMPOSITE_KINDS:
                raise ValueError('Shape enrichment cannot remove observed members: ' + identity)
            # Merge only structural data here. Extraction annotations reference the
            # observation itself and must never be persisted inside its structure.
            if identity in observations:
                observation['structure'] = combine(observations[identity]['structure'], observation['structure'])
            observations[identity] = observation
        contract['shape_observations'] = observations
        fr.build_contract(updated, repo)
        errors = fr.validate_contract(updated, derived, repo, require_complete=False)
        if errors:
            return errors
    except (ValueError, TypeError, KeyError) as error:
        return [str(error)]
    document['classification_review'] = updated['classification_review']
    return []
