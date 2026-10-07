from __future__ import annotations
from collections import Counter
from copy import deepcopy
import heapq
import math
import time
from tools.map_codec import decode_target
from tools.map_constraints import check_map
from tools.map_features.access_counts import analyze_access_counts
from tools.map_features.categories import category_preference
from tools.map_features.common import movement_reference, neighbors, terrain_name
from tools.map_model.ownership_placement import property_territory_distances, repair_property_ownership
from tools.map_model.opening_metrics import evaluate_opening
_SPECIAL = ('Joke', 'Gimmick', 'Toy-Box', 'Sprite', 'Team Play', 'FFA Multiplay', 'Heavy Naval')
_LAND = {'plain', 'road', 'wood', 'river', 'mountain', 'bridge', 'shoal'}

def _scope(target, settings):
    active = [f['slot'] for f in target['factions'] if f['active']]
    if len(active) != 2:
        return 'requires_two_active_players'
    if not (category_preference(settings, 'Standard') is True or category_preference(settings, 'Fog of War') is True or category_preference(settings, 'Standard') is None):
        return 'requires_standard_or_fog_profile'
    if any((category_preference(settings, name) is True for name in _SPECIAL)):
        return 'intentional_special_or_multiplayer_layout'
    if any((settings.get('tags', {}).get(name) is True for name in ('1vX team play', 'immobile pre-deploy', 'predeployed transports'))):
        return 'intentional_special_deployment'
    if any((token.startswith('tile:') and terrain_name(int(token.split(':')[1])) == 'teleport' for token in target['tiles'])):
        return 'teleport_links_unknown'
    base_count = sum((token == 'property:base' for token in target['tiles']))
    if base_count > 64:
        return 'production_inventory_exceeds_bounded_planner_capacity'
    if base_count * target['width'] * target['height'] > 60000:
        return 'opening_analysis_exceeds_bounded_planner_capacity'
    return None

def _distances(target, sources):
    width, height = (target['width'], target['height'])
    costs = movement_reference()['costs']['clear']
    result = {index: 0 for index in sources}
    queue = [(0, index) for index in sources]
    heapq.heapify(queue)
    while queue:
        value, index = heapq.heappop(queue)
        if value != result[index]:
            continue
        for x, y in neighbors(index % width, index // width, width, height):
            destination = y * width + x
            token = target['tiles'][destination]
            family = 'property' if token.startswith('property:') else terrain_name(int(token.split(':')[1]))
            step = costs.get(family, {}).get('foot') if family != 'teleport' else None
            if step is None:
                continue
            candidate = value + step
            if candidate < result.get(destination, math.inf):
                result[destination] = candidate
                heapq.heappush(queue, (candidate, destination))
    return result

def _metrics(target, settings, cluster_radius, opening_cost_tolerance, opening_radius, *, optimize_joint=False):
    routes = property_territory_distances(target)
    active, territories = (routes['active_slots'], routes['distances'])
    bases = [index for index, token in enumerate(target['tiles']) if token == 'property:base']
    neutral = [index for index in bases if target['owners'][index] == 0]
    profiles, conflicts, isolated, excess = ({}, 0, 0, 0)
    area = target['width'] * target['height']
    for slot in active:
        owned = [index for index in bases if target['owners'][index] == slot]
        owned_routes = {index: _distances(target, [index]) for index in owned}
        separation = []
        for index in owned:
            own_cost = territories[slot].get(index, math.inf)
            enemy_cost = min((territories[other].get(index, math.inf) for other in active if other != slot), default=math.inf)
            conflicts += int(enemy_cost < own_cost)
            if len(owned) > 1:
                nearest = min((owned_routes[index].get(other, math.inf) for other in owned if other != index))
                separation.append(None if nearest == math.inf else nearest)
                if nearest > cluster_radius:
                    isolated += 1
                    excess += (area if nearest == math.inf else nearest) - cluster_radius
        origins = owned or routes['anchors'][slot]
        opening = _distances(target, origins)
        opportunities = []
        for index in neutral:
            cost = opening.get(index)
            home = territories[slot].get(index, math.inf)
            enemy = min((territories[other].get(index, math.inf) for other in active if other != slot), default=math.inf)
            if cost is not None and home < enemy:
                opportunities.append((cost, index))
        opportunities.sort()
        profiles[slot] = {'owned_base_positions': owned, 'owned_base_count': len(owned), 'owned_base_hq_costs': [territories[slot].get(index) for index in owned], 'nearest_other_owned_base_costs': separation, 'home_neutral_base_costs': [cost for cost, _ in opportunities], 'home_neutral_base_positions': [index for _, index in opportunities]}
    first, second = active
    left, right = (profiles[first], profiles[second])
    pair_count = min(len(left['home_neutral_base_costs']), len(right['home_neutral_base_costs']))
    mismatch = abs(len(left['home_neutral_base_costs']) - len(right['home_neutral_base_costs']))
    gap = sum((max(0, abs(left['home_neutral_base_costs'][number] - right['home_neutral_base_costs'][number]) - opening_cost_tolerance) for number in range(pair_count)))
    far = 0
    if neutral:
        for profile in (left, right):
            closest = profile['home_neutral_base_costs'][0] if profile['home_neutral_base_costs'] else area
            far += max(0, closest - opening_radius)
    opening_settings = settings
    assumed_standard = not any((category_preference(settings, name) is True for name in ('Standard', 'Fog of War')))
    if assumed_standard:
        categories = settings.get('categories', {})
        categories = dict(categories) if isinstance(categories, dict) else {name: True for name in categories}
        opening_settings = dict(settings, categories=dict(categories, Standard=True))
    opening = evaluate_opening(target, opening_settings, horizons=(2, 3, 4, 6))
    disparity = opening.get('disparity', {})
    compensated = disparity.get('initial_difference_compensated_in_first_capture_model', False)
    reviewed = disparity.get('large_disparity_screen', False)
    production_gap = abs(left['owned_base_count'] - right['owned_base_count'])
    owned_total = left['owned_base_count'] + right['owned_base_count']
    avoidable_gap = 0 if compensated else max(0, production_gap - owned_total % 2)
    missing_sources = disparity.get('owners_without_production_or_capture_unit', [])
    active_joint_objective = reviewed or optimize_joint
    large_horizons = len(disparity.get('large_early_base_gap_horizons', [])) if active_joint_objective else 0
    persistent_excess = sum((max(0, abs(values['starting_plus_first_capture_bases_p1_minus_p2']) - 1) for values in disparity.get('by_horizon', {}).values())) if active_joint_objective else 0
    score = (avoidable_gap + 2 * len(missing_sources), conflicts, large_horizons, persistent_excess, mismatch if active_joint_objective else 0, gap if active_joint_objective else 0, 0)
    return {'profiles': profiles, 'score': score, 'owned_base_count_difference': production_gap, 'avoidable_owned_base_count_difference': avoidable_gap, 'owners_without_production_or_capture_unit': missing_sources, 'foreign_owned_bases': conflicts, 'isolated_owned_bases': isolated, 'neutral_opportunity_count_difference': mismatch, 'neutral_foot_cost_gap_excess': gap, 'first_home_neutral_foot_cost_excess': far, 'balance_certified': False, 'actual_owned_base_count_difference': production_gap, 'large_joint_opening_disparity': reviewed, 'joint_opening_gap_excess': persistent_excess, 'initial_difference_compensated_in_first_capture_model': compensated, 'independent_opening_disparity': disparity, 'opening_profile_assumed_standard': assumed_standard, 'independent_opening_status': opening['status'], 'independent_opening_scope_reason': opening.get('skip_reason'), 'dispersion_descriptive_only': True, 'method': 'independent_first_capture_screen_with_descriptive_foot_profiles'}

def evaluate_start_plan(target, settings=None, *, cluster_radius=23, opening_cost_tolerance=1, opening_radius=6):
    settings = settings or {}
    thresholds = {'cluster_radius_foot_cost': cluster_radius, 'opening_foot_cost_tolerance': opening_cost_tolerance, 'first_home_neutral_foot_cost_radius': opening_radius}
    reason = _scope(target, settings)
    if not any((token == 'property:base' for token in target['tiles'])):
        reason = reason or 'no_production_bases_requested_or_generated'
    if reason:
        return {'status': 'not_evaluated', 'reason': reason, 'thresholds': thresholds, 'balance_certified': False}
    routes = property_territory_distances(target)
    if any((not anchors for anchors in routes['anchors'].values())):
        return {'status': 'not_evaluated', 'reason': 'missing_active_anchor', 'thresholds': thresholds, 'balance_certified': False}
    metric = _metrics(target, settings, cluster_radius, opening_cost_tolerance, opening_radius)
    limitations = []
    if metric['independent_opening_status'] == 'not_applicable':
        limitations.append(f"capture_timing_not_evaluated:{metric['independent_opening_scope_reason']}")
    if not any((token == 'property:base' and owner in routes['active_slots'] for token, owner in zip(target['tiles'], target['owners']))):
        limitations.append('no_owned_production_hq_or_lab_foot_origin_proxy_only')
    if not any((token == 'property:base' and owner == 0 for token, owner in zip(target['tiles'], target['owners']))):
        limitations.append('no_neutral_base_expansion_opportunity_to_compare')
    return {'status': 'not_evaluated' if metric['independent_opening_status'] == 'not_applicable' else 'heuristic_issues' if any(metric['score']) else 'no_heuristic_issue', 'reason': metric['independent_opening_scope_reason'] if metric['independent_opening_status'] == 'not_applicable' else None, **metric, 'score': list(metric['score']), 'thresholds': thresholds, 'limitations': limitations, 'balance_certified': False}

def _owner_patterns(target, group, routes):
    from tools.map_model.sample import _destination
    active, anchor = (routes['active_slots'], f"property:{routes['anchor_kind']}")
    permutations = {}
    for matrix in group:
        mapping = {}
        for slot in active:
            destinations = {_destination(index, target['width'], target['height'], matrix) for index in routes['anchors'][slot]}
            owners = {target['owners'][index] for index in destinations if target['tiles'][index] == anchor}
            if len(owners) != 1 or len(destinations) != len([i for i in destinations if target['tiles'][i] == anchor]):
                return None
            mapping[slot] = next(iter(owners))
        if set(mapping.values()) != set(active):
            return None
        permutations[matrix] = mapping
    return permutations

def _patterns(orbit, target, group, routes, permutations):
    from tools.map_model.sample import _destination
    active, territory = (routes['active_slots'], routes['distances'])
    result = []
    if permutations is None:
        assignment = {}
        for index in orbit:
            ranked = sorted(((values.get(index, math.inf), slot) for slot, values in territory.items()))
            if ranked[0][0] == math.inf or ranked[0][0] == ranked[1][0]:
                return []
            assignment[index] = ranked[0][1]
        return [assignment]
    for root_owner in active:
        assignment = {}
        for matrix, mapping in permutations.items():
            destination = _destination(orbit[0], target['width'], target['height'], matrix)
            owner = mapping[root_owner]
            if destination in assignment and assignment[destination] != owner:
                break
            assignment[destination] = owner
        else:
            if set(assignment) != set(orbit) or any((index not in territory[owner] for index, owner in assignment.items())):
                continue
            if any((territory[owner][index] >= min((territory[other].get(index, math.inf) for other in active if other != owner), default=math.inf) for index, owner in assignment.items())):
                continue
            result.append(assignment)
    return result

def _paired_owners(target, settings, orbits, group, frozen, routes, permutations, max_plans=16, deadline=None):
    result = deepcopy(target)
    active = routes['active_slots']
    base_orbits = [orbit for orbit in orbits if all((target['tiles'][index] == 'property:base' for index in orbit))]
    total = sum((token == 'property:base' and owner in active for token, owner in zip(target['tiles'], target['owners'])))
    before = Counter((target['owners'][index] for orbit in base_orbits for index in orbit if target['owners'][index] in active))
    extra = max(active, key=lambda slot: (before[slot], -slot))
    desired = {slot: total // 2 + int(total % 2 and slot == extra) for slot in active}
    fixed, choices = (Counter(), [])
    for orbit in base_orbits:
        if any((target['owners'][index] not in {*active, 0} for index in orbit)):
            fixed.update((target['owners'][index] for index in orbit if target['owners'][index] in active))
            continue
        options = []
        for pattern in [{index: 0 for index in orbit}, *_patterns(orbit, target, group, routes, permutations)]:
            if any((index in frozen and target['owners'][index] != owner for index, owner in pattern.items())):
                continue
            counts = Counter((owner for owner in pattern.values() if owner in active))
            distance = sum((routes['distances'][owner][index] for index, owner in pattern.items() if owner))
            changes = sum((target['owners'][index] != owner for index, owner in pattern.items()))
            options.append((tuple((counts[slot] for slot in active)), distance, changes, pattern))
        choices.append(options)
    demand = tuple((desired[slot] - fixed[slot] for slot in active))
    if any((value < 0 for value in demand)):
        return []
    states = {(0, 0): [((0, 0), [])]}
    for options in choices:
        if deadline is not None and time.perf_counter() >= deadline:
            return []
        following = {}
        for state, alternatives in states.items():
            for cost, path in alternatives:
                for counts, distance, changes, pattern in options:
                    candidate = tuple((state[number] + counts[number] for number in range(2)))
                    if any((candidate[number] > demand[number] for number in range(2))):
                        continue
                    score = (cost[0] + changes, cost[1] + distance)
                    following.setdefault(candidate, []).append((score, [*path, pattern]))
        for state, alternatives in following.items():
            alternatives.sort(key=lambda item: item[0])
            following[state] = alternatives[:max_plans]
        states = following
    if demand not in states:
        return []
    plans = []
    for _, path in states[demand]:
        candidate = deepcopy(result)
        for pattern in path:
            for index, owner in pattern.items():
                candidate['owners'][index] = owner
        plans.append(candidate)
    return plans

def _swap_orbit(candidate, source, destination, owner_pattern):
    for frm, to in zip(source, destination):
        candidate['tiles'][frm], candidate['tiles'][to] = (candidate['tiles'][to], candidate['tiles'][frm])
        candidate['owners'][frm] = 0
        candidate['owners'][to] = owner_pattern.get(to, 0)
        if 'rendering_tiles' in candidate:
            candidate['rendering_tiles'][frm], candidate['rendering_tiles'][to] = (candidate['rendering_tiles'][to], candidate['rendering_tiles'][frm])

def repair_start_plan(target, settings, *, editor_locks=None, preserve_positions=(), group=None, max_evaluations=48, cluster_radius=23, opening_cost_tolerance=1, opening_radius=6, max_seconds=2.0):
    from tools.map_model.sample import _orbits, symmetry_group
    if type(max_evaluations) is not int or not 0 <= max_evaluations <= 256:
        raise ValueError('max_evaluations must be an integer between 0 and 256')
    if type(max_seconds) not in (int, float) or not math.isfinite(max_seconds) or (not 0 <= max_seconds <= 30):
        raise ValueError('max_seconds must be finite and between 0 and 30')
    started = time.perf_counter()
    deadline = started + max_seconds
    if any((type(value) is not int or value < 0 for value in (cluster_radius, opening_cost_tolerance, opening_radius))):
        raise ValueError('start-plan thresholds must be nonnegative integer foot costs')
    result = deepcopy(target)
    reason = _scope(result, settings)
    if not any((token == 'property:base' for token in result['tiles'])):
        reason = reason or 'no_production_bases_requested_or_generated'
    if reason:
        return (result, [{'kind': 'start_plan_skipped', 'reason': reason}])
    routes = property_territory_distances(result)
    if any((not anchors for anchors in routes['anchors'].values())):
        return (result, [{'kind': 'start_plan_skipped', 'reason': 'missing_active_anchor'}])
    width, height = (result['width'], result['height'])
    size = width * height
    frozen = set(preserve_positions)
    for index, fields in (editor_locks or {}).items():
        if {0, 1, 'tile', 'owner'}.intersection(fields):
            frozen.add(index)
    if any((type(index) is not int or not 0 <= index < size for index in frozen)):
        raise ValueError('start-plan locks require valid row-major indices')
    frozen.update((unit['y'] * width + unit['x'] for unit in result['units']))
    active = set(routes['active_slots'])
    frozen.update((index for index, (token, owner) in enumerate(zip(result['tiles'], result['owners'])) if token in {'property:hq', 'property:lab'} or owner not in {*active, 0}))
    chosen_group = symmetry_group(settings) if group is None else group
    orbits = list(_orbits(width, height, chosen_group))
    if any((not 0 <= index < size for orbit in orbits for index in orbit)):
        raise ValueError('start-plan symmetry group does not preserve map dimensions')
    thresholds = {'cluster_radius_foot_cost': cluster_radius, 'opening_foot_cost_tolerance': opening_cost_tolerance, 'first_home_neutral_foot_cost_radius': opening_radius, 'max_evaluations': max_evaluations, 'max_seconds': max_seconds, 'dispersion_and_hq_distance_descriptive_only': True, 'neutral_radius_descriptive_only': True}
    original = _metrics(result, settings, cluster_radius, opening_cost_tolerance, opening_radius)
    optimize_joint = any(original['score'])
    if optimize_joint:
        original = _metrics(result, settings, cluster_radius, opening_cost_tolerance, opening_radius, optimize_joint=True)
    try:
        baseline_payload = decode_target(result)
        baseline_report = check_map(baseline_payload, settings)
        baseline_access = analyze_access_counts(baseline_payload)['building_counts']
    except (ValueError, KeyError, TypeError, IndexError):
        return (result, [{'kind': 'start_plan_skipped', 'reason': 'invalid_repaired_candidate'}])
    unprotected = {issue['path'] for issue in [*baseline_report['violations'], *baseline_report['unresolved']]}
    evidence, evaluations = ([], 0)
    metric = original

    def accept(candidate, kind, details, candidate_metric=None):
        nonlocal result, metric, evaluations
        if candidate is None or evaluations >= max_evaluations or time.perf_counter() >= deadline:
            return False
        if candidate_metric is None:
            candidate_metric = _metrics(candidate, settings, cluster_radius, opening_cost_tolerance, opening_radius, optimize_joint=optimize_joint)
        if time.perf_counter() >= deadline:
            return False
        if candidate_metric['score'] >= metric['score']:
            return False
        evaluations += 1
        payload = decode_target(candidate)
        report = check_map(payload, settings)
        if report['status'] in {'invalid_map', 'invalid_settings'}:
            return False
        problems = {issue['path'] for issue in [*report['violations'], *report['unresolved']]}
        if problems - unprotected:
            return False
        counts = analyze_access_counts(payload)['building_counts']
        if any((counts[building]['raw_total'] != old['raw_total'] or counts[building]['pre_owned'] != old['pre_owned'] or (old['neutral'] is not None and counts[building]['neutral'] != old['neutral']) for building, old in baseline_access.items())):
            return False
        evidence.append({'kind': kind, **details, 'before_objective': list(metric['score']), 'after_objective': list(candidate_metric['score']), 'preserved_raw_terrain_inventory': True, 'preserved_building_counts_and_accessibility': True, 'preserved_matched_settings': True, 'evaluation': evaluations, 'balance_certified': False})
        result, metric = (candidate, candidate_metric)
        return True
    owned_candidate, owner_evidence = repair_property_ownership(result, settings, editor_locks=editor_locks, preserve_positions=frozen)
    accept(owned_candidate, 'start_plan_active_ownership', {'ownership_evidence': owner_evidence})
    routes = property_territory_distances(result)
    permutations = _owner_patterns(result, chosen_group, routes)
    if any(metric['score']):
        paired = _paired_owners(result, settings, orbits, chosen_group, frozen, routes, permutations, max_plans=max(1, min(max_evaluations, 16)), deadline=deadline)
        ranked = []
        for candidate in paired:
            if time.perf_counter() >= deadline:
                break
            candidate_metric = _metrics(candidate, settings, cluster_radius, opening_cost_tolerance, opening_radius, optimize_joint=optimize_joint)
            ranked.append((candidate_metric['score'], candidate, candidate_metric))
        ranked.sort(key=lambda item: item[0])
        for _, candidate, candidate_metric in ranked:
            if accept(candidate, 'start_plan_paired_production_owners', {'anchor_owner_symmetry_inferred': permutations is not None}, candidate_metric=candidate_metric):
                break

    def movable_base_orbits(owned):
        return [orbit for orbit in orbits if not frozen.intersection(orbit) and all((result['tiles'][i] == 'property:base' for i in orbit)) and (all((result['owners'][i] in active for i in orbit)) if owned else all((result['owners'][i] == 0 for i in orbit)))]

    def land_orbits():
        candidates = []
        for orbit in orbits:
            if frozen.intersection(orbit) or any((result['tiles'][i].startswith('property:') for i in orbit)):
                continue
            families = {terrain_name(int(result['tiles'][i].split(':')[1])) for i in orbit}
            if len(families) == 1 and next(iter(families)) in _LAND:
                candidates.append(orbit)
        return candidates

    def relocate(owned):
        sources = movable_base_orbits(owned)
        if not sources:
            return
        routes = property_territory_distances(result)
        permutations = _owner_patterns(result, chosen_group, routes)
        destination_candidates = land_orbits()
        candidate = deepcopy(result)
        selected, moves = (set(), [])
        current_owned = {slot: [index for index, (token, owner) in enumerate(zip(result['tiles'], result['owners'])) if token == 'property:base' and owner == slot] for slot in active}
        base_routes = {slot: _distances(result, sites or routes['anchors'][slot]) for slot, sites in current_owned.items()}
        moved_positions = {index for source in sources for index in source}
        neutral_home_counts = Counter()
        if not owned:
            for index, (token, owner) in enumerate(zip(result['tiles'], result['owners'])):
                if token != 'property:base' or owner != 0 or index in moved_positions:
                    continue
                distances = sorted(((values.get(index, math.inf), slot) for slot, values in routes['distances'].items()))
                if distances[0][0] < distances[1][0]:
                    neutral_home_counts[distances[0][1]] += 1
        sources.sort(key=lambda orbit: (-sum((routes['distances'][result['owners'][i]].get(i, size) for i in orbit)) if owned else orbit[0], orbit[0]))
        for source in sources:
            if time.perf_counter() >= deadline:
                break
            original_counts = Counter((result['owners'][index] for index in source)) if owned else None
            choices = []
            for destination in destination_candidates:
                if len(destination) != len(source) or selected.intersection(destination):
                    continue
                for pattern in _patterns(destination, result, chosen_group, routes, permutations):
                    if owned and Counter(pattern.values()) != original_counts:
                        continue
                    distances = routes['distances'] if owned else base_routes
                    values = [distances[owner].get(index, math.inf) for index, owner in pattern.items()]
                    if any((value == math.inf for value in values)):
                        continue
                    home_counts = neutral_home_counts + Counter(pattern.values())
                    count_gap = abs(home_counts[routes['active_slots'][0]] - home_counts[routes['active_slots'][1]]) if not owned else 0
                    choices.append(((count_gap, max(values), sum(values), min(destination)), destination, pattern))
            if not choices:
                continue
            _, destination, pattern = min(choices, key=lambda item: item[0])
            selected.update(destination)
            if not owned:
                neutral_home_counts.update(pattern.values())
            _swap_orbit(candidate, source, destination, pattern if owned else {})
            moves.append({'from': list(source), 'to': list(destination), 'owned': owned, 'destination_owner_pattern': pattern if owned else {index: 0 for index in destination}})
        if moves:
            accept(candidate, 'start_plan_joint_base_orbit_relocation', {'moves': moves, 'owned_production': owned})
    if metric['foreign_owned_bases'] or metric['large_joint_opening_disparity'] or metric['joint_opening_gap_excess']:
        relocate(True)
    if metric['large_joint_opening_disparity'] or metric['joint_opening_gap_excess']:
        relocate(False)
    if any(metric['score']):
        evidence.append({'kind': 'start_plan_residual', 'objective': list(metric['score']), 'profiles': metric['profiles'], 'reason': 'time_budget_exhausted' if time.perf_counter() >= deadline else 'preserved_inventory_symmetry_locks_or_bounded_search', 'evaluations': evaluations, 'thresholds': thresholds, 'balance_certified': False})
    if evidence:
        evidence.append({'kind': 'start_plan_summary', 'before': original, 'after': metric, 'thresholds': thresholds, 'evaluations': evaluations, 'elapsed_seconds': time.perf_counter() - started, 'method': 'joint_paired_production_and_foot_cost_heuristic', 'balance_certified': False})
    if not any((token == 'property:base' and owner in active for token, owner in zip(result['tiles'], result['owners']))):
        evidence.append({'kind': 'start_plan_scope_limitation', 'reason': 'no_owned_production_hq_or_lab_foot_origin_proxy_only', 'balance_certified': False})
    if not any((token == 'property:base' and owner == 0 for token, owner in zip(result['tiles'], result['owners']))):
        evidence.append({'kind': 'start_plan_scope_limitation', 'reason': 'no_neutral_base_expansion_opportunity_to_compare', 'balance_certified': False})
    return (result, evidence)
