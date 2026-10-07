from __future__ import annotations
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.map_codec import decode_target
from tools.map_constraints import check_map, validate_settings
from tools.map_features.categories import MULTIPLAYER_CATEGORIES, STRUCTURAL_CATEGORY_TILE_IDS, category_preference, forbidden_tile_tokens
from tools.map_features.symmetry_team import _key, _transform_key
_IDENTITY = (1, 0, 0, 1)
_VERTICAL = (-1, 0, 0, 1)
_DIAGONAL = (0, 1, 1, 0)
_ROTATION = (-1, 0, 0, -1)
_SILOS = {'silo': 'tile:111', 'silo_empty': 'tile:112'}
_HEADS = ('tile', 'owner', 'unit_type', 'unit_owner', 'hp')
SAMPLER_REVISION = 'property-ownership-v3.6'
SAMPLER_LOADED_AT = datetime.now(timezone.utc).isoformat()

def _compose(left, right):
    a, b, c, d = left
    e, f, g, h = right
    return (a * e + b * g, a * f + b * h, c * e + d * g, c * f + d * h)

def symmetry_group(settings):
    tags = settings.get('tags', {})
    generators = []
    if tags.get('Flip symmetry') is True:
        generators.append(_VERTICAL)
    if tags.get('diagonal symmetry') is True:
        generators.append(_DIAGONAL)
    if tags.get('rotational symmetry') is True:
        generators.append(_ROTATION)
    if tags.get('Asymmetrical') is False and (not generators):
        if tags.get('rotational symmetry') is not False:
            generators.append(_ROTATION)
        elif tags.get('Flip symmetry') is not False:
            generators.append(_VERTICAL)
        elif tags.get('diagonal symmetry') is not False:
            generators.append(_DIAGONAL)
    group = {_IDENTITY}
    pending = [_IDENTITY]
    while pending:
        current = pending.pop()
        for generator in generators:
            product = _compose(generator, current)
            if product not in group:
                group.add(product)
                pending.append(product)
    return tuple(sorted(group))

def _destination(index, width, height, matrix):
    x, y = (index % width, index // width)
    a, b, c, d = matrix
    minimum_x = min(0, a * (width - 1), b * (height - 1), a * (width - 1) + b * (height - 1))
    minimum_y = min(0, c * (width - 1), d * (height - 1), c * (width - 1) + d * (height - 1))
    return (c * x + d * y - minimum_y) * width + a * x + b * y - minimum_x

def _orbits(width, height, group):
    visited = set()
    for index in range(width * height):
        if index not in visited:
            orbit = {_destination(index, width, height, matrix) for matrix in group}
            visited.update(orbit)
            yield sorted(orbit)

def _token_transform(token, matrix, key_tokens, gameplay_type_only=False):
    if token.startswith('property:') or gameplay_type_only:
        return token
    return key_tokens[_transform_key(_key(int(token.split(':', 1)[1])), matrix)]

def project_symmetry(tiles, width, height, group, vocabulary, scores=None, forbidden_tokens=()):
    if group == (_IDENTITY,) or len(group) == 1:
        return list(tiles)
    tokens = vocabulary['tile_tokens']
    key_tokens = {_key(int(t.split(':', 1)[1])): t for t in tokens if t.startswith('tile:')}
    token_indices = {token: i for i, token in enumerate(tokens)}
    output = list(tiles)
    for orbit in _orbits(width, height, group):
        root = orbit[0]
        best, best_score = (None, (-math.inf, False))
        sampled = tiles[root]
        for token in [sampled, *(t for t in tokens if t != sampled)]:
            if token in forbidden_tokens:
                continue
            transformed, consistent = ({}, True)
            for matrix in group:
                destination = _destination(root, width, height, matrix)
                try:
                    oriented = _token_transform(token, matrix, key_tokens, vocabulary.get('gameplay_type_only', False))
                except KeyError:
                    consistent = False
                    break
                if oriented not in token_indices or (destination in transformed and transformed[destination] != oriented):
                    consistent = False
                    break
                transformed[destination] = oriented
            if not consistent:
                continue
            if token == sampled:
                best = transformed
                break
            score = sum((float(scores[i][token_indices[t]]) for i, t in transformed.items())) if scores is not None else sum((tiles[i] == t for i, t in transformed.items()))
            ranked_score = (score, False)
            if ranked_score > best_score:
                best, best_score = (transformed, ranked_score)
        if best is None:
            raise ValueError('checkpoint vocabulary cannot represent requested tile symmetry')
        for index, token in best.items():
            output[index] = token
    return output

def _building_token(kind):
    return _SILOS.get(kind, f'property:{kind}')

def _quota_orbits(orbits, desired, score, anchor, tie_breaker=None):
    capacities = {}
    for orbit in orbits:
        capacities[len(orbit)] = capacities.get(len(orbit), 0) + 1
    area = sum(map(len, orbits))
    order = sorted(desired, key=lambda kind: (kind != anchor, -desired[kind], kind))

    def feasible(counts, demands):
        if sum(demands.values()) > sum((weight * count for weight, count in counts.items())):
            return False
        pending = list(demands.values())
        slots, weight = (dict(counts), 1)
        while any(pending):
            available = slots.get(weight, 0)
            mandatory = sum((value % 2 for value in pending))
            if mandatory > available:
                return False
            slots[weight * 2] = slots.get(weight * 2, 0) + (available - mandatory) // 2
            pending = [value // 2 for value in pending]
            weight *= 2
        return True
    pending = dict(desired)
    if not feasible(capacities, pending):
        pending = dict.fromkeys(desired, 0)
        units, slots, weight = (dict(desired), dict(capacities), 1)
        while any(units.values()) and weight <= area:
            available = slots.get(weight, 0)
            for kind in order:
                if units[kind] % 2 and available:
                    pending[kind] += weight
                    available -= 1
                units[kind] //= 2
            slots[weight * 2] = slots.get(weight * 2, 0) + available // 2
            weight *= 2
    allocated = {kind: set() for kind in desired}
    available = set(range(len(orbits)))
    for kind in order:
        token = _building_token(kind)
        ranked = sorted(available, key=lambda i: (sum((score(index, token) for index in orbits[i])) / len(orbits[i]), tie_breaker(orbits[i]) if tie_breaker is not None else -min(orbits[i])), reverse=True)
        while pending[kind]:
            for index in ranked:
                if index not in available or len(orbits[index]) > pending[kind]:
                    continue
                size = len(orbits[index])
                next_capacities = dict(capacities)
                next_capacities[size] -= 1
                next_pending = dict(pending)
                next_pending[kind] -= size
                if feasible(next_capacities, next_pending):
                    allocated[kind].update(orbits[index])
                    available.remove(index)
                    capacities, pending = (next_capacities, next_pending)
                    break
            else:
                break
    return allocated

def _unconstrained_orbit(orbit, width, height, group, vocabulary, score, forbidden_tokens=()):
    tokens = vocabulary['tile_tokens']
    token_set = set(tokens)
    key_tokens = {_key(int(t.split(':', 1)[1])): t for t in tokens if t.startswith('tile:')}
    root = orbit[0]
    best, best_score = (None, -math.inf)
    for token in tokens:
        if token.startswith('property:') or token in _SILOS.values() or token in forbidden_tokens:
            continue
        transformed = {}
        for matrix in group:
            destination = _destination(root, width, height, matrix)
            try:
                oriented = _token_transform(token, matrix, key_tokens, vocabulary.get('gameplay_type_only', False))
            except KeyError:
                break
            if oriented not in token_set or (destination in transformed and transformed[destination] != oriented):
                break
            transformed[destination] = oriented
        else:
            value = sum((score(index, replacement) for index, replacement in transformed.items()))
            if value > best_score:
                best, best_score = (transformed, value)
    if best is None:
        raise ValueError('checkpoint vocabulary has no nonbuilding tile compatible with requested symmetry')
    return best

def repair_target(target, settings, vocabulary, tile_scores=None, unit_scores=None, *, editor_locks=None, editor_group=None, visual_locks=None):
    result = deepcopy(target)
    width, height, players = (result['width'], result['height'], result['players'])
    size = width * height
    tokens = vocabulary['tile_tokens']
    indices = {token: index for index, token in enumerate(tokens)}
    repairs = []
    tiles, owners = (result['tiles'], result['owners'])
    original_owners = list(owners)
    forbidden_tokens = forbidden_tile_tokens(settings)

    def score(index, token):
        if token in forbidden_tokens:
            return -math.inf
        return float(tile_scores[index][indices[token]]) if tile_scores is not None else float(tiles[index] == token)
    group = symmetry_group(settings) if editor_group is None else editor_group
    if editor_locks is not None:
        from tools.map_model.editor_constraints import project_editor_terrain
        projected = project_editor_terrain(tiles, editor_locks, width, height, group, vocabulary, tile_scores, forbidden_tokens)
        for index, fields in editor_locks.items():
            if 1 in fields:
                owners[index] = fields[1]
    else:
        projected = project_symmetry(tiles, width, height, group, vocabulary, tile_scores, forbidden_tokens)
    if tiles != projected:
        repairs.append({'kind': 'tile_symmetry_projection', 'changed_tiles': sum((a != b for a, b in zip(tiles, projected)))})
    tiles[:] = projected
    hq_counts = settings.get('building_counts', {}).get('hq', {})
    lab_only = hq_counts.get('total') == 0 or hq_counts.get('pre_owned') == 0
    anchor_kind = 'lab' if lab_only else 'hq'
    anchor = _building_token(anchor_kind)
    orbits = list(_orbits(width, height, group))
    fixed_orbits = [orbit for orbit in orbits if any((0 in (editor_locks or {}).get(index, {}) for index in orbit))]
    fixed_tiles = {index for orbit in fixed_orbits for index in orbit}
    free_orbits = [orbit for orbit in orbits if not any((index in fixed_tiles for index in orbit))]
    removed = 0
    for orbit in orbits:
        if any((tiles[index] in forbidden_tokens for index in orbit)):
            replacement = _unconstrained_orbit(orbit, width, height, group, vocabulary, score, forbidden_tokens)
            removed += sum((tiles[index] != token for index, token in replacement.items()))
            for index, token in replacement.items():
                tiles[index] = token
                owners[index] = 0
    if removed:
        repairs.append({'kind': 'forbidden_category_tiles', 'changed_tiles': removed, 'forbidden_tokens': sorted(forbidden_tokens)})
    requested = {}
    for kind, counts in settings.get('building_counts', {}).items():
        token = _building_token(kind)
        if token not in indices:
            raise ValueError(f'checkpoint does not support building type {kind}')
        current = [i for i, tile in enumerate(tiles) if tile == token]
        desired = counts.get('total')
        if desired is None and 'pre_owned' in counts and ('neutral' in counts):
            desired = counts['pre_owned'] + counts['neutral']
        if desired is None:
            desired = max(len(current), counts.get('pre_owned', 0) + counts.get('neutral', 0))
        if lab_only and kind == 'lab' and ('total' not in counts) and ('pre_owned' not in counts):
            desired = max(desired, players + counts.get('neutral', 0))
        requested[kind] = desired
    desired = dict(requested)
    if lab_only:
        desired['hq'] = 0
    current_anchors = sum((tile == anchor for tile in tiles))
    if anchor_kind in desired or current_anchors < players:
        desired[anchor_kind] = max(players, desired.get(anchor_kind, current_anchors))
    before_quota = list(tiles)
    if vocabulary.get('gameplay_type_only') and all((sum((tile == _building_token(kind) for tile in tiles)) == count for kind, count in desired.items())) and all((len({tiles[index] for index in orbit}) == 1 for orbit in orbits)):
        allocated = {kind: {index for index, tile in enumerate(tiles) if tile == _building_token(kind)} for kind in desired}
    elif editor_locks is not None:
        fixed_allocated = {kind: {index for index in fixed_tiles if tiles[index] == _building_token(kind)} for kind in desired}
        from tools.map_model.editor_constraints import certified_editor_exclusions
        excluded = certified_editor_exclusions(editor_locks, width, height, vocabulary, players)
        remaining = {kind: max(0, count - len(fixed_allocated[kind]) + excluded[kind]) for kind, count in desired.items()}
        allocated = _quota_orbits(free_orbits, remaining, score, anchor_kind)
        for kind, positions in fixed_allocated.items():
            allocated[kind].update(positions)
        repairs.append({'kind': 'editor_building_budget', 'fixed': {kind: len(positions) for kind, positions in fixed_allocated.items()}, 'remaining': remaining, 'counts_include_preserved_buildings': True})
    else:
        allocated = _quota_orbits(orbits, desired, score, anchor_kind)
    reserved = set().union(*allocated.values()) if allocated else set()
    constrained_tokens = {_building_token(kind) for kind in desired}
    for orbit in orbits:
        if orbit[0] not in reserved and orbit[0] not in fixed_tiles and (tiles[orbit[0]] in constrained_tokens):
            for index, token in _unconstrained_orbit(orbit, width, height, group, vocabulary, score, forbidden_tokens).items():
                tiles[index] = token
                owners[index] = 0
    for kind, selected in allocated.items():
        token = _building_token(kind)
        for index in selected:
            tiles[index] = token
        if kind in requested and {i for i, t in enumerate(before_quota) if t == token} != selected:
            repairs.append({'kind': 'raw_building_quota', 'building': kind, 'requested_raw_tiles': requested[kind]})
    if lab_only:
        for index, (old, new) in enumerate(zip(before_quota, tiles)):
            if old == 'property:hq' and new != old:
                repairs.append({'kind': 'lab_game_remove_hq', 'position': index})
    anchors = [i for i, tile in enumerate(tiles) if tile == anchor]
    inserted_anchors = [i for i in anchors if before_quota[i] != anchor]
    if inserted_anchors:
        repairs.append({'kind': 'active_player_anchors', 'positions': inserted_anchors})
    if len(anchors) < players:
        candidates = sorted(free_orbits, key=lambda orbit: (-sum((i in reserved and tiles[i] != anchor for i in orbit)), sum((score(i, anchor) for i in orbit)) / len(orbit)), reverse=True)
        for orbit in candidates:
            if all((tiles[i] == anchor for i in orbit)):
                continue
            for i in orbit:
                tiles[i] = anchor
            repairs.append({'kind': 'active_player_anchors', 'positions': orbit})
            anchors = [i for i, tile in enumerate(tiles) if tile == anchor]
            if len(anchors) >= players:
                break
    if len(anchors) < players:
        raise ValueError('map has too few cells for active player anchors')
    for category, tile_ids in STRUCTURAL_CATEGORY_TILE_IDS.items():
        category_tokens = {f'tile:{tile}' for tile in tile_ids} & indices.keys()
        if category_preference(settings, category) is not True or any((t in category_tokens for t in tiles)):
            continue
        candidates = [orbit for orbit in free_orbits if all((not tiles[i].startswith('property:') and tiles[i] not in _SILOS.values() for i in orbit))]
        if candidates and category_tokens:
            orbit, token = max(((orbit, token) for orbit in candidates for token in category_tokens), key=lambda choice: sum((score(i, choice[1]) for i in choice[0])) / len(choice[0]))
            for i in orbit:
                tiles[i] = token
                owners[i] = 0
            repairs.append({'kind': 'required_category_tiles', 'category': category, 'positions': orbit})
        else:
            repairs.append({'kind': 'category_presence_residual', 'category': category, 'reason': 'no nonbuilding symmetry orbit available for required category tiles'})
    for kind, count in requested.items():
        actual = sum((tile == _building_token(kind) for tile in tiles))
        if actual != count:
            repairs.append({'kind': 'raw_building_quota_residual', 'building': kind, 'requested_raw_tiles': count, 'actual_raw_tiles': actual, 'difference': actual - count, 'reason': 'quota, symmetry or active-player-anchor combination is not jointly representable'})
    allocated = set()
    for player in range(1, players + 1):
        matching = next((i for i in anchors if i not in allocated and owners[i] == player), None)
        if matching is not None:
            allocated.add(matching)
    missing = [player for player in range(1, players + 1) if not any((owners[i] == player for i in allocated))]
    fixed_owners = {index for index, fields in (editor_locks or {}).items() if 1 in fields}
    available = [i for i in anchors if i not in allocated and i not in fixed_owners]
    for player, index in zip(missing, available):
        owners[index] = player
        allocated.add(index)
    for i in anchors:
        if i in fixed_owners:
            continue
        if not 1 <= owners[i] <= players and (anchor_kind == 'hq' or owners[i] != 0):
            owners[i] = i % players + 1
    for i, tile in enumerate(tiles):
        if i in fixed_owners:
            continue
        if not tile.startswith('property:'):
            owners[i] = 0
        elif tile == 'property:hq':
            owners[i] = min(players, max(1, owners[i]))
        else:
            owners[i] = min(20, max(0, owners[i]))
            if lab_only and tile == 'property:lab':
                owners[i] = min(players, owners[i])
    for kind, counts in settings.get('building_counts', {}).items():
        token = _building_token(kind)
        positions = [i for i, tile in enumerate(tiles) if tile == token]
        if not token.startswith('property:') or kind == 'hq':
            continue
        pre_owned = counts.get('pre_owned')
        if pre_owned is None and 'neutral' in counts:
            pre_owned = max(0, len(positions) - counts['neutral'])
        if pre_owned is not None:
            fixed_owned = sum((owners[i] > 0 for i in positions if i in fixed_owners))
            free_positions = [i for i in positions if i not in fixed_owners]
            free_positions.sort(key=lambda i: (owners[i] > 0, -i), reverse=True)
            for rank, i in enumerate(free_positions):
                owners[i] = owners[i] or rank % players + 1 if rank < max(0, pre_owned - fixed_owned) else 0
            repairs.append({'kind': 'raw_property_ownership_quota', 'building': kind, 'pre_owned': pre_owned})
    units_by_position = {u['y'] * width + u['x']: u for u in result['units']}
    unit_indices = {kind: i + 1 for i, kind in enumerate(vocabulary['unit_types'])}
    fixed_units = {index for index, fields in (editor_locks or {}).items() if 2 in fields}
    for index in fixed_units:
        fields = editor_locks[index]
        if not fields[2]:
            units_by_position.pop(index, None)
        else:
            units_by_position[index] = {'type': vocabulary['unit_types'][fields[2] - 1], 'owner': fields.get(3, 1), 'hp': fields.get(4, 100) / 10, 'x': index % width, 'y': index // width}
    unit_reserved = set(fixed_units)
    for kind, desired in settings.get('predeployed_counts', {}).items():
        current = {i for i, unit in units_by_position.items() if unit['type'] == kind}
        candidates = sorted((i for i in range(size) if i not in unit_reserved), key=lambda i: (float(unit_scores[i][unit_indices[kind]]) if unit_scores is not None else i in current, i in current, -i), reverse=True)
        fixed_kind = current & fixed_units
        selected = fixed_kind | set(candidates[:max(0, desired - len(fixed_kind))])
        for i in current - selected:
            units_by_position.pop(i, None)
        for i in selected:
            old = units_by_position.get(i, {})
            units_by_position[i] = {'type': kind, 'owner': old.get('owner', i % players + 1), 'x': i % width, 'y': i // width, 'hp': old.get('hp', 10)}
        unit_reserved.update(selected)
        if selected != current:
            repairs.append({'kind': 'unit_quota', 'unit_type': kind, 'count': desired})
    result['units'] = sorted(units_by_position.values(), key=lambda u: (u['y'], u['x']))
    for unit in result['units']:
        unit['owner'] = min(20, max(1, int(unit['owner'])))
        unit['hp'] = min(10, max(0.1, float(unit['hp'])))
    largest_owner = max([players, *owners, *(unit['owner'] for unit in result['units'])])
    result['factions'] = [{'slot': i, 'active': i <= players} for i in range(1, largest_owner + 1)]
    changed_owners = sum((a != b for a, b in zip(original_owners, owners)))
    if changed_owners:
        repairs.append({'kind': 'structural_ownership', 'changed_tiles': changed_owners, 'active_players': players})
    from tools.map_model.property_owner_normalization import normalize_generated_property_owners
    result, property_owner_evidence = normalize_generated_property_owners(result, settings, editor_locks=editor_locks)
    repairs.extend(property_owner_evidence)
    from tools.map_model.hq_ownership import repair_hq_ownership
    result, hq_evidence = repair_hq_ownership(result, settings, editor_locks=editor_locks, group=group)
    repairs.extend(hq_evidence)
    from tools.map_model.lab_ownership import repair_lab_ownership
    result, lab_evidence = repair_lab_ownership(result, settings, editor_locks=editor_locks, group=group)
    repairs.extend(lab_evidence)
    from tools.map_model.start_plan import repair_start_plan
    result, start_evidence = repair_start_plan(result, settings, editor_locks=editor_locks, group=group)
    repairs.extend(start_evidence)
    if any((item['kind'] == 'start_plan_skipped' for item in start_evidence)):
        from tools.map_model.ownership_placement import repair_property_ownership
        result, ownership_evidence = repair_property_ownership(result, settings, editor_locks=editor_locks)
        repairs.extend(ownership_evidence)
    else:
        from tools.map_model.ownership_placement import repair_property_ownership
        result, ownership_evidence = repair_property_ownership(result, settings, editor_locks=editor_locks, exclude_buildings={'base'})
        repairs.extend(ownership_evidence)
    from tools.map_model.property_symmetry import repair_property_symmetry
    result, property_evidence = repair_property_symmetry(result, settings, editor_locks=editor_locks, group=group)
    repairs.extend(property_evidence)
    from tools.map_model.unit_start_plan import normalize_generated_unit_owners, repair_unit_start_plan
    result, active_owner_evidence = normalize_generated_unit_owners(result, settings, editor_locks=editor_locks)
    repairs.extend(active_owner_evidence)
    result, unit_start_evidence = repair_unit_start_plan(result, settings, editor_locks=editor_locks)
    repairs.extend(unit_start_evidence)
    tiles = result['tiles']
    from tools.map_model.infantry_placement import repair_infantry_compensation
    infantry_scores = None
    if unit_scores is not None and 'infantry' in unit_indices:
        infantry_scores = [float(row[unit_indices['infantry']]) for row in unit_scores]
    result, infantry_evidence = repair_infantry_compensation(result, settings, editor_locks=editor_locks, position_scores=infantry_scores)
    repairs.extend(infantry_evidence)
    from tools.map_model.immobile_deployment import repair_immobile_deployments
    result, immobile_evidence = repair_immobile_deployments(result, settings, editor_locks=editor_locks)
    repairs.extend(immobile_evidence)
    from tools.map_model.transport_deployment import repair_transport_deployments
    result, transport_evidence = repair_transport_deployments(result, settings, editor_locks=editor_locks)
    repairs.extend(transport_evidence)
    from tools.map_model.deployment_symmetry import build_symmetry_context
    from tools.map_model.immobile_symmetry import repair_immobile_symmetry
    from tools.map_model.transport_symmetry import repair_transport_symmetry
    deployment_context = build_symmetry_context(result, settings, group=group)
    result, paired_static_evidence = repair_immobile_symmetry(result, settings, editor_locks=editor_locks, symmetry_context=deployment_context)
    repairs.extend(paired_static_evidence)
    result, paired_transport_evidence = repair_transport_symmetry(result, settings, editor_locks=editor_locks, symmetry_context=deployment_context)
    repairs.extend(paired_transport_evidence)
    tiles = result['tiles']
    if vocabulary.get('gameplay_type_only') and (not editor_locks):
        from tools.map_model.access_placement import repair_accessible_buildings
        result, access_evidence = repair_accessible_buildings(result, settings, vocabulary, tile_scores=tile_scores, group=group)
        repairs.extend(access_evidence)
        tiles = result['tiles']
    from tools.map_model.orientation import orient_terrain
    oriented = orient_terrain(tiles, width, height, vocabulary, group=group, locked_tiles=visual_locks)
    visual_changes = sum((a != b for a, b in zip(tiles, oriented)))
    if visual_changes:
        tiles[:] = oriented
        repairs.append({'kind': 'visual_sprite_orientation', 'changed_tiles': visual_changes, 'gameplay_types_changed': 0})
    return (result, repairs)

def _validate_request(settings, temperature, refinement_steps, attempts):
    errors = validate_settings(settings)
    if errors:
        raise ValueError('; '.join(errors))
    from tools.map_model.deployment_symmetry import validate_deployment_request
    deployment_errors = validate_deployment_request(settings)
    if deployment_errors:
        raise ValueError('; '.join(deployment_errors))
    for dimension in ('width', 'height'):
        if settings.get(dimension, 1) > 50:
            raise ValueError(f'{dimension} exceeds trained support (maximum 50)')
    if settings.get('players', 1) > 20:
        raise ValueError('players exceeds supported faction slots (maximum 20)')
    minimum_buildings = sum((max(counts.get('total', 0), counts.get('pre_owned', 0) + counts.get('neutral', 0)) for counts in settings.get('building_counts', {}).values()))
    if minimum_buildings > 2500 or sum(settings.get('predeployed_counts', {}).values()) > 2500:
        raise ValueError('requested building or unit quotas exceed supported map area (maximum 2500)')
    buildings = settings.get('building_counts', {})
    hq = buildings.get('hq', {})
    lab = buildings.get('lab', {})
    hq_disabled = hq.get('total') == 0 or hq.get('pre_owned') == 0
    lab_disabled = lab.get('total') == 0 or lab.get('pre_owned') == 0
    if hq_disabled and lab_disabled:
        raise ValueError('model requires owned HQs or labs to represent active players; both cannot be disabled')
    if isinstance(temperature, bool) or not isinstance(temperature, (float, int)) or (not math.isfinite(temperature)) or (not 0.05 <= temperature <= 3):
        raise ValueError('temperature must be between 0.05 and 3')
    if type(refinement_steps) is not int or not 1 <= refinement_steps <= 64:
        raise ValueError('refinement_steps must be an integer between 1 and 64')
    if type(attempts) is not int or not 1 <= attempts <= 16:
        raise ValueError('attempts must be an integer between 1 and 16')
    if 'width' in settings and 'height' in settings:
        area = settings['width'] * settings['height']
        if settings.get('players', 1) > area or sum(settings.get('predeployed_counts', {}).values()) > area:
            raise ValueError('requested players or units exceed available cells')

def _draw(logits, temperature, generator, minimum=None, maximum=None):
    import torch
    values = logits.detach().float().clone() / temperature
    if minimum is not None:
        values[..., :minimum] = -torch.inf
    if maximum is not None:
        values[..., maximum + 1:] = -torch.inf
    probabilities = values.softmax(dim=-1)
    if not torch.isfinite(probabilities).all():
        raise ValueError('checkpoint produced invalid sampling probabilities')
    shape = probabilities.shape[:-1]
    selected = torch.multinomial(probabilities.reshape(-1, probabilities.shape[-1]), 1, generator=generator).reshape(shape)
    confidence = probabilities.gather(-1, selected.unsqueeze(-1)).squeeze(-1)
    return (selected, confidence)

def _choose_metadata(model, conditions, settings, generator, temperature):
    metadata = model.predict_metadata(conditions)
    chosen = {}
    for name, maximum in (('width', 50), ('height', 50), ('players', 20)):
        chosen[name] = settings[name] if name in settings else int(_draw(metadata[name], temperature, generator, 1, maximum)[0].item())
    tags = settings.get('tags', {})
    group = symmetry_group(settings)
    if any((matrix[1] != 0 for matrix in group)):
        if 'width' in settings:
            chosen['height'] = chosen['width']
        elif 'height' in settings:
            chosen['width'] = chosen['height']
        else:
            chosen['height'] = chosen['width']
    requires_multiplayer = any((category_preference(settings, category) is True for category in MULTIPLAYER_CATEGORIES))
    if (tags.get('1vX team play') is True or requires_multiplayer) and 'players' not in settings:
        chosen['players'] = int(_draw(metadata['players'], temperature, generator, 3, 20)[0].item())
    minimum_buildings = sum((max(counts.get('total', 0), counts.get('pre_owned', 0) + counts.get('neutral', 0)) for counts in settings.get('building_counts', {}).values()))
    minimum_area = max(chosen['players'], minimum_buildings, sum(settings.get('predeployed_counts', {}).values()))
    square = any((matrix[1] != 0 for matrix in group))
    if chosen['width'] * chosen['height'] < minimum_area:
        if square:
            minimum_dimension = math.ceil(math.sqrt(minimum_area))
            if 'width' in settings or 'height' in settings or minimum_dimension > 50:
                raise ValueError('requested dimensions have fewer cells than the requested roster or quotas')
            chosen['width'] = int(_draw(metadata['width'], temperature, generator, minimum_dimension, 50)[0].item())
            chosen['height'] = chosen['width']
        elif 'height' not in settings:
            minimum_height = math.ceil(minimum_area / chosen['width'])
            if minimum_height > 50 and 'width' not in settings:
                chosen['width'] = int(_draw(metadata['width'], temperature, generator, math.ceil(minimum_area / 50), 50)[0].item())
                minimum_height = math.ceil(minimum_area / chosen['width'])
            if minimum_height > 50:
                raise ValueError('requested quotas cannot fit supported dimensions')
            chosen['height'] = int(_draw(metadata['height'], temperature, generator, minimum_height, 50)[0].item())
        elif 'width' not in settings:
            minimum_width = math.ceil(minimum_area / chosen['height'])
            if minimum_width > 50:
                raise ValueError('requested quotas cannot fit supported dimensions')
            chosen['width'] = int(_draw(metadata['width'], temperature, generator, minimum_width, 50)[0].item())
    area = chosen['width'] * chosen['height']
    if chosen['players'] > area:
        if 'players' in settings:
            raise ValueError('sampled dimensions have fewer cells than requested players; specify larger dimensions')
        chosen['players'] = int(_draw(metadata['players'], temperature, generator, 1, min(20, area))[0].item())
    faction_count = int(_draw(metadata['faction_count'], temperature, generator, chosen['players'], 20)[0].item())
    return (chosen, faction_count)

def _tile_family_confidence(logits, values, vocabulary, temperature):
    import torch
    from tools.map_features.common import terrain_name
    families = [token if token.startswith('property:') else terrain_name(int(token.split(':')[1])) for token in vocabulary['tile_tokens']]
    names = list(dict.fromkeys(families))
    indices = torch.tensor([names.index(name) for name in families], device=logits.device)
    probabilities = (logits.float() / temperature).softmax(-1)
    summed = probabilities.new_zeros((*probabilities.shape[:-1], len(names)))
    summed.scatter_add_(-1, indices.expand_as(probabilities), probabilities)
    return summed.gather(-1, indices[values].unsqueeze(-1)).squeeze(-1)

def _canonicalize_grid(grid, property_tokens):
    grid[:, :, 1].masked_fill_(~property_tokens[grid[:, :, 0]], 0)
    grid[:, :, 3:5].masked_fill_((grid[:, :, 2] == 0).unsqueeze(-1), 0)

def _apply_sampling_locks(grid, locks):
    from tools.map_model.editor_locks import apply_sampling_locks
    apply_sampling_locks(grid, locks)

def _editor_model_mask(model, masked, locks):
    from tools.map_model.editor_locks import editor_model_mask
    return editor_model_mask(model, masked, locks)

def _target_from_grid(grid, width, height, vocabulary):
    values = grid[0].cpu().tolist()
    target = {'width': width, 'height': height, 'players': 0, 'factions': [], 'tiles': [vocabulary['tile_tokens'][row[0]] for row in values], 'owners': [row[1] for row in values], 'units': []}
    for index, row in enumerate(values):
        if row[2]:
            target['units'].append({'type': vocabulary['unit_types'][row[2] - 1], 'owner': row[3], 'hp': row[4] / 10, 'x': index % width, 'y': index // width})
    return target

def _mask_tile_logits(logits, vocabulary, forbidden_tokens):
    indices = [i for i, token in enumerate(vocabulary['tile_tokens']) if token in forbidden_tokens]
    if indices:
        logits = logits.clone()
        logits[..., indices] = -math.inf
    return logits

def _sampling_policy(checkpoint):
    codec = checkpoint.get('codec', {})
    steps = codec.get('diffusion_steps')
    if codec.get('mask_policy') != 'discrete-diffusion' or codec.get('diffusion_factorization', 'joint') != 'joint':
        raise ValueError('This application requires the joint PPO diffusion model.')
    if type(steps) is not int or not 2 <= steps <= 256 or codec.get('diffusion_schedule') != 'cosine_absorbing':
        raise ValueError('Unsupported discrete diffusion process.')
    if not checkpoint.get('config', {}).get('diffusion_time', False):
        raise ValueError('Checkpoint lacks timestep conditioning.')
    result = {'algorithm': 'absorbing_categorical_diffusion_v1', 'mask_policy': 'discrete-diffusion', 'diffusion_steps': steps, 'diffusion_schedule': 'cosine_absorbing'}
    if codec.get('structured_sampling'):
        if not checkpoint.get('vocab', {}).get('gameplay_type_only'):
            raise ValueError('Checkpoint requires a gameplay-type vocabulary.')
        result.update(algorithm='absorbing_structured_gameplay_diffusion_v3', structured_sampling=True, tile_symmetry='gameplay_type_orbits', ownership='sampled_tile_and_unit_conditioned')
    return result

def _rank_report(report):
    if report['status'] == 'matched':
        return (0, 0)
    invalid = report['status'] in {'invalid_map', 'invalid_settings'}
    return (2 if invalid else 1, len(report['violations']) * 2 + len(report['unresolved']))

def _only_soft_categories_remain(report):
    return report['status'] == 'unresolved' and (not report['violations']) and bool(report['unresolved']) and all((issue.get('path', '').startswith('categories.') and issue.get('reason') == 'official category is a soft label without structural certification' for issue in report['unresolved']))

def generate(checkpoint_path, settings, seed=20261004, temperature=1.0, refinement_steps=16, attempts=4, device='cpu', progress_callback=None, guidance_scale=1.0, editor=None, auto_correct=True):
    import torch
    from tools.map_model.data import encode_settings
    from tools.map_model.model import load_checkpoint
    from backend.map_editor import apply_editor, sampling_locks, validate_editor, resolve_editor_symmetry
    if editor is not None:
        editor, settings = validate_editor(editor, settings)
    _validate_request(settings, temperature, refinement_steps, attempts)
    if type(auto_correct) is not bool:
        raise ValueError('auto_correct must be a boolean')
    editor_group = resolve_editor_symmetry(editor, settings) if editor is not None else None
    from tools.map_model.discrete_diffusion import _check_guidance
    _check_guidance(guidance_scale)
    if type(seed) is not int or not 0 <= seed < 2 ** 63:
        raise ValueError('seed must be an integer from 0 through 2^63-1')
    path = Path(checkpoint_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f'No checkpoint is available yet: {path}')
    started = time.monotonic()
    if progress_callback:
        progress_callback({'phase': 'loading', 'checkpoint': str(path)})
    with torch.random.fork_rng(devices=[]):
        model, checkpoint = load_checkpoint(path, device=device)
    vocabulary = checkpoint['vocab']
    from tools.map_features.common import property_type
    visual_locks = {tile['y'] * editor['width'] + tile['x']: f"tile:{tile['id']}"
                    for tile in editor['tiles'] if property_type(tile['id']) is None} if editor is not None else None
    sampling_policy = _sampling_policy(checkpoint)
    if not auto_correct and sampling_policy.get('structured_sampling'):
        sampling_policy = dict(sampling_policy, structured_sampling=False, algorithm='absorbing_discrete_diffusion_v1', tile_symmetry='none')
    if editor is not None and sampling_policy.get('diffusion_factorization') == 'anchor-staged':
        raise ValueError('anchor-staged diffusion supports new maps only; editor locks are unsupported')
    editor_fields = sampling_locks(editor, vocabulary) if editor is not None else None
    from tools.map_model.editor_palette import editor_palette, map_editor_field_owners, restore_editor_palette
    palette = editor_palette(editor, settings)
    editor_fully_specified = False
    if editor is not None:
        editor_fields = map_editor_field_owners(editor_fields, palette)
        from tools.map_model.editor_constraints import extend_editor_locks
        editor_fields = extend_editor_locks(editor_fields, editor['width'], editor['height'], editor_group, vocabulary, settings)
        from tools.map_model.editor_locks import make_sampling_locks
        editor_fields = make_sampling_locks(editor_fields)
        editor_fully_specified = sum((len(fields) == 5 for fields in editor_fields.values())) == editor['width'] * editor['height']
    if editor_fields and sampling_policy.get('structured_sampling'):
        sampling_policy = dict(sampling_policy, structured_sampling=False, structured_decoder_disabled_for_editor=True, algorithm='absorbing_editor_conditioned_diffusion_v1', tile_symmetry='lock_aware_projection')
    if guidance_scale != 1 and sampling_policy['mask_policy'] != 'discrete-diffusion':
        raise ValueError('guidance_scale is supported only by discrete diffusion checkpoints')
    forbidden_tokens = forbidden_tile_tokens(settings)
    generator = torch.Generator(device=device).manual_seed(seed)
    conditions = encode_settings(settings, vocabulary).to(device).unsqueeze(0)
    metadata_settings = settings
    inferred_players = None
    if editor is not None and 'players' not in settings and (len(editor['tiles']) == editor['width'] * editor['height']):
        from tools.map_features.common import property_type, property_owner
        counts = settings.get('building_counts', {}).get('hq', {})
        anchor_kind = 'lab' if counts.get('total') == 0 or counts.get('pre_owned') == 0 else 'hq'
        owners = {property_owner(tile['id']) for tile in editor['tiles'] if property_type(tile['id']) == anchor_kind}
        owners.discard(None)
        if owners:
            inferred_players = len(owners)
            metadata_settings = dict(settings, players=inferred_players)
    best = None
    attempt_summaries = []
    stop_reason = 'attempt_budget'
    with torch.inference_mode():
        for attempt in range(1, attempts + 1):
            dimensions, faction_count = _choose_metadata(model, conditions, metadata_settings, generator, temperature)
            if editor_fields:
                faction_count = max(faction_count, max((fields.get(column, 0) for fields in editor_fields.values() for column in (1, 3)), default=0))
            if sum(settings.get('predeployed_counts', {}).values()) > dimensions['width'] * dimensions['height']:
                raise ValueError('sampled dimensions have fewer cells than requested units; specify larger dimensions')
            actual_conditions = dict(settings, **dimensions)
            if editor is not None and 'players' not in settings:
                palette = editor_palette(editor, actual_conditions)
                editor_fields = make_sampling_locks(extend_editor_locks(map_editor_field_owners(sampling_locks(editor, vocabulary), palette), editor['width'], editor['height'], editor_group, vocabulary, actual_conditions))
                editor_fully_specified = sum((len(fields) == 5 for fields in editor_fields.values())) == editor['width'] * editor['height']
                faction_count = max(faction_count, max((fields.get(column, 0) for fields in editor_fields.values() for column in (1, 3)), default=0))
            conditions_for_grid = encode_settings(actual_conditions, vocabulary).to(device).unsqueeze(0)
            if progress_callback:
                progress_callback({'phase': 'attempt', 'attempt': attempt, 'attempts': attempts, **dimensions})
            from tools.map_model.discrete_diffusion import sample_discrete
            sampler = sample_discrete
            sampler_steps = sampling_policy['diffusion_steps']
            sampler_options = {}
            if sampling_policy['mask_policy'] == 'discrete-diffusion':
                unconditional = encode_settings({key: dimensions[key] for key in ('width', 'height')}, vocabulary).to(device).unsqueeze(0)
                sampler_options = {'guidance_scale': guidance_scale, 'unconditional_conditions': unconditional, 'settings': actual_conditions, 'structured': sampling_policy.get('structured_sampling', False), 'players': dimensions['players']}
                if sampling_policy.get('diffusion_factorization') in {'staged', 'anchor-staged'}:
                    sampler_options.pop('structured')
            if editor_fields:
                sampler_options['editor_locks'] = editor_fields
            target, tile_scores, unit_scores = sampler(model, conditions_for_grid, dimensions['width'], dimensions['height'], faction_count, vocabulary, generator, temperature, sampler_steps, progress_callback, forbidden_tokens, **sampler_options)
            target.update(dimensions)
            raw_target = deepcopy(target)
            raw_map = raw_report = None
            if sampling_policy.get('structured_sampling') or editor is not None:
                largest = max([dimensions['players'], *raw_target['owners'], *(unit['owner'] for unit in raw_target['units'])])
                raw_target['factions'] = [{'slot': slot, 'active': slot <= dimensions['players']} for slot in range(1, largest + 1)]
                try:
                    raw_map = decode_target(raw_target)
                except ValueError as error:
                    if editor is None:
                        raise
                    raw_report = {'status': 'invalid_representation', 'error': str(error)}
                else:
                    if editor is not None:
                        raw_map = restore_editor_palette(raw_map, palette)
                    raw_report = check_map(raw_map, settings)
            if auto_correct:
                target, repairs = repair_target(target, settings, vocabulary, tile_scores, unit_scores, editor_locks=editor_fields if editor is not None else None, editor_group=editor_group, visual_locks=visual_locks)
            else:
                largest = max([dimensions['players'], *target['owners'], *(unit['owner'] for unit in target['units'])])
                target['factions'] = [{'slot': slot, 'active': slot <= dimensions['players']} for slot in range(1, largest + 1)]
                repairs = []
            payload = decode_target(target, validate_active_factions=auto_correct)
            if editor is not None:
                payload = restore_editor_palette(payload, palette)
                payload = apply_editor(payload, editor)
                if auto_correct:
                    repairs.append({'kind': 'preserved_editor_cells', 'tiles': len(editor['tiles']), 'units': len(editor['units']), 'empty_units': len(editor['empty_units'])})
            payload['Name'] = f"Generated map (seed {seed}, checkpoint {checkpoint.get('step', 0)})"
            report = check_map(payload, settings)
            if auto_correct:
                from tools.map_model.orientation import check_visual_symmetry
                report = check_visual_symmetry(report, payload, editor_group if editor is not None else symmetry_group(settings))
            from tools.map_model.strategy import map_strategic_diagnostics
            strategy = map_strategic_diagnostics(payload, settings, editor_locks=editor_fields if editor is not None else None, deployment_group=editor_group if editor is not None else symmetry_group(settings))
            from tools.map_model.strategy import apply_deployment_constraints
            report = apply_deployment_constraints(report, strategy)
            from tools.map_model.strategy import placement_candidate_rank
            placement_rank = placement_candidate_rank(strategy)
            attempt_summaries.append({'attempt': attempt, 'status': report['status'], 'violations': len(report['violations']), 'unresolved': len(report['unresolved']), 'placement_rank': list(placement_rank)})
            result = {'map': payload, 'report': report, 'success': report['status'] == 'matched', 'seed': seed, 'selected_attempt': attempt, 'checkpoint': {'path': str(path), 'step': checkpoint.get('step', 0)}, 'actual_checkpoint_step': checkpoint.get('step', 0), 'repairs': repairs, 'settings': deepcopy(settings), 'strategy': strategy, 'sampling': {**sampling_policy, 'device': device, 'temperature': temperature, 'refinement_steps': refinement_steps if sampling_policy['mask_policy'] == 'random' else None, 'guidance_scale': guidance_scale, 'auto_correct': auto_correct, 'guidance_method': 'categorical_clean_log_probability_cfg' if guidance_scale != 1 else 'conditional', 'source': 'network_logits', 'dataset_fallback': False, 'generator_revision': SAMPLER_REVISION, 'generator_loaded_at': SAMPLER_LOADED_AT, 'placement_policy': 'joint_player_start_plan_v2', 'forbidden_tile_tokens': sorted(forbidden_tokens)}, 'readiness': 'pilot' if checkpoint.get('step', 0) <= 2000 else 'experimental', 'limitations': ['A matched report certifies requested structural constraints, not playability or balance.', 'Categories without a terrain definition remain soft labels without structural certification.', 'Early pilot checkpoints may produce poor layouts and fail requested features.', 'Neutral building counts use actual reachability checks after raw quota repair.'], 'faction_count': len(target['factions']), 'inactive_factions': sum((not f['active'] for f in target['factions']))}
            if editor is not None:
                result['editor'] = deepcopy(editor)
                result['sampling']['editor_preservation'] = 'during_sampling_and_repairs_exact_sprites_restored_after_orientation'
                result['sampling']['editor_symmetry_group'] = [list(matrix) for matrix in editor_group]
                result['sampling']['editor_owner_slots'] = {str(owner): slot for owner, slot in palette.items() if owner != slot}
                if inferred_players is not None:
                    result['sampling']['editor_inferred_players'] = inferred_players
                result['sampling']['editor_constraint_fields'] = sum((len(fields) for fields in editor_fields.values()))
                result['sampling']['editor_fully_specified'] = editor_fully_specified
                if editor_fully_specified and sampling_policy['mask_policy'] == 'discrete-diffusion':
                    result['sampling']['source'] = 'preserved_and_symmetry_derived_editor'
                result['sampling']['editor_conditioning'] = bool(editor_fields)
                result['sampling']['editor_field_conditioning'] = bool(editor_fields) and getattr(model, 'supports_field_masks', False)
                result['limitations'].append('Representable editor fields and symmetry-derived terrain condition generation; preserved buildings and units consume the requested total inventory. Partial-field context extends checkpoints trained with whole-cell masks.')
                if sampling_policy.get('structured_decoder_disabled_for_editor'):
                    result['limitations'].append('Editor context uses ordinary diffusion transitions and lock-aware quota repairs; raw network samples need not satisfy all requested quotas.')
            if raw_report is not None:
                result.update(raw_target=raw_target, raw_map=raw_map, raw_report=raw_report, structured_sampling=raw_target.get('structured_sampling', {}))
            if editor is None:
                from tools.map_model.strategy import strategic_diagnostics
                result['raw_strategy'] = strategic_diagnostics(raw_target, settings)
            rank = (*_rank_report(report), *placement_rank)
            best_rank = (*_rank_report(best['report']), *placement_candidate_rank(best['strategy'])) if best is not None else None
            if best is None or rank < best_rank:
                best = result
            if report['status'] == 'matched' and placement_rank[0] == 0:
                best, stop_reason = (result, 'matched')
                break
            if _only_soft_categories_remain(report) and placement_rank[0] == 0:
                best, stop_reason = (result, 'soft_categories_only')
                break
    best['attempts'] = len(attempt_summaries)
    best['attempt_reports'] = attempt_summaries
    best['duration_seconds'] = round(time.monotonic() - started, 3)
    best['sampling']['stop_reason'] = stop_reason
    if not best['success']:
        best['failure_reason'] = 'Requested structural constraints are met; remaining category preferences cannot be certified from this map.' if stop_reason == 'soft_categories_only' else 'No sampled candidate satisfied every requested constraint within the retry budget.'
    if progress_callback:
        progress_callback({'phase': 'complete', 'success': best['success'], 'duration_seconds': best['duration_seconds']})
    return best

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint', type=Path, required=True)
    parser.add_argument('--settings', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seed', type=int, default=20261004)
    parser.add_argument('--temperature', type=float, default=1.0)
    parser.add_argument('--guidance-scale', type=float, default=1.0)
    parser.add_argument('--refinement-steps', type=int, default=16)
    parser.add_argument('--attempts', type=int, default=4)
    parser.add_argument('--device', default='cpu')
    args = parser.parse_args(argv)
    settings = json.loads(args.settings.read_text(encoding='utf-8-sig')) if args.settings else {}
    result = generate(args.checkpoint, settings, args.seed, args.temperature, args.refinement_steps, args.attempts, args.device, guidance_scale=args.guidance_scale)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(args.output.resolve()), 'success': result['success'], 'status': result['report']['status'], 'checkpoint': result['checkpoint'], 'duration_seconds': result['duration_seconds']}))
    return 0 if result['success'] else 1
if __name__ == '__main__':
    raise SystemExit(main())
