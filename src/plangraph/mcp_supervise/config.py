"""Configuration loader and validator for mcp_supervise."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


class ConfigError(ValueError):
    """Raised when configuration validation fails."""


@dataclass
class ServerConfig:
    name: str
    port: int
    cmd: List[str]
    scope: str
    env: Dict[str, str] = field(default_factory=dict)
    project_dir: Optional[str] = None
    proxy: bool = True
    host: str = "127.0.0.1"


@dataclass
class SuperviseConfig:
    servers: List[ServerConfig]
    log_file: Optional[str] = None
    state_dir: Optional[str] = None


def _expand_path(p: Optional[str]) -> Optional[str]:
    if p is None:
        return None
    return os.path.expanduser(os.path.expandvars(p))


def _validate_server(raw: Any, index: int) -> ServerConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"Server at index {index} must be an object")

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"Server at index {index} must have a non-empty string 'name'")

    port = raw.get("port")
    if not isinstance(port, int) or isinstance(port, bool):
        raise ConfigError(f"Server '{name}' port must be an integer")
    if port < 1024 or port > 65535:
        raise ConfigError(f"Server '{name}' port {port} out of range [1024, 65535]")

    host = raw.get("host", "127.0.0.1")
    if host != "127.0.0.1":
        raise ConfigError(f"Server '{name}' host must be 127.0.0.1, got '{host}'")

    scope = raw.get("scope")
    if scope not in ("host", "project"):
        raise ConfigError(f"Server '{name}' scope must be 'host' or 'project', got '{scope}'")

    project_dir = raw.get("project_dir")
    if project_dir is not None and not isinstance(project_dir, str):
        raise ConfigError(f"Server '{name}' project_dir must be a string or null")

    cmd = raw.get("cmd")
    if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) and c for c in cmd):
        raise ConfigError(f"Server '{name}' cmd must be a non-empty list of strings")

    env_raw = raw.get("env", {})
    if not isinstance(env_raw, dict):
        raise ConfigError(f"Server '{name}' env must be a dictionary")
    env: Dict[str, str] = {}
    for k, v in env_raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ConfigError(f"Server '{name}' env keys and values must be strings")
        env[k] = v

    proxy = raw.get("proxy", True)
    if not isinstance(proxy, bool):
        raise ConfigError(f"Server '{name}' proxy must be a boolean")

    return ServerConfig(
        name=name,
        port=port,
        cmd=cmd,
        scope=scope,
        env=env,
        project_dir=_expand_path(project_dir),
        proxy=proxy,
        host=host,
    )


def load_config(source: Any) -> SuperviseConfig:
    """Load and validate SuperviseConfig from file path, string, or dict."""
    if isinstance(source, (str, Path)):
        p = Path(os.path.expanduser(os.path.expandvars(str(source))))
        if not p.is_file():
            raise ConfigError(f"Config file not found: {source}")
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigError(f"Invalid JSON in config file {source}: {e}") from e
    elif isinstance(source, dict):
        data = source
    else:
        raise ConfigError("Config source must be a file path or dict")

    if not isinstance(data, dict):
        raise ConfigError("Top-level config must be a JSON object")

    raw_servers = data.get("servers")
    if not isinstance(raw_servers, list):
        raise ConfigError("Config must have a 'servers' list")

    names = set()
    ports = set()
    servers: List[ServerConfig] = []

    for i, s_raw in enumerate(raw_servers):
        srv = _validate_server(s_raw, i)
        if srv.name in names:
            raise ConfigError(f"Duplicate server name: '{srv.name}'")
        if srv.port in ports:
            raise ConfigError(f"Duplicate server port: {srv.port}")
        names.add(srv.name)
        ports.add(srv.port)
        servers.append(srv)

    log_file = data.get("log_file")
    if log_file is not None and not isinstance(log_file, str):
        raise ConfigError("log_file must be a string or null")

    state_dir = data.get("state_dir")
    if state_dir is not None and not isinstance(state_dir, str):
        raise ConfigError("state_dir must be a string or null")

    return SuperviseConfig(
        servers=servers,
        log_file=_expand_path(log_file),
        state_dir=_expand_path(state_dir),
    )
