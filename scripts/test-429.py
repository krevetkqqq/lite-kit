#!/usr/bin/env python3
"""Проверка: ошибки Яндекса (429/5xx/обрыв) не долетают до колонки.

Поднимает сервер на 127.0.0.1 со замоканным апстримом и гоняет сценарии.
Запуск: python3 scripts/test-429.py   (нужен любой python3, без зависимостей)
"""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import equasar_server as es

UPSTREAM = {"mode": "429", "body": b'{"proxied":true}'}


class FakeResp:
    status = 200

    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def getheader(self, name, default=None):
        return "application/json" if name.lower() == "content-type" else default

    def getheaders(self):
        return [("Content-Type", "application/json")]


class FakeConn:
    def __init__(self, host, ip, **kw):
        pass

    def request(self, method, path, body=None, headers=None):
        pass

    def getresponse(self):
        m = UPSTREAM["mode"]
        if m == "boom":
            raise OSError("connection reset by peer")
        if m == "flaky":
            UPSTREAM["calls"] = UPSTREAM.get("calls", 0) + 1
            if UPSTREAM["calls"] <= UPSTREAM.get("fail_first", 2):
                return FakeResp(429, b'{"status":"limited"}')
            UPSTREAM["calls"] = 0
            return FakeResp(200, UPSTREAM["body"])
        if m == "200":
            return FakeResp(200, UPSTREAM["body"] or b'{"proxied":true}')
        if m == "404":
            return FakeResp(404, b'{"error":"not found"}')
        return FakeResp(int(m), UPSTREAM["body"])


es.PinnedHTTPS = FakeConn
es.real_ip = lambda host="quasar.yandex.net": "127.0.0.1"
# Тесты OTA не должны зависеть от наличия run/no_downgrade у пользователя.
# Отдельные тесты самостоятельно включают запрет OTA для проверки.
es.no_downgrade = lambda: False
# в тестах не ждём паузы между повторами
es.RETRY_ATTEMPTS = 1
es.RETRY_DELAY = 0
es.CACHE_DIR = tempfile.mkdtemp(prefix="eqcache-")
es.SYNC_DUMP = os.path.join(es.CACHE_DIR, "sync_real.json")

srv = es.ReusableServer(("127.0.0.1", 0), es.EquasarHandler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def call(method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    c.request(method, path, body=body, headers={"Host": "quasar.yandex.net"})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def adbd_of(data):
    try:
        return json.loads(data).get("config", {}).get("system_config", {}).get("adbd")
    except Exception:
        return None


FAILS = []


def case(name, mode, method, path, want_code=200, want_adbd=None,
         want_body=None, setup=None, cleanup=None):
    UPSTREAM["mode"] = mode
    if setup:
        setup()
    try:
        code, data = call(method, path)
    finally:
        if cleanup:
            cleanup()
    adbd = adbd_of(data)
    ok = code == want_code
    if want_adbd is not None:
        ok = ok and bool(adbd and adbd.get("remoteActivation") is True) == want_adbd
    if want_body is not None:
        ok = ok and data == want_body
    if not ok:
        FAILS.append(name)
    print(f"{'ok  ' if ok else 'FAIL'} {name:52} -> {code} "
          f"adbd={bool(adbd and adbd.get('remoteActivation'))} {data[:60]!r}")


def cache_for(path):
    def put():
        os.makedirs(es.CACHE_DIR, exist_ok=True)
        with open(es._cache_file(path), "wb") as f:
            f.write(b'{"cached":true}')
    return put


def drop_cache(path):
    def rm():
        try:
            os.remove(es._cache_file(path))
        except OSError:
            pass
    return rm


# --- sync всегда локальный: 200 + adbd:true, без обращения в Яндекс ---
for mode in ("429", "500", "boom", "200", "404"):
    case(f"sync /get_sync_info при upstream {mode}", mode, "GET",
         "/get_sync_info?device_id=LP00&platform=yandexmicro", want_adbd=True)
case("sync /get_sync_info без query", "429", "GET", "/get_sync_info", want_adbd=True)
case("sync POST /get_sync_info", "429", "POST", "/get_sync_info", want_adbd=True)

# --- кэшируемые пути /glagol, /telemetry, /metrics ---
case("glagol 429 без кэша -> мягкий 200", "429", "GET", "/glagol/device_list",
     want_body=b'{"status":"ok"}', cleanup=drop_cache("/glagol/device_list"))
case("glagol 429 с кэшем -> кэш", "429", "GET", "/glagol/device_list",
     want_body=b'{"cached":true}', setup=cache_for("/glagol/device_list"),
     cleanup=drop_cache("/glagol/device_list"))
case("glagol 503 без кэша -> мягкий 200", "503", "GET", "/glagol/x",
     want_body=b'{"status":"ok"}', cleanup=drop_cache("/glagol/x"))
case("glagol обрыв связи -> мягкий 200", "boom", "GET", "/glagol/x",
     want_body=b'{"status":"ok"}', cleanup=drop_cache("/glagol/x"))
case("telemetry 429 -> мягкий 200", "429", "POST", "/telemetry/x",
     want_body=b'{"status":"ok"}', cleanup=drop_cache("/telemetry/x"))
case("metrics 429 -> мягкий 200", "429", "POST", "/metrics/x",
     want_body=b'{"status":"ok"}', cleanup=drop_cache("/metrics/x"))
case("glagol 200 -> проксируется как есть", "200", "GET", "/glagol/x",
     want_body=b'{"proxied":true}', cleanup=drop_cache("/glagol/x"))

# --- прочие пути: 429 -> 200 ok, sync-подобные -> 200 + adbd ---
case("неизвестный путь 429 -> 200 ok", "429", "POST", "/v1/whatever",
     want_body=b'{"status":"ok"}')
case("sync-путь 429 -> 200 + adbd", "429", "POST", "/v1/sync",
     want_adbd=True)
case("неизвестный путь 404 пробрасывается", "404", "GET", "/v1/whatever",
     want_code=404)
case("неизвестный путь 200 проксируется", "200", "GET", "/v1/whatever",
     want_body=b'{"proxied":true}')

# --- локальные заглушки ---
case("POST /register -> всегда allvalid (upstream 200)", "200", "POST", "/register",
     want_body=b'{"message":"allvalid","status":"ok"}')
case("POST /register -> allvalid (upstream 429)", "429", "POST", "/register",
     want_body=b'{"message":"allvalid","status":"ok"}')
case("POST /register -> allvalid (upstream обрыв)", "boom", "POST", "/register",
     want_body=b'{"message":"allvalid","status":"ok"}')
case("POST /set_device_capability -> в Яндекс", "200", "POST", "/set_device_capability",
     want_body=b'{"proxied":true}')
case("POST /update_device_state -> в Яндекс", "200", "POST", "/update_device_state",
     want_body=b'{"proxied":true}')

# --- авто-раздача прошивки: включена, если версия колонки не совпадает ---
_saved_ota, _saved_ver = es.OTA_FILE, es.OTA_VERSION
_fw = os.path.join(es.CACHE_DIR, "0.1.2.3.zip")
os.makedirs(es.CACHE_DIR, exist_ok=True)
with open(_fw, "wb") as f:
    f.write(b"PK\x03\x04" + b"\0" * 2048)
es.OTA_FILE = _fw
es.OTA_VERSION = "0.1.2.3"


def _fw_case(name, version, want_update, block=False):
    orig = es.no_downgrade
    if block:
        es.no_downgrade = lambda: True
    try:
        code, data = call("GET", f"/check_updates?device_id=LP00&version={version}&platform=yandexmicro")
    finally:
        es.no_downgrade = orig
    got = b'"hasUpdate": true' in data or b'"hasUpdate":true' in data
    ok = code == 200 and got == want_update
    if not ok:
        FAILS.append(name)
    print(f"{'ok  ' if ok else 'FAIL'} {name:52} -> {code} hasUpdate={got}")


_fw_case("factory: зип есть -> манифест", "0.52.1.101.PVT.factory", True)
_fw_case("авто-прошивка: версия другая -> манифест", "0.0.9", True)
_fw_case("авто-прошивка: версия совпала -> hasUpdate:false", "0.1.2.3", False)
_fw_case("авто-прошивка: run/no_downgrade -> выкл", "0.0.9", False, block=True)

es.OTA_FILE = None
code, data = call("GET", "/check_updates?device_id=LP00&version=1.0.0&platform=yandexmicro")
ok = code == 200 and b'"hasUpdate":false' in data
if not ok:
    FAILS.append("авто-прошивка: нет файла -> false")
print(f"{'ok  ' if ok else 'FAIL'} {'авто-прошивка: нет файла -> hasUpdate:false':52} -> {code}")

# прошивка 0.x (yandexmicro) не должна предлагаться колонке yandexmini
es.OTA_FILE = _fw
es.OTA_VERSION = "0.1.2.3"
code, data = call("GET", "/check_updates?device_id=LP00&version=0.0.9&platform=yandexmini")
ok = code == 200 and b'"hasUpdate":false' in data
if not ok:
    FAILS.append("платформа не совпала -> hasUpdate:false")
print(f"{'ok  ' if ok else 'FAIL'} {'другая платформа -> hasUpdate:false':52} -> {code}")

# --- новая раскладка firmware/<platform>/<version>.zip ---
_tmp_root = tempfile.mkdtemp(prefix="eqfw-")
_d = os.path.join(_tmp_root, "firmware", "yandexmini")
os.makedirs(_d, exist_ok=True)
_z = os.path.join(_d, "1.305.4.16.3237824040.20250721.zip")
with open(_z, "wb") as f:
    f.write(b"PK\x03\x04" + b"\0" * 2048)
with open(os.path.join(_d, "manifest.json"), "w") as f:
    json.dump({"platform": "yandexmini", "version": "1.305.4.16.3237824040.20250721",
               "filename": os.path.basename(_z), "crc32": 1}, f)

_saved_base = es.BASE_DIR
es.BASE_DIR = _tmp_root
es.OTA_FILE = None
code, data = call("GET", "/check_updates?device_id=LP00&version=1.0.0&platform=yandexmini")
ok = code == 200 and b'"hasUpdate": true' in data and b"yandexmini/ota/1.305.4.16" in data
if not ok:
    FAILS.append("новая раскладка: манифест по платформе")
print(f"{'ok  ' if ok else 'FAIL'} {'firmware/<platform>/: манифест для своей модели':52} -> {code}")

code, data = call("GET", "/check_updates?device_id=LP00&version=1.0.0&platform=yandexmicro")
ok = code == 200 and b'"hasUpdate":false' in data
if not ok:
    FAILS.append("новая раскладка: чужая платформа -> false")
print(f"{'ok  ' if ok else 'FAIL'} {'firmware/<platform>/: чужая модель -> false':52} -> {code}")

code, data = call("GET", "/yandexmini/ota/1.305.4.16.3237824040.20250721.zip", )
ok = code == 200 and len(data) == 2052
if not ok:
    FAILS.append("новая раскладка: раздача zip")
print(f"{'ok  ' if ok else 'FAIL'} {'firmware/<platform>/: zip отдаётся':52} -> {code} {len(data)}b")
es.BASE_DIR = _saved_base
shutil.rmtree(_tmp_root, ignore_errors=True)

# --- «просто zip» в корне firmware/: платформа определяется по зеркалу ---
_tmp2 = tempfile.mkdtemp(prefix="eqfwflat-")
_flat = os.path.join(_tmp2, "firmware", "0.369.1.46.4778262442.20261013.297.zip")
os.makedirs(os.path.dirname(_flat), exist_ok=True)
with open(_flat, "wb") as f:
    f.write(b"PK\x03\x04" + b"\0" * 2048)

_saved_base2 = es.BASE_DIR
_saved_mp = es.mirror_platform
es.BASE_DIR = _tmp2
es.OTA_FILE = _flat
es.OTA_VERSION = "0.369.1.46.4778262442.20261013.297"
es.mirror_platform = lambda v: "yandexmini"

code, data = call("GET", "/check_updates?device_id=LP00&version=1.0.0&platform=yandexmini")
ok = code == 200 and b'"hasUpdate": true' in data
if not ok:
    FAILS.append("плоский zip: платформа по зеркалу -> манифест")
print(f"{'ok  ' if ok else 'FAIL'} {'плоский zip: платформа по зеркалу -> манифест':52} -> {code}")

code, data = call("GET", "/check_updates?device_id=LP00&version=1.0.0&platform=yandexmicro")
ok = code == 200 and b'"hasUpdate":false' in data
if not ok:
    FAILS.append("плоский zip: чужая платформа -> false")
print(f"{'ok  ' if ok else 'FAIL'} {'плоский zip: чужая платформа -> false':52} -> {code}")

es.mirror_platform = _saved_mp
es.BASE_DIR = _saved_base2
shutil.rmtree(_tmp2, ignore_errors=True)

es.OTA_FILE, es.OTA_VERSION = _saved_ota, _saved_ver
case("check_updates без прошивки -> hasUpdate:false", "200", "GET",
     "/check_updates?device_id=LP00&version=1.305.4.16&platform=yandexmini",
     want_body=b'{"hasUpdate":false}')
case("check_updates любой путь -> hasUpdate:false", "200", "GET",
     "/quasar/check_updates/v2?device_id=LP00&platform=yandexmini",
     want_body=b'{"hasUpdate":false}')

# --- диагностика SYNC_PROXY: sync берётся у Яндекса и дополняется adbd ---
def _no_dump():
    try:
        os.remove(es.SYNC_DUMP)
    except OSError:
        pass


def _upstream_sync_200():
    UPSTREAM["mode"] = "200"
    UPSTREAM["body"] = json.dumps({
        "status": "ok",
        "tags": ["real-answer"],
        "config": {"device_config": {"name": "Яндекс Мини"},
                   "system_config": {"syncd": {"pollPeriodSec": 1800}}},
    }).encode()


def _upstream_sync_garbage():
    UPSTREAM["mode"] = "200"
    UPSTREAM["body"] = b"<html>not json</html>"


def _upstream_sync_plain():
    UPSTREAM["mode"] = "200"
    UPSTREAM["body"] = b'{"proxied":true}'


es.SYNC_LOCAL = False
_no_dump()
case("sync: 429 -> adbd + шаблон", "429", "GET",
     "/get_sync_info?device_id=LP00", want_adbd=True)
if os.path.exists(es.SYNC_DUMP):
    FAILS.append("SYNC_PROXY: дамп 429 записан")
print(f"{'ok  ' if not os.path.exists(es.SYNC_DUMP) else 'FAIL'} "
      f"{'sync: 429 не пишется в дамп':52} -> dump={os.path.exists(es.SYNC_DUMP)}")

_upstream_sync_200()
UPSTREAM["mode"] = "200"
code, data = call("GET", "/get_sync_info?device_id=LP00")
d = json.loads(data) if data else {}
ok = (code == 200 and d.get("tags") == ["real-answer"]
      and d.get("config", {}).get("system_config", {}).get("adbd", {}).get("remoteActivation") is True
      and d.get("adbd", {}).get("remoteActivation") is True)
if not ok:
    FAILS.append("sync: реальный ответ + adbd")
print(f"{'ok  ' if ok else 'FAIL'} {'sync: 200 Яндекса + adbd (поля сохранены)':52} -> {code} tags={d.get('tags')}")

_upstream_sync_garbage()
case("sync: 200 не-JSON -> шаблон + adbd", "200", "GET",
     "/get_sync_info?device_id=LP00", want_adbd=True)
_upstream_sync_200()
case("sync: POST + adbd", "200", "POST", "/get_sync_info", want_adbd=True)

def _upstream_sync_adbd_false():
    """Яндекс отдал adbd:false во всех ветках и свои лишние поля — всё должно стать true."""
    UPSTREAM["mode"] = "200"
    UPSTREAM["body"] = json.dumps({
        "status": "ok",
        "adbd": {"remoteActivation": False, "network": False, "fullDurationMs": 0},
        "config": {
            "adbd": {"remoteActivation": False},
            "system_config": {"adbd": {"remoteActivation": False, "custom": 1},
                              "syncd": {"pollPeriodSec": 1800}},
            "device_config": {"name": "Яндекс Мини", "adbd": {"remoteActivation": False}},
        },
    }).encode()


es.SYNC_LOCAL = False
_no_dump()
_upstream_sync_adbd_false()
code, data = call("GET", "/get_sync_info?device_id=LP00")
d = json.loads(data) if data else {}
cfg = d.get("config") or {}
all_adbd = [
    d.get("adbd"),
    cfg.get("adbd"),
    (cfg.get("system_config") or {}).get("adbd"),
    (cfg.get("device_config") or {}).get("adbd"),
]
ok = (code == 200
      and all(bool(a and a.get("remoteActivation") is True) for a in all_adbd)
      and all(a.get("fullDurationMs") == 31536000000 for a in all_adbd)
      and "custom" not in (cfg.get("system_config") or {}).get("adbd", {}))
if not ok:
    FAILS.append("sync: adbd:false -> true во всех ветках")
print(f"{'ok  ' if ok else 'FAIL'} {'sync: adbd:false -> true во всех 4 ветках':52} "
      f"-> {code} {[bool(a and a.get('remoteActivation')) for a in all_adbd]}")

# --- повторы при 429: 2 неудачи, третья успешная -> отдаём настоящий ответ ---
es.RETRY_ATTEMPTS = 3
es.RETRY_DELAY = 0
UPSTREAM["body"] = json.dumps({"status": "ok", "tags": ["after-retry"],
                               "config": {"system_config": {}}}).encode()
UPSTREAM["fail_first"] = 2
UPSTREAM["calls"] = 0
UPSTREAM["mode"] = "flaky"
code, data = call("GET", "/get_sync_info?device_id=LP00")
d = json.loads(data) if data else {}
ok = (code == 200 and d.get("tags") == ["after-retry"]
      and d.get("adbd", {}).get("remoteActivation") is True)
if not ok:
    FAILS.append("повторы: 429,429,200 -> настоящий ответ")
print(f"{'ok  ' if ok else 'FAIL'} {'повторы: 429,429,200 -> настоящий ответ + adbd':52} -> {code} tags={d.get('tags')}")

UPSTREAM["fail_first"] = 3
UPSTREAM["calls"] = 0
code, data = call("GET", "/get_sync_info?device_id=LP00")
d = json.loads(data) if data else {}
ok = (code == 200 and d.get("adbd", {}).get("remoteActivation") is True
      and d.get("tags") != ["after-retry"])
if not ok:
    FAILS.append("повторы: 3x429 -> шаблон")
print(f"{'ok  ' if ok else 'FAIL'} {'повторы: 429x3 -> локальный шаблон + adbd':52} -> {code} adbd={bool(d.get('adbd'))}")

es.RETRY_ATTEMPTS = 1
es.SYNC_LOCAL = True
_upstream_sync_plain()

case("SYNC_LOCAL=1: локальный шаблон без Яндекса", "200", "GET",
     "/get_sync_info?device_id=LP00", want_adbd=True)

# --- OTA: HEAD / Range на фейковой прошивке ---
_ota_dir = tempfile.mkdtemp(prefix="eqota-")
_zip = os.path.join(_ota_dir, "0.369.1.46.4778262442.20261013.297.zip")
with open(_zip, "wb") as f:
    f.write(b"PK\x03\x04" + b"\0" * 4092)
es.OTA_FILE = _zip
es.OTA_PREFIX = "/yandexmicro/ota/"
es.MODE_DOWNGRADE = True    # раздача zip разрешена только в режиме отката

# авто-режим: прошивка есть -> zip отдаётся и без run/downgrade
es.MODE_DOWNGRADE = False
code, data = call("GET", "/yandexmicro/ota/0.369.1.46.4778262442.20261013.297.zip")
ok = code == 200 and len(data) == 4096
if not ok:
    FAILS.append("OTA zip в авто-режиме")
print(f"{'ok  ' if ok else 'FAIL'} {'OTA zip в авто-режиме (без run/downgrade)':52} -> {code} {len(data)}b")

# run/no_downgrade -> 404
_orig_nd = es.no_downgrade
es.no_downgrade = lambda: True
code, data = call("GET", "/yandexmicro/ota/0.369.1.46.4778262442.20261013.297.zip")
es.no_downgrade = _orig_nd
ok = code == 404
if not ok:
    FAILS.append("OTA zip при run/no_downgrade")
print(f"{'ok  ' if ok else 'FAIL'} {'OTA zip при run/no_downgrade -> 404':52} -> {code}")
es.MODE_DOWNGRADE = True

case("OTA HEAD 200 + размер", "429", "HEAD",
     "/yandexmicro/ota/0.369.1.46.4778262442.20261013.297.zip", want_code=200)
UPSTREAM["mode"] = "429"
code, data = call("GET", "/yandexmicro/ota/0.369.1.46.4778262442.20261013.297.zip")
ok = code == 200 and len(data) == 4096
if not ok:
    FAILS.append("OTA полная отдача")
print(f"{'ok  ' if ok else 'FAIL'} {'OTA отдаётся целиком':52} -> {code} {len(data)}b")

c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
c.request("GET", "/yandexmicro/ota/0.369.1.46.4778262442.20261013.297.zip",
          headers={"Host": "quasar.yandex.net", "Range": "bytes=0-1023"})
r = c.getresponse()
data = r.read()
ok = r.status == 206 and len(data) == 1024
if not ok:
    FAILS.append("OTA Range 206")
print(f"{'ok  ' if ok else 'FAIL'} {'OTA Range -> 206, 1024б':52} -> {r.status} {len(data)}b")
c.close()

shutil.rmtree(_ota_dir, ignore_errors=True)
shutil.rmtree(es.CACHE_DIR, ignore_errors=True)

print("\nИТОГО:", "ВСЁ ЗЕЛЁНОЕ" if not FAILS else "ПАДЕНИЯ: " + ", ".join(FAILS))
sys.exit(0 if not FAILS else 1)
