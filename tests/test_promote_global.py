"""Project-local installs copy themselves into the agent's global skills."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ask


class PromoteGlobalTest(unittest.TestCase):
    def skill(self, root):
        (root / 'scripts').mkdir(parents=True)
        (root / 'SKILL.md').write_text('skill')
        (root / 'scripts/ask.py').write_text('launcher')
        return root

    def test_copies_only_to_present_agent_homes(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / 'home'
            (home / '.codex').mkdir(parents=True)
            root = self.skill(base / 'skill')
            ask.promote_global(home=home, root=root)
            self.assertEqual((home / '.agents/skills/research/SKILL.md').read_text(), 'skill')
            self.assertEqual((home / '.agents/skills/research/scripts/ask.py').read_text(), 'launcher')
            self.assertFalse((home / '.claude/skills/research').exists())

    def test_both_homes_when_both_exist(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / 'home'
            (home / '.codex').mkdir(parents=True)
            (home / '.claude').mkdir()
            ask.promote_global(home=home, root=self.skill(base / 'skill'))
            self.assertTrue((home / '.agents/skills/research/SKILL.md').is_file())
            self.assertTrue((home / '.claude/skills/research/SKILL.md').is_file())

    def test_existing_global_copy_is_kept(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / 'home'
            target = home / '.agents/skills/research'
            target.mkdir(parents=True)
            (home / '.codex').mkdir()
            (target / 'SKILL.md').write_text('newer')
            ask.promote_global(home=home, root=self.skill(base / 'skill'))
            self.assertEqual((target / 'SKILL.md').read_text(), 'newer')

    def test_no_agent_home_and_git_checkout_do_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / 'home'
            home.mkdir()
            ask.promote_global(home=home, root=self.skill(base / 'skill'))
            self.assertFalse((home / '.agents').exists())
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / 'home'
            (home / '.codex').mkdir(parents=True)
            root = self.skill(base / 'skill')
            (root / '.git').mkdir()
            ask.promote_global(home=home, root=root)
            self.assertFalse((home / '.agents').exists())


if __name__ == '__main__':
    unittest.main()
