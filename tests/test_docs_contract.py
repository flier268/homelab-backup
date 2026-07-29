import json
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

import yaml

from homelab_backup import config, manifest


ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = ROOT / 'docs'
FIXTURES = DOCS_ROOT / 'tests' / 'fixtures'


class DocsContractTests(unittest.TestCase):
    def test_docs_app_uses_pnpm_exclusively(self):
        docs_root = DOCS_ROOT
        package = json.loads(
            (docs_root / 'package.json').read_text(encoding='utf-8')
        )
        workflow = (
            ROOT / '.github' / 'workflows' / 'ci-release.yml'
        ).read_text(encoding='utf-8')

        self.assertEqual(package['packageManager'], 'pnpm@11.9.0')
        self.assertTrue((docs_root / 'pnpm-lock.yaml').is_file())
        self.assertFalse((docs_root / 'package-lock.json').exists())
        self.assertIn('cache: pnpm', workflow)
        self.assertIn('pnpm install --frozen-lockfile', workflow)
        self.assertNotIn('npm ci', workflow)

    def test_service_fixture_is_accepted_by_runtime_validator(self):
        value = yaml.safe_load((FIXTURES / 'backup.yaml').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as tmp:
            value['_path'] = str(Path(tmp) / 'backup.yaml')
            value['_dir'] = tmp
            self.assertTrue(manifest.validate_manifest(value))

    def test_all_documented_service_examples_are_runtime_valid(self):
        for source in sorted((ROOT / 'examples').glob('*.backup.yaml')):
            value = yaml.safe_load(source.read_text(encoding='utf-8'))
            with self.subTest(source=source.name), tempfile.TemporaryDirectory() as tmp:
                value['_path'] = str(Path(tmp) / 'backup.yaml')
                value['_dir'] = tmp
                self.assertTrue(manifest.validate_manifest(value))

    def test_global_fixture_is_accepted_by_runtime_validator(self):
        value = yaml.safe_load((FIXTURES / 'config.yaml').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / 'config.yaml'
            config_path.touch()
            with mock.patch.object(config, 'CFG', config_path), \
                    mock.patch.object(config, 'load_yaml', return_value=value), \
                    redirect_stderr(StringIO()):
                self.assertEqual(config.cfg()['host_id'], 'editor-test')

    def test_editor_components_stay_below_monolith_threshold(self):
        component_root = DOCS_ROOT / 'src' / 'components'
        for path in component_root.rglob('*.vue'):
            lines = path.read_text(encoding='utf-8').count('\n') + 1
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertLessEqual(lines, 360)

    def test_app_shell_does_not_own_domain_logic(self):
        app = (DOCS_ROOT / 'src' / 'App.vue').read_text(encoding='utf-8')
        for forbidden in ('parseInventory', 'stringify(', 'validateDocument',
                          'INVENTORY_COMMAND'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, app)

    def test_guide_is_composed_from_vue_components(self):
        guide_root = DOCS_ROOT / 'src' / 'components' / 'guide'
        guide_view = (guide_root / 'GuideView.vue').read_text(encoding='utf-8')
        for forbidden in ('v-html', '?raw', 'GUIDE.html'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, guide_view)

        component_names = {
            'GuideSetupSections.vue',
            'GuideConfigSections.vue',
            'GuideBackupSections.vue',
            'GuideOperationsSections.vue',
        }
        self.assertTrue(component_names.issubset(
            {path.name for path in guide_root.glob('*.vue')}
        ))

    def test_guide_is_a_generated_untracked_artifact(self):
        ignored = (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()
        workflow = (
            ROOT / '.github' / 'workflows' / 'ci-release.yml'
        ).read_text(encoding='utf-8')
        verifier = (
            DOCS_ROOT / 'scripts' / 'verify-single-file.mjs'
        ).read_text(encoding='utf-8')

        self.assertIn('GUIDE.html', ignored)
        self.assertIn('docs/dist/GUIDE.html', workflow)
        self.assertIn("'../../GUIDE.html'", verifier)


if __name__ == '__main__':
    unittest.main()
