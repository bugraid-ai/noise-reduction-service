import os, pytest
from noise_reduction_agent import NoiseReductionAgent

@pytest.fixture
def agent():
    os.environ["ENVIRONMENT"] = "development"
    return NoiseReductionAgent(config={"min_severity_threshold": 2})

def test_apply_severity_filtering(agent):
    alerts = [
        {"id": "1", "priority": "critical"},
        {"id": "2", "priority": "low"},
    ]
    kept, suppressed = agent.apply_severity_filtering(alerts)
    assert len(kept) == 1
    assert len(suppressed) == 1
    assert suppressed[0]["suppression_reason"] == "low_severity"
