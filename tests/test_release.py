"""فاز ۹: چیزهایی که *قبل از* ریلیز باید درست باشند — و امروز با چشم چک می‌شدند.

هر تست در این فایل یک بی‌نظمیِ واقعی را می‌بندد که در فازهای ۰ تا ۸ آزار داد:

* نسخه در دو جا (و دو جا یکی نباشد) → «📊 وضعیت» روی سایت عددِ دیگری می‌گوید؛
* `tisa-product-importer.zip` ترک‌شده بدون سورس → معلوم نیست کدام PHP در آن است؛
* promise در README بدون کد (و برعکس) → کاربر چیزی را دنبال می‌کند که وجود ندارد؛
* `docker-compose.yml` با نامِ متغیری که هیچ‌جا خوانده نمی‌شود → کانتینر بالا می‌آید
  و بی‌صدا چیزی را از `.env` نمی‌گیرد؛
* کفِ پوششِ CI عددی آرزومندانه → یک ریفکتورِ تمیز «سبز» می‌شود در حالی که تست‌ها
  نصفه‌اند.

همه آفلاین‌اند و با کتابخانهٔ استاندارد می‌دوند (فقط `zipfile`/`re`).
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

REPO = Path(__file__).resolve().parents[1]
PLAN = REPO / "docs" / "archive" / "CODE-REVIEW-AND-UPGRADE-PLAN.md"
CHANGELOG = REPO / "CHANGELOG.md"
README = REPO / "README.md"
ENV_EXAMPLE = REPO / ".env.example"
COMPOSE = REPO / "docker-compose.yml"
CI = REPO / ".github" / "workflows" / "ci.yml"
CONTRACT_WORKFLOW = REPO / ".github" / "workflows" / "contract.yml"
PR_TEMPLATE = REPO / ".github" / "pull_request_template.md"
PLUGIN_DIR = REPO / "plugin" / "tisa-product-importer"
PLUGIN_ZIP = REPO / "tisa-product-importer.zip"
BUILD_SCRIPT = REPO / "scripts" / "build_plugin_zip.py"
REQUIREMENTS = REPO / "requirements.txt"
REQUIREMENTS_DEV = REPO / "requirements-dev.txt"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


#: بلوک کد markdown — با "".join ساخته می‌شود تا داخل رشته‌های تست گیج‌کننده نباشد.
_FENCE = "`" * 3


@unittest.skipUnless(CHANGELOG.exists() and PLAN.exists(), "مستندات رپو پیدا نشد")
class TestVersionIsStatedOnce(unittest.TestCase):
    """نسخه یکی است: کد، CHANGELOG و `main.py --version`."""

    def test_code_and_changelog_agree(self) -> None:
        import bot

        heading = re.search(r"^## (\d+\.\d+\.\d+)", read(CHANGELOG), re.M)
        self.assertIsNotNone(heading, "CHANGELOG باید با `## X.Y.Z — …` شروع شود")
        self.assertEqual(bot.__version__, heading.group(1), "نسخۀ کد و CHANGELOG فرق دارد (docs/RELEASING.md)")

    def test_cli_prints_the_same_version(self) -> None:
        import bot

        out = subprocess.run(
            [sys.executable, "main.py", "--version"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONPATH": str(REPO)},
        ).stdout.strip()
        self.assertEqual(bot.__version__, out, "main.py --version منبعِ دیگری برای نسخه است")

    def test_pyproject_reads_the_version_from_the_code(self) -> None:
        """نسخه فقط در ``bot/__init__.py`` نوشته می‌شود؛ pyproject باید همان را بخواند."""
        text = read(REPO / "pyproject.toml")
        self.assertIn('dynamic = ["version"]', text)
        self.assertIn('version = {attr = "bot.__version__"}', text)
        self.assertNotRegex(text, r'(?m)^version = "', "نسخه دوباره در pyproject دستی نوشته شده")

    def test_the_release_section_names_the_phase_it_closes(self) -> None:
        """بندِ بالای CHANGELOG باید بگوید کدام فاز برنامه بسته شده — برای کسی که
        روی سرور فقط `git log` را می‌بیند، همین سرنخ است."""
        import bot

        section = re.search(
            rf"^## {re.escape(bot.__version__)} — (.+)$", read(CHANGELOG), re.M
        )
        self.assertIsNotNone(section, "سرآمد نسخۀ جاری توضیحِ فارسی ندارد")
        self.assertRegex(section.group(1), r"فاز\s*[۰-۹0-9]+", "سرآمد باید «فاز …» را نام ببرد")

    def test_every_shipped_phase_has_a_status_block(self) -> None:
        """فاز ۱ تا ۹ هر کدام «وضعیت اجرا (فاز …)» دارند؛ فاز ۰ نه، چون آیتم‌هایش
        در جدول P0 همان‌جا خط‌خورده‌اند (تک‌مبدأِ وضعیت، نه کپیِ جدول)."""
        text = read(PLAN)
        for phase in "۱۲۳۴۵۶۷۸۹":
            marker = f"وضعیت اجرا (فاز {phase}"
            # assertTrue و نه assertIn: پیامِ assertIn کل فایل را در لاگ می‌ریزد و
            # خواندنِ شکست را غیرممکن می‌کند.
            self.assertTrue(
                marker in text,
                f"{marker}…) نیست — خواننده نمی‌فهمد این فاز انجام شده یا نه",
            )


class TestPluginIsBuiltFromSource(unittest.TestCase):
    """`plugin/` سورس است و زیپ خروجی؛ زیپِ بی‌سورس یعنی «نمی‌دانم چه روی سایت است»."""

    def test_the_zip_is_exactly_the_build_of_the_source(self) -> None:
        sys.path.insert(0, str(REPO / "scripts"))
        try:
            import build_plugin_zip as builder
        finally:
            sys.path.pop(0)
        built = builder.build_bytes()
        self.assertEqual(
            builder.fingerprints(read_zip(PLUGIN_ZIP)),
            builder.fingerprints(built),
            "زیپ بازنساخته نشده: `python scripts/build_plugin_zip.py` را بزن (docs/RELEASING.md)",
        )
        self.assertEqual(built, builder.build_bytes(), "بیلد قطعیت ندارد (تاریخ/ترتیبِ رکوردها؟)")

    def test_the_declared_plugin_version_is_the_one_the_bot_checks(self) -> None:
        from bot.services import importer_contract

        php = (PLUGIN_DIR / "tisa-product-importer.php").read_text(encoding="utf-8")
        declared = re.search(r"^ \* Version: ([0-9.]+)", php, re.M)
        self.assertIsNotNone(declared, "افزونه `Version:` اعلام‌شده ندارد؛ «🩺 عیب‌یابی» نمی‌تواند قضاوت کند")
        self.assertEqual(importer_contract.read_version(PLUGIN_ZIP), tuple(
            int(part) for part in declared.group(1).split(".")
        ), "نسخۀ فایلِ سورس با زیپی که مالک آپلود می‌کند فرق دارد")

    def test_a_drifted_source_is_caught_by_the_check_flag(self) -> None:
        """--check باید فریاد بزند، نه اینکه لبخند بزند."""
        import shutil
        import tempfile

        sys.path.insert(0, str(REPO / "scripts"))
        try:
            import build_plugin_zip as builder
        finally:
            sys.path.pop(0)

        tmp = Path(tempfile.mkdtemp(prefix="tisa-plugin-drift-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        root = tmp / "plugin"
        shutil.copytree(PLUGIN_DIR, root / "tisa-product-importer")
        target = tmp / "out.zip"
        check = ["--check", "--out", str(target), "--root", str(root)]
        def run_check(*, expect: str, where: str = "out") -> int:
            """`main` جمله‌ای هم چاپ می‌کند — همان جمله‌ای که اپراتور در لاگِ CI می‌بیند.
            پس تست هم کدِ خروج و هم متن را می‌سنجد، و لاگِ سوئیت با چاپ‌های اضافه
            شلوغ نمی‌شود. «❌…» روی stderr چاپ می‌شود (خطا یعنی خطا، نه پیشرفت)."""
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = builder.main(check)
            self.assertIn(expect, out.getvalue() if where == "out" else err.getvalue(),
                          f"جملۀ «{expect}» در خروجیِ --check نبود")
            return code

        target.write_bytes(builder.build_bytes(root))
        self.assertEqual(0, run_check(expect="✅"))

        php = root / "tisa-product-importer" / "tisa-product-importer.php"
        before = php.read_text(encoding="utf-8")
        try:
            php.write_text(before + "\n// drift\n", encoding="utf-8")
            self.assertEqual(
                1,
                run_check(expect="قدیمی است", where="err"),
                "بازنویسیِ PHP و زیپِ کهنه باید خطا بدهد",
            )
        finally:
            php.write_text(before, encoding="utf-8")  # بازگرداندنِ بایت‌به‌بایت، نه replace
        self.assertEqual(0, run_check(expect="✅"), "وقتی اختلاف پاک شد، --check باید ✅ بدهد")

    def test_the_zip_contains_nothing_but_the_plugin(self) -> None:
        with zipfile.ZipFile(PLUGIN_ZIP) as archive:
            names = archive.namelist()
        self.assertTrue(all(name.startswith("tisa-product-importer/") for name in names), names)
        self.assertIn("tisa-product-importer/tisa-product-importer.php", names)
        self.assertIn("tisa-product-importer/README.txt", names)
        for name in names:
            self.assertNotIn("..", name, "مسیر نسبیِ خطرناک در زیپ")
        # یک ورودی پوشهٔ ریشه مجاز است (همان چیزی که وردپرس موقع نصب می‌سازد)؛
        # هیچ رکورد دیگری نباید بیرون از پوشهٔ افزونه یا به‌شکل پوشه باشد.
        directories = [name for name in names if name.endswith("/")]
        self.assertEqual([f"{PLUGIN_DIR.name}/"], directories)


class TestToolingFilesExist(unittest.TestCase):
    """هر چیزی که مستندات اسمش را می‌برند، باید وجود داشته باشد و کار کند."""

    def test_release_and_contract_docs_exist_and_are_persian(self) -> None:
        for path in (
            REPO / "docs" / "RELEASING.md",
            REPO / "docs" / "CONTRACT-TESTS.md",
            REPO / "docs" / "MANUAL-TEST-CHECKLIST.md",
            PR_TEMPLATE,
        ):
            self.assertTrue(path.exists(), f"{path.relative_to(REPO)} نیست")
            text = read(path)
            self.assertGreater(len(text), 800, f"{path.name} فقط تیتر است")
            self.assertTrue(re.search("[\u0600-\u06FF]", text), f"{path.name} فارسی نیست")
        for path in (REPO / "docs" / "RELEASING.md", REPO / "docs" / "CONTRACT-TESTS.md"):
            # راهنمای عملیاتی باید دستور قابل‌کپی داشته باشد؛ قالب PR چک‌لیست است.
            self.assertIn(_FENCE, read(path), f"{path.name} بلوک دستور ندارد")

    #: نام‌هایی که بیرونِ پوشه هم معنا دارند؛ بقیهٔ قول‌ها باید با مسیر نوشته شوند.
    ROOT_LEVEL = ("docker-compose.yml", "requirements-dev.txt", "requirements.txt", "CHANGELOG.md", "README.md")

    def test_every_path_referenced_by_the_docs_exists(self) -> None:
        """نامِ فایل در مستندات = قول. اگر README و چک‌لیست بگوید «برو `x/y.py` را ببین»
        و آن فایل نباشد، خواننده وسطِ عیب‌یابی گیر می‌کند؛ پس مسیرهای repo‌ای که در
        `README.md`، چک‌لیست، راهنمای قرارداد و workflow آمده‌اند بررسی می‌شوند.
        فایل‌های زمانِ اجرا (`data/*.json`) قولِ repo نیستند و کنار گذاشته می‌شوند."""
        referenced = set()
        docs = (
            COMPOSE,
            CONTRACT_WORKFLOW,
            REPO / "docs" / "CONTRACT-TESTS.md",
            REPO / "docs" / "MANUAL-TEST-CHECKLIST.md",
            REPO / "README.md",
        )
        for path in docs:
            text = read(path)
            referenced.update(
                re.findall(r"(?:^|\s|`|\()((?:bot|deploy|docs|scripts|tests|plugin)/[\w./-]+)", text)
            )
            referenced.update(name for name in self.ROOT_LEVEL if f"`{name}`" in text)
        for name in sorted(referenced):
            clean = name.rstrip(".,:")
            if clean.endswith((".md", ".yml", ".py", ".sh", ".txt", ".json", ".php")):
                self.assertTrue((REPO / clean).exists(), f"{clean} در مستندات هست، در repo نیست")

    def test_the_yaml_files_parse(self) -> None:
        """`ci.yml` یک‌بار در *repo* بود که parse نمی‌شد: GitHub رانی به نام
        `.github/workflows/ci.yml` ساخت، بدون لاگ و بدون تست — و تنها نشانه‌اش یک ❌
        بی‌متن بود. دلیلش یک رشته‌ی چندنفرهٔ shell بود که یک خطش به ستون صفر افتاده بود.
        پس قبل از هر چیز: هر فایل YAMLِ زیرساخت باید parse شود و هر step یک mapping
        با `name` و (`run` یا `uses`) باشد."""
        try:
            import yaml
        except Exception:  # pragma: no cover - pyyaml در requirements-dev است
            self.skipTest("pyyaml نصب نیست")

        files = [
            REPO / ".github" / "workflows" / "ci.yml",
            REPO / ".github" / "workflows" / "contract.yml",
            REPO / "docker-compose.yml",
        ]
        for path in files:
            raw = read(path)
            try:
                doc = yaml.safe_load(raw)
            except yaml.YAMLError as exc:  # همان شکلی که GitHub فقط «❌» نشان می‌دهد
                self.fail(f"{path.relative_to(REPO)} parse نشد: {exc}")
            self.assertIsInstance(doc, dict, f"{path.name} خالی/اسکالر است")
            for job, body in (doc.get("jobs") or {}).items():
                self.assertIn("runs-on", body, f"job «{job}» runs-on ندارد")
                for step in body.get("steps", []):
                    self.assertIsInstance(step, dict, f"step ناقص در «{job}»: {step!r}")
                    self.assertTrue(
                        step.get("run") or step.get("uses"),
                        f"step بدون run/uses در «{job}»: {step.get('name')}",
                    )
    def test_the_compose_file_only_uses_env_names_the_test_reads(self) -> None:
        """اگر compose متغیری بدهد که تست نمی‌خواند، یا برعکس — کانتینر بی‌صدا ناقص است."""
        compose_text = read(COMPOSE)
        contract_text = read(REPO / "tests" / "test_contract_wordpress.py")
        given = set(re.findall(r"^      (TISA_[A-Z0-9_]+|BOT_TOKEN|SUDO_IDS):", compose_text, re.M))
        read_by_test = (
            set(re.findall(r"os\.getenv\(\"([A-Z0-9_]+)\"", contract_text))
            | set(re.findall(r"os\.environ\[(?:\"|\')([A-Z0-9_]+)(?:\"|\')\]", contract_text))
            | {"TISA_CONTRACT"}
        )
        self.assertTrue(given, "compose هیچ env به کانتینر تست نمی‌دهد")
        self.assertEqual(
            set(),
            given - read_by_test,
            "compose متغیری می‌دهد که هیچ‌جا خوانده نمی‌شود",
        )
        self.assertEqual(
            set(),
            {name for name in read_by_test if name.startswith("TISA_TEST_")} - given,
            "تست متغیری می‌خواهد که compose نمی‌دهد",
        )

    def test_the_bootstrap_script_is_safe_bash(self) -> None:
        text = read(REPO / "deploy" / "bootstrap-wordpress.sh")
        self.assertTrue(text.startswith("#!/usr/bin/env bash"), "shebang ندارد")
        self.assertIn("set -euo pipefail", text, "اسکریپتِ بدون set -e نیمه‌کاره تمام می‌شود و ✅ می‌دهد")
        self.assertIn(".env.contract", text, "جای کلیدها باید در خودش گفته شود")
        self.assertTrue(os.access(REPO / "deploy" / "bootstrap-wordpress.sh", os.X_OK), "قابل‌اجرا نیست (chmod +x)")


class TestRequirementsSplit(unittest.TestCase):
    """`requirements.txt` لاغر می‌ماند؛ ابزار تست در requirements-dev.txt است."""

    DEV_TOOLS = ("pytest", "pytest-cov", "ruff", "mypy", "hypothesis")

    def test_dev_tools_are_declared_once(self) -> None:
        dev = read(REQUIREMENTS_DEV)
        runtime = read(REQUIREMENTS)
        for tool in self.DEV_TOOLS:
            self.assertIn(tool, dev, f"{tool} در requirements-dev.txt نیست")
            self.assertNotIn(f"{tool}\n", runtime, f"{tool} به requirements.txt رفته — deploy سنگین می‌شود")
            self.assertNotIn(f"{tool}>", runtime, f"{tool} به requirements.txt رفته — deploy سنگین می‌شود")

    def test_ci_installs_from_the_files_not_from_inline_versions(self) -> None:
        text = read(CI)
        self.assertEqual(0, text.count('pip install "ruff'), "CI هنوز ruff را inline نصب می‌کند (دو منبع)")
        self.assertEqual(0, text.count("pip install \"mypy"), "CI هنوز mypy را inline نصب می‌کند")
        for job in re.split(r"\n(?=  [a-z_]+:\n)", text):
            if "pip install" in job:
                self.assertIn("requirements-dev.txt", job, "قدم CI ابزار را از requirements-dev.txt نمی‌گیرد")

    def test_coverage_floors_are_the_ones_the_plan_asked_for(self) -> None:
        text = read(CI)
        self.assertIn("--cov=bot --cov-report=term-missing --cov-fail-under=75", text)
        self.assertIn("--cov=bot/services --cov-report=term --cov-fail-under=85", text)
        self.assertNotIn("--cov-fail-under=50", text, "کف ۵۵→۷۵ برنامه بود؛ ۵۰ یعنی کفِ فاز ۰ جا مانده")

    def test_the_plugin_zip_check_runs_in_ci(self) -> None:
        self.assertIn("build_plugin_zip.py --check", read(CI), "درزی‌سنجیِ زیپ در CI نیست")
        self.assertIn("scripts", read(CI).split("ruff check")[1].split("\n")[0], "ruff روی scripts اجرا نمی‌شود")


@unittest.skipUnless(README.exists(), "README نیست")
class TestWorkDirectoriesAreConfigured(unittest.TestCase):
    """ریشهٔ کارِ هر جریان باید از ``settings.temp_dir`` بیاید، نه یک مسیرِ /tmp ثابت."""

    def test_every_flow_work_root_lives_under_the_configured_temp_dir(self) -> None:
        from bot.config import settings
        from bot.modules import image_compress, product_flow, tracking_converter

        for module in (image_compress, product_flow, tracking_converter):
            self.assertEqual(
                settings.temp_dir,
                Path(module.TEMP_DIR).parent,
                f"{module.__name__} مسیرِ /tmp ثابت دارد",
            )

    def test_the_setting_is_documented_and_configurable(self) -> None:
        text = read(REPO / "bot" / "config.py")
        self.assertIn('"TISA_TEMP_DIR"', text, "کلید خوانده نمی‌شود")
        self.assertIn("TISA_TEMP_DIR=", read(ENV_EXAMPLE))


class TestDocsMatchTheTools(unittest.TestCase):
    """README قول نمی‌دهد جز آنچه هست — و برعکس."""

    def test_readme_documents_every_new_entry_point(self) -> None:
        text = read(README)
        for needle in (
            "tests/test_properties.py",
            "tests/test_extraction_corpus.py",
            "requirements-dev.txt",
            "scripts/build_plugin_zip.py",
            "plugin/tisa-product-importer",
            "docs/RELEASING.md",
            "docs/CONTRACT-TESTS.md",
            "docker-compose.yml",
            "--cov-fail-under",
        ):
            self.assertTrue(needle in text, f"README دربارهٔ «{needle}» حرفی نمی‌زند")

    def test_env_example_and_readme_agree_on_the_documented_keys(self) -> None:
        keys = set(re.findall(r"^([A-Z][A-Z0-9_]{2,})=", read(ENV_EXAMPLE), re.M))
        text = read(README)
        for key in ("PRICE_MIN", "PRICE_MAX", "MAX_ROWS", "MAX_FILE_MB", "VERBOSE_LOG", "TISA_DATA_DIR",
                 "TISA_TEMP_DIR", "TISA_BACKUP_DIR", "TISA_BACKUP_KEEP", "TISA_DAILY_REPORT_HOUR",
                 "TISA_DRY_RUN"):
            self.assertTrue(key in keys, f"{key} در .env.example نیست")
            self.assertTrue(f"`{key}`" in text, f"{key} در README توضیح ندارد")

    def test_the_pull_request_template_carries_the_plan_dod(self) -> None:
        text = read(PR_TEMPLATE)
        for needle in ("تست رگرسیون", "README", "CHANGELOG", "corpus", "build_plugin_zip"):
            self.assertTrue(needle in text, f"DoDِ قالب PR این بند را ندارد: {needle}")


def read_zip(path: Path) -> bytes:
    return path.read_bytes()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
