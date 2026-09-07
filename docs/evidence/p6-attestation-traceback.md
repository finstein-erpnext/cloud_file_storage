# P6 attestation error — traceback evidence

Question: does the attestation error name `test_a_backup_is_refused_without_a_bucket`,
and is `QueryDeadlockError` what escaped?

Answer: **yes to both**, reproduced at the orchestrator's SHA.

```
tree:    phase/P6-backup @ b907603fbd6386e3c96b775bfe887561d6cb0aad (detached, read-only run)
site:    cfs-p6.local
method:  a second process cycling the same Cloud Backup Settings tabSingles rows,
         i.e. the shape of a second suite run on the same site
hunt:    attempt 1 -> 10 errors (not this test); attempt 2 -> 7 errors (not this test);
         attempt 3 -> HIT
```

```
ERROR: test_a_backup_is_refused_without_a_bucket (cloud_file_storage.tests.test_backup.TestBackupRefusals)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/home/user/v15/apps/frappe/frappe/database/database.py", line 230, in sql
    self._cursor.execute(query, values)
  File "/home/user/v15/env/lib/python3.10/site-packages/pymysql/cursors.py", line 153, in execute
    result = self._query(query)
  File "/home/user/v15/env/lib/python3.10/site-packages/pymysql/cursors.py", line 322, in _query
    conn.query(q)
  File "/home/user/v15/env/lib/python3.10/site-packages/pymysql/connections.py", line 563, in query
    self._affected_rows = self._read_query_result(unbuffered=unbuffered)
  File "/home/user/v15/env/lib/python3.10/site-packages/pymysql/connections.py", line 825, in _read_query_result
    result.read()
  File "/home/user/v15/env/lib/python3.10/site-packages/pymysql/connections.py", line 1199, in read
    first_packet = self.connection._read_packet()
  File "/home/user/v15/env/lib/python3.10/site-packages/pymysql/connections.py", line 775, in _read_packet
    packet.raise_for_error()
  File "/home/user/v15/env/lib/python3.10/site-packages/pymysql/protocol.py", line 219, in raise_for_error
    err.raise_mysql_exception(self._data)
  File "/home/user/v15/env/lib/python3.10/site-packages/pymysql/err.py", line 150, in raise_mysql_exception
    raise errorclass(errno, errval)
pymysql.err.OperationalError: (1213, 'Deadlock found when trying to get lock; try restarting transaction')

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "/home/user/v15/apps/cfs-P6/cloud_file_storage/tests/backup_utils.py", line 436, in _cleanup
    restore_backup_settings(self._backup_snapshot)
  File "/home/user/v15/apps/cfs-P6/cloud_file_storage/tests/backup_utils.py", line 314, in restore_backup_settings
    frappe.db.set_single_value(BACKUP_SETTINGS_DOCTYPE, values)
  File "/home/user/v15/apps/frappe/frappe/database/database.py", line 803, in set_single_value
    frappe.db.delete(
  File "/home/user/v15/apps/frappe/frappe/database/database.py", line 1408, in delete
    return query.run(**kwargs)
```

Note the frame `set_single_value -> frappe.db.delete`: frappe's own single-value write
does a DELETE followed by an INSERT on `tabSingles`. The per-field delete loop added
churn on top of that, but the base operation already contends — which is why collapsing
the loop reduced the rate without removing the failure, and why the bounded retry is the
actual fix.
