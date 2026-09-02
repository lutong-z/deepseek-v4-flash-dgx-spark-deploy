from __future__ import annotations

import unittest

from dgx_deploy.redact import redact_mapping, redact_text


class RedactionTests(unittest.TestCase):
    def test_sensitive_keys_are_hidden(self) -> None:
        value = redact_mapping(
            {
                "head_host": "192.0.2.10",
                "model_root": "/srv/models",
                "ssh_identity_file": "/keys/operator",
                "safe": "value",
            }
        )
        self.assertEqual(value["head_host"], "<redacted>")
        self.assertEqual(value["model_root"], "<redacted>")
        self.assertEqual(value["ssh_identity_file"], "<redacted>")
        self.assertEqual(value["safe"], "value")

    def test_secret_text_is_hidden(self) -> None:
        self.assertNotIn("t" * 20, redact_text("access_token=" + "t" * 20))
        self.assertNotIn("s" * 20, redact_text("Bearer " + "s" * 20))

if __name__ == "__main__":
    unittest.main()
