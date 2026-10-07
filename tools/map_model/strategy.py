from collections import Counter
from tools.map_model.infantry_placement import evaluate_infantry_compensation
from tools.map_model.ownership_placement import evaluate_property_territory
from tools.map_model.opening_metrics import evaluate_opening
from tools.map_model.start_plan import evaluate_start_plan
from tools.map_model.unit_start_plan import evaluate_unit_start_plan
from tools.map_model.immobile_deployment import evaluate_immobile_deployments
from tools.map_model.transport_deployment import evaluate_transport_deployments

def property_layout_metrics(target):
    width, height = (target['width'], target['height'])
    cities = {i for i, tile in enumerate(target['tiles']) if tile == 'property:city'}
    border = {i for i in cities if i % width in {0, width - 1} or i // width in {0, height - 1}}
    edges = [range(width), range((height - 1) * width, height * width), range(0, width * height, width), range(width - 1, width * height, width)]
    longest = 0
    for edge in edges:
        current = 0
        for index in edge:
            current = current + 1 if index in cities else 0
            longest = max(longest, current)
    counts = {}
    for tile, owner in zip(target['tiles'], target['owners']):
        if tile.startswith('property:'):
            counts.setdefault(tile.split(':', 1)[1], Counter())[owner] += 1
    return {'cities': len(cities), 'border_cities': len(border), 'border_city_fraction': len(border) / len(cities) if cities else None, 'longest_border_city_run': longest, 'property_owner_counts': {kind: dict(counter) for kind, counter in counts.items()}}

def strategic_diagnostics(target, settings, *, editor_locks=None, deployment_group=None):
    if not target.get('factions') and target.get('players', 0) > 0:
        target = {**target, 'factions': [{'slot': slot, 'active': True} for slot in range(1, target['players'] + 1)]}
    ownership = evaluate_property_territory(target, settings)
    infantry = evaluate_infantry_compensation(target, settings, editor_locks=editor_locks)
    opening = evaluate_opening(target, settings, editor_context=editor_locks is not None)
    start = evaluate_start_plan(target, settings) if editor_locks is None else {'status': 'not_evaluated', 'reason': 'editor_context', 'balance_certified': False}
    unit_start = evaluate_unit_start_plan(target, settings, editor_locks=editor_locks)
    immobile = evaluate_immobile_deployments(target, settings, editor_locks=editor_locks)
    transports = evaluate_transport_deployments(target, settings, editor_locks=editor_locks)
    from tools.map_model.deployment_symmetry import build_symmetry_context
    from tools.map_model.immobile_symmetry import evaluate_immobile_symmetry
    from tools.map_model.transport_symmetry import evaluate_transport_symmetry
    context = build_symmetry_context(target, settings, group=deployment_group)
    paired_static = evaluate_immobile_symmetry(target, settings, editor_locks=editor_locks, symmetry_context=context)
    paired_transports = evaluate_transport_symmetry(target, settings, editor_locks=editor_locks, symmetry_context=context)
    from tools.map_model.lab_ownership import evaluate_lab_ownership
    labs = evaluate_lab_ownership(target, settings, editor_locks=editor_locks, group=deployment_group)
    from tools.map_model.property_symmetry import evaluate_property_symmetry
    properties = evaluate_property_symmetry(target, settings, editor_locks=editor_locks, group=deployment_group)
    warnings = []
    if ownership['conflict_count']:
        warnings.append(f"{ownership['conflict_count']} pre-owned properties have a shorter foot route from an enemy HQ/lab.")
    if infantry['applicable'] and infantry['status'] != 'matched':
        warnings.append('The single starting infantry does not satisfy the P2 starting-placement policy.')
    if opening['applicable'] and opening['status'] == 'review_required':
        owners = opening['active_slots']
        counts = [opening['per_owner'][str(owner)]['pre_owned_base_count'] for owner in owners]
        warnings.append(f'Starting production/capture disparity needs review (owned bases: {counts[0]} / {counts[1]}).')
        for owner in opening['disparity'].get('owners_without_production_or_capture_unit', []):
            warnings.append(f'Player slot {owner} starts with neither a production base nor capture infantry.')
    if unit_start['applicable'] and unit_start['issue_count']:
        warnings.append(f"{unit_start['issue_count']} starting-unit placement/allocation issues need review.")
    if immobile['issue_count']:
        warnings.append(f"{immobile['issue_count']} stationary-unit role/constraint issues need review.")
    if transports.get('issue_count'):
        warnings.append(f"{transports['issue_count']} transport pickup/owner/constraint issues need review.")
    if paired_static.get('issue_count'):
        warnings.append('Stationary units must occupy corresponding buildings with the same role for each player.')
    if paired_transports.get('issue_count'):
        warnings.append('Transport deployments must be symmetric, or use corresponding connected water areas.')
    if labs.get('issue_count'):
        warnings.append('Pre-owned Labs must follow the player ownership symmetry.')
    if properties.get('issue_count'):
        warnings.append('Pre-owned buildings must follow the player ownership symmetry.')
    return {'method': 'placement_diagnostics_v2', 'ownership': ownership, 'infantry_compensation': infantry, 'opening': opening, 'start_plan': start, 'unit_start_plan': unit_start, 'immobile_deployments': immobile, 'transport_deployments': transports, 'layout': property_layout_metrics(target), 'immobile_symmetry': paired_static, 'transport_symmetry': paired_transports, 'lab_ownership': labs, 'property_symmetry': properties, 'deployment_symmetry_context': context, 'warnings': warnings, 'balance_certified': False, 'scope': 'Clear-weather territory, unit placement and bounded early production/capture opportunities; no playability or balance certification.'}

def placement_candidate_rank(strategy):
    opening = strategy.get('opening', {})
    disparity = opening.get('disparity', {}) if opening.get('applicable') else {}
    severe = int(opening.get('status') == 'review_required')
    infantry = strategy.get('infantry_compensation', {})
    failed_infantry = int(infantry.get('applicable', False) and infantry.get('status') != 'matched')
    units = strategy.get('unit_start_plan', {})
    unit_issues = units.get('issue_count', 0) if units.get('applicable') else 0
    for name in ('immobile_deployments', 'transport_deployments'):
        roles = strategy.get(name, {})
        if roles.get('applicable'):
            unit_issues += sum((not (issue.get('editor_unit_fixed', False) or issue.get('unit_fields_locked', False) or issue.get('preservation_reason') == 'inactive_decorative_owner') for issue in roles.get('issues', [])))
            unit_issues += len(roles.get('role_balance_residuals', []))
            unit_issues += len(roles.get('owner_balance_residuals', []))
            units = roles.get('units', [])
            if not units or any((not unit.get('unit_fields_locked', False) for unit in units)):
                unit_issues += len(roles.get('feasibility_residuals', []))
    for name in ('immobile_symmetry', 'transport_symmetry', 'lab_ownership', 'property_symmetry'):
        diagnostic = strategy.get(name, {})
        if diagnostic.get('applicable'):
            unit_issues += diagnostic.get('issue_count', 0)
    start = strategy.get('start_plan', {})
    foreign = strategy.get('ownership', {}).get('conflict_count', 0) if start.get('status') != 'not_evaluated' else 0
    early_gaps = [abs(value['starting_plus_first_capture_bases_p1_minus_p2']) for turn, value in disparity.get('by_horizon', {}).items() if 2 <= int(turn) <= 4]
    return (severe + failed_infantry + unit_issues + foreign, abs(disparity.get('initial_production_p1_minus_p2', 0)), max(early_gaps, default=0))

def apply_deployment_constraints(report, strategy):
    from copy import deepcopy
    result = deepcopy(report)
    if result.get('status') in {'invalid_settings', 'invalid_map', 'invalid_representation'}:
        return result
    for name, path, message in (('immobile_symmetry', 'deployments.immobile_symmetry', 'Stationary units must occupy corresponding buildings with the same role for each player.'), ('transport_symmetry', 'deployments.transport_symmetry', 'Transports must be symmetric or in corresponding connected water areas.'), ('lab_ownership', 'ownership.labs', 'Pre-owned Labs must follow the player ownership symmetry.'), ('property_symmetry', 'ownership.properties', 'Pre-owned buildings must follow the player ownership symmetry.')):
        diagnostic = strategy.get(name, {})
        if not diagnostic.get('applicable') or not diagnostic.get('issue_count'):
            continue
        result.setdefault('violations', []).append({'path': path, 'reason': message, 'policy': diagnostic.get('policy'), 'issue_count': diagnostic['issue_count'], 'issues': deepcopy(diagnostic.get('issues', []))})
    if result.get('violations'):
        result['status'] = 'not_matched'
    return result

def map_strategic_diagnostics(payload, settings, *, editor_locks=None, deployment_group=None):
    from tools.map_codec import encode_target
    if not isinstance(payload, dict):
        return {'method': 'placement_diagnostics_v2', 'status': 'unavailable', 'reason': 'raw map could not be decoded', 'warnings': [], 'balance_certified': False}
    try:
        target = encode_target(payload)
    except (ValueError, KeyError, TypeError, IndexError) as error:
        return {'method': 'placement_diagnostics_v2', 'status': 'unavailable', 'reason': str(error), 'warnings': [], 'balance_certified': False}
    return strategic_diagnostics(target, settings, editor_locks=editor_locks, deployment_group=deployment_group)
