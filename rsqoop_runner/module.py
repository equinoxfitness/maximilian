#!/usr/bin/env python
"""
module for performing simple extract-load operations from mssql to redshift
"""

import csv
import os
import gzip as gz
import json
import argparse
import time
from time import sleep
from datetime import datetime
from dateutil import parser
from cocore.config import Config
from cocore.Logger import Logger
from codb.mssql_tools import MSSQLInteraction
from codb.pg_tools import PGInteraction
from cocloud.s3_interaction import S3Interaction

LOG = Logger()


class rSqoop(object):
    """
    Redshift-Sqoop: quick staging of tables from MSSQL to Redshift
    """
    def __init__(self, src_database=None, tgt_database=None, from_date=None):
        self.src_database = src_database
        self.tgt_database = tgt_database
        self.etl_date = datetime.utcnow()
        self.from_date = from_date
        self.meta_fields = {
            'etl_source_system_cd': None,
            'etl_row_create_dts': None,
            'etl_row_update_dts': None,
            'etl_run_id': int(time.time())
        }
        if not os.path.exists('temp/'):
            os.makedirs('temp/')

        self.sql = None
        self.pg_conn = None
        self.aws_access_key = None
        self.aws_secret_key = None

        self.s3_environment = None
        self.s3_def_bucket = None
        self.s3_def_key_prefix = 'temp'
        self.s3_env = None
        self.conf = None
        self.s3_conn = None

    def init(self):
        self.conf = Config()

        if self.src_database is not None:
            sql_db_name = self.conf[self.src_database]['db_name']
            sql_user = self.conf[self.src_database]['user']
            sql_host = self.conf[self.src_database]['server']
            sql_password = self.conf[self.src_database]['password']
            port = self.conf[self.src_database].get('port', 1433)
            self.sql = MSSQLInteraction(dbname=sql_db_name, host=sql_host, user=sql_user,
                                        password=sql_password, port=port)
            LOG.l(f'Connecting to src db: {self.src_database}, db name: {sql_db_name}, host: {sql_host}')

            self.sql.conn()
            self.sql.batchOpen()

        if self.tgt_database is not None:
            pg_db_name = self.conf[self.tgt_database]['db_name']
            pg_user = self.conf[self.tgt_database]['user']
            pg_host = self.conf[self.tgt_database]['host']
            pg_password = self.conf[self.tgt_database]['password']
            pg_port = self.conf[self.tgt_database]['port']
            self.pg_conn = PGInteraction(dbname=pg_db_name, host=pg_host, user=pg_user,
                                         password=pg_password, port=pg_port, schema='public')
            LOG.l(f'Connecting to tgt db: {self.tgt_database}, db name: {pg_db_name}, host: {pg_host}')

            self.pg_conn.conn()
            self.pg_conn.batchOpen()

        self.aws_access_key = self.conf['general']['aws_access_key']
        self.aws_secret_key = self.conf['general']['aws_secret_key']

        self.s3_environment = S3Interaction(self.aws_access_key, self.aws_secret_key)
        self.s3_def_bucket = self.conf['general']['temp_bucket']
        self.s3_env = self.conf['general']['env']
        self.s3_conn = S3Interaction(self.aws_access_key, self.aws_secret_key)

        return self

    def get_fields(self, select_fields, schema):
        """
        Switch between custom fields provided by user or use src_table fields

        :param select_fields: list of tuple [(<salesforce_field>, <override_field>), (<salesforce_field>)]
        :param schema:
        """
        if select_fields:
            fields = []
            for field in schema:
                selected_field = self.get_field_values(select_fields, str(field[0]).lower())
                if not selected_field:
                    continue
                if isinstance(selected_field[0], tuple):
                    tup = (selected_field[0][1], field[1], field[2], field[3], field[4])
                else:
                    tup = (selected_field[0], field[1], field[2], field[3], field[4])
                fields.append(tup)
        else:
            fields = [(field[0], field[1], field[2], field[3], field[4]) for field in schema]
        return fields

    def get_field_values(self, iterables, key_to_find):
        """
        :param iterables: list of tuples
        :param key_to_find: str
        :return: list of tuple
        """
        return [ i for i in iterables if i == key_to_find or str(i[0]).lower() == str(key_to_find).lower() ]

    def get_source_table_schema(self, src_table):
        """
        :param src_table: str
        :return: list
        """
        schema_name = 'public'

        if '.' in src_table:
            schema_name, table_name = src_table.split('.')
        else:
            table_name = src_table

        if '[' in table_name:
            table_name = table_name[1: len(table_name) - 1]

        schema_sql = f"""
            select
              column_name,
              data_type,
              character_maximum_length,
              numeric_precision,
              numeric_scale
            from information_schema.columns
            where table_name=lower('{table_name}') and table_schema=lower('{schema_name}')
            order by ordinal_position """

        return self.sql.fetch_sql_all(schema_sql)

    def get_select_fields(self, schema, select_fields):
        """
        :param schema:
        :param select_fields:
        :return: 
        """
        field_names = []
        for field in schema:
            selected_field = self.get_field_values(select_fields, str(field[0]).lower())
            if not selected_field:
                continue
            field_names.append(field[0])
        return ','.join(field_names)

    def clone_staging_table(self, src_table, tgt_table, select_fields=None, incremental=False):
        """
        Clones src table schema to redshift

        :param src_table:
        :param tgt_table:
        :return:
        """
        rs_date_types = ('timestamp with time zone', 'time without time zone',
                         'datetime', 'smalldatetime', 'date', 'datetime2')
        rs_char_types = ('timestamp', 'char', 'varchar', 'character', 'nchar', 'bpchar',
                         'character varying', 'nvarchar', 'text')
        rs_num_types = ('decimal', 'numeric')
        rs_smallint_types = ('bit', 'tinyint', 'smallint', 'int2')
        rs_int_types = ('int', 'integer', 'int4')
        rs_bigint_types = ('bigint', 'int8')
        rs_other_types = ('real', 'double precision',
                          'boolean', 'float4', 'float8',
                          'float', 'date', 'bool',
                          'timestamp without time zone')
        rs_reserved_names = ('partition',)

        src_schema = self.get_source_table_schema(src_table)
        schema = self.get_fields(select_fields, src_schema)

        tgt_exists = self.pg_conn.table_exists(tgt_table)

        if len(schema) == 0:
            LOG.l('source table not found')
            src_table = None
            tgt_table = None
            return src_table, tgt_table

        if incremental and tgt_exists[0] is True:
            LOG.l('table exists in target')
            return src_table, tgt_table

        drop_sql = 'drop table if exists ' + tgt_table + ';\n'
        create_sql = 'create table ' + tgt_table + ' (\n'

        for field in schema:
            # reserved column names
            if str(field[0]).lower() not in rs_reserved_names:
                column_name = field[0]
            else:
                column_name = 'v_' + str(field[0])

            # known overrides
            if field[1] in rs_date_types:
                data_type = 'timestamp'
            elif field[1] == 'uuid':
                data_type = 'varchar(50)'
            # character precision
            elif field[1] in rs_char_types:
                if field[1] == "text" or field[1] == "timestamp":
                    field_type = "varchar"
                else:
                    field_type = field[1]

                if field[1] == "timestamp":
                    size = 8
                elif int(field[2]) > 65535:
                    size = 65535
                elif int(field[2]) < 0:
                    size = 2000
                else:
                    size = field[2]
                data_type = ('%s (%s)' % (field_type, size) if field[1] == 'timestamp' or field[2] is not None
                             else 'varchar(2000)')
            elif field[1] in rs_smallint_types:
                data_type = 'smallint'
            elif field[1] in rs_int_types:
                data_type = 'integer'
            elif field[1] in rs_bigint_types:
                data_type = 'bigint'
            # numeric precision
            elif field[1] in rs_num_types:
                data_type = ('%s (%s,%s)' % (field[1], field[3], field[4]) if field[3] is not None
                             else field[1])
            # other mapped types
            elif field[1] in rs_other_types:
                data_type = field[1]
            # uniqueidentifier to varchar(36)
            elif field[1] == 'uniqueidentifier':
                data_type = 'varchar(36)'
            # anthing else goes to varchar
            else:
                data_type = 'varchar(2000)'
            # build row
            create_sql += '\"'+column_name + '\" ' + data_type + ',\n'

        # append metadata fields
        create_sql += 'etl_source_system_cd varchar(50),' \
                      '\netl_row_create_dts timestamp,' \
                      '\netl_row_update_dts timestamp,' \
                      '\netl_run_id bigint );\n'

        query = drop_sql + create_sql

        LOG.l(drop_sql)
        LOG.l(create_sql)

        LOG.l('droping table ' + tgt_table)
        LOG.l('creating table ' + tgt_table)
        try:
            self.pg_conn.exec_sql(query)
            self.pg_conn.batchCommit()
            LOG.l('target table created')
        except Exception as e:
            LOG.l(f'Unable to create table with sql: {create_sql}')
            LOG.l(f'Warning msg: {e}')
            self.pg_conn.batchCommit()

        return src_table, tgt_table

    def source_to_s3(self,
                     src_table,
                     tgt_table,
                     s3_bucket=None,
                     s3_key=None,
                     select_fields=None,
                     date_fields=None,
                     delimiter='\t',
                     gzip=True,
                     source_system_cd=None):
        """
        Transfers data from source table to s3

        :param src_table:
        :param tgt_table:
        :param s3_bucket:
        :param s3_key:
        :param date_fields:
        :param delimiter:
        :param gzip:
        """

        tgt_key = tgt_table.replace('.', '-')
        if s3_bucket is None:
            s3_bucket = self.conf['general']['temp_bucket']
        if s3_key is None:
            s3_key = str('rsqoop/' + self.s3_env + '/' +tgt_key)

        s3_path = 's3://' + s3_bucket + '/' + s3_key

        filter_str = ''

        if date_fields and self.from_date:
            date_str = [date + f" > '{self.from_date.strftime('%Y-%m-%d %H:%M')}'" for date in date_fields]
            filter_str = f"where {' or '.join(date_str)}"

        # for custom field selection
        select_str = '*'
        if select_fields:
            src_schema = self.get_source_table_schema(src_table)
            select_str = self.get_select_fields(src_schema, select_fields)

        ce_sql = f"select {select_str} from {src_table} with (nolock) {filter_str}"
        LOG.l(ce_sql)

        result = self.sql.fetch_sql(sql=ce_sql, blocksize=20000)

        temp_file = gz.open('temp/%s.txt' % tgt_key, mode='wt', encoding='utf-8') if gzip \
            else open('temp/%s.txt' % tgt_key, mode='w', encoding='utf-8')
        LOG.l('exporting to tempfile:' + temp_file.name)

        self.meta_fields['etl_row_create_dts'] = self.etl_date.strftime('%Y-%m-%d %H:%M:%S')
        self.meta_fields['etl_row_update_dts'] = self.meta_fields['etl_row_create_dts']

        # check if there is source_system_cd user input
        self.meta_fields['etl_source_system_cd'] = source_system_cd if source_system_cd else ''
        meta_values = list(self.meta_fields.values())

        writer = csv.writer(temp_file, delimiter=delimiter, quoting=csv.QUOTE_NONE, quotechar="")
        for row in result:
            row_data = []
            for s in row:
                if isinstance(s, bool):
                    s = int(s)
                else:
                    s = str(s).replace('\n', ' ').replace('\t', ' ').replace('\r', ' ').replace('\v', ' ')
                row_data.append(s)
            writer.writerow(row_data + meta_values)
        temp_file.flush()
        temp_file.close()
        sleep(10)

        # simple quick keep alive for large tables
        self.pg_conn.conn()
        self.pg_conn.batchOpen()
        self.pg_conn.fetch_sql("select 1")
        self.sql.fetch_sql("select 1")

        LOG.l('upload starting')
        self.s3_conn.put_file_to_s3(bucket=s3_bucket, key=s3_key + '/output.tsv',
                               local_filename=temp_file.name)

        s3_full_path = s3_path + '/' + 'output.tsv'
        LOG.l('upload complete to ' + s3_path)

        if os.path.exists(temp_file.name):
            os.remove(temp_file.name)

        return s3_full_path

    def s3_to_redshift(self,
                        tgt_table,
                        s3_path,
                        incremental=False,
                        csv_fmt=False,
                        gzip=True,
                        manifest=False,
                        maxerror=0,
                        key_fields=None,
                        delimiter='\\t',
                        remove_quotes=False):
        """
        Copy from s3 into redshift target table

        :param tgt_table:
        :param s3_path:
        :param incremental:
        :param csv_fmt:
        :param gzip:
        :param manifest:
        :param maxerror:
        :param key_fields:
        :param delimiter:
        :param remove_quotes:
        :return:
        """
        # check to make sure parameters make sense
        if incremental and not key_fields:
            raise Exception("incremental loads require key fields")

        # create options list, made this more complicated for future use :)
        options = []
        if gzip:
            options.append('GZIP')
        if manifest:
            options.append('MANIFEST')
        if remove_quotes:
            options.append('REMOVEQUOTES')

        opt_str = ' '.join(options) if len(options) > 0 else ''
        upd_sql = ''
        upd_where = ''

        if not incremental:
            pre_sql1 = """delete from %(tgt_table)s;\n"""

            if csv_fmt:
                cp_sql = """
                COPY %(tgt_table)s from '%(s3_path)s'
                CREDENTIALS 'aws_access_key_id=%(aws_id)s;aws_secret_access_key=%(aws_key)s'
                dateformat 'YYYY-MM-DD'
                NULL AS 'None'
                truncatecolumns
                maxerror %(maxerror)s
                %(options)s;\n"""
            else:
                cp_sql = """
                COPY %(tgt_table)s from '%(s3_path)s'
                CREDENTIALS 'aws_access_key_id=%(aws_id)s;aws_secret_access_key=%(aws_key)s'
                delimiter '%(delimiter)s'
                dateformat 'YYYY-MM-DD'
                NULL AS 'None'
                truncatecolumns
                maxerror %(maxerror)s
                %(options)s;\n"""
        else:
            pre_sql1 = """
                drop table if exists tmp;
                create temporary table tmp (like %(tgt_table)s);\n"""

            if csv_fmt:
                cp_sql = """
                COPY tmp from '%(s3_path)s'
                CREDENTIALS 'aws_access_key_id=%(aws_id)s;aws_secret_access_key=%(aws_key)s'
                dateformat 'YYYY-MM-DD'
                NULL AS 'None'
                truncatecolumns
                maxerror %(maxerror)s %(options)s;\n"""
            else:
                cp_sql = """
                COPY tmp from '%(s3_path)s'
                CREDENTIALS 'aws_access_key_id=%(aws_id)s;aws_secret_access_key=%(aws_key)s'
                delimiter '%(delimiter)s'
                dateformat 'YYYY-MM-DD'
                NULL AS 'None'
                truncatecolumns
                maxerror %(maxerror)s %(options)s;\n"""

            upd_where = ' and '.join([f'tmp.{x} = {tgt_table}.{x}' for x in key_fields])
            upd_sql = """
                delete
                from %(tgt_table)s
                where exists
                  ( select 1
                    from tmp
                    where %(upd_where)s);
                insert into %(tgt_table)s
                select * from tmp;\n"""

        if csv_fmt:
            subs = {'tgt_table': tgt_table, 's3_path': s3_path, 'aws_id': self.aws_access_key,
                    'aws_key': self.aws_secret_key, 'maxerror': maxerror,
                    'options': opt_str, 'upd_where': upd_where }
        else:
            subs = {'tgt_table': tgt_table, 's3_path': s3_path, 'aws_id': self.aws_access_key,
                    'aws_key': self.aws_secret_key, 'maxerror': maxerror,
                    'options': opt_str, 'upd_where': upd_where,
                    'delimiter': delimiter}

        sql = (pre_sql1 + cp_sql + upd_sql) % subs

        LOG.l(f'starting copy to {tgt_table}')
        try:
            self.pg_conn.conn()
            self.pg_conn.batchOpen()
            self.pg_conn.exec_sql(sql)
        except Exception as e:
            LOG.l(f'Error: {e}')
            LOG.l(f'Offending sql: {sql}')
            raise
        LOG.l('granting access')
        self.grant_std_access(tgt_table)

        LOG.l('copy complete')

        self.pg_conn.batchCommit()

    def get_src_count(self, src_table):
        """
        Get src count, should be run at time ETL begins

        :param src_table:
        :return:
        """
        LOG.l('capturing source count')
        src_cnt = self.sql.fetch_sql_all(f"select count(1) from {src_table}")[0][0]
        LOG.l(f'src_cnt: {src_cnt}')
        return src_cnt

    def check_tgt_count(self, src_cnt, tgt_table, pct_threshold=0.01):
        """
        Basic source to target data quality checks

        :param src_cnt:
        :param tgt_table:
        :param pct_threshold:
        :return:
        """
        LOG.l('data quality checks starting')
        tgt_cnt = self.pg_conn.fetch_sql_all(f"select count(1) from {tgt_table}")[0][0]
        LOG.l(f'src_cnt: {src_cnt}')
        LOG.l(f'tgt_cnt: {tgt_cnt}')

        diff = src_cnt - tgt_cnt
        pct_diff = float(diff)/(src_cnt if src_cnt != 0 else 1)

        if pct_diff > pct_threshold:
            error_msg = f"failed count check, difference: {diff}, percent: {pct_diff}"
            LOG.l(error_msg)
            raise Exception(error_msg)
        else:
            LOG.l(f"passed count check, difference: {diff}, percent: {pct_diff}")

        return diff, pct_diff

    def build_rs_manifest(self, url_list, mfst_bucket=None,
                          mfst_key_prefix=None, mfst_filename=None):
        """
        Builds redshift manifest

        :param url_list:
        :param mfst_bucket:
        :param mfst_key_prefix:
        :param mfst_filename:
        :return: mfst_url (s3 url) and mfst (manifest content)
        """
        LOG.l(self.s3_def_bucket)
        mfst_bucket = mfst_bucket if mfst_bucket else self.s3_def_bucket
        mfst_key_prefix = mfst_key_prefix if mfst_key_prefix else self.s3_def_key_prefix
        mfst_filename = mfst_filename if mfst_filename \
            else datetime.now().strftime("%Y%m%d-%H%M%S%f")

        s3_bucket = self.s3_environment.get_bucket(mfst_bucket)

        LOG.l(mfst_filename)
        entries = []
        for url in url_list:
            s3_file = {}
            s3_file['url'] = url
            s3_file["mandatory"] = True
            entries.append(s3_file)
        mfst_key_name = mfst_key_prefix + '/' + mfst_filename
        mfst = {"entries":entries}
        mfst_str = str(json.dumps(mfst))
        s3_bucket.new_key(mfst_key_name).set_contents_from_string(mfst_str)
        mfst_url = f"s3://{mfst_bucket}/{mfst_key_name}"
        return mfst_url, mfst

    def grant_std_access(self, entity):
        """
        Grants standard groups to entity

        :param entity:
        :return:
        """
        grant = f"""
            grant all on {entity} to etl_user;
            grant all on {entity} to group ro_users;
            grant all on {entity} to group power_users;\n"""
        self.pg_conn.exec_sql(grant)
        self.pg_conn.batchCommit()

    def stage_to_redshift(self,
                          src_table,
                          tgt_table,
                          incremental=False,
                          gzip=True,
                          date_fields=None,
                          delimiter='\t',
                          remove_quotes=False,
                          key_fields=None,
                          select_fields=None,
                          source_system_cd=None):
        """
        Clones table from source, stages to s3, and then copies into redshift

        :param src_table:
        :param tgt_table:
        :param incremental:
        :param gzip:
        :param date_fields:
        :param delimiter:
        :param remove_quotes:
        :param key_fields:
        :param select_fields:
        :param source_system_cd:
        :return:
        """
        LOG.l(f'\n\n--starting staging of {src_table}')

        # 1. clone tables (if doesn't already exist)
        src_name, tgt_name = self.clone_staging_table(src_table, tgt_table, select_fields, incremental=incremental)

        LOG.l(f'loading to {tgt_name}')

        if not src_name:
            LOG.l('no source found, no work to do!')

        # 2. get source count
        src_cnt = self.get_src_count(src_table)

        # 3. copy data to s3
        s3_path = self.source_to_s3(src_table, tgt_table,
                                    select_fields=select_fields,
                                    date_fields=date_fields, delimiter=delimiter,
                                    gzip=gzip, source_system_cd=source_system_cd)

        # 4. copy s3 data to redshift
        self.s3_to_redshift(tgt_table=tgt_table,
                            s3_path=s3_path,
                            gzip=gzip,
                            delimiter=delimiter,
                            remove_quotes=remove_quotes,
                            key_fields=key_fields,
                            incremental=incremental)

        # 5. check counts to make sure they match
        self.check_tgt_count(src_cnt, tgt_table)

        LOG.l('--end staging of table\n\n')


if __name__ == '__main__':
    aparser = argparse.ArgumentParser()
    aparser.add_argument('-sc', '--source-conn', help="""source connection""", required=True)
    aparser.add_argument('-tc', '--target-conn', help="""target conneciton""", required=True)
    aparser.add_argument('-st', '--source-tables', nargs='+', help="""source table""", required=True)
    aparser.add_argument('-tt', '--target-tables', nargs='+', help="""target table""", required=True)
    aparser.add_argument('-i', '--incremental', default=False, action='store_true', help='True for incremental', required=False)
    aparser.add_argument('-z', '--gzip', default=True, action='store_true', help='True for incremental', required=False)
    aparser.add_argument('-q', '--remove-quotes', default=False, action='store_true', help='True to remove quotes', required=False)
    aparser.add_argument('-sf', '--select-fields', nargs='*', help='fields needed for incremental (list)', required=False)
    aparser.add_argument('-kf', '--key-fields', nargs='*', help='fields needed for incremental (list)', required=False)
    aparser.add_argument('-df', '--date-fields', nargs='*', help='date fields for incremental (list)', required=False)
    aparser.add_argument('-f', '--from-date', type=parser.parse, help='from date for incremental', required=False)
    aparser.add_argument('-ss', '--source-system', help='source system cd', required=False)
    args = aparser.parse_args()

    r = rSqoop(args.source_conn, args.target_conn, args.from_date).init()

    for i in range(len(args.source_tables)):
        source_table = args.source_tables[i]
        target_table = args.target_tables[i]
        LOG.l(f'Staging source table {source_table} to Redshift table {target_table}')
        r.stage_to_redshift(
            source_table,
            target_table,
            incremental=args.incremental,
            gzip=args.gzip,
            date_fields=args.date_fields,
            remove_quotes=args.remove_quotes,
            select_fields=args.select_fields,
            key_fields=args.key_fields,
            source_system_cd=args.source_system
        )
