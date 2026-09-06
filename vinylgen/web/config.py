from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WebConfig:
    data_dir: Path
    inkscape_shared_dir: Path | None = None
    max_upload_bytes: int = 100 * 1024 * 1024
    max_active_jobs: int = 2
    retention_days: int = 90
    require_proxy_identity: bool = False

    @classmethod
    def from_env(cls) -> "WebConfig":
        root = Path(os.environ.get("VINYLGEN_DATA_DIR", ".vinylgen-data"))
        shared = os.environ.get("VINYLGEN_INKSCAPE_SHARED_DIR")
        return cls(
            root.expanduser().resolve(),
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
