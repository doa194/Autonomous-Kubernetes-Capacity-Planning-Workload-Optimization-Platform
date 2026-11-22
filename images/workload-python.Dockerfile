# W03-W06 Python demonstration workloads. Minimal dependencies (prometheus-client, PyJWT)
# so the memory baseline stays small and predictable. Runs as non-root UID 10001 with a
# read-only root filesystem (only /tmp is writable, via an emptyDir).
FROM python:3.13-slim
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin lab \
 && pip install --no-cache-dir prometheus-client==0.23.1 "pyjwt[crypto]==2.10.1"
COPY --chown=10001:10001 workloads/python /app
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001
