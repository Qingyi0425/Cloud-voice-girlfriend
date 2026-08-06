import tempfile
import unittest
from pathlib import Path

from app.persona import Persona
from app.workspace import DEFAULT_FILES, WorkspaceFiles


class WorkspaceFilesTests(unittest.TestCase):
    def test_creates_eight_editable_markdown_files(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = WorkspaceFiles(Path(directory))
            self.assertEqual(set(workspace.file_names()), set(DEFAULT_FILES))
            for name in DEFAULT_FILES:
                self.assertTrue((Path(directory) / name).exists())

    def test_reads_direct_edits_on_each_prompt_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = WorkspaceFiles(root)
            (root / "STYLE.md").write_text("# 新风格\n- 每次只说一句", encoding="utf-8")

            first = workspace.prompt_text()
            (root / "STYLE.md").write_text("# 修改后的风格\n- 语气更轻松", encoding="utf-8")
            second = workspace.prompt_text()

            self.assertIn("新风格", first)
            self.assertIn("修改后的风格", second)
            self.assertNotIn("新风格", second)

    def test_migrates_legacy_soul_and_json_memory_to_markdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "soul.md").write_text("# 旧 Soul\n- 更温柔", encoding="utf-8")
            (root / "memory.json").write_text(
                '{"version": 1, "memories": [{"category": "preference", "content": "喜欢柠檬茶"}]}',
                encoding="utf-8",
            )
            WorkspaceFiles(root)

            self.assertIn("旧 Soul", (root / "SOUL.md").read_text(encoding="utf-8"))
            self.assertIn("喜欢柠檬茶", (root / "MEMORY.md").read_text(encoding="utf-8"))

    def test_persona_prompt_contains_workspace_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = WorkspaceFiles(root)
            (root / "MEMORY.md").write_text("# 长期记忆\n- 我喜欢晚上听轻音乐", encoding="utf-8")
            prompt = Persona(workspace=workspace).system_prompt()

            self.assertIn("我喜欢晚上听轻音乐", prompt)
            self.assertIn("SOUL.md", prompt)
            self.assertIn("STYLE.md", prompt)


if __name__ == "__main__":
    unittest.main()
