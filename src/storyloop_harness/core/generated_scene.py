"""Immutable, validated evidence for one generated ordinary scene."""
from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from hashlib import sha256
import json
from typing import TYPE_CHECKING

from pydantic import BaseModel
from storyloop_harness.core.contracts import Snapshot

from storyloop_harness.agents.scene_schema import SceneTurn
from storyloop_harness.models.usage import ModelUsage

if TYPE_CHECKING:
    from storyloop_harness.core.store_port import GeneratedSceneSnapshotStore
    from storyloop_harness.world.scenario import ScenarioPackage

FORMAT_VERSION = 1
GENERATION_CONTRACT_VERSION = 1
BODY_KEYS = frozenset(('format_version', 'snapshot_id', 'game_id', 'turn_id', 'input_hash',
    'base_version', 'base_tick', 'base_state_hash', 'scenario_id', 'scenario_version',
    'package_fingerprint', 'generation_contract_version', 'system_prompt_hash',
    'output_schema_hash', 'commit_config_hash', 'generation_config_hash',
    'projected_request_hash', 'focus_actor_ids', 'nearby_actor_ids', 'preference_hash',
    'scene', 'success_usage', 'usage_coverage'))


class GeneratedSceneConflict(ValueError):
    pass


class GeneratedSceneMismatch(ValueError):
    pass


class GeneratedSceneStale(ValueError):
    pass


class GeneratedSceneCorrupt(ValueError):
    pass


class GeneratedSceneWriteFailure(RuntimeError):
    pass


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def content_hash(value: object) -> str:
    return sha256(canonical_json(value).encode('utf-8')).hexdigest()


def snapshot_state_fingerprint(snapshot: Snapshot) -> str:
    """Fingerprint the persisted world data, excluding version and clock bindings."""
    return content_hash(snapshot.data)


def text_hash(value: str) -> str:
    return sha256(value.encode('utf-8')).hexdigest()


def logical_value(value: object) -> object:
    """Traverse public logical data without paths, repr, or object identity."""
    if value is None or type(value) in (str, int, float, bool):
        return value
    if isinstance(value, BaseModel):
        return logical_value(value.model_dump())
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: logical_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {key: logical_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [logical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(logical_value(item) for item in value)
    raise TypeError('unsupported logical package value')


def package_fingerprint(package: ScenarioPackage) -> str:
    data = {field.name: logical_value(getattr(package, field.name))
            for field in fields(package) if field.name != 'worldbook'}
    data['worldbook'] = {'package_id': package.worldbook.package_id,
                         'version': package.worldbook.version,
                         'entries': logical_value(package.worldbook.entries)}
    return content_hash(data)


@dataclass(frozen=True)
class GeneratedSceneRecovery:
    snapshot_store: GeneratedSceneSnapshotStore
    generation_identity: str

    def __post_init__(self) -> None:
        if not isinstance(self.generation_identity, str) or not self.generation_identity.strip():
            raise ValueError('generated scene recovery requires nonempty generation identity')


@dataclass(frozen=True)
class GeneratedSceneRecoveryInspection:
    snapshot: GeneratedSceneSnapshot
    committed: bool


@dataclass(frozen=True)
class GeneratedSceneSnapshot:
    canonical_json: str
    checksum: str
    snapshot_id: str
    game_id: str
    turn_id: str

    @classmethod
    def create(cls, *, scene: SceneTurn, success_usage: tuple[ModelUsage, ...],
               usage_coverage: str, **metadata: object) -> GeneratedSceneSnapshot:
        body = {'format_version': FORMAT_VERSION, 'generation_contract_version': GENERATION_CONTRACT_VERSION,
                **metadata, 'scene': scene.model_dump(),
                'success_usage': [usage.to_dict() for usage in success_usage],
                'usage_coverage': usage_coverage}
        body['snapshot_id'] = content_hash(body)
        encoded = canonical_json(body)
        return cls.from_storage(encoded, text_hash(encoded))

    @classmethod
    def from_storage(cls, canonical_json_text: str, checksum: str) -> GeneratedSceneSnapshot:
        try:
            if text_hash(canonical_json_text) != checksum:
                raise ValueError('checksum mismatch')
            body = json.loads(canonical_json_text)
            if (not isinstance(body, dict) or frozenset(body) != BODY_KEYS
                    or canonical_json(body) != canonical_json_text
                    or body['format_version'] != FORMAT_VERSION
                    or body['generation_contract_version'] != GENERATION_CONTRACT_VERSION):
                raise ValueError('unsupported scene record')
            identity = body['snapshot_id']
            if not isinstance(identity, str) or identity != content_hash({
                    key: value for key, value in body.items() if key != 'snapshot_id'}):
                raise ValueError('snapshot identity mismatch')
            for key in ('game_id', 'turn_id', 'scenario_id', 'scenario_version'):
                if not isinstance(body[key], str) or not body[key]:
                    raise ValueError('invalid scene identity')
            for key in ('input_hash', 'base_state_hash', 'package_fingerprint',
                        'system_prompt_hash', 'output_schema_hash', 'commit_config_hash',
                        'generation_config_hash', 'projected_request_hash', 'preference_hash'):
                if not isinstance(body[key], str) or len(body[key]) != 64 or any(
                        char not in '0123456789abcdef' for char in body[key]):
                    raise ValueError('invalid scene binding')
            if any(type(body[key]) is not int or body[key] < 0 for key in ('base_version', 'base_tick')):
                raise ValueError('invalid base state')
            for key in ('focus_actor_ids', 'nearby_actor_ids'):
                if not isinstance(body[key], list) or not all(isinstance(x, str) for x in body[key]):
                    raise ValueError('invalid actor ids')
            parsed_scene = SceneTurn.model_validate(body['scene'])
            if parsed_scene.model_dump() != body['scene']:
                raise ValueError('scene does not match validated schema')
            if body['usage_coverage'] not in ('reported', 'missing', 'invalid', 'unobserved'):
                raise ValueError('invalid usage coverage')
            if not isinstance(body['success_usage'], list):
                raise ValueError('invalid usage')
            for item in body['success_usage']:
                if not isinstance(item, dict) or set(item) != set(ModelUsage('m', 't', 0, 0).to_dict()):
                    raise ValueError('invalid usage')
                ModelUsage(**item)
            if body['usage_coverage'] == 'reported' and not body['success_usage']:
                raise ValueError('reported usage has no records')
            if body['usage_coverage'] != 'reported' and body['success_usage']:
                raise ValueError('unreported usage has records')
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise GeneratedSceneCorrupt('invalid generated scene snapshot') from exc
        return cls(canonical_json_text, checksum, body['snapshot_id'], body['game_id'], body['turn_id'])

    def metadata(self) -> dict[str, object]:
        return json.loads(self.canonical_json)

    def scene(self) -> SceneTurn:
        return SceneTurn.model_validate(self.metadata()['scene'])

    def success_usage(self) -> tuple[ModelUsage, ...]:
        return tuple(ModelUsage(**item) for item in self.metadata()['success_usage'])


def validate_event_snapshot_reference(event, snapshot: GeneratedSceneSnapshot | None) -> None:
    details = event.details if event is not None else {}
    keys = ('generated_scene_snapshot_id', 'generated_scene_snapshot_checksum')
    if not any(key in details for key in keys):
        return
    if (not all(key in details for key in keys) or snapshot is None
            or event.event_id.rsplit(':', 1)[0] != snapshot.turn_id
            or event.details[keys[0]] != snapshot.snapshot_id
            or event.details[keys[1]] != snapshot.checksum
            or not isinstance(details.get('text'), str)
            or text_hash(details['text']) != snapshot.metadata()['input_hash']
            or content_hash(details.get('single_call')) != content_hash(snapshot.metadata()['scene'])):
        raise GeneratedSceneCorrupt('generated scene event reference mismatch')
