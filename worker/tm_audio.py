#!/usr/bin/env python3
"""Low-memory local speech-to-text child. Model installation is explicit."""
import json
import os
import sys
from pathlib import Path


def result(status, error=None):
    return {'processing_status': status, 'processing_error': error, 'extracted_text': '',
            'transcript': '', 'content_summary': '', 'processing_details': {},
            'text_is_complete': False}


def _peak_rss_bytes():
    try:
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(value if sys.platform == 'darwin' else value * 1024)
    except Exception:
        return None


def _decoded_duration_seconds(path, sample_rate, max_samples):
    import av
    total = 0
    with av.open(path) as media:
        streams = [stream for stream in media.streams if stream.type == 'audio']
        if not streams:
            return None, 0
        resampler = av.AudioResampler(format='s16', layout='mono', rate=sample_rate)
        for frame in media.decode(streams[0]):
            for chunk in resampler.resample(frame):
                total += int(chunk.samples)
                if total > max_samples:
                    return False, total
        for chunk in resampler.resample(None):
            total += int(chunk.samples)
            if total > max_samples:
                return False, total
    return True, total


def transcribe(req):
    from faster_whisper import WhisperModel

    settings = req['settings']
    name = settings['media_audio_model']
    if name not in ('base', 'small', 'medium'):
        raise ValueError('unknown_audio_model')

    models_home = Path(os.environ.get(
        'TM_MODELS_HOME',
        str(Path.home() / 'Library/Application Support/Telegram_Manager')))
    models_dir = Path(os.environ.get('TM_MODELS_DIR', str(models_home / 'models')))
    modelpath = models_dir / ('whisper-' + name)
    if not (modelpath / 'model.bin').is_file() or not (modelpath / 'tokenizer.json').is_file():
        return result('needs_setup', 'local_audio_model_missing')

    sample_rate = 16000
    max_seconds = min(3600, max(10, int(settings['media_max_audio_seconds'])))
    valid, total = _decoded_duration_seconds(
        req['path'], sample_rate, max_seconds * sample_rate)
    if valid is None:
        return result('empty', 'no_audio_stream')
    if valid is False:
        return result('skipped', 'decoded_audio_duration_limit')
    if not total:
        return result('empty', 'empty_audio')

    # Keep the same Small/int8 model but reduce decoder working memory.
    model = WhisperModel(
        str(modelpath), device='cpu', compute_type='int8',
        cpu_threads=1, num_workers=1, local_files_only=True)
    segments, info = model.transcribe(
        req['path'], beam_size=1, language=None, vad_filter=True,
        condition_on_previous_text=False, no_speech_threshold=0.6, temperature=0)

    text, timings, confidence_warnings, count = [], [], [], 0
    truncated = False
    for segment in segments:
        count += len(segment.text)
        if count > settings['media_max_text_chars'] or len(timings) >= 6000:
            truncated = True
            break
        clean = segment.text.strip()
        text.append(clean)
        timings.append({
            'start': round(segment.start, 2),
            'end': round(segment.end, 2),
            'text': clean,
        })
        if segment.avg_logprob < -0.8 and len(confidence_warnings) < 100:
            confidence_warnings.append({
                'start': round(segment.start, 2),
                'end': round(segment.end, 2),
            })

    full = '\n'.join(text)
    out = result('partial' if truncated else ('ready' if full else 'empty'))
    out.update(
        transcript=full,
        text_is_complete=bool(full) and not truncated,
        processing_details={
            'engine': 'faster_whisper_local_cpu',
            'model': name,
            'compute_type': 'int8',
            'beam_size': 1,
            'cpu_threads': 1,
            'duration_seconds': round(total / sample_rate, 2),
            'language': info.language,
            'language_probability': round(info.language_probability, 4),
            'segments': timings,
            'low_confidence_ranges': confidence_warnings,
            'transcript_is_machine_generated': True,
            'speakers_not_identified': True,
            'truncated': truncated,
            'peak_rss_bytes': _peak_rss_bytes(),
        },
    )
    return out


if __name__ == '__main__':
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    os.environ.setdefault('MALLOC_ARENA_MAX', '2')
    os.environ.setdefault('OMP_NUM_THREADS', '1')
    os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
    os.environ.setdefault('MKL_NUM_THREADS', '1')
    try:
        out = transcribe(json.loads(sys.stdin.read(1000000)))
    except ImportError:
        out = result('needs_setup', 'audio_dependencies_missing')
    except MemoryError:
        out = result('failed', 'audio_memory_error')
    except RuntimeError as exc:
        message = str(exc).casefold()
        error = 'audio_memory_error' if ('memory' in message or 'allocat' in message) else 'RuntimeError'
        out = result('failed', error)
    except Exception as exc:
        out = result('failed', type(exc).__name__)
    print(json.dumps(out, ensure_ascii=False))
