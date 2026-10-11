"""Check observable contracts, never infer success from fixture prose."""
import json

CHECK_NAMES = {'clue_retained', 'scope', 'private_player_fields', 'state', 'replay'}


def checks(output, expected):
    request = output['request']
    encoded = json.dumps(request, ensure_ascii=False)
    # Every non-NPC field is player/shared material. Selected private history may
    # appear only inside its NPC context; one model still sees all selected fields.
    player_fields = {k: v for k, v in request.items() if k != 'npc_contexts'}
    contexts = request['npc_contexts']
    replay = output['replay']
    return {
        'clue_retained': expected['clue'] in json.dumps(request['player_history']),
        'scope': ('FOREIGN_GAME' not in encoded and 'UNSELECTED_PRIVATE' not in encoded
                  and len(contexts) == 1 and contexts[0]['id'] == expected['actor']),
        'private_player_fields': ('SELECTED_PRIVATE' not in json.dumps(player_fields)
                                  and 'SELECTED_PRIVATE' in json.dumps(contexts)),
        'state': (output['window_broken'] is expected['window_broken']
                  and output['outcome'] == expected['outcome']),
        'replay': (replay['new_calls'] == replay['new_events'] == replay['version_delta']
                   == replay['usage_count'] == 0 and replay['same_state'] and replay['same_narration']),
    }


def evaluator(*, output, expected_output, **kwargs):
    from langfuse import Evaluation
    return [Evaluation(name=name, value=int(passed))
            for name, passed in checks(output, expected_output).items()]
