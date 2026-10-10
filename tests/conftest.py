import sys
from pathlib import Path

# Make the plugin package root importable (core/, endpoints/) and the tests
# directory importable (fixtures) regardless of how pytest is invoked.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from fixtures import FakeToolInvocation  # noqa: E402


@pytest.fixture
def fake_invoke():
    return FakeToolInvocation()
