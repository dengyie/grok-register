"""python -m apps.control_api"""

from __future__ import annotations

import uvicorn

from apps.control_api.settings import get_settings


def main() -> None:
    s = get_settings()
    uvicorn.run(
        "apps.control_api.app:create_app",
        factory=True,
        host=s.host,
        port=s.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
