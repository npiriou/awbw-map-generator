from collections import Counter
from copy import deepcopy
from tools.map_features.symmetry_team import _symmetry_key

def token_key(token):
    if token.startswith('property:'):
        return ('property', token.split(':', 1)[1])
    return _symmetry_key(int(token.split(':', 1)[1]))

def complete_editor_map(locks, width, height, vocabulary, players):
    size = width * height
    if any((not all((column in locks.get(index, {}) for column in range(5))) for index in range(size))):
        return None
    from tools.map_codec import decode_target
    units = [{'type': vocabulary['unit_types'][locks[index][2] - 1], 'owner': locks[index][3], 'hp': locks[index][4] / 10, 'x': index % width, 'y': index // width} for index in range(size) if locks[index][2]]
    owners = [locks[index][1] for index in range(size)]
    largest = max([players, *owners, *(unit['owner'] for unit in units)])
    target = {'width': width, 'height': height, 'players': players, 'tiles': [vocabulary['tile_tokens'][locks[index][0]] for index in range(size)], 'owners': owners, 'units': units, 'factions': [{'slot': slot, 'active': slot <= players} for slot in range(1, largest + 1)]}
    return decode_target(target)

def certified_editor_exclusions(locks, width, height, vocabulary, players):
    from tools.map_features.access_counts import analyze_access_counts
    payload = complete_editor_map(locks, width, height, vocabulary, players)
    if payload is None:
        return Counter()
    audit = analyze_access_counts(payload)
    return Counter({kind: counts['neutral_unreachable'] for kind, counts in audit['building_counts'].items() if counts['neutral_unreachable']})

def extend_editor_locks(locks, width, height, group, vocabulary, settings):
    from tools.map_model.sample import _orbits, _building_token, _quota_orbits
    from tools.map_features.categories import forbidden_tile_tokens
    result = deepcopy(dict(locks or {}))
    tokens = vocabulary['tile_tokens']
    forbidden = forbidden_tile_tokens(settings)
    forbidden_keys = {token_key(token) for token in forbidden}
    orbits = list(_orbits(width, height, group))
    fixed = {}
    free = []
    for orbit in orbits:
        known = [tokens[result[index][0]] for index in orbit if 0 in result.get(index, {})]
        if not known:
            free.append(orbit)
            continue
        keys = {token_key(token) for token in known}
        if len(keys) > 1:
            raise ValueError('Locked terrain cells conflict with the requested symmetry. Unlock or change those cells.')
        token = known[0]
        if token_key(token) in forbidden_keys:
            raise ValueError('A locked terrain cell conflicts with a disabled terrain category. Unlock it or change the setting.')
        for index in orbit:
            fields = result.setdefault(index, {})
            fields.setdefault(0, tokens.index(token))
            if not token.startswith('property:'):
                fields.setdefault(1, 0)
            fixed[index] = tokens[fields[0]]
    fixed_units = Counter((vocabulary['unit_types'][fields[2] - 1] for fields in result.values() if fields.get(2, 0)))
    unit_demands = settings.get('predeployed_counts', {})
    for kind, total in unit_demands.items():
        if fixed_units[kind] > total:
            raise ValueError(f'Locked {kind} units already number {fixed_units[kind]}; requested total is {total}.')
    required = sum((max(0, total - fixed_units[kind]) for kind, total in unit_demands.items()))
    available = sum((2 not in result.get(index, {}) for index in range(width * height)))
    if required > available:
        raise ValueError('Requested units cannot fit the unlocked unit cells. Unlock more unit cells or reduce the counts.')
    if set(vocabulary['unit_types']).issubset(unit_demands) and (not required):
        for index in range(width * height):
            result.setdefault(index, {}).setdefault(2, 0)
            if result[index][2] == 0:
                result[index].update({3: 0, 4: 0})
    fixed_counts = Counter(fixed.values())
    demands = {}
    buildings = settings.get('building_counts', {})
    lab_only = buildings.get('hq', {}).get('total') == 0 or buildings.get('hq', {}).get('pre_owned') == 0
    anchor_kind = 'lab' if lab_only else 'hq'
    for kind, counts in buildings.items():
        total = counts.get('total')
        if total is None and 'pre_owned' in counts and ('neutral' in counts):
            total = counts['pre_owned'] + counts['neutral']
        if total is not None:
            demands[kind] = max(0, total - fixed_counts[_building_token(kind)])
        owned = sum((fixed.get(index) == _building_token(kind) and fields.get(1, 0) > 0 for index, fields in result.items() if 1 in fields))
        maximum_owned = counts.get('pre_owned', counts.get('total'))
        if maximum_owned is not None and owned > maximum_owned:
            raise ValueError(f'Locked {kind} buildings already include {owned} pre-owned; the requested limit is {maximum_owned}.')
    if lab_only:
        if fixed_counts['property:hq']:
            raise ValueError('Locked HQs conflict with a zero-HQ or lab-only game setting.')
        demands['hq'] = 0
    players = settings.get('players', 1)
    payload = complete_editor_map(result, width, height, vocabulary, players) if 'players' in settings else None
    if payload is not None:
        from tools.map_constraints import check_map
        report = check_map(payload, settings)
        if report['violations']:
            issues = '; '.join((f"{issue['path']}: requested {issue.get('expected')}, observed {issue.get('observed')}" for issue in report['violations']))
            raise ValueError(f'Locked editor cells and their symmetry counterparts fully determine the map but conflict with settings: {issues}. Unlock cells or change the settings.')
    excluded = certified_editor_exclusions(result, width, height, vocabulary, players) if 'players' in settings else Counter()
    for kind, counts in buildings.items():
        if kind in demands:
            total = counts.get('total', counts.get('pre_owned', 0) + counts.get('neutral', 0))
            demands[kind] = max(0, total - fixed_counts[_building_token(kind)] + excluded[kind])
    anchors = _building_token(anchor_kind)
    if anchors not in tokens:
        raise ValueError(f'Checkpoint cannot represent the required {anchor_kind} anchors.')
    demands[anchor_kind] = max(demands.get(anchor_kind, 0), players - fixed_counts[anchors])
    fixed_anchor_owners = [fields[1] for index, fields in result.items() if fixed.get(index) == anchors and 1 in fields]
    available_anchors = fixed_counts[anchors] - len(fixed_anchor_owners) + demands[anchor_kind]
    if 'players' in settings and len(set(range(1, players + 1)) - set(fixed_anchor_owners)) > available_anchors:
        raise ValueError('Locked HQ/lab ownership leaves no anchor for every player. Unlock an anchor or change its owner.')
    allocated = _quota_orbits(free, demands, lambda index, token: 0.0, anchor_kind)
    if any((len(allocated[kind]) != count for kind, count in demands.items())):
        raise ValueError('Building counts cannot fit the unlocked cells while preserving the requested symmetry. Unlock more cells or change the counts.')
    return result

def project_editor_terrain(tiles, locks, width, height, group, vocabulary, scores, forbidden):
    from tools.map_model.sample import _orbits, project_symmetry
    free_input = list(tiles)
    fixed = {}
    for orbit in _orbits(width, height, group):
        known = [index for index in orbit if 0 in (locks or {}).get(index, {})]
        if known:
            token = vocabulary['tile_tokens'][locks[known[0]][0]]
            fixed.update({index: token for index in orbit})
            for index in orbit:
                free_input[index] = 'tile:1'
    projected = project_symmetry(free_input, width, height, group, vocabulary, scores, forbidden)
    for index, token in fixed.items():
        projected[index] = vocabulary['tile_tokens'][locks[index][0]] if 0 in locks.get(index, {}) else token
    return projected
