from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from dgx_deploy import lmcache_server


class LMCacheServerTests(unittest.TestCase):
    def test_lookup_wrapper_refreshes_keys_before_submission(self) -> None:
        events: list[tuple[object, ...]] = []
        root = Path("/cache")

        class Adapter:
            def submit(self, keys: list[str], group_layouts: dict[int, object]) -> str:
                events.append(("submit", keys, group_layouts))
                return "task-id"

        def record_touch(path: Path, keys: list[str]) -> None:
            events.append(("touch", path, keys))

        wrapped = lmcache_server._wrap_key_submit(Adapter.submit, root)
        keys = ["a", "b"]
        layouts = {0: object()}
        with mock.patch.object(lmcache_server, "touch_keys", side_effect=record_touch):
            result = wrapped(Adapter(), keys, layouts)

        self.assertEqual(result, "task-id")
        self.assertEqual(events, [("touch", root, keys), ("submit", keys, layouts)])


if __name__ == "__main__":
    unittest.main()
