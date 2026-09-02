from __future__ import annotations

import json
import unittest

from dgx_deploy.config import DEFAULT_PROFILE, config_sha256, load_config
from dgx_deploy.manifest import deployment_id, manifest_json
from test_config import valid_env, write_env


class ManifestTests(unittest.TestCase):
    def test_manifest_identity_is_deterministic_and_redacted(self) -> None:
        path = write_env(valid_env())
        try:
            config = load_config(path, DEFAULT_PROFILE)
        finally:
            path.unlink()
        first = manifest_json(config)
        second = manifest_json(config)
        self.assertEqual(first, second)
        value = json.loads(first)
        self.assertEqual(value["deployment_id"], deployment_id(config))
        self.assertEqual(value["config_sha256"], config_sha256(config))
        self.assertEqual(value["deployment"]["head_host"], "<redacted>")
        self.assertEqual(value["deployment"]["model_root"], "<redacted>")
        self.assertNotIn("/etc/ssh/known_hosts", first)


if __name__ == "__main__":
    unittest.main()
