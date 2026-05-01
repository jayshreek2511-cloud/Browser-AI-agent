from __future__ import annotations
import logging
from typing import Any
from sqlalchemy.orm import Session
from sqlmodel import select

from .evaluator import evaluate_deal
from .models import DealAlert, DealPriceHistory, DealProduct
from .scraper import (
    ScrapedProduct,
    search_products,
    scrape_url,
    filter_products,
    build_related_query,
    is_primary_product,
    is_valid_product_link,
)

logger = logging.getLogger(__name__)


class DealsService:
    """Orchestrates search → scrape → evaluate → persist for the Deals vertical.

    Uses Synchronous SQLAlchemy for stability on Windows.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # ── Main entry point ─────────────────────────────────────────────────

    async def search(self, *, query: str | None = None, url: str | None = None) -> dict[str, Any]:
        """Returns a rich response dict.

        For URL flow:  { mode: "url", main_product, related, comparison, verdict, results }
        For query flow: { mode: "search", results }
        """
        if url:
            return await self._url_flow(url)
        if query:
            return await self._search_flow(query)
        return {"mode": "search", "results": []}

    # ════════════════════════════════════════════════════════════════════
    #  URL FLOW — extract main → find related → compare → verdict
    # ════════════════════════════════════════════════════════════════════

    async def _url_flow(self, url: str) -> dict[str, Any]:
        # Step 1: Extract main product from the pasted URL
        raw_products = await scrape_url(url)
        if not raw_products:
            return {"mode": "url", "main_product": None, "related": [], "comparison": [], "verdict": None, "results": []}

        main_raw = raw_products[0]
        main_product = self._persist_and_evaluate(main_raw)

        # Step 2: Search for related products using the main product's name
        related_query = build_related_query(main_raw.name)
        related_raw: list[ScrapedProduct] = []
        try:
            related_raw = await search_products(related_query)
        except Exception as exc:
            logger.warning("Failed to find related products: %s", exc)

        # Step 3: Filter strictly — no accessories, valid links, real prices
        related_filtered = filter_products(related_raw, main_raw.name)

        # Remove duplicates that match the main product (same source + similar price)
        related_filtered = [
            p for p in related_filtered
            if not (p.source == main_raw.source and abs(p.price - main_raw.price) < 100)
        ]

        # Persist and evaluate related products
        related: list[dict[str, Any]] = []
        for p in related_filtered[:5]:  # Max 5 related
            item = self._persist_and_evaluate(p)
            related.append(item)

        # Step 4: Build price comparison
        all_items = [main_product] + related
        comparison = self._build_comparison(all_items)

        # Step 5: Final verdict
        verdict = self._build_verdict(main_product, all_items)

        # Also return flat results list for backward compat
        results = [main_product] + related

        return {
            "mode": "url",
            "main_product": main_product,
            "related": related,
            "comparison": comparison,
            "verdict": verdict,
            "results": results,
        }

    # ════════════════════════════════════════════════════════════════════
    #  SEARCH FLOW — search products + filter accessories
    # ════════════════════════════════════════════════════════════════════

    async def _search_flow(self, query: str) -> dict[str, Any]:
        raw_products = await search_products(query)

        if not raw_products:
            return {"mode": "search", "results": []}

        # Apply accessory filter
        filtered = filter_products(raw_products, query)

        results: list[dict[str, Any]] = []
        for p in filtered:
            item = self._persist_and_evaluate(p)
            results.append(item)

        return {"mode": "search", "results": results}

    # ════════════════════════════════════════════════════════════════════
    #  COMPARISON & VERDICT
    # ════════════════════════════════════════════════════════════════════

    def _build_comparison(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Build a source → best price comparison table."""
        sources: dict[str, dict[str, Any]] = {}
        for item in items:
            source = item.get("source", "Unknown")
            price = item.get("price")
            if price is None:
                continue
            if source not in sources or price < sources[source]["price"]:
                sources[source] = {
                    "source": source,
                    "price": price,
                    "link": item.get("link"),
                    "name": item.get("name"),
                }

        comparison = list(sources.values())
        comparison.sort(key=lambda x: x["price"])

        # Mark the lowest
        if comparison:
            comparison[0]["is_best"] = True

        return comparison

    def _build_verdict(self, main_product: dict[str, Any], all_items: list[dict[str, Any]]) -> dict[str, Any]:
        """Determine if the main product is the best deal or if a better option exists."""
        if not all_items:
            return {"status": "UNKNOWN", "message": "No data available for comparison"}

        best = min(all_items, key=lambda x: x.get("price", float("inf")))
        main_price = main_product.get("price", float("inf"))
        best_price = best.get("price", float("inf"))

        if best.get("link") == main_product.get("link") or abs(best_price - main_price) < 100:
            return {
                "status": "BEST_DEAL",
                "message": "This is the BEST available deal!",
                "best_price": main_price,
                "best_source": main_product.get("source"),
            }
        else:
            savings = main_price - best_price
            return {
                "status": "BETTER_AVAILABLE",
                "message": f"Better deal available on {best.get('source')} at ₹{int(best_price):,}",
                "best_price": best_price,
                "best_source": best.get("source"),
                "best_link": best.get("link"),
                "savings": savings,
            }

    # ════════════════════════════════════════════════════════════════════
    #  PERSIST & EVALUATE HELPER
    # ════════════════════════════════════════════════════════════════════

    def _persist_and_evaluate(self, p: ScrapedProduct) -> dict[str, Any]:
        """Persist product + price, evaluate deal, return dict."""
        product = self._get_or_create_product(p.name, p.link)
        self._record_price(product.id, p.price, p.source, p.link, p.rating)
        history = self._price_list(product.id)
        deal_status = evaluate_deal(p.price, history)

        return {
            "product_id": product.id,
            "name": p.name,
            "price": p.price,
            "rating": p.rating,
            "link": p.link,
            "source": p.source,
            "deal_status": deal_status,
        }

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
