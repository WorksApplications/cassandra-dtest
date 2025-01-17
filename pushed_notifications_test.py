import time
from nose.tools import timed
from cassandra import ReadTimeout, ReadFailure
from cassandra import ConsistencyLevel as CL
from cassandra.query import SimpleStatement
from dtest import Tester, debug
from tools import no_vnodes, require, since
from threading import Event
from assertions import assert_invalid


class NotificationWaiter(object):
    """
    A helper class for waiting for pushed notifications from
    Cassandra over the native protocol.
    """

    def __init__(self, tester, node, notification_types, keyspace=None):
        """
        `address` should be a ccmlib.node.Node instance
        `notification_types` should be a list of
        "TOPOLOGY_CHANGE", "STATUS_CHANGE", and "SCHEMA_CHANGE".
        """
        self.node = node
        self.address = node.network_interfaces['binary'][0]
        self.notification_types = notification_types
        self.keyspace = keyspace

        # get a single, new connection
        session = tester.patient_cql_connection(node)
        connection = session.cluster.connection_factory(self.address, is_control_connection=True)

        # coordinate with an Event
        self.event = Event()

        # the pushed notification
        self.notifications = []

        # register a callback for the notification type
        for notification_type in notification_types:
            connection.register_watcher(notification_type, self.handle_notification, register_timeout=5.0)

    def handle_notification(self, notification):
        """
        Called when a notification is pushed from Cassandra.
        """
        debug("Source {} sent {}".format(self.address, notification))

        if self.keyspace and notification['keyspace'] and self.keyspace != notification['keyspace']:
            return  # we are not interested in this schema change

        self.notifications.append(notification)
        self.event.set()

    def wait_for_notifications(self, timeout, num_notifications=1):
        """
        Waits up to `timeout` seconds for notifications from Cassandra. If
        passed `num_notifications`, stop waiting when that many notifications
        are observed.
        """

        deadline = time.time() + timeout
        while time.time() < deadline:
            self.event.wait(deadline - time.time())
            self.event.clear()
            if len(self.notifications) >= num_notifications:
                break

        return self.notifications

    def clear_notifications(self):
        self.notifications = []
        self.event.clear()


class TestPushedNotifications(Tester):
    """
    Tests for pushed native protocol notification from Cassandra.
    """

    @no_vnodes()
    def move_single_node_test(self):
        """
        @jira_ticket CASSANDRA-8516
        Moving a token should result in NODE_MOVED notifications.
        """
        self.cluster.populate(3).start(wait_for_binary_proto=True, wait_other_notice=True)

        # Despite waiting for each node to see the other nodes as UP, there is apparently
        # still a race condition that can result in NEW_NODE events being sent.  We don't
        # want to accidentally collect those, so for now we will just sleep a few seconds.
        time.sleep(3)

        waiters = [NotificationWaiter(self, node, ["TOPOLOGY_CHANGE"])
                   for node in self.cluster.nodes.values()]

        node1 = self.cluster.nodes.values()[0]
        node1.move("123")

        for waiter in waiters:
            debug("Waiting for notification from {}".format(waiter.address,))
            notifications = waiter.wait_for_notifications(60.0)
            self.assertEquals(1, len(notifications))
            notification = notifications[0]
            change_type = notification["change_type"]
            address, port = notification["address"]
            self.assertEquals("MOVED_NODE", change_type)
            self.assertEquals(self.get_ip_from_node(node1), address)

    @no_vnodes()
    @require("10052")
    def move_single_node_localhost_test(self):
        """
        @jira_ticket  CASSANDRA-10052
        Test that we don't get NODE_MOVED notifications from nodes other than the local one,
        when rpc_address is set to localhost.

        To set-up this test we override the rpc_address to "localhost" for all nodes, and
        therefore we must change the rpc port or else processes won't start.
        """
        cluster = self.cluster
        cluster.populate(3)
        node1, node2, node3 = cluster.nodelist()

        # change node3 'rpc_address' from '127.0.0.x' to 'localhost', increase port numbers
        i = 0
        for node in cluster.nodelist():
            node.network_interfaces['thrift'] = ('localhost', node.network_interfaces['thrift'][1] + i)
            node.network_interfaces['binary'] = ('localhost', node.network_interfaces['thrift'][1] + 1)
            node.import_config_files()  # this regenerates the yaml file and sets 'rpc_address' to the 'thrift' address
            debug(node.show())
            i = i + 2

        cluster.start(wait_for_binary_proto=True, wait_other_notice=True)

        # Despite waiting for each node to see the other nodes as UP, there is apparently
        # still a race condition that can result in NEW_NODE events being sent.  We don't
        # want to accidentally collect those, so for now we will just sleep a few seconds.
        time.sleep(3)

        waiters = [NotificationWaiter(self, node, ["TOPOLOGY_CHANGE"])
                   for node in self.cluster.nodes.values()]

        node1 = self.cluster.nodes.values()[0]
        node1.move("123")

        for waiter in waiters:
            debug("Waiting for notification from {}".format(waiter.address,))
            notifications = waiter.wait_for_notifications(30.0)
            self.assertEquals(1 if waiter.node is node1 else 0, len(notifications))

    def restart_node_test(self):
        """
        @jira_ticket CASSANDRA-7816
        Restarting a node should generate exactly one DOWN and one UP notification
        """

        self.cluster.populate(2).start()
        node1, node2 = self.cluster.nodelist()

        waiter = NotificationWaiter(self, node1, ["STATUS_CHANGE", "TOPOLOGY_CHANGE"])

        for i in range(5):
            debug("Restarting second node...")
            node2.stop(wait_other_notice=True)
            node2.start(wait_other_notice=True)
            debug("Waiting for notifications from {}".format(waiter.address,))
            notifications = waiter.wait_for_notifications(timeout=60.0, num_notifications=3)
            self.assertEquals(3, len(notifications))
            for notification in notifications:
                self.assertEquals(self.get_ip_from_node(node2), notification["address"][0])
            self.assertEquals("DOWN", notifications[0]["change_type"])
            self.assertEquals("UP", notifications[1]["change_type"])
            self.assertEquals("NEW_NODE", notifications[2]["change_type"])
            waiter.clear_notifications()

    def restart_node_localhost_test(self):
        """
        Test that we don't get client notifications when rpc_address is set to localhost.
        @jira_ticket  CASSANDRA-10052

        To set-up this test we override the rpc_address to "localhost" for all nodes, and
        therefore we must change the rpc port or else processes won't start.
        """
        cluster = self.cluster
        cluster.populate(2)
        node1, node2 = cluster.nodelist()

        i = 0  # change 'rpc_address' from '127.0.0.x' to 'localhost' and diversify port numbers
        for node in cluster.nodelist():
            node.network_interfaces['thrift'] = ('localhost', node.network_interfaces['thrift'][1] + i)
            node.network_interfaces['binary'] = ('localhost', node.network_interfaces['thrift'][1] + 1)
            node.import_config_files()  # this regenerates the yaml file and sets 'rpc_address' to the 'thrift' address
            debug(node.show())
            i = i + 2

        cluster.start(wait_for_binary_proto=True)

        # register for notification with node1
        waiter = NotificationWaiter(self, node1, ["STATUS_CHANGE", "TOPOLOGY_CHANGE"])

        # restart node 2
        debug("Restarting second node...")
        node2.stop(wait_other_notice=True)
        node2.start(wait_other_notice=True)

        # check that node1 did not send UP or DOWN notification for node2
        debug("Waiting for notifications from {}".format(waiter.address,))
        notifications = waiter.wait_for_notifications(timeout=30.0, num_notifications=3)
        self.assertEquals(0, len(notifications))

    @since("3.0")
    @require("9961")
    def schema_changes_test(self):
        """
        @jira_ticket CASSANDRA-10328
        Creating, updating and dropping a keyspace, a table and a materialized view
        will generate the correct schema change notifications.
        """

        self.cluster.populate(2).start(wait_for_binary_proto=True)
        node1, node2 = self.cluster.nodelist()

        session = self.patient_cql_connection(node1)
        waiter = NotificationWaiter(self, node2, ["SCHEMA_CHANGE"], keyspace='ks')

        self.create_ks(session, 'ks', 3)
        session.execute("create TABLE t (k int PRIMARY KEY , v int)")
        session.execute("alter TABLE t add v1 int;")

        session.execute("create MATERIALIZED VIEW mv as select * from t WHERE v IS NOT NULL AND t IS NOT NULL PRIMARY KEY (v, k)")
        session.execute(" alter materialized view mv with min_index_interval = 100")

        session.execute("drop MATERIALIZED VIEW mv")
        session.execute("drop TABLE t")
        session.execute("drop KEYSPACE ks")

        debug("Waiting for notifications from {}".format(waiter.address,))
        notifications = waiter.wait_for_notifications(timeout=60.0, num_notifications=14)
        self.assertEquals(14, len(notifications))
        self.assertDictContainsSubset({'change_type': u'CREATED', 'target_type': u'KEYSPACE'}, notifications[0])
        self.assertDictContainsSubset({'change_type': u'UPDATED', 'target_type': u'KEYSPACE'}, notifications[1])
        self.assertDictContainsSubset({'change_type': u'CREATED', 'target_type': u'TABLE', u'table': u't'}, notifications[2])
        self.assertDictContainsSubset({'change_type': u'UPDATED', 'target_type': u'KEYSPACE'}, notifications[3])
        self.assertDictContainsSubset({'change_type': u'UPDATED', 'target_type': u'TABLE', u'table': u't'}, notifications[4])
        self.assertDictContainsSubset({'change_type': u'UPDATED', 'target_type': u'KEYSPACE'}, notifications[5])
        self.assertDictContainsSubset({'change_type': u'CREATED', 'target_type': u'TABLE', u'table': u'mv'}, notifications[6])
        self.assertDictContainsSubset({'change_type': u'UPDATED', 'target_type': u'KEYSPACE'}, notifications[7])
        self.assertDictContainsSubset({'change_type': u'UPDATED', 'target_type': u'TABLE', u'table': u'mv'}, notifications[8])
        self.assertDictContainsSubset({'change_type': u'UPDATED', 'target_type': u'KEYSPACE'}, notifications[9])
        self.assertDictContainsSubset({'change_type': u'DROPPED', 'target_type': u'TABLE', u'table': u'mv'}, notifications[10])
        self.assertDictContainsSubset({'change_type': u'UPDATED', 'target_type': u'KEYSPACE'}, notifications[11])
        self.assertDictContainsSubset({'change_type': u'DROPPED', 'target_type': u'TABLE', u'table': u't'}, notifications[12])
        self.assertDictContainsSubset({'change_type': u'DROPPED', 'target_type': u'KEYSPACE'}, notifications[13])


class TestVariousNotifications(Tester):
    """
    Tests for various notifications/messages from Cassandra.
    """

    @since('2.2')
    def tombstone_failure_threshold_message_test(self):
        """
        Ensure nodes return an error message in case of TombstoneOverwhelmingExceptions rather
        than dropping the request. A drop makes the coordinator waits for the specified
        read_request_timeout_in_ms.
        @jira_ticket CASSANDRA-7886
        """

        self.allow_log_errors = True
        self.cluster.set_configuration_options(
            values={
                'tombstone_failure_threshold': 500,
                'read_request_timeout_in_ms': 30000,  # 30 seconds
                'range_request_timeout_in_ms': 40000
            }
        )
        self.cluster.populate(3).start()
        node1, node2, node3 = self.cluster.nodelist()
        session = self.patient_cql_connection(node1)

        self.create_ks(session, 'test', 3)
        session.execute(
            "CREATE TABLE test ( "
            "id int, mytext text, col1 int, col2 int, col3 int, "
            "PRIMARY KEY (id, mytext) )"
        )

        # Add data with tombstones
        values = map(lambda i: str(i), range(1000))
        for value in values:
            session.execute(SimpleStatement(
                "insert into test (id, mytext, col1) values (1, '{}', null) ".format(
                    value
                ),
                consistency_level=CL.ALL
            ))

        failure_msg = ("Scanned over.* tombstones.* query aborted")

        @timed(25)
        def read_failure_query():
            assert_invalid(
                session, SimpleStatement("select * from test where id in (1,2,3,4,5)", consistency_level=CL.ALL),
                expected=ReadTimeout if self.cluster.version() < '3' else ReadFailure,
            )

        read_failure_query()

        failure = (node1.grep_log(failure_msg) or
                   node2.grep_log(failure_msg) or
                   node3.grep_log(failure_msg))

        self.assertTrue(failure, ("Cannot find tombstone failure threshold error in log "
                                  "after failed query"))
        mark1 = node1.mark_log()
        mark2 = node2.mark_log()
        mark3 = node3.mark_log()

        @timed(35)
        def range_request_failure_query():
            assert_invalid(
                session, SimpleStatement("select * from test", consistency_level=CL.ALL),
                expected=ReadTimeout if self.cluster.version() < '3' else ReadFailure,
            )

        range_request_failure_query()

        failure = (node1.watch_log_for(failure_msg, from_mark=mark1, timeout=5) or
                   node2.watch_log_for(failure_msg, from_mark=mark2, timeout=5) or
                   node3.watch_log_for(failure_msg, from_mark=mark3, timeout=5))

        self.assertTrue(failure, ("Cannot find tombstone failure threshold error in log "
                                  "after range_request_timeout_query"))
