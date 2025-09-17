# 🔇 Noise Reduction Service

[![FastAPI](https://img.shields.io/badge/FastAPI-0.95.1-009688.svg?style=flat&logo=FastAPI)](https://fastapi.tiangolo.com)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.0.40-purple.svg?style=flat)](https://github.com/langchain-ai/langgraph)
[![Python](https://img.shields.io/badge/Python-3.11-blue.svg?style=flat&logo=python)](https://python.org)
[![Docker](https://img.shields.io/badge/Docker-Ready-blue.svg?style=flat&logo=docker)](https://docker.com)
[![AWS ECS](https://img.shields.io/badge/AWS-ECS%20Ready-orange.svg?style=flat&logo=amazon-aws)](https://aws.amazon.com/ecs/)

Advanced noise reduction service powered by LangGraph workflows that intelligently filters duplicate alerts, low-priority notifications, and noise from the alert stream.

## 🎯 Purpose

- **Filters duplicate alerts** using time-window and content-based detection
- **Reduces low-priority noise** based on severity and business rules
- **Workflow orchestration** using LangGraph for complex decision logic
- **Forwards clean alerts** to the event correlation service
- **Maintains alert context** throughout the filtering process

## 🧠 LangGraph Workflow

### Workflow Nodes
1. **noise_reduction** - Main filtering logic (duplicate detection, priority filtering)
2. **send_to_correlation** - Forward filtered alerts to correlation service

## 🚀 Quick Start

### Local Development
```bash
pip install -r requirements.txt
uvicorn noise_reduction_agent:app --host 0.0.0.0 --port 8003 --reload
```

### Docker
```bash
docker build -t noise-reduction:latest .
docker run -p 8003:8003 -e ENVIRONMENT=development noise-reduction:latest
```

## 📋 API Endpoints

- `GET /health` - Health check
- `POST /reduce-noise` - Process alerts through noise reduction workflow

## ⚙️ Environment Variables

- `ENVIRONMENT` - development/production
- `CORRELATION_API_URL` - URL of correlation service
- `TIME_WINDOW_MINUTES` - Duplicate detection time window (default: 5)

## 🔗 Dependencies

- FastAPI 0.95.1 - Web framework
- LangGraph 0.0.40 - Workflow orchestration
- LangChain-core 0.1.46 - LangChain components
- NumPy 1.24.3 - Numerical operations

## 📞 Support

Create an issue in this repository or contact the BugRaid AI team.
