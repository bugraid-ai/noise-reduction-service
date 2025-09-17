# Noise Reduction Service

FastAPI service with LangGraph that filters duplicate and low-priority alerts, then forwards to the Event Correlation service.

## Files Structure
```
noise-reduction-service/
├── README.md
├── requirements.txt (from ecs-deployment/requirements/requirements-noise-reduction.txt)
├── Dockerfile (from ecs-deployment/docker/Dockerfile.noise-reduction)
├── noise_reduction_agent.py (from ecs-deployment/services/noise_reduction_agent.py)
├── task-definition.json (from ecs-deployment/infrastructure/task-definitions/noise-reduction-task.json)
├── environment-config.json (from ecs-deployment/update-noise-reduction-env.json)
└── deploy.sh (deployment script)
```

## Deployment
```bash
./deploy.sh
```

## Environment Variables
- `ENVIRONMENT`: development/production
- `CORRELATION_API_URL`: URL of correlation service
- `AWS_DEFAULT_REGION`: AWS region

## Port
- Service runs on port **8003**
- Health check: `GET /health`
- Main endpoint: `POST /reduce-noise`
