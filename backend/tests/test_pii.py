"""PII Guard: masking, restoring, phone normalisation."""
from __future__ import annotations

import pytest

from arix.core.pii import PIIVault, looks_like_phone, mask_text, normalize_phone


def test_masks_and_restores_round_trip():
    original = (
        "Меня зовут Айгерим, телефон +7 777 123 45 67, почта a@b.kz, ИИН 870512300123"
    )
    masked, vault = mask_text(original, extra_names=["Айгерим"])
    assert "+7 777 123 45 67" not in masked
    assert "a@b.kz" not in masked
    assert "870512300123" not in masked
    assert "Айгерим" not in masked
    assert vault.restore(masked) == original


def test_same_value_gets_the_same_token():
    text = "звоните на +77771234567 или на +77771234567"
    masked, vault = mask_text(text)
    assert masked.count("[PHONE_1]") == 2
    assert len(vault.backward) == 1


def test_prices_and_times_are_not_masked():
    masked, vault = mask_text("Чистка стоит 15 000 тенге, приём в 10:00, кабинет 305")
    assert masked == "Чистка стоит 15 000 тенге, приём в 10:00, кабинет 305"
    assert vault.is_empty


def test_masking_can_be_disabled():
    text = "+77771234567"
    masked, vault = mask_text(text, enabled=False)
    assert masked == text
    assert vault.is_empty


def test_vault_is_reusable_across_calls():
    vault = PIIVault()
    first, _ = mask_text("мой номер +77771234567", vault)
    second, _ = mask_text("повторю: +77771234567", vault)
    assert first.split()[-1] == second.split()[-1]


def test_restore_is_a_noop_for_unknown_tokens():
    vault = PIIVault()
    assert vault.restore("[PHONE_9] не знаю") == "[PHONE_9] не знаю"


@pytest.mark.parametrize(
    "raw,country,expected",
    [
        ("87771234567", "KZ", "+77771234567"),
        ("7771234567", "KZ", "+77771234567"),
        ("+7 777 123 45 67", "KZ", "+77771234567"),
        ("901234567", "UZ", "+998901234567"),
        ("998901234567", "UZ", "+998901234567"),
        ("+998 90 123 45 67", "UZ", "+998901234567"),
    ],
)
def test_phone_normalisation(raw, country, expected):
    assert normalize_phone(raw, country) == expected


def test_normalize_phone_handles_empty():
    assert normalize_phone(None) is None
    assert normalize_phone("не скажу") is None


def test_looks_like_phone_requires_enough_digits():
    assert looks_like_phone("+7 701 234 56 78")
    assert not looks_like_phone("15 000")
    assert not looks_like_phone("10:00")
