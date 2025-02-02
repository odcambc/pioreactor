# -*- coding: utf-8 -*-
# test_pubsub.py
from __future__ import annotations

import socket
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from paho.mqtt.client import Client

from pioreactor.pubsub import add_hash_suffix
from pioreactor.pubsub import create_client


def test_add_hash_suffix():
    s = "test_string"
    result = add_hash_suffix(s)
    assert len(result) == len(s) + 5  # 4 random characters and a hyphen
    assert result[: len(s)] == s
    assert result[len(s)] == "-"


@pytest.fixture
def mock_client():
    with patch("pioreactor.pubsub.Client") as mock_client:
        yield mock_client


def test_create_client_default_behavior(mock_client):
    hostname = "test_hostname"
    client_instance = MagicMock(spec=Client)
    mock_client.return_value = client_instance

    create_client(hostname=hostname)

    client_instance.username_pw_set.assert_called_with("pioreactor", "raspberry")
    client_instance.connect.assert_called_with(hostname, keepalive=60)
    client_instance.loop_start.assert_called_once()


def test_create_client_with_last_will(mock_client):
    hostname = "test_hostname"
    client_instance = MagicMock(spec=Client)
    mock_client.return_value = client_instance

    last_will = {"topic": "test/topic", "payload": "test_payload", "retain": True}

    create_client(hostname=hostname, last_will=last_will)

    client_instance.will_set.assert_called_with(**last_will)


def test_create_client_with_custom_on_connect(mock_client):
    hostname = "test_hostname"
    client_instance = MagicMock(spec=Client)
    mock_client.return_value = client_instance

    on_connect = MagicMock()
    create_client(hostname=hostname, on_connect=on_connect)

    assert client_instance.on_connect == on_connect


def test_create_client_with_custom_on_message(mock_client):
    hostname = "test_hostname"
    client_instance = MagicMock(spec=Client)
    mock_client.return_value = client_instance

    on_message = MagicMock()
    create_client(hostname=hostname, on_message=on_message)

    assert client_instance.on_message == on_message


def test_create_client_max_connection_attempts(mock_client):
    hostname = "test_hostname"
    client_instance = MagicMock(spec=Client)
    mock_client.return_value = client_instance
    client_instance.connect.side_effect = socket.gaierror()

    max_connection_attempts = 3
    create_client(hostname=hostname, max_connection_attempts=max_connection_attempts)

    assert client_instance.connect.call_count == max_connection_attempts
