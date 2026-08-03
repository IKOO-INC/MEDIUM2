# Perubahan mediumfix

## Yang diubah

- Seluruh file HTML di folder `templates/` diubah tampilannya menggunakan gaya Adminator.
- `templates/base.html` diubah menjadi layout Adminator dengan sidebar, topbar, footer, dark mode, dan navigasi responsif.
- Asset tema ditempatkan di `static/adminator/`.
- Ditambahkan `static/adminator/medium.css` untuk penyesuaian komponen khusus project.
- Ditambahkan `static/adminator/medium.js` untuk dark mode, tanggal, dan sidebar mobile.
- File `2026-original.js` disimpan sebagai referensi asset asli template, tetapi tidak dimuat karena berisi menu dan link halaman demo Adminator.

## Yang tidak diubah

- `app.py`
- Semua route Flask
- Koneksi dan query MongoDB
- Nama field form
- Nama variabel Jinja
- Struktur data dan logika aplikasi
- `requirements.txt`

## Catatan

Tidak ada perubahan kode Python. Semua penyesuaian berada di HTML, CSS, dan JavaScript tampilan.

## Update Fitur Absensi QR

### Route baru
- `GET/POST /attendance/manual` — form absensi manual.
- `GET /attendance/scan` — halaman scanner kamera QR.
- `GET/POST /attendance/scan/<qr_token>` — konfirmasi anggota hasil scan dan input keterangan.
- `GET /members/qr` — halaman cetak semua QR anggota.
- `GET /members/qr/<qr_token>.png` — gambar QR individual / unduh PNG.

### Perubahan data
- Setiap anggota sekarang memiliki field `qr_token` unik.
- Anggota lama yang masih berupa string atau belum memiliki token akan dimigrasikan otomatis saat halaman anggota/absensi dibuka.
- Catatan absensi baru memiliki field `method` dengan nilai `manual` atau `scan`.
- Absensi hasil scan juga menyimpan `qr_token` anggota.

### File baru
- `templates/attendance_manual.html`
- `templates/attendance_scan.html`
- `templates/attendance_scan_member.html`
- `templates/member_qr.html`

### File yang diperbarui
- `app.py` — helper QR, migrasi anggota, route terpisah, generator QR PNG.
- `templates/base.html` — menu Rekap Absensi, Absen Manual, dan Scan QR dipisahkan.
- `templates/attendance.html` — menjadi halaman rekap/statistik serta menampilkan metode absensi.
- `templates/members.html` — tombol cetak/unduh QR.
- `static/adminator/medium.css` — tampilan scanner, konfirmasi, kartu QR, dan layout cetak.
- `requirements.txt` — menambahkan `qrcode[pil]==8.2`.

### Catatan scanner
- Scanner menggunakan `html5-qrcode` dari CDN dan memiliki fallback BarcodeDetector bawaan browser serta input URL/token manual.
- Kamera browser umumnya memerlukan HTTPS saat aplikasi dipasang di domain publik. `localhost` tetap dapat digunakan saat pengembangan.

## 2026-08-02 — Perbaikan layout Grid QR Scan

- Memperbaiki bug layout pada halaman Scan QR dan halaman lain yang menggunakan kelas grid `col-4`, `col-5`, `col-7`, atau `col-8`.
- Menambahkan definisi span 4/5/7/8 kolom pada CSS custom.
- Menambahkan breakpoint responsif agar kolom-kolom tersebut otomatis menjadi full-width pada layar <= 1100px.
- Tidak mengubah `app.py`, route, database, atau logika absensi.


## 2026-08-03 — Migrasi Upload Assets ke Cloudinary

- Upload asset baru sekarang dikirim langsung dari Flask ke Cloudinary menggunakan signed upload.
- MongoDB menyimpan metadata asset, `cloudinary_public_id`, `cloudinary_resource_type`, URL HTTPS, ukuran file, dan content type.
- Asset lama yang masih tersimpan lokal tetap dapat ditampilkan dan diunduh melalui route kompatibilitas.
- Ditambahkan route `GET /assets/download/<asset_id>` untuk tombol Download. Untuk asset Cloudinary, route mengarahkan ke URL delivery Cloudinary dengan `fl_attachment`; untuk asset lokal lama, route menggunakan `send_from_directory`.
- Ditambahkan konfigurasi environment pada `.env.example`.
- Ditambahkan dependency `requests` untuk komunikasi Upload API Cloudinary.
- Tidak ada perubahan pada route dashboard, anggota, QR, absensi, kas, project, atau DB Console.

### Konfigurasi yang diperlukan

Isi environment berikut sebelum upload: `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET`. Alternatifnya dapat menggunakan `CLOUDINARY_URL=cloudinary://API_KEY:API_SECRET@CLOUD_NAME`. Folder default adalah `media-roudlotul-ulum`.


## Update — Cloudinary Python SDK

- Mengganti upload langsung menggunakan HTTP `requests` dan signature manual dengan **Cloudinary Python SDK** (`cloudinary`).
- Upload dilakukan melalui `cloudinary.uploader.upload(...)` dengan `resource_type='auto'`, sehingga gambar, video, dan file raw dapat diproses sesuai tipe asset Cloudinary.
- Konfigurasi SDK menggunakan `cloudinary.config(...)` dan tetap mendukung `CLOUDINARY_URL` maupun tiga environment variable terpisah.
- `requests` dan kode pembentukan signature SHA-1 manual dihapus dari jalur upload.
- Metadata hasil upload tetap disimpan di MongoDB seperti sebelumnya.
- Tombol download dan kompatibilitas asset lokal lama tetap dipertahankan.

Catatan: SDK Cloudinary akan membuat signature/authentication upload di sisi server berdasarkan kredensial Cloudinary yang dikonfigurasi.

## 2026-08-03 — Update fitur lanjutan

- Memperbaiki download asset Cloudinary: file sekarang diambil melalui server Flask dan dikirim sebagai attachment, tidak lagi mengandalkan URL transformasi `fl_attachment`.
- Menambahkan pencarian live pada halaman Assets berdasarkan nama, deskripsi, format, content type, dan lokasi penyimpanan.
- Mengubah input deskripsi task menjadi textarea dan mempertahankan newline saat ditampilkan.
- Menambahkan daftar kontributor saat membuat task dan route `POST /task/contributors/<task_id>` untuk memperbarui kontributor task lama maupun baru.
- Menambahkan halaman terpisah `GET/POST /announcements` serta route hapus pengumuman. Pengumuman tidak ditampilkan pada dashboard.
- Menambahkan menu FYY AI dengan route `/fyy-ai`, `/fyy-ai/chat`, dan `/fyy-ai/clear`. Chat menggunakan OpenAI Responses API dan model default `chat-latest`.
- Riwayat FYY AI disimpan sementara di koleksi MongoDB `ai_chat_logs` menggunakan TTL, default 24 jam.
- Menambahkan dependency `openai` dan environment `OPENAI_API_KEY`, `OPENAI_MODEL`, `SECRET_KEY`, serta `FYY_AI_LOG_TTL_HOURS`.

## 2026-08-03 — Migrasi FYY AI ke Gemini Flash Free Tier

- Menghapus integrasi OpenAI Responses API dan dependency `openai`.
- Menggunakan SDK resmi Google `google-genai`.
- Model default diubah menjadi `gemini-2.5-flash`.
- Environment FYY AI diubah menjadi `GEMINI_API_KEY` dan `GEMINI_MODEL`.
- Riwayat chat sementara di MongoDB, TTL, route, serta tampilan FYY AI tetap dipertahankan.
- Menambahkan konversi role percakapan MongoDB ke format Gemini (`assistant` menjadi `model`).
- Menambahkan pesan khusus ketika kuota Free Tier habis, API key tidak valid, atau model salah.
- Free Tier tetap memiliki batas kuota. Jika kuota habis, chatbot menampilkan pesan untuk menunggu dan mencoba kembali.
