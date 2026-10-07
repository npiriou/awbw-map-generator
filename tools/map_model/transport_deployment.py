from __future__ import annotations
from collections import Counter, defaultdict
from copy import deepcopy
import heapq
import math
from tools.map_features.categories import category_preference
from tools.map_features.common import movement_reference, terrain_name
from tools.map_features.terrain_units import TRANSPORT_TYPES
POLICY = 'ordinary_transport_pickup_deployment_v1'
_BOATS = {'black_boat', 'lander'}
_SPECIAL = ('Team Play', 'FFA Multiplay', 'Gimmick', 'Joke', 'Sprite', 'Toy-Box')
_LIMITATIONS = ['explicit_decoder_policy_not_learned_transport_ownership', 'clear_weather_structural_routes_without_co_fuel_combat_or_existing_cargo', 'pickup_endpoints_require_owned_foot_territory_and_an_adjacent_foot_destination', 'naval_pickup_endpoints_are_shoals_or_ports_as_in_frozen_island_access', 'alive_unloaded_owned_infantry_mech_are_actual_ground_cargo_footholds', 'teleport_links_and_future_pipe_seam_breaks_not_modelled', 'mobility_and_exact_tag_ignore_temporary_unit_occupancy', 'valid_forward_starts_preserved_where_assignment_capacity_allows', 'ordinary_two_player_mobile_same_type_count_policy_is_not_a_balance_or_fta_certificate', 'three_or_more_players_preserve_per_owner_inventory', 'editor_has_no_count_equalization_but_unlocked_missing_pickup_owners_can_change']

def _helpers():
    from tools.map_model.unit_start_plan import _adjacent, _context, _cost, _kind, _unit_position
    return (_adjacent, _context, _cost, _kind, _unit_position)

def _scope(target, settings):
    active = sorted((f['slot'] for f in target.get('factions', []) if f.get('active')))
    if len(active) < 2 or len(set(active)) != len(active) or target.get('players') != len(active) or (settings.get('players', target.get('players')) != len(active)):
        return 'requires_matching_active_player_roster'
    if any((category_preference(settings, name) is True for name in _SPECIAL)):
        return 'special_category_requested'
    if settings.get('tags', {}).get('1vX team play') is True:
        return 'intentional_unequal_team_deployment'
    preference = settings.get('tags', {}).get('predeployed transports')
    if preference is not True and preference is not False and (not any((category_preference(settings, name) is True for name in ('Standard', 'Fog of War')))):
        return 'requires_explicit_standard_fog_or_transport_preference'
    return None

def _family(token):
    if token.startswith('property:'):
        return 'property'
    try:
        return terrain_name(int(token.split(':', 1)[1]))
    except (ValueError, IndexError):
        return None

def _enter(target, position, movement):
    _, _, _, kind_of, _ = _helpers()
    token, reference = (target['tiles'][position], movement_reference())
    kind = kind_of(token)
    classes = reference['property_costs']['clear'].get(kind) if kind else reference['costs']['clear'].get(_family(token))
    if classes is None or movement not in classes:
        return None
    return classes[movement] is not None

def _facts(target, kind, position):
    adjacent, _, _, _, _ = _helpers()
    size = target['width'] * target['height']
    if not 0 <= position < size:
        return {'mobility': 'unknown', 'tag_qualifies': None, 'reason': 'invalid_unit_position'}
    movement = movement_reference()['units'][kind]['movement_type']
    destinations = adjacent(target, position)
    passable = [_enter(target, p, movement) for p in destinations]
    mobility = 'mobile' if any((value is True for value in passable)) else 'unknown' if any((value is None for value in passable)) or _family(target['tiles'][position]) == 'teleport' else 'immobile'
    if kind in _BOATS:
        families = [_family(target['tiles'][p]) for p in destinations]
        qualifies = True if any((f in {'sea', 'shoal'} for f in families)) else None if None in families else False
    else:
        qualifies = True if mobility == 'mobile' else None if mobility == 'unknown' else False
    return {'mobility': mobility, 'tag_qualifies': qualifies, 'tag_rule': 'adjacent_sea_or_beach' if kind in _BOATS else 'mobile_transport_copter'}

def _locks(editor_locks):
    return {position for position, fields in (editor_locks or {}).items() if {2, 3, 4, 'unit', 'unit_owner', 'unit_hp'}.intersection(fields)}

def _ground_site(target, position, owner, context):
    _, _, cost, kind_of, _ = _helpers()
    if cost(target, position, 'foot') is None:
        return False
    kind, property_owner = (kind_of(target['tiles'][position]), target['owners'][position])
    if kind and property_owner > 0 and (property_owner != owner):
        return False
    own = context['foot'][owner].get(position, math.inf)
    if not math.isfinite(own):
        return False
    if kind and property_owner == owner:
        return True
    opponent = min((context['foot'][other].get(position, math.inf) for other in context['active'] if other != owner), default=math.inf)
    return own <= opponent

def _reverse_routes(target, movement, endpoints):
    adjacent, _, cost, _, _ = _helpers()
    values = {position: 0 for position in endpoints}
    queue = [(0, position) for position in endpoints]
    heapq.heapify(queue)
    while queue:
        value, position = heapq.heappop(queue)
        if value != values[position]:
            continue
        step = cost(target, position, movement)
        for previous in adjacent(target, position):
            if cost(target, previous, movement) is None:
                continue
            candidate = value + step
            if candidate < values.get(previous, math.inf):
                values[previous] = candidate
                heapq.heappush(queue, (candidate, previous))
    return values

def _transport_context(target):
    adjacent, context_of, cost, kind_of, position_of = _helpers()
    context = context_of(target)
    from tools.map_model.unit_start_plan import _distances
    cargo_origins = {owner: [] for owner in context['active']}
    for index, unit in enumerate(target.get('units', [])):
        owner, position, hp = (unit.get('owner'), position_of(target, unit), unit.get('hp'))
        if owner not in cargo_origins or unit.get('type') not in {'infantry', 'mech'} or position < 0 or (type(hp) not in (int, float)) or (not 0 < hp <= 10) or (unit.get('transport') is not None) or (unit.get('loaded', False) is not False) or ('position' in unit and unit['position'] is None) or (cost(target, position, 'foot') is None):
            continue
        cargo_origins[owner].append({'unit_index': index, 'unit_type': unit['type'], 'position': position})
    for owner, sources in cargo_origins.items():
        if sources:
            origins = {position: 0 for position, value in context['foot'][owner].items() if value == 0}
            origins.update({source['position']: 0 for source in sources})
            context['foot'][owner] = _distances(target, 'foot', origins)
    context['capture_unit_foot_origins'] = cargo_origins
    size = target['width'] * target['height']
    ground = {owner: {position for position in range(size) if _ground_site(target, position, owner, context)} for owner in context['active']}
    endpoints, routes = ({'lander': {}, 'air': {}}, {'lander': {}, 'air': {}})
    for owner in context['active']:
        for movement in ('lander', 'air'):
            sites = []
            for position in ground[owner]:
                if cost(target, position, movement) is None:
                    continue
                if movement == 'lander' and (not (_family(target['tiles'][position]) == 'shoal' or kind_of(target['tiles'][position]) == 'port')):
                    continue
                if movement == 'lander' and (not any((cost(target, p, movement) is not None for p in adjacent(target, position)))):
                    continue
                if not any((p in ground[owner] for p in adjacent(target, position))):
                    continue
                sites.append(position)
            endpoints[movement][owner] = sorted(sites)
            routes[movement][owner] = _reverse_routes(target, movement, sites)
    context.update(pickup_endpoints=endpoints, pickup_routes=routes, ground_territory=ground, blocker_cache={}, static_role_context=None)
    return context

def _route_from_source(target, position, movement, owner, context):
    adjacent, _, cost, _, _ = _helpers()
    route = context['pickup_routes'][movement][owner]
    if cost(target, position, movement) is not None:
        return route.get(position, math.inf)
    return min((cost(target, p, movement) + route.get(p, math.inf) for p in adjacent(target, position) if cost(target, p, movement) is not None), default=math.inf)

def _coherent_blocker(target, position, owner, settings, context):
    key = (position, owner)
    if key in context['blocker_cache']:
        return context['blocker_cache'][key]
    from tools.map_model.immobile_deployment import _context as static_context, _role
    if context['static_role_context'] is None:
        context['static_role_context'] = static_context(target, settings)
    diagnostic = _role(target, position, owner, context['static_role_context'])
    role = diagnostic if diagnostic['status'] == 'matched' else None
    context['blocker_cache'][key] = role
    return role

def _site(target, kind, position, owner, settings, context, *, existing=False):
    adjacent, _, cost, kind_of, _ = _helpers()
    preference = settings.get('tags', {}).get('predeployed transports')
    immobile_preference = settings.get('tags', {}).get('immobile pre-deploy')
    facts = _facts(target, kind, position)
    if facts['mobility'] == 'unknown':
        return dict(facts, status='unresolved', reasons=['uncertified_transport_terrain_or_teleport_mobility'])
    if facts['mobility'] == 'immobile':
        blocker = _coherent_blocker(target, position, owner, settings, context) if existing and immobile_preference is not False else None
        if blocker and (preference is not False or facts['tag_qualifies'] is False):
            return dict(facts, status='matched', reasons=[], role=blocker['role'], static_role=blocker, intentional_blocker_preserved=True, pickup_applicable=False)
        return dict(facts, status='not_matched', reasons=['no_mobile_transport_or_supported_existing_blocker_role'])
    if preference is False and facts['tag_qualifies'] is not False:
        return dict(facts, status='not_matched', reasons=['explicit_no_transport_tag_conflicts_with_this_mobile_start'])
    if kind_of(target['tiles'][position]) and target['owners'][position] > 0 and (target['owners'][position] != owner):
        return dict(facts, status='not_matched', reasons=['foreign_owned_property_start'])
    movement = 'air' if kind == 'transport_copter' else 'lander'
    own = _route_from_source(target, position, movement, owner, context)
    other = min((_route_from_source(target, position, movement, slot, context) for slot in context['active'] if slot != owner), default=math.inf)
    if not math.isfinite(own):
        return dict(facts, status='not_matched', reasons=['no_owned_foot_territory_pickup_endpoint_in_usable_component'], pickup_endpoint_count=len(context['pickup_endpoints'][movement][owner]))
    if own > other:
        return dict(facts, status='not_matched', reasons=['other_player_pickup_endpoint_has_shorter_real_transport_route'], pickup_route_cost=own, other_pickup_route_cost=other)
    if kind == 'transport_copter' and own > movement_reference()['units'][kind]['movement_points']:
        return dict(facts, status='not_matched', reasons=['no_own_land_pickup_within_initial_copter_move'], pickup_route_cost=own)
    if movement == 'lander' and cost(target, position, movement) is not None and (not any((cost(target, p, movement) is not None for p in adjacent(target, position)))):
        return dict(facts, status='not_matched', reasons=['no_usable_water_component_destination'])
    return dict(facts, status='matched', reasons=[], role='own_territory_capture_transport', pickup_applicable=True, movement_type=movement, pickup_route_cost=own, pickup_endpoint_count=len(context['pickup_endpoints'][movement][owner]), pickup_territory_tied=own == other, pickup_reachable_in_initial_move=own <= movement_reference()['units'][kind]['movement_points'])

def _tag_summary(target):
    _, _, _, _, position_of = _helpers()
    units = [{'unit_index': index, 'unit_type': unit['type'], 'owner': unit['owner'], **_facts(target, unit['type'], position_of(target, unit))} for index, unit in enumerate(target.get('units', [])) if unit['type'] in TRANSPORT_TYPES]
    witnesses = [u['unit_index'] for u in units if u['tag_qualifies'] is True]
    unknown = [u['unit_index'] for u in units if u['tag_qualifies'] is None]
    return {'status': 'matched' if witnesses else 'unresolved' if unknown else 'not_matched', 'qualifying_unit_indices': witnesses, 'unknown_unit_indices': unknown, 'transport_inventory_count': len(units), 'units': units}

def _role_inventory(target, settings, context, locks, editor_context):
    _, _, _, _, position_of = _helpers()
    from tools.map_model.unit_start_plan import _wanted_counts
    groups = defaultdict(list)
    for index, unit in enumerate(target.get('units', [])):
        position = position_of(target, unit)
        if unit['type'] not in TRANSPORT_TYPES or unit['owner'] not in context['active'] or position in locks:
            continue
        site = _site(target, unit['type'], position, unit['owner'], settings, context, existing=True)
        if not site.get('intentional_blocker_preserved'):
            groups[unit['type']].append(index)
    roles, allocations = ({}, {})
    for kind, indices in sorted(groups.items()):
        inventory = Counter((target['units'][index]['owner'] for index in indices))
        actual = {owner: inventory[owner] for owner in context['active']}
        wanted = _wanted_counts([target['units'][index] for index in indices], context['active']) if len(context['active']) == 2 and (not editor_context) and any((category_preference(settings, name) is True for name in ('Standard', 'Fog of War'))) else actual
        allocations[kind] = {'actual_active_counts': actual, 'requested_mobile_active_counts': wanted, 'odd_remainder_policy': 'retain_existing_majority_owner', 'includes_fixed_or_preserved_blockers': False}
        remaining, unassigned = (dict(wanted), set(indices))
        for owner in context['active']:
            own = sorted((index for index in unassigned if target['units'][index]['owner'] == owner), key=lambda index: (_site(target, kind, position_of(target, target['units'][index]), owner, settings, context, existing=True)['status'] != 'matched', index))
            for index in own[:remaining[owner]]:
                roles[index] = owner
                remaining[owner] -= 1
                unassigned.remove(index)
        for owner in context['active']:
            for index in sorted(unassigned)[:remaining[owner]]:
                roles[index] = owner
                unassigned.remove(index)
    return (sorted(roles), roles, allocations)

def evaluate_transport_deployments(target, settings, *, editor_locks=None):
    reason = _scope(target, settings)
    preference = settings.get('tags', {}).get('predeployed transports')
    tag = _tag_summary(target)
    result = {'policy': POLICY, 'applicable': reason is None, 'balance_certified': False, 'tag_preference': preference, 'exact_transport_tag': tag, 'issues': [], 'units': [], 'feasibility_residuals': [], 'owner_balance_residuals': [], 'mobile_owner_allocation': {}, 'active_slots': sorted((f['slot'] for f in target.get('factions', []) if f.get('active'))), 'limitations': list(_LIMITATIONS)}
    if reason:
        return dict(result, status='not_applicable', skip_reason=reason, issue_count=0)
    if preference is True and tag['status'] != 'matched':
        result['feasibility_residuals'].append({'reason': 'requested_transport_tag_has_no_known_qualifying_unit', 'transport_inventory_count': tag['transport_inventory_count']})
    if preference is False and tag['status'] != 'not_matched':
        result['feasibility_residuals'].append({'reason': 'explicit_no_transport_tag_has_qualifying_or_unknown_unit', 'qualifying_unit_indices': tag['qualifying_unit_indices'], 'unknown_unit_indices': tag['unknown_unit_indices']})
    if not tag['transport_inventory_count']:
        return dict(result, issue_count=len(result['feasibility_residuals']), status='not_matched' if result['feasibility_residuals'] else 'matched')
    _, _, _, _, position_of = _helpers()
    context, locks = (_transport_context(target), _locks(editor_locks))
    result['capture_unit_foot_origins'] = context['capture_unit_foot_origins']
    _, _, allocations = _role_inventory(target, settings, context, locks, editor_locks is not None)
    result['mobile_owner_allocation'] = allocations
    result['owner_balance_residuals'] = [dict(unit_type=kind, **allocation) for kind, allocation in allocations.items() if allocation['actual_active_counts'] != allocation['requested_mobile_active_counts']]
    for index, unit in enumerate(target.get('units', [])):
        if unit['type'] not in TRANSPORT_TYPES:
            continue
        position = position_of(target, unit)
        detail = {'unit_index': index, 'unit_type': unit['type'], 'owner': unit['owner'], 'position': {'x': unit['x'], 'y': unit['y']}, 'unit_fields_locked': position in locks, **_facts(target, unit['type'], position)}
        if unit['owner'] not in context['active']:
            result['units'].append(dict(detail, status='not_applicable', skip_reason='inactive_decorative_owner_preserved'))
            continue
        if any((not origins for origins in context['anchors'].values())):
            site = {'status': 'unresolved', 'reasons': ['missing_active_hq_or_lab_anchor']}
        else:
            site = _site(target, unit['type'], position, unit['owner'], settings, context, existing=True)
        result['units'].append(dict(detail, **site))
        if site['status'] != 'matched':
            result['issues'].append(dict(detail, **site))
    if preference is False and settings.get('tags', {}).get('immobile pre-deploy') is False and any((unit['type'] == 'transport_copter' for unit in target.get('units', []))):
        result['feasibility_residuals'].append({'reason': 'transport_copter_inventory_conflicts_with_both_no_transport_and_no_immobile_tags', 'preserved_unit_count_type_hp': True})
    result['issue_count'] = len(result['issues']) + len(result['feasibility_residuals']) + len(result['owner_balance_residuals'])
    result['status'] = 'matched' if not result['issue_count'] else 'not_matched'
    return result

def repair_transport_deployments(target, settings, *, editor_locks=None):
    from tools.map_model.ownership_placement import _assign
    result, evidence = (deepcopy(target), [])
    before = evaluate_transport_deployments(result, settings, editor_locks=editor_locks)
    if not before['applicable'] or not before['issue_count']:
        return (result, evidence)
    if not before['exact_transport_tag']['transport_inventory_count']:
        return (result, [{'kind': 'transport_deployment_unresolved', 'reason': 'no_transport_inventory', 'diagnostic': before, 'preserved_unit_count_type_hp': True}])
    _, _, _, _, position_of = _helpers()
    context, locks = (_transport_context(result), _locks(editor_locks))
    if any((not origins for origins in context['anchors'].values())):
        return (result, [{'kind': 'transport_deployment_unresolved', 'reason': 'missing_active_anchor', 'diagnostic': before}])
    indices, roles, allocations = _role_inventory(result, settings, context, locks, editor_locks is not None)
    if len(indices) > 64:
        return (result, [{'kind': 'transport_deployment_unresolved', 'reason': 'bounded_planner_transport_limit_exceeded', 'mutable_transport_count': len(indices), 'planner_limit': 64, 'diagnostic': before, 'preserved_unit_count_type_hp': True}])
    frozen = {position_of(result, unit) for index, unit in enumerate(result.get('units', [])) if index not in indices}
    size, width = (result['width'] * result['height'], result['width'])
    free = [position for position in range(size) if position not in frozen and position not in locks]
    if not indices or len(free) < len(indices):
        if before['issue_count']:
            evidence.append({'kind': 'transport_deployment_unresolved', 'reason': 'no_mutable_transport_assignment_capacity', 'diagnostic': before, 'preserved_unit_count_type_hp': True})
        return (result, evidence)
    sites = {}
    for kind in sorted({result['units'][index]['type'] for index in indices}):
        sites[kind] = {owner: {position: _site(result, kind, position, owner, settings, context) for position in free} for owner in context['active']}
    allowed_owners = {}
    fallback_scope = len(context['active']) == 2 and any((category_preference(settings, name) is True for name in ('Standard', 'Fog of War')))
    for index in indices:
        requested_owner, kind = (roles[index], result['units'][index]['type'])
        has_requested_sites = any((site['status'] == 'matched' for site in sites[kind][requested_owner].values()))
        allowed_owners[index] = [requested_owner] if has_requested_sites or not fallback_scope else [requested_owner, *(owner for owner in context['active'] if owner != requested_owner)]
    preference = settings.get('tags', {}).get('predeployed transports')
    forced_witness = None
    immutable_witness = any((index not in indices for index in before['exact_transport_tag']['qualifying_unit_indices']))
    if preference is True and (not immutable_witness):
        possible = [index for index in indices if any((site['status'] == 'matched' and site['tag_qualifies'] is True for owner in allowed_owners[index] for site in sites[result['units'][index]['type']][owner].values()))]
        if possible:
            forced_witness = min(possible, key=lambda index: (not (_site(result, result['units'][index]['type'], position_of(result, result['units'][index]), result['units'][index]['owner'], settings, context, existing=True)['status'] == 'matched' and _facts(result, result['units'][index]['type'], position_of(result, result['units'][index]))['tag_qualifies'] is True), index))
    move_weight = (size + width + result['height'] + 1) * (len(indices) + 1)
    owner_weight = move_weight * (len(indices) + 1)
    invalid_weight = (owner_weight + move_weight + size) * (len(indices) + 1)
    choices = {}
    for index in indices:
        unit, original_position = (result['units'][index], position_of(result, result['units'][index]))
        values = {}
        for position in free:
            options = []
            for owner in allowed_owners[index]:
                site = sites[unit['type']][owner][position]
                if site['status'] != 'matched' or (index == forced_witness and site['tag_qualifies'] is not True):
                    continue
                distance = abs(position % width - unit['x']) + abs(position // width - unit['y'])
                value = int(owner != unit['owner']) * owner_weight + int(position != original_position) * move_weight
                value += distance + site.get('pickup_route_cost', 0)
                options.append((value, owner))
            if options:
                values[position] = min(options)
            elif position == original_position:
                values[position] = (invalid_weight, unit['owner'])
        choices[index] = values
    candidates = sorted(set().union(*(set(values) for values in choices.values())))
    if len(candidates) < len(indices):
        evidence.append({'kind': 'transport_deployment_unresolved', 'reason': 'no_complete_collision_free_transport_assignment', 'preserved_unit_count_type_hp': True, 'diagnostic': before})
        return (result, evidence)
    assignment = _assign(candidates, {index + 1: 1 for index in indices}, lambda slot, position: choices[slot - 1].get(position, (invalid_weight * (len(indices) + 1), 0))[0])
    if any((position not in choices[slot - 1] for position, slot in assignment.items())):
        evidence.append({'kind': 'transport_deployment_unresolved', 'reason': 'no_complete_collision_free_transport_assignment', 'preserved_unit_count_type_hp': True, 'diagnostic': before})
        return (result, evidence)
    for position, slot in sorted(assignment.items(), key=lambda item: item[1]):
        index, original = (slot - 1, deepcopy(result['units'][slot - 1]))
        _, owner = choices[index][position]
        result['units'][index].update(owner=owner, x=position % width, y=position // width)
        if result['units'][index] != original:
            evidence.append({'kind': 'transport_deployment_placement', 'unit_index': index, 'unit_type': original['type'], 'source': 'explicit_own_territory_pickup_route_policy', 'before': original, 'after': deepcopy(result['units'][index]), 'owner_changed_for_feasible_role': owner != original['owner'], 'owner_fallback_for_no_free_pickup_sites': owner != roles[index], 'requested_mobile_owner_allocation': allocations[original['type']], 'preserved_unit_count_type_hp': True, 'balance_certified': False})
    after = evaluate_transport_deployments(result, settings, editor_locks=editor_locks)
    if evidence or after['issue_count']:
        evidence.append({'kind': 'transport_deployment_summary', 'before_issue_count': before['issue_count'], 'after_issue_count': after['issue_count'], 'diagnostic': after, 'balance_certified': False})
    return (result, evidence)
