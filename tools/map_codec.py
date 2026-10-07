from __future__ import annotations
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
from tools.tags import DESCRIPTIONS
from tools.map_features.common import canonical_map, grid, property_owner, property_type, terrain_name, terrain_reference
from tools.map_constraints import OFFICIAL_CATEGORIES
FORMAT = 'awbw_conditional_generation_v1'

def settings_schema():
    from tools.map_constraints import OFFICIAL_CATEGORIES
    from tools.map_features.access_counts import BUILDING_KINDS
    integer = {'type': 'integer', 'minimum': 0}
    counts = {'type': 'object', 'additionalProperties': False, 'properties': {name: integer for name in ('total', 'pre_owned', 'neutral')}}
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema', 'title': 'Optional AWBW map generation settings', 'type': 'object', 'additionalProperties': False, 'description': 'All settings optional. Omission means unconstrained; false and zero are constraints. Null is not a requested value. Terrain categories are checked structurally; other categories are soft labels. Cross-field feasibility is checked by tools.map_constraints.validate_settings.', 'properties': {'width': {'type': 'integer', 'minimum': 1}, 'height': {'type': 'integer', 'minimum': 1}, 'players': {'type': 'integer', 'minimum': 1}, 'tags': {'type': 'object', 'additionalProperties': False, 'properties': {tag: {'type': 'boolean'} for tag in DESCRIPTIONS}}, 'building_counts': {'type': 'object', 'additionalProperties': False, 'properties': {kind: counts for kind in BUILDING_KINDS}}, 'predeployed_counts': {'type': 'object', 'additionalProperties': False, 'properties': {kind: integer for kind in sorted(set(terrain_reference()['unit_types'].values()))}}, 'categories': {'type': ['array', 'object'], 'items': {'type': 'string', 'enum': sorted(OFFICIAL_CATEGORIES)}, 'additionalProperties': False, 'properties': {name: {'type': 'boolean'} for name in sorted(OFFICIAL_CATEGORIES)}}}, 'examples': [{}, {'width': 20, 'height': 20, 'players': 2, 'tags': {'rotational symmetry': True, 'pipe seams': False}, 'building_counts': {'base': {'pre_owned': 4}, 'airport': {'total': 0}}}, {'tags': {'island(s)': True}}]}

def active_countries(data):
    hqs = {b['owner'] for b in data['buildings'] if b['kind'] == 'hq' and b['owner'] is not None}
    labs = {b['owner'] for b in data['buildings'] if b['kind'] == 'lab' and b['owner'] is not None}
    return hqs or labs

def encode_target(payload):
    width, height, tiles = grid(payload)
    data = canonical_map(payload)
    players = payload.get('Player Count')
    if type(players) is not int or players <= 0:
        raise ValueError('nonpositive_player_count')
    if any((terrain_name(t) is None for t in tiles)):
        raise ValueError('unknown_terrain')
    if not data['units_complete']:
        raise ValueError('incomplete_or_invalid_unit_inventory')
    active = active_countries(data)
    if len(active) != players:
        raise ValueError('unknown_active_player_roster')
    countries = {b['owner'] for b in data['buildings'] if b['owner'] is not None}
    countries.update((u['owner'] for u in data['units']))
    ordered = sorted(active) + sorted(countries - active)
    slots = {country: i + 1 for i, country in enumerate(ordered)}
    units, positions = ([], set())
    for unit, source in zip(data['units'], payload['Predeployed Units'], strict=True):
        if type(source.get('Unit ID')) is not int:
            raise ValueError('invalid_unit_id')
        hp = source.get('Unit HP')
        if type(hp) not in (int, float) or not 0 < hp <= 10:
            raise ValueError('invalid_unit_hp')
        position = (unit['position']['x'], unit['position']['y'])
        if position in positions:
            raise ValueError('duplicate_unit_position')
        positions.add(position)
        units.append({'type': unit['unit_type'], 'owner': slots[unit['owner']], 'x': position[0], 'y': position[1], 'hp': hp})
    return {'width': width, 'height': height, 'players': players, 'factions': [{'slot': slots[c], 'active': c in active} for c in ordered], 'tiles': [f'property:{property_type(t)}' if property_type(t) else f'tile:{t}' for t in tiles], 'owners': [slots[property_owner(t)] if property_owner(t) is not None else 0 for t in tiles], 'units': sorted(units, key=lambda u: (u['y'], u['x'], u['owner'], u['type']))}

def decode_target(target, *, validate_active_factions=True):
    width, height, players = (target['width'], target['height'], target['players'])
    if any((type(v) is not int or v <= 0 for v in (width, height, players))):
        raise ValueError('invalid dimensions or players')
    tiles, owners, factions = (target['tiles'], target['owners'], target['factions'])
    if len(tiles) != width * height or len(owners) != len(tiles):
        raise ValueError('incorrect target grid length')
    if not factions or len(factions) > 20 or any((f.get('slot') != i + 1 or type(f.get('active')) is not bool for i, f in enumerate(factions))) or (sum((f['active'] for f in factions)) != players):
        raise ValueError('invalid faction slots')
    reference = terrain_reference()
    property_ids = {(kind, reference['property_owners'][tid]): int(tid) for tid, kind in reference['property_types'].items()}
    unit_ids = {kind: int(uid) for uid, kind in reference['unit_types'].items()}
    codes = {country: code for code, country in reference['country_codes'].items()}
    decoded = []
    for tile, owner in zip(tiles, owners, strict=True):
        if type(owner) is not int or not 0 <= owner <= len(factions) or (not isinstance(tile, str)):
            raise ValueError('invalid owner or tile')
        if tile.startswith('property:'):
            key = (tile.split(':', 1)[1], owner or None)
            if key not in property_ids:
                raise ValueError('property cannot have this owner (HQ cannot be neutral)')
            decoded.append(property_ids[key])
        elif tile.startswith('tile:'):
            try:
                terrain_id = int(tile.split(':', 1)[1])
            except ValueError as error:
                raise ValueError('invalid terrain token') from error
            if owner or property_type(terrain_id) or terrain_name(terrain_id) is None:
                raise ValueError('non-property token must have a known terrain and owner zero')
            decoded.append(terrain_id)
        else:
            raise ValueError('unknown tile token')
    units, occupied = ([], set())
    for unit in target['units']:
        x, y, owner, hp = (unit.get('x'), unit.get('y'), unit.get('owner'), unit.get('hp'))
        if type(x) is not int or type(y) is not int or (not (0 <= x < width and 0 <= y < height)) or (type(owner) is not int) or (not 1 <= owner <= len(factions)) or (type(hp) not in (int, float)) or (not 0 < hp <= 10) or (unit.get('type') not in unit_ids) or ((x, y) in occupied):
            raise ValueError('invalid unit target')
        occupied.add((x, y))
        units.append({'Unit ID': unit_ids[unit['type']], 'Unit X': x, 'Unit Y': y, 'Unit HP': hp, 'Country Code': codes[owner]})
    payload = {'Name': 'Generated map', 'Size X': width, 'Size Y': height, 'Player Count': players, 'Terrain Map': [[decoded[y * width + x] for y in range(height)] for x in range(width)], 'Predeployed Units': units}
    inferred = active_countries(canonical_map(payload))
    if validate_active_factions and inferred != {f['slot'] for f in factions if f['active']}:
        raise ValueError('active faction flags disagree with HQ/lab ownership')
    return payload
