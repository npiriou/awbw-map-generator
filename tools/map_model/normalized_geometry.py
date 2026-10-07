from __future__ import annotations
import math
import torch
DEFAULT_AMPLITUDE = 0.02
MIN_FREQUENCY = 0.5
MAX_FREQUENCY = 8.0

def normalized_coordinates(coordinates, valid, *, dtype=torch.float32):
    if coordinates.ndim != 3 or coordinates.shape[-1] != 2:
        raise ValueError('coordinates must have shape [B,N,2]')
    if coordinates.dtype not in (torch.int32, torch.int64):
        raise ValueError('coordinates must be integer tensors')
    if valid.shape != coordinates.shape[:2] or valid.dtype != torch.bool:
        raise ValueError('valid must be a boolean [B,N] padding mask')
    if valid.device != coordinates.device:
        raise ValueError('coordinates and valid must be on the same device')
    if dtype not in (torch.float32, torch.float64):
        raise ValueError('coordinate calculations require float32 or float64')
    batch, length, _ = coordinates.shape
    if length == 0:
        return torch.zeros(batch, 0, 2, dtype=dtype, device=coordinates.device)
    positions = coordinates.masked_fill(~valid[..., None], 0).to(dtype)
    lower = positions.masked_fill(~valid[..., None], float('inf')).amin(dim=1)
    upper = positions.masked_fill(~valid[..., None], float('-inf')).amax(dim=1)
    present = valid.any(dim=1)
    lower = torch.where(present[:, None], lower, torch.zeros_like(lower))
    upper = torch.where(present[:, None], upper, torch.zeros_like(upper))
    span = upper - lower
    normalized = 2 * (positions - lower[:, None]) / span.clamp_min(1)[:, None] - 1
    normalized = torch.where((span > 0)[:, None], normalized, torch.zeros_like(normalized))
    return normalized.masked_fill(~valid[..., None], 0)

def fourier_geometry(coordinates, valid, channels, *, dtype=torch.float32):
    if isinstance(channels, bool) or not isinstance(channels, int) or channels < 4 or channels % 4:
        raise ValueError('channels must be a positive multiple of four')
    normalized = normalized_coordinates(coordinates, valid, dtype=dtype)
    frequencies = torch.logspace(math.log10(MIN_FREQUENCY), math.log10(MAX_FREQUENCY), channels // 4, dtype=dtype, device=coordinates.device)
    angles = math.pi * normalized[..., None, :] * frequencies[None, None, :, None]
    x, y = (angles[..., 0], angles[..., 1])
    features = torch.stack((x.sin(), x.cos(), y.sin(), y.cos()), dim=-1)
    return features.flatten(-2).masked_fill(~valid[..., None], 0)

def add_normalized_geometry(hidden, coordinates, valid, *, enabled=False, amplitude=DEFAULT_AMPLITUDE):
    if not isinstance(enabled, bool):
        raise ValueError('enabled must be boolean')
    if not enabled:
        return hidden
    if isinstance(amplitude, bool) or not isinstance(amplitude, (int, float)) or (not math.isfinite(amplitude)) or (amplitude < 0):
        raise ValueError('amplitude must be finite and nonnegative')
    if amplitude == 0:
        return hidden
    if hidden.ndim != 3 or not hidden.is_floating_point():
        raise ValueError('hidden must be a floating [B,N,C] tensor')
    if hidden.shape[:2] != coordinates.shape[:2] or hidden.device != coordinates.device:
        raise ValueError('hidden and coordinates must share batch, length and device')
    calculation_dtype = torch.float64 if hidden.dtype == torch.float64 else torch.float32
    features = fourier_geometry(coordinates, valid, hidden.shape[-1], dtype=calculation_dtype)
    return hidden + amplitude * features.to(hidden.dtype)
