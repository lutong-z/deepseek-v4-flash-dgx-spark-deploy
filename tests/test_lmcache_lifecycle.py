from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from dgx_deploy.lmcache_lifecycle import L2Lifecycle, LifecycleError


class LMCacheLifecycleTests(unittest.TestCase):
    def test_initialization_refuses_unowned_nonempty_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "foreign.data").write_bytes(b"foreign")
            with self.assertRaises(LifecycleError):
                L2Lifecycle(root, 60, initialize_root=True)

    def test_restart_removes_only_owned_orphan_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            L2Lifecycle(root, 60, initialize_root=True)
            orphan = root / ".tmp" / "orphan.data"
            orphan.write_bytes(b"partial")

            L2Lifecycle(root, 60)

            self.assertFalse(orphan.exists())

    def test_scan_never_follows_symlinks_outside_owned_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "cache"
            lifecycle = L2Lifecycle(root, 60, initialize_root=True)
            outside = parent / "outside.data"
            outside.write_bytes(b"keep")
            (root / "link.data").symlink_to(outside)

            result = lifecycle.scan_once(now_ns=100_000_000_000)

            self.assertEqual(result.errors, 1)
            self.assertTrue(outside.exists())
            self.assertTrue((root / "link.data").is_symlink())
            self.assertFalse(lifecycle.is_healthy())

    def test_ttl_removes_complete_files_but_never_temp_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lifecycle = L2Lifecycle(
                root,
                60,
                max_bytes=1024,
                initialize_root=True,
            )
            stale = root / "stale.data"
            fresh = root / "fresh.data"
            temporary = root / ".tmp" / "in-flight.data"
            temporary.parent.mkdir(exist_ok=True)
            stale.write_bytes(b"stale")
            fresh.write_bytes(b"fresh")
            temporary.write_bytes(b"partial")
            now_ns = 100_000_000_000
            os.utime(stale, ns=(0, 0))
            os.utime(fresh, ns=(now_ns, now_ns))
            os.utime(temporary, ns=(0, 0))

            result = lifecycle.scan_once(now_ns=now_ns)

            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(temporary.exists())
            self.assertEqual(result.entries, 1)
            self.assertEqual(result.bytes, len(b"fresh"))
            self.assertEqual(result.expired_entries, 1)
            self.assertEqual(result.errors, 0)

    def test_capacity_pressure_trims_oldest_files_to_low_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lifecycle = L2Lifecycle(
                root,
                10_000,
                max_bytes=10,
                capacity_trim_ratio=0.5,
                initialize_root=True,
            )
            files = [root / f"object-{index}.data" for index in range(3)]
            for index, path in enumerate(files, 1):
                path.write_bytes(b"xxxx")
                os.utime(path, ns=(index, index))

            result = lifecycle.scan_once(now_ns=1_000_000_000)

            self.assertFalse(files[0].exists())
            self.assertFalse(files[1].exists())
            self.assertTrue(files[2].exists())
            self.assertEqual(result.capacity_evicted_entries, 2)
            self.assertEqual(result.capacity_evicted_bytes, 8)
            self.assertEqual(result.entries, 1)
            self.assertEqual(result.bytes, 4)
            metrics = lifecycle.prometheus_metrics().decode("ascii")
            self.assertIn("dgx_lmcache_l2_capacity_evicted_entries_total 2", metrics)
            self.assertIn("dgx_lmcache_l2_bytes 4", metrics)


if __name__ == "__main__":
    unittest.main()
