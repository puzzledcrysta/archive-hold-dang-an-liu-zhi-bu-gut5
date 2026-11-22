import json
from app.errors import DataError
from app.store import connection

LIVE = ('待复核', '确认', '退回')


def lookup_list(db, list_id):
    row = db.execute('SELECT * FROM transfer_lists WHERE id=?', (list_id,)).fetchone()
    if row is None:
        raise DataError('LIST_UNKNOWN', '没有此清单', 404)
    return row


def latest_run_id(db):
    row = db.execute('SELECT id FROM runs ORDER BY created DESC,id DESC LIMIT 1').fetchone()
    return row['id'] if row else None


def expire_live(db, changed):
    marks = ','.join('?' * len(LIVE))
    db.execute(f"UPDATE transfer_lists SET status='已失效',changed=? WHERE status IN ({marks})",
               (changed, *LIVE))


def insert_list(db, record):
    db.execute('INSERT INTO transfer_lists VALUES (?,?,?,?,?,?,?,?)',
               (record['list_id'], record['week'], record['run_id'], record['capacity'],
                json.dumps(record, ensure_ascii=False), '待复核', record['created'], record['created']))


def payload(row):
    return {**json.loads(row['result']), 'status': row['status']}


def history():
    with connection() as db:
        return [dict(row) for row in db.execute(
            'SELECT id,week,run_id,status,created FROM transfer_lists ORDER BY created DESC,id DESC LIMIT 50')]


def reviews(db, list_id):
    return [dict(row) for row in db.execute(
        'SELECT version,decision,note,changed FROM transfer_reviews WHERE list_id=? ORDER BY version DESC',
        (list_id,))]
