"""Knowledge Worker - handles historical incident context.

Uses pgvector for similarity search against past incidents.
For testing, the execute() method is monkey-patched with mocks.
"""

from workers.base_worker import BaseWorker


class KnowledgeWorker(BaseWorker):
    """Worker that searches historical incident knowledge base."""

    def __init__(self):
        super().__init__()
        self.register("search_similar", self._search_similar)

    def _search_similar(self, params: dict) -> dict:
        """Search for historically similar incidents."""
        return {"similar_incidents": []}
