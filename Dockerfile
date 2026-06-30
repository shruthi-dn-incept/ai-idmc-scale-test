FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default: scale test runner (ACA Job entrypoint).
# Override in docker-compose.yml per service (governance_ui.py, etc.)
CMD ["python", "run_scale_test.py"]
