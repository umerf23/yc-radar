FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first, so this layer stays cached when only app code changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a normal user, not root, and give it ownership of the data directory
RUN useradd --create-home --uid 1000 radar \
    && mkdir -p /data \
    && chown -R radar:radar /app /data

USER radar

EXPOSE 8000

CMD ["python", "-m", "app.main"]
