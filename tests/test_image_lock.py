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

    def test_lock_records_public_source_heads_and_pending_artifacts(self) -> None:
        self.assertEqual(self.lock["status"], "pending-artifacts")
        self.assertEqual(self.lock["profile_id"], self.profile["profile_id"])
        source = self.lock["source"]
        assert isinstance(source, dict)
        self.assertEqual(source["vllm"]["ref"], "release/dsv4-0731-native432-b12x")
        self.assertEqual(source["vllm"]["head"], "5817170bc0")
        self.assertEqual(source["b12x"]["ref"], "release/dsv4-0731-native432")
        self.assertEqual(source["b12x"]["head"], "d476465")

        artifacts = self.lock["artifacts"]
        provenance = self.lock["provenance"]
        assert isinstance(artifacts, dict)
        assert isinstance(provenance, dict)
        self.assertTrue(all(value is None for key, value in artifacts.items() if key != "role_image_ids"))
        self.assertEqual(artifacts["role_image_ids"], {"head": None, "worker": None})
        self.assertTrue(all(value is None for value in provenance.values()))
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
