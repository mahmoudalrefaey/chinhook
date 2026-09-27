"""The nodes of the workflow, one file each.

They were one file once, and the file had grown past two thousand lines with every stage of
a question in it. Split by what a stage is for:

    route        where a message goes before anything is retrieved, written or run
    greeting     small talk, answered from nothing
    understand   a message read into a structured understanding and its tasks
    clarify      a question put to the user, and held until they answer
    plan         which task is about to be worked on
    retrieve     the schema for one task, from the cache or from Qdrant
    ground       what the user's words mean in this schema
    generate     the query for one task
    validate     whether a query is safe to run
    execute      running one task's query
    verify       whether the result answers the task
    repair       bounded repair, and moving to the next task
    answer       writing the reply
    routing      where a node sends the run next
    common       what every node needs
    prompts      every instruction given to the model, in one place

graph.py imports the nodes from this package, so the graph itself is unchanged by the split.
"""

from agent.nodes.answer import node_answer, node_conversation_answer
from agent.nodes.clarify import node_clarify
from agent.nodes.common import (
    BASE_SQL_RULES,
    STAGE_EXECUTE,
    STAGE_GENERATE,
    STAGE_RETRIEVE,
    STAGE_SUMMARISE,
    STAGE_UNDERSTAND,
)
from agent.nodes.execute import node_execute
from agent.nodes.generate import node_generate
from agent.nodes.greeting import node_greeting
from agent.nodes.ground import node_ground
from agent.nodes.plan import node_plan
from agent.nodes.retrieve import node_retrieve
from agent.nodes.repair import node_next_task, node_repair_or_finish
from agent.nodes.route import node_route
from agent.nodes.routing import (
    route_after_execute,
    route_after_ground,
    route_after_next_task,
    route_after_repair,
    route_after_retrieve,
    route_after_route,
    route_after_understand,
    route_after_validate,
    route_after_verify,
)
from agent.nodes.understand import node_understand
from agent.nodes.validate import node_validate
from agent.nodes.verify import node_verify

__all__ = [
    "BASE_SQL_RULES",
    "STAGE_EXECUTE",
    "STAGE_GENERATE",
    "STAGE_RETRIEVE",
    "STAGE_SUMMARISE",
    "STAGE_UNDERSTAND",
    "node_answer",
    "node_clarify",
    "node_conversation_answer",
    "node_execute",
    "node_generate",
    "node_greeting",
    "node_ground",
    "node_next_task",
    "node_plan",
    "node_repair_or_finish",
    "node_retrieve",
    "node_route",
    "node_understand",
    "node_validate",
    "node_verify",
    "route_after_execute",
    "route_after_ground",
    "route_after_next_task",
    "route_after_repair",
    "route_after_retrieve",
    "route_after_route",
    "route_after_understand",
    "route_after_validate",
    "route_after_verify",
]
