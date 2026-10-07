from __future__ import annotations
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any
from ..common import movement_reference
from .property_access import Position, PropertyAccessReport, _canonical_grid, _known, _position, analyze_property_access

@dataclass(frozen=True)
class IslandTransportSite:
    position: Position
    access: str
    mode: str | None
    reason: str | None

@dataclass(frozen=True)
class IslandTransportReport:
    sites: tuple[IslandTransportSite, ...]
    silos: tuple[IslandTransportSite, ...]
    seams_assumed_destroyed: tuple[Position, ...]

    def to_dict(self):
        return asdict(self)

def analyze_island_transport(data: Mapping[str, Any], *, property_report: PropertyAccessReport | None=None, player_id: int | None=None, origins=None, allow_seams: bool=True) -> IslandTransportReport:
    report = property_report or analyze_property_access(data, player_id=player_id, origins=origins)
    width, height, original = _canonical_grid(data)
    sites = data.get('buildings', ())
    by_position = {_position(site['position']): site for site in sites}
    kinds = {p: _known(site.get('kind')) for p, site in by_position.items()}
    opened = tuple(((i % width, i // width) for i, tile in enumerate(original) if allow_seams and tile == 'pipe_seam'))
    terrain = tuple(('plain' if allow_seams and tile == 'pipe_seam' else tile for tile in original))
    reference = movement_reference()
    costs, property_costs = (reference['costs']['clear'], reference['property_costs']['clear'])

    def neighbors(index):
        x, y = (index % width, index // width)
        return tuple((ny * width + nx for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)) if 0 <= nx < width and 0 <= ny < height))

    def labels_for(movement):
        labels = [None] * len(terrain)
        passable = []
        for i, tile in enumerate(terrain):
            kind = kinds.get((i % width, i // width))
            classes = property_costs.get(kind, costs.get(tile, {})) if tile == 'property' else costs.get(tile, {})
            passable.append(classes.get(movement) is not None)
        label = 0
        for start in range(len(terrain)):
            if not passable[start] or labels[start] is not None:
                continue
            labels[start] = label
            pending = deque([start])
            while pending:
                index = pending.popleft()
                for neighbor in neighbors(index):
                    if passable[neighbor] and labels[neighbor] is None:
                        labels[neighbor] = label
                        pending.append(neighbor)
            label += 1
        return labels
    foot, naval, air = (labels_for(movement) for movement in ('foot', 'lander', 'air'))
    reached = {foot[y * width + x] for x, y in report.origins}
    reached.discard(None)
    modes = {region: 'foot_after_seams' if opened else 'foot' for region in reached}
    acting_player = player_id if player_id is not None else report.player_id
    actors = {acting_player} if acting_player is not None else {_known(site.get('owner', site.get('initial_owner'))) for site in sites if _known(site.get('owner', site.get('initial_owner'))) is not None}
    if not actors:
        actors = {unit.get('owner') for unit in data.get('units', ()) if unit.get('owner') is not None}
    endpoints = {'naval': defaultdict(set), 'air': defaultdict(set)}
    for index, region in enumerate(foot):
        if region is None or not any((foot[n] == region for n in neighbors(index))):
            continue
        position = (index % width, index // width)
        if naval[index] is not None and (terrain[index] == 'shoal' or kinds.get(position) == 'port'):
            endpoints['naval'][naval[index]].add(region)
        if air[index] is not None and terrain[index] not in {'sea', 'reef'}:
            endpoints['air'][air[index]].add(region)
    predeployed = {'naval': set(), 'air': set()}
    inventory_known = data.get('units_complete', 'units' in data and 'viewer' not in data)
    for unit in data.get('units', ()):
        if _known(unit.get('owner')) not in actors or unit.get('position') is None:
            continue
        unit_type = _known(unit.get('unit_type'))
        if unit_type not in {'lander', 'black_boat', 'transport_copter'}:
            continue
        x, y = _position(unit['position'])
        if not (0 <= x < width and 0 <= y < height):
            raise ValueError('predeployed transport position is outside map')
        mode = 'air' if unit_type == 'transport_copter' else 'naval'
        labels = air if mode == 'air' else naval
        component = labels[y * width + x]
        if component is not None:
            predeployed[mode].add(component)
        else:
            predeployed[mode].update((labels[n] for n in neighbors(y * width + x) if labels[n] is not None))
    changed = True
    bans = set(data.get('config', {}).get('banned_units', ()))
    while changed:
        changed = False
        available = {mode: set(components) for mode, components in predeployed.items()}
        for position, site in by_position.items():
            kind = kinds[position]
            if kind not in {'airport', 'port'}:
                continue
            if kind == 'airport' and 'transport_copter' in bans:
                continue
            if kind == 'port' and {'lander', 'black_boat'} <= bans:
                continue
            x, y = position
            index = y * width + x
            owner = _known(site.get('owner', site.get('initial_owner')))
            if owner not in actors and foot[index] not in reached:
                continue
            mode = 'air' if kind == 'airport' else 'naval'
            component = (air if mode == 'air' else naval)[index]
            if component is not None:
                available[mode].add(component)
        for mode, components in available.items():
            for component in components:
                regions = endpoints[mode][component]
                if not regions & reached:
                    continue
                for region in regions - reached:
                    reached.add(region)
                    modes[region] = mode
                    changed = True
    incomplete = not report.origins or not inventory_known or any((tile is None for tile in original)) or any((tile == 'property' and kinds.get((i % width, i // width)) is None for i, tile in enumerate(original))) or any((kinds[position] in {'airport', 'port'} and ('owner' not in site and 'initial_owner' not in site or (isinstance(site.get('owner', site.get('initial_owner')), Mapping) and site.get('owner', site.get('initial_owner', {})).get('knowledge') != 'known')) for position, site in by_position.items()))

    def classify(position):
        x, y = position
        region = foot[y * width + x]
        if region is not None and region in reached:
            return IslandTransportSite(position, 'reachable', modes[region], None)
        if incomplete or 'teleport' in original:
            return IslandTransportSite(position, 'unknown', None, 'incomplete_transport_or_teleport_evidence')
        return IslandTransportSite(position, 'inaccessible', None, 'no_usable_transport_or_capture_route')
    return IslandTransportReport(tuple((classify(site.position) for site in report.properties)), tuple((classify((i % width, i // width)) for i, tile in enumerate(original) if tile in {'silo', 'silo_empty'})), opened)
