from __future__ import annotations
import json
from functools import lru_cache
from pathlib import Path
REFERENCE_ROOT = Path(__file__).with_name('references')

@lru_cache(maxsize=1)
def terrain_reference():
    return json.loads((REFERENCE_ROOT / 'terrain.json').read_text(encoding='utf-8'))

@lru_cache(maxsize=1)
def movement_reference():
    return json.loads((REFERENCE_ROOT / 'movement.json').read_text(encoding='utf-8'))

def grid(payload):
    width, height = (payload.get('Size X'), payload.get('Size Y'))
    if type(width) is not int or type(height) is not int or width <= 0 or (height <= 0):
        raise ValueError('Size X and Size Y must be positive integers')
    columns = payload.get('Terrain Map')
    if not isinstance(columns, list) or len(columns) != width or any((not isinstance(col, list) or len(col) != height for col in columns)) or any((type(tile) is not int for col in columns for tile in col)):
        raise ValueError('Terrain Map must be a complete column-major integer grid')
    return (width, height, tuple((columns[x][y] for y in range(height) for x in range(width))))

def neighbors(x, y, width, height):
    return tuple(((nx, ny) for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)) if 0 <= nx < width and 0 <= ny < height))

def property_type(terrain_id):
    return terrain_reference()['property_types'].get(str(terrain_id))

def property_owner(terrain_id):
    return terrain_reference()['property_owners'].get(str(terrain_id))

def terrain_name(terrain_id):
    if property_type(terrain_id) is not None:
        return 'property'
    if terrain_id == 1:
        return 'plain'
    if terrain_id == 2:
        return 'mountain'
    if terrain_id == 3:
        return 'wood'
    if 4 <= terrain_id <= 14:
        return 'river'
    if 15 <= terrain_id <= 25:
        return 'road'
    if terrain_id in (26, 27):
        return 'bridge'
    if terrain_id == 28:
        return 'sea'
    if 29 <= terrain_id <= 32:
        return 'shoal'
    if terrain_id == 33:
        return 'reef'
    if 101 <= terrain_id <= 110:
        return 'pipe'
    return {111: 'silo', 112: 'silo_empty', 113: 'pipe_seam', 114: 'pipe_seam', 115: 'plain', 116: 'plain', 195: 'teleport'}.get(terrain_id)

def unit_type(unit_id):
    return terrain_reference()['unit_types'].get(str(unit_id))

def country_id(code):
    return terrain_reference()['country_codes'].get(str(code).lower())

def canonical_map(payload):
    width, height, tiles = grid(payload)
    buildings = [{'id': i, 'kind': property_type(tile), 'owner': property_owner(tile), 'position': {'x': i % width, 'y': i // width}} for i, tile in enumerate(tiles) if property_type(tile) is not None]
    inventory = payload.get('Predeployed Units')
    issues = []
    if inventory is not None and (not isinstance(inventory, list)):
        issues.append({'reason': 'invalid_predeployed_inventory'})
        inventory = None
    units = []
    for i, unit in enumerate(inventory or []):
        if not isinstance(unit, dict):
            issues.append({'index': i, 'reason': 'invalid_predeployed_unit'})
            continue
        x, y = (unit.get('Unit X'), unit.get('Unit Y'))
        if type(x) is not int or type(y) is not int or (not (0 <= x < width and 0 <= y < height)):
            issues.append({'index': i, 'reason': 'invalid_predeployed_position', 'x': x, 'y': y})
            continue
        units.append({'id': i, 'unit_type': unit_type(unit.get('Unit ID')), 'owner': country_id(unit.get('Country Code')), 'position': {'x': x, 'y': y}})
    return {'width': width, 'height': height, 'terrain': tuple((terrain_name(t) for t in tiles)), 'buildings': buildings, 'units': units, 'inventory_issues': issues, 'units_complete': inventory is not None and (not issues) and all((unit['unit_type'] is not None and unit['owner'] is not None for unit in units))}
