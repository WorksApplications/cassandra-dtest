import binascii
import glob
import os
import stat
import struct
import subprocess
import time

from cassandra import WriteTimeout
from cassandra.cluster import NoHostAvailable, OperationTimedOut

from ccmlib.common import is_win
from ccmlib.node import Node, TimeoutError
from assertions import assert_almost_equal, assert_none, assert_one
from dtest import Tester, debug
from tools import since, rows_to_list


class TestCommitLog(Tester):
    """ CommitLog Tests """

    def __init__(self, *argv, **kwargs):
        kwargs['cluster_options'] = {'start_rpc': 'true'}
        super(TestCommitLog, self).__init__(*argv, **kwargs)
        self.allow_log_errors = True

    def setUp(self):
        super(TestCommitLog, self).setUp()
        self.cluster.populate(1)
        [self.node1] = self.cluster.nodelist()

    def tearDown(self):
        self._change_commitlog_perms(stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        super(TestCommitLog, self).tearDown()

    def prepare(self, configuration={}, create_test_keyspace=True, **kwargs):
        conf = {'commitlog_sync_period_in_ms': 1000}

        conf.update(configuration)
        self.cluster.set_configuration_options(values=conf, **kwargs)
        self.cluster.start()
        self.session1 = self.patient_cql_connection(self.node1)
        if create_test_keyspace:
            self.session1.execute("DROP KEYSPACE IF EXISTS ks;")
            self.create_ks(self.session1, 'ks', 1)
            self.session1.execute("DROP TABLE IF EXISTS test;")
            query = """
              CREATE TABLE test (
                key int primary key,
                col1 int
              )
            """
            self.session1.execute(query)

    def _change_commitlog_perms(self, mod):
        path = self._get_commitlog_path()
        os.chmod(path, mod)
        commitlogs = glob.glob(path + '/*')
        for commitlog in commitlogs:
            os.chmod(commitlog, mod)

    def _get_commitlog_path(self):
        """ Returns the commitlog path """

        return os.path.join(self.node1.get_path(), 'commitlogs')

    def _get_commitlog_files(self):
        """ Returns the number of commitlog files in the directory """

        path = self._get_commitlog_path()
        return [os.path.join(path, p) for p in os.listdir(path)]

    def _get_commitlog_size(self):
        """ Returns the commitlog directory size in MB """

        path = self._get_commitlog_path()
        cmd_args = ['du', '-m', path]
        p = subprocess.Popen(cmd_args, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        stdout, stderr = p.communicate()
        exit_status = p.returncode
        self.assertEqual(0, exit_status,
                         "du exited with a non-zero status: %d" % exit_status)
        size = int(stdout.split('\t')[0])
        return size

    def _segment_size_test(self, segment_size_in_mb, compressed=False):
        """ Execute a basic commitlog test and validate the commitlog files """

        conf = {'commitlog_segment_size_in_mb': segment_size_in_mb}
        if compressed:
            conf['commitlog_compression'] = [{'class_name': 'LZ4Compressor'}]
        conf['memtable_heap_space_in_mb'] = 512
        self.prepare(configuration=conf, create_test_keyspace=False)

        segment_size = segment_size_in_mb * 1024 * 1024
        self.node1.stress(['write', 'n=150000', '-rate', 'threads=25'])
        time.sleep(1)

        commitlogs = self._get_commitlog_files()
        self.assertTrue(len(commitlogs) > 0, "No commit log files were created")

        # the most recently-written segment of the commitlog may be smaller
        # than the expected size, so we allow exactly one segment to be smaller
        smaller_found = False
        for i, f in enumerate(commitlogs):
            size = os.path.getsize(f)
            size_in_mb = int(size / 1024 / 1024)
            debug('segment file {} {}; smaller already found: {}'.format(f, size_in_mb, smaller_found))
            if size_in_mb < 1 or size < (segment_size * 0.1):
                continue  # commitlog not yet used

            try:
                if compressed:
                    # if compression is used, we assume there will be at most a 50% compression ratio
                    self.assertLess(size, segment_size)
                    self.assertGreater(size, segment_size / 2)
                else:
                    # if no compression is used, the size will be close to what we expect
                    assert_almost_equal(size, segment_size, error=0.05)
            except AssertionError as e:
                #  the last segment may be smaller
                if not smaller_found:
                    self.assertLessEqual(size, segment_size)
                    smaller_found = True
                else:
                    raise e

    def _provoke_commitlog_failure(self):
        """ Provoke the commitlog failure """

        # Test things are ok at this point
        self.session1.execute("""
            INSERT INTO test (key, col1) VALUES (1, 1);
        """)
        assert_one(
            self.session1,
            "SELECT * FROM test where key=1;",
            [1, 1]
        )

        self._change_commitlog_perms(0)

        with open(os.devnull, 'w') as devnull:
            self.node1.stress(['write', 'n=1M', '-col', 'size=FIXED(1000)', '-rate', 'threads=25'], stdout=devnull, stderr=subprocess.STDOUT)

    def test_commitlog_replay_on_startup(self):
        """ Test commit log replay """
        node1 = self.node1
        node1.set_configuration_options(batch_commitlog=True)
        node1.start()

        debug("Insert data")
        session = self.patient_cql_connection(node1)
        self.create_ks(session, 'Test', 1)
        session.execute("""
            CREATE TABLE users (
                user_name varchar PRIMARY KEY,
                password varchar,
                gender varchar,
                state varchar,
                birth_year bigint
            );
        """)
        session.execute("INSERT INTO Test. users (user_name, password, gender, state, birth_year) "
                        "VALUES('gandalf', 'p@$$', 'male', 'WA', 1955);")

        debug("Verify data is present")
        session = self.patient_cql_connection(node1)
        res = session.execute("SELECT * FROM Test. users")
        self.assertItemsEqual(rows_to_list(res),
                              [[u'gandalf', 1955, u'male', u'p@$$', u'WA']])

        debug("Stop node abruptly")
        node1.stop(gently=False)

        debug("Verify commitlog was written before abrupt stop")
        commitlog_dir = os.path.join(node1.get_path(), 'commitlogs')
        commitlog_files = os.listdir(commitlog_dir)
        self.assertTrue(len(commitlog_files) > 0)

        debug("Verify no SSTables were flushed before abrupt stop")
        data_dir = os.path.join(node1.get_path(), 'data')
        cf_id = [s for s in os.listdir(os.path.join(data_dir, "test")) if s.startswith("users")][0]
        cf_data_dir = glob.glob("{data_dir}/test/{cf_id}".format(**locals()))[0]
        cf_data_dir_files = os.listdir(cf_data_dir)
        if "backups" in cf_data_dir_files:
            cf_data_dir_files.remove("backups")
        self.assertEqual(0, len(cf_data_dir_files))

        debug("Verify commit log was replayed on startup")
        node1.start()
        node1.watch_log_for("Log replay complete")
        # Here we verify there was more than 0 replayed mutations
        zero_replays = node1.grep_log(" 0 replayed mutations")
        self.assertEqual(0, len(zero_replays))

        debug("Make query and ensure data is present")
        session = self.patient_cql_connection(node1)
        res = session.execute("SELECT * FROM Test. users")
        self.assertItemsEqual(rows_to_list(res),
                              [[u'gandalf', 1955, u'male', u'p@$$', u'WA']])

    def default_segment_size_test(self):
        """ Test default commitlog_segment_size_in_mb (32MB) """

        self._segment_size_test(32)

    def small_segment_size_test(self):
        """ Test a small commitlog_segment_size_in_mb (5MB) """

        self._segment_size_test(5)

    @since('2.2')
    def default_compressed_segment_size_test(self):
        """ Test default compressed commitlog_segment_size_in_mb (32MB) """

        self._segment_size_test(32, compressed=True)

    @since('2.2')
    def small_compressed_segment_size_test(self):
        """ Test a small compressed commitlog_segment_size_in_mb (5MB) """

        self._segment_size_test(5, compressed=True)

    def stop_failure_policy_test(self):
        """ Test the stop commitlog failure policy (default one) """
        self.prepare()

        self._provoke_commitlog_failure()
        failure = self.node1.grep_log("Failed .+ commit log segments. Commit disk failure policy is stop; terminating thread")
        debug(failure)
        self.assertTrue(failure, "Cannot find the commitlog failure message in logs")
        self.assertTrue(self.node1.is_running(), "Node1 should still be running")

        # Cannot write anymore after the failure
        with self.assertRaises(NoHostAvailable):
            self.session1.execute("""
              INSERT INTO test (key, col1) VALUES (2, 2);
            """)

        # Should not be able to read neither
        with self.assertRaises(NoHostAvailable):
            self.session1.execute("""
              "SELECT * FROM test;"
            """)

    def stop_commit_failure_policy_test(self):
        """ Test the stop_commit commitlog failure policy """
        self.prepare(configuration={
            'commit_failure_policy': 'stop_commit'
        })

        self.session1.execute("""
            INSERT INTO test (key, col1) VALUES (2, 2);
        """)

        self._provoke_commitlog_failure()
        failure = self.node1.grep_log("Failed .+ commit log segments. Commit disk failure policy is stop_commit; terminating thread")
        debug(failure)
        self.assertTrue(failure, "Cannot find the commitlog failure message in logs")
        self.assertTrue(self.node1.is_running(), "Node1 should still be running")

        # Cannot write anymore after the failure
        with self.assertRaises((OperationTimedOut, WriteTimeout)):
            self.session1.execute("""
              INSERT INTO test (key, col1) VALUES (2, 2);
            """)

        # Should be able to read
        assert_one(
            self.session1,
            "SELECT * FROM test where key=2;",
            [2, 2]
        )

    def die_failure_policy_test(self):
        """ Test the die commitlog failure policy """
        self.prepare(configuration={
            'commit_failure_policy': 'die'
        })

        self._provoke_commitlog_failure()
        failure = self.node1.grep_log("ERROR \[COMMIT-LOG-ALLOCATOR\].+JVM state determined to be unstable.  Exiting forcefully")
        debug(failure)
        self.assertTrue(failure, "Cannot find the commitlog failure message in logs")
        self.assertFalse(self.node1.is_running(), "Node1 should not be running")

    def ignore_failure_policy_test(self):
        """ Test the ignore commitlog failure policy """
        self.prepare(configuration={
            'commit_failure_policy': 'ignore'
        })

        self._provoke_commitlog_failure()
        failure = self.node1.grep_log("ERROR \[COMMIT-LOG-ALLOCATOR\].+Failed .+ commit log segments")
        self.assertTrue(failure, "Cannot find the commitlog failure message in logs")
        self.assertTrue(self.node1.is_running(), "Node1 should still be running")

        # on Windows, we can't delete the segments if they're chmod to 0 so they'll still be available for use by CLSM,
        # and we can still create new segments since os.chmod is limited to stat.S_IWRITE and stat.S_IREAD to set files
        # as read-only. New mutations will still be allocated and WriteTimeouts will not be raised. It's sufficient that
        # we confirm that a) the node isn't dead (stop) and b) the node doesn't terminate the thread (stop_commit)
        query = "INSERT INTO test (key, col1) VALUES (2, 2);"
        if is_win():
            # We expect this to succeed
            self.session1.execute(query)
            self.assertFalse(self.node1.grep_log("terminating thread"), "thread was terminated but CL error should have been ignored.")
            self.assertTrue(self.node1.is_running(), "Node1 should still be running after an ignore error on CL")
        else:
            with self.assertRaises((OperationTimedOut, WriteTimeout)):
                self.session1.execute(query)

            # Should not exist
            assert_none(self.session1, "SELECT * FROM test where key=2;")

        # bring back the node commitlogs
        self._change_commitlog_perms(stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)

        self.session1.execute("""
          INSERT INTO test (key, col1) VALUES (3, 3);
        """)
        assert_one(
            self.session1,
            "SELECT * FROM test where key=3;",
            [3, 3]
        )

        time.sleep(2)
        assert_one(
            self.session1,
            "SELECT * FROM test where key=2;",
            [2, 2]
        )

    def test_bad_crc(self):
        """
        if the commit log header crc (checksum) doesn't match the actual crc of the header data,
        and the commit_failure_policy is stop, C* shouldn't startup
        @jira_ticket CASSANDRA-9749
        """
        if not hasattr(self, 'ignore_log_patterns'):
            self.ignore_log_patterns = []

        expected_error = "Exiting due to error while processing commit log during initialization."
        self.ignore_log_patterns.append(expected_error)
        node = self.node1
        assert isinstance(node, Node)
        node.set_configuration_options({'commit_failure_policy': 'stop', 'commitlog_sync_period_in_ms': 1000})
        self.cluster.start()

        cursor = self.patient_cql_connection(self.cluster.nodelist()[0])
        self.create_ks(cursor, 'ks', 1)
        cursor.execute("CREATE TABLE ks.tbl (k INT PRIMARY KEY, v INT)")

        for i in range(10):
            cursor.execute("INSERT INTO ks.tbl (k, v) VALUES ({0}, {0})".format(i))

        results = list(cursor.execute("SELECT * FROM ks.tbl"))
        self.assertEqual(len(results), 10)

        # with the commitlog_sync_period_in_ms set to 1000,
        # this sleep guarantees that the commitlog data is
        # actually flushed to disk before we kill -9 it
        time.sleep(1)

        node.stop(gently=False)

        # check that ks.tbl hasn't been flushed
        path = node.get_path()
        ks_dir = os.path.join(path, 'data', 'ks')
        db_dir = os.listdir(ks_dir)[0]
        sstables = len([f for f in os.listdir(os.path.join(ks_dir, db_dir)) if f.endswith('.db')])
        self.assertEqual(sstables, 0)

        # modify the commit log crc values
        cl_dir = os.path.join(path, 'commitlogs')
        self.assertTrue(len(os.listdir(cl_dir)) > 0)
        for cl in os.listdir(cl_dir):
            # locate the CRC location
            with open(os.path.join(cl_dir, cl), 'r') as f:
                f.seek(0)
                version = struct.unpack('>i', f.read(4))[0]
                crc_pos = 12
                if version >= 5:
                    f.seek(crc_pos)
                    psize = struct.unpack('>h', f.read(2))[0] & 0xFFFF
                    crc_pos += 2 + psize

            # rewrite it with crap
            with open(os.path.join(cl_dir, cl), 'w') as f:
                f.seek(crc_pos)
                f.write(struct.pack('>i', 123456))

            # verify said crap
            with open(os.path.join(cl_dir, cl), 'r') as f:
                f.seek(crc_pos)
                crc = struct.unpack('>i', f.read(4))[0]
                self.assertEqual(crc, 123456)

        mark = node.mark_log()
        node.start()
        node.watch_log_for(expected_error, from_mark=mark)
        with self.assertRaises(TimeoutError):
            node.wait_for_binary_interface(from_mark=mark, timeout=20)
        self.assertFalse(node.is_running())

    def test_compression_error(self):
        """
        if the commit log header refers to an unknown compression class, and the commit_failure_policy is stop, C* shouldn't startup
        """
        if not hasattr(self, 'ignore_log_patterns'):
            self.ignore_log_patterns = []

        expected_error = 'Could not create Compression for type org.apache.cassandra.io.compress.LZ5Compressor'
        self.ignore_log_patterns.append(expected_error)
        node = self.node1
        assert isinstance(node, Node)
        node.set_configuration_options({'commit_failure_policy': 'stop',
                                        'commitlog_compression': [{'class_name': 'LZ4Compressor'}],
                                        'commitlog_sync_period_in_ms': 1000})
        self.cluster.start()

        cursor = self.patient_cql_connection(self.cluster.nodelist()[0])
        self.create_ks(cursor, 'ks1', 1)
        cursor.execute("CREATE TABLE ks1.tbl (k INT PRIMARY KEY, v INT)")

        for i in range(10):
            cursor.execute("INSERT INTO ks1.tbl (k, v) VALUES ({0}, {0})".format(i))

        results = list(cursor.execute("SELECT * FROM ks1.tbl"))
        self.assertEqual(len(results), 10)

        # with the commitlog_sync_period_in_ms set to 1000,
        # this sleep guarantees that the commitlog data is
        # actually flushed to disk before we kill -9 it
        time.sleep(1)

        node.stop(gently=False)

        # check that ks1.tbl hasn't been flushed
        path = node.get_path()
        ks_dir = os.path.join(path, 'data', 'ks1')
        db_dir = os.listdir(ks_dir)[0]
        sstables = len([f for f in os.listdir(os.path.join(ks_dir, db_dir)) if f.endswith('.db')])
        self.assertEqual(sstables, 0)

        def get_header_crc(header):
            """
            When calculating the header crc, C* splits up the 8b id, first adding the 4 least significant
            bytes to the crc, then the 5 most significant bytes, so this splits them and calculates the same way
            """
            new_header = header[:4]
            # C* evaluates most and least significant 4 bytes out of order
            new_header += header[8:12]
            new_header += header[4:8]
            # C* evaluates the short parameter length as an int
            new_header += '\x00\x00' + header[12:14]  # the
            new_header += header[14:]
            return binascii.crc32(new_header)

        # modify the compression parameters to look for a compressor that isn't there
        # while this scenario is pretty unlikely, if a jar or lib got moved or something,
        # you'd have a similar situation, which would be fixable by the user
        cl_dir = os.path.join(path, 'commitlogs')
        self.assertTrue(len(os.listdir(cl_dir)) > 0)
        for cl in os.listdir(cl_dir):
            # read the header and find the crc location
            with open(os.path.join(cl_dir, cl), 'r') as f:
                f.seek(0)
                crc_pos = 12
                f.seek(crc_pos)
                psize = struct.unpack('>h', f.read(2))[0] & 0xFFFF
                crc_pos += 2 + psize

                header_length = crc_pos
                f.seek(crc_pos)
                crc = struct.unpack('>i', f.read(4))[0]

                # check that we're going this right
                f.seek(0)
                header_bytes = f.read(header_length)
                self.assertEqual(get_header_crc(header_bytes), crc)

            # rewrite it with imaginary compressor
            self.assertIn('LZ4Compressor', header_bytes)
            header_bytes = header_bytes.replace('LZ4Compressor', 'LZ5Compressor')
            self.assertNotIn('LZ4Compressor', header_bytes)
            self.assertIn('LZ5Compressor', header_bytes)
            with open(os.path.join(cl_dir, cl), 'w') as f:
                f.seek(0)
                f.write(header_bytes)
                f.seek(crc_pos)
                f.write(struct.pack('>i', get_header_crc(header_bytes)))

            # verify we wrote everything correctly
            with open(os.path.join(cl_dir, cl), 'r') as f:
                f.seek(0)
                self.assertEqual(f.read(header_length), header_bytes)
                f.seek(crc_pos)
                crc = struct.unpack('>i', f.read(4))[0]
                self.assertEqual(crc, get_header_crc(header_bytes))

        mark = node.mark_log()
        node.start()
        node.watch_log_for(expected_error, from_mark=mark)
        with self.assertRaises(TimeoutError):
            node.wait_for_binary_interface(from_mark=mark, timeout=20)
