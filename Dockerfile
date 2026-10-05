# TisaPostToWP — ایمیج ربات.
#
# برای محلی و CI است؛ روی سرورِ خودت supervisor/systemd ساده‌تر است (تصویرِ
# تازه‌build‌شده یعنی باید هر بار deploy هم بدهی). داده‌ها در volume می‌نشینند:
# /var/lib/tisaposttowp نقش همان TISA_DATA_DIR را بازی می‌کند، پس با حذف کانتینر
# تاریخچهٔ SKU، دفترها و صف ارسال از دست نمی‌روند.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TISA_DATA_DIR=/var/lib/tisaposttowp

WORKDIR /app

# بدون apt و build-essential: pandas / Pillow / pymupdf / PTB همه manylinux wheel
# دارند، پس لایهٔ کامپایل فقط سایز و زمان build را زیاد می‌کرد.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=10001:10001 main.py pyproject.toml tisa-product-importer.zip ./
COPY --chown=10001:10001 bot ./bot

RUN useradd --uid 10001 --create-home tisa \
    && mkdir -p /var/lib/tisaposttowp /app/logs \
    && chown -R tisa:tisa /var/lib/tisaposttowp /app

USER tisa

# همان دریچه‌ای که CI می‌گذراند: با .env ناقص (SUDO_IDS خالی، دیسکِ نوشتنی نبودن)
# کانتینر «سالم» بالا نمی‌آید — healthcheck قرمز می‌شود و `docker ps` آن را نشان می‌دهد.
# --build-app همهٔ هندلرها را می‌سازد ولی هیچ درخواستِ تلگرامی/ووکامرس نمی‌زند.
HEALTHCHECK --interval=60s --timeout=25s --start-period=20s --retries=3 \
    CMD ["python", "main.py", "--build-app"]

VOLUME ["/var/lib/tisaposttowp"]

CMD ["python", "main.py"]
