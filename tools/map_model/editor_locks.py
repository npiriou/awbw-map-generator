from __future__ import annotations
from collections.abc import Mapping
import torch

class _ReadOnlyDict(dict):

    @staticmethod
    def _readonly(*args, **kwargs):
        raise TypeError('compiled sampling locks are read-only; compile a new snapshot after changes')
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _readonly

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

class SamplingLocks(_ReadOnlyDict):

    def __init__(self, locks):
        if not isinstance(locks, Mapping):
            raise TypeError('sampling locks must be a position-to-fields mapping')
        mapping, packed = ({}, [])
        for position, fields in locks.items():
            if type(position) is not int or position < 0:
                raise ValueError('sampling lock positions must be nonnegative integers')
            if not isinstance(fields, Mapping):
                raise TypeError('sampling lock fields must be a column-to-value mapping')
            values = {}
            for column, value in fields.items():
                if type(column) is not int or not 0 <= column < 5:
                    raise ValueError('sampling lock columns must be semantic field integers 0 through 4')
                if type(value) is not int:
                    raise ValueError('sampling lock values must be integer categories')
                values[column] = value
                packed.append((position, column, value))
            mapping[position] = _ReadOnlyDict(values)
        dict.__init__(self, mapping)
        self._cpu_packed = torch.tensor(packed, dtype=torch.long).reshape(-1, 3)
        self._max_position = max((position for position, _, _ in packed), default=-1)
        self._by_device = {torch.device('cpu'): self._cpu_packed}
        self._values_by_dtype = {}

    def packed(self, device):
        device = torch.device(device)
        if device.type == 'cuda' and device.index is None:
            device = torch.device('cuda', torch.cuda.current_device())
        value = self._by_device.get(device)
        if value is None:
            value = self._cpu_packed.to(device=device)
            self._by_device[device] = value
        return value

    def _tensor_values(self, device, dtype):
        packed = self.packed(device)
        if dtype == packed.dtype:
            return packed[:, 2]
        key = (packed.device, dtype)
        value = self._values_by_dtype.get(key)
        if value is None:
            value = packed[:, 2].to(dtype=dtype)
            self._values_by_dtype[key] = value
        return value

def make_sampling_locks(locks):
    if locks is None or isinstance(locks, SamplingLocks):
        return locks
    return SamplingLocks(locks)

def apply_sampling_locks(grid, locks):
    if not locks:
        return
    compiled = make_sampling_locks(locks)
    if grid.ndim != 3 or grid.shape[0] < 1 or grid.shape[-1] < 5:
        raise ValueError('sampling grid must have shape batch, cells, at least five fields')
    if compiled._max_position >= grid.shape[1]:
        raise IndexError('sampling lock position is outside the grid')
    packed = compiled.packed(grid.device)
    if packed.shape[0]:
        grid[0, packed[:, 0], packed[:, 1]] = compiled._tensor_values(grid.device, grid.dtype)

def editor_model_mask(model, masked, locks):
    if not locks or not getattr(model, 'supports_field_masks', False):
        return masked
    compiled = make_sampling_locks(locks)
    if masked.dtype != torch.bool or masked.ndim not in (2, 3) or masked.shape[0] < 1:
        raise ValueError('editor mask must be a boolean cell mask or five-field mask')
    if masked.ndim == 3 and masked.shape[-1] != 5:
        raise ValueError('editor field mask must contain five semantic fields')
    if compiled._max_position >= masked.shape[1]:
        raise IndexError('sampling lock position is outside the mask')
    field_mask = masked.unsqueeze(-1).expand(-1, -1, 5).clone() if masked.ndim == 2 else masked.clone()
    packed = compiled.packed(masked.device)
    if packed.shape[0]:
        field_mask[0, packed[:, 0], packed[:, 1]] = False
    return field_mask
