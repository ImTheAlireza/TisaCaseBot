# انتشار (Release) — شماره‌گذاری، بیلد، تگ

## قاعدهٔ شماره‌گذاری (SemVer)

پروژه هنوز `0.x` است، پس **MINOR** جای شکستن و اضافه‌ی هر دو را می‌گیرد:

| چه چیزی عوض می‌شود | کدام عدد | مثال واقعی |
|---|---|---|
| بازگشتِ ناسازگار در داده، `.env`، `product.json` یا مسیر استخراج | `0.MINOR.0` | ۰.۱۱ (سیاست واحدِ مبلغ)، ۰.۱۵ (کارتِ واحدِ محصول) |
| فقط رفع اشکال، بدون تغییر قرارداد | `0.MINOR.PATCH` | اصلاحِ «قیمت ویژه قیمت اصلی را می‌بلعد» |
| اضافهٔ سازگار (دکمه/صفحهٔ تازه، شمارندهٔ تازه) | `0.MINOR.0` تا وقتی ۰.x هستیم | — |

وقتی `1.0.0` زده شد (یعنی قرارداد `product.json` و مسیر انتشار ثابت شد)، SemVer
کلاسیک جاری می‌شود: MAJOR برای شکست، MINOR برای قابلیت، PATCH برای رفع اشکال.

**نسخه فقط یک‌جا نوشته می‌شود: `bot/__init__.py`.** `main.py --version` و
`/start` همان را می‌خوانند؛ `tests/test_release.py` می‌گیرد اگر `CHANGELOG.md`
سرِ دیگری حرف بزند.

## چک‌لیست انتشار

۱. روی `main` باش و تازه‌اش کن:

   ```bash
   git checkout main && git pull
   ```

۲. درِ سبزِ تست‌ها (هر چهارتا باید صفر برگردانند):

   ```bash
   pip install -r requirements-dev.txt          # یک‌بار برای pytest/ruff/mypy/hypothesis
   pytest -q
   ruff check bot tests main.py scripts
   mypy
   python main.py --check-config
   ```

۳. اگر `plugin/tisa-product-importer/*.php` دست‌خورده: زیپ را **بازبساز** (زیپ، همان
   فایلی است که مالک سایت آپلود می‌کند؛ ترک‌شده نگه داشته می‌شود تا نصبِ سایت با
   `importer_contract.py` قابل‌سنجش باشد):

   ```bash
   python scripts/build_plugin_zip.py
   python scripts/build_plugin_zip.py --check    # باید ✅ بدهد
   git add tisa-product-importer.zip
   ```

   `Version:` را در رأس همان فایل PHP بالا ببر — وگرنه `python main.py --check-config`
   می‌گوید افزونه کهنه است (آستانهٔ فیلدهای قیمت در
   `importer_contract.MIN_VERSION_FOR_PRICE_FIELDS`).

۴. نسخه و `CHANGELOG.md`:

   - `bot/__init__.py` → `__version__ = "X.Y.Z"`
   - بالای `CHANGELOG.md` یک بند `## X.Y.Z — <خلاصۀ فارسی>` اضافه کن؛ بندها از همين
     چک‌ليست نوشته می‌شوند، پس «Added / Fixed / Changed» را هم از متنِ همان فازها بردار.

۵. یک `git commit` با پیام نسخه (مثل `0.16.0 — فاز ۹: …`)، بعد تگ و انتشار:

   ```bash
   git commit -m "0.16.0 — فاز ۹: …"
   git tag v0.16.0
   git push origin main --follow-tags
   gh release create v0.16.0 tisa-product-importer.zip \
     --title "0.16.0 — فاز ۹: تست و انتشار" \
     --notes "$(sed -n '/^## 0\.16\.0/,/^## /p' CHANGELOG.md | sed '1d;$d')"
   ```

   زیپ افزونه هم **به‌عنوان فایل ضمیمهٔ ریلیز** می‌چسبد؛ این راهِ رسیدنِ نسخهٔ درست به
   دستِ کسی که به repo دسترسی ندارد.

۶. روی سرور (همان مسیرِ همیشگی، `docs/runbook.md` §۱):

   ```bash
   cd /srv/tisaposttowp && git pull && source .venv/bin/activate
   pip install -r requirements.txt
   supervisorctl restart tisabot
   ```

۷. اگر نسخهٔ WooCommerce/WordPressِ فروشگاه عوض شده، یا مسیر نوشتنِ محصول دست‌خورده:
   workflow تست قرارداد را روی رانر خودساخته بزن:

   ```bash
   gh workflow run contract.yml            # گزینه‌ها اختیاری‌اند
   gh run watch "$(gh run list --workflow contract.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
   ```

   و اگر محلی Docker داری، همان چیزی که workflow می‌زند را خودت اجرا کن
   (`docs/CONTRACT-TESTS.md`).

## چه چیزی در ریلیز نمی‌آید

- `data/` و `logs/` (روی سرور می‌مانند؛ `TISA_DATA_DIR` را در `.env` چک کن);
- `tests/` و `docs/` داخل ایمیج داکر نمی‌روند (`.dockerignore`) — برای همین تست
  قرارداد در کانتینرِ جدائی می‌دود که repo را mount می‌کند;
- کلیدها: هیچ `ck_…`/`cs_…`/توکنی commit نمی‌شود و در گزارش خطا هم نمی‌آید
  (`bot/utils/logging.py` ردپای اعتبارنامه را پاک می‌کند).

## اگر چیزی خراب از آب درآمد

ریلیزِ `0.x` را **اصلاح** نکن، بعدی را بزن (`0.16.1`) و در CHANGELOG بنویس چه چیزی
برگشت — `git revert` روی `main` امن‌تر از force-push به تگ است. اگر فقط زیپ افزونه
خراب بود، ریلیزِ تازه لازم نیست: فایل ضمیمه را با `gh release upload --clobber` عوض کن
و در وردپرس نسخهٔ تازه را ببین.
