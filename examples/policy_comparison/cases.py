"""Versioned, identical input cases for every policy."""
DATASET_ID = 'harbor-context-clues-v1'
CASES = (
    {'id': 'old-clue', 'player_text': 'dockhand brasskey731 break the window'},
    {'id': 'recent-clue', 'player_text': 'dockhand sailmarker824 break the window'},
    {'id': 'unrelated', 'player_text': 'dockhand coastweather999 break the window'},
)


def suite_data(suite: str):
    if suite == 'contracts':
        return DATASET_ID, CASES
    if suite == 'narrative':
        from .narrative import NARRATIVE_CASES, NARRATIVE_DATASET_ID
        return NARRATIVE_DATASET_ID, NARRATIVE_CASES
    raise ValueError('invalid suite selection')
