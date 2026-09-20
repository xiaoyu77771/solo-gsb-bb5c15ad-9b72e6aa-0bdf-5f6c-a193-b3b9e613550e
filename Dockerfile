FROM python:3.12-slim

# 不写 .pyc、日志直出
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/review.db

WORKDIR /app

# 先单独安装依赖，利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码
COPY app ./app

# SQLite 数据文件持久化目录（由应用在启动时自动建库初始化）
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 单条命令启动：uvicorn 的 lifespan 钩子会自动完成建表、默认设置与演示数据初始化
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
