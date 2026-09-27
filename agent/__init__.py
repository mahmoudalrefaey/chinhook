"""LangGraph agentic workflow for the Chinook natural-language database chat.

The modules here sit on top of the infrastructure that already works in this repository:
Azure OpenAI through config.create_azure_client, schema retrieval and SQL execution through
scripts.db_module, and the validation that run_sql_query already performs. Nothing in
scripts/ or config.py was replaced; the graph composes what was already there and adds the
reasoning, task isolation, verification and repair that were missing.

Import order matters only in that agent.llm is imported by the node modules.
"""

from agent.graph import build_graph, run_turn
from agent.session import ChatSession, new_chat

__all__ = ["build_graph", "run_turn", "ChatSession", "new_chat"]
