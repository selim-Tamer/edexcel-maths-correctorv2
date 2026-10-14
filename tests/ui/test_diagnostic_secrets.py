"""Synthetic local TOML only; never read deployed secrets or call Gemini."""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch
from streamlit.runtime.secrets import Secrets
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import marking_requests as mr


class StreamlitSecretDiagnosticsTests(unittest.TestCase):
    def test_top_level_string_is_promoted_and_startup_check_is_safe(self):
        previous = (mr._diagnostics_enabled, mr._startup_enabled)
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'secrets.toml'
            file.write_text('MATH_MARKER_DIAGNOSTICS = "1"\nOTHER_SECRET = "PRIVATE"\n', encoding='utf-8')
            secrets = Secrets()
            try:
                mr._startup_enabled = None
                stream = io.StringIO()
                with patch.dict(os.environ, {}, clear=False), patch('streamlit.runtime.secrets.config.get_option', return_value=[str(file)]), patch.object(Secrets, '_maybe_install_file_watchers'), redirect_stderr(stream):
                    os.environ.pop('MATH_MARKER_DIAGNOSTICS', None)
                    self.assertIsNone(os.getenv('MATH_MARKER_DIAGNOSTICS'))
                    self.assertTrue(mr.configure_diagnostics(secrets))
                    self.assertEqual(os.getenv('MATH_MARKER_DIAGNOSTICS'), '1')
                    self.assertEqual(stream.getvalue(), 'marking_diagnostics_startup {"enabled": true}\n')
            finally:
                mr._diagnostics_enabled, mr._startup_enabled = previous
