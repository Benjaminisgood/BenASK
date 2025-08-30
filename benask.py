#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import signal
import subprocess
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# ---------------- 配置 ----------------
PROJECTS = [
    ("Benata", "./Benata", 5001),
    ("Bendea", "./Bendea", 5002),
]
ADMIN_BIND = ("127.0.0.1", 5500)  # 管理入口移到 5500，避开 5000/AirPlay

procs = {}          # name -> Popen
lock = threading.Lock()


def run_subproject(path, port):
    abs_path = os.path.abspath(path)
    app_path = os.path.join(abs_path, 'app.py')
    venv_python = os.path.join(abs_path, 'venv', 'bin', 'python')

    print("🚀 正在启动：", abs_path)
    print("🔍 Python 路径：", venv_python)
    if not os.path.exists(venv_python):
        raise FileNotFoundError(f"❌ 找不到虚拟环境解释器: {venv_python}")
    if not os.path.exists(app_path):
        raise FileNotFoundError(f"❌ 找不到 app.py: {app_path}")

    # 把端口通过常见变量名传给子项目
    env = os.environ.copy()
    env["PORT"] = str(port)
    env["FLASK_RUN_PORT"] = str(port)
    env["BENSCI_PORT"] = str(port)

    return subprocess.Popen([venv_python, app_path], cwd=abs_path, env=env)


def monitor_process(proc, name):
    code = proc.wait()
    print(f"⚠️ 子进程 {name} 已退出，exit code={code}")
    with lock:
        procs[name] = None


# ---------------- Web 管理入口 ----------------
INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BenSCI 管理面板</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, PingFang SC, sans-serif; margin: 24px; }
  .wrap { max-width: 920px; margin: 0 auto; }
  h1 { font-size: 22px; margin: 0 0 12px; }
  .bar { display:flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom:16px; }
  .btn { padding: 10px 14px; border-radius: 10px; border: 1px solid #8884; cursor: pointer; }
  .btn:hover { filter: brightness(1.05); }
  .btn-primary { background: #4f46e5; color: white; border-color: #4f46e5; }
  .btn-danger  { background: #dc2626; color: white; border-color: #dc2626; }
  .btn-ghost  { background: transparent; }
  table { width: 100%; border-collapse: collapse; }
  th, td { padding: 10px 8px; border-bottom: 1px solid #8884; text-align: left; }
  .tag { display:inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; border: 1px solid #8884; }
  .ok { background:#16a34a; color:white; border-color:#16a34a; }
  .down { background:#f59e0b; color:white; border-color:#f59e0b; }
  footer { opacity: 0.7; font-size: 12px; margin-top: 16px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="bar">
    <h1>BenSCI 管理面板</h1>
    <div style="display:flex; gap:8px;">
      <button class="btn btn-ghost" onclick="refresh()">刷新状态</button>
      <button id="shutdownBtn" class="btn btn-danger" onclick="shutdownAll()">关闭全部</button>
    </div>
  </div>

  <table>
    <thead>
      <tr>
        <th style="width:28%;">项目</th>
        <th style="width:18%;">状态</th>
        <th style="width:14%;">PID</th>
        <th style="width:14%;">端口</th>
        <th>操作</th>
      </tr>
    </thead>
    <tbody id="tbody">
      <tr><td colspan="5">加载中…</td></tr>
    </tbody>
  </table>

  <footer id="hint">GET <code>/status</code> 查看状态，POST <code>/shutdown</code> 关闭全部。</footer>
</div>

<script>
async function getStatus() {
  const res = await fetch('/status');
  if (!res.ok) throw new Error('status fetch failed');
  return res.json();
}

function rowHtml(name, s) {
  const running = s.running;
  const url = s.url || '';
  const pid = s.pid || '-';
  const port = s.port || '-';
  const badge = running ? '<span class="tag ok">Running</span>' : '<span class="tag down">Stopped</span>';
  const openBtn = running ? `<a class="btn btn-primary" target="_blank" href="${url}">打开</a>` : '<button class="btn" disabled>打开</button>';
  return `<tr>
    <td>${name}</td>
    <td>${badge}</td>
    <td>${pid}</td>
    <td>${port}</td>
    <td style="display:flex; gap:8px; align-items:center;">${openBtn}</td>
  </tr>`;
}

async function refresh() {
  try {
    const data = await getStatus();
    const tbody = document.getElementById('tbody');
    const status = data.status || {};
    let html = '';
    for (const k of Object.keys(status)) {
      html += rowHtml(k, status[k]);
    }
    if (!html) html = '<tr><td colspan="5">无项目</td></tr>';
    tbody.innerHTML = html;
  } catch (e) {
    document.getElementById('tbody').innerHTML = '<tr><td colspan="5">状态拉取失败</td></tr>';
  }
}

async function shutdownAll() {
  const btn = document.getElementById('shutdownBtn');
  btn.disabled = true;
  btn.textContent = '正在关闭…';
  try {
    const res = await fetch('/shutdown', { method: 'POST' });
    if (!res.ok) throw new Error('shutdown failed');
    const j = await res.json();
    document.getElementById('hint').textContent = j.message || 'shutting down';
    // 给后端一点时间优雅退出（页面不会自动关闭）
    setTimeout(() => { btn.textContent = '已发送关闭命令'; }, 300);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = '关闭全部';
    alert('关闭失败：' + e.message);
  }
}

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""

class RequestHandler(BaseHTTPRequestHandler):
    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code, html):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            return self._html(200, INDEX_HTML)

        if self.path == "/status":
            with lock:
                status = {}
                for name, path, port in PROJECTS:
                    p = procs.get(name)
                    running = (p is not None) and (p.poll() is None)
                    status[name] = {
                        "running": running,
                        "pid": (p.pid if running else None),
                        "port": port,
                        "url": f"http://127.0.0.1:{port}" if running else None,
                    }
            return self._json(200, {"ok": True, "status": status})

        return self._json(404, {"ok": False, "msg": "not found"})

    def do_POST(self):
        if self.path == "/shutdown":
            # 先回响应，再触发关闭
            self._json(200, {"ok": True, "message": "shutting down"})
            os.kill(os.getpid(), signal.SIGINT)
            return
        return self._json(404, {"ok": False})

    def log_message(self, fmt, *args):
        sys.stdout.write("🌐 " + (fmt % args) + "\n")


def start_admin_server():
    server = HTTPServer(ADMIN_BIND, RequestHandler)
    print(f"🌐 管理入口：http://{ADMIN_BIND[0]}:{ADMIN_BIND[1]}")
    try:
        server.serve_forever()
    finally:
        server.server_close()


def terminate_all():
    print("\n⛔️ 停止中，终止所有子进程...")
    with lock:
        for name, p in procs.items():
            if p and (p.poll() is None):
                print(f"   - 终止 {name} (pid={p.pid})")
                p.terminate()
        for name, p in procs.items():
            if p and (p.poll() is None):
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    print(f"   - 强制杀死 {name} (pid={p.pid})")
                    p.kill()
    print("✅ 已全部停止")


def handle_sigint(signum, frame):
    terminate_all()
    sys.exit(0)


if __name__ == '__main__':
    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    # 启动子项目
    for name, path, port in PROJECTS:
        p = run_subproject(path, port)
        with lock:
            procs[name] = p
        threading.Thread(target=monitor_process, args=(p, name), daemon=True).start()

    print("✅ 两个项目已启动")
    # 启动管理 HTTP 服务
    start_admin_server()