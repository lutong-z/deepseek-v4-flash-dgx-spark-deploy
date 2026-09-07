from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PublicDocumentationTests(unittest.TestCase):
    def test_readme_has_zero_to_production_contract(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for marker in (
            "## Zero-to-production quickstart",
            "production.lock.json",
            "rollback.json",
            "--dry-run",
            "--confirm <DEPLOYMENT_ID>",
            "DEPLOYMENT_MODE=candidate",
            "API_PORT=18101",
            "MASTER_PORT=29621",
            "MODEL_ROOT=<REMOTE_PATH>/models/DeepSeek-V4-Flash-0731",
            "RootFS.Layers",
            "ssh_dgx()",
            "## Troubleshooting",
        ):
            self.assertIn(marker, readme)

    def test_model_and_network_docs_use_exact_layout_and_both_nodes(self) -> None:
        model = (ROOT / "docs" / "model.md").read_text(encoding="utf-8")
        networking = (ROOT / "docs" / "networking.md").read_text(encoding="utf-8")
        for marker in (
            "MODEL_ROOT` is the **model directory itself**",
            "DeepSeek-V4-Flash-0731",
            "54-file allowlist",
            "huggingface_hub==0.34.4",
            "ssh_dgx DGX-SPARK-0",
            "ssh_dgx DGX-SPARK-1",
            ".model-lock.sha256",
        ):
            self.assertIn(marker, model)
        for marker in (
            "ibdev2netdev -v",
            "API `8101`",
            "API `18101`",
            "MASTER_PORT=29621",
            "REMOTE_ROOT=<REMOTE_PATH>/dgx-spark-candidate/deploy",
            "ssh -N -T",
            "candidate-smoke.json",
        ):
            self.assertIn(marker, networking)

    def test_rollback_security_and_image_docs_are_current(self) -> None:
        rollback = (ROOT / "docs" / "rollback.md").read_text(encoding="utf-8")
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        image = (ROOT / "docs" / "image.md").read_text(encoding="utf-8")
        for marker in (
            "state_sha256",
            "environment",
            "mounts",
            "host settings",
            "partial-target",
            "rollback state integrity",
        ):
            self.assertIn(marker, rollback)
        self.assertNotIn("current CLI rejects", security)
        self.assertIn("canonical `lock_sha256`", security)
        for marker in (
            "metadata-only child",
            "RootFS.Layers",
            "docker save",
            "docker load",
            "RepoDigest",
            "lock_sha256",
        ):
            self.assertIn(marker, image)

    def test_public_docs_contain_no_private_paths_or_real_image_ids(self) -> None:
        paths = [ROOT / "README.md", ROOT / "SECURITY.md", *sorted((ROOT / "docs").glob("*.md"))]
        text = "\n".join(path.read_text(encoding="utf-8") for path in paths)
        self.assertIsNone(re.search(r"/(?:Users|home)/[A-Za-z0-9]", text))
        self.assertIsNone(re.search(r"sha256:[0-9a-f]{64}", text))
        self.assertNotIn("candidate/dsv4", text)

    def test_runtime_dependency_is_declared(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('dependencies = ["jsonschema>=4.20,<5"]', pyproject)


if __name__ == "__main__":
    unittest.main()
