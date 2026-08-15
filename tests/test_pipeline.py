import json
from pathlib import Path
from datetime import datetime, timezone
from src.main import dedupe, score_item

def test_scoring_fixture():
    p = Path(__file__).parent / "fixtures" / "sample.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    items = dedupe(data["items"])
    assert len(items) == 2
    scored = [score_item(x, datetime.now(timezone.utc)) for x in items]
    assert all(0 <= x["scores"]["total"] <= 100 for x in scored)
    assert max(x["scores"]["marketing_relevance"] for x in scored) > 0
