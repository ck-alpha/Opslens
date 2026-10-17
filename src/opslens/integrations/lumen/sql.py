"""Opt-in governed tool; does not intercept upstream SQLAgent fetch paths."""

import asyncio

from lumen.ai.tools.base import FunctionTool

from opslens.sql import GovernedSQLService


def make_governed_sql_tool(service: GovernedSQLService) -> FunctionTool:
    async def execute_governed_sql(sql_query: str) -> str:
        """Evaluate and execute a candidate SQL through the configured read-only policy."""
        return (await asyncio.to_thread(service.execute, sql_query)).model_dump_json()

    return FunctionTool(execute_governed_sql, render_output=False)
