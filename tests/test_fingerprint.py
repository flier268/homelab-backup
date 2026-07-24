import json
import os
import tempfile
import unittest
from pathlib import Path

from homelab_backup.fingerprint import (
    snapshot_context_fingerprint, stage_fingerprint,
)


class StageFingerprintTests(unittest.TestCase):
    def _tree(self, root, content=b'payload'):
        (root / 'paths' / 'data').mkdir(parents=True)
        payload = root / 'paths' / 'data' / 'payload.bin'
        payload.write_bytes(content)
        os.utime(payload, ns=(1_700_000_000_000_000_000,) * 2)
        os.utime(
            root / 'paths' / 'data',
            ns=(1_700_000_000_000_000_000,) * 2,
        )
        (root / '_meta').mkdir()
        (root / '_meta' / 'inventory.json').write_text(
            '{"version": 1}\n', encoding='utf-8',
        )

    def test_recreated_staging_tree_has_stable_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / 'first'
            second = Path(tmp) / 'second'
            self._tree(first)
            self._tree(second)

            self.assertEqual(
                stage_fingerprint(first),
                stage_fingerprint(second),
            )

    def test_synthetic_wrapper_directory_mtimes_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / 'first'
            second = Path(tmp) / 'second'
            self._tree(first)
            self._tree(second)
            for relative in ('.', 'paths', '_meta'):
                os.utime(first / relative, ns=(1_600_000_000_000_000_000,) * 2)
                os.utime(second / relative, ns=(1_800_000_000_000_000_000,) * 2)

            self.assertEqual(
                stage_fingerprint(first),
                stage_fingerprint(second),
            )

    def test_payload_directory_mtime_change_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / 'first'
            second = Path(tmp) / 'second'
            self._tree(first)
            self._tree(second)
            os.utime(
                first / 'paths' / 'data',
                ns=(1_600_000_000_000_000_000,) * 2,
            )
            os.utime(
                second / 'paths' / 'data',
                ns=(1_800_000_000_000_000_000,) * 2,
            )

            self.assertNotEqual(
                stage_fingerprint(first),
                stage_fingerprint(second),
            )

    def test_recreated_file_source_wrapper_has_stable_fingerprint(self):
        self._assert_recreated_non_directory_wrapper_is_stable('file')

    def test_recreated_symlink_source_wrapper_has_stable_fingerprint(self):
        self._assert_recreated_non_directory_wrapper_is_stable('symlink')

    def _assert_recreated_non_directory_wrapper_is_stable(self, source_type):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / 'first'
            second = Path(tmp) / 'second'
            for index, root in enumerate((first, second), start=1):
                wrapper = root / 'paths' / 'data'
                wrapper.mkdir(parents=True)
                archived = wrapper / 'payload'
                if source_type == 'file':
                    archived.write_bytes(b'payload')
                    os.utime(
                        archived,
                        ns=(1_700_000_000_000_000_000,) * 2,
                    )
                else:
                    archived.symlink_to('target')
                    os.utime(
                        archived,
                        ns=(1_700_000_000_000_000_000,) * 2,
                        follow_symlinks=False,
                    )
                meta = root / '_meta'
                meta.mkdir()
                (meta / 'inventory.json').write_text(json.dumps({
                    'paths': [{
                        'id': 'data',
                        'type': source_type,
                        'present': True,
                    }],
                }), encoding='utf-8')
                os.utime(
                    wrapper,
                    ns=((1_600_000_000 + index * 100_000_000)
                        * 1_000_000_000,) * 2,
                )

            self.assertEqual(
                stage_fingerprint(first),
                stage_fingerprint(second),
            )

    def test_same_size_and_mtime_content_change_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / 'first'
            second = Path(tmp) / 'second'
            self._tree(first, b'first')
            self._tree(second, b'other')

            self.assertNotEqual(
                stage_fingerprint(first),
                stage_fingerprint(second),
            )

    def test_hardlink_relationship_is_stable_and_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / 'first'
            second = Path(tmp) / 'second'
            third = Path(tmp) / 'third'
            for root in (first, second, third):
                self._tree(root)
            for root in (first, second):
                os.link(
                    root / 'paths' / 'data' / 'payload.bin',
                    root / 'paths' / 'data' / 'linked.bin',
                )
            separate = third / 'paths' / 'data' / 'linked.bin'
            separate.write_bytes(b'payload')
            os.utime(separate, ns=(1_700_000_000_000_000_000,) * 2)

            self.assertEqual(
                stage_fingerprint(first),
                stage_fingerprint(second),
            )
            self.assertNotEqual(
                stage_fingerprint(first),
                stage_fingerprint(third),
            )

    def test_snapshot_context_binds_repository_and_host(self):
        base = {
            'repository': 'rclone:remote:backups/one',
            'host_id': 'server-a',
        }
        repository_id = '1' * 64
        self.assertEqual(
            snapshot_context_fingerprint(base, repository_id),
            snapshot_context_fingerprint(dict(base), repository_id),
        )
        self.assertNotEqual(
            snapshot_context_fingerprint(base, repository_id),
            snapshot_context_fingerprint(base, '2' * 64),
        )
        self.assertNotEqual(
            snapshot_context_fingerprint(base, repository_id),
            snapshot_context_fingerprint({
                **base, 'host_id': 'server-b',
            }, repository_id),
        )
