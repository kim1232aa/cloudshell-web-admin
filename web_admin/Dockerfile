FROM google/cloud-sdk:slim

RUN pip install --no-cache-dir --break-system-packages pysocks==1.7.1

WORKDIR /app
COPY web_admin/*.py ./
COPY web_admin/*.html ./
COPY web_admin/static ./static

ENV STATE_DIR=/state \
    PYTHONUNBUFFERED=1 \
    PORT=8080

EXPOSE 8080
CMD ["python3", "app.py"]
