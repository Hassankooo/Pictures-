# -*- coding: utf-8 -*-

import os
import json
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

def get_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured.")
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    sender_id TEXT UNIQUE NOT NULL,
                    name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS quizzes (
                    id SERIAL PRIMARY KEY,
                    sender_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_filename TEXT,
                    questions_json TEXT NOT NULL,
                    parent_quiz_id INTEGER DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS quiz_attempts (
                    id SERIAL PRIMARY KEY,
                    sender_id TEXT NOT NULL,
                    quiz_id INTEGER NOT NULL,
                    current_index INTEGER DEFAULT 0,
                    correct_count INTEGER DEFAULT 0,
                    wrong_count INTEGER DEFAULT 0,
                    unanswered_count INTEGER DEFAULT 0,
                    answers_json TEXT DEFAULT '[]',
                    status TEXT DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS explanations (
                    id SERIAL PRIMARY KEY,
                    sender_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    messages_json TEXT NOT NULL,
                    completion_logs_json TEXT DEFAULT '[]',
                    last_read_page INTEGER DEFAULT 0,
                    last_read_at TEXT DEFAULT NULL,
                    note TEXT DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            try:
                cur.execute("ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS parent_quiz_id INTEGER DEFAULT NULL")
                cur.execute("ALTER TABLE explanations ADD COLUMN IF NOT EXISTS completion_logs_json TEXT DEFAULT '[]'")
                cur.execute("ALTER TABLE explanations ADD COLUMN IF NOT EXISTS last_read_at TEXT DEFAULT NULL")
            except Exception:
                conn.rollback()

            cur.execute("CREATE INDEX IF NOT EXISTS idx_users_sender_id ON users(sender_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_quizzes_sender_id ON quizzes(sender_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_attempts_sender_id ON quiz_attempts(sender_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_explanations_sender_id ON explanations(sender_id)")
        conn.commit()
    finally:
        conn.close()

def save_user(sender_id, name=None):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (sender_id, name) VALUES (%s, %s)
                ON CONFLICT (sender_id) DO UPDATE SET
                    name = COALESCE(EXCLUDED.name, users.name),
                    updated_at = CURRENT_TIMESTAMP
            """, (sender_id, name))
        conn.commit()
    finally:
        conn.close()

def get_user(sender_id):
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE sender_id = %s", (sender_id,))
            return cur.fetchone()
    finally:
        conn.close()

def save_quiz(sender_id, title, questions, source_filename="", parent_quiz_id=None):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            questions_json = json.dumps(questions, ensure_ascii=False)
            cur.execute("""
                INSERT INTO quizzes (sender_id, title, source_filename, questions_json, parent_quiz_id)
                VALUES (%s, %s, %s, %s, %s) RETURNING id
            """, (sender_id, title, source_filename, questions_json, parent_quiz_id))
            quiz_id = cur.fetchone()[0]
        conn.commit()
        return quiz_id
    finally:
        conn.close()

def get_quiz(quiz_id, sender_id):
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM quizzes WHERE id = %s AND sender_id = %s", (quiz_id, sender_id))
            row = cur.fetchone()
            if not row: return None
            row["questions"] = json.loads(row["questions_json"])
            return row
    finally:
        conn.close()

def get_user_quizzes(sender_id):
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, title, source_filename, parent_quiz_id, created_at FROM quizzes WHERE sender_id = %s AND (parent_quiz_id IS NULL) ORDER BY created_at DESC", (sender_id,))
            return cur.fetchall()
    finally:
        conn.close()

def get_mistake_quiz_by_parent(parent_quiz_id, sender_id):
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM quizzes WHERE parent_quiz_id = %s AND sender_id = %s ORDER BY created_at DESC LIMIT 1", (parent_quiz_id, sender_id))
            row = cur.fetchone()
            if not row: return None
            row["questions"] = json.loads(row["questions_json"])
            return row
    finally:
        conn.close()

def delete_quiz(quiz_id, sender_id):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM quiz_attempts WHERE quiz_id = %s AND sender_id = %s", (quiz_id, sender_id))
            cur.execute("DELETE FROM quizzes WHERE id = %s AND sender_id = %s", (quiz_id, sender_id))
            cur.execute("DELETE FROM quizzes WHERE parent_quiz_id = %s AND sender_id = %s", (quiz_id, sender_id))
        conn.commit()
    finally:
        conn.close()

def create_attempt(sender_id, quiz_id):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE quiz_attempts SET status = 'stopped', updated_at = CURRENT_TIMESTAMP WHERE sender_id = %s AND status = 'active'", (sender_id,))
            cur.execute("""
                INSERT INTO quiz_attempts (sender_id, quiz_id, current_index, correct_count, wrong_count, unanswered_count, answers_json, status)
                VALUES (%s, %s, 0, 0, 0, 0, '[]', 'active') RETURNING id
            """, (sender_id, quiz_id))
            attempt_id = cur.fetchone()[0]
        conn.commit()
        return attempt_id
    finally:
        conn.close()

def get_attempt(attempt_id, sender_id):
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM quiz_attempts WHERE id = %s AND sender_id = %s", (attempt_id, sender_id))
            row = cur.fetchone()
            if not row: return None
            row["answers"] = json.loads(row["answers_json"] or "[]")
            return row
    finally:
        conn.close()

def get_active_attempt(sender_id):
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM quiz_attempts WHERE sender_id = %s AND status = 'active' ORDER BY updated_at DESC LIMIT 1", (sender_id,))
            row = cur.fetchone()
            if not row: return None
            row["answers"] = json.loads(row["answers_json"] or "[]")
            return row
    finally:
        conn.close()

def get_quiz_latest_attempt(sender_id, quiz_id):
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM quiz_attempts WHERE sender_id = %s AND quiz_id = %s ORDER BY updated_at DESC LIMIT 1", (sender_id, quiz_id))
            row = cur.fetchone()
            if not row: return None
            row["answers"] = json.loads(row["answers_json"] or "[]")
            return row
    finally:
        conn.close()

def update_attempt(attempt_id, sender_id, current_index, correct_count, wrong_count, unanswered_count, answers, status="active"):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE quiz_attempts SET current_index = %s, correct_count = %s, wrong_count = %s, unanswered_count = %s, answers_json = %s, status = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND sender_id = %s
            """, (current_index, correct_count, wrong_count, unanswered_count, json.dumps(answers, ensure_ascii=False), status, attempt_id, sender_id))
        conn.commit()
    finally:
        conn.close()

def stop_attempt(attempt_id, sender_id):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE quiz_attempts SET status = 'stopped', updated_at = CURRENT_TIMESTAMP WHERE id = %s AND sender_id = %s", (attempt_id, sender_id))
        conn.commit()
    finally:
        conn.close()

def complete_attempt(attempt_id, sender_id):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE quiz_attempts SET status = 'completed', updated_at = CURRENT_TIMESTAMP WHERE id = %s AND sender_id = %s", (attempt_id, sender_id))
        conn.commit()
    finally:
        conn.close()

def save_explanation(sender_id, title, messages):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            messages_json = json.dumps(messages, ensure_ascii=False)
            cur.execute("""
                INSERT INTO explanations (sender_id, title, messages_json)
                VALUES (%s, %s, %s) RETURNING id
            """, (sender_id, title, messages_json))
            exp_id = cur.fetchone()[0]
        conn.commit()
        return exp_id
    finally:
        conn.close()

def get_user_explanations(sender_id):
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, title, completion_logs_json, last_read_page, created_at FROM explanations WHERE sender_id = %s ORDER BY created_at DESC", (sender_id,))
            rows = cur.fetchall()
            results = []
            for r in rows:
                logs = json.loads(r["completion_logs_json"] or "[]")
                results.append({
                    "id": r["id"],
                    "title": r["title"],
                    "completion_count": len(logs),
                    "last_read_page": r["last_read_page"],
                    "created_at": r["created_at"]
                })
            return results
    finally:
        conn.close()

def get_explanation(explanation_id, sender_id):
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM explanations WHERE id = %s AND sender_id = %s", (explanation_id, sender_id))
            row = cur.fetchone()
            if not row: return None
            row["messages"] = json.loads(row["messages_json"] or "[]")
            row["completion_logs"] = json.loads(row["completion_logs_json"] or "[]")
            return row
    finally:
        conn.close()

def add_explanation_completion(explanation_id, sender_id, log_str):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT completion_logs_json FROM explanations WHERE id = %s AND sender_id = %s", (explanation_id, sender_id))
            row = cur.fetchone()
            if row:
                logs = json.loads(row[0] or "[]")
                logs.append(log_str)
                cur.execute("""
                    UPDATE explanations 
                    SET completion_logs_json = %s, updated_at = CURRENT_TIMESTAMP 
                    WHERE id = %s AND sender_id = %s
                """, (json.dumps(logs, ensure_ascii=False), explanation_id, sender_id))
        conn.commit()
    finally:
        conn.close()

def update_explanation_page(explanation_id, sender_id, page, log_str):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE explanations 
                SET last_read_page = %s, last_read_at = %s, updated_at = CURRENT_TIMESTAMP 
                WHERE id = %s AND sender_id = %s
            """, (page, log_str, explanation_id, sender_id))
        conn.commit()
    finally:
        conn.close()

def update_explanation_note(explanation_id, sender_id, note):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE explanations 
                SET note = %s, updated_at = CURRENT_TIMESTAMP 
                WHERE id = %s AND sender_id = %s
            """, (note, explanation_id, sender_id))
        conn.commit()
    finally:
        conn.close()

if __name__ == "__main__":
    init_db()
    print("Database initialized successfully.")
                        