from __future__ import annotations
from collections import Counter, defaultdict
from copy import deepcopy
from tools.map_features.terrain_units import TRANSPORT_TYPES
POLICY = 'verified_transport_deployment_symmetry_v1'
_BOATS = {'black_boat', 'lander'}
_MAX_UNITS, _MAX_TILES, _MAX_STATES = (64, 2500, 10000)
_LIMITATIONS = ['explicit_decoder_policy_not_learned_unit_symmetry_or_balance_certificate', 'geometry_and_player_permutations_must_be_verified_by_shared_context', 'unit_hp_is_preserved_individually_and_is_not_a_symmetry_field', 'boats_component_fallback_does_not_claim_exact_coordinate_symmetry', 'no_transfers_to_unverified_players_or_noncorresponding_basins', 'bounded_exact_quota_search_with_other_types_at_their_current_cells', 'static_units_use_the_separate_corresponding_building_policy']

def _api():
    from tools.map_model.deployment_symmetry import build_symmetry_context, destination, unit_locks
    from tools.map_model.immobile_deployment import unit_mobility
    from tools.map_model.transport_deployment import _helpers, _site, _transport_context
    return (build_symmetry_context, destination, unit_locks, unit_mobility, _helpers, _site, _transport_context)

def _context(target, settings, supplied):
    build, *_ = _api()
    return supplied if supplied is not None else build(target, settings)

def _closure(state, transforms, move):
    values, pending = ({state}, [state])
    while pending:
        owner, position = pending.pop()
        for transform in transforms:
            candidate = (transform['owners'].get(owner, owner), move(position, transform['matrix']))
            if candidate not in values:
                values.add(candidate)
                pending.append(candidate)
    return tuple(sorted(values))

def _inventory(target, context):
    *_, mobility, helpers, _, _ = _api()
    _, _, _, _, position_of = helpers()
    groups, unknown = (defaultdict(list), [])
    active = set(context.get('active_slots', []))
    for index, unit in enumerate(target.get('units', [])):
        if unit.get('type') not in TRANSPORT_TYPES or unit.get('owner') not in active:
            continue
        status = mobility(target, unit)
        if status == 'unknown':
            unknown.append({'unit_index': index, 'unit_type': unit['type'], 'owner': unit['owner'], 'reason': 'unknown_transport_mobility', 'status': 'unresolved'})
        elif status == 'mobile':
            groups[unit['type'], tuple(sorted(active))].append(index)
    return (groups, unknown)

def _components(target):
    *_, helpers, _, _ = _api()
    adjacent, _, cost, _, _ = helpers()
    labels, members = ({}, {})
    for origin in range(target['width'] * target['height']):
        if origin in labels or cost(target, origin, 'lander') is None:
            continue
        label, pending, cells = (len(members), [origin], [])
        labels[origin] = label
        while pending:
            position = pending.pop()
            cells.append(position)
            for other in adjacent(target, position):
                if other not in labels and cost(target, other, 'lander') is not None:
                    labels[other] = label
                    pending.append(other)
        members[label] = tuple(sorted(cells))
    return (labels, members)

def _component_closure(target, state, context, labels, members):
    _, destination, *_ = _api()

    def move(component, matrix):
        return labels[destination(target, members[component][0], matrix)]
    return _closure(state, context['transforms'], move)

def _group_diagnostic(target, settings, indices, owners, kind, context, pickup, frozen, components):
    _, destination, _, _, helpers, site_of, _ = _api()
    _, _, _, _, position_of = helpers()
    units, issues = ([], [])
    current = {(target['units'][index]['owner'], position_of(target, target['units'][index])) for index in indices}
    inventory = {owner: sum((target['units'][index]['owner'] == owner for index in indices)) for owner in owners}
    for index in indices:
        unit, position = (target['units'][index], position_of(target, target['units'][index]))
        site = site_of(target, kind, position, unit['owner'], settings, pickup, existing=True)
        detail = {'unit_index': index, 'owner': unit['owner'], 'position': position, 'unit_fields_locked': index in frozen, 'pickup_status': site['status'], 'pickup_reasons': site['reasons']}
        units.append(detail)
        if site['status'] != 'matched':
            issues.append(dict(detail, reason='transport_pickup_role_not_matched'))
    exact_missing = sorted({other for state in current for other in _closure(state, context['transforms'], lambda position, matrix: destination(target, position, matrix)) if other not in current})
    component_missing, component_outside, component_count_conflicts = ([], [], [])
    if kind in _BOATS:
        labels, members = components
        states = {(owner, labels[position]) for owner, position in current if position in labels}
        counts = Counter(((owner, labels[position]) for owner, position in current if position in labels))
        component_outside = sorted((position for _, position in current if position not in labels))
        component_missing = sorted({other for state in states for other in _component_closure(target, state, context, labels, members) if other not in states})
        component_count_conflicts = [{'state': list(state), 'count': counts[state], 'counterpart': list(other), 'counterpart_count': counts[other]} for state in sorted(states) for other in _component_closure(target, state, context, labels, members) if counts[state] != counts[other]]
    exact = not exact_missing and len(current) == len(indices)
    corresponding = kind in _BOATS and (not component_missing) and (not component_outside) and (not component_count_conflicts)
    isolated = sorted((owner for orbit in context.get('player_orbits', []) if len(orbit) < 2 for owner in orbit))
    if isolated or len(owners) < 2:
        issues.append({'reason': 'player_has_no_verified_geometric_counterpart', 'owners': isolated or list(owners)})
    missing_owners = [owner for owner, count in inventory.items() if not count]
    if missing_owners:
        issues.append({'reason': 'transport_type_has_no_mobile_counterpart_for_active_players', 'owners': missing_owners})
    if len(set(inventory.values())) > 1:
        issues.append({'reason': 'transport_type_mobile_counts_are_not_equal_across_active_players', 'active_counts': inventory})
    if len(indices) % len(owners):
        issues.append({'reason': 'mobile_inventory_cannot_form_complete_owner_bundles', 'unit_count': len(indices), 'player_orbit_size': len(owners), 'preserved_unit_count_type_hp': True})
    if not exact and (not corresponding):
        issues.append({'reason': 'no_exact_or_corresponding_water_component_deployment', 'missing_exact_states': exact_missing, 'missing_component_states': component_missing, 'component_count_conflicts': component_count_conflicts, 'positions_outside_water_components': component_outside})
    mode = 'exact' if exact else 'corresponding_water_components' if corresponding else 'unmatched'
    return {'unit_type': kind, 'player_orbit': list(owners), 'active_counts': inventory, 'unit_indices': list(indices), 'units': units, 'mode': mode, 'exact_coordinate_symmetry': exact, 'corresponding_water_components': corresponding, 'status': 'matched' if not issues else 'not_matched', 'issues': issues, 'issue_count': len(issues)}

def evaluate_transport_symmetry(target, settings, *, editor_locks=None, symmetry_context=None):
    context = _context(target, settings, symmetry_context)
    _, _, lock_of, _, _, _, pickup_of = _api()
    result = {'policy': POLICY, 'applicable': context.get('applicable', False), 'evaluated': False, 'balance_certified': False, 'groups': [], 'issues': [], 'issue_count': 0, 'limitations': list(_LIMITATIONS)}
    if not result['applicable']:
        return dict(result, status='not_applicable', reason=context.get('reason'))
    groups, unknown = _inventory(target, context)
    count = sum(map(len, groups.values()))
    result['mobile_transport_count'] = count
    if not count and (not unknown):
        return dict(result, status='matched', evaluated=True, vacuous=True)
    if not context.get('geometric', bool(context.get('transforms'))):
        reason = context.get('geometry_reason') or 'no_verified_geometry'
        if any((settings.get('tags', {}).get(tag) is True for tag in ('Flip symmetry', 'diagonal symmetry', 'rotational symmetry'))):
            return dict(result, status='unresolved', reason=reason, issue_count=1, issues=[{'reason': 'requested_symmetry_has_no_certified_player_counterpart', 'geometry_reason': reason}])
        return dict(result, applicable=False, status='not_applicable', reason=reason)
    if count > _MAX_UNITS or target['width'] * target['height'] > _MAX_TILES:
        return dict(result, status='unresolved', issues=[{'reason': 'bounded_transport_symmetry_planner_limit_exceeded'}], issue_count=1)
    _, frozen = lock_of(target, editor_locks)
    pickup, components = (pickup_of(target), _components(target))
    result['evaluated'] = True
    result['issues'].extend(unknown)
    for (kind, owners), indices in sorted(groups.items()):
        detail = _group_diagnostic(target, settings, indices, owners, kind, context, pickup, frozen, components)
        result['groups'].append(detail)
        result['issues'].extend((dict(unit_type=kind, player_orbit=list(owners), **issue) for issue in detail['issues']))
    result['issue_count'] = len(result['issues'])
    result['status'] = 'matched' if not result['issue_count'] else 'not_matched'
    return result

def _desired_counts(target, indices, owners, frozen, *, exact):
    if len(owners) < 2 or (exact and len(indices) % len(owners)):
        return None
    fixed = Counter((target['units'][index]['owner'] for index in indices if index in frozen))
    original = Counter((target['units'][index]['owner'] for index in indices))
    wanted = {owner: len(indices) // len(owners) for owner in owners}
    if not exact:
        extras = len(indices) % len(owners)
        ordered = sorted(owners, key=lambda owner: (-fixed[owner], -original[owner], owner))
        for owner in ordered[:extras]:
            wanted[owner] += 1
    return wanted if all((fixed[owner] <= wanted[owner] for owner in owners)) else None

def _unit_cost(target, unit, owner, position, count):
    width = target['width']
    distance = abs(unit['x'] - position % width) + abs(unit['y'] - position // width)
    move_weight = (width + target['height'] + 1) * (count + 1)
    return int(unit['owner'] != owner) * move_weight * (count + 1) + int(distance > 0) * move_weight + distance

def _pair_units(target, indices, placements, frozen):
    from tools.map_model.ownership_placement import _assign
    *_, helpers, _, _ = _api()
    _, _, _, _, position_of = helpers()
    remaining, plan = (dict(placements), {})
    for index in indices:
        if index not in frozen:
            continue
        unit, position = (target['units'][index], position_of(target, target['units'][index]))
        if remaining.get(position) != unit['owner']:
            return None
        plan[index] = (unit['owner'], position)
        del remaining[position]
    mutable = [index for index in indices if index not in frozen]
    if len(remaining) != len(mutable):
        return None
    if not mutable:
        return plan
    slots = list(sorted(remaining.items()))
    assigned = _assign(mutable, {slot + 1: 1 for slot in range(len(slots))}, lambda slot, index: _unit_cost(target, target['units'][index], slots[slot - 1][1], slots[slot - 1][0], len(indices)))
    plan.update({index: (slots[slot - 1][1], slots[slot - 1][0]) for index, slot in assigned.items()})
    return plan

def _site_tables(target, settings, kind, owners, context, pickup, available):
    *_, site_of, _ = _api()
    return {owner: {position: site_of(target, kind, position, owner, settings, pickup) for position in available} for owner in owners}

def _exact_plan(target, indices, owners, kind, context, frozen, available, sites, require_witness):
    _, destination, _, _, helpers, _, _ = _api()
    _, _, _, _, position_of = helpers()
    wanted = _desired_counts(target, indices, owners, frozen, exact=True)
    if wanted is None:
        return (None, 'inventory_or_fixed_owner_counts_cannot_form_exact_bundles')
    fixed_positions = {position_of(target, target['units'][index]): target['units'][index]['owner'] for index in indices if index in frozen}
    current = {(target['units'][index]['owner'], position_of(target, target['units'][index])) for index in indices}
    available = set(available)
    options = {}
    for owner in owners:
        for position in sorted(available):
            if sites[owner][position]['status'] != 'matched':
                continue
            orbit = _closure((owner, position), context['transforms'], lambda p, matrix: destination(target, p, matrix))
            positions = tuple(sorted((p for _, p in orbit)))
            if len(set(positions)) != len(orbit) or not set(positions) <= available:
                continue
            if any((o not in sites or sites[o][p]['status'] != 'matched' or (p in fixed_positions and fixed_positions[p] != o) for o, p in orbit)):
                continue
            capacities = tuple((sum((o == owner for o, _ in orbit)) for owner in owners))
            if any((count > wanted[owner] for owner, count in zip(owners, capacities))):
                continue
            cost = sum(((0 if (o, p) in current else 1000000) + sites[o][p].get('pickup_route_cost', 0) + min((_unit_cost(target, target['units'][i], o, p, len(indices)) for i in indices), default=0) for o, p in orbit))
            options[orbit] = (positions, capacities, cost)
    forced = set()
    for position, owner in fixed_positions.items():
        orbit = _closure((owner, position), context['transforms'], lambda p, matrix: destination(target, p, matrix))
        if orbit not in options:
            return (None, 'fixed_transport_has_no_complete_legal_coordinate_orbit')
        forced.add(orbit)
    placements, remaining = ({}, dict(wanted))
    for orbit in sorted(forced):
        for owner, position in orbit:
            if position in placements and placements[position] != owner:
                return (None, 'fixed_transport_orbits_conflict')
            if position not in placements:
                remaining[owner] -= 1
                placements[position] = owner
    if any((value < 0 for value in remaining.values())):
        return (None, 'fixed_coordinate_orbits_exceed_unit_inventory')
    geometry = defaultdict(list)
    for orbit, (positions, capacities, cost) in options.items():
        if set(positions).intersection(placements):
            continue
        geometry[positions].append((orbit, capacities, cost))
    goal = tuple((remaining[owner] for owner in owners))
    forced_witness = any((sites[owner][position].get('tag_qualifies') is True for position, owner in placements.items()))
    states = {(tuple((0 for _ in owners)), forced_witness): (0, ())}
    for positions, choices in sorted(geometry.items()):
        following = dict(states)
        for (used, witnessed), (total, chosen) in states.items():
            for orbit, capacities, cost in choices:
                next_used = tuple((a + b for a, b in zip(used, capacities)))
                if any((a > b for a, b in zip(next_used, goal))):
                    continue
                candidate = (total + cost, (*chosen, orbit))
                next_witness = witnessed or any((sites[o][p].get('tag_qualifies') is True for o, p in orbit))
                key = (next_used, next_witness)
                if key not in following or candidate[0] < following[key][0]:
                    following[key] = candidate
        if len(following) > _MAX_STATES:
            return (None, 'bounded_exact_orbit_search_state_limit_exceeded')
        states = following
    eligible = [key for key in states if key[0] == goal and (key[1] or not require_witness)]
    if not eligible:
        return (None, 'no_complete_exact_orbit_assignment_with_required_witness_and_current_occupancy')
    selected = min(eligible, key=lambda key: states[key][0])
    for orbit in states[selected][1]:
        placements.update({position: owner for owner, position in orbit})
    return (_pair_units(target, indices, placements, frozen), None)

def _basin_assignment(target, indices, state_quotas, frozen, available, sites, labels, require_witness):
    from tools.map_model.ownership_placement import _assign
    *_, helpers, _, _ = _api()
    _, _, _, _, position_of = helpers()
    fixed = {position_of(target, target['units'][index]): target['units'][index]['owner'] for index in indices if index in frozen}
    if any(((owner, labels.get(position)) not in state_quotas for position, owner in fixed.items())):
        return None
    remaining = dict(state_quotas)
    for position, owner in fixed.items():
        remaining[owner, labels[position]] -= 1
    if any((value < 0 for value in remaining.values())):
        return None
    free = [p for p in available if p not in fixed and p in labels]
    mutable = [i for i in indices if i not in frozen]

    def valid(owner, component, position, qualifying=False):
        return labels.get(position) == component and sites[owner][position]['status'] == 'matched' and (not qualifying or sites[owner][position].get('tag_qualifies') is True)
    sentinel = 10 ** 15

    def preference(owner, position):
        return min((_unit_cost(target, target['units'][index], owner, position, len(indices)) for index in mutable), default=0)
    if sum(remaining.values()) > len(free):
        return None
    fixed_witness = any((sites[owner][position].get('tag_qualifies') is True for position, owner in fixed.items()))
    states = sorted((state for state, count in remaining.items() if count))
    witnesses = [None]
    if require_witness and (not fixed_witness):
        witnesses = [state for state in states if any((valid(*state, position, qualifying=True) for position in free))]
        witnesses.sort(key=lambda state: min((preference(state[0], position) for position in free if valid(*state, position, qualifying=True))))
        witnesses = witnesses[:16]
    best = None
    for witness_state in witnesses:
        slots = []
        for state in states:
            count = remaining[state]
            if state == witness_state:
                slots.append((*state, True, 1))
                count -= 1
            if count:
                slots.append((*state, False, count))
        selected = _assign(free, {slot + 1: values[3] for slot, values in enumerate(slots)}, lambda slot, p: preference(slots[slot - 1][0], p) if valid(slots[slot - 1][0], slots[slot - 1][1], p, slots[slot - 1][2]) else sentinel)
        if any((not valid(slots[slot - 1][0], slots[slot - 1][1], p, slots[slot - 1][2]) for p, slot in selected.items())):
            continue
        placements = dict(fixed)
        placements.update({position: slots[slot - 1][0] for position, slot in selected.items()})
        plan = _pair_units(target, indices, placements, frozen)
        if plan is None:
            continue
        cost = sum((_unit_cost(target, target['units'][index], owner, position, len(indices)) for index, (owner, position) in plan.items()))
        if best is None or cost < best[0]:
            best = (cost, plan)
    return best[1] if best else None

def _component_plan(target, indices, owners, kind, context, frozen, available, sites, components, require_witness):
    *_, helpers, _, _ = _api()
    _, _, _, _, position_of = helpers()
    wanted = _desired_counts(target, indices, owners, frozen, exact=True)
    if wanted is None:
        return (None, 'fixed_owner_inventory_cannot_fit_corresponding_basins')
    labels, members = components
    orbits = set()
    for owner in owners:
        for component in members:
            orbit = _component_closure(target, (owner, component), context, labels, members)
            if any((o not in owners or not any((p in available and sites[o][p]['status'] == 'matched' for p in members[c])) for o, c in orbit)):
                continue
            orbits.add(orbit)
    fixed_counts = Counter()
    for index in indices:
        if index not in frozen:
            continue
        unit, position = (target['units'][index], position_of(target, target['units'][index]))
        if position not in labels:
            return (None, 'fixed_boat_is_outside_a_certified_water_component')
        orbit = _component_closure(target, (unit['owner'], labels[position]), context, labels, members)
        if orbit not in orbits:
            return (None, 'fixed_boat_has_no_usable_corresponding_water_component')
        fixed_counts[unit['owner'], labels[position]] += 1
    actual = Counter()
    for index in indices:
        unit, position = (target['units'][index], position_of(target, target['units'][index]))
        if position in labels:
            actual[unit['owner'], labels[position]] += 1
    goal = tuple((wanted[owner] for owner in owners))
    fixed_positions = {position_of(target, target['units'][index]): target['units'][index]['owner'] for index in indices if index in frozen}
    fixed_witness = any((sites[owner][position].get('tag_qualifies') is True for position, owner in fixed_positions.items()))
    states = {(tuple((0 for _ in owners)), fixed_witness): (0, ())}
    for orbit in sorted(orbits):
        coefficients = tuple((sum((o == owner for o, _ in orbit)) for owner in owners))
        minimum = max((fixed_counts[state] for state in orbit), default=0)
        maximum = min((wanted[owner] // coefficient for owner, coefficient in zip(owners, coefficients) if coefficient))
        for owner, component in orbit:
            capacity = sum((p in available and sites[owner][p]['status'] == 'matched' for p in members[component]))
            maximum = min(maximum, capacity)
        can_witness = any((p in available and (p not in fixed_positions or fixed_positions[p] == owner) and (sites[owner][p]['status'] == 'matched') and (sites[owner][p].get('tag_qualifies') is True) for owner, component in orbit for p in members[component]))
        following = {}
        for (used, witnessed), (total, chosen) in states.items():
            for multiplicity in range(minimum, maximum + 1):
                next_used = tuple((value + multiplicity * coefficient for value, coefficient in zip(used, coefficients)))
                if any((value > quota for value, quota in zip(next_used, goal))):
                    continue
                cost = sum((abs(actual[state] - multiplicity) for state in orbit))
                candidate = (total + cost, (*chosen, (orbit, multiplicity)))
                key = (next_used, witnessed or bool(multiplicity and can_witness))
                if key not in following or candidate[0] < following[key][0]:
                    following[key] = candidate
        if len(following) > _MAX_STATES:
            return (None, 'bounded_component_quota_search_state_limit_exceeded')
        states = following
    eligible = [key for key in states if key[0] == goal and (key[1] or not require_witness)]
    if not eligible:
        return (None, 'no_complete_corresponding_component_count_assignment')
    selected = min(eligible, key=lambda key: states[key][0])
    state_quotas = {state: multiplicity for orbit, multiplicity in states[selected][1] if multiplicity for state in orbit}
    plan = _basin_assignment(target, indices, state_quotas, frozen, available, sites, labels, require_witness)
    return (plan, None) if plan is not None else (None, 'no_bounded_corresponding_basin_position_assignment_with_current_occupancy')

def repair_transport_symmetry(target, settings, *, editor_locks=None, symmetry_context=None):
    result, evidence = (deepcopy(target), [])
    context = _context(result, settings, symmetry_context)
    before = evaluate_transport_symmetry(result, settings, editor_locks=editor_locks, symmetry_context=context)
    if not before['applicable'] or not before['evaluated'] or before.get('vacuous'):
        return (result, evidence)
    _, _, lock_of, _, helpers, _, pickup_of = _api()
    _, _, _, _, position_of = helpers()
    blocked, frozen = lock_of(result, editor_locks)
    groups, _ = _inventory(result, context)
    pickup, components = (pickup_of(result), _components(result))
    for (kind, owners), indices in sorted(groups.items()):
        isolated = [owner for orbit in context.get('player_orbits', []) if len(orbit) < 2 for owner in orbit]
        if len(owners) < 2 or isolated:
            evidence.append({'kind': 'transport_symmetry_unresolved', 'unit_type': kind, 'reason': 'player_has_no_verified_geometric_counterpart', 'player_orbit': list(owners)})
            continue
        other_positions = {position_of(result, unit) for index, unit in enumerate(result.get('units', [])) if index not in indices}
        own_fixed = {position_of(result, result['units'][index]) for index in indices if index in frozen}
        available = [p for p in range(result['width'] * result['height']) if p not in other_positions and (p not in blocked or p in own_fixed)]
        sites = _site_tables(result, settings, kind, owners, context, pickup, available)
        from tools.map_model.transport_deployment import _facts
        require_witness = settings.get('tags', {}).get('predeployed transports') is True and (not any((index not in indices and unit.get('type') in TRANSPORT_TYPES and (_facts(result, unit['type'], position_of(result, unit)).get('tag_qualifies') is True) for index, unit in enumerate(result.get('units', [])))))
        plan, exact_reason = _exact_plan(result, indices, owners, kind, context, frozen, available, sites, require_witness)
        mode, component_reason = ('exact', None)
        if plan is None and kind in _BOATS:
            plan, component_reason = _component_plan(result, indices, owners, kind, context, frozen, available, sites, components, require_witness)
            mode = 'corresponding_water_components'
        if plan is None:
            evidence.append({'kind': 'transport_symmetry_unresolved', 'unit_type': kind, 'player_orbit': list(owners), 'reason': exact_reason, 'component_reason': component_reason, 'preserved_unit_count_type_hp': True})
            continue
        for index, (owner, position) in sorted(plan.items()):
            original = deepcopy(result['units'][index])
            result['units'][index].update(owner=owner, x=position % result['width'], y=position // result['width'])
            if result['units'][index] != original:
                evidence.append({'kind': 'transport_symmetry_placement', 'unit_index': index, 'unit_type': kind, 'mode': mode, 'before': original, 'after': deepcopy(result['units'][index]), 'player_orbit': list(owners), 'preserved_unit_count_type_hp': True, 'source': 'verified_geometry_and_pickup_decoder_policy', 'balance_certified': False})
        if mode != 'exact':
            evidence.append({'kind': 'transport_symmetry_component_fallback', 'unit_type': kind, 'reason_exact_unavailable': exact_reason, 'player_orbit': list(owners), 'exact_coordinate_symmetry': False, 'balance_certified': False})
    after = evaluate_transport_symmetry(result, settings, editor_locks=editor_locks, symmetry_context=context)
    if evidence or after['issue_count']:
        evidence.append({'kind': 'transport_symmetry_summary', 'before_issue_count': before['issue_count'], 'after_issue_count': after['issue_count'], 'diagnostic': after, 'balance_certified': False})
    return (result, evidence)
