"""Plan menus follow provider listings without waiting on stale-cache refreshes."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from supervisor import agycli, codexcli, core, effortcatalog, grokcli, modelcall, opconfig, planchannel, provider


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "effort_catalog.json"
        self.enterContext(mock.patch.object(core, "SUP_DIR", self.root))
        self.enterContext(mock.patch.dict(effortcatalog._failures, clear=True))
        self.enterContext(mock.patch.object(core, "log"))
        for module, listing in ((codexcli, "_effort_listing"), (agycli, "_model_listing"),
                                (grokcli, "_model_listing")):
            self.enterContext(mock.patch.object(module, "MODELS", []))
            self.enterContext(mock.patch.object(module, listing, return_value={}))

    def cache(self, models, *, stale=False, version=effortcatalog.VERSION):
        self.path.write_text(json.dumps({"codex": {
            "version": version, "fetched_at": "2000-01-01T00:00:00+00:00" if stale
            else datetime.now(timezone.utc).isoformat(), "models": models}}))

    def test_a_model_codex_lists_is_offered_2026_11_17(self):
        catalog = {"gpt-6.2-sol": effortcatalog.entry(["low", "high"], "low"),
                   "gpt-reserve": effortcatalog.entry(["high"], "high", hidden=True)}
        with mock.patch.object(codexcli, "_effort_listing", return_value=catalog) as fetch:
            choices = [item["model"] for item in opconfig.model_choices()]
            self.assertIn("codex/gpt-6.2-sol", choices)
            self.assertNotIn("codex/gpt-reserve", choices)
            self.assertEqual(opconfig.base_url_for("codex/gpt-6.2-sol"), "broker")
            self.assertEqual(codexcli.efforts("codex/gpt-reserve"), (("high",), "high"))
            fetch.assert_called_once_with()
            with mock.patch.object(modelcall, "secrets_env", return_value={}), \
                    mock.patch.object(codexcli, "HOME", self.root), \
                    mock.patch.object(modelcall.os, "access", return_value=True):
                (self.root / "auth.json").write_text("{}")
                self.assertIsNone(modelcall.available("codex/gpt-6.2-sol"))
            preset, _ = provider._codex("codex/gpt-6.2-sol")
            self.assertEqual(preset["SHAFT_CONSULT_MODELS"], "codex/gpt-6.2-sol")

    def test_explicit_models_restrict_the_live_list_in_catalog_order(self):
        catalog = {m: effortcatalog.entry([], None) for m in ("first", "second", "third")}
        for module, listing, prefix in ((codexcli, "_effort_listing", "codex"),
                                        (agycli, "_model_listing", "antigravity"),
                                        (grokcli, "_model_listing", "grok")):
            with self.subTest(provider=prefix), mock.patch.object(module, listing, return_value=catalog):
                self.path.unlink(missing_ok=True)
                effortcatalog._failures.clear()   # the codex case's menu read failed the others' empty listings
                self.assertEqual(core.KNOBS[f"SHAFT_{prefix.upper()}_MODELS"].default, "")
                self.assertEqual(module._models(""), [])
                with self.assertRaises(ValueError):
                    module._models("wrong/model")
                with mock.patch.object(module, "MODELS", module._models(f"{prefix}/second")):
                    self.assertEqual(module.models(), [f"{prefix}/second"])
                    if prefix == "codex":
                        self.assertEqual([c["model"] for c in opconfig.model_choices()
                                          if c["model"].startswith("codex/")], ["codex/second"])
                with mock.patch.object(module, "MODELS", [f"{prefix}/third", f"{prefix}/first"]):
                    self.assertEqual(module.models(), [f"{prefix}/first", f"{prefix}/third"])
                with mock.patch.object(module, "MODELS", [f"{prefix}/absent"]):
                    self.assertEqual(module.models(), [])

    def test_unavailable_listing_uses_only_explicit_names_and_empty_is_known(self):
        for module in (codexcli, agycli, grokcli):
            with self.subTest(provider=module.__name__), \
                    mock.patch.object(effortcatalog, "listed", return_value=None):
                self.assertEqual(module.models(), [])
                with mock.patch.object(module, "MODELS", ["explicit/model"]):
                    self.assertEqual(module.models(), ["explicit/model"])
            with mock.patch.object(effortcatalog, "listed", return_value=[]), \
                    mock.patch.object(module, "MODELS", ["explicit/model"]):
                self.assertEqual(module.models(), [])

    def test_stale_menu_returns_while_one_refresh_serves_concurrent_reads(self):
        self.cache({"old": effortcatalog.entry(["low"], "low")}, stale=True)
        started, release = threading.Event(), threading.Event()
        def fetch():
            started.set()
            if not release.wait(3):
                raise AssertionError("refresh was never released")
            return {"new": effortcatalog.entry(["high"], "high")}
        listing = mock.Mock(side_effect=fetch)
        try:
            with ThreadPoolExecutor(max_workers=8) as readers:
                futures = [readers.submit(effortcatalog.listed, "codex", listing) for _ in range(8)]
                self.assertEqual([f.result(timeout=1) for f in futures], [["old"]] * 8)
                self.assertTrue(started.wait(1))
                listing.assert_called_once_with()
                self.assertEqual(effortcatalog.listed("codex", listing, refresh=False), ["old"])
                with mock.patch.object(codexcli, "_effort_listing", listing):
                    self.assertIn("codex/old", [c["model"] for c in opconfig.model_choices()])
                self.assertEqual(json.loads(self.path.read_text())["codex"]["models"],
                                 {"old": {"levels": ["low"], "default": "low", "hidden": False}})
        finally:
            release.set()
            lock = effortcatalog._locks["codex"]
            if lock.acquire(timeout=3):
                lock.release()
            else:
                self.fail("background refresh did not finish")
        self.assertEqual(effortcatalog.listed("codex", listing), ["new"])
        self.assertEqual(effortcatalog.lookup("codex", "new", listing), (("high",), "high"))
        listing.assert_called_once_with()

    def test_an_empty_listing_is_a_failure_not_an_empty_menu(self):
        fetch = mock.Mock(return_value={})
        self.assertIsNone(effortcatalog.listed("codex", fetch))
        self.assertFalse(self.path.exists())
        core.log.assert_called_once()

    def test_failed_background_refresh_keeps_stale_models_and_backs_off(self):
        self.cache({"old": effortcatalog.entry([], None)}, stale=True)
        fetch = mock.Mock(side_effect=OSError("offline"))
        self.assertEqual(effortcatalog.listed("codex", fetch), ["old"])
        lock = effortcatalog._locks["codex"]
        self.assertTrue(lock.acquire(timeout=3))
        lock.release()
        self.assertEqual(effortcatalog.listed("codex", fetch), ["old"])
        fetch.assert_called_once_with()
        core.log.assert_called_once()
        self.assertEqual(list(json.loads(self.path.read_text())["codex"]["models"]), ["old"])

    def test_stale_menu_does_not_wait_for_an_inline_effort_refresh(self):
        self.cache({"old": effortcatalog.entry(["low"], "low")}, stale=True)
        started, release = threading.Event(), threading.Event()
        def fetch():
            started.set()
            release.wait(3)
            return {"new": effortcatalog.entry(["high"], "high")}
        listing = mock.Mock(side_effect=fetch)
        with ThreadPoolExecutor(max_workers=2) as readers:
            effort = readers.submit(effortcatalog.lookup, "codex", "new", listing)
            try:
                self.assertTrue(started.wait(1))
                menu = readers.submit(effortcatalog.listed, "codex", listing)
                self.assertEqual(menu.result(timeout=1), ["old"])
            finally:
                release.set()
            self.assertEqual(effort.result(timeout=1), (("high",), "high"))
        listing.assert_called_once_with()

    def test_pre_hidden_cache_is_refetched_even_with_a_recent_stamp(self):
        for version, models in ((None, {"old": {"levels": [], "default": None}}),
                                (effortcatalog.VERSION, {"old": {"levels": [], "default": None}})):
            with self.subTest(version=version):
                self.cache(models, version=version)
                fetch = mock.Mock(return_value={"new": effortcatalog.entry([], None)})
                self.assertEqual(effortcatalog.lookup("codex", "new", fetch), ((), None))
                self.assertEqual(effortcatalog.listed("codex", fetch), ["new"])
                fetch.assert_called_once_with()
                self.assertEqual(json.loads(self.path.read_text())["codex"]["version"], effortcatalog.VERSION)

    def test_no_cache_refresh_false_and_failed_inline_fetch_are_unknown(self):
        fetch = mock.Mock(side_effect=OSError("offline"))
        self.assertIsNone(effortcatalog.listed("new-provider", fetch, refresh=False))
        fetch.assert_not_called()
        with mock.patch.object(effortcatalog.time, "monotonic", return_value=0):
            self.assertIsNone(effortcatalog.listed("new-provider", fetch))
        with mock.patch.object(effortcatalog.time, "monotonic", return_value=59):
            self.assertIsNone(effortcatalog.listed("new-provider", fetch))
        fetch.assert_called_once_with()
        with mock.patch.object(effortcatalog.time, "monotonic", return_value=61):
            self.assertIsNone(effortcatalog.listed("new-provider", fetch))
        self.assertEqual(fetch.call_count, 2)
        core.log.assert_called_once()

    def test_any_model_never_fetches_with_missing_stale_or_fresh_cache(self):
        with mock.patch.object(codexcli, "_effort_listing") as fetch:
            self.assertEqual(core.any_model("codex/new"), "codex/new")
            for stale in (True, False):
                self.cache({"new": effortcatalog.entry([], None)}, stale=stale)
                self.assertEqual(core.any_model("codex/new"), "codex/new")
                with self.assertRaisesRegex(ValueError, "SHAFT_CODEX_MODELS.*codex/new"):
                    core.any_model("codex/other")
            fetch.assert_not_called()

    def test_uniform_models_refresh_reaches_every_backend(self):
        from supervisor import claudecli
        with mock.patch.object(codexcli, "models", return_value=["codex/a"]) as codex, \
                mock.patch.object(agycli, "models", return_value=["antigravity/b"]) as agy, \
                mock.patch.object(grokcli, "models", return_value=["grok/c"]) as grok:
            self.assertEqual(planchannel.models(refresh=False),
                             ["codex/a", "grok/c", *claudecli.MODELS, "antigravity/b"])
            for fetch in (codex, agy, grok):
                fetch.assert_called_once_with(refresh=False)


class ListingTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "home"
        self.enterContext(mock.patch.object(agycli, "HOME", self.home))
        self.enterContext(mock.patch.object(grokcli, "HOME", self.home))
        self.home.mkdir()
        self.enterContext(mock.patch.object(agycli, "logged_in", return_value=True))

    def test_antigravity_lists_only_gemini_bases_with_all_mapped_levels(self):
        output = "Fetching available models...\n\n" + "\n".join(
            f"gemini-4-argon-{level}\tGemini 4 Argon ({level.title()})" for level in ("high", "low", "medium"))
        output += ("\ngemini-4-hydrogen-xhigh\tGemini 4 Hydrogen (XHigh)"
                   "\ngemini-pro-orchestrator-g1-pro\tOrchestrator (Argon)"
                   "\ngemini-incomplete-low\tIncomplete (Low)\nmalformed"
                   "\nclaude-opus-5-5-high\tClaude Opus 5.5 (High)"
                   "\ngpt-oss-120b-medium\tGPT-OSS 120B (Medium)")
        output += "\n" + "\n".join(f"gemini-4-complete-{level}\tComplete" for level in
                                    ("max", "xhigh", "high", "medium", "low"))
        with mock.patch.object(agycli.subprocess, "run", return_value=mock.Mock(stdout=output)) as run:
            self.assertEqual(agycli._model_listing(), {
                "gemini-4-argon": {"levels": ["low", "medium", "high"], "default": "medium", "hidden": False},
                "gemini-4-complete": {"levels": ["low", "medium", "high", "xhigh", "max"],
                                      "default": "medium", "hidden": False}})
        self.assertEqual(run.call_args.args[0], [agycli.BIN, "models"])
        self.assertEqual(run.call_args.kwargs["env"],
                         agycli.child_env(self.home.resolve(), run.call_args.kwargs["cwd"]))
        self.assertEqual(run.call_args.kwargs["timeout"], 20)
        self.assertTrue(run.call_args.kwargs["check"])

    def test_a_signed_out_listing_is_refused_not_cached_2026_11_17(self):
        # Signed out, grok listed grok-4.6 and grok-4.5 only; signed in it lists grok-4.7 first. Cached, the signed-out
        # list hid grok-4.7 from the menu for a day after the login.
        output = "You are not authenticated.\n\nAvailable models:\n  * grok-4.6 (default)\n  - grok-4.5\n"
        with mock.patch.object(grokcli.subprocess, "run", return_value=mock.Mock(stdout=output)):
            with self.assertRaisesRegex(RuntimeError, "not signed in"):
                grokcli._model_listing()
        with mock.patch.object(agycli, "logged_in", return_value=False), \
                mock.patch.object(agycli.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "not signed in"):
                agycli._model_listing()
            run.assert_not_called()
        self.home.rmdir()
        with mock.patch.object(grokcli.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "not signed in"):
                grokcli._model_listing()
            run.assert_not_called()
        self.assertFalse(self.home.exists())          # a menu read never creates the login folder

    def test_grok_parses_only_available_models_after_the_preamble(self):
        output = ("You are logged in with grok.com.\nDefault model: grok-ignored\n  - outside\n"
                  "Available models:\n  * grok-4.7 (default)\n  - grok-4.6\n\n"
                  "Other section:\n  - ignored\n")
        with mock.patch.object(grokcli.subprocess, "run", return_value=mock.Mock(stdout=output)) as run:
            self.assertEqual(grokcli._model_listing(), {
                name: {"levels": ["low", "medium", "high", "xhigh"], "default": None, "hidden": False}
                for name in ("grok-4.7", "grok-4.6")})
        self.assertEqual(run.call_args.args[0], [grokcli.BIN, "models"])
        self.assertEqual(run.call_args.kwargs["env"],
                         grokcli.child_env(self.home.resolve(), run.call_args.kwargs["cwd"]))
        self.assertEqual(run.call_args.kwargs["timeout"], 20)
        self.assertTrue(run.call_args.kwargs["check"])

    def test_provider_listing_failures_reach_the_catalog(self):
        for module in (agycli, grokcli):
            with self.subTest(provider=module.__name__), \
                    mock.patch.object(module.subprocess, "run", side_effect=subprocess.TimeoutExpired("fake", 20)):
                with self.assertRaises(subprocess.TimeoutExpired):
                    module._model_listing()

    def test_codex_listing_preserves_hidden_flags_across_pages(self):
        channel = mock.Mock()
        channel._request.side_effect = [
            {"data": [{"model": "gpt-6.2-sol", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}],
             "nextCursor": "next"},
            {"data": [{"model": "gpt-reserve", "supportedReasoningEfforts": [], "hidden": True}]}]
        with mock.patch.object(codexcli, "_CatalogChannel", return_value=channel):
            self.assertEqual(codexcli._effort_listing(), {
                "gpt-6.2-sol": {"levels": ["high"], "default": None, "hidden": False},
                "gpt-reserve": {"levels": [], "default": None, "hidden": True}})
        self.assertEqual(channel._request.call_args_list, [
            mock.call("model/list", {"includeHidden": True}),
            mock.call("model/list", {"includeHidden": True, "cursor": "next"})])
        channel.close.assert_called_once_with()
