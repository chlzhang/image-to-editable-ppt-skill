import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'skills/image-to-editable-ppt/cli/editppt/runtime'
sys.path.insert(0, str(RUNTIME))
import _page_artifacts as artifacts
import process_asset_sheet as processor


class AssetSheetContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='asset contract ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.page = self.root / 'page'
        self.page.mkdir()
        self.source = self.root / 'external source.png'
        image = Image.new('RGBA', (100, 100))
        ImageDraw.Draw(image).rectangle((25, 25, 74, 74), fill=(255, 0, 0, 255))
        image.save(self.source)
        result = self.cli('import', '--job-id', 'icons', '--source-image', self.source,
                          '--dest', 'assets/imported.png', '--backend', 'builtin-imagegen')
        self.assertEqual(0, result.returncode, result.stderr)

    def cli(self, operation, *args):
        return subprocess.run([sys.executable, str(RUNTIME / ('record_imagegen_result.py' if operation == 'import'
                              else 'process_asset_sheet.py')), str(self.page), *map(str, args)],
                              capture_output=True, text=True)

    def snapshot(self):
        return {p.relative_to(self.page): p.read_bytes() for p in self.page.rglob('*') if p.is_file()}

    def assert_rejected_unchanged(self, *args):
        before = self.snapshot()
        result = self.cli('process', '--job-id', 'icons', *args)
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertEqual(before, self.snapshot())

    def job(self):
        return json.loads((self.page / 'imagegen-jobs.json').read_text(encoding='utf-8'))['jobs'][0]

    def test_imported_output_is_default_source(self):
        result = self.cli('process', '--job-id', 'icons')
        self.assertEqual(0, result.returncode, result.stderr)
        job = self.job()
        self.assertEqual('processed', job['status'])
        self.assertTrue((self.page / job['alpha']).is_file())
        report = json.loads((self.page / job['split_manifest']).read_text(encoding='utf-8'))
        self.assertTrue(report['assets'])
        self.assertTrue(all(Path(asset['path']).is_file() for asset in report['assets']))

    def test_unknown_job_without_source_fails_without_creating_job(self):
        before = self.snapshot()
        result = self.cli('process', '--job-id', 'unknown')
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertEqual(before, self.snapshot())

    def test_missing_imported_input_does_not_advance_status(self):
        (self.page / 'assets/imported.png').unlink()
        self.assert_rejected_unchanged()

    def test_invalid_split_preserves_existing_alpha_and_job(self):
        Image.new('RGBA', (20, 20), (0, 0, 255, 150)).save(self.page / 'assets/icons.asset-sheet-alpha.png')
        self.assert_rejected_unchanged('--asset-sheet-source', 'assets/imported.png',
                                       '--asset-names', 'one,two', '--force-chroma')

    def test_invalid_report_directory_preserves_existing_outputs(self):
        Image.new('RGBA', (20, 20), (0, 0, 255, 150)).save(self.page / 'alpha.png')
        (self.page / 'report.json').mkdir()
        self.assert_rejected_unchanged('--alpha', 'alpha.png', '--split-manifest', 'report.json', '--force-chroma')

    def test_file_in_output_parent_preserves_existing_outputs(self):
        Image.new('RGBA', (20, 20), (0, 0, 255, 150)).save(self.page / 'alpha.png')
        (self.page / 'blocked').write_text('keep', encoding='utf-8')
        self.assert_rejected_unchanged('--alpha', 'alpha.png', '--assets-dir', 'blocked/assets', '--force-chroma')

    def test_zero_assets_is_not_success(self):
        self.assert_rejected_unchanged('--asset-sheet-source', 'assets/imported.png', '--split-min-area', '100000')

    def test_path_traversal_is_rejected_before_any_output(self):
        self.assert_rejected_unchanged('--asset-sheet-source', 'assets/imported.png', '--alpha', '../outside.png')
        self.assertFalse((self.root / 'outside.png').exists())

    def test_absolute_external_output_is_rejected(self):
        self.assert_rejected_unchanged('--asset-sheet-source', 'assets/imported.png', '--split-manifest', self.root / 'outside.json')
        self.assertFalse((self.root / 'outside.json').exists())

    def test_unsafe_asset_name_is_rejected(self):
        self.assert_rejected_unchanged('--asset-sheet-source', 'assets/imported.png', '--asset-names', '../../outside')
        self.assertFalse((self.root / 'outside.png').exists())

    def test_generated_asset_cannot_replace_imported_sheet(self):
        self.assert_rejected_unchanged('--asset-sheet-source', 'assets/imported.png', '--asset-names', 'imported')

    def test_existing_asset_symlink_cannot_redirect_write(self):
        outside = self.root / 'outside.png'
        Image.new('RGB', (8, 8), 'blue').save(outside)
        link = self.page / 'assets/asset_01.png'
        try:
            link.symlink_to(outside)
        except OSError as error:
            self.skipTest(f'file symlink unavailable: {error}')
        before = outside.read_bytes()
        self.assert_rejected_unchanged('--asset-sheet-source', 'assets/imported.png')
        self.assertEqual(before, outside.read_bytes())

    def test_directory_link_cannot_redirect_output(self):
        outside = self.root / 'outside'
        outside.mkdir()
        link = self.page / 'linked'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if sys.platform != 'win32':
                raise
            import _winapi
            try:
                _winapi.CreateJunction(str(outside), str(link))
            except OSError as error:
                self.skipTest(f'directory links unavailable: {error}')
        self.assert_rejected_unchanged('--asset-sheet-source', 'assets/imported.png', '--assets-dir', 'linked')
        self.assertEqual([], list(outside.iterdir()))

    def test_external_readonly_input_and_internal_absolute_outputs(self):
        before = self.source.read_bytes()
        result = self.cli('process', '--job-id', 'icons', '--asset-sheet-source', self.source,
                          '--alpha', self.page / 'alpha with spaces.png', '--assets-dir', self.page / 'split assets',
                          '--split-manifest', self.page / 'report.json')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(before, self.source.read_bytes())
        self.assertTrue((self.page / 'alpha with spaces.png').is_file())

    def test_skip_split_only_requires_alpha(self):
        result = self.cli('process', '--job-id', 'icons', '--skip-split', '--skip-chroma')
        self.assertEqual(0, result.returncode, result.stderr)
        job = self.job()
        self.assertEqual('processed', job['status'])
        self.assertTrue((self.page / job['alpha']).is_file())
        self.assertFalse((self.page / job['split_manifest']).exists())

    def test_skip_chroma_resplits_existing_alpha_from_opaque_import(self):
        with Image.open(self.source) as image:
            image.convert('RGB').save(self.source)
        result = self.cli('import', '--job-id', 'icons', '--source-image', self.source,
                          '--dest', 'assets/imported.png', '--backend', 'builtin-imagegen')
        self.assertEqual(0, result.returncode, result.stderr)
        result = self.cli('process', '--job-id', 'icons')
        self.assertEqual(0, result.returncode, result.stderr)
        alpha = self.page / self.job()['alpha']
        before = alpha.read_bytes()
        result = self.cli('process', '--job-id', 'icons', '--skip-chroma')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(before, alpha.read_bytes())

    def test_successful_exit_without_outputs_is_not_processed(self):
        before = self.snapshot()
        argv = ['process', str(self.page), '--job-id', 'icons', '--asset-sheet-source', 'assets/imported.png']
        with mock.patch.object(sys, 'argv', argv), mock.patch.object(artifacts, 'run'):
            with self.assertRaises((SystemExit, FileNotFoundError, ValueError)):
                processor.main()
        self.assertEqual(before, self.snapshot())


if __name__ == '__main__':
    unittest.main()
