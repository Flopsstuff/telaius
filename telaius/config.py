"""Settings: the environment first, then an .env file.

TELAIUS_ENV_FILE points at that file; without it .env in the repository root is used, which is
what a worktree checkout needs (the owner's key lives in the main checkout).
"""
import os
import tomllib
from pathlib import Path

from telaius.errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[1]


def env_file():
    return Path(os.environ["TELAIUS_ENV_FILE"]) if os.environ.get("TELAIUS_ENV_FILE") else REPO_ROOT / ".env"


def env_value(name, default=None):
    """A setting from the environment, else from the .env file, else `default`."""
    if os.environ.get(name):
        return os.environ[name]
    env = env_file()
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith(f"{name}=") and line.split("=", 1)[1].strip():
                return line.split("=", 1)[1].strip()
    return default


def api_key():
    key = env_value("XAI_API_KEY")
    if not key:
        raise ConfigError(f"XAI_API_KEY is not set (environment or {env_file()})")
    return key


def config_file():
    return Path(os.environ.get("TELAIUS_CONFIG", REPO_ROOT / "telaius.toml"))


def load_config():
    path = config_file()
    if not path.exists():
        return {}
    try:
        with path.open("rb") as source:
            return tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot read {path}: {error}") from error


def setting(name, default=None, config=None):
    """Resolve a simple setting as environment, TOML, then default."""
    env_name = f"TELAIUS_{name.upper()}"
    value = env_value(env_name)
    if value is not None:
        return value
    return (config if config is not None else load_config()).get(name, default)


def calls_path(config=None):
    path = Path(setting("calls_dir", "calls", config if config is not None else load_config()))
    return path if path.is_absolute() else REPO_ROOT / path
