"""Text-only media primitives. Never persists original media or storage URLs."""
import hashlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

DEFAULTS = {
    'media_storage_mode': 'text_only', 'media_processing_enabled': True,
    'media_max_bytes': 52428800, 'media_max_audio_seconds': 1800,
    'media_max_pdf_pages': 100, 'media_max_ocr_pages': 20,
    'media_max_text_chars': 200000, 'media_job_timeout_seconds': 900,
    'media_ocr_enabled': True, 'media_image_description_enabled': True,
    'media_audio_model': 'small', 'media_vision_model': 'qwen2.5vl:3b',
}
STATES = {'ready', 'partial', 'empty', 'needs_setup', 'unsupported', 'skipped',
          'failed', 'source_unavailable'}
MODELS_HOME = Path(os.environ.get('TM_MODELS_HOME', str(Path.home() / 'Library/Application Support/Telegram_Manager')))
MODELS_DIR = Path(os.environ.get('TM_MODELS_DIR', str(MODELS_HOME / 'models')))
AUDIO_PYTHON = Path(os.environ.get('TM_AUDIO_PYTHON', str(MODELS_HOME / 'media_venv/bin/python')))


class MediaError(RuntimeError):
    pass


def policy(raw):
    """Clamp resource limits. Missing/invalid mode is fail-closed, never upload."""
    p = dict(DEFAULTS)
    for k in p:
        if k in raw and raw[k] is not None:
            p[k] = raw[k]
    if p['media_storage_mode'] != 'text_only':
        raise MediaError('text_only_policy_required')
    bounds = {'media_max_bytes': (1024, 52428800), 'media_max_audio_seconds': (10, 3600),
              'media_max_pdf_pages': (1, 200), 'media_max_ocr_pages': (0, 30),
              'media_max_text_chars': (1000, 500000), 'media_job_timeout_seconds': (30, 1800)}
    for k, (lo, hi) in bounds.items():
        if isinstance(p[k], bool):
            raise MediaError('invalid_limit_' + k)
        p[k] = min(hi, max(lo, int(p[k])))
    for k in ('media_processing_enabled', 'media_ocr_enabled', 'media_image_description_enabled'):
        if not isinstance(p[k], bool):
            raise MediaError('invalid_boolean_' + k)
    if p['media_audio_model'] not in ('base', 'small', 'medium'):
        raise MediaError('audio_model_not_allowlisted')
    if p['media_vision_model'] not in ('qwen2.5vl:3b', 'qwen2.5vl:7b', 'gemma3:4b'):
        raise MediaError('local_vision_model_not_allowlisted')
    return p


def media_meta(m):
    f = getattr(m, 'file', None)
    if not getattr(m, 'media', None) or f is None:
        return None
    mime = getattr(f, 'mime_type', None)
    filename = getattr(f, 'name', None)
    kind = 'document'
    if getattr(m, 'photo', None): kind = 'photo'
    elif getattr(m, 'voice', None): kind = 'voice'
    elif mime == 'application/pdf': kind = 'pdf'
    elif getattr(m, 'video', None) or (mime or '').startswith('video/'): kind = 'video'
    elif getattr(m, 'audio', None) or (mime or '').startswith('audio/'): kind = 'audio'
    elif (mime or '').startswith('image/'): kind = 'photo'
    obj = getattr(m, 'document', None) or getattr(m, 'photo', None)
    fid = getattr(obj, 'id', None)
    info = {'chat_id': int(m.chat_id), 'message_id': int(m.id), 'attachment_index': 0,
            'kind': kind, 'mime_type': mime, 'file_name': filename,
            'file_size': getattr(f, 'size', None), 'duration_seconds': getattr(f, 'duration', None),
            'width': getattr(f, 'width', None), 'height': getattr(f, 'height', None)}
    identity = dict(info, telegram_file_id=str(fid) if fid is not None else None)
    if fid is None:
        identity['revision'] = str(getattr(m, 'edit_date', None) or getattr(m, 'date', None))
    info['source_token'] = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    return info


def skip_reason(meta, settings):
    if not settings['media_processing_enabled']:
        return 'processing_disabled'
    if meta['kind'] == 'video':
        return 'video_metadata_only'
    size = meta.get('file_size')
    if size is not None and size > settings['media_max_bytes']:
        return 'size_limit'
    seconds = meta.get('duration_seconds')
    if seconds is not None and seconds > settings['media_max_audio_seconds']:
        return 'duration_limit'
    if meta['kind'] == 'document':
        ext = Path(meta.get('file_name') or '').suffix.lower()
        if ext not in ('.txt', '.md', '.csv', '.json', '.log', '.docx'):
            return 'document_format_not_supported'
    return None


def blank_result(status, reason=None):
    return {'processing_status': status, 'processing_error': reason, 'extracted_text': '',
            'transcript': '', 'content_summary': '', 'processing_details': {},
            'text_is_complete': status == 'ready'}


def normalize_result(data, max_chars=200000):
    if not isinstance(data, dict) or data.get('processing_status') not in STATES:
        raise MediaError('invalid_processor_result')
    out = blank_result(data['processing_status'], data.get('processing_error'))
    if out['processing_error'] is not None:
        out['processing_error'] = str(out['processing_error'])[:120]
    truncated = False
    for k in ('extracted_text', 'transcript', 'content_summary'):
        val = data.get(k, '')
        if not isinstance(val, str):
            raise MediaError('invalid_text_field_' + k)
        val = ''.join(c for c in val if c in '\n\r\t' or ord(c) >= 32).replace('\x00', '')
        cap = min(max_chars, 12000) if k == 'content_summary' else max_chars
        truncated = truncated or len(val) > cap
        out[k] = val[:cap]
    details = data.get('processing_details', {})
    if not isinstance(details, dict) or len(json.dumps(details, ensure_ascii=False)) > 1000000:
        raise MediaError('invalid_processing_details')
    # Raw bytes, file paths, credentials, and URL payloads are not accepted as output.
    forbidden = {'base64', 'image_base64', 'original_bytes', 'local_path', 'storage_path', 'storage_bucket'}
    def inspect(x):
        if isinstance(x, dict):
            if forbidden.intersection(x): raise MediaError('binary_or_path_in_result')
            for v in x.values(): inspect(v)
        elif isinstance(x, list):
            for v in x: inspect(v)
    inspect(details)
    out['processing_details'] = details
    out['text_is_complete'] = bool(data.get('text_is_complete', data['processing_status'] == 'ready')) and not truncated
    if truncated:
        out['processing_status'] = 'partial'
        out['processing_details'] = dict(details, output_truncated=True)
    if not any(out[k].strip() for k in ('extracted_text', 'transcript', 'content_summary')) and out['processing_status'] == 'ready':
        out.update(processing_status='empty', text_is_complete=False)
    return out


class LimitedWriter:
    """Download cap is enforced on actual bytes, not only Telegram metadata."""
    def __init__(self, file, max_bytes):
        self.file, self.max_bytes, self.total = file, max_bytes, 0
    def write(self, value):
        if self.total + len(value) > self.max_bytes:
            raise MediaError('download_size_limit')
        n = self.file.write(value)
        self.total += n
        return n
    def flush(self): return self.file.flush()


class TempMedia:
    """Per-job files outside runtime/backups; only our marked directories cleaned."""
    MAGIC = 'Telegram_Manager_V18_ephemeral_media'
    def __init__(self, root=None):
        self.root = Path(root or (Path(tempfile.gettempdir()) / 'Telegram_Manager_V18_media'))
        if self.root.is_symlink(): raise MediaError('unsafe_temp_root')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.stat().st_uid != os.getuid(): raise MediaError('temp_owner_mismatch')
        os.chmod(self.root, 0o700)
        self.path = None
    def sweep(self):
        # Call only after acquiring the collector singleton lock.
        for p in self.root.iterdir():
            if p.is_symlink() or not p.is_dir() or not p.name.startswith('job_'): continue
            marker = p / '.owner'
            if marker.is_file() and not marker.is_symlink() and marker.read_text() == self.MAGIC:
                shutil.rmtree(p)
    def __enter__(self):
        self.path = Path(tempfile.mkdtemp(prefix='job_', dir=str(self.root)))
        (self.path / '.owner').write_text(self.MAGIC)
        return self.path
    def __exit__(self, kind, value, tb):
        if self.path and self.path.exists(): shutil.rmtree(self.path)


def run_processor_request(path, meta, settings, native_path=None):
    """JSON-only protocol; the caller applies a hard timeout to this subprocess."""
    return {'path': str(path), 'metadata': meta, 'settings': settings,
            'native_path': str(native_path) if native_path else str(Path(__file__).with_name('media_native_bridge'))}
