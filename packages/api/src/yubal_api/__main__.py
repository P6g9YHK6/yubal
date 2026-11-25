"""Entry point for running yubal-api as a module: python -m yubal_api."""

import os
import sys

import uvicorn
from pydantic import ValidationError

from yubal_api.services import ytdlp_updater
from yubal_api.settings import get_settings

_ARGV = [sys.executable, "-m", "yubal_api"]


def main() -> None:
    """Start the FastAPI server."""
    try:
        settings = get_settings()
    except ValidationError as e:
        print(f"Configuration error: {e.errors()[0]['msg']}", file=sys.stderr)
        sys.exit(1)

    if settings.reload:
        uvicorn.run(
            "yubal_api.api.app:app",
            host=settings.host,
            port=settings.port,
            reload=True,
        )
        return

    # Start over with a newer downloaded yt-dlp in front of the bundled one
    if env := ytdlp_updater.overlay_env(settings.ytdlp_update_dir):
        os.execve(sys.executable, _ARGV, env)

    server = uvicorn.Server(
        uvicorn.Config("yubal_api.api.app:app", host=settings.host, port=settings.port)
    )
    restart = False

    def request_restart() -> None:
        nonlocal restart
        restart = True
        server.should_exit = True

    ytdlp_updater.set_restart_hook(request_restart)
    server.run()

    # Same process ID, so the container keeps running through it
    if restart:
        os.execv(sys.executable, _ARGV)


if __name__ == "__main__":
    main()
