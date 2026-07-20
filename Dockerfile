FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY slack ./slack

RUN groupadd --gid 10001 swingengine \
    && useradd --uid 10001 --gid swingengine --no-create-home swingengine \
    && chown -R swingengine:swingengine /app

USER 10001:10001

CMD ["python", "main.py"]
