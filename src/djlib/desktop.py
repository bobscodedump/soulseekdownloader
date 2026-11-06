"""Mac app entry point: the same local server, shown in a native window.

Port 8765 is fixed because it is part of the Spotify redirect URI. If the app is already
running, a second launch just opens another window on it.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import urllib.request

PORT = 8765
URL = f"http://127.0.0.1:{PORT}"


def _running() -> bool:
    try:
        with urllib.request.urlopen(f"{URL}/api/summary", timeout=1) as r:
            return r.status == 200
    except OSError:
        return False


def _serve() -> None:
    import uvicorn

    from .api import create_app, default_config
    from .db import session_factory
    uvicorn.run(create_app(session_factory(), default_config(PORT)), host="127.0.0.1", port=PORT, log_level="warning")


def main() -> None:
    if not _running():
        threading.Thread(target=_serve, daemon=True).start()
        for _ in range(100):
            if _running():
                break
            time.sleep(0.1)
        else:
            sys.exit(f"DJ Library could not start its server on port {PORT} (is another program using it?)")
    if os.environ.get("DJLIB_HEADLESS"):  # used by the packaging smoke test
        while True:
            time.sleep(3600)
    import webview
    webview.create_window("DJ Library", URL, width=1400, height=900, min_size=(900, 600),
                          confirm_close=True)  # closing the window stops downloads in progress
    webview.start()


if __name__ == "__main__":
    main()
