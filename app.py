from flask import Flask, request, render_template, send_file, jsonify
import yt_dlp
import os
import threading
import uuid
import re
import sqlite3 # YENİ: Veritabanı kütüphanesi
import zipfile
import shutil

app = Flask(__name__)

DOWNLOAD_FOLDER = "/home/kandemir/Downloads"
DB_NAME = "downloads_history.db" # YENİ: Veritabanı dosyamız

if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

# YENİ: Veritabanını ve tabloyu oluşturma
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            thumbnail TEXT,
            url TEXT,
            format_type TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

# Uygulama başlarken veritabanını kur
init_db()

tasks = {}

def download_worker(task_id, video_url, format_type):
    tasks[task_id] = {'status': 'starting', 'percent': 0, 'filename': None, 'error': None}
    
    def my_hook(d):
        if d['status'] == 'downloading':
            percent_str = d.get('_percent_str', '0.0%')
            percent_str = re.sub(r'\x1b[^m]*m', '', percent_str).replace('%', '').strip()
            try:
                tasks[task_id]['percent'] = float(percent_str)
            except ValueError:
                pass
        elif d['status'] == 'finished':
            tasks[task_id]['percent'] = 100
            # DİKKAT: Geçici dosya adını almamak için filename atamasını buradan sildik!

    if format_type == 'video':
        ydl_format = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
    else:
        ydl_format = 'bestaudio/best'

    ydl_opts = {
        'format': ydl_format,
        'outtmpl': f'{DOWNLOAD_FOLDER}/%(title)s.%(ext)s',
        'noplaylist': True,
        'color': 'never',
        'progress_hooks': [my_hook],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            
            # YENİ: İndirme ve birleştirme tamamen bittikten sonra KESİN dosya adını alıyoruz
            if 'requested_downloads' in info:
                tasks[task_id]['filename'] = info['requested_downloads'][0]['filepath']
            else:
                tasks[task_id]['filename'] = ydl.prepare_filename(info)
                
            tasks[task_id]['status'] = 'ready'
    except Exception as e:
        tasks[task_id]['status'] = 'error'
        tasks[task_id]['error'] = str(e)


# YENİ VE GÜNCELLENMİŞ: Toplu İndirme İş Parçacığı (Veritabanı Kayıtlı)
def download_batch_worker(task_id, music_list, format_type):
    tasks[task_id] = {'status': 'starting', 'percent': 0, 'filename': None, 'error': None, 'detail': 'Hazırlanıyor...'}
    
    batch_folder = os.path.join(DOWNLOAD_FOLDER, task_id)
    os.makedirs(batch_folder, exist_ok=True)
    
    total_songs = len(music_list)
    
    if format_type == 'video':
        ydl_format = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
    else:
        ydl_format = 'bestaudio/best'

    for index, music_name in enumerate(music_list):
        tasks[task_id]['detail'] = f'({index + 1}/{total_songs}) İndiriliyor: {music_name}'
        tasks[task_id]['percent'] = int((index / total_songs) * 100) 
        
        ydl_opts = {
            'format': ydl_format,
            'outtmpl': f'{batch_folder}/%(title)s.%(ext)s',
            'default_search': 'ytsearch1',
            'noplaylist': True,
            'quiet': True,
            'noprogress': True
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Sadece indirmekle kalma, info objesini de çek (DB için)
                info = ydl.extract_info(music_name, download=True)
                
                if 'entries' in info and len(info['entries']) > 0:
                    video_info = info['entries'][0]
                else:
                    video_info = info
                
                # Veritabanı için gerekli verileri ayıkla
                db_title = video_info.get('title', 'Bilinmeyen Başlık')
                db_thumbnail = video_info.get('thumbnail', '')
                db_url = video_info.get('webpage_url', video_info.get('url', music_name))

                # İnen her şarkıyı veritabanına kaydet
                conn = sqlite3.connect(DB_NAME)
                c = conn.cursor()
                c.execute('INSERT INTO history (title, thumbnail, url, format_type) VALUES (?, ?, ?, ?)', 
                          (db_title, db_thumbnail, db_url, format_type))
                conn.commit()
                conn.close()

        except Exception as e:
            print(f"Hata ({music_name}): {e}")
            continue 
            
    tasks[task_id]['detail'] = 'Dosyalar birleştiriliyor (ZIP oluşturuluyor)...'
    zip_filename = f'{DOWNLOAD_FOLDER}/Playlist_{task_id[:8]}.zip'
    
    shutil.make_archive(zip_filename.replace('.zip', ''), 'zip', batch_folder)
    shutil.rmtree(batch_folder)
    
    tasks[task_id]['percent'] = 100
    tasks[task_id]['filename'] = zip_filename
    tasks[task_id]['status'] = 'ready'


    

# YENİ: Toplu indirme isteğini karşılayan uç nokta
@app.route('/start_batch', methods=['POST'])
def start_batch():
    music_list_raw = request.form.get('music_list')
    format_type = request.form.get('format_type', 'audio')
    
    if not music_list_raw:
        return jsonify({'error': 'Liste boş olamaz.'}), 400
        
    # Gelen metni satır satır böl ve boş satırları temizle
    music_list = [m.strip() for m in music_list_raw.split('\n') if m.strip()]
    
    if not music_list:
        return jsonify({'error': 'Geçerli bir müzik adı bulunamadı.'}), 400

    task_id = str(uuid.uuid4())
    # Toplu indirme iş parçacığını başlat
    thread = threading.Thread(target=download_batch_worker, args=(task_id, music_list, format_type))
    thread.start()
    
    return jsonify({'task_id': task_id})






@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

# YENİ: Veritabanından en çok indirilen (popüler) 5 kaydı getirir
@app.route('/popular', methods=['GET'])
def get_popular():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # SQL ile URL'leri grupla, sayısını bul (d_count) ve en çoktan en aza sırala
    c.execute('''
        SELECT title, thumbnail, url, COUNT(url) as d_count 
        FROM history 
        GROUP BY url 
        ORDER BY d_count DESC 
        LIMIT 5
    ''')
    rows = c.fetchall()
    conn.close()
    
    # Şarkı sayısını (count) da sözlüğe ekliyoruz
    popular_list = [{'title': row[0], 'thumbnail': row[1], 'url': row[2], 'count': row[3]} for row in rows]
    return jsonify(popular_list)


@app.route('/search', methods=['POST'])
def search_video():
    music_name = request.form.get('music_name')
    if not music_name:
        return jsonify({'error': 'Lütfen geçerli bir müzik adı girin.'}), 400

    ydl_opts = {
        'default_search': 'ytsearch1',
        'noplaylist': True,
        'quiet': True, 
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(music_name, download=False)
            if 'entries' in info and len(info['entries']) > 0:
                video_info = info['entries'][0]
            else:
                video_info = info
            
            return jsonify({
                'title': video_info.get('title'),
                'thumbnail': video_info.get('thumbnail'),
                'url': video_info.get('webpage_url', video_info.get('url'))
            })
    except Exception as e:
        return jsonify({'error': 'Arama sırasında bir hata oluştu: ' + str(e)}), 500

@app.route('/start', methods=['POST'])
def start():
    video_url = request.form.get('url')
    format_type = request.form.get('format_type', 'audio')
    
    # YENİ: Veritabanına kaydetmek için başlık ve kapak bilgisini de alıyoruz
    title = request.form.get('title', 'Bilinmeyen Başlık')
    thumbnail = request.form.get('thumbnail', '')
    
    if not video_url:
        return jsonify({'error': 'Geçerli bir URL bulunamadı.'}), 400

    # YENİ: İndirme talebini veritabanına kaydet
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('INSERT INTO history (title, thumbnail, url, format_type) VALUES (?, ?, ?, ?)', 
              (title, thumbnail, video_url, format_type))
    conn.commit()
    conn.close()

    task_id = str(uuid.uuid4())
    thread = threading.Thread(target=download_worker, args=(task_id, video_url, format_type))
    thread.start()
    
    return jsonify({'task_id': task_id})

@app.route('/progress/<task_id>', methods=['GET'])
def progress(task_id):
    task = tasks.get(task_id)
    if not task:
        return jsonify({'error': 'Görev bulunamadı'}), 404
    return jsonify(task)

@app.route('/get_file/<task_id>', methods=['GET'])
def get_file(task_id):
    task = tasks.get(task_id)
    if not task or task['status'] != 'ready':
        return "Dosya henüz hazır değil", 400
    return send_file(task['filename'], as_attachment=True)

if __name__ == '__main__':
    app.run(debug=True,port=2000)