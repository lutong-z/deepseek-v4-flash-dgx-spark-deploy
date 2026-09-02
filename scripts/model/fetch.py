#!/usr/bin/env python3
"""Fetch the exact locked model into an external directory atomically.

The command never accepts a token argument or reads one from this repository.
Hugging Face access is delegated to ``snapshot_download`` with implicit token
use disabled, the full commit revision, and an exact lock-derived allowlist.
No model is fetched by importing this module; use ``--dry-run`` to inspect the
plan without network access.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

# Make direct execution work from any current directory without requiring an
# installed package.  The verifier itself has no network or optional imports.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from verify import (  # noqa: E402
    DEFAULT_LOCK,
    DEFAULT_SCHEMA,
    VerificationError,
    load_lock,
    verify_model_root,
)

ROOT = Path(__file__).resolve().parents[2]


class FetchError(RuntimeError):
    """A locked model could not be fetched or safely installed."""


def _lexists(path: Path) -> bool:
    """Return whether *path* exists, including a dangling symlink."""

    return os.path.lexists(path)



def _ensure_external_destination(destination: Path) -> None:
    """Reject repository-local installs and unsafe existing destinations."""

    if not destination.is_absolute():
        raise FetchError("model destination must be an absolute path outside the repository")
    # ``resolve`` normalizes harmless macOS /var and /tmp aliases while still
    # preventing a repository-local destination from being hidden by a path
    # spelling trick.
    try:
        normalized = destination.resolve(strict=False)
        repository = ROOT.resolve()
    except OSError as exc:
        raise FetchError("cannot resolve model destination") from exc
    if normalized == repository or repository in normalized.parents:
        raise FetchError("model destination must be outside the deploy repository")
    if _lexists(destination):
        raise FetchError(f"model destination already exists; refusing to overwrite: {destination}")
    parent = destination.parent
    if not _lexists(parent):
        raise FetchError(f"model destination parent does not exist: {parent}")
    try:
        parent_info = parent.lstat()
    except OSError as exc:
        raise FetchError(f"cannot inspect model destination parent: {parent}") from exc
    if not stat.S_ISDIR(parent_info.st_mode):
        raise FetchError("model destination parent must be a real directory")


def _safe_remove_hub_cache(staging: Path) -> None:
    """Remove only snapshot_download's known local-dir bookkeeping.

    ``snapshot_download(local_dir=...)`` may create ``.cache/huggingface``
    metadata.  It is not part of the model lock and must not be installed.  A
    symlink anywhere in this bookkeeping is treated as an attack rather than
    followed or removed.
    """

    cache_root = staging / ".cache"
    if not _lexists(cache_root):
        return
    if cache_root.is_symlink():
        raise FetchError("snapshot staging contains a symlinked cache directory")
    huggingface_cache = cache_root / "huggingface"
    if not _lexists(huggingface_cache):
        raise FetchError("snapshot staging contains an unexpected .cache directory")
    if huggingface_cache.is_symlink() or not huggingface_cache.is_dir():
        raise FetchError("snapshot staging contains an unsafe Hugging Face cache")
    # The cache was created inside our private temporary directory by the Hub
    # client.  shutil.rmtree does not follow directory symlinks; nevertheless,
    # reject symlinked entries first so a compromised client cannot surprise us.
    for current, directories, files in os.walk(huggingface_cache, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in directories + files:
            candidate = current_path / name
            if candidate.is_symlink():
                raise FetchError("snapshot staging cache contains a symlink")
    shutil.rmtree(huggingface_cache)
    try:
        cache_root.rmdir()
    except OSError:
        raise FetchError("snapshot staging contains unexpected .cache entries")


def _write_marker(staging: Path, lock_path: Path) -> None:
    """Create the install marker only after all download files are present."""

    # Import locally to keep the fetcher's public helpers usable in minimal
    # environments and to share the exact lock-byte digest implementation.
    from verify import lock_sha256

    marker_path = staging / ".model-lock.sha256"
    digest = lock_sha256(lock_path) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(marker_path, flags, 0o644)
    except OSError as exc:
        raise FetchError("cannot create model manifest marker") from exc
    try:
        with os.fdopen(fd, "w", encoding="ascii", newline="") as stream:
            stream.write(digest)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise FetchError("cannot write model manifest marker") from exc


def _remove_staging(staging: Path) -> None:
    """Best-effort cleanup limited to our uniquely-created staging directory."""

    if not _lexists(staging):
        return
    try:
        if staging.is_symlink() or not staging.is_dir():
            staging.unlink()
        else:
            shutil.rmtree(staging)
    except OSError:
        # Preserve the original fetch/verification error.  The path is printed
        # nowhere and remains a private temporary sibling for manual cleanup.
        pass


def _atomic_install(staging: Path, destination: Path) -> None:
    """Rename a verified sibling directory into place without overwriting.

    The parent lock serializes installers using this tool.  ``os.rename`` is
    then atomic because staging and destination are siblings on one
    filesystem; an existing destination is always rejected while holding the
    lock.
    """

    lock_path = destination.parent / ".model-fetch.lock"
    if _lexists(lock_path):
        try:
            lock_info = lock_path.lstat()
        except OSError as exc:
            raise FetchError("cannot inspect model install lock") from exc
        if stat.S_ISLNK(lock_info.st_mode) or not stat.S_ISREG(lock_info.st_mode):
            raise FetchError("model install lock must be a regular file")
    try:
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise FetchError("cannot create model install lock") from exc
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if _lexists(destination):
            raise FetchError(f"model destination appeared during fetch; refusing to overwrite: {destination}")
        try:
            os.rename(staging, destination)
        except FileExistsError as exc:
            raise FetchError("model destination appeared during atomic install") from exc
        except OSError as exc:
            raise FetchError("atomic model install failed") from exc
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def fetch_model(model_root: Path | str, lock_path: Path | str = DEFAULT_LOCK, *, schema_path: Path | str | None = DEFAULT_SCHEMA) -> dict[str, Any]:
    """Download and atomically install the locked model.

    Only the returned summary is exposed; no Hub response, URL, token, or local
    cache path is printed.  Any failure removes the private staging directory
    and leaves an existing destination untouched.
    """

    lock_file = Path(lock_path)
    try:
        lock = load_lock(lock_file, schema_path=schema_path)
    except VerificationError as exc:
        raise FetchError(str(exc)) from exc
    destination = Path(model_root)
    _ensure_external_destination(destination)

    parent = destination.parent
    staging: Path | None = None
    try:
        staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.fetch-", dir=parent))
        allow_patterns = [entry["path"] for entry in lock["files"]]
        # Never let the Hub client discover or transmit an implicit local token.
        os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise FetchError("huggingface_hub is required for model fetch; install it without a token") from exc
        try:
            downloaded = snapshot_download(
                repo_id=lock["model"]["repo_id"],
                revision=lock["model"]["revision"],
                local_dir=str(staging),
                allow_patterns=allow_patterns,
                token=False,
            )
        except Exception as exc:  # Hub errors vary by installed client version.
            raise FetchError("locked model download failed; no model was installed") from exc
        if Path(downloaded).absolute() != staging.absolute():
            raise FetchError("snapshot_download returned an unexpected directory")
        _safe_remove_hub_cache(staging)
        _write_marker(staging, lock_file)
        try:
            summary = verify_model_root(staging, lock_file, schema_path=schema_path)
        except (VerificationError, OSError) as exc:
            raise FetchError(f"downloaded model failed lock verification: {exc}") from exc
        _atomic_install(staging, destination)
        staging = None
        return summary
    finally:
        if staging is not None:
            _remove_staging(staging)


def fetch_plan(lock_path: Path | str = DEFAULT_LOCK, *, schema_path: Path | str | None = DEFAULT_SCHEMA) -> dict[str, Any]:
    """Return the exact no-network fetch plan for dry-run callers."""

    try:
        lock = load_lock(lock_path, schema_path=schema_path)
    except VerificationError as exc:
        raise FetchError(str(exc)) from exc
    return {
        "repo_id": lock["model"]["repo_id"],
        "revision": lock["model"]["revision"],
        "allow_patterns": [entry["path"] for entry in lock["files"]],
        "file_count": len(lock["files"]),
        "shard_count": lock["weights"]["shard_count"],
        "selected_size_bytes": lock["totals"]["selected_size_bytes"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_root", nargs="?", type=Path, help="absolute external directory for the installed model")
    parser.add_argument("--model-root", dest="model_root_option", type=Path, help="absolute external directory for the installed model")
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK, help="model lock JSON (default: repository model.lock.json)")
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA, help="model lock schema JSON")
    parser.add_argument("--dry-run", action="store_true", help="validate the lock and print the fetch plan without network access")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    model_root = args.model_root_option or args.model_root
    if args.model_root_option is not None and args.model_root is not None and args.model_root_option != args.model_root:
        parser.error("model root specified more than once")
    try:
        if args.dry_run:
            plan = fetch_plan(args.lock, schema_path=args.schema)
            print(
                f"dry-run: {plan['repo_id']} revision {plan['revision']} "
                f"({plan['file_count']} files, {plan['shard_count']} weight shards, "
                f"{plan['selected_size_bytes']} bytes); no network requested"
            )
            return 0
        if model_root is None:
            parser.error("a model root is required unless --dry-run is used")
        summary = fetch_model(model_root, args.lock, schema_path=args.schema)
    except (FetchError, VerificationError, OSError) as exc:
        print(f"model fetch failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"installed {summary['repo_id']} revision {summary['revision']} "
        f"({summary['file_count']} files, {summary['shard_count']} weight shards)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
