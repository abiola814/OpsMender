FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY opsmender ./opsmender
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
LABEL org.opencontainers.image.title="OpsMender" \
      org.opencontainers.image.description="Lightweight, open-source AI DevOps companion for Coolify" \
      org.opencontainers.image.licenses="Apache-2.0"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OPSMENDER_DATA_DIR=/data \
    PORT=8000
COPY --from=build /install /usr/local
RUN useradd --system --uid 10001 --home-dir /data opsmender \
    && mkdir -p /data && chown opsmender /data
USER opsmender
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/healthz', timeout=4)"
CMD ["python", "-m", "opsmender"]
