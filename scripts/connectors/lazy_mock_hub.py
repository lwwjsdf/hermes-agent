#!/usr/bin/env python3
"""lazy_mock_hub.py — 延迟监听的本地 mock Hub（测试专用）

用法: python3 lazy_mock_hub.py <port> <transport_mode> <device_id>
被 hub_transport.py 的 HUB_LOCAL_START_CMD 拉起，模拟"Hub 进程由命令启动、
过一会儿才在端口上监听"的真实时序——防止"立即就绪"假路径通过测试。
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


def main():
    port = int(sys.argv[1])
    mode = sys.argv[2]
    device_id = sys.argv[3]

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/health":
                self.send_error(404)
                return
            body = json.dumps({
                "status": "ok",
                "transport_mode": mode,
                "device_id": device_id,
                "device_name": "LazyMockHub",
                "version": "0.21.0-mock",
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format=None, *args):
            pass

    time.sleep(1.5)  # 先 sleep 再 bind：模拟进程启动期
    HTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    main()
