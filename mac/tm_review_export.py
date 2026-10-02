#!/usr/bin/env python3
"""Read-only offline fallback. Interactive answers are available in the ChatGPT app."""
import argparse
import json
import os
from pathlib import Path

def safe_json(value):
    return json.dumps(value,ensure_ascii=False,allow_nan=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')

def render(items,profile=None,interaction=None):
    if not isinstance(items,list) or len(items)>200:
        raise ValueError('review_array_required_maximum_200')
    if profile is None:
        profile=json.loads(Path(__file__).with_name('review_profile.json').read_text(encoding='utf-8'))
    settings=profile.get('settings',profile)
    if not isinstance(settings,dict) or settings.get('layout')!='cards': raise ValueError('cards_profile_required')
    items=items[:1]
    rows=[]
    for item in items:
        actions=item.get('actions') or ['Уточнить','Отложить','Пропустить']
        if not isinstance(actions,list) or not all(isinstance(x,str) for x in actions): raise ValueError('invalid_actions')
        if 'Уточнить' not in actions: actions=list(actions)+['Уточнить']
        if type(item.get('revision')) is not int or item['revision']<1: raise ValueError('revision_required')
        rows.append({'id':str(item['id']),'revision':item['revision'],'title':str(item.get('title','')),
          'group':str(item.get('group_name') or 'Telegram Manager'),'source':str(item.get('source_name','')),
          'context':item.get('context',{}),'actions':actions})
    return _HTML.replace('__DATA__',safe_json(rows)).replace('__PROFILE__',safe_json(settings))

_HTML='''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'none'; img-src 'none'; form-action 'none'; base-uri 'none'">
<title>Telegram Manager — сверка</title><style>body{color-scheme:dark;background:#111;color:#f5f5f5;font:17px -apple-system,sans-serif;max-width:760px;margin:40px auto;padding:20px;line-height:1.5}article{background:#1b1b1b;border:1px solid #343434;border-radius:20px;padding:24px}h1{font-size:24px}h2{font-size:21px}p{color:#bdbdbd}a{color:#57a8ff}</style>
<h1>Telegram Manager · Сверка</h1><p>Это сохранённый снимок. Чтобы ответить на актуальный вопрос кнопкой, откройте сверку в чате Telegram Manager.</p>
<main id="card"></main><p><a href="https://chatgpt.com/plugins/plugin_asdk_app_6aba0191af7c8191b85262fc31825a68">Открыть Telegram Manager</a></p>
<script type="application/json" id="data">__DATA__</script><script>const rows=JSON.parse(document.getElementById('data').textContent);if(rows.length){const a=document.createElement('article'),h=document.createElement('h2'),p=document.createElement('p');h.textContent=rows[0].title;p.textContent='Требуется решение или уточнение в чате.';a.append(h,p);document.getElementById('card').append(a);}else document.getElementById('card').textContent='Сейчас обязательных решений нет.';</script></html>'''

def main():
    p=argparse.ArgumentParser(description=__doc__)
    src=p.add_mutually_exclusive_group(required=True);src.add_argument('--snapshot',type=Path);src.add_argument('--server',action='store_true')
    p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    profile=None;interaction=None
    if args.server:
        from rr_common import database,load_env,INSTANCE_ID
        bundle=database(load_env()).rpc('tm_review_bundle_v18',{'p_instance_id':INSTANCE_ID,'p_explicit':True,'p_limit':50}).execute().data
        from tm_review_cache import validate_bundle,cache_profile
        from rr_common import APP_DIR
        validate_bundle(bundle);cache_profile(bundle,APP_DIR/'review_cache')
        items=bundle['due'];profile=bundle['display_profile'];interaction=bundle['interaction_contract']
    else:
        if args.snapshot.stat().st_size>3000000: raise ValueError('snapshot_too_large')
        obj=json.loads(args.snapshot.read_text(encoding='utf-8'))
        if isinstance(obj,dict):
            items=obj.get('due',obj.get('items'));profile=obj.get('display_profile');interaction=obj.get('interaction_contract')
        else: items=obj
    if args.output.exists(): raise FileExistsError('Choose a new output path; existing files are not overwritten.')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as f: os.chmod(args.output,0o600);f.write(render(items,profile,interaction))
    print(str(args.output))
if __name__=='__main__': main()
