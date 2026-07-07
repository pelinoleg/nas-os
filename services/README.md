# services/ — docker-стеки NAS (лежат рядом с nas-wizard.sh)

Каждый сервис — отдельная подпапка с файлом `docker-compose.yml`
(или `docker-compose.yaml` / `compose.yml` / `compose.yaml`).

`nas-wizard.sh --stage docker` сканирует эту папку, показывает чеклист
«какие поднять», и делает `docker compose up -d` по выбранным (idempotent).
`~/nas-config/scripts/deploy.sh` поднимает всё разом.

## Соглашения (по ТЗ)

- **Фиксированные теги образов**, НЕ `latest` — чтобы не ловить внезапные обновления.
- `restart: unless-stopped` у каждого сервиса.
- Конфиги контейнера → `/opt/docker/<service>/...`
- Большие данные (медиа, документы) → `/mnt/storage/<service>/...` (пул mergerfs).
- Секреты — в файле `.env` рядом с compose (в git НЕ коммитить; добавьте в .gitignore).

## Добавить свой сервис

```bash
mkdir -p services/immich
$EDITOR services/immich/docker-compose.yml
sudo ./nas-wizard.sh --stage docker      # найдёт и предложит поднять
```

## Что здесь есть (готовые шаблоны, фикс. теги)

| Сервис | Порт | Назначение |
|--------|------|-----------|
| `dockge/` | 5001 | Менеджер docker-compose стеков (веб UI) |
| `dozzle/` | 8083 | Логи контейнеров в реальном времени |
| `scrutiny/` | 8084 | Мониторинг SMART дисков (укажите диски в `devices:`) |
| `syncthing/` | 8384 | Синхронизация файлов |
| `nextexplorer/` | 3000 | Веб-файловый менеджер (задайте `SESSION_SECRET`!) |
| `example-service/…example` | — | Шаблон-заготовка (переименуйте в `docker-compose.yml`) |

Перед первым подъёмом проверьте в шаблонах: `SESSION_SECRET` (NextExplorer),
список дисков (Scrutiny), `PUBLIC_URL`/адрес Pi. Порты Dozzle и Scrutiny разведены
(оба по умолчанию 8080 → 8083 и 8084).
