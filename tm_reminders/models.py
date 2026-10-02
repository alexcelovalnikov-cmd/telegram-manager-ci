from datetime import datetime,date
from zoneinfo import ZoneInfo
from typing import Literal
from pydantic import Field,model_validator
from tm_api.write_models import Strict

class Reminder(Strict):
    title: str = Field(min_length=1,max_length=500)
    description: str = Field(default='',max_length=16000)
    due_at: str | None = Field(default=None,max_length=64)
    all_day: bool = False
    timezone: str = Field(default='Asia/Yekaterinburg',max_length=120)
    status: Literal['open','in_progress','completed','cancelled'] = 'open'
    categories: list[str] = Field(default_factory=list,max_length=30)
    priority: int = Field(default=0,ge=0,le=9)
    recurrence: str | None = Field(default=None,max_length=1000)
    @model_validator(mode='after')
    def dates(self):
        ZoneInfo(self.timezone)
        if self.due_at:
            if self.all_day:date.fromisoformat(self.due_at)
            elif datetime.fromisoformat(self.due_at.replace('Z','+00:00')).tzinfo is None:raise ValueError('Due time needs offset')
        if any(not x or len(x)>100 or '\x00' in x for x in self.categories):raise ValueError('Invalid category')
        if self.recurrence and any(c in self.recurrence for c in '\r\n'):raise ValueError('Invalid recurrence')
        return self
