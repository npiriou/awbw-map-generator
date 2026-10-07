from __future__ import annotations
from dataclasses import dataclass
import math
from pathlib import Path
import torch
from torch import nn
from torch.nn import functional as F

@dataclass
class ModelConfig:
    layers: int = 5
    d_model: int = 256
    heads: int = 4
    ff_dim: int = 1024
    dropout: float = 0.1
    max_dimension: int = 50
    max_factions: int = 20
    hp_bins: int = 101
    diffusion_time: bool = False
    local_spatial: bool = False
    conditional_owners: bool = False
    normalized_geometry: bool = False
    geometry_amplitude: float = 0.02
    shared_condition_modulation: bool = False

    def __post_init__(self):
        if type(self.shared_condition_modulation) is not bool:
            raise ValueError('shared_condition_modulation must be a boolean')
        if type(self.normalized_geometry) is not bool:
            raise ValueError('normalized_geometry must be a boolean')
        if isinstance(self.geometry_amplitude, bool) or not isinstance(self.geometry_amplitude, (int, float)) or (not math.isfinite(self.geometry_amplitude)) or (self.geometry_amplitude < 0):
            raise ValueError('geometry_amplitude must be finite and nonnegative')
        if self.normalized_geometry and self.d_model % 4:
            raise ValueError('normalized geometry requires d_model divisible by four')

class SpatialMix(nn.Module):

    def __init__(self, config):
        super().__init__()
        self.canvas_dimension = config.max_dimension
        self.norm = nn.LayerNorm(config.d_model)
        self.depthwise = nn.Conv2d(config.d_model, config.d_model, 3, padding=1, groups=config.d_model)
        self.projection = nn.Conv2d(config.d_model, config.d_model, 1)

    def forward(self, hidden, valid, coordinates):
        coordinates = coordinates.masked_fill(~valid[:, :, None], 0)
        width = height = self.canvas_dimension
        indices = coordinates[:, :, 1] * width + coordinates[:, :, 0]
        batch, _, channels = hidden.shape
        image = hidden.new_zeros(batch, width * height, channels)
        image.scatter_add_(1, indices[:, :, None].expand(-1, -1, channels), self.norm(hidden) * valid[:, :, None])
        image = image.transpose(1, 2).reshape(batch, channels, height, width)
        mixed = self.projection(F.gelu(self.depthwise(image)))
        mixed = mixed.flatten(2).transpose(1, 2).gather(1, indices[:, :, None].expand(-1, -1, channels))
        return mixed * valid[:, :, None]

def _condition_affine(normalized, scale, shift):
    scale = scale.to(dtype=normalized.dtype)
    shift = shift.to(dtype=normalized.dtype)
    return normalized * (1 + scale[:, None, :]) + shift[:, None, :]

class TransformerBlock(nn.Module):

    def __init__(self, config):
        super().__init__()
        self.norm1 = nn.LayerNorm(config.d_model)
        self.norm2 = nn.LayerNorm(config.d_model)
        self.qkv = nn.Linear(config.d_model, 3 * config.d_model)
        self.projection = nn.Linear(config.d_model, config.d_model)
        self.ff = nn.Sequential(nn.Linear(config.d_model, config.ff_dim), nn.GELU(), nn.Dropout(config.dropout), nn.Linear(config.ff_dim, config.d_model))
        self.heads, self.dropout = (config.heads, config.dropout)
        if config.local_spatial:
            self.spatial = SpatialMix(config)

    def forward(self, hidden, valid, coordinates=None, modulation=None):
        if hasattr(self, 'spatial'):
            if coordinates is None:
                raise ValueError('local spatial mixing requires 2D coordinates')
            hidden = hidden + self.spatial(hidden, valid, coordinates)
        batch, length, channels = hidden.shape
        attention_normalized = self.norm1(hidden)
        if modulation is not None:
            attention_normalized = _condition_affine(attention_normalized, *modulation[:2])
        qkv = self.qkv(attention_normalized).view(batch, length, 3, self.heads, channels // self.heads)
        query, key, value = qkv.permute(2, 0, 3, 1, 4).unbind(0)
        attention_mask = None if bool(valid.all()) else valid[:, None, None, :]
        attended = F.scaled_dot_product_attention(query, key, value, attn_mask=attention_mask, dropout_p=self.dropout if self.training else 0)
        attended = attended.transpose(1, 2).contiguous().view(batch, length, channels)
        hidden = hidden + F.dropout(self.projection(attended), self.dropout, self.training)
        ff_normalized = self.norm2(hidden)
        if modulation is not None:
            ff_normalized = _condition_affine(ff_normalized, *modulation[2:])
        return hidden + F.dropout(self.ff(ff_normalized), self.dropout, self.training)

class MapGenerator(nn.Module):
    supports_field_masks = True

    def __init__(self, config, vocab, condition_dim):
        super().__init__()
        self.config = ModelConfig(**config) if isinstance(config, dict) else config
        config = self.config
        if config.d_model % config.heads:
            raise ValueError('d_model must be divisible by heads')
        self.vocab, self.condition_dim = (vocab, condition_dim)
        sizes = (len(vocab['tile_tokens']), config.max_factions + 1, len(vocab['unit_types']) + 1, config.max_factions + 1, config.hp_bins)
        self.field_sizes = sizes
        self.embeddings = nn.ModuleList((nn.Embedding(size + 1, config.d_model) for size in sizes))
        self.x_embedding = nn.Embedding(config.max_dimension, config.d_model)
        self.y_embedding = nn.Embedding(config.max_dimension, config.d_model)
        self.condition_encoder = nn.Sequential(nn.Linear(condition_dim, config.d_model), nn.GELU(), nn.Linear(config.d_model, config.d_model))
        if config.diffusion_time:
            self.time_projection = nn.Linear(config.d_model, config.d_model)
        self.blocks = nn.ModuleList((TransformerBlock(config) for _ in range(config.layers)))
        self.norm = nn.LayerNorm(config.d_model)
        self.output_heads = nn.ModuleDict({name: nn.Linear(config.d_model, size) for name, size in zip(('tile', 'owner', 'unit_type', 'unit_owner', 'hp'), sizes)})
        self.metadata_heads = nn.ModuleDict({'width': nn.Linear(config.d_model, config.max_dimension + 1), 'height': nn.Linear(config.d_model, config.max_dimension + 1), 'players': nn.Linear(config.d_model, config.max_factions + 1), 'faction_count': nn.Linear(config.d_model, config.max_factions + 1)})
        if config.conditional_owners:
            self.property_condition = nn.Linear(config.d_model, config.d_model, bias=False)
            self.unit_condition = nn.Linear(config.d_model, config.d_model, bias=False)
        self.apply(self._initialize)
        for block in self.blocks:
            if hasattr(block, 'spatial'):
                nn.init.zeros_(block.spatial.projection.weight)
                nn.init.zeros_(block.spatial.projection.bias)
        if config.conditional_owners:
            nn.init.zeros_(self.property_condition.weight)
            nn.init.zeros_(self.unit_condition.weight)
        if config.diffusion_time:
            nn.init.zeros_(self.time_projection.weight)
            nn.init.zeros_(self.time_projection.bias)
        if config.shared_condition_modulation:
            cpu_rng = torch.get_rng_state()
            try:
                self.condition_modulation = nn.Linear(config.d_model, 4 * config.d_model, device='cpu', dtype=self.x_embedding.weight.dtype)
                nn.init.zeros_(self.condition_modulation.weight)
                nn.init.zeros_(self.condition_modulation.bias)
            finally:
                torch.set_rng_state(cpu_rng)
            self.condition_modulation.to(device=self.x_embedding.weight.device)

    @staticmethod
    def _initialize(module):
        if isinstance(module, (nn.Linear, nn.Embedding)):
            nn.init.normal_(module.weight, mean=0, std=0.02)
            if isinstance(module, nn.Linear) and module.bias is not None:
                nn.init.zeros_(module.bias)

    def predict_metadata(self, conditions):
        hidden = self.condition_encoder(conditions)
        return {name: head(hidden) for name, head in self.metadata_heads.items()}

    def ownership_logits(self, hidden, tile_ids, unit_type_ids):
        if not self.config.conditional_owners:
            return {name: self.output_heads[name](hidden) for name in ('owner', 'unit_owner', 'hp')}
        property_hidden = hidden + self.property_condition(self.embeddings[0](tile_ids))
        unit_hidden = hidden + self.unit_condition(self.embeddings[2](unit_type_ids))
        return {'owner': self.output_heads['owner'](property_hidden), 'unit_owner': self.output_heads['unit_owner'](unit_hidden), 'hp': self.output_heads['hp'](unit_hidden)}

    def forward(self, grid, conditions, valid_mask=None, masked=None, diffusion_time=None):
        batch, length, _ = grid.shape
        valid_mask = torch.ones((batch, length), dtype=torch.bool, device=grid.device) if valid_mask is None else valid_mask
        masked = torch.zeros_like(valid_mask) if masked is None else masked
        if masked.shape not in {(batch, length), (batch, length, 5)} or masked.dtype != torch.bool:
            raise ValueError('masked must be a boolean cell mask or five-field mask')
        hidden = self.x_embedding(grid[:, :, 5]) + self.y_embedding(grid[:, :, 6])
        if self.config.normalized_geometry:
            from tools.map_model.normalized_geometry import add_normalized_geometry
            hidden = add_normalized_geometry(hidden, grid[:, :, 5:7], valid_mask, enabled=True, amplitude=self.config.geometry_amplitude)
        for field, (embedding, size) in enumerate(zip(self.embeddings, self.field_sizes)):
            field_mask = masked[:, :, field] if masked.ndim == 3 else masked
            tokens = grid[:, :, field].masked_fill(field_mask, size)
            hidden = hidden + embedding(tokens)
        condition_hidden = self.condition_encoder(conditions)
        modulation = self.condition_modulation(condition_hidden).chunk(4, dim=-1) if self.config.shared_condition_modulation else None
        hidden = hidden + condition_hidden[:, None, :]
        if self.config.diffusion_time:
            if diffusion_time is None or diffusion_time.shape != (batch,):
                raise ValueError('diffusion model requires one normalized timestep per map')
            if not torch.isfinite(diffusion_time).all() or (diffusion_time < 0).any() or (diffusion_time > 1).any():
                raise ValueError('diffusion timestep must be finite and between zero and one')
            half = self.config.d_model // 2
            frequencies = torch.exp(-math.log(10000) * torch.arange(half, device=grid.device) / max(1, half - 1))
            angles = diffusion_time.float()[:, None] * 1000 * frequencies[None, :]
            encoded = torch.cat((angles.sin(), angles.cos()), dim=-1)
            if encoded.shape[-1] < self.config.d_model:
                encoded = F.pad(encoded, (0, self.config.d_model - encoded.shape[-1]))
            hidden = hidden + self.time_projection(encoded)[:, None, :]
        elif diffusion_time is not None:
            raise ValueError('checkpoint has no diffusion timestep conditioning')
        for block in self.blocks:
            if modulation is None:
                hidden = block(hidden, valid_mask, grid[:, :, 5:7])
            else:
                hidden = block(hidden, valid_mask, grid[:, :, 5:7], modulation=modulation)
        hidden = self.norm(hidden)
        result = {name: head(hidden) for name, head in self.output_heads.items()}
        if self.config.conditional_owners:
            result.update(self.ownership_logits(hidden, grid[:, :, 0], grid[:, :, 2]))
            result['cell_hidden'] = hidden
        result.update({name: head(condition_hidden) for name, head in self.metadata_heads.items()})
        return result

def load_checkpoint(path, device='cpu'):
    checkpoint = torch.load(Path(path), map_location=device, weights_only=True)
    model = MapGenerator(checkpoint['config'], checkpoint['vocab'], checkpoint['condition_dim'])
    model.load_state_dict(checkpoint['state_dict'])
    model.to(device).eval()
    return (model, checkpoint)
