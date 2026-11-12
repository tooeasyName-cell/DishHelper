"""Desktop entry point. Starts a loopback-only Streamlit server."""
import argparse
import json
import multiprocessing
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser


def available_port(requested):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", requested))
        return sock.getsockname()[1]


def self_test():
    # The demo adapter never initializes HTTPS. Check the frozen interpreter's
    # TLS resources too, without using credentials or making network requests.
    import ssl
    context = ssl.create_default_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert context.cert_store_stats()['x509_ca'] > 0
    from dishhelper.service import ChatService
    from dishhelper.storage import Repository
    from dishhelper.variants import APP_VERSION
    with tempfile.TemporaryDirectory(prefix="dishhelper-self-test-") as directory:
        repo = Repository(Path(directory) / "synthetic.sqlite3")
        repo.initialize()
        cid = repo.create_conversation("cooking")["id"]
        result = ChatService(repo).submit(cid, "测试演示", "self-test")
        assert result["status"] == "succeeded"
        assert len(repo.list_turns(cid)) == 1
        assert "search_proposal" not in repo.get_call("self-test")["request_snapshot"]["body"]["messages"][0]["content"]
        print(json.dumps({"ok": True, "version": APP_VERSION, "demo": "saved", "search": "disabled", "https_tls": "ready"}))


def open_when_ready(url):
    for _ in range(120):
        try:
            with urllib.request.urlopen(url + "/_stcore/health", timeout=1) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    return
        except OSError:
            time.sleep(0.5)
    print("页面未自动打开，请在浏览器输入上方本机地址。", flush=True)


def main():
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="DishHelper desktop launcher")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=8501)
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if not 0 <= args.port <= 65535:
        parser.error("Port must be between 0 and 65535")
    try:
        port = available_port(args.port)
    except OSError:
        port = available_port(0)
    from dishhelper.runtime_paths import data_directory
    data_directory().mkdir(parents=True, exist_ok=True)
    # Start an already-configured mobile bridge even with no browser page.
    # First-run databases/configuration are initialized by the app itself.
    import os
    from dishhelper.service import load_local_env
    from dishhelper.runtime_paths import local_env_path
    from dishhelper.storage import Repository
    from dishhelper.mobile_server import ensure_runtime
    load_local_env(local_env_path())
    database_path = Path(os.environ.get("DISHHELPER_DB", str(data_directory() / "dishhelper.sqlite3")))
    if database_path.is_file():
        mobile_repo = Repository(database_path)
        mobile_repo.initialize()
        mobile_state = ensure_runtime(mobile_repo)
        if mobile_state.get("error"):
            print("手机桥接未启动：" + mobile_state["error"], file=sys.stderr, flush=True)
    url = f"http://127.0.0.1:{port}"
    print(f"DishHelper\n本机地址：{url}\n数据目录：{data_directory()}\n关闭此窗口或按 Ctrl+C 停止程序。", flush=True)
    if not args.no_browser:
        threading.Thread(target=open_when_ready, args=(url,), daemon=True).start()
    from streamlit.web import cli
    app = Path(__file__).resolve().with_name("app.py")
    sys.argv = ["streamlit", "run", str(app), "--server.address", "127.0.0.1", "--server.port", str(port),
                "--server.headless", "true", "--server.fileWatcherType", "none",
                "--global.developmentMode", "false", "--browser.gatherUsageStats", "false"]
    cli.main()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"DishHelper 启动失败：{exc}", file=sys.stderr, flush=True)
        if sys.stdin and sys.stdin.isatty():
            input("按 Enter 关闭窗口。")
        raise SystemExit(1)
