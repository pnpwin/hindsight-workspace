import os
import threading
import unittest
from unittest.mock import patch
import test_workspace as fixture
w = fixture.w


class EdgeTests(unittest.TestCase):
    setUp = fixture.WorkspaceTests.setUp
    git = fixture.WorkspaceTests.git
    init_git = fixture.WorkspaceTests.init_git

    def test_host_summarizer_used_not_hindsight_reflect(self):
        self.target.summarize = lambda source: '# Unique host survey\n' + source
        w.sync_workspace(self.target)
        self.assertIn('Unique host survey', self.api.docs[('fixture-a', w.DOC_SURVEY)])
        self.assertFalse(any(path.endswith('/reflect') for path, _ in self.api.calls))

    def test_missing_host_summarizer_fails_closed(self):
        self.target.summarize = None
        with self.assertRaises(RuntimeError):
            w.sync_workspace(self.target)
        self.assertEqual(self.api.docs, {})

    def test_completed_old_operation_can_restore_prior_content(self):
        client = w.Client(self.target, threading.Event())
        client.retain(w.DOC_GIT, 'first content')
        client.retain(w.DOC_GIT, 'second content')
        failure = None
        try:
            client.retain(w.DOC_GIT, 'first content')
        except RuntimeError as exc:
            failure = str(exc)
        self.assertIsNone(failure, 'history rewind reused an already completed operation')
        self.assertEqual(self.api.docs[('fixture-a', w.DOC_GIT)], 'first content')

    def test_directory_swap_cannot_leak_outside_names(self):
        src = self.repo / 'src'
        src.mkdir()
        (src / 'normal.py').write_text('print(1)')
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'outside_marker.py').write_text('outside contents')
        original_scan = os.scandir
        inode = src.stat().st_ino
        swapped = False
        def scan(path):
            nonlocal swapped
            is_source = (isinstance(path, int) and os.fstat(path).st_ino == inode) or str(path) == str(src)
            if is_source and not swapped:
                src.rename(self.repo / 'saved')
                src.symlink_to(outside, target_is_directory=True)
                swapped = True
            return original_scan(path)
        with patch.object(w.os, 'scandir', side_effect=scan):
            text = w.collect_survey(self.repo)
        self.assertTrue(swapped)
        self.assertNotIn('outside_marker', text)

    def test_newest_300_commits_only(self):
        self.init_git()
        for n in range(300):
            self.git('commit', '--allow-empty', '-qm', f'Bounded fixture change {n}')
        snap = w.git_snapshot(self.repo)
        self.assertEqual(snap['count'], 300)
        self.assertNotIn('Initial fixture history', snap['content'])
        self.assertIn('Bounded fixture change 299', snap['content'])

    def test_git_output_is_bounded(self):
        self.init_git()
        message = self.root / 'message.txt'
        message.write_text('large message\n' + 'a' * 2_100_000)
        self.git('commit', '--allow-empty', '-q', '-F', str(message))
        with self.assertRaises(RuntimeError):
            w.git_snapshot(self.repo)

    def test_git_head_is_snapshot_not_moving_head(self):
        self.init_git()
        original = w.git
        head = self.git('rev-parse', 'HEAD')
        moved = False
        def racing(cwd, *args, **kwargs):
            nonlocal moved
            if 'log' in args and not moved:
                self.git('commit', '--allow-empty', '-qm', 'SHOULD_NOT_ENTER_OLD_SNAPSHOT')
                moved = True
            return original(cwd, *args, **kwargs)
        with patch.object(w, 'git', side_effect=racing):
            snapshot = w.git_snapshot(self.repo)
        self.assertEqual(snapshot['head'], head)
        self.assertNotIn('SHOULD_NOT_ENTER_OLD_SNAPSHOT', snapshot['content'])

if __name__ == '__main__':
    unittest.main()
