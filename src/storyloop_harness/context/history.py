"""Deterministic recipient-owned history ranking and excerpting."""
import re


def _excerpt(text, terms, limit):
    if limit <= 0:
        return ''
    if len(text) <= limit:
        return text
    # Match with the same normalization as ranking, then map expansions such as
    # ß -> ss back to the original characters before slicing the excerpt.
    folded_parts = [character.casefold() for character in text]
    folded_text = ''.join(folded_parts)
    original_offsets = [index for index, part in enumerate(folded_parts)
                        for _ in part]
    first = min(((original_offsets[start], original_offsets[start + len(term) - 1] + 1)
                 for term in sorted(terms)
                 if (start := folded_text.find(term)) >= 0), default=None)
    if first is not None:
        # One contiguous window keeps nearby clues together without repeating text.
        payload = limit - 6 if limit > 6 else limit
        match_start, match_end = first
        padding = max(0, payload - (match_end - match_start)) // 2
        start = max(0, min(match_start - padding, len(text) - payload))
        end = start + payload
        excerpt = text[start:end]
        if limit > 6:
            excerpt = (' … ' if start else '') + excerpt + (' … ' if end < len(text) else '')
        return excerpt
    if limit <= 3:
        return text[:limit]
    head = (limit - 3) // 2
    tail = limit - 3 - head
    return text[:head] + ' … ' + (text[-tail:] if tail else '')


def history_selection(entries, query, *, recent, older, excerpt_chars):
    terms = set()
    for word in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]{3,}", query.casefold()):
        if re.search(r"[\u4e00-\u9fff]", word):
            terms.update(word[index:index + 2] for index in range(len(word) - 1))
        else:
            terms.add(word)
    split = max(0, len(entries) - recent) if recent else len(entries)
    notable = {'private_message', 'campaign_choice', 'shared_experience',
               'witnessed', 'outgoing_message', 'background'}
    def relevance(entry):
        return sum(term in entry.content.casefold() for term in terms)
    ranked = sorted(range(split), key=lambda index: (
        relevance(entries[index]) * 4 + (2 if entries[index].channel in notable else 0),
        index), reverse=True)
    chosen = set(ranked[:older])
    for index, entry in enumerate(entries):
        selected = index >= split or index in chosen
        reason = ('recent' if index >= split else 'relevant' if relevance(entry)
                  else 'notable' if entry.channel in notable else 'ranked')
        text = _excerpt(entry.content, terms, excerpt_chars)
        yield entry, {'channel': entry.channel, 'text': text, 'tick': entry.tick}, selected, reason
