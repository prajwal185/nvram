#!/usr/bin/env python3
"""
CloudOS (Refactored, Error-Resilient Single-File Build)
------------------------------------------------------
Goals:
- Run without errors on Windows/Linux/macOS using only Python stdlib + optional deps.
- Provide *local-first* operation so it works even without GCP credentials.
- Support optional Google Cloud Storage (GCS) if libraries/creds are present.
- Clean CLI: `setup`, `server`, `client`, `show-config`.
- No privileged system calls, no package installation attempts.

Quick start:
    python cloudos_refactored.py setup
    python cloudos_refactored.py server
    # In another terminal:
    python cloudos_refactored.py client --once

Optional GCP mode:
- If `google-cloud-storage` is installed *and* `GOOGLE_APPLICATION_CREDENTIALS` is set
  to a valid service account JSON, set `--storage-backend gcs` (or set env
  `CLOUDOS_STORAGE_BACKEND=gcs`) and specify `--bucket`.
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import platform
import sys
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional

# --------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("cloudos")

# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------
DEFAULT_SETUP_DIR = Path.home() / ".cloudos"
DEFAULT_MOUNTPOINT = DEFAULT_SETUP_DIR / "mount"
DEFAULT_BACKEND_URL = "http://127.0.0.1:5000"
CONFIG_FILE = DEFAULT_SETUP_DIR / "config.json"

SUPPORTED_BACKENDS = {"local", "gcs"}

@dataclass
class Config:
    setup_dir: str
    mountpoint: str
    backend_url: str
    storage_backend: str  # "local" or "gcs"
    project_id: str
    bucket_name: Optional[str] = None
    setup_version: str = "2.0.0"
    platform: str = platform.system().lower()

    @staticmethod
    def default(storage_backend: str = "local",
                bucket_name: Optional[str] = None) -> "Config":
        return Config(
            setup_dir=str(DEFAULT_SETUP_DIR),
            mountpoint=str(DEFAULT_MOUNTPOINT),
            backend_url=DEFAULT_BACKEND_URL,
            storage_backend=storage_backend,
            project_id=f"cloudos-{uuid.uuid4().hex[:8]}",
            bucket_name=bucket_name,
        )

    @staticmethod
    def load(path: Path = CONFIG_FILE) -> "Config":
        if not path.exists():
            raise FileNotFoundError(f"Config not found at {path}. Run 'setup' first.")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Config(**data)

    def save(self, path: Path = CONFIG_FILE) -> None:
        Path(self.setup_dir).mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)
        logger.info("Saved config to %s", path)

# --------------------------------------------------------------------------------------
# Storage backends
# --------------------------------------------------------------------------------------
class StorageBackend:
    def list_files(self, path: str) -> List[str]:
        raise NotImplementedError

    def read_text(self, path: str) -> str:
        raise NotImplementedError

    def write_text(self, path: str, content: str) -> None:
        raise NotImplementedError

class LocalStorage(StorageBackend):
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _abspath(self, rel: str) -> Path:
        rel = rel.lstrip("/\\")
        return (self.root / rel).resolve()

    def list_files(self, path: str) -> List[str]:
        base = self._abspath(path)
        if not base.exists():
            return []
        if base.is_file():
            return [str(Path(path).as_posix())]
        items: List[str] = []
        for p in base.rglob("*"):
            if p.is_file():
                try:
                    items.append(str(p.relative_to(self.root).as_posix()))
                except Exception:
                    pass
        return sorted(items)

    def read_text(self, path: str) -> str:
        p = self._abspath(path)
        with open(p, "r", encoding="utf-8") as f:
            return f.read()

    def write_text(self, path: str, content: str) -> None:
        p = self._abspath(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)

class GCSStorage(StorageBackend):
    def __init__(self, bucket_name: str):
        try:
            from google.cloud import storage  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "google-cloud-storage not available. Install it or use local backend"
            ) from e
        try:
            self.client = storage.Client()
        except Exception as e:
            raise RuntimeError(
                "Failed to init GCS client. Ensure GOOGLE_APPLICATION_CREDENTIALS is set"
            ) from e
        self.bucket = self.client.bucket(bucket_name)

    def list_files(self, path: str) -> List[str]:
        prefix = path.lstrip("/\\")
        return [b.name for b in self.bucket.list_blobs(prefix=prefix)]

    def read_text(self, path: str) -> str:
        blob = self.bucket.blob(path.lstrip("/\\"))
        if not blob.exists():
            raise FileNotFoundError(path)
        return blob.download_as_text()

    def write_text(self, path: str, content: str) -> None:
        blob = self.bucket.blob(path.lstrip("/\\"))
        blob.upload_from_string(content, content_type="text/plain")

# --------------------------------------------------------------------------------------
# Backend factory
# --------------------------------------------------------------------------------------

def make_backend(cfg: Config) -> StorageBackend:
    backend_env = os.getenv("CLOUDOS_STORAGE_BACKEND")
    backend = (backend_env or cfg.storage_backend or "local").lower()
    if backend not in SUPPORTED_BACKENDS:
        logger.warning("Unknown backend '%s', falling back to 'local'", backend)
        backend = "local"

    if backend == "gcs":
        if not cfg.bucket_name:
            raise RuntimeError("GCS backend selected but bucket_name is not set")
        logger.info("Using GCS backend (bucket=%s)", cfg.bucket_name)
        return GCSStorage(cfg.bucket_name)

    logger.info("Using local backend at %s", cfg.mountpoint)
    return LocalStorage(Path(cfg.mountpoint))

# --------------------------------------------------------------------------------------
# Web server
# --------------------------------------------------------------------------------------

def start_server(cfg: Config, host: str = "127.0.0.1", port: int = 5000) -> None:
    try:
        from flask import Flask, jsonify, request
    except Exception as e:
        raise RuntimeError("Flask is required for server mode.\nInstall with: pip install flask") from e

    app = Flask(__name__)
    backend = make_backend(cfg)

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "backend": cfg.storage_backend, "platform": cfg.platform})

    @app.get("/list")
    def list_files_endpoint():
        path = request.args.get("path", "")
        files = backend.list_files(path)
        return jsonify({"files": files})

    @app.get("/read")
    def read_endpoint():
        path = request.args.get("path")
        if not path:
            return jsonify({"error": "missing path"}), 400
        try:
            content = backend.read_text(path)
        except FileNotFoundError:
            return jsonify({"error": "not found"}), 404
        return jsonify({"path": path, "content": content})

    @app.post("/write")
    def write_endpoint():
        data = request.get_json(silent=True) or {}
        path = (data.get("path") or "").strip()
        content = data.get("content")
        if not path:
            return jsonify({"error": "missing path"}), 400
        if content is None:
            return jsonify({"error": "missing content"}), 400
        backend.write_text(path, str(content))
        return jsonify({"ok": True, "path": path})

    logger.info("CloudOS server listening on %s:%d", host, port)
    app.run(host=host, port=port, debug=False)

# --------------------------------------------------------------------------------------
# Client (simple poller + demo I/O)
# --------------------------------------------------------------------------------------

def client_once(cfg: Config) -> None:
    import urllib.request
    import urllib.parse
    import json as _json

    base = cfg.backend_url.rstrip("/")

    if cfg.storage_backend == "local":
        Path(cfg.mountpoint).mkdir(parents=True, exist_ok=True)

    demo_path = "demo/hello.txt"
    content = f"Hello from CloudOS client on {platform.system()}!\n"

    try:
        req = urllib.request.Request(
            base + "/write",
            data=_json.dumps({"path": demo_path, "content": content}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            _ = resp.read()

        with urllib.request.urlopen(base + f"/read?path={urllib.parse.quote(demo_path)}", timeout=10) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
            logger.info("Read back: %s", (data.get("content") or "").strip())

        with urllib.request.urlopen(base + "/list?path=demo/", timeout=10) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
            logger.info("Files under demo/: %s", data.get("files"))
    except Exception as e:
        logger.warning("Client request failed: %s", e)
        raise


def start_client(cfg: Config, interval: int = 60, once: bool = False) -> None:
    logger.info("Starting CloudOS client. Backend URL: %s", cfg.backend_url)
    if once:
        try:
            client_once(cfg)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.warning("Client iteration failed: %s", e)
        return

    while True:
        try:
            client_once(cfg)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.warning("Client iteration failed: %s", e)
        time.sleep(max(5, interval))

# --------------------------------------------------------------------------------------
# Setup helpers
# --------------------------------------------------------------------------------------

def do_setup(storage_backend: str, bucket: Optional[str]) -> None:
    storage_backend = (storage_backend or os.getenv("CLOUDOS_STORAGE_BACKEND") or "local").lower()
    if storage_backend not in SUPPORTED_BACKENDS:
        logger.warning("Unsupported backend '%s', using 'local' instead", storage_backend)
        storage_backend = "local"

    cfg = Config.default(storage_backend=storage_backend, bucket_name=bucket)

    setup_dir = Path(cfg.setup_dir)
    for d in [setup_dir, setup_dir / "logs", setup_dir / "cache", setup_dir / "mount", setup_dir / "credentials"]:
        d.mkdir(parents=True, exist_ok=True)

    env_file = setup_dir / "cloudos.env"
    with open(env_file, "w", encoding="utf-8") as f:
        f.write(f"CLOUDOS_STORAGE_BACKEND={cfg.storage_backend}\n")
        if cfg.bucket_name:
            f.write(f"CLOUDOS_BUCKET={cfg.bucket_name}\n")
        f.write(f"CLOUDOS_BACKEND_URL={cfg.backend_url}\n")
        f.write(f"CLOUDOS_MOUNTPOINT={cfg.mountpoint}\n")

    cfg.save()
    logger.info("Setup complete. Backend: %s", cfg.storage_backend)

# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="cloudos", description="CloudOS refactored CLI")
    sub = p.add_subparsers(dest="cmd")

    p_setup = sub.add_parser("setup", help="Initialize config and folders")
    p_setup.add_argument("--storage-backend", choices=sorted(SUPPORTED_BACKENDS), default="local",
                         help="Storage backend to use (default: local)")
    p_setup.add_argument("--bucket", default=None, help="GCS bucket name (required for gcs backend)")

    p_server = sub.add_parser("server", help="Run HTTP API server")
    p_server.add_argument("--host", default="127.0.0.1")
    p_server.add_argument("--port", type=int, default=5000)

    p_client = sub.add_parser("client", help="Run simple demo client")
    p_client.add_argument("--interval", type=int, default=60, help="Seconds between iterations")
    p_client.add_argument("--once", action="store_true", help="Run one iteration then exit")

    sub.add_parser("show-config", help="Print current config")

    args = p.parse_args(argv)
    if not getattr(args, "cmd", None):
        setattr(args, "cmd", "show-config")
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    if args.cmd == "setup":
        do_setup(args.storage_backend, args.bucket)
        return 0

    if args.cmd == "show-config":
        try:
            cfg = Config.load()
        except Exception as e:
            logger.error("%s", e)
            return 2
        print(json.dumps(asdict(cfg), indent=2))
        return 0

    try:
        cfg = Config.load()
    except Exception as e:
        logger.error("%s", e)
        logger.info("Run: python %s setup", Path(__file__).name)
        return 2

    if args.cmd == "server":
        try:
            start_server(cfg, host=getattr(args, "host", "127.0.0.1"), port=getattr(args, "port", 5000))
        except KeyboardInterrupt:
            logger.info("Server shutting down…")
        return 0

    if args.cmd == "client":
        try:
            start_client(cfg, interval=getattr(args, "interval", 60), once=getattr(args, "once", False))
        except KeyboardInterrupt:
            logger.info("Client interrupted, exiting…")
        return 0

    logger.error("Unknown command: %s", args.cmd)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.info("Interrupted")
        sys.exit(130)
