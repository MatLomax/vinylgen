from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# The page the service serves: the symlink ``just deploy`` swaps to each published release
# (``scripts/publish_web.py``). A build writes ``web/dist``, which is never served.
LIVE_FRONTEND = Path(__file__).resolve().parents[2] / "web" / "live"


@dataclass(frozen=True)
class WebConfig:
    data_dir: Path
    inkscape_shared_dir: Path | None = None
    max_upload_bytes: int = 100 * 1024 * 1024
    max_active_jobs: int = 2
    retention_days: int = 90
    require_proxy_identity: bool = False
    frontend_dir: Path = LIVE_FRONTEND

    @classmethod
    def from_env(cls) -> "WebConfig":
        root = os.environ.get("VINYLGEN_DATA_DIR")
        if not root:
            raise RuntimeError(
                "VINYLGEN_DATA_DIR is not set. Refusing to guess a data directory: an "
                "unconfigured process (a dev server, migration, or REPL run from the repo "
                "root) must not silently open the deployed instance's database. Set it "
                "explicitly; the service reads it from vinylgen.env."
            )
        shared = os.environ.get("VINYLGEN_INKSCAPE_SHARED_DIR")
        return cls(
            Path(root).expanduser().resolve(),
            Path(shared).expanduser().resolve() if shared else None,
            int(os.environ.get("VINYLGEN_MAX_UPLOAD_BYTES", 100 * 1024 * 1024)),
            int(os.environ.get("VINYLGEN_MAX_ACTIVE_JOBS", 2)),
            int(os.environ.get("VINYLGEN_RETENTION_DAYS", 90)),
            os.environ.get("VINYLGEN_REQUIRE_PROXY_IDENTITY", "0").lower()
            in {"1", "true", "yes"},
        )

    @property
    def database_path(self) -> Path:
        return self.data_dir / "vinylgen.sqlite3"

    @property
    def artifact_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def files_dir(self) -> Path:
        """Human-navigable blob root: files/<project-slug>/<timestamp>-<stage>/<filename>."""
        return self.data_dir / "files"
