import base64

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

import dashboard_features as features
from open_teleset import operator_store


class _User:
    id = "operator-1"
    role = "admin"


class _State:
    user = _User()


class _Request:
    state = _State()


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    yield


def test_feature_toggles_default_on_and_persist():
    assert operator_store.get_features() == {"automation_enabled": True, "factory_enabled": True}
    updated = operator_store.set_features({"factory_enabled": False})
    assert updated == {"automation_enabled": True, "factory_enabled": False}
    assert operator_store.feature_enabled("factory") is False
    assert operator_store.feature_enabled("automation") is True


def test_totp_matches_rfc_6238_vector():
    secret = base64.b32encode(b"12345678901234567890").decode()
    code, remaining = features._totp(secret, digits=6, period=30, at=59)
    assert code == "287082"
    assert remaining == 1


def test_totp_accepts_otpauth_uri():
    secret = base64.b32encode(b"12345678901234567890").decode()
    code, _ = features._totp(f"otpauth://totp/Test?secret={secret}&digits=6&period=30", at=59)
    assert code == "287082"


@pytest.mark.asyncio
async def test_ai_settings_never_return_the_api_key():
    saved = await features.update_ai_settings(
        features.AISettingsUpdate(
            provider="custom",
            base_url="https://ai.example.com/v1",
            model="team/model",
            api_key="sk-secret-value",
        ),
        _Request(),
    )
    assert saved["has_key"] is True
    assert saved["provider"] == "custom"
    assert "sk-secret-value" not in str(saved)
    reloaded = await features.ai_settings(_Request())
    assert "sk-secret-value" not in str(reloaded)
    assert features._ai_keys("operator-1") == {"custom": "sk-secret-value"}


@pytest.mark.asyncio
async def test_ai_settings_reject_a_non_https_provider():
    with pytest.raises(HTTPException) as exc:
        await features.update_ai_settings(
            features.AISettingsUpdate(provider="custom", base_url="http://localhost:11434/v1", model="x"),
            _Request(),
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_recovery_vault_lists_metadata_but_not_secrets():
    await features.save_recovery(
        "acc-1",
        features.RecoveryInput(
            login_email="owner@example.com",
            email_password="mail-pass",
            otp_secret=base64.b32encode(b"12345678901234567890").decode(),
        ),
        _Request(),
    )
    listed = await features.list_recovery(_Request())
    assert listed["entries"][0]["fields"] == ["email_password", "login_email", "otp_secret"]
    assert "mail-pass" not in str(listed)
    assert "owner@example.com" not in str(listed)
    otp = await features.recovery_otp("acc-1", _Request())
    assert len(otp["code"]) == 6


@pytest.mark.asyncio
async def test_recovery_clear_removes_a_field():
    await features.save_recovery("acc-2", features.RecoveryInput(twofa_password="pw"), _Request())
    await features.save_recovery("acc-2", features.RecoveryInput(clear=["twofa_password"]), _Request())
    listed = await features.list_recovery(_Request())
    assert listed["entries"][0]["fields"] == []


@pytest.mark.asyncio
async def test_notification_settings_store_the_token_encrypted():
    settings = await features.update_notification_settings(
        features.NotificationUpdate(
            enabled=True,
            chat_id="-1001234567890",
            topic_id="42",
            events=["outreach.job.completed"],
            bot_token="123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        ),
        _Request(),
    )
    assert settings["has_token"] is True
    assert "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in str(settings)
    stored = (operator_store.store_dir() / "notification_tokens.secret.json").read_text()
    assert "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in stored


@pytest.mark.asyncio
async def test_integration_token_is_write_only():
    saved = await features.save_integration(
        features.IntegrationInput(name="Airtable", base_url="https://mcp.airtable.com/mcp", api_key="tok-123"),
        _Request(),
    )
    assert saved["integration"]["has_token"] is True
    listed = await features.list_integrations(_Request())
    assert "tok-123" not in str(listed)


class _FakeContact:
    def __init__(self, user_id, first_name, username="", phone=""):
        self.id = user_id
        self.first_name = first_name
        self.last_name = ""
        self.username = username
        self.phone = phone
        self.bot = False
        self.deleted = False
        self.mutual_contact = True
        self.status = None


class _FakeContactsResult:
    users = [_FakeContact(1, "Ana", "ana", "+639171234567"), _FakeContact(2, "Ben")]


class _FakeClient:
    async def __call__(self, request):
        assert request.__class__.__name__ == "GetContactsRequest"
        return _FakeContactsResult()


@pytest.mark.asyncio
async def test_contacts_endpoint_filters_and_serialises(monkeypatch):
    async def fake_client(account_id):
        return _FakeClient()

    monkeypatch.setattr(features, "_client", fake_client)
    result = await features.list_contacts("acc", q="ana")
    assert result["total"] == 1
    assert result["contacts"][0]["username"] == "ana"
    assert result["contacts"][0]["mutual"] is True
