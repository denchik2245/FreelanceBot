FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir "playwright>=1.50,<2"
RUN python -m playwright install --with-deps chromium

COPY src ./src
COPY config ./config
COPY certs ./certs
RUN pip install --no-cache-dir .

RUN mkdir -p /app/data
CMD ["freelance-bot"]
