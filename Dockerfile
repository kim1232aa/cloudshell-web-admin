FROM google/cloud-sdk:slim
# base image already ships curl, openssh-client and gcloud — nothing to install

COPY watchdog.sh docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/watchdog.sh /usr/local/bin/docker-entrypoint.sh

# everything persistent lives in /state (mounted volume)
ENV CLOUDSDK_CONFIG=/state/gcloud \
    STATE_DIR=/state \
    TUNNEL_HOST= \
    INTERVAL=60 \
    PROBE_PROXY= \
    PROBE_RETRIES=4 \
    KEEPALIVE=1 \
    KEEPALIVE_INTERVAL=300

VOLUME /state
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["--loop"]
