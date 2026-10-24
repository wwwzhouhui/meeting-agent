import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.shared.schema import Meeting  # noqa: E402


@pytest.fixture()
def sample_meeting() -> Meeting:
    data = json.loads((ROOT / "examples" / "sample_meeting.json").read_text(encoding="utf-8"))
    return Meeting.model_validate(data)
