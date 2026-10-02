"""The LangGraph workflow that answers a question.

    graph        the parent graph: routing, parallel tasks, timing, the turn's result
    task_graph   one task's own graph: retrieve, write SQL, check and run, verify, repair
    nodes/       the parent graph's nodes and every prompt the model is given
    llm          every call to the model, over any OpenAI-compatible endpoint
    router       routing that needs no model call
    schema       the connected database's catalog, cached per database, and schema retrieval
    verify       deterministic checks of a task's result
    state        the typed state the workflow passes around

Every call works on the database and model of the current session (see chinhook/runtime.py).
"""
