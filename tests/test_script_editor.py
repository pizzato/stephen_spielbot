import ast
import unittest
from pathlib import Path


APP_SOURCE = (Path(__file__).resolve().parents[1] / "app.py").read_text()
APP_TREE = ast.parse(APP_SOURCE)


def _function_node(name: str) -> ast.FunctionDef:
    for node in APP_TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function not found: {name}")


class ScriptEditorTests(unittest.TestCase):
    def test_generation_callback_uses_active_scene_not_page_textboxes(self):
        args = _function_node("on_generate").args
        params = [arg.arg for arg in args.args]

        self.assertIn("job_id", params)
        self.assertIn("work_dir_str", params)
        self.assertIn("current_scene_id", params)
        self.assertIn("scene_title", params)
        self.assertIn("image_prompt", params)
        self.assertNotIn("scenes_data", params)
        self.assertNotIn("current_page", params)
        self.assertIsNone(args.vararg)

    def test_script_tab_is_configured_for_one_active_scene(self):
        source = ast.get_source_segment(APP_SOURCE, _function_node("build_ui"))

        self.assertIn("current_scene_state", source)
        self.assertIn("scene_picker", source)
        self.assertIn("scene_title_box", source)
        self.assertNotIn("scene_titles:", source)
        self.assertNotIn("scene_preview_images:", source)
        self.assertNotIn("range(_SCENES_PER_PAGE)", source)


if __name__ == "__main__":
    unittest.main()
