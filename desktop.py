#!/usr/bin/env python3
"""Native macOS shell for Task Recorder."""
from __future__ import annotations

import os
import threading
from pathlib import Path

import server
import webview
from desktop_policy import install_policy


def _show_startup_error() -> None:
    try:
        import AppKit
    except Exception:
        return
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_("Task Recorder could not start")
    alert.setInformativeText_("The local app could not open. Restart Task Recorder and try again.")
    alert.addButtonWithTitle_("OK")
    alert.runModal()


def _configured_state_path() -> Path | None:
    if "MACROFORGE_DATA_DIR" not in os.environ:
        return None
    raw_directory = os.environ["MACROFORGE_DATA_DIR"]
    if not raw_directory or not raw_directory.strip():
        raise ValueError("MACROFORGE_DATA_DIR must be a nonempty absolute directory path.")
    directory = Path(raw_directory)
    if not directory.is_absolute():
        raise ValueError("MACROFORGE_DATA_DIR must be a nonempty absolute directory path.")
    return directory / "state.json"


def run() -> None:
    local_server = None
    server_thread = None
    try:
        local_server = server.make_server(port=0, state_path=_configured_state_path())
        server_thread = threading.Thread(
            target=local_server.serve_forever,
            name="macroforge-http",
            daemon=True,
        )
        server_thread.start()
        url = f"http://127.0.0.1:{local_server.server_port}/?desktop=1"
        webview.create_window(
            "Task Recorder",
            url,
            width=1080,
            height=760,
            min_size=(820, 600),
        )
        install_policy(url)
        webview.start()
    except Exception:
        _show_startup_error()
        raise SystemExit(1)
    finally:
        if local_server is not None:
            if server_thread is not None and server_thread.is_alive():
                local_server.shutdown()
            local_server.server_close()
            if server_thread is not None:
                server_thread.join(timeout=3)


if __name__ == "__main__":
    run()
