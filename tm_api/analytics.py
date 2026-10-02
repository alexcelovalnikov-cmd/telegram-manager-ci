"""Informational sums of existing projects; never derive a receipt from a title."""
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated
from pydantic import Field, AfterValidator, model_validator
from .write_models import Strict


def valid_date(value):
    date.fromisoformat(value)
    return value

ISODate = Annotated[str, Field(pattern=r'^\d{4}-\d{2}-\d{2}$'), AfterValidator(valid_date)]

class FinancialQuery(Strict):
    from_date: ISODate | None = None
    to_date: ISODate | None = None
    group_key: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    work_type: Annotated[str, Field(min_length=1, max_length=120)] | None = None

    @model_validator(mode='after')
    def ordered_dates(self):
        if self.from_date and self.to_date and self.from_date > self.to_date:
            raise ValueError('Date range reversed')
        return self


def money(value, signed=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        n = Decimal(str(value))
        if not n.is_finite() or (not signed and n < 0) or abs(n) > 10**12 or n != n.quantize(Decimal('.01')):
            return None
        return n
    except (InvalidOperation, ValueError):
        return None


def summarize(snapshot, filters):
    included, excluded = [], []
    totals = {k: Decimal(0) for k in ('recorded_amount', 'confirmed_components', 'received_net', 'awaiting_recorded_amount')}
    statuses = Counter()
    work_amounts = {}
    incomplete_amounts = 0
    for p in snapshot['items']:
        data = p.get('project_data') or {}
        if filters.group_key and filters.group_key != p.get('group_key'):
            continue
        work_type = data.get('project_kind') or 'unknown'
        streams = p.get('workstreams') or []
        types = {w.get('work_type') or 'unknown' for w in streams} if streams else {work_type}
        if filters.work_type and filters.work_type not in types:
            continue
        reason = None
        request_state = data.get('request_state')
        if p.get('salary') or data.get('salary') or work_type == 'salary':
            reason = 'salary_separate_series'
        elif request_state == 'pending':
            reason = 'unconfirmed_request'
        elif request_state == 'rejected' or p.get('status') == 'cancelled' or p.get('cancelled_at'):
            reason = 'rejected_or_cancelled'
        elif request_state not in (None, 'confirmed'):
            reason = 'unknown_request_state'
        # A legacy marker cannot certify work, even if structured state is absent.
        elif request_state is None and p.get('title', '').startswith('➕'):
            reason = 'unclassified_request_marker'
        when = data.get('date_iso')
        try:
            when = date.fromisoformat(when).isoformat()
        except (TypeError, ValueError):
            when = None
        if not reason and (filters.from_date or filters.to_date):
            if not when:
                reason = 'project_year_or_date_unknown'
            elif (filters.from_date and when < filters.from_date) or (filters.to_date and when > filters.to_date):
                continue
        if reason:
            excluded.append({'id': p['id'], 'reason': reason})
            continue
        # Incomplete/proposed amount breakdowns must not fall back to a misleading
        # legacy scalar. Imported amounts remain recorded amounts, not contracts.
        breakdown = data.get('amount_breakdown')
        if isinstance(data.get('finance_cost'),dict):
            amount=money(data['finance_cost'].get('work_amount'))
        elif isinstance(breakdown, dict):
            amount = money(breakdown.get('total')) if breakdown.get('complete') is True else None
        else:
            amount = money(data.get('amount_rub'))
        if data.get('amount_status') in ('proposed', 'estimate', 'unknown', 'unconfirmed'):
            amount = None
        ledger = p.get('ledger') or {}
        received = money(ledger.get('received_net'), signed=True)
        confirmed = money(ledger.get('confirmed_component_sum'))
        status = p.get('payment_status') or 'unknown'
        statuses[status] += 1
        if amount is None:
            incomplete_amounts += 1
        else:
            totals['recorded_amount'] += amount
            if status in ('awaiting', 'partial'):
                totals['awaiting_recorded_amount'] += amount
        totals['received_net'] += received or Decimal(0)
        totals['confirmed_components'] += confirmed or Decimal(0)
        components = [(w.get('work_type') or 'unknown', money(w.get('amount'))) for w in streams] if streams else [(work_type, amount)]
        for kind, value in components:
            if filters.work_type and kind != filters.work_type:
                continue
            bucket = work_amounts.setdefault(kind, {'recorded_amount': Decimal(0), 'unknown_components': 0})
            if value is None:
                bucket['unknown_components'] += 1
            else:
                bucket['recorded_amount'] += value
        included.append({'id': p['id'], 'title': p['title'], 'group_key': p['group_key'],
                        'project_date': when, 'project_kind': work_type, 'work_types': sorted(types), 'status': p['status'],
                        'payment_status': status, 'archived_at': p.get('project_archived_at'),
                        'recorded_amount': format(amount, '.2f') if amount is not None else None,
                        'received_net': format(received, '.2f') if received is not None else None,
                        'remaining_debt':format(max(amount-received,Decimal(0)),'.2f') if amount is not None and received is not None else None,
                        'legacy_paid_without_matching_ledger':status=='paid' and (received is None or (amount is not None and received<amount)),
                        'tax_excluded':isinstance(data.get('finance_cost'),dict),
                        'ledger_scope_complete': ledger.get('scope_complete') is True})
    return {'as_of': snapshot.get('as_of'), 'currency': 'RUB', 'project_count': len(included),
            'totals': {k: format(v, '.2f') for k, v in totals.items()},
            'work_type_amounts': {k: {'recorded_amount': format(v['recorded_amount'], '.2f'), 'unknown_components': v['unknown_components']} for k, v in sorted(work_amounts.items())},
            'payment_status_counts': dict(statuses), 'incomplete_amount_count': incomplete_amounts,
            'projects': included, 'excluded': excluded,
            'basis': {'dates': 'project_data.date_iso; year is never inferred from MM.DD',
                      'work_type': 'existing workstreams.work_type; project_kind only when there are no workstreams',
                      'work_type_filter': 'selects projects containing that work type; totals are whole-project totals; work_type_amounts is the selected component amount',
                      'work_type_amounts': 'recorded components, including estimates; not certified income or receipts; may differ from project totals',
                      'amount': 'explicit net work amount when finance_cost exists; otherwise legacy recorded amount, without inferring unknown tax',
                      'received': 'existing evidence-valid payment ledger only; paid status is reported separately',
                      'awaiting': 'recorded amounts of awaiting/partial projects, not outstanding balance',
                      'salary': 'excluded as a separate canonical series',
                      'archive': 'included; no duplicate history table'}}
