from __future__ import annotations
from collections import Counter, defaultdict
from copy import deepcopy
import heapq
import math
from tools.map_features.categories import category_preference
from tools.map_features.common import movement_reference, neighbors, terrain_name
from tools.map_model.opening_metrics import evaluate_opening
from tools.map_model.ownership_placement import _assign
POLICY = 'class_unit_start_plan_v2'
_SPECIAL = ('Team Play', 'FFA Multiplay', 'Gimmick', 'Joke', 'Sprite', 'Toy-Box')
_LIMITATIONS = ['explicit_decoder_policy_not_learned_unit_ownership', 'equal_same_type_counts_do_not_certify_balance_or_first_turn_compensation', 'clear_weather_class_routes_without_co_fuel_combat_or_transported_movement', 'terrain_mobility_ignores_temporary_unit_occupancy', 'teleport_links_and_future_pipe_seam_breaks_not_modelled', 'unit_types_are_planned_sequentially_with_other_types_occupying_their_current_cells']

def _scope(target, settings, editor_locks):
    active = sorted((faction['slot'] for faction in target.get('factions', []) if faction.get('active')))
    if len(active) < 2 or len(set(active)) != target.get('players'):
        return 'requires_complete_active_roster'
    if settings.get('players', target.get('players')) != target.get('players'):
        return 'requested_roster_mismatch'
    if not any((category_preference(settings, name) is True for name in ('Standard', 'Fog of War'))):
        return 'requires_explicit_standard_or_fog'
    if any((category_preference(settings, name) is True for name in _SPECIAL)):
        return 'special_category_requested'
    if settings.get('tags', {}).get('1vX team play') is True:
        return 'intentional_team_deployment'
    return None

def _locks(target, editor_locks):
    blocked = {position for position, fields in (editor_locks or {}).items() if any((column in fields for column in (2, 3, 4)))}
    fixed = {index for index, unit in enumerate(target.get('units', [])) if _unit_position(target, unit) in blocked}
    return (blocked, fixed)

def _kind(token):
    return token.split(':', 1)[1] if token.startswith('property:') else None

def _cost(target, position, movement):
    token, reference = (target['tiles'][position], movement_reference())
    kind = _kind(token)
    if kind:
        return reference['property_costs']['clear'].get(kind, {}).get(movement)
    family = terrain_name(int(token.split(':', 1)[1]))
    if family == 'teleport':
        return None
    return reference['costs']['clear'].get(family, {}).get(movement)

def _adjacent(target, position):
    width = target['width']
    return [y * width + x for x, y in neighbors(position % width, position // width, width, target['height'])]

def _distances(target, movement, origins):
    values = {position: cost for position, cost in origins.items() if _cost(target, position, movement) is not None}
    pending = [(cost, position) for position, cost in values.items()]
    heapq.heapify(pending)
    while pending:
        cost, position = heapq.heappop(pending)
        if cost != values[position]:
            continue
        for adjacent in _adjacent(target, position):
            step = _cost(target, adjacent, movement)
            if step is None:
                continue
            candidate = cost + step
            if candidate < values.get(adjacent, math.inf):
                values[adjacent] = candidate
                heapq.heappush(pending, (candidate, adjacent))
    return values

def _context(target):
    active = sorted((faction['slot'] for faction in target['factions'] if faction['active']))
    anchor = 'hq' if any((_kind(token) == 'hq' and owner in active for token, owner in zip(target['tiles'], target['owners']))) else 'lab'
    anchors = {slot: [index for index, (token, owner) in enumerate(zip(target['tiles'], target['owners'])) if _kind(token) == anchor and owner == slot] for slot in active}
    land_origins = {slot: {index: 0 for index, (token, owner) in enumerate(zip(target['tiles'], target['owners'])) if owner == slot and _kind(token) in {'base', anchor}} for slot in active}
    foot = {slot: _distances(target, 'foot', origins) for slot, origins in land_origins.items()}
    return {'active': active, 'anchors': anchors, 'anchor_kind': anchor, 'foot': foot, 'routes': {}}

def _routes(target, movement, context):
    if movement in context['routes']:
        return context['routes'][movement]
    active, anchor = (context['active'], context['anchor_kind'])
    naval = movement in {'sea', 'lander'}
    production = {'port'} if naval else {'airport', anchor} if movement == 'air' else {'base', anchor}
    routes = {}
    for slot in active:
        origins = {index: 0 for index, (token, owner) in enumerate(zip(target['tiles'], target['owners'])) if owner == slot and _kind(token) in production}
        if naval:
            for position in range(target['width'] * target['height']):
                if _cost(target, position, movement) is None:
                    continue
                for shore in _adjacent(target, position):
                    own = context['foot'][slot].get(shore, math.inf)
                    opponent = min((context['foot'][other].get(shore, math.inf) for other in active if other != slot))
                    if own < math.inf and own <= opponent:
                        origins[position] = min(origins.get(position, math.inf), own + 1)
        routes[slot] = _distances(target, movement, origins)
    context['routes'][movement] = routes
    return routes

def _site(target, position, owner, movement, context):
    size = target['width'] * target['height']
    if not 0 <= position < size:
        return {'status': 'not_matched', 'reasons': ['invalid_unit_position']}
    token, property_owner = (target['tiles'][position], target['owners'][position])
    kind = _kind(token)
    if _cost(target, position, movement) is None:
        return {'status': 'not_matched', 'reasons': ['unit_starts_on_impassable_or_uncertified_terrain']}
    mobile = any((_cost(target, adjacent, movement) is not None for adjacent in _adjacent(target, position)))
    if not mobile:
        return {'status': 'not_matched', 'reasons': ['no_passable_adjacent_destination']}
    if property_owner > 0 and property_owner != owner and (kind is not None):
        return {'status': 'not_matched', 'reasons': ['foreign_owned_production_or_anchor']}
    routes = _routes(target, movement, context)
    own = routes[owner].get(position, math.inf)
    opponent = min((routes[slot].get(position, math.inf) for slot in context['active'] if slot != owner))
    own_property = kind is not None and property_owner == owner
    if own_property:
        return {'status': 'matched', 'reasons': [], 'own_property_start': True, 'class_origin_cost': None if not math.isfinite(own) else own, 'territory_tied': own == opponent, 'existing_forward_property_supported': True}
    if not math.isfinite(own):
        return {'status': 'unresolved', 'reasons': ['no_owned_production_or_coast_class_route']}
    if own > opponent:
        return {'status': 'not_matched', 'reasons': ['opponent_class_region_has_shorter_route'], 'class_origin_cost': own, 'opponent_class_origin_cost': opponent}
    return {'status': 'matched', 'reasons': [], 'own_property_start': False, 'class_origin_cost': own, 'territory_tied': own == opponent}

def _unit_position(target, unit):
    x, y = (unit.get('x'), unit.get('y'))
    if type(x) is not int or type(y) is not int or (not 0 <= x < target['width']) or (not 0 <= y < target['height']):
        return -1
    return y * target['width'] + x

def _wanted_counts(units, active):
    counts = Counter((unit['owner'] for unit in units))
    total = len(units)
    if total < 2:
        return {slot: counts[slot] for slot in active}
    extra = max(active, key=lambda slot: (counts[slot], -slot))
    return {slot: total // 2 + int(total % 2 and slot == extra) for slot in active}

def _eligible_group(target, settings, kind, indices, active, editor_locks=None):
    if kind in {'black_boat', 'lander', 'transport_copter'}:
        return 'transport_delegated_to_cargo_or_static_role_policy'
    if kind == 'infantry' and sum((unit.get('type') == kind for unit in target['units'])) == 1:
        from tools.map_model.infantry_placement import evaluate_infantry_compensation
        if evaluate_infantry_compensation(target, settings, editor_locks=editor_locks)['applicable']:
            return 'single_infantry_delegated_to_compensation_policy'
    return None

def _static(target, unit, settings):
    from tools.map_model.immobile_deployment import unit_mobility
    return settings.get('tags', {}).get('immobile pre-deploy') is not False and unit_mobility(target, unit) == 'immobile'

def _compensated_infantry_role(target, settings, kind, indices, context):
    if kind != 'infantry' or len(indices) < 2:
        return None
    inventory = {slot: sum((target['units'][index]['owner'] == slot for index in indices)) for slot in context['active']}
    if len(set(inventory.values())) == 1 or any((not anchors for anchors in context['anchors'].values())):
        return None
    if any((_site(target, _unit_position(target, target['units'][index]), target['units'][index]['owner'], 'foot', context)['status'] != 'matched' for index in indices)):
        return None
    opening = evaluate_opening(target, settings)
    disparity = opening.get('disparity', {})
    if not opening.get('applicable') or opening.get('uncertainty') != [] or opening.get('status') != 'no_large_disparity_detected' or (not disparity.get('initial_production_p1_minus_p2')) or (not disparity.get('initial_difference_compensated_in_first_capture_model')):
        return None
    return {'unit_type': kind, 'role': 'existing_unequal_infantry_economic_compensation', 'preserved_active_counts': inventory, 'independent_opening_policy': opening['policy'], 'initial_production_p1_minus_p2': disparity['initial_production_p1_minus_p2'], 'initial_difference_compensated_in_first_capture_model': True, 'independent_opening_disparity': disparity, 'uncertainty': [], 'unit_start_mobility_checked': True, 'balance_certified': False}

def evaluate_unit_start_plan(target, settings, *, editor_locks=None):
    reason = _scope(target, settings, editor_locks)
    result = {'policy': POLICY, 'applicable': reason is None, 'balance_certified': False, 'limitations': list(_LIMITATIONS), 'issues': [], 'owner_balance_residuals': [], 'units': [], 'preserved_joint_opening_roles': []}
    if reason:
        return dict(result, status='not_applicable', skip_reason=reason, issue_count=0)
    context, groups = (_context(target), defaultdict(list))
    _, fixed = _locks(target, editor_locks)
    result['active_slots'] = context['active']
    for index, unit in enumerate(target.get('units', [])):
        if index in fixed:
            result['units'].append({'unit_index': index, 'unit_type': unit['type'], 'status': 'not_applicable', 'skip_reason': 'immutable_editor_unit_fields'})
        elif _static(target, unit, settings):
            result['units'].append({'unit_index': index, 'unit_type': unit['type'], 'status': 'not_applicable', 'skip_reason': 'actual_immobile_unit_delegated_to_static_role_policy'})
        elif unit['owner'] in context['active']:
            groups[unit['type']].append(index)
        else:
            result['units'].append({'unit_index': index, 'unit_type': unit['type'], 'status': 'not_applicable', 'skip_reason': 'inactive_decorative_owner_preserved'})
    for kind, indices in sorted(groups.items()):
        skip = _eligible_group(target, settings, kind, indices, context['active'], editor_locks)
        reference = movement_reference()['units'].get(kind)
        if skip:
            result['units'].extend(({'unit_index': index, 'unit_type': kind, 'status': 'not_applicable', 'skip_reason': skip} for index in indices))
            continue
        compensated_role = _compensated_infantry_role(target, settings, kind, indices, context)
        if compensated_role:
            result['preserved_joint_opening_roles'].append(compensated_role)
            result['units'].extend(({'unit_index': index, 'unit_type': kind, 'owner': target['units'][index]['owner'], 'status': 'matched', 'role': compensated_role['role'], 'unit_start_mobility_checked': True, 'owner_count_equalization_applicable': False} for index in indices))
            continue
        inventory = {slot: sum((target['units'][index]['owner'] == slot for index in indices)) for slot in context['active']}
        balanced = len(context['active']) == 2 and editor_locks is None
        wanted = _wanted_counts([target['units'][index] for index in indices], context['active']) if balanced else inventory
        if inventory != wanted:
            result['owner_balance_residuals'].append({'unit_type': kind, 'actual_active_counts': inventory, 'requested_conventional_counts': wanted, 'odd_remainder_policy': 'retain_existing_majority_owner'})
        for index in indices:
            unit = target['units'][index]
            detail = {'unit_index': index, 'unit_type': kind, 'owner': unit['owner'], 'position': {'x': unit['x'], 'y': unit['y']}}
            if reference is None:
                site = {'status': 'unresolved', 'reasons': ['unknown_unit_movement_reference']}
            elif any((not anchors for anchors in context['anchors'].values())):
                site = {'status': 'unresolved', 'reasons': ['missing_active_hq_or_lab_anchor']}
            else:
                detail['movement_type'] = reference['movement_type']
                site = _site(target, _unit_position(target, unit), unit['owner'], reference['movement_type'], context)
            result['units'].append(dict(detail, **site))
            if site['status'] != 'matched':
                result['issues'].append(dict(detail, **site))
    result['issue_count'] = len(result['issues']) + len(result['owner_balance_residuals'])
    result['status'] = 'matched' if not result['issue_count'] else 'unresolved' if not result['owner_balance_residuals'] and all((issue['status'] == 'unresolved' for issue in result['issues'])) else 'not_matched'
    return result

def _plan_group(target, indices, capacities, movement, context, blocked=()):
    width, size = (target['width'], target['width'] * target['height'])
    selected, remaining = ({}, dict(capacities))
    others = {_unit_position(target, unit) for index, unit in enumerate(target['units']) if index not in indices} | set(blocked)
    sites = {slot: {position: _site(target, position, slot, movement, context) for position in range(size)} for slot in context['active']}
    ordered = sorted(indices, key=lambda index: (not bool(sites[target['units'][index]['owner']].get(_unit_position(target, target['units'][index]), {}).get('own_property_start')), index))
    occupied = set(others)
    for index in ordered:
        unit, position = (target['units'][index], _unit_position(target, target['units'][index]))
        owner = unit['owner']
        if remaining[owner] and position not in occupied and (sites[owner].get(position, {}).get('status') == 'matched'):
            selected[index] = (owner, position)
            remaining[owner] -= 1
            occupied.add(position)
    pending = [index for index in indices if index not in selected]
    if not pending:
        return selected
    candidates = [position for position in range(size) if position not in occupied and any((sites[slot][position]['status'] == 'matched' for slot in context['active']))]
    if len(candidates) < len(pending):
        return None
    roles, unassigned = ({}, set(pending))
    spare = dict(remaining)
    for slot in context['active']:
        own = [index for index in unassigned if target['units'][index]['owner'] == slot]
        own.sort(key=lambda index: (sites[slot].get(_unit_position(target, target['units'][index]), {}).get('status') != 'matched', sites[slot].get(_unit_position(target, target['units'][index]), {}).get('class_origin_cost') or 0, index))
        for index in own[:spare[slot]]:
            roles[index] = slot
            unassigned.remove(index)
            spare[slot] -= 1
    for slot in context['active']:
        for index in sorted(unassigned, key=lambda index: (sites[slot].get(_unit_position(target, target['units'][index]), {}).get('status') != 'matched', index))[:spare[slot]]:
            roles[index] = slot
            unassigned.remove(index)
    existing = {slot: {_unit_position(target, target['units'][index]) for index in pending if roles[index] == slot} for slot in context['active']}
    maximum = max((site.get('class_origin_cost') or 0 for by_position in sites.values() for site in by_position.values()), default=0) + width + target['height'] + 1
    preserve_weight = maximum * (len(pending) + 1)
    impossible_weight = (preserve_weight + maximum) * (len(pending) + 1)

    def cost(slot, position):
        site = sites[slot][position]
        if site['status'] != 'matched':
            return impossible_weight
        return int(position not in existing[slot]) * preserve_weight + (site.get('class_origin_cost') or 0)
    assignment = _assign(candidates, remaining, cost)
    if any((sites[slot][position]['status'] != 'matched' for position, slot in assignment.items())):
        return None
    pool = set(pending)
    for position, owner in sorted(assignment.items(), key=lambda item: (item[1], item[0])):
        index = min((index for index in pool if roles[index] == owner), key=lambda index: (_unit_position(target, target['units'][index]) != position, abs(target['units'][index]['x'] - position % width) + abs(target['units'][index]['y'] - position // width), index))
        selected[index] = (owner, position)
        pool.remove(index)
    return selected

def normalize_generated_unit_owners(target, settings, *, editor_locks=None):
    result, evidence = (deepcopy(target), [])
    if _scope(result, settings, editor_locks):
        return (result, evidence)
    context = _context(result)
    _, fixed = _locks(result, editor_locks)
    for index, unit in enumerate(result.get('units', [])):
        if index in fixed or unit['owner'] in context['active']:
            continue
        position = _unit_position(result, unit)
        reference = movement_reference()['units'].get(unit['type'])
        if position < 0 or reference is None:
            continue
        routes = _routes(result, reference['movement_type'], context)
        foot = context['foot']
        owner = min(context['active'], key=lambda slot: (routes[slot].get(position, math.inf), foot[slot].get(position, math.inf), sum((other['owner'] == slot and other['type'] == unit['type'] for other in result['units'])), slot))
        original = deepcopy(unit)
        unit['owner'] = owner
        evidence.append({'kind': 'generated_unit_active_owner', 'unit_index': index, 'before': original, 'after': deepcopy(unit), 'source': 'active_player_class_territory_policy', 'preserved_unit_count_type_hp': True, 'balance_certified': False})
    return (result, evidence)

def repair_unit_start_plan(target, settings, *, editor_locks=None):
    result, evidence = (deepcopy(target), [])
    before = evaluate_unit_start_plan(result, settings, editor_locks=editor_locks)
    if not before['applicable']:
        return (result, evidence)
    context, groups = (_context(result), defaultdict(list))
    blocked, fixed = _locks(result, editor_locks)
    if any((not anchors for anchors in context['anchors'].values())):
        return (result, [{'kind': 'unit_start_plan_unresolved', 'reason': 'missing_active_anchor', 'diagnostic': before}])
    for index, unit in enumerate(result.get('units', [])):
        if index not in fixed and (not _static(result, unit, settings)) and (unit['owner'] in context['active']):
            groups[unit['type']].append(index)
    for kind, indices in sorted(groups.items()):
        if _eligible_group(result, settings, kind, indices, context['active'], editor_locks):
            continue
        reference = movement_reference()['units'].get(kind)
        if reference is None:
            evidence.append({'kind': 'unit_start_plan_unresolved', 'unit_type': kind, 'reason': 'unknown_unit_movement_reference'})
            continue
        if _compensated_infantry_role(result, settings, kind, indices, context):
            continue
        inventory = {slot: sum((result['units'][index]['owner'] == slot for index in indices)) for slot in context['active']}
        balanced = len(context['active']) == 2 and editor_locks is None
        wanted = _wanted_counts([result['units'][index] for index in indices], context['active']) if balanced else inventory
        if inventory == wanted and all((_site(result, _unit_position(result, result['units'][index]), result['units'][index]['owner'], reference['movement_type'], context)['status'] == 'matched' for index in indices)):
            continue
        plan = _plan_group(result, indices, wanted, reference['movement_type'], context, blocked)
        if plan is None and wanted != inventory:
            evidence.append({'kind': 'unit_start_owner_balance_residual', 'unit_type': kind, 'requested_conventional_counts': wanted, 'preserved_original_counts': inventory, 'reason': 'no_complete_viable_assignment_for_balanced_owner_capacities', 'balance_certified': False})
            plan = _plan_group(result, indices, inventory, reference['movement_type'], context, blocked)
        if plan is None:
            evidence.append({'kind': 'unit_start_plan_unresolved', 'unit_type': kind, 'reason': 'no_complete_unoccupied_class_territory_start_assignment', 'preserved_unit_count_type_hp': True, 'changed_cells': 0})
            continue
        for index, (owner, position) in sorted(plan.items()):
            unit, original = (result['units'][index], deepcopy(result['units'][index]))
            unit.update(owner=owner, x=position % result['width'], y=position // result['width'])
            if unit != original:
                evidence.append({'kind': 'unit_start_plan_placement', 'unit_index': index, 'unit_type': kind, 'source': 'explicit_conventional_class_territory_policy', 'before': original, 'after': deepcopy(unit), 'movement_type': reference['movement_type'], 'owner_reassigned_for_count_balance': owner != original['owner'], 'balanced_active_counts_requested': wanted, 'odd_remainder_policy': 'retain_existing_majority_owner', 'preserved_unit_count_type_hp': True, 'balance_certified': False})
    after = evaluate_unit_start_plan(result, settings, editor_locks=editor_locks)
    if evidence or after['issue_count']:
        evidence.append({'kind': 'unit_start_plan_summary', 'before_issue_count': before['issue_count'], 'after_issue_count': after['issue_count'], 'diagnostic': after, 'balance_certified': False})
    return (result, evidence)
