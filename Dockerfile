FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml ./
COPY invest_agent ./invest_agent
RUN pip install . \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin agent \
    && mkdir -p /app/data && chown agent /app/data

USER 10001
CMD ["python", "-m", "invest_agent.bot"]
