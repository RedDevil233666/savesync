#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
savesync - 游戏存档云同步工具（单文件，零第三方依赖，Python 3.8+）

用法示例:
    python savesync.py setup-webdav --url https://dav.jianguoyun.com/dav --user a@b.com --password 应用密码
    python savesync.py scan --add-known     # 扫描本机已知游戏存档并加入配置
    python savesync.py add mygame --name "某游戏" --path "C:\\存档路径"
    python savesync.py push                 # 上传全部存档
    python savesync.py pull                 # 下载云端最新存档到本机
    python savesync.py status               # 查看本机/云端差异
    python savesync.py doctor               # 检查后端连通性与配置

云端目录结构:
    {root}/{game_id}/manifest.json          元数据（hash/时间/机器名/历史列表）
    {root}/{game_id}/latest.zip             最新存档包
    {root}/{game_id}/history/*.zip          历史版本（默认保留 5 份）
"""

import argparse
import datetime
import hashlib
import hmac
import http.client
import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlparse, quote

VERSION = "1.0.0"

APP_DIR = Path.home() / ".savesync"
CONFIG_FILE = APP_DIR / "config.json"
STATE_FILE = APP_DIR / "state.json"
BACKUP_DIR = APP_DIR / "backups"
HISTORY_KEEP = 5          # 云端保留历史版本数
BACKUP_KEEP = 10          # 本地 pull 前备份保留数

# ---------------------------------------------------------------------------
# 已知游戏存档数据库
# platform=win（默认）: rel 相对于「Windows 用户目录」（本机 Windows 或 CrossOver bottle 内用户目录）
# platform=mac:        rel 相对于 macOS 真实用户主目录
# ---------------------------------------------------------------------------
KNOWN_GAMES = [
    # --- Windows / CrossOver bottle ---
    {
        "id": "wulin",
        "name": "大侠立志传 (Hero's Adventure)",
        "rel": "AppData/LocalLow/DefaultCompany/Wulin",
        "note": "整个 Wulin 目录，含 SteamID 子目录与 Global 系统数据",
    },
    {
        "id": "wandering-sword",
        "name": "逸剑风云决 (Wandering Sword)",
        "rel": "AppData/Local/Wandering_Sword",
        "note": "存档在 Saved\\SteamID\\SaveGames（0-29手动 30-59自动）",
    },
    {
        "id": "elden-ring",
        "name": "艾尔登法环 (Elden Ring)",
        "rel": "AppData/Roaming/EldenRing",
        "note": "存档 ER0000.sl2，按 SteamID 子目录存放",
    },
    {
        "id": "tlou2",
        "name": "最后生还者2 (The Last of Us Part II)",
        "rel": "AppData/Roaming/Naughty Dog/The Last of Us Part II",
        "note": "user.pso 为玩家数据，crs 为进度",
    },
    {
        "id": "rdr2",
        "name": "荒野大镖客2 (Red Dead Redemption 2)",
        "rel": "Documents/Rockstar Games/Red Dead Redemption 2",
        "note": "存档在 Profiles/<ID>/（cloudsavedata.dat 等），顺带含 Settings",
    },
    {
        "id": "gtav-enhanced",
        "name": "GTA5增强版 (GTA V Enhanced)",
        "rels": ["Documents/Rockstar Games/GTA V Enhanced",
                 "Documents/Rockstar Games/GTAV Enhanced"],
        "note": "存档在 Profiles/<ID>/，其余走 Social Club 云端",
    },
    {
        "id": "kcd2",
        "name": "天国拯救2 (Kingdom Come: Deliverance II)",
        "rel": "Saved Games/kingdomcome2",
        "note": "存档 saves/playline0/*.whs",
    },
    {
        "id": "bg3",
        "name": "博德之门3 (Baldur's Gate 3)",
        "rel": "Documents/Larian Studios/Baldur's Gate 3/PlayerProfiles",
        "note": "存档 PlayerProfiles/<档案>/Savegames/Story（.lsv）",
    },
    {
        "id": "octopath2",
        "name": "八方旅人2 (Octopath Traveler II)",
        "rel": "Documents/My Games/Octopath_Traveler2",
        "note": "UE 存档，按 SteamID 子目录存放",
    },
    {
        "id": "dq7-reimagined",
        "name": "勇者斗恶龙7 重制版 (DQ VII Reimagined)",
        "rel": "Documents/My Games/DRAGON QUEST VII",
        "note": "UE 存档，按 SteamID 子目录存放",
    },
    {
        "id": "ac4-blackflag",
        "name": "刺客信条4黑旗重置版 (AC Black Flag Resynced)",
        "rel": "AppData/Roaming/Goldberg UplayEmu Saves/66088",
        "note": "Uplay 存档位置（Goldberg 模拟目录 66088 = AC4 appid）",
    },
]


def user_roots():
    """返回 [(标签, 用户主目录Path), ...]。
    Windows: 本机用户目录；
    macOS:   本机用户目录 + 所有 CrossOver bottle 内的 Windows 用户目录
             （~/Library/Application Support/CrossOver/Bottles/<名>/drive_c/users/<用户>）
    """
    roots = []
    if os.name == "nt":
        up = os.environ.get("USERPROFILE") or str(Path.home())
        roots.append(("本机", Path(up)))
    else:
        roots.append(("本机", Path.home()))
        bottles = Path.home() / "Library" / "Application Support" / "CrossOver" / "Bottles"
        try:
            if bottles.is_dir():
                for b in sorted(bottles.iterdir()):
                    users = b / "drive_c" / "users"
                    if not users.is_dir():
                        continue
                    for u in sorted(users.iterdir()):
                        if u.is_dir() and (u / "AppData").is_dir():
                            roots.append(("CrossOver:" + b.name, u))
        except OSError:
            pass
    return roots


def _drive_root(user_root):
    """CrossOver bottle 用户目录 -> 所在 drive_c（虚拟C盘）；本机 Windows 返回 None。"""
    for anc in Path(user_root).parents:
        if anc.name == "drive_c":
            return anc
    return None


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ts_tag():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def hostname():
    return socket.gethostname().split(".")[0]


def resolve_path(p):
    return Path(os.path.expandvars(os.path.expanduser(p)))


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f%s" % (n, unit) if unit != "B" else "%dB" % n
        n /= 1024.0


def die(msg):
    print("[!!] " + msg)
    sys.exit(1)


def warn(msg):
    print("[!!] " + msg)


def ok(msg):
    print("[OK] " + msg)


def info(msg):
    print("[--] " + msg)


def hash_dir(path):
    """对目录内容做整体哈希（相对路径+大小+文件内容），跨机器可比。"""
    h = hashlib.sha256()
    files = 0
    total = 0
    for p in sorted(path.rglob("*")):
        if p.is_file():
            rel = p.relative_to(path).as_posix()
            h.update(rel.encode("utf-8"))
            st = p.stat()
            h.update(str(st.st_size).encode())
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 16), b""):
                    h.update(chunk)
            files += 1
            total += st.st_size
    return h.hexdigest(), files, total


def make_zip(path):
    """把目录打包为 zip 字节流，返回 (zip_bytes, 文件数, 压缩后大小)。"""
    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(path.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(path).as_posix())
                n += 1
    data = buf.getvalue()
    return data, n, len(data)


def safe_extract(zf, dest):
    dest = Path(dest).resolve()
    for info in zf.infolist():
        target = (dest / info.filename).resolve()
        if not str(target).startswith(str(dest) + os.sep) and target != dest:
            raise RuntimeError("非法压缩包条目（防目录穿越）: " + info.filename)
    zf.extractall(dest)


# ---------------------------------------------------------------------------
# 存储后端
# ---------------------------------------------------------------------------
class Backend:
    def get(self, key):      raise FileNotFoundError(key)
    def put(self, key, data): raise NotImplementedError
    def delete(self, key):   raise NotImplementedError
    def exists(self, key):   raise NotImplementedError
    def test(self):          return "unknown"


class LocalBackend(Backend):
    """本地目录后端：适合 NAS(SMB挂载)/U盘/双机共享盘。"""

    def __init__(self, cfg):
        self.root = Path(os.path.expandvars(os.path.expanduser(cfg["dir"]))).resolve()

    def _p(self, key):
        return self.root / key

    def get(self, key):
        p = self._p(key)
        if not p.is_file():
            raise FileNotFoundError(str(p))
        return p.read_bytes()

    def put(self, key, data):
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def delete(self, key):
        p = self._p(key)
        if p.exists():
            p.unlink()

    def exists(self, key):
        return self._p(key).is_file()

    def test(self):
        self.root.mkdir(parents=True, exist_ok=True)
        probe = ".savesync_probe"
        self.put(probe, b"ok")
        r = self.get(probe)
        self.delete(probe)
        return "ok (读写正常)" if r == b"ok" else "异常"


class WebDAVBackend(Backend):
    """WebDAV 后端：坚果云 https://dav.jianguoyun.com/dav 等。"""

    def __init__(self, cfg):
        self.base = cfg["url"].rstrip("/")
        self.user = cfg.get("user", "")
        self.password = cfg.get("password", "")
        self._mkdir_cache = set()
        parsed = urlparse(self.base)
        self._base_path = parsed.path.rstrip("/")

    def _headers(self, extra=None):
        h = {}
        if self.user:
            import base64
            token = base64.b64encode(
                ("%s:%s" % (self.user, self.password)).encode("utf-8")).decode()
            h["Authorization"] = "Basic " + token
        if extra:
            h.update(extra)
        return h

    def _mkcol_parents(self, key_path):
        """对 key 的所有父目录逐级 MKCOL（幂等，按完整路径缓存）。"""
        segs = [s for s in key_path.split("/") if s]
        parts = [seg for seg in self._base_path.split("/") if seg]
        dirs = []
        acc = ""
        for pt in parts:
            acc += "/" + pt
            dirs.append(acc)
        acc = self._base_path
        for s in segs[:-1]:
            acc += "/" + s
            dirs.append(acc)
        for d in dirs:
            if d in self._mkdir_cache:
                continue
            try:
                req = urllib.request.Request(
                    self._scheme_host() + quote(d),
                    method="MKCOL", headers=self._headers())
                urllib.request.urlopen(req, timeout=30)
            except Exception:
                pass  # 已存在/无权限均忽略，真正失败会在 PUT 时暴露
            self._mkdir_cache.add(d)

    def _scheme_host(self):
        return self.base.split("/")[0] + "//" + urlparse(self.base).netloc

    def _url(self, key):
        return self.base + "/" + quote(key, safe="/")

    def _request(self, method, key, data=None):
        self._mkcol_parents(key)
        req = urllib.request.Request(self._url(key), data=data, method=method,
                                     headers=self._headers())
        try:
            resp = urllib.request.urlopen(req, timeout=120)
            return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            body = e.read() if hasattr(e, "read") else b""
            if e.code == 404:
                raise FileNotFoundError(key)
            raise RuntimeError("WebDAV %s %s 失败: HTTP %d %s"
                               % (method, key, e.code, body[:200].decode("utf-8", "replace")))
        except urllib.error.URLError as e:
            raise RuntimeError("WebDAV 连接失败: %s（检查网络/URL）" % e.reason)

    def get(self, key):
        _, body = self._request("GET", key)
        return body

    def put(self, key, data):
        self._request("PUT", key, data)

    def delete(self, key):
        try:
            self._request("DELETE", key)
        except FileNotFoundError:
            pass

    def exists(self, key):
        try:
            self._request("GET", key)
            return True
        except FileNotFoundError:
            return False

    def test(self):
        probe = ".savesync_probe"
        self.put(probe, b"ok")
        r = self.get(probe)
        self.delete(probe)
        return "ok (读写正常)" if r == b"ok" else "异常"


class S3Backend(Backend):
    """S3 兼容后端：腾讯云 COS / 阿里云 OSS / Cloudflare R2 等（AWS SigV4）。"""

    def __init__(self, cfg):
        self.bucket = cfg["bucket"]
        self.host = ("%s.%s" % (self.bucket, cfg["endpoint"]))
        self.region = cfg.get("region", "us-east-1")
        self.service = cfg.get("service", "s3")
        self.ak = cfg["ak"]
        self.sk = cfg["sk"]
        self.https = cfg.get("https", True)

    def _sign_headers(self, method, key, payload, amzdate=None):
        if amzdate is None:
            t = datetime.datetime.now(datetime.timezone.utc)
            amzdate = t.strftime("%Y%m%dT%H%M%SZ")
        datestamp = amzdate[:8]
        payload_hash = hashlib.sha256(payload).hexdigest()
        canonical_uri = "/" + quote(key, safe="/")
        headers = {
            "host": self.host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amzdate,
        }
        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join(
            "%s:%s\n" % (k, headers[k]) for k in sorted(headers))
        canonical_request = "\n".join([
            method, canonical_uri, "", canonical_headers,
            signed_headers, payload_hash])
        scope = "%s/%s/%s/aws4_request" % (datestamp, self.region, self.service)
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256", amzdate, scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])

        def _hmac(key, msg):
            return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

        k = _hmac(("AWS4" + self.sk).encode("utf-8"), datestamp)
        k = _hmac(k, self.region)
        k = _hmac(k, self.service)
        k = _hmac(k, "aws4_request")
        signature = hmac.new(k, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        headers["Authorization"] = (
            "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
            % (self.ak, scope, signed_headers, signature))
        return headers

    def _request(self, method, key, payload=b""):
        headers = self._sign_headers(method, key, payload)
        conn_cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
        conn = conn_cls(self.host, timeout=120)
        try:
            path = "/" + quote(key, safe="/")
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
            if resp.status == 404:
                raise FileNotFoundError(key)
            if resp.status >= 300:
                raise RuntimeError("S3 %s %s 失败: HTTP %d %s"
                                   % (method, key, resp.status,
                                      body[:300].decode("utf-8", "replace")))
            return body
        finally:
            conn.close()

    def get(self, key):
        return self._request("GET", key)

    def put(self, key, data):
        self._request("PUT", key, data)

    def delete(self, key):
        try:
            self._request("DELETE", key)
        except FileNotFoundError:
            pass

    def exists(self, key):
        try:
            self._request("GET", key)
            return True
        except FileNotFoundError:
            return False

    def test(self):
        probe = ".savesync_probe"
        self.put(probe, b"ok")
        r = self.get(probe)
        self.delete(probe)
        return "ok (读写正常)" if r == b"ok" else "异常"


def make_backend(cfg, strict=True):
    b = cfg.get("backend")
    if not b or not b.get("type"):
        if strict:
            die("未配置存储后端，请先运行 setup-webdav / setup-s3 / setup-local")
        return None
    t = b["type"]
    if t == "local":
        return LocalBackend(b)
    if t == "webdav":
        return WebDAVBackend(b)
    if t == "s3":
        return S3Backend(b)
    die("未知后端类型: " + t)


# ---------------------------------------------------------------------------
# 配置与状态
# ---------------------------------------------------------------------------
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_config():
    return load_json(CONFIG_FILE, {"backend": None, "games": []})


def save_config(cfg):
    save_json(CONFIG_FILE, cfg)


def load_state():
    return load_json(STATE_FILE, {"games": {}})


def save_state(st):
    save_json(STATE_FILE, st)


def get_game(cfg, gid):
    for g in cfg["games"]:
        if g["id"] == gid:
            return g
    return None


def select_games(cfg, ids):
    if not ids:
        return list(cfg["games"])
    out, missing = [], []
    for i in ids:
        g = get_game(cfg, i)
        if g:
            out.append(g)
        else:
            missing.append(i)
    if missing:
        warn("以下游戏 ID 不存在，已跳过: " + ", ".join(missing))
    return out


# ---------------------------------------------------------------------------
# 云端 manifest
# ---------------------------------------------------------------------------
def fetch_manifest(backend, gid):
    try:
        return json.loads(backend.get("%s/manifest.json" % gid).decode("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def upload_manifest(backend, gid, m):
    backend.put("%s/manifest.json" % gid,
                json.dumps(m, ensure_ascii=False, indent=2).encode("utf-8"))


# ---------------------------------------------------------------------------
# 命令实现
# ---------------------------------------------------------------------------
def cmd_setup_webdav(args):
    url = args.url.rstrip("/")
    root = (args.root or "savesync").strip("/")
    cfg = load_config()
    cfg["backend"] = {
        "type": "webdav",
        "url": "%s/%s" % (url, root),
        "user": args.user,
        "password": args.password,
    }
    save_config(cfg)
    info("已写入 " + str(CONFIG_FILE))
    backend = make_backend(cfg)
    try:
        ok("WebDAV 连接测试: " + backend.test())
    except Exception as e:
        warn("连接测试失败: %s（配置已保存，可用 doctor 重新检查）" % e)


def cmd_setup_s3(args):
    cfg = load_config()
    cfg["backend"] = {
        "type": "s3",
        "bucket": args.bucket,
        "endpoint": args.endpoint.rstrip("/"),
        "region": args.region,
        "ak": args.ak,
        "sk": args.sk,
        "service": args.service,
    }
    save_config(cfg)
    info("已写入 " + str(CONFIG_FILE))
    backend = make_backend(cfg)
    try:
        ok("S3 连接测试: " + backend.test())
    except Exception as e:
        warn("连接测试失败: %s（配置已保存，可用 doctor 重新检查）" % e)


def cmd_setup_local(args):
    cfg = load_config()
    cfg["backend"] = {"type": "local", "dir": args.dir}
    save_config(cfg)
    info("已写入 " + str(CONFIG_FILE))
    backend = make_backend(cfg)
    ok("本地目录测试: " + backend.test())


def cmd_doctor(args):
    cfg = load_config()
    print("savesync v%s  主机: %s  时间: %s" % (VERSION, hostname(), now_str()))
    print()
    if cfg.get("backend"):
        b = cfg["backend"]
        print("存储后端: %s" % b["type"])
        if b["type"] == "webdav":
            print("  地址: %s  账号: %s" % (b["url"], b.get("user", "")))
        elif b["type"] == "s3":
            print("  bucket: %s  endpoint: %s  region: %s"
                  % (b["bucket"], b["endpoint"], b.get("region")))
        else:
            print("  目录: %s" % b.get("dir"))
        try:
            backend = make_backend(cfg)
            ok("后端读写测试: " + backend.test())
        except Exception as e:
            warn("后端读写失败: %s" % e)
    else:
        warn("未配置存储后端")
    print()
    if cfg.get("games"):
        print("已配置游戏:")
        for g in cfg["games"]:
            p = resolve_path(g["path"])
            mark = "[OK]" if p.exists() else "[!!]"
            print("  %s %-18s %s" % (mark, g["id"], g["name"]))
            print("       路径: %s" % p)
    else:
        warn("未配置任何游戏，先运行 scan --add-known 或 add")


def _win_steam_path():
    """Windows 上从注册表找 Steam 安装路径（找不到则回退默认路径）。"""
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"Software\Valve\Steam") as k:
                    v, _ = winreg.QueryValueEx(k, "SteamPath")
                    if v:
                        return str(v).replace("/", "\\")
            except OSError:
                continue
    except Exception:
        pass
    return r"C:\Program Files (x86)\Steam"


def _map_bottle_path(drive_c, winpath):
    """把 CrossOver bottle 内 vdf 记录的 Windows 路径（如 E:\\SteamLibrary）
    通过 dosdevices 盘符映射翻译成真实 Mac 路径；翻译不了返回 None。"""
    m = re.match(r"^([A-Za-z]):[\\/]+(.*)$", winpath.strip())
    if not m:
        return None
    letter, rest = m.group(1).lower(), m.group(2).replace("\\", "/")
    dos = drive_c.parent / "dosdevices" / (letter + ":")
    try:
        if dos.is_dir():
            return dos / rest
    except OSError:
        pass
    if letter == "c":
        return drive_c / rest
    return None


def _steam_libraries(drive_c=None):
    """从 libraryfolders.vdf 解析 Steam 库路径（尽力而为）。
    drive_c 传入 CrossOver bottle 的 drive_c 路径时在该虚拟盘内查找，
    vdf 里的 Windows 路径经 dosdevices 映射回真实路径。
    macOS 本机 Steam 库（~/Library/Application Support/Steam）也会纳入。
    Windows 上优先从注册表读 Steam 安装路径。"""
    libs = []
    if drive_c is not None:
        bases = [str(drive_c / "Program Files (x86)" / "Steam"),
                 str(drive_c / "Program Files" / "Steam")]
    else:
        if os.name == "nt":
            bases = [_win_steam_path()]
        else:
            bases = []
        for d in "CDEFGH":
            bases.append("%s:\\SteamLibrary" % d)
            bases.append("%s:\\Steam" % d)
        if os.name != "nt":
            mac_steam = Path.home() / "Library" / "Application Support" / "Steam"
            bases.append(str(mac_steam))
    raw = []
    for b in bases:
        vdf = Path(b) / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            try:
                text = vdf.read_text(encoding="utf-8", errors="replace")
                raw += re.findall(r'"path"\s+"([^"]+)"', text)
            except Exception:
                pass
        if (Path(b) / "steamapps" / "common").is_dir():
            raw.append(b)
    # bottle 内：vdf 记录的是 Windows 路径，翻译成 Mac 真实路径
    if drive_c is not None:
        for w in raw:
            if re.match(r"^[A-Za-z]:[\\/]", w.strip()):
                mp = _map_bottle_path(drive_c, w)
                if mp and (mp / "steamapps" / "common").is_dir():
                    libs.append(str(mp))
            else:
                libs.append(w)
    else:
        libs = list(raw)
    seen, out = set(), []
    for l in libs:
        l = l.replace("\\\\", "\\")
        key = _real(l) if not re.match(r"^[A-Za-z]:[\\/]", l) else l.lower()
        if key not in seen:
            seen.add(key)
            out.append(l)
    return out


# 存档文件名特征（用于通用扫描识别；刻意排除 .dat/.bak/.vdf 等通用后缀以免误报）
SAVE_FILE_HINT = re.compile(
    r"\.(sav|sl2|save|whs|lsv|ess|erdb|er0|ucp|pso|savemeta|dss|w3)$"
    r"|^(save|autosave|quicksave|slot|manualsave|backup)", re.I)

# AppData 等目录下的非游戏噪音项
NOISE_DIRS = {
    "microsoft", "adobe", "wine_gecko", "wine", "cef", "easyanticheat",
    "temp", "tmp", "programs", "nvidia corporation", "unrealengine", "steam",
    "crashdumps", "mozilla", "google", "cache", "caches", "logs", "code",
    "cryptneturlcache", "crashpad",
}

# macOS ~/Library/Application Support 下与游戏无关的常见应用
MAC_NATIVE_SKIP = {
    "google", "quarkclouddrive", "mihomo-party", "lm studio", "coze",
    "bionic", "codex", "adobe", "mozilla", "apple", "chrome", "electron",
    "crossover", "steam", "gamehub", "com.apple.games", "claude", "cherry studio",
}


def _safe_iter(d):
    """列出目录下非隐藏子项，出错返回空。"""
    try:
        return sorted(x for x in Path(d).iterdir() if not x.name.startswith("."))
    except Exception:
        return []


def _dir_has_save_hint(path, max_depth=2, max_items=500):
    """目录内（限深度）是否存在疑似存档文件。"""
    try:
        count = 0
        stack = [(Path(path), 0)]
        while stack:
            d, depth = stack.pop()
            for e in _safe_iter(d):
                count += 1
                if count > max_items:
                    return False
                if e.is_file():
                    if SAVE_FILE_HINT.search(e.name):
                        return True
                elif depth < max_depth and e.is_dir():
                    stack.append((e, depth + 1))
    except Exception:
        pass
    return False


def _dir_stat(path):
    """返回 (文件数, 总字节)。"""
    files, total = 0, 0
    try:
        for p in Path(path).rglob("*"):
            if p.is_file():
                files += 1
                total += p.stat().st_size
    except Exception:
        pass
    return files, total


def _real(p):
    """规范化真实路径用于去重（macOS 大小写不敏感，统一转小写）。"""
    try:
        s = str(Path(p).resolve())
    except Exception:
        s = str(p)
    return s.lower() if sys.platform == "darwin" else os.path.normcase(s)


def _scan_root_candidates(label, root, candidates):
    """在一个用户根目录（本机或 bottle 内）扫描疑似存档目录。"""

    def add_candidate(kind, path):
        try:
            rp = _real(path)
            if path.is_dir() and not any(_real(c[2]) == rp for c in candidates):
                candidates.append((label, kind, path))
        except Exception:
            pass

    root = Path(root)

    # Documents/My Games/<game>（UE 等常用位置）
    for d in _safe_iter(root / "Documents" / "My Games"):
        if d.is_dir():
            add_candidate("MyGames", d)
    # Documents/<厂商>/<game>（Rockstar/Larian/Naughty Dog 等）
    for vendor in _safe_iter(root / "Documents"):
        if not vendor.is_dir():
            continue
        for game in _safe_iter(vendor):
            if game.is_dir() and _dir_has_save_hint(game):
                add_candidate("Docs/" + vendor.name, game)
    # Saved Games/<game>（KCD2 等）
    for d in _safe_iter(root / "Saved Games"):
        if d.is_dir() and _dir_has_save_hint(d):
            add_candidate("SavedGames", d)
    # AppData/LocalLow/<厂商>/<game>
    for comp in _safe_iter(root / "AppData" / "LocalLow"):
        if not comp.is_dir() or comp.name.lower() in NOISE_DIRS:
            continue
        for game in _safe_iter(comp):
            if game.is_dir():
                add_candidate("LocalLow", game)
    # AppData/Local/<game>：UE 的 Saved/SaveGames 或存档特征
    for d in _safe_iter(root / "AppData" / "Local"):
        if not d.is_dir():
            continue
        if (d / "Saved" / "SaveGames").is_dir():
            add_candidate("UE-SaveGames", d / "Saved" / "SaveGames")
        elif d.name.lower() not in NOISE_DIRS and _dir_has_save_hint(d):
            add_candidate("Local", d)
    # AppData/Roaming/<game>（EldenRing / Naughty Dog 等）
    for d in _safe_iter(root / "AppData" / "Roaming"):
        if d.is_dir() and d.name.lower() not in NOISE_DIRS and _dir_has_save_hint(d):
            add_candidate("Roaming", d)
    # Steam 库：游戏安装目录里的 save/uds 子目录
    # 只在 CrossOver bottle（dr 非 None）或 Windows 上扫；macOS 本机 Steam 是
    # Mac 原生游戏，不同步（dr 为 None 且非 Windows 时跳过）
    dr = _drive_root(root)
    if dr is None and os.name != "nt":
        return
    for lib in _steam_libraries(dr):
        common = Path(lib) / "steamapps" / "common"
        for d in _safe_iter(common):
            if not d.is_dir():
                continue
            for sub in _safe_iter(d):
                if sub.is_dir() and re.search(r"save|存档|uds", sub.name, re.I):
                    add_candidate("Steam/" + d.name, sub)


def _scan_mac_native(candidates):
    """macOS 原生游戏存档（~/Library/Application Support）。"""
    asup = Path.home() / "Library" / "Application Support"
    if not asup.is_dir():
        return

    def add_candidate(kind, path):
        try:
            rp = _real(path)
            if path.is_dir() and not any(_real(c[2]) == rp for c in candidates):
                candidates.append(("Mac原生", kind, path))
        except Exception:
            pass

    for d in _safe_iter(asup):
        if not d.is_dir() or d.name.lower() in MAC_NATIVE_SKIP:
            continue
        # com.* bundle 直接带 SAVEDATA 目录的（如苏丹的游戏）
        if d.name.startswith("com.") and (d / "SAVEDATA").is_dir():
            add_candidate(d.name, d / "SAVEDATA")
            continue
        # <厂商>/<游戏>/Saved 或存档特征（如 CD Projekt Red/Cyberpunk 2077）
        for game in _safe_iter(d):
            if not game.is_dir() or game.name.lower() in NOISE_DIRS:
                continue
            if (game / "Saved").is_dir() or _dir_has_save_hint(game):
                add_candidate(d.name + "/" + game.name, game)


def _scan_game_install_dirs(candidates):
    """游戏安装目录里的 save/uds 子目录。
    覆盖：~/games 等用户目录；Windows 下各盘符的 Games/游戏 目录；
    以及 config.json 里 scan_dirs 自定义的目录。"""
    bases = [Path.home() / "games", Path.home() / "Games", Path.home() / "游戏"]
    if os.name == "nt":
        for d in "CDEFGH":
            for n in ("Games", "games", "游戏"):
                bases.append(Path("%s:\\" % d) / n)
    try:
        for s in (load_config().get("scan_dirs") or []):
            p = resolve_path(s)
            if p.is_dir():
                bases.append(p)
    except Exception:
        pass

    def add_candidate(kind, path):
        try:
            rp = _real(path)
            if path.is_dir() and not any(_real(c[2]) == rp for c in candidates):
                candidates.append(("安装目录", kind, path))
        except Exception:
            pass

    for base in bases:
        if not base.is_dir():
            continue
        for d in _safe_iter(base):
            if not d.is_dir():
                continue
            for sub in _safe_iter(d):
                if sub.is_dir() and re.search(r"^(save|saves|saved|savedata|savefiles|uds|存档)", sub.name, re.I):
                    add_candidate(d.name + "/" + sub.name, sub)


def _slug(text):
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-").lower()[:24]


def cmd_scan(args):
    print("扫描本机游戏存档 ...\n")
    roots = user_roots()
    if len(roots) > 1:
        info("检测到 %d 个用户环境: %s"
             % (len(roots), ", ".join(r[0] for r in roots)))

    # 1) 已知游戏（逐个环境检查，包括 CrossOver bottle；按真实路径去重，
    #    bottle 的 Documents 通常是软链到 ~/Documents，避免重复报告）
    print("\n== 已知游戏 ==")
    found_known = []  # (game, label, path)
    for g in KNOWN_GAMES:
        hits = []
        seen_real = set()
        for rel_s in (g.get("rels") or [g["rel"]]):
            rel = Path(*rel_s.split("/"))
            for label, root in roots:
                if g.get("platform") == "mac" and label != "本机":
                    continue
                p = root / rel
                if not p.is_dir():
                    continue
                rp = _real(p)
                if rp in seen_real:
                    continue
                seen_real.add(rp)
                hits.append((label, p))
        if not hits:
            print("  [ -- ] %-16s 未找到" % g["id"])
            continue
        for label, p in hits:
            cnt, size = _dir_stat(p)
            print("  [发现] %-16s %-42s %s（%d个文件, %s）"
                  % (g["id"], g["name"][:42], label, cnt, human_size(size)))
            print("         %s" % p)
            found_known.append((g, label, p))

    if args.add_known and found_known:
        cfg = load_config()
        multi = len({g["id"] for g, _, _ in found_known}) != len(found_known)
        for g, label, p in found_known:
            gid = g["id"]
            name = g["name"]
            if label != "本机" and multi:
                # 同一游戏出现在多个环境（本机 + 多个 bottle），用后缀区分
                gid = "%s-%s" % (g["id"], _slug(label))
                name = "%s @%s" % (g["name"], label)
            if get_game(cfg, gid):
                info("已存在，跳过: " + gid)
            else:
                cfg["games"].append({"id": gid, "name": name, "path": str(p)})
                ok("已加入配置: %s -> %s" % (gid, p))
        save_config(cfg)

    # 2) 通用候选（仅供参考，需手动 add）
    print("\n== 其他疑似存档目录（仅供参考，用 add 命令手动添加）==")
    candidates = []
    for label, root in roots:
        _scan_root_candidates(label, root, candidates)
    if os.name != "nt":
        _scan_mac_native(candidates)
        _scan_game_install_dirs(candidates)
    else:
        _scan_game_install_dirs(candidates)

    # 过滤掉已知游戏已覆盖的路径（含祖先/后代关系）
    known_real = set()
    for g in KNOWN_GAMES:
        for rel_s in (g.get("rels") or [g["rel"]]):
            for _, root in roots:
                known_real.add(_real(root / Path(*rel_s.split("/"))))
    known_real |= {_real(p) for _, _, p in found_known}

    def is_covered(p):
        rp = _real(p)
        for k in known_real:
            if rp == k or rp.startswith(k + os.sep) or k.startswith(rp + os.sep):
                return True
        return False

    shown = []
    for label, kind, p in candidates:
        if is_covered(p):
            continue
        cnt, size = _dir_stat(p)
        if cnt == 0:
            continue
        shown.append((label, kind, p, cnt, size))
    if shown:
        for label, kind, p, cnt, size in shown[:50]:
            print("  [%s] %-34s %6d文件 %8s  %s"
                  % (label, (kind + "/" + p.name)[:34], cnt, human_size(size), p))
        if len(shown) > 50:
            info("（其余 %d 个省略）" % (len(shown) - 50))
    else:
        info("（未发现其他候选目录）")

    # 3) 自动加入同步清单（--add-known 时）：跳过 Mac 原生（Windows 玩不了）
    if args.add_known and shown:
        cfg = load_config()
        existing_real = set()
        for g in cfg["games"]:
            try:
                existing_real.add(_real(resolve_path(g["path"])))
            except Exception:
                pass

        def candidate_name(label, kind, p):
            if kind.startswith("Steam/"):
                return kind.split("/", 1)[1]
            if label == "安装目录" and "/" in kind:
                return kind.split("/")[0]
            return p.name

        added, skipped_mac = 0, 0
        for label, kind, p, cnt, size in shown:
            if label == "Mac原生":
                skipped_mac += 1
                continue
            if _real(p) in existing_real:
                continue
            existing_real.add(_real(p))
            base = _slug(candidate_name(label, kind, p)) or _slug(p.name) or "game"
            gid, n = base, 1
            while get_game(cfg, gid):
                n += 1
                gid = "%s-%d" % (base, n)
            cfg["games"].append({"id": gid, "name": candidate_name(label, kind, p),
                                 "path": str(p)})
            ok("已加入配置: %s -> %s" % (gid, p))
            added += 1
        if added:
            save_config(cfg)
            info("自动加入 %d 个新游戏（不想要的可用 remove 命令移除）" % added)
        if skipped_mac:
            info("跳过 Mac 原生游戏 %d 个（Windows 无法使用）" % skipped_mac)



def cmd_list(args):
    cfg = load_config()
    backend = None
    try:
        backend = make_backend(cfg)
    except SystemExit:
        pass
    except Exception:
        pass
    if not cfg["games"]:
        info("未配置任何游戏")
        return
    st = load_state()
    print("%-18s %-24s %-10s %s" % ("ID", "游戏", "本地", "云端"))
    for g in cfg["games"]:
        p = resolve_path(g["path"])
        local = "存在" if p.is_dir() else "缺失"
        cloud = "未知"
        if backend:
            m = fetch_manifest(backend, g["id"])
            cloud = ("%s (%s)" % (m["time"][:16], m.get("machine", "?"))) if m else "无"
        s = st["games"].get(g["id"], {})
        last = ("上次%s: %s" % (s.get("direction", "?"), s.get("time", "?"))) if s else ""
        print("%-18s %-24s %-10s %s" % (g["id"], g["name"][:24], local, cloud))
        if last:
            print("%-18s %s" % ("", last))


def cmd_add(args):
    p = resolve_path(args.path)
    if not p.is_dir() and not args.force:
        die("路径不存在: %s（确认后可加 --force）" % p)
    cfg = load_config()
    if get_game(cfg, args.id):
        die("ID 已存在: " + args.id)
    cfg["games"].append({"id": args.id, "name": args.name or args.id, "path": args.path})
    save_config(cfg)
    ok("已添加 %s -> %s" % (args.id, p))


def cmd_remove(args):
    cfg = load_config()
    g = get_game(cfg, args.id)
    if not g:
        die("ID 不存在: " + args.id)
    cfg["games"].remove(g)
    save_config(cfg)
    ok("已移除 " + args.id + "（云端数据不受影响）")


def cmd_push(args):
    cfg = load_config()
    backend = make_backend(cfg)
    games = select_games(cfg, args.games)
    if not games:
        info("没有可上传的游戏")
        return
    st = load_state()
    for g in games:
        gid = g["id"]
        path = resolve_path(g["path"])
        print("\n[%s] %s" % (gid, g["name"]))
        if not path.is_dir():
            warn("存档目录不存在，跳过: %s" % path)
            continue
        lh, files, total = hash_dir(path)
        m = fetch_manifest(backend, gid)
        s = st["games"].get(gid, {})

        if m and m.get("hash") == lh:
            info("云端已是相同版本，无需上传")
            s.update({"synced_hash": lh, "time": now_str(), "direction": "push",
                      "machine": hostname()})
            st["games"][gid] = s
            continue

        # 冲突检测：云端版本 != 我上次同步的版本 => 别的机器推过新档
        if m and s.get("synced_hash") and m.get("hash") != s.get("synced_hash"):
            warn("云端有其他机器推送的新版本（%s @ %s），本地直传会覆盖它！"
                 % (m.get("time", "?"), m.get("machine", "?")))
            warn("建议先 pull 保存云端版本，或确认放弃云端版本后用 --force")
            if not args.force:
                info("已跳过 %s" % gid)
                continue

        data, n, zsize = make_zip(path)
        hist_name = "%s_%s.zip" % (ts_tag(), hostname())
        backend.put("%s/history/%s" % (gid, hist_name), data)
        backend.put("%s/latest.zip" % gid, data)
        old_history = m.get("history", []) if m else []
        history = (old_history + [{"name": hist_name, "time": now_str(),
                                   "hash": lh, "machine": hostname()}])[-HISTORY_KEEP:]
        for old in old_history:
            if old not in history:
                backend.delete("%s/history/%s" % (gid, old["name"]))
        new_m = {
            "game": g["name"], "hash": lh, "time": now_str(),
            "machine": hostname(), "files": n, "size": total,
            "zip_size": zsize, "history": history,
        }
        upload_manifest(backend, gid, new_m)
        s.update({"synced_hash": lh, "time": now_str(), "direction": "push",
                  "machine": hostname()})
        st["games"][gid] = s
        ok("已上传 %d 个文件 (%s -> %s)" % (n, human_size(total), human_size(zsize)))
    save_state(st)


def cmd_pull(args):
    cfg = load_config()
    backend = make_backend(cfg)
    games = select_games(cfg, args.games)
    if not games:
        info("没有可下载的游戏")
        return
    st = load_state()
    for g in games:
        gid = g["id"]
        path = resolve_path(g["path"])
        print("\n[%s] %s" % (gid, g["name"]))
        m = fetch_manifest(backend, gid)
        if not m:
            warn("云端没有该游戏的存档，跳过")
            continue
        lh = None
        if path.is_dir():
            lh, _, _ = hash_dir(path)
            if lh == m["hash"]:
                info("本地已是云端版本，无需下载")
                st["games"].setdefault(gid, {}).update(
                    {"synced_hash": lh, "time": now_str(), "direction": "pull",
                     "machine": hostname()})
                continue
            s = st["games"].get(gid, {})
            if s.get("synced_hash") and lh != s.get("synced_hash"):
                warn("本地有未上传的修改！下载会覆盖（覆盖前会自动备份到 %s）"
                     % (BACKUP_DIR / gid))
                if not args.force:
                    warn("如确认覆盖，请加 --force 重试")
                    continue

        info("下载云端版本: %s（来自 %s @ %s）"
             % (m["time"], m.get("machine", "?"), m.get("game", gid)))
        data = backend.get("%s/latest.zip" % gid)
        info("校验压缩包 ...")
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
            bad = zf.testzip()
            if bad is not None:
                raise RuntimeError("压缩包损坏: " + bad)
        except Exception as e:
            warn("压缩包校验失败，已取消: %s" % e)
            continue

        tmp = Path(tempfile.mkdtemp(prefix="savesync_"))
        try:
            safe_extract(zf, tmp)
            # 备份现有存档
            if path.exists():
                bdir = BACKUP_DIR / gid / ts_tag()
                bdir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(bdir / path.name))
                info("本地原存档已备份 -> %s" % (bdir / path.name))
                _prune_backups(gid)
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp), str(path))
            ok("已恢复 %d 个文件到 %s" % (len(zf.namelist()), path))
        except Exception as e:
            warn("恢复失败: %s" % e)
            shutil.rmtree(tmp, ignore_errors=True)
            continue
        st["games"].setdefault(gid, {}).update(
            {"synced_hash": m["hash"], "time": now_str(), "direction": "pull",
             "machine": hostname()})
    save_state(st)


def _prune_backups(gid, keep=BACKUP_KEEP):
    broot = BACKUP_DIR / gid
    if not broot.is_dir():
        return
    snaps = sorted([d for d in broot.iterdir() if d.is_dir()])
    for d in snaps[:-keep]:
        shutil.rmtree(d, ignore_errors=True)


def cmd_status(args):
    cfg = load_config()
    backend = make_backend(cfg, strict=False)
    if not cfg["games"]:
        info("未配置任何游戏，先运行 scan --add-known 扫描添加")
        return
    if backend is None:
        # 未配后端：降级为本地清单视图（GUI「查看存档」在新机器上也可用）
        info("未配置存储后端，仅显示本地清单（配置后端后可对比云端）\n")
        st = load_state()
        for g in cfg["games"]:
            path = resolve_path(g["path"])
            print("[%s] %s" % (g["id"], g["name"]))
            if path.is_dir():
                lh, files, total = hash_dir(path)
                info("  本地: %d个文件 %s" % (files, human_size(total)))
            else:
                info("  本地: 目录不存在")
        return
    st = load_state()
    any_diff = False
    for g in cfg["games"]:
        gid = g["id"]
        path = resolve_path(g["path"])
        print("\n[%s] %s" % (gid, g["name"]))
        m = fetch_manifest(backend, gid)
        if not m:
            info("  云端: 无存档")
        else:
            info("  云端: %s  %s个文件 %s  来自 %s"
                 % (m.get("time"), m.get("files", "?"),
                    human_size(m.get("size", 0)), m.get("machine", "?")))
        if path.is_dir():
            lh, files, total = hash_dir(path)
            info("  本地: %d个文件 %s  hash=%s...%s"
                 % (files, human_size(total), lh[:8], lh[-8:]))
            if m:
                if lh == m["hash"]:
                    ok("  状态: 本地与云端一致")
                else:
                    s = st["games"].get(gid, {})
                    if m["hash"] == s.get("synced_hash"):
                        warn("  状态: 本地有修改未上传 -> 建议 push")
                    else:
                        warn("  状态: 云端有新版本 -> 建议 pull")
                    any_diff = True
        else:
            info("  本地: 存档目录不存在")
            if m:
                warn("  状态: 可 pull 恢复")
    if not any_diff:
        print()
        info("全部同步，无需操作")


def cmd_history(args):
    cfg = load_config()
    backend = make_backend(cfg)
    g = get_game(cfg, args.id)
    if not g:
        die("ID 不存在: " + args.id)
    m = fetch_manifest(backend, args.id)
    if not m:
        info("云端没有该游戏的存档")
        return
    print("%s 历史版本（云端保留 %d 份）:" % (g["name"], HISTORY_KEEP))
    for h in m.get("history", []):
        print("  %s  %s  %s" % (h.get("time", "?"), h.get("machine", "?"), h.get("name")))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        prog="savesync",
        description="游戏存档云同步工具 v%s（WebDAV / S3兼容 / 本地目录）" % VERSION)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("setup-webdav", help="配置 WebDAV 后端（推荐坚果云）")
    p.add_argument("--url", required=True,
                   help="WebDAV 服务器地址，如 https://dav.jianguoyun.com/dav")
    p.add_argument("--user", required=True, help="账号（坚果云为邮箱）")
    p.add_argument("--password", required=True, help="密码（坚果云为应用密码）")
    p.add_argument("--root", default="savesync", help="云端根目录名，默认 savesync")
    p.set_defaults(fn=cmd_setup_webdav)

    p = sub.add_parser("setup-s3", help="配置 S3 兼容后端（腾讯云COS/阿里云OSS）")
    p.add_argument("--bucket", required=True)
    p.add_argument("--endpoint", required=True,
                   help="如 cos.ap-chengdu.myqcloud.com 或 oss-cn-chengdu.aliyuncs.com")
    p.add_argument("--region", required=True, help="如 ap-chengdu")
    p.add_argument("--ak", required=True, help="AccessKey/SecretId")
    p.add_argument("--sk", required=True, help="SecretKey")
    p.add_argument("--service", default="s3", help="签名服务名，默认 s3")
    p.set_defaults(fn=cmd_setup_s3)

    p = sub.add_parser("setup-local", help="配置本地目录后端（NAS/U盘）")
    p.add_argument("--dir", required=True)
    p.set_defaults(fn=cmd_setup_local)

    p = sub.add_parser("doctor", help="检查配置与后端连通性")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("scan", help="扫描本机游戏存档（含 macOS CrossOver bottle）")
    p.add_argument("--add-known", action="store_true", help="自动添加发现的已知游戏")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("list", help="列出已配置的游戏")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("add", help="添加游戏存档目录")
    p.add_argument("id", help="游戏 ID（英文，简短）")
    p.add_argument("--name", help="游戏显示名")
    p.add_argument("--path", required=True, help="存档目录路径（支持 %USERPROFILE%）")
    p.add_argument("--force", action="store_true", help="路径不存在也强制添加")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("remove", help="移除某个游戏")
    p.add_argument("id")
    p.set_defaults(fn=cmd_remove)

    p = sub.add_parser("push", help="上传存档到云端")
    p.add_argument("games", nargs="*", help="游戏 ID，留空=全部")
    p.add_argument("--force", action="store_true", help="覆盖云端其他机器的新版本")
    p.set_defaults(fn=cmd_push)

    p = sub.add_parser("pull", help="从云端下载存档到本机（覆盖前自动备份）")
    p.add_argument("games", nargs="*", help="游戏 ID，留空=全部")
    p.add_argument("--force", action="store_true", help="本地有未上传修改时仍然覆盖")
    p.set_defaults(fn=cmd_pull)

    p = sub.add_parser("status", help="对比本地与云端状态")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("history", help="查看某游戏的云端历史版本")
    p.add_argument("id")
    p.set_defaults(fn=cmd_history)

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        sys.exit(0)
    try:
        args.fn(args)
    except KeyboardInterrupt:
        print()
        warn("已取消")
    except (RuntimeError, FileNotFoundError) as e:
        die(str(e))


def write_config_backend(btype, params):
    """GUI 用：写入后端配置（不测试，测试由调用方自行进行）。"""
    cfg = load_config()
    cfg["backend"] = dict(params, type=btype)
    save_config(cfg)


def run_cli(argv):
    """GUI 用：等价于命令行执行 python savesync.py <argv...>。"""
    old_argv = sys.argv
    sys.argv = [old_argv[0]] + [str(a) for a in argv if a]
    try:
        main()
    finally:
        sys.argv = old_argv


if __name__ == "__main__":
    main()
