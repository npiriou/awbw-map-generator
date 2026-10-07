from __future__ import annotations
from collections import deque
from copy import deepcopy
import math
from tools.map_codec import decode_target
from tools.map_features.access_counts import analyze_access_counts
from tools.map_features.categories import category_preference, structural_category_values
from tools.map_features.common import movement_reference, neighbors, terrain_name
from tools.map_features.symmetry_team import SYMMETRY_TAGS, analyze_symmetry, analyze_team_play
from tools.map_features.terrain_units import analyze_terrain_units
_TERRAIN_TAGS = frozenset({'pipe seams', 'predeployed transports', 'immobile pre-deploy'})
_SILOS = {'tile:111': 'silo', 'tile:112': 'silo_empty'}

def _kind(token):
    return token.split(':', 1)[1] if token.startswith('property:') else _SILOS.get(token)

def _gameplay_type(token):
    kind = _kind(token)
    return ('building', kind) if kind else ('terrain', terrain_name(int(token.split(':', 1)[1])))

def _position(index, width):
    return {'x': index % width, 'y': index // width}

def _certify(target):
    payload = decode_target(target)
    return (payload, analyze_access_counts(payload))

def _requested_states(payload, access, settings):
    requested = settings.get('tags', {})
    evaluations = dict(access['tags'])
    if set(requested) & _TERRAIN_TAGS:
        evaluations.update(analyze_terrain_units(payload)['tags'])
    symmetry = None
    if set(requested) & set(SYMMETRY_TAGS) or '1vX team play' in requested:
        symmetry = analyze_symmetry(payload)
        evaluations.update(symmetry['tags'])
    if '1vX team play' in requested:
        evaluations['1vX team play'] = analyze_team_play(payload, symmetry or {}, access['evidence']['building_access'])
    states = {f'tags.{tag}': {'matched': True, 'not_matched': False}.get(evaluations.get(tag, {}).get('status')) for tag in requested}
    categories = structural_category_values(payload)
    category_names = settings.get('categories', {})
    for category in category_names:
        states[f'categories.{category}'] = categories.get(category)
    return states

def _protected_states(payload, access, settings):
    expected = {f'tags.{tag}': value for tag, value in settings.get('tags', {}).items()}
    expected.update({f'categories.{name}': category_preference(settings, name) for name in settings.get('categories', {})})
    return {path: value for path, value in _requested_states(payload, access, settings).items() if value is not None and value == expected[path]}

def _errors(access, requests):
    errors = {}
    for kind, fields in requests.items():
        counts = access['building_counts'][kind]
        for field, expected in fields.items():
            actual = counts[field]
            errors[kind, field] = counts['raw_total'] + expected + 1 if actual is None else abs(actual - expected)
    return errors

def _foot_priority(target, access):
    width, height = (target['width'], target['height'])
    costs = movement_reference()['costs']['clear']
    origins = [site['position'] for site in access['evidence']['capture_origins']]
    origins.extend((site['position'] for site in access['evidence']['building_access'] if site['access'] == 'reachable'))
    distances = {position['y'] * width + position['x']: 0 for position in origins}
    pending = deque(sorted(distances))
    while pending:
        index = pending.popleft()
        for x, y in neighbors(index % width, index // width, width, height):
            neighbor = y * width + x
            token = target['tiles'][neighbor]
            name = 'property' if token.startswith('property:') else terrain_name(int(token.split(':', 1)[1]))
            passable = name == 'pipe_seam' or costs.get(name, {}).get('foot') is not None
            if passable and neighbor not in distances:
                distances[neighbor] = distances[index] + 1
                pending.append(neighbor)
    return distances

def repair_accessible_buildings(target, settings, vocabulary, tile_scores=None, group=None, max_evaluations=48):
    from tools.map_model.sample import _orbits, symmetry_group
    if type(max_evaluations) is not int or not 0 <= max_evaluations <= 256:
        raise ValueError('max_evaluations must be an integer between 0 and 256')
    result = deepcopy(target)
    requests = {kind: fields for kind, fields in settings.get('building_counts', {}).items() if fields}
    if not requests:
        return (result, [])
    payload, access = _certify(result)
    protected = _protected_states(payload, access, settings)
    from tools.map_model.deployment_symmetry import build_symmetry_context
    from tools.map_model.immobile_symmetry import evaluate_immobile_symmetry
    from tools.map_model.transport_symmetry import evaluate_transport_symmetry
    deployment_context = build_symmetry_context(result, settings, group=group)
    deployment_checks = (evaluate_immobile_symmetry, evaluate_transport_symmetry)
    protected_deployments = [check for check in deployment_checks if (diagnostic := check(result, settings, symmetry_context=deployment_context)).get('applicable') and (not diagnostic.get('issue_count'))]
    width, height = (result['width'], result['height'])
    orbits = list(_orbits(width, height, group if group is not None else symmetry_group(settings)))
    if any((not 0 <= index < width * height for orbit in orbits for index in orbit)):
        raise ValueError('symmetry group does not preserve the map dimensions')
    occupied = {unit['y'] * width + unit['x'] for unit in result['units']}
    token_indices = {token: index for index, token in enumerate(vocabulary['tile_tokens'])}
    costs = movement_reference()['costs']['clear']
    errors = _errors(access, requests)
    evidence, evaluations = ([], 0)
    seen = {tuple(result['tiles'])}

    def score(index, token):
        if tile_scores is None or token not in token_indices:
            return 0.0
        value = float(tile_scores[index][token_indices[token]])
        return value if math.isfinite(value) else -1000000000.0
    while any(errors.values()) and evaluations < max_evaluations:
        access_by_position = {site['position']['y'] * width + site['position']['x']: site['access'] for site in access['evidence']['building_access']}
        distances = _foot_priority(result, access)
        sources, destinations = ([], {})
        for orbit in orbits:
            if occupied.intersection(orbit):
                continue
            tiles = [result['tiles'][index] for index in orbit]
            if len({_gameplay_type(tile) for tile in tiles}) != 1:
                continue
            kind = _kind(tiles[0])
            if kind:
                fields = requests.get(kind, {})
                deficit = any((access['building_counts'][kind][field] is None or access['building_counts'][kind][field] < expected for field, expected in fields.items() if field in {'total', 'neutral'}))
                if kind == 'hq' or not deficit or any((result['owners'][index] for index in orbit)) or (not any((access_by_position.get(index) != 'reachable' for index in orbit))):
                    continue
                sources.append((kind, orbit))
            else:
                family = _gameplay_type(tiles[0])[1]
                if family == 'teleport' or costs.get(family, {}).get('foot') is None:
                    continue
                destinations.setdefault(len(orbit), []).append(orbit)
        sources.sort(key=lambda item: (-sum((access_by_position.get(index) != 'reachable' for index in item[1])), item[1][0], item[0]))
        sources = sources[:max_evaluations - evaluations]
        proposals = []
        for kind, source in sources:
            token = result['tiles'][source[0]]
            bad = sum((access_by_position.get(index) != 'reachable' for index in source))
            ranked = []
            for destination in destinations.get(len(source), []):
                priority = sum((index in distances for index in destination))
                gain = sum((score(to, token) + score(frm, result['tiles'][to]) - score(to, result['tiles'][to]) - score(frm, token) for frm, to in zip(source, destination)))
                distance = sum((distances.get(index, width * height) for index in destination))
                ranked.append(((priority, gain, -distance, -destination[0]), destination))
            ranked.sort(reverse=True)
            for rank, destination in ranked[:max_evaluations - evaluations]:
                proposals.append(((rank[0], bad, *rank[1:], -source[0]), kind, source, destination))
        proposals.sort(reverse=True)
        accepted = False
        for _, kind, source, destination in proposals:
            if evaluations >= max_evaluations:
                break
            candidate = deepcopy(result)
            for frm, to in zip(source, destination):
                candidate['tiles'][frm], candidate['tiles'][to] = (candidate['tiles'][to], candidate['tiles'][frm])
                candidate['owners'][frm] = candidate['owners'][to] = 0
                if 'rendering_tiles' in candidate:
                    candidate['rendering_tiles'][frm], candidate['rendering_tiles'][to] = (candidate['rendering_tiles'][to], candidate['rendering_tiles'][frm])
            fingerprint = tuple(candidate['tiles'])
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            evaluations += 1
            candidate_payload, candidate_access = _certify(candidate)
            new_errors = _errors(candidate_access, requests)
            if not all((new_errors[key] <= value for key, value in errors.items())) or not any((new_errors[key] < value for key, value in errors.items())):
                continue
            if any((candidate_access['building_counts'][building]['raw_total'] != counts['raw_total'] or candidate_access['building_counts'][building]['pre_owned'] != counts['pre_owned'] or (counts['neutral'] is not None and (candidate_access['building_counts'][building]['neutral'] is None or candidate_access['building_counts'][building]['neutral'] < counts['neutral'])) for building, counts in access['building_counts'].items())):
                continue
            new_states = _requested_states(candidate_payload, candidate_access, settings)
            if any((new_states.get(path) != value for path, value in protected.items())):
                continue
            if protected_deployments:
                candidate_context = build_symmetry_context(candidate, settings, group=group)
                if deployment_context['geometric'] and (not candidate_context['geometric']):
                    continue
                if any((check(candidate, settings, symmetry_context=candidate_context).get('issue_count') for check in protected_deployments)):
                    continue
            evidence.append({'kind': 'accessible_building_relocation', 'building': kind, 'from': [_position(index, width) for index in source], 'to': [_position(index, width) for index in destination], 'before': access['building_counts'][kind], 'after': candidate_access['building_counts'][kind], 'certificate': 'analyze_access_counts', 'preserved_raw_quotas': True, 'evaluation': evaluations})
            result, access, errors = (candidate, candidate_access, new_errors)
            accepted = True
            break
        if not accepted:
            break
    for (kind, field), error in errors.items():
        if not error:
            continue
        counts, expected = (access['building_counts'][kind], requests[kind][field])
        reason = 'pre_owned_inventory_is_preserved' if field == 'pre_owned' else 'requested_count_exceeds_preserved_raw_capacity' if expected > counts['raw_total'] else 'reachability_remains_uncertified' if counts[field] is None else 'candidate_evaluation_budget_exhausted' if evaluations >= max_evaluations else 'no_certified_improving_same_size_orbit_relocation'
        evidence.append({'kind': 'accessible_building_count_residual', 'building': kind, 'path': f'building_counts.{kind}.{field}', 'requested': expected, 'observed': counts[field], 'raw_total': counts['raw_total'], 'neutral_unreachable': counts['neutral_unreachable'], 'neutral_unknown': counts['neutral_unknown'], 'reason': reason, 'certificate': 'analyze_access_counts', 'evaluations': evaluations, 'max_evaluations': max_evaluations})
    return (result, evidence)
