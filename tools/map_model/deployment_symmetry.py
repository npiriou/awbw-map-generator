from __future__ import annotations
from itertools import combinations
from tools.map_features.categories import category_preference
from tools.map_model.editor_constraints import token_key
IDENTITY = (1, 0, 0, 1)
MATRICES = (IDENTITY, (-1, 0, 0, 1), (1, 0, 0, -1), (-1, 0, 0, -1), (0, 1, 1, 0), (0, -1, -1, 0), (0, -1, 1, 0), (0, 1, -1, 0))
_SPECIAL = ('Team Play', 'FFA Multiplay', 'Gimmick', 'Joke', 'Sprite', 'Toy-Box')

def destination(target, position, matrix):
    width, height = (target['width'], target['height'])
    x, y = (position % width, position // width)
    a, b, c, d = matrix
    minimum_x = min(0, a * (width - 1), b * (height - 1), a * (width - 1) + b * (height - 1))
    minimum_y = min(0, c * (width - 1), d * (height - 1), c * (width - 1) + d * (height - 1))
    return (c * x + d * y - minimum_y) * width + a * x + b * y - minimum_x

def unit_locks(target, editor_locks):
    blocked = {int(position) for position, fields in (editor_locks or {}).items() if any((key in fields for key in (2, 3, 4, '2', '3', '4', 'unit', 'unit_type', 'unit_owner', 'unit_hp')))}
    frozen = {index for index, unit in enumerate(target.get('units', [])) if unit.get('y', -1) * target['width'] + unit.get('x', -1) in blocked}
    return (blocked, frozen)

def _compose(left, right):
    a, b, c, d = left
    e, f, g, h = right
    return (a * e + b * g, a * f + b * h, c * e + d * g, c * f + d * h)

def _player_orbits(active, transforms):
    unseen, result = (set(active), [])
    while unseen:
        orbit = {min(unseen)}
        pending = list(orbit)
        while pending:
            owner = pending.pop()
            for transform in transforms:
                other = transform['owners'][owner]
                if other not in orbit:
                    orbit.add(other)
                    pending.append(other)
        unseen -= orbit
        result.append(sorted(orbit))
    return result

def build_symmetry_context(target, settings, *, group=None):
    active = sorted((f['slot'] for f in target.get('factions', []) if f.get('active')))
    result = {'applicable': True, 'reason': None, 'active_slots': active, 'player_orbits': [active], 'transforms': [], 'geometric': False, 'geometry_reason': None, 'property_owners_ignored_for_terrain': True, 'sprite_directions_ignored': True, 'balance_certified': False}
    if len(active) < 2 or len(set(active)) != len(active) or len(active) != target.get('players') or (settings.get('players', len(active)) != len(active)):
        return {**result, 'applicable': False, 'reason': 'requires_matching_active_player_roster'}
    if settings.get('tags', {}).get('1vX team play') is True or any((category_preference(settings, name) is True for name in _SPECIAL)):
        return {**result, 'applicable': False, 'reason': 'special_or_intentionally_unequal_deployment'}
    size = target['width'] * target['height']
    if size > 2500 or len(target.get('tiles', [])) != size or len(target.get('owners', [])) != size:
        return {**result, 'applicable': False, 'reason': 'unsupported_grid_size'}
    tags = settings.get('tags', {})
    if tags.get('Asymmetrical') is True:
        return {**result, 'geometry_reason': 'asymmetrical_map_requested'}
    if group is None:
        allowed = {IDENTITY}
        if tags.get('Flip symmetry') is True:
            allowed.update(((-1, 0, 0, 1), (1, 0, 0, -1)))
        if tags.get('diagonal symmetry') is True:
            allowed.update(((0, 1, 1, 0), (0, -1, -1, 0)))
        if tags.get('rotational symmetry') is True:
            allowed.update(((-1, 0, 0, -1), (0, -1, 1, 0), (0, 1, -1, 0)))
        group = tuple(allowed) if len(allowed) > 1 else MATRICES
    elif len(group) == 1:
        group = MATRICES
    keys = [token_key(token) for token in target['tiles']]
    if any((key[0] == 'unknown' for key in keys)):
        return {**result, 'geometry_reason': 'unknown_gameplay_terrain'}
    anchor_kind = 'hq' if any((token == 'property:hq' and owner in active for token, owner in zip(target['tiles'], target['owners']))) else 'lab'
    anchors = {owner: frozenset((i for i, (token, slot) in enumerate(zip(target['tiles'], target['owners'])) if token == 'property:' + anchor_kind and slot == owner)) for owner in active}
    if any((not positions for positions in anchors.values())):
        return {**result, 'geometry_reason': 'missing_player_hq_or_lab_anchor'}
    anchor_owners = {positions: owner for owner, positions in anchors.items()}
    available = {}
    for matrix in group:
        matrix = tuple(matrix)
        if matrix not in MATRICES or (matrix[1] and target['width'] != target['height']):
            continue
        positions = [destination(target, i, matrix) for i in range(size)]
        if any((keys[i] != keys[j] for i, j in enumerate(positions))):
            continue
        owners = {owner: anchor_owners.get(frozenset((positions[i] for i in starts))) for owner, starts in anchors.items()}
        if set(owners.values()) != set(active):
            continue
        available[matrix] = {'matrix': matrix, 'owners': owners}
    if IDENTITY not in available:
        return {**result, 'geometry_reason': 'no_verified_player_symmetry'}
    candidates = []
    others = [m for m in available if m != IDENTITY]
    for count in range(1, len(others) + 1):
        for subset in combinations(others, count):
            matrices = {IDENTITY, *subset}
            if any((_compose(a, b) not in matrices for a in matrices for b in matrices)):
                continue
            transforms = [available[m] for m in sorted(matrices)]
            orbits = _player_orbits(active, transforms)
            moved = sum((len(orbit) for orbit in orbits if len(orbit) > 1))
            if not moved:
                continue
            fixed = sum((t['owners'][owner] == owner for t in transforms if t['matrix'] != IDENTITY for owner in active))
            candidates.append(((-moved, len(orbits), fixed, len(transforms), tuple(sorted(matrices))), transforms, orbits))
    if not candidates:
        return {**result, 'geometry_reason': 'no_terrain_transform_exchanges_player_anchors'}
    _, transforms, orbits = min(candidates, key=lambda candidate: candidate[0])
    return {**result, 'geometric': True, 'transforms': transforms, 'player_orbits': orbits, 'anchor_kind': anchor_kind, 'geometry_reason': None if all((len(orbit) > 1 for orbit in orbits)) else 'some_players_have_no_geometric_counterpart'}

def validate_deployment_request(settings):
    from tools.map_features.common import movement_reference
    players = settings.get('players')
    tags = settings.get('tags', {})
    counts = settings.get('predeployed_counts', {})
    buildings = settings.get('building_counts', {})
    hq, labs = (buildings.get('hq', {}), buildings.get('lab', {}))
    owned_labs = labs.get('pre_owned')
    if owned_labs is None and type(labs.get('total')) is int and (type(labs.get('neutral')) is int):
        owned_labs = labs['total'] - labs['neutral']
    if (hq.get('total') == 0 or hq.get('pre_owned') == 0) and type(players) is int and (type(owned_labs) is int) and (owned_labs < players):
        return [f'Lab-only maps need at least one pre-owned Lab per player (requested {owned_labs} for {players} players).']
    if type(players) is not int or players < 2 or tags.get('1vX team play') is True or any((category_preference(settings, name) is True for name in _SPECIAL)):
        return []
    errors = []
    known = movement_reference()['units']
    if tags.get('immobile pre-deploy') is True and known.keys() <= counts.keys() and all((type(counts[kind]) is int and 0 <= counts[kind] < players for kind in known)):
        errors.append(f'Immobile deployments need corresponding units on the same kind of building for all {players} players. Request at least {players} units of one type, or set Immobile to No/Any. Unit counts are totals for the whole map.')
    if any((tags.get(name) is True for name in ('rotational symmetry', 'Flip symmetry', 'diagonal symmetry'))):
        for kind, label in (('black_boat', 'Black Boat'), ('lander', 'Lander'), ('transport_copter', 'T-Copter')):
            count = counts.get(kind)
            if type(count) is int and count > 0 and count % players:
                errors.append(f'{label}: symmetric deployments for {players} players need a total count divisible by {players} (requested {count}). Choose a compatible total or leave the count free.')
    return errors
