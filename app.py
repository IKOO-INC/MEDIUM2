from flask import (
    Flask, abort, jsonify, redirect, render_template, request,
    send_file, send_from_directory, session, url_for
)
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from bson.objectid import ObjectId
from werkzeug.utils import secure_filename
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, unquote
from urllib.request import Request as UrlRequest, urlopen
import json
import os
import random
import shutil
import tempfile
import uuid
from pathlib import Path
from datetime import datetime, timedelta
import requests
import cloudinary
import cloudinary.uploader
from cloudinary.exceptions import Error as CloudinaryError
from google import genai

import qrcode
from qrcode.constants import ERROR_CORRECT_M
import requests, json
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'fyy-medium-dev-secret-change-me')

# Konfigurasi Upload
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # Maksimal ukuran file 16MB
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Nama hari bahasa Indonesia
indo_days = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]

# ---------------------------------------------------------
# KONEKSI MONGODB ATLAS
# ---------------------------------------------------------
MONGO_URI = "mongodb+srv://muhammaddarjuni76_db_user:dI42gI11RtTIw7YM@fyy.uddizvu.mongodb.net/?appName=fyy"
client = MongoClient(MONGO_URI)
db = client['medium']


# ==========================================
# HELPER ANGGOTA & ABSENSI QR
# ==========================================
def ensure_member_qr_tokens():
    """Pastikan seluruh anggota berbentuk object dan memiliki qr_token unik."""
    team_data = db.settings.find_one({"_id": "team_settings"})
    raw_members = team_data.get('members_array', []) if team_data else []

    normalized_members = []
    used_tokens = set()
    changed = False

    for member in raw_members:
        if isinstance(member, dict):
            normalized = dict(member)
        else:
            normalized = {
                "name": member,
                "role": "Belum di-set"
            }
            changed = True

        normalized["name"] = str(normalized.get("name", "")).strip()
        normalized["role"] = normalized.get("role") or "Belum di-set"

        qr_token = str(normalized.get("qr_token", "")).strip()
        if not qr_token or qr_token in used_tokens:
            qr_token = uuid.uuid4().hex
            normalized["qr_token"] = qr_token
            changed = True

        used_tokens.add(qr_token)
        normalized_members.append(normalized)

        if normalized != member:
            changed = True

    if changed:
        db.settings.update_one(
            {"_id": "team_settings"},
            {"$set": {"members_array": normalized_members}},
            upsert=True
        )

    return normalized_members


def find_member_by_qr_token(qr_token):
    for member in ensure_member_qr_tokens():
        if member.get('qr_token') == qr_token:
            return member
    return None


def create_attendance_record(name, status, notes, method, qr_token=None):
    allowed_statuses = {"Hadir", "Izin", "Sakit"}
    clean_name = (name or "").strip()
    clean_status = (status or "").strip()
    clean_notes = (notes or "").strip()

    if not clean_name or clean_status not in allowed_statuses:
        return False

    record = {
        "name": clean_name,
        "status": clean_status,
        "notes": clean_notes,
        "method": method,
        "date_submitted": datetime.now()
    }
    if qr_token:
        record["qr_token"] = qr_token

    db.attendance.insert_one(record)
    return True


def get_attendance_context():
    members_list = ensure_member_qr_tokens()
    records = list(db.attendance.find().sort("date_submitted", -1))

    stats = {}
    for member in members_list:
        member_name = member.get('name')
        if not member_name:
            continue
        stats[member_name] = {
            'Hadir': 0,
            'Izin': 0,
            'Sakit': 0,
            'Wajib_Hadir': 0,
            'Role': member.get('role') or 'Belum di-set'
        }

    for record in records:
        submitted_at = record.get('date_submitted')
        if not isinstance(submitted_at, datetime):
            submitted_at = datetime.now()
            record['date_submitted'] = submitted_at

        day_index = submitted_at.weekday()
        record['hari_indo'] = indo_days[day_index]
        record['is_wajib'] = day_index in [0, 5]
        record['method'] = record.get('method') or 'manual'

        name = record.get('name')
        if record['is_wajib'] and name in stats:
            stats[name]['Wajib_Hadir'] += 1
            status = record.get('status')
            if status in stats[name]:
                stats[name][status] += 1

    return members_list, records, stats


# ==========================================
# 1. DASHBOARD & TOTAL TEAM BALANCE
# ==========================================
@app.route('/')
def index():
    incomes = list(db.finance.find({"type": "in"}))
    expenses = list(db.finance.find({"type": "out"}))

    total_in = sum(item.get('amount', 0) for item in incomes)
    total_out = sum(item.get('amount', 0) for item in expenses)
    balance = total_in - total_out

    active_tasks = list(db.tasks.find({"status": "active"}).sort("date_created", -1).limit(5))
    latest_announcements = list(
        db.announcements.find().sort('created_at', -1).limit(5)
    )

    return render_template(
        'index.html',
        balance=balance,
        total_in=total_in,
        total_out=total_out,
        tasks=active_tasks,
        announcements=latest_announcements
    )



# ==========================================
# 2. ABSENSI: REKAP, MANUAL, DAN SCAN QR
# ==========================================
@app.route('/attendance', methods=['GET', 'POST'])
def attendance():
    # Dukungan lama: POST ke /attendance tetap dianggap absen manual.
    if request.method == 'POST':
        create_attendance_record(
            request.form.get('name'),
            request.form.get('status'),
            request.form.get('notes'),
            method='manual'
        )
        return redirect(url_for('attendance'))

    members_list, records, stats = get_attendance_context()
    return render_template(
        'attendance.html',
        records=records,
        members=members_list,
        stats=stats,
        saved=request.args.get('saved')
    )


@app.route('/attendance/manual', methods=['GET', 'POST'])
def attendance_manual():
    members_list = ensure_member_qr_tokens()
    error = ""

    if request.method == 'POST':
        member_name = (request.form.get('name') or '').strip()
        valid_names = {member.get('name') for member in members_list}

        if member_name not in valid_names:
            error = "Anggota tidak ditemukan. Silakan pilih anggota dari daftar."
        elif create_attendance_record(
            member_name,
            request.form.get('status'),
            request.form.get('notes'),
            method='manual'
        ):
            return redirect(url_for('attendance', saved='manual'))
        else:
            error = "Data absensi belum lengkap atau status tidak valid."

    return render_template('attendance_manual.html', members=members_list, error=error)


@app.route('/attendance/scan')
def attendance_scan():
    return render_template('attendance_scan.html')


@app.route('/attendance/scan/<qr_token>', methods=['GET', 'POST'])
def attendance_scan_member(qr_token):
    member = find_member_by_qr_token(qr_token)
    if not member:
        abort(404)

    error = ""
    if request.method == 'POST':
        if create_attendance_record(
            member.get('name'),
            request.form.get('status'),
            request.form.get('notes'),
            method='scan',
            qr_token=qr_token
        ):
            return redirect(url_for('attendance', saved='scan'))
        error = "Status absensi tidak valid. Silakan periksa kembali."

    return render_template('attendance_scan_member.html', member=member, error=error)


# ==========================================
# FITUR: KELOLA ANGGOTA & QR
# ==========================================
@app.route('/members', methods=['GET', 'POST'])
def members():
    if request.method == 'POST':
        action = request.form.get('action')
        member_name = (request.form.get('member_name') or '').strip()
        member_role = (request.form.get('member_role') or '').strip()

        if action == 'add' and member_name and member_role:
            db.settings.update_one(
                {"_id": "team_settings"},
                {"$pull": {"members_array": {"name": member_name}}}
            )
            db.settings.update_one(
                {"_id": "team_settings"},
                {"$pull": {"members_array": member_name}}
            )
            db.settings.update_one(
                {"_id": "team_settings"},
                {"$push": {"members_array": {
                    "name": member_name,
                    "role": member_role,
                    "qr_token": uuid.uuid4().hex
                }}},
                upsert=True
            )
        elif action == 'delete' and member_name:
            db.settings.update_one(
                {"_id": "team_settings"},
                {"$pull": {"members_array": {"name": member_name}}}
            )
            db.settings.update_one(
                {"_id": "team_settings"},
                {"$pull": {"members_array": member_name}}
            )
        return redirect(url_for('members'))

    members_list = ensure_member_qr_tokens()
    return render_template('members.html', members=members_list)


@app.route('/members/qr')
def member_qr_list():
    members_list = ensure_member_qr_tokens()
    return render_template('member_qr.html', members=members_list)


@app.route('/members/qr/<qr_token>.png')
def member_qr_image(qr_token):
    member = find_member_by_qr_token(qr_token)
    if not member:
        abort(404)

    scan_url = url_for('attendance_scan_member', qr_token=qr_token, _external=True)
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=10,
        border=4
    )
    qr.add_data(scan_url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")

    image_buffer = BytesIO()
    image.save(image_buffer, format='PNG')
    image_buffer.seek(0)

    safe_name = secure_filename(member.get('name') or 'anggota') or 'anggota'
    return send_file(
        image_buffer,
        mimetype='image/png',
        as_attachment=request.args.get('download') == '1',
        download_name=f'QR-{safe_name}.png'
    )


# ==========================================
# 3. KAS MASUK KELUAR (TRANSPARAN)
# ==========================================
@app.route('/finance', methods=['GET', 'POST'])
def finance():
    if request.method == 'POST':
        db.finance.insert_one({
            "type": request.form.get('type'),
            "amount": float(request.form.get('amount')),
            "description": request.form.get('description'),
            "user": request.form.get('user'),
            "date": datetime.now()
        })
        return redirect(url_for('finance'))

    transactions = list(db.finance.find().sort("date", -1))
    return render_template('finance.html', transactions=transactions)


# ==========================================
# 4. DAFTAR PROJECT, KONTRIBUTOR & PASSED TASK
# ==========================================
def get_valid_contributor_names():
    return {
        member.get('name')
        for member in ensure_member_qr_tokens()
        if member.get('name')
    }


def normalize_contributors(raw_names):
    valid_names = get_valid_contributor_names()
    contributors = []
    for raw_name in raw_names:
        clean_name = (raw_name or '').strip()
        if clean_name in valid_names and clean_name not in contributors:
            contributors.append(clean_name)
    return contributors


@app.route('/tasks', methods=['GET', 'POST'])
def tasks():
    members_list = ensure_member_qr_tokens()
    error = ''

    if request.method == 'POST':
        title = (request.form.get('title') or '').strip()
        description = (request.form.get('description') or '').strip()
        contributors = normalize_contributors(request.form.getlist('contributors'))

        if not title or not description:
            error = 'Judul dan deskripsi task wajib diisi.'
        else:
            db.tasks.insert_one({
                "title": title,
                "description": description,
                "contributors": contributors,
                "status": "active",
                "date_created": datetime.now()
            })
            return redirect(url_for('tasks'))

    active_tasks = list(db.tasks.find({"status": "active"}).sort("date_created", -1))
    passed_tasks = list(db.tasks.find({"status": "passed"}).sort("date_created", -1))
    return render_template(
        'tasks.html',
        active_tasks=active_tasks,
        passed_tasks=passed_tasks,
        members=members_list,
        error=error
    )


@app.route('/task/contributors/<task_id>', methods=['POST'])
def update_task_contributors(task_id):
    try:
        object_id = ObjectId(task_id)
    except Exception:
        abort(404)

    contributors = normalize_contributors(request.form.getlist('contributors'))
    result = db.tasks.update_one(
        {"_id": object_id},
        {"$set": {"contributors": contributors}}
    )
    if result.matched_count == 0:
        abort(404)
    return redirect(url_for('tasks'))


@app.route('/task/complete/<task_id>')
def complete_task(task_id):
    try:
        object_id = ObjectId(task_id)
    except Exception:
        abort(404)

    result = db.tasks.update_one(
        {"_id": object_id},
        {"$set": {"status": "passed", "date_completed": datetime.now()}}
    )
    if result.matched_count == 0:
        abort(404)
    return redirect(url_for('tasks'))


# ==========================================
# 5. PAPAN PENGUMUMAN (ROUTE TERPISAH)
# ==========================================
@app.route('/announcements', methods=['GET', 'POST'])
def announcements():
    members_list = ensure_member_qr_tokens()
    error = ''

    if request.method == 'POST':
        title = (request.form.get('title') or '').strip()
        content = (request.form.get('content') or '').strip()
        author = (request.form.get('author') or 'Admin').strip() or 'Admin'
        priority = (request.form.get('priority') or 'normal').strip()
        if priority not in {'normal', 'important', 'urgent'}:
            priority = 'normal'

        if not title or not content:
            error = 'Judul dan isi pengumuman wajib diisi.'
        else:
            db.announcements.insert_one({
                'title': title,
                'content': content,
                'author': author,
                'priority': priority,
                'created_at': datetime.now()
            })
            return redirect(url_for('announcements'))

    items = list(db.announcements.find().sort('created_at', -1).limit(100))
    return render_template(
        'announcements.html',
        announcements=items,
        members=members_list,
        error=error
    )


@app.route('/announcements/delete/<announcement_id>', methods=['POST'])
def delete_announcement(announcement_id):
    try:
        object_id = ObjectId(announcement_id)
    except Exception:
        abort(404)

    result = db.announcements.delete_one({'_id': object_id})
    if result.deleted_count == 0:
        abort(404)
    return redirect(url_for('announcements'))


# ==========================================
# 6. FYY AI — CHATBOT GEMINI FLASH
# ==========================================
def get_fyy_ai_session_id():
    session_id = session.get('fyy_ai_session_id')
    if not session_id:
        session_id = uuid.uuid4().hex
        session['fyy_ai_session_id'] = session_id
    return session_id


def get_ai_log_ttl_hours():
    try:
        ttl_hours = int(os.getenv('FYY_AI_LOG_TTL_HOURS', '24'))
    except ValueError:
        ttl_hours = 24
    return max(1, min(ttl_hours, 168))


def ensure_ai_log_ttl_index():
    try:
        db.ai_chat_logs.create_index(
            [('expires_at', 1)],
            expireAfterSeconds=0,
            name='expires_at_ttl'
        )
    except PyMongoError:
        # Chat tetap dapat digunakan walaupun index TTL belum berhasil dibuat.
        pass


def store_ai_message(session_id, role, content):
    now = datetime.utcnow()
    db.ai_chat_logs.insert_one({
        'session_id': session_id,
        'role': role,
        'content': content,
        'created_at': now,
        'expires_at': now + timedelta(hours=get_ai_log_ttl_hours())
    })


def get_ai_messages(session_id, limit=40):
    messages = list(
        db.ai_chat_logs.find({'session_id': session_id})
        .sort('created_at', -1)
        .limit(limit)
    )
    messages.reverse()
    return messages


def get_gemini_api_key():
    """Ambil API key Gemini dari environment yang didukung SDK Google."""
    return (
        os.getenv('KEY')
        or os.getenv('KEY')
    )


def get_gemini_model():
    return 'gemini-3-flash-preview'


def build_gemini_history(messages):
    """Ubah log MongoDB menjadi format percakapan Gemini.

    Gemini menggunakan role `model` untuk jawaban asisten. Pesan berurutan
    dengan role sama digabung agar request tetap valid walaupun request AI
    sebelumnya gagal setelah pesan pengguna tersimpan.
    """
    contents = []

    for item in messages:
        stored_role = item.get('role', 'user')
        content = str(item.get('content', '')).strip()
        if stored_role not in {'user', 'assistant'} or not content:
            continue

        gemini_role = 'model' if stored_role == 'assistant' else 'user'
        if contents and contents[-1]['role'] == gemini_role:
            contents[-1]['parts'][0]['text'] += '\n\n' + content
        else:
            contents.append({
                'role': gemini_role,
                'parts': [{'text': content}]
            })

    return contents


def get_gemini_error_message(exc):
    """Buat pesan error yang mudah dipahami tanpa membocorkan detail API key."""
    error_text = str(exc).lower()

    if '429' in error_text or 'resource_exhausted' in error_text or 'quota' in error_text:
        return (
            'Kuota gratis Gemini sedang habis atau terlalu banyak permintaan. '
            'Tunggu beberapa saat lalu coba lagi.'
        ), 429

    if '401' in error_text or '403' in error_text or 'api key' in error_text:
        return (
            'GEMINI_API_KEY tidak valid atau belum memiliki akses ke Gemini API.'
        ), 503

    if (
        '404' in error_text
        or 'model not found' in error_text
        or ('models/' in error_text and 'not found' in error_text)
        or 'model is not supported' in error_text
    ):
        return (
            'Model Gemini tidak ditemukan. Periksa nilai GEMINI_MODEL pada environment.'
        ), 502

    return (
        'Gagal menghubungi Gemini. Periksa koneksi server dan konfigurasi API key.'
    ), 502


@app.route('/fyy-ai')
def fyy_ai():
    ensure_ai_log_ttl_index()
    session_id = get_fyy_ai_session_id()
    messages = get_ai_messages(session_id)
    return render_template(
        'fyy_ai.html',
        messages=messages,
        model=get_gemini_model(),
        api_configured=bool(get_gemini_api_key()),
        ttl_hours=get_ai_log_ttl_hours()
    )


@app.route('/fyy-ai/chat', methods=['POST'])
def fyy_ai_chat():
    ensure_ai_log_ttl_index()
    payload = request.get_json(silent=True) or request.form
    user_message = (payload.get('message') or '').strip()

    if not user_message:
        return jsonify({'ok': False, 'error': 'Pesan tidak boleh kosong.'}), 400
    if len(user_message) > 6000:
        return jsonify({'ok': False, 'error': 'Pesan terlalu panjang. Maksimal 6.000 karakter.'}), 400

    api_key = get_gemini_api_key()
    if not api_key:
        return jsonify({
            'ok': False,
            'error': 'GEMINI_API_KEY belum diatur pada environment server.'
        }), 503

    session_id = get_fyy_ai_session_id()
    store_ai_message(session_id, 'user', user_message)
    history = get_ai_messages(session_id, limit=18)
    model = get_gemini_model()

    try:
        client_ai = genai.Client(api_key=api_key)
        response = client_ai.models.generate_content(
            model=model,
            contents=build_gemini_history(history),
            config={
                'system_instruction': (
                    'Anda adalah FYY AI, asisten untuk tim media Roudlotul Ulum. '
                    'Jawab dalam bahasa Indonesia yang jelas, ramah, dan langsung membantu. '
                    'Bantu penulisan konten, ide desain, rundown, caption, administrasi tim, '
                    'serta pertanyaan umum. Pertahankan konteks percakapan yang diberikan. '
                    'Jangan mengaku telah melakukan tindakan di sistem yang sebenarnya belum dilakukan.'
                    'Kurangi jawaban point panjang 1 sampai 5, buat saja 2 point tapi secara detail, dan berisi.'
                    'Jika dimintai caption atau kata kata, buat dalam bahasa sastra indonesia yang sangat bermakna.'
                ),
                'temperature': 0.7,
                'max_output_tokens': 1200
            }
        )
        reply = (response.text or '').strip()
        if not reply:
            reply = 'Maaf, FYY AI belum menghasilkan jawaban. Silakan kirim ulang pertanyaan.'
    except Exception as exc:
        app.logger.exception('FYY AI Gemini request failed')
        error_message, status_code = get_gemini_error_message(exc)
        return jsonify({'ok': False, 'error': error_message}), status_code

    store_ai_message(session_id, 'assistant', reply)
    return jsonify({'ok': True, 'reply': reply, 'model': model})


@app.route('/fyy-ai/clear', methods=['POST'])
def clear_fyy_ai_chat():
    session_id = get_fyy_ai_session_id()
    db.ai_chat_logs.delete_many({'session_id': session_id})
    return jsonify({'ok': True})


# ==========================================
# 7. UPLOAD ASSETS / DOKUMENTASI — CLOUDINARY
# ==========================================
def get_cloudinary_config():
    """Ambil konfigurasi Cloudinary dari environment.

    Mendukung dua cara:
    1. CLOUDINARY_CLOUD_NAME + CLOUDINARY_API_KEY + CLOUDINARY_API_SECRET
    2. CLOUDINARY_URL=cloudinary://API_KEY:API_SECRET@CLOUD_NAME
    """
    cloud_name = 'dakwyt1c4'
    api_key = '263124236154547'
    api_secret = 'j4SZavaBMFs7YhEKZDWlyGoBF4E'

    folder = 'medium'
    return cloud_name, api_key, api_secret, folder


def configure_cloudinary():
    """Konfigurasi SDK Cloudinary untuk aplikasi Flask."""
    cloud_name, api_key, api_secret, folder = get_cloudinary_config()
    if not (cloud_name and api_key and api_secret):
        raise RuntimeError(
            'Cloudinary belum dikonfigurasi. Isi CLOUDINARY_CLOUD_NAME, '
            'CLOUDINARY_API_KEY, dan CLOUDINARY_API_SECRET.'
        )

    cloudinary.config(
            cloud_name='dakwyt1c4',
            api_key='263124236154547',
            api_secret='j4SZavaBMFs7YhEKZDWlyGoBF4E',
            secure=True
    )
    return folder


def cloudinary_is_configured():
    cloud_name, api_key, api_secret, _ = get_cloudinary_config()
    return bool(cloud_name and api_key and api_secret)


def upload_to_cloudinary(file_storage):
    """Upload file dari Flask/Werkzeug menggunakan Cloudinary Python SDK."""
    folder = configure_cloudinary()

    original_filename = file_storage.filename or 'asset'
    content_type = file_storage.mimetype or 'application/octet-stream'

    try:
        # SDK menangani signature/authentication di sisi server.
        result = cloudinary.uploader.upload(
            file_storage.stream,
            resource_type='auto',
            folder=folder,
            use_filename=True,
            unique_filename=True,
            overwrite=False
        )
    except CloudinaryError as exc:
        raise RuntimeError(f'Upload ke Cloudinary gagal: {exc}') from exc
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f'Upload ke Cloudinary gagal: {exc}') from exc

    return {
        'filename': original_filename,
        'description': '',
        'storage': 'cloudinary',
        'cloudinary_public_id': result.get('public_id'),
        'cloudinary_resource_type': result.get('resource_type'),
        'cloudinary_format': result.get('format'),
        'cloudinary_version': result.get('version'),
        'cloudinary_type': result.get('type'),
        'url': result.get('secure_url'),
        'bytes': result.get('bytes'),
        'content_type': content_type
    }

def get_cloudinary_asset_url(asset):
    """Ambil delivery URL tersimpan dan pastikan berasal dari Cloudinary."""
    asset_url = (asset.get('url') or '').strip()
    if not asset_url:
        return None

    parsed = urlparse(asset_url)
    hostname = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or not (
        hostname == 'res.cloudinary.com' or hostname.endswith('.cloudinary.com')
    ):
        return None
    return asset_url


def proxy_cloudinary_download(asset):
    """Unduh file dari Cloudinary melalui server lalu kirim sebagai attachment.

    Cara ini tidak bergantung pada transformasi URL `fl_attachment`, sehingga
    kompatibel untuk image, video, dan raw file yang disimpan dengan resource_type auto.
    """
    asset_url = get_cloudinary_asset_url(asset)
    if not asset_url:
        abort(404)

    request_headers = {
        'User-Agent': 'MediaRU-Flask/1.0',
        'Accept': '*/*'
    }
    try:
        cloud_request = UrlRequest(asset_url, headers=request_headers)
        with urlopen(cloud_request, timeout=30) as cloud_response:
            max_download_size = 32 * 1024 * 1024
            file_bytes = cloud_response.read(max_download_size + 1)
            if len(file_bytes) > max_download_size:
                abort(413)
            response_content_type = cloud_response.headers.get_content_type()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        app.logger.warning('Cloudinary download failed: %s', exc)
        abort(502)

    filename = secure_filename(asset.get('filename') or 'asset') or 'asset'
    content_type = asset.get('content_type') or response_content_type or 'application/octet-stream'
    return send_file(
        BytesIO(file_bytes),
        mimetype=content_type,
        as_attachment=True,
        download_name=filename,
        max_age=0
    )


@app.route('/assets', methods=['GET', 'POST'])
def assets():
    error = request.args.get('error', '')

    if request.method == 'POST':
        if 'file' not in request.files:
            return redirect(url_for('assets', error='File tidak ditemukan.'))

        file = request.files['file']
        if not file.filename:
            return redirect(url_for('assets', error='Silakan pilih file terlebih dahulu.'))

        try:
            asset = upload_to_cloudinary(file)
            asset['description'] = (request.form.get('description') or '').strip()
            asset['upload_date'] = datetime.now()
            db.assets.insert_one(asset)
        except RuntimeError as exc:
            return redirect(url_for('assets', error=str(exc)))

        return redirect(url_for('assets'))

    files = list(db.assets.find().sort("upload_date", -1))
    return render_template('assets.html', files=files, error=error)


@app.route('/assets/download/<asset_id>')
def download_asset(asset_id):
    """Download Cloudinary asset atau asset lokal lama."""
    try:
        object_id = ObjectId(asset_id)
    except Exception:
        abort(404)

    asset = db.assets.find_one({'_id': object_id})
    if not asset:
        abort(404)

    if asset.get('storage') == 'cloudinary':
        return proxy_cloudinary_download(asset)

    filename = asset.get('filename')
    if not filename:
        abort(404)

    local_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    if not os.path.isfile(local_path):
        abort(404)

    return send_from_directory(
        app.config['UPLOAD_FOLDER'],
        filename,
        as_attachment=True,
        download_name=filename
    )


# ==========================================
# 8. DATABASE CONSOLE (MIGRASI / BROADCAST KEY)
# ==========================================
@app.route('/console', methods=['GET', 'POST'])
def db_console():
    message = ""
    status_type = "success"

    if request.method == 'POST':
        target = request.form.get('target')
        new_key = request.form.get('new_key')
        default_val = request.form.get('default_val')

        if default_val and default_val.isdigit():
            default_val = int(default_val)

        if not new_key:
            message = "Nama Key tidak boleh kosong!"
            status_type = "danger"
        else:
            if target == 'members':
                team_data = db.settings.find_one({"_id": "team_settings"})
                if team_data and 'members_array' in team_data:
                    updated_members = []
                    for member in team_data['members_array']:
                        if isinstance(member, dict):
                            member[new_key] = default_val
                            updated_members.append(member)
                        else:
                            updated_members.append({
                                "name": member,
                                "role": "Belum di-set",
                                new_key: default_val
                            })

                    db.settings.update_one(
                        {"_id": "team_settings"},
                        {"$set": {"members_array": updated_members}}
                    )
                    message = f"Berhasil broadcast key '{new_key}' ke semua data Anggota!"

            elif target in ['attendance', 'finance', 'tasks', 'assets', 'announcements', 'ai_chat_logs']:
                db[target].update_many({}, {"$set": {new_key: default_val}})
                message = f"Berhasil broadcast key '{new_key}' ke semua data di koleksi '{target}'!"

    return render_template('console.html', message=message, status_type=status_type)


# ==========================================
# 7. TOOLS — YTMP3 / YTAUDIO (LOLHUMAN ONLY)
# ==========================================
import requests, json, ast
def rol(urlw, keya):
    tol = 'https://api.lolhuman.xyz/api/ytaudio2?'
    gas = requests.get(tol+'apikey='+keya+'&url='+urlw).json()
    if gas['status'] == 200:
        return gas
    else :
        return {'status':400}

@app.route('/ytmp3', methods=['GET', 'POST'])
def ytmp3():
    error = ''
    youtube_url = ''

    if request.method == 'POST':
        youtube_url = (request.form.get('url') or '').strip()
        for i in ast.literal_eval(os.getenv('lol', '[]')):
            yo = rol(youtube_url,i)
            if yo['status'] == 200:
                return redirect(yo['result']['link'])
            print('mencoba apikey lain')
        return render_template('ytmp3.html',error='eror guys',youtube_url=youtube_url,lolhuman_key_count=len(ast.literal_eval(os.getenv('lol', '[]'))))
    return render_template(
        'ytmp3.html',
        error=error,
        youtube_url=youtube_url,
        lolhuman_key_count=len(ast.literal_eval(os.getenv('lol', '[]')))
    )


# ==========================================
# 8. TOOLS — REMOVE BACKGROUND (PICWISH SYNC)
# ==========================================
class PicWishProviderError(RuntimeError):
    """Kesalahan terkontrol dari PicWish Background Removal API."""


def get_picwish_api_keys():
    """
    Membaca API key PicWish dari environment variable PICH.

    Format yang didukung:
    PICH=["KEY_1","KEY_2","KEY_3","KEY_4","KEY_5"]

    Atau:
    PICH=KEY_1,KEY_2,KEY_3

    Urutan API key dipertahankan.
    """

    raw_value = (
        os.getenv('PICH')
        or os.getenv('pich')
        or ''
    ).strip()

    if not raw_value:
        return []

    api_keys = []

    # Format JSON array
    try:
        parsed = json.loads(raw_value)

        if isinstance(parsed, list):
            values = parsed

        elif isinstance(parsed, str):
            values = [parsed]

        else:
            values = []

    except (json.JSONDecodeError, TypeError):
        # Format CSV atau newline
        normalized = (
            raw_value
            .replace('\r\n', '\n')
            .replace('\r', '\n')
        )

        if '\n' in normalized:
            values = normalized.split('\n')
        else:
            values = normalized.split(',')

    # Bersihkan API key tanpa mengubah urutan
    for value in values:
        key = str(value).strip().strip('"').strip("'")

        if key and key not in api_keys:
            api_keys.append(key)

    return api_keys

def _picwish_error_message(payload, fallback='PicWish gagal memproses gambar.'):
    if isinstance(payload, dict):
        message = payload.get('message') or payload.get('msg') or payload.get('error')
        if isinstance(message, dict):
            message = message.get('message') or message.get('detail')
        if message:
            return str(message)[:300]
    return fallback


def remove_background_with_picwish(file_storage, foreground_type='', crop=False):
    """Kirim gambar ke PicWish sync API dan berhenti pada API key pertama yang berhasil."""
    api_keys = get_picwish_api_keys()
    if not api_keys:
        raise PicWishProviderError('Environment `PICH` belum berisi API key PicWish.')

    filename = secure_filename(file_storage.filename or 'image.png') or 'image.png'
    content_type = (file_storage.mimetype or 'application/octet-stream').lower()
    allowed_mimetypes = {
        'image/jpeg', 'image/png', 'image/webp', 'image/bmp',
        'image/tiff', 'image/x-tiff', 'image/jfif'
    }
    if not content_type.startswith('image/'):
        raise PicWishProviderError('File yang dipilih harus berupa gambar.')
    if content_type not in allowed_mimetypes and not filename.lower().endswith(
        ('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff', '.jfif')
    ):
        raise PicWishProviderError('Format gambar belum didukung oleh Remove BG.')

    image_bytes = file_storage.read()
    if not image_bytes:
        raise PicWishProviderError('File gambar kosong atau tidak dapat dibaca.')
    if len(image_bytes) > 20 * 1024 * 1024:
        raise PicWishProviderError('Ukuran gambar maksimal 20 MB.')

    foreground_type = (foreground_type or '').strip().lower()
    if foreground_type not in {'', 'person', 'object', 'stamp'}:
        foreground_type = ''

    endpoint = (
        os.getenv('PICWISH_REMOVE_BG_URL')
        or 'https://techhk.aoscdn.com/api/tasks/visual/segmentation'
    ).strip()
    errors = []

    for index, api_key in enumerate(api_keys, start=1):
        try:
            form_data = {
                'sync': '1',
                'return_type': '1',
                'output_type': '2',
                'crop': '1' if crop else '0',
                'format': 'png'
            }
            if foreground_type:
                form_data['type'] = foreground_type

            response = requests.post(
                endpoint,
                headers={'X-API-KEY': api_key},
                data=form_data,
                files={'image_file': (filename, BytesIO(image_bytes), content_type)},
                timeout=(15, 180)
            )

            try:
                payload = response.json()
            except ValueError:
                payload = {}

            api_status = payload.get('status', response.status_code) if isinstance(payload, dict) else response.status_code
            data = payload.get('data') if isinstance(payload, dict) else None
            data = data if isinstance(data, dict) else {}
            state = data.get('state')
            result_url = data.get('image')

            if (
                response.ok
                and str(api_status) == '200'
                and result_url
                and (state is None or str(state) == '1')
            ):
                result_url = str(result_url).strip()
                parsed = urlparse(result_url)
                if parsed.scheme != 'https' or not parsed.hostname:
                    raise PicWishProviderError('PicWish mengembalikan URL hasil yang tidak valid.')
                return {
                    'result_url': result_url,
                    'key_index': index,
                    'filename': f"remove-bg-{Path(filename).stem}.png"
                }

            message = _picwish_error_message(
                payload,
                fallback=f'HTTP {response.status_code}'
            )
            normalized = message.lower()
            key_related = (
                response.status_code in {401, 403, 429, 500, 502, 503, 504}
                or str(api_status) in {'401', '403', '429', '500', '502', '503', '504'}
                or any(word in normalized for word in (
                    'api key', 'apikey', 'unauthorized', 'credit', 'quota',
                    'balance', 'frequency', 'rate limit', 'qps'
                ))
            )

            if key_related:
                errors.append(f'key #{index}: {message[:120]}')
                app.logger.warning('PicWish key #%s gagal: %s', index, message)
                continue

            # Kesalahan gambar/parameter tidak akan dicoba ulang memakai key lain,
            # agar kredit key berikutnya tidak ikut terpakai untuk input yang sama.
            raise PicWishProviderError(message)

        except PicWishProviderError:
            raise
        except requests.RequestException as exc:
            errors.append(f'key #{index}: koneksi gagal')
            app.logger.warning('PicWish key #%s request gagal: %s', index, exc)
        except Exception as exc:
            errors.append(f'key #{index}: {type(exc).__name__}')
            app.logger.exception('PicWish key #%s gagal', index)

    detail = ', '.join(errors) if errors else 'tidak ada key yang berhasil'
    raise PicWishProviderError(f'Semua API key PicWish gagal digunakan ({detail}).')


@app.route('/remove-bg', methods=['GET', 'POST'])
def remove_bg():
    if request.method == 'GET':
        return render_template(
            'remove_bg.html',
            picwish_key_count=len(get_picwish_api_keys())
        )

    file = request.files.get('image')
    if not file or not file.filename:
        return render_template(
            'remove_bg.html',
            picwish_key_count=len(get_picwish_api_keys()),
            error='Silakan pilih gambar terlebih dahulu.'
        ), 400

    try:
        result = remove_background_with_picwish(
            file,
            foreground_type=request.form.get('type'),
            crop=request.form.get('crop') == '1'
        )

        # PicWish Sync mengembalikan URL hasil sementara.
        # Ambil hasil tersebut sekarang lalu kirim sebagai file langsung.
        result_response = requests.get(
            result['result_url'],
            timeout=(15, 120)
        )
        result_response.raise_for_status()

        result_bytes = result_response.content
        if not result_bytes:
            raise PicWishProviderError('Hasil Remove BG dari PicWish kosong.')

        response = send_file(
            BytesIO(result_bytes),
            mimetype='image/png',
            as_attachment=True,
            download_name=result['filename'],
            max_age=0
        )
        response.headers['X-PicWish-Key-Index'] = str(result['key_index'])
        response.headers['Cache-Control'] = 'no-store'
        return response

    except PicWishProviderError as exc:
        return render_template(
            'remove_bg.html',
            picwish_key_count=len(get_picwish_api_keys()),
            error=str(exc)
        ), 502
    except requests.RequestException as exc:
        app.logger.warning('Hasil PicWish gagal diambil: %s', exc)
        return render_template(
            'remove_bg.html',
            picwish_key_count=len(get_picwish_api_keys()),
            error='Remove BG berhasil diproses, tetapi hasil dari PicWish gagal diunduh.'
        ), 502
    except Exception:
        app.logger.exception('Remove BG PicWish gagal')
        return render_template(
            'remove_bg.html',
            picwish_key_count=len(get_picwish_api_keys()),
            error='Remove BG gagal diproses oleh server.'
        ), 500


# ==========================================
# 9. TOOLS — TIKTOK DOWNLOADER HD
# ==========================================
class TikTokDownloadError(RuntimeError):
    """Kesalahan terkontrol dari tiktok-downloader-hd."""


def is_tiktok_url(value):
    try:
        parsed = urlparse((value or '').strip())
    except Exception:
        return False
    if parsed.scheme not in {'http', 'https'}:
        return False
    hostname = (parsed.hostname or '').lower()
    return hostname == 'tiktok.com' or hostname.endswith('.tiktok.com')


def download_tiktok_hd(video_url):
    """Unduh video TikTok penuh ke folder temp sebelum dikirim dengan send_file."""
    try:
        from tiktok_downloader import TikTokDownloader
    except ImportError as exc:
        raise TikTokDownloadError(
            'Package tiktok-downloader-hd belum terpasang. Jalankan pip install -r requirements.txt.'
        ) from exc

    temp_dir = tempfile.mkdtemp(prefix='fyy-tiktok-')
    filename_prefix = f'tiktok-{uuid.uuid4().hex[:12]}'
    output_path = os.path.join(temp_dir, f'{filename_prefix}.mp4')
    downloader = None

    cookies_path = (os.getenv('TIKTOK_COOKIES_PATH') or '').strip() or None
    if cookies_path and not os.path.isfile(cookies_path):
        cookies_path = None

    try:
        downloader = TikTokDownloader(
            download_dir=temp_dir,
            cookies_path=cookies_path,
            headless=True
        )
        success = downloader.download(
            video_url,
            filename_prefix=filename_prefix,
            retries=2
        )
        if not success or not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
            raise TikTokDownloadError('TikTok gagal diunduh. Periksa link lalu coba kembali.')
        return {
            'output_path': output_path,
            'temp_dir': temp_dir,
            'filename': f'{filename_prefix}.mp4'
        }
    except TikTokDownloadError:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        error_text = str(exc).lower()
        if 'chrome' in error_text or 'driver' in error_text or 'browser' in error_text:
            raise TikTokDownloadError(
                'Google Chrome/ChromeDriver tidak tersedia pada server. '
                'Package tiktok-downloader-hd membutuhkannya untuk menjalankan scraper.'
            ) from exc
        raise TikTokDownloadError(f'TikTok downloader gagal: {type(exc).__name__}.') from exc
    finally:
        if downloader is not None:
            try:
                downloader.close()
            except Exception:
                pass


def _send_tiktok_file(result):
    response = send_file(
        result['output_path'],
        mimetype='video/mp4',
        as_attachment=True,
        download_name=result['filename'],
        max_age=0
    )

    @response.call_on_close
    def cleanup_tiktok_file():
        shutil.rmtree(result['temp_dir'], ignore_errors=True)

    return response


@app.route('/tiktok-downloader', methods=['GET', 'POST'])
def tiktok_downloader():
    error = ''
    tiktok_url = ''

    if request.method == 'POST':
        tiktok_url = (request.form.get('url') or '').strip()
        if not is_tiktok_url(tiktok_url):
            error = 'Masukkan URL video TikTok yang valid.'
        else:
            try:
                return _send_tiktok_file(download_tiktok_hd(tiktok_url))
            except TikTokDownloadError as exc:
                app.logger.warning('TikTok downloader gagal: %s', exc)
                error = str(exc)

    return render_template(
        'tiktok_downloader.html',
        error=error,
        tiktok_url=tiktok_url
    )

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=True)
