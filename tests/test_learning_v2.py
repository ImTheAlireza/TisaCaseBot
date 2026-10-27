"""فاز ۶: یادگیری خودکار ۲.۰ — یک قاعده اول پیشنهاد است، نه قانون (plan §فاز ۶).

Four layers, each tested where its decision is made:

* :mod:`bot.services.learning` — the v2 model (``status``/``scope``/``origin``/
  ``applied_to``), the ``version: 1`` migration, and the gates that keep an
  unconfirmed rule from touching a price or a word;
* :mod:`bot.services.learning_corpus` + :mod:`bot.services.learning_impact` — the
  rehearsal that says «روی ۷ محصول آخر، ۲۳ واریژن کمتر می‌کرد» before the owner is
  asked to approve anything;
* :mod:`bot.modules.learning_panel` — the screens and the buttons that answer them
  (a promise without a button is a bug);
* :func:`bot.modules.product_flow.analyze` — the read-only probe that proves a rule
  fires on a real text without creating anything.

Run with ``python -m pytest tests/test_learning_v2.py`` or
``python3 -m unittest discover -s tests``; like the other flow tests, the panel
half skips itself when python-telegram-bot is not installed.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SUDO_IDS", "1234567")

import _flow_harness as h
from bot.services import learning, learning_corpus, learning_impact

try:
    from bot.constants import CB
    from bot.modules import learning_panel

    HAS_FLOW = True
except Exception:                          # pragma: no cover - PTB missing
    CB = learning_panel = None   # type: ignore[assignment]
    HAS_FLOW = False

try:  # the flow and the parser tests need httpx as well (product_extractor)
    from bot.modules import product_flow
    from bot.services import postmodel as ev
    from bot.services import product_extractor

    HAS_PARSER = True
except Exception:                          # pragma: no cover - httpx missing
    product_flow = ev = product_extractor = None          # type: ignore[assignment]
    HAS_PARSER = False

needs_flow = unittest.skipUnless(HAS_FLOW, "python-telegram-bot is not installed")
needs_parser = unittest.skipUnless(
    HAS_FLOW and HAS_PARSER, "python-telegram-bot or httpx is not installed"
)

SUDO = 1234567
OTHER = 7


def answers(seen: list) -> list[str]:
    """The toast/alert texts a query was answered with, in order."""
    return [str(item[1] or "") for item in seen if item[0] == "answer"]


def term_rule(wrong: str, right: str, **over: object) -> learning.Rule:
    return learning.Rule(kind="term", key=wrong, value=right, example=f"{wrong} ← {right}", **over)  # type: ignore[arg-type]


class Snapshot:
    """A ProductData-shaped stand-in: the corpus reads a dict, nothing else."""

    def __init__(self, **payload: object) -> None:
        self._payload = payload

    def to_dict(self) -> dict:
        return dict(self._payload)


class IsolatedMemory(unittest.TestCase):
    """Temp file for both the rules and the replay corpus, like test_learning."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        data_dir = Path(self._tmp.name)
        learning.DATA_DIR = data_dir
        learning.LEARNED_FILE = data_dir / "learned.json"
        learning_corpus.CORPUS_FILE = data_dir / "learning_corpus.json"
        learning._CACHE = None
        learning._CACHE_KEY = None
        learning._APPLIED_QUEUE.clear()

    def tearDown(self) -> None:
        learning._CACHE = None
        learning._CACHE_KEY = None
        learning._APPLIED_QUEUE.clear()

    def reload(self) -> learning.Memory:
        """Force a real read from disk, bypassing the (path, mtime) cache."""
        learning._CACHE = None
        learning._CACHE_KEY = None
        return learning.load()

    def learn(self, rule: learning.Rule, *, origin: str = "") -> learning.Rule:
        """Remember and activate a rule — what the owner does after the preview."""
        learning.remember(rule, learning.Correction(field="title", old=rule.key, new=rule.value),
                          origin=origin)
        learning.confirm_rule(rule.rule_id)
        return rule


# ---------------------------------------------------------------------------
# The lifecycle: propose -> preview -> confirm
# ---------------------------------------------------------------------------

class TestRuleLifecycle(IsolatedMemory):
    def test_new_price_rule_is_pending_and_scales_nothing(self):
        rule = learning.Rule(kind="price_scale", key="4", value="1000")
        learning.remember(rule, learning.Correction(field="price", old="1098", new="1098000"))
        self.assertEqual(rule.status, learning.STATUS_PENDING)
        self.assertEqual(learning.price_multiplier(4, False), 1)
        learning.confirm_rule(rule.rule_id)
        self.assertEqual(learning.price_multiplier(4, False), 1000)

    def test_new_term_rule_rewrites_nothing_until_confirmed(self):
        rule = term_rule("سلفی", "مشکی")
        learning.remember(rule, learning.Correction(field="attributes", old="سلفی", new="مشکی"))
        self.assertEqual(learning.apply_terms("قاب سلفی"), "قاب سلفی")
        learning.confirm_rule(rule.rule_id)
        self.assertEqual(learning.apply_terms("قاب سلفی"), "قاب مشکی")

    def test_disable_is_reversible_and_does_not_delete(self):
        rule = self.learn(term_rule("سلفی", "مشکی"))
        learning.disable_rule(rule.rule_id)
        self.assertEqual(learning.apply_terms("قاب سلفی"), "قاب سلفی")
        self.assertIsNotNone(learning.get_rule(rule.rule_id))
        learning.enable_rule(rule.rule_id)
        self.assertEqual(learning.apply_terms("قاب سلفی"), "قاب مشکی")

    def test_pending_and_active_counts_are_separate(self):
        learning.remember(term_rule("a", "b"), learning.Correction(field="title", old="a", new="b"))
        learning.remember(term_rule("c", "d"), learning.Correction(field="title", old="c", new="d"))
        learning.confirm_rule("term:c")
        counts = learning.count_by_status()
        self.assertEqual(counts[learning.STATUS_PENDING], 1)
        self.assertEqual(counts[learning.STATUS_ACTIVE], 1)
        self.assertEqual([r.key for r in learning.pending_rules()], ["a"])

    def test_changing_the_meaning_needs_a_new_confirmation(self):
        rule = self.learn(term_rule("سلفی", "مشکی"))
        self.assertEqual(learning.get_rule(rule.rule_id).status, learning.STATUS_ACTIVE)
        learning.remember(
            term_rule("سلفی", "سفید"), learning.Correction(field="attributes", old="سلفی", new="سفید")
        )
        # Same id, different promise: it is a proposal again, not a law.
        self.assertEqual(learning.get_rule("term:سلفی").status, learning.STATUS_PENDING)
        self.assertEqual(learning.apply_terms("قاب سلفی"), "قاب سلفی")

    def test_re_learning_the_same_mapping_keeps_the_trust(self):
        rule = self.learn(term_rule("سلفی", "مشکی"))
        learning.remember(
            term_rule("سلفی", "مشکی"), learning.Correction(field="attributes", old="سلفی", new="مشکی")
        )
        self.assertEqual(learning.get_rule(rule.rule_id).status, learning.STATUS_ACTIVE)

    def test_revision_changes_when_a_rule_is_confirmed(self):
        rule = term_rule("سلفی", "مشکی")
        learning.remember(rule, learning.Correction(field="attributes", old="سلفی", new="مشکی"))
        before = learning.revision()
        learning.confirm_rule(rule.rule_id)
        # The product flow caches an extraction by (text, revision): a status flip
        # that left the token alone would keep the old, wrong parse in play.
        self.assertNotEqual(learning.revision(), before)

    def test_status_and_examples_survive_a_restart(self):
        rule = self.learn(term_rule("سلفی", "مشکی"))
        self.reload()
        stored = learning.get_rule(rule.rule_id)
        self.assertEqual(stored.status, learning.STATUS_ACTIVE)
        self.assertEqual(learning.apply_terms("قاب سلفی"), "قاب مشکی")


class TestApplicationExamples(IsolatedMemory):
    def test_examples_are_recorded_without_a_disk_write(self):
        rule = self.learn(term_rule("سلفی", "مشکی"))
        learning.note_application(rule.rule_id, title="قاب آیفون", effect="رنگ: سلفی ← مشکی")
        # Queued in memory: the hot path must not write the file per product…
        self.assertEqual(len(learning.get_rule(rule.rule_id).applied_to), 0)
        self.assertEqual(learning.applied_examples(rule)[-1]["title"], "قاب آیفون")
        # …and the next write of any kind carries them to disk.
        learning.note_application("no-such-rule", title="x")     # must not raise
        learning.disable_rule(rule.rule_id)          # any rule change is the write
        self.assertEqual(self.reload().rules[rule.rule_id].applied_to[-1]["effect"],
                         "رنگ: سلفی ← مشکی")

    @needs_parser
    def test_a_rule_that_fires_while_parsing_leaves_an_example(self):
        # The production hook: the extractor, not a test, is what records that a
        # rule touched a real product.
        import asyncio

        from bot.modules import product_flow

        rule = self.learn(term_rule("سبز", "سفید"))
        asyncio.run(product_flow.analyze("قاب گوشی سبز"))
        example = learning.applied_examples(rule)[-1]
        self.assertEqual(example["effect"], "بازنویسی واژه اعمال شد")
        self.assertIn("سفید", example["title"])

    def test_the_example_list_is_bounded(self):
        rule = self.learn(term_rule("سلفی", "مشکی"))
        for index in range(12):
            learning.note_application(rule.rule_id, title=f"محصول {index}")
        learning.disable_rule(rule.rule_id)             # any write flushes the queue
        rule = learning.get_rule(rule.rule_id)
        self.assertLessEqual(len(rule.applied_to), learning._MAX_APPLIED)
        self.assertEqual(rule.applied_to[-1]["title"], "محصول 11")


# ---------------------------------------------------------------------------
# Storage format: v1 -> v2
# ---------------------------------------------------------------------------

class TestMemoryMigration(IsolatedMemory):
    V1 = {
        "version": 1,
        "rules": [
            {"kind": "term", "key": "سلفی", "value": "مشکی", "example": "e",
             "hits": 3, "created": 1000.0},
        ],
        "corrections": [{"field": "title", "old": "a", "new": "b", "rule": "", "created": 9.0}],
    }

    def write(self, payload: dict) -> None:
        learning.LEARNED_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.reload()

    def test_v1_rules_stay_active(self):
        self.write(self.V1)
        rule = learning.get_rule("term:سلفی")
        self.assertEqual(rule.status, learning.STATUS_ACTIVE)
        self.assertEqual(rule.scope, learning.SCOPE_SHOP)
        self.assertEqual(rule.hits, 3)
        self.assertEqual(rule.applied_to, [])
        # …and they still do their job, so upgrading does not quietly un-teach
        # the shop the corrections it has been relying on.
        self.assertEqual(learning.apply_terms("قاب سلفی"), "قاب مشکی")

    def test_v1_corrections_are_readable(self):
        self.write(self.V1)
        self.assertEqual(len(learning.recent_corrections()), 1)

    def test_unknown_status_is_treated_as_active_not_dropped(self):
        self.write({**self.V1, "rules": [{**self.V1["rules"][0], "status": "galaxy-state"}]})
        self.assertEqual(learning.get_rule("term:سلفی").status, learning.STATUS_ACTIVE)

    def test_a_saved_v2_file_carries_the_new_fields(self):
        self.learn(term_rule("سلفی", "مشکی"), origin="قاب ایفون")
        payload = json.loads(learning.LEARNED_FILE.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], learning.MEMORY_VERSION)
        saved = payload["rules"][0]
        self.assertEqual(saved["status"], learning.STATUS_ACTIVE)
        self.assertEqual(saved["origin"], "قاب ایفون")
        self.assertEqual(saved["scope"], learning.SCOPE_SHOP)


# ---------------------------------------------------------------------------
# Scope: a rule may be limited to the family it was learned from
# ---------------------------------------------------------------------------

class TestScope(IsolatedMemory):
    def test_origin_is_recorded_at_learn_time(self):
        rule = term_rule("سبز", "سفید")
        learning.remember(rule, learning.Correction(field="attributes", old="سبز", new="سفید"),
                          origin="ایفون ۱۳")
        self.assertEqual(learning.get_rule(rule.rule_id).origin, "ایفون ۱۳")

    def test_fold_is_the_shared_normalizer(self):
        # scope matching and origin picking must agree on what "the same word" means
        self.assertEqual(learning.fold("  آیفون\u200c ۱۷ "), "آیفون 17")
        self.assertIn(learning.fold("iPhone 13"), learning.fold("iphone  13"))

    def test_narrowing_then_widening(self):
        rule = self.learn(term_rule("سبز", "سفید"), origin="ایفون ۱۳")
        self.assertTrue(rule.matches("قاب ایفون ۱۳ مشکی"))          # shop-wide: anywhere
        learning.toggle_scope(rule.rule_id)
        scoped = learning.get_rule(rule.rule_id)
        self.assertEqual(scoped.keyword, "ایفون ۱۳")
        self.assertTrue(scoped.matches("قاب ایفون ۱۳ مشکی"))
        self.assertFalse(scoped.matches("قاب شیائومی ۱۳ مشکی"))
        learning.toggle_scope(rule.rule_id)
        self.assertEqual(learning.get_rule(rule.rule_id).scope, learning.SCOPE_SHOP)

    def test_a_scoped_term_rule_is_skipped_on_other_products(self):
        rule = self.learn(term_rule("سبز", "سفید"), origin="ایفون")
        learning.toggle_scope(rule.rule_id)
        self.assertEqual(learning.apply_terms("قاب سبز", where="قاب شیائومی"), "قاب سبز")
        self.assertEqual(learning.apply_terms("قاب سبز", where="قاب ایفون ۱۳"), "قاب سفید")

    def test_a_scoped_price_rule_is_skipped_on_other_lines(self):
        rule = learning.Rule(kind="price_scale", key="4", value="1000")
        learning.remember(rule, learning.Correction(field="price", old="1098", new="1098000"),
                          origin="ایفون")
        learning.confirm_rule(rule.rule_id)
        learning.toggle_scope(rule.rule_id)
        self.assertEqual(learning.price_multiplier(4, False, where="شارژر 1098"), 1)
        self.assertEqual(learning.price_multiplier(4, False, where="قاب ایفون 1098"), 1000)

    def test_the_ai_prompt_sees_only_in_scope_rules(self):
        self.learn(term_rule("سبز", "سفید"), origin="ایفون")
        learning.toggle_scope("term:سبز")
        self.assertIn("سبز", learning.rules_for_prompt("قاب ایفون سبز"))
        self.assertEqual(learning.rules_for_prompt("قاب شیائومی"), "")

    def test_a_rule_without_origin_cannot_be_narrowed(self):
        rule = self.learn(term_rule("سبز", "سفید"))
        self.assertIsNone(learning.toggle_scope(rule.rule_id))
        self.assertEqual(learning.get_rule(rule.rule_id).scope, learning.SCOPE_SHOP)

    def test_suspension_ignores_every_rule(self):
        self.learn(learning.Rule(kind="price_scale", key="4", value="1000"))
        self.assertEqual(learning.price_multiplier(4, False), 1000)
        with learning.suspended():
            self.assertEqual(learning.price_multiplier(4, False), 1)
            self.assertEqual(learning.rules_for_prompt("anything"), "")
        self.assertEqual(learning.price_multiplier(4, False), 1000)


# ---------------------------------------------------------------------------
# The replay corpus
# ---------------------------------------------------------------------------

class TestCorpus(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.file = Path(self._tmp.name) / "learning_corpus.json"
        learning_corpus.CORPUS_FILE = self.file

    def test_round_trip(self):
        learning_corpus.record("قیمت 1098", Snapshot(title="قاب ایفون", price=1098,
                                                     models=["iPhone 13"],
                                                     attributes={"رنگ": ["مشکی"]},
                                                     categories=["قاب > ایفون"],
                                                     variation_count=2))
        entries = learning_corpus.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], "قاب ایفون")
        self.assertEqual(entries[0]["attributes"], {"رنگ": ["مشکی"]})
        self.assertIn("1098", entries[0]["text"])

    def test_empty_products_are_not_recorded(self):
        learning_corpus.record("", Snapshot())
        self.assertEqual(learning_corpus.count(), 0)

    def test_the_ring_is_bounded_and_keeps_the_newest(self):
        for index in range(learning_corpus.MAX_ENTRIES + 5):
            learning_corpus.record(f"متن {index}", Snapshot(title=f"محصول {index}"))
        entries = learning_corpus.entries()
        self.assertEqual(len(entries), learning_corpus.MAX_ENTRIES)
        self.assertEqual(entries[-1]["title"], f"محصول {learning_corpus.MAX_ENTRIES + 4}")
        self.assertEqual(entries[0]["title"], "محصول 5")

    def test_clear_leaves_the_rules_alone(self):
        learning_corpus.record("x", Snapshot(title="الف"))
        learning_corpus.clear()
        self.assertEqual(learning_corpus.count(), 0)

    def test_re_editing_one_product_updates_the_snapshot_instead_of_stacking(self):
        # Six extractions of one draft must stay one corpus entry: the preview is
        # about "my recent products", not "how many times I changed my mind".
        learning_corpus.record("قیمت 1098", Snapshot(title="قاب ایفون", price=1098))
        learning_corpus.record("قیمت 1298", Snapshot(title="قاب ایفون", price=1298))
        entries = learning_corpus.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["price"], 1298)
        # …and a different product is a new entry, not an overwrite.
        learning_corpus.record("قیمت 698000", Snapshot(title="قاب شیائومی", price=698000))
        self.assertEqual([e["title"] for e in learning_corpus.entries()],
                         ["قاب ایفون", "قاب شیائومی"])

    def test_the_variation_count_is_the_card_number(self):
        # ``ProductData.variation_count`` is only filled when the publish plan is
        # built, so copying it would store 0 for every product read in a chat.
        # The corpus counts with ``bot.services.plan`` — the builder behind the
        # card — and keeps the colour limits that decision needs.
        learning_corpus.record(
            "قاب ایفون رنگ ها سبز/بنفش/مشکی",
            Snapshot(
                title="قاب ایفون",
                models=["iPhone 13", "iPhone 14"],
                attributes={"رنگ": ["سبز", "بنفش", "مشکی"]},
                model_colors={"iPhone 13": ["سبز", "بنفش"]},
                variation_count=0,
            ),
        )
        entry = learning_corpus.entries()[0]
        self.assertEqual(entry["variation_count"], 5)  # 6 minus the colour 13 lacks
        self.assertEqual(entry["model_colors"], {"iPhone 13": ["سبز", "بنفش"]})

    def test_text_is_clipped(self):
        learning_corpus.record("ک" * 9000, Snapshot(title="بلند"))
        self.assertLessEqual(len(learning_corpus.entries()[0]["text"]), learning_corpus.MAX_TEXT_CHARS)


# ---------------------------------------------------------------------------
# The impact preview
# ---------------------------------------------------------------------------

class TestImpact(unittest.TestCase):
    ENTRIES = [
        {
            "title": "قاب ایفون ۱۳",
            "text": "قاب ایفون ۱۳ رنگ ها سبز و سفید و مشکی\nقیمت 1098",
            "price": 1098,
            "models": ["iPhone 13", "iPhone 14"],
            "attributes": {"رنگ": ["سبز", "سفید", "مشکی"]},
            "variation_count": 6,
        },
        {
            "title": "قاب گلکسی",
            "text": "قاب گلکسی سبز",
            "price": 0,
            "models": ["S24"],
            "attributes": {"رنگ": ["سبز", "مشکی"]},
            "variation_count": 2,
        },
    ]

    def test_a_collapsing_rule_is_shown_as_removing_variations(self):
        impact = learning_impact.project(term_rule("سبز", "سفید"), self.ENTRIES)
        self.assertEqual(impact.products, 2)
        # The first product has 2 models × 3 colors (6) and «سبز» folds into the
        # «سفید» it already had → 2 × 2 (4). The second product's colors do not
        # collide, so it only gets rewritten, not shortened: -2 in total.
        self.assertEqual(impact.variation_delta, -2)
        self.assertTrue(impact.dangerous)
        self.assertIn("2 واریژن کمتر می‌شد", impact.summary())
        self.assertTrue(any(change.field == "رنگ" for change in impact.changes))

    def test_a_colour_the_model_never_sold_is_not_a_loss(self):
        # iPhone 14 has no «سبز» in stock to begin with, so folding it into
        # «بنفش» takes one variation off the shop, not two. A preview that
        # over-counts the damage trains the owner to ignore the warning.
        entries = [
            {
                "title": "قاب ایفون",
                "text": "قاب ایفون سبز بنفش مشکی",
                "price": 0,
                "models": ["iPhone 13", "iPhone 14"],
                "attributes": {"رنگ": ["سبز", "بنفش", "مشکی"]},
                "model_colors": {"iPhone 13": ["سبز", "بنفش"], "iPhone 14": ["بنفش", "مشکی"]},
                "variation_count": 4,
            }
        ]
        impact = learning_impact.project(term_rule("سبز", "بنفش"), entries)
        self.assertEqual(impact.variation_delta, -1)
        self.assertTrue(impact.dangerous)

    def test_a_rule_that_matches_nothing_says_so(self):
        impact = learning_impact.project(term_rule("پرتقال", "مرغ"), self.ENTRIES)
        self.assertFalse(impact.touched)
        self.assertFalse(impact.dangerous)
        self.assertIn("هیچ چیزی را عوض نمی‌کرد", impact.summary())

    def test_an_empty_corpus_is_admitted_instead_of_guessed(self):
        impact = learning_impact.project(term_rule("سبز", "سفید"), [])
        self.assertTrue(impact.blind)
        self.assertIn("محصول تازه‌ای در حافظه نیست", impact.summary())

    def test_price_rules_are_projected_on_the_bare_amount(self):
        impact = learning_impact.project(
            learning.Rule(kind="price_scale", key="4", value="1000"), self.ENTRIES
        )
        self.assertEqual(impact.price_changes, 1)          # only the 1098 product
        self.assertIn("قیمت 1 محصول عوض می‌شد", impact.summary())
        change = impact.changes[0]
        self.assertEqual((change.before, change.after), ("1,098", "1,098,000"))

    def test_the_product_that_taught_a_price_rule_still_counts(self):
        # A correction re-extracts the same product, so its snapshot in the corpus
        # is already fixed: price 1098000 with «قیمت 1098» still in its text.
        # Telling the owner «هیچ چیزی را عوض نمی‌کرد» about the very draft that
        # proves the rule throws the useful half of the preview away.
        entries = [
            {
                "title": "قاب ایفون ۱۳",
                "text": "قیمت 1098\nقیمت 1098000 تومان",
                "price": 1098000,
                "models": [],
                "attributes": {},
            }
        ]
        impact = learning_impact.project(
            learning.Rule(kind="price_scale", key="4", value="1000"), entries
        )
        self.assertEqual(impact.price_changes, 1)
        self.assertEqual(
            (impact.changes[0].before, impact.changes[0].after), ("1,098", "1,098,000")
        )
        self.assertIn("قیمت 1 محصول عوض می‌شد", impact.summary())

    def test_an_absurd_scaled_price_is_flagged(self):
        impact = learning_impact.project(
            learning.Rule(kind="price_scale", key="4", value=10 ** 9), self.ENTRIES
        )
        self.assertEqual(impact.price_out_of_range, 1)
        self.assertTrue(impact.dangerous)
        self.assertIn("از حد مجاز بیرون می‌زد", impact.summary())

    def test_a_scoped_rule_is_replayed_only_on_matching_products(self):
        rule = term_rule("سبز", "سفید")
        rule.scope = f"{learning.SCOPE_WORD}ایفون"
        impact = learning_impact.project(rule, self.ENTRIES)
        self.assertEqual({change.product for change in impact.changes}, {"«قاب ایفون ۱۳»"})

    def test_render_lists_examples_and_the_warning(self):
        text = learning_impact.project(term_rule("سبز", "سفید"), self.ENTRIES).render()
        self.assertIn("• «قاب ایفون ۱۳»", text)
        self.assertIn("🛑", text)

    def test_a_rule_of_an_unknown_kind_is_not_pretended_to_be_rehearsed(self):
        impact = learning_impact.project(
            learning.Rule(kind="vibes", key="a", value="b"), self.ENTRIES
        )
        self.assertEqual(impact.products, len(self.ENTRIES))
        self.assertFalse(impact.touched)

    def test_the_projector_and_the_parser_share_the_matcher(self):
        # A preview must not use its own matching: a word inside a longer word is
        # untouched in both.
        entries = [{"title": "Airskin case", "text": "Airskin case", "price": 0,
                    "models": [], "attributes": {}, "variation_count": 0}]
        impact = learning_impact.project(term_rule("Air skin", "Airskin"), entries)
        self.assertEqual(impact.changes, ())


# ---------------------------------------------------------------------------
# The panel: screens and buttons (needs python-telegram-bot)
# ---------------------------------------------------------------------------

@needs_flow
class TestLearningPanel(IsolatedMemory):
    def pending_rule(self) -> learning.Rule:
        rule = term_rule("سبز", "سفید")
        learning.remember(rule, learning.Correction(field="attributes", old="سبز", new="سفید"),
                          origin="ایفون")
        learning_corpus.record(
            "قاب ایفون سبز",
            Snapshot(title="قاب ایفون", attributes={"رنگ": ["سبز", "سفید", "مشکی"]},
                     models=["ایفون ۱۳", "ایفون ۱۴"]),
        )
        return rule

    def test_pending_screen_shows_the_rehearsal_not_just_the_rule(self):
        self.pending_rule()
        text = learning_panel._pending_text()
        self.assertIn("سبز", text)
        self.assertIn("تا تو تأیید نکنی", text)
        # The scary number is in the message, not behind a button.
        self.assertIn("2 واریژن کمتر می‌شد", text)

    def test_pending_screen_buttons_are_confirm_and_forget(self):
        rule = self.pending_rule()
        rows = learning_panel._pending_keyboard().inline_keyboard
        sid = learning.short_id(rule.rule_id)
        self.assertEqual(rows[0][0].callback_data, f"{learning_panel._CONFIRM_PREFIX}{sid}")
        self.assertEqual(rows[0][1].callback_data, f"{CB.LEARNING_DELETE}:{sid}")

    def test_confirm_button_activates_the_rule(self):
        import asyncio

        rule = self.pending_rule()
        update, seen = h.query_update(
            f"{learning_panel._CONFIRM_PREFIX}{learning.short_id(rule.rule_id)}", user_id=SUDO
        )
        asyncio.run(learning_panel.cb_confirm(update, h.context()))
        self.assertEqual(learning.get_rule(rule.rule_id).status, learning.STATUS_ACTIVE)
        self.assertTrue(any("✅ فعال شد" in text for text in answers(seen)), answers(seen))

    def test_a_dead_button_says_so_instead_of_failing_silently(self):
        import asyncio

        update, seen = h.query_update(f"{learning_panel._CONFIRM_PREFIX}00000000", user_id=SUDO)
        asyncio.run(learning_panel.cb_confirm(update, h.context()))
        self.assertIn("⚠️ این قاعده دیگر وجود ندارد.", answers(seen))
        # …and a screen is shown instead of leaving a dead card behind.
        self.assertTrue(any(item[0] in ("edit", "text") for item in seen))

    def test_a_tap_on_the_product_preview_survives_the_memory_screen(self):
        # «⏳ در انتظار تأیید» sits on the review card. Editing that message would
        # destroy a half-built product the owner still has to confirm, so the
        # panel only edits its own screens.
        import asyncio

        self.pending_rule()
        update, seen = h.query_update(CB.LEARNING_PENDING, user_id=SUDO)
        update.callback_query.message.text = "🧾 پیش‌نمایش محصول …"
        asyncio.run(learning_panel.cb_pending(update, h.context()))
        kinds = [item[0] for item in seen]
        self.assertIn("text", kinds)
        self.assertNotIn("edit", kinds)

    def test_the_panel_screen_is_edited_in_place(self):
        import asyncio

        self.pending_rule()
        update, seen = h.query_update(CB.LEARNING_PENDING, user_id=SUDO)
        update.callback_query.message.text = "🧠 <b>یادگیری‌ها</b>"
        asyncio.run(learning_panel.cb_pending(update, h.context()))
        self.assertIn("edit", [item[0] for item in seen])

    def test_admins_cannot_touch_the_memory(self):
        import asyncio

        rule = self.pending_rule()
        update, seen = h.query_update(
            f"{learning_panel._CONFIRM_PREFIX}{learning.short_id(rule.rule_id)}", user_id=OTHER
        )
        asyncio.run(learning_panel.cb_confirm(update, h.context()))
        self.assertIn("⛔ فقط مالک (سودو) به یادگیری‌ها دسترسی دارد.", answers(seen))
        # An admin must be *told*, in a dialog they cannot miss — not silently
        # left with a button that does nothing.
        self.assertEqual(seen[0][2].get("show_alert"), True)
        self.assertEqual(learning.get_rule(rule.rule_id).status, learning.STATUS_PENDING)

    def test_scope_button_narrows_the_rule(self):
        import asyncio

        rule = self.pending_rule()
        learning.confirm_rule(rule.rule_id)
        update, _ = h.query_update(
            f"{learning_panel._SCOPE_PREFIX}{learning.short_id(rule.rule_id)}", user_id=SUDO
        )
        asyncio.run(learning_panel.cb_scope(update, h.context()))
        self.assertEqual(learning.get_rule(rule.rule_id).keyword, "ایفون")

    def test_rules_list_shows_status_and_pending_shortcut(self):
        self.pending_rule()                                  # left pending on purpose
        self.learn(term_rule("آبی", "سرمه‌ای"))                # confirmed by learn()
        self.learn(term_rule("قرمز", "صورتی"))
        learning.disable_rule("term:قرمز")                   # …and switched off again
        text = learning_panel._rules_text()
        self.assertIn("1 قاعدهٔ فعال", text)
        self.assertIn("⏳ 1 در انتظار تأیید", text)
        self.assertIn("⏸ 1 غیرفعال", text)
        rows = learning_panel._rules_keyboard().inline_keyboard
        self.assertEqual(rows[0][0].callback_data, CB.LEARNING_PENDING)
        labels = [button.callback_data for row in rows for button in row]
        self.assertTrue(any(data.startswith(learning_panel._ENABLE_PREFIX) for data in labels))

    def test_every_button_on_the_screen_has_a_handler(self):
        class FakeApp:
            def __init__(self) -> None:
                self.patterns: list[str] = []

            def add_handler(self, handler) -> None:
                self.patterns.append(str(handler.pattern))

        app = FakeApp()
        learning_panel.register(app)
        joined = "\n".join(app.patterns)
        for name in ("LEARNING_PENDING", "LEARNING_CONFIRM", "LEARNING_DISABLE",
                     "LEARNING_ENABLE", "LEARNING_SCOPE"):
            self.assertIn(f"^{getattr(CB, name)}", joined)
        self.assertIn(f"^{CB.LEARNING_DELETE}", joined)

    def test_applied_examples_reach_the_screen(self):
        rule = self.pending_rule()
        learning.confirm_rule(rule.rule_id)
        learning.note_application(rule.rule_id, title="قاب ایفون ۱۳", effect="رنگ: سبز ← سفید")
        learning.disable_rule(rule.rule_id)           # any write flushes the queue
        self.assertIn("قاب ایفون ۱۳", learning_panel._rules_text())


# ---------------------------------------------------------------------------
# The read-only probe: nothing is created, nothing is remembered
# ---------------------------------------------------------------------------

@needs_parser
class TestReadOnlyAnalyze(IsolatedMemory):
    def test_the_probe_does_not_pollute_the_corpus(self):
        import asyncio

        asyncio.run(product_flow.analyze("قیمت 698000 تومان"))
        self.assertEqual(learning_corpus.count(), 0)


# ---------------------------------------------------------------------------
# Asking instead of guessing
# ---------------------------------------------------------------------------

@needs_parser
class TestAskingInsteadOfGuessing(IsolatedMemory):
    def session_with(self, source: str):
        data = product_extractor.ProductData(title="قاب", price=1098)
        ev.merge(data.evidence, "price", ev.AI, quote="هوش مصنوعی خوانده", overwrite=True)
        ev.merge(data.evidence, "title", ev.INFO, quote="نوشتهٔ خودت", overwrite=True)
        session = product_flow.ProductSession(mode="new")
        session.info_text = source
        session.data = data
        return session

    def test_an_inferred_value_becomes_a_question(self):
        from bot.modules import product_flow

        session = self.session_with("قیمت 1098")
        lines = product_flow._open_questions(session)
        self.assertTrue(lines)
        self.assertIn("قیمت", "\n".join(lines))
        # A field the seller wrote is never asked about.
        self.assertNotIn("عنوان", "\n".join(lines))

    def test_the_keyboard_offers_the_answer(self):
        from bot.modules import product_flow

        session = self.session_with("قیمت 1098")
        rows = product_flow._keyboard(session).inline_keyboard
        data = [button.callback_data for row in rows for button in row]
        self.assertIn(CB.PRODUCT_CONFIRM_GUESSED, data)

    def test_a_shop_with_no_pending_rules_gets_no_extra_button(self):
        from bot.modules import product_flow

        session = self.session_with("قیمت 1098")
        session.user_id = OTHER
        rows = product_flow._keyboard(session).inline_keyboard
        data = [button.callback_data for row in rows for button in row]
        self.assertNotIn(CB.LEARNING_PENDING, data)

    def test_a_pending_rule_is_announced_to_its_owner(self):
        from bot.modules import product_flow

        self.learn(term_rule("سبز", "سفید"))
        learning.remember(term_rule("قرمز", "صورتی"),
                          learning.Correction(field="attributes", old="قرمز", new="صورتی"))
        session = self.session_with("قیمت 1098")
        session.user_id = SUDO
        buttons = {
            button.callback_data: str(button.text)
            for row in product_flow._keyboard(session).inline_keyboard
            for button in row
        }
        self.assertIn(CB.LEARNING_PENDING, buttons)
        # A term rule is named by the wrong word the owner typed…
        self.assertIn("1 قاعدهٔ تازه در انتظار تأیید (قرمز)", buttons[CB.LEARNING_PENDING])
        # …and a price rule, whose key is only a digit count, is spelled out
        # instead of showing a bare «4» that reads like a bug report.
        self.assertEqual(
            product_flow._pending_hint([learning.Rule(kind="price_scale", key="4", value="1000")]),
            "4 رقمی",
        )
        self.assertEqual(
            product_flow._pending_hint([term_rule("a", "b"), term_rule("c", "d"),
                                        term_rule("e", "f"), term_rule("g", "h")]),
            "a، c، e، +1",
        )
        # …and an admin who may not manage the memory is not told to.
        session.user_id = OTHER
        data = [button.callback_data for row in product_flow._keyboard(session).inline_keyboard
                for button in row]
        self.assertNotIn(CB.LEARNING_PENDING, data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
