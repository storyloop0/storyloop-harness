"""Structured ordinary scene output and persistence schemas."""
from typing import Literal
from pydantic import BaseModel, Field, StrictBool, StrictInt, StrictStr, field_validator
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


class SceneTurn(BaseModel):
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


class NarrativeTurn(BaseModel):
    """Only prose is required; the sidecar helps save actor-scoped context."""

    prose: str = Field(min_length=1)
    replies: list[SceneReply] = Field(default_factory=list)
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

    @field_validator("replies", "options", "memories", mode="before")
    @classmethod
    def valid_items(cls, value: object, info) -> list[BaseModel]:
        if not isinstance(value, list):
            return []
        model = {"replies": SceneReply, "options": ProposedNextAction,
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
            prose=self.prose, replies=self.replies,
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
