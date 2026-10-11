from types import SimpleNamespace

import pytest

from storyloop_harness.context.history import history_selection


def select(text, query, limit=80):
    entry = SimpleNamespace(content=text, channel='scene', tick=1)
    return list(history_selection([entry], query, recent=0, older=1, excerpt_chars=limit))[0]


@pytest.mark.parametrize(('clue', 'query'), [('hidden sapphire clue', 'SAPPHIRE'), ('铜钥匙藏在井底', '铜钥匙')])
def test_middle_query_clue_survives_excerpt(clue, query):
    result = select('a' * 300 + clue + 'z' * 300, query)
    assert clue in result[1]['text']
    assert result[2:] == (True, 'relevant')
    assert len(result[1]['text']) <= 80


def test_multiple_nearby_matches_preserve_context_without_duplication():
    text = 'x' * 300 + 'sapphire beside emerald' + 'z' * 300
    excerpt = select(text, 'emerald sapphire')[1]['text']
    assert 'sapphire beside emerald' in excerpt
    assert excerpt.count('sapphire') == excerpt.count('emerald') == 1
    assert excerpt == select(text, 'sapphire emerald')[1]['text']


def test_no_match_keeps_head_tail_fallback():
    result = select('HEAD' + 'x' * 300 + 'TAIL', 'missing', 15)
    assert result[1]['text'] == 'HEADxx … xxTAIL'
    assert result[3] == 'ranked'


@pytest.mark.parametrize('limit', range(9))
@pytest.mark.parametrize('query', ['missing', 'sapphire'])
def test_tiny_limits_are_bounded_and_deterministic(limit, query):
    text = 'HEAD' + 'x' * 100 + 'sapphire' + 'z' * 100 + 'TAIL'
    excerpt = select(text, query, limit)[1]['text']
    assert len(excerpt) <= limit
    assert excerpt == select(text, query, limit)[1]['text']


def test_recent_and_older_ranking_stays_unchanged():
    entries = [SimpleNamespace(content=text, channel=channel, tick=i) for i, (text, channel) in enumerate([
        ('sapphire', 'scene'), ('no clue', 'background'), ('recent', 'scene')])]
    results = list(history_selection(entries, 'sapphire', recent=1, older=1, excerpt_chars=80))
    assert [(r[2], r[3]) for r in results] == [(True, 'relevant'), (False, 'notable'), (True, 'recent')]


def test_distant_matches_choose_earliest_clue_deterministically():
    text = 'x' * 300 + 'sapphire clue' + 'y' * 300 + 'emerald clue' + 'z' * 300
    excerpt = select(text, 'emerald sapphire')[1]['text']
    assert 'sapphire clue' in excerpt
    assert 'emerald' not in excerpt
    assert len(excerpt) <= 80
    assert excerpt == select(text, 'sapphire emerald')[1]['text']


def test_long_query_term_that_fits_is_not_cut_in_half():
    clue = 'abcdefghijklmnopqrstuvwxabcdefghijklmnopqrstuvwx'
    excerpt = select('x' * 300 + clue + 'z' * 300, clue)[1]['text']
    assert clue in excerpt


def test_casefold_expansion_preserves_middle_clue_and_original_offsets():
    text = 'ß' * 300 + 'Straße clue' + 'z' * 300
    result = select(text, 'STRASSE')
    assert result[2:] == (True, 'relevant')
    assert 'Straße clue' in result[1]['text']
    assert len(result[1]['text']) <= 80
    assert result[1]['text'] == select(text, 'STRASSE')[1]['text']
