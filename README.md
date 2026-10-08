# CallNotifier

Система оповещения о вызовах и мониторинга складов для типографии. Распределённая клиент-серверная архитектура: сервер с TTS-движком и веб-админкой, легковесные клиенты на рабочих местах.

![Version](https://img.shields.io/badge/version-1.1.0-blue)
![Python](https://img.shields.io/badge/python-3.11.9-blue)

---

## 📖 О проекте

CallNotifier принимает события от внешних систем (АСистем), генерирует голосовые оповещения через Silero TTS и рассылает их на рабочие места. Дополнительно отслеживает заполняемость складов готовой продукции и уведомляет коммерческий отдел о превышении порогов.

**Ключевые возможности:**

- 🎙️ Синтез речи Silero TTS v5.5 (русский, офлайн).
- 🔔 Мгновенная рассылка вызовов на клиентские терминалы.
- 🔁 Периодические напоминания по висящим вызовам.
- 🏭 Мониторинг складов с разделением на мгновенные и интервальные алерты.
- 🌐 Веб-админка с полным CRUD по вызовам, типам и складам.
- 💻 Пер-клиентские фильтры типов уведомлений.
- 📦 Механизм версий с бейджем «доступно обновление».
- 🛡️ Устойчивость к сетевым сбоям (экспоненциальный backoff).

---

## 🏗️ Архитектура

```
┌────────────────────────┐          ┌────────────────────────┐
│      СЕРВЕР            │          │      КЛИЕНТ            │
│  (главный ПК цеха)     │          │  (рабочие места)       │
│                        │  HTTP    │                        │
│  • Flask REST API      │◄────────►│  • Tkinter GUI         │
│  • Silero TTS          │          │  • pygame.mixer        │
│  • SQLite (calls.db)   │          │  • Локальный кэш WAV   │
│  • WarehouseManager    │          │  • Автопереподключение │
│  • Веб-админка /admin  │          │                        │
└────────────────────────┘          └────────────────────────┘
         ▲
         │ POST /api/v1/incoming
         │
    ┌────────────────┐
    │    АСистем     │
    │ (внешняя ИС)   │
    └────────────────┘
```

---

## 🖥️ Сервер

### Требования

- Python **3.11.x**
- Windows 10/11
- ~2 ГБ ОЗУ (за счёт Silero TTS)
- ~1.5 ГБ диска (модель TTS + кэш)

### Установка

```powershell
# 1. Клонировать репозиторий
git clone https://github.com/dessernn-sys/CallNotifier.git
cd CallNotifier

# 2. Создать venv и активировать
python -m venv venv
.\venv\Scripts\Activate.ps1

# 3. Установить зависимости
pip install -r requirements_server.txt

# 4. Скопировать шаблоны конфигов и настроить
Copy-Item config_server.template.json config_server.json
Copy-Item config_admin.template.json  config_admin.json
# Отредактировать оба файла: реальный host, port, admin_password

# 5. Запустить сервер
python server.py
```

### Конфигурация

**`config_server.json`** — настройки сервера:
```json
{
  "http_server": { "host": "0.0.0.0", "port": 5499 },
  "display": { "cache_ttl_days": 3 },
  "warehouse": { "enabled": true, "warehouses": [] }
}
```

**`config_admin.json`** — пароли админки:
```json
{
  "admin_password": "ваш-надёжный-пароль",
  "dispatcher_password": "пароль-диспетчера"
}
```

### Веб-админка

Открыть: `http://<host>:<port>/admin`
Аутентификация: HTTP Basic Auth (логин игнорируется, важен пароль из `config_admin.json`).

**Вкладки:**
- **Дашборд** — статус подсистем, активные вызовы, автообновление 5 сек.
- **Создать вызов** — ручная эмуляция АСистем, режим пакетной рассылки.
- **Типы вызовов** — CRUD с защитой от удаления используемых.
- **Склады** — полный CRUD, ручное изменение значения, форсированная проверка.
- **Логи** — хвост `notifier_server.log`, живой поток, скачивание.

---

## 💻 Клиент

### Для разработки (из исходников)

```powershell
# 1. Создать venv для клиента
python -m venv venv_client
.\venv_client\Scripts\Activate.ps1

# 2. Установить зависимости
pip install -r requirements_client.txt

# 3. Скопировать шаблон конфига
Copy-Item config_client.template.json config_client.json
# Отредактировать host/port сервера

# 4. Запустить
python client.py
```

### Для развёртывания на рабочих местах (`.exe`)

Готовые `.exe`-сборки лежат в архиве релизов. Для конкретного рабочего места:

```
C:\CallNotifier\
├── CallNotifierClient-1.1.0.exe
├── config_client.json      ← настроить host/port сервера
└── (опционально) logo.png   ← если клиент не берёт лого с сервера
```

**Хранение данных:**
- Конфиг — рядом с `.exe`.
- Логи и кэш — в `%APPDATA%\CallNotifier\`.

**Управление:**
- **F1** — настройки (IP/порт сервера, TTL кэша, фильтр типов).
- **Esc** — переключение полноэкранного режима.

### Сборка `.exe`

Требуется PyInstaller **6.22.3** и клиентский venv:

```powershell
.\venv_client\Scripts\Activate.ps1

pyinstaller --noconfirm --clean `
    --name CallNotifierClient `
    --windowed `
    --onefile `
    --add-data "config_client.json;." `
    --hidden-import pygame `
    --hidden-import tkinter `
    --collect-submodules pygame `
    client.py
```

Готовый `.exe` — в `dist/CallNotifierClient.exe`. Переименовать в `CallNotifierClient-<версия>.exe` и положить в `static/client/` на сервере для раздачи клиентам.

---

## 📡 API

### Публичное

| Метод | Маршрут | Назначение |
|---|---|---|
| POST | `/api/v1/incoming` | Приём событий от АСистем |
| GET | `/api/v1/sync?types=...` | Синхронизация клиента |
| GET | `/api/v1/audio/<filename>` | Скачивание WAV |
| GET | `/api/v1/version` | Актуальная версия клиента |
| GET | `/health` | Health-check |

**Заголовок `X-Client-Id`** (формат `hostname-username`) обязателен для корректного пер-клиентского трекинга напоминаний.

### Пример: создать вызов вручную

```bash
curl -u admin:<пароль> -X POST http://<host>:5499/admin/api/calls \
    -H "Content-Type: application/json" \
    -d '{"external_id":"manual-001","type":"manager","name":"Иванов","equipment":"Станок №5"}'
```

---

## 🗂️ Структура проекта

```
CallNotifier/
├── server.py                    # точка входа сервера
├── client.py                    # точка входа клиента
├── config.py                    # ClientConfig / ServerConfig / AdminConfig
├── database.py                  # SQLite, CRUD
├── http_server.py               # Flask API + админка
├── tts_service.py               # Silero TTS v5.5
├── warehouse_manager.py         # складской мониторинг
├── audio.py                     # клиентский AudioManager
├── logger.py                    # логирование с разделением по роли
├── gui/                         # Tkinter GUI клиента
│   ├── main_window.py
│   ├── cards.py
│   ├── dialogs.py
│   └── warehouse_display.py
├── static/
│   ├── admin/                   # веб-админка (HTML/CSS/JS)
│   ├── client/                  # релизы .exe
│   ├── logo.png                 # логотип
│   └── index.html               # веб-клиент мониторинга
├── docs/
│   └── status_archive/          # архив PROJECT_STATUS.md
├── config_*.template.json       # шаблоны конфигов
├── requirements_client.txt
├── requirements_server.txt
├── CHANGELOG.md
├── PROJECT_STATUS.md
└── README.md
```

---

## 📦 Релизы

Версионирование — [Semantic Versioning](https://semver.org/lang/ru/).

- **`CHANGELOG.md`** — история изменений.
- **`PROJECT_STATUS.md`** — текущий статус и дорожная карта.
- **Архивы релизов** — хранятся отдельно (на Яндекс.Диске в `CallNotifier_Releases/`).

### Консервация релиза

```powershell
# Запустить из корня проекта
.\make_release.bat 1.1.0
```

Скрипт создаст папку релиза с `.exe`, `.sha256`, `requirements_locked`, `CHANGELOG.md` и `source.zip`.

---

## 🔧 Разработка

### Стек

- **Python 3.11.9**
- **Flask** — REST API
- **Silero TTS v5.5** — синтез речи
- **pygame** — воспроизведение звука на клиентах
- **Tkinter** — GUI клиента
- **SQLite** — хранилище
- **PyInstaller 6.22.3** — сборка `.exe`

### Полезные команды

```powershell
# Проверка синтаксиса всех модулей
python -m py_compile *.py gui/*.py

# Просмотр дерева проекта
python tree.py

# Тест API
curl -u admin:<пароль> http://<host>:5499/admin/api/status
```

---

## 📞 Контакты

- **Автор:** Сергей Почтеннов
- **Организация:** Типография "Кварц"
- **Внутренний проект** — для вопросов обращайтесь к администратору системы.

---

## 📄 Лицензия

Проприетарное программное обеспечение. Внутреннее использование.