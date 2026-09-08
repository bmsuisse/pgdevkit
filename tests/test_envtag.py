from __future__ import annotations

from pathlib import Path

from pgdevkit.envtag import env_allowed, file_env, strip_env_suffix


class TestFileEnv:
    def test_plain_sql_file_is_untagged(self):
        assert file_env(Path("grants.sql")) is None

    def test_init_sql_is_never_a_tag(self):
        assert file_env(Path("user.init.sql")) is None

    def test_prod_suffix_is_the_prod_tag(self):
        assert file_env(Path("grants.prod.sql")) == "prod"

    def test_arbitrary_env_name_is_a_tag(self):
        assert file_env(Path("seed.staging.sql")) == "staging"
        assert file_env(Path("seed.local_test.sql")) == "local_test"

    def test_non_sql_file_is_never_tagged(self):
        assert file_env(Path("widget.test_data.json")) is None


class TestEnvAllowed:
    def test_no_env_requested_allows_everything(self):
        assert env_allowed(Path("grants.prod.sql"), None) is True
        assert env_allowed(Path("grants.sql"), None) is True

    def test_untagged_file_always_allowed(self):
        assert env_allowed(Path("grants.sql"), "prod") is True
        assert env_allowed(Path("grants.sql"), "staging") is True

    def test_matching_tag_allowed(self):
        assert env_allowed(Path("grants.prod.sql"), "prod") is True

    def test_mismatched_tag_disallowed(self):
        assert env_allowed(Path("grants.prod.sql"), "staging") is False
        assert env_allowed(Path("grants.prod.sql"), "local_test") is False

    def test_init_file_always_allowed(self):
        assert env_allowed(Path("user.init.sql"), "prod") is True
        assert env_allowed(Path("user.init.sql"), "staging") is True


class TestStripEnvSuffix:
    def test_untagged_file_unchanged(self):
        assert strip_env_suffix(Path("grants.sql")) == "grants"

    def test_tagged_file_matches_untagged_logical_name(self):
        assert strip_env_suffix(Path("grants.prod.sql")) == "grants"
        assert strip_env_suffix(Path("seed.local_test.sql")) == "seed"

    def test_init_file_keeps_init_in_the_stem(self):
        assert strip_env_suffix(Path("user.init.sql")) == "user.init"
