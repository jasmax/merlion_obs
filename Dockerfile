FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRACES_DB=/data/traces.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Runtime code only; tests, evals and practice scripts stay out (see .dockerignore).
COPY agent.py data.py guardrails.py llm.py redact.py tracer.py server.py prices.json \
     auth.py metrics.py alerts.py build_dashboard.py ./
# The release-gate result the dashboard shows for this build.
COPY evals/results/latest.json evals/results/latest.json

# Traces go to /data. On Railway, attach a volume at /data or they are lost on every redeploy.
RUN mkdir -p /data

CMD ["python", "server.py"]
