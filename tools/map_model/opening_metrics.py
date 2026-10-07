from __future__ import annotations
import heapq
import math
from tools.map_features.categories import category_preference
from tools.map_features.common import movement_reference, neighbors, terrain_name
_SPECIAL = ('Team Play', 'FFA Multiplay', 'Gimmick', 'Joke', 'Sprite', 'Toy-Box')
_CAPACITY_LIMITS = {'total_bases': 64, 'capture_infantry': 64}
_LIMITATIONS = ['personal_turn_numbers_not_global_turn_order', 'canonical_second_active_slot_not_stored_historical_match_turn_order', 'clear_weather_normal_infantry_movement_no_co_effects', 'enemy_predeployments_static_blockers_friendly_units_may_be_crossed', 'friendly_predeployment_cells_not_assumed_vacated_for_transit_overnight_stops', 'no_funds_limit_combat_healing_fuel_or_simultaneous_path_collision_simulation', 'one_first_capture_per_infantry_cohort_no_chain_captures_or_newly_captured_base_production', 'teleport_and_transport_routes_not_modelled', 'potential_capture_does_not_establish_ownership_or_strategic_balance']

def _position(index, width):
    return {'x': index % width, 'y': index // width}

def _terrain_costs(target):
    costs = movement_reference()['costs']['clear']
    result = []
    for token in target['tiles']:
        kind = 'property' if token.startswith('property:') else terrain_name(int(token.split(':', 1)[1]))
        result.append(None if kind == 'teleport' else costs.get(kind, {}).get('foot'))
    return result

def _routes(width, height, costs, origin, blockers, no_stop=()):
    if origin in blockers or costs[origin] is None:
        return ({}, {})
    minimum_cost = {origin: 0}
    pending = [(0, origin)]
    while pending:
        cost, index = heapq.heappop(pending)
        if cost != minimum_cost[index]:
            continue
        for x, y in neighbors(index % width, index // width, width, height):
            adjacent = y * width + x
            step = costs[adjacent]
            if adjacent in blockers or step is None or step > 3:
                continue
            candidate = cost + step
            if candidate < minimum_cost.get(adjacent, math.inf):
                minimum_cost[adjacent] = candidate
                heapq.heappush(pending, (candidate, adjacent))
    no_stop = set(no_stop) - {origin}
    earliest = {}
    state_turn = {(origin, 0): 1}
    pending = [(1, 0, origin)]
    while pending:
        turn, spent, index = heapq.heappop(pending)
        if turn != state_turn[index, spent]:
            continue
        if (turn, spent) < earliest.get(index, (math.inf, math.inf)):
            earliest[index] = (turn, spent)
        if index not in no_stop and spent:
            refreshed = (index, 0)
            if turn + 1 < state_turn.get(refreshed, math.inf):
                state_turn[refreshed] = turn + 1
                heapq.heappush(pending, (turn + 1, 0, index))
        for x, y in neighbors(index % width, index // width, width, height):
            adjacent = y * width + x
            step = costs[adjacent]
            if adjacent in blockers or step is None or spent + step > 3:
                continue
            state = (adjacent, spent + step)
            if turn < state_turn.get(state, math.inf):
                state_turn[state] = turn
                heapq.heappush(pending, (turn, spent + step, adjacent))
    return (minimum_cost, earliest)

def earliest_foot_turns(target, origin, *, owner, activation_turn=1):
    width, height = (target['width'], target['height'])
    if type(origin) is not int or not 0 <= origin < width * height:
        raise ValueError('origin must be a valid row-major cell index')
    if type(activation_turn) is not int or activation_turn < 1:
        raise ValueError('activation_turn must be a positive integer')
    blockers = {unit['y'] * width + unit['x'] for unit in target.get('units', []) if unit['owner'] != owner}
    no_stop = {unit['y'] * width + unit['x'] for unit in target.get('units', [])} - {origin}
    minimum_cost, earliest = _routes(width, height, _terrain_costs(target), origin, blockers, no_stop)
    return {'foot_costs': minimum_cost, 'arrival_turns': {index: turn + activation_turn - 1 for index, (turn, _) in earliest.items()}, 'movement_spent_on_arrival': {index: spent for index, (_, spent) in earliest.items()}}

def _matching(edges):
    chosen = {}

    def augment(source, seen):
        for base in sorted(edges[source]):
            if base in seen:
                continue
            seen.add(base)
            if base not in chosen or augment(chosen[base], seen):
                chosen[base] = source
                return True
        return False
    for source in range(len(edges)):
        augment(source, set())
    return len(chosen)

def _scope(target, settings, editor_context):
    if editor_context:
        return 'editor_context'
    active = sorted((faction['slot'] for faction in target.get('factions', []) if faction.get('active')))
    if target.get('players') != 2 or len(active) != 2 or len(set(active)) != 2:
        return 'requires_two_active_players'
    if not any((category_preference(settings, label) is True for label in ('Standard', 'Fog of War'))):
        return 'requires_explicit_standard_or_fog'
    if any((category_preference(settings, label) is True for label in _SPECIAL)):
        return 'special_category_requested'
    if any((settings.get('tags', {}).get(label) is True for label in ('1vX team play', 'immobile pre-deploy'))):
        return 'special_deployment_requested'
    if sum((token == 'property:base' for token in target['tiles'])) > _CAPACITY_LIMITS['total_bases'] or sum((unit.get('type') == 'infantry' for unit in target.get('units', []))) > _CAPACITY_LIMITS['capture_infantry']:
        return 'bounded_opening_capacity'
    if any((unit.get('type') not in {'infantry', 'black_boat'} for unit in target.get('units', []))):
        return 'other_unit_types_not_modelled'
    return None

def evaluate_opening(target, settings=None, *, horizons=(1, 2, 3, 4, 6), editor_context=False):
    settings = settings or {}
    horizons = tuple(horizons)
    if not horizons or any((type(turn) is not int or not 1 <= turn <= 12 for turn in horizons)):
        raise ValueError('horizons must contain personal turn integers between 1 and 12')
    horizons = tuple(sorted(set(horizons)))
    reason = _scope(target, settings, editor_context)
    output = {'policy': 'independent_ordinary_2p_joint_opening_v1', 'applicable': reason is None, 'balance_certified': False, 'limitations': list(_LIMITATIONS), 'horizons': list(horizons)}
    if reason:
        output.update(status='not_applicable', skip_reason=reason)
        if reason == 'bounded_opening_capacity':
            active = sorted((faction['slot'] for faction in target['factions'] if faction['active']))
            output.update(capacity_limits=dict(_CAPACITY_LIMITS), count_summary={'total_bases': sum((token == 'property:base' for token in target['tiles'])), 'capture_infantry': sum((unit.get('type') == 'infantry' for unit in target.get('units', []))), 'neutral_bases': sum((token == 'property:base' and owner == 0 for token, owner in zip(target['tiles'], target['owners']))), 'pre_owned_bases_by_owner': {str(slot): sum((token == 'property:base' and owner == slot for token, owner in zip(target['tiles'], target['owners']))) for slot in active}})
        return output
    width, height = (target['width'], target['height'])
    if len(target['tiles']) != width * height or len(target['owners']) != width * height:
        raise ValueError('opening target grid has incorrect dimensions')
    active = sorted((faction['slot'] for faction in target['factions'] if faction['active']))
    neutral = [index for index, (token, owner) in enumerate(zip(target['tiles'], target['owners'])) if token == 'property:base' and owner == 0]
    units = target.get('units', [])
    occupied = {unit['y'] * width + unit['x']: unit for unit in units}
    costs = _terrain_costs(target)
    by_owner, all_reachable = ({}, set())
    for owner in active:
        owned = [index for index, (token, actual) in enumerate(zip(target['tiles'], target['owners'])) if token == 'property:base' and actual == owner]
        infantry = [unit for unit in units if unit['type'] == 'infantry' and unit['owner'] == owner]
        blockers = {index for index, unit in occupied.items() if unit['owner'] != owner}
        sources, production_sites = ([], [])
        for base in owned:
            foot, states = _routes(width, height, costs, base, blockers, set(occupied) - {base})
            occupant = occupied.get(base)
            available = occupant is None
            vacates = False
            if occupant and occupant['owner'] == owner and (occupant['type'] == 'infantry'):
                vacates = any((index != base and index not in occupied and (turn == 1) for index, (turn, _) in states.items()))
                available = vacates
            production_sites.append({'index': base, 'position': _position(base, width), 'turn_1_production_available': available, 'predeployed_infantry_can_vacate': vacates, 'blocked_by_unit': None if available else dict(occupant)})
            sources.append({'id': f'base:{base}', 'kind': 'owned_base', 'index': base, 'position': _position(base, width), 'activation_turn': 2, 'capture_actions': 2, 'production_available': available, 'foot': foot, 'states': states})
        for number, unit in enumerate(infantry):
            index = unit['y'] * width + unit['x']
            foot, states = _routes(width, height, costs, index, blockers, set(occupied) - {index})
            actions = math.ceil(20 / max(1, math.ceil(unit['hp'])))
            sources.append({'id': f'infantry:{number}:{index}', 'kind': 'predeployed_infantry', 'index': index, 'position': _position(index, width), 'activation_turn': 1, 'capture_actions': actions, 'hp': unit['hp'], 'foot': foot, 'states': states})
        base_routes = []
        for base in neutral:
            route_list = []
            for source in sources:
                reachable = base in source['states']
                unoccupied = base not in occupied or (source['kind'] == 'predeployed_infantry' and source['index'] == base)
                active_source = source['kind'] != 'owned_base' or source['production_available']
                viable = reachable and unoccupied and active_source
                start = source['states'][base][0] + source['activation_turn'] - 1 if viable else None
                completion = start + source['capture_actions'] - 1 if start is not None else None
                route_list.append({'source': source['id'], 'source_kind': source['kind'], 'source_position': source['position'], 'foot_cost': source['foot'].get(base), 'capture_start_turn': start, 'capture_complete_turn': completion, 'route_exists': reachable, 'capture_endpoint_available': unoccupied, 'source_production_available': active_source})
                if viable:
                    all_reachable.add(base)
            starts = [route['capture_start_turn'] for route in route_list if route['capture_start_turn'] is not None]
            completes = [route['capture_complete_turn'] for route in route_list if route['capture_complete_turn'] is not None]
            base_costs = [route['foot_cost'] for route in route_list if route['source_kind'] == 'owned_base' and route['foot_cost'] is not None]
            inf_costs = [route['foot_cost'] for route in route_list if route['source_kind'] == 'predeployed_infantry' and route['foot_cost'] is not None]
            base_routes.append({'index': base, 'position': _position(base, width), 'routes': route_list, 'minimum_foot_cost_from_owned_bases': min(base_costs, default=None), 'minimum_foot_cost_from_predeployed_infantry': min(inf_costs, default=None), 'earliest_capture_start_turn': min(starts, default=None), 'earliest_capture_complete_turn': min(completes, default=None)})
        by_horizon = {}
        for horizon in horizons:
            start_targets = [site['index'] for site in base_routes if site['earliest_capture_start_turn'] is not None and site['earliest_capture_start_turn'] <= horizon]
            complete_targets = [site['index'] for site in base_routes if site['earliest_capture_complete_turn'] is not None and site['earliest_capture_complete_turn'] <= horizon]
            edges = []
            for source in sources:
                if source['kind'] == 'owned_base' and (not source['production_available']):
                    continue
                productions = range(1, horizon) if source['kind'] == 'owned_base' else (1,)
                for production in productions:
                    targets = []
                    for site in base_routes:
                        route = next((route for route in site['routes'] if route['source'] == source['id']))
                        complete = route['capture_complete_turn']
                        if complete is not None and complete + production - 1 <= horizon:
                            targets.append(site['index'])
                    edges.append(targets)
            matching = _matching(edges)
            by_horizon[str(horizon)] = {'individual_capture_start_opportunities': len(start_targets), 'individual_capture_complete_opportunities': len(complete_targets), 'distinct_first_capture_capacity': matching, 'starting_plus_first_capture_base_count': len(owned) + matching}
        by_owner[str(owner)] = {'owner': owner, 'pre_owned_base_count': len(owned), 'pre_owned_bases': [_position(index, width) for index in owned], 'turn_1_production_available_base_count': sum((site['turn_1_production_available'] for site in production_sites)), 'production_sites': production_sites, 'predeployed_infantry_count': len(infantry), 'predeployed_infantry': [dict(unit) for unit in infantry], 'turn_1_neutral_base_capture_opportunities': sum((site['earliest_capture_start_turn'] == 1 for site in base_routes)), 'neutral_base_routes': base_routes, 'by_horizon': by_horizon}
    first, second = (by_owner[str(owner)] for owner in active)
    production_gap = first['pre_owned_base_count'] - second['pre_owned_base_count']
    gaps = {str(turn): {'distinct_first_capture_capacity_p1_minus_p2': first['by_horizon'][str(turn)]['distinct_first_capture_capacity'] - second['by_horizon'][str(turn)]['distinct_first_capture_capacity'], 'starting_plus_first_capture_bases_p1_minus_p2': first['by_horizon'][str(turn)]['starting_plus_first_capture_base_count'] - second['by_horizon'][str(turn)]['starting_plus_first_capture_base_count']} for turn in horizons}
    uncertainties = []
    if 'tile:195' in target['tiles']:
        uncertainties.append('teleport_links_unknown')
    if any((unit['type'] == 'black_boat' for unit in units)):
        uncertainties.append('transport_capture_routes_not_modelled')
    if set(neutral) - all_reachable:
        uncertainties.append('neutral_bases_without_opening_foot_capture_source')
    if settings.get('tags', {}).get('island(s)') is True:
        uncertainties.append('island_map_foot_only_opening_scope')
    early = [turn for turn in horizons if 2 <= turn <= 6]
    missing_capture_sources = [owner for owner in active if not by_owner[str(owner)]['pre_owned_base_count'] and (not by_owner[str(owner)]['predeployed_infantry_count'])]
    if not any((token == 'property:base' for token in target['tiles'])):
        missing_capture_sources = []
    large = [turn for turn in early if abs(gaps[str(turn)]['starting_plus_first_capture_bases_p1_minus_p2']) >= 2]
    same_side_large = any((gaps[str(turn)]['starting_plus_first_capture_bases_p1_minus_p2'] * production_gap > 0 for turn in large))
    severe = bool(missing_capture_sources) or (abs(production_gap) >= 2 and same_side_large) or len(large) >= 2
    all_infantry = [unit for unit in units if unit['type'] == 'infantry']
    deployment = {'single_infantry': len(all_infantry) == 1, 'expected_canonical_owner': active[1], 'actual_owner': all_infantry[0]['owner'] if len(all_infantry) == 1 else None, 'canonical_p2_owner_matched': all_infantry[0]['owner'] == active[1] if len(all_infantry) == 1 else None}
    compensated = bool(production_gap and early and all((abs(gaps[str(turn)]['starting_plus_first_capture_bases_p1_minus_p2']) <= 1 for turn in early)) and any((gaps[str(turn)]['starting_plus_first_capture_bases_p1_minus_p2'] == 0 for turn in early)))
    return dict(output, status='review_required' if severe else 'no_large_disparity_detected', active_slots=active, per_owner=by_owner, neutral_base_count=len(neutral), uncertainty=uncertainties, complete_opening_simulation=False, deployment_convention=deployment, disparity={'initial_production_p1_minus_p2': production_gap, 'by_horizon': gaps, 'owners_without_production_or_capture_unit': missing_capture_sources, 'large_early_base_gap_horizons': large, 'large_disparity_screen': severe, 'initial_difference_compensated_in_first_capture_model': compensated, 'screening_rule': 'missing_production_and_capture_unit_or_initial_gap_at_least_2_with_same_side_large_joint_gap_or_persistent_2_base_gap_at_two_horizons_2_to_6'})
