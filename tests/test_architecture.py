import ast
import unittest
from pathlib import Path

from homelab_backup import config, restore


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / 'homelab_backup'
RESTORE_LAYERS = {
    'restore', 'restore_apply', 'restore_plan', 'restore_inventory',
}


def top_level_dependencies():
    modules = {path.stem for path in PACKAGE_ROOT.glob('*.py')}
    dependencies = {}
    for path in PACKAGE_ROOT.glob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        dependencies[path.stem] = set()
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom) or node.level != 1:
                continue
            if node.module:
                dependency = node.module.split('.')[0]
                if dependency in modules:
                    dependencies[path.stem].add(dependency)
            else:
                dependencies[path.stem].update(
                    alias.name for alias in node.names if alias.name in modules
                )
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

    def test_owned_symbols_are_not_reexported_by_compatibility_modules(self):
        for name in (
                'DOCKER_VOLUME_RE', 'RETENTION_FLAGS', 'SERVICE_RE',
                'actual_volume_name', 'compose_cmd', 'compose_model', 'manifest',
                'manifests', 'source_path', 'valid_service_name',
                'validate_docker_volume_name', 'validate_manifest',
                'validate_retention',
        ):
            self.assertFalse(hasattr(config, name), name)
        for name in (
                'RestorePlan', 'apply_one', 'compose_authorization_projection',
                'compose_files_exist', 'compose_targets',
                'deferred_compose_sources', 'inventory_volumes',
                'load_restore_inventory', 'normalize_restore_target',
                'prepare_restore_plan', 'restore_authorization_projection',
                'restore_path_source', 'restored_path_details',
                'validate_restore_inventory', 'validate_restore_path_separation',
                'validate_restore_sources',
        ):
            self.assertFalse(hasattr(restore, name), name)


if __name__ == '__main__':
    unittest.main()
