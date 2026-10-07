from copy import deepcopy
from tools.map_features.common import movement_reference, terrain_name
from tools.map_model.deployment_symmetry import build_symmetry_context, destination
from tools.map_model.editor_constraints import token_key
from tools.map_model.lab_ownership import _actions, _geometry_required, repair_lab_ownership

def _relocate_axis_hqs(target, settings, sites, editor_locks, group):
    active = sorted((f['slot'] for f in target['factions'] if f['active']))
    if len(active) != 2 or len(sites) != 2:
        return (target, [])
    blocked = {i for i, fields in (editor_locks or {}).items() if fields}
    blocked.update((u['y'] * target['width'] + u['x'] for u in target['units']))
    if blocked.intersection(sites):
        return (target, [])
    actions, _ = _actions(target, settings, active, True, group)
    choices = []
    costs = movement_reference()['costs']['clear']
    width = target['width']

    def distance(a, b):
        return abs(a % width - b % width) + abs(a // width - b // width)
    for action in actions:
        if len(action) != 2:
            continue
        matrix = next((t['matrix'] for t in action if any((t['owners'][p] != p for p in active))))
        if any((destination(target, i, matrix) != i for i in sites)):
            continue
        for i, tile in enumerate(target['tiles']):
            j = destination(target, i, matrix)
            if i >= j or {i, j}.intersection(blocked) or (not tile.startswith('tile:')):
                continue
            if token_key(tile) != token_key(target['tiles'][j]):
                continue
            family = terrain_name(int(tile.split(':', 1)[1]))
            if costs.get(family, {}).get('foot') is None or family == 'teleport':
                continue
            for pair in ((i, j), (j, i)):
                movement = sum((distance(source, to) for source, to in zip(sites, pair)))
                choices.append(((movement, -distance(i, j), pair), pair))
    if not choices:
        return (target, [])
    _, pair = min(choices)
    result = deepcopy(target)
    owners = [target['owners'][i] for i in sites]
    if set(owners) != set(active):
        owners = active
    for source, to, owner in zip(sites, pair, owners):
        result['tiles'][source], result['owners'][source] = (target['tiles'][to], 0)
        result['tiles'][to], result['owners'][to] = ('property:hq', owner)
    if not build_symmetry_context(result, settings, group=group).get('geometric'):
        return (target, [])
    return (result, [{'kind': 'hq_symmetry_relocation', 'from_positions': sites, 'to_positions': list(pair), 'preserved_terrain_inventory': True, 'reason': 'owned_hqs_on_axis_cannot_exchange_players', 'balance_certified': False}])

def repair_hq_ownership(target, settings, *, editor_locks=None, group=None):
    result = deepcopy(target)
    sites = [i for i, token in enumerate(target['tiles']) if token == 'property:hq']
    context = build_symmetry_context(target, settings, group=group)
    if not sites or not _geometry_required(settings, group) or (not context.get('applicable')) or context.get('geometric'):
        return (result, [])
    placed, relocation = _relocate_axis_hqs(result, settings, sites, editor_locks, group)
    if relocation:
        return (placed, relocation)
    view = deepcopy(target)
    exchanged = {'property:hq': 'property:lab', 'property:lab': 'property:hq'}
    view['tiles'] = [exchanged.get(token, token) for token in target['tiles']]
    options = deepcopy(settings)
    buildings = options.setdefault('building_counts', {})
    buildings['lab'] = deepcopy(buildings.get('hq', {}))
    buildings['hq'] = {'total': 0}
    repaired, evidence = repair_lab_ownership(view, options, editor_locks=editor_locks, group=group)
    for position in sites:
        result['owners'][position] = repaired['owners'][position]
    for item in evidence:
        item['kind'] = item['kind'].replace('lab_ownership', 'hq_ownership')
        item['anchor_kind'] = 'hq'
    return (result, evidence)
