from __future__ import annotations
from collections import Counter
from heapq import heapify, heappop, heappush
from math import isfinite
from statistics import median
from .common import country_id, grid, movement_reference, neighbors, property_owner, property_type, terrain_name
SYMMETRY_TAGS = ('Flip symmetry', 'diagonal symmetry', 'rotational symmetry', 'Asymmetrical')
_VECTORS = ((0, -1), (1, 0), (0, 1), (-1, 0))
_VECTOR_BITS = {vector: 1 << i for i, vector in enumerate(_VECTORS)}
_NS, _EW = (5, 10)
_DIRECTION_MASKS = (10, 5, 15, 6, 12, 9, 3, 14, 13, 11, 7)
_ORIENTED = {**{4 + i: ('river', mask) for i, mask in enumerate(_DIRECTION_MASKS)}, **{15 + i: ('road', mask) for i, mask in enumerate(_DIRECTION_MASKS)}, 26: ('bridge', _EW), 27: ('bridge', _NS), 29: ('shoal', 4), 30: ('shoal', 1), 31: ('shoal', 8), 32: ('shoal', 2), 101: ('pipe', _NS), 102: ('pipe', _EW), 103: ('pipe', 3), 104: ('pipe', 6), 105: ('pipe', 12), 106: ('pipe', 9), 107: ('pipe', 1), 108: ('pipe', 2), 109: ('pipe', 4), 110: ('pipe', 8), 113: ('pipe_seam', _EW), 114: ('pipe_seam', _NS), 115: ('pipe_rubble', _EW), 116: ('pipe_rubble', _NS)}
_MATRICES = {'vertical': (-1, 0, 0, 1), 'horizontal': (1, 0, 0, -1), 'main_diagonal': (0, 1, 1, 0), 'anti_diagonal': (0, -1, -1, 0), 'rotation_90': (0, -1, 1, 0), 'rotation_180': (-1, 0, 0, -1), 'rotation_270': (0, 1, -1, 0)}
_DIRECTED_FAMILIES = frozenset((key[0] for key in _ORIENTED.values()))

def _key(tile):
    kind = property_type(tile)
    if kind is not None:
        return ('property', kind)
    if tile in _ORIENTED:
        return _ORIENTED[tile]
    name = terrain_name(tile)
    return (name, None) if name is not None else ('unknown', tile)

def _transform_key(key, matrix):
    family, mask = key
    if family not in _DIRECTED_FAMILIES:
        return key
    a, b, c, d = matrix
    transformed = 0
    for i, (dx, dy) in enumerate(_VECTORS):
        if mask & 1 << i:
            transformed |= _VECTOR_BITS[a * dx + b * dy, c * dx + d * dy]
    return (family, transformed)

def _symmetry_key(tile):
    kind = property_type(tile)
    if kind is not None:
        return ('property', kind)
    name = terrain_name(tile)
    return (name, None) if name is not None else ('unknown', tile)

def _destination(x, y, width, height, name):
    if name == 'vertical':
        return (width - 1 - x, y)
    if name == 'horizontal':
        return (x, height - 1 - y)
    if name == 'main_diagonal':
        return (y, x)
    if name == 'anti_diagonal':
        return (width - 1 - y, height - 1 - x)
    if name == 'rotation_90':
        return (width - 1 - y, x)
    if name == 'rotation_270':
        return (y, height - 1 - x)
    return (width - 1 - x, height - 1 - y)

def analyze_symmetry(payload):
    width, height, tiles = grid(payload)
    keys = tuple((_symmetry_key(tile) for tile in tiles))
    unknown = sorted({tile for tile, key in zip(tiles, keys) if key[0] == 'unknown'})
    names = ['vertical', 'horizontal', 'rotation_180']
    if width == height:
        names.extend(('main_diagonal', 'anti_diagonal', 'rotation_90', 'rotation_270'))
    transforms = {}
    for name in names:
        mismatches, samples = (0, [])
        for index, key in enumerate(keys):
            x, y = (index % width, index // width)
            nx, ny = _destination(x, y, width, height, name)
            other = ny * width + nx
            if key != keys[other]:
                mismatches += 1
                if len(samples) < 3:
                    samples.append({'position': {'x': x, 'y': y}, 'terrain_id': tiles[index], 'transformed_position': {'x': nx, 'y': ny}, 'destination_terrain_id': tiles[other]})
        transforms[name] = {'matches': mismatches == 0, 'mismatched_tiles': mismatches, 'mismatch_examples': samples}
    axes = [name for name in ('vertical', 'horizontal') if transforms[name]['matches']]
    diagonals = [name for name in ('main_diagonal', 'anti_diagonal') if name in transforms and transforms[name]['matches']]
    rotations = [angle for angle in (90, 180, 270) if f'rotation_{angle}' in transforms and transforms[f'rotation_{angle}']['matches']]
    evidence = {'width': width, 'height': height, 'units_ignored': True, 'property_owners_ignored': True, 'hq_preserved': True, 'terrain_directions_ignored': True, 'comparison': 'gameplay_tile_type', 'pipe_rubble_as_plain': True, 'axes': axes, 'diagonals': diagonals, 'rotation_degrees': rotations, 'transforms': transforms, 'unknown_terrain_ids': unknown}
    criteria = {'Flip symmetry': bool(axes), 'diagonal symmetry': bool(diagonals), 'rotational symmetry': bool(rotations), 'Asymmetrical': not (axes or diagonals or rotations)}
    tags = {}
    for tag, matched in criteria.items():
        details = {'criterion': tag, 'axes': axes, 'diagonals': diagonals, 'rotation_degrees': rotations}
        if unknown:
            details.update(reason='unknown_terrain_ids', unknown_terrain_ids=unknown)
        tags[tag] = {'status': 'unresolved' if unknown else 'matched' if matched else 'not_matched', 'evidence': details}
    return {'tags': tags, 'evidence': evidence}
_PROPERTY_WEIGHTS = {'city': 1, 'hq': 1, 'base': 3, 'airport': 3, 'port': 2, 'com_tower': 2, 'lab': 2}

def _foot_distances(width, height, tiles, starts, radius):
    costs = movement_reference()['costs']['clear']
    distances = {start: 0 for start in starts}
    pending = [(0, start) for start in starts]
    heapify(pending)
    while pending:
        distance, index = heappop(pending)
        if distance != distances[index]:
            continue
        for nx, ny in neighbors(index % width, index // width, width, height):
            neighbor = ny * width + nx
            terrain = terrain_name(tiles[neighbor])
            cost = 1 if terrain == 'pipe_seam' else costs.get(terrain, {}).get('foot')
            if cost is None:
                continue
            candidate = distance + cost
            if candidate <= radius and candidate < distances.get(neighbor, float('inf')):
                distances[neighbor] = candidate
                heappush(pending, (candidate, neighbor))
    return distances

def _ratio(a, b):
    return (max(a, b) + 1) / (min(a, b) + 1)

def analyze_team_play(payload, symmetry, building_access=None, *, nearby_radius=6, property_gap=3, property_ratio=1.5, nearby_gap=4, nearby_ratio=1.5):
    for name, value in (('nearby_radius', nearby_radius), ('property_gap', property_gap), ('nearby_gap', nearby_gap)):
        if type(value) is not int or value < (0 if name == 'nearby_radius' else 1):
            raise ValueError(f"{name} must be an integer >= {('0' if name == 'nearby_radius' else '1')}")
    for name, value in (('property_ratio', property_ratio), ('nearby_ratio', nearby_ratio)):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or (not isfinite(value)) or (value < 1):
            raise ValueError(f'{name} must be finite and >= 1')
    evidence = {'method': 'single_property_profile_outlier_v1', 'confidence': 'heuristic', 'thresholds': {'nearby_radius_foot_mp': nearby_radius, 'property_gap': property_gap, 'property_ratio': property_ratio, 'nearby_gap': nearby_gap, 'nearby_ratio': nearby_ratio}, 'property_weights': _PROPERTY_WEIGHTS, 'nearby_policy': 'unique_nearest_owned_base_clear_foot_mp_seams_openable', 'transport_and_teleport_hops_used_for_nearby': False}
    asymmetry = symmetry.get('tags', {}).get('Asymmetrical', {})
    if asymmetry.get('status') != 'matched':
        status = asymmetry.get('status', 'unresolved')
        evidence['reason'] = 'terrain_not_asymmetrical' if status == 'not_matched' else 'symmetry_not_certified'
        return {'status': 'not_matched' if status == 'not_matched' else status, 'evidence': evidence}
    declared = payload.get('Player Count')
    if type(declared) is not int or declared < 1:
        evidence['reason'] = 'missing_or_invalid_player_count'
        return {'status': 'unresolved', 'evidence': evidence}
    evidence['player_count'] = declared
    if declared < 3:
        evidence['reason'] = 'fewer_than_three_players'
        return {'status': 'not_matched', 'evidence': evidence}
    width, height, tiles = grid(payload)
    profiles, bases, neutrals = ({}, {}, [])
    for index, tile in enumerate(tiles):
        kind = property_type(tile)
        if kind is None:
            continue
        owner = property_owner(tile)
        if owner is None:
            neutrals.append(index)
        else:
            profiles.setdefault(owner, Counter())[kind] += 1
            if kind == 'base':
                bases.setdefault(owner, []).append(index)
    for unit in payload.get('Predeployed Units') or []:
        if isinstance(unit, dict) and (owner := country_id(unit.get('Country Code'))) is not None:
            profiles.setdefault(owner, Counter())
    owners = sorted(profiles)
    evidence['represented_country_ids'] = owners
    if len(owners) != declared:
        evidence['reason'] = 'player_count_does_not_match_represented_countries'
        return {'status': 'unresolved', 'evidence': evidence}
    access_by_id = {record['id']: record.get('access') for record in building_access or []}
    excluded = {index for index in neutrals if access_by_id.get(index) == 'inaccessible'}
    uncertain = {index for index in neutrals if access_by_id.get(index) == 'unknown'}
    distances = {owner: _foot_distances(width, height, tiles, bases.get(owner, []), nearby_radius) for owner in owners}
    nearby, unknown_nearby, tied = ({owner: [] for owner in owners}, [], [])
    unknown_nearby_by_owner = Counter()
    for index in neutrals:
        if index in excluded:
            continue
        reaches = [(by_tile[index], owner) for owner, by_tile in distances.items() if index in by_tile]
        if not reaches:
            continue
        distance = min((item[0] for item in reaches))
        nearest = [owner for dist, owner in reaches if dist == distance]
        if len(nearest) != 1:
            tied.append(index)
        elif index in uncertain:
            unknown_nearby.append(index)
            unknown_nearby_by_owner[nearest[0]] += 1
        else:
            nearby[nearest[0]].append({'id': index, 'position': {'x': index % width, 'y': index // width}, 'foot_mp': distance, 'kind': property_type(tiles[index])})
    metrics = {owner: {'country_id': owner, 'pre_owned_by_type': dict(sorted(profiles[owner].items())), 'pre_owned_total': sum(profiles[owner].values()), 'owned_base_positions': [{'x': i % width, 'y': i // width} for i in bases.get(owner, [])], 'exclusive_nearby_neutral_total': len(nearby[owner]), 'exclusive_nearby_neutrals': nearby[owner]} for owner in owners}
    evidence.update(players=list(metrics.values()), excluded_inaccessible_neutral_ids=sorted(excluded), unknown_nearby_neutral_ids=unknown_nearby, tied_nearby_neutral_ids=tied, profile_peer_tolerance=max(2, property_gap - 1), nearby_peer_tolerance=max(2, nearby_gap - 1))
    candidates, possible = ([], [])
    all_kinds = sorted(_PROPERTY_WEIGHTS)
    for owner in owners:
        peers = [other for other in owners if other != owner]
        peer_profile_distance = max((sum((_PROPERTY_WEIGHTS[kind] * abs(profiles[left][kind] - profiles[right][kind]) for kind in all_kinds)) for i, left in enumerate(peers) for right in peers[i + 1:]))
        peer_nearby_spread = max((len(nearby[p]) for p in peers)) - min((len(nearby[p]) for p in peers))
        if peer_profile_distance > max(2, property_gap - 1) or peer_nearby_spread > max(2, nearby_gap - 1):
            continue
        reference = {kind: median((profiles[p][kind] for p in peers)) for kind in all_kinds}
        peer_total = median((metrics[p]['pre_owned_total'] for p in peers))
        total = metrics[owner]['pre_owned_total']
        weighted_gap = sum((_PROPERTY_WEIGHTS[kind] * abs(profiles[owner][kind] - reference[kind]) for kind in all_kinds))
        component_ratios = {kind: _ratio(profiles[owner][kind], reference[kind]) for kind in all_kinds if profiles[owner][kind] != reference[kind]}
        signals = []
        if abs(total - peer_total) >= property_gap and _ratio(total, peer_total) >= property_ratio:
            signals.append('pre_owned_total_outlier')
        if weighted_gap >= property_gap and max(component_ratios.values(), default=0) >= property_ratio:
            signals.append('pre_owned_configuration_outlier')
        nearby_total = len(nearby[owner])
        peer_nearby = median((len(nearby[p]) for p in peers))
        if nearby_total - peer_nearby >= nearby_gap and _ratio(nearby_total, peer_nearby) >= nearby_ratio:
            signals.append('more_exclusive_nearby_neutral_properties')
        if not signals:
            upper_nearby = nearby_total + unknown_nearby_by_owner[owner]
            if upper_nearby - peer_nearby >= nearby_gap and _ratio(upper_nearby, peer_nearby) >= nearby_ratio:
                possible.append({'country_id': owner, 'other_country_ids': peers, 'signals': ['possible_more_exclusive_nearby_neutral_properties'], 'exclusive_nearby_minimum': nearby_total, 'exclusive_nearby_maximum': upper_nearby, 'peer_median_exclusive_nearby': peer_nearby})
            continue
        candidate = {'country_id': owner, 'other_country_ids': peers, 'signals': signals, 'peer_median_pre_owned_by_type': reference, 'pre_owned_total_difference': total - peer_total, 'pre_owned_total_ratio': _ratio(total, peer_total), 'weighted_configuration_gap': weighted_gap, 'configuration_component_ratios': component_ratios, 'exclusive_nearby_difference': nearby_total - peer_nearby, 'exclusive_nearby_ratio': _ratio(nearby_total, peer_nearby), 'peer_profile_max_distance': peer_profile_distance, 'peer_nearby_spread': peer_nearby_spread}
        if signals == ['more_exclusive_nearby_neutral_properties'] and unknown_nearby:
            possible.append(candidate)
        else:
            candidates.append(candidate)
    evidence.update(candidates=candidates, uncertain_candidates=possible)
    if len(candidates) == 1 and (not possible):
        evidence.update(reason='unique_outlier_with_similar_other_players', singled_out_country_id=candidates[0]['country_id'])
        return {'status': 'matched', 'evidence': evidence}
    if possible:
        evidence['reason'] = 'unknown_nearby_access_can_change_outlier'
        return {'status': 'unresolved', 'evidence': evidence}
    evidence['reason'] = 'no_unique_significant_outlier' if not candidates else 'multiple_significant_outliers'
    return {'status': 'not_matched', 'evidence': evidence}
