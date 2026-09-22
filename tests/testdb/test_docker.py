from __future__ import annotations

from pgdevkit.testdb._docker import _parse_tmpfs, resource_kwargs


def test_parse_tmpfs_single_mount():
    assert _parse_tmpfs("/var/lib/postgresql/data:rw,size=512m") == {
        "/var/lib/postgresql/data": "rw,size=512m"
    }


def test_parse_tmpfs_multiple_mounts_semicolon_separated():
    assert _parse_tmpfs("/data:size=512m;/tmp:rw,size=64m") == {
        "/data": "size=512m",
        "/tmp": "rw,size=64m",
    }


def test_parse_tmpfs_path_without_options():
    assert _parse_tmpfs("/tmp") == {"/tmp": ""}


def test_parse_tmpfs_ignores_blank_entries():
    assert _parse_tmpfs("/data:size=512m;;/tmp:size=64m;") == {
        "/data": "size=512m",
        "/tmp": "size=64m",
    }


def test_resource_kwargs_all_unset_returns_empty_dict():
    assert resource_kwargs(tmpfs="", shm_size="", mem_limit="", cpus="") == {}


def test_resource_kwargs_only_includes_set_values():
    kwargs = resource_kwargs(tmpfs="/data:size=512m", shm_size="", mem_limit="1g", cpus="")
    assert kwargs == {"tmpfs": {"/data": "size=512m"}, "mem_limit": "1g"}


def test_resource_kwargs_cpus_converted_to_nano_cpus():
    assert resource_kwargs(tmpfs="", shm_size="", mem_limit="", cpus="1.5") == {
        "nano_cpus": 1_500_000_000
    }


def test_resource_kwargs_shm_size_passed_through():
    assert resource_kwargs(tmpfs="", shm_size="256m", mem_limit="", cpus="") == {"shm_size": "256m"}
