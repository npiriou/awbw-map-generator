from tools.map_features.common import grid
STRUCTURAL_CATEGORY_TILE_IDS = {'Teleport Tile': frozenset({195})}
MULTIPLAYER_CATEGORIES = frozenset({'Team Play', 'FFA Multiplay'})

def structural_category_values(payload):
    tiles = set(grid(payload)[2])
    values = {category: bool(tiles & tile_ids) for category, tile_ids in STRUCTURAL_CATEGORY_TILE_IDS.items()}
    players = payload.get('Player Count')
    if type(players) is int and 0 < players < 3:
        values.update(dict.fromkeys(MULTIPLAYER_CATEGORIES, False))
    return values

def category_preference(settings, category):
    categories = settings.get('categories', {})
    if isinstance(categories, dict):
        return categories.get(category)
    return True if category in categories else None

def forbidden_tile_tokens(settings):
    return frozenset((f'tile:{tile}' for category, tiles in STRUCTURAL_CATEGORY_TILE_IDS.items() if category_preference(settings, category) is False for tile in tiles))
