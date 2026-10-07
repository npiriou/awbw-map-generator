from __future__ import annotations
from collections.abc import Mapping
from copy import deepcopy
from tools.map_features.common import country_id, grid, property_owner, property_type, terrain_reference

def _identity():
    return {owner: owner for owner in range(21)}

def _validated_mapping(mapping):
    if not isinstance(mapping, Mapping):
        raise TypeError('Editor palette must be an owner-to-slot mapping.')
    result = _identity()
    for owner, slot in mapping.items():
        if type(owner) is not int or type(slot) is not int or (not 0 <= owner <= 20) or (not 0 <= slot <= 20):
            raise ValueError('Editor palette owners and slots must be integers from 0 through 20.')
        result[owner] = slot
    if result[0] != 0 or set(result.values()) != set(range(21)):
        raise ValueError('Editor palette must be a bijection of army IDs with neutral owner zero fixed.')
    return result

def editor_palette(editor, settings):
    identity = _identity()
    if editor is None or type(settings.get('players')) is not int or (not 1 <= settings['players'] <= 20):
        return identity
    players = settings['players']
    hq_counts = settings.get('building_counts', {}).get('hq', {})
    lab_only = hq_counts.get('total') == 0 or hq_counts.get('pre_owned') == 0
    anchor_kind = 'lab' if lab_only else 'hq'
    anchors, existing = (set(), set())
    for tile in editor.get('tiles', []):
        owner = property_owner(tile['id'])
        if owner:
            existing.add(owner)
            if property_type(tile['id']) == anchor_kind:
                anchors.add(owner)
    for unit in editor.get('units', []):
        owner = country_id(unit['Country Code'])
        if owner is not None:
            existing.add(owner)
    if not anchors or anchors.issubset(range(1, players + 1)):
        return identity
    ordered = sorted(anchors) + sorted(existing - anchors) + sorted(set(range(1, 21)) - existing - anchors)
    return {0: 0, **{owner: slot for slot, owner in enumerate(ordered, 1)}}

def map_editor_field_owners(rawlocks, mapping):
    palette = _validated_mapping(mapping)
    result = {}
    for position, fields in (rawlocks or {}).items():
        values = dict(fields)
        for column in (1, 3):
            if column in values:
                owner = values[column]
                if type(owner) is not int or owner not in palette:
                    raise ValueError('A locked owner is outside the supported army palette.')
                values[column] = palette[owner]
        result[position] = values
    return result

def restore_editor_palette(payload, mapping):
    palette = _validated_mapping(mapping)
    inverse = {slot: owner for owner, slot in palette.items()}
    result = deepcopy(payload)
    width, height, _ = grid(result)
    reference = terrain_reference()
    property_ids = {(kind, reference['property_owners'][tile_id]): int(tile_id) for tile_id, kind in reference['property_types'].items()}
    country_codes = {owner: code for code, owner in reference['country_codes'].items()}
    for x in range(width):
        for y in range(height):
            tile = result['Terrain Map'][x][y]
            kind, owner = (property_type(tile), property_owner(tile))
            if kind and owner and (inverse[owner] != owner):
                result['Terrain Map'][x][y] = property_ids[kind, inverse[owner]]
    for unit in result.get('Predeployed Units', []):
        owner = country_id(unit['Country Code'])
        if owner is None:
            raise ValueError('A generated unit has an unknown country code.')
        if inverse[owner] != owner:
            unit['Country Code'] = country_codes[inverse[owner]]
    return result
