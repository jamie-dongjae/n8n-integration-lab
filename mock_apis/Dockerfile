FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY pyproject.toml .
COPY mock_apis ./mock_apis
COPY scripts ./scripts

RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir "fastapi>=0.115,<1.0" "uvicorn[standard]>=0.30,<1.0" \
                                 "pydantic>=2.7,<3.0" "python-dateutil>=2.9" "httpx>=0.27,<1.0"

EXPOSE 8000
CMD ["uvicorn", "mock_apis.main:app", "--host", "0.0.0.0", "--port", "8000"]
