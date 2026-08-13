# Mirror (rsync/rclone) — зеркальные бэкапы

_История и подробности. Открывай, когда правишь приложение Mirror, rsync/rclone-движок, профили или restore-drill._
_Правила и грабли, которые важны ВСЕГДА, живут в `CLAUDE.md` — здесь только детали._

**Общий доступ (SMB)** — вкладка настроек `sharingTab` + модуль `smb` в nas-web.py: шары
(папка+имя; открытая-гость ↔ список юзеров; read-only) и SMB-юзеры (создание, показ пароля,
смена, удаление) прямо из панели; см. граблю про `include` в smb.conf. Cockpit УДАЛЁН целиком
(2026-07-20: под arm64/trixie нет `cockpit-file-sharing`, родное управление в панели вместо него).
Docker master-detail, **«Бэкап»** (мини-приложение, профили с направлением: pull «забрать с другого
NAS» по rsync-демону/SSH И push «отправить с этого NAS» на внешний диск (transport=local) или другой
сервер по SSH; визуальный выбор путей — для push дерево локальной ФС через тот же /api/backup/browse;
опц. пост-сверка `verify` (rsync --checksum -n, событие nb_verify); push-ssh: mkdir/чистка архива
удалённых по SSH, ретеншен только по дням; **push через rclone** (третий transport «Cloud (rclone)»:
S3/B2/SFTP/WebDAV/Drive и т.д.) — подключения НЕ настраиваем в панели, пользователь делает ремоуты на
Маке (`rclone config`) и вставляет `rclone.conf` во встроенный CodeMirror-редактор (`/api/backup/rclone*`;
файл `/etc/nas-os/rclone.conf` root 600, в секретной секции бэкапа настроек). Движок — `_nb_rclone_run`
(`rclone copy` = add/mirror=`sync`/archive=`sync`+`--backup-dir`; `--use-json-log --stats 1s`, прогресс
пишется в формате rsync-progress2 → парсер `nbProg` работает без правок; страж удаления через
`--max-delete`; **ретеншен архива `_deleted` — по возрасту (дни)**, `_nb_rclone_prune`: `lsjson
--dirs-only` под `remote:remote_path/<top>`, `purge` папок старше N дней по ModTime; GB-лимит для
rclone/push-ssh не применяется — сайзить архив по сети дорого; UI прячет поле GB и правит превью
пути под `remote:`). Бинарь ставится ВСЕГДА официальным бинарём (`install_rclone` в auto-base, НЕ apt —
чтобы работал `rclone selfupdate`); кнопка «Update rclone» = `api rclone-update`.
**Централизованное приложение «Cloud (rclone)» (`winRclone`, dock/launchpad `__rclone`)** — ВЕСЬ
rclone здесь, вкладки `.set-nav`: **Remotes** — ДАШБОРД (2026-07-21, `rclone_dashboard()` +
`tabRemotes` переписан): у каждого remote авто-пробы (точка достижимости из `/test`, полоса квоты
из `/about`), бейдж типа (`rclone_remote_types()` — config dump→{name:type}), «Used by <профили>» +
последний прогон/недавние байты (из nb_profiles+history); действия Analyze/Dedupe/Clean up/Mount/
Restore. **Анализатор места «Cloud space»** (`rcloneDuDlg`, движок `_rclone_du_worker`/`rclone_du_start`/
`rclone_du_status`): фоновый `rclone lsf -R --format sp --fast-list` → дерево папка→размер (агрегация до
`RCLONE_DU_DEPTH`=8, топ-`RCLONE_DU_TOPN`=40 детей/папку, остальное→«…other», кап `RCLONE_DU_MAXLINES`),
стрим со стейт-файлом `rclone-du-<remote>.json`, drill-down с барами-долями и хлебными крошками;
**кэш переиспользуется `RCLONE_DU_TTL`=6ч** (полный lsf долбит API) — `force` рескан только по кнопке
Rescan. `_rclone_dedupe`(--dedupe-mode newest)/`_rclone_cleanup` — на remote целиком (destructive →
confirmDlg). API `/api/backup/rclone/{dashboard,dedupe,cleanup,du/start,du/status}`. Проверено live
на pcloud (137k файлов, 1.3ТБ). **Разовое копирование remote→remote** (`rcloneCopyDlg`, кнопка
«Copy between remotes…» в шапке Remotes при >1 remote; движок `_rclone_copy_cli`/`rclone_copy_start`/
`rclone_copy_status`/`rclone_copy_cancel`, транзиент-юнит `nas-rclone-copy`, env `RCC_SR/SP/DR/DP/DRY`,
state/log `rclone-copy.*`): `rclone copy srcRemote:path → dstRemote:path` — БЕЗ локального стейджинга
(rclone умеет remote→remote напрямую). **COPY ONLY** — источник не трогается, на приёмнике ничего не
удаляется (только add/update); guard `sr==dr&&sp==dp` («same place») + валидация обоих remote. Dry-run
превью, прогресс/Stop тем же `_rclone_progress_line`. API `/api/backup/rclone/copy/{start,status,cancel}`.
Проверено live pcloud→rsync.net: dry-run пишет НОЛЬ, реальная копия 3 файла (`rclone check` 0 differences),
источник байт-в-байт цел, guards режут same-place/битый remote. `rcloneRemotePicker` получил 3-й арг
`onClose` (перерисовать родительский диалог после выбора/отмены). Прочие вкладки: **Remotes** (Test/Quota=`about`/Size/Mount/Restore у каждого),
**Mounts** (`rclone mount` remote→`/mnt/rclone/<name>`, виден в ФМ; read-only по умолчанию; свой
транзиент-юнит `nas-rclone-mnt-*` как sshfs-серверы; auto-remount `_rclone_mounts_tick`;
`--allow-other` требует `user_allow_other` в `/etc/fuse.conf` — ставит `_ensure_user_allow_other`
и визард), **Verify** (`rclone check` local↔remote по чек-суммам, read-only, bg-op
`nas-rclone-check`, `--combined` разбор), **Config** (редактор+версия+install/update),
**Options** (глобальные `rclone-opts.json`: transfers/checkers/bwlimit/vfs-cache-mode/size —
`_rclone_perf_args` применяется к push/restore/mount). API `/api/backup/rclone/{mounts,mount,
unmount,mount-remove,about,size,opts,check/*}`. Приложение Бэкап только ВЫБИРАЕТ remote; кнопка
«Cloud (rclone)» в шапке и «Manage remotes» в профиле открывают `winRclone`.
**ГРАБЛЯ (исправлена)**: свежий профиль по умолчанию `direction:pull`, и выбор dst=rclone молча
откатывался (сборка сторон уходила в pull-ветку, где rclone игнорируется). Лечение: в `nb_save`
ветка `cd["kind"]=="rclone"` ПЕРВОЙ — облачное назначение ВСЕГДА делает профиль push (источник
принудительно local). rclone как ИСТОЧНИК (pull-профиль cloud→NAS) пока не сделан — on-demand
покрывает Restore; KINDS_SRC без rclone.
**cloud→cloud (2026-07-21):** rclone умеет remote→remote напрямую (без локальной промежуточной папки, в отличие от rsync SSH→SSH+sshfs). Модель: src=rclone (cfg.remote), dst=rclone хранится в `dst2={kind:rclone,remote,remote_path}` (direction=pull). Предикаты `_nb_rclone_c2c`/`_nb_c2c_dst`; `_nb_rclone_cmd` копирует `src_remote:job.src → dst_remote:path`, archive-backup-dir и `_nb_rclone_prune` идут на DST-remote; `nb_test` проверяет ОБА remote; `_nb_remote_both` возвращает False для rclone (нет sshfs-моста); `nb_dest_state` — облачный dst. UI: `dstKinds` даёт Cloud при облачном источнике, `rcloneDstBody(S,c2c=true)` (свои id `rc_dremote/rc_dpath`, сохраняет через `{dst:{...}}`), `isC2C()`/`c2cDst()`, визард пропускает «Where to». Проверено live: dry-run pcloud→rsync (`both remotes reachable`, result=ok). **6 раундов аудита c2c/бэкапа (round 3–7, 2026-07-21), сводка** (детали — в git-логе коммитов «audit»/«fix»): визард c2c (Finish кажет обе стороны; смена вида в визарде делает полный render, не пропуская обязательные шаги); HIGH data-loss — rsync-раннер guard'ил пустой/пропавший источник только для push, теперь и pull/SSH→SSH (empty через stage; plain-pull — отказ от uncapped-delete на заполнённый приёмник без baseline); pull SSH→SSH везде трактовался как локальный (введён `_nb_dest_ssh`); безопасность — `nb_public` не отдаёт `dst2` пароль, remote-regex без ведущего `-`, `--` перед rsync-позиционными; конкурентность — кросс-процессный flock `_NbStartLock` на старт/дрейн; deletion-safety скипы несут `code:25`+`src_block` → свой баннер и ОДНО подтверждение полного эффекта глобального allow_delete; rclone health-уведомления; куча LOW (schedule `type=time`, dst2-порт-кламп, fuse anchor, prune sibling, per-PID `.tmp`, orphan-lock). Реальные 3 профиля целы; каждый шаг проверен ассертами + live dry-run/compare через транзиент-юниты. **Аудит визарда c2c + 3 агента (2026-07-21, round 2):** (1) HIGH — смена типа приёмника/стороны НА шаге визарда меняла форму `WSTEPS()`, но пейн-only `again()` не пересчитывал `wizStep`/`cur` → push→Cloud перескакивал обязательный «What to copy» (Finish без папок); лечение: в визарде смена вида делает полный `render()` + защитный `!isC2C()` в guard'е «dest». (2) LOW-MED — одноуровневый `_deleted/{year}` (не nested) датировался 1 января → `rclone purge` сносил весь год; лечение: `_nb_deleted_day_bucket` (прунить только `<top>/<day-level>`) + убран `^(\d{4})$` из парсера. (3) HIGH/MED — **pull SSH→SSH** (dst2.kind=ssh) во многих местах проверялся `_nb_push_ssh` (push-only) → трактовался как локальный: `nb_dest_state` давал ложные «not mounted», лишний Separate-пикер/GB-поле, epOk, валидация путей, `--chown`/backup-dir, dest_fs, size-monitor. Введён `_nb_dest_ssh(cfg)` (=`_nb_sides(cfg)[1].kind=='ssh'`, ровно как промоут в nb_run:6807) и JS `isDstSsh()`; заменены все голые предикаты. Плюс: c2c больше не даёт ложную sshfs-плашку `both_remote` (взято из `_nb_remote_both`), Finish-сводка визарда c2c кажет обе стороны. 24 регресс-ассерта по 8 режимам, 0 fail. **Полный аудит c2c+все режимы (4 агента, 2026-07-21):** движок — (1) fold теперь АВТОРИТЕТЕН над remote/remote_path: смена стороны при флипе направления больше не тащит устаревший remote (был само-в-себя R→R при src→cloud из push-rclone, и push после c2c лил на бывший ИСТОЧНИК); (2) push mirror/archive с примонтированным-но-ПУСТЫМ источником теперь пропускается (guard пустого источника, как в pull — иначе первый прогон без --max-delete стирал приёмник) — обе ветки rclone и rsync; (3) skip-джоб сохраняет baseline `files` для --max-delete (setdefault в emit — раньше скип обнулял guard на след. прогон); (4) вложенный шаблон `_deleted/{year}/{month}` отключает авто-prune с нотисом (иначе `{year}` читался как 1 янв и сносил весь год); (5) Compare заглушён серверно для rclone (был битый `@::`); (6) сообщение guard'а по тексту max-delete, не по rc==9. UI — (7) c2c больше не показывает Separate+локальный пикер на облачном приёмнике; (8) Deleted-files для c2c кажет облачный путь и прячет GB; (9) overview верно зовёт SSH-приёмник pull-профиля «ssh», не «This NAS»; (10) «Open in Files» открывает локальный конец (push-rclone→источник) и прячется для c2c/SSH→SSH; (11) connOk/run-guard валидируют dst2.host (SSH→SSH) и host push-ssh/pull; (12) чек-лист/verify-нотис/мёртвый `ep` — по isRcloneAny/isC2C. Все 12 покрыты тестами (21 fold+7 guard, 0 fail).
**Аудит опций по транспортам (что где актуально), 2026-07-21:** delete_mode (Copy/Mirror/Archive)
задаётся ТОЛЬКО на вкладке «Deleted files» — дубль-селектор «Mode» из rclone-карточки назначения
УБРАН (был тот же `delete_mode` с другими лейблами). **Verify (rsync -c) СКРЫТ для rclone**
(rclone чек-суммит при передаче; `_nb_rclone_run` не верифицирует) — в tabConn и wizOpts.
**Compare-таб теперь ВО ВСЕХ режимах** (2026-07-21, обновлено): rsync — прежний itemize dry-run;
облако (rclone push/pull/c2c) — `rclone check` между сторонами профиля (драйвер
`_nb_compare_job_rclone` маппит `--combined`: `+`→new/missing, `-`→extra, `*`→changed, `=`→identical
на ту же форму `{summary,tree}`, existing tabCompare рендерит без переделки; deep=`--download`;
read-only). SSH→SSH (обе стороны remote) rsync не умеет — таб пишет «not supported in this mode»
(сервер отказывает через `_nb_remote_both`). Verify в Rclone-app остаётся как отдельный
local↔remote чек. **Speed limit** остаётся у профиля (rclone `--bwlimit`
для этого бэкапа) — тултип транспорто-специфичный; глобальный bwlimit в Rclone-app Options — только
restore/mount (transfers/checkers — тоже глобальные, `_rclone_perf_args`). **Overview/`nb_dest_state`
rclone-aware**: приёмник = «cloud remote» (не «not mounted»/«missing»), на тик в сеть не ходим. Визард:
шаг «Where to» пропускается для rclone (назначение = сам шаг Connection).
**Полный аудит движка (2026-07-21):** прогон nb_public/_nb_sides/_nb_dest_for/nb_dest_state/
nb_status/nb_build_cmd/_nb_rclone_cmd/nb_compare_cmd/nb_verify_cmd × 5 режимов (push local/ssh/
rclone, pull rsync/ssh) × delete_mode add/mirror/archive — 0 ошибок. e2e dry-run push через
rclone финализирует корректно (`running=False result=ok`). Найдено+исправлено: (1) **фолд сторон
не давал push-rclone→pull** (ветка `cd==rclone` имела приоритет) — теперь при смене стороны чиним по
той, что юзер ТОЛЬКО ЧТО поставил (dst=Cloud→push src local; src=remote→drop cloud dst→pull);
(2) три UI-гейта на `dest_base` блокировали rclone (у него dest_base пустой): добавление папок,
запуск, сводка визарда — теперь для rclone проверяют `remote`.
**Второй аудит (2 параллельных ревью-агента, 2026-07-21).** UI: (a) HIGH — `destCard()` ссылался
на несуществующий `push` → на push-SSH/SSH→SSH назначении падала вкладка «What to copy» и шаг
визарда (→ `isPush()`); (b) MED — переключение per-source профиля на Cloud оставляло `dest_mode:per`
→ локальный пикер на облачном назначении (→ `per` учитывает `!isRclone()`). Движок: (a) MED —
rclone-страж `--max-delete` брал `prev_files` из ПЕРЕДАННЫХ файлов (`transfers`), а не총 → после
инкремента страж схлопывался в 1 и блокировал любое удаление; теперь `files=checks+transfers`
(аналог rsync «Number of files»); (b) MED — `_nb_rclone_prune` парсил только `ModTime`, которого
на объектных хранилищах (S3/B2) у синтетических папок НЕТ → ретеншен не работал; теперь дата
берётся из ИМЕНИ снапшота (`_deleted/{date}`→YYYY-MM-DD), ModTime как fallback (проверено на
pcloud+SFTP); (c) LOW — per-job dir-excludes для rclone получают `/**` (контент папки),
push-ssh dest отклоняет `\n`, для rclone с verify в лог пишется note (rclone чек-суммит при
передаче). retention_gb уже скрыт в UI для rclone/ssh.
Приложение переименовано в **«Rclone»**; иконка — фирменный 3-цветный логотип (`RAW_LOGOS.rclone`
в `svg()`, свой viewBox с паддингом чтоб не был крупнее line-иконок). Save-бар вкладок закреплён
снизу (`setFootAdopt`, как в Настройках). Путь в remote выбирается пикером `rcloneRemotePicker`
(read-only браузер). **rclone-маунты видны в сайдбаре ФМ** (секция «Cloud», `renderRcloneMounts`,
появляется при маунте, исчезает при unmount; кросс-окно через `OPEN.__files._rcloneMounts`).
**ГРАБЛЯ (исправлена)**: read-only rclone/sshfs FUSE-маунт ложно поднимал алерт «Filesystem
read-only» — `_readonly_mounts()` теперь пропускает `fstype fuse*` и `/mnt/{rclone,remote}/`.
Плитки лончпада увеличены (иконки — фикс-размер SVG, масштаб через CSS `.lp-grid .tile>svg`).
Общие настройки rclone (конфиг/версия/remote'ы) — НЕ в сайдбаре профиля (там табы профиля), а
кнопкой «Cloud (rclone)» в шапке окна → оверлей `rcloneDlg` (редактор + Test у каждого remote +
Restore). **Restore из облака (pull)** — `rcloneRestoreDlg`: браузер remote'а (read-only `lsjson`
через `/api/backup/rclone/ls`), выбор папки + локального приёмника, `rclone copy` remote→local
(ТОЛЬКО copy, никогда sync/delete — облако не трогается, локально файлы только добавляются/обновляются;
приёмник обязан быть под /mnt|/media|/srv|/home). Свой транзиент-юнит `nas-rclone-restore` +
state/log в `/var/lib/nas-os/rclone-restore.*`, драйвер `_rclone_restore_cli` (CLI `rclone-restore`,
пути через env `RCR_*`), прогресс тем же `_rclone_progress_line`; dry-run превью. API
`/api/backup/rclone/restore/{start,status,cancel}`),
обновления apt из UI, авто-ремоунт/термозащита/сводка/усиленный USB-импорт, английский UI
(рантайм-i18n снят, см. правило 3), адаптив (планшет+телефон).
**Backup: визард первого запуска (2026-07-20)**: свежий профиль (нет связи И папок, и не было
`setup_done`) вместо левых табов получает пошаговый мастер `renderWizard` — МЕЛКИЕ шаги,
одно решение на экран: Connection → [pull: What to copy → Where to | push: Where to →
What to copy — push обязан выбрать базу ДО папок, dest джобов выводится из неё] →
Exclusions → Deleted files → Schedule → Finish (сводка); порядок считается в `WSTEPS()`
по направлению на каждый рендер. Шаги folders/dest/excl — ЧАСТИЧНЫЕ рендеры
`tabSources(tc, part)` (полный таб = те же секции подряд; вся обвязка под null-guard —
фикс формы чинит и таб, и визард). Лента `.nbwiz-step`, Back/Next/«Set up later»;
панели автосохраняют — назад/вперёд ничего не теряет; Finish/Skip пишут `setup_done:true`
(nb_save, хранится в профиле) → обычный вид. **ГРАБЛЯ (исправлена)**: решение «показывать ли
визард» НЕЛЬЗЯ пересчитывать на каждом render — как только связь+папки готовы, Next выкидывал
в табы, будто мастер завершён. Решение по флагам в профиле: «настроен» = ТОЛЬКО после Finish/Skip (`setup_done`), а не
«поля заполнились» — при первом показе визарда пишется `setup_started`, и пока нет
`setup_done`, визард возвращается (переключение профилей, перезагрузка страницы — резюм
с того же шага, позиция в `win._wizPos` на сессию). До-визардные профили (настроены, оба
флага пусты) при первом render тихо получают `setup_done`. `wizOn` — сессионный кэш решения,
сброс при смене NB_PID (`win._nbPid`).
Шаг Connection в визарде — `tabConn(tc,"sides")` (только карточки сторон + Check);
verify + speed limit — предпоследний шаг «Options» (`wizOpts`). `.nb-chk input` зажат
15px (глобальный `input{width:100%}` растягивал чекбокс su_src). Направление отдельно не спрашивается — его задаёт карточка
сторон src/dst в Connection (сервер выводит direction сам). Верхние плашки `.ovh-row`
переделаны: метка над значением, значение переносится (`word-break`) — однострочный flex
обрезал «ssh root@…» до бесполезности; `.ovh-dot` абсолютный ТОЛЬКО внутри `.ovh-row`
(он же используется инлайн в history/folder-строках). `.nb-sum-i` — flex-wrap.
tabConnPush — мёртвый код старой модели (не вызывается), не удалён.
**Анализатор места** (DaisyDisk-стиль, `winDiskUsage`): фоновый скан тома в пределах одной ФС
(`du -x` по `st_dev`; в mergerfs-пуле `st_dev` консистентен → работает и для пула/системы/USB), кэш
в `/var/lib/nas-os/duscan-*.json` + ленивая отдача узлов (`/api/fs/duscan/{status,node,start,cancel}`),
sunburst 2 кольца с drill-down/подписями/подсветкой круг↔бары, возраст скана цветом.
**ГРАБЛЯ:** живой киоск НЕ подхватывает правки `web/screen.html` — страница загружена один раз
при старте; после правки `sudo systemctl restart nas-screen` (сервер-то отдаёт свежий файл сразу,
и это обманчиво: `curl` показывает новое, а на стене висит старое). Часы при простое (`screen.json:clock_min`, крупное время + основные показатели,
дрейф от выгорания; тап убирает). Действия: запуск бэкапа и безопасное извлечение USB
(двойной тап «Извлечь» → «Точно извлечь?», взвод живёт в JS-переменной — лист перерисовывается
каждый опрос). Гашение подсветки — `bl_power=4` (brightness=0 у этой панели НЕ гасит подсветку
до конца). Пробуждение: пока `dark:true`, клиент держит невидимый щит — первый тап только будит,
не нажимая плашку под пальцем (грейс 4 с от стухшего ответа опроса). Страница масштабируемая:
логическая сцена 800×480, `fit()` скейлит body под любой вьюпорт (телефон/планшет/другая панель),
раскладка не меняется; не-loopback получает класс `.remote` (курсор возвращается). `?page=N` —
открыть сразу N-ю страницу (нужно для скриншотов: свайп в headless не сделать). Кнопки
сна и питания в шапке (питание → две плитки на пол-экрана). Рендерер — `cage` + `chromium --kiosk`
(`nas-screen.service`, tty1), ставит `install_screen()` в визарде ТОЛЬКО если панель реально
подключена (иначе headless-бокс тянул бы 600 МБ хрома). `/screen` и `/api/screen/*` — до auth-гейта,
но ТОЛЬКО с loopback (`_local()`): на экране, до которого дотягиваешься рукой, пароль не спрашиваем,
из локалки те же пути требуют логина. Яркость/ночной режим/интервал опроса — `screen.json` +
`_screen_tick()` в monitor_loop (тревога `health=bad` зажигает экран даже ночью; касание будит
через `POST /api/screen/act {a:"touch"}`). Настройки → вкладка «Тачскрин».
**Заметки** (`winNotes` + модуль notes в nas-web.py): обычные .md с frontmatter в
`notes_root()` (дефолт `/mnt/storage/notes`, конфиг `/var/lib/nas-os/notes.json`, миграция
`/api/notes/root`); `_assets/` для картинок, `.trash/` для удалённых. Редактор — vendored
Toast UI (web/tui-editor*.js/css, MIT, офлайн — НЕ обновлять с CDN бездумно) + плагин
подсветки кода (web/tui-hl.*). **Два типа заметки живут рядом**: markdown (`.md`,
Toast UI) и полный HTML (`.html`, редактор = исходник CodeMirror `xml`/htmlMode +
изолированное превью в `<iframe sandbox="allow-scripts">` без same-origin — скрипты заметки
работают, но не видят сессию/API панели). Тип определяется расширением (`_note_kind`);
`note_get`/`notes_tree` отдают `kind`, фронт ветвит редактор по нему (`NT_KIND`,
`ntGetContent`/`ntHasEditor`). У HTML frontmatter не `---`, а ведущий комментарий
`<!--nas-note … -->` (иначе файл не открылся бы как страница) — `_note_parse` понимает оба.
Кнопка «+ Note» → меню Markdown/HTML; в списке/дереве html-заметки помечены бейджем `HTML`.
Тело HTML пишется на диск ДОСЛОВНО (включая `<script>`) — санитайзинг только у markdown
(Toast UI), у HTML изоляция обеспечивается sandbox-iframe. Клик по картинке — меню размеров (ширина хранится тегом
`<img width>` в markdown), дабл-клик — лайтбокс. Таб настроек «Заметки» (`notesTab`).
Сохранение — оптимистическая блокировка по mtime (`base_mtime`; конфликт → диалог
перезаписать/открыть свежую; unload-флаш через `fetch keepalive` с `conflict_copy` —
сервер паркует текст соседним файлом «(конфликт …)»). Корзина хранит происхождение
(`.trash/<день>/.origins.json`) — «Вернуть» кладёт на исходное место. `notes_gc()` в
`maintenance_daily`: срок корзины `maintenance.json:notes_trash_days`, сиротская
`.history` (заметки нет нигде), несвязанные `_assets` (имени нет ни в одном .md,
файл старше 7 дней).
**SSH-серверы в ФМ**: секция «Серверы» в сайдбаре — sshfs-маунты в `/mnt/remote/<id>`
(`/var/lib/nas-os/remotes.json` с паролями → секретная секция бэкапа; opts
reconnect+ServerAlive, allow_other; пакет sshfs в UTIL_PACKAGES визарда).
Смонтированный сервер = обычная папка, все операции ФМ работают как есть.
**Restore-drill (проверка восстановимости бэкапа, 2026-07-21/22).** Бэкап, который записан, но
не доказано восстановим, — это надежда, а не бэкап. Drill тянет НЕСКОЛЬКО случайных файлов
ОБРАТНО из приёмника и проверяет их. Движок в nas-web.py: `nb_drill_start/status/cancel`,
CLI `backup-drill <pid>`, свой транзиент-юнит `nas-backup-drill[-<pid>]`, стейт
`nas-backup-drill-<pid>.json`. READ-ONLY (пишет только временное под `/var/lib/nas-os/restore-drill/`).
`_nb_drill_local` — local-приёмник (sha256 при вычитывании), `_nb_drill_rclone` — облако
(`rclone copyto`, rclone сам чек-суммит). **V2 — сверка с ИСТОЧНИКОМ (ловит тихий bit-rot на
приёмнике):** после восстановления файла перечитывает оригинал через `_nb_src_meta` (local=hashlib;
ssh=тянет один файл `rsync --protect-args` тем же транспортом, что бэкап, вкл. `sudo rsync`) и
сравнивает size+mtime+sha256. Правка ПОСЛЕ бэкапа (size/mtime разошлись) → пропуск (не тревога);
источник недоступен → «не проверено»; совпали size+mtime, но хеш разный → **bit-rot** (отдельный
провал + уведомление «Backup bit-rot detected», подсказка про e2fsck). **Авто-прогон:** после
каждого чистого бэкапа (`nb_run` финализация) + **расписание-флор** `_nb_drill_sched_tick` в
monitor_loop (перегнать, если последний drill старше `drill_every` дней). Один чип на карточке
Overview (`.nb-drill`, иконка-щит красится по состоянию) = 4 уровня Off/After backups/Weekly/Monthly
→ (`drill_auto`,`drill_every`); API `/api/backup/drill/{start,status,cancel,sched}`.
**«What changed» — пофайловый журнал изменений на прогон (2026-07-22).** Не для восстановления, для
понимания «что делают файлы / их цикл». Бэкап-rsync несёт `--out-format='%i %n'` (строка только на
изменённый/удалённый файл, не на каждый → ограничено числом изменений). Раннер классифицирует
(`_nb_itemize`: added `>f+++`, changed `>f.st`, deleted `*deleting`), держит ИХ ВНЕ человеческого
лога, кап `NB_CHG_CAP`=400 путей/категорию/задача. Детальные списки → отдельный
`nas-backup-changes-<pid>.json` (последние 30 прогонов), в history.json только счётчики
added/changed. `GET /api/backup/changes {p,ts}` — ленивая отдача. History-вкладка: у задачи
цветной бейдж `+A ~C −D`, клик разворачивает списки (25/секция + «show all», свой скролл).
Только для rsync-профилей (rclone — другой движок).
**Аудит №2 (2026-07-22, все фичи):** 23 юнит-проверки (все ветки диагноза вкл. комбинированную,
rollover с диагнозом, fstab-редактор на песочном файле, все guards drill_fix) + e2e: реальный
running-but-disabled юнит → дрель флагает с fixa → drill_fix включает → re-audit чист; write_load
симулирован на подменённых путях состояния с перехватом notify_event. Всё зелёное.
Тестировано живьём: дрель ловит ручной tmpfs-маунт (score 93→100 после umount), confgit коммитит
1852 файла и молчит без изменений, sentry учится, black box пишет и переживает рестарт панели.
**ГРАБЛЯ «создал папку — пикер закрылся и выбрал РОДИТЕЛЯ» (2026-07-24, обе версии пикера).**
Глобальный scrim-хендлер (Enter → `.pri`-кнопка верхнего диалога) срабатывал и в поле имени новой
папки: собственный `onkeydown` создавал папку, а следом Enter жал «Choose this folder» — окно
закрывалось с ещё СТАРЫМ `path`. Лечение: `data-noenter` на `#pfMkName` в ОБОИХ пикерах
(`pickFolder` и `rcloneRemotePicker`) и на `#pdPath` (пикер приёмника Mirror — там Enter должен
переходить в папку, а не подтверждать). ПРАВИЛО: любое поле в диалоге, у которого Enter имеет
СВОЙ смысл (поиск, навигация, создание), обязано нести `data-noenter` — иначе оно молча делает
ДВА действия. Плюс `chrome()` теперь зовётся ДО сетевого `ls`: облачный листинг идёт секунды, и
подвал всё это время показывал старую папку — после «создать» это читалось как «не сработало».
(3) **Удаление задания чистит его историю** (`_kp_forget_runs`): строки этого бэкапа из
`kopia-history.json` (под тем же flock, что и запись), `kopia-run-<id>.{json,log,cancel}`, слот в
очереди `pending`; удаление БЕГУЩЕГО задания отклоняется («stop it first»), UI показывает причину.
`kp_dest_forget` симметрично убирает `kopia-{maint,verify}-<id>.json` и ключи `present/maint/verify`
(иначе новый приёмник с переиспользованным id унаследовал бы чужие штампы обслуживания).
Плюс **`_kp_gc()` в `maintenance_daily`** — подметает сирот, оставшихся от старых версий,
ручной правки конфига или восстановления бэкапа настроек: история/файлы/ключи состояния
несуществующих сущностей + брошенные `kopia-drill.<pid>` старше суток. Guard: если `kopia.json`
НЕТ на диске — GC не трогает ничего (иначе транзиентная потеря конфига стёрла бы всю историю).
Одноразово подмёл текущий бокс: 21 строка истории, 14 файлов, 12 ключей состояния от давно
удалённых тестовых заданий. Проверено: 10 юнит-ассертов (чужая история цела, повторный GC
идемпотентен) + CDP-прогон 24/24 (группировки, бейджи, пунктир, «back it up →» с преселектом,
пикер поверх диалога, гварды).
**Обход всех служб по логам (2026-07-24) — три находки, две исправлены.**
Аварий нет: `systemctl --failed` пуст, ни одного warning/error у nas-web/nas-syncthing/nas-screen/
nas-netguard/nas-blackbox/smbd/docker/avahi, 4 контейнера живы, FUSE-маунты (pcloud, sshfs Ugreen,
T7) отвечают на statvfs, T7 SMART PASSED, бэкапы ok, apt пуст.
- **ГРАБЛЯ (исправлена): часы при загрузке уезжали на 10 суток назад.** chronyd штатно шагал время
  на 874407 с через ~18 с после старта; `journalctl --list-boots` показывал ВСЕ загрузки как
  «13 июля 13:33», `who -b` врал, vnstat ругался «database update is in the future». Причина: RTC
  нет, `systemd-timesyncd` УДАЛЁН (время даёт chrony), `fake-hwclock` не стоял, а осиротевший файл
  `/var/lib/systemd/timesync/clock` с mtime 13 июля работал «полом» времени при старте. Всё, что
  пишет метки в эти 18 секунд (журнал доступности, black box, планировщики), получало время из
  прошлого — ровно тот механизм, что однажды покрасил полосу доступности целиком в красный.
  Лечение: `install_fake_hwclock` в АВТО-БАЗЕ (пакет + `fake-hwclock save` + таймер + touch
  осиротевшего файла). ПРАВИЛО: на боксе без RTC проверять не `who -b`/`--list-boots`, а
  `/proc/uptime` — они расходятся ровно на величину скачка.
- **ГРАБЛЯ, которую принёс сам fake-hwclock (2026-07-30, исправлена): красный
  `fake-hwclock-load.service` сразу после ребута pi4.** `systemctl status` показывал
  `failed (Result: start-limit-hit)` при `Main PID: … status=0/SUCCESS` — то есть НИ ОДИН прогон не
  падал. В журнале загрузки пять пар «Starting… / Finished…» за 20 мс подряд, шестой старт упёрся в
  штатный рейт-лимит systemd (`StartLimitBurst=5` за 10 с) и пометил юнит упавшим. Причина в
  дебиановском юните: `Type=oneshot` БЕЗ `RemainAfterExit`, а `WantedBy=sysinit.target` — отработав,
  юнит возвращается в `inactive`, и каждый следующий раз, когда в раннем старте кто-то снова тянет
  `sysinit.target` (устройства/маунты/cloud-init от rpi-imager — в cmdline `ds=nocloud`), systemd
  честно запускает его заново. Часы при этом восстанавливаются правильно — ломается только ЦВЕТ.
  Воспроведено руками на живом боксе: `fake-hwclock save` (чтобы `load` был no-op и не тянул часы
  назад), `reset-failed`, семь `systemctl start` подряд → те же `start-limit-hit` на шестом.
  Лечение — дроп-ин `RemainAfterExit=yes` (`/etc/systemd/system/fake-hwclock-load.service.d/
  nas-remain.conf`, пишет `install_fake_hwclock`): после идемпотентного восстановления часов юнит
  остаётся `active (exited)`, и повторные притяжки становятся no-op. Тот же семь-стартов-подряд
  после фикса: `active`, `Result=success`, `systemctl --failed` пуст.
- **Исправлено: ложная тревога «NAS backup: never run yet»** повторялась каждые несколько часов для
  профиля с ВЫКЛЮЧЕННЫМ расписанием, который просто ни разу не запускали. Профиль без расписания —
  ручной (или черновик), «ни разу не запускался» это выбор владельца, а не авария. Теперь
  предупреждение гейтится `schedule.enabled`; ветка «not updated for a long time» не тронута —
  профиль, который РАНЬШЕ ходил, а потом перестал, стоит того, чтобы о нём сказать.
- **Диагноз без правки: 4215 заблокированных ufw соединений `192.168.1.95 → pi4:45876`**, ровно раз
  в минуту с момента загрузки. **45876 — дефолтный порт агента Beszel** (henrygd/beszel, мониторинг
  хаб+агенты; в /etc/services его нет, это выбор проекта). На Ugreen крутятся `beszel` (хаб, UI на
  :8590) и `beszel-agent`; хаб опрашивает pi4 как зарегистрированную систему, а агента на pi4 нет —
  ufw молча роняет SYN. Решение за пользователем: убрать pi4 из хаба ИЛИ поставить агента и открыть
  порт. Диагноз получен по SSH с самого Ugreen (read-only), учётка — из профиля бэкапа.
- **Оставлено как есть: `edt_ft5x06 10-0038: Unable to fetch data, error: -5`**, 200–440 записей в
  час, фоном, даже когда экрана не касаются. На шине i2c-10 два устройства: тач `0x38` (edt-ft5506)
  и `0x45` (`rpi_touchscreen_attiny`, он же backlight официальной 7" панели) — известный конфликт
  драйверов на общей шине. НЕ наша вина: `_screen_apply` пишет яркость только при ИЗМЕНЕНИИ, а не
  каждый тик (проверено). Тач работает, киоск жив, журнал от этого прибавляет 0.2 МБ за загрузку —
  износу карты не грозит. Лезть в загрузочный оверлей ради косметики не стал.

**ГЛАВНАЯ ГРАБЛЯ ПЛАНИРОВЩИКОВ: пропущенный слот стоил ЦЕЛОГО ДНЯ бэкапа, молча (2026-07-26).**
Пользователь заметил сам: ежедневный прогон в 15:00 вчера был, сегодня нет, на часах 21:00.
Диагноз по журналу: `_nb_sched_tick` сравнивал `s["time"]` с ТЕКУЩЕЙ минутой (`%H:%M`), то есть
слот существовал ровно 60 секунд и только в памяти живого процесса. А `monitor_loop` начинается с
`_mon_wake.wait(60)` — свежий процесс МОЛЧИТ первую минуту, и лишь потом идёт по списку тиков
(`history_sample`, `monitor_tick`, `maintenance_daily`, `_smart_selftest_tick` — и только затем
планировщик). В тот день я сам правил панель и рестартовал её каждые 40-60 секунд подряд, в том
числе в 14:59:59 → 15:00:58 → 15:01:39: ни один процесс не дожил внутри минуты «15:00» до
планировщика. Никакого «догнать» не было, уведомления «прогон пропущен» тоже — расписание просто
теряло сутки. Ровно то же дал бы ребут, обновление или падение в неудачную минуту.
Лечение — `_nb_sched_last_due(cfg, now)` + ПЕРСИСТЕНТНЫЙ слот (`nas-backup-sched.json`,
`{профиль: "YYYY-MM-DD HH:MM"}`): тик спрашивает не «сейчас ровно 15:00?», а «когда этот профиль
БЫЛ должен последний раз и отработали ли мы тот слот». Пропущенный слот догоняется, если следующий
запуск ещё не ближе пропущенного (окно 12 ч для daily, 24 ч для weekly), и НЕ догоняется, если с
тех пор уже был реальный прогон (`_nb_last_real_run` — история + run-state, сухие прогоны не
считаются) или если профиль СОХРАНЁН позже слота (расписание, поставленное в 20:00, не обязано
задним числом отрабатывать 15:00). Персистентность заодно чинит противоположный риск: рестарт
ВНУТРИ той же минуты больше не может запустить прогон дважды. Дни перебираются через `localtime`,
а не вычитанием 86400 из штампа — на переводе часов это разошлось бы на час.
Плюс СОБЫТИЕ `nb_missed` (каталог монитора, prio 1, кулдаун 6 ч): слот, который пропущен И уже не
догоняется, сообщается В ТОТ ЖЕ ДЕНЬ. Раньше единственным сторожем был `nb_stale` с порогом в
СЕМЬ ДНЕЙ — ежедневный бэкап мог молча пропасть на неделю, и именно так сегодняшний пропуск нашёл
не бокс, а пользователь. ПРАВИЛО: у периодической задачи порог тревоги обязан быть соизмерим с её
ПЕРИОДОМ, а не выбран «на глаз»; для ежедневной задачи неделя тишины — это не порог, это отсутствие
контроля.
ПРАВИЛО для любого расписания в этом проекте: слот — это ИНТЕРВАЛ «должен был к моменту X», а не
минута-совпадение; и он обязан лежать на диске. **2026-07-27: та же дыра нашлась ещё в ДВУХ
планировщиках** (проверка «а нет ли этого в остальных бэкапах»): `_kopia_tick` сравнивал
`s["time"] != hhmm` (персистентный `slot` спасал только от ДВОЙНОГО запуска, не от пропуска),
`_imsb_tick` — `slot.endswith(cfg["time"])` с маркером В ПАМЯТИ. Обе переведены на общий
`sched_last_due(now, "HH:MM", wday=None)` (0=Mon; дни перебираются через localtime — DST) с
персистентным «отработанным слотом»: kopia — `kopia-state.json:done[bid]`, резерв Immich —
`immich-standby.json:last_slot`. Окно догона 12 ч (weekly 24 ч), просроченное — событие
`nb_missed`. Проверено 14 ассертами + живьём: после рестарта ни один прогон не запустился зря
(все слоты уже отработаны), маркеры записались.

**Overview пересобран в панель (2026-07-20, вторая итерация)**: заголовок = ИМЯ ПРОФИЛЯ
(+ карандаш → renameDlg; хардкода «Main NAS backup» больше нет) + бейдж направления
`.nb-dirbadge`; кнопки Dry/Run/Stop — В HERO сверху (главные действия — наверху);
блок `.nb-flow` «From — the data lives here → To — the copies land here» (статус приёмника
`oRecvDot/oRecvV` живёт в карточке To); ниже три плашки (Folders/Schedule+next/Deleted);
oProg сразу под ними. Формулировки против путаницы source/destination: вкладка Sources
переименована в «What to copy» и читается В ПОРЯДКЕ ПОТОКА ДАННЫХ — сабтайтл
«Step 1… Step 2…», секция «Folders» (+Pick) ПЕРВОЙ, затем секция-заголовок
«Where the copies land» с destCard (внутри карточки просто «Destination» — смысл несёт
заголовок секции, дублей «What to copy» больше нет), «a folder on THIS NAS».

## Гард массовых изменений — защита от шифровальщика (2026-08-05)

Дыра, которую закрывает: у удалений гард был с первого дня (`--max-delete` от базлайна),
но шифровальщик НЕ удаляет — он ПЕРЕПИСЫВАЕТ каждый файл, и зеркало послушно заменяло бы
копию шифрованным мусором (archive-режим держит старые версии в `_deleted`, но ретеншен их
доедает). Три слоя:
1. **Зеркала (rsync-путь)**: перед настоящим прогоном — `--dry-run --stats` тем же argv
   (без `--out-format`/`--info`); `xfer > _nb_change_limit(prev, cfg)` → задача отклонена
   С НЕТРОНУТОЙ копией, `code 25, block:"masschange"`. Порог `change_guard_pct`
   (профиль, умолчание 50%, пол `NB_CHANGE_MIN=200` — на мелких базлайнах % это шум),
   строка в настройках рядом с гардом удалений. Разовый обход — тот же общий
   `allow_delete` («Run anyway»), баннер получил отдельный блок с формулировкой про
   малварь и свой пункт в confirm-диалоге. Событие `nb_change` — в `_PUSH_NOW` (звонит).
   Проба пропускается без базлайна (первый прогон — сравнивать не с чем) и на dry-run.
2. **SnapRAID-обёртка**: `snapraid diff` теперь смотрит и на `updated` (порог
   `UPDATE_THRESHOLD=2000`, настраивается в notify.conf, 0=выкл). Пока sync отклонён,
   `snapraid fix` ещё может откатить файлы к последнему чистому состоянию — sync
   по шифрованному уничтожил бы эту возможность. Обёртка добавлена в `check.sh gen`.
   На боксах, где snapraid уже стоит, после пула — `api snapraid` (перегенерация).
3. Kopia защищена своей природой (версии+ретеншен) — сигнал там не критичен.

**Известная дыра**: rclone-путь зеркал гарда изменений пока НЕ имеет (у удалений — имеет);
у rclone нет дешёвого эквивалента dry-run-статистики одним прогоном. В бэклог.

Проверено живым e2e: 300 файлов → базлайн; «шифрование» 250 → отказ (250>200), в копии
«healthy content»; с разрешением → прошло. Плюс 3 юнит-теста на арифметику лимита и парсер
`--stats` (запятые в числах).
