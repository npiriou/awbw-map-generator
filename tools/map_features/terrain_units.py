from __future__ import annotations
from collections import Counter
from .common import grid, movement_reference, neighbors, property_type, terrain_name, unit_type
TRANSPORT_TYPES = frozenset({'black_boat', 'lander', 'transport_copter'})
INTACT_SEAM_IDS = frozenset({113, 114})

def _result(matches, unresolved, **details):
    return {'status': 'matched' if matches else 'unresolved' if unresolved else 'not_matched', 'evidence': {'matches': matches, 'unresolved': unresolved, **details}}

def _can_enter(tile_id, movement, reference):
    terrain = terrain_name(tile_id)
    if terrain is None:
        return None
    if terrain == 'property':
        costs = reference['property_costs']['clear'].get(property_type(tile_id))
    else:
        costs = reference['costs']['clear'].get(terrain)
    if costs is None or movement not in costs:
        return None
    return costs[movement] is not None

def _unit_evidence(index, unit, kind):
    return {'inventory_index': index, 'unit_id': unit.get('Unit ID'), 'unit_type': kind, 'position': [unit.get('Unit X'), unit.get('Unit Y')], 'country_code': unit.get('Country Code')}

def analyze_terrain_units(payload):
    width, height, tiles = grid(payload)
    reference = movement_reference()
    seam_matches = [{'position': [index % width, index // width], 'terrain_id': tile} for index, tile in enumerate(tiles) if tile in INTACT_SEAM_IDS]
    unknown_terrain = [{'position': [index % width, index // width], 'terrain_id': tile, 'reason': 'unknown_terrain_id'} for index, tile in enumerate(tiles) if terrain_name(tile) is None]
    transport_matches, transport_unknown, transport_excluded = ([], [], [])
    immobile_matches, immobile_unknown = ([], [])
    counts = Counter()
    inventory = payload.get('Predeployed Units')
    if not isinstance(inventory, list):
        reason = 'missing_predeployed_inventory' if inventory is None else 'invalid_predeployed_inventory'
        transport_unknown.append({'reason': reason})
        immobile_unknown.append({'reason': reason})
        inventory = []
    for index, unit in enumerate(inventory):
        if not isinstance(unit, dict):
            unknown = {'inventory_index': index, 'reason': 'invalid_predeployed_unit'}
            transport_unknown.append(unknown)
            immobile_unknown.append(unknown)
            counts['unknown'] += 1
            continue
        kind = unit_type(unit.get('Unit ID'))
        evidence = _unit_evidence(index, unit, kind)
        if kind is None or kind not in reference['units']:
            unknown = {**evidence, 'reason': 'unknown_unit_id_or_movement'}
            transport_unknown.append(unknown)
            immobile_unknown.append(unknown)
            counts['unknown'] += 1
            continue
        x, y = (unit.get('Unit X'), unit.get('Unit Y'))
        if type(x) is not int or type(y) is not int or (not (0 <= x < width and 0 <= y < height)):
            unknown = {**evidence, 'reason': 'invalid_unit_position'}
            immobile_unknown.append(unknown)
            if kind in TRANSPORT_TYPES:
                transport_unknown.append(unknown)
            counts['unknown'] += 1
            continue
        movement = reference['units'][kind]['movement_type']
        adjacent = [{'position': [nx, ny], 'terrain_id': tiles[ny * width + nx], 'terrain': terrain_name(tiles[ny * width + nx]), 'passable': _can_enter(tiles[ny * width + nx], movement, reference)} for nx, ny in neighbors(x, y, width, height)]
        evidence = {**evidence, 'movement_type': movement, 'neighbors': adjacent}
        if any((site['passable'] is True for site in adjacent)):
            mobility = 'mobile'
        elif any((site['passable'] is None for site in adjacent)):
            mobility = 'unknown'
        elif terrain_name(tiles[y * width + x]) == 'teleport':
            mobility = 'unknown'
            evidence = {**evidence, 'reason': 'unknown_teleport_destination'}
        else:
            mobility = 'immobile'
        counts[mobility] += 1
        if mobility == 'immobile':
            immobile_matches.append({**evidence, 'reason': 'no_passable_adjacent_destination'})
        elif mobility == 'unknown':
            immobile_unknown.append({**evidence, 'reason': evidence.get('reason', 'unknown_adjacent_terrain')})
        if kind not in TRANSPORT_TYPES:
            continue
        if kind in {'black_boat', 'lander'}:
            qualifies = any((site['terrain'] in {'sea', 'shoal'} for site in adjacent))
            uncertain = any((site['terrain'] is None for site in adjacent))
            reason = 'adjacent_sea_or_beach' if qualifies else 'no_adjacent_sea_or_beach'
        else:
            qualifies, uncertain = (mobility == 'mobile', mobility == 'unknown')
            reason = 'mobile_transport_copter' if qualifies else 'immobile_transport_copter'
        if qualifies:
            transport_matches.append({**evidence, 'reason': reason})
        elif uncertain:
            transport_unknown.append({**evidence, 'reason': 'unknown_transport_mobility_or_neighbor'})
        else:
            transport_excluded.append({**evidence, 'reason': reason})
    return {'tags': {'pipe seams': _result(seam_matches, unknown_terrain, intact_seam_ids=sorted(INTACT_SEAM_IDS)), 'predeployed transports': _result(transport_matches, transport_unknown, excluded=transport_excluded, included_unit_types=sorted(TRANSPORT_TYPES), boat_rule='orthogonally adjacent Sea or Beach/Shoal', copter_rule='at least one passable adjacent destination'), 'immobile pre-deploy': _result(immobile_matches, immobile_unknown, rule='no passable orthogonally adjacent destination', ignores=['unit_occupancy', 'fuel', 'combat', 'property_ownership'])}, 'evidence': {'inventory_count': len(inventory), 'mobility_counts': {key: counts[key] for key in ('mobile', 'immobile', 'unknown')}, 'unknown_terrain_count': len(unknown_terrain)}}
