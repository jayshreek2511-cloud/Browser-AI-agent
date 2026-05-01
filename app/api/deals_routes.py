from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agent.deals_tracker.controller import DealsController
from app.agent.deals_tracker.service import DealsService
from app.core.sync_db import get_sync_session
from app.agent.deals_tracker.models import DealProduct, DealPriceHistory, DealAlert  # noqa: F401

router = APIRouter(tags=["deals"])

# ── Schemas ──────────────────────────────────────────────────────────

class DealsSearchRequest(BaseModel):
    query: str | None = None
    url: str | None = None

class TrackPriceRequest(BaseModel):
    product_id: str
    target_price: float

# ── Endpoints ────────────────────────────────────────────────────────

@router.post("/api/deals/search")
async def deals_search(payload: DealsSearchRequest, session: Session = Depends(get_sync_session)):
    service = DealsService(session)
    controller = DealsController(service)
    try:
        result = await controller.search(query=payload.query, url=payload.url)
        return {"status": "success", **result}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@router.get("/api/deals/history/{product_id}")
def deals_history(product_id: str, session: Session = Depends(get_sync_session)):
    service = DealsService(session)
    try:
        return {"status": "success", "history": service.get_history(product_id)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@router.post("/api/deals/track")
def deals_track(payload: TrackPriceRequest, session: Session = Depends(get_sync_session)):
    service = DealsService(session)
    try:
        return {"status": "success", "alert": service.set_alert(payload.product_id, payload.target_price)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
