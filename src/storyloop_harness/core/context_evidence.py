"""Conservative shared conversion from committed records to recipient history."""

from storyloop_harness.core.contracts import AgentContextEntry, ContextEvidence


PLAYER_KINDS = {'player_input', 'player_query', 'player_action', 'action_rejected'}


def _saved_query_record(parent, speaker_id, speech):
    """Validate committed strict metadata independently of today's catalog."""
    if (parent is None or parent.kind != 'player_input' or parent.actor_id != 'player'
            or parent.cause_id is not None):
        return None
    record = parent.details.get('fact_query')
    if not isinstance(record, dict) or record.get('speaker_id') != speaker_id:
        return None
    try:
        from storyloop_harness.runtime.fact_query import saved_query_scene
        from storyloop_harness.world.facts import FactQuery
        query = FactQuery(record['fact_id'], speaker_id)
        saved_query_scene(parent.details, query)
    except (KeyError, TypeError, ValueError):
        return None
    if record['answer'] != speech:
        return None
    return record


def _strict_answer(parent, speaker_id, speech):
    record = _saved_query_record(parent, speaker_id, speech)
    return record if record is not None and record['status'] == 'known' else None


def _reply_evidence(event, owner, events_by_id, *, combined=False):
    if combined:
        # This legacy shape embeds a player utterance beside the NPC reply.
        return ContextEvidence('utterance', (event.event_id,), owner)
    parent = events_by_id.get(event.cause_id)
    record = (_strict_answer(parent, event.actor_id, event.details.get('speech'))
              if parent is not None and event.event_id ==
              f'{parent.event_id}:reply:{event.actor_id}:spoken' else None)
    if record is not None:
        source = record['source']
        return ContextEvidence('grounded_answer',
            (parent.event_id, event.event_id, f"fact:{record['fact_id']}"), owner,
            None if combined else event.actor_id,
            {'fact_id': record['fact_id'], **source})
    return ContextEvidence('utterance', (event.event_id,), owner,
                           None if combined else event.actor_id)


def _observation_evidence(observation, event, owner, events_by_id):
    sources = (event.event_id, observation.observation_id)
    if event.kind == 'npc_spoke' and observation.observation_id.startswith(event.event_id + ':'):
        speech = event.details.get('speech')
        label = event.details.get('speaker_name')
        exact_player_heard = (observation.observation_id == f'{event.event_id}:player-heard'
            and isinstance(label, str) and observation.content == f'【{label}】\n{speech}')
        exact_overheard = (observation.observation_id == f'{event.event_id}:overheard:{owner}'
            and isinstance(speech, str) and observation.content.endswith(f'对玩家说：{speech}'))
        if exact_overheard:
            return ContextEvidence('utterance', sources, owner, event.actor_id)
        if isinstance(speech, str) and speech and exact_player_heard:
            reply = _reply_evidence(event, owner, events_by_id)
            return ContextEvidence(reply.kind, (*sources, *reply.source_ids[2:]), owner,
                                   event.actor_id, reply.fact_reference)
    if event.kind in PLAYER_KINDS and event.actor_id == 'player':
        if observation.observation_id == f'{event.event_id}:heard:{owner}' and observation.content == event.details.get('text'):
            return ContextEvidence('utterance', sources, owner, 'player')
        if observation.observation_id == f'{event.event_id}:memory:{owner}':
            return ContextEvidence('generated_memory', sources, owner)
        if observation.observation_id == f'{event.event_id}:seen:{owner}' and observation.content == event.details.get('sensory'):
            return ContextEvidence('generated_narrative', sources, owner)
        beats = event.details.get('visible_beats')
        beat = beats.get(observation.observation_id) if isinstance(beats, dict) else None
        beat_text = beat.get('text') if isinstance(beat, dict) else None
        beat_kind = beat.get('kind') if isinstance(beat, dict) else None
        label = beat.get('speaker_name') if isinstance(beat, dict) else None
        exact_beat = (observation.content == f'【{label}】\n{beat_text}'
                      if beat_kind == 'dialogue' and isinstance(label, str)
                      else observation.content == beat_text)
        if (owner == 'player' and observation.observation_id.startswith(event.event_id + ':beat:')
                and isinstance(beat_text, str) and exact_beat):
            if beat.get('kind') == 'dialogue' and isinstance(beat.get('speaker_id'), str):
                record = _strict_answer(event, beat['speaker_id'], beat['text'])
                if record is not None:
                    return ContextEvidence('grounded_answer',
                        (*sources, f"fact:{record['fact_id']}"), owner, beat['speaker_id'],
                        {'fact_id': record['fact_id'], **record['source']})
                return ContextEvidence('utterance', sources, owner, beat['speaker_id'])
            if beat.get('kind') == 'narration':
                return ContextEvidence('generated_narrative', sources, owner)
            if beat.get('kind') == 'scene':
                strict = event.details.get('fact_query')
                if isinstance(strict, dict) and isinstance(strict.get('answer'), str):
                    speaker = strict.get('speaker_id')
                    answer = strict['answer']
                    name = event.details.get('fact_query_novel_speaker_name')
                    if (isinstance(name, str) and name
                            and set(beat) == {'kind', 'text'}
                            and set(beats) == {f'{event.event_id}:beat:0'}
                            and observation.observation_id == f'{event.event_id}:beat:0'
                            and beat['text'] == f'{name}说：“{answer}”'):
                        record = _saved_query_record(event, speaker, answer)
                        if record is not None:
                            if record['status'] == 'known':
                                return ContextEvidence('grounded_answer',
                                    (*sources, f"fact:{record['fact_id']}"), owner, speaker,
                                    {'fact_id': record['fact_id'], **record['source']})
                            return ContextEvidence('utterance', sources, owner, speaker)
                return ContextEvidence('generated_narrative', sources, owner)
    return ContextEvidence('legacy_unverified', sources, owner)


def evidence_payload(evidence):
    """Plain, content-free metadata at the model boundary."""
    return {'kind': evidence.kind, 'source_ids': list(evidence.source_ids),
            'owner_id': evidence.owner_id, 'speaker_id': evidence.speaker_id,
            'fact_reference': evidence.fact_reference}


def context_entries(events, observations, actor_id, after_version=-1):
    """Merge source records exactly as both stores did, with attached evidence."""
    events = tuple(events)
    events_by_id = {event.event_id: event for _, event in events}
    versions = {event.event_id: version for version, event in events}
    seen = {(item.event_id, item.content) for item in observations}
    items = []
    for version, event in events:
        if version <= after_version or event.actor_id != actor_id:
            continue
        if actor_id == 'player' and event.kind in PLAYER_KINDS:
            content = str(event.details.get('text', ''))
            evidence = ContextEvidence('utterance', (event.event_id,), actor_id, 'player')
        elif actor_id != 'player' and event.kind == 'npc_spoke':
            message = str(event.details.get('player_message', ''))
            prefix = '' if (event.cause_id, message) in seen else f'玩家曾说：{message}\n'
            content = f"{prefix}你当时回答：{event.details.get('speech', '')}"
            evidence = _reply_evidence(event, actor_id, events_by_id, combined=bool(prefix))
        else:
            continue
        items.append((version, 0, AgentContextEntry(version, event.event_id, event.kind,
                                                    content, event.tick, evidence)))
    for observation in observations:
        version = versions[observation.event_id]
        if version > after_version:
            event = events_by_id[observation.event_id]
            evidence = _observation_evidence(observation, event, actor_id, events_by_id)
            items.append((version, 1, AgentContextEntry(version, observation.observation_id,
                observation.channel, observation.content, observation.tick, evidence)))
    items.sort(key=lambda item: item[:2])
    return [item[2] for item in items]
