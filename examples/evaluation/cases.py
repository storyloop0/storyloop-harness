"""Ground truth is declared independently of the model fixture."""
CASES = [
    {'input': {'id': 'authored-action', 'window_broken': False},
     'expected_output': {'window_broken': True, 'outcome': 'accepted',
                         'clue': 'brasskey731', 'actor': 'dockhand'}},
    {'input': {'id': 'repeat-action-blocked', 'window_broken': True},
     'expected_output': {'window_broken': True, 'outcome': 'rejected',
                         'clue': 'brasskey731', 'actor': 'dockhand'}},
]
