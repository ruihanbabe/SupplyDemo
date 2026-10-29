"""Encrypted independent fixture recipient and signature fail-closed boundaries."""

import asyncio
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from product_api.channels import ChannelCipher, ChannelError, FixtureAdapter, configured_cipher


def test_channel_defaults_and_wrong_key_never_reveal_payload(monkeypatch):
    monkeypatch.delenv("SUPPLYAGENT_CHANNEL_MODE", raising=False)
    with pytest.raises(ChannelError, match="config"):
        configured_cipher()
    first, other = ChannelCipher(Fernet.generate_key()), ChannelCipher(Fernet.generate_key())
    value = {"body": "SYNTHETIC private message"}
    content = first.proposal_content(value)
    assert "private message" not in content["sealedPayload"]
    assert first.open(content["sealedPayload"]) == value
    assert first.proposal_content(value)["payloadTag"] == content["payloadTag"]
    with pytest.raises(ChannelError, match="invalid"):
        other.open(content["sealedPayload"])


def test_fixture_committed_unknown_response_is_reconciled_after_adapter_restart(tmp_path):
    async def scenario():
        cipher = ChannelCipher(Fernet.generate_key())
        path = tmp_path / "fixture.sqlite"
        logical_id = str(uuid4())
        value = {"destination": "SYNTHETIC-recipient", "body": "SYNTHETIC-private"}
        remote = FixtureAdapter(path, cipher, lose_response=True)
        with pytest.raises(TimeoutError):
            await remote.send(logical_id, value)
        restarted = FixtureAdapter(path, cipher)
        receipt = await restarted.lookup(logical_id)
        assert receipt.state == "accepted" and restarted.send_calls == 0
        assert await restarted.send(logical_id, value) == receipt
        with pytest.raises(ChannelError, match="conflict"):
            await restarted.send(logical_id, {**value, "body": "changed"})
        assert b"SYNTHETIC-private" not in path.read_bytes()
        assert b"SYNTHETIC-recipient" not in path.read_bytes()
    asyncio.run(scenario())
