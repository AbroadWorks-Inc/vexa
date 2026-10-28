FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY exporter/ exporter/
RUN pip install --no-cache-dir .
USER 65534
EXPOSE 8080
CMD ["python", "-m", "exporter"]
