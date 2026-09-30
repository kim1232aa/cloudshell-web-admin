FROM google/cloud-sdk:slim

RUN pip install --no-cache-dir --break-system-packages pysocks==1.7.1

WORKDIR /app
COPY web_admin/*.py ./
COPY web_admin/*.html ./
COPY web_admin/static ./static

# Package cloudshell deployment scripts and patches into container image
RUN mkdir -p /app/cloudshell
COPY install.sh install-residential.sh supervise.sh subserver.py cf-optimize-refresh.sh front-domains.txt res-domains.txt /app/cloudshell/
COPY kui-patches /app/cloudshell/kui-patches

ENV STATE_DIR=/state \
    CLOUDSHELL_SCRIPTS_DIR=/app/cloudshell \
    PYTHONUNBUFFERED=1 \
    PORT=8080

EXPOSE 8080
CMD ["python3", "app.py"]
