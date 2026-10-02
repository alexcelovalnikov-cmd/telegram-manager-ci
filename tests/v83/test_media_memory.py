import json
import sqlite3
from pathlib import Path

from worker.tm_content_queue import ContentQueue


def meta():
    return {
        'chat_id': 1,
        'message_id': 2,
        'source_token': 'token',
        'retry_requested_at': '',
        'kind': 'voice',
    }


def test_audio_path_avoids_full_pcm_accumulation_and_uses_low_memory_decoder():
    text = Path('worker/tm_audio.py').read_text()
    assert 'np.concatenate' not in text
    assert 'samples.append' not in text
    assert "beam_size=1" in text
    assert "cpu_threads=1" in text
    assert "compute_type='int8'" in text
    assert "req['path']" in text


def test_audio_child_reports_sigkill_and_limits_allocator_threads():
    text = Path('worker/tm_extract.py').read_text()
    assert "audio_process_sigkill" in text
    assert "MALLOC_ARENA_MAX='2'" in text
    assert "OPENBLAS_NUM_THREADS='1'" in text
    assert "MKL_NUM_THREADS='1'" in text


def test_content_attempt_is_durable_before_heavy_work_and_quarantines_crash_loop():
    db = sqlite3.connect(':memory:')
    queue = ContentQueue(db)
    assert queue.offer(meta(), 'rev') is True
    job = queue.peek({1})
    first = queue.begin(job)
    assert first['attempts'] == 1
    row = db.execute(
        'SELECT state,attempts FROM content_jobs WHERE chat_id=1 AND message_id=2').fetchone()
    assert row == ('processing', 1)

    # Simulate the worker disappearing while heavy processing owns the lease.
    db.execute(
        'UPDATE content_jobs SET next_attempt=0 WHERE chat_id=1 AND message_id=2')
    db.commit()
    second = queue.begin(queue.peek({1}))
    assert second['attempts'] == 2

    db.execute(
        'UPDATE content_jobs SET next_attempt=0 WHERE chat_id=1 AND message_id=2')
    db.commit()
    exhausted = queue.begin(queue.peek({1}))
    assert exhausted['state'] == 'result'
    assert exhausted['result']['processing_error'] == 'processing_crash_retry_exhausted'


def test_worker_temp_media_is_disk_backed_not_cgroup_tmpfs():
    text = Path('deploy/ops/tmctl-root').read_text()
    start = text[text.index('worker_start_image()'):text.index('worker_stop()', text.index('worker_start_image()'))]
    assert 'src="$WORKER_ROOT/tmp",dst=/tmp' in start
    assert '--tmpfs /tmp:size=256m' not in start
    assert text.count('install -d -m 1777 -o root -g root "$WORKER_ROOT/tmp"') >= 2


def test_memory_telemetry_is_exposed_without_shell_access():
    worker = Path('worker/telegram_supabase.py').read_text()
    api = Path('tm_api/service.py').read_text()
    assert "'oom_kill_count':events.get('oom_kill')" in worker
    assert "'memory': cgroup_memory_stats()" in worker
    assert '"memory": details.get("memory")' in api


def test_normal_defer_releases_processing_lease_without_crash_count():
    db = sqlite3.connect(':memory:')
    queue = ContentQueue(db)
    queue.offer(meta(), 'rev')
    job = queue.begin(queue.peek({1}))
    assert job['attempts'] == 1
    queue.defer(job, 0)
    row = db.execute(
        'SELECT state,attempts FROM content_jobs WHERE chat_id=1 AND message_id=2').fetchone()
    assert row == ('queued', 1)
