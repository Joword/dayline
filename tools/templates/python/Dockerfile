FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY server.py .

# Dayline runs tools as an arbitrary non-root user on a read-only filesystem.
USER 65532:65532
EXPOSE 8080
CMD ["python", "server.py"]
