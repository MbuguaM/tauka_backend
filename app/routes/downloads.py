"""Installers behind sign-in (§155). See download_service.

Public:  GET  /downloads/availability       which platforms exist, with no address
         GET  /downloads/appcast.xml?token  the update feed, for a token from update-feed
Sign-in: POST /downloads/update-feed        the feed address for this user's app
         POST /downloads/{platform}/link    a 5-minute link to the installer
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from app.dependencies import get_current_user
from app.services import download_service as svc
from app.services.download_service import DownloadRefused

router = APIRouter()
_STATUS = {"not_available": 404, "not_configured": 503, "unavailable": 503, "bad_token": 401}


def _refused(exc: DownloadRefused) -> HTTPException:
    return HTTPException(status_code=_STATUS.get(exc.reason, 400), detail={"reason": exc.reason})


@router.get("/availability")
async def availability():
    return svc.availability()


@router.get("/appcast.xml")
async def appcast(token: str = Query(default="", max_length=200)):
    try:
        return Response(content=svc.appcast(token), media_type="application/xml",
                        headers={"Cache-Control": "no-store"})
    except DownloadRefused as exc:
        raise _refused(exc)


@router.post("/update-feed")
async def update_feed(user_id: str = Depends(get_current_user)):
    try:
        return {"feed_url": svc.feed_url(user_id)}
    except DownloadRefused as exc:
        raise _refused(exc)


@router.post("/{platform}/link")
async def download_link(platform: str, user_id: str = Depends(get_current_user)):
    try:
        return svc.link(platform)
    except DownloadRefused as exc:
        raise _refused(exc)
