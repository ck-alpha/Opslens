import asyncio
import json
from pathlib import Path

from lumen.ai.tools.base import FunctionTool

from opslens.clarification import MetricClarificationService
from opslens.integrations.lumen.tools import make_business_document_tools, make_clarification_tool
from opslens.knowledge import BusinessKnowledgeIndex, KnowledgeCorpus
from opslens.metrics import MetricRegistry

ROOT = Path(__file__).parents[2]


def test_public_function_tool_and_document_tools(monkeypatch):
    async def no_llm(*args, **kwargs):
        raise AssertionError("LLM must not be invoked")

    monkeypatch.setattr(FunctionTool, "_invoke_prompt", no_llm)
    registry = MetricRegistry.from_directory(ROOT / "data/metrics/definitions")
    index = BusinessKnowledgeIndex(KnowledgeCorpus(registry, ROOT / "data/knowledge"))
    asyncio.run(index.build())
    tool = make_clarification_tool(MetricClarificationService(registry, index))
    assert isinstance(tool, FunctionTool) and tool.render_output is False
    direct = json.loads(asyncio.run(tool.function(query="GMV")))
    assert direct["action"] == "ASK_USER"
    messages, context = asyncio.run(tool.respond([], {}, query="GMV"))
    assert '"action":"ASK_USER"' in messages[0] and context == {}
    doc_tools = {t.name: t for t in make_business_document_tools(index)}
    assert set(doc_tools) == {"list_indexed_documents", "search_document_chunks"}
    output, _ = asyncio.run(doc_tools["list_indexed_documents"].respond([], {}, limit=40, offset=0))
    assert "refund_policy@1.0" in output[0]
    assert "metric:gross_gmv@0.9" not in output[0]
    output, _ = asyncio.run(
        doc_tools["search_document_chunks"].respond(
            [], {}, query="退款", top_k=5, min_similarity=0.0, filename="refund_policy@1.0"
        )
    )
    assert "approved" in output[0] and "refund_policy@1.0" in output[0]
    assert "/home/" not in output[0]
