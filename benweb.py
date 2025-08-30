import os
import mimetypes
import urllib.parse
import importlib
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import json
import io
import uuid
from urllib.parse import parse_qs
from email.parser import BytesParser
from email.policy import default as email_default_policy
# --- extra imports for doc discovery ---
import re
import sys
import ast
import html
from typing import Any


# 全局：限制并发数（例如最多 32 个同时处理）
MAX_CONCURRENCY = 32
_sema = threading.BoundedSemaphore(MAX_CONCURRENCY)

# ------------------------
# Simple Request/Response
# ------------------------
class Request:
    """Unified view of the incoming HTTP request."""
    def __init__(self, handler: BaseHTTPRequestHandler):
        self.method = handler.command
        self.raw_path = handler.path
        parsed = urllib.parse.urlparse(handler.path)
        self.path = parsed.path
        self.query = {k: v if len(v) > 1 else v[0] for k, v in parse_qs(parsed.query).items()}
        self.headers = handler.headers
        self.content_type = (handler.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        self.content_length = int(handler.headers.get('Content-Length') or 0)
        self._handler = handler
        self.body_bytes = b''
        self.text = None
        self.json = None
        self.form = {}
        self.files = {}  # {field: {filename, content_type, bytes}}

    def read_body(self):
        if self.content_length > 0:
            self.body_bytes = self._handler.rfile.read(self.content_length)
        # Parse by content type
        if self.content_type == 'application/json':
            try:
                self.text = self.body_bytes.decode('utf-8', errors='strict')
                self.json = json.loads(self.text) if self.text else None
            except Exception:
                # leave json=None, but keep text
                self.text = self.body_bytes.decode('utf-8', errors='ignore')
        elif self.content_type == 'application/x-www-form-urlencoded':
            self.text = self.body_bytes.decode('utf-8', errors='ignore')
            self.form = {k: v if len(v) > 1 else v[0] for k, v in parse_qs(self.text).items()}
        elif self.content_type == 'multipart/form-data':
            # Parse multipart/form-data using the stdlib email package (RFC 7578 compatible)
            # Build a synthetic MIME message with Content-Type header so the parser can understand the boundary
            ct_header = self._handler.headers.get('Content-Type') or 'multipart/form-data'
            # Note: the email parser expects CRLF line endings
            raw = (f"Content-Type: {ct_header}\r\nMIME-Version: 1.0\r\n\r\n").encode('utf-8') + self.body_bytes
            try:
                msg = BytesParser(policy=email_default_policy).parsebytes(raw)
            except Exception:
                msg = None

            if msg and msg.is_multipart():
                for part in msg.iter_parts():
                    cd = part.get('Content-Disposition', '') or ''
                    # Extract the field name from Content-Disposition; fallback to None
                    # Examples: form-data; name="field"; filename="a.txt"
                    params = {}
                    for item in cd.split(';'):
                        item = item.strip()
                        if '=' in item:
                            k, v = item.split('=', 1)
                            v = v.strip().strip('"')
                            params[k.strip().lower()] = v
                    field_name = params.get('name')
                    filename = part.get_filename()
                    ctype = part.get_content_type() or 'application/octet-stream'
                    payload = part.get_payload(decode=True) or b''

                    if filename:
                        # File field
                        if field_name:
                            self.files[field_name] = {
                                'filename': filename,
                                'content_type': ctype,
                                'bytes': payload,
                            }
                    else:
                        # Regular form field (treat as UTF-8 text when possible)
                        if field_name:
                            try:
                                value = payload.decode(part.get_content_charset() or 'utf-8', errors='strict')
                            except Exception:
                                value = payload.decode('utf-8', errors='ignore')
                            # If multiple fields of same name appear, promote to list
                            if field_name in self.form:
                                prev = self.form[field_name]
                                if isinstance(prev, list):
                                    prev.append(value)
                                else:
                                    self.form[field_name] = [prev, value]
                            else:
                                self.form[field_name] = value
            else:
                # Fallback: treat as binary if parsing failed
                # (You may choose to raise an error instead.)
                pass
        else:
            # Treat as plain text or binary
            try:
                self.text = self.body_bytes.decode('utf-8', errors='strict')
            except Exception:
                self.text = None

class Response:
    """Helper to send consistent responses."""
    def __init__(self, handler: BaseHTTPRequestHandler):
        self.h = handler

    def _send(self, status: int, body: bytes, content_type: str, extra_headers: dict | None = None):
        self.h.send_response(status)
        self.h.send_header('Content-Type', content_type)
        self.h.send_header('Content-Length', str(len(body)))
        # Basic CORS (can be tightened per your needs)
        self.h.send_header('Access-Control-Allow-Origin', '*')
        self.h.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.h.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        if extra_headers:
            for k, v in extra_headers.items():
                self.h.send_header(k, v)
        self.h.end_headers()
        self.h.wfile.write(body)

    def json(self, obj: dict | list, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self._send(status, body, 'application/json; charset=utf-8')

    def text(self, s: str, status: int = 200, content_type: str = 'text/plain; charset=utf-8'):
        self._send(status, s.encode('utf-8'), content_type)

    def bytes(self, b: bytes, status: int = 200, content_type: str = 'application/octet-stream'):
        self._send(status, b, content_type)

# 统一的错误响应工具
def send_error_json(handler: BaseHTTPRequestHandler, status: int, message: str, detail: str | None = None):
    rid = str(uuid.uuid4())
    payload = {'ok': False, 'error': {'code': status, 'message': message, 'detail': detail, 'request_id': rid}}
    resp = Response(handler)
    resp.json(payload, status=status)

# -----------------------
# Minimal OpenAPI-like Doc
# -----------------------
IGNORED_DIRS = {".git", "__pycache__", ".venv", "venv", "env", "node_modules"}

def _path_from_file(pyfile: str) -> str:
    """Translate a python file path to an API base path.
    e.g. cwd/foo/bar.py -> /foo/bar
    """
    cwd = os.getcwd()
    rel = os.path.relpath(pyfile, cwd)
    rel = rel.replace(os.sep, "/")
    if rel.endswith(".py"):
        rel = rel[:-3]
    if rel == os.path.basename(__file__)[:-3]:
        # skip ben.py itself
        return ""
    if rel == "ben":
        return ""
    return "/" + rel.lstrip("./")

def _parse_docstring(ds: str | None) -> dict:
    """Parse a simple Google/NumPy-style docstring. Extract first line summary, the rest description,
    and lines like `Args:` / `Parameters:` as params. This is intentionally simple.
    """
    if not ds:
        return {"summary": "", "description": "", "params": []}
    lines = [ln.rstrip() for ln in ds.strip().splitlines()]
    summary = lines[0].strip()
    body = "\n".join(lines[1:]).strip()
    params: list[dict[str, str]] = []
    # crude param extraction: match patterns like "name (type): desc" or "name: desc"
    param_block = False
    for ln in lines:
        low = ln.strip().lower()
        if low.startswith("args:") or low.startswith("parameters:"):
            param_block = True
            continue
        if param_block and ln.strip() and not ln.startswith(" "):
            # leaving the block when a non-indented line appears
            param_block = False
        if param_block:
            m = re.match(r"\s*([a-zA-Z_][\w]*)\s*(?:\(([^)]*)\))?\s*:\s*(.*)", ln)
            if m:
                name, typ, desc = m.group(1), m.group(2) or "", m.group(3)
                params.append({"name": name, "type": typ, "description": desc})
    return {"summary": summary, "description": body, "params": params}

def _ast_doc_of_func(node: ast.AST) -> str | None:
    try:
        return ast.get_docstring(node)
    except Exception:
        return None

def _discover_api_entries() -> list[dict]:
    """AST-scan working directory to find potential API endpoints.
    Rules mirror the POST resolver:
      - If a module foo/bar.py defines a callable `handle_request`, it maps to POST /foo/bar
      - Any other top-level def `xxx` in foo/bar.py maps to POST /foo/xxx
    We avoid importing user modules to keep it safe.
    """
    found: list[dict] = []
    for root, dirs, files in os.walk(os.getcwd()):
        # prune ignored dirs
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith('.')]
        for f in files:
            if not f.endswith(".py"):
                continue
            full = os.path.join(root, f)
            # skip this framework file itself
            if os.path.samefile(full, __file__):
                continue
            api_base = _path_from_file(full)
            if not api_base:
                continue
            try:
                with open(full, "r", encoding="utf-8", errors="ignore") as fp:
                    src = fp.read()
                mod = ast.parse(src, filename=full)
            except Exception:
                continue

            mod_doc = ast.get_docstring(mod)
            mod_meta = _parse_docstring(mod_doc)

            has_handle = False
            for node in mod.body:
                if isinstance(node, ast.FunctionDef):
                    name = node.name
                    if name == "handle_request":
                        has_handle = True
                        ds = _ast_doc_of_func(node)
                        meta = _parse_docstring(ds)
                        found.append({
                            "path": api_base,
                            "method": "POST",
                            "summary": meta["summary"] or mod_meta["summary"],
                            "description": meta["description"] or mod_meta["description"],
                            "params": meta["params"],
                            "source": full,
                            "kind": "module-handler"
                        })
                    else:
                        # expose other top-level functions as /<parent>/<func>
                        ds = _ast_doc_of_func(node)
                        meta = _parse_docstring(ds)
                        parent = api_base.rsplit("/", 1)[0] if "/" in api_base else ""
                        func_path = (parent + "/" + name) if parent else ("/" + name)
                        found.append({
                            "path": func_path,
                            "method": "POST",
                            "summary": meta["summary"],
                            "description": meta["description"],
                            "params": meta["params"],
                            "source": full,
                            "kind": "function"
                        })
            # If module has no functions, but may still be imported dynamically: skip
    # Merge duplicates keeping first occurrence
    seen = set()
    uniq = []
    for it in found:
        key = (it["path"], it["method"])
        if key not in seen:
            seen.add(key)
            uniq.append(it)
    return sorted(uniq, key=lambda d: (d["path"], d["method"]))

def build_api_spec(base_url: str | None = None) -> dict[str, Any]:
    """Build a simple OpenAPI-like JSON structure using discovered entries."""
    entries = _discover_api_entries()
    paths: dict[str, dict] = {}
    default_cts = [
        "application/json",
        "text/plain",
        "application/x-www-form-urlencoded",
        "multipart/form-data",
    ]
    for e in entries:
        p = e["path"]
        method = e["method"].lower()
        paths.setdefault(p, {})[method] = {
            "summary": e.get("summary", ""),
            "description": e.get("description", ""),
            "requestBody": {
                "content": {ct: {"schema": {"type": "object"}} for ct in default_cts}
            },
            "responses": {
                "200": {"description": "OK"},
                "500": {"description": "Server Error"}
            },
            "x-source": e.get("source", ""),
            "x-kind": e.get("kind", "")
        }
    return {
        "openapi": "3.0.0-min",
        "info": {
            "title": "Ben Simple Web API",
            "version": "0.1",
            "description": "Auto-discovered documentation (AST-based, stdlib-only)."
        },
        "servers": ([{"url": base_url}] if base_url else []),
        "paths": paths
    }

_DOCS_HTML = """<!DOCTYPE html>
<html lang=\"zh-CN\">
<head>
<meta charset=\"utf-8\" />
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
<title>API 文档 · Ben Simple Web API</title>
<style>
body { font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica, Arial, 'Noto Sans', 'PingFang SC', 'Microsoft YaHei', sans-serif; margin: 0; }
header { background:#111; color:#fff; padding:12px 16px; }
main { padding: 16px; }
code, pre { background:#f5f5f5; padding:2px 4px; border-radius:4px; }
.path { font-weight:600; }
.card { border:1px solid #eee; border-radius:8px; padding:12px; margin:12px 0; }
.method { display:inline-block; padding:2px 6px; border-radius:4px; font-weight:700; }
.method.POST { background:#e8f0fe; }
.small { color:#666; font-size:12px; }
.param { margin-left: 1em; }
footer { color:#888; font-size:12px; text-align:center; padding:16px; }
</style>
</head>
<body>
<header>
  <h1>Ben Simple Web API · 文档</h1>
</header>
<main>
  <p>此页面为 <em>stdlib-only</em> 自动生成文档，规则：含 <code>handle_request</code> 的模块映射到 <code>POST /path/to/module</code>；模块内的其他顶层函数映射到 <code>POST /path/to/function</code>。</p>
  <p>完整 JSON 见 <a href=\"/__api/spec.json\">/__api/spec.json</a></p>
  <div id=\"paths\"></div>
</main>
<footer>生成时间：<span id=\"ts\"></span></footer>
<script>
(async function(){
  try{
    const res = await fetch('/__api/spec.json');
    const spec = await res.json();
    document.getElementById('ts').textContent = new Date().toLocaleString();
    const wrap = document.getElementById('paths');
    const entries = Object.entries(spec.paths || {});
    if(entries.length === 0){
      wrap.innerHTML = '<p class="small">没有发现可用的 API 端点。请在你的模块中添加 <code>handle_request</code> 或顶层函数。</p>';
      return;
    }
    for(const [p, methods] of entries){
      for(const [m, info] of Object.entries(methods)){
        const card = document.createElement('div');
        card.className = 'card';
        const h = document.createElement('div');
        h.innerHTML = `<span class="method ${m.toUpperCase()}">${m.toUpperCase()}</span> <span class="path">${p}</span>`;
        card.appendChild(h);
        if(info.summary){
          const s = document.createElement('div');
          s.textContent = info.summary;
          card.appendChild(s);
        }
        if(info.description){
          const d = document.createElement('div');
          d.className = 'small';
          d.textContent = info.description;
          card.appendChild(d);
        }
        const src = info['x-source'] || '';
        if(src){
          const sm = document.createElement('div');
          sm.className = 'small';
          sm.textContent = '源文件: ' + src;
          card.appendChild(sm);
        }
        const params = info.params || [];
        if(params.length){
          const ph = document.createElement('div');
          ph.innerHTML = '<div class="small">参数:</div>';
          card.appendChild(ph);
          for(const p of params){
            const div = document.createElement('div');
            div.className = 'param small';
            div.textContent = `- ${p.name}${p.type?(' ('+p.type+')'):''}: ${p.description||''}`;
            card.appendChild(div);
          }
        }
        const tryit = document.createElement('details');
        tryit.innerHTML = `<summary>在线尝试 (fetch)</summary>
          <pre>fetch('${p}', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({example:true})}).then(r=>r.json()).then(console.log)</pre>`;
        card.appendChild(tryit);
        wrap.appendChild(card);
      }
    }
  }catch(e){
    document.getElementById('paths').innerHTML = '<p class="small">加载失败：'+e+'</p>';
  }
})();
</script>
</body>
</html>"""

# 定义根路径 "/" 返回的HTML内容，直接写在Python文件中
ROOT_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>主页</title>
</head>
<body>
    <h1>欢迎使用Python简易Web_API框架</h1>
    <p>您可以点击以下链接跳转到其他页面:</p>
    <ul>
        <li><a href="/transdoc">transdoc</a></li>
        <li><a href="/pdftranslater">pdf translater</a></li>
    </ul>
</body>
</html>"""
# 上面定义了一个简单的HTML主页，用户可以根据需要修改此内容。

class SimpleWebFrameworkHandler(BaseHTTPRequestHandler):
    """基于BaseHTTPRequestHandler的请求处理器，用于实现简单的Web框架功能"""
    
    # 每个请求进入时先获取并发令牌
    def handle_one_request(self):
        with _sema:
            super().handle_one_request()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.end_headers()

    def do_GET(self):
        """处理GET请求：返回静态文件或主页"""
        # 解析请求的URL，分离路径和查询参数等
        parsed_url = urllib.parse.urlparse(self.path)
        request_path = parsed_url.path  # 请求的路径部分（不含查询字符串）
        # Built-in documentation endpoints (stdlib-only)
        if request_path in ("/__api/spec", "/__api/spec.json"):
            # best-effort base URL detection (no reverse-proxy awareness)
            host_hdr = self.headers.get('Host') or f"127.0.0.1:{port}"
            base_url = f"http://{host_hdr}"
            spec = build_api_spec(base_url=base_url)
            payload = json.dumps(spec, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if request_path in ("/__api/docs", "/__api" ):
            html_bytes = _DOCS_HTML.encode('utf-8')
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html_bytes)))
            self.end_headers()
            self.wfile.write(html_bytes)
            return
        # 如果请求根路径 "/", 返回预定义的ROOT_PAGE内容
        if request_path == "/" or request_path == "":
            content = ROOT_PAGE.encode('utf-8')  # 将ROOT_PAGE字符串编码为UTF-8字节
            # 发送200响应和HTML内容
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        # 对于其他路径，尝试从本地目录加载对应的静态资源文件
        # 为了安全，防止目录遍历攻击，不允许路径中出现上级目录引用
        safe_path = request_path.lstrip("/")  # 去掉路径前导斜杠，得到本地相对路径
        if ".." in safe_path:
            # 如果路径中含有不安全的上级目录引用，返回400错误
            self.send_response(400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Bad Request".encode('utf-8'))
            return
        # 确定请求的静态文件在本地文件系统中的路径
        local_path = os.path.join(os.getcwd(), safe_path)
        # 如果路径对应一个目录，则尝试返回该目录下的index.html文件
        if os.path.isdir(local_path):
            # 如果请求路径未以"/"结尾，我们可以在此处处理（例如添加斜杠），这里为了简单不做重定向，仅处理文件
            if not request_path.endswith("/"):
                # 确保路径以斜杠结尾，以便正确查找index.html
                request_path += "/"
                safe_path += "/"
                local_path = os.path.join(os.getcwd(), safe_path)
            index_file = os.path.join(local_path, "index.html")
            if os.path.isfile(index_file):
                # 如果目录下存在index.html，则将其作为要返回的文件
                local_path = index_file
            else:
                # 如果目录下没有index.html，返回404 Not Found
                send_error_json(self, 404, "File Not Found")
                return
        # 此时，local_path应当指向一个文件。如果文件不存在或路径非文件，返回404
        if not os.path.exists(local_path) or not os.path.isfile(local_path):
            send_error_json(self, 404, "File Not Found")
            return
        # 基于文件扩展名推断Content-Type（MIME类型）
        ctype = mimetypes.guess_type(local_path)[0] or "application/octet-stream"
        try:
            # 以二进制模式打开文件并读取内容
            with open(local_path, "rb") as f:
                content = f.read()
            # 发送200响应和推断出的内容类型、内容长度
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            # 将文件内容写入响应
            self.wfile.write(content)
        except Exception as e:
            # 如果读取文件或发送过程中出现异常，返回500服务器错误
            send_error_json(self, 500, "Server Error", str(e))
    
    def do_POST(self):
        """处理POST请求：调用对应模块中的函数并返回结果"""
        # 解析URL，获取路径部分（忽略查询参数，在此框架中POST主要处理请求体）
        parsed_url = urllib.parse.urlparse(self.path)
        request_path = parsed_url.path
        safe_path = request_path.lstrip("/")  # 去除开头的/，得到相对路径

        # Unified request/response
        req = Request(self)
        MAX_BODY = 64 * 1024 * 1024  # 64MB
        if int(self.headers.get('Content-Length') or 0) > MAX_BODY:
            send_error_json(self, 413, 'Payload Too Large', 'Body exceeds 64MB limit')
            return
        req.read_body()
        res = Response(self)

        if safe_path == "" or safe_path is None:
            # 如果没有指定路径（POST到根路径），则返回404，因为没有可以处理的目标
            send_error_json(self, 404, "Not Found")
            return
        # 防止目录遍历攻击
        if ".." in safe_path:
            self.send_response(400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Bad Request".encode('utf-8'))
            return
        # 根据请求路径，确定要调用的模块和函数
        # 框架约定：如果路径正好对应一个本地存在的.py模块文件，则优先加载该模块
        module = None
        result = None
        # 将URL路径中的/替换为.，用于模块导入名称
        module_name = safe_path.replace("/", ".")
        module_file_path = os.path.join(os.getcwd(), safe_path)
        if os.path.isfile(module_file_path + ".py"):
            # 情况1：请求路径对应一个.py文件（例如路径/api对应api.py，或路径/foo/bar对应foo/bar.py）
            try:
                module = importlib.import_module(module_name)
            except ImportError as ie:
                # 模块导入失败（可能文件不存在或模块包结构问题），记录错误并返回500
                traceback.print_exc()
                send_error_json(self, 500, "Module Import Error", str(ie))
                return
            except Exception as e:
                # 导入模块过程中出现其它异常（模块代码执行错误等），返回500
                traceback.print_exc()
                send_error_json(self, 500, "Module Import Error", str(e))
                return
            # DEV: auto-reload module on each POST
            try:
                maybe_reload(module)
            except Exception:
                pass
            # 模块成功导入后，检查模块中是否定义了处理函数handle_request
            if hasattr(module, "handle_request") and callable(getattr(module, "handle_request")):
                try:
                    # 调用模块中的handle_request函数，将POST请求数据作为参数
                    try:
                        result = module.handle_request(req)
                    except TypeError:
                        # Backward-compat: old handlers expecting a string
                        result = module.handle_request(req.text or '')
                except Exception as e:
                    # 调用模块函数时发生异常，打印堆栈并返回500错误
                    traceback.print_exc()
                    send_error_json(self, 500, "Error in handler", str(e))
                    return
            else:
                # 模块中未定义handle_request函数，视为配置错误，返回500
                self.send_error(500, "No handle_request function in module")
                return
        else:
            # 情况2：请求路径不直接对应单一模块文件，按“模块/函数”解析
            segments = [seg for seg in safe_path.split("/") if seg]  # 分割路径为各部分
            if len(segments) < 2:
                # 如果路径不足两段（无法确定模块和函数），返回404
                send_error_json(self, 404, "Not Found")
                return
            # 将最后一段视为函数名，前面的部分作为模块路径
            module_part = segments[:-1]  # 模块路径（列表形式）
            func_name = segments[-1]     # 函数名称为路径最后一部分
            module_path = ".".join(module_part)  # 模块导入路径字符串
            try:
                module = importlib.import_module(module_path)
            except ImportError as ie:
                # 模块导入失败，返回404（找不到对应的模块）
                send_error_json(self, 404, "Not Found")
                return
            except Exception as e:
                # 模块导入时发生其它异常，返回500
                traceback.print_exc()
                send_error_json(self, 500, "Module Import Error", str(e))
                return
            # DEV: auto-reload module on each POST
            try:
                maybe_reload(module)
            except Exception:
                pass
            # 模块导入成功后，检查指定函数是否存在于模块中
            if hasattr(module, func_name) and callable(getattr(module, func_name)):
                try:
                    # 调用模块内对应函数，将POST数据传入
                    try:
                        result = getattr(module, func_name)(req)
                    except TypeError:
                        result = getattr(module, func_name)(req.text or '')
                except Exception as e:
                    # 函数执行过程中出现异常
                    traceback.print_exc()
                    send_error_json(self, 500, "Error in handler function", str(e))
                    return
            else:
                # 模块中不存在指定函数，返回404
                send_error_json(self, 404, "Not Found")
                return
        # Normalize result to a consistent response
        if isinstance(result, Response):
            # Handler may have already responded
            return
        elif isinstance(result, dict):
            # If handler returns a business payload with its own ok/error, pass through unchanged
            if ('ok' in result) or ('error' in result and isinstance(result['error'], dict)):
                return res.json(result)
            # otherwise wrap as data
            return res.json({'ok': True, 'data': result})
        elif isinstance(result, list):
            return res.json({'ok': True, 'data': result})
        elif isinstance(result, bytes):
            return res.bytes(result)
        elif isinstance(result, str):
            # If looks like JSON, return parsed; if it has business ok/error, pass through
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict) and (('ok' in parsed) or ('error' in parsed)):
                    return res.json(parsed)
                return res.json({'ok': True, 'data': parsed})
            except Exception:
                return res.text(result)
            
        elif result is None:
            return res.json({'ok': True, 'data': None})
        else:
            # Fallback string representation
            return res.json({'ok': True, 'data': str(result)})

DEV_AUTO_RELOAD = True  # reload user modules on each POST during development

def maybe_reload(module):
    try:
        if DEV_AUTO_RELOAD and module is not None:
            import importlib
            importlib.reload(module)
    except Exception:
        pass

if __name__ == "__main__":
    # 指定服务器监听地址和端口
    host = ""
    port = 8000
    server_address = (host, port)

    # httpd = HTTPServer(server_address, SimpleWebFrameworkHandler)
    httpd = ThreadingHTTPServer(server_address, SimpleWebFrameworkHandler)
    httpd.daemon_threads = True
    httpd.allow_reuse_address = True

    try:
        print(f"Serving on port {port}...")
        print(f"🌐 管理入口：http://127.0.0.1:{port}")
        print(f"📘 文档入口：http://127.0.0.1:{port}/__api/docs  (JSON: /__api/spec.json)")
        httpd.serve_forever()  # 启动服务器，进入循环监听HTTP请求
    except KeyboardInterrupt:
        # 捕捉Ctrl+C中断，关闭服务器
        print("Server is shutting down.")
        httpd.server_close()
