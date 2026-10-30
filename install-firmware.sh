#!/usr/bin/env bash
# install-firmware.sh — скачать прошивку с зеркала и положить в firmware/ как <версия>.zip
#
#   ./install-firmware.sh                     # интерактивное меню (платформа -> версия)
#   ./install-firmware.sh --list              # только показать платформы и версии
#   ./install-firmware.sh -p yandexmicro -v 0.369.1.46.4778262442.20261013.297
#
# На зеркале файл лежит как update.zip — сохраняем под именем версии.
set -euo pipefail
BASE="$(cd "$(dirname "$0")" && pwd)"
MIRROR="${MIRROR_URL:-http://144.76.194.29:62891}"
FW_DIR="$BASE/firmware"
PY="${PYTHON:-python3}"
PLATFORM=""
VERSION=""
LIST_ONLY=0
usage() { sed -n '2,12p' "$0"; exit "${1:-0}"; }
while [ $# -gt 0 ]; do
    case "$1" in
        -p|--platform) PLATFORM="$2"; shift 2 ;;
        -v|--version)  VERSION="$2";  shift 2 ;;
        -l|--list)     LIST_ONLY=1;   shift ;;
        -h|--help)     usage 0 ;;
        *) echo "[!] неизвестный аргумент: $1"; usage 1 ;;
    esac
done
need() { command -v "$1" >/dev/null 2>&1 || { echo "[!] нужен $1"; exit 1; }; }
need curl
need "$PY"
fetch() { curl -fsSL --retry 3 --connect-timeout 10 "$1"; }
ok_archive() {
    # прошивка Яндекса — cpio/SWUpdate (070701/070702), а не только zip (PK)
    local h; h="$(head -c 6 "$1" 2>/dev/null)"
    [ "${h:0:2}" = "PK" ] || [ "$h" = "070701" ] || [ "$h" = "070702" ]
}
# ссылки из листинга каталога
dirs() { grep -oE 'href="[^"]+/"' | sed 's/href="//; s/\/"$//' | sort; }
if [ "$LIST_ONLY" = 1 ]; then
    echo "Зеркало: $MIRROR"
    echo "Платформы:"
    fetch "$MIRROR/" | dirs | sed 's/^/  - /'
    exit 0
fi
PLATS=()
while IFS= read -r line; do PLATS+=("$line"); done < <(fetch "$MIRROR/" | dirs)
# -p 3 — тоже можно: номер из меню
if [[ "$PLATFORM" =~ ^[0-9]+$ ]]; then
    PLATFORM="${PLATS[$((PLATFORM-1))]:-}"
    [ -n "$PLATFORM" ] || { echo "[!] нет платформы с таким номером"; exit 1; }
fi
if [ -z "$PLATFORM" ]; then
    [ "${#PLATS[@]}" -gt 0 ] || { echo "[!] не получил список платформ с $MIRROR"; exit 1; }
    echo "Зеркало: $MIRROR"
    echo "Выбери платформу:"
    i=0
    for p in "${PLATS[@]}"; do i=$((i+1)); printf "  %2d) %s\n" "$i" "$p"; done
    printf "Номер (Enter = yandexmicro): "
    read -r n || true
    [ -z "${n:-}" ] && n=0
    if [ "$n" = 0 ]; then
        PLATFORM="yandexmicro"
    else
        PLATFORM="${PLATS[$((n-1))]:-}"
    fi
    [ -n "$PLATFORM" ] || { echo "[!] нет такой платформы"; exit 1; }
fi
echo "[*] платформа: $PLATFORM"
FW_DIR="$BASE/firmware/$PLATFORM"
VERSIONS_URL="$MIRROR/$PLATFORM/"
VERS=()
while IFS= read -r line; do VERS+=("$line"); done < <(
    fetch "$VERSIONS_URL" | dirs |
    "$PY" -c 'import sys
def key(v):
    try: return [int(x) for x in v.split(".")]
    except Exception: return [0]
rows=[r.strip() for r in sys.stdin if r.strip()]
print("\n".join(sorted(rows, key=key, reverse=True)))')
[ "${#VERS[@]}" -gt 0 ] || { echo "[!] нет версий в $VERSIONS_URL"; exit 1; }
# -v 5 — номер версии из списка
if [[ "$VERSION" =~ ^[0-9]+$ ]]; then
    VERSION="${VERS[$((VERSION-1))]:-}"
    [ -n "$VERSION" ] || { echo "[!] нет версии с таким номером"; exit 1; }
fi
if [ -z "$VERSION" ]; then
    echo "Выбери версию (сверху новые):"
    case "$PLATFORM" in
        yandexmicro) KNOWN="0.369.1.46.4778262442.20261013.297" ;;
        yandexmini)  KNOWN="1.305.4.16.3237824040.20250721" ;;
        yandexmini_2) KNOWN="0.369.1.46.4778262943.20261013.299" ;;
        *)           KNOWN="" ;;
    esac
    i=0
    for v in "${VERS[@]}"; do
        i=$((i+1))
        if [ -n "$KNOWN" ] && [ "$v" = "$KNOWN" ]; then
            printf "  %2d) %s   <-- проверено (ADB работает)\n" "$i" "$v"
        else
            printf "  %2d) %s\n" "$i" "$v"
        fi
    done
    printf "Номер (Enter = самая новая): "
    read -r n || true
    [ -z "${n:-}" ] && n=1
    VERSION="${VERS[$((n-1))]:-}"
fi
[ -n "$VERSION" ] || { echo "[!] версия не выбрана"; exit 1; }
URL="$MIRROR/$PLATFORM/$VERSION"
echo "[*] версия:    $VERSION"
echo "[*] качаю:     $URL/update.zip"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fetch "$URL/metadata.json" > "$TMP/metadata.json" || echo "[!] metadata.json не найден, продолжу без него"
fetch "$URL/update.zip" > "$TMP/update.zip" || echo "[!] зеркало не отдало update.zip"
SIZE=$(wc -c < "$TMP/update.zip" 2>/dev/null | tr -d ' ')
ZIPOK=0
[ "${SIZE:-0}" -gt 1000000 ] && ok_archive "$TMP/update.zip" && ZIPOK=1
if [ "$ZIPOK" = 0 ]; then
    # зеркало бывает битым/обрезанным (отдаёт ~33 КБ вместо 60 МБ) —
    # берём официальный URL из metadata.json
    ALT1=$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("downloadUrl",""))' "$TMP/metadata.json" 2>/dev/null || true)
    ALT2=$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("fallbackUrl",""))' "$TMP/metadata.json" 2>/dev/null || true)
    for ALT in "$ALT1" "$ALT2"; do
        [ -n "$ALT" ] || continue
        echo "[*] зеркало отдало ${SIZE:-0} б — качаю с официального URL: $ALT"
        curl -fsSL --retry 3 --connect-timeout 15 -o "$TMP/update.zip" "$ALT" || continue
        SIZE=$(wc -c < "$TMP/update.zip" | tr -d ' ')
        if [ "$SIZE" -gt 1000000 ] && ok_archive "$TMP/update.zip"; then
            ZIPOK=1
            break
        fi
    done
fi
[ "$ZIPOK" = 1 ] || { echo "[!] не удалось получить прошивку (размер ${SIZE:-0} б, zip: $ZIPOK)"; exit 1; }
# контрольные суммы
read -r CRC SHA < <("$PY" - "$TMP/update.zip" <<'PY'
import sys, zlib, hashlib
p = sys.argv[1]
crc, sha = 0, hashlib.sha256()
with open(p, "rb") as f:
    for chunk in iter(lambda: f.read(1 << 20), b""):
        crc = zlib.crc32(chunk, crc)
        sha.update(chunk)
print(crc & 0xFFFFFFFF, sha.hexdigest())
PY
)
echo "[*] размер:    $SIZE б, crc32 $CRC"
# сверяем с metadata.json (если там есть crc32)
if [ -s "$TMP/metadata.json" ]; then
    META_CRC=$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("crc32",""))' "$TMP/metadata.json" 2>/dev/null || true)
    META_VER=$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("version",""))' "$TMP/metadata.json" 2>/dev/null || true)
    [ -n "$META_VER" ] && [ "$META_VER" != "$VERSION" ] && echo "[!] внимание: в metadata.json версия $META_VER, а папка $VERSION"
    if [ -n "$META_CRC" ] && [ "$META_CRC" != "$CRC" ]; then
        echo "[!] crc32 не совпал с metadata.json ($META_CRC) — файл битый"; exit 1
    fi
fi
mkdir -p "$FW_DIR"
DEST="$FW_DIR/$VERSION.zip"
mv "$TMP/update.zip" "$DEST"
cat > "$FW_DIR/manifest.json" <<JSON
{
  "platform": "$PLATFORM",
  "version": "$VERSION",
  "filename": "$VERSION.zip",
  "size": $SIZE,
  "crc32": $CRC,
  "sha256": "$SHA",
  "note": "скачано install-firmware.sh с $MIRROR"
}
JSON
# чистим старые zip, чтобы сервер не взял не тот файл
find "$FW_DIR" -maxdepth 1 -name '*.zip' ! -name "$VERSION.zip" -print -delete | sed 's/^/[*] удалён старый: /'
echo
echo "[OK] прошивка: $DEST"
echo "     manifest: $FW_DIR/manifest.json"
echo "Дальше: touch run/downgrade && ./stop.sh; sudo ./start.sh  (или .\\stop.ps1 + .\\start.ps1 на Windows)"
