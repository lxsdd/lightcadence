FROM python:3.13.18-slim-bookworm
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY engine/requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt
COPY engine /app/engine
USER 10001:10001
CMD ["python", "-m", "engine.lightcadence_engine.server"]
