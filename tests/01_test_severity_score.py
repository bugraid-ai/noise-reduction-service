import os, pytest
from noise_reduction_agent import NoiseReductionAgent

@pytest.fixture
def agent():
    os.environ["ENVIRONMENT"] = "development"
    return NoiseReductionAgent(config={"min_severity_threshold": 2})

def test_calculate_alert_severity_score_high(agent):
    alert = {"priority": "critical", "impact": "high", "urgency": "high"}
    score = agent.calculate_alert_severity_score(alert)
    assert score >= 5
