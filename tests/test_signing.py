import hashlib
import hmac

from app.signing import beijing_now_iso, sign_douyin, sign_sf, sign_taobao, unix_timestamp_millis, unix_timestamp_seconds


def test_sign_taobao_known_vector():
    params = {"b": "2", "a": "1", "empty": "", "none": None}
    expected = hashlib.md5(b"a1b2secret").hexdigest().upper()
    assert sign_taobao(params, "secret") == expected


def test_sign_douyin_md5_vector():
    params = {"app_id": "1", "method": "order.list", "param_json": "{}", "v": "2", "sign_method": "md5"}
    expected = hashlib.md5(b"app_id1methodorder.listparam_json{}sign_methodmd5v2secret").hexdigest()
    assert sign_douyin(params, "secret", "md5") == expected


def test_sign_douyin_hmac_underscore_alias():
    params = {"app_id": "1", "method": "order.list", "param_json": "{}", "v": "2", "sign_method": "hmac-sha256"}
    base = b"app_id1methodorder.listparam_json{}sign_methodhmac-sha256v2secret"
    expected = hmac.new(b"secret", base, hashlib.sha256).hexdigest()
    assert sign_douyin(params, "secret", "hmac_sha256") == expected


def test_sign_sf_deterministic_uppercase():
    digest = sign_sf('{"a":1}', "1700000000000", "check")
    assert digest == hashlib.md5(b'{"a":1}1700000000000check').hexdigest().upper()
    assert digest == sign_sf('{"a":1}', "1700000000000", "check")


def test_timestamp_helpers():
    assert len(beijing_now_iso()) == 19
    assert int(unix_timestamp_seconds()) > 1_600_000_000
    assert int(unix_timestamp_millis()) >= int(unix_timestamp_seconds()) * 1000
