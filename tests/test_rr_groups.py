from worker.rr_groups import GroupConfig

def test_legacy_manual_task_fallback_uses_rental_name():
    raw={'schema_version':7,'groups':[],'members':[],'targets':[],'chats':[]}
    assert GroupConfig(raw).destination({})=='Рентал'
