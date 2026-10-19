"""Zero-model fact queries and validation of their persisted runtime-owned result."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from storyloop_harness.agents.scene_schema import DialogueBeat, SceneTurn
from storyloop_harness.core.decisions import MainDecision
from storyloop_harness.core.generated_scene import package_fingerprint
from storyloop_harness.world.facts import FactQuery, UNKNOWN_FACT_ANSWER, resolve_fact, validate_identifier
from storyloop_harness.runtime.turn_progress import emit

if TYPE_CHECKING:
    from storyloop_harness.core.contracts import Snapshot
    from storyloop_harness.core.turn_result import TurnOutcome
    from storyloop_harness.runtime.single_call import SingleCallGameSession
    from storyloop_harness.runtime.turn_progress import TurnProgress


def answer_scene(query: FactQuery, text: str) -> SceneTurn:
    # Authored text is opaque. SceneBeat's model-output whitespace/escape
    # normalization must not rewrite it, including on historical replay.
    beat = DialogueBeat.model_construct(kind='dialogue', actor_id=query.speaker_id, text=text)
    return SceneTurn(decision=MainDecision(intent='speech', duration='brief',
        target_ids=[query.speaker_id], channel='private_message'), beats=[beat])


def saved_query_scene(saved: dict, query: FactQuery) -> SceneTurn:
    """Validate historical identity and original answer without consulting today's catalog."""
    record = saved.get('fact_query')
    keys = {'kind', 'fact_id', 'speaker_id', 'recipient_id', 'status', 'source', 'answer'}
    if not isinstance(record, dict) or set(record) != keys or record['kind'] != 'fact_query':
        raise ValueError('invalid saved fact query metadata')
    if (record['fact_id'] != query.fact_id or record['speaker_id'] != query.speaker_id
            or record['recipient_id'] != 'player'):
        raise ValueError('turn ID already belongs to another query')
    text, source = record['answer'], record['source']
    if not isinstance(text, str) or not text.strip() or record['status'] not in ('known', 'unknown'):
        raise ValueError('invalid saved fact query answer')
    if record['status'] == 'unknown':
        if text != UNKNOWN_FACT_ANSWER or source is not None:
            raise ValueError('invalid saved unknown answer')
    else:
        if (not isinstance(source, dict) or set(source) != {
                'package_id', 'package_version', 'package_fingerprint', 'tick'}):
            raise ValueError('invalid saved fact source')
        for key in ('package_id', 'package_version'):
            if not isinstance(source[key], str) or not source[key].strip():
                raise ValueError('invalid saved fact source identity')
        fingerprint = source['package_fingerprint']
        if (not isinstance(fingerprint, str) or len(fingerprint) != 64
                or any(char not in '0123456789abcdef' for char in fingerprint)
                or type(source['tick']) is not int or source['tick'] < 0
                or source['tick'] != saved.get('before_tick')):
            raise ValueError('invalid saved fact source binding')
    plan = answer_scene(query, text)
    if saved.get('single_call') != plan.model_dump():
        raise ValueError('saved fact answer does not match committed scene')
    return plan


def _reachable(session: SingleCallGameSession, before: Snapshot, query: FactQuery) -> None:
    if query.speaker_id == 'player' or query.speaker_id not in dict(session.package.actor_cards):
        raise ValueError('fact query requires a valid NPC speaker')
    actors = before.data.get('actors', {})
    player = actors.get('player') if isinstance(actors, dict) else None
    speaker = actors.get(query.speaker_id) if isinstance(actors, dict) else None
    if (not isinstance(player, dict) or not isinstance(speaker, dict)
            or not isinstance(player.get('location'), str) or not player['location']
            or speaker.get('location') != player['location']):
        raise ValueError('fact query speaker is not reachable')


def _reject_pending_generation(session: SingleCallGameSession, game_id: str, turn_id: str) -> None:
    stores = [session.store]
    recovery = session.generated_scene_recovery
    if recovery is not None and recovery.snapshot_store is not session.store:
        stores.append(recovery.snapshot_store)
    for store in stores:
        for method in ('generated_scene_snapshot', 'generated_scene_generation_marker'):
            lookup = getattr(store, method, None)
            if callable(lookup) and lookup(game_id, turn_id) is not None:
                raise ValueError('turn ID already belongs to pending free generation')


async def run_fact_query(session: SingleCallGameSession, game_id: str, query: FactQuery,
                         turn_id: str, progress: TurnProgress | None = None) -> TurnOutcome:
    validate_identifier(game_id, 'game_id')
    validate_identifier(turn_id, 'turn_id')
    if not isinstance(query, FactQuery):
        raise ValueError('query must be a FactQuery')
    if progress is not None and not callable(progress):
        raise ValueError('progress must be a callback')
    query.__post_init__()
    async with session._locks.setdefault(game_id, asyncio.Lock()):
        event_id, saved = session._saved_turn(game_id, turn_id)
        before = session.store.load(game_id)
        visible_before = {item.observation_id for item in session.store.observations_for(game_id, 'player')}
        drained_ids = ()
        if session.store.event_exists(game_id, event_id):
            if not isinstance(saved, dict):
                raise ValueError('invalid saved fact query')
            plan = saved_query_scene(saved, query)
        else:
            if session.package.story_blueprint is not None or 'campaign' in before.data:
                raise ValueError('fact queries require an ordinary save without campaign or story_blueprint')
            _reachable(session, before, query)
            _reject_pending_generation(session, game_id, turn_id)
            session._reject_legacy_work(game_id)
            drained = await session._runner().run_async(game_id)
            before, drained_ids = drained.snapshot, drained.processed_work_ids
            if any(work.mandatory for work in session.store.ready_work(game_id, before.tick)):
                raise ValueError('mandatory ready work remains before fact query')
            _reachable(session, before, query)
            answer = resolve_fact(session.package, query, before.tick)
            plan = answer_scene(query, answer.text)
            record = dict(kind='fact_query', fact_id=query.fact_id, speaker_id=query.speaker_id,
                recipient_id='player', status='known' if answer.known else 'unknown', answer=answer.text,
                source=(dict(package_id=session.package.package_id, package_version=session.package.version,
                    package_fingerprint=package_fingerprint(session.package), tick=before.tick)
                    if answer.known else None))
            await emit(progress, 'stage', stage='committing')
            session._commit_player(before, turn_id, query.fact_id, plan, None, fact_query=record)
        return await session._finish_turn(game_id, turn_id, plan, visible_before, progress,
                                          processed_before=drained_ids)
