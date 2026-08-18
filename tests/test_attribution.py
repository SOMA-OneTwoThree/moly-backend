"""Meta 설치 리퍼러 복호화 — 자체 생성 AES-256-GCM 벡터로 왕복, 귀속 없음/키 불일치/키 미설정."""
import json
import os
import urllib.parse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient

from app.main import app
from app.services import attribution

client = TestClient(app)
PATH = "/attribution/meta-referrer/decrypt"

KEY = os.urandom(32).hex()
PAYLOAD = {
    "ad_id": "120200000000000000",
    "adgroup_id": "120200000000000001",
    "adgroup_name": "광고세트 A",
    "campaign_id": "120200000000000002",
    "campaign_name": "캠페인 A",
    "campaign_group_id": "120200000000000003",
    "campaign_group_name": "캠페인 그룹 A",
    "account_id": "100000000000000",
    "ad_objective_name": "APP_INSTALLS",
}


def _utm_content(key_hex: str = KEY, payload: dict | None = None) -> str:
    """Meta가 보내는 utm_content를 직접 만든다 — 인증 태그는 암호문 뒤에 붙어 나온다."""
    nonce = os.urandom(12)
    plaintext = json.dumps(payload if payload is not None else PAYLOAD).encode()
    data = AESGCM(bytes.fromhex(key_hex)).encrypt(nonce, plaintext, None)
    return json.dumps(
        {"source": {"data": data.hex(), "nonce": nonce.hex()}, "a": "1234567890", "t": 1700000000}
    )


def _use_key(monkeypatch, key_hex: str) -> None:
    monkeypatch.setattr(attribution.settings, "meta_install_referrer_decryption_key", key_hex)


def test_decrypts_meta_ciphertext(monkeypatch):
    """평문 끝 16바이트를 잘라내면 JSON이 깨진다 — 왕복이 그대로 성립해야 한다."""
    _use_key(monkeypatch, KEY)
    r = client.post(PATH, json={"utm_content": _utm_content()})
    assert r.status_code == 200
    assert r.json()["attribution"] == PAYLOAD


def test_decrypts_percent_encoded_utm_content(monkeypatch):
    _use_key(monkeypatch, KEY)
    encoded = urllib.parse.quote(_utm_content(), safe="")
    r = client.post(PATH, json={"utm_content": encoded})
    assert r.status_code == 200
    assert r.json()["attribution"]["campaign_name"] == "캠페인 A"


def test_omits_fields_absent_from_ciphertext(monkeypatch):
    _use_key(monkeypatch, KEY)
    r = client.post(PATH, json={"utm_content": _utm_content(payload={"ad_id": "1", "is_ct": True})})
    assert r.status_code == 200
    assert r.json()["attribution"] == {
        "ad_id": "1",
        "adgroup_id": None,
        "adgroup_name": None,
        "campaign_id": None,
        "campaign_name": None,
        "campaign_group_id": None,
        "campaign_group_name": None,
        "account_id": None,
        "ad_objective_name": None,
    }


def test_no_ciphertext_is_null_attribution(monkeypatch):
    """Meta 암호문이 없는 설치(오가닉·타 매체)는 오류가 아니라 attribution=null이다."""
    _use_key(monkeypatch, KEY)
    for utm_content in ("", "some_other_network_value", '{"a":"1234567890","t":1}', "{not json"):
        r = client.post(PATH, json={"utm_content": utm_content})
        assert r.status_code == 200, utm_content
        assert r.json() == {"attribution": None}, utm_content


def test_unparseable_hex_is_null_attribution(monkeypatch):
    _use_key(monkeypatch, KEY)
    body = {"utm_content": json.dumps({"source": {"data": "zz", "nonce": "zz"}})}
    r = client.post(PATH, json=body)
    assert r.status_code == 200 and r.json() == {"attribution": None}


def test_wrong_key_returns_422(monkeypatch):
    """암호문은 있는데 못 푼다 — 재시도해도 같으므로 422."""
    _use_key(monkeypatch, os.urandom(32).hex())
    r = client.post(PATH, json={"utm_content": _utm_content()})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "ATTRIBUTION_DECRYPT_FAILED"


def test_missing_key_config_returns_503(monkeypatch):
    """서버 설정 문제 — 클라는 재시도해도 된다."""
    _use_key(monkeypatch, "")
    r = client.post(PATH, json={"utm_content": _utm_content()})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "ATTRIBUTION_KEY_UNAVAILABLE"


def test_malformed_key_config_returns_503(monkeypatch):
    _use_key(monkeypatch, "not-hex")
    r = client.post(PATH, json={"utm_content": _utm_content()})
    assert r.status_code == 503


def test_endpoint_requires_no_authentication(monkeypatch):
    _use_key(monkeypatch, KEY)
    assert client.post(PATH, json={"utm_content": ""}).status_code == 200


def test_rejects_unknown_request_fields(monkeypatch):
    _use_key(monkeypatch, KEY)
    r = client.post(PATH, json={"utm_content": "", "referrer": "x"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION"
