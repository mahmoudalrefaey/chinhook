"""The nodes of the workflow, one file each.

    route        where a message goes before anything is retrieved, written or run
    greeting     small talk, answered from nothing
    understand   a message read into a structured understanding and its tasks
    clarify      a question put to the user, and held until they answer
    answer       writing the reply
    common       what every node needs
    prompts      every instruction given to the model, in one place

Everything that happens per task (retrieve, generate, validate, execute, verify, repair) is
its own small graph in agent/task_graph.py, run once per task and, when a message holds more
than one, run for all of them at once. graph.py is what wires this package's nodes together
with that per-task graph into the one workflow a turn actually runs.
"""

from agent.nodes.answer import node_answer
from agent.nodes.clarify import node_clarify
from agent.nodes.common import (
    BASE_SQL_RULES,
    STAGE_GENERATE,
    STAGE_SUMMARISE,
    STAGE_UNDERSTAND,
)
from agent.nodes.greeting import node_greeting
from agent.nodes.route import node_route
from agent.nodes.understand import node_understand

__all__ = [
    "BASE_SQL_RULES",
    "STAGE_GENERATE",
    "STAGE_SUMMARISE",
    "STAGE_UNDERSTAND",
    "node_answer",
    "node_clarify",
    "node_greeting",
    "node_route",
    "node_understand",
]
