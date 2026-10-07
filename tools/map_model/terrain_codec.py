from __future__ import annotations
from copy import deepcopy
from tools.map_features.common import property_type, terrain_name, terrain_reference
GAMEPLAY_REPRESENTATIVES = {'plain': 1, 'mountain': 2, 'wood': 3, 'river': 4, 'road': 15, 'bridge': 26, 'sea': 28, 'shoal': 29, 'reef': 33, 'pipe': 101, 'silo': 111, 'silo_empty': 112, 'pipe_seam': 113, 'teleport': 195}

def canonical_tile_token(token):
    if not isinstance(token, str):
        raise ValueError('terrain token must be a string')
    if token.startswith('property:'):
        kind = token.split(':', 1)[1]
        if kind not in set(terrain_reference()['property_types'].values()):
            raise ValueError(f'unknown property token: {token}')
        return token
    if not token.startswith('tile:'):
        raise ValueError(f'unknown terrain token: {token}')
    try:
        tile = int(token.split(':', 1)[1])
    except ValueError as error:
        raise ValueError(f'invalid terrain token: {token}') from error
    family = terrain_name(tile)
    if property_type(tile) is not None or family not in GAMEPLAY_REPRESENTATIVES:
        raise ValueError(f'unknown non-property terrain token: {token}')
    return f'tile:{GAMEPLAY_REPRESENTATIVES[family]}'

def canonicalize_target(target):
    result = deepcopy(target)
    result['tiles'] = [canonical_tile_token(token) for token in target['tiles']]
    return result

def gameplay_vocabulary(render_tokens=None):
    properties = {f'property:{kind}' for kind in terrain_reference()['property_types'].values()}
    visual = properties | {f'tile:{tile}' for tile in (*range(1, 34), *range(101, 117), 195)}
    if render_tokens is not None:
        visual.update(render_tokens)
    canonical = {token: canonical_tile_token(token) for token in sorted(visual)}
    variants = {token: [] for token in sorted(set(canonical.values()))}
    for visual_token, canonical_token in canonical.items():
        variants[canonical_token].append(visual_token)
    return {'tile_tokens': sorted(variants), 'owner_slots': list(range(21)), 'unit_types': sorted(set(terrain_reference()['unit_types'].values())), 'gameplay_type_only': True, 'terrain_codec': 'gameplay_type_representatives_v1', 'rendering_tile_tokens': sorted(visual), 'rendering_variants': variants, 'canonical_tile_tokens': canonical, 'tile_gameplay_types': {token: token.split(':', 1)[1] if token.startswith('property:') else terrain_name(int(token.split(':', 1)[1])) for token in sorted(visual)}, 'ordering': 'targets use row-major tiles/owners; active faction slots first'}
