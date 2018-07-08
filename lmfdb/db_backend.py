"""
This module provides an interface to Postgres supporting
the kinds of queries needed by the LMFDB.

EXAMPLES::

    sage: from lmfdb.db_backend import db
    sage: db
    Interface to Postgres database
    sage: len(db.tablenames)
    53
    sage: db.tablenames[0]
    'artin_field_data'
    sage: db.artin_field_data
    Interface to Postgres table artin_field_data

You can search using the methods ``search``, ``lucky`` and ``lookup``::

    sage: G = db.gps_small.lookup('8.2')
    sage: G['Exponent']
    4

- ``extra_table`` -- a string or None.  If provided, gives the name of a table that is linked to the search table by an ``id`` column and provides more data that cannot be searched on.  The reason to separate the data into two tables is to reduce the size of the search table.  For large tables this speeds up some queries.
- ``count_table`` -- a string or None.  If provided, gives the name of a table that caches counts for searches on the search table.  These counts are relevant when many results are returned, allowing the search pages to report the number of records even when it would take Postgres a long time to compute this count.

"""


import logging, tempfile, re, os, time, random
from collections import Counter
from psycopg2 import connect, DatabaseError
from psycopg2.sql import SQL, Identifier, Placeholder, Literal, Composable
from psycopg2.extras import execute_values
from lmfdb.db_encoding import setup_connection, Array, Json, copy_dumps
from sage.misc.cachefunc import cached_method
from sage.misc.mrange import cartesian_product_iterator
from lmfdb.utils import make_logger, format_percentage
from lmfdb.typed_data.artin_types import Dokchitser_ArtinRepresentation, Dokchitser_NumberFieldGaloisGroup

SLOW_QUERY_LOGFILE = "slow_queries.log"
SLOW_CUTOFF = 1

# This list is used when creating new tables
types_whitelist = set([
    "int2", "smallint", "smallserial", "serial2",
    "int4", "int", "integer", "serial", "serial4",
    "int8", "bigint", "bigserial", "serial8",
    "numeric", "decimal",
    "float4", "real",
    "float8", "double precision",
    "boolean", "bool",
    "text", "char", "character", "character varying", "varchar",
    "json", "jsonb", "xml",
    "date", "interval", "time", "time without time zone", "time with time zone", "timetz",
    "timestamp", "timestamp without time zone", "timestamp with time zone", "timestamptz",
    "bytea", "bit", "bit varying", "varbit",
    "point", "line", "lseg", "path", "box", "polygon", "circle",
    "tsquery", "tsvector",
    "txid_snapshot", "uuid",
    "cidr", "inet", "macaddr",
    "money", "pg_lsn",
])
param_types_whitelist = [
    r"^(bit( varying)?|varbit)\s*\([1-9][0-9]*\)$",
    r'(text|(char(acter)?|character varying|varchar(\s*\(1-9][0-9]*\))?))(\s+collate "(c|posix|[a-z][a-z]_[a-z][a-z](\.[a-z0-9-]+)?)")?',
    r"^interval(\s+year|month|day|hour|minute|second|year to month|day to hour|day to minute|day to second|hour to minute|hour to second|minute to second)?(\s*\([0-6]\))?$",
    r"^timestamp\s*\([0-6]\)(\s+with(out)? time zone)?$",
    r"^time\s*\(([0-9]|10)\)(\s+with(out)? time zone)?$",
    r"^(numeric|decimal)\s*\([1-9][0-9]*(,\s*(0|[1-9][0-9]*))?\)$",
]
param_types_whitelist = [re.compile(s) for s in param_types_whitelist]
# the non-default operator classes, used in creating indexes
_operator_classes = {'brin':   ['inet_minmax_ops'],
                     'btree':  ['bpchar_pattern_ops', 'cidr_ops', 'record_image_ops',
                                'text_pattern_ops', 'varchar_ops', 'varchar_pattern_ops'],
                     'gin':    ['jsonb_path_ops'],
                     'gist':   ['inet_ops'],
                     'hash':   ['bpchar_pattern_ops', 'cidr_ops', 'text_pattern_ops',
                                'varchar_ops', 'varchar_pattern_ops'],
                     'spgist': ['kd_point_ops']}
# Valid storage parameters by type, used in creating indexes
_valid_storage_params = {'brin':   ['pages_per_range', 'autosummarize'],
                         'btree':  ['fillfactor'],
                         'gin':    ['fastupdate', 'gin_pending_list_limit'],
                         'gist':   ['fillfactor', 'buffering'],
                         'hash':   ['fillfactor'],
                         'spgist': ['fillfactor']}

class QueryLogFilter(object):
    """
    A filter used when logging slow queries.
    """
    def filter(self, record):
        if record.pathname.startswith('db_backend.py'):
            return 1
        else:
            return 0

class EmptyContext(object):
    """
    Used to simplify code in cases where we may or may not want to open an extras file.
    """
    name = None
    def __enter__(self):
        pass
    def __exit__(self, exc_type, exc_value, traceback):
        pass

class PostgresBase(object):
    """
    A base class for various objects that interact with Postgres.

    Any class inheriting from this one must provide a connection
    to the postgres database, as well as a name used when creating a logger.
    """
    def __init__(self, loggername, conn):
        self.conn = conn
        self.logger = make_logger(loggername)
        handler = logging.FileHandler(SLOW_QUERY_LOGFILE)
        formatter = logging.Formatter("%(asctime)s - %(message)s")
        filt = QueryLogFilter()
        handler.setFormatter(formatter)
        handler.addFilter(filt)
        self.logger.addHandler(handler)

    def _execute(self, query, values=None, silent=False, values_list=False, template=None, commit=True):
        """
        Execute an SQL command, properly catching errors and returning the resulting cursor.

        INPUT:

        - ``query`` -- an SQL Composable object, the SQL command to execute.
        - ``values`` -- values to substitute for %s in the query.  Quoting from the documentation for psycopg2 (http://initd.org/psycopg/docs/usage.html#passing-parameters-to-sql-queries):

        Never, never, NEVER use Python string concatenation (+) or string parameters interpolation (%) to pass variables to a SQL query string. Not even at gunpoint.
        - ``silent`` -- boolean (default False).  If True, don't log a warning for a slow query.
        - ``values_list`` -- boolean (default False).  If True, use the ``execute_values`` method, designed for inserting multiple values.
        - ``template`` -- string, for use with ``values_list`` to insert constant values: for example ``"(%s, %s, 42)"``. See the documentation of ``execute_values`` for more details.
        - ``commit`` -- boolean (default True).  Whether to commit changes on success.

        OUTPUT:

        - a cursor object from which the resulting records can be obtained via iteration.

        This function will also log slow queries.
        """
        if not isinstance(query, Composable):
            raise TypeError("You must use the psycopg2.sql module to execute queries")
        cur = self.conn.cursor()
        try:
            t = time.time()
            if values_list:
                execute_values(cur, query, values, template)
            else:
                cur.execute(query, values)
            if not silent:
                t = time.time() - t
                if t > SLOW_CUTOFF:
                    query = query.as_string(self.conn)
                    if values:
                        query = query%(tuple(values))
                    self.logger.info(query + " ran in %ss"%(t))
        except DatabaseError:
            self.conn.rollback()
            raise
        else:
            if commit:
                self.conn.commit()
        return cur

    @cached_method
    def _table_exists(self, tablename):
        cur = self._execute(SQL("SELECT to_regclass(%s)"), [tablename], silent=True)
        return cur.fetchone()[0] is not None

    @staticmethod
    def _sort_str(sort_list):
        """
        Constructs a psycopg2.sql.Composable object describing a sort order for Postgres from a list of columns.

        INPUT:

        - ``sort_list`` -- a list, either of strings (which are interpreted as column names in the ascending direction) or of pairs (column name, 1 or -1).

        OUTPUT:

        - a Composable to be used by psycopg2 in the ORDER BY clause.
        """
        L = []
        for col in sort_list:
            if isinstance(col, basestring):
                L.append(Identifier(col))
            elif col[1] == 1:
                L.append(Identifier(col[0]))
            else:
                L.append(SQL("{0} DESC").format(Identifier(col[0])))
        return SQL(", ").join(L)

class PostgresTable(PostgresBase):
    """
    This class is used to abstract a table in the LMFDB database
    on which searches are performed.  Technically, it may represent
    more than one table, since some tables are split in two for performance
    reasons.

    INPUT:

    - ``db`` -- an instance of ``PostgresDatabase``, currently just used to store the common connection ``conn``.
    - ``search_table`` -- a string, the name of the table in postgres.
    - ``label_col`` -- the column holding the LMFDB label, or None if no such column exists.
    - ``sort`` -- a list giving the default sort order on the table, or None.  If None, sorts that can return more than one result must explicitly specify a sort order.  Note that the id column is sometimes used for sorting; see the ``search`` method for more details.
    - ``count_cutoff`` -- an integer parameter (default 1000) which determines the threshold at which searches will no longer report the exact number of results.
    """
    def __init__(self, db, search_table, label_col, sort=None, count_cutoff=1000, id_ordered=False, out_of_order=False, has_extras=False, stats_valid=True):
        self._db = db
        self.search_table = search_table
        self._label_col = label_col
        self._count_cutoff = count_cutoff
        self._id_ordered = id_ordered
        self._out_of_order = out_of_order
        self._stats_valid = stats_valid
        PostgresBase.__init__(self, search_table, db.conn)
        self._col_type = {}
        self.has_id = False
        def set_column_info(col_list, table_name):
            cur = self._execute(SQL("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = %s ORDER BY ordinal_position"), [table_name])
            for rec in cur:
                col = rec[0]
                self._col_type[col] = rec[1]
                if col != 'id':
                    col_list.append(col)
                else:
                    self.has_id = True
        self._search_cols = []
        if has_extras:
            self.extra_table = search_table + "_extras"
            self._extra_cols = []
            set_column_info(self._extra_cols, self.extra_table)
        else:
            self.extra_table = None
            self._extra_cols = []
        set_column_info(self._search_cols, search_table)
        self._set_sort(sort)
        self.stats = PostgresStatsTable(self)

    def _set_sort(self, sort):
        """
        Initialize the sorting attributes from a list of columns or pairs (col, direction)
        """
        self._sort_keys = set([])
        if sort:
            for col in sort:
                if isinstance(col, basestring):
                    self._sort_keys.add(col)
                else:
                    self._sort_keys.add(col[0])
            self._primary_sort = sort[0]
            if not isinstance(self._primary_sort, basestring):
                self._primary_sort = self._primary_sort[0]
            self._sort = self._sort_str(sort)
        else:
            self._sort = self._primary_sort = None

    def __repr__(self):
        return "Interface to Postgres table %s"%(self.search_table)

    ##################################################################
    # Helper functions for querying                                  #
    ##################################################################

    def _parse_projection(self, projection):
        """
        Parses various ways of specifying which columns are desired.

        INPUT:

        - ``projection`` -- either 0, 1, 2, a dictionary or list of column names.

          - If 0, projects just to the ``label``.  If the search table does not have a lable column, raises a RuntimeError.
          - If 1, projects to all columns in the search table.
          - If 1.1, as 1 but with id included
          - If 2, projects to all columns in either the search or extras tables.
          - If a dictionary, can specify columns to include by giving True values, or columns to exclude by giving False values.
          - If a list, specifies which columns to include.
          - If a string, projects onto just that column; searches will return the value rather than a dictionary.

        OUTPUT:

        - a tuple of columns to be selected that are in the search table
        - a tuple of columns to be selected that are in the extras table (empty if it doesn't exist)
        - a start position for the columns to be returned to the user (the id column may be needed internally to link the two tables.

        EXAMPLES:

            sage: from lmfdb.db_backend import db
            sage: ec = db.ec_padic
            sage: nf = db.nf_fields
            sage: nf._parse_projection(0)
            ((u'label',), (), 0)
            sage: ec._parse_projection(1)
            ((u'lmfdb_iso', u'p', u'prec', u'val', u'unit'), (), 0)
            sage: ec._parse_projection({"val":True, "unit":True})
            ((u'val', u'unit'), (), 0)

        When the data is split across two tables, some columns may be in the extras table:

            sage: nf._parse_projection(["label", "unitsGmodule"])
            (('id', 'label'), ('unitsGmodule',), 1)

        In the previous example, the id column is included to link the tables.
        If you want the "id" column, list it explicitly.  The start_position will then be 0:

            sage: nf._parse_projection(["id", "label", "unitsGmodule"])
            (('id', 'label'), ('unitsGmodule',), 0)

        You can specify a dictionary with columns to exclude:

            sage: ec._parse_projection({"prec":False})
            ((u'lmfdb_iso', u'p', u'val', u'unit'), (), 0)
        """
        search_cols = []
        extra_cols = []
        if projection == 0:
            if "label" not in self._search_cols:
                raise RuntimeError("label not column of %s"%(self.search_table))
            return (u"label",), (), 0
        elif not projection:
            raise ValueError("You must specify at least one key.")
        if projection == 1:
            return self._search_cols, (), 0
        elif projection == 1.1:
                return ["id"] + self._search_cols, (), 0
        elif projection == 2:
            if self.extra_table is None:
                return self._search_cols, (), 0
            else:
                return ["id"] + self._search_cols, self._extra_cols, 1
        elif isinstance(projection, dict):
            projvals = set(bool(val) for val in projection.values())
            if len(projvals) > 1:
                raise ValueError("You cannot both include and exclude.")
            including = projvals.pop()
            include_id = projection.pop("id", False)
            for col in self._search_cols:
                if (col in projection) == including:
                    search_cols.append(col)
                projection.pop(col, None)
            if self.extra_table is not None:
                for col in self._extra_cols:
                    if (col in projvals) == including:
                        extra_cols.append(col)
                    projection.pop(col, None)
            if projection: # there were extra columns requested
                raise ValueError("%s not column of %s"%(", ".join(projection), self.search_table))
        else: # iterable or basestring
            if isinstance(projection, basestring):
                projection = [projection]
            include_id = False
            for col in projection:
                if col in self._search_cols:
                    search_cols.append(col)
                elif col in self._extra_cols:
                    extra_cols.append(col)
                elif col == 'id':
                    include_id = True
                else:
                    raise ValueError("%s not column of table"%col)
        if include_id or extra_cols:
            search_cols.insert(0, "id")
        return tuple(search_cols), tuple(extra_cols), 0 if (include_id or not extra_cols) else 1

    def _parse_special(self, key, value, col):
        """
        Implements more complicated query conditions than just testing for equality:
        inequalities, containment and disjunctions.

        INPUT:

        - ``key`` -- a code starting with $ from the following list:
          - ``$lte`` -- less than or equal to
          - ``$lt`` -- less than
          - ``$gte`` -- greater than or equal to
          - ``$gt`` -- greater than
          - ``$ne`` -- not equal to
          - ``$in`` -- the column must be one of the given set of values
          - ``$nin`` -- the column must not be any of the given set of values
          - ``$contains`` -- for json columns, the given value should be a subset of the column.
          - ``$notcontains`` -- for json columns, the column must not contain any entry of the given value (which should be iterable)
          - ``$containedin`` -- for json columns, the column should be a subset of the given list
          - ``$exists`` -- if True, require not null; if False, require null.
        - ``value`` -- The value to compare to.  The meaning depends on the key.
        - ``col`` -- The name of the column.

        OUTPUT:

        - A string giving the SQL test corresponding to the requested query, with %s
        - values to fill in for the %s entries (see ``_execute`` for more discussion).

        EXAMPLES::

            sage: from lmfdb.db_backend import db
            sage: statement, vals = db.nf_fields._parse_special("$lte", 5, "degree")
            ('"degree" <= %s', [5])
            sage: statement, vals = db.nf_fields._parse_special("$or", [{"degree":{"$lte":5}},{"class_number":{"$gte":3}}], None)
            sage: statement.as_string(db.conn), vals
            ('("degree" <= %s OR "class_number" >= %s)', [5, 3])
            sage: statement, vals = db.nf_fields._parse_special("$or", [{"$lte":5}, {"$gte":10}], "degree")
            sage: statement.as_string(db.conn), vals
            ('("degree" <= %s OR "degree" >= %s)', [5, 10])
            sage: statement, vals = db.nf_fields._parse_special("$and", [{"$gte":5}, {"$lte":10}], "degree")
            sage: statement.as_string(db.conn), vals
            ('("degree" >= %s AND "degree" <= %s)', [5, 10])
            sage: statement, vals = db.nf_fields._parse_special("$contains", [2,3,5], "ramps")
            sage: statement.as_string(db.conn), vals
            ('"ramps" @> %s', [[2, 3, 5]])
        """
        if key in ['$or', '$and']:
            pairs = [self._parse_dict(clause, outer=col) for clause in value]
            pairs = [pair for pair in pairs if pair[0] is not None]
            if pairs:
                strings, values = zip(*pairs)
                # flatten values
                values = [item for sublist in values for item in sublist]
                joiner = " OR " if key == '$or' else " AND "
                return SQL("({0})").format(SQL(joiner).join(strings)), values
            else:
                return None, None
        if isinstance(col, Composable):
            # Compound specifier like cc.1
            force_json = True
        else:
            force_json = (self._col_type[col] == 'jsonb')
            col = Identifier(col)
        # First handle the cases that have unusual values
        if key == '$exists':
            if value:
                cmd = SQL("{0} IS NOT NULL").format(col)
            else:
                cmd = SQL("{0} IS NULL").format(col)
            value = []
        elif key == '$notcontains':
            cmd = SQL(" AND ").join(SQL("NOT {0} @> %s").format(col) * len(value))
            value = [[v] for v in value]
        else:
            if key == '$lte':
                cmd = SQL("{0} <= %s")
            elif key == '$lt':
                cmd = SQL("{0} < %s")
            elif key == '$gte':
                cmd = SQL("{0} >= %s")
            elif key == '$gt':
                cmd = SQL("{0} > %s")
            elif key == '$ne':
                cmd = SQL("{0} != %s")
            elif key == '$in':
                cmd = SQL("{0} = ANY(%s)")
                value = Array(value)
            elif key == '$nin':
                cmd = SQL("NOT ({0} = ANY(%s)")
                value = Array(value)
            elif key == '$contains':
                cmd = SQL("{0} @> %s")
            elif key == '$containedin':
                cmd = SQL("{0} <@ %s")
            else:
                raise ValueError("Error building query: {0}".format(key))
            if force_json:
                if isinstance(value, Array):
                    raise ValueError("$in and $nin operators not supported for jsonb")
                value = Json(value)
            cmd = cmd.format(col)
            value = [value]
        return cmd, value

    def _parse_dict(self, D, outer=None):
        """
        Parses a dictionary that specifies a query in something close to Mongo syntax into an SQL query.

        INPUT:

        - ``D`` -- a dictionary
        - ``outer`` -- the column that we are parsing (None if not yet parsing any column).  Used in recursion.

        OUTPUT:

        - An SQL Composable giving the WHERE component of an SQL query (possibly containing %s), or None if D imposes no constraint
        - A list of values to fill in for the %s in the string.  See ``_execute`` for more details.

        EXAMPLES::

            sage: from lmfdb.db_backend import db
            sage: statement, vals = db.nf_fields._parse_dict({"degree":2, "class_number":6})
            sage: statement.as_string(db.conn), vals
            ('"class_number" = %s AND "degree" = %s', [6, 2])
            sage: statement, vals = db.nf_fields._parse_dict({"degree":{"$gte":4,"$lte":8}, "r2":1})
            sage: statement.as_string(db.conn), vals
            ('"r2" = %s AND "degree" <= %s AND "degree" >= %s', [1, 8, 4])
            sage: statement, vals = db.nf_fields._parse_dict({"degree":2, "$or":[{"class_number":1,"r2":0},{"disc_sign":1,"disc_abs":{"$lte":10000},"class_number":{"$lte":8}}]})
            sage: statement.as_string(db.conn), vals
            ('("class_number" = %s AND "r2" = %s OR "disc_sign" = %s AND "class_number" <= %s AND "disc_abs" <= %s) AND "degree" = %s', [1, 0, 1, 8, 10000, 2])
            sage: db.nf_fields._parse_dict({})
            (None, None)
        """
        if len(D) == 0:
            return None, None
        else:
            strings = []
            values = []
            for key, value in D.iteritems():
                if not key:
                    raise ValueError("Error building query: empty key")
                if key[0] == '$':
                    sub, vals = self._parse_special(key, value, outer)
                    if sub is not None:
                        strings.append(sub)
                        values.extend(vals)
                    continue
                if '.' in key:
                    path = [int(p) if p.isdigit() else p for p in key.split('.')]
                    key, path = path[0], [SQL("->{0}").format(Literal(p)) for p in path[1:]]
                    force_json = True
                else:
                    path = []
                    force_json = (self._col_type[key] == 'jsonb')
                if key != 'id' and key not in self._search_cols:
                    raise ValueError("%s is not a column of %s"%(key, self.search_table))
                if path:
                    # Only call SQL for compound keys here so that _parse_special calls
                    # can distinguish between basic and compound keys based on type
                    key = SQL("{0}{1}").format(Identifier(key), SQL("").join(path))
                if isinstance(value, dict) and all(k.startswith('$') for k in value.iterkeys()):
                    sub, vals = self._parse_dict(value, key)
                    if sub is not None:
                        strings.append(sub)
                        values.extend(vals)
                    continue
                if not path:
                    # Now need to make key an identifier
                    key = Identifier(key)
                if value is None:
                    strings.append(SQL("{0} IS NULL").format(key))
                else:
                    if force_json:
                        value = Json(value)
                    strings.append(SQL("{0} = %s").format(key))
                    values.append(value)
            if strings:
                return SQL(" AND ").join(strings), values
            else:
                return None, None

    def _build_query(self, query, limit=None, offset=0, sort=None):
        """
        Build an SQL query from a dictionary, including limit, offset and sorting.

        INPUT:

        - ``query`` -- a dictionary query, in the mongo style (but only supporting certain special operators, as in ``_parse_special``)
        - ``limit`` -- a limit on the number of records returned
        - ``offset`` -- an offset on how many records to skip
        - ``sort`` -- a sort order (to be passed into the ``_sort_str`` method, or None.

        OUTPUT:

        - an SQL Composable giving the WHERE, ORDER BY, LIMIT and OFFSET components of an SQL query, possibly including %s
        - a list of values to substitute for the %s entries

        EXAMPLES::

            sage: from lmfdb.db_backend import db
            sage: statement, vals = db.nf_fields._build_query({"degree":2, "class_number":6})
            sage: statement.as_string(db.conn), vals
            (' WHERE "class_number" = %s AND "degree" = %s ORDER BY "degree", "disc_abs", "disc_sign", "label"', [6, 2])
            sage: statement, vals = db.nf_fields._build_query({"class_number":1}, 20)
            sage: statement.as_string(db.conn), vals
            (' WHERE "class_number" = %s ORDER BY "id" LIMIT %s', [1, 20])
        """
        qstr, values = self._parse_dict(query)
        if qstr is None:
            s = SQL("")
            values = []
        else:
            s = SQL(" WHERE {0}").format(qstr)
        if sort is None:
            has_sort = True
            if self._sort is None:
                if limit is not None and not (limit == 1 and offset == 0):
                    raise ValueError("You must specify a sort order")
                has_sort = False
            elif self._primary_sort in query or self._out_of_order:
                # We use the actual sort because the postgres query planner doesn't know that
                # the primary key is connected to the id.
                sort = self._sort
            else:
                sort = Identifier("id")
        else:
            has_sort = bool(sort)
            sort = self._sort_str(sort)
        if has_sort:
            s = SQL("{0} ORDER BY {1}").format(s, sort)
        if limit is not None:
            s = SQL("{0} LIMIT %s").format(s)
            values.append(limit)
            if offset != 0:
                s = SQL("{0} OFFSET %s").format(s)
                values.append(offset)
        return s, values

    def _search_iterator(self, cur, search_cols, extra_cols, id_offset, projection):
        """
        Returns an iterator over the results in a cursor,
        filling in columns from the extras table if needed.

        INPUT:

        - ``cur`` -- a psycopg2 cursor
        - ``search_cols`` -- the columns in the search table in the results
        - ``extra_cols`` -- the columns in the extras table in the results
        - ``id_offset`` -- 0 or 1.  Where to start in search_cols,
                           depending on whether ``id`` should be included.
        - ``projection`` -- the projection requested.

        OUTPUT:

        If projection is 0 or a string, an iterator that yields the labels/column values of the query results.
        Otherwise, an iterator that yields dictionaries with keys
        from ``search_cols`` and ``extra_cols``.
        """
        # Eventually want to batch the queries on the extra_table so that we make
        # fewer SQL queries here.
        for rec in cur:
            if projection == 0 or isinstance(projection, basestring) and not extra_cols:
                yield rec[0]
            else:
                D = {k:v for k,v in zip(search_cols[id_offset:], rec[id_offset:]) if v is not None}
                if extra_cols:
                    selecter = SQL("SELECT {0} FROM {1} WHERE id = %s").format(SQL(", ").join(map(Identifier, extra_cols)), Identifier(self.extra_table))
                    extra_cur = self._execute(selecter, [rec[0]])
                    extra_rec = extra_cur.fetchone()
                    for k,v in zip(extra_cols, extra_rec):
                        if v is not None:
                            D[k] = v
                if isinstance(projection, basestring):
                    yield D[projection]
                else:
                    yield D

    ##################################################################
    # Methods for querying                                           #
    ##################################################################

    def lucky(self, query={}, projection=2, offset=0):
        """
        One of the two main public interfaces for performing SELECT queries,
        intended for situations where only a single result is desired.

        INPUT:

        - ``query`` -- a mongo-style dictionary specifying the query.
           Generally, the keys will correspond to columns,
           and values will either be specific numbers (specifying an equality test)
           or dictionaries giving more complicated constraints.
           The main exception is that "$or" can be a top level key,
           specifying a list of constraints of which at least one must be true.
        - ``projection`` -- which columns are desired.
          This can be specified either as a list of columns to include;
           a dictionary specifying columns to include (using all True values)
                                           or exclude (using all False values);
           a string giving a single column (only returns the value, not a dictionary);
           or an integer code (0 means only return the label,
                               1 means return all search columns,
                               2 means all columns (default)).
        - ``offset`` -- integer. allows retrieval of a later record rather than just first.

        OUTPUT:

        If projection is 0 or a string, returns the label/column value of the first record satisfying the query.
        Otherwise, return a dictionary with keys the column names requested by the projection.

        EXAMPLES::

            sage: from lmfdb.db_backend import db
            sage: nf = db.nf_fields
            sage: nf.lucky({'degree':int(2),'disc_sign':int(1),'disc_abs':int(5)},projection=0)
            u'2.2.5.1'
            sage: nf.lucky({'label':u'6.6.409587233.1'},projection=1)
            {u'class_group': [],
             u'class_number': 1,
             u'cm': False,
             u'coeffs': [2, -31, 30, 11, -13, -1, 1],
             u'degree': 6,
             u'disc_abs': 409587233,
             u'disc_rad': 409587233,
             u'disc_sign': 1,
             u'galt': 16,
             u'label': u'6.6.409587233.1',
             u'oldpolredabscoeffs': None,
             u'r2': 0,
             u'ramps': [11, 53, 702551],
             u'used_grh': False}
            sage: nf.lucky({'label':u'6.6.409587233.1'},projection=['reg'])
            {'reg':455.191694993}
        """
        print query
        search_cols, extra_cols, id_offset = self._parse_projection(projection)
        vars = SQL(", ").join(map(Identifier, search_cols))
        qstr, values = self._build_query(query, 1, offset)
        selecter = SQL("SELECT {0} FROM {1}{2}").format(vars, Identifier(self.search_table), qstr)
        cur = self._execute(selecter, values)
        if cur.rowcount > 0:
            rec = cur.fetchone()
            if projection == 0:
                return rec[0]
            elif extra_cols:
                id = rec[0]
                D = {k:v for k,v in zip(search_cols[id_offset:], rec[id_offset:]) if v is not None}
                vars = SQL(", ").join(map(Identifier, extra_cols))
                selecter = SQL("SELECT {0} FROM {1} WHERE id = %s").format(vars, Identifier(self.extra_table))
                cur = self._execute(selecter, [id])
                rec = cur.fetchone()
                for k,v in zip(extra_cols, rec):
                    if v is not None:
                        D[k] = v
                if isinstance(projection, basestring):
                    return D[projection]
                else:
                    return D
            elif isinstance(projection, basestring):
                return rec[0]
            else:
                return {k:v for k,v in zip(search_cols, rec) if v is not None}

    def search(self, query={}, projection=1, limit=None, offset=0, sort=None, info=None, silent=False):
        """
        One of the two main public interfaces for performing SELECT queries,
        intended for usage from search pages where multiple results may be returned.

        INPUT:

        - ``query`` -- a mongo-style dictionary specifying the query.
           Generally, the keys will correspond to columns,
           and values will either be specific numbers (specifying an equality test)
           or dictionaries giving more complicated constraints.
           The main exception is that "$or" can be a top level key,
           specifying a list of constraints of which at least one must be true.
        - ``projection`` -- which columns are desired.
          This can be specified either as a list of columns to include;
           a dictionary specifying columns to include (using all True values)
                                           or exclude (using all False values);
           a string giving a single column (only returns the value, not a dictionary);
           or an integer code (0 means only return the label,
                               1 means return all search columns (default),
                               2 means all columns).
        - ``limit`` -- an integer or None (default), giving the maximum number of records to return.
        - ``offset`` -- an integer (default 0), where to start in the list of results.
        - ``sort`` -- a sort order.  Either None or a list of strings (which are interpreted as column names in the ascending direction) or of pairs (column name, 1 or -1).  If not specified, will use the default sort order on the table.  If you want the result unsorted, use [].
        - ``info`` -- a dictionary, which is updated with values of 'query', 'count', 'start', 'exact_count' and 'number'.  Optional.
        - ``silent`` -- a boolean.  If True, slow query warnings will be suppressed.

        WARNING:

        For tables that are split into a search table and an extras table,
        requesting columns in the extras table via this function will
        require a separate database query for EACH ROW of the result.
        This function is intended for use only on the columns in the search table.

        OUTPUT:

        If ``limit`` is None, returns an iterator over the results, yielding dictionaries with keys the columns requested by the projection (or labels/column values if the projection is 0 or a string)

        Otherwise, returns a list with the same data.

        EXAMPLES::

            sage: from lmfdb.db_backend import db
            sage: nf = db.nf_fields
            sage: info = {}
            sage: nf.search({'degree':int(2),'class_number':int(1),'disc_sign':int(-1)}, projection=0, limit=4, info=info)
            [u'2.0.3.1', u'2.0.4.1', u'2.0.7.1', u'2.0.8.1']
            sage: info['number'], info['exact_count']
            (9, True)
            sage: info = {}
            sage: nf.search({'degree':int(6)}, projection=['label','class_number','galt'], limit=4, info=info)
            [{'class_number': 1, 'galt': 5, 'label': u'6.0.9747.1'},
             {'class_number': 1, 'galt': 11, 'label': u'6.0.10051.1'},
             {'class_number': 1, 'galt': 11, 'label': u'6.0.10571.1'},
             {'class_number': 1, 'galt': 5, 'label': u'6.0.10816.1'}]
            sage: info['number'], info['exact_count']
            (5522600, True)
            sage: info = {}
            sage: nf.search({'ramps':{'$contains':[int(2),int(7)]}}, limit=4, info=info)
            [{'label': u'2.2.28.1', 'ramps': [2, 7]},
             {'label': u'2.0.56.1', 'ramps': [2, 7]},
             {'label': u'2.2.56.1', 'ramps': [2, 7]},
             {'label': u'2.0.84.1', 'ramps': [2, 3, 7]}]
            sage: info['number'], info['exact_count']
            (1000, False)
        """
        search_cols, extra_cols, id_offset = self._parse_projection(projection)
        vars = SQL(", ").join(map(Identifier, search_cols))
        if limit is None:
            qstr, values = self._build_query(query, sort=sort)
        else:
            nres = self.stats.quick_count(query)
            if nres is None:
                prelimit = max(limit, self._count_cutoff - offset)
                qstr, values = self._build_query(query, prelimit, offset, sort)
            else:
                qstr, values = self._build_query(query, limit, offset, sort)
        selecter = SQL("SELECT {0} FROM {1}{2}").format(vars, Identifier(self.search_table), qstr)
        cur = self._execute(selecter, values, silent=silent)
        if limit is None:
            if info is not None:
                # caller is requesting count data
                info['number'] = self.count(query)
            return self._search_iterator(cur, search_cols, extra_cols, id_offset, projection)
        if nres is None:
            exact_count = (cur.rowcount < prelimit)
            nres = offset + cur.rowcount
        else:
            exact_count = True
        res = cur.fetchmany(limit)
        res = list(self._search_iterator(res, search_cols, extra_cols, id_offset, projection))
        if info is not None:
            if offset >= nres:
                offset -= (1 + (offset - nres) / limit) * limit
            if offset < 0:
                offset = 0
            info['query'] = dict(query)
            info['number'] = nres
            info['count'] = limit
            info['start'] = offset
            info['exact_count'] = exact_count
        return res

    def lookup(self, label, projection=2):
        """
        Look up a record by its label.

        INPUT:

        - ``label`` -- string, the label for the desired record.
        - ``projection`` -- which columns are requested (default 2, meaning all columns).
                            See ``_parse_projection`` for more details.

        OUTPUT:

        A dictionary with keys the column names requested by the projection.

        EXAMPLES::

            sage: from lmfdb.db_backend import db
            sage: nf = db.nf_fields
            sage: rec = nf.lookup('8.0.374187008.1')
            sage: rec['loc_algebras']['13']
            u'x^2-13,x^2-x+2,x^4+x^2-x+2'
        """
        if self._label_col is None:
            raise ValueError("Lookup method not supported for tables with no label column")
        return self.lucky({self._label_col:label}, projection=projection)

    def exists(self, query):
        """
        Determines whether there exists at least one record satisfying the query.

        INPUT:

        - ``query`` -- a mongo style dictionary specifying the search.
          See ``search`` for more details.

        OUTPUT:

        Boolean, whether there exists a record.

        EXAMPLES::

            sage: from lmfdb.db_backend import db
            sage: nf = db.nf_fields
            sage: nf.exists({'class_number':int(7)})
            True
        """
        return self.lucky(query, projection=1) is not None

    def random(self, projection=0):
        """
        Return a random label or record from this table.

        INPUT:

        - ``projection`` -- which columns are requested (default 0, meaning just the label).
                            See ``_parse_projection`` for more details.

        OUTPUT:

        If projection is 0, a random label from the table.
        Otherwise, a dictionary with keys specified by the projection.

        EXAMPLES::

            sage: from lmfdb.db_backend import db
            sage: nf = db.nf_fields
            sage: nf.random()
            u'2.0.294787.1'
        """
        maxtries = 100
        maxid = self.max('id')
        for _ in range(maxtries):
            # The id may not exist if rows have been deleted
            rid = random.randint(1, maxid)
            res = self.lucky({'id':rid}, projection=projection)
            if res: return res
        ### This code was used when not every table had an id.
        ## Get the number of pages occupied by the search_table
        #cur = self._execute(SQL("SELECT relpages FROM pg_class WHERE relname = %s"), [self.search_table])
        #num_pages = cur.fetchone()[0]
        ## extra_cols will be () and id_offset will be 0 since there is no id
        #search_cols, extra_cols, id_offset = self._parse_projection(projection)
        #vars = SQL(", ").join(map(Identifier, search_cols))
        #selecter = SQL("SELECT {0} FROM {1} TABLESAMPLE SYSTEM(%s)").format(vars, Identifier(self.search_table))
        ## We select 3 pages in an attempt to not accidentally get nothing.
        #percentage = min(float(300) / num_pages, 100)
        #for _ in range(maxtries):
        #    cur = self._execute(selecter, [percentage])
        #    if cur.rowcount > 0:
        #        return {k:v for k,v in zip(search_cols, random.choice(list(cur)))}
        raise RuntimeError("Random selection failed!")

    ##################################################################
    # Convenience methods for accessing statistics                   #
    ##################################################################

    def max(self, col):
        """
        The maximum value attained by the given column.

        EXAMPLES::

            sage: from lmfdb.db_backend import db
            sage: db.nf_fields.max('class_number')
            1892503075117056
        """
        return self.stats.max(col)

    def distinct(self, col, query={}):
        """
        Returns a list of the distinct values taken on by a given column.
        """
        selecter = SQL("SELECT DISTINCT {0} FROM {1}").format(Identifier(col), Identifier(self.search_table))
        qstr, values = self._parse_dict(query)
        if qstr is not None:
            selecter = SQL("{0} WHERE {1}").format(selecter, qstr)
        selecter = SQL("{0} ORDER BY {1}").format(selecter, Identifier(col))
        cur = self._execute(selecter)
        return [res[0] for res in cur]

    def count(self, query={}):
        """
        Count the number of results for a given query.

        INPUT:

        - ``query`` -- a mongo-style dictionary, as in the ``search`` method.

        OUTPUT:

        The number of records satisfying the query.

        EXAMPLES::

            sage: from lmfdb.db_backend import db
            sage: nf = db.nf_fields
            sage: nf.count({'degree':int(6),'galt':int(7)})
            244006
        """
        return self.stats.count(query)

    ##################################################################
    # Indexes and performance analysis                               #
    ##################################################################

    def analyze(self, query, projection=1, limit=1000, offset=0, sort=None, explain_only=False):
        """
        EXAMPLES::

        sage: from lmfdb.db_backend import db
        sage: nf = db.nf_fields
        sage: nf.analyze({'degree':int(5)},limit=20)
        SELECT label, coeffs, degree, r2, cm, disc_abs, disc_sign, disc_rad, ramps, galt, class_number, class_group, used_grh, oldpolredabscoeffs FROM nf_fields WHERE degree = 5 ORDER BY degree, disc_abs, disc_sign, label LIMIT 20
        Limit  (cost=671790.56..671790.61 rows=20 width=305) (actual time=1947.351..1947.358 rows=20 loops=1)
          ->  Sort  (cost=671790.56..674923.64 rows=1253232 width=305) (actual time=1947.348..1947.352 rows=20 loops=1)
                Sort Key: disc_abs, disc_sign, label COLLATE "C"
                Sort Method: top-N heapsort  Memory: 30kB
                ->  Bitmap Heap Scan on nf_fields  (cost=28589.11..638442.51 rows=1253232 width=305) (actual time=191.837..1115.096 rows=1262334 loops=1)
                      Recheck Cond: (degree = 5)
                      Heap Blocks: exact=35140
                      ->  Bitmap Index Scan on nfs_ddd  (cost=0.00..28275.80 rows=1253232 width=0) (actual time=181.789..181.789 rows=1262334 loops=1)
                            Index Cond: (degree = 5)
        Planning time: 2.880 ms
        Execution time: 1947.655 ms
        """
        search_cols, extra_cols, id_offset = self._parse_projection(projection)
        vars = SQL(", ").join(map(Identifier, search_cols))
        if limit is None:
            qstr, values = self._build_query(query, sort=sort)
        else:
            qstr, values = self._build_query(query, limit, offset, sort)
        selecter = SQL("SELECT {0} FROM {1}{2}").format(vars, Identifier(self.search_table), qstr)
        if explain_only:
            analyzer = SQL("EXPLAIN {0}").format(selecter)
        else:
            analyzer = SQL("EXPLAIN ANALYZE {0}").format(selecter)
        print selecter.as_string(self.conn)%tuple(values)
        cur = self._execute(analyzer, values, silent=True)
        for line in cur:
            print line[0]

    def list_indexes(self):
        """
        Lists the indexes on the search table.
        """
        selecter = SQL("SELECT index_name, type, columns, modifiers FROM meta_indexes WHERE table_name = %s")
        cur = self._execute(selecter, [self.search_table], silent=True)
        for name, typ, columns, modifiers in cur:
            colspec = [" ".join([col] + mods) for col, mods in zip(columns, modifiers)]
            print "{0} ({1}): {2}".format(name, typ, ", ".join(colspec))

    @staticmethod
    def _create_index_statement(name, table, type, columns, modifiers, storage_params):
        """
        Utility function for making the create index SQL statement.
        """
        # We whitelisted the type, modifiers and storage parameters
        # when creating the index so the following is safe from SQL injection
        if storage_params:
            # The inner format is on a string rather than a psycopg2.sql.Composable:
            # the keys of storage_params have been whitelisted.
            storage_params = SQL(" WITH ({0})").format(SQL(", ").join(SQL("{0} = %s".format(param)) for param in storage_params))
        else:
            storage_params = SQL("")
        modifiers = [" " + " ".join(mods) if mods else "" for mods in modifiers]
        # The inner % operator is on strings prior to being wrapped by SQL: modifiers have been whitelisted.
        columns = SQL(", ").join(SQL("{0}%s"%mods).format(Identifier(col)) for col, mods in zip(columns, modifiers))
        # The inner % operator is on strings prior to being wrapped by SQL: type has been whitelisted.
        creator = SQL("CREATE INDEX {0} ON {1} USING %s ({2}){3}"%(type))
        return creator.format(Identifier(name), Identifier(table), columns, storage_params)

    def create_index(self, columns, type="btree", modifiers=None, name=None, storage_params=None):
        """
        Create an index.

        This function will also add the indexing data to the meta_indexes table
        so that indexes can be dropped and recreated when uploading data.

        INPUT:

        - ``columns`` -- a list of column names
        - ``type`` -- one of the postgres index types: btree, gin, gist, brin, hash, spgist.
        - ``modifiers`` -- a list of lists of strings.  The overall length should be
            the same as the length of ``columns``, and each internal list can only contain the
            following whitelisted column modifiers:
            - a non-default operator class
            - ``ASC``
            - ``DESC``
            - ``NULLS FIRST``
            - ``NULLS LAST``
            This interface doesn't currently support creating indexes with nonstandard collations.
        """
        now = time.time()
        if type not in _operator_classes:
            raise ValueError("Unrecognized index type")
        if modifiers is None:
            if type == "gin":
                modifiers = [["jsonb_path_ops"]] * len(columns)
            else:
                modifiers = [[]] * len(columns)
        else:
            if len(modifiers) != len(columns):
                raise ValueError("modifiers must have same length as columns")
            for mods in modifiers:
                for mod in mods:
                    if mod.lower() not in ["asc", "desc", "nulls first", "nulls last"] + _operator_classes[type]:
                        raise ValueError("Invalid modifier %s"%(mod,))
        if storage_params is None:
            if type in ["btree", "hash", "gist", "spgist"]:
                storage_params = {"fillfactor": 100}
            else:
                storage_params = {}
        else:
            for key in storage_params:
                if key not in _valid_storage_params[type]:
                    raise ValueError("Invalid storage parameter %s"%key)
        for col in columns:
            if col != "id" and col not in self._search_cols:
                raise ValueError("%s not a column"%(col))
        if name is None:
            name = "_".join([self.search_table] + columns + ([] if type == "btree" else [type]))
        selecter = SQL("SELECT 1 FROM meta_indexes WHERE index_name = %s AND table_name = %s")
        cur = self._execute(selecter, [name, self.search_table], silent=True)
        if cur.rowcount > 0:
            raise ValueError("Index with that name already exists; try specifying a different name")
        creator = self._create_index_statement(name, self.search_table, type, columns, modifiers, storage_params)
        self._execute(creator, storage_params.values(), silent=True, commit=False)
        inserter = SQL("INSERT INTO meta_indexes (index_name, table_name, type, columns, modifiers, storage_params) VALUES (%s, %s, %s, %s, %s, %s)")
        self._execute(inserter, [name, self.search_table, type, columns, modifiers, storage_params], silent=True, commit=False)
        self.conn.commit()
        print "Index %s created in %.3f secs"%(name, time.time()-now)

    def drop_index(self, name, suffix="", permanent=False):
        """
        Drop a specified index.

        INPUT:

        - ``name`` -- the name of the index
        - ``suffix`` -- a string such as "_tmp" or "_old1" to be appended to the names in the DROP INDEX statement.
        - ``permanent`` -- whether to remove the index from the meta_indexes table
        """
        now = time.time()
        if permanent:
            deleter = SQL("DELETE FROM meta_indexes WHERE table_name = %s AND index_name = %s")
            self._execute(deleter, [self.search_table, name], silent=True, commit=False)
        dropper = SQL("DROP INDEX {0}").format(Identifier(name + suffix))
        self._execute(dropper, silent=True, commit=False)
        self.conn.commit()
        print "Dropped index %s in %.3f secs"%(name, time.time() - now)

    def restore_index(self, name, suffix=""):
        """
        Restore a specified index using the meta_indexes table.

        INPUT:

        - ``name`` -- the name of the index
        - ``suffix`` -- a string such as "_tmp" or "_old1" to be appended to the names in the CREATE INDEX statement.
        """
        now = time.time()
        selecter = SQL("SELECT type, columns, modifiers, storage_params FROM meta_indexes WHERE table_name = %s AND index_name = %s")
        cur = self._execute(selecter, [self.search_table, name], silent=True)
        if cur.rowcount > 1:
            raise RuntimeError("Duplicated rows in meta_indexes")
        elif cur.rowcount == 0:
            raise ValueError("Index %s does not exist in meta_indexes"%(name,))
        type, columns, modifiers, storage_params = cur.fetchone()
        creator = self._create_index_statement(name + suffix, self.search_table + suffix, type, columns, modifiers, storage_params)
        self._execute(creator, storage_params.values(), silent=True)
        print "Created index %s in %.3f secs"%(name, time.time() - now)

    def _indexes_touching(self, columns):
        """
        Utility function for determining which indexes reference any of the given columns.
        """
        selecter = SQL("SELECT index_name FROM meta_indexes WHERE table_name = %s")
        if columns:
            selecter = SQL("{0} AND ({1})").format(selecter, SQL(" OR ").join(SQL("columns @> %s") * len(columns)))
            columns = [[col] for col in columns]
        return self._execute(selecter, [self.search_table] + columns, silent=True)

    def drop_indexes(self, columns=[], suffix=""):
        """
        Drop all indexes, or indexes that refer to any of a list of columns.

        INPUT:

        - ``columns`` -- a list of column names.  If any are included,
            then only indexes referencing those columns will be included.
        - ``suffix`` -- a string such as "_tmp" or "_old1" to be appended to the names in the DROP INDEX statement.
        """
        for res in self._indexes_touching(columns):
            self.drop_index(res[0], suffix)

    def restore_indexes(self, columns=[], suffix=""):
        """
        Restore all indexes using the meta_indexes table, or indexes that refer to any of a list of columns.

        INPUT:

        - ``columns`` -- a list of column names.  If any are included,
            then only indexes referencing those columns will be included.
        - ``suffix`` -- a string such as "_tmp" or "_old1" to be appended to the names in the CREATE INDEX statement.
        """
        for res in self._indexes_touching(columns):
            self.restore_index(res[0], suffix)

    def _pkey_common(self, command, suffix, action, commit):
        """
        Common code for ``drop_pkeys`` and ``restore_pkeys``.

        INPUT:

        - ``command`` -- an sql.Composable object giving the command to execute.
        - ``suffix`` -- a string such as "_tmp" or "_old1" to be appended to the names in the command.
        - ``action`` -- either "Dropped" or "Built", for printing.
        """
        now = time.time()
        # Note that the primary keys don't follow the same convention as the other
        # indexes, since they end in _pkey rather than the suffix.
        self._execute(command.format(Identifier(self.search_table + suffix),
                                     Identifier(self.search_table + suffix + "_pkey")),
                      silent=True, commit=False)
        if self.extra_table is not None:
            self._execute(command.format(Identifier(self.extra_table + suffix),
                                         Identifier(self.extra_table + suffix + "_pkey")),
                          silent=True, commit=False)
        if commit:
            self.conn.commit()
        print "%s primary key on %s in %.3f secs"%(action, self.search_table, time.time()-now)

    def drop_pkeys(self, suffix="", commit=True):
        """
        Drop the primary key on the id columns.

        INPUT:

        - ``suffix`` -- a string such as "_tmp" or "_old1" to be appended to the names in the ALTER TABLE statements.
        """
        command = SQL("ALTER TABLE {0} DROP CONSTRAINT {1}")
        self._pkey_common(command, suffix, "Dropped", commit)

    def restore_pkeys(self, suffix="", commit=True):
        """
        Restore the primary key on the id columns.

        INPUT:

        - ``suffix`` -- a string such as "_tmp" or "_old1" to be appended to the names in the ALTER TABLE statements.
        """
        command = SQL("ALTER TABLE {0} ADD CONSTRAINT {1} PRIMARY KEY (id)")
        self._pkey_common(command, suffix, "Built", commit)

    ##################################################################
    # Insertion and updating data                                    #
    ##################################################################

    def _break_stats(self):
        """
        This function should be called when the statistics are invalidated by an insertion or update.

        Note that this function does not commit to the connection.
        """
        if self._stats_valid:
            # Only need to interact with database in this case.
            updater = SQL("UPDATE meta_tables SET stats_valid = false WHERE name = %s")
            self._execute(updater, [self.search_table], silent=True, commit=False)
            self._stats_valid = False

    def _break_order(self):
        """
        This function should be called when the id ordering is invalidated by an insertion or update.

        Note that this function does not commit to the connection.
        """
        if not self._out_of_order:
            # Only need to interact with database in this case.
            updater = SQL("UPDATE meta_tables SET out_of_order = true WHERE name = %s")
            self._execute(updater, [self.search_table], silent=True, commit=False)
            self._out_of_order = True

    def finalize_changes(self):
        # TODO
        # Update stats.total
        # Refresh stats targets
        # Sort and set self._out_of_order
        pass

    def rewrite(self, func, query={}, resort=True, reindex=True, restat=True, tostr_func=None, **kwds):
        """
        This function can be used to edit some or all records in the table.

        Note that if you want to add new columns, you must explicitly call add_column() first.

        For example, to add a new column to artin_reps that tracks the
        signs of the galois conjugates, you would do the following::

            sage: from lmfdb.db_backend import db
            sage: db.artin_reps.add_column('GalConjSigns','jsonb')
            sage: def add_signs(rec):
            ....:     rec['GalConjSigns'] = sorted(list(set([conj['Sign'] for conj in rec['GaloisConjugates']])))
            ....:     return rec
            sage: db.artin_reps.rewrite(add_signs)
        """
        search_cols = ["id"] + self._search_cols
        if self.extra_table is None:
            projection = search_cols
        else:
            projection = search_cols + self._extra_cols
            extra_cols = ["id"] + self._extra_cols
        # It would be nice to just use Postgres' COPY TO here, but it would then be hard
        # to give func access to the data to process.
        # An alternative approach would be to use COPY TO and have func and filter both
        # operate on the results, but then func would have to process the strings
        if tostr_func is None:
            tostr_func = copy_dumps
        searchfile = tempfile.NamedTemporaryFile('w', delete=False)
        extrafile = EmptyContext() if self.extra_table is None else tempfile.NamedTemporaryFile('w', delete=False)
        try:
            with searchfile:
                with extrafile:
                    for rec in self.search(query, projection=projection, sort=[]):
                        processed = func(rec)
                        searchfile.write(u'\t'.join(tostr_func(processed.get(col), self._col_type[col]) for col in search_cols) + u'\n')
                        if self.extra_table is not None:
                            extrafile.write(u'\t'.join(tostr_func(processed.get(col), self._col_type[col]) for col in extra_cols) + u'\n')
            self.reload(searchfile.name, extrafile.name, includes_ids=True, resort=resort, reindex=reindex, restat=restat, **kwds)
        finally:
            searchfile.unlink(searchfile.name)
            if self.extra_table is not None:
                extrafile.unlink(extrafile.name)

    def delete(self, query, resort=True, restat=True):
        """
        Delete all rows matching the query.
        """
        qstr, values = self._parse_dict(query)
        if qstr is None:
            qstr = SQL("")
        else:
            qstr = SQL(" WHERE {0}").format(qstr)
        deleter = SQL("DELETE FROM {0}{1}").format(Identifier(self.search_table), qstr)
        if self.extra_table is not None:
            deleter = SQL("WITH deleted_ids AS ({0} RETURNING id) DELETE FROM {1} WHERE id IN (SELECT id FROM deleted_ids)").format(deleter, Identifier(self.extra_table))
        cur = self._execute(deleter, values)
        self._break_order()
        self._break_stats()
        self.stats.total -= cur.rowcount
        self.stats._record_count({}, self.stats.total)
        if resort:
            self.resort()
        if restat:
            self.stats.refresh_stats(total = False)

    def upsert(self, query, data):
        """
        Update the unique row satisfying the given query, or insert a new row if no such row exists.
        If more than one row exists, raises an error.

        INPUT:

        - ``query`` -- a dictionary with key/value pairs specifying at most one row of the table.
          The most common case is that there is one key, which is either an id or a label.
        - ``data`` -- a dictionary containing key/value pairs to be set on this row.

        The keys of both inputs must be columns in either the search or extras table.

        Upserting will often break the order constraint if the table is id_ordered,
        so you will probably want to call ``resort`` after all upserts are complete.
        """
        if not query or not data:
            raise ValueError("Both query and data must be nonempty")
        if "id" in data:
            raise ValueError("Cannot set id")
        for col in query:
            if col != "id" and col not in self._search_cols:
                raise ValueError("%s is not a column of %s"%(col, self.search_table))
        if self.extra_table is None:
            search_data = data
            for col in data:
                if col not in self._search_cols:
                    raise ValueError("%s is not a column of %s"%(col, self.search_table))
        else:
            search_data = {}
            extras_data = {}
            for col, val in data.items():
                if col in self._search_cols:
                    search_data[col] = val
                elif col in self._extra_cols:
                    extras_data[col] = val
                else:
                    raise ValueError("%s is not a column of %s"%(col, self.search_table))
        cases = [(self.search_table, search_data)]
        if self.extra_table is not None:
            cases.append((self.extra_table, extras_data))
        # We have to split this command into a SELECT and an INSERT statement
        # rather than using postgres' INSERT INTO ... ON CONFLICT statement
        # because we have to take different additional steps depending on whether
        # an insertion actually occurred
        qstr, values = self._parse_dict(query)
        selecter = SQL("SELECT {0} FROM {1} WHERE {2} LIMIT 2").format(Identifier("id"), Identifier(self.search_table), qstr)
        cur = self._execute(selecter, values, silent=True)
        if cur.rowcount > 1:
            raise ValueError("Query %s does not specify a unique row"%(query))
        elif cur.rowcount == 1: # update
            row_id = cur.fetchone()[0]
            for table, dat in cases:
                updater = SQL("UPDATE {0} SET ({1}) = ({2}) WHERE {3}")
                updater = updater.format(Identifier(table),
                                         SQL(", ").join(map(Identifier, dat.keys())),
                                         SQL(", ").join(Placeholder() * len(dat)),
                                         SQL("id = %s"))
                dvalues = dat.values()
                dvalues.append(row_id)
                self._execute(updater, dvalues, silent=True, commit=False)
            if not self._out_of_order and any(key in self._sort_keys for key in data):
                self._break_order()
        else: # insertion
            if "id" in data or "id" in query:
                raise ValueError("Cannot specify an id for insertion")
            for col, val in query.items():
                if col not in search_data:
                    search_data[col] = val
            # We use the total on the stats object for the new id.  If someone else
            # has inserted data this will be a problem,
            # but it will raise an error rather than leading to invalid database state,
            # so it should be okay.
            search_data["id"] = self.stats.total + 1
            if self.extra_table is not None:
                extras_data["id"] = self.stats.total + 1
            for table, dat in cases:
                inserter = SQL("INSERT INTO {0} ({1}) VALUES ({2})")
                inserter.format(Identifier(table),
                                SQL(", ").join(map(Identifier, dat.keys())),
                                SQL(", ").join(Placeholder() * len(dat)))
                self._execute(inserter, dat.values(), silent=True, commit=False)
            self._break_order()
            self.stats.total += 1
        self._break_stats()
        self.conn.commit()

    def insert_many(self, search_data, extras_data=None, resort=True, reindex=False, restat=True):
        """
        Insert multiple rows.

        This function will be faster than repeated ``upsert`` calls, but slower than ``copy_from``

        INPUT:

        - ``search_data`` -- a list of dictionaries, whose keys are columns and values the values to be set
          in the search table.  All dictionaries should have the same set of keys;
          if this assumption is broken, some values may be set to their default values
          instead of the desired value, or an error may be raised.
        - ``extras_data`` -- a list of dictionaries with data to be inserted into the extras table.
          Must be present, and of the same length as search_data, if the extras table exists.
        - ``resort`` -- whether to sort the ids after copying in the data.  Only relevant for tables that are id_ordered.
        - ``reindex`` -- boolean (default False). Whether to drop the indexes
          before insertion and restore afterward.  Note that if there is an exception during insertion
          the indexes will need to be restored manually using ``restore_indexes``.
        - ``restat`` -- whether to refresh statistics after insertion

        If the search table has an id, the dictionaries will be updated with the ids of the inserted records,
        though note that those ids will change if the ids are resorted.
        """
        if not search_data:
            raise ValueError("No data provided")
        if (extras_data is None) != (self.extra_table is None):
            raise ValueError("extras_data must be present iff extra_table is")
        if extras_data is not None and len(search_data) != len(extras_data):
            raise ValueError("search_data and extras_data must have same length")
        if reindex:
            self.drop_pkeys()
            self.drop_indexes()
        for i, SD in enumerate(search_data):
            SD["id"] = self.stats.total + i + 1
        cases = [(self.search_table, search_data)]
        if extras_data is not None:
            for i, ED in enumerate(extras_data):
                ED["id"] = self.stats.total + i + 1
            cases.append((self.extra_table, extras_data))
        now = time.time()
        for table, L in cases:
            template = SQL("({0})").format(map(Placeholder, L[0].keys()))
            inserter = SQL("INSERT INTO {0} ({1}) VALUES %s")
            inserter = inserter.format(Identifier(table),
                                       SQL(", ").join(map(Identifier, L[0].keys())))
            self._execute(inserter, L, values_list=True, template=template, silent=True, commit=False)
        print "Inserted %s records into %s in %.3f secs"%(len(search_data), self.search_table, time.time()-now)
        self._break_order()
        self._break_stats()
        self.stats.total += len(search_data)
        self.stats._record_count({}, self.stats.total)
        self.conn.commit()
        if resort:
            self.resort()
        if reindex:
            self.restore_pkeys()
            self.restore_indexes()
        if restat:
            self.stats.refresh_stats(total=False)

    def _identify_tables(self, search_table, extra_table):
        """
        Utility function for normalizing input on ``resort``.
        """
        if search_table is not None:
            search_table = Identifier(search_table)
        else:
            search_table = Identifier(self.search_table)
        if extra_table is not None:
            if self.extra_table is None:
                raise ValueError("No extra table")
            extra_table = Identifier(extra_table)
        elif self.extra_table is not None:
            extra_table = Identifier(self.extra_table)
        return search_table, extra_table

    def resort(self, search_table=None, extra_table=None, commit=True):
        """
        Restores the sort order on the id column.

        INPUT:

        - ``search_table`` -- a string giving the name of the search_table to be sorted.
            If None, will use ``self.search_table``; another common input is ``self.search_table + "_tmp"``.
        - ``extra_table`` -- a string giving the name of the extra_table to be sorted.
            If None, will use ``self.extra_table``; another common input is ``self.extra_table + "_tmp"``.
        """
        if self._id_ordered and (search_table is not None or self._out_of_order):
            now = time.time()
            search_table, extra_table = self._identify_tables(search_table, extra_table)
            newid = "newid"
            while newid in self._search_cols or newid in self._extra_cols:
                newid += "_"
            newid = Identifier(newid)
            oldid = Identifier("id")
            addcol = SQL("ALTER TABLE {0} ADD COLUMN {1} bigint")
            dropcol = SQL("ALTER TABLE {0} DROP COLUMN {1}")
            movecol = SQL("ALTER TABLE {0} RENAME COLUMN {1} TO {2}")
            pkey = SQL("ALTER TABLE {0} ADD PRIMARY KEY ({1})")
            self._execute(addcol.format(search_table, newid), silent=True, commit=False)
            updater = SQL("UPDATE {0} SET {1} = newsort.newid FROM (SELECT id, ROW_NUMBER() OVER(ORDER BY {2}) AS newid FROM {0}) newsort WHERE {0}.id = newsort.id")
            updater = updater.format(search_table, newid, self._sort)
            self._execute(updater, silent=True, commit=False)
            if extra_table is not None:
                self._execute(addcol.format(extra_table, newid), silent=True, commit=False)
                updater = SQL("UPDATE {0} SET {1} = search_table.{1} FROM (SELECT id, {1} FROM {2}) search_table WHERE {0}.id = search_table.id")
                updater = updater.format(extra_table, newid, search_table)
                self._execute(updater, silent=True, commit=False)
                self._execute(dropcol.format(extra_table, oldid), silent=True, commit=False)
                self._execute(movecol.format(extra_table, newid, oldid), silent=True, commit=False)
                self._execute(pkey.format(extra_table, oldid), silent=True, commit=False)
            self._execute(dropcol.format(search_table, oldid), silent=True, commit=False)
            self._execute(movecol.format(search_table, newid, oldid), silent=True, commit=False)
            self._execute(pkey.format(search_table, oldid), silent=True, commit=False)
            self._set_ordered(commit=False)
            print "Resorted %s in %.3f secs"%(self.search_table, time.time() - now)
            if commit:
                self.conn.commit()
        elif self._id_ordered:
            print "Data already sorted"
        else:
            print "Data does not have an id column to be sorted"

    def _set_ordered(self, commit=True):
        """
        Marks this table as sorted in meta_tables
        """
        updater = SQL("UPDATE meta_tables SET out_of_order = false WHERE name = %s")
        self._execute(updater, [self.search_table], silent=True, commit=commit)
        self._out_of_order = False

    def _copy_from(self, filename, table, columns, cur_count, includes_ids, kwds):
        """
        Helper function for ``copy_from`` and ``reload``.

        INPUT:

        - ``filename`` -- the filename to load
        - ``table`` -- the table into which the data should be added
        - ``columns`` -- a list of columns specifying the format of the file
        - ``cur_count`` -- the current number of rows in the table
        - ``includes_ids`` -- whether the file starts with an id column.
            If not, a temporary file will be written to.
        - ``kwds`` -- passed on to psycopg2's copy_from
        """
        cur = self.conn.cursor()
        # We have to add quotes manually since copy_from doesn't accept psycopg2.sql.Identifiers
        # None of our column names have double quotes in them. :-D
        columns = ['"' + col + '"' for col in columns]
        if not includes_ids:
            idfile = tempfile.NamedTemporaryFile('w', delete=False)
            try:
                sep = kwds.get("sep", u"\t")
                with open(filename) as F:
                    with idfile:
                        for i, line in enumerate(F):
                            idfile.write((unicode(i + cur_count + 1) + sep + line).encode("utf-8"))
                try:
                    with open(idfile.name) as Fid:
                        cur.copy_from(Fid, table, columns=columns, **kwds)
                        return cur.rowcount
                except Exception:
                    self.conn.rollback()
                    raise
            finally:
                idfile.unlink(idfile.name)
        else:
            with open(filename) as F:
                try:
                    cur.copy_from(F, table, columns=columns, **kwds)
                    return cur.rowcount
                except Exception:
                    self.conn.rollback()
                    raise

    def _clone(self, table, tmp_table, commit=False):
        """
        Utility function: creates a table with the same schema as the given one.
        """
        creator = SQL("CREATE TABLE {0} (LIKE {1})").format(Identifier(tmp_table), Identifier(table))
        self._execute(creator, commit=commit)

    def _swap_in_tmp(self, tables, indexed):
        """
        Helper function for ``reload``: appends _old{n} to the names of tables/indexes/pkeys
        and renames the _tmp versions to the live versions.

        INPUT:

        - ``tables`` -- a list of tables to rename (e.g. self.search_table, self.extra_table, self.stats.counts, self.stats.stats)
        - ``indexed`` -- boolean, whether the temporary table has indexes on it.
        """
        now = time.time()
        backup_number = 1
        for table in tables:
            while self._table_exists("{0}_old{1}".format(table, backup_number)):
                backup_number += 1
        rename_table = SQL("ALTER TABLE {0} RENAME TO {1}")
        rename_pkey = SQL("ALTER TABLE {0} RENAME CONSTRAINT {1} TO {2}")
        rename_index = SQL("ALTER INDEX {0} RENAME TO {1}")
        for table in tables:
            self._execute(rename_table.format(Identifier(table), Identifier("{0}_old{1}".format(table, backup_number))), silent=True, commit=False)
            self._execute(rename_table.format(Identifier(table + "_tmp"), Identifier(table)), silent=True, commit=False)
            self._execute(rename_pkey.format(Identifier("{0}_old{1}".format(table, backup_number)),
                                             Identifier("{0}_pkey".format(table)),
                                             Identifier("{0}_old{1}_pkey".format(table, backup_number))),
                          silent=True, commit=False)
            self._execute(rename_pkey.format(Identifier(table),
                                             Identifier("{0}_tmp_pkey".format(table)),
                                             Identifier("{0}_pkey".format(table))),
                          silent=True, commit=False)
        selecter = SQL("SELECT index_name FROM meta_indexes WHERE table_name = %s")
        cur = self._execute(selecter, [self.search_table], silent=True, commit=False)
        for res in cur:
            self._execute(rename_index.format(Identifier(res[0]), Identifier("{0}_old{1}".format(res[0], backup_number))), silent=True, commit=False)
            if indexed:
                self._execute(rename_index.format(Identifier(res[0] + "_tmp"), Identifier(res[0])), silent=True, commit=False)
        print "Swapped temporary tables for %s into place in %s secs\nNew backup at %s"%(self.search_table, time.time()-now, "{0}_old{1}".format(self.search_table, backup_number))
        self.conn.commit()

    def _check_file_input(self, searchfile, extrafile, kwds):
        """
        Utility function for validating the inputs to ``rewrite``, ``reload`` and ``copy_from``.
        """
        if searchfile is None:
            raise ValueError("Must specify search file")
        if extrafile is not None and self.extra_table is None:
            raise ValueError("No extra table available")
        if extrafile is None and self.extra_table is not None:
            raise ValueError("Must provide file for extra table")
        if "columns" in kwds:
            raise ValueError("Cannot specify column order using the columns parameter")

    def reload(self, searchfile, extrafile=None, countsfile=None, statsfile=None, includes_ids=True, resort=None, reindex=True, restat=None, **kwds):
        """
        Safely and efficiently replaces this table with the contents of one or more files.

        INPUT:

        - ``searchfile`` -- a string, the file with data for the search table
        - ``extrafile`` -- a string, the file with data for the extra table.
            If there is an extra table, this argument is required.
        - ``countsfile`` -- a string (optional), giving a file containing counts information for the table.
        - ``statsfile`` -- a string (optional), giving a file containing stats information for the table.
        - ``includes_ids`` -- whether the search/extra files include ids as the first column.
            If so, the ids should be contiguous, starting immediately after the current max id (or at 1 if empty).
            If the file does not include ids, and this table has ids, the user must have write permission
            to the file's directory: the filename with "_with_ids" will be used as a temporary file.
        - ``resort`` -- whether to sort the ids after copying in the data.  Only relevant for tables that are id_ordered.
        - ``reindex`` -- whether to drop the indexes before importing data and rebuild them afterward.
            If the number of rows is a substantial fraction of the size of the table, this will be faster.
        - ``restat`` -- whether to refresh statistics afterward.  Default behavior is to refresh stats if either countsfile or statsfile is missing.
        - ``kwds`` -- passed on to psycopg2's ``copy_from``.  Cannot include "columns".
        """
        if resort is None:
            resort = not includes_ids
        if restat is None:
            restat = (countsfile is None or statsfile is None)
        self._check_file_input(searchfile, extrafile, kwds)
        tables = []
        counts = {}
        for table, cols, addid, filename in [(self.search_table, self._search_cols, True, searchfile),
                                             (self.extra_table, self._extra_cols, True, extrafile),
                                             (self.stats.counts, ["cols", "values", "count"],False, countsfile),
                                             (self.stats.stats, ["cols", "stat", "value", "constraint_cols", "constraint_values", "threshold"], False, statsfile)]:
            if filename is None:
                continue
            tables.append(table)
            now = time.time()
            if addid:
                cols = ["id"] + cols
            tmp_table = table + "_tmp"
            self._clone(table, tmp_table)
            counts[table] = self._copy_from(filename, tmp_table, cols, 0, includes_ids, kwds)
            print "Loaded data into %s in %.3f secs"%(table, time.time() - now)
        if extrafile is not None and counts[self.search_table] != counts[self.extra_table]:
            self.conn.rollback()
            raise RuntimeError("Different number of rows in searchfile and extrafile")
        if self._id_ordered and resort:
            extra_table = None if self.extra_table is None else self.extra_table + "_tmp"
            self.resort(self.search_table + "_tmp", extra_table)
        else:
            # We still need to build primary keys
            self.restore_pkeys(suffix="_tmp")
        if reindex:
            self.restore_indexes(suffix="_tmp")
        if restat:
            self.stats.refresh_stats(suffix="_tmp")
            for table in [self.stats.counts, self.stats.stats]:
                if table not in tables:
                    tables.append(table)
        self._swap_in_tmp(tables, reindex)
        self.conn.commit()

    def copy_from(self, searchfile, extrafile=None, search_cols=None, extra_cols=None, includes_ids=False, resort=True, reindex=False, restat=True, **kwds):
        """
        Efficiently copy data from files into this table.

        INPUT:

        - ``searchfile`` -- a string, the file with data for the search table
        - ``extrafile`` -- a string, the file with data for the extra table.
            If there is an extra table, this argument is required.
        - ``search_cols`` -- the order of the cols in the search_file, tab-separated.
            Defaults to ``self._search_cols``.  Do not include "id".
        - ``extra_cols`` -- the order of the cols in the extra_file, tab-separated.
            Defaults to ``self._extra_cols``.  Do not include "id".
        - ``includes_ids`` -- whether the search/extra files include ids as the first column.
            If so, the ids should be contiguous, starting immediately after the current max id (or at 1 if empty).
            If the file does not include ids, and this table has ids, the user must have write permission
            to the file's directory: the filename with "_with_ids" will be used as a temporary file.
        - ``resort`` -- whether to sort the ids after copying in the data.  Only relevant for tables that are id_ordered.
        - ``reindex`` -- whether to drop the indexes before importing data and rebuild them afterward.
            If the number of rows is a substantial fraction of the size of the table, this will be faster.
        - ``kwds`` -- passed on to psycopg2's ``copy_from``.  Cannot include "columns".
        """
        self._check_file_input(searchfile, extrafile, kwds)
        if search_cols is None:
            search_cols = self._search_cols
        search_cols = ["id"] + search_cols
        if reindex:
            self.drop_indexes()
        now = time.time()
        search_count = self._copy_from(searchfile, self.search_table, search_cols, self.stats.total, includes_ids, kwds)
        print "Loaded data into %s in %.3f secs"%(self.search_table, time.time() - now)
        if extrafile is not None:
            if extra_cols is None:
                extra_cols = self._extra_cols
            extra_cols = ["id"] + extra_cols
            extra_count = self._copy_from(extrafile, self.extra_table, extra_cols, self.stats.total, includes_ids, kwds)
            if search_count != extra_count:
                self.conn.rollback()
                raise RuntimeError("Different number of rows in searchfile and extrafile")
        self._break_order()
        if self._id_ordered and resort:
            self.resort()
        if reindex:
            self.restore_indexes()
        self._break_stats()
        self.stats.total += search_count
        self.stats._record_count({}, self.stats.total)
        if restat:
            self.stats.refresh_stats(total=False)
        self.conn.commit()

    def copy_to(self, searchfile, extrafile=None, countsfile=None, statsfile=None, include_ids=True, **kwds):
        """
        Efficiently copy data from the database to a file.

        The result will have one line per row of the table, tab separated and in order
        given by self._search_cols and self._extra_cols.

        INPUT:

        - ``searchfile`` -- a string, the file with data for the search table
        - ``extrafile`` -- a string, the file with data for the extra table.
            If there is an extra table, this argument is required.
        - ``countsfile`` -- a string (optional), giving a file containing counts information for the table.
        - ``statsfile`` -- a string (optional), giving a file containing stats information for the table.
        - ``include_ids`` -- whether to include the id column.  Note that this keyword differs from that in ``copy_from`` (no "s")
        - ``kwds`` -- passed on to psycopg2's ``copy_to``.  Cannot include "columns".
        """
        self._check_file_input(searchfile, extrafile, kwds)
        for table, cols, addid, filename in [(self.search_table, self._search_cols, include_ids, searchfile),
                                             (self.extra_table, self._extra_cols, include_ids, extrafile),
                                             (self.stats.counts, ["cols", "values", "count"],False, countsfile),
                                             (self.stats.stats, ["cols", "stat", "value", "constraint_cols", "constraint_values", "threshold"], False, statsfile)]:
            if filename is None:
                continue
            now = time.time()
            if addid:
                cols = ["id"] + cols
            cur = self.conn.cursor()
            with open(filename, "w") as F:
                try:
                    cur.copy_to(F, table, columns=cols, **kwds)
                except Exception:
                    self.conn.rollback()
                    raise
                else:
                    self.conn.commit()
            print "Exported data from %s in %.3f secs"%(table, time.time() - now)

    ##################################################################
    # Updating the schema                                            #
    ##################################################################

    # Note that create_table and drop_table are methods on PostgresDatabase

    def set_sort(self, sort, resort=True):
        """
        Change the default sort order for this table
        """
        self._set_sort(sort)
        if sort:
            updater = SQL("UPDATE meta_tables SET sort = %s WHERE name = %s")
            values = [sort, self.search_table]
        else:
            updater = SQL("UPDATE meta_tables SET sort = NULL WHERE name = %s")
            values = [self.search_table]
        self._execute(updater, values, commit=False)
        self._break_order()
        if resort:
            self.resort() # commits
        else:
            self.conn.commit()

    def add_column(self, name, datatype, extra=False, commit=True):
        if name in self._search_cols: #name == 'id' or 
            raise ValueError("%s already has column %s"%(self.search_table, name))
        if name in self._extra_cols:
            raise ValueError("%s already has column %s"%(self.extra_table, name))
        if datatype.lower() not in types_whitelist:
            if not any(regexp.match(datatype.lower()) for regexp in param_types_whitelist):
                raise ValueError("%s is not a valid type"%(datatype))
        self._col_type[name] = datatype
        if extra:
            if self.extra_table is None:
                raise ValueError("No extra table")
            table = self.extra_table
        else:
            table = self.search_table
        # Since we have run the datatype through the whitelist,
        # the following string substitution is safe
        modifier = SQL("ALTER TABLE {0} ADD COLUMN {1} %s"%datatype).format(Identifier(table), Identifier(name))
        self._execute(modifier, commit=False)
        if extra and name != 'id':
            self._extra_cols.append(name)
        elif not extra and name != 'id':
            self._search_cols.append(name)
        if commit:
            self.conn.commit()

    def drop_column(self, name, commit=True):
        if name in self._sort_keys:
            raise ValueError("Sorting for %s depends on %s; change default sort order with set_sort() before dropping column"%(self.search_table, name))
        if name in self._search_cols:
            table = self.search_table
            deleter = SQL("DELETE FROM meta_indexes WHERE table_name = %s AND columns @> %s")
            self._execute(deleter, [self.search_table, [name]], commit=False)
            self._search_cols.remove(name)
        elif name in self._extra_cols:
            table = self.extra_table
            self._extra_cols.remove(name)
        else:
            raise ValueError("%s is not a column of %s"%(name, self.search_table))
        modifier = SQL("ALTER TABLE {0} DROP COLUMN {1}").format(Identifier(table), Identifier(name))
        self._execute(modifier, commit=commit)
        self._col_type.pop(name, None)

    def create_extra_table(self, columns, ordered=False):
        """
        Splits this search table into two, linked by an id column.

        INPUT:

        - ``columns`` -- columns that are currently in the search table
            that should be moved to the new extra table. Can be empty.
        - ``ordered`` -- whether the id column should be kept in sorted
            order based on the default sort order stored in meta_tables.
        """
        if self.extra_table is not None:
            raise ValueError("Extra table already exists")
        if ordered and not self._id_ordered:
            updater = SQL("UPDATE meta_tables SET (id_ordered, out_of_order, has_extras) = (%s, %s, %s)")
            self._execute(updater, [True, True, True], commit=False)
            self._id_ordered = True
            self._out_of_order = True
            self.resort(commit=False)
        else:
            updater = SQL("UPDATE meta_tables SET (has_extras) = (%s)")
            self._execute(updater, [True], commit=False)
        self.extra_table = self.search_table + "_extras"
        vars = [('id', 'bigint')]
        for col in columns:
            if col not in self._col_type:
                raise ValueError("%s is not a column of %s"%(col, self.search_table))
            if col in self._sort_keys:
                raise ValueError("Sorting for %s depends on %s; change default sort order with set_sort() before moving column to extra table"%(self.search_table, col))
            selecter = SQL("SELECT index_name FROM meta_indexes WHERE table_name = %s AND columns @> %s")
            cur = self._execute(selecter, [self.search_table, [col]], commit=False)
            if cur.rowcount > 0:
                raise ValueError("Indexes (%s) depend on %s"%(", ".join(rec[0] for rec in cur), col))
            typ = self._col_type[col]
            if typ not in types_whitelist:
                if not any(regexp.match(typ.lower()) for regexp in param_types_whitelist):
                    raise RuntimeError("%s is not a valid type"%(typ))
            if typ == 'text' or typ.startswith('char'):
                typ += ' COLLATE "C"'
            vars.append((col, typ))
        self._extra_cols = []
        vars = SQL(", ").join(SQL("{0} %s"%typ).format(Identifier(col)) for col, typ in vars)
        creator = SQL("CREATE TABLE {0} ({1})").format(Identifier(self.extra_table), vars)
        self._execute(creator)
        if columns:
            try:
                transfer_file = tempfile.NamedTemporaryFile('w', delete=False)
                cur = self.conn.cursor()
                with transfer_file:
                    try:
                        cur.copy_to(transfer_file, self.search_table, columns=['id'] + columns)
                    except Exception:
                        self.conn.rollback()
                        raise
                with open(transfer_file.name) as F:
                    try:
                        cur.copy_from(F, self.extra_table, columns=['id'] + columns)
                    except Exception:
                        self.conn.rollback()
                        raise
            finally:
                transfer_file.unlink(transfer_file.name)
            for col in columns:
                modifier = SQL("ALTER TABLE {0} DROP COLUMN {1}").format(Identifier(self.search_table), Identifier(col))
                self._execute(modifier, commit=False)
        else:
            sequencer = SQL("CREATE TEMPORARY SEQUENCE tmp_id")
            self._execute(sequencer, commit=False)
            updater = SQL("UPDATE {0} SET id = nextval('tmp_id')").format(Identifier(self.extra_table))
            self._execute(updater, commit=False)
        self.restore_pkeys(commit=False)
        self.conn.commit()

class PostgresStatsTable(PostgresBase):
    """
    This object is used for storing statistics and counts for a search table.

    INPUT:

    - ``table`` -- a ``PostgresTable`` object.
    """
    def __init__(self, table):
        PostgresBase.__init__(self, table.search_table, table.conn)
        self.table = table
        self.search_table = st = table.search_table
        self.stats = st + "_stats"
        self.counts = st + "_counts"
        self.total = self.quick_count({})
        if self.total is None:
            self.total = self._slow_count({}, record=True)

    def _has_stats(self, jcols, ccols, cvals, threshold):
        """
        Checks whether statistics have been recorded for a given set of columns.
        It just checks whether the "total" stat has been computed.

        INPUT:

        - ``jcols`` -- a list of the columns to be accumulated.
        - ``ccols`` -- a list of the constraint columns.
        - ``cvals`` -- a list of the values required for the constraint columns.
        - ``threshold`` -- an integer: if the number of rows with a given tuple of
           values for the accumulated columns is less than this threshold, those
           rows are thrown away.
        """
        values = [jcols, "total"]
        if ccols is None:
            ccols = "constraint_cols IS NULL"
            cvals = "constraint_values IS NULL"
        else:
            values.extend([ccols, cvals])
            ccols = "constraint_cols = %s"
            cvals = "constraint_values = %s"
        if threshold is None:
            threshold = "threshold IS NULL"
        else:
            values.append(threshold)
            threshold = "threshold = %s"
        selecter = SQL("SELECT 1 FROM {0} WHERE cols = %s AND stat = %s AND {1} AND {2} AND {3}")
        selecter = selecter.format(Identifier(self.stats), SQL(ccols), SQL(cvals), SQL(threshold))
        cur = self._execute(selecter, values)
        return cur.rowcount > 0

    def quick_count(self, query):
        """
        Tries to quickly determine the number of results for a given query
        using the count table.

        INPUT:

        - ``query`` -- a mongo-style dictionary, as in the ``search`` method.

        OUTPUT:

        Either an integer giving the number of results, or None if not cached.
        """
        cols, vals = self._split_dict(query)
        selecter = SQL("SELECT count FROM {0} WHERE cols = %s AND values = %s").format(Identifier(self.counts))
        cur = self._execute(selecter, [cols, vals])
        if cur.rowcount:
            return int(cur.fetchone()[0])

    def _slow_count(self, query, record=False):
        """
        No shortcuts: actually count the rows in the search table.

        INPUT:

        - ``query`` -- a mongo-style dictionary, as in the ``search`` method.
        - ``record`` -- boolean (default False).  Whether to store the result in the count table.

        OUTPUT:

        The number of rows in the search table satisfying the query.
        """
        selecter = SQL("SELECT COUNT(*) FROM {0}").format(Identifier(self.search_table))
        qstr, values = self.table._parse_dict(query)
        if qstr is not None:
            selecter = SQL("{0} WHERE {1}").format(selecter, qstr)
        cur = self._execute(selecter, values)
        nres = cur.fetchone()[0]
        if record:
            self._record_count(query, nres)
        return nres

    def _record_count(self, query, count):
        cols, vals = self._split_dict(query)
        if self.quick_count(query) is None:
            updater = SQL("INSERT INTO {0} (count, cols, values) VALUES (%s, %s, %s)")
        else:
            updater = SQL("UPDATE {0} SET count = %s WHERE cols = %s AND values = %s")
        self._execute(updater.format(Identifier(self.counts)), [count, cols, vals])

    def count(self, query={}):
        """
        Count the number of results for a given query.

        INPUT:

        - ``query`` -- a mongo-style dictionary, as in the ``search`` method.

        OUTPUT:

        The number of records satisfying the query.

        EXAMPLES::

            sage: from lmfdb.db_backend import db
            sage: nf = db.nf_fields
            sage: nf.stats.count({'degree':int(6),'galt':int(7)})
            244006
        """
        if not query:
            return self.total
        nres = self.quick_count(query)
        if nres is None:
            nres = self._slow_count(query)
        return int(nres)

    def max(self, col):
        """
        The maximum value attained by the given column, which must be in the search table.

        EXAMPLES::

            sage: from lmfdb.db_backend import db
            sage: db.nf_fields.stats.max('class_number')
            1892503075117056
        """
        if col == "id":
            # We just use the count in this case
            return self.count() - 1
        if col not in self.table._search_cols:
            raise ValueError("%s not a column of %s"%(col, self.search_table))
        selecter = SQL("SELECT value FROM {0} WHERE stat = %s AND cols = %s AND threshold IS NULL AND constraint_cols IS NULL")
        cur = self._execute(selecter.format(Identifier(self.stats)), ["max", [col]])
        if cur.rowcount:
            return cur.fetchone()[0]
        selecter = SQL("SELECT {0} FROM {1} ORDER BY {0} DESC LIMIT 1")
        cur = self._execute(selecter.format(Identifier(col), Identifier(self.search_table)))
        m = cur.fetchone()[0]
        if m is None:
            # the default order ends with NULLs, so we now have to use NULLS LAST,
            # preventing the use of indexes.
            selecter = SQL("SELECT {0} FROM {1} ORDER BY {0} DESC NULLS LAST LIMIT 1")
            cur = self._execute(selecter.format(Identifier(col), Identifier(self.search_table)))
            m = cur.fetchone()[0]
        try:
            inserter = SQL("INSERT INTO {0} (cols, stat, value) VALUES (%s, %s, %s)")
            self._execute(inserter.format(Identifier(self.stats)), [[col], "max", m])
        except Exception:
            pass
        return m

    def _split_buckets(self, buckets, constraint, include_upper=True):
        """
        Utility function for adding buckets to a constraint

        INPUT:

        - ``buckets`` -- a dictionary whose keys are columns, and whose values are lists of break points.
            The buckets are the values between these break points.  Repeating break points
            makes one bucket consist of just that point.
        - ``constraint`` -- a dictionary giving additional constraints on other columns.
        - ``include_upper`` -- whether to use intervals of the form A < x <= B (vs A <= x < B).

        OUTPUT:

        Iterates over the cartesian product of the buckets formed, yielding in each case
        a dictionary that can be used as a query.
        """
        expanded_buckets = []
        for col, divisions in buckets.items():
            expanded_buckets.append([])
            if len(divisions) < 2:
                raise ValueError
            divisions = [None] + sorted(divisions) + [None]
            for a,b,c,d in zip(divisions[:-3],divisions[1:-2],divisions[2:-1],divisions[3:]):
                if b == c:
                    expanded_buckets[-1].append({col:b})
                else:
                    if include_upper:
                        gt = True
                        lt = (c == d)
                    else:
                        lt = True
                        gt = (a == b)
                    expanded_buckets[-1].append({col:{"$gt" if gt else "$gte": b,
                                                      "$lt" if lt else "$lte": c}})
        for X in cartesian_product_iterator(expanded_buckets):
            bucketed_constraint = dict(constraint) # copy
            for D in X:
                bucketed_constraint.update(D)
            yield bucketed_constraint

    def add_bucketed_counts(self, cols, buckets, constraint={}, include_upper=True):
        """
        A convenience function for adding statistics on a given set of columns,
        where rows are grouped into intervals by a bucketing dictionary.

        See the ``add_stats`` mehtod for the actual statistics computed.

        INPUT:

        - ``buckets`` -- a dictionary whose keys are columns, and whose values are lists of break points.
            The buckets are the values between these break points.  Repeating break points
            makes one bucket consist of just that point.
        - ``constraint`` -- a dictionary giving additional constraints on other columns.
        - ``include_upper`` -- whether to use intervals of the form A < x <= B (vs A <= x < B).
        """
        # Need to check that the buckets cover all cases.
        for bucketed_constraint in self._split_buckets(buckets, constraint, include_upper):
            self.add_stats(cols, bucketed_constraint)

    def _split_dict(self, D):
        """
        A utility function for splitting a dictionary into parallel lists of keys and values.
        """
        if D:
            return zip(*sorted(D.items()))
        else:
            return [], []

    def add_stats(self, cols, constraint=None, threshold=None):
        """
        Add statistics on counts, average, min and max values for a given set of columns.

        INPUT:

        - ``cols`` -- a list of columns, usually of length 1 or 2.
        - ``constraint`` -- only rows satisfying this constraint will be considered.
            It should take the form of a dictionary of the form used in search queries.
        - ``threshold`` -- an integer or None.

        OUTPUT:

        Counts for each distinct tuple of values will be stored,
        as long as the number of rows sharing that tuple is above
        the given threshold.  If there is only one column and it is numeric,
        average, min, and max will be computed as well.
        """
        cols = sorted(cols)
        where = SQL(" WHERE {0}").format(SQL(" AND ").join(SQL("{0} IS NOT NULL").format(Identifier(col)) for col in cols))
        values, ccols, cvals = [], None, None
        if constraint is None:
            allcols = cols
        else:
            # We need to include the constraints in the count table if we're not grouping by that column
            allcols = sorted(list(set(cols + constraint.keys())))
            if any(key.startswith('$') for key in constraint.keys()):
                raise ValueError("Top level special keys not allowed")
            ccols, cvals = self._split_dict(constraint)
            qstr, values = self.table._parse_dict(constraint)
            if qstr is not None:
                where = SQL(" AND ").join([where, qstr])
        if self._has_stats(cols, ccols, cvals, threshold):
            return
        self.logger.info("Adding stats for {0} ({1})".format(", ".join(cols), "no threshold" if threshold is None else "threshold = %s"%threshold))
        having = SQL("")
        if threshold is not None:
            having = SQL(" HAVING COUNT(*) >= {0}").format(Literal(threshold))
        if cols:
            vars = SQL(", ").join(map(Identifier, cols))
            groupby = SQL(" GROUP BY {0}").format(vars)
            vars = SQL("{0}, COUNT(*)").format(vars)
        else:
            vars = SQL("COUNT(*)")
            groupby = SQL("")
            if not allcols:
                where = SQL("")
        selecter = SQL("SELECT {vars} FROM {table}{where}{groupby}{having}").format(vars=vars, table=Identifier(self.search_table), groupby=groupby, where=where, having=having)
        cur = self._execute(selecter, values, silent=True)
        to_add = []
        total = 0
        onenumeric = False # whether we're grouping by a single numeric column
        if len(cols) == 1:
            col = cols[0]
            if self.table._col_type.get(col) in ["numeric", "bigint", "integer", "smallint", "double precision"]:
                onenumeric = True
                avg = 0
                mn = None
                mx = None
        for countvec in cur:
            colvals, count = countvec[:-1], countvec[-1]
            if constraint is None:
                allcolvals = colvals
            else:
                allcolvals = []
                i = 0
                for col in allcols:
                    if col in cols:
                        allcolvals.append(colvals[i])
                        i += 1
                    else:
                        allcolvals.append(constraint[col])
            to_add.append((allcols, allcolvals, count))
            total += count
            if onenumeric:
                val = colvals[0]
                avg += val * count
                if mn is None or val < mn:
                    mn = val
                if mx is None or val > mx:
                    mx = val
        stats = [(cols, "total", total, ccols, cvals, threshold)]
        if onenumeric:
            avg = float(avg) / total
            stats.append((cols, "avg", avg, ccols, cvals, threshold))
            stats.append((cols, "min", mn, ccols, cvals, threshold))
            stats.append((cols, "max", mx, ccols, cvals, threshold))
        # Note that the cols in the stats table does not add the constraint columns, while in the counts table it does.
        inserter = SQL("INSERT INTO {0} (cols, stat, value, constraint_cols, constraint_values, threshold) VALUES %s")
        self._execute(inserter.format(Identifier(self.stats)), stats, values_list=True, silent=True)
        inserter = SQL("INSERT INTO {0} (cols, values, count) VALUES %s")
        self._execute(inserter.format(Identifier(self.counts)), to_add, values_list=True, silent=True)

    def refresh_stats(self, total=True, suffix=None):
        pass

    def _get_values_counts(self, cols, constraint):
        """
        Utility function used in ``display_data``.

        Returns a list of pairs (value, count), where value is a list of values taken on by the specified
        columns and count is an integer giving the number of rows with those values.

        INPUT:

        - ``cols`` -- a list of column names that are stored in the counts table.
        - ``constraint`` -- a dictionary specifying a constraint on rows to consider.
        """
        selecter_constraints = [SQL("cols = %s")]
        if constraint:
            allcols = sorted(list(set(cols + constraint.keys())))
            positions = [allcols.index(x) for x in cols]
            selecter_values = [allcols]
            for i, x in enumerate(allcols):
                if x in constraint:
                    selecter_constraints.append(SQL("values->{0} = %s".format(i)))
                    selecter_values.append(constraint[x])
        else:
            selecter_values = [cols]
            positions = range(len(cols))
        selecter = SQL("SELECT values, count FROM {0} WHERE {1}").format(Identifier(self.counts), SQL(" AND ").join(selecter_constraints))
        return [([values[i] for i in positions], int(count)) for values, count in self._execute(selecter, values=selecter_values)]

    def _get_total_avg(self, cols, constraint, include_avg):
        """
        Utility function used in ``display_data``.

        Returns the total number of rows and average value for the column, subject to the given constraint.

        INPUT:

        - ``cols`` -- a list of columns
        - ``constraint`` -- a dictionary specifying a constraint on rows to consider.
        - ``include_avg`` -- boolean, whether to compute the average.

        OUTPUT:

        - the total number of rows satisying the constraint
        - the average value of the given column (only possible if cols has length 1), or None if the average not requested.
        """
        totaler = SQL("SELECT value FROM {0} WHERE cols = %s AND stat = %s AND threshold IS NULL").format(Identifier(self.stats))
        if constraint:
            ccols, cvals = self._split_dict(constraint)
            totaler = SQL("{0} AND constraint_cols = %s AND constraint_values = %s").format(totaler)
            totaler_values = [cols, "total", ccols, cvals]
        else:
            totaler = SQL("{0} AND constraint_cols IS NULL").format(totaler)
            totaler_values = [cols, "total"]
        cur_total = self._execute(totaler, values=totaler_values)
        if cur_total.rowcount == 0:
            raise ValueError("Database does not contain stats for %s"%(cols[0],))
        total = cur_total.fetchone()[0]
        if include_avg:
            # Modify totaler_values in place since query for avg is very similar
            totaler_values[1] = "avg"
            cur_avg = self._execute(totaler, values=totaler_values)
            avg = cur_avg.fetchone()[0]
        else:
            avg = None
        return total, avg

    def display_data(self, cols, base_url, constraint=None, include_avg=False, formatter=None, buckets = None, include_upper=True, query_formatter=None, count_key='count'):
        """
        Returns statistics data in a common format that is used by page templates.

        INPUT:

        - ``cols`` -- a list of column names
        - ``base_url`` -- a base url, to which col=value tags are appended.
        - ``constraint`` -- a dictionary giving constraints on other columns.
            Only rows satsifying those constraints are included in the counts.
        - ``include_avg`` -- whether to include the average value of cols[0]
            (cols must be of length 1 with no bucketing)
        - ``formatter`` -- a function applied to the tuple of values for display.
        - ``buckets`` -- a dictionary whose keys are columns, and whose values are lists of break points.
            The buckets are the values between these break points.  Repeating break points
            makes one bucket consist of just that point.  Values of these columns
            are grouped based on which bucket they fall into.
        - ``include_upper`` -- For bucketing, whether to use intervals of the form A < x <= B (vs A <= x < B).
        - ``query_formatter`` -- a function for encoding the values into the url.
        - ``count_key`` -- the key to use for counts in the returned dictionaries.

        OUTPUT:

        A list of dictionaries, each with four keys.
        - ``value`` -- a tuple of values taken on by the given columns.
        - ``count_key`` -- (this key specified by the input parameter).  The number of rows with that tuple of values.
        - ``query`` -- a url resulting in a list of entries with the given tuple of values.
        - ``proportion`` -- the fraction of rows having this tuple of values, as a string formatted as a percentage.
        """
        if formatter is None:
            formatter = lambda x: x
        if len(cols) == 1 and buckets is None:
            if query_formatter is None:
                query_formatter = lambda x: str(x)
            col = cols[0]
            total, avg = self._get_total_avg(cols, constraint, include_avg)
            data = [(values[0], count) for values, count in self._get_values_counts(cols, constraint)]
            data.sort()
        elif len(cols) == 0 and buckets is not None and len(buckets) == 1:
            if include_avg:
                raise ValueError
            if query_formatter is None:
                def query_formatter(x):
                    if isinstance(x, dict):
                        a = x.get('$gte',x['$gt']+1)
                        b = x.get('$lte',x['$lt']-1)
                        return "{0}-{1}".format(a,b)
                    return str(x)
            col = buckets.keys()[0]
            total = 0
            data = []
            for bucketed_constraint in self._split_buckets(buckets, constraint, include_upper):
                L = self._get_values_counts(cols, bucketed_constraint)
                if len(L) != 1:
                    raise RuntimeError
                cnt = L[0][1]
                data.append((bucketed_constraint[col], cnt))
                total += cnt
        else:
            raise NotImplementedError
        data = [{'value':formatter(value),
                 count_key:count,
                 'query':"{0}?{1}={2}".format(base_url, col, query_formatter(value)),
                 'proportion':format_percentage(count, total)}
                for value, count in data]
        if include_avg:
            data.append({'value':'\(\\mathrm{avg}\\ %.2f\)'%avg,
                         count_key:total,
                         'query':"{0}?{1}".format(base_url, cols[0]),
                         'proportion':format_percentage(1,1)})
        return data

    def create_oldstats(self, filename):
        name = self.search_table + "_oldstats"
        creator = SQL('CREATE TABLE {0} (_id text COLLATE "C", data jsonb)').format(Identifier(name))
        self._execute(creator)
        self.table._db.grant_select(name)
        cur = self.conn.cursor()
        with open(filename) as F:
            try:
                cur.copy_from(F, self.search_table + "_oldstats")
            except Exception:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()
                print "Oldstats created successfully"

    def get_oldstat(self, name):
        selecter = SQL("SELECT data FROM {0} WHERE _id = %s").format(Identifier(self.search_table + "_oldstats"))
        cur = self._execute(selecter, [name])
        if cur.rowcount != 1:
            raise ValueError("Not a unique oldstat identifier")
        return cur.fetchone()[0]

class ExtendedTable(PostgresTable):
    """
    This class supports type conversion when extracting data from the database.

    It's use is currently hardcoded for artin_reps and artin_field_data,
    but could eventually be specified by columns in meta_tables.
    """
    def __init__(self, type_conversion, *args, **kwds):
        self._type_conversion = type_conversion
        PostgresTable.__init__(self, *args, **kwds)
    def _search_and_convert_iterator(self, source):
        for x in source:
            yield self._type_conversion(x)
    def search_and_convert(self, query={}, projection=1, limit=None, offset=0, sort=None, info=None):
        results = self.search(query, projection, limit=limit, offset=offset, sort=sort, info=info)
        if limit is None:
            return self._search_and_convert_iterator(results)
        else:
            return [self._type_conversion(x) for x in results]
    def convert_lucky(self, *args, **kwds):
        result = self.lucky(*args, **kwds)
        if result:
            return self._type_conversion(result)

class PostgresDatabase(PostgresBase):
    """
    The interface to the postgres database.

    It creates and stores the global connection object,
    and collects the table interfaces.

    INPUT:

    - ``tables`` -- the information needed to construct the table interfaces.
      Namely, a list, each entry of which is either a string (the name of the search table)
      or a tuple which is passed on to the PostgresTable constructor.

    EXAMPLES::

        sage: from lmfdb.db_backend import db
        sage: db
        Interface to Postgres database
        sage: db.conn
        <connection object at 0x...>
        sage: db.tablenames[:3]
        ['artin_field_data', 'artin_reps', 'av_fqisog']
        sage: db.av_fqisog
        Interface to Postgres table av_fqisog
    """
    def __init__(self):
        from lmfdb.config import Configuration
        options = Configuration().get_postgresql();
        self.fetch_userpassword(options);
        logging.info("Connecting to PostgresSQL...")
        connection = connect( **options)
        logging.info("Done!\n connection = %s" % connection)
        PostgresBase.__init__(self, 'db_all', connection)
        # The following function controls how Python classes are converted to
        # strings for passing to Postgres, and how the results are decoded upon
        # extraction from the database.
        # Note that it has some global effects, since register_adapter
        # is not limited to just one connection
        setup_connection(self.conn)
        cur = self._execute(SQL("SELECT name, label_col, sort, count_cutoff, id_ordered, out_of_order, has_extras, stats_valid FROM meta_tables"))

        if options['user'] == "webserver":
            self._execute(SQL("SET SESSION statement_timeout = '25s'"))
        self.tablenames = []
        for tabledata in cur:
            tablename = tabledata[0]
            # it would be nice to include this in meta_tables
            if tablename == 'artin_reps':
                table = ExtendedTable(Dokchitser_ArtinRepresentation, self, *tabledata)
            elif tablename == 'artin_field_data':
                table = ExtendedTable(Dokchitser_NumberFieldGaloisGroup, self, *tabledata)
            else:
                table = PostgresTable(self, *tabledata)
            self.__dict__[tablename] = table
            self.tablenames.append(tablename)
        self.tablenames.sort()

    def __repr__(self):
        return "Interface to Postgres database"

    def fetch_userpassword(self, options):
        if 'user' not in options:
            options['user'] = 'lmfdb'

        if options['user'] == 'webserver':
            logging.info("Fetching webserver password...")
            # tries to read the file "password" on root of the project
            pw_filename = os.path.join(os.path.dirname(os.path.dirname(__file__)), "password")
            try:
                password = open(pw_filename, "r").readlines()[0].strip()
                logging.info("Done!")
            except Exception:
                # file not found or any other problem
                # this is read-only everywhere
                logging.warning("PostgresSQL authentication: no webserver password -- fallback to read-only access")
                options['user'], options['password'] = ['lmfdb', 'lmfdb']

        elif 'password' not in options:
            options['user'], options['password'] = ['lmfdb', 'lmfdb']

    def _grant(self, action, table_name, users, commit):
        action = action.upper()
        if action not in ['SELECT', 'INSERT', 'UPDATE', 'DELETE']:
            raise ValueError("%s is not a valid action"%action)
        grantor = SQL('GRANT %s ON TABLE {0} TO {1}'%action)
        for user in users:
            self._execute(grantor.format(Identifier(table_name), Identifier(user)), silent=True, commit=False)
        if commit:
            self.conn.commit()
    def grant_select(self, table_name, users=['lmfdb', 'webserver'], commit=True):
        self._grant("SELECT", table_name, users, commit)
    def grant_insert(self, table_name, users=['webserver'], commit=True):
        self._grant("INSERT", table_name, users, commit)
    def grant_update(self, table_name, users=['webserver'], commit=True):
        self._grant("UPDATE", table_name, users, commit)
    def grant_delete(self, table_name, users=['webserver'], commit=True):
        self._grant("DELETE", table_name, users, commit)

    def is_alive(self):
        """
        Check that the connection to the database is active.
        """
        try:
            cur = self._execute(SQL("SELECT 1"))
            if cur.rowcount == 1:
                return True
        except Exception:
            pass
        return False

    def __getitem__(self, name):
        if name in self.tablenames:
            return getattr(self, name)
        else:
            raise ValueError

    def create_table(self, name, search_columns, label_col, sort=None, id_ordered=None, extra_columns=None, search_order=None, extra_order=None):
        """
        Add a new search table to the database.

        INPUT:

        - ``name`` -- the name of the table.  See existing names for consistency.
        - ``search_columns`` -- a dictionary whose keys are valid postgres types and whose values
            are lists of column names (or just a string if only one column has the specified type).
            An id column of type bigint will be added as a primary key (do not include it).
        - ``label_col`` -- the column holding the LMFDB label.  This will be used in the ``lookup`` method
            and in the display of results on the API.  Use None if there is no appropriate column.
        - ``sort`` -- If not None, provides a default sort order for the table, in formats accepted by
            the ``_sort_str`` method.
        - ``id_ordered`` -- boolean (default None).  If set, the table will be sorted by id when
            pushed to production, speeding up some kinds of search queries.  Defaults to True
            when sort is not None.
        - ``extra_columns`` -- a dictionary in the same format as the search_columns dictionary.
            If present, will create a second table (the name with "_extras" appended), linked by
            an id column.  Data in this table cannot be searched on, but will also not appear
            in the search table, speeding up scans.
        - ``search_order`` -- (optional) list of column names, specifying the default order of columns
        - ``extra_order`` -- (optional) list of column names, specifying the default order of columns

        COMMON TYPES:

        The postgres types most commonly used in the lmfdb are:

        - smallint -- a 2-byte signed integer.
        - integer -- a 4-byte signed integer.
        - bigint -- an 8-byte signed integer.
        - numeric -- exact, high precision integer or decimal.
        - real -- a 4-byte float.
        - double precision -- an 8-byte float.
        - text -- string (see collation note above).
        - boolean -- true or false.
        - jsonb -- data iteratively built from numerics, strings, booleans, nulls, lists and dictionaries.
        - timestamp -- 8-byte date and time with no timezone.
        """
        if name in self.tablenames:
            raise ValueError("%s already exists"%name)
        now = time.time()
        if id_ordered is None:
            id_ordered = (sort is not None)
        for typ, L in search_columns.items():
            if isinstance(L, basestring):
                search_columns[typ] = [L]
        valid_list = sum(search_columns.values(),[])
        valid_set = set(valid_list)
        # Check that columns aren't listed twice
        if len(valid_list) != len(valid_set):
            C = Counter(valid_list)
            raise ValueError("Column %s repeated"%(C.most_common(1)[0][0]))
        # Check that label_col is valid
        if label_col is not None and label_col not in valid_set:
            raise ValueError("label_col must be a search column")
        # Check that sort is valid
        if sort is not None:
            for col in sort:
                if isinstance(col, tuple):
                    if len(col) != 2:
                        raise ValueError("Sort terms must be either strings or pairs")
                    if col[1] not in [1, -1]:
                        raise ValueError("Sort terms must be of the form (col, 1) or (col, -1)")
                    col = col[0]
                if col not in valid_set:
                    raise ValueError("Column %s does not exist"%(col))
        # Check that search order is valid
        if search_order is not None:
            for col in search_order:
                if col not in valid_set:
                    raise ValueError("Column %s does not exist"%(col))
            if len(search_order) != len(valid_set):
                raise ValueError("Must include all columns")
        def process_columns(coldict, colorder):
            allcols = {}
            hasid = False
            dictorder = []
            for typ, cols in coldict.items():
                if typ.lower() not in types_whitelist:
                    if not any(regexp.match(typ.lower()) for regexp in param_types_whitelist):
                        raise ValueError("%s is not a valid type"%(typ))
                if isinstance(cols, basestring):
                    cols = [cols]
                for col in cols:
                    if col == 'id':
                        hasid = True
                    # We have whitelisted the types, so it's okay to use string formatting
                    # to insert them into the SQL command.
                    # This is useful so that we can specify the collation in the type
                    allcols[col] = SQL("{0} " + typ).format(Identifier(col))
                    dictorder.append(col)
            allcols = [allcols[col] for col in (dictorder if colorder is None else colorder)]
            if (not hasid):
                allcols.insert(0, SQL("id bigint"))
            return allcols
        search_columns = process_columns(search_columns, search_order)
        creator = SQL('CREATE TABLE {0} ({1})').format(Identifier(name), SQL(", ").join(search_columns))
        self._execute(creator, silent=True, commit=False)
        self.grant_select(name)
        if extra_columns is not None:
            valid_extra_list = sum(extra_columns.values(),[])
            valid_extra_set = set(valid_extra_list)
            # Check that columns aren't listed twice
            if len(valid_extra_list) != len(valid_extra_set):
                C = Counter(valid_extra_list)
                raise ValueError("Column %s repeated"%(C.most_common(1)[0][0]))
            if extra_order is not None:
                for col in extra_order:
                    if col not in valid_extra_set:
                        raise ValueError("Column %s does not exist"%(col))
                if len(extra_order) != len(valid_extra_set):
                    raise ValueError("Must include all columns")
            extra_columns = process_columns(extra_columns, extra_order)
            creator = SQL('CREATE TABLE {0} ({1})')
            creator = creator.format(Identifier(name+"_extras"),
                                     SQL(", ").join(extra_columns))
            self._execute(creator, silent=True, commit=False)
            self.grant_select(name+"_extras")
        creator = SQL('CREATE TABLE {0} (cols jsonb, values jsonb, count bigint)')
        creator = creator.format(Identifier(name+"_counts"))
        self._execute(creator, silent=True, commit=False)
        self.grant_select(name+"_counts")
        self.grant_insert(name+"_counts")
        creator = SQL('CREATE TABLE {0} (cols jsonb, stat text COLLATE "C", value numeric, constraint_cols jsonb, constraint_values jsonb, threshold integer)')
        creator = creator.format(Identifier(name + "_stats"))
        self._execute(creator, silent=True, commit=False)
        self.grant_select(name+"_stats")
        inserter = SQL('INSERT INTO meta_tables (name, sort, id_ordered, out_of_order, has_extras, label_col) VALUES (%s, %s, %s, %s, %s, %s)')
        self._execute(inserter, [name, sort, id_ordered, not id_ordered, extra_columns is not None, label_col], silent=True, commit=False)
        print "Table %s created in %.3f secs"%(name, time.time()-now)
        self.conn.commit()
        self.__dict__[name] = PostgresTable(self, name, label_col, sort=sort, id_ordered=id_ordered, out_of_order=(not id_ordered), has_extras=(extra_columns is not None))
        self.tablenames.append(name)
        self.tablenames.sort()

    def drop_table(self, name, commit=True):
        if name not in self.tablenames:
            raise ValueError("%s is not a search table")
        table = getattr(self, name)
        indexes = list(self._execute(SQL("SELECT index_name FROM meta_indexes WHERE table_name = %s"), [name]))
        if indexes:
            self._execute(SQL("DELETE FROM meta_indexes WHERE table_name = %s"), [name], commit=False)
            print "Deleted indexes {0}".format(", ".join(index[0] for index in indexes))
        self._execute(SQL("DELETE FROM meta_tables WHERE name = %s"), [name], commit=False)
        if table.extra_table is not None:
            self._execute(SQL("DROP TABLE {0}").format(Identifier(table.extra_table)), commit=False)
            print "Dropped {0}".format(table.extra_table)
        for tbl in [name, name + "_counts", name + "_stats"]:
            self._execute(SQL("DROP TABLE {0}").format(Identifier(tbl)), commit=False)
            print "Dropped {0}".format(tbl)
        self.tablenames.remove(name)
        if commit:
            self.conn.commit()

db = PostgresDatabase()
