import os, pytest
from noise_reduction_agent import NoiseReductionAgent

@pytest.fixture
def agent():
    os.environ["ENVIRONMENT"] = "development"
    return NoiseReductionAgent()

def test_group_alerts_by_company(agent):
    alerts = [
        {"id": "a", "company_id": "c1"},
        {"id": "b", "company_id": "c2"},
        {"id": "c", "company_id": "c1"},
    ]
    grouped = agent.group_alerts_by_company(alerts)
    assert "c1" in grouped and len(grouped["c1"]) == 2
    assert "c2" in grouped and len(grouped["c2"]) == 1
