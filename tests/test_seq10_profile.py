from __future__ import annotations

import json
import unittest

from dgx_deploy.config import CANDIDATE_SEQ10_PROFILE, DEFAULT_PROFILE, load_config
from dgx_deploy.render import render_contract, render_service_argv
from tests.test_config import valid_env, write_env


class Seq10CandidateProfileTests(unittest.TestCase):
    def _candidate_values(self) -> dict[str, str]:
        values = valid_env()
        values.update(
            {
                "DEPLOYMENT_MODE": "candidate",
                "REMOTE_ROOT": "/srv/dgx-spark/deploy-candidate",
                "STATE_ROOT": "/var/lib/dgx-spark/state-candidate",
                "CACHE_ROOT": "/var/cache/dgx-spark/cache-candidate",
                "LOG_ROOT": "/var/log/dgx-spark/logs-candidate",
                "RESULT_ROOT": "/var/lib/dgx-spark/results-candidate",
                "MASTER_ADDR": "192.168.100.10",
                "MASTER_PORT": "29621",
                "API_PORT": "18101",
                "HEAD_NODE_ADDR": "192.168.100.10",
                "WORKER_NODE_ADDR": "192.168.100.11",
                "HEAD_IMAGE_REF": "candidate/head@sha256:" + "c" * 64,
                "WORKER_IMAGE_REF": "candidate/worker@sha256:" + "d" * 64,
                "MAX_NUM_SEQS": "10",
                "MAX_NUM_BATCHED_TOKENS": "8192",
            }
        )
        return values

    def test_runtime_limits_override_profile_defaults_without_changing_profile(self) -> None:
        path = write_env(self._candidate_values())
        try:
            config = load_config(path, DEFAULT_PROFILE)
        finally:
            path.unlink()
        self.assertEqual(config["profile"]["limits"]["max_num_seqs"], 5)
        self.assertEqual(config["profile"]["limits"]["max_num_batched_tokens"], 1024)
        self.assertEqual(config["deployment"]["runtime_max_num_seqs"], 10)
        self.assertEqual(config["deployment"]["runtime_max_num_batched_tokens"], 8192)
        argv = render_service_argv(config, "head")
        self.assertEqual(argv[argv.index("--max-num-seqs") + 1], "10")
        self.assertEqual(argv[argv.index("--max-num-batched-tokens") + 1], "8192")
        self.assertEqual(argv[argv.index("--block-size") + 1], "256")
        self.assertEqual(argv[argv.index("--max-model-len") + 1], "327680")

    def test_runtime_overrides_require_candidate_and_complete_pair(self) -> None:
        values = self._candidate_values()
        values["DEPLOYMENT_MODE"] = "production"
        path = write_env(values)
        try:
            with self.assertRaises(ValueError):
                load_config(path, DEFAULT_PROFILE)
        finally:
            path.unlink()
        values = self._candidate_values()
        values.pop("MAX_NUM_BATCHED_TOKENS")
        path = write_env(values)
        try:
            with self.assertRaises(ValueError):
                load_config(path, DEFAULT_PROFILE)
        finally:
            path.unlink()

    def test_seq10_profile_is_candidate_only_and_renders_limits(self) -> None:
        path = write_env(self._candidate_values())
        try:
            config = load_config(path, CANDIDATE_SEQ10_PROFILE)
        finally:
            path.unlink()
        self.assertEqual(config["profile"]["limits"], {"max_model_len": 327680, "max_num_seqs": 10, "max_num_batched_tokens": 8192})
        argv = render_service_argv(config, "head")
        self.assertEqual(argv[argv.index("--max-num-seqs") + 1], "10")
        self.assertEqual(argv[argv.index("--max-num-batched-tokens") + 1], "8192")
        self.assertEqual(render_contract(config, "head")["container"], "dsv4-candidate-native432-dspark5-327k-seq10-head")

    def test_seq10_profile_cannot_be_used_for_production(self) -> None:
        values = self._candidate_values()
        values["DEPLOYMENT_MODE"] = "production"
        path = write_env(values)
        try:
            with self.assertRaises(ValueError):
                load_config(path, CANDIDATE_SEQ10_PROFILE)
        finally:
            path.unlink()

    def test_production_profile_remains_seq5(self) -> None:
        profile = json.loads(DEFAULT_PROFILE.read_text(encoding="utf-8"))
        self.assertEqual(profile["limits"]["max_num_seqs"], 5)
        self.assertEqual(profile["limits"]["max_num_batched_tokens"], 1024)


if __name__ == "__main__":
    unittest.main()
