from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import subprocess
import types
import unittest
from pathlib import Path
from unittest.mock import patch

MODEL_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "model"
if str(MODEL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(MODEL_SCRIPTS))

import fetch  # noqa: E402
import verify  # noqa: E402


class ModelFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="dgx-model-test-")
        self.temp = Path(self.tempdir.name)
        self.model_root = self.temp / "model"
        self.model_root.mkdir()
        self.lock_path = self.temp / "model.lock.json"
        self.contents = {
            "LICENSE": b"MIT fixture license\n",
            "config.json": b'{"architectures":["FixtureForCausalLM"],"model_type":"fixture"}\n',
            "generation_config.json": b'{"do_sample":true,"temperature":1.0,"top_p":1.0}\n',
            "tokenizer.json": b'{"version":"1"}\n',
            "tokenizer_config.json": b'{"tokenizer_class":"FixtureTokenizer"}\n',
            "model.safetensors.index.json": b'{"weight_map":{"layer":"model-00001-of-00001.safetensors"}}\n',
            "model-00001-of-00001.safetensors": b"fixture shard\n",
        }
        roles = {
            "LICENSE": "license",
            "config.json": "config",
            "generation_config.json": "generation_config",
            "tokenizer.json": "tokenizer",
            "tokenizer_config.json": "tokenizer_config",
            "model.safetensors.index.json": "index",
        }
        self.lock = {
            "schema_version": 1,
            "model": {
                "repo_id": "fixture-org/fixture-model",
                "revision": "a" * 40,
                "library_name": "transformers",
                "model_type": "fixture",
                "architecture": "FixtureForCausalLM",
                "config_path": "config.json",
            },
            "provenance": {
                "model_api": "https://huggingface.co/api/models/fixture-org/fixture-model/revision/" + "a" * 40,
                "tree_api": "https://huggingface.co/api/models/fixture-org/fixture-model/tree/" + "a" * 40,
                "metadata_observed_on": "2026-09-02",
            },
            "license": {"spdx": "MIT", "file_path": "LICENSE"},
            "generation_config": {
                "path": "generation_config.json",
                "do_sample": True,
                "temperature": 1.0,
                "top_p": 1.0,
            },
            "chat_template": {"policy": "absent", "jinja": False},
            "tokenizer": {
                "tokenizer_path": "tokenizer.json",
                "config_path": "tokenizer_config.json",
                "tokenizer_class": "FixtureTokenizer",
            },
            "weights": {
                "format": "safetensors",
                "index_path": "model.safetensors.index.json",
                "shard_count": 1,
                "shards": ["model-00001-of-00001.safetensors"],
            },
            "install": {"manifest_marker": ".model-lock.sha256", "unexpected_files": "reject"},
            "totals": {"file_count": len(self.contents), "selected_size_bytes": 0, "weight_size_bytes": 0},
            "files": [],
        }
        for path, data in self.contents.items():
            self.lock["files"].append(
                {
                    "path": path,
                    "size": len(data),
                    "role": roles.get(path, "weight"),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        self._write_lock()
        for path, data in self.contents.items():
            (self.model_root / path).write_bytes(data)
        self._write_marker()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write_lock(self) -> None:
        self.lock["totals"]["selected_size_bytes"] = sum(item["size"] for item in self.lock["files"])
        self.lock["totals"]["weight_size_bytes"] = sum(item["size"] for item in self.lock["files"] if item["role"] == "weight")
        self.lock_path.write_text(json.dumps(self.lock, indent=2) + "\n", encoding="utf-8")

    def _write_marker(self) -> None:
        (self.model_root / ".model-lock.sha256").write_text(verify.lock_sha256(self.lock_path) + "\n", encoding="ascii")

    def _replace_and_relock(self, path: str, data: bytes) -> None:
        self.contents[path] = data
        for entry in self.lock["files"]:
            if entry["path"] == path:
                entry["size"] = len(data)
                entry["sha256"] = hashlib.sha256(data).hexdigest()
                break
        (self.model_root / path).write_bytes(data)
        self._write_lock()
        self._write_marker()

    def _assert_rejected(self) -> None:
        with self.assertRaises(verify.VerificationError):
            verify.verify_model_root(self.model_root, self.lock_path, schema_path=None)

    def test_fixture_verifies_with_hashes_and_index_closure(self) -> None:
        summary = verify.verify_model_root(self.model_root, self.lock_path, schema_path=None)
        self.assertEqual(summary["repo_id"], "fixture-org/fixture-model")
        self.assertEqual(summary["file_count"], 7)
        self.assertEqual(summary["shard_count"], 1)

    def test_git_blob_oid_is_verified_when_sha256_is_unavailable(self) -> None:
        license_entry = next(entry for entry in self.lock["files"] if entry["path"] == "LICENSE")
        license_entry.pop("sha256")
        header = f"blob {len(self.contents['LICENSE'])}\0".encode("ascii")
        license_entry["git_blob_sha1"] = hashlib.sha1(header + self.contents["LICENSE"]).hexdigest()
        self._write_lock()
        self._write_marker()
        self.assertEqual(verify.verify_model_root(self.model_root, self.lock_path, schema_path=None)["file_count"], 7)

    def test_tampered_shard_fails_hash_verification(self) -> None:
        (self.model_root / "model-00001-of-00001.safetensors").write_bytes(b"tampered\n")
        self._assert_rejected()

    def test_missing_or_extra_files_fail_closed(self) -> None:
        (self.model_root / "model-00001-of-00001.safetensors").unlink()
        self._assert_rejected()
        (self.model_root / "model-00001-of-00001.safetensors").write_bytes(self.contents["model-00001-of-00001.safetensors"])
        (self.model_root / "unexpected.txt").write_bytes(b"not locked\n")
        self._assert_rejected()

    def test_index_shard_set_mismatch_is_rejected(self) -> None:
        self._replace_and_relock(
            "model.safetensors.index.json",
            b'{"weight_map":{"layer":"model-00002-of-00002.safetensors"}}\n',
        )
        self._assert_rejected()

    def test_symlink_and_jinja_template_are_rejected(self) -> None:
        shard = self.model_root / "model-00001-of-00001.safetensors"
        shard.unlink()
        shard.symlink_to(self.temp / "outside")
        (self.temp / "outside").write_bytes(b"fixture shard\n")
        self._assert_rejected()
        shard.unlink()
        shard.write_bytes(self.contents["model-00001-of-00001.safetensors"])
        self._replace_and_relock("tokenizer_config.json", b'{"tokenizer_class":"FixtureTokenizer","chat_template":"{{ x }}"}\n')
        self._assert_rejected()

    def test_manifest_marker_is_bound_to_exact_lock_bytes(self) -> None:
        (self.model_root / ".model-lock.sha256").write_text("0" * 64 + "\n", encoding="ascii")
        self._assert_rejected()

    def test_lock_path_traversal_is_rejected(self) -> None:
        self.lock["files"][0]["path"] = "../LICENSE"
        self._write_lock()
        with self.assertRaises(verify.VerificationError):
            verify.load_lock(self.lock_path, schema_path=None)

    def test_fetch_uses_exact_revision_allowlist_and_atomic_install(self) -> None:
        calls: dict[str, object] = {}

        def fake_snapshot_download(**kwargs: object) -> str:
            calls.update(kwargs)
            staging = Path(str(kwargs["local_dir"]))
            for path, data in self.contents.items():
                (staging / path).write_bytes(data)
            return str(staging)

        destination = self.temp / "fetched-model"
        hub = types.SimpleNamespace(snapshot_download=fake_snapshot_download)
        with patch.dict(sys.modules, {"huggingface_hub": hub}):
            summary = fetch.fetch_model(destination, self.lock_path, schema_path=None)
        self.assertEqual(summary["revision"], "a" * 40)
        self.assertEqual(calls["repo_id"], "fixture-org/fixture-model")
        self.assertEqual(calls["revision"], "a" * 40)
        self.assertEqual(calls["token"], False)
        self.assertEqual(calls["allow_patterns"], [entry["path"] for entry in self.lock["files"]])
        self.assertEqual(verify.verify_model_root(destination, self.lock_path, schema_path=None)["file_count"], 7)
        self.assertFalse(list(self.temp.glob(".fetched-model.fetch-*")))

    def test_failed_fetch_cleans_staging_and_does_not_install(self) -> None:
        def failing_snapshot_download(**_: object) -> str:
            raise RuntimeError("network unavailable")

        destination = self.temp / "failed-model"
        hub = types.SimpleNamespace(snapshot_download=failing_snapshot_download)
        with patch.dict(sys.modules, {"huggingface_hub": hub}):
            with self.assertRaises(fetch.FetchError):
                fetch.fetch_model(destination, self.lock_path, schema_path=None)
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.temp.glob(".failed-model.fetch-*")))

    def test_existing_destination_is_never_overwritten(self) -> None:
        destination = self.temp / "existing-model"
        destination.mkdir()
        sentinel = destination / "sentinel"
        sentinel.write_bytes(b"keep")
        with self.assertRaises(fetch.FetchError):
            fetch.fetch_model(destination, self.lock_path, schema_path=None)
        self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_dry_run_plan_is_network_free(self) -> None:
        plan = fetch.fetch_plan(self.lock_path, schema_path=None)
        self.assertEqual(plan["repo_id"], "fixture-org/fixture-model")
        self.assertEqual(plan["file_count"], 7)
        self.assertEqual(plan["shard_count"], 1)

    def test_verify_wrapper_works_from_another_directory(self) -> None:
        wrapper = MODEL_SCRIPTS / "verify.sh"
        result = subprocess.run(
            [
                str(wrapper),
                str(self.model_root),
                "--lock",
                str(self.lock_path),
            ],
            cwd=self.temp,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verified fixture-org/fixture-model", result.stdout)


if __name__ == "__main__":
    unittest.main()
