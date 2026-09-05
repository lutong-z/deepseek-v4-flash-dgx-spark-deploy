from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

ROOT = (
    Path(__file__).resolve().parents[1]
    / "artifacts"
    / "native432-lmcache-rollout-20260904"
)


def _option_value(script: str, option: str) -> str:
    lines = [line.strip().removesuffix("\\").strip() for line in script.splitlines()]
    index = lines.index(option)
    return lines[index + 1]


class LMCacheRolloutTests(unittest.TestCase):
    def test_gpu_ttl_patch_and_images_are_digest_pinned(self) -> None:
        image_dir = ROOT / "image"
        metadata = json.loads(
            (image_dir / "prefix-cache-idle-ttl.metadata.json").read_text()
        )
        patch = (image_dir / "prefix-cache-idle-ttl.patch").read_bytes()

        self.assertEqual(hashlib.sha256(patch).hexdigest(), metadata["patch_sha256"])
        build = (image_dir / "build.sh").read_text()
        self.assertIn(f"PATCH_SHA256={metadata['patch_sha256']}", build)
        for role in ("head", "worker"):
            self.assertIn(f"BASE_ID={metadata['base_images'][role]}", build)
            containerfile = (image_dir / f"Containerfile.{role}").read_text()
            self.assertIn("COPY prefix-cache-idle-ttl.patch", containerfile)
            self.assertIn(metadata["patch_sha256"], containerfile)

    def test_server_defaults_match_layered_cache_budget(self) -> None:
        script = (ROOT / "commands" / "run-server.sh").read_text()
        metadata = json.loads(
            (ROOT / "image" / "streaming-restore-20260905.metadata.json").read_text()
        )

        self.assertIn('L1_GB="${LMCACHE_L1_GB:-1}"', script)
        self.assertIn('L2_TTL_SECONDS="${LMCACHE_L2_TTL_SECONDS:-7200}"', script)
        self.assertIn('L2_MAX_GB="${LMCACHE_L2_MAX_GB:-100}"', script)
        self.assertIn('L2_CAPACITY_TRIM_RATIO="${LMCACHE_L2_CAPACITY_TRIM_RATIO:-0.8}"', script)
        self.assertIn("--eviction-policy noop", script)
        self.assertIn("--l2-store-policy skip_l1", script)
        self.assertIn("--l2-prefetch-policy default", script)
        self.assertIn('"$L1_GB" "${LMCACHE_WORKER_REAP_TIMEOUT:-0}"', script)
        self.assertIn("label=com.dgx-spark.deployment_id", script)
        self.assertNotIn("label=com.dgx-spark.role'", script)
        for image_id in metadata["derived_images"].values():
            self.assertIn(f"DEFAULT_IMAGE={image_id}", script)

    def test_streaming_restore_images_are_digest_pinned(self) -> None:
        image_dir = ROOT / "image"
        metadata = json.loads(
            (image_dir / "streaming-restore-20260905.metadata.json").read_text()
        )
        build = (image_dir / "streaming-restore-20260905" / "build.sh").read_text()
        for role in ("head", "worker"):
            self.assertIn(f"BASE_ID={metadata['base_images'][role]}", build)
            containerfile = (
                image_dir / "streaming-restore-20260905" / f"Containerfile.{role}"
            ).read_text()
            self.assertIn("COPY overlay/", containerfile)
            for rel in metadata["files"]:
                self.assertTrue(
                    (image_dir / "streaming-restore-20260905" / "overlay" / rel).is_file(),
                    f"overlay file missing: {rel}",
                )

    def test_incident_fix_images_are_digest_pinned(self) -> None:
        image_dir = ROOT / "image"
        metadata = json.loads(
            (image_dir / "incident-fix-20260905.metadata.json").read_text()
        )
        build = (image_dir / "incident-fix-20260905" / "build.sh").read_text()
        for role in ("head", "worker"):
            self.assertIn(f"BASE_ID={metadata['base_images'][role]}", build)
            containerfile = (
                image_dir / "incident-fix-20260905" / f"Containerfile.{role}"
            ).read_text()
            self.assertIn("COPY overlay/", containerfile)
            for rel in metadata["files"]:
                self.assertTrue(
                    (image_dir / "incident-fix-20260905" / "overlay" / rel).is_file(),
                    f"overlay file missing: {rel}",
                )

    def test_engine_commands_reserve_memory_and_enable_gpu_ttl(self) -> None:
        metadata = json.loads(
            (ROOT / "image" / "incident-fix-20260905.metadata.json").read_text()
        )
        for role in ("head", "worker"):
            script = (ROOT / "commands" / f"create-{role}.sh").read_text()
            self.assertIn(f"IMAGE_ID={metadata['derived_images'][role]}", script)
            self.assertIn('  "$IMAGE_ID"\n  vllm\n  serve', script)
            self.assertNotIn(f"native432-lmcache:{role}-fork", script)
            self.assertEqual(
                _option_value(script, "--max-num-batched-tokens"),
                "1024",
            )
            self.assertEqual(_option_value(script, "--gpu-memory-utilization"), "0.85")
            self.assertEqual(
                _option_value(script, "--prefix-cache-idle-timeout-seconds"),
                "300",
            )
            self.assertIn('"kv_connector":"Native432LMCacheMPConnector"', script)
            self.assertIn('"kv_role":"kv_both"', script)


if __name__ == "__main__":
    unittest.main()
