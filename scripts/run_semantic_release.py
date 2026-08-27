"""Entry point for semantic-release, with a shim for GitPython >= 3.1.60.

GitPython 3.1.60 dropped ``Actor.name_email_regex`` as part of a security
release; its own author parsing now uses plain string splitting instead. But
python-semantic-release (through 10.6.1) still reads that attribute to validate
the ``commit_author`` config, so the release job dies at config load with
``type object 'Actor' has no attribute 'name_email_regex'``.

Pinning GitPython back to 3.1.59 would fix it too, but would mean skipping three
security advisories. So restore the attribute here instead: the only string it
ever matches is our own static ``commit_author``, never anything user supplied,
and GitPython no longer routes its own parsing through it.

Drop this file once python-semantic-release stops referencing the attribute.
"""

from __future__ import annotations

import re
import sys

import git

if not hasattr(git.Actor, "name_email_regex"):
    git.Actor.name_email_regex = re.compile(r"(.*) <(.*?)>")

from semantic_release.__main__ import main  # noqa: E402  (must follow the shim)

if __name__ == "__main__":
    sys.exit(main())
