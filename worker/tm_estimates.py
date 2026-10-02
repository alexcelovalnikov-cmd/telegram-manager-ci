"""Deterministic reading of already-extracted estimate text, not financial truth.

No attachment download, file persistence, LLM call or payment/task mutation.
Ambiguous cells stay unknown. Operator POV is excluded from the personal sum.
"""
import re
from decimal import Decimal, InvalidOperation
from tm_projects import label_key, parse_title

PARSER_VERSION = 'estimate16.1'
MAX_TEXT = 100000
MAX_ROWS = 200
PERSONAL = {'оператор': 'operator', 'монтаж': 'edit', 'логистика': 'logistics'}
CURRENCY = re.compile(r'(₽|\bруб(?:лей|ля|ль)?\.?\b|\brub\b)', re.I)
ROW = re.compile(r'^\s*(?:\d+[.)]\s+)?(оператор(?:\s+pov)?|монтаж|логистика|итого|всего)(?=\s|[:;|—–-]|$)', re.I)
VALUE = r'\d+(?:[ \u00a0\u202f]\d{3})*(?:[.,]\d{1,2})?(?:\s*[кk])?'
MONEY = re.compile(r'(?<![\w.])(' + VALUE + r')\s*(₽|руб(?:лей|ля|ль)?\.?|rub)?(?![\w.])', re.I)


class EstimateError(ValueError):
    pass


def decimal_rubles(raw):
    value = re.sub(r'[\s\u00a0\u202f]', '', raw).replace(',', '.')
    kilo = value[-1:].casefold() in ('к', 'k')
    if kilo:
        value = value[:-1]
    try:
        n = Decimal(value) * (1000 if kilo else 1)
    except InvalidOperation:
        raise EstimateError('ambiguous_amount')
    if not n.is_finite() or n < 0 or n > 1000000000 or n != n.quantize(Decimal('0.01')):
        raise EstimateError('invalid_amount')
    return format(n, '.2f')


def row_amount(tail, rubles_explicit=False):
    """An explicit final total is accepted; unresolved arithmetic is not guessed."""
    if re.search(r'(?<!\w)[−-]\d|\(\s*\d', tail):
        return None, 'signed_amount_requires_review'
    if '?' in tail or re.search(r'(?<!\w)(от|до|около|примерно)\s+\d', tail, re.I):
        return None, 'uncertain_amount'
    if re.search(r'\d\s*[-–—]\s*\d|[€$]|\b(?:usd|eur)\b', tail, re.I):
        return None, 'range_or_other_currency'
    matches = list(MONEY.finditer(tail))
    if not matches:
        return None, 'amount_not_found'
    if len(matches) > 1:
        # Only an explicit equals/total label disambiguates rate x quantity.
        split = re.split(r'=|\b(?:итого|сумма)\s*[:=]?', tail, flags=re.I)
        if len(split) < 2 or len(list(MONEY.finditer(split[-1]))) != 1:
            return None, 'multiple_numeric_cells_require_review'
        matches = list(MONEY.finditer(split[-1]))
    match = matches[-1]
    raw = match.group(1)
    if not (match.group(2) or rubles_explicit or raw.strip().casefold().endswith(('к', 'k'))):
        return None, 'currency_not_explicit'
    try:
        return decimal_rubles(raw), None
    except EstimateError as exc:
        return None, str(exc)


def match_candidates(attachment, projects, text):
    linked = set(attachment.get('linked_task_ids') or [])
    allowed = [p for p in projects if p.get('source_chat_id') == attachment.get('chat_id') or p.get('id') in linked]
    candidates = []
    normalized = label_key(text[:20000])
    for p in allowed:
        if p['id'] in linked:
            candidates.append({'task_id': p['id'], 'reason': 'existing_message_link'})
            continue
        data = p.get('project_data') or parse_title(p.get('title') or '')
        tokens = [x for x in label_key(data.get('label') or '').split() if len(x) > 2]
        # Date alone, client alone or a generic work label never identify a project.
        specific = [x for x in tokens if x not in {'съемка', 'монтаж', 'проект', 'оператор', 'логистика'}]
        date_text = data.get('date_mmdd')
        if len(specific) >= 2 and date_text and date_text in text and all(x in normalized.split() for x in specific):
            candidates.append({'task_id': p['id'], 'reason': 'same_chat_date_and_project_words'})
    return sorted(candidates, key=lambda p: p['task_id'])[:20]


def parse_estimate(attachment, projects=()):
    text = attachment.get('extracted_text') or ''
    if not isinstance(text, str) or not text.strip():
        return None
    truncated = len(text) > MAX_TEXT
    text = text[:MAX_TEXT]
    lines = text.splitlines()
    rows = []
    currency_header = bool(re.search(r'(?:валюта|сумма|стоимость|цена).*?(?:₽|руб|RUB)', text[:3000], re.I))
    for line_number, line in enumerate(lines, 1):
        match = ROW.match(line)
        if not match:
            continue
        if len(rows) >= MAX_ROWS:
            truncated = True
            break
        label = ' '.join(match.group(1).casefold().split())
        category = PERSONAL.get(label)
        excluded = label == 'оператор pov'
        if excluded:
            category = 'operator_pov'
        if label in ('итого', 'всего'):
            category = 'estimate_total'
        value, warning = row_amount(line[match.end():], rubles_explicit=currency_header)
        rows.append({'line': line_number, 'label': match.group(1), 'category': category,
                     'amount_rub': value, 'included_in_personal_subtotal': label in PERSONAL,
                     'excluded': excluded, 'warning': warning, 'source_text': line[:180]})
    personal = [r for r in rows if r['included_in_personal_subtotal']]
    # An ordinary message saying "монтаж готов" is not an estimate.
    if not rows or not (re.search(r'\bсмет[аыуе]\b', text, re.I) or
                        sum(r['amount_rub'] is not None for r in rows) >= 2):
        return None
    source_complete = (attachment.get('processing_status') == 'ready'
                       and attachment.get('text_is_complete') is True and not truncated)
    known = sum((Decimal(r['amount_rub']) for r in personal if r['amount_rub'] is not None), Decimal(0))
    duplicates = len({r['category'] for r in personal}) != len(personal)
    complete = bool(personal) and source_complete and not duplicates and all(r['amount_rub'] is not None for r in personal)
    return {'parser_version': PARSER_VERSION, 'confirmation': 'unconfirmed_estimate',
            'review_required': True, 'payment_confirmed': False, 'rows': rows,
            'source_complete': source_complete, 'text_truncated': truncated,
            'personal_subtotal_rub': format(known, '.2f') if complete else None,
            'known_personal_subtotal_rub': format(known, '.2f') if personal else None,
            'personal_subtotal_complete': complete, 'duplicate_personal_rows': duplicates,
            'operator_pov_policy': 'excluded_unless_explicit_chat_override',
            'match_candidates': match_candidates(attachment, projects, text),
            'notice': 'Смета — предложение, не договор и не подтверждение оплаты. Строки и связь с проектом требуют проверки.'}


def process_batch(db, instance_id, limit=10):
    data = db.rpc('tm_media_batch_v16', {'p_instance_id': instance_id, 'p_limit': limit}).execute().data
    if not isinstance(data, dict) or not isinstance(data.get('attachments'), list) or not isinstance(data.get('projects'), list):
        raise EstimateError('invalid_media_batch')
    totals = {'read': 0, 'estimates': 0, 'stale': 0, 'native_changes': 0}
    for a in data['attachments']:
        parsed = parse_estimate(a, data['projects'])
        result = db.rpc('tm_record_media_v16', {
            'p_instance_id': instance_id, 'p_attachment_id': a['id'],
            'p_content_token': a['content_token'], 'p_parser_version': PARSER_VERSION,
            'p_parsed': parsed}).execute().data
        if result.get('applied'):
            totals['read'] += 1
            totals['estimates'] += int(parsed is not None)
        else:
            totals['stale'] += 1
    return totals
