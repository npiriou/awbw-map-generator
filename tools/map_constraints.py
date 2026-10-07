from __future__ import annotations
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.tags import DESCRIPTIONS
from tools.map_features.access_counts import BUILDING_KINDS, analyze_access_counts
from tools.map_features.categories import MULTIPLAYER_CATEGORIES, category_preference, structural_category_values
from tools.map_features.common import country_id, grid, movement_reference, property_owner, property_type, terrain_name, unit_type
from tools.map_features.symmetry_team import SYMMETRY_TAGS, analyze_symmetry, analyze_team_play
from tools.map_features.terrain_units import analyze_terrain_units
SETTING_KEYS = frozenset({'width', 'height', 'players', 'tags', 'building_counts', 'predeployed_counts', 'categories'})
COUNT_KEYS = frozenset({'total', 'pre_owned', 'neutral'})
TERRAIN_TAGS = frozenset({'pipe seams', 'predeployed transports', 'immobile pre-deploy'})
GEOMETRIC_TAGS = ('Flip symmetry', 'diagonal symmetry', 'rotational symmetry')
OFFICIAL_CATEGORIES = frozenset({'Joke', 'Sprite', 'Under Review', 'Team Play', 'FFA Multiplay', 'Toy-Box', 'Historical/Geographical', 'B-Rank', 'New', 'S-Rank', 'Global League', 'Teleport Tile', 'Heavy Naval', 'Base Light', 'Mixed Base', 'Hall of Fame', 'High Funds', 'Fog of War', 'Gimmick', 'C-Rank', 'Live Play', 'Standard', 'A-Rank', 'RBC Playable', 'HFOG', 'Contested Bases', 'Medium Funds'})

def _issue(path, reason, **details):
    return {'path': path, 'reason': reason, **details}

def _integer(value, minimum):
    return type(value) is int and value >= minimum

def _settings_issues(settings):
    if not isinstance(settings, dict):
        return [_issue('settings', 'must be an object')]
    errors = [_issue(str(key), 'unknown setting') for key in settings if key not in SETTING_KEYS]
    for key in ('width', 'height', 'players'):
        if key in settings and (not _integer(settings[key], 1)):
            errors.append(_issue(key, 'must be a positive integer'))
    tags = settings.get('tags', {})
    if not isinstance(tags, dict):
        errors.append(_issue('tags', 'must be an object'))
        tags = {}
    for name, value in tags.items():
        if name not in DESCRIPTIONS:
            errors.append(_issue(f'tags.{name}', 'unknown tag'))
        if type(value) is not bool:
            errors.append(_issue(f'tags.{name}', 'must be a boolean'))
    buildings = settings.get('building_counts', {})
    if not isinstance(buildings, dict):
        errors.append(_issue('building_counts', 'must be an object'))
        buildings = {}
    minimum_tiles = 0
    for kind, counts in buildings.items():
        path = f'building_counts.{kind}'
        if kind not in BUILDING_KINDS:
            errors.append(_issue(path, 'unknown building type'))
        if not isinstance(counts, dict):
            errors.append(_issue(path, 'must be an object'))
            continue
        valid = {}
        for key, value in counts.items():
            if key not in COUNT_KEYS:
                errors.append(_issue(f'{path}.{key}', 'unknown building count field'))
            elif not _integer(value, 0):
                errors.append(_issue(f'{path}.{key}', 'must be a nonnegative integer'))
            else:
                valid[key] = value
        if 'total' in valid:
            total = valid['total']
            if 'pre_owned' in valid and 'neutral' in valid:
                if total != valid['pre_owned'] + valid['neutral']:
                    errors.append(_issue(path, 'total must equal pre_owned + neutral'))
            elif any((valid[key] > total for key in ('pre_owned', 'neutral') if key in valid)):
                errors.append(_issue(path, 'total cannot be smaller than a supplied component'))
        if kind in BUILDING_KINDS:
            minimum_tiles += max(valid.get('total', 0), valid.get('pre_owned', 0) + valid.get('neutral', 0))
    units = settings.get('predeployed_counts', {})
    if not isinstance(units, dict):
        errors.append(_issue('predeployed_counts', 'must be an object'))
        units = {}
    known_units = movement_reference()['units']
    for kind, count in units.items():
        if kind not in known_units:
            errors.append(_issue(f'predeployed_counts.{kind}', 'unknown unit type'))
        if not _integer(count, 0):
            errors.append(_issue(f'predeployed_counts.{kind}', 'must be a nonnegative integer'))
    if tags.get('predeployed transports') is False and tags.get('immobile pre-deploy') is False and _integer(units.get('transport_copter'), 1):
        errors.append(_issue('predeployed_counts.transport_copter', 'mobile T-Copters always count as transports; enable transports or allow an immobile deployment'))
    transport_types = {'black_boat', 'lander', 'transport_copter'}
    if tags.get('predeployed transports') is True and transport_types <= units.keys() and all((type(units[kind]) is int and units[kind] == 0 for kind in transport_types)):
        errors.append(_issue('tags.predeployed transports', 'requires a Black Boat, Lander or T-Copter; all three requested counts are zero'))
    if tags.get('immobile pre-deploy') is True and known_units.keys() <= units.keys() and all((type(units[kind]) is int and units[kind] == 0 for kind in known_units)):
        errors.append(_issue('tags.immobile pre-deploy', 'requires a unit; all requested unit counts are zero'))
    categories = settings.get('categories', [])
    if isinstance(categories, dict):
        for category, value in categories.items():
            if category not in OFFICIAL_CATEGORIES:
                errors.append(_issue(f'categories.{category}', 'unknown official category label'))
            if type(value) is not bool:
                errors.append(_issue(f'categories.{category}', 'must be a boolean'))
    elif isinstance(categories, list):
        for index, category in enumerate(categories):
            if not isinstance(category, str) or category not in OFFICIAL_CATEGORIES:
                errors.append(_issue(f'categories.{index}', 'unknown official category label'))
    else:
        errors.append(_issue('categories', 'must be an object of boolean preferences or a list of official category labels'))
    players = settings.get('players')
    if isinstance(categories, (dict, list)) and _integer(players, 1) and (players < 3):
        for category in sorted(MULTIPLAYER_CATEGORIES):
            if category_preference(settings, category) is True:
                errors.append(_issue(f'categories.{category}', 'requires at least three players'))
    width, height = (settings.get('width'), settings.get('height'))
    if _integer(width, 1) and _integer(height, 1):
        if minimum_tiles > width * height:
            errors.append(_issue('building_counts', 'minimum building tiles exceed width * height'))
        if tags.get('diagonal symmetry') is True and width != height:
            errors.append(_issue('tags.diagonal symmetry', 'requires equal width and height'))
    if tags.get('Asymmetrical') is True and any((tags.get(name) is True for name in GEOMETRIC_TAGS)):
        errors.append(_issue('tags', 'Asymmetrical cannot coexist with a required symmetry'))
    if tags.get('Asymmetrical') is False and all((tags.get(name) is False for name in GEOMETRIC_TAGS)):
        errors.append(_issue('tags', 'Asymmetrical false requires at least one supported symmetry'))
    if tags.get('Flip symmetry') is True and tags.get('diagonal symmetry') is True and (tags.get('rotational symmetry') is False):
        errors.append(_issue('tags', 'Flip and diagonal symmetry together require rotational symmetry'))
    if tags.get('1vX team play') is True:
        players = settings.get('players')
        if _integer(players, 1) and players < 3:
            errors.append(_issue('tags.1vX team play', 'requires at least three players'))
        if tags.get('Asymmetrical') is False or any((tags.get(name) is True for name in GEOMETRIC_TAGS)):
            errors.append(_issue('tags.1vX team play', 'requires an Asymmetrical map'))
    return errors

def validate_settings(settings):
    return [f"{error['path']}: {error['reason']}" for error in _settings_issues(settings)]

def _validate_map(payload):
    errors, observed = ([], {})
    if not isinstance(payload, dict):
        return ([_issue('map', 'must be a raw AWBW map object')], observed)
    try:
        width, height, tiles = grid(payload)
    except (ValueError, TypeError, KeyError, IndexError) as error:
        return ([_issue('Terrain Map', str(error))], observed)
    observed.update(width=width, height=height, players=payload.get('Player Count'))
    if not _integer(payload.get('Player Count'), 1):
        errors.append(_issue('Player Count', 'must be a positive integer'))
    hq_owners, lab_owners, represented = (set(), set(), set())
    for index, tile in enumerate(tiles):
        if terrain_name(tile) is None:
            errors.append(_issue(f'Terrain Map.{index % width}.{index // width}', 'unknown terrain ID', observed=tile))
        kind, owner = (property_type(tile), property_owner(tile))
        if owner is not None:
            represented.add(owner)
            if kind == 'hq':
                hq_owners.add(owner)
            elif kind == 'lab':
                lab_owners.add(owner)
    counts, occupied = (Counter(), set())
    inventory = payload.get('Predeployed Units')
    if not isinstance(inventory, list):
        errors.append(_issue('Predeployed Units', 'must be a supplied list, including when empty'))
        inventory = []
    for index, unit in enumerate(inventory):
        path = f'Predeployed Units.{index}'
        if not isinstance(unit, dict):
            errors.append(_issue(path, 'must be an object'))
            continue
        raw_id = unit.get('Unit ID')
        kind = unit_type(raw_id)
        if type(raw_id) is not int or kind is None:
            errors.append(_issue(f'{path}.Unit ID', 'must be a known integer unit ID', observed=raw_id))
        else:
            counts[kind] += 1
        owner = country_id(unit.get('Country Code'))
        if owner is None:
            errors.append(_issue(f'{path}.Country Code', 'must be a known country code'))
        else:
            represented.add(owner)
        x, y = (unit.get('Unit X'), unit.get('Unit Y'))
        if type(x) is not int or type(y) is not int or (not (0 <= x < width and 0 <= y < height)):
            errors.append(_issue(path, 'unit coordinates must be integers within map bounds', observed=[x, y]))
        elif (x, y) in occupied:
            errors.append(_issue(path, 'duplicate unit position', observed=[x, y]))
        else:
            occupied.add((x, y))
        if 'Unit HP' in unit:
            hp = unit['Unit HP']
            if type(hp) not in (int, float) or not 0 < hp <= 10 or (not math.isfinite(hp)):
                errors.append(_issue(f'{path}.Unit HP', 'must be a finite number greater than 0 and at most 10'))
    active = hq_owners or lab_owners
    observed.update(active_country_ids=sorted(active), active_country_source='hq' if hq_owners else 'lab' if lab_owners else None, ghost_country_ids=sorted(represented - active) if active else [], predeployed_counts={kind: counts[kind] for kind in movement_reference()['units']})
    if active and _integer(payload.get('Player Count'), 1) and (len(active) != payload['Player Count']):
        errors.append(_issue('Player Count', 'must match active HQ owners, or lab owners when no HQ exists', expected=len(active), observed=payload['Player Count']))
    return (errors, observed)

def check_map(payload, settings):
    errors = _settings_issues(settings)
    if errors:
        return {'status': 'invalid_settings', 'violations': errors, 'unresolved': [], 'observed': {}}
    errors, observed = _validate_map(payload)
    if errors:
        return {'status': 'invalid_map', 'violations': errors, 'unresolved': [], 'observed': observed}
    violations, unresolved = ([], [])

    def compare(path, expected, actual):
        if actual is None:
            unresolved.append(_issue(path, 'requested value is not certified', expected=expected, observed=None))
        elif actual != expected:
            violations.append(_issue(path, 'value does not match', expected=expected, observed=actual))
    for key in ('width', 'height', 'players'):
        if key in settings:
            compare(key, settings[key], observed[key])
    for kind, count in settings.get('predeployed_counts', {}).items():
        compare(f'predeployed_counts.{kind}', count, observed['predeployed_counts'][kind])
    requested_tags = settings.get('tags', {})
    evaluations = {}
    symmetry = None
    access = None

    def run(name, detector):
        try:
            return detector()
        except (ValueError, TypeError, KeyError, IndexError) as error:
            unresolved.append(_issue(name, 'detector could not certify requested features', error=str(error)))
            return None
    if set(requested_tags) & TERRAIN_TAGS:
        terrain = run('tags', lambda: analyze_terrain_units(payload))
        if terrain is not None:
            evaluations.update(terrain['tags'])
    if set(requested_tags) & set(SYMMETRY_TAGS) or '1vX team play' in requested_tags:
        symmetry = run('tags', lambda: analyze_symmetry(payload))
        if symmetry is not None:
            evaluations.update(symmetry['tags'])
    team_needs_access = '1vX team play' in requested_tags and observed['players'] >= 3 and (symmetry is not None) and (symmetry['tags']['Asymmetrical']['status'] == 'matched')
    if 'island(s)' in requested_tags or any(settings.get('building_counts', {}).values()) or team_needs_access:
        access = run('building_counts', lambda: analyze_access_counts(payload))
        if access is not None:
            evaluations.update(access['tags'])
            observed['building_counts'] = access['building_counts']
    if '1vX team play' in requested_tags:
        team = run('tags.1vX team play', lambda: analyze_team_play(payload, symmetry or {}, access['evidence']['building_access'] if access else None))
        if team is not None:
            evaluations['1vX team play'] = team
    if requested_tags:
        observed['tags'] = {}
    for tag, required in requested_tags.items():
        evaluation = evaluations.get(tag, {'status': 'unresolved', 'evidence': {'reason': 'detector_failed'}})
        observed['tags'][tag] = evaluation
        actual = {'matched': True, 'not_matched': False}.get(evaluation['status'])
        compare(f'tags.{tag}', required, actual)
    for kind, counts in settings.get('building_counts', {}).items():
        for field, required in counts.items():
            actual = access['building_counts'].get(kind, {}).get(field) if access else None
            compare(f'building_counts.{kind}.{field}', required, actual)
    categories = settings.get('categories', [])
    if 'categories' in settings:
        observed['categories'] = {}
    structural_categories = structural_category_values(payload)
    for category in categories:
        path = f'categories.{category}'
        expected = categories[category] if isinstance(categories, dict) else category
        if category in structural_categories:
            actual = structural_categories[category]
            observed['categories'][category] = actual
            compare(path, category_preference(settings, category), actual)
        else:
            observed['categories'][category] = 'unresolved'
            unresolved.append(_issue(path, 'official category is a soft label without structural certification', expected=expected, observed=None))
    status = 'not_matched' if violations else 'unresolved' if unresolved else 'matched'
    return {'status': status, 'violations': violations, 'unresolved': unresolved, 'observed': observed}

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--map', type=Path, required=True, dest='map_path', help='raw AWBW map JSON')
    parser.add_argument('--settings', type=Path, help='optional settings JSON; omission means {}')
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.map_path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as error:
        result = {'status': 'invalid_map', 'violations': [_issue('map', str(error))], 'unresolved': [], 'observed': {}}
    else:
        try:
            settings = json.loads(args.settings.read_text(encoding='utf-8-sig')) if args.settings else {}
        except (OSError, ValueError) as error:
            result = {'status': 'invalid_settings', 'violations': [_issue('settings', str(error))], 'unresolved': [], 'observed': {}}
        else:
            result = check_map(payload, settings)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {'matched': 0, 'not_matched': 1, 'invalid_settings': 2, 'invalid_map': 2, 'unresolved': 3}[result['status']]
if __name__ == '__main__':
    raise SystemExit(main())
