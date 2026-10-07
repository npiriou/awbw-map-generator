from __future__ import annotations
from copy import deepcopy
import heapq
import math
from tools.map_features.categories import category_preference
from tools.map_features.common import movement_reference, neighbors, terrain_name
_LIMITATIONS = ['canonical_second_active_slot_is_a_generated_role_convention_not_stored_match_turn_order', 'clear_weather_foot_costs_no_co_effects', 'teleport_links_not_used', 'conventional_compensation_does_not_certify_balance']

def _scope(target, settings, editor_locks):
    if editor_locks is not None:
        return 'editor_context'
    if target.get('players') != 2 or settings.get('players', 2) != 2:
        return 'requires_two_players'
    if not any((category_preference(settings, category) is True for category in ('Standard', 'Fog of War'))):
        return 'requires_explicit_standard_or_fog'
    if any((category_preference(settings, category) is True for category in ('Team Play', 'FFA Multiplay', 'Gimmick', 'Joke', 'Sprite', 'Toy-Box'))):
        return 'special_category_requested'
    if settings.get('tags', {}).get('1vX team play') is True:
        return 'special_deployment_requested'
    if settings.get('tags', {}).get('immobile pre-deploy') is True:
        from tools.map_model.immobile_deployment import unit_mobility
        if any((unit.get('type') == 'infantry' and unit_mobility(target, unit) != 'mobile' for unit in target.get('units', []))):
            return 'stationary_infantry_delegated_to_static_role_policy'
    requests = settings.get('predeployed_counts', {})
    if requests.get('infantry') != 1:
        return 'requires_exactly_one_requested_infantry'
    if any((value > 0 for kind, value in requests.items() if kind not in {'infantry', 'black_boat'})):
        return 'other_unit_types_requested'
    units = target.get('units', [])
    if sum((unit.get('type') == 'infantry' for unit in units)) != 1:
        return 'requires_exactly_one_generated_infantry'
    if any((unit.get('type') not in {'infantry', 'black_boat'} for unit in units)):
        return 'other_unit_types_generated'
    active = sorted((faction['slot'] for faction in target.get('factions', []) if faction.get('active')))
    if len(active) != 2 or len(set(active)) != 2:
        return 'invalid_active_roster'
    return None

def _cost(target, index):
    token = target['tiles'][index]
    family = 'property' if token.startswith('property:') else terrain_name(int(token.split(':', 1)[1]))
    if family == 'teleport':
        return None
    return movement_reference()['costs']['clear'].get(family, {}).get('foot')

def _distances(target, origins, *, blocked=(), maximum=None):
    width, height = (target['width'], target['height'])
    blocked = set(blocked)
    distances = {index: 0 for index in origins if index not in blocked and _cost(target, index) is not None}
    pending = [(0, index) for index in distances]
    heapq.heapify(pending)
    while pending:
        cost, index = heapq.heappop(pending)
        if cost != distances[index]:
            continue
        for x, y in neighbors(index % width, index // width, width, height):
            adjacent = y * width + x
            step = _cost(target, adjacent)
            if adjacent in blocked or step is None:
                continue
            candidate = cost + step
            if maximum is not None and candidate > maximum:
                continue
            if candidate < distances.get(adjacent, math.inf):
                distances[adjacent] = candidate
                heapq.heappush(pending, (candidate, adjacent))
    return distances

def _context(target, *, compensation_owner=None):
    active = sorted((faction['slot'] for faction in target['factions'] if faction['active']))
    anchor_kind = 'hq' if any((token == 'property:hq' for token in target['tiles'])) else 'lab'
    anchors = {owner: [index for index, token in enumerate(target['tiles']) if token == f'property:{anchor_kind}' and target['owners'][index] == owner] for owner in active}
    distances = {owner: _distances(target, positions) for owner, positions in anchors.items()}
    width = target['width']
    infantry = next((unit for unit in target['units'] if unit['type'] == 'infantry'))
    others = [unit for unit in target['units'] if unit is not infantry]
    occupied = {unit['y'] * width + unit['x'] for unit in others}
    p1, p2 = active if compensation_owner is None else (next((owner for owner in active if owner != compensation_owner)), compensation_owner)
    blockers = {unit['y'] * width + unit['x'] for unit in others if unit['owner'] != p2}
    neutral_bases = [index for index, token in enumerate(target['tiles']) if token == 'property:base' and target['owners'][index] == 0 and (index not in occupied) and (distances[p2].get(index, math.inf) < distances[p1].get(index, math.inf))]
    return {'p1': p1, 'p2': p2, 'infantry': infantry, 'anchors': anchors, 'distances': distances, 'occupied': occupied, 'blockers': blockers, 'neutral_bases': neutral_bases, 'anchor_kind': anchor_kind}

def _existing_economic_compensation(target, settings, context):
    owner = context['infantry']['owner']
    active = (context['p1'], context['p2'])
    if owner not in active:
        return None
    owned = {slot: sum((token == 'property:base' and actual == slot for token, actual in zip(target['tiles'], target['owners']))) for slot in active}
    other = next((slot for slot in active if slot != owner))
    if owned[owner] >= owned[other]:
        return None
    from tools.map_model.opening_metrics import evaluate_opening
    opening = evaluate_opening(target, settings)
    if not opening['applicable'] or opening.get('uncertainty') or (not opening['disparity']['initial_difference_compensated_in_first_capture_model']):
        return None
    own_context = _context(target, compensation_owner=owner)
    unit = own_context['infantry']
    index = unit['y'] * target['width'] + unit['x']
    placement = _placement(target, index, own_context, preserve_existing_owned_base=True)
    if placement is None:
        return None
    placement = dict(placement, compensation_owner=owner)
    return {'expected_owner': owner, 'placement': placement, 'initial_owned_base_counts': owned, 'joint_opening_disparity': opening['disparity'], 'uncertainty': opening['uncertainty']}

def _placement(target, index, context, *, preserve_existing_owned_base=False):
    p1, p2 = (context['p1'], context['p2'])
    if not all(context['anchors'].values()):
        return None
    if index in context['occupied'] or _cost(target, index) is None:
        return None
    d1, d2 = (context['distances'][p1].get(index, math.inf), context['distances'][p2].get(index, math.inf))
    if target['owners'][index] not in (0, p2):
        return None
    owned_base = target['tiles'][index] == 'property:base' and target['owners'][index] == p2
    territory_verified = d2 < d1
    if not territory_verified and (not (preserve_existing_owned_base and owned_base)):
        return None
    reachable = _distances(target, [index], blocked=context['blockers'], maximum=3)
    capture = [{'x': base % target['width'], 'y': base // target['width'], 'foot_cost': reachable[base]} for base in context['neutral_bases'] if base in reachable]
    capture.sort(key=lambda site: (site['foot_cost'], site['y'], site['x']))
    mobile = any((position != index for position in reachable))
    if not (owned_base and mobile or capture):
        return None
    return {'position': {'x': index % target['width'], 'y': index // target['width']}, 'on_p2_owned_base': owned_base, 'turn_1_neutral_bases': capture, 'p2_anchor_foot_distance': None if not math.isfinite(d2) else d2, 'p1_anchor_foot_distance': None if not math.isfinite(d1) else d1, 'territory_verified': territory_verified, 'existing_owned_base_preserved_without_hq_proximity_assumption': not territory_verified, 'placement_mode': 'p2_owned_base' if owned_base else 'turn_1_neutral_base_capture'}

def evaluate_infantry_compensation(target, settings, *, editor_locks=None):
    reason = _scope(target, settings, editor_locks)
    diagnostic = {'policy': 'ordinary_2p_single_infantry_standard_or_fog_compensation_v2', 'applicable': reason is None, 'limitations': list(_LIMITATIONS)}
    if reason:
        return dict(diagnostic, status='not_applicable', skip_reason=reason)
    context = _context(target)
    unit = context['infantry']
    economic = _existing_economic_compensation(target, settings, context)
    if economic is not None:
        return dict(diagnostic, status='matched', role='existing_economic_compensation', canonical_p2_convention_applied=False, expected_owner=economic['expected_owner'], actual_owner=unit['owner'], hp=unit['hp'], position={'x': unit['x'], 'y': unit['y']}, placement=economic['placement'], owner_matched=True, placement_matched=True, residuals=[], economic_compensation_evidence=economic)
    index = unit['y'] * target['width'] + unit['x']
    placement = _placement(target, index, context, preserve_existing_owned_base=True)
    owner_matched = unit['owner'] == context['p2']
    residual = []
    if not all(context['anchors'].values()):
        residual.append('missing_active_hq_or_lab_anchor')
    if not owner_matched:
        residual.append('infantry_owner_is_not_canonical_p2')
    if placement is None:
        residual.append('infantry_not_on_eligible_p2_base_or_turn_1_neutral_base_route')
    return dict(diagnostic, status='matched' if owner_matched and placement else 'not_matched', role='canonical_p2_compensation', canonical_p2_convention_applied=True, expected_owner=context['p2'], actual_owner=unit['owner'], hp=unit['hp'], position={'x': unit['x'], 'y': unit['y']}, placement=placement, owner_matched=owner_matched, placement_matched=placement is not None, residuals=residual)

def repair_infantry_compensation(target, settings, *, editor_locks=None, position_scores=None):
    result = deepcopy(target)
    before = evaluate_infantry_compensation(result, settings, editor_locks=editor_locks)
    if not before['applicable'] or before['status'] == 'matched':
        return (result, [])
    context = _context(result)
    current = context['infantry']['y'] * result['width'] + context['infantry']['x']
    placement = _placement(result, current, context, preserve_existing_owned_base=True)
    chosen = current if placement else None
    if chosen is None:
        candidates = []
        for index in range(result['width'] * result['height']):
            candidate = _placement(result, index, context)
            if candidate is None:
                continue
            score = float(position_scores[index]) if position_scores is not None else 0.0
            score = score if math.isfinite(score) else -1000000000.0
            nearest = min((site['foot_cost'] for site in candidate['turn_1_neutral_bases']), default=4)
            rank = (candidate['on_p2_owned_base'], bool(candidate['turn_1_neutral_bases']), -nearest, score, -candidate['p2_anchor_foot_distance'], -index)
            candidates.append((rank, index, candidate))
        if candidates:
            _, chosen, placement = max(candidates)
    if chosen is None:
        return (result, [{'kind': 'infantry_compensation_unresolved', 'reason': 'no_eligible_p2_start', 'diagnostic': before, 'changed_cells': 0}])
    unit = context['infantry']
    original = deepcopy(unit)
    unit.update(owner=context['p2'], x=chosen % result['width'], y=chosen // result['width'])
    after = evaluate_infantry_compensation(result, settings, editor_locks=editor_locks)
    return (result, [{'kind': 'infantry_compensation_placement', 'source': 'explicit_conventional_policy', 'before': original, 'after': deepcopy(unit), 'placement': placement, 'diagnostic': after, 'changed_cells': 1 if chosen == current else 2}])
