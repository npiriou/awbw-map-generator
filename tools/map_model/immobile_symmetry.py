from __future__ import annotations
from collections import Counter, defaultdict
from copy import deepcopy
from tools.map_model.immobile_deployment import _capture_transport, _context, _kind, _position, _role, unit_mobility
POLICY = 'equivalent_building_static_deployment_groups_v1'
_ROLES = {'own_capture_guard', 'foreign_production_blocker', 'neutral_production_blocker', 'neutral_capture_blocker'}
_TYPE_PRIORITY = {'black_boat': 0, 'lander': 1, 'battleship': 2, 'carrier': 3, 'cruiser': 4, 'sub': 5, 'infantry': 99}
_LIMITATIONS = ['exact_unit_type_count_hp_and_terrain_preserved', 'asymmetric_building_role_equivalence_is_not_geometric_symmetry', 'blocker_advantage_combat_effectiveness_and_first_turn_compensation_not_certified']

def _shared(target, settings, editor_locks, symmetry_context):
    from tools.map_model.deployment_symmetry import build_symmetry_context, unit_locks
    context = symmetry_context if symmetry_context is not None else build_symmetry_context(target, settings)
    blocked, frozen = unit_locks(target, editor_locks)
    return (context, set(blocked), set(frozen))

def _profile(target, position, owner, role):
    building_owner = target['owners'][position]
    relation = 'neutral' if building_owner == 0 else 'own' if building_owner == owner else 'foreign'
    return (_kind(target['tiles'][position]), role.get('role'), relation)

def _site(target, position, owner, role_context):
    kind = _kind(target['tiles'][position])
    if kind is None:
        return {'status': 'not_matched', 'reasons': ['strict_static_deployment_requires_a_building']}
    role = _role(target, position, owner, role_context)
    if role['status'] != 'matched' or role['role'] not in _ROLES:
        return {**role, 'status': 'unresolved' if role['status'] == 'unresolved' else 'not_matched'}
    return {**role, 'building_kind': kind, 'profile': _profile(target, position, owner, role)}

def _groups(target, active):
    result = defaultdict(list)
    for index, unit in enumerate(target.get('units', [])):
        if unit.get('owner') in active and unit_mobility(target, unit) == 'immobile':
            result[unit['type']].append(index)
    return result

def evaluate_immobile_symmetry(target, settings, *, editor_locks=None, symmetry_context=None):
    context, _, frozen = _shared(target, settings, editor_locks, symmetry_context)
    requested = settings.get('tags', {}).get('immobile pre-deploy')
    result = {'policy': POLICY, 'applicable': context.get('applicable', False) and requested is not False, 'geometric': bool(context.get('geometric')), 'geometry_certified': False, 'equivalence_method': 'verified_player_terrain_transform' if context.get('geometric') else 'building_role_equivalence_only', 'active_slots': list(context.get('active_slots', [])), 'issues': [], 'units': [], 'groups': [], 'limitations': list(_LIMITATIONS), 'balance_certified': False}
    if not result['applicable']:
        return {**result, 'status': 'not_applicable', 'reason': 'immobile_requested_false' if requested is False else context.get('reason'), 'issue_count': 0}
    active, role_context = (result['active_slots'], _context(target, settings))
    groups = _groups(target, active)
    if not groups:
        if requested is True:
            result['issues'].append({'reason': 'no_active_equivalent_building_static_group'})
        else:
            return {**result, 'applicable': False, 'status': 'not_applicable', 'reason': 'no_active_immobile_units', 'issue_count': 0}
    from tools.map_model.deployment_symmetry import destination
    for kind, indices in sorted(groups.items()):
        profiles, members = (defaultdict(Counter), {})
        for index in indices:
            unit, position = (target['units'][index], _position(target, target['units'][index]))
            role = _site(target, position, unit['owner'], role_context)
            detail = {'unit_index': index, 'unit_type': kind, 'owner': unit['owner'], 'position': {'x': unit['x'], 'y': unit['y']}, 'fixed': index in frozen, **role}
            result['units'].append(detail)
            members[position, unit['owner']] = detail
            if role['status'] != 'matched':
                result['issues'].append({'unit_index': index, 'unit_type': kind, 'reason': 'stationary_unit_has_no_supported_building_role', 'reasons': role.get('reasons', []), 'fixed': index in frozen})
            else:
                profiles[role['profile']][unit['owner']] += 1
        for profile, counts in profiles.items():
            inventory = {slot: counts[slot] for slot in active}
            affected = Counter((detail.get('affected_player') for detail in members.values() if detail.get('profile') == profile))
            affected_counts = {slot: affected[slot] for slot in active}
            summary = {'unit_type': kind, 'building_kind': profile[0], 'role': profile[1], 'property_relation': profile[2], 'per_player_counts': inventory, 'affected_player_counts': affected_counts, 'geometry_comparison_available': result['geometric']}
            result['groups'].append(summary)
            if len(set(inventory.values())) != 1:
                result['issues'].append({**summary, 'reason': 'missing_equivalent_static_building_role_for_each_player'})
            if affected_counts != inventory:
                result['issues'].append({**summary, 'reason': 'static_blocker_affected_player_roles_are_not_equivalent'})
        if context.get('geometric'):
            seen = set()
            for (position, owner), detail in members.items():
                for transform in context.get('transforms', []):
                    counterpart = (destination(target, position, transform['matrix']), transform['owners'][owner])
                    other = members.get(counterpart)
                    expected_affected = transform['owners'].get(detail.get('affected_player'))
                    signature = (tuple(sorted(((position, owner), counterpart))), expected_affected)
                    if signature in seen:
                        continue
                    seen.add(signature)
                    if other is None or detail.get('profile') != other.get('profile') or other.get('affected_player') != expected_affected:
                        result['issues'].append({'unit_type': kind, 'unit_index': detail['unit_index'], 'reason': 'missing_transformed_static_building_counterpart', 'counterpart_owner': counterpart[1], 'counterpart_position': {'x': counterpart[0] % target['width'], 'y': counterpart[0] // target['width']}, 'matrix': tuple(transform['matrix'])})
    result.update(issue_count=len(result['issues']), status='matched' if not result['issues'] else 'not_matched', geometry_certified=bool(context.get('geometric')) and (not result['issues']), immobile_unit_indices=sorted((index for indices in groups.values() for index in indices)), geometry_reason=context.get('geometry_reason'), uncertainty=role_context['uncertainty'])
    return result

def _protected_transport(target, settings, static_groups):
    if settings.get('tags', {}).get('predeployed transports') is not True:
        return set()
    witnesses = [index for index, unit in enumerate(target.get('units', [])) if _capture_transport(target, unit)]
    if not witnesses:
        return set()
    chosen = min(witnesses, key=lambda index: (target['units'][index]['type'] in static_groups, index))
    return {chosen}

def _options(target, kind, active, role_context, blocked, frozen, pool):
    occupied = {_position(target, unit) for index, unit in enumerate(target['units']) if index not in pool}
    fixed = {_position(target, target['units'][index]): target['units'][index]['owner'] for index in frozen & set(pool)}
    options = {}
    for position, token in enumerate(target['tiles']):
        if not _kind(token) or position in occupied or (position in blocked and position not in fixed):
            continue
        probe = {'type': kind, 'x': position % target['width'], 'y': position // target['width']}
        if unit_mobility(target, probe) != 'immobile':
            continue
        for owner in active:
            if position in fixed and owner != fixed[position]:
                continue
            role = _site(target, position, owner, role_context)
            if role['status'] == 'matched':
                options[position, owner] = role
    return (options, fixed)

def _bundles(target, options, context, current):
    from tools.map_model.deployment_symmetry import destination
    bundles = {}
    if context.get('geometric'):
        for (position, owner), role in options.items():
            members = frozenset(((destination(target, position, transform['matrix']), transform['owners'][owner]) for transform in context['transforms']))
            if len({site for site, _ in members}) != len(members) or any((member not in options for member in members)):
                continue
            if any((options[member]['profile'] != role['profile'] for member in members)):
                continue
            if any((options[destination(target, position, transform['matrix']), transform['owners'][owner]]['affected_player'] != transform['owners'][role['affected_player']] for transform in context['transforms'])):
                continue
            bundles[members] = role['profile']
    else:
        by_profile = defaultdict(lambda: defaultdict(list))
        for member, role in options.items():
            by_profile[role['profile']][member[1]].append(member)
        active = context['active_slots']
        for profile, by_owner in by_profile.items():
            partial = [()]
            for owner in active:
                choices = sorted(by_owner[owner], key=lambda member: (member not in current, options[member].get('role_priority', 9), member[0]))[:32]
                candidates = [(*previous, member) for previous in partial for member in choices if member[0] not in {site for site, _ in previous} and options[member]['affected_player'] not in {options[item]['affected_player'] for item in previous}]
                candidates.sort(key=lambda members: (-sum((member in current for member in members)), sum((options[member].get('role_priority', 9) for member in members)), tuple(members)))
                partial = candidates[:64]
                if not partial:
                    break
            for members in partial:
                if len(members) == len(active):
                    bundles[frozenset(members)] = profile
    return sorted(bundles.items(), key=lambda item: (-len(item[0] & current), sum((options[member].get('role_priority', 9) for member in item[0])), len(item[0]), tuple(sorted(item[0]))))[:128]

def _equivalent(members, options, active):
    profiles, affected = (defaultdict(Counter), defaultdict(Counter))
    for member in members:
        role = options[member]
        profiles[role['profile']][member[1]] += 1
        affected[role['profile']][role['affected_player']] += 1
    return bool(profiles) and all((len({counts[owner] for owner in active}) == 1 and all((affected[profile][owner] == counts[owner] for owner in active)) for profile, counts in profiles.items()))

def _portfolio(options, bundles, active, minimum, available, required, current):
    states = [frozenset()]
    for _ in range(available + 1):
        completed = [members for members in states if len(members) >= minimum and required.issubset(members) and _equivalent(members, options, active)]
        if completed:
            return min(completed, key=lambda members: (len(members) - minimum, -len(members & current), sum((options[member].get('role_priority', 9) for member in members)), tuple(sorted(members))))
        candidates, seen = ([], set())
        for members in states:
            occupied = {position for position, _ in members}
            for bundle, _ in bundles:
                if len(members) + len(bundle) > available or occupied.intersection((position for position, _ in bundle)):
                    continue
                combined = members | bundle
                if combined not in seen:
                    seen.add(combined)
                    candidates.append(combined)
        if not candidates:
            break

        def priority(members):
            counts = defaultdict(Counter)
            for member in members:
                counts[options[member]['profile']][member[1]] += 1
            unequal = sum((max((counts[owner] for owner in active)) - min((counts[owner] for owner in active)) for counts in counts.values()))
            return (-len(members & required), unequal, max(0, len(members) - minimum), -len(members & current), sum((options[member].get('role_priority', 9) for member in members)), tuple(sorted(members)))
        candidates.sort(key=priority)
        states = candidates[:48]
    return None

def _assign_units(target, members, indices, pool, frozen):
    chosen, assignments = (set(indices), {})
    remaining = set(members)
    for index in sorted(frozen & chosen):
        member = (_position(target, target['units'][index]), target['units'][index]['owner'])
        if member not in remaining:
            return None
        assignments[index] = member
        remaining.remove(member)
    for index in sorted(chosen - assignments.keys()):
        member = (_position(target, target['units'][index]), target['units'][index]['owner'])
        if member in remaining:
            assignments[index] = member
            remaining.remove(member)
    pending = chosen - assignments.keys()
    needed = len(members) - len(chosen)
    while needed:
        wanted = Counter((owner for _, owner in remaining))
        present = Counter((target['units'][index]['owner'] for index in pending))
        candidates = set(pool) - chosen
        if not candidates:
            return None
        index = min(candidates, key=lambda index: (present[target['units'][index]['owner']] >= wanted[target['units'][index]['owner']], index))
        chosen.add(index)
        pending.add(index)
        needed -= 1
    for member in sorted(remaining, key=lambda member: (member[1], member[0])):
        position, owner = member
        index = min(pending, key=lambda index: (target['units'][index]['owner'] != owner, abs(target['units'][index]['x'] - position % target['width']) + abs(target['units'][index]['y'] - position // target['width']), index))
        assignments[index] = member
        pending.remove(index)
    return assignments

def _demote_free_transport_singletons(target, settings, kind, indices, editor_locks, context, blocked, frozen):
    if settings.get('tags', {}).get('immobile pre-deploy') is True or settings.get('tags', {}).get('predeployed transports') is not True:
        return None
    if kind not in {'black_boat', 'lander', 'transport_copter'}:
        return None
    diagnostic = evaluate_immobile_symmetry(target, settings, editor_locks=editor_locks, symmetry_context=context)
    details = {(detail['position']['y'] * target['width'] + detail['position']['x'], detail['owner']): detail for detail in diagnostic['units'] if detail['unit_type'] == kind}
    paired = set()
    if context.get('geometric'):
        from tools.map_model.deployment_symmetry import destination
        for (position, owner), detail in details.items():
            if detail['status'] != 'matched':
                continue
            if all(((other := details.get((destination(target, position, transform['matrix']), transform['owners'][owner]))) is not None and other.get('profile') == detail.get('profile') and (other.get('affected_player') == transform['owners'].get(detail.get('affected_player'))) for transform in context['transforms'])):
                paired.add(detail['unit_index'])
    else:
        profiles = defaultdict(lambda: defaultdict(list))
        for detail in details.values():
            if detail['status'] == 'matched':
                profiles[detail['profile']][detail['owner']].append(detail)
        for by_owner in profiles.values():
            count = min((len(by_owner[owner]) for owner in context['active_slots']))
            candidates = [detail for owner in context['active_slots'] for detail in sorted(by_owner[owner], key=lambda detail: (detail['unit_index'] not in frozen, detail['unit_index']))[:count]]
            affected = Counter((detail['affected_player'] for detail in candidates))
            if all((affected[owner] == count for owner in context['active_slots'])):
                paired.update((detail['unit_index'] for detail in candidates))
    unpaired = set(indices) - paired
    if not unpaired or unpaired & frozen:
        return None
    locks = {position: dict(fields) for position, fields in (editor_locks or {}).items()}
    for position in blocked:
        locks.setdefault(position, {}).setdefault(2, 0)
    for index, unit in enumerate(target['units']):
        if index not in unpaired:
            locks.setdefault(_position(target, unit), {}).setdefault(2, 1)
    from tools.map_model.immobile_deployment import repair_immobile_deployments
    temporary_settings = {**settings, 'tags': {**settings.get('tags', {}), 'immobile pre-deploy': False}}
    candidate, mobile_evidence = repair_immobile_deployments(target, temporary_settings, editor_locks=locks)
    if any((unit_mobility(candidate, candidate['units'][index]) != 'mobile' for index in unpaired)):
        return None
    if any((candidate['units'][index] != unit for index, unit in enumerate(target['units']) if index not in unpaired)):
        return None
    after = evaluate_immobile_symmetry(candidate, settings, editor_locks=editor_locks, symmetry_context=context)
    if after['issue_count'] >= diagnostic['issue_count']:
        return None
    certificates = {item['unit_index']: item.get('role_evidence') for item in mobile_evidence if item['kind'] == 'immobile_deployment_placement'}
    return (candidate, [{'kind': 'immobile_symmetry_demotion', 'unit_index': index, 'unit_type': kind, 'before': deepcopy(target['units'][index]), 'after': deepcopy(candidate['units'][index]), 'reason': 'free_immobility_yields_to_requested_mobile_transport', 'mobile_start_evidence': certificates.get(index), 'preserved_complete_static_group_indices': sorted(paired), 'balance_certified': False} for index in sorted(unpaired)])

def repair_immobile_symmetry(target, settings, *, editor_locks=None, symmetry_context=None):
    result, evidence = (deepcopy(target), [])
    context, blocked, frozen = _shared(result, settings, editor_locks, symmetry_context)
    before = evaluate_immobile_symmetry(result, settings, editor_locks=editor_locks, symmetry_context=context)
    if not before['applicable'] or before['status'] == 'matched':
        return (result, evidence)
    if len(result.get('units', [])) > 64:
        return (result, [{'kind': 'immobile_symmetry_unresolved', 'reason': 'bounded_planner_unit_limit_exceeded', 'diagnostic': before}])
    active, role_context = (context['active_slots'], _context(result, settings))
    groups = _groups(result, active)
    protected = _protected_transport(result, settings, groups)
    if not groups and settings.get('tags', {}).get('immobile pre-deploy') is True:
        kinds = sorted({unit['type'] for index, unit in enumerate(result['units']) if index not in frozen | protected and unit['owner'] in active}, key=lambda kind: (_TYPE_PRIORITY.get(kind, 20), kind))
        groups = {kind: [] for kind in kinds}
        introducing = True
    else:
        introducing = False
    for kind, indices in sorted(groups.items(), key=lambda item: (_TYPE_PRIORITY.get(item[0], 20), item[0])):
        pool = [index for index, unit in enumerate(result['units']) if unit['type'] == kind and unit['owner'] in active and (index in indices or (index not in frozen | protected and unit_mobility(result, unit) == 'mobile'))]
        minimum = max(len(indices), len(active))
        if len(pool) < minimum:
            demoted = _demote_free_transport_singletons(result, settings, kind, indices, editor_locks, context, blocked, frozen)
            if demoted is not None:
                result, changes = demoted
                evidence.extend(changes)
                continue
            evidence.append({'kind': 'immobile_symmetry_unresolved', 'unit_type': kind, 'reason': 'insufficient_available_same_type_units_for_player_counterparts', 'available': len(pool), 'required_minimum': minimum, 'protected_transport_witness_indices': sorted(protected)})
            continue
        options, fixed = _options(result, kind, active, role_context, blocked, frozen, pool)
        current = frozenset(((_position(result, result['units'][index]), result['units'][index]['owner']) for index in indices))
        required = frozenset(fixed.items())
        bundles = _bundles(result, options, context, current)
        members = _portfolio(options, bundles, active, minimum, len(pool), required, current)
        assignments = None if members is None else _assign_units(result, members, indices, pool, frozen)
        if assignments is None:
            demoted = _demote_free_transport_singletons(result, settings, kind, indices, editor_locks, context, blocked, frozen)
            if demoted is not None:
                result, changes = demoted
                evidence.extend(changes)
                continue
            evidence.append({'kind': 'immobile_symmetry_unresolved', 'unit_type': kind, 'reason': 'no_complete_equivalent_building_role_assignment', 'geometry_certified': bool(context.get('geometric')), 'fixed_static_unit_indices': sorted(frozen & set(indices)), 'candidate_bundle_count': len(bundles)})
            continue
        for index, (position, owner) in sorted(assignments.items()):
            unit, original = (result['units'][index], deepcopy(result['units'][index]))
            unit.update(owner=owner, x=position % result['width'], y=position // result['width'])
            if unit != original:
                evidence.append({'kind': 'immobile_symmetry_placement', 'unit_index': index, 'unit_type': kind, 'before': original, 'after': deepcopy(unit), 'promoted_mobile_counterpart': index not in indices, 'building_kind': options[position, owner]['building_kind'], 'role': options[position, owner]['role'], 'affected_player': options[position, owner]['affected_player'], 'geometry_certified': bool(context.get('geometric')), 'balance_certified': False})
        if introducing:
            break
    after = evaluate_immobile_symmetry(result, settings, editor_locks=editor_locks, symmetry_context=context)
    if evidence or after['issue_count']:
        evidence.append({'kind': 'immobile_symmetry_summary', 'before_issue_count': before['issue_count'], 'after_issue_count': after['issue_count'], 'diagnostic': after, 'balance_certified': False})
    return (result, evidence)
