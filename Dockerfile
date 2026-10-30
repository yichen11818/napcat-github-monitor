FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY services services
COPY handlers handlers
ENV HOST=0.0.0.0
EXPOSE 8088
CMD ["python", "app.py"]
