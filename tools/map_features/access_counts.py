from __future__ import annotations
from collections import Counter
from typing import Any
from .common import canonical_map, movement_reference, neighbors
from .vendor_access import analyze_island_transport, analyze_property_access
from .vendor_access.island_transport import IslandTransportSite
CAPTURABLE_KINDS = ('city', 'base', 'airport', 'port', 'hq', 'com_tower', 'lab')
BUILDING_KINDS = (*CAPTURABLE_KINDS, 'silo', 'silo_empty')
ISLAND_CRITERION = "sea-separated from initial HQ/base footholds (capturer fallback for players without HQ/base), and accessible by that player's usable transports, allowing destruction of breakable seams"

def _position(site: dict) -> tuple[int, int]:
    return (site['position']['x'], site['position']['y'])

def _sources(data: dict) -> tuple[list[dict], list[dict], list[int], list[dict]]:
    starts = [site for site in data['buildings'] if site['owner'] is not None and site['kind'] in {'hq', 'base'}]
    base_players = {site['owner'] for site in starts}
    players = {site['owner'] for site in data['buildings'] if site['owner'] is not None}
    players.update((unit['owner'] for unit in data.get('units', ()) if unit.get('owner') is not None))
    capturers, stranded = ([], [])
    costs = movement_reference()['costs']['clear']
    for unit in data.get('units', ()):
        if unit.get('owner') is None or unit.get('unit_type') not in {'infantry', 'mech'} or unit.get('position') is None or (unit.get('transport') is not None):
            continue
        x, y = _position(unit)
        tile = data['terrain'][y * data['width'] + x]
        if tile is None or costs[tile]['foot'] is not None:
            capturers.append(unit)
            continue
        exits = [(nx, ny) for nx, ny in neighbors(x, y, data['width'], data['height']) if data['terrain'][ny * data['width'] + nx] == 'pipe_seam' or costs.get(data['terrain'][ny * data['width'] + nx], {}).get('foot') is not None]
        capturers.extend(({**unit, 'position': {'x': nx, 'y': ny}, 'departure_position': unit['position']} for nx, ny in exits))
        if not exits:
            stranded.append({'id': unit['id'], 'owner': unit['owner'], 'unit_type': unit['unit_type'], 'position': unit['position'], 'reason': 'no_passable_capture_origin_or_adjacent_escape'})
    audit = [*starts, *(unit for unit in capturers if unit['owner'] not in base_players and costs.get(data['terrain'][_position(unit)[1] * data['width'] + _position(unit)[0]], {}).get('foot') is not None)]
    return (audit, [*starts, *capturers], sorted(players), stranded)

def _combine(reports: dict[int, Any], field: str, *, sources_complete: bool) -> dict:
    result = {}
    priority = {'reachable': 0, 'unknown': 1, 'inaccessible': 2}
    for report in reports.values():
        for site in getattr(report, field):
            old = result.get(site.position)
            if old is None or priority[site.access] < priority[old.access]:
                result[site.position] = site
    if not sources_complete:
        result = {p: IslandTransportSite(p, 'unknown', None, 'incomplete_player_capture_origins') if site.access == 'inaccessible' else site for p, site in result.items()}
    return result

def _record(site: dict, access: IslandTransportSite | None) -> dict:
    return {'id': site['id'], 'kind': site['kind'], 'owner': site['owner'], 'position': site['position'], 'access': access.access if access else 'unknown', 'mode': access.mode if access else None, 'reason': access.reason if access else 'no_player_capture_origins'}

def analyze_access_counts(payload: dict) -> dict:
    data = canonical_map(payload)
    data = {**data, 'buildings': [site for site in data['buildings'] if site['kind'] in CAPTURABLE_KINDS]}
    audit_sources, capture_sources, players, stranded_capturers = _sources(data)
    expected_players = payload.get('Player Count')
    source_players = {site['owner'] for site in capture_sources}
    sources_complete = type(expected_players) is int and expected_players > 0 and (len(players) == expected_players) and (source_players == set(players))
    collective = analyze_property_access(data, origins=[site['position'] for site in audit_sources])
    capture_grid = {**data, 'terrain': tuple(('plain' if tile == 'pipe_seam' else tile for tile in data['terrain']))}
    player_reports = {}
    for player in players:
        foot = analyze_property_access(capture_grid, player_id=player, origins=[site['position'] for site in capture_sources if site['owner'] == player])
        player_reports[player] = analyze_island_transport(data, property_report=foot, player_id=player)
    properties = _combine(player_reports, 'sites', sources_complete=sources_complete)
    silos = _combine(player_reports, 'silos', sources_complete=sources_complete)
    by_id = {site['id']: site for site in data['buildings']}
    island_sites, island_unknown = ([], [])
    for geometric in collective.properties:
        site = by_id[geometric.building_id]
        access = properties.get(geometric.position)
        if geometric.access == 'sea_separated':
            item = {**_record(site, access), 'infantry_region': geometric.infantry_region}
            if access and access.access == 'reachable':
                island_sites.append(item)
            elif not access or access.access == 'unknown':
                island_unknown.append(item)
        elif geometric.access == 'unknown':
            island_unknown.append({**_record(site, access), 'geometric_reason': geometric.reason})
    tag = 'matched' if island_sites else 'unresolved' if island_unknown else 'not_matched'
    counters = {kind: Counter() for kind in BUILDING_KINDS}
    excluded, unknown = ([], [])
    all_sites = list(data['buildings'])
    for index, terrain in enumerate(data['terrain']):
        if terrain in {'silo', 'silo_empty'}:
            all_sites.append({'id': index, 'kind': terrain, 'owner': None, 'position': {'x': index % data['width'], 'y': index // data['width']}})
    for site in all_sites:
        counts = counters[site['kind']]
        counts['raw_total'] += 1
        if site['owner'] is not None:
            counts['pre_owned'] += 1
            continue
        access = (silos if site['kind'] in {'silo', 'silo_empty'} else properties).get(_position(site))
        state = access.access if access else 'unknown'
        if state == 'reachable':
            counts['neutral'] += 1
        elif state == 'inaccessible':
            counts['neutral_unreachable'] += 1
            excluded.append(_record(site, access))
        else:
            counts['neutral_unknown'] += 1
            unknown.append(_record(site, access))
    building_counts = {}
    for kind, counts in counters.items():
        uncertain = counts['neutral_unknown'] > 0
        building_counts[kind] = {'total': None if uncertain else counts['pre_owned'] + counts['neutral'], 'pre_owned': counts['pre_owned'], 'neutral': None if uncertain else counts['neutral'], 'neutral_unreachable': counts['neutral_unreachable'], 'neutral_unknown': counts['neutral_unknown'], 'raw_total': counts['raw_total']}
    return {'tags': {'island(s)': {'status': tag, 'evidence': {'criterion': ISLAND_CRITERION, 'island_properties': island_sites, 'unknown_properties': island_unknown, 'island_region_count': len({site['infantry_region'] for site in island_sites})}}}, 'building_counts': building_counts, 'evidence': {'access_criterion': "structural capture access by each player's own army; seams may open", 'island_origins': [{'owner': site['owner'], 'position': site['position']} for site in audit_sources], 'capture_origins': [{'owner': site['owner'], 'position': site['position']} | ({'departure_position': site['departure_position']} if 'departure_position' in site else {}) for site in capture_sources], 'stranded_capture_units': stranded_capturers, 'players': players, 'expected_players': expected_players, 'capture_origins_complete': sources_complete, 'units_complete': data.get('units_complete', False), 'building_access': [_record(site, (silos if site['kind'] in {'silo', 'silo_empty'} else properties).get(_position(site))) for site in all_sites], 'neutral_excluded': excluded, 'neutral_unknown': unknown, 'seams_assumed_destroyed': [{'x': index % data['width'], 'y': index // data['width']} for index, tile in enumerate(data['terrain']) if tile == 'pipe_seam'], 'silo_convention': 'silo and silo_empty are separate always-neutral structures'}}
