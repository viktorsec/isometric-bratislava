"""The server's stdlib worker detects offline changes and folder edits."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import serve


class WorkerTests(unittest.TestCase):
    def wait_manifest(self, path, predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                manifest = json.loads(path.read_text())
                if predicate(manifest):
                    return manifest
            except (OSError, ValueError):
                pass
            time.sleep(.1)
        self.fail('worker did not publish expected snapshot')

    def test_startup_add_replace_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            web = root / 'web'
            (web / 'tiles').mkdir(parents=True)
            (web / 'tiles/info.js').write_text('window.MOSAIC_INFO = ' + json.dumps(dict(width=2051, height=1155, tileSize=512, maxLevel=3)) + ';')
            inbox = root / 'redrawn-cells'
            inbox.mkdir()
            (root / 'scripts').mkdir()
            shutil.copy(Path(serve.__file__).with_name('redrawn_pyramid.py'), root / 'scripts/redrawn_pyramid.py')
            (root / '.venv').symlink_to(Path(sys.prefix), target_is_directory=True)
            manifest_path = web / 'redrawn-pyramid/manifest.json'
            cell = inbox / 'raw_c000_r000_x0_y0.png'
            Image.new('RGB', (1024, 1024), 'red').save(cell)
            with patch.multiple(serve, ROOT=root, WEB=web, INBOX=inbox, PYRAMID=manifest_path):
                worker = serve.PyramidWorker()
                worker.start()
                try:
                    first = self.wait_manifest(manifest_path, lambda m: '0,0' in m['sources'])
                    other = inbox / 'raw_c001_r000_x0_y0.png'
                    Image.new('RGB', (1024, 1024), 'blue').save(other)
                    second = self.wait_manifest(manifest_path, lambda m: '1,0' in m['sources'])
                    self.assertNotEqual(first['version'], second['version'])
                    Image.new('RGB', (1024, 1024), 'green').save(cell)
                    third = self.wait_manifest(manifest_path, lambda m: m['sources']['0,0']['v'] != second['sources']['0,0']['v'])
                    cell.unlink()
                    self.wait_manifest(manifest_path, lambda m: '0,0' not in m['sources'])
                    # A stable scan does not reconstruct tiles again.
                    self.assertNotEqual(second['version'], third['version'])
                finally:
                    worker.stop.set()
                    worker.join(timeout=15)
                    self.assertFalse(worker.is_alive())


if __name__ == '__main__':
    unittest.main()
