from __future__ import annotations
from collections import Counter
from copy import deepcopy
import heapq
from itertools import permutations
import math
from tools.map_features.categories import category_preference
from tools.map_features.common import movement_reference, neighbors, terrain_name
from tools.map_model.deployment_symmetry import IDENTITY, MATRICES, _compose, build_symmetry_context, destination
from tools.map_model.editor_constraints import token_key
from tools.map_model.ownership_placement import property_territory_distances
POLICY = 'hq_or_independent_lab_seed_ownership_v1'
_SPECIAL = ('Team Play', 'FFA Multiplay', 'Gimmick', 'Joke', 'Sprite', 'Toy-Box')
_LIMITATIONS = ['only_lab_owner_fields_can_change', 'owner_counts_per_player_are_soft', 'lab_only_geometry_is_independent_of_sampled_lab_owners', 'unreachable_foot_routes_use_disclosed_manhattan_fallback', 'ownership_coherence_does_not_certify_game_balance']

def _scope(target, settings):
    active = sorted((f['slot'] for f in target.get('factions', []) if f.get('active')))
    sites = [position for position, token in enumerate(target.get('tiles', [])) if token == 'property:lab']
    if target.get('players') != len(active) or len(active) < 2 or len(set(active)) != len(active):
        return (active, sites, 'requires_matching_multiple_active_factions')
    if settings.get('tags', {}).get('1vX team play') is True or any((category_preference(settings, name) is True for name in _SPECIAL)):
        return (active, sites, 'special_or_intentionally_unequal_ownership')
    if target['width'] * target['height'] > 2500 or len(sites) > 64:
        return (active, sites, 'bounded_lab_planner_support_exceeded')
    return (active, sites, None)

def _lab_only(target, settings, active):
    hq = settings.get('building_counts', {}).get('hq', {})
    return hq.get('total') == 0 or hq.get('pre_owned') == 0 or (not any((token == 'property:hq' and owner in active for token, owner in zip(target['tiles'], target['owners']))))

def _quota(target, settings, sites):
    counts = settings.get('building_counts', {}).get('lab', {})
    total = len(sites)
    observed = sum((target['owners'][position] > 0 for position in sites))
    free = counts.get('pre_owned') is None and counts.get('neutral') is None
    owned = counts.get('pre_owned')
    if owned is None:
        owned = total - counts['neutral'] if type(counts.get('neutral')) is int else observed
    error = None
    if counts.get('total') is not None and counts['total'] != total or not 0 <= owned <= total or (counts.get('neutral') is not None and counts['neutral'] != total - owned):
        error = 'lab_total_pre_owned_neutral_counts_cannot_fit_existing_lab_tiles'
    return ({'total': total, 'pre_owned': owned, 'neutral': total - owned, 'free_owned_budget': free}, error)

def _locks(target, sites, active, editor_locks):
    return {position: target['owners'][position] for position in sites if target['owners'][position] > 0 and target['owners'][position] not in active or any((key in (editor_locks or {}).get(position, {}) for key in (1, '1', 'owner')))}

def _owner(owner, transform):
    return 0 if owner == 0 else transform['owners'].get(owner, owner)

def _geometry_required(settings, group):
    tags = settings.get('tags', {})
    return tags.get('Asymmetrical') is not True and (tags.get('Asymmetrical') is False or any((tags.get(name) is True for name in ('rotational symmetry', 'Flip symmetry', 'diagonal symmetry'))) or (group is not None and len(group) > 1))

def _available(target, settings, group):
    tags = settings.get('tags', {})
    if tags.get('Asymmetrical') is True:
        return []
    if group is not None and len(group) > 1:
        allowed = [tuple(matrix) for matrix in group]
    elif tags.get('rotational symmetry') is True:
        allowed = [IDENTITY, (-1, 0, 0, -1), (0, -1, 1, 0), (0, 1, -1, 0)]
    elif tags.get('diagonal symmetry') is True:
        allowed = [IDENTITY, (0, 1, 1, 0), (0, -1, -1, 0)]
    elif tags.get('Flip symmetry') is True:
        allowed = [IDENTITY, (-1, 0, 0, 1), (1, 0, 0, -1)]
    else:
        allowed = list(MATRICES)
    keys = [token_key(token) for token in target['tiles']]
    return [matrix for matrix in allowed if matrix in MATRICES and (not matrix[1] or target['width'] == target['height']) and all((keys[position] == keys[destination(target, position, matrix)] for position in range(len(keys))))]

def _actions(target, settings, active, lab_only, group):
    if not lab_only:
        context = build_symmetry_context(target, settings, group=group)
        return ([context['transforms']] if context.get('geometric') else [], context.get('geometry_reason'))
    available = _available(target, settings, group)
    generators = [matrix for matrix in available if matrix != IDENTITY]
    generators.sort(key=lambda matrix: (matrix != (-1, 0, 0, -1), matrix))
    actions, seen = ([], set())
    for generator in generators:
        matrices, current = ([], IDENTITY)
        while current not in matrices:
            matrices.append(current)
            current = _compose(current, generator)
        if current != IDENTITY or any((matrix not in available for matrix in matrices)):
            continue
        order = len(matrices)
        if order == 4 and len(active) == 4:
            sequences = [(active[0], *remaining) for remaining in permutations(active[1:])]
            owner_maps = [{sequence[i]: sequence[(i + 1) % 4] for i in range(4)} for sequence in sequences]
        elif order == 2:
            sequences = [active, list(reversed(active)), active[len(active) // 2:] + active[:len(active) // 2]]
            if len(active) <= 4:
                sequences = list(permutations(active))
            owner_maps = []
            for sequence in sequences:
                mapping = {owner: owner for owner in active}
                for index in range(0, len(sequence) - 1, 2):
                    first, second = sequence[index:index + 2]
                    mapping[first], mapping[second] = (second, first)
                owner_maps.append(mapping)
        else:
            continue
        for owner_map in owner_maps:
            signature = (generator, tuple(sorted(owner_map.items())))
            if signature in seen or all((owner_map[owner] == owner for owner in active)):
                continue
            seen.add(signature)
            transforms, values = ([], {owner: owner for owner in active})
            for matrix in matrices:
                transforms.append({'matrix': matrix, 'owners': dict(values)})
                values = {owner: owner_map[values[owner]] for owner in active}
            actions.append(transforms)
    return (actions[:32], None if actions else 'no_independent_gameplay_transform_can_exchange_players')

def _orbits(target, sites, action):
    unseen, result = (set(sites), [])
    while unseen:
        position = min(unseen)
        orbit = tuple(sorted({destination(target, position, transform['matrix']) for transform in action}))
        if not set(orbit).issubset(set(sites)):
            return []
        unseen -= set(orbit)
        result.append(orbit)
    return result

def _patterns(target, orbit, action, active, fixed, seeds=None):
    seeds = seeds or {}
    result, seen = ([], set())
    for initial in [0, *active, *sorted({target['owners'][position] for position in orbit if target['owners'][position] > 0 and target['owners'][position] not in active})]:
        values = {}
        for transform in action:
            position, owner = (destination(target, orbit[0], transform['matrix']), _owner(initial, transform))
            if position in values and values[position] != owner:
                values = {}
                break
            values[position] = owner
        if set(values) != set(orbit) or any((values[position] != owner for position, owner in fixed.items() if position in values)):
            continue
        if any((values[position] != owner for position, owner in seeds.items() if position in values)):
            continue
        if any((owner not in active and owner != 0 and (target['owners'][position] != owner) for position, owner in values.items())):
            continue
        signature = tuple(sorted(values.items()))
        if signature not in seen:
            seen.add(signature)
            result.append(values)
    return result

def _foot(target, origins):
    reference = movement_reference()
    values, queue = ({position: 0 for position in origins}, [(0, position) for position in origins])
    heapq.heapify(queue)
    while queue:
        cost, position = heapq.heappop(queue)
        if values[position] != cost:
            continue
        for x, y in neighbors(position % target['width'], position // target['width'], target['width'], target['height']):
            adjacent = y * target['width'] + x
            token = target['tiles'][adjacent]
            family = 'property' if token.startswith('property:') else terrain_name(int(token.split(':', 1)[1]))
            step = 1 if family == 'teleport' else reference['costs']['clear'].get(family, {}).get('foot')
            if step is None:
                continue
            candidate = cost + step
            if candidate < values.get(adjacent, math.inf):
                values[adjacent] = candidate
                heapq.heappush(queue, (candidate, adjacent))
    return values

def _manhattan(target, first, second):
    width = target['width']
    return abs(first % width - second % width) + abs(first // width - second // width)

def _seed_proposals(target, sites, action, active, fixed):
    orbits = _orbits(target, sites, action)
    patterns = [pattern for orbit in orbits for pattern in _patterns(target, orbit, action, active, fixed) if set(pattern.values()).issubset(active)]
    proposals = [({}, frozenset())]
    for _ in range(len(active)):
        completed = [seed for seed, owners in proposals if set(active).issubset(owners)]
        if completed:
            return completed[:16]
        candidates = []
        for seed, owners in proposals:
            missing = min(set(active) - set(owners))
            for pattern in patterns:
                pattern_owners = set(pattern.values())
                if missing not in pattern_owners or owners.intersection(pattern_owners) or set(seed).intersection(pattern):
                    continue
                if len(pattern_owners) == 1 and len(pattern) > 1:
                    continue
                combined = {**seed, **pattern}
                candidates.append((combined, owners | pattern_owners))

        def rank(item):
            seed, _ = item
            distance = min((_manhattan(target, a, b) for a in seed for b in seed if seed[a] != seed[b]), default=0)
            trusted = sum((position in fixed and fixed[position] == owner for position, owner in seed.items()))
            return (-trusted, -distance, tuple(sorted(seed.items())))
        candidates.sort(key=rank)
        proposals, seen = ([], set())
        for candidate in candidates:
            key = tuple(sorted(candidate[0].items()))
            if key not in seen:
                proposals.append(candidate)
                seen.add(key)
            if len(proposals) == 16:
                break
        if not proposals:
            break
    return []

def _asymmetric_seeds(target, sites, active, fixed):
    proposals = []
    for first in sites[:16]:
        seeds, occupied = ({}, set())
        for owner in active:
            trusted = [position for position, value in fixed.items() if value == owner]
            candidates = [position for position in sites if position not in occupied and fixed.get(position, owner) == owner]
            if not candidates:
                break
            if trusted:
                position = min(trusted)
            elif not seeds:
                if first not in candidates:
                    break
                position = first
            else:
                position = max(candidates, key=lambda point: (min((_manhattan(target, point, other) for other in seeds)), -point))
            seeds[position] = owner
            occupied.add(position)
        if len(set(seeds.values())) == len(active):
            proposals.append(seeds)
    return proposals[:16]

def _routes(target, active, seeds, fixed):
    origins = {owner: [position for position, value in seeds.items() if value == owner] for owner in active}
    for position, owner in fixed.items():
        if owner in origins and position not in origins[owner]:
            origins[owner].append(position)
    return ({owner: _foot(target, positions) for owner, positions in origins.items()}, origins)

def _choose_orbit_assignment(choices, owned, *, free_budget=False):
    maximum = sum((max((option[0] for option in stage)) for stage in choices)) if free_budget else owned
    previous = [math.inf] * (maximum + 1)
    previous[0] = 0.0
    history = []
    for options in choices:
        current = [math.inf] * (maximum + 1)
        selected = bytearray([255]) * (maximum + 1)
        for number, (increment, cost, _) in enumerate(options):
            for used in range(increment, maximum + 1):
                candidate = previous[used - increment] + cost
                if candidate < current[used]:
                    current[used], selected[used] = (candidate, number)
        previous = current
        history.append(selected)
    feasible = [used for used, cost in enumerate(previous) if math.isfinite(cost)]
    if not feasible or (not free_budget and owned not in feasible):
        return None
    selected_budget = min(feasible, key=lambda used: (abs(used - owned), previous[used], used)) if free_budget else owned
    assigned, remaining = ({}, selected_budget)
    for options, selected in zip(reversed(choices), reversed(history)):
        increment, _, pattern = options[selected[remaining]]
        assigned.update(pattern)
        remaining -= increment
    return (previous[selected_budget], assigned, selected_budget)

def _solve(target, sites, action, active, fixed, seeds, routes, origins, owned, *, free_budget=False):
    choices = []
    for orbit in _orbits(target, sites, action):
        options = _patterns(target, orbit, action, active, fixed, seeds)
        best = {}
        for pattern in options:
            increment = sum((owner > 0 for owner in pattern.values()))
            value = 0.0
            for position, owner in pattern.items():
                if owner in active:
                    cost = routes[owner].get(position)
                    if cost is None:
                        cost = 10000 + min((_manhattan(target, position, origin) for origin in origins[owner]), default=10000)
                    value += cost
                value += 0.0001 * (owner != target['owners'][position])
            item = (value, tuple(pattern.items()), pattern)
            if increment not in best or item[:2] < best[increment][:2]:
                best[increment] = item
        if not best:
            return None
        choices.append([(increment, item[0], item[2]) for increment, item in sorted(best.items())])
    solution = _choose_orbit_assignment(choices, owned, free_budget=free_budget)
    if not solution:
        return None
    score, owners, _ = solution
    actual_fallback = [position for position, owner in owners.items() if owner in active and position not in routes[owner]]
    return (score, owners, actual_fallback)

def _coherent(target, sites, action, active, lab_only, quota):
    counts = Counter((target['owners'][position] for position in sites))
    if sum((count for owner, count in counts.items() if owner > 0)) != quota['pre_owned'] or (lab_only and any((not counts[owner] for owner in active))):
        return False
    return all((target['owners'][destination(target, position, transform['matrix'])] == _owner(target['owners'][position], transform) for position in sites for transform in action))

def _plan(target, settings, editor_locks, group):
    active, sites, reason = _scope(target, settings)
    lab_only = _lab_only(target, settings, active)
    quota, quota_error = _quota(target, settings, sites)
    bounded = reason == 'bounded_lab_planner_support_exceeded'
    requested_sites = settings.get('building_counts', {}).get('lab', {}).get('total', 0)
    result = {'policy': POLICY, 'applicable': (reason is None or bounded) and (bool(sites) or bool(requested_sites)), 'active_slots': active, 'lab_only': lab_only, 'quota': quota, 'reason': reason or quota_error, 'geometry_certified': False, 'balance_certified': False, 'limitations': list(_LIMITATIONS)}
    if not result['applicable']:
        return (result, None)
    if bounded:
        if not lab_only and quota['pre_owned'] == 0 and (not any((target['owners'][position] for position in sites))) and (not quota_error):
            return ({**result, 'applicable': False, 'reason': 'neutral_labs_have_no_active_owner_pattern_to_repair'}, None)
        return (result, None)
    fixed = _locks(target, sites, active, editor_locks)
    actions, geometry_reason = _actions(target, settings, active, lab_only, group)
    required = _geometry_required(settings, group)
    if quota_error or (lab_only and (not quota['free_owned_budget']) and (quota['pre_owned'] - sum((owner not in active and owner > 0 for owner in fixed.values())) < len(active))):
        return ({**result, 'reason': quota_error or 'lab_only_requires_an_owned_lab_for_every_active_player'}, None)
    if lab_only:
        for action in actions:
            if _coherent(target, sites, action, active, True, quota):
                return ({**result, 'reason': None, 'geometry_certified': True, 'transforms': action, 'method': 'independent_player_exchanging_lab_geometry_existing_coherent_groups', 'seeds': {}, 'manhattan_fallback_positions': []}, {position: target['owners'][position] for position in sites})
    proposals = []
    if not lab_only:
        context = property_territory_distances(target)
        routes, origins = (context['distances'], context['anchors'])
        for action in actions or [[{'matrix': IDENTITY, 'owners': {owner: owner for owner in active}}]]:
            solved = _solve(target, sites, action, active, fixed, {}, routes, origins, quota['pre_owned'], free_budget=quota['free_owned_budget'])
            if solved:
                gap = abs(sum((owner > 0 for owner in solved[1].values())) - quota['pre_owned'])
                proposals.append(((gap, solved[0]), 0, tuple(sorted(solved[1].items())), solved, action, {}))
    else:
        choices = [(action, seeds) for action in actions for seeds in _seed_proposals(target, sites, action, active, fixed)][:64]
        if not actions:
            identity = [{'matrix': IDENTITY, 'owners': {owner: owner for owner in active}}]
            choices = [(identity, seeds) for seeds in _asymmetric_seeds(target, sites, active, fixed)]
        base_cues = [(position, owner) for position, (token, owner) in enumerate(zip(target['tiles'], target['owners'])) if token == 'property:base' and owner in active]
        unit_cues = [(unit['y'] * target['width'] + unit['x'], unit['owner']) for unit in target.get('units', []) if unit['owner'] in active and any((key in (editor_locks or {}).get(unit['y'] * target['width'] + unit['x'], {}) for key in (2, 3, 4)))]
        for action, seeds in choices:
            routes, origins = _routes(target, active, seeds, fixed)
            solved = _solve(target, sites, action, active, fixed, seeds, routes, origins, quota['pre_owned'], free_budget=quota['free_owned_budget'])
            if solved:
                cue = sum((routes[owner].get(position, 10000 + min((_manhattan(target, position, origin) for origin in origins[owner]))) for position, owner in base_cues + unit_cues))
                gap = abs(sum((owner > 0 for owner in solved[1].values())) - quota['pre_owned'])
                proposals.append(((gap, solved[0]), cue, tuple(sorted(seeds.items())), solved, action, seeds))
    if not proposals:
        return ({**result, 'reason': 'no_compatible_lab_owner_pattern_for_counts_seeds_and_locks', 'geometry_reason': geometry_reason}, None)
    _, _, _, solved, action, seeds = min(proposals, key=lambda proposal: proposal[:3])
    geometric = len(action) > 1
    selected_owned = sum((owner > 0 for owner in solved[1].values()))
    return ({**result, 'reason': 'required_player_exchanging_lab_geometry_unavailable' if required and (not geometric) else None, 'geometry_reason': geometry_reason, 'geometry_certified': geometric, 'method': 'hq_foot_territory_and_player_geometry' if not lab_only else 'independent_lab_seed_foot_regions', 'transforms': action, 'seeds': {owner: sorted((position for position, value in seeds.items() if value == owner)) for owner in active}, 'selected_pre_owned': selected_owned, 'owned_budget_adjustment': selected_owned - quota['pre_owned'], 'manhattan_fallback_positions': solved[2]}, solved[1])

def evaluate_lab_ownership(target, settings, *, editor_locks=None, group=None):
    plan, owners = _plan(target, settings, editor_locks, group)
    issues = []
    if not plan['applicable']:
        return {**plan, 'status': 'not_applicable', 'issue_count': 0, 'issues': []}
    if plan.get('reason'):
        issues.append({'reason': plan['reason']})
    if owners is not None:
        for position, owner in owners.items():
            if target['owners'][position] != owner:
                issues.append({'reason': 'lab_owner_does_not_match_player_geometry_or_bootstrap_region', 'position': {'x': position % target['width'], 'y': position // target['width']}, 'actual_owner': target['owners'][position], 'suggested_owner': owner})
    return {**plan, 'status': 'matched' if not issues else 'not_matched', 'issue_count': len(issues), 'issues': issues, 'geometry_certified': plan['geometry_certified'] and (not issues)}

def repair_lab_ownership(target, settings, *, editor_locks=None, group=None):
    result, evidence = (deepcopy(target), [])
    plan, owners = _plan(result, settings, editor_locks, group)
    if not plan['applicable']:
        return (result, evidence)
    if owners is None:
        return (result, [{'kind': 'lab_ownership_unresolved', 'reason': plan['reason'], 'diagnostic': evaluate_lab_ownership(result, settings, editor_locks=editor_locks, group=group)}])
    if plan.get('owned_budget_adjustment'):
        evidence.append({'kind': 'lab_owned_budget_adjustment', 'before_pre_owned': plan['quota']['pre_owned'], 'after_pre_owned': plan['selected_pre_owned'], 'reason': 'nearest_feasible_free_owned_budget', 'explicit_pre_owned_and_neutral_absent': True, 'balance_certified': False})
    for position, owner in sorted(owners.items()):
        if result['owners'][position] != owner:
            evidence.append({'kind': 'lab_ownership_assignment', 'position': {'x': position % result['width'], 'y': position // result['width']}, 'before_owner': result['owners'][position], 'after_owner': owner, 'lab_only': plan['lab_only'], 'source': plan['method'], 'balance_certified': False})
            result['owners'][position] = owner
    if evidence or plan.get('reason'):
        evidence.append({'kind': 'lab_ownership_summary', 'diagnostic': evaluate_lab_ownership(result, settings, editor_locks=editor_locks, group=group), 'bootstrap_seeds': plan.get('seeds', {}), 'manhattan_fallback_positions': plan.get('manhattan_fallback_positions', []), 'preserved_lab_total_pre_owned_neutral': plan['quota'] if not plan.get('owned_budget_adjustment') else False, 'selected_pre_owned': plan.get('selected_pre_owned', plan['quota']['pre_owned']), 'balance_certified': False})
    return (result, evidence)
