"""Deterministic recipient-owned history ranking and excerpting."""
import re


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
        text = entry.content
        if len(text) > excerpt_chars:
            head = (excerpt_chars - 3) // 2
            tail = excerpt_chars - 3 - head
            text = text[:head] + ' … ' + text[-tail:] if tail else text[:head] + ' … '
        yield entry, {'channel': entry.channel, 'text': text, 'tick': entry.tick}, selected, reason
