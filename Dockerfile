FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default entrypoint — overridden per-service in docker-compose.yml
CMD ["python", "governance_ui.py"]
