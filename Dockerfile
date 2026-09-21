FROM python:3.12-slim

# LibreOffice（Luckysheet 保存時の数式キャッシュ焼き込み用）と git（デモリセットの版復元用）
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-calc-nogui git ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# デモリセットで fixtures/excel を戻せるように、コンテナ内でも git として扱う
RUN git init -q && git add -A && git -c user.email=build@local -c user.name=build commit -qm "image" || true

ENV PORT=8080 DB_PATH=/srv/data/app.db PYTHONUNBUFFERED=1 TZ=Asia/Tokyo
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
