import bisect
import operator
import os
import pprint
import random
import re
import schema_metadata_test
import signal
import subprocess
import time
import uuid
from collections import defaultdict
from distutils.version import LooseVersion
from multiprocessing import Process, Queue
from Queue import Empty, Full

import psutil

from cassandra import ConsistencyLevel, WriteTimeout
from cassandra.query import SimpleStatement
from dtest import DEFAULT_DIR, Tester, debug
from tools import generate_ssl_stores, new_node

# Versions are tuples of (major_ver, minor_ver)
# Used to build upgrade path(s) for tests. Some tests will go from start to finish,
# other tests will focus on single upgrades from UPGRADE_PATH[n] to UPGRADE_PATH[n+1]

TRUNK_VER = (3, 2)

# maps protocol version to c* version(s)
PROTOCOL_PATHS = {
    1: [(2, 0), (2, 1), (2, 2)],
    2: [(2, 0), (2, 1), (2, 2)],
    3: [(2, 1), (2, 2), (3, 0), TRUNK_VER],
    4: [(2, 2), (3, 0), TRUNK_VER]
}

PROTOCOL_VERSION = int(os.environ.get('PROTOCOL_VERSION', 3))

CUSTOM_PATH = os.environ.get('UPGRADE_PATH', None)
if CUSTOM_PATH:
    # provide a custom path like so: "1_2:2_0:2_1" to test upgrading from 1.2 to 2.0 to 2.1
    UPGRADE_PATH = []
    for _vertup in CUSTOM_PATH.split(':'):
        _major, _minor = _vertup.split('_')
        UPGRADE_PATH.append((int(_major), int(_minor)))
else:
    UPGRADE_PATH = PROTOCOL_PATHS[PROTOCOL_VERSION]

LOCAL_MODE = os.environ.get('LOCAL_MODE', '').lower() in ('yes', 'true')
if LOCAL_MODE:
    REPO_LOCATION = os.environ.get('CASSANDRA_DIR')
else:
    REPO_LOCATION = "https://git-wip-us.apache.org/repos/asf/cassandra.git"

# lets cache this once so we don't make a bunch of remote requests
GIT_LS = subprocess.check_output(["git", "ls-remote", "-h", "-t", REPO_LOCATION]).rstrip()

# maps ref type (branch, tags) to ref names and sha's
MAPPED_REFS = defaultdict(dict)
for row in GIT_LS.split('\n'):
    sha, _fullref = row.split('\t')
    _, ref_type, ref = _fullref.split('/')
    MAPPED_REFS[ref_type][ref.split('^')[0]] = sha

# We often want this post-mortem when debugging may have been disabled, so print/pprint is intentional here
print("************************************* GIT REFS USED FOR THIS TEST RUN *********************************************")
print("************************** KEEP IN MIND THAT A SHA MAY POINT TO ANOTHER COMMIT SHA! *******************************")
for ref_type in MAPPED_REFS.keys():
    print("Git refs for {}:").format(ref_type.upper())
    pprint.pprint(MAPPED_REFS[ref_type], indent=4)

if os.environ.get('CASSANDRA_VERSION'):
    debug('CASSANDRA_VERSION is not used by upgrade tests!')


def sha_for_ref_name(ref_name, ref_type='tags'):
    return MAPPED_REFS[ref_type][ref_name]


class GitSemVer(object):
    """
    Wraps a git ref up with a semver (as LooseVersion)
    """
    git_ref = None
    semver = None

    def __init__(self, git_ref, semver_str):
        self.git_ref = git_ref
        self.semver = LooseVersion(semver_str)
        if semver_str == 'trunk':
            self.semver = LooseVersion(make_ver_str(TRUNK_VER))

    def __cmp__(self, other):
        # when comparing x.y.z and x.y.z-foo, we need to value x.y.z higher than the "nicknamed" tag x.y.z-foo
        # likewise for shorter versions of the form X.Y and X.Y-foo
        # to accomplish this, check if "x.y.z-" (note the dash there) is contained within "x.y.z-foo", and if so declare x.y.z the higher version
        # e.g. when comparing 3.0.0 and 3.0.0-rc1, consider 3.0.0 higher
        # e.g. when comparing 3.3 and 3.3-beta1, consider 3.3 higher
        if (len(self.semver.version) <= 3) or (len(other.semver.version) <= 3):
            if self.semver.vstring + "-" in other.semver.vstring:
                return 1
            elif other.semver.vstring + "-" in self.semver.vstring:
                return -1
            elif other.semver.vstring == self.semver.vstring:
                return 0

        return cmp(self.semver, other.semver)


def latest_tag_matching(ver_tuple):
    """
    Returns the latest tag matching a version tuple, such as (1, 2) to represent version 1.2
    """
    ver_str = make_ver_str(ver_tuple)

    wrappers = []
    # step through each tag found in the git repo
    # check if the tag is a match for the base version provided in ver_tuple
    # if it's a match add it to wrappers and when we complete this process give back the latest version found
    for t in MAPPED_REFS['tags'].keys():
        # let's short circuit if the tag we are checking matches the cassandra-x.y.z format, otherwise make another attempt for x.y.z-foo in case it's something line 1.2.3-tentative
        match = re.match('^cassandra-({ver_str}\.\d+(-+\w+)*)$'.format(ver_str=ver_str), t) or re.match('^({ver_str}\.\d*(-+\w+)*)$'.format(ver_str=ver_str), t)
        if match:
            gsv = GitSemVer(t, match.group(1))
            bisect.insort(wrappers, gsv)

    if wrappers:
        latest = wrappers.pop().git_ref
        return latest

    return None


def make_ver_str(_tuple):
    """
    Takes a tuple like (1,2) and returns a string like '1.2'
    """
    return '{}.{}'.format(_tuple[0], _tuple[1])


def make_branch_str(_tuple):
    """
    Takes a tuple like (1,2) and formats that version specifier as something
    like 'cassandra-1.2' to match the branch naming convention
    """

    # special case trunk version to just return 'trunk'
    if _tuple == TRUNK_VER:
        return 'trunk'

    return 'cassandra-{}.{}'.format(_tuple[0], _tuple[1])


def sanitize_version(version):
    """
    Takes versions of the form cassandra-1.2, 2.0.10, or trunk.
    Returns just the version string 'X.Y.Z'
    """
    if version.find('-') >= 0:
        return LooseVersion(version.split('-')[1])
    elif version == 'trunk':
        return LooseVersion(make_ver_str(TRUNK_VER))
    else:
        return LooseVersion(version)


def switch_jdks(version):
    version = sanitize_version(version)
    try:
        if version < '2.1':
            os.environ['JAVA_HOME'] = os.environ['JAVA7_HOME']
        else:
            os.environ['JAVA_HOME'] = os.environ['JAVA8_HOME']
    except KeyError:
        raise RuntimeError("You need to set JAVA7_HOME and JAVA8_HOME to run these tests!")
    debug("Set JAVA_HOME: [{}] for cassandra version: [{}]".format(os.environ['JAVA_HOME'], version))


def data_writer(tester, to_verify_queue, verification_done_queue, rewrite_probability=0):
    """
    Process for writing/rewriting data continuously.

    Pushes to a queue to be consumed by data_checker.

    Pulls from a queue of already-verified rows written by data_checker that it can overwrite.

    Intended to be run using multiprocessing.
    """
    # 'tester' is a cloned object so we shouldn't be inappropriately sharing anything with another process
    session = tester.patient_cql_connection(tester.node1, keyspace="upgrade", protocol_version=PROTOCOL_VERSION)

    prepared = session.prepare("UPDATE cf SET v=? WHERE k=?")
    prepared.consistency_level = ConsistencyLevel.QUORUM

    def handle_sigterm(signum, frame):
        # need to close queue gracefully if possible, or the data_checker process
        # can't seem to empty the queue and test failures result.
        to_verify_queue.close()
        exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)

    while True:
        try:
            key = None

            if (rewrite_probability > 0) and (random.randint(0, 100) <= rewrite_probability):
                try:
                    key = verification_done_queue.get_nowait()
                except Empty:
                    # we wanted a re-write but the re-writable queue was empty. oh well.
                    pass

            key = key or uuid.uuid4()

            val = uuid.uuid4()

            session.execute(prepared, (val, key))

            to_verify_queue.put_nowait((key, val,))
        except Exception:
            debug("Error in data writer process!")
            to_verify_queue.close()
            raise


def data_checker(tester, to_verify_queue, verification_done_queue):
    """
    Process for checking data continuously.

    Pulls from a queue written to by data_writer to know what to verify.

    Pushes to a queue to tell data_writer what's been verified and could be a candidate for re-writing.

    Intended to be run using multiprocessing.
    """
    # 'tester' is a cloned object so we shouldn't be inappropriately sharing anything with another process
    session = tester.patient_cql_connection(tester.node1, keyspace="upgrade", protocol_version=PROTOCOL_VERSION)

    prepared = session.prepare("SELECT v FROM cf WHERE k=?")
    prepared.consistency_level = ConsistencyLevel.QUORUM

    def handle_sigterm(signum, frame):
        # need to close queue gracefully if possible, or the data_checker process
        # can't seem to empty the queue and test failures result.
        verification_done_queue.close()
        exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)

    while True:
        try:
            # here we could block, but if the writer process terminates early with an empty queue
            # we would end up blocking indefinitely
            (key, expected_val) = to_verify_queue.get_nowait()

            actual_val = session.execute(prepared, (key,))[0][0]
        except Empty:
            time.sleep(0.1)  # let's not eat CPU if the queue is empty
            continue
        except Exception:
            debug("Error in data verifier process!")
            verification_done_queue.close()
            raise
        else:
            try:
                verification_done_queue.put_nowait(key)
            except Full:
                # the rewritable queue is full, not a big deal. drop this one.
                # we keep the rewritable queue held to a modest max size
                # and allow dropping some rewritables because we don't want to
                # rewrite rows in the same sequence as originally written
                pass

        tester.assertEqual(expected_val, actual_val, "Data did not match expected value!")


def counter_incrementer(tester, to_verify_queue, verification_done_queue, rewrite_probability=0):
    """
    Process for incrementing counters continuously.

    Pushes to a queue to be consumed by counter_checker.

    Pulls from a queue of already-verified rows written by data_checker that it can increment again.

    Intended to be run using multiprocessing.
    """
    # 'tester' is a cloned object so we shouldn't be inappropriately sharing anything with another process
    session = tester.patient_cql_connection(tester.node1, keyspace="upgrade", protocol_version=PROTOCOL_VERSION)

    prepared = session.prepare("UPDATE countertable SET c = c + 1 WHERE k1=?")
    prepared.consistency_level = ConsistencyLevel.QUORUM

    def handle_sigterm(signum, frame):
        # need to close queue gracefully if possible, or the data_checker process
        # can't seem to empty the queue and test failures result.
        to_verify_queue.close()
        exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)

    while True:
        try:
            key = None
            count = 0  # this will get set to actual last known count if we do a re-write

            if (rewrite_probability > 0) and (random.randint(0, 100) <= rewrite_probability):
                try:
                    key, count = verification_done_queue.get_nowait()
                except Empty:
                    # we wanted a re-write but the re-writable queue was empty. oh well.
                    pass

            key = key or uuid.uuid4()

            session.execute(prepared, (key))

            to_verify_queue.put_nowait((key, count + 1,))
        except Exception:
            debug("Error in counter incrementer process!")
            to_verify_queue.close()
            raise


def counter_checker(tester, to_verify_queue, verification_done_queue):
    """
    Process for checking counters continuously.

    Pulls from a queue written to by counter_incrementer to know what to verify.

    Pushes to a queue to tell counter_incrementer what's been verified and could be a candidate for incrementing again.

    Intended to be run using multiprocessing.
    """
    # 'tester' is a cloned object so we shouldn't be inappropriately sharing anything with another process
    session = tester.patient_cql_connection(tester.node1, keyspace="upgrade", protocol_version=PROTOCOL_VERSION)

    prepared = session.prepare("SELECT c FROM countertable WHERE k1=?")
    prepared.consistency_level = ConsistencyLevel.QUORUM

    def handle_sigterm(signum, frame):
        # need to close queue gracefully if possible, or the data_checker process
        # can't seem to empty the queue and test failures result.
        verification_done_queue.close()
        exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)

    while True:
        try:
            # here we could block, but if the writer process terminates early with an empty queue
            # we would end up blocking indefinitely
            (key, expected_count) = to_verify_queue.get_nowait()

            actual_count = session.execute(prepared, (key,))[0][0]
        except Empty:
            time.sleep(0.1)  # let's not eat CPU if the queue is empty
            continue
        except Exception:
            debug("Error in counter verifier process!")
            verification_done_queue.close()
            raise
        else:
            tester.assertEqual(expected_count, actual_count, "Data did not match expected value!")

            try:
                verification_done_queue.put_nowait((key, actual_count))
            except Full:
                # the rewritable queue is full, not a big deal. drop this one.
                # we keep the rewritable queue held to a modest max size
                # and allow dropping some rewritables because we don't want to
                # rewrite rows in the same sequence as originally written
                pass


class TestUpgradeThroughVersions(Tester):
    """
    Upgrades a 3-node Murmur3Partitioner cluster through versions specified in test_versions.
    """
    test_versions = None  # set on init to know which versions to use
    subprocs = None  # holds any subprocesses, for status checking and cleanup

    def __init__(self, *args, **kwargs):
        # Ignore these log patterns:
        self.ignore_log_patterns = [
            # This one occurs if we do a non-rolling upgrade, the node
            # it's trying to send the migration to hasn't started yet,
            # and when it does, it gets replayed and everything is fine.
            r'Can\'t send migration request: node.*is down',
        ]
        self.subprocs = []
        # Force cluster options that are common among versions:
        kwargs['cluster_options'] = {'partitioner': 'org.apache.cassandra.dht.Murmur3Partitioner'}
        Tester.__init__(self, *args, **kwargs)

    @property
    def test_versions(self):
        # Murmur was not present until 1.2+
        return [make_branch_str(v) for v in UPGRADE_PATH]

    def _init_local(self, git_ref):
        cdir = os.environ.get('CASSANDRA_DIR', DEFAULT_DIR)

        subprocess.check_call(
            ["git", "checkout", "{git_ref}".format(git_ref=git_ref)], cwd=cdir)

        subprocess.check_call(
            ["ant", "-Dbase.version={}".format(git_ref), "clean", "jar"], cwd=cdir)

    def setUp(self):
        # Forcing cluster version on purpose
        if LOCAL_MODE:
            self._init_local(self.test_versions[0])
        else:
            os.environ['CASSANDRA_VERSION'] = 'git:' + self.test_versions[0]

        debug("Versions to test (%s): %s" % (type(self), str([v for v in self.test_versions])))
        switch_jdks(os.environ['CASSANDRA_VERSION'][-3:])
        super(TestUpgradeThroughVersions, self).setUp()

    def parallel_upgrade_test(self):
        """
        Test upgrading cluster all at once (requires cluster downtime).
        """
        self.upgrade_scenario()

    def rolling_upgrade_test(self):
        """
        Test rolling upgrade of the cluster, so we have mixed versions part way through.
        """
        self.upgrade_scenario(rolling=True)

    def parallel_upgrade_with_internode_ssl_test(self):
        """
        Test upgrading cluster all at once (requires cluster downtime), with internode ssl.
        """
        self.upgrade_scenario(internode_ssl=True)

    def rolling_upgrade_with_internode_ssl_test(self):
        """
        Rolling upgrade test using internode ssl.
        """
        self.upgrade_scenario(rolling=True, internode_ssl=True)

    def upgrade_scenario(self, populate=True, create_schema=True, rolling=False, after_upgrade_call=(), internode_ssl=False):
        # Record the rows we write as we go:
        self.row_values = set()
        cluster = self.cluster
        if cluster.version() >= '3.0':
            cluster.set_configuration_options({'enable_user_defined_functions': 'true',
                                               'enable_scripted_user_defined_functions': 'true'})
        elif cluster.version() >= '2.2':
            cluster.set_configuration_options({'enable_user_defined_functions': 'true'})

        if internode_ssl:
            debug("***using internode ssl***")
            generate_ssl_stores(self.test_path)
            self.cluster.enable_internode_ssl(self.test_path)

        if populate:
            # Start with 3 node cluster
            debug('Creating cluster (%s)' % self.test_versions[0])
            cluster.populate(3)
            [node.start(use_jna=True, wait_for_binary_proto=True) for node in cluster.nodelist()]
        else:
            debug("Skipping cluster creation (should already be built)")

        # add nodes to self for convenience
        for i, node in enumerate(cluster.nodelist(), 1):
            node_name = 'node' + str(i)
            setattr(self, node_name, node)

        if create_schema:
            if rolling:
                self._create_schema_for_rolling()
            else:
                self._create_schema()
        else:
            debug("Skipping schema creation (should already be built)")
        time.sleep(5)  # sigh...

        self._log_current_ver(self.test_versions[0])
        self.created_metadata_versions = []

        if rolling:
            # start up processes to write and verify data
            write_proc, verify_proc, verification_queue = self._start_continuous_write_and_verify(wait_for_rowcount=5000)
            increment_proc, incr_verify_proc, incr_verify_queue = self._start_continuous_counter_increment_and_verify(wait_for_rowcount=5000)

            # upgrade through versions
            for tag in self.test_versions[1:]:
                for num, node in enumerate(self.cluster.nodelist()):
                    # sleep (sigh) because driver needs extra time to keep up with topo and make quorum possible
                    # this is ok, because a real world upgrade would proceed much slower than this programmatic one
                    # additionally this should provide more time for timeouts and other issues to crop up as well, which we could
                    # possibly "speed past" in an overly fast upgrade test
                    time.sleep(60)

                    self.upgrade_to_version(tag, partial=True, nodes=(node,))

                    self._check_on_subprocs(self.subprocs)
                    debug('Successfully upgraded %d of %d nodes to %s' %
                          (num + 1, len(self.cluster.nodelist()), tag))

                self.cluster.set_install_dir(version='git:' + tag)

            # Stop write processes
            write_proc.terminate()
            increment_proc.terminate()
            # wait for the verification queue's to empty (and check all rows) before continuing
            self._wait_until_queue_condition('writes pending verification', verification_queue, operator.le, 0, max_wait_s=600)
            self._wait_until_queue_condition('counters pending verification', incr_verify_queue, operator.le, 0, max_wait_s=600)
            self._check_on_subprocs([verify_proc, incr_verify_proc])  # make sure the verification processes are running still

            self._terminate_subprocs()
        # not a rolling upgrade, do everything in parallel:
        else:
            # upgrade through versions
            for tag in self.test_versions[1:]:
                self._write_values()
                self._increment_counters()

                for completed_version in self.created_metadata_versions:
                    self._check_metadata_schemas(completed_version[0], completed_version[1])
                self._create_metadata_schemas(self.test_versions[self.test_versions.index(tag) - 1])

                self.upgrade_to_version(tag)
                self.cluster.set_install_dir(version='git:' + tag)

                self._check_values()
                self._check_counters()
                self._check_select_count()
                for completed_version in self.created_metadata_versions:
                    self._check_metadata_schemas(completed_version[0], completed_version[1])

            # run custom post-upgrade callables
        for call in after_upgrade_call:
            call()

            debug('All nodes successfully upgraded to %s' % tag)
            self._log_current_ver(tag)

        cluster.stop()

    def tearDown(self):
        # just to be super sure we get cleaned up
        self._terminate_subprocs()

        super(TestUpgradeThroughVersions, self).tearDown()

    def _check_on_subprocs(self, subprocs):
        """
        Check on given subprocesses.

        If any are not alive, we'll go ahead and terminate any remaining alive subprocesses since this test is going to fail.
        """
        subproc_statuses = [s.is_alive() for s in subprocs]
        if not all(subproc_statuses):
            message = "A subprocess has terminated early. Subprocess statuses: "
            for s in subprocs:
                message += "{name} (is_alive: {aliveness}), ".format(name=s.name, aliveness=s.is_alive())
            message += "attempting to terminate remaining subprocesses now."
            self._terminate_subprocs()
            raise RuntimeError(message)

    def _terminate_subprocs(self):
        for s in self.subprocs:
            if s.is_alive():
                try:
                    psutil.Process(s.pid).kill()  # with fire damnit
                except Exception:
                    debug("Error terminating subprocess. There could be a lingering process.")
                    pass

    def upgrade_to_version(self, tag, partial=False, nodes=None):
        """
        Upgrade Nodes - if *partial* is True, only upgrade those nodes
        that are specified by *nodes*, otherwise ignore *nodes* specified
        and upgrade all nodes.
        """
        debug('Upgrading {nodes} to {tag}'.format(nodes=[n.name for n in nodes] if nodes is not None else 'all nodes', tag=tag))
        switch_jdks(tag)
        debug(os.environ['JAVA_HOME'])
        if not partial:
            nodes = self.cluster.nodelist()

        for node in nodes:
            debug('Shutting down node: ' + node.name)
            node.drain()
            node.watch_log_for("DRAINED")
            node.stop(wait_other_notice=False)

        # Update source or get a new version
        if LOCAL_MODE:
            self._init_local(tag)
            cdir = os.environ.get('CASSANDRA_DIR', DEFAULT_DIR)

            # Although we're not changing dirs, the source has changed, so ccm probably needs to know
            for node in nodes:
                node.set_install_dir(install_dir=cdir)
                debug("Set new cassandra dir for %s: %s" % (node.name, node.get_install_dir()))
        else:
            for node in nodes:
                node.set_install_dir(version='git:' + tag)
                debug("Set new cassandra dir for %s: %s" % (node.name, node.get_install_dir()))

        # hacky? yes. We could probably extend ccm to allow this publicly.
        # the topology file needs to be written before any nodes are started
        # otherwise they won't be grouped into dc's properly for multi-dc tests
        self.cluster._Cluster__update_topology_files()

        # Restart nodes on new version
        for node in nodes:
            debug('Starting %s on new version (%s)' % (node.name, tag))
            # Setup log4j / logback again (necessary moving from 2.0 -> 2.1):
            node.set_log_level("INFO")
            node.start(wait_other_notice=True, wait_for_binary_proto=True)
            node.nodetool('upgradesstables -a')

    def _log_current_ver(self, current_tag):
        """
        Logs where we currently are in the upgrade path, surrounding the current branch/tag, like ***sometag***
        """
        vers = self.test_versions
        curr_index = vers.index(current_tag)
        debug(
            "Current upgrade path: {}".format(
                vers[:curr_index] + ['***' + current_tag + '***'] + vers[curr_index + 1:]))

    def _create_metadata_schemas(self, tag):
        self.created_metadata_versions.append((self.cluster.version(), tag))
        session = self.patient_cql_connection(self.node2)
        session.execute('use upgrade')
        debug("schema metadata establish tables tag: {0}".format(tag))

        for m in filter(lambda mtd: mtd.startswith('establish_'), dir(schema_metadata_test)):
            debug("schema establish calling: [{0}]".format(m))
            getattr(schema_metadata_test, m)(self.cluster.version(), session, tag)

    def _check_metadata_schemas(self, version, tag):
        session = self.patient_cql_connection(self.node2)
        session.execute('use upgrade')
        debug("schema metadata verify version: {0}, tag: {1}".format(version, tag))

        for m in filter(lambda mtd: mtd.startswith('verify_'), dir(schema_metadata_test)):
            debug("schema verify calling: [{0}]".format(m))
            getattr(schema_metadata_test, m)(version, self.cluster.version(), 'upgrade', session, tag)

    def _create_schema_for_rolling(self):
        """
        Slightly different schema variant for testing rolling upgrades with quorum reads/writes.
        """
        session = self.patient_cql_connection(self.node2, protocol_version=PROTOCOL_VERSION)

        session.execute("CREATE KEYSPACE upgrade WITH replication = {'class':'SimpleStrategy', 'replication_factor':3};")

        session.execute('use upgrade')
        session.execute('CREATE TABLE cf ( k uuid PRIMARY KEY, v uuid )')
        session.execute('CREATE INDEX vals ON cf (v)')

        session.execute("""
            CREATE TABLE countertable (
                k1 uuid,
                c counter,
                PRIMARY KEY (k1)
                );""")

    def _create_schema(self):
        session = self.patient_cql_connection(self.node2, protocol_version=PROTOCOL_VERSION)

        session.execute("CREATE KEYSPACE upgrade WITH replication = {'class':'SimpleStrategy', 'replication_factor':2};")

        session.execute('use upgrade')
        session.execute('CREATE TABLE cf ( k int PRIMARY KEY, v text )')
        session.execute('CREATE INDEX vals ON cf (v)')

        session.execute("""
            CREATE TABLE countertable (
                k1 text,
                k2 int,
                c counter,
                PRIMARY KEY (k1, k2)
                );""")

    def _write_values(self, num=100):
        session = self.patient_cql_connection(self.node2, protocol_version=PROTOCOL_VERSION)
        session.execute("use upgrade")
        for i in xrange(num):
            x = len(self.row_values) + 1
            session.execute("UPDATE cf SET v='%d' WHERE k=%d" % (x, x))
            self.row_values.add(x)

    def _check_values(self, consistency_level=ConsistencyLevel.ALL):
        for node in self.cluster.nodelist():
            session = self.patient_cql_connection(node, protocol_version=PROTOCOL_VERSION)
            session.execute("use upgrade")
            for x in self.row_values:
                query = SimpleStatement("SELECT k,v FROM cf WHERE k=%d" % x, consistency_level=consistency_level)
                result = session.execute(query)
                k, v = result[0]
                self.assertEqual(x, k)
                self.assertEqual(str(x), v)

    def _wait_until_queue_condition(self, label, queue, opfunc, required_len, max_wait_s=300):
        """
        Waits up to max_wait_s for queue size to return True when evaluated against a condition function from the operator module.

        Label is just a string identifier for easier debugging.

        On Mac OS X may not be able to check queue size, in which case it will not block.

        If time runs out, raises RuntimeError.
        """
        wait_end_time = time.time() + max_wait_s

        while time.time() < wait_end_time:
            try:
                qsize = queue.qsize()
            except NotImplementedError:
                debug("Queue size may not be checkable on Mac OS X. Test will continue without waiting.")
                break
            if opfunc(qsize, required_len):
                debug("{} queue size ({}) is '{}' to {}. Continuing.".format(label, qsize, opfunc.__name__, required_len))
                break

            if divmod(round(time.time()), 30)[1] == 0:
                debug("{} queue size is at {}, target is to reach '{}' {}".format(label, qsize, opfunc.__name__, required_len))

            time.sleep(0.1)
            continue
        else:
            raise RuntimeError("Ran out of time waiting for queue size ({}) to be '{}' to {}. Aborting.".format(qsize, opfunc.__name__, required_len))

    def _start_continuous_write_and_verify(self, wait_for_rowcount=0, max_wait_s=300):
        """
        Starts a writer process, a verifier process, a queue to track writes,
        and a queue to track successful verifications (which are rewrite candidates).

        wait_for_rowcount provides a number of rows to write before unblocking and continuing.

        Returns the writer process, verifier process, and the to_verify_queue.
        """
        # queue of writes to be verified
        to_verify_queue = Queue()
        # queue of verified writes, which are update candidates
        verification_done_queue = Queue(maxsize=500)

        writer = Process(target=data_writer, args=(self, to_verify_queue, verification_done_queue, 25))
        # daemon subprocesses are killed automagically when the parent process exits
        writer.daemon = True
        self.subprocs.append(writer)
        writer.start()

        if wait_for_rowcount > 0:
            self._wait_until_queue_condition('rows written (but not verified)', to_verify_queue, operator.ge, wait_for_rowcount, max_wait_s=max_wait_s)

        verifier = Process(target=data_checker, args=(self, to_verify_queue, verification_done_queue))
        # daemon subprocesses are killed automagically when the parent process exits
        verifier.daemon = True
        self.subprocs.append(verifier)
        verifier.start()

        return writer, verifier, to_verify_queue

    def _start_continuous_counter_increment_and_verify(self, wait_for_rowcount=0, max_wait_s=300):
        """
        Starts a counter incrementer process, a verifier process, a queue to track writes,
        and a queue to track successful verifications (which are re-increment candidates).

        Returns the writer process, verifier process, and the to_verify_queue.
        """
        # queue of writes to be verified
        to_verify_queue = Queue()
        # queue of verified writes, which are update candidates
        verification_done_queue = Queue(maxsize=500)

        incrementer = Process(target=data_writer, args=(self, to_verify_queue, verification_done_queue, 25))
        # daemon subprocesses are killed automagically when the parent process exits
        incrementer.daemon = True
        self.subprocs.append(incrementer)
        incrementer.start()

        if wait_for_rowcount > 0:
            self._wait_until_queue_condition('counters incremented (but not verified)', to_verify_queue, operator.ge, wait_for_rowcount, max_wait_s=max_wait_s)

        count_verifier = Process(target=data_checker, args=(self, to_verify_queue, verification_done_queue))
        # daemon subprocesses are killed automagically when the parent process exits
        count_verifier.daemon = True
        self.subprocs.append(count_verifier)
        count_verifier.start()

        return incrementer, count_verifier, to_verify_queue

    def _increment_counters(self, opcount=25000):
        debug("performing {opcount} counter increments".format(opcount=opcount))
        session = self.patient_cql_connection(self.node2, protocol_version=PROTOCOL_VERSION)
        session.execute("use upgrade;")

        update_counter_query = ("UPDATE countertable SET c = c + 1 WHERE k1='{key1}' and k2={key2}")

        self.expected_counts = {}
        for i in range(10):
            self.expected_counts[uuid.uuid4()] = defaultdict(int)

        fail_count = 0

        for i in range(opcount):
            key1 = random.choice(self.expected_counts.keys())
            key2 = random.randint(1, 10)
            try:
                query = SimpleStatement(update_counter_query.format(key1=key1, key2=key2), consistency_level=ConsistencyLevel.ALL)
                session.execute(query)
            except WriteTimeout:
                fail_count += 1
            else:
                self.expected_counts[key1][key2] += 1
            if fail_count > 100:
                break

        assert fail_count < 100, "Too many counter increment failures"

    def _check_counters(self):
        debug("Checking counter values...")
        session = self.patient_cql_connection(self.node2, protocol_version=PROTOCOL_VERSION)
        session.execute("use upgrade;")

        for key1 in self.expected_counts.keys():
            for key2 in self.expected_counts[key1].keys():
                expected_value = self.expected_counts[key1][key2]

                query = SimpleStatement("SELECT c from countertable where k1='{key1}' and k2={key2};".format(key1=key1, key2=key2),
                                        consistency_level=ConsistencyLevel.ONE)
                results = session.execute(query)

                if results is not None:
                    actual_value = results[0][0]
                else:
                    # counter wasn't found
                    actual_value = None

                assert actual_value == expected_value, "Counter not at expected value. Got %s, expected %s" % (actual_value, expected_value)

    def _check_select_count(self, consistency_level=ConsistencyLevel.ALL):
        debug("Checking SELECT COUNT(*)")
        session = self.patient_cql_connection(self.node2, protocol_version=PROTOCOL_VERSION)
        session.execute("use upgrade;")

        expected_num_rows = len(self.row_values)

        countquery = SimpleStatement("SELECT COUNT(*) FROM cf;", consistency_level=consistency_level)
        result = session.execute(countquery)

        if result is not None:
            actual_num_rows = result[0][0]
            self.assertEqual(actual_num_rows, expected_num_rows, "SELECT COUNT(*) returned %s when expecting %s" % (actual_num_rows, expected_num_rows))
        else:
            self.fail("Count query did not return")


class TestRandomPartitionerUpgrade(TestUpgradeThroughVersions):
    """
    Upgrades a 3-node RandomPartitioner cluster through versions specified in test_versions.
    """

    def __init__(self, *args, **kwargs):
        # Ignore these log patterns:
        self.ignore_log_patterns = [
            # This one occurs if we do a non-rolling upgrade, the node
            # it's trying to send the migration to hasn't started yet,
            # and when it does, it gets replayed and everything is fine.
            r'Can\'t send migration request: node.*is down',
            r'RejectedExecutionException.*ThreadPoolExecutor has shut down',
        ]
        self.subprocs = []
        # Force cluster options that are common among versions:
        kwargs['cluster_options'] = {'partitioner': 'org.apache.cassandra.dht.RandomPartitioner'}
        Tester.__init__(self, *args, **kwargs)

    @property
    def test_versions(self):
        return [make_branch_str(v) for v in UPGRADE_PATH]


class PointToPointUpgradeBase(TestUpgradeThroughVersions):
    """
    Base class for testing a single upgrade (ver1->ver2).

    We are dynamically creating subclasses of this for testing point upgrades, so this is a convenient
    place to add functionality/tests for those subclasses to run.

    __test__ is False for this class. Subclasses need to revert to True to run tests!
    """
    __test__ = False

    def setUp(self):
        if LOCAL_MODE:
            self._init_local(self.test_versions[0])
        else:
            # Forcing cluster version on purpose
            os.environ['CASSANDRA_VERSION'] = 'git:' + self.test_versions[0]

        debug("Versions to test (%s): %s" % (type(self), str([v for v in self.test_versions])))
        switch_jdks(os.environ['CASSANDRA_VERSION'])
        super(TestUpgradeThroughVersions, self).setUp()

    def _bootstrap_new_node(self):
        # Check we can bootstrap a new node on the upgraded cluster:
        debug("Adding a node to the cluster")
        nnode = new_node(self.cluster, remote_debug_port=str(2000 + len(self.cluster.nodes)))
        nnode.start(use_jna=True, wait_other_notice=True, wait_for_binary_proto=True)
        self._write_values()
        self._increment_counters()
        self._check_values()
        self._check_counters()

    def _bootstrap_new_node_multidc(self):
        # Check we can bootstrap a new node on the upgraded cluster:
        debug("Adding a node to the cluster")
        nnode = new_node(self.cluster, remote_debug_port=str(2000 + len(self.cluster.nodes)), data_center='dc2')

        nnode.start(use_jna=True, wait_other_notice=True, wait_for_binary_proto=True)
        self._write_values()
        self._increment_counters()
        self._check_values()
        self._check_counters()

    def bootstrap_test(self):
        # try and add a new node
        self.upgrade_scenario(after_upgrade_call=(self._bootstrap_new_node,))

    def bootstrap_multidc_test(self):
        # try and add a new node
        # multi dc, 2 nodes in each dc
        cluster = self.cluster

        if cluster.version() >= '3.0':
            cluster.set_configuration_options({'enable_user_defined_functions': 'true',
                                               'enable_scripted_user_defined_functions': 'true'})
        elif cluster.version() >= '2.2':
            cluster.set_configuration_options({'enable_user_defined_functions': 'true'})

        cluster.populate([2, 2])
        [node.start(use_jna=True, wait_for_binary_proto=True) for node in self.cluster.nodelist()]
        self._multidc_schema_create()
        self.upgrade_scenario(populate=False, create_schema=False, after_upgrade_call=(self._bootstrap_new_node_multidc,))

    def _multidc_schema_create(self):
        session = self.patient_cql_connection(self.cluster.nodelist()[0], protocol_version=PROTOCOL_VERSION)

        if self.cluster.version() >= '1.2':
            # DDL for C* 1.2+
            session.execute("CREATE KEYSPACE upgrade WITH replication = {'class':'NetworkTopologyStrategy', 'dc1':1, 'dc2':2};")
        else:
            # DDL for C* 1.1
            session.execute("""CREATE KEYSPACE upgrade WITH strategy_class = 'NetworkTopologyStrategy'
            AND strategy_options:'dc1':1
            AND strategy_options:'dc2':2;
            """)

        session.execute('use upgrade')
        session.execute('CREATE TABLE cf ( k int PRIMARY KEY , v text )')
        session.execute('CREATE INDEX vals ON cf (v)')

        session.execute("""
            CREATE TABLE countertable (
                k1 text,
                k2 int,
                c counter,
                PRIMARY KEY (k1, k2)
                );""")

# create test classes for upgrading from latest tag on branch to the head of that same branch
for from_ver in UPGRADE_PATH:
    # we only want to do single upgrade tests for 1.2+
    # and trunk is the final version, so there's no test where trunk is upgraded to something else
    if make_ver_str(from_ver) >= '1.2' and from_ver != TRUNK_VER:
        cls_name = ('TestUpgrade_from_' + make_ver_str(from_ver) + '_latest_tag_to_' + make_ver_str(from_ver) + '_HEAD').replace('-', '_').replace('.', '_')
        start_ver_latest_tag = latest_tag_matching(from_ver)
        debug('Creating test upgrade class: {} with start tag of: {} ({})'.format(cls_name, start_ver_latest_tag, sha_for_ref_name(start_ver_latest_tag)))
        vars()[cls_name] = type(
            cls_name,
            (PointToPointUpgradeBase,),
            {'test_versions': [start_ver_latest_tag, make_branch_str(from_ver)], '__test__': True})

# build a list of tuples like so:
# [(A, B), (B, C) ... ]
# each pair in the list represents an upgrade test (A, B)
# where we will upgrade from the latest *tag* matching A, to the HEAD of branch B
POINT_UPGRADES = []
points = [v for v in UPGRADE_PATH if make_ver_str(v) >= '1.2']
for i, _ in enumerate(points):
    verslice = tuple(points[i:i + 2])
    if len(verslice) == 2:  # exclude dangling version at end
        POINT_UPGRADES.append(tuple(points[i:i + 2]))

# create test classes for upgrading from latest tag on one branch, to head of the next branch (see comment above)
for (from_ver, to_branch) in POINT_UPGRADES:
    cls_name = ('TestUpgrade_from_' + make_ver_str(from_ver) + '_latest_tag_to_' + make_branch_str(to_branch) + '_HEAD').replace('-', '_').replace('.', '_')
    from_ver_latest_tag = latest_tag_matching(from_ver)
    debug('Creating test upgrade class: {} with start tag of: {} ({})'.format(cls_name, from_ver_latest_tag, sha_for_ref_name(from_ver_latest_tag)))
    vars()[cls_name] = type(
        cls_name,
        (PointToPointUpgradeBase,),
        {'test_versions': [from_ver_latest_tag, make_branch_str(to_branch)], '__test__': True})

# create test classes for upgrading from HEAD of one branch to HEAD of next.
for (from_branch, to_branch) in POINT_UPGRADES:
    cls_name = ('TestUpgrade_from_' + make_branch_str(from_branch) + '_HEAD_to_' + make_branch_str(to_branch) + '_HEAD').replace('-', '_').replace('.', '_')
    debug('Creating test upgrade class: {}'.format(cls_name))
    vars()[cls_name] = type(
        cls_name,
        (PointToPointUpgradeBase,),
        {'test_versions': [make_branch_str(from_branch), make_branch_str(to_branch)], '__test__': True})

# create test classes for upgrading from HEAD of one branch, to latest tag of next branch
for (from_branch, to_branch) in POINT_UPGRADES:
    cls_name = ('TestUpgrade_from_' + make_branch_str(from_branch) + '_HEAD_to_' + make_branch_str(to_branch) + '_latest_tag').replace('-', '_').replace('.', '_')
    to_ver_latest_tag = latest_tag_matching(to_branch)
    # in some cases we might not find a tag (like when the to_branch is trunk)
    # so these will be skipped.
    if to_ver_latest_tag is None:
        continue
    debug('Creating test upgrade class: {} with end tag of: {} ({})'.format(cls_name, to_ver_latest_tag, sha_for_ref_name(to_ver_latest_tag)))

    vars()[cls_name] = type(
        cls_name,
        (PointToPointUpgradeBase,),
        {'test_versions': [make_branch_str(from_branch), to_ver_latest_tag], '__test__': True})
