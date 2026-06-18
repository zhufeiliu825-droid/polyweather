import json

from polyweather.util.atomicio import dump_json_atomic, write_text_atomic


def test_write_text_atomic(tmp_path):
    p = tmp_path / "x.txt"
    write_text_atomic(p, "hello")
    assert p.read_text() == "hello"
    assert not (tmp_path / "x.txt.tmp").exists()        # the temp file is consumed by the atomic rename


def test_dump_json_atomic_roundtrips(tmp_path):
    p = tmp_path / "d.json"
    dump_json_atomic(p, {"a": 1, "b": [2, 3]}, indent=2)
    assert json.loads(p.read_text()) == {"a": 1, "b": [2, 3]}
    assert not (tmp_path / "d.json.tmp").exists()


def test_atomic_overwrite_replaces_cleanly(tmp_path):
    p = tmp_path / "o.json"
    dump_json_atomic(p, {"v": 1})
    dump_json_atomic(p, {"v": 2})
    assert json.loads(p.read_text()) == {"v": 2}
