import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from storyloop_harness.agents.action_advisor import ActionOptionAdvisor
from storyloop_harness.world.scenario import ScenarioPackage


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "freeform"


class FakeOptionModel:
    def __init__(self):
        self.calls = 0

    async def __call__(self, prompt, **kwargs):
        self.calls += 1
        return SimpleNamespace(metadata={"options": [
            {"label": "看看四周", "input": "我观察周围。"},
            {"label": "和人交谈", "input": "我找在场的人聊聊。"},
            {"label": "试着行动", "input": "我参与眼前的事情。"},
        ]})


class BlankOptionModel(FakeOptionModel):
    async def __call__(self, prompt, **kwargs):
        response = await super().__call__(prompt, **kwargs)
        response.metadata["options"][0]["input"] = "   "
        return response


class PresentationModeTests(unittest.TestCase):


    def test_manifest_defaults_to_interactive_and_accepts_novel(self):
        self.assertEqual(ScenarioPackage.load(EXAMPLE).presentation_mode, "interactive")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            for name in ("manifest.json", "worldbook.json"):
                (target / name).write_bytes((EXAMPLE / name).read_bytes())
            manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
            manifest["presentation_mode"] = "novel"
            (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(ScenarioPackage.load(target).presentation_mode, "novel")

    def test_action_advisor_makes_one_model_call_for_three_clickable_actions(self):
        model = FakeOptionModel()
        options = asyncio.run(ActionOptionAdvisor(model).suggest("灯亮了。", "novel"))
        self.assertEqual(model.calls, 1)
        self.assertEqual(len(options), 3)
        self.assertEqual(options[0].input, "我观察周围。")

    def test_blank_model_action_uses_three_executable_fallbacks(self):
        model = BlankOptionModel()
        options = asyncio.run(ActionOptionAdvisor(model).suggest("灯亮了。", "novel"))
        self.assertEqual(model.calls, 1)
        self.assertEqual(len(options), 3)
        self.assertTrue(all(option.label.strip() and option.input.strip() for option in options))

    def test_campaign_leads_replace_actions_unrelated_to_visible_story(self):
        model = FakeOptionModel()
        leads = [
            {"label": "交还行李牌", "input": "我把行李牌交给刚认识的嘉宾。"},
            {"label": "问节目安排", "input": "我问节目组今晚有什么共同环节。"},
            {"label": "加入晚餐准备", "input": "我到客厅帮大家准备晚餐。"},
        ]
        options = asyncio.run(ActionOptionAdvisor(model).suggest(
            "嘉宾们在客厅等着。", "interactive",
            story_context={"goal": "认识嘉宾", "anchors": ["行李牌", "节目组", "晚餐"],
                           "leads": leads},
        ))
        self.assertEqual(model.calls, 1)
        self.assertEqual([option.label for option in options], [lead["label"] for lead in leads])

    def test_authored_fallback_never_repeats_a_completed_action(self):
        leads = [
            {"label": "问报名缘由", "input": "我问周闻野为什么报名。"},
            {"label": "了解节目安排", "input": "我问节目组今晚有什么安排。"},
            {"label": "加入晚餐准备", "input": "我去帮大家准备晚餐。"},
        ]
        options = asyncio.run(ActionOptionAdvisor(FakeOptionModel()).suggest(
            "周闻野刚回答了报名原因。", "novel",
            story_context={"anchors": ["周闻野", "节目组", "晚餐"], "leads": leads},
            recent_actions=(leads[0]["input"],),
        ))
        self.assertEqual([item.input for item in options],
                         [leads[1]["input"], leads[2]["input"]])


if __name__ == "__main__":
    unittest.main()
