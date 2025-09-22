import os, pytest
from datetime import datetime, timedelta
from noise_reduction_agent import NoiseReductionAgent

@pytest.fixture
def agent():
    os.environ["ENVIRONMENT"] = "development"
    return NoiseReductionAgent()

def test_is_duplicate_alert_title_time(agent):
    now = datetime.utcnow()
    alert1 = {"id": "1", "title": "Memory usage high", "company_id": "comp1", "service": "compute", "parsed_time": now}
    alert2 = {"id": "2", "title": "Memory usage high", "company_id": "comp1", "service": "compute", "parsed_time": now + timedelta(seconds=60)}
    assert agent.is_duplicate_alert(alert1, alert2) is True
