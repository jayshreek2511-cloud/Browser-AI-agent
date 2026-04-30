from __future__ import annotations
import logging
from typing import Any
from sqlalchemy.orm import Session
from sqlmodel import select

from .evaluator import evaluate_deal
from .models import DealAlert, DealPriceHistory, DealProduct
from .scraper import ScrapedProduct, search_products, scrape_url

logger = logging.getLogger(__name__)

class DealsService:
    """Orchestrates search → scrape → evaluate → persist for the Deals vertical.
    
    Now uses Synchronous SQLAlchemy for stability on Windows.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # ── Search & Compare (Keeps async because scraping is async) ─────────

    async def search(self, *, query: str | None = None, url: str | None = None) -> list[dict[str, Any]]:
        raw_products: list[ScrapedProduct] = []

        if url:
            raw_products = await scrape_url(url)
        if query:
            raw_products = await search_products(query)

        if not raw_products:
            return []

        results: list[dict[str, Any]] = []
        for p in raw_products:
            # Persist product + price snapshot (Sync DB calls)
            product = self._get_or_create_product(p.name, p.link)
            self._record_price(product.id, p.price, p.source, p.link, p.rating)

            # Evaluate deal
            history = self._price_list(product.id)
            deal_status = evaluate_deal(p.price, history)

            results.append({
                "product_id": product.id,
                "name": p.name,
                "price": p.price,
                "rating": p.rating,
                "link": p.link,
                "source": p.source,
                "deal_status": deal_status,
            })

        return results

    # ── Price History ───────────────────────────────────────────────────

    def get_history(self, product_id: str) -> list[dict[str, Any]]:
        stmt = (
            select(DealPriceHistory)
            .where(DealPriceHistory.product_id == product_id)
            .order_by(DealPriceHistory.checked_at.desc())
            .limit(100)
        )
        result = self.session.execute(stmt)
        rows = result.scalars().all()
        return [
            {
                "price": r.price,
                "source": r.source,
                "link": r.link,
                "rating": r.rating,
                "checked_at": r.checked_at.isoformat() if r.checked_at else None,
            }
            for r in rows
        ]

    # ── Alerts ──────────────────────────────────────────────────────────

    def set_alert(self, product_id: str, target_price: float) -> dict[str, Any]:
        alert = DealAlert(product_id=product_id, target_price=target_price)
        self.session.add(alert)
        self.session.commit()
        return {"alert_id": alert.id, "product_id": product_id, "target_price": target_price}

    def check_alerts(self) -> list[dict[str, Any]]:
        """Check all pending alerts and mark triggered ones."""
        stmt = select(DealAlert).where(DealAlert.triggered == False)
        alerts = self.session.execute(stmt).scalars().all()

        triggered: list[dict[str, Any]] = []
        for alert in alerts:
            prices = self._price_list(alert.product_id)
            if prices and min(prices) <= alert.target_price:
                alert.triggered = True
                self.session.add(alert)
                triggered.append({"alert_id": alert.id, "product_id": alert.product_id, "target_price": alert.target_price})

        if triggered:
            self.session.commit()

        return triggered

    # ── Internal helpers (Synchronous) ──────────────────────────────────

    def _get_or_create_product(self, name: str, url: str | None) -> DealProduct:
        stmt = select(DealProduct).where(DealProduct.name == name).limit(1)
        existing = self.session.execute(stmt).scalars().first()
        if existing:
            return existing

        product = DealProduct(name=name, canonical_url=url)
        self.session.add(product)
        self.session.commit()
        self.session.refresh(product)
        return product

    def _record_price(
        self, product_id: str, price: float, source: str, link: str | None, rating: float | None
    ) -> None:
        record = DealPriceHistory(
            product_id=product_id,
            price=price,
            source=source,
            link=link,
            rating=rating,
        )
        self.session.add(record)
        self.session.commit()

    def _price_list(self, product_id: str) -> list[float]:
        stmt = select(DealPriceHistory).where(DealPriceHistory.product_id == product_id)
        return [r.price for r in self.session.execute(stmt).scalars().all()]
