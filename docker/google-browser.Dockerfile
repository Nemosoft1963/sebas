ARG CPTR_TAG=latest
FROM ghcr.io/open-webui/computer:${CPTR_TAG}

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        fonts-noto-cjk \
        fonts-noto-color-emoji \
        xauth \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY --chown=cptr:cptr docker/google_browser_bootstrap.py /usr/local/lib/local-cowork/google_browser_bootstrap.py
COPY docker/patch_cptr_chromium.py /usr/local/lib/local-cowork/patch_cptr_chromium.py
COPY docker/google-sites-extension /opt/local-supporter-google-sites
COPY docker/google-chrome-wrapper.sh /usr/local/bin/google-chrome

USER root
RUN chmod 0755 /usr/local/bin/google-chrome \
    && /home/cptr/.venv/bin/python /usr/local/lib/local-cowork/patch_cptr_chromium.py

USER cptr
CMD ["sh", "-c", "python /usr/local/lib/local-cowork/google_browser_bootstrap.py && exec xvfb-run -n 99 -s '-screen 0 1024x720x24 -nolisten tcp -ac' cptr run --host 0.0.0.0 --port 8000 --headless"]
