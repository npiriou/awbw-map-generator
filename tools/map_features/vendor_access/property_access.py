from __future__ import annotations
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any, Literal
from ..common import movement_reference
Position = tuple[int, int]
Access = Literal['foot_reachable', 'sea_separated', 'blocked', 'unknown']

@dataclass(frozen=True)
class PropertyAccess:
    building_id: int | None
    position: Position
    infantry_region: int | None
    access: Access
    reason: str | None = None

@dataclass(frozen=True)
class PropertyAccessReport:
    width: int
    height: int
    player_id: int | None
    origins: tuple[Position, ...]
    infantry_regions: tuple[int | None, ...]
    properties: tuple[PropertyAccess, ...]

    @property
    def island_properties(self) -> tuple[PropertyAccess, ...]:
        return tuple((site for site in self.properties if site.access == 'sea_separated'))

    @property
    def has_island_properties(self) -> bool | None:
        if self.island_properties:
            return True
        if any((site.access == 'unknown' for site in self.properties)):
            return None
        return False

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), 'has_island_properties': self.has_island_properties}

@dataclass(frozen=True)
class NeutralSeamAccess:
    building_id: int | None
    position: Position
    access: Literal['foot_reachable', 'seam_blocked', 'unreachable', 'unknown']
    minimum_seams_to_destroy: int | None
    seams_to_destroy: tuple[Position, ...]
    path: tuple[Position, ...]
    reason: str | None = None

@dataclass(frozen=True)
class NeutralSeamAccessReport:
    width: int
    height: int
    player_id: int | None
    origins: tuple[Position, ...]
    properties: tuple[NeutralSeamAccess, ...]
    unknown_ownership_positions: tuple[Position, ...]

    @property
    def seam_blocked_properties(self) -> tuple[NeutralSeamAccess, ...]:
        return tuple((site for site in self.properties if site.access == 'seam_blocked'))

    @property
    def has_seam_blocked_neutral_properties(self) -> bool | None:
        if self.seam_blocked_properties:
            return True
        if self.unknown_ownership_positions or any((site.access == 'unknown' for site in self.properties)):
            return None
        return False

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), 'has_seam_blocked_neutral_properties': self.has_seam_blocked_neutral_properties}

def _known(value: Any) -> Any:
    if isinstance(value, Mapping) and 'knowledge' in value:
        return value.get('value') if value['knowledge'] == 'known' else None
    return value

def _position(value: Any) -> Position:
    if isinstance(value, Mapping):
        value = (value.get('x'), value.get('y'))
    if not isinstance(value, (tuple, list)) or len(value) != 2 or any((type(coordinate) is not int for coordinate in value)):
        raise ValueError('positions must contain integer x and y coordinates')
    return (value[0], value[1])

@lru_cache(maxsize=1)
def _movement_costs() -> Mapping[str, Any]:
    return movement_reference()['costs']['clear']

def _canonical_grid(data: Mapping[str, Any]) -> tuple[int, int, tuple[str | None, ...]]:
    grid = data.get('map', data)
    width, height = (grid.get('width'), grid.get('height'))
    if type(width) is not int or type(height) is not int or width <= 0 or (height <= 0):
        raise ValueError('a canonical map with positive integer width and height is required')
    terrain = tuple((_known(tile) for tile in grid.get('terrain', ())))
    if len(terrain) != width * height:
        raise ValueError('terrain must contain width * height row-major tiles')
    valid = _movement_costs()
    if any((tile is not None and (not isinstance(tile, str) or tile not in valid) for tile in terrain)):
        raise ValueError('terrain must use canonical engine names or unknown knowledge values')
    return (width, height, terrain)

@lru_cache(maxsize=1)
def _foot_terrains() -> frozenset[str]:
    costs = _movement_costs()
    return frozenset((terrain for terrain, classes in costs.items() if classes['foot'] is not None))

def _regions(width: int, height: int, terrain: tuple[str | None, ...], *, water: bool, optimistic: bool) -> tuple[int | None, ...]:
    passable = _foot_terrains() | ({'sea', 'reef'} if water else set())
    labels: list[int | None] = [None] * len(terrain)
    pads = [index for index, tile in enumerate(terrain) if tile == 'teleport']
    pads_linked = False
    region = 0
    for start, tile in enumerate(terrain):
        if labels[start] is not None or not (tile in passable or (optimistic and tile is None)):
            continue
        labels[start] = region
        pending = deque([start])
        while pending:
            index = pending.popleft()
            x, y = (index % width, index // width)
            neighbors = []
            if x > 0:
                neighbors.append(index - 1)
            if x + 1 < width:
                neighbors.append(index + 1)
            if y > 0:
                neighbors.append(index - width)
            if y + 1 < height:
                neighbors.append(index + width)
            if optimistic and terrain[index] == 'teleport' and (not pads_linked):
                neighbors.extend(pads)
                pads_linked = True
            for neighbor in neighbors:
                tile = terrain[neighbor]
                if labels[neighbor] is None and (tile in passable or (optimistic and tile is None)):
                    labels[neighbor] = region
                    pending.append(neighbor)
        region += 1
    return tuple(labels)

@lru_cache(maxsize=8)
def _topology(width: int, height: int, terrain: tuple[str | None, ...]):
    return tuple((_regions(width, height, terrain, water=water, optimistic=optimistic) for water, optimistic in ((False, False), (False, True), (True, False), (True, True))))

def analyze_property_access(data: Mapping[str, Any], *, player_id: int | None=None, origins: Iterable[Position | Mapping[str, int]] | None=None) -> PropertyAccessReport:
    width, height, terrain = _canonical_grid(data)

    def index(position: Position) -> int:
        x, y = position
        if not (0 <= x < width and 0 <= y < height):
            raise ValueError(f'position {position} is outside the map')
        return y * width + x
    if player_id is None:
        player_id = data.get('viewer', data.get('active_player'))
    buildings = data.get('buildings')
    if buildings is None:
        buildings = [{'id': None, 'position': {'x': i % width, 'y': i // width}} for i, tile in enumerate(terrain) if tile == 'property']
    if origins is None:
        if player_id is None:
            raise ValueError('provide player_id or explicit origins for map-only analysis')
        sources = [_position(site['position']) for site in buildings if _known(site.get('owner', site.get('initial_owner'))) == player_id and _known(site.get('kind')) in {'hq', 'base'}]
        sources.extend((_position(unit['position']) for unit in data.get('units', ()) if unit.get('owner') == player_id and unit.get('unit_type') in {'infantry', 'mech'} and (unit.get('position') is not None) and (unit.get('transport') is None)))
    else:
        sources = [_position(origin) for origin in origins]
    sources = tuple(sorted(set(sources)))
    source_indices = tuple((index(position) for position in sources))
    if any((terrain[i] is not None and terrain[i] not in _foot_terrains() for i in source_indices)):
        raise ValueError('an infantry origin is on impassable terrain')
    graphs = _topology(width, height, terrain)
    source_regions = [{labels[i] for i in source_indices if labels[i] is not None} for labels in graphs]
    properties = []
    for site in buildings:
        position = _position(site['position'])
        tile_index = index(position)
        connected = [labels[tile_index] in regions for labels, regions in zip(graphs, source_regions)]
        reason = None
        if not sources:
            access, reason = ('unknown', 'no_infantry_origins')
        elif terrain[tile_index] is None:
            access, reason = ('unknown', 'unknown_property_terrain')
        elif graphs[0][tile_index] is None:
            access, reason = ('blocked', 'property_terrain_is_impassable')
        elif connected[0]:
            access = 'foot_reachable'
        elif connected[1]:
            access, reason = ('unknown', 'unknown_terrain_or_teleport_route')
        elif connected[2]:
            access = 'sea_separated'
        elif connected[3]:
            access, reason = ('unknown', 'unknown_terrain_or_teleport_route')
        else:
            access, reason = ('blocked', 'non_water_terrain_barrier')
        properties.append(PropertyAccess(site['id'], position, graphs[0][tile_index], access, reason))
    return PropertyAccessReport(width, height, player_id, sources, graphs[0], tuple(properties))

def _seam_routes(width, height, terrain, origins, *, optimistic):
    distance: list[int | None] = [None] * len(terrain)
    previous: list[int | None] = [None] * len(terrain)
    pending = deque()
    passable = _foot_terrains() | {'pipe_seam'}
    pads = [index for index, tile in enumerate(terrain) if tile == 'teleport']

    def neighbors_of(index, link_pads):
        x, y = (index % width, index // width)
        neighbors = []
        if x > 0:
            neighbors.append(index - 1)
        if x + 1 < width:
            neighbors.append(index + 1)
        if y > 0:
            neighbors.append(index - width)
        if y + 1 < height:
            neighbors.append(index + width)
        if link_pads:
            neighbors.extend(pads)
        return neighbors
    for x, y in origins:
        index = y * width + x
        if terrain[index] in passable or (optimistic and terrain[index] is None):
            distance[index] = 0
            pending.append(index)
    pads_linked = False
    while pending:
        index = pending.popleft()
        link_pads = optimistic and terrain[index] == 'teleport' and (not pads_linked)
        neighbors = neighbors_of(index, link_pads)
        pads_linked |= link_pads
        for neighbor in neighbors:
            tile = terrain[neighbor]
            if tile not in passable and (not (optimistic and tile is None)):
                continue
            cost = int(tile == 'pipe_seam')
            candidate = distance[index] + cost
            if distance[neighbor] is None or candidate < distance[neighbor]:
                distance[neighbor] = candidate
                if cost:
                    pending.append(neighbor)
                else:
                    pending.appendleft(neighbor)
    visited = set()
    for x, y in origins:
        index = y * width + x
        if distance[index] == 0:
            visited.add(index)
            pending.append(index)
    pads_linked = False
    while pending:
        index = pending.popleft()
        link_pads = optimistic and terrain[index] == 'teleport' and (not pads_linked)
        neighbors = neighbors_of(index, link_pads)
        pads_linked |= link_pads
        for neighbor in neighbors:
            if neighbor in visited or distance[neighbor] is None:
                continue
            if distance[neighbor] != distance[index] + int(terrain[neighbor] == 'pipe_seam'):
                continue
            visited.add(neighbor)
            previous[neighbor] = index
            pending.append(neighbor)
    return (distance, previous)

def analyze_neutral_seam_access(data: Mapping[str, Any], *, player_id: int | None=None, origins: Iterable[Position | Mapping[str, int]] | None=None) -> NeutralSeamAccessReport:
    base = analyze_property_access(data, player_id=player_id, origins=origins)
    grid = data.get('map', data)
    terrain = tuple((_known(tile) for tile in grid['terrain']))
    distance, previous = _seam_routes(base.width, base.height, terrain, base.origins, optimistic=False)
    possible, _ = _seam_routes(base.width, base.height, terrain, base.origins, optimistic=True)
    buildings = data.get('buildings')
    if buildings is None:
        return NeutralSeamAccessReport(base.width, base.height, base.player_id, base.origins, (), tuple((site.position for site in base.properties)))
    results, unknown_ownership = ([], [])
    for building in buildings:
        position = _position(building['position'])
        if 'owner' not in building and 'initial_owner' not in building:
            unknown_ownership.append(position)
            continue
        owner = building.get('owner', building.get('initial_owner'))
        if isinstance(owner, Mapping) and (owner.get('knowledge') != 'known' or 'value' not in owner):
            unknown_ownership.append(position)
            continue
        if _known(owner) is not None:
            continue
        index = position[1] * base.width + position[0]
        count, lower = (distance[index], possible[index])
        route, seams, reason = ((), (), None)
        if not base.origins:
            access, reason = ('unknown', 'no_infantry_origins')
        elif terrain[index] is None:
            access, reason = ('unknown', 'unknown_property_terrain')
        elif count != lower:
            access, reason = ('unknown', 'unknown_terrain_or_teleport_route')
        elif count is None:
            access, reason = ('unreachable', 'no_foot_route_even_after_destroying_all_seams')
        else:
            access = 'foot_reachable' if count == 0 else 'seam_blocked'
            indices = []
            cursor = index
            while cursor is not None:
                indices.append(cursor)
                cursor = previous[cursor]
            indices.reverse()
            route = tuple(((i % base.width, i // base.width) for i in indices))
            seams = tuple(((i % base.width, i // base.width) for i in indices if terrain[i] == 'pipe_seam'))
        certified_count = count if access in {'foot_reachable', 'seam_blocked'} else None
        results.append(NeutralSeamAccess(building['id'], position, access, certified_count, seams, route, reason))
    return NeutralSeamAccessReport(base.width, base.height, base.player_id, base.origins, tuple(results), tuple(unknown_ownership))
