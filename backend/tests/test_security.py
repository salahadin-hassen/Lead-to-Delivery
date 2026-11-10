from app.security import (
    TOKEN_HASH_RE,
    constant_time_equals,
    generate_token,
    hash_password,
    hash_token,
    is_valid_password_hash,
    verify_password,
)


def test_password_hash_round_trip() -> None:
    stored = hash_password("correct horse battery")
    assert stored.startswith("scrypt$")
    assert "correct horse battery" not in stored
    assert is_valid_password_hash(stored)
    assert verify_password("correct horse battery", stored)
    assert not verify_password("wrong password", stored)


def test_same_password_gets_different_salts() -> None:
    assert hash_password("same password!") != hash_password("same password!")


def test_missing_or_malformed_hash_never_authenticates() -> None:
    assert not verify_password("anything", None)
    assert not verify_password("anything", "")
    assert not verify_password("anything", "plaintext")
    assert not verify_password("anything", "scrypt$16384$8$1$!!!$!!!")
    assert not verify_password("", hash_password("x" * 12))


def test_tokens_are_high_entropy_and_stored_as_sha256_hex() -> None:
    tokens = {generate_token() for _ in range(200)}
    assert len(tokens) == 200
    token = tokens.pop()
    assert len(token) >= 43  # 32 random bytes, URL-safe base64
    digest = hash_token(token)
    assert TOKEN_HASH_RE.match(digest)
    assert digest == hash_token(token)
    assert token not in digest


def test_constant_time_equals() -> None:
    assert constant_time_equals("abc", "abc")
    assert not constant_time_equals("abc", "abd")
    assert not constant_time_equals("abc", "abcd")
