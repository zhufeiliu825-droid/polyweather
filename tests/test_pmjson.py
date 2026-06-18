from polyweather.util.pmjson import loads_maybe, parse_prices, parse_str_list


def test_loads_maybe_decodes_json_string():
    assert loads_maybe('["Yes","No"]') == ["Yes", "No"]


def test_loads_maybe_idempotent_on_list():
    assert loads_maybe(["Yes", "No"]) == ["Yes", "No"]


def test_loads_maybe_none_and_empty():
    assert loads_maybe(None) is None
    assert loads_maybe("") is None


def test_loads_maybe_non_json_string_passthrough():
    assert loads_maybe("not json") == "not json"


def test_parse_prices():
    assert parse_prices('["0.51","0.49"]') == [0.51, 0.49]
    assert parse_prices(None) == []


def test_parse_str_list_keeps_big_token_ids_as_strings():
    raw = '["98022490269692409998126496127597032490334070080325855126491859374983463996227","538"]'
    ids = parse_str_list(raw)
    assert len(ids) == 2
    assert ids[0] == "98022490269692409998126496127597032490334070080325855126491859374983463996227"
    assert isinstance(ids[0], str)


def test_parse_str_list_malformed_returns_empty():
    # un-decodable JSON must NOT iterate into per-character fragments (audit r6 F2)
    assert parse_str_list("[broken") == []
    assert parse_str_list("notjson") == []
    assert parse_str_list({"a": 1}) == []
    assert parse_str_list(None) == []


def test_parse_prices_malformed_returns_empty():
    assert parse_prices("[broken") == []
    assert parse_prices('["x","y"]') == []          # non-numeric items → [], not a crash
    assert parse_prices(None) == []
