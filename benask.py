from http.server import BaseHTTPRequestHandler, HTTPServer
import re, os

# 控制是否输出调试信息
DEBUG = True

# 所有动态路由规则注册在此
ROUTES = []

# 支持访问这些静态文件路径（前缀）
static_dirs = ['photos', 'video', 'audios']

# 装饰器：注册路由规则
def route(path):
    def decorator(func):
        # 将 <name> 替换为正则 (?P<name>[^/]+)
        pattern = re.sub(r"<(\w+)>", r"(?P<\1>[^/]+)", path)
        pattern = f"^{pattern}/?$"  # 支持末尾可选 /
        ROUTES.append((re.compile(pattern), func))
        if DEBUG:
            print(f"[DEBUG] 注册路由：{path} -> {func.__name__}")
        return func
    return decorator

# HTML 模板目录（htmls 文件夹）
TEMPLATE_DIR = "htmls"

# 模板渲染函数，支持变量替换
def showhtml(filename, **kwargs):
    filepath = os.path.join(TEMPLATE_DIR, filename)
    try:
        with open(filepath, encoding="utf-8") as f:
            content = f.read()
        # 替换 {{ key }} 为对应的变量值
        def replacer(match):
            key = match.group(1).strip()
            return str(kwargs.get(key, f"{{{{ {key} }}}}"))  # 若无值保留原样
        rendered = re.sub(r"{{\s*(\w+)\s*}}", replacer, content)
        return rendered
    except FileNotFoundError:
        return f"<h1>Template '{filename}' Not Found</h1>"

# 请求处理类
class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if DEBUG:
            print(f"[DEBUG] 收到请求路径：{self.path}")

        # 遍历注册路由，匹配请求路径
        for pattern, handler in ROUTES:
            match = pattern.match(self.path)
            if match:
                if DEBUG:
                    print(f"[DEBUG] 匹配函数：{handler.__name__}，参数：{match.groupdict()}")
                response = handler(**match.groupdict())
                self.send_response(200)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(response.encode("utf-8"))
                return

        # 静态文件访问
        for static_dir in static_dirs:
            if self.path.startswith(f'/{static_dir}/'):
                return self.getfile(static_dir)

        # 所有规则都不匹配，返回 404
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"404 Not Found")

    # 静态资源处理函数
    def getfile(self, static_dir):
        filepath = self.path.lstrip("/")  # 移除前导 /
        full_path = os.path.join(os.getcwd(), filepath)
        if DEBUG:
            print(f"[DEBUG] 请求静态文件：{full_path}")

        if os.path.exists(full_path) and os.path.isfile(full_path):
            # 获取扩展名决定内容类型
            ext = os.path.splitext(full_path)[1].lower()
            content_types = {
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.png': 'image/png',
                '.gif': 'image/gif',
                '.mp4': 'video/mp4',
                '.mp3': 'audio/mpeg',
                '.css': 'text/css',
                '.js': 'application/javascript'
            }
            self.send_response(200)
            self.send_header("Content-type", content_types.get(ext, 'application/octet-stream'))
            self.end_headers()
            with open(full_path, 'rb') as f:
                self.wfile.write(f.read())
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Static file not found")

# ========= 路由定义区 =========

@route("/")
def index():
    return showhtml("index.html")

@route("/hello/<name>")
def hello(name):
    return showhtml("hello.html", name=name, greeting="Welcome")

@route("/info")
def info():
    return showhtml("info.html", user="Alice", age=25, job="Scientist")

@route("/page")
def page():
    return showhtml("page.html")

# ========= 启动服务器 =========

print("🚀 Running on http://127.0.0.1:5000")
server = HTTPServer(("127.0.0.1", 5000), RequestHandler)
server.serve_forever()