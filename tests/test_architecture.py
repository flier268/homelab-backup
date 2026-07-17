import ast
import unittest
from pathlib import Path

from homelab_backup import (
    config, manifest, restore, restore_apply, restore_inventory, restore_plan,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / 'homelab_backup'
RESTORE_LAYERS = {
    'restore', 'restore_apply', 'restore_plan', 'restore_inventory',
}


def top_level_dependencies():
    modules = {path.stem for path in PACKAGE_ROOT.glob('*.py')}
    dependencies = {}
    for path in PACKAGE_ROOT.glob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        dependencies[path.stem] = {
            node.module.split('.')[0]
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
            and node.level == 1
            and node.module
            and node.module.split('.')[0] in modules
        }
    return dependencies


class ArchitectureTests(unittest.TestCase):
    def test_top_level_module_graph_has_no_cycles(self):
        dependencies = top_level_dependencies()
        visiting = set()
        visited = set()

        def visit(module, path):
            if module in visiting:
                self.fail('cyclic import: ' + ' -> '.join((*path, module)))
            if module in visited:
                return
            visiting.add(module)
            for dependency in dependencies[module]:
                visit(dependency, (*path, module))
            visiting.remove(module)
            visited.add(module)

        for module in dependencies:
            visit(module, ())

    def test_restore_layers_follow_one_way_dependency_order(self):
        dependencies = top_level_dependencies()
        self.assertEqual(
            dependencies['restore'] & RESTORE_LAYERS,
            {'restore_apply'},
        )
        self.assertEqual(
            dependencies['restore_apply'] & RESTORE_LAYERS,
            {'restore_plan'},
        )
        self.assertEqual(
            dependencies['restore_plan'] & RESTORE_LAYERS,
            {'restore_inventory'},
        )
        self.assertFalse(dependencies['restore_inventory'] & RESTORE_LAYERS)

    def test_compatibility_modules_reexport_owned_symbols(self):
        self.assertIs(config.validate_manifest, manifest.validate_manifest)
        self.assertIs(config.compose_model, manifest.compose_model)
        self.assertIs(restore.apply_one, restore_apply.apply_one)
        self.assertIs(restore.prepare_restore_plan, restore_plan.prepare_restore_plan)
        self.assertIs(
            restore.validate_restore_inventory,
            restore_inventory.validate_restore_inventory,
        )


if __name__ == '__main__':
    unittest.main()
