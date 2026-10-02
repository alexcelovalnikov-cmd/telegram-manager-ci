"""Presentation only; mutations retain the existing focused-answer transaction."""
import uuid
from typing import Annotated
from pydantic import Field
from .write_models import Strict, Answer
from .backend import GuardRejected

class PreparedAnswer(Strict):
    label: Annotated[str, Field(min_length=1, max_length=100)]
    answer: Answer

PreparedAnswers = Annotated[list[PreparedAnswer], Field(max_length=12)]
SIMPLE = {'Уточнить': 'clarify', 'Отложить': 'defer', 'Пропустить': 'dismiss', 'Игнорировать': 'dismiss'}
EFFECT_LABELS = {'set_payment_window': {'Ожидает оплату'},
                 'update_existing_project': {'Обновить существующий'},
                 'set_project_request_state': {'Подтвердить заявку', 'Отклонить заявку'}}

def context_text(question):
    c=question.get('context') or {}
    if isinstance(c,str): return c[:1200]
    text=[c[k] for k in ('short_context','known_state','context','uncertainty','question') if isinstance(c.get(k),str)]
    if text: return '\n\n'.join(text)[:1200]
    if c.get('amounts'): return 'Нужно уточнить сумму или её составляющие.'
    if question.get('kind')=='payment_uncertain': return 'Подтверждения оплаты пока нет.'
    return 'Для этого пункта требуется решение или уточнение в чате.'

async def _legacy_show(service, prepared_answers):
    current=await service.get_current_review_question()
    if current.get('backend')=='v24' and current['question']:
        profile=await service.db.rpc('tm_review_display_profile_v15',{})
        return await service.dynamic.run(service.dynamic.reviews.show,profile)
    if not current['question']:
        bundle=await service.get_review_bundle(explicit=True,limit=100)
        allowed=set(await service.group_ids())
        candidates=[q for q in bundle.get('due',[]) if q.get('status','open') in ('open','awaiting_user') and
                    (q.get('group_id') in allowed or q.get('reference',{}).get('instance_id')==service.config.instance_id)]
        previous=current['focus'].get('review_id')
        candidates.sort(key=lambda q:q.get('id')!=previous)
        if candidates:
            q=candidates[0]
            try:
                await service.guarded_write('set_review_focus','ui-focus-'+uuid.uuid4().hex,{
                    'review_id':q['id'],'review_revision':q['revision'],
                    'expected_version':current['focus']['version']})
            except GuardRejected:
                # Another client may have selected focus. Never force replacement.
                pass
            current=await service.get_current_review_question()
            if not current['question']: raise GuardRejected('review_focus_changed')
    q=current['question']; focus=current['focus']
    result={'review_ui':True,'focus':focus,'question':None,'selected_button_style':'solid_blue'}
    if not q: return result
    result['source_context']=current.get('source_context')
    result['source_instruction']='Read source_context and search linked chats before asking the user for missing facts. Keep this focus; source retrieval is not a business decision.'
    labels=list(dict.fromkeys([*(q.get('actions') or []),'Уточнить']))
    prepared={}
    for option in prepared_answers:
        a=option.answer
        if option.label not in labels or (a.review_id,a.review_revision,a.expected_version)!=(q['id'],q['revision'],focus['version']):
            raise GuardRejected('prepared_answer_not_current')
        if a.action!='resolved' or option.label not in EFFECT_LABELS[a.effect.operation]:
            raise GuardRejected('prepared_answer_label_mismatch')
        if a.effect.arguments.project_id != q.get('task_id'):
            raise GuardRejected('prepared_answer_project_mismatch')
        prepared[option.label]=a.model_dump(mode='json')
    options=[]
    for label in labels:
        answer=prepared.get(label) or {'review_id':q['id'],'review_revision':q['revision'],
            'expected_version':focus['version'],'action':SIMPLE.get(label,'clarify'),
            'user_text':q['title']+' — '+label,'confirmed':True,'effect':None}
        # The actual click is the explicit user reply, not model-authored user text.
        answer['user_text']=q['title']+' — '+label
        options.append({'label':label,'answer':answer,'needs_dialogue':answer['action']=='clarify'})
    change=current.get('project_change') or {}
    if change.get('prepared'):
        label='Обновить: '+change['after_title']
        details=[];patch=change.get('patch',{})
        if 'aliases' in patch:details.append('Альтернативные названия: '+(', '.join(patch['aliases']) or '(пусто)'))
        if 'date_iso' in patch:details.append('Дата: '+patch['date_iso'])
        if details:change['rationale']+='\n'+'\n'.join(details)
        answer={'review_id':q['id'],'review_revision':q['revision'],'expected_version':focus['version'],
            'action':'resolved','user_text':change['before_title']+' → '+change['after_title']+('\n'+'\n'.join(details) if details else ''),
            'confirmed':True,'effect':{'operation':'update_existing_project','arguments':change['apply_arguments']}}
        options.insert(0,{'label':label,'answer':answer,'needs_dialogue':False})
    result['question']={'id':q['id'],'revision':q['revision'],'title':q['title'],
                        'context':('Предлагаемое изменение:\n'+change['before_title']+'\n→ '+change['after_title']+'\n\n'+change['rationale']) if change.get('prepared') else context_text(q),'options':options}
    return result

async def show(service, prepared_answers):
    dynamic=getattr(service,'dynamic',None)
    profile=None
    if dynamic is not None:
        profile=await service.db.rpc('tm_review_display_profile_v15',{})
        result=await dynamic.run(dynamic.reviews.show,profile)
        if result.get('question'):
            return result
    result=await _legacy_show(service,prepared_answers)
    if profile is None and hasattr(service,'db'):
        profile=await service.db.rpc('tm_review_display_profile_v15',{})
    if profile is not None:result['display_profile']=profile
    return result
