"""merge_tasks: the reducer that reconciles parallel task branches back into one list.

This is the piece that makes running several tasks at once safe. A plain list reducer such
as operator.add would concatenate what each branch returns instead of replacing a task by its
id, which is exactly the bug this closes: without it, a task that got retried inside its own
branch would show up twice, once as its stale first attempt and once as its real result.
"""

from agent.state import TaskState, merge_tasks


def _task(task_id, **kwargs):
    return TaskState(task_id=task_id, raw="", question="", **kwargs)


def test_a_fresh_write_from_each_branch_is_kept_in_task_id_order():
    result = merge_tasks([], [_task("T2"), _task("T1")])
    assert [t.task_id for t in result] == ["T1", "T2"]


def test_a_later_update_replaces_the_earlier_copy_of_the_same_task_rather_than_duplicating_it():
    first_attempt = _task("T1", attempts=0, status="pending")
    retried = _task("T1", attempts=1, status="verified")

    after_first = merge_tasks([], [first_attempt])
    after_retry = merge_tasks(after_first, [retried])

    assert len(after_retry) == 1
    assert after_retry[0].attempts == 1
    assert after_retry[0].status == "verified"


def test_two_different_tasks_finishing_in_the_same_step_both_survive():
    existing = merge_tasks([], [_task("T1")])
    result = merge_tasks(existing, [_task("T2")])
    assert [t.task_id for t in result] == ["T1", "T2"]


def test_a_single_task_not_wrapped_in_a_list_is_still_accepted():
    # LangGraph hands a reducer whatever a node returned for that key; node_run_task always
    # returns a one-item list, but the reducer itself should not assume that is the only
    # shape it will ever see.
    result = merge_tasks([], _task("T1"))
    assert [t.task_id for t in result] == ["T1"]
