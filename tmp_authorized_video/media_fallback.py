import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

VIDEO_ID = 'ClPY7_mYcoo'
WORK = 'work'
os.makedirs(WORK, exist_ok=True)
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/151 Safari/537.36',
    'Accept': 'application/json,text/plain,*/*',
}


def log(message):
    print(message, flush=True)


def fetch_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as response:
        body = response.read()
    data = json.loads(body.decode('utf-8'))
    if not isinstance(data, dict):
        raise RuntimeError('API response is not an object')
    if data.get('error'):
        raise RuntimeError(str(data['error']))
    return data


def clean_files():
    for name in ('source.mp4', 'bridge-video.mp4', 'bridge-audio.m4a', 'bridge-audio.webm'):
        try:
            os.remove(os.path.join(WORK, name))
        except FileNotFoundError:
            pass


def curl_download(url, path, referer=None):
    cmd = [
        'curl', '-L', '--fail', '--show-error', '--silent',
        '--retry', '4', '--retry-all-errors', '--retry-delay', '2',
        '--connect-timeout', '20', '--max-time', '900',
        '-A', HEADERS['User-Agent'],
    ]
    if referer:
        cmd += ['-e', referer]
    cmd += ['-o', path, url]
    log(f'Downloading {os.path.basename(path)} from {url[:140]}')
    subprocess.run(cmd, check=True)
    if not os.path.isfile(path) or os.path.getsize(path) < 1024:
        raise RuntimeError(f'Downloaded file is missing or too small: {path}')


def stream_count(path, kind):
    result = subprocess.run([
        'ffprobe', '-v', 'error', '-select_streams', kind,
        '-show_entries', 'stream=index', '-of', 'csv=p=0', path,
    ], text=True, capture_output=True, check=False)
    return len([line for line in result.stdout.splitlines() if line.strip()])


def valid_progressive(path):
    return stream_count(path, 'v') > 0 and stream_count(path, 'a') > 0


def valid_video(path):
    return stream_count(path, 'v') > 0


def valid_audio(path):
    return stream_count(path, 'a') > 0


def merge(video_path, audio_path, output_path):
    subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'warning', '-y',
        '-i', video_path, '-i', audio_path,
        '-map', '0:v:0', '-map', '1:a:0', '-c', 'copy',
        '-movflags', '+faststart', output_path,
    ], check=True)
    if not valid_progressive(output_path):
        raise RuntimeError('Merged file has no video or audio stream')


def get_height(item):
    for key in ('height',):
        try:
            value = int(item.get(key) or 0)
            if value:
                return value
        except Exception:
            pass
    text = str(item.get('qualityLabel') or item.get('resolution') or item.get('quality') or '')
    digits = ''
    for ch in text:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    try:
        return int(digits)
    except Exception:
        return 0


def absolute_invidious_url(base, url):
    if url.startswith('//'):
        return 'https:' + url
    if url.startswith('/'):
        return urllib.parse.urljoin(base + '/', url.lstrip('/'))
    return url


def try_invidious(base):
    api = f'{base}/api/v1/videos/{VIDEO_ID}'
    log(f'Invidious API: {api}')
    data = fetch_json(api)
    with open(os.path.join(WORK, 'bridge.info.json'), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    progressive = []
    for item in data.get('formatStreams') or []:
        url = item.get('url')
        height = get_height(item)
        mime = str(item.get('type') or '').lower()
        container = str(item.get('container') or '').lower()
        if url and 0 < height <= 720 and ('video/mp4' in mime or container == 'mp4'):
            progressive.append((height, int(item.get('bitrate') or 0), item))
    progressive.sort(key=lambda row: (row[0], row[1]), reverse=True)
    for _, _, item in progressive:
        clean_files()
        url = absolute_invidious_url(base, item['url'])
        try:
            curl_download(url, os.path.join(WORK, 'source.mp4'), referer=base + '/')
            if valid_progressive(os.path.join(WORK, 'source.mp4')):
                log('Invidious progressive stream succeeded')
                return True
        except Exception as exc:
            log(f'Invidious progressive candidate failed: {exc}')

    videos = []
    audios = []
    for item in data.get('adaptiveFormats') or []:
        url = item.get('url')
        mime = str(item.get('type') or '').lower()
        if not url:
            continue
        if 'video/mp4' in mime:
            height = get_height(item)
            if 0 < height <= 720:
                videos.append((height, int(item.get('bitrate') or 0), item))
        elif 'audio/mp4' in mime:
            audios.append((int(item.get('bitrate') or 0), item))
    videos.sort(key=lambda row: (row[0], row[1]), reverse=True)
    audios.sort(key=lambda row: row[0], reverse=True)
    for _, _, video in videos[:3]:
        for _, audio in audios[:3]:
            clean_files()
            try:
                vurl = absolute_invidious_url(base, video['url'])
                aurl = absolute_invidious_url(base, audio['url'])
                vpath = os.path.join(WORK, 'bridge-video.mp4')
                apath = os.path.join(WORK, 'bridge-audio.m4a')
                curl_download(vurl, vpath, referer=base + '/')
                curl_download(aurl, apath, referer=base + '/')
                if not valid_video(vpath) or not valid_audio(apath):
                    raise RuntimeError('Downloaded adaptive streams are invalid')
                merge(vpath, apath, os.path.join(WORK, 'source.mp4'))
                log('Invidious adaptive streams succeeded')
                return True
            except Exception as exc:
                log(f'Invidious adaptive candidate failed: {exc}')
    raise RuntimeError('No working Invidious MP4 stream candidate')


def piped_is_mp4(item):
    text = ' '.join(str(item.get(key) or '') for key in ('format', 'mimeType', 'codec')).lower()
    return ('mpeg_4' in text or 'mp4' in text or 'avc1' in text or 'h264' in text)


def try_piped(base):
    api = f'{base}/streams/{VIDEO_ID}'
    log(f'Piped API: {api}')
    data = fetch_json(api)
    with open(os.path.join(WORK, 'bridge.info.json'), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    progressive = []
    videos = []
    for item in data.get('videoStreams') or []:
        url = item.get('url')
        height = get_height(item)
        if not url or not (0 < height <= 720) or not piped_is_mp4(item):
            continue
        row = (height, int(item.get('bitrate') or 0), item)
        if item.get('videoOnly') is False:
            progressive.append(row)
        else:
            videos.append(row)
    progressive.sort(key=lambda row: (row[0], row[1]), reverse=True)
    videos.sort(key=lambda row: (row[0], row[1]), reverse=True)

    for _, _, item in progressive:
        clean_files()
        try:
            curl_download(item['url'], os.path.join(WORK, 'source.mp4'), referer=base + '/')
            if valid_progressive(os.path.join(WORK, 'source.mp4')):
                log('Piped progressive stream succeeded')
                return True
        except Exception as exc:
            log(f'Piped progressive candidate failed: {exc}')

    audios = []
    for item in data.get('audioStreams') or []:
        url = item.get('url')
        text = ' '.join(str(item.get(key) or '') for key in ('format', 'mimeType', 'codec')).lower()
        if url and ('m4a' in text or 'mp4' in text or 'mp4a' in text):
            audios.append((int(item.get('bitrate') or 0), item))
    audios.sort(key=lambda row: row[0], reverse=True)

    for _, _, video in videos[:4]:
        for _, audio in audios[:4]:
            clean_files()
            try:
                vpath = os.path.join(WORK, 'bridge-video.mp4')
                apath = os.path.join(WORK, 'bridge-audio.m4a')
                curl_download(video['url'], vpath, referer=base + '/')
                curl_download(audio['url'], apath, referer=base + '/')
                if not valid_video(vpath) or not valid_audio(apath):
                    raise RuntimeError('Downloaded Piped streams are invalid')
                merge(vpath, apath, os.path.join(WORK, 'source.mp4'))
                log('Piped adaptive streams succeeded')
                return True
            except Exception as exc:
                log(f'Piped adaptive candidate failed: {exc}')
    raise RuntimeError('No working Piped MP4 stream candidate')


invidious_instances = [
    'https://invidious.tiekoetter.com',
    'https://yt.chocolatemoo53.com',
    'https://inv.nadeko.net',
    'https://invidious.f5.si',
    'https://invidious.nerdvpn.de',
]
piped_instances = [
    'https://pipedapi.kavin.rocks',
    'https://pipedapi.tokhmi.xyz',
    'https://pipedapi.moomoo.me',
    'https://pipedapi.syncpundit.io',
    'https://api-piped.mha.fi',
    'https://piped-api.garudalinux.org',
    'https://pipedapi.rivo.lol',
    'https://pipedapi.leptons.xyz',
]

errors = []
for base in invidious_instances:
    try:
        if try_invidious(base):
            sys.exit(0)
    except Exception as exc:
        errors.append(f'{base}: {exc}')
        log(f'Invidious instance failed: {base}: {exc}')
        clean_files()

for base in piped_instances:
    try:
        if try_piped(base):
            sys.exit(0)
    except Exception as exc:
        errors.append(f'{base}: {exc}')
        log(f'Piped instance failed: {base}: {exc}')
        clean_files()

log('All bridge instances failed:')
for error in errors:
    log('  ' + error)
raise SystemExit(1)
