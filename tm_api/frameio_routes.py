"""Owner-only Frame.io OAuth start and public Adobe callback routes."""
import asyncio
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route
from .frameio import FrameIOError

PUBLIC_PATHS=frozenset({"/frameio/oauth/callback"})

def routes(oauth,record):
    async def start(request):
        try:
            target=oauth.begin()
            record("frameio_oauth_start","ok")
            return RedirectResponse(target,status_code=303,
                headers={"Cache-Control":"no-store","Referrer-Policy":"no-referrer"})
        except FrameIOError:
            record("frameio_oauth_start","unavailable")
            return HTMLResponse("Frame.io authorization is unavailable.",status_code=503,
                                headers={"Cache-Control":"no-store"})

    async def callback(request):
        if request.query_params.get("error"):
            record("frameio_oauth_callback","rejected")
            return HTMLResponse("Frame.io authorization was not completed.",status_code=400,
                                headers={"Cache-Control":"no-store"})
        code=request.query_params.get("code")
        state=request.query_params.get("state")
        try:
            await asyncio.to_thread(oauth.complete,code,state)
        except FrameIOError:
            record("frameio_oauth_callback","rejected")
            return HTMLResponse("Frame.io authorization could not be completed.",status_code=400,
                                headers={"Cache-Control":"no-store"})
        record("frameio_oauth_callback","ok")
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><title>Frame.io connected</title>"
            "<p>Frame.io read-only access is connected. This window can be closed.</p>",
            status_code=200,headers={"Cache-Control":"no-store","Referrer-Policy":"no-referrer"})

    return [
        Route("/frameio-owner/oauth/start",start,methods=["GET"]),
        Route("/frameio/oauth/callback",callback,methods=["GET"]),
    ]
