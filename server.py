# -*- coding: utf-8 -*-
"""
局域网文件分享后端:
- 托管前端页面、本地字体与 QR 库
- 接收文件上传, 生成短 id
- 直链 /f/<文件名> 打开即下载; 另有下载页 /d/<id> 与直链 /d/<id>/raw
- /api/host 返回局域网基地址(支持 IPv4 + IPv6), 供前端生成二维码
- 启动时自动清理端口残留的旧进程(只清理本程序, 不误杀其他软件), 避免多进程冲突
- 端口被其他程序占用时自动换一个可用端口
- 元数据本地持久化 (metadata.json)，重启/多进程不丢文件
"""
import atexit
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time

from flask import Flask, abort, jsonify, request, send_file, send_from_directory, render_template_string
from werkzeug.exceptions import HTTPException

# 适应 PyInstaller 打包路径：资源存 MEIPASS，用户文件存 exe 目录
if getattr(sys, 'frozen', False):
    RESOURCE_DIR = sys._MEIPASS
    EXE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
    EXE_DIR = RESOURCE_DIR

WEB_DIR = os.path.join(RESOURCE_DIR, 'web')        # 前端资源(页面/二维码库/字体)
ASSETS_DIR = os.path.join(RESOURCE_DIR, 'assets')  # 图标
UPLOAD_DIR = os.path.join(EXE_DIR, 'uploads')
META_FILE = os.path.join(UPLOAD_DIR, 'metadata.json')
CONFIG_FILE = os.path.join(UPLOAD_DIR, 'config.json')
os.makedirs(UPLOAD_DIR, exist_ok=True)

DEFAULT_PORT = 8000
PORT = DEFAULT_PORT       # 当前实际监听的端口(可在设置里改, 写进 config.json)
TTL = 24 * 3600  # 文件保留 24 小时
CLEAN_INTERVAL = 10 * 60  # 后台清理周期
MIN_FREE_BYTES = 16 * 1024 * 1024  # 上传前至少保留的磁盘余量

app = Flask(__name__)

# metadata.json 是唯一的文件索引来源(内存里不再维护第二份副本, 避免两边不一致)
META_LOCK = threading.RLock()

# ============================================================
# 进程与元数据/配置管理
# ============================================================

def load_config():
    """加载配置"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                return cfg
        except Exception:
            pass
    return {'clean_on_exit': False, 'port': DEFAULT_PORT}


def _write_json(path, data):
    """原子写 JSON：先写临时文件再替换, 避免中途被杀导致文件损坏"""
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save_config(cfg):
    """保存配置"""
    try:
        with META_LOCK:
            _write_json(CONFIG_FILE, cfg)
    except Exception as e:
        print(f'⚠️ 保存配置失败: {e}', flush=True)


def _image_name(pid):
    """取进程可执行文件名(小写), 取不到返回空串"""
    try:
        out = subprocess.check_output(
            ['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'],
            text=True, errors='ignore', timeout=5,
        )
    except Exception:
        return ''
    m = re.match(r'\s*"([^"]+)"', out)
    return m.group(1).lower() if m else ''


def _is_our_image(image):
    """判断该进程是不是本程序(源码运行或打包后的 exe)"""
    if not image:
        return False
    if image in ('python.exe', 'pythonw.exe'):
        return True
    # 打包后 exe 叫什么名字都认(sys.executable 就是它自己)；
    # 另外兼容历史/当前的产品名，避免旧版 exe 占着端口没被清掉
    return (image == os.path.basename(sys.executable).lower()
            or 'quickshare' in image or 'lan-fileshare' in image)


def kill_port_occupant(port):
    """启动前清理占用该端口的旧进程：只清理本程序, 不误杀其他软件"""
    if platform.system() != 'Windows':
        return
    current_pid = os.getpid()
    foreign = []
    try:
        cmd = f'netstat -ano | findstr :{port}'
        out = subprocess.check_output(cmd, shell=True, text=True, errors='ignore')
        pids = set()
        for line in out.strip().splitlines():
            parts = line.split()
            # TCP  0.0.0.0:8000  0.0.0.0:0  LISTENING  1234
            if len(parts) >= 5 and parts[0].upper() == 'TCP' and 'LISTENING' in parts:
                if not parts[1].endswith(f':{port}'):   # 避免把 :80000 这类端口也算进来
                    continue
                try:
                    pid = int(parts[-1])
                    if pid != current_pid:
                        pids.add(pid)
                except ValueError:
                    pass
        for pid in sorted(pids):
            image = _image_name(pid)
            if _is_our_image(image):
                print(f'🧹 清理占用端口的旧进程 (PID: {pid})', flush=True)
                subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True)
            else:
                foreign.append((pid, image or '未知进程'))
    except Exception:
        pass
    for pid, image in foreign:
        print(f'⚠️ 端口 {port} 被其他程序占用，未自动结束: {image} (PID: {pid})', flush=True)


def port_in_use(port):
    """该端口是否已被监听"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(('127.0.0.1', port)) == 0


# ---------- 监听端口的起停（支持运行时换端口） ----------

_SERVER = None                      # 当前 server 实例
_PENDING = None                     # 已绑好、等待接手的新端口 socket: (sock, 模式说明)
_PENDING_LOCK = threading.Lock()
RESTART_EVENT = threading.Event()   # 置位 = 主循环需要重新绑定端口


def create_listen_socket(port):
    """绑好监听 socket：优先 IPv4+IPv6 双栈，退回仅 IPv4。

    直接返回绑好的 socket，把"端口可用性校验"和"占住端口"合并成一步，
    避免校验通过之后、真正切换之前端口被别人抢走。
    """
    if socket.has_dualstack_ipv6():
        try:
            return (socket.create_server(('', port), family=socket.AF_INET6, dualstack_ipv6=True),
                    'IPv4 + IPv6 双栈')
        except OSError:
            pass
    return socket.create_server(('', port)), '仅 IPv4'


def open_listen_socket(preferred):
    """按配置端口监听；被占用就顺延到下一个可用端口，返回 (socket, 端口, 模式)"""
    for port in range(preferred, preferred + 50):
        try:
            sock, mode = create_listen_socket(port)
        except OSError:
            continue
        if port != preferred:
            print(f'⚠️ 端口 {preferred} 被占用，自动改用 {port}', flush=True)
        return sock, port, mode
    raise SystemExit(f'❌ {preferred} 起的 50 个端口都不可用，无法启动')


def make_http_server(sock, port):
    """把绑好的 socket 交给 werkzeug（沿用它现成的请求处理，只换掉监听 socket）"""
    from werkzeug.serving import make_server
    srv = make_server('127.0.0.1', 0, app, threaded=True)
    srv.socket.close()
    srv.socket = sock
    srv.server_address = ('::' if sock.family == socket.AF_INET6 else '0.0.0.0', port)
    return srv


def claim_port(new_port, cfg):
    """设置里改端口：校验 + 立刻把新端口绑下来，成功返回 (端口, None)，失败返回 (None, 错误)"""
    try:
        port = int(str(new_port).strip())
    except (TypeError, ValueError):
        return None, '端口必须是数字'
    if not 1 <= port <= 65535:
        return None, '端口范围是 1 - 65535'
    if port == PORT:
        cfg['port'] = port
        return port, None                       # 没变，存一下就行

    try:
        sock, mode = create_listen_socket(port)
    except OSError:
        return None, f'端口 {port} 已被占用或没有权限'

    global _PENDING
    with _PENDING_LOCK:
        if _PENDING is not None:
            _PENDING[0].close()                 # 丢弃上一次没生效的
        _PENDING = (sock, mode)
    cfg['port'] = port
    return port, None


def schedule_restart():
    """稍后重启监听（先让本次响应发回客户端，再停掉旧端口）"""
    RESTART_EVENT.set()

    def _later():
        time.sleep(0.35)
        srv = _SERVER
        if srv is not None:
            srv.shutdown()

    threading.Thread(target=_later, daemon=True, name='port-switch').start()


def print_banner(port, mode, ip4, ip6):
    print('=' * 50, flush=True)
    print('  局域网文件分享服务', flush=True)
    print('=' * 50, flush=True)
    print(f'  本机访问:     http://localhost:{port}', flush=True)
    print(f'  IPv4 局域网:  http://{ip4}:{port}', flush=True)
    if ip6:
        print(f'  IPv6 局域网:  {format_ipv6_url(ip6, port)}', flush=True)
    else:
        print('  IPv6 局域网:  未检测到可用 IPv6 地址', flush=True)
    print('-' * 50, flush=True)
    if ip6:
        print('  二维码默认使用 IPv4，点右侧按钮可一键切换 IPv6', flush=True)
    else:
        print('  二维码使用 IPv4（未检测到 IPv6，接入后会自动启用，无需刷新）', flush=True)
    print(f'  监听模式: {mode}' + (' ✅' if '双栈' in mode else ''), flush=True)
    print('  端口可在网页右上角「设置」里修改', flush=True)
    print('  按 Ctrl+C 关闭程序', flush=True)
    print('=' * 50, flush=True)


def load_meta():
    """从磁盘加载文件元数据"""
    if os.path.exists(META_FILE):
        try:
            with open(META_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_meta(data):
    """保存文件元数据到磁盘"""
    try:
        _write_json(META_FILE, data)
    except Exception as e:
        print(f'⚠️ 保存元数据失败: {e}', flush=True)


def sanitize_name(name):
    """把客户端给的文件名收敛成安全的展示名(去掉目录部分和不合法字符)"""
    name = os.path.basename(str(name).replace('\\', '/')).strip()
    name = re.sub(r'[\x00-\x1f\x7f]', '', name)
    return name or '未命名文件'


def unique_slug(name, meta_dict, skip_fid=None):
    """生成不与他人重复的直链名: 报告.pdf -> 报告-2.pdf

    直链要写在 URL 里, 所以把有歧义的字符换成下划线: 空格(聊天软件里粘贴会被截断)、
    #?%&+ 和路径分隔符。中文等照常保留, 展示名不受影响。
    """
    slug = re.sub(r'[\s#?%&+\\/]+', '_', name).strip('_') or 'file'
    taken = {m.get('slug') for fid, m in meta_dict.items() if fid != skip_fid}
    stem, ext = os.path.splitext(slug)
    i = 1
    while slug in taken:
        i += 1
        slug = f'{stem}-{i}{ext}'
    return slug


def ensure_slugs(meta_dict):
    """给还没有直链名的旧条目补上(按索引顺序, 去重结果稳定)"""
    for fid in list(meta_dict.keys()):
        meta = meta_dict.get(fid)
        if meta is not None and not meta.get('slug'):
            meta_dict[fid] = dict(
                meta, slug=unique_slug(meta.get('name') or fid, meta_dict, skip_fid=fid))


def find_by_slug(meta_dict, slug):
    """按直链名找条目, 返回 (fid, meta)"""
    ensure_slugs(meta_dict)
    for fid, meta in meta_dict.items():
        if meta.get('slug') == slug:
            return fid, meta
    return None, None


def resolve_meta(meta_dict, fid):
    """按 id 取出 (元数据, 真实路径), 各处下载入口共用同一套规则。

    - 元数据缺失但 uploads/ 下有同名文件: 自动补建(兼容历史遗留文件)
    - 元数据里带 path 的是零复制分享, 源文件被删/改名时顺手清掉索引
    - 旧数据没有直链名时补一个, 老条目也能用 /f/文件名 访问
    - 返回 (None, None) 表示确实没有这个文件
    """
    meta = meta_dict.get(fid)

    if meta is not None and 'path' in meta:
        real_path = meta['path']
        if not os.path.isfile(real_path):
            meta_dict.pop(fid, None)      # 源文件已不在了
            return None, None
    else:
        real_path = os.path.join(UPLOAD_DIR, fid)
        if not os.path.isfile(real_path):
            if meta is not None:
                meta_dict.pop(fid, None)  # 临时文件被手工删掉了
            return None, None
        size = os.path.getsize(real_path)
        if meta is None:
            meta = {
                'name': f'文件-{fid}',
                'size': size,
                'ts': os.path.getmtime(real_path),
            }
        elif meta.get('size') != size:
            meta = dict(meta, size=size)  # 落盘后大小以磁盘为准

    if not meta.get('slug'):
        meta = dict(meta, slug=unique_slug(meta.get('name') or fid, meta_dict, skip_fid=fid))
    meta_dict[fid] = meta
    return meta, real_path


# ============================================================
# 网络地址检测
# ============================================================

def lan_ip():
    """通过 UDP 取本机局域网 IPv4"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        s.close()


def lan_ipv6():
    """获取本机的局域网 IPv6 地址（非 link-local/loopback）"""
    # 优先方法：通过 UDP 连接测试出站 IPv6
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        try:
            s.connect(('2001:4860:4860::8888', 80))
            addr = s.getsockname()[0].split('%')[0]
            if addr and addr != '::1' and not addr.startswith('fe80'):
                return addr
        except OSError:
            pass
        finally:
            s.close()
    except OSError:
        pass

    # 备用方法：遍历本机接口
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET6):
            addr = info[4][0].split('%')[0]
            if addr != '::1' and not addr.startswith('fe80'):
                return addr
    except (socket.gaierror, OSError):
        pass

    return None


def format_ipv6_url(addr, port):
    """格式化 IPv6 地址为 URL"""
    clean_addr = addr.split('%')[0]
    return f'http://[{clean_addr}]:{port}'


# ============================================================
# 退出清理
# ============================================================

def on_server_exit():
    """退出程序时如果有启用 clean_on_exit 则清空已上传临时文件"""
    cfg = load_config()
    if cfg.get('clean_on_exit'):
        print('🧹 退出服务：自动清空已上传的临时文件与元数据...', flush=True)
        try:
            for item in os.listdir(UPLOAD_DIR):
                item_path = os.path.join(UPLOAD_DIR, item)
                if item != 'config.json' and os.path.isfile(item_path):
                    try:
                        os.remove(item_path)
                    except OSError:
                        pass
            print('✨ 所有临时文件清理完成', flush=True)
        except Exception as e:
            print(f'⚠️ 清理临时文件异常: {e}', flush=True)


# ============================================================
# 工具与清理
# ============================================================

def human_size(n):
    if n < 1024:
        return f'{n} B'
    units = ['KB', 'MB', 'GB', 'TB']
    i = -1
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f'{n:.1f} {units[i]}'


def cleanup():
    """清理过期条目，并回收磁盘上已经没有对应文件的索引"""
    now = time.time()
    removed = 0
    with META_LOCK:
        meta_dict = load_meta()
        for fid, meta in list(meta_dict.items()):
            if now - meta.get('ts', 0) > TTL:
                # 仅在 uploads/ 目录下的拖拽上传临时文件才物理删除，本地零复制文件保留原文件仅删除索引
                if 'path' not in meta:
                    try:
                        os.remove(os.path.join(UPLOAD_DIR, fid))
                    except OSError:
                        pass
                meta_dict.pop(fid, None)
                removed += 1
            elif not os.path.isfile(meta.get('path') or os.path.join(UPLOAD_DIR, fid)):
                meta_dict.pop(fid, None)   # 文件已不在, 索引没有意义
                removed += 1
        if removed:
            save_meta(meta_dict)
    if removed:
        print(f'🧹 已清理 {removed} 个过期/失效条目', flush=True)
    return removed


def start_cleanup_loop():
    """后台定时清理：不再依赖“恰好有人访问”才触发"""
    def _loop():
        while True:
            time.sleep(CLEAN_INTERVAL)
            try:
                cleanup()
            except Exception as e:
                print(f'⚠️ 定时清理异常: {e}', flush=True)

    threading.Thread(target=_loop, daemon=True, name='cleanup-loop').start()


# ---------- 静态资源 ----------

@app.get('/')
def index():
    return send_from_directory(WEB_DIR, 'index.html')


@app.get('/qrcode.min.js')
def qr_lib():
    return send_from_directory(WEB_DIR, 'qrcode.min.js')


@app.get('/fonts/<path:filename>')
def font_file(filename):
    """本地字体(离线可用, 图标字体是裁切过的子集)"""
    return send_from_directory(os.path.join(WEB_DIR, 'fonts'), filename)


@app.get('/icon.png')
def app_icon():
    """页面 logo / 高清图标"""
    return send_from_directory(ASSETS_DIR, 'icon.png')


@app.get('/favicon.ico')
def favicon():
    """浏览器标签页图标"""
    return send_from_directory(ASSETS_DIR, 'favicon.ico')


# ---------- 统一错误响应 ----------
# 接口出错时返回 JSON，前端才能显示成一句人话，而不是一页 HTML

@app.errorhandler(HTTPException)
def handle_http_error(e):
    if request.path.startswith('/api/'):
        msg = '接口不存在' if e.code == 404 else (e.description or e.name)
        return jsonify(ok=False, error=msg, code=e.code), e.code
    return e


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    print(f'⚠️ 未处理异常: {e!r}', flush=True)
    if request.path.startswith('/api/'):
        return jsonify(ok=False, error=f'服务器内部错误: {e}'), 500
    return '服务器内部错误', 500


# ---------- API ----------

@app.get('/api/host')
def host():
    ip4 = lan_ip()
    ip6 = lan_ipv6()

    result = {
        'base': f'http://{ip4}:{PORT}',
        'ipv4': ip4,
    }
    if ip6:
        result['ipv6'] = ip6
        result['base_v6'] = format_ipv6_url(ip6, PORT)

    return jsonify(**result)


@app.post('/api/upload')
def upload():
    cleanup()
    f = request.files.get('file')
    if f is None or not f.filename:
        return jsonify(ok=False, error='没有收到文件'), 400

    # 预判磁盘空间: multipart 的 Content-Length 略大于文件本身, 作为上限估计足够
    need = request.content_length or 0
    free = shutil.disk_usage(UPLOAD_DIR).free
    if need and free < need + MIN_FREE_BYTES:
        return jsonify(
            ok=False,
            error=f'磁盘剩余空间不足（剩余 {human_size(free)}）',
        ), 507

    fid = secrets.token_urlsafe(6)
    path = os.path.join(UPLOAD_DIR, fid)
    try:
        f.save(path)
    except OSError as e:
        try:
            os.remove(path)           # 清掉写了一半的临时文件
        except OSError:
            pass
        print(f'⚠️ 保存上传文件失败: {e}', flush=True)
        return jsonify(ok=False, error=f'保存失败: {e}'), 500

    meta = {
        'name': sanitize_name(f.filename),
        'size': os.path.getsize(path),
        'ts': time.time(),
    }
    with META_LOCK:
        meta_dict = load_meta()
        # 直链名(真文件名)在索引里保持唯一, 重名自动变成 报告-2.pdf
        meta['slug'] = unique_slug(meta['name'], meta_dict)
        meta_dict[fid] = meta
        save_meta(meta_dict)
    print(f'[文件上传] {meta["name"]} ({human_size(meta["size"])}) -> id: {fid}, 直链: /f/{meta["slug"]}',
          flush=True)
    return jsonify(id=fid, name=meta['name'], size=meta['size'],
                   url=f'/d/{fid}', raw=f'/d/{fid}/raw', direct=f'/f/{meta["slug"]}')


@app.get('/api/files')
def list_files():
    cleanup()
    items = []
    with META_LOCK:
        meta_dict = load_meta()
        before = dict(meta_dict)
        for fid in list(meta_dict.keys()):
            meta, _ = resolve_meta(meta_dict, fid)   # 顺手修正失效/缺元数据的条目
            if meta is not None:
                items.append({
                    'id': fid,
                    'name': meta['name'],
                    'size': meta['size'],
                    'url': f'/d/{fid}',
                    'raw': f'/d/{fid}/raw',
                    'direct': f'/f/{meta["slug"]}',
                    'ts': meta.get('ts', 0),
                })
        if meta_dict != before:
            save_meta(meta_dict)
    items.sort(key=lambda x: x['ts'], reverse=True)
    return jsonify(files=items)


@app.post('/api/pick_file')
def pick_file():
    cleanup()
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        selected = filedialog.askopenfilenames(parent=root, title='选择要分享的文件')
        root.destroy()
    except Exception as e:
        print(f'⚠️ 原生文件选择框打开失败: {e}', flush=True)
        return jsonify(ok=False, error=str(e))

    if not selected:
        return jsonify(ok=False, cancelled=True)

    with META_LOCK:
        meta_dict = load_meta()
        files_res = []

        for fpath in selected:
            abs_path = os.path.abspath(fpath)
            if not os.path.isfile(abs_path):
                continue
            fid = secrets.token_urlsafe(6)
            filename = sanitize_name(os.path.basename(abs_path))
            filesize = os.path.getsize(abs_path)
            meta = {
                'name': filename,
                'size': filesize,
                'ts': time.time(),
                'path': abs_path,  # 零复制，直接引用本地绝对路径
                'slug': unique_slug(filename, meta_dict),
            }
            meta_dict[fid] = meta
            files_res.append({
                'id': fid,
                'name': filename,
                'size': filesize,
                'url': f'/d/{fid}',
                'raw': f'/d/{fid}/raw',
                'direct': f'/f/{meta["slug"]}',
            })
            print(f'[零复制分享] {filename} ({human_size(filesize)}) -> 直链: /f/{meta["slug"]}', flush=True)

        save_meta(meta_dict)
    return jsonify(ok=True, files=files_res)


@app.post('/api/delete/<fid>')
def delete_file(fid):
    with META_LOCK:
        meta_dict = load_meta()
        meta = meta_dict.pop(fid, None)
        if meta is not None:
            # 如果是拖拽上传文件，删临时文件；如果是零复制文件，不删源文件
            if 'path' not in meta:
                path = os.path.join(UPLOAD_DIR, fid)
                try:
                    os.remove(path)
                except OSError:
                    pass
            save_meta(meta_dict)
    return jsonify(ok=True, removed=meta is not None)


@app.post('/api/delete_all')
def delete_all():
    with META_LOCK:
        meta_dict = load_meta()
        for fid, meta in list(meta_dict.items()):
            # 拖拽上传的文件在 uploads/ 物理删除；零复制文件仅解绑索引，不删磁盘原文件
            if 'path' not in meta:
                try:
                    os.remove(os.path.join(UPLOAD_DIR, fid))
                except OSError:
                    pass
        count = len(meta_dict)
        save_meta({})
    print(f'[解绑清空] 已物理删除临时文件并解除 {count} 个零复制文件映射', flush=True)
    return jsonify(ok=True, removed=count)


@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    cfg = load_config()
    changed_port = False

    if request.method == 'POST':
        data = request.get_json(silent=True) or {}

        if 'clean_on_exit' in data:
            cfg['clean_on_exit'] = bool(data['clean_on_exit'])

        if 'port' in data:
            port, err = claim_port(data['port'], cfg)
            if err:
                return jsonify(ok=False, error=err), 400
            changed_port = port != PORT

        save_config(cfg)
        if changed_port:
            print(f'🔀 端口切换: {PORT} -> {cfg["port"]}', flush=True)
            schedule_restart()

    return jsonify(
        ok=True,
        clean_on_exit=cfg.get('clean_on_exit', False),
        port=cfg.get('port') or DEFAULT_PORT,   # 配置里保存的端口
        current_port=PORT,                      # 此刻真正监听的端口
        restarting=changed_port,
    )


# ---------- 下载页面与直链 ----------

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }}</title>
<style>
  body { font-family: system-ui, -apple-system, sans-serif; background: #FEF7FF; color: #1D1B20;
         display: flex; align-items: center; justify-content: center; min-height: 100vh;
         margin: 0; padding: 24px; box-sizing: border-box; }
  .card { background: #F3EDF7; border-radius: 28px; padding: 40px 32px;
          max-width: 420px; width: 100%; text-align: center; box-sizing: border-box; }
  .icon { width: 72px; height: 72px; border-radius: 50%; background: #EADDFF; color: #21005D;
          display: flex; align-items: center; justify-content: center; margin: 0 auto 20px; }
  .name { font-size: 18px; font-weight: 500; word-break: break-all; margin-bottom: 6px; }
  .size { font-size: 14px; color: #49454F; margin-bottom: 28px; }
  .btn  { display: inline-flex; align-items: center; gap: 8px; background: #6750A4; color: #fff;
          text-decoration: none; font-size: 15px; font-weight: 500; padding: 0 28px; height: 48px;
          border-radius: 999px; }
  .err  { color: #B3261E; }
</style>
</head>
<body>
  <div class="card">
    <div class="icon">
      <svg width="32" height="32" viewBox="0 0 24 24" fill="currentColor">
        <path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/>
      </svg>
    </div>
    {% if name %}
    <div class="name">{{ name }}</div>
    <div class="size">{{ size }}</div>
    <a class="btn" href="/d/{{ fid }}/raw" download>下载文件</a>
    {% else %}
    <div class="name err">文件不存在或已过期</div>
    <div class="size">请重新生成分享二维码</div>
    {% endif %}
  </div>
</body>
</html>"""


@app.get('/d/<fid>')
def download_page(fid):
    with META_LOCK:
        meta_dict = load_meta()
        before = dict(meta_dict)
        meta, _ = resolve_meta(meta_dict, fid)
        if meta_dict != before:
            save_meta(meta_dict)

    if meta is None:
        return render_template_string(PAGE, title='文件不存在', name=None), 404
    return render_template_string(
        PAGE,
        title=f'下载 {meta["name"]}',
        name=meta['name'],
        size=human_size(meta['size']),
        fid=fid,
    )


@app.get('/d/<fid>/raw')
def download_raw(fid):
    with META_LOCK:
        meta_dict = load_meta()
        before = dict(meta_dict)
        meta, real_path = resolve_meta(meta_dict, fid)
        if meta_dict != before:
            save_meta(meta_dict)          # 顺手清掉失效索引
    if meta is None:
        abort(404)
    return send_file(real_path, as_attachment=True, download_name=meta['name'])


@app.get('/f/<path:slug>')
def download_direct(slug):
    """直链: /f/<真文件名>, 打开即下载, 不用经过下载页"""
    with META_LOCK:
        meta_dict = load_meta()
        before = dict(meta_dict)
        fid, meta = find_by_slug(meta_dict, slug)
        real_path = None
        if fid is not None:
            meta, real_path = resolve_meta(meta_dict, fid)
        if meta_dict != before:
            save_meta(meta_dict)
    if meta is None or real_path is None:
        return render_template_string(PAGE, title='文件不存在', name=None), 404
    return send_file(real_path, as_attachment=True, download_name=meta['name'])


if __name__ == '__main__':
    # 启动前清理占端口的旧进程(只清理本程序, 不会误杀别的软件)
    old_port = load_config().get('port') or DEFAULT_PORT
    kill_port_occupant(old_port)
    for _ in range(10):                  # 被结束的进程释放端口需要一点时间
        if not port_in_use(old_port):
            break
        time.sleep(0.2)

    # 注册程序退出时的清理钩子
    atexit.register(on_server_exit)

    # 后台定时清理过期文件(不依赖是否有人访问)
    start_cleanup_loop()

    ip4 = lan_ip()
    ip6 = lan_ipv6()
    first_round = True

    while True:
        # 优先接手"设置里刚改好的端口"，否则按配置端口起
        with _PENDING_LOCK:
            pending = _PENDING
            _PENDING = None
        if pending is not None:
            sock, mode = pending
            port = sock.getsockname()[1]
        else:
            preferred = load_config().get('port') or DEFAULT_PORT
            sock, port, mode = open_listen_socket(preferred)

        PORT = port
        _SERVER = make_http_server(sock, port)
        RESTART_EVENT.clear()
        print_banner(port, mode, ip4, ip6)

        # 自动弹出默认浏览器（只在首次启动时；改端口后由页面自己跳转）
        if first_round:
            first_round = False

            def _auto_open_browser():
                import webbrowser
                time.sleep(0.5)
                webbrowser.open(f'http://localhost:{PORT}')

            try:
                threading.Thread(target=_auto_open_browser, daemon=True).start()
            except Exception:
                pass

        try:
            _SERVER.serve_forever()
        except KeyboardInterrupt:
            print('\n已停止服务', flush=True)
            break
        finally:
            _SERVER.server_close()
            _SERVER = None

        if not RESTART_EVENT.is_set():
            break
