#!/usr/bin/env python3
"""
equasar_server.py — Кроссплатформенный локальный сервер eQuasar (Linux / Windows / macOS)
Имитирует сервер активации Яндекса, отдавая Станции Лайт команду на включение постоянного ADB
или манифест даунгрейда на уязвимую прошивку.
Работает на портах 80 (HTTP) и 443 (HTTPS) с встроенными сертификатами eMudhra.
"""

import http.server
import ssl
import sys
import os
import threading
import socketserver
import argparse
import socket
import http.client
import json
import time
import urllib.request
import urllib.parse

# Windows-консоль по умолчанию cp1251 — принудительно UTF-8, иначе падаем на «←»/«→»
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

UPSTREAM_HOST = "quasar.yandex.net"


def real_ip(host=UPSTREAM_HOST):
    """Настоящий IP: DoH -> системный DNS -> известный адрес (свой IP не отдаём)."""
    mine = set(local_ips()) if "local_ips" in globals() else set()
    try:
        u = "https://dns.google/resolve?name=%s&type=A" % host
        d = json.loads(urllib.request.urlopen(u, timeout=5).read())
        for a in d.get("Answer", []):
            ip = a.get("data")
            if a.get("type") == 1 and ip and ip not in mine:
                return ip
    except Exception:
        pass
    try:
        ip = socket.gethostbyname(host)
        if ip and ip not in mine:
            return ip
    except Exception:
        pass
    return "213.180.193.230"


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, ip, **kw):
        super().__init__(host, **kw)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port or 443), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)

SYNC_INFO_PAYLOAD = b'{"status":"ok","config":{"account_config":{"spotter":"alisa"},"device_config":{"name":"quasar"},"system_config":{"adbd":{"remoteActivation":true,"network":true,"fullDurationMs":31536000000}},"adbd":{"remoteActivation":true,"network":true,"fullDurationMs":31536000000}},"subscription":{"mode":"transaction"}}'

NO_UPDATE_PAYLOAD = b'{"hasUpdate":false}'

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _local_ip():
    """IP этой машины в локалке — чтобы колонка могла скачать прошивку с нас."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _find_ota():
    """Ищем образ прошивки и метаданные в firmware/ (модель может быть любой)."""
    import glob
    d = os.path.join(BASE_DIR, "firmware")
    zips = sorted(glob.glob(os.path.join(d, "*.zip")))
    meta = {}
    try:
        meta = json.load(open(os.path.join(d, "manifest.json"), encoding="utf-8"))
    except Exception:
        pass
    if not zips:
        return None, meta
    # если в манифесте указан filename — берём его, иначе самый свежий по имени
    want = meta.get("filename")
    path = os.path.join(d, want) if want and os.path.exists(os.path.join(d, want)) else zips[-1]
    return path, meta


OTA_FILE, OTA_META = _find_ota()
_OTA_NAME = os.path.basename(OTA_FILE) if OTA_FILE else ""


def _guess_platform(name):
    """Платформа по имени файла прошивки.

    0.x  -> yandexmicro (Лайт 1)
    1.3x -> yandexmini  (Мини 1)
    иначе 1.x -> yandexstation
    """
    b = os.path.basename(name or "")
    if b.startswith("0."):
        return "yandexmicro"
    if b.startswith("1.3"):
        return "yandexmini"
    return "yandexstation"


MIRROR_URL = os.environ.get("MIRROR_URL", "http://144.76.194.29:62891").rstrip("/")
_PLAT_CACHE = {}


def mirror_platform(version):
    """К какой платформе на зеркале относится эта версия (для «просто zip» без папки)."""
    if not version:
        return None
    if version in _PLAT_CACHE:
        return _PLAT_CACHE[version]
    try:
        import urllib.request
        root = urllib.request.urlopen(MIRROR_URL + "/", timeout=8).read().decode("utf-8", "replace")
        import re as _re
        plats = _re.findall(r'href="([^"/]+)/"', root)
        for plat in plats:
            url = "%s/%s/%s/metadata.json" % (MIRROR_URL, plat, version)
            try:
                with urllib.request.urlopen(url, timeout=6) as r:
                    if r.status == 200:
                        _PLAT_CACHE[version] = plat
                        print(f"  >>> [OTA] версия {version} на зеркале относится к {plat} <<<")
                        return plat
            except Exception:
                continue
    except Exception as e:
        print(f"  !!! [OTA] не смог проверить зеркало для {version}: {e!r}")
    _PLAT_CACHE[version] = None
    return None


def fw_manifest(platform):
    """Прошивка для платформы из firmware/<platform>/<версия>.zip (+ manifest.json рядом)."""
    if not platform:
        return None
    import glob
    d = os.path.join(BASE_DIR, "firmware", platform)
    meta = {}
    try:
        meta = json.load(open(os.path.join(d, "manifest.json"), encoding="utf-8"))
    except Exception:
        pass
    path = None
    name = meta.get("filename")
    if name and os.path.exists(os.path.join(d, name)):
        path = os.path.join(d, name)
    else:
        zips = sorted(glob.glob(os.path.join(d, "*.zip")))
        if zips:
            path = zips[-1]
    if not path:
        return None
    return {
        "path": path,
        "name": os.path.basename(path),
        "version": os.path.basename(path)[:-4],
        "platform": meta.get("platform") or platform,
        "crc32": meta.get("crc32"),
    }


_CRC_CACHE = {}


def fw_crc(fw):
    import zlib
    p = fw["path"]
    if p in _CRC_CACHE:
        return _CRC_CACHE[p]
    crc = 0
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            crc = zlib.crc32(chunk, crc)
    _CRC_CACHE[p] = crc & 0xFFFFFFFF
    return _CRC_CACHE[p]


def ota_platform():
    """Платформа прошивки: из firmware/manifest.json (его пишет install-firmware),
    иначе из имени файла. По версии модель НЕ определить: у mini_2, midi, plum тоже 0.x."""
    try:
        m = json.load(open(os.path.join(BASE_DIR, "firmware", "manifest.json"), encoding="utf-8"))
        if m.get("platform"):
            return m["platform"]
    except Exception:
        pass
    return _guess_platform(OTA_FILE) if OTA_FILE else ""


# версия и модель берутся ИЗ ИМЕНИ ФАЙЛА — манифест в firmware/ может быть устаревшим
OTA_VERSION = _OTA_NAME[:-4] if _OTA_NAME.endswith(".zip") else _OTA_NAME
OTA_PLATFORM = _guess_platform(_OTA_NAME)
OTA_PREFIX = "/%s/ota/" % OTA_PLATFORM
_OTA_CRC = {"v": None}


def _ota_crc():
    """CRC32 реального файла (с кэшем). Манифесту не доверяем — он часто не совпадает."""
    if _OTA_CRC["v"] is not None:
        return _OTA_CRC["v"]
    if not OTA_FILE or not os.path.exists(OTA_FILE):
        return 0
    import zlib
    crc = 0
    with open(OTA_FILE, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            crc = zlib.crc32(chunk, crc)
    _OTA_CRC["v"] = crc & 0xFFFFFFFF
    return _OTA_CRC["v"]


def _downgrade_payload(fw=None):
    if fw:
        name, ver = fw["name"], fw["version"]
        pfx = "/%s/ota/" % (fw.get("platform") or OTA_PLATFORM)
        crc = fw["crc32"] if fw.get("crc32") is not None else fw_crc(fw)
    else:
        name, ver = (os.path.basename(OTA_FILE) if OTA_FILE else ""), OTA_VERSION
        pfx = OTA_PREFIX
        crc = _ota_crc()
    return json.dumps({
        "hasUpdate": True,
        "version": ver,
        "downloadUrl": "https://quasar.yandex.net" + pfx + name,
        "crc32": crc,
        "critical": True,
        "fallbackUrl": "https://quasar.yandex.net" + pfx + name,
        "fallbackIp": LOCAL_IP,
        "updateId": "150484",
    }).encode()


# --- опциональные настройки через окружение (все необязательные) ---
LOCAL_IP = os.environ.get("LOCAL_IP") or _local_ip()
COLUMN_IPS = {ip.strip() for ip in os.environ.get("COLUMN_IPS", "").split(",") if ip.strip()}
COLUMN_ID = os.environ.get("COLUMN_ID", "LP00000000000001982300007d20fe15")

MODE_DOWNGRADE = False
BUILD = "V7-LOCAL-SYNC"
MODE_PASSTHROUGH = False   # всё прозрачно в настоящий Яндекс (диагностика)
MODE_OTA_ONLY = False      # только прошивка: /check_updates + zip, остальное — в Яндекс (Mini)
SYNC_LOCAL = os.environ.get("SYNC_LOCAL") == "1"   # аварийно: отдавать локальный шаблон вместо Яндекса
# повторы при 429/5xx: сколько всего попыток и пауза между ними
RETRY_ATTEMPTS = max(1, int(os.environ.get("SYNC_RETRIES", "3")))
RETRY_DELAY = max(0.0, float(os.environ.get("SYNC_RETRY_DELAY", "2.5")))
UPSTREAM_TIMEOUT = max(2.0, float(os.environ.get("UPSTREAM_TIMEOUT", "8")))


SYNC_DUMP = os.path.join(BASE_DIR, "log", "sync_real.json")
SYNC_TEMPLATE = os.path.join(BASE_DIR, "log", "sync_template.json")
# базовый шаблон — «обычные» станции, он лежит в репозитории и не зависит от Mini
SYNC_BASE = os.path.join(BASE_DIR, "scripts", "sync_template.json")
ADBD = {"remoteActivation": True, "network": True, "fullDurationMs": 31536000000}
# Android TV / YaOS (Станция Макс, Дуо Макс): ADB включается не через quasar-ключ
# system_config.adbd, а через настройки приложения com.yandex.tv.services.platform.
# Оно пишет Settings.Global.ADB_ENABLED / DEVELOPMENT_SETTINGS_ENABLED.
TV_PLATFORM = {"adbEnabled": True, "developmentSettingsEnabled": True}
# У yandexstation_2 (Макс) и yandexstation (Станция 1, Android) в quasar.cfg НЕТ
# quasmodromNetworkActivateCmd. Если отдать network=true, AdbService выберет пустую
# команду и ничего не запустит. У этих платформ activate_adb.sh и без --network
# прописывает service.adb.tcp.port=5555, поэтому network нужно выключать.
ADB_NO_NETWORK_PLATFORMS = {"yandexstation_2", "yandexstation"}

_TEMPLATE = {"loaded": False, "data": None}


def _sanitize(d):
    """Убираем из шаблона секреты и привязку к конкретному устройству."""
    if not isinstance(d, dict):
        return d
    d.pop("glagol", None)                       # там локальные серт+ключ
    cfg = d.get("config")
    if isinstance(cfg, dict):
        acc = cfg.get("account_config")
        if isinstance(acc, dict):
            acc.pop("user_wifi_config", None)   # хеш Wi-Fi пользователя
        dev = cfg.get("device_config")
        if isinstance(dev, dict):
            dev.pop("location", None)           # координаты пользователя
    return d


def _load_template():
    """Шаблон: базовый (Lite) -> сгенерированный; дамп Mini — только по USE_SYNC_DUMP=1."""
    paths = [SYNC_BASE, SYNC_TEMPLATE]
    if os.environ.get("USE_SYNC_DUMP") == "1":
        paths.append(SYNC_DUMP)
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            d = _sanitize(json.load(open(path, encoding="utf-8")))
        except Exception:
            continue
        return d
    return None


def _native_adb_state(d):
    """ADB-поля в ответе Яндекса ДО нашей инъекции — для сравнения моделей."""
    cfg = d.get("config") or {}
    syscfg = cfg.get("system_config") or {}
    dev = cfg.get("device_config") or {}
    return {
        "system.adbd": syscfg.get("adbd"),
        "device.adbd": dev.get("adbd"),
        "top.adbd": d.get("adbd"),
        "tv.platform": syscfg.get("com.yandex.tv.services.platform"),
    }


def _inject_tv_platform(section):
    """Android TV / YaOS: com.yandex.tv.services.platform.adbEnabled.

    На Максе (yandexstation_2) нет quasar-демона, который читает
    system_config.adbd. Настройку ADB обрабатывает системное приложение
    YandexTvPlatformServices: оно ставит Settings.Global.ADB_ENABLED=1.
    В /vendor/build.prop у Макса уже прописан service.adb.tcp.port=5555,
    поэтому после этого adbd начинает слушать сетевой порт.
    """
    if not isinstance(section, dict):
        return
    current = section.get("com.yandex.tv.services.platform")
    tv = dict(current) if isinstance(current, dict) else {}
    tv.update(TV_PLATFORM)          # не затираем другие флаги платформы
    section["com.yandex.tv.services.platform"] = tv


def _adbd_config(platform=None):
    """ADB-флаги с учётом платформы (у Макса нет network-команды в quasar.cfg)."""
    d = dict(ADBD)
    if (platform or "").lower() in ADB_NO_NETWORK_PLATFORMS:
        d["network"] = False
    return d


def _inject_adbd(d, platform=None):
    """Вписывает ADB во все ветки, которые читают разные прошивки."""
    adbd = _adbd_config(platform)
    cfg = d.setdefault("config", {})
    for section in ("system_config", "device_config"):
        cfg.setdefault(section, {})["adbd"] = dict(adbd)
    cfg["adbd"] = dict(adbd)
    d["adbd"] = dict(adbd)
    # Android TV (Макс/Дуо Макс): отдельный ключ, Buildroot-моделям он не мешает.
    _inject_tv_platform(cfg.setdefault("system_config", {}))
    _inject_tv_platform(cfg)
    d.setdefault("status", "ok")
    return d


def sync_payload(source=None, platform=None):
    """Ответ /get_sync_info со включённым ADB.

    platform — платформа колонки из query (`yandexstation_2`, `yandexmini`, …).
    source — готовый dict (например, настоящий ответ Яндекса); иначе шаблон.
    """
    if source is not None:
        d = json.loads(json.dumps(source))       # копия, оригинал не портим
        return json.dumps(_inject_adbd(d, platform), ensure_ascii=False).encode()
    if not _TEMPLATE["loaded"]:
        _TEMPLATE["data"] = _load_template()
        _TEMPLATE["loaded"] = True
    base = _TEMPLATE["data"]
    if base is None:
        d = json.loads(SYNC_INFO_PAYLOAD.decode())
    else:
        d = json.loads(json.dumps(base))        # копия
    return json.dumps(_inject_adbd(d, platform), ensure_ascii=False).encode()


OK_PAYLOAD = b'{"status":"ok"}'
# 429/5xx Яндекса колонке не отдаём вообще: она считает это провалом активации.
UPSTREAM_SOFT_ERRORS = (429,)


def no_downgrade():
    """Аварийный выключатель авто-раздачи прошивки: touch run/no_downgrade."""
    return os.path.exists(os.path.join(BASE_DIR, "run", "no_downgrade"))


def is_sync_path(path):
    """Sync-подобный запрос: /get_sync_info, /sync, ... — отвечаем локально."""
    return "sync" in (path or "").lower()


def upstream_soft_error(status):
    """Ответы, которые нельзя пробрасывать колонке (лимиты и перегрузка Яндекса)."""
    return status in UPSTREAM_SOFT_ERRORS or status >= 500


# ключи, значения которых не должны попадать в лог
SECRET_MARKERS = ("token", "secret", "password", "private", "authorization",
                  "cookie", "wifi_hash", "latitude", "longitude", "certificate")


def _masked(obj):
    if isinstance(obj, dict):
        return {k: ("<скрыто>" if any(m in str(k).lower() for m in SECRET_MARKERS)
                    else _masked(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_masked(x) for x in obj]
    return obj


def _masked_json(payload):
    """JSON для лога: секреты/координаты заменены на <скрыто>."""
    try:
        return json.dumps(_masked(json.loads(payload)), ensure_ascii=False)
    except Exception:
        return payload[:1000].decode("utf-8", "replace")


CACHE_DIR = os.path.join(BASE_DIR, "log", "cache")
LOG_DIR = os.path.join(BASE_DIR, "log")
_LOG_LOCK = threading.Lock()


_last_log = {}
_LOG_DEDUP_SEC = 5


def _log_to(name, msg):
    """Пишет строку в log/<name> (yandex.log, local.log, dns.log)."""
    key = (name, msg)
    now = time.time()
    prev = _last_log.get(key, 0)
    if now - prev < _LOG_DEDUP_SEC:
        return
    _last_log[key] = now
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with _LOG_LOCK:
            with open(os.path.join(LOG_DIR, name), "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def log_yandex(msg):
    _log_to("yandex.log", msg)


def log_local(msg):
    _log_to("local.log", msg)


def log_dns(msg):
    _log_to("dns.log", msg)


def _dns_name(qname):
    """Имя из DNS-запроса для лога (idna-кодек не принимает errors=...)."""
    try:
        return (qname or b"?").decode("ascii", "replace")
    except Exception:
        return "?"


def _cache_file(path_qs):
    import hashlib
    return os.path.join(CACHE_DIR, hashlib.sha1(path_qs.encode()).hexdigest() + ".cache")


class EquasarHandler(http.server.BaseHTTPRequestHandler):
    # старые прошивки (Mini 2019) ждут HTTP/1.1, на HTTP/1.0 считают проверку связи неудачной
    protocol_version = "HTTP/1.1"
    def log_message(self, format, *args):
        client = self.client_address[0]
        ua = self.headers.get("User-Agent", "-")
        print(f"[{client}] {self.command} {self.path} UA={ua} -> {args[1] if len(args) > 1 else 'OK'}")
        sys.stdout.flush()

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def do_HEAD(self):
        # оказывается, алиска через этот эндпоинт не только проверяет связь, но и ставит часы
        if self.path.split("?")[0] == "/generate_204":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.send_header("X-Start-Time", str(time.time_ns() // 1000))
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()

    def _drain_body(self):
        self._read_body()

    def _read_body(self):
        """Читает тело запроса один раз. Поддерживает Content-Length и chunked."""
        if getattr(self, "_body", None) is None:
            body = b""
            try:
                if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                    while True:
                        line = self.rfile.readline(65536).strip()
                        if not line:
                            break
                        try:
                            size = int(line.split(b";")[0], 16)
                        except ValueError:
                            break
                        if size == 0:
                            self.rfile.readline(65536)      # трейлер
                            break
                        body += self.rfile.read(size)
                        self.rfile.read(2)                  # CRLF после чанка
                else:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(n) if n > 0 else b""
            except (ValueError, OSError):
                body = b""
            self._body = body
        return self._body

    def _send_json(self, payload, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _request_platform(self):
        """platform= из query (yandexstation_2, yandexmini, …) или пусто."""
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return (q.get("platform") or [""])[0]
        except Exception:
            return ""

    def _log_sync_request(self):
        """Что именно спросила колонка: путь, заголовки, тело (секреты маскируются)."""
        body = self._read_body() if self.command in ("POST", "PUT") else b""
        hdrs = "; ".join(
            f"{k}: {'<скрыто>' if any(m in k.lower() for m in SECRET_MARKERS) else v}"
            for k, v in self.headers.items()
            if k.lower() not in ("host", "connection", "content-length"))
        print(f"  <<< [SYNC←] {self.command} {self.path} от {self.client_address[0]}")
        print(f"      заголовки: {hdrs[:800]}")
        if body:
            print(f"      тело {len(body)}b: {_masked_json(body)}")
        sys.stdout.flush()

    def _log_payload(self, tag, payload, source):
        """Что именно уходит колонке: размер, состояние adbd и сам JSON (без секретов)."""
        status = "нет данных"
        try:
            d = json.loads(payload)
            cfg = d.get("config") or {}
            syscfg = cfg.get("system_config") or {}
            tv = syscfg.get("com.yandex.tv.services.platform") or {}
            adb = syscfg.get("adbd") if isinstance(syscfg.get("adbd"), dict) else {}
            marks = {
                "config.system_config.adbd": syscfg.get("adbd"),
                "config.system_config.adbd.network": adb.get("network"),
                "config.device_config.adbd": (cfg.get("device_config") or {}).get("adbd"),
                "config.adbd": cfg.get("adbd"),
                "top.adbd": d.get("adbd"),
                "tv.adbEnabled": tv.get("adbEnabled"),
                "tv.developmentSettingsEnabled": tv.get("developmentSettingsEnabled"),
            }
            status = ", ".join(f"{k}={'on' if v else 'НЕТ'}" for k, v in marks.items())
            text = _masked_json(payload)
        except Exception:
            text = payload[:1000].decode("utf-8", "replace")
        print(f"  >>> [{tag}] payload {len(payload)}b, источник: {source}, adbd: {status}")
        print(f"      {text[:4000]}{' …(обрезано)' if len(text) > 4000 else ''}")
        sys.stdout.flush()

    def _send_local_fallback(self, kind, why):
        """Апстрим ответил 429/5xx — колонке всё равно отдаём 200.

        kind="sync" — полный конфиг с adbd.remoteActivation=true,
        иначе — короткий {"status":"ok"}, чтобы активация не срывалась.
        """
        if kind == "register":
            payload = b'{"message":"allvalid","status":"ok"}'   # чтобы колонка не долбила /register
        elif kind == "sync":
            payload = sync_payload(platform=self._request_platform())
        else:
            payload = OK_PAYLOAD
        self._send_json(payload)
        log_local(f"FALLBACK {self.path}: {why}")
        if kind in ("register",):
            try:
                log_local(f"FALLBACK-BODY {self.path.split('?')[0]}: {payload.decode('utf-8')}")
            except Exception:
                pass
        _what = {"sync": "sync + adbd:true", "register": "allvalid", "ok": "ok"}.get(kind, "ok")
        print(f"  >>> [FALLBACK] {why} — отдаю локально 200 ({_what}) <<<")
        if kind == "sync":
            self._log_payload("SYNC→", payload, "локальный шаблон (fallback)")

    def _upstream(self, host, ip, body, hdrs):
        """Запрос в Яндекс с повторами на 429/5xx: 3 попытки, пауза 2.5с по умолчанию."""
        last = (None, b"")
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            conn = PinnedHTTPS(host, ip, timeout=UPSTREAM_TIMEOUT)
            conn.request(self.command, self.path, body=body, headers=hdrs)
            r = conn.getresponse()
            data = r.read()
            if not upstream_soft_error(r.status):
                return r, data
            last = (r, data)
            if attempt < RETRY_ATTEMPTS:
                print(f"  ... [RETRY] upstream {r.status} на {self.path.split('?')[0]}, "
                      f"попытка {attempt}/{RETRY_ATTEMPTS}, пауза {RETRY_DELAY:g}с")
                sys.stdout.flush()
                time.sleep(RETRY_DELAY)
        print(f"  ... [RETRY] upstream {last[0].status if last[0] else '-'} после "
              f"{RETRY_ATTEMPTS} попыток — сдаюсь, отдаю локально")
        sys.stdout.flush()
        return last

    def handle_request(self):
        clean_path = self.path.split("?")[0]
        if is_sync_path(clean_path) and not SYNC_LOCAL:
            # реальный ответ Яндекса (родные поля устройства) + adbd; при 429/5xx — шаблон
            self.serve_sync_real(dump_path=os.path.join(BASE_DIR, "log", "sync_real.json"))
            return
        if MODE_PASSTHROUGH:
            # режим «сквозь»: ничего не подменяем, просто проксируем в настоящий Яндекс
            self.proxy_upstream()
            return
        if MODE_OTA_ONLY:
            # только доставка прошивки: манифест и сам zip отдаём мы, всё прочее — в Яндекс
            if clean_path.endswith(".zip") or clean_path.startswith(OTA_PREFIX):
                self.serve_ota()
                return
            if clean_path.startswith("/check_updates") or clean_path.startswith("/allow_update"):
                # отдаём манифест всем, кто спросил про обновление (это безопасно: только прошивка)
                payload = _downgrade_payload()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                print("  >>> [OTA-ONLY] выдан манифест прошивки <<<")
                return
            self.proxy_upstream()
            return
        # подменяем ответы ТОЛЬКО самой колонке; телефон/приложение идут в настоящий Яндекс
        ua = self.headers.get("User-Agent", "")
        ua_l = ua.lower()
        path_l = self.path.lower()
        is_column = (
            (COLUMN_ID in self.path)
            or (self.client_address[0] in COLUMN_IPS)
            or any(k in ua_l for k in ("yandexstation", "yandexmicro", "yandexmini", "yandexmidi"))
            # станция иногда приходит без User-Agent — узнаём по характерным параметрам
            or ("platform=yandex" in path_l)
            or (path_l.startswith("/get_sync_info") and "device_id=" in path_l)
            or (path_l.startswith("/check_updates") and "device_id=" in path_l)
        )


        # --- то, что не наше: кэшируем, чтобы 429 от Яндекса не долетал до колонки ---
        if path_l.startswith("/glagol/") or path_l.startswith("/telemetry") or path_l.startswith("/metrics"):
            self.serve_cached()
            return

        # --- раздача OTA со своего диска (кусками, с Range) ---
        m_zip = None
        if clean_path.endswith(".zip"):
            import re as _re
            m_zip = _re.match(r"^/([^/]+)/ota/(.+)$", clean_path)
        if m_zip:
            plat, name = m_zip.group(1), m_zip.group(2)
            if MODE_OTA_ONLY or not no_downgrade():
                if self.serve_ota(plat, name):
                    return
            print("  >>> [OTA] нет файла или run/no_downgrade — 404 <<<")
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        
        if clean_path in ("/", "/favicon.ico"):
            # это браузер открыл адрес стенда — в Яндекс такое гонять незачем
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            log_local(f"GET {clean_path} от {self.client_address[0]} -> 404 (браузерный шум)")
            return

        if clean_path == "/generate_204":
            log_local(f"GET /generate_204 от {self.client_address[0]} -> 204")
            self.send_response(204)
            self.send_header("X-Start-Time", str(time.time_ns() // 1000))
            self.end_headers()
            return
            
        # локальных заглушек больше нет: register / capability / update_state идут в Яндекс
        if "get_sync_info" in clean_path.lower():
            self._log_sync_request()
            try:
                _p = sync_payload(platform=self._request_platform())
            except Exception as e:
                print(f"  !!! [SYNC] шаблон сломан ({e!r}), отдаю встроенный")
                _p = SYNC_INFO_PAYLOAD
            self._send_json(_p)
            print("  >>> [SUCCESS] Колонке отправлена команда активации ADB (локальный режим) <<<")
            log_local(f"SYNC (SYNC_LOCAL=1) {self.path}")
            self._log_payload("SYNC→", _p, "локальный шаблон")
            return
            
        # любые варианты пути (в т.ч. /quasar/check_updates, /check_updates/v2) — не только точный
        if ("check_updates" in clean_path.lower() or "allow_update" in clean_path.lower()) and is_column:
            # Предлагаем обновление ВСЕГДА, пока колонка не на этой версии.
            # Если попытка не удалась — она осталась на старой версии и получит предложение снова.
            cur = ""
            plat = ""
            try:
                import urllib.parse as _u
                q = _u.parse_qs(_u.urlparse(self.path).query)
                cur = (q.get("version") or [""])[0]
                plat = (q.get("platform") or [""])[0]
            except Exception:
                pass
            fw = fw_manifest(plat) if plat else None
            if not fw and OTA_FILE:
                # старая плоская раскладка firmware/<версия>.zip
                legacy_plat = ota_platform()
                # если в манифесте нет платформы — по версии её не определить, спрашиваем зеркало
                flat = True
                try:
                    _m = json.load(open(os.path.join(BASE_DIR, "firmware", "manifest.json"), encoding="utf-8"))
                    flat = not _m.get("platform")
                except Exception:
                    flat = True
                if flat:
                    legacy_plat = mirror_platform(OTA_VERSION) or legacy_plat
                if not plat or plat == legacy_plat:
                    fw = {"path": OTA_FILE, "name": os.path.basename(OTA_FILE),
                          "version": OTA_VERSION, "platform": legacy_plat}
            if plat and fw and plat != (fw.get("platform") or plat):
                print(f"  >>> [OTA] платформа колонки {plat} != прошивки {fw.get('platform')} — прошивку не даю <<<")
                fw = None
            _ver = (fw or {}).get("version", OTA_VERSION)
            already = bool(cur) and (cur.startswith(_ver) or _ver.startswith(cur))
            if fw and not no_downgrade() and not already:
                # авто: в firmware/ лежит другая версия -> предлагаем её (единственный локальный ответ)
                payload = _downgrade_payload(fw)
                print(f"  >>> [OTA-AUTO] у колонки {cur or '?'}, у нас {_ver} — "
                      f"отдаю манифест прошивки <<<")
                log_local(f"OTA-AUTO {self.path} -> манифест {_ver} ({fw.get('platform')})")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            # прошивки нет (или версия совпала, или run/no_downgrade) — обновлений не предлагаем
            payload = NO_UPDATE_PAYLOAD
            print("  >>> [OTA] прошивки нет для этой модели — hasUpdate:false <<<")
            log_local(f"OTA {self.path} -> hasUpdate:false")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
            
        if "register" in path_l:
            self.register_passthrough()
            return

        # Всё остальное (/register, /get_config, ...) — пробрасываем в настоящий Яндекс,
        # иначе колонка получает "OK" вместо реального ответа и ругается "что-то не так".
        # Но 429/5xx от Яндекса колонке не отдаём: она принимает это за провал синка.
        if "/update_device_state" in clean_path or "/set_device_capability" in clean_path:
            _fb = "ok"
        else:
            _fb = "sync" if is_sync_path(self.path) else ("register" if "register" in path_l else "ok")
        self.proxy_upstream(fallback=_fb)

    def register_passthrough(self):
        """Отправляем /register в Яндекс, но колонке ВСЕГДА отвечаем allvalid."""
        host = (self.headers.get("Host") or UPSTREAM_HOST).split(":")[0]
        ip = real_ip(host)
        try:
            body = self._read_body() if self.command in ("POST", "PUT") else None
            skip = ("host", "connection", "content-length", "accept-encoding", "transfer-encoding")
            hdrs = {k: v for k, v in self.headers.items() if k.lower() not in skip}
            r, data = self._upstream(host, ip, body, hdrs)
            log_yandex(f"REGISTER {self.command} {self.path} -> {r.status} ({len(data)}b)")
            print(f"  >>> [REGISTER] Яндекс: {r.status} ({len(data)}b) — колонке отдаю allvalid <<<")
        except Exception as e:
            log_yandex(f"REGISTER ERROR {self.path}: {e!r}")
            print(f"  !!! [REGISTER] Яндекс недоступен ({e!r}) — колонке отдаю allvalid")
        payload = b'{"message":"allvalid","status":"ok"}'
        self._send_json(payload)
        log_local(f"REGISTER allvalid {self.path}")
        print("  >>> [REGISTER] отдано allvalid <<<")

    def serve_ota(self, platform=None, name=None):
        """Отдача OTA из firmware/<platform>/<name> с Range. Возвращает True, если отдал."""
        import os
        path = None
        if platform and name:
            cand = os.path.join(BASE_DIR, "firmware", platform, name)
            if os.path.exists(cand):
                path = cand
        if not path:
            fw = fw_manifest(platform) if platform else None
            if not fw and OTA_FILE and os.path.exists(OTA_FILE):
                fw = {"path": OTA_FILE}
            if not fw:
                return False
            path = fw["path"]
        size = os.path.getsize(path)
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        code = 200
        if rng and rng.startswith("bytes="):
            try:
                a, _, b = rng[6:].partition("-")
                if a:
                    start = int(a)
                if b:
                    end = int(b)
                code = 206
            except Exception:
                start, end, code = 0, size - 1, 200
        length = max(0, end - start + 1)
        self.send_response(code)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if self.command == "HEAD":
            return True
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(65536, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except Exception:
                    return True
                left -= len(chunk)
        if start == 0 and end == size - 1:
            print("  >>> [OTA] файл отдан ПОЛНОСТЬЮ (%d б) <<<" % size)
        print(f"  >>> [OTA] отдано {length}б (bytes {start}-{end}/{size}) <<<")
        log_local(f"OTA {self.path} -> {code} {length}b")
        sys.stdout.flush()
        return True

    def serve_sync_real(self, dump_path=None):
        """Настоящий ответ Яндекса + вписанный ADB.

        Ответ отдаётся как есть (со всеми полями устройства), меняется/добавляется
        только adbd. Любая проблема (429/5xx/не JSON/обрыв) — локальный шаблон с ADB.
        Сырой ответ пишется в dump_path только при DUMP_SYNC=1.
        """
        host = (self.headers.get("Host") or UPSTREAM_HOST).split(":")[0]
        ip = real_ip(host)
        self._log_sync_request()
        try:
            body = self._read_body() if self.command in ("POST", "PUT") else None
            skip = ("host", "connection", "content-length", "accept-encoding", "transfer-encoding")
            hdrs = {k: v for k, v in self.headers.items() if k.lower() not in skip}
            r, data = self._upstream(host, ip, body, hdrs)
            log_yandex(f"SYNC {self.command} {self.path} -> {r.status} ({len(data)}b)")
            if r.status == 200:
                try:
                    d = json.loads(data)
                except (ValueError, UnicodeDecodeError):
                    d = None
                if isinstance(d, dict) and d:
                    native = _native_adb_state(d)
                    print(f"  >>> [SYNC-NATIVE] platform={self._request_platform() or '?'} "
                          f"system.adbd={native['system.adbd']} "
                          f"device.adbd={native['device.adbd']} "
                          f"top.adbd={native['top.adbd']} "
                          f"tv.platform={native['tv.platform']}")
                    if dump_path and os.environ.get("DUMP_SYNC") == "1":
                        try:
                            with open(dump_path, "wb") as f:
                                f.write(data)
                            print(f"  >>> [DUMP] сырой ответ сохранён: {dump_path} ({len(data)}b)")
                        except OSError as e:
                            print(f"  >>> [DUMP] не смог сохранить: {e}")
                    payload = sync_payload(source=d, platform=self._request_platform())
                    self._send_json(payload)
                    print(f"  >>> [SYNC-REAL] ответ Яндекса + adbd ({len(data)}b) <<<")
                    log_local(f"SYNC adbd-инъекция {self.path} ({len(payload)}b)")
                    self._log_payload("SYNC-REAL→", payload, "настоящий ответ Яндекса")
                    return
                print("  !!! [SYNC-REAL] 200, но не JSON — беру локальный шаблон")
            else:
                print(f"  !!! [SYNC-REAL] upstream {r.status} — беру локальный шаблон")
            self._send_local_fallback("sync", f"upstream {r.status} на sync")
        except Exception as e:
            self._send_local_fallback("sync", f"нет связи с Яндексом ({e!r})")

    def serve_cached(self, ttl_ok=True, dump_path=None):
        """Проксируем с кэшем: 200 — кэшируем и отдаём; ошибка/429 — отдаём кэш, если есть."""
        key = self.path
        cache = _cache_file(key)
        host = (self.headers.get("Host") or UPSTREAM_HOST).split(":")[0]
        ip = real_ip(host)
        kind = "sync" if is_sync_path(self.path) else "ok"
        body = self._read_body() if self.command in ("POST", "PUT") else None
        skip = ("host", "connection", "content-length", "accept-encoding", "transfer-encoding")
        hdrs = {k: v for k, v in self.headers.items() if k.lower() not in skip}
        try:
            r, data = self._upstream(host, ip, body, hdrs)
            log_yandex(f"{self.command} {self.path} -> {r.status} ({len(data)}b)")
            if r.status == 200:
                try:
                    os.makedirs(CACHE_DIR, exist_ok=True)
                    with open(cache, "wb") as f:
                        f.write(data)
                except OSError:
                    pass
                self.send_response(200)
                self.send_header("Content-Type", r.getheader("Content-Type") or "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if os.path.exists(cache):
                with open(cache, "rb") as f:
                    data = f.read()
                print(f"  >>> [CACHE] upstream {r.status}, отдаю кэш ({len(data)}b)")
                log_local(f"CACHE {self.path} (upstream {r.status})")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            # кэша нет, а Яндекс ответил 429/5xx — колонке отдаём локальный 200
            if upstream_soft_error(r.status):
                self._send_local_fallback(kind, f"upstream {r.status} на {self.path}")
                return
            self.send_response(r.status)
            self.end_headers()
        except Exception as e:
            if os.path.exists(cache):
                with open(cache, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self._send_local_fallback(kind, f"нет связи с Яндексом ({e!r})")

    def proxy_upstream(self, dump_path=None, fallback=None):
        host = (self.headers.get("Host") or UPSTREAM_HOST).split(":")[0]
        ip = real_ip(host)
        try:
            body = self._read_body() if self.command in ("POST", "PUT") else None
            skip = ("host", "connection", "content-length", "accept-encoding", "transfer-encoding")
            hdrs = {k: v for k, v in self.headers.items() if k.lower() not in skip}
            r, data = self._upstream(host, ip, body, hdrs)
            log_yandex(f"{self.command} {self.path} -> {r.status} ({len(data)}b)")
            if dump_path and r.status == 200:
                try:
                    with open(dump_path, "wb") as f:
                        f.write(data)
                    print(f"  >>> [DUMP] настоящий ответ сохранён: {dump_path} ({len(data)}b)")
                except OSError as e:
                    print(f"  >>> [DUMP] не смог сохранить: {e}")
            elif dump_path:
                print(f"  >>> [DUMP] пропущен: Яндекс ответил {r.status}, в шаблон не пишем")
            # лимит/перегрузка Яндекса не должна долетать до колонки
            if fallback and upstream_soft_error(r.status):
                self._send_local_fallback(fallback, f"upstream {r.status} на {self.path}")
                return
            self.send_response(r.status)
            for k, v in r.getheaders():
                if k.lower() in ("content-type", "content-length", "cache-control", "etag", "last-modified"):
                    self.send_header(k, v)
            if not r.getheader("Content-Length"):
                self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
            print(f"  >>> [PROXY] {self.command} {self.path} -> {r.status} ({len(data)}b) <<<")
            sys.stdout.flush()
        except Exception as e:
            if fallback:
                self._send_local_fallback(fallback, f"нет связи с Яндексом ({e!r})")
                return
            self.send_response(502)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
            print(f"  !!! [PROXY] ошибка {self.path}: {e!r}")
            log_yandex(f"ERROR {self.path}: {e!r}")
            sys.stdout.flush()

# --- встроенный DNS (Windows: dnsmasq там нет) ---
DNS_NAMES = {b"quasar.yandex.net", b"quasar.yandex.ru", b"scbh.yandex.net"}


def local_ips():
    """Все IPv4 этой машины (LAN, хот-спот, Wi-Fi) — чтобы ответить тем, до кого клиент дотянется."""
    ips = []
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
                ips.append(ip)
    except Exception:
        pass
    primary = _local_ip()
    if primary and primary not in ips:
        ips.insert(0, primary)
    return ips or [LOCAL_IP]
DNS_UPSTREAMS = [("1.1.1.1", 53), ("8.8.8.8", 53), ("77.88.8.8", 53)]


def _dns_qname(data):
    """Возвращает (имя в нижнем регистре, qtype) или (None, None)."""
    try:
        if len(data) < 12:
            return None, None
        i = 12
        labels = []
        while i < len(data) and data[i]:
            n = data[i]
            if n & 0xC0:
                return None, None
            labels.append(data[i + 1:i + 1 + n])
            i += 1 + n
        qtype = int.from_bytes(data[i + 1:i + 3], "big") if i + 3 <= len(data) else None
        return b".".join(labels).lower(), qtype
    except Exception:
        return None, None


def _dns_reply(query, an_ip=None):
    """Ответ: an_ip (строка или список) -> A-записи; иначе NODATA (AAAA и прочее)."""
    qname, qtype = _dns_qname(query)
    question = query[12:]
    # обрезаем question ровно до конца QNAME + 4 байта (qtype+qclass)
    i = 12
    while i < len(query) and query[i]:
        i += 1 + query[i]
    qend = min(len(query), i + 5)
    question = query[12:qend]
    flags = b"\x81\x80"                     # ответ, рекурсия доступна
    if an_ip is not None:
        ips = [an_ip] if isinstance(an_ip, str) else [x for x in an_ip if x]
        if not ips:
            return _dns_reply(query)
        header = query[:2] + flags + b"\x00\x01" + len(ips).to_bytes(2, "big") + b"\x00\x00\x00\x00"
        ttl = int(os.environ.get("DNS_TTL", "60")).to_bytes(4, "big")
        answer = b"".join(b"\xc0\x0c" + b"\x00\x01" + b"\x00\x01" + ttl +
                          b"\x00\x04" + socket.inet_aton(ip) for ip in ips)
        return header + question + answer
    header = query[:2] + flags + b"\x00\x01\x00\x00\x00\x00\x00\x00"
    return header + question


def _dns_forward(query):
    for host, port in DNS_UPSTREAMS:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(2)
            s.sendto(query, (host, port))
            data, _ = s.recvfrom(4096)
            s.close()
            return data
        except Exception:
            try:
                s.close()
            except Exception:
                pass
    return None


def _best_ips(client_ip=None):
    """Если клиент пришёл из другой подсети (хот-спот, второй Wi-Fi) — отвечаем IP из ЕГО сети."""
    ips = local_ips()
    if client_ip and not client_ip.startswith("127."):
        net = client_ip.rsplit(".", 1)[0] + "."
        same = [ip for ip in ips if ip.startswith(net)]
        if same:
            return same
    # нет совпадения по подсети (или клиент локальный) — отдаём только основной IP,
    # чтобы не подсовывать VPN/виртуальные адаптеры
    return ips[:1]


def _dns_handle(query, client_ip=None):
    qname, qtype = _dns_qname(query)
    if qname in DNS_NAMES:
        # A спрашивают -> отдаём свой IP (или несколько); AAAA и остальное -> NODATA
        ips = _best_ips(client_ip) if qtype == 1 else []
        log_dns(f"{client_ip or '-'} {'A' if qtype == 1 else 'TYPE' + str(qtype)} "
                f"{_dns_name(qname)} -> {','.join(ips) if ips else 'NODATA'}")
        return _dns_reply(query, ips) if ips else _dns_reply(query)
    log_dns(f"{client_ip or '-'} FORWARD {_dns_name(qname)} (type {qtype})")
    return _dns_forward(query) or _dns_reply(query)


def run_dns(port=53):
    try:
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        udp.bind(("0.0.0.0", port))
        _ips = ", ".join(local_ips())
        print(f"[*] DNS сервер слушает UDP {port} (quasar.* -> {_ips})")
        sys.stdout.flush()

        def udp_loop():
            while True:
                try:
                    data, addr = udp.recvfrom(4096)
                    if data:
                        udp.sendto(_dns_handle(data, addr[0]), addr)
                except Exception as e:
                    print(f"[!] DNS UDP: {e!r}")

        threading.Thread(target=udp_loop, daemon=True).start()

        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tcp.bind(("0.0.0.0", port))
        tcp.listen(16)

        while True:
            try:
                conn, _ = tcp.accept()
            except Exception:
                continue
            threading.Thread(target=_dns_tcp_conn, args=(conn,), daemon=True).start()
    except Exception as e:
        print(f"[!] Ошибка запуска DNS на порту {port}: {e}")


def _dns_tcp_conn(conn):
    try:
        peer = conn.getpeername()[0]
        conn.settimeout(5)
        head = conn.recv(2)
        if len(head) < 2:
            return
        n = int.from_bytes(head, "big")
        data = b""
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                break
            data += chunk
        resp = _dns_handle(data, peer)
        conn.sendall(len(resp).to_bytes(2, "big") + resp)
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


class ReusableServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True

def run_http(port=80):
    try:
        httpd = ReusableServer(("0.0.0.0", port), EquasarHandler)
        print(f"[*] HTTP сервер слушает порт {port}...")
        httpd.serve_forever()
    except Exception as e:
        print(f"[!] Ошибка запуска HTTP на порту {port}: {e}")

def run_https(cert_file, key_file, port=443):
    try:
        httpd = ReusableServer(("0.0.0.0", port), EquasarHandler)
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        # старые прошивки (Mini 2019) умеют только TLS 1.2 и ниже — не отсекаем их
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1
            ctx.set_ciphers("DEFAULT@SECLEVEL=0")
        except (AttributeError, ValueError, ssl.SSLError):
            pass
        ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        print(f"[*] HTTPS сервер слушает порт {port} (с сертификатами)...")
        httpd.serve_forever()
    except Exception as e:
        print(f"[!] Ошибка запуска HTTPS на порту {port}: {e}")

def find_certificates():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        (os.path.join(base_dir, "..", "certs", "fullchain.pem"),
         os.path.join(base_dir, "..", "certs", "quasar-all.key")),
        (os.path.join(base_dir, "..", "nginx-win", "nginx-1.26.2", "conf", "certs", "fullchain.pem"),
         os.path.join(base_dir, "..", "nginx-win", "nginx-1.26.2", "conf", "certs", "quasar-all.key")),
    ]
    for cert_path, key_path in candidates:
        if os.path.exists(cert_path) and os.path.exists(key_path):
            return cert_path, key_path
    return None, None

def main():
    global MODE_DOWNGRADE, MODE_PASSTHROUGH, MODE_OTA_ONLY
    parser = argparse.ArgumentParser(description="eQuasar Server for Yandex Station Lite 1")
    parser.add_argument("--downgrade", action="store_true", help="Включить режим даунгрейда на уязвимую прошивку")
    parser.add_argument("--ota-only", action="store_true",
                        help="Только прошивка (Mini): отдавать манифест и zip, всё остальное — в Яндекс")
    parser.add_argument("--passthrough", action="store_true",
                        help="Прозрачный режим: всё проксируется в настоящий Яндекс (для диагностики TLS)")
    parser.add_argument("--dns", action="store_true",
                        help="Поднять встроенный DNS на :53 (Windows; на Linux/macOS обычно dnsmasq)")
    args = parser.parse_args()

    if args.downgrade or os.environ.get("EQUASAR_MODE") == "downgrade":
        MODE_DOWNGRADE = True
    if args.ota_only or os.environ.get("EQUASAR_MODE") == "ota":
        MODE_OTA_ONLY = True
    if args.passthrough or os.environ.get("EQUASAR_MODE") == "passthrough":
        MODE_PASSTHROUGH = True

    # создаём файлы логов сразу, чтобы они были даже без активности
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        for _n in ("yandex.log", "local.log", "dns.log"):
            open(os.path.join(LOG_DIR, _n), "a", encoding="utf-8").close()
    except OSError as e:
        print(f"[!] не смог создать логи в {LOG_DIR}: {e}")

    cert_path, key_path = find_certificates()
    if not cert_path or not key_path:
        print("[!] Ошибка: Сертификаты fullchain.pem и quasar-all.key не найдены.")
        sys.exit(1)

    if OTA_FILE:
        print(f"[OTA] {os.path.basename(OTA_FILE)} | версия {OTA_VERSION} | crc32 {_ota_crc()} | "
              f"модель {OTA_PLATFORM} | отдаём по {OTA_PREFIX}")
    else:
        print("[OTA] в firmware/ нет .zip — режим отката будет отдавать 404")
    print("==================================================================")
    print("      eQuasar Server — Кроссплатформенный сервер eQuasar          ")
    print(f"      build {BUILD}")
    if MODE_PASSTHROUGH:
        print("[РЕЖИМ] PASSTHROUGH — всё уходит в настоящий Яндекс, подмены выключены")
    if MODE_OTA_ONLY:
        print("[РЕЖИМ] OTA-ONLY — только прошивка: манифест и zip, всё остальное в Яндекс (ADB не включаем)")
    if SYNC_LOCAL:
        print("[РЕЖИМ] SYNC_LOCAL=1 — sync из локального шаблона (Яндекс не спрашиваем)")
    else:
        print("[РЕЖИМ] sync: настоящий ответ Яндекса + adbd (429/5xx → локальный шаблон)")
    print("==================================================================")
    if MODE_DOWNGRADE:
        print("РЕЖИМ: [ДАУНГРЕЙД] — Отдача официальной уязвимой прошивки Яндекса")
    else:
        print("РЕЖИМ: [ВЗЛОМ / АКТИВАЦИЯ] — Включение ADB, обновления заблокированы")
    print("Сертификаты загружены из:", os.path.dirname(cert_path))
    print("Готов принимать запросы от Станции Лайт на портах 80 и 443.")
    print("Нажмите Ctrl+C для остановки.")
    print("------------------------------------------------------------------")

    # порты можно переопределить (удобно для проверки, когда 80/443 заняты)
    _hp = int(os.environ.get("HTTP_PORT", "80"))
    _sp = int(os.environ.get("HTTPS_PORT", "443"))
    t_http = threading.Thread(target=run_http, args=(_hp,), daemon=True)
    t_https = threading.Thread(target=run_https, args=(cert_path, key_path, _sp), daemon=True)
    t_dns = None
    if args.dns or os.environ.get("BUILTIN_DNS") == "1":
        _dp = int(os.environ.get("DNS_PORT", "53"))
        t_dns = threading.Thread(target=run_dns, args=(_dp,), daemon=True)

    t_http.start()
    t_https.start()
    if t_dns:
        t_dns.start()

    try:
        t_http.join()
        t_https.join()
        if t_dns:
            t_dns.join()
    except KeyboardInterrupt:
        print("\n[*] Сервер остановлен.")

if __name__ == "__main__":
    main()
