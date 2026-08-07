import logging
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from app.dependencies import require_admin
from app.models.schemas import TestimonialSubmitRequest
from app.services import testimonial_service
from app.services.supabase_client import supabase_admin as db

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/submit")
async def submit(req: TestimonialSubmitRequest):
    """PUBLIC — supporter submits their testimonial (link from email)."""
    try:
        return await testimonial_service.submit_testimonial(
            request_id=req.request_id,
            quote_text=req.quote_text,
            display_name=req.display_name,
            display_preference=req.display_preference,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/decline/{request_id}", response_class=HTMLResponse)
async def decline(request_id: str):
    """PUBLIC — supporter declines (link from email)."""
    await testimonial_service.decline_testimonial(request_id)
    return HTMLResponse(
        "<html><body><h2>Thanks for letting us know</h2>"
        "<p>We understand and won't ask again.</p></body></html>"
    )


@router.get("/published")
async def published(language: str | None = None):
    """PUBLIC — returns published testimonials for the marketing site."""
    return await testimonial_service.get_published_testimonials(language)


@router.post("/admin/approve/{request_id}")
async def admin_approve(
    request_id: str,
    user_id: str = Depends(require_admin),
):
    """Admin — approve a submitted testimonial."""
    result = db.schema("app").table("testimonial_requests").select("status").eq(
        "id", request_id
    ).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Testimonial request not found")

    if result.data[0]["status"] != "submitted":
        raise HTTPException(status_code=400, detail="Testimonial is not in submitted state")

    db.schema("app").table("testimonial_requests").update({
        "approved_by_admin": True,
        "status": "approved",
    }).eq("id", request_id).execute()

    return {"request_id": request_id, "status": "approved"}


@router.post("/admin/publish/{request_id}")
async def admin_publish(
    request_id: str,
    user_id: str = Depends(require_admin),
):
    """Admin — publish an approved testimonial."""
    from datetime import datetime, timezone

    result = db.schema("app").table("testimonial_requests").select("status").eq(
        "id", request_id
    ).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Testimonial request not found")

    if result.data[0]["status"] != "approved":
        raise HTTPException(status_code=400, detail="Testimonial is not approved yet")

    db.schema("app").table("testimonial_requests").update({
        "status": "published",
        "published_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", request_id).execute()

    return {"request_id": request_id, "status": "published"}
