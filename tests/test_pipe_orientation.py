from collections import Counter
import unittest

from tools.map_features.symmetry_team import _ORIENTED, _symmetry_key
from tools.map_model.orientation import check_visual_symmetry, orient_terrain


class PipeOrientationTests(unittest.TestCase):
    vocabulary = {'gameplay_type_only': True, 'tile_tokens': ['tile:1', 'tile:101', 'tile:113'],
                  'rendering_tile_tokens': ['tile:1'] + [f'tile:{tile}' for tile in _ORIENTED]}

    def check(self, before, width, height, expected, **options):
        after = orient_terrain(before, width, height, self.vocabulary, **options)
        self.assertEqual(after, expected)
        self.assertEqual(Counter(_symmetry_key(int(token.split(':')[1])) for token in before),
                         Counter(_symmetry_key(int(token.split(':')[1])) for token in after))
        self.assertEqual(after, orient_terrain(after, width, height, self.vocabulary, **options))

    def test_horizontal_pair_opens_toward_each_other(self):
        self.check(['tile:108', 'tile:110'], 2, 1, ['tile:110', 'tile:108'])

    def test_vertical_pair_opens_toward_each_other(self):
        self.check(['tile:109', 'tile:107'], 1, 2, ['tile:107', 'tile:109'])

    def test_pipe_and_seam_connect_in_all_four_directions(self):
        cases = [(['tile:101', 'tile:114'], 2, 1, ['tile:110', 'tile:113']),
                 (['tile:114', 'tile:101'], 2, 1, ['tile:113', 'tile:108']),
                 (['tile:101', 'tile:113'], 1, 2, ['tile:107', 'tile:114']),
                 (['tile:113', 'tile:101'], 1, 2, ['tile:114', 'tile:109'])]
        for before, width, height, expected in cases:
            with self.subTest(width=width, before=before):
                self.check(before, width, height, expected)

    def test_straight_chains_have_inward_endpoints(self):
        self.check(['tile:101'] * 3, 3, 1, ['tile:110', 'tile:102', 'tile:108'])
        self.check(['tile:101'] * 3, 1, 3, ['tile:107', 'tile:101', 'tile:109'])

    def test_seam_between_pipes_extends_the_line(self):
        self.check(['tile:101', 'tile:114', 'tile:101'], 3, 1, ['tile:110', 'tile:113', 'tile:108'])
        self.check(['tile:101', 'tile:113', 'tile:101'], 1, 3, ['tile:107', 'tile:114', 'tile:109'])

    def test_corner_keeps_connectors_and_rotates_endpoints(self):
        self.check(['tile:101', 'tile:101', 'tile:101', 'tile:1'], 2, 2,
                   ['tile:104', 'tile:108', 'tile:109', 'tile:1'])

    def test_symmetry_correction_keeps_pairs_connected(self):
        group = ((-1, 0, 0, -1), (1, 0, 0, 1))
        before = ['tile:1'] * 16
        for index in (0, 1, 14, 15):
            before[index] = 'tile:101'
        expected = list(before)
        expected[0], expected[1], expected[14], expected[15] = 'tile:110', 'tile:108', 'tile:110', 'tile:108'
        self.check(before, 4, 4, expected, group=group)
        payload = {'Size X': 4, 'Size Y': 4,
                   'Terrain Map': [[int(expected[y * 4 + x].split(':')[1]) for y in range(4)] for x in range(4)]}
        report = check_visual_symmetry({'status': 'matched', 'violations': [], 'observed': {}}, payload, group)
        self.assertEqual(report['status'], 'matched')

    def test_locked_endpoint_is_preserved(self):
        self.check(['tile:101', 'tile:114'], 2, 1, ['tile:108', 'tile:113'], locked_tiles={0: 'tile:108'})


if __name__ == '__main__':
    unittest.main()
