from __future__ import annotations
from collections import Counter
from copy import deepcopy
from tools.map_model.deployment_symmetry import build_symmetry_context, destination
from tools.map_model.lab_ownership import _choose_orbit_assignment, _geometry_required, _locks, _orbits, _patterns
from tools.map_model.ownership_placement import property_territory_distances
KINDS = ('base', 'city', 'airport', 'port', 'com_tower')
POLICY = 'verified_player_property_owner_orbits_v1'

def _position(target, index):
    return {'x': index % target['width'], 'y': index // target['width']}

def _quota(target, settings, kind, sites):
    specified = settings.get('building_counts', {}).get(kind, {})
    observed = sum((target['owners'][index] > 0 for index in sites))
    total = len(sites)
    owned = specified.get('pre_owned')
    if owned is None:
        owned = specified['total'] - specified['neutral'] if type(specified.get('total')) is int and type(specified.get('neutral')) is int else observed
    quota = {'total': total, 'pre_owned': owned, 'neutral': total - owned if type(owned) is int else None, 'requested_reachable_counts': dict(specified), 'free_owned_budget': specified.get('pre_owned') is None and specified.get('neutral') is None}
    error = None
    if type(owned) is not int or not 0 <= owned <= total:
        error = 'property_total_pre_owned_neutral_budget_cannot_fit_existing_tiles'
    return (quota, observed, error)

def _conflicts(target, sites, action):
    conflicts = []
    for position in sites:
        owner = target['owners'][position]
        for transform in action:
            other = destination(target, position, transform['matrix'])
            expected = transform['owners'].get(owner, owner) if owner else 0
            actual = target['owners'][other]
            if actual != expected:
                conflicts.append({'position': _position(target, position), 'owner': owner, 'counterpart': _position(target, other), 'actual_counterpart_owner': actual, 'expected_counterpart_owner': expected, 'matrix': tuple(transform['matrix'])})
                break
    return conflicts

def _solve_kind(target, sites, action, active, fixed, routes, owned, *, free_budget=False):
    orbits = _orbits(target, sites, action)
    if not orbits and sites:
        return (None, 'property_tile_orbit_is_incomplete')
    penalty = max((cost for values in routes.values() for cost in values.values()), default=0) + 1
    choices = []
    for orbit in orbits:
        best = {}
        for pattern in _patterns(target, orbit, action, active, fixed):
            increment = sum((owner > 0 for owner in pattern.values()))
            cost = sum((routes[owner].get(position, penalty) for position, owner in pattern.items() if owner in active))
            cost += 0.0001 * sum((target['owners'][position] != owner for position, owner in pattern.items()))
            item = (cost, tuple(pattern.items()), pattern)
            if increment not in best or item[:2] < best[increment][:2]:
                best[increment] = item
        if not best:
            return (None, 'immutable_property_owners_conflict_with_player_permutation')
        choices.append([(increment, item[0], item[2]) for increment, item in sorted(best.items())])
    solution = _choose_orbit_assignment(choices, owned, free_budget=free_budget)
    if solution is None:
        return (None, 'owned_neutral_budget_is_incompatible_with_property_orbits_and_locks')
    return (solution[1], None)

def _plan(target, settings, editor_locks, group):
    by_kind = {kind: [index for index, token in enumerate(target.get('tiles', [])) if token == 'property:' + kind] for kind in KINDS}
    budgets = {kind: _quota(target, settings, kind, sites) for kind, sites in by_kind.items()}
    owned_present = any((observed or (type(quota['pre_owned']) is int and quota['pre_owned'] > 0) for quota, observed, _ in budgets.values()))
    requested_geometry = _geometry_required(settings, group)
    has_hqs = 'property:hq' in target.get('tiles', [])
    base = {'policy': POLICY, 'applicable': False, 'reason': None, 'geometry_certified': False, 'balance_certified': False, 'method': 'exact_property_owner_permutation_and_clear_weather_foot_cost', 'limitations': ['only_ordinary_property_owner_fields_can_change', 'hq_and_lab_anchors_preserved', 'per_player_counts_are_soft', 'foot_routes_exclude_transport_and_teleport_shortcuts', 'ownership_symmetry_does_not_certify_balance'], 'active_slots': [], 'kinds': {}, 'issues': [], 'uncertainty': [], 'transforms': []}
    if not owned_present and (not (requested_geometry and has_hqs)):
        return ({**base, 'reason': 'no_owned_ordinary_properties_require_symmetry'}, {})
    context = build_symmetry_context(target, settings, group=group)
    base['active_slots'] = context['active_slots']
    if not context['applicable']:
        if context.get('reason') == 'unsupported_grid_size' and owned_present:
            return ({**base, 'applicable': True, 'reason': context['reason'], 'issues': [{'reason': 'property_symmetry_planner_grid_bound_exceeded'}]}, {})
        return ({**base, 'reason': context.get('reason')}, {})
    if not context.get('geometric'):
        if requested_geometry and (owned_present or has_hqs):
            reason = context.get('geometry_reason') or 'missing_verified_player_property_symmetry'
            return ({**base, 'applicable': True, 'reason': reason, 'issues': [{'reason': 'verified_player_anchor_context_unavailable', 'anchor_context_reason': reason}]}, {})
        return ({**base, 'reason': context.get('geometry_reason') or 'asymmetric_ownership_uses_territory_policy'}, {})
    if not owned_present:
        return ({**base, 'reason': 'no_owned_ordinary_properties_require_symmetry'}, {})
    action, active = (context['transforms'], context['active_slots'])
    base.update(applicable=True, transforms=action, anchor_kind=context['anchor_kind'], player_orbits=context['player_orbits'], geometry_certified=True)
    assignments, need_solve = ({}, [])
    for kind, sites in by_kind.items():
        quota, observed, error = budgets[kind]
        conflicts = _conflicts(target, sites, action)
        detail = {'quota': quota, 'actual_pre_owned': observed, 'actual_neutral': len(sites) - observed, 'owner_counts': {str(owner): count for owner, count in sorted(Counter((target['owners'][position] for position in sites)).items())}, 'conflict_count': len(conflicts), 'conflicts': conflicts, 'reason': error}
        base['kinds'][kind] = detail
        if error:
            base['issues'].append({'reason': error, 'building': kind, 'quota': quota})
            continue
        if observed != quota['pre_owned']:
            base['issues'].append({'reason': 'property_owned_neutral_inventory_does_not_match_budget', 'building': kind, 'actual_pre_owned': observed, 'expected_pre_owned': quota['pre_owned']})
        if conflicts:
            base['issues'].append({'reason': 'property_owners_do_not_follow_player_permutation', 'building': kind, 'conflict_count': len(conflicts), 'conflicts': conflicts})
        if observed == quota['pre_owned'] and (not conflicts):
            assignments.update({position: target['owners'][position] for position in sites})
        else:
            need_solve.append((kind, sites, quota))
    if need_solve:
        territory = property_territory_distances(target)
        base['uncertainty'] = list(territory['uncertainty'])
        for kind, sites, quota in need_solve:
            fixed = _locks(target, sites, active, editor_locks)
            solution, error = _solve_kind(target, sites, action, active, fixed, territory['distances'], quota['pre_owned'], free_budget=quota['free_owned_budget'])
            if error:
                base['kinds'][kind]['reason'] = error
                base['issues'].append({'reason': error, 'building': kind})
                continue
            assignments.update(solution)
            selected_owned = sum((owner > 0 for owner in solution.values()))
            base['kinds'][kind]['selected_pre_owned'] = selected_owned
            base['kinds'][kind]['owned_budget_adjustment'] = selected_owned - quota['pre_owned']
            unreachable = [position for position, owner in solution.items() if owner in active and position not in territory['distances'][owner]]
            base['kinds'][kind]['suggested_owned_without_owner_foot_route'] = [_position(target, index) for index in unreachable]
            if unreachable and 'selected_property_owner_foot_route_unknown' not in base['uncertainty']:
                base['uncertainty'].append('selected_property_owner_foot_route_unknown')
    base['geometry_certified'] = not base['issues']
    return (base, assignments)

def evaluate_property_symmetry(target, settings, editor_locks=None, group=None):
    plan, _ = _plan(target, settings, editor_locks, group)
    return {**plan, 'status': 'not_applicable' if not plan['applicable'] else 'not_matched' if plan['issues'] else 'matched', 'issue_count': len(plan['issues'])}

def repair_property_symmetry(target, settings, editor_locks=None, group=None):
    result, evidence = (deepcopy(target), [])
    plan, assigned = _plan(result, settings, editor_locks, group)
    if not plan['applicable']:
        return (result, evidence)
    for kind, detail in plan['kinds'].items():
        if detail.get('owned_budget_adjustment'):
            evidence.append({'kind': 'property_owned_budget_adjustment', 'building': kind, 'before_pre_owned': detail['quota']['pre_owned'], 'after_pre_owned': detail['selected_pre_owned'], 'reason': 'nearest_feasible_free_owned_budget', 'explicit_pre_owned_and_neutral_absent': True, 'balance_certified': False})
    for position, owner in sorted(assigned.items()):
        if result['owners'][position] != owner:
            evidence.append({'kind': 'property_symmetry_assignment', 'building': result['tiles'][position].split(':', 1)[1], 'position': _position(result, position), 'before_owner': result['owners'][position], 'after_owner': owner, 'balance_certified': False})
            result['owners'][position] = owner
    after = evaluate_property_symmetry(result, settings, editor_locks, group)
    if after['issues']:
        evidence.append({'kind': 'property_symmetry_unresolved', 'diagnostic': after})
    if evidence:
        evidence.append({'kind': 'property_symmetry_summary', 'diagnostic': after, 'preserved_hq_lab_tiles_units': True, 'balance_certified': False})
    return (result, evidence)
