FROM python:3.12-slim
RUN pip install --no-cache-dir prometheus-exporter-celery==0.11.2 celery==5.6.3 kombu==5.6.2
USER 65534:65534
ENTRYPOINT ["python", "-c", "from src.cli import cli; cli()"]
