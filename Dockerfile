# 인증키·파이썬 버전 없이 바로 뜨는 것이 기본값(USE_MOCK=1).
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app app
COPY sql sql
COPY scripts scripts

RUN mkdir -p /app/data/events && chown -R nobody:nogroup /app/data
VOLUME ["/app/data"]

USER nobody
ENV USE_MOCK=1 PYTHONUNBUFFERED=1
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
