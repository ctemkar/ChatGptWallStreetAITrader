from wallstreet_ls.news_cli import headline_sentiment


def test_headline_sentiment_is_bounded_and_directional():
    assert headline_sentiment("Company beats estimates and raises guidance") > 0
    assert headline_sentiment("Company misses estimates amid fraud probe") < 0
    assert -1 <= headline_sentiment("profit growth record wins approval") <= 1
