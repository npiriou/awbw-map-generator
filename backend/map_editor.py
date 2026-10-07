from __future__ import annotations
from copy import deepcopy
import math
from tools.map_features.common import country_id, grid, property_owner, property_type, terrain_name, unit_type
EDITOR_SYMMETRY_MODES = frozenset({'none', 'rotate-2', 'rotate-4', 'flip-x', 'flip-y', 'flip-4', 'diagonal-x', 'diagonal-y'})
_IDENTITY = (1, 0, 0, 1)
_VERTICAL = (-1, 0, 0, 1)
_HORIZONTAL = (1, 0, 0, -1)
_MAIN_DIAGONAL = (0, 1, 1, 0)
_ANTI_DIAGONAL = (0, -1, -1, 0)
_ROTATION_180 = (-1, 0, 0, -1)
_ROTATION_90 = (0, -1, 1, 0)
_ROTATION_270 = (0, 1, -1, 0)
_MODE_GENERATORS = {'none': (), 'rotate-2': (_ROTATION_180,), 'rotate-4': (_ROTATION_90,), 'flip-x': (_VERTICAL,), 'flip-y': (_HORIZONTAL,), 'flip-4': (_VERTICAL, _HORIZONTAL), 'diagonal-x': (_MAIN_DIAGONAL,), 'diagonal-y': (_ANTI_DIAGONAL,)}
_MODE_LABELS = {'rotate-2': 'Rotate 2Q', 'rotate-4': 'Rotate 4Q', 'flip-x': 'Flip X', 'flip-y': 'Flip Y', 'flip-4': 'Flip 4Q', 'diagonal-x': 'Diagonal X', 'diagonal-y': 'Diagonal Y'}

def _position(value, width, height, label, keys=('x', 'y')):
    if not isinstance(value, dict):
        raise ValueError(f'{label} must be an object.')
    x, y = (value.get(key) for key in keys)
    if type(x) is not int or type(y) is not int or (not (0 <= x < width and 0 <= y < height)):
        raise ValueError(f'{label} has an out-of-bounds or invalid position.')
    return (x, y)

def _unit(value, width, height, label):
    _position(value, width, height, label, ('Unit X', 'Unit Y'))
    uid, code, hp = (value.get('Unit ID'), value.get('Country Code'), value.get('Unit HP', 10))
    if type(uid) is not int or unit_type(uid) is None:
        raise ValueError(f'{label} has an unknown Unit ID.')
    if not isinstance(code, str) or country_id(code) is None:
        raise ValueError(f'{label} has an unknown Country Code.')
    if type(hp) not in (int, float) or not math.isfinite(hp) or (not 0 < hp <= 10):
        raise ValueError(f'{label} Unit HP must be greater than 0 and at most 10.')
    return {**deepcopy(value), 'Unit HP': hp, 'Country Code': code.lower()}

def validate_editor(editor, settings):
    if not isinstance(settings, dict):
        raise ValueError('settings must be an object.')
    if not isinstance(editor, dict) or set(editor) - {'width', 'height', 'tiles', 'units', 'empty_units', 'symmetry'}:
        raise ValueError('editor must be an object containing width, height, tiles, units, empty_units and optional symmetry only.')
    normalized = {}
    settings = deepcopy(settings)
    for key in ('width', 'height'):
        value = editor.get(key, settings.get(key))
        if type(value) is not int or not 1 <= value <= 50:
            raise ValueError(f'editor.{key} must be an integer between 1 and 50.')
        if key in settings and settings[key] != value:
            raise ValueError(f'editor.{key} conflicts with settings.{key}.')
        normalized[key] = settings[key] = value
    width, height = (normalized['width'], normalized['height'])
    if 'symmetry' in editor:
        mode = editor['symmetry']
        if not isinstance(mode, str) or mode not in EDITOR_SYMMETRY_MODES:
            raise ValueError('editor.symmetry must be a supported drawing symmetry mode.')
        normalized['symmetry'] = mode
    tile_positions, unit_positions = (set(), set())
    for key in ('tiles', 'units', 'empty_units'):
        values = editor.get(key, [])
        if not isinstance(values, list) or len(values) > width * height:
            raise ValueError(f'editor.{key} must be an array no larger than the map area.')
        normalized[key] = []
        for index, value in enumerate(values):
            label = f'editor.{key}[{index}]'
            position = _position(value, width, height, label, ('Unit X', 'Unit Y') if key == 'units' else ('x', 'y'))
            occupied = tile_positions if key == 'tiles' else unit_positions
            if position in occupied:
                raise ValueError(f'{label} duplicates or conflicts with another locked position.')
            occupied.add(position)
            if key == 'tiles':
                tile = value.get('id')
                if type(tile) is not int or terrain_name(tile) is None:
                    raise ValueError(f'{label} has an unknown terrain ID.')
                normalized[key].append({'x': position[0], 'y': position[1], 'id': tile})
            elif key == 'units':
                normalized[key].append(_unit(value, width, height, label))
            else:
                normalized[key].append({'x': position[0], 'y': position[1]})
    return (normalized, settings)

def _symmetry_group(generators):
    group, pending = ({_IDENTITY}, [_IDENTITY])
    while pending:
        a, b, c, d = pending.pop()
        for e, f, g, h in generators:
            product = (a * e + b * g, a * f + b * h, c * e + d * g, c * f + d * h)
            if product not in group:
                group.add(product)
                pending.append(product)
    return tuple(sorted(group))

def _symmetry_destination(x, y, width, height, matrix):
    a, b, c, d = matrix
    minimum_x = min(0, a * (width - 1), b * (height - 1), a * (width - 1) + b * (height - 1))
    minimum_y = min(0, c * (width - 1), d * (height - 1), c * (width - 1) + d * (height - 1))
    return (a * x + b * y - minimum_x, c * x + d * y - minimum_y)

def resolve_editor_symmetry(editor, settings):
    if editor is None:
        from tools.map_model.sample import symmetry_group
        return symmetry_group(settings)
    mode = editor.get('symmetry', 'none')
    if not isinstance(mode, str) or mode not in EDITOR_SYMMETRY_MODES:
        raise ValueError('editor.symmetry must be a supported drawing symmetry mode.')
    tags = settings.get('tags', {})
    if not isinstance(tags, dict):
        raise ValueError('settings.tags must be an object.')
    positive = [tag for tag in ('Flip symmetry', 'diagonal symmetry', 'rotational symmetry') if tags.get(tag) is True]
    if not positive and tags.get('Asymmetrical') is not False:
        return (_IDENTITY,)
    width, height = (editor['width'], editor['height'])
    choices, explicit_mode = ([], False)

    def alternatives(tag):
        nonlocal explicit_mode
        if tag == 'Flip symmetry':
            if mode in {'flip-x', 'flip-y', 'flip-4'}:
                explicit_mode = True
                return [_MODE_GENERATORS[mode]]
            return [(_VERTICAL,), (_HORIZONTAL,)]
        if tag == 'diagonal symmetry':
            if mode in {'diagonal-x', 'diagonal-y'}:
                explicit_mode = True
                return [_MODE_GENERATORS[mode]]
            return [(_MAIN_DIAGONAL,), (_ANTI_DIAGONAL,)]
        if mode in {'rotate-2', 'rotate-4'}:
            explicit_mode = True
            return [_MODE_GENERATORS[mode]]
        return [(_ROTATION_180,)]
    if positive:
        combinations = [()]
        for tag in positive:
            options = alternatives(tag)
            combinations = [(*existing, *candidate) for existing in combinations for candidate in options]
        choices = [_symmetry_group(generators) for generators in combinations]
    else:
        for tag in ('rotational symmetry', 'Flip symmetry', 'diagonal symmetry'):
            if tags.get(tag) is not False:
                choices.extend((_symmetry_group(generators) for generators in alternatives(tag)))
    locked = {(tile['x'], tile['y']): ('property', property_type(tile['id'])) if property_type(tile['id']) else ('terrain', terrain_name(tile['id'])) for tile in editor.get('tiles', [])}
    seen, dimensional_problem, disabled_problem, conflicts = (set(), False, False, [])
    for group in choices:
        if group in seen:
            continue
        seen.add(group)
        if width != height and any((matrix[1] or matrix[2] for matrix in group)):
            dimensional_problem = True
            continue
        forced = {'Flip symmetry': bool({_VERTICAL, _HORIZONTAL}.intersection(group)), 'diagonal symmetry': bool({_MAIN_DIAGONAL, _ANTI_DIAGONAL}.intersection(group)), 'rotational symmetry': bool({_ROTATION_90, _ROTATION_180, _ROTATION_270}.intersection(group))}
        if any((tags.get(tag) is False and guaranteed for tag, guaranteed in forced.items())):
            disabled_problem = True
            continue
        conflict = None
        for (x, y), key in locked.items():
            for matrix in group:
                destination = _symmetry_destination(x, y, width, height, matrix)
                if destination in locked and locked[destination] != key:
                    conflict = ((x, y), destination)
                    break
            if conflict:
                break
        if conflict is None:
            return group
        conflicts.append(conflict)
    if dimensional_problem and (not conflicts):
        raise ValueError('Diagonal symmetry and four-way rotation require equal width and height. Choose a compatible drawing symmetry or resize the editor map.')
    if disabled_problem and (not conflicts):
        raise ValueError('The selected drawing symmetry forces another symmetry disabled in generation settings. Change the drawing symmetry or generation symmetry settings.')
    if explicit_mode and positive and conflicts:
        first, counterpart = conflicts[0]
        raise ValueError(f'Locked terrain conflicts with {_MODE_LABELS.get(mode, mode)} symmetry at {first} and {counterpart}. Unlock or edit these tiles, or change the drawing symmetry.')
    if positive == ['diagonal symmetry']:
        name = 'diagonal symmetry on either diagonal'
    elif positive == ['Flip symmetry']:
        name = 'mirror symmetry on either axis'
    elif positive == ['rotational symmetry']:
        name = 'rotational symmetry'
    else:
        name = 'the requested symmetry combination'
    raise ValueError(f'Locked terrain cannot satisfy {name}. Unlock or edit the conflicting tiles.')

def apply_editor(payload, editor):
    result = deepcopy(payload)
    width, height, _ = grid(result)
    if (width, height) != (editor['width'], editor['height']):
        raise ValueError('Generated dimensions do not match the editor canvas.')
    for tile in editor['tiles']:
        result['Terrain Map'][tile['x']][tile['y']] = tile['id']
    overridden = {(value['x'], value['y']) for value in editor['empty_units']}
    overridden.update(((unit['Unit X'], unit['Unit Y']) for unit in editor['units']))
    result['Predeployed Units'] = sorted([unit for unit in result.get('Predeployed Units', []) if (unit['Unit X'], unit['Unit Y']) not in overridden] + deepcopy(editor['units']), key=lambda unit: (unit['Unit Y'], unit['Unit X']))
    return result

def sampling_locks(editor, vocabulary):
    locks = {}
    width = editor['width']
    for tile in editor['tiles']:
        kind = property_type(tile['id'])
        token = f'property:{kind}' if kind else f"tile:{tile['id']}"
        if vocabulary.get('gameplay_type_only'):
            from tools.map_model.terrain_codec import canonical_tile_token
            token = canonical_tile_token(token)
        if token in vocabulary['tile_tokens']:
            locks.setdefault(tile['y'] * width + tile['x'], {}).update({0: vocabulary['tile_tokens'].index(token), 1: property_owner(tile['id']) or 0})
    for unit in editor['units']:
        kind = unit_type(unit['Unit ID'])
        if kind in vocabulary['unit_types']:
            locks.setdefault(unit['Unit Y'] * width + unit['Unit X'], {}).update({2: vocabulary['unit_types'].index(kind) + 1, 3: country_id(unit['Country Code']), 4: max(1, min(100, round(unit['Unit HP'] * 10)))})
    for value in editor['empty_units']:
        locks.setdefault(value['y'] * width + value['x'], {}).update({2: 0, 3: 0, 4: 0})
    return locks

def validate_editable_map(payload):
    if not isinstance(payload, dict):
        raise ValueError('map must be an AWBW JSON object.')
    width, height, tiles = grid(payload)
    if width > 100 or height > 100:
        raise ValueError('Map dimensions cannot exceed 100 by 100.')
    players = payload.get('Player Count')
    if type(players) is not int or not 1 <= players <= 20:
        raise ValueError('Player Count must be an integer between 1 and 20.')
    if any((terrain_name(tile) is None for tile in tiles)):
        raise ValueError('Map contains an unknown terrain ID.')
    units = payload.get('Predeployed Units', [])
    if not isinstance(units, list) or len(units) > width * height:
        raise ValueError('Predeployed Units must be an array no larger than the map area.')
    occupied = set()
    for index, unit in enumerate(units):
        position = _position(unit, width, height, f'unit {index}', ('Unit X', 'Unit Y'))
        _unit(unit, width, height, f'unit {index}')
        if position in occupied:
            raise ValueError('Map has more than one unit on a cell.')
        occupied.add(position)
    return payload
