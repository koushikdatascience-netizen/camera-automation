from camera_service.events.retry import RetryPolicy


def test_retry_policy_is_bounded():
    policy = RetryPolicy(base_seconds=2, maximum_seconds=60, jitter_ratio=0)
    assert policy.delay(1) == 2
    assert policy.delay(2) == 4
    assert policy.delay(20) == 60
