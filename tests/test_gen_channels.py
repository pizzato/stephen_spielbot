import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("gen_channels", ROOT / "scripts" / "gen_channels.py")
gen_channels = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen_channels)


def _yaml(body: str) -> Path:
    path = Path(tempfile.mkdtemp(prefix="spielbot-channels-")) / "channels.yaml"
    path.write_text(body)
    return path


class LoadChannelsTests(unittest.TestCase):
    """``channels.yaml`` is edited by outside contributors in pull requests, so
    the loader is the gate that turns a malformed entry into a failing CI check
    (see .github/workflows/channels.yml) instead of a broken README / About page.
    """

    def test_loads_entries_in_source_order(self):
        channels = gen_channels.load(_yaml(
            "channels:\n"
            "  - name: One\n"
            "    url: https://example.com/one\n"
            "    platform: youtube\n"
            "  - name: Two\n"
            "    url: https://example.com/two\n"
        ))
        self.assertEqual(["One", "Two"], [c["name"] for c in channels])
        # platform is optional and defaults to the generic icon bucket.
        self.assertEqual("youtube", channels[0]["platform"])
        self.assertEqual("other", channels[1].get("platform", "other"))

    def test_missing_file_sections_yield_no_channels(self):
        self.assertEqual([], gen_channels.load(_yaml("channels:\n")))

    def test_entry_missing_required_field_is_rejected(self):
        for body in (
            "channels:\n  - url: https://example.com/one\n",       # no name
            "channels:\n  - name: One\n",                          # no url
        ):
            with self.assertRaises(ValueError):
                gen_channels.load(_yaml(body))

    def test_non_http_url_is_rejected(self):
        # Guards against a PR slipping in a javascript: or relative link that
        # would be rendered straight into an anchor on the About screen.
        with self.assertRaises(ValueError):
            gen_channels.load(_yaml(
                "channels:\n  - name: One\n    url: javascript:alert(1)\n"
            ))

    def test_unknown_platform_is_rejected(self):
        with self.assertRaises(ValueError):
            gen_channels.load(_yaml(
                "channels:\n  - name: One\n    url: https://e.com\n    platform: tiktok\n"
            ))


class RenderMarkdownTests(unittest.TestCase):
    def test_renders_handle_platform_and_note(self):
        line = gen_channels.render_markdown([{
            "name": "Stephen Spielbot", "platform": "youtube",
            "url": "https://youtube.com/@s", "handle": "@s", "note": "The original",
        }])
        self.assertEqual(
            "- [Stephen Spielbot (@s)](https://youtube.com/@s) — YouTube · The original", line)

    def test_optional_parts_are_dropped(self):
        line = gen_channels.render_markdown([
            {"name": "Bare", "platform": "other", "url": "https://e.com"}])
        self.assertEqual("- [Bare](https://e.com)", line)

    def test_empty_list_has_a_placeholder(self):
        self.assertIn("No channels listed yet", gen_channels.render_markdown([]))


class RepoFilesTests(unittest.TestCase):
    def test_checked_in_channels_yaml_is_valid(self):
        self.assertTrue(gen_channels.load())

    def test_generated_copies_are_up_to_date(self):
        """The Action commits the generator's output, so a stale checked-in copy
        would silently reappear in the next unrelated push."""
        channels = gen_channels.load()
        self.assertFalse(gen_channels.write_json(channels), "run `make channels`")
        self.assertFalse(gen_channels.write_readme(channels), "run `make channels`")


if __name__ == "__main__":
    unittest.main()
