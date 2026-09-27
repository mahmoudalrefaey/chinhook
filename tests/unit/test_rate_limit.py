"""rate_limit.py's token bucket: burst allowance, refill over time, and the shared ceiling."""

import time

from rate_limit import TokenBucket, check


def test_burst_up_to_capacity_then_refused():
    bucket = TokenBucket(capacity=3, per_minute=3)
    results = [bucket.take()[0] for _ in range(4)]
    assert results == [True, True, True, False]


def test_refused_request_reports_a_positive_wait():
    bucket = TokenBucket(capacity=1, per_minute=60)  # one token per second
    bucket.take()
    allowed, wait = bucket.take()
    assert allowed is False
    assert 0 < wait <= 1.1


def test_token_refills_after_waiting():
    bucket = TokenBucket(capacity=1, per_minute=600)  # one token per 0.1s
    bucket.take()
    time.sleep(0.15)
    allowed, _ = bucket.take()
    assert allowed is True


def test_give_back_restores_a_token_without_exceeding_capacity():
    bucket = TokenBucket(capacity=1, per_minute=60)
    bucket.take()
    bucket.give_back()
    allowed, _ = bucket.take()
    assert allowed is True


def test_check_refuses_and_refunds_when_only_the_shared_budget_is_exhausted(monkeypatch):
    import rate_limit as rl

    monkeypatch.setattr(rl, "_global_bucket", TokenBucket(capacity=1, per_minute=60))
    session_a = rl.TokenBucket(capacity=5, per_minute=300)
    session_b = rl.TokenBucket(capacity=5, per_minute=300)

    allowed_a, _ = check(session_a)
    allowed_b, message = check(session_b)

    assert allowed_a is True
    assert allowed_b is False
    assert "busy" in message.lower()
    # session_b's own token was spent then handed back, since it was the shared budget that
    # said no, not anything about that session specifically.
    assert session_b.tokens == 5
