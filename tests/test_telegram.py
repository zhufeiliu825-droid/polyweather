from polyweather.notify.telegram import _parse_reply


def test_parse_simple_bet():
    assert _parse_reply("london bet 23") == {"city": "london", "action": "bet", "bucket": "23"}


def test_parse_simple_skip():
    assert _parse_reply("london skip") == {"city": "london", "action": "skip"}


def test_parse_multiword_city():
    # multi-word city → slug (spaces removed) so it matches the daemon's by_slug keys
    assert _parse_reply("mexico city skip") == {"city": "mexicocity", "action": "skip"}
    assert _parse_reply("cape town bet 18") == {"city": "capetown", "action": "bet", "bucket": "18"}


def test_parse_bet_without_bucket():
    assert _parse_reply("sao paulo bet") == {"city": "saopaulo", "action": "bet", "bucket": None}


def test_parse_garbage():
    assert _parse_reply("garbage") is None
    assert _parse_reply("") is None
    assert _parse_reply("bet") is None        # no city before the keyword


def test_parse_rejects_malformed_bucket_tokens():
    # '--3' and unicode '³' pass lstrip('-').isdigit()/str.isdigit() but crash int() downstream
    # (which aborted the rest of an already-ACKed reply batch — audit r5). They must drop to None.
    assert _parse_reply("london bet --3") == {"city": "london", "action": "bet", "bucket": None}
    assert _parse_reply("london bet ³") == {"city": "london", "action": "bet", "bucket": None}


def test_parse_accepts_negative_bucket():
    # a legitimately-negative threshold (a cold city's sub-zero bucket) still parses as int
    assert _parse_reply("helsinki bet -5") == {"city": "helsinki", "action": "bet", "bucket": "-5"}


def test_parse_picks_first_intlike_token():
    # skips a non-numeric word and takes the first token that genuinely parses as int
    assert _parse_reply("london bet please 23") == {"city": "london", "action": "bet", "bucket": "23"}
