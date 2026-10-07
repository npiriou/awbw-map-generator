from copy import deepcopy
import unittest

from tools.map_features.symmetry_team import _MATRICES, _ORIENTED, _symmetry_key
from tools.map_model.orientation import check_visual_symmetry, orient_terrain
from tools.map_model.sample import _compose, _orbits


def group_for(*generators):
    group = {(1, 0, 0, 1)}
    pending = list(group)
    while pending:
        current = pending.pop()
        for generator in generators:
            value = _compose(current, generator)
            if value not in group:
                group.add(value)
                pending.append(value)
    return tuple(sorted(group))


class VisualSymmetryTests(unittest.TestCase):
    vocabulary = {'gameplay_type_only': True, 'tile_tokens': ['tile:1'],
                  'rendering_tile_tokens': ['tile:1', 'tile:28', 'property:city'] +
                                           [f'tile:{tile}' for tile in _ORIENTED]}

    def report(self, tiles, width, height, group):
        payload = {'Size X': width, 'Size Y': height,
                   'Terrain Map': [[int(tiles[y * width + x].split(':')[1])
                                    if tiles[y * width + x].startswith('tile:') else 34
                                    for y in range(height)] for x in range(width)],
                   'Predeployed Units': [{'Unit ID': 1, 'Unit X': 0, 'Unit Y': 0, 'Country Code': 'os'}]}
        original = deepcopy(payload)
        result = check_visual_symmetry({'status': 'matched', 'violations': [], 'observed': {}}, payload, group)
        self.assertEqual(payload, original)
        return result

    def test_all_directional_families_and_transforms(self):
        families = [4, 15, 26, 29, 101, 113, 115]
        for name, matrix in _MATRICES.items():
            group = group_for(matrix)
            for tile in families:
                with self.subTest(transform=name, tile=tile):
                    before = [f'tile:{tile}'] * 16
                    for orbit in _orbits(4, 4, group):
                        if len(orbit) < len(group):
                            for index in orbit:
                                before[index] = 'tile:1'
                    original = list(before)
                    after = orient_terrain(before, 4, 4, self.vocabulary, group=group)
                    self.assertEqual(self.report(after, 4, 4, group)['status'], 'matched')
                    self.assertEqual([_symmetry_key(int(t.split(':')[1])) for t in before],
                                     [_symmetry_key(int(t.split(':')[1])) for t in after])
                    self.assertEqual(before, original)
                    self.assertEqual(after, orient_terrain(before, 4, 4, self.vocabulary, group=group))

    def test_dihedral_group_mixed_layout_and_property_connections(self):
        group = group_for(_MATRICES['rotation_90'], _MATRICES['vertical'])
        before = ['tile:1'] * 64
        families = ['tile:15', 'tile:4', 'tile:101', 'tile:113', 'tile:29', 'property:city', 'tile:26']
        for position, orbit in enumerate(_orbits(8, 8, group)):
            for index in orbit:
                before[index] = families[position % len(families)] if len(orbit) == len(group) else 'tile:15'
        after = orient_terrain(before, 8, 8, self.vocabulary, group=group)
        self.assertEqual(self.report(after, 8, 8, group)['status'], 'matched')
        for old, new in zip(before, after):
            if old.startswith('property:'):
                self.assertEqual(old, new)

    def test_odd_road_center_is_invariant_under_quarter_rotations(self):
        group = group_for(_MATRICES['rotation_90'])
        before = ['tile:1'] * 9
        before[4] = 'tile:15'
        after = orient_terrain(before, 3, 3, self.vocabulary, group=group)
        self.assertEqual(after[4], 'tile:17')
        self.assertEqual(self.report(after, 3, 3, group)['status'], 'matched')

    def test_locked_variant_controls_its_unlocked_counterpart(self):
        group = group_for(_MATRICES['rotation_180'])
        before = ['tile:1'] * 16
        before[0] = before[15] = 'tile:29'
        after = orient_terrain(before, 4, 4, self.vocabulary, group=group, locked_tiles={0: 'tile:32'})
        self.assertEqual(after[0], 'tile:32')
        self.assertEqual(after[15], 'tile:31')
        self.assertEqual(self.report(after, 4, 4, group)['status'], 'matched')

    def test_conflicting_locks_preserved_and_reported(self):
        group = group_for(_MATRICES['rotation_180'])
        before = ['tile:1'] * 16
        before[0] = before[15] = 'tile:29'
        after = orient_terrain(before, 4, 4, self.vocabulary, group=group,
                               locked_tiles={0: 'tile:29', 15: 'tile:29'})
        self.assertEqual(after[0], 'tile:29')
        self.assertEqual(after[15], 'tile:29')
        report = self.report(after, 4, 4, group)
        self.assertEqual(report['status'], 'not_matched')
        self.assertEqual(report['observed']['visual_symmetry']['mismatched_tiles'], 2)

    def test_locked_rubble_rotates_plain_gameplay_counterpart(self):
        group = group_for(_MATRICES['rotation_90'])
        before = ['tile:1'] * 16
        after = orient_terrain(before, 4, 4, self.vocabulary, group=group, locked_tiles={0: 'tile:115'})
        self.assertEqual(after[0], 'tile:115')
        self.assertEqual(after[3], 'tile:116')
        self.assertEqual(after[12], 'tile:116')
        self.assertEqual(after[15], 'tile:115')
        self.assertEqual(self.report(after, 4, 4, group)['status'], 'matched')
        self.assertTrue(all(_symmetry_key(int(token.split(':')[1])) == ('plain', None) for token in after))

    def test_unavailable_center_orientation_reported_without_changing_terrain(self):
        group = group_for(_MATRICES['rotation_180'])
        before = ['tile:1'] * 9
        before[4] = 'tile:29'
        after = orient_terrain(before, 3, 3, self.vocabulary, group=group)
        self.assertIn(after[4], {'tile:29', 'tile:30', 'tile:31', 'tile:32'})
        self.assertEqual(self.report(after, 3, 3, group)['status'], 'not_matched')

    def test_rectangular_rotation_and_reflections(self):
        for name in ('rotation_180', 'vertical', 'horizontal'):
            group = group_for(_MATRICES[name])
            after = orient_terrain(['tile:29'] * 24, 6, 4, self.vocabulary, group=group)
            self.assertEqual(self.report(after, 6, 4, group)['status'], 'matched')

    def test_single_transform_retains_independent_autotiling(self):
        before = ['tile:29'] * 4
        self.assertEqual(orient_terrain(before, 2, 2, self.vocabulary),
                         orient_terrain(before, 2, 2, self.vocabulary, group=group_for()))


if __name__ == '__main__':
    unittest.main()
