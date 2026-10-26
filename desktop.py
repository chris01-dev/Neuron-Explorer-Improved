"""Neuron Explorer as a desktop application: the same web UI inside a native window (WebView2 / Chromium).

Run `python desktop.py` (or the packaged NeuronExplorer.exe): it starts the server, loads the model,
opens the window, and stops everything the moment the window is closed. Without a WebView2 runtime
it falls back to opening the system browser instead.

    python desktop.py            # window; close it to stop
    python desktop.py --smoke 8 # closes itself after 8 seconds (for testing)
"""
import importlib.util
import os
import socket
import sys
import threading
import time

import uvicorn

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8791"))

if os.name == "nt":  # the Claude agent's subprocesses need the Proactor event loop on Windows
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

sys.path.insert(0, ROOT)
spec = importlib.util.spec_from_file_location("server", os.path.join(ROOT, "explorer", "server.py"))
server_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server_mod)

uvicorn_server = uvicorn.Server(uvicorn.Config(server_mod.app, host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=uvicorn_server.run, daemon=True).start()


def wait_port(timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def main():
    if not wait_port():
        print("[desktop] the server did not come up in time; check the logs printed above")
        time.sleep(10)
        os._exit(1)
    url = f"http://127.0.0.1:{PORT}/"
    smoke = None
    if "--smoke" in sys.argv:
        i = sys.argv.index("--smoke")
        smoke = float(sys.argv[i + 1]) if i + 1 < len(sys.argv) else 8.0
    try:
        import webview
        win = webview.create_window("Neuron Explorer", url, width=1500, height=950, min_size=(1000, 700))
        if smoke:
            threading.Timer(smoke, win.destroy).start()
        webview.start()
    except Exception as e:
        print(f"[desktop] native window unavailable ({e!r}); opening the browser instead")
        import webbrowser
        webbrowser.open(url)
        print("[desktop] close this console window to stop the server")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    os._exit(0)


if __name__ == "__main__":
    main()
