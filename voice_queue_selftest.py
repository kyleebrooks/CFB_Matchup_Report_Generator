"""Exercise the voice-job queue against the real database.

Run this on the droplet after deploying. It uses MySQL-specific behaviour that cannot be
faked convincingly on SQLite — `UPDATE … ORDER BY … LIMIT 1` for the atomic claim, and
`ON DUPLICATE KEY UPDATE` for heartbeats — so this is the only place the queue is
genuinely proven.

    ./venv/bin/python voice_queue_selftest.py

It creates jobs for a throwaway account id, checks them, and deletes everything it made.
Nothing belonging to a real account is read or touched.
"""

import sys
import threading
import time
from datetime import datetime, timedelta

# The service gets DB_PASSWORD from /etc/afplna.env via systemd; an interactive shell
# gets none of it, and the failure surfaces as a confusing "Access denied ... (using
# password: NO)". Bootstrap first and, critically, *before* importing anything that
# reads config — config.py resolves the database settings at import time, so a later
# load would be too late to matter.
import envfile            # noqa: E402
ENV_REPORT = envfile.bootstrap()

import db                 # noqa: E402
import voice_jobs         # noqa: E402

# Far outside the real id space; every row this script writes carries it, and the
# cleanup at the end deletes strictly by this id.
TEST_ACCOUNT = -9_999

_failures: list[str] = []


def check(label: str, condition: bool, detail: str = '') -> None:
    if condition:
        print(f'  ok    {label}')
    else:
        _failures.append(label)
        print(f'  FAIL  {label}' + (f'\n        {detail}' if detail else ''))


def cleanup() -> None:
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f'DELETE FROM {voice_jobs.TABLE} WHERE account_id=%s',
                        (TEST_ACCOUNT,))
            cur.execute(f'DELETE FROM {voice_jobs.WORKERS_TABLE} WHERE account_id=%s',
                        (TEST_ACCOUNT,))
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    print('Voice queue self-test\n')

    if ENV_REPORT.get('missing'):
        # Say exactly what is missing and how to supply it, rather than letting the
        # first query fail with a database error that names none of that.
        for line in envfile.guidance(ENV_REPORT):
            print(line)
        return 2
    if ENV_REPORT.get('source') and ENV_REPORT['source'] != 'environment':
        print(f"environment loaded from {ENV_REPORT['source']}\n")

    try:
        voice_jobs.ensure_schema()
    except Exception as e:
        print(f'  FAIL  could not reach the database: {e}')
        print('\nThe tables are created on demand, so this is a connection problem,')
        print('not a schema one. Check DB_HOST/DB_USER/DB_PASSWORD in /etc/afplna.env.')
        return 1
    cleanup()

    # -- schema -------------------------------------------------------------
    print('schema')
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute('SHOW TABLES LIKE %s', (voice_jobs.TABLE,))
            check('voice_jobs table exists', cur.fetchone() is not None)
            cur.execute('SHOW TABLES LIKE %s', (voice_jobs.WORKERS_TABLE,))
            check('voice_workers table exists', cur.fetchone() is not None)

            # Reported, not asserted. The queue never reads the database's clock — it
            # writes and compares datetime.now() from this host throughout — so a
            # difference here is harmless. It is printed because it is precisely the
            # thing that makes a lease or heartbeat look wrong to a human reading rows
            # by hand, and because a query using NOW() would quietly inherit it.
            cur.execute('SELECT NOW()')
            db_now = cur.fetchone()[0]
            skew = (db_now - datetime.now()).total_seconds()
            note = 'in step' if abs(skew) < 60 else f'{skew / 3600:+.1f} h from this host'
            print(f'  note  database clock reads {db_now} ({note})')
    finally:
        conn.close()

    # -- enqueue ------------------------------------------------------------
    print('\nenqueue')
    job = voice_jobs.enqueue(
        account_id=TEST_ACCOUNT, title='Self Test Episode',
        script='Speaker 1: Testing.\nSpeaker 2: Testing back.',
        automation='cfb_reports_podcast', vibevoice_model='vibevoice/VibeVoice-7B',
        preset='podcast', speakers={'1': 'Johnny_Vibe', '2': 'Ed_Clean_Vibe'})
    check('job created', bool(job.get('id')))
    check('state is queued', job['state'] == voice_jobs.QUEUED, str(job.get('state')))
    check('speakers round-trip as a dict',
          job['speakers'] == {'1': 'Johnny_Vibe', '2': 'Ed_Clean_Vibe'},
          repr(job.get('speakers')))
    check('status view omits the script body', 'script' not in job)
    job_id = job['id']

    long_script = 'Speaker 1: x\n' * 20
    voice_jobs.enqueue(account_id=TEST_ACCOUNT, title='Second', script=long_script)

    try:
        voice_jobs.enqueue(account_id=TEST_ACCOUNT, title='Empty', script='   ')
        check('empty script rejected', False, 'no error raised')
    except voice_jobs.VoiceJobError:
        check('empty script rejected', True)

    # -- the important one: two workers, one job ----------------------------
    print('\nconcurrent claim')
    claims: list = []
    barrier = threading.Barrier(8)

    def race(n: int):
        barrier.wait()                       # all eight hit the UPDATE together
        try:
            claims.append(voice_jobs.claim_next(f'selftest-worker-{n}'))
        except Exception as e:                # noqa: BLE001 — reported, not swallowed
            claims.append(e)

    threads = [threading.Thread(target=race, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    errors = [c for c in claims if isinstance(c, Exception)]
    got = [c for c in claims if isinstance(c, dict)]
    check('no claim raised', not errors, '; '.join(str(e)[:120] for e in errors))
    check('exactly two jobs handed out (two were queued)', len(got) == 2,
          f'{len(got)} claims returned')
    check('no job handed to two workers',
          len({g['id'] for g in got}) == len(got),
          f'ids: {[g["id"] for g in got]}')
    check('claimed jobs carry the script', all(g.get('script') for g in got))
    check('claim marks the job running',
          all(g['state'] == voice_jobs.RUNNING for g in got))

    mine = next((g for g in got if g['id'] == job_id), None)
    check('the first job was one of them', mine is not None)
    if mine is None:
        cleanup()
        return 1
    worker = mine['worker_id']

    check('nothing left to claim', voice_jobs.claim_next('selftest-late') is None)

    # -- progress -----------------------------------------------------------
    print('\nprogress')
    updated = voice_jobs.progress(job_id, worker_id=worker, stage='Rendering', percent=42)
    check('stage recorded', updated['stage'] == 'Rendering', updated.get('stage'))
    check('percent recorded', updated['percent'] == 42, str(updated.get('percent')))
    clamped = voice_jobs.progress(job_id, worker_id=worker, percent=500)
    check('percent clamped to 100', clamped['percent'] == 100,
          str(clamped.get('percent')))

    held = voice_jobs.claimed_by(job_id, worker)
    check('claimed_by returns the script', bool(held.get('script')))
    try:
        voice_jobs.claimed_by(job_id, 'someone-else')
        check('another worker cannot read the claim', False, 'no error raised')
    except voice_jobs.VoiceJobError as e:
        check('another worker cannot read the claim', e.status == 409, f'status {e.status}')

    # -- lease expiry -------------------------------------------------------
    # Expire the lease with a PYTHON datetime, not MySQL's NOW(). Every timestamp the
    # queue writes and every comparison it makes uses datetime.now() on this host, so
    # the two clocks never meet in production — but a test that reached for NOW() here
    # would silently depend on the database server agreeing with this one, and report a
    # timezone difference as a broken reaper.
    print('\nlease expiry')
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f'UPDATE {voice_jobs.TABLE} SET lease_expires=%s WHERE id=%s',
                        (datetime.now() - timedelta(hours=1), job_id))
        conn.commit()
    finally:
        conn.close()
    freed = voice_jobs.reap_expired()
    check('expired lease reclaimed', freed >= 1, f'{freed} freed')
    back = voice_jobs.get(TEST_ACCOUNT, job_id)
    check('job is queued again', back['state'] == voice_jobs.QUEUED, back.get('state'))
    check('attempt count survived', back['attempts'] >= 1, str(back.get('attempts')))

    reclaimed = voice_jobs.claim_next('selftest-worker-second-pass')
    check('a fresh worker can take it',
          bool(reclaimed) and reclaimed['id'] == job_id,
          'nothing was claimable' if not reclaimed else f"got job {reclaimed['id']}")
    if not reclaimed:
        # Without a claim there is no worker id, and every check after this one is
        # about the job this worker holds. Stop cleanly rather than dying on a None.
        print('\n  cannot continue without a reclaimed job')
        cleanup()
        print('\n' + f'{len(_failures)} FAILED: ' + ', '.join(_failures))
        return 1
    worker = reclaimed['worker_id']

    # -- completion ---------------------------------------------------------
    print('\ncompletion')
    done = voice_jobs.complete(job_id, worker_id=worker, episode=7,
                               filename='ep007_Self_Test_Episode.mp3')
    check('state is done', done['state'] == voice_jobs.DONE, done.get('state'))
    check('percent is 100', done['percent'] == 100)
    check('episode recorded', done['episode'] == 7)
    check('filename recorded', done['filename'] == 'ep007_Self_Test_Episode.mp3')
    check('finished job is not claimable',
          (voice_jobs.claim_next('selftest-after-done') or {}).get('id') != job_id)

    # -- heartbeat ----------------------------------------------------------
    print('\nheartbeat and studio status')
    voice_jobs.heartbeat('selftest-worker-1', account_id=TEST_ACCOUNT, label='Self test',
                         catalog={'voices': ['Johnny_Vibe', 'Ed_Clean_Vibe'],
                                  'presets': ['podcast', 'comedic']})
    status = voice_jobs.studio_status(TEST_ACCOUNT)
    check('studio reads as online', status['online'] is True, repr(status))
    check('catalog round-trips',
          status['catalog'].get('voices') == ['Johnny_Vibe', 'Ed_Clean_Vibe'],
          repr(status.get('catalog')))

    voice_jobs.heartbeat('selftest-worker-1', account_id=TEST_ACCOUNT, busy=True)
    status = voice_jobs.studio_status(TEST_ACCOUNT)
    check('a catalog-less heartbeat keeps the old catalog',
          bool(status['catalog'].get('voices')), repr(status.get('catalog')))
    check('busy flag recorded', status['busy'] is True)

    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            # Python's clock again, for the same reason as the lease above.
            cur.execute(f'UPDATE {voice_jobs.WORKERS_TABLE} SET last_seen=%s '
                        f'WHERE account_id=%s',
                        (datetime.now() - timedelta(hours=1), TEST_ACCOUNT))
        conn.commit()
    finally:
        conn.close()
    check('a stale studio reads as offline',
          voice_jobs.studio_status(TEST_ACCOUNT)['online'] is False)

    # -- isolation ----------------------------------------------------------
    print('\nisolation')
    try:
        voice_jobs.get(TEST_ACCOUNT - 1, job_id)
        check('another account cannot read the job', False, 'no error raised')
    except voice_jobs.VoiceJobError as e:
        check('another account cannot read the job', e.status == 404, f'status {e.status}')

    cleanup()
    print('\ncleanup: test rows removed')

    if _failures:
        print(f'\n{len(_failures)} FAILED: ' + ', '.join(_failures))
        return 1
    print('\nAll checks passed.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        try:
            cleanup()
        finally:
            raise
