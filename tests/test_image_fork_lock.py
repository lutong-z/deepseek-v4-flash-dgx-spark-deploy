from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "image.fork.lock.json"
SCHEMA = ROOT / "config" / "image-fork-lock.schema.json"
SCRIPT = ROOT / "scripts" / "image" / "build-fork.sh"


class ForkLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lock = json.loads(LOCK.read_text(encoding="utf-8"))
        self.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    def test_lock_validates_against_schema(self) -> None:
        Draft202012Validator.check_schema(self.schema)
        errors = list(Draft202012Validator(self.schema).iter_errors(self.lock))
        self.assertEqual(errors, [])

    def test_lock_pins_exact_commits_and_fork_coordinates(self) -> None:
        for name in ("vllm", "lmcache", "b12x"):
            entry = self.lock[name]
            self.assertEqual(
                entry["repository"],
                f"https://github.com/lutong-z/{'LMCache' if name == 'lmcache' else name}.git",
            )
            self.assertRegex(entry["commit"], r"^[0-9a-f]{40}$")
            self.assertNotEqual(entry["ref"], "")
        # The production reproduction branch is the pinned vllm ref.
        self.assertEqual(self.lock["vllm"]["ref"], "release/production-20260907")
        # b12x must stay on the production-verified commit.
        self.assertEqual(
            self.lock["b12x"]["commit"],
            "d476465883cc7e46c128e0effa89fad1a7200cd7",
        )

    def test_folded_patches_match_build_system_files(self) -> None:
        folded = {p["file"] for p in self.lock["folded_in_patches"]}
        self.assertEqual(
            folded,
            {
                "docker/patch_vllm_flashinfer_b12x_swigluoai.py",
                "docker/patch_vllm_disable_minimax_qk_rmsnorm_ipc.py",
                "docker/patch_vllm_spark_kv_cache_cleanup.py",
            },
        )
        # The build script must no-op exactly these files.
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("FOLDED_PATCHES", script)
        self.assertIn("SKIP: patch folded into fork source", script)


class BuildForkScriptTests(unittest.TestCase):
    def test_script_is_bash_syntax_clean(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dry_run_clones_patches_and_builds_nothing(self) -> None:
        result = subprocess.run(
            ["bash", str(SCRIPT), "--work-dir", "/tmp/fork-build-dry-run", "--dry-run"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        # Every stage is announced.
        for stage in ("fetch", "prepare", "vllm", "lmcache", "final", "summary"):
            self.assertIn(f"[{stage}]", out)
        # Dry-run only prints; it never performs mutation.
        self.assertIn("DRY-RUN git clone", out)
        self.assertIn("DRY-RUN retarget", out)
        self.assertIn("no clone, patch, or docker mutation performed", out)
        # The eugr preset retargeting covers all four coordinate lines.
        self.assertIn("local-inference-lab/vllm", out)
        self.assertIn("dev/infernal-invocation", out)
        self.assertIn("lukealonso/b12x.git", out)
        # And the three folded patches are stubbed, not applied.
        for patch in (
            "patch_vllm_flashinfer_b12x_swigluoai.py",
            "patch_vllm_disable_minimax_qk_rmsnorm_ipc.py",
            "patch_vllm_spark_kv_cache_cleanup.py",
        ):
            self.assertIn(f"DRY-RUN no-op /tmp/fork-build-dry-run/spark-vllm-docker/docker/{patch}", out)

    def test_script_requires_work_dir(self) -> None:
        result = subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 64)
        self.assertIn("--work-dir is required", result.stderr)


if __name__ == "__main__":
    unittest.main()
