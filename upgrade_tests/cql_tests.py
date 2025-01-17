# coding: utf-8

import math
import random
import struct
import time
from collections import OrderedDict
from uuid import UUID, uuid4

from cassandra import ConsistencyLevel, InvalidRequest
from cassandra.concurrent import execute_concurrent_with_args
from cassandra.protocol import ProtocolException, SyntaxException
from cassandra.query import SimpleStatement
from cassandra.util import sortedset
from nose.exc import SkipTest

from assertions import assert_all, assert_invalid, assert_none, assert_one
from dtest import debug, freshCluster
from thrift_bindings.v22.ttypes import ConsistencyLevel as ThriftConsistencyLevel
from thrift_bindings.v22.ttypes import (CfDef, Column, ColumnOrSuperColumn, Mutation)
from thrift_tests import get_thrift_client
from tools import require, rows_to_list, since
from upgrade_base import UpgradeTester


class TestCQL(UpgradeTester):

    def static_cf_test(self):
        """ Test static CF syntax """
        cursor = self.prepare()

        # Create
        cursor.execute("""
            CREATE TABLE users (
                userid uuid PRIMARY KEY,
                firstname text,
                lastname text,
                age int
            );
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE users")

            # Inserts
            cursor.execute("INSERT INTO users (userid, firstname, lastname, age) VALUES (550e8400-e29b-41d4-a716-446655440000, 'Frodo', 'Baggins', 32)")
            cursor.execute("UPDATE users SET firstname = 'Samwise', lastname = 'Gamgee', age = 33 WHERE userid = f47ac10b-58cc-4372-a567-0e02b2c3d479")

            # Queries
            res = cursor.execute("SELECT firstname, lastname FROM users WHERE userid = 550e8400-e29b-41d4-a716-446655440000")
            assert rows_to_list(res) == [['Frodo', 'Baggins']], res

            res = cursor.execute("SELECT * FROM users WHERE userid = 550e8400-e29b-41d4-a716-446655440000")
            assert rows_to_list(res) == [[UUID('550e8400-e29b-41d4-a716-446655440000'), 32, 'Frodo', 'Baggins']], res

            res = cursor.execute("SELECT * FROM users")
            assert rows_to_list(res) == [
                [UUID('f47ac10b-58cc-4372-a567-0e02b2c3d479'), 33, 'Samwise', 'Gamgee'],
                [UUID('550e8400-e29b-41d4-a716-446655440000'), 32, 'Frodo', 'Baggins'],
            ], res

            # Test batch inserts
            cursor.execute("""
                BEGIN BATCH
                    INSERT INTO users (userid, age) VALUES (550e8400-e29b-41d4-a716-446655440000, 36)
                    UPDATE users SET age = 37 WHERE userid = f47ac10b-58cc-4372-a567-0e02b2c3d479
                    DELETE firstname, lastname FROM users WHERE userid = 550e8400-e29b-41d4-a716-446655440000
                    DELETE firstname, lastname FROM users WHERE userid = f47ac10b-58cc-4372-a567-0e02b2c3d479
                APPLY BATCH
            """)

            res = cursor.execute("SELECT * FROM users")
            assert rows_to_list(res) == [
                [UUID('f47ac10b-58cc-4372-a567-0e02b2c3d479'), 37, None, None],
                [UUID('550e8400-e29b-41d4-a716-446655440000'), 36, None, None],
            ], res

    def large_collection_errors(self):
        """ For large collections, make sure that we are printing warnings """

        # We only warn with protocol 2
        cursor = self.prepare(protocol_version=2)

        cluster = self.cluster
        node1 = cluster.nodelist()[0]
        self.ignore_log_patterns = ["Detected collection for table"]

        cursor.execute("""
            CREATE TABLE maps (
                userid text PRIMARY KEY,
                properties map<int, text>
            );
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE maps")

            # Insert more than the max, which is 65535
            for i in range(70000):
                cursor.execute("UPDATE maps SET properties[%i] = 'x' WHERE userid = 'user'" % i)

            # Query for the data and throw exception
            cursor.execute("SELECT properties FROM maps WHERE userid = 'user'")
            node1.watch_log_for("Detected collection for table ks.maps with 70000 elements, more than the 65535 limit. "
                                "Only the first 65535 elements will be returned to the client. "
                                "Please see http://cassandra.apache.org/doc/cql3/CQL.html#collections for more details.")

    def noncomposite_static_cf_test(self):
        """ Test non-composite static CF syntax """
        cursor = self.prepare()

        # Create
        cursor.execute("""
            CREATE TABLE users (
                userid uuid PRIMARY KEY,
                firstname ascii,
                lastname ascii,
                age int
            ) WITH COMPACT STORAGE;
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE users")

            # Inserts
            cursor.execute("INSERT INTO users (userid, firstname, lastname, age) VALUES (550e8400-e29b-41d4-a716-446655440000, 'Frodo', 'Baggins', 32)")
            cursor.execute("UPDATE users SET firstname = 'Samwise', lastname = 'Gamgee', age = 33 WHERE userid = f47ac10b-58cc-4372-a567-0e02b2c3d479")

            # Queries
            res = cursor.execute("SELECT firstname, lastname FROM users WHERE userid = 550e8400-e29b-41d4-a716-446655440000")
            self.assertEqual([['Frodo', 'Baggins']], rows_to_list(res))

            res = cursor.execute("SELECT * FROM users WHERE userid = 550e8400-e29b-41d4-a716-446655440000")
            self.assertEqual([[UUID('550e8400-e29b-41d4-a716-446655440000'), 32, 'Frodo', 'Baggins']], rows_to_list(res))

            # FIXME There appears to be some sort of problem with reusable cells
            # when executing this query.  It's likely that CASSANDRA-9705 will
            # fix this, but I'm not 100% sure.
            res = cursor.execute("SELECT * FROM users WHERE userid = f47ac10b-58cc-4372-a567-0e02b2c3d479")
            self.assertEqual([[UUID('f47ac10b-58cc-4372-a567-0e02b2c3d479'), 33, 'Samwise', 'Gamgee']], rows_to_list(res))

            res = cursor.execute("SELECT * FROM users")
            self.assertEqual([
                [UUID('f47ac10b-58cc-4372-a567-0e02b2c3d479'), 33, 'Samwise', 'Gamgee'],
                [UUID('550e8400-e29b-41d4-a716-446655440000'), 32, 'Frodo', 'Baggins'],
            ], rows_to_list(res))

            # Test batch inserts
            cursor.execute("""
                BEGIN BATCH
                    INSERT INTO users (userid, age) VALUES (550e8400-e29b-41d4-a716-446655440000, 36)
                    UPDATE users SET age = 37 WHERE userid = f47ac10b-58cc-4372-a567-0e02b2c3d479
                    DELETE firstname, lastname FROM users WHERE userid = 550e8400-e29b-41d4-a716-446655440000
                    DELETE firstname, lastname FROM users WHERE userid = f47ac10b-58cc-4372-a567-0e02b2c3d479
                APPLY BATCH
            """)

            res = cursor.execute("SELECT * FROM users")
            self.assertEqual([
                [UUID('f47ac10b-58cc-4372-a567-0e02b2c3d479'), 37, None, None],
                [UUID('550e8400-e29b-41d4-a716-446655440000'), 36, None, None],
            ], rows_to_list(res))

    def dynamic_cf_test(self):
        """ Test non-composite dynamic CF syntax """
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE clicks (
                userid uuid,
                url text,
                time bigint,
                PRIMARY KEY (userid, url)
            ) WITH COMPACT STORAGE;
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE clicks")

            # Inserts
            cursor.execute("INSERT INTO clicks (userid, url, time) VALUES (550e8400-e29b-41d4-a716-446655440000, 'http://foo.bar', 42)")
            cursor.execute("INSERT INTO clicks (userid, url, time) VALUES (550e8400-e29b-41d4-a716-446655440000, 'http://foo-2.bar', 24)")
            cursor.execute("INSERT INTO clicks (userid, url, time) VALUES (550e8400-e29b-41d4-a716-446655440000, 'http://bar.bar', 128)")
            cursor.execute("UPDATE clicks SET time = 24 WHERE userid = f47ac10b-58cc-4372-a567-0e02b2c3d479 and url = 'http://bar.foo'")
            cursor.execute("UPDATE clicks SET time = 12 WHERE userid IN (f47ac10b-58cc-4372-a567-0e02b2c3d479, 550e8400-e29b-41d4-a716-446655440000) and url = 'http://foo-3'")

            # Queries
            res = cursor.execute("SELECT url, time FROM clicks WHERE userid = 550e8400-e29b-41d4-a716-446655440000")
            assert rows_to_list(res) == [['http://bar.bar', 128], ['http://foo-2.bar', 24], ['http://foo-3', 12], ['http://foo.bar', 42]], res

            res = cursor.execute("SELECT * FROM clicks WHERE userid = f47ac10b-58cc-4372-a567-0e02b2c3d479")
            assert rows_to_list(res) == [
                [UUID('f47ac10b-58cc-4372-a567-0e02b2c3d479'), 'http://bar.foo', 24],
                [UUID('f47ac10b-58cc-4372-a567-0e02b2c3d479'), 'http://foo-3', 12]
            ], res

            res = cursor.execute("SELECT time FROM clicks")
            # Result from 'f47ac10b-58cc-4372-a567-0e02b2c3d479' are first
            assert rows_to_list(res) == [[24], [12], [128], [24], [12], [42]], res

            # Check we don't allow empty values for url since this is the full underlying cell name (#6152)
            assert_invalid(cursor, "INSERT INTO clicks (userid, url, time) VALUES (810e8500-e29b-41d4-a716-446655440000, '', 42)")

    def dense_cf_test(self):
        """ Test composite 'dense' CF syntax """
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE connections (
                userid uuid,
                ip text,
                port int,
                time bigint,
                PRIMARY KEY (userid, ip, port)
            ) WITH COMPACT STORAGE;
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE connections")

            # Inserts
            cursor.execute("INSERT INTO connections (userid, ip, port, time) VALUES (550e8400-e29b-41d4-a716-446655440000, '192.168.0.1', 80, 42)")
            cursor.execute("INSERT INTO connections (userid, ip, port, time) VALUES (550e8400-e29b-41d4-a716-446655440000, '192.168.0.2', 80, 24)")
            cursor.execute("INSERT INTO connections (userid, ip, port, time) VALUES (550e8400-e29b-41d4-a716-446655440000, '192.168.0.2', 90, 42)")
            cursor.execute("UPDATE connections SET time = 24 WHERE userid = f47ac10b-58cc-4372-a567-0e02b2c3d479 AND ip = '192.168.0.2' AND port = 80")

            # we don't have to include all of the clustering columns (see CASSANDRA-7990)
            cursor.execute("INSERT INTO connections (userid, ip, time) VALUES (f47ac10b-58cc-4372-a567-0e02b2c3d479, '192.168.0.3', 42)")
            cursor.execute("UPDATE connections SET time = 42 WHERE userid = f47ac10b-58cc-4372-a567-0e02b2c3d479 AND ip = '192.168.0.4'")

            # Queries
            res = cursor.execute("SELECT ip, port, time FROM connections WHERE userid = 550e8400-e29b-41d4-a716-446655440000")
            assert rows_to_list(res) == [['192.168.0.1', 80, 42], ['192.168.0.2', 80, 24], ['192.168.0.2', 90, 42]], res

            res = cursor.execute("SELECT ip, port, time FROM connections WHERE userid = 550e8400-e29b-41d4-a716-446655440000 and ip >= '192.168.0.2'")
            assert rows_to_list(res) == [['192.168.0.2', 80, 24], ['192.168.0.2', 90, 42]], res

            res = cursor.execute("SELECT ip, port, time FROM connections WHERE userid = 550e8400-e29b-41d4-a716-446655440000 and ip = '192.168.0.2'")
            assert rows_to_list(res) == [['192.168.0.2', 80, 24], ['192.168.0.2', 90, 42]], res

            res = cursor.execute("SELECT ip, port, time FROM connections WHERE userid = 550e8400-e29b-41d4-a716-446655440000 and ip > '192.168.0.2'")
            assert rows_to_list(res) == [], res

            res = cursor.execute("SELECT ip, port, time FROM connections WHERE userid = f47ac10b-58cc-4372-a567-0e02b2c3d479 AND ip = '192.168.0.3'")
            self.assertEqual([['192.168.0.3', None, 42]], rows_to_list(res))

            res = cursor.execute("SELECT ip, port, time FROM connections WHERE userid = f47ac10b-58cc-4372-a567-0e02b2c3d479 AND ip = '192.168.0.4'")
            self.assertEqual([['192.168.0.4', None, 42]], rows_to_list(res))

            # Deletion
            cursor.execute("DELETE time FROM connections WHERE userid = 550e8400-e29b-41d4-a716-446655440000 AND ip = '192.168.0.2' AND port = 80")
            res = list(cursor.execute("SELECT * FROM connections WHERE userid = 550e8400-e29b-41d4-a716-446655440000"))
            assert len(res) == 2, res

            cursor.execute("DELETE FROM connections WHERE userid = 550e8400-e29b-41d4-a716-446655440000")
            res = list(cursor.execute("SELECT * FROM connections WHERE userid = 550e8400-e29b-41d4-a716-446655440000"))
            assert len(res) == 0, res

            cursor.execute("DELETE FROM connections WHERE userid = f47ac10b-58cc-4372-a567-0e02b2c3d479 AND ip = '192.168.0.3'")
            res = list(cursor.execute("SELECT * FROM connections WHERE userid = f47ac10b-58cc-4372-a567-0e02b2c3d479 AND ip = '192.168.0.3'"))
            self.assertEqual([], res)

    def sparse_cf_test(self):
        """ Test composite 'sparse' CF syntax """
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE timeline (
                userid uuid,
                posted_month int,
                posted_day int,
                body ascii,
                posted_by ascii,
                PRIMARY KEY (userid, posted_month, posted_day)
            );
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE timeline")

            frodo_id = UUID('550e8400-e29b-41d4-a716-446655440000')
            sam_id = UUID('f47ac10b-58cc-4372-a567-0e02b2c3d479')

            # Inserts
            cursor.execute("INSERT INTO timeline (userid, posted_month, posted_day, body, posted_by) VALUES (%s, 1, 12, 'Something else', 'Frodo Baggins')", (frodo_id,))
            cursor.execute("INSERT INTO timeline (userid, posted_month, posted_day, body, posted_by) VALUES (%s, 1, 24, 'Something something', 'Frodo Baggins')", (frodo_id,))
            cursor.execute("UPDATE timeline SET body = 'Yo Froddo', posted_by = 'Samwise Gamgee' WHERE userid = %s AND posted_month = 1 AND posted_day = 3", (sam_id,))
            cursor.execute("UPDATE timeline SET body = 'Yet one more message' WHERE userid = %s AND posted_month = 1 and posted_day = 30", (frodo_id,))

            # Queries
            res = cursor.execute("SELECT body, posted_by FROM timeline WHERE userid = %s AND posted_month = 1 AND posted_day = 24", (frodo_id,))
            self.assertEqual([['Something something', 'Frodo Baggins']], rows_to_list(res))

            res = cursor.execute("SELECT posted_day, body, posted_by FROM timeline WHERE userid = %s AND posted_month = 1 AND posted_day > 12", (frodo_id,))
            self.assertEqual([
                [24, 'Something something', 'Frodo Baggins'],
                [30, 'Yet one more message', None]
            ], rows_to_list(res))

            res = cursor.execute("SELECT posted_day, body, posted_by FROM timeline WHERE userid = %s AND posted_month = 1", (frodo_id,))
            self.assertEqual([
                [12, 'Something else', 'Frodo Baggins'],
                [24, 'Something something', 'Frodo Baggins'],
                [30, 'Yet one more message', None]
            ], rows_to_list(res))

    @freshCluster()
    def limit_ranges_test(self):
        """ Validate LIMIT option for 'range queries' in SELECT statements """
        cursor = self.prepare(ordered=True)

        cursor.execute("""
            CREATE TABLE clicks (
                userid int,
                url text,
                time bigint,
                PRIMARY KEY (userid, url)
            ) WITH COMPACT STORAGE;
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE clicks")

            # Inserts
            for id in xrange(0, 100):
                for tld in ['com', 'org', 'net']:
                    cursor.execute("INSERT INTO clicks (userid, url, time) VALUES (%i, 'http://foo.%s', 42)" % (id, tld))

            # Queries
            res = cursor.execute("SELECT * FROM clicks WHERE token(userid) >= token(2) LIMIT 1")
            assert rows_to_list(res) == [[2, 'http://foo.com', 42]], res

            res = cursor.execute("SELECT * FROM clicks WHERE token(userid) > token(2) LIMIT 1")
            assert rows_to_list(res) == [[3, 'http://foo.com', 42]], res

    def limit_multiget_test(self):
        """ Validate LIMIT option for 'multiget' in SELECT statements """
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE clicks (
                userid int,
                url text,
                time bigint,
                PRIMARY KEY (userid, url)
            ) WITH COMPACT STORAGE;
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE clicks")

            # Inserts
            for id in xrange(0, 100):
                for tld in ['com', 'org', 'net']:
                    cursor.execute("INSERT INTO clicks (userid, url, time) VALUES (%i, 'http://foo.%s', 42)" % (id, tld))

            # Check that we do limit the output to 1 *and* that we respect query
            # order of keys (even though 48 is after 2)
            res = cursor.execute("SELECT * FROM clicks WHERE userid IN (48, 2) LIMIT 1")

            if self.get_node_version(is_upgraded) >= '2.2':
                # the coordinator is the upgraded 2.2+ node
                assert rows_to_list(res) == [[2, 'http://foo.com', 42]], res
            else:
                # the coordinator is the non-upgraded 2.1 node
                assert rows_to_list(res) == [[48, 'http://foo.com', 42]], res

    def simple_tuple_query_test(self):
        """Covers CASSANDRA-8613"""
        cursor = self.prepare()

        cursor.execute("create table bard (a int, b int, c int, d int , e int, PRIMARY KEY (a, b, c, d, e))")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE bard")

            cursor.execute("""INSERT INTO bard (a, b, c, d, e) VALUES (0, 2, 0, 0, 0);""")
            cursor.execute("""INSERT INTO bard (a, b, c, d, e) VALUES (0, 1, 0, 0, 0);""")
            cursor.execute("""INSERT INTO bard (a, b, c, d, e) VALUES (0, 0, 0, 0, 0);""")
            cursor.execute("""INSERT INTO bard (a, b, c, d, e) VALUES (0, 0, 1, 1, 1);""")
            cursor.execute("""INSERT INTO bard (a, b, c, d, e) VALUES (0, 0, 2, 2, 2);""")
            cursor.execute("""INSERT INTO bard (a, b, c, d, e) VALUES (0, 0, 3, 3, 3);""")
            cursor.execute("""INSERT INTO bard (a, b, c, d, e) VALUES (0, 0, 1, 1, 1);""")

            res = cursor.execute("SELECT * FROM bard WHERE b=0 AND (c, d, e) > (1, 1, 1) ALLOW FILTERING;")
            assert rows_to_list(res) == [[0, 0, 2, 2, 2], [0, 0, 3, 3, 3]]

    def limit_sparse_test(self):
        """ Validate LIMIT option for sparse table in SELECT statements """
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE clicks (
                userid int,
                url text,
                day int,
                month text,
                year int,
                PRIMARY KEY (userid, url)
            );
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE clicks")

            # Inserts
            for id in xrange(0, 100):
                for tld in ['com', 'org', 'net']:
                    cursor.execute("INSERT INTO clicks (userid, url, day, month, year) VALUES (%i, 'http://foo.%s', 1, 'jan', 2012)" % (id, tld))

            # Queries
            # Check we do get as many rows as requested
            res = list(cursor.execute("SELECT * FROM clicks LIMIT 4"))
            assert len(res) == 4, res

    def counters_test(self):
        """ Validate counter support """
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE clicks (
                userid int,
                url text,
                total counter,
                PRIMARY KEY (userid, url)
            ) WITH COMPACT STORAGE;
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE clicks")

            cursor.execute("UPDATE clicks SET total = total + 1 WHERE userid = 1 AND url = 'http://foo.com'")
            res = cursor.execute("SELECT total FROM clicks WHERE userid = 1 AND url = 'http://foo.com'")
            assert rows_to_list(res) == [[1]], res

            cursor.execute("UPDATE clicks SET total = total - 4 WHERE userid = 1 AND url = 'http://foo.com'")
            res = cursor.execute("SELECT total FROM clicks WHERE userid = 1 AND url = 'http://foo.com'")
            assert rows_to_list(res) == [[-3]], res

            cursor.execute("UPDATE clicks SET total = total+1 WHERE userid = 1 AND url = 'http://foo.com'")
            res = cursor.execute("SELECT total FROM clicks WHERE userid = 1 AND url = 'http://foo.com'")
            assert rows_to_list(res) == [[-2]], res

            cursor.execute("UPDATE clicks SET total = total -2 WHERE userid = 1 AND url = 'http://foo.com'")
            res = cursor.execute("SELECT total FROM clicks WHERE userid = 1 AND url = 'http://foo.com'")
            assert rows_to_list(res) == [[-4]], res

    def indexed_with_eq_test(self):
        """ Check that you can query for an indexed column even with a key EQ clause """
        cursor = self.prepare()

        # Create
        cursor.execute("""
            CREATE TABLE users (
                userid uuid PRIMARY KEY,
                firstname text,
                lastname text,
                age int
            );
        """)

        cursor.execute("CREATE INDEX byAge ON users(age)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE users")

            # Inserts
            cursor.execute("INSERT INTO users (userid, firstname, lastname, age) VALUES (550e8400-e29b-41d4-a716-446655440000, 'Frodo', 'Baggins', 32)")
            cursor.execute("UPDATE users SET firstname = 'Samwise', lastname = 'Gamgee', age = 33 WHERE userid = f47ac10b-58cc-4372-a567-0e02b2c3d479")

            # Queries
            res = cursor.execute("SELECT firstname FROM users WHERE userid = 550e8400-e29b-41d4-a716-446655440000 AND age = 33")
            assert rows_to_list(res) == [], res

            res = cursor.execute("SELECT firstname FROM users WHERE userid = f47ac10b-58cc-4372-a567-0e02b2c3d479 AND age = 33")
            assert rows_to_list(res) == [['Samwise']], res

    def select_key_in_test(self):
        """ Query for KEY IN (...) """
        cursor = self.prepare()

        # Create
        cursor.execute("""
            CREATE TABLE users (
                userid uuid PRIMARY KEY,
                firstname text,
                lastname text,
                age int
            );
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE users")

            # Inserts
            cursor.execute("""
                    INSERT INTO users (userid, firstname, lastname, age)
                    VALUES (550e8400-e29b-41d4-a716-446655440000, 'Frodo', 'Baggins', 32)
            """)
            cursor.execute("""
                    INSERT INTO users (userid, firstname, lastname, age)
                    VALUES (f47ac10b-58cc-4372-a567-0e02b2c3d479, 'Samwise', 'Gamgee', 33)
            """)

            # Select
            res = list(cursor.execute("""
                    SELECT firstname, lastname FROM users
                    WHERE userid IN (550e8400-e29b-41d4-a716-446655440000, f47ac10b-58cc-4372-a567-0e02b2c3d479)
            """))

            assert len(res) == 2, res

    def exclusive_slice_test(self):
        """ Test SELECT respects inclusive and exclusive bounds """
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int,
                c int,
                v int,
                PRIMARY KEY (k, c)
            ) WITH COMPACT STORAGE;
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            # Inserts
            for x in range(0, 10):
                cursor.execute("INSERT INTO test (k, c, v) VALUES (0, %s, %s)", (x, x))

            # Queries
            res = cursor.execute("SELECT v FROM test WHERE k = 0")
            self.assertEqual([[x] for x in range(10)], rows_to_list(res))

            res = cursor.execute("SELECT v FROM test WHERE k = 0 AND c >= 2 AND c <= 6")
            self.assertEqual([[x] for x in range(2, 7)], rows_to_list(res))

            res = cursor.execute("SELECT v FROM test WHERE k = 0 AND c > 2 AND c <= 6")
            self.assertEqual([[x] for x in range(3, 7)], rows_to_list(res))

            res = cursor.execute("SELECT v FROM test WHERE k = 0 AND c >= 2 AND c < 6")
            self.assertEqual([[x] for x in range(2, 6)], rows_to_list(res))

            res = cursor.execute("SELECT v FROM test WHERE k = 0 AND c > 2 AND c < 6")
            self.assertEqual([[x] for x in range(3, 6)], rows_to_list(res))

            # With LIMIT
            res = cursor.execute("SELECT v FROM test WHERE k = 0 AND c > 2 AND c <= 6 LIMIT 2")
            self.assertEqual([[3], [4]], rows_to_list(res))

            res = cursor.execute("SELECT v FROM test WHERE k = 0 AND c >= 2 AND c < 6 ORDER BY c DESC LIMIT 2")
            self.assertEqual([[5], [4]], rows_to_list(res))

    def in_clause_wide_rows_test(self):
        """ Check IN support for 'wide rows' in SELECT statement """
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test1 (
                k int,
                c int,
                v int,
                PRIMARY KEY (k, c)
            ) WITH COMPACT STORAGE;
        """)

        # composites
        cursor.execute("""
            CREATE TABLE test2 (
                k int,
                c1 int,
                c2 int,
                v int,
                PRIMARY KEY (k, c1, c2)
            ) WITH COMPACT STORAGE;
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test1")
            cursor.execute("TRUNCATE test2")

            # Inserts
            for x in range(0, 10):
                cursor.execute("INSERT INTO test1 (k, c, v) VALUES (0, %i, %i)" % (x, x))

            res = cursor.execute("SELECT v FROM test1 WHERE k = 0 AND c IN (5, 2, 8)")
            assert rows_to_list(res) == [[2], [5], [8]], res

            # Inserts
            for x in range(0, 10):
                cursor.execute("INSERT INTO test2 (k, c1, c2, v) VALUES (0, 0, %i, %i)" % (x, x))

            # Check first we don't allow IN everywhere
            if self.get_node_version(is_upgraded) >= '2.2':
                # the coordinator is the upgraded 2.2+ node
                assert_none(cursor, "SELECT v FROM test2 WHERE k = 0 AND c1 IN (5, 2, 8) AND c2 = 3")
            else:
                # the coordinator is the non-upgraded 2.1 node
                assert_invalid(cursor, "SELECT v FROM test2 WHERE k = 0 AND c1 IN (5, 2, 8) AND c2 = 3")

            res = cursor.execute("SELECT v FROM test2 WHERE k = 0 AND c1 = 0 AND c2 IN (5, 2, 8)")
            assert rows_to_list(res) == [[2], [5], [8]], res

    def order_by_test(self):
        """ Check ORDER BY support in SELECT statement """
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test1 (
                k int,
                c int,
                v int,
                PRIMARY KEY (k, c)
            ) WITH COMPACT STORAGE;
        """)

        # composites
        cursor.execute("""
            CREATE TABLE test2 (
                k int,
                c1 int,
                c2 int,
                v int,
                PRIMARY KEY (k, c1, c2)
            );
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test1")
            cursor.execute("TRUNCATE test2")

            # Inserts
            for x in range(0, 10):
                cursor.execute("INSERT INTO test1 (k, c, v) VALUES (0, %i, %i)" % (x, x))

            res = cursor.execute("SELECT v FROM test1 WHERE k = 0 ORDER BY c DESC")
            expected = [[x] for x in reversed(range(10))]
            self.assertEqual(expected, rows_to_list(res))

            # Inserts
            for x in range(0, 4):
                for y in range(0, 2):
                    cursor.execute("INSERT INTO test2 (k, c1, c2, v) VALUES (0, %i, %i, %i)" % (x, y, x * 2 + y))

            # Check first we don't always ORDER BY
            assert_invalid(cursor, "SELECT v FROM test2 WHERE k = 0 ORDER BY c DESC")
            assert_invalid(cursor, "SELECT v FROM test2 WHERE k = 0 ORDER BY c2 DESC")
            assert_invalid(cursor, "SELECT v FROM test2 WHERE k = 0 ORDER BY k DESC")

            res = cursor.execute("SELECT v FROM test2 WHERE k = 0 ORDER BY c1 DESC")
            expected = [[x] for x in reversed(range(8))]
            self.assertEqual(expected, rows_to_list(res))

            res = cursor.execute("SELECT v FROM test2 WHERE k = 0 ORDER BY c1")
            expected = [[x] for x in range(8)]
            self.assertEqual(expected, rows_to_list(res))

    def more_order_by_test(self):
        """ More ORDER BY checks (#4160) """
        cursor = self.prepare()

        cursor.execute("""
            CREATE COLUMNFAMILY Test (
                row text,
                number int,
                string text,
                PRIMARY KEY (row, number)
            ) WITH COMPACT STORAGE
        """)

        cursor.execute("""
            CREATE COLUMNFAMILY test2 (
                row text,
                number int,
                number2 int,
                string text,
                PRIMARY KEY (row, number, number2)
            ) WITH COMPACT STORAGE
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO Test (row, number, string) VALUES ('row', 1, 'one');")
            cursor.execute("INSERT INTO Test (row, number, string) VALUES ('row', 2, 'two');")
            cursor.execute("INSERT INTO Test (row, number, string) VALUES ('row', 3, 'three');")
            cursor.execute("INSERT INTO Test (row, number, string) VALUES ('row', 4, 'four');")

            res = cursor.execute("SELECT number FROM Test WHERE row='row' AND number < 3 ORDER BY number ASC;")
            assert rows_to_list(res) == [[1], [2]], res

            res = cursor.execute("SELECT number FROM Test WHERE row='row' AND number >= 3 ORDER BY number ASC;")
            assert rows_to_list(res) == [[3], [4]], res

            res = cursor.execute("SELECT number FROM Test WHERE row='row' AND number < 3 ORDER BY number DESC;")
            assert rows_to_list(res) == [[2], [1]], res

            res = cursor.execute("SELECT number FROM Test WHERE row='row' AND number >= 3 ORDER BY number DESC;")
            assert rows_to_list(res) == [[4], [3]], res

            res = cursor.execute("SELECT number FROM Test WHERE row='row' AND number > 3 ORDER BY number DESC;")
            assert rows_to_list(res) == [[4]], res

            res = cursor.execute("SELECT number FROM Test WHERE row='row' AND number <= 3 ORDER BY number DESC;")
            assert rows_to_list(res) == [[3], [2], [1]], res

            # composite clustering
            cursor.execute("INSERT INTO test2 (row, number, number2, string) VALUES ('a', 1, 0, 'a');")
            cursor.execute("INSERT INTO test2 (row, number, number2, string) VALUES ('a', 2, 0, 'a');")
            cursor.execute("INSERT INTO test2 (row, number, number2, string) VALUES ('a', 2, 1, 'a');")
            cursor.execute("INSERT INTO test2 (row, number, number2, string) VALUES ('a', 3, 0, 'a');")
            cursor.execute("INSERT INTO test2 (row, number, number2, string) VALUES ('a', 3, 1, 'a');")
            cursor.execute("INSERT INTO test2 (row, number, number2, string) VALUES ('a', 4, 0, 'a');")

            res = cursor.execute("SELECT number, number2 FROM test2 WHERE row='a' AND number < 3 ORDER BY number ASC;")
            assert rows_to_list(res) == [[1, 0], [2, 0], [2, 1]], res

            res = cursor.execute("SELECT number, number2 FROM test2 WHERE row='a' AND number >= 3 ORDER BY number ASC;")
            assert rows_to_list(res) == [[3, 0], [3, 1], [4, 0]], res

            res = cursor.execute("SELECT number, number2 FROM test2 WHERE row='a' AND number < 3 ORDER BY number DESC;")
            assert rows_to_list(res) == [[2, 1], [2, 0], [1, 0]], res

            res = cursor.execute("SELECT number, number2 FROM test2 WHERE row='a' AND number >= 3 ORDER BY number DESC;")
            assert rows_to_list(res) == [[4, 0], [3, 1], [3, 0]], res

            res = cursor.execute("SELECT number, number2 FROM test2 WHERE row='a' AND number > 3 ORDER BY number DESC;")
            assert rows_to_list(res) == [[4, 0]], res

            res = cursor.execute("SELECT number, number2 FROM test2 WHERE row='a' AND number <= 3 ORDER BY number DESC;")
            assert rows_to_list(res) == [[3, 1], [3, 0], [2, 1], [2, 0], [1, 0]], res

    def order_by_validation_test(self):
        """ Check we don't allow order by on row key (#4246) """
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k1 int,
                k2 int,
                v int,
                PRIMARY KEY (k1, k2)
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            q = "INSERT INTO test (k1, k2, v) VALUES (%d, %d, %d)"
            cursor.execute(q % (0, 0, 0))
            cursor.execute(q % (1, 1, 1))
            cursor.execute(q % (2, 2, 2))

            assert_invalid(cursor, "SELECT * FROM test ORDER BY k2")

    def order_by_with_in_test(self):
        """ Check that order-by works with IN (#4327) """
        cursor = self.prepare()
        cursor.execute("""
            CREATE TABLE test(
                my_id varchar,
                col1 int,
                value varchar,
                PRIMARY KEY (my_id, col1)
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")
            cursor.default_fetch_size = None

            cursor.execute("INSERT INTO test(my_id, col1, value) VALUES ( 'key1', 1, 'a')")
            cursor.execute("INSERT INTO test(my_id, col1, value) VALUES ( 'key2', 3, 'c')")
            cursor.execute("INSERT INTO test(my_id, col1, value) VALUES ( 'key3', 2, 'b')")
            cursor.execute("INSERT INTO test(my_id, col1, value) VALUES ( 'key4', 4, 'd')")

            query = SimpleStatement("SELECT col1 FROM test WHERE my_id in('key1', 'key2', 'key3') ORDER BY col1")
            res = cursor.execute(query)
            assert rows_to_list(res) == [[1], [2], [3]], res

            query = SimpleStatement("SELECT col1, my_id FROM test WHERE my_id in('key1', 'key2', 'key3') ORDER BY col1")
            res = cursor.execute(query)
            assert rows_to_list(res) == [[1, 'key1'], [2, 'key3'], [3, 'key2']], res

            query = SimpleStatement("SELECT my_id, col1 FROM test WHERE my_id in('key1', 'key2', 'key3') ORDER BY col1")
            res = cursor.execute(query)
            assert rows_to_list(res) == [['key1', 1], ['key3', 2], ['key2', 3]], res

    def reversed_comparator_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int,
                c int,
                v int,
                PRIMARY KEY (k, c)
            ) WITH CLUSTERING ORDER BY (c DESC);
        """)

        cursor.execute("""
            CREATE TABLE test2 (
                k int,
                c1 int,
                c2 int,
                v text,
                PRIMARY KEY (k, c1, c2)
            ) WITH CLUSTERING ORDER BY (c1 ASC, c2 DESC);
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")
            cursor.execute("TRUNCATE test2")

            # Inserts
            for x in range(0, 10):
                cursor.execute("INSERT INTO test (k, c, v) VALUES (0, %i, %i)" % (x, x))

            res = cursor.execute("SELECT c, v FROM test WHERE k = 0 ORDER BY c ASC")
            assert rows_to_list(res) == [[x, x] for x in range(0, 10)], res

            res = cursor.execute("SELECT c, v FROM test WHERE k = 0 ORDER BY c DESC")
            assert rows_to_list(res) == [[x, x] for x in range(9, -1, -1)], res

            # Inserts
            for x in range(0, 10):
                for y in range(0, 10):
                    cursor.execute("INSERT INTO test2 (k, c1, c2, v) VALUES (0, %i, %i, '%i%i')" % (x, y, x, y))

            assert_invalid(cursor, "SELECT c1, c2, v FROM test2 WHERE k = 0 ORDER BY c1 ASC, c2 ASC")
            assert_invalid(cursor, "SELECT c1, c2, v FROM test2 WHERE k = 0 ORDER BY c1 DESC, c2 DESC")

            res = cursor.execute("SELECT c1, c2, v FROM test2 WHERE k = 0 ORDER BY c1 ASC")
            assert rows_to_list(res) == [[x, y, '%i%i' % (x, y)] for x in range(0, 10) for y in range(9, -1, -1)], res

            res = cursor.execute("SELECT c1, c2, v FROM test2 WHERE k = 0 ORDER BY c1 ASC, c2 DESC")
            assert rows_to_list(res) == [[x, y, '%i%i' % (x, y)] for x in range(0, 10) for y in range(9, -1, -1)], res

            res = cursor.execute("SELECT c1, c2, v FROM test2 WHERE k = 0 ORDER BY c1 DESC, c2 ASC")
            assert rows_to_list(res) == [[x, y, '%i%i' % (x, y)] for x in range(9, -1, -1) for y in range(0, 10)], res

            assert_invalid(cursor, "SELECT c1, c2, v FROM test2 WHERE k = 0 ORDER BY c2 DESC, c1 ASC")

    def null_support_test(self):
        """ Test support for nulls """
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int,
                c int,
                v1 int,
                v2 set<text>,
                PRIMARY KEY (k, c)
            );
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            # Inserts
            cursor.execute("INSERT INTO test (k, c, v1, v2) VALUES (0, 0, null, {'1', '2'})")
            cursor.execute("INSERT INTO test (k, c, v1) VALUES (0, 1, 1)")

            res = cursor.execute("SELECT * FROM test")
            assert rows_to_list(res) == [[0, 0, None, set(['1', '2'])], [0, 1, 1, None]], res

            cursor.execute("INSERT INTO test (k, c, v1) VALUES (0, 1, null)")
            cursor.execute("INSERT INTO test (k, c, v2) VALUES (0, 0, null)")

            res = cursor.execute("SELECT * FROM test")
            assert rows_to_list(res) == [[0, 0, None, None], [0, 1, None, None]], res

            assert_invalid(cursor, "INSERT INTO test (k, c, v2) VALUES (0, 2, {1, null})")
            assert_invalid(cursor, "SELECT * FROM test WHERE k = null")
            assert_invalid(cursor, "INSERT INTO test (k, c, v2) VALUES (0, 0, { 'foo', 'bar', null })")

    def nameless_index_test(self):
        """ Test CREATE INDEX without name and validate the index can be dropped """
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE users (
                id text PRIMARY KEY,
                birth_year int,
            )
        """)

        cursor.execute("CREATE INDEX on users(birth_year)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE users")

            cursor.execute("INSERT INTO users (id, birth_year) VALUES ('Tom', 42)")
            cursor.execute("INSERT INTO users (id, birth_year) VALUES ('Paul', 24)")
            cursor.execute("INSERT INTO users (id, birth_year) VALUES ('Bob', 42)")

            res = cursor.execute("SELECT id FROM users WHERE birth_year = 42")
            assert rows_to_list(res) == [['Tom'], ['Bob']]

    def deletion_test(self):
        """ Test simple deletion and in particular check for #4193 bug """

        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE testcf (
                username varchar,
                id int,
                name varchar,
                stuff varchar,
                PRIMARY KEY(username, id)
            );
        """)

        # Compact case
        cursor.execute("""
            CREATE TABLE testcf2 (
                username varchar,
                id int,
                name varchar,
                stuff varchar,
                PRIMARY KEY(username, id, name)
            ) WITH COMPACT STORAGE;
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE testcf")
            cursor.execute("TRUNCATE testcf2")

            q = "INSERT INTO testcf (username, id, name, stuff) VALUES (%s, %s, %s, %s);"
            row1 = ('abc', 2, 'rst', 'some value')
            row2 = ('abc', 4, 'xyz', 'some other value')
            cursor.execute(q, row1)
            cursor.execute(q, row2)

            res = cursor.execute("SELECT * FROM testcf")
            self.assertEqual([list(row1), list(row2)], rows_to_list(res))

            cursor.execute("DELETE FROM testcf WHERE username='abc' AND id=2")

            res = cursor.execute("SELECT * FROM testcf")
            self.assertEqual([list(row2)], rows_to_list(res))

            q = "INSERT INTO testcf2 (username, id, name, stuff) VALUES (%s, %s, %s, %s);"
            row1 = ('abc', 2, 'rst', 'some value')
            row2 = ('abc', 4, 'xyz', 'some other value')
            cursor.execute(q, row1)
            cursor.execute(q, row2)

            res = cursor.execute("SELECT * FROM testcf2")
            self.assertEqual([list(row1), list(row2)], rows_to_list(res))

            cursor.execute("DELETE FROM testcf2 WHERE username='abc' AND id=2")

            res = cursor.execute("SELECT * FROM testcf")
            self.assertEqual([list(row2)], rows_to_list(res))

    def count_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE events (
                kind text,
                time int,
                value1 int,
                value2 int,
                PRIMARY KEY(kind, time)
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE events")

            full = "INSERT INTO events (kind, time, value1, value2) VALUES ('ev1', %d, %d, %d)"
            no_v2 = "INSERT INTO events (kind, time, value1) VALUES ('ev1', %d, %d)"

            cursor.execute(full % (0, 0, 0))
            cursor.execute(full % (1, 1, 1))
            cursor.execute(no_v2 % (2, 2))
            cursor.execute(full % (3, 3, 3))
            cursor.execute(no_v2 % (4, 4))
            cursor.execute("INSERT INTO events (kind, time, value1, value2) VALUES ('ev2', 0, 0, 0)")

            res = cursor.execute("SELECT COUNT(*) FROM events WHERE kind = 'ev1'")
            assert rows_to_list(res) == [[5]], res

            res = cursor.execute("SELECT COUNT(1) FROM events WHERE kind IN ('ev1', 'ev2') AND time=0")
            assert rows_to_list(res) == [[2]], res

    def batch_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE users (
                userid text PRIMARY KEY,
                name text,
                password text
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE users")

            query = SimpleStatement("""
                BEGIN BATCH
                    INSERT INTO users (userid, password, name) VALUES ('user2', 'ch@ngem3b', 'second user');
                    UPDATE users SET password = 'ps22dhds' WHERE userid = 'user3';
                    INSERT INTO users (userid, password) VALUES ('user4', 'ch@ngem3c');
                    DELETE name FROM users WHERE userid = 'user1';
                APPLY BATCH;
            """, consistency_level=ConsistencyLevel.QUORUM)
            cursor.execute(query)

    def token_range_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int PRIMARY KEY,
                c int,
                v int
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            c = 100
            for i in range(0, c):
                cursor.execute("INSERT INTO test (k, c, v) VALUES (%d, %d, %d)" % (i, i, i))

            rows = cursor.execute("SELECT k FROM test")
            inOrder = [x[0] for x in rows]
            assert len(inOrder) == c, 'Expecting %d elements, got %d' % (c, len(inOrder))

            min_token = -2 ** 63
            res = list(cursor.execute("SELECT k FROM test WHERE token(k) >= %d" % min_token))
            assert len(res) == c, "%s [all: %s]" % (str(res), str(inOrder))

            # assert_invalid(cursor, "SELECT k FROM test WHERE token(k) >= 0")
            # cursor.execute("SELECT k FROM test WHERE token(k) >= 0")

            res = cursor.execute("SELECT k FROM test WHERE token(k) >= token(%d) AND token(k) < token(%d)" % (inOrder[32], inOrder[65]))
            assert rows_to_list(res) == [[inOrder[x]] for x in range(32, 65)], "%s [all: %s]" % (str(res), str(inOrder))

    def timestamp_and_ttl_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int PRIMARY KEY,
                c text,
                d text
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (k, c) VALUES (1, 'test')")
            cursor.execute("INSERT INTO test (k, c) VALUES (2, 'test') USING TTL 400")

            res = list(cursor.execute("SELECT k, c, writetime(c), ttl(c) FROM test"))
            assert len(res) == 2, res
            for r in res:
                assert isinstance(r[2], (int, long))
                if r[0] == 1:
                    assert r[3] is None, res
                else:
                    assert isinstance(r[3], (int, long)), res

            # wrap writetime(), ttl() in other functions (test for CASSANDRA-8451)
            res = list(cursor.execute("SELECT k, c, blobAsBigint(bigintAsBlob(writetime(c))), ttl(c) FROM test"))
            assert len(res) == 2, res
            for r in res:
                assert isinstance(r[2], (int, long))
                if r[0] == 1:
                    assert r[3] is None, res
                else:
                    assert isinstance(r[3], (int, long)), res

            res = list(cursor.execute("SELECT k, c, writetime(c), blobAsInt(intAsBlob(ttl(c))) FROM test"))
            assert len(res) == 2, res
            for r in res:
                assert isinstance(r[2], (int, long))
                if r[0] == 1:
                    assert r[3] is None, res
                else:
                    assert isinstance(r[3], (int, long)), res

            assert_invalid(cursor, "SELECT k, c, writetime(k) FROM test")

            res = cursor.execute("SELECT k, d, writetime(d) FROM test WHERE k = 1")
            assert rows_to_list(res) == [[1, None, None]]

    def no_range_ghost_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int PRIMARY KEY,
                v int
            )
        """)

        # Example from #3505
        cursor.execute("CREATE KEYSPACE ks1 with replication = { 'class' : 'SimpleStrategy', 'replication_factor' : 1 };")
        cursor.execute("""
            CREATE COLUMNFAMILY ks1.users (
                KEY varchar PRIMARY KEY,
                password varchar,
                gender varchar,
                birth_year bigint)
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")
            cursor.execute("TRUNCATE ks1.users")

            for k in range(0, 5):
                cursor.execute("INSERT INTO test (k, v) VALUES (%d, 0)" % k)

            unsorted_res = cursor.execute("SELECT k FROM test")
            res = sorted(unsorted_res)
            assert rows_to_list(res) == [[k] for k in range(0, 5)], res

            cursor.execute("DELETE FROM test WHERE k=2")

            unsorted_res = cursor.execute("SELECT k FROM test")
            res = sorted(unsorted_res)
            assert rows_to_list(res) == [[k] for k in range(0, 5) if k is not 2], res

            # Example from #3505
            cursor.execute("USE ks1")

            cursor.execute("INSERT INTO users (KEY, password) VALUES ('user1', 'ch@ngem3a')")
            cursor.execute("UPDATE users SET gender = 'm', birth_year = 1980 WHERE KEY = 'user1'")
            res = cursor.execute("SELECT * FROM users WHERE KEY='user1'")
            assert rows_to_list(res) == [['user1', 1980, 'm', 'ch@ngem3a']], res

            cursor.execute("TRUNCATE users")

            res = cursor.execute("SELECT * FROM users")
            assert rows_to_list(res) == [], res

            res = cursor.execute("SELECT * FROM users WHERE KEY='user1'")
            assert rows_to_list(res) == [], res

    @freshCluster()
    def undefined_column_handling_test(self):
        cursor = self.prepare(ordered=True)

        cursor.execute("""
            CREATE TABLE test (
                k int PRIMARY KEY,
                v1 int,
                v2 int,
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (k, v1, v2) VALUES (0, 0, 0)")
            cursor.execute("INSERT INTO test (k, v1) VALUES (1, 1)")
            cursor.execute("INSERT INTO test (k, v1, v2) VALUES (2, 2, 2)")

            res = cursor.execute("SELECT v2 FROM test")
            assert rows_to_list(res) == [[0], [None], [2]], res

            res = cursor.execute("SELECT v2 FROM test WHERE k = 1")
            assert rows_to_list(res) == [[None]], res

    @freshCluster()
    def range_tombstones_test(self):
        """ Test deletion by 'composite prefix' (range tombstones) """

        # Uses 3 nodes just to make sure RowMutation are correctly serialized
        cursor = self.prepare(nodes=3)

        cursor.execute("""
            CREATE TABLE test1 (
                k int,
                c1 int,
                c2 int,
                v1 int,
                v2 int,
                PRIMARY KEY (k, c1, c2)
            );
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test1")

            rows = 5
            col1 = 2
            col2 = 2
            cpr = col1 * col2
            for i in xrange(0, rows):
                for j in xrange(0, col1):
                    for k in xrange(0, col2):
                        n = (i * cpr) + (j * col2) + k
                        cursor.execute("INSERT INTO test1 (k, c1, c2, v1, v2) VALUES (%d, %d, %d, %d, %d)" % (i, j, k, n, n))

            for i in xrange(0, rows):
                res = cursor.execute("SELECT v1, v2 FROM test1 where k = %d" % i)
                assert rows_to_list(res) == [[x, x] for x in xrange(i * cpr, (i + 1) * cpr)], res

            for i in xrange(0, rows):
                cursor.execute("DELETE FROM test1 WHERE k = %d AND c1 = 0" % i)

            for i in xrange(0, rows):
                res = cursor.execute("SELECT v1, v2 FROM test1 WHERE k = %d" % i)
                assert rows_to_list(res) == [[x, x] for x in xrange(i * cpr + col1, (i + 1) * cpr)], res

            self.cluster.flush()
            time.sleep(0.2)

            for i in xrange(0, rows):
                res = cursor.execute("SELECT v1, v2 FROM test1 WHERE k = %d" % i)
                assert rows_to_list(res) == [[x, x] for x in xrange(i * cpr + col1, (i + 1) * cpr)], res

    def range_tombstones_compaction_test(self):
        """ Test deletion by 'composite prefix' (range tombstones) with compaction """
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test1 (
                k int,
                c1 int,
                c2 int,
                v1 text,
                PRIMARY KEY (k, c1, c2)
            );
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test1")

            for c1 in range(0, 4):
                for c2 in range(0, 2):
                    cursor.execute("INSERT INTO test1 (k, c1, c2, v1) VALUES (0, %d, %d, '%s')" % (c1, c2, '%i%i' % (c1, c2)))

            self.cluster.flush()

            cursor.execute("DELETE FROM test1 WHERE k = 0 AND c1 = 1")

            self.cluster.flush()
            self.cluster.compact()

            res = cursor.execute("SELECT v1 FROM test1 WHERE k = 0")
            assert rows_to_list(res) == [['%i%i' % (c1, c2)] for c1 in xrange(0, 4) for c2 in xrange(0, 2) if c1 != 1], res

    def delete_row_test(self):
        """ Test deletion of rows """
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                 k int,
                 c1 int,
                 c2 int,
                 v1 int,
                 v2 int,
                 PRIMARY KEY (k, c1, c2)
            );
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            q = "INSERT INTO test (k, c1, c2, v1, v2) VALUES (%d, %d, %d, %d, %d)"
            cursor.execute(q % (0, 0, 0, 0, 0))
            cursor.execute(q % (0, 0, 1, 1, 1))
            cursor.execute(q % (0, 0, 2, 2, 2))
            cursor.execute(q % (0, 1, 0, 3, 3))

            cursor.execute("DELETE FROM test WHERE k = 0 AND c1 = 0 AND c2 = 0")
            res = list(cursor.execute("SELECT * FROM test"))
            assert len(res) == 3, res

    def range_query_2ndary_test(self):
        """ Test range queries with 2ndary indexes (#4257) """
        cursor = self.prepare()

        cursor.execute("CREATE TABLE indextest (id int primary key, row int, setid int);")
        cursor.execute("CREATE INDEX indextest_setid_idx ON indextest (setid)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE indextest")

            q = "INSERT INTO indextest (id, row, setid) VALUES (%d, %d, %d);"
            cursor.execute(q % (0, 0, 0))
            cursor.execute(q % (1, 1, 0))
            cursor.execute(q % (2, 2, 0))
            cursor.execute(q % (3, 3, 0))

            assert_invalid(cursor, "SELECT * FROM indextest WHERE setid = 0 AND row < 1;")
            res = cursor.execute("SELECT * FROM indextest WHERE setid = 0 AND row < 1 ALLOW FILTERING;")
            assert rows_to_list(res) == [[0, 0, 0]], res

    def set_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE user (
                fn text,
                ln text,
                tags set<text>,
                PRIMARY KEY (fn, ln)
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE user")

            q = "UPDATE user SET %s WHERE fn='Tom' AND ln='Bombadil'"
            cursor.execute(q % "tags = tags + { 'foo' }")
            cursor.execute(q % "tags = tags + { 'bar' }")
            cursor.execute(q % "tags = tags + { 'foo' }")
            cursor.execute(q % "tags = tags + { 'foobar' }")
            cursor.execute(q % "tags = tags - { 'bar' }")

            res = cursor.execute("SELECT tags FROM user")
            assert rows_to_list(res) == [[set(['foo', 'foobar'])]], res

            q = "UPDATE user SET %s WHERE fn='Bilbo' AND ln='Baggins'"
            cursor.execute(q % "tags = { 'a', 'c', 'b' }")
            res = cursor.execute("SELECT tags FROM user WHERE fn='Bilbo' AND ln='Baggins'")
            assert rows_to_list(res) == [[set(['a', 'b', 'c'])]], res

            time.sleep(.01)

            cursor.execute(q % "tags = { 'm', 'n' }")
            res = cursor.execute("SELECT tags FROM user WHERE fn='Bilbo' AND ln='Baggins'")
            assert rows_to_list(res) == [[set(['m', 'n'])]], res

            cursor.execute("DELETE tags['m'] FROM user WHERE fn='Bilbo' AND ln='Baggins'")
            res = cursor.execute("SELECT tags FROM user WHERE fn='Bilbo' AND ln='Baggins'")
            assert rows_to_list(res) == [[set(['n'])]], res

            cursor.execute("DELETE tags FROM user WHERE fn='Bilbo' AND ln='Baggins'")
            res = cursor.execute("SELECT tags FROM user WHERE fn='Bilbo' AND ln='Baggins'")
            assert rows_to_list(res) == [], res

    def map_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE user (
                fn text,
                ln text,
                m map<text, int>,
                PRIMARY KEY (fn, ln)
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE user")

            q = "UPDATE user SET %s WHERE fn='Tom' AND ln='Bombadil'"
            cursor.execute(q % "m['foo'] = 3")
            cursor.execute(q % "m['bar'] = 4")
            cursor.execute(q % "m['woot'] = 5")
            cursor.execute(q % "m['bar'] = 6")
            cursor.execute("DELETE m['foo'] FROM user WHERE fn='Tom' AND ln='Bombadil'")

            res = cursor.execute("SELECT m FROM user")
            assert rows_to_list(res) == [[{'woot': 5, 'bar': 6}]], res

            q = "UPDATE user SET %s WHERE fn='Bilbo' AND ln='Baggins'"
            cursor.execute(q % "m = { 'a' : 4 , 'c' : 3, 'b' : 2 }")
            res = cursor.execute("SELECT m FROM user WHERE fn='Bilbo' AND ln='Baggins'")
            assert rows_to_list(res) == [[{'a': 4, 'b': 2, 'c': 3}]], res

            time.sleep(.01)

            # Check we correctly overwrite
            cursor.execute(q % "m = { 'm' : 4 , 'n' : 1, 'o' : 2 }")
            res = cursor.execute("SELECT m FROM user WHERE fn='Bilbo' AND ln='Baggins'")
            assert rows_to_list(res) == [[{'m': 4, 'n': 1, 'o': 2}]], res

            cursor.execute(q % "m = {}")
            res = cursor.execute("SELECT m FROM user WHERE fn='Bilbo' AND ln='Baggins'")
            assert rows_to_list(res) == [], res

    def list_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE user (
                fn text,
                ln text,
                tags list<text>,
                PRIMARY KEY (fn, ln)
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE user")

            q = "UPDATE user SET %s WHERE fn='Tom' AND ln='Bombadil'"
            cursor.execute(q % "tags = tags + [ 'foo' ]")
            cursor.execute(q % "tags = tags + [ 'bar' ]")
            cursor.execute(q % "tags = tags + [ 'foo' ]")
            cursor.execute(q % "tags = tags + [ 'foobar' ]")

            res = cursor.execute("SELECT tags FROM user")
            self.assertItemsEqual(rows_to_list(res), [[['foo', 'bar', 'foo', 'foobar']]])

            q = "UPDATE user SET %s WHERE fn='Bilbo' AND ln='Baggins'"
            cursor.execute(q % "tags = [ 'a', 'c', 'b', 'c' ]")
            res = cursor.execute("SELECT tags FROM user WHERE fn='Bilbo' AND ln='Baggins'")
            self.assertItemsEqual(rows_to_list(res), [[['a', 'c', 'b', 'c']]])

            cursor.execute(q % "tags = [ 'm', 'n' ] + tags")
            res = cursor.execute("SELECT tags FROM user WHERE fn='Bilbo' AND ln='Baggins'")
            self.assertItemsEqual(rows_to_list(res), [[['m', 'n', 'a', 'c', 'b', 'c']]])

            cursor.execute(q % "tags[2] = 'foo', tags[4] = 'bar'")
            res = cursor.execute("SELECT tags FROM user WHERE fn='Bilbo' AND ln='Baggins'")
            self.assertItemsEqual(rows_to_list(res), [[['m', 'n', 'foo', 'c', 'bar', 'c']]])

            cursor.execute("DELETE tags[2] FROM user WHERE fn='Bilbo' AND ln='Baggins'")
            res = cursor.execute("SELECT tags FROM user WHERE fn='Bilbo' AND ln='Baggins'")
            self.assertItemsEqual(rows_to_list(res), [[['m', 'n', 'c', 'bar', 'c']]])

            cursor.execute(q % "tags = tags - [ 'bar' ]")
            res = cursor.execute("SELECT tags FROM user WHERE fn='Bilbo' AND ln='Baggins'")
            self.assertItemsEqual(rows_to_list(res), [[['m', 'n', 'c', 'c']]])

    def multi_collection_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE foo(
                k uuid PRIMARY KEY,
                L list<int>,
                M map<text, int>,
                S set<int>
            );
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE foo")

            cursor.execute("UPDATE ks.foo SET L = [1, 3, 5] WHERE k = b017f48f-ae67-11e1-9096-005056c00008;")
            cursor.execute("UPDATE ks.foo SET L = L + [7, 11, 13] WHERE k = b017f48f-ae67-11e1-9096-005056c00008;")
            cursor.execute("UPDATE ks.foo SET S = {1, 3, 5} WHERE k = b017f48f-ae67-11e1-9096-005056c00008;")
            cursor.execute("UPDATE ks.foo SET S = S + {7, 11, 13} WHERE k = b017f48f-ae67-11e1-9096-005056c00008;")
            cursor.execute("UPDATE ks.foo SET M = {'foo': 1, 'bar' : 3} WHERE k = b017f48f-ae67-11e1-9096-005056c00008;")
            cursor.execute("UPDATE ks.foo SET M = M + {'foobar' : 4} WHERE k = b017f48f-ae67-11e1-9096-005056c00008;")

            res = cursor.execute("SELECT L, M, S FROM foo WHERE k = b017f48f-ae67-11e1-9096-005056c00008")
            self.assertItemsEqual(rows_to_list(res), [[
                [1, 3, 5, 7, 11, 13],
                OrderedDict([('bar', 3), ('foo', 1), ('foobar', 4)]),
                sortedset([1, 3, 5, 7, 11, 13])
            ]])

    def range_query_test(self):
        """ Range test query from #4372 """
        cursor = self.prepare()

        cursor.execute("CREATE TABLE test (a int, b int, c int, d int, e int, f text, PRIMARY KEY (a, b, c, d, e) )")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (a, b, c, d, e, f) VALUES (1, 1, 1, 1, 2, '2');")
            cursor.execute("INSERT INTO test (a, b, c, d, e, f) VALUES (1, 1, 1, 1, 1, '1');")
            cursor.execute("INSERT INTO test (a, b, c, d, e, f) VALUES (1, 1, 1, 2, 1, '1');")
            cursor.execute("INSERT INTO test (a, b, c, d, e, f) VALUES (1, 1, 1, 1, 3, '3');")
            cursor.execute("INSERT INTO test (a, b, c, d, e, f) VALUES (1, 1, 1, 1, 5, '5');")

            res = cursor.execute("SELECT a, b, c, d, e, f FROM test WHERE a = 1 AND b = 1 AND c = 1 AND d = 1 AND e >= 2;")
            assert rows_to_list(res) == [[1, 1, 1, 1, 2, u'2'], [1, 1, 1, 1, 3, u'3'], [1, 1, 1, 1, 5, u'5']], res

    def composite_row_key_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k1 int,
                k2 int,
                c int,
                v int,
                PRIMARY KEY ((k1, k2), c)
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            req = "INSERT INTO test (k1, k2, c, v) VALUES (%d, %d, %d, %d)"
            for i in range(0, 4):
                cursor.execute(req % (0, i, i, i))

            res = cursor.execute("SELECT * FROM test")
            assert rows_to_list(res) == [[0, 2, 2, 2], [0, 3, 3, 3], [0, 0, 0, 0], [0, 1, 1, 1]], res

            res = cursor.execute("SELECT * FROM test WHERE k1 = 0 and k2 IN (1, 3)")
            assert rows_to_list(res) == [[0, 1, 1, 1], [0, 3, 3, 3]], res

            assert_invalid(cursor, "SELECT * FROM test WHERE k2 = 3")

            if self.get_node_version(is_upgraded) < '2.2':
                # the coordinator is the upgraded 2.2+ node
                assert_invalid(cursor, "SELECT * FROM test WHERE k1 IN (0, 1) and k2 = 3")

            res = cursor.execute("SELECT * FROM test WHERE token(k1, k2) = token(0, 1)")
            assert rows_to_list(res) == [[0, 1, 1, 1]], res

            res = cursor.execute("SELECT * FROM test WHERE token(k1, k2) > " + str(-((2 ** 63) - 1)))
            assert rows_to_list(res) == [[0, 2, 2, 2], [0, 3, 3, 3], [0, 0, 0, 0], [0, 1, 1, 1]], res

    def cql3_insert_thrift_test(self):
        """ Check that we can insert from thrift into a CQL3 table (#4377) """
        cursor = self.prepare(start_rpc=True)

        cursor.execute("""
            CREATE TABLE test (
                k int,
                c int,
                v int,
                PRIMARY KEY (k, c)
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            node = self.cluster.nodelist()[0]
            host, port = node.network_interfaces['thrift']
            client = get_thrift_client(host, port)
            client.transport.open()
            client.set_keyspace('ks')
            key = struct.pack('>i', 2)
            column_name_component = struct.pack('>i', 4)
            # component length + component + EOC + component length + component + EOC
            column_name = '\x00\x04' + column_name_component + '\x00' + '\x00\x01' + 'v' + '\x00'
            value = struct.pack('>i', 8)
            client.batch_mutate(
                {key: {'test': [Mutation(ColumnOrSuperColumn(column=Column(name=column_name, value=value, timestamp=100)))]}},
                ThriftConsistencyLevel.ONE)

            res = cursor.execute("SELECT * FROM test")
            assert rows_to_list(res) == [[2, 4, 8]], res

    def row_existence_test(self):
        """ Check the semantic of CQL row existence (part of #4361) """
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int,
                c int,
                v1 int,
                v2 int,
                PRIMARY KEY (k, c)
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (k, c, v1, v2) VALUES (1, 1, 1, 1)")

            res = cursor.execute("SELECT * FROM test")
            assert rows_to_list(res) == [[1, 1, 1, 1]], res

            assert_invalid(cursor, "DELETE c FROM test WHERE k = 1 AND c = 1")

            cursor.execute("DELETE v2 FROM test WHERE k = 1 AND c = 1")
            res = cursor.execute("SELECT * FROM test")
            assert rows_to_list(res) == [[1, 1, 1, None]], res

            cursor.execute("DELETE v1 FROM test WHERE k = 1 AND c = 1")
            res = cursor.execute("SELECT * FROM test")
            assert rows_to_list(res) == [[1, 1, None, None]], res

            cursor.execute("DELETE FROM test WHERE k = 1 AND c = 1")
            res = cursor.execute("SELECT * FROM test")
            assert rows_to_list(res) == [], res

            cursor.execute("INSERT INTO test (k, c) VALUES (2, 2)")
            res = cursor.execute("SELECT * FROM test")
            assert rows_to_list(res) == [[2, 2, None, None]], res

    @freshCluster()
    def only_pk_test(self):
        """ Check table with only a PK (#4361) """
        cursor = self.prepare(ordered=True)

        cursor.execute("""
            CREATE TABLE test (
                k int,
                c int,
                PRIMARY KEY (k, c)
            )
        """)

        # Check for dense tables too
        cursor.execute("""
            CREATE TABLE test2 (
                k int,
                c int,
                PRIMARY KEY (k, c)
            ) WITH COMPACT STORAGE
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")
            cursor.execute("TRUNCATE test2")

            q = "INSERT INTO test (k, c) VALUES (%s, %s)"
            for k in range(0, 2):
                for c in range(0, 2):
                    cursor.execute(q, (k, c))

            res = cursor.execute("SELECT * FROM test")
            assert rows_to_list(res) == [[x, y] for x in range(0, 2) for y in range(0, 2)], res

            q = "INSERT INTO test2 (k, c) VALUES (%s, %s)"
            for k in range(0, 2):
                for c in range(0, 2):
                    cursor.execute(q, (k, c))

            res = cursor.execute("SELECT * FROM test2")
            assert rows_to_list(res) == [[x, y] for x in range(0, 2) for y in range(0, 2)], res

    def no_clustering_test(self):
        cursor = self.prepare()
        cursor.execute("CREATE TABLE test (k int PRIMARY KEY, v int)")
        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))

            for i in range(10):
                cursor.execute("INSERT INTO test (k, v) VALUES (%s, %s)", (i, i))

            cursor.default_fetch_size = None
            results = rows_to_list(cursor.execute("SELECT * FROM test"))
            results.sort()
            self.assertEqual(10, len(results))
            self.assertEqual([[i, i] for i in range(10)], results)

    def date_test(self):
        """ Check dates are correctly recognized and validated """
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int PRIMARY KEY,
                t timestamp
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (k, t) VALUES (0, '2011-02-03')")
            assert_invalid(cursor, "INSERT INTO test (k, t) VALUES (0, '2011-42-42')")

    @freshCluster()
    def range_slice_test(self):
        """ Test a regression from #1337 """

        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k text PRIMARY KEY,
                v int
            );
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (k, v) VALUES ('foo', 0)")
            cursor.execute("INSERT INTO test (k, v) VALUES ('bar', 1)")

            res = list(cursor.execute("SELECT * FROM test"))
            assert len(res) == 2, res

    @freshCluster()
    def composite_index_with_pk_test(self):

        cursor = self.prepare(ordered=True)
        cursor.execute("""
            CREATE TABLE blogs (
                blog_id int,
                time1 int,
                time2 int,
                author text,
                content text,
                PRIMARY KEY (blog_id, time1, time2)
            )
        """)

        cursor.execute("CREATE INDEX ON blogs(author)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE blogs")

            req = "INSERT INTO blogs (blog_id, time1, time2, author, content) VALUES (%d, %d, %d, '%s', '%s')"
            cursor.execute(req % (1, 0, 0, 'foo', 'bar1'))
            cursor.execute(req % (1, 0, 1, 'foo', 'bar2'))
            cursor.execute(req % (2, 1, 0, 'foo', 'baz'))
            cursor.execute(req % (3, 0, 1, 'gux', 'qux'))

            res = cursor.execute("SELECT blog_id, content FROM blogs WHERE author='foo'")
            assert rows_to_list(res) == [[1, 'bar1'], [1, 'bar2'], [2, 'baz']], res

            res = cursor.execute("SELECT blog_id, content FROM blogs WHERE time1 > 0 AND author='foo' ALLOW FILTERING")
            assert rows_to_list(res) == [[2, 'baz']], res

            res = cursor.execute("SELECT blog_id, content FROM blogs WHERE time1 = 1 AND author='foo' ALLOW FILTERING")
            assert rows_to_list(res) == [[2, 'baz']], res

            res = cursor.execute("SELECT blog_id, content FROM blogs WHERE time1 = 1 AND time2 = 0 AND author='foo' ALLOW FILTERING")
            assert rows_to_list(res) == [[2, 'baz']], res

            res = cursor.execute("SELECT content FROM blogs WHERE time1 = 1 AND time2 = 1 AND author='foo' ALLOW FILTERING")
            assert rows_to_list(res) == [], res

            res = cursor.execute("SELECT content FROM blogs WHERE time1 = 1 AND time2 > 0 AND author='foo' ALLOW FILTERING")
            assert rows_to_list(res) == [], res

            assert_invalid(cursor, "SELECT content FROM blogs WHERE time2 >= 0 AND author='foo'")

            # as discussed in CASSANDRA-8148, some queries that should have required ALLOW FILTERING
            # in 2.0 have been fixed for 2.2
            if self.get_node_version(is_upgraded) < '2.2':
                # the coordinator is the non-upgraded 2.1 node
                cursor.execute("SELECT blog_id, content FROM blogs WHERE time1 > 0 AND author='foo'")
                cursor.execute("SELECT blog_id, content FROM blogs WHERE time1 = 1 AND author='foo'")
                cursor.execute("SELECT blog_id, content FROM blogs WHERE time1 = 1 AND time2 = 0 AND author='foo'")
                cursor.execute("SELECT content FROM blogs WHERE time1 = 1 AND time2 = 1 AND author='foo'")
                cursor.execute("SELECT content FROM blogs WHERE time1 = 1 AND time2 > 0 AND author='foo'")
            else:
                # the coordinator is the upgraded 2.2+ node
                assert_invalid(cursor, "SELECT blog_id, content FROM blogs WHERE time1 > 0 AND author='foo'")
                assert_invalid(cursor, "SELECT blog_id, content FROM blogs WHERE time1 = 1 AND author='foo'")
                assert_invalid(cursor, "SELECT blog_id, content FROM blogs WHERE time1 = 1 AND time2 = 0 AND author='foo'")
                assert_invalid(cursor, "SELECT content FROM blogs WHERE time1 = 1 AND time2 = 1 AND author='foo'")
                assert_invalid(cursor, "SELECT content FROM blogs WHERE time1 = 1 AND time2 > 0 AND author='foo'")

    @freshCluster()
    def limit_bugs_test(self):
        """ Test for LIMIT bugs from 4579 """

        cursor = self.prepare(ordered=True)
        cursor.execute("""
            CREATE TABLE testcf (
                a int,
                b int,
                c int,
                d int,
                e int,
                PRIMARY KEY (a, b)
            );
        """)

        cursor.execute("""
            CREATE TABLE testcf2 (
                a int primary key,
                b int,
                c int,
            );
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE testcf")
            cursor.execute("TRUNCATE testcf2")

            cursor.execute("INSERT INTO testcf (a, b, c, d, e) VALUES (1, 1, 1, 1, 1);")
            cursor.execute("INSERT INTO testcf (a, b, c, d, e) VALUES (2, 2, 2, 2, 2);")
            cursor.execute("INSERT INTO testcf (a, b, c, d, e) VALUES (3, 3, 3, 3, 3);")
            cursor.execute("INSERT INTO testcf (a, b, c, d, e) VALUES (4, 4, 4, 4, 4);")

            res = cursor.execute("SELECT * FROM testcf;")
            assert rows_to_list(res) == [[1, 1, 1, 1, 1], [2, 2, 2, 2, 2], [3, 3, 3, 3, 3], [4, 4, 4, 4, 4]], res

            res = cursor.execute("SELECT * FROM testcf LIMIT 1;")  # columns d and e in result row are null
            assert rows_to_list(res) == [[1, 1, 1, 1, 1]], res

            res = cursor.execute("SELECT * FROM testcf LIMIT 2;")  # columns d and e in last result row are null
            assert rows_to_list(res) == [[1, 1, 1, 1, 1], [2, 2, 2, 2, 2]], res

            cursor.execute("INSERT INTO testcf2 (a, b, c) VALUES (1, 1, 1);")
            cursor.execute("INSERT INTO testcf2 (a, b, c) VALUES (2, 2, 2);")
            cursor.execute("INSERT INTO testcf2 (a, b, c) VALUES (3, 3, 3);")
            cursor.execute("INSERT INTO testcf2 (a, b, c) VALUES (4, 4, 4);")

            res = cursor.execute("SELECT * FROM testcf2;")
            assert rows_to_list(res) == [[1, 1, 1], [2, 2, 2], [3, 3, 3], [4, 4, 4]], res

            res = cursor.execute("SELECT * FROM testcf2 LIMIT 1;")  # gives 1 row
            assert rows_to_list(res) == [[1, 1, 1]], res

            res = cursor.execute("SELECT * FROM testcf2 LIMIT 2;")  # gives 1 row
            assert rows_to_list(res) == [[1, 1, 1], [2, 2, 2]], res

            res = cursor.execute("SELECT * FROM testcf2 LIMIT 3;")  # gives 2 rows
            assert rows_to_list(res) == [[1, 1, 1], [2, 2, 2], [3, 3, 3]], res

            res = cursor.execute("SELECT * FROM testcf2 LIMIT 4;")  # gives 2 rows
            assert rows_to_list(res) == [[1, 1, 1], [2, 2, 2], [3, 3, 3], [4, 4, 4]], res

            res = cursor.execute("SELECT * FROM testcf2 LIMIT 5;")  # gives 3 rows
            assert rows_to_list(res) == [[1, 1, 1], [2, 2, 2], [3, 3, 3], [4, 4, 4]], res

    def bug_4532_test(self):

        cursor = self.prepare()
        cursor.execute("""
            CREATE TABLE compositetest(
                status ascii,
                ctime bigint,
                key ascii,
                nil ascii,
                PRIMARY KEY (status, ctime, key)
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE compositetest")

            cursor.execute("INSERT INTO compositetest(status,ctime,key,nil) VALUES ('C',12345678,'key1','')")
            cursor.execute("INSERT INTO compositetest(status,ctime,key,nil) VALUES ('C',12345678,'key2','')")
            cursor.execute("INSERT INTO compositetest(status,ctime,key,nil) VALUES ('C',12345679,'key3','')")
            cursor.execute("INSERT INTO compositetest(status,ctime,key,nil) VALUES ('C',12345679,'key4','')")
            cursor.execute("INSERT INTO compositetest(status,ctime,key,nil) VALUES ('C',12345679,'key5','')")
            cursor.execute("INSERT INTO compositetest(status,ctime,key,nil) VALUES ('C',12345680,'key6','')")

            assert_invalid(cursor, "SELECT * FROM compositetest WHERE ctime>=12345679 AND key='key3' AND ctime<=12345680 LIMIT 3;")
            assert_invalid(cursor, "SELECT * FROM compositetest WHERE ctime=12345679  AND key='key3' AND ctime<=12345680 LIMIT 3")

    @freshCluster()
    def order_by_multikey_test(self):
        """ Test for #4612 bug and more generaly order by when multiple C* rows are queried """

        cursor = self.prepare(ordered=True)
        cursor.execute("""
            CREATE TABLE test(
                my_id varchar,
                col1 int,
                col2 int,
                value varchar,
                PRIMARY KEY (my_id, col1, col2)
            );
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")
            cursor.default_fetch_size = None

            cursor.execute("INSERT INTO test(my_id, col1, col2, value) VALUES ( 'key1', 1, 1, 'a');")
            cursor.execute("INSERT INTO test(my_id, col1, col2, value) VALUES ( 'key2', 3, 3, 'a');")
            cursor.execute("INSERT INTO test(my_id, col1, col2, value) VALUES ( 'key3', 2, 2, 'b');")
            cursor.execute("INSERT INTO test(my_id, col1, col2, value) VALUES ( 'key4', 2, 1, 'b');")

            res = cursor.execute("SELECT col1 FROM test WHERE my_id in('key1', 'key2', 'key3') ORDER BY col1;")
            assert rows_to_list(res) == [[1], [2], [3]], res

            res = cursor.execute("SELECT col1, value, my_id, col2 FROM test WHERE my_id in('key3', 'key4') ORDER BY col1, col2;")
            assert rows_to_list(res) == [[2, 'b', 'key4', 1], [2, 'b', 'key3', 2]], res

            assert_invalid(cursor, "SELECT col1 FROM test ORDER BY col1;")
            assert_invalid(cursor, "SELECT col1 FROM test WHERE my_id > 'key1' ORDER BY col1;")

    def remove_range_slice_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int PRIMARY KEY,
                v int
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            for i in range(0, 3):
                cursor.execute("INSERT INTO test (k, v) VALUES (%d, %d)" % (i, i))

            cursor.execute("DELETE FROM test WHERE k = 1")
            res = cursor.execute("SELECT * FROM test")
            assert rows_to_list(res) == [[0, 0], [2, 2]], res

    def indexes_composite_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                blog_id int,
                timestamp int,
                author text,
                content text,
                PRIMARY KEY (blog_id, timestamp)
            )
        """)

        cursor.execute("CREATE INDEX ON test(author)")
        time.sleep(1)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            req = "INSERT INTO test (blog_id, timestamp, author, content) VALUES (%d, %d, '%s', '%s')"
            cursor.execute(req % (0, 0, "bob", "1st post"))
            cursor.execute(req % (0, 1, "tom", "2nd post"))
            cursor.execute(req % (0, 2, "bob", "3rd post"))
            cursor.execute(req % (0, 3, "tom", "4nd post"))
            cursor.execute(req % (1, 0, "bob", "5th post"))

            res = cursor.execute("SELECT blog_id, timestamp FROM test WHERE author = 'bob'")
            assert rows_to_list(res) == [[1, 0], [0, 0], [0, 2]], res

            cursor.execute(req % (1, 1, "tom", "6th post"))
            cursor.execute(req % (1, 2, "tom", "7th post"))
            cursor.execute(req % (1, 3, "bob", "8th post"))

            res = cursor.execute("SELECT blog_id, timestamp FROM test WHERE author = 'bob'")
            assert rows_to_list(res) == [[1, 0], [1, 3], [0, 0], [0, 2]], res

            cursor.execute("DELETE FROM test WHERE blog_id = 0 AND timestamp = 2")

            res = cursor.execute("SELECT blog_id, timestamp FROM test WHERE author = 'bob'")
            assert rows_to_list(res) == [[1, 0], [1, 3], [0, 0]], res

    def refuse_in_with_indexes_test(self):
        """ Test for the validation bug of #4709 """

        cursor = self.prepare()
        cursor.execute("create table t1 (pk varchar primary key, col1 varchar, col2 varchar);")
        cursor.execute("create index t1_c1 on t1(col1);")
        cursor.execute("create index t1_c2 on t1(col2);")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE t1")

            cursor.execute("insert into t1  (pk, col1, col2) values ('pk1','foo1','bar1');")
            cursor.execute("insert into t1  (pk, col1, col2) values ('pk1a','foo1','bar1');")
            cursor.execute("insert into t1  (pk, col1, col2) values ('pk1b','foo1','bar1');")
            cursor.execute("insert into t1  (pk, col1, col2) values ('pk1c','foo1','bar1');")
            cursor.execute("insert into t1  (pk, col1, col2) values ('pk2','foo2','bar2');")
            cursor.execute("insert into t1  (pk, col1, col2) values ('pk3','foo3','bar3');")
            assert_invalid(cursor, "select * from t1 where col2 in ('bar1', 'bar2');")

    def reversed_compact_test(self):
        """ Test for #4716 bug and more generally for good behavior of ordering"""

        cursor = self.prepare()
        cursor.execute("""
            CREATE TABLE test1 (
                k text,
                c int,
                v int,
                PRIMARY KEY (k, c)
            ) WITH COMPACT STORAGE
              AND CLUSTERING ORDER BY (c DESC);
        """)

        cursor.execute("""
            CREATE TABLE test2 (
                k text,
                c int,
                v int,
                PRIMARY KEY (k, c)
            ) WITH COMPACT STORAGE;
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test1")
            cursor.execute("TRUNCATE test2")

            for i in range(0, 10):
                cursor.execute("INSERT INTO test1(k, c, v) VALUES ('foo', %s, %s)", (i, i))

            res = cursor.execute("SELECT c FROM test1 WHERE c > 2 AND c < 6 AND k = 'foo'")
            assert rows_to_list(res) == [[5], [4], [3]], res

            res = cursor.execute("SELECT c FROM test1 WHERE c >= 2 AND c <= 6 AND k = 'foo'")
            assert rows_to_list(res) == [[6], [5], [4], [3], [2]], res

            res = cursor.execute("SELECT c FROM test1 WHERE c > 2 AND c < 6 AND k = 'foo' ORDER BY c ASC")
            assert rows_to_list(res) == [[3], [4], [5]], res

            res = cursor.execute("SELECT c FROM test1 WHERE c >= 2 AND c <= 6 AND k = 'foo' ORDER BY c ASC")
            assert rows_to_list(res) == [[2], [3], [4], [5], [6]], res

            res = cursor.execute("SELECT c FROM test1 WHERE c > 2 AND c < 6 AND k = 'foo' ORDER BY c DESC")
            assert rows_to_list(res) == [[5], [4], [3]], res

            res = cursor.execute("SELECT c FROM test1 WHERE c >= 2 AND c <= 6 AND k = 'foo' ORDER BY c DESC")
            assert rows_to_list(res) == [[6], [5], [4], [3], [2]], res

            for i in range(0, 10):
                cursor.execute("INSERT INTO test2(k, c, v) VALUES ('foo', %s, %s)", (i, i))

            res = cursor.execute("SELECT c FROM test2 WHERE c > 2 AND c < 6 AND k = 'foo'")
            assert rows_to_list(res) == [[3], [4], [5]], res

            res = cursor.execute("SELECT c FROM test2 WHERE c >= 2 AND c <= 6 AND k = 'foo'")
            assert rows_to_list(res) == [[2], [3], [4], [5], [6]], res

            res = cursor.execute("SELECT c FROM test2 WHERE c > 2 AND c < 6 AND k = 'foo' ORDER BY c ASC")
            assert rows_to_list(res) == [[3], [4], [5]], res

            res = cursor.execute("SELECT c FROM test2 WHERE c >= 2 AND c <= 6 AND k = 'foo' ORDER BY c ASC")
            assert rows_to_list(res) == [[2], [3], [4], [5], [6]], res

            res = cursor.execute("SELECT c FROM test2 WHERE c > 2 AND c < 6 AND k = 'foo' ORDER BY c DESC")
            assert rows_to_list(res) == [[5], [4], [3]], res

            res = cursor.execute("SELECT c FROM test2 WHERE c >= 2 AND c <= 6 AND k = 'foo' ORDER BY c DESC")
            assert rows_to_list(res) == [[6], [5], [4], [3], [2]], res

    def reversed_compact_multikey_test(self):
        """ Test for the bug from #4760 and #4759 """

        cursor = self.prepare()
        cursor.execute("""
            CREATE TABLE test (
                key text,
                c1 int,
                c2 int,
                value text,
                PRIMARY KEY(key, c1, c2)
                ) WITH COMPACT STORAGE
                  AND CLUSTERING ORDER BY(c1 DESC, c2 DESC);
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            for i in range(0, 3):
                for j in range(0, 3):
                    cursor.execute("INSERT INTO test(key, c1, c2, value) VALUES ('foo', %i, %i, 'bar');" % (i, j))

            # Equalities

            res = cursor.execute("SELECT c1, c2 FROM test WHERE key='foo' AND c1 = 1")
            assert rows_to_list(res) == [[1, 2], [1, 1], [1, 0]], res

            res = cursor.execute("SELECT c1, c2 FROM test WHERE key='foo' AND c1 = 1 ORDER BY c1 ASC, c2 ASC")
            assert rows_to_list(res) == [[1, 0], [1, 1], [1, 2]], res

            res = cursor.execute("SELECT c1, c2 FROM test WHERE key='foo' AND c1 = 1 ORDER BY c1 DESC, c2 DESC")
            assert rows_to_list(res) == [[1, 2], [1, 1], [1, 0]], res

            # GT

            res = cursor.execute("SELECT c1, c2 FROM test WHERE key='foo' AND c1 > 1")
            assert rows_to_list(res) == [[2, 2], [2, 1], [2, 0]], res

            res = cursor.execute("SELECT c1, c2 FROM test WHERE key='foo' AND c1 > 1 ORDER BY c1 ASC, c2 ASC")
            assert rows_to_list(res) == [[2, 0], [2, 1], [2, 2]], res

            res = cursor.execute("SELECT c1, c2 FROM test WHERE key='foo' AND c1 > 1 ORDER BY c1 DESC, c2 DESC")
            assert rows_to_list(res) == [[2, 2], [2, 1], [2, 0]], res

            res = cursor.execute("SELECT c1, c2 FROM test WHERE key='foo' AND c1 >= 1")
            assert rows_to_list(res) == [[2, 2], [2, 1], [2, 0], [1, 2], [1, 1], [1, 0]], res

            res = cursor.execute("SELECT c1, c2 FROM test WHERE key='foo' AND c1 >= 1 ORDER BY c1 ASC, c2 ASC")
            assert rows_to_list(res) == [[1, 0], [1, 1], [1, 2], [2, 0], [2, 1], [2, 2]], res

            res = cursor.execute("SELECT c1, c2 FROM test WHERE key='foo' AND c1 >= 1 ORDER BY c1 ASC")
            assert rows_to_list(res) == [[1, 0], [1, 1], [1, 2], [2, 0], [2, 1], [2, 2]], res

            res = cursor.execute("SELECT c1, c2 FROM test WHERE key='foo' AND c1 >= 1 ORDER BY c1 DESC, c2 DESC")
            assert rows_to_list(res) == [[2, 2], [2, 1], [2, 0], [1, 2], [1, 1], [1, 0]], res

            # LT

            res = cursor.execute("SELECT c1, c2 FROM test WHERE key='foo' AND c1 < 1")
            assert rows_to_list(res) == [[0, 2], [0, 1], [0, 0]], res

            res = cursor.execute("SELECT c1, c2 FROM test WHERE key='foo' AND c1 < 1 ORDER BY c1 ASC, c2 ASC")
            assert rows_to_list(res) == [[0, 0], [0, 1], [0, 2]], res

            res = cursor.execute("SELECT c1, c2 FROM test WHERE key='foo' AND c1 < 1 ORDER BY c1 DESC, c2 DESC")
            assert rows_to_list(res) == [[0, 2], [0, 1], [0, 0]], res

            res = cursor.execute("SELECT c1, c2 FROM test WHERE key='foo' AND c1 <= 1")
            assert rows_to_list(res) == [[1, 2], [1, 1], [1, 0], [0, 2], [0, 1], [0, 0]], res

            res = cursor.execute("SELECT c1, c2 FROM test WHERE key='foo' AND c1 <= 1 ORDER BY c1 ASC, c2 ASC")
            assert rows_to_list(res) == [[0, 0], [0, 1], [0, 2], [1, 0], [1, 1], [1, 2]], res

            res = cursor.execute("SELECT c1, c2 FROM test WHERE key='foo' AND c1 <= 1 ORDER BY c1 ASC")
            assert rows_to_list(res) == [[0, 0], [0, 1], [0, 2], [1, 0], [1, 1], [1, 2]], res

            res = cursor.execute("SELECT c1, c2 FROM test WHERE key='foo' AND c1 <= 1 ORDER BY c1 DESC, c2 DESC")
            assert rows_to_list(res) == [[1, 2], [1, 1], [1, 0], [0, 2], [0, 1], [0, 0]], res

    def collection_and_regular_test(self):

        cursor = self.prepare()

        cursor.execute("""
          CREATE TABLE test (
            k int PRIMARY KEY,
            l list<int>,
            c int
          )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test(k, l, c) VALUES(3, [0, 1, 2], 4)")
            cursor.execute("UPDATE test SET l[0] = 1, c = 42 WHERE k = 3")
            res = cursor.execute("SELECT l, c FROM test WHERE k = 3")
            self.assertItemsEqual(rows_to_list(res), [[[1, 1, 2], 42]])

    def batch_and_list_test(self):
        cursor = self.prepare()

        cursor.execute("""
          CREATE TABLE test (
            k int PRIMARY KEY,
            l list<int>
          )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("""
              BEGIN BATCH
                UPDATE test SET l = l + [ 1 ] WHERE k = 0;
                UPDATE test SET l = l + [ 2 ] WHERE k = 0;
                UPDATE test SET l = l + [ 3 ] WHERE k = 0;
              APPLY BATCH
            """)

            res = cursor.execute("SELECT l FROM test WHERE k = 0")
            self.assertItemsEqual(rows_to_list(res[0]), [[1, 2, 3]])

            cursor.execute("""
              BEGIN BATCH
                UPDATE test SET l = [ 1 ] + l WHERE k = 1;
                UPDATE test SET l = [ 2 ] + l WHERE k = 1;
                UPDATE test SET l = [ 3 ] + l WHERE k = 1;
              APPLY BATCH
            """)

            res = cursor.execute("SELECT l FROM test WHERE k = 1")
            self.assertItemsEqual(rows_to_list(res[0]), [[3, 2, 1]])

    def boolean_test(self):
        cursor = self.prepare()

        cursor.execute("""
          CREATE TABLE test (
            k boolean PRIMARY KEY,
            b boolean
          )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (k, b) VALUES (true, false)")
            res = cursor.execute("SELECT * FROM test WHERE k = true")
            assert rows_to_list(res) == [[True, False]], res

    def multiordering_test(self):
        cursor = self.prepare()
        cursor.execute("""
            CREATE TABLE test (
                k text,
                c1 int,
                c2 int,
                PRIMARY KEY (k, c1, c2)
            ) WITH CLUSTERING ORDER BY (c1 ASC, c2 DESC);
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            for i in range(0, 2):
                for j in range(0, 2):
                    cursor.execute("INSERT INTO test(k, c1, c2) VALUES ('foo', %i, %i)" % (i, j))

            res = cursor.execute("SELECT c1, c2 FROM test WHERE k = 'foo'")
            assert rows_to_list(res) == [[0, 1], [0, 0], [1, 1], [1, 0]], res

            res = cursor.execute("SELECT c1, c2 FROM test WHERE k = 'foo' ORDER BY c1 ASC, c2 DESC")
            assert rows_to_list(res) == [[0, 1], [0, 0], [1, 1], [1, 0]], res

            res = cursor.execute("SELECT c1, c2 FROM test WHERE k = 'foo' ORDER BY c1 DESC, c2 ASC")
            assert rows_to_list(res) == [[1, 0], [1, 1], [0, 0], [0, 1]], res

            assert_invalid(cursor, "SELECT c1, c2 FROM test WHERE k = 'foo' ORDER BY c2 DESC")
            assert_invalid(cursor, "SELECT c1, c2 FROM test WHERE k = 'foo' ORDER BY c2 ASC")
            assert_invalid(cursor, "SELECT c1, c2 FROM test WHERE k = 'foo' ORDER BY c1 ASC, c2 ASC")

    def bug_4882_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int,
                c1 int,
                c2 int,
                v int,
                PRIMARY KEY (k, c1, c2)
            ) WITH CLUSTERING ORDER BY (c1 ASC, c2 DESC);
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (k, c1, c2, v) VALUES (0, 0, 0, 0);")
            cursor.execute("INSERT INTO test (k, c1, c2, v) VALUES (0, 1, 1, 1);")
            cursor.execute("INSERT INTO test (k, c1, c2, v) VALUES (0, 0, 2, 2);")
            cursor.execute("INSERT INTO test (k, c1, c2, v) VALUES (0, 1, 3, 3);")

            res = cursor.execute("select * from test where k = 0 limit 1;")
            assert rows_to_list(res) == [[0, 0, 2, 2]], res

    def multi_list_set_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int PRIMARY KEY,
                l1 list<int>,
                l2 list<int>
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (k, l1, l2) VALUES (0, [1, 2, 3], [4, 5, 6])")
            cursor.execute("UPDATE test SET l2[1] = 42, l1[1] = 24  WHERE k = 0")

            res = cursor.execute("SELECT l1, l2 FROM test WHERE k = 0")
            self.assertItemsEqual(rows_to_list(res), [[[1, 24, 3], [4, 42, 6]]])

    @freshCluster()
    def composite_index_collections_test(self):
        cursor = self.prepare(ordered=True)
        cursor.execute("""
            CREATE TABLE blogs (
                blog_id int,
                time1 int,
                time2 int,
                author text,
                content set<text>,
                PRIMARY KEY (blog_id, time1, time2)
            )
        """)

        cursor.execute("CREATE INDEX ON blogs(author)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE blogs")

            req = "INSERT INTO blogs (blog_id, time1, time2, author, content) VALUES (%d, %d, %d, '%s', %s)"
            cursor.execute(req % (1, 0, 0, 'foo', "{ 'bar1', 'bar2' }"))
            cursor.execute(req % (1, 0, 1, 'foo', "{ 'bar2', 'bar3' }"))
            cursor.execute(req % (2, 1, 0, 'foo', "{ 'baz' }"))
            cursor.execute(req % (3, 0, 1, 'gux', "{ 'qux' }"))

            res = cursor.execute("SELECT blog_id, content FROM blogs WHERE author='foo'")
            assert rows_to_list(res) == [[1, set(['bar1', 'bar2'])], [1, set(['bar2', 'bar3'])], [2, set(['baz'])]], res

    @freshCluster()
    def truncate_clean_cache_test(self):
        cursor = self.prepare(ordered=True, use_cache=True)

        if self.node_version_above('2.1'):
            cursor.execute("""
                CREATE TABLE test (
                    k int PRIMARY KEY,
                    v1 int,
                    v2 int,
                ) WITH caching = {'keys': 'NONE', 'rows_per_partition': 'ALL'};
            """)
        else:
            cursor.execute("""
                CREATE TABLE test (
                    k int PRIMARY KEY,
                    v1 int,
                    v2 int,
                ) WITH CACHING = ALL;
            """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            for i in range(0, 3):
                cursor.execute("INSERT INTO test(k, v1, v2) VALUES (%d, %d, %d)" % (i, i, i * 2))

            res = cursor.execute("SELECT v1, v2 FROM test WHERE k IN (0, 1, 2)")
            assert rows_to_list(res) == [[0, 0], [1, 2], [2, 4]], res

            cursor.execute("TRUNCATE test")

            res = cursor.execute("SELECT v1, v2 FROM test WHERE k IN (0, 1, 2)")
            assert rows_to_list(res) == [], res

    def range_with_deletes_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int PRIMARY KEY,
                v int,
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            nb_keys = 30
            nb_deletes = 5

            for i in range(0, nb_keys):
                cursor.execute("INSERT INTO test(k, v) VALUES (%d, %d)" % (i, i))

            for i in random.sample(xrange(nb_keys), nb_deletes):
                cursor.execute("DELETE FROM test WHERE k = %d" % i)

            res = list(cursor.execute("SELECT * FROM test LIMIT %d" % (nb_keys / 2)))
            assert len(res) == nb_keys / 2, "Expected %d but got %d" % (nb_keys / 2, len(res))

    def collection_function_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int PRIMARY KEY,
                l set<int>
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            assert_invalid(cursor, "SELECT ttl(l) FROM test WHERE k = 0")
            assert_invalid(cursor, "SELECT writetime(l) FROM test WHERE k = 0")

    def composite_partition_key_validation_test(self):
        """ Test for bug from #5122 """
        cursor = self.prepare()

        cursor.execute("CREATE TABLE foo (a int, b text, c uuid, PRIMARY KEY ((a, b)));")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE foo")

            cursor.execute("INSERT INTO foo (a, b , c ) VALUES (  1 , 'aze', 4d481800-4c5f-11e1-82e0-3f484de45426)")
            cursor.execute("INSERT INTO foo (a, b , c ) VALUES (  1 , 'ert', 693f5800-8acb-11e3-82e0-3f484de45426)")
            cursor.execute("INSERT INTO foo (a, b , c ) VALUES (  1 , 'opl', d4815800-2d8d-11e0-82e0-3f484de45426)")

            res = list(cursor.execute("SELECT * FROM foo"))
            assert len(res) == 3, res

            assert_invalid(cursor, "SELECT * FROM foo WHERE a=1")

    @since('2.2')
    def multi_in_test(self):
        self.__multi_in(False)

    @since('2.2')
    def multi_in_compact_test(self):
        self.__multi_in(True)

    def __multi_in(self, compact):
        cursor = self.prepare()

        data = [
            ('test', '06029', 'CT', 9, 'Ellington'),
            ('test', '06031', 'CT', 9, 'Falls Village'),
            ('test', '06902', 'CT', 9, 'Stamford'),
            ('test', '06927', 'CT', 9, 'Stamford'),
            ('test', '10015', 'NY', 36, 'New York'),
            ('test', '07182', 'NJ', 34, 'Newark'),
            ('test', '73301', 'TX', 48, 'Austin'),
            ('test', '94102', 'CA', 06, 'San Francisco'),

            ('test2', '06029', 'CT', 9, 'Ellington'),
            ('test2', '06031', 'CT', 9, 'Falls Village'),
            ('test2', '06902', 'CT', 9, 'Stamford'),
            ('test2', '06927', 'CT', 9, 'Stamford'),
            ('test2', '10015', 'NY', 36, 'New York'),
            ('test2', '07182', 'NJ', 34, 'Newark'),
            ('test2', '73301', 'TX', 48, 'Austin'),
            ('test2', '94102', 'CA', 06, 'San Francisco'),
        ]

        create = """
            CREATE TABLE zipcodes (
                group text,
                zipcode text,
                state text,
                fips_regions int,
                city text,
                PRIMARY KEY(group,zipcode,state,fips_regions)
            )"""

        if compact:
            create = create + " WITH COMPACT STORAGE"

        cursor.execute(create)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE zipcodes")

            for d in data:
                cursor.execute("INSERT INTO zipcodes (group, zipcode, state, fips_regions, city) VALUES ('%s', '%s', '%s', %i, '%s')" % d)

            res = list(cursor.execute("select zipcode from zipcodes"))
            assert len(res) == 16, res

            res = list(cursor.execute("select zipcode from zipcodes where group='test'"))
            assert len(res) == 8, res

            assert_invalid(cursor, "select zipcode from zipcodes where zipcode='06902'")

            res = list(cursor.execute("select zipcode from zipcodes where zipcode='06902' ALLOW FILTERING"))
            assert len(res) == 2, res

            res = list(cursor.execute("select zipcode from zipcodes where group='test' and zipcode='06902'"))
            assert len(res) == 1, res

            if is_upgraded:
                # the coordinator is the upgraded 2.2+ node

                res = list(cursor.execute("select zipcode from zipcodes where group='test' and zipcode IN ('06902','73301','94102')"))
                assert len(res) == 3, res

                res = list(cursor.execute("select zipcode from zipcodes where group='test' AND zipcode IN ('06902','73301','94102') and state IN ('CT','CA')"))
                assert len(res) == 2, res

                res = list(cursor.execute("select zipcode from zipcodes where group='test' AND zipcode IN ('06902','73301','94102') and state IN ('CT','CA') and fips_regions = 9"))
                assert len(res) == 1, res

                res = list(cursor.execute("select zipcode from zipcodes where group='test' AND zipcode IN ('06902','73301','94102') and state IN ('CT','CA') ORDER BY zipcode DESC"))
                assert len(res) == 2, res

                res = list(cursor.execute("select zipcode from zipcodes where group='test' AND zipcode IN ('06902','73301','94102') and state IN ('CT','CA') and fips_regions > 0"))
                assert len(res) == 2, res

                res = list(cursor.execute("select zipcode from zipcodes where group='test' AND zipcode IN ('06902','73301','94102') and state IN ('CT','CA') and fips_regions < 0"))
                assert len(res) == 0, res

    @since('2.2')
    def multi_in_compact_non_composite_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                key int,
                c int,
                v int,
                PRIMARY KEY (key, c)
            ) WITH COMPACT STORAGE
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (key, c, v) VALUES (0, 0, 0)")
            cursor.execute("INSERT INTO test (key, c, v) VALUES (0, 1, 1)")
            cursor.execute("INSERT INTO test (key, c, v) VALUES (0, 2, 2)")

            res = cursor.execute("SELECT * FROM test WHERE key=0 AND c IN (0, 2)")
            assert rows_to_list(res) == [[0, 0, 0], [0, 2, 2]], res

    def large_clustering_in_test(self):
        # Test for CASSANDRA-8410
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int,
                c int,
                v int,
                PRIMARY KEY (k, c)
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            insert_statement = cursor.prepare("INSERT INTO test (k, c, v) VALUES (?, ?, ?)")
            cursor.execute(insert_statement, (0, 0, 0))

            select_statement = cursor.prepare("SELECT * FROM test WHERE k=? AND c IN ?")
            in_values = list(range(10000))

            # try to fetch one existing row and 9999 non-existing rows
            rows = list(cursor.execute(select_statement, [0, in_values]))
            self.assertEqual(1, len(rows))
            self.assertEqual((0, 0, 0), rows[0])

            # insert approximately 1000 random rows between 0 and 10k
            clustering_values = set([random.randint(0, 9999) for _ in range(1000)])
            clustering_values.add(0)
            args = [(0, i, i) for i in clustering_values]
            execute_concurrent_with_args(cursor, insert_statement, args)

            rows = list(cursor.execute(select_statement, [0, in_values]))
            self.assertEqual(len(clustering_values), len(rows))

    def timeuuid_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int,
                t timeuuid,
                PRIMARY KEY (k, t)
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            assert_invalid(cursor, "INSERT INTO test (k, t) VALUES (0, 2012-11-07 18:18:22-0800)", expected=SyntaxException)

            for i in range(4):
                cursor.execute("INSERT INTO test (k, t) VALUES (0, now())")
                time.sleep(1)

            res = list(cursor.execute("SELECT * FROM test"))
            assert len(res) == 4, res
            dates = [d[1] for d in res]

            res = list(cursor.execute("SELECT * FROM test WHERE k = 0 AND t >= %s" % dates[0]))
            assert len(res) == 4, res

            res = list(cursor.execute("SELECT * FROM test WHERE k = 0 AND t < %s" % dates[0]))
            assert len(res) == 0, res

            res = list(cursor.execute("SELECT * FROM test WHERE k = 0 AND t > %s AND t <= %s" % (dates[0], dates[2])))
            assert len(res) == 2, res

            res = list(cursor.execute("SELECT * FROM test WHERE k = 0 AND t = %s" % dates[0]))
            assert len(res) == 1, res

            assert_invalid(cursor, "SELECT dateOf(k) FROM test WHERE k = 0 AND t = %s" % dates[0])

            cursor.execute("SELECT dateOf(t), unixTimestampOf(t) FROM test WHERE k = 0 AND t = %s" % dates[0])
            cursor.execute("SELECT t FROM test WHERE k = 0 AND t > maxTimeuuid(1234567) AND t < minTimeuuid('2012-11-07 18:18:22-0800')")
            # not sure what to check exactly so just checking the query returns

    def float_with_exponent_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int PRIMARY KEY,
                d double,
                f float
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test(k, d, f) VALUES (0, 3E+10, 3.4E3)")
            cursor.execute("INSERT INTO test(k, d, f) VALUES (1, 3.E10, -23.44E-3)")
            cursor.execute("INSERT INTO test(k, d, f) VALUES (2, 3, -2)")

    def compact_metadata_test(self):
        """ Test regression from #5189 """
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE bar (
                id int primary key,
                i int
            ) WITH COMPACT STORAGE;
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE bar")

            cursor.execute("INSERT INTO bar (id, i) VALUES (1, 2);")
            res = cursor.execute("SELECT * FROM bar")
            assert rows_to_list(res) == [[1, 2]], res

    def clustering_indexing_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE posts (
                id1 int,
                id2 int,
                author text,
                time bigint,
                v1 text,
                v2 text,
                PRIMARY KEY ((id1, id2), author, time)
            )
        """)

        cursor.execute("CREATE INDEX ON posts(time)")
        cursor.execute("CREATE INDEX ON posts(id2)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE posts")

            cursor.execute("INSERT INTO posts(id1, id2, author, time, v1, v2) VALUES(0, 0, 'bob', 0, 'A', 'A')")
            cursor.execute("INSERT INTO posts(id1, id2, author, time, v1, v2) VALUES(0, 0, 'bob', 1, 'B', 'B')")
            cursor.execute("INSERT INTO posts(id1, id2, author, time, v1, v2) VALUES(0, 1, 'bob', 2, 'C', 'C')")
            cursor.execute("INSERT INTO posts(id1, id2, author, time, v1, v2) VALUES(0, 0, 'tom', 0, 'D', 'D')")
            cursor.execute("INSERT INTO posts(id1, id2, author, time, v1, v2) VALUES(0, 1, 'tom', 1, 'E', 'E')")

            res = cursor.execute("SELECT v1 FROM posts WHERE time = 1")
            assert rows_to_list(res) == [['B'], ['E']], res

            res = cursor.execute("SELECT v1 FROM posts WHERE id2 = 1")
            assert rows_to_list(res) == [['C'], ['E']], res

            res = cursor.execute("SELECT v1 FROM posts WHERE id1 = 0 AND id2 = 0 AND author = 'bob' AND time = 0")
            assert rows_to_list(res) == [['A']], res

            # Test for CASSANDRA-8206
            cursor.execute("UPDATE posts SET v2 = null WHERE id1 = 0 AND id2 = 0 AND author = 'bob' AND time = 1")

            res = cursor.execute("SELECT v1 FROM posts WHERE id2 = 0")
            assert rows_to_list(res) == [['A'], ['B'], ['D']], res

            res = cursor.execute("SELECT v1 FROM posts WHERE time = 1")
            assert rows_to_list(res) == [['B'], ['E']], res

    def edge_2i_on_complex_pk_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE indexed (
                pk0 int,
                pk1 int,
                ck0 int,
                ck1 int,
                ck2 int,
                value int,
                PRIMARY KEY ((pk0, pk1), ck0, ck1, ck2)
            )
        """)

        cursor.execute("CREATE INDEX ON indexed(pk0)")
        cursor.execute("CREATE INDEX ON indexed(ck0)")
        cursor.execute("CREATE INDEX ON indexed(ck1)")
        cursor.execute("CREATE INDEX ON indexed(ck2)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE indexed")

            cursor.execute("INSERT INTO indexed (pk0, pk1, ck0, ck1, ck2, value) VALUES (0, 1, 2, 3, 4, 5)")
            cursor.execute("INSERT INTO indexed (pk0, pk1, ck0, ck1, ck2, value) VALUES (1, 2, 3, 4, 5, 0)")
            cursor.execute("INSERT INTO indexed (pk0, pk1, ck0, ck1, ck2, value) VALUES (2, 3, 4, 5, 0, 1)")
            cursor.execute("INSERT INTO indexed (pk0, pk1, ck0, ck1, ck2, value) VALUES (3, 4, 5, 0, 1, 2)")
            cursor.execute("INSERT INTO indexed (pk0, pk1, ck0, ck1, ck2, value) VALUES (4, 5, 0, 1, 2, 3)")
            cursor.execute("INSERT INTO indexed (pk0, pk1, ck0, ck1, ck2, value) VALUES (5, 0, 1, 2, 3, 4)")

            res = cursor.execute("SELECT value FROM indexed WHERE pk0 = 2")
            self.assertEqual([[1]], rows_to_list(res))

            res = cursor.execute("SELECT value FROM indexed WHERE ck0 = 0")
            self.assertEqual([[3]], rows_to_list(res))

            res = cursor.execute("SELECT value FROM indexed WHERE pk0 = 3 AND pk1 = 4 AND ck1 = 0")
            self.assertEqual([[2]], rows_to_list(res))

            res = cursor.execute("SELECT value FROM indexed WHERE pk0 = 5 AND pk1 = 0 AND ck0 = 1 AND ck2 = 3 ALLOW FILTERING")
            self.assertEqual([[4]], rows_to_list(res))

    def bug_5240_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test(
                interval text,
                seq int,
                id int,
                severity int,
                PRIMARY KEY ((interval, seq), id)
            ) WITH CLUSTERING ORDER BY (id DESC);
        """)

        cursor.execute("CREATE INDEX ON test(severity);")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("insert into test(interval, seq, id , severity) values('t',1, 1, 1);")
            cursor.execute("insert into test(interval, seq, id , severity) values('t',1, 2, 1);")
            cursor.execute("insert into test(interval, seq, id , severity) values('t',1, 3, 2);")
            cursor.execute("insert into test(interval, seq, id , severity) values('t',1, 4, 3);")
            cursor.execute("insert into test(interval, seq, id , severity) values('t',2, 1, 3);")
            cursor.execute("insert into test(interval, seq, id , severity) values('t',2, 2, 3);")
            cursor.execute("insert into test(interval, seq, id , severity) values('t',2, 3, 1);")
            cursor.execute("insert into test(interval, seq, id , severity) values('t',2, 4, 2);")

            res = cursor.execute("select * from test where severity = 3 and interval = 't' and seq =1;")
            assert rows_to_list(res) == [['t', 1, 4, 3]], res

    def ticket_5230_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE foo (
                key text,
                c text,
                v text,
                PRIMARY KEY (key, c)
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE foo")

            cursor.execute("INSERT INTO foo(key, c, v) VALUES ('foo', '1', '1')")
            cursor.execute("INSERT INTO foo(key, c, v) VALUES ('foo', '2', '2')")
            cursor.execute("INSERT INTO foo(key, c, v) VALUES ('foo', '3', '3')")

            res = cursor.execute("SELECT c FROM foo WHERE key = 'foo' AND c IN ('1', '2');")
            assert rows_to_list(res) == [['1'], ['2']], res

    def conversion_functions_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int PRIMARY KEY,
                i varint,
                b blob
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test(k, i, b) VALUES (0, blobAsVarint(bigintAsBlob(3)), textAsBlob('foobar'))")
            res = cursor.execute("SELECT i, blobAsText(b) FROM test WHERE k = 0")
            assert rows_to_list(res) == [[3, 'foobar']], res

    def bug_5376(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                key text,
                c bigint,
                v text,
                x set<text>,
                PRIMARY KEY (key, c)
            );
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            assert_invalid(cursor, "select * from test where key = 'foo' and c in (1,3,4);")

    def function_and_reverse_type_test(self):
        """ Test for #5386 """

        cursor = self.prepare()
        cursor.execute("""
            CREATE TABLE test (
                k int,
                c timeuuid,
                v int,
                PRIMARY KEY (k, c)
            ) WITH CLUSTERING ORDER BY (c DESC)
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("INSERT INTO test (k, c, v) VALUES (0, now(), 0);")

    def bug_5404(self):
        cursor = self.prepare()

        cursor.execute("CREATE TABLE test (key text PRIMARY KEY)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            # We just want to make sure this doesn't NPE server side
            assert_invalid(cursor, "select * from test where token(key) > token(int(3030343330393233)) limit 1;")

    def empty_blob_test(self):
        cursor = self.prepare()

        cursor.execute("CREATE TABLE test (k int PRIMARY KEY, b blob)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (k, b) VALUES (0, 0x)")
            res = cursor.execute("SELECT * FROM test")
            assert rows_to_list(res) == [[0, '']], res

    def rename_test(self):
        cursor = self.prepare(start_rpc=True)

        node = self.cluster.nodelist()[0]
        host, port = node.network_interfaces['thrift']
        client = get_thrift_client(host, port)
        client.transport.open()

        cfdef = CfDef()
        cfdef.keyspace = 'ks'
        cfdef.name = 'test'
        cfdef.column_type = 'Standard'
        cfdef.comparator_type = 'CompositeType(Int32Type, Int32Type, Int32Type)'
        cfdef.key_validation_class = 'UTF8Type'
        cfdef.default_validation_class = 'UTF8Type'

        client.set_keyspace('ks')
        client.system_add_column_family(cfdef)

        time.sleep(1)

        cursor.execute("INSERT INTO ks.test (key, column1, column2, column3, value) VALUES ('foo', 4, 3, 2, 'bar')")

        time.sleep(1)

        cursor.execute("ALTER TABLE test RENAME column1 TO foo1 AND column2 TO foo2 AND column3 TO foo3")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            assert_one(cursor, "SELECT foo1, foo2, foo3 FROM test", [4, 3, 2])

    def clustering_order_and_functions_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int,
                t timeuuid,
                PRIMARY KEY (k, t)
            ) WITH CLUSTERING ORDER BY (t DESC)
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            for i in range(0, 5):
                cursor.execute("INSERT INTO test (k, t) VALUES (%d, now())" % i)

            cursor.execute("SELECT dateOf(t) FROM test")

    def conditional_update_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int PRIMARY KEY,
                v1 int,
                v2 text,
                v3 int
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            # Shouldn't apply
            assert_one(cursor, "UPDATE test SET v1 = 3, v2 = 'bar' WHERE k = 0 IF v1 = 4", [False])
            assert_one(cursor, "UPDATE test SET v1 = 3, v2 = 'bar' WHERE k = 0 IF EXISTS", [False])

            # Should apply
            assert_one(cursor, "INSERT INTO test (k, v1, v2) VALUES (0, 2, 'foo') IF NOT EXISTS", [True])

            # Shouldn't apply
            assert_one(cursor, "INSERT INTO test (k, v1, v2) VALUES (0, 5, 'bar') IF NOT EXISTS", [False, 0, 2, 'foo', None])
            assert_one(cursor, "SELECT * FROM test", [0, 2, 'foo', None])

            # Should not apply
            assert_one(cursor, "UPDATE test SET v1 = 3, v2 = 'bar' WHERE k = 0 IF v1 = 4", [False, 2])
            assert_one(cursor, "SELECT * FROM test", [0, 2, 'foo', None])

            # Should apply (note: we want v2 before v1 in the statement order to exercise #5786)
            assert_one(cursor, "UPDATE test SET v2 = 'bar', v1 = 3 WHERE k = 0 IF v1 = 2", [True])
            assert_one(cursor, "UPDATE test SET v2 = 'bar', v1 = 3 WHERE k = 0 IF EXISTS", [True])
            assert_one(cursor, "SELECT * FROM test", [0, 3, 'bar', None])

            # Shouldn't apply, only one condition is ok
            assert_one(cursor, "UPDATE test SET v1 = 5, v2 = 'foobar' WHERE k = 0 IF v1 = 3 AND v2 = 'foo'", [False, 3, 'bar'])
            assert_one(cursor, "SELECT * FROM test", [0, 3, 'bar', None])

            # Should apply
            assert_one(cursor, "UPDATE test SET v1 = 5, v2 = 'foobar' WHERE k = 0 IF v1 = 3 AND v2 = 'bar'", [True])
            assert_one(cursor, "SELECT * FROM test", [0, 5, 'foobar', None])

            # Shouldn't apply
            assert_one(cursor, "DELETE v2 FROM test WHERE k = 0 IF v1 = 3", [False, 5])
            assert_one(cursor, "SELECT * FROM test", [0, 5, 'foobar', None])

            # Shouldn't apply
            assert_one(cursor, "DELETE v2 FROM test WHERE k = 0 IF v1 = null", [False, 5])
            assert_one(cursor, "SELECT * FROM test", [0, 5, 'foobar', None])

            # Should apply
            assert_one(cursor, "DELETE v2 FROM test WHERE k = 0 IF v1 = 5", [True])
            assert_one(cursor, "SELECT * FROM test", [0, 5, None, None])

            # Shouln't apply
            assert_one(cursor, "DELETE v1 FROM test WHERE k = 0 IF v3 = 4", [False, None])

            # Should apply
            assert_one(cursor, "DELETE v1 FROM test WHERE k = 0 IF v3 = null", [True])
            assert_one(cursor, "SELECT * FROM test", [0, None, None, None])

            # Should apply
            assert_one(cursor, "DELETE FROM test WHERE k = 0 IF v1 = null", [True])
            assert_none(cursor, "SELECT * FROM test")

            # Shouldn't apply
            assert_one(cursor, "UPDATE test SET v1 = 3, v2 = 'bar' WHERE k = 0 IF EXISTS", [False])

            if self.get_version() > "2.1.1":
                # Should apply
                assert_one(cursor, "DELETE FROM test WHERE k = 0 IF v1 IN (null)", [True])

    @since('2.1.1')
    def non_eq_conditional_update_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int PRIMARY KEY,
                v1 int,
                v2 text,
                v3 int
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            # non-EQ conditions
            cursor.execute("INSERT INTO test (k, v1, v2) VALUES (0, 2, 'foo')")
            assert_one(cursor, "UPDATE test SET v2 = 'bar' WHERE k = 0 IF v1 < 3", [True])
            assert_one(cursor, "UPDATE test SET v2 = 'bar' WHERE k = 0 IF v1 <= 3", [True])
            assert_one(cursor, "UPDATE test SET v2 = 'bar' WHERE k = 0 IF v1 > 1", [True])
            assert_one(cursor, "UPDATE test SET v2 = 'bar' WHERE k = 0 IF v1 >= 1", [True])
            assert_one(cursor, "UPDATE test SET v2 = 'bar' WHERE k = 0 IF v1 != 1", [True])
            assert_one(cursor, "UPDATE test SET v2 = 'bar' WHERE k = 0 IF v1 != 2", [False, 2])
            assert_one(cursor, "UPDATE test SET v2 = 'bar' WHERE k = 0 IF v1 IN (0, 1, 2)", [True])
            assert_one(cursor, "UPDATE test SET v2 = 'bar' WHERE k = 0 IF v1 IN (142, 276)", [False, 2])
            assert_one(cursor, "UPDATE test SET v2 = 'bar' WHERE k = 0 IF v1 IN ()", [False, 2])

    def conditional_delete_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int PRIMARY KEY,
                v1 int,
            )
        """)

        # static columns
        cursor.execute("""
            CREATE TABLE test2 (
                k text,
                s text static,
                i int,
                v text,
                PRIMARY KEY (k, i)
            )""")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")
            cursor.execute("TRUNCATE test2")

            assert_one(cursor, "DELETE FROM test WHERE k=1 IF EXISTS", [False])

            cursor.execute("INSERT INTO test (k, v1) VALUES (1, 2)")
            assert_one(cursor, "DELETE FROM test WHERE k=1 IF EXISTS", [True])
            assert_none(cursor, "SELECT * FROM test WHERE k=1")
            assert_one(cursor, "DELETE FROM test WHERE k=1 IF EXISTS", [False])

            cursor.execute("UPDATE test USING TTL 1 SET v1=2 WHERE k=1")
            time.sleep(1.5)
            assert_one(cursor, "DELETE FROM test WHERE k=1 IF EXISTS", [False])
            assert_none(cursor, "SELECT * FROM test WHERE k=1")

            cursor.execute("INSERT INTO test (k, v1) VALUES (2, 2) USING TTL 1")
            time.sleep(1.5)
            assert_one(cursor, "DELETE FROM test WHERE k=2 IF EXISTS", [False])
            assert_none(cursor, "SELECT * FROM test WHERE k=2")

            cursor.execute("INSERT INTO test (k, v1) VALUES (3, 2)")
            assert_one(cursor, "DELETE v1 FROM test WHERE k=3 IF EXISTS", [True])
            assert_one(cursor, "SELECT * FROM test WHERE k=3", [3, None])
            assert_one(cursor, "DELETE v1 FROM test WHERE k=3 IF EXISTS", [True])
            assert_one(cursor, "DELETE FROM test WHERE k=3 IF EXISTS", [True])

            cursor.execute("INSERT INTO test2 (k, s, i, v) VALUES ('k', 's', 0, 'v')")
            assert_one(cursor, "DELETE v FROM test2 WHERE k='k' AND i=0 IF EXISTS", [True])
            assert_one(cursor, "DELETE FROM test2 WHERE k='k' AND i=0 IF EXISTS", [True])
            assert_one(cursor, "DELETE v FROM test2 WHERE k='k' AND i=0 IF EXISTS", [False])
            assert_one(cursor, "DELETE FROM test2 WHERE k='k' AND i=0 IF EXISTS", [False])

            # CASSANDRA-6430
            v = self.get_version()
            if v >= "2.1.1" or v < "2.1" and v >= "2.0.11":
                assert_invalid(cursor, "DELETE FROM test2 WHERE k = 'k' IF EXISTS")
                assert_invalid(cursor, "DELETE FROM test2 WHERE k = 'k' IF v = 'foo'")
                assert_invalid(cursor, "DELETE FROM test2 WHERE i = 0 IF EXISTS")
                assert_invalid(cursor, "DELETE FROM test2 WHERE k = 0 AND i > 0 IF EXISTS")
                assert_invalid(cursor, "DELETE FROM test2 WHERE k = 0 AND i > 0 IF v = 'foo'")

    @freshCluster()
    def range_key_ordered_test(self):
        cursor = self.prepare(ordered=True)

        cursor.execute("CREATE TABLE test ( k int PRIMARY KEY)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test(k) VALUES (-1)")
            cursor.execute("INSERT INTO test(k) VALUES ( 0)")
            cursor.execute("INSERT INTO test(k) VALUES ( 1)")

            assert_all(cursor, "SELECT * FROM test", [[0], [1], [-1]])
            assert_invalid(cursor, "SELECT * FROM test WHERE k >= -1 AND k < 1;")

    def select_with_alias_test(self):
        cursor = self.prepare()
        cursor.execute('CREATE TABLE users (id int PRIMARY KEY, name text)')

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE users")

            for id in range(0, 5):
                cursor.execute("INSERT INTO users (id, name) VALUES (%d, 'name%d') USING TTL 10 AND TIMESTAMP 0" % (id, id))

            # test aliasing count(*)
            res = cursor.execute('SELECT count(*) AS user_count FROM users')
            self.assertEqual('user_count', res[0]._fields[0])
            self.assertEqual(5, res[0].user_count)

            # test aliasing regular value
            res = cursor.execute('SELECT name AS user_name FROM users WHERE id = 0')
            self.assertEqual('user_name', res[0]._fields[0])
            self.assertEqual('name0', res[0].user_name)

            # test aliasing writetime
            res = cursor.execute('SELECT writeTime(name) AS name_writetime FROM users WHERE id = 0')
            self.assertEqual('name_writetime', res[0]._fields[0])
            self.assertEqual(0, res[0].name_writetime)

            # test aliasing ttl
            res = cursor.execute('SELECT ttl(name) AS name_ttl FROM users WHERE id = 0')
            self.assertEqual('name_ttl', res[0]._fields[0])
            self.assertIn(res[0].name_ttl, (9, 10))

            # test aliasing a regular function
            res = cursor.execute('SELECT intAsBlob(id) AS id_blob FROM users WHERE id = 0')
            self.assertEqual('id_blob', res[0]._fields[0])
            self.assertEqual('\x00\x00\x00\x00', res[0].id_blob)

            # test that select throws a meaningful exception for aliases in where clause
            assert_invalid(cursor, 'SELECT id AS user_id, name AS user_name FROM users WHERE user_id = 0', matching="Aliases aren't allowed in the where clause")

            # test that select throws a meaningful exception for aliases in order by clause
            assert_invalid(cursor, 'SELECT id AS user_id, name AS user_name FROM users WHERE id IN (0) ORDER BY user_name', matching="Aliases are not allowed in order by clause")

    def nonpure_function_collection_test(self):
        """ Test for bug #5795 """

        cursor = self.prepare()
        cursor.execute("CREATE TABLE test (k int PRIMARY KEY, v list<timeuuid>)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            # we just want to make sure this doesn't throw
            cursor.execute("INSERT INTO test(k, v) VALUES (0, [now()])")

    def empty_in_test(self):
        cursor = self.prepare()
        cursor.execute("CREATE TABLE test (k1 int, k2 int, v int, PRIMARY KEY (k1, k2))")
        # Same test, but for compact
        cursor.execute("CREATE TABLE test_compact (k1 int, k2 int, v int, PRIMARY KEY (k1, k2)) WITH COMPACT STORAGE")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")
            cursor.execute("TRUNCATE test_compact")

            def fill(table):
                for i in range(0, 2):
                    for j in range(0, 2):
                        cursor.execute("INSERT INTO %s (k1, k2, v) VALUES (%d, %d, %d)" % (table, i, j, i + j))

            def assert_nothing_changed(table):
                res = cursor.execute("SELECT * FROM %s" % table)  # make sure nothing got removed
                self.assertEqual([[0, 0, 0], [0, 1, 1], [1, 0, 1], [1, 1, 2]], rows_to_list(sorted(res)))

            # Inserts a few rows to make sure we don't actually query something
            fill("test")

            # Test empty IN () in SELECT
            assert_none(cursor, "SELECT v FROM test WHERE k1 IN ()")
            assert_none(cursor, "SELECT v FROM test WHERE k1 = 0 AND k2 IN ()")

            # Test empty IN () in DELETE
            cursor.execute("DELETE FROM test WHERE k1 IN ()")
            assert_nothing_changed("test")

            # Test empty IN () in UPDATE
            cursor.execute("UPDATE test SET v = 3 WHERE k1 IN () AND k2 = 2")
            assert_nothing_changed("test")

            fill("test_compact")

            assert_none(cursor, "SELECT v FROM test_compact WHERE k1 IN ()")
            assert_none(cursor, "SELECT v FROM test_compact WHERE k1 = 0 AND k2 IN ()")

            # Test empty IN () in DELETE
            cursor.execute("DELETE FROM test_compact WHERE k1 IN ()")
            assert_nothing_changed("test_compact")

            # Test empty IN () in UPDATE
            cursor.execute("UPDATE test_compact SET v = 3 WHERE k1 IN () AND k2 = 2")
            assert_nothing_changed("test_compact")

    def collection_flush_test(self):
        """ Test for 5805 bug """
        cursor = self.prepare()

        cursor.execute("CREATE TABLE test (k int PRIMARY KEY, s set<int>)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test(k, s) VALUES (1, {1})")
            self.cluster.flush()
            cursor.execute("INSERT INTO test(k, s) VALUES (1, {2})")
            self.cluster.flush()

            assert_one(cursor, "SELECT * FROM test", [1, set([2])])

    def select_distinct_test(self):
        cursor = self.prepare(ordered=True)

        # Test a regular (CQL3) table.
        cursor.execute('CREATE TABLE regular (pk0 int, pk1 int, ck0 int, val int, PRIMARY KEY((pk0, pk1), ck0))')
        # Test a 'compact storage' table.
        cursor.execute('CREATE TABLE compact (pk0 int, pk1 int, val int, PRIMARY KEY((pk0, pk1))) WITH COMPACT STORAGE')
        # Test a 'wide row' thrift table.
        cursor.execute('CREATE TABLE wide (pk int, name text, val int, PRIMARY KEY(pk, name)) WITH COMPACT STORAGE')

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE regular")
            cursor.execute("TRUNCATE compact")
            cursor.execute("TRUNCATE wide")

            for i in xrange(0, 3):
                cursor.execute('INSERT INTO regular (pk0, pk1, ck0, val) VALUES (%d, %d, 0, 0)' % (i, i))
                cursor.execute('INSERT INTO regular (pk0, pk1, ck0, val) VALUES (%d, %d, 1, 1)' % (i, i))

            res = cursor.execute('SELECT DISTINCT pk0, pk1 FROM regular LIMIT 1')
            self.assertEqual([[0, 0]], rows_to_list(res))

            res = cursor.execute('SELECT DISTINCT pk0, pk1 FROM regular LIMIT 3')
            self.assertEqual([[0, 0], [1, 1], [2, 2]], rows_to_list(sorted(res)))

            for i in xrange(0, 3):
                cursor.execute('INSERT INTO compact (pk0, pk1, val) VALUES (%d, %d, %d)' % (i, i, i))

            res = cursor.execute('SELECT DISTINCT pk0, pk1 FROM compact LIMIT 1')
            self.assertEqual([[0, 0]], rows_to_list(res))

            res = cursor.execute('SELECT DISTINCT pk0, pk1 FROM compact LIMIT 3')
            self.assertEqual([[0, 0], [1, 1], [2, 2]], rows_to_list(sorted(res)))

            for i in xrange(0, 3):
                cursor.execute("INSERT INTO wide (pk, name, val) VALUES (%d, 'name0', 0)" % i)
                cursor.execute("INSERT INTO wide (pk, name, val) VALUES (%d, 'name1', 1)" % i)

            res = cursor.execute('SELECT DISTINCT pk FROM wide LIMIT 1')
            self.assertEqual([[0]], rows_to_list(res))

            res = cursor.execute('SELECT DISTINCT pk FROM wide LIMIT 3')
            self.assertEqual([[0], [1], [2]], rows_to_list(sorted(res)))

            # Test selection validation.
            assert_invalid(cursor, 'SELECT DISTINCT pk0 FROM regular', matching="queries must request all the partition key columns")
            assert_invalid(cursor, 'SELECT DISTINCT pk0, pk1, ck0 FROM regular', matching="queries must only request partition key columns")

    def select_distinct_with_deletions_test(self):
        cursor = self.prepare()
        cursor.execute('CREATE TABLE t1 (k int PRIMARY KEY, c int, v int)')

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE t1")

            for i in range(10):
                cursor.execute('INSERT INTO t1 (k, c, v) VALUES (%d, %d, %d)' % (i, i, i))

            rows = list(cursor.execute('SELECT DISTINCT k FROM t1'))
            self.assertEqual(10, len(rows))
            key_to_delete = rows[3].k

            cursor.execute('DELETE FROM t1 WHERE k=%d' % (key_to_delete,))
            rows = list(cursor.execute('SELECT DISTINCT k FROM t1'))
            self.assertEqual(9, len(rows))

            rows = list(cursor.execute('SELECT DISTINCT k FROM t1 LIMIT 5'))
            self.assertEqual(5, len(rows))

            cursor.default_fetch_size = 5
            rows = list(cursor.execute('SELECT DISTINCT k FROM t1'))
            self.assertEqual(9, len(rows))

    def function_with_null_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int PRIMARY KEY,
                t timeuuid,
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test(k) VALUES (0)")
            assert_one(cursor, "SELECT dateOf(t) FROM test WHERE k=0", [None])

    @freshCluster()
    def cas_simple_test(self):
        # cursor = self.prepare(nodes=3, rf=3)
        cursor = self.prepare()

        cursor.execute("CREATE TABLE tkns (tkn int, consumed boolean, PRIMARY KEY (tkn));")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE tkns")

            for i in range(1, 10):
                query = SimpleStatement("INSERT INTO tkns (tkn, consumed) VALUES (%i,FALSE);" % i, consistency_level=ConsistencyLevel.QUORUM)
                cursor.execute(query)
                assert_one(cursor, "UPDATE tkns SET consumed = TRUE WHERE tkn = %i IF consumed = FALSE;" % i, [True], cl=ConsistencyLevel.QUORUM)
                assert_one(cursor, "UPDATE tkns SET consumed = TRUE WHERE tkn = %i IF consumed = FALSE;" % i, [False, True], cl=ConsistencyLevel.QUORUM)

    def bug_6050_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int PRIMARY KEY,
                a int,
                b int
            )
        """)

        cursor.execute("CREATE INDEX ON test(a)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            assert_invalid(cursor, "SELECT * FROM test WHERE a = 3 AND b IN (1, 3)")

    def bug_6069_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int PRIMARY KEY,
                s set<int>
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            assert_one(cursor, "INSERT INTO test(k, s) VALUES (0, {1, 2, 3}) IF NOT EXISTS", [True])
            assert_one(cursor, "SELECT * FROM test", [0, {1, 2, 3}])

    def bug_6115_test(self):
        cursor = self.prepare()

        cursor.execute("CREATE TABLE test (k int, v int, PRIMARY KEY (k, v))")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (k, v) VALUES (0, 1)")
            cursor.execute("BEGIN BATCH DELETE FROM test WHERE k=0 AND v=1; INSERT INTO test (k, v) VALUES (0, 2); APPLY BATCH")

            assert_one(cursor, "SELECT * FROM test", [0, 2])

    def column_name_validation_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k text,
                c int,
                v timeuuid,
                PRIMARY KEY (k, c)
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            assert_invalid(cursor, "INSERT INTO test(k, c) VALUES ('', 0)")

            # Insert a value that don't fit 'int'
            assert_invalid(cursor, "INSERT INTO test(k, c) VALUES (0, 10000000000)")

            # Insert a non-version 1 uuid
            assert_invalid(cursor, "INSERT INTO test(k, c, v) VALUES (0, 0, 550e8400-e29b-41d4-a716-446655440000)")

    def user_types_test(self):
        cursor = self.prepare()

        userID_1 = uuid4()
        stmt = """
              CREATE TYPE address (
              street text,
              city text,
              zip_code int,
              phones set<text>
              )
           """
        cursor.execute(stmt)

        stmt = """
              CREATE TYPE fullname (
               firstname text,
               lastname text
              )
           """
        cursor.execute(stmt)

        stmt = """
              CREATE TABLE users (
               id uuid PRIMARY KEY,
               name frozen<fullname>,
               addresses map<text, frozen<address>>
              )
           """
        cursor.execute(stmt)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE users")

            stmt = """
                  INSERT INTO users (id, name)
                  VALUES ({id}, {{ firstname: 'Paul', lastname: 'smith'}});
               """.format(id=userID_1)
            cursor.execute(stmt)

            stmt = """
                  SELECT name.firstname FROM users WHERE id = {id}
            """.format(id=userID_1)
            res = cursor.execute(stmt)
            self.assertEqual(['Paul'], list(res[0]))

            stmt = """
                  UPDATE users
                  SET addresses = addresses + {{ 'home': {{ street: '...', city: 'SF', zip_code: 94102, phones: {{}} }} }}
                  WHERE id={id};
               """.format(id=userID_1)
            cursor.execute(stmt)

            stmt = """
                  SELECT addresses FROM users WHERE id = {id}
            """.format(id=userID_1)
            res = cursor.execute(stmt)
            # TODO: deserialize the value here and check it's right.

    def more_user_types_test(self):
        """ user type test that does a little more nesting"""

        cursor = self.prepare()

        cursor.execute("""
            CREATE TYPE type1 (
                s set<text>,
                m map<text, text>,
                l list<text>
            )
        """)

        cursor.execute("""
            CREATE TYPE type2 (
                s set<frozen<type1>>,
            )
        """)

        cursor.execute("""
            CREATE TABLE test (id int PRIMARY KEY, val frozen<type2>)
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test(id, val) VALUES (0, { s : {{ s : {'foo', 'bar'}, m : { 'foo' : 'bar' }, l : ['foo', 'bar']} }})")

            # TODO: check result once we have an easy way to do it. For now we just check it doesn't crash
            cursor.execute("SELECT * FROM test")

    def bug_6327_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int,
                v int,
                PRIMARY KEY (k, v)
            )
        """)

        cursor.execute("""
            CREATE TABLE test2 (
                k int,
                v int,
                c1 int,
                c2 int,
                PRIMARY KEY (k, v)
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (k, v) VALUES (0, 0)")
            self.cluster.flush()
            assert_one(cursor, "SELECT v FROM test WHERE k=0 AND v IN (1, 0)", [0])
            assert_one(cursor, "SELECT v FROM test WHERE v IN (1, 0) ALLOW FILTERING", [0])

            cursor.execute("INSERT INTO test2 (k, v) VALUES (0, 0)")
            self.cluster.flush()
            assert_one(cursor, "SELECT v FROM test2 WHERE k=0 AND v IN (1, 0)", [0])
            assert_one(cursor, "SELECT v FROM test2 WHERE v IN (1, 0) ALLOW FILTERING", [0])

            cursor.execute("DELETE FROM test2 WHERE k = 0")
            cursor.execute("UPDATE test2 SET c2 = 1 WHERE k = 0 AND v = 0")
            assert_one(cursor, "SELECT v FROM test2 WHERE k=0 AND v IN (1, 0)", [0])
            cursor.execute("DELETE c2 FROM test2 WHERE k = 0 AND v = 0")
            assert_none(cursor, "SELECT v FROM test2 WHERE k=0 AND v IN (1, 0)")
            assert_none(cursor, "SELECT v FROM test2 WHERE v IN (1, 0) ALLOW FILTERING")

    def large_count_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int,
                v int,
                PRIMARY KEY (k)
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.default_fetch_size = 10000
            # We know we page at 10K, so test counting just before, at 10K, just after and
            # a bit after that.
            insert_statement = cursor.prepare("INSERT INTO test(k) VALUES (?)")
            execute_concurrent_with_args(cursor, insert_statement, [(i,) for i in range(1, 10000)])

            assert_one(cursor, "SELECT COUNT(*) FROM test", [9999])

            cursor.execute(insert_statement, (10000,))
            assert_one(cursor, "SELECT COUNT(*) FROM test", [10000])

            cursor.execute(insert_statement, (10001,))
            assert_one(cursor, "SELECT COUNT(*) FROM test", [10001])

            execute_concurrent_with_args(cursor, insert_statement, [(i,) for i in range(10002, 15001)])
            assert_one(cursor, "SELECT COUNT(*) FROM test", [15000])

    def collection_indexing_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int,
                v int,
                l list<int>,
                s set<text>,
                m map<text, int>,
                PRIMARY KEY (k, v)
            )
        """)

        cursor.execute("CREATE INDEX ON test(l)")
        cursor.execute("CREATE INDEX ON test(s)")
        cursor.execute("CREATE INDEX ON test(m)")

        time.sleep(5.0)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (k, v, l, s, m) VALUES (0, 0, [1, 2],    {'a'},      {'a' : 1})")
            cursor.execute("INSERT INTO test (k, v, l, s, m) VALUES (0, 1, [3, 4],    {'b', 'c'}, {'a' : 1, 'b' : 2})")
            cursor.execute("INSERT INTO test (k, v, l, s, m) VALUES (0, 2, [1],       {'a', 'c'}, {'c' : 3})")
            cursor.execute("INSERT INTO test (k, v, l, s, m) VALUES (1, 0, [1, 2, 4], {},         {'b' : 1})")
            cursor.execute("INSERT INTO test (k, v, l, s, m) VALUES (1, 1, [4, 5],    {'d'},      {'a' : 1, 'b' : 3})")

            # lists
            assert_all(cursor, "SELECT k, v FROM test WHERE l CONTAINS 1", [[1, 0], [0, 0], [0, 2]])
            assert_all(cursor, "SELECT k, v FROM test WHERE k = 0 AND l CONTAINS 1", [[0, 0], [0, 2]])
            assert_all(cursor, "SELECT k, v FROM test WHERE l CONTAINS 2", [[1, 0], [0, 0]])
            assert_none(cursor, "SELECT k, v FROM test WHERE l CONTAINS 6")

            # sets
            assert_all(cursor, "SELECT k, v FROM test WHERE s CONTAINS 'a'", [[0, 0], [0, 2]])
            assert_all(cursor, "SELECT k, v FROM test WHERE k = 0 AND s CONTAINS 'a'", [[0, 0], [0, 2]])
            assert_all(cursor, "SELECT k, v FROM test WHERE s CONTAINS 'd'", [[1, 1]])
            assert_none(cursor, "SELECT k, v FROM test  WHERE s CONTAINS 'e'")

            # maps
            assert_all(cursor, "SELECT k, v FROM test WHERE m CONTAINS 1", [[1, 0], [1, 1], [0, 0], [0, 1]])
            assert_all(cursor, "SELECT k, v FROM test WHERE k = 0 AND m CONTAINS 1", [[0, 0], [0, 1]])
            assert_all(cursor, "SELECT k, v FROM test WHERE m CONTAINS 2", [[0, 1]])
            assert_none(cursor, "SELECT k, v FROM test  WHERE m CONTAINS 4")

    def map_keys_indexing(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int,
                v int,
                m map<text, int>,
                PRIMARY KEY (k, v)
            )
        """)

        cursor.execute("CREATE INDEX ON test(keys(m))")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (k, v, m) VALUES (0, 0, {'a' : 1})")
            cursor.execute("INSERT INTO test (k, v, m) VALUES (0, 1, {'a' : 1, 'b' : 2})")
            cursor.execute("INSERT INTO test (k, v, m) VALUES (0, 2, {'c' : 3})")
            cursor.execute("INSERT INTO test (k, v, m) VALUES (1, 0, {'b' : 1})")
            cursor.execute("INSERT INTO test (k, v, m) VALUES (1, 1, {'a' : 1, 'b' : 3})")

            # maps
            assert_all(cursor, "SELECT k, v FROM test WHERE m CONTAINS KEY 'a'", [[1, 1], [0, 0], [0, 1]])
            assert_all(cursor, "SELECT k, v FROM test WHERE k = 0 AND m CONTAINS KEY 'a'", [[0, 0], [0, 1]])
            assert_all(cursor, "SELECT k, v FROM test WHERE m CONTAINS KEY 'c'", [[0, 2]])
            assert_none(cursor, "SELECT k, v FROM test  WHERE m CONTAINS KEY 'd'")

            # we're not allowed to create a value index if we already have a key one
            assert_invalid(cursor, "CREATE INDEX ON test(m)")

    def nan_infinity_test(self):
        cursor = self.prepare()

        cursor.execute("CREATE TABLE test (f float PRIMARY KEY)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test(f) VALUES (NaN)")
            cursor.execute("INSERT INTO test(f) VALUES (-NaN)")
            cursor.execute("INSERT INTO test(f) VALUES (Infinity)")
            cursor.execute("INSERT INTO test(f) VALUES (-Infinity)")

            selected = rows_to_list(cursor.execute("SELECT * FROM test"))

            # selected should be [[nan], [inf], [-inf]],
            # but assert element-wise because NaN != NaN
            assert len(selected) == 3
            assert len(selected[0]) == 1
            assert math.isnan(selected[0][0])
            assert selected[1] == [float("inf")]
            assert selected[2] == [float("-inf")]

    def static_columns_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int,
                p int,
                s int static,
                v int,
                PRIMARY KEY (k, p)
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test(k, s) VALUES (0, 42)")

            assert_one(cursor, "SELECT * FROM test", [0, None, 42, None])

            # Check that writetime works (#7081) -- we can't predict the exact value easily so
            # we just check that it's non zero
            row = cursor.execute("SELECT s, writetime(s) FROM test WHERE k=0")
            assert list(row[0])[0] == 42 and list(row[0])[1] > 0, row

            cursor.execute("INSERT INTO test(k, p, s, v) VALUES (0, 0, 12, 0)")
            cursor.execute("INSERT INTO test(k, p, s, v) VALUES (0, 1, 24, 1)")

            # Check the static columns in indeed "static"
            assert_all(cursor, "SELECT * FROM test", [[0, 0, 24, 0], [0, 1, 24, 1]])

            # Check we do correctly get the static column value with a SELECT *, even
            # if we're only slicing part of the partition
            assert_one(cursor, "SELECT * FROM test WHERE k=0 AND p=0", [0, 0, 24, 0])
            assert_one(cursor, "SELECT * FROM test WHERE k=0 AND p=0 ORDER BY p DESC", [0, 0, 24, 0])
            assert_one(cursor, "SELECT * FROM test WHERE k=0 AND p=1", [0, 1, 24, 1])
            assert_one(cursor, "SELECT * FROM test WHERE k=0 AND p=1 ORDER BY p DESC", [0, 1, 24, 1])

            # Test for IN on the clustering key (#6769)
            assert_all(cursor, "SELECT * FROM test WHERE k=0 AND p IN (0, 1)", [[0, 0, 24, 0], [0, 1, 24, 1]])

            # Check things still work if we don't select the static column. We also want
            # this to not request the static columns internally at all, though that part
            # require debugging to assert
            assert_one(cursor, "SELECT p, v FROM test WHERE k=0 AND p=1", [1, 1])

            # Check selecting only a static column with distinct only yield one value
            # (as we only query the static columns)
            assert_one(cursor, "SELECT DISTINCT s FROM test WHERE k=0", [24])
            # But without DISTINCT, we still get one result per row
            assert_all(cursor, "SELECT s FROM test WHERE k=0", [[24], [24]])
            # but that querying other columns does correctly yield the full partition
            assert_all(cursor, "SELECT s, v FROM test WHERE k=0", [[24, 0], [24, 1]])
            assert_one(cursor, "SELECT s, v FROM test WHERE k=0 AND p=1", [24, 1])
            assert_one(cursor, "SELECT p, s FROM test WHERE k=0 AND p=1", [1, 24])
            assert_one(cursor, "SELECT k, p, s FROM test WHERE k=0 AND p=1", [0, 1, 24])

            # Check that deleting a row don't implicitely deletes statics
            cursor.execute("DELETE FROM test WHERE k=0 AND p=0")
            assert_all(cursor, "SELECT * FROM test", [[0, 1, 24, 1]])

            # But that explicitely deleting the static column does remove it
            cursor.execute("DELETE s FROM test WHERE k=0")
            assert_all(cursor, "SELECT * FROM test", [[0, 1, None, 1]])

    def static_columns_cas_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                id int,
                k text,
                version int static,
                v text,
                PRIMARY KEY (id, k)
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            # Test that INSERT IF NOT EXISTS concerns only the static column if no clustering nor regular columns
            # is provided, but concerns the CQL3 row targetted by the clustering columns otherwise
            cursor.execute("INSERT INTO test(id, k, v) VALUES (1, 'foo', 'foo')")
            assert_one(cursor, "INSERT INTO test(id, k, version) VALUES (1, 'foo', 1) IF NOT EXISTS", [False, 1, 'foo', None, 'foo'])
            assert_one(cursor, "INSERT INTO test(id, version) VALUES (1, 1) IF NOT EXISTS", [True])
            assert_one(cursor, "SELECT * FROM test", [1, 'foo', 1, 'foo'])
            cursor.execute("DELETE FROM test WHERE id = 1")

            cursor.execute("INSERT INTO test(id, version) VALUES (0, 0)")

            assert_one(cursor, "UPDATE test SET v='foo', version=1 WHERE id=0 AND k='k1' IF version = 0", [True])
            assert_all(cursor, "SELECT * FROM test", [[0, 'k1', 1, 'foo']])

            assert_one(cursor, "UPDATE test SET v='bar', version=1 WHERE id=0 AND k='k2' IF version = 0", [False, 1])
            assert_all(cursor, "SELECT * FROM test", [[0, 'k1', 1, 'foo']])

            assert_one(cursor, "UPDATE test SET v='bar', version=2 WHERE id=0 AND k='k2' IF version = 1", [True])
            assert_all(cursor, "SELECT * FROM test", [[0, 'k1', 2, 'foo'], [0, 'k2', 2, 'bar']])

            # Testing batches
            assert_one(cursor,
                       """
                         BEGIN BATCH
                           UPDATE test SET v='foobar' WHERE id=0 AND k='k1';
                           UPDATE test SET v='barfoo' WHERE id=0 AND k='k2';
                           UPDATE test SET version=3 WHERE id=0 IF version=1;
                         APPLY BATCH
                       """, [False, 0, None, 2])

            assert_one(cursor,
                       """
                         BEGIN BATCH
                           UPDATE test SET v='foobar' WHERE id=0 AND k='k1';
                           UPDATE test SET v='barfoo' WHERE id=0 AND k='k2';
                           UPDATE test SET version=3 WHERE id=0 IF version=2;
                         APPLY BATCH
                       """, [True])
            assert_all(cursor, "SELECT * FROM test", [[0, 'k1', 3, 'foobar'], [0, 'k2', 3, 'barfoo']])

            assert_all(cursor,
                       """
                         BEGIN BATCH
                           UPDATE test SET version=4 WHERE id=0 IF version=3;
                           UPDATE test SET v='row1' WHERE id=0 AND k='k1' IF v='foo';
                           UPDATE test SET v='row2' WHERE id=0 AND k='k2' IF v='bar';
                         APPLY BATCH
                       """, [[False, 0, 'k1', 3, 'foobar'], [False, 0, 'k2', 3, 'barfoo']])

            assert_one(cursor,
                       """
                         BEGIN BATCH
                           UPDATE test SET version=4 WHERE id=0 IF version=3;
                           UPDATE test SET v='row1' WHERE id=0 AND k='k1' IF v='foobar';
                           UPDATE test SET v='row2' WHERE id=0 AND k='k2' IF v='barfoo';
                         APPLY BATCH
                       """, [True])

            assert_invalid(cursor,
                           """
                             BEGIN BATCH
                               UPDATE test SET version=5 WHERE id=0 IF version=4;
                               UPDATE test SET v='row1' WHERE id=0 AND k='k1';
                               UPDATE test SET v='row2' WHERE id=1 AND k='k2';
                             APPLY BATCH
                           """)

            assert_one(cursor,
                       """
                         BEGIN BATCH
                           INSERT INTO TEST (id, k, v) VALUES(1, 'k1', 'val1') IF NOT EXISTS;
                           INSERT INTO TEST (id, k, v) VALUES(1, 'k2', 'val2') IF NOT EXISTS;
                         APPLY BATCH
                       """, [True])
            assert_all(cursor, "SELECT * FROM test WHERE id=1", [[1, 'k1', None, 'val1'], [1, 'k2', None, 'val2']])

            assert_one(cursor,
                       """
                         BEGIN BATCH
                           INSERT INTO TEST (id, k, v) VALUES(1, 'k2', 'val2') IF NOT EXISTS;
                           INSERT INTO TEST (id, k, v) VALUES(1, 'k3', 'val3') IF NOT EXISTS;
                         APPLY BATCH
                       """, [False, 1, 'k2', None, 'val2'])

            assert_one(cursor,
                       """
                         BEGIN BATCH
                           UPDATE test SET v='newVal' WHERE id=1 AND k='k2' IF v='val0';
                           INSERT INTO TEST (id, k, v) VALUES(1, 'k3', 'val3') IF NOT EXISTS;
                         APPLY BATCH
                       """, [False, 1, 'k2', None, 'val2'])
            assert_all(cursor, "SELECT * FROM test WHERE id=1", [[1, 'k1', None, 'val1'], [1, 'k2', None, 'val2']])

            assert_one(cursor,
                       """
                         BEGIN BATCH
                           UPDATE test SET v='newVal' WHERE id=1 AND k='k2' IF v='val2';
                           INSERT INTO TEST (id, k, v, version) VALUES(1, 'k3', 'val3', 1) IF NOT EXISTS;
                         APPLY BATCH
                       """, [True])
            assert_all(cursor, "SELECT * FROM test WHERE id=1", [[1, 'k1', 1, 'val1'], [1, 'k2', 1, 'newVal'], [1, 'k3', 1, 'val3']])

            assert_one(cursor,
                       """
                         BEGIN BATCH
                           UPDATE test SET v='newVal1' WHERE id=1 AND k='k2' IF v='val2';
                           UPDATE test SET v='newVal2' WHERE id=1 AND k='k2' IF v='val3';
                         APPLY BATCH
                       """, [False, 1, 'k2', 'newVal'])

    def static_columns_with_2i_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int,
                p int,
                s int static,
                v int,
                PRIMARY KEY (k, p)
            )
        """)

        cursor.execute("CREATE INDEX ON test(v)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test(k, p, s, v) VALUES (0, 0, 42, 1)")
            cursor.execute("INSERT INTO test(k, p, v) VALUES (0, 1, 1)")
            cursor.execute("INSERT INTO test(k, p, v) VALUES (0, 2, 2)")

            assert_all(cursor, "SELECT * FROM test WHERE v = 1", [[0, 0, 42, 1], [0, 1, 42, 1]])
            assert_all(cursor, "SELECT p, s FROM test WHERE v = 1", [[0, 42], [1, 42]])
            assert_all(cursor, "SELECT p FROM test WHERE v = 1", [[0], [1]])
            # We don't support that
            assert_invalid(cursor, "SELECT s FROM test WHERE v = 1")

    def static_columns_with_distinct_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int,
                p int,
                s int static,
                PRIMARY KEY (k, p)
            )
        """)

        # additional testing for CASSANRA-8087
        cursor.execute("""
            CREATE TABLE test2 (
                k int,
                c1 int,
                c2 int,
                s1 int static,
                s2 int static,
                PRIMARY KEY (k, c1, c2)
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")
            cursor.execute("TRUNCATE test2")

            cursor.execute("INSERT INTO test (k, p) VALUES (1, 1)")
            cursor.execute("INSERT INTO test (k, p) VALUES (1, 2)")

            assert_all(cursor, "SELECT k, s FROM test", [[1, None], [1, None]])
            assert_one(cursor, "SELECT DISTINCT k, s FROM test", [1, None])
            assert_one(cursor, "SELECT DISTINCT s FROM test WHERE k=1", [None])
            assert_none(cursor, "SELECT DISTINCT s FROM test WHERE k=2")

            cursor.execute("INSERT INTO test (k, p, s) VALUES (2, 1, 3)")
            cursor.execute("INSERT INTO test (k, p) VALUES (2, 2)")

            assert_all(cursor, "SELECT k, s FROM test", [[1, None], [1, None], [2, 3], [2, 3]])
            assert_all(cursor, "SELECT DISTINCT k, s FROM test", [[1, None], [2, 3]])
            assert_one(cursor, "SELECT DISTINCT s FROM test WHERE k=1", [None])
            assert_one(cursor, "SELECT DISTINCT s FROM test WHERE k=2", [3])

            assert_invalid(cursor, "SELECT DISTINCT s FROM test")

            # paging to test for CASSANDRA-8108
            cursor.execute("TRUNCATE test")
            for i in range(10):
                for j in range(10):
                    cursor.execute("INSERT INTO test (k, p, s) VALUES (%s, %s, %s)", (i, j, i))

            cursor.default_fetch_size = 7
            rows = list(cursor.execute("SELECT DISTINCT k, s FROM test"))
            self.assertEqual(range(10), sorted([r[0] for r in rows]))
            self.assertEqual(range(10), sorted([r[1] for r in rows]))

            keys = ",".join(map(str, range(10)))
            rows = list(cursor.execute("SELECT DISTINCT k, s FROM test WHERE k IN (%s)" % (keys,)))
            self.assertEqual(range(10), [r[0] for r in rows])
            self.assertEqual(range(10), [r[1] for r in rows])

            # additional testing for CASSANRA-8087
            for i in range(10):
                for j in range(5):
                    for k in range(5):
                        cursor.execute("INSERT INTO test2 (k, c1, c2, s1, s2) VALUES (%s, %s, %s, %s, %s)", (i, j, k, i, i + 1))

            for fetch_size in (None, 2, 5, 7, 10, 24, 25, 26, 1000):
                cursor.default_fetch_size = fetch_size
                rows = list(cursor.execute("SELECT DISTINCT k, s1 FROM test2"))
                self.assertEqual(range(10), sorted([r[0] for r in rows]))
                self.assertEqual(range(10), sorted([r[1] for r in rows]))

                rows = list(cursor.execute("SELECT DISTINCT k, s2 FROM test2"))
                self.assertEqual(range(10), sorted([r[0] for r in rows]))
                self.assertEqual(range(1, 11), sorted([r[1] for r in rows]))

                rows = list(cursor.execute("SELECT DISTINCT k, s1 FROM test2 LIMIT 10"))
                self.assertEqual(range(10), sorted([r[0] for r in rows]))
                self.assertEqual(range(10), sorted([r[1] for r in rows]))

                keys = ",".join(map(str, range(10)))
                rows = list(cursor.execute("SELECT DISTINCT k, s1 FROM test2 WHERE k IN (%s)" % (keys,)))
                self.assertEqual(range(10), [r[0] for r in rows])
                self.assertEqual(range(10), [r[1] for r in rows])

                keys = ",".join(map(str, range(10)))
                rows = list(cursor.execute("SELECT DISTINCT k, s2 FROM test2 WHERE k IN (%s)" % (keys,)))
                self.assertEqual(range(10), [r[0] for r in rows])
                self.assertEqual(range(1, 11), [r[1] for r in rows])

                keys = ",".join(map(str, range(10)))
                rows = list(cursor.execute("SELECT DISTINCT k, s1 FROM test2 WHERE k IN (%s) LIMIT 10" % (keys,)))
                self.assertEqual(range(10), sorted([r[0] for r in rows]))
                self.assertEqual(range(10), sorted([r[1] for r in rows]))

    def select_count_paging_test(self):
        """ Test for the #6579 'select count' paging bug """

        cursor = self.prepare()
        cursor.execute("create table test(field1 text, field2 timeuuid, field3 boolean, primary key(field1, field2));")
        cursor.execute("create index test_index on test(field3);")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("insert into test(field1, field2, field3) values ('hola', now(), false);")
            cursor.execute("insert into test(field1, field2, field3) values ('hola', now(), false);")

            # the result depends on which node we're connected to, see CASSANDRA-8216
            if self.get_node_version(is_upgraded) >= '2.2':
                # the coordinator is the upgraded 2.2+ node
                assert_one(cursor, "select count(*) from test where field3 = false limit 1;", [2])
            else:
                # the coordinator is the not-upgraded 2.1 node
                assert_one(cursor, "select count(*) from test where field3 = false limit 1;", [1])

    def cas_and_ttl_test(self):
        cursor = self.prepare()
        cursor.execute("CREATE TABLE test (k int PRIMARY KEY, v int, lock boolean)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (k, v, lock) VALUES (0, 0, false)")
            cursor.execute("UPDATE test USING TTL 1 SET lock=true WHERE k=0")
            time.sleep(2)
            assert_one(cursor, "UPDATE test SET v = 1 WHERE k = 0 IF lock = null", [True])

    def tuple_notation_test(self):
        """ Test the syntax introduced by #4851 """
        cursor = self.prepare()

        cursor.execute("CREATE TABLE test (k int, v1 int, v2 int, v3 int, PRIMARY KEY (k, v1, v2, v3))")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            for i in range(0, 2):
                for j in range(0, 2):
                    for k in range(0, 2):
                        cursor.execute("INSERT INTO test(k, v1, v2, v3) VALUES (0, %d, %d, %d)" % (i, j, k))

            assert_all(cursor, "SELECT v1, v2, v3 FROM test WHERE k = 0", [[0, 0, 0],
                                                                           [0, 0, 1],
                                                                           [0, 1, 0],
                                                                           [0, 1, 1],
                                                                           [1, 0, 0],
                                                                           [1, 0, 1],
                                                                           [1, 1, 0],
                                                                           [1, 1, 1]])

            assert_all(cursor, "SELECT v1, v2, v3 FROM test WHERE k = 0 AND (v1, v2, v3) >= (1, 0, 1)", [[1, 0, 1], [1, 1, 0], [1, 1, 1]])
            assert_all(cursor, "SELECT v1, v2, v3 FROM test WHERE k = 0 AND (v1, v2) >= (1, 1)", [[1, 1, 0], [1, 1, 1]])
            assert_all(cursor, "SELECT v1, v2, v3 FROM test WHERE k = 0 AND (v1, v2) > (0, 1) AND (v1, v2, v3) <= (1, 1, 0)", [[1, 0, 0], [1, 0, 1], [1, 1, 0]])

            assert_invalid(cursor, "SELECT v1, v2, v3 FROM test WHERE k = 0 AND (v1, v3) > (1, 0)")

    @since('2.0', max_version='2.2.X')
    def test_v2_protocol_IN_with_tuples(self):
        """
        @jira_ticket CASSANDRA-8062
        """
        cursor = self.prepare(protocol_version=2)
        cursor.execute("CREATE TABLE test (k int, c1 int, c2 text, PRIMARY KEY (k, c1, c2))")

        for version in self.get_node_versions():
            if version >= '3.0':
                raise SkipTest('version {} not compatible with protocol version 2'.format(version))

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))

            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (k, c1, c2) VALUES (0, 0, 'a')")
            cursor.execute("INSERT INTO test (k, c1, c2) VALUES (0, 0, 'b')")
            cursor.execute("INSERT INTO test (k, c1, c2) VALUES (0, 0, 'c')")

            p = cursor.prepare("SELECT * FROM test WHERE k=? AND (c1, c2) IN ?")
            rows = cursor.execute(p, (0, [(0, 'b'), (0, 'c')]))
            self.assertEqual(2, len(rows))
            self.assertEqual((0, 0, 'b'), rows[0])
            self.assertEqual((0, 0, 'c'), rows[1])

    def in_with_desc_order_test(self):
        cursor = self.prepare()

        cursor.execute("CREATE TABLE test (k int, c1 int, c2 int, PRIMARY KEY (k, c1, c2))")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test(k, c1, c2) VALUES (0, 0, 0)")
            cursor.execute("INSERT INTO test(k, c1, c2) VALUES (0, 0, 1)")
            cursor.execute("INSERT INTO test(k, c1, c2) VALUES (0, 0, 2)")

            assert_all(cursor, "SELECT * FROM test WHERE k=0 AND c1 = 0 AND c2 IN (0, 2)", [[0, 0, 0], [0, 0, 2]])
            assert_all(cursor, "SELECT * FROM test WHERE k=0 AND c1 = 0 AND c2 IN (2, 0)", [[0, 0, 0], [0, 0, 2]])
            assert_all(cursor, "SELECT * FROM test WHERE k=0 AND c1 = 0 AND c2 IN (2, 0) ORDER BY c1 ASC", [[0, 0, 0], [0, 0, 2]])
            assert_all(cursor, "SELECT * FROM test WHERE k=0 AND c1 = 0 AND c2 IN (2, 0) ORDER BY c1 DESC", [[0, 0, 2], [0, 0, 0]])
            assert_all(cursor, "SELECT * FROM test WHERE k=0 AND c1 = 0 AND c2 IN (0, 2) ORDER BY c1 ASC", [[0, 0, 0], [0, 0, 2]])
            assert_all(cursor, "SELECT * FROM test WHERE k=0 AND c1 = 0 AND c2 IN (0, 2) ORDER BY c1 DESC", [[0, 0, 2], [0, 0, 0]])

    def in_order_by_without_selecting_test(self):
        """ Test that columns don't need to be selected for ORDER BY when there is a IN (#4911) """

        cursor = self.prepare()
        cursor.execute("CREATE TABLE test (k int, c1 int, c2 int, v int, PRIMARY KEY (k, c1, c2))")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")
            cursor.default_fetch_size = None

            cursor.execute("INSERT INTO test(k, c1, c2, v) VALUES (0, 0, 0, 0)")
            cursor.execute("INSERT INTO test(k, c1, c2, v) VALUES (0, 0, 1, 1)")
            cursor.execute("INSERT INTO test(k, c1, c2, v) VALUES (0, 0, 2, 2)")
            cursor.execute("INSERT INTO test(k, c1, c2, v) VALUES (1, 1, 0, 3)")
            cursor.execute("INSERT INTO test(k, c1, c2, v) VALUES (1, 1, 1, 4)")
            cursor.execute("INSERT INTO test(k, c1, c2, v) VALUES (1, 1, 2, 5)")

            assert_all(cursor, "SELECT * FROM test WHERE k=0 AND c1 = 0 AND c2 IN (2, 0)", [[0, 0, 0, 0], [0, 0, 2, 2]])
            assert_all(cursor, "SELECT * FROM test WHERE k=0 AND c1 = 0 AND c2 IN (2, 0) ORDER BY c1 ASC, c2 ASC", [[0, 0, 0, 0], [0, 0, 2, 2]])

            # check that we don't need to select the column on which we order
            assert_all(cursor, "SELECT v FROM test WHERE k=0 AND c1 = 0 AND c2 IN (2, 0)", [[0], [2]])
            assert_all(cursor, "SELECT v FROM test WHERE k=0 AND c1 = 0 AND c2 IN (2, 0) ORDER BY c1 ASC", [[0], [2]])
            assert_all(cursor, "SELECT v FROM test WHERE k=0 AND c1 = 0 AND c2 IN (2, 0) ORDER BY c1 DESC", [[2], [0]])
            if self.get_node_version(is_upgraded) >= '2.2':
                # the coordinator is the upgraded 2.2+ node
                assert_all(cursor, "SELECT v FROM test WHERE k IN (1, 0)", [[0], [1], [2], [3], [4], [5]])
            else:
                # the coordinator is the non-upgraded 2.1 node
                assert_all(cursor, "SELECT v FROM test WHERE k IN (1, 0)", [[3], [4], [5], [0], [1], [2]])
            assert_all(cursor, "SELECT v FROM test WHERE k IN (1, 0) ORDER BY c1 ASC", [[0], [1], [2], [3], [4], [5]])

            # we should also be able to use functions in the select clause (additional test for CASSANDRA-8286)
            results = list(cursor.execute("SELECT writetime(v) FROM test WHERE k IN (1, 0) ORDER BY c1 ASC"))
            # since we don't know the write times, just assert that the order matches the order we expect
            self.assertEqual(results, list(sorted(results)))

    def cas_and_compact_test(self):
        """ Test for CAS with compact storage table, and #6813 in particular """
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE lock (
                partition text,
                key text,
                owner text,
                PRIMARY KEY (partition, key)
            ) WITH COMPACT STORAGE
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE lock")

            cursor.execute("INSERT INTO lock(partition, key, owner) VALUES ('a', 'b', null)")
            assert_one(cursor, "UPDATE lock SET owner='z' WHERE partition='a' AND key='b' IF owner=null", [True])

            assert_one(cursor, "UPDATE lock SET owner='b' WHERE partition='a' AND key='b' IF owner='a'", [False, 'z'])
            assert_one(cursor, "UPDATE lock SET owner='b' WHERE partition='a' AND key='b' IF owner='z'", [True])

            assert_one(cursor, "INSERT INTO lock(partition, key, owner) VALUES ('a', 'c', 'x') IF NOT EXISTS", [True])

    @since('2.1.1')
    def whole_list_conditional_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE tlist (
                k int PRIMARY KEY,
                l list<text>
            )""")

        cursor.execute("""
            CREATE TABLE frozentlist (
                k int PRIMARY KEY,
                l frozen<list<text>>
            )""")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE tlist")
            cursor.execute("TRUNCATE frozentlist")

            for frozen in (False, True):

                table = "frozentlist" if frozen else "tlist"
                cursor.execute("INSERT INTO %s(k, l) VALUES (0, ['foo', 'bar', 'foobar'])" % (table,))

                def check_applies(condition):
                    assert_one(cursor, "UPDATE %s SET l = ['foo', 'bar', 'foobar'] WHERE k=0 IF %s" % (table, condition), [True])
                    assert_one(cursor, "SELECT * FROM %s" % (table,), [0, ['foo', 'bar', 'foobar']])

                check_applies("l = ['foo', 'bar', 'foobar']")
                check_applies("l != ['baz']")
                check_applies("l > ['a']")
                check_applies("l >= ['a']")
                check_applies("l < ['z']")
                check_applies("l <= ['z']")
                check_applies("l IN (null, ['foo', 'bar', 'foobar'], ['a'])")

                # multiple conditions
                check_applies("l > ['aaa', 'bbb'] AND l > ['aaa']")
                check_applies("l != null AND l IN (['foo', 'bar', 'foobar'])")

                def check_does_not_apply(condition):
                    assert_one(cursor, "UPDATE %s SET l = ['foo', 'bar', 'foobar'] WHERE k=0 IF %s" % (table, condition),
                               [False, ['foo', 'bar', 'foobar']])
                    assert_one(cursor, "SELECT * FROM %s" % (table,), [0, ['foo', 'bar', 'foobar']])

                # should not apply
                check_does_not_apply("l = ['baz']")
                check_does_not_apply("l != ['foo', 'bar', 'foobar']")
                check_does_not_apply("l > ['z']")
                check_does_not_apply("l >= ['z']")
                check_does_not_apply("l < ['a']")
                check_does_not_apply("l <= ['a']")
                check_does_not_apply("l IN (['a'], null)")
                check_does_not_apply("l IN ()")

                # multiple conditions
                check_does_not_apply("l IN () AND l IN (['foo', 'bar', 'foobar'])")
                check_does_not_apply("l > ['zzz'] AND l < ['zzz']")

                def check_invalid(condition, expected=InvalidRequest):
                    assert_invalid(cursor, "UPDATE %s SET l = ['foo', 'bar', 'foobar'] WHERE k=0 IF %s" % (table, condition), expected=expected)
                    assert_one(cursor, "SELECT * FROM %s" % (table,), [0, ['foo', 'bar', 'foobar']])

                check_invalid("l = [null]")
                check_invalid("l < null")
                check_invalid("l <= null")
                check_invalid("l > null")
                check_invalid("l >= null")
                check_invalid("l IN null", expected=SyntaxException)
                check_invalid("l IN 367", expected=SyntaxException)
                check_invalid("l CONTAINS KEY 123", expected=SyntaxException)

                # not supported yet
                check_invalid("m CONTAINS 'bar'", expected=SyntaxException)

    def list_item_conditional_test(self):
        # Lists
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE tlist (
                k int PRIMARY KEY,
                l list<text>
            )""")

        cursor.execute("""
            CREATE TABLE frozentlist (
                k int PRIMARY KEY,
                l frozen<list<text>>
            )""")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE tlist")
            cursor.execute("TRUNCATE frozentlist")

            for frozen in (False, True):

                table = "frozentlist" if frozen else "tlist"

                cursor.execute("INSERT INTO %s(k, l) VALUES (0, ['foo', 'bar', 'foobar'])" % (table,))

                assert_invalid(cursor, "DELETE FROM %s WHERE k=0 IF l[null] = 'foobar'" % (table,))
                assert_invalid(cursor, "DELETE FROM %s WHERE k=0 IF l[-2] = 'foobar'" % (table,))
                assert_one(cursor, "DELETE FROM %s WHERE k=0 IF l[1] = null" % (table,), [False, ['foo', 'bar', 'foobar']])
                assert_one(cursor, "DELETE FROM %s WHERE k=0 IF l[1] = 'foobar'" % (table,), [False, ['foo', 'bar', 'foobar']])
                assert_one(cursor, "SELECT * FROM %s" % (table,), [0, ['foo', 'bar', 'foobar']])

                assert_one(cursor, "DELETE FROM %s WHERE k=0 IF l[1] = 'bar'" % (table,), [True])
                assert_none(cursor, "SELECT * FROM %s" % (table,))

    @since('2.1.1')
    def expanded_list_item_conditional_test(self):
        # expanded functionality from CASSANDRA-6839

        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE tlist (
                k int PRIMARY KEY,
                l list<text>
            )""")

        cursor.execute("""
            CREATE TABLE frozentlist (
                k int PRIMARY KEY,
                l frozen<list<text>>
            )""")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE tlist")
            cursor.execute("TRUNCATE frozentlist")

            for frozen in (False, True):

                table = "frozentlist" if frozen else "tlist"

                cursor.execute("INSERT INTO %s(k, l) VALUES (0, ['foo', 'bar', 'foobar'])" % (table,))

                def check_applies(condition):
                    assert_one(cursor, "UPDATE %s SET l = ['foo', 'bar', 'foobar'] WHERE k=0 IF %s" % (table, condition), [True])
                    assert_one(cursor, "SELECT * FROM %s" % (table,), [0, ['foo', 'bar', 'foobar']])

                check_applies("l[1] < 'zzz'")
                check_applies("l[1] <= 'bar'")
                check_applies("l[1] > 'aaa'")
                check_applies("l[1] >= 'bar'")
                check_applies("l[1] != 'xxx'")
                check_applies("l[1] != null")
                check_applies("l[1] IN (null, 'xxx', 'bar')")
                check_applies("l[1] > 'aaa' AND l[1] < 'zzz'")

                # check beyond end of list
                check_applies("l[3] = null")
                check_applies("l[3] IN (null, 'xxx', 'bar')")

                def check_does_not_apply(condition):
                    assert_one(cursor, "UPDATE %s SET l = ['foo', 'bar', 'foobar'] WHERE k=0 IF %s" % (table, condition), [False, ['foo', 'bar', 'foobar']])
                    assert_one(cursor, "SELECT * FROM %s" % (table,), [0, ['foo', 'bar', 'foobar']])

                check_does_not_apply("l[1] < 'aaa'")
                check_does_not_apply("l[1] <= 'aaa'")
                check_does_not_apply("l[1] > 'zzz'")
                check_does_not_apply("l[1] >= 'zzz'")
                check_does_not_apply("l[1] != 'bar'")
                check_does_not_apply("l[1] IN (null, 'xxx')")
                check_does_not_apply("l[1] IN ()")
                check_does_not_apply("l[1] != null AND l[1] IN ()")

                # check beyond end of list
                check_does_not_apply("l[3] != null")
                check_does_not_apply("l[3] = 'xxx'")

                def check_invalid(condition, expected=InvalidRequest):
                    assert_invalid(cursor, "UPDATE %s SET l = ['foo', 'bar', 'foobar'] WHERE k=0 IF %s" % (table, condition), expected=expected)
                    assert_one(cursor, "SELECT * FROM %s" % (table,), [0, ['foo', 'bar', 'foobar']])

                check_invalid("l[1] < null")
                check_invalid("l[1] <= null")
                check_invalid("l[1] > null")
                check_invalid("l[1] >= null")
                check_invalid("l[1] IN null", expected=SyntaxException)
                check_invalid("l[1] IN 367", expected=SyntaxException)
                check_invalid("l[1] IN (1, 2, 3)")
                check_invalid("l[1] CONTAINS 367", expected=SyntaxException)
                check_invalid("l[1] CONTAINS KEY 367", expected=SyntaxException)
                check_invalid("l[null] = null")

    @since('2.1.1')
    def whole_set_conditional_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE tset (
                k int PRIMARY KEY,
                s set<text>
            )""")

        cursor.execute("""
            CREATE TABLE frozentset (
                k int PRIMARY KEY,
                s frozen<set<text>>
            )""")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE tset")
            cursor.execute("TRUNCATE frozentset")

            for frozen in (False, True):

                table = "frozentset" if frozen else "tset"
                cursor.execute("INSERT INTO %s(k, s) VALUES (0, {'bar', 'foo'})" % (table,))

                def check_applies(condition):
                    assert_one(cursor, "UPDATE %s SET s = {'bar', 'foo'} WHERE k=0 IF %s" % (table, condition), [True])
                    assert_one(cursor, "SELECT * FROM %s" % (table,), [0, set(['bar', 'foo'])])

                check_applies("s = {'bar', 'foo'}")
                check_applies("s = {'foo', 'bar'}")
                check_applies("s != {'baz'}")
                check_applies("s > {'a'}")
                check_applies("s >= {'a'}")
                check_applies("s < {'z'}")
                check_applies("s <= {'z'}")
                check_applies("s IN (null, {'bar', 'foo'}, {'a'})")

                # multiple conditions
                check_applies("s > {'a'} AND s < {'z'}")
                check_applies("s IN (null, {'bar', 'foo'}, {'a'}) AND s IN ({'a'}, {'bar', 'foo'}, null)")

                def check_does_not_apply(condition):
                    assert_one(cursor, "UPDATE %s SET s = {'bar', 'foo'} WHERE k=0 IF %s" % (table, condition),
                               [False, {'bar', 'foo'}])
                    assert_one(cursor, "SELECT * FROM %s" % (table,), [0, {'bar', 'foo'}])

                # should not apply
                check_does_not_apply("s = {'baz'}")
                check_does_not_apply("s != {'bar', 'foo'}")
                check_does_not_apply("s > {'z'}")
                check_does_not_apply("s >= {'z'}")
                check_does_not_apply("s < {'a'}")
                check_does_not_apply("s <= {'a'}")
                check_does_not_apply("s IN ({'a'}, null)")
                check_does_not_apply("s IN ()")
                check_does_not_apply("s != null AND s IN ()")

                def check_invalid(condition, expected=InvalidRequest):
                    assert_invalid(cursor, "UPDATE %s SET s = {'bar', 'foo'} WHERE k=0 IF %s" % (table, condition), expected=expected)
                    assert_one(cursor, "SELECT * FROM %s" % (table,), [0, {'bar', 'foo'}])

                check_invalid("s = {null}")
                check_invalid("s < null")
                check_invalid("s <= null")
                check_invalid("s > null")
                check_invalid("s >= null")
                check_invalid("s IN null", expected=SyntaxException)
                check_invalid("s IN 367", expected=SyntaxException)
                check_invalid("s CONTAINS KEY 123", expected=SyntaxException)

                # element access is not allow for sets
                check_invalid("s['foo'] = 'foobar'")

                # not supported yet
                check_invalid("m CONTAINS 'bar'", expected=SyntaxException)

    @since('2.1.1')
    def whole_map_conditional_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE tmap (
                k int PRIMARY KEY,
                m map<text, text>
            )""")

        cursor.execute("""
            CREATE TABLE frozentmap (
                k int PRIMARY KEY,
                m frozen<map<text, text>>
            )""")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE tmap")
            cursor.execute("TRUNCATE frozentmap")

            for frozen in (False, True):
                debug("Testing %s maps" % ("frozen" if frozen else "normal"))

                table = "frozentmap" if frozen else "tmap"
                cursor.execute("INSERT INTO %s(k, m) VALUES (0, {'foo' : 'bar'})" % (table,))

                def check_applies(condition):
                    assert_one(cursor, "UPDATE %s SET m = {'foo': 'bar'} WHERE k=0 IF %s" % (table, condition), [True])
                    assert_one(cursor, "SELECT * FROM %s" % (table,), [0, {'foo': 'bar'}])

                check_applies("m = {'foo': 'bar'}")
                check_applies("m > {'a': 'a'}")
                check_applies("m >= {'a': 'a'}")
                check_applies("m < {'z': 'z'}")
                check_applies("m <= {'z': 'z'}")
                check_applies("m != {'a': 'a'}")
                check_applies("m IN (null, {'a': 'a'}, {'foo': 'bar'})")

                # multiple conditions
                check_applies("m > {'a': 'a'} AND m < {'z': 'z'}")
                check_applies("m != null AND m IN (null, {'a': 'a'}, {'foo': 'bar'})")

                def check_does_not_apply(condition):
                    assert_one(cursor, "UPDATE %s SET m = {'foo': 'bar'} WHERE k=0 IF %s" % (table, condition), [False, {'foo': 'bar'}])
                    assert_one(cursor, "SELECT * FROM %s" % (table,), [0, {'foo': 'bar'}])

                # should not apply
                check_does_not_apply("m = {'a': 'a'}")
                check_does_not_apply("m > {'z': 'z'}")
                check_does_not_apply("m >= {'z': 'z'}")
                check_does_not_apply("m < {'a': 'a'}")
                check_does_not_apply("m <= {'a': 'a'}")
                check_does_not_apply("m != {'foo': 'bar'}")
                check_does_not_apply("m IN ({'a': 'a'}, null)")
                check_does_not_apply("m IN ()")
                check_does_not_apply("m = null AND m != null")

                def check_invalid(condition, expected=InvalidRequest):
                    assert_invalid(cursor, "UPDATE %s SET m = {'foo': 'bar'} WHERE k=0 IF %s" % (table, condition), expected=expected)
                    assert_one(cursor, "SELECT * FROM %s" % (table,), [0, {'foo': 'bar'}])

                check_invalid("m = {null: null}")
                check_invalid("m = {'a': null}")
                check_invalid("m = {null: 'a'}")
                check_invalid("m < null")
                check_invalid("m IN null", expected=SyntaxException)

                # not supported yet
                check_invalid("m CONTAINS 'bar'", expected=SyntaxException)
                check_invalid("m CONTAINS KEY 'foo'", expected=SyntaxException)
                check_invalid("m CONTAINS null", expected=SyntaxException)
                check_invalid("m CONTAINS KEY null", expected=SyntaxException)

    def map_item_conditional_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE tmap (
                k int PRIMARY KEY,
                m map<text, text>
            )""")

        cursor.execute("""
            CREATE TABLE frozentmap (
                k int PRIMARY KEY,
                m frozen<map<text, text>>
            )""")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE tmap")
            cursor.execute("TRUNCATE frozentmap")

            for frozen in (False, True):

                table = "frozentmap" if frozen else "tmap"
                cursor.execute("INSERT INTO %s(k, m) VALUES (0, {'foo' : 'bar'})" % (table,))
                assert_invalid(cursor, "DELETE FROM %s WHERE k=0 IF m[null] = 'foo'" % (table,))
                assert_one(cursor, "DELETE FROM %s WHERE k=0 IF m['foo'] = 'foo'" % (table,), [False, {'foo': 'bar'}])
                assert_one(cursor, "DELETE FROM %s WHERE k=0 IF m['foo'] = null" % (table,), [False, {'foo': 'bar'}])
                assert_one(cursor, "SELECT * FROM %s" % (table,), [0, {'foo': 'bar'}])

                assert_one(cursor, "DELETE FROM %s WHERE k=0 IF m['foo'] = 'bar'" % (table,), [True])
                assert_none(cursor, "SELECT * FROM %s" % (table,))

                if self.get_version() > "2.1.1":
                    cursor.execute("INSERT INTO %s(k, m) VALUES (1, null)" % (table,))
                    if frozen:
                        assert_invalid(cursor, "UPDATE %s set m['foo'] = 'bar', m['bar'] = 'foo' WHERE k = 1 IF m['foo'] IN ('blah', null)" % (table,))
                    else:
                        assert_one(cursor, "UPDATE %s set m['foo'] = 'bar', m['bar'] = 'foo' WHERE k = 1 IF m['foo'] IN ('blah', null)" % (table,), [True])

    @since('2.1.1')
    def expanded_map_item_conditional_test(self):
        # expanded functionality from CASSANDRA-6839
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE tmap (
                k int PRIMARY KEY,
                m map<text, text>
            )""")

        cursor.execute("""
            CREATE TABLE frozentmap (
                k int PRIMARY KEY,
                m frozen<map<text, text>>
            )""")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE tmap")
            cursor.execute("TRUNCATE frozentmap")

            for frozen in (False, True):
                debug("Testing %s maps" % ("frozen" if frozen else "normal"))

                table = "frozentmap" if frozen else "tmap"
                cursor.execute("INSERT INTO %s(k, m) VALUES (0, {'foo' : 'bar'})" % (table,))

                def check_applies(condition):
                    assert_one(cursor, "UPDATE %s SET m = {'foo': 'bar'} WHERE k=0 IF %s" % (table, condition), [True])
                    assert_one(cursor, "SELECT * FROM %s" % (table,), [0, {'foo': 'bar'}])

                check_applies("m['xxx'] = null")
                check_applies("m['foo'] < 'zzz'")
                check_applies("m['foo'] <= 'bar'")
                check_applies("m['foo'] > 'aaa'")
                check_applies("m['foo'] >= 'bar'")
                check_applies("m['foo'] != 'xxx'")
                check_applies("m['foo'] != null")
                check_applies("m['foo'] IN (null, 'xxx', 'bar')")
                check_applies("m['xxx'] IN (null, 'xxx', 'bar')")  # m['xxx'] is not set

                # multiple conditions
                check_applies("m['foo'] < 'zzz' AND m['foo'] > 'aaa'")

                def check_does_not_apply(condition):
                    assert_one(cursor, "UPDATE %s SET m = {'foo': 'bar'} WHERE k=0 IF %s" % (table, condition), [False, {'foo': 'bar'}])
                    assert_one(cursor, "SELECT * FROM %s" % (table,), [0, {'foo': 'bar'}])

                check_does_not_apply("m['foo'] < 'aaa'")
                check_does_not_apply("m['foo'] <= 'aaa'")
                check_does_not_apply("m['foo'] > 'zzz'")
                check_does_not_apply("m['foo'] >= 'zzz'")
                check_does_not_apply("m['foo'] != 'bar'")
                check_does_not_apply("m['xxx'] != null")  # m['xxx'] is not set
                check_does_not_apply("m['foo'] IN (null, 'xxx')")
                check_does_not_apply("m['foo'] IN ()")
                check_does_not_apply("m['foo'] != null AND m['foo'] = null")

                def check_invalid(condition, expected=InvalidRequest):
                    assert_invalid(cursor, "UPDATE %s SET m = {'foo': 'bar'} WHERE k=0 IF %s" % (table, condition), expected=expected)
                    assert_one(cursor, "SELECT * FROM %s" % (table,), [0, {'foo': 'bar'}])

                check_invalid("m['foo'] < null")
                check_invalid("m['foo'] <= null")
                check_invalid("m['foo'] > null")
                check_invalid("m['foo'] >= null")
                check_invalid("m['foo'] IN null", expected=SyntaxException)
                check_invalid("m['foo'] IN 367", expected=SyntaxException)
                check_invalid("m['foo'] IN (1, 2, 3)")
                check_invalid("m['foo'] CONTAINS 367", expected=SyntaxException)
                check_invalid("m['foo'] CONTAINS KEY 367", expected=SyntaxException)
                check_invalid("m[null] = null")

    @since("2.1.1")
    def cas_and_list_index_test(self):
        """ Test for 7499 test """
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int PRIMARY KEY,
                v text,
                l list<text>
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test(k, v, l) VALUES(0, 'foobar', ['foi', 'bar'])")

            assert_one(cursor, "UPDATE test SET l[0] = 'foo' WHERE k = 0 IF v = 'barfoo'", [False, 'foobar'])
            assert_one(cursor, "UPDATE test SET l[0] = 'foo' WHERE k = 0 IF v = 'foobar'", [True])

            assert_one(cursor, "SELECT * FROM test", [0, ['foo', 'bar'], 'foobar'])

    @since("2.0")
    def static_with_limit_test(self):
        """ Test LIMIT when static columns are present (#6956) """
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int,
                s int static,
                v int,
                PRIMARY KEY (k, v)
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test(k, s) VALUES(0, 42)")
            for i in range(0, 4):
                cursor.execute("INSERT INTO test(k, v) VALUES(0, %d)" % i)

            assert_one(cursor, "SELECT * FROM test WHERE k = 0 LIMIT 1", [0, 0, 42])
            assert_all(cursor, "SELECT * FROM test WHERE k = 0 LIMIT 2", [[0, 0, 42], [0, 1, 42]])
            assert_all(cursor, "SELECT * FROM test WHERE k = 0 LIMIT 3", [[0, 0, 42], [0, 1, 42], [0, 2, 42]])

    @since("2.0")
    def static_with_empty_clustering_test(self):
        """ Test for bug of #7455 """
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test(
                pkey text,
                ckey text,
                value text,
                static_value text static,
                PRIMARY KEY(pkey, ckey)
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test(pkey, static_value) VALUES ('partition1', 'static value')")
            cursor.execute("INSERT INTO test(pkey, ckey, value) VALUES('partition1', '', 'value')")

            assert_one(cursor, "SELECT * FROM test", ['partition1', '', 'static value', 'value'])

    @since("1.2")
    def limit_compact_table(self):
        """ Check for #7052 bug """
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int,
                v int,
                PRIMARY KEY (k, v)
            ) WITH COMPACT STORAGE
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            for i in range(0, 4):
                for j in range(0, 4):
                    cursor.execute("INSERT INTO test(k, v) VALUES (%d, %d)" % (i, j))

            assert_all(cursor, "SELECT v FROM test WHERE k=0 AND v > 0 AND v <= 4 LIMIT 2", [[1], [2]])
            assert_all(cursor, "SELECT v FROM test WHERE k=0 AND v > -1 AND v <= 4 LIMIT 2", [[0], [1]])

            assert_all(cursor, "SELECT * FROM test WHERE k IN (0, 1, 2) AND v > 0 AND v <= 4 LIMIT 2", [[0, 1], [0, 2]])
            assert_all(cursor, "SELECT * FROM test WHERE k IN (0, 1, 2) AND v > -1 AND v <= 4 LIMIT 2", [[0, 0], [0, 1]])
            assert_all(cursor, "SELECT * FROM test WHERE k IN (0, 1, 2) AND v > 0 AND v <= 4 LIMIT 6", [[0, 1], [0, 2], [0, 3], [1, 1], [1, 2], [1, 3]])

            # This doesn't work -- see #7059
            # assert_all(cursor, "SELECT * FROM test WHERE v > 1 AND v <= 3 LIMIT 6 ALLOW FILTERING", [[1, 2], [1, 3], [0, 2], [0, 3], [2, 2], [2, 3]])

    def key_index_with_reverse_clustering(self):
        """ Test for #6950 bug """
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k1 int,
                k2 int,
                v int,
                PRIMARY KEY ((k1, k2), v)
            ) WITH CLUSTERING ORDER BY (v DESC)
        """)

        cursor.execute("CREATE INDEX ON test(k2)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test(k1, k2, v) VALUES (0, 0, 1)")
            cursor.execute("INSERT INTO test(k1, k2, v) VALUES (0, 1, 2)")
            cursor.execute("INSERT INTO test(k1, k2, v) VALUES (0, 0, 3)")
            cursor.execute("INSERT INTO test(k1, k2, v) VALUES (1, 0, 4)")
            cursor.execute("INSERT INTO test(k1, k2, v) VALUES (1, 1, 5)")
            cursor.execute("INSERT INTO test(k1, k2, v) VALUES (2, 0, 7)")
            cursor.execute("INSERT INTO test(k1, k2, v) VALUES (2, 1, 8)")
            cursor.execute("INSERT INTO test(k1, k2, v) VALUES (3, 0, 1)")

            assert_all(cursor, "SELECT * FROM test WHERE k2 = 0 AND v >= 2 ALLOW FILTERING", [[2, 0, 7], [0, 0, 3], [1, 0, 4]])

    def invalid_custom_timestamp_test(self):
        cursor = self.prepare()

        # Conditional updates
        cursor.execute("CREATE TABLE test (k int, v int, PRIMARY KEY (k, v))")
        # Counters
        cursor.execute("CREATE TABLE counters (k int PRIMARY KEY, c counter)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")
            cursor.execute("TRUNCATE counters")

            cursor.execute("BEGIN BATCH INSERT INTO test(k, v) VALUES(0, 0) IF NOT EXISTS; INSERT INTO test(k, v) VALUES(0, 1) IF NOT EXISTS; APPLY BATCH")
            assert_invalid(cursor, "BEGIN BATCH INSERT INTO test(k, v) VALUES(0, 2) IF NOT EXISTS USING TIMESTAMP 1; INSERT INTO test(k, v) VALUES(0, 3) IF NOT EXISTS; APPLY BATCH")
            assert_invalid(cursor, "BEGIN BATCH USING TIMESTAMP 1 INSERT INTO test(k, v) VALUES(0, 4) IF NOT EXISTS; INSERT INTO test(k, v) VALUES(0, 1) IF NOT EXISTS; APPLY BATCH")

            cursor.execute("INSERT INTO test(k, v) VALUES(1, 0) IF NOT EXISTS")
            assert_invalid(cursor, "INSERT INTO test(k, v) VALUES(1, 1) IF NOT EXISTS USING TIMESTAMP 5")

            # counters
            cursor.execute("UPDATE counters SET c = c + 1 WHERE k = 0")
            assert_invalid(cursor, "UPDATE counters USING TIMESTAMP 10 SET c = c + 1 WHERE k = 0")

            cursor.execute("BEGIN COUNTER BATCH UPDATE counters SET c = c + 1 WHERE k = 0; UPDATE counters SET c = c + 1 WHERE k = 0; APPLY BATCH")
            assert_invalid(cursor, "BEGIN COUNTER BATCH UPDATE counters USING TIMESTAMP 3 SET c = c + 1 WHERE k = 0; UPDATE counters SET c = c + 1 WHERE k = 0; APPLY BATCH")
            assert_invalid(cursor, "BEGIN COUNTER BATCH USING TIMESTAMP 3 UPDATE counters SET c = c + 1 WHERE k = 0; UPDATE counters SET c = c + 1 WHERE k = 0; APPLY BATCH")

    def clustering_order_in_test(self):
        """Test for #7105 bug"""
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                a int,
                b int,
                c int,
                PRIMARY KEY ((a, b), c)
            ) with clustering order by (c desc)
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (a, b, c) VALUES (1, 2, 3)")
            cursor.execute("INSERT INTO test (a, b, c) VALUES (4, 5, 6)")

            assert_one(cursor, "SELECT * FROM test WHERE a=1 AND b=2 AND c IN (3)", [1, 2, 3])
            assert_one(cursor, "SELECT * FROM test WHERE a=1 AND b=2 AND c IN (3, 4)", [1, 2, 3])

    def bug7105_test(self):
        """Test for #7105 bug"""
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                a int,
                b int,
                c int,
                d int,
                PRIMARY KEY (a, b)
            )
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (a, b, c, d) VALUES (1, 2, 3, 3)")
            cursor.execute("INSERT INTO test (a, b, c, d) VALUES (1, 4, 6, 5)")

            assert_one(cursor, "SELECT * FROM test WHERE a=1 AND b=2 ORDER BY b DESC", [1, 2, 3, 3])

    def bug_6612_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE session_data (
                username text,
                session_id text,
                app_name text,
                account text,
                last_access timestamp,
                created_on timestamp,
                PRIMARY KEY (username, session_id, app_name, account)
            );
        """)

        # cursor.execute("create index sessionIndex ON session_data (session_id)")
        cursor.execute("create index sessionAppName ON session_data (app_name)")
        cursor.execute("create index lastAccessIndex ON session_data (last_access)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE session_data")

            assert_one(cursor, "select count(*) from session_data where app_name='foo' and account='bar' and last_access > 4 allow filtering", [0])

            cursor.execute("insert into session_data (username, session_id, app_name, account, last_access, created_on) values ('toto', 'foo', 'foo', 'bar', 12, 13)")

            assert_one(cursor, "select count(*) from session_data where app_name='foo' and account='bar' and last_access > 4 allow filtering", [1])

    def blobAs_functions_test(self):
        cursor = self.prepare()

        cursor.execute("""
            CREATE TABLE test (
                k int PRIMARY KEY,
                v int
            );
        """)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            # A blob that is not 4 bytes should be rejected
            assert_invalid(cursor, "INSERT INTO test(k, v) VALUES (0, blobAsInt(0x01))")

    def invalid_string_literals_test(self):
        """ Test for CASSANDRA-8101 """
        cursor = self.prepare()
        cursor.execute("create table invalid_string_literals (k int primary key, a ascii, b text)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE invalid_string_literals")

            assert_invalid(cursor, u"insert into ks.invalid_string_literals (k, a) VALUES (0, '\u038E\u0394\u03B4\u03E0')")

            # since the protocol requires strings to be valid UTF-8, the error response to this is a ProtocolError
            try:
                cursor.execute("insert into ks.invalid_string_literals (k, c) VALUES (0, '\xc2\x01')")
                self.fail("Expected error")
            except ProtocolException as e:
                self.assertTrue("Cannot decode string as UTF8" in str(e))

    def negative_timestamp_test(self):
        cursor = self.prepare()

        cursor.execute("CREATE TABLE test (k int PRIMARY KEY, v int)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (k, v) VALUES (1, 1) USING TIMESTAMP -42")

            assert_one(cursor, "SELECT writetime(v) FROM TEST WHERE k = 1", [-42])

    @since('2.2')
    @require("7396")
    def select_map_key_single_row_test(self):
        cursor = self.prepare()

        cursor.execute("CREATE TABLE test (k int PRIMARY KEY, v map<int, text>)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (k, v) VALUES ( 0, {1:'a', 2:'b', 3:'c', 4:'d'})")

            assert_one(cursor, "SELECT v[1] FROM test WHERE k = 0", ['a'])
            assert_one(cursor, "SELECT v[5] FROM test WHERE k = 0", [])
            assert_one(cursor, "SELECT v[1] FROM test WHERE k = 1", [])

            assert_one(cursor, "SELECT v[1..3] FROM test WHERE k = 0", ['a', 'b', 'c'])
            assert_one(cursor, "SELECT v[3..5] FROM test WHERE k = 0", ['c', 'd'])
            assert_invalid(cursor, "SELECT v[3..1] FROM test WHERE k = 0")

            assert_one(cursor, "SELECT v[..2] FROM test WHERE k = 0", ['a', 'b'])
            assert_one(cursor, "SELECT v[3..] FROM test WHERE k = 0", ['c', 'd'])
            assert_one(cursor, "SELECT v[0..] FROM test WHERE k = 0", ['a', 'b', 'c', 'd'])
            assert_one(cursor, "SELECT v[..5] FROM test WHERE k = 0", ['a', 'b', 'c', 'd'])

            assert_one(cursor, "SELECT sizeof(v) FROM test where k = 0", [4])

    @since('2.2')
    @require("7396")
    def select_set_key_single_row_test(self):
        cursor = self.prepare()

        cursor.execute("CREATE TABLE test (k int PRIMARY KEY, v set<text>)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (k, v) VALUES ( 0, {'e', 'a', 'd', 'b'})")

            assert_one(cursor, "SELECT v FROM test WHERE k = 0", [sortedset(['a', 'b', 'd', 'e'])])
            assert_one(cursor, "SELECT v['a'] FROM test WHERE k = 0", [True])
            assert_one(cursor, "SELECT v['c'] FROM test WHERE k = 0", [False])
            assert_one(cursor, "SELECT v['a'] FROM test WHERE k = 1", [])

            assert_one(cursor, "SELECT v['b'..'d'] FROM test WHERE k = 0", ['b', 'd'])
            assert_one(cursor, "SELECT v['b'..'e'] FROM test WHERE k = 0", ['b', 'd', 'e'])
            assert_one(cursor, "SELECT v['a'..'d'] FROM test WHERE k = 0", ['a', 'b', 'd'])
            assert_one(cursor, "SELECT v['b'..'f'] FROM test WHERE k = 0", ['b', 'd', 'e'])
            assert_invalid(cursor, "SELECT v['d'..'a'] FROM test WHERE k = 0")

            assert_one(cursor, "SELECT v['d'..] FROM test WHERE k = 0", ['d', 'e'])
            assert_one(cursor, "SELECT v[..'d'] FROM test WHERE k = 0", ['a', 'b', 'd'])
            assert_one(cursor, "SELECT v['f'..] FROM test WHERE k = 0", [])
            assert_one(cursor, "SELECT v[..'f'] FROM test WHERE k = 0", ['a', 'b', 'd', 'e'])

            assert_one(cursor, "SELECT sizeof(v) FROM test where k = 0", [4])

    @since('2.2')
    @require("7396")
    def select_list_key_single_row_test(self):
        cursor = self.prepare()

        cursor.execute("CREATE TABLE test (k int PRIMARY KEY, v list<text>)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (k, v) VALUES ( 0, ['e', 'a', 'd', 'b'])")

            assert_one(cursor, "SELECT v FROM test WHERE k = 0", [['e', 'a', 'd', 'b']])
            assert_one(cursor, "SELECT v[0] FROM test WHERE k = 0", ['e'])
            assert_one(cursor, "SELECT v[3] FROM test WHERE k = 0", ['b'])
            assert_one(cursor, "SELECT v[0] FROM test WHERE k = 1", [])

            assert_invalid(cursor, "SELECT v[-1] FROM test WHERE k = 0")
            assert_invalid(cursor, "SELECT v[5] FROM test WHERE k = 0")

            assert_one(cursor, "SELECT v[1..3] FROM test WHERE k = 0", ['a', 'd', 'b'])
            assert_one(cursor, "SELECT v[0..2] FROM test WHERE k = 0", ['e', 'a', 'd'])
            assert_invalid(cursor, "SELECT v[0..4] FROM test WHERE k = 0")
            assert_invalid(cursor, "SELECT v[2..0] FROM test WHERE k = 0")

            assert_one(cursor, "SELECT sizeof(v) FROM test where k = 0", [4])

    @since('2.2')
    @require("7396")
    def select_map_key_multi_row_test(self):
        cursor = self.prepare()

        cursor.execute("CREATE TABLE test (k int PRIMARY KEY, v map<int, text>)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (k, v) VALUES ( 0, {1:'a', 2:'b', 3:'c', 4:'d'})")
            cursor.execute("INSERT INTO test (k, v) VALUES ( 1, {1:'a', 2:'b', 5:'e', 6:'f'})")

            assert_all(cursor, "SELECT v[1] FROM test", [['a'], ['a']])
            assert_all(cursor, "SELECT v[5] FROM test", [[], ['e']])
            assert_all(cursor, "SELECT v[4] FROM test", [['d'], []])

            assert_all(cursor, "SELECT v[1..3] FROM test", [['a', 'b', 'c'], ['a', 'b', 'e']])
            assert_all(cursor, "SELECT v[3..5] FROM test", [['c', 'd'], ['e']])
            assert_invalid(cursor, "SELECT v[3..1] FROM test")

            assert_all(cursor, "SELECT v[..2] FROM test", [['a', 'b'], ['a', 'b']])
            assert_all(cursor, "SELECT v[3..] FROM test", [['c', 'd'], ['e', 'f']])
            assert_all(cursor, "SELECT v[0..] FROM test", [['a', 'b', 'c', 'd'], ['a', 'b', 'e', 'f']])
            assert_all(cursor, "SELECT v[..5] FROM test", [['a', 'b', 'c', 'd'], ['a', 'b', 'e']])

            assert_all(cursor, "SELECT sizeof(v) FROM test", [[4], [4]])

    @since('2.2')
    @require("7396")
    def select_set_key_multi_row_test(self):
        cursor = self.prepare()

        cursor.execute("CREATE TABLE test (k int PRIMARY KEY, v set<text>)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (k, v) VALUES ( 0, {'e', 'a', 'd', 'b'})")
            cursor.execute("INSERT INTO test (k, v) VALUES ( 1, {'c', 'f', 'd', 'b'})")

            assert_all(cursor, "SELECT v FROM test", [[sortedset(['b', 'c', 'd', 'f'])], [sortedset(['a', 'b', 'd', 'e'])]])
            assert_all(cursor, "SELECT v['a'] FROM test", [[True], [False]])
            assert_all(cursor, "SELECT v['c'] FROM test", [[False], [True]])

            assert_all(cursor, "SELECT v['b'..'d'] FROM test", [['b', 'd'], ['b', 'c', 'd']])
            assert_all(cursor, "SELECT v['b'..'e'] FROM test", [['b', 'd', 'e'], ['b', 'c', 'd']])
            assert_all(cursor, "SELECT v['a'..'d'] FROM test", [['a', 'b', 'd'], ['b', 'c', 'd']])
            assert_all(cursor, "SELECT v['b'..'f'] FROM test", [['b', 'd', 'e'], ['b', 'c', 'd', 'f']])
            assert_invalid(cursor, "SELECT v['d'..'a'] FROM test")

            assert_all(cursor, "SELECT v['d'..] FROM test", [['d', 'e'], ['d', 'f']])
            assert_all(cursor, "SELECT v[..'d'] FROM test", [['a', 'b', 'd'], ['b', 'c', 'd']])
            assert_all(cursor, "SELECT v['f'..] FROM test", [[], ['f']])
            assert_all(cursor, "SELECT v[..'f'] FROM test", [['a', 'b', 'd', 'e'], ['b', 'c', 'd', 'f']])

            assert_all(cursor, "SELECT sizeof(v) FROM test", [[4], [4]])

    @since('2.2')
    @require("7396")
    def select_list_key_multi_row_test(self):
        cursor = self.prepare()

        cursor.execute("CREATE TABLE test (k int PRIMARY KEY, v list<text>)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE test")

            cursor.execute("INSERT INTO test (k, v) VALUES ( 0, ['e', 'a', 'd', 'b'])")
            cursor.execute("INSERT INTO test (k, v) VALUES ( 1, ['c', 'f', 'd', 'b'])")

            assert_all(cursor, "SELECT v FROM test", [[['c', 'f', 'd', 'b']], [['e', 'a', 'd', 'b']]])
            assert_all(cursor, "SELECT v[0] FROM test", [['e'], ['c']])
            assert_all(cursor, "SELECT v[3] FROM test", [['b'], ['b']])
            assert_invalid(cursor, "SELECT v[-1] FROM test")
            assert_invalid(cursor, "SELECT v[5] FROM test")

            assert_all(cursor, "SELECT v[1..3] FROM test", [['a', 'd', 'b'], ['f', 'd', 'b']])
            assert_all(cursor, "SELECT v[0..2] FROM test", [['e', 'a', 'd'], ['c', 'f', 'd']])
            assert_invalid(cursor, "SELECT v[0..4] FROM test")
            assert_invalid(cursor, "SELECT v[2..0] FROM test")

            assert_all(cursor, "SELECT sizeof(v) FROM test", [[4], [4]])

    def bug_8558_test(self):
        cursor = self.prepare()
        node1 = self.cluster.nodelist()[0]

        cursor.execute("CREATE  KEYSPACE space1 WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1}")
        cursor.execute("CREATE  TABLE space1.table1(a int, b int, c text,primary key(a,b))")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            cursor.execute("TRUNCATE space1.table1")

            cursor.execute("INSERT INTO space1.table1(a,b,c) VALUES(1,1,'1')")
            node1.nodetool('flush')
            cursor.execute("DELETE FROM space1.table1 where a=1 and b=1")
            node1.nodetool('flush')

            assert_none(cursor, "select * from space1.table1 where a=1 and b=1")

    def bug_5732_test(self):
        cursor = self.prepare(use_cache=True)

        cursor.execute("""
            CREATE TABLE test (
                k int PRIMARY KEY,
                v int,
            )
        """)

        if self.node_version_above('2.1'):
            cursor.execute("ALTER TABLE test WITH caching = {'keys': 'ALL', 'rows_per_partition': 'ALL'}")
        else:
            cursor.execute("ALTER TABLE test WITH CACHING='ALL'")
        cursor.execute("INSERT INTO test (k,v) VALUES (0,0)")
        cursor.execute("INSERT INTO test (k,v) VALUES (1,1)")
        cursor.execute("CREATE INDEX testindex on test(v)")

        # wait for the index to be fully built
        start = time.time()
        while True:
            if self.node_version_above('3.0'):
                results = cursor.execute("""SELECT * FROM system_schema.indexes WHERE keyspace_name = 'ks' AND table_name = 'test' AND index_name = 'testindex'""")
            else:
                results = cursor.execute("""SELECT * FROM system."IndexInfo" WHERE table_name = 'ks' AND index_name = 'test.testindex'""")
            if results:
                break

            if time.time() - start > 10.0:
                if self.node_version_above('3.0'):
                    results = list(cursor.execute('SELECT * FROM system_schema.indexes'))
                else:
                    results = list(cursor.execute('SELECT * FROM system."IndexInfo"'))
                raise Exception("Failed to build secondary index within ten seconds: %s" % (results,))
            time.sleep(0.1)

        assert_all(cursor, "SELECT k FROM test WHERE v = 0", [[0]])

        self.cluster.stop()
        time.sleep(0.5)
        self.cluster.start(wait_for_binary_proto=True)
        time.sleep(0.5)

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))
            assert_all(cursor, "SELECT k FROM ks.test WHERE v = 0", [[0]])

    def bug_10652_test(self):
        cursor = self.prepare()

        cursor.execute("CREATE KEYSPACE foo WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1}")
        cursor.execute("CREATE TABLE foo.bar (k int PRIMARY KEY, v int)")

        for is_upgraded, cursor in self.do_upgrade(cursor):
            debug("Querying %s node" % ("upgraded" if is_upgraded else "old",))

            future = cursor.execute_async("INSERT INTO foo.bar(k, v) VALUES (0, 0)", trace=True)
            future.result()
            trace = future.get_query_trace(max_wait=120)

            self.cluster.flush()

            assert_one(cursor, "SELECT * FROM foo.bar", [0, 0])


class TestCQLNodes3RF3(TestCQL):
    NODES, RF, __test__, CL = 3, 3, True, ConsistencyLevel.ALL


class TestCQLNodes2RF1(TestCQL):
    NODES, RF, __test__ = 2, 1, True
