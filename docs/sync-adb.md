# Sync с ADB для всех моделей

`/get_sync_info` всегда заканчивается `200` и `adbd.remoteActivation=true` —
независимо от того, что ответил Яндекс. Два режима:

| Режим | Что отдаём | Как включить |
|---|---|---|
| локальный шаблон (по умолчанию) | `scripts/sync_template.json` + `adbd` | ничего |
| настоящий ответ + `adbd` | ответ Яндекса для этой модели, `adbd` перезаписан на `true` | `touch run/sync_proxy` |

При `429`/`5xx` — до 3 повторов с паузой 2.5 с, затем локальный шаблон.
Проверено на Лайт 1 (`yandexmicro`) и Мини (`yandexmini`, `1.305.4.16…20250721`).

## Как это работает

1. Берётся **шаблон** ответа:
   - `scripts/sync_template.json` — базовый, от «обычных» станций (Lite/Midi), лежит в репозитории;
   - иначе `log/sync_template.json` — свой сгенерированный;
   - дамп `log/sync_real.json` используется **только** с `USE_SYNC_DUMP=1`
     (нужен, если хотите повторить ответ конкретной модели, например Mini);
   - если шаблона нет вовсе — минимальный встроенный ответ.
   По умолчанию Mini-дамп **не** подмешивается.
2. Из шаблона **вырезается приватное**:
   - `glagol.security` (локальный сертификат и приватный ключ станции);
   - `config.account_config.user_wifi_config` (хеш Wi-Fi);
   - `config.device_config.location` (координаты).
3. В ответ вписывается ADB:
   ```json
   "adbd": {"remoteActivation": true, "network": true, "fullDurationMs": 31536000000}
   ```
   сразу в `config.system_config`, `config.device_config`, `config` и на верхний уровень —
   чтобы сработало на любой ветке прошивки.
4. Служебные `POST /set_device_capability` и `/update_device_state` для колонки
   тоже отвечаются локально (`{"status":"ok"}`) — больше никаких 429.
5. Если Яндекс отвечает `429`/`5xx` на любой **другой** запрос колонки, ошибка
   не пробрасывается: sync-пути получают локальный конфиг с `adbd`, остальные —
   `200 {"status":"ok"}`. То есть колонка в любом случае видит успешный синк.

## Настоящий ответ Яндекса + ADB (рекомендуется)

Режим `SYNC_PROXY`: sync **проксируется в Яндекс**, в полученный ответ вписывается
`adbd`, и колонка получает родной ответ для своей модели (со всеми её полями).

```bash
cd ~/lite-kit
./stop.sh
touch run/sync_proxy && sudo ./start.sh   # или: SYNC_PROXY=1 sudo ./start.sh
```

- `200` от Яндекса → отдаём его же + `adbd` во всех ветках;
- `429`/`5xx` → до `SYNC_RETRIES` попыток (по умолчанию 3) с паузой `SYNC_RETRY_DELAY`
  (по умолчанию 2.5 с); если всё равно лимит — локальный шаблон с `adbd`;
- не JSON/обрыв → локальный шаблон с `adbd` (колонка всё равно видит `200`);
- сырой ответ **не сохраняется**. Нужен дамп — запусти с `DUMP_SYNC=1`
  (в нём приватные поля станции — файл не коммитить и не выкладывать).

Выключить и вернуться к чистому локальному шаблону:

```bash
rm -f run/sync_proxy
./stop.sh && sudo ./start.sh
```

## Что видно в логе

На каждый sync пишется и запрос, и то, что уходит колонке:

```
<<< [SYNC←] GET /get_sync_info?device_id=…&platform=yandexmini от 192.168.0.171
    заголовки: User-Agent: …; Accept: …
>>> [SYNC-REAL→] payload 10846b, источник: настоящий ответ Яндекса, adbd: config.system_config.adbd=on, config.device_config.adbd=on, config.adbd=on, top.adbd=on
    {"status":"ok","config":{...}}      ← тела целиком, секреты заменены на <скрыто>
```

Маскируются: `*token*`, `*secret*`, `*password*`, `*private*`, `*auth*`, `*cookie*`,
`wifi_hash`, `latitude`, `longitude`, `certificate`.

## Проверка

```bash
curl -k -s -H "Host: quasar.yandex.net" \
  "https://127.0.0.1/get_sync_info?device_id=test&platform=yandexmini" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);\
print('adbd:', d['config']['system_config']['adbd']);\
print('секреты вырезаны:', 'glagol' not in d and 'user_wifi_config' not in d['config']['account_config'])"
```

Ожидаемо: `adbd` с `remoteActivation: True`, секретов нет.

## Станция Макс (`yandexstation_2`, YNDX-0008) — отдельная ветка

Макс — это **Android TV (YaOS_X)**, а не Buildroot/quasar. Одного
`config.system_config.adbd` ему недостаточно, и на разных прошивках работают
разные пути:

| Прошивка | Что включает ADB |
|---|---|
| 1.56.x / старые Android-сборки | quasar-сервис `adbd` читает `system_config.adbd` |
| 0921 / 3.x YaOS TV | приложение `com.yandex.tv.services.platform` ставит `Settings.Global.ADB_ENABLED=1` |

### Почему `adbd.network=true` не работает на старом Максе

В `quasar.cfg` платформ `yandexstation_2` и `yandexstation` есть
`quasmodromActivateCmd`, но **нет** `quasmodromNetworkActivateCmd`:
`yasrc/smart_devices/.../quasar-prod.cfg`. Сервис `AdbService` при
`network=true` выбирает именно network-команду — пустую — и ничего не
запускает. Поэтому для этих двух платформ сервер отдаёт `network:false`;
`activate_adb.sh --quasmodrom activate` всё равно прописывает
`service.adb.tcp.port=5555` и поднимает `adbd`.

### Почему на 0921/3.x нужен TV-ключ

Приложение `YandexTvPlatformServices` читает
`system_config.com.yandex.tv.services.platform`:

```json
"com.yandex.tv.services.platform": {
  "adbEnabled": true,
  "developmentSettingsEnabled": true
}
```

и выставляет `Settings.Global.ADB_ENABLED=1` /
`DEVELOPMENT_SETTINGS_ENABLED=1`. В `/vendor/build.prop` Макса уже есть
`service.adb.tcp.port=5555`, поэтому после этого `adbd` слушает сеть.
Сервер вписывает этот ключ во все sync-ответы (и в настоящий ответ Яндекса,
и в локальный шаблон).

### Проверено на живом YNDX-0008

После OTA на `PPR1.180610.011.5000000921`:

```text
adb connect 192.168.42.16:5555     # connected
adb shell getprop ro.build.display.id
# PPR1.180610.011.5000000921
adb shell settings get global adb_enabled
# 1
adb shell getprop service.adb.tcp.port
# 5555
adb shell getprop persist.sys.usb.config
# adb
```

ADB переживает перезагрузку: `adb_enabled` и `persist.sys.usb.config`
лежат в `/data`/persist, а `service.adb.tcp.port` — в `vendor/build.prop`.
После возврата DNS на роутере ADB не пропадает (в отличие от некоторых
Buildroot-моделей).

> Одинаковый `ro.build.display.id` **не означает одинаковое состояние**.
> OTA меняет `/system` и `/vendor`, но не `/data`, `/persist` и U-Boot env.
> Новый Макс с завода может уже иметь `adb_enabled=1`,
> `persist.sys.usb.config=adb` или `ro.boot.rh_unlock=1`; сброшенный
> YNDX-0008 — нет. Поэтому «на 0052/0053 тот же скрипт сработал» может
> означать не другой парсер, а другое factory/persistent-состояние или
> другой ответ Яндекса для этого device_id. Сервер теперь пишет
> `[SYNC-NATIVE]` — значения `adbd`/`tv.platform` **до** инъекции, чтобы
> это сравнить на двух колонках.

> Root на Максе не выдаётся через `adb root` (production adbd). На 0921
> работает отдельный локальный эксплойт CVE-2022-36449: он даёт `uid=0`
> одному процессу и runtime Permissive. Это не часть lite-kit.

## Важно

- `log/sync_real.json` содержит приватный ключ станции — **не коммитить** и не выкладывать.
  В шаблон он не попадает (вырезается).
- Локальный sync включает ADB на прошивках, которые читают это поле.
  Проверено: **Лайт 1** (`yandexmicro`) и **Мини** (`yandexmini`,
  `1.305.4.16.3237824040.20250721`) — отдельный способ для Мини не нужен.
- **Макс** (`yandexstation_2`): ADB включается через
  `com.yandex.tv.services.platform.adbEnabled` (0921/3.x) или через
  `adbd.network=false` (старый quasar). Проверено на YNDX-0008,
  `5000000921`.
