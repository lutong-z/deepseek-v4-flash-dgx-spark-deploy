"""Bounded lifecycle and Prometheus metrics for a dedicated LMCache L2 root."""

from __future__ import annotations

import argparse
import os
import signal
import stat
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

MARKER_NAME = ".dgx-spark-lmcache-l2"
MARKER_CONTENT = b"dgx-spark-lmcache-l2-v1\n"
TEMP_DIR_NAME = ".tmp"


class LifecycleError(RuntimeError):
    """Raised when the cache root cannot be managed safely."""


@dataclass(frozen=True, slots=True)
class ScanResult:
    entries: int
    bytes: int
    expired_entries: int
    expired_bytes: int
    capacity_evicted_entries: int
    capacity_evicted_bytes: int
    errors: int


class L2Lifecycle:
    """Apply idle TTL and size-bounded LRU to a dedicated cache root."""

    def __init__(
        self,
        root: Path,
        ttl_seconds: float,
        *,
        max_bytes: int | None = None,
        capacity_trim_ratio: float = 0.8,
        initialize_root: bool = False,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than 0")
        if max_bytes is not None and max_bytes <= 0:
            raise ValueError("max_bytes must be greater than 0")
        if not 0 < capacity_trim_ratio < 1:
            raise ValueError("capacity_trim_ratio must be between 0 and 1")
        self.root = root
        self.ttl_ns = max(1, round(ttl_seconds * 1e9))
        self.max_bytes = max_bytes
        self.capacity_trim_ratio = capacity_trim_ratio
        self._lock = threading.Lock()
        self._entries = 0
        self._bytes = 0
        self._expired_entries_total = 0
        self._expired_bytes_total = 0
        self._capacity_evicted_entries_total = 0
        self._capacity_evicted_bytes_total = 0
        self._scan_errors_total = 0
        self._last_scan_timestamp_seconds = 0.0
        self._healthy = False
        self._prepare_root(initialize_root)

    def _prepare_root(self, initialize_root: bool) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        root_stat = self.root.lstat()
        if not stat.S_ISDIR(root_stat.st_mode) or self.root.is_symlink():
            raise LifecycleError(f"cache root must be a real directory: {self.root}")

        marker = self.root / MARKER_NAME
        try:
            marker_content = marker.read_bytes()
        except FileNotFoundError:
            if not initialize_root:
                raise LifecycleError(
                    f"cache root marker is missing: {marker}"
                ) from None
            unknown_entries = [
                entry.name
                for entry in os.scandir(self.root)
                if entry.name != MARKER_NAME
            ]
            if unknown_entries:
                raise LifecycleError(
                    "refusing to initialize a non-empty cache root without its marker"
                )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(marker, flags, 0o600)
            try:
                os.write(fd, MARKER_CONTENT)
                os.fsync(fd)
            finally:
                os.close(fd)
            marker_content = MARKER_CONTENT
        if marker_content != MARKER_CONTENT:
            raise LifecycleError(f"cache root marker is invalid: {marker}")
        self._prepare_temp_dir()

    def _prepare_temp_dir(self) -> None:
        """Remove crash leftovers before the LMCache server starts writing."""
        temp_dir = self.root / TEMP_DIR_NAME
        try:
            metadata = temp_dir.lstat()
        except FileNotFoundError:
            temp_dir.mkdir(mode=0o700)
            return
        if not stat.S_ISDIR(metadata.st_mode) or temp_dir.is_symlink():
            raise LifecycleError(
                f"cache temp path must be a real directory: {temp_dir}"
            )
        with os.scandir(temp_dir) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    raise LifecycleError(
                        f"cache temp path contains an unsafe entry: {entry.path}"
                    )
                os.unlink(entry.path)

    def _iter_files(self) -> Iterator[os.DirEntry[str]]:
        pending = [self.root]
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    if directory == self.root and entry.name in {
                        MARKER_NAME,
                        TEMP_DIR_NAME,
                    }:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        yield entry
                    else:
                        raise LifecycleError(
                            f"cache root contains an unsafe entry: {entry.path}"
                        )

    def scan_once(self, *, now_ns: int | None = None) -> ScanResult:
        if now_ns is None:
            now_ns = time.time_ns()
        entries = 0
        bytes_used = 0
        expired_entries = 0
        expired_bytes = 0
        capacity_evicted_entries = 0
        capacity_evicted_bytes = 0
        errors = 0
        retained: list[tuple[str, os.stat_result]] = []

        try:
            for entry in self._iter_files():
                try:
                    before = entry.stat(follow_symlinks=False)
                    if now_ns - before.st_mtime_ns >= self.ttl_ns:
                        current = os.stat(entry.path, follow_symlinks=False)
                        if (
                            stat.S_ISREG(current.st_mode)
                            and current.st_dev == before.st_dev
                            and current.st_ino == before.st_ino
                            and current.st_mtime_ns == before.st_mtime_ns
                        ):
                            os.unlink(entry.path)
                            expired_entries += 1
                            expired_bytes += current.st_size
                            continue
                        before = current
                    entries += 1
                    bytes_used += before.st_size
                    retained.append((entry.path, before))
                except FileNotFoundError:
                    continue
                except OSError:
                    errors += 1
        except (OSError, LifecycleError):
            errors += 1

        if self.max_bytes is not None and bytes_used > self.max_bytes:
            trim_target = int(self.max_bytes * self.capacity_trim_ratio)
            for path, before in sorted(
                retained, key=lambda candidate: candidate[1].st_mtime_ns
            ):
                if bytes_used <= trim_target:
                    break
                try:
                    current = os.stat(path, follow_symlinks=False)
                    if (
                        not stat.S_ISREG(current.st_mode)
                        or current.st_dev != before.st_dev
                        or current.st_ino != before.st_ino
                        or current.st_mtime_ns != before.st_mtime_ns
                    ):
                        continue
                    os.unlink(path)
                    entries -= 1
                    bytes_used -= current.st_size
                    capacity_evicted_entries += 1
                    capacity_evicted_bytes += current.st_size
                except FileNotFoundError:
                    entries -= 1
                    bytes_used -= before.st_size
                except OSError:
                    errors += 1

        result = ScanResult(
            entries=entries,
            bytes=bytes_used,
            expired_entries=expired_entries,
            expired_bytes=expired_bytes,
            capacity_evicted_entries=capacity_evicted_entries,
            capacity_evicted_bytes=capacity_evicted_bytes,
            errors=errors,
        )
        with self._lock:
            self._entries = result.entries
            self._bytes = result.bytes
            self._expired_entries_total += result.expired_entries
            self._expired_bytes_total += result.expired_bytes
            self._capacity_evicted_entries_total += result.capacity_evicted_entries
            self._capacity_evicted_bytes_total += result.capacity_evicted_bytes
            self._scan_errors_total += result.errors
            self._last_scan_timestamp_seconds = now_ns / 1e9
            self._healthy = result.errors == 0
        return result

    def is_healthy(self) -> bool:
        with self._lock:
            return self._healthy

    def prometheus_metrics(self) -> bytes:
        with self._lock:
            values = {
                "entries": self._entries,
                "bytes": self._bytes,
                "expired_entries_total": self._expired_entries_total,
                "expired_bytes_total": self._expired_bytes_total,
                "capacity_evicted_entries_total": (
                    self._capacity_evicted_entries_total
                ),
                "capacity_evicted_bytes_total": self._capacity_evicted_bytes_total,
                "scan_errors_total": self._scan_errors_total,
                "last_scan_timestamp_seconds": self._last_scan_timestamp_seconds,
            }
        lines = [
            "# HELP dgx_lmcache_l2_entries Current persistent L2 cache files.",
            "# TYPE dgx_lmcache_l2_entries gauge",
            f"dgx_lmcache_l2_entries {values['entries']}",
            "# HELP dgx_lmcache_l2_bytes Current persistent L2 cache bytes.",
            "# TYPE dgx_lmcache_l2_bytes gauge",
            f"dgx_lmcache_l2_bytes {values['bytes']}",
            "# HELP dgx_lmcache_l2_expired_entries_total Files removed by TTL.",
            "# TYPE dgx_lmcache_l2_expired_entries_total counter",
            f"dgx_lmcache_l2_expired_entries_total {values['expired_entries_total']}",
            "# HELP dgx_lmcache_l2_expired_bytes_total Bytes removed by TTL.",
            "# TYPE dgx_lmcache_l2_expired_bytes_total counter",
            f"dgx_lmcache_l2_expired_bytes_total {values['expired_bytes_total']}",
            "# HELP dgx_lmcache_l2_capacity_evicted_entries_total Files removed by size-bounded LRU.",
            "# TYPE dgx_lmcache_l2_capacity_evicted_entries_total counter",
            "dgx_lmcache_l2_capacity_evicted_entries_total "
            f"{values['capacity_evicted_entries_total']}",
            "# HELP dgx_lmcache_l2_capacity_evicted_bytes_total Bytes removed by size-bounded LRU.",
            "# TYPE dgx_lmcache_l2_capacity_evicted_bytes_total counter",
            "dgx_lmcache_l2_capacity_evicted_bytes_total "
            f"{values['capacity_evicted_bytes_total']}",
            "# HELP dgx_lmcache_l2_scan_errors_total Lifecycle scan errors.",
            "# TYPE dgx_lmcache_l2_scan_errors_total counter",
            f"dgx_lmcache_l2_scan_errors_total {values['scan_errors_total']}",
            "# HELP dgx_lmcache_l2_last_scan_timestamp_seconds Last completed scan.",
            "# TYPE dgx_lmcache_l2_last_scan_timestamp_seconds gauge",
            "dgx_lmcache_l2_last_scan_timestamp_seconds "
            f"{values['last_scan_timestamp_seconds']}",
            "",
        ]
        return "\n".join(lines).encode("ascii")


def _handler(lifecycle: L2Lifecycle) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/metrics":
                body = lifecycle.prometheus_metrics()
                status = HTTPStatus.OK
                content_type = "text/plain; version=0.0.4; charset=utf-8"
            elif self.path == "/health":
                healthy = lifecycle.is_healthy()
                body = b"ok\n" if healthy else b"unhealthy\n"
                status = HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE
                content_type = "text/plain; charset=utf-8"
            else:
                body = b"not found\n"
                status = HTTPStatus.NOT_FOUND
                content_type = "text/plain; charset=utf-8"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def run(
    lifecycle: L2Lifecycle,
    *,
    scan_interval_seconds: float,
    listen_host: str,
    metrics_port: int,
) -> None:
    if scan_interval_seconds <= 0:
        raise ValueError("scan_interval_seconds must be greater than 0")
    stop = threading.Event()

    def scan_loop() -> None:
        while not stop.is_set():
            lifecycle.scan_once()
            stop.wait(scan_interval_seconds)

    scanner = threading.Thread(target=scan_loop, name="lmcache-l2-ttl", daemon=True)
    scanner.start()
    server = ThreadingHTTPServer((listen_host, metrics_port), _handler(lifecycle))
    server.timeout = 1.0

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        while not stop.is_set():
            server.handle_request()
    finally:
        stop.set()
        scanner.join(timeout=max(1.0, scan_interval_seconds + 1.0))
        server.server_close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--ttl-seconds", type=float, required=True)
    parser.add_argument("--max-size-gb", type=float, required=True)
    parser.add_argument("--capacity-trim-ratio", type=float, default=0.8)
    parser.add_argument("--scan-interval-seconds", type=float, default=60.0)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--metrics-port", type=int, default=19120)
    parser.add_argument("--initialize-root", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    lifecycle = L2Lifecycle(
        args.root,
        args.ttl_seconds,
        max_bytes=round(args.max_size_gb * 1024**3),
        capacity_trim_ratio=args.capacity_trim_ratio,
        initialize_root=args.initialize_root,
    )
    run(
        lifecycle,
        scan_interval_seconds=args.scan_interval_seconds,
        listen_host=args.listen_host,
        metrics_port=args.metrics_port,
    )


if __name__ == "__main__":
    main()
