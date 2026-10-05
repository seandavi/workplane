"""``workplane-server``: run the API, dashboard and background sync."""

from __future__ import annotations

import logging
import os

import uvicorn


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s: %(message)s")
    uvicorn.run(
        "workplane.api:create_app",
        factory=True,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8642")),
        workers=1,  # the background sync loop assumes a single process
    )


if __name__ == "__main__":
    main()
