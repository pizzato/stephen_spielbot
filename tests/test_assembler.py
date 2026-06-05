import importlib
import unittest
from unittest import mock

import pipeline.assembler as assembler


class AssemblerToolResolutionTests(unittest.TestCase):
    def test_media_tool_path_can_be_configured_when_path_is_sparse(self):
        with mock.patch.dict("os.environ", {"FFPROBE_PATH": "/custom/bin/ffprobe"}, clear=False):
            reloaded = importlib.reload(assembler)
            self.assertEqual(reloaded._FFPROBE, "/custom/bin/ffprobe")
        importlib.reload(assembler)


if __name__ == "__main__":
    unittest.main()
