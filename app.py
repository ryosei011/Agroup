from flask import Flask, jsonify, request, render_template, session, redirect, url_for
from urllib.parse import urlparse, urljoin, quote
from functools import wraps
import json
import os
import urllib.request
import math
from datetime import datetime, timedelta, timezone

# app.py はプロジェクト直下に置く。
# 実体（templates / static / data）は bousai_app/ 配下にあるので、そこを参照する。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(BASE_DIR, 'bousai_app')

app = Flask(
    __name__,
    template_folder=os.path.join(APP_DIR, 'templates'),
    static_folder=os.path.join(APP_DIR, 'static'),
)
app.secret_key = 'your-secret-key-here'

# 管理者認証情報
ADMIN_CREDENTIALS = {
    'admin': '123'
}

# ────────────────────────────────
# 気象警報・注意報設定
PREFECTURE_CODE = "020000"  # 青森県
AREA_NAME = "青森市"

# 青森市の市区町村コード
AREA_CODE = "0220100"

WARNING_URL = (
    f"https://www.jma.go.jp/bosai/warning/data/r8/{PREFECTURE_CODE}.json"
)

JST = timezone(timedelta(hours=9))

# 警報・注意報のコード一覧
WARNING_CODES = {
    "00": "解除",
    "02": "暴風雪警報",
    "03": "レベル3大雨警報",
    "04": "洪水警報",
    "05": "暴風警報",
    "06": "大雪警報",
    "07": "波浪警報",
    "08": "レベル3高潮警報",
    "09": "レベル3土砂災害警報",
    "10": "レベル2大雨注意報",
    "12": "大雪注意報",
    "13": "風雪注意報",
    "14": "雷注意報",
    "15": "強風注意報",
    "16": "波浪注意報",
    "17": "融雪注意報",
    "18": "洪水注意報",
    "19": "レベル2高潮注意報",
    "20": "濃霧注意報",
    "21": "乾燥注意報",
    "22": "なだれ注意報",
    "23": "低温注意報",
    "24": "霜注意報",
    "25": "着氷注意報",
    "26": "着雪注意報",
    "27": "その他の注意報",
    "29": "レベル2土砂災害注意報",
    "32": "暴風雪特別警報",
    "33": "レベル5大雨特別警報",
    "35": "暴風特別警報",
    "36": "大雪特別警報",
    "37": "波浪特別警報",
    "38": "レベル5高潮特別警報",
    "39": "レベル5土砂災害特別警報",
    "43": "レベル4大雨危険警報",
    "48": "レベル4高潮危険警報",
    "49": "レベル4土砂災害危険警報"
}

# ────────────────────────────────
# サンプルデータの読み込み
DATA_FILE = os.path.join(APP_DIR, 'data', 'shelters.json')
INSTRUCTIONS_FILE = os.path.join(APP_DIR, 'data', 'instructions.json')

def load_json(path, default):
    """JSONファイルを読み込む（存在しない・壊れている場合は default を返す）"""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

shelters = load_json(DATA_FILE, [])
instructions = load_json(INSTRUCTIONS_FILE, [])

STATUS_VALUES = ('◎', '○', '△', '×')
SORT_FIELDS = {
    '混雑状況': 'crowd_status',
    '物資状況': 'supply_status',
    '被害状況': 'damage_status',
}
CRITERIA_ALIASES = {
    '混雑度': '混雑状況',
    '物資充実度': '物資状況',
    '被災度': '被害状況',
}
STATUS_RANK = {status: rank for rank, status in enumerate(STATUS_VALUES)}
RESULTS_PER_PAGE = 5
GEOCODE_CACHE = {}


def save_shelters():
    """避難所データを保存する"""
    try:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(shelters, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def parse_coordinate(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def geocode_address(address):
    """住所をジオコードする。未設定・失敗時は座標を推測しない。"""
    address = (address or '').strip()
    if not address:
        return None, None
    if address in GEOCODE_CACHE:
        return GEOCODE_CACHE[address]

    try:
        query = quote(address)
        url = f'https://nominatim.openstreetmap.org/search?q={query}&format=json&limit=1'
        request = urllib.request.Request(url, headers={'User-Agent': 'bousai-app/1.0'})
        with urllib.request.urlopen(request, timeout=5) as response:
            matches = json.loads(response.read())
        if matches:
            coordinates = (
                parse_coordinate(matches[0].get('lat')),
                parse_coordinate(matches[0].get('lon'))
            )
            GEOCODE_CACHE[address] = coordinates
            return coordinates
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return None, None


def get_reference_coordinates():
    latitude = parse_coordinate(os.environ.get('BOUSAI_REFERENCE_LATITUDE'))
    longitude = parse_coordinate(os.environ.get('BOUSAI_REFERENCE_LONGITUDE'))
    if latitude is not None and longitude is not None:
        return latitude, longitude, os.environ.get('BOUSAI_REFERENCE_ADDRESS', '')

    address = os.environ.get('BOUSAI_REFERENCE_ADDRESS', '').strip()
    if address:
        latitude, longitude = geocode_address(address)
        return latitude, longitude, address
    return None, None, ''


def haversine_km(latitude1, longitude1, latitude2, longitude2):
    radius_km = 6371.0088
    lat1, lat2 = math.radians(latitude1), math.radians(latitude2)
    delta_lat = math.radians(latitude2 - latitude1)
    delta_lon = math.radians(longitude2 - longitude1)
    value = (math.sin(delta_lat / 2) ** 2
             + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2)
    return radius_km * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def distance_status(distance_km):
    if distance_km is None:
        return '×'
    if distance_km <= 1:
        return '◎'
    if distance_km <= 5:
        return '○'
    return '△'


def enrich_shelter(shelter, reference_coordinates):
    item = dict(shelter)
    latitude = parse_coordinate(item.get('latitude'))
    longitude = parse_coordinate(item.get('longitude'))
    address = item.get('address', '').strip()
    if (latitude is None or longitude is None) and address:
        latitude, longitude = geocode_address(address)
        if latitude is not None and longitude is not None:
            item['latitude'] = latitude
            item['longitude'] = longitude

    distance_km = None
    if reference_coordinates[0] is not None and latitude is not None and longitude is not None:
        distance_km = haversine_km(
            reference_coordinates[0], reference_coordinates[1], latitude, longitude
        )
    item['distance_km'] = distance_km
    item['distance_status'] = distance_status(distance_km)
    item['latitude'] = latitude
    item['longitude'] = longitude
    return item


def get_map_shelters():
    """住所を座標へ変換し、地図表示用の避難所データを返す"""
    map_items = []
    for shelter in shelters:
        item = dict(shelter)
        latitude = parse_coordinate(item.get('latitude'))
        longitude = parse_coordinate(item.get('longitude'))
        if (latitude is None or longitude is None) and item.get('address'):
            latitude, longitude = geocode_address(item['address'])
        if latitude is None or longitude is None:
            continue
        item['latitude'] = latitude
        item['longitude'] = longitude
        map_items.append(item)
    return map_items

def save_instructions():
    """指示ボードのデータをファイルに保存する"""
    try:
        with open(INSTRUCTIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(instructions, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False
# ────────────────────────────────

# ────────────────────────────────
# 認証関連の設定とヘルパー関数
def is_safe_url(target):
    """リダイレクト先URLが安全かどうかチェック"""
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc

def login_required(f):
    """認証が必要なページに付けるデコレータ"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            # 現在のURLをnextパラメータとしてログイン画面にリダイレクト
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def get_japan_time():
    """日本時間（JST）の現在時刻を取得する"""
    return datetime.now(JST).strftime("%Y年%m月%d日 %H:%M")


def format_report_time(iso_str):
    """気象庁の発表時刻（ISO形式）をJSTの表示用文字列に変換する"""
    if not iso_str:
        return "不明"
    try:
        parsed = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        if parsed.tzinfo:
            parsed = parsed.astimezone(JST)
        return parsed.strftime("%Y年%m月%d日 %H:%M")
    except ValueError:
        return iso_str


def filter_shelters(district=None):
    """district 指定があれば一致する避難所のみ、なければ全件を返す"""
    return [s for s in shelters if not district or s.get('district') == district]


def parse_area_warnings(warning_data):
    """気象庁の新形式JSONから対象市区町村の発表・継続中の情報を抽出する"""
    if not isinstance(warning_data, list):
        raise ValueError("気象庁の警報・注意報データが新形式の配列ではありません")

    warnings = []
    seen_codes = set()
    report_datetimes = []

    for report in warning_data:
        if not isinstance(report, dict):
            continue

        report_datetime = report.get("reportDatetime")
        if isinstance(report_datetime, str) and report_datetime:
            report_datetimes.append(report_datetime)

        warning = report.get("warning")
        if not isinstance(warning, dict):
            continue

        class20_items = warning.get("class20Items", [])
        if not isinstance(class20_items, list):
            continue

        area = next(
            (
                item for item in class20_items
                if isinstance(item, dict)
                and item.get("areaCode") == AREA_CODE
            ),
            None
        )
        if not area:
            continue

        kinds = area.get("kinds", [])
        if not isinstance(kinds, list):
            continue

        for kind in kinds:
            if not isinstance(kind, dict):
                continue

            status = kind.get("status", "")
            code = kind.get("code", "")
            if status not in ("発表", "継続") or not code or code in seen_codes:
                continue

            warnings.append({
                "name": WARNING_CODES.get(
                    code,
                    f"不明な警報・注意報 (コード: {code})"
                ),
                "code": code,
                "status": status
            })
            seen_codes.add(code)

    latest_report_datetime = max(report_datetimes, default="")
    return warnings, latest_report_datetime


def get_weather_warnings():
    """対象市区町村の警報・注意報を取得する"""
    try:
        # 青森県の新形式（令和8年～）警報・注意報データを取得
        with urllib.request.urlopen(url=WARNING_URL, timeout=10) as res:
            warning_data = json.loads(res.read())

        warnings, report_datetime = parse_area_warnings(warning_data)

        return {
            "area_name": AREA_NAME,
            "warnings": warnings,
            "report_time": format_report_time(report_datetime),
            "last_fetch_time": get_japan_time()
        }

    except Exception:
        return {
            "area_name": AREA_NAME,
            "warnings": [],
            "report_time": "取得失敗",
            "last_fetch_time": get_japan_time(),
            "error": True
        }


# トップページ：templates/index.html を返す（住民向け指示も表示する）
@app.route('/')
def index():
    resident_notices = [i for i in instructions if i.get('target') == '住民']
    return render_template(
        'index.html',
        resident_notices=resident_notices,
        map_shelters=get_map_shelters()
    )

# ログインページ
@app.route('/login', methods=['GET', 'POST'])
def login():
    # リダイレクト先を取得（デフォルトは避難所登録画面）
    next_url = request.args.get('next') or request.form.get('next')

    # 安全でないURLの場合はデフォルトページにリダイレクト
    if not next_url or not is_safe_url(next_url):
        next_url = url_for('shelter_register')

    if request.method == 'POST':
        password = request.form.get('password', '').strip()

        # 認証チェック
        username = next(
            (name for name, registered_password in ADMIN_CREDENTIALS.items()
             if registered_password == password),
            None
        )
        if username:
            session['logged_in'] = True
            session['username'] = username
            # ログイン成功後は指定されたページにリダイレクト
            return redirect(next_url)
        return render_template('login.html', error=True, message="パスワードが正しくありません。", next=next_url)

    # ログイン済みの場合は指定されたページにリダイレクト
    if session.get('logged_in'):
        return redirect(next_url)

    return render_template('login.html', next=next_url)

# ログアウト
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# 避難所登録ページ
@app.route('/shelter_register', methods=['GET', 'POST'])
@login_required
def shelter_register():
    name = ''
    error = False
    success = False
    message = ''

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        registration_type = request.form.get('registration_type', 'pre')
        if registration_type == 'post':
            name = request.form.get('shelter_name', '').strip()

        if not name:
            error = True
            message = '避難所名を入力してください'
        elif registration_type == 'pre' and not any(shelter.get('name') == name for shelter in shelters):
            address = request.form.get('address', '').strip()
            now = datetime.now(JST).isoformat()
            new_shelter = {
                'id': max((shelter.get('id', 0) for shelter in shelters), default=0) + 1,
                'name': name,
                'address': address,
                'registered_at': now,
                'updated_at': now
            }
            shelters.append(new_shelter)
            if not save_shelters():
                shelters.pop()
                error = True
                message = '避難所情報を保存できませんでした。'
            else:
                success = True
                message = '避難所を新規登録しました！'
        elif not any(shelter.get('name') == name for shelter in shelters):
            error = True
            message = 'エラー：登録されていない避難所名です。'
        else:
            shelter = next(shelter for shelter in shelters if shelter.get('name') == name)
            if registration_type == 'post':
                operation = request.form.get('operation', 'update')
                if operation == 'delete':
                    shelter_index = shelters.index(shelter)
                    removed_shelter = shelters.pop(shelter_index)
                    if not save_shelters():
                        shelters.insert(shelter_index, removed_shelter)
                        error = True
                        message = '避難所を削除できませんでした。'
                    else:
                        success = True
                        message = '避難所を削除しました。'
                    return render_template(
                        'shelter_register.html',
                        shelters=shelters,
                        map_shelters=get_map_shelters(),
                        name=name,
                        error=error,
                        success=success,
                        message=message
                    )
                crowd_status = request.form.get('crowd_status', '').strip()
                supply_status = request.form.get('supply_status', '').strip()
                damage_status = request.form.get('damage_status', '').strip()
                opening_status = request.form.get('opening_status', '').strip()
                post_address = request.form.get('post_address', '').strip()
                if (crowd_status not in STATUS_VALUES
                        or supply_status not in STATUS_VALUES
                        or damage_status not in STATUS_VALUES):
                    error = True
                    message = '混雑状況、物資状況、被害状況を選択してください。'
                elif opening_status not in ('未開設', '開設準備中', '開設中'):
                    error = True
                    message = '開催状況ステータスを選択してください。'
                else:
                    shelter['crowd_status'] = crowd_status
                    shelter['supply_status'] = supply_status
                    shelter['damage_status'] = damage_status
                    shelter['opening_status'] = opening_status
                    if post_address:
                        shelter['address'] = post_address
                    shelter['updated_at'] = datetime.now(JST).isoformat()
                    if not save_shelters():
                        error = True
                        message = '避難所情報を保存できませんでした。'
                    else:
                        success = True
                        message = '事後登録を更新しました！'
            else:
                address = request.form.get('address', '').strip()
                shelter['address'] = address
                shelter['updated_at'] = datetime.now(JST).isoformat()
                if not save_shelters():
                    error = True
                    message = '避難所情報を保存できませんでした。'
                else:
                    success = True
                    message = '登録完了しました！'

    return render_template(
        'shelter_register.html',
        shelters=shelters,
        map_shelters=get_map_shelters(),
        name=name,
        error=error,
        success=success,
        message=message
    )

# 避難所検索ページ
@app.route('/shelter_search')
def shelter_search():
    return render_template(
        'shelter_search.html',
        map_shelters=get_map_shelters()
    )

# 全施設一覧ページ
@app.route('/all_shelters')
def all_shelters():
    return redirect(url_for('search_results'))


# 指示ボード：住民向けの指示を登録・一覧表示する
@app.route('/board', methods=['GET', 'POST'])
@login_required
def board():
    if request.method == 'POST':
        shelter = request.form.get('shelter', '').strip()
        content = request.form.get('content', '').strip()
        status = request.form.get('status', '').strip()

        if not shelter or not content or not status:
            resident_instructions = sorted(
                (i for i in instructions if i.get('target') == '住民'),
                key=lambda item: item.get('id', 0),
                reverse=True
            )
            return render_template(
                'board.html',
                instructions=resident_instructions,
                error='対象地域・避難所、災害情報・指示、緊急レベルを入力してください。',
                form_data=request.form
            )

        now = get_japan_time()
        instructions.append({
            'id': max((item.get('id', 0) for item in instructions), default=0) + 1,
            'target': '住民',
            'content': content,
            'shelter': shelter,
            'status': status,
            'created_at': now,
            'updated_at': now
        })

        if not save_instructions():
            instructions.pop()
            resident_instructions = sorted(
                (i for i in instructions if i.get('target') == '住民'),
                key=lambda item: item.get('id', 0),
                reverse=True
            )
            return render_template(
                'board.html',
                instructions=resident_instructions,
                error='発信内容を保存できませんでした。',
                form_data=request.form
            )

        return redirect(url_for('board'))

    resident_instructions = [i for i in instructions if i.get('target') == '住民']
    resident_instructions.sort(key=lambda item: item.get('id', 0), reverse=True)
    return render_template('board.html', instructions=resident_instructions, form_data={})


@app.route('/board/delete', methods=['POST'])
@login_required
def board_delete():
    selected_ids = {
        int(value)
        for value in request.form.getlist('instruction_ids')
        if value.isdigit()
    }
    if selected_ids:
        instructions[:] = [
            item for item in instructions
            if item.get('id') not in selected_ids
        ]
        save_instructions()
    return redirect(url_for('board'))

# 検索結果ページ：templates/search_results.html を返す
@app.route('/search_results')
def search_results():
    criteria = request.args.get('criteria', '').strip()
    criteria = CRITERIA_ALIASES.get(criteria, criteria)
    district = request.args.get('district')
    crowd_status = request.args.get('crowd_status', '').strip()
    supply_status = request.args.get('supply_status', '').strip()
    results = filter_shelters(district)
    if crowd_status in STATUS_VALUES:
        results = [s for s in results if s.get('crowd_status') == crowd_status]
    if supply_status in STATUS_VALUES:
        results = [s for s in results if s.get('supply_status') == supply_status]
    sort_field = SORT_FIELDS.get(criteria)
    if sort_field:
        results = sorted(
            results,
            key=lambda shelter: STATUS_RANK.get(shelter.get(sort_field), len(STATUS_VALUES))
        )

    page = request.args.get('page', 1, type=int)
    total_count = len(results)
    total_pages = max(1, math.ceil(total_count / RESULTS_PER_PAGE))
    page = min(max(page, 1), total_pages)
    start_index = (page - 1) * RESULTS_PER_PAGE
    end_index = min(start_index + RESULTS_PER_PAGE, total_count)
    reference_coordinates = get_reference_coordinates()
    enriched_results = [
        enrich_shelter(shelter, reference_coordinates)
        for shelter in results[start_index:end_index]
    ]

    query_params = request.args.to_dict(flat=True)
    query_params.pop('page', None)
    previous_url = url_for('search_results', **query_params, page=page - 1) if page > 1 else '#'
    next_url = url_for('search_results', **query_params, page=page + 1) if page < total_pages else '#'
    return render_template(
        'search_results.html',
        results=enriched_results,
        criteria=criteria,
        page=page,
        total_count=total_count,
        total_pages=total_pages,
        start_index=start_index + 1 if total_count else 0,
        end_index=end_index,
        query_params=query_params,
        previous_url=previous_url,
        next_url=next_url,
        reference_coordinates=reference_coordinates
    )

# JSON API：/shelters?district=地区名
@app.route('/shelters', methods=['GET'])
def get_shelters():
    results = filter_shelters(request.args.get('district'))

    if not results:
        # 見つからなければエラー JSON を返す
        return jsonify({'error': 'No shelters found'}), 404

    # 見つかったらリストを JSON で返す
    return jsonify(results)

# 気象警報・注意報API
@app.route('/api/weather_warnings')
def api_weather_warnings():
    """気象警報・注意報をJSON形式で返すAPI"""
    return jsonify(get_weather_warnings())

if __name__ == '__main__':
    app.run(debug=True, port=5000)
