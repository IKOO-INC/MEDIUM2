from flask import (
    Flask, abort, jsonify, redirect, render_template, request,
    send_file, send_from_directory, session, url_for
)
from pymongo import MongoClient
from pymongo.errors import PyMongoError
import gridfs
from bson.objectid import ObjectId
from werkzeug.utils import secure_filename
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlencode
from urllib.request import Request as UrlRequest, urlopen
import json
import os
import random
import shutil
import socket
import ipaddress
import threading
import tempfile
import uuid
import zipfile
from pathlib import Path
from datetime import datetime, timedelta

import requests
import cloudinary
import cloudinary.uploader
from cloudinary.exceptions import Error as CloudinaryError
from google import genai
from PIL import Image, ImageOps, UnidentifiedImageError

import qrcode
from qrcode.constants import ERROR_CORRECT_M
import requests, json
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'fyy-medium-dev-secret-change-me')

# Konfigurasi Upload
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # Maksimal ukuran file 16MB
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
# Konfigurasi Upload
app.config['UPLOAD_FOLDER'] = 'static/uploads'
try:
    _default_upload_mb = 20 if os.getenv('VERCEL') else 512
    _max_upload_mb = int(os.getenv('APP_MAX_UPLOAD_MB', str(_default_upload_mb)))
except ValueError:
    _max_upload_mb = 20 if os.getenv('VERCEL') else 512
app.config['MAX_CONTENT_LENGTH'] = max(20, min(_max_upload_mb, 4096)) * 1024 * 1024
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

# ==========================================
# 9. TOOLS — ENHANCE PHOTO (PICWISH SYNC)
# ==========================================
def enhance_photo_with_picwish(
    file_storage,
    enhance_type='clean',
    scale_factor='',
    output_format='jpg'
):
    """Enhance gambar lewat PicWish Sync dan berhenti pada key pertama yang sukses."""
    api_keys = get_picwish_api_keys()
    if not api_keys:
        raise PicWishProviderError('Environment `PICH` belum berisi API key PicWish.')

    filename = secure_filename(file_storage.filename or 'photo.jpg') or 'photo.jpg'
    content_type = (file_storage.mimetype or 'application/octet-stream').lower()
    if not content_type.startswith('image/'):
        raise PicWishProviderError('File yang dipilih harus berupa gambar.')

    image_bytes = file_storage.read()
    if not image_bytes:
        raise PicWishProviderError('File gambar kosong atau tidak dapat dibaca.')
    if len(image_bytes) > 20 * 1024 * 1024:
        raise PicWishProviderError('Ukuran gambar maksimal 20 MB.')

    enhance_type = (enhance_type or 'clean').strip().lower()
    if enhance_type not in {'clean', 'face'}:
        enhance_type = 'clean'

    scale_factor = str(scale_factor or '').strip()
    if scale_factor not in {'', '1', '2', '4'}:
        scale_factor = ''

    output_format = (output_format or 'jpg').strip().lower()
    if output_format not in {'jpg', 'png'}:
        output_format = 'jpg'

    endpoint = (
        os.getenv('PICWISH_PHOTO_ENHANCER_URL')
        or 'https://techhk.aoscdn.com/api/tasks/visual/scale'
    ).strip()
    errors = []

    for index, api_key in enumerate(api_keys, start=1):
        try:
            form_data = {
                'sync': '1',
                'return_type': '1',
                'type': enhance_type,
                'format': output_format
            }
            if scale_factor:
                form_data['scale_factor'] = scale_factor

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
                extension = 'png' if output_format == 'png' else 'jpg'
                return {
                    'result_url': result_url,
                    'key_index': index,
                    'filename': f"enhanced-{Path(filename).stem}.{extension}",
                    'mimetype': 'image/png' if extension == 'png' else 'image/jpeg'
                }

            message = _picwish_error_message(payload, fallback=f'HTTP {response.status_code}')
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
                app.logger.warning('PicWish enhancer key #%s gagal: %s', index, message)
                continue

            raise PicWishProviderError(message)

        except PicWishProviderError:
            raise
        except requests.RequestException as exc:
            errors.append(f'key #{index}: koneksi gagal')
            app.logger.warning('PicWish enhancer key #%s request gagal: %s', index, exc)
        except Exception as exc:
            errors.append(f'key #{index}: {type(exc).__name__}')
            app.logger.exception('PicWish enhancer key #%s gagal', index)

    detail = ', '.join(errors) if errors else 'tidak ada key yang berhasil'
    raise PicWishProviderError(f'Semua API key PicWish gagal digunakan ({detail}).')


@app.route('/enhance-photo', methods=['GET', 'POST'])
def enhance_photo():
    if request.method == 'GET':
        return render_template(
            'enhance_photo.html',
            picwish_key_count=len(get_picwish_api_keys())
        )

    file = request.files.get('image')
    if not file or not file.filename:
        return render_template(
            'enhance_photo.html',
            picwish_key_count=len(get_picwish_api_keys()),
            error='Silakan pilih gambar terlebih dahulu.'
        ), 400

    try:
        result = enhance_photo_with_picwish(
            file,
            enhance_type=request.form.get('type'),
            scale_factor=request.form.get('scale_factor'),
            output_format=request.form.get('format')
        )

        result_response = requests.get(result['result_url'], timeout=(15, 120))
        result_response.raise_for_status()
        result_bytes = result_response.content
        if not result_bytes:
            raise PicWishProviderError('Hasil Enhance Photo dari PicWish kosong.')

        response = send_file(
            BytesIO(result_bytes),
            mimetype=result['mimetype'],
            as_attachment=True,
            download_name=result['filename'],
            max_age=0
        )
        response.headers['X-PicWish-Key-Index'] = str(result['key_index'])
        response.headers['Cache-Control'] = 'no-store'
        return response

    except PicWishProviderError as exc:
        return render_template(
            'enhance_photo.html',
            picwish_key_count=len(get_picwish_api_keys()),
            error=str(exc)
        ), 502
    except requests.RequestException as exc:
        app.logger.warning('Hasil PicWish enhancer gagal diambil: %s', exc)
        return render_template(
            'enhance_photo.html',
            picwish_key_count=len(get_picwish_api_keys()),
            error='Enhance Photo berhasil diproses, tetapi hasil PicWish gagal diunduh.'
        ), 502
    except Exception:
        app.logger.exception('Enhance Photo PicWish gagal')
        return render_template(
            'enhance_photo.html',
            picwish_key_count=len(get_picwish_api_keys()),
            error='Enhance Photo gagal diproses oleh server.'
        ), 500

# ==========================================
# TOOLS — FYY SHARE LAN (BIDIRECTIONAL WIFI TRANSFER)
# ==========================================
class FyyShareError(RuntimeError):
    """Kesalahan terkontrol dari fitur FYY Share LAN."""


FYY_SHARE_ROOT = Path(
    os.getenv(
        'FYY_SHARE_DIR',
        str(Path(tempfile.gettempdir()) / 'fyy-share-lan')
    )
)
FYY_SHARE_ROOT.mkdir(parents=True, exist_ok=True)
FYY_SHARE_LOCK = threading.RLock()
FYY_SHARE_ROLES = {'host', 'peer'}


def get_fyy_share_ttl_minutes():
    try:
        value = int(os.getenv('FYY_SHARE_TTL_MINUTES', '30'))
    except ValueError:
        value = 30
    return max(5, min(value, 1440))


def get_fyy_share_max_mb():
    try:
        value = int(os.getenv('FYY_SHARE_MAX_MB', '512'))
    except ValueError:
        value = 512
    return max(1, min(value, 4096))


def fyy_share_local_runtime_enabled():
    """LAN transfer harus dijalankan pada perangkat lokal, bukan Vercel."""
    forced = (os.getenv('FYY_SHARE_LAN_ENABLED') or '').strip().lower()
    if forced in {'1', 'true', 'yes', 'on'}:
        return True
    if forced in {'0', 'false', 'no', 'off'}:
        return False
    return not bool(os.getenv('VERCEL'))


def _valid_fyy_share_token(token):
    token = (token or '').strip().lower()
    return len(token) == 32 and all(char in '0123456789abcdef' for char in token)


def _valid_fyy_share_role(role):
    return (role or '').strip().lower() in FYY_SHARE_ROLES


def _fyy_share_other_role(role):
    return 'peer' if role == 'host' else 'host'


def _fyy_share_dir(token):
    if not _valid_fyy_share_token(token):
        raise FyyShareError('Token FYY Share tidak valid.')
    return FYY_SHARE_ROOT / token


def _fyy_share_meta_path(token):
    return _fyy_share_dir(token) / 'meta.json'


def _empty_fyy_share_file(role):
    return {
        'state': 'empty',
        'filename': '',
        'stored_name': f'payload-{role}.bin',
        'content_type': '',
        'bytes': 0,
        'ready_at': '',
        'download_count': 0,
        'transfer_state': 'idle',
        'transferred_bytes': 0,
        'transfer_total': 0,
        'transfer_updated_at': '',
        'last_completed_id': ''
    }


def _normalize_fyy_share_meta(meta):
    """Normalisasi metadata dan migrasikan format FYY Share LAN lama."""
    if not isinstance(meta, dict):
        return None

    normalized = dict(meta)
    files = normalized.get('files')
    if not isinstance(files, dict):
        files = {}

    for role in FYY_SHARE_ROLES:
        current = files.get(role)
        if not isinstance(current, dict):
            current = {}
        merged = _empty_fyy_share_file(role)
        merged.update(current)
        files[role] = merged

    # Migrasi satu file versi lama sebagai file milik host.
    if normalized.get('filename') and files['host'].get('state') == 'empty':
        files['host'].update({
            'state': 'ready' if normalized.get('state') == 'ready' else 'empty',
            'filename': normalized.get('filename') or '',
            'stored_name': normalized.get('stored_name') or 'payload.bin',
            'content_type': normalized.get('content_type') or '',
            'bytes': int(normalized.get('bytes') or 0),
            'ready_at': normalized.get('ready_at') or '',
            'download_count': int(normalized.get('download_count') or 0)
        })

    participants = normalized.get('participants')
    if not isinstance(participants, dict):
        participants = {}

    host = participants.get('host') if isinstance(participants.get('host'), dict) else {}
    peer = participants.get('peer') if isinstance(participants.get('peer'), dict) else {}
    participants['host'] = {
        'ip': host.get('ip') or '',
        'last_seen': host.get('last_seen') or ''
    }
    participants['peer'] = {
        'ip': peer.get('ip') or normalized.get('receiver_ip') or '',
        'last_seen': peer.get('last_seen') or normalized.get('receiver_last_seen') or ''
    }

    normalized['files'] = files
    normalized['participants'] = participants
    normalized.pop('state', None)
    return normalized


def _read_fyy_share_meta(token):
    try:
        meta_path = _fyy_share_meta_path(token)
    except FyyShareError:
        return None

    if not meta_path.is_file():
        return None

    try:
        data = json.loads(meta_path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return None

    return _normalize_fyy_share_meta(data)


def _write_fyy_share_meta(meta):
    normalized = _normalize_fyy_share_meta(meta)
    if not normalized:
        raise FyyShareError('Metadata sesi FYY Share tidak valid.')

    token = normalized.get('token')
    share_dir = _fyy_share_dir(token)
    share_dir.mkdir(parents=True, exist_ok=True)
    meta_path = share_dir / 'meta.json'
    temp_path = share_dir / 'meta.tmp'
    temp_path.write_text(
        json.dumps(normalized, ensure_ascii=False, separators=(',', ':')),
        encoding='utf-8'
    )
    os.replace(temp_path, meta_path)


def _parse_share_datetime(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def cleanup_expired_fyy_shares(limit=100):
    """Hapus file lokal yang masa aktifnya habis, tanpa MongoDB/Cloudinary."""
    now = datetime.utcnow()
    removed = 0

    try:
        candidates = list(FYY_SHARE_ROOT.iterdir())
    except OSError:
        return

    for share_dir in candidates:
        if removed >= limit:
            break
        if not share_dir.is_dir():
            continue

        meta = _read_fyy_share_meta(share_dir.name)
        expires_at = _parse_share_datetime(meta.get('expires_at')) if meta else None
        if not meta or not expires_at or expires_at <= now:
            shutil.rmtree(share_dir, ignore_errors=True)
            removed += 1


def create_fyy_share_session():
    cleanup_expired_fyy_shares()
    now = datetime.utcnow()
    token = uuid.uuid4().hex
    meta = {
        'token': token,
        'files': {
            'host': _empty_fyy_share_file('host'),
            'peer': _empty_fyy_share_file('peer')
        },
        'participants': {
            'host': {'ip': '', 'last_seen': ''},
            'peer': {'ip': '', 'last_seen': ''}
        },
        'created_at': now.isoformat(),
        'expires_at': (now + timedelta(minutes=get_fyy_share_ttl_minutes())).isoformat()
    }
    with FYY_SHARE_LOCK:
        _write_fyy_share_meta(meta)
    return meta


def get_active_fyy_share(token):
    meta = _read_fyy_share_meta(token)
    if not meta:
        return None

    expires_at = _parse_share_datetime(meta.get('expires_at'))
    if not expires_at or expires_at <= datetime.utcnow():
        shutil.rmtree(_fyy_share_dir(token), ignore_errors=True)
        return None

    return meta


def _mutate_fyy_share(token, callback):
    """Mutasi metadata secara atomik dalam satu proses Flask."""
    with FYY_SHARE_LOCK:
        meta = get_active_fyy_share(token)
        if not meta:
            return None
        callback(meta)
        _write_fyy_share_meta(meta)
        return meta


def _fyy_share_role_authorized(token, role):
    if role == 'host':
        return session.get('fyy_share_sender_token') == token
    if role == 'peer':
        return session.get('fyy_share_peer_token') == token
    return False


def _mark_fyy_share_seen(token, role):
    if not _valid_fyy_share_role(role):
        return None

    def update(meta):
        participant = meta['participants'][role]
        participant['ip'] = request.remote_addr or participant.get('ip') or ''
        participant['last_seen'] = datetime.utcnow().isoformat()

    return _mutate_fyy_share(token, update)


def _fyy_share_participant_online(meta, role, timeout_seconds=8):
    participant = (meta.get('participants') or {}).get(role) or {}
    last_seen = _parse_share_datetime(participant.get('last_seen'))
    if not last_seen:
        return False
    return (datetime.utcnow() - last_seen).total_seconds() <= timeout_seconds


def save_fyy_share_file(token, role, file_storage):
    if not _valid_fyy_share_role(role):
        raise FyyShareError('Peran perangkat tidak valid.')

    meta = get_active_fyy_share(token)
    if not meta:
        raise FyyShareError('Sesi FYY Share sudah tidak aktif.')

    original_filename = secure_filename(file_storage.filename or 'file') or 'file'
    content_type = file_storage.mimetype or 'application/octet-stream'
    share_dir = _fyy_share_dir(token)
    stored_name = f'payload-{role}.bin'
    final_path = share_dir / stored_name
    temp_path = share_dir / f'payload-{role}.uploading'

    try:
        file_storage.save(temp_path)
        size = temp_path.stat().st_size
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        raise FyyShareError('File gagal disimpan pada perangkat server lokal.') from exc

    max_bytes = get_fyy_share_max_mb() * 1024 * 1024
    if size <= 0:
        temp_path.unlink(missing_ok=True)
        raise FyyShareError('File kosong tidak dapat dibagikan.')
    if size > max_bytes:
        temp_path.unlink(missing_ok=True)
        raise FyyShareError(
            f'Ukuran file melebihi batas {get_fyy_share_max_mb()} MB.'
        )

    os.replace(temp_path, final_path)

    def update(current):
        current_file = current['files'][role]
        current_file.update({
            'state': 'ready',
            'filename': original_filename,
            'stored_name': stored_name,
            'content_type': content_type,
            'bytes': size,
            'ready_at': datetime.utcnow().isoformat(),
            'download_count': 0,
            'transfer_state': 'ready',
            'transferred_bytes': 0,
            'transfer_total': size,
            'transfer_updated_at': datetime.utcnow().isoformat(),
            'last_completed_id': ''
        })

    updated = _mutate_fyy_share(token, update)
    if not updated:
        final_path.unlink(missing_ok=True)
        raise FyyShareError('Sesi FYY Share sudah berakhir.')
    return updated


def delete_fyy_share_file_data(token, role):
    if not _valid_fyy_share_role(role):
        raise FyyShareError('Peran perangkat tidak valid.')

    meta = get_active_fyy_share(token)
    if not meta:
        raise FyyShareError('Sesi FYY Share sudah tidak aktif.')

    file_data = meta['files'][role]
    stored_name = file_data.get('stored_name') or f'payload-{role}.bin'
    (_fyy_share_dir(token) / stored_name).unlink(missing_ok=True)

    def update(current):
        current['files'][role] = _empty_fyy_share_file(role)

    return _mutate_fyy_share(token, update)


def _public_fyy_share_file(token, role, file_data, include_download=False):
    payload = {
        'state': file_data.get('state') or 'empty',
        'filename': file_data.get('filename') or '',
        'content_type': file_data.get('content_type') or '',
        'bytes': int(file_data.get('bytes') or 0),
        'download_count': int(file_data.get('download_count') or 0),
        'transfer_state': file_data.get('transfer_state') or 'idle',
        'transferred_bytes': int(file_data.get('transferred_bytes') or 0),
        'transfer_total': int(file_data.get('transfer_total') or file_data.get('bytes') or 0)
    }
    if include_download and payload['state'] == 'ready':
        payload['download_url'] = url_for(
            'fyy_share_download',
            token=token,
            source_role=role
        )
    else:
        payload['download_url'] = ''
    return payload


def _private_ipv4(value):
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None
    if address.version != 4 or address.is_loopback or address.is_unspecified:
        return None
    if address.is_private or address.is_link_local:
        return str(address)
    return None


def detect_fyy_share_lan_ip():
    explicit = _private_ipv4(os.getenv('FYY_SHARE_LAN_HOST', ''))
    if explicit:
        return explicit

    try:
        request_host = request.host.split(':', 1)[0].strip('[]')
    except RuntimeError:
        request_host = ''
    detected = _private_ipv4(request_host)
    if detected:
        return detected

    candidates = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.25)
            sock.connect(('10.255.255.255', 1))
            candidates.append(sock.getsockname()[0])
    except OSError:
        pass

    try:
        hostname = socket.gethostname()
        candidates.extend(socket.gethostbyname_ex(hostname)[2])
    except OSError:
        pass

    for candidate in candidates:
        detected = _private_ipv4(candidate)
        if detected:
            return detected

    return '127.0.0.1'


def get_fyy_share_lan_port():
    explicit = (os.getenv('FYY_SHARE_LAN_PORT') or '').strip()
    if explicit.isdigit():
        return int(explicit)

    try:
        host_value = request.host
        if ':' in host_value:
            possible = host_value.rsplit(':', 1)[1]
            if possible.isdigit():
                return int(possible)
    except RuntimeError:
        pass

    return 5000


def build_fyy_share_lan_url(token):
    host = detect_fyy_share_lan_ip()
    port = get_fyy_share_lan_port()
    port_suffix = '' if port == 80 else f':{port}'
    path = url_for('fyy_share_receive', token=token)
    return f'http://{host}{port_suffix}{path}'


def _fyy_share_template_context(share, role, **extra):
    other_role = _fyy_share_other_role(role)
    context = {
        'share': share,
        'role': role,
        'other_role': other_role,
        'own_file': share['files'][role],
        'incoming_file': share['files'][other_role],
        'other_online': _fyy_share_participant_online(share, other_role),
        'other_ip': share['participants'][other_role].get('ip') or '',
        'ttl_minutes': get_fyy_share_ttl_minutes(),
        'max_mb': get_fyy_share_max_mb()
    }
    context.update(extra)
    return context


@app.route('/fyy-share', methods=['GET', 'POST'])
def fyy_share():
    cleanup_expired_fyy_shares()
    local_runtime = fyy_share_local_runtime_enabled()
    error = ''

    if not local_runtime:
        return render_template(
            'fyy_share.html',
            error='',
            share=None,
            local_runtime=False,
            ttl_minutes=get_fyy_share_ttl_minutes(),
            max_mb=get_fyy_share_max_mb()
        )

    token = (
        request.form.get('token')
        or request.args.get('token')
        or session.get('fyy_share_sender_token')
        or ''
    ).strip()
    share = get_active_fyy_share(token)

    if request.args.get('new') == '1' or not share:
        share = create_fyy_share_session()
        token = share['token']
        session['fyy_share_sender_token'] = token

    _mark_fyy_share_seen(token, 'host')
    share = get_active_fyy_share(token)

    # Fallback tanpa JavaScript tetap didukung.
    if request.method == 'POST':
        if not _fyy_share_role_authorized(token, 'host'):
            abort(403)
        file = request.files.get('file')
        if not file or not file.filename:
            error = 'Silakan pilih file yang akan dikirim.'
        else:
            try:
                share = save_fyy_share_file(token, 'host', file)
                return redirect(url_for('fyy_share', token=token))
            except FyyShareError as exc:
                error = str(exc)
            except Exception:
                app.logger.exception('FYY Share LAN gagal menyimpan file host')
                error = 'File gagal disiapkan pada perangkat pengirim.'

    lan_ip = detect_fyy_share_lan_ip()
    lan_port = get_fyy_share_lan_port()
    context = _fyy_share_template_context(
        share,
        'host',
        error=error,
        local_runtime=True,
        lan_ready=lan_ip != '127.0.0.1',
        lan_ip=lan_ip,
        lan_port=lan_port,
        share_url=build_fyy_share_lan_url(token)
    )
    return render_template('fyy_share.html', **context)


@app.route('/fyy-share/<token>', methods=['GET'])
def fyy_share_receive(token):
    share = get_active_fyy_share(token)
    if not share:
        abort(404)

    session['fyy_share_peer_token'] = token
    _mark_fyy_share_seen(token, 'peer')
    share = get_active_fyy_share(token)
    context = _fyy_share_template_context(
        share,
        'peer',
        local_runtime=True,
        share_url=request.url,
        lan_ip=detect_fyy_share_lan_ip(),
        lan_port=get_fyy_share_lan_port(),
        lan_ready=True,
        error=''
    )
    return render_template('fyy_share_receive.html', **context)


@app.route('/fyy-share/status/<token>')
def fyy_share_status(token):
    role = (request.args.get('role') or '').strip().lower()
    if not _valid_fyy_share_role(role) or not _fyy_share_role_authorized(token, role):
        return jsonify({'ok': False, 'error': 'Sesi perangkat tidak valid.'}), 403

    share = _mark_fyy_share_seen(token, role)
    if not share:
        return jsonify({'ok': False, 'expired': True}), 404

    other_role = _fyy_share_other_role(role)
    return jsonify({
        'ok': True,
        'role': role,
        'other_role': other_role,
        'other_connected': _fyy_share_participant_online(share, other_role),
        'other_ip': share['participants'][other_role].get('ip') or '',
        'own_file': _public_fyy_share_file(token, role, share['files'][role]),
        'incoming_file': _public_fyy_share_file(
            token,
            other_role,
            share['files'][other_role],
            include_download=True
        ),
        'expires_at': share.get('expires_at') or ''
    })


@app.route('/fyy-share/upload/<token>/<role>', methods=['POST'])
def fyy_share_upload(token, role):
    role = (role or '').strip().lower()
    if not _valid_fyy_share_role(role) or not _fyy_share_role_authorized(token, role):
        return jsonify({'ok': False, 'error': 'Akses upload ditolak.'}), 403

    file = request.files.get('file')
    if not file or not file.filename:
        return jsonify({'ok': False, 'error': 'Silakan pilih file.'}), 400

    try:
        share = save_fyy_share_file(token, role, file)
    except FyyShareError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except Exception:
        app.logger.exception('FYY Share LAN upload gagal')
        return jsonify({'ok': False, 'error': 'File gagal disiapkan.'}), 500

    return jsonify({
        'ok': True,
        'file': _public_fyy_share_file(token, role, share['files'][role])
    })


@app.route('/fyy-share/file/<token>/<role>/delete', methods=['POST'])
def delete_fyy_share_file(token, role):
    role = (role or '').strip().lower()
    if not _valid_fyy_share_role(role) or not _fyy_share_role_authorized(token, role):
        return jsonify({'ok': False, 'error': 'Akses ditolak.'}), 403

    try:
        delete_fyy_share_file_data(token, role)
    except FyyShareError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 404
    return jsonify({'ok': True})


@app.route('/fyy-share/progress/<token>/<source_role>', methods=['POST'])
def fyy_share_transfer_progress(token, source_role):
    source_role = (source_role or '').strip().lower()
    receiver_role = _fyy_share_other_role(source_role) if _valid_fyy_share_role(source_role) else ''
    if not receiver_role or not _fyy_share_role_authorized(token, receiver_role):
        return jsonify({'ok': False, 'error': 'Akses progres ditolak.'}), 403

    payload = request.get_json(silent=True) or {}
    try:
        received = max(0, int(payload.get('received') or 0))
        total = max(0, int(payload.get('total') or 0))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Nilai byte tidak valid.'}), 400

    state = str(payload.get('state') or 'downloading').strip().lower()
    if state not in {'downloading', 'completed', 'cancelled', 'error'}:
        state = 'downloading'
    transfer_id = str(payload.get('transfer_id') or '').strip()[:80]

    def update(meta):
        file_data = meta['files'][source_role]
        file_size = int(file_data.get('bytes') or 0)
        safe_total = total or file_size
        safe_received = min(received, safe_total) if safe_total else received
        file_data['transfer_state'] = state
        file_data['transferred_bytes'] = safe_received
        file_data['transfer_total'] = safe_total
        file_data['transfer_updated_at'] = datetime.utcnow().isoformat()
        if state == 'completed':
            file_data['transferred_bytes'] = safe_total or file_size
            completed_id = transfer_id or f'{receiver_role}:{datetime.utcnow().isoformat()}'
            if file_data.get('last_completed_id') != completed_id:
                file_data['download_count'] = int(file_data.get('download_count') or 0) + 1
                file_data['last_completed_id'] = completed_id

    share = _mutate_fyy_share(token, update)
    if not share:
        return jsonify({'ok': False, 'expired': True}), 404
    return jsonify({'ok': True})


@app.route('/fyy-share/qr/<token>.png')
def fyy_share_qr(token):
    share = get_active_fyy_share(token)
    if not share:
        abort(404)

    receive_url = build_fyy_share_lan_url(token)
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=9,
        border=4
    )
    qr.add_data(receive_url)
    qr.make(fit=True)
    image = qr.make_image(fill_color='black', back_color='white')
    buffer = BytesIO()
    image.save(buffer, format='PNG')
    buffer.seek(0)
    return send_file(buffer, mimetype='image/png', max_age=0)


@app.route('/fyy-share/download/<token>/<source_role>')
def fyy_share_download(token, source_role):
    source_role = (source_role or '').strip().lower()
    receiver_role = _fyy_share_other_role(source_role) if _valid_fyy_share_role(source_role) else ''
    if not receiver_role or not _fyy_share_role_authorized(token, receiver_role):
        abort(403)

    share = get_active_fyy_share(token)
    if not share:
        abort(404)

    file_data = share['files'][source_role]
    if file_data.get('state') != 'ready':
        abort(404)

    file_path = _fyy_share_dir(token) / (
        file_data.get('stored_name') or f'payload-{source_role}.bin'
    )
    if not file_path.is_file():
        abort(404)

    return send_file(
        file_path,
        mimetype=file_data.get('content_type') or 'application/octet-stream',
        as_attachment=True,
        download_name=file_data.get('filename') or 'fyy-share-file',
        max_age=0,
        conditional=True
    )


@app.route('/fyy-share/delete/<token>', methods=['POST'])
def delete_fyy_share(token):
    if session.get('fyy_share_sender_token') != token:
        abort(403)

    try:
        shutil.rmtree(_fyy_share_dir(token), ignore_errors=True)
    except FyyShareError:
        abort(404)

    session.pop('fyy_share_sender_token', None)
    return redirect(url_for('fyy_share', new='1'))


# ==========================================
# TOOLS — IMAGE SPLITTER (PILLOW)
# ==========================================
class ImageSplitterError(RuntimeError):
    """Kesalahan terkontrol dari Image Splitter."""


def get_image_split_ttl_hours():
    try:
        value = int(os.getenv('IMAGE_SPLIT_TTL_HOURS', '6'))
    except ValueError:
        value = 6
    return max(1, min(value, 24))


def cleanup_expired_image_splits(limit=12):
    now = datetime.utcnow()
    expired = list(db.image_split_sessions.find({'expires_at': {'$lte': now}}).limit(limit))
    for session_doc in expired:
        for item in session_doc.get('items', []):
            file_id = item.get('file_id')
            try:
                split_fs.delete(file_id)
            except Exception:
                pass
        db.image_split_sessions.delete_one({'_id': session_doc['_id']})


def _axis_bounds_by_count(length, count):
    if count < 1 or count > length:
        raise ImageSplitterError('Jumlah blok melebihi ukuran piksel gambar.')
    return [(index * length) // count for index in range(count)] + [length]


def _axis_bounds_by_size(length, block_size):
    if block_size < 1:
        raise ImageSplitterError('Ukuran blok minimal 1 piksel.')
    bounds = list(range(0, length, block_size))
    if not bounds or bounds[-1] != length:
        bounds.append(length)
    return bounds


def _parse_positive_int(value, label, minimum=1, maximum=64):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ImageSplitterError(f'{label} harus berupa angka.')
    if number < minimum or number > maximum:
        raise ImageSplitterError(f'{label} harus antara {minimum} dan {maximum}.')
    return number


def build_image_split_boxes(width, height, form):
    direction = (form.get('direction') or 'vertical').strip().lower()
    split_by = (form.get('split_by') or 'quantity').strip().lower()
    if direction not in {'vertical', 'horizontal', 'grid'}:
        direction = 'vertical'
    if split_by not in {'quantity', 'pixels'}:
        split_by = 'quantity'

    if split_by == 'quantity':
        if direction == 'vertical':
            columns = _parse_positive_int(form.get('quantity'), 'Quantity of blocks', maximum=32)
            rows = 1
        elif direction == 'horizontal':
            rows = _parse_positive_int(form.get('quantity'), 'Quantity of blocks', maximum=32)
            columns = 1
        else:
            columns = _parse_positive_int(form.get('columns'), 'Jumlah kolom', maximum=16)
            rows = _parse_positive_int(form.get('rows'), 'Jumlah baris', maximum=16)
        x_bounds = _axis_bounds_by_count(width, columns)
        y_bounds = _axis_bounds_by_count(height, rows)
    else:
        if direction == 'vertical':
            block_width = _parse_positive_int(form.get('block_width'), 'Lebar blok', maximum=width)
            x_bounds = _axis_bounds_by_size(width, block_width)
            y_bounds = [0, height]
        elif direction == 'horizontal':
            block_height = _parse_positive_int(form.get('block_height'), 'Tinggi blok', maximum=height)
            x_bounds = [0, width]
            y_bounds = _axis_bounds_by_size(height, block_height)
        else:
            block_width = _parse_positive_int(form.get('block_width'), 'Lebar blok', maximum=width)
            block_height = _parse_positive_int(form.get('block_height'), 'Tinggi blok', maximum=height)
            x_bounds = _axis_bounds_by_size(width, block_width)
            y_bounds = _axis_bounds_by_size(height, block_height)

    total = (len(x_bounds) - 1) * (len(y_bounds) - 1)
    if total < 2:
        raise ImageSplitterError('Pengaturan tersebut hanya menghasilkan satu blok.')
    if total > 64:
        raise ImageSplitterError('Maksimal 64 blok dalam sekali proses.')

    boxes = []
    for row_index in range(len(y_bounds) - 1):
        for column_index in range(len(x_bounds) - 1):
            boxes.append({
                'box': (
                    x_bounds[column_index],
                    y_bounds[row_index],
                    x_bounds[column_index + 1],
                    y_bounds[row_index + 1]
                ),
                'row': row_index + 1,
                'column': column_index + 1
            })
    return boxes


def split_image_with_pillow(file_storage, form):
    filename = secure_filename(file_storage.filename or 'image') or 'image'
    image_bytes = file_storage.read()
    if not image_bytes:
        raise ImageSplitterError('File gambar kosong atau tidak dapat dibaca.')
    if len(image_bytes) > 20 * 1024 * 1024:
        raise ImageSplitterError('Ukuran gambar maksimal 20 MB.')

    try:
        with Image.open(BytesIO(image_bytes)) as source:
            image = ImageOps.exif_transpose(source)
            image.load()
            icc_profile = image.info.get('icc_profile')
            if image.mode == 'P' and 'transparency' in image.info:
                image = image.convert('RGBA')
            elif image.mode not in {'1', 'L', 'LA', 'P', 'RGB', 'RGBA'}:
                image = image.convert('RGBA' if 'A' in image.getbands() else 'RGB')
            boxes = build_image_split_boxes(image.width, image.height, form)
            stem = Path(filename).stem or 'image'
            pieces = []

            for item in boxes:
                crop = image.crop(item['box'])
                output = BytesIO()
                save_options = {'format': 'PNG', 'compress_level': 0, 'optimize': False}
                if icc_profile:
                    save_options['icc_profile'] = icc_profile
                crop.save(output, **save_options)
                pieces.append({
                    'filename': f"{stem}-r{item['row']:02d}-c{item['column']:02d}.png",
                    'bytes': output.getvalue(),
                    'row': item['row'],
                    'column': item['column'],
                    'width': crop.width,
                    'height': crop.height
                })
            return pieces, stem
    except UnidentifiedImageError as exc:
        raise ImageSplitterError('File tidak dikenali sebagai gambar yang valid.') from exc
    except ImageSplitterError:
        raise
    except Exception as exc:
        raise ImageSplitterError(f'Gambar gagal dipotong: {type(exc).__name__}.') from exc


def make_image_split_zip(pieces, stem):
    output = BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
        for piece in pieces:
            archive.writestr(piece['filename'], piece['bytes'])
    output.seek(0)
    return output, f'{stem}-split.zip'


def store_image_split_session(pieces, stem):
    token = uuid.uuid4().hex
    now = datetime.utcnow()
    expires_at = now + timedelta(hours=get_image_split_ttl_hours())
    items = []
    stored_ids = []

    try:
        for piece in pieces:
            file_id = split_fs.put(
                piece['bytes'],
                filename=piece['filename'],
                content_type='image/png',
                metadata={'token': token, 'expires_at': expires_at}
            )
            stored_ids.append(file_id)
            items.append({
                'file_id': file_id,
                'filename': piece['filename'],
                'row': piece['row'],
                'column': piece['column'],
                'width': piece['width'],
                'height': piece['height']
            })

        db.image_split_sessions.insert_one({
            'token': token,
            'stem': stem,
            'items': items,
            'created_at': now,
            'expires_at': expires_at
        })
    except Exception:
        for file_id in stored_ids:
            try:
                split_fs.delete(file_id)
            except Exception:
                pass
        raise

    return token


def get_active_image_split_session(token):
    session_doc = db.image_split_sessions.find_one({'token': token})
    if not session_doc:
        return None
    if session_doc.get('expires_at') and session_doc['expires_at'] <= datetime.utcnow():
        cleanup_expired_image_splits()
        return None
    return session_doc


@app.route('/image-splitter', methods=['GET', 'POST'])
def image_splitter():
    cleanup_expired_image_splits()
    error = ''
    form_values = {
        'direction': 'vertical',
        'split_by': 'quantity',
        'quantity': '2',
        'columns': '2',
        'rows': '2',
        'block_width': '500',
        'block_height': '500',
        'output_mode': 'zip'
    }

    if request.method == 'POST':
        form_values.update({key: request.form.get(key, form_values[key]) for key in form_values})
        file = request.files.get('image')
        if not file or not file.filename:
            error = 'Silakan pilih gambar terlebih dahulu.'
        else:
            try:
                pieces, stem = split_image_with_pillow(file, request.form)
                if request.form.get('output_mode') == 'individual':
                    token = store_image_split_session(pieces, stem)
                    return redirect(url_for('image_splitter_result', token=token))

                zip_buffer, zip_name = make_image_split_zip(pieces, stem)
                return send_file(
                    zip_buffer,
                    mimetype='application/zip',
                    as_attachment=True,
                    download_name=zip_name,
                    max_age=0
                )
            except ImageSplitterError as exc:
                error = str(exc)
            except Exception:
                app.logger.exception('Image Splitter gagal')
                error = 'Image Splitter gagal memproses gambar.'

    return render_template(
        'image_splitter.html',
        error=error,
        form_values=form_values,
        ttl_hours=get_image_split_ttl_hours()
    )


@app.route('/image-splitter/result/<token>')
def image_splitter_result(token):
    cleanup_expired_image_splits()
    session_doc = get_active_image_split_session(token)
    if not session_doc:
        abort(404)
    return render_template('image_splitter_result.html', split_session=session_doc)


@app.route('/image-splitter/file/<token>/<file_id>')
def image_splitter_file(token, file_id):
    session_doc = get_active_image_split_session(token)
    if not session_doc:
        abort(404)
    try:
        object_id = ObjectId(file_id)
    except Exception:
        abort(404)

    allowed = next((item for item in session_doc.get('items', []) if item.get('file_id') == object_id), None)
    if not allowed:
        abort(404)

    try:
        grid_file = split_fs.get(object_id)
    except Exception:
        abort(404)

    inline = request.args.get('view') == '1'
    return send_file(
        BytesIO(grid_file.read()),
        mimetype='image/png',
        as_attachment=not inline,
        download_name=allowed.get('filename') or 'split.png',
        max_age=0
    )


@app.route('/image-splitter/zip/<token>')
def image_splitter_zip(token):
    session_doc = get_active_image_split_session(token)
    if not session_doc:
        abort(404)

    pieces = []
    for item in session_doc.get('items', []):
        try:
            grid_file = split_fs.get(item['file_id'])
            pieces.append({'filename': item['filename'], 'bytes': grid_file.read()})
        except Exception:
            abort(404)

    zip_buffer, zip_name = make_image_split_zip(pieces, session_doc.get('stem') or 'image')
    return send_file(
        zip_buffer,
        mimetype='application/zip',
        as_attachment=True,
        download_name=zip_name,
        max_age=0
    )

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=True)
