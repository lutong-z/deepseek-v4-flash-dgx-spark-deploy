from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
IMAGE_LOCK = ROOT / "image.lock.json"
IMAGE_SCHEMA = ROOT / "config" / "image-lock.schema.json"
PROFILE = ROOT / "config" / "profiles" / "dsv4-native432-b12x-tp2.json"
PROFILE_SCHEMA = ROOT / "config" / "service-profile.schema.json"


def load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


class ImageLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lock = load_json(IMAGE_LOCK)
        self.schema = load_json(IMAGE_SCHEMA)
        self.profile = load_json(PROFILE)
        self.profile_schema = load_json(PROFILE_SCHEMA)

    def test_provisional_lock_and_profile_validate_against_schemas(self) -> None:
        image_validator = Draft202012Validator(self.schema)
        image_validator.check_schema(self.schema)
        self.assertEqual(list(image_validator.iter_errors(self.lock)), [])

        profile_validator = Draft202012Validator(self.profile_schema)
        profile_validator.check_schema(self.profile_schema)
        self.assertEqual(list(profile_validator.iter_errors(self.profile)), [])

    def test_lock_records_public_source_heads_and_partial_artifacts(self) -> None:
        self.assertEqual(self.lock["status"], "pending-artifacts")
        self.assertEqual(self.lock["profile_id"], self.profile["profile_id"])
        source = self.lock["source"]
        assert isinstance(source, dict)
        self.assertEqual(source["vllm"]["ref"], "release/dsv4-0731-native432-b12x")
        self.assertEqual(source["vllm"]["head"], "5817170bc04b0f203797c4a667f976bff49c12d4")
        self.assertEqual(source["b12x"]["ref"], "release/dsv4-0731-native432")
        self.assertEqual(source["b12x"]["head"], "d476465883cc7e46c128e0effa89fad1a7200cd7")
        self.assertEqual(
            self.lock["profile_sha256"],
            "4ba2758db623b6fab6d1f0e7055a35c1751fff78951d6126b823ea020a673d1e",
        )

        artifacts = self.lock["artifacts"]
        provenance = self.lock["provenance"]
        assert isinstance(artifacts, dict)
        assert isinstance(provenance, dict)
        self.assertEqual(
            artifacts["role_image_ids"],
            {
                "head": "sha256:3de518818a9cd0eb5d44473bf30e3e9766b8a13c35ebb79349fa372c11ce9535",
                "worker": "sha256:cbc26665c7a6739043ad1596940840acbfffafd43ecec6cb5289053841b0e4fc",
            },
        )
        self.assertEqual(
            artifacts["role_repo_digests"],
            {
                "head": None,
                "worker": "candidate/dsv4-native432-dspark5@sha256:cbc26665c7a6739043ad1596940840acbfffafd43ecec6cb5289053841b0e4fc",
            },
        )
        self.assertIsNone(artifacts["image_ref"])
        self.assertIsNone(artifacts["repo_digest"])
        self.assertIsNone(artifacts["config_digest"])
        self.assertIsNone(artifacts["image_id"])
        self.assertIsNone(artifacts["archive_sha256"])
        self.assertIsNone(provenance["build_method"])
        self.assertEqual(
            provenance["vllm_source_archive_sha256"],
            "28c744eb38585e4e77857e33b7a52a3b9d68ba8daed25c4dae0be137bb5188fd",
        )
        self.assertEqual(
            provenance["b12x_source_archive_sha256"],
            "6d5d16e667d58070757ba5af78fb9625c2b5d46b19b25e51b9e8285a518cdca1",
        )
        self.assertEqual(
            provenance["runtime_manifest_sha256"],
            "da3de6d274de7921c8188231ca147927ea01094d79f93db484ad28a289acb673",
        )
        self.assertIsNone(self.lock["base_image_ref"])
        self.assertIsNone(self.lock["dependency_lock_sha256"])
        self.assertIsNone(self.lock["service_contract_sha256"])
        self.assertNotIn("/Users/", json.dumps(self.lock))
        self.assertNotIn("/tmp/", json.dumps(self.lock))
        self.assertNotIn("local/", json.dumps(self.lock))
        self.assertNotIn("evidence/", json.dumps(self.lock))

    def test_containerfile_wires_only_reviewed_provenance_labels(self) -> None:
        containerfile = (ROOT / "container" / "Containerfile").read_text(encoding="utf-8")
        for argument in ("IMAGE_LOCK_SHA256", "SOURCE_REVISION", "VLLM_COMMIT", "B12X_COMMIT"):
            self.assertIn(f"ARG {argument}", containerfile)
        for label in (
            "org.opencontainers.image.revision",
            "com.dgx-spark.image_lock_sha256",
            "com.dgx-spark.vllm.commit",
            "com.dgx-spark.b12x.commit",
        ):
            self.assertIn(f"LABEL {label}", containerfile)

    def test_pending_lock_cannot_be_marked_ready_without_artifacts(self) -> None:
        candidate = copy.deepcopy(self.lock)
        candidate["status"] = "ready-for-review"
        errors = list(Draft202012Validator(self.schema).iter_errors(candidate))
        paths = {tuple(error.absolute_path) for error in errors}
        self.assertTrue(any(path[:1] == ("base_image_ref",) for path in paths))
        self.assertTrue(any(path[:1] == ("artifacts",) for path in paths))
        self.assertTrue(any(path[:1] == ("provenance",) for path in paths))


if __name__ == "__main__":
    unittest.main()
