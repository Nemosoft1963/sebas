FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libreoffice-calc fonts-noto-cjk && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
RUN python -m pip install --no-cache-dir --upgrade "pip>=26.1.2"
RUN pip install --no-cache-dir "fastapi>=0.116,<1" "uvicorn[standard]>=0.35,<1" "httpx>=0.27,<1" "numpy>=1.26,<3" "faster-whisper>=1.2,<2" "python-multipart>=0.0.20,<1" "pypdf>=5,<7" "pypdfium2>=4,<6" "pillow>=10,<13" "openpyxl==3.1.5"
RUN python -m pip install --no-cache-dir --upgrade "setuptools>=78.1.1" "msgpack>=1.2.1"
RUN rm -rf /usr/local/lib/python3.12/site-packages/setuptools-70.3.0.dist-info /usr/local/lib/python3.12/site-packages/msgpack-1.1.2.dist-info \
    && python -c "from importlib.metadata import version; assert tuple(map(int, version('setuptools').split('.')[:2])) >= (78, 1); assert tuple(map(int, version('msgpack').split('.')[:2])) >= (1, 2)"
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"
ARG INSTALL_EXPERIENCE_RAG=0
RUN if [ "$INSTALL_EXPERIENCE_RAG" = "1" ]; then pip install --no-cache-dir "langchain-chroma==1.1.0" "langchain-ollama==1.1.0" "chromadb==1.5.9"; fi
# Runtime package installation is unsupported; remove installers and pip's vendored libraries.
RUN python -m pip uninstall -y pip setuptools \
    && rm -rf /usr/local/lib/python3.12/site-packages/pip* /usr/local/lib/python3.12/site-packages/setuptools*
COPY app ./app
RUN mkdir -p /data/memory /data/logs
EXPOSE 8000
CMD ["uvicorn","app.web:app","--host","0.0.0.0","--port","8000"]
