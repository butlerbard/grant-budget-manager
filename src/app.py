from __future__ import annotations

import csv
import base64
import binascii
import gc
import io
import json
import os
import re
import secrets
import sqlite3
import shutil
import smtplib
import ssl
import sys
import unicodedata
import threading
import webbrowser
import zipfile
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.message import EmailMessage
from email.utils import formataddr
from xml.sax.saxutils import escape
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATE_DIR = BASE_DIR / "templates"
DB_PATH = PROJECT_ROOT / "data" / "經費管理.db"
HOST = "127.0.0.1"
PORT = int(os.environ.get("BUDGET_PORT", "8765"))
SERVER_SESSION = secrets.token_urlsafe(12)
EMAIL_JOBS = {}
EMAIL_JOBS_LOCK = threading.Lock()


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()
        return False


def display_error(error):
    """Convert database implementation errors into actionable UI messages."""
    message=str(error)
    if isinstance(error,sqlite3.IntegrityError):
        if 'FOREIGN KEY constraint failed' in message:
            return '此資料仍被其他紀錄使用，無法刪除或修改。請先移除或改派相關資料後再試。'
        if 'UNIQUE constraint failed' in message:
            return '資料與既有項目重複，請檢查名稱或內容後再試。'
        if 'NOT NULL constraint failed' in message:
            return '有必填資料未填寫，請補齊後再試。'
        if 'CHECK constraint failed' in message:
            return '資料不符合欄位規則，請檢查輸入內容後再試。'
        return '資料未能儲存，請檢查欄位內容及資料關聯後再試。'
    if isinstance(error,sqlite3.OperationalError):
        if 'database is locked' in message:
            return '資料庫目前忙碌中，請稍候再試。'
        if 'no such table' in message or 'no such column' in message:
            return '系統資料結構發生問題，請重新啟動程式；若仍出現，請回報此情況。'
        return '系統暫時無法完成資料操作，請重新整理後再試；若仍出現，請回報此情況。'
    return message


def initialize():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS projects(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, year TEXT DEFAULT '', code TEXT DEFAULT '',
          unit TEXT DEFAULT '', start_date TEXT DEFAULT '', end_date TEXT DEFAULT '', contact TEXT DEFAULT '',
          phone TEXT DEFAULT '', email TEXT DEFAULT '', total_budget INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS budget_items(
          id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          source TEXT NOT NULL, owner TEXT NOT NULL, subject TEXT NOT NULL, category TEXT DEFAULT '',
          initial REAL NOT NULL DEFAULT 0,
          funding_source_id INTEGER REFERENCES funding_sources(id) ON DELETE RESTRICT,
          unit_id INTEGER REFERENCES units(id) ON DELETE RESTRICT,
          budget_subject_id INTEGER REFERENCES budget_subjects(id) ON DELETE RESTRICT,
          budget_attribute_id INTEGER REFERENCES budget_attributes(id) ON DELETE RESTRICT,
          UNIQUE(project_id,funding_source_id,unit_id,budget_subject_id,budget_attribute_id));
        CREATE TABLE IF NOT EXISTS reimbursement_batches(
          id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          submitted_date TEXT DEFAULT '', description TEXT DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS expenses(
          id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          voucher_date TEXT DEFAULT '',
          batch_id INTEGER REFERENCES reimbursement_batches(id) ON DELETE SET NULL,
          owner TEXT NOT NULL, source TEXT NOT NULL, subject TEXT NOT NULL, category TEXT NOT NULL DEFAULT '',
          amount REAL NOT NULL CHECK(amount >= 0), note TEXT DEFAULT '', voided INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS payees(
          id INTEGER PRIMARY KEY, expense_id INTEGER NOT NULL REFERENCES expenses(id) ON DELETE CASCADE,
          name TEXT NOT NULL, amount REAL NOT NULL CHECK(amount >= 0));
        CREATE TABLE IF NOT EXISTS expense_delete_log(
          id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          original_expense_id INTEGER NOT NULL, batch_id INTEGER, batch_description TEXT NOT NULL DEFAULT '',
          submitted_date TEXT NOT NULL DEFAULT '', voucher_date TEXT NOT NULL DEFAULT '',
          source TEXT NOT NULL, owner TEXT NOT NULL, category TEXT NOT NULL, subject TEXT NOT NULL,
          amount INTEGER NOT NULL, payees_json TEXT NOT NULL DEFAULT '[]', note TEXT NOT NULL DEFAULT '',
          voided INTEGER NOT NULL DEFAULT 0, deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS transfers(
          id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          date TEXT NOT NULL, source TEXT NOT NULL, from_owner TEXT NOT NULL, from_subject TEXT NOT NULL,
          from_category TEXT NOT NULL DEFAULT '', to_owner TEXT NOT NULL, to_subject TEXT NOT NULL,
          to_category TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'executed' CHECK(status IN ('planned','executed')),
          amount REAL CHECK((status='planned' AND (amount IS NULL OR amount>=0)) OR (status='executed' AND amount>0)),
          locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0,1)),
          note TEXT DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS estimate_rules(
          id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          source TEXT NOT NULL, category TEXT NOT NULL, subject TEXT NOT NULL,
          budget_subject_id INTEGER REFERENCES budget_subjects(id) ON DELETE RESTRICT,
          UNIQUE(project_id,source,category,subject));
        CREATE TABLE IF NOT EXISTS import_batches(
          id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          filenames TEXT NOT NULL, row_count INTEGER NOT NULL, imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS project_options(
          id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          kind TEXT NOT NULL, value TEXT NOT NULL, UNIQUE(project_id,kind,value));
        -- Typed, project-scoped option tables.  Legacy labels remain as
        -- display/import columns; all finance rows also carry real FK IDs.
        CREATE TABLE IF NOT EXISTS funding_sources(
          id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          name TEXT NOT NULL, UNIQUE(project_id,name));
        CREATE TABLE IF NOT EXISTS units(
          id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          name TEXT NOT NULL, UNIQUE(project_id,name));
        CREATE TABLE IF NOT EXISTS budget_subjects(
          id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          name TEXT NOT NULL, UNIQUE(project_id,name));
        CREATE TABLE IF NOT EXISTS budget_attributes(
          id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          name TEXT NOT NULL, UNIQUE(project_id,name));
        CREATE TABLE IF NOT EXISTS project_notes(
          project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
          content TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS email_smtp_settings(
          id INTEGER PRIMARY KEY CHECK(id=1), host TEXT NOT NULL DEFAULT '', port INTEGER NOT NULL DEFAULT 587,
          security TEXT NOT NULL DEFAULT 'starttls' CHECK(security IN ('starttls','ssl','none')),
          username TEXT NOT NULL DEFAULT '', sender_email TEXT NOT NULL DEFAULT '', sender_name TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS email_recipients(
          id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          role TEXT NOT NULL CHECK(role IN ('unit','manager')), unit_id INTEGER NOT NULL REFERENCES units(id) ON DELETE RESTRICT,
          name TEXT NOT NULL, email TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
          CHECK(unit_id IS NOT NULL));
        CREATE TABLE IF NOT EXISTS email_send_batches(
          id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          report_as_of TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'sending' CHECK(status IN ('sending','sent','partial','failed')),
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS email_delivery_logs(
          id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES email_send_batches(id) ON DELETE CASCADE,
          recipient_id INTEGER REFERENCES email_recipients(id) ON DELETE SET NULL,
          recipient_type TEXT NOT NULL, recipient_name TEXT NOT NULL DEFAULT '', recipient_email TEXT NOT NULL,
          report_scope TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('sent','failed')),
          error TEXT NOT NULL DEFAULT '', sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS schema_meta(version INTEGER PRIMARY KEY, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        """)
        estimate_cols={r[1] for r in db.execute('PRAGMA table_info(estimate_rules)')}
        if not {'source','category'}.issubset(estimate_cols):
            legacy_rules=[dict(r) for r in db.execute('SELECT id,project_id,subject FROM estimate_rules ORDER BY id')]
            db.execute('''CREATE TABLE estimate_rules_new(id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                source TEXT NOT NULL,category TEXT NOT NULL,subject TEXT NOT NULL,
                budget_subject_id INTEGER REFERENCES budget_subjects(id) ON DELETE RESTRICT,
                UNIQUE(project_id,source,category,subject))''')
            for rule in legacy_rules:
                pairs=list(db.execute('SELECT DISTINCT source,category FROM budget_items WHERE project_id=? AND subject=? ORDER BY source,category',(rule['project_id'],rule['subject'])))
                for index,pair in enumerate(pairs or [{'source':'','category':''}]):
                    db.execute('INSERT OR IGNORE INTO estimate_rules_new(id,project_id,source,category,subject) VALUES(?,?,?,?,?)',
                        (rule['id'] if index==0 else None,rule['project_id'],pair['source'],pair['category'],rule['subject']))
            db.execute('DROP TABLE estimate_rules')
            db.execute('ALTER TABLE estimate_rules_new RENAME TO estimate_rules')
        elif 'rate' in estimate_cols:
            db.execute('ALTER TABLE estimate_rules DROP COLUMN rate')
        if 'locked' not in {r[1] for r in db.execute('PRAGMA table_info(transfers)')}:
            db.execute('ALTER TABLE transfers ADD COLUMN locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0,1))')
        if 'status' not in {r[1] for r in db.execute('PRAGMA table_info(transfers)')}:
            db.execute("ALTER TABLE transfers ADD COLUMN status TEXT NOT NULL DEFAULT 'executed' CHECK(status IN ('planned','executed'))")
        if 'transfer_lock_date' in {r[1] for r in db.execute('PRAGMA table_info(projects)')}:
            db.execute('ALTER TABLE projects DROP COLUMN transfer_lock_date')
        db.executescript("""
        DROP TRIGGER IF EXISTS email_recipient_project_unit_insert;
        DROP TRIGGER IF EXISTS email_recipient_project_unit_update;
        CREATE TRIGGER IF NOT EXISTS email_recipient_project_unit_insert BEFORE INSERT ON email_recipients
        WHEN (NEW.unit_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM units WHERE id=NEW.unit_id AND project_id=NEW.project_id)) OR NEW.unit_id IS NULL
        BEGIN SELECT RAISE(ABORT,'收件人所屬單位必須屬於此專案'); END;
        CREATE TRIGGER IF NOT EXISTS email_recipient_project_unit_update BEFORE UPDATE OF project_id,role,unit_id ON email_recipients
        WHEN (NEW.unit_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM units WHERE id=NEW.unit_id AND project_id=NEW.project_id)) OR NEW.unit_id IS NULL
        BEGIN SELECT RAISE(ABORT,'收件人所屬單位必須屬於此專案'); END;
        """)
        db.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES('default_estimate_rate','2.11')")
        if not db.execute("SELECT 1 FROM app_settings WHERE key='table_alignments_reset_20261125'").fetchone():
            db.execute("INSERT INTO app_settings(key,value) VALUES('table_alignments','{}') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            db.execute("INSERT INTO app_settings(key,value) VALUES('table_alignments_reset_20261125','1')")
        if not db.execute("SELECT 1 FROM app_settings WHERE key='empty_batches_cleanup_20261126'").fetchone():
            db.execute("DELETE FROM reimbursement_batches WHERE NOT EXISTS(SELECT 1 FROM expenses WHERE expenses.batch_id=reimbursement_batches.id)")
            db.execute("INSERT INTO app_settings(key,value) VALUES('empty_batches_cleanup_20261126','1')")
        install_row=db.execute("SELECT value FROM app_settings WHERE key='smtp_install_id'").fetchone()
        if not install_row:
            install_id=secrets.token_hex(16)
            db.execute("INSERT INTO app_settings(key,value) VALUES('smtp_install_id',?)",(install_id,))
            smtp_row=db.execute("SELECT username,sender_email FROM email_smtp_settings WHERE id=1").fetchone()
            legacy_identity=(smtp_row['username'] or smtp_row['sender_email']) if smtp_row else ''
            if legacy_identity:
                try:
                    import keyring
                    legacy_password=keyring.get_password('project-finance-smtp',_legacy_smtp_keyring_account(legacy_identity))
                    if legacy_password:keyring.set_password('project-finance-smtp',_smtp_account_for_install(install_id,legacy_identity),legacy_password)
                except Exception:pass
        # Keep the project's option IDs stable while the old text columns are
        # retained for readable exports and the existing UI data contract.
        for kind, table in (('source','funding_sources'),('owner','units'),('subject','budget_subjects'),('category','budget_attributes')):
            db.execute(f"INSERT OR IGNORE INTO {table}(id,project_id,name) SELECT id,project_id,value FROM project_options WHERE kind=?",(kind,))
        for table, column, ref_table in (
            ('budget_items','funding_source_id','funding_sources'),('budget_items','unit_id','units'),
            ('budget_items','budget_subject_id','budget_subjects'),('budget_items','budget_attribute_id','budget_attributes'),
            ('expenses','budget_item_id','budget_items'),('transfers','from_budget_item_id','budget_items'),
            ('transfers','to_budget_item_id','budget_items'),('estimate_rules','budget_subject_id','budget_subjects')):
            cols={r[1] for r in db.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER REFERENCES {ref_table}(id) ON DELETE RESTRICT")
        # A budget line is uniquely identified by its project-scoped source,
        # unit, subject, and attribute. Add the attribute labels to activity
        # rows so their foreign keys can be checked without guessing.
        for table, column in (('expenses','category'),('transfers','from_category'),('transfers','to_category')):
            cols={r[1] for r in db.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        _upgrade_non_reusable_ids(db)
        db.executescript("""
        DROP TRIGGER IF EXISTS expense_budget_item_id;
        DROP TRIGGER IF EXISTS expense_budget_item_id_update;
        DROP TRIGGER IF EXISTS expense_budget_fk_scope;
        DROP TRIGGER IF EXISTS transfer_budget_item_ids;
        DROP TRIGGER IF EXISTS transfer_budget_item_ids_update;
        DROP TRIGGER IF EXISTS transfer_budget_fk_scope;
        DROP TRIGGER IF EXISTS transfer_status_check;
        """)
        transfer_sql=db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='transfers'").fetchone()[0]
        if 'CHECK(amount > 0)' in transfer_sql or 'amount REAL NOT NULL CHECK(amount > 0)' in transfer_sql:
            db.execute("""CREATE TABLE transfers_rebuild(
              id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
              date TEXT NOT NULL, source TEXT NOT NULL, from_owner TEXT NOT NULL, from_subject TEXT NOT NULL,
              from_category TEXT NOT NULL DEFAULT '', to_owner TEXT NOT NULL, to_subject TEXT NOT NULL,
              to_category TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'executed' CHECK(status IN ('planned','executed')),
              amount REAL CHECK((status='planned' AND (amount IS NULL OR amount>=0)) OR (status='executed' AND amount>0)),
              locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0,1)), note TEXT DEFAULT '',
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              from_budget_item_id INTEGER REFERENCES budget_items(id) ON DELETE RESTRICT,
              to_budget_item_id INTEGER REFERENCES budget_items(id) ON DELETE RESTRICT)""")
            db.execute("""INSERT INTO transfers_rebuild(id,project_id,date,source,from_owner,from_subject,from_category,to_owner,to_subject,to_category,status,amount,locked,note,created_at,from_budget_item_id,to_budget_item_id)
              SELECT id,project_id,date,source,from_owner,from_subject,from_category,to_owner,to_subject,to_category,status,amount,locked,note,created_at,from_budget_item_id,to_budget_item_id FROM transfers""")
            db.execute("DROP TABLE transfers")
            db.execute("ALTER TABLE transfers_rebuild RENAME TO transfers")
        db.executescript("""
        CREATE TRIGGER IF NOT EXISTS project_option_typed_insert AFTER INSERT ON project_options BEGIN
          INSERT OR IGNORE INTO funding_sources(id,project_id,name) SELECT NEW.id,NEW.project_id,NEW.value WHERE NEW.kind='source';
          INSERT OR IGNORE INTO units(id,project_id,name) SELECT NEW.id,NEW.project_id,NEW.value WHERE NEW.kind='owner';
          INSERT OR IGNORE INTO budget_subjects(id,project_id,name) SELECT NEW.id,NEW.project_id,NEW.value WHERE NEW.kind='subject';
          INSERT OR IGNORE INTO budget_attributes(id,project_id,name) SELECT NEW.id,NEW.project_id,NEW.value WHERE NEW.kind='category';
        END;
        CREATE TRIGGER IF NOT EXISTS project_option_typed_update AFTER UPDATE OF value ON project_options BEGIN
          UPDATE funding_sources SET name=NEW.value WHERE id=NEW.id AND NEW.kind='source';
          UPDATE units SET name=NEW.value WHERE id=NEW.id AND NEW.kind='owner';
          UPDATE budget_subjects SET name=NEW.value WHERE id=NEW.id AND NEW.kind='subject';
          UPDATE budget_attributes SET name=NEW.value WHERE id=NEW.id AND NEW.kind='category';
        END;
        CREATE TRIGGER IF NOT EXISTS project_option_typed_delete AFTER DELETE ON project_options BEGIN
          DELETE FROM funding_sources WHERE id=OLD.id AND OLD.kind='source';
          DELETE FROM units WHERE id=OLD.id AND OLD.kind='owner';
          DELETE FROM budget_subjects WHERE id=OLD.id AND OLD.kind='subject';
          DELETE FROM budget_attributes WHERE id=OLD.id AND OLD.kind='category';
        END;
        CREATE TRIGGER IF NOT EXISTS budget_item_option_ids AFTER INSERT ON budget_items BEGIN
          SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM funding_sources WHERE project_id=NEW.project_id AND name=NEW.source) THEN RAISE(ABORT,'經費類別不屬於此專案') END;
          SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM units WHERE project_id=NEW.project_id AND name=NEW.owner) THEN RAISE(ABORT,'單位不屬於此專案') END;
          SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM budget_subjects WHERE project_id=NEW.project_id AND name=NEW.subject) THEN RAISE(ABORT,'預算科目不屬於此專案') END;
          SELECT CASE WHEN TRIM(NEW.category)='' OR NOT EXISTS(SELECT 1 FROM budget_attributes WHERE project_id=NEW.project_id AND name=NEW.category) THEN RAISE(ABORT,'經費屬性必須是本專案已建立的選項') END;
          UPDATE budget_items SET funding_source_id=(SELECT id FROM funding_sources WHERE project_id=NEW.project_id AND name=NEW.source),
            unit_id=(SELECT id FROM units WHERE project_id=NEW.project_id AND name=NEW.owner),
            budget_subject_id=(SELECT id FROM budget_subjects WHERE project_id=NEW.project_id AND name=NEW.subject),
            budget_attribute_id=(SELECT id FROM budget_attributes WHERE project_id=NEW.project_id AND name=NEW.category)
            WHERE id=NEW.id;
        END;
        CREATE TRIGGER IF NOT EXISTS budget_item_option_ids_update AFTER UPDATE OF project_id,source,owner,subject,category ON budget_items BEGIN
          SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM funding_sources WHERE project_id=NEW.project_id AND name=NEW.source) THEN RAISE(ABORT,'經費類別不屬於此專案') END;
          SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM units WHERE project_id=NEW.project_id AND name=NEW.owner) THEN RAISE(ABORT,'單位不屬於此專案') END;
          SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM budget_subjects WHERE project_id=NEW.project_id AND name=NEW.subject) THEN RAISE(ABORT,'預算科目不屬於此專案') END;
          SELECT CASE WHEN TRIM(NEW.category)='' OR NOT EXISTS(SELECT 1 FROM budget_attributes WHERE project_id=NEW.project_id AND name=NEW.category) THEN RAISE(ABORT,'經費屬性必須是本專案已建立的選項') END;
          UPDATE budget_items SET funding_source_id=(SELECT id FROM funding_sources WHERE project_id=NEW.project_id AND name=NEW.source),
            unit_id=(SELECT id FROM units WHERE project_id=NEW.project_id AND name=NEW.owner),
            budget_subject_id=(SELECT id FROM budget_subjects WHERE project_id=NEW.project_id AND name=NEW.subject),
            budget_attribute_id=(SELECT id FROM budget_attributes WHERE project_id=NEW.project_id AND name=NEW.category)
            WHERE id=NEW.id;
        END;
        CREATE TRIGGER IF NOT EXISTS budget_item_fk_scope BEFORE UPDATE OF funding_source_id,unit_id,budget_subject_id,budget_attribute_id ON budget_items BEGIN
          SELECT CASE WHEN NEW.funding_source_id IS NULL OR NOT EXISTS(SELECT 1 FROM funding_sources WHERE id=NEW.funding_source_id AND project_id=NEW.project_id) THEN RAISE(ABORT,'經費類別 ID 不屬於此專案') END;
          SELECT CASE WHEN NEW.unit_id IS NULL OR NOT EXISTS(SELECT 1 FROM units WHERE id=NEW.unit_id AND project_id=NEW.project_id) THEN RAISE(ABORT,'單位 ID 不屬於此專案') END;
          SELECT CASE WHEN NEW.budget_subject_id IS NULL OR NOT EXISTS(SELECT 1 FROM budget_subjects WHERE id=NEW.budget_subject_id AND project_id=NEW.project_id) THEN RAISE(ABORT,'預算科目 ID 不屬於此專案') END;
          SELECT CASE WHEN NEW.budget_attribute_id IS NULL OR NOT EXISTS(SELECT 1 FROM budget_attributes WHERE id=NEW.budget_attribute_id AND project_id=NEW.project_id) THEN RAISE(ABORT,'經費屬性 ID 不屬於此專案') END;
        END;
        CREATE TRIGGER IF NOT EXISTS expense_budget_item_id AFTER INSERT ON expenses BEGIN
          SELECT CASE WHEN NEW.batch_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM reimbursement_batches WHERE id=NEW.batch_id AND project_id=NEW.project_id) THEN RAISE(ABORT,'核銷批次不屬於此專案') END;
          SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM budget_items WHERE project_id=NEW.project_id AND source=NEW.source AND owner=NEW.owner AND subject=NEW.subject AND category=NEW.category) THEN RAISE(ABORT,'核銷資料找不到完全相符的預算項目') END;
          UPDATE expenses SET budget_item_id=(SELECT id FROM budget_items WHERE project_id=NEW.project_id AND source=NEW.source AND owner=NEW.owner AND subject=NEW.subject AND category=NEW.category) WHERE id=NEW.id;
        END;
        CREATE TRIGGER IF NOT EXISTS expense_budget_item_id_update AFTER UPDATE OF project_id,source,owner,subject,category ON expenses BEGIN
          SELECT CASE WHEN NEW.batch_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM reimbursement_batches WHERE id=NEW.batch_id AND project_id=NEW.project_id) THEN RAISE(ABORT,'核銷批次不屬於此專案') END;
          SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM budget_items WHERE project_id=NEW.project_id AND source=NEW.source AND owner=NEW.owner AND subject=NEW.subject AND category=NEW.category) THEN RAISE(ABORT,'核銷資料找不到完全相符的預算項目') END;
          UPDATE expenses SET budget_item_id=(SELECT id FROM budget_items WHERE project_id=NEW.project_id AND source=NEW.source AND owner=NEW.owner AND subject=NEW.subject AND category=NEW.category) WHERE id=NEW.id;
        END;
        CREATE TRIGGER IF NOT EXISTS expense_batch_project_update AFTER UPDATE OF project_id,batch_id ON expenses BEGIN
          SELECT CASE WHEN NEW.batch_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM reimbursement_batches WHERE id=NEW.batch_id AND project_id=NEW.project_id) THEN RAISE(ABORT,'核銷批次不屬於此專案') END;
        END;
        CREATE TRIGGER IF NOT EXISTS expense_budget_fk_scope BEFORE UPDATE OF budget_item_id ON expenses BEGIN
          SELECT CASE WHEN NEW.budget_item_id IS NULL OR NOT EXISTS(SELECT 1 FROM budget_items WHERE id=NEW.budget_item_id AND project_id=NEW.project_id) THEN RAISE(ABORT,'核銷預算項目 ID 不屬於此專案') END;
        END;
        CREATE TRIGGER IF NOT EXISTS transfer_budget_item_ids AFTER INSERT ON transfers BEGIN
          SELECT CASE WHEN NEW.status='executed' AND NOT EXISTS(SELECT 1 FROM budget_items WHERE project_id=NEW.project_id AND source=NEW.source AND owner=NEW.from_owner AND subject=NEW.from_subject AND category=NEW.from_category) THEN RAISE(ABORT,'流出項目找不到完全相符的預算') END;
          SELECT CASE WHEN NEW.status='executed' AND NOT EXISTS(SELECT 1 FROM budget_items WHERE project_id=NEW.project_id AND source=NEW.source AND owner=NEW.to_owner AND subject=NEW.to_subject AND category=NEW.to_category) THEN RAISE(ABORT,'流入項目找不到完全相符的預算') END;
          UPDATE transfers SET from_budget_item_id=(SELECT id FROM budget_items WHERE project_id=NEW.project_id AND source=NEW.source AND owner=NEW.from_owner AND subject=NEW.from_subject AND category=NEW.from_category),
            to_budget_item_id=CASE WHEN NEW.status='executed' THEN (SELECT id FROM budget_items WHERE project_id=NEW.project_id AND source=NEW.source AND owner=NEW.to_owner AND subject=NEW.to_subject AND category=NEW.to_category) ELSE NULL END WHERE id=NEW.id;
        END;
        CREATE TRIGGER IF NOT EXISTS transfer_budget_item_ids_update AFTER UPDATE OF project_id,source,from_owner,from_subject,from_category,to_owner,to_subject,to_category,status ON transfers BEGIN
          SELECT CASE WHEN NEW.status='executed' AND NOT EXISTS(SELECT 1 FROM budget_items WHERE project_id=NEW.project_id AND source=NEW.source AND owner=NEW.from_owner AND subject=NEW.from_subject AND category=NEW.from_category) THEN RAISE(ABORT,'流出項目找不到完全相符的預算') END;
          SELECT CASE WHEN NEW.status='executed' AND NOT EXISTS(SELECT 1 FROM budget_items WHERE project_id=NEW.project_id AND source=NEW.source AND owner=NEW.to_owner AND subject=NEW.to_subject AND category=NEW.to_category) THEN RAISE(ABORT,'流入項目找不到完全相符的預算') END;
          UPDATE transfers SET from_budget_item_id=(SELECT id FROM budget_items WHERE project_id=NEW.project_id AND source=NEW.source AND owner=NEW.from_owner AND subject=NEW.from_subject AND category=NEW.from_category),
            to_budget_item_id=CASE WHEN NEW.status='executed' THEN (SELECT id FROM budget_items WHERE project_id=NEW.project_id AND source=NEW.source AND owner=NEW.to_owner AND subject=NEW.to_subject AND category=NEW.to_category) ELSE NULL END WHERE id=NEW.id;
        END;
        CREATE TRIGGER IF NOT EXISTS transfer_budget_fk_scope BEFORE UPDATE OF from_budget_item_id,to_budget_item_id ON transfers BEGIN
          SELECT CASE WHEN NEW.status='executed' AND (NEW.from_budget_item_id IS NULL OR NOT EXISTS(SELECT 1 FROM budget_items WHERE id=NEW.from_budget_item_id AND project_id=NEW.project_id)) THEN RAISE(ABORT,'流出預算 ID 不屬於此專案') END;
          SELECT CASE WHEN NEW.status='executed' AND (NEW.to_budget_item_id IS NULL OR NOT EXISTS(SELECT 1 FROM budget_items WHERE id=NEW.to_budget_item_id AND project_id=NEW.project_id)) THEN RAISE(ABORT,'流入預算 ID 不屬於此專案') END;
          SELECT CASE WHEN NEW.status='planned' AND NEW.to_budget_item_id IS NOT NULL THEN RAISE(ABORT,'規劃中的流用不可連結實際流入預算') END;
          SELECT CASE WHEN NEW.from_budget_item_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM budget_items WHERE id=NEW.from_budget_item_id AND project_id=NEW.project_id) THEN RAISE(ABORT,'流出預算 ID 不屬於此專案') END;
        END;
        CREATE TRIGGER IF NOT EXISTS estimate_subject_id AFTER INSERT ON estimate_rules BEGIN
          SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM budget_subjects WHERE project_id=NEW.project_id AND name=NEW.subject) THEN RAISE(ABORT,'估算科目不屬於此專案') END;
          UPDATE estimate_rules SET budget_subject_id=(SELECT id FROM budget_subjects WHERE project_id=NEW.project_id AND name=NEW.subject) WHERE id=NEW.id;
        END;
        CREATE TRIGGER IF NOT EXISTS estimate_subject_id_update AFTER UPDATE OF project_id,subject ON estimate_rules BEGIN
          SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM budget_subjects WHERE project_id=NEW.project_id AND name=NEW.subject) THEN RAISE(ABORT,'估算科目不屬於此專案') END;
          UPDATE estimate_rules SET budget_subject_id=(SELECT id FROM budget_subjects WHERE project_id=NEW.project_id AND name=NEW.subject) WHERE id=NEW.id;
        END;
        CREATE TRIGGER IF NOT EXISTS estimate_subject_fk_scope BEFORE UPDATE OF budget_subject_id ON estimate_rules BEGIN
          SELECT CASE WHEN NEW.budget_subject_id IS NULL OR NOT EXISTS(SELECT 1 FROM budget_subjects WHERE id=NEW.budget_subject_id AND project_id=NEW.project_id) THEN RAISE(ABORT,'估算科目 ID 不屬於此專案') END;
        END;
        """)
        db.execute("INSERT OR IGNORE INTO schema_meta(version) VALUES(2)")
        db.execute("INSERT OR IGNORE INTO schema_meta(version) VALUES(3)")


def _upgrade_non_reusable_ids(db):
    """Upgrade legacy ledger IDs without losing rows or delete-log history."""
    definitions={
        'reimbursement_batches': "id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE, submitted_date TEXT DEFAULT '', description TEXT DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
        'expenses': "id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE, voucher_date TEXT DEFAULT '', batch_id INTEGER REFERENCES reimbursement_batches(id) ON DELETE SET NULL, owner TEXT NOT NULL, source TEXT NOT NULL, subject TEXT NOT NULL, category TEXT NOT NULL DEFAULT '', amount REAL NOT NULL CHECK(amount >= 0), note TEXT DEFAULT '', voided INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, budget_item_id INTEGER REFERENCES budget_items(id) ON DELETE RESTRICT",
    }
    needs=[]
    for table in ('reimbursement_batches','expenses'):
        row=db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
        if row and 'AUTOINCREMENT' not in (row['sql'] or '').upper(): needs.append(table)
    if not needs: return
    high={
        'reimbursement_batches': max([db.execute('SELECT COALESCE(MAX(id),0) FROM reimbursement_batches').fetchone()[0],db.execute('SELECT COALESCE(MAX(batch_id),0) FROM expense_delete_log').fetchone()[0]]),
        'expenses': max([db.execute('SELECT COALESCE(MAX(id),0) FROM expenses').fetchone()[0],db.execute('SELECT COALESCE(MAX(original_expense_id),0) FROM expense_delete_log').fetchone()[0]]),
    }
    db.commit()
    db.execute('PRAGMA foreign_keys=OFF')
    try:
        db.execute('BEGIN IMMEDIATE')
        # Triggers attached to expenses reference reimbursement_batches.
        # Drop them before replacing the parent table; initialize() recreates
        # them after the migration has completed.
        for trigger in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('reimbursement_batches','expenses')").fetchall():
            db.execute(f'DROP TRIGGER "{trigger[0].replace(chr(34), chr(34)*2)}"')
        for table in needs:
            cols=[r['name'] for r in db.execute(f'PRAGMA table_info({table})')]
            db.execute(f'CREATE TABLE {table}_new ({definitions[table]})')
            names=','.join('"'+c+'"' for c in cols)
            db.execute(f'INSERT INTO {table}_new ({names}) SELECT {names} FROM {table}')
            db.execute(f'DROP TABLE {table}')
            db.execute(f'ALTER TABLE {table}_new RENAME TO {table}')
            db.execute('DELETE FROM sqlite_sequence WHERE name=?',(table,))
            db.execute('INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)',(table,high[table]))
        violations=list(db.execute('PRAGMA foreign_key_check'))
        if violations: raise RuntimeError(f'資料表編號升級後外鍵檢查失敗：{[tuple(r) for r in violations[:5]]}')
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute('PRAGMA foreign_keys=ON')


def fnum(x):
    try:
        return round(float(x or 0), 4)
    except (ValueError, TypeError):
        return 0.0


def round_amount(x):
    try:
        raw=str(x if x is not None else '').strip()
        if isinstance(x,(int,float,Decimal)) and not isinstance(x,bool):value=Decimal(raw)
        else:
            if not re.fullmatch(r'[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?',raw):
                raise ValueError('金額格式不正確，請輸入數字，例如 2000 或 2,000')
            value=Decimal(raw.replace(',',''))
        return int(value.to_integral_value(rounding=ROUND_HALF_UP))
    except (InvalidOperation,ValueError,TypeError,OverflowError):
        raise ValueError('金額格式不正確，請輸入數字，例如 2000 或 2,000')


MAX_SAFE_MONEY=2**53-1
def input_amount(value,label):
    raw=str(value if value is not None else '').strip()
    if not re.fullmatch(r'[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?',raw):
        raise ValueError(f'{label}格式不正確，請輸入數字，例如 2000 或 2,000')
    decimal_value=Decimal(raw.replace(',',''))
    if decimal_value!=decimal_value.to_integral_value():
        raise ValueError(f'{label}須為整數元')
    amount=int(decimal_value)
    if abs(amount)>MAX_SAFE_MONEY:raise ValueError(f'{label}不可超過 {MAX_SAFE_MONEY:,} 元，以確保整數精度')
    return amount


def validate_transfer_date(project,value):
    date=parse_date(value,'流用日期')
    if not date or not project['start_date']<=date<=project['end_date']:
        raise ValueError('流用日期必須位於專案執行起日至執行迄日之間')
    return date


def validate_transfer_unlocked(row):
    if row and row['locked']:
        raise ValueError('此流用紀錄已鎖定，不可編輯或刪除；請先解鎖後再操作')


def transfer_overdraws(db,pid,ids):
    """Return per-budget deficits that would remain after selected transfers are removed."""
    out=[]
    for item in totals(db,pid):
        outgoing=sum(round_amount(r['amount'] or 0) for r in db.execute(
            f"SELECT amount FROM transfers WHERE project_id=? AND from_budget_item_id=? AND status='executed' AND id IN ({','.join('?' for _ in ids)})",
            [pid,item['id'],*ids])) if ids else 0
        incoming=sum(round_amount(r['amount'] or 0) for r in db.execute(
            f"SELECT amount FROM transfers WHERE project_id=? AND to_budget_item_id=? AND status='executed' AND id IN ({','.join('?' for _ in ids)})",
            [pid,item['id'],*ids])) if ids else 0
        after=item['balance']+outgoing-incoming
        released=sum(round_amount(r['amount'] or 0) for r in db.execute(
            f"SELECT amount FROM transfers WHERE project_id=? AND from_budget_item_id=? AND status='planned' AND id IN ({','.join('?' for _ in ids)})",
            [pid,item['id'],*ids])) if ids else 0
        required=max(0,item.get('reserved_out',0)-released)
        before=item['balance']
        if after<before and (after<0 or (required>0 and after<required)):
            out.append({'source':item['source'],'owner':item['owner'],'subject':item['subject'],'category':item['category'],'deficit':max(-after,required-after),'reserved_deficit':max(0,required-after) if required>0 else 0})
    return out


def expense_duplicate_key(submitted,voucher,source,owner,category,subject,amount,payee,note):
    return (submitted or '',voucher or '',source,owner,category,subject,round_amount(amount),payee or '',note or '')


OPTION_FIELDS=('source','owner','subject','category','from_owner','from_subject','from_category','to_owner','to_subject','to_category')


def clean_option_name(value):
    # Only fold fullwidth ASCII and the ideographic space; NFKC would also
    # change circled numbers and measurement symbols in legitimate names.
    folded=''.join(chr(ord(char)-0xFEE0) if '\uff01'<=char<='\uff5e' else ' ' if char in ('\u00a0','\u3000') else char for char in str(value or ''))
    return ''.join(char for char in folded if unicodedata.category(char)!='Cf').strip()


def clean_option_fields(row):
    for key in OPTION_FIELDS:
        if key in row:row[key]=clean_option_name(row[key])
    return row


def existing_expense_duplicates(db,pid):
    found={}
    for row in db.execute('''SELECT e.id,e.voucher_date,e.source,e.owner,e.category,e.subject,e.amount,e.note,
        COALESCE(b.submitted_date,'') submitted_date,
        COALESCE((SELECT GROUP_CONCAT(name,';') FROM (SELECT p.name FROM payees p WHERE p.expense_id=e.id ORDER BY p.id)),'') payee_names
        FROM expenses e LEFT JOIN reimbursement_batches b ON b.id=e.batch_id
        WHERE e.project_id=? AND e.voided=0 ORDER BY e.id''',(pid,)):
        key=expense_duplicate_key(row['submitted_date'],row['voucher_date'],row['source'],row['owner'],row['category'],row['subject'],row['amount'],row['payee_names'],row['note'])
        found.setdefault(key,[]).append({'id':row['id'],'date':row['submitted_date'],'source':row['source'],'owner':row['owner'],'category':row['category'],'subject':row['subject'],'amount':round_amount(row['amount'])})
    return found


def transfer_duplicate_key(row):
    return (row['date'],row['source'],row['from_owner'],row['from_subject'],row['from_category'],row['to_owner'],row['to_subject'],row['to_category'],round_amount(row['amount']),str(row.get('note') or '').strip())


def existing_transfer_duplicates(db,pid):
    found={}
    for raw in db.execute("SELECT * FROM transfers WHERE project_id=? AND amount>0 AND date<>'' ORDER BY id",(pid,)):
        row=dict(raw);key=transfer_duplicate_key(row)
        found.setdefault(key,[]).append({'id':row['id'],'date':row['date'],'source':row['source'],'from_owner':row['from_owner'],'from_category':row['from_category'],'from_subject':row['from_subject'],'to_owner':row['to_owner'],'to_category':row['to_category'],'to_subject':row['to_subject'],'amount':round_amount(row['amount'])})
    return found


def cross_unit_reservations(db,pid):
    rows=db.execute("SELECT from_owner,source,from_category,COALESCE(to_owner,'') to_owner,SUM(amount) amount FROM transfers WHERE project_id=? AND status='planned' AND from_budget_item_id IS NOT NULL AND amount>0 GROUP BY from_owner,source,from_category,COALESCE(to_owner,'')",(pid,))
    owner={};combo={}
    for r in rows:
        amount=round_amount(r['amount'])
        if r['to_owner']!=r['from_owner']:owner[r['from_owner']]=owner.get(r['from_owner'],0)+amount
        key=(r['from_owner'],r['source'],r['from_category']);combo[key]=combo.get(key,0)+amount
    return owner,combo


def planned_reservation_message(db,pid,combo,book_balance,reserved,action='核銷'):
    owner,source,category=combo
    records=list(db.execute("SELECT id,from_subject,to_owner,to_subject FROM transfers WHERE project_id=? AND status='planned' AND from_budget_item_id IS NOT NULL AND amount>0 AND from_owner=? AND source=? AND from_category=? ORDER BY id",(pid,owner,source,category)))
    detail='；'.join(f"#{r['id']} {r['from_subject']} → {(r['to_owner']+'／'+r['to_subject']) if r['to_owner'] and r['to_subject'] else '流入尚未指定'}" for r in records)
    return f'單位「{owner}」／經費類別「{source}」／經費屬性「{category}」目前組合餘額 {book_balance:,} 元，其中規劃流用保留 {reserved:,} 元（{detail}）；本筆最多可{action} {max(0,book_balance-reserved):,} 元。如需動用，請先調整或刪除該規劃流用'


def validate_ledger_caps(db,pid):
    items=totals(db,pid);owner_reserved,combo_reserved=cross_unit_reservations(db,pid)
    groups=[('全專案',items,False)]
    groups.extend((f'單位「{name}」', [x for x in items if x['owner']==name], True) for name in {x['owner'] for x in items})
    groups.extend((f'經費類別「{name}」',[x for x in items if x['source']==name],False) for name in {x['source'] for x in items})
    groups.extend((f'單位「{o}」／經費類別「{s}」／經費屬性「{c}」',[x for x in items if (x['owner'],x['source'],x['category'])==(o,s,c)],True) for o,s,c in {(x['owner'],x['source'],x['category']) for x in items})
    for label,rows,subtract_reservation in groups:
        balance=sum(x['budget']-x['spent'] for x in rows)
        book_balance=balance;reserved=0;combo=None
        if subtract_reservation:
            if rows and label.startswith('單位「') and '／經費類別' not in label:balance-=owner_reserved.get(rows[0]['owner'],0)
            elif rows:
                combo=(rows[0]['owner'],rows[0]['source'],rows[0]['category']);reserved=combo_reserved.get(combo,0);balance-=reserved
        if balance<0:
            if combo and reserved and book_balance>=0:raise ValueError(planned_reservation_message(db,pid,combo,book_balance,reserved,'核銷或流出'))
            raise ValueError(f'此操作會使{label}可用餘額變成 {balance:,} 元，無法儲存；本次未變更任何資料')


def ledger_cap_balances(db,pid):
    items=totals(db,pid);owner_reserved,combo_reserved=cross_unit_reservations(db,pid)
    result={('project',):sum(x['budget']-x['spent'] for x in items)}
    for item in items:
        value=item['budget']-item['spent']
        for key in (('owner',item['owner']),('source',item['source']),('combo',item['owner'],item['source'],item['category'])):
            result[key]=result.get(key,0)+value
    for owner,amount in owner_reserved.items():result[('owner',owner)]=result.get(('owner',owner),0)-amount
    for key,amount in combo_reserved.items():result[('combo',*key)]=result.get(('combo',*key),0)-amount
    return result


def validate_reserved_funding(db,pid):
    for item in totals(db,pid):
        if item['reserved_out']>0 and item['reserved_out']>item['balance']:
            raise ValueError(f"此操作會占用「{item['owner']}／{item['subject']}」已圈存的規劃流用 {item['reserved_out']:,} 元；請先調整或取消規劃，本次未變更資料")


def backup_ledger_issues(db):
    issues=[]
    for project in db.execute('SELECT id,name,total_budget FROM projects ORDER BY id'):
        pid=project['id'];items=totals(db,pid)
        def add(level,name,excess):
            if excess>0:issues.append({'project_id':pid,'project_name':project['name'],'level':level,'name':name,'excess':excess})
        allocated=sum(round_amount(x['initial']) for x in items)
        add('專案總額','預算明細合計',allocated-round_amount(project['total_budget']))
        spent=sum(x['spent'] for x in items)
        add('專案核銷','專案經費總額',spent-round_amount(project['total_budget']))
        for key,balance in ledger_cap_balances(db,pid).items():
            if len(key)==1:level,name='專案','全專案'
            elif key[0]=='owner':level,name='單位',key[1]
            elif key[0]=='source':level,name='經費類別',key[1]
            else:level,name='組合','／'.join(key[1:])
            add(level,name,-balance)
        for item in items:
            label='／'.join(str(item[k]) for k in ('source','owner','category','subject'))
            if item['reserved_out']>0 and item['reserved_out']>item['balance']:
                add('規劃圈存',label,item['reserved_out']-item['balance'])
    return issues


def check_source_caps(db, project_id, totals_by_source):
    p=require_project(db,project_id)
    allocated=sum(round_amount(v) for v in totals_by_source.values())
    if allocated>round_amount(p['total_budget']):
        raise ValueError(f'預算明細合計 {allocated:,} 元，超過專案總額 {round_amount(p["total_budget"]):,} 元')


def parse_date(value, field):
    raw=str(value if value is not None else '').strip()
    if not raw:return ''
    try:
        if re.fullmatch(r'\d{8}',raw):
            return datetime.strptime(raw,'%Y%m%d').date().isoformat()
        return datetime.strptime(raw.replace('/','-'),'%Y-%m-%d').date().isoformat()
    except (ValueError,TypeError,OverflowError,InvalidOperation):
        raise ValueError(f'{field}請使用有效的 yyyy/mm/dd 日期') from None


def validate_expense_dates(project, reimburse, voucher, row_label='核銷紀錄'):
    if not project['start_date'] or not project['end_date']:
        raise ValueError('請先在專案資料填妥執行起日與執行迄日，才能填寫登錄日期')
    start=parse_date(project['start_date'],'專案執行起日')
    end=parse_date(project['end_date'],'專案執行迄日')
    for label,value in (('登錄日期',reimburse),('憑證日期',voucher)):
        if value and ((start and value<start) or (end and value>end)):
            raise ValueError(f'{row_label}的{label}須在專案執行期間內（{start or "未設定"} 至 {end or "未設定"}）')
    if reimburse and voucher and voucher>reimburse:
        raise ValueError(f'{row_label}的憑證日期不可晚於登錄日期')


def validate_payee_name(value):
    name=str(value or '').strip()
    if not name:raise ValueError('請填寫付款對象名稱')
    if ';' in name or '；' in name:raise ValueError('請勿於付款對象名稱內輸入「;」或「；」；分號僅用於匯入檔的多人分隔')
    return name


def import_payee_pairs(names, amounts, total, row_num):
    names=[x.strip() for x in re.split(r'[;；]',str(names or ''))] if str(names or '').strip() else []
    values=[x.strip() for x in re.split(r'[;；]',str(amounts or ''))] if str(amounts or '').strip() else []
    if not names and not values:return []
    if len(names)!=len(values) or any(not x for x in names) or any(not x for x in values):
        raise ValueError(f'第 {row_num} 列付款對象與分配金額需成對填寫（以「；」分隔）')
    pairs=[{'name':n,'amount':input_amount(v,f'第 {row_num} 列付款分配金額')} for n,v in zip(names,values)]
    if sum(x['amount'] for x in pairs)!=total:
        raise ValueError(f'第 {row_num} 列付款分配合計需等於核銷金額 {total:,} 元')
    return pairs


def execution_ratio(spent,budget):
    return abs(spent)/budget if budget>0 else None


def execution_percent(spent,budget):
    return float((Decimal(abs(spent))*100/Decimal(budget)).quantize(Decimal('0.1'),rounding=ROUND_HALF_UP)) if budget>0 else '—'


def execution_label(ratio):
    return f"{(Decimal(str(ratio))*100).quantize(Decimal('0.1'),rounding=ROUND_HALF_UP)}%" if ratio is not None else '—'


def totals(db, project_id):
    rows=db.execute("""
      SELECT b.*,fs.name AS source_label,u.name AS owner_label,bs.name AS subject_label,ba.name AS category_label,
        COALESCE((SELECT SUM(t.amount) FROM transfers t WHERE t.to_budget_item_id=b.id AND t.status='executed'),0) AS incoming,
        COALESCE((SELECT SUM(t.amount) FROM transfers t WHERE t.from_budget_item_id=b.id AND t.status='executed'),0) AS outgoing,
        COALESCE((SELECT SUM(t.amount) FROM transfers t WHERE t.from_budget_item_id=b.id AND t.status='planned'),0) AS reserved_out,
        COALESCE((SELECT SUM(e.amount) FROM expenses e WHERE e.budget_item_id=b.id AND e.voided=0),0) AS spent
      FROM budget_items b
      LEFT JOIN funding_sources fs ON fs.id=b.funding_source_id
      LEFT JOIN units u ON u.id=b.unit_id
      LEFT JOIN budget_subjects bs ON bs.id=b.budget_subject_id
      LEFT JOIN budget_attributes ba ON ba.id=b.budget_attribute_id
      WHERE b.project_id=? ORDER BY source_label,subject_label,owner_label
    """,(project_id,)).fetchall()
    result=[]
    for row in rows:
        item=dict(row); item['source']=item.pop('source_label') or item['source']; item['owner']=item.pop('owner_label') or item['owner']; item['subject']=item.pop('subject_label') or item['subject']; item['category']=item.pop('category_label') or item['category']
        item['incoming']=round_amount(item['incoming']); item['outgoing']=round_amount(item['outgoing']); item['spent']=round_amount(item['spent'])
        item['reserved_out']=round_amount(item['reserved_out']); item['budget']=round_amount(item['initial'])+item['incoming']-item['outgoing']; item['balance']=item['budget']-item['spent']; item['available_balance']=item['balance']-item['reserved_out']; item['rate']=execution_ratio(item['spent'],item['budget'])
        result.append(item)
    return result


def flow_change_report(db, project_id, start, end, include_category=False):
    if not start or not end or end < start: raise ValueError('請輸入有效的流用日期範圍')
    if include_category:
        groups=db.execute("""SELECT b.funding_source_id,b.budget_subject_id,b.budget_attribute_id,fs.name AS source,bs.name AS subject,ba.name AS category FROM budget_items b JOIN funding_sources fs ON fs.id=b.funding_source_id JOIN budget_subjects bs ON bs.id=b.budget_subject_id LEFT JOIN budget_attributes ba ON ba.id=b.budget_attribute_id WHERE b.project_id=? GROUP BY b.funding_source_id,b.budget_subject_id,b.budget_attribute_id ORDER BY fs.name,ba.name,bs.name""",(project_id,)).fetchall()
    else:
        groups=db.execute("""SELECT b.funding_source_id,b.budget_subject_id,fs.name AS source,bs.name AS subject FROM budget_items b JOIN funding_sources fs ON fs.id=b.funding_source_id JOIN budget_subjects bs ON bs.id=b.budget_subject_id WHERE b.project_id=? GROUP BY b.funding_source_id,b.budget_subject_id ORDER BY fs.name,bs.name""",(project_id,)).fetchall()
    report=[]
    for g in groups:
        args=(project_id,g['funding_source_id'],g['budget_subject_id'])
        category_clause=' AND budget_attribute_id IS ?' if include_category else ''
        transfer_category_clause=' AND x.budget_attribute_id IS ?' if include_category else ''
        query_args=args+(g['budget_attribute_id'],) if include_category else args
        base=db.execute("SELECT COALESCE(SUM(initial),0) FROM budget_items WHERE project_id=? AND funding_source_id=? AND budget_subject_id=?"+category_clause,query_args).fetchone()[0]
        def moved(column, cutoff, operator):
            return db.execute(f"SELECT COALESCE(SUM(t.amount),0) FROM transfers t JOIN budget_items x ON x.id=t.{column} WHERE t.project_id=? AND t.status='executed' AND x.funding_source_id=? AND x.budget_subject_id=?"+transfer_category_clause+f" AND t.date{operator}?",query_args+(cutoff,)).fetchone()[0]
        before=round_amount(base)+round_amount(moved('to_budget_item_id',start,'<'))-round_amount(moved('from_budget_item_id',start,'<'))
        after=round_amount(base)+round_amount(moved('to_budget_item_id',end,'<='))-round_amount(moved('from_budget_item_id',end,'<='))
        if after!=before:
            item={'source':g['source'],'subject':g['subject'],'before':before,'after':after,'change':after-before}
            if include_category:item['category']=g['category'] or '未分類'
            report.append(item)
    return report


def bootstrap():
    with connect() as db:
        projects = [dict(r) for r in db.execute("SELECT * FROM projects ORDER BY year DESC,name")]
        mail_cfg=db.execute("SELECT * FROM email_smtp_settings WHERE id=1").fetchone()
        setting=db.execute("SELECT value FROM app_settings WHERE key='default_estimate_rate'").fetchone()
        def setting_json(key,default):
            row=db.execute("SELECT value FROM app_settings WHERE key=?",(key,)).fetchone()
            try:return json.loads(row[0]) if row else default
            except (TypeError,json.JSONDecodeError):return default
        data = {'server_session':SERVER_SESSION,'projects':projects,'budgets':[],'expenses':[],'batches':[],'project_notes':{},'transfers':[],'payees':{},'rules':[],'applications':[],'subjects':[],'owners':[],'sources':[],'options':{},'optionRows':[],'email_recipients':[],'email_logs':[],'email_config':dict(mail_cfg) if mail_cfg else {},'settings':{'default_estimate_rate':fnum(setting[0]) if setting else 2.11,'table_alignments':setting_json('table_alignments',{}),'custom_theme':setting_json('custom_theme_palette',{}),'theme_choice':setting_json('theme_choice',None)}}
        if mail_cfg:
            data['email_config']['credential_saved']=smtp_credential_saved(mail_cfg['username'] or mail_cfg['sender_email'])
        for rec in db.execute("SELECT r.*,u.name AS unit_name FROM email_recipients r LEFT JOIN units u ON u.id=r.unit_id ORDER BY r.project_id,r.role,r.name"):
            data['email_recipients'].append(dict(rec))
        data['email_logs']=[dict(r) for r in db.execute("SELECT l.*,b.project_id,b.report_as_of FROM email_delivery_logs l JOIN email_send_batches b ON b.id=l.batch_id ORDER BY l.id DESC")]
        bg=db.execute("SELECT value FROM app_settings WHERE key='theme_background'").fetchone();data['theme_background']=bg[0] if bg else ''
        mark=db.execute("SELECT value FROM app_settings WHERE key='brandmark'").fetchone();data['brandmark']=json.loads(mark[0]) if mark else {'type':'emoji','value':'經'}
        for p in projects:
            pid=p['id']; data['budgets'] += totals(db,pid)
            batches=[dict(r) for r in db.execute("SELECT * FROM reimbursement_batches WHERE project_id=? ORDER BY id DESC",(pid,))]
            data['batches'] += batches
            batch_dates={x['id']:x.get('submitted_date') or '' for x in batches}
            note=db.execute("SELECT content FROM project_notes WHERE project_id=?",(pid,)).fetchone()
            data['project_notes'][str(pid)]=note['content'] if note else ''
            exps=[dict(r) for r in db.execute("SELECT * FROM expenses WHERE project_id=? ORDER BY id DESC",(pid,))]
            for e in exps:
                e['submitted_date']=batch_dates.get(e.get('batch_id'),'')
                data['payees'][str(e['id'])]=[dict(x) for x in db.execute("SELECT * FROM payees WHERE expense_id=?",(e['id'],))]
            data['expenses'] += exps
            data['transfers'] += [dict(r) for r in db.execute("SELECT * FROM transfers WHERE project_id=? ORDER BY date DESC,id DESC",(pid,))]
            data['rules'] += [dict(r) for r in db.execute("SELECT * FROM estimate_rules WHERE project_id=? ORDER BY subject",(pid,))]
            # Return the typed, project-scoped master lists, including sources.
            option_tables={'source':'funding_sources','owner':'units','subject':'budget_subjects','category':'budget_attributes'}
            opts={k:[] for k in option_tables}
            for kind,table in option_tables.items():
                opts[kind]=[r['name'] for r in db.execute(f"SELECT name FROM {table} WHERE project_id=? ORDER BY name",(pid,))]
            for r in db.execute("SELECT * FROM project_options WHERE project_id=? ORDER BY kind,value",(pid,)):
                data['optionRows'].append(dict(r))
            data['options'][str(pid)]=opts
        data['subjects']=sorted({r['subject'] for r in data['budgets']})
        data['owners']=sorted({r['owner'] for r in data['budgets']})
        data['recent_payees']=[r[0] for r in db.execute("SELECT DISTINCT name FROM payees ORDER BY id DESC LIMIT 100")]
        return data


def _smtp_account_for_install(install_id,username):
    import hashlib
    return hashlib.sha256((str(install_id)+'|'+str(username)).encode('utf-8')).hexdigest()


def _legacy_smtp_keyring_account(username):
    import hashlib
    return hashlib.sha256((str(DB_PATH.resolve())+'|'+str(username)).encode('utf-8')).hexdigest()


def smtp_keyring_account(username):
    with connect() as db:
        row=db.execute("SELECT value FROM app_settings WHERE key='smtp_install_id'").fetchone()
    if not row:raise ValueError('找不到本機郵件憑證識別資料，請重新啟動系統')
    return _smtp_account_for_install(row['value'],username)


def smtp_credential_saved(username):
    if not username:return False
    try:
        import keyring
        return bool(keyring.get_password('project-finance-smtp',smtp_keyring_account(username)))
    except Exception:return False


def smtp_password(username):
    try:import keyring
    except ImportError:raise ValueError('尚未安裝安全憑證套件。請關閉系統後執行 Install-optional-packages.bat 安裝選用套件，再重新啟動。')
    value=keyring.get_password('project-finance-smtp',smtp_keyring_account(username))
    if not value:raise ValueError('尚未設定寄件密碼或應用程式專用密碼。請在「寄件設定」輸入密碼並儲存。')
    return value


def require_project(db, pid):
    p=db.execute("SELECT * FROM projects WHERE id=?",(pid,)).fetchone()
    if not p: raise ValueError('找不到專案')
    return p


def ensure_project_option(db, project_id, kind, value):
    value=str(value or '').strip()
    if value and kind in ('owner','subject','category','source'):
        db.execute("INSERT OR IGNORE INTO project_options(project_id,kind,value) VALUES(?,?,?)",(project_id,kind,value))


def require_project_option(db, project_id, kind, value, label):
    value=str(value or '').strip()
    if not value or not db.execute("SELECT 1 FROM project_options WHERE project_id=? AND kind=? AND value=?",(project_id,kind,value)).fetchone():
        raise ValueError(f'「{value or label}」不是本專案已建立的{label}，請先到「選項資料」新增或選取既有項目')


DELETE_OPTION_FIELD={'source':'source','owner':'owner','subject':'subject','category':'category'}


def deletion_impact(db, project_id, entity, ids):
    """Describe downstream records before removing budget lines or project options."""
    if entity not in ('budget','option'):raise ValueError('不支援此類資料的相依檢視')
    ids=list(dict.fromkeys(int(x) for x in ids))
    if not ids:raise ValueError('請先選取要刪除的資料')
    if entity=='budget':
        marks=','.join('?' for _ in ids)
        selected=[dict(r) for r in db.execute(f"SELECT * FROM budget_items WHERE project_id=? AND id IN ({marks}) ORDER BY id",[project_id,*ids])]
        if len(selected)!=len(ids):raise ValueError('部分預算項目已不存在或不屬於目前專案，請重新整理')
        options=[]
    else:
        marks=','.join('?' for _ in ids)
        selected=[dict(r) for r in db.execute(f"SELECT * FROM project_options WHERE project_id=? AND id IN ({marks}) ORDER BY kind,value",[project_id,*ids])]
        if len(selected)!=len(ids):raise ValueError('部分選項已不存在或不屬於目前專案，請重新整理')
        options=selected

    def option_match(row, kind, value):
        column=DELETE_OPTION_FIELD.get(kind)
        if column and row.get(column)==value:return True
        transfer_fields={'source':('source',),'owner':('from_owner','to_owner'),'subject':('from_subject','to_subject'),'category':('from_category','to_category')}.get(kind,())
        return any(row.get(field)==value for field in transfer_fields)

    all_budgets=[dict(r) for r in db.execute('SELECT * FROM budget_items WHERE project_id=? ORDER BY source,owner,subject,category',(project_id,))]
    budget_ids={r['id'] for r in selected} if entity=='budget' else set()
    if entity=='option':
        budget_ids={b['id'] for b in all_budgets if any(option_match(b,o['kind'],o['value']) for o in options)}
    budget_rows=[b for b in all_budgets if b['id'] in budget_ids]
    signatures={(b['source'],b['owner'],b['subject'],b['category']) for b in budget_rows}

    expenses=[]
    for raw in db.execute("SELECT e.*,rb.description AS batch_description,rb.submitted_date FROM expenses e LEFT JOIN reimbursement_batches rb ON rb.id=e.batch_id WHERE e.project_id=? ORDER BY COALESCE(rb.submitted_date,''),e.voucher_date,e.id",(project_id,)):
        row=dict(raw)
        expense_sig=(row.get('source'),row.get('owner'),row.get('subject'),row.get('category',''))
        if row.get('budget_item_id') in budget_ids or expense_sig in signatures or (entity=='option' and any(option_match(row,o['kind'],o['value']) for o in options)):
            row['status_label']='作廢' if row.get('voided') else ('待核銷' if is_pending_expense(row) else '已核銷')
            expenses.append(row)

    transfers=[]
    for raw in db.execute('SELECT * FROM transfers WHERE project_id=? ORDER BY date,id',(project_id,)):
        row=dict(raw)
        source_sig=(row.get('source'),row.get('from_owner'),row.get('from_subject'),row.get('from_category',''))
        target_sig=(row.get('source'),row.get('to_owner'),row.get('to_subject'),row.get('to_category',''))
        tied=(row.get('from_budget_item_id') in budget_ids or row.get('to_budget_item_id') in budget_ids or
              source_sig in signatures or target_sig in signatures)
        if tied or (entity=='option' and any(option_match(row,o['kind'],o['value']) for o in options)):
            row['status_label']='規劃中' if row.get('status')=='planned' else ('已鎖定' if row.get('locked') else '已流用')
            transfers.append(row)

    estimate_rules=[]; recipients=[]
    if entity=='option':
        for option in options:
            if option['kind']=='subject':
                estimate_rules += [dict(r) for r in db.execute('SELECT * FROM estimate_rules WHERE project_id=? AND subject=? ORDER BY id',(project_id,option['value']))]
            elif option['kind']=='owner':
                recipients += [dict(r) for r in db.execute('''SELECT r.id,r.role,r.name,r.email,u.name AS unit_name
                    FROM email_recipients r LEFT JOIN units u ON u.id=r.unit_id
                    WHERE r.project_id=? AND u.name=? ORDER BY r.name''',(project_id,option['value']))]
    budget_totals={r['id']:r for r in totals(db,project_id)}
    for row in budget_rows:
        total=budget_totals.get(row['id'],{})
        row.update({k:total.get(k,0) for k in ('budget','spent','balance','incoming','outgoing','reserved_out')})
    executed=[r for r in transfers if r.get('status')!='planned']
    plans=[r for r in transfers if r.get('status')=='planned']
    locked_plans=[r for r in plans if r.get('locked')]

    # Preview with the same validation used by the final write.
    cap_blockers=[]
    if budget_ids and not (expenses or executed or locked_plans):
        db.execute('SAVEPOINT deletion_preview_check')
        try:
            plan_ids=[r['id'] for r in plans]
            if plan_ids:db.execute('DELETE FROM transfers WHERE project_id=? AND id IN ('+','.join('?' for _ in plan_ids)+')',[project_id,*plan_ids])
            db.execute('DELETE FROM budget_items WHERE project_id=? AND id IN ('+','.join('?' for _ in budget_ids)+')',[project_id,*budget_ids])
            validate_ledger_caps(db,project_id)
            validate_reserved_funding(db,project_id)
        except ValueError as error:
            message=str(error);matched=re.search(r'使(.+?)可用餘額變成 (-[\d,]+) 元',message)
            cap_blockers.append({'label':matched.group(1) if matched else message,'deficit':abs(int(matched.group(2).replace(',',''))) if matched else 0})
        finally:
            db.execute('ROLLBACK TO deletion_preview_check');db.execute('RELEASE deletion_preview_check')
    blockers=[]
    if expenses:blockers.append({'type':'expense','count':len(expenses)})
    if executed:blockers.append({'type':'transfer','count':len(executed)})
    if locked_plans:blockers.append({'type':'locked_plan','count':len(locked_plans)})
    if cap_blockers:blockers.append({'type':'cap','count':len(cap_blockers)})
    selected_labels=[]
    if entity=='budget':
        selected_labels=[{'id':r['id'],'label':f"{r['source']}／{r['owner']}／{r['category']}／{r['subject']}"} for r in selected]
    else:
        selected_labels=[{'id':r['id'],'kind':r['kind'],'label':r['value']} for r in selected]
    return {'entity':entity,'project_id':project_id,'selected':selected_labels,'deletable':not blockers,
        'blocking':blockers,'budgets':budget_rows,'expenses':expenses,'transfers':transfers,
        'planned_transfers':plans,'locked_plans':locked_plans,'estimate_rules':estimate_rules,'recipients':recipients,'cap_blockers':cap_blockers,
        'counts':{'budgets':len(budget_rows),'expenses':len(expenses),'executed_transfers':len(executed),
          'planned_transfers':len(plans),'locked_plans':len(locked_plans),'estimate_rules':len(estimate_rules),'recipients':len(recipients)}}


def delete_with_impact(db, project_id, entity, ids):
    impact=deletion_impact(db,project_id,entity,ids)
    if not impact['deletable']:
        parts=[]
        if impact['counts']['expenses']:parts.append(f"核銷紀錄 {impact['counts']['expenses']} 筆")
        if impact['counts']['executed_transfers']:parts.append(f"已流用紀錄 {impact['counts']['executed_transfers']} 筆")
        if impact['counts']['locked_plans']:parts.append(f"已鎖定規劃流用 {impact['counts']['locked_plans']} 筆")
        if impact['cap_blockers']:parts.append('刪除後會使既有經費上限超支')
        raise ValueError('已有帳務紀錄或經費上限受影響，為保留帳面金流，無法刪除：'+'、'.join(parts)+'。請先檢視相依資料。')
    plan_ids=[r['id'] for r in impact['planned_transfers']]
    if plan_ids:
        marks=','.join('?' for _ in plan_ids)
        db.execute(f'DELETE FROM transfers WHERE project_id=? AND id IN ({marks})',[project_id,*plan_ids])
    if entity=='option':
        option_rows=[dict(r) for r in db.execute('SELECT * FROM project_options WHERE project_id=? AND id IN ('+','.join('?' for _ in ids)+')',[project_id,*ids])]
        for option in option_rows:
            if option['kind']=='subject':db.execute('DELETE FROM estimate_rules WHERE project_id=? AND subject=?',(project_id,option['value']))
            elif option['kind']=='owner':db.execute('DELETE FROM email_recipients WHERE project_id=? AND unit_id=(SELECT id FROM units WHERE project_id=? AND name=?)',(project_id,project_id,option['value']))
    budget_ids=[r['id'] for r in impact['budgets']]
    if budget_ids:
        marks=','.join('?' for _ in budget_ids)
        db.execute(f'DELETE FROM budget_items WHERE project_id=? AND id IN ({marks})',[project_id,*budget_ids])
    if entity=='option':
        marks=','.join('?' for _ in ids)
        db.execute(f'DELETE FROM project_options WHERE project_id=? AND id IN ({marks})',[project_id,*ids])
    validate_ledger_caps(db,project_id)
    validate_reserved_funding(db,project_id)
    return impact


def is_pending_expense(row):
    submitted=str(row.get('submitted_date') or '')
    voucher=str(row.get('voucher_date') or '')
    valid=lambda value: bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}',value))
    return not row.get('voided') and (not valid(submitted) or not valid(voucher) or submitted>date.today().isoformat())


def strict_positive_ids(value, label):
    if not isinstance(value,list): raise ValueError(f'{label}格式不正確')
    result=[]
    for item in value:
        if isinstance(item,bool) or not (isinstance(item,int) or isinstance(item,str) and re.fullmatch(r'[0-9]+',item)):
            raise ValueError(f'{label}格式不正確')
        number=int(item)
        if number<=0: raise ValueError(f'{label}格式不正確')
        if number not in result: result.append(number)
    return result


def apply_action(a):
    op=a.get('op')
    if op in ('budget_save','expense_save','transfer_save','reference_save') and isinstance(a.get('data'),dict):
        clean_option_fields(a['data'])
        if op=='reference_save':
            a['data']['value']=clean_option_name(a['data'].get('value'))
    if op in ('import_budget','import_expenses','import_transfers'):
        for row in a.get('rows',[]):clean_option_fields(row)
    if op=='reference_save_many':a['values']=[clean_option_name(value) for value in a.get('values',[])]
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        tables={'project_save':'projects','budget_save':'budget_items','expense_save':'expenses','transfer_save':'transfers','reference_save':'project_options','batch_save':'reimbursement_batches','email_recipient_save':'email_recipients'}
        data=a.get('data') or {}
        if op in tables and data.get('id'):
            table=tables[op]
            row=db.execute(f'SELECT * FROM {table} WHERE id=?',(int(data['id']),)).fetchone()
            if not row or (data.get('project_id') and 'project_id' in row.keys() and row['project_id']!=int(data['project_id'])):
                raise ValueError('這筆資料已不存在或不屬於目前專案，請重新整理後確認')
        if op=='email_recipient_save':
            v=a.get('data',{});pid=int(v.get('project_id') or 0);project=require_project(db,pid)
            role=str(v.get('role') or '');name=str(v.get('name') or '').strip();address=str(v.get('email') or '').strip()
            if role not in ('unit','manager'):raise ValueError('請選擇收件人類型')
            if not name:raise ValueError('請輸入收件人姓名或稱呼')
            if not valid_email(address):raise ValueError('請輸入有效的 Email 地址')
            rid=int(v['id']) if v.get('id') else None
            unit_id=int(v.get('unit_id') or 0)
            unit=db.execute('SELECT id FROM units WHERE id=? AND project_id=?',(unit_id,pid)).fetchone()
            if not unit:raise ValueError('請選擇本專案已建立的所屬單位')
            if rid:
                existing=db.execute('SELECT id FROM email_recipients WHERE id=? AND project_id=?',(rid,pid)).fetchone()
                if not existing:raise ValueError('找不到此收件人')
                db.execute('UPDATE email_recipients SET role=?,unit_id=?,name=?,email=?,enabled=1 WHERE id=?',(role,unit_id,name,address,rid))
            else:
                cur=db.execute('INSERT INTO email_recipients(project_id,role,unit_id,name,email) VALUES(?,?,?,?,?)',(pid,role,unit_id,name,address));rid=cur.lastrowid
            return {'ok':True,'id':rid}
        if op=='email_recipient_bulk_delete':
            pid=int(a.get('project_id') or 0);require_project(db,pid);ids=sorted({int(x) for x in a.get('ids',[]) if str(x).isdigit()})
            if not ids:raise ValueError('請先選取要刪除的收件人')
            marks=','.join('?' for _ in ids);found={r[0] for r in db.execute(f'SELECT id FROM email_recipients WHERE project_id=? AND id IN ({marks})',(pid,*ids))}
            if found!=set(ids):raise ValueError('部分收件人已不存在；本次未執行刪除')
            db.execute(f'DELETE FROM email_recipients WHERE project_id=? AND id IN ({marks})',(pid,*ids));return {'ok':True,'count':len(ids)}
        if op=='email_logs_delete':
            pid=int(a.get('project_id') or 0);require_project(db,pid)
            ids=sorted({int(x) for x in a.get('ids',[]) if str(x).isdigit()})
            if not ids:raise ValueError('請先選取要刪除的寄送紀錄')
            placeholders=','.join('?' for _ in ids)
            cur=db.execute(f"DELETE FROM email_delivery_logs WHERE id IN ({placeholders}) AND batch_id IN (SELECT id FROM email_send_batches WHERE project_id=?)",(*ids,pid))
            removed=cur.rowcount
            db.execute('DELETE FROM email_send_batches WHERE project_id=? AND NOT EXISTS(SELECT 1 FROM email_delivery_logs WHERE batch_id=email_send_batches.id)',(pid,))
            return {'ok':True,'count':removed}
        if op=='email_smtp_save':
            v=a.get('data',{});host=str(v.get('host') or '').strip();sender=str(v.get('sender_email') or '').strip();username=str(v.get('username') or '').strip();port=int(v.get('port') or 0);security=str(v.get('security') or '')
            if not host:raise ValueError('請輸入郵件伺服器名稱')
            if not 1<=port<=65535:raise ValueError('連接埠需介於 1 到 65535')
            if security not in ('starttls','ssl','none'):raise ValueError('請選擇連線安全方式')
            if not valid_email(sender):raise ValueError('請輸入有效的寄件 Email 地址')
            old=db.execute('SELECT username,sender_email FROM email_smtp_settings WHERE id=1').fetchone();old_identity=(old['username'] or old['sender_email']) if old else ''
            identity=username or sender;password=str(v.get('password') or '');clear_password=bool(v.get('clear_password'))
            if not password and identity!=old_identity and identity and not clear_password:raise ValueError('寄件帳號變更時請重新輸入密碼或應用程式專用密碼')
            if password:
                try:
                    import keyring
                    keyring.set_password('project-finance-smtp',smtp_keyring_account(identity),password)
                except ImportError:raise ValueError('尚未安裝安全憑證套件。請關閉系統後執行 Install-optional-packages.bat 安裝選用套件，再重新啟動。')
                except Exception as e:raise ValueError(f'無法安全儲存密碼：{e}')
            elif clear_password:
                try:
                    import keyring
                    for old_user in {old_identity,identity}:
                        if old_user:
                            for account in {smtp_keyring_account(old_user),_legacy_smtp_keyring_account(old_user)}:
                                try:keyring.delete_password('project-finance-smtp',account)
                                except Exception:pass
                except Exception:pass
            db.execute("INSERT INTO email_smtp_settings(id,host,port,security,username,sender_email,sender_name,updated_at) VALUES(1,?,?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(id) DO UPDATE SET host=excluded.host,port=excluded.port,security=excluded.security,username=excluded.username,sender_email=excluded.sender_email,sender_name=excluded.sender_name,updated_at=CURRENT_TIMESTAMP",(host,port,security,username,sender,str(v.get('sender_name') or '').strip()))
            return {'ok':True,'credential_saved':smtp_credential_saved(identity)}
        if op=='project_save':
            v=a.get('data',{}); name=(v.get('name') or '').strip()
            if not name: raise ValueError('請輸入專案名稱')
            code=str(v.get('code','')).strip(); pid=int(v['id']) if v.get('id') else None
            if not code:raise ValueError('專案編號必填')
            start=parse_date(v.get('start_date'),'執行起日'); end=parse_date(v.get('end_date'),'執行迄日')
            if not start or not end:raise ValueError('執行起日與執行迄日皆必填')
            if start>end:raise ValueError('執行起日不可晚於執行迄日')
            if not str(v.get('total_budget','')).strip():raise ValueError('請輸入專案經費總額')
            for existing in db.execute("SELECT id,code FROM projects"):
                if existing['id']==pid:continue
                if str(existing['code'] or '').strip().casefold()==code.casefold():raise ValueError('此專案編號已建立，請確認是否選擇既有專案')
            total_budget=input_amount(v.get('total_budget'),'專案經費總額')
            if total_budget<0:raise ValueError('專案經費總額不可小於 0')
            if pid:
                require_project(db,pid)
                for erow in db.execute("SELECT id,batch_id,voucher_date FROM expenses WHERE project_id=? AND voided=0",(pid,)):
                    batch=db.execute("SELECT submitted_date FROM reimbursement_batches WHERE id=?",(erow['batch_id'],)).fetchone() if erow['batch_id'] else None
                    submitted=parse_date(batch['submitted_date'],'登錄日期') if batch else None
                    try:validate_expense_dates({'start_date':start,'end_date':end},submitted,parse_date(erow['voucher_date'],'憑證日期'),f'既有核銷 #{erow["id"]}')
                    except ValueError as exc:raise ValueError(f'執行期間調整後與既有資料衝突：{exc}')
                for transfer in db.execute("SELECT date,status FROM transfers WHERE project_id=?",(pid,)):
                    if transfer['date']:validate_transfer_date({'start_date':start,'end_date':end},transfer['date'])
                allocated=round_amount(db.execute("SELECT COALESCE(SUM(initial),0) FROM budget_items WHERE project_id=?",(pid,)).fetchone()[0])
                if allocated>total_budget:raise ValueError(f'目前預算明細已編列 {allocated:,} 元，專案總額不可低於已編列金額')
            year=start[:4]
            vals=(name,year,code,v.get('unit',''),start,end,v.get('contact',''),v.get('phone',''),v.get('email',''),total_budget)
            if pid: db.execute("UPDATE projects SET name=?,year=?,code=?,unit=?,start_date=?,end_date=?,contact=?,phone=?,email=?,total_budget=? WHERE id=?",vals+(pid,))
            else:
                cur=db.execute("INSERT INTO projects(name,year,code,unit,start_date,end_date,contact,phone,email,total_budget) VALUES(?,?,?,?,?,?,?,?,?,?)",vals);new_pid=cur.lastrowid
                db.executemany("INSERT OR IGNORE INTO project_options(project_id,kind,value) VALUES(?,?,?)",[(new_pid,'source','補助款'),(new_pid,'source','自籌款'),(new_pid,'category','人事費'),(new_pid,'category','業務費'),(new_pid,'category','資本門')])
            return {'ok':True}
        if op=='project_delete':
            pid=int(a['id']); require_project(db,pid)
            # Remove restrictive references first, then deleting the project
            # cascades the remaining project-owned records in this transaction.
            db.execute("DELETE FROM email_recipients WHERE project_id=?",(pid,))
            db.execute("DELETE FROM expenses WHERE project_id=?",(pid,))
            db.execute("DELETE FROM transfers WHERE project_id=?",(pid,))
            db.execute("DELETE FROM estimate_rules WHERE project_id=?",(pid,))
            db.execute("DELETE FROM budget_items WHERE project_id=?",(pid,))
            db.execute("DELETE FROM project_options WHERE project_id=?",(pid,))
            db.execute("DELETE FROM projects WHERE id=?",(pid,)); return {'ok':True}
        if op=='budget_save':
            v=a['data']; pid=int(v['project_id']); require_project(db,pid)
            if not all(str(v.get(k,'')).strip() for k in ('source','owner','subject','category')): raise ValueError('經費類別、單位、預算科目及經費屬性必填')
            if not str(v.get('initial','')).strip(): raise ValueError('初始預算必填')
            require_project_option(db,pid,'source',v.get('source'),'經費類別'); require_project_option(db,pid,'owner',v.get('owner'),'單位'); require_project_option(db,pid,'subject',v.get('subject'),'預算科目')
            require_project_option(db,pid,'category',v.get('category'),'經費屬性')
            initial=input_amount(v.get('initial'),'初始預算')
            if initial<0: raise ValueError('初始預算不可小於 0')
            before_total=sum(fnum(r[0]) for r in db.execute("SELECT initial FROM budget_items WHERE project_id=?",(pid,)))
            old_initial=0
            oldrow=None
            if v.get('id'):
                oldrow=db.execute("SELECT * FROM budget_items WHERE id=? AND project_id=?",(int(v['id']),pid)).fetchone()
                if oldrow: old_initial=fnum(oldrow['initial'])
                if oldrow and any(oldrow[k]!=v.get(k,'') for k in ('source','owner','subject','category')):
                    usage=db.execute("SELECT (SELECT COUNT(*) FROM expenses WHERE budget_item_id=?)+(SELECT COUNT(*) FROM transfers WHERE from_budget_item_id=? OR to_budget_item_id=?)",(oldrow['id'],oldrow['id'],oldrow['id'])).fetchone()[0]
                    if usage: raise ValueError('此預算項目已有核銷或流用紀錄，不能更改識別欄位；請保留原項目並調整金額')
            source_totals={r['source']:round_amount(r['total']) for r in db.execute("SELECT source,SUM(initial) total FROM budget_items WHERE project_id=? GROUP BY source",(pid,))}
            if oldrow:source_totals[oldrow['source']]=source_totals.get(oldrow['source'],0)-round_amount(oldrow['initial'])
            source_totals[v['source']]=source_totals.get(v['source'],0)+initial
            check_source_caps(db,pid,source_totals)
            candidate_total=before_total-old_initial+initial
            plan_spent=fnum(db.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE project_id=? AND voided=0",(pid,)).fetchone()[0])
            if candidate_total+0.0001<plan_spent: raise ValueError('調整後專案預算低於已用額度（含待核銷），不能儲存')
            if v.get('id'):
                owner_spent=fnum(db.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE project_id=? AND owner=? AND voided=0",(pid, v.get('owner',''))).fetchone()[0])
                owner_budget=sum(x['budget'] for x in totals(db,pid) if x['owner']==v.get('owner'))
                if oldrow and oldrow['owner']==v.get('owner'): owner_budget=owner_budget-old_initial+initial
                elif not oldrow or oldrow['owner']!=v.get('owner'): owner_budget+=initial
                if owner_budget+0.0001<owner_spent: raise ValueError('調整後單位預算低於其已用額度（含待核銷），不能儲存')
                db.execute("UPDATE budget_items SET source=?,owner=?,subject=?,category=?,initial=? WHERE id=?",(v['source'],v['owner'],v['subject'],v.get('category',''),initial,int(v['id'])))
            else:
                existing=db.execute("SELECT 1 FROM budget_items WHERE project_id=? AND source=? AND owner=? AND subject=? AND category=?",(pid,v['source'],v['owner'],v['subject'],v['category'])).fetchone()
                if existing: raise ValueError('此預算項目已存在，請在明細中編輯原項目')
                owner_spent=fnum(db.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE project_id=? AND owner=? AND voided=0",(pid,v['owner'])).fetchone()[0])
                owner_budget=sum(x['budget'] for x in totals(db,pid) if x['owner']==v['owner'])+initial
                if owner_budget+0.0001<owner_spent: raise ValueError('調整後單位預算低於其已用額度（含待核銷），不能儲存')
                db.execute("INSERT INTO budget_items(project_id,source,owner,subject,category,initial) VALUES(?,?,?,?,?,?)",(pid,v['source'],v['owner'],v['subject'],v['category'],initial))
            ensure_project_option(db,pid,'owner',v['owner'])
            ensure_project_option(db,pid,'subject',v['subject'])
            ensure_project_option(db,pid,'category',v.get('category',''))
            validate_ledger_caps(db,pid)
            validate_reserved_funding(db,pid)
            return {'ok':True}
        if op=='deletion_impact':
            pid=int(a.get('project_id') or 0);require_project(db,pid)
            return deletion_impact(db,pid,str(a.get('entity') or ''),a.get('ids') or [])
        if op=='budget_bulk_delete':
            pid=int(a['project_id']);require_project(db,pid);ids=list(dict.fromkeys(int(x) for x in a.get('ids',[])))
            if not ids:raise ValueError('請先勾選預算明細')
            delete_with_impact(db,pid,'budget',ids);return {'ok':True,'count':len(ids)}
        if op=='project_note_save':
            pid=int(a['project_id']);require_project(db,pid);content=str(a.get('content') or '')
            db.execute("INSERT INTO project_notes(project_id,content,updated_at) VALUES(?,?,CURRENT_TIMESTAMP) ON CONFLICT(project_id) DO UPDATE SET content=excluded.content,updated_at=CURRENT_TIMESTAMP",(pid,content))
            return {'ok':True}
        if op=='theme_background_save':
            value=str(a.get('data') or '')
            if value and (not value.startswith('data:image/jpeg;base64,') or len(value)>2_500_000):raise ValueError('主題背景需為 JPEG，且圖片請控制在約 1.8 MB 內')
            db.execute("INSERT INTO app_settings(key,value) VALUES('theme_background',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(value,));return {'ok':True}
        if op=='brandmark_save':
            value=a.get('data') or {'type':'emoji','value':'經'}
            if not isinstance(value,dict) or value.get('type') not in ('emoji','image'):raise ValueError('品牌標記格式不正確')
            if value['type']=='emoji' and (not value.get('value') or len(value['value'])>16):raise ValueError('請選擇有效的 Emoji')
            if value['type']=='image' and (not str(value.get('value','')).startswith('data:image/')):raise ValueError('圖片資料格式不正確')
            db.execute("INSERT INTO app_settings(key,value) VALUES('brandmark',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(value,ensure_ascii=False),));return {'ok':True}
        if op=='theme_choice_save':
            theme=str(a.get('theme') or '')
            if theme not in ('light','dark','custom'):raise ValueError('不支援的主題')
            db.execute("INSERT INTO app_settings(key,value) VALUES('theme_choice',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(theme),));return {'ok':True}
        if op=='custom_theme_save':
            palette=a.get('palette') or {}; keys=('canvas','paper','table','ink','accent')
            if any(not re.fullmatch(r'#[0-9a-fA-F]{6}',str(palette.get(k,''))) for k in keys):raise ValueError('請為頁面、面板、表格、文字與重點色選擇有效色彩')
            db.execute("INSERT INTO app_settings(key,value) VALUES('custom_theme_palette',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps({k:str(palette[k]).upper() for k in keys}),))
            db.execute("INSERT INTO app_settings(key,value) VALUES('theme_choice',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps('custom'),));return {'ok':True}
        if op=='table_alignments_save':
            alignments=a.get('alignments') or {}
            if not isinstance(alignments,dict) or len(json.dumps(alignments,ensure_ascii=False))>200_000:raise ValueError('表格對齊設定格式不正確')
            allowed={'left','center','right','auto'}
            if any(not isinstance(cols,dict) or any(v not in allowed for v in cols.values()) for cols in alignments.values()):raise ValueError('欄位對齊設定不正確')
            db.execute("INSERT INTO app_settings(key,value) VALUES('table_alignments',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(alignments,ensure_ascii=False),));return {'ok':True}
        if op=='batch_save':
            v=a.get('data',{});pid=int(v['project_id']);project=require_project(db,pid);batch_id=int(v['id']) if v.get('id') else None
            if not batch_id: raise ValueError('新增核銷批次請由新增核銷明細流程建立')
            submitted=parse_date(v.get('submitted_date'),'登錄日期')
            if submitted:validate_expense_dates(project,submitted,None)
            if batch_id:
                old=db.execute("SELECT id FROM reimbursement_batches WHERE id=? AND project_id=?",(batch_id,pid)).fetchone()
                if not old:raise ValueError('找不到核銷批次')
                for row in db.execute("SELECT id,voucher_date FROM expenses WHERE batch_id=? AND voided=0",(batch_id,)):
                    validate_expense_dates(project,submitted,parse_date(row['voucher_date'],'憑證日期'),f'核銷項目 #{row["id"]}')
                db.execute("UPDATE reimbursement_batches SET submitted_date=?,description=? WHERE id=?",(submitted,v.get('description','').strip(),batch_id))
            return {'ok':True,'id':batch_id}
        if op=='expense_bulk_delete':
            pid=int(a['project_id']);require_project(db,pid);ids=strict_positive_ids(a.get('ids',[]),'核銷 ID 清單')
            if ids:
                marks=','.join('?' for _ in ids)
                rows=list(db.execute(f'''SELECT e.*,b.description batch_description,COALESCE(b.submitted_date,'') submitted_date
                    FROM expenses e LEFT JOIN reimbursement_batches b ON b.id=e.batch_id
                    WHERE e.project_id=? AND e.id IN ({marks})''',[pid,*ids]))
                if len(rows)!=len(ids):raise ValueError('部分核銷紀錄已不存在，未執行刪除')
                for row in rows:
                    payees=[dict(x) for x in db.execute('SELECT name,amount FROM payees WHERE expense_id=? ORDER BY id',(row['id'],))]
                    db.execute('''INSERT INTO expense_delete_log(project_id,original_expense_id,batch_id,batch_description,submitted_date,voucher_date,source,owner,category,subject,amount,payees_json,note,voided)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(pid,row['id'],row['batch_id'],row['batch_description'] or '',row['submitted_date'] or '',row['voucher_date'] or '',row['source'],row['owner'],row['category'],row['subject'],round_amount(row['amount']),json.dumps(payees,ensure_ascii=False),row['note'] or '',row['voided']))
                db.execute(f"DELETE FROM expenses WHERE project_id=? AND id IN ({marks})",[pid,*ids])
                db.execute("DELETE FROM reimbursement_batches WHERE project_id=? AND NOT EXISTS(SELECT 1 FROM expenses WHERE expenses.batch_id=reimbursement_batches.id)",(pid,))
            return {'ok':True,'count':len(ids)}
        if op=='batch_bulk_delete':
            pid=int(a['project_id']);require_project(db,pid);ids=strict_positive_ids(a.get('ids',[]),'批次 ID 清單')
            if not ids:return {'ok':True,'count':0,'expense_count':0}
            marks=','.join('?' for _ in ids)
            batches=list(db.execute(f'SELECT id FROM reimbursement_batches WHERE project_id=? AND id IN ({marks})',[pid,*ids]))
            if len(batches)!=len(ids):raise ValueError('部分核銷批次已不存在或不屬於目前專案，未執行刪除')
            expected=a.get('expected_expense_ids')
            if not isinstance(expected,dict): raise ValueError('批次內容已變更，請重新整理後再確認')
            actual={str(batch_id):[r['id'] for r in db.execute(f'SELECT id FROM expenses WHERE project_id=? AND batch_id=? ORDER BY id',(pid,batch_id))] for batch_id in ids}
            normalized={str(k):strict_positive_ids(v,'明細 ID 清單') for k,v in expected.items()}
            if normalized!=actual: raise ValueError('批次內容已變更，請重新整理後再確認')
            rows=list(db.execute(f'''SELECT e.*,b.description batch_description,COALESCE(b.submitted_date,'') submitted_date
                FROM expenses e JOIN reimbursement_batches b ON b.id=e.batch_id
                WHERE e.project_id=? AND e.batch_id IN ({marks})''',[pid,*ids]))
            for row in rows:
                payees=[dict(x) for x in db.execute('SELECT name,amount FROM payees WHERE expense_id=? ORDER BY id',(row['id'],))]
                db.execute('''INSERT INTO expense_delete_log(project_id,original_expense_id,batch_id,batch_description,submitted_date,voucher_date,source,owner,category,subject,amount,payees_json,note,voided)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(pid,row['id'],row['batch_id'],row['batch_description'] or '',row['submitted_date'] or '',row['voucher_date'] or '',row['source'],row['owner'],row['category'],row['subject'],round_amount(row['amount']),json.dumps(payees,ensure_ascii=False),row['note'] or '',row['voided']))
            db.execute(f'DELETE FROM expenses WHERE project_id=? AND batch_id IN ({marks})',[pid,*ids])
            db.execute(f'DELETE FROM reimbursement_batches WHERE project_id=? AND id IN ({marks})',[pid,*ids])
            return {'ok':True,'count':len(ids),'expense_count':len(rows)}
        if op=='expense_delete_log':
            pid=int(a['project_id']);require_project(db,pid)
            return {'ok':True,'rows':[{**dict(row),'payees':json.loads(row['payees_json'])} for row in db.execute('SELECT * FROM expense_delete_log WHERE project_id=? ORDER BY deleted_at DESC,id DESC',(pid,))]}
        if op=='expense_bulk_void':
            pid=int(a['project_id']);require_project(db,pid);ids=list(dict.fromkeys(int(x) for x in a.get('ids',[])));voided=1 if a.get('voided',True) else 0
            for eid in ids:
                row=db.execute("SELECT * FROM expenses WHERE id=? AND project_id=?",(eid,pid)).fetchone()
                if not row:raise ValueError('部分核銷紀錄已不存在，未執行變更')
                if not voided and row['voided']:
                    batch=db.execute('SELECT submitted_date FROM reimbursement_batches WHERE id=?',(row['batch_id'],)).fetchone() if row['batch_id'] else None
                    validate_expense_dates(require_project(db,pid),parse_date(batch['submitted_date'],'登錄日期') if batch else None,parse_date(row['voucher_date'],'憑證日期'),f'核銷項目 #{eid}（{row["owner"]}／{row["subject"]}）')
                    item=db.execute("SELECT id FROM budget_items WHERE project_id=? AND source=? AND owner=? AND subject=? AND category=?",(pid,row['source'],row['owner'],row['subject'],row['category'])).fetchone()
                    if not item:raise ValueError('復原後找不到對應預算項目，未執行變更')
                    projrow=require_project(db,pid);plan=min(round_amount(db.execute("SELECT COALESCE(SUM(initial),0) FROM budget_items WHERE project_id=?",(pid,)).fetchone()[0]),round_amount(projrow['total_budget']))
                    plan_used=round_amount(db.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE project_id=? AND voided=0",(pid,)).fetchone()[0])
                    if plan_used+round_amount(row['amount'])>plan:raise ValueError('復原後會超過全專案核銷上限，未執行變更')
                    src_budget=round_amount(db.execute("SELECT COALESCE(SUM(initial),0) FROM budget_items WHERE project_id=? AND source=?",(pid,row['source'])).fetchone()[0]);src_used=round_amount(db.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE project_id=? AND source=? AND voided=0",(pid,row['source'])).fetchone()[0])
                    if src_used+round_amount(row['amount'])>src_budget:raise ValueError('復原後會超過此經費類別預算上限，未執行變更')
                    owner_cap=sum(x['budget'] for x in totals(db,pid) if x['owner']==row['owner']);owner_used=round_amount(db.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE project_id=? AND owner=? AND voided=0",(pid,row['owner'])).fetchone()[0])
                    if owner_used+round_amount(row['amount'])>owner_cap:raise ValueError('復原後會超過單位核銷上限，未執行變更')
                    item_total=next((x for x in totals(db,pid) if x['id']==item['id']),None)
                    if item_total and item_total['reserved_out'] and round_amount(row['amount'])>max(0,item_total['balance']-item_total['reserved_out']):raise ValueError(f'復原核銷 #{eid} 會占用規劃流用圈存額度；此明細最多可核銷 {max(0,item_total["balance"]-item_total["reserved_out"]):,} 元')
                    db.execute("UPDATE expenses SET voided=0 WHERE id=?",(eid,))
            if ids and voided:
                marks=','.join('?' for _ in ids);db.execute(f"UPDATE expenses SET voided=1 WHERE project_id=? AND id IN ({marks})",[pid,*ids])
            validate_ledger_caps(db,pid)
            return {'ok':True,'count':len(ids),'voided':bool(voided)}
        if op=='expense_save':
            v=a['data']; pid=int(v['project_id']); require_project(db,pid); total=input_amount(v.get('amount'),'核銷金額')
            if total<0: raise ValueError('核銷金額不可小於 0')
            if not all(str(v.get(k,'')).strip() for k in ('source','owner','subject','category')): raise ValueError('經費類別、單位、預算科目及經費屬性必填')
            require_project_option(db,pid,'source',v.get('source'),'經費類別'); require_project_option(db,pid,'owner',v.get('owner'),'單位'); require_project_option(db,pid,'subject',v.get('subject'),'預算科目'); require_project_option(db,pid,'category',v.get('category'),'經費屬性')
            voucher=parse_date(v.get('voucher_date',''),'憑證日期')
            rows=[]
            for x in v.get('payees') or []:
                name=str(x.get('name') or '').strip(); raw=x.get('amount')
                if not name and not str(raw if raw is not None else '').strip():continue
                if not name or not str(raw if raw is not None else '').strip():raise ValueError('付款對象與分配金額需同時填寫，或都留白')
                validate_payee_name(name)
                val=input_amount(raw,'付款分配金額')
                if val<0:raise ValueError('付款分配金額不可小於 0')
                rows.append({'name':name,'amount':val})
            if rows and sum(x['amount'] for x in rows)!=total: raise ValueError(f'付款對象分配合計 {sum(x["amount"] for x in rows):,} 元，必須等於核銷金額 {total:,} 元')
            item=db.execute("SELECT id FROM budget_items WHERE project_id=? AND source=? AND owner=? AND subject=? AND category=?",(pid,v['source'],v['owner'],v['subject'],v['category'])).fetchone()
            if not item:
                if not v.get('create_missing_budget'):
                    raise ValueError('此核銷項目尚無相符的預算明細，請重新儲存並確認建立 0 元預算明細。')
                db.execute("INSERT INTO budget_items(project_id,source,owner,subject,category,initial) VALUES(?,?,?,?,?,0)",(pid,v['source'],v['owner'],v['subject'],v['category']))
                item=db.execute("SELECT id FROM budget_items WHERE project_id=? AND source=? AND owner=? AND subject=? AND category=?",(pid,v['source'],v['owner'],v['subject'],v['category'])).fetchone()
            old=None
            if v.get('id'): old=db.execute("SELECT amount,voided,owner,source,category,batch_id,budget_item_id FROM expenses WHERE id=? AND project_id=?",(int(v['id']),pid)).fetchone()
            if v.get('id') and not old:raise ValueError('此核銷紀錄已不存在或不屬於目前專案')
            new_batch=v.get('new_batch')
            if new_batch is not None and (not isinstance(new_batch,dict) or v.get('batch_id') or old):raise ValueError('新批次資料不可與既有批次或核銷項目編輯一起提交')
            batch_id=int(v.get('batch_id') or (old['batch_id'] if old else 0) or 0) or None
            if not old and not batch_id and new_batch is None: raise ValueError('新增核銷明細必須指定批次')
            if new_batch is not None:
                submitted=parse_date(new_batch.get('submitted_date'),'登錄日期')
                db.execute('INSERT INTO reimbursement_batches(project_id,submitted_date,description) VALUES(?,?,?)',(pid,submitted,str(new_batch.get('description') or '').strip()))
                batch_id=db.execute('SELECT last_insert_rowid()').fetchone()[0]
            if batch_id:
                batch=db.execute("SELECT submitted_date FROM reimbursement_batches WHERE id=? AND project_id=?",(batch_id,pid)).fetchone()
                if not batch:raise ValueError('找不到所屬核銷批次；此批次可能已被刪除，請關閉表單後重新建立批次')
                effective_date=parse_date(batch['submitted_date'],'登錄日期')
            else:
                effective_date=None
            validate_expense_dates(require_project(db,pid),effective_date,voucher)
            if not old or not old['voided']:
                current_all=round_amount(db.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE project_id=? AND voided=0",(pid,)).fetchone()[0])-(round_amount(old['amount']) if old else 0)
                project=require_project(db,pid); project_limit=sum(round_amount(x[0]) for x in db.execute("SELECT initial FROM budget_items WHERE project_id=?",(pid,)))
                hard_plan_limit=round_amount(project['total_budget']); project_limit=min(project_limit,hard_plan_limit)
                if current_all+total>project_limit: raise ValueError(f'超過全專案核銷上限；目前最多可核銷 {max(0,project_limit-current_all):,} 元')
                source_budget=round_amount(db.execute("SELECT COALESCE(SUM(initial),0) FROM budget_items WHERE project_id=? AND source=?",(pid,v['source'])).fetchone()[0])
                source_limit=source_budget
                source_spent=round_amount(db.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE project_id=? AND source=? AND voided=0",(pid,v['source'])).fetchone()[0])
                if old and not old['voided'] and old['source']==v['source']:source_spent-=round_amount(old['amount'])
                if source_spent+total>source_limit:raise ValueError(f'{v["source"]}核銷將超過此經費類別的可用額度；目前最多可核銷 {max(0,source_limit-source_spent):,} 元')
                owner_spent=fnum(db.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE project_id=? AND owner=? AND voided=0",(pid,v['owner'])).fetchone()[0])
                if old and not old['voided'] and old['owner']==v['owner']: owner_spent-=fnum(old['amount'])
                owner_limit=sum(x['budget'] for x in totals(db,pid) if x['owner']==v['owner'])
                owner_reserved,_=cross_unit_reservations(db,pid);owner_limit-=owner_reserved.get(v['owner'],0)
                if owner_spent+total>owner_limit: raise ValueError(f'超過此單位核銷上限；目前最多可核銷 {max(0,owner_limit-owner_spent):,.0f} 元')
                combo=(v['owner'],v['source'],v['category']);combo_budget=sum(x['budget'] for x in totals(db,pid) if (x['owner'],x['source'],x['category'])==combo)
                _,combo_reserved=cross_unit_reservations(db,pid);combo_budget-=combo_reserved.get(combo,0)
                combo_spent=round_amount(db.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE project_id=? AND owner=? AND source=? AND category=? AND voided=0",(pid,*combo)).fetchone()[0])
                if old and not old['voided'] and (old['owner'],old['source'],old['category'])==combo:combo_spent-=round_amount(old['amount'])
                if combo_spent+total>combo_budget:
                    reserved=combo_reserved.get(combo,0)
                    if reserved and combo_spent+total<=combo_budget+reserved:raise ValueError(planned_reservation_message(db,pid,combo,combo_budget+reserved-combo_spent,reserved))
                    raise ValueError(f'超過單位「{v["owner"]}」／經費類別「{v["source"]}」／經費屬性「{v["category"]}」核銷上限；目前最多可核銷 {max(0,combo_budget-combo_spent):,} 元')
                item_total=next((x for x in totals(db,pid) if x['id']==item['id']),None)
                if item_total and item_total['reserved_out']:
                    prior=round_amount(old['amount']) if old and not old['voided'] and old['budget_item_id']==item['id'] else 0
                    item_limit=max(0,item_total['balance']-item_total['reserved_out']+prior)
                    if total>item_limit:raise ValueError(f'此預算明細扣除規劃流用後最多可核銷 {item_limit:,} 元；請先調整或取消規劃流用')
            if v.get('id'):
                eid=int(v['id']);db.execute("UPDATE expenses SET batch_id=?,voucher_date=?,owner=?,source=?,subject=?,category=?,budget_item_id=?,amount=?,note=? WHERE id=?",(batch_id,voucher,v['owner'],v['source'],v['subject'],v['category'],item['id'],total,v.get('note',''),eid)); db.execute("DELETE FROM payees WHERE expense_id=?",(eid,))
            else:
                cur=db.execute("INSERT INTO expenses(project_id,batch_id,voucher_date,owner,source,subject,category,budget_item_id,amount,note) VALUES(?,?,?,?,?,?,?,?,?,?)",(pid,batch_id,voucher,v['owner'],v['source'],v['subject'],v['category'],item['id'],total,v.get('note',''))); eid=cur.lastrowid
            if rows:db.executemany("INSERT INTO payees(expense_id,name,amount) VALUES(?,?,?)",[(eid,x['name'],x['amount']) for x in rows])
            if old and old['batch_id'] and old['batch_id']!=batch_id:
                db.execute('DELETE FROM reimbursement_batches WHERE id=? AND project_id=? AND NOT EXISTS(SELECT 1 FROM expenses WHERE batch_id=?)',(old['batch_id'],pid,old['batch_id']))
            ensure_project_option(db,pid,'owner',v['owner'])
            ensure_project_option(db,pid,'subject',v['subject'])
            return {'ok':True,'id':eid,'batch_id':batch_id}
        if op=='transfer_save':
            v=a['data'];pid=int(v['project_id']);project=require_project(db,pid);status=str(v.get('status') or '')
            if status not in ('planned','executed'):raise ValueError('請選擇流用狀態')
            keys=('date','source','from_owner','from_subject','from_category','to_owner','to_subject','to_category')
            for key in keys:v[key]=str(v.get(key) or '').strip()
            raw_amount=str(v.get('amount') if v.get('amount') is not None else '').strip();amount=input_amount(raw_amount,'流用金額') if raw_amount else None
            v['note']=str(v.get('note') or '').strip()
            old=db.execute("SELECT * FROM transfers WHERE id=? AND project_id=?",(int(v['id']),pid)).fetchone() if v.get('id') else None
            if v.get('id') and not old:raise ValueError('此流用紀錄已不存在或不屬於目前專案')
            if old:validate_transfer_unlocked(old)
            before_balances={item['id']:item for item in totals(db,pid)} if old and old['status']=='executed' else {}
            if status=='executed':
                if not all(v[k] for k in keys):raise ValueError('已流用需填妥流用日期、經費類別、流出與流入單位／屬性／科目')
                if amount is None or amount<=0:raise ValueError('已流用金額需大於 0')
                v['date']=validate_transfer_date(project,v['date'])
            else:
                if v['date']:v['date']=validate_transfer_date(project,v['date'])
                if amount is not None and amount<=0:raise ValueError('規劃金額需大於 0')
                if not any(v[k] for k in keys) and amount is None and not v['note']:raise ValueError('請至少填寫一項規劃資訊或備註')
            if v['source']:require_project_option(db,pid,'source',v['source'],'經費類別')
            for key in ('from_owner','to_owner'):
                if v[key]:require_project_option(db,pid,'owner',v[key],'單位')
            for key in ('from_subject','to_subject'):
                if v[key]:require_project_option(db,pid,'subject',v[key],'預算科目')
            for key in ('from_category','to_category'):
                if v[key]:require_project_option(db,pid,'category',v[key],'經費屬性')
            if v['from_category'] and v['from_category']!=v['to_category']:raise ValueError('流出經費屬性已選擇時，流入經費屬性必須相同')
            if v['to_subject'] and not v['to_category']:raise ValueError('選擇流入預算科目前，請先選擇流入經費屬性')
            if v['to_subject'] and v['to_category']:
                mapped=db.execute("SELECT 1 FROM budget_items WHERE project_id=? AND source=? AND subject=? AND category=? LIMIT 1",(pid,v['source'],v['to_subject'],v['to_category'])).fetchone()
                legacy_same=bool(old and old['source']==v['source'] and old['to_subject']==v['to_subject'] and old['to_category']==v['to_category'])
                if not mapped and not legacy_same:raise ValueError('本專案尚無此「經費類別＋經費屬性＋預算科目」配對，請先到「預算明細」建立 0 元預算明細後再新增流用')
            if all(v[k] for k in ('source','from_owner','from_subject','from_category','to_owner','to_subject','to_category')) and v['from_owner']==v['to_owner'] and v['from_subject']==v['to_subject'] and v['from_category']==v['to_category']:raise ValueError('流出與流入不可為同一項目')
            if old:db.execute('SAVEPOINT transfer_edit_check');db.execute('DELETE FROM transfers WHERE id=?',(old['id'],))
            out=totals(db,pid);source_complete=all(v[k] for k in ('source','from_owner','from_subject','from_category'));src=None
            if source_complete:
                src=next((x for x in out if x['source']==v['source'] and x['owner']==v['from_owner'] and x['subject']==v['from_subject'] and x['category']==v['from_category']),None)
                if status=='executed' or (amount is not None and amount>0):
                    if not src:raise ValueError('找不到完全相符的流出預算明細')
                if src and status=='planned' and amount is not None and amount>0 and amount>src['available_balance']:
                    raise ValueError(f"扣除其他規劃流用後，此預算明細最多可再圈存 {max(0,src['available_balance']):,} 元")
            elif status=='executed':raise ValueError('已流用必須填妥流出預算明細')
            target=None
            if status=='executed':
                target=db.execute("SELECT id FROM budget_items WHERE project_id=? AND source=? AND owner=? AND subject=? AND category=?",(pid,v['source'],v['to_owner'],v['to_subject'],v['to_category'])).fetchone()
                if not target:
                    if not a.get('confirm_missing_budget_item'):raise ValueError('流入單位尚無相符的預算明細，請重新儲存並確認建立 0 元預算明細。')
                    db.execute("INSERT INTO budget_items(project_id,source,owner,subject,category,initial) VALUES(?,?,?,?,?,0)",(pid,v['source'],v['to_owner'],v['to_subject'],v['to_category']))
                    target=db.execute("SELECT id FROM budget_items WHERE project_id=? AND source=? AND owner=? AND subject=? AND category=?",(pid,v['source'],v['to_owner'],v['to_subject'],v['to_category'])).fetchone()
                if amount>src['available_balance']:raise ValueError(f"扣除其他規劃流用後，此預算明細最多可再流出 {max(0,src['available_balance']):,} 元")
                if v['from_owner']!=v['to_owner']:
                    owner_budget=sum(x['budget'] for x in out if x['owner']==v['from_owner']);owner_spent=sum(x['spent'] for x in out if x['owner']==v['from_owner']);owner_left=owner_budget-owner_spent
                    if amount>owner_left:raise ValueError(f'流出後會使該單位總經費超支，最多可流出 {max(0,owner_left):,} 元')
            if old:
                columns=list(old.keys());db.execute('INSERT INTO transfers('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',tuple(old))
            from_id=src['id'] if src and source_complete else None;to_id=target['id'] if target and status=='executed' else None
            if v.get('id'):
                db.execute("UPDATE transfers SET date=?,source=?,from_owner=?,from_subject=?,from_category=?,to_owner=?,to_subject=?,to_category=?,status=?,amount=?,note=?,from_budget_item_id=?,to_budget_item_id=? WHERE id=?",(v['date'],v['source'],v['from_owner'],v['from_subject'],v['from_category'],v['to_owner'],v['to_subject'],v['to_category'],status,amount,v['note'],from_id,to_id,int(v['id'])))
            else:
                db.execute("INSERT INTO transfers(project_id,date,source,from_owner,from_subject,from_category,to_owner,to_subject,to_category,status,amount,note,locked,from_budget_item_id,to_budget_item_id) VALUES(?,?,?,?,?,?,?,?,?,?,?, ?,0,?,?)",(pid,v['date'],v['source'],v['from_owner'],v['from_subject'],v['from_category'],v['to_owner'],v['to_subject'],v['to_category'],status,amount,v['note'],from_id,to_id))
            validate_ledger_caps(db,pid)
            validate_reserved_funding(db,pid)
            if before_balances:
                overdraws=[]
                for item in totals(db,pid):
                    before=before_balances.get(item['id'])
                    if before and item['balance']<before['balance'] and item['balance']<0:
                        overdraws.append({'source':item['source'],'owner':item['owner'],'subject':item['subject'],'category':item['category'],'deficit':-item['balance'],'before_balance':before['balance'],'after_balance':item['balance']})
                if overdraws and not a.get('confirm_overdraw'):
                    db.execute('ROLLBACK TO transfer_edit_check');db.execute('RELEASE transfer_edit_check')
                    return {'ok':True,'confirmation_required':True,'overdraws':overdraws}
            if old:db.execute('RELEASE transfer_edit_check')
            for key,kind in (('from_owner','owner'),('to_owner','owner'),('from_subject','subject'),('to_subject','subject')):
                if v[key]:ensure_project_option(db,pid,kind,v[key])
            return {'ok':True}
        if op=='transfer_bulk_execute':
            pid=int(a['project_id']);project=require_project(db,pid);ids=list(dict.fromkeys(int(x) for x in a.get('ids',[])))
            if not ids:raise ValueError('請先勾選流用紀錄')
            marks=','.join('?' for _ in ids);rows=list(db.execute(f"SELECT * FROM transfers WHERE project_id=? AND id IN ({marks})",[pid,*ids]))
            by_id={int(x['id']):x for x in rows};blocked=[];targets={}
            for tid in ids:
                row=by_id.get(tid)
                if not row:blocked.append({'id':tid,'reason':'紀錄不存在'});continue
                reasons=[]
                if row['locked']:reasons.append('已鎖定')
                if row['status']!='planned':reasons.append('不是規劃中')
                if not row['date']:reasons.append('缺流用日期')
                else:
                    try:validate_transfer_date(project,row['date'])
                    except ValueError as e:reasons.append(str(e))
                for key,label in [('source','經費類別'),('from_owner','流出單位'),('from_subject','流出科目'),('from_category','流出屬性'),('to_owner','流入單位'),('to_subject','流入科目'),('to_category','流入屬性')]:
                    if not str(row[key] or '').strip():reasons.append(f'缺{label}')
                if row['amount'] is None or round_amount(row['amount'])<=0:reasons.append('流用金額需大於 0')
                if row['from_category'] and row['to_category'] and row['from_category']!=row['to_category']:reasons.append('流出與流入經費屬性不同')
                if all(row[k] for k in ('from_owner','from_subject','from_category','to_owner','to_subject','to_category')) and (row['from_owner'],row['from_subject'],row['from_category'])==(row['to_owner'],row['to_subject'],row['to_category']):reasons.append('流出與流入為同一項目')
                if reasons:blocked.append({'id':tid,'reason':'、'.join(reasons)})
                else:
                    key=(row['source'],row['to_owner'],row['to_subject'],row['to_category']);target=db.execute("SELECT id FROM budget_items WHERE project_id=? AND source=? AND owner=? AND subject=? AND category=?",(pid,*key)).fetchone()
                    if not target:targets[key]=None
            if blocked:return {'ok':True,'blocked':blocked}
            caps_before=ledger_cap_balances(db,pid)
            db.execute('SAVEPOINT transfer_bulk_execute_check')
            try:
                target_ids={}
                for key in targets:
                    db.execute("INSERT INTO budget_items(project_id,source,owner,subject,category,initial) VALUES(?,?,?,?,?,0)",(pid,*key));target_ids[key]=db.execute("SELECT id FROM budget_items WHERE project_id=? AND source=? AND owner=? AND subject=? AND category=?",(pid,*key)).fetchone()['id']
                trial_blocked=[]
                for row in rows:
                    db.execute('SAVEPOINT transfer_bulk_execute_item')
                    try:
                        source=next((x for x in totals(db,pid) if (x['source'],x['owner'],x['subject'],x['category'])==(row['source'],row['from_owner'],row['from_subject'],row['from_category'])),None)
                        if not source:raise ValueError('找不到流出預算明細')
                        key=(row['source'],row['to_owner'],row['to_subject'],row['to_category']);target=db.execute("SELECT id FROM budget_items WHERE project_id=? AND source=? AND owner=? AND subject=? AND category=?",(pid,*key)).fetchone()
                        db.execute("UPDATE transfers SET status='executed',from_budget_item_id=?,to_budget_item_id=? WHERE id=?",(source['id'],target['id'],row['id']))
                        after=next(x for x in totals(db,pid) if x['id']==source['id'])
                        if after['balance']<0:raise ValueError(f"流出預算明細「{row['from_owner']}／{row['from_subject']}」餘額不足 {abs(after['balance']):,} 元")
                    except ValueError as e:
                        db.execute('ROLLBACK TO transfer_bulk_execute_item')
                        trial_blocked.append({'id':row['id'],'reason':str(e)})
                    finally:db.execute('RELEASE transfer_bulk_execute_item')
                if trial_blocked:
                    db.execute('ROLLBACK TO transfer_bulk_execute_check');db.execute('RELEASE transfer_bulk_execute_check')
                    return {'ok':True,'blocked':trial_blocked}
                try:
                    validate_ledger_caps(db,pid);validate_reserved_funding(db,pid)
                except ValueError as e:
                    caps_after=ledger_cap_balances(db,pid)
                    worsened={key for key,value in caps_after.items() if value<0 and value<caps_before.get(key,0)}
                    involved=[]
                    for row in rows:
                        source_key=('owner',row['from_owner']);target_key=('owner',row['to_owner'])
                        combo_source=('combo',row['from_owner'],row['source'],row['from_category'])
                        combo_target=('combo',row['to_owner'],row['source'],row['to_category'])
                        if (source_key in worsened and source_key!=target_key) or (combo_source in worsened and combo_source!=combo_target):involved.append(row['id'])
                    reason=f'合計超過上限或占用圈存：{e}'
                    db.execute('ROLLBACK TO transfer_bulk_execute_check');db.execute('RELEASE transfer_bulk_execute_check')
                    return {'ok':True,'blocked':[{'id':tid,'reason':reason} for tid in involved] if involved else [{'id':None,'reason':reason}]}
            except Exception:
                db.execute('ROLLBACK TO transfer_bulk_execute_check');db.execute('RELEASE transfer_bulk_execute_check')
                raise
            db.execute('ROLLBACK TO transfer_bulk_execute_check');db.execute('RELEASE transfer_bulk_execute_check')
            if not a.get('confirm_missing_budget_items'):return {'ok':True,'confirmation_required':True,'count':len(ids),'missing_budget_items':[dict(source=k[0],to_owner=k[1],to_subject=k[2],to_category=k[3]) for k in targets]}
            # Re-run the validated conversion in the surrounding transaction.
            target_ids={}
            for key in targets:
                db.execute("INSERT INTO budget_items(project_id,source,owner,subject,category,initial) VALUES(?,?,?,?,?,0)",(pid,*key));target_ids[key]=db.execute("SELECT id FROM budget_items WHERE project_id=? AND source=? AND owner=? AND subject=? AND category=?",(pid,*key)).fetchone()['id']
            for row in rows:
                source=next(x for x in totals(db,pid) if (x['source'],x['owner'],x['subject'],x['category'])==(row['source'],row['from_owner'],row['from_subject'],row['from_category']))
                target=db.execute("SELECT id FROM budget_items WHERE project_id=? AND source=? AND owner=? AND subject=? AND category=?",(pid,row['source'],row['to_owner'],row['to_subject'],row['to_category'])).fetchone()
                db.execute("UPDATE transfers SET status='executed',from_budget_item_id=?,to_budget_item_id=? WHERE id=?",(source['id'],target['id'],row['id']))
            validate_ledger_caps(db,pid);validate_reserved_funding(db,pid)
            return {'ok':True,'count':len(ids)}
        if op=='transfer_bulk_delete':
            pid=int(a['project_id']);require_project(db,pid)
            ids=list(dict.fromkeys(int(x) for x in a.get('ids',[])))
            if ids:
                marks=','.join('?' for _ in ids)
                rows=list(db.execute(f"SELECT * FROM transfers WHERE project_id=? AND id IN ({marks})",[pid,*ids]))
                if {r['id'] for r in rows}!=set(ids):raise ValueError('部分流用紀錄不存在或不屬於此專案，未執行刪除')
                for row in rows:validate_transfer_unlocked(row)
                overdraws=transfer_overdraws(db,pid,ids)
                if any(x.get('reserved_deficit') for x in overdraws):raise ValueError('刪除所選流用會占用其他規劃紀錄已圈存的額度；請先調整或取消規劃')
                # Check project/source/unit caps before asking the user to confirm
                # a budget-item overdraw. SAVEPOINT ensures a normal return from
                # this action cannot accidentally commit the trial deletion.
                db.execute('SAVEPOINT transfer_delete_cap_check')
                try:
                    db.execute(f"DELETE FROM transfers WHERE project_id=? AND id IN ({marks})",[pid,*ids])
                    validate_ledger_caps(db,pid)
                except Exception:
                    db.execute('ROLLBACK TO transfer_delete_cap_check')
                    db.execute('RELEASE transfer_delete_cap_check')
                    raise
                db.execute('ROLLBACK TO transfer_delete_cap_check')
                db.execute('RELEASE transfer_delete_cap_check')
                if overdraws and not a.get('confirm_overdraw'):return {'ok':True,'confirmation_required':True,'overdraws':overdraws}
                db.execute(f"DELETE FROM transfers WHERE project_id=? AND id IN ({marks})",[pid,*ids])
                validate_ledger_caps(db,pid)
                validate_reserved_funding(db,pid)
            return {'ok':True,'count':len(ids)}
        if op in ('transfer_bulk_lock','transfer_bulk_unlock'):
            pid=int(a['project_id']);require_project(db,pid);ids=list(dict.fromkeys(int(x) for x in a.get('ids',[])))
            if not ids:raise ValueError('請先勾選流用紀錄')
            marks=','.join('?' for _ in ids);rows=list(db.execute(f"SELECT id,locked FROM transfers WHERE project_id=? AND id IN ({marks})",[pid,*ids]))
            if {r['id'] for r in rows}!=set(ids):raise ValueError('部分流用紀錄不存在或不屬於此專案')
            locked=op=='transfer_bulk_lock';db.execute(f"UPDATE transfers SET locked=? WHERE project_id=? AND id IN ({marks}) AND locked<>?",[int(locked),pid,*ids,int(locked)])
            return {'ok':True,'count':sum(bool(r['locked'])!=locked for r in rows)}
        if op=='rule_save_many':
            pid=int(a['project_id']); require_project(db,pid); rows=a.get('rules',[])
            choices={(r['source'],r['category'],r['subject']) for r in db.execute("SELECT DISTINCT source,category,subject FROM budget_items WHERE project_id=?",(pid,))}; seen=set()
            if not rows: raise ValueError('請新增至少一筆估算科目')
            for r in rows:
                key=tuple(clean_option_name(r.get(field)) for field in ('source','category','subject'))
                if key not in choices: raise ValueError(f'「{"／".join(key)}」不是本專案既有經費類別、經費屬性與預算科目組合')
                if key in seen: raise ValueError(f'本次輸入有重複科目：{"／".join(key)}')
                if db.execute("SELECT 1 FROM estimate_rules WHERE project_id=? AND source=? AND category=? AND subject=?",(pid,*key)).fetchone(): raise ValueError(f'「{"／".join(key)}」已納入估算')
                seen.add(key)
            db.executemany("INSERT INTO estimate_rules(project_id,source,category,subject) VALUES(?,?,?,?)",[(pid,*key) for key in seen]); return {'ok':True,'count':len(rows)}
        if op=='default_rate_save':
            try:percent=Decimal(str(a.get('rate_percent','')))
            except InvalidOperation:raise ValueError('共用倍率請輸入有效數字') from None
            if not percent.is_finite():raise ValueError('共用倍率請輸入有效數字')
            if percent<0 or percent>100: raise ValueError('共用倍率請輸入 0 至 100 的百分比')
            if percent.as_tuple().exponent < -2:raise ValueError('共用倍率最多只能輸入小數點後 2 位')
            db.execute("INSERT INTO app_settings(key,value) VALUES('default_estimate_rate',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(percent),)); return {'ok':True}
        if op=='rule_bulk_delete':
            ids=list(dict.fromkeys(int(x) for x in a.get('ids',[])))
            if ids:
                marks=','.join('?' for _ in ids);db.execute(f"DELETE FROM estimate_rules WHERE project_id=? AND id IN ({marks})",[int(a['project_id']),*ids])
            return {'ok':True,'count':len(ids)}
        if op=='reference_save':
            v=a['data']; pid=int(v['project_id']); require_project(db,pid); kind=v.get('kind'); value=(v.get('value') or '').strip()
            if kind not in ('owner','subject','category','source') or not value: raise ValueError('請選擇資料類型並輸入選項名稱')
            if v.get('from_value'):
                original=str(v['from_value']).strip()
                existing=db.execute("SELECT id FROM project_options WHERE project_id=? AND kind=? AND value=?",(pid,kind,original)).fetchone()
                if not existing:raise ValueError('原選項已不存在，請重新整理後再編輯')
                v['id']=existing['id']
            if v.get('id'):
                old=db.execute("SELECT * FROM project_options WHERE id=? AND project_id=?",(int(v['id']),pid)).fetchone()
                if not old: raise ValueError('找不到選項')
                if old['kind']!=kind: raise ValueError('不能直接變更選項類型')
                try: db.execute("UPDATE project_options SET value=? WHERE id=?",(value,int(v['id'])))
                except sqlite3.IntegrityError: raise ValueError('此選項名稱已存在')
                if kind=='owner':
                    collision=db.execute("SELECT 1 FROM budget_items a JOIN budget_items b ON a.project_id=b.project_id AND a.source=b.source AND a.subject=b.subject AND a.category=b.category WHERE a.project_id=? AND a.owner=? AND b.owner=? AND a.id<>b.id LIMIT 1",(pid,old['value'],value)).fetchone()
                    if collision: raise ValueError('更名後會造成重複預算項目，請先整理預算資料')
                    db.execute("UPDATE budget_items SET owner=? WHERE project_id=? AND owner=?",(value,pid,old['value'])); db.execute("UPDATE expenses SET owner=? WHERE project_id=? AND owner=?",(value,pid,old['value']))
                    db.execute("UPDATE transfers SET from_owner=? WHERE project_id=? AND from_owner=?",(value,pid,old['value'])); db.execute("UPDATE transfers SET to_owner=? WHERE project_id=? AND to_owner=?",(value,pid,old['value']))
                elif kind=='subject':
                    collision=db.execute("SELECT 1 FROM budget_items a JOIN budget_items b ON a.project_id=b.project_id AND a.source=b.source AND a.owner=b.owner AND a.category=b.category WHERE a.project_id=? AND a.subject=? AND b.subject=? AND a.id<>b.id LIMIT 1",(pid,old['value'],value)).fetchone()
                    if collision: raise ValueError('更名後會造成重複預算項目，請先整理預算資料')
                    db.execute("UPDATE budget_items SET subject=? WHERE project_id=? AND subject=?",(value,pid,old['value'])); db.execute("UPDATE expenses SET subject=? WHERE project_id=? AND subject=?",(value,pid,old['value']))
                    db.execute("UPDATE transfers SET from_subject=? WHERE project_id=? AND from_subject=?",(value,pid,old['value'])); db.execute("UPDATE transfers SET to_subject=? WHERE project_id=? AND to_subject=?",(value,pid,old['value'])); db.execute("UPDATE estimate_rules SET subject=? WHERE project_id=? AND subject=?",(value,pid,old['value']))
                elif kind=='source':
                    for table in ('budget_items','expenses','transfers'):
                        db.execute(f"UPDATE {table} SET source=? WHERE project_id=? AND source=?",(value,pid,old['value']))
                    db.execute("UPDATE estimate_rules SET source=? WHERE project_id=? AND source=?",(value,pid,old['value']))
                else:
                    db.execute("UPDATE budget_items SET category=? WHERE project_id=? AND category=?",(value,pid,old['value']));db.execute("UPDATE expenses SET category=? WHERE project_id=? AND category=?",(value,pid,old['value']));db.execute("UPDATE transfers SET from_category=? WHERE project_id=? AND from_category=?",(value,pid,old['value']));db.execute("UPDATE transfers SET to_category=? WHERE project_id=? AND to_category=?",(value,pid,old['value']))
                    db.execute("UPDATE estimate_rules SET category=? WHERE project_id=? AND category=?",(value,pid,old['value']))
            else:
                try: db.execute("INSERT INTO project_options(project_id,kind,value) VALUES(?,?,?)",(pid,kind,value))
                except sqlite3.IntegrityError: raise ValueError('此選項已存在')
            return {'ok':True}
        if op=='reference_save_many':
            pid=int(a['project_id']); require_project(db,pid); kind=a.get('kind'); rows=a.get('values',[])
            if kind not in ('owner','subject','category','source') or not rows: raise ValueError('請選擇類型並輸入至少一個選項')
            clean=[]; seen=set()
            for value in rows:
                value=str(value or '').strip()
                if not value: continue
                if value in seen: raise ValueError(f'本次新增有重複選項：{value}')
                if db.execute("SELECT 1 FROM project_options WHERE project_id=? AND kind=? AND value=?",(pid,kind,value)).fetchone(): raise ValueError(f'「{value}」已在管理清單中')
                seen.add(value); clean.append((pid,kind,value))
            if not clean: raise ValueError('請輸入至少一個選項名稱')
            db.executemany("INSERT INTO project_options(project_id,kind,value) VALUES(?,?,?)",clean); return {'ok':True,'count':len(clean)}
        if op=='reference_bulk_delete':
            ids=list(dict.fromkeys(int(x) for x in a.get('ids',[])))
            if not ids:raise ValueError('請先勾選選項資料')
            pid=int(a['project_id']);require_project(db,pid);delete_with_impact(db,pid,'option',ids)
            return {'ok':True,'count':len(ids)}
        if op=='import_budget':
            pid=int(a['project_id']); require_project(db,pid); rows=a.get('rows',[]); names=a.get('filenames',[])
            if not rows: raise ValueError('沒有可匯入的預算資料')
            seen=set(); clean=[]; new_options={'source':set(),'owner':set(),'subject':set(),'category':set()}
            for i,r in enumerate(rows,1):
                source=(r.get('source') or '').strip(); owner=(r.get('owner') or '').strip(); subject=(r.get('subject') or '').strip(); category=(r.get('category') or '').strip()
                if not source: raise ValueError(f'第 {i} 列經費類別不可空白')
                if not str(r.get('initial','')).strip(): raise ValueError(f'第 {i} 列初始預算不可空白')
                if not category: raise ValueError(f'第 {i} 列經費屬性不可空白；請修正匯入檔後重新檢查')
                if not owner or not subject: raise ValueError(f'第 {i} 列缺少單位或科目')
                for kind,value in (('source',source),('owner',owner),('subject',subject),('category',category)):
                    if value and not db.execute("SELECT 1 FROM project_options WHERE project_id=? AND kind=? AND value=?",(pid,kind,value)).fetchone(): new_options[kind].add(value)
                initial=input_amount(r.get('initial'),f'第 {i} 列初始預算')
                if initial<0:raise ValueError(f'第 {i} 列初始預算不可小於 0')
                key=(source,owner,subject,category)
                if key in seen: raise ValueError(f'匯入檔案內有重複項目：{source}/{owner}/{subject}/{category}')
                seen.add(key); clean.append((pid,source,owner,subject,category,initial))
            if a.get('mode')=='replace':
                used=db.execute("SELECT (SELECT COUNT(*) FROM expenses WHERE project_id=?)+(SELECT COUNT(*) FROM transfers WHERE project_id=?)",(pid,pid)).fetchone()[0]
                if used: raise ValueError('本專案已有核銷或流用紀錄，不能取代預算；請改用新增／逐筆修改')
                candidate={}
                for x in clean:candidate[x[1]]=candidate.get(x[1],0)+x[5]
                check_source_caps(db,pid,candidate)
                db.execute("DELETE FROM budget_items WHERE project_id=?",(pid,))
            else:
                existing=db.execute("SELECT source,owner,subject,category FROM budget_items WHERE project_id=?",(pid,)).fetchall(); ex={(r['source'],r['owner'],r['subject'],r['category']) for r in existing}
                dup=[f'{x[1]}/{x[2]}/{x[3]}/{x[4]}' for x in clean if (x[1],x[2],x[3],x[4]) in ex]
                if dup: raise ValueError('資料庫已存在相同預算項目：'+', '.join(dup[:8])+'。可選擇「取代本專案預算」重新匯入。')
                candidate={r['source']:round_amount(r['total']) for r in db.execute("SELECT source,SUM(initial) total FROM budget_items WHERE project_id=? GROUP BY source",(pid,))}
                for x in clean:candidate[x[1]]=candidate.get(x[1],0)+x[5]
                check_source_caps(db,pid,candidate)
            if any(new_options.values()) and not a.get('confirm_new_options'):
                raise ValueError('匯入資料含有尚未建立的經費類別、單位、預算科目或經費屬性；請先在預覽畫面確認新增選項')
            for kind,values in new_options.items():
                db.executemany("INSERT INTO project_options(project_id,kind,value) VALUES(?,?,?)",[(pid,kind,value) for value in sorted(values)])
            db.executemany("INSERT INTO budget_items(project_id,source,owner,subject,category,initial) VALUES(?,?,?,?,?,?)",clean)
            db.execute("INSERT INTO import_batches(project_id,filenames,row_count) VALUES(?,?,?)",(pid,json.dumps(names,ensure_ascii=False),len(clean)))
            return {'ok':True,'count':len(clean),'added_options':sum(len(values) for values in new_options.values())}
        if op=='import_expenses':
            pid=int(a['project_id']); project=require_project(db,pid); rows=a.get('rows',[])
            if not rows:raise ValueError('沒有可匯入的核銷資料')
            duplicate_keys=existing_expense_duplicates(db,pid)
            batch_mode=a.get('batch_mode','separate')
            if batch_mode not in ('merge','separate'):raise ValueError('不支援的批次建立方式')
            prepared=[]; created_budget_count=0; all_spent=round_amount(db.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE project_id=? AND voided=0",(pid,)).fetchone()[0])
            plan_cap=round_amount(project['total_budget']); plan_budgets=sum(round_amount(x[0]) for x in db.execute("SELECT initial FROM budget_items WHERE project_id=?",(pid,))); plan_limit=min(plan_cap,plan_budgets)
            owner_spent={r['owner']:round_amount(r['total']) for r in db.execute("SELECT owner,SUM(amount) total FROM expenses WHERE project_id=? AND voided=0 GROUP BY owner",(pid,))}
            owner_limits={}
            owner_reserved,combo_reserved=cross_unit_reservations(db,pid);combo_limits={}
            item_room={}
            item_reserved={}
            for item in totals(db,pid):
                owner_limits[item['owner']]=owner_limits.get(item['owner'],0)+round_amount(item['budget'])
                key=(item['owner'],item['source'],item['category']);combo_limits[key]=combo_limits.get(key,0)+round_amount(item['budget'])
                item_room[item['id']]=round_amount(item['balance'])-round_amount(item['reserved_out'])
                item_reserved[item['id']]=round_amount(item['reserved_out'])
            source_spent={r['source']:round_amount(r['total']) for r in db.execute("SELECT source,SUM(amount) total FROM expenses WHERE project_id=? AND voided=0 GROUP BY source",(pid,))}
            source_limits={r['source']:round_amount(r['total']) for r in db.execute("SELECT source,SUM(initial) total FROM budget_items WHERE project_id=? GROUP BY source",(pid,))}
            for owner,amount in owner_reserved.items():owner_limits[owner]=owner_limits.get(owner,0)-amount
            for key,amount in combo_reserved.items():combo_limits[key]=combo_limits.get(key,0)-amount
            combo_spent={tuple(r[k] for k in ('owner','source','category')):round_amount(r['total']) for r in db.execute("SELECT owner,source,category,SUM(amount) total FROM expenses WHERE project_id=? AND voided=0 GROUP BY owner,source,category",(pid,))}
            for i,r in enumerate(rows,1):
                source=(r.get('source') or '').strip(); owner=(r.get('owner') or '').strip(); subject=(r.get('subject') or '').strip(); category=(r.get('category') or '').strip(); total=input_amount(r.get('amount'),f'第 {i} 列核銷金額')
                if not db.execute("SELECT 1 FROM project_options WHERE project_id=? AND kind='source' AND value=?",(pid,source)).fetchone() or not owner or not subject:raise ValueError(f'第 {i} 列需填妥有效的經費類別、單位及科目')
                require_project_option(db,pid,'owner',owner,f'第 {i} 列單位'); require_project_option(db,pid,'subject',subject,f'第 {i} 列預算科目')
                if total<0:raise ValueError(f'第 {i} 列核銷金額不可小於 0')
                require_project_option(db,pid,'category',category,f'第 {i} 列經費屬性')
                item=db.execute("SELECT id FROM budget_items WHERE project_id=? AND source=? AND owner=? AND subject=? AND category=?",(pid,source,owner,subject,category)).fetchone()
                if not item:
                    if not a.get('confirm_missing_budget_items'):
                        raise ValueError(f'第 {i} 列：找不到經費類別「{source}」、單位「{owner}」、預算科目「{subject}」、經費屬性「{category}」完全相符的預算明細；本批資料未匯入')
                    db.execute("INSERT INTO budget_items(project_id,source,owner,subject,category,initial) VALUES(?,?,?,?,?,0)",(pid,source,owner,subject,category))
                    item=db.execute("SELECT id FROM budget_items WHERE project_id=? AND source=? AND owner=? AND subject=? AND category=?",(pid,source,owner,subject,category)).fetchone()
                    item_room[item['id']]=0
                    created_budget_count+=1
                voucher=parse_date(r.get('voucher_date'),'憑證日期');submitted=parse_date(r.get('submitted_date'),'登錄日期');description=str(r.get('batch_description') or '').strip()
                validate_expense_dates(project,submitted,voucher,f'第 {i} 列')
                payees=[{'name':validate_payee_name(x.get('name')), 'amount':input_amount(x.get('amount'),f'第 {i} 列付款分配金額')} for x in (r.get('payees') or [])]
                if any(x['amount']<0 for x in payees):raise ValueError(f'第 {i} 列付款分配金額不可小於 0')
                if payees and sum(x['amount'] for x in payees)!=total:raise ValueError(f'第 {i} 列付款分配合計必須等於核銷金額')
                duplicate_key=expense_duplicate_key(submitted,voucher,source,owner,category,subject,total,';'.join(x['name'] for x in payees),str(r.get('note') or ''))
                if duplicate_key in duplicate_keys and not a.get('confirm_duplicates'):
                    raise ValueError(f'第 {i} 列疑似重複核銷；請先確認預覽中的重複紀錄，本批資料未匯入')
                duplicate_keys.setdefault(duplicate_key,[]).append({'row':i})
                if all_spent+total>plan_limit:raise ValueError(f'第 {i} 列會超過全專案核銷上限')
                if source_spent.get(source,0)+total>source_limits.get(source,0):raise ValueError(f'第 {i} 列會超過「{source}」類別的預算上限')
                if owner_spent.get(owner,0)+total>owner_limits.get(owner,0):raise ValueError(f'第 {i} 列會超過單位「{owner}」核銷上限')
                combo=(owner,source,category)
                if combo_spent.get(combo,0)+total>combo_limits.get(combo,0):
                    reserved=combo_reserved.get(combo,0)
                    if reserved and combo_spent.get(combo,0)+total<=combo_limits.get(combo,0)+reserved:
                        raise ValueError(f'第 {i} 列：'+planned_reservation_message(db,pid,combo,combo_limits.get(combo,0)+reserved-combo_spent.get(combo,0),reserved))
                    raise ValueError(f'第 {i} 列會超過單位「{owner}」／經費類別「{source}」／經費屬性「{category}」核銷上限')
                if item_room.get(item['id'],0)<0 or total>max(0,item_room.get(item['id'],0)):
                    if item_reserved.get(item['id'],0):raise ValueError(f'第 {i} 列會占用規劃流用圈存額度；此預算明細最多可核銷 {max(0,item_room.get(item["id"],0)):,} 元')
                item_room[item['id']]=item_room.get(item['id'],0)-total
                all_spent+=total;source_spent[source]=source_spent.get(source,0)+total;owner_spent[owner]=owner_spent.get(owner,0)+total
                combo_spent[combo]=combo_spent.get(combo,0)+total
                prepared.append((i,description,submitted,voucher,owner,source,subject,category,item['id'],total,str(r.get('note') or ''),payees))
            batch_ids={}
            for index,description,submitted,voucher,owner,source,subject,category,item_id,total,note,payees in prepared:
                key=('group',description,submitted) if batch_mode=='merge' and description and submitted else ('row',index)
                if key not in batch_ids:
                    cur_batch=db.execute("INSERT INTO reimbursement_batches(project_id,submitted_date,description) VALUES(?,?,?)",(pid,submitted,description));batch_ids[key]=cur_batch.lastrowid
                cur=db.execute("INSERT INTO expenses(project_id,batch_id,voucher_date,owner,source,subject,category,budget_item_id,amount,note) VALUES(?,?,?,?,?,?,?,?,?,?)",(pid,batch_ids[key],voucher,owner,source,subject,category,item_id,total,note))
                if payees:db.executemany("INSERT INTO payees(expense_id,name,amount) VALUES(?,?,?)",[(cur.lastrowid,x['name'],x['amount']) for x in payees])
            return {'ok':True,'count':len(prepared),'batch_count':len(batch_ids),'created_budget_count':created_budget_count}
        if op=='import_transfers':
            pid=int(a['project_id']);require_project(db,pid);rows=a.get('rows',[])
            if not rows:raise ValueError('沒有可匯入的流用紀錄')
            duplicate_keys=existing_transfer_duplicates(db,pid)
            allowed_target_pairs={(x['source'],x['category'],x['subject']) for x in totals(db,pid)}
            created_budget_count=0
            required=('date','source','from_owner','from_subject','from_category','to_owner','to_subject','to_category','amount')
            option_kinds={'source':'source','from_owner':'owner','to_owner':'owner','from_subject':'subject','to_subject':'subject','from_category':'category','to_category':'category'}
            for i,r in enumerate(rows,1):
                r=dict(r)
                if not all(str(r.get(k,'')).strip() for k in required):raise ValueError(f'第 {i} 列有必填欄位空白；本批資料未匯入')
                date=validate_transfer_date(require_project(db,pid),r['date']);amount=input_amount(r['amount'],f'第 {i} 列流用金額')
                if amount<=0:raise ValueError(f'第 {i} 列金額必須大於 0；本批資料未匯入')
                r['date']=date;r['amount']=amount
                duplicate_key=transfer_duplicate_key(r)
                if duplicate_key in duplicate_keys and not a.get('confirm_duplicates'):
                    raise ValueError(f'第 {i} 列疑似重複流用；請先確認預覽中的重複紀錄，本批資料未匯入')
                duplicate_keys.setdefault(duplicate_key,[]).append({'row':i})
                for key,kind in option_kinds.items():require_project_option(db,pid,kind,r[key],f'第 {i} 列選項')
                if r['from_category']!=r['to_category']:raise ValueError(f'第 {i} 列流出與流入經費屬性不同；本批資料未匯入')
                if (r['source'],r['to_category'],r['to_subject']) not in allowed_target_pairs:raise ValueError(f'第 {i} 列在本專案預算明細中找不到相同經費類別、經費屬性及預算科目的配對；請先建立預算明細，本批資料未匯入')
                if r['from_owner']==r['to_owner'] and r['from_subject']==r['to_subject'] and r['from_category']==r['to_category']:raise ValueError(f'第 {i} 列流出與流入不可為同一預算項目；本批資料未匯入')
                out=next((x for x in totals(db,pid) if x['source']==r['source'] and x['owner']==r['from_owner'] and x['subject']==r['from_subject'] and x['category']==r['from_category']),None)
                if not out:raise ValueError(f'第 {i} 列找不到流出預算項目；本批資料未匯入')
                if amount>round_amount(out['available_balance']):raise ValueError(f'第 {i} 列扣除規劃流用後，流出最多可用 {max(0,round_amount(out["available_balance"])):,} 元；本批資料未匯入')
                if r['from_owner']!=r['to_owner']:
                    owner_rows=[x for x in totals(db,pid) if x['owner']==r['from_owner']];owner_left=sum(round_amount(x['budget'])-round_amount(x['spent']) for x in owner_rows)
                    if amount>owner_left:raise ValueError(f'第 {i} 列會超過「{r["from_owner"]}」單位可控經費餘額；本批資料未匯入')
                target=db.execute("SELECT id FROM budget_items WHERE project_id=? AND source=? AND owner=? AND subject=? AND category=?",(pid,r['source'],r['to_owner'],r['to_subject'],r['to_category'])).fetchone()
                if not target:
                    if not a.get('confirm_missing_budget_items'):raise ValueError('流入單位尚無相符的預算明細，請重新匯入並確認建立 0 元預算明細。')
                    db.execute("INSERT INTO budget_items(project_id,source,owner,subject,category,initial) VALUES(?,?,?,?,?,0)",(pid,r['source'],r['to_owner'],r['to_subject'],r['to_category']))
                    created_budget_count+=1
                    target=db.execute("SELECT id FROM budget_items WHERE project_id=? AND source=? AND owner=? AND subject=? AND category=?",(pid,r['source'],r['to_owner'],r['to_subject'],r['to_category'])).fetchone()
                db.execute("INSERT INTO transfers(project_id,date,source,from_owner,from_subject,from_category,to_owner,to_subject,to_category,from_budget_item_id,to_budget_item_id,amount,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(pid,date,r['source'],r['from_owner'],r['from_subject'],r['from_category'],r['to_owner'],r['to_subject'],r['to_category'],out['id'],target['id'],amount,str(r.get('note') or '').strip()))
            validate_ledger_caps(db,pid)
            validate_reserved_funding(db,pid)
            return {'ok':True,'count':len(rows),'created_budget_count':created_budget_count}
    raise ValueError('不支援的操作')


BUDGET_IMPORT_HEADERS={'經費類別':'source','單位':'owner','經費屬性':'category','預算科目':'subject','初始預算':'initial'}
EXPENSE_IMPORT_HEADERS={'批次說明':'batch_description','登錄日期':'submitted_date','憑證日期':'voucher_date','經費類別':'source','單位':'owner','經費屬性':'category','預算科目':'subject','核銷金額':'amount','付款對象':'payee_names','分配金額':'payee_amounts','備註':'note'}
TRANSFER_IMPORT_HEADERS={'流用日期':'date','經費類別':'source','流出單位':'from_owner','流出經費屬性':'from_category','流出預算科目':'from_subject','流入單位':'to_owner','流入經費屬性':'to_category','流入預算科目':'to_subject','流用金額':'amount','備註':'note'}


class ParsedUpload(list):
    def __init__(self, headers=None, rows=None, row_cells=None, row_numbers=None):
        super().__init__(rows or [])
        self.headers=headers or []
        self.row_cells=row_cells or []
        self.row_numbers=row_numbers or list(range(2,2+len(self.row_cells)))


def validate_import_headers(parsed, file_name, mapping):
    headers=[str(value or '').strip() for value in parsed.headers]
    errors=[]
    seen_headers=set()
    for index,header in enumerate(headers):
        if not header:
            for row_index,cells in zip(parsed.row_numbers,parsed.row_cells):
                if index<len(cells) and str(cells[index] or '').strip():
                    errors.append(f'{file_name} 第 {row_index} 列：第 {index+1} 欄缺少欄名')
            continue
        if header in seen_headers:
            errors.append(f'{file_name}：欄名「{header}」重複出現，請只保留一欄')
            continue
        seen_headers.add(header)
        if header not in mapping:
            available='、'.join(mapping.keys())
            errors.append(f'{file_name}：無法識別欄位「{header}」，可用欄名：{available}')
    for row_index,cells in zip(parsed.row_numbers,parsed.row_cells):
        if any(str(value or '').strip() for value in cells[len(headers):]):errors.append(f'{file_name} 第 {row_index} 列：欄位數多於表頭')
    return errors


def parse_upload(file_name, raw):
    ext=Path(file_name).suffix.lower()
    if ext=='.csv':
        try:text=raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            try:text=raw.decode('cp950')
            except UnicodeDecodeError:raise ValueError(f'{file_name}：無法辨識文字編碼，請另存為 UTF-8 或 Big5 CSV') from None
        reader=csv.reader(io.StringIO(text));matrix=[];line_numbers=[]
        for cells in reader:matrix.append(cells);line_numbers.append(reader.line_num)
        if not matrix:return ParsedUpload()
        headers=matrix[0]
        row_cells=matrix[1:]
        rows=[{header:cells[index] for index,header in enumerate(headers) if str(header or '').strip() and index<len(cells)} for cells in row_cells]
        return ParsedUpload(headers,rows,row_cells,line_numbers[1:])
    if ext not in ('.xlsx','.xlsm'): raise ValueError(f'{file_name}：僅支援 .xlsx、.xlsm 或 .csv')
    try: z=zipfile.ZipFile(io.BytesIO(raw))
    except Exception: raise ValueError(f'{file_name}：無法讀取 Excel 檔')
    ns={'a':'http://schemas.openxmlformats.org/spreadsheetml/2006/main','r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
    shared=[]
    if 'xl/sharedStrings.xml' in z.namelist():
        root=ET.fromstring(z.read('xl/sharedStrings.xml'))
        for si in root.findall('a:si',ns):
            shared.append(''.join(t.text or '' for t in [*si.findall('a:t',ns),*(run.find('a:t',ns) for run in si.findall('a:r',ns))] if t is not None))
    wb=ET.fromstring(z.read('xl/workbook.xml')); rel=ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
    relmap={x.attrib['Id']:x.attrib['Target'] for x in rel}
    sheet=wb.find('a:sheets/a:sheet',ns)
    if sheet is None: return ParsedUpload()
    target=relmap[sheet.attrib['{'+ns['r']+'}id']].lstrip('/')
    path=target if target.startswith('xl/') else 'xl/'+target
    root=ET.fromstring(z.read(path)); matrix=[];numeric_cells=set()
    for row in root.findall('.//a:sheetData/a:row',ns):
        cells={}
        for c in row.findall('a:c',ns):
            ref=c.attrib['r']; col=re.match(r'[A-Z]+',ref).group(0); idx=0
            for ch in col: idx=idx*26+ord(ch)-64
            idx-=1; v=c.find('a:v',ns); val=v.text if v is not None else ''
            if c.attrib.get('t')=='s' and val: val=shared[int(val)]
            elif c.attrib.get('t')=='inlineStr': val=''.join(t.text or '' for t in c.findall('.//a:t',ns))
            elif c.attrib.get('t')=='b':val='TRUE' if val=='1' else 'FALSE'
            elif c.attrib.get('t')=='e':val='#EXCEL_ERROR'
            if c.attrib.get('t') in (None,'n') and val and re.fullmatch(r'\d+(?:\.\d+)?',val):numeric_cells.add((int(row.attrib.get('r') or len(matrix)+1),idx))
            cells[idx]=val
        if cells: matrix.append((int(row.attrib.get('r') or len(matrix)+1),cells))
    if not matrix:return []
    header_cells=matrix[0][1];headers=[str(header_cells.get(i,'')).strip() for i in range(max(header_cells)+1)]
    date_headers={'登錄日期','憑證日期','流用日期','submitted_date','voucher_date','date'}
    for number,row in matrix[1:]:
        for i,header in enumerate(headers):
            if header in date_headers and (number,i) in numeric_cells and i in row:
                serial=Decimal(row[i])
                if Decimal(20000)<serial<Decimal(2958466):row[i]=(datetime(1899,12,30)+timedelta(days=int(serial))).date().isoformat()
    row_cells=[[row.get(i,'') for i in range(max(len(headers),max(row)+1))] for _,row in matrix[1:]]
    rows=[{headers[i]:row.get(i,'') for i in range(len(headers)) if headers[i] and i in row} for _,row in matrix[1:]]
    return ParsedUpload(headers,rows,row_cells,[number for number,_ in matrix[1:]])


def is_blank_import_row(row):
    """Ignore rows containing only empty cells or invisible whitespace in uploaded sheets."""
    invisible='\ufeff\u200b\u200c\u200d\u200e\u200f\u2060\u00ad\u034f'
    def has_content(value):
        if isinstance(value,(list,tuple)):
            return any(has_content(item) for item in value)
        if value is None:
            return False
        text=''.join(ch for ch in str(value) if ch not in invisible).strip()
        return bool(text)
    return not any(has_content(value) for value in row.values())


MONEY_EXPORT_HEADERS={'初始預算','流入','流出','流用後預算','核銷金額','分配金額','流用金額','已核銷','待核銷','帳面餘額','可用餘額','專案經費總額','變更前預算','變更後預算','增減','估算母數','逐筆加總保費','整組直接算','差額','逐筆保費'}

def is_money_header(header):
    return str(header).strip() in MONEY_EXPORT_HEADERS


def format_amount_list(value):
    parts=re.split(r'([；;])',str(value or ''))
    return ''.join(f'{int(part.replace(",", "")):,}' if part and part not in ('；',';') and re.fullmatch(r'\s*-?\d[\d,]*\s*',part) else part for part in parts)


def format_display_value(header,value):
    if is_money_header(header) and isinstance(value,(int,float)) and not isinstance(value,bool):
        return f'{round_amount(value):,}'
    if header=='分配金額' and isinstance(value,str):
        return format_amount_list(value)
    return '' if value is None else str(value)


def make_xlsx(headers, rows):
    return make_xlsx_sheets([('經費明細',headers,rows)])


def make_xlsx_sheets(sheets):
    def col(n):
        out=''
        while n:
            n,rem=divmod(n-1,26); out=chr(65+rem)+out
        return out
    used=set();clean=[]
    for name,headers,rows in sheets:
        name=re.sub(r'[\\/*?:\[\]]','_',str(name))[:31] or '工作表'
        base=name;index=2
        while name in used:
            suffix=f'_{index}';name=base[:31-len(suffix)]+suffix;index+=1
        used.add(name);all_rows=[headers]+rows;xml_rows=[];money_columns={i for i,h in enumerate(headers) if is_money_header(h)};state_col=headers.index('狀態') if '狀態' in headers else -1
        for ri,row in enumerate(all_rows,1):
            cells=[];pending_row=ri>1 and state_col>=0 and state_col<len(row) and str(row[state_col])=='待核銷'
            for ci,val in enumerate(row,1):
                ref=f'{col(ci)}{ri}'
                if isinstance(val,(int,float)) and not isinstance(val,bool):
                    style_attr=' s="3"' if pending_row and ci-1 in money_columns else (' s="4"' if pending_row else (' s="3"' if val<0 and ci-1 in money_columns else (' s="4"' if val<0 else (' s="1"' if ci-1 in money_columns else ''))))
                    cells.append(f'<c r="{ref}"{style_attr}><v>{val}</v></c>')
                else:
                    text=format_amount_list(val) if headers[ci-1]=='分配金額' and isinstance(val,str) else str(val if val is not None else '')
                    text=re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]','',text)
                    style_attr=' s="5"' if pending_row and ci-1 in money_columns else (' s="4"' if pending_row else (' s="2"' if ci-1 in money_columns else ''))
                    cells.append(f'<c r="{ref}"{style_attr} t="inlineStr"><is><t>{escape(text)}</t></is></c>')
            xml_rows.append(f'<row r="{ri}">{"".join(cells)}</row>')
        xml='<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+''.join(xml_rows)+'</sheetData></worksheet>'
        clean.append((name,xml))
    overrides=''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,len(clean)+1))+'<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    sheets_xml=''.join(f'<sheet name="{escape(name,{chr(34):"&quot;"})}" sheetId="{i}" r:id="rId{i}"/>' for i,(name,_) in enumerate(clean,1))
    rels=''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,len(clean)+1))+f'<Relationship Id="rId{len(clean)+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    mem=io.BytesIO()
    with zipfile.ZipFile(mem,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml',f'<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>{overrides}</Types>')
        z.writestr('_rels/.rels','''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>''')
        z.writestr('xl/workbook.xml',f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{sheets_xml}</sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels',f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>')
        z.writestr('xl/styles.xml','''<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0;-#,##0"/></numFmts><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FF8B0000"/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="6"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/><xf numFmtId="164" fontId="0" fillId="0" borderId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf><xf numFmtId="0" fontId="0" fillId="0" borderId="0" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf><xf numFmtId="164" fontId="1" fillId="0" borderId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf><xf numFmtId="0" fontId="1" fillId="0" borderId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>''')
        for i,(_,xml) in enumerate(clean,1):z.writestr(f'xl/worksheets/sheet{i}.xml',xml)
    return mem.getvalue()


def transfer_record_sheet(rows, include_source=True):
    headers=['流用日期']+(['經費類別'] if include_source else [])+['流出單位','流出經費屬性','流出預算科目','流入單位','流入經費屬性','流入預算科目','流用金額','備註','狀態']
    ordered=sorted(rows,key=lambda x:(x.get('status')!='executed',x.get('date') or '9999-12-31',x.get('id') or 0))
    values=[]
    for x in ordered:
        row=[(x.get('date') or '').replace('-','/')]
        if include_source:row.append(x.get('source') or '')
        row.extend([x.get('from_owner') or '',x.get('from_category') or '',x.get('from_subject') or '',x.get('to_owner') or '',x.get('to_category') or '',x.get('to_subject') or '',round_amount(x['amount']) if x.get('amount') is not None else '',x.get('note') or '','已流用' if x.get('status')=='executed' else '規劃中'])
        values.append(row)
    return headers,values


def estimate_report(data, project_id, sources=None, categories=None, owners=None):
    basis=int(Decimal(str(data['settings'].get('default_estimate_rate',2.11)))*100)
    chosen={(r['source'],r['category'],r['subject']) for r in data['rules'] if r['project_id']==project_id}
    groups={}
    for item in data['budgets']:
        if item['project_id']!=project_id or (item['source'],item['category'],item['subject']) not in chosen:continue
        if sources is not None and item['source'] not in sources:continue
        if categories is not None and item['category'] not in categories:continue
        if owners is not None and item['owner'] not in owners:continue
        base=max(0,round_amount(item['available_balance']))
        detail={'owner':item['owner'],'subject':item['subject'],'available_balance':round_amount(item['available_balance']),'base':base,'premium':(base*basis+5000)//10000}
        group=groups.setdefault((item['source'],item['category']),{'source':item['source'],'category':item['category'],'details':[]})
        group['details'].append(detail)
    result=[];category_totals={}
    for key,group in sorted(groups.items()):
        group['details'].sort(key=lambda x:(x['owner'],x['subject']))
        group['subjects']=sorted({x['subject'] for x in group['details']})
        group['base']=sum(x['base'] for x in group['details']);group['premium']=sum(x['premium'] for x in group['details'])
        group['direct']=(group['base']*basis+5000)//10000
        units={}
        for detail in group['details']:
            unit=units.setdefault(detail['owner'],{'owner':detail['owner'],'base':0,'premium':0})
            unit['base']+=detail['base'];unit['premium']+=detail['premium']
        group['units']=[units[name] for name in sorted(units)]
        subtotal=category_totals.setdefault(group['source'],{'source':group['source'],'base':0,'premium':0})
        subtotal['base']+=group['base'];subtotal['premium']+=group['premium']
        result.append(group)
    for subtotal in category_totals.values():subtotal['direct']=(subtotal['base']*basis+5000)//10000
    total={'base':sum(x['base'] for x in result),'premium':sum(x['premium'] for x in result)}
    total['direct']=(total['base']*basis+5000)//10000
    return {'rate_percent':str(Decimal(basis)/100),'groups':result,'category_totals':[category_totals[name] for name in sorted(category_totals)],'total':total}


def build_export_sheets(data, project_id, selected_owners, include_overall):
    project=next((x for x in data['projects'] if x['id']==project_id),None)
    if not project:raise ValueError('找不到指定的專案，請重新選擇專案後匯出')
    budgets=[x for x in data['budgets'] if x['project_id']==project_id]
    expenses=[x for x in data['expenses'] if x['project_id']==project_id]
    transfers=[x for x in data['transfers'] if x['project_id']==project_id]
    result=[]
    def add(name,headers,rows):result.append((name,headers,rows))
    def payee_names(x):return '；'.join(z['name'] for z in data['payees'].get(str(x['id']),[]))
    def payee_amounts(x):return '；'.join(str(round_amount(z['amount'])) for z in data['payees'].get(str(x['id']),[]))
    def expense_state(x):return '作廢' if x['voided'] else ('待核銷' if is_pending_expense(x) else '已核銷')
    def expense_sort_key(x):
        state=expense_state(x);order=0 if state=='待核銷' else (2 if state=='作廢' else 1)
        return (order,x.get('submitted_date') or '9999-12-31',x.get('id',0))
    def date_text(value):return (value or '').replace('-','/')
    if include_overall:
        grouped={}
        for x in budgets:
            k=(x['source'],x.get('category','') or '未分類',x['subject']);g=grouped.setdefault(k,{'initial':0,'incoming':0,'outgoing':0,'budget':0,'confirmed':0,'pending':0,'reserved':0})
            for col in ('initial','incoming','outgoing','budget'):g[col]+=round_amount(x.get(col))
            g['reserved']+=round_amount(x.get('reserved_out') or 0)
        for x in expenses:
            if x['voided']:continue
            k=(x['source'],x.get('category','') or '未分類',x['subject']);g=grouped.setdefault(k,{'initial':0,'incoming':0,'outgoing':0,'budget':0,'confirmed':0,'pending':0,'reserved':0})
            g['pending' if is_pending_expense(x) else 'confirmed']+=round_amount(x['amount'])
        overall_rows=[[src,category,subject,g['initial'],g['incoming'],g['outgoing'],g['budget'],g['confirmed'],g['pending'],g['budget']-g['confirmed']-g['pending'],g['budget']-g['confirmed']-g['pending']-g['reserved'],execution_percent(g['confirmed']+g['pending'],g['budget'])] for (src,category,subject),g in sorted(grouped.items())]
        sums={k:sum(g[k] for g in grouped.values()) for k in ('initial','incoming','outgoing','budget','confirmed','pending','reserved')}
        overall_rows.append(['專案合計','','',sums['initial'],sums['incoming'],sums['outgoing'],sums['budget'],sums['confirmed'],sums['pending'],sums['budget']-sums['confirmed']-sums['pending'],sums['budget']-sums['confirmed']-sums['pending']-sums['reserved'],execution_percent(sums['confirmed']+sums['pending'],round_amount(project['total_budget']))])
        add('專案經費總表',['經費類別','經費屬性','預算科目','初始預算','流入','流出','流用後預算','已核銷','待核銷','帳面餘額','可用餘額','執行率（含待核銷）（%）'],overall_rows)
        add('核銷彙總',['狀態','登錄日期','憑證日期','經費類別','單位','經費屬性','預算科目','核銷金額','付款對象','分配金額','備註'],[[expense_state(x),date_text(x.get('submitted_date')),date_text(x.get('voucher_date')),x['source'],x['owner'],x.get('category',''),x['subject'],round_amount(x['amount']),payee_names(x),payee_amounts(x),x.get('note','')] for x in sorted(expenses,key=expense_sort_key)])
        add('流用紀錄',*transfer_record_sheet(transfers))
    for owner in selected_owners:
        owner_b=[x for x in budgets if x['owner']==owner]
        owner_group={}
        for x in owner_b:
            key=(x['source'],x.get('category','') or '未分類',x['subject'])
            g=owner_group.setdefault(key,{'budget':0,'incoming':0,'outgoing':0,'confirmed':0,'pending':0,'reserved':0})
            for col in ('budget','incoming','outgoing'):g[col]+=round_amount(x.get(col))
            g['reserved']+=round_amount(x.get('reserved_out') or 0)
        for x in expenses:
            if x['voided'] or x['owner']!=owner:continue
            key=(x['source'],x.get('category','') or '未分類',x['subject'])
            g=owner_group.setdefault(key,{'budget':0,'incoming':0,'outgoing':0,'confirmed':0,'pending':0,'reserved':0})
            g['pending' if is_pending_expense(x) else 'confirmed']+=round_amount(x['amount'])
        owner_rows=[[source,category,subject,g['budget'],g['incoming'],g['outgoing'],g['confirmed'],g['pending'],g['budget']-g['confirmed']-g['pending'],g['budget']-g['confirmed']-g['pending']-g['reserved'],execution_percent(g['confirmed']+g['pending'],g['budget'])] for (source,category,subject),g in sorted(owner_group.items())]
        owner_sums={k:sum(g[k] for g in owner_group.values()) for k in ('budget','incoming','outgoing','confirmed','pending','reserved')}
        owner_rows.append(['單位合計','','',owner_sums['budget'],owner_sums['incoming'],owner_sums['outgoing'],owner_sums['confirmed'],owner_sums['pending'],owner_sums['budget']-owner_sums['confirmed']-owner_sums['pending'],owner_sums['budget']-owner_sums['confirmed']-owner_sums['pending']-owner_sums['reserved'],execution_percent(owner_sums['confirmed']+owner_sums['pending'],owner_sums['budget'])])
        add(f'{owner[:20]}_經費',['經費類別','經費屬性','預算科目','流用後預算','流入','流出','已核銷','待核銷','帳面餘額','可用餘額','執行率（含待核銷）（%）'],owner_rows)
        owner_e=sorted((x for x in expenses if x['owner']==owner),key=expense_sort_key)
        add(f'{owner[:20]}_核銷',['狀態','登錄日期','憑證日期','經費屬性','預算科目','核銷金額','付款對象','分配金額','備註'],[[expense_state(x),date_text(x.get('submitted_date')),date_text(x.get('voucher_date')),x.get('category',''),x['subject'],round_amount(x['amount']),payee_names(x),payee_amounts(x),x.get('note','')] for x in owner_e])
        owner_t=[x for x in transfers if x['from_owner']==owner or x['to_owner']==owner]
        add(f'{owner[:20]}_流用紀錄',*transfer_record_sheet(owner_t,False))
    if include_overall or selected_owners:
        add('餘額說明',['名詞','說明'],[['帳面餘額','流用後預算扣除已核銷與待核銷後的金額，不考慮規劃中流用。'],['可用餘額','帳面餘額再扣除規劃中流用已保留的金額，代表目前實際還能核銷或流出的金額。']])
    return project,result


def make_pdf_sheets(sheets,orientation='landscape'):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer
    from pypdf import PdfReader, PdfWriter
    font_path=next((Path(p) for p in (r'C:\Windows\Fonts\msjh.ttc',r'C:\Windows\Fonts\msjh.ttf',r'C:\Windows\Fonts\NotoSansTC-Regular.ttf') if Path(p).exists()),None)
    if not font_path:raise ValueError('找不到支援繁體中文的字型，PDF 無法正確輸出')
    bold_font_path=next((Path(p) for p in (r'C:\Windows\Fonts\msjhbd.ttc',r'C:\Windows\Fonts\msjhbd.ttf',r'C:\Windows\Fonts\NotoSansTC-Bold.ttf') if Path(p).exists()),font_path)
    font_name='BudgetCJK'
    if font_name not in pdfmetrics.getRegisteredFontNames():pdfmetrics.registerFont(TTFont(font_name,str(font_path),subfontIndex=0) if font_path.suffix.lower()=='.ttc' else TTFont(font_name,str(font_path)))
    bold_font_name='BudgetCJKBold'
    if bold_font_name not in pdfmetrics.getRegisteredFontNames():pdfmetrics.registerFont(TTFont(bold_font_name,str(bold_font_path),subfontIndex=0) if bold_font_path.suffix.lower()=='.ttc' else TTFont(bold_font_name,str(bold_font_path)))
    pdfmetrics.registerFontFamily(font_name,normal=font_name,bold=bold_font_name,italic=font_name,boldItalic=bold_font_name)
    page_size=landscape(A4) if orientation=='landscape' else A4
    writer=PdfWriter()
    for section_name,headers,rows in sheets:
        mem=io.BytesIO();doc=SimpleDocTemplate(mem,pagesize=page_size,rightMargin=12*mm,leftMargin=12*mm,topMargin=15*mm,bottomMargin=15*mm,title=section_name)
        title_style=ParagraphStyle('BudgetTitle',fontName=font_name,fontSize=16,leading=21,textColor=colors.HexColor('#1F2937'),alignment=TA_LEFT,spaceAfter=8)
        cell_style=ParagraphStyle('BudgetCell',fontName=font_name,fontSize=8,leading=11,wordWrap='CJK')
        head_style=ParagraphStyle('BudgetHead',parent=cell_style,fontSize=8.5,leading=12,textColor=colors.white,alignment=TA_CENTER)
        table_data=[[Paragraph(escape(str(x)),head_style) for x in headers]]
        numeric_headers=MONEY_EXPORT_HEADERS|{'執行率（含待核銷）（%）','真實執行率','預估執行率'}
        for row in rows:
            cells=[]
            for i,value in enumerate(row):
                negative=isinstance(value,(int,float)) and not isinstance(value,bool) and value<0
                rendered=escape(format_display_value(headers[i],value)).replace('\n','<br/>')
                if negative:rendered='<b>'+rendered+'</b>'
                style=ParagraphStyle(f'cell{len(table_data)}_{i}',parent=cell_style,alignment=TA_RIGHT if headers[i] in numeric_headers else TA_LEFT,textColor=colors.HexColor('#8B0000') if negative else colors.black)
                cells.append(Paragraph(rendered,style))
            table_data.append(cells)
        usable=page_size[0]-24*mm
        widths=[usable/max(1,len(headers))]*len(headers)
        table=LongTable(table_data,colWidths=widths,repeatRows=1,hAlign='LEFT',splitByRow=1,splitInRow=1)
        table.setStyle([
            ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#374151')),('GRID',(0,0),(-1,-1),.35,colors.HexColor('#D1D5DB')),
            ('VALIGN',(0,0),(-1,-1),'MIDDLE'),('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5),
            ('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#F4F1EC')])])
        story=[Paragraph(escape(section_name),title_style),table]
        def footer(canvas,document):
            canvas.saveState();canvas.setFont(font_name,8);canvas.setFillColor(colors.HexColor('#4B5563'))
            canvas.drawRightString(page_size[0]-12*mm,7*mm,f'頁碼 {document.page}')
            canvas.restoreState()
        doc.build(story,onFirstPage=footer,onLaterPages=footer);mem.seek(0);writer.append(PdfReader(mem))
    result=io.BytesIO();writer.write(result);return result.getvalue()


def sheets_to_markdown(project, sheets):
    lines=[f"# {project['name']}（{project['code']}）經費匯出",'']
    for name,headers,rows in sheets:
        lines.extend([f'## {name}','', '| '+' | '.join(str(x).replace('|','\\|') for x in headers)+' |','| '+' | '.join(['---']*len(headers))+' |'])
        for row in rows:lines.append('| '+' | '.join(str(x if x is not None else '').replace('|','\\|').replace('\n',' ') for x in row)+' |')
        lines.append('')
    return '\n'.join(lines)


def valid_email(value):
    value=str(value or '').strip()
    return len(value)<=254 and bool(re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+",value))


def email_filename(value):
    """Sanitize an attachment filename without changing its file extension."""
    name=re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(value or '經費累計報告.xlsx')).strip(' .')
    if not name:
        name='經費累計報告.xlsx'
    stem,separator,suffix=name.rpartition('.')
    if separator and stem:
        return stem[:180-len(suffix)-1]+'.'+suffix
    return name[:180]


def email_report_data(project_id,data=None):
    data=data or bootstrap();project=next((p for p in data['projects'] if p['id']==project_id),None)
    if not project:raise ValueError('找不到專案')
    budgets=[x for x in data['budgets'] if x['project_id']==project_id]
    expenses=[x for x in data['expenses'] if x['project_id']==project_id]
    batches={x['id']:x for x in data['batches'] if x['project_id']==project_id}
    unit_names=sorted({x['owner'] for x in budgets})
    unit_reports=[]
    for unit in unit_names:
        ub=[x for x in budgets if x['owner']==unit]
        ue=[x for x in expenses if x['owner']==unit]
        grouped={}
        for x in ub:
            k=(x['source'],x['subject']);g=grouped.setdefault(k,{'budget':0,'confirmed':0,'pending':0,'balance':0,'available':0})
            g['budget']+=round_amount(x['budget']);g['balance']+=round_amount(x['balance']);g['available']+=round_amount(x['available_balance'])
        for x in ue:
            if x['voided']:continue
            k=(x['source'],x['subject']);g=grouped.setdefault(k,{'budget':0,'confirmed':0,'pending':0,'balance':0,'available':0})
            if not is_pending_expense(x):g['confirmed']+=round_amount(x['amount'])
            else:g['pending']+=round_amount(x['amount'])
        summary=[]
        for (source,subject),g in sorted(grouped.items()):
            g['balance']=g['budget']-g['confirmed']-g['pending']
            g['available']=g['balance']-sum(round_amount(x.get('reserved_out') or 0) for x in ub if (x['source'],x['subject'])==(source,subject))
            summary.append({'source':source,'subject':subject,**g,'rate':execution_ratio(g['confirmed'],g['budget'])})
        confirmed=sum(x['confirmed'] for x in summary);pending=sum(x['pending'] for x in summary);budget=sum(x['budget'] for x in summary)
        details=[]
        for x in ue:
            batch=batches.get(x.get('batch_id')) or {};state='作廢' if x['voided'] else ('待核銷' if is_pending_expense(x) else '已核銷')
            details.append({'id':x['id'],'submitted_date':x.get('submitted_date') or '', 'voucher_date':x.get('voucher_date') or '', 'state':state,'source':x['source'],'subject':x['subject'],'amount':round_amount(x['amount']),'batch_description':batch.get('description') or '', 'note':x.get('note') or '', 'payees':'；'.join(z['name'] for z in data['payees'].get(str(x['id']),[])), 'payee_amounts':'；'.join(str(round_amount(z['amount'])) for z in data['payees'].get(str(x['id']),[]))})
        details.sort(key=lambda x:(0 if x['state']=='待核銷' else (2 if x['state']=='作廢' else 1),x['submitted_date'] or '9999-12-31',x['id']))
        unit_reserved=sum(round_amount(x.get('reserved_out') or 0) for x in ub)
        unit_reports.append({'name':unit,'budget':budget,'confirmed':confirmed,'pending':pending,'book_balance':budget-confirmed-pending,'available':budget-confirmed-pending-unit_reserved,'rate':execution_ratio(confirmed,budget),'summary':summary,'details':details})
    # Match the project total used by the shared export summary: sum the
    # allocated, flow-adjusted budget rows rather than the project ceiling.
    total_budget=sum(round_amount(x.get('budget')) for x in budgets)
    project_budget=round_amount(project.get('total_budget') or 0)
    confirmed=sum(x['confirmed'] for x in unit_reports);pending=sum(x['pending'] for x in unit_reports)
    return {'project':project,'report_as_of':date.today().isoformat(),'total_budget':total_budget,'project_budget':project_budget,'confirmed':confirmed,'pending':pending,'book_balance':total_budget-confirmed-pending,'available':sum(x['available'] for x in unit_reports),'rate':execution_ratio(confirmed,project_budget),'units':unit_reports}


def load_monthly_template():
    template_path=TEMPLATE_DIR/'monthly_email.html'
    if not template_path.exists():raise ValueError('找不到信件範本 src/templates/monthly_email.html')
    return template_path.read_text(encoding='utf-8')


def email_summary_html(report, included_units=None, show_unit_rows=False, unit_only=None):
    p=report['project'];unit_rows=''
    balance_note='<p class="balance-note">帳面餘額：流用後預算扣除已核銷與待核銷，不考慮規劃中流用。可用餘額：帳面餘額再扣除規劃中流用已保留的金額，代表目前實際還能核銷或流出的金額。</p>'
    if unit_only:
        u=next(x for x in report['units'] if x['name']==unit_only)
        unit_rows=f'<table><tbody><tr><th>單位</th><td>{escape(u["name"])}</td></tr><tr><th>流用後預算</th><td>{u["budget"]:,}</td></tr><tr><th>已核銷</th><td>{u["confirmed"]:,}</td></tr><tr><th>待核銷</th><td>{u["pending"]:,}</td></tr><tr><th>預估核銷合計</th><td>{u["confirmed"]+u["pending"]:,}</td></tr><tr><th>帳面餘額</th><td>{u["book_balance"]:,}</td></tr><tr><th>可用餘額</th><td>{u["available"]:,}</td></tr></tbody></table>'
        return f'<p>{escape(p.get("name", ""))}（{escape(p.get("code", ""))}）</p><p>資料範圍：截至寄送時系統中的全部紀錄；以下只包含你的單位資料。附件明細先列待核銷紀錄，其餘依登錄日期由舊至新排列。</p>{unit_rows}{balance_note}'
    if show_unit_rows:
        chosen=report['units'] if included_units is None else [u for u in report['units'] if u['name'] in included_units]
        unit_rows='<h3>單位摘要</h3><table><thead><tr><th>單位</th><th>流用後預算</th><th>已核銷</th><th>待核銷</th><th>帳面餘額</th><th>可用餘額</th></tr></thead><tbody>'+''.join(f'<tr><td>{escape(u["name"])}</td><td>{u["budget"]:,}</td><td>{u["confirmed"]:,}</td><td>{u["pending"]:,}</td><td>{u["book_balance"]:,}</td><td>{u["available"]:,}</td></tr>' for u in chosen)+'</tbody></table>'
    overall=f'<table><tbody><tr><th>專案經費總額</th><td>{report["project_budget"]:,}</td></tr><tr><th>專案預算合計</th><td>{report["total_budget"]:,}</td></tr><tr><th>已核銷</th><td>{report["confirmed"]:,}</td></tr><tr><th>待核銷</th><td>{report["pending"]:,}</td></tr><tr><th>預估核銷合計</th><td>{report["confirmed"]+report["pending"]:,}</td></tr><tr><th>帳面餘額</th><td>{report["book_balance"]:,}</td></tr><tr><th>可用餘額</th><td>{report["available"]:,}</td></tr><tr><th>真實執行率</th><td>{execution_label(report["rate"])}</td></tr></tbody></table>'
    return f'<p>{escape(p.get("name", ""))}（{escape(p.get("code", ""))}）</p><p>資料範圍：截至寄送時系統中的全部紀錄；附件明細先列待核銷紀錄，其餘依登錄日期由舊至新排列。金額為新臺幣整數元。</p>{overall}{unit_rows}{balance_note}'


def render_monthly_email(report,title,body_content):
    template=load_monthly_template()
    replacements={'{{EMAIL_TITLE}}':escape(title),'{{PROJECT_NAME}}':escape(report['project'].get('name','')),'{{PROJECT_CODE}}':escape(report['project'].get('code','')),'{{REPORT_DATE}}':escape(report['report_as_of'].replace('-','/')),'{{SUMMARY_CONTENT}}':body_content}
    for key,value in replacements.items():template=template.replace(key,value)
    return template


def email_delivery_plan(db,report,project_id,recipient_ids=None,manager_scopes=None):
    recs=[dict(r) for r in db.execute("SELECT r.*,u.name AS unit_name FROM email_recipients r LEFT JOIN units u ON u.id=r.unit_id WHERE r.project_id=? AND r.enabled=1 ORDER BY r.role,r.name",(project_id,))]
    if recipient_ids is not None:
        selected={int(x) for x in recipient_ids}
        known={r['id'] for r in recs}
        if selected-known:raise ValueError('收件人選項已變更，請重新產生預覽')
        recs=[r for r in recs if r['id'] in selected]
    unit_recs=[r for r in recs if r['role']=='unit']
    messages=[]
    for r in unit_recs:
        unit=next((u for u in report['units'] if u['name']==r['unit_name']),None)
        if unit is None:raise ValueError(f'收件人「{r["name"]}」所屬單位尚無預算資料，無法產生附件')
        body=email_summary_html(report,[unit['name']],False,unit['name'])
        title=f'{report["project"].get("name", "專案")}｜{unit["name"]} 經費累計報告'
        messages.append({'recipient_id':r['id'],'recipient_type':'unit','recipient_name':r['name'],'email':r['email'],'report_scope':f'單位：{unit["name"]}','subject':title,'html':render_monthly_email(report,title,body),'export_owners':[unit['name']],'include_overall':False})
    if manager_scopes is None:manager_scopes={}
    if not isinstance(manager_scopes,dict):raise ValueError('主管資料範圍格式錯誤，請重新選擇')
    manager_recs=[x for x in recs if x['role']=='manager']
    for r in manager_recs:
        if not r.get('unit_id') or not r.get('unit_name'):
            raise ValueError(f'主管收件人「{r["name"]}」尚未設定所屬單位，請先編輯收件人')
        scope_choice=manager_scopes.get(str(r['id']))
        if not isinstance(scope_choice,dict):raise ValueError(f'請選擇主管「{r["name"]}」本次要收到的資料範圍')
        mode=scope_choice.get('mode')
        if mode not in ('summary','self','selected','all'):raise ValueError(f'請選擇主管「{r["name"]}」本次要收到的資料範圍')
        if mode=='summary':chosen=[]
        elif mode=='self':chosen=[r['unit_name']]
        elif mode=='all':chosen=[u['name'] for u in report['units']]
        else:
            try:unit_ids=sorted({int(x) for x in scope_choice.get('unit_ids',[])})
            except (TypeError,ValueError):raise ValueError(f'主管「{r["name"]}」的指定單位資料格式錯誤')
            if not unit_ids:raise ValueError(f'請至少選擇一個要寄送給主管「{r["name"]}」的單位')
            placeholders=','.join('?' for _ in unit_ids)
            linked=[dict(x) for x in db.execute(f'SELECT id,name FROM units WHERE project_id=? AND id IN ({placeholders}) ORDER BY name',(project_id,*unit_ids))]
            if {x['id'] for x in linked}!=set(unit_ids):raise ValueError(f'主管「{r["name"]}」指定的單位必須屬於本專案')
            chosen=[x['name'] for x in linked]
        if mode in ('self','selected') and not set(chosen).issubset({u['name'] for u in report['units']}):
            raise ValueError(f'主管「{r["name"]}」所屬或選取的單位尚無預算資料，無法產生明細附件')
        include_overall=mode in ('summary','selected','all')
        body=email_summary_html(report,unit_only=r['unit_name']) if mode=='self' else email_summary_html(report,chosen,True if chosen else False)
        title=f'{report["project"].get("name", "專案")}｜專案經費摘要'
        scope='經費摘要' if mode=='summary' else ('本單位：'+r['unit_name'] if mode=='self' else ('經費摘要＋全部單位' if mode=='all' else '經費摘要＋指定單位：'+ '、'.join(chosen)))
        messages.append({'recipient_id':r['id'],'recipient_type':'manager','recipient_name':r['name'],'email':r['email'],'report_scope':scope,'subject':title,'html':render_monthly_email(report,title,body),'export_owners':chosen,'include_overall':include_overall})
    seen={};duplicates=[]
    for m in messages:
        address=m['email'].casefold()
        if address in seen:duplicates.append({'email':m['email'],'recipients':[seen[address],m['recipient_name']]})
        else:seen[address]=m['recipient_name']
    return messages,duplicates


def email_smtp_config():
    with connect() as db:
        row=db.execute('SELECT * FROM email_smtp_settings WHERE id=1').fetchone()
        if not row:raise ValueError('請先在「Email 經費報告」儲存寄件設定')
        config=dict(row)
    if not config['host'] or not valid_email(config['sender_email']):raise ValueError('寄件設定未完成')
    identity=config['username'] or config['sender_email']
    config['password']=smtp_password(identity) if identity else ''
    return config


def send_email(config,to_address,subject,html_body,attachments=None):
    subject=str(subject).replace('\r',' ').replace('\n',' ').strip()
    msg=EmailMessage();msg['Subject']=subject;msg['From']=formataddr((config.get('sender_name') or '',config['sender_email']));msg['To']=to_address
    msg.set_content('此信件為 HTML 格式，請使用支援 HTML 的郵件軟體檢視。')
    msg.add_alternative(html_body,subtype='html')
    for filename,content,maintype,subtype in (attachments or []):
        msg.add_attachment(content,maintype=maintype,subtype=subtype,filename=filename)
    security=config['security'];host=config['host'];port=int(config['port']);timeout=30
    phase='建立 SMTP 連線';server=None
    try:
        if security=='ssl':server=smtplib.SMTP_SSL(host,port,timeout=timeout,context=ssl.create_default_context())
        else:server=smtplib.SMTP(host,port,timeout=timeout)
        phase='讀取 SMTP 伺服器問候'
        server.ehlo()
        if security=='starttls':
            phase='切換 STARTTLS 加密'
            server.starttls(context=ssl.create_default_context())
            phase='加密後重新問候伺服器'
            server.ehlo()
        if config.get('username'):
            phase='SMTP 帳號驗證'
            server.login(config['username'],config['password'])
        phase='提交郵件'
        server.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        response=e.smtp_error.decode('utf-8','replace') if isinstance(e.smtp_error,bytes) else str(e.smtp_error)
        raise ValueError(f'SMTP 帳號驗證遭拒（回應碼 {e.smtp_code}）：{response}。請確認帳號、密碼類型，以及郵件服務是否允許 SMTP 登入。') from e
    except smtplib.SMTPServerDisconnected as e:
        detail=str(e).strip() or '伺服器未提供其他說明'
        raise ValueError(f'{phase}階段連線中斷：{detail}。請核對伺服器、連接埠與安全方式。') from e
    except smtplib.SMTPResponseException as e:
        response=e.smtp_error.decode('utf-8','replace') if isinstance(e.smtp_error,bytes) else str(e.smtp_error)
        raise ValueError(f'{phase}失敗（SMTP 回應碼 {e.smtp_code}）：{response}') from e
    except ssl.SSLError as e:
        raise ValueError(f'{phase}失敗（TLS/SSL）：{e}') from e
    except (TimeoutError, OSError) as e:
        raise ValueError(f'{phase}失敗（網路連線）：{e}') from e
    finally:
        if server is not None:
            try:server.quit()
            except Exception:pass


def email_report_attachments(project,sheets,formats):
    code=project.get('code') or '專案';result=[]
    for fmt in formats:
        if fmt=='xlsx':
            filename=f'{code}_經費報表.xlsx';content=make_xlsx_sheets(sheets);maintype='application';subtype='vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        elif fmt=='md':
            filename=f'{code}_經費報表.md';content=('\ufeff'+sheets_to_markdown(project,sheets)).encode('utf-8');maintype='text';subtype='markdown'
        elif fmt=='pdf':
            filename=f'{code}_經費報表.pdf';content=make_pdf_sheets(sheets,'landscape');maintype='application';subtype='pdf'
        else:raise ValueError('不支援的報表格式')
        result.append((email_filename(filename),content,maintype,subtype))
    return result


def build_email_payloads(project_id,recipient_ids=None,manager_scopes=None,formats=None):
    supported={'xlsx','md','pdf'}
    formats=list(dict.fromkeys(formats or ['xlsx']))
    if not formats or any(x not in supported for x in formats):raise ValueError('請選擇至少一種有效的附件格式')
    data=bootstrap();report=email_report_data(project_id,data)
    with connect() as db:messages,duplicates=email_delivery_plan(db,report,project_id,recipient_ids,manager_scopes)
    for m in messages:
        _,m['sheets']=build_export_sheets(data,project_id,m['export_owners'],m['include_overall'])
        m['attachments']=email_report_attachments(report['project'],m['sheets'],formats)
        m['attachment_names']=[x[0] for x in m['attachments']]
        m['attachment_formats']=formats
    return report,messages,duplicates


def run_email_delivery_job(batch_id,messages,config):
    try:
        for index,m in enumerate(messages):
            with EMAIL_JOBS_LOCK:
                job=EMAIL_JOBS.get(str(batch_id))
                if job:
                    job['current_index']=index
                    job['current_name']=m['recipient_name']
                    job['items'][index]['status']='sending'
            try:
                send_email(config,m['email'],m['subject'],m['html'],m['attachments'])
                status='sent';error=''
            except Exception as e:
                status='failed';error=str(e)[:1000]
            with connect() as db:
                db.execute("INSERT INTO email_delivery_logs(batch_id,recipient_id,recipient_type,recipient_name,recipient_email,report_scope,status,error) VALUES(?,?,?,?,?,?,?,?)",(batch_id,m.get('recipient_id'),m['recipient_type'],m['recipient_name'],m['email'],m['report_scope'],status,error))
            with EMAIL_JOBS_LOCK:
                job=EMAIL_JOBS.get(str(batch_id))
                if job:
                    job['items'][index].update({'status':status,'error':error})
                    job['completed']+=1
                    job['sent']+=int(status=='sent')
                    job['failed']+=int(status=='failed')
                    job['current_name']=''
        sent=sum(x['status']=='sent' for x in EMAIL_JOBS.get(str(batch_id),{}).get('items',[]))
        failed=len(messages)-sent
        run_status='sent' if not failed else ('partial' if sent else 'failed')
        with connect() as db:db.execute('UPDATE email_send_batches SET status=? WHERE id=?',(run_status,batch_id))
        with EMAIL_JOBS_LOCK:
            job=EMAIL_JOBS.get(str(batch_id))
            if job:job.update({'status':'complete','current_name':''})
    except Exception as e:
        with connect() as db:db.execute("UPDATE email_send_batches SET status='failed' WHERE id=?",(batch_id,))
        with EMAIL_JOBS_LOCK:
            job=EMAIL_JOBS.get(str(batch_id))
            if job:job.update({'status':'failed','fatal_error':display_error(e)[:1000],'current_name':''})


class Handler(BaseHTTPRequestHandler):
    def log_message(self,fmt,*args): return
    def send(self,status,body,typ='application/json; charset=utf-8',extra=None):
        b=body.encode('utf-8') if isinstance(body,str) else body
        self.send_response(status); self.send_header('Content-Type',typ); self.send_header('Content-Length',str(len(b))); self.send_header('Cache-Control','no-store')
        for key,value in (extra or {}).items(): self.send_header(key,value)
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.headers.get('Host','').lower() not in {f'{HOST}:{PORT}'.lower(),f'localhost:{PORT}'.lower()}:
            self.send(403,json.dumps({'error':'請從本機系統頁面連線'},ensure_ascii=False));return
        path=urlparse(self.path).path
        if path=='/api/health':
            self.send(200,json.dumps({'ok':True},ensure_ascii=False));return
        elif path=='/api/email/progress':
            from urllib.parse import parse_qs
            batch_id=parse_qs(urlparse(self.path).query).get('batch_id',[''])[0]
            with EMAIL_JOBS_LOCK:job=json.loads(json.dumps(EMAIL_JOBS.get(str(batch_id)))) if batch_id and str(batch_id) in EMAIL_JOBS else None
            if not job:self.send(404,json.dumps({'error':'找不到此寄送工作，請確認系統仍在執行'},ensure_ascii=False));return
            self.send(200,json.dumps(job,ensure_ascii=False));return
        elif path=='/api/data':
            try:self.send(200,json.dumps(bootstrap(),ensure_ascii=False))
            except Exception as e:self.send(500,json.dumps({'error':display_error(e)},ensure_ascii=False))
        elif path=='/api/template.csv':
            out=io.StringIO(); w=csv.writer(out); w.writerow(['經費類別','單位','經費屬性','預算科目','初始預算']); w.writerow(['補助款','王小明','業務費','講座鐘點費','10000']); self.send(200,'\ufeff'+out.getvalue(),'text/csv; charset=utf-8')
        elif path=='/api/expense-template.csv':
            out=io.StringIO(); w=csv.writer(out); w.writerow(['登錄日期','憑證日期','經費類別','單位','經費屬性','預算科目','核銷金額','批次說明','付款對象','分配金額','備註']); w.writerow(['2026/02/28','2026/02/27','補助款','王小明','業務費','講座鐘點費','2000','A活動第一批','講師甲；講師乙','1200；800','A活動鐘點費']); w.writerow(['2026-2-28','20260227','補助款','王小明','人事費','補充保費','42','A活動第一批','','','A活動補充保費']); w.writerow(['','20260303','補助款','王小明','業務費','講座鐘點費','2000','','','','無批次資訊範例']); self.send(200,'\ufeff'+out.getvalue(),'text/csv; charset=utf-8')
        elif path=='/api/transfer-template.csv':
            out=io.StringIO();w=csv.writer(out);w.writerow(['流用日期','經費類別','流出單位','流出經費屬性','流出預算科目','流入單位','流入經費屬性','流入預算科目','流用金額','備註']);w.writerow(['2026/03/10','補助款','王小明','業務費','講座鐘點費','李小華','業務費','講座鐘點費','1000','yyyy/mm/dd 範例']);w.writerow(['2026-3-11','補助款','王小明','業務費','講座鐘點費','李小華','業務費','講座鐘點費','1000','yyyy-m-d 範例']);w.writerow(['20260312','補助款','王小明','業務費','講座鐘點費','李小華','業務費','講座鐘點費','1000','yyyymmdd 範例']);self.send(200,'\ufeff'+out.getvalue(),'text/csv; charset=utf-8')
        elif path=='/api/backup':
            self.send(200,DB_PATH.read_bytes(),'application/vnd.sqlite3')
        elif path=='/api/estimate-export':
            from urllib.parse import parse_qs
            q=parse_qs(urlparse(self.path).query)
            try:
                pid=int(q.get('project_id',['0'])[0]);data=bootstrap()
                if not any(p['id']==pid for p in data['projects']):raise ValueError('找不到專案')
                report=estimate_report(data,pid,set(q['source']) if 'source' in q else None,set(q['category']) if 'category' in q else None,set(q['owner']) if 'owner' in q else None)
                headers=['經費類別','經費屬性','納入科目','估算母數','逐筆加總保費','整組直接算','差額']
                rows=[]
                for group in report['groups']:
                    rows.append([group['source'],group['category'],'、'.join(group['subjects']),group['base'],group['premium'],group['direct'],group['premium']-group['direct']])
                    subtotal=next(x for x in report['category_totals'] if x['source']==group['source'])
                    if group is next((x for x in reversed(report['groups']) if x['source']==group['source']),None):
                        rows.append([group['source'],'類別小計','',subtotal['base'],subtotal['premium'],subtotal['direct'],subtotal['premium']-subtotal['direct']])
                total=report['total'];rows.append(['總合計','','',total['base'],total['premium'],total['direct'],total['premium']-total['direct']])
                detail_rows=[]
                for group in report['groups']:
                    current_owner=None;base=0;premium=0
                    for detail in group['details']:
                        if current_owner is not None and detail['owner']!=current_owner:
                            detail_rows.append([group['source'],group['category'],current_owner,'單位小計','',base,premium]);base=0;premium=0
                        current_owner=detail['owner'];base+=detail['base'];premium+=detail['premium']
                        detail_rows.append([group['source'],group['category'],detail['owner'],detail['subject'],detail['available_balance'],detail['base'],detail['premium']])
                    if current_owner is not None:detail_rows.append([group['source'],group['category'],current_owner,'單位小計','',base,premium])
                sheets=[('分組總表',headers,rows),('展開明細',['經費類別','經費屬性','單位','預算科目','可用餘額','估算母數','逐筆保費'],detail_rows),('估算說明',['項目','說明'],[['估算母數','每筆預算明細的可用餘額，負數以 0 計，不與其他明細互抵。'],['逐筆加總保費','每筆明細乘共用倍率後四捨五入，再由下往上加總。'],['整組直接算','母數合計乘共用倍率後四捨五入；兩種數字供使用者自行判斷。'],['差額','逐筆加總保費減整組直接算；差異來自逐筆四捨五入。']])]
                self.send(200,make_xlsx_sheets(sheets),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            except Exception as e:self.send(400,json.dumps({'error':display_error(e)},ensure_ascii=False))
        elif path=='/api/flow-report':
            from urllib.parse import parse_qs
            q=parse_qs(urlparse(self.path).query); start=q.get('date_from',[''])[0]; end=q.get('date_to',[start])[0]
            try:pid=int(q.get('project_id',['0'])[0])
            except (TypeError,ValueError):self.send(400,json.dumps({'error':'專案編號格式不正確'},ensure_ascii=False));return
            try:
                with connect() as db:
                    require_project(db,pid)
                    start=parse_date(start,'起始流用日期');end=parse_date(end,'結束流用日期')
                    rows=flow_change_report(db,pid,start,end,include_category=True)
                self.send(200,json.dumps({'date_from':start,'date_to':end,'rows':rows},ensure_ascii=False))
            except Exception as e:self.send(400,json.dumps({'error':display_error(e)},ensure_ascii=False))
        elif path=='/api/export-projects':
            from urllib.parse import parse_qs, quote
            q=parse_qs(urlparse(self.path).query);status=q.get('status',['all'])[0];fmt=q.get('format',['xlsx'])[0]
            try:
                if status not in ('active','future','done','all'):raise ValueError('不支援的專案狀態')
                if fmt not in ('xlsx','md','pdf'):raise ValueError('不支援的匯出格式')
                data=bootstrap();today=date.today().isoformat();counts={'active':0,'future':0,'done':0}
                def project_status(p):
                    if p.get('end_date') and today>p['end_date']:return 'done'
                    if p.get('start_date') and today<p['start_date']:return 'future'
                    return 'active'
                projects=data['projects']
                for p in projects:counts[project_status(p)]+=1
                projects=[p for p in projects if status=='all' or project_status(p)==status]
                rank={'active':0,'future':1,'done':2}
                projects.sort(key=lambda p:(rank[project_status(p)] if status=='all' else 0,p.get('end_date') or '9999-12-31',p.get('name') or ''))
                headers=['專案名稱','專案編號','執行單位','執行起日','執行迄日','專案經費總額','真實執行率','預估執行率','聯絡人','電話','Email'];rows=[]
                for p in projects:
                    denominator=round_amount(p.get('total_budget') or 0)
                    related=[x for x in data['expenses'] if x['project_id']==p['id'] and not x['voided']]
                    confirmed=sum(round_amount(x['amount']) for x in related if not is_pending_expense(x))
                    forecast=sum(round_amount(x['amount']) for x in related)
                    real_rate=execution_percent(confirmed,denominator);forecast_rate=execution_percent(forecast,denominator)
                    rows.append([p.get('name',''),p.get('code',''),p.get('unit',''),(p.get('start_date') or '').replace('-','/'),(p.get('end_date') or '').replace('-','/'),denominator,real_rate,forecast_rate,p.get('contact',''),p.get('phone',''),p.get('email','')])
                title={'active':'執行中','future':'未執行','done':'已完成','all':'總覽'}[status]
                sheets=[(f'專案清單（{title}）',headers,rows)]
                if fmt=='xlsx':content=make_xlsx_sheets(sheets);typ='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';filename=f'所有專案_{title}.xlsx'
                elif fmt=='md':content='\ufeff'+sheets_to_markdown({'name':'所有專案','code':title},sheets);typ='text/markdown; charset=utf-8';filename=f'所有專案_{title}.md'
                else:content=make_pdf_sheets([(f'所有專案 · {title}',headers,rows)],'landscape');typ='application/pdf';filename=f'所有專案_{title}.pdf'
                self.send(200,content,typ,{'Content-Disposition':f"attachment; filename*=UTF-8''{quote(filename)}"})
            except Exception as e:self.send(400,json.dumps({'error':display_error(e)},ensure_ascii=False))
        elif path=='/api/export-data':
            from urllib.parse import parse_qs, quote
            q=parse_qs(urlparse(self.path).query);scope=q.get('scope',['overall'])[0];fmt=q.get('format',['xlsx'])[0];mode=q.get('mode',['combined'])[0];orientation=q.get('orientation',['landscape'])[0]
            try:
                try:pid=int(q.get('project_id',['0'])[0])
                except (TypeError,ValueError):raise ValueError('專案編號格式不正確')
                data=bootstrap();owners=q.get('owner',[]);include_overall=scope in ('overall','all')
                project,sheets=build_export_sheets(data,pid,owners,include_overall)
                if not sheets:raise ValueError('請至少選擇一種要匯出的資料')
                if scope=='owners' and not owners:raise ValueError('請至少選擇一位單位')
                if fmt=='md':
                    if mode=='separate' and scope=='owners':
                        mem=io.BytesIO()
                        with zipfile.ZipFile(mem,'w',zipfile.ZIP_DEFLATED) as z:
                            for owner in owners:
                                _,owner_sheets=build_export_sheets(data,pid,[owner],False);safe=re.sub(r'[\\/:*?"<>|]','_',owner)
                                z.writestr(f'{safe}.md',sheets_to_markdown(project,owner_sheets))
                        content=mem.getvalue();typ='application/zip';filename=f'{project["code"]}_單位經費.zip'
                    else:content='\ufeff'+sheets_to_markdown(project,sheets);typ='text/markdown; charset=utf-8';filename=f'{project["code"]}_經費報表.md'
                elif fmt=='pdf':
                    if orientation not in ('portrait','landscape'):orientation='landscape'
                    content=make_pdf_sheets([(f'{project["name"]}（{project["code"]}） · {name}',headers,rows) for name,headers,rows in sheets],orientation);typ='application/pdf';filename=f'{project["code"]}_經費報表.pdf'
                else:
                    if mode=='separate' and scope=='owners':
                        mem=io.BytesIO()
                        with zipfile.ZipFile(mem,'w',zipfile.ZIP_DEFLATED) as z:
                            for owner in owners:
                                _,owner_sheets=build_export_sheets(data,pid,[owner],False);safe=re.sub(r'[\\/:*?"<>|]','_',owner)
                                z.writestr(f'{safe}.xlsx',make_xlsx_sheets(owner_sheets))
                        content=mem.getvalue();typ='application/zip';filename=f'{project["code"]}_單位經費.zip'
                    else:content=make_xlsx_sheets(sheets);typ='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';filename=f'{project["code"]}_經費報表.xlsx'
                self.send(200,content,typ,{'Content-Disposition':f"attachment; filename*=UTF-8''{quote(filename)}"})
            except Exception as e:self.send(400,json.dumps({'error':display_error(e)},ensure_ascii=False))
        elif path=='/api/export':
            from urllib.parse import parse_qs
            q=parse_qs(urlparse(self.path).query); kind=q.get('kind',['budget'])[0]
            if kind!='flowchanges':
                self.send(400,json.dumps({'error':'不支援的匯出資料類型'},ensure_ascii=False));return
            try:pid=int(q.get('project_id',['0'])[0])
            except (TypeError,ValueError):self.send(400,json.dumps({'error':'專案編號格式不正確'},ensure_ascii=False));return
            data=bootstrap(); p=next((x for x in data['projects'] if x['id']==pid),None)
            if not p:self.send(404,'找不到專案','text/plain');return
            start=q.get('date_from',[''])[0]; end=q.get('date_to',[start])[0]
            try:
                start=parse_date(start,'起始流用日期');end=parse_date(end,'結束流用日期')
                with connect() as db: changes=flow_change_report(db,pid,start,end,include_category=True)
                headers=['經費類別','經費屬性','預算科目','變更前預算','變更後預算','增減']
                def report_rows(items):return [[x['source'],x['category'],x['subject'],x['before'],x['after'],x['change']] for x in items]
                sheets=[('總表',headers,report_rows(changes)+[['合計','','',sum(x['before'] for x in changes),sum(x['after'] for x in changes),sum(x['change'] for x in changes)]])]
                for source in sorted({x['source'] for x in changes}):
                    items=[x for x in changes if x['source']==source]
                    sheets.append((source,headers,report_rows(items)+[['合計','','',sum(x['before'] for x in items),sum(x['after'] for x in items),sum(x['change'] for x in items)]]))
                history=[x for x in data['transfers'] if x['project_id']==pid and x.get('status')=='executed' and start<=x.get('date','')<=end]
                sheets.append(('流用紀錄',*transfer_record_sheet(history)))
                self.send(200,make_xlsx_sheets(sheets),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            except Exception as e:self.send(400,json.dumps({'error':display_error(e)},ensure_ascii=False))
        elif path=='/':self.send(200,(STATIC_DIR/'index.html').read_bytes(),'text/html; charset=utf-8')
        elif path=='/style.css':self.send(200,(STATIC_DIR/'style.css').read_bytes(),'text/css; charset=utf-8')
        elif path=='/app.js':self.send(200,(STATIC_DIR/'app.js').read_bytes(),'application/javascript; charset=utf-8')
        else:self.send(404,'Not found','text/plain')
    def do_POST(self):
        host=self.headers.get('Host','').lower()
        origin=self.headers.get('Origin','')
        allowed_hosts={f'{HOST}:{PORT}'.lower(),f'localhost:{PORT}'.lower()}
        if host not in allowed_hosts or (origin and urlparse(origin).netloc.lower() not in allowed_hosts):
            self.send(403,json.dumps({'error':'請從本機系統頁面送出請求'},ensure_ascii=False));return
        if self.headers.get_content_type()!='application/json':
            self.send(415,json.dumps({'error':'請求格式必須是 JSON'},ensure_ascii=False));return
        length=int(self.headers.get('Content-Length',0)); raw=self.rfile.read(length)
        try:
            if self.path=='/api/email/preview':
                payload=json.loads(raw.decode('utf-8'));pid=int(payload.get('project_id') or 0);recipient_ids=payload.get('recipient_ids',[]) or [];manager_scopes=payload.get('manager_scopes',{}) or {};formats=payload.get('formats') or ['xlsx']
                report,messages,duplicates=build_email_payloads(pid,recipient_ids,manager_scopes,formats)
                public=[]
                for m in messages:
                    item={k:m[k] for k in ('recipient_id','recipient_type','recipient_name','email','report_scope','subject','html','attachment_names','attachment_formats')};item['sheet_count']=len(m['sheets']);item['sheet_names']=[x[0] for x in m['sheets']];public.append(item)
                self.send(200,json.dumps({'report_as_of':report['report_as_of'],'messages':public,'duplicates':duplicates,'recipient_ids':recipient_ids,'formats':formats},ensure_ascii=False));return
            if self.path=='/api/email/smtp-test':
                payload=json.loads(raw.decode('utf-8'));to=str(payload.get('to') or '').strip()
                if not valid_email(to):raise ValueError('請輸入有效的測試收件 Email')
                config=email_smtp_config();html='<html><body><h2>寄件設定測試成功</h2><p>本訊息由專案經費管理系統寄出。</p></body></html>'
                send_email(config,to,'專案經費管理系統－寄件測試',html)
                self.send(200,json.dumps({'ok':True},ensure_ascii=False));return
            if self.path=='/api/email/send':
                payload=json.loads(raw.decode('utf-8'));pid=int(payload.get('project_id') or 0);recipient_ids=payload.get('recipient_ids',[]) or [];manager_scopes=payload.get('manager_scopes',{}) or {};formats=payload.get('formats') or ['xlsx']
                report,messages,duplicates=build_email_payloads(pid,recipient_ids,manager_scopes,formats)
                if not messages:raise ValueError('請至少選取一位收件人')
                config=email_smtp_config()
                with connect() as db:
                    cur=db.execute("INSERT INTO email_send_batches(project_id,report_as_of,status) VALUES(?,?,'sending')",(pid,report['report_as_of']));batch_id=cur.lastrowid
                job={'batch_id':batch_id,'status':'sending','total':len(messages),'completed':0,'sent':0,'failed':0,'current_index':-1,'current_name':'','fatal_error':'','items':[{'name':m['recipient_name'],'email':m['email'],'status':'waiting','error':''} for m in messages]}
                with EMAIL_JOBS_LOCK:EMAIL_JOBS[str(batch_id)]=job
                threading.Thread(target=run_email_delivery_job,args=(batch_id,messages,config),daemon=True).start()
                self.send(202,json.dumps({'ok':True,'batch_id':batch_id,'total':len(messages),'duplicates':duplicates},ensure_ascii=False));return
            if self.path=='/api/preview':
                payload=json.loads(raw.decode('utf-8')); mapped=BUDGET_IMPORT_HEADERS
                parsed=[]; names=[]; errors=[]
                for f in payload.get('files',[]):
                    names.append(f['name'])
                    try:
                        file_rows=parse_upload(f['name'],base64.b64decode(f['data']))
                        if not file_rows:errors.append(f"{f['name']}：沒有資料列");continue
                        header_errors=validate_import_headers(file_rows,f['name'],mapped)
                        if header_errors:errors.extend(header_errors);continue
                        normalized=[clean_option_fields({mapped.get(str(k).strip(),str(k).strip()):v for k,v in r.items()}) for r in file_rows]
                        required={'source','owner','category','subject','initial'}
                        header_fields={mapped.get(str(h).strip(),str(h).strip()) for h in file_rows.headers if str(h or '').strip()}
                        if not required.issubset(header_fields):errors.append(f"{f['name']}：缺少必填欄位（經費類別、單位、經費屬性、預算科目、初始預算）")
                        for i,row in zip(file_rows.row_numbers,normalized):
                            if is_blank_import_row(row):continue
                            row['_file']=f['name'];row['_row_number']=i
                            missing=[label for key,label in [('source','經費類別'),('owner','單位'),('category','經費屬性'),('subject','預算科目'),('initial','初始預算')] if not str(row.get(key) or '').strip()]
                            if missing:errors.append(f"{f['name']} 第 {i} 列：必填欄位空白（{'、'.join(missing)}）")
                            elif not re.fullmatch(r'\s*[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?\s*',str(row.get('initial',''))):errors.append(f"{f['name']} 第 {i} 列：初始預算必須是數字")
                            else:
                                try:
                                    row['initial']=input_amount(row['initial'],'初始預算')
                                    if row['initial']<0:errors.append(f"{f['name']} 第 {i} 列：初始預算不可小於 0（{row.get('owner','')}／{row.get('subject','')}）")
                                except ValueError as e:errors.append(f"{f['name']} 第 {i} 列：{e}")
                            parsed.append(row)
                    except Exception as e: errors.append(f"{f['name']}：{e}")
                new_options={'source':[],'owner':[],'subject':[],'category':[]}
                pid=int(payload.get('project_id') or 0)
                if pid:
                    with connect() as db:
                        require_project(db,pid)
                        known={kind:{r[0] for r in db.execute("SELECT value FROM project_options WHERE project_id=? AND kind=?",(pid,kind))} for kind in new_options}
                        for kind,column in (('owner','owner'),('subject','subject'),('category','category')):
                            new_options[kind]=sorted({str(r.get(column) or '').strip() for r in parsed if str(r.get(column) or '').strip() and str(r.get(column) or '').strip() not in known[kind]})
                        known_sources=known['source']
                        new_options['source']=sorted({str(r.get('source') or '').strip() for r in parsed if str(r.get('source') or '').strip() and str(r.get('source') or '').strip() not in known_sources})
                self.send(200,json.dumps({'rows':parsed,'filenames':names,'errors':errors,'new_options':new_options},ensure_ascii=False)); return
            elif self.path=='/api/preview-expenses':
                payload=json.loads(raw.decode('utf-8')); mapped=EXPENSE_IMPORT_HEADERS
                pid=int(payload.get('project_id',0))
                with connect() as db:
                    project=require_project(db,pid);base_items=totals(db,pid);owner_res,combo_res=cross_unit_reservations(db,pid)
                    project_left=min(round_amount(project['total_budget']),sum(round_amount(x['initial']) for x in base_items))-sum(x['spent'] for x in base_items)
                    source_left={};owner_left={};combo_left={}
                    for x in base_items:
                        source_left[x['source']]=source_left.get(x['source'],0)+round_amount(x['initial'])-x['spent']
                        owner_left[x['owner']]=owner_left.get(x['owner'],0)+x['budget']-x['spent']
                        key=(x['owner'],x['source'],x['category']);combo_left[key]=combo_left.get(key,0)+x['budget']-x['spent']
                    for k,v in owner_res.items():owner_left[k]=owner_left.get(k,0)-v
                    for k,v in combo_res.items():combo_left[k]=combo_left.get(k,0)-v
                rows=[];names=[];errors=[];warnings=[];duplicates=[]
                with connect() as db:duplicate_keys=existing_expense_duplicates(db,pid)
                for f in payload.get('files',[]):
                    names.append(f['name'])
                    try:
                        parsed=parse_upload(f['name'],base64.b64decode(f['data']))
                        if not parsed:errors.append(f"{f['name']}：沒有資料列");continue
                        header_errors=validate_import_headers(parsed,f['name'],mapped)
                        if header_errors:errors.extend(header_errors);continue
                        canonical={mapped.get(str(k).strip(),str(k).strip()) for k in parsed.headers if str(k or '').strip()}
                        if parsed and not {'source','owner','subject','category','amount'}.issubset(canonical):
                            errors.append(f"{f['name']}：缺少經費類別、單位、預算科目、經費屬性或核銷金額欄位");continue
                        for i,rawrow in zip(parsed.row_numbers,parsed):
                            if is_blank_import_row(rawrow):continue
                            row=clean_option_fields({mapped.get(str(k).strip(),str(k).strip()):v for k,v in rawrow.items()})
                            try:
                                missing=[label for key,label in [('owner','單位'),('source','經費類別'),('subject','預算科目'),('category','經費屬性')] if not str(row.get(key) or '').strip()]
                                if missing:raise ValueError(f"必填欄位空白（{'、'.join(missing)}）")
                                if str(row.get('amount') or '').strip()=='':raise ValueError('核銷金額不可空白')
                                total=input_amount(row.get('amount'),'核銷金額')
                                if total<0:raise ValueError('核銷金額不可小於 0')
                                missing_budget_item=False
                                with connect() as db:
                                    pid=int(payload.get('project_id',0)); require_project(db,pid)
                                    for kind,key,label in [('source','source','經費類別'),('owner','owner','單位'),('subject','subject','預算科目'),('category','category','經費屬性')]:require_project_option(db,pid,kind,str(row.get(key) or '').strip(),label)
                                    matched=db.execute("SELECT 1 FROM budget_items WHERE project_id=? AND source=? AND owner=? AND subject=? AND category=?",(pid,str(row.get('source') or '').strip(),str(row.get('owner') or '').strip(),str(row.get('subject') or '').strip(),str(row.get('category') or '').strip())).fetchone()
                                    missing_budget_item=not bool(matched)
                                payees=import_payee_pairs(row.get('payee_names'),row.get('payee_amounts'),total,i)
                                voucher=parse_date(row.get('voucher_date'),'憑證日期'); submitted=parse_date(row.get('submitted_date'),'登錄日期'); validate_expense_dates(project,submitted,voucher,f'{f["name"]} 第 {i} 列')
                                if submitted and submitted>date.today().isoformat():warnings.append(f'{f["name"]} 第 {i} 列：此登錄日期晚於今天，該日之前此筆明細會列為待核銷')
                                if missing_budget_item:
                                    source=str(row.get('source') or '').strip();owner=str(row.get('owner') or '').strip();subject=str(row.get('subject') or '').strip();category=str(row.get('category') or '').strip()
                                    warnings.append(f'{f["name"]} 第 {i} 列：找不到經費類別「{source}」、單位「{owner}」、預算科目「{subject}」、經費屬性「{category}」完全相符的預算明細。')
                                source=str(row.get('source') or '').strip();owner=str(row.get('owner') or '').strip();category=str(row.get('category') or '').strip();combo=(owner,source,category)
                                if total>project_left:raise ValueError(f'超過全專案核銷上限；目前最多可核銷 {max(0,project_left):,} 元')
                                if total>source_left.get(source,0):raise ValueError(f'超過「{source}」經費類別上限；目前最多可核銷 {max(0,source_left.get(source,0)):,} 元')
                                if total>owner_left.get(owner,0):raise ValueError(f'超過單位「{owner}」核銷上限；目前最多可核銷 {max(0,owner_left.get(owner,0)):,} 元')
                                if total>combo_left.get(combo,0):
                                    reserved=combo_res.get(combo,0)
                                    if reserved and total<=combo_left.get(combo,0)+reserved:
                                        with connect() as detail_db:raise ValueError(planned_reservation_message(detail_db,pid,combo,combo_left.get(combo,0)+reserved,reserved))
                                    raise ValueError(f'超過單位「{owner}」／經費類別「{source}」／經費屬性「{category}」核銷上限；目前最多可核銷 {max(0,combo_left.get(combo,0)):,} 元')
                                project_left-=total;source_left[source]=source_left.get(source,0)-total;owner_left[owner]=owner_left.get(owner,0)-total;combo_left[combo]=combo_left.get(combo,0)-total
                                subject=str(row.get('subject') or '').strip();note=str(row.get('note') or '')
                                duplicate_key=expense_duplicate_key(submitted,voucher,source,owner,category,subject,total,';'.join(x['name'] for x in payees),note)
                                matches=list(duplicate_keys.get(duplicate_key,[]))
                                if matches:duplicates.append({'row':len(rows)+1,'matches':matches})
                                duplicate_keys.setdefault(duplicate_key,[]).append({'file':f['name'],'row':i})
                                rows.append({'_file':f['name'],'_row_number':i,'batch_description':str(row.get('batch_description') or '').strip(),'submitted_date':submitted,'voucher_date':voucher,'source':source,'owner':owner,'subject':subject,'category':category,'amount':total,'note':note,'payees':payees,'missing_budget_item':missing_budget_item,'duplicate_matches':matches})
                            except Exception as e:errors.append(f"{f['name']} 第 {i} 列：{e}")
                    except Exception as e:errors.append(f"{f['name']}：{e}")
                self.send(200,json.dumps({'rows':rows,'filenames':names,'errors':errors,'warnings':warnings,'duplicates':duplicates},ensure_ascii=False));return
            elif self.path=='/api/preview-transfers':
                payload=json.loads(raw.decode('utf-8'));mapped=TRANSFER_IMPORT_HEADERS
                pid=int(payload.get('project_id',0));rows=[];names=[];errors=[];duplicates=[]
                with connect() as db:
                    require_project(db,pid);project_totals=totals(db,pid);options={k:{x[0] for x in db.execute("SELECT value FROM project_options WHERE project_id=? AND kind=?",(pid,k))} for k in ('source','owner','subject','category')}
                    owner_spent={};owner_budget={}
                    for x in project_totals:
                        owner_spent[x['owner']]=owner_spent.get(x['owner'],0)+round_amount(x['spent']);owner_budget[x['owner']]=owner_budget.get(x['owner'],0)+round_amount(x['budget'])
                    budget_map={(x['source'],x['owner'],x['subject'],x['category']):dict(x) for x in project_totals};initial_budget_keys=set(budget_map);allowed_target_pairs={(x['source'],x['category'],x['subject']) for x in project_totals};seen=existing_transfer_duplicates(db,pid)
                    for f in payload.get('files',[]):
                        names.append(f['name'])
                        try:
                            parsed=parse_upload(f['name'],base64.b64decode(f['data']))
                            if not parsed:errors.append(f"{f['name']}：沒有資料列");continue
                            header_errors=validate_import_headers(parsed,f['name'],mapped)
                            if header_errors:errors.extend(header_errors);continue
                            normalized=[clean_option_fields({mapped.get(str(k).strip(),str(k).strip()):v for k,v in rawrow.items()}) for rawrow in parsed]
                            required={'date','source','from_owner','from_subject','from_category','to_owner','to_subject','to_category','amount'}
                            header_fields={mapped.get(str(h).strip(),str(h).strip()) for h in parsed.headers if str(h or '').strip()}
                            if not required.issubset(header_fields):errors.append(f"{f['name']}：缺少必填欄位（流用日期、經費類別、流出與流入單位、預算科目及經費屬性、流用金額）")
                            for rownum,r in zip(parsed.row_numbers,normalized):
                                if is_blank_import_row(r):continue
                                try:
                                    required_labels=[('date','流用日期'),('source','經費類別'),('from_owner','流出單位'),('from_subject','流出預算科目'),('from_category','流出經費屬性'),('to_owner','流入單位'),('to_subject','流入預算科目'),('to_category','流入經費屬性'),('amount','金額')]
                                    missing=[label for key,label in required_labels if not str(r.get(key) or '').strip()]
                                    if missing:raise ValueError('必填欄位空白（'+'、'.join(missing)+'）')
                                    r['date']=validate_transfer_date(require_project(db,pid),r['date']);amount=input_amount(r['amount'],'流用金額')
                                    r['_file']=f['name'];r['_row_number']=rownum
                                    if amount<=0:raise ValueError('金額必須大於 0')
                                    option_kind={'from_owner':'owner','to_owner':'owner','from_subject':'subject','to_subject':'subject','from_category':'category','to_category':'category'}
                                    for key,label in [('source','經費類別'),('from_owner','流出單位'),('to_owner','流入單位'),('from_subject','流出預算科目'),('to_subject','流入預算科目'),('from_category','流出經費屬性'),('to_category','流入經費屬性')]:
                                        if r[key] not in options[option_kind.get(key,key)]:raise ValueError(f'{label}不是本專案已建立的選項')
                                    if r['from_category']!=r['to_category']:raise ValueError('流出與流入經費屬性必須相同')
                                    if (r['source'],r['to_category'],r['to_subject']) not in allowed_target_pairs:raise ValueError('本專案預算明細中沒有相同經費類別、經費屬性及預算科目的配對，請先建立預算明細')
                                    if r['from_owner']==r['to_owner'] and r['from_subject']==r['to_subject'] and r['from_category']==r['to_category']:raise ValueError('流出與流入不可為同一預算項目')
                                    key=(r['date'],r['source'],r['from_owner'],r['from_subject'],r['from_category'],r['to_owner'],r['to_subject'],r['to_category'],amount,str(r.get('note') or '').strip())
                                    out=budget_map.get((r['source'],r['from_owner'],r['from_subject'],r['from_category']))
                                    if not out:raise ValueError('找不到流出預算項目')
                                    if amount>round_amount(out.get('available_balance',out['balance'])):raise ValueError(f"扣除規劃圈存後流出超過可用餘額，目前最多可流出 {max(0,round_amount(out.get('available_balance',out['balance']))):,} 元")
                                    if r['from_owner']!=r['to_owner']:
                                        left=owner_budget.get(r['from_owner'],0)-owner_spent.get(r['from_owner'],0)
                                        if amount>left:raise ValueError(f'流出後會超過「{r["from_owner"]}」單位可控經費餘額')
                                    matches=list(seen.get(key,[]))
                                    is_duplicate=bool(matches)
                                    if is_duplicate:duplicates.append({'row':len(rows)+1,'matches':matches})
                                    seen.setdefault(key,[]).append({'file':f['name'],'row':rownum})
                                    out['budget']=round_amount(out['budget'])-amount;out['balance']=round_amount(out['balance'])-amount;out['available_balance']=round_amount(out.get('available_balance',out['balance']))-amount
                                    target_key=(r['source'],r['to_owner'],r['to_subject'],r['to_category']);target=budget_map.get(target_key);r['missing_budget_item']=target_key not in initial_budget_keys
                                    if target:
                                        target['budget']=round_amount(target['budget'])+amount;target['balance']=round_amount(target['balance'])+amount;target['available_balance']=round_amount(target['balance'])-round_amount(target.get('reserved_out',0))
                                    else:
                                        budget_map[target_key]={'source':r['source'],'owner':r['to_owner'],'subject':r['to_subject'],'category':r['to_category'],'budget':amount,'balance':amount,'available_balance':amount,'reserved_out':0,'spent':0,'id':None}
                                    owner_budget[r['from_owner']]=owner_budget.get(r['from_owner'],0)-(amount if r['from_owner']!=r['to_owner'] else 0);owner_budget[r['to_owner']]=owner_budget.get(r['to_owner'],0)+(amount if r['from_owner']!=r['to_owner'] else 0)
                                    r['amount']=amount;r['note']=str(r.get('note') or '').strip();r['duplicate']=is_duplicate;r['duplicate_matches']=matches
                                    rows.append(r)
                                except Exception as e:errors.append(f"{f['name']} 第 {rownum} 列：{e}")
                        except Exception as e:errors.append(f"{f['name']}：{e}")
                self.send(200,json.dumps({'rows':rows,'filenames':names,'errors':errors,'duplicates':duplicates},ensure_ascii=False));return
            elif self.path=='/api/restore':
                candidate=DB_PATH.with_name('經費管理.restore.db');backup=DB_PATH.with_name('經費管理.before-restore.db')
                try:
                    payload=json.loads(raw.decode('utf-8'));candidate.write_bytes(base64.b64decode(payload['data'],validate=True))
                    check=sqlite3.connect(candidate)
                    check.row_factory=sqlite3.Row
                    try:
                        check.execute('PRAGMA query_only=ON')
                        integrity=check.execute('PRAGMA integrity_check').fetchone()[0]
                        tables={r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                        if integrity!='ok' or not {'projects','budget_items','expenses','transfers'}.issubset(tables):raise ValueError('備份檔不是有效的經費資料庫')
                        issues=backup_ledger_issues(check)
                    finally:check.close()
                    if issues and not payload.get('confirm_ledger_issues'):
                        self.send(200,json.dumps({'ok':True,'confirmation_required':True,'issues':issues},ensure_ascii=False));return
                    if DB_PATH.exists():shutil.copy2(DB_PATH,backup)
                    gc.collect()
                    os.replace(candidate,DB_PATH)
                    try:initialize()
                    except Exception:
                        if backup.exists():os.replace(backup,DB_PATH)
                        else:DB_PATH.unlink(missing_ok=True)
                        raise
                except (sqlite3.DatabaseError,ValueError,KeyError,binascii.Error,OSError) as e:
                    if isinstance(e,sqlite3.DatabaseError):raise ValueError('備份檔無法讀取或不是有效的經費資料庫') from e
                    if isinstance(e,(KeyError,binascii.Error)):raise ValueError('備份檔格式不正確或缺少資料內容') from e
                    if isinstance(e,OSError):raise ValueError('資料庫檔案目前無法替換，請關閉其他使用中的系統視窗後重試') from e
                    raise
                finally:candidate.unlink(missing_ok=True)
                self.send(200,json.dumps({'ok':True},ensure_ascii=False)); return
            else:res=apply_action(json.loads(raw.decode('utf-8')))
            self.send(200,json.dumps(res,ensure_ascii=False))
        except Exception as e:self.send(400,json.dumps({'error':display_error(e)},ensure_ascii=False))
    def do_DELETE(self): self.send(405,'Use POST action','text/plain')


def main():
    initialize()
    server=ThreadingHTTPServer((HOST,PORT),Handler)
    url=f'http://{HOST}:{PORT}'
    # 每次啟動使用新的網址識別碼，讓瀏覽器重用舊分頁時也能辨認為新啟動。
    launch_url=f'{url}/?startup={SERVER_SESSION}'
    print(f'經費管理系統已啟動：{url}\n資料庫：{DB_PATH}\n關閉視窗或按 Ctrl+C 可停止。')
    if '--no-browser' not in sys.argv: webbrowser.open(launch_url)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()

if __name__=='__main__':main()
