from __future__ import annotations
import math
import torch
from tools.tags import DESCRIPTIONS
from tools.map_constraints import OFFICIAL_CATEGORIES
from tools.map_features.access_counts import BUILDING_KINDS

def condition_fields(vocab):
    return [('width',), ('height',), ('players',)] + [('tags', name) for name in sorted(DESCRIPTIONS)] + [('building_counts', kind, field) for kind in sorted(BUILDING_KINDS) for field in ('total', 'pre_owned', 'neutral')] + [('predeployed_counts', kind) for kind in vocab['unit_types']] + [('categories', category) for category in sorted(OFFICIAL_CATEGORIES)]

def encode_settings(settings, vocab):
    values = []
    for path in condition_fields(vocab):
        value = settings
        present = True
        for component in path:
            if isinstance(value, dict) and component in value:
                value = value[component]
            elif isinstance(value, list) and component in value:
                value = True
            else:
                present = False
                break
        scaled = 0.0
        if present:
            if path[0] in {'tags', 'categories'}:
                scaled = float(value)
            elif path[0] in {'width', 'height'}:
                scaled = float(value) / 50
            elif path[0] == 'players':
                scaled = float(value) / 20
            else:
                scaled = math.log1p(value) / math.log1p(2500)
        values.extend((float(present), scaled))
    return torch.tensor(values, dtype=torch.float32)
