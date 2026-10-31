import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'skills/image-to-editable-ppt/cli/editppt/runtime'
sys.path.insert(0, str(RUNTIME))
from build_pptx_from_manifest import write_pptx
from deck_run_state import sha256_file


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding='utf-8')


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


class FinalizeRecordedInputsTest(unittest.TestCase):
    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'editppt.cli', *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True)

    def ok(self, result):
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='recorded inputs ')
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        pages = [{'page_id': f'page_{i:03d}', 'page_index': i, 'page_dir': f'pages/page_{i:03d}',
                  'manifest': f'pages/page_{i:03d}/manifest.json',
                  'validation': f'pages/page_{i:03d}/validation.json',
                  'page_request': f'pages/page_{i:03d}/page_request.json'} for i in (1, 2)]
        self.deck = {'schema_version': 1, 'page_count': 2, 'pages': pages,
                     'notes_manifest': 'notes_manifest.json', 'output': 'final/custom.pptx',
                     'slide': {'width': 13.333, 'height': 7.5}}
        write_json(self.run / 'deck_manifest.json', self.deck)
        write_json(self.run / 'notes_manifest.json', {'notes': [{'page_index': 2, 'text': 'Keep notes'}]})
        write_json(self.run / 'page_jobs.json', {'pages': [dict(p, status='pending') for p in pages]})
        for i, page in enumerate(pages, 1):
            directory = self.run / page['page_dir']
            write_json(directory / 'page_request.json', {'page_id': page['page_id']})
            prompt = directory / 'worker-prompt.md'
            prompt.write_text('Synthetic worker prompt', encoding='utf-8')
            self.ok(self.cli('run', 'dispatch', self.run, '--page', i, '--agent-id', f'worker-{i}', '--prompt-file', prompt))
            model = {
                'source': {'width_px': 320, 'height_px': 180}, 'slide': self.deck['slide'],
                'text_inventory': [f'APPROVED {i}00'],
                'text_boxes': [{'text': f'APPROVED {i}00', 'box_px': [10, 20, 250, 40]}],
                'shapes': [], 'visual_inventory': [],
                'background_strategy': {'mode': 'native-or-script'},
                'quality_checks': dict.fromkeys(['font_size_calibrated', 'visual_inventory_matched',
                    'background_strategy_checked', 'shape_corner_geometry_checked'], True),
                'images': [{'path': 'asset.png', 'box_px': [10, 100, 20, 20]}],
                'asset_provenance': [{'path': 'asset.png', 'source': 'asset.png',
                    'source_type': 'user-provided', 'provenance_note': 'Synthetic fixture'}],
            }
            selected = directory / 'approved/approved.json'
            write_json(selected, model)
            Image.new('RGB', (8, 8), ('red', 'blue')[i - 1]).save(selected.parent / 'asset.png')
            write_pptx(model, directory / 'page.pptx', selected)
            default = copy.deepcopy(model)
            default['text_inventory'] = ['UNRECORDED 999']
            default['text_boxes'][0]['text'] = 'UNRECORDED 999'
            default['images'] = []
            write_json(directory / 'manifest.json', default)
            write_json(directory / 'imagegen-jobs.json', {'jobs': []})
            for name in ('preview.png', 'split_assets_contact.png'):
                Image.new('RGB', (8, 8)).save(directory / name)
            for name in ('validation.json', 'qa.json'):
                write_json(directory / name, {'passed': True})
            write_json(directory / 'page_result.json', {'page_manifest': 'approved/approved.json', 'validation': 'qa.json'})
            self.ok(self.cli('run', 'record', self.run, '--page', i, '--agent-id', f'worker-{i}'))

    def finalize(self):
        return self.cli('run', 'finalize', self.run)

    def assert_rejected(self):
        result = self.finalize()
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertFalse((self.run / self.deck['output']).exists())
        self.assertTrue(all(p['status'] == 'recorded' for p in read_json(self.run / 'page_jobs.json')['pages']))

    def assert_selected_deck(self):
        self.ok(self.finalize())
        with zipfile.ZipFile(self.run / self.deck['output']) as z:
            for i in (1, 2):
                text = z.read(f'ppt/slides/slide{i}.xml').decode()
                self.assertIn(f'APPROVED {i}00', text)
                self.assertNotIn('UNRECORDED', text)
            self.assertIn(b'Keep notes', z.read('ppt/notesSlides/notesSlide2.xml'))
        self.assertTrue(read_json(self.run / 'final/validation.json')['passed'])
        report = read_json(self.run / 'final/validation.json')
        assembly_path = Path(report['deck_manifest'])
        self.assertTrue(assembly_path.is_file())
        assembly = read_json(assembly_path)
        for page in assembly['pages']:
            self.assertTrue(page['manifest'].endswith('approved.json'))
            self.assertTrue(page['validation'].endswith('qa.json'))

    def test_custom_recorded_manifest_is_final_source(self):
        self.assert_selected_deck()

    def test_default_manifest_and_validation_are_not_assembly_inputs(self):
        for page in self.deck['pages']:
            (self.run / page['manifest']).unlink()
            (self.run / page['validation']).unlink()
        self.assert_selected_deck()

    def test_absolute_and_normalized_page_local_paths(self):
        for i, page in enumerate(self.deck['pages'], 1):
            directory = self.run / page['page_dir']
            write_json(directory / 'page_result.json', {'page_manifest': str(directory / 'approved/../approved/approved.json'),
                                                       'validation': str(directory / 'qa.json')})
            self.ok(self.cli('run', 'record', self.run, '--page', i, '--agent-id', f'worker-{i}'))
        self.assert_selected_deck()

    def test_changed_recorded_manifest_is_rejected(self):
        path = self.run / 'pages/page_001/approved/approved.json'
        path.write_text(path.read_text(encoding='utf-8') + ' ', encoding='utf-8')
        self.assert_rejected()

    def test_missing_hashes_are_rejected(self):
        jobs = read_json(self.run / 'page_jobs.json')
        jobs['pages'][0]['result'].pop('hashes')
        write_json(self.run / 'page_jobs.json', jobs)
        self.assert_rejected()

    def test_missing_recorded_manifest_key_is_rejected(self):
        jobs = read_json(self.run / 'page_jobs.json')
        for key in ('outputs', 'hashes'):
            jobs['pages'][0]['result'][key].pop('page_manifest')
        write_json(self.run / 'page_jobs.json', jobs)
        self.assert_rejected()

    def test_page_count_mismatch_is_rejected_before_build(self):
        self.deck['page_count'] = 1
        write_json(self.run / 'deck_manifest.json', self.deck)
        self.assert_rejected()

    def test_page_order_mismatch_is_rejected(self):
        self.deck['pages'].reverse()
        write_json(self.run / 'deck_manifest.json', self.deck)
        self.assert_rejected()

    def test_duplicate_page_id_is_rejected(self):
        jobs = read_json(self.run / 'page_jobs.json')
        jobs['pages'][1]['page_id'] = jobs['pages'][0]['page_id']
        write_json(self.run / 'page_jobs.json', jobs)
        self.assert_rejected()

    def test_recorded_manifest_outside_page_is_rejected(self):
        jobs = read_json(self.run / 'page_jobs.json')
        result = jobs['pages'][0]['result']
        outside = self.run / 'outside.json'
        outside.write_bytes((self.run / result['outputs']['page_manifest']).read_bytes())
        result['outputs']['page_manifest'] = 'outside.json'
        result['hashes']['page_manifest'] = sha256_file(outside)
        write_json(self.run / 'page_jobs.json', jobs)
        self.assert_rejected()

    def test_standalone_deck_build_keeps_explicit_manifest_semantics(self):
        out = self.run / 'standalone.pptx'
        result = subprocess.run([sys.executable, str(RUNTIME / 'build_pptx_from_manifest.py'),
            '--deck-manifest', str(self.run / 'deck_manifest.json'), '--out', str(out)],
            capture_output=True, text=True)
        self.ok(result)
        with zipfile.ZipFile(out) as z:
            self.assertIn(b'UNRECORDED 999', z.read('ppt/slides/slide1.xml'))


if __name__ == '__main__':
    unittest.main()
