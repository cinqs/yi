"""Import this *first* in any test that touches yi.state.

Without it, ``state.load_config()`` merges the developer's real
``~/.config/yi/config.toml`` into the defaults, so tests depend on whatever
the machine happens to have configured — which is exactly how a test suite starts
passing locally and failing on someone else's laptop.
"""

import os
import tempfile

SANDBOX_HOME = tempfile.mkdtemp(prefix="yi-tests-")
os.environ["YI_HOME"] = SANDBOX_HOME
