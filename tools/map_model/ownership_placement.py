from __future__ import annotations
from collections import Counter, defaultdict
from copy import deepcopy
import heapq
import math
from tools.map_features.categories import category_preference
from tools.map_features.common import movement_reference, neighbors, terrain_name
_SPECIAL = ('Joke', 'Gimmick', 'Toy-Box', 'Sprite')

def _kind(token):
    return token.split(':', 1)[1] if token.startswith('property:') else None

def property_territory_distances(target):
    width, height = (target['width'], target['height'])
    size = width * height
    if len(target['tiles']) != size or len(target['owners']) != size:
        raise ValueError('incorrect ownership target grid length')
    active = sorted((faction['slot'] for faction in target['factions'] if faction['active']))
    anchor_kind = 'hq' if any((token == 'property:hq' and owner in active for token, owner in zip(target['tiles'], target['owners']))) else 'lab'
    anchors = {slot: [] for slot in active}
    for index, (token, owner) in enumerate(zip(target['tiles'], target['owners'])):
        if token == f'property:{anchor_kind}' and owner in anchors:
            anchors[owner].append(index)
    costs = movement_reference()['costs']['clear']
    terrain = ['property' if _kind(token) else terrain_name(int(token.split(':', 1)[1])) for token in target['tiles']]
    step_costs = [1 if family == 'teleport' else costs.get(family, {}).get('foot') for family in terrain]
    distances = {}
    for slot, origins in anchors.items():
        values = {index: 0 for index in origins}
        queue = [(0, index) for index in origins]
        heapq.heapify(queue)
        while queue:
            cost, index = heapq.heappop(queue)
            if cost != values[index]:
                continue
            for x, y in neighbors(index % width, index // width, width, height):
                destination = y * width + x
                step = step_costs[destination]
                if step is None:
                    continue
                candidate = cost + step
                if candidate < values.get(destination, math.inf):
                    values[destination] = candidate
                    heapq.heappush(queue, (candidate, destination))
        distances[slot] = values
    uncertainty = []
    if any((not positions for positions in anchors.values())):
        uncertainty.append('active_faction_has_no_hq_or_lab_origin')
    if 'teleport' in terrain:
        uncertainty.append('teleport_links_unknown')
    if 'pipe_seam' in terrain:
        uncertainty.append('routes_after_breaking_pipe_seams_not_modelled')
    if any((unit.get('type') in {'lander', 'transport_copter', 'black_boat'} for unit in target.get('units', []))):
        uncertainty.append('transported_capture_routes_not_modelled')
    return {'active_slots': active, 'anchor_kind': anchor_kind, 'anchors': anchors, 'distances': distances, 'uncertainty': uncertainty, 'method': 'clear_weather_foot_cost_from_owned_hq_or_lab_no_teleport_shortcuts'}

def evaluate_property_territory(target, settings=None):
    routes = property_territory_distances(target)
    active, distances = (routes['active_slots'], routes['distances'])
    conflicts, ties, unreachable, reviewed = ([], [], [], 0)
    for index, (token, owner) in enumerate(zip(target['tiles'], target['owners'])):
        kind = _kind(token)
        if not kind or kind in {'hq', 'lab'} or owner not in active:
            continue
        reviewed += 1
        reachable = {slot: values[index] for slot, values in distances.items() if index in values}
        site = {'index': index, 'position': {'x': index % target['width'], 'y': index // target['width']}, 'building': kind, 'owner': owner}
        if not reachable:
            unreachable.append({**site, 'reason': 'no_anchor_foot_route'})
            continue
        closest = min(reachable.values())
        closest_slots = sorted((slot for slot, cost in reachable.items() if cost == closest))
        own = reachable.get(owner)
        if own is None or own > closest:
            conflicts.append({**site, 'owner_foot_cost': own, 'closest_slots': closest_slots, 'closest_foot_cost': closest, 'reason': 'enemy_anchor_has_shorter_foot_route'})
        elif len(closest_slots) > 1:
            ties.append({**site, 'closest_slots': closest_slots, 'foot_cost': own})
    return {'conflict_count': len(conflicts), 'conflicts': conflicts, 'tie_count': len(ties), 'ties': ties, 'unreachable_count': len(unreachable), 'unreachable': unreachable, 'reviewed_owned_properties': reviewed, 'active_slots': active, 'anchor_kind': routes['anchor_kind'], 'uncertainty': routes['uncertainty'], 'method': routes['method'], 'balance_certified': False}

def _standard_two_player(target, settings):
    if len([f for f in target['factions'] if f['active']]) != 2:
        return False
    if category_preference(settings, 'Standard') is False and category_preference(settings, 'Fog of War') is not True:
        return False
    if settings.get('tags', {}).get('1vX team play') is True:
        return False
    return not any((category_preference(settings, name) is True for name in (*_SPECIAL, 'Team Play', 'FFA Multiplay')))

def _locked_positions(editor_locks, preserve_positions, size):
    locked = set(preserve_positions)
    for index, fields in (editor_locks or {}).items():
        if {0, 1, 'tile', 'owner'}.intersection(fields):
            locked.add(index)
    if any((type(index) is not int or not 0 <= index < size for index in locked)):
        raise ValueError('ownership lock must be a valid row-major position')
    return locked

def _assign(positions, capacities, cost):
    slots = sorted((slot for slot, count in capacities.items() if count))
    if not slots:
        return {}
    if sum(capacities.values()) > len(positions):
        raise ValueError('ownership capacities exceed available property positions')
    if len(slots) == 1:
        slot = slots[0]
        return {index: slot for index in sorted(positions, key=lambda index: (cost(slot, index), index))[:capacities[slot]]}
    if len(slots) == 2:
        first, second = slots
        ranked = [(cost(first, index), cost(second, index), index) for index in positions]
        ranked.sort(key=lambda item: (item[0] - item[1], item[2]))
        count = len(ranked)

        def half_sums(quota, column, backwards=False):
            values, heap, total = ([math.inf] * (count + 1), [], 0)
            values[count if backwards else 0] = 0 if quota == 0 else math.inf
            for number in range(count - 1, -1, -1) if backwards else range(count):
                value = ranked[number][column]
                heapq.heappush(heap, (-value, -ranked[number][2]))
                total += value
                if len(heap) > quota:
                    total += heapq.heappop(heap)[0]
                if len(heap) == quota:
                    values[number if backwards else number + 1] = total
            return values
        prefix = half_sums(capacities[first], 0)
        suffix = half_sums(capacities[second], 1, backwards=True)
        boundary = min(range(capacities[first], count - capacities[second] + 1), key=lambda index: (prefix[index] + suffix[index], index))
        assigned = {item[2]: first for item in sorted(ranked[:boundary], key=lambda item: (item[0], item[2]))[:capacities[first]]}
        assigned.update({item[2]: second for item in sorted(ranked[boundary:], key=lambda item: (item[1], item[2]))[:capacities[second]]})
        return assigned
    source, owner_start = (0, 1)
    position_start = owner_start + len(slots)
    sink = position_start + len(positions)
    graph = [[] for _ in range(sink + 1)]

    def edge(start, end, capacity, value):
        forward = [end, len(graph[end]), capacity, value]
        graph[start].append(forward)
        graph[end].append([start, len(graph[start]) - 1, 0, -value])
        return forward
    assignments = []
    for offset, slot in enumerate(slots):
        node = owner_start + offset
        edge(source, node, capacities[slot], 0)
        for place, index in enumerate(positions):
            assignments.append((slot, index, edge(node, position_start + place, 1, cost(slot, index))))
    for place in range(len(positions)):
        edge(position_start + place, sink, 1, 0)
    potential = [0] * len(graph)
    for _ in range(sum(capacities.values())):
        distance, previous = ([math.inf] * len(graph), [None] * len(graph))
        distance[source] = 0
        queue = [(0, source)]
        while queue:
            value, node = heapq.heappop(queue)
            if value != distance[node]:
                continue
            for number, candidate in enumerate(graph[node]):
                destination, _, capacity, weight = candidate
                if not capacity:
                    continue
                new = value + weight + potential[node] - potential[destination]
                if new < distance[destination]:
                    distance[destination], previous[destination] = (new, (node, number))
                    heapq.heappush(queue, (new, destination))
        if previous[sink] is None:
            raise ValueError('ownership capacities exceed available property positions')
        for node, value in enumerate(distance):
            if value != math.inf:
                potential[node] += value
        node = sink
        while node != source:
            parent, number = previous[node]
            candidate = graph[parent][number]
            candidate[2] -= 1
            graph[node][candidate[1]][2] += 1
            node = parent
    return {index: slot for slot, index, candidate in assignments if candidate[2] == 0}

def repair_property_ownership(target, settings, *, editor_locks=None, preserve_positions=(), exclude_buildings=()):
    result = deepcopy(target)
    if settings.get('tags', {}).get('immobile pre-deploy') is True or any((category_preference(settings, name) is True for name in _SPECIAL)):
        return (result, [{'kind': 'property_territory_placement_skipped', 'reason': 'intentional_special_or_immobile_layout'}])
    routes = property_territory_distances(result)
    active, distances = (routes['active_slots'], routes['distances'])
    if len(active) < 2:
        return (result, [])
    if any((not origins for origins in routes['anchors'].values())):
        return (result, [{'kind': 'property_territory_placement_skipped', 'reason': 'missing_active_anchor', 'uncertainty': routes['uncertainty']}])
    size, width, height = (result['width'] * result['height'], result['width'], result['height'])
    frozen = _locked_positions(editor_locks, preserve_positions, size)
    for index, (token, owner) in enumerate(zip(result['tiles'], result['owners'])):
        if _kind(token) in {'hq', 'lab'} or owner not in {*active, 0} or (not any((index in distances[slot] for slot in active))):
            frozen.add(index)
    by_kind = defaultdict(list)
    for index, token in enumerate(result['tiles']):
        kind = _kind(token)
        if kind and kind not in {'hq', 'lab', *exclude_buildings}:
            by_kind[kind].append(index)
    balanced = _standard_two_player(result, settings)
    evidence = []
    before = evaluate_property_territory(result)
    conflict_positions = {site['index'] for site in before['conflicts']}
    for kind, positions in sorted(by_kind.items()):
        inventory = Counter((result['owners'][index] for index in positions if result['owners'][index] in active))
        fixed = Counter((result['owners'][index] for index in positions if index in frozen and result['owners'][index] in active))
        quotas = {slot: inventory[slot] for slot in active}
        wanted = dict(quotas)
        if balanced:
            total = sum(quotas.values())
            first, second = active
            extra = first if inventory[first] >= inventory[second] else second
            wanted = {slot: total // 2 + int(total % 2 and slot == extra) for slot in active}
            low, high = (fixed[first], total - fixed[second])
            first_count = max(low, min(high, wanted[first]))
            quotas = {first: first_count, second: total - first_count}
            if quotas != wanted:
                evidence.append({'kind': 'property_ownership_capacity_residual', 'building': kind, 'requested_balanced_active_counts': wanted, 'feasible_active_counts': quotas, 'frozen_active_counts': dict(fixed), 'reason': 'immutable_properties_prevent_equal_allocation'})
        available = [index for index in positions if index not in frozen]
        capacities = {slot: quotas[slot] - fixed[slot] for slot in active}
        if quotas == {slot: inventory[slot] for slot in active} and (not conflict_positions.intersection(available)):
            continue
        owned_count = sum(capacities.values())
        position_set = set(positions)
        maximum_distance = max((value for slot in active for index, value in distances[slot].items() if index in position_set), default=0) + width + height + 1
        change_weight = maximum_distance * (owned_count + 1)
        conflict_weight = (2 * change_weight + maximum_distance) * (owned_count + 1)

        def cost(slot, index):
            reachable = {owner: distances[owner][index] for owner in active if index in distances[owner]}
            nearest = min(reachable.values())
            distance = reachable.get(slot)
            conflict = distance is None or distance > nearest
            if distance is None:
                distance = maximum_distance
            old = result['owners'][index]
            change = int(old != slot) - int(old != 0) + 1
            return int(conflict) * conflict_weight + change * change_weight + distance
        assignments = _assign(available, capacities, cost)
        changes = []
        for index in available:
            owner = assignments.get(index, 0)
            if owner != result['owners'][index]:
                changes.append({'index': index, 'position': {'x': index % width, 'y': index // width}, 'from_owner': result['owners'][index], 'to_owner': owner})
                result['owners'][index] = owner
        if changes:
            evidence.append({'kind': 'property_territory_ownership', 'building': kind, 'changes': changes, 'active_counts_before': {slot: inventory[slot] for slot in active}, 'active_counts_after': quotas, 'balanced_standard_two_player': balanced, 'preserved_raw_pre_owned_neutral_counts': True, 'preserved_anchor_owners': True, 'method': routes['method'], 'balance_certified': False})
    after = evaluate_property_territory(result)
    if after['conflict_count']:
        evidence.append({'kind': 'property_territory_residual', 'conflict_count': after['conflict_count'], 'conflicts': after['conflicts'], 'reason': 'immutable_properties_or_preserved_owner_capacities', 'balance_certified': False})
    if evidence:
        evidence.append({'kind': 'property_territory_summary', 'before_conflicts': before['conflict_count'], 'after_conflicts': after['conflict_count'], 'ties': after['tie_count'], 'unreachable': after['unreachable_count'], 'uncertainty': routes['uncertainty'], 'balance_certified': False})
    return (result, evidence)
