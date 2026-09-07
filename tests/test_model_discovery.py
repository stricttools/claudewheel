"""Tests for model discovery: the Anthropic model list, its cache, and the sort.

The models endpoint is the only network call here, and every test stubs it at
the urllib boundary -- the suite's socket guard denies real traffic outright.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any
from unittest import mock
from unittest.mock import MagicMock

from claudewheel.appdata import OptionsFile
from claudewheel.config import _MIGRATIONS, AppConfigStore
from claudewheel.defaults import DEFAULT_CONFIG, DEFAULT_OPTIONS
from claudewheel.profile_store import Profile
from claudewheel.segment import (
    MODEL_LIST_CACHE_KEY,
    SegmentState,
    _discover_anthropic_models,
    _discover_anthropic_models_cached,
    build_segment_bar,
    fetch_available_models,
    run_slow_discovery_via_registry,
)
from claudewheel.workspace import Workspace
from tests.wheelhelpers import setup_temp_config_dir, write_json

MODELS_URL_PAGE1 = "https://api.anthropic.com/v1/models?limit=100"

# The schema version a config that has run every versioned migration carries.
CURRENT_SCHEMA_VERSION = max(m["version"] for m in _MIGRATIONS)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(MODELS_URL_PAGE1, code, "message", Message(), None)


def _page(
    models: list[dict[str, str]], *, has_more: bool = False, last_id: str | None = None
) -> MagicMock:
    """A urlopen context manager yielding one models-endpoint page."""
    payload: dict[str, Any] = {"data": models, "has_more": has_more}
    if last_id is not None:
        payload["last_id"] = last_id
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    cm = MagicMock()
    cm.__enter__.return_value = resp
    return cm


def _model(model_id: str, created_at: str | None = None) -> dict[str, str]:
    entry = {"type": "model", "id": model_id, "display_name": model_id.title()}
    if created_at is not None:
        entry["created_at"] = created_at
    return entry


def _ws(tokens: dict[str, str | None]) -> Workspace:
    """A workspace stand-in whose profiles carry the given stored tokens.

    A None token means the profile has no token at all (the vanilla
    ``default``), so it is never offered as a candidate.
    """
    ws = MagicMock(spec=Workspace)
    ws.profiles.enumerate.return_value = [
        Profile(name, Path("/nonexistent") / name, False, token is not None)
        for name, token in tokens.items()
    ]

    def data_for(name: str) -> MagicMock:
        store = MagicMock()
        store.token.return_value = tokens.get(name)
        return store

    ws.profiles.data_for.side_effect = data_for
    return ws


class FetchAvailableModelsTests(unittest.TestCase):
    def test_request_shape(self) -> None:
        urlopen = MagicMock(
            return_value=_page([_model("claude-fable-5-1", "2026-08-28T00:00:00Z")])
        )
        state: dict[str, Any] = {}
        with mock.patch("urllib.request.urlopen", urlopen):
            models = fetch_available_models(state, _ws({"work": "sk-ant-work"}))

        self.assertEqual(
            models, [{"id": "claude-fable-5-1", "created_at": "2026-08-28T00:00:00Z"}]
        )
        (req,), kwargs = urlopen.call_args
        self.assertEqual(req.full_url, MODELS_URL_PAGE1)
        self.assertEqual(req.get_header("Authorization"), "Bearer sk-ant-work")
        self.assertEqual(req.get_header("Anthropic-version"), "2023-06-01")
        self.assertEqual(kwargs.get("timeout"), 5.0)

    def test_pagination_follows_after_id(self) -> None:
        urlopen = MagicMock(
            side_effect=[
                _page(
                    [_model("m1", "2026-01-01T00:00:00Z")], has_more=True, last_id="m1"
                ),
                _page(
                    [_model("m2", "2026-02-01T00:00:00Z")], has_more=False, last_id="m2"
                ),
            ]
        )
        with mock.patch("urllib.request.urlopen", urlopen):
            models = fetch_available_models({}, _ws({"work": "sk-ant-work"}))

        self.assertEqual([m["id"] for m in models], ["m1", "m2"])
        self.assertEqual(urlopen.call_count, 2)
        second_url = urlopen.call_args_list[1][0][0].full_url
        self.assertIn("after_id=m1", second_url)
        self.assertIn("limit=100", second_url)

    def test_pagination_stops_when_the_far_side_stops_advancing(self) -> None:
        """A page that keeps reporting the same last_id ends the walk."""
        urlopen = MagicMock(
            side_effect=lambda *a, **k: _page(
                [_model("m1", "2026-01-01T00:00:00Z")], has_more=True, last_id="m1"
            )
        )
        with mock.patch("urllib.request.urlopen", urlopen):
            models = fetch_available_models({}, _ws({"work": "sk-ant-work"}))

        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual([m["id"] for m in models], ["m1", "m1"])

    def test_401_tries_the_next_token(self) -> None:
        urlopen = MagicMock(
            side_effect=[
                _http_error(401),
                _page([_model("m1", "2026-01-01T00:00:00Z")]),
            ]
        )
        state: dict[str, Any] = {}
        with mock.patch("urllib.request.urlopen", urlopen):
            models = fetch_available_models(
                state, _ws({"stale": "sk-ant-stale", "work": "sk-ant-work"})
            )

        self.assertEqual([m["id"] for m in models], ["m1"])
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(
            urlopen.call_args_list[1][0][0].get_header("Authorization"),
            "Bearer sk-ant-work",
        )

    def test_network_error_stops_at_the_first_token(self) -> None:
        urlopen = MagicMock(side_effect=urllib.error.URLError("name resolution failed"))
        with mock.patch("urllib.request.urlopen", urlopen):
            models = fetch_available_models(
                {}, _ws({"work": "sk-ant-work", "other": "sk-ant-other"})
            )

        self.assertEqual(models, [])
        self.assertEqual(urlopen.call_count, 1)

    def test_network_error_returns_the_stale_cache(self) -> None:
        stale = [{"id": "m-old", "created_at": "2025-01-01T00:00:00Z"}]
        state = {
            MODEL_LIST_CACHE_KEY: {"fetched_at": time.time() - 7200, "models": stale}
        }
        urlopen = MagicMock(side_effect=urllib.error.URLError("offline"))
        with mock.patch("urllib.request.urlopen", urlopen):
            models = fetch_available_models(state, _ws({"work": "sk-ant-work"}))

        self.assertEqual(models, stale)
        self.assertEqual(urlopen.call_count, 1)

    def test_server_error_ends_the_refresh(self) -> None:
        urlopen = MagicMock(side_effect=_http_error(500))
        with mock.patch("urllib.request.urlopen", urlopen):
            models = fetch_available_models(
                {}, _ws({"work": "sk-ant-work", "other": "sk-ant-other"})
            )

        self.assertEqual(models, [])
        self.assertEqual(urlopen.call_count, 1)

    def test_fresh_cache_skips_the_network(self) -> None:
        cached = [{"id": "m-cached", "created_at": "2026-03-01T00:00:00Z"}]
        state = {MODEL_LIST_CACHE_KEY: {"fetched_at": time.time(), "models": cached}}
        urlopen = MagicMock()
        with mock.patch("urllib.request.urlopen", urlopen):
            models = fetch_available_models(state, _ws({"work": "sk-ant-work"}))

        self.assertEqual(models, cached)
        urlopen.assert_not_called()

    def test_success_writes_the_cache(self) -> None:
        urlopen = MagicMock(return_value=_page([_model("m1", "2026-01-01T00:00:00Z")]))
        state: dict[str, Any] = {}
        with mock.patch("urllib.request.urlopen", urlopen):
            fetch_available_models(state, _ws({"work": "sk-ant-work"}))

        cache = state[MODEL_LIST_CACHE_KEY]
        self.assertEqual(
            cache["models"], [{"id": "m1", "created_at": "2026-01-01T00:00:00Z"}]
        )
        self.assertLess(time.time() - cache["fetched_at"], 60)

    def test_profiles_without_tokens_never_reach_the_network(self) -> None:
        urlopen = MagicMock()
        with mock.patch("urllib.request.urlopen", urlopen):
            models = fetch_available_models({}, _ws({"default": None}))

        self.assertEqual(models, [])
        urlopen.assert_not_called()

    def test_last_used_profile_is_tried_first(self) -> None:
        urlopen = MagicMock(return_value=_page([_model("m1")]))
        state = {"last_config": {"profile": "second"}}
        with mock.patch("urllib.request.urlopen", urlopen):
            fetch_available_models(
                state, _ws({"first": "sk-ant-first", "second": "sk-ant-second"})
            )

        self.assertEqual(
            urlopen.call_args_list[0][0][0].get_header("Authorization"),
            "Bearer sk-ant-second",
        )

    def test_entries_without_an_id_are_ignored(self) -> None:
        urlopen = MagicMock(
            return_value=_page(
                [{"type": "model"}, _model("m1", "2026-01-01T00:00:00Z")]
            )
        )
        with mock.patch("urllib.request.urlopen", urlopen):
            models = fetch_available_models({}, _ws({"work": "sk-ant-work"}))

        self.assertEqual([m["id"] for m in models], ["m1"])

    def test_undated_model_carries_no_created_at(self) -> None:
        urlopen = MagicMock(return_value=_page([_model("m1")]))
        with mock.patch("urllib.request.urlopen", urlopen):
            models = fetch_available_models({}, _ws({"work": "sk-ant-work"}))

        self.assertEqual(models, [{"id": "m1"}])


class ModelDiscoveryFunctionTests(unittest.TestCase):
    def test_slow_discovery_returns_values_and_dates(self) -> None:
        urlopen = MagicMock(return_value=_page([_model("m1", "2026-01-01T00:00:00Z")]))
        with mock.patch("urllib.request.urlopen", urlopen):
            result = _discover_anthropic_models({}, {}, _ws({"work": "sk-ant-work"}))

        self.assertEqual(result.values, ["m1"])
        self.assertEqual(
            result.metadata, {"m1": {"created_at": "2026-01-01T00:00:00Z"}}
        )

    def test_warm_discovery_reads_a_fresh_cache(self) -> None:
        state = {
            MODEL_LIST_CACHE_KEY: {
                "fetched_at": time.time(),
                "models": [{"id": "m1", "created_at": "2026-01-01T00:00:00Z"}],
            }
        }
        result = _discover_anthropic_models_cached({}, state, MagicMock(spec=Workspace))
        self.assertEqual(result.values, ["m1"])

    def test_warm_discovery_ignores_a_stale_cache(self) -> None:
        state = {
            MODEL_LIST_CACHE_KEY: {
                "fetched_at": time.time() - 7200,
                "models": [{"id": "m1"}],
            }
        }
        result = _discover_anthropic_models_cached({}, state, MagicMock(spec=Workspace))
        self.assertEqual(result.values, [])

    def test_warm_discovery_never_touches_the_network(self) -> None:
        urlopen = MagicMock()
        with mock.patch("urllib.request.urlopen", urlopen):
            _discover_anthropic_models_cached({}, {}, MagicMock(spec=Workspace))
        urlopen.assert_not_called()


class RecordDiscoveredModelsTests(unittest.TestCase):
    """Persistence of discovered models into options.json (main thread only)."""

    def setUp(self) -> None:
        self._tmp_obj = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_obj.cleanup)
        self.tmp = Path(self._tmp_obj.name)

    def _store(self, model_entry: dict[str, Any]) -> tuple[AppConfigStore, Path]:
        options = {**DEFAULT_OPTIONS, "model": model_entry}
        paths = setup_temp_config_dir(self.tmp, options=options)
        return Workspace.open(paths["CONFIG_DIR"]).appconfig(), paths["OPTIONS_FILE"]

    def _on_disk(self, options_file: Path) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(options_file.read_text())
        return data

    def test_new_models_are_appended_at_the_end(self) -> None:
        cfg, options_file = self._store({"values": ["a", "b"], "pinned": []})
        before = list(cfg.options_def["model"]["values"])

        cfg.record_discovered_models(["b", "c"], {})

        values = cfg.options_def["model"]["values"]
        self.assertEqual(values[: len(before)], before)
        self.assertEqual(values[-1], "c")
        self.assertEqual(values.count("b"), 1)
        self.assertEqual(self._on_disk(options_file)["model"]["values"], values)

    def test_created_at_is_written_for_new_and_existing_ids(self) -> None:
        cfg, options_file = self._store({"values": ["a"], "pinned": []})

        cfg.record_discovered_models(
            ["a", "z"],
            {
                "a": {"created_at": "2025-01-01T00:00:00Z"},
                "z": {"created_at": "2026-01-01T00:00:00Z"},
            },
        )

        metadata = cfg.options_def["model"]["metadata"]
        self.assertEqual(metadata["a"]["created_at"], "2025-01-01T00:00:00Z")
        self.assertEqual(metadata["z"]["created_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(self._on_disk(options_file)["model"]["metadata"], metadata)

    def test_existing_metadata_keys_survive(self) -> None:
        cfg, options_file = self._store(
            {
                "values": ["a"],
                "pinned": [],
                "metadata": {"a": {"model_id": "vendor/a"}},
            }
        )

        cfg.record_discovered_models(
            ["a"], {"a": {"created_at": "2025-01-01T00:00:00Z"}}
        )

        entry = cfg.options_def["model"]["metadata"]["a"]
        self.assertEqual(entry["model_id"], "vendor/a")
        self.assertEqual(entry["created_at"], "2025-01-01T00:00:00Z")
        self.assertEqual(self._on_disk(options_file)["model"]["metadata"]["a"], entry)

    def test_nothing_changed_writes_nothing(self) -> None:
        cfg, _options_file = self._store(
            {
                "values": ["a"],
                "pinned": [],
                "metadata": {"a": {"created_at": "2025-01-01T00:00:00Z"}},
            }
        )

        with mock.patch(
            "claudewheel.appdata.write_json_atomic", autospec=True
        ) as writer:
            cfg.record_discovered_models(
                ["a"], {"a": {"created_at": "2025-01-01T00:00:00Z"}}
            )
        writer.assert_not_called()

    def test_empty_discovery_writes_nothing(self) -> None:
        cfg, _options_file = self._store({"values": ["a"], "pinned": []})
        with mock.patch(
            "claudewheel.appdata.write_json_atomic", autospec=True
        ) as writer:
            cfg.record_discovered_models([], {})
        writer.assert_not_called()

    def test_options_file_accessor_is_append_only(self) -> None:
        """OptionsFile.record_discovered never removes or reorders values."""
        path = self.tmp / "options.json"
        write_json(path, {"model": {"values": ["a", "b"], "pinned": []}})

        options = OptionsFile(path).record_discovered("model", ["c"], {}, {})

        self.assertEqual(options["model"]["values"], ["a", "b", "c"])
        self.assertEqual(
            json.loads(path.read_text())["model"]["values"], ["a", "b", "c"]
        )


class ModelReleaseSortTests(unittest.TestCase):
    """The display sort: newest release first, [1m] beside its base, pins on top."""

    def _state(
        self,
        values: list[str],
        metadata: dict[str, dict[str, Any]],
        pinned: list[str] | None = None,
    ) -> SegmentState:
        state = SegmentState(
            collection_order=["pinned", "defaults"], sort="release_date_desc"
        )
        state.set_defaults(list(values))
        for pin in pinned or []:
            state.add_pinned(pin)
        state.set_metadata(metadata)
        return state

    def test_newest_release_first(self) -> None:
        state = self._state(
            ["old", "new", "middle"],
            {
                "old": {"created_at": "2024-01-01T00:00:00Z"},
                "new": {"created_at": "2026-01-01T00:00:00Z"},
                "middle": {"created_at": "2025-01-01T00:00:00Z"},
            },
        )
        self.assertEqual(state.options, ["new", "middle", "old"])

    def test_1m_entry_follows_its_base(self) -> None:
        state = self._state(
            ["base-old", "base-new[1m]", "base-new"],
            {
                "base-old": {"created_at": "2024-01-01T00:00:00Z"},
                "base-new": {"created_at": "2026-01-01T00:00:00Z"},
            },
        )
        self.assertEqual(state.options, ["base-new", "base-new[1m]", "base-old"])

    def test_each_1m_entry_follows_its_own_base_when_bases_share_a_date(self) -> None:
        """Two bases released the same day still each keep their variant beside them."""
        state = self._state(
            ["base-a", "base-a[1m]", "base-b", "base-b[1m]"],
            {
                "base-a": {"created_at": "2026-01-01T00:00:00Z"},
                "base-b": {"created_at": "2026-01-01T00:00:00Z"},
            },
        )
        self.assertEqual(
            state.options,
            ["base-a", "base-a[1m]", "base-b", "base-b[1m]"],
        )

    def test_undated_entries_follow_dated_ones_in_stored_order(self) -> None:
        state = self._state(
            ["undated-first", "dated", "undated-second"],
            {"dated": {"created_at": "2026-01-01T00:00:00Z"}},
        )
        self.assertEqual(state.options, ["dated", "undated-first", "undated-second"])

    def test_pinned_entries_stay_on_top_in_stored_order(self) -> None:
        state = self._state(
            ["newest", "pin-b", "pin-a"],
            {
                "newest": {"created_at": "2026-06-01T00:00:00Z"},
                "pin-a": {"created_at": "2020-01-01T00:00:00Z"},
                "pin-b": {"created_at": "2021-01-01T00:00:00Z"},
            },
            pinned=["pin-b", "pin-a"],
        )
        self.assertEqual(state.options, ["pin-b", "pin-a", "newest"])

    def test_no_metadata_preserves_stored_order(self) -> None:
        state = self._state(["c", "a", "b"], {})
        self.assertEqual(state.options, ["c", "a", "b"])

    def test_metadata_update_reorders_the_option_list(self) -> None:
        state = self._state(["a", "b"], {})
        self.assertEqual(state.options, ["a", "b"])
        state.update_metadata({"b": {"created_at": "2026-01-01T00:00:00Z"}})
        self.assertEqual(state.options, ["b", "a"])


class AccumulatedModelValuesTests(unittest.TestCase):
    """options.json's model values are what the picker offers."""

    def setUp(self) -> None:
        self._tmp_obj = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_obj.cleanup)
        self.tmp = Path(self._tmp_obj.name)

    def _bar_model_options(self, model_entry: dict[str, Any]) -> list[str]:
        paths = setup_temp_config_dir(
            self.tmp,
            # A config that has already been through every versioned migration:
            # the one-time classification pass (migration 3) pins values it
            # cannot attribute to a shipped default, which is not the state a
            # model appended by discovery arrives in.
            config={
                **DEFAULT_CONFIG,
                "enabled_segments": ["model"],
                "_schema_version": CURRENT_SCHEMA_VERSION,
            },
            options={**DEFAULT_OPTIONS, "model": model_entry},
        )
        cfg = Workspace.open(paths["CONFIG_DIR"]).appconfig()
        bar = build_segment_bar(cfg, skip_slow=True)
        return bar.segments[0].options

    def test_model_only_in_options_json_is_offered(self) -> None:
        options = self._bar_model_options(
            {"values": ["claude-newly-discovered-9"], "pinned": []}
        )
        self.assertIn("claude-newly-discovered-9", options)

    def test_shipped_defaults_are_still_offered(self) -> None:
        options = self._bar_model_options(
            {"values": ["claude-newly-discovered-9"], "pinned": []}
        )
        for shipped in DEFAULT_OPTIONS["model"]["values"]:
            self.assertIn(shipped, options)

    def test_warm_start_records_cached_models_in_options_json(self) -> None:
        """A warm cache is persisted on the main thread that builds the bar."""
        paths = setup_temp_config_dir(
            self.tmp,
            config={
                **DEFAULT_CONFIG,
                "enabled_segments": ["model"],
                "_schema_version": CURRENT_SCHEMA_VERSION,
            },
            options={**DEFAULT_OPTIONS, "model": {"values": ["a"], "pinned": []}},
            state={
                MODEL_LIST_CACHE_KEY: {
                    "fetched_at": time.time(),
                    "models": [
                        {"id": "a", "created_at": "2024-01-01T00:00:00Z"},
                        {"id": "z", "created_at": "2026-01-01T00:00:00Z"},
                    ],
                }
            },
        )
        cfg = Workspace.open(paths["CONFIG_DIR"]).appconfig()
        bar = build_segment_bar(cfg, skip_slow=True)

        on_disk = json.loads(paths["OPTIONS_FILE"].read_text())["model"]
        # "a" keeps its position, "z" joins the end, nothing else moves.
        self.assertEqual(on_disk["values"][0], "a")
        self.assertEqual(on_disk["values"][-1], "z")
        self.assertEqual(on_disk["metadata"]["a"]["created_at"], "2024-01-01T00:00:00Z")
        self.assertEqual(on_disk["metadata"]["z"]["created_at"], "2026-01-01T00:00:00Z")
        # Newest first in the picker, and no network was needed to get there.
        self.assertEqual(bar.segments[0].options[:2], ["z", "a"])

    def test_slow_registry_run_includes_the_model_segment(self) -> None:
        """The model segment is part of the background discovery sweep."""
        paths = setup_temp_config_dir(self.tmp)
        cfg = Workspace.open(paths["CONFIG_DIR"]).appconfig()
        urlopen = MagicMock(return_value=_page([_model("m1", "2026-01-01T00:00:00Z")]))
        # Only the model entry: the sweep's other slow members shell out to npm
        # and gh, which have nothing to do with what this asserts.
        options_def = {"model": cfg.options_def["model"]}
        with mock.patch("urllib.request.urlopen", urlopen):
            results = run_slow_discovery_via_registry(
                options_def, {}, _ws({"work": "sk-ant-work"})
            )

        self.assertIn("model", results)
        self.assertEqual(results["model"].values, ["m1"])

    def test_recorded_release_dates_order_the_picker(self) -> None:
        options = self._bar_model_options(
            {
                "values": ["older-model", "newer-model"],
                "pinned": [],
                "metadata": {
                    "older-model": {"created_at": "2024-01-01T00:00:00Z"},
                    "newer-model": {"created_at": "2026-01-01T00:00:00Z"},
                },
            }
        )
        self.assertLess(options.index("newer-model"), options.index("older-model"))


if __name__ == "__main__":
    unittest.main()
