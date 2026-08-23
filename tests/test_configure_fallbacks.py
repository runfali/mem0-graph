"""Server configure endpoint: LLM fallbacks provider validation + secret redaction.

POST /configure must reject llm.fallbacks entries whose provider is not in
BUNDLED_LLM_PROVIDERS (same 400 contract as the top-level llm/embedder
providers), and GET /configure must redact api_key inside fallback configs.
"""

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient

os.environ.setdefault("AUTH_DISABLED", "true")
os.environ.setdefault("JWT_SECRET", "test-secret-for-fallbacks")
os.environ.setdefault("OPENAI_API_KEY", "test-key-not-used")

# server/ modules use bare imports (from auth import ...), so the server
# directory itself must be importable, mirroring how it runs in Docker.
_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)


def _mock_session():
    """DB session whose reads return None so bootstrap auth paths behave empty."""
    session = MagicMock()
    session.scalar.return_value = None
    session.query.return_value.first.return_value = None
    session.__enter__.return_value = session
    session.__exit__.return_value = False
    return session


@pytest.fixture
def _mock_memory():
    """Patch Memory.from_config so the server imports without a real backend.

    Yields (mock_instance, mock_save_config, mock_save_overrides):
    the last two record whether config.json got written and DB overrides
    got bypassed, without touching the real filesystem or database.
    """
    mock_instance = MagicMock()
    with patch.dict(os.environ, {"OPENAI_API_KEY": "fake-key"}):
        with patch("mem0.Memory.from_config", return_value=mock_instance):
            with patch("server_state._load_overrides", return_value={}):
                with patch("server_state._save_overrides") as mock_save_overrides:
                    with patch("server_state._save_config_file") as mock_save_config:
                        with patch("db.SessionLocal", return_value=_mock_session()):
                            with patch("auth.SessionLocal", return_value=_mock_session()):
                                yield mock_instance, mock_save_config, mock_save_overrides


def _load_app(env_overrides: dict):
    """Reload server/main.py with the given environment and return the module."""
    import main as server_main

    with patch.dict(os.environ, env_overrides, clear=False):
        importlib.reload(server_main)
    return server_main


class TestValidateBundledFallbacks:
    """POST /configure rejects non-bundled LLM fallback providers."""

    @pytest.fixture(autouse=True)
    def _setup(self, _mock_memory):
        self.server_main = _load_app({"ADMIN_API_KEY": ""})
        self.client = TestClient(self.server_main.app)

    def test_valid_fallback_provider_accepted(self):
        resp = self.client.post(
            "/configure",
            json={"llm": {"fallbacks": [{"provider": "openai", "config": {"api_key": "sk-x"}}]}},
        )
        assert resp.status_code == 200

    def test_invalid_fallback_provider_rejected(self):
        resp = self.client.post(
            "/configure",
            json={"llm": {"fallbacks": [{"provider": "notexist", "config": {"api_key": "sk-x"}}]}},
        )
        assert resp.status_code == 400
        assert "notexist" in resp.json()["detail"]

    def test_invalid_fallback_provider_names_index(self):
        resp = self.client.post(
            "/configure",
            json={
                "llm": {
                    "fallbacks": [
                        {"provider": "openai", "config": {}},
                        {"provider": "notexist", "config": {}},
                    ]
                }
            },
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "fallbacks" in detail
        assert "notexist" in detail

    def test_no_fallbacks_behavior_unchanged(self):
        resp = self.client.post("/configure", json={"llm": {"provider": "openai"}})
        assert resp.status_code == 200

    def test_non_list_fallbacks_rejected(self):
        resp = self.client.post(
            "/configure",
            json={"llm": {"fallbacks": {"provider": "openai", "config": {}}}},
        )
        assert resp.status_code == 400


class TestRedactFallbackSecrets:
    """GET /configure redacts api_key inside fallback configs."""

    @pytest.fixture(autouse=True)
    def _setup(self, _mock_memory):
        self.server_main = _load_app({"ADMIN_API_KEY": ""})
        self.client = TestClient(self.server_main.app)

    def test_redact_config_redacts_fallback_api_key(self):
        redacted = self.server_main._redact_config(
            {"llm": {"fallbacks": [{"provider": "openai", "config": {"api_key": "sk-xxx"}}]}}
        )
        assert redacted["llm"]["fallbacks"][0]["config"]["api_key"] == "[redacted]"

    def test_get_configure_redacts_fallback_api_key(self):
        self.client.post(
            "/configure",
            json={"llm": {"fallbacks": [{"provider": "openai", "config": {"api_key": "sk-xxx"}}]}},
        )
        resp = self.client.get("/configure")
        assert resp.status_code == 200
        fb = resp.json()["llm"]["fallbacks"][0]
        assert fb["config"]["api_key"] == "[redacted]"


class TestBuildLlmFallbacksFromEnv:
    """MEM0_LLM_FALLBACK* env vars build llm.fallbacks in DEFAULT_CONFIG."""

    @pytest.fixture(autouse=True)
    def _setup(self, _mock_memory):
        self.server_main = _load_app({"ADMIN_API_KEY": ""})
        self.build = self.server_main.build_llm_fallbacks_from_env

    def test_env_wired_into_llm_config(self):
        server_main = _load_app(
            {"MEM0_LLM_FALLBACK_MODEL": "deepseek-chat", "MEM0_LLM_FALLBACK_API_KEY": "sk-fb"}
        )
        llm_config = server_main.DEFAULT_CONFIG["llm"]
        assert llm_config["fallbacks"] == [
            {"provider": "openai", "config": {"model": "deepseek-chat", "api_key": "sk-fb"}}
        ]
        # fallbacks 必须在 llm 层，不能污染 llm.config（否则 OpenAIConfig(**config) 抛 TypeError）
        assert "fallbacks" not in server_main.DEFAULT_CONFIG["llm"]["config"]
        # layer_timeout 同样在 llm 层（与 fallbacks 平级），默认 60s
        assert server_main.DEFAULT_CONFIG["llm"]["layer_timeout"] == 60.0

    def test_no_fallback_env_returns_empty(self):
        assert self.build({}) == []
        assert self.build({"MEM0_LLM_FALLBACK_API_KEY": "sk-x"}) == []
        assert self.build({"MEM0_LLM_FALLBACK2_REASONING_EFFORT": "high"}) == []

    def test_fallback1_only(self):
        assert self.build(
            {
                "MEM0_LLM_FALLBACK_MODEL": "deepseek-chat",
                "MEM0_LLM_FALLBACK_BASE_URL": "https://api.deepseek.com/v1",
                "MEM0_LLM_FALLBACK_API_KEY": "sk-1",
            }
        ) == [
            {
                "provider": "openai",
                "config": {
                    "model": "deepseek-chat",
                    "openai_base_url": "https://api.deepseek.com/v1",
                    "api_key": "sk-1",
                },
            }
        ]

    def test_fallback1_and_2_with_reasoning_effort(self):
        fb = self.build(
            {
                "MEM0_LLM_FALLBACK_MODEL": "deepseek-chat",
                "MEM0_LLM_FALLBACK2_MODEL": "gpt-4o",
                "MEM0_LLM_FALLBACK2_BASE_URL": "https://openai.example/v1",
                "MEM0_LLM_FALLBACK2_REASONING_EFFORT": "high",
            }
        )
        assert len(fb) == 2
        assert fb[0] == {"provider": "openai", "config": {"model": "deepseek-chat"}}
        assert fb[1] == {
            "provider": "openai",
            "config": {
                "model": "gpt-4o",
                "openai_base_url": "https://openai.example/v1",
                "reasoning_effort": "high",
            },
        }

    def test_empty_api_key_not_written(self):
        fb = self.build(
            {
                "MEM0_LLM_FALLBACK_MODEL": "deepseek-chat",
                "MEM0_LLM_FALLBACK_API_KEY": "",
                "MEM0_LLM_FALLBACK2_MODEL": "gpt-4o",
                "MEM0_LLM_FALLBACK2_API_KEY": None,
            }
        )
        assert fb[0]["config"] == {"model": "deepseek-chat"}
        assert fb[1]["config"] == {"model": "gpt-4o"}
        assert "api_key" not in fb[0]["config"]
        assert "api_key" not in fb[1]["config"]


class TestConfigurePersistsToConfigFile:
    """POST /configure atomically writes config.json; DB overrides no longer written."""

    @pytest.fixture(autouse=True)
    def _setup(self, _mock_memory, tmp_path):
        import server_state

        self.mock_save_config, self.mock_save_overrides = _mock_memory[1], _mock_memory[2]
        self.server_state = server_state
        # Fresh config file per test: the merge assertions below depend on the
        # on-disk baseline, so sharing one session-wide file leaks state.
        self.server_main = _load_app(
            {"ADMIN_API_KEY": "", "MEM0_CONFIG_PATH": str(tmp_path / "config.json")}
        )
        self.client = TestClient(self.server_main.app)

    def test_configure_writes_merged_config_to_file(self):
        fallbacks = [{"provider": "openai", "config": {"model": "deepseek-chat", "api_key": "sk-fb"}}]
        self.client.post("/configure", json={"llm": {"fallbacks": fallbacks}})
        self.mock_save_config.assert_called_once()
        path, saved = self.mock_save_config.call_args.args
        assert path == self.server_state._config_file_path()
        assert saved["llm"]["fallbacks"] == fallbacks
        assert "embedder" in saved

    def test_configure_no_longer_writes_db_overrides(self):
        self.client.post("/configure", json={"llm": {"provider": "openai"}})
        self.mock_save_overrides.assert_not_called()

    def test_save_config_failure_keeps_current_config(self):
        self.mock_save_config.side_effect = OSError("disk full")
        before = self.server_state.get_current_config()
        with pytest.raises(OSError):
            self.server_state.update_config({"llm": {"provider": "openai"}})
        assert self.server_state.get_current_config() == before
