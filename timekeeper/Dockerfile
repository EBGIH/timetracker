FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY seed_demo.py pytest.ini ./

# Run as a non-root user.
RUN useradd --create-home --uid 10001 timekeeper && chown -R timekeeper /app
USER timekeeper

EXPOSE 8000

# In a multi-instance deployment set TK_ENABLE_SCHEDULER=0 and drive
# POST /api/admin/run-batch from a single external scheduler instead.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
