from __future__ import annotations
from collections import Counter, defaultdict
from copy import deepcopy
import math
from tools.map_features.categories import category_preference
from tools.map_features.common import movement_reference, neighbors, terrain_name
from tools.map_model.ownership_placement import _assign, property_territory_distances
POLICY = 'static_deployment_role_plan_v1'
_PRODUCTION = {'base', 'airport', 'port'}
_GUARDS = {'hq', 'lab', 'city', 'com_tower'}
_LIMITATIONS = ['explicit_decoder_policy_not_learned_static_role', 'structural_mobility_ignores_unit_occupancy_fuel_and_combat', 'foot_anchor_territory_not_naval_routes_for_landlocked_ships', 'combat_effectiveness_and_advantage_of_blockers_not_certified', 'no_first_turn_compensation_or_balance_guarantee', 'teleport_links_and_future_pipe_breaks_not_modelled']

def _kind(token):
    return token.split(':', 1)[1] if token.startswith('property:') else None

def _position(target, unit):
    x, y = (unit.get('x'), unit.get('y'))
    if type(x) is not int or type(y) is not int or (not 0 <= x < target['width']) or (not 0 <= y < target['height']):
        return None
    return y * target['width'] + x

def _adjacent(target, position):
    width = target['width']
    return [y * width + x for x, y in neighbors(position % width, position // width, width, target['height'])]

def _can_enter(target, position, movement):
    token, reference = (target['tiles'][position], movement_reference())
    kind = _kind(token)
    if kind:
        costs = reference['property_costs']['clear'].get(kind)
    else:
        try:
            family = terrain_name(int(token.split(':', 1)[1]))
        except (ValueError, IndexError, AttributeError):
            return None
        costs = reference['costs']['clear'].get(family)
    if costs is None or movement not in costs:
        return None
    return costs[movement] is not None

def unit_mobility(target, unit):
    reference = movement_reference()['units'].get(unit.get('type'))
    position = _position(target, unit)
    if reference is None or position is None:
        return 'unknown'
    adjacent = [_can_enter(target, site, reference['movement_type']) for site in _adjacent(target, position)]
    if True in adjacent:
        return 'mobile'
    if None in adjacent or target['tiles'][position] == 'tile:195':
        return 'unknown'
    return 'immobile'

def _capture_transport(target, unit):
    if unit.get('type') == 'transport_copter':
        return unit_mobility(target, unit) == 'mobile'
    if unit.get('type') not in {'black_boat', 'lander'}:
        return False
    position = _position(target, unit)
    if position is None:
        return False
    return any((not _kind(target['tiles'][site]) and terrain_name(int(target['tiles'][site].split(':', 1)[1])) in {'sea', 'shoal'} for site in _adjacent(target, position)))

def _locks(target, editor_locks):
    blocked = {position for position, fields in (editor_locks or {}).items() if any((column in fields for column in (2, 3, 4)))}
    frozen = {index for index, unit in enumerate(target.get('units', [])) if _position(target, unit) in blocked}
    return (blocked, frozen)

def _balanced_scope(target, settings, editor_locks, active):
    return editor_locks is None and len(active) == 2 and (target.get('players') == 2) and any((category_preference(settings, name) is True for name in ('Standard', 'Fog of War'))) and (not settings.get('tags', {}).get('1vX team play')) and (not any((category_preference(settings, name) is True for name in ('Team Play', 'FFA Multiplay', 'Gimmick', 'Joke', 'Toy-Box', 'Sprite'))))

def _articulations(target):
    size = target['width'] * target['height']
    passable = {position for position in range(size) if _can_enter(target, position, 'foot') is True and target['tiles'][position] != 'tile:195'}
    discovered, low, parent, children, result = ({}, {}, {}, Counter(), set())
    for origin in sorted(passable):
        if origin in discovered:
            continue
        discovered[origin] = low[origin] = len(discovered)
        parent[origin] = None
        stack = [(origin, iter((site for site in _adjacent(target, origin) if site in passable)))]
        while stack:
            position, adjacent = stack[-1]
            destination = next(adjacent, None)
            if destination is None:
                stack.pop()
                previous = parent[position]
                if previous is None:
                    if children[position] > 1:
                        result.add(position)
                else:
                    low[previous] = min(low[previous], low[position])
                    if parent[previous] is not None and low[position] >= discovered[previous]:
                        result.add(previous)
                continue
            if destination not in discovered:
                parent[destination] = position
                children[position] += 1
                discovered[destination] = low[destination] = len(discovered)
                stack.append((destination, iter((site for site in _adjacent(target, destination) if site in passable))))
            elif destination != parent[position]:
                low[position] = min(low[position], discovered[destination])
    return result

def _context(target, settings):
    routes = property_territory_distances(target)
    return {**routes, 'chokepoints': None, 'teams_unknown': settings.get('tags', {}).get('1vX team play') is True or category_preference(settings, 'Team Play') is True}

def _territory(target, position, context):
    sites = [position, *_adjacent(target, position)]
    costs = {slot: min((values.get(site, math.inf) for site in sites), default=math.inf) for slot, values in context['distances'].items()}
    finite = {slot: cost for slot, cost in costs.items() if math.isfinite(cost)}
    if not finite:
        return (None, costs, 'unreachable_from_every_foot_anchor')
    minimum = min(finite.values())
    winners = [slot for slot, cost in finite.items() if cost == minimum]
    if len(winners) != 1:
        return (None, costs, 'tied_foot_anchor_territory')
    return (winners[0], costs, None)

def _role(target, position, owner, context):
    token, property_owner = (target['tiles'][position], target['owners'][position])
    kind, active = (_kind(token), context['active_slots'])
    if owner not in active:
        return {'status': 'not_applicable', 'role': 'inactive_decoration', 'reasons': []}
    nearest, costs, uncertainty = _territory(target, position, context)
    evidence = {'foot_anchor_costs': {slot: None if not math.isfinite(cost) else cost for slot, cost in costs.items()}, 'nearest_foot_anchor_owner': nearest, 'balance_certified': False}
    if kind in _GUARDS and property_owner == owner:
        return {**evidence, 'status': 'matched', 'role': 'own_capture_guard', 'protected_property': kind, 'affected_player': owner, 'reasons': [], 'role_priority': 0 if kind in {'hq', 'lab'} else 2}
    if kind in _PRODUCTION and property_owner == owner:
        return {**evidence, 'status': 'not_matched', 'role': 'self_production_obstruction', 'affected_player': owner, 'reasons': ['stationary_unit_blocks_its_own_production']}
    if kind in _PRODUCTION and property_owner in active and (property_owner != owner):
        if context['teams_unknown']:
            return {**evidence, 'status': 'unresolved', 'role': 'production_blocker_team_relation_unknown', 'affected_player': property_owner, 'reasons': ['alliance_relation_not_supplied']}
        return {**evidence, 'status': 'matched', 'role': 'foreign_production_blocker', 'affected_player': property_owner, 'blocked_property': kind, 'reasons': [], 'role_priority': 1}
    if uncertainty:
        return {**evidence, 'status': 'unresolved', 'role': 'uncertain_static_site', 'reasons': [uncertainty]}
    if context['teams_unknown']:
        return {**evidence, 'status': 'unresolved', 'role': 'static_blocker_team_relation_unknown', 'reasons': ['alliance_relation_not_supplied']}
    if kind and property_owner == 0 and (nearest != owner):
        return {**evidence, 'status': 'matched', 'role': 'neutral_production_blocker' if kind in _PRODUCTION else 'neutral_capture_blocker', 'affected_player': nearest, 'blocked_property': kind, 'reasons': [], 'role_priority': 2}
    if context['chokepoints'] is None:
        context['chokepoints'] = _articulations(target)
    if position in context['chokepoints'] and nearest != owner:
        return {**evidence, 'status': 'matched', 'role': 'foot_route_chokepoint_blocker', 'affected_player': nearest, 'reasons': [], 'role_priority': 3}
    return {**evidence, 'status': 'not_matched', 'role': 'unexplained_stationary_site', 'reasons': ['no_supported_capture_guard_production_blocker_or_foot_chokepoint_role']}

def evaluate_immobile_deployments(target, settings=None, *, editor_locks=None):
    settings = settings or {}
    requested = settings.get('tags', {}).get('immobile pre-deploy')
    context = _context(target, settings)
    _, frozen = _locks(target, editor_locks)
    units, issues, groups, counts = ([], [], defaultdict(list), Counter())
    for index, unit in enumerate(target.get('units', [])):
        mobility = unit_mobility(target, unit)
        counts[mobility] += 1
        detail = {'unit_index': index, 'unit_type': unit.get('type'), 'owner': unit.get('owner'), 'position': {'x': unit.get('x'), 'y': unit.get('y')}, 'mobility': mobility, 'editor_unit_fixed': index in frozen, 'balance_certified': False}
        position = _position(target, unit)
        if mobility == 'immobile':
            role = _role(target, position, unit.get('owner'), context)
            detail.update(role)
            if unit.get('owner') in context['active_slots']:
                groups[unit['type']].append(index)
            if requested is False:
                detail.update(status='not_matched', reasons=['immobile_deployments_explicitly_disabled', *role['reasons']])
            if detail['status'] not in {'matched', 'not_applicable'} or requested is False:
                issues.append(deepcopy(detail))
        elif mobility == 'unknown':
            detail.update(status='unresolved', role='unknown_mobility', reasons=['unknown_unit_terrain_position_or_teleport_escape'])
            issues.append(deepcopy(detail))
        else:
            detail.update(status='not_applicable', role='mobile_deployment', reasons=[])
        if index in frozen:
            detail['preservation_reason'] = 'immutable_editor_unit_fields'
        elif unit.get('owner') not in context['active_slots']:
            detail['preservation_reason'] = 'inactive_decorative_owner'
        units.append(detail)
    residuals = []
    balanced = _balanced_scope(target, settings, editor_locks, context['active_slots'])
    if balanced and requested is not False:
        for kind, indices in groups.items():
            inventory = {slot: sum((target['units'][index]['owner'] == slot for index in indices)) for slot in context['active_slots']}
            if len(indices) >= 2 and max(inventory.values()) - min(inventory.values()) > 1:
                residuals.append({'unit_type': kind, 'stationary_active_counts': inventory, 'reason': 'ordinary_two_player_static_roles_have_unequal_type_counts'})
    if requested is True:
        tag_status = 'matched' if counts['immobile'] else 'unresolved' if counts['unknown'] else 'not_matched'
    elif requested is False:
        tag_status = 'not_matched' if counts['immobile'] else 'unresolved' if counts['unknown'] else 'matched'
    else:
        tag_status = 'not_requested'
    if tag_status == 'not_matched' and (not issues):
        issues.append({'status': 'not_matched', 'reasons': ['requested_immobile_tag_has_no_structural_witness']})
    issue_count = len(issues) + len(residuals)
    return {'policy': POLICY, 'applicable': True, 'requested': requested, 'tag_status': tag_status, 'status': 'matched' if not issue_count and tag_status != 'unresolved' else 'unresolved' if not residuals and all((issue['status'] == 'unresolved' for issue in issues)) else 'not_matched', 'immobile_count': counts['immobile'], 'mobile_count': counts['mobile'], 'unknown_count': counts['unknown'], 'immobile_unit_indices': [unit['unit_index'] for unit in units if unit['mobility'] == 'immobile'], 'units': units, 'issues': issues, 'issue_count': issue_count, 'role_balance_residuals': residuals, 'active_slots': context['active_slots'], 'uncertainty': context['uncertainty'], 'balanced_static_role_counts_applicable': balanced, 'limitations': list(_LIMITATIONS), 'balance_certified': False}

def _capacities(target, indices, active, balanced):
    inventory = {slot: sum((target['units'][index]['owner'] == slot for index in indices)) for slot in active}
    if not balanced or len(indices) < 2:
        return inventory
    extra = max(active, key=lambda slot: (inventory[slot], -slot))
    return {slot: len(indices) // 2 + int(len(indices) % 2 and slot == extra) for slot in active}

def _plan(target, indices, capacities, sites, blocked):
    occupied = {_position(target, unit) for index, unit in enumerate(target['units']) if index not in indices} | blocked
    candidates = sorted({position for by_position in sites.values() for position, site in by_position.items() if site['status'] == 'matched' and position not in occupied})
    if len(candidates) < len(indices):
        return None
    current = {slot: {_position(target, target['units'][index]) for index in indices if target['units'][index]['owner'] == slot} for slot in capacities}

    def cost(slot, position):
        site = sites[slot][position]
        if site['status'] != 'matched':
            return 10 ** 12
        affected = site.get('affected_player', slot)
        distance = site.get('foot_anchor_costs', {}).get(affected)
        if distance is None:
            distance = site.get('class_origin_cost', 0) or 0
        return int(position not in current[slot]) * 10 ** 7 + site.get('role_priority', 0) * 10 ** 4 + distance
    assigned = _assign(candidates, capacities, cost)
    if any((sites[slot][position]['status'] != 'matched' for position, slot in assigned.items())):
        return None
    pending, plan = (set(indices), {})
    for position, owner in sorted(assigned.items(), key=lambda item: (item[1], item[0])):
        index = min(pending, key=lambda index: (target['units'][index]['owner'] != owner, _position(target, target['units'][index]) != position, abs(target['units'][index]['x'] - position % target['width']) + abs(target['units'][index]['y'] - position // target['width']), index))
        pending.remove(index)
        plan[index] = (owner, position)
    return plan

def repair_immobile_deployments(target, settings, *, editor_locks=None):
    result, evidence = (deepcopy(target), [])
    before = evaluate_immobile_deployments(result, settings, editor_locks=editor_locks)
    requested = before['requested']
    blocked, frozen = _locks(result, editor_locks)
    context = _context(result, settings)
    active = context['active_slots']
    if not active or len(result.get('units', [])) > 64:
        reason = 'missing_active_factions' if not active else 'bounded_planner_unit_limit_exceeded'
        return (result, [{'kind': 'immobile_deployment_unresolved', 'reason': reason, 'diagnostic': before}])
    balanced = _balanced_scope(result, settings, editor_locks, active)
    groups = defaultdict(list)
    for index in before['immobile_unit_indices']:
        unit = result['units'][index]
        if index not in frozen and unit['owner'] in active:
            groups[unit['type']].append(index)
    mobile_context = None

    def sites_for(kind, want_mobile=False):
        nonlocal mobile_context
        sites = {slot: {} for slot in active}
        reference = movement_reference()['units'].get(kind)
        if reference is None:
            return sites
        if want_mobile:
            from tools.map_model.unit_start_plan import _context as class_context, _site
            if mobile_context is None:
                mobile_context = class_context(result)
        for position in range(result['width'] * result['height']):
            probe = {'type': kind, 'x': position % result['width'], 'y': position // result['width']}
            mobility = unit_mobility(result, probe)
            for slot in active:
                if want_mobile and len(active) == 1:
                    foreign = _kind(result['tiles'][position]) is not None and result['owners'][position] not in {0, slot}
                    legal = mobility == 'mobile' and _can_enter(result, position, reference['movement_type']) is True and (not foreign)
                    sites[slot][position] = {'status': 'matched' if legal else 'not_matched', 'role': 'single_player_legal_mobile_start', 'class_route_reachability_certified': False}
                elif want_mobile:
                    sites[slot][position] = _site(result, position, slot, reference['movement_type'], mobile_context)
                elif mobility == 'immobile':
                    sites[slot][position] = _role(result, position, slot, context)
                else:
                    sites[slot][position] = {'status': 'not_matched'}
        return sites

    def apply_plan(plan, sites, kind, source):
        for index, (owner, position) in sorted(plan.items()):
            unit, original = (result['units'][index], deepcopy(result['units'][index]))
            unit.update(owner=owner, x=position % result['width'], y=position // result['width'])
            if unit != original:
                evidence.append({'kind': 'immobile_deployment_placement', 'unit_index': index, 'unit_type': kind, 'source': source, 'before': original, 'after': deepcopy(unit), 'role_evidence': deepcopy(sites[owner][position]), 'preserved_unit_count_type_hp_and_terrain': True, 'balance_certified': False})
    for kind, indices in sorted(groups.items()):
        sites = sites_for(kind, requested is False)
        original_counts = _capacities(result, indices, active, False)
        wanted = _capacities(result, indices, active, balanced and requested is not False)
        plan = _plan(result, indices, wanted, sites, blocked)
        if plan is None and wanted != original_counts:
            evidence.append({'kind': 'immobile_role_owner_balance_residual', 'unit_type': kind, 'requested_counts': wanted, 'preserved_counts': original_counts, 'reason': 'balanced_static_roles_have_no_complete_assignment'})
            plan = _plan(result, indices, original_counts, sites, blocked)
        if plan is None:
            evidence.append({'kind': 'immobile_deployment_unresolved', 'unit_type': kind, 'reason': 'no_complete_unoccupied_meaningful_static_role' if requested is not False else 'no_complete_legal_mobile_class_start', 'preserved_unit_count_type_hp_and_terrain': True})
        else:
            apply_plan(plan, sites, kind, 'remove_disabled_immobility' if requested is False else 'explicit_static_blocker_or_capture_guard')
    if requested is True and (not any((unit_mobility(result, unit) == 'immobile' for unit in result['units']))):
        witnesses = [index for index, unit in enumerate(result['units']) if _capture_transport(result, unit)]
        protected = set(witnesses) if settings.get('tags', {}).get('predeployed transports') is True and len(witnesses) == 1 else set()
        candidates = [index for index, unit in enumerate(result['units']) if index not in frozen | protected and unit['owner'] in active and (unit_mobility(result, unit) == 'mobile')]
        priority = {'black_boat': 0, 'lander': 1, 'battleship': 2, 'artillery': 3}
        for index in sorted(candidates, key=lambda index: (priority.get(result['units'][index]['type'], 10), index)):
            kind = result['units'][index]['type']
            sites = sites_for(kind)
            plan = _plan(result, [index], _capacities(result, [index], active, False), sites, blocked)
            if plan is not None:
                apply_plan(plan, sites, kind, 'introduce_one_requested_structurally_immobile_role')
                break
        else:
            evidence.append({'kind': 'immobile_deployment_unresolved', 'reason': 'no_available_meaningful_immobile_site_or_unit', 'preserved_unique_mobile_transport_witness': bool(protected), 'preserved_unit_count_type_hp_and_terrain': True})
    after = evaluate_immobile_deployments(result, settings, editor_locks=editor_locks)
    if evidence or after['issue_count'] or after['tag_status'] == 'unresolved':
        evidence.append({'kind': 'immobile_deployment_summary', 'before_issue_count': before['issue_count'], 'after_issue_count': after['issue_count'], 'diagnostic': after, 'balance_certified': False})
    return (result, evidence)
