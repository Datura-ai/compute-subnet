from datura.requests.validator_requests import fresh_upload_nonce, upload_nonce_issued_at


def test_dated_nonce_round_trip():
    nonce = fresh_upload_nonce(1_760_000_000)
    assert len(bytes.fromhex(nonce)) == 32
    assert upload_nonce_issued_at(nonce) == 1_760_000_000
    assert fresh_upload_nonce(1_760_000_000) != nonce


def test_other_nonces_are_undated():
    for nonce in (None, "", "ab" * 32, "zz" * 32, fresh_upload_nonce()[:-2]):
        assert upload_nonce_issued_at(nonce) is None
