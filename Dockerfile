FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SWINGENGINE_FILES_DIR=/var/lib/swingengine/files

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY database ./database
COPY slack ./slack
COPY upstox ./upstox

RUN groupadd --gid 10001 swingengine \
    && useradd --uid 10001 --gid swingengine --no-create-home swingengine \
    && mkdir -p \
        /var/lib/swingengine/files/input \
        /var/lib/swingengine/files/output \
    && chown -R swingengine:swingengine /app /var/lib/swingengine

USER 10001:10001

CMD ["python", "main.py"]
