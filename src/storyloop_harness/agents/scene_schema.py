"""Structured ordinary scene output and persistence schemas."""
from typing import Literal
import unicodedata
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator
from storyloop_harness.core.decisions import MainDecision
from storyloop_harness.runtime.player_knowledge import PlayerEncounter

class SceneReply(BaseModel):
    actor_id: str
    speech: str = Field(min_length=1)


class ProposedEffect(BaseModel):
    path: list[str]
    value: StrictStr | StrictInt | StrictBool


class ProposedStatusChange(BaseModel):
    id: str
    delta: int = Field(strict=True)


class ProposedAction(BaseModel):
    status: Literal["occurred", "attempted", "blocked"] = "attempted"
    player_result: str = ""
    sensory: str = ""
    effects: list[ProposedEffect] = Field(default_factory=list)


class ProposedNextAction(BaseModel):
    label: str
    input: str


class SharedMoment(BaseModel):
    actor_id: str
    fact: str = Field(min_length=1)


class LegacySceneTurn(BaseModel):
    """Read-only schema for already committed pre-beats events. Never generation input."""
    decision: MainDecision
    prose: str = Field(min_length=1)
    replies: list[SceneReply] = Field(default_factory=list)
    action: ProposedAction = Field(default_factory=ProposedAction)
    status_changes: list[ProposedStatusChange] = Field(default_factory=list)
    options: list[ProposedNextAction] = Field(default_factory=list)
    memories: list[SharedMoment] = Field(default_factory=list)
    story_first: bool = False
    milestones_fulfilled: list[str] = Field(default_factory=list)
    encounters: list[PlayerEncounter] = Field(default_factory=list)


class NarrationBeat(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['narration']
    text: StrictStr = Field(min_length=1)

    @field_validator('text')
    @classmethod
    def normalized_text(cls, value):
        value = value.replace('\\r\\n', '\n').replace('\\n', '\n').replace('\\r', '\n').strip()
        if not value:
            raise ValueError('beat text must not be blank')
        return value


class DialogueBeat(NarrationBeat):
    kind: Literal['dialogue']
    actor_id: StrictStr = Field(min_length=1)

    @field_validator('actor_id')
    @classmethod
    def nonblank_actor(cls, value):
        if not value.strip() or value != value.strip():
            raise ValueError('dialogue actor_id must be nonblank and unpadded')
        return value


# Literal kinds and forbidden extra fields distinguish the alternatives without
# discriminator mappings, which retain dangling $defs references after SDK expansion.
SceneBeat = NarrationBeat | DialogueBeat


class SceneContent(BaseModel):
    """Ordered content; narration and dialogue text each have one authoritative source."""
    beats: list[SceneBeat] = Field(min_length=1, strict=True)

    @model_validator(mode='before')
    @classmethod
    def reject_old_content(cls, value):
        if isinstance(value, dict) and {'prose', 'replies'} & value.keys():
            raise ValueError('prose/replies are obsolete; supply ordered beats')
        return value

    @model_validator(mode='after')
    def reject_full_answer_repetition(self):
        # Bounded lexical check, not semantic review: NFKC + casefold, keeping
        # only Unicode letters/numbers. A complete dialogue of >= 8 such
        # characters repeated in any one narration beat is rejected. Dialogue
        # beats are never compared to one another (different speakers may agree).
        def normalized(text):
            return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold()
                           if c.isalnum())
        narrations = [normalized(b.text) for b in self.beats if b.kind == 'narration']
        for beat in self.beats:
            if beat.kind == 'dialogue':
                text = normalized(beat.text)
                if len(text) >= 8 and any(text in item for item in narrations):
                    raise ValueError('narration repeats a complete dialogue answer')
        return self

    @property
    def prose(self) -> str:
        """Derived text for evidence matching; not a separately stored content field."""
        return '\n\n'.join(beat.text for beat in self.beats)

    @property
    def replies(self) -> list[SceneReply]:
        return [SceneReply(actor_id=beat.actor_id, speech=beat.text)
                for beat in self.beats if beat.kind == 'dialogue']


class SceneTurn(SceneContent):
    decision: MainDecision
    action: ProposedAction = Field(default_factory=ProposedAction)
    status_changes: list[ProposedStatusChange] = Field(default_factory=list)
    options: list[ProposedNextAction] = Field(default_factory=list)
    memories: list[SharedMoment] = Field(default_factory=list)
    story_first: bool = False
    milestones_fulfilled: list[str] = Field(default_factory=list)
    encounters: list[PlayerEncounter] = Field(default_factory=list)


def committed_scene(value: dict) -> SceneTurn | LegacySceneTurn:
    """Explicit historical event reader. New/custom generation uses SceneTurn only."""
    return (SceneTurn.model_validate(value) if 'beats' in value
            else LegacySceneTurn.model_validate(value))


class NarrativeTurn(SceneContent):
    """Required ordered beats supply all visible narration and character speech."""

    options: list[ProposedNextAction] = Field(default_factory=list)
    memories: list[SharedMoment] = Field(default_factory=list)
    status_changes: list[ProposedStatusChange] = Field(default_factory=list)
    participants: list[str] = Field(default_factory=list)
    interaction: Literal["speech", "inspect", "action"] = "speech"
    action_id: str | None = None
    entry_id: str | None = None
    action: ProposedAction | None = None
    duration: Literal["brief", "standard", "extended", "rest"] = "brief"
    delivery: Literal["targets", "room", "private_message"] = "targets"
    witnessed: str = ""

    @field_validator("options", "memories", mode="before")
    @classmethod
    def valid_items(cls, value: object, info) -> list[BaseModel]:
        if not isinstance(value, list):
            return []
        model = {"options": ProposedNextAction,
                 "memories": SharedMoment}[info.field_name]
        valid = []
        for item in value:
            try:
                valid.append(model.model_validate(item))
            except (TypeError, ValueError):
                continue
        return valid

    @field_validator("participants", mode="before")
    @classmethod
    def valid_participants(cls, value: object) -> list[str]:
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

    @field_validator("interaction", "duration", "delivery", mode="before")
    @classmethod
    def valid_label(cls, value: object, info) -> str:
        choices = {
            "interaction": ("speech", "inspect", "action"),
            "duration": ("brief", "standard", "extended", "rest"),
            "delivery": ("targets", "room", "private_message"),
        }
        return value if value in choices[info.field_name] else choices[info.field_name][0]

    @field_validator("witnessed", mode="before")
    @classmethod
    def valid_witnessed(cls, value: object) -> str:
        return value if isinstance(value, str) else ""

    def for_storage(self) -> SceneTurn:
        participants = list(dict.fromkeys([
            *self.participants,
            *(item.actor_id for item in self.replies),
            *(item.actor_id for item in self.memories),
        ]))
        return SceneTurn(
            decision=MainDecision(
                intent=self.interaction, duration=self.duration,
                action_id=self.action_id, entry_id=self.entry_id,
                target_ids=participants,
                channel="private_message" if self.delivery == "private_message" else "speech",
                audience="room" if self.delivery == "room" else "targets",
            ),
            beats=self.beats,
            options=self.options, memories=self.memories,
            status_changes=self.status_changes,
            action=self.action or ProposedAction(sensory=self.witnessed),
            story_first=True,
        )


class SourceNarrativeTurn(NarrativeTurn):
    """Optional progress metadata only for source-driven scenario packages."""

    options: list[ProposedNextAction] = Field(min_length=3, max_length=3)
    milestones_fulfilled: list[str] = Field(default_factory=list)
    encounters: list[PlayerEncounter] = Field(default_factory=list)

    @field_validator("encounters", mode="before")
    @classmethod
    def valid_encounters(cls, value: object) -> list[PlayerEncounter]:
        if not isinstance(value, list):
            return []
        result = []
        for item in value:
            try:
                result.append(PlayerEncounter.model_validate(item))
            except (TypeError, ValueError):
                continue
        return result

    @field_validator("milestones_fulfilled", mode="before")
    @classmethod
    def valid_milestones(cls, value: object) -> list[str]:
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

    def for_storage(self) -> SceneTurn:
        return super().for_storage().model_copy(update={
            "milestones_fulfilled": self.milestones_fulfilled,
            "status_changes": self.status_changes,
            "encounters": self.encounters,
        })


def scene_output_schema(package):
    return SourceNarrativeTurn if package.story_blueprint is not None else NarrativeTurn
