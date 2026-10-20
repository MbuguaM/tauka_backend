"""The milestone tracker (enrolment plan 4.7). Display only (E-16).

Learner: `GET /tracker/mine` — status, projected B2 date (lesson 60), one fix.
Tutor: `GET /tracker/pods` — their pods' members, status and lesson only.
"""

from fastapi import APIRouter, Depends

from app.dependencies import get_current_user
from app.services import tracker_service

router = APIRouter()


@router.get("/mine")
async def mine(user_id: str = Depends(get_current_user)):
    return tracker_service.for_student(user_id)


@router.get("/pods")
async def pods(user_id: str = Depends(get_current_user)):
    return {"pods": tracker_service.for_tutor(user_id)}
