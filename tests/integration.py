#!/usr/bin/env python3
"""Integration tests using real ipset in a fresh, automatically isolated netns."""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import shlex
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'update-ipset'
IPSET = shutil.which('ipset')


class UpdateIPSetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='update-ipset-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.work = self.root / 'workdirs'
        self.work.mkdir()
        self.env = dict(os.environ, PATH=str(self.bin) + ':' + os.environ['PATH'], TMPDIR=str(self.work))

    def tearDown(self):
        self.ipset('destroy', 'reference', check=False)
        self.ipset('destroy')

    def command(self, *args, check=True, timeout=20, **kwargs):
        child = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, start_new_session=True, **kwargs)
        out, err = self.finish_child(child, timeout=timeout)
        result = subprocess.CompletedProcess(args, child.returncode, out, err)
        if check:
            self.assertEqual(result.returncode, 0, (args, result.stdout, result.stderr))
        return result

    def ipset(self, *args, **kwargs):
        return self.command(IPSET, *args, **kwargs)

    def arguments(self, name='testset', kind='hash:ip', family='inet', data=None):
        return ['bash', str(SCRIPT), '--quiet', '--setname', name, '--settype', kind,
                '--family', family, '--data-path', str(data or self.data)]

    def update(self, *extra, check=True, **kwargs):
        return self.command(*self.arguments(**kwargs), *extra, check=check, env=self.env)

    def source(self, content, name='entries.dat'):
        path = self.data / name
        path.write_bytes(content.encode('utf-8'))
        return path

    def seed(self, kind='hash:ip', family='inet'):
        self.ipset('create', 'testset', kind, 'family', family, 'maxelem', '1000000')
        self.ipset('add', 'testset', '192.0.2.1' if family == 'inet' else '2001:db8::1')
        return self.ipset('save', 'testset').stdout

    def unchanged(self, before):
        self.assertEqual(self.ipset('save', 'testset').stdout, before)
        self.assertEqual(self.ipset('list', '-n').stdout.splitlines(), ['testset'])

    def stub(self, command, body):
        path = self.bin / command
        path.write_text('#!/bin/bash\n' + body + '\n')
        path.chmod(0o755)

    def test_migration_all_types_and_families(self):
        for kind in ('hash:ip', 'hash:net'):
            for family in ('inet', 'inet6'):
                with self.subTest(kind=kind, family=family):
                    self.seed(kind, family)
                    self.ipset('create', 'reference', 'list:set')
                    self.ipset('add', 'reference', 'testset')
                    entry = '198.51.100.1' if family == 'inet' else '2001:db8::2'
                    self.source(entry + '\n' + entry + '\n')
                    self.update(kind=kind, family=family)
                    self.update(kind=kind, family=family)
                    info = self.ipset('list', 'testset', '-t').stdout
                    self.assertIn('maxelem 10002 ', info)
                    self.assertIn('Number of entries: 1', info)
                    self.assertIn('References: 1', info)
                    self.ipset('test', 'testset', entry)
                    self.ipset('test', 'reference', 'testset')
                    self.assertEqual(set(self.ipset('list', '-n').stdout.splitlines()), {'testset', 'reference'})
                    self.ipset('destroy', 'reference')
                    self.ipset('destroy', 'testset')

    def test_whitespace_crlf_comments_and_unterminated_line(self):
        self.source('  # comment\r\n\r\n 192.0.2.1 \r\n\t192.0.2.2\t\n192.0.2.3')
        self.update()
        for n in range(1, 4):
            self.ipset('test', 'testset', '192.0.2.' + str(n))
        self.assertIn('Number of entries: 3', self.ipset('list', 'testset', '-t').stdout)

    def test_ipv4_mapped_ipv6(self):
        self.source('::ffff:192.0.2.1\n')
        self.update(family='inet6')
        self.ipset('test', 'testset', '::ffff:192.0.2.1')

    def test_mixed_sources_filter_and_count_entries(self):
        logfile = self.root / 'logger.log'
        self.env['QA_LOG'] = str(logfile)
        self.stub('logger', 'printf "%s\\n" "$*" >> "$QA_LOG"')
        self.source('# comment\n\n198.51.100.1\n2001:db8::2\nnot-an-address\n')
        self.source('198.51.100.0/24\n2001:db8::/120\n::ffff:192.0.2.1\n', 'second.dat')
        for family, accepted, skipped in (('inet', 2, 4), ('inet6', 3, 3)):
            with self.subTest(family=family):
                self.seed(kind='hash:net', family=family)
                result = self.update('--logger', kind='hash:net', family=family)
                self.assertEqual(result.stdout, '')
                self.assertEqual(result.stderr, '')
                self.assertIn(f'2 files with {accepted} input entries, {skipped} skipped',
                              logfile.read_text())
                info = self.ipset('list', 'testset', '-t').stdout
                self.assertIn(f'maxelem {10000 + accepted} ', info)
                self.assertIn(f'Number of entries: {accepted}', info)
                entry = '198.51.100.1' if family == 'inet' else '2001:db8::2'
                self.ipset('test', 'testset', entry)
                self.ipset('destroy', 'testset')

    def test_filtered_empty_set_policy(self):
        before = self.seed()
        self.source('2001:db8::1\nnot-an-address\n')
        self.assertEqual(self.update('--disallow-empty-set', check=False).returncode, 1)
        self.unchanged(before)
        args = self.arguments()
        args.remove('--quiet')
        result = self.command(*args, env=self.env)
        self.assertIn('0 input entries, 2 skipped', result.stdout)
        info = self.ipset('list', 'testset', '-t').stdout
        self.assertIn('maxelem 10000 ', info)
        self.assertIn('Number of entries: 0', info)

    def test_invalid_address_preserves_active_set(self):
        before = self.seed()
        self.source('198.51.100.1\n999.999.999.999\n')
        self.assertEqual(self.update(check=False).returncode, 2)
        self.unchanged(before)

    def test_failed_first_load_leaves_no_sets(self):
        self.source('999.999.999.999\n')
        self.assertEqual(self.update(check=False).returncode, 2)
        self.assertEqual(self.ipset('list', '-n').stdout, '')

    def test_empty_set_policy(self):
        before = self.seed()
        self.source(' # comment only\n')
        self.assertEqual(self.update('--disallow-empty-set', check=False).returncode, 1)
        self.unchanged(before)
        self.update()
        self.assertIn('Number of entries: 0', self.ipset('list', 'testset', '-t').stdout)

    def test_enumeration_failure(self):
        before = self.seed()
        self.source('198.51.100.1\n')
        self.stub('find', 'exit 1')
        self.assertEqual(self.update(check=False).returncode, 74)
        self.unchanged(before)

    def test_partial_source_read_failure(self):
        before = self.seed()
        self.source('198.51.100.1\n')
        self.stub('cat', "printf '198.51.100.1\\n'; exit 1")
        self.assertEqual(self.update(check=False).returncode, 74)
        self.unchanged(before)

    def test_nonregular_source(self):
        before = self.seed()
        os.mkfifo(self.data / 'pipe.dat')
        self.assertEqual(self.update(check=False).returncode, 74)
        self.unchanged(before)

    def test_dump_write_failure(self):
        before = self.seed()
        self.source('198.51.100.1\n')
        self.stub('mktemp', 'dir=$(' + shutil.which('mktemp') + ' "$@") || exit $?\n'
                  'ln -s /dev/full "$dir/testset.ipset"\nprintf "%s\\n" "$dir"')
        self.assertEqual(self.update(check=False).returncode, 74)
        self.unchanged(before)


    def test_mktemp_failure(self):
        before = self.seed()
        self.source('198.51.100.1\n')
        self.stub('mktemp', 'exit 1')
        self.assertEqual(self.update(check=False).returncode, 74)
        self.unchanged(before)

    def test_temporary_path_with_spaces(self):
        tmpdir = self.root / 'temporary files'
        tmpdir.mkdir()
        self.env['TMPDIR'] = str(tmpdir)
        self.source('192.0.2.1\n')
        self.update()
        self.ipset('test', 'testset', '192.0.2.1')
        self.assertEqual(list(tmpdir.iterdir()), [])

    def test_keep_working_dir(self):
        tmpdir = self.root / 'tmp'
        tmpdir.mkdir()
        self.env['TMPDIR'] = str(tmpdir)
        self.source('192.0.2.1\n')
        self.update('--keep-working-dir')
        directories = list(tmpdir.iterdir())
        self.assertEqual(len(directories), 1)
        self.assertTrue((directories[0] / 'testset.ipset').is_file())
        self.assertEqual(self.ipset('list', '-n').stdout.splitlines(), ['testset'])

    def test_random_name_collision_preserves_foreign_set(self):
        work = self.root / 'updipset.ABCDE12345'
        self.stub('mktemp', 'mkdir -- ' + shlex.quote(str(work)) + '\nprintf "%s\\n" ' + shlex.quote(str(work)))
        self.ipset('create', 'testset_tmp_ABCDE12345', 'hash:ip')
        self.ipset('add', 'testset_tmp_ABCDE12345', '203.0.113.1')
        before = self.ipset('save').stdout
        self.source('192.0.2.1\n')
        self.assertEqual(self.update(check=False).returncode, 2)
        self.assertEqual(self.ipset('save').stdout, before)
        self.assertFalse(work.exists())

    def wait_for_file(self, path, children):
        deadline = time.monotonic() + 10
        while not path.exists():
            self.assertLess(time.monotonic(), deadline, 'Timed out waiting for ' + str(path))
            for child in children:
                self.assertIsNone(child.poll(), 'Child exited before synchronization point')
            time.sleep(.01)

    def stop_child(self, child):
        if getattr(child, '_test_cleanup_done', False):
            return
        # Killing only the shell leaves foreground commands alive, including
        # commands retaining pipes after the shell has already exited.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.communicate(timeout=10)
        child._test_cleanup_done = True

    def finish_child(self, child, timeout=10):
        try:
            return child.communicate(timeout=timeout)
        finally:
            self.stop_child(child)

    def test_termination_cleans_up_during_create_and_restore(self):
        before = self.seed()
        self.source('198.51.100.1\n')
        for stage in ('create', 'restore'):
            with self.subTest(stage=stage):
                ready = self.root / (stage + '.ready')
                release = self.root / (stage + '.release')
                self.env.update(QA_STAGE=stage, QA_READY=str(ready), QA_RELEASE=str(release), QA_IPSET=IPSET)
                self.stub('ipset', """
if [[ $QA_STAGE == create && $1 == create && $2 == *_tmp_* ]]; then
    "$QA_IPSET" "$@" || exit $?
elif [[ $QA_STAGE != restore || $1 != '-!' ]]; then
    exec "$QA_IPSET" "$@"
fi
touch "$QA_READY"
while [[ ! -e $QA_RELEASE ]]; do sleep .01; done
if [[ $QA_STAGE == restore ]]; then exec "$QA_IPSET" "$@"; fi
exit 0
""")
                child = subprocess.Popen(self.arguments(), env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
                self.addCleanup(self.stop_child, child)
                self.wait_for_file(ready, [child])
                child.terminate()
                release.touch()
                out, err = self.finish_child(child)
                self.assertEqual(child.returncode, 143, (out, err))
                self.unchanged(before)

    def test_empty_cli_argument_is_not_ignored(self):
        self.source('192.0.2.1\n')
        self.assertEqual(self.update('', check=False).returncode, 64)
        self.assertEqual(self.ipset('list', '-n').stdout, '')



    def test_first_publication_never_creates_empty_target(self):
        self.source('192.0.2.1\n')
        self.env['QA_IPSET'] = IPSET
        self.stub('ipset', """
if [[ $1 == create && $2 == testset ]]; then
    echo 'An empty target must not be created' >&2; exit 1
fi
if [[ $1 == rename ]]; then
    "$QA_IPSET" test "$2" 192.0.2.1 || exit 1
fi
exec "$QA_IPSET" "$@"
""")
        self.update()
        self.ipset('test', 'testset', '192.0.2.1')

    def test_incompatible_family_preserves_active_set(self):
        before = self.seed()
        self.source('2001:db8::1\n')
        self.assertEqual(self.update(family='inet6', check=False).returncode, 2)
        self.unchanged(before)

    def test_concurrent_updates_have_private_sets(self):
        for existing in (True, False):
            with self.subTest(existing=existing):
                name = 'long-target-name-for-testing'
                if existing:
                    self.ipset('create', name, 'hash:ip', 'maxelem', '1000000')
                    self.ipset('add', name, '192.0.2.100')
                markers = self.root / ('existing' if existing else 'new')
                markers.mkdir()
                self.env.update(QA_IPSET=IPSET, QA_MARKERS=str(markers), QA_TARGET=name)
                self.stub('ipset', """
if [[ $1 == rename ]]; then
    printf '%s' "$2" > "$QA_MARKERS/$QA_ID.ready"
    while [[ ! -e $QA_MARKERS/$QA_ID.release ]]; do sleep .01; done
fi
exec "$QA_IPSET" "$@"
""")
                children = []
                for ident, entry in (('a', '192.0.2.1'), ('b', '192.0.2.2')):
                    data = markers / ident
                    data.mkdir()
                    (data / 'entries.dat').write_text(entry + '\n')
                    env = dict(self.env, QA_ID=ident)
                    child = subprocess.Popen(self.arguments(name=name, data=data), env=env,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
                    self.addCleanup(self.stop_child, child)
                    children.append(child)
                for ident in ('a', 'b'):
                    self.wait_for_file(markers / (ident + '.ready'), children)
                temp_a, temp_b = [(markers / (ident + '.ready')).read_text() for ident in ('a', 'b')]
                self.assertNotEqual(temp_a, temp_b)
                self.assertLessEqual(max(len(temp_a), len(temp_b)), 31)
                self.ipset('test', temp_a, '192.0.2.1')
                self.ipset('test', temp_b, '192.0.2.2')
                if existing:
                    self.ipset('test', name, '192.0.2.100')
                else:
                    self.assertNotIn(name, self.ipset('list', '-n').stdout.splitlines())
                (markers / 'b.release').touch()
                out, err = self.finish_child(children[1])
                self.assertEqual(children[1].returncode, 0, (out, err))
                self.ipset('test', name, '192.0.2.2')
                self.ipset('test', temp_a, '192.0.2.1')
                (markers / 'a.release').touch()
                out, err = self.finish_child(children[0])
                self.assertEqual(children[0].returncode, 0, (out, err))
                self.ipset('test', name, '192.0.2.1')
                self.assertEqual(self.ipset('list', '-n').stdout.splitlines(), [name])
                self.ipset('destroy', name)

    def test_cleanup_failure_reports_already_published_data(self):
        self.seed()
        self.source('198.51.100.1\n')
        marker = self.root / 'destroy-failed'
        self.env.update(QA_IPSET=IPSET, QA_MARKER=str(marker))
        self.stub('ipset', """
if [[ $1 == destroy && ! -e $QA_MARKER ]]; then
    touch "$QA_MARKER"; echo 'Injected destroy failure' >&2; exit 1
fi
exec "$QA_IPSET" "$@"
""")
        result = self.update(check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn('was updated, but final cleanup failed', result.stderr)
        self.ipset('test', 'testset', '198.51.100.1')
        self.assertEqual(self.ipset('list', '-n').stdout.splitlines(), ['testset'])



    def test_logger_includes_failure_details(self):
        before = self.seed()
        self.source('198.51.100.1\n999.999.999.999\n')
        logfile = self.root / 'logger.log'
        self.env['QA_LOG'] = str(logfile)
        self.stub('logger', 'printf "%s\\n" "$*" >> "$QA_LOG"')
        self.assertNotEqual(self.update('--logger', check=False).returncode, 0)
        self.assertIn('999.999.999.999', logfile.read_text())
        self.unchanged(before)

    def test_workdir_cleanup_failure_does_not_log_success(self):
        self.source('192.0.2.1\n')
        logfile = self.root / 'logger.log'
        marker = self.root / 'rm-failed'
        self.env.update(QA_LOG=str(logfile), QA_MARKER=str(marker))
        self.stub('logger', 'printf "%s\\n" "$*" >> "$QA_LOG"')
        self.stub('rm', 'if [[ ! -e $QA_MARKER ]]; then touch "$QA_MARKER"; exit 1; fi\nexec '
                  + shutil.which('rm') + ' "$@"')
        self.assertEqual(self.update('--logger', check=False).returncode, 74)
        self.assertIn('was updated, but final cleanup failed', logfile.read_text())
        self.assertNotIn('successfully updated', logfile.read_text())
        self.ipset('test', 'testset', '192.0.2.1')



    def test_buffered_parser_counts_multiple_sources(self):
        self.source('# first\n192.0.2.1\n192.0.2.2', 'first.dat')
        self.source('192.0.2.2\n192.0.2.3\n\n', 'second.dat')
        logfile = self.root / 'logger.log'
        self.env['QA_LOG'] = str(logfile)
        self.stub('logger', 'printf "%s\\n" "$*" >> "$QA_LOG"')
        self.update('--logger')
        self.assertIn('2 files with 4 input entries', logfile.read_text())
        self.assertIn('Number of entries: 3', self.ipset('list', 'testset', '-t').stdout)

    def test_parser_paths_preserve_backslashes(self):
        tmpdir = self.root / r'tmp\nfiles'
        tmpdir.mkdir()
        self.env['TMPDIR'] = str(tmpdir)
        self.source('192.0.2.1\n', r'list\n.dat')
        self.update()
        self.source('192.0.2.1\ninvalid-entry\n', r'list\n.dat')
        args = self.arguments()
        args.remove('--quiet')
        result = self.command(*args, '--verbose', env=self.env)
        self.assertIn(r'list\n.dat at line 2: invalid-entry', result.stdout)
        self.assertIn('1 input entries, 1 skipped', result.stdout)
        self.ipset('test', 'testset', '192.0.2.1')
        self.assertIn('Number of entries: 1', self.ipset('list', 'testset', '-t').stdout)

    def test_counter_write_failure_preserves_active_set(self):
        before = self.seed()
        self.source('198.51.100.1\n')
        self.stub('mktemp', 'dir=$(' + shutil.which('mktemp') + ' "$@") || exit $?\n'
                  'ln -s /dev/full "$dir/source.count"\nprintf "%s\\n" "$dir"')
        self.assertEqual(self.update(check=False).returncode, 74)
        self.unchanged(before)

    def test_parser_failure_preserves_active_set(self):
        before = self.seed()
        self.source('198.51.100.1\n')
        self.stub('awk', 'echo "injected parser failure" >&2; exit 2')
        result = self.update(check=False)
        self.assertEqual(result.returncode, 74)
        self.assertIn('injected parser failure', result.stderr)
        self.unchanged(before)


    def test_timeout_terminates_descendants(self):
        for parent_exits in (False, True):
            with self.subTest(parent_exits=parent_exits):
                pidfile = self.root / ('worker-' + str(parent_exits))
                # The worker inherits the output pipes. Even if its parent
                # exits, communicate must time out and terminate the worker.
                command = 'sleep 60 & printf "%s\\n" "$!" > "$1"; '
                command += 'exit 0' if parent_exits else 'wait'
                with self.assertRaises(subprocess.TimeoutExpired):
                    self.command('bash', '-c', command, 'timeout-test', str(pidfile), timeout=1)
                pid = int(pidfile.read_text())
                deadline = time.monotonic() + 2
                while True:
                    try:
                        state = Path('/proc', str(pid), 'stat').read_text().rsplit(') ', 1)[1].split()[0]
                    except (FileNotFoundError, ProcessLookupError):
                        break
                    if state == 'Z':
                        break  # Exited; only the parent's reaping remains.
                    self.assertLess(time.monotonic(), deadline, 'Descendant survived timeout')
                    time.sleep(.01)



    def test_capacity_follows_input_size(self):
        self.source(''.join('192.0.2.%d\n' % n for n in range(1, 6)))
        self.update()
        self.assertIn('maxelem 10005 ', self.ipset('list', 'testset', '-t').stdout)
        self.source('')
        self.update()
        self.assertIn('maxelem 10000 ', self.ipset('list', 'testset', '-t').stdout)

    def test_ipv4_cidr_expansion_grows_existing_hash_ip(self):
        self.source('192.0.2.1\n')
        self.update()
        self.ipset('create', 'reference', 'list:set')
        self.ipset('add', 'reference', 'testset')
        self.source('198.51.0.0/18\n')
        self.update()
        info = self.ipset('list', 'testset', '-t').stdout
        self.assertIn('maxelem 26384 ', info)
        self.assertIn('Number of entries: 16384', info)
        self.assertIn('References: 1', info)
        self.ipset('test', 'testset', '198.51.0.0')
        self.ipset('test', 'testset', '198.51.63.255')
        self.ipset('test', 'reference', 'testset')

    def test_capacity_counts_networks_and_duplicates_conservatively(self):
        self.source('192.0.2.0/24\n192.0.2.0/24\n', 'first.dat')
        self.source('198.51.100.0/24\n', 'second.dat')
        self.update(kind='hash:net')
        info = self.ipset('list', 'testset', '-t').stdout
        self.assertIn('maxelem 10003 ', info)
        self.assertIn('Number of entries: 2', info)

    def test_capacity_above_ten_million_and_uint32_saturation(self):
        before = self.seed()
        capture = self.root / 'create-args'
        self.env.update(QA_IPSET=IPSET, QA_CREATE_ARGS=str(capture))
        # Inspect creation parameters without materializing millions of IPs.
        self.stub('ipset', """
if [[ $1 == create && $2 == *_tmp_* ]]; then
    printf '%s\\n' "$@" > "$QA_CREATE_ARGS"
    exit 1
fi
exec "$QA_IPSET" "$@"
""")
        for sources, expected in [
            (['10.0.0.0/8\n'], 16787216),
            (['128.0.0.0/1\n'] * 3, 4294967295),
            (['128.0.0.0/1\n' * 3], 4294967295),
        ]:
            with self.subTest(expected=expected, files=len(sources)):
                for source in self.data.glob('*.dat'):
                    source.unlink()
                for n, source in enumerate(sources):
                    self.source(source, str(n) + '.dat')
                self.assertEqual(self.update(check=False).returncode, 2)
                arguments = capture.read_text().splitlines()
                self.assertEqual(int(arguments[arguments.index('maxelem') + 1]), expected)
                self.unchanged(before)

    def test_invalid_cidr_preserves_active_set(self):
        before = self.seed()
        for entry in ('192.0.2.1/0', '192.0.2.1/33', '192.0.2.1/99999999999999999999'):
            with self.subTest(entry=entry):
                self.source(entry + '\n')
                self.assertEqual(self.update(check=False).returncode, 2)
                self.unchanged(before)



    def test_octal_cidr_capacity_matches_ipset(self):
        self.source('198.51.100.1/020\n')
        self.update()
        info = self.ipset('list', 'testset', '-t').stdout
        self.assertIn('maxelem 75536 ', info)
        self.assertIn('Number of entries: 65536', info)
        self.ipset('test', 'testset', '198.51.0.0')
        self.ipset('test', 'testset', '198.51.255.255')


if __name__ == '__main__':
    if '--inside-netns' not in sys.argv:
        if os.geteuid() != 0 or not IPSET or not shutil.which('unshare'):
            sys.exit('Tests require root, ipset and unshare on Linux.')
        sys.exit(subprocess.call(['unshare', '--net', '--', sys.executable,
                                  str(Path(__file__).resolve()), '--inside-netns', *sys.argv[1:]]))
    sys.argv.remove('--inside-netns')
    unittest.main()
