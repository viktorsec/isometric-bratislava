"""Regression coverage for sparse composition and incremental reconstruction."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from redrawn_pyramid import Builder, half_rgba


class PyramidTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.inbox = self.root / 'cells'
        self.inbox.mkdir()
        self.out = self.root / 'pyramid'
        self.geometry = self.root / 'info.js'
        self.geometry.write_text('window.MOSAIC_INFO = ' + json.dumps(dict(width=2051, height=1155, tileSize=512, maxLevel=3)) + ';')

    def tearDown(self):
        self.tmp.cleanup()

    def cell(self, c, r, color, size=1024):
        path = self.inbox / f'raw_c{c:03}_r{r:03}_x0_y0.png'
        Image.new('RGBA', (size, size), color).save(path)
        return path

    def build(self, out=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return Builder(self.inbox, out or self.out, self.geometry).build()

    def image(self, manifest, mode, z, x, y, out=None):
        path = manifest['variants'][mode][z].get(f'{x},{y}')
        if not path:
            return None
        with Image.open((out or self.out) / path) as im:
            return np.asarray(im.convert('RGBA'))

    def assert_matches_clean(self, current):
        clean_out = self.root / 'clean'
        clean = self.build(clean_out)
        for mode in current['variants']:
            for z, level in enumerate(current['variants'][mode]):
                self.assertEqual(set(level), set(clean['variants'][mode][z]))
                for key in level:
                    x, y = map(int, key.split(','))
                    np.testing.assert_array_equal(self.image(current, mode, z, x, y), self.image(clean, mode, z, x, y, clean_out))

    def test_sparse_lossless_and_noop(self):
        self.cell(0, 0, (29, 47, 83, 255))
        m = self.build()
        self.assertEqual(len(m['variants']['plain'][3]), 4)
        self.assertIsNone(self.image(m, 'plain', 3, 4, 2))
        np.testing.assert_array_equal(self.image(m, 'plain', 3, 0, 0)[10, 10], [29, 47, 83, 255])
        self.assertEqual(self.build()['version'], m['version'])
        self.assertEqual(m['variants']['plain'][0].keys(), {'0,0'})

    def test_add_replace_delete_matches_clean_and_reuses_unaffected(self):
        left = self.cell(0, 0, 'red')
        self.cell(2, 1, 'blue')
        old = self.build()
        self.cell(1, 0, 'green')
        added = self.build()
        self.assertEqual(old['variants']['plain'][3]['0,0'], added['variants']['plain'][3]['0,0'])
        self.assert_matches_clean(added)
        self.cell(0, 0, 'yellow')
        replaced = self.build()
        self.assert_matches_clean(replaced)
        left.unlink()
        deleted = self.build()
        self.assertNotIn('0,0', deleted['variants']['plain'][3])
        self.assert_matches_clean(deleted)
        for p in self.inbox.iterdir():
            p.unlink()
        empty = self.build()
        self.assertTrue(all(not lv for levels in empty['variants'].values() for lv in levels))

    def test_neighbour_arrival_changes_dither_mask(self):
        self.cell(1, 0, 'blue')
        self.build()
        self.cell(0, 0, 'red')
        m = self.build()
        plain = self.image(m, 'plain', 3, 1, 0)
        faded = self.image(m, 'crossfade', 3, 1, 0)
        # At x=896 (start of cell 1), Bayer threshold is above t.
        np.testing.assert_array_equal(plain[0, 384], [0, 0, 255, 255])
        np.testing.assert_array_equal(faded[0, 384], [255, 0, 0, 255])
        # Past the 96px fade the later cell is fully opaque.
        np.testing.assert_array_equal(faded[0, 480], [0, 0, 255, 255])

    def test_premultiplied_reduction_and_odd_edges(self):
        a = np.zeros((3, 3, 4), dtype=np.uint8)
        a[0, 0] = [200, 40, 20, 255]
        a[2, 2] = [10, 20, 30, 255]
        small = np.asarray(half_rgba(Image.fromarray(a)))
        np.testing.assert_array_equal(small[0, 0], [200, 40, 20, 64])
        np.testing.assert_array_equal(small[1, 1], [10, 20, 30, 255])

    def test_high_resolution_adds_level_and_preserves_pixels(self):
        path = self.cell(0, 0, 'blue', 2048)
        with Image.open(path) as im:
            im.putpixel((1, 1), (255, 0, 0, 255))
            im.save(path)
        m = self.build()
        self.assertEqual(m['maxLevel'], 4)
        self.assertEqual(m['density'], 2)
        np.testing.assert_array_equal(self.image(m, 'crossfade', 4, 0, 0)[1, 1], [255, 0, 0, 255])


if __name__ == '__main__':
    unittest.main()
