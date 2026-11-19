# 주간 진척 보고 에이전트 API 서버 이미지
# 비밀 정보(.env)는 이미지에 넣지 않는다. 실행 시 주입한다:
#   docker build -t weekly-progress-agent .
#   docker run --env-file .env -p 8000:8000 weekly-progress-agent
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8000"]
