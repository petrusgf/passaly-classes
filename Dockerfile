FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py models.py memory.py ./
COPY static static
RUN useradd -m app && mkdir /data && chown app /data
USER app
ENV DATABASE_URL=sqlite+aiosqlite:////data/classes.db FILES_DIR=/data/files
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8010"]
