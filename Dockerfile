FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 1000 athan \
    && useradd -u 1000 -g 1000 -m -s /bin/sh athan

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY athan_caster.py /app/athan_caster.py
COPY volumes.json /app/volumes.json

RUN mkdir -p /app/audio \
    && chown -R athan:athan /app

USER athan

EXPOSE 8089

ENTRYPOINT ["python", "-u", "/app/athan_caster.py"]
