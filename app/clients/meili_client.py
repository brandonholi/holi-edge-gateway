import logging
import httpx
from typing import List, Tuple
from ..core.config import settings

_logger = logging.getLogger(__name__)


class MeiliClient:
    """Lightweight async Meilisearch client returning list of matched SKUs."""

    def __init__(self):
        self.base_url = settings.MEILI_URL
        self.api_key = settings.MEILI_KEY

    async def search_skus(self, query: str, sede_code: str, limit: int = 20,
                          offset: int = 0) -> Tuple[List[str], int]:
        """Search products and return one page of matching SKUs plus the estimated total."""
        if not query or not query.strip():
            return [], 0

        index_name = f"products_{sede_code.lower()}"
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=1.0) as client:
                res = await client.post(
                    f"/indexes/{index_name}/search",
                    json={"q": query, "limit": limit, "offset": offset, "attributesToRetrieve": ["sku"]},
                    headers=headers
                )
                if res.status_code == 200:
                    data = res.json()
                    skus = [hit["sku"] for hit in data.get("hits", []) if "sku" in hit]
                    return skus, int(data.get("estimatedTotalHits", len(skus)))
                else:
                    _logger.warning("Meilisearch responded with status %d", res.status_code)
                    return [], 0
        except Exception as e:
            _logger.warning("Meilisearch error (degrading search): %s", e)
            return [], 0
