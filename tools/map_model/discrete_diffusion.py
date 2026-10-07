from __future__ import annotations
import math
import torch
_HEADS = ('tile', 'owner', 'unit_type', 'unit_owner', 'hp')
_METADATA = ('width', 'height', 'players', 'faction_count')

def _check_steps(steps):
    if type(steps) is not int or steps < 1:
        raise ValueError('diffusion steps must be a positive integer')

def alpha_bar(t, steps=64):
    _check_steps(steps)
    if not math.isfinite(t) or not 0 <= t <= steps:
        raise ValueError('diffusion time must be between 0 and steps')
    if t == 0:
        return 1.0
    if t == steps:
        return 0.0
    return math.cos(math.pi * t / (2 * steps)) ** 2

def reveal_probability(t, steps=64):
    _check_steps(steps)
    if type(t) is not int or not 1 <= t <= steps:
        raise ValueError('reverse diffusion time must be an integer in 1..steps')
    if t == 1:
        return 1.0
    survival = alpha_bar(t, steps)
    return min(1.0, max(0.0, (alpha_bar(t - 1, steps) - survival) / (1.0 - survival)))

def _check_guidance(scale):
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or (not math.isfinite(scale)) or (not 0 <= scale <= 8):
        raise ValueError('guidance_scale must be finite and between 0 and 8')

def _guided_categorical(conditional, unconditional, scale):
    if scale == 1:
        return conditional
    support = torch.isfinite(conditional) & torch.isfinite(unconditional)
    if not bool(support.any(-1).all()):
        raise ValueError('conditional and unconditional categorical support do not intersect')
    cond = conditional.float().masked_fill(~support, -torch.inf).log_softmax(-1)
    uncond = unconditional.float().masked_fill(~support, -torch.inf).log_softmax(-1)
    cond, uncond = (cond.masked_fill(~support, 0), uncond.masked_fill(~support, 0))
    return (uncond + scale * (cond - uncond)).masked_fill(~support, -torch.inf)

def _clean_logits(model, grid, conditions, valid, masked, diffusion_time, guidance_scale=1.0, unconditional_conditions=None, editor_locks=None):
    from tools.map_model.sample import _editor_model_mask
    masked = _editor_model_mask(model, masked, editor_locks)
    conditional = model(grid, conditions, valid, masked, diffusion_time=diffusion_time)
    if guidance_scale == 1:
        return conditional
    if unconditional_conditions is None or unconditional_conditions.shape != conditions.shape:
        raise ValueError('guidance requires an unconditional condition vector of the same shape')
    unconditional = model(grid, unconditional_conditions, valid, masked, diffusion_time=diffusion_time)
    result = dict(conditional)
    for head in _HEADS:
        result[head] = _guided_categorical(conditional[head], unconditional[head], guidance_scale)
    if 'cell_hidden' in unconditional:
        result['unconditional_cell_hidden'] = unconditional['cell_hidden']
    return result

def _ownership_logits(model, logits, indices, tile_ids, unit_ids, guidance_scale):
    if not getattr(getattr(model, 'config', None), 'conditional_owners', False):
        return {head: logits[head][:, indices] for head in ('owner', 'unit_owner', 'hp')}
    conditional = model.ownership_logits(logits['cell_hidden'][:, indices], tile_ids, unit_ids)
    if guidance_scale == 1:
        return conditional
    unconditional = model.ownership_logits(logits['unconditional_cell_hidden'][:, indices], tile_ids, unit_ids)
    return {head: _guided_categorical(conditional[head], unconditional[head], guidance_scale) for head in ('owner', 'unit_owner', 'hp')}

def _structured_plan(logits, width, height, players, vocab, settings, group, forbidden_tokens, generator=None, temperature=1.0):
    from tools.map_model.sample import _building_token, _orbits, _quota_orbits
    tokens = vocab['tile_tokens']
    token_ids = {token: i for i, token in enumerate(tokens)}
    orbits = list(_orbits(width, height, group))
    hq = settings.get('building_counts', {}).get('hq', {})
    lab_only = hq.get('total') == 0 or hq.get('pre_owned') == 0
    anchor_kind = 'lab' if lab_only else 'hq'
    anchor = _building_token(anchor_kind)
    if anchor not in token_ids:
        raise ValueError('structured checkpoint does not include the requested active-player anchor')
    desired, requested = ({}, {})
    for kind, counts in settings.get('building_counts', {}).items():
        count = counts.get('total')
        if count is None and 'pre_owned' in counts and ('neutral' in counts):
            count = counts['pre_owned'] + counts['neutral']
        if count is not None:
            if _building_token(kind) not in token_ids:
                raise ValueError(f'checkpoint does not support building type {kind}')
            requested[kind] = desired[kind] = count
        elif 'pre_owned' in counts or 'neutral' in counts:
            token = _building_token(kind)
            if token not in token_ids:
                raise ValueError(f'checkpoint does not support building type {kind}')
            desired[kind] = counts.get('pre_owned', 0) + counts.get('neutral', 0)
    if lab_only:
        desired['hq'] = 0
    desired[anchor_kind] = max(players, desired.get(anchor_kind, players))
    ranking_scores = logits.detach().float().log_softmax(-1).cpu().tolist()
    orbit_for_cell = {index: orbit_index for orbit_index, orbit in enumerate(orbits) for index in orbit}
    noise = None
    tie_keys = [float(-orbit[0]) for orbit in orbits]
    if generator is not None:
        uniform = torch.rand((len(orbits), len(tokens) + 1), generator=generator, device=logits.device, dtype=torch.float64)
        tiny = torch.finfo(uniform.dtype).eps
        uniform = uniform.clamp(tiny, 1 - tiny)
        tie_keys = uniform[:, -1].cpu().tolist()
        noise = (-torch.log(-torch.log(uniform[:, :-1]))).cpu().tolist()

    def score(index, token):
        token_id = token_ids[token]
        value = ranking_scores[index][token_id] / temperature
        if noise is not None:
            value += noise[orbit_for_cell[index]][token_id]
        return value

    def tie_breaker(orbit):
        return tie_keys[orbit_for_cell[orbit[0]]]
    allocated = _quota_orbits(orbits, desired, score, anchor_kind, tie_breaker=tie_breaker)
    selected = allocated[anchor_kind]
    if len(selected) < players:
        ranked = sorted(orbits, key=lambda orbit: (sum((score(i, anchor) for i in orbit)) / len(orbit), tie_breaker(orbit)), reverse=True)
        for orbit in ranked:
            if set(orbit) <= selected:
                continue
            for positions in allocated.values():
                positions.difference_update(orbit)
            selected.update(orbit)
            if len(selected) >= players:
                break
    if len(selected) < players:
        raise ValueError('map has too few cells for active player anchors')
    for kind, minimum in desired.items():
        if kind in requested or kind == anchor_kind:
            continue
        chosen = allocated[kind]
        occupied = set().union(*allocated.values())
        available = [orbit for orbit in orbits if not occupied.intersection(orbit)]
        available.sort(key=lambda orbit: (sum((score(index, _building_token(kind)) for index in orbit)) / len(orbit), tie_breaker(orbit)), reverse=True)
        for orbit in available:
            if len(chosen) >= minimum:
                break
            chosen.update(orbit)
    forced = {i: token_ids[_building_token(kind)] for kind, positions in allocated.items() for i in positions}
    anchors = sorted(selected)
    roster_anchors = anchors[:players] if lab_only else anchors
    anchor_owners = {index: rank % players + 1 for rank, index in enumerate(roster_anchors)}
    suppressed = {_building_token(kind) for kind in requested} | {anchor}
    if lab_only:
        suppressed.add('property:hq')
    from tools.map_features.categories import STRUCTURAL_CATEGORY_TILE_IDS, category_preference
    category_residuals = []
    for category, tile_ids in STRUCTURAL_CATEGORY_TILE_IDS.items():
        if category_preference(settings, category) is not True:
            continue
        choices = [f'tile:{tile}' for tile in tile_ids if f'tile:{tile}' in token_ids and f'tile:{tile}' not in forbidden_tokens]
        available = [orbit for orbit in orbits if not any((index in forced for index in orbit))]
        if not choices or not available:
            category_residuals.append({'category': category, 'reason': 'no free terrain orbit for required category'})
            continue
        orbit, token = max(((orbit, token) for orbit in available for token in choices), key=lambda choice: sum((score(i, choice[1]) for i in choice[0])) / len(choice[0]))
        for index in orbit:
            forced[index] = token_ids[token]
    return (orbits, forced, anchor_owners, suppressed, requested, category_residuals, lab_only)

def _structured_unit_plan(logits, vocab, settings):
    requested = settings.get('predeployed_counts', {})
    size = logits.shape[1]
    forced = [-1] * size
    free = set(range(size))
    scores = logits[0].float().log_softmax(-1).detach().cpu().tolist()
    unit_ids = {kind: index + 1 for index, kind in enumerate(vocab['unit_types'])}
    for kind, count in sorted(requested.items(), key=lambda item: (-item[1], item[0])):
        if kind not in unit_ids:
            raise ValueError(f'checkpoint does not support unit type {kind}')
        ranked = sorted(free, key=lambda index: (scores[index][unit_ids[kind]], -index), reverse=True)
        selected = ranked[:count]
        for index in selected:
            forced[index] = unit_ids[kind]
        free.difference_update(selected)
    return (torch.tensor(forced, dtype=torch.long, device=logits.device), [unit_ids[kind] for kind in requested])

def _structured_owner_plan(model, logits, forced_tiles, anchor_owners, forced_units, vocab, settings, faction_count, guidance_scale):
    device, size = (logits['tile'].device, logits['tile'].shape[1])
    modes, defaults = ([-1] * size, [-1] * len(vocab['tile_tokens']))
    for kind, counts in settings.get('building_counts', {}).items():
        token = f'property:{kind}'
        if token not in vocab['tile_tokens'] or not {'pre_owned', 'neutral'} & counts.keys():
            continue
        token_id = vocab['tile_tokens'].index(token)
        positions = sorted((index for index, tile in forced_tiles.items() if tile == token_id))
        mandatory = {index for index in positions if index in anchor_owners}
        wanted = counts.get('pre_owned', max(0, len(positions) - counts.get('neutral', 0)))
        defaults[token_id] = 0 if 'pre_owned' in counts else 1
        owned = set(mandatory)
        available = [index for index in positions if index not in mandatory]
        needed = max(0, wanted - len(owned))
        if 0 < needed < len(available):
            indices = torch.tensor(available, dtype=torch.long, device=device)
            tiles = torch.full((1, len(available)), token_id, dtype=torch.long, device=device)
            units = forced_units[indices].clamp_min(0)[None]
            owner_logits = _ownership_logits(model, logits, indices, tiles, units, guidance_scale)['owner'][0].float()
            odds = (owner_logits[:, 1:faction_count + 1].logsumexp(-1) - owner_logits[:, 0]).detach().cpu().tolist()
            available = [index for _, index in sorted(zip(odds, available), key=lambda pair: (pair[0], -pair[1]), reverse=True)]
        owned.update(available[:needed])
        for index in positions:
            modes[index] = int(index in owned)
    return (torch.tensor(modes, dtype=torch.long, device=device), torch.tensor(defaults, dtype=torch.long, device=device))

def _sample_structured(model, conditions, width, height, faction_count, vocab, generator, temperature, steps, progress_callback, forbidden_tokens, guidance_scale, unconditional_conditions, settings, players):
    from tools.map_model.sample import _canonicalize_grid, _draw, _mask_tile_logits, _target_from_grid, symmetry_group, _building_token
    if not vocab.get('gameplay_type_only'):
        raise ValueError('structured diffusion requires a gameplay-type vocabulary')
    if type(players) is not int or not 1 <= players <= faction_count or players > width * height:
        raise ValueError('structured diffusion requires a valid active player count')
    group = symmetry_group(settings)
    if width != height and any((matrix[1] for matrix in group)):
        raise ValueError('diagonal symmetry requires a square map')
    size, device = (width * height, conditions.device)
    grid = torch.zeros((1, size, 7), dtype=torch.long, device=device)
    positions = torch.arange(size, device=device)
    grid[0, :, 5], grid[0, :, 6] = (positions % width, positions // width)
    valid, masked = (torch.ones((1, size), dtype=torch.bool, device=device), torch.ones((1, size), dtype=torch.bool, device=device))
    tokens, scores = (vocab['tile_tokens'], {})
    properties = torch.tensor([token.startswith('property:') for token in tokens], dtype=torch.bool, device=device)
    plan = None
    orbit_ids = orbit_roots = orbit_sizes = forced_orbits = anchor_slots = None
    forced_units = requested_unit_ids = ownership_modes = ownership_defaults = None
    for step, t in enumerate(range(steps, 0, -1), 1):
        diffusion_time = torch.tensor([t / steps], dtype=torch.float32, device=device)
        logits = _clean_logits(model, grid, conditions, valid, masked, diffusion_time, guidance_scale, unconditional_conditions)
        logits['tile'] = _mask_tile_logits(logits['tile'], vocab, forbidden_tokens)
        if plan is None:
            plan = _structured_plan(logits['tile'][0], width, height, players, vocab, settings, group, forbidden_tokens, generator=generator, temperature=temperature)
            orbits, forced, anchor_owners, _, _, _, _ = plan
            membership = [0] * size
            for orbit_index, orbit in enumerate(orbits):
                for index in orbit:
                    membership[index] = orbit_index
            orbit_ids = torch.tensor(membership, dtype=torch.long, device=device)
            orbit_roots = torch.tensor([orbit[0] for orbit in orbits], dtype=torch.long, device=device)
            orbit_sizes = torch.tensor([len(orbit) for orbit in orbits], dtype=torch.float32, device=device)
            forced_orbits = torch.tensor([forced.get(orbit[0], -1) for orbit in orbits], dtype=torch.long, device=device)
            anchor_slots = torch.tensor([anchor_owners.get(index, -1) for index in range(size)], dtype=torch.long, device=device)
            forced_units, requested_unit_ids = _structured_unit_plan(logits['unit_type'], vocab, settings)
            ownership_modes, ownership_defaults = _structured_owner_plan(model, logits, forced, anchor_owners, forced_units, vocab, settings, faction_count, guidance_scale)
        orbits, forced, anchor_owners, suppressed, requested, category_residuals, lab_only = plan
        reveal = (torch.rand((len(orbits),), generator=generator, device=device) < reveal_probability(t, steps)) & masked[0, orbit_roots]
        selected_orbits = reveal.nonzero(as_tuple=False).flatten()
        indices = reveal[orbit_ids].nonzero(as_tuple=False).flatten()
        pooled = logits['tile'].new_zeros((len(orbits), len(tokens)), dtype=torch.float32)
        pooled.index_add_(0, orbit_ids, logits['tile'][0].float())
        pooled = pooled / orbit_sizes[:, None]
        ranking_pooled = pooled.clone()
        unforced = forced_orbits < 0
        for token in suppressed:
            if token in tokens:
                pooled[unforced, tokens.index(token)] = -torch.inf
        forced_mask = ~unforced
        if bool(forced_mask.any()):
            keep = pooled[forced_mask, forced_orbits[forced_mask]].clone()
            pooled[forced_mask] = -torch.inf
            pooled[forced_mask, forced_orbits[forced_mask]] = keep
        if indices.numel():
            values, _ = _draw(pooled[selected_orbits], temperature, generator)
            drawn_tiles = torch.zeros((len(orbits),), dtype=torch.long, device=device)
            drawn_tiles[selected_orbits] = values
            grid[0, indices, 0] = drawn_tiles[orbit_ids[indices]]
            unit_logits = logits['unit_type'][:, indices].clone()
            if requested_unit_ids:
                unit_logits[..., requested_unit_ids] = -torch.inf
            planned_units = forced_units[indices]
            specified_units = planned_units >= 0
            if bool(specified_units.any()):
                keep = logits['unit_type'][0, indices[specified_units], planned_units[specified_units]].clone()
                unit_logits[0, specified_units] = -torch.inf
                unit_logits[0, specified_units, planned_units[specified_units]] = keep
            values, _ = _draw(unit_logits, temperature, generator)
            grid[:, indices, 2] = values
            owner_logits = _ownership_logits(model, logits, indices, grid[:, indices, 0], grid[:, indices, 2], guidance_scale)
            owner_logits['owner'] = owner_logits['owner'].clone()
            tile_ids = grid[0, indices, 0]
            property_cells = properties[tile_ids]
            roster_cells = tile_ids == tokens.index('property:hq')
            classes = torch.arange(owner_logits['owner'].shape[-1], device=device)[None, :]
            legal = property_cells[:, None] & (classes <= faction_count) | ~property_cells[:, None] & (classes == 0)
            legal &= ~roster_cells[:, None] | (classes >= 1) & (classes <= players)
            if lab_only:
                lab_cells = tile_ids == tokens.index('property:lab')
                legal &= ~lab_cells[:, None] | (classes <= players)
            specified = anchor_slots[indices]
            modes = ownership_modes[indices]
            modes = torch.where(modes < 0, ownership_defaults[tile_ids], modes)
            modes = torch.where(specified >= 0, torch.ones_like(modes), modes)
            legal &= (modes[:, None] != 0) | (classes == 0)
            legal &= (modes[:, None] != 1) | (classes >= 1)
            legal &= (specified[:, None] < 0) | (classes == specified[:, None])
            owner_logits['owner'][0].masked_fill_(~legal, -torch.inf)
            for column, head in ((1, 'owner'), (3, 'unit_owner'), (4, 'hp')):
                values, _ = _draw(owner_logits[head], temperature, generator, 1 if head in {'unit_owner', 'hp'} else None, faction_count if head in {'owner', 'unit_owner'} else 100)
                grid[:, indices, column] = values
            _canonicalize_grid(grid, properties)
        for head in ('tile', 'unit_type'):
            if head not in scores:
                scores[head] = torch.empty_like(logits[head][0], dtype=torch.float32)
            if indices.numel():
                scores[head][indices] = ranking_pooled[orbit_ids[indices]] if head == 'tile' else unit_logits[0].float()
        masked[0, indices] = False
        remaining = int(masked.sum().item())
        if progress_callback:
            progress_callback({'phase': 'sampling', 'step': step, 'steps': steps, 'diffusion_time': t / steps, 'remaining_cells': remaining, 'structured_sampling': True})
        if not remaining:
            break
    if bool(masked.any()):
        raise RuntimeError('the final structured diffusion transition left masked cells')
    target = _target_from_grid(grid, width, height, vocab)
    target['players'] = players
    largest_owner = max([players, *target['owners'], *(unit['owner'] for unit in target['units'])])
    target['factions'] = [{'slot': slot, 'active': slot <= players} for slot in range(1, largest_owner + 1)]
    residuals = [{'building': kind, 'requested_raw_tiles': count, 'actual_raw_tiles': target['tiles'].count(_building_token(kind)), 'reason': 'quota, symmetry and active-player anchors are not jointly representable'} for kind, count in requested.items() if target['tiles'].count(_building_token(kind)) != count]
    owner_residuals = []
    for kind, counts in settings.get('building_counts', {}).items():
        token = _building_token(kind)
        for field in ('pre_owned', 'neutral'):
            if field not in counts:
                continue
            actual = sum((tile == token and (owner > 0 if field == 'pre_owned' else owner == 0) for tile, owner in zip(target['tiles'], target['owners'])))
            if actual != counts[field]:
                owner_residuals.append({'building': kind, 'field': field, 'requested': counts[field], 'actual_raw': actual, 'reason': 'property capacity or protected active anchors prevent the raw ownership quota'})
    unit_residuals = [{'unit_type': kind, 'requested': count, 'actual': sum((unit['type'] == kind for unit in target['units'])), 'reason': 'requested deployments exceed available individual cells'} for kind, count in settings.get('predeployed_counts', {}).items() if sum((unit['type'] == kind for unit in target['units'])) != count]
    target['structured_sampling'] = {'tile_symmetry_in_sampler': len(group) > 1, 'owners_and_units_independent': True, 'active_roster_constructed_in_sampler': True, 'anchor_positions': sorted(anchor_owners), 'raw_quota_residuals': residuals, 'category_presence_residuals': category_residuals, 'raw_property_ownership_constructed_in_sampler': bool(settings.get('building_counts')), 'unit_counts_constructed_in_sampler': bool(settings.get('predeployed_counts')), 'raw_owner_quota_residuals': owner_residuals, 'unit_quota_residuals': unit_residuals, 'planned_unit_positions': {kind: (forced_units == index + 1).nonzero().flatten().cpu().tolist() for index, kind in enumerate(vocab['unit_types']) if kind in settings.get('predeployed_counts', {})}, 'constraint_construction': 'probability-weighted decoder plans; quota adherence is not evidence the network learned counts', 'property_position_policy': 'normalized orbit log-probabilities with seeded Gumbel sampling without replacement', 'neutral_accessibility_constructed': False}
    return (target, scores['tile'].cpu().tolist(), scores['unit_type'].cpu().tolist())

@torch.no_grad()
def sample_discrete(model, conditions, width, height, faction_count, vocab, generator, temperature, steps=64, progress_callback=None, forbidden_tokens=(), guidance_scale=1.0, unconditional_conditions=None, settings=None, structured=False, players=None, editor_locks=None):
    from tools.map_model.sample import _apply_sampling_locks, _canonicalize_grid, _draw, _mask_tile_logits, _target_from_grid
    _check_steps(steps)
    if type(width) is not int or type(height) is not int or min(width, height) < 1:
        raise ValueError('sample dimensions must be positive integers')
    if type(faction_count) is not int or not 1 <= faction_count <= 20:
        raise ValueError('faction_count must be an integer in 1..20')
    if conditions.ndim != 2 or conditions.shape[0] != 1:
        raise ValueError('discrete sampling requires one condition vector')
    if not isinstance(generator, torch.Generator):
        raise ValueError('discrete sampling requires a local torch.Generator')
    if not math.isfinite(temperature) or temperature <= 0:
        raise ValueError('temperature must be finite and positive')
    _check_guidance(guidance_scale)
    if structured and (not editor_locks):
        return _sample_structured(model, conditions, width, height, faction_count, vocab, generator, temperature, steps, progress_callback, forbidden_tokens, guidance_scale, unconditional_conditions, settings or {}, players)
    from tools.map_model.editor_locks import make_sampling_locks
    editor_locks = make_sampling_locks(editor_locks)
    size, device = (width * height, conditions.device)
    grid = torch.zeros((1, size, 7), dtype=torch.long, device=device)
    positions = torch.arange(size, device=device)
    grid[0, :, 5], grid[0, :, 6] = (positions % width, positions // width)
    valid = torch.ones((1, size), dtype=torch.bool, device=device)
    masked = valid.clone()
    _apply_sampling_locks(grid, editor_locks)
    fully_known = [index for index, fields in (editor_locks or {}).items() if all((column in fields for column in range(5)))]
    if fully_known:
        masked[0, fully_known] = False
    properties = torch.tensor([token.startswith('property:') for token in vocab['tile_tokens']], dtype=torch.bool, device=device)
    scores = {}
    if not bool(masked.any()):
        if progress_callback:
            progress_callback({'phase': 'sampling', 'step': 0, 'steps': steps, 'remaining_cells': 0, 'fully_specified_editor': True})
        return (_target_from_grid(grid, width, height, vocab), [[0.0] * len(vocab['tile_tokens']) for _ in range(size)], [[0.0] * (len(vocab['unit_types']) + 1) for _ in range(size)])
    for step, t in enumerate(range(steps, 0, -1), 1):
        diffusion_time = torch.tensor([t / steps], dtype=torch.float32, device=device)
        logits = _clean_logits(model, grid, conditions, valid, masked, diffusion_time, guidance_scale, unconditional_conditions, editor_locks)
        logits['tile'] = _mask_tile_logits(logits['tile'], vocab, forbidden_tokens)
        revealed = masked & (torch.rand(masked.shape, generator=generator, device=device) < reveal_probability(t, steps))
        indices = revealed[0].nonzero(as_tuple=False).flatten()
        if indices.numel():
            conditional_owners = getattr(getattr(model, 'config', None), 'conditional_owners', False)
            field_logits = logits
            if conditional_owners:
                for column, head in ((0, 'tile'), (2, 'unit_type')):
                    values, _ = _draw(logits[head][:, indices], temperature, generator)
                    grid[:, indices, column] = values
                _apply_sampling_locks(grid, editor_locks)
                owner_logits = _ownership_logits(model, logits, indices, grid[:, indices, 0], grid[:, indices, 2], guidance_scale)
                field_logits = dict(logits, **owner_logits)
            fields = ((1, 'owner'), (3, 'unit_owner'), (4, 'hp')) if conditional_owners else enumerate(_HEADS)
            for column, head in fields:
                values_logits = field_logits[head] if conditional_owners else field_logits[head][:, indices]
                values, _ = _draw(values_logits, temperature, generator, 1 if head in {'unit_owner', 'hp'} else None, faction_count if head in {'owner', 'unit_owner'} else None)
                grid[:, indices, column] = values
            _canonicalize_grid(grid, properties)
            _apply_sampling_locks(grid, editor_locks)
        for head in ('tile', 'unit_type'):
            if head not in scores:
                scores[head] = torch.zeros_like(logits[head][0], dtype=torch.float32)
            scores[head][indices] = logits[head][0, indices].float()
        masked = masked & ~revealed
        remaining = int(masked.sum().item())
        if progress_callback:
            progress_callback({'phase': 'sampling', 'step': step, 'steps': steps, 'diffusion_time': t / steps, 'remaining_cells': remaining})
        if not remaining:
            break
    if bool(masked.any()):
        raise RuntimeError('the final diffusion transition did not reveal every cell')
    return (_target_from_grid(grid, width, height, vocab), scores['tile'].cpu().tolist(), scores['unit_type'].cpu().tolist())
