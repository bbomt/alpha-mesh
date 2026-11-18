import pytest


@pytest.fixture(autouse=True)
def aws_region(monkeypatch):
    """Workers' settings need a region (PMAT-347); a made-up one, so no test reads the owner's AWS profile."""
    monkeypatch.setenv("AWS_REGION", "zz-test-1")
