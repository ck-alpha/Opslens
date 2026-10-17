"""Do not duplicate upstream document tools or modify the Lumen planner."""

from lumen.ai.tools.base import FunctionTool
from lumen.ai.tools.document_llm_tools import make_document_vector_llm_tools

from opslens.clarification.service import MetricClarificationService
from opslens.knowledge.index import BusinessKnowledgeIndex


def make_clarification_tool(service: MetricClarificationService) -> FunctionTool:
    async def clarify_metric(query: str) -> str:
        """Clarify an analytic metric; ASK_USER requires explicit user confirmation."""
        return (await service.clarify(query)).model_dump_json()

    return FunctionTool(clarify_metric, render_output=False)


def make_business_document_tools(index: BusinessKnowledgeIndex) -> list[FunctionTool]:
    # Upstream tools have no status option. Visible versioned names exclude historical docs.
    context = {
        "document_vector_store": index.store,
        "visible_docs": {
            f"{d.doc_id}@{d.version}" for d in index.corpus.documents if d.status == "ACTIVE"
        },
    }
    return make_document_vector_llm_tools(context)
