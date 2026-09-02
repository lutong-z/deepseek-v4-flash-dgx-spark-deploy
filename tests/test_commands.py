from __future__ import annotations

import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

from dgx_deploy.config import DEFAULT_PROFILE, load_config
from dgx_deploy.render import render_container_argv, render_environment, render_service_argv
from test_config import valid_env, write_env


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env_path = write_env(valid_env())
        self.config = load_config(self.env_path, DEFAULT_PROFILE)

    def tearDown(self) -> None:
        self.env_path.unlink()

    def test_worker_starts_first_and_has_no_api_flags(self) -> None:
        worker = render_service_argv(self.config, "worker")
        head = render_service_argv(self.config, "head")
        self.assertEqual(worker[worker.index("--node-rank") + 1], "1")
        self.assertEqual(head[head.index("--node-rank") + 1], "0")
        self.assertIn("--headless", worker)
        self.assertNotIn("--host", worker)
        self.assertIn("--host", head)
        self.assertNotIn("FLASHINFER", " ".join(worker + head))

    def test_transport_environment_is_derived_by_role(self) -> None:
        worker = render_environment(self.config, "worker")
        self.assertEqual(worker["VLLM_HOST_IP"], "192.0.2.11")
        self.assertEqual(worker["NCCL_SOCKET_IFNAME"], "rdma0")
        self.assertEqual(worker["NCCL_IB_DISABLE"], "0")

    def test_container_argv_uses_read_only_model_mount(self) -> None:
        command = render_container_argv(self.config, "head")
        self.assertIn("--read-only", command)
        mount = command[command.index("--mount") + 1]
        self.assertEqual(mount, "type=bind,src=/srv/models,dst=/models,readonly")
        self.assertIn("--security-opt", command)
        self.assertIn("no-new-privileges:true", command)

    def test_cli_renders_redacted_plan_and_rejects_apply(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            result = __import__("dgx_deploy.cli", fromlist=["main"]).main(
                ["plan", "--env-file", str(self.env_path)]
            )
        self.assertEqual(result, 0)
        self.assertNotIn("192.0.2.10", output.getvalue())
        self.assertNotIn("/srv/models", output.getvalue())
        errors = StringIO()
        with redirect_stderr(errors):
            result = __import__("dgx_deploy.cli", fromlist=["main"]).main(
                ["plan", "--env-file", str(self.env_path), "--apply"]
            )
        self.assertEqual(result, 78)
        self.assertIn("mutation is disabled", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
