"""Marks the meeting-minutes tests as a package.

Not decoration. ``tests/test_web.py`` and ``tests/minutes/test_web.py`` both
exist, and pytest's default import mode names a test module after its file, so
without this file the two collide and the second one collected refuses to
import — taking the whole run down with it, not just itself.

With it, this directory's modules are imported as ``minutes.test_web`` and the
names stay distinct. Keep it, even though it is empty of code.
"""
