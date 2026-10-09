# Changelog

## 0.2.0

- Remove per-NPC ReAct handler injection from `TurnEngine` and `SingleCallGameSession`.
- Remove the obsolete `advanced.submit_player_input` helper that queued beta `npc_reply` work.
- Reject saves with pending `npc_reply` work before generation or world writes. Start a new game; no migration or automatic deletion is performed.
- Retain scene context projection, character visibility and histories, deterministic `single_npc_reply` delivery and saved-turn replay.
- Preserve the original multi-agent beta archive references documented in `PROVENANCE.md`.

## 0.1.0

- Extract the independent narrative runtime from the original StoryLoop repository.
- Provide the turn API, deterministic in-memory store and offline model example.
- Add independent CI, package import boundary checks and contribution guidance.

See `PROVENANCE.md` for exact source and extraction commits.
