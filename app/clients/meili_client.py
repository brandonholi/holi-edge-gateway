import logging
import httpx
from typing import List
from ..core.config import settings

_logger = logging.getLogger(__name__)


class MeiliClient:
    """Lightweight async Meilisearch client returning list of matched SKUs."""

    def __init__(self):
        self.base_url = settings.MEILI_URL
        self.api_key = settings.MEILI_KEY

    async def search_skus(self, query: str, sede_code: str, limit: int = 20) -> List[str]:
        """Search products and return matching list of SKUs."""
        if not query or not query.strip():
            return []

        index_name = f"products_{sede_code.lower()}"
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=1.0) as client:
                res = await client.post(
                    f"/indexes/{index_name}/search",
                    json={"q": query, "limit": limit, "attributesToRetrieve": ["sku"]},
                    headers=headers
                )
                if res.status_code == 200:
                    data = res.json()
                    return [hit["sku"] for hit in data.get("hits", []) if "sku" in hit]
                else:
                    _logger.warning("Meilisearch responded with status %d", res.status_code)
                    return []
        except Exception as e:
            _logger.warning("Meilisearch error (degrading search): %s", e)
            return []
