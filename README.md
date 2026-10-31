# Task Recorder

**First principle:** Make a short browser task safe to inspect and replay.

Task Recorder records actions in its built-in demo workspace. You can review and edit each step before replay. It never controls your desktop or other apps. The demo uses made-up work items and stores its settings and steps in a private local state file.

## Run

**Native macOS app.** Build and install on macOS with Python 3.12:

```sh
python3.12 -m venv .desktop-venv
source .desktop-venv/bin/activate
python -m pip install -r requirements-desktop.lock.txt
python build_desktop.py
python install_desktop.py
```

The app is ad-hoc signed for local use; it is not Developer ID signed or notarized. Launch **Task Recorder** from Finder and quit with ⌘Q. To uninstall, quit it and remove `~/Applications/Task Recorder.app`. Saved state remains in `~/.macroforge/state.json`; remove that folder only if you want to erase it. Build output stays in `dist/`.

For development, run `python desktop.py` for the native shell. The native app starts its own loopback server on a temporary port.

## Use

Click the demo workspace to arm it. Start recording, then enter a made-up work code and add the item. A short summary is optional. Change it to Ready to record the second action. Stop recording, inspect the steps, and edit or remove any step. Click the workspace again before replay.

Task Recorder records safe text fields, clicks, scrolls, and waits. It skips email and password fields. Playback runs only in the built-in demo workspace. It stops if the page loses focus or the selected target changes. Use **Step** to run one action or **Replay** for the selected loop count. Settings can set the loop limit from one to three; it starts at two. Press Escape or Stop to halt playback.

## Test

Run `node --check app.js && node --check macro.js && node tests/test_macro.js && python3 -m py_compile server.py state.py && python3 -m unittest discover -s tests -v`.

## Data and limits

The backend owns saved state so it survives a new browser port. By default, the browser and native versions use the private `.macroforge` folder in your home directory. Before launching the native app, you can set `MACROFORGE_DATA_DIR` to a nonempty absolute folder path to choose another private state folder; it uses the same owner-only protections. The state file is limited to safe demo steps and synthetic rows. If saved data is corrupt, Task Recorder preserves it and shows an error instead of replacing it with defaults. Do not enter real personal or customer data. Task Recorder runs only in its built-in demo workspace. It does not automate your desktop or other apps.

The text check blocks some common credential patterns. It cannot find every secret, so keep credentials out of saved text.

## License

MIT. See [LICENSE](LICENSE).

**Optional browser version.** Requires Python 3.10 or later. Run `python3 server.py` from this folder, then open http://127.0.0.1:8342. Stop the server with Ctrl+C.

For development, run `python server.py` for the browser version. The browser version uses port 8342. This package is built for macOS; other operating systems are untested.
