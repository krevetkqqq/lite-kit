#!/bin/bash
# Поднимает стенд eQuasar (DNS + HTTPS на :53/:80/:443) для Яндекс Станций.
# Работает на Linux и macOS. Запуск: ./start.sh
set -uo pipefail

BASE="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" = 0 ] || exec sudo --preserve-env=SYNC_PROXY,COLUMN_IPS,LOCAL_IP,BIND_IP,EQUASAR_MODE,SYNC_RETRIES,SYNC_RETRY_DELAY bash "$0" "$@"

OS="$(uname -s)"

# --- IP этой машины в локальной сети (не VPN, не docker) ---
if [ "$OS" = "Darwin" ]; then
    IP="$(ipconfig getifaddr en0 2>/dev/null || true)"
    [ -z "$IP" ] && IP="$(ipconfig getifaddr en1 2>/dev/null || true)"
else
    IFACE="$(ip route 2>/dev/null | awk '/^default/{print $5; exit}')"
    [ -n "$IFACE" ] && IP="$(ip -4 addr show dev "$IFACE" 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 | head -1)"
    [ -z "${IP:-}" ] && IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
fi
if [ -z "${IP:-}" ]; then echo "[!] не смог определить IP этой машины в локальной сети"; exit 1; fi

DNSMASQ="$(command -v dnsmasq || true)"
for c in /opt/homebrew/sbin/dnsmasq /usr/local/sbin/dnsmasq /usr/sbin/dnsmasq; do
    [ -n "$DNSMASQ" ] && break
    [ -x "$c" ] && DNSMASQ="$c"
done
[ -n "$DNSMASQ" ] && [ -x "$DNSMASQ" ] || {
    echo "[!] dnsmasq не найден: apt install dnsmasq | pacman -S dnsmasq | brew install dnsmasq"; exit 1; }

mkdir -p "$BASE/run" "$BASE/log"

# --- гасим прошлый запуск ---
[ -f "$BASE/run/dnsmasq.pid" ] && kill -9 "$(cat "$BASE/run/dnsmasq.pid")" 2>/dev/null
[ -f "$BASE/run/server.pid" ] && kill -9 "$(cat "$BASE/run/server.pid")" 2>/dev/null
sleep 1

# --- конфиг DNS ---
sed -e "s|__MACIP__|$IP|g" -e "s|__LOGDIR__|$BASE/log|g" -e "s|__RUNDIR__|$BASE/run|g" \
    "$BASE/config/dnsmasq.conf.tpl" > "$BASE/run/dnsmasq.conf"
"$DNSMASQ" --test -C "$BASE/run/dnsmasq.conf" || { echo "[!] конфиг dnsmasq битый"; exit 1; }
"$DNSMASQ" --conf-file="$BASE/run/dnsmasq.conf"
sleep 1

# --- какой питон брать: нужен OpenSSL >= 1.1.1 (TLS 1.3) ---
PYBIN=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 "$(command -v python3 2>/dev/null)"; do
    [ -n "$c" ] && [ -x "$c" ] || continue
    if "$c" -c 'import ssl,sys; sys.exit(0 if ssl.OPENSSL_VERSION_INFO >= (1,1,1) else 1)' 2>/dev/null; then
        PYBIN="$c"; break
    fi
done
[ -z "$PYBIN" ] && PYBIN="$(command -v python3 || echo /usr/bin/python3)"
echo "[*] python: $PYBIN ($("$PYBIN" -c 'import ssl; print(ssl.OPENSSL_VERSION)' 2>/dev/null))"
export LOCAL_IP="${LOCAL_IP:-$IP}"
# флаг-файл: touch run/sync_proxy — sync берётся у Яндекса и дополняется adbd
[ -f "$BASE/run/sync_proxy" ] && export SYNC_PROXY=1

# --- сервер :80/:443 ---
# по умолчанию: ADB + обновления ЗАБЛОКИРОВАНЫ (check_updates -> hasUpdate:false, zip не отдаём)
# включить откат/раздачу прошивки из firmware/: touch run/downgrade
ARGS=""
[ -f "$BASE/run/downgrade" ] && ARGS="--downgrade"
( cd "$BASE" && "$PYBIN" scripts/equasar_server.py $ARGS >> "$BASE/log/server.log" 2>&1 & echo $! > "$BASE/run/server.pid" )
sleep 2

echo
echo "=== СТЕНД ПОДНЯТ ==="
echo "  адрес машины   : $IP"
echo "  DNS            : quasar.yandex.net / .ru / scbh.yandex.net -> $IP"
echo "  сервер         : 80 и 443 (серты eMudhra)"
echo "  режим          : $(if [ -f "$BASE/run/no_downgrade" ]; then echo "обновления выключены (run/no_downgrade)"; elif find "$BASE/firmware" -name '*.zip' -print -quit | grep -q .; then echo "ADB + авто-прошивка (если версия не совпадает)"; else echo "ADB, обновления выключены (нет прошивки)"; fi)"
FW="$(find "$BASE/firmware" -name '*.zip' 2>/dev/null | head -1)"
if [ -n "$FW" ]; then
    echo "  прошивка       : $(basename "$(dirname "$FW")")/$(basename "$FW") ($(wc -c <"$FW" | tr -d ' ') б)"
else
    echo "  прошивка       : в firmware/ нет .zip (раздача прошивки недоступна)"
fi
echo "  лог            : $BASE/log/server.log"
echo
echo "Проверка DNS на этой машине (должно быть $IP):"
dig +short +time=3 @"$IP" quasar.yandex.net A 2>/dev/null || true
echo
echo "Дальше: в роутере поставь DNS = $IP, потом сбрасывай колонку (см. README.md)."
