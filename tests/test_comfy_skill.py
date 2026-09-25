"""Regression checks for the app-owned Comfy API skill and typed contract."""

import re
import sys
import unittest
from pathlib import Path

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from routers.workflows import WorkflowPlacementPolicy  # noqa: E402


class ComfySkillDiscoveryTests(unittest.TestCase):
    def test_agent_entry_point_routes_to_app_owned_skill(self):
        instructions = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("skills/omni-comfy-api/SKILL.md", instructions)
        self.assertTrue((ROOT / "docs/comfy-api.md").is_file())

    def test_user_guide_is_linked_from_comfy_and_workflow_tabs(self):
        guide = ROOT / "server/static/comfy-api-guide.html"
        self.assertTrue(guide.is_file())
        for relative in ("server/static/tab-comfy.js", "server/static/tab-workflows.js"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("/static/comfy-api-guide.html", source)

    def test_skill_has_no_scaffold_placeholders_and_references_exist(self):
        skill_path = ROOT / "skills/omni-comfy-api/SKILL.md"
        skill = skill_path.read_text(encoding="utf-8")
        self.assertNotIn("TODO", skill)
        self.assertRegex(skill, r"^---\s+name: omni-comfy-api\s+description:", re.MULTILINE)
        references = re.findall(r"\]\((references/[^)]+\.md)\)", skill)
        self.assertGreaterEqual(len(references), 3)
        for relative in references:
            self.assertTrue((skill_path.parent / relative).is_file(), relative)


class PlacementSchemaTests(unittest.TestCase):
    def test_policy_is_typed_and_serializes_all_documented_fields(self):
        policy = WorkflowPlacementPolicy(
            mode="manual",
            eligible_devices=["GPU-one", "GPU-two"],
            primary_device="GPU-one",
            reserve_mb=1536,
            device_reserve_mb={"GPU-two": 2048},
            overrides={"12:model": "GPU-one"},
            require_all=False,
            allow_cpu=False,
        )
        payload = policy.model_dump(mode="json")
        self.assertEqual(payload["mode"], "manual")
        self.assertEqual(payload["device_reserve_mb"]["GPU-two"], 2048)
        self.assertEqual(payload["overrides"]["12:model"], "GPU-one")

    def test_unknown_mode_and_fields_are_rejected(self):
        with self.assertRaises(ValidationError):
            WorkflowPlacementPolicy(mode="magic")
        with self.assertRaises(ValidationError):
            WorkflowPlacementPolicy(mode="auto", invented_field=True)


if __name__ == "__main__":
    unittest.main()
