# Kopia — снапшот-бэкапы

_История и подробности. Открывай, когда правишь окно Kopia, раннер снапшотов, Explorer или репозитории._
_Правила и грабли, которые важны ВСЕГДА, живут в `CLAUDE.md` — здесь только детали._

**Локальный тач-экран** (Waveshare 4.3″ DSI 800×480, ёмкостный `ft5x06`; драйвер не нужен —
панель поднимает `display_auto_detect=1`): `web/screen.html` — дашборд в материалах панели
(обои + стекло, ВСЕГДА тёмная тема из `themeProfiles.dark`, что бы ни стояло в браузере).
Стр. 1: полукруглые спидометры CPU/темп/память/диск (окраска по порогам: низко зелёное →
жёлтое → красное), плашки **Mirror | Kopia** (верхний ряд — оба движка бэкапа рядом; живой
прогресс: %, скорость/фаза, текущая папка, задача N из M) и **События | USB-импорт**,
полосы доступности 24 ч и 30 дн. Стр. 2 (свайп/точки):
**Диски** (полоски заполнения, системный подписан «System»), трафик vnstat, обновления apt+образы+cron, графики CPU/темп/сеть за сутки (сетка,
шкала, пик и среднее — без чисел график бесполезен). Тап по ЛЮБОЙ плашке → детали на весь экран
(заголовок + крестик), лист перерисовывается на каждом опросе с сохранением прокрутки. Стр. 4: «Следующие
запуски» (бэкапы по расписанию — и Mirror (`kind:mirror`), и Kopia (`kind:snapshot`) — + cron) и
«Здоровье дисков» (SMART-вердикт, температура, наработка).
**Раскладка 2026-07-24 (по просьбе пользователя):** главная = `Mirror | Kopia` / `События |
USB-импорт`; Диски уехали на стр. 2 на место **Time Machine — её с экрана убрали совсем**
(не используется: карточка, лист, `.tmb/.tmcol`-CSS и расчёт `tm` в `_screen_page2` удалены —
минус `tm_status()` из payload). Карточка **Kopia** (`#c_kp`, `renderKopia`, лист `kopia`)
повторяет язык Mirror: идёт прогон → крупный вид (%, ФАЗА `KP_PHASE` snapshot/replicate/drill/
waiting, файлы, ETA, записано, Stop с двойным тапом `KPSTOP`), покоя → строки «точка · имя ·
размер · когда» + жёлтая плашка «Repository disk unplugged» (бэкапы с `usb_trigger` не краснеют —
им отсутствие диска ШТАТНО). Данные — `_screen_kopia()` в payload (`kopia[]`, кэш 8 с): читает
ТОЛЬКО файлы на диске (kopia.json, run-state, history) — ни одного открытия репозитория и ни
одного похода в сеть, поэтому годится для быстрого опроса; `_kp_next_run(bk)` — следующий слот
расписания (те же правила, что у `_kopia_tick`). Действия экрана: `a:"kopia"` / `a:"kopia_stop"`
(поле `id`), обе сбрасывают кэш `_SCR_KP`, чтобы плашка ожила на СЛЕДУЮЩЕМ опросе.
**Приложение «Kopia» (в разработке, план — заметка «Kopia — план приложения» V3, реализация с 2026-07-23).**
Снапшот-бэкапы с дедупом и 3-2-1 (запасная копия через `kopia repository sync-to`). Три
движко-независимые сущности в `/etc/nas-os/kopia.json` (root 600, секретная секция nasbackup
бэкапа настроек): Source {id,name,folders[],excludes[]}, Destination {id,name,kind:fs|rclone,
path|remote+remote_path} = один kopia-репозиторий, Backup {source,dest,dest2?,dest2_mode:sync|
independent,retention,schedule,usb_trigger,notify_ok/err,enabled,setup_*}. **Пароль репозиториев
ОДИН на все** (решение пользователя: хранится и показывается ОТКРЫТО — важна ненатеряемость, не
секретность), генерится при первом Destination (`_kp_ensure_password`). Готово (стадия 1,
2026-07-23): `install_kopia` в визарде (`api kopia`/`api kopia-update`, официальный GitHub-бинарь,
НЕ в авто-базе — ставится кнопкой из приложения; kopia 0.23.1); серверный модуль в nas-web.py
(перед POKE-блоком): `_kp(destid,args)` — обёртка с `--config-file /etc/nas-os/kopia/<id>.config`
+ KOPIA_PASSWORD/RCLONE_CONFIG в env (не argv) + `--log-dir` в кэш; кэш `storage_root()/.kopia-cache/
<dest>` (фолбэк /var/cache/kopia с капом 2G/1G — `--content-cache-size-mb`); dest create/connect
(fs: путь только /mnt|/media|/srv; rclone: remote из rclone.conf, `--rclone-exe`), **self-heal
connect** (`_kp_ensure_connected`: derived .config после переустановки пересоздаётся из сущности),
test (repository status --json), stats (`content stats --raw` — у него НЕТ --json; + snapshot list
→ data_bytes/дедуп), forget (данные не трогает), CRUD sources/backups с guard'ами «in use by».
API: GET `/api/kopia/status` (всё одним запросом, без сети); POST `install|update`, `source/save|
delete`, `dest/create|connect|test|stats|delete`, `backup/save|delete`. Live-проверено: fs-репо на
T7 (`/media/nas/t7-4TB/kopia-repo`, dest 0b08dd) + rclone-репо на pcloud (`pcloud:kopia-repo`,
dest b3684c), пароль `nas-958b5442`. ГРАБЛИ: rclone-бэкенд kopia помечен «[Not maintained]» —
работает (проверено), но при проблемах смотреть сюда; `content stats --json` не существует
(парсим `--raw`). **Стадия 2 готова (2026-07-23): snapshot-раннер + 3-2-1.** CLI `kopia-snap`
(env KPS_BID) → транзиент-юнит `nas-kopia-<bid>`; state/log `~/nas-config/kopia-run-<bid>.{json,log}`,
история `kopia-history.json` (100 записей, фазы snapshot/replicate). `_kp_policy_apply` — retention/
compression(zstd)/ignores на каждую папку source (`kopia policy set`; ретеншен kopia применяет САМ
после снапшота). Снапшот всех папок одной командой + `--tags nasbk:<bid>` (фильтр в Snapshots по
бэкапу). Guard'ы старта: `_NbStartLock` (общий с rsync-app), один прогон на destination, source
непустой/существует, fs-dest statvfs+200MB. §9.2 guard в драйвере: `_kp_rsync_busy` — пересечение
папок с job'ами БЕГУЩЕГО rsync-прогона → фаза waiting (poll 20с, кап 2ч). dest2 sync-режим:
`repository sync-to --delete` (фаза replicate; провал реплики = warn «main copy OK, spare copy
failed» — снапшот не считается провалом, §6.16); independent-режим: второй полный снапшот в dest2.
`kp_dest_create(no_init=True)` — регистрация spare БЕЗ создания репо (sync-to сам населяет пустое
место; создавать репо в spare НЕЛЬЗЯ — sync-to откажет «incompatible data», проверено). API:
GET `run/status?b=`, `history`; POST `backup/run|cancel`. **ГРАБЛИ стадии 2 (все проверены живьём):**
(1) kopia пишет прогресс-счётчики ТОЛЬКО на TTY — драйвер держит stderr снапшота на pty
(`pty.openpty`), парсит `\r`-чанки `_KP_PROG_RE` («N hashed (X), uploaded Y, (P%) T left»),
stdout отдельным pipe (JSON-манифесты). **И ЭТОГО МАЛО: `--json` ГЛУШИТ прогресс совсем** —
нужен ЯВНЫЙ `--progress` рядом с `--json` (2026-07-24, доказано на изолированном репо: `--json`
= 0 строк прогресса, `--json --progress` = нормальные счётчики). Без него панель честно рисует
0% часами («долго думает»), а хуже — **сторож «нет вывода 20 мин» убивал ЗДОРОВЫЙ прогон**
(поймал живьём на первом снапшоте 20 ГБ в pcloud на 19-й минуте; удержал, подсунув `\n` в pty
прогона — сторож считает это активностью). Лечение двойное: флаг `--progress` + сторож теперь
перед убийством спрашивает у ЯДРА, жжёт ли процесс CPU (`_proc_cpu_time`, utime+stime из
`/proc/<pid>/stat`): молчание — не признак зависания, отсутствие CPU за 20 минут — признак.
ПРАВИЛО: таймаут «по тишине» вешать только вместе с независимой проверкой живости; (2) `policy set --clear-ignore` в ОДНОЙ команде с
`--add-ignore` затирает добавленное (clear применяется после add) — сброс отдельным вызовом;
(3) транзиент-юнит ОБЯЗАН получать `--setenv=SUDO_USER/HOME` (как rclone-драйверы) — без них
NAS_CONFIG в драйвере уезжает в /root/nas-config (state «не пишется», юнит «молчит»);
(4) sync-to на rclone-приёмник — ТОЛЬКО `--parallel 1`: параллельный MkdirAll через
`rclone serve webdav` гонится на общих родительских папках, pcloud отвечает 423 Locked
(10 ретраев впустую); fs-приёмник — parallel 4; (5) sync-to запускает `rclone serve webdav`
как дочку kopia — жив только на время команды. E2E доказано: snapshot 43 файла → реплика
на pcloud → **spare = полноценный репо** (Connect с тем же паролем, тот же uniqueIDHex, все
снапшоты читаются — §6.17); отмена посреди реплики работает. **Стадия 3 готова (2026-07-23):
`_kopia_tick` в monitor_loop** (слот-паттерн `_nb_sched_tick`): расписания daily/weekly с
**pending-очередью** (dest занят в слот → ретраи до 6ч в `kopia-state.json`); **USB-триггер §9.3**
(`present`-переходы fs-dest absent→present + `usb_trigger` бэкапа → автозапуск, флап-guard 30 мин);
weekly `maintenance run --full` + monthly `snapshot verify --verify-files-percent=1` на PRIMARY
dest'ы (sync-spare — байт-реплика, наследует maintenance главного; **first-sight штампы** — свежая
установка не молотит верификацию огромного репо сразу); долгое — ТОЛЬКО транзиент-юниты
`nas-kopia-{maint,vfy}-<destid>` (CLI `kopia-bg`, env KPB_KIND/KPB_DEST, стейт
`kopia-{maint,verify}-<destid>.json`), не в monitor-нити. Health раз в 30 мин: kp_stale (свежайший
ok/warn-прогон старше threshold дней), fs-dest пропал (кроме usb_trigger-бэкапов — их диску
ПОЛОЖЕНО отсутствовать). События каталога: `kp_run` (off/desk, гейт per-backup notify_ok),
`kp_err` (prio 1, notify_err), `kp_stale` (threshold 7), `kp_maint` (prio 1) + лейблы в notifyTab
(группа «Kopia (snapshot backups)»); раннер шлёт notify_event сам (как nb-драйвер), «spare failed»
идёт kp_err'ом при result=warn. API: POST `dest/maintenance|verify|bg-status`. Юнит-тесты: слот-дедуп,
pending queue/drain, first-sight, weekly-due, usb-триггер, stale — все зелёные; live maintenance
через юнит ok. **Стадии 4-5 готовы (2026-07-23): UI-окно `winKopia`** (`__kopia` в APPS, иконка —
родной логотип kopia в RAW_LOGOS, дал пользователь; каркас `.nbmain`+`.set-nav`, body flex-колонка).
Табы: **Overview** — hero-вердикт + грид `.rs-card`-карточек бэкапов (статус/направление/next run/
прогресс-бар живого рана) + карточка «New backup»; клик по карточке → **деталка** (переименование,
Run/Stop, карточки From/To/Spare, последние прогоны, настройки = те же секции, что в визарде) или
**Sources**-таб (карточки, диалог srcDlg: имя+папки через кастомный однопапочный пикер kpPick
`/api/kopia/browse`+excludes; delete заблокирован «in use by»), **Destinations**-таб (карточки
с точкой connected, Test/Stats/Maintenance/Remove на карточке; destDlg: Local disk|Cloud(rclone),
«Connect existing», spare создаётся с no_init), **Snapshots**-таб (чипы-фильтры по dest,
`/api/kopia/snapshots?d=` — список с тегом nasbk→имя бэкапа, группы по дням), **History**-таб
(глобальная лента с фазами), **Options** (пароль ОТКРЫТО+Copy, версия+Update, заметка про кэш).
Пассивный поллинг 3с — репейнт только при изменении JSON статуса, под визардом не репейнтит.
Экран установки при !installed (кнопка Install). Серверные дополнения: `kp_browse` (локальный
листинг для пикера, фильтр `_NB_ROOT_SKIP`), `kp_snapshots` (парс `snapshot list --json`, тег
`tag:nasbk`, UTC-время через `calendar.timegm` — НЕ `time.timezone`, DST). ГРАБЛИ UI: иконки
брать ТОЛЬКО из существующего словаря `P` (sliders/dots/cloud/left/up НЕ существуют — cog/
arrowup/chev-rotate/globe); `showMenu(x,y,items)` принимает КООРДИНАТЫ и items `{label,ic,act,dng}`,
не event. **Стадия 6 готова (2026-07-23): Snapshots — browse/restore/mount/delete.**
Навигация по снапшоту — ПО OBJECT ID (`kp_snap_ls`: `kopia ls -l <oid>`, у каждой папки в выводе
свой oid → никаких склеек путей; парсер `_KP_LS_RE`, имя после oid отделено 2+ пробелами;
`kp_snapshots` отдаёт `root`-oid). Restore: `kp_restore_start/status/cancel` + драйвер
`_kp_restore_cli` (CLI `kopia-restore`, env KPR2_*, юнит `nas-kopia-restore`, ОДИН за раз) —
`kopia restore <oid> <target> --skip-existing` (copy-only, существующие не трогаются), приёмник
только /mnt|/media|/srv|/home, прогресс под pty (`_KP_RST_RE` «Processed N (X) of M (Y) … (P%)»),
стейт/лог `kopia-restore.{json,log}`. Mount: `kp_mount_start/stop` — `kopia mount all
/mnt/kopia/<destid>` в юните `nas-kopia-mnt-<destid>` (ExecStopPost umount -l; fuse → дрель и
`_readonly_mounts` его уже пропускают); в статусе dests есть `mounted`. Delete:
`kopia snapshot delete --delete`. API: POST `snap/ls|delete|restore/start|restore/cancel`,
`mount|unmount`; GET `restore/status`; `kp_status().restore` — для полосы прогресса. UI Snapshots:
кнопка Mount/Unmount у выбранного dest (+строка «browse it in Files»), полоса Restoring…+Stop
(рисуется из статус-поллинга), у строки снапшота Browse (диалог `kpSnapBrowse`: крошки по
oid-стеку, клик по файлу = restore файла)/Restore…/корзинка; restore-цель выбирается kpPick →
confirmDlg. Live-проверено: ls по oid (root+подпапка), restore папки fonts на T7 (6 файлов,
result ok), mount → листинг снапшотов через ФС → unmount чисто, delete снапшота 6→5.
**Стадия 7 готова (2026-07-23): интеграции.** (1) **Disaster card** — секция «Kopia snapshot
backups» в `disaster_build()`: пароль ОТКРЫТО, список репозиториев, команды `repository connect`
+ restore (spare = полноценный репо), карта бэкапов source→dest(+spare). (2) **Restore-drill в
раннере** (`_kp_drill` + `_kp_drill_pick`): после каждого ok/warn-снапшота фаза `drill` — до 3
случайных файлов (случайный спуск по oid, кап 200МБ) восстанавливаются во временную папку и
sha256-сравниваются с источником; источник изменился (size разошёлся) → скип, не тревога;
расхождение при том же size → `kp_err` «restore drill mismatch» (тихая порча приёмника) + result
warn; итог в `phases.drill` {checked,ok,rot,dur}. (3) **Glance-плитка `kopia`** в GLANCE_TILES +
`_glance_tile`: running/never/возраст свежайшего ok-прогона (danger при failing-бэкапах, warn >2д)
+ note «N failing». Live: прогон с drill 3/3 byte-identical, tile отдаёт возраст, карта собирается.
**Стадия 8 готова (2026-07-23): аудит (2 параллельных ревью-агента: движок + UI) — все
подтверждённые находки исправлены, регресс 8/8 + guard-сьют 21/21 + e2e (snapshot→drill→sync,
independent-режим) зелёные.** Исправления ДВИЖКА: (1) HIGH data-loss — `sync-to --delete` мог
переписать ЧУЖОЙ репозиторий: kp_backup_save запрещает dest2(sync) = чей-то PRIMARY dest и шаринг
одного spare под РАЗНЫЕ primary; no_init-spare на непустую папку без kopia.repository* — отказ;
(2) HIGH — guard пустого источника в драйвере (пустая папка = отмонтированный mountpoint; снапшот
«0 файлов ok» дал бы ретеншену выесть настоящие копии — урок rsync-app): пустые скипаются, все
пустые = error; (3) HIGH — fs-пути создают только ЛИСТ (родитель обязан существовать — иначе
makedirs тихо строил дерево на SD-rootfs и реплика лилась в карту), spare перед sync проверяется
parent+statvfs 200MB, провал = «spare failed», не молчание; (4) гонки: `_KP_CFG_LOCK` вокруг всех
RMW kopia.json, flock у history (`kopia-history.json.lock`, кап 200), restore start под
`_NbStartLock`, drill-tmp per-PID; (5) run⟂maintenance теперь взаимный запрет (kp_run_start
проверяет maint/vfy-юниты); (6) ретеншен «все нули» → floor latest=1 (иначе kopia удалит ВСЁ);
(7) cancel: сперва cancel-файл + до 3с на финализацию драйвера (история/лог пишутся), stop только
потом; cancel завершённого — no-op; (8) слот расписания персистится в kopia-state.json (рестарт
панели в ту же минуту не даёт второй прогон); (9) health берёт last-ok из run-state (переживает
ротацию истории); (10) independent-spare тоже получает weekly maintenance/monthly verify;
kp_dest_forget гасит mount-юнит и отказывает при busy. Исправления UI: (1) CRITICAL — вложенные
диалоги делили #scrim (kpPick затирал разметку родителя → создание source/fs-dest было СЛОМАНО):
kpPick теперь СВОЙ оверлей `z-index:600` поверх; ПРАВИЛО: диалог, открывающий другой диалог, не
должен рисоваться в тот же #scrim; (2) load() фильтрует `_mock`-фолбэк api() (сбой сети показывал
«Install kopia» на настроенном боксе; offline-плашка вместо этого); (3) поллинг: полный repaint
только Overview; открытая деталка получает ТОЧЕЧНЫЙ апдейт прогресса (#kdProg, kdProgHTML),
переход running→done = полный repaint; Snapshots перерисовывается только при изменении restore-
состояния — формы настроек больше не затираются тиком; (4) настройки деталки перерисовываются из
СВОЕГО draft (New source/dest теперь виден выбранным и не теряется); (5) гонка фетча снапшотов
(быстрое переключение чипов вешало чужой список под чужие кнопки) — guard `want`; (6) визард:
Back/«Set up later» читают поля текущего шага (readStep), незаполненный драфт при Later честно
говорит «not created»; (7) файл-restore из браузера снапшота закрывает диалог перед пикером.
Принятые допущения: `_systemd_active` под нагрузкой может дать ложный «aborted» (общий с nb
паттерн); минутный слот, пропущенный из-за >60с тика — пропуск дня (pending-очередь покрывает
только «dest busy»). **Аудит №2 (2026-07-23, по заметке-плану V3 + живой UI-прогон).**
(1) Сверка плана пункт-в-пункт → добраны пробелы: kp_opts (`kopia-opts.json`: parallel→`--parallel`
снапшотов, cloud bwlimit→env `RCLONE_BWLIMIT` дочернему rclone (у kopia НЕТ своего тротлинга —
локальные диски не ограничиваются, честно написано в UI), кэш-капы→`kopia cache set`; API
GET/POST `/api/kopia/opts`, UI в Options); лог-хвост (25 строк) в записях истории + разворачивание
кликом в History; кнопка Run на карточке Overview; деталка: «last replicated N ago» на Spare,
вердикт drill (`x/y files verified`), «View snapshots →» (переход с фильтром по бэкапу),
«Repository stats» on-demand; Snapshots: чипы-фильтр ПО БЭКАПУ поверх dest-чипов + дельта размера
к предыдущему снапшоту того же пути; тумблер компрессии (секция Retention визарда/настроек);
Finish: чекбокс «Run the first snapshot now» (вкл. по умолчанию); destDlg: опция
«KOPIA-PASSWORD.txt рядом с данными» (`_kp_write_pwfile`, fs=файл/rclone=rcat, дефолт ВЫКЛ, §2);
Sources: кнопка Size (ленивый `du -sb -x`, POST source/size); Destinations: кнопка Cache
(размер + clear, POST dest/cache). Осознанные отклонения от заметки: «двойное подтверждение»
удаления dest = один confirmDlg (данные не трогаются вообще); GET verify/status = POST
dest/bg-status; плитка НАСТЕННОГО экрана не делалась (открытый вопрос §8 заметки); compression —
свойство БЭКАПА (kopia policy per-folder), не dest. (2) **Живой UI-прогон через CDP** (chromium
--headless + python3-websockets (apt) + Runtime.evaluate; сессия панели минтится записью токена в
`/etc/nas-os/sessions.json` + restart — и ОБЯЗАТЕЛЬНО отзывается после; ГРАБЛЯ: весь JS desktop
в IIFE — функции не глобальны, драйвить только ДОМ-кликами, `[data-did="__kopia"]` в доке);
50 скриншотов: все табы, деталка, визард все 6 шагов, вложенные диалоги, пустое приложение,
not-installed, repo-gone, empty-source. Найдено+исправлено: подсветка активного таба в `.set-nav`
не обновлялась (paint() не трогает nav → `syncNav()`); fs-dest `missing` (папка пропала = диск
вынут) не был виден — теперь красная точка+текст на карточке dests, в деталке (To-карта) и
СТАТУС КАРТОЧКИ Overview «destination disk is not plugged in» (usb_trigger-бэкапы не краснеют);
«Set up later» давал имя «Backup» (теперь выводится «Source to Dest» как в Finish). Подтверждено
скриншотами: nested-диалоги живут (kpPick-оверлей поверх «New source»), guard'ы Next в визарде,
Test у пропавшего dest («path does not exist»), empty-app/not-installed экраны, отказ прогона при
maintenance на dest (тост) и при пустом источнике (result error «all empty — disk not mounted?»).
Регресс 8/8. ГРАБЛЯ теста: не забывать чинить данные, которые сам тест и портил (stale-тест
переписал ts всей kopia-history — в UI даты уехали на 9 дней; восстановлено).
**Дизайн-кит Kopia (2026-07-23, второй заход по фидбэку «всё в кашу, нет цветовых разделителей»):**
CSS-кит `.kp-sec` (секция-карточка на `--card` с UPPERCASE-заголовком-бровью `h4` + акцентная
иконка; `.srow+.srow` внутри получают линию-разделитель), `.kp-run` (grid-строка прогона
`auto auto 1fr auto`: время/бейдж/имя/метрики, табличные цифры, разделители между строками),
`.kp-badge ok|warn|err` (цветные пилюли-статусы на `color-mix(...15%,transparent)` от адаптивных
`--win-good/warn/danger` — НЕ хардкод hex), `.kp-note[.err]` (ошибка ОТДЕЛЬНОЙ строкой на
тонированной подложке, не инлайн-каша), `.kp-tail` (лог тёмным код-блоком). ЕДИНЫЙ компонент
`kpRunRow(x, withName)` + `kpWireTails` — History (карточки по дням) и Recent runs деталки рисуются
одним кодом; деталка кажет короткую дату, History — время (день в заголовке карточки). Settings
деталки сгруппированы в 4 `.kp-sec`-карточки: «Data — what goes where» (source/dest/spare),
«History — how much to keep» (retention+compression), «Schedule» (+usb-триггер), «Notifications»
(+enabled); шаги визарда обёрнуты в те же карточки. Правило дизайна здесь: статус — ЦВЕТНОЙ
бейдж, ошибка — своя строка с подложкой, метрики — вправо моноширинно, секция — карточка с бровью;
новые поверхности Kopia строить из этого кита, не из инлайн-стилей.
**Диалоги Kopia переделаны (2026-07-23, третий заход: «placeholder не видны, чекбоксы с
простынёй»):** кит расширен — `.kp-kinds/.kp-kind[.on]` (выбор типа = две ПЛИТКИ с иконками,
активная с акцентной рамкой на `--sel`), `.kp-eyebrow` (UPPERCASE-метки секций в диалогах),
`.kp-well/.kp-witem/.kp-wadd` (списки в рамке-«колодце»: строки с разделителями + акцентная
кнопка «+ Add» внутри), `.kp-opt` (опция = заголовок + серое описание + `.tgl`-тумблер справа,
разделители между опциями — НЕ голый checkbox с длинным текстом), и главное
`.dialog input::placeholder{color:var(--win-dim);opacity:.8}` — плейсхолдеры во ВСЕХ диалогах
панели теперь видимы (было браузерное умолчание, почти невидимое на тёмном). destDlg: плитки
Local/Cloud, «Repository folder» как well с «Choose a folder…»→строка пути с карандашом,
опции «already holds a repository» (кнопка меняется Create↔Connect) и «password file» —
тумблерами; srcDlg: папки в well со строками fmIcon+путь+✕, excludes с видимым placeholder
(textarea зажата 64px — глобальный стиль растягивал); чекбокс Finish-шага визарда — тоже
`.kp-opt`. Сверено скриншотами (обе плитки, cloud-вариант, srcDlg).
**Kopia v2 — редизайн Overview/деталки с нуля (2026-07-23, четвёртый заход, концепция «лента
защиты»):** кит v3 — `.kp-hero/.kp-hero-ic` (hero: тонированная плитка-щит в цвет статуса через
`--kp-hc`+color-mix, крупный вердикт `.kp-big` 19px/750 + подстрока), `.kp-bk` (карточка бэкапа с
ЦВЕТНОЙ СТАТУС-ПОЛОСОЙ слева через `--kp-st` и ::before), `.kp-ticks` (мини-таймлайн последних
14 прогонов цветными тиками — данные из одного чтения history на пейнт), `.kp-route` (маршрут
source→dest→spare с иконками), `.kp-chip2` (стат-чипы: значение + UPPERCASE-подпись — в деталке
Last run/Files/Restore drill/Spare updated), `.kp-flow/.kp-seg` (FROM→TO→SPARE одной сегментной
полосой с разделителями вместо трёх карточек), `.kp-new` (пунктирная карточка «New backup»).
Overview: hero-вердикт («You're protected.» / «Attention needed» / «Backing up…») задаёт цвет
плитки; пустое состояние = один большой призыв. Деталка: hero (щит+имя+статус) → чипы → flow →
тихие ссылки → Recent runs → Settings-аккордеон. **ГРАБЛЯ (чуть не потерял пол-Resilience):**
подстрочный `h.find(" function tabOverview(tc){")` совпал ВНУТРИ `async function tabOverview`
винRESIL (подстрока!) — вырезало чужой код; спасло `git checkout --`. ПРАВИЛО для скриптовых
правок desktop.html: якорить поиск от УНИКАЛЬНОГО комментария секции приложения, резать до
СЛЕДУЮЩЕГО известного определения и ASSERT'ить, что в вырезаемом куске ровно ожидаемое число
`function` и нет чужих маркеров (winResil/winRclone…).
**Kopia UX-доводка по фидбэку (2026-07-23, шестой заход):** (1) **kopia-маунты в сайдбаре ФМ** —
секция «Snapshots» (после «Cloud»), паттерн 1:1 с rclone-маунтами: сервер `GET /api/kopia/mounts`
(ismount по dests), `renderKopiaMounts` в ФМ (иконка `kopia`, клик→`load(mp)`, извлечение→
`/api/kopia/unmount`), в цепочке `renderRcloneMounts()`; kopia-app после mount/unmount дёргает
`OPEN.__files._kopiaMounts()`; кнопка Mount на вкладке Snapshots подписана и с иконкой, плашка
«mounted read-only — see Snapshots in the Files sidebar». (2) **Overview: полноширинные карточки**
(`.kp-grid.wide`→1 колонка, `.kp-bk.kp-wide` flex: слева вердикт+маршрут+прогресс, справа тики+
«last run 3.1 MB · 43 files · 32s»+next, кнопка «Back up» в торце), **«New backup» — крупная
кнопка в hero** (справа от вердикта), пустое состояние — большая пунктирная карточка. (3)
**Snapshots: подписи к фильтрам** — группы «Repository» и «Show only backup» (`.kp-filt`
UPPERCASE-метки; раньше были голые пилюли без пояснения) + сабтайтл-объяснение. (4) **History:
сегмент «By day / By backup»** (`histMode`) — by-backup группирует все прогоны под заголовком
задания «TEST 321 · 8 runs» (иконка-щит, скрывает имя в строках). (5) **Пикер папок переписан с
сайдбаром** (`kpPick(...,opts)`): колонка `.kp-pick-side` (Places: Root/Home/Mounts + Disks из
`/media/nas` одним `browse`) + `.kp-pick-ctx` — синяя контекстная полоса «что → куда» на каждом
вызове (source: «protected by <source>», dest: «Snapshots written into», restore: «Restoring
<folder> — copied into»), заголовок/кнопка/иконка тоже из opts. Всё проверено CDP-скриншотами
(широкие карточки, ФМ-сайдбар «SNAPSHOTS → T7 repo», пикер с Places/Disks+контекст, history
by-backup, mount live).
**Kopia UX-доводка №2 (2026-07-23, седьмой заход):** (1) **Пикер папок — выброшен свой велосипед,
подключён общий `pickFolder(opts)`** (тот же, что в Backup: сайдбар Quick access/Favorites/Disks,
навигация, mkdir). Расширил `pickFolder` опцией `overlay:true` — рендерит в СВОЙ слой
`.scrim z-index:600` поверх родительского диалога (srcDlg/destDlg/#scrim не затирается);
`showS()/hideS()` вместо прямого toggle. Все 3 вызова (add source folder / repo folder / restore
target) → `pickFolder({overlay:true,start,title,note,onPick})`, контекст «что→куда» идёт в `note`
(серая mon-note). Удалён `.kp-pick*`-CSS и функция `kpPick`. ПРАВИЛО: пикер папок в панели ОДИН —
`pickFolder`; не плодить свои. (2) **Снапшот-браузер `kpSnapBrowse` вылезал за окно** — был
`.nb-pick` 880px + `pf-list min-height:220` БЕЗ капа (40+ файлов → рос бесконечно); теперь
`width:min(620px,94vw)` + `pf-list height:min(52vh,420px)` (прокрутка). (3) **Overview-карточки —
3 зоны** (`.kp-zone` с разделителями `border-left`, статус-полоса слева): главная (имя+бейдж+
вердикт+маршрут inline+next) | «LAST 14 RUNS» (тики+`3.1 MB · 43 files · 32s`) | действие «Back up».
ГРАБЛЯ: `@media(max-width)` НЕ срабатывает для узкого ОКНА при широком вьюпорте (media-queries
смотрят на viewport, не на элемент) → зоны кроил через `flex:N 1 <basis>`+`flex-wrap`, не через
media. (4) **Destinations — плашки на всю ширину** (тот же зон-кит): главная (иконка+имя+путь+
used-by+статус-полоса) | «REPOSITORY» (ленивый Check → `N snapshots · X of data in Y · Z× dedup`,
ссылка refresh) | действия (Mount/Unmount прямо на карточке + шестерёнка-меню: Browse snapshots/
Check/Maintenance/Clear cache/Remove). Всё сверено CDP-скриншотами.
**Kopia UX-доводка №3 — деталь бэкапа переорганизована (2026-07-23, восьмой заход, «самая важная
страница, всё вперемешку»):** сверху вниз чёткие зоны: (1) hero (имя+статус+Back up+шестерёнка);
(2) **«WHAT THIS BACKUP PROTECTS»** — панель `.kp-prot` КРУПНО: From (source+все папки-пути) →
To (dest+путь+статус-точка) → Spare (реплика, «updated Nh ago»), кнопка Edit (разворачивает+
скроллит к Settings), «edit folders» у source (открывает srcDlg); (3) **4 крупных стат-плитки**
`.kp-stat` (21px значение): Snapshots (счётчик + «restore points kept»), In the repository (размер
свежайшего снапшота), **First backup** (дата+год+«N runs» — отвечает «когда начал, сколько всего»),
Restore drill (x/y ✓); данные из истории (первый ts, счётчики) + `/api/kopia/snapshots?d=` (кол-во
снапшотов по тегу бэкапа); (4) **«RECENT RUNS»** с внятной ЛЕНТОЙ `.kp-tl` (до 24 столбиков,
высота ∝ записанным байтам, цвет=результат) + **ЛЕГЕНДА** («ok/warning/failed · bar height = data
written») — раньше были безымянные полоски с враньём «14» при 8 прогонах; ниже список последних 6
+ «View all →»; (5) Settings-аккордеон (retention/schedule/notifications) как было. Destinations:
**курсор-pointer убран** с некликабельных `.kp-bk.kp-static` (визуально «нажимались», но нет);
действия ВЫНЕСЕНЫ НА КАРТОЧКУ кнопками (Snapshots/Mount/Maintain), в шестерёнке остались только
Check/Clear cache/Remove (used → «In use — can't remove»). ПРАВИЛО дизайна детали: важное и крупное
сверху (что бэкапим), метрики плитками, история — с легендой и понятной единицей; настройки —
свёрнуты. Сверено скриншотами.
**Kopia UX-доводка №4 (2026-07-23, девятый заход):** (1) **Options переоформлен** из плоской
простыни в 4 секции-карточки `.kp-sec` (Repository password / Performance / Cache / Engine); каждая
настройка — строка `.srow` (заголовок + описание «что это и как применять» слева, контрол справа,
разделители). Версия kopia + Update — в секции Engine (ответ на «где показывается версия и как
обновить» — там). (2) **Destinations — действия единым горизонтальным рядом** (было часть кнопок/
часть меню): важные крупные кнопки (Snapshots, Mount), второстепенные — иконки поменьше с тултипами
(Maintain=`wand`, Clear cache=`eraser`, Remove=`trash`, дизейбл+тултип «In use by…» если занят);
меню `[data-more]` убрано совсем. (3) **Стат-плитки деталки нажимаемы** (`button.kp-stat` с hover):
Snapshots-плитка → вкладка Snapshots с фильтром бэкапа (шеврон-подсказка). (4) **Настройки бэкапа —
retention/schedule облагорожены**: retention-тиры теперь СЕТКА `.kp-retgrid` карточек `.kp-retf`
(LATEST/DAILY/WEEKLY/MONTHLY/YEARLY, крупное значение + подпись «one per day» и т.п.) вместо
слипшихся инпутов + intro-help; schedule — «Manual only/Daily/Weekly» + «at HH:MM» (время прячется
в Manual) + intro-help. ГРАБЛЯ: старый обработчик пресета форсил `#kRetF display=flex` при выборе
Custom → сетка схлопывалась в 1 колонку; чинить в ОБОИХ местах (инлайн-стиль рендера И обработчик
кнопки). Сверено скриншотами (5 колонок retention, секции Options, ряд действий dests).
**Kopia UX-доводка №5 (2026-07-23, десятый заход):** (1) **Пароль задаётся пользователем**
(решение: генерить не обязательно, один глобальный). `kp_set_password(newpw)`: нет коннектнутых
репо → просто сохранить; есть → preflight `repository status` (таймаут 240с — облако медленно
открывается) на ВСЕХ, потом `repository change-password --new-password` (env `KOPIA_NEW_PASSWORD`,
не argv; `_kp` получил параметр `extra_env`) на каждом, при сбое середины — откат уже-сменённых
назад, сохранённый пароль двигается только если ВСЕ сменились. API POST `/api/kopia/password`.
UI: destDlg при ПЕРВОМ приёмнике (нет пароля) показывает поле «Repository password» с
предзаполненным сгенерированным (редактируемо) → уходит в `password` create; Options — Copy +
«Set my own»/«Change…» → диалог `kpPwDlg` (при существующих репо — предупреждение про re-key).
Live-проверено: смена на T7+pcloud (оба открылись новым паролем), откат, preflight режет
недоступный. ОТВЕТ пользователю: пароль ОДИН на всё, больше нигде не прячется. (2) **Проверка новой
версии kopia** — `kp_update_info()` (кэш `kopia-update.json`, TTL 24ч, GitHub releases/latest,
сравнение version-tuple), в `kp_status().update`; API POST `/api/kopia/update-check` (force). UI:
Options→Engine показывает «up to date»/«X available» + Update; **баннер `.kp-banner` на Overview**
при доступном обновлении (Update→Options, крестик прячет на сессию). Проверок «периодически»:
health-тик `_kopia_tick` раз в 30 мин (kp_stale, fs-dest пропал); реальная достижимость облака —
по кнопке Check (дорого дёргать облако постоянно), статус connected/missing выводится дёшево на
каждый статус-полл. (3) **Sources-карточки ожили**: `.kp-src` полноширинная (акцентная плитка с
числом папок + «FOLDERS», имя, пути, «measure size» инлайн, чипы-исключения, «used by»,
edit/delete-иконки справа) вместо вялой `.rs-card`.
**Kopia: «что не используется» + пикер облачного пути + уборка за удалённым заданием (2026-07-24).**
(1) **Sources/Destinations сгруппированы по факту использования** (`usedBy`): секция «NOT USED — N»
(жёлтая бровь `.kp-grp.idle`, идёт ПЕРВОЙ — забытое должно попадаться на глаза) и «BACKED UP» /
«IN USE» (зелёная). Неиспользуемая карточка — `.kp-idle`: ПУНКТИРНАЯ рамка, чуть жёлтая подложка,
жёлтая иконка-плитка, бейдж `.kp-badge.idle` «not used», строка «Nothing backs these folders up» /
«Nothing is written here» и тихая кнопка **«back it up →»**, которая открывает мастер нового бэкапа
с УЖЕ выбранной этой стороной (`kpNewWiz(pre)` — общая точка входа мастера, Overview зовёт её же).
Используемая карточка — зелёным «Backed up by X» / «Receives X». Корзина активна ТОЛЬКО у
неиспользуемых (было так же, но теперь это видно и без наведения). ПРИНЦИП: «не используется» — не
авария, поэтому жёлтый+пунктир, а не красный; работу по поиску делает группировка, а не крик.
(2) **Облачный приёмник больше не набирается руками**: `rcloneRemotePicker` получил 4-й аргумент
`opts` (`overlay:true` — свой слой `.scrim z-index:600` поверх родительского диалога, ровно как у
`pickFolder`; плюс `title/note/okLabel/start`), в `destDlg` поле `#kdRp` заменено на «колодец» с
кнопкой «Browse the remote…» (как у локального пути), выбранный путь показывается как
`remote:path`, смена remote сбрасывает путь, создание без выбранной папки отклоняется. Создать
папку прямо в пикере можно кнопкой «+ Folder» (уже было). Старые 3-аргументные вызовы пикера
(Mirror/rclone-app) не тронуты. **ГРАБЛЯ:** в подвале пикера «Selected: pcloud:» с висящим
двоеточием рендерился как «:Selected: pcloud» (bidi тащит нейтральный символ в начало) — в корне
remote теперь пишем «pcloud: (remote root)».
**Kopia: выбор source/destination — карточки вместо `<select>` (2026-07-24).** Кит `.kp-pk`
(строка-опция: иконка + имя с тегами + путь моно-строкой + галочка `.pk-ck`; `.on` — «белая фишка»
`--sel`+акцентный текст; `.add` — пунктирная «+ New…»; `.off` — пунктирная, приглушённая,
`disabled`). Секции `secSource`/`secDest`/`secSpare` переписаны на него (общие для мастера и
настроек деталки), `freeDests`/`takenNote` удалены. **Занятый приёмник теперь ВИДЕН, но не
выбирается** (`.off` + тег «used by <бэкап>») — раньше он просто ПРОПАДАЛ из списка, и это
читалось как «куда делся мой диск»; та же логика для «уже основной приёмник» / «уже запасная
копия» и красный тег «disk not plugged in». Источник, который уже используется другим бэкапом,
показывает серый тег «in <бэкап>», но ВЫБРАТЬ его можно (один source в двух бэкапах — законно,
это 3-2-1). ПРАВИЛО: недоступный вариант лучше показать выключенным с причиной, чем спрятать.
**Kopia: заметки у бэкапа, запрет вложенных папок, даты по-английски, ховер не дёргает строки
(2026-07-24).** (1) **Описание бэкапа** (`description`, ≤2000 симв. в сущности): секция «Notes» в
настройках деталки и поле на шаге Finish мастера; в деталке — плашка `.kp-note2` под hero
(`white-space:pre-wrap`, многострочно), на карточке Overview — ОДНА первая строка `.kp-desc1` с
тултипом (карточка — это взгляд мельком, полный текст в деталке). (2) **Вложенные папки в
источнике запрещены**: `/x/docker` + `/x/docker/bytestash` = kopia обошла бы данные ДВАЖДЫ.
Сервер авторитетен — `_kp_unnest()` в `kp_source_save` (сортировка по длине, предок побеждает,
возвращает `nested[]` для UI); в `srcDlg` добавление ребёнка отбивается тостом «already covered
by …», а добавление РОДИТЕЛЯ втягивает уже добавленных детей («N folders inside it folded in»).
Сиблинги не путать: `/x/docker` и `/x/dockerfiles` — НЕ вложенность (сравнение только по
`path + "/"`). (3) **Даты были русскими**: `toLocale*()` следует локали БРАУЗЕРА, а панель
english-only → на ru-системе в UI лезли «24 июл.» и русские разделители разрядов. Введена
константа `LOC="en-GB"` (desktop.html) и проставлена во ВСЕ 23 call-site'а (`toLocaleDateString`/
`toLocaleString`; и Date, и Number принимают локаль первым аргументом), в screen.html — 3 места
(там ещё жил `NAS_LANG==="ru"?"ru-RU":"en-GB"`). ПРАВИЛО: любой новый `toLocale*` — только с `LOC`.
(4) **ГРАБЛЯ «ховер дёргает строки»**: глобальное `input,textarea,select{border:1px solid
transparent;padding:10px 13px}` + `input:hover{border-color:…}` применялось и к ЧЕКБОКСАМ — как
только у чекбокса появляется видимая граница, Blink бросает нативный виджет и рисует
стилизованный бокс ДРУГОГО размера: чекбокс 15px→22px, строка файла 30px→35px, список прыгал под
курсором. Лечение: `input[type=checkbox],input[type=radio]{padding:0;border:none;background:none;
width:auto}` + сброс `:hover`-границы. Чинит и ФМ (`.fm-ck`), и все прочие голые чекбоксы.
**Kopia UX-доводка №6 — 8 улучшений по всему приложению (2026-07-23, одиннадцатый заход):**
(1) **Онбординг** на пустом Overview: 3 нумерованных шага `.kp-step` (Set password → Add
destination → Choose source) с галочками готового и кнопками, ведущими в нужный таб + авто-открытие
диалога; исчезает после первого бэкапа. (2) **Баннер проблем** на Overview: худшая беда по всем
бэкапам (failing→err-красный / stale→warn-жёлтый `.kp-banner`), кнопка Open → деталка. (3)
**Спарклайн роста хранилища** в стат-плитке «In the repository» (svg `.kp-spark`, размер снапшотов
во времени oldest→newest из snapshot-list). (4) **Поиск файлов по снапшоту**: сервер `kp_snap_find`
(`kopia ls -lr <oid>`, фильтр по подстроке имени, кап 200) + API `snap/find`; в снапшот-браузере
поле `.kp-search` (debounce 350мс) переключает список в режим результатов (путь+размер, клик по
файлу=restore), «которого снапшота лежал X». (5) **Единый пустой-компонент** `kpEmpty(ic,title,
desc,btn)` в Snapshots/History (крупная иконка+текст вместо разнобоя «no X yet»). (6) **Светлая
тема** проверена живьём (themeBtn) — карточки/бейджи/подложки/спарклайн адаптировались (всё на
`--win-*` + color-mix, хардкода нет). (7) **Мобильный** (412px, CDP device-metrics) — окно
полноэкранное, табы-таблетки горизонтально, стат-плитки в 2 колонки (`auto-fit minmax(118px)`),
панель protects/flow складываются, dock внизу — читаемо. (8) **Клавиатура**: глобальный
scrim-keydown — Enter→`.pri`-кнопка диалога (кроме фокуса в textarea/select), Esc→Cancel/Close;
работает во ВСЕХ диалогах панели. ГРАБЛЯ CDP: `cycleTheme`/`winKopia` внутри IIFE (не глобальны) —
драйвить кликом по `#themeBtn`/`[data-did]`, не вызовом функции. Все 8 проверены скриншотами
(онбординг, баннер, спарклайн, поиск «woff»→шрифты, светлая тема, мобильный 2-кол). Kopia app: ГОТОВО.
**Иконки дока/лончпада — оптический баланс (2026-07-23):** глифы приложений заполняли свои
viewBox по-разному → визуально разного размера при одинаковом CSS-размере. RAW_LOGOS рендерятся в
viewBox `-12 -12 88 88` (svg()), AP/P — в `0 0 24 24`. Правки: **kopia** (RAW_LOGOS) был scale(0.125)
= 73% и низкий → `translate(-6.4,-6.4) scale(0.15)` (≈87%, отцентрован); **lifef** (Resilience,
в AP) заливал 92% → обёрнут `translate(1.32,1.32) scale(0.89)` (чуть меньше, перестал быть самым
крупным); **whale** (Docker, широкий-низкий, читался мелким) → `translate(-1.55,-1.55) scale(1.13)`;
лончпад-иконки крупнее: `.lp-grid .app .tile>svg` 56→64px, bare 60→68px. ПРАВИЛО: баланс иконок —
ОПТИЧЕСКИЙ (насколько глиф заполняет свой бокс), не по CSS-размеру; RAW_LOGOS обычно надо
подкручивать scale/translate под квадратный бокс. Проверено скриншотом лончпада (все ~одного веса).
**Rsync-приложение «Backup» переименовано в «Mirror» (2026-07-23)** — путалось с Kopia (тоже бэкап).
Сменено ТОЛЬКО отображаемое имя: dock-лейбл, заголовок окна (`openWin("__nasbak","Mirror"…)`),
контекст-меню; id `__nasbak`, `winBackup()`, API `/api/backup/*`, `nb_*`, событие `nas_backup` и вся
внутренняя терминология «backup/profiles» НЕ тронуты. Т.е. приложение, показываемое как «Mirror» =
rsync-mirror (pull/push/rclone), а «Kopia» = версионные снапшоты.
**ПОЛНЫЙ АУДИТ Kopia (2026-07-23, финальный): 2 параллельных ревью-агента (движок+UI) + 69
автотестов движка + живой CDP-обход всех табов/диалогов/взаимосвязей + светлая тема + мобильный.
18 находок, ВСЕ исправлены и перепроверены.** ДВИЖОК: (E1 HIGH data-loss) `no_init`-spare guard
покрывал только fs → облачный spare мог быть направлен на непустую remote-папку, которую первый
`sync-to --delete` вычищал; добавлен `rclone lsf`-чек пустоты и для rclone. (E2 HIGH data-loss)
primary-приёмник бэкапа мог оказаться sync-spare'ом ДРУГОГО бэкапа → его sync-to --delete стирал
снапшоты; `kp_backup_save` теперь запрещает выбирать primary, который кто-то зеркалит как spare.
(E3 MED) смена пароля игнорировала результат отката (частичный сбой оставлял репо на новом пароле
при сохранённом старом — навсегда недоступен) и не блокировала конкурентный прогон → добавлены
проверка `rb["ok"]`+сохранение нового пароля с явным сообщением какой репо чинить, и отказ смены
пока идёт любой run/restore/maintenance. (E4 MED RAM) `kp_snap_find` буферил ВСЁ рекурсивное дерево
в память (кап был только на ответе) → переписан на потоковый `Popen` с жёстким лимитом 400k строк +
kill дочки. (E5 MED) usb_trigger-бэкапы получали ложный kp_stale (диск втыкают реже порога) → изъяты
из stale-проверки. (E7 LOW) `kp_status` дёргал GitHub синхронно при протухшем кэше (блокировал
polling) → `kp_update_info(net=False)` в статусе, фоновый refresh в тике. Приняты как документированные
допущения (не критично): E6 `_systemd_active` таймаут под нагрузкой (общий с nb), E8 отмена во время
replicate (systemctl stop покрывает), E9 не-dict JSON body→500 (всё равно JSON), E10 stale
false-negative после ротации 200-истории. UI: (U1 HIGH) Enter в поиске снапшота жал `.pri` глобального
scrim-хендлера = запускал Restore → поле помечено `data-noenter`, а глобальный хендлер теперь целит в
ВЕРХНИЙ `.scrim.show` (не в диалог под оверлеем). (U2 MED-HIGH) деталка застревала на «99%», если
прогон стартовал ИЗВНЕ (schedule/usb/телефон) — `detailWasRunning` ставился только в paintDetail;
теперь и в `updDetailLive`. (U3 MED) вложенный `pickFolder`-оверлей: Enter/Esc действовали на скрытый
родитель → топ-оверлей + `data-noenter` на `#pfPath`. (U4 MED) тумблеры notify/enabled в настройках
сбрасывались при смене селекта (читались только в Save) → `readFlags()` в onChange. (U5 LOW) имя на
шаге Finish терялось при Back → читается в readStep. (U6 LOW) `kpAskName` двойной сабмит на Enter →
снят собственный keydown (глобальный жмёт .pri). (U7 LOW) незакрытый `<b>` в err-баннере при
нескольких падениях. (U8 LOW) фильтр снапшотов по удалённому бэкапу оставался залипшим → сброс, если
чипа нет. Плюс: курсор dest-карточки был `pointer` (`.kp-bk.kp-wide` бил `.kp-bk.kp-static` по
порядку) → `.kp-bk.kp-wide.kp-static{cursor:default}`. ГРАБЛЯ теста: importlib-тесты, реально
меняющие состояние (пароль!), портят боевой конфиг — ранний unguarded-тест сменил пароль на
`nas-newpw-xyz` на ОБОИХ репо; восстановлено через `change-password` назад (changed:2, оба
открываются). ПРАВИЛО: тесты, вызывающие мутирующие kp-функции, обязаны восстанавливать состояние
(пароль, backups, history ts). Docker-иконка в лончпаде увеличена (whale→scale 1.22, широкий-низкий
логотип). Kopia app: АУДИРОВАН И ГОТОВ.
**ИСПРАВЛЕНИЯ:** (F3/S-hang) **watchdog «нет прогресса» в `_kp_snap_cli`** — 20 мин молчания pty →
kill+error «destination may be unreachable» (зависшая kopia больше не держит приёмник вечно);
(S1) `kp_snap_find` — единственный `_kp`-путь БЕЗ таймаута → добавлен дедлайн 300с (стрим-цикл);
(S2) `kp_set_password` держал `_KP_CFG_LOCK` минуты сетевого I/O → тяжёлый CLI вынесен ИЗ замка,
busy-флаг сериализует, замок только на снимок+сохранение; (S3) **KOPIA-PASSWORD.txt и disaster-card.md
писались 0644 (глобальный пароль читаем не-root SMB-гостем при force-user=root)** → оба 0600;
(S4) ручной/старый kopia.json с `schedule:"daily"`/`retention:5` (не-dict) крашил тик каждую минуту и
глушил ВСЮ автоматику → `kp_load` коерсит per-field типы (schedule/retention→dict, folders/excludes→
list); (S5) excludes с ведущим `-` могли стать `--add-ignore`-опцией → отброс. Security-агент
подтвердил well-defended: argv-инъекция системно закрыта (анкор-регэкспы oid/id/path, remote из
rclone.conf), секреты только в env не argv, kopia.json 0600, /etc/nas-os/kopia 0700, стуки-running
серилизованы `_NbStartLock`+per-dest-busy, рекурсия дрели depth-6, списки капнуты. Приняты как
низкий риск: restore TOCTOU (skip-existing, actor=root-equiv). ГРАБЛЯ теста подтверждена вновь:
изолированные фикстуры + cleanup в `finally` обязательны (E2E/fail-тесты трогают реальную kopia).
Kopia app: АУДИРОВАН ДВАЖДЫ, ГОТОВ.
**Backup Explorer — Snapshots-таб переписан в проводник (2026-07-23, стиль Synology Hyper Backup).**
Раньше вкладка Snapshots была плоским списком снапшотов; теперь это трёхзонный проводник (`.kpx`,
функции `ex*` в winKopia): СЛЕВА дерево «Backed-up folders» (папки source этого репо + разворачиваемое
дерево ТЕКУЩЕГО снапшота), ПО ЦЕНТРУ файловая таблица (имя/размер/тип/дата, сортировка кликом по
заголовку, папки всегда первыми), ВНИЗУ таймлайн точек восстановления (`.kpx-tlbar`: тики по датам +
◄►-стрелки + календарь-поповер `exCal` с подсвеченными днями + корзина). **Ключевая идея: всё
адресуется как `<root-oid снапшота>/<rel-путь>`** — смена точки восстановления НЕ меняет папку (остаёшься
в той же, просто в другой момент времени), навигация по дереву/крошкам/двойному клику переиспользует
кэш папок снапшота (кэш живёт с точкой восстановления, `EX.kids`). Действия на выбранном файле/папке
ИЛИ на текущей папке (тулбар `exTarget`): **Restore** (положить на исходное место — `_kp_obj_arg`
собирает `oid/rel`, single-file → target=файл-путь через `name`, folder → target=папка), **Copy to…**
(в любую папку NAS через `pickFolder`), **Download** (файл — стрим `kopia show` прямо в сокет без
стейджинга; папка — временный zip через `kopia restore …/x.zip`, кап 2 ГБ + проверка места). Restore
всегда copy-only (`--skip-existing`), репозиторий не пишется; цель только под /mnt|/media|/srv|/home.
Фильтр (Enter → рекурсивный `kp_snap_find` по всему снапшоту), удаление точки из таймлайна.
**ГРАБЛИ, найденные при переписывании:** (1) старый `_KP_OID_RE=k?[0-9a-f]+` МОЛЧА ронял файлы с
INDIRECT-oid (`Ix…` — всё крупнее пары МБ дробится kopia на части) — новый regex ловит `I…`/kind-префикс;
проверено на боевом pcloud-репо (2 из 5 файлов были `Ix…` и не показывались бы). (2) `kopia ls -l`
пробел-в-oid-колонке варьируется → перешёл на `kopia show <dir-oid>` (JSON-манифест: у папок РЕКУРСИВНЫЙ
размер+число файлов, чего `ls` не даёт; поток проверяется на magic `{"stream":"kopia:directory"` — file-oid
не дампится как «папка»). (3) restore файла в существующую ПАПКУ → kopia «is a directory»; лечение —
target делается файл-путём через `name` (basename, `..` отбит). Серверное: `kp_snap_ls(destid,oid,rel)`,
`kp_snap_find` (mtime добавлен), `kp_dl_open`/`kp_dl_zip`, HTTP GET `/api/kopia/snap/file?d&oid&rel&name&zip`
(за auth-гейтом, HTTP/1.0 close-delimited стрим), `kp_restore_start(…,rel,name)`. **Эксклюзивность
приёмника (по просьбе пользователя):** один destination = один backup. `kp_backup_save` отклоняет выбор
репозитория, уже занятого другим бэкапом как dest ИЛИ spare (не только sync-spare, как было); UI-пикеры
(`freeDests`/`secDest`/`secSpare`) показывают только свободные + текущий выбор, с пояснением «N repositories
not listed». CSS-кит `.kpx-*`. Проверено CDP-прогоном (44 ассерта: листинг/сортировка/навигация деревом
и крошками/юникод-папки/смена точки сохраняет папку/календарь/фильтр+deep-search/download-URL работают и
отдают точные байты/restore на исходное место — байт-в-байт/copy-to папки — diff -r IDENTICAL/guard'ы
traversal+target+bad-oid/эксклюзивность в пикерах) + модульные 25/25 на изолированном репо + светлая тема
+ телефон 412px. Изолированные фикстуры (`/media/nas/t7-4TB/.kpx-audit`) вычищены, боевые source/dest/backup целы.
**Explorer — визуальное разделение зон + клавиатура (2026-07-23, по фидбэку «все три зоны
однотипны»).** Было: сайдбар/таблица/таймлайн — три одинаковые `--card`-карточки. Стало (язык ОС —
сайдбары `.pf-side`/`.fm-fav` = заподлицо+`border-right`, НЕ карточка): (1) сайдбар+таблица —
ОДНА панель `.kpx-body` (Finder-стиль), рейл заподлицо с разделителем; (2) рейл утоплен нейтральной
тёмной вуалью `rgba(8,13,23,.05)` — В СВЕТЛОЙ теме `--card`==`--win-bg`==сплошной белый (контраста
заливки НЕТ вообще, различие ТОЛЬКО от тонировки/рамок → вуаль явная, не через elevation-токены,
+`data-theme`-вариант); (3) таймлайн — отдельная АКЦЕНТНО-тонированная полоса-скраббер
(`color-mix --win-accent 11% в --card`, акцентная рамка, метка «N POINTS» с часами, утопленный
трек-жёлоб, дата-кнопка с «· N files · size»). **Клавиатура в таблице** (`.kpx-list tabindex=0`,
клик по строке ставит фокус): ↑/↓ выбор, Enter/→ открыть папку, ←/Backspace вверх, печать →
фокус в фильтр. Проверено CDP: обе темы дают distinct-поверхности (ассерты по computed-bg),
клавинавигация 5/5, регресс функционала зелёный. ПРАВИЛО: в СВЕТЛОЙ теме различать поверхности
внутри окна заливкой нельзя (всё белое) — только тонировка/рамка/тень.
**Explorer — мультивыбор: пакетный restore / copy / download (2026-07-24).** Выделение стало
множеством (`EX.selMulti` — Map rel→entry; `EX.sel` = ЯКОРЬ диапазона, `EX.focus` = каретка):
клик — один, Ctrl/Cmd+клик — добавить/убрать, Shift+клик — диапазон от якоря, **чекбокс в строке**
(`.kpx-ck` — всегда добавляет/убирает без модификаторов; на таче модификаторов НЕТ вовсе, поэтому
`@media(hover:none)` держит его видимым, с мышью — `opacity:0` до ховера/выделения), клавиатура:
Ctrl+A — всё, Esc — снять, Shift+↑/↓ — растянуть от якоря, голая стрелка — схлопнуть в одну строку;
пилюля `#kxSelLbl` показывает «N items» и по клику снимает выделение, тултипы кнопок говорят про
пакет. Движок: `kp_restore_jobs(destid, items[])` — ОДИН транзиент-юнит на всю пачку, каждый
элемент со своим target'ом, прогресс складывается («i-й из N» + pct внутри элемента → общий pct),
`kp_restore_start` теперь тонкая обёртка над ним; API `snap/restore/start` принимает `items[]`
(старая одиночная форма жива). Пакетная загрузка — POST `/api/kopia/snap/zip` (`kp_zip_items`:
стейджинг во временную папку кэша → zip → стрим → удаление; кап `KP_MULTI_ZIP_MAX`=512 МБ, т.к.
браузер держит его блобом в памяти; ≤500 элементов; дубли basename → «name (2)»), фронт качает
`fetch`→blob→`<a download>`. **ГРАБЛЯ:** файл, восстановленный по ГОЛОМУ object id, приходит с
mtime 1970 (метаданные лежат в манифесте РОДИТЕЛЯ) — поэтому и одиночные, и пакетные действия
адресуют `<root-oid снапшота>/<rel>` (`exItemFor`), а ZipInfo клэмпит дату к 1980 (ZIP не умеет
epoch 0). Удалённый «призрак» адресуется внутри ПРЕДЫДУЩЕЙ точки восстановления, где он ещё жив.
Проверено: движок 21/21 на изолированном репо (zip байт-в-байт с источником, `diff -r` IDENTICAL,
multi-restore 2 задачи → mtime сохранён, guard'ы traversal/цели/лимитов), CDP-прогон UI 24/24
(чекбоксы, ctrl/shift, вся клавиатура, пилюля, три пакетных действия, сброс выделения при
навигации, ноль JS-ошибок). **ГРАБЛЯ CDP-тестов:** у CSSStyleRule ТЕПЕРЬ есть (пустой) `.cssRules`
(CSS Nesting) → обход `if(r.cssRules)walk(...)` уходит в пустоту и не находит ни одного правила;
проверять `r.selectorText` ПЕРВЫМ. И headless-chromium репортит `hover:none` и НЕ эмулирует
`hover` через `Emulation.setEmulatedMedia` — ховер-ветку CSS проверять по CSSOM, не по computed.
**Kopia: выбор папок деревом с чекбоксами, как в Mirror (2026-07-24) — СДЕЛАНО.**
Семантика ровно как у `nbPickSource`: галка на папке = вся папка ЦЕЛИКОМ, включая то, что
появится в ней позже; снял галку с дочерней — исключается ТОЛЬКО она, новые соседние папки
попадают в бэкап САМИ. Ничего не перечисляем заранее: храним «выбрано» (`folders`) + «явные
исключения» (`exclude_paths`, АБСОЛЮТНЫЕ пути), остальное выводится.
- **Движок.** `exclude_paths[]` у Source рядом с текстовыми `excludes[]` (паттерны `*.tmp`
  остались, идут во ВСЕ папки). `_kp_excl_paths(folders, raw)` в `kp_source_save`: normpath
  (НЕ realpath — правило матчится по пути, а резолв симлинка увёл бы его туда, куда kopia не
  ходит), путь обязан лежать ВНУТРИ одной из `folders` (иначе → `dropped_excludes`, это остаток
  удалённой папки), дедуп, схлопывание вложенных исключений в верхнее, кап 200. `kp_load`
  коерсит поле в список. `_kp_ignore_rule(folder, path)` → правило политики ИМЕННО этой папки:
  `/x/docker` + `/x/docker/bytestash` = `--add-ignore "/bytestash"`. `kp_source_size` вычитает
  исключённое (иначе цифра врёт). Disaster card печатает «NOT backed up (left out)» — после
  катастрофы надо знать, чего в копии нет вовсе.
- **ГРАБЛЯ/проверено живьём на изолированном репо:** ВЕДУЩИЙ СЛЭШ якорит правило к корню
  политики (gitignore-подобно) — `/bytestash` вырезает только её, а `bytestash2` и
  `sub/bytestash` остаются; ЗАВЕРШАЮЩИЙ слэш НЕ нужен: одна и та же форма без него матчит и
  папку, и файл (`/root.txt`), поэтому из дерева можно исключить и отдельный файл. Юникод и
  пробелы в правиле работают. Сброс правил по-прежнему ОТДЕЛЬНЫМ вызовом `--clear-ignore`
  (старая грабля: clear в одной команде с add применяется ПОСЛЕ add).
- **UI.** `kpPickSource(sel, excl, onPick)` — копия дерева `nbPickSource` (те же состояния
  checked/covered/excluded/exout/indet и `.nb-trow`-кит), но листинг локальный
  (`/api/kopia/browse`, абсолютные пути) и ВСЕГДА в своём оверлее `z-index:600` — открывается
  из `srcDlg`, который обязан выжить под ним (правило вложенных диалогов). Пустой выбор →
  ветки `/mnt`, `/media`, `/media/nas` раскрыты сразу (диски в один клик), иначе раскрываются
  ветки к уже выбранному/исключённому. Одиночный файл нельзя выбрать КОРНЕМ источника (бэкап
  начинается с папки), но можно исключить внутри выбранной. В `srcDlg` кнопка «Add folder…»
  (`pickFolder`) заменена на «Choose folders…» (дерево); исключения показываются под своей
  папкой зачёркнутыми чипами `.kp-wexc` с ✕ («вернуть»), счётчик «— N folders, M excluded»;
  удаление папки чистит её исключения (и на клиенте, и на сервере). Карточка источника и
  панель «What this backup protects» кажут «N folders left out».
- Заодно `.nb-trow:hover` убран под `@media(hover:hover)` (на таче подсветка залипала).
- **Тесты:** 23 юнита на конверсию путь→правило и валидацию (вложенность, сиблинги по префиксу,
  traversal, юникод/пробелы, кап); **live e2e 16/16** на изолированных фикстурах через РЕАЛЬНЫЕ
  функции (source/dest/backup → `kp_run_start` → содержимое снапшота: исключённые папка/вложенная/
  файл отсутствуют, `bytestash2` и `sub/bytestash` на месте, НОВАЯ соседняя папка приехала сама,
  новый файл в исключённой — нет; уборка + ассерт «боевой конфиг не тронут»); **CDP 29/29** (оверлей
  поверх диалога, все состояния дерева, exout не кликается, чипы, счётчики, сохранение и
  перечитывание из конфига, ноль JS-ошибок). Фикстуры и тестовый источник вычищены, временная
  сессия панели отозвана.

**Kopia: добраны неиспользованные возможности движка (2026-07-25).** Полная сверка нашего кода
с CLI kopia 0.23.1 (`--help-full` по каждой группе) показала, что по смыслу покрыт весь обычный
цикл, но восемь содержательных вещей не были задействованы. Сделаны все, кроме сознательно
отложенных (см. конец блока).
- **Лимит скорости — на ЛЮБОЙ приёмник** (`repository throttle set`). В CLAUDE.md и в UI раньше
  стояло «у kopia НЕТ своего тротлинга, cloud-only через `RCLONE_BWLIMIT`» — **это было неверно**:
  throttle работает на всех бэкендах, включая filesystem, и хранится В РЕПОЗИТОРИИ (переживает
  переустановку). `RCLONE_BWLIMIT` из `_kp_env` убран, чтобы не было двух ограничителей.
  `kp_opts` получил `dnlimit_kb` (скачивание: restore/verify/просмотр); `kp_throttle_args/push/get`,
  push на каждый `kp_opts_set` и на каждый `_kp_ensure_connected` (свежий connect заново
  утверждает наши капы). API `POST /api/kopia/dest/throttle` — читает то, что репозиторий реально
  хранит. Теперь можно придушить снапшот на USB-мост, а не только в облако.
- **Хуки до/после снапшота** (`policy set --before/--after-snapshot-root-action`) — ПРАВИЛЬНЫЙ
  способ бэкапить БД: дамп в `before`, уборка в `after`, вместо копирования живых файлов базы.
  Сущность Backup: `hooks {before,after,mode:essential|optional,timeout}`. **ГРАБЛЯ №1:** kopia
  НЕ пускает action через шелл — она режет строку по пробелам на path+args, и пайплайн
  `pg_dump … | gzip > f` превратился бы в мусор. Поэтому панель материализует команду в скрипт
  `/etc/nas-os/kopia/hooks/<bid>-{before,after}.sh` (0700, root, с `#!/bin/sh` и `set -e`) и отдаёт
  kopia ПУТЬ — есть полный шелл и он же аудируем на диске. **ГРАБЛЯ №2:** actions выполняются
  только если у КЛИЕНТА стоит `enableActions` — иначе kopia молча их пропускает; `--enable-actions`
  добавлен в `_kp_repo_args`, а старым конфигам флаг дописывает `_kp_ensure_actions` правкой
  derived-JSON (дешевле реконнекта, для облака — без сети). Пустая строка в
  `--before-snapshot-root-action` СНИМАЕТ хук. Скрипты удаляются вместе с заданием
  (`_kp_forget_runs`). UI: секция «Before & after» в настройках задания (мода «Stop the backup» =
  essential — для дампа, без которого снапшот бесполезен).
- **Правила чтения** (`rules` у Backup → policy): `--one-file-system` (не уходить в примонтированное
  внутри источника), `--ignore-cache-dirs` (CACHEDIR.TAG, по умолчанию вкл.), `--max-file-size`
  (**ГРАБЛЯ: хочет БАЙТЫ, «1GB» не парсит**), `--ignore-file-errors`/`--ignore-dir-errors` (в UI
  честно сказано, что это делает «успешный» бэкап тихо неполным). Секция «What gets read».
- **Закрепление точки** (`snapshot pin --add/--remove keep`) — ретеншен никогда не съест помеченную.
  **ГРАБЛЯ: pin ПЕРЕПИСЫВАЕТ манифест, id снапшота МЕНЯЕТСЯ** — после pin нельзя переиспользовать
  прежний id, надо перечитать список (в Explorer это `exReload(tc, ts)`: ищем ту же точку по
  ВРЕМЕНИ, а не по id, и остаёмся в той же папке). `kp_snapshots` отдаёт `pins[]`; в таймлайне
  звезда-тумблер, у закреплённой точки корзина заблокирована, тик обведён жёлтым.
- **Сравнение точек** (`kopia diff <oid1> <oid2>`) — «что изменилось» на уровне файлов, как
  «What changed» у Mirror. `kp_snap_diff` парсит `added/removed/changed`, отбрасывая построчный шум
  «sizes differ / modification times differ», итоги берёт из ЗАВЕРШАЮЩЕЙ строки JSON; кап 400 путей
  на категорию. Кнопка в таймлайне Explorer (сравнивает с предыдущей точкой).
- **Оценка объёма** (`snapshot estimate`) — «сколько это будет» ДО первого прогона (первый снапшот
  20 ГБ в облако иначе выглядит как зависание — на этом уже ловили ложное срабатывание сторожа).
  `--json` у неё НЕТ, парсим текст («Snapshot includes N file(s), total size X» + excludes);
  человекочитаемые размеры разбирает `_kp_human_bytes` (**приблизительно по своей природе — только
  для показа, никогда для гардов**). Меню шестерёнки в деталке → диалог с файлами/объёмом/исключённым.
- **Перенос истории** (`snapshot move-history` / `copy-history`) — папка переехала (диск
  перемонтировали, переименовали) → иначе kopia считает новый путь новым источником и история
  начинается с нуля. `kp_snap_sources` даёт список `user@host:/path` с числом точек и признаком
  «папка на месте», `kp_move_history` валидирует спеку/абсолютность/traversal и отказывается при
  занятом приёмнике. Диалог в шестерёнке приёмника.
- **Ремонт** (`snapshot fix invalid-files`) — пара к `verify`: находили порчу, а починить из UI было
  нечем. Первый прогон ТОЛЬКО смотрит (kopia пишет что-либо лишь с `--commit`), кнопка «Repair for
  real» появляется, если найдено. **ГРАБЛЯ: и `fix`, и `move-history` пишут ВСЁ в stderr — включая
  вердикт «No changes.»**; парсер, читавший только stdout, показывал «0 перенесено» и пустой отчёт.
  Правило: у kopia не угадывай поток — бери `out+err`.
- **Мелочи из того же аудита:** `maintenance info` (без `--json`, парсим два блока цикла) — строка
  «housekeeping: next …» в панели Check приёмника; `blob stats` = сколько РЕАЛЬНО занято на носителе
  (**это полный листинг объектов — на облаке дорого, поэтому по кнопке «measure space used», не в
  Check**); `repository validate-provider` (проверка, что бэкенд ведёт себя как надо — прямо в тему
  для rclone-бэкенда с пометкой «[Not maintained]»; ~40 с, поэтому в фоновом юните);
  `policy export` — «какие правила kopia реально применяет» (read-back, независимый от нашего
  конфига); `snapshot create --description` = имя задания; **restore с перезаписью** — галка
  «Replace files that are already there» в подтверждении (по умолчанию по-прежнему `--skip-existing`,
  перезапись = `--overwrite-files/-directories/-symlinks`, осознанный выбор при восстановлении
  ПОВЕРХ повреждённого).
- **Фоновые задачи обобщены:** `KP_BG_KINDS = {maint,verify,est,fix,val}`, свой транзиент-юнит на
  вид (`nas-kopia-{maint,vfy,est,fix,val}-<destid>`), `_kp_bg_start(kind,destid,arg)` через
  `--setenv=KPB_ARG`, `dest/bg-status` отдаёт все виды. `_kp_dest_busy` считает занятостью
  maint/verify/fix/val, но НЕ est (он только читает локальные файлы). UI-обёртка одна — `kpBgDlg`
  (запуск, поллинг 2 с, вердикт+хвост вывода); диалог можно закрыть и вернуться, задача в юните.
- **Логи kopia больше не растут молча.** Найдено: 38 МБ за двое суток в
  `<кэш>/<dest>/logs` (kopia пишет файл на КАЖДЫЙ вызов, а Explorer вызывает её на каждый клик), и
  всё это лежит на приёмнике бэкапа. Своих ограничений у нас не было, а дефолт kopia щедрый
  (~30 дней / ~1 ГБ). Лечение: команды просмотра (`show`, `ls -lr`) получили
  `--file-log-level error --disable-content-log`, а `_kp_logs_gc()` в `maintenance_daily` режет по
  возрасту (`KP_LOG_KEEP_DAYS`=14), затем самые старые до `KP_LOG_KEEP_BYTES`=64 МБ на приёмник.
- **Новые API:** POST `/api/kopia/{snap/pin,snap/diff,dest/medium,dest/policies,dest/throttle,
  dest/history,dest/validate,dest/fix,backup/estimate}`; `snap/restore/start` принимает `overwrite`.
- **Проверено:** 63 юнит-ассерта на парсеры и гарды (включая «диалог не должен принимать
  не-словарь», капы, traversal, конверсию правил) + **live e2e 48/48 на ИЗОЛИРОВАННЫХ фикстурах**
  (свой fs-репозиторий на T7: хуки реально отработали, исключение соблюдено, две точки → diff видит
  add/remove/change, pin/unpin с проверкой смены id, move-history перенёс 2 точки, fix говорит «No
  changes», blob stats/maintenance info/validate-provider/policy export живьём, restore с
  перезаписью заменил устаревший файл, log-GC не ломает репозиторий; уборка + ассерт «боевой
  kopia.json не тронут») + **CDP 24/24, ноль JS-ошибок** (Options, все 4 диалога шестерёнки
  приёмника, деталка с Estimate, обе новые секции настроек, тумблер моды, таймлайн с diff+pin,
  галка перезаписи; высота чекбокса под ховером не прыгает).
- **ГРАБЛИ CDP-прогона (стоили трёх заходов, записать):** (1) сессию мало вписать в
  `/etc/nas-os/sessions.json` — панель держит их в ПАМЯТИ, нужен `systemctl restart nas-web`, иначе
  оболочка грузится, а все `/api/*` отвечают 401 и окно рисует «no connection to the panel»;
  (2) `/json/list` первым отдаёт НЕ вкладку, а background-страницу расширения — брать
  `type=="page"` и не `chrome-extension://`, иначе весь прогон падает с «Cannot read properties of
  null»; (3) облачный репозиторий открывается секунды-десятки — ждать появления элемента в цикле,
  а не фиксированным `sleep` (с фиксированным тест то проходил, то нет).
- **Осознанно НЕ сделано** (нужен отдельный разговор): `repository set-parameters
  --retention-mode/--retention-period` (S3 Object Lock — защита от шифровальщика, требует НАТИВНЫХ
  s3/b2, не через rclone), `kopia server start` (постоянно открытый репозиторий → быстрый облачный
  Explorer + приём бэкапов с ноутбуков), `snapshot expire` (применить изменённый ретеншен сразу, а
  не со следующего снапшота), `snapshot create --stdin-file` (снапшот потока — дамп БД без
  промежуточного файла), `snapshot fix remove-files` (вырезать файл из ВСЕХ снапшотов), нативные
  бэкенды s3/b2/sftp/webdav/gcs/azure/gdrive, `notification` (у нас свои уведомления),
  `benchmark`, низкоуровневые `blob/index/content/manifest`.

**Kopia, доводка по фидбэку того же дня (2026-07-25, round 2).** Три замечания пользователя, все
по делу, и три граблей вдогонку.
- **Источник в настройках живого бэкапа больше НЕ переключается в один клик.** Свободный пикер там
  был опасен: kopia ключует снапшоты ПУТЁМ, поэтому смена источника ничего не теряет и не двигает,
  но заводит ВТОРУЮ историю рядом с первой — а выглядит как «просто выбрал другое». Теперь
  `secSource(d, locked)`: в деталке источник показан статической строкой (`kind:"static"`, в
  `wireSecs` неизвестные виды игнорируются, чтобы клик по ней не сбросил dest2) + кнопка «Edit
  folders» (то, что нужно в 95 % случаев — «защитить ещё одну папку») + тихая ссылка «change the
  source…» за подтверждением, которое объясняет про вторую историю. В визарде — как было.
- **Закрепление точки «визуально ничего не делало» — настоящий баг, найден в коде.** Обработчик
  ставил `EX.snaps=null` перед перечитыванием, а `tabSnaps` трактует `null` как «загрузи меня
  заново» и на следующем тике поллинга (3 с) вызывал `exBoot`, который выбирает САМУЮ СВЕЖУЮ точку.
  Пин применялся к прежней точке, а на экране оказывалась другая — звезда честно не горела.
  ПРАВИЛО: не используй «поле = null» как способ инвалидировать кэш, если это же значение служит
  чужому коду сигналом «инициализируйся» — race неизбежен. Плюс закрепление сделано ЗАМЕТНЫМ (об
  этом и просили): тик на таймлайне становится янтарным (заливка+ореол, не только обводка), у
  выбранной точки бейдж «KEPT», в подписи таймлайна счётчик «N kept» — КНОПКА, обходящая
  закреплённые точки по кругу (ответ на «как я потом найду запиненный снапшот»), в календаре день с
  закреплённой точкой подсвечен янтарным, корзина у закреплённой заблокирована. Кнопка на время
  запроса дизейблится (в облаке pin/unpin — это перезапись манифеста, 20-60 с).
- **«What changed» переделан под РЕАЛЬНЫЕ объёмы.** Сервер больше не буферизует вывод: `kp_snap_diff`
  стримит `kopia diff` через Popen, считает ВСЕ строки (`n_add/n_del/n_chg`), а в ответ кладёт только
  первые `KP_DIFF_CAP`=400 путей на группу + `KP_DIFF_SCAN_MAX`=400k строк и 20 мин как жёсткий
  предел (`truncated` → «числа это пол, а не итог»). **ГРАБЛЯ: итоговому JSON kopia доверять нельзя
  — `fileEntries.modified` приходит УДВОЕННЫМ** (1 изменённый файл → «modified: 2», 600 → 1200;
  проверено на обоих масштабах), поэтому итоги берём из своих счётчиков строк, а JSON только
  пропускаем. UI: диалог 900px, группировка ПО ПАПКАМ (папка + число, свёрнуто; на большом diff
  ничего не раскрыто заранее, на маленьком — первая группа), фильтр по пути, сегмент
  All/Added/Changed/Removed, в каждой папке максимум 200 строк с «+N more», честная подпись про
  капы. Проверено на изолированной фикстуре с 960 изменениями: 240/640/80 отрисовались за 3.4 с,
  16 папок × 3 секции = 42 группы, ноль JS-ошибок.
- **ГРАБЛЯ ВЁРСТКИ (чинит не только Kopia): `.dialog{max-width:440px}`** — инлайновый
  `style="width:min(900px,94vw)"` НЕ расширяет диалог, его срезает max-width, и все «широкие»
  диалоги Kopia месяцами рисовались в 440px (я поймал это, замерив `getBoundingClientRect().width`
  в CDP: ждал 900, получил 440). Конвенция панели — задавать `max-width:` (так делают старые
  диалоги), поэтому 4 диалога получили `max-width:none;width:min(...)`. При добавлении широкого
  диалога ВСЕГДА снимай/переопределяй max-width и проверяй фактическую ширину, а не свой инлайн.
- **Проверено:** движок 22/22 на изолированной фикстуре (счётчики diff точны при 240/600/120,
  списки капнуты, крошечный cap не врёт в итогах, pin меняет id но сохраняет ts, unpin чист) +
  **CDP 36/36 и 12/13** (два прогона: боевые репозитории и фикстура с большим diff), ноль
  JS-ошибок. **ГРАБЛЯ теста:** CDP-прогон на облачном репозитории оставил закреплённой РЕАЛЬНУЮ
  точку (окно ожидания 90 с не хватило на unpin через pcloud) — нашёл проверкой `pins` во всех
  боевых репозиториях и снял. Тест, который пишет в боевые данные, обязан заканчиваться проверкой
  «а что я там оставил».

**Kopia: репозиторный СЕРВЕР — чужие машины бэкапятся В этот бокс (2026-07-25).** Вкладка
**Server** в приложении Kopia, два пейна (`srvPane`): **Settings** (переключатель) и **How to
connect** (готовые команды с ПОДСТАВЛЕННЫМ адресом, отпечатком и логином — то, что никто не
запоминает). Смысл: у нас была только модель «бокс сам забирает/бэкапит», ноутбуку приехать было
некуда; SMB-шара не даёт ни версий, ни ретеншена, Syncthing синхронизирует, а не версионирует.
- **Модель.** Один сервер = ОДИН репозиторий (`server.dest` в kopia.json). Клиент делает
  `kopia repository connect server --url=https://бокс:51515 --server-cert-fingerprint=…` и логинится
  СВОИМ `login@hostname` + собственным паролем: пароля репозитория он не знает и к хранилищу
  (pcloud/диск) доступа не имеет. Дедуп общий с нашими бэкапами — один и тот же файл с трёх машин
  хранится один раз (проверено: два клиента с одинаковой папкой дали ОДИН root-oid).
- **TLS обязателен** для не-loopback, CA у домашнего бокса нет → свой самоподписанный сертификат
  (`/etc/nas-os/kopia/server/{cert,key}.pem`, 0600, SAN = LAN-IP + hostname + hostname.local +
  localhost, openssl, 10 лет), клиент ПИНИТ его отпечаток. Отпечаток = `sha256(DER)`, считаем сами
  из PEM (`_kp_srv_fingerprint`, base64→hashlib) — не дёргая openssl. Сертификат создаётся ЗАРАНЕЕ
  при первом открытии вкладки, чтобы инструкция была без placeholder'ов. **Он в СЕКРЕТНОЙ секции
  бэкапа настроек** (`nasbackup`, префикс `etc/nas-os/kopia/server/`): потеряешь — все клиенты
  откажутся подключаться, пока не перепинишь руками (та же логика, что у key.pem Syncthing).
- **ГРАБЛЯ (стоила провального прогона): пользователь, добавленный при ЖИВОМ сервере, не работает
  сразу.** kopia честно пишет «take effect in 5-10 minutes or when the server is restarted», клиент
  до этого получает `PermissionDenied: access denied for user@host` — читается как неверный пароль.
  Штатный способ — `kopia server refresh` через control API, но он требует пароль, а **сервер НЕ
  читает `KOPIA_SERVER_CONTROL_PASSWORD` из окружения** (проверено: и с верным, и с неверным env
  ответ `401 Unauthorized`) — только argv, то есть секрет в `/proc/*/cmdline`. Поэтому control API
  выключен совсем (`--no-control-api`, плюс `--no-ui` — UI у нас свой), а `kp_srv_refresh()` просто
  ПЕРЕЗАПУСКАЕТ юнит: детерминированно и без секретов. Прерванную заливку клиент возобновляет сам.
- **Пароль пользователя тоже не через argv.** `--user-password` кладёт его в командную строку, а
  `--ask-password` отказывается читать из пайпа («inappropriate ioctl for device») → `_kp_srv_pw_set`
  ведёт его ДВА промпта («Enter new password…», «Re-enter password for verification:») через **pty** —
  тот же приём, что для прогресса снапшота. Открытый пароль зеркалим в kopia.json, чтобы показывать
  в UI (как `smb-users.json`).
- **Живучесть.** Свой транзиент-юнит `nas-kopia-server` (`Restart=on-failure`), потому что рестарт
  панели убивает её cgroup; `_kp_srv_tick` в `_kopia_tick` поднимает сервер, если `enabled`, а юнита
  нет (ребут, краш, переустановка — kopia.json приезжает с бэкапом настроек, этого достаточно), при
  неудаче — событие `kp_err` с кулдауном. Порт: открывается в ufw при включении, закрывается при
  выключении, И добавлен в `_ufw_managed_ports()` — `ufw_autosync` держит его открытым сам.
- **Гарды:** приёмник, который отдаёт сервер, нельзя забыть, пока сервер ВКЛЮЧЁН (а если выключен —
  ссылка просто снимается, это было отдельной находкой: гард сначала блокировал удаление всегда);
  включение без выбранного репозитория и с портом вне 1024-65535 отклоняется; смена порта/приёмника
  на живом сервере сперва гасит старый (иначе прежний порт остался бы открытым в ufw); имя
  пользователя строго `login@hostname` (regex), пароль 4-128.
- **Чужие снапшоты видны как свои:** `kp_snapshots` теперь отдаёт `user`/`host` (ДВЕ машины могут
  иметь ОДИН путь — без владельца Explorer слепил бы их в одну ложную историю), Explorer листает,
  сравнивает, скачивает и восстанавливает их наравне с нашими. Ретеншен клиентских папок задаётся НА
  КЛИЕНТЕ — в UI это сказано прямо, чтобы не искали настройку у нас.
- **Disaster card** получила секцию «Other machines backing up into this box»: отпечаток, порт,
  логины с паролями и команда переподключения — после катастрофы клиентам нужен именно отпечаток.
- API: POST `/api/kopia/server` (status, `deep:1` — плюс список клиентов из репозитория),
  `server/set`, `server/restart`, `server/user` (add/set/remove), `server/cert` (пересоздать).
  CSS-кит вкладки: `.kp-cmd` (блок команды + Copy), `.kp-copyv` (клик-копирование значения),
  `.kp-stepn` (номер шага).
- **Проверено:** live e2e 23/23 на ИЗОЛИРОВАННОМ репозитории — сервер включился через реальный
  `kp_srv_set`, зарегистрирована машина, НАСТОЯЩИЙ клиент (свой config+кэш, другой user@host)
  подключился и залил снапшот ЧЕРЕЗ сервер, панель увидела его с владельцем и пролистала, неверный
  пароль отклонён, отпечаток совпал с `sha256(DER)`, ключ 0600, секретов в argv нет, гарды и
  авто-подъём после «краша» работают; фикстуры и тестовый приёмник вычищены, боевые сущности целы.
  **CDP 42/42, ноль JS-ошибок** (оба пейна, гард «нельзя включить без репозитория», диалог машины с
  валидацией, 5 шагов инструкции с реальными значениями, кнопки Copy).

**АУДИТ всего сделанного 2026-07-25 (8 фич + доводка + сервер): 8 находок, все исправлены;
229 ассертов зелёные.** Прогон: юниты 63/63, e2e «8 фич» 48/48, e2e diff/pin 22/22, e2e сервера
23/23, новые e2e хуков 11/11 и владельцев 11/11, гарды 8/8, CDP 42/42 + 35/35, ноль JS-ошибок,
боевые сущности и репозитории после всех прогонов байт-в-байт целы.
- **(HIGH, потеря данных) Сервер нельзя направлять в СИНХРОННУЮ запасную копию.** `repository
  sync-to --delete` делает spare байт-репликой чужого primary → всё, что туда записали клиенты,
  снесло бы следующей репликацией. `kp_srv_set` отказывает с объяснением, `kp_srv_status` отдаёт
  `spares` (карта dest→бэкап), пикер показывает такой репозиторий выключенным с причиной
  (правило: недоступный вариант лучше показать с причиной, чем спрятать). Независимая (не sync)
  вторая копия — разрешена.
- **(HIGH, корректность) Explorer группировал истории ТОЛЬКО по пути** — а с сервером две машины
  могут прислать один и тот же путь (`/home/oleg/Documents` с двух ноутбуков). Они слепились бы в
  одну перемешанную историю, и восстановление дало бы смесь. Теперь ключ истории — `владелец+путь`
  (`exKey`, `EX.key`; `EX.path` остаётся настоящим путём — он нужен для «вернуть на место»),
  `kp_snapshots` отдаёт `user`/`host` и `own` (кто МЫ), чужая история помечена в сайдбаре именем
  машины. Проверено живьём: два клиента (alice@laptopa, bob@laptopb) в одну папку → две отдельные
  истории, у каждой свои файлы.
- **ГРАБЛЯ (нашлась только тестом): NUL внутри значения HTML-атрибута не выживает.** Ключ группы
  `owner\\u0000path` я положил в `data-p`, парсер его сжевал, и клик по второй папке молча ничего не
  делал (`ps.find(x=>x.key===dataset.p)` → undefined). Лечение: в DOM — ИНДЕКС (`data-p="${gi}"`,
  выбор `ps[+n.dataset.p]`), составной ключ живёт только в JS. Правило: составной ключ в атрибут
  кладём либо как индекс, либо через `JSON.stringify` — никогда с непечатаемым разделителем.
- **Закреплённые точки были видны только в ВЫБРАННОЙ папке.** Таймлайн показывает одну папку, поэтому
  и счётчик «N kept», и «нет закреплённых тиков» отвечали лишь про неё — из другой папки найти
  помеченную точку было нельзя. Теперь счётчик считает ВЕСЬ репозиторий («3 kept · 1 here»), обход
  переключает папку и говорит, куда перешёл, а в сайдбаре у папки с закреплёнными точками звезда.
  **Та же ошибка была в МОЕЙ методике проверки:** тест утверждал «пинов нет», глядя на один таймлайн,
  и дважды оставил закреплённой боевую точку. ПРАВИЛО: наличие/отсутствие пинов проверять серверно по
  ВСЕМУ репозиторию (`kp_snapshots` → `pins`), а не по UI одной папки. Обе забытые точки сняты.
- **Тик сервера мог заблокировать монитор-нить:** при `enabled` и упавшем юните `_kp_srv_tick`
  каждую минуту звал `kp_srv_start` → `_kp_ensure_connected` (для облака до 300 с). Введён backoff
  300 с (`_kp_srv_try`), сброс при живом юните.
- **Вкладка Server открывалась медленно на облачном репозитории:** список клиентов шёл через
  `deep=1` → `kp_snap_sources` (открытие репо) на каждом входе. Теперь по кнопке «check what
  clients have sent →», а сама вкладка не ходит в сеть.
- **Уборка за удалённым приёмником была неполной:** оставались папка кэша (с логами kopia) и
  стейт-файлы новых фоновых задач. `kp_dest_forget` теперь сносит `_kp_cache_dir(destid)` и
  стейты ВСЕХ видов из `KP_BG_KINDS`; `_kp_gc` подметает `kopia-(run|maint|verify|est|fix|val)-*`
  и осиротевшие папки кэша (`_kp_cache_gc`, матч по `_KP_ID_RE`, чтобы не тронуть чужое).
  Одноразово подмёл два кэша от тестовых приёмников.
- **Мелкие:** старт прогона блокировался только maint/verify — добавлены fix/val (тоже пишут в
  репозиторий); `kp_srv_user_save` при недоступном списке пользователей пробует второй глагол
  (add↔set), а не падает на догадке.
- **Проверены ветки, которых не касались тесты:** упавший `essential`-хук РУШИТ прогон и снапшот не
  создаётся, `optional` — только логируется; снятие хуков убирает и скрипт, и action из политики;
  удалённый скрипт хука пересоздаётся из kopia.json на следующем прогоне (случай переустановки) и
  реально выполняется. Все новые POST-роуты отвечают 401 без сессии.
- **Осталось осознанно не сделанным:** immutability (`repository set-parameters --retention-mode`,
  нужен нативный s3/b2, не rclone), `snapshot expire`, `--stdin-file`, `fix remove-files`, нативные
  бэкенды, `notification`, `benchmark`.

**Kopia: кэш ответов, адресуемых object id — просмотр облачного репозитория стал мгновенным
(2026-07-25).** Серверный режим для этого НЕ нужен и не нужен вовсе: панель ходит к репозиторию
своим CLI, а не через сервер, поэтому включение сервера на скорость просмотра не влияло бы никак
(это два независимых процесса, каждый открывает репозиторий сам). Сделано дешевле и без служб.
- **Идея:** object id у kopia содержательный, значит папка с таким id НИКОГДА не меняет содержимое.
  Ответ «листинг oid/rel» (а также «поиск по oid+запрос» и «diff двух oid») можно хранить ВЕЧНО —
  инвалидации не существует по определению, нет ни TTL, ни риска устаревания. Дорога не сама
  выборка: КАЖДЫЙ промах открывает репозиторий, а для облака это ещё и запуск моста
  `rclone serve webdav`.
- **Реализация:** sqlite-стор `panel-answers.db` ВНУТРИ папки кэша приёмника (`_kp_ans_db/get/put`,
  ключи `ls:`/`find:`/`diff:`), поэтому «Clear local cache» и удаление приёмника его сносят
  автоматически. Капы `KP_ANS_MAX_ROWS`=20000 / `KP_ANS_MAX_BYTES`=64 МБ, при переполнении
  выбрасывается наименее давно использованная четверть (`ts` обновляется при чтении). Оборванный
  diff (`truncated`) НЕ кэшируется — это не окончательный ответ.
- **Список снапшотов — единственное, что меняется**, поэтому у него TTL 45 с плюс отпечаток
  «мог ли появиться новый снапшот»: `_kp_snaps_stamp` = mtime run-state файлов бэкапов этого
  приёмника. Раннер — ОТДЕЛЬНЫЙ процесс и память панели он не тронет, но файл состояния пишет
  рядом — поэтому смотрим на него, а не надеемся на таймер (тест это и поймал: после прогона
  панель до 45 с показывала бы прежний список). Пин/удаление/перенос истории сбрасывают кэш явно,
  `exReload` (после пина) запрашивает `?force=1`. Клиент, пишущий через сервер, локального следа не
  оставляет — его покрывает только TTL, это записанное допущение.
- **ГРАБЛЯ (нашлась тестом): битый sqlite-файл убивал кэш НАВСЕГДА и молча.** sqlite падает уже на
  первом `CREATE TABLE`, старая обёртка возвращала None — и чтение, и запись тихо перестали
  работать, при этом «всё выглядит нормально». Лечение: `_kp_ans_open` при `DatabaseError` удаляет
  файл и пробует ещё раз. Плюс промах больше не создаёт пустую базу (`create=False` у чтения).
- **Измерено живьём на pcloud:** листинг папки 2.77 с → 0.003 с (835×), поиск 3.04 → 0.003,
  diff 2.83 → 0.018, список снапшотов 3.41 → 0. Как это видит пользователь (замер в браузере, на
  снапшоте, который UI ещё не открывал): **10.0 с → 45 мс**. Размер стора после реального
  просмотра — 12 КБ.
- **Допущение:** листинг удалённой точки восстановления может остаться в кэше; навигация туда
  невозможна (список обновляется), а restore/download такого oid честно упадёт с ошибкой kopia.
- Тесты: 21 юнит на стор (изоляция по приёмнику, попадание НЕ открывает репозиторий, оба капа,
  самолечение битого файла, TTL/force/сброс на пине-удалении-переносе, «прогон завершился →
  список перечитан»); 178 ассертов остальных наборов перепрогнаны зелёными; CDP 28/28.

**Kopia Explorer: таймлайн переделан, загрузка стала видимой, diff — цветным (2026-07-25,
по фидбэку).** Четыре претензии пользователя, все по делу.
- **«То дата, то время» и дёрганье ленты.** Первый тик дня подписывался ДАТОЙ, остальные ВРЕМЕНЕМ —
  одно место значило две разные вещи, и, поскольку строки разной ширины, вся лента ездила под
  курсором при переходе между точками. Плюс выбранный тик становился жирным (`font-weight:750`) —
  это ТОЖЕ реflow. Теперь: **дни — заголовками над своими точками** (`.kpx-day`/`.kpx-dlab`,
  «24 JUL ②»), каждый тик — только время, ширина фиксированная (46px), выделение — ЦВЕТОМ, не
  весом, цифры табличные. ПРАВИЛО: в шкале, по которой ходят, ни ширина, ни вес шрифта не должны
  зависеть от выбора.
- **Много точек.** Лента больше не растягивает тики: при >60 точках класс `tight` (26px, время
  только у выбранной), при >200 — `tiny` (14px, точки-бусины); заголовки дней остаются, так что
  масштаб читается всегда. Прокрутка к выбранной точке сохранена (это НЕ сдвиг вёрстки — тест
  сначала принял её за баг, меряй `offsetLeft`, а не `getBoundingClientRect().left`).
- **Кнопка даты меняла ширину.** Причины две: разные цифры (лечится `font-variant-numeric:
  tabular-nums`) и **разная ДЛИНА текста** («5 files» vs «12 files»), которую табличные цифры не
  лечат — поэтому `min-width:250px` + метрики прижаты вправо (`.m{margin-left:auto}`), а внутри
  разные стили: дата обычным весом, **время акцентом**, метрики мельче и приглушены. Секунды из
  подписи убраны (шум), полный штамп остался в тултипе.
- **Лента и шапка разнесены на ДВЕ строки** (`.kpx-tlhead` + `.kpx-tlstrip`): в одну строку кнопка
  даты + действия съедали ширину, и трек схлопывался почти в ноль (поймано скриншотом, ассерты
  этого не видели — глазами смотреть обязательно).
- **Загрузку теперь видно:** бар `.kpx-busy` («Reading the repository…», бегущая полоса) над
  таблицей, появляется через 260 мс (чтобы мгновенный ответ из кэша не мигал), скелет строк
  `.kpx-skel` вместо серого «loading…», и отдельный экран `.kpx-boot` пока читается список
  снапшотов. Плюс **предзагрузка** `exWarmAround`: после открытия папки в фоне прогреваются её
  подпапки (до 6) и ТА ЖЕ папка в соседних точках восстановления — шаг по таймлайну становится
  мгновенным. Один запрос за раз, не встаёт в очередь перед пользователем (`if(EX._busy)return`).
- **Диалог «What changed» перекрашен:** сводка — три ТОНИРОВАННЫЕ плитки (`.kdf-sum/.kdf-st`,
  число сверху крупно в цвете вида, подпись снизу) вместо чужого `.kp-flow/.kp-seg`, где число и
  подпись слипались в «1ADDED»; у секций цветная левая грань и число в цвете; у файлов маркер
  `+`/`~`/`−` в цвете вида.
- Проверено: CDP 37/37 без JS-ошибок (день-заголовки, «каждый тик — время», одинаковая ширина
  тиков, лента не двигается при переборе точек, три уровня плотности, ширина кнопки даты 250/250/
  250/250, три разных стиля внутри неё, бар загрузки и его текст, три плитки сводки с зазором и
  тремя цветами) + скриншоты (лента, диалог с содержимым, состояние загрузки).

**АУДИТ №2 — кэш ответов и переделанный Explorer (2026-07-25).** 4 находки, все исправлены;
регрессия 217 ассертов зелёная.
- **Предзагрузка могла размножиться.** `exWarmAround` вызывался на КАЖДОЕ открытие папки без
  единого флага — быстрая навигация запускала несколько фоновых проходов сразу, и на облачном
  репозитории это несколько параллельных дорогих запросов с Pi. Введён `exWarming` (один проход за
  раз). Плюс жадность: 6 подпапок + 2 соседние точки = до 8 холодных чтений в фоне после каждого
  клика. Теперь `exLs` замеряет, сколько занял СОБСТВЕННЫЙ запрос пользователя (`EX._lastMs`), и
  если репозиторий медленный (>1.5 с), греются только соседние точки восстановления + 2 подпапки —
  шаг по таймлайну ценнее, чем спекулятивный обход дерева. Неудачный прогрев больше не помечается
  как сделанный (раньше временная недоступность навсегда исключала папку из прогрева).
- **Индикатор загрузки мог зажечься на чужом состоянии:** отложенный на 260 мс таймер переживал
  `exReset` (смена репозитория/закрытие) и включал полосу уже для новой сессии Explorer — таймер
  теперь гасится в `exReset`.
- **Незадекларированная зависимость:** сертификат сервера делает `openssl`; на Debian он есть
  всегда, но по правилу полноты установщика он добавлен в `UTIL_PACKAGES` визарда — иначе
  переустановка на «голом» образе оставила бы сервер без TLS без внятной причины.
- **Прогрев не должен занимать репозиторий, когда пользователь чего-то ждёт.** Клиентских
  предохранителей мало: каждый запрос — это отдельный процесс kopia, а для облака ещё и мост
  `rclone serve webdav`, и Pi это чувствует (во время прогонов load доходил до 4). Серверная
  сторона: `kp_snap_ls(..., warm=True)` — если по этому приёмнику УЖЕ идёт чтение, спекулятивный
  запрос ОТБРАСЫВАЕТСЯ (`skipped:true`), а не встаёт в очередь; счётчик `_KP_BUSY` под локом,
  реальные запросы не отбрасываются никогда. Фронт помечает прогрев `warm:1` и при `skipped`
  прекращает проход.
- **Проверено адверсарно (14 новых ассертов):** 8 потоков одновременно пишут и читают стор — ни
  одной ошибки, все 200 записей на месте; вызывающий НЕ может испортить кэшированную копию (каждый
  ответ — свой `json.loads`); read-only папка кэша деградирует в «без кэша», а не в ошибку; ключ
  разделяет приёмники, object id И путь внутри снапшота; обрезанный файл лечится и на ЗАПИСИ, а не
  только на чтении; списки снапшотов не путаются между приёмниками; появление первого run-state
  файла меняет отпечаток (первый в жизни прогон виден сразу).
- **Регрессия:** юниты 63, кэш 21, аудит-2 14, e2e «8 фич» 48, diff/pin 22, сервер 23, хуки 11,
  гарды 8, владельцы 11 — **221 ассерт**, 0 падений, боевые сущности не тронуты;
  CDP 37+35+42 = 114 проверок, ноль JS-ошибок.
- **ГРАБЛЯ прогонов:** два CDP-скрипта подряд гонятся друг с другом — каждый минтит сессию и
  ПЕРЕЗАПУСКАЕТ nas-web, поэтому второй ловит панель на старте и валится каскадом «Cannot read
  properties of null» (29 «провалов» на пустом месте). Запускать по одному; и `python3 -u`, иначе
  вывод буферизуется и фоновый прогон выглядит зависшим.

**Приложение «Syncthing» — СИСТЕМНЫЙ сервис, не контейнер (2026-07-24).**
Непрерывная двусторонняя синхронизация с ноутбуками/телефонами/другими боксами. Ставится в
АВТО-БАЗЕ (`install_syncthing` в `stage_system_apply`, `api syncthing` / `api syncthing-update`).
- **Источник — ОФИЦИАЛЬНЫЙ apt-репозиторий Syncthing**, не Debian: в trixie лежит 1.29.5 (мажор
  назад), в `apt.syncthing.net` компонент `stable-v2` даёт 2.1.2. Ключ `syncthing.net/release-key.gpg`
  в `/etc/apt/keyrings/`, строка источника с `arch=` (на RPi OS 64-бит armhf — чужая архитектура,
  без пина apt тянет индекс, из которого никогда не поставит). Дальше apt обновляет сам.
- **Свой юнит `nas-syncthing.service`** (не пакетный `syncthing@user`): home `/var/lib/syncthing`,
  от root — синхронизируемые папки в пуле принадлежат кому угодно (та же логика, что
  `force user = root` у Samba). Пакетный `syncthing@root` глушится (`disable --now`), иначе второй
  экземпляр из другого конфига. **ГРАБЛЯ:** syncthing ПАДАЕТ на старте, если не определён `$HOME`
  (`panic: Failed to get user home dir`) — он резолвит домашнюю папку в init пакета, ДО разбора
  `--home`, а у юнита своего HOME нет → `Environment=HOME=/root` обязателен. Плюс
  `STNODEFAULTFOLDER=1` (не создавать `~/Sync`), `SuccessExitStatus=3 4` (3 = рестарт из GUI, 4 =
  апгрейд — перезапускает systemd).
- **GUI — только loopback, панель его ПРОКСИРУЕТ** (`/syncthing/*` → `127.0.0.1:8384`,
  `_st_serve`/`st_proxy` в nas-web.py) за своим логином, поэтому в локалке нет неавторизованной
  админки и отдельный пароль не нужен. Работает это потому, что весь SPA Syncthing
  **относительный** (`urlbase = 'rest'`, `assets/…`, `vendor/…`) и **без websockets** (только
  long-poll `/rest/events`), а окно панели — тот же origin, что удовлетворяет их
  `X-Frame-Options: SAMEORIGIN` (прямой iframe на `:8384` был бы заблокирован). Прокси добавляет
  заголовок `X-API-Key` из config.xml — это и авторизует, и снимает их CSRF; хоп-бай-хоп заголовки
  и `Accept-Encoding` срезаются, `Location:/…` переписывается в `/syncthing/…`. Ради прокси
  добавлены `do_PUT/do_DELETE/do_PATCH/do_HEAD` (`_st_only`) — свой API панели говорит только
  GET/POST. Таймаут апстрима 180 с (их long-poll — 60 с).
- **Настройки → Syncthing** (`syncthingTab`): состояние службы + Restart/Update, device ID (Copy),
  тумблер **«Reachable on the local network»** (`st_set_lan` — правит адрес через ИХ REST
  `PUT /rest/config/gui`, НЕ редактированием config.xml под живой службой: она перезаписывает файл
  сама; плюс открывает/закрывает порт в ufw и честно предупреждает, что своего пароля у GUI нет),
  тумблер **«Match the panel's theme»** и ручной выбор темы.
- **Тема Vellum** (github.com/pelinoleg/syncthing-vellum) ставится обе (light+dark) в
  `<home>/gui/`, активной делается `vellum-light` — но ТОЛЬКО пока в конфиге стоит `default`
  (осознанный выбор пользователя переустановка не затирает). Дальше тему ведёт ПАНЕЛЬ:
  `SET.stFollow` (по умолчанию вкл., НЕ в `THEMED_KEYS` — это одна настройка про обе темы, а не
  значение на тему) + `syncthingFollowTheme()` в ветке смены темы `applySettings()` и разово на
  загрузке. Сервер (`st_set_theme`) при совпадении темы НИЧЕГО не пишет и отвечает
  `unchanged:true` — иначе каждая загрузка панели переписывала бы их конфиг; при реальной смене
  открытое окно перезагружается (`stReloadWindow`), иначе в нём остаётся старый CSS.
- **Иконка** — родной логотип (RAW_LOGOS.syncthing, дал пользователь). Оптический баланс:
  диск full-bleed, поэтому `translate(-8.1,-8.1) scale(0.684)` (≈125 % бокса 64) — на глаз в один
  вес с Cron/Kopia/Disk usage; мерил шириной «чернил» через CDP (`getBoundingClientRect` по всем
  фигурам), а не CSS-размером.
- **ufw**: открываются только порты синхронизации `22000/tcp+udp` и `21027/udp`; GUI-порт — только
  когда его сознательно выставили в локалку.
- **Бэкап настроек**: секция `syncthing` (секретная) — `config.xml` + `cert.pem` + `key.pem`.
  Identity бокса ЭТО и есть key.pem: потеряешь — NAS вернётся чужим устройством, и его придётся
  принимать заново на каждом пире. База индексов НЕ бэкапится (она пересобирается из файлов).
  При восстановлении файлы кладутся 0600 в 0700-папку и служба перезапускается (живой syncthing
  держит конфиг в памяти и записал бы поверх восстановленного).
- **Проверено живьём**: установка через `api syncthing`, прокси (index/assets/REST/POST/HEAD,
  302 с `/syncthing` на `/syncthing/`, 401 без сессии), CDP 12/12 (иконка, окно с их интерфейсом
  внутри, фон `rgb(233,231,225)` = Vellum light, вкладка настроек) и CDP 9/9 на авто-тему
  (light↔dark ведёт Syncthing за собой, тумблер её отключает, ручной выбор работает).
- **ГРАБЛЯ (исправлена): смена темы «не работала» — 304 Not Modified.** Каждая тема отдаёт свой
  файл по ОДНОМУ И ТОМУ ЖЕ адресу `assets/css/theme.css`, а Syncthing валидирует его по mtime;
  оба Vellum скопированы `cp -r` в одну секунду, поэтому браузер, закэшировавший светлую тему,
  спрашивал «изменилось с тех пор?» и получал 304 — в конфиге тема менялась, на экране нет
  (в настройках при этом честно показана новая). Лечение в прокси (`_ST_NOCACHE`): для
  `theme.css` и `img/logo-horizontal.svg` срезаются условные заголовки запроса и
  `Cache-Control/ETag/Last-Modified` ответа, отдаём `no-store`. ПРАВИЛО: разные файлы под одним
  URL нельзя валидировать датой — им кэш противопоказан. Плюс смена gui-конфига заставляет
  Syncthing перезапустить свой слушатель, и запрос в эту щель показывал «not answering» —
  прокси делает один ретрай через 0.8 с (только для запросов без тела), а перезагрузка окна
  отложена на 1.2 с.
- **Docker-версия Syncthing СНЕСЕНА** (2026-07-24, по просьбе пользователя): удалён стек
  `/opt/stacks/syncthing/` (был с pi5, контейнера не было) и рецепт каталога `services/syncthing/`
  — в докере он больше не нужен и не должен предлагаться, иначе конфликт портов 8384/22000
  с системной службой и второй ярлык на столе.
- **ГРАБЛЯ иконки Docker (исправлена):** whale уже занимает ВСЮ ширину своего бокса 24×24
  (bbox x=0, w=24), поэтому прежний `scale(1.22)` выносил его за viewBox и срезал оба плавника.
  Логотип широкий и низкий по своей природе — зумом это не «чинится». Теперь
  `translate(0.6,0.6) scale(0.95)` (небольшой отступ, как у соседей). ПРАВИЛО: перед подкруткой
  масштаба логотипа смотри его `getBBox()` — если арт уже упирается в границы viewBox, любой
  масштаб >1 это обрезка, а не увеличение.

**Kopia: красные точки на настенном экране — оба облачных бэкапа падали ДО снапшота (2026-07-27).**
Экран не врал: `last:"error"` у обоих ночных прогонов (03:30 и 04:30), оба на фазе `policy`, оба
в pcloud. Разбор дал ДВЕ разные причины, обе наши.
- **kopia запускает СВОЁ обслуживание побочным эффектом любой команды**, которая первой подвернулась
  после того, как цикл стал «должен». Подвернулась наша двухсекундная `policy set` в начале бэкапа —
  и в логе видно `Running full maintenance...` прямо посреди неё. На облачном репозитории полный
  цикл это минуты листингов и компакции, а у вызова стоял таймаут 60 с → `policy: timed out after
  60s`, бэкап умер, не сделав ни одного снапшота. Обслуживание у нас И ТАК своё (еженедельный
  `maintenance run --full` в отдельном юните), поэтому автоматическое просто выключено:
  `_kp_automaint_off` (`maintenance set --enable-quick=false --enable-full=false`, флаг
  `automaint:"off"` у приёмника, ставится при подключении и в начале прогона). Проверено на
  изолированном fs-репозитории (10 ассертов): после выключения `policy set` больше не запускает
  цикл, а НАШ явный `maintenance run --full` работает как работал. ПРАВИЛО: если инструмент умеет
  делать тяжёлую работу «когда придётся», а мы делаем её по расписанию — автоматику надо гасить,
  иначе она однажды случится внутри чужой короткой операции.
- **pcloud через rclone-мост kopia ПЛАВАЮЩЕ теряет мелкую запись**: `unable to write session
  marker … BLOB not found` на `PutBlob` длиной 197 байт. Замерено: в одну минуту падает 3 попытки
  из 3, через десять минут проходит 10 из 10. Прямой `rclone rcat`/`ls`/MKCOL в тот же путь
  работают всегда — то есть виноват не путь и не права. **Гипотезу «нужен `--vfs-cache-mode=writes`»
  (rclone сам её советует для pcloud) проверил парным A/B: не подтвердилась** — конфигурация без
  кэша дала 5/5 успехов, с кэшем 4/5. Значит лечится только настойчивостью: `_kp_retry` для
  ИДЕМПОТЕНТНЫХ команд (policy set, maintenance set) с растущими паузами 5/15/45 с — сбои идут
  ПАЧКАМИ, и три попытки по 6 с все укладывались в одну плохую минуту; плюс ОДИН повтор самого
  снапшота при транзиентной ошибке (kopia инкрементальна — то, что уже уехало, повторно не шлётся).
  Классификатор `_kp_transient` (blob not found / session marker / reset / 429 / 423 Locked /
  timeout…) специально НЕ считает транзиентными «invalid password» и «no such file».
- **И главное про приоритеты: неудачная запись ПРАВИЛ больше не выбрасывает СНАПШОТ.** Правила
  (ретеншен, исключения) хранятся В РЕПОЗИТОРИИ и остаются там с прошлого прогона, поэтому если
  повторы не пробились, а правила когда-то уже применялись (`policy_ts` у задания или удачный
  прогон в истории — `_kp_policy_ever`), прогон продолжается со снапшотом и заканчивается как
  `warn` с явной строкой «правила переутвердить не удалось, действуют ранее сохранённые». Совсем
  новое задание, у которого правил в репозитории ещё нет, по-прежнему падает — там продолжать
  нечем. ПРАВИЛО: подготовительный шаг не имеет права стоить главного, если его результат уже
  есть в системе.
- **ГРАБЛЯ моей же правки (поймана до коммита):** внутри раннера уже был локальный список `tail`
  (последние 25 строк лога, в него пишет `w()`), а я в новой ветке присвоил `tail = "\n".join(...)` —
  это перезаписало бы ЗАМЫКАНИЕ, и следующий же `w()` упал бы на `str.append`. Отдельное имя
  `tail_txt`. Перед вводом новой переменной в длинную функцию — grep по имени в её теле.
- **Результат живьём:** оба задания прогнаны вручную и зелёные (17 файлов / 10 МБ и 132086 файлов /
  17 ГБ, у обоих restore-drill 3/3 байт-в-байт), экран показывает `ok`. Юнит-тестов 12 (классификатор,
  повторы, backoff, `_kp_policy_ever`) + 10 на изолированном репозитории.

**ЦЕНА этой дыры, измеренная по истории:** ежедневный Mirror-бэкап не прошёл 24.07 И 26.07 —
двое суток за неделю, и никто не заметил, потому что единственным сторожем был `nb_stale` с
порогом 7 дней. У Kopia очередь `pending` уже была, у Mirror не
было ничего. Проверено 16 ассертами (выбор слота, включая DST-сутки и weekly-переход через
полночь; догон один раз; отказ догонять просроченное/уже отработанное/сохранённое позже) и живьём:
после рестарта тик сказал «the 2026-07-26 15:00 run was missed — starting it now, 381 min late» и
поднял настоящий прогон.

**визард** (незавершённый setup_started резюмится с win._kpWizPos). Визард 6 шагов: What (выбор/
создание Source) → Where (Destination) → Spare (3-2-1, sync/independent) → Retention (**пресеты
§9.4**: Year of history дефолт / Couple of months / Forever+annuals / Custom, превью «≈N restore
points») → Schedule (Manual/Daily/Weekly + **USB-триггер §9.3**) → Finish (имя+сводка); сущность
сохраняется НАЧИНАЯ с шага Where (source+dest есть), Finish/Set-up-later пишут setup_done.
**ГРАБЛЯ вёрстки:** футер мастера имел `class="r"`, а правило есть только у `.dialog .r` — вне
диалога класс НЕ СТИЛИЗОВАН, и кнопки Back/Next слипались без отступов; отдельный `.kp-wfoot`
(flex+gap+разделитель сверху). **Длинные пути в диалоге источника:** строка папки — две строки
(`.kp-wpath`: ИМЯ папки жирным + полный путь моно-строкой с ellipsis), список папок скроллится
(`.kp-wlist`, max-height min(34vh,240px)) — раньше `text-overflow` не работал вовсе (не было
`nowrap`/`min-width:0`), путь заворачивался и распирал диалог. Обрезаем ХВОСТ, а имя папки —
отдельной строкой: `direction:rtl` для обрезки спереди пробовал — с `unicode-bidi:plaintext`
эффекта нет, а без него ведущий «/» уезжает в конец (та же bidi-грабля, что с «Selected: pcloud:»).
Проверено CDP: 15/15 (карточки вместо select, тег занятости, клик по занятому ничего не делает,
зазор кнопок футера, длинный путь не заворачивается и не вылезает за диалог).
**АУДИТ №2 «все фронты» (2026-07-23): HTTP-слой + живой E2E + инъекция сбоев + интеграции +
переживаемость рестарта + adversarial security-агент. ~50 живых проверок, 7 находок исправлены.**
Фронт1 HTTP (curl): 12/12 авторизация (401 без cookie на всех GET/POST), формы верные, инъекция в
argv не проходит (`{"id":"; rm"}`→`no such destination`), НО не-dict JSON тело (`[]`,`5`) давало 500 →
`_body()` теперь коерсит любой не-объект в `{}` (кросс-эндпоинтный фикс всей панели). Фронт2 E2E на
ИЗОЛИРОВАННЫХ фикстурах (тест-source/dest/backup на `/media/nas/t7-4TB/.kp-audit`, чистятся): 27/27 —
полный цикл create→run→snapshot→browse→search→restore со **сверкой sha256 восстановленных файлов**,
юникод/пробелы/вложенность в путях, honored exclude, 3-2-1 spare=полноценный репо, mount/ФС/unmount+
эндпоинт ФМ, инкремент, delete, maintenance, cache clear. Фронт3 инъекция сбоев: 2 конкурентных
старта→второй отказан; kill драйвера→детект not-running; порча run-state/kopia-state→graceful; отмена
посреди→stopped+история; **F3 FAIL: репо пропал посреди снапшота→прогон висел >600с** (у snapshot-
драйвера НЕ было предела). Фронт4: маунт+статус переживают рестарт nas-web (транзиент-юниты). Фронт5:
kopia.json в бэкапе настроек ✓, disaster card с паролем ✓, glance-плитка ✓, события kp_* в каталоге ✓.
