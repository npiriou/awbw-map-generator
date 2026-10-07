from __future__ import annotations
from tools.map_features.symmetry_team import _ORIENTED, _key, _symmetry_key, _transform_key
_NEIGHBOR_FAMILIES = {'road': frozenset({'road', 'bridge'}), 'river': frozenset({'river', 'bridge', 'sea', 'shoal', 'reef'}), 'pipe': frozenset({'pipe', 'pipe_seam'}), 'pipe_seam': frozenset({'pipe', 'pipe_seam'}), 'bridge': frozenset({'road'})}
_PROPERTY_CONNECTIONS = frozenset({'road', 'bridge'})
_DIRECTIONS = ((0, -1), (1, 0), (0, 1), (-1, 0))

def _token_key(token):
    if token.startswith('property:'):
        return ('property', token.split(':', 1)[1])
    if token.startswith('tile:'):
        return _key(int(token.split(':', 1)[1]))
    raise ValueError(f'unsupported terrain token: {token}')

def _gameplay_key(token):
    if token.startswith('tile:'):
        return _symmetry_key(int(token.split(':', 1)[1]))
    return _token_key(token)

def _connector_mask(key):
    family, mask = key
    if family == 'pipe' and mask.bit_count() == 1:
        return _transform_key(key, (-1, 0, 0, -1))[1]
    return mask

def orient_terrain(tiles, width, height, vocabulary, *, group=None, locked_tiles=None):
    if type(width) is not int or type(height) is not int or width < 1 or (height < 1):
        raise ValueError('width and height must be positive integers')
    if len(tiles) != width * height:
        raise ValueError('tiles must be a complete row-major grid')
    tokens = vocabulary.get('rendering_tile_tokens', vocabulary['tile_tokens']) if vocabulary.get('gameplay_type_only') else vocabulary['tile_tokens']
    allowed = set(tokens)
    if any((token not in allowed for token in tiles)):
        raise ValueError('tiles contain a token outside the checkpoint vocabulary')
    keys = [_token_key(token) for token in tiles]
    families = dict(_NEIGHBOR_FAMILIES)
    if vocabulary.get('gameplay_type_only'):
        families['shoal'] = frozenset({'sea', 'reef'})
    variants = {family: [] for family in families}
    for token in tokens:
        if not token.startswith('tile:'):
            continue
        tile_id = int(token.split(':', 1)[1])
        if tile_id not in _ORIENTED:
            continue
        family, mask = _ORIENTED[tile_id]
        if family in variants:
            variants[family].append((token, _connector_mask((family, mask))))
    output = list(tiles)
    desired_masks = {}
    for index, (family, _) in enumerate(keys):
        if family not in families or not variants[family]:
            continue
        x, y = (index % width, index // width)
        desired = 0
        for bit, (dx, dy) in enumerate(_DIRECTIONS):
            nx, ny = (x + dx, y + dy)
            if not (0 <= nx < width and 0 <= ny < height):
                continue
            neighbor_family = keys[ny * width + nx][0]
            connects = neighbor_family in families[family]
            connects |= family in _PROPERTY_CONNECTIONS and neighbor_family == 'property'
            if connects:
                desired |= 1 << bit
        desired_masks[index] = desired
        output[index] = min(variants[family], key=lambda variant: ((variant[1] ^ desired).bit_count(), variant[0] != tiles[index]))[0]
    locked_tiles = locked_tiles or {}
    for index, token in locked_tiles.items():
        if token not in allowed or _gameplay_key(token) != _gameplay_key(tiles[index]):
            raise ValueError('locked visual tile must match its terrain family and vocabulary')
        output[index] = token
    if group and len(group) > 1:
        from tools.map_model.sample import _destination, _orbits
        by_key = {_token_key(token): token for token in tokens}
        for orbit in _orbits(width, height, group):
            representative = orbit[0]
            source = next((index for index in orbit if index in locked_tiles and _token_key(output[index])[1] is not None), representative)
            source_key = _token_key(output[source])
            family = source_key[0]
            if source_key[1] is None or any((_gameplay_key(output[index]) != _gameplay_key(output[source]) for index in orbit)):
                continue
            candidates = []
            for token in tokens:
                key = _token_key(token)
                if key[0] != family:
                    continue
                assignment = {}
                for matrix in group:
                    index = _destination(representative, width, height, matrix)
                    transformed = by_key.get(_transform_key(key, matrix))
                    if transformed is None or (index in assignment and assignment[index] != transformed):
                        break
                    if index in locked_tiles and locked_tiles[index] != transformed:
                        break
                    assignment[index] = transformed
                else:
                    cost = sum(((_connector_mask(_token_key(value)) ^ desired_masks[index]).bit_count() for index, value in assignment.items() if index in desired_masks))
                    changes = sum((value != output[index] for index, value in assignment.items()))
                    candidates.append(((cost, changes), assignment))
            if candidates:
                assignment = min(candidates, key=lambda candidate: candidate[0])[1]
                for index, token in assignment.items():
                    output[index] = token
    return output

def check_visual_symmetry(report, payload, group):
    if not group or len(group) <= 1:
        return report
    from tools.map_features.common import grid
    from tools.map_model.sample import _destination
    width, height, tiles = grid(payload)
    keys = [_key(tile) for tile in tiles]
    mismatches = set()
    for matrix in group:
        for index, key in enumerate(keys):
            if _transform_key(key, matrix) != keys[_destination(index, width, height, matrix)]:
                mismatches.add(index)
    report['observed']['visual_symmetry'] = {'matched': not mismatches, 'mismatched_tiles': len(mismatches)}
    if mismatches:
        report['violations'].append({'path': 'symmetry.visual', 'expected': 0, 'observed': len(mismatches), 'reason': 'Tile orientations cannot satisfy the requested symmetry while preserving locked tiles and terrain types.'})
        report['status'] = 'not_matched'
    return report
