from worker.tm_source_discovery import discovery_due


def test_discovery_is_due_immediately_before_first_scan():
    assert discovery_due(None, 0.01, 600) is True


def test_discovery_waits_after_successful_scan():
    assert discovery_due(10.0, 609.0, 600) is False
    assert discovery_due(10.0, 610.0, 600) is True
