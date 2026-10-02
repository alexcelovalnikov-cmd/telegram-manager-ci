"""Execute only explicitly reviewed shooting workflows; never infer acceptance."""
from rr_common import INSTANCE_ID, safe_error
from tm_projects import PROFILE, norm_id


class WorkflowError(RuntimeError):
    pass


def active_project_groups(cfg, instance=INSTANCE_ID):
    return {g['id']: g for g in cfg.groups.values()
            if g.get('enabled') and g.get('monitoring_enabled')
            and g.get('rules_profile') == PROFILE
            and g.get('reminder_list_instance_id') == instance
            and g.get('reminder_list_id')}


def verified_snapshot(bridge, group):
    value = bridge('snapshot', 'id:' + group['reminder_list_id'])
    rows = value.get('reminders')
    if (value.get('calendar_id') != group['reminder_list_id'] or not isinstance(rows, list)
            or value.get('complete') is not True or value.get('writable') is not True):
        raise WorkflowError('incomplete_native_snapshot')
    ids = [norm_id(r.get('id')) for r in rows]
    if not all(ids) or len(ids) != len(set(ids)):
        raise WorkflowError('duplicate_or_empty_native_identity')
    return {norm_id(r['id']): r for r in rows}


def synchronized(task, row):
    """A matching ID is insufficient: independently check the last native state."""
    base = task.get('project_sync_snapshot') or {}
    if not row or not base.get('fingerprint'):
        return False
    return (row.get('fingerprint') == base['fingerprint']
            and row.get('title') == task.get('title') == base.get('title')
            and (row.get('notes') or '') == (task.get('description') or '') == (base.get('notes') or '')
            and type(row.get('completed')) is bool
            and row['completed'] == (task.get('status') == 'completed') == base.get('completed')
            and (row.get('due_spec') or {}).get('kind') == 'none'
            and not task.get('project_sync_error') and not task.get('reminder_sync_conflict')
            and not task.get('project_delete_request'))


class WorkflowEngine:
    def __init__(self, db, bridge, instance=INSTANCE_ID):
        self.db, self.bridge, self.instance = db, bridge, instance

    def tick(self, cfg, dry=False, task_ids=None):
        context = self.db.rpc('tm_workflow_context_v16', {'p_instance_id': self.instance}).execute().data
        if not isinstance(context, dict) or not isinstance(context.get('workflows'), list):
            raise WorkflowError('invalid_workflow_context')
        groups = active_project_groups(cfg, self.instance)
        totals = {'completed': 0, 'preview': 0, 'hold': 0, 'awaiting_review': 0, 'task_ids': [], 'items': []}
        snapshots = {}
        for w in context['workflows']:
            tid = w['task_id']
            if task_ids is not None and tid not in task_ids:
                continue
            g = groups.get(w.get('group_id'))
            if not g or not w.get('enabled') or w.get('auto_done'):
                continue
            if w.get('hold_reason'):
                totals['awaiting_review'] += 1
                continue
            # Post/edit/color/mixed are completed through an explicit chat
            # acceptance RPC. They are never completed from a date or a keyword.
            if w.get('project_kind') != 'shoot' or w.get('auto_ready') is not True:
                continue
            try:
                if g['id'] not in snapshots:
                    snapshots[g['id']] = verified_snapshot(self.bridge, g)
                r = snapshots[g['id']].get(norm_id(w.get('apple_reminder_id')))
                if (not r or not w.get('native_fingerprint')
                        or r.get('fingerprint') != w['native_fingerprint']
                        or r.get('completed') is not False
                        or (r.get('due_spec') or {}).get('kind') != 'none'):
                    raise WorkflowError('workflow_native_changed')
                if dry:
                    totals['preview'] += 1
                    continue
                result = self.db.rpc('tm_workflow_complete_v16', {
                    'p_task_id': tid, 'p_expected_updated_at': w['task_updated_at'],
                    'p_instance_id': self.instance, 'p_list_id': g['reminder_list_id'],
                    'p_fingerprint': r['fingerprint']}).execute().data
                if result.get('applied'):
                    totals['completed'] += 1
                    totals['task_ids'].append(tid)
                elif result.get('reason'):
                    totals['awaiting_review'] += 1
                totals['items'].append(dict(task_id=tid, result=result))
            except Exception as exc:
                totals['hold'] += 1
                totals['items'].append({'task_id': tid, 'error': safe_error(exc)})
        return totals
