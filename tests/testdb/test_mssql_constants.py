from __future__ import annotations

import pytest

from pgdevkit.testdb.mssql.constants import conninfo, validate_sa_password


def test_validate_sa_password_accepts_complexity_valid_password():
    validate_sa_password("TestPwd!2026")  # must not raise


def test_validate_sa_password_rejects_too_short():
    with pytest.raises(ValueError, match="8 characters"):
        validate_sa_password("Ab1!")


def test_validate_sa_password_rejects_containing_login_name():
    with pytest.raises(ValueError, match="'sa'"):
        validate_sa_password("Sup3rSaPass!")


def test_validate_sa_password_rejects_insufficient_character_classes():
    with pytest.raises(ValueError, match="3 of"):
        validate_sa_password("alllowercase")


def test_conninfo_omits_driver_and_app_keys():
    cs = conninfo("mydb")
    assert "Driver=" not in cs
    assert "APP=" not in cs
    assert "Database=mydb" in cs
