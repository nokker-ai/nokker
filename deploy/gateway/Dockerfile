# Relais de la passerelle nokker.app (docs/GATEWAY.md). Sans état sur disque ; derrière le tunnel Cloudflare.
FROM python:3.13-slim
RUN useradd -r -u 10001 relay
WORKDIR /opt/relay
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY relay.py .
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8080
USER relay
EXPOSE 8080
CMD ["python3", "relay.py"]
