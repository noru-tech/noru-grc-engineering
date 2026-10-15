"""Shared declaration identities for projection, review and semantic comparison."""


def expanded_names(activity):
    uses = activity.get('proposed_data_uses') or ['']
    purpose = activity.get('purpose', '')
    return [(f'{purpose} ({use})' if len(uses) > 1 else purpose, use) for use in uses]


def mechanical_renames(previous, current):
    """Recognize only the exact per-use expansion with all other dimensions unchanged."""
    renames = []
    for key, before in (previous or {}).get('activities', {}).items():
        if key in current.get('activities', {}):
            continue
        uses = before.get('purposes', [])
        targets = [key + ' (' + use + ')' for use in uses if isinstance(use, str)]
        if targets and len(targets) == len(uses) and all(
            current.get('activities', {}).get(target) == {**before, 'purposes': [use]}
            for target, use in zip(targets, uses)
        ):
            renames.append({'previous': key, 'current': targets})
    return renames
