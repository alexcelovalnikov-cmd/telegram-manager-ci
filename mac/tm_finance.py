"""Structured amounts and payment observations; no model output authorizes payment.

Money uses Decimal. A number in a Reminder title is imported information, not
proof of a contract or receipt. This module never edits Reminders or a ledger.
"""
from decimal import Decimal, InvalidOperation
import calendar
import re
from datetime import date

class FinanceError(ValueError):
    pass

_TERM = re.compile(r'^(\d+(?:[.,]\d{1,3})?)\s*(к|k|₽|руб\.?|р\.?)?$', re.I)

def rubles(value):
    if isinstance(value, bool) or value is None:
        raise FinanceError('amount_required')
    try:
        n = Decimal(str(value).strip().replace(',', '.'))
        if not n.is_finite() or n < 0 or n > Decimal('1000000000') or n != n.quantize(Decimal('.01')):
            raise FinanceError('invalid_rubles')
    except (InvalidOperation, ValueError) as exc:
        raise FinanceError('invalid_rubles') from exc
    return n

def format_money(value):
    return format(rubles(value), '.2f')

def amount_breakdown(title):
    """Parse only the final amount expression, preserving '?' and term labels.

    10к + 4к -> known sum 14000, 15к + ? -> known sum 15000 but total unknown.
    10к? -> proposed 10000, NOT confirmed debt. No guess of suffix on bare terms.
    """
    from tm_projects import strip_payment_suffix
    title = strip_payment_suffix(title or '')
    m = re.search(r'\s[-–—]\s+([^\n]+)$', title or '')
    raw = m.group(1).strip() if m else ''
    result = {'raw': raw, 'terms': [], 'known_subtotal': '0.00', 'total': None,
              'proposed_total': None, 'complete': False, 'source': 'reminder_title',
              'confirmation': 'imported_unverified'}
    if not raw or len(raw) > 500:
        return result
    parts = raw.split('+')
    if len(parts) > 16:
        return result
    complete, total, proposed = True, Decimal(0), Decimal(0)
    for i, text in enumerate(parts):
        text = text.strip()
        if text in ('?', '', '…', '...'):
            result['terms'].append({'key': 'part_'+str(i+1), 'raw': text, 'amount': None, 'state': 'unknown'})
            complete = False
            proposed = None
            continue
        doubtful = '?' in text
        term_text=text.rstrip('?').strip()
        match = _TERM.fullmatch(term_text)
        term_label=None
        if not match and not doubtful:
            labelled=re.fullmatch(r'([^?+\n]{1,80}?)\s+(\d+(?:[.,]\d{1,3})?\s*(?:к|k|₽|руб\.?|р\.?))',term_text,re.I)
            if labelled:
                term_label=labelled[1].strip()
                match=_TERM.fullmatch(labelled[2])
        if not match or not match[2]:
            result['terms'].append({'key': 'part_'+str(i+1), 'raw': text, 'amount': None, 'state': 'unparsed'})
            complete, proposed = False, None
            continue
        try:
            value = rubles(Decimal(match[1].replace(',', '.')) * (1000 if match[2].casefold() in ('к','k') else 1))
        except FinanceError:
            complete, proposed = False, None
            continue
        result['terms'].append({'key': 'part_'+str(i+1), 'raw': text,
                               'amount': format_money(value), 'state': 'proposed' if doubtful else 'stated'})
        if term_label:
            result['terms'][-1]['label']=term_label
        if not doubtful:
            total += value
        else:
            complete = False
        if proposed is not None:
            proposed += value
    if total > Decimal('1000000000') or (proposed is not None and proposed > Decimal('1000000000')):
        raise FinanceError('total_too_large')
    result.update(known_subtotal=format_money(total), complete=complete and bool(result['terms']),
                  total=format_money(total) if complete and result['terms'] else None,
                  proposed_total=format_money(proposed) if proposed is not None and result['terms'] else None)
    return result

def classify_payment_observation(text):
    """Conservative review hint; always review_required, never an authorization.

    Full conversation and stable project identity remain necessary. This is not
    an automatic keyword-based account reconciliation engine.
    """
    s = ' '.join(str(text or '').casefold().replace('ё','е').split())
    state = 'unknown'
    if not s:
        return {'state': state, 'review_required': True, 'is_payment_confirmation': False}
    instructional = ('платформ' in s and any(k in s for k in ('инструкц','сначала','после того','необходимо')))
    if instructional:
        state = 'instruction_not_receipt'
    elif '?' in s or any(k in s for k in ('не пришл','не оплат','еще не дош','ещe не дош','не поступ')):
        state = 'question_or_nonreceipt'
    elif any(k in s for k in ('аванс','частичн','половин','остаток','докину')):
        state = 'partial_or_remainder'
    elif any(k in s for k in ('деньги в офисе','деньги в сейф','дожидаются тебя','можешь забрать','наличные в')):
        state = 'available_not_received'
    elif any(k in s for k in ('оплатим','оплатят','передал в оплат','передали в оплат','отправил заявку','на следующей недел','постараюсь','оплата налом в')):
        state = 'awaiting_or_promised'
    elif any(k in s for k in ('пришло','деньги получил','получил оплат','забрал, спасибо','оплачен','деньги пришли')):
        state = 'receipt_candidate'
    elif 'чек' in s:
        state = 'document_not_receipt'
    return {'state': state, 'review_required': True, 'is_payment_confirmation': False}

def financial_summary(components, allocations, scope_confirmed=False):
    """Informational balance of confirmed known scope, not a 'delete' signal."""
    complete = scope_confirmed is True and bool(components)
    due, received = Decimal(0), Decimal(0)
    for c in components:
        if c.get('amount') is None or c.get('state') != 'confirmed' or c.get('evidence_valid') is False:
            complete = False
        else:
            due += rubles(c['amount'])
    for a in allocations:
        if a.get('state') != 'confirmed' or a.get('evidence_valid') is False:
            continue
        kind = a.get('kind')
        if kind in ('received','refund'):
            received += rubles(a['amount']) * (1 if kind == 'received' else -1)
    return {'agreed_known': format_money(due), 'received_net': format(received, '.2f'),
            'scope_complete': complete, 'balance': format(due-received, '.2f') if complete else None,
            'paid': None, 'delete_authorized': False}

def occurrence(pattern, period):
    """Explicit monthly period -> proposed record; never an Apple recurring alarm."""
    if pattern.get('frequency') != 'monthly':
        raise FinanceError('ad_hoc_has_no_schedule')
    if not re.fullmatch(r'\d{4}-\d{2}', str(period)):
        raise FinanceError('invalid_period')
    year, month = map(int,period.split('-'))
    date(year,month,1)
    label = pattern.get('label')
    if not isinstance(label,str) or not label.strip():
        raise FinanceError('missing_label')
    day = pattern.get('day')
    if day is not None and (type(day) is not int or not 1 <= day <= 31):
        raise FinanceError('invalid_day')
    # Do not shift the 31st silently in a short month.
    if day and day > calendar.monthrange(year,month)[1]:
        raise FinanceError('period_day_requires_review')
    from tm_projects import render_title
    data={'label':label, 'amount_rub':pattern.get('amount'), 'date_mmdd':f'{month:02d}.{day:02d}' if day else None}
    return {'occurrence_key':str(pattern['pattern_key'])+':'+period, 'period':period,
            'title':render_title(data), 'project_data':data, 'review_required':True,
            'due_spec':{'kind':'none'}, 'native_recurrence':False}
