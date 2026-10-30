# Yandex Station — root / ADB kit (Windows · Linux · macOS)

Стенд для получения ADB/root на **своей** Яндекс Станции в домашней сети.
Поднимает DNS + HTTPS, в ответ на `/get_sync_info` отдаёт
`adbd.remoteActivation=true`, и колонка включает `:5555`.

Все три платформы равнозначны — выбирай свою:

| | Windows | Linux | macOS |
|---|---|---|---|
| Стенд | `start.ps1` | `start.sh` | `start.sh` |
| Остановить | `stop.ps1` | `stop.sh` | `stop.sh` |
| Проверить ADB | `check.ps1` | `check.sh` | `check.sh` |
| Прошивка | `install-firmware.ps1` | `install-firmware.sh` | `install-firmware.sh` |
| DNS | встроенный в сервер (`--dns`) | `dnsmasq` | `dnsmasq` |

> ⚠️ Только свои устройства и своя сеть. Сброс снимает привязку аккаунта,
> неудачная прошивка может превратить колонку в кирпич. Всё на свой риск.

---

## Что проверено

| Модель | Статус |
|---|---|
| Yandex Station Lite 1 (`yandexmicro`, YNDX-00025, Amlogic A113X) | ✅ работает |
| Yandex Mini (`yandexmini`, `1.305.4.16.3237824040.20250721`) | ✅ работает |
| Yandex Station Midi (`cucumber`, с часами) | ✅ работает |
| Yandex Mini 2 | ✅ рутована (версия прошивки не зафиксирована) |
| Yandex Station Max (`yandexstation_2`, YNDX-0008) | ✅ ADB на `5000000921`; root/ Permissive — отдельным эксплойтом |

- **Мини**: даунгрейд проверен до `1.305.4.16.3237824040.20250721` (ставили с `1.3.4.12…`),
  и уже с этой прошивки включается ADB через `/get_sync_info` → `:5555`.
- На живой колонке: `adb connect <IP>:5555` → `adb shell id` → `uid=0(root)`.
- **Макс (YNDX-0008)**: это Android TV, а не Buildroot. На старых сборках
  quasar-сервис `adbd` не имеет `quasmodromNetworkActivateCmd`, поэтому ему
  нужен `network:false`; на `0921`/3.x ADB включает
  `com.yandex.tv.services.platform.adbEnabled` (см. [docs/sync-adb.md](docs/sync-adb.md)).
  Проверено: `adb connect` работает, `settings get global adb_enabled` = `1`.

---

## Требования

Общее для всех платформ — **роутер, где можно вручную поменять DNS** (или раздать
эту машину как DNS по DHCP). Железо любое: ПК, ноутбук, мини-ПК, Raspberry Pi.

**Windows**
- Windows 10/11, права администратора (скрипт сам попросит UAC);
- Python 3.8+ с python.org, галочка **Add python.exe to PATH**;
- свободные порты 53 (UDP/TCP), 80, 443 (порт 53 иногда держит служба ICS);
- `dnsmasq` не нужен — DNS встроен в сервер.

**Linux**
- `dnsmasq`, `python3` (OpenSSL ≥ 1.1.1), `openssl`, `dig`;
- свободные порты 53, 80, 443 (`systemd-resolved`/`avahi` не должны мешать);
- `sudo` для запуска.

**macOS**
- `dnsmasq` (`brew install dnsmasq`), `python3`, `openssl`, `dig`;
- свободные порты 53, 80, 443;
- `sudo` для запуска.

Плюс на любой платформе: `adb` в PATH (или `adb.exe` в `tools\` / свой путь в `check`),
колонка в той же сети и физический доступ к ней.

---

## Быстрый старт

### Windows

```powershell
cd C:\lite-kit
.\start.ps1        # UAC, firewall, DNS + HTTPS, покажет IP и проверит DNS
```

### Linux

```bash
cd ~/lite-kit
sudo ./start.sh    # поднимет dnsmasq + HTTPS, покажет IP и проверит DNS
```

### macOS

```bash
cd ~/lite-kit
sudo ./start.sh    # то же: dnsmasq + HTTPS
```

Дальше одинаково на всех платформах:

1. На роутере поставь **DNS = IP этой машины** (скрипт его печатает).
2. Сбрось колонку в factory: выдерни питание → включи → зажми **кнопку паузы**
   (▶/⏸) на ~7–10 с до жёлто-салатового индикатора → дождись Алисы.
3. В приложении «Дом с Алисой» добавь устройство, дойди до выбора Wi-Fi
   и подключи к своей сети (DNS уже указывает на машину стенда).
4. В логе появится `>>> [SYNC-REAL→] payload …, adbd: … =on`
   (или `[SYNC→]`, если Яндекс ответил 429).
5. Проверь ADB:

| Windows | Linux / macOS |
|---|---|
| `.\check.ps1` | `./check.sh` |

```bash
adb connect <IP-колонки>:5555
adb shell id
```

Остановить стенд:

| Windows | Linux / macOS |
|---|---|
| `.\stop.ps1` | `./stop.sh` |

и верни на роутере DNS в «Автоматически».

---

## Прошивка: скачать нужную версию

Скрипт качает прошивку с зеркала и кладёт её по платформе:

```
firmware/
  yandexmicro/0.369.1.46…297.zip + manifest.json
  yandexmini/1.305.4.16…20250721.zip + manifest.json
  yandexmini_2/…
```

Внутри `<platform>/manifest.json` — platform, version, filename, size, crc32, sha256.
Стенд при `/check_updates` берёт прошивку **по платформе колонки** (`platform=` в запросе)
и предлагает её только если версия отличается. Чужая модель манифест не получает —
запрос уходит в Яндекс.

Старая плоская раскладка `firmware/<версия>.zip` тоже работает: если платформы в
`manifest.json` нет, стенд спрашивает зеркало (`MIRROR_URL`), к какой платформе
относится эта версия, и сверяет с `platform=` колонки.

### Интерактивно (меню по цифрам)

Windows:
```powershell
.\install-firmware.ps1
```
Linux / macOS:
```bash
./install-firmware.sh
```

Меню: сначала платформа (`1) bergamot … 15) yandexmicro …`), потом версия
(сверху новые, проверенные помечены `<-- проверено (ADB работает)`).
Enter = значение по умолчанию.

### Из CLI — по имени

```powershell
.\install-firmware.ps1 -Platform yandexmini -Version 1.305.4.16.3237824040.20250721
```
```bash
./install-firmware.sh -p yandexmini -v 1.305.4.16.3237824040.20250721
```

### Из CLI — по номерам из меню

```powershell
.\install-firmware.ps1 -Platform 15 -Version 3
```
```bash
./install-firmware.sh -p 15 -v 3
```

Дополнительно:

| Windows | Linux / macOS |
|---|---|
| `.\install-firmware.ps1 -List` | `./install-firmware.sh --list` |
| `.\install-firmware.ps1 -Mirror http://…:62891` | `./install-firmware.sh -p … -v …` с `MIRROR_URL=…` |

Проверенные версии: `yandexmicro` → `0.369.1.46.4778262442.20261013.297`,
`yandexmini` → `1.305.4.16.3237824040.20250721`. Старые `.zip` удаляются **внутри
папки этой платформы** (`firmware/<platform>/`), другие модели не затрагиваются.

Если зеркало отдаёт обрезанный файл (типичный симптом — ~33 КБ вместо 60 МБ),
установщик сам скачивает сборку с официального URL из `metadata.json` и сверяет `crc32`.

### Раздача прошивки — автоматически по версии

Ничего включать не нужно: если в `firmware/<platform>/` лежит `.zip`, а версия
колонки (из `check_updates?version=…` и `platform=…`) с ним **не совпадает**,
стенд отдаёт манифест и файл — **в том числе для factory-прошивок**.
Если файла для этой платформы нет (или версия совпала, или стоит `run/no_downgrade`) —
локальный `hasUpdate:false`, в Яндекс не ходим.

```
>>> [OTA-AUTO] у колонки 1.3.4.12…, у нас 1.305.4.16… — отдаю манифест прошивки
>>> [OTA] файл отдан ПОЛНОСТЬЮ
```

Полностью запретить раздачу (и обновления) — флаг `run/no_downgrade`:
| Windows | Linux / macOS |
|---|---|
| `New-Item run\no_downgrade -ItemType File -Force` | `touch run/no_downgrade` |

Вернуть — удалить флаг и перезапустить стенд.

---

## После root: Entware и `opkg`

Если обвязка джейлбрейка экспортирует `LD_PRELOAD`/`LD_LIBRARY_PATH`, `opkg`
падает (`CANNOT LINK EXECUTABLE`, segfault). Правильная обёртка — снять оба
только для `opkg`, `PATH` оставить:

```sh
opkg() { PATH="$OPT/bin:$OPT/sbin:$PATH" env -u LD_LIBRARY_PATH -u LD_PRELOAD "$OPKG" "$@"; }
```

Подробно (куда положить, fallback, проверка): [docs/root-opkg.md](docs/root-opkg.md).

---

## Sync: что отдаётся колонке

- `200` от Яндекса → отдаём **его же ответ** (родные поля модели), но `adbd`
  **перезаписываем** на `true` в `config.system_config`, `config.device_config`,
  `config` и на верхнем уровне;
- `429`/`5xx` → до 3 повторов с паузой 2.5 с, затем локальный шаблон с `adbd`;
- не JSON/обрыв → локальный шаблон с `adbd`.

Колонка **всегда** получает `200`. Подробно — [docs/sync-adb.md](docs/sync-adb.md).

### Логи

- `log/server.log` — консольный лог (всё вместе);
- `log/yandex.log` — **только запросы, ушедшие в Яндекс** (метод, путь, статус, размер);
- `log/local.log` — **только то, что отдали локально** (sync с `adbd`, авто-прошивка, OTA, fallback, `generate_204`, кэш);
- `log/dns.log` — **каждый DNS-запрос** (клиент, имя, тип, ответ) — на Windows тоже.

Локальных заглушек (`register`, `set_device_capability`, `update_device_state`,
`hasUpdate:false`) больше нет: такие запросы уходят в настоящий Яндекс и
возвращаются как есть. Локально остаются только: sync с `adbd`, авто-манифест
прошивки при несовпадении версии, раздача файла прошивки и fallback при 429/5xx.

---

## Настройки (необязательно)

| Переменная | По умолчанию | Зачем |
|---|---|---|
| `LOCAL_IP` | определяется сам | основной IP (в манифесте `fallbackIp`); DNS отвечает IP из подсети клиента, если их несколько (LAN + хот-спот) |
| `COLUMN_IPS` | пусто | IP колонок через запятую, если автоопределение не сработало |
| `SYNC_RETRIES` / `SYNC_RETRY_DELAY` | `3` / `2.5` | повторы при 429/5xx |
| `MIRROR_URL` | `http://144.76.194.29:62891` | зеркало прошивок |

Флаг-файлы (создать пустой файл, потом перезапуск):

- `SYNC_LOCAL=1` (env) — отдавать локальный шаблон sync вместо Яндекса;
- `run/no_downgrade` — запретить авто-раздачу прошивки из `firmware/`.

---

## Если что-то не так

**Сначала — диагностика в один архив.** Запусти и ответь на 3 вопроса
(что происходит, что дословно говорит колонка, когда началось):

```bash
./diag.sh                      # Linux / macOS
```
```powershell
.\diag.ps1                     # Windows
```

Скрипт соберёт: ответы, `log/yandex.log` / `local.log` / `dns.log` / `server.log`
(последние 5000 строк, токены замаскированы), DNS-ответы для
`quasar.yandex.net` / `yandex.ru` / `okko.tv` / `music.yandex.ru` (+ контроль
через `8.8.8.8`), слушателей `53/80/443`, процессы стенда, ответы самого стенда
(`generate_204`, `check_updates`, `sync`), содержимое `firmware/` (размер,
zip-магия, crc32, sha256 — сразу видно обрезанный файл 33212 б), дерево папок,
`run/`, состояние git, `config/` и сертификаты.

Если выбрал «колонка не видит интернет» (или у колонки адрес `169.254.x.x`),
в архив дополнительно попадут `dhcp.txt` (адрес, MAC, ARP, link-local) и
`advice.txt` — пошаговый чек-лист по DHCP: включён ли пул, нет ли старой
аренды/резервации для MAC колонки, изоляция клиентов, второй DHCP в сети,
и как заставить колонку **самой** взять свободный адрес.

Результат — `lite-kit-diag-<дата>_<время>.zip` **в корне репозитория**.
Его и отправляй: по архиву видно, на каком шаге сломалось.

Флаги: `--no-ask` (без вопросов), `--column <ip>`, `--out <папка>`
(в PowerShell: `-NoAsk`, `-Column`, `-Out`).


| Симптом | Что смотреть |
|---|---|
| Windows: `start.ps1` просит UAC и закрывается | согласись с UAC, окно откроется заново уже от админа |
| `порт 53 уже занят` | Windows: служба ICS; Linux: `systemd-resolved`/`avahi`; освободи 53 |
| Колонка «нет интернета» | идут ли `HEAD /generate_204 → 204` в `log/server.log`; DNS на роутере точно на IP машины |
| Колонка в `169.254.x.x` / «нет интернета» | DHCP не выдал адрес: смотри `advice.txt` из `diag`; удали старую аренду и резервацию для MAC колонки |
| Нет `:5555` после ребута | был ли `[SYNC…] … adbd=on`; подожди минуту; проверь `check` |
| `не смог определить IP` | задай явно: `LOCAL_IP=<ip>` (аналог `$env:LOCAL_IP` в PowerShell) |
| `unknown ca` | прошивка не принимает серт — ставь проверенную версию через `install-firmware` |

---

## Состав

```
start.ps1 / stop.ps1 / check.ps1        стенд, остановка, поиск колонки (Windows)
diag.ps1                               диагностика в один архив (Windows)
install-firmware.ps1                   меню/CLI установки прошивки (Windows)
start.sh / stop.sh / check.sh          то же для Linux/macOS
diag.sh                                диагностика в один архив (Linux/macOS)
install-firmware.sh                    то же для Linux/macOS
scripts/equasar_server.py              DNS + HTTP/HTTPS, sync, OTA
scripts/dnscheck.py                    проверка DNS-ответа стенда
scripts/sync_template.json             локальный шаблон ответа
scripts/test-429.py                    30+ проверок: 429/5xx, повторы, OTA
certs/                                 серты eMudhra (fullchain.pem + ключ)
config/dnsmasq.conf.tpl                шаблон DNS для Linux/macOS
firmware/<platform>/                   прошивка (<версия>.zip) + manifest.json
```

Прошивка **не входит** в репозиторий — качается `install-firmware.*`.

---

## Сторонние проекты

- **eQuasar** (исходный вектор): https://git.gay/iwakura/eQuasar
- **babybreakfix**: https://github.com/qwikly3/babybreakfix
- **YAM**: https://github.com/nyakokitsu/yam
- Зеркало OTA: `http://144.76.194.29:62891/<platform>/<version>/update.zip`

---

## Лицензия

Код стенда — MIT. Сертификаты и прошивки — собственность правообладателей,
в репозитории только публично известный набор сертов eQuasar и метаданные.
