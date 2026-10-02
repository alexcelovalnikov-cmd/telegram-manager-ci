"""Only a successfully read, matching server profile can update the local cache."""
from __future__ import annotations
import json
import os
import tempfile
from pathlib import Path


def validate_bundle(bundle: dict) -> dict:
    if not isinstance(bundle, dict): raise ValueError('review_bundle_required')
    profile, contract, interaction = (bundle.get(k) for k in ('display_profile','display_contract','interaction_contract'))
    if not isinstance(profile,dict) or not isinstance(contract,dict) or not isinstance(interaction,dict):
        raise ValueError('server_review_contracts_missing')
    if contract.get('required') is not True or contract.get('profile_key')!=profile.get('profile_key') or contract.get('profile_version')!=profile.get('version'):
        raise ValueError('display_profile_version_mismatch')
    s=profile.get('settings')
    if not isinstance(s,dict) or s.get('layout')!='cards' or s.get('theme') not in ('dark','light'):
        raise ValueError('invalid_cards_profile')
    controls=('quick_answer_enabled','always_include_clarify','no_fake_buttons')
    if s.get('quick_answer_style')!='immediate_single_question':
        controls+=('quick_answer_footer','show_copy_answer','show_reset_selection')
    elif any(s.get(k) is not False for k in ('quick_answer_footer','show_copy_answer','show_reset_selection')):
        raise ValueError('batch_controls_forbidden')
    for key in controls:
        if s.get(key) is not True: raise ValueError('mandatory_review_control_missing:'+key)
    if interaction.get('mode')!='sequential_clarification' or interaction.get('max_unresolved_questions_per_message')!=1:
        raise ValueError('sequential_clarification_contract_missing')
    if not isinstance(bundle.get('due'),list): raise ValueError('review_due_array_required')
    return bundle


def cache_profile(bundle: dict, root: Path) -> Path:
    validate_bundle(bundle)
    root=Path(root); root.mkdir(parents=True,mode=0o700,exist_ok=True)
    target=root/'review_profile_cache.json'
    if root.is_symlink() or target.is_symlink(): raise ValueError('unsafe_profile_cache')
    os.chmod(root,0o700)
    payload={k:bundle[k] for k in ('display_profile','display_contract','interaction_contract')}
    fd,tmp=tempfile.mkstemp(prefix='.profile-',dir=root)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(payload,stream,ensure_ascii=False,indent=2)
            stream.flush();os.fsync(stream.fileno())
        os.replace(tmp,target)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return target
