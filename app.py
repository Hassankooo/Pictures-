# -*- coding: utf-8 -*-

import os
import re
import json
import uuid
import time
import shutil
import threading
import subprocess
import traceback
import tempfile
import mimetypes
from datetime import datetime

import requests
from flask import Flask, request
from pdf2image import convert_from_path, pdfinfo_from_path
from google import genai
from google.genai import types

import database

# ============================================================
# APP & CONFIG
# ============================================================

app = Flask(__name__)

PAGE_ACCESS_TOKEN = os.environ.get("PAGE_ACCESS_TOKEN", "")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v20.0")
GRAPH_URL = f"https://graph.facebook.com/{GRAPH_VERSION}/me/messages"

# ============================================================
# GEMINI SETUP
# ============================================================

env_keys = os.environ.get("GEMINI_API_KEYS", "").strip()
GEMINI_API_KEYS = [key.strip() for key in env_keys.split(",") if key.strip()]

MODELS_TO_TRY = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.7-flash"
]

clients = [genai.Client(api_key=key) for key in GEMINI_API_KEYS]
current_key = 0
key_lock = threading.Lock()

def get_next_client():
    global current_key
    if not clients:
        raise RuntimeError("No Gemini API keys configured.")
    with key_lock:
        client = clients[current_key]
        current_key = (current_key + 1) % len(clients)
        return client

# ============================================================
# CONSTANTS & SESSIONS
# ============================================================

ALLOWED_EXTENSIONS = (".pdf", ".pptx", ".ppt", ".potx")
PAGE_SIZE = 10
MAX_DOWNLOAD_SIZE = 50 * 1024 * 1024

user_sessions = {}
session_lock = threading.Lock()

try:
    database.init_db()
except Exception:
    traceback.print_exc()

# ============================================================
# HELPERS
# ============================================================

def safe_filename(filename):
    filename = filename or "file"
    filename = os.path.basename(filename)
    return re.sub(r"[^\w\-. ]+", "_", filename, flags=re.UNICODE)

def format_arabic_datetime(dt=None):
    if dt is None:
        dt = datetime.now()
    months = {
        1: "يناير", 2: "فبراير", 3: "مارس", 4: "أبريل",
        5: "مايو", 6: "يونيو", 7: "يوليو", 8: "أغسطس",
        9: "سبتمبر", 10: "أكتوبر", 11: "نوفمبر", 12: "ديسمبر"
    }
    m_name = months.get(dt.month, str(dt.month))
    day = dt.day
    time_str = dt.strftime("%H:%M")
    return f"شهر {m_name}، يوم {day}، الساعة {time_str}"

def build_explanation_info_text(exp):
    logs = exp.get("completion_logs", [])
    lines = [f"📊 عدد مرات إكتمال القراءة: {len(logs)}"]
    for idx, log_str in enumerate(logs, start=1):
        lines.append(f"▪️ إكتمال القراءة رقم {idx}: {log_str}")
    
    last_page = exp.get("last_read_page", 0)
    last_at = exp.get("last_read_at")
    if last_page > 0:
        if last_at:
            lines.append(f"📄 آخر صفحة تمت القراءة إليها: {last_page} (بتاريخ: {last_at})")
        else:
            lines.append(f"📄 آخر صفحة تمت القراءة إليها: {last_page}")
    else:
        lines.append("📄 لم يتم تحديد صفحة التوقف بعد.")
    return "\n".join(lines)

def cleanup_directory(path):
    if not path:
        return
    try:
        if os.path.exists(path):
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        traceback.print_exc()

def cleanup_session(sender_id):
    with session_lock:
        session = user_sessions.pop(sender_id, None)
    if session and session.get("output_dir"):
        cleanup_directory(session.get("output_dir"))

def get_page_count(pdf_path):
    try:
        info = pdfinfo_from_path(pdf_path)
        return info.get("Pages", 0)
    except Exception:
        traceback.print_exc()
        return 0

# ============================================================
# MESSENGER API
# ============================================================

def send_message(sender_id, text):
    if not PAGE_ACCESS_TOKEN:
        return False
    payload = {"recipient": {"id": sender_id}, "message": {"text": str(text)}}
    try:
        res = requests.post(GRAPH_URL, params={"access_token": PAGE_ACCESS_TOKEN}, json=payload, timeout=30)
        return res.status_code < 400
    except Exception:
        traceback.print_exc()
        return False

def send_quick_replies(sender_id, text, options):
    if not PAGE_ACCESS_TOKEN:
        return False
    quick_replies = [{"content_type": "text", "title": str(opt)[:20], "payload": str(opt)} for opt in options]
    payload = {"recipient": {"id": sender_id}, "message": {"text": str(text), "quick_replies": quick_replies}}
    try:
        res = requests.post(GRAPH_URL, params={"access_token": PAGE_ACCESS_TOKEN}, json=payload, timeout=30)
        return res.status_code < 400
    except Exception:
        traceback.print_exc()
        return False

def send_image_file(sender_id, image_path):
    if not PAGE_ACCESS_TOKEN:
        return False
    try:
        with open(image_path, "rb") as img_f:
            files = {"filedata": (os.path.basename(image_path), img_f, "image/jpeg")}
            payload = {
                "recipient": json.dumps({"id": sender_id}),
                "message": json.dumps({"attachment": {"type": "image", "payload": {}}})
            }
            res = requests.post(GRAPH_URL, params={"access_token": PAGE_ACCESS_TOKEN}, data=payload, files=files, timeout=60)
        return res.status_code < 400
    except Exception:
        traceback.print_exc()
        return False

def get_facebook_name(sender_id):
    if not PAGE_ACCESS_TOKEN:
        return None
    try:
        url = f"https://graph.facebook.com/{sender_id}"
        res = requests.get(url, params={"fields": "first_name,last_name", "access_token": PAGE_ACCESS_TOKEN}, timeout=15)
        if res.status_code != 200: return None
        data = res.json()
        name = f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()
        return name or None
    except Exception:
        return None

def download_file(url, output_dir, fallback_filename):
    try:
        res = requests.get(url, timeout=120, stream=True)
        if res.status_code != 200:
            return None, None

        filename = None
        cd = res.headers.get("Content-Disposition", "")
        if "filename=" in cd:
            matches = re.findall(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
            if matches:
                filename = matches[0].strip()

        if not filename:
            content_type = res.headers.get("Content-Type", "").split(";")[0].strip()
            ext = mimetypes.guess_extension(content_type)
            if ext == ".ps": ext = ".pdf"
            
            orig_ext = os.path.splitext(fallback_filename)[1]
            if orig_ext and orig_ext.lower() in ALLOWED_EXTENSIONS:
                ext = orig_ext
            elif not ext:
                ext = ".pptx"
                
            base_name = os.path.splitext(fallback_filename)[0] or "file"
            filename = f"{base_name}{ext}"

        filename = safe_filename(filename)
        input_path = os.path.join(output_dir, filename)

        total = 0
        with open(input_path, "wb") as f:
            for chunk in res.iter_content(chunk_size=1024 * 1024):
                if not chunk: continue
                total += len(chunk)
                if total > MAX_DOWNLOAD_SIZE:
                    f.close()
                    try: os.remove(input_path)
                    except Exception: pass
                    return None, None
                f.write(chunk)

        return input_path, filename
    except Exception:
        traceback.print_exc()
        return None, None

def convert_to_pdf_if_needed(input_path, output_dir, extension):
    if extension in (".ppt", ".pptx", ".potx"):
        lo_profile_dir = tempfile.mkdtemp(prefix="lo_prof_")
        try:
            res = subprocess.run(
                [
                    "libreoffice",
                    f"-env:UserInstallation=file://{lo_profile_dir}",
                    "--headless",
                    "--convert-to", "pdf",
                    input_path,
                    "--outdir", output_dir
                ],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180
            )
            if res.returncode == 0:
                pdfs = [os.path.join(output_dir, f) for f in os.listdir(output_dir) if f.endswith(".pdf")]
                if pdfs:
                    return pdfs[0]
        except Exception:
            traceback.print_exc()
        finally:
            shutil.rmtree(lo_profile_dir, ignore_errors=True)
        return None
    return input_path

# ============================================================
# GEMINI GENERATION
# ============================================================

def extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"```$", "", text).strip()
    
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try: return json.loads(text[start:end + 1])
        except Exception: pass

    start, end = text.find("["), text.rfind("]")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])

    raise ValueError("Gemini did not return valid JSON.")

def generate_mcqs_from_file(file_path, count):
    prompt = f"""
أنت خبير طبي وخبير في إنشاء اختبارات MCQs.
اقرأ الملف المرفق فقط وأنشئ {count} سؤال اختيار من متعدد.
الشروط:
- الأسئلة باللغة الإنجليزية، وأربعة خيارات فقط: A, B, C, D.
- الإجابة الصحيحة من محتوى الملف فقط.
- explanation باللغة العربية مع إبقاء المصطلحات الطبية بالإنجليزية.
- أرجع JSON فقط بهذا الشكل:
{{
  "title": "Automatic quiz title",
  "questions": [
    {{
      "question": "Question?",
      "options": {{"A": "Opt A", "B": "Opt B", "C": "Opt C", "D": "Opt D"}},
      "answer": "A",
      "explanation": "شرح عربي مختصر."
    }}
  ]
}}
لا تكتب أي شيء خارج JSON.
"""
    last_error = None
    for _ in range(max(1, len(clients))):
        try:
            client = get_next_client()
            uploaded = client.files.upload(file=file_path)
            try:
                for model in MODELS_TO_TRY:
                    try:
                        response = client.models.generate_content(
                            model=model,
                            contents=[uploaded, prompt],
                            config=types.GenerateContentConfig(temperature=0.2)
                        )
                        data = extract_json(response.text)
                        title = data.get("title", "اختبار جديد").strip()
                        questions = data.get("questions", [])
                        if questions:
                            return {"title": title, "questions": questions}
                    except Exception as exc:
                        last_error = exc
                        continue
            finally:
                try: client.files.delete(name=uploaded.name)
                except Exception: pass
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"MCQ generation failed: {last_error}")

def generate_mcqs_from_mistakes(mistakes, count=10):
    prompt = f"""
أنشئ {count} سؤال MCQ جديد باللغة الإنجليزية يركز على نقاط الضعف هذه:
{json.dumps(mistakes, ensure_ascii=False, indent=2)}
الشروط: أربعة خيارات A,B,C,D، أسئلة جديدة، explanation باللغة العربية، أرجع JSON فقط.
الصيغة:
{{
  "title": "MCQs for mistakes",
  "questions": [
    {{
      "question": "...",
      "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}},
      "answer": "A",
      "explanation": "..."
    }}
  ]
}}
"""
    client = get_next_client()
    last_error = None
    for model in MODELS_TO_TRY:
        try:
            res = client.models.generate_content(
                model=model, contents=prompt, config=types.GenerateContentConfig(temperature=0.3)
            )
            data = extract_json(res.text)
            if data.get("questions"):
                return data
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Could not generate mistake MCQs: {last_error}")

# ============================================================
# FILE HANDLING & CONVERSION
# ============================================================

def handle_new_file(sender_id, attachment_url, fallback_filename):
    cleanup_session(sender_id)

    output_dir = os.path.join(tempfile.gettempdir(), "messenger_bot", str(uuid.uuid4()))
    os.makedirs(output_dir, exist_ok=True)

    input_path, filename = download_file(attachment_url, output_dir, fallback_filename)

    if not input_path or not os.path.exists(input_path):
        cleanup_directory(output_dir)
        send_message(sender_id, "❌ تعذر تحميل الملف.")
        return

    extension = os.path.splitext(filename)[1].lower()

    if extension not in ALLOWED_EXTENSIONS:
        cleanup_directory(output_dir)
        send_message(
            sender_id,
            f"❌ نوع الملف ({extension}) غير مدعوم.\n\nالصيغ المدعومة:\n▪ PDF\n▪️ PPT / PPTX / POTX"
        )
        return

    pdf_path = convert_to_pdf_if_needed(input_path, output_dir, extension)
    if not pdf_path or not os.path.exists(pdf_path):
        cleanup_directory(output_dir)
        send_message(sender_id, "❌ تعذر معالجة أو تحويل الملف إلى PDF.")
        return

    total_pages = get_page_count(pdf_path)

    with session_lock:
        user_sessions[sender_id] = {
            "state": "MENU",
            "output_dir": output_dir,
            "input_path": input_path,
            "pdf_path": pdf_path,
            "file_path_for_gemini": pdf_path,
            "filename": filename,
            "extension": extension,
            "total_pages": total_pages,
            "image_paths": [],
            "current_index": 0
        }

    pages_str = f"\n📊 عدد الصفحات: {total_pages}" if total_pages > 0 else ""

    send_quick_replies(
        sender_id,
        f"📚 تم استلام الملف بنجاح.\n\n📄 الملف: {filename}{pages_str}\n\nاختر العملية:",
        ["📷 تحويل إلى صور", "📖 الشرح", "❓ صنع أسئلة MCQs"]
    )

def convert_to_images_on_demand(sender_id):
    with session_lock:
        session = user_sessions.get(sender_id)

    if not session:
        return False

    pdf_path = session.get("pdf_path")
    output_dir = session.get("output_dir")

    if not pdf_path or not os.path.exists(pdf_path):
        return False

    try:
        pages = convert_from_path(pdf_path, dpi=100)
        image_paths = []
        for index, page in enumerate(pages, start=1):
            image_path = os.path.join(output_dir, f"page_{index}.jpg")
            page.save(image_path, "JPEG", quality=80)
            image_paths.append(image_path)

        with session_lock:
            if sender_id in user_sessions:
                user_sessions[sender_id]["image_paths"] = image_paths
                user_sessions[sender_id]["total_pages"] = len(image_paths)

        return True
    except Exception:
        traceback.print_exc()
        return False

def send_next_batch(sender_id):
    with session_lock:
        session = user_sessions.get(sender_id)

    if not session:
        return

    image_paths = session.get("image_paths", [])
    current_index = session.get("current_index", 0)
    total_pages = len(image_paths)

    if current_index >= total_pages:
        send_message(sender_id, "✅ تم إرسال جميع الصفحات.")
        cleanup_session(sender_id)
        return

    start = current_index
    end = min(start + PAGE_SIZE, total_pages)

    for img_p in image_paths[start:end]:
        send_image_file(sender_id, img_p)
        time.sleep(0.25)

    with session_lock:
        if sender_id in user_sessions:
            user_sessions[sender_id]["current_index"] = end

    if end >= total_pages:
        send_quick_replies(sender_id, f"✅ تم إرسال الصفحات من {start + 1} إلى {end} من أصل {total_pages}.\n\nتم الانتهاء.", ["الخروج"])
    else:
        send_quick_replies(sender_id, f"📌 تم إرسال الصفحات من {start + 1} إلى {end} من أصل {total_pages}.", ["التالي", "إنهاء"])

# ============================================================
# QUIZ LOGIC
# ============================================================

def quiz_preview(sender_id, quiz_data):
    title = quiz_data.get("title", "اختبار جديد")
    questions = quiz_data.get("questions", [])
    send_quick_replies(
        sender_id,
        f"📝 تم إنشاء الاختبار.\n\n📌 الاسم: {title}\n❓ عدد الأسئلة: {len(questions)}",
        ["تغيير الاسم", "حفظ", "بدء الاختبار", "حذف الاختبار"]
    )

def show_question(sender_id, attempt):
    quiz = database.get_quiz(attempt["quiz_id"], sender_id)
    if not quiz:
        send_message(sender_id, "❌ الاختبار غير موجود.")
        return

    questions = quiz["questions"]
    index = attempt["current_index"]

    if index >= len(questions):
        finish_quiz(sender_id, attempt)
        return

    q = questions[index]
    text = f"📌 السؤال ({index + 1} من {len(questions)}):\n\n{q.get('question', '')}\n\n🌀 A. {q['options']['A']}\n🌀 B. {q['options']['B']}\n🌀 C. {q['options']['C']}\n🌀 D. {q['options']['D']}"
    send_quick_replies(sender_id, text, ["A", "B", "C", "D", "لا أعرف", "إيقاف"])

def build_mistakes(quiz, attempt):
    questions = quiz["questions"]
    answers = attempt.get("answers", [])
    mistakes = []

    for item in answers:
        idx = item.get("index")
        if idx is None or item.get("result") not in ("wrong", "unanswered") or idx >= len(questions):
            continue
        q = questions[idx]
        mistakes.append({
            "question": q.get("question", ""),
            "options": q.get("options", {}),
            "correct_answer": q.get("answer", ""),
            "student_answer": item.get("answer", "لا أعرف"),
            "explanation": q.get("explanation", "")
        })
    return mistakes

def finish_quiz(sender_id, attempt):
    quiz = database.get_quiz(attempt["quiz_id"], sender_id)
    if not quiz:
        return
    database.complete_attempt(attempt["id"], sender_id)
    send_quick_replies(
        sender_id,
        f"🏁 انتهى الاختبار.\n\n📌 الاختبار: {quiz['title']}\n📊 النتيجة:\n▪️ الصحيح: {attempt['correct_count']}\n▪️ الخطأ: {attempt['wrong_count']}\n▪️ بدون إجابة: {attempt['unanswered_count']}\n▪️ المجموع: {len(quiz['questions'])}",
        ["Make prompt", "Make MCQs for أخطائي", "إعادة الاختبار", "الخروج"]
    )

def stop_quiz(sender_id, attempt):
    database.stop_attempt(attempt["id"], sender_id)
    send_quick_replies(
        sender_id,
        f"⏸️ تم إيقاف الاختبار.\n\n▪️ الصحيح: {attempt['correct_count']}\n▪️ الخطأ: {attempt['wrong_count']}\n▪️ بدون إجابة: {attempt['unanswered_count']}",
        ["مواصلة الاختبار", "Make MCQs for أخطائي", "إعادة الاختبار", "الخروج"]
    )

def start_quiz(sender_id, quiz_id):
    attempt_id = database.create_attempt(sender_id, quiz_id)
    attempt = database.get_attempt(attempt_id, sender_id)
    show_question(sender_id, attempt)

def resume_quiz(sender_id, attempt):
    database.update_attempt(
        attempt["id"], sender_id, attempt["current_index"],
        attempt["correct_count"], attempt["wrong_count"],
        attempt["unanswered_count"], attempt["answers"], "active"
    )
    show_question(sender_id, database.get_attempt(attempt["id"], sender_id))
def answer_question(sender_id, choice):
    attempt = database.get_active_attempt(sender_id)
    if not attempt:
        send_message(sender_id, "لا يوجد اختبار نشط.")
        return

    quiz = database.get_quiz(attempt["quiz_id"], sender_id)
    if not quiz: return

    questions = quiz["questions"]
    index = attempt["current_index"]

    if index >= len(questions):
        finish_quiz(sender_id, attempt)
        return

    q = questions[index]
    correct = str(q.get("answer", "")).upper()
    choice = choice.upper()

    if choice == "لا أعرف":
        result = "unanswered"
        attempt["unanswered_count"] += 1
        explanation = f"⚠️ لم تقم بالإجابة.\nالجواب الصحيح هو: ({correct})\n\n💡 {q.get('explanation', '')}"
    elif choice == correct:
        result = "correct"
        attempt["correct_count"] += 1
        explanation = f"✅ إجابة صحيحة.\n\n{q.get('explanation', '')}"
    else:
        result = "wrong"
        attempt["wrong_count"] += 1
        explanation = f"❌ إجابة خاطئة.\nالجواب الصحيح هو: ({correct})\n\n💡 {q.get('explanation', '')}"

    attempt["answers"].append({"index": index, "answer": choice, "correct_answer": correct, "result": result})
    attempt["current_index"] += 1

    database.update_attempt(
        attempt["id"], sender_id, attempt["current_index"],
        attempt["correct_count"], attempt["wrong_count"],
        attempt["unanswered_count"], attempt["answers"], "active"
    )

    send_message(sender_id, explanation)
    updated = database.get_attempt(attempt["id"], sender_id)

    if updated["current_index"] >= len(questions):
        finish_quiz(sender_id, updated)
    else:
        time.sleep(0.4)
        show_question(sender_id, updated)

# ============================================================
# MISTAKES & WALLET
# ============================================================

def make_mistakes_prompt(sender_id):
    attempt = database.get_active_attempt(sender_id)
    if not attempt:
        conn = database.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, quiz_id, current_index, correct_count, wrong_count, unanswered_count, answers_json
                    FROM quiz_attempts WHERE sender_id = %s AND status IN ('completed', 'stopped')
                    ORDER BY updated_at DESC LIMIT 1
                """, (sender_id,))
                row = cur.fetchone()
                if not row:
                    send_message(sender_id, "❌ لا توجد نتيجة اختبار.")
                    return
                attempt = {"id": row[0], "quiz_id": row[1], "current_index": row[2], "correct_count": row[3], "wrong_count": row[4], "unanswered_count": row[5], "answers": json.loads(row[6] or "[]")}
        finally:
            conn.close()

    quiz = database.get_quiz(attempt["quiz_id"], sender_id)
    if not quiz: return

    mistakes = build_mistakes(quiz, attempt)
    if not mistakes:
        send_message(sender_id, "🎉 لا توجد أخطاء في هذا الاختبار.")
        return

    lines = ["I want to study my mistakes from this MCQ quiz.\n", "MY MISTAKES:\n"]
    for num, m in enumerate(mistakes, start=1):
        lines.append(f"{num}. {m['question']}\nOptions: {m['options']}\nMy answer: {m['student_answer']}\nCorrect answer: {m['correct_answer']}\nExplanation: {m['explanation']}\n")

    send_message(sender_id, "\n".join(lines))

def create_mistake_mcqs(sender_id):
    conn = database.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, quiz_id, current_index, correct_count, wrong_count, unanswered_count, answers_json
                FROM quiz_attempts WHERE sender_id = %s AND status IN ('completed', 'stopped')
                ORDER BY updated_at DESC LIMIT 1
            """, (sender_id,))
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        send_message(sender_id, "❌ لا توجد نتيجة اختبار سابقة.")
        return

    attempt = {"id": row[0], "quiz_id": row[1], "answers": json.loads(row[6] or "[]")}
    parent_quiz_id = attempt["quiz_id"]
    quiz = database.get_quiz(parent_quiz_id, sender_id)
    if not quiz: return

    mistakes = build_mistakes(quiz, attempt)
    if not mistakes:
        send_message(sender_id, "🎉 لا توجد أخطاء لإنشاء أسئلة جديدة منها.")
        return

    send_message(sender_id, "⏳ جارٍ إنشاء MCQs جديدة تركز على الأخطاء...")
    try:
        generated = generate_mcqs_from_mistakes(mistakes, 10)
        title, questions = generated.get("title", "MCQs for أخطائي"), generated.get("questions", [])
        if not questions: raise RuntimeError("No questions generated.")

        with session_lock:
            user_sessions[sender_id] = {
                "state": "QUIZ_DRAFT",
                "draft_quiz": {"title": title, "questions": questions},
                "source_filename": "Mistakes",
                "parent_quiz_id": parent_quiz_id
            }
        quiz_preview(sender_id, {"title": title, "questions": questions})
    except Exception:
        traceback.print_exc()
        send_message(sender_id, "❌ تعذر إنشاء MCQs من الأخطاء.")

def show_wallet(sender_id, page=0):
    quizzes = database.get_user_quizzes(sender_id)
    if not quizzes:
        send_quick_replies(sender_id, "📚 محفظة الاختبارات فارغة.", ["رجوع", "الخروج"])
        return

    start, end = page * 10, min((page + 1) * 10, len(quizzes))
    selected = quizzes[start:end]
    lines, options = ["📚 اختبارات المحفظة\nاختر رقم الاختبار:"], []

    for i, q in enumerate(selected, start=1):
        lines.append(f"▪ {i}. {q['title']}")
        options.append(str(i))

    if end < len(quizzes): options.append("التالي")
    options.extend(["رجوع", "الخروج"])

    with session_lock:
        session = user_sessions.get(sender_id, {})
        session.update({"state": "WALLET_MCQS", "wallet_page": page, "wallet_items": selected})
        user_sessions[sender_id] = session

    send_quick_replies(sender_id, "\n".join(lines), options[:13])

def show_explanations_wallet(sender_id, page=0):
    exps = database.get_user_explanations(sender_id)
    if not exps:
        send_quick_replies(sender_id, "📚 الشروحات المحفوظة فارغة.", ["رجوع", "الخروج"])
        return

    start, end = page * 10, min((page + 1) * 10, len(exps))
    selected = exps[start:end]
    lines, options = ["📚 الشروحات المحفوظة\nاختر رقم الشرح:"], []

    for i, exp in enumerate(selected, start=1):
        lines.append(f"▪ {i}. {exp['title']}")
        options.append(str(i))

    if end < len(exps): options.append("التالي")
    options.extend(["رجوع", "الخروج"])

    with session_lock:
        session = user_sessions.get(sender_id, {})
        session.update({"state": "WAITING_EXPLANATION_SELECTION", "explanation_page": page, "explanation_items": selected})
        user_sessions[sender_id] = session

    send_quick_replies(sender_id, "\n".join(lines), options[:13])
# ============================================================
# TEXT HANDLER
# ============================================================
def handle_text(sender_id, text):
    text = (text or "").strip()
    if not text: return

    if text in ("المحفظة", "wallet"):
        with session_lock:
            user_sessions[sender_id] = user_sessions.get(sender_id, {})
            user_sessions[sender_id]["state"] = "WALLET_CHOICE"
        send_quick_replies(sender_id, "📚 اختر القسم المطلوب في المحفظة:", ["MCQs", "الشرح", "الخروج"])
        return

    if text == "الخروج":
        cleanup_session(sender_id)
        send_message(sender_id, "👋 تم الخروج.")
        return

    with session_lock:
        session = user_sessions.get(sender_id)

    if session and session.get("state") == "WALLET_CHOICE":
        if text == "MCQs":
            show_wallet(sender_id, 0)
            return
        if text == "الشرح":
            show_explanations_wallet(sender_id, 0)
            return
        if text == "رجوع":
            send_message(sender_id, "👋 تم العودة.")
            return
        if text == "الخروج":
            cleanup_session(sender_id)
            send_message(sender_id, "👋 تم الخروج.")
            return
        send_quick_replies(sender_id, "الرجاء اختيار أحد الأزرار أدناه:", ["MCQs", "الشرح", "الخروج"])
        return

    if session and session.get("state") == "WAITING_EXPLANATION_SELECTION":
        if text == "التالي":
            show_explanations_wallet(sender_id, session.get("explanation_page", 0) + 1)
            return
        if text == "رجوع":
            with session_lock:
                user_sessions[sender_id]["state"] = "WALLET_CHOICE"
            send_quick_replies(sender_id, "📚 اختر القسم المطلوب في المحفظة:", ["MCQs", "الشرح", "الخروج"])
            return
        if text == "الخروج":
            cleanup_session(sender_id)
            send_message(sender_id, "👋 تم الخروج.")
            return
        if text.isdigit():
            num = int(text)
            items = session.get("explanation_items", [])
            if 1 <= num <= len(items):
                exp_id = items[num - 1]["id"]
                exp = database.get_explanation(exp_id, sender_id)
                if not exp:
                    send_message(sender_id, "❌ الشرح غير موجود.")
                    return
                with session_lock:
                    user_sessions[sender_id] = {
                        "state": "EXPLANATION_SELECTED",
                        "selected_explanation_id": exp_id,
                        "explanation_data": exp,
                        "explanation_page": session.get("explanation_page", 0)
                    }
                info_text = build_explanation_info_text(exp)
                send_quick_replies(sender_id, info_text, ["الملاحظات", "رسائل الشرح", "رجوع", "الخروج"])
            else:
                send_message(sender_id, "❌ الرقم غير صحيح.")
            return

    if session and session.get("state") == "EXPLANATION_SELECTED":
        exp_id = session.get("selected_explanation_id")
        exp = database.get_explanation(exp_id, sender_id)
        if not exp:
            send_message(sender_id, "❌ الشرح غير موجود.")
            return

        if text == "رجوع":
            show_explanations_wallet(sender_id, session.get("explanation_page", 0))
            return
        if text == "الخروج":
            cleanup_session(sender_id)
            send_message(sender_id, "👋 تم الخروج.")
            return

        if text == "رسائل الشرح":
            messages = exp.get("messages", [])
            if not messages:
                send_message(sender_id, "لا توجد رسائل محفوظة لهذا الشرح.")
            else:
                for msg in messages:
                    send_message(sender_id, msg)
                    time.sleep(3)
            
            with session_lock:
                user_sessions[sender_id]["state"] = "EXPLANATION_READING_MENU"
            send_quick_replies(
                sender_id,
                "📌 ما الخطوة التالية؟",
                ["أكتمال القراءة", "القراءة حتى", "ملاحظة", "رجوع", "الخروج"]
            )
            return

        if text in ("الملاحظات", "ملاحظة"):
            note = exp.get("note")
            if not note:
                with session_lock:
                    user_sessions[sender_id]["state"] = "WAITING_EXPLANATION_NOTE"
                send_message(sender_id, "الرجاء كتابة الملاحظة التي استخرجتها من الشرح:")
            else:
                with session_lock:
                    user_sessions[sender_id]["state"] = "EXPLANATION_NOTE_VIEWED"
                send_quick_replies(
                    sender_id,
                    f"📝 الملاحظة المحفوظة:\n{note}",
                    ["تعديل الملاحظة", "رجوع", "الخروج"]
                )
            return

    if session and session.get("state") == "EXPLANATION_READING_MENU":
        exp_id = session.get("selected_explanation_id")
        
        if text == "رجوع":
            exp = database.get_explanation(exp_id, sender_id)
            with session_lock:
                user_sessions[sender_id]["state"] = "EXPLANATION_SELECTED"
            info_text = build_explanation_info_text(exp)
            send_quick_replies(sender_id, info_text, ["الملاحظات", "رسائل الشرح", "رجوع", "الخروج"])
            return
        if text == "الخروج":
            cleanup_session(sender_id)
            send_message(sender_id, "👋 تم الخروج.")
            return

        if text == "أكتمال القراءة":
            time_str = format_arabic_datetime()
            database.add_explanation_completion(exp_id, sender_id, time_str)
            exp = database.get_explanation(exp_id, sender_id)
            with session_lock:
                user_sessions[sender_id]["state"] = "EXPLANATION_SELECTED"
            info_text = build_explanation_info_text(exp)
            send_quick_replies(sender_id, info_text, ["الملاحظات", "رسائل الشرح", "رجوع", "الخروج"])
            return

        if text == "القراءة حتى":
            with session_lock:
                user_sessions[sender_id]["state"] = "WAITING_EXPLANATION_PAGE"
            send_message(sender_id, "الرجاء إرسال رقم الصفحة التي توقفت فيها:")
            return

        if text in ("ملاحظة", "الملاحظات"):
            exp = database.get_explanation(exp_id, sender_id)
            note = exp.get("note")
            if not note:
                with session_lock:
                    user_sessions[sender_id]["state"] = "WAITING_EXPLANATION_NOTE"
                send_message(sender_id, "الرجاء كتابة الملاحظة التي استخرجتها من الشرح:")
            else:
                with session_lock:
                    user_sessions[sender_id]["state"] = "EXPLANATION_NOTE_VIEWED"
                send_quick_replies(
                    sender_id,
                    f"📝 الملاحظة المحفوظة:\n{note}",
                    ["تعديل الملاحظة", "رجوع", "الخروج"]
                )
            return

    if session and session.get("state") == "WAITING_EXPLANATION_PAGE":
        exp_id = session.get("selected_explanation_id")
        if not text.isdigit():
            send_message(sender_id, "❌ الرجاء إرسال رقم صحيح للصفحة.")
            return
        page_num = int(text)
        time_str = format_arabic_datetime()
        database.update_explanation_page(exp_id, sender_id, page_num, time_str)
        exp = database.get_explanation(exp_id, sender_id)
        with session_lock:
            user_sessions[sender_id]["state"] = "EXPLANATION_SELECTED"
        info_text = build_explanation_info_text(exp)
        send_quick_replies(sender_id, info_text, ["الملاحظات", "رسائل الشرح", "رجوع", "الخروج"])
        return

    if session and session.get("state") == "WAITING_EXPLANATION_NOTE":
        exp_id = session.get("selected_explanation_id")
        database.update_explanation_note(exp_id, sender_id, text)
        exp = database.get_explanation(exp_id, sender_id)
        with session_lock:
            user_sessions[sender_id]["state"] = "EXPLANATION_SELECTED"
        send_message(sender_id, "✅ تم حفظ الملاحظة بنجاح.")
        info_text = build_explanation_info_text(exp)
        send_quick_replies(sender_id, info_text, ["الملاحظات", "رسائل الشرح", "رجوع", "الخروج"])
        return

    if session and session.get("state") == "EXPLANATION_NOTE_VIEWED":
        exp_id = session.get("selected_explanation_id")
        if text == "تعديل الملاحظة":
            with session_lock:
                user_sessions[sender_id]["state"] = "WAITING_EDIT_EXPLANATION_NOTE"
            exp = database.get_explanation(exp_id, sender_id)
            send_message(sender_id, f"الرجاء إرسال الملاحظة المعدلة (يمكنك نسخ القديمة وتعديلها وإرسالها كاملاً):\n\n{exp.get('note', '')}")
            return
        if text == "رجوع":
            exp = database.get_explanation(exp_id, sender_id)
            with session_lock:
                user_sessions[sender_id]["state"] = "EXPLANATION_SELECTED"
            info_text = build_explanation_info_text(exp)
            send_quick_replies(sender_id, info_text, ["الملاحظات", "رسائل الشرح", "رجوع", "الخروج"])
            return
        if text == "الخروج":
            cleanup_session(sender_id)
            send_message(sender_id, "👋 تم الخروج.")
            return

    if session and session.get("state") == "WAITING_EDIT_EXPLANATION_NOTE":
        exp_id = session.get("selected_explanation_id")
        database.update_explanation_note(exp_id, sender_id, text)
        exp = database.get_explanation(exp_id, sender_id)
        with session_lock:
            user_sessions[sender_id]["state"] = "EXPLANATION_SELECTED"
        send_message(sender_id, "✅ تم تعديل الملاحظة بنجاح.")
        info_text = build_explanation_info_text(exp)
        send_quick_replies(sender_id, info_text, ["الملاحظات", "رسائل الشرح", "رجوع", "الخروج"])
        return

    if session and session.get("state") == "IMAGE_SESSION":
        if text == "التالي": send_next_batch(sender_id)
        elif text == "إنهاء":
            cleanup_session(sender_id)
            send_message(sender_id, "✅ تم إنهاء إرسال الصور.")
        return

    if session and session.get("state") == "WALLET_MCQS":
        if text == "التالي":
            show_wallet(sender_id, session.get("wallet_page", 0) + 1)
            return
        if text == "رجوع":
            with session_lock:
                user_sessions[sender_id]["state"] = "WALLET_CHOICE"
            send_quick_replies(sender_id, "📚 اختر القسم المطلوب في المحفظة:", ["MCQs", "الشرح", "الخروج"])
            return
        if text == "الخروج":
            cleanup_session(sender_id)
            send_message(sender_id, "👋 تم الخروج.")
            return
        if text.isdigit():
            num = int(text)
            items = session.get("wallet_items", [])
            if 1 <= num <= len(items):
                quiz_id = items[num - 1]["id"]
                quiz_title = items[num - 1]["title"]
                
                mistake_quiz = database.get_mistake_quiz_by_parent(quiz_id, sender_id)
                options = ["بدء جديد", "إستئناف", "الأخطاء"]
                if mistake_quiz:
                    options.append("أسئلة الأخطاء")
                options.extend(["حذف الاختبار", "رجوع", "الخروج"])

                with session_lock:
                    user_sessions[sender_id] = {
                        "state": "WALLET_SELECTED_MCQ",
                        "selected_quiz_id": quiz_id,
                        "mistake_quiz_id": mistake_quiz["id"] if mistake_quiz else None,
                        "wallet_page": session.get("wallet_page", 0),
                        "wallet_items": items
                    }
                send_quick_replies(
                    sender_id,
                    f"📌 {quiz_title}\n\nاختر العملية:",
                    options[:13]
                )
            else:
                send_message(sender_id, "❌ الرقم غير صحيح.")
            return

    if session and session.get("state") in ("WALLET_SELECTED", "WALLET_SELECTED_MCQ"):
        quiz_id = session.get("selected_quiz_id")
        if text in ("بدء جديد", "بدء الاختبار"):
            start_quiz(sender_id, quiz_id)
            return
        if text == "إستئناف":
            attempt = database.get_quiz_latest_attempt(sender_id, quiz_id)
            if not attempt:
                start_quiz(sender_id, quiz_id)
            else:
                database.update_attempt(
                    attempt["id"], sender_id, attempt["current_index"],
                    attempt["correct_count"], attempt["wrong_count"],
                    attempt["unanswered_count"], attempt["answers"], "active"
                )
                show_question(sender_id, database.get_attempt(attempt["id"], sender_id))
            return
        if text == "الأخطاء":
            attempt = database.get_quiz_latest_attempt(sender_id, quiz_id)
            if not attempt:
                send_message(sender_id, "❌ لا توجد محاولات سابقة لهذا الاختبار.")
                return
            quiz = database.get_quiz(quiz_id, sender_id)
            if not quiz:
                send_message(sender_id, "❌ الاختبار غير موجود.")
                return
            mistakes = build_mistakes(quiz, attempt)
            
            details_msg = (
                f"📊 تفاصيل آخر محاولة:\n"
                f"▪️ تاريخ آخر محاولة: {attempt.get('updated_at', 'غير محدد')}\n"
                f"▪️ آخر سؤال تم الوصول إليه: رقم {attempt.get('current_index', 0)} من {len(quiz['questions'])}\n"
                f"▪️ عدد الإجابات الصحيحة: {attempt.get('correct_count', 0)}\n"
                f"▪️ عدد الإجابات الخاطئة: {attempt.get('wrong_count', 0)}\n"
                f"▪️ بدون إجابة: {attempt.get('unanswered_count', 0)}"
            )
            send_message(sender_id, details_msg)
            time.sleep(0.4)

            if not mistakes:
                send_message(sender_id, "🎉 لا توجد أخطاء في هذا الاختبار.")
                send_quick_replies(sender_id, "📌 ما الخطوة التالية؟", ["رجوع", "الخروج"])
                return
            
            lines = ["I want to study my mistakes from this MCQ quiz.\n", "MY MISTAKES:\n"]
            for num, m in enumerate(mistakes, start=1):
                lines.append(f"{num}. {m['question']}\nOptions: {m['options']}\nMy answer: {m['student_answer']}\nCorrect answer: {m['correct_answer']}\nExplanation: {m['explanation']}\n")
            
            send_message(sender_id, "\n".join(lines))
            time.sleep(0.5)
            send_quick_replies(sender_id, "📌 ما الخطوة التالية؟", ["Make MCQs for أخطائي", "رجوع", "الخروج"])
            return
        if text == "أسئلة الأخطاء":
            m_quiz_id = session.get("mistake_quiz_id")
            if not m_quiz_id:
                send_message(sender_id, "❌ لا توجد أسئلة أخطاء منشأة لهذا الاختبار.")
                return
            start_quiz(sender_id, m_quiz_id)
            return
        if text == "حذف الاختبار":
            database.delete_quiz(quiz_id, sender_id)
            cleanup_session(sender_id)
            send_message(sender_id, "🗑️ تم حذف الاختبار.")
            return
        if text == "رجوع":
            show_wallet(sender_id, session.get("wallet_page", 0))
            return

    if text == "مواصلة الاختبار":
        attempt = database.get_active_attempt(sender_id)
        if not attempt:
            conn = database.get_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM quiz_attempts WHERE sender_id = %s AND status = 'stopped' ORDER BY updated_at DESC LIMIT 1", (sender_id,))
                    row = cur.fetchone()
            finally: conn.close()
            if row: resume_quiz(sender_id, database.get_attempt(row[0], sender_id))
            else: send_message(sender_id, "❌ لا يوجد اختبار يمكن مواصلته.")
        else:
            resume_quiz(sender_id, attempt)
        return

    if text in ("A", "B", "C", "D", "لا أعرف"):
        answer_question(sender_id, text)
        return

    if text == "إيقاف":
        attempt = database.get_active_attempt(sender_id)
        if attempt: stop_quiz(sender_id, attempt)
        else: send_message(sender_id, "لا يوجد اختبار نشط.")
        return

    if text == "Make prompt":
        make_mistakes_prompt(sender_id)
        return

    if text == "Make MCQs for أخطائي":
        create_mistake_mcqs(sender_id)
        return

    if text == "إعادة الاختبار":
        conn = database.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT quiz_id FROM quiz_attempts WHERE sender_id = %s ORDER BY updated_at DESC LIMIT 1", (sender_id,))
                row = cur.fetchone()
        finally: conn.close()
        if row: start_quiz(sender_id, row[0])
        else: send_message(sender_id, "❌ لا يوجد اختبار لإعادته.")
        return

    if session and session.get("state") == "QUIZ_DRAFT":
        draft = session.get("draft_quiz")
        if not draft: return
        if text == "تغيير الاسم":
            with session_lock: user_sessions[sender_id]["state"] = "WAITING_QUIZ_NAME"
            send_message(sender_id, "✏️ أرسل الاسم الجديد للاختبار.")
            return
        if text == "حفظ":
            parent_id = session.get("parent_quiz_id")
            quiz_id = database.save_quiz(sender_id, draft["title"], draft["questions"], session.get("source_filename", ""), parent_quiz_id=parent_id)
            with session_lock: user_sessions[sender_id] = {"state": "SAVED_QUIZ", "saved_quiz_id": quiz_id}
            send_quick_replies(sender_id, f"✅ تم حفظ الاختبار.\n\n📌 {draft['title']}", ["بدء الاختبار", "إنهاء"])
            return
        if text == "بدء الاختبار":
            parent_id = session.get("parent_quiz_id")
            quiz_id = database.save_quiz(sender_id, draft["title"], draft["questions"], session.get("source_filename", ""), parent_quiz_id=parent_id)
            start_quiz(sender_id, quiz_id)
            return
        if text == "حذف الاختبار":
            cleanup_session(sender_id)
            send_message(sender_id, "🗑️ تم حذف الاختبار الحالي.")
            return
    if session and session.get("state") == "WAITING_QUIZ_NAME":
        new_title = text[:100]
        with session_lock:
            user_sessions[sender_id]["draft_quiz"]["title"] = new_title
            user_sessions[sender_id]["state"] = "QUIZ_DRAFT"
            draft = user_sessions[sender_id]["draft_quiz"]
        send_message(sender_id, f"✅ تم تغيير اسم الاختبار إلى:\n📌 {new_title}")
        quiz_preview(sender_id, draft)
        return

    if session and session.get("state") == "SAVED_QUIZ":
        if text == "بدء الاختبار":
            start_quiz(sender_id, session["saved_quiz_id"])
            return
        if text == "إنهاء":
            cleanup_session(sender_id)
            send_message(sender_id, "👋 تم إنهاء العملية.")
            return

    if session and session.get("state") == "MENU":
        if text == "📷 تحويل إلى صور":
            send_message(sender_id, "⏳ جارٍ معالجة وتحويل الملف إلى صور...")
            if convert_to_images_on_demand(sender_id):
                with session_lock:
                    user_sessions[sender_id]["state"] = "IMAGE_SESSION"
                send_next_batch(sender_id)
            else:
                send_message(sender_id, "❌ حدث خطأ أثناء تحويل الملف إلى صور.")
            return

        if text == "📖 الشرح":
            file_path = session.get("file_path_for_gemini")
            if not file_path or not os.path.exists(file_path):
                send_message(sender_id, "❌ الملف لم يعد متاحاً.")
                cleanup_session(sender_id)
                return

            send_message(sender_id, "⏳ جارٍ قراءة الملف وإعداد الشرح الشامل...")
            try:
                explanation = generate_explanation_from_file(file_path)
                chunks = []
                if len(explanation) > 1900:
                    chunks = [explanation[i:i+1900] for i in range(0, len(explanation), 1900)]
                    for chunk in chunks:
                        send_message(sender_id, chunk)
                        time.sleep(0.3)
                else:
                    chunks = [explanation]
                    send_message(sender_id, explanation)

                with session_lock:
                    if sender_id in user_sessions:
                        user_sessions[sender_id]["current_explanation_messages"] = chunks
                        user_sessions[sender_id]["state"] = "EXPLANATION_READY"

                send_quick_replies(
                    sender_id,
                    "📌 ما الخطوة التالية؟",
                    ["📷 تحويل إلى صور", "❓ صنع أسئلة MCQs", "حفظ", "الخروج"]
                )
            except Exception:
                traceback.print_exc()
                with session_lock:
                    if sender_id in user_sessions:
                        user_sessions[sender_id]["state"] = "EXPLANATION_ERROR"
                send_quick_replies(
                    sender_id,
                    "❌ حدث خطأ أثناء إعداد الشرح.",
                    ["إعادة المحاولة للشرح", "رجوع", "إنهاء"]
                )
            return

        if text == "❓ صنع أسئلة MCQs":
            with session_lock:
                user_sessions[sender_id]["state"] = "WAITING_MCQ_COUNT"
            send_message(sender_id, "❓ كم سؤال MCQ تريد؟\n\nأرسل رقماً مثل:\n▪️ 10\n▪️ 20\n▪️ 30")
            return

    if session and session.get("state") == "EXPLANATION_READY":
        if text == "حفظ":
            with session_lock:
                user_sessions[sender_id]["state"] = "WAITING_EXPLANATION_SAVE_NAME"
            send_message(sender_id, "الرجاء إختيار اسم")
            return
        if text == "📷 تحويل إلى صور":
            send_message(sender_id, "⏳ جارٍ معالجة وتحويل الملف إلى صور...")
            if convert_to_images_on_demand(sender_id):
                with session_lock:
                    user_sessions[sender_id]["state"] = "IMAGE_SESSION"
                send_next_batch(sender_id)
            return
        if text == "❓ صنع أسئلة MCQs":
            with session_lock:
                user_sessions[sender_id]["state"] = "WAITING_MCQ_COUNT"
            send_message(sender_id, "❓ كم سؤال MCQ تريد؟\n\nأرسل رقماً مثل:\n▪️ 10\n▪️ 20\n▪️ 30")
            return

    if session and session.get("state") == "WAITING_EXPLANATION_SAVE_NAME":
        title = text[:100]
        messages = session.get("current_explanation_messages", ["شرح الملف"])
        database.save_explanation(sender_id, title, messages)
        with session_lock:
            user_sessions[sender_id]["state"] = "MENU"
        send_quick_replies(
            sender_id,
            "✅ تم الحفظ.\n\n📌 ما الخطوة التالية؟",
            ["📷 تحويل إلى صور", "❓ صنع أسئلة MCQs", "الخروج"]
        )
        return

    if session and session.get("state") == "WAITING_MCQ_COUNT":
        if not text.isdigit():
            send_message(sender_id, "❌ أرسل عدد الأسئلة كرقم.")
            return
        count = int(text)
        if count < 1 or count > 100:
            send_message(sender_id, "❌ العدد يجب أن يكون بين 1 و100.")
            return

        file_path = session.get("file_path_for_gemini")
        if not file_path or not os.path.exists(file_path):
            send_message(sender_id, "❌ الملف لم يعد متاحاً.")
            cleanup_session(sender_id)
            return

        with session_lock:
            if sender_id in user_sessions:
                user_sessions[sender_id]["mcq_count"] = count

        send_message(sender_id, "⏳ جارٍ إنشاء الأسئلة...")
        try:
            generated = generate_mcqs_from_file(file_path, count)
            with session_lock:
                user_sessions[sender_id] = {
                    "state": "QUIZ_DRAFT",
                    "draft_quiz": generated,
                    "source_filename": session.get("filename", ""),
                    "output_dir": session.get("output_dir"),
                    "filename": session.get("filename"),
                    "total_pages": session.get("total_pages"),
                    "file_path_for_gemini": file_path,
                    "mcq_count": count
                }
            quiz_preview(sender_id, generated)
        except Exception:
            traceback.print_exc()
            with session_lock:
                if sender_id in user_sessions:
                    user_sessions[sender_id]["state"] = "MCQ_ERROR"
            send_quick_replies(
                sender_id,
                "❌ حدث خطأ أثناء إنشاء الأسئلة.",
                ["إعادة المحاولة", "رجوع", "إنهاء"]
            )
        return

    if session and session.get("state") == "MCQ_ERROR":
        if text == "إعادة المحاولة":
            file_path = session.get("file_path_for_gemini")
            count = session.get("mcq_count", 10)
            if not file_path or not os.path.exists(file_path):
                send_message(sender_id, "❌ الملف لم يعد متاحاً.")
                cleanup_session(sender_id)
                return

            send_message(sender_id, "⏳ جارٍ إعادة المحاولة وإنشاء الأسئلة...")
            try:
                generated = generate_mcqs_from_file(file_path, count)
                with session_lock:
                    user_sessions[sender_id] = {
                        "state": "QUIZ_DRAFT",
                        "draft_quiz": generated,
                        "source_filename": session.get("filename", ""),
                        "output_dir": session.get("output_dir"),
                        "filename": session.get("filename"),
                        "total_pages": session.get("total_pages"),
                        "file_path_for_gemini": file_path,
                        "mcq_count": count
                    }
                quiz_preview(sender_id, generated)
            except Exception:
                traceback.print_exc()
                send_quick_replies(
                    sender_id,
                    "❌ حدث خطأ أثناء إنشاء الأسئلة.",
                    ["إعادة المحاولة", "رجوع", "إنهاء"]
                )
            return

        if text == "رجوع":
            with session_lock:
                if sender_id in user_sessions:
                    user_sessions[sender_id]["state"] = "MENU"
                filename = session.get("filename", "الملف")
                total_pages = session.get("total_pages", 0)
            pages_str = f"\n📊 عدد الصفحات: {total_pages}" if total_pages > 0 else ""
            send_quick_replies(
                sender_id,
                f"📄 الملف: {filename}{pages_str}\n\nاختر العملية:",
                ["📷 تحويل إلى صور", "📖 الشرح", "❓ صنع أسئلة MCQs"]
            )
            return

        if text == "إنهاء":
            cleanup_session(sender_id)
            send_message(sender_id, "👋 تم الخروج وإنهاء الملف.")
            return

        send_quick_replies(
            sender_id,
            "الرجاء اختيار أحد الأزرار أدناه:",
            ["إعادة المحاولة", "رجوع", "إنهاء"]
        )
        return

    if session and session.get("state") == "EXPLANATION_ERROR":
        if text == "إعادة المحاولة للشرح":
            file_path = session.get("file_path_for_gemini")
            if not file_path or not os.path.exists(file_path):
                send_message(sender_id, "❌ الملف لم يعد متاحاً.")
                cleanup_session(sender_id)
                return

            send_message(sender_id, "⏳ جارٍ إعادة المحاولة لإعداد الشرح...")
            try:
                explanation = generate_explanation_from_file(file_path)
                chunks = []
                if len(explanation) > 1900:
                    chunks = [explanation[i:i+1900] for i in range(0, len(explanation), 1900)]
                    for chunk in chunks:
                        send_message(sender_id, chunk)
                        time.sleep(0.3)
                else:
                    chunks = [explanation]
                    send_message(sender_id, explanation)

                with session_lock:
                    if sender_id in user_sessions:
                        user_sessions[sender_id]["current_explanation_messages"] = chunks
                        user_sessions[sender_id]["state"] = "EXPLANATION_READY"

                send_quick_replies(
                    sender_id,
                    "📌 ما الخطوة التالية؟",
                    ["📷 تحويل إلى صور", "❓ صنع أسئلة MCQs", "حفظ", "الخروج"]
                )
            except Exception:
                traceback.print_exc()
                send_quick_replies(
                    sender_id,
                    "❌ حدث خطأ أثناء إعداد الشرح.",
                    ["إعادة المحاولة للشرح", "رجوع", "إنهاء"]
                )
            return

        if text == "رجوع":
            with session_lock:
                if sender_id in user_sessions:
                    user_sessions[sender_id]["state"] = "MENU"
                filename = session.get("filename", "الملف")
                total_pages = session.get("total_pages", 0)
            pages_str = f"\n📊 عدد الصفحات: {total_pages}" if total_pages > 0 else ""
            send_quick_replies(
                sender_id,
                f"📄 الملف: {filename}{pages_str}\n\nاختر العملية:",
                ["📷 تحويل إلى صور", "📖 الشرح", "❓ صنع أسئلة MCQs"]
            )
            return

        if text == "إنهاء":
            cleanup_session(sender_id)
            send_message(sender_id, "👋 تم الخروج وإنهاء الملف.")
            return

        send_quick_replies(
            sender_id,
            "الرجاء اختيار أحد الأزرار أدناه:",
            ["إعادة المحاولة للشرح", "رجوع", "إنهاء"]
        )
        return

    send_message(sender_id, "👋 مرحباً.\n\n📌 أرسل ملف PDF أو PPT أو PPTX للبدء.\n\n📚 أو اكتب «المحفظة» للوصول إلى الاختبارات والشروحات المحفوظة.")

def generate_explanation_from_file(file_path):
    prompt = """
أنت خبير أكاديمي وطبي. اقرأ الملف المرفق جيداً وأعطني شرحاً شاملاً، منظماً، وسهل الفهم لمحتوى الملف.
الشروط:
- الشرح باللغة العربية مع إبقاء المصطلحات العلمية أو الطبية بالإنجليزية.
- نظم الشرح في نقاط وعناوين واضحة ومفيدة للمذاكرة.
- ركز على شرح المفاهيم الأساسية والأفكار الرئيسية في الملف.
"""
    last_error = None
    for _ in range(max(1, len(clients))):
        try:
            client = get_next_client()
            uploaded = client.files.upload(file=file_path)
            try:
                for model in MODELS_TO_TRY:
                    try:
                        response = client.models.generate_content(
                            model=model,
                            contents=[uploaded, prompt],
                            config=types.GenerateContentConfig(temperature=0.3)
                        )
                        if response and response.text:
                            return response.text.strip()
                    except Exception as exc:
                        last_error = exc
                        continue
            finally:
                try: client.files.delete(name=uploaded.name)
                except Exception: pass
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Explanation generation failed: {last_error}")
# ============================================================
# WEBHOOKS & ROUTING
# ============================================================

@app.route("/", methods=["GET"])
def home():
    return "Messenger Bot is running."

@app.route("/webhook", methods=["GET"])
def verify():
    if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge"), 200
    return "Verification failed", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    try:
        if data.get("object") != "page":
            return "EVENT_RECEIVED", 200

        for entry in data.get("entry", []):
            for event in entry.get("messaging", []):
                sender_id = event.get("sender", {}).get("id")
                if not sender_id: continue

                database.save_user(sender_id, get_facebook_name(sender_id))
                message = event.get("message", {})
                if not message or message.get("is_echo"): continue

                text = message.get("text")
                if text:
                    threading.Thread(target=handle_text, args=(sender_id, text), daemon=True).start()
                    continue

                attachments = message.get("attachments", [])
                for attachment in attachments:
                    if attachment.get("type") == "file" and attachment.get("payload", {}).get("url"):
                        url = attachment["payload"]["url"]
                        filename = attachment["payload"].get("filename") or attachment.get("title") or attachment["payload"].get("title") or "file.pptx"
                        threading.Thread(target=handle_new_file, args=(sender_id, url, filename), daemon=True).start()
                    else:
                        send_message(sender_id, "📎 أرسل ملف PDF أو PPT أو PPTX.")
        return "EVENT_RECEIVED", 200
    except Exception:
        traceback.print_exc()
        return "EVENT_RECEIVED", 200

if __name__ == "__main__":
    try: database.init_db()
    except Exception: traceback.print_exc()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))

