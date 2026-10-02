"""Small bounded maintenance tasks; no extra schedule or push notifications."""
import asyncio
from rr_common import INSTANCE_ID,safe_error
from tm_review import refresh
from tm_estimates import process_batch

async def review_loop(c):
    while not c.stop.is_set():
        interval=300
        try:
            pref=getattr(c,'client_preferences',{})
            interval=max(60,min(3600,int(pref.get('review_interval_seconds',300))))
            if pref.get('review_enabled',True) and c.config:
                estimate_error=None
                try:
                    c.estimate_last=await c.io(lambda:process_batch(c.db,INSTANCE_ID,limit=10))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    estimate_error=safe_error(exc)
                    c.estimate_last={'error':estimate_error}
                c.review_last=await c.io(lambda:refresh(c.db,INSTANCE_ID))
                c.review_error=estimate_error
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            c.review_error=safe_error(exc)
        try:
            await asyncio.wait_for(c.stop.wait(),timeout=interval)
        except asyncio.TimeoutError:
            pass
