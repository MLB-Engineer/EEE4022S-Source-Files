"""
Broker connection abstraction.

Every other module (schema, command_validation, buffering, adaptive_scoring)
takes a BrokerConnection instance as a constructor/function argument. None of
them import config.yaml directly — that keeps them testable against a mock
broker and portable between local_docker and hostinger_tukzie without any
code changes, only a config swap.
"""

from __future__ import annotations

import os
import ssl
import yaml
from dataclasses import dataclass
from pathlib import Path

import paho.mqtt.client as mqtt  # pip install paho-mqtt


@dataclass
class ProfileConfig:
    name: str
    host: str
    port: int
    tls: bool
    ca_cert_path: str | None
    username: str
    password: str
    topics: dict


def load_profile(config_path: str = "config/config.yaml", profile_override: str | None = None) -> ProfileConfig:
    # NOTE: config_path is resolved relative to the current working
    # directory. Always run scripts from the repo root (e.g.
    # `python -m gateway.sensor_simulator`) so this path resolves,
    # or pass an absolute path explicitly.
    """Load the active (or explicitly named) profile from config.yaml."""
    with open(config_path, "r") as f:
        cfg = yaml.safe_load(f)

    profile_name = profile_override or cfg["active_profile"]
    raw = cfg["profiles"][profile_name]

    password_env_var = raw["password_env_var"]
    password = os.environ.get(password_env_var)
    if password is None:
        raise RuntimeError(
            f"Environment variable {password_env_var} is not set. "
            f"Export it (or put it in a local .env you load before running) "
            f"before connecting to profile '{profile_name}'."
        )

    return ProfileConfig(
        name=profile_name,
        host=raw["host"],
        port=raw["port"],
        tls=raw["tls"],
        ca_cert_path=raw.get("ca_cert_path"),
        username=raw["username"],
        password=password,
        topics=raw["topics"],
    )


class BrokerConnection:
    """Thin wrapper around paho-mqtt, built from a ProfileConfig.

    Downstream modules only ever see this class, never config.yaml or
    profile names directly.
    """

    def __init__(self, profile: ProfileConfig, client_id: str = "tukzie-gateway"):
        self.profile = profile
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        self.client.username_pw_set(profile.username, profile.password)

        if profile.tls:
            if not profile.ca_cert_path or not Path(profile.ca_cert_path).exists():
                raise RuntimeError(
                    f"Profile '{profile.name}' requires TLS but ca_cert_path "
                    f"'{profile.ca_cert_path}' was not found."
                )
            self.client.tls_set(ca_certs=profile.ca_cert_path, tls_version=ssl.PROTOCOL_TLS)

    def connect(self) -> None:
        self.client.connect(self.profile.host, self.profile.port)

    def topic(self, name: str) -> str:
        """Look up a logical topic name (e.g. 'commands') for the active profile."""
        return self.profile.topics[name]


if __name__ == "__main__":
    # Quick manual smoke test:
    #   TUKZIE_LOCAL_MQTT_PASSWORD=xxxx python gateway/broker.py
    profile = load_profile()
    conn = BrokerConnection(profile)
    conn.connect()
    print(f"Connected to profile '{profile.name}' at {profile.host}:{profile.port}")
