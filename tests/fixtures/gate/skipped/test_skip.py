import pytest


@pytest.mark.skip(reason='fixture')
def test_skipped():
    assert False
