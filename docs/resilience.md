# Resilience — переживаемость

_История и подробности. Открывай, когда правишь приложение Resilience, чёрный ящик, дрель ребута, журнал износа._
_Правила и грабли, которые важны ВСЕГДА, живут в `CLAUDE.md` — здесь только детали._

**Приложение «Resilience» (`winResil`, dock `__resil`, иконка-спасательный круг `lifef`, 2026-07-22)** —
«переживёт ли бокс плохой день». Пять модулей, API `/api/resil/*`, серверный блок в nas-web.py перед
monitor_loop, тик `_resil_tick` в monitor_loop. ЖЕЛЕЗНЫЙ принцип дизайна: НИЧЕГО не настраивается и не
инвентаризируется руками — каждая картина пересобирается из ЖИВОЙ системы на каждом прогоне (сменил
диск/стек — следующий прогон сам увидит; та же грабля, что список UAS-мостов).
- **Black box** — бортовой самописец: СВОЙ юнит `nas-blackbox.service` (`install_blackbox` в авто-базе
  визарда, `api blackbox`; ExecStart=`nas-web.py blackbox-daemon` — CLI-режим как thumbs-sweep; свой
  юнит потому, что рестарт nas-web убивает всю cgroup — грабля sshfs). Семпл каждые 10 с (load/CPU%/
  mem/temp/throttled/top-процессы + dmesg-хвост) в кольцо на 90 семплов: каждый семпл → `/run/nas-blackbox/`
  (tmpfs, ноль износа SD), каждый 3-й → `/var/lib/nas-wizard/blackbox/current.json` (переживает обрыв
  питания). На старте кольцо ЧУЖОГО boot_id архивируется в `flight-*.json` (хранится 20) + строка в
  `boots.jsonl` с вердиктом clean/dirty: маркер `clean-shutdown` пишет ExecStopPost, нет маркера =
  обрыв питания/краш → событие `dirty_boot` (шлёт `_resil_tick` по новой записи в boots.jsonl).
- **Reboot drill** — аудит «поднимется ли всё после ребута» БЕЗ ребута: running-но-disabled юниты
  (transient nas-remote-*/nas-rclone-* скипаются — их владельцы сами поднимают), критичные юниты
  (nas-web/docker/nas-stacks/smbd/avahi/netguard.timer/blackbox), fstab↔/proc/mounts в ОБЕ стороны
  (маунт без fstab = «не вернётся»; fstab без диска без nofail = «повиснет boot»; fuse/rclone/remote и
  automount-база из automount.conf скипаются — они самовосстанавливающиеся), `findmnt --verify`,
  контейнеры с restart=no ВНЕ автостартуемых стеков (стеки поднимает nas-stacks — политика не важна).
  Score 100−20×bad−7×warn → `resil-drill.json`; еженедельный авто-прогон, блокеры → событие `resil_drill`.
- **Config history** — git-снапшоты конфиг-поверхности в `/var/lib/nas-wizard/confgit` (root 0700 —
  там shadow): rsync /etc (минус volatile: mtab/adjtime/*.dpkg-*…), composes+.env из /opt/stacks,
  топ-левел *.json из nas-config (минус журналы/кэши: events, nas-backup-history*, duscan-* и т.п.).
  Ежедневно + кнопка; коммит только при изменениях. UI (переделан после фидбэка 2026-07-22): НЕ сырой
  гигантский патч — снапшот раскрывается аккордеоном в список файлов по человеческим областям
  (`cfgArea`: Samba/Network/systemd/стек X/Panel settings…), клик по файлу → дифф ТОЛЬКО этого файла
  (`/api/resil/confgit/{files,filediff}` — name-status+numstat / show -- path); baseline-коммит
  («initial snapshot») вместо списка объясняет, что это стартовая точка. ГРАБЛЯ вёрстки: паттерн
  `.nbmain`+`.set-nav` скроллит ТОЛЬКО .set-pane лишь если body окна — flex-колонка с overflow:hidden
  (`body.style.display="flex"` и т.д., как Settings/ФМ) — иначе скроллится всё окно вместе с сайдбаром.
- **Disaster card** — авто-документ `~/nas-config/disaster-card.md` («бокс умер — что делать»: диски
  с серийниками, fstab, snapraid, шары, стеки+образы, профили бэкапа, шаги восстановления). Пересборка
  ежедневно; лежит в nas-config → уезжает с бэкапом настроек сам. UI: просмотр+Download+Rebuild.
- **Log sentry** — детектор НОВЫХ паттернов ошибок в journald (`-p 3`, курсор в `logsentry.json`):
  нормализация сообщения (числа/hex/пути → #), первые 24 ч — обучение базлайну, потом новый паттерн
  с ≥3 повторами → событие `log_sentry` (один раз на паттерн); Mute в UI. Скан раз в 5 мин в тике.
- **Write load** (вкладка + карточка Overview, 2026-07-22) — износ SD/системного диска: `_writes_sample`
  в тике (60 с) копит дневные вёдра из `/proc/diskstats` (поле 10 × 512, по базовым дискам
  sd*/mmcblk*/nvme*n*) и ПО-ПРОЦЕССНО из `/proc/<pid>/io write_bytes` (identity pid+starttime —
  переиспользованный pid не наследует чужой базлайн; первый семпл процесса = только базлайн).
  Стейт `/var/lib/nas-wizard/writelog.json` пишется раз в 5 мин (лог износа не должен сам изнашивать),
  90 дней, топ-20 писателей/день. Ребут → счётчики ядра обнулились → кредитуем sect×512 сегодняшнему
  дню, НО только если uptime < 6 ч (иначе первый запуск на давно работающем боксе свалил бы дни старых
  записей в «сегодня»). UI: GB/день (7-дневное среднее), «полная перезапись карты раз в ~N дней»,
  бар-чарт по дням, топ писателей сегодня/за неделю (писатели — по ВСЕМ дискам: ядро не делит по
  устройствам). Пороги вердикта: SD 5/20 ГБ/день, SSD 40/150. `/api/resil/writes`.
- **Boot time trend** (секция во вкладке Black box) — `_bootlog_tick`: раз на загрузку парсит
  `systemd-analyze time` (ретрай, пока startup не finished; формат «1min 7.716s» понимается) + top-12
  `blame` → `/var/lib/nas-wizard/bootlog.jsonl` (60 записей; дедуп по ts последней записи — потеря
  resil.json не дублирует загрузку). UI: бар на загрузку, клик → самые медленные юниты той загрузки.
  `/api/resil/boottime`.
- **Авто-диагноз полёта** (2026-07-22): `_bb_diagnose(flight)` — гипотеза причины смерти по последней
  минуте кольца + dmesg-хвосту: power (thr-бит 0x1 / «Under-voltage» / 5V < 4.75), heat (≥80°),
  oom (oom-kill в dmesg / mem ≥93%), storage (I/O error/offlined/emergency_ro), iohang (load ≥8 при
  CPU ≤25%). Приоритет причин: power > heat > oom > storage > iohang; evidence-строки сохраняются все.
  Диагноз пишется в flight и boots.jsonl при rollover; старые полёты диагностируются лениво в
  `blackbox_flight`. В семплы добавлен **вольтаж 5V** (`vcgencmd pmic_read_adc EXT5V_V`, только Pi 5;
  проба один раз — `_BB_HAS`, на pi4 тихо выключается). UI: чип «likely: …» на записи ребута
  (тултип = evidence), плашка в просмотре полёта, график «5V rail» появляется при наличии данных.
- **Fix-кнопки дрели**: issue несёт структурированное `fixa` ({a:enable|enable_now|mount|nofail|
  docker_restart, unit|mp|name}), POST `/api/resil/drill/fix` валидирует ЗНАЧЕНИЯ (regex юнита/
  контейнера, mp обязан быть в fstab) — свободных команд нет; после фикса сразу пере-прогон дрели.
  `_fstab_add_nofail(mp, path)` правит одну строку fstab (бэкап `.nasos-bak`, daemon-reload),
  тестируема через параметр path. masked-юнит Fix не предлагает (нужен unmask руками).
- **Событие `write_load`** (каталог монитора, threshold=20 ГБ/день): проверка в тике раз в 6 ч —
  3 ПОЛНЫХ дня подряд выше порога → уведомление (cooldown 3 дня). Сегодняшний частичный день не судится.
События в каталоге монитора: `dirty_boot`(prio 1)/`resil_drill`/`log_sentry`/`write_load` (+ лейблы в notifyTab).
**Аудит 2026-07-22 (все фиксы в коде):** (1) sentry: протухший journal-курсор (ротация/vacuum)
вешал скан НАВСЕГДА — теперь курсор сбрасывается и скан пересеивается; (2) вердикт clean/dirty:
маркер учитывается только если mtime ≥ последней записи кольца − 120 с (маркер от давнего ручного
stop не обеляет последующую смерть); (3) ts записи в boots.jsonl = `updated` погибшего кольца
(момент смерти), НЕ «сейчас» — на Pi без RTC часы при старте стухшие; (4) confgit исключает
`ssl/private`; (5) resil_writes ретраит RuntimeError (гонка с тиком-мутатором); (6) логика
архивации вынесена в `_bb_rollover(boot_id)` — тестируема отдельно (4 кейса: power cut / clean /
stale marker / same boot — все проверены в песочнице через подмену `m.BB_VAR`). Guards проверены:
flight-имя по regex, filediff режет `..` и ведущий `-`, mute несуществующего ключа. Известные
допущения: первый _resil_tick свежей установки делает initial confgit-снапшот (~30-60 с в
monitor-нити, разово); top-писатели по ВСЕМ дискам (ядро не делит write_bytes по устройствам);
ps %cpu в семплах — среднее за жизнь процесса, не мгновенное.
**UX-редизайн по фидбэку (2026-07-23, «apple way, не кокпит»): чистая поверхность + прогрессивное
раскрытие, функционал не удалялся.** (1) **ГРАБЛЯ «прилипает» найдена**: `.rs-card:hover` был БЕЗ
`@media(hover:hover)` — на таче hover-подсветка ЗАЛИПАЛА после тапа (чинит и Resilience — класс
общий); правило: интерактивные hover-стили ВСЕГДА под `@media(hover:hover)`; некликабельные
карточки — класс `.kp-static` (без cursor:pointer/hover). (2) Деталка: заголовок = back+имя+ОДНА
главная кнопка (Back up now/Stop)+шестерёнка (меню: Rename…/Delete; карандаш убран, native
`prompt()` заменён дизайн-системным диалогом `kpAskName`); **Settings свёрнуты в аккордеон**
`.kp-disc` (состояние в `win._kpSetOpen`, рендер ленивый); статус-строка обновляется live вместе
с прогрессом. (3) Destinations: 5 кнопок → **[Check]** (реachability+занятость одним нажатием —
отвечает на «всё ли хорошо?») + шестерёнка-меню {Run maintenance now / Clear local cache (с
размером в confirm) / Remove (спрятан, если dest используется)}. (4) Sources: клик по карточке =
редактирование, футер = тихая ссылка «measure size» + корзинка-иконка. (5) Snapshots: кнопки
Browse/Restore/Delete у строк УБРАНЫ — строка `.kp-row` кликабельна целиком (шеврон-подсказка) →
диалог снапшота, Restore и Delete живут ТАМ (drill in → act there); чипы-фильтры через класс
`.kp-on`, не инлайн-стили. (6) Мелочи: «Set up later» — тихая текстовая кнопка `.kp-quiet`, даты
Recent runs компактные (`dtShort` «Jul 23, 09:07»). Всё сверено скриншотами через CDP-прогон.
