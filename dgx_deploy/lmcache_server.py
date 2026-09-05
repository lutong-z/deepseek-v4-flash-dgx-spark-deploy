"""Run LMCache MP server with filesystem-L2 access timestamps refreshed."""

from __future__ import annotations

import functools
import os
import runpy
import stat
from pathlib import Path
from typing import Any, Callable, Iterable


def touch_keys(root: Path, keys: Iterable[Any]) -> None:
    """Refresh final-object mtimes so the external sweeper implements LRU."""
    from lmcache.v1.distributed.l2_adapters.fs_l2_adapter import (
        _object_key_to_filename,
    )

    for key in keys:
        path = root / _object_key_to_filename(key)
        try:
            metadata = path.stat(follow_symlinks=False)
            if stat.S_ISREG(metadata.st_mode):
                os.utime(path, None, follow_symlinks=False)
        except OSError:
            # A lookup miss or concurrent GC must degrade to an L2 miss, not
            # fail an inference request.
            continue


def _wrap_key_submit(method: Callable[..., Any], root: Path) -> Callable[..., Any]:
    @functools.wraps(method)
    def wrapped(self: Any, keys: Iterable[Any], *args: Any, **kwargs: Any) -> Any:
        touch_keys(root, keys)
        return method(self, keys, *args, **kwargs)

    return wrapped


def install_access_tracking(root: Path) -> None:
    """Patch the pinned native connector before the server constructs it."""
    from lmcache.v1.distributed.l2_adapters.native_connector_l2_adapter import (
        NativeConnectorL2Adapter,
    )

    if getattr(NativeConnectorL2Adapter, "_dgx_l2_access_tracking", False):
        return
    name = "submit_lookup_and_lock_task"
    method = getattr(NativeConnectorL2Adapter, name)
    setattr(NativeConnectorL2Adapter, name, _wrap_key_submit(method, root))
    NativeConnectorL2Adapter._dgx_l2_access_tracking = True


def main() -> None:
    root_value = os.environ.get("LMCACHE_L2_ROOT")
    if not root_value:
        raise RuntimeError("LMCACHE_L2_ROOT is required")
    root = Path(root_value)
    install_access_tracking(root)
    runpy.run_module("lmcache.v1.multiprocess.server", run_name="__main__")


if __name__ == "__main__":
    main()
