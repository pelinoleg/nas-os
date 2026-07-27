# Backup reliability changes — handoff for independent review

Date: 2026-07-27

Purpose: make scheduled Mirror/Kopia starts and Kopia post-snapshot restore verification report
the truth more reliably. This change deliberately does **not** alter rsync/rclone/Kopia copy
commands, source/destination mapping, retention, deletion, archive paths, repository policies,
or the web UI.

## Initial findings

Two silent-failure paths were identified during a read-only audit:

1. Mirror and Kopia schedule ticks persisted a due slot before knowing whether the transient
   systemd unit had started. A synchronous launch failure could therefore consume the slot.
2. `_kp_drill()` ignored a failed `kopia restore`. If all sampled restores failed, it returned
   zero checked files and zero mismatches; `_kp_snap_cli()` only reacted to mismatches and could
   leave the overall snapshot result as `ok`.

## Files changed

### `nas-web.py`

Mirror scheduler:

- `_nb_sched_tick()` calls `nb_run_bg()` before persisting the slot.
- `ok` (including a safely persisted queue entry) marks the slot.
- A failed start leaves the slot due, emits a cooldown-controlled `nb_missed` event, and is retried
  during the existing catch-up window.

Kopia scheduler:

- `_kopia_tick()` marks `done[backup_id]` only after `kp_run_start()` succeeds.
- `busy` retains the existing pending queue behavior.
- Other synchronous failures remain due and emit a cooldown-controlled event.
- A backup already in `pending` is skipped by the primary schedule loop, so only the pending loop
  retries it. This prevents duplicate attempts and prevents the original six-hour queue timestamp
  from being refreshed forever.
- When a pending start succeeds, the corresponding due label is persisted before the queue item is
  removed.

Kopia restore drill:

- `_kp_drill()` now returns `attempted`, `checked`, `ok`, `failed`, `failures`, and `rot`.
- A failed restore is logged and counted instead of silently skipped.
- `_kp_snap_cli()` records the drill phase when an attempt occurred.
- Any restore failure, mismatch, or zero verified sample downgrades an otherwise successful snapshot
  from `ok` to `warn`. Snapshot creation remains represented accurately as successful; the warning
  says that restore verification was incomplete or failed.

### `tests/test_backup_reliability.py`

New stdlib-only regression tests cover:

- failed Kopia sample restore is counted and logged;
- failed scheduled Mirror start does not consume the slot;
- successful scheduled Mirror start consumes the slot;
- failed scheduled Kopia start does not consume the slot;
- an existing Kopia pending item is attempted only once per tick and preserves its timestamp.

The tests import `nas-web.py` without starting the server and mock all external/system operations.
They do not access backup destinations or execute rsync, rclone, Kopia, or systemd-run.

### `check.sh` and `.gitignore`

- `test` was added to the default check groups.
- `python3 -m unittest discover -s tests` is now part of `./check.sh`.
- The top-level allow-list now tracks `tests/`.

### Documentation

- `CLAUDE.md` links this handoff document from its documentation index.
- The existing local `docs/backup-mirror.md` and `docs/kopia.md` were intentionally left unchanged:
  the repository's top-level `.gitignore` currently excludes the whole `docs/` directory. This one
  requested handoff file is force-tracked without pulling all local documentation into the change.

## Verification performed

Run from `/opt/nas-os`:

```bash
python3 -m unittest discover -s tests -v
./check.sh py test sh gen css i18n git
./check.sh js
python3 -m py_compile nas-web.py
bash -n nas-wizard.sh install.sh check.sh
git diff --check
```

Expected regression result: five tests pass. Static checks and the Docker-backed JavaScript checks
should report `ALL CLEAN`; before commit, the git group only notes the intentional changes.

## Suggested reviewer focus

1. Confirm that `nb_run_bg(...)->{ok:true, queued:true}` should consume the Mirror slot. The queue is
   persisted and drained elsewhere, so this is intentional.
2. Confirm Kopia pending entries cannot be processed by both loops in one tick.
3. Confirm a failed Kopia restore must be a warning, not a snapshot error.
4. Consider whether repeated non-busy start attempts once per minute during the catch-up window are
   acceptable. Notifications are cooldown-limited; source/destination preflight is cheap and no copy
   unit starts on those failures.
5. Confirm backward compatibility of the expanded drill result. Existing consumers use `.get()` and
   the old keys `checked`, `ok`, and `rot` remain present.

## Known non-goals and remaining observations

- This does not add a full-byte verification of rclone Mirror destinations; the existing cloud drill
  proves readability, while `rclone check` remains the independent identity check.
- Kopia's regular drill remains a sample of up to three files, and monthly repository verification
  remains 1%; this patch only makes a failed or absent sample honest.
- No large-file split or frontend refactor is included.
- No live backup was started, stopped, deleted, or restored while making this change.
