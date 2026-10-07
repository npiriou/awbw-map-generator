from copy import deepcopy
from tools.map_features.categories import category_preference
from tools.map_model.ownership_placement import property_territory_distances
_KINDS = frozenset({'hq', 'lab', 'base', 'city', 'airport', 'port', 'com_tower'})
_SPECIAL = ('Team Play', 'FFA Multiplay', 'Gimmick', 'Joke', 'Sprite', 'Toy-Box')

def normalize_generated_property_owners(target, settings, editor_locks=None):
    result, evidence = (deepcopy(target), [])
    active = sorted((faction['slot'] for faction in result.get('factions', []) if faction.get('active')))
    if not active or len(set(active)) != result.get('players') or settings.get('players', result.get('players')) != result.get('players') or (settings.get('tags', {}).get('1vX team play') is True) or any((category_preference(settings, name) is True for name in _SPECIAL)):
        return (result, evidence)
    positions = []
    for position, (token, owner) in enumerate(zip(result['tiles'], result['owners'])):
        fields = (editor_locks or {}).get(position, (editor_locks or {}).get(str(position), {}))
        if token.startswith('property:') and token.split(':', 1)[1] in _KINDS and (owner > 0) and (owner not in active) and (not any((key in fields for key in (1, '1', 'owner')))):
            positions.append(position)
    if not positions:
        return (result, evidence)
    territory = property_territory_distances(result)
    for position in positions:
        reachable = {slot: distances[position] for slot, distances in territory['distances'].items() if position in distances}
        owner = min(reachable, key=lambda slot: (reachable[slot], slot)) if reachable else active[0]
        previous = result['owners'][position]
        result['owners'][position] = owner
        evidence.append({'kind': 'generated_property_active_owner', 'building': result['tiles'][position].split(':', 1)[1], 'position': {'x': position % result['width'], 'y': position // result['width']}, 'before_owner': previous, 'after_owner': owner, 'anchor_kind': territory['anchor_kind'], 'source': 'nearest_active_anchor_weighted_foot_route' if reachable else 'deterministic_active_slot_fallback_no_foot_route', 'owner_foot_cost': reachable.get(owner), 'route_unknown': not bool(reachable), 'uncertainty': territory['uncertainty'], 'preserved_owned_neutral_counts': True, 'preserved_terrain_units': True, 'balance_certified': False})
    return (result, evidence)
